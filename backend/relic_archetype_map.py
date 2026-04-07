"""
backend/relic_archetype_map.py
STS2 Relic → Archetype synergy map (v2.0 — full STS2 relic list)

Format: relic_id (UPPERCASE_UNDERSCORE) → [(archetype_id, boost_score), ...]
  - Only includes relics with meaningful archetype synergy (boost ≥ 0.30)
  - Universal/gold/HP relics with no archetype preference are omitted
  - boost_score 0.0–1.0: synergy strength with that archetype

Archetypes in system:
  Ironclad  : ironclad_strength, ironclad_self_damage, ironclad_exhaust
  Silent    : silent_poison, silent_shiv, silent_sly_discard
  Defect    : defect_orb_focus, defect_dark_evoke, defect_zero_cost_cycle
  Necrobinder: necrobinder_osty_attack, necrobinder_doom_execute,
               necrobinder_soul_engine, necrobinder_ethereal_engine
  Regent    : regent_star_engine, regent_sovereign_blade_forge, regent_colorless_create
"""

from __future__ import annotations

# ---------------------------------------------------------------------------
# Ironclad
# ---------------------------------------------------------------------------

_IRONCLAD: dict[str, list[tuple[str, float]]] = {
    # --- Starter ---
    # Burning Blood: heal 6 HP after combat — self-damage buffer
    "BURNING_BLOOD": [("ironclad_self_damage", 0.72)],
    # Black Blood: heal 12 HP after combat — stronger self-damage buffer
    "BLACK_BLOOD": [("ironclad_self_damage", 0.84)],

    # --- Common ---
    # Vajra: start combat with 1 Strength — minor boost to strength builds
    "VAJRA": [("ironclad_strength", 0.50)],
    # Red Skull: ≤50% HP → +3 Strength — self-damage keeps HP low = constant bonus
    "RED_SKULL": [
        ("ironclad_self_damage", 0.90),
        ("ironclad_strength",    0.78),
    ],

    # --- Uncommon ---
    # Paper Phrog: Vulnerable = 75% more dmg (not 50%) — amplifies strength scaling
    "PAPER_PHROG": [
        ("ironclad_strength",    0.82),
        ("ironclad_self_damage", 0.36),
    ],
    # Self-Forming Clay: gain 3 Block next turn on HP loss — self-damage safety net
    "SELF_FORMING_CLAY": [("ironclad_self_damage", 0.92)],

    # --- Rare ---
    # Beating Remnant: cannot lose >20 HP/turn — critical for self-damage survival
    "BEATING_REMNANT": [("ironclad_self_damage", 0.40)],
    # Girya: gain Strength at rest sites (3x max) — core strength scaling
    "GIRYA": [("ironclad_strength", 0.70)],
    # Lizard Tail: die → heal to 50% (once) — self-damage safety net
    "LIZARD_TAIL": [("ironclad_self_damage", 0.30)],
    # Meat on the Bone: heal 12 HP at ≤50% HP end of combat — self-damage synergy
    "MEAT_ON_THE_BONE": [("ironclad_self_damage", 0.50)],
    # Shuriken: every 3 attacks → +1 Strength — attack-heavy / strength scaling
    "SHURIKEN": [("ironclad_strength", 0.55)],
    # Charon's Ashes: exhaust a card → 3 dmg ALL enemies — exhaust engine core relic
    "CHARONS_ASHES": [("ironclad_exhaust", 0.97)],
    # Demon Tongue: first HP loss on your turn → heal equal — self-damage reversal
    "DEMON_TONGUE": [("ironclad_self_damage", 0.99)],
    # Ruined Helmet: first Strength gain each combat is doubled — strength burst
    "RUINED_HELMET": [
        ("ironclad_strength",    0.98),
        ("ironclad_self_damage", 0.63),
    ],

    # --- Shop ---
    # Brimstone: +2 Strength per turn (enemies +1 Strength) — strength core
    "BRIMSTONE": [
        ("ironclad_strength",    0.96),
        ("ironclad_self_damage", 0.46),
    ],
    # Burning Sticks: exhaust a Skill → copy added to hand — exhaust cycle value
    "BURNING_STICKS": [("ironclad_exhaust", 0.65)],
    # Sling of Courage: +2 Strength at start of elite combats
    "SLING_OF_COURAGE": [("ironclad_strength", 0.45)],

    # --- Event ---
    # Sword of Jade: start each combat with 3 Strength — strength builds love this
    "SWORD_OF_JADE": [("ironclad_strength", 0.55)],
    # Forgotten Soul: exhaust a card → 1 dmg to random enemy
    "FORGOTTEN_SOUL": [("ironclad_exhaust", 0.50)],
    # Toasty Mittens (Ancient - Tezcatara): exhaust top card each turn + 1 Strength
    "TOASTY_MITTENS": [
        ("ironclad_strength",  0.60),
        ("ironclad_exhaust",   0.55),
    ],
    # Ember Tea: +2 Strength first 5 combats
    "EMBER_TEA": [("ironclad_strength", 0.45)],
}

# ---------------------------------------------------------------------------
# Silent
# ---------------------------------------------------------------------------

_SILENT: dict[str, list[tuple[str, float]]] = {
    # --- Common ---
    # Snecko Skull: applying Poison gives +1 extra — core poison relic
    "SNECKO_SKULL": [("silent_poison", 0.99)],

    # --- Uncommon ---
    # Twisted Funnel: start of combat → 4 Poison to ALL enemies — poison opening
    "TWISTED_FUNNEL": [("silent_poison", 0.95)],
    # Tingsha: discard during turn → 3 dmg per discarded card — discard synergy
    "TINGSHA": [("silent_sly_discard", 0.97)],
    # Tough Bandages: discard during turn → +3 Block — discard / defensive
    "TOUGH_BANDAGES": [("silent_sly_discard", 0.96)],

    # --- Rare ---
    # Paper Krane: Weak reduces enemy dmg 40% (not 25%) — poison applies Weak too
    "PAPER_KRANE": [
        ("silent_poison",       0.45),
        ("silent_sly_discard",  0.35),
    ],
    # Kunai: every 3 attacks → +1 Dexterity — Shivs = attacks = Dex stacking
    "KUNAI": [("silent_shiv", 0.45)],
    # Helical Dart: play a Shiv → +1 Dexterity this turn
    "HELICAL_DART": [("silent_shiv", 0.94)],
    # Gambling Chip: discard and redraw at combat start — cycle / discard synergy
    "GAMBLING_CHIP": [("silent_sly_discard", 0.40)],
    # Unsettling Lamp: first debuff each combat is doubled — doubles first Poison
    "UNSETTLING_LAMP": [("silent_poison", 0.50)],

    # --- Shop ---
    # Ninja Scroll: start of combat → 3 Shivs in Hand
    "NINJA_SCROLL": [("silent_shiv", 0.98)],
    # Unceasing Top: draw when hand empty — cycle / discard combo
    "UNCEASING_TOP": [("silent_sly_discard", 0.40)],
}

# ---------------------------------------------------------------------------
# Defect
# ---------------------------------------------------------------------------

_DEFECT: dict[str, list[tuple[str, float]]] = {
    # --- Starter ---
    # Cracked Core: Channel 1 Lightning at start — minimal orb boost
    "CRACKED_CORE": [("defect_orb_focus", 0.70)],
    # Infused Core: Channel 3 Lightning at start — strong orb opening
    "INFUSED_CORE": [
        ("defect_orb_focus",  0.92),
        ("defect_dark_evoke", 0.35),
    ],

    # --- Common ---
    # Data Disk: start combat with 1 Focus — Orb/Focus core relic
    "DATA_DISK": [
        ("defect_orb_focus",   0.99),
        ("defect_dark_evoke",  0.62),
    ],

    # --- Uncommon ---
    # Gold-Plated Cables: rightmost Orb passive triggers an extra time
    "GOLD_PLATED_CABLES": [
        ("defect_orb_focus",   0.93),
        ("defect_dark_evoke",  0.66),
    ],
    # Symbiotic Virus: start of combat → Channel 1 Dark — Dark/Evoke core
    "SYMBIOTIC_VIRUS": [
        ("defect_dark_evoke",  0.98),
        ("defect_orb_focus",   0.48),
    ],

    # --- Rare ---
    # Emotion Chip: lost HP last turn → trigger all Orb passives at turn start
    "EMOTION_CHIP": [
        ("defect_orb_focus",   0.90),
        ("defect_dark_evoke",  0.70),
    ],
    # Metronome: Channel 7 Orbs → deal 30 dmg ALL once per combat
    "METRONOME": [
        ("defect_orb_focus",   0.87),
        ("defect_dark_evoke",  0.58),
    ],
    # Power Cell: start of combat → add 2 zero-cost cards from draw pile to hand
    "POWER_CELL": [("defect_zero_cost_cycle", 0.99)],
    # Ice Cream: energy carries between turns — strong for zero-cost builds
    "ICE_CREAM": [("defect_zero_cost_cycle", 0.60)],
    # Screaming Flagon: empty hand at end of turn → 20 dmg ALL — hand-empty synergy
    "SCREAMING_FLAGON": [("defect_zero_cost_cycle", 0.45)],

    # --- Shop ---
    # Runic Capacitor: start with 3 extra Orb Slots
    "RUNIC_CAPACITOR": [
        ("defect_orb_focus",   0.96),
        ("defect_dark_evoke",  0.72),
    ],
    # Unceasing Top: draw when hand empty — zero-cost cycle empties hand
    "UNCEASING_TOP_DEFECT": [("defect_zero_cost_cycle", 0.45)],
}

# ---------------------------------------------------------------------------
# Necrobinder
# ---------------------------------------------------------------------------

_NECROBINDER: dict[str, list[tuple[str, float]]] = {
    # --- Starter ---
    # Bound Phylactery: Summon 1 at start of each turn
    "BOUND_PHYLACTERY": [("necrobinder_osty_attack", 0.86)],
    # Phylactery Unbound: Summon 5 at combat start, Summon 2 per turn
    "PHYLACTERY_UNBOUND": [
        ("necrobinder_osty_attack", 0.97),
        ("necrobinder_doom_execute", 0.40),
    ],

    # --- Common ---
    # Bone Flute: whenever Osty attacks → gain 2 Block
    "BONE_FLUTE": [("necrobinder_osty_attack", 0.95)],

    # --- Uncommon ---
    # Book Repair Knife: non-Minion enemy dies to Doom → heal 3 HP
    "BOOK_REPAIR_KNIFE": [("necrobinder_doom_execute", 0.98)],
    # Funerary Mask: start of combat → add 3 Souls to Draw Pile
    "FUNERARY_MASK": [("necrobinder_soul_engine", 0.99)],

    # --- Rare ---
    # Big Hat: start of combat → add 2 random Ethereal cards to Hand
    "BIG_HAT": [("necrobinder_ethereal_engine", 0.98)],
    # Bookmark: end of turn → lower cost of a random Retained card by 1
    "BOOKMARK": [
        ("necrobinder_ethereal_engine", 0.50),
        ("necrobinder_soul_engine",     0.40),
    ],
    # Ivory Tile: play a 3+ cost card → gain 1 Energy — high-cost synergy
    "IVORY_TILE": [("necrobinder_doom_execute", 0.50)],

    # --- Shop ---
    # Undying Sigil: enemies with Doom ≥ HP deal 50% less damage
    "UNDYING_SIGIL": [("necrobinder_doom_execute", 0.99)],
}

# ---------------------------------------------------------------------------
# Regent
# ---------------------------------------------------------------------------

_REGENT: dict[str, list[tuple[str, float]]] = {
    # --- Starter ---
    # Divine Right: start of combat gain 3 Stars
    "DIVINE_RIGHT": [
        ("regent_star_engine",           0.90),
        ("regent_sovereign_blade_forge", 0.42),
    ],
    # Divine Destiny: start of combat gain 6 Stars
    "DIVINE_DESTINY": [
        ("regent_star_engine",           0.98),
        ("regent_sovereign_blade_forge", 0.48),
    ],

    # --- Common ---
    # Fencing Manual: start of combat Forge 10 — Sovereign Blade core
    "FENCING_MANUAL": [("regent_sovereign_blade_forge", 0.99)],

    # --- Uncommon ---
    # Galactic Dust: every 10 Stars spent → 10 Block — star engine passive defense
    "GALACTIC_DUST": [("regent_star_engine", 0.96)],
    # Regalite: create a Colorless card → 2 Block — colorless engine defense
    "REGALITE": [("regent_colorless_create", 0.98)],

    # --- Rare ---
    # Lunar Pastry: end of turn gain 1 Star — steady star generation
    "LUNAR_PASTRY": [("regent_star_engine", 0.92)],
    # Mini Regent: first Star spend per turn → +1 Strength
    "MINI_REGENT": [
        ("regent_star_engine",           0.88),
        ("regent_sovereign_blade_forge", 0.62),
    ],
    # Orange Dough: start of combat → 2 random Colorless cards in Hand
    "ORANGE_DOUGH": [("regent_colorless_create", 0.85)],

    # --- Shop ---
    # Vitruvian Minion: Minion cards deal double dmg and gain double Block
    "VITRUVIAN_MINION": [("regent_sovereign_blade_forge", 0.80)],
    # Toolbox: choose 1 of 3 Colorless cards at combat start
    "TOOLBOX": [("regent_colorless_create", 0.75)],
    # Dingy Rug: Colorless cards can appear in card rewards
    "DINGY_RUG": [("regent_colorless_create", 0.60)],

    # --- Event ---
    # Vexing Puzzlebox: start of combat → 1 free random card in Hand
    "VEXING_PUZZLEBOX": [("regent_colorless_create", 0.55)],
}

# ---------------------------------------------------------------------------
# Universal relics with archetype synergy across characters
# (relics available to any character that boost specific play patterns)
# ---------------------------------------------------------------------------

_UNIVERSAL: dict[str, list[tuple[str, float]]] = {
    # Joss Paper: every 5 exhausts → draw 1 — exhaust engine draw
    "JOSS_PAPER": [("ironclad_exhaust", 0.60)],

    # Permafrost: first Power played each combat → 6 Block — power build synergy
    # (powers typically support scaling archetypes)
    # No specific archetype mapping — too universal.

    # Game Piece: play a Power → draw 1 card — power/draw synergy
    "GAME_PIECE": [
        ("defect_orb_focus",  0.30),
        ("ironclad_strength", 0.30),
    ],

    # Frozen Egg: add a Power → it's Upgraded — all power-scaling archetypes
    "FROZEN_EGG": [
        ("ironclad_strength",  0.45),
        ("defect_orb_focus",   0.45),
        ("silent_poison",      0.35),
        ("regent_star_engine", 0.35),
    ],

    # Mummified Hand: play a Power → random card in hand is free — power plays
    "MUMMIFIED_HAND": [
        ("ironclad_strength",  0.40),
        ("defect_orb_focus",   0.40),
    ],

    # Reptile Trinket: use a Potion → +3 Strength this turn
    "REPTILE_TRINKET": [("ironclad_strength", 0.50)],

    # Sparkling Rouge: turn 3 → +1 Str +1 Dex — minor scaling for any archetype
    "SPARKLING_ROUGE": [
        ("ironclad_strength",  0.35),
        ("silent_shiv",        0.30),
    ],

    # Rainbow Ring: play Attack+Skill+Power in a turn → +1 Str +1 Dex
    "RAINBOW_RING": [
        ("ironclad_strength",  0.40),
        ("silent_shiv",        0.35),
    ],

    # Ancient: Pael's Blood — draw 1 extra card per turn (strong for all cycle builds)
    "PAELS_BLOOD": [
        ("defect_zero_cost_cycle",      0.50),
        ("silent_sly_discard",          0.45),
        ("necrobinder_soul_engine",     0.40),
    ],

    # Ancient: Pael's Tears — unspent energy → +2 energy next turn
    "PAELS_TEARS": [
        ("defect_zero_cost_cycle", 0.55),
        ("regent_star_engine",     0.40),
    ],

    # Ancient: Pael's Flesh — extra energy from turn 3 onward
    "PAELS_FLESH": [
        ("defect_zero_cost_cycle", 0.50),
        ("regent_star_engine",     0.45),
    ],

    # Ancient: Philosopher's Stone — +1 Energy, enemies start with 1 Strength
    # Good for any deck that can end fights fast (strength / damage builds)
    "PHILOSOPHERS_STONE": [
        ("ironclad_strength",  0.55),
        ("silent_shiv",        0.35),
    ],

    # Ancient: Ectoplasm — no Gold, +1 Energy — any energy-hungry archetype
    "ECTOPLASM": [
        ("defect_zero_cost_cycle", 0.50),
        ("regent_star_engine",     0.40),
    ],

    # Ancient: Pumpkin Candle — extra energy until Act 3
    "PUMPKIN_CANDLE": [
        ("defect_zero_cost_cycle", 0.45),
        ("regent_star_engine",     0.40),
    ],

    # Ancient: Blood-Soaked Rose (Vakuu) — +1 Energy, Vakuu plays first turn
    "BLOOD_SOAKED_ROSE": [
        ("defect_zero_cost_cycle", 0.40),
    ],

    # Ancient: Whispering Earring (Vakuu) — +1 Energy, Vakuu plays first turn
    "WHISPERING_EARRING": [
        ("defect_zero_cost_cycle", 0.40),
    ],

    # Ancient: Blessed Antler (Nonupeipe) — +1 Energy, 3 Dazed each combat
    "BLESSED_ANTLER": [
        ("defect_zero_cost_cycle", 0.40),
        ("necrobinder_ethereal_engine", 0.35),  # Dazed are ethereal
    ],

    # Ancient: Prismatic Gem (Orobas) — +1 Energy, off-color cards in rewards
    "PRISMATIC_GEM": [
        ("defect_zero_cost_cycle", 0.45),
        ("regent_colorless_create", 0.40),
    ],

    # Ancient: Radiant Pearl (Orobas) — start combat with Luminesce (2 Energy token)
    "RADIANT_PEARL": [
        ("defect_zero_cost_cycle", 0.50),
        ("regent_star_engine",     0.40),
    ],

    # Spiked Gauntlets (Tanx) — +1 Energy, Powers cost 1 more
    "SPIKED_GAUNTLETS": [
        ("defect_zero_cost_cycle", 0.40),
        ("ironclad_exhaust",       0.30),
    ],

    # Crossbow (Tanx) — start of turn: free random Attack
    "CROSSBOW": [
        ("ironclad_strength",  0.40),
        ("silent_shiv",        0.35),
    ],

    # Toasty Mittens (Tezcatara) — exhaust top card + 1 Strength per turn
    "TOASTY_MITTENS_UNIVERSAL": [
        ("ironclad_exhaust",   0.55),
        ("ironclad_strength",  0.60),
    ],

    # Sai (Tanx) — start of turn gain 7 Block
    "SAI": [
        ("necrobinder_osty_attack", 0.35),  # Any block synergy
    ],

    # Mr. Struggles — deal damage equal to turn number to ALL enemies each turn
    "MR_STRUGGLES": [
        ("defect_orb_focus",   0.35),  # AoE scaling
    ],

    # Mercury Hourglass — 3 AoE damage per turn start
    "MERCURY_HOURGLASS": [
        ("silent_poison",      0.35),
    ],

    # Stone Calendar — turn 7: 52 AoE damage
    "STONE_CALENDAR": [
        ("defect_dark_evoke",  0.30),
    ],
}

# ---------------------------------------------------------------------------
# Merge into global map
# ---------------------------------------------------------------------------

RELIC_ARCHETYPE_MAP: dict[str, list[tuple[str, float]]] = {
    **_IRONCLAD,
    **_SILENT,
    **_DEFECT,
    **_NECROBINDER,
    **_REGENT,
    **_UNIVERSAL,
}
