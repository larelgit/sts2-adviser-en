"""
backend/deck_profiler.py
V2-lite: deck supply/need profiler for CDPE.
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field

from .models import Card, CardType, GamePhase, RunState


def _clamp(v: float, lo: float = 0.0, hi: float = 1.0) -> float:
    return max(lo, min(hi, v))


@dataclass
class DeckProfile:
    # Supply (what deck already provides)
    frontload_damage: float = 0.0
    reliable_block: float = 0.0
    aoe_coverage: float = 0.0
    long_fight_scaling: float = 0.0
    draw_filtering: float = 0.0
    energy_smoothing: float = 0.0
    exhaust_thinning: float = 0.0
    status_handling: float = 0.0

    # Need (what deck currently lacks)
    needs_frontload: float = 0.0
    needs_block: float = 0.0
    needs_aoe: float = 0.0
    needs_scaling: float = 0.0
    needs_draw: float = 0.0
    needs_energy: float = 0.0

    # Meta
    deck_size: int = 0
    target_size: int = 12
    consistency_score: float = 0.5
    dead_draw_rate: float = 0.35
    setup_burden: float = 0.0
    critical_gaps: list[str] = field(default_factory=list)


_AOE_PATTERN = re.compile(r"all enemies|enemy.*all|aoe|multi-target|each enemy")
_SCALING_PATTERN = re.compile(r"strength|dexterity|focus|poison|scaling|star|doom")
_DRAW_PATTERN = re.compile(r"\bdraw\b|\bdiscard\b|\bscry\b|filter")
_ENERGY_PATTERN = re.compile(r"gain .*energy|next turn .*energy|energy")
_STATUS_PATTERN = re.compile(r"status|curse|exhaust")
_SETUP_PATTERN = re.compile(r"next turn|if .* this turn|retain|setup")


def analyze_deck(run_state: RunState, card_db: dict[str, Card], target_size: int = 12) -> DeckProfile:
    """
    Build a lightweight supply/need vector from the current deck.
    """
    deck_cards: list[Card] = []
    for cid in run_state.deck:
        norm = cid.rstrip("+").lower()
        c = card_db.get(norm)
        if c is not None:
            deck_cards.append(c)

    deck_size = len(deck_cards)
    if deck_size == 0:
        return DeckProfile(deck_size=0, target_size=target_size, critical_gaps=["frontload", "block"])

    attack_count = 0
    block_count = 0
    aoe_count = 0
    scaling_count = 0
    draw_points = 0.0
    energy_count = 0
    exhaust_count = 0
    status_count = 0
    heavy_cards = 0
    setup_count = 0
    immediate_cards = 0

    for c in deck_cards:
        desc = (c.description or "").lower()
        tags = {t.lower() for t in c.tags}

        dmg = c.base_damage or 0
        block = c.base_block or 0
        draw = c.base_draw or 0

        if c.card_type == CardType.ATTACK:
            attack_count += 1
            if dmg >= 8 and c.cost <= 1:
                immediate_cards += 1
        if c.card_type == CardType.SKILL and block >= 6:
            block_count += 1
            if c.cost <= 1:
                immediate_cards += 1
        if c.card_type == CardType.POWER:
            scaling_count += 1
            setup_count += 1

        if _AOE_PATTERN.search(desc) or "aoe" in tags or "multi" in tags:
            aoe_count += 1
        if _SCALING_PATTERN.search(desc) or {"scaling", "strength", "poison", "focus", "star", "doom"} & tags:
            scaling_count += 1
        if _DRAW_PATTERN.search(desc) or draw > 0 or {"draw", "discard", "scry", "cycle"} & tags:
            draw_points += max(1.0, float(draw))
        if _ENERGY_PATTERN.search(desc) or c.cost == 0 or {"energy", "xcost", "zero_cost"} & tags:
            energy_count += 1
        if c.keywords.exhaust or "exhaust" in desc or "exhaust" in tags:
            exhaust_count += 1
        if _STATUS_PATTERN.search(desc) or {"status", "curse", "cleanse", "purge"} & tags:
            status_count += 1
        if c.cost >= 2 and dmg <= 0 and block <= 0 and draw <= 0:
            heavy_cards += 1
        if _SETUP_PATTERN.search(desc) or c.keywords.retain or c.keywords.ethereal:
            setup_count += 1

    supply_frontload = _clamp((attack_count * 0.06) + (immediate_cards * 0.04))
    supply_block = _clamp((block_count * 0.07) + (sum(1 for c in deck_cards if (c.base_block or 0) >= 10) * 0.03))
    supply_aoe = _clamp(aoe_count * 0.25)
    supply_scaling = _clamp(scaling_count * 0.12)
    supply_draw = _clamp(draw_points * 0.08)
    supply_energy = _clamp(energy_count * 0.10)
    supply_exhaust = _clamp(exhaust_count * 0.14)
    supply_status = _clamp(status_count * 0.14)

    phase_factor = {
        GamePhase.EARLY: 0.15,
        GamePhase.MID: 0.25,
        GamePhase.LATE: 0.35,
    }.get(run_state.phase, 0.25)

    needs_frontload = _clamp(0.85 - supply_frontload + (0.08 if run_state.phase == GamePhase.EARLY else 0.0))
    needs_block = _clamp(0.85 - supply_block + (0.25 if run_state.hp_ratio < 0.45 else 0.0))
    needs_aoe = _clamp(0.70 - supply_aoe + phase_factor * 0.25)
    needs_scaling = _clamp(0.80 - supply_scaling + phase_factor * 0.35)
    needs_draw = _clamp(0.75 - supply_draw + (0.15 if deck_size > target_size + 2 else 0.0))
    needs_energy = _clamp(0.70 - supply_energy + (0.10 if heavy_cards > max(2, deck_size // 6) else 0.0))

    setup_burden = _clamp(setup_count / max(1, deck_size))
    dead_draw_rate = _clamp((heavy_cards / max(1, deck_size)) * 0.8 + setup_burden * 0.35)
    consistency = _clamp(0.50 + supply_draw * 0.22 + supply_energy * 0.16 - dead_draw_rate * 0.28)

    gaps: list[str] = []
    if needs_block > 0.72:
        gaps.append("block")
    if needs_frontload > 0.72:
        gaps.append("frontload")
    if needs_aoe > 0.72 and run_state.phase != GamePhase.EARLY:
        gaps.append("aoe")
    if needs_scaling > 0.72 and run_state.phase != GamePhase.EARLY:
        gaps.append("scaling")
    if needs_draw > 0.72:
        gaps.append("draw")

    return DeckProfile(
        frontload_damage=supply_frontload,
        reliable_block=supply_block,
        aoe_coverage=supply_aoe,
        long_fight_scaling=supply_scaling,
        draw_filtering=supply_draw,
        energy_smoothing=supply_energy,
        exhaust_thinning=supply_exhaust,
        status_handling=supply_status,
        needs_frontload=needs_frontload,
        needs_block=needs_block,
        needs_aoe=needs_aoe,
        needs_scaling=needs_scaling,
        needs_draw=needs_draw,
        needs_energy=needs_energy,
        deck_size=deck_size,
        target_size=target_size,
        consistency_score=consistency,
        dead_draw_rate=dead_draw_rate,
        setup_burden=setup_burden,
        critical_gaps=gaps,
    )
