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

**账户管理统一在 `/assets`（2026-06-27）**：所有账户（银行/信用卡/券商/加密/交易所/现金）的增删改、同步、子账户清单、余额调整、拖动排序全部在资产页「账户」tab。设置页只剩分类管理 / 智能分类 / 知识库 / API Token（不再有账户列表）。
- **拖动排序**：账户卡左上角 ⠿ 把手（仅把手 `draggable`，避免破坏卡内输入/按钮）→ `PATCH /accounts/reorder`（全量有序 id 列表）持久化 `accounts.sort_order`；`list_accounts` 按 `sort_order, id` 排序，新账户 `sort_order` 自动置末。
- **显示币种**：`lib/utils.ts` 的 `DISPLAY_CURRENCIES` 收敛为 CNY/USD/EUR/USDT 四种（总览 + 资产页的「显示币种」切换共用）。
- **分析页分类分布周期**：底部「支出/收入分类分布」有独立周期控件（单月 / 区间汇总），独立于上方图表的时间范围，且自己管理 loading 不连带其它图表。后端 `/cashflow/by-category` 支持 `?period=` 或 `?from=&to=`（区间聚合）。分类分布**上卷到一级类目（大类）**：前端用 `useCategories()` 建 child→top-level 映射再喂 `ExpensePieChart`，避免铺满二级类目看不清。
- **饼图标签坑**：recharts 的 label/tooltip 回调入参里,**data row 的同名字段会 shadow recharts 注入的 `percent`(0–1 分数)**。`ExpensePieChart` 的 data 自带 `percent`(已×100),所以 label 里别读 `percent`,改用 `value`+闭包 `total` 自己算(见 `charts.tsx`)。

**总览(dashboard)改版(2026-06-27)**：只保留「总资产」卡(净值 + 按币种 + 资产类型 mini-cards),去掉了本月收支/储蓄率/快捷操作/最近交易。两张图:
- `FinancialFlowChart`(收支与现金资产趋势)：每月收入/支出(左轴) + **现金资产**(右轴)。现金资产 = 所有现金/银行/信用卡(=非快照、`include_in_total`)账户在该月末的**真实余额**(initial_balance + 账本签名累加,按币种折 base)。**不是**从 0 滚加的净储蓄(那版误导,已废弃)。数据来自 `/cashflow/timeseries`(钉 BASE_CURRENCY)。
- `PortfolioValueChart`(组合市值走势)：来自 `portfolio_snapshots` 的**前向周度快照**(投资标的市值)。
- **总资产 = 现金资产 + 组合市值** 恒等成立:现金资产线最新点 == `net_worth.cash_total`,组合市值 == `investment_total`。现金资产含信用卡负债(净现金口径),所以早期月份可能为负(净卡债)。

### 现金资产历史（cash_history，可回溯）

- 现金历史**可从账本重建**(不像组合市值):`services/valuation/cash_history.py::compute_cash_history` 按 (币种, 月) 累加签名金额(镜像 `v_account_balance` 的符号 CASE,含 pending、排除子账户移动),每币种桶用**最新 FX** 折 base ——与 `net_worth` 现金腿同口径,故最新点必然等于 `cash_total`。`/cashflow/timeseries` 新增 `cash[]`(carry-forward 对齐到各月)。

### 组合市值快照（portfolio_snapshots，周度）

- **为何前向**：组合市值历史**无法回溯**——`asset_holdings` 只存当前数量(crypto/券商是快照同步,无逐周持仓历史)。scheduler **每周** upsert 当周一行(键=本周一 `period`='YYYY-MM-DD' 唯一,最新估值),跨周自动冻结上周值。
- **净值口径单一来源**：`services/valuation/net_worth.py::compute_net_worth(db, base)` 是净资产(现金+投资,折 base)的**唯一实现**,`GET /holdings/portfolio/net-worth` 与快照 job 都调它 → 数字必然一致(同 `paired_dedup_predicate`/`_AMOUNT_BASE_EXPR` 的单源理念)。快照逻辑在 `services/valuation/snapshot.py`。
- **调度**：`market_data/scheduler.py` 的 `portfolio_snapshot` job(每周 + 启动后 ~30s 首跑)。读取端点 `GET /holdings/portfolio/value-history`(按 period 升序)。
- **未做（C 方案明确不做）**：被动收入(已实现现金)线、已实现/未实现拆分。被动收入若以后做,用「类目加开关」(给 Category 加 `is_passive_income`),已与用户对齐但本期未实现。

### PDF 解析架构

银行账单格式碎片化，**没有通用解析器**。`services/pdf_parser/engine.py` 内通过文本特征检测银行（icbc/cmb/n26/revolut/...），分发到对应解析器。新增银行 = 加一个 parser 文件 + 在 detector 注册关键词。`pdfplumber` 是主力库，`pypdf` 兜底。

- **银行检测按最早出现位置**（2026-06）：`_detect_bank` 取 `_BANK_MARKERS` 中**在文中出现位置最靠前**的银行。发行行标识在头部、对方银行 BIC 只在正文转账行——earliest-position 天然选发行行。修复了 N26↔Revolut 互转账单的交叉误判（N26 账单含 Revolut 的 `revodeb2`，反之含 `ntsbdeb1`）。
- **导入暂存流程（2026-06，preview-before-commit）**：upload **只解析不入库**，落 `status='awaiting_review'` 并返回**全部** `parsed_preview`（解析输出，非 DB 行）。`POST /statements/{id}/commit?account_id=` 才真正插入 + 跑 ingestion；`DELETE`（取消）连带删除暂存记录 + 存储的 PDF 文件（无痕，可重传）。`account_id` 可选（用 upload 时解析的候选账户）。旧 `awaiting_account`/`assign-account`/`confirm`(翻 is_pending) 保留向后兼容。`GET /statements/{id}` 对 awaiting_* 状态**重解析**出预览（DB 里还没有行）。
- **银行识别覆盖的两道防线**（不做「改银行重新解析」——冗余）：(1) 上传时 `bank_format` 下拉手动指定；(2) 暂存预览里看到识别结果不对就取消重传。已入库的:撤销 + 重传。`list` 加 `offset` + `meta.total` 支持「加载更多」。`PdfImportStatus` 新增 `awaiting_review`（lifespan 幂等重建 CHECK）。

#### 期末余额提取 + 对账锚定（2026-06-27）

- **问题**：账户 `initial_balance` 记账前填的是 0,不是真实期初 → 整条余额曲线被平移一个常数(现金资产线/净值现金腿都受影响)。
- **解法:从 PDF 提取「这一期期末余额」反推 `initial_balance`**。`engine.py::extract_closing_balance(bank, text)` 按银行抽期末余额(**as printed**,调用方按账户类型定符号):
  - n26: 所有 `Your new balance ±X€` **求和**(主账户+各 Space;子账户移动被账本忽略,所以总额=主+Space)。
  - amex_de: `Saldo des laufenden Monats für… X`;tfbank: `Neuer Saldo: X`(点小数);advanzia: `NEUER SALDO X`。
  - revolut: 期末在定位列里,**返回 None**(优雅跳过,不做对账)。
- **符号**:`services/valuation/anchor.py::normalize_closing` 对 `credit_card` 取 `−abs`(账本里卡债为负;advanzia/AMEX 印正数、TFBank 印负数,统一成负);资产账户原样。
- **锚定(race-free)**:`anchor_account_balance(db, acct, balance, as_of)` 设 `initial_balance = balance − Σ(signed tx ≤ as_of)`(签名口径镜像 `v_account_balance`,见 `cash_history._SIGNED_AMOUNT`)。**不建交易、平移整条历史**。可任何时候做(减掉了截至 as_of 的已记交易,as_of 之后的交易自然叠加)→ 不用卡「月底且下一笔开支之前」。端点 `POST /accounts/{id}/anchor-balance`(快照账户拒绝)。
- **对账(catch 记账错误)**:`commit_statement` 插入后调 `compute_reconciliation`(as_of = 本期最后一笔交易日)→ 返回 `{closing_balance, computed_balance, discrepancy, currency, as_of}` 进 `PdfImportOut.reconciliation`。前端 `pdf-import-panel` 若 `|discrepancy|>0.005` 弹⚠️卡(账单期末/系统推算/差额)+「以账单为准锚定」按钮(调 anchor);一致则显示 ✓。**不自动改 `initial_balance`**——透明展示,用户点按钮才锚,这样漏记/重复能被发现而非被静默吸收。
- **未做**:revolut 期末(定位列解析);PDF 自动从期末日期解析 as_of(目前用最后一笔交易日,对信用卡中周期也对)。

### CSV 导入 / PayPal（2026-06-25 实装）

PDF 之外的来源走 CSV。和 PDF 一样**没有通用 CSV 格式**——每个来源一个 parser，按表头签名识别。首个（目前唯一）来源：**PayPal**。

- **包结构**：`services/csv_parser/`：`paypal.py`（`parse_paypal_csv`/`is_paypal_csv`）+ `__init__.detect_and_parse_csv`（按表头分发）。输出与 PDF parser 同 shape（`transactions[]` 里的 dict key 与 `_insert_and_ingest` 读取的一致），所以复用 ingestion 管道。`services/csv_import/import_csv_rows` 做**行级去重 + 插入 + ingest**。
- **PayPal CSV 要点**：英文表头、`DD.MM.YYYY`、逗号小数、UTF-8 BOM；金额用 `Net`（余额影响），存 ABS，方向在 type/`metadata.transfer_direction`。`external_id = Transaction ID`（PayPal 偶尔**复用**一个 id，如 ACH 提现+其反转——parser 给后出现者加确定性 `#N` 后缀保证唯一且可重复去重）。
- **行分类**：`Bank Deposit to PP Account`/`General Card Deposit` → transfer in（银行/卡→PayPal 的充值腿）；`User Initiated Withdrawal`/ACH 反转 → transfer out；其余（Mobile Payment/Express Checkout/PreApproved Bill/Payment Refund/EUR 货币转换腿）→ income/expense。**外币购买**（如 GitHub USD）：跳过净额为 0 的非 EUR FX 清算对，保留 EUR `General Currency Conversion` 作为真实支出（用 Reference Txn ID 回填商户名）。
- **去重（关键）**：CSV 上传**预期日期范围重叠**（用户嫌登录麻烦，一次传几个月）。按 `external_id` 行级去重——已存在的行跳过，重复上传重叠月份安全。端点 `POST /statements/upload-csv?account_id=`（**直接入库，无 preview**，区别于 PDF 的暂存流程）。
- **开户余额**：首次导入时把账户 `initial_balance` 设为 CSV 的开账余额（最早行 `Balance − Net`），使账户余额等于 PayPal 真实余额（只导了一个窗口，窗口前的余额=开账余额）。仅在账户为空且 `initial_balance==0` 时 seed，避免后续重叠上传移位。
- **PayPal 账户建模**：`type='bank'` 流水账户（非快照），EUR；充值/提现腿与银行账单里的对应记录由 transfer-matcher 配对，形成「PayPal↔储蓄账户」闭环。历史银行侧记录（之前归在 餐饮/其他/工资 的 PayPal 扣款/到账）经 `pair_transactions` 翻成 transfer（跨行划转），其具体消费分类移到 PayPal 付款行。
- **前端**：`pdf-import-panel.tsx` 加「CSV 导入（PayPal）」卡片（先选账户 → 选 .csv 上传 → 显示导入/去重计数）。

### 交易拆分（AA / 代付，2026-06-25 实装）

一笔交易拆成多条明细(各条之和=原额),用于「我集中付款、人均其实更少」的场景。`POST /transactions/{id}/split`(明细列表,校验和=原额)+ `POST /transactions/{id}/unsplit`(还原)。
- **模型(无父容器)**:原行**变成第一条明细**(保留 id/external_id,所以 CSV 重导仍能去重),其余建**兄弟行**,都打 `metadata.split_group_id=原id`。因为各条之和=原额,**余额/现金流天然正确,无需任何排除谓词**。原始 {金额/类型/分类/metadata} 存进 `metadata.split_original` 供 unsplit 还原。
- **转账腿方向**:拆分行若是 transfer(如「借出」),`transfer_direction` 继承原交易的资金流向(支出源→out),保证签名余额不变。
- **base_amount** 按比例缩放(`原base × 本条/原额`),保 FX 口径。外币也对。
- **典型用法**:€100 餐饮 → €20 餐饮 + €80 借出;别人转还的钱在交易列表改成「还款收回」(transfer)。前端 `split-transaction-form.tsx`(交易详情里「拆分」按钮,实时显示剩余额)。

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
  - `services/exchange_sync/`：`ExchangeProvider` Protocol + Binance / Bitget 实现 + `sign.py` 集中 HMAC 工具（method 强制大写避免 Bitget 40006）。Bitget **经典账户**调 4 个端点（spot + USDT-M + USDC-M + COIN-M），按 coin 跨端点 `sum(available + locked)`，**不计 unrealizedPL**。**统一账户（UTA）**：经典 v2 端点会返回 `code 40085`（"Unified Account mode... Classic Account API not supported"），provider 探测到 40085 即切到 v3。**统一账户分两个钱包，都要查并按币合并**：`/api/v3/account/assets`（交易账户）+ `/api/v3/account/funding-assets`（资金账户，易漏）。每币取 `balance`（== available+locked/frozen，仍排除 PnL）；资金端点失败不影响已取到的交易余额。两种账户都覆盖，互不影响。
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
- **接线**：项目根 `.mcp.json` 注册了该 server（`command` 指向 `mcp-server/run.sh`），Claude / 任何 stdio MCP 客户端启动时自动连接。
- **21 个 tool = 19 读 + 2 写**（2026-06-28 完整读取改造）：
  - **读路径（`finance_mcp/read_tools.py` + `_backend.py`）走 async**：每个读工具 `async with async_session_factory()` 开 session，**直接调后端 service / 序列化函数 / 共享 SQL 片段**（`compute_net_worth`、`_AMOUNT_BASE_EXPR`、`paired_dedup_predicate`、`_account_to_out`/`_tx_to_out`/`_holding_to_out` 等）。**读数与 REST/Web 构造上必然一致**——不再有手抄 SQL 漂移（旧版 V6/V7/V8 反复修的就是这个）。`_backend.py` 首次连接幂等确保 `v_account_balance` 视图存在（仅 CREATE-if-missing，不 DROP，避免影响在跑的后端）。
  - **写路径（`server.py`：`add_transaction` / `parse_bank_statement`）仍走同步 `sqlite3`**，内联镜像后端 ingestion 不变量（金额符号 / category-kind 校验 / FX 折算 / 转账配对 / cashflow 快照重算）。本期未迁移；与后端 async 通过 WAL 安全共享。
  - 完整工具清单见 `docs/API.md §17`；改造计划与未做项见 `docs/MCP_READ_PLAN.md`。
- **护栏**：`mcp-server/tests/test_mcp_read.py` 用同一临时库断言「MCP 读工具 == 后端 service」（6 测试），防止读路径未来漂移。
- **LLM 路径**：MCP `add_transaction` 不走 LLM（source=`mcp_agent`，与 `manual` 同样跳过自动分类）。如果想让 agent 也享受 LLM 分类，未来可在 `services/llm/classifier.py` 改条件。

## Deployment / Ops

完整部署与数据迁移流程见 `docs/DEPLOYMENT.md`(实测)。要点:
- **GitHub 只有代码**:`data/`(DB/PDF/备份)与 `.env`(密钥)在 `.gitignore`,迁移须手动搬。
- **三大迁移坑**:① SQLite 用在线 `backup()` 取一致快照(别直接拷 .db,有未 checkpoint 的 -wal);② `FINANCE_BANK_ENCRYPTION_KEY` 必须随数据一起搬(否则交易所/券商/TR 加密凭据全部无法解密;启动日志 `credential_health_ok` 验证);③ `pdf_imports.storage_path` 是绝对路径,换机要 `REPLACE` 改前缀。
- **AUTH 护栏**:`AUTH_DISABLED=true` 仅允许 loopback;绑 `0.0.0.0`(供局域网浏览器访问)必须 `AUTH_DISABLED=false` + token(浏览器 Settings 粘贴一次)。
- **前端 build-time 注入**:`NEXT_PUBLIC_API_URL` 在 `frontend/.env.local`(Next 不读项目根 `.env`),build 前必须设成服务器地址。
- **进程守护**:用 tmux(`deploy/start.sh`/`stop.sh`)——经一次性 SSH `nohup &` 启动长进程不可靠(绑 SSH channel 即被杀);tmux 不扛重启,开机自启用 systemd(需 sudo)。
- **当前线上**:cortana-box(192.168.178.65),后端 `:8000` / 前端 `:3100`(3000 被本机 World Monitor 占用) / MCP 经 `run.sh` 供同机 OpenClaw stdio 接入(跨机需改 HTTP/SSE,未实现)。

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
