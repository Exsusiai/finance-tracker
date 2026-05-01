# 技术选型 (TECH_STACK)

> Finance Tracker — 个人资金管理与记账系统
> 决策日期: 2026-05-01

本文档记录 P0 阶段所有技术选型与决策依据。所有选型遵循 **本地优先 / 单用户 / 务实不过度设计** 三大原则。

---

## 1. 后端框架: **Python 3.11 + FastAPI**

**选择理由**

- **PDF 解析生态最强**: `pdfplumber` / `pypdf` / `camelot` 都是 Python 库,中文银行账单解析有现成方案。
- **金融/数据生态**: `pandas` / `numpy` / `Decimal` 处理多币种金额、时间序列分析得心应手。
- **市场数据 SDK**: `yfinance` (股票)、`pycoingecko` (加密货币)、`forex-python` 全部 Python 原生。
- **MCP 官方 SDK 一流**: Anthropic 官方 `mcp` 包就是 Python,与后端可共享数据层。
- **FastAPI 优势**: 原生 async、自动 OpenAPI 文档、Pydantic v2 数据校验、性能足够个人使用。

**淘汰选项**

- Node/TypeScript: PDF 解析弱、金融库少。
- Go: 生态太薄,PDF 中文表格解析无成熟库。

---

## 2. 数据库: **SQLite (WAL 模式) + SQLAlchemy 2.x**

**选择理由 (核心决策)**

需求规模评估:
- 单用户、本地部署、并发极低 (Web UI + MCP 两个客户端)
- 数据量级: 月 100~500 条交易,5~10 年累计 < 100k 行
- 查询模式: 时间序列聚合 + 简单过滤,不需要复杂 JOIN/CTE
- 部署: Ubuntu 自部署,运维要尽量轻

**SQLite 胜出的关键点**

| 维度 | SQLite | PostgreSQL |
|------|--------|-----------|
| 部署复杂度 | 一个文件,零配置 | 需要 Docker 服务 + 卷管理 + 用户/密码 |
| 备份恢复 | `cp finance.db backup.db` | `pg_dump` / `pg_restore` |
| 单用户写并发 | WAL 模式下足够 (读不阻塞写) | 真正的多写并发 (本场景用不到) |
| Numeric 精度 | 用 `NUMERIC` 仿射类型 + Python `Decimal` | 原生 `NUMERIC` |
| JSON 支持 | `JSON1` 扩展 (查询语法略弱) | `JSONB` 强大 |
| 时序查询 | 窗口函数齐全,够用 | 更强,但本场景用不到 |
| 学习/排错 | 工具简单 (DB Browser, sqlite3 CLI) | 工具丰富但更重 |

**关键护栏**

- 通过 SQLAlchemy 2.x ORM 屏蔽方言差异 → 未来如需切换 Postgres,代码改动 < 5%
- 启动时强制开启 `PRAGMA journal_mode=WAL; PRAGMA foreign_keys=ON; PRAGMA synchronous=NORMAL;`
- 所有金额字段一律使用 `NUMERIC(20, 8)` (支持加密货币 8 位精度) + Python `Decimal`,**禁止 float**
- Alembic 管理迁移,从第一天起就有版本化 schema

---

## 3. 前端: **Next.js 15 (App Router) + TypeScript + Tailwind + shadcn/ui + Recharts**

**选择理由**

- **Next.js**: 全栈一体,SSR/SSG 灵活,部署简单 (`next start` + Docker)。本地工具不需要 Vercel,自部署即可。
- **TypeScript**: 与后端 OpenAPI schema 通过 `openapi-typescript` 生成类型,全链路类型安全。
- **Tailwind + shadcn/ui**: 复制即用的组件库,深色模式开箱即用,UI 一致性好。
- **Recharts**: 声明式 API、与 React 契合度最高、对资产趋势/现金流堆叠图等场景够用。重型场景再上 ECharts。
- **i18n**: 默认中文,通过 `next-intl` 支持多语言。

**淘汰选项**

- Vue/Nuxt: 个人偏好与生态考虑选 React。
- 纯 SPA (Vite + React): 不需要 SSR,但 Next.js 的文件路由 + Server Actions 长期更省心。
- ECharts: 配置式 API 较重,本期 Recharts 够用。

---

## 4. PDF 账单解析: **pdfplumber (主) + pypdf (备) + 银行专用解析器**

**策略**

中国/欧洲银行账单 PDF 结构差异极大,**没有银行通用解析器**,必须按行做适配。

```
services/pdf_parser/
├── base.py              # 抽象 BankStatementParser 基类
├── detector.py          # 通过文本特征自动识别银行
├── parsers/
│   ├── icbc.py          # 工商银行
│   ├── ccb.py           # 建设银行
│   ├── cmb.py           # 招商银行
│   ├── boc.py           # 中国银行
│   ├── n26.py           # 欧洲 N26
│   ├── revolut.py       # Revolut
│   └── generic.py       # 通用兜底 (启发式)
```

**库选择**

- **pdfplumber**: 主力,表格抽取最强,支持中文。
- **pypdf**: 部分加密 PDF / 元数据兜底。
- **camelot-py**: 复杂表格场景备用 (依赖较重,按需引入)。
- **pdf2image + tesseract**: 图片型 PDF 兜底 OCR (P1 阶段)。

---

## 5. 市场数据源

| 资产类型 | 数据源 | 库 / 端点 | 限频 |
|---------|--------|-----------|------|
| 加密货币 | CoinGecko Free | `pycoingecko` | 30 calls/min |
| A 股 | Yahoo Finance | `yfinance` (代码格式 `600519.SS` / `000001.SZ`) | 宽松 |
| 欧股/美股 | Yahoo Finance | `yfinance` | 宽松 |
| 汇率 | exchangerate.host (免费) + 备份 open.er-api.com | `httpx` 直接请求 | 充足 |
| 黄金 (XAU) | metals.live / goldapi.io 备份 | `httpx` | 看具体源 |
| RMB 现金 | 无需取价 (=1) | — | — |

**架构原则**

- 所有市场数据 **抽象为 `MarketDataProvider` 接口**,价格实现可插拔
- 后端通过 APScheduler 定时拉取 (5 min ~ 1 h 不等),写入 `market_prices` 表
- 估值计算只读本地表 → 离线也能工作

---

## 6. MCP Server: **Python `mcp` SDK (官方)**

**选择理由**

- Anthropic 官方 SDK,生态/文档质量最高。
- 与后端同语言 → 共享 SQLAlchemy models / services,无需 HTTP 跨进程。
- Cortana / OpenClaw 等本地 Agent 客户端均支持 stdio MCP transport。

**部署形态**

- MCP server 作为独立进程 (`mcp-server/`),通过 stdio 暴露 tools。
- Tools: `query_balance`, `query_transactions`, `import_pdf_statement`, `query_asset_value`, `categorize_transaction`, `get_cashflow_summary`。
- **共享数据库连接**: 直接读写同一份 `finance.db`,WAL 模式下安全。

---

## 7. 部署与运维

- **Docker Compose** 编排 3 个服务: `backend` (FastAPI + uvicorn)、`frontend` (Next.js)、可选 `mcp-server`
- SQLite 数据文件挂载到宿主机 `./data/finance.db`,容器重启不丢
- 反向代理: 宿主机 nginx 或 Caddy 终结 TLS,容器内只跑 HTTP
- 备份: cron + `sqlite3 .backup` 命令每日全量到 `./data/backups/`
- 日志: structlog → 文件 + stderr,无需 ELK

---

## 8. 鉴权策略

**单用户 → 简单 API Token**

- 启动时从 `FINANCE_TRACKER_API_TOKEN` 环境变量读取
- 所有 API 端点要求 `Authorization: Bearer <token>` 头
- 无登录页/无用户表/无 session
- MCP server 在本地进程内调用 → 不需要 token

---

## 9. 核心依赖清单

**Backend (Python)**

```
fastapi >= 0.115
uvicorn[standard]
sqlalchemy >= 2.0
alembic
pydantic >= 2.6
pydantic-settings
pdfplumber
pypdf
yfinance
pycoingecko
httpx
apscheduler
structlog
python-multipart    # PDF 上传
```

**Frontend (TypeScript)**

```
next ^15
react ^19
typescript
tailwindcss
recharts
swr / @tanstack/react-query
zod
next-intl
@radix-ui/* (shadcn/ui 依赖)
lucide-react
```

**MCP Server (Python)**

```
mcp >= 1.0
fastapi  # 共享 backend 代码
sqlalchemy
```

---

## 10. 决策摘要表

| 项目 | 选择 | 一句话理由 |
|------|------|-----------|
| 后端 | Python 3.11 + FastAPI | PDF/金融/MCP 三个生态都最强 |
| 数据库 | SQLite (WAL) + SQLAlchemy | 单用户场景下零运维 |
| 前端 | Next.js + Recharts + shadcn | 全栈一体 + 类型安全 |
| PDF | pdfplumber + 银行专用解析器 | 银行账单格式碎片化无法通用 |
| 行情 | yfinance + CoinGecko + ER-API | 全免费、覆盖所需资产类型 |
| MCP | 官方 Python SDK | 共享后端数据层零成本 |
| 部署 | Docker Compose + 卷挂载 SQLite | Ubuntu 自部署最简方案 |
| 鉴权 | API Token (env var) | 单用户无需复杂鉴权 |
