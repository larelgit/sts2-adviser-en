"""
backend/delta_scoring.py
V2 Marginal Delta Scoring: Evaluates cards based on how they fill deck gaps.

Instead of scoring cards by rarity or archetype fit, we score by:
- How much does this card contribute to what the deck NEEDS?
- Penalty for what the deck already has (diminishing returns)
- Dilution cost for adding any card to the deck
"""

from __future__ import annotations

import json
import logging
from dataclasses import dataclass
from typing import Optional

from .deck_profile import DeckProfile, get_card_data
from .gap_analysis import GapVector

log = logging.getLogger(__name__)


def _clamp(v: float, lo: float = 0.0, hi: float = 1.0) -> float:
    return max(lo, min(hi, v))


# Act-aware normalization factors for converting raw stats to contribution.
# Enemies scale with acts: Act 1 enemies have ~50 HP, Act 3 bosses have 300+.
# Static normalizers created an internal contradiction with dynamic ACT_TARGETS.
_ACT_NORMS = {
    1: {"damage": 8.0, "block": 6.0, "draw": 2.0, "scaling": 4.0, "aoe": 0.4},
    2: {"damage": 12.0, "block": 9.0, "draw": 2.0, "scaling": 3.0, "aoe": 0.5},
    3: {"damage": 15.0, "block": 11.0, "draw": 2.0, "scaling": 2.5, "aoe": 0.5},
    4: {"damage": 18.0, "block": 13.0, "draw": 2.0, "scaling": 2.0, "aoe": 0.5},
}
# Fallback (legacy static values)
_DAMAGE_NORM = 10.0
_BLOCK_NORM = 8.0
_DRAW_NORM = 2.0
_SCALING_NORM = 3.0
_AOE_BONUS = 0.5

# Dilution cost curve
_BASE_DILUTION_COST = 2.0       # Base cost for adding any card
_DILUTION_PER_CARD = 0.3        # Additional cost per card in deck over 12
_TARGET_DECK_SIZE = 12          # "Ideal" deck size


@dataclass
class DeltaScore:
    """
    Result of delta scoring for a card.
    """
    card_id: str
    total_delta: float          # Final score (higher = better pick)
    
    # Contribution breakdown
    damage_contrib: float = 0.0
    block_contrib: float = 0.0
    draw_contrib: float = 0.0
    scaling_contrib: float = 0.0
    aoe_contrib: float = 0.0
    
    # Penalties
    dilution_cost: float = 0.0
    surplus_penalty: float = 0.0
    
    # For explanation
    fills_gaps: list[str] = None
    has_surplus: list[str] = None
    
    def __post_init__(self):
        if self.fills_gaps is None:
            self.fills_gaps = []
        if self.has_surplus is None:
            self.has_surplus = []


def compute_dilution_cost(deck_size: int, card_draw: int = 0) -> float:
    """
    Compute dilution cost for adding a card.
    
    Args:
        deck_size: Current deck size
        card_draw: How much draw the card provides (reduces dilution)
    
    Returns:
        Dilution cost (always positive)
    """
    if deck_size <= _TARGET_DECK_SIZE:
        base = _BASE_DILUTION_COST * 0.5  # Small decks have low dilution cost
    else:
        overage = deck_size - _TARGET_DECK_SIZE
        base = _BASE_DILUTION_COST + overage * _DILUTION_PER_CARD
    
    # Draw cards partially offset dilution (they help you see other cards)
    draw_offset = card_draw * 1.5
    
    return max(0.0, base - draw_offset)


def _extract_card_stats(cf: Optional[dict], card_db_entry=None) -> dict:
    """
    Extract raw card stats from card_functions.json entry or fallback Card object.
    Returns dict with keys: damage, block, draw, is_aoe, scaling_value, cost, tags.
    """
    if cf is not None:
        funcs = cf.get("functions", {})
        tags = set(t.lower() for t in cf.get("tags", []))
        cost = cf.get("cost", 1)

        damage_flat = funcs.get("damage_flat", 0) or 0
        hits = funcs.get("hits", 1) or 1
        block_flat = funcs.get("block_flat", 0) or 0
        draw = funcs.get("draw", 0) or 0
        is_aoe = funcs.get("aoe", False) or "aoe" in tags

        scaling_value = 0.0
        scaling_type = funcs.get("scaling_type")
        strength_gain = funcs.get("strength_gain", 0) or 0
        dex_gain = funcs.get("dexterity_gain", 0) or 0
        focus_gain = funcs.get("focus_gain", 0) or 0
        poison = funcs.get("poison", 0) or 0

        if scaling_type:
            scaling_value += 2.0
        if strength_gain > 0:
            scaling_value += strength_gain * 1.5
        if dex_gain > 0:
            scaling_value += dex_gain * 1.2
        if focus_gain > 0:
            scaling_value += focus_gain * 1.5
        if poison > 0:
            scaling_value += poison * 0.5
        if "scaling" in tags or "strength" in tags or "poison" in tags:
            scaling_value += 1.0

        return {
            "damage": damage_flat * hits, "block": block_flat, "draw": draw,
            "is_aoe": is_aoe, "scaling_value": scaling_value, "cost": cost, "tags": tags,
        }

    # Fallback: infer from legacy Card object
    if card_db_entry is not None:
        dmg = getattr(card_db_entry, "base_damage", 0) or 0
        blk = getattr(card_db_entry, "base_block", 0) or 0
        drw = getattr(card_db_entry, "base_draw", 0) or 0
        cst = getattr(card_db_entry, "cost", 1) or 1
        desc = (getattr(card_db_entry, "description", "") or "").lower()
        card_tags = set(t.lower() for t in getattr(card_db_entry, "tags", []))

        is_aoe = "all enem" in desc or "each enemy" in desc or "aoe" in card_tags
        scaling_value = 0.0
        for kw in ("strength", "dexterity", "focus", "poison", "scaling", "star", "doom"):
            if kw in desc or kw in card_tags:
                scaling_value += 2.0
                break

        return {
            "damage": dmg, "block": blk, "draw": drw,
            "is_aoe": is_aoe, "scaling_value": scaling_value, "cost": cst, "tags": card_tags,
        }

    return None


def score_candidate(
    card_id: str,
    gaps: GapVector,
    deck_profile: DeckProfile,
    card_db: Optional[dict] = None,
    existing_scaling_sources: int = 0,
) -> DeltaScore:
    """
    Score a candidate card based on how well it fills deck gaps.

    The formula is:
        delta = Σ(card_contribution[mechanic] × gap[mechanic] × priority[mechanic])
              - dilution_cost
              - surplus_penalty
              - scaling_saturation_penalty

    Args:
        card_id: ID of the card to evaluate
        gaps: Current gap vector
        deck_profile: Current deck profile
        card_db: Optional legacy card database for fallback
        existing_scaling_sources: Count of scaling cards already in deck

    Returns:
        DeltaScore with breakdown
    """
    # Get card data
    cf = get_card_data(card_id)

    # Resolve fallback Card object for unknown cards
    fallback_card = None
    if cf is None and card_db is not None:
        norm = card_id.rstrip("+").lower()
        fallback_card = card_db.get(norm)

    stats = _extract_card_stats(cf, fallback_card)

    if stats is None:
        # Truly unknown card: return small positive score (not 0 — avoids systematic skip bias)
        log.warning(f"Card {card_id} not found anywhere, using minimal positive score")
        dil = compute_dilution_cost(deck_profile.deck_size)
        return DeltaScore(
            card_id=card_id,
            total_delta=max(2.0, 10.0 - dil),
            dilution_cost=dil,
        )

    damage = stats["damage"]
    block = stats["block"]
    draw = stats["draw"]
    is_aoe = stats["is_aoe"]
    scaling_value = stats["scaling_value"]
    tags = stats["tags"]

    # Act-aware normalization: same raw stats have different value by act
    act = gaps.act
    norms = _ACT_NORMS.get(act, _ACT_NORMS[2])
    dmg_norm = norms["damage"]
    blk_norm = norms["block"]
    drw_norm = norms["draw"]
    scl_norm = norms["scaling"]
    aoe_bonus = norms["aoe"]

    # Normalize contributions (0-1 scale roughly)
    damage_contrib = damage / dmg_norm
    block_contrib = block / blk_norm
    draw_contrib = draw / drw_norm
    scaling_contrib = scaling_value / scl_norm
    aoe_contrib = aoe_bonus if is_aoe else 0.0

    # Scaling saturation: diminishing returns for redundant scaling
    # e.g., 3rd Inflame when Demon Form exists is less valuable
    if scaling_contrib > 0.1 and existing_scaling_sources >= 3:
        saturation_factor = max(0.3, 1.0 - (existing_scaling_sources - 2) * 0.2)
        scaling_contrib *= saturation_factor
    
    # Apply gaps and priorities
    damage_delta = damage_contrib * gaps.damage * gaps.damage_priority
    block_delta = block_contrib * gaps.block * gaps.block_priority
    draw_delta = draw_contrib * gaps.draw * gaps.draw_priority
    scaling_delta = scaling_contrib * gaps.scaling * gaps.scaling_priority
    aoe_delta = aoe_contrib * gaps.aoe * gaps.aoe_priority
    
    # Sum positive contributions (filling gaps)
    positive_total = 0.0
    fills_gaps = []
    
    if damage_delta > 0.05:
        positive_total += damage_delta
        fills_gaps.append("damage")
    if block_delta > 0.05:
        positive_total += block_delta
        fills_gaps.append("block")
    if draw_delta > 0.05:
        positive_total += draw_delta
        fills_gaps.append("draw")
    if scaling_delta > 0.05:
        positive_total += scaling_delta
        fills_gaps.append("scaling")
    if aoe_delta > 0.05:
        positive_total += aoe_delta
        fills_gaps.append("aoe")
    
    # Calculate surplus penalty (negative gaps = surplus, adding more hurts)
    surplus_penalty = 0.0
    has_surplus = []
    
    for mechanic, contrib, gap in [
        ("damage", damage_contrib, gaps.damage),
        ("block", block_contrib, gaps.block),
        ("draw", draw_contrib, gaps.draw),
        ("scaling", scaling_contrib, gaps.scaling),
        ("aoe", aoe_contrib, gaps.aoe),
    ]:
        if gap < -0.1 and contrib > 0.1:
            # We have surplus and card adds more = penalty
            penalty = contrib * abs(gap) * 0.5
            surplus_penalty += penalty
            if penalty > 0.05:
                has_surplus.append(mechanic)
    
    # Dilution cost
    dilution_cost = compute_dilution_cost(deck_profile.deck_size, draw)
    
    # Final delta score
    # Scale up to make scores more readable (roughly 0-100 range)
    raw_delta = positive_total - surplus_penalty - dilution_cost / 10.0
    total_delta = raw_delta * 50.0  # Scale to ~0-100
    
    return DeltaScore(
        card_id=card_id,
        total_delta=total_delta,
        damage_contrib=damage_contrib,
        block_contrib=block_contrib,
        draw_contrib=draw_contrib,
        scaling_contrib=scaling_contrib,
        aoe_contrib=aoe_contrib,
        dilution_cost=dilution_cost,
        surplus_penalty=surplus_penalty,
        fills_gaps=fills_gaps,
        has_surplus=has_surplus,
    )


def compute_skip_delta(
    gaps: GapVector,
    deck_profile: DeckProfile,
) -> float:
    """
    Compute delta score for Skip action.

    Fixed: Old formula had double-counting — (deck_size-12)*3 gave linear bonus
    regardless of gap severity, while critical_gap_count penalized separately.
    New formula uses a single unified calculation:
      skip_value = dilution_saved - opportunity_cost

    Returns:
        Skip delta score
    """
    # Dilution saved: the actual dilution cost that picking any card would incur
    dilution_saved = compute_dilution_cost(deck_profile.deck_size, card_draw=0)

    # Opportunity cost: how much value we lose by not filling gaps
    # Weighted by gap severity, not just count (avoids double-counting)
    opportunity_cost = 0.0
    for mechanic in ["damage", "block", "scaling", "draw", "aoe"]:
        gap = gaps.get_gap(mechanic)
        priority = gaps.get_priority(mechanic)
        if gap > 0:
            # Larger gaps = higher cost of skipping (nonlinear)
            opportunity_cost += (gap ** 1.3) * priority * 6.0

    # Consistency bonus: well-rounded decks benefit more from skipping
    consistency_bonus = 0.0
    if not gaps.critical_needs:
        consistency_bonus = 3.0
    if hasattr(deck_profile, "draw_density") and deck_profile.draw_density > 0.6:
        consistency_bonus += 2.0

    return dilution_saved + consistency_bonus - opportunity_cost


def get_delta_explanation(score: DeltaScore) -> str:
    """Generate human-readable explanation of delta score."""
    lines = [f"Delta Score: {score.total_delta:.1f}"]
    
    if score.fills_gaps:
        lines.append(f"  Fills gaps: {', '.join(score.fills_gaps)}")
    
    if score.has_surplus:
        lines.append(f"  Surplus penalty: {', '.join(score.has_surplus)}")
    
    lines.append(f"  Dilution cost: {score.dilution_cost:.2f}")
    
    # Contribution breakdown
    contribs = []
    if score.damage_contrib > 0.1:
        contribs.append(f"dmg:{score.damage_contrib:.2f}")
    if score.block_contrib > 0.1:
        contribs.append(f"blk:{score.block_contrib:.2f}")
    if score.draw_contrib > 0.1:
        contribs.append(f"drw:{score.draw_contrib:.2f}")
    if score.scaling_contrib > 0.1:
        contribs.append(f"scl:{score.scaling_contrib:.2f}")
    if score.aoe_contrib > 0.1:
        contribs.append(f"aoe:{score.aoe_contrib:.2f}")
    
    if contribs:
        lines.append(f"  Contributions: {', '.join(contribs)}")
    
    return "\n".join(lines)
