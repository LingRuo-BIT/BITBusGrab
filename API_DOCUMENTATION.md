# 班车预订 API 文档

## 基础信息

- Base URL: `http://hqapp1.bit.edu.cn`
- 协议: HTTP
- 认证: 班车系统 `userid` 参数，加上每请求动态生成的 `apitoken`、`apitime` 请求头。

## 账号登录与接口凭证

本项目用 `BIT-CourseKiller` 中的 CAS 客户端完成统一身份认证账号密码登录。班车网页的 OAuth 流程为：

1. 请求 `https://sso.bit.edu.cn/cas/oauth2.0/authorize`，客户端 ID 为 `BCFW`，回调为 `http://hqapp1.bit.edu.cn/newbanche/`。
2. 从统一身份认证跳转中读取 CAS `service`，由客户端处理密码、风控和可能出现的短信验证。
3. 从班车回调地址提取一次性 OAuth `code`，调用 `GET /vehicle/auth-login?code=...`。
4. 登录接口返回的 `data.userid` 用于后续班车请求；本地只保存该用户标识，不保存密码、短信验证码或 SSO Cookie。

后续每个班车 API 请求都根据当前毫秒时间戳和网页内置算法重新生成 `apitoken`、`apitime`。它们不是需要手工复制并长期保存的账号凭证。

## 通用说明

- 所有返回均为 JSON。
- 成功码依接口而异：列表类接口通常为 `"0"`，座位与登录接口也可能为 `"1"`；系统异常可能为 `"SYS_UNKNOWN"`，业务错误用 message 描述。
- 时间格式：`YYYY-MM-DD`（日期），班车时间字段为 `HH:MM`。

---

## 1. 获取班车列表

- URL: `/vehicle/get-list`
- 方法: `GET`
- 认证: 必须（需 apitoken/apitime）
- 描述: 查询指定日期、线路的班车列表。

### 请求参数（Query）

- page (int) 必填：页码（示例：1）
- limit (int) 必填：每页数量（示例：20）
- date (string) 必填：查询日期，格式 `YYYY-MM-DD`
- address (string) 必填：路线（需 URL 编码），示例：`良乡校区->中关村校区`
- userid (string) 必填：用户 ID

### 请求示例（curl）

```
curl -G "http://hqapp1.bit.edu.cn/vehicle/get-list" \
  --data-urlencode "page=1" \
  --data-urlencode "limit=20" \
  --data-urlencode "date=2025-10-29" \
  --data-urlencode "address=" \
  --data-urlencode "userid=<your_user_id>" \
  -H "Accept: application/json" \
  -H "apitoken: <your_api_token>" \
  -H "apitime: <timestamp>"
```

### 成功响应示例 (HTTP 200)

```json
{
  "code": "0",
  "count": 8,
  "message": "ok",
  "data": [
    {
      "id": "3606230833423801788",
      "pkid": 13289,
      "name": "良乡校区-中关村校区",
      "origin_address": "良乡校区",
      "end_address": "中关村校区",
      "origin_time": "17:05",
      "end_time": "17:55",
      "reservation_num_able": 51,
      "student_ticket_price": "0.00",
      "teacher_ticket_price": "0.00",
      "type": 1,
      "shuttle_type": 3,
      "service_time": "1,2,3,4,5",
      "train_number": "",
      "car_number": null,
      "intermediate_site": null
    },
    ...
  ]
}
```

#### 响应字段说明（顶层）

- code (string)：响应代码，`"0"` 表示成功；`"SYS_UNKNOWN"` 系统异常。
- count (int)：本次查询匹配的班车数量。
- message (string)：响应消息（如 `"ok"` 或错误描述）。
- data (array)：班车项列表。

#### data 数组元素（单个班车项）

- id (string)：班车唯一标识（用于后续查询与下单）。
- pkid (int)：内部主键 id。
- name (string)：线路名称（如 `良乡校区-中关村校区`）。
- origin_address (string)：出发校区/地址。
- end_address (string)：到达校区/地址。
- origin_time (string)：发车时间（HH:MM）。
- end_time (string)：到达时间（HH:MM）。
- reservation_num_able (int)：可预订座位总数（示例：51）。
- student_ticket_price (string)：学生票价（元）。。
- teacher_ticket_price (string)：教师票价（元）。
- type (int)：班车类型（0=普通班车，1=彩虹巴士）。
- shuttle_type (int)：内部班车种类标识。
- service_time (string)：服务星期编码，逗号分隔（1=周一 … 7=周日）。
- train_number (string)：车次号（若有）。
- car_number (string|null)：车牌号（若有或 null）。
- intermediate_site (string|null)：中间停靠站（若有或 null）。

### 错误响应示例

```json
{
  "code": "SYS_UNKNOWN",
  "message": "unknown system error",
  "data": []
}
```

### 注意

- address 参数必须 URL 编码或通过请求库的 query 参数自动编码。
- route 格式常见：`良乡校区->中关村校区` 或 `中关村校区->良乡校区`。

---

## 2. 获取班车座位信息

- URL: `/vehicle/get-reserved-seats`
- 方法: `GET`
- 认证: 必需（需 apitoken/apitime）
- 描述: 查询指定班车在某日的座位预订状态。

### 请求参数（Query）

- id (string) 必填：班车 id（来自 get-list 的 id 字段）
- date (string) 必填：查询日期（YYYY-MM-DD）
- userid (string) 必填：用户 ID

### 请求示例（curl）

```
curl -G "http://hqapp1.bit.edu.cn/vehicle/get-reserved-seats" \
  --data-urlencode "id=3606230833423801788" \
  --data-urlencode "date=2025-10-29" \
  --data-urlencode "userid=<your_user_id>" \
  -H "Accept: application/json" \
  -H "apitoken: <your_api_token>" \
  -H "apitime: <timestamp>"
```

### 成功响应示例

```json
{
  "code": "1",
  "message": "ok",
  "data": {
    "is_full": 0, 
    "reservation_num": 34, 
    "reserved_count": 17, 
    "reserved_seat_number": ["11", "12", "13", "16", "18", "19", "28", "3", "32", "35", "36", "37", "4", "5", "6", "8", "9"]
  }
}
```

#### 响应字段说明（顶层）

- code (string)：响应代码，`"0"` 表示成功；`"SYS_UNKNOWN"` 系统异常。
- message (string)：响应消息（如 `"ok"` 或错误描述）。
- data (array)：车辆数据。

#### data 字典元素

- is_full (int)：是否已满（0=否，1=是）。
- reservation_num (int)：剩余可预订座位数。**（注：1、2、49默认不可预订，因此该字段减3才是真实的剩余可预定座位数）**
- reserved_count (int)：已预订座位数。
- reserved_seat_number (array[string])：已预订座位号列表（字符串形式）。

---

## 3. 创建订单（预订座位）

- URL: `/vehicle/create-order`
- 方法: `POST`
- 认证: 必需（apitoken/apitime）
- Content-Type: `application/x-www-form-urlencoded`

### 请求参数（表单）

- id (string) 必填：班车 id
- date (string) 必填：日期（YYYY-MM-DD）
- seat_number (int) 必填：座位号（1 - total_seats）
- userid (string) 必填：用户 ID

### 请求示例（curl）

```
curl -X POST "http://hqapp1.bit.edu.cn/vehicle/create-order" \
  -H "Content-Type: application/x-www-form-urlencoded" \
  -H "apitoken: <your_api_token>" \
  -H "apitime: <timestamp>" \
  -d "id=3606230833423801788&date=2025-10-29&seat_number=10&userid=<userid>"
```

#### 预定成功示例

```json
{
  "code": "0",
  "message": "ok",
  "data": null
}
```

#### 预定失败示例

```json
{
    "code": "C_1",
    "message": "此座位已被预约，请另行选座",
    "data": null
}
```

上一条的查询优先级高于此条：

```json
{
    "code": "C_1",
    "message": "暂时未开启预约，发车前一小时开启预约，请稍后再来!",
    "data": null
}
```

---

## 附录

- 座位编号范围：通常 1..51（以接口返回 total_seats 为准），其中1、2、49使用UI页面锁定（但可通过api接口预定？要我来开车吗！？）。
- service_time 编码：1..7 分别代表 周一..周日。
- type: `0` 普通班车；`1` 彩虹班车。
