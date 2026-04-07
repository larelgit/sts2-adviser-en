"""
backend/gap_analysis.py
V2 Gap Analysis: Computes deck deficits based on act-specific targets.

This is the "brain" that determines what the deck needs.
Instead of static archetype weights, we compute dynamic gaps:
- What does the deck have? (from DeckProfile)
- What does the deck need for this act? (from ACT_TARGETS)
- Gap = Target - Current (positive = need, negative = surplus)
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Optional

from .deck_profile import DeckProfile


def _clamp(v: float, lo: float = 0.0, hi: float = 1.0) -> float:
    return max(lo, min(hi, v))


# Target ratios for each act
# These represent "ideal" deck composition for surviving that act
ACT_TARGETS = {
    1: {
        "damage": 0.55,    # Need decent frontload damage
        "block": 0.35,     # Some block, but less critical early
        "scaling": 0.15,   # Minimal scaling needed
        "draw": 0.20,      # Some draw helps consistency
        "aoe": 0.10,       # Minor AoE for hallway fights
    },
    2: {
        "damage": 0.50,    # Still need damage
        "block": 0.55,     # Block becomes more important
        "scaling": 0.45,   # Need scaling for elites/boss
        "draw": 0.40,      # More draw for consistency
        "aoe": 0.55,       # Many multi-enemy fights
    },
    3: {
        "damage": 0.45,    # Less raw damage needed
        "block": 0.60,     # High block requirement
        "scaling": 0.75,   # Critical for boss fights
        "draw": 0.50,      # Engine consistency
        "aoe": 0.30,       # Less AoE, more single target
    },
    4: {
        "damage": 0.40,    # Heart fight is scaling-based
        "block": 0.65,     # Very high block needed
        "scaling": 0.85,   # Maximum scaling
        "draw": 0.55,      # Full engine
        "aoe": 0.20,       # Mostly single target
    },
}

# Diminishing returns factor for surplus
_SURPLUS_PENALTY_FACTOR = 0.5


@dataclass
class GapVector:
    """
    Computed gaps (deficits) for each mechanic.
    
    Positive value = deck needs this
    Negative value = deck has surplus (diminishing returns)
    """
    damage: float = 0.0
    block: float = 0.0
    scaling: float = 0.0
    draw: float = 0.0
    aoe: float = 0.0
    
    # Priority weights based on urgency
    damage_priority: float = 1.0
    block_priority: float = 1.0
    scaling_priority: float = 1.0
    draw_priority: float = 1.0
    aoe_priority: float = 1.0
    
    # Meta info
    act: int = 1
    critical_needs: list[str] = field(default_factory=list)
    
    def get_gap(self, mechanic: str) -> float:
        """Get gap value for a mechanic."""
        return getattr(self, mechanic, 0.0)
    
    def get_priority(self, mechanic: str) -> float:
        """Get priority weight for a mechanic."""
        return getattr(self, f"{mechanic}_priority", 1.0)


def compute_gap_vector(
    profile: DeckProfile,
    act: int,
    hp_ratio: float = 1.0,
    floor: int = 1,
    has_upcoming_elite: bool = False,
    has_upcoming_boss: bool = False,
) -> GapVector:
    """
    Compute gap vector based on deck profile and current situation.
    
    Args:
        profile: Current deck capabilities
        act: Current act (1-4)
        hp_ratio: Current HP / Max HP
        floor: Current floor number
        has_upcoming_elite: Is there an elite fight coming?
        has_upcoming_boss: Is boss fight imminent?
    
    Returns:
        GapVector with deficits and priorities
    """
    # Get targets for this act (default to act 3 if beyond)
    targets = ACT_TARGETS.get(act, ACT_TARGETS[3])
    
    # Map profile fields to mechanic names
    profile_map = {
        "damage": profile.damage_output,
        "block": profile.block_output,
        "scaling": profile.scaling_quality,
        "draw": profile.draw_density,
        "aoe": profile.aoe_capability,
    }
    
    # Compute raw gaps
    gaps = {}
    for mechanic, target in targets.items():
        current = profile_map.get(mechanic, 0.0)
        
        if current < target:
            # Deficit: need more of this
            gaps[mechanic] = target - current
        else:
            # Surplus: diminishing returns (negative value)
            gaps[mechanic] = -_SURPLUS_PENALTY_FACTOR * (current - target)
    
    # Compute priorities based on situation
    priorities = {
        "damage": 1.0,
        "block": 1.0,
        "scaling": 1.0,
        "draw": 1.0,
        "aoe": 1.0,
    }
    
    # Low HP: prioritize block and immediate survival
    if hp_ratio < 0.40:
        priorities["block"] *= 1.5
        priorities["damage"] *= 1.2  # Need to kill fast
        priorities["scaling"] *= 0.7  # Less time for scaling
    
    # Very low HP: survival mode
    if hp_ratio < 0.25:
        priorities["block"] *= 1.8
        priorities["scaling"] *= 0.5
    
    # Upcoming elite: need frontload
    if has_upcoming_elite:
        priorities["damage"] *= 1.3
        priorities["block"] *= 1.2
        priorities["aoe"] *= 0.8  # Elites are usually single target
    
    # Boss imminent: need scaling
    if has_upcoming_boss:
        priorities["scaling"] *= 1.5
        priorities["draw"] *= 1.2
        priorities["block"] *= 1.3
    
    # Act-specific adjustments
    if act == 1:
        # Early: frontload matters most
        priorities["damage"] *= 1.2
        priorities["scaling"] *= 0.8
    elif act >= 3:
        # Late: scaling and consistency critical
        priorities["scaling"] *= 1.3
        priorities["draw"] *= 1.2
    
    # Large deck penalty: draw becomes more important
    if profile.deck_size > 15:
        priorities["draw"] *= 1.0 + (profile.deck_size - 15) * 0.05
    
    # Identify critical needs (high gap + high priority)
    critical = []
    for mechanic in ["damage", "block", "scaling", "draw", "aoe"]:
        effective_gap = gaps[mechanic] * priorities[mechanic]
        if effective_gap > 0.3:  # Significant deficit
            critical.append(mechanic)
    
    return GapVector(
        damage=gaps["damage"],
        block=gaps["block"],
        scaling=gaps["scaling"],
        draw=gaps["draw"],
        aoe=gaps["aoe"],
        damage_priority=priorities["damage"],
        block_priority=priorities["block"],
        scaling_priority=priorities["scaling"],
        draw_priority=priorities["draw"],
        aoe_priority=priorities["aoe"],
        act=act,
        critical_needs=critical,
    )


def get_gap_summary(gaps: GapVector) -> str:
    """Generate human-readable gap summary."""
    lines = [f"Act {gaps.act} Gap Analysis:"]
    
    for mechanic in ["damage", "block", "scaling", "draw", "aoe"]:
        gap = gaps.get_gap(mechanic)
        priority = gaps.get_priority(mechanic)
        
        if gap > 0.2:
            status = f"NEED (+{gap:.2f})"
        elif gap > 0:
            status = f"want (+{gap:.2f})"
        elif gap > -0.1:
            status = f"ok ({gap:.2f})"
        else:
            status = f"surplus ({gap:.2f})"
        
        prio_str = ""
        if priority > 1.2:
            prio_str = " [HIGH PRIORITY]"
        elif priority < 0.8:
            prio_str = " [low priority]"
        
        lines.append(f"  {mechanic}: {status}{prio_str}")
    
    if gaps.critical_needs:
        lines.append(f"  Critical: {', '.join(gaps.critical_needs)}")
    
    return "\n".join(lines)
