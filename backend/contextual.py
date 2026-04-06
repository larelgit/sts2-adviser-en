from __future__ import annotations

import math
import re
from collections import Counter
from collections.abc import Mapping
from dataclasses import dataclass, field
from typing import Iterable

from .models import Card, CardType, Character, RunState


_SCALING_POWERS = {
    "accuracy",
    "arsenal",
    "biassed_cognition",
    "biased_cognition",
    "countdown",
    "dansemacabre",
    "darkembrace",
    "dexterity",
    "doom",
    "focus",
    "mantra",
    "poison",
    "rupture",
    "strength",
    "thorns",
}

_DEFENSIVE_POWERS = {
    "dexterity",
    "intangible",
    "metallicize",
    "plated armor",
    "thorns",
    "weak",
}

_ATTACK_SPAWNS = {
    "minion_dive_bomb",
    "minion_strike",
    "shiv",
}

_NEED_LABELS = {
    "frontload": "frontload",
    "block": "reliable block",
    "aoe": "AoE coverage",
    "scaling": "boss scaling",
    "consistency": "draw consistency",
    "energy": "energy smoothing",
    "cleanup": "cleanup/status handling",
}


def clamp01(value: float) -> float:
    return max(0.0, min(1.0, float(value)))


def soft_or(values: Iterable[float]) -> float:
    product = 1.0
    any_value = False
    for value in values:
        clipped = clamp01(value)
        if clipped <= 0.0:
            continue
        any_value = True
        product *= 1.0 - clipped
    return 1.0 - product if any_value else 0.0


def sigmoid_score(value: float, center: float, width: float) -> float:
    safe_width = max(width, 1e-6)
    return 1.0 / (1.0 + math.exp(-(value - center) / safe_width))


def _clean_text(card: Card, raw_card: Mapping | None) -> tuple[str, str]:
    raw_text = (
        (raw_card or {}).get("description_raw")
        or (raw_card or {}).get("description")
        or card.description
        or ""
    )
    lower = raw_text.lower().replace("\\n", " ").replace("\n", " ")
    cleaned = re.sub(r"\[[^\]]+\]", " ", lower)
    cleaned = re.sub(r"\s+", " ", cleaned).strip()
    return lower, cleaned


def _extract_powers(raw_card: Mapping | None) -> tuple[str, ...]:
    powers = []
    for item in (raw_card or {}).get("powers_applied") or []:
        if isinstance(item, Mapping):
            key = str(item.get("power_key") or item.get("power") or "").strip().lower()
            if key:
                powers.append(key)
    return tuple(sorted(set(powers)))


def _extract_keywords(card: Card, raw_card: Mapping | None) -> tuple[str, ...]:
    keywords: set[str] = set()
    for item in (raw_card or {}).get("keywords_key") or (raw_card or {}).get("keywords") or []:
        key = str(item).strip().lower()
        if key:
            keywords.add(key)
    if card.keywords.exhaust:
        keywords.add("exhaust")
    if card.keywords.innate:
        keywords.add("innate")
    if card.keywords.retain:
        keywords.add("retain")
    if card.keywords.ethereal:
        keywords.add("ethereal")
    if card.keywords.scry:
        keywords.add("scry")
    for item in card.keywords.extras:
        key = str(item).strip().lower()
        if key:
            keywords.add(key)
    return tuple(sorted(keywords))


def _extract_tags(card: Card, raw_card: Mapping | None) -> tuple[str, ...]:
    tags: set[str] = set()
    for item in (raw_card or {}).get("tags") or []:
        key = str(item).strip().lower()
        if key:
            tags.add(key)
    for item in card.tags:
        key = str(item).strip().lower()
        if key:
            tags.add(key)
    return tuple(sorted(tags))


def _extract_spawns(card: Card, raw_card: Mapping | None) -> tuple[str, ...]:
    spawns: set[str] = set()
    for item in (raw_card or {}).get("spawns_cards") or []:
        key = str(item).strip().lower()
        if key:
            spawns.add(key)
    for item in card.spawned_cards:
        key = str(item).strip().lower()
        if key:
            spawns.add(key)
    return tuple(sorted(spawns))


def _collect_signals(
    *,
    raw_text: str,
    clean_text: str,
    powers: tuple[str, ...],
    keywords: tuple[str, ...],
    tags: tuple[str, ...],
    spawns: tuple[str, ...],
    cost: int,
    hp_loss: int,
) -> tuple[str, ...]:
    signals: set[str] = set()

    keyword_set = set(keywords)
    tag_set = set(tags)
    spawn_set = set(spawns)
    power_set = set(powers)

    if "ostyattack" in tag_set or re.search(r"\bosty\b|\bsummon\b|\bminion\b", clean_text):
        signals.add("osty")
    if "soul" in clean_text or "soul" in spawn_set:
        signals.add("soul")
    if "doom" in clean_text or "doom" in power_set:
        signals.add("doom")
    if "ethereal" in clean_text or "ethereal" in keyword_set:
        signals.add("ethereal")
    if "shiv" in clean_text or "shiv" in spawn_set or "shiv" in tag_set:
        signals.add("shiv")
    if "poison" in clean_text or "poison" in power_set:
        signals.add("poison")
    if "discard" in clean_text or "sly" in keyword_set:
        signals.update({"discard", "sly"})
    if (
        "channel" in clean_text
        or "evoke" in clean_text
        or "orb" in clean_text
        or {"focus", "frost", "dark"} & power_set
    ):
        signals.add("orb")
    if "focus" in clean_text or "focus" in power_set:
        signals.add("focus")
    if "dark" in clean_text or "dark" in power_set:
        signals.add("dark")
    if "frost" in clean_text or "frost" in power_set:
        signals.add("frost")
    if cost == 0 or re.search(r"\b0 cost\b|costs? 0", clean_text):
        signals.add("zero_cost")
    if "claw" in clean_text:
        signals.add("claw")
    if "strength" in clean_text or "strength" in power_set:
        signals.add("strength")
    if hp_loss > 0 or re.search(r"lose \d+ ?hp", clean_text):
        signals.add("self_damage")
    if "exhaust" in clean_text or "exhaust" in keyword_set:
        signals.add("exhaust")
    if "status" in clean_text or "curse" in clean_text:
        signals.add("status")
    if "[star:" in raw_text or re.search(r"\bstar\b", clean_text):
        signals.add("star")
    if "forge" in clean_text:
        signals.add("forge")
    if "colorless" in clean_text:
        signals.add("colorless")

    return tuple(sorted(signals))


@dataclass(frozen=True)
class CardProfile:
    frontload: float
    block: float
    aoe: float
    scaling: float
    draw: float
    consistency: float
    energy: float
    cleanup: float
    setup: float
    upgrade_tax: float
    dead_draw: float
    immediate_impact: float
    powers: tuple[str, ...] = ()
    keywords: tuple[str, ...] = ()
    tags: tuple[str, ...] = ()
    spawns: tuple[str, ...] = ()
    signals: tuple[str, ...] = ()
    raw_text: str = ""
    clean_text: str = ""


@dataclass(frozen=True)
class DeckMetrics:
    size: int
    frontload: float
    block: float
    aoe: float
    scaling: float
    draw: float
    consistency: float
    energy: float
    cleanup: float
    setup: float
    dead_draw: float
    upgrade_tax: float
    average_cost: float
    starter_density: float
    status_density: float
    power_density: float
    signal_strengths: Mapping[str, float] = field(default_factory=dict)


@dataclass
class RunContext:
    deck: DeckMetrics
    needs: dict[str, float]
    short_term_survival: float
    boss_plan_need: float
    dilution_pressure: float
    greed_tolerance: float
    elite_pressure: float
    boss_pressure: float
    skip_score_norm: float
    skip_threshold: float
    input_confidence: float
    motif_confidences: dict[str, float] = field(default_factory=dict)


def profile_card(card: Card, raw_card: Mapping | None = None) -> CardProfile:
    raw_text, clean_text = _clean_text(card, raw_card)
    powers = _extract_powers(raw_card)
    keywords = _extract_keywords(card, raw_card)
    tags = _extract_tags(card, raw_card)
    spawns = _extract_spawns(card, raw_card)

    damage = card.base_damage or int((raw_card or {}).get("damage") or 0)
    raw_hit_count = (raw_card or {}).get("hit_count")
    hit_count = card.hit_count if card.hit_count is not None else int(raw_hit_count or (1 if damage else 0))
    block = card.base_block or int((raw_card or {}).get("block") or 0)
    draw = card.base_draw or int((raw_card or {}).get("cards_draw") or 0)
    energy_gain = card.energy_gain or int((raw_card or {}).get("energy_gain") or 0)
    hp_loss = card.hp_loss or int((raw_card or {}).get("hp_loss") or 0)

    if card.card_type in (CardType.STATUS, CardType.CURSE):
        return CardProfile(
            frontload=0.0,
            block=0.0,
            aoe=0.0,
            scaling=0.0,
            draw=0.0,
            consistency=0.0,
            energy=0.0,
            cleanup=0.0,
            setup=0.35,
            upgrade_tax=0.0,
            dead_draw=1.0,
            immediate_impact=0.0,
            powers=powers,
            keywords=keywords,
            tags=tags,
            spawns=spawns,
            signals=_collect_signals(
                raw_text=raw_text,
                clean_text=clean_text,
                powers=powers,
                keywords=keywords,
                tags=tags,
                spawns=spawns,
                cost=card.cost,
                hp_loss=hp_loss,
            ),
            raw_text=raw_text,
            clean_text=clean_text,
        )

    cost = 2 if card.cost < 0 else card.cost
    total_damage = damage * max(1, hit_count)
    has_aoe = bool(re.search(r"\ball enemies?\b|\ball enemy\b|\beach enemy\b", clean_text))
    has_filter = bool(
        re.search(
            r"\bdiscard\b|\btransform\b|\bscry\b|put .+ on top of your draw pile|choose 1 of 3|choose a card|random colorless",
            clean_text,
        )
    )
    has_draw_hook = bool(
        draw > 0
        or re.search(r"\bdraw \d+\b|put .+ into your hand|add .+ into your hand", clean_text)
    )
    has_cost_reduction = bool(
        energy_gain > 0
        or re.search(r"costs? .+ less|free to play|reduce .+ cost|costs? 1", clean_text)
    )
    has_setup_text = bool(
        re.search(r"next turn|at the start of your turn|whenever|for each|additional time", clean_text)
    )
    has_status_cleanup = bool(
        re.search(r"\bstatus\b|\bcurse\b|\bremove\b|\bpurge\b|\btransform\b", clean_text)
    )
    power_set = set(powers)
    generated_attack_value = 0.18 * sum(1 for spawn in spawns if spawn in _ATTACK_SPAWNS)
    debuff_value = 0.12 if power_set & {"vulnerable", "weak", "frail"} else 0.0
    defensive_hook = 0.12 if power_set & _DEFENSIVE_POWERS else 0.0

    frontload = clamp01(total_damage / 18.0 + generated_attack_value + debuff_value)
    block_score = clamp01(block / 12.0 + defensive_hook)
    aoe = clamp01(0.72 if has_aoe else 0.0)
    draw_score = clamp01(draw / 3.0 + (0.18 if "choose 1 of 3" in clean_text else 0.0))
    consistency = clamp01(
        draw_score
        + (0.22 if has_filter else 0.0)
        + (0.10 if {"retain", "innate", "scry"} & set(keywords) else 0.0)
    )
    energy = clamp01(
        energy_gain / 2.0
        + (0.18 if has_cost_reduction else 0.0)
        + (0.10 if card.cost == 0 and (frontload > 0.15 or block_score > 0.15 or draw_score > 0.15) else 0.0)
    )
    cleanup = clamp01(
        (0.45 if "exhaust" in keywords or "exhaust" in clean_text else 0.0)
        + (0.35 if has_status_cleanup else 0.0)
        + (0.18 if "ethereal" in keywords or "ethereal" in clean_text else 0.0)
    )
    scaling = clamp01(
        (0.42 if card.card_type == CardType.POWER else 0.0)
        + (0.32 if power_set & _SCALING_POWERS else 0.0)
        + (0.18 if has_setup_text else 0.0)
        + (
            0.12
            if {"star", "forge", "soul", "doom", "poison", "focus", "dark", "strength"} & set(
                _collect_signals(
                    raw_text=raw_text,
                    clean_text=clean_text,
                    powers=powers,
                    keywords=keywords,
                    tags=tags,
                    spawns=spawns,
                    cost=card.cost,
                    hp_loss=hp_loss,
                )
            )
            else 0.0
        )
    )

    immediate_impact = soft_or(
        [
            frontload * 0.95,
            block_score * 0.85,
            draw_score * 0.75,
            energy * 0.70,
            aoe * 0.65,
        ]
    )
    setup = clamp01(
        (0.22 if card.card_type == CardType.POWER else 0.0)
        + 0.18 * max(cost - 1.0, 0.0)
        + (0.18 if has_setup_text else 0.0)
        + (0.10 if scaling > 0.55 and immediate_impact < 0.35 else 0.0)
        - 0.25 * immediate_impact
    )
    upgrade_tax = clamp01(
        (0.12 if cost >= 2 else 0.0)
        + (0.10 if card.card_type == CardType.POWER else 0.0)
        + (0.08 if "upgrade" in clean_text else 0.0)
    )
    dead_draw = clamp01(
        0.42 * setup
        + 0.10 * upgrade_tax
        + 0.12 * max(cost - 1.5, 0.0)
        - 0.30 * immediate_impact
        - 0.10 * consistency
        - 0.06 * cleanup
    )
    if card.card_type == CardType.POWER and scaling > 0.55 and immediate_impact < 0.35:
        dead_draw = clamp01(dead_draw + 0.08)

    return CardProfile(
        frontload=frontload,
        block=block_score,
        aoe=aoe,
        scaling=scaling,
        draw=draw_score,
        consistency=consistency,
        energy=energy,
        cleanup=cleanup,
        setup=setup,
        upgrade_tax=upgrade_tax,
        dead_draw=dead_draw,
        immediate_impact=immediate_impact,
        powers=powers,
        keywords=keywords,
        tags=tags,
        spawns=spawns,
        signals=_collect_signals(
            raw_text=raw_text,
            clean_text=clean_text,
            powers=powers,
            keywords=keywords,
            tags=tags,
            spawns=spawns,
            cost=card.cost,
            hp_loss=hp_loss,
        ),
        raw_text=raw_text,
        clean_text=clean_text,
    )


def build_deck_metrics(deck_cards: Iterable[CardProfile], raw_costs: Iterable[int], starters: Iterable[bool], power_cards: Iterable[bool], status_cards: Iterable[bool]) -> DeckMetrics:
    profiles = list(deck_cards)
    costs = [2 if value < 0 else value for value in raw_costs]
    starter_flags = list(starters)
    power_flags = list(power_cards)
    status_flags = list(status_cards)
    size = len(profiles)
    if size == 0:
        return DeckMetrics(
            size=0,
            frontload=0.0,
            block=0.0,
            aoe=0.0,
            scaling=0.0,
            draw=0.0,
            consistency=0.0,
            energy=0.0,
            cleanup=0.0,
            setup=0.0,
            dead_draw=0.0,
            upgrade_tax=0.0,
            average_cost=0.0,
            starter_density=0.0,
            status_density=0.0,
            power_density=0.0,
            signal_strengths={},
        )

    totals = {
        "frontload": sum(profile.frontload for profile in profiles),
        "block": sum(profile.block for profile in profiles),
        "aoe": sum(profile.aoe for profile in profiles),
        "scaling": sum(profile.scaling for profile in profiles),
        "draw": sum(profile.draw for profile in profiles),
        "consistency": sum(profile.consistency for profile in profiles),
        "energy": sum(profile.energy for profile in profiles),
        "cleanup": sum(profile.cleanup for profile in profiles),
        "setup": sum(profile.setup for profile in profiles),
        "dead_draw": sum(profile.dead_draw for profile in profiles),
        "upgrade_tax": sum(profile.upgrade_tax for profile in profiles),
    }

    signal_counter: Counter[str] = Counter()
    for profile in profiles:
        signal_counter.update(profile.signals)
    signal_strengths = {
        signal: clamp01(count / 2.5)
        for signal, count in signal_counter.items()
    }

    return DeckMetrics(
        size=size,
        frontload=clamp01(totals["frontload"] / max(3.8, size * 0.22)),
        block=clamp01(totals["block"] / max(3.6, size * 0.20)),
        aoe=clamp01(totals["aoe"] / 1.6),
        scaling=clamp01(totals["scaling"] / max(2.3, size * 0.12)),
        draw=clamp01(totals["draw"] / max(1.8, size * 0.10)),
        consistency=clamp01(totals["consistency"] / max(2.2, size * 0.12)),
        energy=clamp01(totals["energy"] / 1.8),
        cleanup=clamp01(totals["cleanup"] / 1.7),
        setup=clamp01(totals["setup"] / max(3.0, size * 0.16)),
        dead_draw=clamp01(totals["dead_draw"] / max(3.4, size * 0.18)),
        upgrade_tax=clamp01(totals["upgrade_tax"] / max(3.0, size * 0.15)),
        average_cost=sum(costs) / max(1, len(costs)),
        starter_density=sum(1 for flag in starter_flags if flag) / size,
        status_density=sum(1 for flag in status_flags if flag) / size,
        power_density=sum(1 for flag in power_flags if flag) / size,
        signal_strengths=signal_strengths,
    )


def build_run_context(run_state: RunState, deck: DeckMetrics) -> RunContext:
    hp_ratio = run_state.hp_ratio
    low_hp = clamp01((0.65 - hp_ratio) / 0.35)
    critical_hp = clamp01((0.35 - hp_ratio) / 0.20)
    early_factor = 1.0 if run_state.phase.value == "early" else 0.45 if run_state.phase.value == "mid" else 0.10
    late_factor = 0.10 if run_state.phase.value == "early" else 0.45 if run_state.phase.value == "mid" else 1.0

    upcoming = [node.lower() for node in run_state.upcoming_nodes[:5]]
    elite_pressure = clamp01(
        0.22 * sum(1 for node in upcoming if "elite" in node)
        + (0.12 if run_state.floor >= 6 and run_state.floor <= 20 else 0.0)
    )
    rest_buffer = 0.08 * sum(1 for node in upcoming if "rest" in node)
    potion_buffer = min(0.18, len(run_state.potions) * 0.07)
    boss_pressure = clamp01(
        (0.12 if run_state.act_boss else 0.0)
        + (0.10 if run_state.act_number >= 3 else 0.0)
        + (0.08 if run_state.floor >= 28 else 0.0)
    )
    avg_cost_pressure = clamp01((deck.average_cost - 1.25) / 0.75)
    deck_pressure = clamp01((deck.size - 18) / 10.0)
    status_pressure = clamp01(deck.status_density * 1.8)

    needs = {
        "frontload": clamp01(
            0.35
            + 0.22 * early_factor
            + 0.18 * elite_pressure
            + 0.14 * low_hp
            - 0.60 * deck.frontload
            - 0.08 * deck.consistency
        ),
        "block": clamp01(
            0.30
            + 0.28 * low_hp
            + 0.18 * elite_pressure
            + 0.06 * late_factor
            - 0.62 * deck.block
        ),
        "aoe": clamp01(
            0.22
            + 0.12 * early_factor
            + 0.08 * elite_pressure
            - 0.70 * deck.aoe
        ),
        "scaling": clamp01(
            0.16
            + 0.12 * late_factor
            + 0.26 * boss_pressure
            - 0.64 * deck.scaling
        ),
        "consistency": clamp01(
            0.26
            + 0.20 * deck_pressure
            + 0.20 * deck.dead_draw
            + 0.10 * deck.setup
            - 0.62 * deck.consistency
        ),
        "energy": clamp01(
            0.18
            + 0.18 * avg_cost_pressure
            + 0.08 * late_factor
            - 0.60 * deck.energy
        ),
        "cleanup": clamp01(
            0.10
            + 0.16 * status_pressure
            + 0.10 * deck.dead_draw
            - 0.58 * deck.cleanup
        ),
    }

    short_term_survival = soft_or(
        [
            needs["frontload"] * 0.75,
            needs["block"],
            needs["consistency"] * 0.55 * low_hp,
        ]
    )
    boss_plan_need = soft_or(
        [
            needs["scaling"],
            needs["energy"] * 0.70,
            needs["consistency"] * 0.60,
        ]
    )
    dilution_pressure = clamp01(
        0.18
        + 0.38 * deck_pressure
        + 0.22 * deck.dead_draw
        + 0.14 * deck.starter_density
        + 0.12 * deck.status_density
        - 0.06 * run_state.planned_removes
        - 0.04 * deck.cleanup
    )
    greed_tolerance = clamp01(
        0.70
        - 0.55 * critical_hp
        - 0.18 * elite_pressure
        + rest_buffer
        + potion_buffer
    )
    solvedness = soft_or(
        [
            deck.frontload * 0.35,
            deck.block * 0.25,
            deck.scaling * 0.25,
            deck.consistency * 0.20,
        ]
    )
    skip_score_norm = clamp01(
        0.40
        + 0.34 * dilution_pressure
        + 0.10 * deck.dead_draw
        + 0.08 * deck.upgrade_tax
        + 0.05 * solvedness
        - 0.18 * short_term_survival
        - 0.10 * boss_plan_need
    )
    skip_threshold = max(
        3.0,
        min(
            7.0,
            3.5 + 2.0 * dilution_pressure + 1.5 * (1.0 - run_state.input_confidence),
        ),
    )

    return RunContext(
        deck=deck,
        needs=needs,
        short_term_survival=short_term_survival,
        boss_plan_need=boss_plan_need,
        dilution_pressure=dilution_pressure,
        greed_tolerance=greed_tolerance,
        elite_pressure=elite_pressure,
        boss_pressure=boss_pressure,
        skip_score_norm=skip_score_norm,
        skip_threshold=skip_threshold,
        input_confidence=run_state.input_confidence,
    )


def motif_signal_support(key_tags: Iterable[str], deck: DeckMetrics) -> float:
    scores: list[float] = []
    for key_tag in key_tags:
        tag = key_tag.strip().lower()
        if not tag:
            continue
        if tag in deck.signal_strengths:
            scores.append(deck.signal_strengths[tag])
            continue
        if tag in {"sly", "discard"}:
            scores.append(max(deck.signal_strengths.get("discard", 0.0), deck.signal_strengths.get("sly", 0.0)))
        elif tag in {"orb", "focus", "dark", "frost"}:
            scores.append(
                max(
                    deck.signal_strengths.get(tag, 0.0),
                    deck.signal_strengths.get("orb", 0.0),
                )
            )
        elif tag in {"star", "forge", "colorless", "create"}:
            scores.append(
                max(
                    deck.signal_strengths.get(tag, 0.0),
                    deck.signal_strengths.get("star", 0.0),
                    deck.signal_strengths.get("forge", 0.0),
                    deck.signal_strengths.get("colorless", 0.0),
                )
            )
        elif tag in {"engine", "combo", "resource_loop", "hand_filtering", "needs_payoffs"}:
            scores.append(soft_or([deck.consistency * 0.7, deck.draw * 0.5, deck.energy * 0.4]))
        elif tag in {"deck_thinning", "status_synergy", "conversion"}:
            scores.append(soft_or([deck.cleanup * 0.8, deck.consistency * 0.3]))
        elif tag in {"board_presence", "attack_scaling", "tempo", "attack_volume"}:
            scores.append(soft_or([deck.frontload * 0.7, deck.scaling * 0.4]))
        elif tag in {"stable", "survivability", "control_friendly", "needs_survivability"}:
            scores.append(soft_or([deck.block * 0.8, deck.consistency * 0.3]))
        elif tag in {"scaling", "setup_required", "needs_setup"}:
            scores.append(soft_or([deck.scaling * 0.8, (1.0 - deck.dead_draw) * 0.3]))
        elif tag == "density_sensitive":
            scores.append(soft_or([deck.consistency * 0.5, (1.0 - deck.dead_draw) * 0.5]))
    return soft_or(scores)


def need_contributions(card_profile: CardProfile, run_context: RunContext) -> dict[str, float]:
    return {
        "frontload": run_context.needs["frontload"] * card_profile.frontload,
        "block": run_context.needs["block"] * card_profile.block,
        "aoe": run_context.needs["aoe"] * card_profile.aoe,
        "scaling": run_context.needs["scaling"] * card_profile.scaling,
        "consistency": run_context.needs["consistency"] * card_profile.consistency,
        "energy": run_context.needs["energy"] * card_profile.energy,
        "cleanup": run_context.needs["cleanup"] * card_profile.cleanup,
    }


def need_coverage(card_profile: CardProfile, run_context: RunContext) -> float:
    return soft_or(need_contributions(card_profile, run_context).values())


def top_need_hits(card_profile: CardProfile, run_context: RunContext, limit: int = 3) -> list[str]:
    ranked = sorted(
        need_contributions(card_profile, run_context).items(),
        key=lambda item: item[1],
        reverse=True,
    )
    result = []
    for key, value in ranked:
        if value < 0.10:
            continue
        result.append(_NEED_LABELS.get(key, key))
        if len(result) >= limit:
            break
    return result


def network_synergy(card_profile: CardProfile, run_context: RunContext) -> float:
    components = [
        card_profile.consistency * run_context.deck.scaling,
        card_profile.energy * run_context.deck.draw,
        card_profile.cleanup * max(run_context.deck.dead_draw, run_context.deck.setup),
        card_profile.block * run_context.deck.scaling,
        card_profile.frontload * run_context.deck.scaling * 0.75,
    ]
    if {"shiv", "poison", "discard", "doom", "ethereal", "soul", "star", "forge", "colorless"} & set(card_profile.signals):
        for signal in card_profile.signals:
            components.append(run_context.deck.signal_strengths.get(signal, 0.0) * 0.70)
    return soft_or(components)


def estimate_confidence(
    *,
    run_state: RunState,
    run_context: RunContext,
    matched_strength: float,
    need_cover: float,
    exact_match: bool,
    inferred_only: bool,
    has_community: bool,
) -> tuple[float, str]:
    coverage = 1.0 if exact_match else 0.78 if matched_strength >= 0.36 else 0.58 if need_cover >= 0.35 else 0.42
    motif_certainty = clamp01(0.30 + matched_strength * 0.90 + need_cover * 0.25)
    community_certainty = 0.72 if has_community else 0.45
    confidence = clamp01(
        0.35 * run_state.input_confidence
        + 0.25 * coverage
        + 0.25 * motif_certainty
        + 0.15 * community_certainty
    )
    if inferred_only:
        confidence = clamp01(confidence - 0.08)
    if run_context.skip_threshold > 5.5:
        confidence = clamp01(confidence - 0.03)

    if confidence >= 0.78:
        return confidence, "High confidence"
    if confidence >= 0.56:
        return confidence, "Medium confidence"
    return confidence, "Low confidence"


def community_prior_alpha(run_state: RunState, run_context: RunContext, has_community: bool) -> float:
    if not has_community:
        return 0.0

    alpha = 0.16
    if not run_state.patch_version:
        alpha *= 0.85
    if run_state.character in (Character.REGENT, Character.NECROBINDER):
        alpha *= 0.90
    if run_state.floor <= 5 or run_context.deck.size >= 28:
        alpha *= 0.85
    alpha *= 0.75 + 0.25 * run_state.input_confidence
    return clamp01(alpha / 0.20) * 0.20
