# Finance Tracker — 开发任务优先级 ROADMAP

> 修订日期: 2026-05-06
> 来源: `docx/PRD.md`、`docx/REQUIREMENT_GAP.md`、`code review/review V1.md`
> 排序原则: ① 现行资金正确性 > ② 公开发布前安全 > ③ PRD 明文功能 > ④ 工程化债务

---

## ✅ Sprint 0 — R0 紧急资金正确性修复（已完成 2026-05-06）

> 来源：`code review/review V1.md` 实地验证。这些 bug **当前每次操作都在产生错数据**，必须先修。

| # | 任务 | 来源 | 状态 |
|---|---|---|---|
| **FIX-1** | `mark-transfer` 持久化 `transfer_direction`（counter→`pair_transactions`，单边→`metadata.transfer_direction`）；前端 dialog/suggestions panel 把 `direction` 真正发送；新加 `MarkTransferIn` schema + `InvalidInputError`；5/5 backend tests pass | review P0-3 | ✅ |
| **FIX-2** | Savings 公式 4 处统一为 `ABS(income) − ABS(expense)`（cashflow/engine.py + cashflow.py 三处 + mcp server.py） | review P1-1 | ✅ |
| **FIX-3** | cashflow SQL 用 `COALESCE(base_amount, amount * fx_rate_to_base, amount)` 折算；`CashFlowMonthly` 响应加 `base_currency`；前端 CategoryBreakdownView 去掉硬编码 EUR + 改读 displayCurrency + 优先使用 `base_amount` | review P1-2 | ✅ |

**验收（已通过）**：
- ✅ 5/5 mark-transfer 测试：单边 in / 单边 out / 单边缺 direction→422 / 双边配对余额一致 / 跨月配对 cashflow 重算
- ✅ smoke test：3000 income(CNY) + 1000 expense(CNY) + 100 EUR(base=800) expense + 50 USD(fx=7.2) income → income=3360 expense=1800 **savings=1560** ✓

---

## ✅ Sprint 1 — R1 数据一致性 + 测试基础设施（已完成 2026-05-06）

| # | 任务 | 来源 | 状态 |
|---|---|---|---|
| **FIX-4** | 新增 `services/ingestion/`，upload / reparse / batch / bank_sync 共用统一管道：amount normalize（非 adjustment ABS）→ categorize → transfer match → recompute affected periods。MCP 用 sync mirror（共享 SQL 公式）。reparse 现在会保留 metadata、跑分类/matcher、重算。delete 也重算。| review P1-3, P1-5, P1-7 | ✅ |
| **FIX-5** | `_validate_kind_match` 在 transactions create/update + inbox confirm 强制；categories create 校验 parent 存在 + kind 一致 | review P1-4 | ✅ |
| **FIX-6** | Transaction 加 3 个 Index（account+occurred / category / pdf_import）+ partial unique `(account_id, external_id) WHERE deleted_at IS NULL AND external_id IS NOT NULL`；lifespan 用 CREATE INDEX IF NOT EXISTS 兜住 | review P1-6 | ✅ |
| **FIX-7** | pytest 已装；test_pdf_parser 重写（5 家真实 PDF 解析全过 + 检测器测试）；test_api 改为 integration（无服务器自动 skip）；新增 test_ingestion + test_kind_invariant + test_index_invariants | review P3-2 | ✅ |

**测试结果**：`pytest backend/tests/ -v` → **27 passed, 15 skipped**
- 5/5 mark-transfer
- 4/4 ingestion 管道（amount normalize / adjustment 保留符号 / 规则命中自动通过 / 跨月重算）
- 6/6 kind invariant（mismatch→422 / match→OK / patch flip→422 / parent kind / unknown parent / nested ok）
- 5/5 index invariants（4 个索引存在 / 重复 external_id 同账户失败 / 跨账户允许 / 软删可复用 / NULL 不冲突）
- 5/5 PDF parser（5 家欧洲银行真实 PDF round-trip）
- 2/2 detector + import smoke
- 15 integration（server 未起跳过）

---

## ✅ Sprint 2 — R2 公开 GitHub 前安全加固（已完成 2026-05-06）

| # | 任务 | 来源 | 状态 |
|---|---|---|---|
| **FIX-8** | Notion router 加 `dependencies=[Depends(require_auth)]`（一次覆盖 6 个 endpoint）+ test_notion_auth.py 5 个用例 | review P0-1 | ✅ |
| **FIX-9** | 默认 `BACKEND_HOST=127.0.0.1`；CORS 改用 `ALLOWED_ORIGINS` 列表配置（默认本机 3000/3010）+ 收紧 methods/headers；lifespan 在 AUTH_DISABLED+非 loopback 时 RuntimeError 拒启 | review P0-2 | ✅ |
| **FIX-10** | PDF upload `MAX_PDF_SIZE_MB=10` + 校验 `%PDF-` magic bytes，违例 ParserError 422 | review P2-1 | ✅ |
| **FIX-11** | `validate_regex_complexity` 写入时校验（长度 ≤200 + 嵌套量词检测 + 编译验证）；`_safe_regex_search` 用线程池+1s timeout 兜运行时（GIL 限制：写入时校验是主防线） | review P2-8 | ✅ |
| **FIX-12** | docs/SCHEMA.sql v_account_balance 同步实际公式；删 valuation/engine.py 死 helper；transactions list 抽 `_apply_filters` 共享 11 个过滤条件给 data+count；layout.tsx 改用 next/script + JSON.stringify 注入 token | review P2-5/6/7, P3-1 | ✅ |

**测试结果**：`pytest backend/tests/ -v --ignore=test_api.py` → **53 passed**
- 5/5 mark-transfer (FIX-1)
- 4/4 ingestion (FIX-4)
- 6/6 kind invariant (FIX-5)
- 5/5 index invariants (FIX-6)
- 7/7 PDF parser real-PDF + smoke (FIX-7)
- 5/5 notion auth (FIX-8)
- 10/10 security invariants (FIX-9)
- 11/11 regex safety + PDF guards (FIX-10/11)

> ✅ R0+R1+R2 全部落地，无致命 bug，可以 push 到公开 GitHub。

---

## ✅ Sprint 3 — 把 V1 partial 修完（已完成 2026-05-06）

> 来源：`code review/review V2.md` 复核。V1 21 项里 V2 标记的 6 个 partial 全部闭合。

| # | 任务 | 来源 | 状态 |
|---|---|---|---|
| **FIX-13** | ingestion Step 1.5：`resolve_fx_to_base` + 写 `base_amount/fx_rate_to_base`；缺汇率标 `metadata.fx_missing=true` | V2 §V2-P0-1 / V1 P1-2 | ✅ |
| **FIX-14** | categorize_transaction + apply_to_similar_pending + /rules/apply-all 全部加 `Category.kind == tx.type` 守卫；rules create/update 校验 category 存在 | V2 §V2-P0-2 / V1 P1-4 | ✅ |
| **FIX-15** | transactions PATCH 和 inbox confirm 加 ABS（非 adjustment）；MCP `parse_bank_statement` 重写：完整 amount normalize + categorize + kind guard + recompute_period_sync | V2 §V2-P0-3 / V1 P1-5 | ✅ |
| **FIX-16** | rules.py `_match_rule` 改用 categorizer 的 `_safe_regex_search`；/rules/test 和 /rules/apply-all 共用 | V2 §V2-P1-4 / V1 P2-8 | ✅ |
| **FIX-17** | Settings.resolved_database_url：把 `sqlite:///./xxx` 锚到 `_PROJECT_ROOT`，cwd 不敏感 | V2 §V2-P2-3 / V1 P3-2 | ✅ |
| **FIX-18** | 删除 layout.tsx 内 NEXT_PUBLIC_API_TOKEN 注入；settings 页加 ApiTokenInput；统一 NEXT_PUBLIC_API_URL（删除 _BASE_URL）| V2 §V2-P2-4 + §V2-P2-2 / V1 P2-7 | ✅ |

**测试结果**：`pytest backend/tests/ --ignore=test_api.py` → **67 passed**（+14 from Sprint 2 baseline 53）

V1 21 项问题修复进度：
- ✅ 完全修复：**17 项**（R0+R1+R2 全部 + V1 6 个 partial 全部闭合）
- ❌ 未修：**4 项**（R3，等启用 GoCardless / Notion 时一并修）：P1-8, P2-2, P2-3, P2-4

---

## ✅ Sprint 4 — review V3 派生：当前 P0/P1 + 安全（已完成 2026-05-06）

> 来源：`code review/review V3.md` 实地核查后属实的 14 项问题中，按用户范围（"目前正在关注的 P0/P1 + 安全"）筛出 7 项。半成品（GoCardless / Notion / bank encryption key 配置链）和 UX 优化（持仓跨币种市值、账户删除一致性）按用户约定推迟到对应子系统启用时一起修。
> **测试**：`pytest backend/tests/ --ignore=test_api.py` → **75 passed** (+8 from 67)。

| # | 任务 | 来源 | 状态 |
|---|---|---|---|
| **FIX-19** | base_amount 生命周期：cashflow SQL CASE 表达式（外币缺 FX → NULL 被 SUM 跳过）+ `fx_missing_count`；PATCH/inbox 清空 `base_amount` + `fx_rate_to_base` 后调 `ingest_transactions` 重折算 | V3-P0-1 + V3-P0-2 | ✅ |
| **FIX-20** | MCP 完整 mirror：`add_transaction` 用 `_convert_fx` 写 base_amount/fx_rate_to_base；`parse_bank_statement` 加 PDF size/magic guard + ThreadPool timeout regex + 同账户 amount-match 镜像 | V3-P1-1 | ✅ |
| **FIX-21** | `/cashflow/recompute` 改 `from`/`to` (YYYY-MM) period 字符串比较，跨年范围正确；旧 `from_year/from_month` 仍兼容 | V3-P1-4 | ✅ |
| **FIX-22** | 资产/净值 by_currency 拆 `{original_value, base_value}` (PortfolioSummary/Breakdown/NetWorth)；MCP `get_total_assets` 缺 FX 不计入 base total，返回 `fx_missing_cash` | V3-P1-5 + V3-P1-8 | ✅ |
| **FIX-23** | TransactionCreate/Update.metadata_json `field_validator` 强制 JSON object；v_account_balance SQL 加 `json_valid()` 防护 | V3-P1-6 | ✅ |
| **FIX-24** | apply_to_similar_pending 改 `or_(category_id.is_(None), category_id != X)`，未分类 pending sibling 现在也能被级联 | V3-P2-1 | ✅ |
| **FIX-25** | IntegrityError handler 不再回传 `str(exc.orig)`，原始 db 错误只写日志 | V3-P3-1 | ✅ |

按用户约定**跳过**（启用对应子系统时一起修）：
- V3-P1-2 GoCardless query/country bug → 启用 GoCardless 时
- V3-P1-3 Notion 资产摘要旧公式 → 启用 Notion 时
- V3-P1-7 bank encryption key 走 Settings → 启用 GoCardless 时
- V3-P2-2 持仓详情跨币种市值 → 持仓 UX 优化阶段
- V3-P2-3 账户删除一致性 → 账户管理 UX 阶段

---

## Sprint 5+ — R3 子系统启用前修（按需触发）

| 启用项 | 必须先修 | 来源 |
|---|---|---|
| **GoCardless**（原 P1-2） | bank_sync 复用统一 ingestion；凭据走 body 不走 query；country 字段独立修复；bank encryption key 走 Settings | V1 P1-7, P2-2, P2-3 / V3-P1-2, P1-7 |
| **Notion**（原 P1-3） | 资产摘要改读 `v_account_balance` | V1 P1-8 / V3-P1-3 |

---

## ✅ P0 — 全部完成

| # | 任务 | 状态 | Commit |
|---|------|------|--------|
| **P0-1** | 市场价格定时刷新（APScheduler） | ✅ 2026-05-04 | `7b0916d` |
| **P0-2** | CashFlow snapshot 自动重算 | ✅ 2026-05-04 | `7b0916d` |
| **P0-3** | 分类自动学习 / 记忆 | ✅ 2026-05-04 | `7b0916d` |
| **P0-4** | 「待确认」收件箱工作流 | ✅ 2026-05-04 | `7b0916d` |
| **P0-5** | MCP 端到端真实集成测试（6 轮回归 9 bug 修复） | ✅ 2026-05-04 | `7b0916d` |
| **P0-7** | 记账页层级化分类视图（CategoryBreakdownView） | ✅ 2026-05-04 | `7b0916d` |
| **P0-8** | 跨账户转账识别（transfer_matcher） | ✅ 2026-05-05 | `7dae743` |
| **P0-9** | 子账户 L1+L2+L3 识别（关键词 / 用户清单 / amount-match） | ✅ 2026-05-05 | `8bb46a1` |
| **P0-10** | IBAN 字段 + 内部转账识别 + 续行 IBAN 提取 | ✅ 2026-05-05 | `7008c57` |
| **P0-11** | inbox 自动通过 + 同描述级联学习 | ✅ 2026-05-05 | `c41a757` |
| **P0-12** | 余额校准 UX（"调整余额"对话框 + 三模式） | ✅ 2026-05-05 | `7008c57` |
| **P0-13** | 内联分类编辑 + 跨 kind 切换 | ✅ 2026-05-05 | `69d4fc0`+`ad84112` |

---

## P1 — 进行中 / 待启动

> **2026-05-04 重排**：用户提出"分类管道升级"为最高 P1，原 GoCardless / Notion 顺延

| # | 任务 | 状态 | 依赖/前置 | 验收标准 |
|---|---|---|---|---|
| **P1-1a** | LLM 分类 fallback 基础版 | ✅ 2026-05-08 | — | Gemini provider；L1 miss / requires_llm → L2 LLM；置信度阈值 settings 表可调 |
| **P1-1b** | 用户备注体系 | ✅ 2026-05-04 | — | `transactions.user_note` 字段；inbox 行内 textarea |
| **P1-1c** | 知识库注入 LLM | ✅ 2026-05-08 | — | LLM prompt 自动注入相关 categorization_notes 作 few-shot |
| **P1-1d** | 知识库管理 UI | ✅ 2026-05-08 | — | Settings 页 CategorizationNotesTable + 自动从用户备注沉淀 |
| **P1-2** | GoCardless 银行直连联调 | ⏸️ scaffold | 用户决策 + GoCardless 账号 | N26 / Revolut 真实账户连接，每日同步交易入库 |
| **P1-3** | Notion 同步联调 + 形态决策 | ⏸️ scaffold | 用户提供 integration token + 决定库结构 | `POST /notion/setup` 一键建库；transactions / cashflow / assets 三模块每日同步成功 |
| **P1-4** | **链上加密钱包 + CEX API**（多链多地址聚合 / Binance / Bitget / 价格自动发现 / include_in_total） | ✅ 2026-05-18~19 | — | 11 EVM 链 + BTC + Solana + Tron；Binance 现货；Bitget 现货 + 三套合约钱包；CoinGecko 价格自动；总值含加密；包含「不计入总资产」开关。详见 `docx/CRYPTO_WALLET_PLAN.md` |
| **P1-4-ext** | 持仓表 UI 加列（数量 / 当前价 / 市值 / 成本价） | ❌ 待启动 | — | 用户已提需求；成本价手工录入（链上拿不到买入价上下文） |
| **P1-4-ext** | Binance 合约钱包（USDT-M + 币本位） | ❌ 待启动 | — | 与 Bitget 同 pattern；半天可完成 |
| **P1-5** | "储蓄"口径定义 + 实现 | ❌ 待启动 | PRD 二次澄清 | `cash_flow_snapshots.savings_total` 计算口径文档化、有单测 |

---

## P2 — 工程化与扩展覆盖

| # | 任务 | 备注 |
|---|---|---|
| **P2-3** | 扫描件 OCR 兜底（pdf2image + tesseract） | 当前 5 家 PDF 都是文本型，未阻塞 |
| **P2-4** | Alembic 真迁移版本化 | 当前用 `_column_migrations` idempotent ALTER 顶住 |
| **P2-5** | Dockerfile 真实可构建 + 部署文档 | docker-compose 在但 Dockerfile 占位 |
| **P2-6** | E2E Playwright 测试（覆盖核心用户流） | 当前手测 + agent E2E |
| **P2-7** | 后端 CI（ruff + mypy + pytest GitHub Actions） | |
| **P2-8** | 黄金 GoldAPI 接入 | 需用户申请 key |
| **P2-9** | 生产模式 Bearer 启用 + 前端登录页 | 当前 `AUTH_DISABLED=true` |

---

## P3 — 后续 / PRD 明示推迟

- 移动端 App
- Notion 反向 / 双向同步（视 P1-3 决策）
- 投资分析 / 财务规划顾问类（明确**非目标**）

---

---

## ✅ Sprint 6 — PDF 预览后入库 + 转账口径修正（2026-06）

| # | 任务 | 状态 |
|---|---|---|
| **FIX-26** | PDF 导入改为「预览后入库」：`POST /upload` 只解析不入库，落 `status='awaiting_review'`，返回全量 `parsed_preview`；新增 `POST /statements/{id}/commit?account_id=` 才真正插入 + ingestion；`DELETE` 取消连带删除 PDF 文件（无痕可重传）；`GET /statements/{id}` 对 `awaiting_*` 状态重解析出预览 | ✅ 2026-06 |
| **FIX-27** | `GET /statements` 列表加 `offset` 参数 + 响应 `meta.total`（支持「加载更多」） | ✅ 2026-06 |
| **FIX-28** | 上传支持 `bank_format` 查询参数手动指定银行；`PdfImportStatus` 新增 `awaiting_review`（lifespan 幂等重建 CHECK）；移除「改银行重新解析」功能（冗余） | ✅ 2026-06 |
| **FIX-29** | 银行检测改为「按最早出现位置」：`_detect_bank` 取 `_BANK_MARKERS` 中在文中出现位置最靠前的银行，防止 N26↔Revolut 互转账单交叉误判（发行行标识在头部，对方 BIC 只在正文转账行） | ✅ 2026-06 |
| **FIX-30** | 转账方向推断：PATCH/mark-transfer 翻 transfer 时若无 direction 自动从旧 type 推断（income→in，else→out） | ✅ 2026-06 |
| **FIX-31** | 快照账户单边转账（brokerage/crypto_wallet/exchange 作对手）降级为单边不合成镜像腿，防持仓余额双算；`/transfers/unpaired` 排除 `counter_account_hint` 行 | ✅ 2026-06 |
| **FIX-32** | 内部储蓄/利息分类守卫：`/transfers/unpaired` 按 category_id 排除内部储蓄类别（`kind=subaccount`）；利息/手续费不被子账户扫描误判 | ✅ 2026-06 |
| **FIX-33** | 新增 `GET /transfers/{id}/counter-leg-candidates` 和 `POST /{id}/unbind-counter` 端点 | ✅ 2026-06 |

---

## ✅ Sprint 7 — IBKR Flex 券商同步（2026-06-10 实装）

| # | 任务 | 状态 |
|---|---|---|
| **P1-5a** | 新建 `broker_connections` 表（provider/token_enc/query_id/last_sync_*，唯一 `(account_id, provider)`）+ alembic migration `a1b2c3d4e5f6` | ✅ |
| **P1-5b** | `services/broker_sync/ibkr.py`：IBKRFlexProvider，两步下载（async httpx），错误码重试；`services/broker_sync/upsert.py`：apply_broker_snapshot；asset_class 映射（STK/FUND/ETF/BOND/other） | ✅ |
| **P1-5c** | API：`GET/PUT/DELETE /accounts/{id}/broker-connection`（token 加密入库，不回显）；orchestrator brokerage 分支；复用 `POST /accounts/{id}/sync` | ✅ |
| **P1-5d** | 估值：`services/valuation/fx.py::convert_to_base` 抽出公共函数（EUR/USD→CNY 三角换算）；`compute_brokerage_value_per_account` 供 `/accounts/balances` 调用 | ✅ |
| **P1-5e** | `security_health` 把 broker token 纳入凭据健康自检；`_safe_error_text` 对 Flex token（`t=` 参数）脱敏 | ✅ |
| **P1-5f** | 前端：`AccountForm` `CONNECTION_SETUP_TYPES` 含 brokerage；`BrokerConnectionEditor` 内嵌填 Query ID + Token；`SyncAccountButton` 对 brokerage 显示 | ✅ |

**特性说明**：IBKR 为收盘快照（EOD）；Flex `markPrice` 原币写 `market_prices(source='ibkr')`；conid 存 `data_source_id`；按 `(asset_class, symbol)` 与手动建的同名 Asset 合并。

---

## ✅ Sprint 8 — Trade Republic 券商同步（2026-06-23 实装，未 UAT）

| # | 任务 | 状态 |
|---|---|---|
| **P1-6a** | `services/broker_sync/traderepublic.py`：TradeRepublicProvider，两步登录（playwright WAF token）；`compactPortfolioByType` 获取持仓（旧 `portfolio` topic 已废弃）；per-ISIN ticker 取价 | ✅ |
| **P1-6b** | broker_connections 迁移 `b2c3d4e5f6a7`：provider CHECK 加 `'traderepublic'`，`query_id` 改可空；TR 行存加密 cookie jar（`token_enc`）、`query_id=NULL`、`metadata_json` 存脱敏手机号 | ✅ |
| **P1-6c** | API：两步登录专属端点 `POST /accounts/{id}/broker-connection/tr/connect`（手机+PIN→进程内暂存）+ `POST /accounts/{id}/broker-connection/tr/verify`（4 位码→加密 cookies 入库） | ✅ |
| **P1-6d** | 复用 `POST /accounts/{id}/sync`：orchestrator 按 `row.provider` dispatch，TR 走 `resume_websession` cookies 路径（不需要 playwright，不影响常驻服务性能） | ✅ |
| **P1-6e** | 测试：`test_broker_sync.py` 覆盖 ISIN 映射 / 持仓映射 / fetch / 过期 session / 登录 round-trip / orchestrator（`_FakeTR` monkeypatch，不打外网） | ✅ |

**注意**：TR 无官方 API（社区逆向库 `pytr`，仅只读）；session 过期需重连；登录依赖 Playwright（`python -m playwright install chromium`）。**未做**：会话自动续期、交易流水导入。未经过真实账户 UAT。

---

## 修订后的执行序列（2026-06-25 更新）

| 顺序 | 阶段 | 估时 | 状态 |
|---|---|---|---|
| 1 | **Sprint 0** R0 资金正确性修复 | 1-2 天 | ✅ 2026-05-06 |
| 2 | **Sprint 1** R1 数据一致性 + 测试 | 2-3 天 | ✅ 2026-05-06 |
| 3 | **Sprint 2** R2 GitHub 公开前安全 | 0.5-1 天 | ✅ 2026-05-06 |
| 4 | **Sprint 3** V1 partial 闭合 | 1 天 | ✅ 2026-05-06 |
| 5 | **Sprint 4** review V3 派生 | 1 天 | ✅ 2026-05-06 |
| 6 | **UAT 大版本**（2026-05-07） | 1 天 | ✅ 2026-05-07 |
| 7 | **P1-1a/c/d** LLM fallback + 知识库 | 2-3 天 | ✅ 2026-05-08 |
| 8 | **transfer-matcher** 改进（5 天窗口 + 手动绑 + 容差） | 1 天 | ✅ 2026-05-09 |
| 9 | **P1-4** 链上钱包 + CEX + 价格 + include_in_total | 3-4 天 | ✅ 2026-05-18~19 |
| 10 | **Sprint 6** PDF 预览入库 + 转账口径修正 | 1-2 天 | ✅ 2026-06 |
| 11 | **Sprint 7** IBKR Flex 券商同步 | 2-3 天 | ✅ 2026-06-10 |
| 12 | **Sprint 8** Trade Republic 券商同步 | 2-3 天 | ✅ 2026-06-23（未 UAT） |
| 13 | **TR UAT** 真实账户验证 | 0.5 天 | 待用户操作 |
| 14 | **P1-2** GoCardless | 1-2 天 | 依赖账号 |
| 15 | **P1-3** Notion | 1-2 天 | 依赖 token + 库结构 |
| 16+ | **P2** 工程化债务（Dockerfile / E2E / CI / 生产 Bearer） | 视优先级 | 待启动 |

---

## 下一步 / 未来计划

| 优先级 | 功能 | 备注 |
|---|---|---|
| 高 | **TR UAT**（真实账户验证） | 需用户提供 TR 手机号+PIN 做一次完整登录+同步测试 |
| 中 | **盘中实时券商行情** | IBKR 需 Pro 账户 + 常驻 Client Portal Gateway；TR 无官方实时 API |
| 中 | **券商交易流水导入**（IBKR Flex Trades section / TR 未来 API） | 目前只有持仓快照，无历史成交记录 |
| 低 | **账单期缺口检测** | 检测导入序列中的月份空缺，提醒用户补传 |
| 低 | **P1-2** GoCardless 银行直连联调 | 依赖用户决策 + GoCardless 账号 |
| 低 | **P1-3** Notion 同步联调 | 依赖 integration token + 库结构决策 |

---

## 决策待澄清（用户输入）

1. ~~LLM 提供商~~（已答 2026-05-04：Gemini）
2. ~~置信度阈值~~（已答 2026-05-04：放进 settings 表，可调，默认 0.7）
3. ~~是否对手动 manual 走 LLM~~（已答：不走，仅 PDF / bank_api）
4. **GoCardless 沙箱 vs 生产**：愿意在欧洲账户上联调？
5. **Notion 库结构**：扁平一张 transactions DB，还是按月分库？asset 走 page 还是 DB？
6. **储蓄口径**：自动 `income - expense - 必要支出`？还是手动标记某些 transactions 为 savings？
7. ~~链上钱包覆盖~~（已答：主流 L1+L2 全覆盖；只算现货；多链多地址聚合到同一账户。见 `docx/CRYPTO_WALLET_PLAN.md`）
8. **Binance 合约钱包要不要加？**（已对 Bitget 加了 USDT-M / USDC-M / COIN-M）
9. **持仓表 UI 加列优先级？**（用户提过想看每个币种当前价 / 市值；成本价手工录入 vs 不显示）
