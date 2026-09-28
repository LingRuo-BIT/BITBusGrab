"""北京理工统一身份认证的最小客户端。

认证协议按 BIT101-dev/BIT-Login-Python 的 SSO 实现移植；本文件只保留
账号密码登录、USTC 风控、短信二次验证和 CAS ticket 处理。
"""

import base64
import hashlib
import json
import secrets
import string
import time
from dataclasses import dataclass
from html.parser import HTMLParser
from typing import Any, Callable, Mapping, Optional
from urllib.parse import parse_qs, quote, urlencode, urljoin, urlparse

import requests
from Crypto.Cipher import AES, PKCS1_v1_5
from Crypto.PublicKey import RSA
from Crypto.Util.Padding import pad, unpad


URL_CRYPTO_PUBLIC_KEY = """-----BEGIN PUBLIC KEY-----
MIIBIjANBgkqhkiG9w0BAQEFAAOCAQ8AMIIBCgKCAQEAjVr1zKwohU3xA0afprWLSQvIymaSH/V27MedFc+CecXSnORIFMAp4uEIb4taDq/2X4eMeTI66Mu/rB5GKSFDbExF2Gu4NaO/CNDpf1gHMScUrIFCh4CDqzBnx17kclvezLkIK0T8FVa4cRsINvzjbnA6jUSMaf6Fm1n9wTAtW6QYBjssGOEtCj+c38PTBdFMmJbXp3brt1tEBesz6lb3Fjp76FGvDZ08xtYG8fxYPuiMwKU04eS+mcX/BunwgpU3zwekHYB+PWRIvq0lBry9Wms25sJE5T/RAv5fEuMLbBkfcZK3+7ivSZthTmPpr2Ap/ji70ZZ6u2jvR5VJq+LJHQIDAQAB
-----END PUBLIC KEY-----"""


class SsoError(RuntimeError):
    """统一身份认证流程失败。"""


@dataclass(frozen=True)
class LoginPage:
    execution: str
    crypto_key: str
    form_action: str
    risk_system: str
    target_system: str
    site_id: str


@dataclass(frozen=True)
class SecondFactorPage:
    execution: str
    form_action: str
    user_object_id: str
    phone: str = ""


class _LoginHtmlParser(HTMLParser):
    wanted = {
        "login-page-flowkey",
        "login-croypto",
        "riskSystemSwitch",
        "targetSystem",
        "siteId",
        "user-object-id",
        "phone-number",
    }

    def __init__(self):
        super().__init__(convert_charrefs=True)
        self.values: dict[str, str] = {}
        self.form_action = ""
        self._capture: Optional[str] = None
        self._parts: list[str] = []

    def handle_starttag(self, tag, attrs):
        attrs_dict = dict(attrs)
        element_id = attrs_dict.get("id")
        if element_id in self.wanted:
            value = attrs_dict.get("value")
            if value is not None:
                self.values[element_id] = str(value).strip()
            else:
                self._capture = element_id
                self._parts = []
        if tag.lower() == "form" and not self.form_action:
            self.form_action = attrs_dict.get("action") or ""

    def handle_data(self, data):
        if self._capture is not None:
            self._parts.append(data)

    def handle_endtag(self, tag):
        if self._capture is not None and tag.lower() in {"p", "div", "span"}:
            self.values[self._capture] = "".join(self._parts).strip()
            self._capture = None
            self._parts = []


def _parse_html(html: str, response_url: str):
    parser = _LoginHtmlParser()
    parser.feed(html)
    values = parser.values
    execution = values.get("login-page-flowkey", "")
    crypto_key = values.get("login-croypto", "")
    page = None
    if execution and crypto_key:
        page = LoginPage(
            execution=execution,
            crypto_key=crypto_key,
            form_action=urljoin(response_url, parser.form_action or "login"),
            risk_system=values.get("riskSystemSwitch", ""),
            target_system=values.get("targetSystem", ""),
            site_id=values.get("siteId", ""),
        )
    return page, values


def _parse_second_factor(html: str, response_url: str):
    parser = _LoginHtmlParser()
    parser.feed(html)
    values = parser.values
    if not values.get("login-page-flowkey") or not values.get("user-object-id"):
        return None
    if not any(marker in html for marker in ("secondSmsLoginForm", "second-auth-tip", "cas-gateway")):
        return None
    return SecondFactorPage(
        execution=values["login-page-flowkey"],
        form_action=urljoin(response_url, parser.form_action or "login"),
        user_object_id=values["user-object-id"],
        phone=values.get("phone-number", ""),
    )


def protected_csrf_headers() -> dict[str, str]:
    key = "".join(secrets.choice(string.ascii_letters + string.digits) for _ in range(32))
    encoded = base64.b64encode(key.encode("ascii")).decode("ascii")
    midpoint = len(encoded) // 2
    mixed = encoded[:midpoint] + encoded + encoded[midpoint:]
    return {
        "Csrf-Key": key,
        "Csrf-Value": hashlib.md5(mixed.encode("ascii")).hexdigest(),
    }


def encrypt_aes_base64(plaintext: str, encoded_key: str) -> str:
    key = base64.b64decode(encoded_key, validate=True)
    return base64.b64encode(
        AES.new(key, AES.MODE_ECB).encrypt(pad(plaintext.encode("utf-8"), AES.block_size))
    ).decode("ascii")


def encrypt_url_crypto_body(value: Mapping[str, Any]):
    aes_key = secrets.token_bytes(16)
    plaintext = json.dumps(value, ensure_ascii=False, separators=(",", ":")).encode("utf-8")
    body = base64.b64encode(
        AES.new(aes_key, AES.MODE_ECB).encrypt(pad(plaintext, AES.block_size))
    ).decode("ascii")
    encrypted_key = PKCS1_v1_5.new(RSA.import_key(URL_CRYPTO_PUBLIC_KEY)).encrypt(
        base64.b64encode(aes_key)
    )
    return body, base64.b64encode(encrypted_key).decode("ascii"), aes_key


def decrypt_url_crypto_response(value: str, aes_key: bytes):
    current: Any = value
    for _ in range(4):
        if not isinstance(current, str):
            return current
        try:
            parsed = json.loads(current)
        except json.JSONDecodeError:
            parsed = current
        if isinstance(parsed, str) and parsed != current:
            current = parsed
            continue
        if not isinstance(parsed, str):
            return parsed
        try:
            plaintext = unpad(
                AES.new(aes_key, AES.MODE_ECB).decrypt(base64.b64decode(parsed, validate=True)),
                AES.block_size,
            ).decode("utf-8")
        except (ValueError, TypeError, UnicodeDecodeError):
            return current
        try:
            current = json.loads(plaintext)
        except json.JSONDecodeError:
            current = plaintext
    return current


def _json_compact(value: Mapping[str, Any]) -> str:
    return json.dumps(value, ensure_ascii=False, separators=(",", ":"))


def _response_message(value: Any) -> str:
    if not isinstance(value, Mapping):
        return ""
    for key in ("message", "msg", "errorMessage"):
        if value.get(key):
            return str(value[key])
    data = value.get("data")
    if isinstance(data, Mapping):
        for key in ("message", "msg", "errorMessage"):
            if data.get(key):
                return str(data[key])
    return ""


SmsCallback = Callable[[str], str]


class BitSsoClient:
    """按 BIT-Login-Python 的请求顺序执行一次 CAS 密码登录。"""

    def __init__(self, session: requests.Session, base_url="https://sso.bit.edu.cn", timeout=15):
        self.session = session
        self.base_url = base_url.rstrip("/")
        self.cas_url = self.base_url + "/cas"
        self.timeout = timeout
        self.login_referer = self.cas_url + "/login"
        self.last_execution = ""
        self.session.headers.update(
            {
                "Accept-Language": "zh-CN,zh;q=0.9,en;q=0.7",
                "sec-ch-ua": '"Not;A=Brand";v="8", "Chromium";v="140", "Google Chrome";v="140"',
                "sec-ch-ua-mobile": "?0",
                "sec-ch-ua-platform": '"Windows"',
            }
        )

    def _request(self, method, url, *, allow_redirects=True, cache_bust=True,
                 raise_for_status=True, **kwargs):
        method = method.upper()
        headers = dict(kwargs.pop("headers", {}) or {})
        headers.setdefault("Referer", self.login_referer)
        if method not in {"GET", "HEAD", "OPTIONS"}:
            headers.setdefault("Origin", self.base_url)
        if "protected" in url:
            headers.update(protected_csrf_headers())
            headers.setdefault("Sid-Language", "zh_CN")
        if method == "GET" and cache_bust:
            params = dict(kwargs.pop("params", {}) or {})
            if params:
                separator = "&" if "?" in url else "?"
                url += separator + urlencode(params, doseq=True)
            url += ("&" if "?" in url else "?") + str(int(time.time() * 1000))
        response = self.session.request(
            method,
            url,
            headers=headers,
            timeout=self.timeout,
            allow_redirects=allow_redirects,
            **kwargs,
        )
        if raise_for_status and response.status_code >= 400:
            response.raise_for_status()
        return response

    def _json(self, method, url, **kwargs):
        response = self._request(method, url, **kwargs)
        try:
            return response.json()
        except (ValueError, TypeError) as error:
            raise SsoError(f"统一认证接口返回了无法识别的数据：{url}") from error

    def login_password(self, username: str, password: str, *, service: str,
                       sms_callback: Optional[SmsCallback] = None,
                       trust_device: bool = False, follow_redirects=True):
        username = username.strip()
        if not username or not password:
            raise ValueError("学号和密码不能为空")

        response = self._request(
            "GET", f"{self.cas_url}/login", params={"service": service},
            cache_bust=False, allow_redirects=follow_redirects,
        )
        page, _ = _parse_html(response.text, response.url)
        if page is None:
            if self.extract_ticket(response):
                return response
            raise SsoError("统一认证登录页缺少 execution 或 croypto 字段")
        self.login_referer = response.url
        self.last_execution = page.execution

        captcha_info = self._json(
            "GET",
            f"{self.cas_url}/api/protected/user/findCaptchaCount/{quote(username, safe='')}",
        )
        captcha_data = captcha_info.get("data") if isinstance(captcha_info, Mapping) else None
        if isinstance(captcha_data, Mapping) and captcha_data.get("captchaInvisible"):
            raise SsoError("统一认证要求图形验证码，当前程序未配置 OCR")

        form = {
            "type": "UsernamePassword",
            "_eventId": "submit",
            "geolocation": "",
            "execution": page.execution,
            "username": username,
            "croypto": page.crypto_key,
            "captcha_code": "",
            "password": encrypt_aes_base64(password, page.crypto_key),
            "captcha_payload": encrypt_aes_base64("{}", page.crypto_key),
        }
        if page.risk_system.upper() == "USTC":
            form.update(self._risk_fields(page, username))

        response = self._request(
            "POST", page.form_action, data=form, allow_redirects=follow_redirects,
            raise_for_status=False,
        )
        if response.status_code >= 400 and not self._is_cas_login(response):
            response.raise_for_status()

        second_factor = _parse_second_factor(response.text, response.url)
        if second_factor is not None:
            return self._complete_sms(
                username, second_factor, sms_callback, trust_device, follow_redirects
            )
        return response

    def _risk_fields(self, page: LoginPage, username: str):
        cookie_value = self._cookie("device")
        if not cookie_value:
            cookie_value = hashlib.sha256(str(int(time.time() * 1000)).encode()).hexdigest()
            self.session.cookies.set("device", cookie_value, path="/")
        ua = str(self.session.headers.get("User-Agent", ""))
        values = {
            "fonts": json.dumps(["Arial", "Helvetica Neue", "PingFang SC", "Times New Roman"], separators=(",", ":")),
            "deviceMemory": "16",
            "hardwareConcurrency": "10",
            "timezone": json.dumps("Asia/Shanghai"),
            "cpuClass": json.dumps("not available"),
            "platform": json.dumps("MacIntel"),
            "language": json.dumps("zh-CN"),
            "screenResolution": json.dumps([956, 1470], separators=(",", ":")),
        }
        fingerprint = {
            "fonts": hashlib.sha256(values["fonts"].encode()).hexdigest(),
            "deviceMemory": hashlib.sha256(values["deviceMemory"].encode()).hexdigest(),
            "hardwareConcurrency": hashlib.sha256(values["hardwareConcurrency"].encode()).hexdigest(),
            "localgroupId": self._cookie("riskSystemGroupId"),
            "timezone": values["timezone"],
            "cpuClass": hashlib.sha256(values["cpuClass"].encode()).hexdigest(),
            "platform": hashlib.sha256(values["platform"].encode()).hexdigest(),
            "language": values["language"],
            "screenResolution": values["screenResolution"],
            "fingerprint": hashlib.sha256("".join(values.values()).encode()).hexdigest(),
            "cookieValue": cookie_value,
            "userAgent": ua,
            "platformAuthenticator": "support",
        }
        try:
            result = self._json(
                "POST", f"{self.base_url}/ustc-rba-front/fp",
                headers={"Origin": self.base_url}, json=fingerprint,
            )
            token = result.get("responsetoken") if isinstance(result, Mapping) else None
            if not token and isinstance(result, Mapping) and isinstance(result.get("data"), Mapping):
                token = result["data"].get("responsetoken")
            payload = {"token": token, "groupId": self._cookie("riskSystemGroupId")} if token else {"error": True}
        except (requests.RequestException, SsoError):
            payload = {"error": True}
        return {
            "risk_payload": encrypt_aes_base64(_json_compact(payload), page.crypto_key),
            "targetSystem": page.target_system or "sso",
            "siteId": page.site_id or "sourceId",
            "riskEngine": "true",
        }

    def _complete_sms(self, username, page, callback, trust_device, follow_redirects):
        self.login_referer = self.cas_url + "/"
        phone_data = self._second_factor_phone(page)
        opaque_phone = self._string(phone_data, "tel") or page.phone
        masked_phone = self._string(phone_data, "maskTel")
        if not opaque_phone:
            raise SsoError("二次认证页面没有提供绑定手机标识")
        sent = self._json(
            "POST", f"{self.cas_url}/api/protected/sms/publicNoToken/sendSmsCode",
            json={"phone": opaque_phone, "businessNo": "0008"},
        )
        if str(sent.get("code")) != "200" and not self._sms_still_valid(sent):
            raise SsoError(_response_message(sent) or "短信验证码发送失败")
        if callback is None:
            callback = lambda target: input(f"请输入发送到 {target or '绑定手机'} 的短信验证码：").strip()
        code = str(callback(masked_phone or "绑定手机")).strip()
        if not code:
            raise SsoError("短信验证码不能为空")
        checked = self._json(
            "POST", f"{self.cas_url}/api/protected/sms/checkToken",
            json={
                "phone": opaque_phone,
                "token": code,
                "delete": False,
                "trustDevice": bool(trust_device),
            },
        )
        if str(checked.get("code")) != "200":
            raise SsoError(_response_message(checked) or "短信验证码错误或已失效")
        form = {
            "username": username,
            "password": code,
            "type": "smsLogin",
            "_eventId": "submit",
            "geolocation": "",
            "execution": page.execution,
            "captcha_code": "",
            "trustDevice": str(bool(trust_device)).lower(),
        }
        response = self._request(
            "POST", page.form_action, data=form, allow_redirects=follow_redirects,
            raise_for_status=False,
        )
        if response.status_code >= 400 and not self._is_cas_login(response):
            response.raise_for_status()
        return response

    def _second_factor_phone(self, page):
        body, encrypted_key, aes_key = encrypt_url_crypto_body({"userId": page.user_object_id})
        response = self._request(
            "POST", f"{self.cas_url}/api/protected/sms/getPhoneNumberByUserId",
            headers={"Content-Type": "application/json", "hasCrypto": "true", "privateKey": encrypted_key},
            data=body,
        )
        if not response.text:
            raise SsoError("手机号查询接口返回空响应")
        unpacked = decrypt_url_crypto_response(response.text, aes_key)
        if isinstance(unpacked, Mapping):
            return unpacked.get("data")
        return unpacked

    def _cookie(self, name):
        try:
            return str(self.session.cookies.get(name) or "")
        except (KeyError, TypeError):
            return ""

    @staticmethod
    def _string(value, key):
        return str(value.get(key)) if isinstance(value, Mapping) and value.get(key) else ""

    @staticmethod
    def _sms_still_valid(value):
        message = _response_message(value)
        return "验证码" in message and "有效期内" in message and "重复发送" in message

    @staticmethod
    def _is_cas_login(response):
        return urlparse(str(response.url or "")).path.rstrip("/").endswith("/cas/login")

    @staticmethod
    def extract_ticket(response):
        candidates = list(getattr(response, "history", ()) or ()) + [response]
        for item in candidates:
            urls = [str(getattr(item, "url", "") or "")]
            location = getattr(item, "headers", {}).get("Location") if getattr(item, "headers", None) else None
            if location:
                urls.append(urljoin(urls[0], str(location)))
            for url in urls:
                ticket = next(iter(parse_qs(urlparse(url).query).get("ticket", [])), None)
                if ticket:
                    return ticket
        return None

    @classmethod
    def callback_from_response(cls, response, service):
        candidates = list(getattr(response, "history", ()) or ()) + [response]
        for item in candidates:
            location = getattr(item, "headers", {}).get("Location") if getattr(item, "headers", None) else None
            if location:
                callback = urljoin(str(getattr(item, "url", "") or ""), str(location))
                if cls.extract_ticket(item) or "ticket=" in callback:
                    return callback
        ticket = cls.extract_ticket(response)
        if ticket:
            return f"{service}{'&' if '?' in service else '?'}ticket={quote(ticket, safe='')}"
        raise SsoError("CAS 没有返回 service ticket")
