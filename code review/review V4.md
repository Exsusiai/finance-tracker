# Finance Tracker Code Review V4（中文）

Review 日期：2026-05-08  
Review 范围：基于 V3 后的大版本变更，重新阅读 `README.md`、`PROGRESS.md`、`docs/API.md`、`docs/ARCHITECTURE.md`、`docs/SCHEMA.sql`、`docx/WORKLOG_2026-05-07.md`、`docx/ROADMAP.md`、`docx/MCP_TEST_REPORT.md`、`CLAUDE.md`，并复查 backend / frontend / MCP server 的资金计算逻辑、安全边界、导入链路、转账匹配和资产估值路径。  
本次只做 code review，仅新增本文件，没有修改业务代码。

## 1. 总结

V3 后确实修复了不少关键点：`ingest_transactions()` 会为外币补 `fx_rate_to_base/base_amount`，交易 PATCH 会清掉 stale FX 字段并重新走 ingestion，余额视图加入 `json_valid()`，Portfolio 后端也把 `by_currency` 改成 `{original_value, base_value}`。

但目前仍不能认为“资金计算闭环已经完成”。核心风险集中在三类：

1. **现金流仍有旧 SQL 残留**：REST `/cashflow/by-category`、MCP snapshot recompute、MCP `get_cashflow()` 仍使用 `COALESCE(base_amount, amount * fx_rate_to_base, amount)`，外币缺 FX 会被当 base currency 汇总。
2. **MCP 与后端业务语义继续漂移**：MCP PDF import 虽补了部分 guard，但仍不写 FX/base amount，也没有完整复用 REST ingestion 的 transfer matcher。
3. **安全与外部同步的 V3 遗留仍在**：GoCardless 凭据 query string、country 参数 bug、Notion 资产摘要旧余额公式、bank encryption key 绕过 Settings 都还存在。

验证结果：

- Backend: `../.venv/bin/python -m pytest`，结果 `95 passed, 15 skipped`。
- Frontend: `npm run build`，结果通过。
- 说明：现有测试没有覆盖本次发现的 `/cashflow/by-category` 多币种缺 FX、MCP cashflow、Notion asset summary、GoCardless setup、前端 `by_currency` 新 shape 等路径。

## 2. V3 问题复核

| V3 finding | V4 状态 | 复核结论 |
|---|---|---|
| V3-F1 缺汇率外币仍混入现金流 | 部分修复 | backend cashflow engine / monthly / timeseries / recompute 已改 CASE+NULL；但 REST `/cashflow/by-category` 与 MCP cashflow/snapshot 仍 raw fallback |
| V3-F2 PATCH 外币金额 stale `base_amount` | 已修复 | `PATCH /transactions/{id}` 和 inbox confirm 会清空 stale FX/base 字段并调用 ingestion 重算 |
| V3-F3 MCP PDF import 不是完整 ingestion mirror | 部分修复 | 增加 PDF size/magic、regex timeout、同账户 amount-match；但仍不写 FX/base amount，也没有完整 transfer matcher |
| V3-F4 GoCardless 凭据和 country bug | 未修复 | query string 凭据与 `country=body.redirect_url` 仍在 |
| V3-F5 Notion asset summary 旧余额公式 | 未修复 | 仍用 `initial_balance + SUM(Transaction.amount)` |
| V3-F6 资产币种 breakdown 单位错误 | 部分修复 | backend schema/接口已改 `{original_value, base_value}`；frontend 类型和分布图仍按旧 `{value}` 读取 |
| V3-F7 无效 metadata_json 打坏余额视图 | 已修复 | Transaction schema 校验 JSON object；`v_account_balance` SQL 加 `json_valid()` guard |
| V3-F8 加密 key 绕过 Settings | 未修复 | `bank_sync/crypto.py` 仍直接 `os.environ.get()` |
| V3-F9 MCP total_assets 缺 FX 混加原币 | 已修复 | `get_total_assets()` 缺 FX 不再计入 base total，并返回 `fx_missing_cash` |

## 3. V4 Findings

### V4-P0-1. REST `/cashflow/by-category` 仍把缺 FX 外币按 raw amount 汇总

位置：`backend/app/api/v1/cashflow.py:114-129`

`monthly_cashflow()` 与 `timeseries()` 已改成 CASE：同币种用 amount，外币优先 base_amount / fx_rate_to_base，缺 FX 返回 NULL。但 `/cashflow/by-category` 仍是：

```sql
SUM(ABS(COALESCE(t.base_amount, t.amount * t.fx_rate_to_base, t.amount))) AS total
```

影响：

- base=CNY 时，一笔 `100 GBP` 且没有汇率的支出会在 category breakdown 中显示为 `100 CNY`。
- 同一个月份的 monthly total 与 by-category total 会不一致，用户在分类视图里看到的支出结构会错误。

建议：

- 抽一个共享 SQL 表达式，所有 cashflow route 和 recompute 都用同一套 CASE。
- `/cashflow/by-category` 增加 `base_currency` 参数绑定，并返回或至少忽略 `fx_missing` rows。
- 增加测试：同月一笔 GBP 缺 FX，monthly 和 by-category 都不能把 100 当 base currency。

### V4-P0-2. MCP cashflow/snapshot 仍使用旧 raw fallback

位置：`mcp-server/src/finance_mcp/server.py:77-99`、`mcp-server/src/finance_mcp/server.py:884-918`

MCP `_RECOMPUTE_SNAPSHOT_SQL_SYNC` 和 `get_cashflow()` 仍使用 `COALESCE(base_amount, amount * fx_rate_to_base, amount)`。这会让 MCP 写入后生成的 `cash_flow_snapshots` 与 backend recompute 的结果不一致，并且 MCP 查询现金流时继续混加缺 FX 外币。

影响：

- 即使 backend REST dashboard 已排除缺 FX 外币，只要 MCP 插入后调用自己的 recompute，就可能把错误 snapshot 写回数据库。
- Agent 通过 MCP 查询 `get_cashflow()` 会得到和 Web API 不一致的资金结果。

建议：

- MCP 侧复用 backend `cashflow.engine._AMOUNT_BASE_EXPR` 的语义，或改成调用 backend service / REST。
- 加 MCP cashflow 回归测试，覆盖缺 FX 外币、同币种、已有 `fx_rate_to_base` 三种情况。

### V4-P1-1. MCP PDF import 仍不是完整 REST ingestion mirror

位置：`mcp-server/src/finance_mcp/server.py:723-749`、`mcp-server/src/finance_mcp/server.py:775-824`

MCP PDF import 现在会保留 parser metadata、做 amount normalize、自动分类、regex timeout、同账户 amount-match，也会重算 cashflow。但它仍直接 INSERT：

- 没有为外币行写 `fx_rate_to_base/base_amount`。
- 没有运行 backend `replace_synthetic_with_real()`、`auto_pair_after_import()`、IBAN single-leg detection、orphan single-leg pairing。
- snapshot recompute 仍走旧 raw fallback。

影响：

- 同一份 PDF 走 REST upload 与 MCP import，生成的资金语义不同。
- 外币 PDF、跨账户转账、后续真腿接管 synthetic mirror 这些 V3/V4 的资金修复不会覆盖 MCP。

建议：

- 优先让 MCP PDF import 调 backend REST upload/assign-account，避免继续维护两份 ingestion。
- 如果必须 sync SQLite，至少复刻 ingestion Step 1.5 FX、synthetic-to-real upgrade、transfer matcher 全流程。

### V4-P1-2. 手动转账绑定缺少后端 invariant，可能错误排除真实收支

位置：`backend/app/api/v1/transactions.py:546-587`、`backend/app/api/v1/transactions.py:663-682`

`counter_transaction_id` 分支只根据 `transfer_direction` 或原始 type 决定 out/in，然后直接 `pair_transactions()`，没有验证：

- 两腿金额是否一致或在允许误差内。
- 两腿币种是否一致。
- 两腿账户是否不同。
- 对手交易是否已经有 incompatible pairing metadata。

`counter_account_id` synthetic mirror 分支也直接复制源交易的 `amount/currency` 到目标账户，未确认 `counter_account.currency == tx.currency`，也不写 `base_amount/fx_rate_to_base`。

影响：

- API 用户或 stale UI 可以把 `100 EUR` 支出和 `500 CNY` 收入配成 transfer，两笔都会从 income/expense 中排除，现金流被改错。
- 用户把 EUR 交易绑定到 CNY 账户时，余额视图会在 CNY 账户里按 raw `amount` 记一笔 EUR 数值，账户余额单位被污染。

建议：

- 后端必须校验 manual pair invariants，不能只依赖前端候选列表过滤。
- synthetic mirror 先禁止跨币种 counter account，或要求显式 counter amount/currency/FX 并写 base fields。
- 增加测试：不同金额、不同币种、同账户 counter tx 都应 422；不同币种 counter account synthetic mirror 也应 422。

### V4-P1-3. GoCardless 凭据与 country bug 仍未闭合

位置：`backend/app/api/v1/bank_sync.py:70-85`、`backend/app/api/v1/bank_sync.py:106-111`

`/bank-sync/institutions` 仍把 `encrypted_credentials` 放在 GET query 参数中；`create_connection()` 仍把 `redirect_url` 当 `country` 传给 institution lookup。

影响：

- 凭据会进入浏览器历史、代理/access log 或监控系统。
- `redirect_url` 不是 ISO country code，连接流程会请求错误参数，GoCardless connection setup 可能直接失败。

建议：

- 改成 POST body，或服务端保存 setup id，前端只传 setup id。
- `BankConnectionCreate` 增加 `country` 字段，或删除 create_connection 里当前未使用的 institution lookup。

### V4-P1-4. Notion 资产摘要仍同步错误账户余额

位置：`backend/app/services/notion_sync/engine.py:353-367`

Notion asset summary 仍用：

```python
Account.initial_balance + func.coalesce(func.sum(Transaction.amount), 0)
```

影响：

- expense 被加到账户余额里，transfer direction、subaccount、adjustment 符号全部被忽略。
- Notion 页面中的资产摘要会和 `/accounts/balances`、`net-worth` 不一致。

建议：

- 直接查询 `v_account_balance`，或抽一个 account balance service 给 Notion 和 REST 共用。
- 增加 Notion asset summary 单元测试：expense 应减少余额，transfer in/out 应按 metadata 处理，subaccount 应跳过。

### V4-P1-5. 前端资产分布仍按旧 `by_currency.value` 读取新接口

位置：`frontend/src/lib/api.ts:90-100`、`frontend/src/app/assets/page.tsx:909-948`

backend `PortfolioSummary/Breakdown.by_currency` 已改成：

```json
{ "EUR": { "original_value": "...", "base_value": "...", "count": 1 } }
```

但前端类型和 `DistributionPanel` 仍假设：

```ts
Record<string, { value: string; count: number }>
```

并在 `totalRaw` 与 pieData 中读取 `val.value`。结果 currency mode 下 `val.value` 是 `undefined`，会被当 0。

影响：

- 资产页“按币种分布”会显示 0 或错误占比。
- TypeScript build 仍通过，因为接口类型是前端手写的旧 shape，不能发现后端契约变化。

建议：

- 更新 frontend API types：`by_currency: Record<string, { original_value: string; base_value: string; count?: number }>`。
- currency distribution 用 `base_value` 做饼图值，同时展示 `original_value` 作为原币明细。
- 增加一个前端单测或至少 fixture，覆盖 backend 最新响应 shape。

### V4-P1-6. 前端分类视图仍把缺 FX 外币 fallback 到 raw amount

位置：`frontend/src/components/category-breakdown-view.tsx:465-476`

分类视图不是用 `/cashflow/by-category`，而是拉取当月交易后在前端聚合。这里只优先 `base_amount`，否则直接 `amount`：

```ts
const raw = baseAmt != null ? parseFloat(baseAmt) : parseFloat(t.amount);
```

影响：

- 缺 FX 外币会在前端分类结构中继续按 raw amount 混入。
- 如果手动交易只填了 `fx_rate_to_base` 但未填 `base_amount`，后端 cashflow SQL 能折算，前端分类视图不能折算。

建议：

- 聚合时使用与后端一致的 helper：same currency amount；base_amount；amount * fx_rate_to_base；否则排除并显示 fx missing warning。
- 或改为完全使用后端 `/cashflow/by-category` 的修复后结果，避免前后端两套资金公式。

### V4-P1-7. Bank sync 加密 key 仍绕过统一 Settings

位置：`backend/app/services/bank_sync/crypto.py:12-27`

项目配置已经在 `Settings.finance_bank_encryption_key` 中声明该 key，但 crypto helper 直接读 `os.environ["FINANCE_BANK_ENCRYPTION_KEY"]`。

影响：

- 本地只通过 `.env` 被 pydantic-settings 读取时，crypto 可能拿不到 key。
- 安全配置校验分散，未来 key rotation / 测试注入 / 配置审计更容易漏。

建议：

- 改为 `get_settings().finance_bank_encryption_key`，继续做 64 hex / 32 bytes 校验。
- `.env.example` 与 README/API 文档补齐生成方式。

### V4-P2-1. PDF reparse 仍有 `account_id=0` 旧 fallback

位置：`backend/app/api/v1/statements.py:601-605`

`upload` 和 `assign-account` 已经避免无法确定账户时写入交易，但 `reparse_statement()` 仍用：

```python
account_id=pdf_import.account_id or tx_data.get("account_id", 0)
```

影响：

- 对 `awaiting_account` 或缺 account 的 import 调 reparse，会尝试写 `account_id=0` 并触发 FK 错误。
- 事务会 rollback，不太会造成永久脏数据，但用户会得到 reparse failed，且错误路径不如 upload/assign-account 清晰。

建议：

- reparse 前要求 `pdf_import.account_id` 存在；否则返回 422 并提示先 assign-account。
- 删除所有 `or 0` fallback。

### V4-P2-2. 股票行情刷新可能因重复 `quoted_at` 触发唯一约束

位置：`backend/app/services/market_data/engine.py:47-72`、`backend/app/services/market_data/engine.py:149-158`、`backend/app/models/__init__.py:323-325`

`MarketPrice` 有唯一约束 `(asset_id, source, quoted_at)`。`_fetch_stock_price()` 使用 yfinance `period="1d"` 的最后一个 bar 时间作为 `quoted_at`；在同一个交易日内，scheduler 每 15 分钟刷新会反复插入同一个 `(asset_id, yfinance, quoted_at)`。

影响：

- 第二次刷新可能 IntegrityError，stock job rollback，本日后续价格刷新变成失败/partial。
- 资产估值不会更新，scheduler 状态也会噪音化。

建议：

- stock/crypto/fx 写入改为 upsert/ignore duplicate，或对同一个 `(asset_id, source, quoted_at)` 做 update。
- 增加重复刷新同一资产的测试。

### V4-P2-3. 项目文档与当前代码存在多处漂移

位置示例：

- `docs/ARCHITECTURE.md:54` 仍说 Alembic 暂未启用，但 README 已说 `backend/alembic/` baseline 已启用。
- `docs/ARCHITECTURE.md:64` 与 `docs/API.md:343` 仍记录 cashflow 使用 `COALESCE(..., amount)` raw fallback。
- `docs/SCHEMA.sql:269-275` 的 `v_account_balance` 仍没有 `json_valid()` guard。
- `docs/API.md:273-292` 仍展示旧 `by_currency` / `cash_by_currency` / `investment_by_currency` shape。
- README/PROGRESS 使用本地端口 8010/3010，CLAUDE.md 仍出现 8000/3000/3002。

影响：

- 之后继续开发或让 agent 根据文档修改代码时，很容易把已修复的资金逻辑退回旧公式。
- API consumer 会按旧 response shape 写前端，当前 `by_currency` 前端 bug 就是这种契约漂移的表现。

建议：

- 把 `docs/API.md`、`docs/ARCHITECTURE.md`、`docs/SCHEMA.sql` 作为 release checklist 必更新项。
- 为关键资金公式建立单一文档来源，并在代码注释中只引用该来源。

## 4. 优先级建议

1. 先修 P0：REST `/cashflow/by-category` 与 MCP cashflow/snapshot 的旧 FX fallback。
2. 再修 P1 资金入口：MCP PDF import 完整 ingestion mirror、manual transfer binding invariant、Notion asset summary。
3. 同一轮补安全配置：GoCardless query credential/country、bank encryption key Settings。
4. 前端同步 backend 新 shape：资产 `by_currency`、分类视图 FX 聚合。
5. 最后补文档和测试：尤其是 MCP、bank sync、Notion、frontend response shape。

## 5. 当前测试缺口

现有测试通过，但缺少以下回归：

- `/cashflow/by-category` 外币缺 FX 不应 raw fallback。
- MCP `_recompute_period_sync` / `get_cashflow` 与 backend CASE 公式一致。
- MCP PDF import 写入 FX/base_amount 并运行完整 transfer matcher。
- `mark-transfer` 不允许不同金额/币种/同账户/已绑定交易被配成一对。
- Notion asset summary 使用 `v_account_balance`。
- Frontend `PortfolioBreakdown.by_currency` 新 shape 渲染。
- market data 重复刷新同一 yfinance quote 不报 IntegrityError。
