"""Prompt construction for the LLM classifier.

Kept as a pure function so unit tests can assert exact text without
spinning up a provider. The LLM is asked to return JSON; we don't use
function calling to stay portable across providers.
"""
from __future__ import annotations

from decimal import Decimal
from typing import Iterable

from app.models import CategorizationNote, CategorizationRule, Category, Transaction


def _category_tree(categories: Iterable[Category]) -> str:
    """Render a 2-level category tree grouped by kind."""
    by_kind: dict[str, dict[str, list[str]]] = {"expense": {}, "income": {}, "transfer": {}}
    parents: dict[int, Category] = {c.id: c for c in categories if c.parent_id is None}

    for c in categories:
        if c.parent_id is None:
            by_kind.setdefault(c.kind, {}).setdefault(c.name, [])
        else:
            parent = parents.get(c.parent_id)
            if parent is None:
                continue
            by_kind.setdefault(parent.kind, {}).setdefault(parent.name, []).append(c.name)

    lines: list[str] = []
    for kind in ("expense", "income", "transfer"):
        if not by_kind.get(kind):
            continue
        lines.append(f"## {kind}")
        for parent_name, children in sorted(by_kind[kind].items()):
            lines.append(f"- {parent_name}")
            for child in children:
                lines.append(f"  - {child}")
    return "\n".join(lines)


def _rule_summary(rules: Iterable[CategorizationRule], categories_by_id: dict[int, Category]) -> str:
    """Group enabled keyword rules by category — kept short."""
    by_cat: dict[str, list[str]] = {}
    for rule in rules:
        if not rule.enabled:
            continue
        cat = categories_by_id.get(rule.category_id)
        if cat is None:
            continue
        parent = categories_by_id.get(cat.parent_id) if cat.parent_id else None
        path = f"{parent.name}/{cat.name}" if parent else cat.name
        by_cat.setdefault(path, []).append(rule.pattern)

    lines: list[str] = []
    for path, patterns in sorted(by_cat.items()):
        if len(patterns) > 12:
            patterns = patterns[:12] + ["..."]
        lines.append(f"- {path}: {', '.join(patterns)}")
    return "\n".join(lines) if lines else "(none yet)"


def _notes_block(notes: Iterable[CategorizationNote], categories_by_id: dict[int, Category]) -> str:
    items = list(notes)
    if not items:
        return "(empty — no user notes yet)"
    lines: list[str] = []
    for n in items:
        cat = categories_by_id.get(n.category_id)
        path = "(unknown)"
        if cat is not None:
            parent = categories_by_id.get(cat.parent_id) if cat.parent_id else None
            path = f"{parent.name}/{cat.name}" if parent else cat.name
        lines.append(f"- 「{n.trigger_text}」→ {path}（备注: {n.note_text}）")
    return "\n".join(lines)


def _format_amount(amount: Decimal | float | None, currency: str) -> str:
    if amount is None:
        return "?"
    return f"{Decimal(str(amount)):.2f} {currency}"


def build_classification_prompt(
    tx: Transaction,
    *,
    categories: list[Category],
    rules: list[CategorizationRule],
    notes: list[CategorizationNote],
    account_name: str,
    account_currency: str,
) -> str:
    """Render the full prompt for one transaction.

    The format is fixed so tests can pin behaviour. The model is told to
    output JSON — see `services.llm.gemini._parse_classification` for the
    expected shape.
    """
    categories_by_id = {c.id: c for c in categories}
    tree = _category_tree(categories)
    rule_summary = _rule_summary(rules, categories_by_id)
    notes_section = _notes_block(notes, categories_by_id)

    return f"""你是一个个人财务记账助手。请把下面这条银行交易归到给定分类树的某个二级类目下。

# 分类树（仅可在此树内选择，按一级名/二级名输出路径）
{tree}

# 已生效的关键词规则（仅供参考；命中过这些 keyword 的会自动走 L1，未命中的才会到你这里）
{rule_summary}

# 用户维护的知识库（最相关条目）
{notes_section}

# 待分类交易
- 描述: {tx.description or ''}
- 原始描述: {tx.raw_description or ''}
- 对方: {tx.counterparty or ''}
- 金额: {_format_amount(tx.amount, tx.currency)}
- 日期: {tx.occurred_at}
- 账户: {account_name} ({account_currency})
- 来源: {tx.source}
- 类型: {tx.type}

请按以下规则判断：
1. 优先查阅"用户维护的知识库"。若知识库中有条目能匹配本交易（关键词、金额、日期模式等），直接采纳并标注 used_search=false。
2. 若知识库不足，可基于交易描述/对方进行联网搜索，确认商户性质后再分类（标 used_search=true）。
3. 若仍无法判断，输出 category_path=null。

请严格输出 JSON（不要 markdown 代码块包装），字段：
{{
  "category_path": "一级名/二级名",
  "confidence": 0.0,
  "reason": "简短说明依据，命中知识库哪条 / 联网核实结果",
  "used_search": false
}}
"""
