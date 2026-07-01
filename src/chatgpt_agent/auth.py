"""Login state detection + login/logout flow."""
from __future__ import annotations

import time
from dataclasses import dataclass

from .browser import Browser, BrowserError


@dataclass
class AuthInfo:
    email: str
    name: str
    picture: str | None
    raw: dict


def auth_session(b: Browser) -> dict | None:
    """Fetch /api/auth/session in the page; return parsed JSON.

    Returns None if the request itself fails (browser not on chatgpt.com,
    network error, etc).
    """
    try:
        return b.evaluate(
            """async () => {
                const r = await fetch('/api/auth/session', { credentials: 'include' });
                if (!r.ok) return null;
                return await r.json();
            }"""
        )
    except BrowserError:
        return None


def is_logged_in(b: Browser) -> AuthInfo | None:
    """If logged in, return AuthInfo; else None."""
    payload = auth_session(b)
    if not isinstance(payload, dict):
        return None
    user = payload.get("user")
    if not user or not isinstance(user, dict):
        return None
    email = user.get("email") or ""
    if not email:
        return None
    return AuthInfo(
        email=email,
        name=user.get("name") or "",
        picture=user.get("image"),
        raw=user,
    )


def login(b: Browser, *, poll_s: float = 2.0, timeout_s: float = 600.0) -> AuthInfo:
    """Ensure the browser is open and the user is logged in.

    Behavior:
        1. If session not running -> open it on chatgpt.com.
        2. If already logged in -> return immediately.
        3. Otherwise navigate to chatgpt.com and poll the auth endpoint
           until login is detected (the user logs in manually) or timeout.
    """
    if not b.is_running():
        b.open_session("https://chatgpt.com/")
        # Give the SPA a beat to mount before first probe.
        time.sleep(1.5)

    info = is_logged_in(b)
    if info:
        return info

    # Make sure we're actually on chatgpt.com (or its login page) so the cookie
    # context is correct for the auth fetch.
    try:
        url = b.current_url()
    except BrowserError:
        url = ""
    if "chatgpt.com" not in url:
        b.goto("https://chatgpt.com/")
        time.sleep(1.5)

    deadline = time.monotonic() + timeout_s
    while time.monotonic() < deadline:
        info = is_logged_in(b)
        if info:
            return info
        time.sleep(poll_s)
    raise TimeoutError(f"login not detected within {timeout_s}s")


def logout(b: Browser) -> bool:
    """Close the browser session. Returns True if it was running.

    Always call close_session so stale runtime files are removed even if the
    CDP endpoint already disappeared.
    """
    was = b.is_running()
    b.close_session()
    return was
