# Finance Tracker Code Review V6

> 审查日期: 2026-05-20  
> 审查范围: V5 之后的最新代码、README/PROGRESS/docs、backend/frontend/MCP server、Alembic 迁移和本地测试状态。  
> 审查重点: 记账/资金计算逻辑、安全与隐私风险。本轮只新增此 review 文档，没有修改业务代码。

## 验证结果

- 后端完整测试: `289 passed, 15 skipped, 1 failed`。失败用例是 `tests/test_security_invariants.py::test_lifespan_allows_auth_disabled_on_loopback`，错误为默认本地 SQLite 写 `DROP VIEW IF EXISTS v_account_balance` 时 `attempt to write a readonly database`。同一用例指定临时库 `DATABASE_URL=sqlite:////private/tmp/finance-tracker-v6-lifespan.db` 后单测通过，说明更像本地默认数据库/环境隔离问题，而不是该 security invariant 本身失败。
- 前端构建: `npm run build` 通过。
- secret 扫描: tracked files 中没有发现真实 `AIza...`、`sk-...`、GitHub token、Slack token、Notion token 或 Bearer token。命中项主要是代码变量名 `secret_key`、`api_secret` 和测试 dummy 值。
- ignored 文件检查: `.env`、`frontend/.env.local`、`data/finance.db`、WAL/SHM、`data/pdfs/`、`data/backups/` 均处于 ignored 状态；`.gitignore` 已覆盖这些本地隐私/数据文件。

## V5 回归概览

- 已修复: MCP USDT/USDC/DAI 等 USD-pegged 稳定币 FX alias、资产 identity `(chain, contract)`、LLM key 加密存储、wallet sync 错误日志脱敏、前端 `by_currency` nested shape、持仓 `price_currency` 显示、`.env.example` 加密 key 提示、旧 market scheduler 过滤 on-chain/native。
- 部分修复: `include_in_total`/inactive holding 已进入 net worth、holdings summary 和 MCP total assets，但 `/accounts/balances` 仍会返回全部账户且 crypto/exchange 持仓价值单位存在问题；文档也仍有多处旧结构。
- 未修复或仍有风险: MCP PDF import 仍没有完整复用 ingestion；GoCardless institutions/create_connection 仍有 query string 凭据和 country bug；Notion asset summary 仍使用旧余额公式。

## P0

本轮没有发现新的 P0。当前最需要优先修的是下面的 P1，因为它们会导致资金口径不一致、资产估值错误或凭据泄露面扩大。

## P1 高优先级

### P1-1 自动配对成功的转账仍可能留在 pending，导致现金流与 inbox 状态错误

`backend/app/api/v1/statements.py:276-277` 导入 PDF 时默认 `is_pending=True`。`backend/app/services/ingestion/__init__.py:147-168` 只会把原本就是 `transfer` 或 L1 规则命中的行改成 `is_pending=False`，但后续 `auto_pair_after_import()` 会把普通 expense/income 自动提升为 transfer。

问题在于 `backend/app/services/transfer_matcher/engine.py:600-672` 的 `pair_transactions()` 和 `backend/app/services/transfer_matcher/engine.py:1101-1132` 的 `mark_subaccount_pair()` 只改 `type/category/metadata/counter_account_id`，没有清 `is_pending`。`auto_pair_after_import()` 在 `backend/app/services/transfer_matcher/engine.py:695-717` 调用它们后也没有统一清 pending。现金流重算又在 `backend/app/services/cashflow/engine.py:87-90` 排除了 pending 行。

影响: 自动识别出的转账会显示为 inbox 待处理，且不进入 `cash_flow_snapshots.transfer_total`。余额视图不按 pending 过滤，导致余额和现金流使用不同数据集。建议在 `pair_transactions()`、`mark_subaccount_pair()` 内同时设置两条腿 `is_pending=False`，并补一个 PDF 导入后自动配对的回归测试。

### P1-2 Solana/Tron token contract 被强制 lower-case，可能导致价格查询失败

V5 后资产按 `(asset_class, symbol, chain, contract)` 拆分是正确方向，但 `backend/app/services/wallet_sync/upsert.py:81-87` 对所有 on-chain token 都执行 `contract.strip().lower()`。这只适合 EVM hex address，不适合大小写敏感的 Solana mint 或 Tron contract/address。

Solana provider 在 `backend/app/services/crypto_sync/sol_rpc.py:62-67` 直接把 `info["mint"]` 作为 contract；Tron provider 在 `backend/app/services/crypto_sync/tron_grid.py:46-65` 直接使用 TronGrid 返回的 contract。价格刷新在 `backend/app/services/wallet_sync/orchestrator.py:294-327` 使用 `asset.data_source_id` 调 `fetch_token_prices()`，而 `backend/app/services/market_data/coingecko.py:221-247` 又用该 contract 作为 upstream 查询参数和结果 key。

影响: Solana SPL 或 TRC-20 token 可能因为 contract 大小写被改写而查不到 CoinGecko 价格，进而从 net worth/asset summary 中消失或估值为 0。建议按 chain 归一化: 只有 EVM 链 lower-case，Solana/Tron 保留原始 contract；同时补 Solana mint 和 Tron contract 的大小写回归测试。

### P1-3 `/accounts/balances` 把 USDT 持仓价值加到账户币种余额里

`backend/app/services/wallet_sync/holdings_value.py:1-10` 和 `:50-58` 明确返回的是 USDT 计价的持仓价值。`backend/app/api/v1/accounts.py:121-137` 把这个 USDT 值直接加到 `v_account_balance.balance`，但返回的 `currency` 仍是账户原始币种 `r[2]`。

前端创建 crypto/exchange 账户时会尽量 auto-snap 到 USDT，但 `frontend/src/components/account-form.tsx:94-108` 只在 `!currencyTouched` 时生效，`frontend/src/components/account-form.tsx:376-383` 也允许用户先选其他币种再切到 crypto/exchange。后端 schema `backend/app/schemas/__init__.py:68-84` 没有强制 crypto/exchange 必须是 USDT。

影响: 通过 API 或 UI 顺序创建出 `crypto_wallet`/`exchange` 且 `currency=EUR/CNY` 的账户后，`/accounts/balances` 会把 `1000 USDT` 显示为 `1000 EUR/CNY`。net worth 主口径目前独立计算，风险主要在账户余额列表、dashboard 账户 chips 和任何消费 `/accounts/balances` 的客户端。建议后端强制 crypto/exchange 账户币种为 USDT，或在 balances API 中返回明确的 `market_value_currency` 并做 FX 折算。

### P1-4 MCP cashflow 仍会把 subaccount transfer 计入 transfer_total

后端现金流已经用 `_NOT_SUBACCOUNT` 排除内部储蓄/同账户子账户移动，见 `backend/app/services/cashflow/engine.py:54-83`。MCP mirror 没有同步这个条件。

`mcp-server/src/finance_mcp/server.py:96-126` 的 `_recompute_snapshot_sql()` 中 `transfer_total` 是 `CASE WHEN type = 'transfer' THEN ABS(...)`，没有排除 `metadata_json.subaccount=true`。`mcp-server/src/finance_mcp/server.py:933-949` 的 `get_cashflow()` 月度 transfer 也同样没有 subaccount guard。

影响: 通过 MCP recompute 或 MCP cashflow 查询时，N26 主账户到 Saving Space 这类内部移动会膨胀 transfer volume；REST/backend snapshot 与 MCP 返回不一致。建议把后端 `_NOT_SUBACCOUNT` JSON guard 原样镜像到 MCP snapshot 和 `get_cashflow()`，并补 MCP 层 regression。

### P1-5 MCP PDF import 仍不是完整 ingestion mirror

`mcp-server/src/finance_mcp/server.py:695-703` 注释写着要 mirror REST ingestion invariants，但实际仍在 `mcp-server/src/finance_mcp/server.py:773-794` 直接 `INSERT INTO transactions`。写入列没有 `fx_rate_to_base`、`base_amount`、`categorization_method`、`categorization_confidence`、`llm_reason` 等 ingestion/audit 字段，也没有调用后端统一的 transfer matcher 和 L2 LLM 分类链路。

影响: 同一张 PDF 通过 REST 和 MCP 导入，外币折算、分类审计字段、自动转账匹配、LLM fallback 和现金流刷新口径仍可能不同。建议 MCP PDF import 不再手写交易管道，而是通过后端 API 或共享 ingestion service 执行；如果必须保留 SQLite 直写，需要逐项复刻 FX、matcher、LLM、audit 字段和 pending 语义。

### P1-6 L2 LLM 分类在事务提交前 fire-and-forget，存在竞态

`backend/app/services/ingestion/__init__.py:178-188` 在 `db.flush()` 后立刻 `_dispatch_llm_classification()`，但 FastAPI dependency `backend/app/db/session.py:50-55` 是 endpoint 返回后才 commit。PDF route `backend/app/api/v1/statements.py:282-301` 没有在 dispatch 前显式 commit。

背景任务在 `backend/app/services/ingestion/__init__.py:273-277` 开新 session 读取同一个 `tx_id`。如果任务先于主事务 commit 执行，新 session 可能看不到这条交易并直接 return；SQLite 下还可能遇到锁竞争。异常被 `:285-286` 吞掉，只留下 warning。

影响: L2 分类会随机不运行，用户看到需要 LLM 的 PDF/bank_api 交易长期留在 inbox。建议改成 commit 后派发，比如 route 显式 commit 后调用、FastAPI `BackgroundTasks`、outbox 表，或 worker 对 tx missing/locked 做短重试。

### P1-7 GoCardless 凭据和 country bug 仍未修复

`backend/app/api/v1/bank_sync.py:70-85` 的 `GET /institutions` 仍要求 `encrypted_credentials` query 参数。即使它是加密后的 credential blob，仍会进入浏览器历史、反向代理/access log、APM 和 crash 上报。`backend/app/api/v1/bank_sync.py:106-111` 在创建连接时仍把 `body.redirect_url` 当作 `country` 传给 institution lookup。`backend/app/api/v1/bank_sync.py:522-530` 仍直接读 `os.environ.get("FINANCE_BANK_ENCRYPTION_KEY")`，绕开统一 Settings。

影响: 安全上扩大凭据暴露面；功能上 create_connection 的 institution 解析会用 URL 当 country，连接流程可能失败或匹配错误。建议改成 POST body 或服务端 setup id，schema 中显式带 country，并统一通过 Settings 读取加密 key。

### P1-8 Notion asset summary 仍使用旧余额公式

`backend/app/services/notion_sync/engine.py:304-310` 拉取 holdings 时只过滤 `Account.is_active == True`，没有排除 `Account.include_in_total=False` 和 inactive holdings 之外的语义。`backend/app/services/notion_sync/engine.py:355-359` 账户余额仍用 `initial_balance + SUM(Transaction.amount)`。

影响: expense 由于系统内金额存正数，会被加到账户余额里；transfer、subaccount、base_amount/FX、deleted/pending/inactive 口径也没有和余额视图/net worth 对齐。启用 Notion asset sync 后，Notion 上会出现错误余额和错误资产摘要。建议改用 `v_account_balance` 或复用已有 balance/net worth service，并同步 `include_in_total` 和 active holding 过滤。

## P2 中优先级

### P2-1 LLM cost budget 计费是非原子 read-modify-write

`backend/app/services/llm/cost_tracker.py:36-44` 先读当前月累计，再加 delta，再写回 app_settings。ingestion 会对多条交易 `asyncio.create_task()` 并发执行 LLM 分类，因此多个任务可能读到同一个旧值并互相覆盖。

影响: 月度成本会被低估，budget guard 可能被绕过。建议用单条 SQL 原子累加、行级锁或专门的 usage ledger 表。

### P2-2 缺少加密 key 时 LLM key 保存/旧数据迁移会 500 或阻断启动

`backend/app/api/v1/llm.py:56-62` 直接调用 `set_gemini_api_key()`，如果没有配置 `FINANCE_BANK_ENCRYPTION_KEY`，底层会抛 `RuntimeError`，当前 endpoint 没有转成明确 4xx。启动迁移在 `backend/app/main.py:302-305` 调 `_migrate_legacy_gemini_key_to_encrypted()`，如果旧库里有 plaintext Gemini key 但环境缺加密 key，也可能阻断应用启动。

影响: 运维体验和错误语义不稳定。建议和 wallet sync key 保存保持一致，缺 key 返回 400/配置错误；启动迁移遇到缺 key 时记录安全 warning 并保留只读旧值，要求管理员配置 key 后再迁移。

### P2-3 asset identity v2 的历史数据拆分脚本没有纳入升级流程

Alembic migration `backend/alembic/versions/05f31889722c_asset_identity_chain_contract.py:18-20` 明确说明只改 schema，已有数据拆分在 `backend/scripts/migrate_crypto_asset_identity.py`。但 README quickstart `README.md:17-30` 只要求 `alembic upgrade head`，没有提示老用户运行数据迁移脚本。

影响: V5 之前已经同步过的钱包可能保留旧的合并资产行，直到重同步或手工跑脚本前，资产拆分和估值仍可能不完全正确。建议把数据迁移接入 Alembic 或启动 migration job，至少在 README/PROGRESS 中加入旧库升级步骤和幂等验证命令。

### P2-4 后端测试仍依赖默认本地数据库，完整套件可被本地状态打断

本轮完整 pytest 失败在 lifespan 进入时写默认 `data/finance.db` 的视图 DDL，报 `readonly database`；指定临时 SQLite 后同一测试通过。说明至少部分测试没有隔离数据库，受本地 `.env`、ignored DB 文件、文件权限或上一次运行状态影响。

影响: CI/本地 review 信号会被环境噪音污染，容易掩盖真实回归。建议安全/lifespan 类测试统一设置临时 `DATABASE_URL`，或在 test fixture 中强制清掉 `.env` 的数据库配置。

### P2-5 文档与最新实现仍有漂移

发现的文档漂移包括:

- `README.md` 和 `PROGRESS.md` 仍提到旧的测试数量/“242 passed”，当前后端已收集 305 个测试。
- `docs/ARCHITECTURE.md` 仍有旧 cashflow `COALESCE(base_amount, amount * fx_rate_to_base, amount)` 描述，且 Alembic head 仍写旧 revision。
- `docs/API.md` 的 portfolio/net worth `by_currency` 示例仍是旧的 string map，和后端 nested shape 不一致。
- `docs/SCHEMA.sql` 仍是旧 assets unique `(symbol, asset_class)`，缺 `chain/contract/is_active`。
- `docx/MCP_TEST_REPORT.md` 仍是旧的 PARTIAL/FAIL 状态，和 README 的 “MCP 6 轮全 PASS” 不一致。

影响: 后续开发和 AI/MCP 使用者容易按旧口径实现，尤其是 cashflow FX fallback 和 asset identity。建议在修完 P1 后统一重生成 schema/API 文档，并把 MCP 测试报告状态更新为可追溯的最新结论。

## 安全结论

本轮没有发现真实 token/API key 被提交到 git。当前最明显的安全问题仍是 GoCardless encrypted credentials 放在 query string。其次是 LLM/Gemini key 加密链路在缺少加密 key 时的错误处理不一致，可能造成配置阶段的 500 或启动失败。建议优先修 P1-7，再补 P2-2 的错误语义。

## 建议修复顺序

1. 先修资金口径错误: P1-1、P1-2、P1-3、P1-4、P1-5。
2. 再修凭据和同步路径: P1-7、P1-8。
3. 然后补稳定性和文档: P1-6、P2-1 到 P2-5。

