# 需求 vs 实现 — Gap 分析（修订版）

> 基于 PRD v1.0 与当前代码 (master @ 2026-05-03) 的对比
> **本次修订承认上一版高估了实现度**：把"代码 scaffold 存在"误判为"功能可用"。本版按"是否真正闭环并验证过"重新分级。
>
> 图例：
> - ✅ **A 闭环可用** — 代码到位且基本路径已验证
> - ⚠️ **B 半成品 / 文档与代码不符** — 之前被错标为 ✅，实际有核心缺口
> - ⏸️ **C scaffold + 等待许可启用** — 代码完整但未联调或开关默认关
> - ❌ **D 未实现** — PRD 要求但代码完全没有
> - 🛠 **E 工程化债务** — 部署/测试/迁移配套

---

## 1. 自动记账

### 1.1 手动导入 PDF
| 子需求 | 等级 | 证据 / 缺口 |
|---|---|---|
| 上传入口 + SHA-256 去重 | ✅ A | `frontend/src/components/pdf-import-panel.tsx`, `pdf_imports.file_hash` UNIQUE |
| 银行自动检测 (`_detect_bank`) | ✅ A | `pdf_parser/engine.py` 文本特征匹配 |
| ICBC/CMB/CCB/BOC/N26/Revolut 解析器 | ⚠️ B | 代码到位、有单测，但**未用真实样本回归**——上次跑 `tests/test_pdf_parser.py` 距今未知 |
| Amex / Advanzia 解析器 | ❌ D | PROGRESS Task #14 未启动 |
| 扫描件 OCR 兜底 | ❌ D | TECH_STACK 列为 P1，未实现 |
| 上传后预览 → 用户确认入库 | ⚠️ B | `is_pending` 字段在，但前端"待确认"工作流是否完整未核 |

### 1.2 银行直连
| 子需求 | 等级 | 证据 / 缺口 |
|---|---|---|
| GoCardless / Nordigen 接入 | ⏸️ C | `services/bank_sync/engine.py` 428 行 + `api/v1/bank_sync.py` 10+ 端点 + `bank_connections` 表，**未联调**，`BANK_SYNC_ENABLED=false` |
| **链上加密钱包余额同步**（**只填公钥**即自动读余额） | ❌ D | `services/bank_sync/crypto.py` 仅 **55 行 stub**。PRD 2026-05-03 明确：新建"加密钱包"账户时输入钱包地址即可，软件自动列出该地址下所有原生币 + token 余额，无需手动添加资产 |

### 1.3 分类
| 子需求 | 等级 | 证据 / 缺口 |
|---|---|---|
| 多级分类树 + 用户 CRUD | ✅ A | `categories.parent_id`, settings 页 |
| 关键字 / 正则规则匹配 | ✅ A | `categorization_rules` 表 + `categorizer/engine.py` |
| **LLM 兜底分类** | ⏸️ C | PROGRESS Task #7 等待许可，代码内零 LLM 调用 |
| **不确定收件箱（pending）** | ❌ D | `is_pending` 字段存在但 categorizer **不写**，前端**没有专栏**。PRD 原话"不确定的内容放进不确定列表"完全未落地 |
| 用户改正分类 | ✅ A | `PATCH /api/v1/transactions/{id}` |
| **自动学习 / 记忆** | ❌ D | PRD 原话"软件需要记住，下次不要放错"完全未落地。修正分类**不会**反向新建/调整规则；`hit_count` 只统计已有规则命中，无反向管道 |

---

## 2. 资产实时跟踪

| 子需求 | 等级 | 证据 / 缺口 |
|---|---|---|
| 资产种类枚举（CNY/A股/欧美股/crypto/gold/cash等） | ✅ A | `AssetClass` |
| 资产搜索 + 自动识别 | ✅ A | PROGRESS Task #12 |
| 持仓 CRUD | ✅ A | `api/v1/holdings.py` |
| yfinance / CoinGecko / FX 取价**逻辑** | ✅ A | `market_data/engine.py` `refresh_all_market_data()` |
| **价格定时自动刷新** | ⚠️ B | `pyproject.toml` 列了 `apscheduler` 但**代码零引用**；只能手动 `POST /api/v1/market/refresh`。PROGRESS Task #5 标 ✅ 不实 |
| 黄金 GoldAPI | ⚠️ B/E | 配置项在，需用户申请 key |
| 总资产汇总（折算到 base） | ✅ A | `holdings/portfolio/summary` 等接口 |
| **链上钱包余额读取** | ❌ D | 见 1.2 |

---

## 3. 现金流分析

| 子需求 | 等级 | 证据 / 缺口 |
|---|---|---|
| 月度 income/expense/savings/transfer/other | ✅ A | `cash_flow_snapshots` 表 |
| 按分类 / 按账户细分 JSON | ✅ A | `by_category_json`, `by_account_json` |
| 时间序列图表 | ✅ A | `frontend/src/app/analytics/page.tsx` (224 行) |
| **transaction CRUD 后自动重算 snapshot** | ⚠️ B | ARCHITECTURE.md 写"异步触发对应月度 snapshot 重算（debounce 2s）"——**代码里没有任何 hook**。只有手动 `POST /api/v1/cashflow/recompute` |
| 「储蓄」计算口径 | ⚠️ B | 字段存在，定义（净流入 vs 显式入账）需 PRD 二次澄清 |

---

## 4. 产品形态

| 子需求 | 等级 | 备注 |
|---|---|---|
| Web UI（Next.js + shadcn） | ✅ A | 5 大页面均已渲染 |
| PDF 上传区域 | ✅ A | `pdf-import-panel.tsx` |
| 资金变化图表 | ✅ A | dashboard 净值卡 + analytics |
| 记账模块 | ✅ A | transactions 页 |
| App | ❌ D | PRD 明示后续 |

---

## 5. Agent 接口

| 子需求 | 等级 | 证据 / 缺口 |
|---|---|---|
| MCP server 进程 + stdio | ✅ A | `mcp-server/src/finance_mcp/server.py` |
| 6 个 tools 注册 | ✅ A | `get_total_assets / get_transactions / add_transaction / parse_bank_statement / get_cashflow / get_asset_allocation / search_transactions` |
| REST API（同等能力） | ✅ A | `/api/v1/*` |
| **MCP 端到端真实集成测试** | ❌ D | PROGRESS Task #9 未开始 |
| 是否需 `update_transaction_category`（让 Agent 触发学习） | ❌ D | 取决于"自动学习"功能落地后是否暴露给 Agent |

---

## 6. 数据存储

| 子需求 | 等级 | 证据 / 缺口 |
|---|---|---|
| SQLite（WAL）本地 | ✅ A | `data/finance.db` |
| **Notion 同步** | ⏸️ C | `notion_sync/engine.py` + 一键建库 API 完整。**用户从未跑通**，需 ① integration token ② 建库 ③ 启用 |
| **Notion 存储形态决策** | ❌ D | PRD 明示稍后再定 |

---

## 7. 工程化债务（部署/测试/迁移）

| 项 | 等级 | 备注 |
|---|---|---|
| Alembic 迁移版本 | 🛠 E | `db/migrations/` 目录空，启动时仍用 `Base.metadata.create_all()`，生产前必须补 |
| Dockerfile 真实可构建 | 🛠 E | `docker-compose.yml` 在，Dockerfile 标"P0 占位" |
| E2E 测试（Playwright） | 🛠 E | PROGRESS Task #8 未开始 |
| 后端代码 lint / typecheck CI | 🛠 E | 本地能跑 ruff/mypy，CI 未配 |
| Bearer Token 真启用（生产） | 🛠 E | 当前本地通过 `AUTH_DISABLED=true` 绕过，部署前需打开 |

---

## 8. 当前**最危险**的事实

> 这些是用户体感会被"骗"的地方——文档/PROGRESS 写已完成，实际未跑通：

1. **价格永远不会自动更新**（无 scheduler）→ 前端"实时资产"其实是**最近一次手动刷新的快照**
2. **CashFlow 图表数据可能滞后任意久**（无重算 hook）→ 新增交易后图不变，需手动 `/recompute`
3. **PDF 解析器对外宣称的银行覆盖度未经真实样本回归** → 实际上传别家账单大概率挂在 generic fallback
4. **Notion 同步从未跑通过** → 用户期望的"两个地方都有"目前只有一个
5. **加密钱包同步是 55 行 stub** → PRD"加密钱包资产"无法满足

详细优先级排序见 `docx/ROADMAP.md`。
