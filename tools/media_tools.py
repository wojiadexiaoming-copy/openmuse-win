"""多模态生成工具：集成 Agnes Image 2.5 Flash (文生图) 与 Agnes Video 2.5 Flash (文生视频)。"""
from __future__ import annotations

import json
import os
import time
from typing import Any

import httpx

from agent.tools_registry import ToolDefinition

BASE_URL = "https://apihub.agnes-ai.com/v1"
API_KEY = ""


def configure(base_url: str = "", api_key: str = ""):
    global BASE_URL, API_KEY
    if base_url:
        BASE_URL = base_url.rstrip("/")
    if api_key:
        API_KEY = api_key


def _get_key() -> str:
    if API_KEY:
        return API_KEY
    env_key = (
        os.environ.get("ALTEREGO_MEDIA_API_KEY", "")
        or os.environ.get("ALTEREGO_BACKUP_LLM_API_KEY", "")
        or os.environ.get("ALTEREGO_LLM_API_KEY", "")
    )
    if env_key:
        return env_key
    try:
        from pathlib import Path
        from vault.manager import Vault
        repo_root = Path(__file__).resolve().parent.parent
        data_dir = Path(os.environ.get("ALTEREGO_DATA", str(repo_root)))
        v = Vault.for_machine(data_dir)
        if v.has("media_api_key"):
            return v.resolve("media_api_key", caller="media_tools")
        if v.has("backup_llm_api_key"):
            return v.resolve("backup_llm_api_key", caller="media_tools")
        if v.has("llm_api_key"):
            return v.resolve("llm_api_key", caller="media_tools")
    except Exception:
        pass
    return ""


def image_generate(args: dict[str, Any]) -> str:
    """使用 Agnes Image 2.5 Flash 生成图像。"""
    prompt = args.get("prompt", "")
    if not prompt:
        return "[生成失败] prompt 不能为空"
    size = args.get("size", "1K")
    key = _get_key()
    if not key:
        return "[生成失败] 未配置 API Key"

    url = f"{BASE_URL}/images/generations"
    headers = {
        "Authorization": f"Bearer {key}",
        "Content-Type": "application/json",
    }
    payload = {
        "model": "agnes-image-2.5-flash",
        "prompt": prompt,
        "size": size,
    }
    try:
        resp = httpx.post(url, headers=headers, json=payload, timeout=60.0)
        if resp.status_code != 200:
            return f"[生成失败] HTTP {resp.status_code}: {resp.text}"
        data = resp.json()
        items = data.get("data", [])
        if not items:
            return f"[生成完成但无图像返回] {data}"
        img_url = items[0].get("url") or items[0].get("b64_json", "")[:100]
        return f"[图像生成成功]\nPrompt: {prompt}\nURL: {img_url}"
    except Exception as e:
        return f"[生成异常] {e}"


def video_generate(args: dict[str, Any]) -> str:
    """使用 Agnes Video 2.5 Flash 创建异步视频生成任务并返回任务 ID 与查询链接。"""
    prompt = args.get("prompt", "")
    if not prompt:
        return "[生成失败] prompt 不能为空"
    key = _get_key()
    if not key:
        return "[生成失败] 未配置 API Key"

    url = f"{BASE_URL}/videos"
    headers = {
        "Authorization": f"Bearer {key}",
        "Content-Type": "application/json",
    }
    payload = {
        "model": "agnes-video-2.5-flash",
        "prompt": prompt,
        "size": "720P",
    }
    try:
        resp = httpx.post(url, headers=headers, json=payload, timeout=60.0)
        if resp.status_code not in (200, 201, 202):
            return f"[视频任务创建失败] HTTP {resp.status_code}: {resp.text}"
        data = resp.json()
        task_id = data.get("id") or data.get("video_id") or data.get("task_id", "")
        query_url = f"https://apihub.agnes-ai.com/agnesapi?video_id={task_id}&model_name=agnes-video-2.5-flash"
        return f"[视频生成任务已创建]\n任务ID: {task_id}\n状态查询链接: {query_url}\nPrompt: {prompt}"
    except Exception as e:
        return f"[生成异常] {e}"


IMAGE_TOOL = ToolDefinition(
    name="image_generate",
    description="使用 Agnes Image 2.5 Flash 根据提示词生成高质量图像，返回图像访问链接。",
    parameters={
        "type": "object",
        "properties": {
            "prompt": {"type": "string", "description": "图像生成的提示词描述"},
            "size": {"type": "string", "enum": ["1K", "2K", "1024x1024"], "default": "1K", "description": "图像分辨率规格"},
        },
        "required": ["prompt"],
    },
    func=image_generate,
)

VIDEO_TOOL = ToolDefinition(
    name="video_generate",
    description="使用 Agnes Video 2.5 Flash 创建 720P 视频生成任务，返回视频任务 ID 和查询状态链接。",
    parameters={
        "type": "object",
        "properties": {
            "prompt": {"type": "string", "description": "视频场景与动作细节描述"},
        },
        "required": ["prompt"],
    },
    func=video_generate,
)

MEDIA_TOOLS = [IMAGE_TOOL, VIDEO_TOOL]
