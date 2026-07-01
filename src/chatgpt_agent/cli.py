"""chatgpt-agent CLI."""
from __future__ import annotations

import json
import os
import sys
from datetime import datetime, timezone

import click

from . import __version__, auth, chat, lock, paths, store
from .browser import Browser, BrowserError


def _profile() -> str:
    return os.environ.get("CHATGPT_AGENT_PROFILE", "default")


def _die(msg: str, *, code: int = 1) -> None:
    click.echo(msg, err=True)
    sys.exit(code)


def _require_login(b: Browser) -> None:
    """Quick local check that the profile-managed chrome is running.

    We do NOT verify login state here — that would cost an extra
    /api/auth/session round-trip. The first real operation will fail
    fast with a clear error if the user isn't actually logged in.
    """
    if not b.is_running():
        _die("not logged in (browser not running). run: chatgpt-agent login", code=2)


@click.group()
@click.version_option(__version__)
@click.option("--profile", "-p", default=None, help="Profile name; default reads $CHATGPT_AGENT_PROFILE then 'default'.")
@click.option("--no-wait", is_flag=True, help="Fail immediately if another instance holds the lock.")
@click.option("--wait-s", default=60.0, show_default=True, help="Seconds to wait for the per-profile lock.")
@click.pass_context
def main(ctx: click.Context, profile: str | None, no_wait: bool, wait_s: float) -> None:
    """Drive ChatGPT (web) as a session-oriented agent."""
    if profile is None:
        profile = _profile()
    ctx.ensure_object(dict)
    try:
        ctx.obj["profile"] = paths.validate_profile(profile)
    except ValueError as e:
        raise click.BadParameter(str(e), param_hint="--profile") from e
    ctx.obj["lock_wait"] = 0.0 if no_wait else wait_s


def _with_lock(ctx: click.Context, command: str):
    return lock.acquire(ctx.obj["profile"], command, wait_s=ctx.obj["lock_wait"])


# ----------------------------------------------------------------- login/out

def _die_browser_error(e: BrowserError) -> None:
    """Translate a BrowserError into a clean CLI error.

    Exit codes:
      4 — conversation not found (wrong id, deleted, etc.)
      5 — generic browser/server error (rate limit, 5xx, eval failure, etc.)
    Both are non-zero so scripts can detect failure; the message itself
    tells the user what went wrong without exposing a Python traceback.
    """
    msg = str(e)
    code = 4 if "conversation not found" in msg or "http 404" in msg else 5
    _die(f"chatgpt-agent: {msg}", code=code)


@main.command("login")
@click.pass_context
def cmd_login(ctx: click.Context) -> None:
    """Open the browser (if needed) and ensure logged in."""
    profile = ctx.obj["profile"]
    try:
        with _with_lock(ctx, "login"):
            b = Browser(profile)
            info = auth.login(b)
        click.echo(f"logged in as {info.email}")
    except lock.LockError as e:
        _die(f"chatgpt-agent: {e}", code=11)
    except TimeoutError as e:
        _die(f"chatgpt-agent: {e}", code=3)
    except BrowserError as e:
        _die_browser_error(e)


@main.command("logout")
@click.pass_context
def cmd_logout(ctx: click.Context) -> None:
    """Close the browser instance (cookies persist)."""
    profile = ctx.obj["profile"]
    try:
        with _with_lock(ctx, "logout"):
            b = Browser(profile)
            was = auth.logout(b)
        click.echo("logged out (browser closed)" if was else "already logged out (browser was not running)")
    except lock.LockError as e:
        _die(f"chatgpt-agent: {e}", code=11)
    except BrowserError as e:
        _die_browser_error(e)


# --------------------------------------------------------------- list/dump

@main.command("list")
@click.option("--limit", default=50, show_default=True)
@click.option("--json", "as_json", is_flag=True, help="Emit JSON instead of a table.")
@click.pass_context
def cmd_list(ctx: click.Context, limit: int, as_json: bool) -> None:
    """List conversations on the server."""
    profile = ctx.obj["profile"]
    try:
        with _with_lock(ctx, "list"):
            b = Browser(profile)
            _require_login(b)
            items = chat.list_conversations(b, limit=limit)
    except lock.LockError as e:
        _die(f"chatgpt-agent: {e}", code=11)
    except BrowserError as e:
        _die_browser_error(e)
    if as_json:
        click.echo(json.dumps(items, ensure_ascii=False, indent=2))
        return
    if not items:
        click.echo("(no conversations)")
        return
    for it in items:
        cid = it.get("id", "")
        title = it.get("title") or "(untitled)"
        ts = it.get("update_time") or it.get("create_time") or ""
        if isinstance(ts, (int, float)):
            ts = datetime.fromtimestamp(ts, tz=timezone.utc).isoformat()
        click.echo(f"{cid}\t{str(ts)[:19]}\t{title}")


@main.command("dump")
@click.argument("session_id")
@click.option("--no-images", is_flag=True, help="Skip image downloads.")
@click.pass_context
def cmd_dump(ctx: click.Context, session_id: str, no_images: bool) -> None:
    """Print full conversation history (turns + image paths) as JSON."""
    profile = ctx.obj["profile"]
    try:
        with _with_lock(ctx, "dump"):
            b = Browser(profile)
            _require_login(b)
            data = chat.dump_conversation(b, profile, session_id, download_images=not no_images)
    except lock.LockError as e:
        _die(f"chatgpt-agent: {e}", code=11)
    except BrowserError as e:
        _die_browser_error(e)
    click.echo(json.dumps(data, ensure_ascii=False, indent=2))


# --------------------------------------------------------------- new/send

@main.command("new")
@click.option("--initial", default="你好", show_default=True, help="The first message to seed the conversation.")
@click.option(
    "--model", "model_slug", default="gpt-5-5-thinking", show_default=True,
    help="Model slug, e.g. 'gpt-5-5-thinking' (Thinking, supports image gen), 'gpt-5-3' (Instant).",
)
@click.option(
    "--effort", "thinking_effort", default="extended", show_default=True,
    type=click.Choice(["standard", "extended"], case_sensitive=False),
    help="Reasoning effort for thinking models. Ignored by instant models.",
)
@click.option(
    "--attach", "attachments", multiple=True,
    type=click.Path(exists=True, dir_okay=False, readable=True),
    help="Path to a file (image, PDF, etc.) to attach. Repeatable for multiple files.",
)
@click.pass_context
def cmd_new(ctx: click.Context, initial: str, model_slug: str, thinking_effort: str, attachments: tuple[str, ...]) -> None:
    """Start a new conversation. Prints session id to stdout, reply text
    + any image paths to stderr."""
    profile = ctx.obj["profile"]
    try:
        with _with_lock(ctx, "new"):
            b = Browser(profile)
            _require_login(b)
            conv_id, reply = chat.new_session(
                b, profile, initial, model=model_slug, thinking_effort=thinking_effort,
                attachments=list(attachments) or None,
            )
            store.add_session(profile, store.Session(id=conv_id, title=initial[:40], model=reply.model_slug))
    except lock.LockError as e:
        _die(f"chatgpt-agent: {e}", code=11)
    except BrowserError as e:
        _die_browser_error(e)
    click.echo(conv_id)
    click.echo("---", err=True)
    if reply.text:
        click.echo(reply.text, err=True)
    for img in reply.images:
        click.echo(f"[image] {img.path}", err=True)


@main.command("send")
@click.argument("session_id")
@click.argument("message")
@click.option(
    "--model", "model_slug", default="gpt-5-5-thinking", show_default=True,
    help="Model slug, e.g. 'gpt-5-5-thinking' (Thinking, supports image gen), 'gpt-5-3' (Instant).",
)
@click.option(
    "--effort", "thinking_effort", default="extended", show_default=True,
    type=click.Choice(["standard", "extended"], case_sensitive=False),
    help="Reasoning effort for thinking models. Ignored by instant models.",
)
@click.option(
    "--attach", "attachments", multiple=True,
    type=click.Path(exists=True, dir_okay=False, readable=True),
    help="Path to a file (image, PDF, etc.) to attach. Repeatable for multiple files.",
)
@click.pass_context
def cmd_send(ctx: click.Context, session_id: str, message: str, model_slug: str, thinking_effort: str, attachments: tuple[str, ...]) -> None:
    """Send MESSAGE into SESSION_ID; prints the reply text to stdout."""
    profile = ctx.obj["profile"]
    try:
        with _with_lock(ctx, "send"):
            b = Browser(profile)
            _require_login(b)
            reply = chat.send_message(
                b, profile, session_id, message,
                model=model_slug, thinking_effort=thinking_effort,
                attachments=list(attachments) or None,
            )
    except lock.LockError as e:
        _die(f"chatgpt-agent: {e}", code=11)
    except BrowserError as e:
        _die_browser_error(e)
    if reply.text:
        click.echo(reply.text)
    for img in reply.images:
        click.echo(f"[image] {img.path}", err=True)


# ------------------------------------------------------------- tab control

@main.command("close")
@click.argument("session_id", required=False)
@click.option("--all", "close_all", is_flag=True, help="Close every chatgpt-agent tab.")
@click.pass_context
def cmd_close(ctx: click.Context, session_id: str | None, close_all: bool) -> None:
    """Close one or all tabs (does not delete the conversation server-side)."""
    profile = ctx.obj["profile"]
    if not session_id and not close_all:
        _die("usage: close <session_id> | close --all", code=64)
    try:
        with _with_lock(ctx, "close"):
            b = Browser(profile)
            if not b.is_running():
                click.echo("browser not running; nothing to close")
                return
            if close_all:
                # Walk in reverse order so indices remain stable as we close.
                for t in sorted(b.tabs(), key=lambda t: t["index"], reverse=True):
                    if "chatgpt.com" in t["url"]:
                        b.tab_close(t["index"])
                click.echo("closed all chatgpt tabs")
                return
            idx = b.find_tab_for_conv(session_id)
            if idx is None:
                click.echo(f"no tab open for {session_id}")
                return
            b.tab_close(idx)
            click.echo(f"closed tab for {session_id}")
    except lock.LockError as e:
        _die(f"chatgpt-agent: {e}", code=11)
    except BrowserError as e:
        _die_browser_error(e)


if __name__ == "__main__":
    main()
