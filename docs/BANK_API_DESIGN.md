# Bank API Direct Connect — Design Document

> Finance Tracker P2: 自动银行交易同步
> 决策日期: 2026-05-02

---

## 1. PSD2 服务商对比

### 1.1 对比矩阵

| 维度 | GoCardless (Nordigen) | Tink | Fintoc |
|------|----------------------|------|--------|
| **定位** | Account Information Service Provider (AISP) | 全栈 Open Banking 平台 | 拉美/欧洲支付聚合 |
| **欧洲覆盖** | ✅ 全 EEA (PSD2)，2000+ 机构 | ✅ 全欧洲，2500+ 机构 | ❌ 拉美为主，欧洲极有限 |
| **中国银行** | ❌ 无 | ❌ 无 | ❌ 无 |
| **免费层** | ✅ 完全免费（Sandbox + 生产） | ❌ 需联系销售，无公开免费层 | ❌ 按交易收费 |
| **定价** | 免费（GoCardless 2022 收购 Nordigen 后开放） | Enterprise 定制 | 按量计费 |
| **沙盒** | ✅ Sandbox Finance 模拟银行 | ✅ 有沙盒 | ✅ 有沙盒 |
| **API 风格** | REST + Bearer Token | REST + OAuth 2.0 | REST + OAuth 2.0 |
| **历史交易** | 最长 24 个月 | 取决于银行 | 取决于银行 |
| **持续访问** | 最长 90 天（可续期） | 需要定期刷新 | 需要定期刷新 |
| **Python SDK** | 无官方 SDK（REST 足够简单） | 有官方 SDK | 有官方 SDK |
| **适合个人用户** | ✅ 非常适合 | ⚠️ 偏企业 | ❌ 不适合 |

### 1.2 结论

**推荐 GoCardless Bank Account Data (原 Nordigen)**

理由：
1. **完全免费** — 个人使用零成本，无需信用卡
2. **覆盖广泛** — 德国所有主要银行（N26, Sparkasse, DKB, Commerzbank, Deutsche Bank, ING, etc.）
3. **API 简洁** — REST + Bearer Token，无需复杂 OAuth 流程
4. **数据质量** — 交易数据结构符合 Berlin Group PSD2 标准
5. **活跃维护** — GoCardless 背书，持续更新

### 1.3 中国银行方案

中国银行 PSD2 接口不可用。可行方案：
- **主要方案**：PDF 账单导入（已实现的 PDF 解析引擎）
- **半自动方案**：银行 App CSV 导出 → 解析入库
- **长期观察**：关注中国 Open Banking 发展（如 CFCA、网联）

---

## 2. OAuth/SCA 授权流程

GoCardless Bank Account Data 的授权流程：

```
用户                    Finance Tracker           GoCardless           银行
 │                           │                       │                   │
 │  1. 选择银行              │                       │                   │
 │──────────────────────────>│                       │                   │
 │                           │  2. 创建 Requisition  │                   │
 │                           │──────────────────────>│                   │
 │                           │  3. 返回授权链接       │                   │
 │                           │<──────────────────────│                   │
 │  4. 重定向到授权链接       │                       │                   │
 │<──────────────────────────│                       │                   │
 │  5. GoCardless 托管同意页面│                       │                   │
 │──────────────────────────────────────────────────>│                   │
 │  6. 重定向到银行 SCA 认证  │                       │                   │
 │──────────────────────────────────────────────────────────────────────>│
 │  7. 用户在银行完成认证      │                       │                   │
 │<──────────────────────────────────────────────────────────────────────│
 │  8. 重定向回 redirect URL  │                       │                   │
 │──────────────────────────>│                       │                   │
 │                           │  9. 查询账户列表       │                   │
 │                           │──────────────────────>│                   │
 │                           │  10. 返回账户 ID       │                   │
 │                           │<──────────────────────│                   │
 │                           │  11. 拉取交易数据      │                   │
 │                           │──────────────────────>│                   │
 │                           │  12. 返回交易记录      │                   │
 │                           │<──────────────────────│                   │
 │  13. 显示同步结果          │                       │                   │
 │<──────────────────────────│                       │                   │
```

### 关键端点

| 步骤 | 方法 | 端点 | 说明 |
|------|------|------|------|
| Token | POST | `/api/v2/token/new/` | 用 secret_id + secret_key 获取 refresh token |
| Token | POST | `/api/v2/token/refresh/` | 用 refresh token 获取 access token |
| 银行列表 | GET | `/api/v2/institutions/?country={code}` | 获取国家支持的银行 |
| 创建协议 | POST | `/api/v2/agreements/enduser/` | 创建用户协议（可选，有默认值） |
| 创建连接 | POST | `/api/v2/requisitions/` | 创建授权请求 |
| 查询连接 | GET | `/api/v2/requisitions/{id}/` | 获取连接状态和账户列表 |
| 账户详情 | GET | `/api/v2/accounts/{id}/` | 获取账户信息 |
| 余额 | GET | `/api/v2/accounts/{id}/balances/` | 获取账户余额 |
| 交易 | GET | `/api/v2/accounts/{id}/transactions/` | 获取交易记录 |
| 删除连接 | DELETE | `/api/v2/requisitions/{id}/` | 撤销银行授权 |

---

## 3. 架构设计

### 3.1 模块结构

```
backend/app/
├── services/
│   └── bank_sync/
│       ├── __init__.py
│       ├── engine.py          # 核心同步引擎（调度、增量、去重）
│       ├── providers/
│       │   ├── __init__.py
│       │   ├── base.py        # 抽象 BankProvider 基类
│       │   └── gocardless.py  # GoCardless (Nordigen) 实现
│       └── crypto.py          # 凭证加密/解密工具
├── models/
│   └── bank_connection.py     # ORM 模型（新增）
├── api/v1/
│   └── bank_sync.py           # API 路由（新增）
└── schemas/
    └── bank_sync.py           # Pydantic 模型（新增）
```

### 3.2 数据库新增表

```sql
-- bank_connections: 银行连接管理
CREATE TABLE bank_connections (
    id                  INTEGER PRIMARY KEY AUTOINCREMENT,
    provider            TEXT NOT NULL,          -- gocardless | (future: tink, plaid)
    institution_id      TEXT NOT NULL,          -- 银行在服务商的 ID
    institution_name    TEXT NOT NULL,          -- 银行名称
    institution_bic     TEXT,                   -- BIC 码
    institution_country TEXT NOT NULL,          -- 国家代码 DE/GB/...
    institution_logo    TEXT,                   -- Logo URL
    
    -- GoCardless 专用字段
    gc_requisition_id   TEXT,                   -- GoCardless Requisition ID
    gc_agreement_id     TEXT,                   -- GoCardless Agreement ID
    gc_account_ids_json TEXT,                   -- JSON array: GoCardless account IDs
    
    -- 映射到本地账户
    account_id          INTEGER,               -- 关联的本地 accounts.id
    currency            TEXT NOT NULL,          -- 账户币种
    
    -- 同步状态
    status              TEXT NOT NULL DEFAULT 'pending',  -- pending | connecting | active | expired | error | revoked
    last_sync_at        TEXT,                   -- 最近一次成功同步时间
    last_sync_status    TEXT,                   -- success | error
    last_sync_error     TEXT,                   -- 最近一次同步错误信息
    next_sync_at        TEXT,                   -- 下次计划同步时间
    sync_interval_hours INTEGER NOT NULL DEFAULT 24,       -- 同步间隔（小时）
    total_transactions  INTEGER NOT NULL DEFAULT 0,
    
    -- 凭证（加密存储）
    encrypted_creds     TEXT,                   -- AES-256-GCM 加密的凭证 JSON
    
    -- 元数据
    metadata_json       TEXT,
    created_at          TEXT NOT NULL DEFAULT (datetime('now')),
    updated_at          TEXT NOT NULL DEFAULT (datetime('now')),
    deleted_at          TEXT,
    FOREIGN KEY (account_id) REFERENCES accounts(id) ON DELETE SET NULL,
    CHECK (provider IN ('gocardless')),
    CHECK (status IN ('pending','connecting','active','expired','error','revoked'))
);
CREATE INDEX idx_bank_conn_status ON bank_connections(status) WHERE deleted_at IS NULL;
CREATE INDEX idx_bank_conn_provider ON bank_connections(provider) WHERE deleted_at IS NULL;
```

### 3.3 凭证加密方案

```
加密方案: AES-256-GCM
密钥来源: FINANCE_BANK_ENCRYPTION_KEY 环境变量 (32 字节 hex)
存储格式: base64(nonce + ciphertext + tag)

加密内容 (GoCardless):
{
    "secret_id": "...",
    "secret_key": "...",
    "refresh_token": "..."
}
```

---

## 4. 同步机制

### 4.1 增量更新策略

1. **首次同步**：拉取银行允许的全部历史交易（最长 24 个月）
2. **增量同步**：每次只拉取 `last_sync_at` 之后的交易
3. **去重机制**：使用 `external_id`（银行交易 ID）+ `account_id` 联合唯一约束
4. **Rate Limit**：尊重银行和 GoCardless 的速率限制（4 次/天/账户）
5. **续期提醒**：在 `access_valid_for_days` 到期前 7 天触发续期通知

### 4.2 定时同步

使用 APScheduler（项目已有）：
- 默认每 24 小时同步一次活跃连接
- 可在连接级别配置间隔
- 同步失败自动退避（最多重试 3 次，间隔递增）

### 4.3 交易数据映射

GoCardless 交易 → Finance Tracker Transaction：

| GoCardless 字段 | Finance Tracker 字段 | 转换逻辑 |
|-----------------|---------------------|---------|
| transactionId | external_id | 直接映射 |
| bookingDate | occurred_at | ISO 日期 |
| valueDate | posted_at | ISO 日期 |
| transactionAmount.amount | amount | 直接映射（含正负） |
| transactionAmount.currency | currency | 直接映射 |
| remittanceInformationUnstructured | raw_description | 直接映射 |
| remittanceInformationUnstructured | description | 清洗后（截断、去多余空格） |
| debtorName / creditorName | counterparty | 取有值的那个 |
| bookingStatus | is_pending | "information" → true, "booked" → false |

---

## 5. API 端点设计

| 方法 | 路径 | 说明 |
|------|------|------|
| GET | `/bank-sync/providers` | 获取支持的银行列表（按国家） |
| GET | `/bank-sync/institutions` | 获取指定国家的银行列表 |
| POST | `/bank-sync/connections` | 创建银行连接（开始 OAuth 流程） |
| GET | `/bank-sync/connections` | 列出所有银行连接 |
| GET | `/bank-sync/connections/{id}` | 获取连接详情 |
| DELETE | `/bank-sync/connections/{id}` | 删除连接（撤销银行授权） |
| POST | `/bank-sync/connections/{id}/sync` | 手动触发同步 |
| POST | `/bank-sync/connections/{id}/reconnect` | 重新连接（重新授权） |
| GET | `/bank-sync/callback` | OAuth 回调端点 |
| POST | `/bank-sync/setup` | 配置 GoCardless API 凭证 |
| GET | `/bank-sync/status` | 获取同步服务状态 |

---

## 6. 安全性

1. **凭证加密**：AES-256-GCM 加密存储，密钥从环境变量读取
2. **传输安全**：所有 API 通信走 HTTPS
3. **访问令牌**：GoCardless access token 每日刷新，不持久化明文
4. **最小权限**：End User Agreement 只请求所需 scope（balances, details, transactions）
5. **撤销机制**：支持一键撤销银行授权（DELETE /requisitions）
6. **无凭证泄露**：GoCardless 不存储银行登录凭证，只存储 PSD2 授权 token
