# Finance Tracker — 开发任务优先级 ROADMAP

> 版本: 2026-05-03
> 来源: `docx/PRD.md` 与 `docx/REQUIREMENT_GAP.md`
> 排序原则: ① PRD 明文要求 + ② 用户当下感受得到的破窗 + ③ 解锁后续功能的依赖度
>
> **执行约定**：每完成一项 → 同步 `PROGRESS.md` 真实状态、写测试、`docx/REQUIREMENT_GAP.md` 对应行升级。

---

## P0 — 立刻做（修复"已声称但未实现"的虚标 + PRD 核心缺口）

| # | 任务 | 类型 | 验收标准 |
|---|---|---|---|
| **P0-1** | **市场价格定时刷新（接 APScheduler）** | 修虚标 | backend 启动后自动周期性写入 `market_prices` / `fx_rates`；首屏资产值无需手动 `/refresh` |
| **P0-2** | **CashFlow snapshot 自动重算** | 修虚标 | transaction CRUD 后 debounce 2s 触发对应月份重算；analytics 页数据与新交易一致 |
| **P0-3** | **分类自动学习 / 记忆** | PRD 核心缺口 | 用户改一笔交易的 category → 自动新建/更新 `categorization_rules`（基于 description/counterparty 关键字 + 防重 + priority 累加）；下次相似账单直接命中 |
| **P0-4** | **「待确认」收件箱工作流** | PRD 核心缺口 | 规则全部 miss → categorizer 写 `is_pending=true`；前端 `/transactions/inbox` 列出，用户分类后清空 pending（与 P0-3 联动产生新规则） |
| **P0-5** | **MCP 端到端真实集成测试** | PRD 验证缺口 | 用真实 client (claude / openclaw) 跑通：① 喂 PDF → 入库 ② 查询资产 ③ 查询现金流 |
| **P0-6** | **修正 PROGRESS.md 虚标 + 补全任务列表** | 文档同步 | 本次提交内完成 |

---

## P1 — 启用已 scaffold 的能力（用户给许可后逐项开）

| # | 任务 | 依赖/前置 | 验收标准 |
|---|---|---|---|
| **P1-1** | LLM 兜底分类（PROGRESS Task #7） | 用户许可 + 选择 LLM 提供商 | categorizer 规则 miss 时调用模型 → 写 confidence；低 confidence 仍走 P0-4 收件箱 |
| **P1-2** | GoCardless 银行直连联调（Task #13） | 用户许可 + GoCardless 账号 | N26/Revolut 真实账户连接成功，每日同步交易入库 |
| **P1-3** | Notion 同步联调 + 形态决策 | 用户提供 integration token + 决定 Notion 库结构 | `POST /notion/setup` 一键建库；transactions / cashflow / assets 三模块每日同步成功 |
| **P1-4** | 链上加密钱包余额同步（**公钥即同步**，**多链多地址同钱包**） + **CEX (Binance + Bitget) 接入** | `crypto.py` 重写（55 行 stub → 真实实现）；`AccountForm` `type=crypto_wallet` 时显示**地址列表**输入；新增 `type=exchange` 录入 Binance/Bitget API key（加密入库）；后端按 `account.type` 分发到对应 sync service；**手动触发**同步（不接 scheduler）。仅现货持仓 | ① 多地址聚合一个钱包账户 ② Alchemy + Blockstream + Helius + TronGrid 覆盖 EVM/BTC/SOL/Tron ③ Binance `GET /api/v3/account` + Bitget 现货 API 拉余额 ④ 自动 upsert holdings。详细方案见 `docx/CRYPTO_WALLET_PLAN.md` |
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

## 一周建议节奏（如果连续推进）

| 时段 | 重点 |
|---|---|
| **Day 1** | P0-6 文档同步 → P0-1 APScheduler |
| **Day 2** | P0-2 CashFlow 重算 hook |
| **Day 3-4** | P0-3 自动学习 + P0-4 收件箱（这两天是体验质变点） |
| **Day 5** | P0-5 MCP E2E |
| **Day 6+** | 进入 P1（按用户许可逐项启用） |

---

## 决策待澄清（用户输入）

> 这些悬而未决，建议在动 P1 前给出答案：

1. **LLM 提供商**：用 Anthropic / OpenAI / 本地 Ollama？是否限月度预算？
2. **GoCardless 沙箱 vs 生产**：愿意在欧洲账户上联调？
3. **Notion 库结构**：扁平一张 transactions DB，还是按月分库？asset 走 page 还是 DB？
4. **储蓄口径**：自动 `income - expense - 必要支出`？还是手动标记某些 transactions 为 savings？
5. **链上钱包**：~~已有用户答复 2026-05-03~~ — **主流 L1+L2 全覆盖；只算现货；同一钱包账户支持多链多地址聚合**。详细方案见 `docx/CRYPTO_WALLET_PLAN.md`。
