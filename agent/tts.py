"""§30 TTS 语音合成（视频配音刚需）[必选]

选型：Edge TTS（微软，免费）主力 + Fish Audio 备选。
Edge TTS 无需 Key，走 wss 协议；这里用最稳的 HTTP 降级思路——
实际走 Edge 的在线接口，失败时明确报错（不静默）。
"""
from __future__ import annotations

import logging

log = logging.getLogger("tts")

import os
import re
import tempfile
from dataclasses import dataclass


@dataclass
class TTSRequest:
    text: str                           # 文案
    voice: str = "zh-CN-YunxiNeural"    # 音色 ID
    rate: str = "+0%"                   # 语速
    output: str = ""                    # 输出 mp3 路径（空则自动）


class TTSManager:
    """Edge TTS 合成。"""

    # 常用音色
    VOICES = {
        "zh-CN-YunxiNeural": "云希（男，推荐）",
        "zh-CN-XiaoxiaoNeural": "晓晓（女）",
        "zh-CN-YunjianNeural": "云健（男）",
        "zh-CN-XiaoyiNeural": "晓伊（女）",
        "zh-CN-liaoning-XiaobeiNeural": "晓北（女，辽宁）",
        "en-US-AriaNeural": "Aria（女，英文）",
    }

    def synthesize(self, req: TTSRequest) -> str:
        """合成一段，返回音频文件路径。"""
        out = req.output or os.path.join(
            tempfile.gettempdir(), f"tts_{os.getpid()}.mp3")
        log.info(f"[tts] 合成开始：{len(req.text)} 字，音色={req.voice}")
        self._edge_synthesize(req.text, req.voice, req.rate, out)
        log.info(f"[tts] 合成完成：{out}")
        return out

    def synthesize_long(self, text: str, voice: str = "zh-CN-YunxiNeural",
                        output: str = "", max_chars: int = 500) -> dict:
        """长文案分段合成再拼接：
        1. 按句号/段落切分（每段 < max_chars）
        2. 逐段合成
        3. ffmpeg concat 拼成一个 mp3
        4. 返回 {path, duration_sec}
        """
        import subprocess
        segments = self._split(text, max_chars)
        tmpdir = tempfile.mkdtemp(prefix="tts_long_")
        parts = []
        for i, seg in enumerate(segments):
            p = os.path.join(tmpdir, f"part_{i:03d}.mp3")
            self._edge_synthesize(seg, voice, "+0%", p)
            parts.append(p)
        out = output or os.path.join(tempfile.gettempdir(), "tts_long.mp3")
        if len(parts) == 1:
            import shutil
            shutil.copy(parts[0], out)
        else:
            lst = os.path.join(tmpdir, "list.txt")
            with open(lst, "w") as f:
                for p in parts:
                    f.write(f"file '{p}'\n")
            subprocess.run(["ffmpeg", "-y", "-f", "concat", "-safe", "0",
                            "-i", lst, "-c", "copy", out],
                           check=True, capture_output=True)
        dur = self._probe_duration(out)
        return {"path": out, "duration_sec": dur, "segments": len(parts)}

    # ---- 内部 ----
    def _split(self, text: str, max_chars: int) -> list[str]:
        """按句切分，保证每段 < max_chars。"""
        sents = re.split(r"(?<=[。！？!?])", text)
        out, buf = [], ""
        for s in sents:
            s = s.strip()
            if not s:
                continue
            if len(buf) + len(s) <= max_chars:
                buf += s
            else:
                if buf:
                    out.append(buf)
                buf = s
        if buf:
            out.append(buf)
        return out or [text]

    def _edge_synthesize(self, text: str, voice: str, rate: str, out: str):
        """调 Edge TTS（非官方接口，失败明确报错）。"""
        # 用 edge-tts 的 HTTP 兼容思路：实际走 wss 需要 websocket 库，
        # 这里用最简方案——尝试 import edge_tts，有则用，无则报错指引安装。
        try:
            import edge_tts
        except ImportError:
            raise RuntimeError(
                "TTS 需要 edge-tts 库：pip install edge-tts（微软免费接口，无需 Key）")
        import asyncio

        async def _run():
            comm = edge_tts.Communicate(text, voice, rate=rate)
            await comm.save(out)

        asyncio.run(_run())
        if not os.path.exists(out) or os.path.getsize(out) == 0:
            raise RuntimeError("TTS 合成失败：输出文件为空")

    def _probe_duration(self, path: str) -> float:
        import subprocess, json
        try:
            r = subprocess.run(
                ["ffprobe", "-v", "quiet", "-print_format", "json",
                 "-show_format", path],
                capture_output=True, text=True, timeout=15)
            return float(json.loads(r.stdout)["format"]["duration"])
        except Exception:
            return 0.0


def make_tts_tools(manager: "TTSManager"):
    """构建 TTS 工具。"""
    from agent.tools_registry import ToolDefinition

    def _synth(args: dict) -> str:
        from agent.tts import TTSRequest
        try:
            p = manager.synthesize(TTSRequest(
                text=args["text"],
                voice=args.get("voice", "zh-CN-YunxiNeural"),
                rate=args.get("rate", "+0%")))
            return f"[TTS] 已合成：{p}"
        except Exception as e:
            return f"[工具执行失败] {e}"

    return [
        ToolDefinition(
            name="tts_synthesize",
            description="文本转语音（Edge TTS 免费）。视频配音用。",
            parameters={"type": "object",
                         "properties": {
                             "text": {"type": "string", "description": "文案"},
                             "voice": {"type": "string",
                                        "description": "音色，如 zh-CN-YunxiNeural"},
                             "rate": {"type": "string", "description": "语速，如 +0%"}},
                         "required": ["text"]},
            func=_synth,
        ),
    ]
