# LLM 智能分类系统 — 实施计划

> 起草日期: 2026-05-08
> 分支: `feat/llm-classification`
> 取代 `docx/CLASSIFICATION_PLAN.md` 中关于 LLM 接入的草案部分（保留为历史背景）

## 0. 范围与决策（用户确认）

| # | 议题 | 决策 |
|---|---|---|
| **D1** | 「污染」机制 | `categorization_rules` 加 `requires_llm BOOL` 列；用户写备注 → 同 keyword 规则置 True；不删旧规则（保留 keyword 命中索引） |
| **D2** | L1 置信度模型 | 二态：rule 命中 + `requires_llm=False` ⇒ 高置信，直接落；其余皆走 L2 |
| **D3** | 知识库 schema | `categorization_notes(id, category_id, trigger_text, note_text, source_tx_id, usage_count, enabled, created_at, updated_at)`；trigger_text 自然语言；category_id 强类型 |
| **D4** | 联网搜索 | 用 Gemini 内置 Google Search grounding；`settings.llm_use_grounding` 默认 True |
| **D5** | 调用时机 | **异步**：PDF 上传立即返回；后台 worker 批量跑（`asyncio.create_task` 起步，10 条/批，无外部队列依赖） |
| **D6** | Provider + 默认值 | 当前**仅** Gemini；未来兼容 OpenAI/Anthropic（先做 Provider Protocol）。默认 `gemini-2.5-flash` / 月度预算 5 USD / 阈值 0.7 / grounding=True |

**Provider**：仅 `google-genai` SDK。API key 从 `.env` 读 `GEMINI_API_KEY`，启动若 `llm_enabled=True` 但 key 缺失则 warn 并禁用 LLM。

**生效来源**：仅 `source ∈ {pdf_import, bank_api}` 走 LLM；`manual` / `mcp_agent` 跳过自动分类（用户既然亲自录入，分类已明确）。

---

## 1. 流程总览

```
入站 tx → (manual/mcp_agent? skip) → amount norm → FX → L1 keyword
        → 命中 & !requires_llm → 落库
        → 否则                 → L2 LLM
              Step A: 知识库 top-N 注入 prompt → Gemini
              Step B: 知识库不够 + grounding=True → Gemini Google Search
              → conf ≥ 阈值: 落库 (method=llm, confidence, reason)
              → conf <  阈值: Inbox（带 LLM 推荐、理由、是否搜索）
        → 用户改分类 + 写 user_note
              → learn_from_user_assignment（已有：建/强化 keyword 规则）
              → if user_note 非空:
                    INSERT categorization_notes
                    UPDATE 同 keyword 规则 SET requires_llm=True
```

---

## 2. Schema 改动

### 2.1 Alembic revision: `add_llm_classification`

```sql
-- 1) categorization_rules 加列
ALTER TABLE categorization_rules ADD COLUMN requires_llm BOOLEAN DEFAULT 0 NOT NULL;
CREATE INDEX ix_rules_requires_llm ON categorization_rules(requires_llm);

-- 2) transactions 加列
ALTER TABLE transactions ADD COLUMN categorization_method TEXT;  -- 'rule' | 'llm' | 'manual' | NULL
ALTER TABLE transactions ADD COLUMN categorization_confidence REAL;  -- 0..1, llm only
ALTER TABLE transactions ADD COLUMN llm_reason TEXT;  -- LLM 给的理由

-- 3) categorization_notes（知识库）
CREATE TABLE categorization_notes (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    category_id INTEGER NOT NULL REFERENCES categories(id),
    trigger_text TEXT NOT NULL,           -- 自然语言触发条件
    note_text TEXT NOT NULL,              -- 备注内容（同 trigger 描述也行）
    source_transaction_id INTEGER REFERENCES transactions(id) ON DELETE SET NULL,
    usage_count INTEGER DEFAULT 0 NOT NULL,
    enabled BOOLEAN DEFAULT 1 NOT NULL,
    created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL
);
CREATE INDEX ix_notes_category ON categorization_notes(category_id);
CREATE INDEX ix_notes_enabled ON categorization_notes(enabled);

-- 4) app_settings KV 表（如尚不存在）
CREATE TABLE IF NOT EXISTS app_settings (
    key TEXT PRIMARY KEY,
    value TEXT NOT NULL,
    updated_at TEXT NOT NULL
);
```

### 2.2 默认 settings 行（lifespan 幂等 INSERT OR IGNORE）

| key | default | 说明 |
|---|---|---|
| `llm_enabled` | `false` | 总开关；启动时若 GEMINI_API_KEY 存在则可手动开启 |
| `llm_provider` | `gemini` | 暂只支持 gemini |
| `llm_model` | `gemini-2.5-flash` | |
| `llm_monthly_usd_budget` | `5.0` | |
| `llm_confidence_threshold` | `0.7` | |
| `llm_use_grounding` | `true` | Google Search grounding |
| `llm_max_notes_in_prompt` | `20` | top-N 知识库条目 |

---

## 3. 后端模块

### 3.1 `backend/app/services/llm/`

```
llm/
├── __init__.py            # 公开 API: classify_with_llm
├── provider.py            # Protocol: classify(...) -> ClassificationResult
├── gemini.py              # GeminiProvider 实现（google-genai SDK）
├── prompt.py              # build_classification_prompt(tx, notes, categories)
├── cost_tracker.py        # 月度成本累计 + 预算守门
└── classifier.py          # 业务编排：检索 notes → 构造 prompt → call provider → 写 tx
```

**Provider Protocol**:
```python
class LLMProvider(Protocol):
    async def classify(
        self,
        prompt: str,
        *,
        use_grounding: bool,
        timeout_s: float = 15.0,
    ) -> ClassificationResult: ...

@dataclass(frozen=True)
class ClassificationResult:
    category_path: str | None       # "住家/房租"; None = 弃权
    confidence: float               # 0..1
    reason: str
    used_search: bool               # 是否触发了 grounding
    input_tokens: int
    output_tokens: int
    cost_usd: float
```

### 3.2 `services/categorizer/engine.py` 变更（最小改动）

`categorize_transaction` 当前返回 `bool`。改为返回 `MatchResult`：

```python
@dataclass
class MatchResult:
    matched: bool
    rule_id: int | None
    requires_llm: bool   # 命中规则的 requires_llm 字段
```

调用方（`services/ingestion/__init__.py`）按 `matched && !requires_llm` 决定是否短路。

### 3.3 `services/ingestion/__init__.py` 变更

Step 2 现有 L1 分支后追加 L2 分支：

```python
should_call_llm = (not match.matched) or (match.matched and match.requires_llm)
if should_call_llm and tx.source in {"pdf_import", "bank_api"}:
    # 异步派遣（不阻塞 ingestion）
    asyncio.create_task(_run_llm_classification(tx.id))
```

`_run_llm_classification(tx_id)` 在新 session 内：
1. 重新 load tx
2. 调 `services.llm.classifier.classify_with_llm(db, tx)`
3. 命中阈值 → 写 category_id / categorization_method='llm' / confidence / llm_reason / is_pending=False
4. 不命中 → is_pending=True + 在 metadata 写 LLM 推荐供 inbox 显示
5. recompute cashflow

> **批量优化**：先单条调；若验证 OK 后续合并 10 条/批用 Gemini batch。

### 3.4 用户备注回灌

`services/categorizer/engine.py::learn_from_user_assignment` 末尾扩展（API 层 `transactions.confirm_inbox` / `update_transaction` 传 `user_note` 时）：

```python
if user_note:
    # 1) 写知识库
    note = CategorizationNote(
        category_id=new_category_id,
        trigger_text=user_note,           # 用户写的备注作触发文本
        note_text=user_note,
        source_transaction_id=tx.id,
        ...
    )
    db.add(note)

    # 2) 标记同 keyword 的所有规则 requires_llm=True
    derived = derive_keyword_for_tx(tx)
    if derived:
        field, keyword = derived
        await db.execute(
            update(CategorizationRule)
            .where(CategorizationRule.pattern.ilike(keyword),
                   CategorizationRule.field == field)
            .values(requires_llm=True)
        )
```

### 3.5 新 API 端点

| 方法 | 路径 | 说明 |
|---|---|---|
| GET | `/api/v1/categorization-notes` | 列表（支持 category_id / enabled 过滤） |
| POST | `/api/v1/categorization-notes` | 创建 |
| PATCH | `/api/v1/categorization-notes/{id}` | 编辑 |
| DELETE | `/api/v1/categorization-notes/{id}` | 软删（实为 enabled=False） |
| GET | `/api/v1/llm/settings` | 读 7 个 KV |
| PUT | `/api/v1/llm/settings` | 更新（部分） |
| GET | `/api/v1/llm/cost` | 当月已用 USD / 预算 / 剩余 |

---

## 4. 前端模块

### 4.1 Settings 页新增两个 section

- **「智能分类」表单**：
  - 总开关 `llm_enabled`
  - provider（只读 `gemini`）+ model 选择
  - threshold（滑块 0–1，step 0.05）
  - monthly_usd_budget（数字输入）
  - use_grounding 开关
  - 「当月已用 / 预算」实时进度条
- **「知识库」表格**：
  - 列：trigger_text / category / note_text / 命中次数 / 来源 tx 链接 / 启用开关 / 操作
  - 顶部「+ 新建条目」
  - 编辑用 dialog
  - 空状态文案：「在 Inbox 改分类时写备注会自动入库」

### 4.2 Inbox 行内 LLM 推荐展示

当 `tx.metadata_json.llm_suggestion` 存在时：
- 行内显示 ✨「LLM 推荐：餐饮（置信 0.65，理由：商户名称匹配 wolt food delivery，已联网核实）」
- 「采纳」按钮一键应用
- 仍可走原有的手动选分类 + 备注流程

---

## 5. Prompt 模板（`services/llm/prompt.py`）

```
你是一个个人财务记账助手。请把这条银行交易归到下面给定分类树的某个二级类目下。

# 分类树（一级 / 二级，仅 expense/income/transfer 三大 kind）
{category_tree}

# 已生效的关键词规则（参考；命中过这些 keyword 的会自动走 L1）
{rule_keywords_summary}

# 用户维护的知识库（最相关的 N 条）
{notes_block}

# 待分类交易
描述: {description}
原始描述: {raw_description}
对方: {counterparty}
金额: {amount} {currency}
日期: {occurred_at}
账户: {account_name} ({account_currency})
来源: {source}

请输出 JSON（不要 markdown 代码块包装）：
{
  "category_path": "一级名/二级名",   // 例如 "住家/房租"，无法判断填 null
  "confidence": 0.0-1.0,
  "reason": "简短说明匹配依据，命中知识库哪条 / 联网核实结果",
  "used_search": true|false
}
```

知识库相关性：用 description token 重合度倒排取 top-`llm_max_notes_in_prompt`（默认 20）。MVP 不引入 embedding。

---

## 6. 安全 / 成本 / 韧性

- **API key**：`.env` 的 `GEMINI_API_KEY`，已纳入 .gitignore；`.env.example` 加占位。
- **成本累计**：`app_settings.llm_monthly_cost_usd_YYYY_MM` 累加。预算超额 → 自动 disable 直到下月。
- **超时**：单次 15s。失败 → fallback 进 inbox（不阻塞）。
- **JSON 解析容错**：失败也 fallback。
- **PII**：原始 description 会发给 Google；用户在 settings 上明确告知（一行小字）。

---

## 7. 测试计划

### 单元
- `test_llm_provider_gemini.py` — mock SDK 返回，验证 prompt 拼装、JSON 解析、cost 计算
- `test_llm_classifier.py` — 检索 top-N、阈值守门、写 tx 字段
- `test_cost_tracker.py` — 月度累加、跨月切换、预算超额拒绝
- `test_categorization_notes_api.py` — CRUD + 软删
- `test_requires_llm_flag.py` — 用户写备注 → 同 keyword 规则 requires_llm=True

### 集成
- PDF 上传 → 1 笔 PayPal 2.99 EUR + 1 笔常规 Wolt
  - 假定 wolt 已有 L1 规则 ⇒ 直接落
  - PayPal 在测试 setup 中已被打污染 ⇒ 走 LLM（用 mock provider 返回 "订阅 X / 0.85"）⇒ 落库 method=llm
- 用户对 PayPal 改分类 + 写「PayPal 每月 2.99 是订阅 X」⇒ 知识库新增 + 规则 requires_llm=True

### Mock 策略
- 默认 mock Gemini；只在 `RUN_LIVE_LLM=1` 环境变量下跑真实调用（开发本地手动验证）。

---

## 8. 实施顺序

1. **Phase 1（约 0.5 天）**：alembic 迁移 + models + 基础 LLM 模块 + cost tracker + 单测
2. **Phase 2（约 1 天）**：ingestion 接入 + classifier 编排 + 知识库回灌 + 集成测试
3. **Phase 3（约 1 天）**：API endpoints + 前端 Settings/知识库 UI + Inbox LLM 推荐
4. **Phase 4（约 0.5 天）**：完整测试 + 文档同步（README / PROGRESS / CLAUDE.md）

总计约 3 天。

---

## 9. 后续可选扩展（**不在本次范围**）

- OpenAI / Anthropic provider
- Sentence-transformers embedding 替换 keyword 倒排
- Gemini batch API 真批
- 单笔 LLM 调用结果缓存（description hash → 24h TTL）
