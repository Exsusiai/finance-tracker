# Finance Tracker — 项目进度

## 项目信息
- **Notion Project ID**: `3534d644-6869-8039-be68-c2b97354d4ad`
- **Repo**: `~/projects/finance-tracker`
- **Git**: `master` branch, 7 commits (9031230 → 2be349b)
- **运行端口**: Backend 8000, Frontend 3002
- **访问**: `ssh -L 3002:localhost:3002 -L 8000:localhost:8000 cortana-box`

## 整体状态

### ✅ 已完成（功能可用）

| Task | Notion ID | 状态 | 说明 |
|------|-----------|------|------|
| P0 技术选型与项目架构设计 | `3534d644-6869-81c4-b7ff-cd38e0cf840c` | Done | FastAPI + SQLite WAL + Next.js 15 + Recharts + shadcn/ui |
| P0 数据库设计与实现 | `3534d644-6869-8115-a08e-c787a75edc14` | Done | 10 表 + 1 视图, SQLAlchemy ORM, Pydantic v2 schemas |
| P0 核心 API 层开发 | `3534d644-6869-81d7-b3bf-fa098c742bce` | Done | 全部 CRUD 路由 (3000+ 行), token auth |
| P1 Web UI - 总览仪表盘 | `3534d644-6869-812b-84b5-d3b88bd25173` | Done | `/dashboard` — 总资产/饼图/月收支/趋势/最近交易 |
| P1 Web UI - 记账与交易管理 | `3534d644-6869-8168-b694-ceaec8b7a37d` | Done | `/transactions` + `/import` — 列表/筛选/编辑/PDF上传/解析预览 |
| P1 Web UI - 资产与现金流图表 | `3534d644-6869-814b-9ad3-f45f5462b48d` | Done | `/analytics` — 5 图表, CSV/PNG 导出, 暗色模式 |
| P2 Notion 数据同步 | `3534d644-6869-8129-9c23-c2c02862e14c` | Done | 三路同步, 增量更新, 速率限制 |

### 🔶 骨架已写，需测试/完善

| Task | Notion ID | 代码量 | 缺失 |
|------|-----------|--------|------|
| P1 PDF 账单解析引擎 | `3534d644-6869-8131-8487-d26cc35d31ee` | 473 行 | 正则有, 但未用真实 PDF 测试; 银行格式基于猜测 |
| P1 交易智能分类引擎 | `3534d644-6869-81ca-892c-e5aaa2c38362` | 55 行 | 仅规则匹配, 无 AI/LLM fallback |
| P1 现金流分析引擎 | `3534d644-6869-8110-aa26-c236961cb87d` | 268 行 API + 50 行 valuation | API 端点完整, 需前端对接验证 |
| P1 市场数据集成与资产实时估值 | `3534d644-6869-8142-b772-d63e490c178a` | 200 行 | 有 coingecko/yfinance/exchangerate.host 调用, 未测试; 无定时任务 |
| P2 MCP Server | `3534d644-6869-81ba-9048-c1bce19df3b6` | 1007 行 | 代码完整, 未测试/集成 |
| P2 银行 API 直连 | `3534d644-6869-8133-a5b5-e968fc3f3221` | 428 行 | GoCardless 骨架, 未实际对接 |
| P2 Docker 部署 | `3534d644-6869-81dd-8e2f-e636568d8d96` | compose.yml | 无 Dockerfile, 未测试 |

## 执行记录

| 日期 | Task | Commit | 备注 |
|------|------|--------|------|
| 2026-05-01 | 技术选型 + 数据库 + API | f06e4d6 | ~5000 行, 45 files |
| 2026-05-01 | 资产与现金流图表 | d132da6 | 5 图表, 暗色模式 |
| 2026-05-01 | 总览仪表盘 | 1074b97 | Dashboard 完整 |
| 2026-05-02 | 记账与交易管理 | (uncommitted in PROGRESS) | /transactions + /import |
| 2026-05-02 | Auth 修复 | — | .env token + 前端自动注入 |

## 当前问题
1. 前端 401 已修复（dev-token + layout.tsx 自动注入）
2. PROGRESS.md 与 Notion 状态不同步（Notion 上多数标记 Done，但实际是骨架）
3. 市场数据任务在 Notion 上状态为 "blocked"，需解除

## 下一步优先级
1. 市场数据集成测试 + APScheduler 定时刷新
2. PDF 解析引擎真实 PDF 测试 + 修复
3. 智能分类引擎 LLM fallback
4. 端到端集成测试（PDF→解析→分类→展示）
5. MCP Server 测试
6. Docker 部署验证
