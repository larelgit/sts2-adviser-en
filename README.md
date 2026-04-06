# STS2 Adviser — Slay the Spire 2 Real-time Card Selection Adviser

<p align="center">
  <img src="assets/preview.png" width="220" alt="STS2 Adviser Preview"/>
</p>

STS2 Adviser automatically takes a screenshot to recognize candidate cards when you enter the card selection screen. It reads game saves and logs to retrieve your current state (character, floor, deck, relics). The adviser evaluates cards across five dimensions: Archetype Synergy, Intrinsic Card Value, Current Phase (Floor) Adaptability, Archetype Completion Contribution, and Relic Synergy. Scores are then cross-validated with community win/pick rate data to provide a final recommendation score and reasoning, displayed as a top-level overlay window above the game.

## Quick Start

### Method 1: Run EXE Directly (Recommended, no Python required)

1. Go to [Releases](https://github.com/larelgit/sts2-adviser-en/releases) and download the latest ZIP version.
2. Extract the archive and double-click `sts2_adviser.exe`.

### Method 2: Run from Source

```bash
# Install dependencies
pip install -r requirements.txt

# Start the application
python main.py
```

## Usage Instructions

### Automatic Mode (OCR)

Upon launch, an overlay window will appear on top of the game. When you enter a card reward screen, the adviser will automatically take a screenshot, identify the three candidate cards, and display their scores without any manual input required.

**Resize Window**: You can freely resize the overlay by dragging the scaling handle in the bottom-right corner. Fonts will scale proportionally.

### Manual Selection Mode

Click the **◀** button on the right side of the overlay to open the card selection drawer. The window will expand to the right without taking up space on the main panel.

1. The top of the drawer lists all cards for the current character, grouped by Attack / Skill / Power.
2. Click on cards to select them (highlighted). You can select up to 4 candidate cards.
3. Click the **⟳ Evaluate** button at the bottom. The main panel will display the scoring results.
4. Click **▶** again to collapse the drawer.

### Configuring Game Log Path

`GameWatcher` reads game logs to obtain character, floor, and current deck information, making scoring much more accurate (especially for Phase Adaptability and Archetype Completion). If not configured, scoring relies solely on OCR results.

**Auto-search**: On startup, it automatically tries the following paths:
```
%AppData%\Roaming\SlayTheSpire2\
%AppData%\Local\SlayTheSpire2\saves\
C:\Program Files (x86)\Steam\steamapps\common\SlayTheSpire2\
```

**Manual Configuration**: If auto-search fails, enter the game log path in the settings interface (gear icon) or run the diagnostic tool:
```bash
python diagnose_save_path.py
```
Configuration is saved in `~/.sts2-adviser/config.json` and takes effect after restarting.

### Poor OCR Recognition?

**First Step: Maximize the game window.**
OCR relies on the resolution of the game window screenshot. The larger the window, the more accurate the recognition. Card text is easily misread in small windows.

Other measures:
- Ensure the game language is set to **Chinese** or **English**.
- Ensure the game window title contains `Slay the Spire 2`.
- Run the diagnostic tool on the card selection screen to view screenshots and OCR segmentation results:
  ```bash
  python diagnose_ocr.py
  ```

## Module Architecture

```text
sts2-adviser/
├── main.py                     # Main entry: starts both backend service and frontend overlay
│
├── backend/                    # FastAPI backend, handles evaluation logic
│   ├── main.py                 # HTTP/WebSocket server, coordinates GameWatcher and VisionBridge
│   ├── evaluator.py            # Evaluation engine: calls scoring dimensions, aggregates results/reasons
│   ├── scoring.py              # 5-dimension scoring algorithm + community data cross-validation
│   ├── archetypes.py           # Archetype library: definitions and core cards for each character
│   ├── archetype_inference.py  # Archetype inference: infers weights via keywords for unlisted cards
│   └── models.py               # Data models: Card / RunState / ScoreBreakdown, etc.
│
├── frontend/                   # PyQt6 overlay UI
│   ├── ui.py                   # Main window: overlay, dragging, score display, side drawer
│   ├── card_locale.py          # English Card ID → Chinese Name mapping
│   └── styles.qss              # Dark theme styling (Slay the Spire style)
│
├── vision/                     # Vision recognition module
│   ├── vision_bridge.py        # Dispatcher: polls screenshots, drives state machine, pushes results
│   ├── window_capture.py       # PrintWindow API capture (works even when window is covered)
│   ├── ocr_engine.py           # Windows WinRT OCR wrapper, includes OpenCV/PIL preprocessing
│   ├── screen_detector.py      # Detects current UI type (Card Reward / Shop / Other)
│   └── card_normalizer.py      # OCR post-processing: typo correction + fuzzy whitelist matching
│
├── scripts/
│   ├── game_watcher.py         # Monitors game logs to parse character/floor/deck/relics
│   └── config_manager.py       # Reads/writes ~/.sts2-adviser/config.json (paths, language, etc.)
│
├── data/
│   ├── cards.json              # Card DB: cost, rarity, type metadata
│   ├── card_library.json       # Community stats: win rate and pick rate for each card
│   ├── card_locale_zh.json     # Localization: English ID → Chinese Name
│   └── card_names_zh.json      # Chinese Card Name Index (for OCR matching)
│
├── diagnose_ocr.py             # Diagnostic tool: captures screen & outputs OCR segments
└── diagnose_save_path.py       # Diagnostic tool: auto-searches save/log paths
```

### Data Flow

```text
Game Window
  │
  ├─ PrintWindow Capture ──→ OCR Engine ──→ card_normalizer ──→ Card Name List
  │                                                                    │
  └─ Game Log Files ──→ GameWatcher ──→ RunState (Character/Deck)      │
                                              │                        │
                                              └────────────────────────┤
                                                                       ▼
                                                             evaluator (5-Dimension Scoring)
                                                                       │
                                                                       ▼
                                                             Frontend Overlay Display
```

## Scoring Algorithm

### 5-Dimension Weighted Scoring

Each candidate card is independently scored across the following five dimensions (normalized to 0~1), weighted, and then mapped to a 0~100 score:

> **Planned features (Not yet implemented)**: HP Dimension — adds weight to healing/defense cards when Current HP / Max HP ratio is low; Depth Dimension — dynamically adjusts card value based on deck thickness (harsher penalties for low-value cards in thick decks).

| Dimension | Weight | Scoring Logic |
|-----------|--------|---------------|
| Archetype Synergy | **35%** | V2: Soft-sum across all matched archetypes `1 - ∏(1-w_i)`. Multi-archetype cards get credit for versatility. |
| Intrinsic Value | **25%** | Rarity baseline + Cost efficiency + V2 bonuses: draw, exhaust, block, AoE, innate, retain. Penalties: ethereal, setup requirements. |
| Phase Adaptability | **15%** | Core/Enabler cards score higher late-game (Early 0.75 → Late 0.88); Transition cards are strong early (Early 0.85 → Late 0.15); Curse/Status is fixed at 0. |
| Completion Contribution| **15%** | V2: Top 3 motif deltas + motif unlock bonus. Single card contribution amplified. |
| Synergy Bonus | **10%** | V2: Increased from 5%. Relic/deck tag overlap + explicit archetype boosts. |

**Penalties** (deducted directly from the raw score, bypassing weights):
- **Curse/Status Penalty**: Curse cards −0.50, an extra −0.015 per additional curse in deck (max deduction 0.25).
- **Fat Deck Penalty**: For decks ≥ 20 cards, each additional low-value card −0.01 (max 0.15); Core/Enabler cards are exempt.

Final Tiers:

| Score | Recommendation Level |
|-------|----------------------|
| 80~100| Highly Recommended |
| 65~79 | Recommended |
| 50~64 | Optional / Viable |
| 30~49 | Situational / Careful |
| 0~29  | Skip |

### Community Data Cross-Validation

Community win/pick rates are normalized via sigmoid and mixed with the algorithm score (max weight 25%, with an additional 15% discount for patch lag):

| Comparison Result | Verdict | Processing Method |
|-------------------|---------|-------------------|
| delta ≤ 0.15 | AGREEMENT | Amplified up/down by 5%, 100% confidence. |
| 0.15 < delta ≤ 0.30 | SOFT_CONFLICT | Community weight reduced to 75%, compromised mix. |
| delta > 0.30 | CONFLICT | Community weight reduced to 50%, algorithm score takes priority. |
| No Community Data | — | Uses pure algorithm score. |

## System Requirements

- **Windows 10 / 11** (Relies on built-in Windows OCR)
- Python 3.10+
- `opencv-python` is highly recommended (provides better OCR preprocessing):
  ```bash
  pip install opencv-python
  ```

## Troubleshooting

**Cannot find game window**: Ensure the game window title contains `Slay the Spire 2`.

**Low OCR recognition rate**: Maximize the game window and try again, or run the diagnostic tool:
```bash
python diagnose_ocr.py
```

**Backend connection failed**: Start the backend manually:
```bash
python -m uvicorn backend.main:app --port 8001
```

---

## Version History

### v2.0 (CDPE - Contextual Delta Pick Engine)

**Major Architecture Upgrade** - Complete overhaul of the scoring system to evaluate **pick delta vs Skip** instead of absolute card scores.

**Core Changes:**
- **Skip as Full Option**: Skip is now evaluated as a 4th alternative. Recommendations show delta vs Skip: "Inflame (+25.3 vs Skip)" or "Skip is better (-5.2)".
- **Soft Archetype Aggregation**: Replaced `max(weights)` with soft-sum `1 - ∏(1-w_i)`. Cards good in multiple archetypes now score higher than single-archetype specialists.
- **Soft Role Thresholds**: Replaced hard cutoffs (0.85/0.60/0.30) with sigmoid transitions. Card at 0.29 vs 0.30 is no longer a cliff.
- **Extended Value Dimension**: Added bonuses for draw (+0.05/card), exhaust (+0.06), block (+0.006/point), AoE (+0.06), innate (+0.04), retain (+0.03). Added penalties for ethereal (-0.04) and setup requirements (-0.06).
- **Raised Inference Cap**: Inference weight cap raised from 0.35 to 0.50 (can now reach ENABLER level).
- **STS2 Character Support**: Removed Watcher profiles (not in STS2). Added Necrobinder (doom, soul_engine, ethereal, osty_attack) and Regent (star_engine, sovereign_blade, colorless) inference profiles.
- **Multi-Motif Completion**: Completion now considers top 3 motifs + motif unlock bonus, not just primary archetype.
- **Adjusted Weights**: Archetype 40%→35%, Synergy 5%→10% (more credit for relic/deck synergies).

**Skip Scoring Formula:**
```
skip_score = 50 (base)
  + dilution_resistance (up to +15 if deck > target)
  + consistency_protection (+5 if deck is good)
  - opportunity_cost (-8 in early game)
  - weakness_urgency (-12 if critical gaps)
  ± hp_modifier
```

**Files Changed:**
- `backend/scoring.py` - V2 scoring functions, skip scoring, soft roles
- `backend/evaluator.py` - Skip integration, V2 recommendations
- `backend/models.py` - Added SKIP CardRole
- `backend/archetype_inference.py` - Necrobinder/Regent profiles, raised cap

### v1.0 Test (Previous)
- **Standalone EXE Available**: First fully independent compiled version. No Python installation needed, extract and play.
- **GameWatcher Fixes**: Refactored `scripts/` into a standard Python package. Character/floor/deck states now load correctly in EXE mode, making Archetype Synergy scoring much more accurate.
- **Path Resolution Fix (PyInstaller 6.x)**: Used `sys._MEIPASS` to resolve `_internal/` directory. `data/`, `styles.qss`, and log paths now work correctly in EXE mode.
- **Process Termination Fix**: Switched to `os._exit()` to kill all processes immediately (including uvicorn backend threads) when closing the overlay.
- **UI Adjustments**: Increased initial window height by 1.5x (600×750), changed bottom-right resize handle to a visible gold style.

### v0.99
- **EXE Pack Path Compatibility**: Added `utils/paths.py` to unify root directory resolution (Dev vs PyInstaller frozen mode), fixing path dislocation issues for `data/`, `logs/`, and `styles.qss`.
- **Dependency Splitting**: Separated `requirements-prod.txt` (production only) and `requirements.txt` (dev+test) for cleaner installation.
- **build_exe.bat Upgrade**: Auto-creates/reuses `.venv`, installs only prod dependencies, displays final directory size, and detects UPX.
- **Spec Enhancements**: Added hidden imports (`rapidfuzz`, `psutil`, `mss`, `PIL`, `numpy`, `anyio._backends._trio`, `uvicorn.protocols.websockets.wsproto_impl`) for better compilation coverage.

### v0.95
- **Relic Synergy System**: Added `relic_archetype_map.py` for Relic→Archetype adaptability mapping; populated `data/relics.json`.
- **Community Data Completion**: `data/card_library.json` now covers win rates and pick rates for all available cards.
- **Build Infrastructure**: Added `build_exe.bat` and `sts2_adviser.spec` for one-click independent EXE packaging.

### v0.9
- **Code Quality / Cleanup**: Replaced `print` debug outputs with structured `logging` for better readability.
- **vision_bridge.py Optimization**: Removed redundant OCR methods, unified under `_extract_card_names_combined` dual-strategy (full-image clustering + region completion).
- **UI Fixes**:
  - Drawer expansion/collapse dynamically adjusts window width (prevents truncated card names).
  - Auto-adaptive window height on startup.
  - Card buttons changed to `Expanding` strategy for even width distribution.

### v0.8
- **Massive OCR Stability Improvements**:
  - Whitelist filtering replaces blacklist (fuzzy matching automatically filters out gibberish).
  - Narrowed down Y-axis range for full-image OCR to exclude card type label rows (Attack/Skill).
  - OCR concurrency locks added to prevent overlapping WinRT RecognizeAsync calls.
  - Graceful fallback to `ctypes` window enumeration if `win32gui` is unavailable.
- **OpenCV Preprocessing**: Uses INTER_CUBIC scaling + CLAHE + Gaussian Blur + Sharpening (if OpenCV is present). Fallback to PIL contrast enhancement.
- **Chinese OCR Misread Correction Dictionary Expanded**: Covers high-frequency gibberish for cards like "Combust", "Twin Strike", etc.
- **UI Refactoring**:
  - Global font size increased by 20%.
  - Candidate cards use vertical layout (Name → Chinese position → Score → Recommendation → Reason).
  - Manual selection moved to a side drawer (◀/▶ toggles), preventing it from taking up main panel space.
  - Colored recommendation reasoning (Green / Orange-Red).
  - Card selection panel changed to a 3-column grid, widened to 340px.

### v0.7
- Community data cross-validation layer: Algorithm score joint decision with community win/pick rates.
- Sigmoid normalization to convert community stats into 0~1 scores.
- AGREEMENT / SOFT_CONFLICT / CONFLICT confidence tiers.
- Added community data notes to recommendation reasoning.

### v0.6
- Archetype inference layer (`archetype_inference.py`): Automatically infers card archetype weights based on keywords/descriptions.
- Covers 11 archetype configurations across Ironclad / Silent / Defect / Watcher.
- Cards not in the exact card list can now receive inferred weights, expanding archetype coverage significantly.

### v0.5
- OCR rewrite: Dual-strategy (full-image clustering + region completion).
- Scoring engine refactor (archetype / value / phase / completion / synergy).
- Logging infrastructure: Auto-saving of scoring JSON logs + OCR snapshots.
- WebSocket stability fixes (UTF-8 encoding / asyncio blocking issues).

### v0.1 — v0.4
- Project initialization, basic FastAPI backend + PyQt6 overlay.
- Windows PrintWindow capture module.
- Windows OCR engine wrapper.
- GameWatcher (Log file monitor).

---

## License

This project is licensed under the [GNU GPL-3.0](LICENSE) License.

[![License: GPL v3](https://img.shields.io/badge/License-GPLv3-blue.svg)](https://www.gnu.org/licenses/gpl-3.0)

You are free to use, modify, and distribute this project, but derivative works must be open-sourced under the same license and cannot be used for closed-source commercial purposes.

Copyright (c) 2026 Skyerolic
