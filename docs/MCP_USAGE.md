# MCP 使用说明(面向 Agent)

> 给通过 MCP 接入本系统的 Agent(如 OpenClaw)看的速查。**读工具与 REST/Web 数字逐位一致**
> (复用后端同一套 service / SQL,见 `docs/API.md §17`、`docs/MCP_READ_PLAN.md`)。

## 连接方式

stdio 传输,客户端把 server 当子进程拉起。OpenClaw 已在
`~/.openclaw/workspace/config/mcporter.json` 注册:
```json
"finance-tracker": { "command": "/home/jason/finance-tracker/mcp-server/run.sh" }
```
连上后 Agent **自动发现**全部工具(名称 + 描述 + 入参 schema)与 server 级 `instructions`——
不需要额外读文档就能正确调用。本文件是补充的「何时用哪个」速查。

## 约定

- 所有金额是**字符串**(保留精度,勿当 float)。币种默认配置的 base(本部署为 **EUR**)。
- 每个工具返回 `{ "success": true, "data": ... }` 或 `{ "success": false, "error": ... }`。
- 21 工具 = **19 读 + 2 写**。Agent 日常用读工具即可。

## 读工具:何时用哪个

| 想知道… | 用 | 关键入参 |
|---|---|---|
| 我现在有多少钱(净资产) | `get_net_worth` | `currency?` |
| 资产怎么分布(现金 vs 各类投资 + 百分比) | `get_asset_allocation` | `currency?` |
| 所有账户清单 + 余额 | `list_accounts` | `active_only?` |
| 某个账户详情(投资账户带持仓) | `get_account` | `account_id` |
| 逐个持仓的数量/成本/市值/盈亏 | `list_holdings` | `account_id?` |
| 组合市值历史(周度) | `get_portfolio_value_history` | — |
| 有哪些分类(大类→子类) | `list_categories` | `kind?`, `tree?` |
| 查交易(按账户/分类/类型/日期/金额过滤) | `list_transactions` | 多过滤 + `cursor` 分页 |
| 某笔交易完整明细(对手/FX/拆分/LLM 理由) | `get_transaction` | `transaction_id` |
| 关键词搜交易 | `search_transactions` | `query` |
| 待复核(未分类)交易 | `list_inbox` | — |
| 月度现金流(收入/支出/储蓄 + 分类) | `get_cashflow` | `from_period?`,`to_period?` |
| 画图用的月度序列(收入/支出/现金资产线) | `get_cashflow_timeseries` | `from_period?`,`to_period?` |
| 某期/某区间的分类支出占比 | `get_cashflow_by_category` | `period` 或 `from_period`+`to_period` |
| 导入的银行账单清单 / 单个 | `list_statements` / `get_statement` | `import_id` |
| 为什么这笔被自动分类(规则/知识库) | `list_categorization_rules` / `list_kb_notes` | — |
| 最新行情价 + 汇率(估值基础) | `get_market_data` | — |

## 写工具(2,谨慎用)

- `add_transaction` — 代记一笔(`account_id`, `amount`, `type`, `currency`…)。source=`mcp_agent`,不走 LLM 自动分类。
- `parse_bank_statement` — 给 PDF 绝对路径,解析 + 入库。

## 示例

调用 `get_net_worth` →
```json
{ "success": true, "data": {
  "base_currency": "EUR", "net_worth": "10107.12519...",
  "cash_total": "699.40", "investment_total": "9407.72...",
  "cash_by_currency": {...}, "investment_by_currency": {...}, "as_of": "..." } }
```

调用 `list_transactions {"type":"expense","from_date":"2026-05-01","to_date":"2026-05-31","limit":50}` →
`data.transactions[]`(每条含 `category_name`)+ `data.has_more` + `data.next_cursor`(翻页传回 `cursor`)。

## 排障

- 工具报 DB / 视图错误:确保后端至少跑过一次(会建 `v_account_balance` 视图);本 MCP 也会在首次连接时幂等补建。
- 数字与 Web 对不上:不应发生(同源)。若发生,优先怀疑看的是不同 base 币种或 pending 过滤。
- 跨机器连不上:本 MCP 是 stdio,**只能同机**把 server 当子进程拉起;远程需改 HTTP/SSE(未实现)。
