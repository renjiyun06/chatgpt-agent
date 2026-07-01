# chatgpt-agent

`chatgpt-agent` is a small Python CLI for driving the ChatGPT web app from a
persistent local Chrome profile. It is intended for agent automation workflows
that need ChatGPT-web-only capabilities, such as native image generation and
server-side multi-turn ChatGPT conversations.

It does not store your ChatGPT password. Login happens in a real Chrome window,
and subsequent commands reuse the browser profile's cookies.

## Install

```bash
uv tool install --editable .
```

Chrome or Chromium must be installed and available on `PATH`.

## Login

```bash
chatgpt-agent login
```

Complete ChatGPT login in the opened browser window. The profile is persisted
under:

```text
~/.local/share/chatgpt-agent/profiles/default/chrome/
```

Runtime pid/CDP metadata is stored under:

```text
~/.cache/chatgpt-agent/runtime/default.session
```

## Commands

```bash
chatgpt-agent list --limit 5

chatgpt-agent new --initial "Summarize this topic in one paragraph"

ID=$(chatgpt-agent new --initial "Create an outline for X" | head -1)
chatgpt-agent send "$ID" "Expand the second section"

chatgpt-agent dump "$ID" > history.json
```

Use `chatgpt-agent --help` and `chatgpt-agent <command> --help` for the current
command surface.

## Profiles And Concurrency

The default profile is `default`. Use it as the base login profile:

```bash
chatgpt-agent login
```

When a new non-default profile is opened for the first time, `chatgpt-agent`
clones the default Chrome user-data-dir into that profile before launching it.
That means a profile such as `agent-a` usually inherits the default profile's
ChatGPT login state and does not need a separate manual login:

```bash
chatgpt-agent --profile agent-a new --initial "Generate a four-image product campaign set"
```

If the default Chrome profile is currently running, it is closed before cloning
so Chrome's profile files are copied from a stable state.

Commands on the same profile are serialized with a file lock. Different
profiles can run in parallel because they use different Chrome user-data
directories and different locks. If multiple profiles log into the same
ChatGPT account, they still share that account's quota and anti-abuse limits.

Profile names must be 1-64 characters from `A-Z`, `a-z`, `0-9`, `.`, `_`, `-`,
and must start with a letter or digit.

## Image Sets

ChatGPT can generate a coherent image set directly from one semantic prompt.
Use `new` for the first request and keep the returned conversation id if you
want to refine or extend the set with `send`.

```bash
ID=$(chatgpt-agent new --initial "Generate a coherent set of 4 square social images for a premium coffee brand: storefront hero, latte close-up, coffee bag still life, and morning takeaway lifestyle scene. Keep one visual system across all images." | head -1)
chatgpt-agent send "$ID" "Generate 2 more images in the same visual system: pastry pairing and iced coffee."
```

Generated images are printed on stderr as `[image] <path>` and saved under the
active profile's image cache.

## License

MIT
