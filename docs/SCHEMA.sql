-- =====================================================================
-- Finance Tracker — Database Schema (SQLite, WAL mode)
-- =====================================================================
-- 设计原则:
--   1. 所有金额: NUMERIC(20, 8) — 8 位小数支持加密货币
--   2. 所有币种: ISO-4217 三字母码 (CNY, EUR, USD, USDT 等加密视为伪币种)
--   3. 所有时间: TEXT ISO-8601 (UTC) "YYYY-MM-DDTHH:MM:SSZ"
--   4. 所有日期: TEXT "YYYY-MM-DD"
--   5. 软删除: deleted_at 列 (NULL = 活跃)
--   6. 审计: created_at / updated_at 自动维护
--   7. 外键: 强制启用 (PRAGMA foreign_keys=ON)
--
-- 此文件用于参考与文档化。实际表创建通过 Alembic 迁移脚本完成
-- (alembic/versions/0001_initial.py),保持 ORM 模型与迁移单源真相。
-- =====================================================================

-- ---------------------------------------------------------------------
-- accounts: 账户 (银行卡 / 钱包 / 证券账户 / 现金)
-- ---------------------------------------------------------------------
CREATE TABLE accounts (
    id              INTEGER PRIMARY KEY AUTOINCREMENT,
    name            TEXT NOT NULL,                     -- "招行储蓄卡", "Binance Spot"
    type            TEXT NOT NULL,                     -- bank | credit_card | brokerage | crypto_wallet | cash | other
    institution     TEXT,                              -- "招商银行", "Binance", "N26"
    account_number  TEXT,                              -- 后四位或别名 (隐私安全)
    currency        TEXT NOT NULL,                     -- 主币种 ISO-4217: CNY / EUR / USD / BTC ...
    initial_balance NUMERIC(20, 8) NOT NULL DEFAULT 0,
    is_active       INTEGER NOT NULL DEFAULT 1,        -- 0/1 boolean
    notes           TEXT,
    metadata_json   TEXT,                              -- JSON 任意扩展字段
    created_at      TEXT NOT NULL DEFAULT (datetime('now')),
    updated_at      TEXT NOT NULL DEFAULT (datetime('now')),
    deleted_at      TEXT,
    CHECK (type IN ('bank','credit_card','brokerage','crypto_wallet','cash','other'))
);
CREATE INDEX idx_accounts_active ON accounts(is_active) WHERE deleted_at IS NULL;

-- ---------------------------------------------------------------------
-- categories: 交易分类 (支出/收入分类树)
-- ---------------------------------------------------------------------
CREATE TABLE categories (
    id              INTEGER PRIMARY KEY AUTOINCREMENT,
    name            TEXT NOT NULL,                     -- "餐饮", "工资"
    kind            TEXT NOT NULL,                     -- expense | income | transfer
    parent_id       INTEGER,                           -- 支持二级分类 (餐饮 → 早餐)
    icon            TEXT,                              -- emoji 或 lucide 图标名
    color           TEXT,                              -- hex 色值 用于图表
    sort_order      INTEGER NOT NULL DEFAULT 0,
    is_system       INTEGER NOT NULL DEFAULT 0,        -- 系统预设分类不允许删除
    created_at      TEXT NOT NULL DEFAULT (datetime('now')),
    updated_at      TEXT NOT NULL DEFAULT (datetime('now')),
    FOREIGN KEY (parent_id) REFERENCES categories(id) ON DELETE SET NULL,
    CHECK (kind IN ('expense','income','transfer')),
    UNIQUE (name, kind, parent_id)
);
CREATE INDEX idx_categories_kind ON categories(kind);
CREATE INDEX idx_categories_parent ON categories(parent_id);

-- ---------------------------------------------------------------------
-- pdf_imports: PDF 上传批次 (用于追溯 / 重新解析)
-- ---------------------------------------------------------------------
CREATE TABLE pdf_imports (
    id                  INTEGER PRIMARY KEY AUTOINCREMENT,
    filename            TEXT NOT NULL,
    file_hash           TEXT NOT NULL UNIQUE,          -- SHA-256 用于去重
    file_size           INTEGER NOT NULL,
    storage_path        TEXT NOT NULL,                 -- ./data/pdfs/<hash>.pdf
    detected_bank       TEXT,                          -- icbc / cmb / n26 / unknown
    parser_version      TEXT,
    account_id          INTEGER,
    statement_period    TEXT,                          -- "2026-04" 或 "2026-04-01_2026-04-30"
    transactions_count  INTEGER NOT NULL DEFAULT 0,
    status              TEXT NOT NULL DEFAULT 'pending', -- pending | parsing | success | failed
    error_message       TEXT,
    raw_text            TEXT,
    metadata_json       TEXT,
    created_at          TEXT NOT NULL DEFAULT (datetime('now')),
    updated_at          TEXT NOT NULL DEFAULT (datetime('now')),
    FOREIGN KEY (account_id) REFERENCES accounts(id) ON DELETE SET NULL,
    CHECK (status IN ('pending','parsing','success','failed'))
);
CREATE INDEX idx_pdf_status ON pdf_imports(status, created_at DESC);

-- ---------------------------------------------------------------------
-- transactions: 交易流水 (支出 / 收入 / 转账)
-- ---------------------------------------------------------------------
CREATE TABLE transactions (
    id                  INTEGER PRIMARY KEY AUTOINCREMENT,
    account_id          INTEGER NOT NULL,
    counter_account_id  INTEGER,                       -- 转账对方账户
    category_id         INTEGER,
    occurred_at         TEXT NOT NULL,                 -- 交易发生时间 (ISO-8601)
    posted_at           TEXT,                          -- 入账时间 (信用卡场景)
    amount              NUMERIC(20, 8) NOT NULL,       -- 主币种金额 (支出负 / 收入正)
    currency            TEXT NOT NULL,
    fx_rate_to_base     NUMERIC(20, 8),                -- 折算到基准币种 (CNY) 的汇率快照
    base_amount         NUMERIC(20, 8),                -- = amount * fx_rate_to_base
    type                TEXT NOT NULL,                 -- expense | income | transfer | adjustment
    description         TEXT,                          -- 商户名 / 备注
    raw_description     TEXT,                          -- 原始账单文本 (未清洗)
    counterparty        TEXT,
    location            TEXT,
    tags_json           TEXT,                          -- JSON array ["差旅","报销"]
    source              TEXT NOT NULL DEFAULT 'manual',-- manual | pdf_import | bank_api | mcp_agent
    pdf_import_id       INTEGER,
    external_id         TEXT,                          -- 银行流水号 (去重用)
    is_pending          INTEGER NOT NULL DEFAULT 0,
    metadata_json       TEXT,
    created_at          TEXT NOT NULL DEFAULT (datetime('now')),
    updated_at          TEXT NOT NULL DEFAULT (datetime('now')),
    deleted_at          TEXT,
    FOREIGN KEY (account_id)         REFERENCES accounts(id)    ON DELETE RESTRICT,
    FOREIGN KEY (counter_account_id) REFERENCES accounts(id)    ON DELETE SET NULL,
    FOREIGN KEY (category_id)        REFERENCES categories(id)  ON DELETE SET NULL,
    FOREIGN KEY (pdf_import_id)      REFERENCES pdf_imports(id) ON DELETE SET NULL,
    CHECK (type IN ('expense','income','transfer','adjustment')),
    CHECK (source IN ('manual','pdf_import','bank_api','mcp_agent'))
);
CREATE INDEX idx_tx_occurred       ON transactions(occurred_at)            WHERE deleted_at IS NULL;
CREATE INDEX idx_tx_account_time   ON transactions(account_id, occurred_at) WHERE deleted_at IS NULL;
CREATE INDEX idx_tx_category_time  ON transactions(category_id, occurred_at) WHERE deleted_at IS NULL;
CREATE INDEX idx_tx_type           ON transactions(type, occurred_at)      WHERE deleted_at IS NULL;
CREATE UNIQUE INDEX idx_tx_external_dedup ON transactions(account_id, external_id)
    WHERE external_id IS NOT NULL AND deleted_at IS NULL;

-- ---------------------------------------------------------------------
-- assets: 资产定义 (一只股票 / 币种 / 黄金品类)
-- ---------------------------------------------------------------------
CREATE TABLE assets (
    id              INTEGER PRIMARY KEY AUTOINCREMENT,
    symbol          TEXT NOT NULL,                     -- BTC / 600519.SS / EUR / XAU
    name            TEXT NOT NULL,                     -- "贵州茅台", "Bitcoin"
    asset_class     TEXT NOT NULL,                     -- cash | a_share | eu_stock | us_stock | crypto | gold | bond | fund
    currency        TEXT NOT NULL,                     -- 计价币种
    market          TEXT,                              -- SSE / SZSE / NASDAQ / XETR / Binance
    data_source     TEXT,                              -- yfinance | coingecko | metals_api
    data_source_id  TEXT,                              -- bitcoin / 600519.SS (供应商内部 id)
    decimals        INTEGER NOT NULL DEFAULT 2,
    notes           TEXT,
    metadata_json   TEXT,
    created_at      TEXT NOT NULL DEFAULT (datetime('now')),
    updated_at      TEXT NOT NULL DEFAULT (datetime('now')),
    CHECK (asset_class IN ('cash','a_share','eu_stock','us_stock','crypto','gold','bond','fund','other')),
    UNIQUE (symbol, asset_class)
);
CREATE INDEX idx_assets_class ON assets(asset_class);

-- ---------------------------------------------------------------------
-- asset_holdings: 持仓 (账户 × 资产 → 数量)
-- ---------------------------------------------------------------------
-- 持仓数量可以从交易流水推导,但维护一份"快照式持仓表"用于:
--   1. 实时估值查询无需聚合全表
--   2. 加密钱包余额可直接同步链上数据写入,无对应 transaction
CREATE TABLE asset_holdings (
    id              INTEGER PRIMARY KEY AUTOINCREMENT,
    account_id      INTEGER NOT NULL,
    asset_id        INTEGER NOT NULL,
    quantity        NUMERIC(20, 8) NOT NULL DEFAULT 0,
    avg_cost        NUMERIC(20, 8),                    -- 平均买入成本
    cost_currency   TEXT,
    last_synced_at  TEXT,                              -- 最近一次链上/券商同步时间
    notes           TEXT,
    created_at      TEXT NOT NULL DEFAULT (datetime('now')),
    updated_at      TEXT NOT NULL DEFAULT (datetime('now')),
    FOREIGN KEY (account_id) REFERENCES accounts(id) ON DELETE CASCADE,
    FOREIGN KEY (asset_id)   REFERENCES assets(id)   ON DELETE RESTRICT,
    UNIQUE (account_id, asset_id)
);
CREATE INDEX idx_holdings_account ON asset_holdings(account_id);
CREATE INDEX idx_holdings_asset   ON asset_holdings(asset_id);

-- ---------------------------------------------------------------------
-- market_prices: 市场价格快照 (时间序列)
-- ---------------------------------------------------------------------
CREATE TABLE market_prices (
    id              INTEGER PRIMARY KEY AUTOINCREMENT,
    asset_id        INTEGER NOT NULL,
    quoted_at       TEXT NOT NULL,                     -- 价格时间戳 (ISO-8601)
    price           NUMERIC(20, 8) NOT NULL,
    currency        TEXT NOT NULL,
    source          TEXT NOT NULL,
    raw_payload     TEXT,
    created_at      TEXT NOT NULL DEFAULT (datetime('now')),
    FOREIGN KEY (asset_id) REFERENCES assets(id) ON DELETE CASCADE
);
CREATE INDEX idx_prices_asset_time ON market_prices(asset_id, quoted_at DESC);
CREATE UNIQUE INDEX idx_prices_dedup ON market_prices(asset_id, source, quoted_at);

-- ---------------------------------------------------------------------
-- fx_rates: 汇率时间序列 (基础币种 = CNY)
-- ---------------------------------------------------------------------
CREATE TABLE fx_rates (
    id              INTEGER PRIMARY KEY AUTOINCREMENT,
    base_currency   TEXT NOT NULL,                     -- 通常 CNY
    quote_currency  TEXT NOT NULL,                     -- EUR / USD / ...
    quoted_at       TEXT NOT NULL,
    rate            NUMERIC(20, 8) NOT NULL,           -- 1 base = rate quote
    source          TEXT NOT NULL,
    created_at      TEXT NOT NULL DEFAULT (datetime('now'))
);
CREATE INDEX idx_fx_pair_time ON fx_rates(base_currency, quote_currency, quoted_at DESC);
CREATE UNIQUE INDEX idx_fx_dedup ON fx_rates(base_currency, quote_currency, source, quoted_at);

-- ---------------------------------------------------------------------
-- cash_flow_snapshots: 月度现金流汇总 (派生 / 物化)
-- ---------------------------------------------------------------------
CREATE TABLE cash_flow_snapshots (
    id                  INTEGER PRIMARY KEY AUTOINCREMENT,
    period_year         INTEGER NOT NULL,              -- 2026
    period_month        INTEGER NOT NULL,              -- 1..12
    base_currency       TEXT NOT NULL DEFAULT 'CNY',
    income_total        NUMERIC(20, 8) NOT NULL DEFAULT 0,
    expense_total       NUMERIC(20, 8) NOT NULL DEFAULT 0,
    transfer_total      NUMERIC(20, 8) NOT NULL DEFAULT 0,
    savings_total       NUMERIC(20, 8) NOT NULL DEFAULT 0,    -- = income - expense
    other_total         NUMERIC(20, 8) NOT NULL DEFAULT 0,
    by_category_json    TEXT,                                  -- {"餐饮":-1234.5,...}
    by_account_json     TEXT,
    computed_at         TEXT NOT NULL DEFAULT (datetime('now')),
    UNIQUE (period_year, period_month, base_currency)
);
CREATE INDEX idx_cashflow_period ON cash_flow_snapshots(period_year DESC, period_month DESC);

-- ---------------------------------------------------------------------
-- categorization_rules: 分类规则 (关键字 → 分类)
-- ---------------------------------------------------------------------
CREATE TABLE categorization_rules (
    id              INTEGER PRIMARY KEY AUTOINCREMENT,
    pattern         TEXT NOT NULL,                     -- 子串 / 正则
    pattern_type    TEXT NOT NULL DEFAULT 'contains',  -- contains | regex | exact | starts_with
    field           TEXT NOT NULL DEFAULT 'description',
    category_id     INTEGER NOT NULL,
    priority        INTEGER NOT NULL DEFAULT 0,
    enabled         INTEGER NOT NULL DEFAULT 1,
    hit_count       INTEGER NOT NULL DEFAULT 0,
    created_at      TEXT NOT NULL DEFAULT (datetime('now')),
    updated_at      TEXT NOT NULL DEFAULT (datetime('now')),
    FOREIGN KEY (category_id) REFERENCES categories(id) ON DELETE CASCADE,
    CHECK (pattern_type IN ('contains','regex','exact','starts_with')),
    CHECK (field IN ('description','counterparty','raw_description'))
);
CREATE INDEX idx_rules_priority ON categorization_rules(priority DESC, enabled);

-- ---------------------------------------------------------------------
-- 视图: v_account_balance (账户实时余额, 派生)
-- ---------------------------------------------------------------------
CREATE VIEW v_account_balance AS
SELECT
    a.id              AS account_id,
    a.name            AS account_name,
    a.currency        AS currency,
    a.initial_balance + COALESCE(SUM(t.amount), 0) AS balance
FROM accounts a
LEFT JOIN transactions t
    ON t.account_id = a.id
    AND t.deleted_at IS NULL
WHERE a.deleted_at IS NULL
GROUP BY a.id;

-- =====================================================================
-- End of schema
-- =====================================================================
