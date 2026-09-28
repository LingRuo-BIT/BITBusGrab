# coding=utf-8
"""
    @Author：零 若
    @file： app.py
    @date：2025/10/29 21:09
    @Python  : 3.10.18
    别放弃，即使前方荆棘成林！
"""

import json
import ipaddress
import os
import threading
from urllib.parse import urlparse

from flask import Flask, render_template, request, jsonify

from api_client import BusAPI
from sso_auth import BusSsoError, SsoLoginManager
from task_manager import TaskManager

app = Flask(__name__)
app.secret_key = os.urandom(24)

# 全局任务管理器
task_manager = TaskManager()

# 配置文件路径
CONFIG_FILE = 'config.json'
PRIORITIES_FILE = 'seat_priorities.json'  # 新增:座位优先级配置文件
CONFIG_LOCK = threading.RLock()


def load_config():
    """加载配置"""
    with CONFIG_LOCK:
        if os.path.exists(CONFIG_FILE):
            with open(CONFIG_FILE, 'r', encoding='utf-8') as f:
                config = json.load(f)
        else:
            config = {}
        config.setdefault('API_HOST', 'hqapp1.bit.edu.cn')
        config.setdefault('USER_ID', '')
        return config


def save_config(config):
    """保存配置"""
    with CONFIG_LOCK:
        with open(CONFIG_FILE, 'w', encoding='utf-8') as f:
            json.dump(config, f, indent=2, ensure_ascii=False)


def save_authenticated_userid(userid):
    """仅保存班车系统返回的用户标识，不保存账号、密码或 CAS 会话。"""
    config = load_config()
    config['API_HOST'] = 'hqapp1.bit.edu.cn'
    config['USER_ID'] = userid
    config.pop('API_TOKEN', None)
    config.pop('API_TIME', None)
    save_config(config)


def _is_local_auth_request():
    """账号密码只允许经本机打开的网页提交。"""
    try:
        if not ipaddress.ip_address(request.remote_addr or '').is_loopback:
            return False
    except ValueError:
        return False

    local_hosts = {'localhost', '127.0.0.1'}
    host = urlparse(f'//{request.host}').hostname
    if (host or '').lower() not in local_hosts:
        return False

    origin = request.headers.get('Origin')
    if origin:
        parsed_origin = urlparse(origin)
        if parsed_origin.scheme not in {'http', 'https'} or (parsed_origin.hostname or '').lower() not in local_hosts:
            return False
    return True


auth_manager = SsoLoginManager(on_authenticated=save_authenticated_userid)


@app.route('/')
def index():
    """主页"""
    return render_template('index.html')


@app.route('/api/search', methods=['POST'])
def search_buses():
    """车辆查询"""
    try:
        data = request.json
        origin = data.get('origin')
        destination = data.get('destination')
        date = data.get('date')

        if not all([origin, destination, date]):
            return jsonify({'success': False, 'error': '缺少必要参数'})

        config = load_config()

        # 验证配置
        if not config.get('API_HOST'):
            return jsonify({'success': False, 'error': '请先在设置中配置 API 地址'})

        if not config.get('USER_ID'):
            return jsonify({'success': False, 'error': '请先登录北理统一身份认证'})

        # 调用查询接口
        with BusAPI(config) as api:
            buses = api.search_buses(origin, destination, date)

        return jsonify({'success': True, 'data': buses})

    except Exception as e:
        return jsonify({'success': False, 'error': str(e)})


@app.route('/api/seats/<bus_id>')
def get_seats(bus_id):
    """获取座位信息"""
    try:
        date = request.args.get('date')

        if not date:
            return jsonify({'success': False, 'error': '缺少日期参数'})

        config = load_config()

        if not config.get('USER_ID'):
            return jsonify({'success': False, 'error': '请先登录北理统一身份认证'})

        with BusAPI(config) as api:
            seats_info = api.get_seats(bus_id, date)

        return jsonify({'success': True, 'data': seats_info})

    except Exception as e:
        return jsonify({'success': False, 'error': str(e)})


@app.route('/api/reserve', methods=['POST'])
def reserve_ticket():
    """预定车票"""
    try:
        data = request.json
        bus_id = data.get('bus_id')
        bus_info = data.get('bus_info', {})
        seat_ids = data.get('seat_ids', [])
        auto_mode = data.get('auto_mode', False)
        target_count = data.get('target_count', 1)
        seat_priorities = data.get('seat_priorities', {})  # 新增：座位优先级

        if not bus_id:
            return jsonify({'success': False, 'error': '缺少班车ID'})

        if not auto_mode and not seat_ids:
            return jsonify({'success': False, 'error': '请至少选择一个座位'})

        config = load_config()

        # 验证配置
        if not config.get('API_HOST') or not config.get('USER_ID'):
            return jsonify({'success': False, 'error': '请先登录北理统一身份认证'})

        # 创建抢票任务
        task_id = task_manager.create_task(
            bus_id=bus_id,
            bus_info=bus_info,
            seat_ids=seat_ids,
            auto_mode=auto_mode,
            target_count=target_count,
            config=config,
            seat_priorities=seat_priorities  # 传递优先级配置
        )

        return jsonify({'success': True, 'task_id': task_id})

    except Exception as e:
        return jsonify({'success': False, 'error': str(e)})


@app.route('/api/tasks')
def get_tasks():
    """获取任务列表"""
    try:
        tasks = task_manager.get_all_tasks()
        return jsonify({'success': True, 'data': tasks})
    except Exception as e:
        return jsonify({'success': False, 'error': str(e)})


@app.route('/api/tasks/<task_id>', methods=['DELETE'])
def delete_task(task_id):
    """删除任务"""
    try:
        task_manager.delete_task(task_id)
        return jsonify({'success': True})
    except Exception as e:
        return jsonify({'success': False, 'error': str(e)})


@app.route('/api/tasks/<task_id>/cancel', methods=['POST'])
def cancel_task(task_id):
    """取消任务"""
    try:
        task_manager.cancel_task(task_id)
        return jsonify({'success': True})
    except Exception as e:
        return jsonify({'success': False, 'error': str(e)})


@app.route('/api/config', methods=['GET', 'POST'])
def manage_config():
    """配置管理"""
    if request.method == 'GET':
        config = load_config()
        public_config = {
            key: value for key, value in config.items()
            if key not in {'API_TOKEN', 'API_TIME', 'USER_ID'}
        }
        public_config['authenticated'] = bool(config.get('USER_ID'))
        return jsonify({'success': True, 'data': public_config})
    else:
        try:
            incoming = request.get_json(silent=True) or {}
            config = load_config()
            api_host = str(incoming.get('API_HOST') or config.get('API_HOST') or 'hqapp1.bit.edu.cn').strip()
            api_host = api_host.removeprefix('http://').removeprefix('https://').rstrip('/')
            if api_host.lower() != 'hqapp1.bit.edu.cn':
                return jsonify({'success': False, 'error': '当前只支持 hqapp1.bit.edu.cn'})

            config['API_HOST'] = api_host
            for key in ('notification_methods', 'email_config', 'wechat_config'):
                if key in incoming:
                    config[key] = incoming[key]
            config.pop('API_TOKEN', None)
            config.pop('API_TIME', None)
            save_config(config)
            return jsonify({'success': True, 'message': '配置保存成功'})

        except Exception as e:
            return jsonify({'success': False, 'error': str(e)})


@app.route('/api/auth/login', methods=['POST'])
def start_sso_login():
    """启动本机网页发起的统一身份认证。"""
    if not _is_local_auth_request():
        return jsonify({'success': False, 'error': '登录仅允许从本机网页发起'}), 403

    data = request.get_json(silent=True) or {}
    if not isinstance(data, dict):
        return jsonify({'success': False, 'error': '登录参数格式无效'}), 400
    try:
        auth_id = auth_manager.start(data.get('username', ''), data.get('password', ''))
        return jsonify({'success': True, 'auth_id': auth_id}), 202
    except (ValueError, BusSsoError) as error:
        return jsonify({'success': False, 'error': str(error)}), 400


@app.route('/api/auth/status/<auth_id>')
def sso_login_status(auth_id):
    if not _is_local_auth_request():
        return jsonify({'success': False, 'error': '登录状态仅允许从本机网页读取'}), 403
    status = auth_manager.status(auth_id)
    if status is None:
        return jsonify({'success': False, 'error': '登录请求已失效'}), 404
    return jsonify({'success': True, **status})


@app.route('/api/auth/sms', methods=['POST'])
def submit_sso_sms():
    if not _is_local_auth_request():
        return jsonify({'success': False, 'error': '短信验证码仅允许从本机网页提交'}), 403
    data = request.get_json(silent=True) or {}
    if not isinstance(data, dict):
        return jsonify({'success': False, 'error': '短信验证参数格式无效'}), 400
    try:
        auth_manager.submit_sms(data.get('auth_id', ''), data.get('code', ''))
        return jsonify({'success': True})
    except (ValueError, BusSsoError) as error:
        return jsonify({'success': False, 'error': str(error)}), 400


@app.route('/api/auth/logout', methods=['POST'])
def logout_sso():
    if not _is_local_auth_request():
        return jsonify({'success': False, 'error': '退出登录仅允许从本机网页发起'}), 403
    config = load_config()
    config['USER_ID'] = ''
    config.pop('API_TOKEN', None)
    config.pop('API_TIME', None)
    save_config(config)
    return jsonify({'success': True})


@app.route('/api/priorities', methods=['GET', 'POST'])
def manage_priorities():
    """座位优先级管理"""
    if request.method == 'GET':
        # 加载优先级配置
        if os.path.exists(PRIORITIES_FILE):
            with open(PRIORITIES_FILE, 'r', encoding='utf-8') as f:
                priorities = json.load(f)
        else:
            # 默认优先级:所有座位为中等优先级
            priorities = {}
            for i in range(1, 52):
                if i not in [1, 2, 49]:
                    priorities[str(i)] = 2  # 1=高, 2=中, 3=低

        return jsonify({'success': True, 'data': priorities})
    else:
        # 保存优先级配置
        try:
            priorities = request.json

            with open(PRIORITIES_FILE, 'w', encoding='utf-8') as f:
                json.dump(priorities, f, indent=2, ensure_ascii=False)

            return jsonify({'success': True, 'message': '优先级配置保存成功'})
        except Exception as e:
            return jsonify({'success': False, 'error': str(e)})


if __name__ == '__main__':
    app.run(debug=False, host='127.0.0.1', port=23200)
