# Finance Tracker — 项目进度

> 修订日期: 2026-05-06
> 根据"是否真正闭环并验证过"打分；详细分析见 `docx/REQUIREMENT_GAP.md`，剩余优先级见 `docx/ROADMAP.md`。

## 项目信息
- **Repo**: https://github.com/Exsusiai/finance-tracker
- **Git**: `master` branch
- **本地端口**: Backend `8010`, Frontend `3010`（默认 8000 / 3000 已被其他项目占用，详见 `CLAUDE.md`）
- **当前阶段**: ✅ **Sprint 0 + 1 + 2 全部完成**（review V1 R0 + R1 + R2 全部，21 项问题修了 13 项）。`pytest backend/tests/ --ignore=test_api.py` → **53 passed**。**可以 push 到公开 GitHub**。下一步：P1-4（链上钱包）→ P1-1（LLM）→ Sprint 3（启用 GoCardless / Notion 时再修 R3 P1-7/8/P2-2/3/4）

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
| **P1-1a** | LLM 兜底分类（关键词 miss → 调 LLM） | 1 天 | 用户选 LLM provider + 月度预算 |
| **P1-1c** | 知识库注入 LLM（rules + 关键词 + 用户备注 作 few-shot） | 1 天 | P1-1a |
| **P1-1d** | 知识库管理 UI（settings 页表格） | 0.5 天 | P1-1b（已完成） |
| **P1-4** | 链上加密钱包同步（公钥即同步，多链多地址）+ Binance/Bitget CEX API | 3-4 天 | 决策已敲定，可立即开工。详见 `docx/CRYPTO_WALLET_PLAN.md` |
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

按 `docx/ROADMAP.md` 的 **P1 修订执行序列** 推进。剩余**最优先**：

1. **P1-1a + P1-1c LLM fallback 接入**（依赖你回答 LLM provider）
2. **P1-4 链上钱包 + CEX**（决策已敲定，可独立开工）
3. **P1-1d 知识库管理 UI**（依赖 P1-1a/c）
4. **P1-5 储蓄口径**（依赖你给定义）
5. **P1-2 GoCardless** / **P1-3 Notion**（视用户决策）

工程化债务 (P2) 可在功能稳定后插入。
