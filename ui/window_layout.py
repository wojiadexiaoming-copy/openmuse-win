"""Window split layout calculator for desktop multi-window coordination."""
from __future__ import annotations

import ctypes
from ctypes import wintypes
import sys
from typing import TypedDict


class WindowRect(TypedDict):
    x: int
    y: int
    width: int
    height: int


class SplitLayout(TypedDict):
    browser: WindowRect
    ui: WindowRect
    work_area: dict[str, int]
    browser_ratio: float


def get_work_area() -> dict[str, int]:
    """获取主显示器的可用工作区（已扣除任务栏）。"""
    if sys.platform == "win32":
        try:
            class RECT(ctypes.Structure):
                _fields_ = [
                    ("left", wintypes.LONG),
                    ("top", wintypes.LONG),
                    ("right", wintypes.LONG),
                    ("bottom", wintypes.LONG),
                ]

            rect = RECT()
            # SPI_GETWORKAREA = 0x0030
            ok = ctypes.windll.user32.SystemParametersInfoW(0x0030, 0, ctypes.byref(rect), 0)
            if ok and (rect.right > rect.left) and (rect.bottom > rect.top):
                return {
                    "left": int(rect.left),
                    "top": int(rect.top),
                    "right": int(rect.right),
                    "bottom": int(rect.bottom),
                }
        except Exception:
            pass

    # 非 Windows 或获取失败时的默认安全回退
    return {"left": 0, "top": 0, "right": 1920, "bottom": 1040}


def get_split_layout(browser_ratio: float = 0.80, work_area: dict[str, int] | None = None) -> SplitLayout:
    """计算左侧浏览器与右侧 AlterEgo UI 窗口的并排坐标与尺寸。

    参数:
        browser_ratio: 浏览器占据屏幕宽度的比例 (默认 0.80，即 80%)
        work_area: 可选自定义工作区，默认自动获取系统工作区

    返回:
        SplitLayout 字典，包含 browser、ui 的 (x, y, width, height) 及工作区信息
    """
    if work_area is None:
        work_area = get_work_area()

    left = int(work_area.get("left", 0))
    top = int(work_area.get("top", 0))
    right = int(work_area.get("right", 1920))
    bottom = int(work_area.get("bottom", 1040))

    total_width = max(640, right - left)
    total_height = max(480, bottom - top)

    # 边界保护：比例限制在 0.40 ~ 0.85 之间
    ratio = max(0.40, min(0.85, float(browser_ratio)))

    browser_width = int(total_width * ratio)
    ui_width = total_width - browser_width

    browser_rect: WindowRect = {
        "x": left,
        "y": top,
        "width": browser_width,
        "height": total_height,
    }

    ui_rect: WindowRect = {
        "x": left + browser_width,
        "y": top,
        "width": ui_width,
        "height": total_height,
    }

    return {
        "browser": browser_rect,
        "ui": ui_rect,
        "work_area": {"left": left, "top": top, "right": right, "bottom": bottom},
        "browser_ratio": ratio,
    }
