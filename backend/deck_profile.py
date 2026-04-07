"""
backend/deck_profile.py
V2 Deck Profiler: Analyzes deck capabilities using structured card_functions.json data.

Builds a normalized profile (0.0-1.0) of what the deck can do:
- damage_output: Raw damage capability
- block_output: Defensive capability
- draw_density: Card draw / cycling
- scaling_quality: Scaling potential (strength, poison, focus, orbs, etc.)
- aoe_capability: Multi-target damage
"""

from __future__ import annotations

import json
import logging
from dataclasses import dataclass, field
from pathlib import Path
from typing import Optional

log = logging.getLogger(__name__)


def _clamp(v: float, lo: float = 0.0, hi: float = 1.0) -> float:
    return max(lo, min(hi, v))


# Card functions cache
_CARD_FUNCTIONS: dict[str, dict] = {}


def _load_card_functions() -> None:
    """Load card_functions.json into memory."""
    global _CARD_FUNCTIONS
    if _CARD_FUNCTIONS:
        return
    try:
        from utils.paths import get_app_root
        path = get_app_root() / "data" / "card_functions.json"
        if path.exists():
            with open(path, "r", encoding="utf-8") as f:
                data = json.load(f)
            _CARD_FUNCTIONS = {k.lower(): v for k, v in data.items() if not k.startswith("_")}
            log.info(f"DeckProfile: Loaded {len(_CARD_FUNCTIONS)} card functions")
    except Exception as e:
        log.warning(f"DeckProfile: Failed to load card_functions.json: {e}")


def get_card_data(card_id: str) -> Optional[dict]:
    """Get structured data for a card, or None if not in database."""
    _load_card_functions()
    norm = card_id.rstrip("+").lower()
    return _CARD_FUNCTIONS.get(norm)


@dataclass
class DeckProfile:
    """
    Normalized profile of deck capabilities.
    All metrics are 0.0 (weak) to 1.0 (excellent).
    """
    deck_size: int = 0
    damage_output: float = 0.0      # Raw damage capability
    block_output: float = 0.0       # Defensive capability
    draw_density: float = 0.0       # Card draw / cycling density
    scaling_quality: float = 0.0    # Long-fight scaling potential
    aoe_capability: float = 0.0     # Multi-target damage
    
    # Additional metrics for CDPE
    energy_efficiency: float = 0.0  # Low-cost / 0-cost density
    exhaust_density: float = 0.0    # Exhaust cards for thinning
    status_handling: float = 0.0    # Ability to handle statuses/curses
    
    # Computed needs (what deck is missing)
    needs_damage: float = 0.0
    needs_block: float = 0.0
    needs_draw: float = 0.0
    needs_scaling: float = 0.0
    needs_aoe: float = 0.0
    
    # Critical gaps list
    critical_gaps: list[str] = field(default_factory=list)


# Normalization constants (what counts as "excellent" for each metric)
_NORM_DAMAGE_PER_CARD = 8.0      # Average damage per card for "good" deck
_NORM_BLOCK_PER_CARD = 6.0       # Average block per card
_NORM_DRAW_RATIO = 0.25          # 25% of deck draws cards = excellent
_NORM_SCALING_RATIO = 0.15       # 15% scaling cards = excellent
_NORM_AOE_RATIO = 0.12           # 12% AoE cards = excellent
_NORM_ZERO_COST_RATIO = 0.20     # 20% zero-cost = excellent
_NORM_EXHAUST_RATIO = 0.15       # 15% exhaust = excellent


def build_deck_profile(deck_card_ids: list[str], card_db: Optional[dict] = None) -> DeckProfile:
    """
    Build a normalized DeckProfile from a list of card IDs.
    
    Uses card_functions.json for structured data.
    Falls back to basic estimation for unknown cards.
    
    Args:
        deck_card_ids: List of card IDs in the deck
        card_db: Optional legacy card database (for fallback)
    
    Returns:
        DeckProfile with normalized metrics (0.0-1.0)
    """
    _load_card_functions()
    
    deck_size = len(deck_card_ids)
    if deck_size == 0:
        return DeckProfile(
            deck_size=0,
            critical_gaps=["damage", "block", "draw"]
        )
    
    # Accumulators
    total_damage = 0.0
    total_block = 0.0
    draw_cards = 0
    scaling_cards = 0
    aoe_cards = 0
    zero_cost_cards = 0
    exhaust_cards = 0
    status_cards = 0
    
    cards_found = 0
    
    for card_id in deck_card_ids:
        cf = get_card_data(card_id)
        
        if cf is not None:
            cards_found += 1
            funcs = cf.get("functions", {})
            tags = set(t.lower() for t in cf.get("tags", []))
            cost = cf.get("cost", 1)
            
            # Damage
            damage = funcs.get("damage_flat", 0) or 0
            hits = funcs.get("hits", 1) or 1
            total_damage += damage * hits
            
            # Block
            block = funcs.get("block_flat", 0) or 0
            total_block += block
            
            # Draw
            draw = funcs.get("draw", 0) or 0
            if draw > 0 or "draw" in tags or "cycle" in tags:
                draw_cards += 1
            
            # Scaling
            scaling_type = funcs.get("scaling_type")
            strength_gain = funcs.get("strength_gain", 0) or 0
            dex_gain = funcs.get("dexterity_gain", 0) or 0
            focus_gain = funcs.get("focus_gain", 0) or 0
            poison = funcs.get("poison", 0) or 0
            
            if (scaling_type or strength_gain > 0 or dex_gain > 0 or 
                focus_gain > 0 or poison > 0 or "scaling" in tags or 
                "strength" in tags or "poison" in tags or "focus" in tags):
                scaling_cards += 1
            
            # AoE
            is_aoe = funcs.get("aoe", False)
            if is_aoe or "aoe" in tags or "multi_target" in tags:
                aoe_cards += 1
            
            # Zero cost
            if cost == 0:
                zero_cost_cards += 1
            
            # Exhaust
            is_exhaust = funcs.get("exhaust", False)
            if is_exhaust or "exhaust" in tags:
                exhaust_cards += 1
            
            # Status handling
            if "status_handling" in tags or "purge" in tags or "cleanse" in tags:
                status_cards += 1
                
        elif card_db is not None:
            # Fallback to legacy card_db
            norm_id = card_id.rstrip("+").lower()
            legacy_card = card_db.get(norm_id)
            if legacy_card:
                cards_found += 1
                dmg = getattr(legacy_card, 'base_damage', 0) or 0
                blk = getattr(legacy_card, 'base_block', 0) or 0
                drw = getattr(legacy_card, 'base_draw', 0) or 0
                cst = getattr(legacy_card, 'cost', 1) or 1
                
                total_damage += dmg
                total_block += blk
                if drw > 0:
                    draw_cards += 1
                if cst == 0:
                    zero_cost_cards += 1
    
    # Calculate normalized metrics
    avg_damage = total_damage / deck_size if deck_size > 0 else 0
    avg_block = total_block / deck_size if deck_size > 0 else 0
    
    damage_output = _clamp(avg_damage / _NORM_DAMAGE_PER_CARD)
    block_output = _clamp(avg_block / _NORM_BLOCK_PER_CARD)
    draw_density = _clamp((draw_cards / deck_size) / _NORM_DRAW_RATIO) if deck_size > 0 else 0
    scaling_quality = _clamp((scaling_cards / deck_size) / _NORM_SCALING_RATIO) if deck_size > 0 else 0
    aoe_capability = _clamp((aoe_cards / deck_size) / _NORM_AOE_RATIO) if deck_size > 0 else 0
    energy_efficiency = _clamp((zero_cost_cards / deck_size) / _NORM_ZERO_COST_RATIO) if deck_size > 0 else 0
    exhaust_density = _clamp((exhaust_cards / deck_size) / _NORM_EXHAUST_RATIO) if deck_size > 0 else 0
    status_handling = _clamp(status_cards * 0.3) if status_cards > 0 else 0
    
    # Calculate needs (inverse of what we have)
    # Lower supply = higher need
    needs_damage = _clamp(1.0 - damage_output * 1.2)
    needs_block = _clamp(1.0 - block_output * 1.2)
    needs_draw = _clamp(1.0 - draw_density * 1.1)
    needs_scaling = _clamp(1.0 - scaling_quality * 1.1)
    needs_aoe = _clamp(1.0 - aoe_capability * 1.0)
    
    # Identify critical gaps (need > 0.7)
    critical_gaps = []
    if needs_block > 0.70:
        critical_gaps.append("block")
    if needs_damage > 0.70:
        critical_gaps.append("damage")
    if needs_draw > 0.70:
        critical_gaps.append("draw")
    if needs_scaling > 0.70:
        critical_gaps.append("scaling")
    if needs_aoe > 0.70:
        critical_gaps.append("aoe")
    
    coverage_ratio = cards_found / deck_size if deck_size > 0 else 0
    if coverage_ratio < 0.5:
        log.warning(f"DeckProfile: Only {coverage_ratio:.0%} of deck cards found in database")
    
    return DeckProfile(
        deck_size=deck_size,
        damage_output=damage_output,
        block_output=block_output,
        draw_density=draw_density,
        scaling_quality=scaling_quality,
        aoe_capability=aoe_capability,
        energy_efficiency=energy_efficiency,
        exhaust_density=exhaust_density,
        status_handling=status_handling,
        needs_damage=needs_damage,
        needs_block=needs_block,
        needs_draw=needs_draw,
        needs_scaling=needs_scaling,
        needs_aoe=needs_aoe,
        critical_gaps=critical_gaps,
    )


def get_deck_summary(profile: DeckProfile) -> str:
    """Generate a human-readable summary of deck capabilities."""
    lines = [f"Deck Size: {profile.deck_size}"]
    
    def rating(val: float) -> str:
        if val >= 0.8: return "Excellent"
        if val >= 0.6: return "Good"
        if val >= 0.4: return "Okay"
        if val >= 0.2: return "Weak"
        return "Poor"
    
    lines.append(f"  Damage: {rating(profile.damage_output)} ({profile.damage_output:.2f})")
    lines.append(f"  Block: {rating(profile.block_output)} ({profile.block_output:.2f})")
    lines.append(f"  Draw: {rating(profile.draw_density)} ({profile.draw_density:.2f})")
    lines.append(f"  Scaling: {rating(profile.scaling_quality)} ({profile.scaling_quality:.2f})")
    lines.append(f"  AoE: {rating(profile.aoe_capability)} ({profile.aoe_capability:.2f})")
    
    if profile.critical_gaps:
        lines.append(f"  Critical Gaps: {', '.join(profile.critical_gaps)}")
    
    return "\n".join(lines)
