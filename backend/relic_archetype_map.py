"""
backend/relic_archetype_map.py
遗物→套路强关联映射表 (v0.9)

数据来源：用户提供的 sts2_character_relic_archetype_matches.json（score ≥ 0.85）

格式：relic_id (大写) → [(archetype_id, boost_score), ...]
  - relic_id 以存档文件中实际字段为准（大写，下划线分隔）
  - boost_score 范围 0.0~1.0，表示该遗物对套路的主观协同强度
  - 单个遗物可同时加成多个套路（如通用型遗物）
"""

from __future__ import annotations

# ---------------------------------------------------------------------------
# Ironclad
# ---------------------------------------------------------------------------

_IRONCLAD: dict[str, list[tuple[str, float]]] = {
    # Burning Blood: end of combat heal 6 HP — 对自伤套路有明显缓冲价值
    "BURNING_BLOOD": [("ironclad_self_damage", 0.72)],

    # Black Blood: end of combat heal 12 HP — 自伤套路更强版升级
    "BLACK_BLOOD": [("ironclad_self_damage", 0.84)],

    # Brimstone: start of turn +2 Strength (enemies +1) — 力量套路核心遗物
    "BRIMSTONE": [
        ("ironclad_strength",     0.96),
        ("ironclad_self_damage",  0.46),
    ],

    # Charon's Ashes: exhaust a card → deal 3 damage ALL — 排除套最强遗物之一
    "CHARONS_ASHES": [("ironclad_exhaust", 0.97)],

    # Demon Tongue: first HP loss per turn → heal equal amount — 自伤套路近乎免费的逆转
    "DEMON_TONGUE": [("ironclad_self_damage", 0.99)],

    # Paper Phrog: Vulnerable 75% instead of 50%
    "PAPER_PHROG": [
        ("ironclad_strength",    0.82),
        ("ironclad_self_damage", 0.36),
    ],

    # Red Skull: ≤50% HP → +3 Strength — 自伤套路常驻低血，收益极高
    "RED_SKULL": [
        ("ironclad_self_damage", 0.90),
        ("ironclad_strength",    0.78),
    ],

    # Ruined Helmet: first Strength gain per combat → double — 力量套路爆发加速器
    "RUINED_HELMET": [
        ("ironclad_strength",    0.98),
        ("ironclad_self_damage", 0.63),
    ],

    # Self-Forming Clay: lose HP → gain 3 Block next turn — 自伤套路防御回收
    "SELF_FORMING_CLAY": [("ironclad_self_damage", 0.92)],
}

# ---------------------------------------------------------------------------
# Silent
# ---------------------------------------------------------------------------

_SILENT: dict[str, list[tuple[str, float]]] = {
    # Snecko Skull: apply Poison → +1 extra — 毒套最强遗物之一
    "SNECKO_SKULL": [("silent_poison", 0.99)],

    # Twisted Funnel: start of combat → 4 Poison to ALL enemies
    "TWISTED_FUNNEL": [("silent_poison", 0.95)],

    # Ninja Scroll: start of combat → 3 Shivs in Hand
    "NINJA_SCROLL": [("silent_shiv", 0.98)],

    # Helical Dart: play a Shiv → +1 Dexterity this turn
    "HELICAL_DART": [("silent_shiv", 0.94)],

    # Tingsha: discard card during turn → deal 3 dmg per card discarded
    "TINGSHA": [("silent_sly_discard", 0.97)],

    # Tough Bandages: discard card during turn → gain 3 Block
    "TOUGH_BANDAGES": [("silent_sly_discard", 0.96)],
}

# ---------------------------------------------------------------------------
# Defect
# ---------------------------------------------------------------------------

_DEFECT: dict[str, list[tuple[str, float]]] = {
    # Infused Core: start of combat → Channel 3 Lightning
    "INFUSED_CORE": [("defect_orb_focus", 0.92)],

    # Data Disk: start each combat with 1 Focus — Orb/Focus套核心遗物
    "DATA_DISK": [
        ("defect_orb_focus",   0.99),
        ("defect_dark_evoke",  0.62),
    ],

    # Gold-Plated Cables: rightmost Orb triggers passive an extra time
    "GOLD_PLATED_CABLES": [
        ("defect_orb_focus",   0.93),
        ("defect_dark_evoke",  0.66),
    ],

    # Symbiotic Virus: start of combat → Channel 1 Dark — Dark套完美起点
    "SYMBIOTIC_VIRUS": [
        ("defect_dark_evoke",  0.98),
        ("defect_orb_focus",   0.48),
    ],

    # Emotion Chip: lost HP last turn → trigger all Orb passives at start
    "EMOTION_CHIP": [
        ("defect_orb_focus",   0.90),
        ("defect_dark_evoke",  0.70),
    ],

    # Metronome: Channel 7 Orbs → deal 30 dmg ALL (once per combat)
    "METRONOME": [
        ("defect_orb_focus",   0.87),
        ("defect_dark_evoke",  0.58),
    ],

    # Power Cell: start of combat → add 2 zero-cost cards from Draw Pile to Hand
    "POWER_CELL": [("defect_zero_cost_cycle", 0.99)],

    # Runic Capacitor: start with 3 extra Orb Slots
    "RUNIC_CAPACITOR": [
        ("defect_orb_focus",   0.96),
        ("defect_dark_evoke",  0.72),
    ],
}

# ---------------------------------------------------------------------------
# Necrobinder
# ---------------------------------------------------------------------------

_NECROBINDER: dict[str, list[tuple[str, float]]] = {
    # Phylactery Unbound: start of combat Summon 5 + start of turn Summon 2
    "PHYLACTERY_UNBOUND": [
        ("necrobinder_osty_attack", 0.97),
        ("necrobinder_doom_execute", 0.40),
    ],

    # Bone Flute: whenever Osty attacks, gain 2 Block
    "BONE_FLUTE": [("necrobinder_osty_attack", 0.95)],

    # Book Repair Knife: non-Minion enemy dies to Doom → heal 3 HP
    "BOOK_REPAIR_KNIFE": [("necrobinder_doom_execute", 0.98)],

    # Funerary Mask: start of combat → add 3 Souls to Draw Pile
    "FUNERARY_MASK": [("necrobinder_soul_engine", 0.99)],

    # Big Hat: start of combat → add 2 random Ethereal cards to Hand
    "BIG_HAT": [("necrobinder_ethereal_engine", 0.98)],

    # Undying Sigil: enemies with Doom ≥ HP deal 50% less damage
    "UNDYING_SIGIL": [("necrobinder_doom_execute", 0.99)],

    # Bound Phylactery: start of turn Summon 1
    "BOUND_PHYLACTERY": [("necrobinder_osty_attack", 0.86)],
}

# ---------------------------------------------------------------------------
# Regent
# ---------------------------------------------------------------------------

_REGENT: dict[str, list[tuple[str, float]]] = {
    # Divine Destiny: start of combat gain 6 Stars — 星引擎完美起点
    "DIVINE_DESTINY": [
        ("regent_star_engine",              0.98),
        ("regent_sovereign_blade_forge",    0.48),
    ],

    # Fencing Manual: start of combat Forge 10 — Sovereign Blade套核心
    "FENCING_MANUAL": [("regent_sovereign_blade_forge", 0.99)],

    # Galactic Dust: every 10 Stars spent → gain 10 Block
    "GALACTIC_DUST": [("regent_star_engine", 0.96)],

    # Regalite: create a Colorless card → gain 2 Block
    "REGALITE": [("regent_colorless_create", 0.98)],

    # Lunar Pastry: end of turn gain 1 Star
    "LUNAR_PASTRY": [("regent_star_engine", 0.92)],

    # Mini Regent: first time spend Stars each turn → gain 1 Strength
    "MINI_REGENT": [
        ("regent_star_engine",           0.88),
        ("regent_sovereign_blade_forge", 0.62),
    ],
}

# ---------------------------------------------------------------------------
# 合并为全局映射表
# ---------------------------------------------------------------------------

RELIC_ARCHETYPE_MAP: dict[str, list[tuple[str, float]]] = {
    **_IRONCLAD,
    **_SILENT,
    **_DEFECT,
    **_NECROBINDER,
    **_REGENT,
}
