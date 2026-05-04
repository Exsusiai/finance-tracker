"""Seed default expense categories + a starter pack of matching rules.

User-provided taxonomy (2026-05-03):
  住家 / 日常生活 / 个人支出 / 交通 / 出行与旅游 / 订阅费用 / 娱乐活动 / 保险 / 教育

The seeder is **idempotent**: it only inserts rows that don't exist yet (matched
by `name + kind + parent_id`). Users may freely edit / delete / extend the
taxonomy afterwards — re-running this seeder will not clobber their changes.
"""

from __future__ import annotations

import structlog
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.models import CategorizationRule, Category

logger = structlog.get_logger(__name__)


# (parent_name, [(child_name, [keyword1, keyword2, ...]), ...])
_TAXONOMY: list[tuple[str, list[tuple[str, list[str]]]]] = [
    ("住家", [
        ("房租", ["miete", "rent", "房租"]),
        ("房屋维修", ["reparatur", "repair", "维修"]),
        ("清洁费", ["reinigung", "cleaning", "清洁"]),
        ("家具家电", ["möbel", "ikea", "media markt", "saturn", "furniture"]),
    ]),
    ("日常生活", [
        ("餐饮", ["restaurant", "wolt", "uber eats", "lieferando", "mcdonald", "burger king",
                "kfc", "kabuki", "noodle", "pizza", "cafe", "kitchen"]),
        ("超市", ["rewe", "edeka", "aldi", "lidl", "kaufland", "asia mini markt",
                "dm drogerie", "rossmann"]),
        ("咖啡饮料", ["coffee", "starbucks", "comebuy", "alley", "lap coffee",
                  "charlies tea", "tgtg"]),
        ("购物", ["amazon", "zalando", "apple store", "media markt", "douglas",
                "shopping", "pop mart"]),
    ]),
    ("个人支出", [
        ("医疗", ["apotheke", "pharmacy", "arzt", "doctor", "klinik", "medical"]),
        ("理发", ["friseur", "barber", "haircut", "salon"]),
        ("健身", ["fitness", "gym", "mcfit", "fit first"]),
        ("其他个人", []),
    ]),
    ("交通", [
        ("公交地铁", ["bvg", "vbb", "u-bahn", "s-bahn", "transit"]),
        ("打车", ["uber", "bolt", "taxi", "freenow"]),
        ("加油停车", ["shell", "aral", "tank", "parkhaus", "parking"]),
    ]),
    ("出行与旅游", [
        ("机票", ["finnair", "lufthansa", "ryanair", "easyjet", "flight", "booking.com flight"]),
        ("火车票", ["db ", "deutsche bahn", "trainline", "sncf", "ouigo", "omio"]),
        ("大巴票", ["flixbus", "bus"]),
        ("住宿", ["hotel", "airbnb", "booking.com", "ibis", "hilton"]),
    ]),
    ("订阅费用", [
        ("会员费", ["amazon prime", "netflix", "spotify", "apple one", "youtube premium",
                "google one"]),
        ("AI 订阅", ["openai", "chatgpt", "anthropic", "claude", "github copilot",
                  "midjourney"]),
        ("话费", ["o2 ", "vodafone", "telekom", "ultra mobile", "1&1"]),
        ("软件订阅", ["adobe", "notion", "figma", "1password"]),
    ]),
    ("娱乐活动", [
        ("演出", ["ticketmaster", "eventim"]),
        ("游戏", ["steam", "epic games", "playstation", "nintendo"]),
    ]),
    ("保险", [
        ("健康保险", ["tk ", "techniker", "aok", "krankenkasse"]),
        ("责任险", ["haftpflicht", "huk", "allianz"]),
        ("其他保险", []),
    ]),
    ("教育", [
        ("课程", ["udemy", "coursera", "udacity", "linkedin learning"]),
        ("书籍", ["thalia", "hugendubel", "kindle", "books"]),
    ]),
]


async def seed_categories(db: AsyncSession) -> dict[str, int]:
    """Idempotently insert taxonomy + starter matching rules.

    Returns counts of {categories_added, rules_added}.
    """
    cat_added = 0
    rule_added = 0

    for parent_name, children in _TAXONOMY:
        parent = await _ensure_category(db, name=parent_name, kind="expense", parent_id=None)
        if parent[1]:
            cat_added += 1

        for child_name, keywords in children:
            child = await _ensure_category(db, name=child_name, kind="expense", parent_id=parent[0])
            if child[1]:
                cat_added += 1
            child_id = child[0]

            for kw in keywords:
                if await _ensure_rule(db, pattern=kw, category_id=child_id):
                    rule_added += 1

    if cat_added or rule_added:
        await db.flush()
        logger.info("categories_seeded", categories_added=cat_added, rules_added=rule_added)
    return {"categories_added": cat_added, "rules_added": rule_added}


async def _ensure_category(
    db: AsyncSession, *, name: str, kind: str, parent_id: int | None,
) -> tuple[int, bool]:
    """Return (category_id, was_inserted)."""
    stmt = select(Category).where(
        Category.name == name,
        Category.kind == kind,
        Category.parent_id.is_(parent_id) if parent_id is None else Category.parent_id == parent_id,
    )
    existing = (await db.execute(stmt)).scalar_one_or_none()
    if existing:
        return existing.id, False
    cat = Category(name=name, kind=kind, parent_id=parent_id, is_system=False)
    db.add(cat)
    await db.flush()
    return cat.id, True


async def _ensure_rule(
    db: AsyncSession, *, pattern: str, category_id: int,
) -> bool:
    """Return True if rule was inserted, False if it already exists."""
    stmt = select(CategorizationRule).where(
        CategorizationRule.pattern == pattern,
        CategorizationRule.category_id == category_id,
    )
    existing = (await db.execute(stmt)).scalar_one_or_none()
    if existing:
        return False
    rule = CategorizationRule(
        pattern=pattern,
        pattern_type="contains",
        field="description",
        category_id=category_id,
        priority=10,  # seed rules sit above user-learned rules at default priority 0
        enabled=True,
    )
    db.add(rule)
    return True
