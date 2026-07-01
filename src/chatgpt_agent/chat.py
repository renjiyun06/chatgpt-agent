"""High-level operations on a ChatGPT conversation.

Send path: `wire.send` → page-context fetch to /backend-api/f/conversation,
streamed via SSE inside the page. No DOM, no React fiber, no composer.

Read path: page-context fetch to /backend-api/conversation/<id> for the
canonical mapping (citations, image refs, full content). Image bytes
download via /backend-api/files/download.
"""
from __future__ import annotations

import base64
import json
import os
import sys
import time
from dataclasses import dataclass, field
from pathlib import Path

from . import paths, upload as upload_mod, wire
from .browser import Browser, BrowserError

_MIME_EXT = {
    "image/png": "png",
    "image/jpeg": "jpg",
    "image/webp": "webp",
    "image/gif": "gif",
}


@dataclass
class ImageAsset:
    url: str
    file_id: str
    mime: str
    path: Path


@dataclass
class Reply:
    message_id: str | None
    text: str
    model_slug: str | None
    images: list[ImageAsset] = field(default_factory=list)


@dataclass
class Turn:
    """One turn from a conversation mapping. Roles: 'user', 'assistant',
    'tool', 'system'."""
    role: str
    text: str
    message_id: str | None
    model_slug: str | None
    image_file_ids: list[str]


def _debug(msg: str) -> None:
    if os.environ.get("CHATGPT_AGENT_DEBUG"):
        print(f"[chatgpt-agent] {msg}", file=sys.stderr, flush=True)


# ----------------------------------------------------------- mapping fetch

def _get_mapping(b: Browser, conv_id: str) -> dict:
    """Pull the full conversation mapping. Dict has 'mapping',
    'current_node', 'title', etc."""
    return b.http_get(f"/backend-api/conversation/{conv_id}")


def _is_404(err_str: str) -> bool:
    return "http 404" in err_str or "conversation_not_found" in err_str


def _wait_for_finish(
    b: Browser,
    conv_id: str,
    *,
    baseline_leaf_id: str | None,
    expected_msg_id: str | None = None,
    timeout_s: float = 300.0,
    poll_s: float = 0.4,
) -> dict:
    """Re-fetch the mapping until the assistant message is fully written.

    `wire.send` returns when the SSE stream closes, but the canonical
    content (citations, image refs, full text) is what the mapping
    endpoint reflects, and on the thinking-model handoff path the
    deltas continue arriving on a separate channel after the SSE
    closes. So we poll the mapping here.

    Termination: the conversation's current_node must point to a
    NEW assistant leaf (different from the leaf observed before the
    send) with end_turn=true. The baseline-leaf check is what
    prevents us from instantly returning the previous turn's reply
    on a follow-up — without it, the very first poll sees the same
    old leaf still sitting at current_node and reports it as today's
    answer.

    `expected_msg_id`: if SSE handed us the new assistant message id
    (it does for instant-model paths; thinking-handoff often does
    not), require an exact match for added safety. Optional — the
    baseline check alone is enough for correctness.
    """
    deadline = time.monotonic() + timeout_s
    last_state: tuple = ()
    # When the SSE stream hands off to a different transport (thinking
    # models, generative-image flows), the conversation/<id> mapping
    # endpoint can briefly 404 while the server is still assembling
    # the new conversation. Tolerate that for a short window before
    # giving up — but cap it, so a genuinely-bad id surfaces quickly
    # instead of hanging the full timeout.
    consecutive_404 = 0
    max_404 = max(15, int(15 / poll_s))  # ≈ 15s of "not yet ready"
    while time.monotonic() < deadline:
        try:
            m = _get_mapping(b, conv_id)
        except BrowserError as e:
            if _is_404(str(e)):
                consecutive_404 += 1
                if consecutive_404 > max_404:
                    raise BrowserError(f"conversation not found: {conv_id}") from e
                time.sleep(poll_s)
                continue
            time.sleep(poll_s)
            continue
        consecutive_404 = 0
        cur = m.get("current_node")
        mapping = m.get("mapping") or {}
        node = mapping.get(cur) or {}
        msg = node.get("message") or {}
        role = ((msg.get("author") or {}).get("role"))
        end_turn = msg.get("end_turn")
        ct = ((msg.get("content") or {}).get("content_type"))
        last_state = (cur, role, ct, end_turn)
        # Two gates, both must pass:
        #   1. The leaf has advanced past the one we saw before sending.
        #      For new_session, baseline is None and the very first
        #      assistant leaf qualifies. For send_message, baseline is
        #      the previous turn's leaf — we wait for the model to
        #      write a fresh one.
        is_new_leaf = cur != baseline_leaf_id
        #   2. The leaf is a finished assistant message. Tool / user /
        #      system leaves never carry end_turn=true, so role + flag
        #      is enough — no need to constrain content_type (which
        #      varies between text replies, image replies, etc.).
        is_finished_assistant = role == "assistant" and end_turn is True
        # Optional extra safety when SSE gave us the precise id.
        match_id = (
            expected_msg_id is None
            or msg.get("id") == expected_msg_id
        )
        if is_new_leaf and is_finished_assistant and match_id:
            return m
        time.sleep(poll_s)
    raise BrowserError(
        f"timed out after {timeout_s}s waiting for mapping to reflect "
        f"finished assistant message in {conv_id} (last_state={last_state})"
    )


# -------------------------------------------------- mapping -> Reply / Turn

def _walk_reply_chain(mapping_resp: dict) -> list[dict]:
    """Walk back from current_node up to (but not including) the most
    recent user/system message. Returns the assistant + tool messages
    making up the latest reply, oldest-first.
    """
    mapping = mapping_resp.get("mapping") or {}
    cur_id = mapping_resp.get("current_node")
    chain: list[dict] = []
    seen: set[str] = set()
    while cur_id and cur_id not in seen:
        seen.add(cur_id)
        node = mapping.get(cur_id) or {}
        msg = node.get("message")
        if not msg:
            break
        role = ((msg.get("author") or {}).get("role"))
        if role in ("user", "system"):
            break
        chain.append(msg)
        cur_id = node.get("parent")
    chain.reverse()
    return chain


def _resolve_content_references(text: str, refs: list[dict] | None) -> str:
    """Replace ChatGPT's inline citation markers (delimited by private-use
    Unicode chars like U+E200/E202/E201) with the resolved markdown link
    in `metadata.content_references[*].alt`.

    `matched_text` includes the invisible delimiters, so .replace() is safe.
    """
    if not refs:
        return text
    for ref in refs:
        matched = ref.get("matched_text")
        if not matched:
            continue
        alt = ref.get("alt") or ""
        text = text.replace(matched, alt)
    return text


def _extract_reply(mapping_resp: dict) -> dict:
    """Pull the latest reply (text + images + meta) from a mapping JSON.

    Returns a dict with: message_id, text, model_slug, image_refs.
    image_refs is [{file_id, width, height, size_bytes}, ...].
    """
    text_parts: list[str] = []
    image_refs: list[dict] = []
    seen_files: set[str] = set()
    msg_id: str | None = None
    model_slug: str | None = None
    for msg in _walk_reply_chain(mapping_resp):
        role = ((msg.get("author") or {}).get("role"))
        content = msg.get("content") or {}
        ct = content.get("content_type")
        parts = content.get("parts") or []
        meta = msg.get("metadata") or {}
        if role == "assistant" and ct == "text":
            refs = meta.get("content_references")
            for p in parts:
                if isinstance(p, str) and p:
                    text_parts.append(_resolve_content_references(p, refs))
            msg_id = msg.get("id") or msg_id
            slug = meta.get("model_slug")
            if slug:
                model_slug = slug
        for p in parts:
            if isinstance(p, dict) and p.get("content_type") == "image_asset_pointer":
                ap = p.get("asset_pointer") or ""
                fid = ap.replace("sediment://", "").replace("file-service://", "")
                if fid and fid not in seen_files:
                    seen_files.add(fid)
                    image_refs.append({
                        "file_id": fid,
                        "width": p.get("width"),
                        "height": p.get("height"),
                        "size_bytes": p.get("size_bytes"),
                    })
    return {
        "message_id": msg_id,
        "text": "\n".join(text_parts),
        "model_slug": model_slug,
        "image_refs": image_refs,
    }


def _extract_all_turns(mapping_resp: dict) -> list[dict]:
    """Linearize the conversation by walking from root to current_node.
    Each visited message becomes one turn. Branches not on the active
    path are skipped.
    """
    mapping = mapping_resp.get("mapping") or {}
    cur_id = mapping_resp.get("current_node")
    path: list[dict] = []
    seen: set[str] = set()
    while cur_id and cur_id not in seen:
        seen.add(cur_id)
        node = mapping.get(cur_id) or {}
        path.append(node)
        cur_id = node.get("parent")
    path.reverse()
    turns: list[dict] = []
    for node in path:
        msg = node.get("message")
        if not msg:
            continue
        role = ((msg.get("author") or {}).get("role"))
        if role == "system":
            continue
        content = msg.get("content") or {}
        ct = content.get("content_type")
        parts = content.get("parts") or []
        meta = msg.get("metadata") or {}
        refs = meta.get("content_references") if role == "assistant" and ct == "text" else None
        text_parts = [
            _resolve_content_references(p, refs) if refs else p
            for p in parts if isinstance(p, str)
        ]
        image_file_ids: list[str] = []
        for p in parts:
            if isinstance(p, dict) and p.get("content_type") == "image_asset_pointer":
                ap = p.get("asset_pointer") or ""
                fid = ap.replace("sediment://", "").replace("file-service://", "")
                if fid:
                    image_file_ids.append(fid)
        turns.append({
            "role": role,
            "text": "\n".join(text_parts),
            "content_type": ct,
            "message_id": msg.get("id"),
            "model_slug": meta.get("model_slug"),
            "image_file_ids": image_file_ids,
        })
    return turns


# -------------------------------------------------------------- images

def _download_image(b: Browser, conv_id: str, file_id: str) -> tuple[str, bytes]:
    """Fetch a file's bytes via /backend-api/files/download, which returns
    a signed URL + metadata. We then fetch the signed URL through the
    page (same-origin) to get the bytes.
    """
    info = b.http_get(
        f"/backend-api/files/download/{file_id}"
        f"?conversation_id={conv_id}&inline=false"
    )
    download_url = info.get("download_url")
    if not download_url:
        raise BrowserError(f"no download_url for file {file_id}: {info}")
    mime = info.get("mime_type")
    if not mime:
        fn = info.get("file_name") or ""
        ext = fn.rsplit(".", 1)[-1].lower() if "." in fn else ""
        mime = {
            "png": "image/png", "jpg": "image/jpeg", "jpeg": "image/jpeg",
            "webp": "image/webp", "gif": "image/gif",
        }.get(ext, "application/octet-stream")
    b64 = b.evaluate(
        "async () => {"
        f"  const r = await fetch({json.dumps(download_url)});"
        "  if (!r.ok) throw new Error('download ' + r.status);"
        "  const bytes = new Uint8Array(await r.arrayBuffer());"
        "  let bin = ''; const CHUNK = 0x8000;"
        "  for (let i = 0; i < bytes.length; i += CHUNK) {"
        "    bin += String.fromCharCode.apply(null, bytes.subarray(i, i + CHUNK));"
        "  }"
        "  return btoa(bin);"
        "}"
    )
    return mime, base64.b64decode(b64)


def _save_image(profile: str, conv_id: str, file_id: str, mime: str, blob: bytes) -> Path:
    out_dir = paths.images_dir(profile, conv_id)
    out_dir.mkdir(parents=True, exist_ok=True)
    ext = _MIME_EXT.get(mime, "bin")
    p = out_dir / f"{file_id}.{ext}"
    p.write_bytes(blob)
    return p


def _materialize_images(
    b: Browser, profile: str, conv_id: str, refs: list[dict]
) -> list[ImageAsset]:
    out: list[ImageAsset] = []
    for ref in refs:
        fid = ref["file_id"]
        try:
            mime, blob = _download_image(b, conv_id, fid)
        except BrowserError as e:
            _debug(f"image download failed for {fid}: {e}")
            continue
        path = _save_image(profile, conv_id, fid, mime, blob)
        out.append(ImageAsset(url="", file_id=fid, mime=mime, path=path))
    return out


# ---------------------------------------------------------------- public api

def _materialize_attachments(b: Browser, attach_paths: list[str | Path] | None) -> list[dict]:
    """Upload each path and return the list of attachment dicts that
    `wire.send` consumes. Empty input -> empty list (no upload calls).
    Failures surface as BrowserError, aborting before /f/conversation
    so we never send a half-attached message.
    """
    if not attach_paths:
        return []
    out: list[dict] = []
    for ap in attach_paths:
        u = upload_mod.upload_file(b, ap)
        out.append({
            "file_id": u.file_id,
            "name": u.name,
            "size": u.size,
            "mime_type": u.mime_type,
            "library_file_id": u.library_file_id,
            "width": u.width,
            "height": u.height,
        })
    return out


def new_session(
    b: Browser, profile: str, initial_message: str,
    *, model: str = "gpt-5-5-thinking", thinking_effort: str = "extended",
    attachments: list[str | Path] | None = None,
) -> tuple[str, Reply]:
    """Start a new conversation with `initial_message`; return its id +
    the assistant reply. `attachments` is a list of local file paths to
    upload and reference from the first message."""
    if not initial_message or not initial_message.strip():
        raise BrowserError("new: --initial cannot be empty")
    atts = _materialize_attachments(b, attachments)
    result = wire.send(b, initial_message, model=model, thinking_effort=thinking_effort, attachments=atts)
    # New conversation: baseline is None (any assistant end_turn is fresh).
    mapping = _wait_for_finish(
        b, result.conversation_id,
        baseline_leaf_id=None,
        expected_msg_id=result.assistant_msg_id,
    )
    reply_data = _extract_reply(mapping)
    images = _materialize_images(b, profile, result.conversation_id, reply_data["image_refs"])
    return result.conversation_id, Reply(
        message_id=reply_data["message_id"],
        text=reply_data["text"],
        model_slug=reply_data["model_slug"],
        images=images,
    )


def send_message(
    b: Browser, profile: str, conv_id: str, message: str,
    *, model: str = "gpt-5-5-thinking", thinking_effort: str = "extended",
    attachments: list[str | Path] | None = None,
) -> Reply:
    """Append `message` to existing conversation `conv_id`. `attachments`
    is a list of local file paths uploaded before the send."""
    if not message or not message.strip():
        raise BrowserError("send: message cannot be empty")
    # Read the current leaf to use as parent_message_id. Doubles as a
    # cheap existence check — 404 here means the conv id is bad.
    baseline = _get_mapping(b, conv_id)
    parent_msg_id = baseline.get("current_node")
    if not parent_msg_id:
        raise BrowserError(f"conversation {conv_id} has no current_node")
    atts = _materialize_attachments(b, attachments)
    result = wire.send(
        b, message, conv_id=conv_id, parent_msg_id=parent_msg_id,
        model=model, thinking_effort=thinking_effort, attachments=atts,
    )
    # Wait for a NEW leaf — without baseline_leaf_id pin, the very
    # first poll would see the previous turn's still-current leaf and
    # mis-report it as today's reply.
    mapping = _wait_for_finish(
        b, conv_id,
        baseline_leaf_id=parent_msg_id,
        expected_msg_id=result.assistant_msg_id,
    )
    reply_data = _extract_reply(mapping)
    images = _materialize_images(b, profile, conv_id, reply_data["image_refs"])
    return Reply(
        message_id=reply_data["message_id"],
        text=reply_data["text"],
        model_slug=reply_data["model_slug"],
        images=images,
    )


def list_conversations(b: Browser, *, limit: int = 200) -> list[dict]:
    """List conversations via /backend-api/conversations."""
    items: list[dict] = []
    offset = 0
    page_size = 100
    while len(items) < limit:
        want = min(page_size, limit - len(items))
        path = (
            f"/backend-api/conversations?offset={offset}&limit={want}"
            f"&order=updated&is_archived=false"
        )
        try:
            page = b.http_get(path)
        except BrowserError as e:
            raise BrowserError(f"list_conversations failed: {e}") from e
        rows = page.get("items") or page.get("conversations") or []
        if not rows:
            break
        items.extend(rows)
        if len(rows) < want:
            break
        offset += want
    return items


def dump_conversation(
    b: Browser, profile: str, conv_id: str, *, download_images: bool = True
) -> dict:
    """Pull all turns from a conversation by fetching its full mapping.
    Images are downloaded by default into the per-profile cache dir.
    """
    mapping_resp = _get_mapping(b, conv_id)
    turns = _extract_all_turns(mapping_resp)
    image_paths: dict[str, str] = {}
    if download_images:
        unique_fids: list[str] = []
        seen: set[str] = set()
        for t in turns:
            for fid in t.get("image_file_ids", []):
                if fid not in seen:
                    seen.add(fid)
                    unique_fids.append(fid)
        for fid in unique_fids:
            try:
                mime, blob = _download_image(b, conv_id, fid)
            except BrowserError as e:
                _debug(f"image download failed for {fid}: {e}")
                continue
            p = _save_image(profile, conv_id, fid, mime, blob)
            image_paths[fid] = str(p)
    return {"id": conv_id, "turns": turns, "image_paths": image_paths}
