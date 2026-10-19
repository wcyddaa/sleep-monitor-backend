# 智能睡眠监测系统 - 后端

基于 **FastAPI + LangGraph** 的多模态睡眠监测后端服务。

> 开箱即用：使用 uv 管理依赖，SQLite 作为默认存储，**未配置 OPENAI_API_KEY 时自动启用本地 Mock LLM**，保证完整流程可跑通。

---

## 功能概览

| 模块 | 能力 |
| --- | --- |
| **API Gateway (FastAPI)** | 统一入口、自动 OpenAPI 文档、CORS、健康检查 |
| **认证服务 (Auth)** | 注册 / 登录（JWT）/ 获取当前用户 |
| **设备服务 (Device)** | 设备注册、列表 |
| **睡眠数据接入 (Sleep)** | 设备上报数据入库、查询、详情 |
| **DataAgent (LangGraph)** | 特征抽取 → LLM 摘要 → 风险评估 |
| **ReportAgent (LangGraph)** | 提示词组装 → LLM 生成 → 后处理 |
| **ChatAgent (LangGraph)** | 上下文检索 → LLM 答复 |
| **MQTT 模拟器** | 未配置真实 MQTT Broker 时按周期生成演示数据 |

整体调用流：

```
设备 (MQTT / HTTP)
   │
   ▼
[POST /api/v1/sleep/ingest] ──► SleepRecord(原始数据)
                                   │
            ┌──────────────────────┼───────────────────────┐
            ▼                      ▼                       ▼
  [POST /analyze]         [POST /reports/generate]    [POST /chat]
     DataAgent              ReportAgent                ChatAgent
   (LangGraph)             (LangGraph)                (LangGraph)
```

---

## 快速开始

### 1. 安装 [uv](https://docs.astral.sh/uv/)

```powershell
# Windows (PowerShell)
irm https://astral.sh/uv/install.ps1 | iex
```

### 2. 同步依赖并创建虚拟环境

```powershell
cd sleep-monitor-backend
uv sync
```

### 3. 配置环境变量

```powershell
Copy-Item .env.example .env
```

按需编辑 `.env`：
- 不填 `OPENAI_API_KEY` → 启用 Mock LLM（默认）
- 填入 `OPENAI_API_KEY` / `OPENAI_BASE_URL` → 使用真实 OpenAI 兼容 API
- 关闭 `SIMULATOR_ENABLED` → 停止内置模拟器

### 4. 启动服务

```powershell
# 方式 A：uv 直接调用 uvicorn（推荐）
uv run uvicorn main:app --host 0.0.0.0 --port 8000 --reload

# 方式 B：脚本直跑
uv run python main.py
```

服务启动后访问：

- API 根路径：<http://localhost:8000/>
- Swagger 文档：<http://localhost:8000/docs>
- ReDoc：<http://localhost:8000/redoc>
- 健康检查：<http://localhost:8000/health>

---

## 完整调用示例

```bash
# 1) 注册
curl -X POST http://localhost:8000/api/v1/auth/register \
  -H "Content-Type: application/json" \
  -d '{"username":"alice","password":"secret123","email":"a@x.com","full_name":"Alice"}'

# 2) 登录拿 token
TOKEN=$(curl -s -X POST http://localhost:8000/api/v1/auth/login \
  -H "Content-Type: application/json" \
  -d '{"username":"alice","password":"secret123"}' | python -c "import sys,json;print(json.load(sys.stdin)['data']['access_token'])")

# 3) 注册设备
curl -X POST http://localhost:8000/api/v1/devices \
  -H "Authorization: Bearer $TOKEN" -H "Content-Type: application/json" \
  -d '{"device_id":"bedroom-01","name":"主卧监测仪"}'

# 4) 模拟上报一次睡眠数据（实际生产由 MQTT/SDK 上报）
curl -X POST http://localhost:8000/api/v1/sleep/ingest \
  -H "Authorization: Bearer $TOKEN" -H "Content-Type: application/json" \
  -d @sample_sleep.json

# 5) 触发分析
curl -X POST http://localhost:8000/api/v1/sleep/1/analyze \
  -H "Authorization: Bearer $TOKEN"

# 6) 生成报告
curl -X POST http://localhost:8000/api/v1/reports/generate \
  -H "Authorization: Bearer $TOKEN" -H "Content-Type: application/json" \
  -d '{"sleep_record_id":1,"style":"detailed"}'

# 7) 对话
curl -X POST http://localhost:8000/api/v1/chat \
  -H "Authorization: Bearer $TOKEN" -H "Content-Type: application/json" \
  -d '{"message":"我最近老打鼾怎么办？","sleep_record_id":1}'
```

> `sample_sleep.json` 见下方 § 附录。

---

## 项目结构

```
sleep-monitor-backend/
├── pyproject.toml            # uv 项目元数据与依赖
├── .env.example              # 环境变量模板
├── main.py                   # FastAPI 入口（uvicorn 启动）
├── app/
│   ├── config.py             # Pydantic Settings
│   ├── database.py           # 异步 SQLAlchemy
│   ├── security.py           # 密码哈希 / JWT
│   ├── deps.py               # FastAPI 依赖
│   ├── models.py             # ORM 模型
│   ├── schemas.py            # Pydantic Schema
│   ├── services.py           # 业务服务层
│   ├── api/
│   │   └── v1.py             # 路由
│   └── agents/
│       ├── llm.py            # LLM 客户端（含 Mock）
│       ├── state.py          # 状态类型
│       ├── data_graph.py     # DataAgent
│       ├── report_graph.py   # ReportAgent
│       └── chat_graph.py     # ChatAgent
├── data/                     # SQLite 文件
└── tests/                    # 预留
```

---

## 关键技术点

- **uv**：极快的 Python 包管理工具（替代 pip + venv + poetry）
- **uvicorn**：ASGI 服务器，`--reload` 开启热重载
- **FastAPI**：类型驱动、OpenAPI 自动生成
- **LangGraph**：以 StateGraph 组织多步推理，支持节点级错误兜底
- **LangChain ChatModel**：抽象 LLM，便于在 OpenAI / Mock 间切换
- **Mock LLM**：保证未配置 API Key 时端到端流程仍可演示

---

## 切换到生产 LLM

在 `.env` 中：

```env
OPENAI_API_KEY=sk-xxxx
OPENAI_BASE_URL=https://api.openai.com/v1   # 或兼容服务地址
OPENAI_MODEL=gpt-4o-mini
```

重启后，`get_chat_model()` 会自动返回 `ChatOpenAI` 实例，节点无侵入。

---

## 接入真实 MQTT（可选）

当前内置了 `MqttSimulator`，在没有 MQTT Broker 时按 `SIMULATOR_INTERVAL_SECONDS` 自动生成测试数据。

接入真实 MQTT 只需在 `app/services.py` 中把模拟器替换为 `aiomqtt` 客户端，回调里调用 `SleepService.ingest` 即可。

---

## 附录：sample_sleep.json

```json
{
  "device_id": "bedroom-01",
  "start_time": "2026-09-11T22:00:00Z",
  "end_time":   "2026-09-12T06:00:00Z",
  "vital_signs": [
    {"timestamp":"2026-09-11T22:00:00Z","heart_rate":68,"respiratory_rate":15,"bed_status":"in_bed","turnover_count":0},
    {"timestamp":"2026-09-11T23:00:00Z","heart_rate":62,"respiratory_rate":13,"bed_status":"in_bed","turnover_count":1},
    {"timestamp":"2026-09-12T02:00:00Z","heart_rate":70,"respiratory_rate":17,"bed_status":"in_bed","turnover_count":2}
  ],
  "audio_segments": [
    {"start":"2026-09-12T01:10:00Z","end":"2026-09-12T01:11:30Z","snore_detected":true,"intensity":0.62}
  ]
}
```
