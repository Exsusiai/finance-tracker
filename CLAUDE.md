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

- **银行检测按最早出现位置**（2026-06）：`_detect_bank` 取 `_BANK_MARKERS` 中**在文中出现位置最靠前**的银行。发行行标识在头部、对方银行 BIC 只在正文转账行——earliest-position 天然选发行行。修复了 N26↔Revolut 互转账单的交叉误判（N26 账单含 Revolut 的 `revodeb2`，反之含 `ntsbdeb1`）。
- **导入暂存流程（2026-06，preview-before-commit）**：upload **只解析不入库**，落 `status='awaiting_review'` 并返回**全部** `parsed_preview`（解析输出，非 DB 行）。`POST /statements/{id}/commit?account_id=` 才真正插入 + 跑 ingestion；`DELETE`（取消）连带删除暂存记录 + 存储的 PDF 文件（无痕，可重传）。`account_id` 可选（用 upload 时解析的候选账户）。旧 `awaiting_account`/`assign-account`/`confirm`(翻 is_pending) 保留向后兼容。`GET /statements/{id}` 对 awaiting_* 状态**重解析**出预览（DB 里还没有行）。
- **银行识别覆盖的两道防线**（不做「改银行重新解析」——冗余）：(1) 上传时 `bank_format` 下拉手动指定；(2) 暂存预览里看到识别结果不对就取消重传。已入库的:撤销 + 重传。`list` 加 `offset` + `meta.total` 支持「加载更多」。`PdfImportStatus` 新增 `awaiting_review`（lifespan 幂等重建 CHECK）。

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

### 券商同步（IBKR Flex，2026-06-10 实装）

- **目标**：把传统券商（首发 Interactive Brokers）的持仓与估值纳入资产视图。复用 crypto/CEX 的 `wallet_sync` orchestrator + `asset_holdings` 模型。
- **为何选 Flex Web Service**：它是**报表 API（非交易 API）**，所有 IBKR 账户类型（含 **Lite**，无 Pro / 入金要求）都能用；token 机制不涉及 IB 登录密码。代价：**收盘后快照（EOD）**，无盘中实时（实时需 Pro + 常驻 Client Portal Gateway，不符 local-first）。
- **复用现成库**：`ibflex2`（`ibflex` 的安全 fork，defusedxml + 容忍新字段）——**只用其 parser**。两步下载（SendRequest 拿 ReferenceCode → GetStatement 轮询）是我们自己的 **async httpx** 实现（稳定端点 `gdcdyn.../Universal/servlet/FlexStatementService.*`，必带 `user-agent` 头；**不用** fork client 里硬塞的实验性 v1 URL）。
- **包结构**：`services/broker_sync/`：`__init__.py`（`BrokerPosition` + `BrokerProvider` Protocol + `dispatch` + `map_asset_class`）/ `ibkr.py`（`IBKRFlexProvider`，可注入 `client`+`sleep` 供 MockTransport 测试，错误码 1009/1019 重试、1018 限流、1012/1015 等致命）/ `upsert.py`（`apply_broker_snapshot`）。
- **数据模型**：`broker_connections`（仿 `exchange_connections`：`provider='ibkr'`、`token_enc` AES-256-GCM 加密、`query_id` 明文非密、`last_sync_*`，唯一 `(account_id, provider)`）。`accounts.type='brokerage'` 在 baseline 就允许，无需改 CHECK。
- **Asset 映射**：`map_asset_class(assetCategory, currency)` 把 IBKR `STK`+币种 → `us_stock`/`eu_stock`/`a_share`，`FUND/ETF`→`fund`，`BOND`→`bond`，未知→`other`（绝不抛错）。Asset 标识沿用非加密约定 `chain=''`/`contract=''`，**conid 存 `data_source_id`**（靠 `(asset_class, symbol)` 与用户手动建的同名 Asset 合并 + 回填 conid）。
- **价格**：Flex 自带 `markPrice`/`currency`，upsert 直接写 `market_prices`（`source='ibkr'`，**原币**，非 USDT）——所以 brokerage 分支**跳过** CoinGecko 刷价。`market_prices` 唯一键 `(asset_id, source, quoted_at)` → 同秒重复同步用 **upsert 守卫**避免 IntegrityError。
- **估值/折算**：`convert_to_base` 已抽到 `services/valuation/fx.py`（holdings.py 委托保留旧名）；`/holdings`、`/portfolio/summary`、`/net-worth` 走它，**天生支持多币种（USD/EUR→CNY）**，无需改动。`/accounts/balances` 用新 `compute_brokerage_value_per_account`（折 base 币种）给券商账户补值（区别于 crypto 的 USDT 路径）。
- **API**：`/accounts/{id}/broker-connection`（GET/PUT/DELETE，token 加密入库绝不回显）+ 复用 `POST /accounts/{id}/sync`（orchestrator 加 brokerage 分支）。`security_health` 把 broker token 纳入凭据健康自检。`_safe_error_text` 加 Flex token（`t=` 查询参数）脱敏。
- **环境变量**：无新增——Flex token 走 UI 加密入库，仅依赖既有 `FINANCE_BANK_ENCRYPTION_KEY`。用户一次性在 IBKR Client Portal 网页配置（Settings → Reporting → Flex Queries → 启用 Flex Web Service 生成 token + 建 Activity Flex Query **只需勾 Open Positions**（解析器只读这一 section，不用 Equity Summary）+ Format=XML + Date Format=yyyyMMdd，记 Query ID）。
- **前端**：`AccountForm` 的 `CONNECTION_SETUP_TYPES` 含 brokerage（创建后不关弹窗，内嵌 `BrokerConnectionEditor` 填 Query ID + Token，必须 `<div>` 非嵌套 form）。`SyncAccountButton` ↻ 对 brokerage 也显示（settings 页）。
- **未做（下一阶段）**：盘中实时；Flex `Trades` 流水导入（目前只做持仓快照）。

### Trade Republic 同步（2026-06-23 实装，未 UAT）

- **目标**：把 Trade Republic（德国 neobroker）持仓纳入资产视图，复用 `broker_sync` + `wallet_sync` orchestrator + `asset_holdings`，与 IBKR 同表 `broker_connections`。
- **为何与 IBKR 本质不同**：TR **无官方 API**，用社区逆向库 `pytr`（仅只读 portfolio/cash，**绝不下单**，违反 ToS、可能随 TR 改版失效）。认证是交互式 **Web login**：手机号+PIN → 4 位码（App/SMS）→ **cookie session**（无静态 token）。选 Web login 而非 App login：不登出用户手机（TR 限单设备），代价是 session 过期需重新连接。
- **AWS WAF**：TR 登录有 AWS WAF 保护。**实测（2026-06-23）：纯 Python `awswaf`（curl_cffi）路径生成的 token 被 TR 的 WAF 拒绝（HTTP 405，ALB 层无 `x-amzn-waf-action` 头）；只有 `playwright`（无头 Chromium）路径被接受**（同样的假手机号 playwright 路径返回 400 NUMBER_INVALID = WAF 已放行）。所以默认 `_WAF_TOKEN="playwright"`（env `FINANCE_TR_WAF_METHOD` 可切回 awswaf 备未来 pytr 修复）。**需先装浏览器**：`python -m playwright install chromium`（Ubuntu 无头：`--with-deps`）。**关键缓解**：playwright 只在**登录**（发验证码）时启动一次；日常余额同步走 cookie `resume_websession`，**不启动浏览器**，所以服务器平时不重。排查过程：UA / JA3(curl_cffi impersonate) / 浏览器头 全试过都 405，唯独 playwright token 通过 → 确认是 awswaf token 本身无效。
- **包**：`services/broker_sync/traderepublic.py`：`TradeRepublicProvider`（cookies→`resume_websession`→websocket 读）+ `initiate_login`/`complete_login`/`cleanup_login`（两步登录助手，同步调用在 API 层用 `asyncio.to_thread` 包）+ `normalize_phone`（去空格/连字符、`00→+`——TR 对带空格号码返 `NUMBER_INVALID`）+ `map_instrument_type_to_asset_class`。`pytr.run_blocking` 用 `asyncio.run` 会与 FastAPI loop 冲突——provider 里直接 `await` 底层 `subscribe`+`_recv_subscription`。
- **持仓读取（关键：topic 会变）**：pytr 0.4.9 的 `portfolio`/`compactPortfolio` topic **已被 TR 废弃**（返回 `BAD_SUBSCRIPTION_TYPE`）。当前用 **`compactPortfolioByType`**（返回 `{categories:[{categoryType, positions:[{isin, netSize, averageBuyIn, instrumentType, name}]}]}`，**无现价**）→ 再对每个 ISIN 拉 **`ticker`**（`{isin}.LSX`，Lang & Schwarz）取 `last.price`（EUR）。`cash`/`portfolioStatus`/`availableCashForPayout` 也可用。`instrumentType`（stock/fund/bond/crypto/derivative）→ asset_class（stock 按 ISIN 前缀分 us/eu）。
- **数据模型**：复用 `broker_connections`（迁移 `b2c3d4e5f6a7`：provider CHECK 加 `'traderepublic'`、`query_id` 改可空 + 去掉非空 check）。TR 行：`token_enc` 存**加密的 cookie jar 文本**、`query_id=NULL`、`metadata_json` 存脱敏手机号。`BrokerPosition` 加可选 `asset_class`（TR 预填；IBKR 留 None 走 `map_asset_class`）；标的 `data_source='traderepublic'`、`data_source_id=ISIN`、currency=EUR、price 来自 ticker `last`。ticker 失败时持仓仍入库（price=None，只缺市值）。
- **两步连接 API**（TR 专属，IBKR 仍用 PUT）：`POST /accounts/{id}/broker-connection/tr/connect`（手机+PIN→initiate→**进程内 `_TR_PENDING` 暂存 live pytr 实例**，带 TTL=countdown+120s）→ `POST .../tr/verify`（4 位码→complete→加密 cookies 入库）。单进程 local-first 才安全；重启则重新发起。orchestrator brokerage 分支按 `row.provider` dispatch，TR 走 cookies 路径无需改动。
- **估值**：复用 `convert_to_base`（EUR→CNY）+ `compute_brokerage_value_per_account`，与 IBKR 同路径。
- **测试**：`test_broker_sync.py` 用 `_FakeTR` monkeypatch `traderepublic._new_api`，覆盖 ISIN 映射 / 持仓映射 / fetch / 过期 session / 登录 round-trip / orchestrator——不连真网。真实登录需用户用真凭据 UAT。
- **未做**：会话自动续期（过期就提示重连）；instrument_details 富化 asset_class（当前 ISIN 启发式）；交易流水导入。

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
