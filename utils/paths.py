"""
utils/paths.py
应用根目录解析工具。

在 PyInstaller 打包（onedir）模式下，__file__ 指向打包目录内部的 .pyc，
而数据文件（data/、frontend/styles.qss 等）被放在 EXE 旁边的同级目录中。
此工具统一解析"应用根目录"，使路径在开发模式和 EXE 模式下均正确。
"""

from __future__ import annotations

import sys
from pathlib import Path


def get_app_root() -> Path:
    """
    返回应用根目录：
    - EXE 模式（PyInstaller frozen）：返回 exe 文件所在目录
    - 开发模式：返回项目根目录（utils/ 的上一级）
    """
    if getattr(sys, "frozen", False):
        return Path(sys.executable).parent
    return Path(__file__).parent.parent
