# coding=utf-8
"""
    @Author：零 若
    @file： api_client.py
    @date：2025/10/29 21:09
    @Python  : 3.10.18
    别放弃，即使前方荆棘成林！
"""

from datetime import datetime

from api import ShuttleAPI, APIConfig, UserToken
from api import ShuttleAPIError, ValidationError, NetworkError, BusinessError
from inform import NotificationManager


class BusAPI:
    def __init__(self, config):
        """
        初始化 BusAPI
        
        Args:
            config: 配置字典，包含 API_HOST 和 USER_ID
        """
        self.user_id = config.get('USER_ID', '')

        # 创建 ShuttleAPI 配置
        host = config.get('API_HOST', 'hqapp1.bit.edu.cn')
        # 移除协议前缀（ShuttleAPI 内部会添加 http://）
        host = host.replace('https://', '').replace('http://', '')

        api_config = APIConfig(
            host=host,
            timeout=15,
            max_retries=3,
            debug=config.get('DEBUG', False)
        )

        # 创建用户令牌
        self.token = UserToken()

        # 创建 ShuttleAPI 实例
        self.api = ShuttleAPI(api_config)

        # 初始化通知管理器
        self.notification_manager = NotificationManager(config)

    def search_buses(self, origin, destination, date):
        """
        查询车辆
        
        Args:
            origin: 出发地
            destination: 目的地
            date: 日期 (YYYY-MM-DD)
            
        Returns:
            车辆列表
        """
        try:
            # 构建路线方向（使用->连接）
            route = f"{origin}->{destination}"

            # 调用 API
            shuttles = self.api.get_shuttle_list(
                date=date,
                address=route,
                userid=self.user_id,
                token=self.token
            )

            # 转换数据格式以匹配前端需求
            result = []
            for shuttle in shuttles:
                result.append({
                    'id': shuttle.get('id', ''),
                    'origin_address': shuttle.get('origin_address', origin),
                    'origin_time': shuttle.get('origin_time', ''),
                    'end_address': shuttle.get('end_address', destination),
                    'end_time': shuttle.get('end_time', ''),
                    'student_ticket_price': shuttle.get('student_ticket_price', 0),
                    'type': shuttle.get('type', 0),
                    'reserved_count': shuttle.get('reserved_count', 0),
                    'reservation_num': shuttle.get('reservation_num', 0),
                    'reservation_num_able': shuttle.get('reservation_num_able', 50)
                })

            return result

        except ValidationError as e:
            raise Exception(f"参数验证失败: {str(e)}")
        except NetworkError as e:
            raise Exception(f"网络请求失败: {str(e)}")
        except BusinessError as e:
            raise Exception(f"业务错误: {str(e)}")
        except ShuttleAPIError as e:
            raise Exception(f"API错误: {str(e)}")
        except Exception as e:
            raise Exception(f"未知错误: {str(e)}")

    def get_seats(self, bus_id, date=None):
        """
        获取座位信息
        
        Args:
            bus_id: 班车ID
            date: 日期，默认为今天
            
        Returns:
            座位信息字典
        """
        try:
            if not date:
                date = datetime.now().strftime("%Y-%m-%d")

            # 调用 API
            seat_info = self.api.get_reserved_seats(
                shuttle_id=str(bus_id),
                date=date,
                userid=self.user_id,
                token=self.token
            )

            # 提取数据
            reserved_count = seat_info.get('reserved_count', 0)
            reservation_num = seat_info.get('reservation_num', 0)
            reservation_num_able = seat_info.get('reservation_num_able', 51)

            # 获取已预订的座位号列表
            reserved_seat_numbers = seat_info.get('reserved_seat_number', [])
            reserved_seats_int = []

            # 转换为整数列表
            if isinstance(reserved_seat_numbers, list):
                for seat in reserved_seat_numbers:
                    try:
                        # 处理字符串或整数类型
                        reserved_seats_int.append(int(seat))
                    except (ValueError, TypeError):
                        continue

            # 系统禁用的座位（1、2、49号）
            disabled_seats = [1, 2, 49]

            # 生成座位列表
            seats = []
            for i in range(1, reservation_num_able + 1):
                if i in disabled_seats:
                    seat = {
                        'id': i,
                        'status': 'disabled',
                        'reason': '系统保留座位'
                    }
                elif i in reserved_seats_int:
                    seat = {
                        'id': i,
                        'status': 'reserved',
                        'reason': '已被预订'
                    }
                else:
                    seat = {
                        'id': i,
                        'status': 'available'
                    }
                seats.append(seat)

            # 计算实际可用座位数
            available_count = reservation_num - len(disabled_seats)

            return {
                'reserved_count': reserved_count,
                'reservation_num': reservation_num,
                'reservation_num_able': reservation_num_able,
                'available_count': max(0, available_count),
                'seats': seats,
                'reserved_seats': reserved_seats_int
            }

        except ValidationError as e:
            raise Exception(f"参数验证失败: {str(e)}")
        except NetworkError as e:
            raise Exception(f"网络请求失败: {str(e)}")
        except BusinessError as e:
            raise Exception(f"业务错误: {str(e)}")
        except ShuttleAPIError as e:
            raise Exception(f"API错误: {str(e)}")
        except Exception as e:
            raise Exception(f"未知错误: {str(e)}")

    def reserve_seat(self, bus_id, seat_id, date=None):
        """
        预订座位
        
        Args:
            bus_id: 班车ID
            seat_id: 座位号
            date: 日期，默认为今天
            
        Returns:
            预订结果
        """
        try:
            if not date:
                date = datetime.now().strftime("%Y-%m-%d")

            # 调用创建订单 API
            message = self.api.create_order(
                shuttle_id=str(bus_id),
                date=date,
                userid=self.user_id,
                seat_number=int(seat_id),
                token=self.token
            )

            # 如果返回 "ok" 表示成功
            success = (message == 'ok')

            return {
                'success': success,
                'message': message if success else f'预订失败: {message}',
                'seat_id': seat_id
            }

        except ValidationError as e:
            return {'success': False, 'error': f"参数验证失败: {str(e)}", 'seat_id': seat_id}
        except NetworkError as e:
            return {'success': False, 'error': f"网络请求失败: {str(e)}", 'seat_id': seat_id}
        except BusinessError as e:
            return {'success': False, 'error': f"业务错误: {str(e)}", 'seat_id': seat_id}
        except ShuttleAPIError as e:
            return {'success': False, 'error': f"API错误: {str(e)}", 'seat_id': seat_id}
        except Exception as e:
            return {'success': False, 'error': f"未知错误: {str(e)}", 'seat_id': seat_id}

    def send_notification(self, title, message):
        """
        发送通知
        
        Args:
            title: 通知标题
            message: 通知内容
        """
        self.notification_manager.send_notification(title, message)

    def close(self):
        """关闭 API 连接"""
        if hasattr(self, 'api'):
            self.api.close()

    def __enter__(self):
        return self

    def __exit__(self, exc_type, exc_val, exc_tb):
        self.close()
