# API 设计文档

> Finance Tracker REST API — v1
> 基地址: `http://localhost:8000/api/v1`
> 鉴权: `Authorization: Bearer <FINANCE_TRACKER_API_TOKEN>` (除 `/health` 外全部要求)

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

### 分页

列表端点使用 cursor + limit:
- 请求: `?limit=50&cursor=<opaque>`
- 响应 `meta`: `{ "next_cursor": "...", "total": 1234 }`

### 时间/金额

- 时间统一 ISO-8601 UTC: `2026-05-01T10:30:00Z`
- 金额统一字符串格式以保留精度: `"1234.56789012"`
- 币种 ISO-4217 (大写): `"CNY"`、`"EUR"`、`"BTC"`

---

## 1. 健康检查

| Method | Path        | 说明                  | 鉴权 |
|--------|-------------|-----------------------|------|
| GET    | `/health`   | 服务存活检查          | 否   |
| GET    | `/version`  | 后端版本与构建信息    | 否   |

---

## 2. 账户 Accounts

| Method | Path                      | 说明                   |
|--------|---------------------------|------------------------|
| GET    | `/accounts`               | 列出所有账户           |
| POST   | `/accounts`               | 创建账户               |
| GET    | `/accounts/{id}`          | 单个账户详情           |
| PATCH  | `/accounts/{id}`          | 更新账户               |
| DELETE | `/accounts/{id}`          | 软删除账户             |
| GET    | `/accounts/{id}/balance`  | 当前余额 (来自 view)   |
| GET    | `/accounts/balances`      | 全账户余额一次返回     |

**创建请求示例**
```json
{
  "name": "招行储蓄卡",
  "type": "bank",
  "institution": "招商银行",
  "account_number": "**** 1234",
  "currency": "CNY",
  "initial_balance": "10000.00"
}
```

---

## 3. 分类 Categories

| Method | Path                  | 说明                        |
|--------|-----------------------|-----------------------------|
| GET    | `/categories`         | 列出全部 (支持 `?kind=`)    |
| POST   | `/categories`         | 创建分类                    |
| PATCH  | `/categories/{id}`    | 更新分类                    |
| DELETE | `/categories/{id}`    | 删除 (system 分类禁止)      |
| GET    | `/categories/tree`    | 返回带子分类的树形结构      |

---

## 4. 交易 Transactions

| Method | Path                              | 说明                          |
|--------|-----------------------------------|-------------------------------|
| GET    | `/transactions`                   | 列表 (过滤参数见下)           |
| POST   | `/transactions`                   | 手动录入交易                  |
| POST   | `/transactions/batch`             | 批量录入 (PDF 导入用)         |
| GET    | `/transactions/{id}`              | 单条详情                      |
| PATCH  | `/transactions/{id}`              | 更新 (常用于修改分类)         |
| DELETE | `/transactions/{id}`              | 软删除                        |
| POST   | `/transactions/{id}/categorize`   | 重新跑分类规则                |

**列表过滤参数**
- `account_id` / `category_id` / `type` (expense/income/transfer)
- `from_date` / `to_date` (YYYY-MM-DD)
- `min_amount` / `max_amount`
- `search` (在 description / counterparty 中模糊搜索)
- `tags` (CSV)
- `limit` / `cursor`

**列表项示例**
```json
{
  "id": 42,
  "occurred_at": "2026-04-28T19:30:00Z",
  "account": { "id": 1, "name": "招行储蓄卡" },
  "category": { "id": 5, "name": "餐饮" },
  "amount": "-128.50",
  "currency": "CNY",
  "base_amount": "-128.50",
  "type": "expense",
  "description": "海底捞",
  "counterparty": "海底捞火锅",
  "source": "pdf_import",
  "tags": ["朋友聚餐"]
}
```

---

## 5. PDF 账单导入 Statements

| Method | Path                              | 说明                                           |
|--------|-----------------------------------|------------------------------------------------|
| POST   | `/statements/upload`              | 上传 PDF (multipart/form-data, field=`file`)   |
| GET    | `/statements`                     | 历史导入批次列表                               |
| GET    | `/statements/{id}`                | 批次详情 (含已生成的 transactions)             |
| POST   | `/statements/{id}/reparse`        | 触发重新解析 (用于解析器升级后)                |
| POST   | `/statements/{id}/confirm`        | 确认入账 (将 pending 交易转为正式)             |
| DELETE | `/statements/{id}`                | 撤销整个批次的所有 transactions                |

**上传响应示例**
```json
{
  "id": 7,
  "filename": "cmb_2026_04.pdf",
  "detected_bank": "cmb",
  "transactions_count": 42,
  "status": "success",
  "preview": [ { "...": "前 5 条 transactions" } ]
}
```

---

## 6. 资产与持仓 Assets / Holdings

| Method | Path                              | 说明                                 |
|--------|-----------------------------------|--------------------------------------|
| GET    | `/assets`                         | 资产定义列表 (?asset_class=crypto)   |
| POST   | `/assets`                         | 新增资产定义                         |
| GET    | `/assets/{id}`                    | 资产详情 + 最新价                    |
| GET    | `/holdings`                       | 全部持仓 + 实时估值                  |
| POST   | `/holdings`                       | 新增持仓                             |
| PATCH  | `/holdings/{id}`                  | 更新数量 (链上余额同步可走此接口)    |
| DELETE | `/holdings/{id}`                  | 删除持仓                             |
| GET    | `/portfolio/summary`              | 总资产估值 (折算到基础币种)          |
| GET    | `/portfolio/timeseries`           | 历史净值曲线 (?range=1m\|3m\|1y\|all)|
| GET    | `/portfolio/breakdown`            | 按类别 / 币种饼图数据                |

**Portfolio Summary 响应**
```json
{
  "base_currency": "CNY",
  "total_value": "1234567.89",
  "as_of": "2026-05-01T10:30:00Z",
  "by_class": {
    "cash":     "100000.00",
    "a_share":  "300000.00",
    "eu_stock": "200000.00",
    "crypto":   "500000.00",
    "gold":     "134567.89"
  },
  "by_currency": {
    "CNY": "650000.00",
    "EUR": "300000.00",
    "USD": "284567.89"
  }
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

---

## 8. 现金流分析 Cash Flow

| Method | Path                              | 说明                                  |
|--------|-----------------------------------|---------------------------------------|
| GET    | `/cashflow/monthly`               | 按月聚合 (?from=2025-01&to=2026-04)   |
| GET    | `/cashflow/by-category`           | 按分类聚合 (?period=2026-04)          |
| GET    | `/cashflow/timeseries`            | 收入/支出/储蓄三线时间序列            |
| POST   | `/cashflow/recompute`             | 触发指定区间快照重算                  |

**月度聚合响应示例**
```json
[
  {
    "period": "2026-04",
    "income":   "25000.00",
    "expense":  "-12340.50",
    "transfer": "0.00",
    "savings":  "12659.50",
    "by_category": {
      "餐饮":   "-2300.00",
      "交通":   "-800.00",
      "工资":   "25000.00"
    }
  }
]
```

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

---

## 10. 系统/配置 System

| Method | Path                          | 说明                                 |
|--------|-------------------------------|--------------------------------------|
| GET    | `/settings`                   | 当前配置 (基础币种 / 行情源 / 排程)  |
| PATCH  | `/settings`                   | 更新配置                             |
| POST   | `/system/backup`              | 触发数据库备份                       |
| GET    | `/system/backups`             | 列出已有备份文件                     |

---

## 11. MCP Tools (非 HTTP, stdio)

由 `mcp-server/` 进程暴露,Agent 可调用以下 tool:

| Tool 名                       | 功能                                      |
|-------------------------------|-------------------------------------------|
| `query_balance`               | 查询账户余额                              |
| `query_transactions`          | 查询交易 (过滤条件传 JSON)                |
| `import_pdf_statement`        | 接收 PDF 字节流并触发解析+入库            |
| `query_asset_value`           | 查询单资产实时估值                        |
| `query_portfolio_summary`     | 查询总资产汇总                            |
| `categorize_transaction`      | 给一笔交易建议分类                        |
| `get_cashflow_summary`        | 获取月度现金流摘要                        |
| `add_transaction`             | Agent 代用户记一笔账                      |

每个 tool 返回 `{ "ok": true, "data": ... }` 或 `{ "ok": false, "error": "..." }`,内部直接调用后端 service 层 (无 HTTP 跨进程)。

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
