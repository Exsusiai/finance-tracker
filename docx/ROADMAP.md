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

## 🔥 Sprint 4 — review V3 派生：当前 P0/P1 + 安全（待启动 2026-05-06）

> 来源：`code review/review V3.md` 实地核查后属实的 14 项问题中，按用户范围（"目前正在关注的 P0/P1 + 安全"）筛出 7 项。半成品（GoCardless / Notion / bank encryption key 配置链）和 UX 优化（持仓跨币种市值、账户删除一致性）按用户约定推迟到对应子系统启用时一起修。
> 估时 2-3 天。

| # | 任务 | 来源 | 范围 |
|---|---|---|---|
| **FIX-19** | base_amount 生命周期：cashflow SQL 缺汇率不再 fallback raw amount（改为剔除外币缺 FX 行 + 返回 `fx_missing_count` warning）；PATCH/inbox 改 amount/currency/fx 字段时清空旧 `base_amount` 触发重算 | V3-P0-1 + V3-P0-2 | 资金正确性 |
| **FIX-20** | MCP 写入路径完整 mirror：`add_transaction` 加 sync `_convert_fx` 折算 + 写 base_amount；`parse_bank_statement` 改 `_safe_regex_search` + PDF size/magic guard + 同账户 amount-match 镜像 | V3-P1-1 | 一致性 + 安全 |
| **FIX-21** | `/cashflow/recompute` 跨年范围：改 `substr(occurred_at, 1, 7)` period 字符串比较 | V3-P1-4 | 功能 |
| **FIX-22** | 资产/净值币种语义：`by_currency` entry 拆分 `original_value` + `base_value`，明确 key 是报价币种 + 值是 base 折算；MCP `get_total_assets` 缺 FX 不计入 base total，返回 missing_fx 列表 | V3-P1-5 + V3-P1-8 | 资产展示 |
| **FIX-23** | metadata_json 校验：Pydantic schema 校验输入是 JSON object；`v_account_balance` SQL 加 `json_valid()` 防护 | V3-P1-6 | 安全可用性 |
| **FIX-24** | 同描述级联 NULL category：`apply_to_similar_pending` 改 `or_(category_id.is_(None), category_id != X)` | V3-P2-1 | 功能 |
| **FIX-25** | IntegrityError 响应脱敏：客户端只返回通用 message，原始 `exc.orig` 写日志 | V3-P3-1 | 安全 |

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
| **P1-1a** | LLM 分类 fallback 基础版 | ❌ 待启动 | 用户决定 LLM provider + 月度预算（推荐 Anthropic Haiku 4.5 / 月预算 ≤ 5 USD） | L1 关键词 miss → L2 LLM 调用；置信度 ≥ 阈值（settings 表可调）写入分类，否则进 inbox。详见 `docx/CLASSIFICATION_PLAN.md` §3 |
| **P1-1b** | 用户备注体系 | ✅ 2026-05-04 | — | `transactions.user_note` 字段；inbox 行内 textarea 输入；提交时与分类一起写入 |
| **P1-1c** | 知识库注入 LLM | ❌ 待启动 | P1-1a + P1-1b（已就绪） | LLM 调用时把 rules + 关键词 + 用户备注（最近 N 条相关）作为 prompt 上下文 |
| **P1-1d** | 知识库管理 UI | ❌ 待启动 | P1-1a/b/c | settings 页加「知识库」section：表格列出所有备注 + 来源 + 使用次数；可编辑 / 删除 / 导出 |
| **P1-2** | GoCardless 银行直连联调 | ⏸️ scaffold | 用户决策 + GoCardless 账号 | N26 / Revolut 真实账户连接，每日同步交易入库 |
| **P1-3** | Notion 同步联调 + 形态决策 | ⏸️ scaffold | 用户提供 integration token + 决定库结构 | `POST /notion/setup` 一键建库；transactions / cashflow / assets 三模块每日同步成功 |
| **P1-4** | **链上加密钱包同步**（公钥即同步，多链多地址）+ **Binance/Bitget CEX API** | ❌ 待启动 | 决策已敲定 | ① 多地址聚合一个钱包账户 ② Alchemy + Blockstream + Helius + TronGrid 覆盖 EVM / BTC / SOL / Tron ③ Binance + Bitget 现货 API。详见 `docx/CRYPTO_WALLET_PLAN.md` |
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

## 修订后的执行序列（2026-05-06）

| 顺序 | 阶段 | 估时 | 依赖你 |
|---|---|---|---|
| 1 | **Sprint 0** R0 资金正确性修复（FIX-1/2/3） | 1-2 天 | 无 |
| 2 | **Sprint 1** R1 数据一致性 + 测试（FIX-4~7） | 2-3 天 | 无 |
| 3 | **Sprint 2** R2 GitHub 公开前安全（FIX-8~12） | 0.5-1 天 | 无 |
| 4 | **P1-4** 链上钱包 + Binance/Bitget CEX | 3-4 天 | 无（决策已敲定） |
| 5 | **P1-1a/c/d** LLM fallback + 知识库 | 2-3 天 | LLM provider + 月预算 |
| 6 | **P1-2** GoCardless（含 FIX-13/14/15） | 1-2 天 | GoCardless 账号 |
| 7 | **P1-3** Notion 同步（含 FIX-16） | 1-2 天 | Notion token + 库结构 |
| 8+ | **P2** 工程化债务（Alembic / Dockerfile / E2E / CI） | 视优先级 | |

---

## 决策待澄清（用户输入）

1. **LLM 提供商 + 月度预算**（动 P1-1a 必答）：Anthropic Claude Haiku / OpenAI GPT-4o-mini / 本地 Ollama？月预算？
2. ~~置信度阈值~~（已答 2026-05-04：放进 settings 表，可调，默认 0.7）
3. ~~是否对手动 manual 走 LLM~~（已答：不走，仅 PDF / bank_api）
4. **GoCardless 沙箱 vs 生产**：愿意在欧洲账户上联调？
5. **Notion 库结构**：扁平一张 transactions DB，还是按月分库？asset 走 page 还是 DB？
6. **储蓄口径**：自动 `income - expense - 必要支出`？还是手动标记某些 transactions 为 savings？
7. ~~链上钱包覆盖~~（已答：主流 L1+L2 全覆盖；只算现货；多链多地址聚合到同一账户。见 `docx/CRYPTO_WALLET_PLAN.md`）
