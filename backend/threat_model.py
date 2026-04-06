"""
backend/threat_model.py
V2-lite: threat assessment from run context + deck profile.
"""

from __future__ import annotations

from dataclasses import dataclass

from .deck_profiler import DeckProfile
from .models import GamePhase, RunState


def _clamp(v: float, lo: float = 0.0, hi: float = 1.0) -> float:
    return max(lo, min(hi, v))


@dataclass
class ThreatProfile:
    hallway_pressure: float = 0.0
    elite_readiness: float = 0.0
    boss_plan_completeness: float = 0.0

    survival_urgency: float = 0.0
    greed_tolerance: float = 0.0
    aoe_need: float = 0.0

    tempo_weight: float = 0.0
    consistency_weight: float = 0.0
    scaling_weight: float = 0.0


def assess_threats(run_state: RunState, deck_profile: DeckProfile) -> ThreatProfile:
    """
    Build a lightweight threat profile.
    """
    hp_ratio = run_state.hp_ratio
    phase = run_state.phase

    phase_pressure = {
        GamePhase.EARLY: 0.25,
        GamePhase.MID: 0.45,
        GamePhase.LATE: 0.60,
    }.get(phase, 0.45)

    hallway_pressure = _clamp(
        phase_pressure
        + (0.35 if hp_ratio < 0.45 else 0.0)
        + (0.15 if deck_profile.needs_block > 0.70 else 0.0)
        + (0.10 if deck_profile.needs_frontload > 0.70 else 0.0)
    )

    elite_readiness = _clamp(
        0.50
        + deck_profile.frontload_damage * 0.20
        + deck_profile.reliable_block * 0.24
        + deck_profile.consistency_score * 0.20
        - deck_profile.dead_draw_rate * 0.18
    )

    boss_plan = _clamp(
        0.40
        + deck_profile.long_fight_scaling * 0.35
        + deck_profile.draw_filtering * 0.10
        + deck_profile.energy_smoothing * 0.08
        - (0.12 if phase == GamePhase.LATE and deck_profile.needs_scaling > 0.70 else 0.0)
    )

    survival_urgency = _clamp(
        (1.0 - hp_ratio) * 0.60
        + (1.0 - elite_readiness) * 0.30
        + hallway_pressure * 0.20
    )

    greed_tolerance = _clamp(
        hp_ratio * 0.55
        + deck_profile.consistency_score * 0.20
        + (0.10 if phase == GamePhase.EARLY else 0.0)
        - survival_urgency * 0.35
    )

    aoe_need = _clamp(
        0.45
        + (0.25 if phase != GamePhase.EARLY else 0.0)
        + (0.20 if deck_profile.needs_aoe > 0.70 else 0.0)
        - deck_profile.aoe_coverage * 0.35
    )

    tempo_weight = _clamp(0.35 + survival_urgency * 0.35 + (1.0 - elite_readiness) * 0.20)
    consistency_weight = _clamp(0.30 + deck_profile.dead_draw_rate * 0.30 + (0.15 if run_state.floor > 20 else 0.0))
    scaling_weight = _clamp(0.25 + (0.35 if phase == GamePhase.LATE else 0.15) + deck_profile.needs_scaling * 0.25)

    return ThreatProfile(
        hallway_pressure=hallway_pressure,
        elite_readiness=elite_readiness,
        boss_plan_completeness=boss_plan,
        survival_urgency=survival_urgency,
        greed_tolerance=greed_tolerance,
        aoe_need=aoe_need,
        tempo_weight=tempo_weight,
        consistency_weight=consistency_weight,
        scaling_weight=scaling_weight,
    )
