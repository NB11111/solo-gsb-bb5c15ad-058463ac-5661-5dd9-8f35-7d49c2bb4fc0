# Event Timeline Audit（跨系统事件时间线核对服务）

一个 FastAPI 服务，用来核对来自多个系统的事件时间能否**同时成立**。

输入每个事件的最早 / 最晚时间，以及事件之间的“至少间隔 / 至多间隔”约束。
服务把所有条件统一为**差分约束**（形如 `x[v] - x[u] ≤ w`），以 Unix 纪元为固定锚点
（`x[0] = 0`）构建有向图，用 **Bellman-Ford 最短路**判定可行性：

- **可行**：返回每个事件收紧后的时间范围（`earliest`/`latest`），并给出一个具体可行时刻；
- **不可行**：图中存在负权环，返回由**原始约束**构成的一条矛盾闭环（事件链 + 逐步不等式
  + 权重和），闭环上的不等式相加得到 `0 ≤ 负数`，直接定位证据冲突。

时间统一使用 **RFC 3339 UTC**（输出始终带 `Z`），内部用**整数微秒**运算，无浮点误差。

## 单条命令启动

需要 Docker + Compose 插件（`docker compose`）：

```bash
docker compose up --build
```

服务监听 **8000** 端口：

- 健康检查：<http://localhost:8000/healthz>（`GET /` 同样可用）
- 在线接口文档：<http://localhost:8000/docs>

停止：

```bash
docker compose down
```

## 配置（环境变量）

在 `docker-compose.yml` 的 `environment` 中配置，均可省略（括号内为默认值）：

| 变量        | 默认值     | 说明                                   |
| ----------- | ---------- | -------------------------------------- |
| `HOST`      | `0.0.0.0`  | uvicorn 绑定地址                       |
| `PORT`      | `8000`     | 监听端口（容器内固定暴露 8000）        |
| `LOG_LEVEL` | `info`     | 日志级别（`critical`/`error`/`warning`/`info`/`debug`/`trace`） |

## API

### `GET /healthz`

```json
{"status": "ok", "service": "event-timeline-audit"}
```

### `POST /audits`

请求体：

| 字段                          | 类型     | 说明                                                                 |
| ----------------------------- | -------- | -------------------------------------------------------------------- |
| `events`                      | 数组     | 至少 1 个事件；`id` 不可重复                                         |
| `events[].id`                 | string   | 事件唯一标识                                                         |
| `events[].earliest`           | string   | 最早时间，RFC 3339，**必须带时区偏移**（建议 `Z`）                   |
| `events[].latest`             | string   | 最晚时间，RFC 3339，必须带偏移                                       |
| `constraints`                 | 数组     | 可省略                                                               |
| `constraints[].type`          | string   | `at_least`（至少间隔）或 `at_most`（至多间隔）                       |
| `constraints[].left_event`    | string   | 间隔的参照事件                                                       |
| `constraints[].right_event`   | string   | 另一侧事件                                                           |
| `constraints[].seconds`       | number/string | 间隔秒数，**非负**，最高微秒精度（如 `"0.000001"`）；建议用字符串  |
| `constraints[].id`            | string   | 可选，约束标识，便于在矛盾报告中定位证据                             |

语义：

- `at_least`：`right_event` 的时间 ≥ `left_event` + `seconds`
- `at_most` ：`right_event` 的时间 ≤ `left_event` + `seconds`

#### 可行示例

```bash
curl -s -X POST http://localhost:8000/audits \
  -H 'Content-Type: application/json' \
  -d '{
    "events": [
      {"id": "login", "earliest": "2026-09-21T10:00:00Z", "latest": "2026-09-21T12:00:00Z"},
      {"id": "token", "earliest": "2026-09-21T10:00:00Z", "latest": "2026-09-21T12:00:00Z"}
    ],
    "constraints": [
      {"id": "gap", "type": "at_least", "left_event": "login", "right_event": "token", "seconds": "1.5"},
      {"id": "cap", "type": "at_most",  "left_event": "login", "right_event": "token", "seconds": "3600"}
    ]
  }'
```

响应 `200`：

```json
{
  "feasible": true,
  "events": [
    {"id": "login", "earliest": "2026-09-21T10:00:00Z",       "latest": "2026-09-21T11:59:58.500000Z", "feasible_time": "2026-09-21T11:59:58.500000Z"},
    {"id": "token", "earliest": "2026-09-21T10:00:01.500000Z","latest": "2026-09-21T12:00:00Z",        "feasible_time": "2026-09-21T12:00:00Z"}
  ]
}
```

收紧规则：每个事件的新上界 = 锚点到该节点的最短路；新下界 = 反向图最短路取负。
`feasible_time` 直接取正向最短路距离，保证所有约束都满足。

#### 不可行示例（矛盾闭环）

`b−a ≤ 100s`、`c−b ≤ 100s` 与 `c−a ≥ 1000s` 无法同时成立：

```bash
curl -s -X POST http://localhost:8000/audits \
  -H 'Content-Type: application/json' \
  -d '{
    "events": [
      {"id": "a", "earliest": "2026-09-21T10:00:00Z", "latest": "2026-09-21T12:00:00Z"},
      {"id": "b", "earliest": "2026-09-21T10:00:00Z", "latest": "2026-09-21T12:00:00Z"},
      {"id": "c", "earliest": "2026-09-21T10:00:00Z", "latest": "2026-09-21T12:00:00Z"}
    ],
    "constraints": [
      {"id": "c1", "type": "at_most",  "left_event": "a", "right_event": "b", "seconds": "100"},
      {"id": "c2", "type": "at_most",  "left_event": "b", "right_event": "c", "seconds": "100"},
      {"id": "c4", "type": "at_least", "left_event": "a", "right_event": "c", "seconds": "1000"}
    ]
  }'
```

响应 `200`（业务结果为不可行，HTTP 仍为 200；仅请求格式错误才是 422）：

```json
{
  "feasible": false,
  "conflict": {
    "events": ["a", "b", "c", "a"],
    "steps": [
      {"kind": "at_most",  "constraint_id": "c1", "left_event": "a", "right_event": "b", "seconds": "100",  "inequality": "b - a <= 100s"},
      {"kind": "at_most",  "constraint_id": "c2", "left_event": "b", "right_event": "c", "seconds": "100",  "inequality": "c - b <= 100s"},
      {"kind": "at_least", "constraint_id": "c4", "left_event": "a", "right_event": "c", "seconds": "1000", "inequality": "c - a >= 1000s"}
    ],
    "total_seconds": "-800",
    "contradiction": "Summing the inequalities around the closed loop gives 0 <= -800s; ..."
  }
}
```

`events` 首尾相同，逐步相连；每个 step 都引用了一条**原始约束**（或原始 `earliest`/`latest`
边界；经过纪元锚点时事件名为 `<epoch>`）。

### 错误（HTTP 422）

统一错误信封：

```json
{"error": {"code": "unknown_event_reference", "message": "...", "details": {}}}
```

| `code`                     | 触发条件                                       |
| -------------------------- | ---------------------------------------------- |
| `invalid_request`          | JSON / 字段类型 / 朴素时间戳 / 负数间隔 / 精度细于微秒等 |
| `duplicate_event_id`       | 事件 `id` 重复                                 |
| `duplicate_constraint_id`  | 约束 `id` 重复                                 |
| `unknown_event_reference`  | 约束引用了未定义的事件 `id`                    |

注意：单个事件若 `earliest > latest`，属于**语义矛盾**而非格式错误，会在 200 响应中返回
一条经过 `<epoch>` 的 `earliest` + `latest` 双环。

## 本地开发（不用 Docker）

需要 Python 3.11+：

```bash
pip install -r requirements-dev.txt
uvicorn app.main:app --reload --port 8000
```

运行测试：

```bash
python -m pytest -q
```

## 项目结构

```
app/
  main.py        # FastAPI 路由、图构建、响应组装、错误处理
  solver.py      # 差分约束：Bellman-Ford 可行性判定、范围收紧、负环提取
  schemas.py     # 请求/响应模型与输入校验
  timeutils.py   # RFC 3339 UTC 解析/格式化，整数微秒换算
tests/           # pytest 端到端测试
Dockerfile       # python:3.12-slim，依赖随镜像安装，非 root 运行
docker-compose.yml
```

## 实现要点

- 约束到边的映射（节点 0 为纪元锚点，`x[0] = 0`）：
  - `earliest = E` → `v → 0`，权 `-E`
  - `latest = L` → `0 → v`，权 `L`
  - `at_least d`（a→b 含义 `b−a ≥ d`）→ `b → a`，权 `-d`
  - `at_most d`（含义 `b−a ≤ d`）→ `a → b`，权 `d`
- 可行 ⇔ 图中无负权环；负环检测额外使用“隐式超级源”初始化做兜底，保证不会漏报。
- 时间戳以整数微秒存储；间隔接受小数秒，但拒绝亚微秒精度，避免静默舍入改变判定结果。
