"""
frontend/ui.py
PyQt6 浮窗主界面（CardAdviserWindow）

特性：
  - 永远置顶（WindowStaysOnTopHint）
  - 无边框 + 半透明背景（适合游戏覆盖）
  - 可拖拽移动
  - 显示评估结果卡片列表
  - 通过 HTTP 调用后端 /api/evaluate

布局：
  ┌──────────────────────────┐
  │  [STS2 Adviser]   [×]    │  ← 标题栏（可拖拽）
  ├──────────────────────────┤
  │  [刷新] [状态指示]        │  ← 工具栏
  ├──────────────────────────┤
  │  卡名        分数  推荐   │
  │  ─────────────────────── │
  │  Catalyst     82  强烈推荐│
  │  Ninjutsu     65  推荐    │
  │  Reflex       40  可选    │
  └──────────────────────────┘
"""

import logging
import sys
import json
import requests
import subprocess
import os
import websocket
import threading
import time

from utils.paths import get_app_root

log = logging.getLogger(__name__)

from PyQt6.QtCore import (
    Qt, QPoint, QThread, pyqtSignal, QTimer,
)
from PyQt6.QtGui import QFont, QColor, QPixmap
from PyQt6.QtWidgets import (
    QApplication, QWidget, QVBoxLayout, QHBoxLayout,
    QLabel, QPushButton, QScrollArea, QFrame,
    QSizePolicy, QDialog, QLineEdit, QFileDialog, QGroupBox,
    QGridLayout, QComboBox, QSizeGrip,
)

_port = os.environ.get("STS2_BACKEND_PORT", "8000")
BACKEND_URL = f"http://127.0.0.1:{_port}"


# ---------------------------------------------------------------------------
# 后台 HTTP 请求线程（避免阻塞 UI）
# ---------------------------------------------------------------------------

class EvaluateWorker(QThread):
    """
    在独立线程中调用后端 /api/evaluate，
    完成后通过信号返回结果。
    """
    result_ready = pyqtSignal(dict)
    error_occurred = pyqtSignal(str)

    def __init__(self, run_state: dict) -> None:
        super().__init__()
        self.run_state = run_state

    def run(self) -> None:
        try:
            resp = requests.post(
                f"{BACKEND_URL}/api/evaluate",
                json={"run_state": self.run_state},
                timeout=5,
            )
            resp.raise_for_status()
            self.result_ready.emit(resp.json())
        except requests.exceptions.ConnectionError:
            self.error_occurred.emit("Cannot connect to backend service (please start main.py first)")
        except requests.exceptions.Timeout:
            self.error_occurred.emit("Request timed out")
        except Exception as exc:
            self.error_occurred.emit(str(exc))


class _OcrSnapshotWorker(QThread):
    """
    手动触发一次 OCR 截图识别。
    直接在前端进程调用 vision 模块（不走后端 HTTP），
    结果通过 result_ready 信号返回。
    """
    result_ready = pyqtSignal(dict)

    def __init__(self, backend_url: str) -> None:
        super().__init__()
        self._backend_url = backend_url

    def run(self) -> None:
        try:
            from vision.window_capture import WindowCapture
            from vision.screen_detector import ScreenDetector, ScreenType
            from vision.card_normalizer import get_card_normalizer
            import datetime

            capture = WindowCapture()
            if capture.find_window() is None:
                self.result_ready.emit({
                    "screen_type": "unknown",
                    "error": "STS2 window not found",
                })
                return

            screenshot = capture.capture()
            if screenshot is None:
                self.result_ready.emit({
                    "screen_type": "unknown",
                    "error": "Screenshot failed",
                })
                return

            # 界面检测（单帧，不投票）
            detector = ScreenDetector(vote_frames=1)
            det = detector.detect(screenshot)

            if det.screen_type == ScreenType.CARD_REWARD:
                # 用比例坐标裁剪三个卡名区域分别 OCR
                from vision.vision_bridge import VisionBridge
                from vision.ocr_engine import get_ocr_engine
                normalizer = get_card_normalizer()

                ocr_engine = get_ocr_engine()
                title_y = VisionBridge._find_title_y(det.ocr_result)
                ocr_texts = VisionBridge._extract_card_names_combined(
                    screenshot, ocr_engine, det.ocr_result, title_y
                )
                norm = normalizer.normalize(ocr_texts)
                card_ids = [m.card_id if m else None for m in norm.cards]
                card_names = [m.matched_name if m else "" for m in norm.cards]
                confidences = [m.confidence if m else 0.0 for m in norm.cards]

                self.result_ready.emit({
                    "source": "vision_snapshot",
                    "screen_type": "card_reward",
                    "card_choices": [c for c in card_ids if c],
                    "card_names": card_names,
                    "confidences": confidences,
                    "ocr_texts": ocr_texts,
                    "all_reliable": norm.all_reliable,
                    "timestamp": datetime.datetime.utcnow().isoformat() + "Z",
                })

            elif det.screen_type == ScreenType.SHOP:
                self.result_ready.emit({
                    "source": "vision_snapshot",
                    "screen_type": "shop",
                    "matched_keywords": det.matched_keywords,
                })
            else:
                self.result_ready.emit({
                    "source": "vision_snapshot",
                    "screen_type": det.screen_type.value,
                    "ocr_text": det.ocr_text[:200] if det.ocr_text else "",
                })

        except Exception as e:
            log.error(f"OCR 截图识别失败: {e}")
            self.result_ready.emit({
                "screen_type": "unknown",
                "error": str(e),
            })


class CardsFetchWorker(QThread):
    """在独立线程中拉取指定角色的卡牌列表（含无色卡）"""
    cards_ready = pyqtSignal(list)
    error_occurred = pyqtSignal(str)

    def __init__(self, character: str) -> None:
        super().__init__()
        self.character = character

    def run(self) -> None:
        try:
            char_resp = requests.get(
                f"{BACKEND_URL}/api/cards",
                params={"character": self.character},
                timeout=5,
            )
            char_resp.raise_for_status()
            char_cards = char_resp.json().get("cards", [])

            # 同时拉取无色卡牌
            colorless_resp = requests.get(
                f"{BACKEND_URL}/api/cards",
                params={"character": "colorless"},
                timeout=5,
            )
            colorless_cards = []
            if colorless_resp.status_code == 200:
                colorless_cards = colorless_resp.json().get("cards", [])

            self.cards_ready.emit(char_cards + colorless_cards)
        except requests.exceptions.ConnectionError:
            self.error_occurred.emit("Cannot connect to backend")
        except Exception as exc:
            self.error_occurred.emit(str(exc))


# ---------------------------------------------------------------------------
# 卡牌选择器组件
# ---------------------------------------------------------------------------

_RARITY_CHIP_STYLE = {
    "common": {
        "normal":   "background:rgba(35,28,18,0.8);border:1px solid #3A2E1E;border-radius:3px;color:#9A8A6A;font-size:13px;padding:3px 6px;text-align:left;",
        "selected": "background:rgba(50,80,30,0.85);border:1px solid #5A8A2E;border-radius:3px;color:#A8D870;font-size:13px;padding:3px 6px;text-align:left;",
    },
    "uncommon": {
        "normal":   "background:rgba(20,35,50,0.8);border:1px solid #2E5A8A;border-radius:3px;color:#64B5F6;font-size:13px;padding:3px 6px;text-align:left;",
        "selected": "background:rgba(20,55,90,0.9);border:1px solid #4A8ABA;border-radius:3px;color:#90CAF9;font-size:13px;padding:3px 6px;text-align:left;",
    },
    "rare": {
        "normal":   "background:rgba(50,30,10,0.8);border:1px solid #8A5A1E;border-radius:3px;color:#FFD54F;font-size:13px;padding:3px 6px;text-align:left;",
        "selected": "background:rgba(80,50,10,0.9);border:1px solid #C8901E;border-radius:3px;color:#FFE082;font-size:13px;padding:3px 6px;text-align:left;",
    },
    "basic": {
        "normal":   "background:rgba(30,30,30,0.8);border:1px solid #444;border-radius:3px;color:#888;font-size:13px;padding:3px 6px;text-align:left;",
        "selected": "background:rgba(50,50,50,0.9);border:1px solid #666;border-radius:3px;color:#bbb;font-size:13px;padding:3px 6px;text-align:left;",
    },
}


class CardChipButton(QPushButton):
    """单张卡牌的可切换按钮（颜色按稀有度区分）"""
    toggled_card = pyqtSignal(dict, bool)  # (card, is_selected)

    def __init__(self, card: dict, display_name: str | None = None, parent=None) -> None:
        super().__init__(parent)
        self._card = card
        self._selected = False
        self._display_name = display_name or card.get("name", "")
        rarity_raw = card.get("rarity", "common").lower()
        self._rarity = rarity_raw if rarity_raw in _RARITY_CHIP_STYLE else "common"

        cost = card.get("cost", 0)
        cost_str = "X" if cost == -1 else str(cost)
        self.setText(f"[{cost_str}] {self._display_name}")
        self.setObjectName("CardChip")
        self._apply_style()
        self.setMinimumWidth(100)
        self.setSizePolicy(QSizePolicy.Policy.Expanding, QSizePolicy.Policy.Preferred)
        self.clicked.connect(self._on_click)

    def _apply_style(self) -> None:
        key = "selected" if self._selected else "normal"
        self.setStyleSheet(_RARITY_CHIP_STYLE[self._rarity][key])

    @property
    def card(self) -> dict:
        return self._card

    def is_selected(self) -> bool:
        return self._selected

    def set_selected(self, value: bool, emit: bool = True) -> None:
        self._selected = value
        self._apply_style()
        if emit:
            self.toggled_card.emit(self._card, value)

    def _on_click(self) -> None:
        self.set_selected(not self._selected)


class CardPickerPanel(QScrollArea):
    """按费用+类型分组显示的卡牌选择面板"""
    selection_changed = pyqtSignal(list, list)  # (已选卡列表, 显示名列表)

    _TYPE_ORDER = ["attack", "skill", "power"]
    _TYPE_LABELS_ZH = {"attack": "攻击", "skill": "技能", "power": "能力", "colorless": "无色"}
    _TYPE_LABELS_EN = {"attack": "Attack", "skill": "Skill", "power": "Power", "colorless": "Colorless"}

    def __init__(self, parent=None) -> None:
        super().__init__(parent)
        self.setObjectName("CardPickerScroll")
        self.setWidgetResizable(True)
        self.setHorizontalScrollBarPolicy(Qt.ScrollBarPolicy.ScrollBarAlwaysOff)
        self.setMinimumHeight(160)
        self.setSizePolicy(QSizePolicy.Policy.Expanding, QSizePolicy.Policy.Expanding)

        self._content = QWidget()
        self._content.setSizePolicy(
            QSizePolicy.Policy.Expanding, QSizePolicy.Policy.Preferred
        )
        self._layout = QVBoxLayout(self._content)
        self._layout.setContentsMargins(4, 4, 4, 4)
        self._layout.setSpacing(2)
        self._layout.addStretch()
        self.setWidget(self._content)

        self._chips: list[CardChipButton] = []
        self._language: str = "en"

    def set_language(self, lang: str) -> None:
        self._language = lang

    def populate(self, cards: list[dict]) -> None:
        """按类型分组、按费用排序填充卡牌"""
        self.clear_cards()

        # 加载中文名映射（仅中文模式）
        locale_map: dict[str, str] = {}
        if self._language == "zh":
            try:
                try:
                    from frontend.card_locale import get_card_locale
                except ImportError:
                    from card_locale import get_card_locale
                lc = get_card_locale()
                locale_map = {cid: lc.get_chinese_name(cid) or "" for cid in [c.get("id", "") for c in cards]}
            except Exception:
                pass

        type_labels = self._TYPE_LABELS_ZH if self._language == "zh" else self._TYPE_LABELS_EN

        # 按类型分组
        groups: dict[str, list[dict]] = {t: [] for t in self._TYPE_ORDER}
        for card in cards:
            ct = card.get("card_type", "").lower()
            if ct in groups:
                groups[ct].append(card)

        # 每组按费用排序（X费排最后）
        def cost_key(c):
            cost = c.get("cost", 0)
            return 99 if cost == -1 else cost

        # 无色卡牌单独一组（character == colorless，不按 type 拆分）
        colorless_cards = sorted(
            [c for c in cards if c.get("character", "").lower() == "colorless"],
            key=cost_key
        )

        cols = 3
        for type_key in self._TYPE_ORDER:
            group = sorted(groups[type_key], key=cost_key)
            if not group:
                continue

            # 章节标题
            header = QLabel(type_labels.get(type_key, type_key))
            header.setObjectName("CardPickerSectionHeader")
            self._layout.insertWidget(self._layout.count() - 1, header)

            # 卡片网格
            grid = QGridLayout()
            grid.setSpacing(3)
            for i, card in enumerate(group):
                card_id = card.get("id", "")
                display = locale_map.get(card_id) or card.get("name", "") if self._language == "zh" else card.get("name", "")
                chip = CardChipButton(card, display_name=display)
                chip.toggled_card.connect(self._on_chip_toggled)
                self._chips.append(chip)
                grid.addWidget(chip, i // cols, i % cols)

            grid_container = QWidget()
            grid_container.setLayout(grid)
            self._layout.insertWidget(self._layout.count() - 1, grid_container)

        # 无色卡牌分组
        if colorless_cards:
            header = QLabel(type_labels.get("colorless", "Colorless"))
            header.setObjectName("CardPickerSectionHeader")
            self._layout.insertWidget(self._layout.count() - 1, header)

            grid = QGridLayout()
            grid.setSpacing(3)
            for i, card in enumerate(colorless_cards):
                card_id = card.get("id", "")
                display = locale_map.get(card_id) or card.get("name", "") if self._language == "zh" else card.get("name", "")
                chip = CardChipButton(card, display_name=display)
                chip.toggled_card.connect(self._on_chip_toggled)
                self._chips.append(chip)
                grid.addWidget(chip, i // cols, i % cols)

            grid_container = QWidget()
            grid_container.setLayout(grid)
            self._layout.insertWidget(self._layout.count() - 1, grid_container)

    def clear_cards(self) -> None:
        """清除所有卡片和标题"""
        while self._layout.count() > 1:
            item = self._layout.takeAt(0)
            if item.widget():
                item.widget().deleteLater()
        self._chips.clear()

    def clear_selection(self) -> None:
        """取消所有选中（不触发信号）"""
        for chip in self._chips:
            chip.set_selected(False, emit=False)

    def selected_cards(self) -> list[dict]:
        return [chip.card for chip in self._chips if chip.is_selected()]

    def selected_display_names(self) -> list[str]:
        return [chip._display_name for chip in self._chips if chip.is_selected()]

    def _on_chip_toggled(self, card: dict, selected: bool) -> None:
        self.selection_changed.emit(self.selected_cards(), self.selected_display_names())


class SelectionTrayWidget(QWidget):
    """显示已选卡牌的托盘 + 评估按钮"""
    evaluate_requested = pyqtSignal(list)

    def __init__(self, parent=None) -> None:
        super().__init__(parent)
        self.setObjectName("SelectionTray")
        self._selected_cards: list[dict] = []

        layout = QHBoxLayout(self)
        layout.setContentsMargins(6, 4, 6, 4)
        layout.setSpacing(6)

        self._prefix_label = QLabel("Candidates:")
        self._prefix_label.setStyleSheet("color: #888; font-size: 12pt;")
        layout.addWidget(self._prefix_label)

        # 动态卡名区域
        self._chips_widget = QWidget()
        self._chips_layout = QHBoxLayout(self._chips_widget)
        self._chips_layout.setContentsMargins(0, 0, 0, 0)
        self._chips_layout.setSpacing(4)
        layout.addWidget(self._chips_widget, 1)

        self._count_label = QLabel("0/4")
        self._count_label.setStyleSheet("color: #666; font-size: 12pt;")
        layout.addWidget(self._count_label)

        self._evaluate_btn = QPushButton("⟳ Evaluate")
        self._evaluate_btn.setObjectName("EvaluateButton")
        self._evaluate_btn.setEnabled(False)
        self._evaluate_btn.clicked.connect(
            lambda: self.evaluate_requested.emit(self._selected_cards)
        )
        layout.addWidget(self._evaluate_btn)

    def update_selection(self, cards: list[dict], display_names: list[str] | None = None) -> None:
        self._selected_cards = cards

        # 清除旧的卡名标签
        while self._chips_layout.count():
            item = self._chips_layout.takeAt(0)
            if item.widget():
                item.widget().deleteLater()

        names = display_names if display_names else [c.get("name", "?") for c in cards]
        for name in names:
            if len(name) > 12:
                name = name[:11] + "…"
            lbl = QLabel(name)
            lbl.setObjectName("TrayCardLabel")
            lbl.setStyleSheet(
                "color: #A8D870; background: rgba(60,90,40,0.7); "
                "border: 1px solid #6A9A3E; border-radius: 3px; "
                "padding: 1px 5px; font-size: 12pt;"
            )
            self._chips_layout.addWidget(lbl)

        self._count_label.setText(f"{len(cards)}/4")
        self._evaluate_btn.setEnabled(len(cards) >= 1)


# ---------------------------------------------------------------------------
# 游戏状态实时监视 WebSocket 线程
# ---------------------------------------------------------------------------

class GameStateWatcher(QThread):
    """
    连接到后端 WebSocket，接收实时游戏状态更新

    信号：
    - game_state_updated: 游戏状态更新
    - connection_status: 连接状态变化
    - log_status_updated: 日志监视状态更新
    """
    game_state_updated = pyqtSignal(dict)
    connection_status = pyqtSignal(str, bool)  # (状态消息, 是否连接)
    log_status_updated = pyqtSignal(dict)  # 日志状态
    vision_state_updated = pyqtSignal(dict)  # OCR 识别状态

    def __init__(self, backend_url: str) -> None:
        super().__init__()
        self.backend_url = backend_url.replace("http://", "ws://").replace("https://", "wss://")
        self.ws = None
        self.is_running = False
        self.current_state = {}

    def run(self) -> None:
        """连接 WebSocket 并监听游戏状态更新"""
        ws_url = f"{self.backend_url}/ws/game-state"
        log.info(f"连接到 WebSocket: {ws_url}")
        self.is_running = True

        while self.is_running:
            try:
                import websocket as ws_lib
                self.ws = ws_lib.WebSocketApp(
                    ws_url,
                    on_message=self.on_message,
                    on_error=self.on_error,
                    on_close=self.on_close,
                    on_open=self.on_open,
                )

                self.ws.run_forever()

            except Exception as e:
                log.error(f"WebSocket 连接失败: {e}")
                self.connection_status.emit(f"Connection failed: {e}", False)

                # 重试连接
                if self.is_running:
                    time.sleep(3)

    def on_open(self, ws):
        """WebSocket 连接打开"""
        log.info("✓ WebSocket 已连接")
        self.connection_status.emit("Game monitor connected", True)

    def on_message(self, ws, message: str):
        """接收 WebSocket 消息"""
        try:
            data = json.loads(message)

            if data.get("type") == "game_state":
                state = data.get("data", {})
                self.current_state.update(state)
                log.debug(f"游戏状态更新: {state}")

                # 发出信号更新UI
                self.game_state_updated.emit(self.current_state)

            elif data.get("type") == "log_status":
                log_status = data.get("data", {})
                log.debug(f"日志状态更新: {log_status}")
                self.log_status_updated.emit(log_status)

            elif data.get("type") == "vision_state":
                vision_data = data.get("data", {})
                log.debug(f"OCR 识别结果: {vision_data}")
                self.vision_state_updated.emit(vision_data)

        except json.JSONDecodeError as e:
            log.warning(f"无效的 WebSocket 消息: {e}")
        except Exception as e:
            log.error(f"处理 WebSocket 消息失败: {e}")

    def on_error(self, ws, error):
        """WebSocket 错误"""
        log.error(f"WebSocket 错误: {error}")
        self.connection_status.emit(f"Connection error: {error}", False)

    def on_close(self, ws, close_status_code, close_msg):
        """WebSocket 关闭"""
        log.info("WebSocket 已关闭")
        if self.is_running:
            self.connection_status.emit("Connection lost, trying to reconnect...", False)

    def stop(self):
        """停止 WebSocket 连接"""
        self.is_running = False
        if self.ws:
            self.ws.close()

    def send_ping(self):
        """发送 ping 保持连接"""
        if self.ws:
            try:
                self.ws.send("ping")
            except Exception as e:
                log.debug(f"Ping 失败: {e}")


# ---------------------------------------------------------------------------
# 路径设置对话框
# ---------------------------------------------------------------------------

class PathSettingsDialog(QDialog):
    """允许用户设置存档和日志路径"""

    def __init__(self, parent: QWidget | None = None, backend_url: str = "") -> None:
        super().__init__(parent)
        self.backend_url = backend_url
        self.setWindowTitle("Path Settings")
        self.setModal(True)
        self.setMinimumWidth(500)
        self._build_ui()

    def _build_ui(self) -> None:
        layout = QVBoxLayout(self)
        layout.setSpacing(12)

        # 帮助信息
        help_text = QLabel(
            "Set the folders where game files are located. The system will automatically search for the corresponding files in the folders.\n"
            "• Save Folder: Should contain save files like current_run.save\n"
            "• Log Folder: Should contain log files like godot.log"
        )
        help_text.setStyleSheet("color: #666; font-size: 12pt; padding: 8px;")
        help_text.setWordWrap(True)
        layout.addWidget(help_text)

        # ===== 语言设置 =====
        lang_layout = QHBoxLayout()
        lang_label = QLabel("🌐 Card Display Language:")
        lang_label.setStyleSheet("font-weight: bold; font-size: 11pt;")
        lang_layout.addWidget(lang_label)

        self._lang_combo = QComboBox()
        self._lang_combo.addItem("English", "en")
        self._lang_combo.addItem("简体中文", "zh")
        try:
            from scripts.config_manager import get_language
            current_lang = get_language()
            idx = self._lang_combo.findData(current_lang)
            if idx >= 0:
                self._lang_combo.setCurrentIndex(idx)
        except Exception:
            pass
        self._lang_combo.setMaximumWidth(150)
        lang_layout.addWidget(self._lang_combo)
        lang_layout.addStretch()
        layout.addLayout(lang_layout)

        # 分隔线
        separator1 = QFrame()
        separator1.setFrameShape(QFrame.Shape.HLine)
        separator1.setFrameShadow(QFrame.Shadow.Sunken)
        layout.addWidget(separator1)

        # ===== 存档路径 =====
        save_header_layout = QHBoxLayout()
        save_label = QLabel("📂 Save Folder:")
        save_label.setStyleSheet("font-weight: bold; font-size: 11pt;")
        save_header_layout.addWidget(save_label)

        self._save_indicator = QLabel("●")
        self._save_indicator.setStyleSheet("color: #aaa; font-size: 16pt;")
        save_header_layout.addWidget(self._save_indicator)
        save_header_layout.addStretch()
        layout.addLayout(save_header_layout)

        save_input_layout = QHBoxLayout()
        self._save_path_input = QLineEdit()
        self._save_path_input.setPlaceholderText("Select the folder containing save files...")
        self._save_path_input.setMinimumHeight(32)
        self._save_path_input.textChanged.connect(self._validate_save_path)
        save_browse_btn = QPushButton("Browse...")
        save_browse_btn.setMaximumWidth(80)
        save_browse_btn.clicked.connect(self._browse_save_path)
        save_input_layout.addWidget(self._save_path_input)
        save_input_layout.addWidget(save_browse_btn)
        layout.addLayout(save_input_layout)

        # 存档文件夹验证提示
        self._save_status = QLabel()
        self._save_status.setStyleSheet("color: #666; font-size: 9pt; margin-left: 4px;")
        self._save_status.setWordWrap(True)
        layout.addWidget(self._save_status)
        self._update_save_hint()

        # 分隔线
        separator2 = QFrame()
        separator2.setFrameShape(QFrame.Shape.HLine)
        separator2.setFrameShadow(QFrame.Shadow.Sunken)
        layout.addWidget(separator2)

        # ===== 日志路径 =====
        log_header_layout = QHBoxLayout()
        log_label = QLabel("📋 Log Folder:")
        log_label.setStyleSheet("font-weight: bold; font-size: 11pt;")
        log_header_layout.addWidget(log_label)

        self._log_indicator = QLabel("●")
        self._log_indicator.setStyleSheet("color: #aaa; font-size: 16pt;")
        log_header_layout.addWidget(self._log_indicator)
        log_header_layout.addStretch()
        layout.addLayout(log_header_layout)

        log_input_layout = QHBoxLayout()
        self._log_path_input = QLineEdit()
        self._log_path_input.setPlaceholderText("Select the folder containing log files...")
        self._log_path_input.setMinimumHeight(32)
        self._log_path_input.textChanged.connect(self._validate_log_path)
        log_browse_btn = QPushButton("Browse...")
        log_browse_btn.setMaximumWidth(80)
        log_browse_btn.clicked.connect(self._browse_log_path)
        log_input_layout.addWidget(self._log_path_input)
        log_input_layout.addWidget(log_browse_btn)
        layout.addLayout(log_input_layout)

        # 日志文件夹验证提示
        self._log_status = QLabel()
        self._log_status.setStyleSheet("color: #666; font-size: 9pt; margin-left: 4px;")
        self._log_status.setWordWrap(True)
        layout.addWidget(self._log_status)
        self._update_log_hint()

        layout.addStretch()

        # 按钮
        button_layout = QHBoxLayout()
        button_layout.addStretch()

        save_btn = QPushButton("Save Settings")
        save_btn.setMinimumWidth(100)
        save_btn.clicked.connect(self._save_settings)
        button_layout.addWidget(save_btn)

        cancel_btn = QPushButton("Cancel")
        cancel_btn.setMinimumWidth(100)
        cancel_btn.clicked.connect(self.reject)
        button_layout.addWidget(cancel_btn)

        layout.addLayout(button_layout)

    def _validate_save_path(self) -> None:
        """实时验证存档路径"""
        path_text = self._save_path_input.text().strip()
        self._check_save_folder(path_text)

    def _validate_log_path(self) -> None:
        """实时验证日志路径"""
        path_text = self._log_path_input.text().strip()
        self._check_log_folder(path_text)

    def _check_save_folder(self, path_text: str) -> bool:
        """检查存档文件夹并搜索合适的文件"""
        from pathlib import Path

        if not path_text:
            self._save_indicator.setStyleSheet("color: #aaa;")
            self._save_status.setText("Not set")
            return False

        try:
            folder = Path(path_text)
            if not folder.exists():
                self._save_indicator.setStyleSheet("color: #F44336;")
                self._save_status.setText(f"❌ Folder does not exist: {path_text}")
                return False

            if not folder.is_dir():
                self._save_indicator.setStyleSheet("color: #F44336;")
                self._save_status.setText(f"❌ Not a folder: {path_text}")
                return False

            # 搜索合适的存档文件
            save_files = list(folder.glob("*.save")) + list(folder.glob("current_run.save*"))
            if save_files:
                found_file = save_files[0]
                self._save_indicator.setStyleSheet("color: #4CAF50;")
                self._save_status.setText(f"✓ Found save file: {found_file.name}")
                return True
            else:
                self._save_indicator.setStyleSheet("color: #FFC107;")
                self._save_status.setText(f"⚠ Folder exists but no *.save files found")
                return False

        except Exception as e:
            self._save_indicator.setStyleSheet("color: #F44336;")
            self._save_status.setText(f"❌ Check failed: {e}")
            return False

    def _check_log_folder(self, path_text: str) -> bool:
        """检查日志文件夹并搜索合适的文件"""
        from pathlib import Path

        if not path_text:
            self._log_indicator.setStyleSheet("color: #aaa;")
            self._log_status.setText("Not set")
            return False

        try:
            folder = Path(path_text)
            if not folder.exists():
                self._log_indicator.setStyleSheet("color: #F44336;")
                self._log_status.setText(f"❌ Folder does not exist: {path_text}")
                return False

            if not folder.is_dir():
                self._log_indicator.setStyleSheet("color: #F44336;")
                self._log_status.setText(f"❌ Not a folder: {path_text}")
                return False

            # 搜索合适的日志文件
            log_files = list(folder.glob("*.log")) + list(folder.glob("*.txt"))
            if log_files:
                # 找最新的日志文件
                latest = max(log_files, key=lambda p: p.stat().st_mtime)
                self._log_indicator.setStyleSheet("color: #4CAF50;")
                self._log_status.setText(f"✓ Found log file: {latest.name}")
                return True
            else:
                self._log_indicator.setStyleSheet("color: #FFC107;")
                self._log_status.setText(f"⚠ Folder exists but no *.log or *.txt files found")
                return False

        except Exception as e:
            self._log_indicator.setStyleSheet("color: #F44336;")
            self._log_status.setText(f"❌ Check failed: {e}")
            return False

    def _update_save_hint(self) -> None:
        """更新存档路径的自动检测提示"""
        try:
            from pathlib import Path
            steam_path = Path.home() / "AppData" / "Roaming" / "SlayTheSpire2" / "steam"
            if steam_path.exists():
                for save_dir in steam_path.glob("*/profile*/saves"):
                    if save_dir.is_dir():
                        self._save_path_input.setPlaceholderText(f"Default location: {save_dir}")
                        self._check_save_folder(str(save_dir))
                        return
        except Exception:
            pass

    def _update_log_hint(self) -> None:
        """更新日志路径的自动检测提示"""
        try:
            from pathlib import Path
            log_path = Path.home() / "AppData" / "Roaming" / "SlayTheSpire2" / "logs"
            if log_path.exists():
                self._log_path_input.setPlaceholderText(f"Default location: {log_path}")
                self._check_log_folder(str(log_path))
                return
        except Exception:
            pass

    def _browse_save_path(self) -> None:
        """浏览并选择存档文件夹"""
        from pathlib import Path
        # 默认打开Steam存档目录
        default_dir = str(Path.home() / "AppData" / "Roaming" / "SlayTheSpire2")

        path = QFileDialog.getExistingDirectory(
            self,
            "Select Save Folder",
            default_dir,
            QFileDialog.Option.ShowDirsOnly
        )
        if path:
            self._save_path_input.setText(path)

    def _browse_log_path(self) -> None:
        """浏览并选择日志文件夹"""
        from pathlib import Path
        # 默认打开日志目录
        default_dir = str(Path.home() / "AppData" / "Roaming" / "SlayTheSpire2")

        path = QFileDialog.getExistingDirectory(
            self,
            "Select Log Folder",
            default_dir,
            QFileDialog.Option.ShowDirsOnly
        )
        if path:
            self._log_path_input.setText(path)

    def _save_settings(self) -> None:
        try:
            # 保存语言设置
            lang = self._lang_combo.currentData()
            from scripts.config_manager import set_language
            set_language(lang)

            save_path = self._save_path_input.text().strip()
            log_path = self._log_path_input.text().strip()

            payload = {}
            if save_path:
                payload["save_path"] = save_path
            if log_path:
                payload["log_path"] = log_path

            if payload:
                resp = requests.post(
                    f"{self.backend_url}/api/config",
                    json=payload,
                    timeout=5,
                )
                resp.raise_for_status()

            log.info("✓ 设置已保存")
            self.accept()
        except Exception as e:
            log.error(f"保存设置失败: {e}")


# ---------------------------------------------------------------------------
# 单张卡评估结果 Widget
# ---------------------------------------------------------------------------

_ROLE_ZH = {
    "core":       "Core",
    "enabler":    "Enabler",
    "transition": "Transition",
    "filler":     "Filler",
    "pollution":  "Curse/Status",
    "unknown":    "Unknown",
}

_REC_COLORS = {
    "强烈推荐": "#A8D870", "Highly Recommended": "#A8D870",
    "推荐":     "#64B5F6", "Recommended":        "#64B5F6",
    "可选":     "#FFD54F", "Optional":           "#FFD54F",
    "谨慎":     "#FFB74D", "Caution":            "#FFB74D",
    "不推荐":   "#FF7043", "Not Recommended":    "#FF7043",
    "跳过":     "#EF5350", "Skip":               "#EF5350",
}

_GRADE_COLORS = {
    "S":  "#FFD700",  # 金色
    "A+": "#A8D870",  # 亮绿
    "A":  "#A8D870",
    "A-": "#64B5F6",  # 蓝色
    "B+": "#64B5F6",
    "B":  "#FFD54F",  # 黄色
    "B-": "#FFB74D",  # 橙色
    "C+": "#FFB74D",
    "C":  "#FF7043",  # 红橙
    "D":  "#EF5350",  # 红色
}


class CardResultWidget(QFrame):
    """
    垂直布局的单张卡评估结果块：
      卡牌名（中文，粗体大字）
      定位（中文角色标签） | 分数 | 推荐
      推荐理由（绿色小字）
      不推荐理由（橙红色小字）
    """

    def __init__(self, result: dict, language: str = "en", parent: QWidget | None = None) -> None:
        super().__init__(parent)
        self.setObjectName("CardResultWidget")
        self._language = language
        self._build_ui(result)

    def _build_ui(self, result: dict) -> None:
        outer = QVBoxLayout(self)
        outer.setContentsMargins(10, 6, 10, 6)
        outer.setSpacing(3)

        # ── 行1：卡牌名（中文） ──────────────────────────────────────────
        raw_name = result.get("card_name", "?")
        card_id = result.get("card_id", "")
        try:
            try:
                from frontend.card_locale import get_card_locale
            except ImportError:
                from card_locale import get_card_locale
            zh_name = get_card_locale().get_chinese_name(card_id)
            raw_name = zh_name or raw_name
        except Exception:
            pass

        name_label = QLabel(raw_name)
        name_label.setObjectName("cardName")
        name_label.setStyleSheet("font-weight:bold;font-size:17px;")
        outer.addWidget(name_label)

        # ── 行2：定位 | 分数 | 推荐 ─────────────────────────────────────
        meta_row = QHBoxLayout()
        meta_row.setSpacing(6)

        role_en = result.get("role", "unknown")
        role_zh = _ROLE_ZH.get(role_en, role_en)
        role_label = QLabel(role_zh)
        role_label.setObjectName("cardRole")
        role_label.setStyleSheet("color:#A09070;font-size:14px;")
        meta_row.addWidget(role_label)

        meta_row.addStretch()

        grade = result.get("grade", "")
        score = result.get("total_score", 0)
        grade_text = grade if grade else f"{score:.0f}"
        grade_color = _GRADE_COLORS.get(grade, "#C8A96E")
        score_label = QLabel(grade_text)
        score_label.setObjectName("cardScore")
        score_label.setStyleSheet(f"color:{grade_color};font-size:15px;font-weight:bold;")
        meta_row.addWidget(score_label)

        rec = result.get("recommendation", "")
        rec_color = _REC_COLORS.get(rec, "#9A8A6A")
        rec_label = QLabel(rec)
        rec_label.setObjectName("cardRecommendation")
        rec_label.setStyleSheet(f"color:{rec_color};font-weight:bold;font-size:14px;")
        meta_row.addWidget(rec_label)

        outer.addLayout(meta_row)

        # ── 行3+：推荐 / 不推荐理由 ─────────────────────────────────────
        reasons_for     = result.get("reasons_for", [])
        reasons_against = result.get("reasons_against", [])

        if reasons_for:
            lbl = QLabel("▸ " + "; ".join(reasons_for))
            lbl.setObjectName("cardReasonFor")
            lbl.setWordWrap(True)
            lbl.setStyleSheet("color:#8BC34A;font-size:12px;padding-top:1px;")
            outer.addWidget(lbl)

        if reasons_against:
            lbl = QLabel("▸ " + "; ".join(reasons_against))
            lbl.setObjectName("cardReasonAgainst")
            lbl.setWordWrap(True)
            lbl.setStyleSheet("color:#FF8A65;font-size:12px;padding-top:1px;")
            outer.addWidget(lbl)


class VerdictWidget(QFrame):
    """
    V2: Final recommendation widget showing pick vs skip verdict.
    Displayed at the bottom of the results list.
    """

    def __init__(self, verdict: dict, parent: QWidget | None = None) -> None:
        super().__init__(parent)
        self.setObjectName("VerdictWidget")
        self._build_ui(verdict)

    def _build_ui(self, verdict: dict) -> None:
        layout = QVBoxLayout(self)
        layout.setContentsMargins(12, 10, 12, 10)
        layout.setSpacing(6)

        best_action = verdict.get("best_action", "skip")
        recommendation = verdict.get("recommendation", "")
        skip_score = verdict.get("skip_score", 50)
        pick_delta = verdict.get("pick_delta", 0)

        # Background color based on action
        if best_action == "pick":
            if pick_delta > 15:
                bg_color = "rgba(76, 175, 80, 0.25)"   # Strong pick - green
                border_color = "#4CAF50"
            else:
                bg_color = "rgba(100, 181, 246, 0.25)"  # Marginal pick - blue
                border_color = "#64B5F6"
        else:
            if pick_delta < -10:
                bg_color = "rgba(239, 83, 80, 0.20)"   # Strong skip - red
                border_color = "#EF5350"
            else:
                bg_color = "rgba(255, 183, 77, 0.25)"   # Borderline skip - orange
                border_color = "#FFB74D"

        self.setStyleSheet(f"""
            QFrame#VerdictWidget {{
                background: {bg_color};
                border: 2px solid {border_color};
                border-radius: 8px;
            }}
        """)

        # Header: "FINAL VERDICT"
        header = QLabel("📊 FINAL VERDICT")
        header.setStyleSheet(f"color: {border_color}; font-size: 11px; font-weight: bold;")
        layout.addWidget(header)

        # Main recommendation text
        rec_label = QLabel(recommendation)
        rec_label.setWordWrap(True)
        rec_label.setStyleSheet(f"color: #E0E0E0; font-size: 15px; font-weight: bold;")
        layout.addWidget(rec_label)

        # Skip score info
        info_parts = [f"Skip score: {skip_score:.0f}"]
        if pick_delta != 0:
            delta_sign = "+" if pick_delta > 0 else ""
            info_parts.append(f"Delta: {delta_sign}{pick_delta:.0f}")
        
        info_label = QLabel(" | ".join(info_parts))
        info_label.setStyleSheet("color: #888; font-size: 11px;")
        layout.addWidget(info_label)


# ---------------------------------------------------------------------------
# 主窗口
# ---------------------------------------------------------------------------

class CardAdviserWindow(QWidget):
    """
    永远置顶的浮窗主窗口。
    支持鼠标拖拽移动（无边框模式）。
    """

    def __init__(self) -> None:
        super().__init__()
        self._drag_pos: QPoint | None = None
        self._dragging_from_title: bool = False
        self._worker: EvaluateWorker | None = None
        self._game_watcher: GameStateWatcher | None = None
        self._run_state = {}
        self._current_character: str = ""
        self._card_picker: CardPickerPanel | None = None
        self._selection_tray: SelectionTrayWidget | None = None
        self._cards_fetch_worker: CardsFetchWorker | None = None

        # 读取语言配置
        try:
            from scripts.config_manager import get_language
            self._language = get_language()
        except Exception:
            self._language = "en"

        self._init_window()
        self._build_ui()
        self._load_stylesheet()
        # 样式加载后让窗口根据内容自适应高度，宽度保持 _init_window 中设定的值
        QTimer.singleShot(0, self._auto_fit_height)

        # 启动时检查后端连通性
        QTimer.singleShot(500, self._check_backend)

        # 启动游戏状态监视
        QTimer.singleShot(1000, self._start_game_watcher)

    # ------------------------------------------------------------------
    # 窗口初始化
    # ------------------------------------------------------------------

    def _init_window(self) -> None:
        self.setWindowTitle("STS2 Card Adviser")
        self.setWindowFlags(
            Qt.WindowType.FramelessWindowHint
            | Qt.WindowType.WindowStaysOnTopHint
            | Qt.WindowType.Tool   # 不在任务栏显示
        )
        self.setAttribute(Qt.WidgetAttribute.WA_TranslucentBackground)
        self.setMinimumSize(460, 600)
        self.resize(600, 750)   # 初始高度由 _auto_fit_height 在布局完成后自动调整
        self._drawer_open = False   # 侧边抽屉初始收起

    # ------------------------------------------------------------------
    # UI 构建
    # ------------------------------------------------------------------

    def _build_ui(self) -> None:
        # 最外层水平：主面板 | 拨片按钮 | 侧边抽屉
        root = QHBoxLayout(self)
        root.setContentsMargins(0, 0, 0, 0)
        root.setSpacing(0)

        # ── 主面板 ──────────────────────────────────────────────────────
        main_panel = QWidget()
        main_panel.setObjectName("MainContainer")
        main_panel.setSizePolicy(QSizePolicy.Policy.Preferred, QSizePolicy.Policy.Expanding)
        root.addWidget(main_panel, 0)   # stretch=0：主面板宽度不被抽屉挤压

        main_layout = QVBoxLayout(main_panel)
        main_layout.setContentsMargins(0, 0, 0, 8)
        main_layout.setSpacing(0)

        # ---- 标题栏 ----
        title_bar = self._build_title_bar()
        main_layout.addWidget(title_bar)

        # ---- 工具栏 ----
        toolbar = self._build_toolbar()
        main_layout.addWidget(toolbar)

        # ---- 分隔线 ----
        sep = QFrame()
        sep.setFrameShape(QFrame.Shape.HLine)
        sep.setObjectName("Separator")
        main_layout.addWidget(sep)

        # ---- 评分区（含 OCR 识别预览 + 列表头 + 卡牌列表）----
        score_section = QWidget()
        score_section.setObjectName("ScoreSection")
        score_layout = QVBoxLayout(score_section)
        score_layout.setContentsMargins(0, 0, 0, 0)
        score_layout.setSpacing(0)

        self._ocr_preview_panel = self._build_ocr_preview_panel()
        score_layout.addWidget(self._ocr_preview_panel)

        header = self._build_list_header()
        score_layout.addWidget(header)

        self._scroll_area = QScrollArea()
        self._scroll_area.setWidgetResizable(True)
        self._scroll_area.setHorizontalScrollBarPolicy(
            Qt.ScrollBarPolicy.ScrollBarAlwaysOff
        )
        self._scroll_area.setObjectName("CardScrollArea")

        self._list_container = QWidget()
        self._list_layout = QVBoxLayout(self._list_container)
        self._list_layout.setContentsMargins(4, 4, 4, 4)
        self._list_layout.setSpacing(4)
        self._list_layout.addStretch()

        self._scroll_area.setWidget(self._list_container)
        score_layout.addWidget(self._scroll_area)

        main_layout.addWidget(score_section)

        # ---- 套路提示 + 状态栏 + 缩放手柄 ----
        bottom_bar = QWidget()
        bottom_bar.setObjectName("BottomBar")
        bottom_bar.setStyleSheet("background:rgba(22,18,14,0.85);border-top:1px solid #2E2416;")
        bottom_layout = QVBoxLayout(bottom_bar)
        bottom_layout.setContentsMargins(8, 4, 8, 2)
        bottom_layout.setSpacing(2)

        self._archetype_label = QLabel("")
        self._archetype_label.setObjectName("ArchetypeLabel")
        self._archetype_label.setAlignment(Qt.AlignmentFlag.AlignCenter)
        self._archetype_label.setWordWrap(True)
        self._archetype_label.setStyleSheet(
            "color:#C8A96E;font-size:18px;font-weight:bold;padding:2px 0px;"
        )
        self._archetype_label.setVisible(False)
        bottom_layout.addWidget(self._archetype_label)

        status_row = QHBoxLayout()
        status_row.setContentsMargins(0, 0, 0, 0)
        status_row.setSpacing(0)

        self._status_label = QLabel("Ready — Waiting for game data")
        self._status_label.setObjectName("StatusBar")
        self._status_label.setAlignment(Qt.AlignmentFlag.AlignCenter)
        status_row.addWidget(self._status_label, 1)

        grip = QSizeGrip(self)
        grip.setObjectName("ResizeGrip")
        status_row.addWidget(grip, 0, Qt.AlignmentFlag.AlignBottom | Qt.AlignmentFlag.AlignRight)
        bottom_layout.addLayout(status_row)

        main_layout.addWidget(bottom_bar)

        # ── 拨片按钮（主面板右侧，始终可见）───────────────────────────
        self._drawer_toggle_btn = QPushButton("◀")
        self._drawer_toggle_btn.setObjectName("DrawerToggleBtn")
        self._drawer_toggle_btn.setToolTip("Expand/Collapse Manual Card Selection")
        self._drawer_toggle_btn.setCursor(Qt.CursorShape.PointingHandCursor)
        self._drawer_toggle_btn.clicked.connect(self._toggle_side_drawer)
        self._drawer_toggle_btn.setSizePolicy(
            QSizePolicy.Policy.Fixed, QSizePolicy.Policy.Expanding
        )
        root.addWidget(self._drawer_toggle_btn)

        # ── 侧边抽屉（嵌入式，初始隐藏）────────────────────────────────
        self._side_drawer = self._build_side_drawer()
        self._side_drawer.setVisible(False)
        root.addWidget(self._side_drawer)

        # 展示占位数据
        self._show_placeholder()

    def _build_title_bar(self) -> QWidget:
        bar = QWidget()
        bar.setObjectName("TitleBar")
        bar.setMinimumHeight(38)
        # 鼠标事件转发给主窗口，实现标题栏拖拽
        bar.mousePressEvent = self._title_mouse_press
        bar.mouseMoveEvent = self._title_mouse_move
        bar.mouseReleaseEvent = self._title_mouse_release
        layout = QHBoxLayout(bar)
        layout.setContentsMargins(10, 0, 6, 0)

        title = QLabel("⚔ STS2 Adviser")
        title.setObjectName("TitleLabel")
        layout.addWidget(title)
        layout.addStretch()

        close_btn = QPushButton("×")
        close_btn.setObjectName("CloseButton")
        close_btn.setFixedSize(26, 26)
        close_btn.clicked.connect(self.close)
        layout.addWidget(close_btn)

        return bar

    def _build_toolbar(self) -> QWidget:
        toolbar = QWidget()
        toolbar.setObjectName("Toolbar")
        toolbar.setMinimumHeight(100)
        main_layout = QVBoxLayout(toolbar)
        main_layout.setContentsMargins(8, 4, 8, 4)
        main_layout.setSpacing(4)

        # ===== 第一行：按钮 =====
        btn_layout = QHBoxLayout()
        btn_layout.setSpacing(4)

        # 刷新检测按钮
        refresh_detect_btn = QPushButton("🔄 Detect")
        refresh_detect_btn.setObjectName("RefreshDetectButton")
        refresh_detect_btn.setToolTip("Re-detect game and logs")
        refresh_detect_btn.clicked.connect(self._on_refresh_detect)
        btn_layout.addWidget(refresh_detect_btn)

        # 手动截图识别按钮（单次触发，与后台自动轮询独立）
        self._ocr_btn = QPushButton("📷 OCR Snapshot")
        self._ocr_btn.setObjectName("OcrButton")
        self._ocr_btn.setToolTip("Manually take a snapshot for OCR (runs independently from background polling)")
        self._ocr_btn.clicked.connect(self._on_ocr_snapshot)
        btn_layout.addWidget(self._ocr_btn)

        # 设置按钮
        settings_btn = QPushButton("⚙ Settings")
        settings_btn.setObjectName("SettingsButton")
        settings_btn.setToolTip("Path Settings")
        settings_btn.clicked.connect(self._on_settings)
        btn_layout.addWidget(settings_btn)

        btn_layout.addStretch()
        main_layout.addLayout(btn_layout)

        # ===== 第二行：游戏信息 =====
        info_layout = QHBoxLayout()
        info_layout.setSpacing(8)

        self._game_info_label = QLabel("Waiting for game data...")
        self._game_info_label.setObjectName("GameInfoLabel")
        self._game_info_label.setStyleSheet("font-size: 12pt; color: #666;")
        self._game_info_label.setTextFormat(Qt.TextFormat.RichText)
        info_layout.addWidget(self._game_info_label)

        info_layout.addStretch()
        main_layout.addLayout(info_layout)

        # ===== 第三行：三个指示灯（分三列） =====
        indicators_layout = QHBoxLayout()
        indicators_layout.setSpacing(16)

        # 后端连接指示器
        backend_box = QVBoxLayout()
        backend_box.setSpacing(2)
        self._backend_indicator = QLabel("● Backend")
        self._backend_indicator.setObjectName("BackendIndicator")
        self._backend_indicator.setStyleSheet("color: #aaa; font-weight: bold;")
        backend_box.addWidget(self._backend_indicator)
        indicators_layout.addLayout(backend_box)

        # 游戏存档指示器
        game_box = QVBoxLayout()
        game_box.setSpacing(2)
        self._game_indicator = QLabel("● Game")
        self._game_indicator.setObjectName("GameIndicator")
        self._game_indicator.setStyleSheet("color: #F44336; font-weight: bold;")
        game_box.addWidget(self._game_indicator)
        indicators_layout.addLayout(game_box)

        # 日志监视指示器
        log_box = QVBoxLayout()
        log_box.setSpacing(2)
        self._log_indicator = QLabel("● Logs")
        self._log_indicator.setObjectName("LogIndicator")
        self._log_indicator.setStyleSheet("color: #F44336; font-weight: bold;")
        log_box.addWidget(self._log_indicator)
        indicators_layout.addLayout(log_box)

        # 视觉自动轮询指示器（后台 VisionBridge 状态）
        ocr_box = QHBoxLayout()
        ocr_box.setSpacing(4)
        self._ocr_indicator = QLabel("● Vision")
        self._ocr_indicator.setObjectName("OcrIndicator")
        self._ocr_indicator.setStyleSheet("color: #aaa; font-weight: bold;")
        self._ocr_indicator.setToolTip("Background Auto Vision Status (Polling 1/sec)")
        ocr_box.addWidget(self._ocr_indicator)
        # 轮询状态小字（监视中 / 识别中 / 已锁定）
        self._ocr_state_badge = QLabel("Monitoring")
        self._ocr_state_badge.setObjectName("OcrStateBadge")
        self._ocr_state_badge.setStyleSheet(
            "color: #555; font-size: 10px; "
            "border: 1px solid #333; border-radius: 3px; padding: 0px 4px;"
        )
        ocr_box.addWidget(self._ocr_state_badge)
        indicators_layout.addLayout(ocr_box)

        indicators_layout.addStretch()
        main_layout.addLayout(indicators_layout)

        # ===== 第四行：OCR 界面识别提示 =====
        self._ocr_screen_label = QLabel("")
        self._ocr_screen_label.setObjectName("OcrScreenLabel")
        self._ocr_screen_label.setStyleSheet(
            "color: #888; font-size: 11pt; padding: 2px 0px;"
        )
        self._ocr_screen_label.setVisible(False)
        main_layout.addWidget(self._ocr_screen_label)

        return toolbar

    def _run_debug(self) -> None:
        log.info("调试按钮被点击，开始执行调试逻辑...")
        self._status_label.setText("Debugging, check logs...")

    def _open_log_directory(self) -> None:
        log_dir = str(get_app_root())
        subprocess.Popen(f'explorer "{log_dir}"')
        log.info("打开日志目录窗口。")

    def _build_ocr_preview_panel(self) -> QWidget:
        """
        OCR 视觉识别 + 评分提示合并面板。
        默认隐藏，检测到选卡界面时显示在评分列表上方。
        """
        panel = QWidget()
        panel.setObjectName("OcrPreviewPanel")
        panel.setVisible(False)
        panel.setStyleSheet(
            "background:rgba(15,22,35,0.85);border-bottom:1px solid #1E3A5A;"
        )

        layout = QVBoxLayout(panel)
        layout.setContentsMargins(8, 6, 8, 6)
        layout.setSpacing(4)

        # 标题行：图标 + "视觉识别" + 状态
        title_row = QHBoxLayout()
        lbl_title = QLabel("📷 Vision Recognition")
        lbl_title.setStyleSheet("color:#64B5F6;font-size:12px;font-weight:bold;")
        title_row.addWidget(lbl_title)
        title_row.addStretch()

        self._ocr_preview_status = QLabel("Recognizing...")
        self._ocr_preview_status.setStyleSheet("color:#888;font-size:11px;")
        title_row.addWidget(self._ocr_preview_status)
        layout.addLayout(title_row)

        # 三张候选卡名（大字，作为识别结果展示）
        cards_row = QHBoxLayout()
        cards_row.setSpacing(6)
        self._ocr_preview_cards: list[QLabel] = []
        for i in range(3):
            card_lbl = QLabel(f"— Card {i+1} —")
            card_lbl.setObjectName(f"OcrPreviewCard{i}")
            card_lbl.setAlignment(Qt.AlignmentFlag.AlignCenter)
            card_lbl.setStyleSheet(
                "color:#555;font-size:13px;"
                "border:1px solid #1E3A5A;border-radius:4px;"
                "padding:4px 6px;background:#0d1520;"
            )
            card_lbl.setSizePolicy(QSizePolicy.Policy.Expanding, QSizePolicy.Policy.Preferred)
            self._ocr_preview_cards.append(card_lbl)
            cards_row.addWidget(card_lbl)
        layout.addLayout(cards_row)

        # 提示文字（解释性）
        self._ocr_hint_label = QLabel("Card reward screen detected, evaluating candidates...")
        self._ocr_hint_label.setStyleSheet("color:#556672;font-size:11px;padding-top:2px;")
        self._ocr_hint_label.setAlignment(Qt.AlignmentFlag.AlignCenter)
        layout.addWidget(self._ocr_hint_label)

        return panel

    def _build_list_header(self) -> QWidget:
        header = QWidget()
        header.setObjectName("ListHeader")
        layout = QHBoxLayout(header)
        layout.setContentsMargins(10, 2, 10, 2)

        lbl = QLabel("Candidate Evaluation")
        lbl.setObjectName("HeaderLabel")
        lbl.setStyleSheet("color:#8A7A5A;font-size:11px;")
        layout.addWidget(lbl)
        layout.addStretch()

        return header

    # ------------------------------------------------------------------
    # 数据加载
    # ------------------------------------------------------------------

    def _check_backend(self) -> None:
        try:
            resp = requests.get(f"{BACKEND_URL}/", timeout=2)
            if resp.status_code == 200:
                self._set_backend_connected(True)
            else:
                self._set_backend_connected(False)
        except Exception:
            self._set_backend_connected(False)

    def _set_backend_connected(self, connected: bool) -> None:
        if connected:
            self._backend_indicator.setText("● Backend Connected")
            self._backend_indicator.setStyleSheet("color: #4CAF50;")
        else:
            self._backend_indicator.setText("● Backend Disconnected")
            self._backend_indicator.setStyleSheet("color: #F44336;")

    def _start_game_watcher(self) -> None:
        """启动游戏状态实时监视"""
        if self._game_watcher is not None:
            return

        self._game_watcher = GameStateWatcher(BACKEND_URL)
        self._game_watcher.game_state_updated.connect(self._on_game_state_update)
        self._game_watcher.connection_status.connect(self._on_connection_status)
        self._game_watcher.log_status_updated.connect(self._on_log_status_update)
        self._game_watcher.vision_state_updated.connect(self._on_vision_state_update)
        self._game_watcher.start()

        log.info("✓ 游戏状态监视已启动")

    def _on_game_state_update(self, state: dict) -> None:
        """处理游戏状态更新"""
        log.debug(f"游戏状态更新: {state}")

        # 更新本地状态（规范化前缀）
        raw_char = state.get("character", "silent")
        raw_deck = state.get("deck", [])
        raw_relics = state.get("relics", [])

        norm_char = raw_char.replace("CHARACTER.", "").lower() if isinstance(raw_char, str) else raw_char
        norm_deck = [c.replace("CARD.", "").lower() if isinstance(c, str) else c for c in raw_deck]
        norm_relics = [
            {"id": r.replace("RELIC.", "").lower(), "name": r.replace("RELIC.", ""), "tags": []}
            if isinstance(r, str) else r
            for r in raw_relics
        ]

        self._run_state.update({
            "character": norm_char,
            "floor": state.get("floor", 0),
            "hp": state.get("hp", 0),
            "max_hp": state.get("max_hp", 70),
            "gold": state.get("gold", 0),
            "ascension": state.get("ascension", 0),
            "deck": norm_deck,
            "relics": norm_relics,
            "mode": state.get("mode", "single"),
        })

        # 更新游戏存档指示器
        character = state.get("character", "").replace("CHARACTER.", "").upper()
        floor = state.get("floor", 0)

        if character and floor > 0:
            self._game_indicator.setText("● Game")
            self._game_indicator.setStyleSheet("color: #4CAF50; font-weight: bold;")

            # 显示游戏信息
            hp = state.get("hp", 0)
            max_hp = state.get("max_hp", 70)
            gold = state.get("gold", 0)
            deck_size = len(state.get("deck", []))
            ascension = state.get("ascension", 0)

            # HP 颜色（低血量变红）
            hp_ratio = hp / max(max_hp, 1)
            if hp_ratio > 0.6:
                hp_color = "#4CAF50"   # 绿
            elif hp_ratio > 0.3:
                hp_color = "#FF9800"   # 橙
            else:
                hp_color = "#F44336"   # 红

            asc_text = f" <span style='color:#9C27B0'>A{ascension}</span>" if ascension > 0 else ""
            game_mode = state.get("mode", "single")
            if game_mode == "coop":
                mode_text = "  <span style='color:#26C6DA;font-size:10pt'>👥 Co-op</span>"
            else:
                mode_text = "  <span style='color:#81C784;font-size:10pt'>🧍 Solo</span>"
            info_html = (
                f"<span style='color:#64B5F6;font-weight:bold'>{character}</span>"
                f"{asc_text}"
                f"  <span style='color:#aaa'>F{floor}</span>"
                f"  <span style='color:{hp_color}'>❤ {hp}/{max_hp}</span>"
                f"  <span style='color:#FFD54F'>💰 {gold}</span>"
                f"  <span style='color:#90CAF9'>🃏 {deck_size}</span>"
                f"{mode_text}"
            )
            self._game_info_label.setText(info_html)
            self._game_info_label.setStyleSheet("font-size: 12pt;")
        else:
            self._game_indicator.setText("● Game")
            self._game_indicator.setStyleSheet("color: #F44336; font-weight: bold;")
            self._game_info_label.setText("<span style='color:#999'>Game not detected</span>")
            self._game_info_label.setStyleSheet("font-size: 12pt;")

        # 更新底部状态栏
        if state.get("hand"):
            self._status_label.setText(f"Hand size: {len(state.get('hand', []))} cards")
        elif character and floor > 0:
            self._status_label.setText(f"Ready — Select candidates and click Evaluate")

        # 检测到角色时加载对应卡牌
        if norm_char and norm_char != self._current_character:
            self._fetch_cards_for_character(norm_char)

    def _on_connection_status(self, status: str, connected: bool) -> None:
        """处理 WebSocket 连接状态变化"""
        if connected:
            log.info(f"游戏监视: {status}")
        else:
            log.warning(f"游戏监视: {status}")

    def _on_log_status_update(self, status: dict) -> None:
        """处理日志监视状态更新"""
        active = status.get("active", False)
        if active:
            self._log_indicator.setText("● Logs")
            self._log_indicator.setStyleSheet("color: #4CAF50;")
            log.info(f"日志正在监视: {status.get('path')}")
        else:
            self._log_indicator.setText("● Logs")
            self._log_indicator.setStyleSheet("color: #F44336;")
            log.warning("日志未被监视")

    def _on_vision_state_update(self, data: dict) -> None:
        """处理 OCR 视觉识别结果"""
        screen_type = data.get("screen_type", "unknown")
        all_reliable = data.get("all_reliable", False)
        card_names = data.get("card_names", [])
        card_choices = data.get("card_choices", [])
        confidences = data.get("confidences", [])

        # 更新视觉轮询指示灯 + badge
        if screen_type == "card_reward":
            if all_reliable:
                self._ocr_indicator.setText("● Vision")
                self._ocr_indicator.setStyleSheet("color: #4CAF50; font-weight: bold;")
                self._ocr_indicator.setToolTip("Background Vision: Locked on Card Reward (Auto Polling)")
                self._ocr_state_badge.setText("Locked")
                self._ocr_state_badge.setStyleSheet(
                    "color: #4CAF50; font-size: 10px; "
                    "border: 1px solid #2E7D32; border-radius: 3px; padding: 0px 4px;"
                )
            else:
                self._ocr_indicator.setText("● Vision")
                self._ocr_indicator.setStyleSheet("color: #FF9800; font-weight: bold;")
                self._ocr_indicator.setToolTip("Background Vision: Recognizing (Waiting for stabilization)")
                self._ocr_state_badge.setText("Recognizing")
                self._ocr_state_badge.setStyleSheet(
                    "color: #FF9800; font-size: 10px; "
                    "border: 1px solid #E65100; border-radius: 3px; padding: 0px 4px;"
                )
        elif screen_type == "shop":
            self._ocr_indicator.setText("● Vision")
            self._ocr_indicator.setStyleSheet("color: #64B5F6; font-weight: bold;")
            self._ocr_indicator.setToolTip("Background Vision: Shop Detected")
            self._ocr_state_badge.setText("Shop")
            self._ocr_state_badge.setStyleSheet(
                "color: #64B5F6; font-size: 10px; "
                "border: 1px solid #1565C0; border-radius: 3px; padding: 0px 4px;"
            )
        else:
            self._ocr_indicator.setText("● Vision")
            self._ocr_indicator.setStyleSheet("color: #aaa; font-weight: bold;")
            self._ocr_indicator.setToolTip("Background Vision: Monitoring (Auto snapshot every 1s)")
            self._ocr_state_badge.setText("Monitoring")
            self._ocr_state_badge.setStyleSheet(
                "color: #555; font-size: 10px; "
                "border: 1px solid #333; border-radius: 3px; padding: 0px 4px;"
            )

        # 更新 OCR 界面提示文字
        _SCREEN_ICONS = {
            "card_reward": "🃏 Card Reward",
            "shop":        "🛒 Shop",
            "other":       "🗺 Other Screen",
            "unknown":     "",
        }
        screen_label = _SCREEN_ICONS.get(screen_type, "")

        if screen_type == "card_reward" and card_names:
            # 显示识别到的卡名（置信度颜色区分）
            parts = []
            for name, conf in zip(card_names, confidences):
                if not name:
                    parts.append("<span style='color:#666'>?</span>")
                elif conf >= 0.8:
                    parts.append(f"<span style='color:#A8D870'>{name}</span>")
                elif conf >= 0.55:
                    parts.append(f"<span style='color:#FFD54F'>{name}</span>")
                else:
                    parts.append(f"<span style='color:#FF7043'>{name}?</span>")
            cards_html = "  /  ".join(parts)
            self._ocr_screen_label.setText(
                f"<span style='color:#ccc'>{screen_label}</span>  {cards_html}"
            )
            self._ocr_screen_label.setTextFormat(Qt.TextFormat.RichText)
            self._ocr_screen_label.setVisible(True)
        elif screen_label:
            self._ocr_screen_label.setText(
                f"<span style='color:#888'>{screen_label}</span>"
            )
            self._ocr_screen_label.setTextFormat(Qt.TextFormat.RichText)
            self._ocr_screen_label.setVisible(True)
        else:
            self._ocr_screen_label.setVisible(False)

        # 更新 OCR 预览面板
        self._update_ocr_preview_panel(screen_type, card_names, confidences, all_reliable)

        # 选卡界面且识别稳定 → 自动填入候选卡并触发评估
        if screen_type == "card_reward" and all_reliable and card_choices:
            self._auto_fill_vision_cards(card_choices)

    def _update_ocr_preview_panel(
        self,
        screen_type: str,
        card_names: list,
        confidences: list,
        all_reliable: bool,
    ) -> None:
        """更新 OCR 预览面板的显示内容"""
        if screen_type != "card_reward":
            self._ocr_preview_panel.setVisible(False)
            return

        self._ocr_preview_panel.setVisible(True)

        # 状态文字
        if all_reliable:
            self._ocr_preview_status.setText("Locked ✓")
            self._ocr_preview_status.setStyleSheet("color:#4CAF50;font-size:11px;")
            self._ocr_hint_label.setText("Recognition stable, candidates auto-filled and evaluated")
            self._ocr_hint_label.setStyleSheet("color:#4A7A40;font-size:11px;padding-top:2px;")
        else:
            self._ocr_preview_status.setText("Recognizing...")
            self._ocr_preview_status.setStyleSheet("color:#FF9800;font-size:11px;")
            self._ocr_hint_label.setText("Waiting for stabilization across multiple frames...")
            self._ocr_hint_label.setStyleSheet("color:#7A6030;font-size:11px;padding-top:2px;")

        # 三张卡名标签
        for i, lbl in enumerate(self._ocr_preview_cards):
            name = card_names[i] if i < len(card_names) else ""
            conf = confidences[i] if i < len(confidences) else 0.0
            if not name:
                lbl.setText(f"Card {i+1}")
                lbl.setStyleSheet(
                    "color: #555; font-size: 11px; "
                    "border: 1px solid #333; border-radius: 4px; "
                    "padding: 2px 6px; background: #1a1a1a;"
                )
            elif conf >= 0.8:
                lbl.setText(name)
                lbl.setStyleSheet(
                    "color: #A8D870; font-size: 11px; font-weight: bold; "
                    "border: 1px solid #4CAF50; border-radius: 4px; "
                    "padding: 2px 6px; background: #0d1a0d;"
                )
            elif conf >= 0.55:
                lbl.setText(name)
                lbl.setStyleSheet(
                    "color: #FFD54F; font-size: 11px; "
                    "border: 1px solid #FF9800; border-radius: 4px; "
                    "padding: 2px 6px; background: #1a1200;"
                )
            else:
                lbl.setText(f"{name}?")
                lbl.setStyleSheet(
                    "color: #FF7043; font-size: 11px; "
                    "border: 1px solid #BF360C; border-radius: 4px; "
                    "padding: 2px 6px; background: #1a0800;"
                )

    def _on_ocr_snapshot(self) -> None:
        """手动触发一次截图识别（独立于后台自动轮询）"""
        self._ocr_btn.setEnabled(False)
        self._ocr_btn.setText("📷 Processing...")
        self._status_label.setText("Manual OCR snapshot in progress...")

        # 在后台线程执行，避免冻结 UI
        worker = _OcrSnapshotWorker(BACKEND_URL)
        worker.result_ready.connect(self._on_ocr_snapshot_result)
        def _restore_btn():
            self._ocr_btn.setEnabled(True)
            self._ocr_btn.setText("📷 OCR Snapshot")
        worker.finished.connect(_restore_btn)
        worker.start()
        # 保持引用避免被 GC
        self._ocr_snapshot_worker = worker

    def _on_ocr_snapshot_result(self, data: dict) -> None:
        """处理手动 OCR 截图的结果"""
        self._on_vision_state_update(data)
        screen_type = data.get("screen_type", "unknown")
        if screen_type == "card_reward":
            self._status_label.setText("OCR Complete: Card Reward Screen")
        elif screen_type == "shop":
            self._status_label.setText("OCR Complete: Shop Screen")
        elif screen_type == "other":
            self._status_label.setText("OCR Complete: Other Screen")
        else:
            self._status_label.setText("OCR Complete: Unknown Screen")

    def _auto_fill_vision_cards(self, card_ids: list) -> None:
        """OCR 识别稳定后，自动填入候选卡到当前 run_state 并触发评估"""
        if not card_ids:
            return
        normalized = [cid.lower() for cid in card_ids]
        # 构造虚拟 card dict 列表（只需 id 字段供评估器使用）
        fake_cards = [{"id": cid} for cid in normalized]
        self._status_label.setText(f"OCR automatically identified {len(normalized)} candidates, evaluating...")
        log.info(f"OCR 自动填入候选卡: {normalized}")
        self._on_evaluate_from_picker(fake_cards)

    def _on_refresh_detect(self) -> None:
        """刷新检测：重新初始化游戏和日志检测"""
        try:
            self._status_label.setText("Re-detecting...")

            # 通过调用配置端点来触发后端重新初始化 GameWatcher
            resp = requests.post(
                f"{BACKEND_URL}/api/config",
                json={},  # 空配置会触发重新检测
                timeout=5,
            )

            if resp.status_code == 200:
                log.info("✓ 已触发重新检测")
                self._status_label.setText("Detecting... Please wait")
                # 延迟1秒后查看指示灯更新
                QTimer.singleShot(1000, lambda: self._status_label.setText("Detection complete"))
            else:
                self._status_label.setText(f"Detection failed: {resp.status_code}")

        except Exception as e:
            log.error(f"重新检测失败: {e}")
            self._status_label.setText(f"Error: {e}")

    def _on_settings(self) -> None:
        """打开设置对话框"""
        dialog = PathSettingsDialog(self, BACKEND_URL)
        dialog.exec()
        # 重新读取语言配置，如有变化则刷新卡牌
        try:
            from scripts.config_manager import get_language
            new_lang = get_language()
        except Exception:
            new_lang = "en"
        if new_lang != self._language:
            self._language = new_lang
            # 强制重新加载（重置 _current_character 使 _fetch 不被跳过）
            char = self._current_character
            self._current_character = ""
            if char:
                self._fetch_cards_for_character(char)

    # ------------------------------------------------------------------
    # 卡牌选择器
    # ------------------------------------------------------------------

    def _build_side_drawer(self) -> QWidget:
        """嵌入式侧边手动选牌抽屉"""
        drawer = QWidget()
        drawer.setObjectName("SideDrawerPanel")
        drawer.setMinimumWidth(340)

        layout = QVBoxLayout(drawer)
        layout.setContentsMargins(6, 8, 6, 8)
        layout.setSpacing(4)

        title = QLabel("Manual Selection")
        title.setObjectName("DrawerTitle")
        layout.addWidget(title)

        self._card_picker = CardPickerPanel()
        self._card_picker.selection_changed.connect(self._on_card_selection_changed)
        layout.addWidget(self._card_picker, 1)

        self._selection_tray = SelectionTrayWidget()
        self._selection_tray.evaluate_requested.connect(self._on_evaluate_from_picker)
        layout.addWidget(self._selection_tray, 0)

        return drawer

    def _auto_fit_height(self) -> None:
        """根据内容自适应窗口高度（宽度保持不变）"""
        hint = self.sizeHint()
        new_h = max(hint.height(), self.minimumHeight())
        self.resize(self.width(), new_h)

    def _toggle_side_drawer(self) -> None:
        self._drawer_open = not self._drawer_open
        self._side_drawer.setVisible(self._drawer_open)
        self._drawer_toggle_btn.setText("▶" if self._drawer_open else "◀")
        drawer_w = self._side_drawer.minimumWidth()
        delta = drawer_w if self._drawer_open else -drawer_w
        self.resize(self.width() + delta, self.height())

    def _fetch_cards_for_character(self, character: str) -> None:
        if not character:
            return
        self._current_character = character

        if self._cards_fetch_worker and self._cards_fetch_worker.isRunning():
            self._cards_fetch_worker.terminate()

        self._card_picker.set_language(self._language)
        self._card_picker.clear_cards()
        self._selection_tray.update_selection([])
        self._status_label.setText(f"Loading cards for {character.upper()}...")

        self._cards_fetch_worker = CardsFetchWorker(character)
        self._cards_fetch_worker.cards_ready.connect(self._on_cards_fetched)
        self._cards_fetch_worker.error_occurred.connect(
            lambda e: self._status_label.setText(f"Failed to load cards: {e}")
        )
        self._cards_fetch_worker.start()

    def _on_cards_fetched(self, cards: list[dict]) -> None:
        playable_types = {"attack", "skill", "power"}
        playable = [c for c in cards if c.get("card_type", "").lower() in playable_types]
        self._card_picker.populate(playable)
        self._status_label.setText(f"Loaded {len(playable)} cards. Please select candidates.")

    def _on_card_selection_changed(self, selected_cards: list[dict], display_names: list[str]) -> None:
        self._selection_tray.update_selection(selected_cards, display_names)

    def _on_evaluate_from_picker(self, selected_cards: list[dict]) -> None:
        if not selected_cards:
            self._status_label.setText("Please select candidate cards first")
            return

        card_ids = [c["id"].lower() for c in selected_cards]
        run_state = self._run_state.copy() if self._run_state else {}
        run_state.setdefault("character", "silent")
        run_state.setdefault("floor", 1)
        run_state.setdefault("hp", 70)
        run_state.setdefault("max_hp", 70)
        run_state.setdefault("gold", 0)
        run_state.setdefault("ascension", 0)
        run_state.setdefault("deck", [])
        run_state.setdefault("relics", [])
        run_state["card_choices"] = card_ids

        # 规范化前缀
        run_state["character"] = run_state["character"].replace("CHARACTER.", "").lower()
        run_state["deck"] = [
            c.replace("CARD.", "").lower() if isinstance(c, str) else c
            for c in run_state["deck"]
        ]
        run_state["relics"] = [
            {"id": r.replace("RELIC.", "").lower(), "name": r.replace("RELIC.", ""), "tags": []}
            if isinstance(r, str) else r
            for r in run_state["relics"]
        ]

        self._status_label.setText("Evaluating...")
        self._selection_tray._evaluate_btn.setEnabled(False)

        self._worker = EvaluateWorker(run_state)
        self._worker.result_ready.connect(self._on_result)
        self._worker.error_occurred.connect(self._on_error)
        self._worker.finished.connect(
            lambda: self._selection_tray._evaluate_btn.setEnabled(True)
        )
        self._worker.start()

    def _on_refresh(self) -> None:
        """
        触发评估请求。
        使用 GameWatcher 推送的实时游戏状态。
        """
        # 确保有完整的 run_state 数据
        run_state = getattr(self, '_run_state', {
            "character": "silent",
            "floor": 8,
            "hp": 55,
            "max_hp": 70,
            "gold": 99,
            "deck": ["blade_dance", "cloak_and_dagger"],
            "relics": [],
            "card_choices": ["catalyst", "ninjutsu", "reflex"],
        })

        # 确保必需字段存在
        if "character" not in run_state:
            run_state["character"] = "silent"
        if "floor" not in run_state:
            run_state["floor"] = 1
        if "hp" not in run_state or run_state["hp"] == 0:
            run_state["hp"] = 70
        if "max_hp" not in run_state or run_state["max_hp"] == 0:
            run_state["max_hp"] = 70
        if "gold" not in run_state:
            run_state["gold"] = 0
        if "ascension" not in run_state:
            run_state["ascension"] = 0
        if "deck" not in run_state:
            run_state["deck"] = []
        if "card_choices" not in run_state or not run_state["card_choices"]:
            # 无法进行评估，因为没有选卡池
            self._status_label.setText("Waiting for card data... (Needs active game or log files)")
            return

        # 规范化字段（去除游戏前缀）
        run_state["character"] = run_state.get("character", "silent").replace("CHARACTER.", "").lower()
        run_state["deck"] = [c.replace("CARD.", "").lower() if isinstance(c, str) else c for c in run_state.get("deck", [])]
        raw_relics = run_state.get("relics", [])
        run_state["relics"] = [
            {"id": r.replace("RELIC.", "").lower(), "name": r.replace("RELIC.", ""), "tags": []}
            if isinstance(r, str) else r
            for r in raw_relics
        ]
        self._status_label.setText("Evaluating...")

        self._worker = EvaluateWorker(run_state)
        self._worker.result_ready.connect(self._on_result)
        self._worker.error_occurred.connect(self._on_error)
        self._worker.start()

    def _on_result(self, data: dict) -> None:
        results = data.get("results", [])
        archetypes = data.get("detected_archetypes", [])
        verdict = data.get("verdict")
        
        # V2: Filter out __SKIP__ from results (it's now in verdict)
        results = [r for r in results if r.get("card_id") != "__SKIP__"]
        
        self._render_results(results, verdict)

        if not results:
            self._status_label.setText("❌ No matching cards found")
            self._archetype_label.setVisible(False)
        else:
            if archetypes:
                arch_text = ", ".join(archetypes)
                self._archetype_label.setText(f"⚔ Archetypes: {arch_text}")
                self._archetype_label.setVisible(True)
            else:
                self._archetype_label.setVisible(False)
            self._status_label.setText("Evaluation complete")

    def _on_error(self, message: str) -> None:
        self._status_label.setText(f"Error: {message}")

    def _render_results(self, results: list[dict], verdict: dict | None = None) -> None:
        """清空列表并重新渲染评估结果"""
        # 移除旧 widget（保留 stretch）
        while self._list_layout.count() > 1:
            item = self._list_layout.takeAt(0)
            if item.widget():
                item.widget().deleteLater()

        # Render card results
        for result in results:
            widget = CardResultWidget(result, language=self._language)
            self._list_layout.insertWidget(self._list_layout.count() - 1, widget)
        
        # V2: Add verdict widget at the bottom (before stretch)
        if verdict:
            verdict_widget = VerdictWidget(verdict)
            self._list_layout.insertWidget(self._list_layout.count() - 1, verdict_widget)

    def _show_placeholder(self) -> None:
        """初始占位内容"""
        placeholder = QLabel("Click 'Detect' to load evaluation results")
        placeholder.setObjectName("Placeholder")
        placeholder.setAlignment(Qt.AlignmentFlag.AlignCenter)
        self._list_layout.insertWidget(0, placeholder)

    # ------------------------------------------------------------------
    # 样式加载
    # ------------------------------------------------------------------

    def _load_stylesheet(self) -> None:
        qss_path = get_app_root() / "frontend" / "styles.qss"
        if qss_path.exists():
            with open(qss_path, encoding="utf-8") as f:
                self.setStyleSheet(f.read())

    # ------------------------------------------------------------------
    # 拖拽移动（无边框窗口）
    # ------------------------------------------------------------------

    def _title_mouse_press(self, event) -> None:
        if event.button() == Qt.MouseButton.LeftButton:
            self._drag_pos = event.globalPosition().toPoint()

    def _title_mouse_move(self, event) -> None:
        if self._drag_pos is not None and event.buttons() == Qt.MouseButton.LeftButton:
            delta = event.globalPosition().toPoint() - self._drag_pos
            self.move(self.pos() + delta)
            self._drag_pos = event.globalPosition().toPoint()

    def _title_mouse_release(self, event) -> None:
        self._drag_pos = None


# ---------------------------------------------------------------------------
# 独立运行入口（调试用）
# ---------------------------------------------------------------------------

def run_ui() -> None:
    app = QApplication.instance() or QApplication(sys.argv)
    window = CardAdviserWindow()
    window.show()
    sys.exit(app.exec())

if __name__ == "__main__":
    run_ui()
