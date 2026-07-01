"""Style-consistent image suite generation through ChatGPT web."""
from __future__ import annotations

import json
import re
import shutil
import time
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from . import chat
from .browser import Browser


DEFAULT_SLEEP_S = 10.0


@dataclass
class SuiteItem:
    name: str
    brief: str
    aspect: str | None = None
    attachments: list[str] = field(default_factory=list)


@dataclass
class SuiteSpec:
    series_name: str
    master_brief: str
    style: str | None
    negative: str | None
    attachments: list[str]
    items: list[SuiteItem]


def _string(value: Any, field_name: str, *, required: bool = True) -> str | None:
    if value is None:
        if required:
            raise ValueError(f"missing required field: {field_name}")
        return None
    if not isinstance(value, str) or not value.strip():
        if required:
            raise ValueError(f"{field_name} must be a non-empty string")
        return None
    return value.strip()


def _string_list(value: Any, field_name: str) -> list[str]:
    if value is None:
        return []
    if not isinstance(value, list):
        raise ValueError(f"{field_name} must be a list")
    out: list[str] = []
    for idx, item in enumerate(value):
        if not isinstance(item, str) or not item.strip():
            raise ValueError(f"{field_name}[{idx}] must be a non-empty string")
        out.append(item.strip())
    return out


def load_spec(path: str | Path) -> SuiteSpec:
    """Load and validate a suite spec JSON file."""
    p = Path(path).expanduser().resolve()
    data = json.loads(p.read_text(encoding="utf-8"))
    if not isinstance(data, dict):
        raise ValueError("suite spec must be a JSON object")

    raw_items = data.get("items")
    if not isinstance(raw_items, list) or not raw_items:
        raise ValueError("items must be a non-empty list")

    items: list[SuiteItem] = []
    for idx, row in enumerate(raw_items, start=1):
        if not isinstance(row, dict):
            raise ValueError(f"items[{idx - 1}] must be an object")
        name = _string(row.get("name"), f"items[{idx - 1}].name") or f"item-{idx:02d}"
        brief = _string(row.get("brief"), f"items[{idx - 1}].brief") or ""
        items.append(SuiteItem(
            name=name,
            brief=brief,
            aspect=_string(row.get("aspect"), f"items[{idx - 1}].aspect", required=False),
            attachments=_string_list(row.get("attachments"), f"items[{idx - 1}].attachments"),
        ))

    series_name = _string(data.get("series_name"), "series_name", required=False)
    return SuiteSpec(
        series_name=series_name or p.stem,
        master_brief=_string(data.get("master_brief"), "master_brief") or "",
        style=_string(data.get("style"), "style", required=False),
        negative=_string(data.get("negative"), "negative", required=False),
        attachments=_string_list(data.get("attachments"), "attachments"),
        items=items,
    )


def _slug(text: str, fallback: str) -> str:
    slug = re.sub(r"[^A-Za-z0-9._-]+", "_", text).strip("._-").lower()
    return slug[:64] or fallback


def _copy_images(reply: chat.Reply, output_dir: Path, item_slug: str) -> list[dict]:
    output_dir.mkdir(parents=True, exist_ok=True)
    copied: list[dict] = []
    for idx, asset in enumerate(reply.images, start=1):
        suffix = Path(asset.path).suffix or ".png"
        name = f"{item_slug}{suffix}" if len(reply.images) == 1 else f"{item_slug}_{idx}{suffix}"
        dest = output_dir / name
        collision = 1
        while dest.exists():
            dest = output_dir / f"{item_slug}_{collision}{suffix}"
            collision += 1
        shutil.copy2(asset.path, dest)
        copied.append({
            "file_id": asset.file_id,
            "mime": asset.mime,
            "source_path": str(asset.path),
            "path": str(dest),
        })
    return copied


def build_prompt(spec: SuiteSpec, item: SuiteItem, *, index: int) -> str:
    """Build the exact prompt sent to ChatGPT for one suite item."""
    total = len(spec.items)
    aspect = item.aspect or "use the best aspect ratio for the brief"
    shared = [
        "You are generating a coherent image suite with ChatGPT native image generation.",
        "Always produce an actual image in this turn; do not only describe the image.",
        f"Suite name: {spec.series_name}",
        f"Overall suite brief: {spec.master_brief}",
    ]
    if spec.style:
        shared.append(f"Required visual system: {spec.style}")
    if spec.negative:
        shared.append(f"Avoid: {spec.negative}")

    if index == 0:
        shared.extend([
            "This is the first image. Establish a reusable visual system for the whole suite.",
            "Keep the design language specific enough that later images can match it.",
        ])
    else:
        shared.extend([
            f"This is image {index + 1} of {total}.",
            "Maintain the same visual system, palette, lighting, rendering quality, and composition logic as the earlier images in this conversation.",
            "Change only the subject matter required by this item.",
        ])

    shared.extend([
        f"Item name: {item.name}",
        f"Item brief: {item.brief}",
        f"Aspect ratio: {aspect}",
        "Text inside the image should be avoided unless the brief explicitly requires it.",
        "Return the generated image first. A short note after the image is acceptable.",
    ])
    return "\n".join(shared)


def dry_run_plan(spec: SuiteSpec) -> dict:
    return {
        "series_name": spec.series_name,
        "master_brief": spec.master_brief,
        "style": spec.style,
        "negative": spec.negative,
        "attachments": spec.attachments,
        "items": [
            {
                "index": idx + 1,
                "name": item.name,
                "brief": item.brief,
                "aspect": item.aspect,
                "attachments": item.attachments,
                "prompt": build_prompt(spec, item, index=idx),
            }
            for idx, item in enumerate(spec.items)
        ],
    }


def run_suite(
    b: Browser,
    profile: str,
    spec: SuiteSpec,
    output_dir: str | Path,
    *,
    model: str = "gpt-5-5-thinking",
    thinking_effort: str = "extended",
    sleep_s: float = DEFAULT_SLEEP_S,
    continue_on_error: bool = False,
) -> dict:
    """Generate a suite sequentially in one ChatGPT conversation."""
    out_dir = Path(output_dir).expanduser().resolve()
    out_dir.mkdir(parents=True, exist_ok=True)

    manifest: dict[str, Any] = {
        "series_name": spec.series_name,
        "master_brief": spec.master_brief,
        "style": spec.style,
        "negative": spec.negative,
        "model": model,
        "thinking_effort": thinking_effort,
        "created_at": datetime.now(timezone.utc).isoformat(),
        "conversation_id": None,
        "output_dir": str(out_dir),
        "items": [],
    }

    conv_id: str | None = None
    for idx, item in enumerate(spec.items):
        prompt = build_prompt(spec, item, index=idx)
        attachments = list(item.attachments)
        if idx == 0:
            attachments = list(spec.attachments) + attachments
        item_slug = f"{idx + 1:02d}_{_slug(item.name, f'item_{idx + 1:02d}')}"
        row: dict[str, Any] = {
            "index": idx + 1,
            "name": item.name,
            "brief": item.brief,
            "aspect": item.aspect,
            "prompt": prompt,
            "attachments": attachments,
            "status": "pending",
            "images": [],
        }
        try:
            if idx == 0:
                conv_id, reply = chat.new_session(
                    b, profile, prompt,
                    model=model,
                    thinking_effort=thinking_effort,
                    attachments=attachments or None,
                )
                manifest["conversation_id"] = conv_id
            else:
                if sleep_s > 0:
                    time.sleep(sleep_s)
                if not conv_id:
                    raise RuntimeError("missing conversation id after first item")
                reply = chat.send_message(
                    b, profile, conv_id, prompt,
                    model=model,
                    thinking_effort=thinking_effort,
                    attachments=attachments or None,
                )
            row["reply_text"] = reply.text
            row["message_id"] = reply.message_id
            row["model_slug"] = reply.model_slug
            row["images"] = _copy_images(reply, out_dir, item_slug)
            if not row["images"]:
                row["status"] = "no_images"
                row["error"] = "ChatGPT reply did not expose any downloadable image assets"
                manifest["items"].append(row)
                if not continue_on_error:
                    break
                continue
            row["status"] = "success"
        except Exception as e:  # noqa: BLE001 - persist the failure in the manifest.
            row["status"] = "failed"
            row["error"] = str(e)
            manifest["items"].append(row)
            if not continue_on_error:
                break
            continue
        manifest["items"].append(row)

    manifest["success_count"] = sum(1 for item in manifest["items"] if item.get("status") == "success")
    manifest["total_count"] = len(spec.items)
    manifest_path = out_dir / "manifest.json"
    manifest_path.write_text(json.dumps(manifest, indent=2, ensure_ascii=False), encoding="utf-8")
    manifest["manifest_path"] = str(manifest_path)
    return manifest
