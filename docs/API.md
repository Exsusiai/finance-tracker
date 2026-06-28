# API 设计文档

> Finance Tracker REST API — v1
> 基地址: `http://localhost:8010/api/v1` (本地开发默认端口)
> 鉴权: `Authorization: Bearer <FINANCE_TRACKER_API_TOKEN>` (除 `/health` 外全部要求；本地默认 `AUTH_DISABLED=true` 跳过——Sprint 2 FIX-9 加了 invariant：仅当 `BACKEND_HOST` 是 loopback 时才允许跳过鉴权)
> CORS: 允许列表由 `ALLOWED_ORIGINS` 控制（默认 `http://localhost:3000,http://localhost:3010,…`）
> 前端 token 注入：用户在 Settings → API Token 输入框手动粘贴（FIX-18 后不再走 `NEXT_PUBLIC_API_TOKEN` bundle 注入）
> 最后修订: 2026-06-25 (券商同步 + PDF 预览入库 + 转账端点补全)

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
| GET    | `/accounts`                       | 列出所有账户 (按 `sort_order`, `id` 排序)   |
| POST   | `/accounts`                       | 创建账户 (`sort_order` 自动置末)            |
| PATCH  | `/accounts/reorder`               | 手动拖动排序 (body: `account_ids` 全量有序列表) |
| GET    | `/accounts/{id}`                  | 单个账户详情                                |
| PATCH  | `/accounts/{id}`                  | 更新账户 (含 IBAN / 子账户清单)             |
| DELETE | `/accounts/{id}`                  | 软删除账户                                  |
| GET    | `/accounts/{id}/balance`          | 当前余额 (来自 `v_account_balance` 视图)    |
| GET    | `/accounts/balances`              | 全账户余额一次返回                          |
| POST   | `/accounts/{id}/adjust-balance`   | 余额校准（自动建一笔 `adjustment` 交易）    |
| POST   | `/accounts/{id}/anchor-balance`   | 锚定真实余额：按 `(balance, as_of)` 反推并设 `initial_balance`（不建交易、平移整条历史；快照账户拒绝） |

**`AccountOut` 新增字段** (P1-4):
- `include_in_total: bool` — 账户是否计入 net_worth / portfolio 汇总。`AccountCreate` 和 `AccountUpdate` 均接受此字段。

**`/accounts/balances` 与 `/accounts/{id}/balance`**：
- `crypto_wallet` / `exchange` 账户：余额 = `v_account_balance` + holdings × 最新 CoinGecko 价（USDT 路径）
- `brokerage` 账户：余额 = `v_account_balance` + `compute_brokerage_value_per_account`（持仓 × markPrice，折算到 `BASE_CURRENCY`，由 `services/valuation/fx.py::convert_to_base` 处理 EUR/USD→CNY）

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
  "include_in_total": true,
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

| Method | Path                                                              | 说明                                                         |
|--------|-------------------------------------------------------------------|--------------------------------------------------------------|
| GET    | `/transactions/transfers/unpaired`                                | 未配对单边转账列表（排除内部储蓄分类 + counter_account_hint 行） |
| GET    | `/transactions/transfers/{id}/counter-leg-candidates`             | 模糊匹配对手腿候选（仅其他账户、同币、金额±容差、日期±窗口）  |
| GET    | `/transactions/transfers/suggestions`                             | 自动评分的候选配对（评分 50–74，未自动配对）                 |
| POST   | `/transactions/{id}/mark-transfer`                                | 用户手动确认配对（body schema 见下）                         |
| POST   | `/transactions/{id}/unbind-counter`                               | 解绑对手（合成腿软删，真实腿清指针）                         |

**`GET /transfers/unpaired`** 过滤规则：`type='transfer'`、`counter_account_id IS NULL`、非子账户、`paired_with_tx_id IS NULL`、非 `counter_account_hint`（快照账户单边转账不再出现在此列表）、非内部储蓄分类。

**`GET /transfers/{id}/counter-leg-candidates`** 查询参数：
- `window_days` (int, 默认 10, 最大 30) — 日期窗口天数
- `amount_tolerance` (str 数字, 默认 `"0.01"`) — 允许金额差上限

每条候选返回字段：`transaction_id`, `account_id`, `account_name`, `occurred_at`, `amount`, `amount_diff`（带符号）, `currency`, `type`, `description`, `raw_description`, `days_diff`, `status`（`free` 或 `synthetic_bound`）。

**`mark-transfer` 请求 body**（`MarkTransferIn`）：
```json
{
  "counter_transaction_id": 42,       // 可选；给定时双腿配对
  "counter_account_id": 3,            // 可选；给定时合成镜像腿
  "transfer_direction": "out",        // "in" | "out"，单边/counter_account_id 场景必填
  "category_id": 7,                   // 可选；必须是 kind='transfer' 分类
  "amount_tolerance": "0.01"          // 可选；双腿配对时允许金额差
}
```
- `counter_transaction_id` 给定 → 双腿配对（`pair_transactions()`）；要求同币种、不同账户、金额在 tolerance 内
- `counter_account_id` 给定（无 counter_transaction_id） → 若对手账户是快照型（`brokerage`/`crypto_wallet`/`exchange`），**降级为单边**（存 `counter_account_hint`，不合成镜像，防持仓余额双算）；否则合成镜像腿
- 两者都不给 → 纯单边，`transfer_direction` 必填
- 缺 direction 且无法推断 → 422 INVALID_INPUT

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

> **2026-06 重要改造**：上传流程改为「预览后入库」。`POST /statements/upload` 现在**只解析不入库**，返回全量 `parsed_preview`，交易记录要到 `POST /statements/{id}/commit` 才真正写入账本。

| Method | Path                              | 说明                                                                              |
|--------|-----------------------------------|-----------------------------------------------------------------------------------|
| POST   | `/statements/upload`              | 上传并解析 PDF（只解析，不插交易）；落 `status='awaiting_review'`，返回全量 `parsed_preview` |
| GET    | `/statements`                     | 历史导入批次列表；支持 `?offset=N&limit=N&status=X`；响应 `meta.total`           |
| GET    | `/statements/{id}`                | 批次详情；`awaiting_review` 状态时**重解析**返回预览（不从 DB 读交易）            |
| POST   | `/statements/{id}/commit`         | 确认入库：重解析 PDF + 插入交易 + 跑 ingestion；`?account_id=N`（可选，缺省用上传时候选账户） |
| POST   | `/statements/{id}/confirm`        | 将已入库的 pending 交易翻为已确认（仅已分类行；未分类行留在 inbox）；向后兼容保留 |
| POST   | `/statements/{id}/reparse`        | 触发重新解析（用于解析器升级；需 account_id 已绑定）                              |
| DELETE | `/statements/{id}`                | 取消 / 撤销：暂存期删除 import 记录 + PDF 文件（无痕可重传）；已入库则软删所有交易 |

**工作流（新）**：
1. `POST /upload` → `status=awaiting_review`，响应携带 `parsed_preview`（全部解析结果）
2. 用户在 UI 确认预览 → `POST /{id}/commit?account_id=N` → 交易入库，`status=success`
3. 用户想取消 → `DELETE /{id}` → 记录和 PDF 文件一并删除，SHA-256 去重哈希释放，可重传

**`POST /statements/upload` 查询参数**：
- `account_id` (int, 可选) — 指定目标账户；省略时自动推断（唯一活跃账户 / 机构名匹配）
- `bank_format` (str, 可选) — 手动指定银行格式（`n26`/`revolut`/`tfbank`/`advanzia`/`amex_de`/其他）；省略则自动检测

**`PdfImportOut` 字段**：
- `parsed_preview: list[ParsedPreviewTx]` — 解析输出的全部行（`awaiting_review` 状态）；每行含 `occurred_at`, `amount`, `currency`, `type`, `description`
- `preview: list[TransactionOut]` — 已入库的交易（前 5 条，`success` 状态）
- `status`: `pending` | `parsing` | `awaiting_review` | `awaiting_account` | `success` | `failed`

**支持银行**：AMEX-DE / N26 / Revolut / TFBank / Advanzia（5 家欧洲银行）
- Revolut 使用 column-aware 解析器（按 Money out / Money in 列定位）
- 银行检测按最早出现位置，防止跨行互转账单误判
- 上传时 SHA-256 哈希去重（409 CONFLICT）

**上传响应示例（awaiting_review）**：
```json
{
  "id": 7,
  "filename": "statement_2026_04.pdf",
  "file_hash": "abc...",
  "file_size": 124567,
  "detected_bank": "n26",
  "parser_version": "n26-v1",
  "account_id": 1,
  "statement_period": "2026-04",
  "transactions_count": 42,
  "status": "awaiting_review",
  "parsed_preview": [
    { "occurred_at": "2026-04-01T00:00:00Z", "amount": "12.50", "currency": "EUR", "type": "expense", "description": "GROCERY STORE" }
  ],
  "preview": [],
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
| GET    | `/holdings/portfolio/value-history` | 月度组合市值快照序列（前向记录，无法回溯） |

**HoldingOut (单条持仓) 字段补充** — 2026-05-20 起：
- `price_currency`：最新 `market_prices` 行的 currency（钱包/CEX 同步写入的 crypto 通常是 `USDT`）；与 `cost_currency` 解耦
- `market_value_currency`：与 `price_currency` 同步；用于前端格式化「市值」列单位
- `market_value`：只要 `current_price` 存在就计算（`quantity × current_price`），不再要求与 `cost_currency` 一致
- `unrealized_pnl`：仍仅在 `cost_currency == price_currency` 时计算，否则 `null`

**Portfolio Summary 响应**

> **⚠️ 2026-05-19 shape 变更**：`by_currency` 的值从字符串金额改为嵌套对象，区分原币种值和折算后的 base 值。见 Sprint 4 FIX-22。

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
    "CNY": { "original_value": "650000.00", "base_value": "650000.00" },
    "EUR": { "original_value": "38000.00",  "base_value": "300000.00" },
    "USD": { "original_value": "40000.00",  "base_value": "284567.89" }
  }
}
```

**Portfolio Breakdown `by_currency` 响应形状** — 区分原币种值和折算到 base 后的值：
```json
{
  "by_currency": {
    "USDT": {
      "original_value": "1500.0",
      "base_value": "1380.0",
      "count": 5
    },
    "USD": { "original_value": "300", "base_value": "276", "count": 1 }
  }
}
```

**Net Worth 响应**

> **⚠️ 2026-05-19 shape 变更**：`investment_by_currency` 的值从字符串金额改为嵌套对象（同 Portfolio Summary），`cash_by_currency` 的键名也调整为 `original` / `converted`。见 Sprint 4 FIX-22。

```json
{
  "base_currency": "CNY",
  "cash_total": "350000.00",
  "investment_total": "884567.89",
  "net_worth": "1234567.89",
  "cash_by_currency": {
    "CNY": { "original": "120000.00", "converted": "120000.00" },
    "EUR": { "original": "30000.00",  "converted": "230000.00" }
  },
  "investment_by_currency": {
    "USD": { "original_value": "40000.00",  "base_value": "284567.89" },
    "CNY": { "original_value": "600000.00", "base_value": "600000.00" }
  },
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
| GET    | `/cashflow/by-category`           | 按分类聚合：单月 `?period=2026-04` 或区间汇总 `?from=2026-01&to=2026-05` |
| GET    | `/cashflow/timeseries`            | 收入/支出/储蓄 + 现金资产(`cash[]`,真实月末余额)时间序列 |
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
- `requires_llm: bool` — 命中该规则时是否仍路由到 LLM (L2)（`RuleOut` 新增字段，P1-1）

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
| POST   | `/bank-sync/institutions`                             | 列出支持的银行（body: `country` + `encrypted_credentials`；V7-P1-7 改 POST，凭据移出 query string）|
| POST   | `/bank-sync/connections`                              | 创建连接（body 含显式 `country`，返回 OAuth 跳转链接）|
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

## 12. 钱包同步 Wallet Sync (P1-4)

链上地址与 CEX 连接管理。地址类路由仅接受 `type=crypto_wallet` 账户，交易所连接路由仅接受 `type=exchange` 账户。

### 链上地址 Chain Addresses

| Method | Path                                              | 说明                                              |
|--------|---------------------------------------------------|---------------------------------------------------|
| GET    | `/accounts/{id}/addresses`                        | 列出该 crypto_wallet 账户的全部链上地址           |
| POST   | `/accounts/{id}/addresses`                        | 添加链上地址（链名 + 地址格式正则校验，409 防重） |
| DELETE | `/accounts/{id}/addresses/{addr_id}`              | 删除链上地址                                      |

**`POST /accounts/{id}/addresses` 请求**
```json
{ "chain": "ethereum", "address": "0xAbc...123", "label": "Cold Wallet" }
```
`chain` 必须是受支持的链之一 (`ethereum`, `arbitrum`, `optimism`, `base`, `polygon`, `polygon-zkevm`, `zksync`, `linea`, `scroll`, `mantle`, `blast`, `bitcoin`, `solana`, `tron`)；地址格式按链名正则校验（见 `backend/app/schemas/__init__.py::_ADDRESS_PATTERNS`）。

**`ChainAddressOut` 字段**：`id`, `chain`, `address`, `label`, `last_synced_at`, `last_sync_status`, `last_sync_error`。

### 交易所连接 Exchange Connection

| Method | Path                                              | 说明                                                         |
|--------|---------------------------------------------------|--------------------------------------------------------------|
| GET    | `/accounts/{id}/exchange-connection`              | 获取连接（未设时 `data=null`）                               |
| PUT    | `/accounts/{id}/exchange-connection`              | 创建或覆盖凭证（upsert）；secrets 加密入库，响应不回显      |
| DELETE | `/accounts/{id}/exchange-connection`              | 删除连接                                                     |

**`PUT /accounts/{id}/exchange-connection` 请求**
```json
{ "exchange": "binance", "api_key": "...", "api_secret": "...", "passphrase": null }
```
- `exchange`: `"binance"` 或 `"bitget"`（Bitget 必须提供 `passphrase`）
- 凭证以 AES-256-GCM 加密（`FINANCE_BANK_ENCRYPTION_KEY`）后存入 `exchange_connections` 表
- 未配置 `FINANCE_BANK_ENCRYPTION_KEY` 时返回 400

**`ExchangeConnectionOut` 字段**：`id`, `exchange`, `has_credentials` (bool), `has_passphrase` (bool), `last_synced_at`, `last_sync_status`, `last_sync_error`。密钥本身从不出现在任何响应中。

### 同步触发 Sync

| Method | Path                              | 说明                                                            |
|--------|-----------------------------------|-----------------------------------------------------------------|
| POST   | `/accounts/{id}/sync`             | 阻塞式触发链上/CEX 余额同步，返回 `SyncSummaryOut`            |

**`SyncSummaryOut` 字段**：`account_id`, `account_type`, `total_synced`, `total_errors`, `results: [SyncResultOut]`。`SyncResultOut` 含 `label`, `chain`, `exchange`, `synced`, `error`。

> 同步在请求生命周期内完成（无后台队列）。个人钱包 1-3 个地址通常数秒内结束。

---

## 13. 券商同步 Broker Sync (IBKR Flex + Trade Republic)

券商账户（`type=brokerage`）持仓快照与估值。与 crypto/exchange 同表 `asset_holdings`，共用 `POST /accounts/{id}/sync` orchestrator。Flex token / TR cookie session 以 AES-256-GCM 加密存入 `broker_connections`（需 `FINANCE_BANK_ENCRYPTION_KEY`）；secrets 绝不回显。

### IBKR Flex 连接（静态 token）

| Method | Path                                   | 说明                                                      |
|--------|----------------------------------------|-----------------------------------------------------------|
| GET    | `/accounts/{id}/broker-connection`     | 获取连接状态（未设时 `data=null`；`credentials_stale=true` 表示加密密钥已轮换需重填）|
| PUT    | `/accounts/{id}/broker-connection`     | IBKR：创建或覆盖（body: `provider`+`token`+`query_id`；token 加密入库）|
| DELETE | `/accounts/{id}/broker-connection`     | 删除连接                                                  |

**`PUT /accounts/{id}/broker-connection` 请求**（`BrokerConnectionIn`）：
```json
{ "provider": "ibkr", "token": "<Flex Web Service token>", "query_id": "<数字 Query ID>" }
```
- `provider` 当前仅支持 `"ibkr"`（TR 走下方专属端点）
- `query_id` 为 IBKR Client Portal 创建的 Activity Flex Query ID（只需勾选 Open Positions section，Format=XML）
- 未配置 `FINANCE_BANK_ENCRYPTION_KEY` 时返回 400

**`BrokerConnectionOut` 字段**：`id`, `provider`, `query_id`（TR 为 `null`）, `has_token` (bool), `credentials_stale` (bool), `last_synced_at`, `last_sync_status`, `last_sync_error`。

> IBKR 为**收盘快照（EOD）**：`markPrice` 原币写入 `market_prices(source='ibkr')`，不调 CoinGecko。

### Trade Republic 两步登录（交互式）

TR 无官方 API，使用社区逆向库 `pytr`（仅只读）。登录需两步：

| Method | Path                                                    | 说明                                                                  |
|--------|---------------------------------------------------------|-----------------------------------------------------------------------|
| POST   | `/accounts/{id}/broker-connection/tr/connect`           | 第一步：手机号 + PIN → TR 发送 4 位验证码；进程内暂存 pending session |
| POST   | `/accounts/{id}/broker-connection/tr/verify`            | 第二步：4 位验证码 → 加密 cookie session 写入 `broker_connections`    |

**`tr/connect` 请求**（`TRConnectIn`）：
```json
{ "phone": "+4917612345678", "pin": "1234" }
```
- 手机号自动格式化（去空格/连字符，`00→+`）
- 成功响应（`TRConnectOut`）：`{ "countdown_seconds": 120, "message": "验证码已发送..." }`
- WAF 防护：内部走 Playwright（无头 Chromium），需在服务器提前执行 `python -m playwright install chromium`

**`tr/verify` 请求**（`TRVerifyIn`）：
```json
{ "code": "1234" }
```
- 成功后返回 `BrokerConnectionOut`（`provider="traderepublic"`, `query_id=null`）
- Session 过期需重新调用 `tr/connect` + `tr/verify`（不支持自动续期）

### 同步触发（共用）

`POST /accounts/{id}/sync` 对 `brokerage` 类型账户也适用（orchestrator 按 `provider` dispatch：IBKR 走 Flex token，TR 走 cookie session）。同步完成后持仓写入 `asset_holdings`，价格写入 `market_prices`。

> `GET /accounts/balances` 对 brokerage 账户返回 `compute_brokerage_value_per_account` 折算到 `BASE_CURRENCY` 的估值（EUR/USD→CNY via `services/valuation/fx.py`）。

---

## 14. LLM 智能分类 LLM Classification (P1-1)

运行时配置存于 `app_settings` KV 表，不需要重启进程即可调整。

| Method | Path                          | 说明                                          |
|--------|-------------------------------|-----------------------------------------------|
| GET    | `/llm/settings`               | 获取当前 LLM 配置                             |
| PUT    | `/llm/settings`               | 更新配置（含 `gemini_api_key` 写入加密存储）  |
| GET    | `/llm/cost`                   | 本月累计消耗 (USD) vs 预算                    |

**`GET /llm/settings` 响应 (`LLMSettingsOut`)**
```json
{
  "enabled": true,
  "provider": "gemini",
  "model": "gemini-2.0-flash",
  "monthly_usd_budget": 5.0,
  "confidence_threshold": 0.7,
  "use_grounding": false,
  "max_notes_in_prompt": 10,
  "api_key_present": true
}
```
`api_key_present` 是布尔值，密钥本身从不出现在响应中。

**`PUT /llm/settings` 可接受字段** (`LLMSettingsUpdate`)：`enabled`, `model`, `monthly_usd_budget`, `confidence_threshold`, `use_grounding`, `max_notes_in_prompt`, `gemini_api_key`（write-only；空字符串清除已存密钥）。

**`GET /llm/cost` 响应 (`LLMCostOut`)**：`used_usd`, `budget_usd`, `remaining_usd`, `period` (`"YYYY-MM"`)。

---

## 15. 分类知识库 Categorization Notes (P1-1)

供 LLM 管道使用的 few-shot 上下文条目。大多数条目由 inbox confirm + `user_note` 自动创建，也可在 Settings → 知识库 UI 直接编辑。

| Method | Path                              | 说明                                    |
|--------|-----------------------------------|-----------------------------------------|
| GET    | `/categorization-notes`           | 列表 (?category_id=N&enabled=true/false)|
| POST   | `/categorization-notes`           | 创建条目                                |
| PATCH  | `/categorization-notes/{id}`      | 更新条目                                |
| DELETE | `/categorization-notes/{id}`      | 硬删除                                  |

**`NoteOut` 字段**：`id`, `category_id`, `category_name`, `trigger_text`, `note_text`, `source_transaction_id`, `usage_count`, `enabled`, `created_at`, `updated_at`。

---

## 16. Notion 同步 Notion Sync (P2-3)

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

## 17. MCP Tools (非 HTTP, stdio)

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
