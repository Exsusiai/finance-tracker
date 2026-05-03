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
| 6 | PDF 解析引擎 — 单测通过 | ✅ | 样本回归 → P2-2（重新审视） |
| 12 | 资产搜索与自动识别 | ✅ | CoinGecko + yfinance 联合查询 |

## 半成品 — B 类（之前被错标 ✅）
| # | Task | 状态 | 真实情况 |
|---|------|------|------|
| 5 | 市场数据定时刷新 | ⚠️ → ❌ | **APScheduler 未接**，价格只能手动 `/refresh`；见 P0-1 |
| — | CashFlow snapshot 自动重算 | ⚠️ | 文档说"debounce 2s 异步重算"，**代码无 hook**；见 P0-2 |
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
| **NEW-1** | 分类自动学习 / 记忆 | 🔴 P0-3 | PRD 原话"软件需要记住，下次不要放错" |
| **NEW-2** | 不确定收件箱工作流 | 🔴 P0-4 | PRD 原话"不确定的内容放进不确定列表" |
| 9 | MCP Server 端到端集成测试 | 🔴 P0-5 | 6 tools 已注册，未真实跑通 |
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

## 下一步
按 `docx/ROADMAP.md` 的 P0-1 ~ P0-5 顺序推进。建议起点：**P0-1 APScheduler 接入**（解锁后续所有"实时"体感）。
