# Finance Tracker — 项目进度

> ⚠️ 2026-05-03 修订：上一版高估了多项任务的完成度。本版按"是否真正闭环可用"重打分。详细分析见 `docx/REQUIREMENT_GAP.md`，优先级见 `docx/ROADMAP.md`。

## 项目信息
- **Repo**: https://github.com/Exsusiai/finance-tracker
- **Git**: `master` branch
- **本地端口**: Backend 8010, Frontend 3010（默认 8000/3000 已被其他项目占用，详见 CLAUDE.md）

## 状态图例
- ✅ 闭环可用（验证通过）
- ⚠️ 半成品（代码在但有核心缺口或未跑通）
- ⏸️ 完整 scaffold + 等用户许可启用
- ❌ 未实现
- 🛠 工程化债务

## 已闭环 — A 类
| # | Task | 状态 | 备注 |
|---|------|------|------|
| 1 | 导航重构 + 页面合并 | ✅ | dashboard / transactions / assets / analytics / settings |
| 2 | 资产管理页面 — 完整 CRUD | ✅ | 持仓表/饼图/账户余额面板 |
| 3 | 总览页重构 — 去重 + 资产概览 | ✅ | 净值卡片 + 快速操作 |
| 4 | 设置页面 — 账户与分类管理 | ✅ | |
| 6 | PDF 解析引擎 — 真实样本回归 | ✅ | 5 家全部通过 `data/inputpdf_reference/`：AMEX-DE / N26 / Revolut / TFBank / Advanzia。识别 + 期间 + 交易数与 PDF 抬头汇总对得上 |
| 12 | 资产搜索与自动识别 | ✅ | CoinGecko + yfinance 联合查询 |

## 半成品 — B 类（之前被错标 ✅）
| # | Task | 状态 | 真实情况 |
|---|------|------|------|
| 5 | 市场数据定时刷新 | ✅ | P0-1 已完成：`AsyncIOScheduler` 注册 crypto/stocks/fx 三个 job，按配置间隔自动刷新；`GET /api/v1/system/scheduler/status` 看运行状态 |
| — | CashFlow snapshot 自动重算 | ✅ | P0-2 完成：`services/cashflow/engine.py` 提供 `recompute_period` / `recompute_for_periods`；transaction CRUD（create/batch/update/delete）+ statement confirm + account adjust 全部 hook 后即时重算 |
| — | "待确认"工作流 | ⚠️ | `is_pending` 字段在但 categorizer 不写、前端无专栏；见 P0-4 |
| — | 银行解析器真实样本回归 | ⚠️ | 单测通过 ≠ 真实账单通过；见 P2-2 |
| — | `v_account_balance` 视图被前端使用 | ⚠️ | 视图创建在，是否被读未核 |

## scaffold + 等许可 — C 类
| # | Task | 状态 | 备注 |
|---|------|------|------|
| 7 | 智能分类 LLM Fallback | ⏸️ | 等待许可 + 选 LLM 提供商；见 P1-1 |
| 13 | GoCardless 银行同步 (N26 + Revolut) | ⏸️ | 代码 428 行，未联调；见 P1-2 |
| — | Notion 同步 (transactions / cashflow / assets) | ⏸️ | 代码完整，从未跑通；见 P1-3 |
| 14 | Amex / Advanzia PDF 适配 | ⏸️ → ❌ | 未启动；见 P2-1 |

## 未实现 — D 类（PRD 要求但代码完全没有）
| # | Task | 优先级 | 说明 |
|---|------|------|------|
| **NEW-1** | 分类自动学习 / 记忆 | ✅ P0-3 | 完成。`learn_from_user_assignment` 从用户分类反向新建/加强规则；keyword 提取避免噪音/数字/银行名；学到规则下次自动匹配验证通过 |
| **NEW-2** | 不确定收件箱工作流 | ✅ P0-4 | **后端 + 前端均完成**。transactions 页加「待确认」tab + 红色徽章计数；行内分类下拉（带 optgroup 一级/二级）+ 一键确认；改选别的分类时 UI 提示"⚡ 确认后会被记住" |
| **NEW-11** | 记账页层级化分类视图 | ✅ P0-7 | 完成。`CategoryBreakdownView` 组件：月份选择 + kind 切换 + 总额；左栏一级类目卡（带占比条）；右栏二级类目（带占比条 + 点开看明细）。挂为 `/transactions` 默认 tab |
| **NEW-12** | LLM 分类 fallback | 🟡 P1-1a | L1 关键词 miss → 调 LLM；置信度门槛后写分类。提供商待用户选 |
| **NEW-13** | 用户分类备注 | ✅ P1-1b | 完成。`transactions.user_note` 字段（idempotent ALTER TABLE）+ Pydantic schemas + Inbox UI 加 「+ 备注」展开式输入框；E2E 验证写入/取回一致 |
| **NEW-14** | 分类知识库注入 LLM | 🟡 P1-1c | 调 LLM 时把 rules + 关键词 + 用户备注（最近 N 条相关）作为 prompt 上下文 |
| **NEW-15** | 知识库管理 UI | 🟡 P1-1d | settings 页表格列出所有备注 + 来源 + 使用次数；可编辑 |
| 9 | MCP Server 端到端集成测试 | ✅ P0-5 | 完成 6 轮回归测试，发现并修复 9 个 bug（B1~B9）；7 tools 全部 PASS。修复要点：parse_bank_statement INSERT 缺字段、FX 折算方向反 + pivot 不全、parser 与 backend 漂移（已改为复用）、view 余额公式按 type 取符号 |
| **NEW-3** | 链上加密钱包余额同步（**只填公钥即同步**） | 🟡 P1-4 | `crypto.py` 仅 55 行 stub；新建账户时只输入钱包地址即可自动读余额 |
| **NEW-4** | 扫描件 OCR 兜底 | 🟢 P2-3 | TECH_STACK 列为 P1 |
| **NEW-5** | "储蓄"口径定义 + 单测 | 🟡 P1-5 | PRD 二次澄清 |

## 工程化债务 — E 类
| # | Task | 备注 |
|---|------|------|
| 8 | E2E 测试 (Playwright) | P2-6 |
| **NEW-6** | Alembic 真迁移版本化 | 当前空目录；P2-4 |
| **NEW-7** | Dockerfile 真实可构建 | P0 占位中；P2-5 |
| **NEW-8** | 后端 CI (ruff + mypy + pytest) | P2-7 |
| **NEW-9** | 生产 Bearer 鉴权打开 + 前端登录页 | 当前 `AUTH_DISABLED=true`；P2-9 |
| **NEW-10** | 黄金 GoldAPI key 接入 | 需用户申请；P2-8 |

## 执行记录
| 日期 | 动作 | 备注 |
|------|------|------|
| 2026-05-01 | P0×3 + P1×7 建站 | Claude Code overnight, ~15000 lines |
| 2026-05-02 | UI 审查 + 任务重构 | 发现资产管理缺失、导航/页面重叠问题 |
| 2026-05-02 | Task #2 资产管理页面 | 持仓表格/资产分布饼图/账户余额面板 |
| 2026-05-03 | Task #3 总览页重构 | Dashboard 去重 + 资产概览卡片 + 快速操作 |
| 2026-05-03 | Task #4 设置页面 | 验证已存在，build 通过 |
| 2026-05-03 | Task #12 资产搜索 | CoinGecko + yfinance 自动识别填充 |
| 2026-05-03 | UX 重构 | 净值摘要 + 账户管理 UI + 余额调整 + 货币统一 |
| 2026-05-03 | 本地启动 | backend 8010 / frontend 3010；`AUTH_DISABLED=true` 绕过鉴权 |
| 2026-05-03 | PRD + Gap 分析落地 | `docx/PRD.md` `docx/REQUIREMENT_GAP.md` `docx/ROADMAP.md` |
| 2026-05-03 | **PROGRESS 真实化** | 修正 Task #5 / #14 等虚标，新增 NEW-1 ~ NEW-10 |
| 2026-05-03 | 资产页 UX 重构 | 侧栏「投资组合」→「资产」、tab 加「账户」、双按钮、SWR 模糊刷新、存/取款增减模式 |
| 2026-05-03 | 显示币种切换 + FX 折算修复 | 加 USDT/USD/EUR/CNY 切换；后端 `_convert_to_base` 反向 rate bug 修复；FX 源切换至 `open.er-api.com` |
| 2026-05-03 | 币种选项扩展 | 账户/持仓加入稳定币（USDT/USDC/DAI）和 BTC/ETH/SOL；"证券账户"→"证券账户/交易所" |
| 2026-05-03 | PRD 细化 | 新增"加密钱包：只填公钥即同步"要求，登记到 P1-4 |
| 2026-05-03 | 加密钱包方案细化 | 用户答复：覆盖主流 L1+L2、仅现货、多链多地址聚合到同一账户。方案沉到 `docx/CRYPTO_WALLET_PLAN.md` |
| 2026-05-03 | 加密钱包方案定稿 | 决策：参考 rotki 自写、阶段 1 接入 Binance+Bitget、砍掉 ENS/SNS、手动同步触发 |
| 2026-05-03 | 显示币种切换全覆盖 | 资产页持仓表/资产分布/账户余额/账户卡四处补折算；dashboard 加币种切换器（共享 localStorage）+ 修原本"不同币种直接相加"bug |
| 2026-05-03 | **P0-1 APScheduler 接入** | engine 拆 4 个细粒度 refresh；新建 `services/market_data/scheduler.py`；lifespan 启动注册 3 个 job；`/system/scheduler/status` 暴露状态。fx 已自动写入 165 条 |
| 2026-05-03 | **P0-2 CashFlow 自动重算** | 新建 `services/cashflow/engine.py`；transaction create/batch/update/delete + statement confirm + account adjust-balance 全部 hook；端到端验证 income/savings 即时反映 |
| 2026-05-03 | PDF 解析重写（仅本人需要的 5 家） | engine.py 完全重写，AMEX-DE/N26/Revolut/TFBank/Advanzia 真实样本通过。砍掉 ICBC/CMB/CCB/BOC（非需求）。修 detector 误识别 + greenlet lazy load bug |
| 2026-05-04 | **P0-3 + P0-4 自动学习 + Inbox** | 9+30 类用户分类作为种子；70 条 keyword starter rule。PDF 导入后 78% 自动命中，剩 22% 进 inbox；用户分类一笔 → 反向学规则 → 同类项自动归并。E2E 验证：1 次手动归类后命中率升至 92% |
| 2026-05-04 | **Inbox + 分类管理 前端** | transactions 页新增「待确认」tab（带数量徽章 + 一键确认 + 改选自动学习提示）；settings 页新增分类管理（一级 + 二级两层 CRUD，重命名/删除/新建） |
| 2026-05-04 | 用户分类管道演进需求 | 用户提出三类新需求：① 记账页层级 UI 重构 ② LLM 分类 fallback + 用户备注 + 知识库注入。文档化到 `docx/CLASSIFICATION_PLAN.md`，ROADMAP 重排 P0-7 / P1-1a~d |
| 2026-05-04 | **P0-7 记账页层级化视图** | 新增 `CategoryBreakdownView`：月份选择 + 双栏（一级 → 二级 → 明细）+ 占比条；挂为 transactions 页默认 tab。后端 transactions 接口 limit 上限 200 → 1000 |
| 2026-05-04 | **P0-5 MCP 端到端测试 + 9 个 bug 全修** | 6 轮 agent 回归驱动：B1 INSERT 缺 transactions_count、B2 FX 方向、B3 mcp 包未装、B4 重复 import、B5 三角 pivot 缺 CNY、B6 parser 漂移（改为复用 backend）、B7 asyncio.run in event loop、B8 account_id 缺省 FK 失败、B9 v_account_balance 把 expense 加而非减。所有 bug 清零，7 tools 全 PASS |
| 2026-05-04 | **P1-1b 用户备注字段 + UI** | ORM 加 `user_note` 字段；lifespan 加 idempotent ALTER TABLE 自动迁移已有 DB；TransactionCreate/Update/Out schemas 同步；inbox 行内「+ 备注」展开式 textarea，提交时与分类一起写入 |
| 2026-05-05 | **跨账户转账识别（P0-8/9 新需求）** | PDF parser 加子账户/跨行关键词预标 transfer + metadata（`subaccount` / `cross_bank_hint`）；新增 `services/transfer_matcher`（评分模型：金额=50 / 日期 0..30 / 描述提示 0..30；阈值 75 自动配对）；`v_account_balance` 视图按 metadata `transfer_direction` 取符号，子账户 transfer 跳过；新增 `POST /transactions/{id}/mark-transfer` 与 `GET /transactions/transfers/suggestions`。E2E：N26+Revolut 双 PDF 导入后 6 笔跨行转账自动配对、子账户操作不影响余额、数学完全对得上 |
| 2026-05-05 | **分类树补全 + 子账户 L1+L2+L3 识别** | seed 加 income（工资/退款/利息/礼金/其他）+ transfer（信用卡还款/跨行/内部储蓄/投资划转/其他）两个 kind；inbox 下拉按 tx.type 过滤；UI 区分子账户(灰"内部")vs 跨行(蓝"跨行")标识。子账户三层识别：**L1** 关键词扩充 + **L2** per-account 用户清单（settings 卡内 SubaccountListEditor）+ **L3** 同账户 ±X 金额匹配启发式。「这是转账」按钮改为弹 modal 选方向 + 对方账户 + 候选 tx 配对 |
| 2026-05-05 | **修复 inbox dialog 不弹 + Revolut multi-product 解析 + amount-match 误配** | 修 3 个 bug：①「这是转账」点击无响应 — dialog 在 hidden td 内，把状态提到 InboxPanel 顶层；② inbox 分类下拉 income/transfer 行空 — categories 不预过滤，让 row 自己按 tx.type 选；③ Revolut PDF 把 "Net interest paid" 算成 expense — Revolut PDF 实际有 Account+Deposit 双 product 双 section，互转两边都有；改 Revolut 用 column-aware parser（按 word.x0 落在 Money out/Money in 列定位方向）、`skip_classify=True` 不预标 subaccount、依靠 amount-match 配对；amount-match 加 description 相似度门槛防误配（相同/共享 ≥4 字符 token） |

## 下一步
按 `docx/ROADMAP.md` 的 P0-1 ~ P0-5 顺序推进。建议起点：**P0-1 APScheduler 接入**（解锁后续所有"实时"体感）。
