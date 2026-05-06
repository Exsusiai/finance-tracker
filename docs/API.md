# API 设计文档

> Finance Tracker REST API — v1
> 基地址: `http://localhost:8010/api/v1` (本地开发默认端口)
> 鉴权: `Authorization: Bearer <FINANCE_TRACKER_API_TOKEN>` (除 `/health` 外全部要求；本地默认 `AUTH_DISABLED=true` 跳过——Sprint 2 FIX-9 加了 invariant：仅当 `BACKEND_HOST` 是 loopback 时才允许跳过鉴权)
> CORS: 允许列表由 `ALLOWED_ORIGINS` 控制（默认 `http://localhost:3000,http://localhost:3010,…`）
> 前端 token 注入：用户在 Settings → API Token 输入框手动粘贴（FIX-18 后不再走 `NEXT_PUBLIC_API_TOKEN` bundle 注入）
> 最后修订: 2026-05-06

## 通用约定

### 请求/响应信封

成功响应:
```json
{ "success": true, "data": <payload>, "meta": { ... } }
```

错误响应 (HTTP 4xx/5xx):
```json
{ "success": false, "error": { "code": "INVALID_INPUT", "message": "...", "details": {...} } }
```

### 时间/金额

- 时间统一 ISO-8601: `2026-05-01T10:30:00Z`
- 金额统一字符串格式以保留精度: `"1234.56789012"`
- `transactions.amount` **始终存正绝对值**，方向由 `type` 决定（`adjustment` 例外，保留符号）
- 币种 ISO-4217 (大写): `"CNY"`、`"EUR"`、`"BTC"`

---

## 1. 健康检查

| Method | Path        | 说明                  | 鉴权 |
|--------|-------------|-----------------------|------|
| GET    | `/health`   | 服务存活检查          | 否   |

---

## 2. 账户 Accounts

| Method | Path                              | 说明                                        |
|--------|-----------------------------------|---------------------------------------------|
| GET    | `/accounts`                       | 列出所有账户                                |
| POST   | `/accounts`                       | 创建账户                                    |
| GET    | `/accounts/{id}`                  | 单个账户详情                                |
| PATCH  | `/accounts/{id}`                  | 更新账户 (含 IBAN / 子账户清单)             |
| DELETE | `/accounts/{id}`                  | 软删除账户                                  |
| GET    | `/accounts/{id}/balance`          | 当前余额 (来自 `v_account_balance` 视图)    |
| GET    | `/accounts/balances`              | 全账户余额一次返回                          |
| POST   | `/accounts/{id}/adjust-balance`   | 余额校准（自动建一笔 `adjustment` 交易）    |

**创建请求示例**
```json
{
  "name": "Main Checking",
  "type": "bank",
  "institution": "<bank-name>",
  "account_number": "<masked-account-number>",
  "iban": "<IBAN>",
  "currency": "EUR",
  "initial_balance": "1500.00",
  "metadata_json": "{\"subaccount_names\": [\"Investing\", \"Dream List\"]}"
}
```

**`adjust-balance` 请求**
```json
{
  "target_balance": "1234.56",
  "note": "校准至银行 App 显示",
  "occurred_at": "2026-05-05T10:00:00Z"
}
```
返回新的 `BalanceOut`。系统自动创建 `type=adjustment` 交易，金额 = `target − current`。

---

## 3. 分类 Categories

| Method | Path                  | 说明                                        |
|--------|-----------------------|---------------------------------------------|
| GET    | `/categories`         | 列出全部 (支持 `?kind=expense\|income\|transfer`) |
| GET    | `/categories/tree`    | 返回带子分类的树形结构                      |
| POST   | `/categories`         | 创建分类                                    |
| PATCH  | `/categories/{id}`    | 更新分类                                    |
| DELETE | `/categories/{id}`    | 删除 (system 分类禁止)                      |

---

## 4. 交易 Transactions

### 基础 CRUD

| Method | Path                                    | 说明                                     |
|--------|-----------------------------------------|------------------------------------------|
| GET    | `/transactions`                         | 列表 (过滤参数见下)                      |
| POST   | `/transactions`                         | 手动录入交易 (触发 cashflow 重算)        |
| POST   | `/transactions/batch`                   | 批量录入 (PDF 导入用)                    |
| GET    | `/transactions/{id}`                    | 单条详情                                 |
| PATCH  | `/transactions/{id}`                    | 更新 (常用于改分类，触发学习+级联)       |
| DELETE | `/transactions/{id}`                    | 软删除                                   |
| POST   | `/transactions/{id}/categorize`         | 重新跑分类规则                           |

### Inbox 工作流

| Method | Path                                              | 说明                                                  |
|--------|---------------------------------------------------|-------------------------------------------------------|
| GET    | `/transactions/inbox/list`                        | 待确认列表 (`is_pending=true`)                        |
| POST   | `/transactions/inbox/{id}/confirm`                | 用户确认并归类（触发学习+级联）                       |

**`/inbox/{id}/confirm` 请求**
```json
{ "category_id": 12, "user_note": "每月房租，房东微信" }
```
副作用：
1. 设 `category_id` + `user_note` + `is_pending=false`
2. 调用 `learn_from_user_assignment`：从 `description` 提取关键词新建/加强 `categorization_rules`
3. 调用 `apply_to_similar_pending`：同 `description` 的兄弟交易级联归类（保护 `source!=manual`、`type!=transfer`、`type==seed.type`）
4. cashflow 重算受影响月份

> **自动通过 inbox**：PDF 导入时若命中规则 → 直接 `is_pending=false` 入账，不再要求人工确认。

### 跨账户转账识别

| Method | Path                                            | 说明                                                |
|--------|-------------------------------------------------|-----------------------------------------------------|
| GET    | `/transactions/transfers/suggestions`           | 候选转账配对列表（评分 ≥ 50 但未自动配对）         |
| POST   | `/transactions/{id}/mark-transfer`              | 用户手动确认配对（body schema 见下）                |

**`mark-transfer` 请求 body**（Sprint 0 FIX-1）：
```json
{
  "counter_transaction_id": 42,           // optional
  "transfer_direction": "out"             // "in" | "out"，单边场景必填
}
```
- 双边（counter_transaction_id 给定）：调用 `pair_transactions()` 同时给两腿打 `metadata.transfer_direction`，`v_account_balance` 视图据此正负折算余额
- 单边（无 counter）：仅当腿 `type = transfer` + 写 metadata.transfer_direction
- 缺 direction → 422 INVALID_INPUT

**评分算法（`transfer_matcher`）**：
- 金额相同 +50；±0.5% 浮动 +30
- 日期相同 +30；±1 天 +20；±3 天 +10
- 描述提示词 (transfer/sepa/wire/...) 0..30
- IBAN 命中对方账户 +40
- **阈值 75 自动配对**；50..74 进入 suggestions 列表等用户确认

### 列表过滤参数

- `account_id` / `category_id` / `type` (expense/income/transfer/adjustment)
- `from_date` / `to_date` (YYYY-MM-DD)
- `min_amount` / `max_amount`
- `search` (在 description / counterparty 中模糊搜索)
- `is_pending` (true / false)
- `limit` / `offset`

### 列表项示例

```json
{
  "id": 42,
  "account_id": 1,
  "account_name": "Main Checking",
  "counter_account_id": null,
  "category_id": 5,
  "category_name": "餐饮",
  "occurred_at": "2026-04-28T19:30:00Z",
  "amount": "12.50",
  "currency": "EUR",
  "base_amount": "98.00",
  "type": "expense",
  "description": "GROCERY STORE",
  "raw_description": "Card payment GROCERY STORE 28.04",
  "tags": [],
  "source": "pdf_import",
  "pdf_import_id": 7,
  "is_pending": false,
  "metadata_json": "{\"subaccount\": false, \"transfer_direction\": null}",
  "user_note": null,
  "created_at": "2026-04-29T08:00:00Z",
  "updated_at": "2026-04-29T08:00:00Z"
}
```

`metadata_json` 约定字段：
- `subaccount` (bool) — 同行子账户搬运（不影响余额）
- `transfer_direction` (`"in" | "out"`) — 配对后赋值
- `cross_bank_hint` (bool) — 跨行预标
- `matched` (bool) — 已配对
- `source` (`"keyword" | "user_list" | "amount_match"`) — subaccount 三层识别来源
- `paired_with_tx_id` (int) — 对端交易 ID

---

## 5. PDF 账单导入 Statements

| Method | Path                              | 说明                                           |
|--------|-----------------------------------|------------------------------------------------|
| POST   | `/statements/upload`              | 上传 PDF (multipart/form-data, field=`file`, query `?account_id=N`) |
| GET    | `/statements`                     | 历史导入批次列表                               |
| GET    | `/statements/{id}`                | 批次详情 (含已生成的 transactions)             |
| POST   | `/statements/{id}/reparse`        | 触发重新解析 (用于解析器升级后)                |
| POST   | `/statements/{id}/confirm`        | 确认入账 (将 pending 交易转为正式)             |
| DELETE | `/statements/{id}`                | 撤销整个批次的所有 transactions                |

**支持银行**：AMEX-DE / N26 / Revolut / TFBank / Advanzia (5 家欧洲银行)
- Revolut 使用 column-aware 解析器（按 Money out / Money in 列定位）
- 其他使用 text-regex 解析器
- 上传时 SHA-256 哈希去重

**上传响应示例**
```json
{
  "id": 7,
  "filename": "statement_2026_04.pdf",
  "file_hash": "abc...",
  "file_size": 124567,
  "detected_bank": "<bank-key>",
  "parser_version": "<bank-key>-v1",
  "account_id": 1,
  "statement_period": "2026-04",
  "transactions_count": 42,
  "status": "success",
  "preview": [ /* 前 5 条 TransactionOut */ ],
  "created_at": "2026-04-29T08:00:00Z",
  "updated_at": "2026-04-29T08:00:00Z"
}
```

---

## 6. 资产与持仓 Assets / Holdings

### Assets

| Method | Path                              | 说明                                 |
|--------|-----------------------------------|--------------------------------------|
| GET    | `/assets`                         | 资产定义列表 (?asset_class=crypto)   |
| GET    | `/assets/search`                  | CoinGecko + yfinance 联合搜索 (`?q=BTC`) |
| POST   | `/assets`                         | 新增资产定义                         |
| GET    | `/assets/{id}`                    | 资产详情 + 最新价                    |
| PATCH  | `/assets/{id}`                    | 更新                                 |
| DELETE | `/assets/{id}`                    | 删除                                 |

### Holdings & Portfolio

| Method | Path                              | 说明                                 |
|--------|-----------------------------------|--------------------------------------|
| GET    | `/holdings`                       | 全部持仓 + 实时估值                  |
| GET    | `/holdings/{id}`                  | 单个持仓详情                         |
| POST   | `/holdings`                       | 新增持仓                             |
| PATCH  | `/holdings/{id}`                  | 更新数量 / 成本                      |
| DELETE | `/holdings/{id}`                  | 删除持仓                             |
| GET    | `/holdings/portfolio/summary`     | 总资产估值 (折算到基础币种)          |
| GET    | `/holdings/portfolio/breakdown`   | 按类别 / 币种饼图数据                |
| GET    | `/holdings/portfolio/net-worth`   | 净资产 = 现金 + 投资（按币种细分）   |

**Portfolio Summary 响应**
```json
{
  "base_currency": "CNY",
  "total_value": "1234567.89",
  "as_of": "2026-05-01T10:30:00Z",
  "by_class": {
    "cash":     "100000.00",
    "crypto":   "500000.00",
    "us_stock": "284567.89",
    "eu_stock": "200000.00",
    "gold":     "150000.00"
  },
  "by_currency": {
    "CNY": "650000.00",
    "EUR": "300000.00",
    "USD": "284567.89"
  }
}
```

**Net Worth 响应**
```json
{
  "base_currency": "CNY",
  "cash_total": "350000.00",
  "investment_total": "884567.89",
  "net_worth": "1234567.89",
  "cash_by_currency": {
    "CNY": { "amount": "120000.00", "base_amount": "120000.00" },
    "EUR": { "amount": "30000.00",  "base_amount": "230000.00" }
  },
  "investment_by_currency": { "USD": "284567.89", "CNY": "600000.00" },
  "as_of": "2026-05-01T10:30:00Z"
}
```

---

## 7. 行情 Market Data

| Method | Path                                  | 说明                                     |
|--------|---------------------------------------|------------------------------------------|
| GET    | `/market/prices/{asset_id}`           | 单资产价格 (?range=1d\|1m\|1y)           |
| POST   | `/market/refresh`                     | 立即拉取一次行情 (异步任务)              |
| GET    | `/market/refresh/status`              | 上次刷新状态                             |
| GET    | `/market/fx`                          | 汇率快照 (?base=CNY&quote=EUR,USD,JPY)   |

**FX 折算策略**：先 direct → inverse → 三角换算（依次经 CNY / USD / EUR pivot）

---

## 8. 现金流分析 Cash Flow

| Method | Path                              | 说明                                  |
|--------|-----------------------------------|---------------------------------------|
| GET    | `/cashflow/monthly`               | 按月聚合 (?from=2025-01&to=2026-04)   |
| GET    | `/cashflow/by-category`           | 按分类聚合 (?period=2026-04)          |
| GET    | `/cashflow/timeseries`            | 收入/支出/储蓄三线时间序列            |
| POST   | `/cashflow/recompute`             | 触发指定区间快照重算                  |

> 数据源：`cash_flow_snapshots` 表（写时计算）。每次 transaction CRUD / inbox confirm / adjust-balance 都会自动重算受影响月份。

**月度聚合响应示例**
```json
[
  {
    "period": "2026-04",
    "base_currency": "CNY",
    "income":   "25000.00",
    "expense":  "12340.50",
    "transfer": "0.00",
    "savings":  "12659.50",
    "by_category": {
      "餐饮":   "2300.00",
      "交通":   "800.00",
      "工资":   "25000.00"
    },
    "by_account": { "Main Checking": "...", "Savings": "..." }
  }
]
```

> Sprint 0 FIX-3：响应额外包含 `base_currency` 字段；金额都已折算到 base 币种（用 `COALESCE(base_amount, amount * fx_rate_to_base, amount)`）。`expense` 是 `ABS()` 后的正值；`savings = income − expense`。

---

## 9. 分类规则 Categorization Rules

| Method | Path                              | 说明                                    |
|--------|-----------------------------------|-----------------------------------------|
| GET    | `/rules`                          | 规则列表                                |
| POST   | `/rules`                          | 新增规则                                |
| PATCH  | `/rules/{id}`                     | 更新                                    |
| DELETE | `/rules/{id}`                     | 删除                                    |
| POST   | `/rules/test`                     | 测试规则 (传 description, 返回命中)     |
| POST   | `/rules/apply-all`                | 对历史 transactions 重跑规则            |

**学习机制**：
- 用户在 inbox confirm / 列表 PATCH 改分类时 → 自动从 `description` 提取关键词建/加强规则
- `hit_count` 字段记录该规则命中次数

---

## 10. 系统/配置 System

| Method | Path                          | 说明                                    |
|--------|-------------------------------|-----------------------------------------|
| GET    | `/system/settings`            | 当前配置 (基础币种 / 行情刷新周期)      |
| PATCH  | `/system/settings`            | 更新配置                                |
| POST   | `/system/backup`              | 触发数据库备份                          |
| GET    | `/system/backups`             | 列出已有备份文件                        |
| GET    | `/system/scheduler/status`    | APScheduler 任务运行状态 + 下次触发时间 |

---

## 11. 银行直连 Bank Sync (scaffold, P2-1)

| Method | Path                                                  | 说明                                  |
|--------|-------------------------------------------------------|---------------------------------------|
| POST   | `/bank-sync/setup`                                    | GoCardless 初始化（保存 secret_id/key）|
| GET    | `/bank-sync/institutions`                             | 列出支持的银行（?country=DE）          |
| POST   | `/bank-sync/connections`                              | 创建连接（返回 OAuth 跳转链接）        |
| GET    | `/bank-sync/connections`                              | 已有连接列表                          |
| GET    | `/bank-sync/connections/{id}`                         | 单个连接详情                          |
| PATCH  | `/bank-sync/connections/{id}`                         | 更新连接（绑定 account_id 等）         |
| DELETE | `/bank-sync/connections/{id}`                         | 删除连接                              |
| POST   | `/bank-sync/callback`                                 | OAuth 回调端点                        |
| POST   | `/bank-sync/connections/{id}/sync`                    | 手动触发同步                          |
| GET    | `/bank-sync/status`                                   | 整体同步状态                          |
| GET    | `/bank-sync/providers`                                | 列出可用 provider（GoCardless / Tink） |

> **当前状态**：scaffold 完成，未联调。详见 `docs/BANK_API_DESIGN.md`。

---

## 12. Notion 同步 Notion Sync (P2-3)

| Method | Path                              | 说明                                       |
|--------|-----------------------------------|--------------------------------------------|
| POST   | `/notion/sync`                    | 全量同步 (transactions + cashflow + assets)|
| POST   | `/notion/sync/transactions`       | 仅同步交易                                 |
| POST   | `/notion/sync/cashflow`           | 仅同步月度现金流快照                       |
| POST   | `/notion/sync/assets`             | 仅同步资产持仓汇总                         |
| POST   | `/notion/setup`                   | 一键创建 Notion 数据库                     |
| GET    | `/notion/status`                  | 上次同步时间 + 状态                        |

> **当前状态**：scaffold 完成，未联调。

---

## 13. MCP Tools (非 HTTP, stdio)

由 `mcp-server/` 进程暴露，Agent 可调用以下 tool（共 7 个，已 6 轮回归测试 9 bug 全修）：

| Tool 名                       | 功能                                              |
|-------------------------------|---------------------------------------------------|
| `get_total_assets`            | 总资产估值 + 按类别/币种细分                      |
| `get_transactions`            | 查询交易（支持过滤条件 JSON）                     |
| `add_transaction`             | Agent 代用户记一笔账                              |
| `parse_bank_statement`        | 接收 PDF 字节流并触发解析+入库                    |
| `get_cashflow`                | 月度现金流摘要                                    |
| `get_asset_allocation`        | 资产配置饼图数据（按类别 / 币种）                 |
| `search_transactions`         | 关键词搜索交易                                    |

每个 tool 返回结构化 JSON（`{ ok: true, data }` 或 `{ ok: false, error }`），内部直接调用后端 service 层（无 HTTP 跨进程）。

详见 `docx/MCP_TEST_REPORT.md`。

---

## 错误码表

| code                   | HTTP | 说明                              |
|------------------------|------|-----------------------------------|
| `UNAUTHORIZED`         | 401  | Token 缺失或错误                  |
| `NOT_FOUND`            | 404  | 资源不存在                        |
| `INVALID_INPUT`        | 422  | 字段校验失败 (附 Pydantic details)|
| `CONFLICT`             | 409  | 唯一约束冲突 (如重复 PDF 哈希)    |
| `PARSER_ERROR`         | 422  | PDF 解析失败                      |
| `MARKET_DATA_ERROR`    | 502  | 上游行情接口异常                  |
| `INTERNAL_ERROR`       | 500  | 未分类异常                        |
