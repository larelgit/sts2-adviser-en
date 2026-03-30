"""
utils/paths.py
应用根目录解析工具。

PyInstaller 6.x onedir 模式将所有数据文件放在 _internal/ 子目录中，
而非直接放在 EXE 旁边。此工具统一解析数据文件根目录，使路径在开发
模式和 EXE 模式下均正确。
"""

from __future__ import annotations

import sys
from pathlib import Path


def get_app_root() -> Path:
    """
    返回数据文件根目录：
    - EXE 模式（PyInstaller 6.x frozen）：返回 _internal/ 目录
      （data/、frontend/、utils/ 均在此目录下）
    - 开发模式：返回项目根目录（utils/ 的上一级）
    """
    if getattr(sys, "frozen", False):
        # PyInstaller 6.x: sys._MEIPASS 指向 _internal/ 目录
        meipass = getattr(sys, "_MEIPASS", None)
        if meipass:
            return Path(meipass)
        # 兜底：旧版 PyInstaller，数据在 exe 旁边
        return Path(sys.executable).parent
    return Path(__file__).parent.parent
