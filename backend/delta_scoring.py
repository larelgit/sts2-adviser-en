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


# Normalization factors for converting raw stats to contribution
_DAMAGE_NORM = 10.0     # 10 damage = 1.0 contribution
_BLOCK_NORM = 8.0       # 8 block = 1.0 contribution
_DRAW_NORM = 2.0        # 2 draw = 1.0 contribution
_SCALING_NORM = 3.0     # 3 scaling points = 1.0 contribution
_AOE_BONUS = 0.5        # AoE cards get flat bonus to AoE contribution

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


def score_candidate(
    card_id: str,
    gaps: GapVector,
    deck_profile: DeckProfile,
    card_db: Optional[dict] = None,
) -> DeltaScore:
    """
    Score a candidate card based on how well it fills deck gaps.
    
    The formula is:
        delta = Σ(card_contribution[mechanic] × gap[mechanic] × priority[mechanic])
              - dilution_cost
              - surplus_penalty
    
    Args:
        card_id: ID of the card to evaluate
        gaps: Current gap vector
        deck_profile: Current deck profile
        card_db: Optional legacy card database for fallback
    
    Returns:
        DeltaScore with breakdown
    """
    # Get card data
    cf = get_card_data(card_id)
    
    if cf is None:
        # Fallback: return neutral score
        log.warning(f"Card {card_id} not found in database, using neutral score")
        return DeltaScore(
            card_id=card_id,
            total_delta=0.0,
            dilution_cost=compute_dilution_cost(deck_profile.deck_size),
        )
    
    funcs = cf.get("functions", {})
    tags = set(t.lower() for t in cf.get("tags", []))
    cost = cf.get("cost", 1)
    
    # Extract raw stats
    damage_flat = funcs.get("damage_flat", 0) or 0
    hits = funcs.get("hits", 1) or 1
    block_flat = funcs.get("block_flat", 0) or 0
    draw = funcs.get("draw", 0) or 0
    is_aoe = funcs.get("aoe", False) or "aoe" in tags
    
    # Scaling detection
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
    
    # Normalize contributions (0-1 scale roughly)
    damage_contrib = (damage_flat * hits) / _DAMAGE_NORM
    block_contrib = block_flat / _BLOCK_NORM
    draw_contrib = draw / _DRAW_NORM
    scaling_contrib = scaling_value / _SCALING_NORM
    aoe_contrib = _AOE_BONUS if is_aoe else 0.0
    
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
    
    Skip is better when:
    - Deck is already large (high dilution cost avoided)
    - No critical gaps (deck is well-rounded)
    - All offered cards add to surplus
    
    Returns:
        Skip delta score
    """
    # Base skip value
    base = 0.0
    
    # Dilution avoidance (not adding a card is good for large decks)
    if deck_profile.deck_size > _TARGET_DECK_SIZE:
        overage = deck_profile.deck_size - _TARGET_DECK_SIZE
        base += overage * 3.0  # +3 points per card over target
    
    # No critical needs = skip is safer
    if not gaps.critical_needs:
        base += 5.0
    else:
        # Critical needs = penalty for skipping
        base -= len(gaps.critical_needs) * 8.0
    
    # Good draw = consistency is already good
    if deck_profile.draw_density > 0.6:
        base += 3.0
    
    return base


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
