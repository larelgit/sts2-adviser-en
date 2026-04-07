"""
backend/threat_model.py
V2.2: threat assessment from run context + deck profile + boss threat database.

Changes in V2.2:
- Exact boss targeting: when save file provides boss_id, use that specific boss's
  priority_needs instead of averaging across all possible bosses in the act.
- Zone-aware boss lookup: uses zone_id (overgrowth/underdocks/hive/glory) to
  narrow boss pool when exact boss is unknown.
- get_specific_boss_priorities() for targeted boss data.
"""

from __future__ import annotations

import json
import logging
from dataclasses import dataclass, field
from pathlib import Path
from typing import Optional

from .deck_profiler import DeckProfile
from .models import GamePhase, RunState

log = logging.getLogger(__name__)


def _clamp(v: float, lo: float = 0.0, hi: float = 1.0) -> float:
    return max(lo, min(hi, v))


# ---------------------------------------------------------------------------
# Boss threat database
# ---------------------------------------------------------------------------
_BOSS_DB: dict = {}


def _load_boss_db() -> None:
    global _BOSS_DB
    if _BOSS_DB:
        return
    try:
        from utils.paths import get_app_root
        path = get_app_root() / "data" / "boss_threats.json"
        if path.exists():
            with open(path, "r", encoding="utf-8") as f:
                _BOSS_DB = json.load(f)
            log.info(f"Loaded boss threat database")
    except Exception as e:
        log.warning(f"Failed to load boss_threats.json: {e}")


def get_specific_boss_priorities(act: int, boss_id: str, zone_id: Optional[str] = None) -> dict[str, float]:
    """
    Get priority_needs for a SPECIFIC boss (identified from save file).

    Args:
        act: Act number (1-3)
        boss_id: Boss ID from save file (e.g. "CEREMONIAL_BEAST", "KNOWLEDGE_DEMON")
        zone_id: Optional zone (e.g. "overgrowth") to narrow lookup

    Returns:
        priority_needs dict for the specific boss, or empty dict if not found.
    """
    _load_boss_db()
    act_key = f"act_{act}"
    act_data = _BOSS_DB.get(act_key, {})
    if not act_data:
        return {}

    # If zone is known, search only in that zone
    if zone_id:
        zone_data = act_data.get(zone_id, {})
        if isinstance(zone_data, dict) and boss_id in zone_data:
            boss_data = zone_data[boss_id]
            if isinstance(boss_data, dict) and "priority_needs" in boss_data:
                log.info(f"Exact boss match: {boss_id} in {zone_id} (act {act})")
                return boss_data["priority_needs"]

    # Fallback: search all zones in the act for this boss_id
    for zone_name, zone_data in act_data.items():
        if isinstance(zone_data, dict) and boss_id in zone_data:
            boss_data = zone_data[boss_id]
            if isinstance(boss_data, dict) and "priority_needs" in boss_data:
                log.info(f"Exact boss match: {boss_id} in {zone_name} (act {act})")
                return boss_data["priority_needs"]

    log.debug(f"Boss {boss_id} not found in boss_threats.json for act {act}")
    return {}


def get_boss_priority_overrides(act: int, zone_id: Optional[str] = None) -> dict[str, float]:
    """
    Get aggregated priority needs across bosses in an act.
    If zone_id is known, only average bosses in that zone.
    Otherwise average across all possible bosses.
    """
    _load_boss_db()
    act_key = f"act_{act}"
    act_data = _BOSS_DB.get(act_key, {})
    if not act_data:
        return {}

    totals: dict[str, float] = {}
    count = 0

    zones_to_scan = {}
    if zone_id and zone_id in act_data:
        zones_to_scan = {zone_id: act_data[zone_id]}
    else:
        zones_to_scan = act_data

    for zone_data in zones_to_scan.values():
        if isinstance(zone_data, dict):
            for boss_id, boss_data in zone_data.items():
                if isinstance(boss_data, dict) and "priority_needs" in boss_data:
                    count += 1
                    for mechanic, value in boss_data["priority_needs"].items():
                        totals[mechanic] = totals.get(mechanic, 0.0) + value

    if count == 0:
        return {}

    return {k: round(v / count, 2) for k, v in totals.items()}


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

    # V2.1: Boss-specific priority overrides (averaged across possible bosses)
    boss_priorities: dict = field(default_factory=dict)


def assess_threats(run_state: RunState, deck_profile: DeckProfile) -> ThreatProfile:
    """
    Build a lightweight threat profile.
    V2.2: Uses exact boss ID from save file when available, falls back to
    zone-averaged or act-averaged boss priorities.
    """
    hp_ratio = run_state.hp_ratio
    phase = run_state.phase
    current_act = run_state.current_act

    # V2.2: Try exact boss first, then zone average, then act average
    boss_id = run_state.current_boss_id
    zone_id = run_state.zone_id
    if boss_id:
        boss_priorities = get_specific_boss_priorities(current_act, boss_id, zone_id)
        if not boss_priorities:
            # Boss not in DB, fall back to zone/act average
            boss_priorities = get_boss_priority_overrides(current_act, zone_id)
    else:
        boss_priorities = get_boss_priority_overrides(current_act, zone_id)

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

    # Boss-aware AoE need: if bosses in current act need AoE, boost it
    boss_aoe_need = boss_priorities.get("aoe", 0.0) if boss_priorities else 0.0
    aoe_need = _clamp(
        0.45
        + (0.25 if phase != GamePhase.EARLY else 0.0)
        + (0.20 if deck_profile.needs_aoe > 0.70 else 0.0)
        + boss_aoe_need * 0.15  # Boss-specific AoE boost
        - deck_profile.aoe_coverage * 0.35
    )

    tempo_weight = _clamp(0.35 + survival_urgency * 0.35 + (1.0 - elite_readiness) * 0.20)
    consistency_weight = _clamp(0.30 + deck_profile.dead_draw_rate * 0.30 + (0.15 if run_state.floor > 20 else 0.0))

    # Boss-aware scaling weight
    boss_scaling_need = boss_priorities.get("scaling", 0.0) if boss_priorities else 0.0
    scaling_weight = _clamp(
        0.25
        + (0.35 if phase == GamePhase.LATE else 0.15)
        + deck_profile.needs_scaling * 0.25
        + boss_scaling_need * 0.10  # Boss-specific scaling boost
    )

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
        boss_priorities=boss_priorities,
    )
