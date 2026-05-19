# CLAUDE.md

This file provides guidance to Claude Code (claude.ai/code) when working with code in this repository.

## Project Overview

Finance Tracker — 个人资金管理与记账系统。**Local-first, single-user**。三个组件共用一份 SQLite 数据库：

- **`backend/`** — FastAPI + SQLAlchemy 2.x async + SQLite (WAL)，端口 **8000**
- **`frontend/`** — Next.js 15 (App Router) + React 19 + Tailwind + shadcn/ui + Recharts + SWR，开发端口 **3002**（PROGRESS.md 中的实际运行端口；`next dev` 默认 3000）
- **`mcp-server/`** — Python MCP SDK (stdio)，通过 `PYTHONPATH` 复用 `backend/app` 模块和同一份 `data/finance.db`

详细架构见 `docs/ARCHITECTURE.md`，技术选型理由见 `TECH_STACK.md`，进度跟踪见 `PROGRESS.md`。

## Common Commands

### Backend (run from `backend/`)

```bash
pip install -e .[dev]                    # install with dev tools
uvicorn app.main:app --reload --port 8000   # dev server
finance-tracker                          # CLI entry (uvicorn wrapper, no reload)
pytest                                   # all tests (asyncio_mode=auto)
pytest tests/test_pdf_parser.py          # single file
pytest tests/test_api.py::test_health    # single test
pytest --cov=app --cov-report=term-missing
ruff check app/                          # lint
ruff format app/                         # format
mypy app/                                # type check
```

API 文档自动生成于 `http://localhost:8000/docs` (Swagger) 与 `/redoc`。

### Frontend (run from `frontend/`)

```bash
npm install
npm run dev      # next dev (默认 3000；项目惯例用 -p 3002)
npm run build
npm run start
npm run lint     # next lint
```

设置后端根地址（浏览器侧）：`NEXT_PUBLIC_API_URL=http://localhost:8000`（不含 `/api/v1` 后缀，前端 `lib/api.ts` 自己加）。Bearer token 不再走 `NEXT_PUBLIC_*` 注入（那样会编进 bundle 公开）——用户在 Settings → API Token 输入框粘贴，存 `localStorage["finance_api_token"]`。本地默认 `AUTH_DISABLED=true` + loopback 时不需要 token。

### MCP Server

```bash
./mcp-server/run.sh    # stdio MCP, 通过 PYTHONPATH 注入 backend/
```

依赖项目根目录 `.venv/bin/python`（即 backend 装好的虚拟环境），无需独立安装。

### Docker

```bash
docker compose up -d              # 启动 backend + frontend
docker compose --profile mcp up   # 含 MCP server
```

数据卷：宿主机 `./data/` → 容器 `/app/data/`（含 `finance.db`、`pdfs/`、`backups/`）。

## Architecture & Conventions

### 数据层（关键约束）

- **金额一律 `NUMERIC(20, 8)` + Python `Decimal`**，禁止 `float`。8 位小数支持加密货币。
- **币种 ISO-4217 三字母大写**：`CNY`/`EUR`/`USD`/`BTC` 等（加密视作伪币种）。
- **时间 ISO-8601 UTC 字符串**：`"YYYY-MM-DDTHH:MM:SSZ"`，存为 `String(30)`，由 `_utcnow_str()` 生成。
- **软删除**：`deleted_at` 列（NULL = 活跃）。查询时一律加 `WHERE deleted_at IS NULL` 过滤。
- **SQLite PRAGMA**：每次连接自动启用 `journal_mode=WAL` / `foreign_keys=ON` / `synchronous=NORMAL`（见 `app/db/session.py`）。
- **Schema 来源**：启动时 `Base.metadata.create_all()` 兜底创建表，**2026-05-07 起新增 schema 改动走 alembic**（`backend/alembic/`，async 配置；当前 head = `1ed07e31cab5_baseline_2026_05_07`）。流程：
  - 改 ORM 模型 → `cd backend && alembic revision --autogenerate -m "<change>"` → 检查生成的 revision → `alembic upgrade head`
  - 已存在的 DB 第一次接入 alembic：`alembic stamp head`
  - lifespan 里旧的 inline DDL 迁移保留作向后兼容，已是幂等
  - `docs/SCHEMA.sql` 仍是手写参考文档
- **`v_account_balance` 视图**在启动时由 lifespan 创建，用于读取账户余额（=`initial_balance + SUM(transactions.amount)`）。

### 后端代码结构

```
backend/app/
├── core/        # config (pydantic-settings)、auth (Bearer token)、errors、structlog
├── db/          # async engine、session factory、Base、(planned) Alembic migrations
├── models/      # SQLAlchemy ORM, 所有实体集中在 models/__init__.py
├── schemas/     # Pydantic v2 请求/响应模型
├── api/v1/      # 路由薄壳: accounts, categories, transactions, statements, assets,
│                # holdings, market, cashflow, rules, system, notion, bank_sync
├── services/    # 业务逻辑: pdf_parser, market_data, asset_search, categorizer,
│                # valuation, notion_sync, bank_sync (GoCardless)
└── main.py      # FastAPI app + lifespan + /health, /version (公开)
```

路由设计：`api/v1/*.py` 仅做参数校验和调用 service；业务规则全部在 `services/` 内。`api_router` 在 `api/v1/__init__.py` 聚合，全部挂在 `/api/v1/*` 下。除 `/api/v1/health` 与 `/api/v1/version` 外，所有端点要求 `Authorization: Bearer <FINANCE_TRACKER_API_TOKEN>`。

### 前端代码结构

```
frontend/src/
├── app/         # Next.js App Router: dashboard / transactions / assets /
│                # analytics / settings (root page → redirect /dashboard)
├── components/  # ui/ 是 shadcn/ui，业务组件平铺：charts、*-form、sidebar 等
└── lib/        # api.ts (fetch wrapper + 统一错误)、hooks (SWR)、time-range、utils
```

API 响应封装：`{ success, data, error, meta }`。`lib/api.ts` 的 `request<T>()` 自动解包 `data` 并把错误转成 `ApiError`。

### PDF 解析架构

银行账单格式碎片化，**没有通用解析器**。`services/pdf_parser/engine.py` 内通过文本特征检测银行（icbc/cmb/n26/revolut/...），分发到对应解析器。新增银行 = 加一个 parser 文件 + 在 detector 注册关键词。`pdfplumber` 是主力库，`pypdf` 兜底。

### 市场数据 / 估值

- `services/market_data/` 通过 APScheduler 定时拉取 yfinance / CoinGecko / exchangerate.host / metals，写入 `market_prices` 与 `fx_rates` 表。
- `services/valuation/` 估值时**只读本地表**——上游挂掉也能基于上次缓存正常显示组合估值。
- 折算原则：原始 transaction 保留原币，**只在汇总展示时折算到 `BASE_CURRENCY`**（默认 `CNY`）。

### LLM 智能分类（P1-1，2026-05-08 实装）

- **Provider 抽象**：`backend/app/services/llm/`：`provider.py` 定义 `LLMProvider` Protocol → 当前唯一实现 `gemini.py`（`google-genai` SDK）。新加 OpenAI / Anthropic = 新增一个 provider 文件，无需改其他代码。
- **运行时配置**：环境变量只放 secret（`GEMINI_API_KEY`），其他可调项（`llm_enabled` / `llm_model` / `llm_monthly_usd_budget` / `llm_confidence_threshold` / `llm_use_grounding` / `llm_max_notes_in_prompt`）放在 `app_settings` KV 表，可经 `/api/v1/llm/settings` 改，不必重启。lifespan 启动时 idempotent seed 默认值。
- **三层管道**：`services/categorizer/engine.py::categorize_transaction` 返回 `MatchResult(matched, requires_llm)`；`services/ingestion/__init__.py` 在 L1 后追加 L2 异步派遣（`asyncio.create_task` + 各任务起独立 session）。L2 实现：`services/llm/classifier.py::classify_with_llm` —— 检索 top-N 知识库条目（按 token 重合度）→ 构造 prompt → Provider 调用 → 解析 JSON → 写 `tx.categorization_method/confidence/llm_reason` 或在 inbox stash `metadata.llm_suggestion`。
- **「污染」语义**：L1 命中 + `rule.requires_llm=True` ⇒ 不短路，路由到 L2。每当用户改分类同时写了 `user_note`，`services/categorizer/engine.py::record_note_to_kb` 把同 keyword 的 L1 规则全部翻为 `requires_llm=True` 并新增一行 `categorization_notes`（解决 PayPal+amount 复合规则）。
- **来源限制**：仅 `source ∈ {pdf_import, bank_api}` 的交易走 LLM；`manual` / `mcp_agent` 跳过（用户已亲自录入）。
- **成本/预算**：`services/llm/cost_tracker.py` 按月 KV 累加（`app_settings` 中 `llm_monthly_cost_usd_YYYY_MM`）；超额时 classifier 直接返回 abstain。
- **API 端点**：`/api/v1/categorization-notes` (CRUD)、`/api/v1/llm/settings` (GET/PUT)、`/api/v1/llm/cost`。
- **前端**：Settings 页两个新 section —— 「智能分类」(LLMSettingsForm) + 「分类知识库」(CategorizationNotesTable)；Inbox 行内显示 ✨ LLM 推荐 + 一键采纳。

### 加密钱包 / CEX 同步（P1-4，2026-05-18~19 实装）

- **包结构**：
  - `services/crypto_sync/`：一个链一个 provider 文件 + `dispatch(chain, api_key)` 路由。Alchemy（11 EVM L1+L2）/ Blockstream（BTC）/ 公共 Solana RPC / TronGrid。所有 provider 接受可注入的 `httpx.AsyncClient`，测试通过 `MockTransport` stub 不打外网。
  - `services/exchange_sync/`：`ExchangeProvider` Protocol + Binance / Bitget 实现 + `sign.py` 集中 HMAC 工具（method 强制大写避免 Bitget 40006）。Bitget 调 4 个端点（spot + USDT-M + USDC-M + COIN-M），按 coin 跨端点 `sum(available + locked)`，**不计 unrealizedPL**。
  - `services/wallet_sync/`：跨切面 orchestrator。`sync_account` 按 `account.type` 派遣 → 收齐 `BalanceItem` → `apply_balance_snapshot` upsert 持仓 + 缺失 token 设 `quantity=0, is_active=False` → `_refresh_prices_for_account` 拉 CoinGecko 价格写 `market_prices`。`spam_filter.is_spam_token` 在 upsert 前过滤空投诈骗 token；`holdings_value.compute_holdings_value_per_account` 给 `/balances` 加 `SUM(quantity × latest_price)`。
  - `services/market_data/coingecko.py`：`fetch_native_price` by ticker / `fetch_token_prices` by contract（**per-contract 循环**，免费 tier 每调用 1 合约的限制）。
- **数据模型**：
  - `chain_addresses (id, account_id, chain, address, label, last_sync_*)`，`(account_id, chain, address)` 唯一约束。
  - `exchange_connections`：仿 `bank_connections`，三列加密 (`api_key_enc / api_secret_enc / api_passphrase_enc`)，AES-256-GCM via `FINANCE_BANK_ENCRYPTION_KEY`。
  - `asset_holdings` 加 `chain`（NOT NULL DEFAULT `''`，非加密用 `''`）+ `is_active`（缺失 token 标 False），`(account_id, asset_id, chain)` 唯一。
  - `accounts.include_in_total` 默认 1；`net_worth` 两条腿都 JOIN accounts 过滤。
- **API**：`POST /accounts/{id}/sync`（阻塞同步，返回 `SyncSummary`）/ `/accounts/{id}/addresses` CRUD / `/accounts/{id}/exchange-connection`（PUT 加密入库，secrets 绝不回显）。`/accounts/balances` 对 crypto / exchange 类型在 v_account_balance 之外追加 `holdings × 最新价`。
- **汇率**：`holdings.py::_convert_to_base` 把 USDT/USDC/DAI 等 **USD-pegged stablecoin 别名为 USD**；三角换算 pivot 含 **CNY**（项目 FX 源全部 `base_currency='CNY'`）。
- **环境变量**：`ALCHEMY_API_KEY`（必填，EVM 链同步）；`FINANCE_BANK_ENCRYPTION_KEY`（32 字节 hex，必须，否则 exchange 凭据写入 500）。BTC / Solana / Tron 不需要 key。
- **前端**：`AccountForm` 加 `exchange` 枚举 + 类型条件渲染（IBAN 仅 bank/credit_card，初始余额隐藏 crypto/exchange）+ 创建后切「添加地址 / API 凭据」模式不关弹窗。`ChainAddressesEditor` / `ExchangeConnectionEditor` 内嵌时**必须用 `<div>`**（嵌套 form 会被浏览器折叠触发外层 submit）。`SyncAccountButton` ↻ 显示每个错误源详情。`INVESTMENT_TYPES = {brokerage, crypto_wallet, exchange}` 在 bank / credit_card 等账户上隐藏「添加持仓」入口。

### MCP Server 与后端的关系

- `mcp-server/run.sh` 把 `backend/` 加入 `PYTHONPATH`，所以 MCP server 直接 `from app.models import ...` 复用同一份 ORM 模型与服务。
- MCP server 当前用**同步** `sqlite3` 连接（见 `finance_mcp/server.py` 头部），后端用 **async** `aiosqlite`——两侧通过 SQLite WAL 模式安全共享。
- **LLM 路径**：MCP `add_transaction` 不走 LLM（source=`mcp_agent`，与 `manual` 同样跳过自动分类）。如果想让 agent 也享受 LLM 分类，未来可在 `services/llm/classifier.py` 改条件。

## Code Conventions

- **Python 3.11+** （`StrEnum`、`X | Y` 联合类型、`from __future__ import annotations`）。
- **Ruff 规则**：`E,F,I,N,W,UP,B,SIM,RUF`，行宽 100（`backend/pyproject.toml`）。
- **TypeScript** 使用 strict mode；React 组件 props 用具名 `interface`。
- **结构化日志**：后端用 `structlog.get_logger(__name__)`，事件名 snake_case (`finance_tracker_started`)；勿用 `print`。
- **更新时间戳**：async SQLAlchemy 不会自动维护 `updated_at`，写操作后调用 `models.touch_updated_at(instance)`。

## Environment

- `.env` 在项目根目录（**不是** `backend/.env`）；`Settings` 通过 `_PROJECT_ROOT / ".env"` 读取。
- 必填：`FINANCE_TRACKER_API_TOKEN`（32 字节 hex）。未设置时启动会自动生成临时 token 并 warn——**不要把这个临时 token 当持久化值**。
- `BASE_CURRENCY` 控制汇总折算的目标币种，默认 `CNY`。
- 银行同步密钥用 `FINANCE_BANK_ENCRYPTION_KEY` 加密后入库（`bank_connections` 表 + `exchange_connections` 表）。**P1-4 后也是必填**：交易所 API key 走同一加密路径。生成：`python -c "import os; print(os.urandom(32).hex())"`。
- LLM 分类需要 `GEMINI_API_KEY`（去 https://aistudio.google.com/apikey 申请）。运行时其他 LLM 配置（model/budget/threshold/grounding）走 Settings UI，不在 env。
- 加密钱包 EVM 链同步需要 `ALCHEMY_API_KEY`（https://www.alchemy.com 免费注册，300M CU/mo）。BTC / Solana / Tron 走公共端点不需要 key。

**注意：** 改 `.env` 后 `uvicorn --reload` **不会**自动读取（pydantic-settings 是启动一次性载入）。必须 kill 进程 + 重新 `uvicorn` 启动。
