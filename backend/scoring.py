"""
backend/scoring.py
评分引擎 v2.1 + 社区数据交叉验证层 (CDPE - Contextual Delta Pick Engine)

V2.1 Changes:
  - Soft archetype aggregation: 1 - ∏(1-w_i) instead of max(w)
  - Extended value dimension: draw/filter/exhaust/AoE/block bonuses
  - Soft role scoring: sigmoid transitions instead of hard thresholds
  - Skip scoring: evaluates "don't pick" as a full option

评分逻辑：
  以 60 分为 "无害中性" 基线，向上向下浮动。

  分档定义（对应 recommendation）：
    80~100  强烈推荐（套路核心 or 稀有高价值）
    65~79   推荐
    50~64   可选（有一定价值，但非必需）
    30~49   谨慎（轻微稀释，或与套路无关）
    0~29    跳过（污染或明显不适合当前 run）

  各维度（均归一化 0~1）：
    1. archetype_score   套路契合度     权重 0.35  (V2: reduced from 0.40)
    2. value_score       卡牌固有价值   权重 0.25  稀有度+费用+utility综合
    3. phase_score       阶段适配       权重 0.15  当前阶段适配性
    4. completion_score  完成度贡献     权重 0.15  拿了这张后套路更完整多少
    5. synergy_bonus     额外协同       权重 0.10  (V2: increased from 0.05)

  惩罚（直接从 raw score 减分）：
    pollution_penalty: 污染牌 -30~-50 分
    deck_bloat_penalty: deck 过厚时对低价值牌额外惩罚

  社区交叉验证（post-processing）：
    combine_scores() 输出 algo_score，再经 cross_validate() 与社区数据混合。
    同趋势时放大置信度，冲突时折中，无数据时直接使用 algo_score。
"""

from __future__ import annotations

import logging
import math
from dataclasses import dataclass
from enum import Enum
from typing import Optional

from .models import (
    Card, Rarity, GamePhase, RunState,
    ScoreBreakdown, CardRole, Character,
)
from .deck_profiler import DeckProfile
from .threat_model import ThreatProfile

log = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# 社区交叉验证：可调常数
# ---------------------------------------------------------------------------

_COMMUNITY_WEIGHT: float      = 0.25   # 社区数据最大影响权重
_DAMPENING: float             = 0.85   # 补丁滞后折扣（永久降低社区权重 15%）
_AGREEMENT_THRESHOLD: float   = 0.15   # delta ≤ 此值 → AGREEMENT
_CONFLICT_THRESHOLD: float    = 0.30   # delta > 此值 → CONFLICT


# ---------------------------------------------------------------------------
# 社区交叉验证：数据结构
# ---------------------------------------------------------------------------

class Alignment(str, Enum):
    AGREEMENT     = "agreement"       # |delta| ≤ 0.15
    SOFT_CONFLICT = "soft_conflict"   # 0.15 < |delta| ≤ 0.30
    CONFLICT      = "conflict"        # |delta| > 0.30


@dataclass
class CrossValidationResult:
    blended_norm:        float      # 最终归一化分数 0-1
    community_score:     float      # 社区归一化分数（无数据时 = algo_norm）
    confidence:          float      # 0.0(无数据) / 0.50 / 0.75 / 1.0
    delta:               float      # |algo_norm - community_norm|
    alignment:           Alignment
    has_community_data:  bool


# ---------------------------------------------------------------------------
# 社区评分转换工具
# ---------------------------------------------------------------------------

def _sigmoid(x: float, center: float, steepness: float) -> float:
    """Logistic sigmoid，避免极值压缩中间段。"""
    return 1.0 / (1.0 + math.exp(-steepness * (x - center)))


def community_score_from_raw(win_rate_pct: float, pick_rate_pct: float) -> float:
    """
    将社区统计（百分比）转换为归一化评分 0~1。

    win_rate_pct : 胜率百分比，e.g. 61.4
    pick_rate_pct: 选取率百分比，e.g. 34.1

    sigmoid 中心：
      win_rate  → 50%（中性基线）
      pick_rate → 18%（经验均值）
    权重：win_rate 65%，pick_rate 35%
    """
    norm_win  = _sigmoid(win_rate_pct,  center=50.0, steepness=0.12)
    norm_pick = _sigmoid(pick_rate_pct, center=18.0, steepness=0.08)
    return round(0.65 * norm_win + 0.35 * norm_pick, 4)


# ---------------------------------------------------------------------------
# 社区交叉验证：核心函数
# ---------------------------------------------------------------------------

def cross_validate(
    algo_norm: float,
    community_score: Optional[float],
    community_weight: float = _COMMUNITY_WEIGHT,
    dampening: float = _DAMPENING,
) -> CrossValidationResult:
    """
    将算法归一化分数（0-1）与社区归一化分数（0-1 或 None）交叉验证。

    规则：
      - 无社区数据：直接返回 algo_norm，confidence=0.0
      - AGREEMENT (delta ≤ 0.15)：双强/双弱时放大，confidence=1.0
      - SOFT_CONFLICT (0.15 < delta ≤ 0.30)：社区权重打 75%，confidence=0.75
      - CONFLICT (delta > 0.30)：社区权重打 50%，confidence=0.50

    最终混合：
      effective_cw = community_weight * confidence * dampening
      blended = (1 - effective_cw) * algo_norm + effective_cw * adjusted_community
    """
    if community_score is None:
        return CrossValidationResult(
            blended_norm=algo_norm,
            community_score=algo_norm,
            confidence=0.0,
            delta=0.0,
            alignment=Alignment.AGREEMENT,
            has_community_data=False,
        )

    delta = abs(algo_norm - community_score)

    if delta <= _AGREEMENT_THRESHOLD:
        # 同趋势放大
        amp = 0.05 * (1.0 - delta / _AGREEMENT_THRESHOLD)
        if algo_norm > 0.5 and community_score > 0.5:
            adjusted = min(1.0, community_score + amp)
        elif algo_norm < 0.5 and community_score < 0.5:
            adjusted = max(0.0, community_score - amp)
        else:
            adjusted = community_score  # 混合方向，不放大
        confidence = 1.0
        alignment  = Alignment.AGREEMENT

    elif delta <= _CONFLICT_THRESHOLD:
        adjusted   = community_score
        confidence = 0.75
        alignment  = Alignment.SOFT_CONFLICT

    else:
        adjusted   = community_score
        confidence = 0.50
        alignment  = Alignment.CONFLICT

    effective_cw = community_weight * confidence * dampening
    blended = (1.0 - effective_cw) * algo_norm + effective_cw * adjusted
    blended = max(0.0, min(1.0, blended))

    log.debug(
        f"cross_validate: algo={algo_norm:.3f} community={community_score:.3f}"
        f" delta={delta:.3f} align={alignment.value} conf={confidence:.2f}"
        f" ecw={effective_cw:.3f} blended={blended:.3f}"
    )

    return CrossValidationResult(
        blended_norm=blended,
        community_score=community_score,
        confidence=confidence,
        delta=delta,
        alignment=alignment,
        has_community_data=True,
    )

# ---------------------------------------------------------------------------
# 权重配置
# ---------------------------------------------------------------------------

WEIGHTS: dict[str, float] = {
    "archetype":   0.35,   # V2: reduced from 0.40 (套路契合)
    "value":       0.25,   # 卡牌固有价值（稀有度+费用+utility）
    "phase":       0.15,   # 阶段适配
    "completion":  0.15,   # 完成度贡献
    "synergy":     0.10,   # V2: increased from 0.05 (协同加成)
}
# 合计 = 1.00

# ---------------------------------------------------------------------------
# 维度1：套路契合度 (V2: Soft aggregation)
# ---------------------------------------------------------------------------

def score_archetype_dimension(
    card: Card,
    matched_archetype_weights: list[float],
) -> float:
    """
    V2: Soft aggregation instead of max().
    
    Formula: 1 - ∏(1 - w_i)
    
    This gives credit for multi-archetype synergy:
    - Card good in 2 directions (0.5, 0.4) = 0.70 (better than max=0.5)
    - Card great in 1 direction (0.8) = 0.80
    
    Old behavior: max(weights) penalized versatile cards.
    """
    if not matched_archetype_weights:
        return 0.0
    
    # Soft-sum: 1 - ∏(1 - w_i)
    product = 1.0
    for w in matched_archetype_weights:
        product *= (1.0 - w)
    
    return 1.0 - product


# ---------------------------------------------------------------------------
# 维度2：卡牌固有价值 (V2: Extended with utility bonuses)
# ---------------------------------------------------------------------------

def score_value_dimension(card: Card, phase: GamePhase) -> float:
    """
    V2: Extended card value beyond just rarity + cost.
    
    Includes:
      - Rarity baseline (0.20 ~ 0.90)
      - Cost efficiency bonus/penalty
      - Draw/filter bonus (consistency)
      - Exhaust bonus (deck quality)
      - AoE bonus (multi-target)
      - Block bonus (survival)
      - Setup tax (needs other cards)
      - Dead draw risk (situational cards)
    
    设计原则：
      - rare 卡在任何时候都有底线价值（0.75+）
      - common 给 0.45 左右作为中性值
      - 0 费牌有显著加成（灵活性价值）
      - Utility (draw, exhaust, AoE) adds real value
    """
    rarity_base: dict[Rarity, float] = {
        Rarity.ANCIENT:  0.90,
        Rarity.RARE:     0.80,
        Rarity.UNCOMMON: 0.60,
        Rarity.COMMON:   0.45,
        Rarity.BASIC:    0.35,
        Rarity.STARTER:  0.20,
        Rarity.SPECIAL:  0.10,
        Rarity.CURSE:    0.00,
        Rarity.STATUS:   0.05,
    }
    base = rarity_base.get(card.rarity, 0.45)

    # 费用效率
    cost_bonus = 0.0
    if card.cost == 0:
        cost_bonus = 0.12
    elif card.cost == 1:
        cost_bonus = 0.05
    elif card.cost >= 3:
        cost_bonus = -0.05   # 高费用略微减分

    # V2: Draw/filter value (consistency)
    draw_bonus = 0.0
    if card.base_draw is not None and card.base_draw > 0:
        draw_bonus = min(0.15, card.base_draw * 0.05)
    
    # V2: Exhaust bonus (deck thinning/quality)
    exhaust_bonus = 0.0
    if card.keywords.exhaust:
        exhaust_bonus = 0.06
    
    # V2: Block value (survival)
    block_bonus = 0.0
    if card.base_block is not None and card.base_block > 0:
        block_bonus = min(0.10, card.base_block * 0.006)
    
    # V2: AoE bonus (check description for "all enemies" pattern)
    aoe_bonus = 0.0
    desc_lower = card.description.lower()
    if "all enem" in desc_lower or "each enemy" in desc_lower:
        aoe_bonus = 0.06
    
    # V2: Innate bonus (reliable opening)
    innate_bonus = 0.04 if card.keywords.innate else 0.0
    
    # V2: Retain bonus (flexibility)
    retain_bonus = 0.03 if card.keywords.retain else 0.0
    
    # V2: Ethereal penalty (must use or lose)
    ethereal_penalty = -0.04 if card.keywords.ethereal else 0.0
    
    # V2: High setup tax detection (rough heuristic)
    setup_penalty = 0.0
    if "requires" in desc_lower or "if you have" in desc_lower:
        setup_penalty = -0.06
    
    total = (base + cost_bonus + draw_bonus + exhaust_bonus 
             + block_bonus + aoe_bonus + innate_bonus + retain_bonus
             + ethereal_penalty + setup_penalty)
    
    return min(1.0, max(0.0, total))


# ---------------------------------------------------------------------------
# 维度3：阶段适配
# ---------------------------------------------------------------------------

def score_phase_dimension(
    card: Card,
    phase: GamePhase,
    card_role: CardRole,
    hp_ratio: float = 1.0,
) -> float:
    """
    当前阶段对该卡的适配度。
      - CORE/ENABLER 任何阶段都高分
      - TRANSITION 早期强，后期弱
      - POLLUTION 所有阶段 0 分
      - FILLER/UNKNOWN 中性 0.55

    hp_ratio: 当前 HP / 最大 HP（来自 RunState.hp_ratio）。
      hp_ratio < 0.30 时，对有格挡价值的技能/能力牌加 +0.06，
      对纯进攻攻击牌（无格挡、无摸牌）减 -0.08。
      最终影响上限约 ±1.2 分（权重 0.15），不会翻转推荐等级。
    """
    if card_role == CardRole.POLLUTION:
        return 0.0
    if card_role == CardRole.TRANSITION:
        base = {
            GamePhase.EARLY: 0.85,
            GamePhase.MID:   0.45,
            GamePhase.LATE:  0.15,
        }[phase]
    elif card_role in (CardRole.CORE, CardRole.ENABLER):
        base = {
            GamePhase.EARLY: 0.75,
            GamePhase.MID:   0.82,
            GamePhase.LATE:  0.88,
        }[phase]
    else:
        base = 0.55

    # HP 低血量修正（仅在 < 30% 时生效）
    if hp_ratio < 0.30:
        from .models import CardType
        has_block = (card.base_block or 0) > 0
        is_skill_or_power = card.card_type in (CardType.SKILL, CardType.POWER)
        is_pure_attack = (
            card.card_type == CardType.ATTACK
            and not has_block
            and (card.base_draw or 0) == 0
        )
        if has_block or is_skill_or_power:
            base = min(1.0, base + 0.06)
        elif is_pure_attack:
            base = max(0.0, base - 0.08)

    return base


# ---------------------------------------------------------------------------
# 维度4：完成度贡献 (V2: Multi-motif + weakness repair)
# ---------------------------------------------------------------------------

def score_completion_dimension(
    archetype_completion_before: float,
    archetype_completion_after: float,
    # V2 optional parameters for enhanced scoring
    all_completions_before: dict[str, float] | None = None,
    all_completions_after: dict[str, float] | None = None,
) -> float:
    """
    V2: Enhanced completion scoring.
    
    Basic mode (backward compatible):
      拿了这张卡后套路完成度提升多少。
      完成度 delta 放大 3 倍（因为单张卡通常只提升 5~10%）
    
    Enhanced mode (when all_completions provided):
      - Consider top 3 motifs, not just primary
      - Bonus for unlocking new viable motifs
      - More nuanced weighting
    """
    # Basic mode (backward compatible)
    if all_completions_before is None or all_completions_after is None:
        delta = archetype_completion_after - archetype_completion_before
        return min(1.0, max(0.0, delta * 3.0))
    
    # V2 Enhanced mode: Multi-motif completion
    improvements = []
    unlock_bonus = 0.0
    
    for motif_id in all_completions_before:
        before = all_completions_before.get(motif_id, 0.0)
        after = all_completions_after.get(motif_id, 0.0)
        delta = after - before
        improvements.append(delta)
        
        # Check for motif unlock (crossing viability threshold)
        if before < 0.20 and after >= 0.25:
            unlock_bonus += 0.08  # Bonus for unlocking new viable path
    
    # Take top 3 improvements (card can contribute to multiple motifs)
    improvements.sort(reverse=True)
    top3_delta = sum(improvements[:3])
    
    # Scale factor (slightly lower than single-motif to avoid inflation)
    motif_score = min(0.8, top3_delta * 2.5)
    
    # Add unlock bonus
    total = motif_score + min(0.2, unlock_bonus)
    
    return min(1.0, max(0.0, total))


# ---------------------------------------------------------------------------
# 维度5：协同加成
# ---------------------------------------------------------------------------

def score_synergy_bonus(
    card: Card,
    run_state: RunState,
    relic_synergy_tags: list[str],
    relic_boosts: dict[str, float] | None = None,
    matched_archetype_ids: list[str] | None = None,
) -> float:
    """
    遗物/已有卡协同加成。
    - tag 路径：每个匹配标签贡献 0.2，上限 1.0（旧逻辑，tags 当前为空）
    - boost 路径：遗物→套路显式映射（relic_archetype_map），取该卡已匹配套路中的最高 boost
    取两条路径的最大值，避免重复累加。
    """
    tag_score = min(1.0, len(set(card.tags) & set(relic_synergy_tags)) * 0.20)

    boost_score = 0.0
    if relic_boosts and matched_archetype_ids:
        for aid in matched_archetype_ids:
            if aid in relic_boosts:
                boost_score = max(boost_score, relic_boosts[aid])

    return min(1.0, max(tag_score, boost_score))


# ---------------------------------------------------------------------------
# 惩罚项
# ---------------------------------------------------------------------------

def pollution_penalty(
    card: Card,
    deck_size: int,
    card_role: CardRole,
) -> float:
    """
    污染惩罚（直接减分，不经过权重，单位：0~1）。
    污染牌在合并时会造成约 -30~-50 分的实际分数下降。
    deck 越小，污染代价越大。
    """
    if card_role != CardRole.POLLUTION:
        return 0.0
    # 基础惩罚 0.50，deck 每增加一张卡折扣 0.015
    base_penalty = 0.50
    size_discount = min(0.25, deck_size * 0.015)
    return base_penalty - size_discount


def deck_bloat_penalty(
    card: Card,
    deck_size: int,
    card_role: CardRole,
) -> float:
    """
    厚牌组对低价值牌的额外惩罚。
    deck >= 20 张后，FILLER/UNKNOWN 卡每多一张 deck 给 0.01 惩罚，上限 0.15。
    CORE/ENABLER 不受影响。
    """
    if card_role in (CardRole.CORE, CardRole.ENABLER):
        return 0.0
    if deck_size < 20:
        return 0.0
    extra = deck_size - 20
    return min(0.15, extra * 0.01)


# ---------------------------------------------------------------------------
# Ascension 修正
# ---------------------------------------------------------------------------

def ascension_modifier(
    card_role: CardRole,
    ascension: int,
    archetype_score: float,
) -> float:
    """
    根据 Ascension 难度层级，对最终分数施加小幅修正（加减分）。
    修正范围：-5 ~ +5 分（不影响等级档次的主要判断）。

    设计依据（来自 STS2 Ascension 文档）：
      A5+（抽牌污染）：不稳定/高依赖套路成型率下降，CORE 更珍贵
      A7+（卡池质量）：稀有卡更难获取，遇到 CORE 卡更应该拿
      A10（双 Boss）：需要持续输出，一波流 / FILLER 价值更低

    修正逻辑：
      - 无套路命中时（archetype_score ≈ 0）：不施加修正，避免对通用牌误判
      - CORE/ENABLER + 高 Ascension：轻微加分（套路已难成型，此牌更关键）
      - FILLER/UNKNOWN + A7+：轻微减分（卡池质量下降，FILLER 价值更低）
      - POLLUTION：不额外修正（已有 pollution_penalty 处理）
    """
    if ascension <= 0 or archetype_score < 0.3:
        # 普通模式或无套路命中时不修正
        return 0.0

    if card_role in (CardRole.CORE, CardRole.ENABLER):
        # A5+: +1, A7+: +2, A10: +3（最高 +5，保持 A10 上限）
        if ascension >= 10:
            return min(5.0, 3.0 + (ascension - 10) * 0.5)
        elif ascension >= 7:
            return 2.0
        elif ascension >= 5:
            return 1.0
        else:
            return 0.5

    elif card_role in (CardRole.FILLER, CardRole.UNKNOWN):
        # A7+: -1, A10: -2
        if ascension >= 10:
            return -2.0
        elif ascension >= 7:
            return -1.0
        else:
            return 0.0

    return 0.0


# ---------------------------------------------------------------------------
# 合并：加权求和 → 0~100 分
# ---------------------------------------------------------------------------

def combine_scores(breakdown: "ScoreBreakdown", bloat_penalty: float = 0.0) -> float:
    """
    将 ScoreBreakdown 各维度加权合并，返回 0~100 的算法分（algo_score）。

    合并逻辑：
      raw = Σ(维度得分 × 权重) - 惩罚
      raw 映射到 [0, 1]，×100 取整到 0.1 精度。

    v0.7 变更：
      - bloat_penalty 改为显式参数（不再从 breakdown.rarity_score 读取）
      - breakdown.rarity_score 现存储 community_score（由 evaluator 写入）
    """
    raw = (
        breakdown.archetype_score    * WEIGHTS["archetype"]
        + breakdown.base_score       * WEIGHTS["value"]
        + breakdown.phase_score      * WEIGHTS["phase"]
        + breakdown.completion_score * WEIGHTS["completion"]
        + breakdown.synergy_bonus    * WEIGHTS["synergy"]
        - breakdown.pollution_penalty
        - bloat_penalty
    )
    total = round(max(0.0, min(1.0, raw)) * 100, 1)
    log.debug(
        f"分数合并: archetype={breakdown.archetype_score:.2f}×{WEIGHTS['archetype']}"
        f" value={breakdown.base_score:.2f}×{WEIGHTS['value']}"
        f" phase={breakdown.phase_score:.2f}×{WEIGHTS['phase']}"
        f" completion={breakdown.completion_score:.2f}×{WEIGHTS['completion']}"
        f" synergy={breakdown.synergy_bonus:.2f}×{WEIGHTS['synergy']}"
        f" poll_pen={breakdown.pollution_penalty:.2f}"
        f" bloat_pen={bloat_penalty:.2f}"
        f" → raw={raw:.3f} total={total}"
    )
    return total


# ---------------------------------------------------------------------------
# 保留旧接口兼容（evaluator.py 调用）
# ---------------------------------------------------------------------------

def score_base_dimension(card: Card, phase: GamePhase) -> float:
    """兼容旧接口：内部调用 score_value_dimension"""
    return score_value_dimension(card, phase)


def score_rarity_dimension(card: Card) -> float:
    """兼容旧接口：返回 0（bloat_penalty 在 evaluator 中单独计算）"""
    return 0.0


# ---------------------------------------------------------------------------
# V2: Soft Role Scoring (sigmoid transitions instead of hard thresholds)
# ---------------------------------------------------------------------------

def soft_role_confidence(weight: float) -> dict[str, float]:
    """
    V2: Calculate confidence scores for each role using sigmoid transitions.
    
    Returns dict with confidence 0-1 for: core, enabler, filler, pollution
    
    This replaces hard thresholds (0.85/0.60/0.30) with smooth transitions,
    so a card at 0.29 vs 0.30 is no longer a cliff.
    
    Sigmoid centers:
      - CORE: 0.82 (steep, need strong signal)
      - ENABLER: 0.62 
      - FILLER: 0.40
      - POLLUTION: 0.15 (inverted, low weight = high pollution)
    """
    def sigmoid(x: float, center: float, steepness: float) -> float:
        return 1.0 / (1.0 + math.exp(-steepness * (x - center)))
    
    core_conf = sigmoid(weight, center=0.82, steepness=12.0)
    enabler_conf = sigmoid(weight, center=0.62, steepness=10.0) * (1.0 - core_conf)
    filler_conf = sigmoid(weight, center=0.40, steepness=8.0) * (1.0 - core_conf - enabler_conf)
    pollution_conf = sigmoid(0.18 - weight, center=0.0, steepness=15.0)  # inverted
    
    # Normalize to sum to ~1 (except pollution which is independent)
    total = core_conf + enabler_conf + filler_conf
    if total > 0:
        scale = 1.0 / total
        core_conf *= scale
        enabler_conf *= scale
        filler_conf *= scale
    
    return {
        "core": round(core_conf, 3),
        "enabler": round(enabler_conf, 3),
        "filler": round(filler_conf, 3),
        "pollution": round(pollution_conf, 3),
    }


def determine_role_v2(
    max_weight: float,
    inferred_only: bool = False,
) -> CardRole:
    """
    V2: Determine role using soft thresholds with hysteresis.
    
    Still returns a single CardRole, but uses smoother boundaries
    and respects the inferred_only flag.
    """
    # Soft thresholds with hysteresis (±0.03)
    if max_weight >= 0.82:
        return CardRole.CORE
    elif max_weight >= 0.57:  # lowered from 0.60
        return CardRole.ENABLER
    elif max_weight >= 0.25:  # lowered from 0.30
        return CardRole.FILLER
    else:
        # Inferred cards shouldn't be marked pollution
        if inferred_only:
            return CardRole.FILLER
        return CardRole.POLLUTION


# ---------------------------------------------------------------------------
# V2: Skip Scoring - Skip as a full alternative option
# ---------------------------------------------------------------------------

def calculate_skip_score(
    deck_size: int,
    target_deck_size: int,
    phase: GamePhase,
    hp_ratio: float,
    deck_consistency: float = 0.5,  # 0-1, how consistent is current deck
    has_critical_gaps: bool = False,  # Does deck have critical weaknesses
) -> float:
    """
    V2: Calculate the value of skipping (not taking any card).
    
    Skip is valuable when:
      - Deck is already large (dilution hurts)
      - Deck is consistent (don't want to break it)
      - Phase is late (less time to use new cards)
    
    Skip is bad when:
      - Deck is small (need cards)
      - Deck has critical gaps (need to fill weaknesses)
      - Phase is early (more time to build)
    
    Returns: score 0-100 (like card scores)
    """
    # Base skip value (neutral)
    base = 50.0
    
    # Dilution resistance: bigger deck = skip more valuable
    if deck_size > target_deck_size:
        excess = deck_size - target_deck_size
        base += min(15.0, excess * 2.0)  # max +15
    elif deck_size < target_deck_size - 3:
        deficit = target_deck_size - deck_size
        base -= min(10.0, deficit * 1.5)  # max -10
    
    # Consistency protection: good deck = protect it
    if deck_consistency > 0.7:
        base += 5.0
    elif deck_consistency < 0.3:
        base -= 5.0
    
    # Phase adjustment: early = need cards, late = be selective
    phase_mod = {
        GamePhase.EARLY: -8.0,  # Need to build
        GamePhase.MID: 0.0,     # Neutral
        GamePhase.LATE: 5.0,    # Be selective
    }
    base += phase_mod.get(phase, 0.0)
    
    # Critical gaps: if deck needs something specific, don't skip
    if has_critical_gaps:
        base -= 12.0
    
    # HP modifier: low HP = need good cards to survive
    if hp_ratio < 0.3:
        base -= 5.0  # Need survival cards
    elif hp_ratio > 0.8:
        base += 2.0  # Can afford to be picky
    
    return max(20.0, min(80.0, base))


def calculate_skip_score_v2(
    deck_profile: DeckProfile,
    threat_profile: ThreatProfile,
    phase: GamePhase,
) -> float:
    """
    V2-lite skip model driven by deck profile + threat profile.
    """
    base = 50.0

    # Dilution resistance
    if deck_profile.deck_size > deck_profile.target_size:
        excess = deck_profile.deck_size - deck_profile.target_size
        base += min(18.0, excess * 2.2)
    elif deck_profile.deck_size < deck_profile.target_size - 3:
        deficit = deck_profile.target_size - deck_profile.deck_size
        base -= min(12.0, deficit * 1.8)

    # Preserve consistency in good, stable decks
    base += (deck_profile.consistency_score - 0.5) * 14.0

    # Dead-draw and setup burden incentivize skipping weak fillers
    base += deck_profile.dead_draw_rate * 10.0
    base += deck_profile.setup_burden * 6.0

    # Threat pressure reduces skip appetite
    base -= threat_profile.survival_urgency * 14.0
    base -= (1.0 - threat_profile.elite_readiness) * 8.0

    # Explicit deck gaps reduce skip value
    base -= min(15.0, len(deck_profile.critical_gaps) * 4.0)

    # Phase adjustment
    phase_mod = {
        GamePhase.EARLY: -6.0,
        GamePhase.MID: 0.0,
        GamePhase.LATE: 5.0,
    }
    base += phase_mod.get(phase, 0.0)

    return max(15.0, min(85.0, base))


def calculate_pick_delta(card_score: float, skip_score: float) -> float:
    """
    V2: Calculate how much better a card is compared to Skip.
    
    Positive = card is better than skipping
    Negative = skip is better than taking this card
    
    Returns: delta in points (can be negative)
    """
    return card_score - skip_score


def format_pick_recommendation(
    card_name: str,
    card_score: float,
    skip_score: float,
) -> str:
    """
    V2: Format recommendation showing delta vs Skip.
    
    Examples:
      "Inflame (+25.3 vs Skip)" 
      "Skip is better (-5.2)"
    """
    delta = card_score - skip_score
    if delta > 0:
        return f"{card_name} (+{delta:.1f} vs Skip)"
    else:
        return f"Skip is better ({delta:.1f})"
