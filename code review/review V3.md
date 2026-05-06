# Finance Tracker Code Review V3（中文）

Review 日期：2026-05-06  
Review 范围：基于 V2 后的 Sprint 3 修改，先复核 V2 标记为“部分修复”的问题是否已经闭合，再扩展检查整个项目的资金计算逻辑、安全暴露、资产/净值汇总、MCP 写入路径和关键文档/配置链路。  
本次只做 code review，没有修改业务代码。

## 1. 总结

这轮修复比 V2 前进明显：测试环境恢复、前端构建通过、规则分类主路径加了 kind guard，REST/manual/bank sync create 路径的多币种 `base_amount` 也开始落地。

但“V2 中的部分修改已经全部完成”这个结论还不能成立。当前仍有几类问题：

| 状态 | 数量 | 说明 |
|---|---:|---|
| 已修复 | 5 | 自动分类主路径、backend rules regex、前端 env/token bootstrap、DB cwd、PATCH/inbox amount 正负号 |
| 部分修复 | 3 | 多币种现金流、MCP PDF import、前端 token 存储模型 |
| 未修复 | 5 | Notion 资产余额旧公式、GoCardless query credential、GoCardless country bug、cashflow recompute 跨年过滤、同描述级联 NULL bug |
| 扩展新增 | 7 | 资产/净值币种维度、`metadata_json` 可用性、bank encryption key 配置链路、持仓市值展示、MCP 资产汇总、账户删除一致性、错误详情泄露 |

最需要优先处理的新增/未闭合问题：
- 外币缺汇率时仍会被 cashflow 当 raw amount 汇总。
- 外币交易 PATCH amount/currency 后可能留下 stale `base_amount`。
- MCP 写入路径虽然补了部分 ingestion mirror，但仍没有完整 FX/base_amount、转账匹配和安全 regex 闭环。
- GoCardless 和 Notion 的 V2 未修项仍在原位置。
- 资产/净值接口存在“金额已折算但仍用原币种 key 标记”的问题，会误导资产配置和币种占比。
- `metadata_json` 未校验 JSON，有效鉴权用户的一次错误/恶意写入就可能让余额视图查询失败。

## 2. V2 问题逐项复核

| V2 编号 | V3 状态 | 复核结论 |
|---|---|---|
| V2-P0-1 多币种现金流仍没有真正折算闭环 | 🟡 部分修复 | `ingest_transactions()` 新增 `resolve_fx_to_base()`，可为 REST/manual/bank create 路径填 `fx_rate_to_base/base_amount`；但缺汇率时 cashflow 仍 fallback raw amount，MCP 写入仍不填 FX，PATCH amount 后也不会重算 stale base_amount |
| V2-P0-2 自动分类规则绕过 kind invariant | ✅ 已修复主路径 | `categorize_transaction()` 和 `/rules/apply-all` 已加载 category 并校验 `Category.kind == tx.type`；规则 create/update 也校验 category 存在 |
| V2-P0-3 MCP PDF import 绕过 ingestion | 🟡 部分修复 | MCP PDF import 已补 metadata、amount normalize、自动分类、kind guard、cashflow recompute；但仍没有 transfer matcher、FX/base_amount、PDF size/magic guard，也没有对应测试 |
| V2-P1-1 Notion 资产摘要旧余额公式 | ❌ 未修复 | 仍使用 `Account.initial_balance + SUM(Transaction.amount)` |
| V2-P1-2 GoCardless query credential + country bug | ❌ 未修复 | `/bank-sync/institutions` 仍接收 query `encrypted_credentials`；`create_connection()` 仍 `country=body.redirect_url` |
| V2-P1-3 cashflow recompute 跨年范围错误 | ❌ 未修复 | 仍按 year/month 分别比较 |
| V2-P1-4 backend rules regex 直接 `re.search` | ✅ backend 已修复 | `backend/app/api/v1/rules.py` 已改用 `_safe_regex_search()`；但 MCP PDF import 内部仍有直接 `re.search` |
| V2-P2-1 分类级联对 `category_id IS NULL` 不生效 | ❌ 未修复 | `apply_to_similar_pending()` 仍使用 `Transaction.category_id != category_id` |
| V2-P2-2 前端 env 名与文档/docker-compose 不一致 | ✅ 已修复 | 已统一为 `NEXT_PUBLIC_API_URL`，且不含 `/api/v1` 后缀 |
| V2-P2-3 SQLite 相对路径 cwd 敏感 | ✅ 已修复 | 新增 `Settings.resolved_database_url`，`backend/` 目录运行测试也已通过 |
| V2-P2-4 token 被 `NEXT_PUBLIC_API_TOKEN` 注入 bundle | ✅ 主要风险已修复 | 已删除自动注入，Settings 页改为手动粘贴 token；仍使用 localStorage，但文案已明确本地风险 |

## 3. V3 新发现 / 仍需修复的问题

### V3-P0-1. 外币缺汇率时仍会被 cashflow 当原币金额混入 base currency

证据：
- `backend/app/services/ingestion/__init__.py:127-133` 在找不到汇率时只写 `metadata.fx_missing=true`。
- `backend/app/services/cashflow/engine.py:40` 仍是 `COALESCE(base_amount, amount * fx_rate_to_base, amount)`。
- `backend/app/api/v1/cashflow.py:47-52`、`:204-210` 也同样 fallback 到 raw amount。

影响：
- 如果 base=CNY，导入 `100 GBP` 且没有汇率路径，现金流会按 `100 CNY` 计入，而不是排除/告警。
- 这会继续产生“看起来正常但实际错误”的 savings 和 category totals。

建议：
- 对 `currency != base_currency` 且 `base_amount/fx_rate_to_base` 缺失的交易，不要 fallback raw amount。
- cashflow API 应返回 `fx_missing_count` / `fx_missing_transactions` 或至少 warning。
- snapshot recompute 也应跳过这些 rows 或把月份标为 incomplete。

### V3-P0-2. PATCH 外币交易金额后不会重算 `base_amount`

证据：
- `backend/app/api/v1/transactions.py:343-348` 会解析 `amount/fx_rate_to_base/base_amount`。
- `backend/app/api/v1/transactions.py:359-364` 只把 negative amount 改成 ABS，然后直接 `setattr`。
- 如果旧交易已有 `base_amount`，PATCH 新 amount 不会同步更新 `base_amount`。

影响：
- 例如 EUR 交易原本 `amount=50, fx_rate_to_base=8, base_amount=400`，用户 PATCH amount 为 `100` 后，cashflow 仍会优先读取旧 `base_amount=400`，实际应为 `800`。
- 这是直接资金计算错误，且发生在正常编辑交易路径。

建议：
- 当 `amount/currency/fx_rate_to_base` 任一字段变化时，统一重算或清空 `base_amount`。
- 最好把 FX/base_amount normalization 抽成可复用 helper，让 create、patch、inbox confirm、MCP 都走同一逻辑。
- 增加测试：外币交易 PATCH amount 后 cashflow snapshot 更新为新 base amount。

### V3-P1-1. MCP 写入路径仍不是完整 ingestion mirror

证据：
- `mcp-server/src/finance_mcp/server.py:444-454` 的 `add_transaction()` 直接 INSERT，不填 `fx_rate_to_base/base_amount`。
- `mcp-server/src/finance_mcp/server.py:635-678` 直接 INSERT transaction。
- 该路径没有写 `fx_rate_to_base/base_amount`，没有调用或镜像 `_convert_fx()`。
- 没有运行 transfer matcher，因此跨账户 PDF 双边转账、subaccount amount-match 等不会被配对。
- `mcp-server/src/finance_mcp/server.py:627-631` 对 regex 规则仍直接 `re.search()`。

影响：
- MCP 手动新增外币交易时，即使 FX 表有汇率，也不会落 `base_amount`。
- MCP 导入外币 PDF 时，现金流仍可能混币。
- MCP 导入包含转账的 PDF 时，REST upload 的自动转账识别不会生效。
- legacy/异常 regex 规则仍可能卡 MCP import。

建议：
- MCP 最稳的方式是调用 backend REST upload，或直接复用 async backend ingestion pipeline。
- 如果坚持 sync SQLite，需要至少补 FX folding、transfer matcher mirror、safe regex、PDF size/magic guard。
- 增加 MCP import 回归测试，不要只靠人工声明“全部接入”。

### V3-P1-2. GoCardless 凭据和连接 country bug 仍未修

证据：
- `backend/app/api/v1/bank_sync.py:70-85` 仍将 `encrypted_credentials` 放在 query string。
- `backend/app/api/v1/bank_sync.py:106-110` 仍将 `redirect_url` 传给 `country`。

影响：
- query string 凭据会进入浏览器历史、代理日志或服务访问日志。
- `redirect_url` 不是 ISO country code，GoCardless institution lookup 会失败或请求错误国家。

建议：
- `/institutions` 改为 POST body，或服务端保存 setup credentials 后只给前端 setup id。
- `BankConnectionCreate` 增加 `country` 字段，或删除 create connection 里未使用的 institution lookup。

### V3-P1-3. Notion 资产摘要仍会同步错误账户余额

证据：
- `backend/app/services/notion_sync/engine.py:355-359` 仍使用 `Account.initial_balance + SUM(Transaction.amount)`。

影响：
- 支出会被加到账户余额里；transfer/subaccount metadata 全部被忽略。
- 只要启用 Notion asset sync，外部 Notion 页面中的账户余额就是错的。

建议：
- 直接查询 `v_account_balance`，或抽账户余额 service 给 Notion 和 `/accounts/balances` 共用。

### V3-P1-4. 手动 cashflow recompute 跨年范围仍错误

证据：
- `backend/app/api/v1/cashflow.py:215-218` 仍分别比较 `from_year/from_month/to_year/to_month`。

影响：
- `2025-12` 到 `2026-02` 这种范围会错误过滤月份。

建议：
- 改为比较 `substr(occurred_at, 1, 7)` 的 period 字符串，或构造真实 date boundary。
- 增加跨年 recompute 测试。

### V3-P1-5. 资产/净值的币种 breakdown 把 base amount 标在原币种 key 下

证据：
- `backend/app/api/v1/holdings.py:271-278` 在 `portfolio_summary()` 中先把持仓价值折算成 `base_currency`，随后仍写入 `by_currency[latest.currency]`。
- `backend/app/api/v1/holdings.py:317-338` 的 `portfolio_breakdown()` 也同样先折算，再按 `latest.currency` 汇总。
- `backend/app/api/v1/holdings.py:469-476` 的 `net_worth()` 把 `converted` 加到 `investment_by_currency[latest.currency]`。
- MCP 侧同样存在：`mcp-server/src/finance_mcp/server.py:231-241`、`:865-890`。

影响：
- 例如一笔 EUR 资产市值为 `100 EUR`，折算后为 `780 CNY`；接口会返回 `by_currency["EUR"]="780"`，前端/agent 很容易理解成 `780 EUR`。
- 资产配置、币种占比和净值解释会被误导，尤其是用户按币种做风险暴露分析时。

建议：
- 如果字段语义是“按原始报价币种分组”，每个 entry 应同时返回 `original_value` 和 `base_value`。
- 如果字段语义是“base currency breakdown”，key 应改成 `base_currency` 或返回单独的 `by_quote_currency_base_value`，避免把单位和标签混在一起。
- 后端、MCP 和前端类型应统一字段命名，并补一个 EUR/USD 持仓的回归测试。

### V3-P1-6. `metadata_json` 未校验会让余额视图进入 malformed JSON 错误

证据：
- `backend/app/schemas/__init__.py:156-178` 允许 `TransactionCreate/TransactionUpdate.metadata_json` 作为任意字符串写入。
- `backend/app/main.py:93-100` 的 `v_account_balance` 直接对 `t.metadata_json` 调用 `json_extract()`，没有 `json_valid()` guard。
- 本地 SQLite 验证：`json_extract('not json', '$.subaccount')` 会直接报 `malformed JSON`。

影响：
- 一个无效 `metadata_json` 就可以让 `/accounts/balances`、`/{account_id}/balance`、`adjust-balance`、net worth 和 MCP 资产查询等依赖 `v_account_balance` 的路径报错。
- 这是安全可用性问题：虽然接口需要 bearer token，但对个人财务系统来说，已认证用户误填或恶意写入都不应破坏全局余额查询。

建议：
- API schema 用 Pydantic validator 校验 `metadata_json` 必须是 JSON object，或把字段改成结构化 `dict` 再统一序列化。
- SQL 侧防御式改成 `CASE WHEN json_valid(t.metadata_json) THEN json_extract(...) END`。
- 增加测试：创建 invalid metadata transaction 后余额接口仍能返回，或创建时直接 422。

### V3-P1-7. 银行凭据加密 key 绕过 Settings，`.env` 配置链路不闭合

证据：
- `backend/app/core/config.py:71-75` 已声明 `finance_bank_encryption_key`，按项目配置体系应由 `Settings` 读取。
- `backend/app/services/bank_sync/crypto.py:18-26` 却直接读取 `os.environ["FINANCE_BANK_ENCRYPTION_KEY"]`。
- `.env.example` 当前没有给出 `FINANCE_BANK_ENCRYPTION_KEY` 模板；文档只在 `docs/BANK_API_DESIGN.md`/`CLAUDE.md` 中提到。

影响：
- 本地非 docker 启动时，Pydantic 可以从 `.env` 读到 settings，但 `os.environ` 不一定有该变量，导致 bank sync setup 在加密时失败。
- 安全配置绕过统一 `Settings`，也让 key 长度校验、缺失提示、测试覆盖和未来轮换策略分散。

建议：
- `crypto._get_key()` 改为读取 `get_settings().finance_bank_encryption_key`，并保留 32-byte hex 校验。
- `.env.example`、README/API docs 加上生成方式和“不要提交真实 key”的说明。
- 增加测试：只通过 `.env`/Settings 注入 key 时，`encrypt_credentials/decrypt_credentials` 可正常往返。

### V3-P1-8. MCP `get_total_assets()` 在现金缺 FX 时仍把原币金额混进总资产

证据：
- `mcp-server/src/finance_mcp/server.py:249-250` 中，现金余额 `_convert_fx()` 失败时使用 `converted if converted is not None else amt`。
- 后端 `net_worth()` 对不能折算的 cash currency 会记录 `converted=""` 并跳过总额；MCP 这里行为不一致。

影响：
- 如果 base=CNY，一个 `1000 GBP` 账户缺 FX，MCP 会把它当 `1000 CNY` 加入 `total_assets`。
- Agent 侧查询“总资产”会得到混币总额，且结果看起来没有任何 warning。

建议：
- MCP 与后端 `net_worth()` 语义对齐：缺 FX 时不要计入 base total，并返回 warning/missing_fx 列表。
- 增加 MCP asset allocation/total assets 的缺 FX 测试。

### V3-P2-1. 同描述级联仍跳过 `category_id IS NULL` 的交易

证据：
- `backend/app/services/categorizer/engine.py:232` 仍是 `Transaction.category_id != category_id`。

影响：
- SQL 中 `NULL != x` 不为 true，所以未分类 pending 交易不会被同描述级联覆盖。
- 用户以为“改 1 笔，同描述兄弟全改”，但原本没有分类的兄弟不会动。

建议：
- 改成 `or_(Transaction.category_id.is_(None), Transaction.category_id != category_id)`。
- 增加测试覆盖 `category_id=None` 的 pending sibling。

### V3-P2-2. 持仓列表/详情在报价币种与成本币种不同时不返回市值

证据：
- `backend/app/api/v1/holdings.py:40-45` 只有 `price_currency == h.cost_currency` 时才计算 `market_value/unrealized_pnl`。
- 同一个文件的 portfolio summary 已经具备 `_convert_to_base()`，但 `_holding_to_out()` 没有使用任何 FX 折算。

影响：
- 用户以 EUR 成本记录美股/crypto，最新价格为 USD 时，列表和详情会显示 `current_price`，但 `market_value` 和 `unrealized_pnl` 为空。
- 这会让单项持仓看起来“无法估值”，而 portfolio summary 又可能计入该资产，前后端金额展示不一致。

建议：
- `HoldingOut` 明确返回 `market_value_currency`，至少支持用 `latest.price_currency` 计算市值。
- PnL 需要把 cost basis 和 market value 折算到同一币种；缺 FX 时返回 warning，而不是静默空值。

### V3-P2-3. 删除账户后交易仍活跃，余额/现金流口径会分裂

证据：
- `backend/app/api/v1/accounts.py:192-195` 删除账户只设置 `accounts.deleted_at`。
- `backend/app/main.py:109-113` 的 `v_account_balance` 会过滤 deleted account，但 `transactions` 本身仍是 `deleted_at IS NULL`。

影响：
- 删除账户后，该账户历史交易仍会进入 cashflow/category/timeseries，但账户余额和净值中该账户完全消失。
- 如果“删除”被用户理解为移除/归档账户，这种状态会导致 cashflow 和 net worth 口径不同，且交易仍指向一个 UI 上不可见的 account_id。

建议：
- 明确区分 `archive/close account` 与 `delete account`。
- 账户有交易/持仓时优先阻止删除，或只允许 `is_active=false` 的归档；如确实删除，应提供是否软删关联交易的显式选项。

### V3-P3-1. IntegrityError 响应仍把数据库原始错误返回给客户端

证据：
- `backend/app/core/errors.py:66-99` 会把 `str(exc.orig)` 放入 `details.original`。

影响：
- 外部调用者能看到表名、列名、索引名和部分底层数据库错误格式。
- 这不是资金计算问题，但属于安全信息泄露；在个人财务 API 中，生产响应应尽量稳定且不暴露内部 schema。

建议：
- 客户端只返回通用错误 code/message；原始 DB 错误写入服务端日志。
- 测试断言 API 响应不含 `details.original`，但日志仍保留排障信息。

## 4. 已确认改善项

- 后端规则分类主路径已经不会把 expense category 自动套到 income transaction。
- backend `/rules/test` 和 `/rules/apply-all` 已不再直接 `re.search`。
- REST/manual/bank sync create 路径开始通过 ingestion 填外币 base amount。
- PATCH 和 inbox confirm 已修非 adjustment amount 正负号归一化。
- 前端不再把 token 编进 `NEXT_PUBLIC_*` bundle；文档/env/docker-compose 已统一 `NEXT_PUBLIC_API_URL`。
- SQLite 相对路径已锚到项目根，`backend/` 目录跑测试不再失败。

## 5. 验证结果

已执行：
- 仓库根目录：`.venv/bin/python -m pytest backend/tests --ignore=backend/tests/test_api.py` → ✅ 67 passed。
- `backend/` 目录：`../.venv/bin/python -m pytest` → ✅ 67 passed, 15 skipped（integration suite 需要真实 backend，跳过合理）。
- `frontend/` 目录：`npm run build` → ✅ 通过。

当前工作区状态：
- 本次只更新 review 文档，没有改业务代码。
- 本次新增/更新文件：`code review/review V3.md`。

## 6. 建议修复顺序

1. 修 `base_amount` 生命周期：create/patch/inbox/MCP 都统一走同一个 FX normalization helper；缺汇率不能 fallback raw amount。
2. 修资产/净值币种语义：拆分 original/base value，后端、MCP、前端字段统一。
3. 修 `metadata_json` 输入校验和 SQL `json_valid()` 防护，避免余额视图被无效 JSON 打坏。
4. 修 MCP 写入路径：复用 backend ingestion 或补齐 FX、transfer matcher、safe regex、PDF guard 和测试。
5. 修 GoCardless：query credential 改 body/服务端 setup id；`country=redirect_url` 改掉；bank encryption key 统一走 Settings。
6. 修 Notion asset summary：读取 `v_account_balance`。
7. 修 `/cashflow/recompute` 跨年过滤。
8. 修 `apply_to_similar_pending()` 的 NULL category 条件。
9. 收敛较低优先级问题：持仓详情跨币种市值、账户删除语义、IntegrityError 响应脱敏。
