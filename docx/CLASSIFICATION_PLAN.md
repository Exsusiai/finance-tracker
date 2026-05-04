# 分类系统设计 (P1-1 演进版)

> 起源：用户 2026-05-04 提出的三层分类管道 + 备注 + 知识库需求
>
> 当前已实现（P0-3 / P0-4，2026-05-03）：
> - 关键词 / 正则规则匹配（`categorization_rules` 表）
> - 反向学习：用户改分类 → 自动新建 / 加强规则
> - Inbox 工作流（前后端均已就绪）

本文档描述把"关键词单层"演进为"**关键词 → LLM → 用户**"三层管道的方案。

---

## 1. 三层管道概览

```
┌──────────────────┐
│  入站交易 tx     │  (PDF import / bank API / manual)
└────────┬─────────┘
         │
         ▼
┌──────────────────────────┐    HIT     ┌────────────────┐
│  L1  规则关键词匹配       │ ─────────▶ │ 写 category_id │
│  (categorize_transaction) │            │ is_pending=False│
└────────┬─────────────────┘            └────────────────┘
         │ MISS
         ▼
┌──────────────────────────┐    高置信   ┌────────────────┐
│  L2  LLM 分类             │ ─────────▶ │ 同上 + LLM 标记 │
│  + 注入「分类知识库」      │            └────────────────┘
└────────┬─────────────────┘
         │ 低置信 / LLM 弃权
         ▼
┌──────────────────────────┐
│  L3  Inbox 待用户确认     │
│  (可写备注 → 回灌知识库)  │
└──────────────────────────┘
```

---

## 2. 数据模型扩展（Schema 改动）

### 2.1 `transactions` 加字段
| 字段 | 类型 | 说明 |
|---|---|---|
| `user_note` | TEXT | 用户在 inbox 确认时填的备注（新需求）。会被回灌到知识库 |
| `categorization_method` | TEXT | `rule` / `llm` / `manual` 之一 — 用于审计 / 复盘 |
| `categorization_confidence` | REAL | LLM 给出的置信度（0–1），仅 `method='llm'` 时有值 |

### 2.2 新表 `categorization_notes`（知识库的备注层）
| 字段 | 说明 |
|---|---|
| `id` | PK |
| `category_id` | FK → categories |
| `keyword_or_pattern` | 触发该备注的关键字/特征（可为空，泛指该分类） |
| `note_text` | 用户写的备注内容 |
| `source_transaction_id` | FK → transactions（追溯） |
| `created_at` | |
| `usage_count` | 被注入 LLM 上下文的次数（用于回收冷数据） |

> 也可不建新表，直接复用 `transactions.user_note` + 检索时 join。但单独建表能让后续做"全局笔记 / 与具体 tx 解耦"更灵活。**默认采纳新表**。

---

## 3. LLM 调用规范

### 3.1 输入构造
```text
你是一个个人财务记账助手。请把这条银行交易归到下面给定分类树的某个二级类目下。

# 分类树（一级 / 二级）
住家
  房租 / 房屋维修 / 清洁费 / 家具家电
日常生活
  餐饮 / 超市 / 咖啡饮料 / 购物
  ...（实时拉取用户当前 categories 表）

# 已有的关键词规则（参考，命中过这些 keyword 的会自动走 L1）
- 餐饮: wolt, uber eats, restaurant, ...
- 超市: rewe, edeka, ...
  ...

# 用户历史备注（最近 20 条最相关）
- 「Patricia Moubarak」→ 住家/房租（用户备注：每月转给房东 Patricia）
- 「Schivelbeiner Str」→ 住家/房屋维修（用户备注：物业服务地址）
- ...

# 待分类交易
描述: PATRICIA MOUBARAK
金额: -985.00 EUR
日期: 2026-04-02
账户: N26

请输出 JSON: {"category_path": "住家/房租", "confidence": 0.92, "reason": "用户历史备注..."}
若无法判断，输出 {"category_path": null, "confidence": 0.0, "reason": "..."}.
```

### 3.2 模型选择
- **首选**：Claude Haiku 4.5（便宜、快、对中英文混合好）
- **备选**：本地 Ollama（如 qwen2.5:7b） — 完全离线、零成本，但准确率较低
- **对比指标**：在用户已有的 inbox 样本上跑，看人工对照的命中率

### 3.3 速率与成本控制
- **批量请求**：一次批 10–20 条（Anthropic Batch API）
- **缓存**：对相同 (description hash) 的请求缓存 24 h
- **预算硬上限**：每月 N 美元（配置项），超限则跳过 L2 直接进 L3
- **prompt 缓存**：分类树 + 关键词 + 备注上下文用 Anthropic prompt caching，单条增量 token 极低

### 3.4 知识库选取策略（"最近 20 条最相关"如何选）
- **简单版**：按 description 关键词倒排 → 取重合 token 最多的 N 条
- **进阶版**：本地 sentence-transformers 嵌入 + 余弦相似度（需新依赖）
- **MVP 选简单版**

---

## 4. 用户备注体验

- Inbox 行内"确认"前可点「+ 备注」展开输入框
- 备注非必填；不填则只回灌关键词规则（即当前行为）
- 已有备注的分类在下次 inbox 选项里加 ⓘ 图标，hover 展示备注内容
- Settings 页加「知识库」section：表格列出所有备注 + 来源 tx + 使用次数；可编辑 / 删除

---

## 5. 实施分期

### 5.1 Phase 1（约 1 天）
- 加 `transactions.user_note` 字段（不建新表，最简）
- Inbox UI 加「备注」输入框
- 后端 `learn_from_user_assignment` 扩展：备注一并存

### 5.2 Phase 2（约 2-3 天）
- 加 LLM 客户端封装（Anthropic SDK）+ 配置项 `LLM_PROVIDER` / `LLM_MODEL` / `LLM_MONTHLY_USD_BUDGET`
- 实现"知识库相关条目"检索（简单 keyword 倒排）
- L1 miss 时调 L2 LLM；按置信度阈值（>= 0.7）写入分类
- 失败 / 低置信 → 进 inbox（标 method='llm' confidence=X）
- Inbox UI 显示 LLM 推荐 + 置信度 + 推理理由

### 5.3 Phase 3（约 1 天）
- 抽备注表 `categorization_notes`（如果 Phase 1 用单字段证明不够灵活）
- Settings 页加「知识库」管理 UI
- 加 LLM 月度成本统计 + 预算告警

---

## 6. 已确定的决策（用户答复 2026-05-04）

| # | 议题 | 决策 |
|---|---|---|
| 1 | LLM 提供商 + 月度预算 | **后期再定**。当前阶段先把 schema / config 字段位预留好（`LLM_PROVIDER` / `LLM_MODEL` / `LLM_MONTHLY_USD_BUDGET`），等真正动手 P1-1a 时再选 |
| 2 | 置信度阈值 | **放进 settings 表**（不是 env var），方便边测边调。前端 settings 页加滑块或数字输入。后端读 `app_settings.llm_confidence_threshold`，默认 0.7 |
| 3 | 哪些来源走 LLM | **仅 PDF / bank_api 来源走 LLM**。`source='manual'` 与 `source='mcp_agent'` 不走 LLM（用户既然自己输入，分类信息已明确） |

> 因此：动 P1-1 时落地点就是：① 在 `app_settings`（或新建表）加置信度阈值 + LLM provider / model / budget 三列字段
> ② categorizer 在 source ∈ {pdf_import, bank_api} 且 L1 miss 时才调 LLM
> ③ 阈值低于 settings 值 → 进 inbox
