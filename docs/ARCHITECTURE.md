# 架构概览 (ARCHITECTURE)

> Finance Tracker — 个人资金管理与记账系统

## 高层架构

```
                         ┌─────────────────────────┐
                         │  浏览器 (Web UI)        │
                         │  Next.js 15 + Recharts  │
                         └────────────┬────────────┘
                                      │ HTTPS (token)
                                      ▼
┌─────────────────────────┐   ┌────────────────────────────────┐
│  本地 Agent             │   │  Backend API (FastAPI)         │
│  Cortana / OpenClaw     │   │  /api/v1/*                     │
│                         │◀──┤   - routes                     │
│         (stdio MCP)     │   │   - services                   │
│                         │   │   - PDF parsers                │
└────────┬────────────────┘   │   - market data poller         │
         │                    │   - categorization engine      │
         ▼                    │   - Notion sync (P2)           │
┌─────────────────────────┐   └─────────────┬──────────────────┘
│  MCP Server (Python)    │                 │
│  shared SQLAlchemy      │─────────────────┤
│  models / services      │                 │
└─────────────────────────┘                 │
                                            ▼
                              ┌──────────────────────────────┐
                              │  SQLite (WAL)                │
                              │  ./data/finance.db           │
                              └──────────────────────────────┘
                                            ▲
                                            │
                              ┌──────────────────────────────┐
                              │  外部行情源                  │
                              │  yfinance / CoinGecko /      │
                              │  exchangerate.host / metals  │
                              └──────────────────────────────┘
```

## 模块切分

### Backend (`backend/app/`)

| 目录 | 职责 |
|------|------|
| `core/` | 配置、DB 连接、token 鉴权中间件、日志 |
| `models/` | SQLAlchemy ORM 实体 (与 `docs/SCHEMA.sql` 一一对应) |
| `schemas/` | Pydantic v2 请求/响应模型 (与 `docs/API.md` 对齐) |
| `api/v1/` | FastAPI route handlers (薄,只做参数校验和调用 service) |
| `services/` | 业务逻辑层 (PDF 解析、分类、估值、现金流计算、行情拉取) |
| `db/migrations/` | Alembic 迁移脚本 |

### Frontend (`frontend/src/`)

| 目录 | 职责 |
|------|------|
| `app/` | Next.js App Router 页面 (dashboard / transactions / assets / settings) |
| `components/` | UI 原子 (shadcn/ui 衍生) + 业务组件 (charts/forms) |
| `lib/` | API client、formatters、i18n、constants |
| `types/` | OpenAPI 生成的类型 |

### MCP Server (`mcp-server/`)

独立进程,通过 `pip install -e ../backend` 复用后端 service 层,共享同一数据库文件。

## 关键流程

### 1. PDF 上传 → 入库

```
[Web UI] → POST /statements/upload (multipart)
       → save file → SHA-256 hash 去重
       → detector.detect_bank(text) → 选择 parser
       → parser.parse() → List[ParsedTransaction]
       → categorizer.suggest(tx) → 应用规则 / fallback 模型
       → batch insert 到 transactions (is_pending=1)
       → 返回预览给前端
       → 用户确认 → POST /statements/{id}/confirm → is_pending=0
```

### 2. 实时估值

```
[APScheduler 定时任务]
  ├─ 每 5 min: 拉取 crypto (CoinGecko)
  ├─ 每 15 min: 拉取股票 (yfinance)
  ├─ 每 1 h:    拉取汇率 (exchangerate.host)
  └─ 每 1 h:    拉取黄金

[Web UI / MCP] → GET /portfolio/summary
       → 读 asset_holdings × latest market_prices
       → 折算到 base_currency (使用最新 fx_rates)
       → 返回汇总
```

### 3. 现金流分析

- 写入: 每次 transaction CRUD 后,异步触发对应月度 snapshot 重算 (debounce 2s)
- 读取: 仪表盘直接读 `cash_flow_snapshots` (O(months))

### 4. Notion 数据同步 (P2)

```
[API / Cron] → POST /api/v1/notion/sync
       → NotionSyncService.sync_all(db_session)
       ├─ sync_transactions: 按更新时间增量同步到 Notion 交易数据库
       ├─ sync_cashflow: 月度现金流快照同步到 Notion 现金流数据库
       └─ sync_asset_summary: 资产持仓汇总写入 Notion 资产页面

特性:
- 单向同步: finance-tracker → Notion (只读镜像)
- 增量更新: 通过 internal Tx ID 匹配已有 Notion 条目,避免重复
- 速率限制: 内置 0.4s 请求间隔 + 429 Retry-After 处理
- 一键建库: POST /api/v1/notion/setup 自动创建 Notion 数据库
- 分模块触发: 支持 /sync/transactions、/sync/cashflow、/sync/assets 单独调用
```

## 数据流原则

1. **币种最小折算**: 只在汇总展示时折算到 base_currency,原始 transaction 保留原币
2. **写时计算 vs 读时计算**: 现金流月度汇总写时计算 (snapshot 表),其余派生数据读时计算 (view 或 service)
3. **价格缓存优先**: 即便上游行情挂掉,也要能基于最近一次本地缓存正常显示 portfolio
4. **单向依赖**: frontend → backend HTTP, mcp-server → backend python import,backend 不依赖任何客户端

## 部署形态

```
docker-compose.yml
├── backend     (Python 3.11 + uvicorn:8000)
├── frontend    (Node 20 + next start:3000)
└── mcp-server  (Python 3.11, stdio,只在需要时被 Agent 拉起)

宿主机 ./data/   ─── 卷挂载 ──→  容器内 /app/data/
                                    ├── finance.db
                                    ├── pdfs/<hash>.pdf
                                    └── backups/
```

## 安全/隐私

- 数据全部本地存储,默认不出网 (除拉取行情)
- Token 强制 32 字节随机,启动检查
- PDF 原文保留,因为银行流水号在重新解析时需要
- 备份: 每日凌晨 cron 触发 `sqlite3 .backup` → 保留最近 30 天

## 演进路径

| 阶段 | 内容 |
|------|------|
| P0 (现在) | 架构 + 数据库 + 骨架 |
| P1 | 核心 API、PDF 解析(优先 1~2 家银行)、Web UI 仪表盘 |
| P2 | MCP server、银行 API 直连、Notion 同步、Docker 一键部署 |
| 未来 | 移动 App (React Native 复用 API)、ML 分类器优化 |
