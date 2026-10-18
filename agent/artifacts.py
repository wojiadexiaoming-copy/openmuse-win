"""§26 Artifacts 产物系统 [必选]

Agent 做出来的东西（文档、图片、视频、网页）不是随手丢个文件，
要有：构建 → 预览 → 交付 → 版本 的完整链路。

生命周期：draft → building → ready → delivered → edit → v2, v3...
"""
from __future__ import annotations

import logging

log = logging.getLogger("artifacts")

import os
import time
import uuid
from dataclasses import dataclass, field, asdict


@dataclass
class Artifact:
    artifact_id: str                    # "art_xxx"
    kind: str                           # document | image | video | webpage | spreadsheet | presentation
    title: str
    file_path: str = ""                 # 当前版本路径
    version: int = 1
    status: str = "draft"               # draft | building | ready | delivered
    versions: list[dict] = field(default_factory=list)
    # versions: [{"v": 1, "path": "...", "at": ..., "note": "初版"}]
    created_at: float = 0.0


class ArtifactManager:
    """产物全链路管理。"""

    def __init__(self, root: str | None = None):
        if root is None:
            root = os.path.join(os.path.expanduser("~"), ".alterego", "artifacts")
        self.root = root
        os.makedirs(root, exist_ok=True)
        self._items: dict[str, Artifact] = {}

    def create(self, kind: str, title: str) -> Artifact:
        """新建空产物，返回 Artifact。"""
        aid = f"art_{uuid.uuid4().hex[:8]}"
        art = Artifact(artifact_id=aid, kind=kind, title=title,
                        created_at=time.time())
        self._items[aid] = art
        log.info(f"[artifact] 创建 {aid}（{kind}）：{title}")
        return art

    def build(self, artifact_id: str, builder_fn) -> str:
        """执行构建（如渲染长图、剪辑视频）。building 状态，失败回 draft。

        builder_fn: () -> str，返回产物文件路径。
        返回产物路径，失败抛异常且状态回 draft。
        """
        art = self._require(artifact_id)
        art.status = "building"
        try:
            path = builder_fn()
        except Exception:
            art.status = "draft"
            raise
        art.file_path = path
        art.status = "ready"
        art.versions.append({"v": art.version, "path": path,
                              "at": time.time(), "note": "构建"})
        log.info(f"[artifact] {artifact_id} 构建完成 → {path}")
        return path

    def deliver(self, artifact_id: str) -> str:
        """标记已交付，返回当前路径。"""
        art = self._require(artifact_id)
        if art.status != "ready":
            raise ValueError(f"产物未就绪（当前 {art.status}），不能交付")
        art.status = "delivered"
        return art.file_path

    def edit(self, artifact_id: str, editor_fn, note: str = "") -> str:
        """修订：版本号 +1，重新构建。editor_fn: (旧路径) -> 新路径。"""
        art = self._require(artifact_id)
        art.status = "building"
        try:
            new_path = editor_fn(art.file_path)
        except Exception:
            art.status = "ready"  # 修订失败回退到上一版就绪态
            raise
        art.version += 1
        art.file_path = new_path
        art.status = "ready"
        art.versions.append({"v": art.version, "path": new_path,
                              "at": time.time(), "note": note or "修订"})
        return new_path

    def get(self, artifact_id: str) -> Artifact | None:
        return self._items.get(artifact_id)

    def list(self, kind: str | None = None) -> list[Artifact]:
        items = list(self._items.values())
        if kind:
            items = [a for a in items if a.kind == kind]
        return sorted(items, key=lambda a: -a.created_at)

    def _require(self, artifact_id: str) -> Artifact:
        art = self._items.get(artifact_id)
        if art is None:
            raise ValueError(f"未知产物: {artifact_id}")
        return art


def make_artifact_tools(manager: "ArtifactManager"):
    """构建产物工具。"""
    from agent.tools_registry import ToolDefinition

    def _create(args: dict) -> str:
        a = manager.create(args.get("kind", "document"),
                            args.get("title", "未命名"))
        return f"[产物已创建] {a.artifact_id}（{a.kind}）：{a.title}"

    def _list(args: dict) -> str:
        items = manager.list(args.get("kind"))
        if not items:
            return "[产物] 暂无"
        return "[产物]\n" + "\n".join(
            f"- {a.artifact_id} [{a.kind}/{a.status}/v{a.version}] {a.title}"
            for a in items[:10])

    return [
        ToolDefinition(
            name="artifact_create",
            description="新建产物（文档/图片/视频/网页等），进入构建链路。",
            parameters={"type": "object",
                         "properties": {
                             "kind": {"type": "string",
                                       "enum": ["document", "image", "video",
                                                 "webpage", "spreadsheet",
                                                 "presentation"]},
                             "title": {"type": "string"}},
                         "required": ["kind", "title"]},
            func=_create,
        ),
        ToolDefinition(
            name="artifact_list",
            description="列出产物。",
            parameters={"type": "object",
                         "properties": {"kind": {"type": "string"}},
                         "required": []},
            func=_list,
        ),
    ]
