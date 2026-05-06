# Finance Tracker Code Review V1（中文）

Review 日期：2026-05-05  
Review 范围：本地仓库代码、README / PROGRESS / PRD / 架构 / API / Schema 文档、后端服务与 API、前端资金和分类流程、MCP Server、银行同步与 Notion 同步 scaffold。

> **2026-05-06 验证 + 修复跟踪**：21 项问题全部经代码实地核查属实。修复已并入 `docx/ROADMAP.md` 顶部的 Sprint 0/1/2 计划。每条 issue 在标题旁标注状态：
> - 🟡 = 计划中（已写入 ROADMAP）
> - 🔧 = Sprint 进行中
> - ✅ = 已修复 + commit

| 等级 | 含义 | 涉及条目 |
|---|---|---|
| **R0** | 当前每次操作都在产生错数据（资金错误） | P0-3, P1-1, P1-2 |
| **R1** | 数据一致性 + 测试基础设施 | P1-3, P1-4, P1-5, P1-6, P3-2 |
| **R2** | 公开 GitHub / 暴露非 loopback 前必修 | P0-1, P0-2, P2-1, P2-8 |
| **R3** | 子系统启用前修（GoCardless / Notion） | P1-7, P1-8, P2-2, P2-3 |
| **R4** | 清理类（低风险，顺手） | P2-4, P2-5, P2-6, P2-7, P3-1 |

## 1. 当前已实现功能概览

- 后端：FastAPI + async SQLAlchemy + SQLite WAL，已实现账户、分类、交易 CRUD，PDF 上传 / 重解析 / 删除，规则分类，自动学习，转账匹配，现金流聚合和快照，市场数据，持仓与净资产，系统备份和设置接口。
- 前端：已实现总览页、交易列表、分类视图、待确认 inbox、转账建议、PDF 导入、账户设置、资产和持仓页面，以及多币种显示辅助逻辑。
- MCP Server：已提供总资产查询、交易查询、新增交易、PDF 解析、现金流、资产配置、交易搜索等工具。
- 文档描述的当前可用能力包括：5 家欧洲银行 PDF 解析、自动分类和学习、跨账户转账识别、多币种显示、现金流分析和 MCP tools。

## 2. P0 - 严重问题 / 真实使用前必须修复

### P0-1. Notion API 路由完全没有鉴权

证据：
- `backend/app/api/v1/notion.py:44`、`:84`、`:101`、`:117`、`:133`、`:160` 定义的路由都没有 `Depends(require_auth)`。
- 虽然该 router 被挂在 `/api/v1` 下，但项目其他模块都是逐个路由显式加鉴权；这里没有继承任何全局鉴权。

影响：
- 一旦配置了 `NOTION_TOKEN` 和 database/page ID，任何能访问 backend 的人都可以触发交易、现金流和资产数据同步到 Notion，也可以在指定 Notion 页面下创建数据库。
- 这是直接的数据外泄和外部写入风险。

建议：
- 给 Notion router 添加 `dependencies=[Depends(require_auth)]`，或给每个 Notion endpoint 加 `_token: _auth`。
- 增加鉴权回归测试：无 token 访问 `/api/v1/notion/status` 和所有 Notion mutation route 应返回 401。

### P0-2. 本地运行配置关闭鉴权，同时 backend 监听所有网卡并开放通配 CORS

证据：
- 本地 `.env` 中 `AUTH_DISABLED=true`，`BACKEND_HOST=0.0.0.0`。本报告没有复制任何 secret 值。
- `backend/app/core/auth.py:18` 在 `auth_disabled` 为 true 时直接跳过所有 token 检查。
- `backend/app/core/config.py:35` 默认 host 是 `0.0.0.0`。
- `backend/app/main.py:157-162` 设置了 `allow_origins=["*"]`、`allow_methods=["*"]`、`allow_headers=["*"]`。

影响：
- 当应用运行时，同一局域网内任何设备都可能直接调用 API。
- 因为无需凭证且 CORS 全开放，恶意网页也可以从用户浏览器跨源请求本地 `http://localhost:8010`。
- 这会暴露账户、交易、PDF 导入、备份、设置和所有写操作接口。

建议：
- 默认保持鉴权开启，即使是本地开发。
- 本地开发默认绑定 `127.0.0.1`，不要默认监听 `0.0.0.0`。
- CORS 只允许配置中的前端 origin。
- 如果 `AUTH_DISABLED=true` 且 host 不是 loopback，启动时直接失败。

### P0-3. 手动确认转账会导致余额计算错误 ✅ FIX-1（2026-05-06）

证据：
- 余额视图依赖 `metadata_json.transfer_direction` 来决定 transfer 的正负号：`backend/app/main.py:68-75`。
- 自动匹配路径 `pair_transactions` 会正确写入方向：`backend/app/services/transfer_matcher/engine.py:252-257`。
- 手动接口只把两边交易改成 `type='transfer'` 并设置 counter account，没有写入 `transfer_direction`：`backend/app/api/v1/transactions.py:401-424`。
- 前端弹窗要求用户选择转入 / 转出，但提交时丢弃了这个方向：`frontend/src/components/mark-transfer-dialog.tsx:56-69`。

影响：
- 手动确认一组转账时，两边都会变成没有方向的 transfer。余额视图会把无方向 transfer 默认当作 `-ABS(amount)`，导致转入账户也被扣钱。
- 单边转入交易通过弹窗标记为转账时，用户选择的“转入”不会被后端保存，仍会被当成出账处理。

建议：
- 当提供 `counter_transaction_id` 时，`/mark-transfer` 应直接调用 `pair_transactions()`。
- 单边转账需要接受请求 body，例如 `transfer_direction: "in" | "out"`，并写入 metadata。
- 增加测试：单边转出、单边转入、双边配对、跨月配对后的余额都必须正确。

## 3. P1 - 高优先级资金正确性问题

### P1-1. 储蓄计算把支出加上了，而不是减掉 ✅ FIX-2（2026-05-06）

证据：
- cashflow snapshot 重算使用 `WHEN type = 'expense' THEN amount`：`backend/app/services/cashflow/engine.py:43-45`。
- 实时 monthly API 也重复了同样公式：`backend/app/api/v1/cashflow.py:43`、`:136`、`:191`。
- MCP cashflow 也重复该公式：`mcp-server/src/finance_mcp/server.py:601`。
- PDF parser 和文档都约定非 adjustment 的 `transactions.amount` 存正数。

影响：
- 如果某月收入 3000、支出 1000，系统会报告储蓄 4000，而不是 2000。
- Dashboard、Analytics、CSV 导出、MCP 回答和 Notion cashflow snapshot 都可能错误。

建议：
- 统一改为 `income - expense`，例如：
  `SUM(CASE WHEN type='income' THEN ABS(amount) WHEN type='expense' THEN -ABS(amount) ELSE 0 END)`。
- 增加测试覆盖：正数支出、历史 signed expense、adjustment。

### P1-2. 现金流聚合直接混加不同币种 ✅ FIX-3（2026-05-06）

证据：
- cashflow SQL 直接 `SUM(amount)`，没有使用 `currency`、`fx_rate_to_base` 或 `base_amount`：`backend/app/api/v1/cashflow.py:40-43`、`:98`，`backend/app/services/cashflow/engine.py:40-46`。
- 前端分类视图也直接累加 raw amount，并且硬编码用 EUR 展示：`frontend/src/components/category-breakdown-view.tsx:223-224`、`:447-450`。

影响：
- EUR、CNY、USD、加密货币会被当作同一种单位直接相加。
- 月度现金流和分类占比可能看起来合理，但财务含义错误。

建议：
- 明确定义现金流基准币种。
- 优先使用 `base_amount`；缺失时按交易币种通过 FX 折算。
- cashflow API 返回币种元信息，前端不要硬编码 EUR。

### P1-3. PDF upload / confirm / reparse / delete 路径会留下 stale cashflow 或丢失分类与转账逻辑

证据：
- upload 路径会自动分类和转账匹配，但没有对导入时已变成 non-pending 的交易重算 cashflow：`backend/app/api/v1/statements.py:163-207`。
- confirm 路径只重算当前 pending rows：`backend/app/api/v1/statements.py:287-306`。
- reparse 会删除旧交易并插入新交易，但没有重新自动分类、没有保留 parser metadata、没有跑转账匹配、没有 cashflow 重算：`backend/app/api/v1/statements.py:331-371`。
- delete statement 软删除相关交易后没有重算受影响月份：`backend/app/api/v1/statements.py:404-412`。

影响：
- 自动分类成功或 transfer 类型的导入交易可能已经出现在实时查询里，但 `cash_flow_snapshots` 仍是旧值。
- reparse 后可能丢失 `metadata_json` 中的 subaccount / transfer 信息，原本自动确认的交易也会变成 pending。
- Notion cashflow sync 读取 snapshot，因此可能同步错误数据。

建议：
- 抽一个统一的 PDF transaction ingestion service，upload 和 reparse 复用同一套插入逻辑。
- upload / reparse / delete 后重算所有新旧受影响月份。
- reparse 时保留 parser metadata，并重新跑 categorizer 和 transfer matcher。

### P1-4. 交易 type 与分类 kind 没有后端一致性校验

证据：
- Transaction schema 允许独立传入 `type` 和 `category_id`：`backend/app/schemas/__init__.py:137-157`、`:160-179`。
- create / update 会直接持久化二者：`backend/app/api/v1/transactions.py:187-207`、`:306-317`。
- 创建分类时也没有校验 parent 是否存在，或 parent.kind 是否与 child.kind 一致：`backend/app/api/v1/categories.py:95-102`。

影响：
- API、MCP 或前端 bug 都可能写出 `type='expense'` 但分类属于 income 的交易。
- cashflow、分类视图、学习规则和图表会产生不一致，甚至隐藏部分交易。

建议：
- 后端校验 `Category.kind == Transaction.type`，至少对 expense / income / transfer 强制一致。
- 创建子分类时校验 parent 存在，且 parent.kind 与 child.kind 相同。
- 添加服务层测试，不依赖前端过滤来保证数据正确。

### P1-5. amount 正负号约定在不同写入路径中不一致

证据：
- 文档和 PDF parser 约定非 adjustment 金额存正数；`_make_tx` 返回 `abs(amount)`。
- Transaction API 直接保存 `Decimal(body.amount)`，没有归一化：`backend/app/api/v1/transactions.py:193`。
- Bank sync 通过金额正负判断类型，并保存 signed amount：`backend/app/services/bank_sync/engine.py:43-53`、`:365-370`。
- MCP tool 文案告诉 agent 支出可以传负数。

影响：
- 余额视图使用 `ABS` 后多数情况还能工作，但 cashflow savings 当前依赖符号，导致 PDF / API / bank sync 数据结果不一致。
- 前端按 type 加正负号，如果 amount 本身为负，可能显示双重负号。

建议：
- 强制统一 invariant：非 adjustment 的 `amount = ABS(input)`，方向只由 `type` 和 transfer metadata 决定。
- 如果需要兼容历史 signed rows，应做迁移和兼容测试。

### P1-6. 文档中的 transaction 去重和索引没有在 ORM 中实现

证据：
- `docs/SCHEMA.sql:129-134` 写了 transaction indexes 和 `(account_id, external_id)` 去重索引。
- ORM 的 `Transaction.__table_args__` 只有 check constraints，没有索引和 unique constraint：`backend/app/models/__init__.py:217-223`。
- 本地 SQLite schema 中 transactions 表没有这些索引。

影响：
- PDF / bank API 的 duplicate 保护只依赖应用层逻辑，遇到并发或其他写入路径可能失效。
- 数据量增长后，交易过滤和查询性能会变差。

建议：
- 在 ORM 或 Alembic migration 中添加真实索引和 partial unique index。
- 加测试验证同一账户 active transaction 的重复 `external_id` 会失败。

### P1-7. Bank sync 绕过核心记账流水线

证据：
- Bank sync 直接创建 `Transaction`：`backend/app/services/bank_sync/engine.py:364-388`。
- 它没有调用自动分类、转账匹配、金额归一化或 cashflow 重算。

影响：
- 一旦启用 GoCardless，银行同步交易不会和 PDF / 手动交易表现一致。
- cashflow snapshot、inbox 状态、分类和转账识别都会出现不一致。

建议：
- Bank sync batch 应复用与 PDF import 相同的 transaction ingestion service。
- 每批同步后运行分类、转账匹配并重算受影响月份。

### P1-8. Notion 资产摘要使用旧的错误余额公式

证据：
- `backend/app/services/notion_sync/engine.py:353-359` 用 `initial_balance + SUM(Transaction.amount)` 计算账户余额。

影响：
- Notion 资产摘要会把支出加到账户余额里，也忽略 transfer metadata。

建议：
- 直接读取 `v_account_balance`，或复用 `/accounts/balances` 使用的余额服务。

## 4. P2 - 中优先级安全 / 稳定性问题

### P2-1. PDF 上传没有大小和类型限制

证据：
- 上传接口直接把整个文件读入内存并写盘，再交给 parser：`backend/app/api/v1/statements.py:100-117`。

影响：
- 大文件或恶意 PDF 可能造成内存、磁盘或 CPU 消耗过高。

建议：
- 限制 request / file size。
- 校验 content type 和 PDF magic bytes。
- 给 parser 加页数、耗时和错误处理限制。

### P2-2. Bank sync 的加密凭据通过 query string 传递

证据：
- `/bank-sync/institutions` 使用 query 参数 `encrypted_credentials`：`backend/app/api/v1/bank_sync.py:70-85`。

影响：
- 即使是加密后的凭据，也可能进入浏览器历史、代理日志、access log 或监控系统。

建议：
- 改为 request body。
- 更好做法是 setup 后服务端保存凭据，前端只传 connection/setup ID。

### P2-3. 创建银行连接时很可能把 redirect URL 当 country 调用了 GoCardless

证据：
- `create_connection` 调用 `list_institutions(..., country=body.redirect_url)`：`backend/app/api/v1/bank_sync.py:106-111`。

影响：
- 连接创建流程可能在 requisition 前失败，或向 provider 发送非法国家参数。

建议：
- 删除这段未使用的 institutions 调用，或在请求 schema 中增加明确的 `country` 字段。

### P2-4. cashflow recompute 的跨年范围过滤错误

证据：
- `from_year`、`from_month`、`to_year`、`to_month` 被独立比较：`backend/app/api/v1/cashflow.py:197-200`。

影响：
- 例如 2025-12 到 2026-02 这样的范围会错误过滤月份。

建议：
- 使用单个 `YYYY-MM` period 字符串比较，或构造真实日期边界。

### P2-5. 未使用的 valuation helper 汇率方向是反的

证据：
- `compute_holding_value` 查询 `(base_currency -> latest.currency)`，然后返回 `value * fx.rate`：`backend/app/services/valuation/engine.py:37-49`。

影响：
- 如果未来启用该 helper，用 CNY->EUR 汇率把 EUR 资产折算成 CNY 时会乘错方向。

建议：
- 删除重复实现，复用 holdings / MCP 中更完整的 `_convert_to_base` 逻辑。

### P2-6. Transaction 列表分页 total 没有应用全部过滤条件

证据：
- list 查询支持 min/max/search/tags/source/is_pending 等过滤。
- count 查询只重复了 account/category/type/date：`backend/app/api/v1/transactions.py:155-167`。

影响：
- 搜索、source、tag、金额区间、pending 筛选下，前端显示的 total 和分页状态可能错误。

建议：
- 把 filters 构建成一个共享函数，同时应用到 data query 和 count query。

### P2-7. 前端 token bootstrap 有问题，且 token 存储方式较弱

证据：
- `DevTokenBootstrap` 没有被渲染；inline script 在浏览器中引用 `process.env`：`frontend/src/app/layout.tsx:10-19`、`:26-29`。
- API token 存在 `localStorage` 中。

影响：
- inline script 可能在浏览器中报 `process is not defined`，也不能稳定设置 token。
- 未来如果出现 XSS，bearer token 会被直接读取。

建议：
- 移除 inline script。
- 使用真正的 auth flow，或由服务端安全地注入 public env。
- 如果后续会暴露在非纯本地环境，优先使用 HttpOnly cookie。

### P2-8. 用户自定义 regex 分类规则可能阻塞服务

证据：
- regex rule 直接用 Python `re.search` 执行：`backend/app/services/categorizer/engine.py:66`。

影响：
- 灾难性回溯 regex 可以让分类或 `/rules/test` 卡死。

建议：
- 使用支持 timeout 的 regex 引擎。
- 或限制 regex 规则只允许可信本地用户使用，并增加复杂度校验。

## 5. P3 - 文档 / 测试 / 可维护性问题

### P3-1. `docs/SCHEMA.sql` 中的余额视图已经过期，作为参考有风险

证据：
- 文档仍写着 `initial_balance + SUM(t.amount)`：`docs/SCHEMA.sql:257-262`。
- 运行时 view 已改成按 type 和 transfer metadata 取符号：`backend/app/main.py:57-75`。

影响：
- 未来如果按文档写迁移或重建 schema，会重新引入旧的余额 bug。

建议：
- 更新 schema 文档，使其与 runtime view 一致。
- 更好的做法是由 migration / ORM 自动生成 schema 文档。

### P3-2. 后端测试陈旧，且当前本地无法运行

证据：
- `python3 -m pytest` 和 `.venv/bin/python -m pytest` 都失败，因为没有安装 `pytest`。
- `backend/tests/test_pdf_parser.py:281-290` 仍导入已经不存在的 `_parse_for_bank`、`_parse_icbc`、`_parse_cmb`、`_parse_ccb`、`_parse_boc`。

影响：
- 当前测试不能保护欧洲银行 parser、转账余额、储蓄计算、鉴权、Notion 安全、分类 kind 一致性等关键路径。

建议：
- 恢复 / 安装测试依赖。
- 用 AMEX / N26 / Revolut / TFBank / Advanzia 的测试替换旧的中国银行 parser 测试。
- 为 P0 / P1 问题增加聚焦单测。

## 6. 本次验证结果

- 在 `frontend/` 执行 `npm run build`：通过。
- 在 `backend/` 执行 `python3 -m pytest`：失败，原因是未安装 `pytest`。
- 在 repo root 执行 `.venv/bin/python -m pytest`：失败，原因是未安装 `pytest`。
- 本次 review 主要使用 `rg`、源码阅读、文档阅读和本地 schema inspection。没有修改业务代码。

## 7. 建议修复顺序

1. 收紧 auth / CORS，并给 Notion routes 补鉴权。
2. 修复手动确认转账的方向持久化，并补余额测试。
3. 修复 backend API、snapshot service 和 MCP 中的 savings 公式。
4. 在 API / service 层建立 amount sign 和 category kind invariant。
5. 抽统一 transaction ingestion pipeline，让 PDF upload / reparse / bank sync 复用，并可靠重算 cashflow。
6. 实现 ORM / migration 中的 transaction indexes 和 external dedup。
7. 定义并实现多币种 cashflow 折算语义。
8. 恢复测试环境，并覆盖 P0 / P1 路径后再继续扩展自动化能力。
