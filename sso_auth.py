"""北理 CAS 登录与班车 OAuth code 兑换。"""

import re
import secrets
import threading
import time
from dataclasses import dataclass, field
from typing import Callable, Optional
from urllib.parse import parse_qs, urljoin, urlparse

import requests

from api.shuttle import UserToken
from sso_client import BitSsoClient, SsoError


SSO_BASE_URL = "https://sso.bit.edu.cn"
BUS_HOST = "hqapp1.bit.edu.cn"
BUS_CALLBACK = f"http://{BUS_HOST}/newbanche/"
OAUTH_CLIENT_ID = "BCFW"
OAUTH_AUTHORIZE_URL = f"{SSO_BASE_URL}/cas/oauth2.0/authorize"
BUS_AUTH_LOGIN_URL = f"http://{BUS_HOST}/vehicle/auth-login"
SMS_WAIT_SECONDS = 300


class BusSsoError(RuntimeError):
    """班车账号认证失败。"""


def _is_bus_callback(url: str) -> bool:
    parsed = urlparse(url)
    return (
        parsed.scheme == "http"
        and (parsed.hostname or "").lower() == BUS_HOST
        and parsed.path.rstrip("/") == "/newbanche"
    )


def _extract_oauth_code(response: requests.Response) -> Optional[str]:
    """只接受班车网页登记的回调地址上的 OAuth code。"""
    candidates = list(getattr(response, "history", ()) or ()) + [response]
    for item in candidates:
        urls = [str(getattr(item, "url", "") or "")]
        headers = getattr(item, "headers", {}) or {}
        location = headers.get("Location")
        if location:
            urls.append(urljoin(urls[0], str(location)))
        for candidate in urls:
            if not _is_bus_callback(candidate):
                continue
            code = next(iter(parse_qs(urlparse(candidate).query).get("code", [])), None)
            if code:
                return code
    return None


def _start_oauth_login(session: requests.Session) -> str:
    """初始化班车 OAuth，并取出 CAS 页面实际要求的 service 地址。"""
    params = {
        "response_type": "code",
        "client_id": OAUTH_CLIENT_ID,
        "redirect_uri": BUS_CALLBACK,
    }
    response = session.get(
        OAUTH_AUTHORIZE_URL,
        params=params,
        timeout=15,
        allow_redirects=False,
    )
    if response.status_code not in {301, 302, 303, 307, 308}:
        raise BusSsoError("班车授权入口没有跳转到统一身份认证")

    login_url = urljoin(response.url, response.headers.get("Location", ""))
    parsed = urlparse(login_url)
    if (parsed.hostname or "").lower() != "sso.bit.edu.cn" or parsed.path.rstrip("/") != "/cas/login":
        raise BusSsoError("统一身份认证没有返回预期的 CAS 登录地址")

    service = next(iter(parse_qs(parsed.query).get("service", [])), None)
    if not service:
        raise BusSsoError("统一身份认证跳转中缺少 service 参数")

    service_url = urlparse(service)
    if (
        service_url.scheme != "https"
        or (service_url.hostname or "").lower() != "sso.bit.edu.cn"
        or service_url.path.rstrip("/") != "/cas/oauth2.0/callbackAuthorize"
    ):
        raise BusSsoError("班车 OAuth 返回了非预期的 CAS service 地址")
    return service


def authenticate_bus_account(
    username: str,
    password: str,
    sms_callback: Optional[Callable[[str], str]] = None,
) -> str:
    """完成 CAS 密码认证、班车 OAuth code 兑换，并返回班车 userid。"""
    session = requests.Session()
    session.trust_env = False
    try:
        service = _start_oauth_login(session)
        client = BitSsoClient(session, base_url=SSO_BASE_URL, timeout=15)
        try:
            response = client.login_password(
                username,
                password,
                service=service,
                sms_callback=sms_callback,
                trust_device=False,
                follow_redirects=True,
            )
        except requests.HTTPError as error:
            # 班车回调页偶尔返回非 2xx；只要 CAS 已经把 code 交回登记的
            # 班车回调地址，就继续由班车 auth-login 接口完成兑换。
            response = error.response
            if response is None or not _extract_oauth_code(response):
                raise BusSsoError("统一身份认证未能完成班车 OAuth 回调") from error

        code = _extract_oauth_code(response)
        if not code:
            raise BusSsoError("统一身份认证未返回班车 OAuth code")

        token = UserToken()
        headers = {
            "Accept": "application/json",
            "apitoken": token.api_token,
            "apitime": token.api_time,
        }
        try:
            exchange = session.get(
                BUS_AUTH_LOGIN_URL,
                params={"code": code},
                headers=headers,
                timeout=15,
            )
            exchange.raise_for_status()
            result = exchange.json()
        except (requests.RequestException, ValueError) as error:
            # 不把包含一次性 code 的请求 URL 放进界面错误信息。
            raise BusSsoError("班车系统未能完成登录信息兑换") from error

        if not isinstance(result, dict) or str(result.get("code")) != "1":
            message = result.get("message") if isinstance(result, dict) else None
            if message:
                message = re.sub(
                    r"(?i)(code|ticket|token|password)=([^&\s]+)",
                    r"\1=[已隐藏]",
                    str(message),
                )
                raise BusSsoError(message[:200])
            raise BusSsoError("班车系统拒绝了这次登录")

        data = result.get("data")
        userid = data.get("userid") if isinstance(data, dict) else None
        if not userid:
            raise BusSsoError("班车登录成功响应中缺少用户标识")
        return str(userid)
    except requests.RequestException as error:
        raise BusSsoError("无法连接统一身份认证或班车服务") from error
    finally:
        session.close()


@dataclass
class _LoginJob:
    job_id: str
    status: str = "starting"
    message: str = "正在连接统一身份认证…"
    phone: str = ""
    condition: threading.Condition = field(default_factory=threading.Condition, repr=False)
    sms_code: Optional[str] = field(default=None, repr=False)


class SsoLoginManager:
    """在后台完成登录，并在需要时等待网页端提交短信验证码。"""

    TERMINAL_STATES = {"succeeded", "failed"}

    def __init__(self, on_authenticated: Callable[[str], None]):
        self._on_authenticated = on_authenticated
        self._lock = threading.Lock()
        self._jobs: dict[str, _LoginJob] = {}
        self._active_job: Optional[_LoginJob] = None

    def start(self, username: str, password: str) -> str:
        if not isinstance(username, str) or not isinstance(password, str):
            raise ValueError("请输入统一身份认证账号和密码")
        username = username.strip()
        if not username or not password:
            raise ValueError("请输入统一身份认证账号和密码")

        with self._lock:
            if self._active_job is not None:
                with self._active_job.condition:
                    if self._active_job.status not in self.TERMINAL_STATES:
                        raise BusSsoError("已有一次登录正在进行")

            for old_id in list(self._jobs):
                if old_id != (self._active_job.job_id if self._active_job else None):
                    self._jobs.pop(old_id, None)

            job = _LoginJob(job_id=secrets.token_urlsafe(18))
            self._jobs[job.job_id] = job
            self._active_job = job
            worker = threading.Thread(
                target=self._run,
                args=(job, username, password),
                name="bit-sso-login",
                daemon=True,
            )
            worker.start()
            return job.job_id

    def status(self, job_id: str):
        with self._lock:
            job = self._jobs.get(job_id)
        if job is None:
            return None
        with job.condition:
            return {
                "status": job.status,
                "message": job.message,
                "phone": job.phone,
            }

    def submit_sms(self, job_id: str, code: str):
        code = (code or "").strip()
        if not code.isdigit() or not 4 <= len(code) <= 8:
            raise ValueError("短信验证码应为4至8位数字")

        with self._lock:
            job = self._jobs.get(job_id)
        if job is None:
            raise BusSsoError("登录请求已失效，请重新登录")

        with job.condition:
            if job.status != "waiting_sms" or job.sms_code is not None:
                raise BusSsoError("当前登录请求不在等待短信验证码")
            job.sms_code = code
            job.status = "authenticating"
            job.message = "正在验证短信验证码…"
            job.condition.notify_all()

    def _wait_for_sms(self, job: _LoginJob, masked_phone: str) -> str:
        deadline = time.monotonic() + SMS_WAIT_SECONDS
        with job.condition:
            job.phone = masked_phone or "绑定手机"
            job.status = "waiting_sms"
            job.message = "短信验证码已发送，请输入验证码"
            job.condition.notify_all()

            while job.sms_code is None:
                remaining = deadline - time.monotonic()
                if remaining <= 0:
                    job.status = "failed"
                    job.message = "等待短信验证码超时，请重新登录"
                    raise SsoError(job.message)
                job.condition.wait(remaining)

            code = job.sms_code
            job.sms_code = None
            job.status = "authenticating"
            job.message = "正在验证短信验证码…"
            return code

    def _run(self, job: _LoginJob, username: str, password: str):
        try:
            userid = authenticate_bus_account(
                username,
                password,
                sms_callback=lambda phone: self._wait_for_sms(job, phone),
            )
            self._on_authenticated(userid)
            with job.condition:
                job.status = "succeeded"
                job.message = "班车账号登录成功"
                job.phone = ""
        except Exception as error:
            message = str(error) if isinstance(error, (BusSsoError, SsoError, ValueError)) else "登录过程遇到网络或服务错误"
            message = re.sub(
                r"(?i)(code|ticket|token|password)=([^&\s]+)",
                r"\1=[已隐藏]",
                message,
            )
            with job.condition:
                job.status = "failed"
                job.message = message[:240]
                job.phone = ""
        finally:
            with self._lock:
                if self._active_job is job:
                    self._active_job = None
