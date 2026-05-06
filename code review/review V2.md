# Finance Tracker Code Review V2（中文）

Review 日期：2026-05-06  
Review 范围：基于 `review V1.md` 后的一轮修复结果，重新检查后端资金逻辑、分类逻辑、安全边界、MCP 写入路径、前端配置、测试与文档状态。  
重点：确认 V1 问题的修复状态，并继续寻找会影响资金正确性、分类正确性和安全性的新增问题。

## 1. 总结

V1 共 21 个问题，本轮复核结论：

| 状态 | 数量 | 说明 |
|---|---:|---|
| 已修复 | 11 | P0 资金紧急问题基本闭环；Notion 鉴权、余额方向、savings 公式、PDF REST 路径、索引等已落地 |
| 部分修复 | 6 | 多币种现金流、分类 kind invariant、amount invariant、regex 防护、前端 token、测试可运行性仍有缺口 |
| 未修复 | 4 | Notion 资产余额旧公式、GoCardless 凭据 query string、GoCardless country/redirect_url bug、cashflow 手动 recompute 跨年过滤 |

整体判断：
- R0 里最危险的手动转账余额错误、savings 加法错误、Notion 路由无鉴权已经修复。
- 仍有几个会产生错数据的路径没有闭合，尤其是 **多币种现金流没有真正折算闭环**、**自动规则分类仍可写出 type/category.kind 不一致的数据**、**MCP PDF 导入绕过 ingestion 管道**。
- 安全层面比 V1 明显收紧，但 **GoCardless 凭据 query string**、**regex 测试/批量应用路径直接 `re.search`**、**前端 token 暴露/存储模型** 仍需要继续修。

## 2. V1 问题逐项状态

| V1 编号 | 状态 | 复核结论 |
|---|---|---|
| P0-1 Notion API 无鉴权 | ✅ 已修复 | `backend/app/api/v1/notion.py` 已使用 `APIRouter(dependencies=[Depends(require_auth)])`，并新增 `test_notion_auth.py` |
| P0-2 AUTH_DISABLED + 0.0.0.0 + wildcard CORS | ✅ 已修复主风险 | 默认 host 改为 `127.0.0.1`，CORS 改 allow-list，lifespan 会拒绝 `AUTH_DISABLED=true` + 非 loopback；本地 `.env` 仍是 `AUTH_DISABLED=true`，但 host 已是 loopback |
| P0-3 手动确认转账余额错误 | ✅ 已修复 | `/mark-transfer` 已保存 `transfer_direction`，双边配对走 `pair_transactions()`，前端也传 direction |
| P1-1 savings 公式错误 | ✅ 已修复 | 后端 live/snapshot/MCP 都改为 `ABS(income) - ABS(expense)` |
| P1-2 多币种现金流混加 | ✅ 已修复（FIX-13, 2026-05-06）| ingestion 加 Step 1.5：自动 resolve_fx_to_base + 写 base_amount/fx_rate_to_base；缺汇率时标 metadata.fx_missing=true 而非静默 fallback。5 用例覆盖 |
| P1-3 PDF upload/reparse/delete stale cashflow | ✅ REST 路径已修复 | REST upload/reparse/delete 已接入 ingestion/recompute；但 MCP PDF 导入仍绕过，见 V2-P0-3 |
| P1-4 Transaction.type 与 Category.kind invariant | ✅ 已修复（FIX-14, 2026-05-06）| categorize_transaction selectinload Category 后跳过 kind 不匹配的规则；apply_to_similar_pending 入口加守卫；/rules/apply-all 同样保护；rules create/update 校验 category 存在 |
| P1-5 amount 正负号 invariant | ✅ 已修复（FIX-15, 2026-05-06）| transactions PATCH 和 inbox confirm 加 ABS（非 adjustment）；MCP parse_bank_statement 改成走完整 sync mirror（amount normalize + categorize + kind guard + recompute）|
| P1-6 transaction 索引和 external_id 去重 | ✅ 已修复 | ORM + lifespan DDL 已加 index 和 partial unique；测试覆盖通过 |
| P1-7 bank sync 绕过核心流水线 | ✅ 已修复 | `BankSyncEngine.sync_transactions()` 新交易已调用 `ingest_transactions()` |
| P1-8 Notion 资产摘要旧余额公式 | ❌ 未修复 | 仍使用 `Account.initial_balance + SUM(Transaction.amount)` |
| P2-1 PDF 上传无大小/类型限制 | ✅ REST 路径已修复 | REST upload 已限制 10 MiB 并校验 `%PDF-`；MCP PDF 读取本地文件未套这个限制 |
| P2-2 Bank sync 加密凭据走 query string | ❌ 未修复 | `/bank-sync/institutions` 仍把 `encrypted_credentials` 放在 query 参数 |
| P2-3 Bank connection 把 redirect_url 当 country | ❌ 未修复 | `create_connection()` 仍 `country=body.redirect_url` |
| P2-4 cashflow recompute 跨年过滤错误 | ❌ 未修复 | 仍独立比较 year/month，跨年范围会错 |
| P2-5 valuation helper FX 方向反 | ✅ 已修复 | 旧 helper 已删除 |
| P2-6 transaction 分页 total 过滤不一致 | ✅ 已修复 | data query 和 count query 共用 `_apply_filters()` |
| P2-7 前端 token bootstrap/存储 | ✅ 已修复（FIX-18, 2026-05-06）| 删除 layout.tsx 内 NEXT_PUBLIC_API_TOKEN 注入；settings 页加 ApiTokenInput 组件让用户手动粘贴。HttpOnly cookie/真正 auth flow 仍是 P3（远端部署时再上）|
| P2-8 regex ReDoS | ✅ 已修复（FIX-16, 2026-05-06）| rules.py _match_rule 改用 _safe_regex_search；/rules/test、/rules/apply-all 全部走同一 wrapper |
| P3-1 `docs/SCHEMA.sql` 余额视图过期 | ✅ 已修复 | 文档视图已同步 `transfer_direction`/`subaccount` 逻辑 |
| P3-2 测试陈旧/不可运行 | ✅ 已修复（FIX-17, 2026-05-06）| Settings.resolved_database_url 把 sqlite:///./xxx 解析到 _PROJECT_ROOT；从任何 cwd 跑 pytest 都使用同一份 DB |

## 3. V2 新发现 / 仍需优先修复的问题

### V2-P0-1. 多币种现金流仍没有真正折算闭环

证据：
- `backend/app/api/v1/cashflow.py:40-47`、`:204-210` 和 `backend/app/services/cashflow/engine.py:36-52` 使用 `COALESCE(base_amount, amount * fx_rate_to_base, amount)`。
- 但 `backend/app/services/ingestion/__init__.py` 只做 amount 正负归一化、分类、转账匹配、recompute，没有填充 `fx_rate_to_base` 或 `base_amount`。
- PDF parser、bank sync 和前端手动交易表单也没有稳定写入 `base_amount`。

影响：
- 只要外币交易没有 `base_amount/fx_rate_to_base`，现金流仍会 fallback 到 raw amount。
- 例如 base=CNY 时，`100 EUR` expense 会被当成 `100 CNY`，分类视图、月度 savings、MCP cashflow 都会低估支出。

建议：
- 在 ingestion 管道中根据 `Transaction.currency` 和 `settings.base_currency` 统一补 `fx_rate_to_base/base_amount`。
- 如果没有汇率，应标记为 `fx_missing` 并从 base cashflow 中剔除或返回 warning，不要静默混加 raw amount。
- 增加测试：EUR/USD/CNY 混合交易，缺汇率时必须可见失败或 warning，有汇率时必须折算正确。

### V2-P0-2. 自动分类规则仍可写出 `Transaction.type` 与 `Category.kind` 不一致的数据

证据：
- `backend/app/services/categorizer/engine.py:90-92` 规则命中后直接 `tx.category_id = rule.category_id`。
- `backend/app/api/v1/rules.py:92-99` 创建规则时只保存 `category_id`，没有限制规则适用的 transaction type。
- `backend/app/api/v1/rules.py:225-227` `/rules/apply-all` 也直接赋值。

影响：
- 用户可以创建一个指向 expense category 的规则，但该规则命中 income 交易时，系统会写出 `type='income'` + `category.kind='expense'`。
- 这会破坏分类视图、cashflow by-category、规则学习和后续报表口径。V1 的 kind invariant 只保护了手动 API 路径，没有保护自动分类路径。

建议：
- 规则匹配时 join/load Category，并只允许 `Category.kind == tx.type` 的规则命中。
- 创建/更新规则时校验 category 存在；如果未来支持跨 kind，需要 rule schema 显式包含 `applies_to_type`。
- 给 `categorize_transaction()` 和 `/rules/apply-all` 加不匹配规则不会赋值的测试。

### V2-P0-3. MCP `parse_bank_statement` 仍绕过统一 ingestion 管道

证据：
- `mcp-server/src/finance_mcp/server.py:585-608` 直接 INSERT `transactions`。
- 该路径没有写入 parser 产生的 `metadata_json`，没有自动分类，没有转账匹配，没有 cashflow recompute。

影响：
- 通过 MCP 导入 PDF 时，REST 修复的 PDF ingestion 逻辑不会生效。
- 子账户 metadata、cross-bank transfer hint 会丢失；`auto_confirm=True` 时交易进入非 pending，但 cashflow snapshot 不会刷新。
- 这与 `PROGRESS.md` 中“mcp 全部接入统一管道”的描述不一致。

建议：
- 最好让 MCP 调用 backend API 或复用 async `ingest_transactions()`。
- 如果必须保留 sync SQLite 写入，也要补：metadata_json、amount normalize、categorize、transfer matching、affected periods recompute。
- 增加 MCP PDF import 回归测试，验证导入后 cashflow snapshot 与 REST upload 一致。

### V2-P1-1. Notion 资产摘要仍使用旧余额公式

证据：
- `backend/app/services/notion_sync/engine.py:355-359` 仍是 `Account.initial_balance + SUM(Transaction.amount)`。

影响：
- 支出会被加到账户余额里，transfer/subaccount metadata 也完全被忽略。
- 一旦启用 Notion asset sync，Notion 中的账户余额会明显错误。

建议：
- 改为读取 `v_account_balance`，或抽一个账户余额 service，让 `/accounts/balances` 和 Notion 共用。

### V2-P1-2. GoCardless 连接流程两个 V1 问题仍未修

证据：
- `backend/app/api/v1/bank_sync.py:70-85` 仍通过 query 参数接收 `encrypted_credentials`。
- `backend/app/api/v1/bank_sync.py:106-111` 仍把 `body.redirect_url` 传给 provider 的 `country`。

影响：
- query string 可能进入浏览器历史、代理日志、服务日志。
- `redirect_url` 不是 ISO country code，连接创建流程可能直接失败。

建议：
- `/institutions` 改为 POST body，或由服务端保存 setup credential 后前端只传 setup/connection id。
- `BankConnectionCreate` 增加 `country` 字段，或删除 create_connection 里未使用的 institution lookup。

### V2-P1-3. 手动 cashflow recompute 跨年范围仍错误

证据：
- `backend/app/api/v1/cashflow.py:215-218` 仍分别比较 year/month。

影响：
- `from=2025-12` 到 `to=2026-02` 会错误过滤掉 2026-01/02 或包含/排除异常月份。

建议：
- 改为比较 `substr(occurred_at, 1, 7)` 的 `YYYY-MM`，或构造真实日期边界。

### V2-P1-4. regex ReDoS 修复只覆盖部分路径

证据：
- 主分类引擎 `_safe_regex_search()` 已存在。
- 但 `backend/app/api/v1/rules.py:48-60` 的 `_match_rule()` 仍直接 `re.search()`，并被 `/rules/test` 和 `/rules/apply-all` 使用。

影响：
- 即使写入时校验拦截了一部分危险 pattern，已有旧规则、未来绕过写入的规则、或漏判 pattern 仍可能卡住 `/rules/test` / `/rules/apply-all`。
- 线程池 timeout 也不是强隔离，Python `re` 的灾难性回溯可能让 worker 长时间占用，后续 regex 任务排队超时。

建议：
- 所有 regex 路径共用同一个安全 matcher。
- 更稳妥是使用支持 timeout 的 regex 库，或禁用自定义 regex，只保留 contains/exact/starts_with。

### V2-P2-1. 分类级联对 `category_id IS NULL` 的交易不会生效

证据：
- `backend/app/services/categorizer/engine.py:202` 使用 `Transaction.category_id != category_id`。

影响：
- SQL 中 `NULL != x` 为 unknown，不会命中。因此同描述、未分类的 pending 交易不会被“改 1 笔带动同描述兄弟”覆盖。

建议：
- 改成 `or_(Transaction.category_id.is_(None), Transaction.category_id != category_id)`。
- 增加测试：seed 交易改分类后，同 description 且 `category_id=None` 的 pending 交易也应被确认并赋同分类。

### V2-P2-2. 前端环境变量名与文档 / docker-compose 不一致

证据：
- 前端实际读取 `frontend/src/lib/api.ts:1` 的 `NEXT_PUBLIC_API_URL`。
- `.env.example:50` 和 `docker-compose.yml:37` 写的是 `NEXT_PUBLIC_API_BASE_URL`，且示例值包含 `/api/v1`。

影响：
- 新环境按文档配置时，前端会忽略配置并 fallback 到 `http://localhost:8000`。
- 如果把变量名改对但保留 `/api/v1` 后缀，又会拼成 `/api/v1/api/v1/...`。

建议：
- 统一变量名和语义：要么 `NEXT_PUBLIC_API_URL=http://localhost:8000`，要么修改 API client 不再硬编码 `/api/v1`。
- 同步 README、CLAUDE、docker-compose 和 `.env.example`。

### V2-P2-3. `DATABASE_URL=sqlite:///./data/finance.db` 对 cwd 敏感

证据：
- `.env.example:32` 和本地 `.env` 使用相对路径。
- 从仓库根目录运行测试通过；从 `backend/` 目录运行 `../.venv/bin/python -m pytest` 会尝试打开 `backend/data/finance.db` 并失败。
- `backend/app/db/session.py:17-23` 直接把 `sqlite:///./data/finance.db` 转成 async URL，没有按项目根解析。

影响：
- 不同启动 cwd 会使用不同数据库路径，可能导致测试失败、备份路径不一致，甚至启动一个空库。

建议：
- 在 settings 层把相对 SQLite 路径解析到项目根 `_PROJECT_ROOT`。
- 或把 `.env.example` 改成绝对容器/项目路径，并在 README 明确只能从仓库根启动。

### V2-P2-4. 前端 token 方案仍不适合非纯本地环境

证据：
- `frontend/src/app/layout.tsx:20-28` 会把 `NEXT_PUBLIC_API_TOKEN` 写入浏览器 `localStorage["finance_api_token"]`。

影响：
- `NEXT_PUBLIC_*` 本质是公开变量；一旦使用该方式，token 会进入前端 bundle。
- localStorage token 遇到 XSS 会被直接读取。

建议：
- 对纯本地模式可以继续 `AUTH_DISABLED=true + loopback`。
- 对任何非本机访问，应做登录页 + HttpOnly cookie/session，或至少手动输入 token 且不要通过 `NEXT_PUBLIC_*` 注入。

## 4. 本次验证结果

已执行：
- `frontend/` 下 `npm run build`：✅ 通过。
- 仓库根目录执行 `.venv/bin/python -m pytest backend/tests --ignore=backend/tests/test_api.py`：✅ 53 passed。
- `backend/` 目录执行 `../.venv/bin/python -m pytest`：❌ 52 passed / 1 failed / 15 skipped，失败原因是相对 `DATABASE_URL` 被解析到 `backend/data/finance.db`。
- `backend/` 目录执行系统 `python3 -m pytest`：❌ 当前系统 Python 没有安装 pytest。

说明：
- 当前“测试已恢复”是成立的，但运行方式必须固定在仓库根目录或修正相对 DB path。
- `test_api.py` 是需要真实 backend 的 integration suite，未启动服务时跳过是合理的。

## 5. 建议修复顺序

1. 先补多币种现金流闭环：ingestion 写入 `base_amount/fx_rate_to_base`，缺汇率时显式 warning/失败。
2. 修自动分类 kind invariant：规则匹配、规则创建/更新、`apply-all` 都必须保证 `Category.kind == Transaction.type`。
3. 修 MCP PDF import：不能再直接 INSERT 半套字段，必须复用 REST ingestion 语义。
4. 修 Notion asset summary：读取 `v_account_balance`。
5. 修 GoCardless 的 query credential 与 `country=redirect_url`。
6. 修 `/cashflow/recompute` 跨年范围。
7. 统一 regex matcher，关闭所有直接 `re.search` 路径。
8. 统一前端环境变量和 DB 路径解析，让 README / docker-compose / 本地测试不再互相矛盾。
