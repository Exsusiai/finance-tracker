# Finance Tracker — 开发任务优先级 ROADMAP

> 修订日期: 2026-05-05
> 来源: `docx/PRD.md` 与 `docx/REQUIREMENT_GAP.md`
> 排序原则: ① PRD 明文要求 + ② 用户当下感受得到的破窗 + ③ 解锁后续功能的依赖度

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

## 修订后的执行序列（2026-05-05）

| 顺序 | 任务 | 估时 | 依赖你 |
|---|---|---|---|
| 1 | **P1-1a + P1-1c** LLM fallback + 知识库注入 | 2-3 天 | LLM provider + 月预算 |
| 2 | **P1-1d** 知识库管理 UI | 0.5 天 | P1-1a/c 完成 |
| 3 | **P1-4** 链上钱包 + Binance/Bitget CEX | 3-4 天 | 无（决策已敲定） |
| 4 | **P1-5** 储蓄口径 | 0.5 天 | 你给口径定义 |
| 5 | **P1-2** GoCardless | 1-2 天 | GoCardless 账号 |
| 6 | **P1-3** Notion 同步 | 1-2 天 | Notion token + 库结构 |
| 7+ | **P2** 工程化债务 | 视优先级 | |

---

## 决策待澄清（用户输入）

1. **LLM 提供商 + 月度预算**（动 P1-1a 必答）：Anthropic Claude Haiku / OpenAI GPT-4o-mini / 本地 Ollama？月预算？
2. ~~置信度阈值~~（已答 2026-05-04：放进 settings 表，可调，默认 0.7）
3. ~~是否对手动 manual 走 LLM~~（已答：不走，仅 PDF / bank_api）
4. **GoCardless 沙箱 vs 生产**：愿意在欧洲账户上联调？
5. **Notion 库结构**：扁平一张 transactions DB，还是按月分库？asset 走 page 还是 DB？
6. **储蓄口径**：自动 `income - expense - 必要支出`？还是手动标记某些 transactions 为 savings？
7. ~~链上钱包覆盖~~（已答：主流 L1+L2 全覆盖；只算现货；多链多地址聚合到同一账户。见 `docx/CRYPTO_WALLET_PLAN.md`）
