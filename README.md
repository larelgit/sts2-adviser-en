# STS2 Adviser — Slay the Spire 2 Real-time Card Selection Adviser

<p align="center">
  <img src="assets/preview.png" width="220" alt="STS2 Adviser Preview"/>
</p>

STS2 Adviser uses **CDPE (Contextual Delta Pick Engine)** — a marginal value scoring system that evaluates cards based on what your deck actually needs, not just card rarity or archetype labels.

**Key Innovation:** Instead of asking "How good is this card?", the system asks **"How much better is picking this card than skipping?"**

## How It Works

```
┌─────────────────┐     ┌─────────────────┐     ┌─────────────────┐
│  Game Window    │────▶│   OCR + Logs    │────▶│  Deck Profile   │
│  (Screenshot)   │     │  (Card names,   │     │  (What you have)│
└─────────────────┘     │   deck, relics) │     └────────┬────────┘
                        └─────────────────┘              │
                                                         ▼
┌─────────────────┐     ┌─────────────────┐     ┌─────────────────┐
│  Final Verdict  │◀────│  Delta Scoring  │◀────│   Gap Analysis  │
│  "Pick Inflame" │     │  (Card vs Skip) │     │  (What you need)│
└─────────────────┘     └─────────────────┘     └─────────────────┘
```

### The CDPE Algorithm

1. **Deck Profile** — Analyzes your current deck capabilities:
   - Damage output (0.0 - 1.0)
   - Block capability
   - Card draw density
   - Scaling potential (strength, poison, orbs)
   - AoE coverage

2. **Gap Analysis** — Compares deck profile against act-specific targets:
   ```
   Act 1: {damage: 55%, block: 35%, scaling: 15%, draw: 20%, aoe: 10%}
   Act 2: {damage: 50%, block: 55%, scaling: 45%, draw: 40%, aoe: 55%}
   Act 3: {damage: 45%, block: 60%, scaling: 75%, draw: 50%, aoe: 30%}
   ```
   
   Gap = Target - Current (positive = need more, negative = surplus)

3. **Delta Scoring** — Each card is scored by how well it fills gaps:
   ```
   delta = Σ(card_contribution × gap × priority)
         - dilution_cost
         - surplus_penalty
   ```

4. **Skip Evaluation** — Skip is a real option:
   - Large deck → Skip is valuable (avoid dilution)
   - No gaps → Skip is safe
   - Critical gaps → Skip is costly

### Example Output

```
#1 INFLAME — Score: 45.2
   🟢 +32.5 Scaling. Fills critical deficit (target 45%, current 10%).
   🟢 +8.0 Strength synergy with existing cards.
   🔴 -5.3 Dilution. Deck size (18) above optimal.

#2 SHRUG IT OFF — Score: 28.1
   🟢 +18.2 Block. Improves deck coverage (need 25% more).
   🟢 Draw value. Large deck benefits from card draw.

#3 CLEAVE — Score: 12.4
   🔴 -8.5 AoE surplus. Already 15% over target.

📌 VERDICT: INFLAME
   Score: 45.2 (+30.1 vs Skip)
   Strong pick - fills: scaling
```

## Quick Start

### Method 1: Run EXE Directly (Recommended)

1. Go to [Releases](https://github.com/larelgit/sts2-adviser-en/releases) and download the latest ZIP.
2. Extract and double-click `sts2_adviser.exe`.

### Method 2: Run from Source

```bash
pip install -r requirements.txt
python main.py
```

## Features

### Automatic Mode
- Overlay window appears on top of the game
- Automatically captures card reward screens via OCR
- Displays scores and explanations without any input

### Manual Mode
- Click **◀** to open the card selection drawer
- Select up to 4 cards manually
- Click **⟳ Evaluate** to see scores

### Game Integration
- Reads game logs for character, floor, deck, relics
- Path auto-detection: `%AppData%\SlayTheSpire2\`, Steam paths
- Manual config: Run `python diagnose_save_path.py`

## Architecture

```
sts2-adviser/
├── backend/
│   ├── deck_profile.py       # Analyzes deck capabilities (0.0-1.0 metrics)
│   ├── gap_analysis.py       # Computes deficits vs act targets
│   ├── delta_scoring.py      # Marginal value scoring engine
│   ├── delta_reasons.py      # Generates human-readable explanations
│   ├── evaluator.py          # Orchestrates scoring pipeline
│   └── models.py             # Data models
│
├── data/
│   ├── card_functions.json   # Structured card data (142 cards)
│   │                         # {damage_flat, block_flat, draw, aoe, scaling_type, ...}
│   ├── cards.json            # Card metadata (cost, rarity, type)
│   └── card_library.json     # Community win/pick rates
│
├── frontend/
│   └── ui.py                 # PyQt6 overlay window
│
├── vision/
│   ├── vision_bridge.py      # Screenshot → OCR → Card names
│   ├── ocr_engine.py         # Windows WinRT OCR
│   └── card_normalizer.py    # Typo correction + fuzzy matching
│
└── scripts/
    └── game_watcher.py       # Monitors game logs
```

### Data Flow

```
Game Window ──PrintWindow──▶ OCR Engine ──▶ Card Names
     │                                           │
Game Logs ──GameWatcher──▶ RunState              │
     │                        │                  │
     ▼                        ▼                  ▼
card_functions.json ──▶ DeckProfile ──▶ GapVector ──▶ DeltaScore
                                                          │
                                                          ▼
                                               Verdict + Explanations
```

## Card Database Format

Cards are defined in `data/card_functions.json` with structured functions:

```json
{
  "INFLAME": {
    "id": "INFLAME",
    "character": "ironclad",
    "type": "power",
    "cost": 1,
    "functions": {
      "damage_flat": 0,
      "block_flat": 0,
      "draw": 0,
      "aoe": false,
      "scaling_type": "strength",
      "strength_gain": 2,
      "exhaust": false
    },
    "tags": ["power", "strength", "scaling"]
  }
}
```

Currently covers **142 cards**: Ironclad (33), Silent (39), Defect (24), Necrobinder (23), Regent (23).

## Scoring Details

### Gap-Based Scoring

| Metric | Normalization | Explanation |
|--------|---------------|-------------|
| Damage | 10 dmg = 1.0 | Raw damage contribution |
| Block | 8 block = 1.0 | Defensive capability |
| Draw | 2 draw = 1.0 | Card cycling |
| Scaling | 3 points = 1.0 | Strength/poison/focus |
| AoE | +0.5 flat | Multi-target bonus |

### Priority Modifiers

| Situation | Effect |
|-----------|--------|
| HP < 40% | Block priority ×1.5 |
| HP < 25% | Block priority ×1.8, Scaling ×0.5 |
| Elite ahead | Damage ×1.3, Block ×1.2 |
| Boss ahead | Scaling ×1.5, Draw ×1.2 |
| Deck > 15 | Draw priority increases |

### Dilution Cost

```
dilution_cost = 2.0 + (deck_size - 12) × 0.3  if deck_size > 12
              = 1.0                            if deck_size ≤ 12
```

Draw cards partially offset dilution (draw × 1.5 reduction).

### Skip Scoring

```
skip_score = 0
  + (deck_size - 12) × 3.0   if large deck
  + 5.0                      if no critical gaps
  - 8.0 × critical_gap_count if gaps exist
  + 3.0                      if draw_density > 0.6
```

## System Requirements

- **Windows 10 / 11** (requires Windows OCR)
- Python 3.10+
- `opencv-python` recommended for better OCR preprocessing

## Troubleshooting

**OCR issues?** Maximize game window. Run `python diagnose_ocr.py`.

**Wrong game path?** Run `python diagnose_save_path.py`.

**Backend failed?** Start manually: `python -m uvicorn backend.main:app --port 8001`

---

## Version History

### v2.1 (CDPE V2 - Marginal Delta Engine)

**Complete Rewrite** — Replaced archetype-based scoring with gap-based marginal evaluation.

**New Modules:**
- `deck_profile.py` — Deck capability analysis (damage, block, draw, scaling, aoe)
- `gap_analysis.py` — Act-specific target comparison, priority modifiers
- `delta_scoring.py` — Card contribution × gap × priority scoring
- `delta_reasons.py` — Human-readable gap-based explanations

**New Card Database:**
- `card_functions.json` — 142 cards with structured functions
- Format: `{damage_flat, block_flat, draw, aoe, scaling_type, strength_gain, ...}`
- Tags for semantic search: `["damage", "scaling", "exhaust", "cycle"]`

**Key Changes:**
- Scores represent "delta vs Skip", not absolute value
- Diminishing returns for surplus (adding 5th AoE card penalized)
- Dilution cost for large decks (+0.3 per card over 12)
- Priority modifiers for HP, elite, boss situations

### v2.0 (CDPE - Contextual Delta Pick Engine)

- Skip as full 4th option
- Soft archetype aggregation: `1 - ∏(1-w_i)`
- Extended value dimension (draw, exhaust, AoE bonuses)
- Necrobinder/Regent character support
- Raised inference cap 0.35 → 0.50

### v1.0 and earlier

See [CHANGELOG.md](CHANGELOG.md) for full history.

---

## License

[GNU GPL-3.0](LICENSE)

Copyright (c) 2026 Skyerolic
