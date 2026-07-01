---
name: chatgpt-agent
version: 0.1.0
description: "Drive a paid ChatGPT (web) account from the CLI, intended for agent automation. Use when an agent needs ChatGPT-web-only capabilities: (1) GPT-5 thinking with extended reasoning effort, (2) native image generation via gpt-5-5-thinking, (3) multi-turn conversations whose context lives on ChatGPT's server (not in the agent's prompt), (4) reading back conversation history with images. Typical triggers: '问一下 ChatGPT (thinking)', '让 ChatGPT 帮我画...', '在 ChatGPT 里继续那个会话', 'dump that ChatGPT conversation', '用 GPT-5 thinking 推理一下'. Do NOT use for: plain text generation the agent could answer itself; anything the Anthropic API can do directly (use the API instead); batch / high-frequency loops (browser session has hard rate limits); or as a substitute for memory (use conversation ids deliberately, not to replace agent state)."
metadata:
  requires:
    bins: ["chatgpt-agent"]
    services: ["a paid ChatGPT account (Plus / Pro / Team) already logged-in via `chatgpt-agent login`"]
  cliHelp: "chatgpt-agent --help"
---

# chatgpt-agent

A CLI front-end to ChatGPT's web app, built so an autonomous agent can talk to the user's paid ChatGPT account exactly the way a human at the keyboard would — but as commands, not clicks.

## When to use

✅ Reach for this skill when the task needs something only the ChatGPT web app provides:
- **GPT-5 thinking** with `--effort extended` — reasoning + tool use that the API doesn't expose for paid web users.
- **Native image generation** — `gpt-5-5-thinking` produces images inline; `chatgpt-agent` materializes them to local files automatically.
- **Vision / image references** — pass local files via `--attach`. The model sees images directly and can also be asked to redraw / restyle them (image-edit via `gpt-5-5-thinking`). Non-image files (PDFs, etc.) attach the same way.
- **Persistent multi-turn context** — ChatGPT keeps conversation state server-side; you can resume a conversation hours/days later by id, without re-feeding history.
- **Reading historical conversations** — pull mappings + images for a conversation the user already has.

## When NOT to use

❌ Avoid this skill for:
- **Plain text generation** an agent / Claude can do itself. Don't proxy "summarize this paragraph" through ChatGPT.
- **Anything the Anthropic API solves directly.** If the task is doable with the API, use the API — it's faster, cheaper, and not rate-limited the same way.
- **Batch / high-frequency loops.** The browser session has both hard quota (paid tier) and soft anti-abuse rate limiting (HTTP 429 — see "Rate limiting" below).
- **As a substitute for agent memory.** Use conversation ids deliberately — don't open one and abandon it just to "save state".

## Quick start

```bash
# 0. Liveness probe + auth check before any flow
chatgpt-agent list --limit 1

# If exit 2 ("not logged in"), open the browser and let the user finish login:
chatgpt-agent login

# 1. Single-shot question
chatgpt-agent new --initial "用一段话总结量子纠缠"

# 2. Multi-turn — capture conv_id from `new` stdout, feed it to `send`
ID=$(chatgpt-agent new --initial "我要研究 X 主题, 帮我列大纲" | head -1)
chatgpt-agent send "$ID" "把第二点展开"
chatgpt-agent send "$ID" "再加个示例"

# 3. Image generation (default model = gpt-5-5-thinking, supports image gen)
chatgpt-agent new --initial "画一张赛博朋克猫坐在霓虹招牌下"
# stderr lists each image's local path: [image] /home/.../<file_id>.png

# 4. Attach a local file (image, PDF, …). Repeatable for multiple files.
chatgpt-agent new --initial "用一句话描述这张图" --attach ./photo.jpg
chatgpt-agent send "$ID" "再看这张,两张图比一比" --attach ./other.jpg
# Image-edit: attach the source, ask for a redraw on the thinking model.
chatgpt-agent new --initial "把这张图改成赛博朋克风格,生成一张新图" --attach ./sketch.png

# 5. Read full history of an existing conversation
chatgpt-agent dump "$ID" > history.json

# 6. Generate a style-consistent image suite from a JSON spec
chatgpt-agent suite ./suite.json --out ./out

# Inspect the exact prompts first without spending ChatGPT quota
chatgpt-agent suite ./suite.json --dry-run
```

## Discovering commands and flags

**Don't rely on this doc for the command surface — it goes stale.** Read `--help`:

```bash
chatgpt-agent --help            # top-level commands
chatgpt-agent <cmd> --help      # per-command flags + defaults
```

This SKILL.md only covers what `--help` can't or won't tell you: I/O contract (what's on stdout vs stderr), exit-code semantics, rate-limiting behavior, cache locations, profiles, and which defaults are sane for agent use.

## Default model + effort

Defaults are tuned for an agent's typical "I want a strong answer" use case:
- `--model gpt-5-5-thinking`
- `--effort extended`

Switch to `--model gpt-5-3` (Instant) when the task is genuinely simple (one-line classification, format conversion) and the latency / quota cost of thinking is wasted. Switch `--effort standard` for the same reason if you want thinking but cheaper. Image generation only works on `gpt-5-5-thinking`.

## I/O contract

| Command | stdout | stderr |
|---|---|---|
| `new` | conversation id (one line) | `---` separator, then reply text, then `[image] <path>` lines |
| `send` | reply text | `[image] <path>` lines (one per generated image) |
| `dump` | full conversation as JSON: `{id, turns: [{role, text, content_type, message_id, model_slug, image_file_ids}], image_paths: {...}}` | (nothing on success) |
| `list` | tab-separated `<id>\t<update_time>\t<title>` (newest first); `--json` returns the server's array shape | (nothing on success) |
| `suite` | manifest path | progress summary |
| `login` / `logout` | status message | (nothing) |

`--attach <path>` (on `new` and `send`) uploads the file before the message
is sent. Repeat the flag for multiple files. Failures during upload abort
the command before any message is delivered (exit 5 with the server's
reason on stderr).

`suite` expects a JSON spec:

```json
{
  "series_name": "Launch visuals",
  "master_brief": "A coherent product launch image suite",
  "style": "clean studio photography, bright neutral lighting, restrained premium palette",
  "negative": "no unreadable text, no cluttered backgrounds",
  "attachments": ["./reference.png"],
  "items": [
    {"name": "Hero", "brief": "main product hero image", "aspect": "16:9"},
    {"name": "Square", "brief": "social media square version", "aspect": "1:1"}
  ]
}
```

The first item starts a new ChatGPT conversation and attaches global
`attachments`; later items continue in that same conversation so ChatGPT
can keep style continuity. Generated images are copied into `--out`, and
`manifest.json` records the conversation id, prompts, local image paths,
and per-item status.

## Exit codes

| Code | Meaning | What an agent should do |
|---|---|---|
| 0 | Success | Continue |
| 2 | Not logged in (browser not running) | Run `chatgpt-agent login`; if scripted, surface to the user |
| 3 | Login timed out | The user didn't finish OAuth in 10 min — surface to the user |
| 4 | Conversation not found | Don't retry, the id is wrong; check `list` |
| 5 | Generic browser/server error (rate limit, 5xx, eval failure) | Possibly retry once after a short backoff; the stderr message tells you what happened |
| 11 | Lock conflict (another instance is running) | Either wait, retry, or invoke with `--no-wait` and surface |
| 64 | CLI usage error | Fix the invocation |

## Rate limiting (read this before looping)

ChatGPT's anti-abuse layer scores client behavior server-side and starts returning **HTTP 429 "Too many requests"** when it decides a session is acting non-human. This trips for human users too — even a person clicking Send rapidly can hit it. Symptom:

```
chatgpt-agent: http 429: {"detail":"Too many requests"}
```

**As an agent, mitigate by:**
- **Don't loop.** Multi-turn is fine ("ask, then ask again with the answer"). But "ask 50 times in a `for`" gets rate-limited every time, by design.
- **Insert pauses.** Real users pause to read. A `sleep(5..15)` between sends materially reduces 429 risk.
- **Don't outer-loop poll.** The CLI already polls the mapping endpoint internally for long thinking replies; don't poll on top of it.
- **On 429: back off 5–15 minutes.** Don't immediately retry; the score takes time to decay.

## Profiles (multi-account)

```bash
CHATGPT_AGENT_PROFILE=alice chatgpt-agent login
CHATGPT_AGENT_PROFILE=alice chatgpt-agent list
# or via flag
chatgpt-agent --profile alice list
```

Each profile has its own Chrome user-data-dir + cookies + conversation pool. Conversation ids are account-scoped — a `conv_id` from profile A won't resolve under profile B. Default profile is `default`.

Use `default` as the base login profile. When a new non-default profile is opened for the first time, `chatgpt-agent` clones `default`'s Chrome user-data-dir into that profile before launching it, so the new profile usually inherits the existing ChatGPT login state. If `default` is currently running, it is closed before cloning to avoid copying a live Chrome profile.

Profile names must be 1-64 chars from `A-Z`, `a-z`, `0-9`, `.`, `_`, `-`, and must start with a letter or digit.

## Cache locations

- Browser user-data-dir (cookies, login state): `~/.local/share/chatgpt-agent/profiles/<profile>/chrome/`
- Generated images: `~/.local/share/chatgpt-agent/profiles/<profile>/images/<conv_id>/<file_id>.<ext>`
- Session index: `~/.config/chatgpt-agent/profiles/<profile>/sessions.json`
- Per-profile lock files: `~/.config/chatgpt-agent/locks/<profile>.lock`
- Running browser pid/CDP port: `~/.cache/chatgpt-agent/runtime/<profile>.session`

## Common pitfalls

- **"not logged in" (exit 2)** — run `chatgpt-agent login`; you'll only need to re-OAuth if the saved session actually expired.
- **`send` immediately after `new`** is fine — `new` already waits for the assistant's first reply before returning. A small pause between sends is still good for rate limiting.
- **Image expectations**: only `gpt-5-5-thinking` generates images. `gpt-5-3` (Instant) will reply with text describing what it would have drawn. Both models *read* attached images via `--attach`.
- **`--attach` with a missing path** errors out via click's path-validation (exit 2, "Invalid value"). The browser is never opened — safe to invoke speculatively.
- **Conversation id reuse** across profiles doesn't work; ids are account-scoped.
- **Concurrent invocations on the same profile** are serialized — running two `chatgpt-agent send` in parallel won't both go through; the second waits (or fails fast with `--no-wait`). Use separate profiles if you genuinely need parallelism.
- **Different profiles can run in parallel**, because they use different Chrome user-data-dirs and different locks. If those profiles log into the same ChatGPT account, they still share that account's quota and anti-abuse limits.

## Quota awareness

The user's ChatGPT subscription has a real monthly quota. Don't:
- Spam thinking-model requests for tasks that don't need them.
- Generate many images speculatively.
- Use this skill when the Anthropic API would do.

When in doubt, ask the user before kicking off a long ChatGPT-driven workflow.
