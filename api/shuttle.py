# coding=utf-8
"""
    @Author：零 若
    @file： shuttle.py
    @date：2025/10/29 20:46
    @Python  : 3.10.18
    别放弃，即使前方荆棘成林！
"""

import json
import hashlib
import logging
import time
from dataclasses import dataclass
from datetime import datetime
from enum import Enum
from typing import List, Dict, Optional, Any
from urllib.parse import quote

import requests
from requests.adapters import HTTPAdapter
from urllib3.util.retry import Retry


# ==================== 自定义异常 ====================

class ShuttleAPIError(Exception):
    """班车 API 基础异常"""
    pass


class AuthenticationError(ShuttleAPIError):
    """认证失败异常"""
    pass


class ValidationError(ShuttleAPIError):
    """参数验证失败异常"""
    pass


class NetworkError(ShuttleAPIError):
    """网络请求失败异常"""
    pass


class BusinessError(ShuttleAPIError):
    """业务逻辑错误异常"""
    pass


# ==================== 枚举类型 ====================

class HttpMethod(str, Enum):
    """HTTP 方法枚举"""
    GET = "GET"
    POST = "POST"


class RouteDirection(str, Enum):
    """班车路线方向枚举"""
    LIANGXIANG_TO_ZHONGGUANCUN = "良乡校区->中关村校区"
    ZHONGGUANCUN_TO_LIANGXIANG = "中关村校区->良乡校区"
    ZHONGGUANCUN_TO_XISHAN = "中关村校区->西山校区"
    XISHAN_TO_ZHONGGUANCUN = "西山校区->中关村校区"
    ZHONGGUANCUN_TO_HUILONGGUAN = "中关村校区->回龙观"
    HUILONGGUAN_TO_ZHONGGUANCUN = "回龙观->中关村校区"
    ZHONGGUANCUN_TO_FANGSHAN = "中关村校区->房山分校阎村"
    FANGSHAN_TO_ZHONGGUANCUN = "房山分校阎村->中关村校区"
    LIANGXIANG_TO_HUILONGGUAN = "良乡校区->回龙观"
    HUILONGGUAN_TO_LIANGXIANG = "回龙观->良乡校区"


# ==================== 数据类 ====================

@dataclass
class UserToken:
    """按班车网页的前端算法生成当前请求令牌。"""
    api_token: str = ""
    api_time: str = ""

    def __post_init__(self):
        self.refresh()

    def refresh(self):
        """生成 apitime 和双重 MD5 apitoken；前端每个请求都会重新计算。"""
        self.api_time = str(int(time.time() * 1000))
        first_hash = hashlib.md5(
            f"oVw5lgBQ32mygdfZUvYuKAbYtm7DRN37{self.api_time}".encode("utf-8")
        ).hexdigest()
        self.api_token = hashlib.md5(first_hash.encode("ascii")).hexdigest()
        return self

    def is_valid(self) -> bool:
        """检查令牌是否有效"""
        return bool(self.api_token and self.api_time)


@dataclass
class APIConfig:
    """API 配置类"""
    host: str
    timeout: int = 10
    max_retries: int = 3
    retry_backoff_factor: float = 0.5
    debug: bool = False

    def __post_init__(self):
        """初始化后验证"""
        if not self.host or not self.host.strip():
            raise ValidationError("host cannot be empty")
        if self.timeout <= 0:
            raise ValidationError("timeout must be positive")
        if self.max_retries < 0:
            raise ValidationError("max_retries cannot be negative")

    @property
    def base_url(self) -> str:
        """获取基础 URL"""
        return f"http://{self.host.strip()}"


# ==================== 工具函数 ====================

def validate_date_format(date_str: str) -> bool:
    """
    验证日期格式是否为 YYYY-MM-DD

    Args:
        date_str: 日期字符串

    Returns:
        是否有效
    """
    try:
        datetime.strptime(date_str, "%Y-%m-%d")
        return True
    except ValueError:
        return False


def sanitize_log_data(data: str, max_length: int = 50) -> str:
    """
    脱敏日志数据

    Args:
        data: 原始数据
        max_length: 最大显示长度

    Returns:
        脱敏后的数据
    """
    if not data:
        return "(empty)"
    if len(data) <= max_length:
        return data
    return f"{data[:max_length // 2]}...{data[-max_length // 2:]}"


# ==================== 核心 API 类 ====================

class ShuttleAPI:
    """班车 API 客户端"""

    def __init__(self, config: APIConfig):
        """
        初始化 API 客户端

        Args:
            config: API 配置对象
        """
        self.config = config
        self._setup_logging()
        self._session = self._create_session()

    def _setup_logging(self):
        """配置日志系统"""
        self.logger = logging.getLogger(__name__)

        if not self.logger.handlers:
            handler = logging.StreamHandler()
            formatter = logging.Formatter(
                '%(asctime)s - %(name)s - %(levelname)s - %(message)s'
            )
            handler.setFormatter(formatter)
            self.logger.addHandler(handler)

        self.logger.setLevel(logging.DEBUG if self.config.debug else logging.INFO)

    def _create_session(self) -> requests.Session:
        """
        创建带重试机制的 requests Session

        Returns:
            配置好的 Session 对象
        """
        session = requests.Session()

        # 配置重试策略
        retry_strategy = Retry(
            total=self.config.max_retries,
            backoff_factor=self.config.retry_backoff_factor,
            status_forcelist=[429, 500, 502, 503, 504],
            allowed_methods=["GET", "POST"]
        )

        adapter = HTTPAdapter(max_retries=retry_strategy)
        session.mount("http://", adapter)
        session.mount("https://", adapter)

        return session

    def _get_default_headers(self, token: UserToken) -> Dict[str, str]:
        """
        获取默认请求头

        Args:
            token: 用户令牌

        Returns:
            请求头字典
        """
        token.refresh()
        return {
            "Host": self.config.host,
            "Accept": "application/json",
            "apitoken": token.api_token,
            "apitime": token.api_time
        }

    def _make_request(
            self,
            method: HttpMethod,
            url: str,
            headers: Dict[str, str],
            body: Optional[str] = None
    ) -> bytes:
        """
        构建 HTTP 请求并返回响应体

        Args:
            method: HTTP 方法
            url: 请求 URL
            headers: 请求头
            body: 请求体（可选）

        Returns:
            响应体字节数组

        Raises:
            NetworkError: 网络请求失败
            AuthenticationError: 认证失败
        """
        self.logger.debug(f"{method.value} {url}")
        self.logger.debug(f"Headers: {self._sanitize_headers(headers)}")

        if body:
            self.logger.debug(f"Body: {body[:200]}...")

        try:
            start_time = time.time()

            if method == HttpMethod.GET:
                response = self._session.get(
                    url,
                    headers=headers,
                    timeout=self.config.timeout
                )
            elif method == HttpMethod.POST:
                response = self._session.post(
                    url,
                    headers=headers,
                    data=body,
                    timeout=self.config.timeout
                )
            else:
                raise ValidationError(f"Unsupported HTTP method: {method}")

            elapsed_time = time.time() - start_time
            self.logger.debug(
                f"Response: status={response.status_code}, "
                f"time={elapsed_time:.2f}s, "
                f"size={len(response.content)} bytes"
            )

            # 处理特定的 HTTP 状态码
            if response.status_code == 401:
                raise AuthenticationError("Authentication failed: invalid token")
            elif response.status_code == 403:
                raise AuthenticationError("Access forbidden: insufficient permissions")

            response.raise_for_status()
            return response.content

        except requests.Timeout as e:
            raise NetworkError(f"Request timeout after {self.config.timeout}s: {str(e)}")
        except requests.ConnectionError as e:
            raise NetworkError(f"Connection error: {str(e)}")
        except requests.RequestException as e:
            raise NetworkError(f"Request failed: {str(e)}")

    def _sanitize_headers(self, headers: Dict[str, str]) -> Dict[str, str]:
        """脱敏请求头中的敏感信息"""
        sanitized = headers.copy()
        for key in ["apitoken", "apitime"]:
            if key in sanitized:
                sanitized[key] = sanitize_log_data(sanitized[key], 5)
        return sanitized

    def _parse_json_response(self, body: bytes, operation: str) -> Dict[str, Any]:
        """
        解析 JSON 响应

        Args:
            body: 响应体
            operation: 操作描述(用于错误信息)

        Returns:
            解析后的字典

        Raises:
            BusinessError: JSON 解析失败
        """
        try:
            return json.loads(body)
        except (ValueError, json.JSONDecodeError) as e:
            self.logger.error(f"Failed to parse JSON for {operation}: {str(e)}")
            self.logger.debug(f"Response body: {body[:500]}")
            raise BusinessError(f"Invalid JSON response for {operation}: {str(e)}")

    def get_shuttle_list(
            self,
            date: str,
            address: str,
            userid: str,
            token: UserToken
    ) -> List[Dict[str, Any]]:
        """
        获取班车列表

        Args:
            date: 日期，格式 YYYY-MM-DD
            address: 地址/路线
            userid: 用户 ID
            token: 用户令牌

        Returns:
            班车信息列表

        Raises:
            ValidationError: 参数验证失败
            AuthenticationError: 认证失败
            NetworkError: 网络请求失败
            BusinessError: 业务逻辑错误
        """
        # 参数验证
        if not validate_date_format(date):
            raise ValidationError(f"Invalid date format: {date}, expected YYYY-MM-DD")

        if not address or not address.strip():
            raise ValidationError("address cannot be empty")

        if token and not token.is_valid():
            raise ValidationError("Invalid token provided")

        if not userid or not userid.strip():
            raise ValidationError("userid cannot be empty")

        # 构建请求
        encoded_address = quote(address)
        url = (
            f"{self.config.base_url}/vehicle/get-list"
            f"?page=1&limit=20&date={date}"
            f"&address={encoded_address}&userid={userid}"
        )

        headers = self._get_default_headers(token)
        body = self._make_request(HttpMethod.GET, url, headers)

        # 解析响应
        response_data = self._parse_json_response(body, "get_shuttle_list")

        # 检查业务错误码
        code = response_data.get("code")
        if code == "SYS_UNKNOWN":
            raise BusinessError("System error occurred")

        data = response_data.get("data", [])
        self.logger.info(f"Retrieved {len(data)} shuttles for {date} ({address})")

        return data

    def get_reserved_seats(
            self,
            shuttle_id: str,
            date: str,
            userid: str,
            token: UserToken
    ) -> Dict[str, Any]:
        """
        获取班车座位预订状态

        Args:
            shuttle_id: 班车 ID
            date: 日期，格式 YYYY-MM-DD
            userid: 用户 ID
            token: 用户令牌

        Returns:
            座位预订信息字典

        Raises:
            ValidationError: 参数验证失败
            AuthenticationError: 认证失败
            NetworkError: 网络请求失败
            BusinessError: 业务逻辑错误
        """
        # 参数验证
        if not shuttle_id or not shuttle_id.strip():
            raise ValidationError("shuttle_id cannot be empty")

        if not validate_date_format(date):
            raise ValidationError(f"Invalid date format: {date}")

        if not userid or not userid.strip():
            raise ValidationError("userid cannot be empty")

        if not token.is_valid():
            raise ValidationError("Invalid token")

        # 构建请求
        url = (
            f"{self.config.base_url}/vehicle/get-reserved-seats"
            f"?id={shuttle_id}&date={date}&userid={userid}"
        )

        body = self._make_request(HttpMethod.GET, url, self._get_default_headers(token))

        # 解析响应
        response_data = self._parse_json_response(body, "get_reserved_seats")

        data = response_data.get("data", {})
        self.logger.info(
            f"Retrieved seat info for shuttle {shuttle_id}: "
            f"reserved={data.get('reserved_count', 'N/A')}, "
            f"remaining={data.get('reservation_num', 'N/A')}"
        )

        return data

    def create_order(
            self,
            shuttle_id: str,
            date: str,
            userid: str,
            seat_number: int,
            token: UserToken
    ) -> str:
        """
        创建订单（预订座位）

        Args:
            shuttle_id: 班车 ID
            date: 日期，格式 YYYY-MM-DD
            userid: 用户 ID
            seat_number: 座位号
            token: 用户令牌

        Returns:
            响应消息

        Raises:
            ValidationError: 参数验证失败
            AuthenticationError: 认证失败
            NetworkError: 网络请求失败
            BusinessError: 订单创建失败
        """
        # 参数验证
        if not shuttle_id or not shuttle_id.strip():
            raise ValidationError("shuttle_id cannot be empty")

        if not validate_date_format(date):
            raise ValidationError(f"Invalid date format: {date}")

        if not userid or not userid.strip():
            raise ValidationError("userid cannot be empty")

        if seat_number <= 0:
            raise ValidationError(f"Invalid seat_number: {seat_number}")

        if not token.is_valid():
            raise ValidationError("Invalid token")

        # 构建请求
        url = f"{self.config.base_url}/vehicle/create-order"
        data = f"id={shuttle_id}&date={date}&seat_number={seat_number}&userid={userid}"

        headers = self._get_default_headers(token)
        headers["Content-Type"] = "application/x-www-form-urlencoded"

        body = self._make_request(HttpMethod.POST, url, headers, data)

        # 解析响应
        response_data = self._parse_json_response(body, "create_order")

        message = response_data.get("message", "")
        if message != "ok":
            raise BusinessError(f"Order creation failed: {message}")

        self.logger.info(
            f"Order created successfully: shuttle={shuttle_id}, "
            f"seat={seat_number}, date={date}"
        )

        return message

    def close(self):
        """关闭 session，释放资源"""
        if self._session:
            self._session.close()
            self.logger.debug("Session closed")

    def __enter__(self):
        """上下文管理器入口"""
        return self

    def __exit__(self, exc_type, exc_val, exc_tb):
        """上下文管理器退出"""
        self.close()


# ==================== 示例使用 ====================

def main():
    """主函数示例"""
    import os
    from dotenv import load_dotenv

    load_dotenv()

    # 配置日志
    logging.basicConfig(
        level=logging.INFO,
        format='%(asctime)s - %(name)s - %(levelname)s - %(message)s'
    )

    try:
        # 创建配置
        config = APIConfig(
            host=os.getenv("API_HOST", "hqapp1.bit.edu.cn"),
            timeout=15,
            max_retries=3,
            debug=False
        )

        # 创建令牌
        token = UserToken()

        userid = os.getenv("USER_ID", "")

        # 使用上下文管理器
        with ShuttleAPI(config) as api:
            today = datetime.now().strftime("%Y-%m-%d")

            # 测试路线
            for route in RouteDirection:
                print(f"\n{'=' * 60}")
                print(f"测试路线: {route.value}")
                print('=' * 60)

                try:
                    shuttles = api.get_shuttle_list(today, route.value, userid, token)

                    if shuttles:
                        print(f"✓ 找到 {len(shuttles)} 趟班车")

                        # 仅显示第一趟班车
                        if len(shuttles) > 0:
                            shuttle = shuttles[0]
                            shuttle_id = shuttle.get("id")
                            print(f"\n班车 ID: {shuttle_id}")

                            # 获取座位信息
                            reservation = api.get_reserved_seats(
                                shuttle_id, today, userid, token
                            )
                            print(f"已预订: {reservation.get('reserved_count', 'N/A')}")
                            print(f"剩余: {reservation.get('reservation_num', 'N/A')}")
                    else:
                        print("⚠ 该路线今天没有班车")

                except ShuttleAPIError as e:
                    print(f"✗ 错误: {e}")
                except Exception as e:
                    print(f"✗ 未知错误: {e}")

    except ValidationError as e:
        print(f"配置错误: {e}")
    except Exception as e:
        print(f"初始化失败: {e}")


if __name__ == "__main__":
    main()
