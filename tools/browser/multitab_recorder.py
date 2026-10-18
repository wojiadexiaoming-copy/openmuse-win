"""多标签切换连续录屏与智能运镜追踪控制器（Spec §15.7 / §20）。

落地文档规范：
1. docs/多标签切换 + 跨 Tab 连续录屏.txt
2. docs/智能自动缩放与运镜.txt
3. docs/建议作为 §15.7 浏览器视觉遮罩与硬件输入锁联动方案.txt

功能：
1. MultiTabBrowserController:
   - Context 级别自动注入蓝紫流光遮罩与虚拟鼠标层；
   - 监听新标签页打开与切换事件，镜头与输入锁无缝续接；
   - 基于 CDP Page.startScreencast + ffmpeg 管道的一镜到底连续录屏（跨 Tab 不中断，单一 MP4 输出）。
2. SmartZoomDirector:
   - 记录 AI 操作的关键坐标与时间戳（点击、输入、切标签）；
   - 生成 actions_meta.json 运镜元数据，驱动平滑推拉运镜（Pan & Zoom）。
"""
from __future__ import annotations

import base64
import json
import logging
import os
import shutil
import subprocess
import threading
import time
from pathlib import Path
from typing import Any, Optional

from .input_guard import INJECT_OVERLAY_JS

logger = logging.getLogger("alterego.browser.multitab_recorder")


class SmartZoomDirector:
    """收集 AI 操作轨迹与视口焦点，生成智能运镜脚本（Smart Zoom & Motion Metadata）。"""

    def __init__(self, start_time: Optional[float] = None):
        self.start_time = start_time or time.time()
        self.events: list[dict[str, Any]] = []
        self._lock = threading.Lock()

    def record_action(
        self,
        action_type: str,
        x: int = 0,
        y: int = 0,
        target_desc: str = "",
        zoom_level: float = 1.6,
    ):
        """记录一次关键交互动作与位置。"""
        elapsed = max(0.0, time.time() - self.start_time)
        with self._lock:
            self.events.append({
                "timestamp": round(elapsed, 2),
                "type": action_type,
                "x": int(x),
                "y": int(y),
                "zoom_level": float(zoom_level),
                "description": target_desc[:80] if target_desc else action_type,
            })

    def save_meta(self, path: str | Path) -> str:
        """保存运镜元数据到本地 json 文件。"""
        p = Path(path)
        p.parent.mkdir(parents=True, exist_ok=True)
        with self._lock:
            data = list(self.events)
        with open(p, "w", encoding="utf-8") as f:
            json.dump(data, f, ensure_ascii=False, indent=2)
        return str(p)


class MultiTabBrowserController:
    """多标签页（Multi-Tab）自动追踪与一镜到底连续录屏控制器。"""

    def __init__(
        self,
        context: Any,
        output_mp4: str = "",
        fps: int = 25,
        director: Optional[SmartZoomDirector] = None,
    ):
        self.context = context
        self.fps = fps
        self.output_mp4 = output_mp4 or self._default_video_path()
        self.director = director or SmartZoomDirector()

        self.active_page: Any = None
        self.mode = "AGENT_ACTIVE"
        self._recording = False
        self._ffmpeg_proc: Optional[subprocess.Popen] = None
        self._current_cdp: Any = None
        self._lock = threading.Lock()

        # 核心 1：在 Context 级别注入初始化脚本
        # 无论打开多少个 target="_blank" 新标签页，新页面一出生即挂载流光遮罩与虚拟鼠标
        try:
            self.context.add_init_script(f"""
                window.addEventListener('DOMContentLoaded', () => {{
                    try {{
                        if (window.__alterego_mode && window.__alterego_mode !== 'IDLE') {{
                            ({INJECT_OVERLAY_JS})({{ mode: window.__alterego_mode, reason: '' }});
                        }} else {{
                            ({INJECT_OVERLAY_JS})({{ mode: 'AGENT_ACTIVE', reason: '' }});
                        }}
                    }} catch (e) {{}}
                }});
            """)
        except Exception as e:
            logger.debug("Failed to add context init script: %s", e)

        # 核心 2：监听新标签页打开事件
        try:
            self.context.on("page", self._on_new_tab_opened)
        except Exception as e:
            logger.debug("Failed to attach context page listener: %s", e)

    @staticmethod
    def _default_video_path() -> str:
        ts = int(time.time())
        target_dir = Path("artifacts/videos")
        target_dir.mkdir(parents=True, exist_ok=True)
        return str(target_dir / f"session_{ts}.mp4")

    def _on_new_tab_opened(self, new_page: Any):
        """当 AI 点击链接弹出新标签页时自动触发。"""
        try:
            logger.info("[multitab] 检测到新标签页打开，自动同步遮罩与运镜焦点")
            self.director.record_action("switch_tab", 0, 0, "弹出新标签页")
            new_page.wait_for_load_state("domcontentloaded", timeout=4000)
        except Exception:
            pass
        self.switch_to_tab(new_page)

    def start_recording(self, first_page: Any) -> bool:
        """启动 ffmpeg 实时视频录制管道，并绑定首个标签页。"""
        if not shutil.which("ffmpeg"):
            logger.warning("[multitab] 系统未检测到 ffmpeg，跳过视频录制")
            return False

        Path(self.output_mp4).parent.mkdir(parents=True, exist_ok=True)
        try:
            self._ffmpeg_proc = subprocess.Popen(
                [
                    "ffmpeg", "-y",
                    "-f", "image2pipe",
                    "-vcodec", "mjpeg",
                    "-r", str(self.fps),
                    "-i", "-",
                    "-c:v", "libx264",
                    "-preset", "ultrafast",
                    "-pix_fmt", "yuv420p",
                    self.output_mp4,
                ],
                stdin=subprocess.PIPE,
                stdout=subprocess.DEVNULL,
                stderr=subprocess.DEVNULL,
            )
            self._recording = True
            logger.info(f"[multitab] 跨 Tab 连续录屏管道已启动: {self.output_mp4}")
            self.switch_to_tab(first_page)
            return True
        except Exception as e:
            logger.warning(f"[multitab] 启动录屏失败: {e}")
            self._recording = False
            return False

    def switch_to_tab(self, target_page: Any):
        """将焦点与画面流切换至指定标签页。"""
        if not target_page:
            return
        with self._lock:
            # 1. 停止旧 Tab 的 Screencast
            if self._current_cdp:
                try:
                    self._current_cdp.send("Page.stopScreencast")
                except Exception:
                    pass

            self.active_page = target_page
            try:
                target_page.bring_to_front()
            except Exception:
                pass

            # 2. 建立新 Tab 的 CDP 会话并同步遮罩与输入锁
            try:
                cdp = self.context.new_cdp_session(target_page)
                self._current_cdp = cdp
                cdp.send("Emulation.setIgnoreInputEvents", {"ignore": False})
            except Exception:
                cdp = None
                self._current_cdp = None

            try:
                target_page.evaluate(INJECT_OVERLAY_JS, {"mode": self.mode, "reason": ""})
            except Exception:
                pass

            # 3. 开启新 Tab 的 Screencast 灌入 ffmpeg 管道
            if self._recording and cdp is not None:
                try:
                    cdp.on("Page.screencastFrame", lambda params: self._on_frame(cdp, params))
                    cdp.send("Page.startScreencast", {
                        "format": "jpeg",
                        "quality": 85,
                        "maxWidth": 1920,
                        "maxHeight": 1080,
                        "everyNthFrame": 1,
                    })
                except Exception as e:
                    logger.debug(f"[multitab] 启动 Tab screencast 失败: {e}")

    def _on_frame(self, cdp: Any, params: dict):
        """收到画面帧写入 ffmpeg 管道并确认下一帧。"""
        if not self._recording or not self._ffmpeg_proc or not self._ffmpeg_proc.stdin:
            return
        try:
            frame_bytes = base64.b64decode(params["data"])
            self._ffmpeg_proc.stdin.write(frame_bytes)
            self._ffmpeg_proc.stdin.flush()
            cdp.send("Page.screencastFrameAck", {"sessionId": params["sessionId"]})
        except Exception:
            pass

    def stop_recording(self) -> Optional[str]:
        """停止录屏并保存成品视频与运镜元数据。"""
        with self._lock:
            self._recording = False
            if self._current_cdp:
                try:
                    self._current_cdp.send("Page.stopScreencast")
                except Exception:
                    pass
                self._current_cdp = None

            if self._ffmpeg_proc:
                try:
                    if self._ffmpeg_proc.stdin:
                        self._ffmpeg_proc.stdin.close()
                    self._ffmpeg_proc.wait(timeout=8)
                except Exception:
                    try:
                        self._ffmpeg_proc.kill()
                    except Exception:
                        pass
                self._ffmpeg_proc = None

        meta_path = Path(self.output_mp4).with_suffix(".meta.json")
        self.director.save_meta(meta_path)
        logger.info(f"[multitab] 录屏结束，产物: {self.output_mp4} (元数据: {meta_path})")
        return self.output_mp4 if os.path.exists(self.output_mp4) else None
