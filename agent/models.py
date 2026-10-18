"""§3.1 数据结构：本文件是 Message / ToolCall / ImagePart 的唯一定义。

其他模块只许 import，不许重定义（见 docs/spec.md 附3.5）。
构造一律用关键字参数，禁止位置参数。
"""
from __future__ import annotations

from dataclasses import dataclass, field


from pathlib import Path


@dataclass
class ImagePart:
    """截图/图片附件（spec §24）。支持 bytes 或 base64，最长边 1568px 预处理。"""
    data: bytes = b""
    data_base64: str = ""
    mime: str = "image/jpeg"
    mime_type: str = "image/jpeg"

    def __post_init__(self):
        if self.mime and not self.mime_type:
            self.mime_type = self.mime
        elif self.mime_type and not self.mime:
            self.mime = self.mime_type
        if self.data and not self.data_base64:
            import base64
            self.data_base64 = base64.b64encode(self.data).decode("ascii")
        elif self.data_base64 and not self.data:
            import base64
            try:
                self.data = base64.b64decode(self.data_base64)
            except Exception:
                pass

    @classmethod
    def from_bytes(cls, png_or_jpeg_bytes: bytes, max_dim: int = 1568, quality: int = 80) -> ImagePart:
        """从二进制图片构造，自动压缩为 JPEG 80，最长边 <= 1568px（spec §24.3）。"""
        import io
        import base64
        from PIL import Image

        try:
            img = Image.open(io.BytesIO(png_or_jpeg_bytes))
            w, h = img.size
            longest = max(w, h)
            if longest > max_dim:
                scale = max_dim / longest
                img = img.resize((int(w * scale), int(h * scale)), Image.LANCZOS)
            buf = io.BytesIO()
            img.convert("RGB").save(buf, "JPEG", quality=quality)
            data = buf.getvalue()
            b64 = base64.b64encode(data).decode("ascii")
            return cls(data=data, data_base64=b64, mime="image/jpeg", mime_type="image/jpeg")
        except Exception:
            import base64
            b64 = base64.b64encode(png_or_jpeg_bytes).decode("ascii")
            return cls(data=png_or_jpeg_bytes, data_base64=b64, mime="image/jpeg", mime_type="image/jpeg")

    @classmethod
    def from_file(cls, path: str | Path) -> ImagePart:
        p = Path(path)
        return cls.from_bytes(p.read_bytes())



@dataclass
class ToolCall:
    id: str
    name: str
    arguments: dict = field(default_factory=dict)
    extra_content: dict | None = None


@dataclass
class Message:
    role: str                      # "system" | "user" | "assistant" | "tool"
    content: str = ""
    tool_calls: list[ToolCall] = field(default_factory=list)
    tool_call_id: str = ""         # role="tool" 时回填对应的 ToolCall.id
    name: str = ""                 # role="tool" 时为工具名
    images: list[ImagePart] = field(default_factory=list)
    created_at: float = 0.0
    id: str = ""
    metadata: dict = field(default_factory=dict)
    # §36.10 快捷选项芯片：助手输出草稿求确认时，前端在气泡下方渲染为胶囊按钮
    # 例如：["就按这个发", "我改一下再发"]；点击等价于用户发送该句话
    suggested_actions: list[str] = field(default_factory=list)

    def __post_init__(self):
        import time
        import uuid
        assert self.role in ("system", "user", "assistant", "tool"), \
            f"非法 role: {self.role}"
        if not self.created_at:
            self.created_at = time.time()
        if not self.id:
            self.id = f"msg_{uuid.uuid4().hex[:10]}"



def terminal_tool_control(message: Message) -> dict | None:
    """Accept only explicit, typed control metadata from an actual tool result."""
    if not isinstance(message, Message):
        return None
    control = message.metadata.get("tool_control")
    if not isinstance(control, dict) or control.get("retryable") is not False:
        return None
    import re
    reason = control.get("reason")
    if not isinstance(reason, str) or not re.fullmatch(r"[a-z][a-z0-9_]{0,79}", reason):
        reason = "nonretryable_tool_failure"
    return {"retryable": False, "requires_user": control.get("requires_user") is True,
            "reason": reason}


def tool_result_is_error(content: str | Message) -> bool:
    """Recognize failures by the result header, never by arbitrary body text."""
    import re
    if isinstance(content, Message):
        if terminal_tool_control(content) is not None:
            return True
        verification = content.metadata.get("desktop_verification")
        if (content.name == "desktop_inspect_focus" and isinstance(verification, dict)
                and verification.get("focus_verified") is False):
            return True
        content = content.content
    exit_code = re.match(r"^\s*exit=(-?\d+)(?:\r?\n|$)", content or "")
    if exit_code:
        return int(exit_code.group(1)) != 0
    match = re.match(r"^\s*\[([^\]\n]+)\]", content or "")
    if not match:
        return False
    return any(word in match.group(1) for word in (
        "失败", "拒绝", "未通过", "未配置", "未启用", "不存在",
        "未找到", "超时", "冲突", "不可用", "未就绪", "不支持", "错误", "异常",
    ))


def task_tool_messages(messages: list[Message], task_origin_id: str) -> list[Message]:
    """Return tools owned by a real task, excluding interleaved tasks."""
    internal_users = {"tool_observation", "verification_feedback", "plan_step", "context_task_goal"}

    def real_user(message):
        return (message.role == "user" and message.name not in internal_users
                and not message.content.startswith("[自我验收"))

    if not any(real_user(message) and message.id == task_origin_id for message in messages):
        return []
    owner = None
    scoped = []
    for message in messages:
        if real_user(message):
            owner = message.metadata.get("task_origin_id") or message.id
        elif message.role == "tool":
            if (message.metadata.get("task_origin_id") or owner) == task_origin_id:
                scoped.append(message)
    return scoped


def desktop_submission_progress(messages: list[Message], *, task_origin_id: str | None = None) -> dict | None:
    """Summarize typed desktop evidence belonging to the requested task.

    Real user turns establish ownership for older tool results without metadata.
    Internal planning/observation messages never switch the owning task. If the
    original task is missing, keep the recovery summary conservative.
    """
    if task_origin_id is not None:
        messages = task_tool_messages(messages, task_origin_id)

    mutating = {"desktop_type", "desktop_press", "desktop_click", "desktop_hotkey",
                "desktop_open", "desktop_open_app", "desktop_show_desktop"}
    for message in reversed(messages):
        if message.role != "tool":
            continue
        if message.metadata.get("tool_cancelled") is True:
            continue
        if not message.name.startswith("desktop_"):
            # An arbitrary tool or script could have changed the desktop or
            # submitted data. Keep progress, but do not infer an unsubmitted UI.
            return None
        verification = message.metadata.get("desktop_verification")
        verification = verification if isinstance(verification, dict) else {}
        if tool_result_is_error(message):
            return None
        if verification.get("submitted") is True:
            return {"state": "submitted_unconfirmed", "submitted": True, "result_confirmed": False}
        if (message.name == "desktop_type" and verification.get("input_verified") is True
                and verification.get("input_allowed") is True
                and verification.get("action") == "type"
                and (verification.get("submitted") is None or verification.get("submitted") is False)):
            return {"state": "input_verified_unsubmitted", "input_verified": True, "submitted": False}
        if message.name in mutating:
            return None
    return None
