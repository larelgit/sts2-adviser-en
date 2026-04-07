"""
backend/delta_reasons.py
V2 Explanation Generator: Creates human-readable reasons based on delta scoring.

Replaces old archetype-based explanations with gap-based logic:
- What gaps does this card fill?
- What surplus does it add to?
- Why is Skip a good/bad choice?
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Optional

from .deck_profile import DeckProfile
from .gap_analysis import GapVector, ACT_TARGETS
from .delta_scoring import DeltaScore


# Thresholds for explanation generation
_SIGNIFICANT_GAP = 0.15        # Gap needs to be this big to mention
_SIGNIFICANT_CONTRIB = 0.10    # Contribution needs to be this big to mention
_CRITICAL_GAP = 0.30           # Gap is "critical" if above this
_SURPLUS_THRESHOLD = -0.10     # Surplus kicks in below this


# Mechanic display names
_MECHANIC_NAMES = {
    "damage": "Damage",
    "block": "Block",
    "draw": "Card Draw",
    "scaling": "Scaling",
    "aoe": "AoE",
}


@dataclass
class DeltaReason:
    """A single reason for/against a card pick."""
    text: str
    delta_value: float
    is_positive: bool
    mechanic: Optional[str] = None
    
    def format(self) -> str:
        """Format reason with emoji indicator."""
        if self.is_positive:
            emoji = "🟢"
            sign = "+"
        else:
            emoji = "🔴"
            sign = ""
        
        return f"{emoji} {sign}{self.delta_value:.1f} {self.text}"


def generate_card_reasons(
    delta: DeltaScore,
    gaps: GapVector,
    profile: DeckProfile,
) -> tuple[list[str], list[str]]:
    """
    Generate reasons for and against picking a card.
    
    Returns:
        (reasons_for, reasons_against) - lists of formatted reason strings
    """
    reasons_for = []
    reasons_against = []
    
    targets = ACT_TARGETS.get(gaps.act, ACT_TARGETS[3])
    
    # Check each mechanic for gap-filling
    mechanic_data = [
        ("damage", delta.damage_contrib, gaps.damage, gaps.damage_priority, profile.damage_output),
        ("block", delta.block_contrib, gaps.block, gaps.block_priority, profile.block_output),
        ("draw", delta.draw_contrib, gaps.draw, gaps.draw_priority, profile.draw_density),
        ("scaling", delta.scaling_contrib, gaps.scaling, gaps.scaling_priority, profile.scaling_quality),
        ("aoe", delta.aoe_contrib, gaps.aoe, gaps.aoe_priority, profile.aoe_capability),
    ]
    
    for mechanic, contrib, gap, priority, current in mechanic_data:
        target = targets.get(mechanic, 0.5)
        name = _MECHANIC_NAMES[mechanic]
        
        if gap > _SIGNIFICANT_GAP and contrib > _SIGNIFICANT_CONTRIB:
            # Card fills a gap
            effective_delta = contrib * gap * priority * 50  # Scale to match score
            
            if gap > _CRITICAL_GAP:
                # Critical gap
                reasons_for.append(
                    f"🟢 +{effective_delta:.1f} {name}. "
                    f"Fills critical deficit (target {target*100:.0f}%, current {current*100:.0f}%)."
                )
            else:
                # Normal gap
                reasons_for.append(
                    f"🟢 +{effective_delta:.1f} {name}. "
                    f"Improves deck coverage (need {gap*100:.0f}% more)."
                )
        
        elif gap < _SURPLUS_THRESHOLD and contrib > _SIGNIFICANT_CONTRIB:
            # Card adds to surplus (diminishing returns)
            penalty = contrib * abs(gap) * 0.5 * 50  # Scale to match score
            surplus_pct = abs(gap) * 100
            
            reasons_against.append(
                f"🔴 -{penalty:.1f} {name} surplus. "
                f"Already {surplus_pct:.0f}% over target, adding more dilutes the deck."
            )
    
    # Dilution cost
    if delta.dilution_cost > 2.5:
        dilution_penalty = delta.dilution_cost / 10.0 * 50
        reasons_against.append(
            f"🔴 -{dilution_penalty:.1f} Dilution. "
            f"Deck size ({profile.deck_size}) above optimal, adding cards reduces consistency."
        )
    
    # High priority modifiers
    if gaps.block_priority > 1.3:
        reasons_for.insert(0, "⚠️ Low HP - Block is high priority!")
    
    if gaps.scaling_priority > 1.3:
        reasons_for.insert(0, "⚠️ Boss incoming - Scaling is high priority!")
    
    # Draw value in large decks
    if profile.deck_size > 15 and delta.draw_contrib > 0.3:
        reasons_for.append(
            f"🟢 Draw value. Large deck ({profile.deck_size} cards) benefits from card draw."
        )
    
    return reasons_for, reasons_against


def generate_skip_reason(
    skip_delta: float,
    gaps: GapVector,
    profile: DeckProfile,
    best_card_delta: float,
) -> str:
    """
    Generate explanation for Skip recommendation.
    
    Args:
        skip_delta: Delta score for skip action
        gaps: Current gap vector
        profile: Current deck profile
        best_card_delta: Delta score of the best card option
    
    Returns:
        Formatted skip reason string
    """
    # Determine if skip is recommended
    skip_margin = skip_delta - best_card_delta
    
    if skip_margin > 5:
        # Skip is clearly better
        emoji = "⚪"
        
        reasons = []
        if profile.deck_size > 12:
            reasons.append(f"deck is already {profile.deck_size} cards")
        if not gaps.critical_needs:
            reasons.append("no critical gaps to fill")
        if profile.draw_density > 0.5:
            reasons.append("good draw consistency")
        
        reason_text = ", ".join(reasons) if reasons else "offered cards don't improve the deck"
        
        return f"{emoji} SKIP (+{skip_delta:.1f}). Deck is efficient: {reason_text}."
    
    elif skip_margin > 0:
        # Skip is slightly better
        return f"⚪ SKIP (+{skip_delta:.1f}). Marginal - none of the cards significantly improve the deck."
    
    else:
        # Skip is worse than best card
        penalty = abs(skip_margin)
        
        if gaps.critical_needs:
            needs = ", ".join(gaps.critical_needs)
            return f"🔴 SKIP (-{penalty:.1f}). Not recommended - deck needs: {needs}."
        else:
            return f"⚪ SKIP ({skip_delta:.1f}). Viable but picking would be better (+{penalty:.1f})."


def generate_verdict(
    card_deltas: list[DeltaScore],
    skip_delta: float,
    gaps: GapVector,
    profile: DeckProfile,
) -> str:
    """
    Generate final verdict/recommendation.
    
    Returns:
        Formatted verdict string with recommendation
    """
    if not card_deltas:
        return "⚪ No cards to evaluate."
    
    # Find best card
    best_card = max(card_deltas, key=lambda d: d.total_delta)
    best_delta = best_card.total_delta
    
    # Compare with skip
    if skip_delta > best_delta + 5:
        # Skip is significantly better
        return (
            f"📌 VERDICT: SKIP\n"
            f"   Skip score: {skip_delta:.1f}\n"
            f"   Best card ({best_card.card_id}): {best_delta:.1f}\n"
            f"   Deck efficiency is more valuable than any offered card."
        )
    
    elif skip_delta > best_delta:
        # Skip is slightly better
        return (
            f"📌 VERDICT: SKIP (marginal)\n"
            f"   Skip: {skip_delta:.1f} vs Best card: {best_delta:.1f}\n"
            f"   Consider skipping, but {best_card.card_id} is acceptable."
        )
    
    else:
        # Card is better
        margin = best_delta - skip_delta
        
        if margin > 15:
            confidence = "Strong pick"
        elif margin > 8:
            confidence = "Good pick"
        else:
            confidence = "Marginal pick"
        
        fills = ", ".join(best_card.fills_gaps) if best_card.fills_gaps else "general value"
        
        return (
            f"📌 VERDICT: {best_card.card_id.upper()}\n"
            f"   Score: {best_delta:.1f} (+{margin:.1f} vs Skip)\n"
            f"   {confidence} - fills: {fills}"
        )


def format_card_summary(
    delta: DeltaScore,
    gaps: GapVector,
    profile: DeckProfile,
    rank: int = 1,
) -> str:
    """
    Format a complete card summary for UI display.
    
    Args:
        delta: Delta score for the card
        gaps: Current gap vector
        profile: Current deck profile
        rank: Card rank (1 = best, 2 = second, etc.)
    
    Returns:
        Multi-line formatted summary
    """
    reasons_for, reasons_against = generate_card_reasons(delta, gaps, profile)
    
    # Header
    lines = [
        f"#{rank} {delta.card_id.upper()} — Score: {delta.total_delta:.1f}"
    ]
    
    # Positive reasons
    for reason in reasons_for[:3]:  # Limit to top 3
        lines.append(f"   {reason}")
    
    # Negative reasons
    for reason in reasons_against[:2]:  # Limit to top 2
        lines.append(f"   {reason}")
    
    return "\n".join(lines)
