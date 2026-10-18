"""file_read / file_write / file_edit。

安全边界：所有路径必须落在 agent.workspace 配置的根目录下（防跑出界）。
用 pathlib 处理 Windows/ Linux 路径差异；读写一律 encoding="utf-8"。
"""
from __future__ import annotations

from pathlib import Path

from agent.tools_registry import ToolDefinition

# 由 tools/__init__.register_all 注入（M1 简化：模块级变量）
WORKSPACE = Path(".").resolve()


def _safe(path: str) -> Path:
    p = (WORKSPACE / path).resolve()
    if WORKSPACE not in p.parents and p != WORKSPACE:
        raise ValueError(f"路径越界（只允许 {WORKSPACE} 内）: {path}")
    return p


def _read(args: dict) -> str:
    p = _safe(args["path"])
    if not p.is_file():
        return f"[不存在] {p}"
    offset = int(args.get("offset", 1))
    limit = int(args.get("limit", 200))
    lines = p.read_text(encoding="utf-8", errors="replace").splitlines()
    chunk = lines[offset - 1: offset - 1 + limit]
    total = len(lines)
    head = f"[{p} 共 {total} 行，显示 {offset}-{offset + len(chunk) - 1}]\n"
    return head + "\n".join(chunk)


def _write(args: dict) -> str:
    p = _safe(args["path"])
    mode = args.get("mode", "overwrite")
    content: str = args.get("content", "")
    if mode == "append" and p.exists():
        old = p.read_text(encoding="utf-8", errors="replace")
        p.write_text(old + content, encoding="utf-8")
    else:
        if p.exists():
            # 写前备份（spec §7）
            bak = p.with_suffix(p.suffix + ".bak")
            bak.write_bytes(p.read_bytes())
        p.parent.mkdir(parents=True, exist_ok=True)
        p.write_text(content, encoding="utf-8")
    return f"[已写入] {p}（{len(content)} 字符）"


def _edit(args: dict) -> str:
    p = _safe(args["path"])
    old: str = args["old"]
    new: str = args["new"]
    text = p.read_text(encoding="utf-8", errors="replace")
    count = text.count(old)
    if count == 0:
        return f"[未找到] 待替换文本在 {p} 中出现 0 次"
    if count > 1:
        return f"[歧义] 待替换文本出现 {count} 次，请给更长的 old 精确定位"
    p.write_text(text.replace(old, new, 1), encoding="utf-8")
    return f"[已替换] {p}（1 处）"


def _schema(desc: str, props: dict, required: list[str]) -> dict:
    return {"type": "object", "properties": props, "required": required}


FILE_TOOLS = [
    ToolDefinition(
        name="file_read",
        description="读文件内容，超大文件分页（offset/limit）。只能读 workspace 根目录内的文件。",
        parameters=_schema("", {
            "path": {"type": "string", "description": "相对 workspace 的路径"},
            "offset": {"type": "integer", "default": 1},
            "limit": {"type": "integer", "default": 200},
        }, ["path"]),
        func=_read,
        is_readonly=True,
        cacheable=True,
        cache_ttl_sec=300,
        parallel_safe=True,
    ),
    ToolDefinition(
        name="file_write",
        description="写文件（overwrite/append），覆盖前自动备份 .bak。只能写 workspace 内。",
        parameters=_schema("", {
            "path": {"type": "string"},
            "content": {"type": "string"},
            "mode": {"type": "string", "enum": ["overwrite", "append"], "default": "overwrite"},
        }, ["path", "content"]),
        func=_write,
        is_readonly=False,
        cacheable=False,
        parallel_safe=False,
    ),
    ToolDefinition(
        name="file_edit",
        description="精确替换文件中唯一出现的一段文本。出现 0 次或多次都拒绝。",
        parameters=_schema("", {
            "path": {"type": "string"},
            "old": {"type": "string", "description": "待替换的原文（必须唯一）"},
            "new": {"type": "string", "description": "替换成的新文本"},
        }, ["path", "old", "new"]),
        func=_edit,
        is_readonly=False,
        cacheable=False,
        parallel_safe=False,
    ),
]
# [M2/M4 完成] file_write/file_edit L2审批（§36）、文件资源锁（§5.9）、file_read 缓存与失效（§7.3）均已闭环接入。
