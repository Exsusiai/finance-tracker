# Finance Tracker

> 个人资金管理与记账系统 · **本地优先 · 单用户 · MCP-ready**

一个为我个人需求自研的财务工具：自动解析银行 PDF 账单 → 智能分类 → 跨账户转账识别 → 实时资产估值 → 现金流分析。同时提供面向 AI Agent 的 MCP 接口。

## 当前状态

✅ **已可用** — 5 家欧洲银行 PDF 解析（AMEX-DE / N26 / Revolut / TFBank / Advanzia）+ 自动分类与学习 + 跨账户转账识别 + 多币种切换 + 7 个 MCP tools。Sprint 0–4 + 2026-05-06/07 UAT 大版本（25 项关键修复、agent 多维架构审查、alembic 启用、22 项回归测试）已合入。

详细进度：`PROGRESS.md` · 最近一日工作日志：`docx/WORKLOG_2026-05-07.md` · 剩余优先级：`docx/ROADMAP.md` · 完整需求：`docx/PRD.md`

## 快速启动

```bash
# 后端
python3 -m venv .venv && .venv/bin/pip install -e backend
cp .env.example .env  # 编辑 token / 端口；本地默认开 AUTH_DISABLED=true
.venv/bin/uvicorn app.main:app --app-dir backend --port 8010

# 前端
cd frontend && npm install && npm run dev -- -p 3010

# MCP server (面向 AI 客户端)
./mcp-server/run.sh
```

打开 http://localhost:3010

## 核心能力速览

| 模块 | 功能要点 |
|---|---|
| **PDF 导入** | 5 家银行真实样本回归通过；Revolut 用 column-aware 按 Money out/in 列定位；必选关联账户；SHA-256 disambiguator 让同银行多月账单 external_id 不冲撞；待确认 inbox 工作流 |
| **自动分类** | 4 大类（转账/支出/收入/收入子项）30+ 子类种子；70+ 规则关键词；用户改分类弹窗三选一传播范围（仅本笔 / 同名一起改 / 以后别再自动归类）+ 备注合并；同名预览数；inbox 自动通过命中规则的 tx |
| **跨账户转账识别** | 评分制配对（金额 50 + 日期 0..30 + 描述提示 0..30 + IBAN +40，阈值 75 自动配对）+ 双向兜底解决无方向描述；MarkTransferDialog 必选转账分类；未配对面板列出所有 single-leg 跨行转账，可手动绑定对手账户（实腿优先 dedup，避免与已存在真腿重复造镜像）；synthetic mirror 后续真腿自动接管；解除绑定 + delete 自动清对手指针；子账户三层识别（关键词 / 用户清单 / 同账户 ±X 金额匹配）；防双计月度支出 |
| **全局重新匹配** | 一键重跑 10 步流水线（孤儿指针清理 → 类型重判 → 规则重分类 → 子账户/IBAN 检测 → 跨账户配对 → orphan 修复 → 内部储蓄补全 → 重入收件箱 → cashflow 重算）；尊重 `source=manual` 与 `user_note`，永不覆盖手动行；type 重判带 audit trail，可单笔撤销 |
| **资产估值** | yfinance + CoinGecko + open.er-api 实时取价；APScheduler 定时刷新；7 种显示币种切换（CNY/USD/EUR/USDT/HKD/JPY/GBP）；FX direct/inverse/三角换算；记账域 fixed EUR、资产域可切换 |
| **现金流分析** | 月度 income/expense/savings/transfer/other snapshot；transaction CRUD 后自动重算；记账页月份导航 ◀▶ + ←/→ 快捷键；分类视图层级化（一级类目 → 二级 → 明细 + 占比条） |
| **Agent 接口** | MCP server 7 tools（已 6 轮回归测试 9 bug 全修）；Anthropic / OpenAI / 任何 stdio MCP 客户端都能接 |

## 技术栈

| 层 | 选型 |
|---|---|
| 后端 | Python 3.11+ / FastAPI / SQLAlchemy 2.x async / SQLite (WAL) / Alembic / APScheduler / pdfplumber |
| 前端 | Next.js 15 / React 19 / Tailwind / shadcn-ui / Recharts / SWR |
| MCP | Anthropic 官方 `mcp` SDK，stdio transport，复用 backend ORM |
| 部署 | Docker Compose（占位中，P2-5 待完善） |

详见 `TECH_STACK.md`。

## 文档导航

| 文件 | 内容 |
|---|---|
| **PROGRESS.md** | 当前进度 + 已完成 / 待开发任务一览 + 执行记录 |
| `CLAUDE.md` | Claude Code 工作上下文（本地端口、约定、约束、alembic 迁移流程） |
| `TECH_STACK.md` | 技术选型详细理由 |
| `docs/ARCHITECTURE.md` | 模块切分 + 关键流程图 |
| `docs/API.md` | REST API endpoints 设计 |
| `docs/SCHEMA.sql` | 完整数据库 schema |
| `docs/BANK_API_DESIGN.md` | GoCardless / Tink 等 PSD2 服务商对比 |
| `docx/PRD.md` | 产品需求文档 |
| `docx/REQUIREMENT_GAP.md` | 需求 vs 实现 gap 分析 |
| `docx/ROADMAP.md` | 优先级 + 剩余开发计划 |
| `docx/CLASSIFICATION_PLAN.md` | 三层分类管道（关键词 → LLM → 用户）方案 |
| `docx/CRYPTO_WALLET_PLAN.md` | 链上钱包同步方案（覆盖链 + indexer 选型） |
| `docx/MCP_TEST_REPORT.md` | MCP server 6 轮回归测试报告 |
| `docx/WORKLOG_2026-05-07.md` | 一日工作日志：12 项新功能 + 25 项 bug fix + 架构审查归档 |
| `backend/alembic/` | 数据库迁移版本链（baseline `1ed07e31cab5`；新 schema 改动走 `alembic revision --autogenerate`） |

## 协议

私有项目（个人使用）。第三方依赖各自遵循其原协议。
