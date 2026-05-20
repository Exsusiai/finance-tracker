# Finance Tracker — 项目进度

> 修订日期: 2026-05-19
> 根据"是否真正闭环并验证过"打分；详细分析见 `docx/REQUIREMENT_GAP.md`，剩余优先级见 `docx/ROADMAP.md`。

## 项目信息
- **Repo**: https://github.com/Exsusiai/finance-tracker
- **Git**: `master` 分支（feat/llm-classification 已合并；P1-4 + Phase 1/2 已 commit，本地领先 origin 2-4 个 commit）
- **本地端口**: Backend `8010`, Frontend `3002`（默认 8000 / 3000 已被其他项目占用，详见 `CLAUDE.md`）
- **当前阶段**: ✅ Sprint 0+1+2+3+4+UAT + **P1-1 LLM 智能分类** + **P1-4 加密钱包 / CEX 同步 + 总资产计入 + include_in_total** 全部已实装。下一步：稳定化（doc + code review）→ 等用户拍板 P1-2 GoCardless / P1-3 Notion / P1-5 储蓄口径

## 2026-05-18~19 新交付：P1-4 加密钱包 + CEX 同步全栈
详见 [docx/CRYPTO_WALLET_PLAN.md](docx/CRYPTO_WALLET_PLAN.md)。核心交付：

**数据模型 + 迁移**（alembic `3317bd446ae0` + `7bc98bcff7fe`）：
- AccountType 加 `exchange` 枚举；ck_account_type CHECK 同步
- `chain_addresses` 表（按 chain 聚合多个地址到一个加密钱包账户）
- `exchange_connections` 表（AES-256-GCM 加密 api_key/secret/passphrase 三列，复用 FINANCE_BANK_ENCRYPTION_KEY）
- `asset_holdings.chain` + `.is_active` 列；(account_id, asset_id, chain) 替换原 unique
- `accounts.include_in_total` 字段（账户级排除总资产统计）

**链同步**（`services/crypto_sync/`）：覆盖 11 条 EVM L1+L2（Ethereum/Arbitrum/Optimism/Base/Polygon/zkSync/Linea/Scroll/Mantle/Blast，全部走 Alchemy）+ BTC（Blockstream）+ Solana（公共 RPC）+ Tron（TronGrid）

**CEX 同步**（`services/exchange_sync/`）：
- Binance：`/api/v3/account` HMAC-SHA256 hex sig
- Bitget：spot + USDT-M + USDC-M + COIN-M 四端点聚合，`available + locked` per coin（不计 unrealizedPL）

**价格自动发现**（`services/market_data/coingecko.py`）：同步后按 chain 批量拉 token_price（per-contract loop，应对免费 tier 1-call 限制）+ 按 ticker 拉 native price；USDT/USDC/DAI 等 USD-pegged 别名为 USD 解决 fiat 折算

**Orchestrator**（`services/wallet_sync/`）：
- 垃圾空投 token 过滤（URL / CLAIM / VISIT 等关键词）
- per-(chain, account) upsert 持仓；缺失 token 设 quantity=0 + is_active=False
- 失败不阻断（单链 / 单 CEX 端点错误隔离在该 source 行）
- 价格刷新去重（同 asset 多链共享 Asset 行，避免 UNIQUE 违反）

**前端**：
- AccountForm 加 `exchange` 类型 + IBAN/初始余额按类型条件渲染 + 创建后不关弹窗直接进「添加地址 / API 凭据」+ 「纳入总资产」checkbox
- 内嵌 ChainAddressesEditor / ExchangeConnectionEditor（用 `<div>` 避免嵌套 `<form>` 折叠）
- 账户卡 ↻ 立即同步按钮（每错误源详情显示）+ 「不计入总资产」徽章 + dim
- bank / credit_card 自动隐藏「+ 添加持仓」入口
- 机构字段对 exchange 用下拉（仅显示已对接 binance/bitget）

**汇率折算修复**：`_convert_to_base` 三角换算 pivot 加 CNY（项目 FX 源全部 `base_currency='CNY'`）

**测试**：全套 **306 passed**（含 spam_filter 28、coingecko 11、chain providers 15、exchange providers 14、wallet schema 14、orchestrator 8、upsert 7、API 10、holdings_value 7、usdt_alias 9、asset_identity 9、asset_lookup_chain_contract 14、transfer_pair_clears_pending 7、llm_dispatch_race 2）

## 2026-05-08 新交付：P1-1 LLM 智能分类
详见 [docx/LLM_CLASSIFICATION_PLAN.md](docx/LLM_CLASSIFICATION_PLAN.md)：
- **三层管道**：L1 关键词（命中 + `requires_llm=False` 短路）→ L2 LLM（Gemini，可联网）→ L3 inbox
- **「污染」机制**：用户对一笔交易写备注后，同 keyword 的 L1 规则自动 `requires_llm=True`，下次必走 LLM（解决 PayPal+amount 复合规则问题）
- **知识库**：`categorization_notes` 表 + Inbox 改分类时备注自动入库 + 注入 LLM prompt 作 few-shot
- **Provider 抽象**：`LLMProvider` Protocol，今日仅 Gemini，未来扩展 OpenAI/Anthropic
- **运行时配置**：`app_settings` KV 表（provider/model/budget/threshold/grounding/max_notes）
- **成本守门**：月度 USD 累计 + 超额自动降级
- **UI**：Settings 页加「智能分类」+ 「分类知识库」两个 section；Inbox 行内显示 ✨ LLM 推荐 + 采纳按钮
- **测试**：17 个 LLM 单测全过；全套回归 117 个全过

## 2026-05-07 新交付速览
12 项功能（详见 [docx/WORKLOG_2026-05-07.md](docx/WORKLOG_2026-05-07.md)）：
- CategoryScopeDialog · MarkTransferDialog 强制分类 · 子账户单边自动分类
- 删除「信用卡还款」合并到「跨行划转」 · 交易记录月份导航
- 「未配对转账」面板 + 手动绑定（信用卡缺端场景）
- 全局「↻ 重新匹配」按钮（9 步流水线）
- 编辑表单显示对手账户 + 解除绑定 · delete/refresh 孤儿指针清理
- Counter-leg 智能绑定（避免重复镜像） · matcher 双向兜底

---

## ✅ Sprint 0 — R0 资金正确性（已完成 2026-05-06）

| FIX | 内容 | 来源 |
|---|---|---|
| **FIX-1** | mark-transfer 持久化 transfer_direction | review V1 §P0-3 |
| **FIX-2** | savings 公式 4 处统一为 `ABS(income) − ABS(expense)` | review V1 §P1-1 |
| **FIX-3** | cashflow `COALESCE(base_amount, amount*fx_rate, amount)` + 前端去 EUR 硬编码 | review V1 §P1-2 |

## ✅ Sprint 1 — R1 数据一致性 + 测试基础设施（已完成 2026-05-06）

| FIX | 内容 | 来源 |
|---|---|---|
| **FIX-4** | 新增 `services/ingestion/` 统一管道（amount normalize → categorize → matcher → recompute），upload/reparse/batch/bank_sync/mcp 全部接入；reparse 保留 metadata + 跑分类/matcher（之前丢）；delete 重算 | review V1 §P1-3, §P1-5, §P1-7 |
| **FIX-5** | Category.kind ↔ Transaction.type 后端 invariant（create/update/inbox-confirm/categories-create） | review V1 §P1-4 |
| **FIX-6** | Transaction 3 个 Index + `(account_id, external_id)` partial unique（lifespan idempotent） | review V1 §P1-6 |
| **FIX-7** | pytest 已恢复；test_pdf_parser 重写（5 家欧洲 PDF round-trip）；test_api 转 integration；新增 test_ingestion + test_kind_invariant + test_index_invariants | review V1 §P3-2 |

## ✅ Sprint 2 — R2 GitHub 公开前安全加固（已完成 2026-05-06）

| FIX | 内容 | 来源 |
|---|---|---|
| **FIX-8** | Notion router 加 router 级 `Depends(require_auth)`，6 个 endpoint 一次性闭合；test_notion_auth.py 5 用例 | review V1 §P0-1 |
| **FIX-9** | `BACKEND_HOST` 默认 127.0.0.1；CORS 收紧到 `ALLOWED_ORIGINS` 列表；lifespan 拒启 AUTH_DISABLED+非 loopback 组合 | review V1 §P0-2 |
| **FIX-10** | PDF 上传 `MAX_PDF_SIZE_MB=10` + `%PDF-` magic bytes 双重校验 | review V1 §P2-1 |
| **FIX-11** | regex 规则写入时复杂度校验（长度 + 嵌套量词检测 + 编译验证）+ 运行时线程池 timeout | review V1 §P2-8 |
| **FIX-12** | SCHEMA.sql 视图同步真值；删 valuation 死 helper；transactions list 过滤共享 helper（count/data 一致）；layout.tsx 改 next/script | review V1 §P2-5/6/7, §P3-1 |

## ✅ Sprint 3 — 把 V1 partial 修完 + V2 衍生（已完成 2026-05-06）

| FIX | 内容 | 来源 |
|---|---|---|
| **FIX-13** | ingestion Step 1.5：`resolve_fx_to_base` 自动填 `base_amount/fx_rate_to_base`，缺汇率标 `metadata.fx_missing=true` | review V2 §V2-P0-1（V1 P1-2 partial） |
| **FIX-14** | categorize / apply_to_similar / /rules/apply-all 全部加 `Category.kind == tx.type` 守卫 | review V2 §V2-P0-2（V1 P1-4 partial） |
| **FIX-15** | transactions PATCH + inbox confirm + MCP `parse_bank_statement` 全部走完整 invariant | review V2 §V2-P0-3（V1 P1-5 partial） |
| **FIX-16** | rules.py `_match_rule` 共用 `_safe_regex_search` | review V2 §V2-P1-4（V1 P2-8 partial） |
| **FIX-17** | `resolved_database_url` 把相对 SQLite 路径锚到 `_PROJECT_ROOT` | review V2 §V2-P2-3（V1 P3-2 partial） |
| **FIX-18** | 删除前端 token 自动注入；settings 页加 ApiTokenInput；统一 `NEXT_PUBLIC_API_URL` | review V2 §V2-P2-4 + §V2-P2-2（V1 P2-7 partial） |

## ✅ Sprint 4 — review V3 派生：当前 P0/P1 + 安全（已完成 2026-05-06）

> review V3 14 项核查属实，按用户约定（仅当前 P0/P1 + 安全）修了 7 项；半成品 / UX 优化（5 项）延后到子系统启用阶段。

| FIX | 内容 | 来源 |
|---|---|---|
| **FIX-19** | cashflow SQL CASE 表达式（外币缺 FX → NULL 被 SUM 跳过）+ fx_missing_count；PATCH/inbox 清空 base_amount + fx_rate_to_base 后调 ingest_transactions 重折算 | V3-P0-1 + V3-P0-2 |
| **FIX-20** | MCP add_transaction 用 _convert_fx 写 base_amount；parse_bank_statement 加 PDF size/magic guard + ThreadPool regex timeout + 同账户 amount-match | V3-P1-1 |
| **FIX-21** | `/cashflow/recompute` from/to 改 YYYY-MM period 字符串比较；旧 from_year/from_month 仍兼容 | V3-P1-4 |
| **FIX-22** | PortfolioSummary/Breakdown/NetWorth.by_currency 拆 `{original_value, base_value}`；MCP get_total_assets 缺 FX 不混入 base total + fx_missing_cash 列表 | V3-P1-5 + V3-P1-8 |
| **FIX-23** | TransactionCreate/Update.metadata_json field_validator 强制 JSON object；v_account_balance SQL 用 json_valid() 防护 | V3-P1-6 |
| **FIX-24** | apply_to_similar_pending 改 `or_(category_id.is_(None), category_id != X)` 让未分类 pending sibling 也能被级联 | V3-P2-1 |
| **FIX-25** | IntegrityError handler 不回传 str(exc.orig)，原始 db 错误只写日志 | V3-P3-1 |

## 状态图例
- ✅ 闭环可用（验证通过）
- ⚠️ 半成品（代码在但有核心缺口）
- ⏸️ 完整 scaffold + 等用户许可启用
- ❌ 未实现
- 🛠 工程化债务

---

## 已完成 — A 类

### 资产管理 + 多币种
| Task | 状态 | 备注 |
|------|------|------|
| 导航重构 + 5 大页面 | ✅ | dashboard / transactions / assets / analytics / settings |
| 资产管理页面（账户 + 持仓双 CRUD） | ✅ | 账户卡 + 持仓表 + 资产分布饼图 + 余额面板 |
| 资产搜索 / 自动识别（CoinGecko + yfinance） | ✅ | 搜索关键词 → 自动填 `data_source` / `data_source_id` |
| **市场数据定时刷新**（APScheduler） | ✅ P0-1 | crypto 5min / stocks 15min / fx 1h；`GET /api/v1/system/scheduler/status` |
| **多币种显示切换** | ✅ | CNY/USD/EUR/USDT/HKD/JPY/GBP；dashboard + assets 共享 localStorage |
| FX 折算（direct → inverse → CNY/USD/EUR triangulate） | ✅ | 修了原本的方向反 bug |
| 「调整余额」对话框（存入/取出/校准三模式） | ✅ | 新建 adjustment tx 完成校准；明确"用于修正错记/漏记"文案 |
| 账户字段：name/type/institution/**iban**/currency/initial_balance/notes/sub-account names | ✅ | iban 字段 + AccountForm 输入；子账户清单存 metadata_json |

### PDF 导入 + 银行解析
| Task | 状态 | 备注 |
|------|------|------|
| PDF 上传 + SHA-256 去重 | ✅ | |
| **5 家银行解析器**（AMEX-DE / N26 / Revolut / TFBank / Advanzia） | ✅ | 真实样本回归通过：识别 + 期间 + 交易数 + 收支方向都与 PDF 抬头汇总对得上 |
| **Revolut column-aware parser** | ✅ | 按 word.x0 坐标定位 Money out / Money in 列，不靠描述启发式 |
| Revolut 续行合并（Reference / From / IBAN ...） | ✅ | append 到 raw_description，让 IBAN 进字段 |
| Bank detector 用 BIC / 域名 / 法定名 | ✅ | 避免被交易描述误判（如 N26 PDF 含 "AMERICAN EXPRESS" 被误识别） |

### 分类系统
| Task | 状态 | 备注 |
|------|------|------|
| 多级分类（一级 + 二级两层 CRUD） | ✅ | 9+30+5+5+5 = 54 类（expense 9 一级 / income 1 一级 5 子 / transfer 1 一级 5 子）|
| 分类种子（70+ starter keywords） | ✅ | seed.py 启动幂等写入 |
| 分类管理 UI（settings 页两栏） | ✅ | kind tabs + 一级 → 二级 CRUD + 重命名 |
| 关键词规则匹配（contains/regex/exact/starts_with） | ✅ | |
| **自动学习**（用户改分类 → 反向建规则） | ✅ P0-3 | 关键词提取去噪 + 防重 + priority |
| **「待确认」收件箱** | ✅ P0-4 | 后端 inbox API + 前端 InboxPanel + 数量徽章 |
| **高置信度自动通过 inbox** | ✅ | 命中规则的 + transfer 类直接 `is_pending=False` |
| **同描述级联学习** | ✅ | 用户改 1 笔 → 同 description 兄弟全部跟着改；跨 kind 时只改 seed |
| **用户备注**（`transactions.user_note`） | ✅ P1-1b | inbox 行内 textarea；为后续 LLM 上下文铺路 |
| **内联分类编辑**（点 category 标弹下拉） | ✅ | 共享 `InlineCategoryPicker` 组件；transactions 列表 + 分类视图明细行都能用 |
| **跨 kind 内联切换**（支出 ↔ 收入 ↔ 转账） | ✅ | optgroup label "支出·住家"；选中跨 kind 时同步 PATCH `type` |

### 现金流分析
| Task | 状态 | 备注 |
|------|------|------|
| 月度 income/expense/savings/transfer/other snapshot | ✅ | `cash_flow_snapshots` 表 |
| 时间序列图表（analytics 页） | ✅ | Recharts |
| **transaction CRUD 后即时重算** | ✅ P0-2 | hook 在 create/batch/update/delete + statement confirm + adjust-balance |
| **记账页层级化视图**（CategoryBreakdownView） | ✅ P0-7 | 月份选择 + kind 切换 + 双栏（一级 → 二级 → 明细） + 占比条 |

### 跨账户转账识别（核心反双计能力）
| Task | 状态 | 备注 |
|------|------|------|
| transfer_matcher service（评分配对） | ✅ | 金额 50 + 日期 0..30 + 描述提示 0..30 + IBAN +40 = 阈值 75 自动配 |
| **IBAN 字段 + 评分** | ✅ | accounts.iban；matcher 检索 description + raw_description |
| **同账户 ±X amount-match heuristic**（L3） | ✅ | 同账户 / 同金额 / ±3d / 描述相似度门槛 → 标 subaccount net 0 |
| **子账户三层识别**（L1 关键词 / L2 用户清单 / L3 amount-match） | ✅ | settings 账户卡内 SubaccountListEditor |
| 「这是转账」内联 modal（方向 + 对方账户 + 候选 tx 配对） | ✅ | inbox 行调用，弹 MarkTransferDialog |
| `v_account_balance` 视图按 type/方向取符号 | ✅ | 修了 expense 被加进余额的金融数据完整性 bug；subaccount 跳过；transfer 按 transfer_direction 取符号 |
| 转账建议面板（中置信度待确认配对） | ✅ | `/transfers/suggestions` + 前端 TransferSuggestionsPanel |

### Agent 接口（MCP）
| Task | 状态 | 备注 |
|------|------|------|
| MCP server 进程 + stdio | ✅ | `mcp-server/run.sh` |
| 7 tools 注册 | ✅ | get_total_assets / get_transactions / add_transaction / parse_bank_statement / get_cashflow / get_asset_allocation / search_transactions |
| **端到端集成测试**（6 轮回归） | ✅ P0-5 | 修复 9 个 bug（详见 `docx/MCP_TEST_REPORT.md`），全 PASS |
| MCP 复用 backend pdf_parser（消除漂移） | ✅ | mcp-server 直接 `await parse_pdf_statement` |

---

## ⏸️ 已 scaffold 但等启用

| Task | 状态 | 卡点 |
|------|------|------|
| GoCardless 银行同步（N26/Revolut） | ⏸️ | 代码 428 行 + 10+ endpoints；需用户决定沙箱 vs 生产 + 提供 GoCardless 账号 |
| Notion 同步（transactions/cashflow/assets 三模块 + 一键建库） | ⏸️ | 代码完整 + `POST /notion/setup`；需用户提供 integration token + 决定库结构 |

---

## ❌ 未实现 — D 类（PRD 要求 / 用户后续扩展）

### 🟡 P1（优先做）
| # | 任务 | 估时 | 依赖 |
|---|------|------|------|
| **P1-1a-d** | LLM 兜底分类 | — | ✅ 已实装（2026-05-08）|
| **P1-4** | 链上加密钱包同步 + CEX | — | ✅ 已实装（2026-05-18~19，含 Phase 1 价格 + Phase 2 include_in_total / Bitget 合约） |
| **P1-4-ext** | Binance 合约钱包（USDT-M + 币本位，同 Bitget pattern） | 0.5 天 | 可选扩展；用户尚未明确要 |
| **P1-4-ext** | 持仓表 UI 加列（数量 / 当前价 / 市值 / 成本价；成本价手工录入） | 1 天 | 用户已提过想看每个币种的当前价/市值 |
| **P1-5** | "储蓄"计算口径定义 + 单测 | 0.5 天 | 等用户给定义（自动 income−expense？还是手动标记？） |

### 🟢 P2（工程化债务）
| # | 任务 | 备注 |
|---|------|------|
| **P2-3** | 扫描件 OCR 兜底（pdf2image + tesseract） | 当前 5 家 PDF 都是文本型 |
| **P2-4** | Alembic 真迁移版本化 | 当前用 idempotent ALTER 顶住，生产前必须接 |
| **P2-5** | Dockerfile 真实可构建 + 部署文档 | docker-compose 在但 Dockerfile 占位 |
| **P2-6** | E2E Playwright 测试 | 当前全靠手测 + agent E2E |
| **P2-7** | 后端 CI（ruff + mypy + pytest GitHub Actions） | 本地能跑，CI 未配 |
| **P2-8** | 黄金 GoldAPI 接入 | 需用户申请 key |
| **P2-9** | 生产模式 Bearer 启用 + 前端登录页 | 当前 `AUTH_DISABLED=true` |

### P3（PRD 明示推迟）
- 移动端 App
- Notion 双向同步
- 投资分析 / 财务规划顾问类（明确**非目标**）

---

## 执行记录（按时间倒序）

| 日期 | Commit | 动作 |
|------|--------|------|
| 2026-05-19 | `cf9cfa4` | fix(ux): 嵌套 form / React 19 state-during-render / 持仓表单范围限制 |
| 2026-05-18~19 | `ca6d756` | feat(crypto-sync): P1-4 加密钱包 + CEX 同步全栈 + 总资产计入 + include_in_total（6300+ 行）|
| 2026-05-09 | `6b6ce32` | feat(transfer-matcher): 5 天窗口 + 手动绑定 + 自定义金额容差 + 信用卡还款方向 |
| 2026-05-08 | `707dd3b` | feat(llm-classification): P1-1 智能分类三层管道 + 知识库 |
| 2026-05-05 | `ad84112` | 内联分类编辑支持跨 kind 切换（支出/收入/转账）+ apply_to_similar 加 type 守卫保护跨 kind 改时不误级联 |
| 2026-05-05 | `69d4fc0` | 共享 InlineCategoryPicker 组件 + 分类视图明细行可内联编辑 + 级联范围扩到已分类条目（`source!=manual` AND `type!=transfer`） |
| 2026-05-05 | `c41a757` | inbox 自动通过（命中规则 + transfer 直接 is_pending=False）+ apply_to_similar_pending 同描述级联 |
| 2026-05-05 | `7008c57` | IBAN 字段 + AccountForm UI + transfer_matcher IBAN 评分（+40）+ 余额校准 UX 重命名 |
| 2026-05-05 | `8078aca` | 修 inbox dialog 不弹 / Revolut multi-product column-aware 解析 / amount-match 误配（加描述相似度门槛） |
| 2026-05-05 | `8bb46a1` | 分类树补全（income + transfer kind）+ 子账户 L1+L2+L3 识别 + MarkTransferDialog（方向 + 对方账户 + 候选配对） |
| 2026-05-05 | `7dae743` | P1-1b 用户备注（user_note 字段 + Inbox UI）+ 跨账户转账识别（transfer_matcher service v1） |
| 2026-05-04 | `7b0916d` | P0-1~P0-7 全部完成 + MCP server 6 轮回归测试 9 bug 全修 |
| 2026-05-03 | `1f38fae` | 显示币种切换 + FX 折算修复（方向反 bug） |
| 2026-05-03 | `03fecff` | 资产页 UX 重构 + 账户管理 UI |
| 2026-05-03 | `83e9334` | 资产搜索（CoinGecko + yfinance） |
| 早期 | — | 项目脚手架 + 5 家 PDF parser 基础 + 多币种 + transactions/categories/cashflow CRUD + MCP server 7 tools 注册 |

---

## 下一步

P1-1 + P1-4 主体均闭环。**当前阶段（2026-05-19）**：稳定化（updated docs + 多 agent 代码审查）。

之后按 `docx/ROADMAP.md` 推进，剩余排序：

1. **P1-4-ext 持仓表 UI 加列**（数量 / 当前价 / 市值 / 成本价）— 用户已提出明确需求
2. **P1-4-ext Binance 合约钱包**（与 Bitget 同 pattern，半天）— 用户可选
3. **P1-5 储蓄口径** — 依赖你给定义
4. **P1-2 GoCardless** / **P1-3 Notion** — 依赖外部账号 / token
5. **P2 工程化债务**（CI / Dockerfile / E2E / 生产 Bearer）— 功能稳定后插入
