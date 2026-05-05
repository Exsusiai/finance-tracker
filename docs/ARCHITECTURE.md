# 架构概览 (ARCHITECTURE)

> Finance Tracker — 个人资金管理与记账系统
> 最后修订: 2026-05-05

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
| `services/` | 业务逻辑层（见下表） |
| `db/migrations/` | Alembic 目录（暂未启用，由 `_column_migrations` idempotent ALTER 顶住） |

#### `services/` 详细结构（2026-05-05）

| 子目录 | 职责 |
|---|---|
| `pdf_parser/` | 5 家银行 parser + column-aware Revolut + 子账户 / 跨行关键词预标 |
| `categorizer/` | 关键词规则匹配 + `learn_from_user_assignment` + `apply_to_similar_pending` 级联 + seed 种子 |
| `transfer_matcher/` | 跨账户配对（评分制 + IBAN +40）+ 同账户 amount-match L3（subaccount 标记） |
| `cashflow/` | `recompute_period` / `recompute_for_periods`：transaction CRUD 后即时重算 snapshot |
| `market_data/` | 取价（yfinance / CoinGecko / FX）+ `scheduler.py` AsyncIOScheduler 三 job |
| `asset_search/` | CoinGecko + yfinance 联合查询自动识别资产 |
| `valuation/` | 持仓估值聚合（含 FX 三角换算） |
| `bank_sync/` | GoCardless 银行直连（scaffold，未启用） + crypto.py（55 行 stub） |
| `notion_sync/` | Notion 三模块同步（scaffold，未联调） |

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

### 1. PDF 上传 → 入库 → 自动分类 + 转账识别 + 级联

```
[Web UI] → POST /statements/upload?account_id=X (multipart)
       → SHA-256 hash 去重
       → asyncio.to_thread(parse_pdf_statement)：pdfplumber 抽 text + words
       → _detect_bank() 按 BIC/域名识别银行
       → 分发：
           - Revolut → _parse_revolut_columns（按 Money out/in 列定位）
           - 其他 → text-regex parsers
       → 每条 tx 经 _classify_transfer：
           - 子账户关键词 / 用户清单 → type='transfer' + metadata.subaccount=true
           - 跨行 cue（Outgoing Transfer / SEPA / 配置的 owner 姓名）→ type='transfer' + cross_bank_hint
       → batch insert 到 transactions
       → 每条 categorize_transaction(rules)
           - 命中 → category_id 写入 + is_pending=False（自动通过 inbox）
           - 未命中 → is_pending=True
       → transfer 类直接 is_pending=False
       → auto_pair_after_import：
           1. detect_same_account_pairs (L3) → mark_subaccount_pair
           2. find_transfer_pairs (cross-account, score≥75 自动) → pair_transactions
       → 返回 preview（前 5 笔，用 selectinload 避免 lazy load）

[用户在 inbox] → POST /transactions/inbox/{id}/confirm {category_id, user_note?}
       → setattr(category_id, user_note); is_pending=False
       → learn_from_user_assignment：从 description 提取关键词建/加强规则
       → apply_to_similar_pending：同 description 兄弟全部级联（含已分类的，
                                  保护 source!=manual / type!=transfer / type==seed.type）
       → cashflow recompute_for_periods（含级联兄弟所在月份）

[用户在 transactions/分类视图] → PATCH /transactions/{id} {category_id, type?}
       → 同上学习 + 级联（跨 kind 时不级联，保护其他兄弟）
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

- **写入**：transaction CRUD（create/batch/update/delete）+ statement confirm + adjust-balance + 级联学习全部 hook 后**同步重算**（单用户场景无需 debounce）
- **读取**：dashboard 直接读 `cash_flow_snapshots`（O(months)）
- **层级化视图**：`/transactions` 默认 tab `CategoryBreakdownView` — 月份选择 + kind 切换 + 双栏（一级类目卡 + 占比条 → 二级类目 + 明细 + 占比条），明细行支持内联跨 kind 改分类（触发级联）

### 3.5 余额视图（避免双计的关键）

`v_account_balance` 视图（启动时由 lifespan DROP+CREATE 重建）：

- `subaccount=true` 的 transfer 跳过（钱在同银行内，不影响整体余额）
- 已配对的 transfer 按 `transfer_direction` 取符号（in +ABS / out −ABS）
- 未配对 transfer 默认 −ABS（单边视角假定为出账）
- expense −ABS / income +ABS / adjustment 保留原符号

`transactions.amount` 始终存正绝对值，方向由 `type` + metadata 决定（adjustment 例外保留符号）。

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
