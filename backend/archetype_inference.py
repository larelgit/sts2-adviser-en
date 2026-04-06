"""
backend/archetype_inference.py
套路推断层 (v0.6)

职责：
  对手动 card_weights 中不存在的卡，根据卡牌的结构化数据
  （powers_applied、keywords、description、card_type、cost）
  自动推断该卡与每个套路的相关度，返回一个推断权重。

设计原则：
  - 推断权重上限 0.35，永远低于手动定义（最低 0.40）
  - 角色最高 FILLER（不会推断出 CORE/ENABLER）
  - 推断是"兜底"，不覆盖精确层
  - 每个套路维护一份"推断规则集"（ArchetypeInferenceRule）

推断分类（三级）：
  HIGH   (~0.30~0.35)  关键词直接命中套路核心机制
  MID    (~0.18~0.28)  关键词与套路方向相关但非核心
  LOW    (~0.08~0.15)  类型/费用/通用价值符合，无直接关键词匹配
"""

from __future__ import annotations

import re
import logging
from dataclasses import dataclass, field
from typing import Optional

from .models import Card, CardRole, CardType, Character

log = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# 推断规则数据结构
# ---------------------------------------------------------------------------

@dataclass
class InferenceRule:
    """
    一条推断规则：如果卡牌满足条件，给该套路加权。

    匹配优先级（按顺序逐级检查，取最高命中）：
      1. powers_applied_any  — powers_applied 列表含指定 power_key（精确匹配）
      2. keywords_any        — keywords_key 列表含指定词
      3. desc_pattern        — description 正则匹配
      4. card_type_match     — 卡牌类型匹配
      5. cost_max            — 费用 <= 指定值
    """
    # 若命中，加到权重上的分值（可叠加多条规则）
    weight_add: float

    # 匹配条件（满足任意一个即触发本条规则）
    powers_applied_any: list[str] = field(default_factory=list)   # power_key 列表
    keywords_any:       list[str] = field(default_factory=list)   # keyword_key 列表
    desc_pattern:       Optional[str] = None                      # 正则（忽略大小写）
    card_type_match:    Optional[CardType] = None                  # 精确类型
    cost_max:           Optional[int] = None                       # 费用上限（含）
    cost_exact:         Optional[int] = None                       # 精确费用

    # 附加必须满足的条件（AND关系）
    require_type:       Optional[CardType] = None                  # 必须是此类型
    require_not_type:   Optional[CardType] = None                  # 必须不是此类型


@dataclass
class ArchetypeInferenceProfile:
    """
    单个套路的推断配置。
    包含一组规则，最终权重 = min(0.35, sum(命中规则的 weight_add))
    """
    archetype_id: str
    rules: list[InferenceRule] = field(default_factory=list)

    # 套路排斥：若卡牌命中这些关键词/powers，推断权重强制清零
    anti_patterns: list[str] = field(default_factory=list)  # desc_pattern 正则列表


# ---------------------------------------------------------------------------
# 各套路推断规则表
# ---------------------------------------------------------------------------

_PROFILES: list[ArchetypeInferenceProfile] = [

    # ── IRONCLAD ─────────────────────────────────────────────────────────

    ArchetypeInferenceProfile(
        archetype_id="ironclad_strength",
        rules=[
            # 直接给力量 → HIGH
            InferenceRule(0.30, powers_applied_any=["Strength"]),
            # 施加脆弱/力量倍化 → MID
            InferenceRule(0.20, powers_applied_any=["Vulnerable"]),
            # 描述含 Strength/力量 → MID
            InferenceRule(0.18, desc_pattern=r"\bstrength\b|力量"),
            # 普通攻击牌（有damage）→ LOW（可以利用力量）
            InferenceRule(0.10, card_type_match=CardType.ATTACK),
        ],
        anti_patterns=[r"\bexhaust\b.*\bskill\b|corruption"],  # exhaust引擎套排斥
    ),

    ArchetypeInferenceProfile(
        archetype_id="ironclad_self_damage",
        rules=[
            # 主动扣HP → HIGH
            InferenceRule(0.30, desc_pattern=r"lose \d+ ?hp|损失.*hp|hp_loss"),
            InferenceRule(0.28, powers_applied_any=["Rupture"]),
            # 与HP损耗协同 → MID
            InferenceRule(0.18, powers_applied_any=["Strength"]),
            InferenceRule(0.15, desc_pattern=r"\bbloodletting\b|\boffering\b"),
            # 格挡（撑住存活）→ LOW
            InferenceRule(0.08, card_type_match=CardType.SKILL,
                          require_not_type=None),
        ],
        anti_patterns=[r"\bcorruption\b"],
    ),

    ArchetypeInferenceProfile(
        archetype_id="ironclad_exhaust",
        rules=[
            # 排除关键词 → HIGH（但需配合其他特征，避免所有带Exhaust的牌都进来）
            InferenceRule(0.25, keywords_any=["Exhaust"],
                          require_type=CardType.SKILL),      # 只对技能牌加分
            # 技能牌（Corruption下0费）→ MID（需同时有排除或摸牌效果）
            InferenceRule(0.18, desc_pattern=r"\bexhaust\b|排除"),
            # 触发排除效果的描述 → MID
            InferenceRule(0.22, powers_applied_any=["DarkEmbrace", "FeelNoPain",
                                                     "Dark Embrace", "Feel No Pain"]),
            # 摸牌（引擎辅助）→ LOW
            InferenceRule(0.10, desc_pattern=r"draw \d+(?! pile)|摸.{0,2}张"),
        ],
        anti_patterns=[r"\bdemonic form\b|demon_form|\bshivs?\b"],
    ),

    # ── SILENT ───────────────────────────────────────────────────────────

    ArchetypeInferenceProfile(
        archetype_id="silent_poison",
        rules=[
            InferenceRule(0.32, powers_applied_any=["Poison"]),
            InferenceRule(0.28, desc_pattern=r"\bpoison\b|毒素|叠毒"),
            InferenceRule(0.20, powers_applied_any=["Vulnerable", "Weak"]),
            InferenceRule(0.15, desc_pattern=r"\bvulnerable\b|易伤"),
            # 技能牌（叠毒辅助）→ LOW
            InferenceRule(0.10, card_type_match=CardType.SKILL),
        ],
        anti_patterns=[r"\bshiv\b|blade_dance|accuracy"],
    ),

    ArchetypeInferenceProfile(
        archetype_id="silent_shiv",
        rules=[
            InferenceRule(0.32, desc_pattern=r"\bshivs?\b|小刀"),
            InferenceRule(0.28, powers_applied_any=["Accuracy"]),
            # 生成卡牌 → MID（生成Shiv）
            InferenceRule(0.20, desc_pattern=r"add .{0,15} to (?:your )?hand|加入.*手牌"),
            # 多段攻击 → MID
            InferenceRule(0.18, desc_pattern=r"hit.{0,6}time|(\d)\s*time|\d\s*×"),
            InferenceRule(0.10, card_type_match=CardType.ATTACK),
        ],
        anti_patterns=[r"\bpoison\b|noxious_fumes"],
    ),

    ArchetypeInferenceProfile(
        archetype_id="silent_sly_discard",
        rules=[
            InferenceRule(0.32, keywords_any=["Sly"]),
            InferenceRule(0.30, desc_pattern=r"\bdiscard\b|弃.{0,3}张|弃牌"),
            InferenceRule(0.25, desc_pattern=r"when(ever)? you discard|弃牌时"),
            # draw 规则：排除 "draw pile"（如 headbutt 的"从弃牌堆取牌"描述）
            InferenceRule(0.18, desc_pattern=r"draw \d+(?! pile)|摸.{0,2}张"),
            InferenceRule(0.12, cost_exact=0),  # 0费牌循环价值
        ],
        anti_patterns=[r"\bpoison\b|\bdraw pile\b"],
    ),

    # ── DEFECT ───────────────────────────────────────────────────────────

    ArchetypeInferenceProfile(
        archetype_id="defect_orb_focus",
        rules=[
            InferenceRule(0.30, powers_applied_any=["Focus"]),
            InferenceRule(0.28, desc_pattern=r"\bchannel\b|通道|orb"),
            InferenceRule(0.25, powers_applied_any=["Frost", "Lightning", "Dark", "Plasma"]),
            InferenceRule(0.20, desc_pattern=r"\bevoke\b|激发"),
            InferenceRule(0.10, card_type_match=CardType.POWER),
        ],
        anti_patterns=[r"\bclaw\b"],
    ),

    ArchetypeInferenceProfile(
        archetype_id="defect_dark_evoke",
        rules=[
            InferenceRule(0.32, desc_pattern=r"\bdark\b.*orb|\bdarkness\b"),
            InferenceRule(0.28, desc_pattern=r"\bevoke\b|激发"),
            InferenceRule(0.25, powers_applied_any=["Dark"]),
            InferenceRule(0.20, desc_pattern=r"\bchannel\b.*dark|dark.*\bchannel\b"),
            InferenceRule(0.15, desc_pattern=r"\borb\b"),
        ],
        anti_patterns=[r"\bclaw\b|all_for_one"],
    ),

    ArchetypeInferenceProfile(
        archetype_id="defect_zero_cost_cycle",
        rules=[
            InferenceRule(0.30, cost_exact=0),
            InferenceRule(0.28, desc_pattern=r"\bclaw\b"),
            InferenceRule(0.20, powers_applied_any=["Claw"]),
            InferenceRule(0.18, desc_pattern=r"0 cost|费用.*变为.*0|costs? 0"),
            InferenceRule(0.15, desc_pattern=r"draw \d|摸.{0,2}张"),
        ],
        anti_patterns=[r"\bfocus\b|defragment"],
    ),

    ArchetypeInferenceProfile(
        archetype_id="defect_frost_block",
        rules=[
            InferenceRule(0.30, powers_applied_any=["Frost"]),
            InferenceRule(0.25, desc_pattern=r"\bfrost\b|冰霜"),
            InferenceRule(0.22, desc_pattern=r"\bblock\b|\bgain \d+ block\b|获得.*格挡"),
            InferenceRule(0.15, powers_applied_any=["Metallicize", "Plated Armor"]),
            InferenceRule(0.10, card_type_match=CardType.SKILL),
        ],
        anti_patterns=[r"\bdark\b.*burst|evoke.*dark"],
    ),

    # ── WATCHER ──────────────────────────────────────────────────────────

    ArchetypeInferenceProfile(
        archetype_id="watcher_divinity",
        rules=[
            InferenceRule(0.32, powers_applied_any=["Mantra", "Divinity"]),
            InferenceRule(0.28, desc_pattern=r"\bmantra\b|顿悟|神性|divinity"),
            InferenceRule(0.22, keywords_any=["Scry"]),
            InferenceRule(0.18, desc_pattern=r"\bscry\b|占卜"),
            InferenceRule(0.12, card_type_match=CardType.SKILL),
        ],
        anti_patterns=[r"\bwrath\b.*aggro"],
    ),

    ArchetypeInferenceProfile(
        archetype_id="watcher_wrath_aggro",
        rules=[
            InferenceRule(0.30, desc_pattern=r"\bwrath\b|愤怒"),
            InferenceRule(0.28, desc_pattern=r"\bcalm\b.*\bwrath\b|\bwrath\b.*\bcalm\b"),
            InferenceRule(0.22, powers_applied_any=["Strength"]),
            InferenceRule(0.18, card_type_match=CardType.ATTACK),
            InferenceRule(0.12, cost_max=1),
        ],
        anti_patterns=[r"\bmantra\b|divinity"],
    ),
]

# 按 archetype_id 索引，加快查找
_PROFILE_INDEX: dict[str, ArchetypeInferenceProfile] = {
    p.archetype_id: p for p in _PROFILES
}

# ---------------------------------------------------------------------------
# 卡牌数据解析辅助（从 cards.json 原始结构提取）
# ---------------------------------------------------------------------------

def _get_powers(card_raw: dict) -> list[str]:
    """提取 powers_applied 中的 power_key 列表"""
    pa = card_raw.get("powers_applied") or []
    return [p.get("power_key", "") for p in pa if isinstance(p, dict)]


def _get_keywords(card_raw: dict) -> list[str]:
    """提取 keywords_key 列表（大小写不变）"""
    return card_raw.get("keywords_key") or []


def _get_desc(card_raw: dict) -> str:
    """提取 description，去除标签"""
    raw = card_raw.get("description") or ""
    # 去除 [gold]...[/gold] 等标签
    return re.sub(r'\[/?[a-z_]+\]', '', raw, flags=re.IGNORECASE)


def _get_cost(card_raw: dict) -> int:
    """提取费用，X费返回 -1"""
    if card_raw.get("is_x_cost") or card_raw.get("is_x_star_cost"):
        return -1
    return int(card_raw.get("cost") or 0)


def _get_card_type(card_raw: dict) -> Optional[CardType]:
    type_key = (card_raw.get("type_key") or "").lower()
    mapping = {
        "attack": CardType.ATTACK,
        "skill":  CardType.SKILL,
        "power":  CardType.POWER,
        "status": CardType.STATUS,
        "curse":  CardType.CURSE,
    }
    return mapping.get(type_key)


# ---------------------------------------------------------------------------
# 推断函数（核心接口）
# ---------------------------------------------------------------------------

def infer_weight(
    card_raw: dict,
    archetype_id: str,
) -> float:
    """
    对一张卡（cards.json 原始 dict）推断其与指定套路的相关度。

    返回值：0.0 ~ 0.35
      0.0    — 无关联 or 被排斥
      >0     — 推断权重（角色固定为 FILLER）
    """
    profile = _PROFILE_INDEX.get(archetype_id)
    if profile is None:
        return 0.0

    powers   = _get_powers(card_raw)
    keywords = _get_keywords(card_raw)
    desc     = _get_desc(card_raw).lower()
    cost     = _get_cost(card_raw)
    ctype    = _get_card_type(card_raw)

    # ── 排斥检查 ────────────────────────────────────────────────────────
    for anti in profile.anti_patterns:
        if re.search(anti, desc, re.IGNORECASE):
            return 0.0

    # ── 规则匹配 ────────────────────────────────────────────────────────
    total = 0.0
    for rule in profile.rules:

        # 附加必须条件
        if rule.require_type is not None and ctype != rule.require_type:
            continue
        if rule.require_not_type is not None and ctype == rule.require_not_type:
            continue

        # 主匹配（任意一个触发即加分）
        hit = False

        if rule.powers_applied_any:
            powers_lower = [p.lower() for p in powers]
            if any(p.lower() in powers_lower for p in rule.powers_applied_any):
                hit = True

        if not hit and rule.keywords_any:
            kws_lower = [k.lower() for k in keywords]
            if any(k.lower() in kws_lower for k in rule.keywords_any):
                hit = True

        if not hit and rule.desc_pattern:
            if re.search(rule.desc_pattern, desc, re.IGNORECASE):
                hit = True

        if not hit and rule.card_type_match is not None:
            if ctype == rule.card_type_match:
                hit = True

        if not hit and rule.cost_max is not None:
            if cost != -1 and cost <= rule.cost_max:
                hit = True

        if not hit and rule.cost_exact is not None:
            if cost == rule.cost_exact:
                hit = True

        if hit:
            total += rule.weight_add

    return min(0.35, total)


def infer_all_archetypes(
    card_raw: dict,
    archetype_ids: list[str],
) -> dict[str, float]:
    """
    对一张卡推断所有套路的权重。
    返回 {archetype_id: weight}，权重为 0 的套路不包含在结果中。
    """
    result = {}
    for aid in archetype_ids:
        w = infer_weight(card_raw, aid)
        if w > 0.0:
            result[aid] = w
    return result
