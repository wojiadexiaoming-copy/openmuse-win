"""屏幕录制管理（spec §20）。

支持 Recordly 全局快捷键驱动与 ffmpeg gdigrab 降级裸录。
录制文件统一输出至 recordings/ 目录。
"""
from __future__ import annotations

import datetime
import logging
from pathlib import Path
import shutil
import subprocess
import time
from typing import Any

log = logging.getLogger("alterego.recorder")


def is_process_running(proc_name: str) -> bool:
    """检查进程是否正在运行。"""
    try:
        out = subprocess.check_output(
            ["tasklist", "/FI", f"IMAGENAME eq {proc_name}"],
            text=True,
            timeout=3.0,
        )
        return proc_name.lower() in out.lower()
    except Exception:
        return False


class ScreenRecorder:
    """屏幕录制管理器（Recordly 快捷键 / ffmpeg gdigrab / mock 降级）。"""

    HOTKEY_START = ("ctrl", "shift", "r")
    HOTKEY_STOP = ("ctrl", "shift", "r")

    def __init__(
        self,
        recordings_dir: str | Path = "recordings",
        force_backend: str | None = None,
    ):
        self.recordings_dir = Path(recordings_dir)
        self.recordings_dir.mkdir(parents=True, exist_ok=True)
        self.force_backend = force_backend
        self.name: str = ""
        self.is_recording: bool = False
        self.start_time: float = 0.0
        self.video_path: Path | None = None
        self._ffmpeg_proc: subprocess.Popen | None = None
        self._backend: str = "mock"

    def _detect_backend(self) -> str:
        if self.force_backend:
            return self.force_backend

        # 1. 优先检查 Recordly
        recordly_path = Path("C:/Program Files/Recordly/Recordly.exe")
        if recordly_path.exists() or is_process_running("Recordly.exe"):
            return "recordly"

        # 2. 检查 ffmpeg 是否可用
        if shutil.which("ffmpeg"):
            return "ffmpeg"

        return "mock"

    def start(self, name: str) -> str:
        """开始录屏。"""
        if self.is_recording and self.video_path:
            return str(self.video_path)

        ts = datetime.datetime.now().strftime("%Y%m%d_%H%M%S")
        self.name = f"{name}_{ts}"
        self.video_path = self.recordings_dir / f"{self.name}.mp4"
        self.start_time = time.time()
        self._backend = self._detect_backend()

        if self._backend == "recordly":
            try:
                import pyautogui
                if not is_process_running("Recordly.exe"):
                    p = Path("C:/Program Files/Recordly/Recordly.exe")
                    if p.exists():
                        subprocess.Popen([str(p)])
                        time.sleep(2.0)
                pyautogui.hotkey(*self.HOTKEY_START)
                log.info(f"[recorder] Recordly 快捷键已启动录屏: {self.name}")
            except Exception as e:
                log.warning(f"[recorder] Recordly 热键启动失败，降级为 mock: {e}")
                self._backend = "mock"
        elif self._backend == "ffmpeg":
            try:
                cmd = [
                    "ffmpeg",
                    "-y",
                    "-f", "gdigrab",
                    "-framerate", "30",
                    "-i", "desktop",
                    "-c:v", "libx264",
                    "-preset", "ultrafast",
                    "-pix_fmt", "yuv420p",
                    str(self.video_path),
                ]
                self._ffmpeg_proc = subprocess.Popen(
                    cmd,
                    stdin=subprocess.PIPE,
                    stdout=subprocess.DEVNULL,
                    stderr=subprocess.DEVNULL,
                )
                log.info(f"[recorder] ffmpeg 已启动桌面录屏: {self.name}")
            except Exception as e:
                log.warning(f"[recorder] ffmpeg 启动录制失败，降级为 mock: {e}")
                self._backend = "mock"

        if self._backend == "mock":
            # 生成测试占位 mp4 文件
            self.video_path.write_bytes(b"\x00\x00\x00 ftypmp42\x00\x00\x00\x00isommp42")

        self.is_recording = True
        return str(self.video_path)

    def stop(self) -> str:
        """停止录屏并确保视频落盘。"""
        if not self.is_recording:
            return str(self.video_path) if self.video_path else ""

        if self._backend == "recordly":
            try:
                import pyautogui
                pyautogui.hotkey(*self.HOTKEY_STOP)
                log.info(f"[recorder] Recordly 快捷键已停止录屏: {self.name}")
            except Exception as e:
                log.warning(f"[recorder] Recordly 停止失败: {e}")
        elif self._backend == "ffmpeg" and self._ffmpeg_proc:
            try:
                # 注意：terminate() 而不是 kill()，以便让 ffmpeg 正常写入 mp4 文件头（spec §20.2）
                self._ffmpeg_proc.terminate()
                self._ffmpeg_proc.wait(timeout=10)
            except Exception as e:
                log.warning(f"[recorder] 终止 ffmpeg 异常: {e}")
                try:
                    self._ffmpeg_proc.kill()
                except Exception:
                    pass
            self._ffmpeg_proc = None

        self.is_recording = False
        log.info(f"[recorder] 录屏结束，产物落盘: {self.video_path}")
        return str(self.video_path)

    def status(self) -> dict[str, Any]:
        """获取当前录屏状态。"""
        elapsed = time.time() - self.start_time if self.is_recording else 0.0
        return {
            "name": self.name,
            "is_recording": self.is_recording,
            "backend": self._backend,
            "elapsed_sec": round(elapsed, 1),
            "video_path": str(self.video_path) if self.video_path else "",
        }
