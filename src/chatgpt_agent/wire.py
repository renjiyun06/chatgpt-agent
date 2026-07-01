"""Direct HTTP wire to ChatGPT's /backend-api/f/conversation, no UI.

The page bundle hosts a window-global `SentinelSDK` whose `token('next')`
call returns a JSON `{p, t, c, id, flow}` where `t` is a Cloudflare
Turnstile token freshly minted by the SDK's iframe-side bytecode VM.
We hold onto `t` (we don't compute turnstile ourselves — we can't,
the bytecode VM lives in closure-private state).

We then POST that whole bundle to `/sentinel/chat-requirements` to
get back a server-issued chat-requirements token plus a PoW challenge.
We solve the FNV-1a PoW locally (~1 ms) for the proof token, then
POST `/f/conversation` carrying the three sentinel headers:
  - chat-requirements-token: from /chat-requirements response
  - proof-token: locally-computed PoW for that chat-req
  - turnstile-token: `t` from the SDK output

The turnstile token is NOT bound to the chat-req; the server accepts
any valid SDK-minted turnstile alongside any valid chat-req for the
same session. Without it the server's anti-abuse layer scores us
upward on each request and starts returning 429 after a handful of
sends — verified empirically.

This module owns the JS that runs in page context. The Python side is a
thin `send` that hands the prompt to `b.evaluate(_SEND_JS, ...)` and
returns the conversation id + assistant message id. The caller (chat
module) re-fetches the mapping for full content/citations/images.

Why we still need the browser at all: SDK.token() reaches into the
sentinel iframe (a separate frame.html) to gather a fingerprint that
the server validates. Reproducing that purely in Python would require
running the iframe's bytecode VM, which is the only piece we can't
trivially recreate. Everything else (PoW, request body, headers) is
cheap to do ourselves — that's what this module does.
"""
from __future__ import annotations

import json
from dataclasses import dataclass
from typing import Any

from .browser import Browser, BrowserError

# Headers that don't change for a given session/profile. We let the JS
# read them off the page each call (cheap; cookies + localStorage),
# which guarantees they stay fresh after the user reloads or rotates.
_OAI_CLIENT_VERSION = "prod-5d86787f9f8d1f6b6e7e021b6aa4d6b14a14445c"
_OAI_CLIENT_BUILD = "6232230"

# The big one. Lives in the page so it can call SentinelSDK and do
# same-origin fetch with cookies attached. Returns a JSON dict.
#
# IMPORTANT: do NOT monkey-patch window.fetch in here. The SDK runs an
# anti-tamper regex `fn.toString().search("(((.+)+)+)+$")` on its
# internal references — a polluted fetch source string causes
# catastrophic backtracking and hangs token() forever.
_SEND_JS = r"""
async ({ prompt, conv_id, parent_msg_id, model, thinking_effort, timezone, tz_offset_min, attachments }) => {
  // Wrap the whole flow in try/catch so any uncaught exception (most
  // commonly: a fetch returning HTML instead of JSON when the server
  // injects a Cloudflare challenge / WAF block / login redirect)
  // surfaces as a structured error with the failing URL, instead of
  // a bare "Unexpected token '<' is not valid JSON" Python-side.
  try { return await (async () => {
  // Helper for any fetch that we expect to return JSON. Catches both
  // non-2xx and "200 OK with HTML body" cases — both of which arise
  // when CF / OpenAI's anti-abuse layer interposes — and reports the
  // URL + status + first chunk of body so debugging doesn't require a
  // browser session.
  async function fetchJson(url, init) {
    let r;
    try { r = await fetch(url, init); }
    catch (e) { throw new Error('network_failed at ' + url + ': ' + String(e)); }
    let body = '';
    try { body = await r.text(); } catch (_) {}
    const ct = r.headers.get('content-type') || '';
    if (!r.ok) {
      throw new Error('http ' + r.status + ' at ' + url + ': ' + body.slice(0, 300));
    }
    if (!ct.includes('json')) {
      throw new Error('expected_json got_' + ct.split(';')[0] + ' at ' + url + ': ' + body.slice(0, 300));
    }
    try { return JSON.parse(body); }
    catch (e) { throw new Error('json_parse_failed at ' + url + ': ' + body.slice(0, 300)); }
  }
  function getCookie(name) {
    for (const c of document.cookie.split('; ')) {
      const i = c.indexOf('=');
      if (c.slice(0, i) === name) return decodeURIComponent(c.slice(i+1));
    }
    return null;
  }
  // Mirror of the SDK's getConfig() — see sentinel/<v>/sdk.js, class _.
  // The exact set of fields is verified empirically; if the server
  // starts rejecting these, recheck the SDK's `getConfig`.
  function buildConfig() {
    function P(arr) { return arr[Math.floor(Math.random() * arr.length)]; }
    function R() {
      try {
        const n = P(Object.keys(Object.getPrototypeOf(navigator)));
        return n + "−" + navigator[n].toString();
      } catch (_) { return ""; }
    }
    return [
      screen.width + screen.height,
      "" + new Date(),
      performance?.memory?.jsHeapSizeLimit ?? null,
      Math.random(),
      navigator.userAgent,
      P(Array.from(document.scripts).map(s => s?.src).filter(s => s)),
      (Array.from(document.scripts || []).map(s => s?.src?.match("c/[^/]*/_")).filter(t => t?.length)[0]?.[0]) ??
        document.documentElement.getAttribute("data-build"),
      navigator.language, navigator.languages?.join(","), Math.random(), R(),
      P(Object.keys(document)), P(Object.keys(window)), performance.now(),
      "sid", [...new URLSearchParams(window.location.search).keys()].join(","),
      navigator?.hardwareConcurrency, performance.timeOrigin,
      Number("ai" in window), Number("createPRNG" in window),
      Number("cache" in window), Number("data" in window),
      Number("solana" in window), Number("dump" in window),
      Number("InstallTrigger" in window),
    ];
  }
  // FNV-1a hash + base64 fingerprint -> proof-of-work answer. See the
  // SDK source comments for the algorithm shape.
  function computePoW(seed, difficulty) {
    const t0 = performance.now();
    function fnv(str) {
      let e = 2166136261;
      for (let r = 0; r < str.length; r++) {
        e ^= str.charCodeAt(r);
        e = Math.imul(e, 16777619) >>> 0;
      }
      e ^= e >>> 16; e = Math.imul(e, 2246822507) >>> 0;
      e ^= e >>> 13; e = Math.imul(e, 3266489909) >>> 0;
      e ^= e >>> 16;
      return (e >>> 0).toString(16).padStart(8, "0");
    }
    function b64(arr) {
      return btoa(String.fromCharCode(...(new TextEncoder()).encode(JSON.stringify(arr))));
    }
    const cfg = buildConfig();
    for (let attempt = 0; attempt < 5e5; attempt++) {
      cfg[3] = attempt;
      cfg[9] = Math.round(performance.now() - t0);
      const i = b64(cfg);
      const s = fnv(seed + i);
      if (s.substring(0, difficulty.length) <= difficulty) return i + "~S";
    }
    return null;
  }

  // Identity: accessToken + OAI-* headers + session cookies (auto-attached).
  // The React app injects the sentinel SDK lazily — depending on route /
  // user state it may not be loaded when we land here. We force-load it
  // ourselves: the bootstrap shim at /backend-api/sentinel/sdk.js installs
  // a queue under window.SentinelSDK.{init,token} and then loads the real
  // SDK from /sentinel/<v>/sdk.js, which replaces those stubs with its
  // actual `Me` / `Ie` implementations. We wait until that swap happens.
  async function ensureSentinelSDK(deadline_ms = 15000) {
    function realInitLoaded() {
      return typeof window.SentinelSDK === 'object'
          && typeof window.SentinelSDK.init === 'function'
          && String(window.SentinelSDK.init).startsWith('async function');
    }
    if (realInitLoaded()) return true;
    if (typeof window.SentinelSDK !== 'object') {
      // Bootstrap shim hasn't even been injected. Add it ourselves.
      const s = document.createElement('script');
      s.src = '/backend-api/sentinel/sdk.js';
      s.async = false;
      document.head.appendChild(s);
    }
    const deadline = Date.now() + deadline_ms;
    while (Date.now() < deadline) {
      if (realInitLoaded()) return true;
      await new Promise(r => setTimeout(r, 200));
    }
    return false;
  }
  if (!await ensureSentinelSDK()) {
    return { error: 'sentinel_sdk_not_loaded',
             hint: 'tried to inject /backend-api/sentinel/sdk.js but the real SDK never replaced the shim within 15s' };
  }

  // Prime the SDK: init('next') makes the iframe POST to /sentinel/req
  // so its cachedChatReq + cachedProof get refreshed. token('next')
  // would do this lazily on its own, but priming makes the timing
  // deterministic and refreshes the `oai-sc` cookie as a side effect.
  try { await SentinelSDK.init('next'); } catch (_) { /* tolerate */ }
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

  // 1. Mint a fresh chat-requirements token + PoW challenge.
  //    fpBody.t is the SDK-minted Cloudflare Turnstile token; we keep
  //    it for the /f/conversation header below — without it the
  //    server scores us upward on every send and 429's after a few.
  const fpResult = await SentinelSDK.token('next');
  const fpBody = JSON.parse(fpResult);
  if (!fpBody.t) {
    // SDK error path returns {e, p} without the turnstile field.
    // Surface the SDK's own error message verbatim — usually says
    // why the iframe / bytecode VM failed.
    return { error: 'sdk_token_no_turnstile', sdk_error: fpBody.e || null };
  }
  const crBody = await fetchJson('/backend-api/sentinel/chat-requirements', {
    method: 'POST',
    headers: Object.assign({}, baseHeaders, {
      'x-openai-target-path': '/backend-api/sentinel/chat-requirements',
      'x-openai-target-route': '/backend-api/sentinel/chat-requirements',
    }),
    body: JSON.stringify(fpBody),
  });

  // 2. PoW.
  const proofAnswer = computePoW(crBody.proofofwork.seed, crBody.proofofwork.difficulty);
  if (!proofAnswer) return { error: 'pow_exhausted' };
  const proofToken = "gAAAAAB" + proofAnswer;

  // 3. POST /f/conversation with all three sentinel tokens.
  // Attachments: if `attachments` is a non-empty array, switch the
  // user-message content_type to 'multimodal_text' and include an
  // image_asset_pointer part per image attachment, followed by the
  // text part. Non-image attachments (PDFs etc.) only contribute a
  // metadata.attachments entry — the model reads them server-side
  // via the file_id, no asset_pointer needed.
  const userMsgId = crypto.randomUUID();
  const hasAttachments = Array.isArray(attachments) && attachments.length > 0;
  let content;
  if (hasAttachments) {
    const parts = [];
    for (const a of attachments) {
      if (a && a.mime_type && a.mime_type.startsWith('image/')) {
        const part = {
          content_type: 'image_asset_pointer',
          asset_pointer: 'sediment://' + a.file_id,
          size_bytes: a.size,
        };
        if (a.width)  part.width = a.width;
        if (a.height) part.height = a.height;
        parts.push(part);
      }
    }
    parts.push(prompt);
    content = { content_type: 'multimodal_text', parts };
  } else {
    content = { content_type: 'text', parts: [prompt] };
  }
  const messageMetadata = {};
  if (hasAttachments) {
    messageMetadata.attachments = attachments.map(a => {
      const m = {
        id: a.file_id,
        size: a.size,
        name: a.name,
        mime_type: a.mime_type,
        source: 'library',
      };
      if (a.library_file_id) m.library_file_id = a.library_file_id;
      if (a.width)  m.width = a.width;
      if (a.height) m.height = a.height;
      m.is_big_paste = false;
      return m;
    });
  }
  const body = {
    action: 'next',
    messages: [{
      id: userMsgId, author: { role: 'user' },
      create_time: Date.now() / 1000,
      content,
      metadata: messageMetadata,
    }],
    parent_message_id: parent_msg_id,
    model,
    // Forwarded for thinking models; instant models ignore the field.
    thinking_effort,
    client_prepare_state: 'failure',
    timezone_offset_min: tz_offset_min,
    timezone,
    conversation_mode: { kind: 'primary_assistant' },
    enable_message_followups: false,
    system_hints: [],
    supports_buffering: true,
    supported_encodings: ['v1'],
    client_contextual_info: { app_name: 'chatgpt.com' },
  };
  if (conv_id) body.conversation_id = conv_id;
  const sendHeaders = Object.assign({}, baseHeaders, {
    accept: 'text/event-stream',
    'openai-sentinel-chat-requirements-token': crBody.token,
    'openai-sentinel-proof-token': proofToken,
    'openai-sentinel-turnstile-token': fpBody.t,
    // Telemetry counters the React app always attaches; values are
    // not validated for content but absence makes our requests look
    // distinct from a normal browser session.
    'oai-echo-logs': '0,1',
    'oai-telemetry': '[1,null]',
    'x-oai-turn-trace-id': crypto.randomUUID(),
    'x-openai-target-path': '/backend-api/f/conversation',
    'x-openai-target-route': '/backend-api/f/conversation',
  });
  const sendReq = await fetch('/backend-api/f/conversation', {
    method: 'POST', headers: sendHeaders, body: JSON.stringify(body),
  });
  if (sendReq.status !== 200) {
    let body = ''; try { body = await sendReq.text(); } catch (_) {}
    return { error: 'conversation_send_failed', status: sendReq.status, body: body.slice(0, 500) };
  }

  // 4. Stream the SSE response. We don't need the streamed text — the
  //    caller will re-fetch the mapping for the canonical reply
  //    (with citations + image refs resolved). We only watch for:
  //      - conversation_id + assistant message id (to tell the caller
  //        where to look in the mapping)
  //      - terminal events: [DONE], stream_handoff, or
  //        message_stream_complete
  //
  //    We MUST treat stream_handoff as terminal: when the model's
  //    content stream is moved to a separate channel (WebSocket or a
  //    resume SSE endpoint), the original POST's SSE *may* close
  //    immediately after announcing the handoff WITHOUT emitting
  //    [DONE]. Waiting for [DONE] in that case hangs forever. We
  //    don't actually need to follow the handoff — `_wait_for_finish`
  //    will catch the assistant's final message via the mapping
  //    endpoint regardless of which transport carried the deltas.
  const reader = sendReq.body.getReader();
  const dec = new TextDecoder();
  let buf = '';
  let conversation_id = conv_id || null;
  let assistant_msg_id = null;
  let saw_end_turn = false;
  let saw_terminal = null;  // 'done' | 'handoff' | 'stream_complete'
  drain: while (true) {
    const { value, done } = await reader.read();
    if (done) break;
    buf += dec.decode(value, { stream: true });
    let idx;
    while ((idx = buf.indexOf('\n\n')) !== -1) {
      const raw = buf.slice(0, idx); buf = buf.slice(idx + 2);
      let data = null;
      for (const line of raw.split('\n')) {
        if (line.startsWith('data: ')) data = (data == null ? '' : data + '\n') + line.slice(6);
      }
      if (!data) continue;
      if (data === '[DONE]') { saw_terminal = 'done'; break drain; }
      try {
        const j = JSON.parse(data);
        if (j.conversation_id && !conversation_id) conversation_id = j.conversation_id;
        if (j.type === 'message_marker' && j.message_id) assistant_msg_id = j.message_id;
        if (j.type === 'stream_handoff') {
          // Content is moving to another channel. Stop draining; the
          // mapping-poll layer will pick up the final message.
          saw_terminal = 'handoff';
          break drain;
        }
        if (j.type === 'message_stream_complete') {
          // Sometimes precedes [DONE] by a noticeable interval; safe
          // to exit early — the assistant message is fully written.
          saw_end_turn = true;
        }
        const v = j.v;
        if (v && v.message && v.message.author && v.message.author.role === 'assistant') {
          assistant_msg_id = v.message.id;
        }
        if (j.o === 'patch' && Array.isArray(j.v)) {
          for (const op of j.v) {
            if (op.p === '/message/end_turn' && op.v === true) saw_end_turn = true;
            if (op.p === '/message/status' && op.v === 'finished_successfully') saw_end_turn = true;
          }
        }
      } catch (_) {}
    }
  }
  // Best-effort cancel — releases the underlying socket promptly
  // instead of letting it sit half-open until GC.
  try { await reader.cancel(); } catch (_) {}
  return {
    conversation_id, assistant_msg_id,
    end_turn: saw_end_turn, sse_terminal: saw_terminal,
  };
  })(); } catch (e) {
    return { error: 'js_exception', message: String(e && e.message || e), stack: String(e && e.stack || '').slice(0, 1500) };
  }
}
""".replace("__OAI_VERSION__", _OAI_CLIENT_VERSION).replace("__OAI_BUILD__", _OAI_CLIENT_BUILD)


@dataclass
class SendResult:
    """Outcome of a /f/conversation POST. The reply text is NOT here —
    the caller fetches the mapping endpoint to read the canonical
    assistant message (including citations + image refs)."""
    conversation_id: str
    assistant_msg_id: str | None
    end_turn: bool


def send(
    b: Browser,
    prompt: str,
    *,
    conv_id: str | None = None,
    parent_msg_id: str = "client-created-root",
    model: str = "gpt-5-5-thinking",
    thinking_effort: str = "extended",
    timezone: str = "Asia/Shanghai",
    tz_offset_min: int = -480,
    attachments: list[dict] | None = None,
    timeout_s: float = 300.0,
) -> SendResult:
    """Mint sentinel tokens and POST /f/conversation, streaming SSE.

    Returns once the SSE stream closes (with `[DONE]` or `end_turn`).
    The caller is responsible for fetching the conversation mapping
    afterward to read the assistant's full reply.

    `parent_msg_id`:
        - "client-created-root" for a brand-new conversation
        - the previous assistant message id for a follow-up turn
    `conv_id`:
        - None for a brand-new conversation (server allocates one)
        - the existing conversation id for a follow-up turn
    `attachments`:
        - None or [] for a plain-text turn (current behavior).
        - Each entry is a dict with keys
          {file_id, name, size, mime_type, library_file_id, width?, height?}
          — produced by `upload.upload_file`. Image attachments
          (mime_type starts with "image/") become an
          `image_asset_pointer` content part; non-image attachments
          contribute only to `metadata.attachments` (model reads them
          server-side via the file_id).
    """
    if not prompt or not prompt.strip():
        raise BrowserError("send: prompt cannot be empty")
    args = {
        "prompt": prompt,
        "conv_id": conv_id,
        "parent_msg_id": parent_msg_id,
        "model": model,
        "thinking_effort": thinking_effort,
        "timezone": timezone,
        "tz_offset_min": tz_offset_min,
        "attachments": attachments or [],
    }
    out: dict[str, Any] = b.evaluate(_SEND_JS, args, timeout_s=timeout_s) or {}
    if "error" in out:
        # Surface the server's reason if there is one — that's what
        # tells the user 'rate limited', 'unusual activity', etc.
        kind = out["error"]
        if kind == "js_exception":
            raise BrowserError(f"js_exception: {out.get('message', '')}")
        body = (out.get("body") or "").strip()
        if body:
            raise BrowserError(f"{kind} (status={out.get('status')}): {body[:300]}")
        if out.get("status"):
            raise BrowserError(f"{kind} (status={out['status']})")
        hint = out.get("hint")
        raise BrowserError(f"{kind}: {hint}" if hint else kind)
    if not out.get("conversation_id"):
        raise BrowserError(
            "no conversation_id in SSE response — request may have been silently rejected"
        )
    return SendResult(
        conversation_id=out["conversation_id"],
        assistant_msg_id=out.get("assistant_msg_id"),
        end_turn=bool(out.get("end_turn")),
    )
