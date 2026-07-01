"""Upload a local file into ChatGPT's library so it can be referenced
from a chat message. Page-context fetch only, no UI.

The wire shape (mapped via experiments/upload/probe_drive_v2.py):

    1. POST /backend-api/files
         { file_name, file_size, use_case: "multimodal",
           timezone_offset_min, reset_rate_limits: false,
           store_in_library: true,
           library_persistence_mode: "opportunistic" }
       -> { status, upload_url, file_id }
       upload_url is a pre-signed Azure-blob SAS URL on
       *.oaiusercontent.com — auth is in the query string, no Bearer.

    2. PUT <upload_url>
         body = raw file bytes
         headers: Content-Type: <mime>, x-ms-blob-type: BlockBlob
       -> 201 Created (empty body)

    3. POST /backend-api/files/process_upload_stream
         { file_id, use_case, file_name, ...
           index_for_retrieval: false,
           library_persistence_mode: "opportunistic",
           metadata: { store_in_library: true },
           entry_surface: "chat_composer" }
       -> SSE: a series of events ending with
          file.processing.completed. Along the way,
          file.indexing.completed carries
          extra.metadata_object_id (libfile_*) + mime_type.

After step 3 the file_id is referenced in the next /f/conversation
request body via metadata.attachments[*] and (for images) a
multimodal_text content part with image_asset_pointer.

This module owns JS that runs in the page so it can call same-origin
fetch with cookies attached. The Python side reads the file off disk,
sends bytes to JS as base64, and asks for the upload outcome. Returns
a dict the caller (chat module) can splice into wire.send.
"""
from __future__ import annotations

import base64
import json
import mimetypes
import struct
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from .browser import Browser, BrowserError

# These are the same identity headers wire.py and browser.py use; kept
# in sync with the page bundle. Bumping these requires no behavior
# change here, just refreshed values.
_OAI_CLIENT_VERSION = "prod-5d86787f9f8d1f6b6e7e021b6aa4d6b14a14445c"
_OAI_CLIENT_BUILD = "6232230"


# ----------------------------------------------------------- file metadata

@dataclass
class UploadedFile:
    """Outcome of `upload_file`. The fields here are exactly what the
    chat send path needs to build the message body — no more, no less."""
    file_id: str           # "file_..." — used in asset_pointer + attachments[*].id
    name: str              # original file name
    size: int              # byte count of what was uploaded
    mime_type: str         # e.g. "image/png" — from the server's indexing event
    library_file_id: str   # "libfile_..." — used as attachments[*].library_file_id
    width: int | None      # image-only, None for non-images
    height: int | None


def _guess_mime(path: Path) -> str:
    """Best-effort MIME from extension. The server overrides this with
    its own detection (returned in the SSE 'file.indexing.completed'
    event), but we still need a value for the Azure PUT's Content-Type
    header so the blob is stored with sensible metadata."""
    mt, _ = mimetypes.guess_type(path.name)
    if mt:
        return mt
    return "application/octet-stream"


def _image_dimensions(path: Path) -> tuple[int, int] | None:
    """Return (width, height) for PNG/JPEG/GIF images, else None.

    No PIL dependency — we parse the few well-known header layouts
    directly. This is enough for the formats ChatGPT actually accepts
    as inline images (PNG/JPEG/GIF/WebP). Fallback for anything
    unrecognized is None; the message body just omits dimensions.
    """
    try:
        with path.open("rb") as f:
            head = f.read(32)
    except OSError:
        return None
    # PNG: signature is 8 bytes, then IHDR chunk: 4-byte len, 4-byte
    # type "IHDR", 4-byte width, 4-byte height (big-endian).
    if head[:8] == b"\x89PNG\r\n\x1a\n":
        try:
            return struct.unpack(">II", head[16:24])
        except struct.error:
            return None
    # GIF: bytes 6..10 hold width then height as little-endian uint16.
    if head[:6] in (b"GIF87a", b"GIF89a"):
        try:
            return struct.unpack("<HH", head[6:10])
        except struct.error:
            return None
    # JPEG: scan SOF markers (0xFFC0..0xFFCF excluding DHT 0xC4 / DAC 0xCC / DRI 0xCD).
    if head[:2] == b"\xff\xd8":
        try:
            with path.open("rb") as f:
                f.seek(2)
                while True:
                    while True:
                        b = f.read(1)
                        if not b:
                            return None
                        if b == b"\xff":
                            break
                    while True:
                        m = f.read(1)
                        if m != b"\xff":
                            break
                    if not m:
                        return None
                    marker = m[0]
                    if 0xC0 <= marker <= 0xCF and marker not in (0xC4, 0xCC, 0xC8, 0xCD):
                        f.read(3)  # length(2) + precision(1)
                        h, w = struct.unpack(">HH", f.read(4))
                        return (w, h)
                    seg_len = struct.unpack(">H", f.read(2))[0]
                    f.seek(seg_len - 2, 1)
        except (OSError, struct.error):
            return None
    # WebP: "RIFF????WEBP" + chunk; skip — most users send PNG/JPEG.
    return None


# ----------------------------------------------------------- page-context JS

# All three steps in one round-trip to keep latency low and make the
# error path simple (one Python-visible failure point per upload).
#
# The PUT in step 2 uses `xhr.send(blob)` rather than fetch with a
# ReadableStream body — Chrome's fetch sometimes refuses cross-origin
# PUTs to oaiusercontent.com from the chatgpt.com origin even though
# the SAS URL is otherwise valid; XHR works in every observed case
# and matches what the React app does.
_UPLOAD_JS = r"""
async ({ name, size, mime, blob_b64 }) => {
  try {
    return await (async () => {
      function getCookie(n) {
        for (const c of document.cookie.split('; ')) {
          const i = c.indexOf('=');
          if (c.slice(0, i) === n) return decodeURIComponent(c.slice(i+1));
        }
        return null;
      }
      async function fetchJson(url, init) {
        const r = await fetch(url, init);
        const t = await r.text();
        if (!r.ok) throw new Error('http ' + r.status + ' at ' + url + ': ' + t.slice(0, 300));
        try { return JSON.parse(t); }
        catch (e) { throw new Error('bad_json at ' + url + ': ' + t.slice(0, 300)); }
      }
      // Decode base64 -> Uint8Array (Blob-friendly).
      function b64ToBytes(s) {
        const bin = atob(s);
        const out = new Uint8Array(bin.length);
        for (let i = 0; i < bin.length; i++) out[i] = bin.charCodeAt(i);
        return out;
      }

      // Identity (same shape as wire.py).
      const sess = await fetchJson('/api/auth/session');
      if (!sess || !sess.accessToken) return { error: 'not_logged_in' };
      const oaiDid = JSON.parse(localStorage.getItem('oai-did') || '""');
      let sessionId = null;
      for (let i = 0; i < localStorage.length; i++) {
        const k = localStorage.key(i);
        if (k && k.startsWith('statsig.session_id.')) {
          try { sessionId = JSON.parse(localStorage.getItem(k)).sessionID; break; } catch (_) {}
        }
      }
      if (!sessionId) sessionId = crypto.randomUUID();
      const baseHeaders = {
        'authorization': 'Bearer ' + sess.accessToken,
        'content-type': 'application/json',
        'oai-client-build-number': '__OAI_BUILD__',
        'oai-client-version': '__OAI_VERSION__',
        'oai-device-id': oaiDid,
        'oai-language': 'en-US',
        'oai-session-id': sessionId,
        'x-oai-is': getCookie('__Secure-oai-is'),
      };
      const tzMin = new Date().getTimezoneOffset() * -1;

      // Step 1: register the file, receive upload_url + file_id.
      const create = await fetchJson('/backend-api/files', {
        method: 'POST',
        headers: Object.assign({}, baseHeaders, {
          'x-openai-target-path': '/backend-api/files',
          'x-openai-target-route': '/backend-api/files',
        }),
        body: JSON.stringify({
          file_name: name,
          file_size: size,
          use_case: 'multimodal',
          timezone_offset_min: tzMin,
          reset_rate_limits: false,
          store_in_library: true,
          library_persistence_mode: 'opportunistic',
        }),
      });
      if (create.status !== 'success' || !create.upload_url || !create.file_id) {
        return { error: 'files_create_unexpected', detail: create };
      }
      const file_id = create.file_id;

      // Step 2: PUT bytes to the Azure SAS URL via XHR. The SAS URL
      // carries auth in the query string; we MUST include the
      // x-ms-blob-type header (Azure rejects the PUT without it) and
      // a sensible Content-Type so the blob is stored with metadata.
      const bytes = b64ToBytes(blob_b64);
      const putStatus = await new Promise((resolve, reject) => {
        const xhr = new XMLHttpRequest();
        xhr.open('PUT', create.upload_url);
        xhr.setRequestHeader('x-ms-blob-type', 'BlockBlob');
        xhr.setRequestHeader('Content-Type', mime);
        xhr.onload = () => resolve(xhr.status);
        xhr.onerror = () => reject(new Error('xhr_network_error PUT ' + create.upload_url));
        xhr.send(bytes);
      });
      if (putStatus < 200 || putStatus >= 300) {
        return { error: 'azure_put_failed', status: putStatus };
      }

      // Step 3: trigger server-side processing; drain the SSE stream
      // until file.processing.completed and capture the indexing
      // event's extra metadata along the way.
      const procResp = await fetch('/backend-api/files/process_upload_stream', {
        method: 'POST',
        headers: Object.assign({}, baseHeaders, {
          accept: 'text/event-stream',
          'x-openai-target-path': '/backend-api/files/process_upload_stream',
          'x-openai-target-route': '/backend-api/files/process_upload_stream',
        }),
        body: JSON.stringify({
          file_id,
          use_case: 'multimodal',
          index_for_retrieval: false,
          file_name: name,
          library_persistence_mode: 'opportunistic',
          metadata: { store_in_library: true },
          entry_surface: 'chat_composer',
        }),
      });
      if (!procResp.ok) {
        let body = ''; try { body = await procResp.text(); } catch (_) {}
        return { error: 'process_upload_stream_http', status: procResp.status, body: body.slice(0, 500) };
      }
      // Drain the SSE. Events are NDJSON-style — one JSON object per
      // line, no `data: ` prefix in the observed traffic. Parse loosely
      // so we don't choke on any blank lines.
      const reader = procResp.body.getReader();
      const dec = new TextDecoder();
      let buf = '';
      let library_file_id = null;
      let server_mime = null;
      let completed = false;
      drain: while (true) {
        const { value, done } = await reader.read();
        if (done) break;
        buf += dec.decode(value, { stream: true });
        let nl;
        while ((nl = buf.indexOf('\n')) !== -1) {
          const line = buf.slice(0, nl).trim(); buf = buf.slice(nl + 1);
          if (!line) continue;
          let j;
          try { j = JSON.parse(line); } catch (_) { continue; }
          if (j.event === 'file.indexing.completed' && j.extra) {
            library_file_id = j.extra.metadata_object_id || null;
            server_mime = j.extra.mime_type || null;
          }
          if (j.event === 'file.processing.completed') { completed = true; break drain; }
        }
      }
      try { await reader.cancel(); } catch (_) {}
      if (!completed) return { error: 'process_did_not_complete' };

      return {
        file_id,
        library_file_id,
        mime_type: server_mime || mime,
      };
    })();
  } catch (e) {
    return { error: 'js_exception', message: String(e && e.message || e), stack: String(e && e.stack || '').slice(0, 1500) };
  }
}
""".replace("__OAI_VERSION__", _OAI_CLIENT_VERSION).replace("__OAI_BUILD__", _OAI_CLIENT_BUILD)


# --------------------------------------------------------------- public api

def upload_file(b: Browser, path: str | Path, *, timeout_s: float = 180.0) -> UploadedFile:
    """Upload `path` to ChatGPT's user library; return its `file_id`
    and the metadata the chat send path needs.

    Raises BrowserError if any step (create / PUT / process) fails.
    The conversation send path consumes the returned object; callers
    don't talk to /backend-api/files directly.
    """
    p = Path(path).expanduser().resolve()
    if not p.is_file():
        raise BrowserError(f"upload: file not found: {p}")
    blob = p.read_bytes()
    if not blob:
        raise BrowserError(f"upload: empty file: {p}")
    mime = _guess_mime(p)
    dims = _image_dimensions(p)
    args = {
        "name": p.name,
        "size": len(blob),
        "mime": mime,
        "blob_b64": base64.b64encode(blob).decode("ascii"),
    }
    out: dict[str, Any] = b.evaluate(_UPLOAD_JS, args, timeout_s=timeout_s) or {}
    if "error" in out:
        kind = out["error"]
        if kind == "js_exception":
            raise BrowserError(f"upload js_exception: {out.get('message', '')}")
        if kind == "azure_put_failed":
            raise BrowserError(f"upload azure_put_failed (status={out.get('status')})")
        if kind == "process_upload_stream_http":
            raise BrowserError(
                f"upload process_upload_stream http {out.get('status')}: "
                f"{(out.get('body') or '')[:300]}"
            )
        if kind == "files_create_unexpected":
            raise BrowserError(f"upload files_create_unexpected: {out.get('detail')}")
        raise BrowserError(f"upload error: {kind}")
    if not out.get("file_id"):
        raise BrowserError(f"upload: no file_id in result: {out}")
    return UploadedFile(
        file_id=out["file_id"],
        name=p.name,
        size=len(blob),
        mime_type=out.get("mime_type") or mime,
        library_file_id=out.get("library_file_id") or "",
        width=dims[0] if dims else None,
        height=dims[1] if dims else None,
    )
