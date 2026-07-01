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

chatgpt-agent suite examples/suite.example.json --out ./out
```

Use `chatgpt-agent --help` and `chatgpt-agent <command> --help` for the current
command surface.

## Profiles And Concurrency

The default profile is `default`. You can use additional profiles when multiple
agents need isolated browser sessions:

```bash
chatgpt-agent --profile agent-a login
chatgpt-agent --profile agent-a suite ./suite.json --out ./out-a
```

Commands on the same profile are serialized with a file lock. Different
profiles can run in parallel because they use different Chrome user-data
directories and different locks. If multiple profiles log into the same
ChatGPT account, they still share that account's quota and anti-abuse limits.

Profile names must be 1-64 characters from `A-Z`, `a-z`, `0-9`, `.`, `_`, `-`,
and must start with a letter or digit.

## Image Suites

`chatgpt-agent suite` reads a JSON spec and generates all items in a single
ChatGPT conversation so later images can reuse the visual system established by
earlier images.

```json
{
  "series_name": "Launch visuals",
  "master_brief": "A coherent product launch image suite",
  "style": "clean studio photography, bright neutral lighting",
  "negative": "no unreadable text, no cluttered backgrounds",
  "items": [
    {"name": "Hero", "brief": "main product hero image", "aspect": "16:9"},
    {"name": "Square", "brief": "social media square version", "aspect": "1:1"}
  ]
}
```

The output directory contains generated image files and `manifest.json`.

## License

MIT
