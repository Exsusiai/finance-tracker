# MCP 完整读取能力改造计划

> 目标:让任何 stdio MCP 客户端(Claude / 其它 Agent)能通过 MCP **完整、准确**地读取本系统的全部信息——
> 每个账户的信息、资产分布、每笔记账及其分类、现金流、组合估值历史等。
> **本期只做「读」,不扩展「写」**(现有 `add_transaction` / `parse_bank_statement` 两个写工具保持不动)。
>
> 状态:**已实施(P0–P3 完成)**。创建于 2026-06-28,同日落地。
> 结果:MCP 由 7 → **21 个 tool(19 读 + 2 写)**;读路径全部改为复用后端 async service,
> 删除手抄 SQL;`.mcp.json` 已接线;`test_mcp_read.py` 6 个 parity 测试 + 19 工具 live 冒烟全过;
> backend 399 测试无回归。分支 `feat/mcp-read-complete`。

---

## 一、现状诊断

### 1.1 已注册?——**否**(致命,先修)
- `~/.claude.json` 本项目 `mcpServers = {}`,项目根也没有 `.mcp.json`。
- **结论:代码能跑(7 工具可加载),但 Agent 现在根本连不上。** 不接线,一切都是空谈。

### 1.2 现有 7 个工具(5 读 + 2 写)
| 工具 | 类型 | 后端对应 | 现状问题 |
|---|---|---|---|
| `get_total_assets` | 读 | `compute_net_worth` | **手抄**净值逻辑,未复用单源 |
| `get_asset_allocation` | 读 | `/holdings/portfolio/breakdown` | **手抄** FX + 持仓聚合 |
| `get_cashflow` | 读 | `cashflow.engine` | **手抄** paired_dedup / not_subaccount / `_AMOUNT_BASE_EXPR` |
| `get_transactions` | 读 | `/transactions` | 仅摘要字段,无完整明细 |
| `search_transactions` | 读 | `/transactions/search` | 基本可用 |
| `add_transaction` | 写 | — | 本期不动 |
| `parse_bank_statement` | 写 | `parse_pdf_statement`(已 await 复用) | 本期不动 |

### 1.3 根因问题:**手抄 SQL 漂移**(架构级)
MCP 是独立的**同步 `sqlite3`** 进程,无法直接用后端的 async 服务,于是把关键资金口径 SQL **逐字重写**了一遍:
`_AMOUNT_BASE_EXPR_SYNC`、`_convert_fx`(复制自 `valuation/fx.convert_to_base`)、`not_subaccount`、`paired_dedup`、`_recompute_snapshot_sql`……

> 代码注释里 V6-P1-4 / V7-P1-3 / V8-P1-4 多次「让 MCP 对齐 REST」的修复,**全部是这套手抄逻辑漂移的直接后果**。每次后端改口径,MCP 就静默落后一次。

**而后端近期新增的读能力,MCP 完全没有跟上**:单源净值 `compute_net_worth`、组合周度快照 `value-history`、现金历史重建 `cash[]`、期末余额对账 `reconciliation`、LLM 分类理由、分类知识库……

### 1.4 读取覆盖缺口(对比后端 GET 端点)
当前 Agent **拿不到**以下信息:
1. **账户清单与元数据** — 无 `list_accounts`。拿不到账户 id/类型/币种/`include_in_total`/`sort_order`/同步状态/子账户。
2. **单源净值** — 无对应工具(现有是手抄版)。
3. **分类体系** — 无 `list_categories` / 分类树。**Agent 不知道有哪些分类、大类↔子类关系**——直接影响「每笔记账及其分类」的可理解性。
4. **单笔交易完整明细** — 无 `get_transaction`。拿不到对手账户、`base_amount`/`fx_rate`、拆分信息、`llm_reason`/`categorization_method`、metadata。
5. **逐持仓明细** — 无 `list_holdings`。拿不到每个标的的数量/成本/市值/盈亏/链/`is_active`。
6. **组合市值历史** — 无 `value-history`(周度快照)。
7. **现金流时间序列** — 无 `timeseries`(收入/支出 + `cash[]`,即 dashboard 那张图的数据)。
8. **分类分布** — 无 `by-category`(单期/区间聚合)。
9. **PDF 导入与对账** — 无 `list_statements`/`get_statement`(状态、`reconciliation`)。
10. **分类规则 + 知识库** — 无读取(Agent 无法解释「为什么这么分类」)。
11. **行情 / 汇率** — 无读取最新 `market_prices` / `fx_rates`。
12. **收件箱(待复核)** — 无 `inbox/list`。

### 1.5 测试缺口
现有 MCP 测试只校验自身 SQL,**没有「MCP == REST」的一致性(parity)断言**。所以漂移只能靠人肉 review 发现(已发生 8 轮)。

---

## 二、改造策略(keystone)

### 2.1 用后端 async 服务**取代**手抄 SQL —— 一次性根治漂移
关键事实(已验证):
- MCP 工具本身就是 `async def`,运行在 FastMCP 的事件循环里。
- MCP **已有先例**:`parse_bank_statement` 里直接 `await parse_pdf_statement(...)` 复用后端 async 代码。
- 后端导出 `app.db.session.async_session_factory` + 一批 async 服务:`compute_net_worth(db, base)`、`cashflow.engine`、`valuation.fx.convert_to_base`、`valuation.cash_history`、`valuation.snapshot` 等。

**做法**:所有读工具改为
```python
from app.db.session import async_session_factory
async with async_session_factory() as db:
    result = await compute_net_worth(db, base)   # 直接调后端单源逻辑
```
→ MCP 读数与 REST/Web **构造上必然一致**,不再靠手抄镜像。删除 `_AMOUNT_BASE_EXPR_SYNC` / `_convert_fx` / `not_subaccount` / `paired_dedup` 等全部复制品。

> 写工具(`add_transaction`/`parse_bank_statement`)**本期不动**——它们的同步 sqlite3 路径保留,避免扩大改动面(Karpathy 精准修改)。后续若要,再单独立项把写路径也迁到 async ingestion。

### 2.2 复用 REST 的响应构造逻辑
能直接调 service 的调 service;只有少数 REST 把组装逻辑写在 `api/v1/*.py` 路由里(如 `_account_to_out`、`portfolio_breakdown` 的拼装)。两种处理:
- **优先**:把这些纯组装函数抽到 `services/` 或 `schemas` 层,REST 与 MCP 共用。
- **退路**:若抽取成本高,MCP 内调用 service 拿数据后用**同一个 Pydantic schema** 序列化(`app.schemas` 已有 `AccountOut`/`HoldingOut`/...),保证字段形状一致。

---

## 三、目标读工具清单

> 命名统一:列表用 `list_*`,单对象用 `get_*`。标 **[新]** 为新增,**[改]** 为重写后端复用。

### P0 — 完整读取核心(必须)
| 工具 | 返回 | 后端来源 |
|---|---|---|
| `list_accounts` **[新]** | 全部账户:id/name/type/currency/balance/`include_in_total`/`sort_order`/子账户/同步状态 | `/accounts` + `/accounts/balances` |
| `get_account` **[新]** | 单账户详情(投资类账户带其 holdings) | `/accounts/{id}` |
| `get_net_worth` **[改]** | 单源净值:cash/investment/total + by_currency + as_of | `compute_net_worth` |
| `get_asset_allocation` **[改]** | 资产配置(按 class + 币种 + 百分比) | `portfolio_breakdown` |
| `list_holdings` **[新]** | 逐持仓:qty/avg_cost/价/市值/盈亏/链/`is_active` | `/holdings` |
| `list_categories` **[新]** | 分类树:大类 + 子类 + kind | `/categories/tree` |
| `list_transactions` **[改]** | 现 `get_transactions`,改名 + 复用后端过滤/折算 | `/transactions` |
| `get_transaction` **[新]** | 单笔完整明细:对手账户/`base_amount`/fx/拆分/`llm_reason`/`categorization_method`/metadata | `/transactions/{id}` |
| `search_transactions` **[改]** | 全文搜索,复用后端 | `/transactions/search` |
| `get_cashflow` **[改]** | 月度收支/储蓄/分类,复用 `cashflow.engine` | `/cashflow/monthly` |

### P1 — 分析与可解释性(强烈建议)
| 工具 | 返回 | 后端来源 |
|---|---|---|
| `get_cashflow_timeseries` **[新]** | 收入/支出 + `cash[]` 月度序列(dashboard 图数据) | `/cashflow/timeseries` |
| `get_cashflow_by_category` **[新]** | 分类分布(单期 / 区间聚合) | `/cashflow/by-category` |
| `get_portfolio_value_history` **[新]** | 组合周度快照序列 | `/holdings/portfolio/value-history` |
| `list_statements` + `get_statement` **[新]** | PDF 导入清单 + 单个(状态 + `reconciliation`) | `/statements` |
| `list_inbox` **[新]** | 待复核交易(含 LLM 建议) | `/transactions/inbox/list` |

### P2 — 元数据 / 排障(可选)
| 工具 | 返回 | 后端来源 |
|---|---|---|
| `list_categorization_rules` **[新]** | 分类规则(解释「为何这么分」) | `/rules` |
| `list_kb_notes` **[新]** | 分类知识库条目 | `/categorization-notes` |
| `get_market_data` **[新]** | 最新行情价 + FX 汇率 | `/market/prices` + `/market/fx` |
| `get_sync_status` **[新]** | 账户同步 / scheduler 状态(只读健康) | `/accounts/.../status` + `/system/scheduler/status` |

> P0 即满足用户需求(账户信息 / 资产分布 / 每笔记账及分类)。P1/P2 让 Agent 能做趋势分析与「可解释性」追问。

---

## 四、分阶段实施

### Phase 0 — 接线 + 冒烟(0.5 天)
1. 项目根建 `.mcp.json`:
   ```json
   {
     "mcpServers": {
       "finance-tracker": {
         "command": "./mcp-server/run.sh"
       }
     }
   }
   ```
2. `claude mcp list` / 重启会话确认连接;实跑现有 7 工具,确认读数与 Web 前端一致(基线)。
- **验证**:Agent 能列出并成功调用 7 个工具。

### Phase 1 — 架构重构(消除手抄漂移)(1–1.5 天)
1. 新增 `mcp-server/src/finance_mcp/_session.py`:封装 `async with async_session_factory()`。
2. 把 `get_total_assets`→`get_net_worth`、`get_asset_allocation`、`get_cashflow`、`get_transactions`→`list_transactions`、`search_transactions` **逐个改为调后端 async service**。
3. **删除** `_AMOUNT_BASE_EXPR_SYNC` / `_convert_fx` / `_recompute_snapshot_sql` / `not_subaccount` / `paired_dedup` 等手抄件(写工具仍需的部分保留在写工具内,但读路径一律不再引用)。
- **验证**:Phase 3 的 parity 测试(MCP 数值 == REST 数值)。

### Phase 2 — 补齐读工具(2–3 天)
按 §三 P0 → P1 → P2 顺序新增工具。每个工具:调 service / 复用 schema → 返回 `{success, data}` 信封。
- **验证**:每个工具一条 happy-path 测试 + 关键工具一条 parity 测试。

### Phase 3 — Parity 测试 + 文档(1 天)
1. 新增 `mcp-server/tests/test_mcp_parity.py`:同一份内存/临时 DB,断言
   `mcp.get_net_worth() == REST /portfolio/net-worth`、`get_cashflow ≈ /cashflow/monthly` 等。
   **这是防止未来漂移的护栏**(把人肉 review 变成 CI 断言)。
2. 更新 `docs/API.md` §17 MCP 工具清单、`README.md` 工具数、`docx/MCP_TEST_REPORT.md`、`CLAUDE.md`「MCP Server 与后端的关系」。
- **验证**:`pytest` 全绿;`docs` 与实际工具数一致。

---

## 五、风险与注意

1. **MCP 依赖后端已初始化 schema/视图**:`v_account_balance` 视图、alembic 迁移在后端 lifespan 创建。MCP 不跑 lifespan。
   - local-first 下后端通常与 MCP 同时在跑,视图已存在 → 可接受。
   - 缓解:`_session.py` 首次连接时幂等确保视图存在(调用后端现成的 `_BALANCE_VIEW_SQL`),或文档明确「先起后端」。
2. **async/sync 在同进程共存**:读走 async aiosqlite,写仍走 sync sqlite3,共享同一 WAL 库——与「后端 async + MCP sync」现状同构,WAL 保证安全。本期不混用。
3. **改动面控制**:严格只动读路径;写工具零改动。每个工具独立小提交,便于回滚。
4. **工具数量膨胀**:P0 10 个已覆盖核心需求;P1/P2 视 Agent 实际使用反馈再加,避免一次塞太多增加 Agent 选择噪音。

---

## 六、验收标准(对应用户需求)
- [x] Agent 能列出每个账户的完整信息(`list_accounts` / `get_account`)。
- [x] Agent 能获取资产分布(`get_net_worth` + `get_asset_allocation` + `list_holdings`)。
- [x] Agent 能逐笔读取记账及其分类(`list_transactions` + `get_transaction` + `list_categories`)。
- [x] 所有读数与 Web 前端**完全一致**(parity 测试 + live 验证:net-worth / cashflow 逐位对齐 REST)。
- [x] MCP 已接线(`.mcp.json`),新会话即可调用。
- [x] 不引入任何新的写能力(写工具原样保留,未扩展)。
