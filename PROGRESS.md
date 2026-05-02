# Finance Tracker — 项目进度

## 项目信息
- **Notion Project ID**: `3534d644-6869-8039-be68-c2b97354d4ad`
- **Repo**: `~/projects/finance-tracker`
- **Git**: `master` branch
- **运行端口**: Backend 8000, Frontend 3002
- **访问**: `ssh -L 3002:localhost:3002 -L 8000:localhost:8000 cortana-box`

## 任务列表 (Notion 同步 2026-05-02)

### P0 — 前端重构（当前执行）
| # | Task | Notion ID | 状态 |
|---|------|-----------|------|
| 1 | 导航重构 + 页面合并 | `3544d644-6869-81ea-b4aa-c67f336ba17d` | ✅ Done |
| 2 | 资产管理页面 - 完整 CRUD | `3544d644-6869-8178-9403-e0e51cbd4612` | ✅ Done |
| 3 | 总览页重构 - 去重 + 资产概览 | `3544d644-6869-815a-bd87-d1426c0dbd08` | ✅ Done |

### P1 — 功能增强
| # | Task | Notion ID | 状态 |
|---|------|-----------|------|
| 4 | 设置页面 - 账户与分类管理 | `3544d644-6869-81be-8115-c52acd892e7e` | Not started |
| 5 | 市场数据 - 定时刷新 + 测试 | `3544d644-6869-8125-807d-c335240ad842` | Not started |
| 6 | PDF 解析引擎 - 测试与修复 | `3544d644-6869-81de-b4f3-d83bcd21092b` | Not started |
| 7 | 智能分类 - LLM Fallback | `3544d644-6869-81de-9898-d46704f34dc5` | Not started |

### P2 — 集成与部署
| # | Task | Notion ID | 状态 |
|---|------|-----------|------|
| 8 | 端到端集成测试 | `3544d644-6869-8143-8351-cec4acc6eebc` | Not started |
| 9 | MCP Server 测试 | `3544d644-6869-81df-b8a9-eb6d5415cad4` | Not started |
| 10 | Docker 部署 | `3544d644-6869-81f8-b671-e32f9dd13dca` | Not started |
| 11 | 银行 API - GoCardless | `3544d644-6869-815f-9c35-c4e3c0046e68` | Not started |

## UI 分析结论 (2026-05-02)
- ❌ 资产管理完全缺失：无法添加资产/持仓，API/hooks 都有但无 UI
- ❌ 导航结构不合理：PDF导入独立为顶级导航项
- ❌ 总览与分析70%重叠：收入/支出/储蓄率/趋势图/饼图都重复
- ❌ 无设置页：无法管理账户和分类
- ✅ 交易列表功能完整
- ✅ PDF上传 UI 可用
- ✅ 分析页图表丰富（但应与总览去重）

## 执行记录
| 日期 | 动作 | 备注 |
|------|------|------|
| 2026-05-01 | P0×3 + P1×7 建站 | Claude Code overnight, ~15000 lines |
| 2026-05-02 | UI 审查 | 发现资产管理缺失、导航/页面重叠问题 |
| 2026-05-02 | 任务重构 | Archive 8 stale tasks, create 11 new tasks |
| 2026-05-02 | Task #2 资产管理页面 | 总资产卡片/持仓表格/资产分布饼图/账户余额面板。Commit: d71f0aa |
| 2026-05-03 | Task #3 总览页重构 | Dashboard去重+资产概览卡片+快速操作，Analytics简化为现金流分析。Commit: f54bd6f |
