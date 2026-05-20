# Finance Tracker Code Review V5

> 审查日期：2026-05-19  
> 审查范围：V4 之后新增/修改的全项目代码、README/PROGRESS/方案文档、后端/前端/MCP/迁移/配置。  
> 重点：资金计算与记账逻辑、分类逻辑、安全与隐私风险。

## 验证结果

- 后端测试：`../.venv/bin/python -m pytest`，结果 `251 passed, 15 skipped`。
- 前端构建：`npm run build`，通过。
- tracked 文件密钥扫描：未发现 `.env`、SQLite 数据库、真实 API key/token 被 git 跟踪；`.env` 与 `data/finance.db*` 当前均被 `.gitignore` 忽略。

## V4 回归概览

| V4 项目 | 当前状态 | 说明 |
|---|---|---|
| REST `/cashflow/by-category` raw fallback | 已修复 | 后端 REST 已复用 `_AMOUNT_BASE_EXPR`，缺 FX 外币不再按 raw amount 混入。 |
| MCP cashflow/snapshot raw fallback | 部分修复 | MCP 月度主 totals 已改 CASE，但 by-category 仍用 raw fallback；转账 total 也未排除 subaccount。 |
| MCP PDF import ingestion mirror | 部分修复 | 已加部分 parser metadata、分类和同账户 amount-match；仍没有 FX 折算、完整 transfer matcher、LLM/规则污染语义。 |
| 手动转账绑定 invariants | 已修复 | 已检查 same-account、same-currency、amount tolerance、synthetic mirror 等约束。 |
| GoCardless credentials/country | 未修复 | `encrypted_credentials` 仍在 query string；`redirect_url` 仍被当 country 用。 |
| Notion asset summary 旧余额公式 | 未修复 | 仍用 `initial_balance + SUM(Transaction.amount)`。 |
| frontend by_currency shape | 未修复 | backend 已改 nested shape，资产分布 currency tab 仍按 `value` 读取。 |
| category view raw FX fallback | 已修复 | 前端分类视图已按 base/currency/fx CASE 跳过缺 FX 外币。 |
| bank encryption key 绕过 Settings | 部分修复 | crypto helper 已读 Settings；bank sync status 仍用 `os.environ` 判断。 |

## P0

### V5-P0-1. MCP 总资产默认会遗漏 USDT 计价加密资产

位置：
- `mcp-server/src/finance_mcp/server.py:148-193`
- `mcp-server/src/finance_mcp/server.py:260-267`
- `mcp-server/src/finance_mcp/server.py:1023-1029`

`wallet_sync` 写入的 crypto/CEX 行情统一是 `market_prices.currency='USDT'`，后端 REST 的 `_convert_to_base()` 已把 USDT/USDC/DAI 等稳定币折成 USD 再做 FX。但 MCP 的 `_convert_fx()` 没有 stablecoin alias。默认 base currency 是 CNY/EUR 时，`USDT -> CNY/EUR` 没有 FX rate，`get_total_assets()` 和 `get_asset_allocation()` 会 `continue` 跳过这些 holdings。

结果是：用户刚新增的加密钱包/CEX 资产，在 Web 后端 net worth 里可计入，但 MCP agent 查询总资产时可能把 crypto portfolio 计为 0。这是资金总额级别的错误。

建议：
- MCP `_convert_fx()` 复用后端 `_convert_to_base()` 的 stablecoin alias 逻辑，至少支持 `USDT/USDC/DAI/BUSD/TUSD/FRAX -> USD`。
- 给 MCP `get_total_assets` / `get_asset_allocation` 加一组测试：1 BTC/ETH 行情币种 USDT，base=CNY/EUR 时必须计入。
- 同时统一 MCP 与 REST 的 include/exclude 口径，见 V5-P1-2。

## P1

### V5-P1-1. on-chain token 只按 symbol 建 Asset，会导致跨链/同名 token 价格串用

位置：
- `backend/app/services/wallet_sync/upsert.py:53-87`
- `backend/app/services/wallet_sync/orchestrator.py:295-324`

`_get_or_create_asset()` 对有 symbol 的 crypto token 只按 `(symbol, asset_class='crypto')` 找 Asset。第一个 USDT/ABC/ETH-like symbol 会决定该 Asset 的 `data_source_id`，后续不同链、不同合约但同 symbol 的 token 都共享同一个 Asset。价格刷新时又按 `asset.id` 去重，只写第一条价格。

这对常见 USDC/USDT/ETH 可能暂时接近正确，但对同名假币、桥接资产、CEX 小币或不同链价格不一致的 token，会把 A 合约价格套到 B 合约持仓上，直接污染净值、资产分类和 dashboard。

建议：
- crypto Asset identity 至少包含 `chain + contract`；native/CEX 可用 `source + symbol`。
- `MarketPrice` 也应能区分 contract-level price，不要只以 `asset_id` 承载一个全局价格。
- 保留 symbol 作为展示字段，而不是唯一金融身份。

### V5-P1-2. include_in_total / inactive holdings 在多个资产汇总路径未生效

位置：
- `backend/app/api/v1/holdings.py:240-244`
- `backend/app/api/v1/holdings.py:317-318`
- `backend/app/api/v1/accounts.py:121-129`
- `frontend/src/app/dashboard/page.tsx:58-77`
- `mcp-server/src/finance_mcp/server.py:224-247`
- `mcp-server/src/finance_mcp/server.py:1064-1069`

`/holdings/portfolio/net-worth` 已过滤 `Account.include_in_total` 和 `AssetHolding.is_active`，但 `/portfolio/summary`、`/portfolio/breakdown`、`/accounts/balances`、Dashboard hero、MCP total/allocation 仍从所有账户/持仓聚合。用户把 business/shared/experimental account 关掉 `include_in_total=false` 后，某些页面和 MCP 仍会把它算进总资产。

这会造成“净值卡正确，但 dashboard 总资产、资产分布、MCP agent 回答错误”的多口径资产总额。

建议：
- 明确两个 API 层级：`total/net-worth` 默认只算 `include_in_total=true`；per-account list 可返回所有账户但不用于 grand total。
- `/portfolio/summary`、`/portfolio/breakdown`、MCP total/allocation 应 join accounts 并过滤 `include_in_total=1 AND deleted_at IS NULL`，holdings 过滤 `is_active=1`。
- Dashboard 总资产应直接用 `/holdings/portfolio/net-worth`，不要从 `/accounts/balances` 自己重算 grand total。

### V5-P1-3. MCP cashflow by-category 仍把缺 FX 外币按 raw amount 汇总

位置：
- `mcp-server/src/finance_mcp/server.py:936-949`

MCP `get_cashflow()` 的月份 income/expense/savings 主 totals 已改为 CASE，但同一个返回值里的 `by_category` 仍使用：

```sql
SUM(ABS(COALESCE(t.base_amount, t.amount * t.fx_rate_to_base, t.amount)))
```

因此一笔没有 FX 的 `100 GBP` 分类支出，在 MCP by_category 中仍会变成 `100 base currency`。这会让 agent 给出的分类支出分析与 REST `/cashflow/monthly`、`/cashflow/by-category` 不一致。

建议：
- MCP by-category 改用同一个 `_AMOUNT_BASE_EXPR_SYNC`。
- 加测试覆盖：缺 FX 外币在 category breakdown 中应被跳过或明确列入 `fx_missing`，不能 fallback raw amount。

### V5-P1-4. MCP PDF import 仍没有完整复用 ingestion 的资金语义

位置：
- `mcp-server/src/finance_mcp/server.py:733-776`

MCP `parse_bank_statement` 仍直接 `INSERT INTO transactions`，插入列没有 `fx_rate_to_base/base_amount/categorization_method/categorization_confidence/llm_reason`，也没有跑后端 `ingest_transactions()` 的完整流程。它手写了一份分类和同账户 amount-match，但没有完整 cross-account transfer matcher、FX fold、requires_llm 污染规则、LLM fallback、现金流 affected periods 收集。

结果：同一份 PDF 经 REST 上传和 MCP 导入，会得到不同的交易语义。外币账单尤其明显：REST 会尽力补 `base_amount`，MCP 会留下缺 FX 交易，现金流和分类可能不一致。

建议：
- MCP 写入不要维护第二套 ingestion；改成调用后端 service 层的 async `ingest_transactions()`。
- 如果 MCP 必须 sync sqlite3，至少把 FX fold、requires_llm、transfer matcher、subaccount exclusion 与 REST 共享成可复用模块。

### V5-P1-5. LLM Gemini API key 明文存入 SQLite

位置：
- `backend/app/api/v1/llm.py:1-5`
- `backend/app/api/v1/llm.py:53-61`
- `frontend/src/components/llm-settings-form.tsx:190-191`

`llm.py` 文件头还写着 API key “never accepted via this API”，但 PUT `/llm/settings` 实际接受 `gemini_api_key` 并通过 `app_settings_svc.set_setting()` 明文存入 `app_settings`。前端文案也明确说 key 存于 SQLite 明文。

虽然 `.env` 和 `data/` 当前被 gitignore，但本项目已经对银行/CEX 凭据使用 AES-GCM；LLM key 作为外部付费 API 凭据，不应低一个安全级别。SQLite 备份、Notion/日志、support bundle 或误提交都可能泄露它。

建议：
- Gemini key 和 exchange/bank credentials 一样用 `FINANCE_BANK_ENCRYPTION_KEY` 加密。
- 或者恢复最初设计：只允许 env 配置，不允许 API 写 key。
- 如果保留 UI 写 key，更新文档注释并加入 backup/export 红线说明。

### V5-P1-6. wallet/CEX sync 失败日志会记录未脱敏异常 repr

位置：
- `backend/app/services/wallet_sync/orchestrator.py:183-191`
- `backend/app/services/wallet_sync/orchestrator.py:235-240`

`_safe_error_text()` 已经为 DB/UI 做了脱敏，但异常日志仍写 `error_repr=repr(exc)`。对 Alchemy HTTP 错误，repr/异常链可能包含 `/v2/<api_key>` URL；对 Binance/Bitget 签名请求，可能包含带 `signature=` 的 URL 或上游错误细节。

本地开发时风险低，但一旦 stdout 被采集到日志系统，这会把 provider key 或签名请求写进日志。

建议：
- 日志也使用 `_safe_error_text()` 或专门的 `safe_repr`。
- 如果确实需要 full repr，只允许 DEBUG 本地模式，并显式 redaction URL path/query。

### V5-P1-7. GoCardless 凭据 query string 与 country bug 仍存在

位置：
- `backend/app/api/v1/bank_sync.py:70-85`
- `backend/app/api/v1/bank_sync.py:106-111`

`/bank-sync/institutions` 仍要求 `encrypted_credentials` 作为 GET query 参数。query string 会进入浏览器历史、access log、proxy log、监控系统。`create_connection()` 中仍调用 `list_institutions(... country=body.redirect_url)`，把 callback URL 当成 ISO country。

这同时是安全风险和功能 bug。即使 bank sync 目前不是主路径，router 已经挂载在 `/api/v1/bank-sync`，不能只按“未启用”处理。

建议：
- institutions 改 POST body，或改成服务端 setup/session id。
- `BankConnectionCreate` 增加 `country` 字段；不要从 `redirect_url` 推断。
- `/bank-sync/status` 的 key 检测也应走 Settings，而不是 `os.environ`。

### V5-P1-8. Notion asset summary 仍使用错误余额公式

位置：
- `backend/app/services/notion_sync/engine.py:304-310`
- `backend/app/services/notion_sync/engine.py:353-360`

Notion asset summary 仍聚合所有 `Account.is_active=True` holdings，没有过滤 `include_in_total` / `AssetHolding.is_active`，账户余额仍用 `Account.initial_balance + SUM(Transaction.amount)`。由于交易 amount 统一存正数，支出会被加到账户余额里；transfer/subaccount/FX 也全部绕过了 `v_account_balance` 的符号和 metadata 语义。

启用 Notion asset sync 后，Notion 会同步错误余额和错误资产数量。

建议：
- 余额直接读 `v_account_balance`，并按 include_in_total 口径决定是否进入“总资产”。
- holdings 过滤 `AssetHolding.is_active == True`；crypto/CEX 估值复用 `/portfolio/net-worth` 或共享 valuation service。

### V5-P1-9. 新迁移没有进入快速启动路径，旧库升级容易缺列

位置：
- `README.md:17-19`
- `backend/app/main.py:202-207`
- `backend/alembic/versions/e53bc1301436_add_llm_classification.py`
- `backend/alembic/versions/3317bd446ae0_add_crypto_wallet_and_exchange.py`
- `backend/alembic/versions/7bc98bcff7fe_add_include_in_total.py`

项目已经有 Alembic revisions，但 README 快速启动只安装依赖并直接 `uvicorn`，没有 `alembic upgrade head`。FastAPI lifespan 的轻量 `_column_migrations` 只补 `transactions.user_note` 和 `accounts.iban`，不补 `accounts.include_in_total`、`transactions.llm_*`、`categorization_notes`、`chain_addresses`、`exchange_connections`、`asset_holdings.chain/is_active` 等 V4 后新 schema。

全新数据库靠 `Base.metadata.create_all()` 能启动；已有用户数据库如果没手动跑 Alembic，会在新代码路径访问缺失列/表时失败。

建议：
- 快速启动和部署文档加入 `cd backend && ../.venv/bin/alembic upgrade head`。
- 启动时检测当前 DB revision，缺 revision 时给出明确错误，而不是运行到某个 endpoint 才 500。
- `docs/ARCHITECTURE.md` 里“暂未启用 Alembic”的描述也应更新。

## P2

### V5-P2-1. 前端资产分布「按币种」仍按旧 shape 读取

位置：
- `frontend/src/lib/api.ts:90-100`
- `backend/app/api/v1/holdings.py:359-379`
- `frontend/src/app/assets/page.tsx:924-965`

后端 `PortfolioBreakdown.by_currency` 返回 `{ original_value, base_value, count }`，但前端类型和 `DistributionPanel` 仍认为是 `{ value, count }`。切到「按币种」后 `val.value` 为 undefined，`totalRaw` 会按 0 计算，图表和总计会失真。

建议：前端把 currency mode 的 value 改为 `base_value`，并显示 original/base 两个字段；TypeScript interface 与 backend schema 同步。

### V5-P2-2. holdings 列表缺 price_currency，crypto 市值/价格展示单位不可靠

位置：
- `backend/app/api/v1/holdings.py:34-45`
- `frontend/src/lib/api.ts:108-116`
- `frontend/src/app/assets/page.tsx:587-603`

`_holding_to_out()` 只有当 `price_currency == h.cost_currency` 才计算 `market_value`。wallet/CEX sync 写入的 holdings 通常没有 `cost_currency`，所以持仓表里 crypto 市值为空；前端又用 `h.cost_currency || "EUR"` 去格式化 `current_price` 和 `market_value`，会把 USDT 价格显示成 EUR。

建议：
- `HoldingOut` 增加 `price_currency` 和 `market_value_currency`。
- market value 应按 latest price currency 计算，不应依赖 cost currency；PnL 才需要 cost currency 一致或做 FX 转换。

### V5-P2-3. `.env.example` 缺少 CEX/银行凭据加密 key 模板

位置：
- `.env.example:70-78`
- `backend/app/services/bank_sync/crypto.py`
- `backend/app/api/v1/wallet_sync.py:193-195`

Exchange connection 保存 API key 时强依赖 `FINANCE_BANK_ENCRYPTION_KEY`，但 `.env.example` 只列出 `ALCHEMY_API_KEY`，没有告诉用户必须生成 64 hex key。结果用户按 README 复制 `.env.example` 后，CEX 凭据保存会 500。

建议：在 `.env.example` crypto/CEX 段加入：

```env
FINANCE_BANK_ENCRYPTION_KEY=
```

并附生成命令；如果 key 缺失，wallet_sync PUT 应返回明确 400/配置错误，而不是 500。

### V5-P2-4. scheduler 的旧 crypto price refresh 与新 on-chain 资产模型不匹配

位置：
- `backend/app/services/market_data/engine.py:21-43`
- `backend/app/services/market_data/engine.py:97-124`

旧 scheduler 对所有 `Asset.asset_class == "crypto" AND data_source IS NOT NULL` 调 `/simple/price?ids=<asset.data_source_id>`。新 on-chain token 的 `data_source='onchain'`、`data_source_id=<contract>`，应该走 token_price endpoint，而不是 simple price ids。现在同步后的 on-chain assets 会被旧 scheduler 反复用错误 endpoint 刷价，通常拿不到价格。

建议：旧 scheduler 区分 `native/coingecko/onchain`；onchain 用 `fetch_token_prices(chain, contract)`，或只让 wallet_sync orchestrator 管 crypto 价格刷新。

### V5-P2-5. API/docs/schema 文档仍明显落后于当前实现

位置：
- `docs/ARCHITECTURE.md`
- `docs/API.md`
- `docs/SCHEMA.sql`
- `docx/REQUIREMENT_GAP.md`

README/PROGRESS 已描述 LLM、crypto wallet、CEX、include_in_total、Alembic，但 API/ARCHITECTURE/SCHEMA 仍停留在 2026-05-06 口径，缺少新 endpoint、新表/列、新 by_currency shape，并仍保留一些旧现金流/迁移描述。

建议：V5 修复后同步这些文档，尤其是 schema、API response shape、Alembic upgrade、secret 存储策略。

## 安全扫描结论

- 没有发现真实 secret/token 被 git 跟踪。
- `.env`、`data/finance.db*`、PDF 原稿目录均被忽略。
- 仍存在安全设计问题：LLM key 明文落库、wallet sync 日志可能泄露 provider key/签名 URL、GoCardless encrypted credentials 走 query string。

## 建议优先级

1. 先修 V5-P0-1、V5-P1-2、V5-P1-3、V5-P1-4：这些会直接造成不同入口资金总额/现金流不一致。
2. 同步修 V5-P1-5、V5-P1-6、V5-P1-7：这是凭据和隐私风险。
3. 再修 V5-P1-1：crypto token identity 是新功能的底层模型问题，越晚修迁移成本越高。
4. P2 项可以并行修，尤其 `.env.example` 和前端 by_currency shape 成本低、收益高。
