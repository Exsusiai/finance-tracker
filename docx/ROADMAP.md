# Finance Tracker — 开发任务优先级 ROADMAP

> 版本: 2026-05-04（增量修订）
> 来源: `docx/PRD.md` 与 `docx/REQUIREMENT_GAP.md`
> 排序原则: ① PRD 明文要求 + ② 用户当下感受得到的破窗 + ③ 解锁后续功能的依赖度
>
> **执行约定**：每完成一项 → 同步 `PROGRESS.md` 真实状态、写测试、`docx/REQUIREMENT_GAP.md` 对应行升级。

---

## P0 — 完成情况 + 当前进行项

| # | 任务 | 状态 | 备注 |
|---|---|---|---|
| **P0-1** | 市场价格定时刷新（APScheduler） | ✅ 2026-05-03 | crypto / stocks / fx 三个 job 自动跑 |
| **P0-2** | CashFlow snapshot 自动重算 | ✅ 2026-05-03 | tx CRUD + adjust + statement confirm 全部 hook |
| **P0-3** | 分类自动学习 / 记忆 | ✅ 2026-05-04 | `learn_from_user_assignment` + 关键字提取去噪 |
| **P0-4** | 「待确认」收件箱工作流 | ✅ 2026-05-04 | 后端 + 前端 InboxPanel + 数量徽章 + 一键确认 |
| **P0-5** | MCP 端到端真实集成测试 | ⏳ 未开始 | 用真实 Agent client 跑通：① 喂 PDF ② 查资产 ③ 查现金流 |
| **P0-6** | 修正 PROGRESS.md 虚标 | ✅ 2026-05-03 | |
| **P0-7** | **记账页层级化分类视图重构**（用户 2026-05-04 提出） | 🔴 新增，立即做 | 当前所有 tx 平铺不直观。重构为：**选一级类目 → 看二级类目本月总额列表 → 点开看明细**。顶部展示该一级总额 + 占总支出比 |

---

## P1 — 启用已 scaffold 的能力 + 用户新增高优需求

> **2026-05-04 重排**：用户提出"分类管道升级"为最高 P1，原 GoCardless / Notion 顺延

| # | 任务 | 依赖/前置 | 验收标准 |
|---|---|---|---|
| **P1-1a** | **LLM 分类 fallback 基础版**（用户 2026-05-04 强调） | 用户决定 LLM provider + 月度预算 | L1 关键词 miss → L2 LLM 调用；置信度 ≥ 0.7 写入分类，否则进 inbox。详细方案见 `docx/CLASSIFICATION_PLAN.md` §3 |
| **P1-1b** | **用户备注体系**（用户 2026-05-04 新增） | P0-4 已就绪 | `transactions.user_note` 字段；inbox 行内可输入备注；备注随分类一起写入 |
| **P1-1c** | **分类知识库注入 LLM**（用户 2026-05-04 新增） | P1-1a + P1-1b | LLM 调用时把"已有 rules + 关键词 + 用户备注（最近 N 条相关）"作为 prompt 上下文；提升准确率 |
| **P1-1d** | **知识库管理 UI** | P1-1b | settings 页加「知识库」section：表格列出所有备注 + 来源 tx + 使用次数；可编辑 / 删除 / 导出 |
| **P1-2** | GoCardless 银行直连联调（Task #13） | 用户许可 + GoCardless 账号 | N26/Revolut 真实账户连接成功，每日同步交易入库 |
| **P1-3** | Notion 同步联调 + 形态决策 | 用户提供 integration token + 决定 Notion 库结构 | `POST /notion/setup` 一键建库；transactions / cashflow / assets 三模块每日同步成功 |
| **P1-4** | 链上加密钱包余额同步（**公钥即同步**，**多链多地址同钱包**） + **CEX (Binance + Bitget) 接入** | `crypto.py` 重写；`AccountForm` `type=crypto_wallet` 时显示地址列表；新增 `type=exchange` 录入 API key | ① 多地址聚合一个钱包账户 ② Alchemy + Blockstream + Helius + TronGrid 覆盖 EVM/BTC/SOL/Tron ③ Binance + Bitget 现货 API。详细方案见 `docx/CRYPTO_WALLET_PLAN.md` |
| **P1-5** | "储蓄"口径定义 + 实现 | PRD 二次澄清 | `cash_flow_snapshots.savings_total` 计算口径文档化、有单测 |

---

## P2 — 工程化与扩展覆盖

| # | 任务 | 验收标准 |
|---|---|---|
| **P2-1** | Amex / Advanzia PDF 解析器（Task #14） | 真实样本通过单测 |
| **P2-2** | 现有银行解析器**真实样本回归** | 每家银行至少 1 个真实 PDF 进 `tests/fixtures/`，单测覆盖 |
| **P2-3** | 扫描件 OCR 兜底（pdf2image + tesseract） | 1 个扫描型样本可解析 |
| **P2-4** | Alembic 真迁移版本化 | 把当前 schema 冻结成 0001，新字段走 alembic |
| **P2-5** | Dockerfile 真实可构建 + 部署文档 | `docker compose up` 可跑通后端 + 前端 |
| **P2-6** | E2E Playwright 测试（Task #8） | 覆盖：上传 PDF / 查看资产 / 改分类 / 查看现金流 |
| **P2-7** | 后端 CI（ruff + mypy + pytest） | GitHub Actions 通过 |
| **P2-8** | 黄金 GoldAPI 接入 | 需用户申请 key |
| **P2-9** | 生产模式打开 Bearer 鉴权 + 前端登录页 | 部署前清单 |

---

## P3 — 后续 / PRD 明示推迟

- 移动端 App（PRD 已明示后期）
- Notion 反向 / 双向同步（视 P1-3 决策）
- 投资分析 / 财务规划顾问类（明确**非目标**，记此防 scope creep）

---

## 修订后的执行序列（2026-05-04）

| 顺序 | 任务 | 估时 | 用户感知价值 |
|---|---|---|---|
| 1 | **P0-7 记账页层级化视图** | 半天-1 天 | 🔥 当前最痛的 UX 缺口，立即可见 |
| 2 | **P1-1b 用户备注字段 + Inbox UI** | 0.5 天 | 为 LLM 上下文铺路 |
| 3 | **P1-1a + P1-1c LLM fallback + 知识库注入** | 2-3 天 | 把命中率从 78% 提到 95%+ |
| 4 | **P1-1d 知识库管理 UI** | 0.5 天 | 让用户能审计 / 维护 LLM 上下文 |
| 5 | P0-5 MCP E2E 测试 | 0.5 天 | 验收 Agent 接口 |
| 6+ | P1-4（钱包+CEX）/ P1-2（GoCardless）/ P1-3（Notion） | 视用户决策 | |

## 一周建议节奏（如果连续推进，已弃用）

旧版本已删除——以"修订后的执行序列"为准。

---

## 决策待澄清（用户输入）

> 这些悬而未决，建议在动 P1 前给出答案：

1. ~~LLM 提供商 + 月度预算~~（用户 2026-05-04 答：**后期再定**，schema/config 先留位）
2. ~~置信度阈值~~（用户 2026-05-04 答：**放进 settings 表**，前端可调，默认 0.7）
   ~~是否对手动 manual 走 LLM~~（用户 2026-05-04 答：**不走**，仅 PDF / bank_api）
3. **GoCardless 沙箱 vs 生产**：愿意在欧洲账户上联调？
4. **Notion 库结构**：扁平一张 transactions DB，还是按月分库？asset 走 page 还是 DB？
5. **储蓄口径**：自动 `income - expense - 必要支出`？还是手动标记某些 transactions 为 savings？
6. **链上钱包**：~~已答~~ — 主流 L1+L2 全覆盖；只算现货；多链多地址聚合。见 `docx/CRYPTO_WALLET_PLAN.md`。
