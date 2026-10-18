"""§5.8 结果自我验证："工具没报错" ≠ "任务完成了"。

任务开始时记录产物基线，结束时按目标检查本轮新增或变更的产物。
旧调用不传 baseline 时保留总量检查和视频时长回退行为。
"""
from __future__ import annotations

import os
import re
import stat
from pathlib import Path

_IMAGE_EXTS = {".png", ".jpg", ".jpeg", ".webp", ".gif", ".bmp", ".tif", ".tiff", ".ico", ".avif"}
_VIDEO_EXTS = {".mp4", ".mkv", ".mov", ".avi", ".webm"}
_KIND_EXTS = {
    "图片": _IMAGE_EXTS,
    "视频": _VIDEO_EXTS,
    "音频": {".mp3", ".wav", ".aac", ".flac", ".ogg", ".m4a"},
    "PDF": {".pdf"},
    "Word": {".doc", ".docx"},
    "Excel": {".xls", ".xlsx"},
    "文本": {".txt"},
    "Markdown": {".md", ".markdown"},
    "JSON": {".json"},
    "HTML": {".html", ".htm"},
}
_FILE_EXTS = set().union(*_KIND_EXTS.values()) | {".csv", ".py", ".js", ".ts", ".ppt", ".pptx"}
_RUNTIME_EXTS = {".db", ".sqlite", ".sqlite3", ".log", ".bin", ".token", ".lock", ".pid", ".pyc", ".pyo"}
_EXCLUDED_DIRS = {
    ".git", ".hg", ".svn", ".venv", "venv", "env", "node_modules",
    "__pycache__", ".cache", "cache", "caches", ".pytest_cache",
    ".mypy_cache", ".ruff_cache", ".tox", ".nox", "browsers",
    "browser_cache", "browser-cache", "ms-playwright", ".playwright",
}
_COUNT_PATTERN = r"(\d+|[零〇一二两三四五六七八九十百千]+)\s*(张|个|份|段|秒|分钟)"
_VISUAL_CREATE = r"(?:生图|生成(?!的)|制作(?!的)|绘制(?!的))"
_NAMED_VISUAL_APP = r"(?:豆包|即梦|通义万相|文心一格|ChatGPT|Midjourney|DALL[·-]?E|Canva|Firefly)"
_INTERNAL_VISUAL_APP = r"(?:桌面应用|桌面软件|浏览器(?:应用)?|网页应用|网站|网页|客户端|应用|软件|平台|app)"

ArtifactBaseline = dict[str, tuple[int, int, int]]
Workspace = str | Path | list[str | Path]


def _skip_directory(path: Path) -> bool:
    if path.name.casefold() in _EXCLUDED_DIRS:
        return True
    try:
        attrs = path.stat(follow_symlinks=False)
        # Windows junctions and symlinks must not lead outside the workspace.
        return path.is_symlink() or bool(
            getattr(attrs, "st_file_attributes", 0)
            & getattr(stat, "FILE_ATTRIBUTE_REPARSE_POINT", 0)
        )
    except OSError:
        return True


def _workspace_roots(workspace: Workspace) -> list[Path]:
    requested = workspace if isinstance(workspace, list) else [workspace]
    roots = []
    seen = set()
    for value in requested:
        root = Path(os.path.abspath(value))
        key = os.path.normcase(str(root))
        if key not in seen:
            seen.add(key)
            roots.append(root)
    return roots


def _iter_artifacts(workspace: Workspace = "."):
    """Walk only explicit roots, pruning caches and links before descent."""
    roots = _workspace_roots(workspace)
    multiple_roots = len(roots) > 1
    seen_files = set()
    for root in roots:
        for directory, dirs, names in os.walk(root, followlinks=False):
            parent = Path(directory)
            dirs[:] = [name for name in dirs if not _skip_directory(parent / name)]
            for name in names:
                if name.casefold().startswith(("desktop_shot_", "shot_")):
                    continue
                path = parent / name
                try:
                    if path.is_symlink() or not path.is_file():
                        continue
                    absolute_key = os.path.normcase(os.path.abspath(path))
                    if absolute_key in seen_files:
                        continue
                    info = path.stat()
                    seen_files.add(absolute_key)
                    key = absolute_key if multiple_roots else path.relative_to(root).as_posix()
                    yield path, key, (
                        info.st_size, info.st_mtime_ns, info.st_ctime_ns,
                    )
                except OSError:
                    # Files can disappear while tools are creating or replacing them.
                    continue


def capture_artifacts(workspace: Workspace = ".") -> ArtifactBaseline:
    """Capture metadata; keys are relative for one root, normalized absolute for multiple roots."""
    return {name: metadata for _, name, metadata in _iter_artifacts(workspace)}


def _candidate_files(workspace: Workspace, baseline: ArtifactBaseline | None) -> list[Path]:
    return [
        path for path, name, metadata in _iter_artifacts(workspace)
        if baseline is None or (metadata[0] > 0 and baseline.get(name) != metadata)
    ]


def _target_kind(text: str) -> str:
    """Recognize an explicit file type immediately following a count."""
    text = text.strip()
    for extension in _FILE_EXTS:
        if re.match(r"\.?" + re.escape(extension[1:]) + r"(?![a-z0-9])", text, re.I):
            return extension
    aliases = (
        (r"(?:图片|图像|照片|插画|海报|图(?:$|[\s，,。；;]))", "图片"),
        (r"(?:视频|录像|影片)", "视频"),
        (r"(?:音频|音乐|录音)", "音频"),
        (r"pdf", "PDF"),
        (r"(?:word|docx?)(?![a-z])", "Word"),
        (r"(?:excel|xlsx?)(?![a-z])", "Excel"),
        (r"(?:文本|txt)(?![a-z])", "文本"),
        (r"(?:markdown|md)(?![a-z])", "Markdown"),
        (r"json(?![a-z])", "JSON"),
        (r"html?(?![a-z])", "HTML"),
    )
    for pattern, kind in aliases:
        if re.match(pattern, text, re.I):
            return kind
    for ext in _IMAGE_EXTS | _VIDEO_EXTS:
        if re.match(re.escape(ext.lstrip(".")) + r"(?![a-z])", text, re.I):
            return "图片" if ext in _IMAGE_EXTS else "视频"
    return ""


def _is_explanatory_request(task_goal: str) -> bool:
    """An explanation of an action is not an instruction to execute it."""
    explanatory = re.match(
        r"^\s*(?:(?:请(?:你)?|帮我|麻烦(?:你)?|告诉我|能否|可以|"
        r"我想知道|想知道)\s*)?"
        r"(?:解释|介绍|说明|讲解|科普|什么是|如何|怎样|怎么|教程|explain\b|how to\b)",
        task_goal, re.I,
    )
    actions = r"(?:生成|制作|绘制|下载|保存|导出|写入|创建|录制|生图)"
    first_action = re.search(actions, task_goal)
    question = re.search(r"(?:如何|怎样|怎么)", task_goal)
    explanatory = bool(explanatory or (
        question and first_action and question.start() < first_action.start()
    ) or re.search(r"(?:的(?:教程|方法|步骤|流程|原理)|教程)(?:[？?。.]|$)", task_goal))
    execution_after_explanation = re.search(
        r"(?:(?:然后|接着|随后|再)\s*(?:请(?:你)?|帮我|替我)?\s*"
        r"|并\s*(?:请(?:你)?|帮我|替我)\s*|[，,；;]\s*并?\s*)"
        r"(?:(?:在|用|使用|通过)[^。；;\n]{0,24}?)?" + actions,
        task_goal,
    )
    return bool(explanatory and not execution_after_explanation)


def _quantity_value(text: str) -> int:
    if text.isdecimal():
        return int(text)
    digits = {"零": 0, "〇": 0, "一": 1, "二": 2, "两": 2, "三": 3, "四": 4,
              "五": 5, "六": 6, "七": 7, "八": 8, "九": 9}
    units = {"十": 10, "百": 100, "千": 1000}
    total = current = 0
    for char in text:
        if char in digits:
            current = digits[char]
        else:
            total += (current or 1) * units[char]
            current = 0
    return total + current


def _is_runtime_file(path: Path) -> bool:
    """Runtime state and diagnostics are not generic user deliverables."""
    name = path.name.casefold()
    return (
        path.suffix.casefold() in _RUNTIME_EXTS
        or name.endswith(("-wal", "-shm", "-journal"))
        or bool(re.search(r"\.log\.\d+$", name))
        or path.stem.casefold() in {"token", "tokens", "access_token", "refresh_token"}
    )


def _goal_artifact_kind(task_goal: str) -> str:
    """Recognize output requests; ordinary discussion need not create files."""
    if _is_explanatory_request(task_goal):
        return ""
    if "生图" in task_goal and not re.search(r"(?:什么是|介绍|解释|如何|怎样|怎么|教程).{0,8}生图", task_goal):
        return "图片"
    actions = r"(?:生成|制作|绘制|下载|保存|导出|写入|创建|录制)"
    if not re.search(actions, task_goal):
        return ""
    if re.search(actions + r".{0,60}(?:图片|图像|照片|插画|海报|张[^，。；]{0,12}图|(?:一|张)图)", task_goal):
        return "图片"
    if re.search(actions + r".{0,60}(?:视频|录像|影片)", task_goal):
        return "视频"
    if re.search(actions + r".{0,60}(?:文件|文档)", task_goal):
        return "文件"
    return ""


def extract_criteria(task_goal: str, plan=None) -> list[str]:
    """Extract count/duration criteria; retain plan precedence for legacy callers."""
    if _is_explanatory_request(task_goal):
        return []
    criteria = []
    if plan is not None:
        for step in getattr(plan, "steps", []):
            if getattr(step, "done_when", ""):
                criteria.append(step.done_when)
        if criteria:
            return criteria
    for match in re.finditer(_COUNT_PATTERN, task_goal):
        amount = _quantity_value(match.group(1))
        unit = match.group(2)
        if unit in ("秒", "分钟"):
            # "3分钟后/内" 是截止时间不是时长要求，不应生成时长检查
            # （qq_run1 教训："3分钟后看收件箱" 被误判为要生成3分钟视频）
            after = task_goal[match.end():match.end()+2]
            if after.startswith(("后", "内", "之")):
                continue
            criteria.append(f"时长检查：{amount}{unit}")
            continue
        tail = task_goal[match.end():]
        kind = _target_kind(tail)
        # "生成 3 张山水图" is still a request for three images.
        if not kind and unit == "张" and _goal_artifact_kind(task_goal) == "图片":
            kind = "图片"
        if kind or tail.lstrip().startswith(("文件", "文档")):
            criteria.append(f"数量检查：{amount}{unit}{kind}")
        # Counts of suggestions, steps or chat topics are not file requirements.
    return criteria


def _external_generation(task_goal: str, kind: str) -> bool:
    if kind != "图片":
        return False
    # Merely mentioning an app as the subject/style of a locally generated
    # image does not establish an instruction to operate that application.
    named = (
        r"(?:在|用|使用|通过|打开|进入|启动|前往|去|(?<!提)到)[^。；;\n]{0,24}"
        + _NAMED_VISUAL_APP + r"[^。；;\n]{0,100}?" + _VISUAL_CREATE
        + "|" + _NAMED_VISUAL_APP
        + r"(?:桌面版|网页版|应用|客户端|网页|平台|app)?(?:中|里|内|上)?[\s，,]*"
        r"(?:帮我|请(?:你)?|为我|给我)?\s*" + _VISUAL_CREATE
    )
    explicit = (r"(?:在|用|使用|通过|打开|进入|启动|前往|去|(?<!提)到)[^。；;\n]{0,24}"
                + _INTERNAL_VISUAL_APP + r"[^。；;\n]{0,100}?" + _VISUAL_CREATE)
    for match in re.finditer(named + "|" + explicit, task_goal, re.I):
        before = task_goal[max(0, match.start() - 12):match.start()]
        if re.search(r"(?:不要|别|无需|不用|而非|不在|不)\s*$", before):
            continue
        return True
    return False


def _requires_local_delivery(task_goal: str) -> bool:
    # "不用下载到本地" is not a local-delivery requirement. Mask only that
    # negative clause; a later "但保存到本地" remains an explicit requirement.
    remaining = re.sub(
        r"(?:无需|无须|不必|不用|不需要|不要求|不要|别|不)\s*(?:再|额外)?\s*"
        r"(?:下载|保存|导出|本地|文件|发送|发给|发到|上传|交付|分享|发布|打印)"
        r"(?:(?!但|不过|而|，|,|。|；|;|\n).){0,18}",
        "", task_goal,
    )
    return bool(re.search(r"(?:下载|保存|导出|本地|文件|发送|发给|发到|上传|交付|分享|发布|打印)", remaining))


def _has_generation_followup(task_goal: str) -> bool:
    creation = re.search(_VISUAL_CREATE, task_goal, re.I)
    if creation is None:
        return False
    return bool(re.search(
        r"(?:然后|接着|随后|之后|接下来|(?:后|并|再)\s*(?:帮我|请(?:你)?)?\s*"
        r"(?:解释|整理|总结|分析|翻译|回答|发送|保存|下载|导出|制作|生成|修改|调整|编辑|告诉|列出|打开|处理|提取|对比))",
        task_goal[creation.end():],
    ))


def outcome_requirement(task_goal: str) -> dict:
    """Describe the evidence required by an explicit outcome request.

    Only an explicit request to create images inside a named/desktop/browser
    application qualifies for external visual completion. The caller must bind
    observer evidence to the active task and snapshot before verification.
    """
    ordinary = {"mode": "ordinary", "kind": "", "minimum_count": 0,
                "external_generation": False}
    if _is_explanatory_request(task_goal):
        return ordinary
    kind = _goal_artifact_kind(task_goal)
    criteria = extract_criteria(task_goal)
    counts = []
    mixed_outputs = any(criterion.startswith("时长检查：") for criterion in criteria)
    for criterion in criteria:
        match = re.match(r"数量检查：(\d+)(?:张|个|份|段)(.*)", criterion)
        if match:
            count_kind = _target_kind(match.group(2)) or "文件"
            if not kind:
                kind = count_kind
            if kind == "文件" or count_kind == kind or (kind == "图片" and count_kind in _IMAGE_EXTS):
                counts.append(int(match.group(1)))
            elif kind == "图片":
                mixed_outputs = True
    if not kind and any(criterion.startswith("时长检查：") for criterion in criteria):
        kind = "视频"
    if not kind:
        return ordinary
    external = _external_generation(task_goal, kind)
    mode = ("external_visual" if external and not _requires_local_delivery(task_goal)
            and not mixed_outputs and not _has_generation_followup(task_goal) else "local_artifact")
    return {"mode": mode, "kind": kind, "minimum_count": max([1, *counts]),
            "external_generation": external}


def _external_evidence_complete(evidence, requirement: dict) -> bool:
    if not isinstance(evidence, dict):
        return False
    count = evidence.get("result_count")
    return (
        evidence.get("status") == "completed"
        and evidence.get("goal_match") is True
        and evidence.get("source") == "visual_observer"
        and type(count) is int and count >= requirement["minimum_count"]
        and all(isinstance(evidence.get(field), str) and bool(evidence[field].strip())
                for field in ("snapshot_id", "task_origin_id"))
    )


def _valid_image(path: Path) -> bool:
    try:
        from PIL import Image
        with Image.open(path) as image:
            image.verify()
        with Image.open(path) as image:
            image.load()
            return image.width > 0 and image.height > 0
    except Exception:
        return False


def _files_for_kind(files: list[Path], kind: str, strict: bool) -> list[Path]:
    selected = [path for path in files if not kind or kind == "文件"
                or path.suffix.lower() in _KIND_EXTS.get(kind, {kind})]
    if strict and (not kind or kind == "文件"):
        selected = [path for path in selected if not _is_runtime_file(path)]
    if (kind == "图片" or kind in _IMAGE_EXTS) and strict:
        selected = [path for path in selected if _valid_image(path)]
    return selected


def check_criterion(criterion: str, workspace: Workspace = ".",
                    baseline: ArtifactBaseline | None = None, *,
                    _files: list[Path] | None = None) -> bool:
    """Return whether a criterion is supported by eligible local artifacts."""
    count = re.match(r"数量检查：(\d+)(张|个|份|段)(.*)", criterion)
    output = re.match(r"产物检查：(图片|视频|文件)", criterion)
    duration = re.match(r"(?:时长|数量)检查：(\d+)(秒|分钟)", criterion)
    if not (count or output or duration):
        return True
    files = _files if _files is not None else _candidate_files(workspace, baseline)
    if count:
        kind = _target_kind(count.group(3))
        if not kind and count.group(2) == "张":
            kind = "图片"
        return len(_files_for_kind(files, kind, baseline is not None)) >= int(count.group(1))
    if output:
        return bool(_files_for_kind(files, output.group(1), baseline is not None))

    target_sec = int(duration.group(1)) * (60 if duration.group(2) == "分钟" else 1)
    video_files = _files_for_kind(files, "视频", baseline is not None)
    if not video_files:
        return False
    import shutil
    import subprocess
    if shutil.which("ffprobe"):
        total_duration = 0.0
        parsed_any = False
        for video_file in video_files:
            try:
                result = subprocess.run(
                    ["ffprobe", "-v", "error", "-show_entries", "format=duration",
                     "-of", "default=noprint_wrappers=1:nokey=1", str(video_file)],
                    capture_output=True, text=True, timeout=5,
                )
                if result.returncode == 0 and result.stdout.strip():
                    total_duration += float(result.stdout.strip())
                    parsed_any = True
            except Exception:
                pass
        if parsed_any:
            return total_duration >= target_sec * 0.8
        # Preserve the existing fallback when media duration cannot be parsed.
        return True
    return True


def verify_outcome(task_goal: str, plan=None, workspace: Workspace = ".",
                   baseline: ArtifactBaseline | None = None,
                   external_evidence=None) -> tuple[bool, list[str]]:
    """Return (passed, unmet criteria); never infer UI completion from local files."""
    requirement = outcome_requirement(task_goal)
    if requirement["mode"] == "external_visual":
        if _external_evidence_complete(external_evidence, requirement):
            return True, []
        return False, [
            f"界面结果检查：至少{requirement['minimum_count']}张{requirement['kind']}"
            "（缺少与当前任务和截图绑定的可信视觉完成证据；须确认生成结果已出现、"
            "内容匹配且数量满足要求。仅提交、截图或未知结果不能判定完成；无需强加本地下载。）"
        ]
    criteria = extract_criteria(task_goal, plan)
    if baseline is not None:
        # A vague plan must not replace an explicit artifact goal.
        for criterion in extract_criteria(task_goal):
            if criterion not in criteria:
                criteria.append(criterion)
        kind = _goal_artifact_kind(task_goal)
        if kind and not any(
            re.match(r"数量检查：\d+(?:张|个|份|段)" + re.escape(kind), criterion)
            for criterion in criteria
        ):
            criteria.append(f"产物检查：{kind}")
    if not criteria:
        return True, []
    known = any(re.match(r"(?:数量|时长|产物)检查：", criterion) for criterion in criteria)
    files = _candidate_files(workspace, baseline) if known else []
    failed = []
    for criterion in criteria:
        if not check_criterion(criterion, workspace, baseline, _files=files):
            if baseline is None:
                failed.append(criterion)
            else:
                failed.append(
                    criterion + "（未找到足够的本轮新增或变更、非空产物；"
                    "图片须可解码，旧文件、桌面截图、缓存及运行数据库或日志不计入。请继续生成并保存到工作目录。）"
                )
    return not failed, failed
