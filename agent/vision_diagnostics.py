"""Vision request audit and isolated screenshot diagnostics.

Default CLI use is offline. --run-model explicitly sends only the selected image
to a configured role; no tools, session history or desktop actions are involved.
"""
from __future__ import annotations

import argparse
import base64
import hashlib
import io
import json
import math
import re
import tempfile
from dataclasses import dataclass
from pathlib import Path
from typing import Any
from urllib.parse import urlsplit


def _image_metadata(url: str) -> dict:
    """Describe the actual image payload without retaining pixels or its URL."""
    info: dict[str, Any] = {"dimensions": None, "sha256": None}
    if not isinstance(url, str) or not url.startswith("data:"):
        info["source"] = "external"
        return info
    try:
        header, encoded = url.split(",", 1)
        if ";base64" not in header:
            raise ValueError("unsupported image encoding")
        raw = base64.b64decode(encoded, validate=True)
        info.update({
            "source": "inline", "sha256": hashlib.sha256(raw).hexdigest(),
            "byte_count": len(raw),
        })
        from PIL import Image
        with Image.open(io.BytesIO(raw)) as image:
            info["dimensions"] = list(image.size)
            info["format"] = image.format
    except Exception as exc:
        info["metadata_error"] = type(exc).__name__
    return info


def collect_request_metadata(model: str, base_url: str,
                             payload: list[dict], tools: list[dict] | None = None) -> dict:
    """Collect metadata from the exact serialized request, never source messages."""
    images = []
    text_characters = 0
    for message in payload:
        content = message.get("content")
        if isinstance(content, str):
            text_characters += len(content)
            continue
        if not isinstance(content, list):
            continue
        for part in content:
            if not isinstance(part, dict):
                continue
            if part.get("type") == "text" and isinstance(part.get("text"), str):
                text_characters += len(part["text"])
            elif part.get("type") == "image_url":
                image_url = part.get("image_url")
                url = image_url.get("url") if isinstance(image_url, dict) else image_url
                images.append(_image_metadata(url))
    try:
        host = urlsplit(base_url).hostname or ""
    except ValueError:
        host = ""
    return {
        "model": model, "actual_model": model, "base_url_host": host,
        "image_count": len(images), "images": images, "tool_count": len(tools or []),
        "message_count": len(payload), "text_characters": text_characters,
    }


def _finite_number(value: Any) -> float:
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        raise ValueError("coordinate/confidence must be numeric")
    result = float(value)
    if not math.isfinite(result):
        raise ValueError("coordinate/confidence must be finite")
    return result


def validate_bbox(values) -> tuple[float, float, float, float]:
    if not isinstance(values, (list, tuple)) or len(values) != 4:
        raise ValueError("bbox must contain four normalized coordinates")
    x1, y1, x2, y2 = map(_finite_number, values)
    if not (0 <= x1 < x2 <= 1 and 0 <= y1 < y2 <= 1):
        raise ValueError("bbox must be ordered and inside [0, 1]")
    return x1, y1, x2, y2


@dataclass(frozen=True)
class VisionDiagnosis:
    description: str
    input_box: tuple[float, float, float, float] | None
    confidence: float

    def to_dict(self) -> dict:
        box = dict(zip(("x1", "y1", "x2", "y2"), self.input_box)) if self.input_box else None
        return {"description": self.description, "input_box": box, "confidence": self.confidence}


def _unique_object(pairs):
    result = {}
    for key, value in pairs:
        if key in result:
            raise ValueError("duplicate JSON key")
        result[key] = value
    return result


def _reject_constant(value):
    raise ValueError("JSON must not contain nonfinite numbers")


def parse_diagnosis(raw: str) -> VisionDiagnosis:
    """Strict JSON/schema/bounds parsing; no fence extraction or guessed repair."""
    obj = json.loads(raw, object_pairs_hook=_unique_object, parse_constant=_reject_constant)
    if not isinstance(obj, dict) or set(obj) != {"description", "input_box", "confidence"}:
        raise ValueError("diagnosis must contain exactly description/input_box/confidence")
    description = obj["description"]
    if not isinstance(description, str) or not description.strip() or len(description) > 2000:
        raise ValueError("description must be nonempty and at most 2000 characters")
    confidence = _finite_number(obj["confidence"])
    if not 0 <= confidence <= 1:
        raise ValueError("confidence must be inside [0, 1]")
    box = obj["input_box"]
    if box is not None:
        if not isinstance(box, dict) or set(box) != {"x1", "y1", "x2", "y2"}:
            raise ValueError("input_box must contain exactly x1/y1/x2/y2 or null")
        box = validate_bbox([box[key] for key in ("x1", "y1", "x2", "y2")])
    return VisionDiagnosis(description.strip(), box, confidence)


def score_bbox(predicted, ground_truth) -> dict:
    """Compare normalized boxes; score uses manual ground truth, never self-judgment."""
    truth = validate_bbox(ground_truth)
    if predicted is None:
        return {"iou": 0.0, "center_distance": None, "center_inside": False,
                "mean_coordinate_error": None}
    box = validate_bbox(predicted)
    left, top = max(box[0], truth[0]), max(box[1], truth[1])
    right, bottom = min(box[2], truth[2]), min(box[3], truth[3])
    intersection = max(0.0, right - left) * max(0.0, bottom - top)
    area = (box[2] - box[0]) * (box[3] - box[1])
    truth_area = (truth[2] - truth[0]) * (truth[3] - truth[1])
    cx, cy = (box[0] + box[2]) / 2, (box[1] + box[3]) / 2
    tx, ty = (truth[0] + truth[2]) / 2, (truth[1] + truth[3]) / 2
    return {
        "iou": intersection / (area + truth_area - intersection),
        "center_distance": math.hypot(cx - tx, cy - ty),
        "center_inside": truth[0] <= cx <= truth[2] and truth[1] <= cy <= truth[3],
        "mean_coordinate_error": sum(abs(a - b) for a, b in zip(box, truth)) / 4,
    }


_DIAGNOSTIC_PROMPT = """只检查所附目标应用窗口截图，不执行操作。
先描述界面，再定位用于输入新提示词的主聊天输入框，不要把搜索框当作输入框。
输出严格 JSON，且只允许以下三个字段：
{"description":"简要界面描述","input_box":{"x1":0.1,"y1":0.7,"x2":0.8,"y2":0.9},"confidence":0.9}
坐标是相对于整张所附图片宽高的归一化比例，必须满足 0<=x1<x2<=1、0<=y1<y2<=1。
若无法找到输入框，input_box 必须为 null，不要猜测。confidence 在 [0,1]。
不要使用 Markdown 代码块，不要输出工具调用、私人账户信息或窗口外内容。"""


def _safe_error_code(value) -> str | None:
    if isinstance(value, int) and not isinstance(value, bool):
        return str(value) if 0 <= value <= 1_000_000_000 else None
    if (isinstance(value, str) and re.fullmatch(r"[A-Za-z0-9_.-]{1,64}", value)
            and not re.search(r"(?i)(sk[-_]|cfat_|bearer|api[-_]?key|secret|token)", value)):
        return value
    return None


def safe_diagnostic_error(exc: Exception, stage: str = "request") -> dict:
    """Classify known provider signals without retaining their private error text."""
    status = getattr(exc, "status_code", None)
    if not isinstance(status, int) or isinstance(status, bool) or not 100 <= status <= 599:
        status = None
    codes = [getattr(exc, "code", None)]
    messages = [str(exc)[:20000]]
    body = getattr(exc, "body", None)
    if isinstance(body, dict):
        nodes = [body]
        if isinstance(body.get("error"), dict):
            nodes.append(body["error"])
        if isinstance(body.get("errors"), list):
            nodes.extend(item for item in body["errors"] if isinstance(item, dict))
        for node in nodes:
            codes.extend([node.get("code"), node.get("type")])
            if isinstance(node.get("message"), str):
                messages.append(node["message"][:20000])
    code = next((clean for value in codes if (clean := _safe_error_code(value)) is not None), None)
    text = "\n".join(messages).lower()
    category, evidence = "unclassified_error", []
    if stage == "response":
        category, evidence = "response_schema_error", ["strict_diagnostic_schema_rejected_response"]
    elif re.search(
        r"(?:does not|do not|doesn't|cannot|can't|not)\s+(?:support|accept|process).{0,50}"
        r"(?:image|vision|multimodal)|(?:image|vision|multimodal).{0,60}"
        r"(?:not supported|unsupported|text.only|disabled)|model.{0,60}text.only", text
    ):
        category, evidence = "vision_unsupported", ["provider_reports_unsupported_image_input"]
    elif re.search(
        r"(?:invalid|malformed|unsupported).{0,40}(?:image_url|image format|image data|base64|image payload)"
        r"|(?:messages|content).{0,100}(?:expected|must be|invalid|type)"
        r"|expected.{0,40}(?:string|object).{0,50}(?:array|list)", text
    ):
        category, evidence = "request_format_error", ["provider_reports_request_schema_or_image_encoding_error"]
    elif status in {401, 403, 429} or (status is not None and status >= 500):
        category, evidence = "gateway_error", [f"http_status_{status}"]
    elif type(exc).__name__ in {"APIConnectionError", "APITimeoutError", "ConnectError", "ReadTimeout"}:
        category, evidence = "gateway_error", ["transport_or_timeout_error"]
    elif status in {400, 422}:
        category, evidence = "unclassified_bad_request", [f"http_status_{status}"]
    return {"error_type": type(exc).__name__, "status_code": status,
            "code": code, "category": category, "evidence": evidence, "stage": stage}


def run_vision_probe(client, image, ground_truth=None) -> dict:
    """One selected-model request; failures retain audit metadata and safe evidence."""
    from .models import Message

    requests = []
    previous = getattr(client, "_request_observer", None)
    previous_fallback = getattr(client, "fallback", None)
    messages = [
        Message(role="system", content="你是界面视觉定位诊断器，只返回指定结构的 JSON。"),
        Message(role="user", content=_DIAGNOSTIC_PROMPT, images=[image]),
    ]
    from .llm import LLMClient
    summary = collect_request_metadata(
        client.model, client.base_url, [LLMClient._to_openai(message) for message in messages])
    report = {"success": False, "request_summary": summary, "requests": requests}

    def observe(metadata):
        requests.append(metadata)
        if previous:
            previous(metadata)

    # Even a direct caller must not silently switch the selected diagnostic model.
    client.fallback = None
    client.set_request_observer(observe)
    stage = "request"
    try:
        reply = client.chat(messages, tools=None, known_tools=set())
        stage = "response"
        if reply.tool_calls:
            raise ValueError("diagnostic response must not contain tool calls")
        diagnosis = parse_diagnosis(reply.content)
        report.update({"success": True, "diagnosis": diagnosis.to_dict()})
        if ground_truth is not None:
            report["score"] = score_bbox(diagnosis.input_box, ground_truth)
    except Exception as exc:
        report["diagnostic_error"] = type(exc).__name__
        report["error"] = safe_diagnostic_error(exc, stage)
    finally:
        client.fallback = previous_fallback
        client.set_request_observer(previous)
    return report


def _prepare_image(path: Path, crop, temporary: Path):
    from PIL import Image
    from .models import ImagePart

    with Image.open(path) as source:
        source.load()
        source_dimensions = list(source.size)
        if crop is not None:
            left, top, right, bottom = crop
            if not (0 <= left < right <= source.width and 0 <= top < bottom <= source.height):
                raise ValueError("crop must be ordered and inside the source image")
            cropped = source.crop((left, top, right, bottom)).convert("RGB")
            crop_path = temporary / "diagnostic-window.png"
            cropped.save(crop_path)
            image = ImagePart.from_file(crop_path)
        else:
            buffer = io.BytesIO()
            source.convert("RGB").save(buffer, format="PNG")
            image = ImagePart.from_bytes(buffer.getvalue())
    return image, {"source_dimensions": source_dimensions, "crop_box_pixels": crop}


def configured_diagnostic_client(config_path: Path, role: str, target: str, vault_dir=None):
    """Read configuration only on explicit online invocation; never rewrite it."""
    import yaml
    from .llm import LLMClient

    cfg = yaml.safe_load(config_path.read_text(encoding="utf-8"))
    vault = None
    if vault_dir is not None:
        from vault import Vault
        vault = Vault.for_machine(Path(vault_dir).parent)
    client = LLMClient.for_role(cfg, role, vault=vault)
    if target == "fallback":
        if client.fallback is None:
            raise ValueError("selected role has no configured fallback")
        client = client.fallback
    # A comparison must never silently replace the selected model.
    client.fallback = None
    return client


def main(argv=None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("image", type=Path, help="Local target-window screenshot")
    parser.add_argument("--crop", type=int, nargs=4, metavar=("LEFT", "TOP", "RIGHT", "BOTTOM"),
                        help="Pixel crop of target window; crop is stored only temporarily")
    parser.add_argument("--ground-truth", type=float, nargs=4, metavar=("X1", "Y1", "X2", "Y2"),
                        help="Manual normalized bbox on the submitted crop/image")
    mode = parser.add_mutually_exclusive_group()
    mode.add_argument("--response-json", type=Path, help="Offline model JSON to validate and score")
    mode.add_argument("--run-model", action="store_true", help="Explicitly send image to configured model")
    parser.add_argument("--config", type=Path, default=Path("config.yaml"))
    parser.add_argument("--role", default="main")
    parser.add_argument("--target", choices=("primary", "fallback"), default="primary")
    parser.add_argument("--vault-dir", type=Path,
                        help="Optional existing Vault for the same credentials as the application")
    parser.add_argument("--timeout-seconds", type=float, default=60.0, help="Bound each diagnostic API request; retries disabled")
    parser.add_argument("--output", type=Path, help="Write metadata/results JSON report")
    args = parser.parse_args(argv)
    report = {"mode": "model" if args.run_model else "offline",
              "success": False, "requests": []}
    stage = "preparation"
    try:
        if not 0 < args.timeout_seconds <= 120:
            raise ValueError("timeout must be within 0..120 seconds")
        if args.ground_truth is not None:
            validate_bbox(args.ground_truth)
        with tempfile.TemporaryDirectory(prefix="alterego-vision-") as temporary:
            image, preparation = _prepare_image(args.image, args.crop, Path(temporary))
            from .llm import LLMClient
            from .models import Message
            payload = [LLMClient._to_openai(Message(role="user", content="", images=[image]))]
            report.update({"image": collect_request_metadata("", "", payload)["images"][0],
                           "preparation": preparation})
            if args.run_model:
                stage = "configuration"
                report.update({"role": args.role, "target": args.target})
                client = configured_diagnostic_client(
                    args.config, args.role, args.target, args.vault_dir)
                report["request_summary"] = collect_request_metadata(
                    client.model, client.base_url, payload)
                client.client = client.client.with_options(timeout=args.timeout_seconds, max_retries=0)
                stage = "request"
                report.update(run_vision_probe(client, image, args.ground_truth))
            elif args.response_json:
                stage = "response"
                diagnosis = parse_diagnosis(args.response_json.read_text(encoding="utf-8"))
                report["diagnosis"] = diagnosis.to_dict()
                if args.ground_truth is not None:
                    report["score"] = score_bbox(diagnosis.input_box, args.ground_truth)
                report["success"] = True
            else:
                report["success"] = True
    except Exception as exc:
        report["diagnostic_error"] = type(exc).__name__
        report["error"] = safe_diagnostic_error(exc, stage)
    rendered = json.dumps(report, ensure_ascii=False, indent=2)
    try:
        if args.output:
            args.output.parent.mkdir(parents=True, exist_ok=True)
            args.output.write_text(rendered + "\n", encoding="utf-8")
    except Exception as exc:
        # Keep diagnostics available on stdout even when the chosen output cannot be written.
        report["success"] = False
        report["report_write_error"] = type(exc).__name__
        rendered = json.dumps(report, ensure_ascii=False, indent=2)
    print(rendered)
    return 0 if report["success"] else 2


if __name__ == "__main__":
    raise SystemExit(main())
