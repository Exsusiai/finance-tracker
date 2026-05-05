# 需求 vs 实现 — Gap 分析

> 修订日期: 2026-05-05
> 基于 PRD v1.1 与当前代码 (`master` @ ad84112) 的对比
>
> 图例：
> - ✅ **A 闭环可用** — 代码到位且基本路径已验证
> - ⚠️ **B 半成品** — 有核心缺口
> - ⏸️ **C scaffold + 等待启用** — 代码完整但未联调或开关默认关
> - ❌ **D 未实现** — PRD 要求但代码完全没有
> - 🛠 **E 工程化债务** — 部署/测试/迁移配套

---

## 1. 自动记账

### 1.1 手动导入 PDF

| 子需求 | 等级 | 证据 / 缺口 |
|---|---|---|
| 上传入口 + SHA-256 去重 | ✅ A | `pdf-import-panel.tsx`, `pdf_imports.file_hash` UNIQUE |
| 银行检测（BIC / 域名 / 法定名） | ✅ A | `pdf_parser/engine.py::_detect_bank` |
| **5 家银行解析器**（AMEX-DE / N26 / Revolut / TFBank / Advanzia） | ✅ A | 真实样本回归通过 |
| **Revolut column-aware parser**（Money out / Money in 列定位） | ✅ A | 按 `word.x0` 坐标，纯 text 无法区分双 product |
| Revolut 续行合并（Reference / From / IBAN 进 raw_description） | ✅ A | matcher 后续可识别 IBAN |
| 上传后预览 → 用户确认（inbox） | ✅ A | InboxPanel + 数量徽章 |
| 扫描件 OCR 兜底 | ❌ D | 当前 5 家 PDF 都是文本型；P2-3 |

### 1.2 银行直连

| 子需求 | 等级 | 证据 / 缺口 |
|---|---|---|
| GoCardless 接入 | ⏸️ C | `services/bank_sync/engine.py` 428 行 + 10+ endpoints；`BANK_SYNC_ENABLED=false`。**P1-2** |
| **链上加密钱包余额同步**（公钥即同步） | ❌ D | `crypto.py` 仅 55 行 stub。方案见 `docx/CRYPTO_WALLET_PLAN.md`。**P1-4** |
| **CEX API**（Binance / Bitget）只读余额 | ❌ D | 与 P1-4 一起做 |

### 1.3 分类

| 子需求 | 等级 | 证据 |
|---|---|---|
| 多级分类（一级 + 二级） | ✅ A | `categories.parent_id`；settings 页两栏 CRUD |
| 分类种子（54 类 + 70 keywords） | ✅ A | `categorizer/seed.py`；含 expense (9 一级 30 二级) + income (1+5) + transfer (1+5) |
| 关键词规则匹配 | ✅ A | contains / regex / exact / starts_with；priority 排序 |
| **自动学习**（用户改分类 → 反向建规则） | ✅ A | `learn_from_user_assignment` + 关键字提取去噪 |
| **「待确认」收件箱** | ✅ A | inbox API + InboxPanel |
| **高置信度自动通过 inbox** | ✅ A | 命中规则 + transfer 直接 `is_pending=False` |
| **同描述级联**（用户改 1 → 同名兄弟全跟着） | ✅ A | `apply_to_similar_pending`，含 `source!=manual` / `type!=transfer` / `type==seed.type` 等保护 |
| **用户备注**（user_note 字段 + UI） | ✅ A | inbox 行内「+ 备注」textarea；为 LLM 上下文铺路 |
| **内联分类编辑**（点 category 弹下拉） | ✅ A | 共享 `InlineCategoryPicker`；列表 + 分类视图明细行复用 |
| **跨 kind 内联切换**（支出 ↔ 收入 ↔ 转账） | ✅ A | 下拉列出全部 3 个 kind；选中跨 kind 同步 PATCH `type` |
| **LLM 兜底分类** | ❌ D | 三层管道方案见 `docx/CLASSIFICATION_PLAN.md`。**P1-1a** |
| **知识库注入 LLM** | ❌ D | 调 LLM 时把 rules + 关键词 + 用户备注作为 prompt 上下文。**P1-1c** |
| **知识库管理 UI** | ❌ D | settings 页表格列出所有备注。**P1-1d** |

### 1.4 跨账户转账识别（PRD 隐含 + 用户 2026-05-05 明确）

| 子需求 | 等级 | 证据 |
|---|---|---|
| transfer_matcher 服务（评分配对） | ✅ A | 金额 50 + 日期 0..30 + 描述提示 0..30 + IBAN +40，阈值 75 自动配 |
| **IBAN 字段** + 评分 | ✅ A | `accounts.iban`；matcher 检索 description + raw_description |
| **同账户 amount-match heuristic**（L3） | ✅ A | 同账户 / 金额相同 / ±3d / 描述相似度门槛 |
| **子账户三层识别** | ✅ A | L1 关键词 / L2 user_list / L3 amount-match |
| 「这是转账」内联 modal（方向 + 对方账户 + 候选配对） | ✅ A | `MarkTransferDialog` |
| `v_account_balance` 视图按 type/方向取符号 | ✅ A | subaccount 跳过；transfer 按 `transfer_direction` 取符号；修了 expense 被加进余额的 bug |
| 转账建议面板（中置信度待确认） | ✅ A | `/transfers/suggestions` + `TransferSuggestionsPanel` |

---

## 2. 资产实时跟踪

| 子需求 | 等级 | 证据 |
|---|---|---|
| 资产种类枚举 | ✅ A | `AssetClass` (cash / a_share / eu_stock / us_stock / crypto / gold / bond / fund / other) |
| 资产搜索 / 自动识别 | ✅ A | CoinGecko + yfinance |
| 持仓 CRUD | ✅ A | `api/v1/holdings.py` |
| yfinance / CoinGecko / FX 取价 | ✅ A | `market_data/engine.py` |
| **价格定时自动刷新**（APScheduler） | ✅ A | crypto / stocks / fx 三 job；启动后 15s 首次跑 |
| FX 折算（direct → inverse → 三角） | ✅ A | 含 CNY/USD/EUR 三 pivot；修了方向反 bug |
| **多币种切换**（CNY/USD/EUR/USDT/HKD/JPY/GBP） | ✅ A | dashboard + assets 共享 localStorage；稳定币 USDT/USDC = USD 折算 |
| 总资产汇总 / 资产分布 / 净值卡 | ✅ A | `/holdings/portfolio/summary` + `/net-worth` + `/breakdown` |
| **余额校准**（`accounts/{id}/adjust-balance`） | ✅ A | 三模式：存入 / 取出 / 校准目标值；创建 adjustment tx |
| 黄金 GoldAPI | ⚠️ B/E | 配置项在；需用户申请 key。P2-8 |
| **链上钱包余额读取** | ❌ D | 见 1.2；P1-4 |

---

## 3. 现金流分析

| 子需求 | 等级 | 证据 |
|---|---|---|
| 月度 income/expense/savings/transfer/other snapshot | ✅ A | `cash_flow_snapshots` 表 |
| 时间序列图表 | ✅ A | `analytics/page.tsx` |
| **transaction CRUD 后即时重算** | ✅ A | tx create/batch/update/delete + statement confirm + adjust-balance + 级联学习全部 hook |
| **记账页层级化视图**（类目 → 子类目 → 明细） | ✅ A | `CategoryBreakdownView` |
| 「储蓄」计算口径 | ⚠️ B | 字段存在，定义需 PRD 二次澄清；P1-5 |

---

## 4. 产品形态

| 子需求 | 等级 | 备注 |
|---|---|---|
| Web UI（Next.js 15 + shadcn/ui） | ✅ A | 5 大页面 |
| PDF 上传区域 | ✅ A | |
| 资金变化图表 | ✅ A | dashboard 净值卡 + analytics 时间序列 |
| 记账模块（含层级化分类视图） | ✅ A | `CategoryBreakdownView` 默认 tab |
| App | ❌ D | PRD 明示后续 |

---

## 5. Agent 接口

| 子需求 | 等级 | 证据 |
|---|---|---|
| MCP server 进程 + stdio | ✅ A | `mcp-server/run.sh` |
| 7 tools 注册 | ✅ A | get_total_assets / get_transactions / add_transaction / parse_bank_statement / get_cashflow / get_asset_allocation / search_transactions |
| **端到端集成测试**（6 轮回归） | ✅ A | 9 bug 全修；详见 `docx/MCP_TEST_REPORT.md` |
| MCP 复用 backend pdf_parser | ✅ A | 消除了 parser 漂移 |
| REST API（同等能力） | ✅ A | `/api/v1/*` |

---

## 6. 数据存储

| 子需求 | 等级 | 证据 |
|---|---|---|
| SQLite（WAL）本地 | ✅ A | `data/finance.db` |
| **Notion 同步** | ⏸️ C | 代码完整 + 一键建库 API；从未跑通。**P1-3** |

---

## 7. 工程化债务

| 项 | 等级 | 备注 |
|---|---|---|
| Alembic 迁移 | 🛠 E | 当前用 idempotent ALTER 顶住；P2-4 |
| Dockerfile 真实可构建 | 🛠 E | 占位中；P2-5 |
| E2E Playwright | 🛠 E | P2-6 |
| 后端 CI | 🛠 E | P2-7 |
| 生产 Bearer 启用 + 登录页 | 🛠 E | 当前 `AUTH_DISABLED=true`；P2-9 |

---

## 8. 当前最大缺口

> 按"用户实际触不到 → PRD 要求"排序：

1. **🔴 LLM 兜底分类**（P1-1a/c/d）— 当前命中率 ~80%，剩 20% 进 inbox 全靠人工；接入 LLM 可推到 95%+
2. **🟡 链上钱包同步**（P1-4）— PRD 明文"加密钱包资产"目前只能手动录入数量
3. **🟡 储蓄计算口径**（P1-5）— `cash_flow_snapshots.savings_total` 含义模糊，需用户给定义
4. **🟡 Notion 同步**（P1-3）— "数据存两个地方"目前只在本地
5. **🟢 GoCardless**（P1-2）— "完全自动化记账"还差 PDF 之外的实时入账渠道

详细优先级排序见 `docx/ROADMAP.md`。
