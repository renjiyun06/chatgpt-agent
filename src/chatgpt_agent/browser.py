"""Profile-aware wrapper that drives a persistent Chrome via CDP.

`chatgpt-agent` launches a headed Chrome with a persistent user-data-dir
and a local remote-debugging port. Inside a single command, all interaction
goes over a single `connect_over_cdp` connection.

Profile naming:
    profile = "default"   -> session name = "chatgpt-agent"
    profile = "alice"     -> session name = "chatgpt-agent-alice"

Each profile is a separate persistent Chrome (own user-data-dir, own
CDP port while running). The launcher writes the current pid + port into
`~/.cache/chatgpt-agent/runtime/<profile>.session`; cookies and login
state live separately under
`~/.local/share/chatgpt-agent/profiles/<profile>/chrome`.
New non-default profiles are cloned from the default profile's Chrome
user-data-dir when possible, so a single default login can seed later
per-agent profiles.

Driver model: chatgpt-agent never simulates clicks, never types
characters, never walks React fibers, never touches the composer
DOM. All operations go through page-internal `fetch` from CDP
`evaluate` calls. The chat-send path lives in `wire.py` — it mints
sentinel tokens via `window.SentinelSDK`, then POSTs
`/backend-api/f/conversation` directly with cookies + identity
headers and streams the SSE response.
"""
from __future__ import annotations

import atexit
import json
import os
import shutil
import signal
import socket
import subprocess
import urllib.request
import urllib.error
from glob import glob
from typing import Any
import time

from . import paths

# Silence the noisy `url.parse()` DeprecationWarning emitted by playwright's
# bundled node helper. Set before importing playwright (lazy, in _connect).
os.environ.setdefault("NODE_NO_WARNINGS", "1")

LEGACY_DAEMON_ROOT = os.path.expanduser("~/.cache/ms-playwright/daemon")
PROFILE_COPY_IGNORE = shutil.ignore_patterns(
    "SingletonLock",
    "SingletonSocket",
    "SingletonCookie",
    "DevToolsActivePort",
)


class BrowserError(RuntimeError):
    pass


class NotRunningError(BrowserError):
    """Raised when an op needs the browser open but it isn't."""


def session_name(profile: str) -> str:
    """Map profile -> human-readable browser session name."""
    return "chatgpt-agent" if profile == "default" else f"chatgpt-agent-{profile}"


def _session_file(profile: str) -> str:
    return str(paths.runtime_session_file(profile))


def _read_session_data(profile: str) -> dict | None:
    sf = _session_file(profile)
    if not os.path.isfile(sf):
        return None
    try:
        with open(sf, encoding="utf-8") as f:
            data = json.load(f)
    except (OSError, ValueError):
        return None
    data["_session_file"] = sf
    return data


def _read_cdp_port(profile: str) -> int | None:
    """Return the chrome CDP port for this session, or None if the
    profile-managed chrome is not running.
    """
    d = _read_session_data(profile)
    if not d:
        return None
    port = (
        d.get("resolvedConfig", {})
        .get("browser", {})
        .get("launchOptions", {})
        .get("cdpPort")
    )
    return int(port) if isinstance(port, int) else None


def _read_direct_pid(profile: str) -> int | None:
    d = _read_session_data(profile)
    if not d:
        return None
    pid = d.get("pid")
    return int(pid) if isinstance(pid, int) else None


def _legacy_user_data_dirs(session: str) -> list[str]:
    """Return old playwright-cli profile dirs, newest first."""
    candidates: list[tuple[float, str]] = []
    for sf in glob(os.path.join(LEGACY_DAEMON_ROOT, "*", f"{session}.session")):
        try:
            with open(sf, encoding="utf-8") as f:
                data = json.load(f)
        except (OSError, ValueError):
            continue
        user_data_dir = (
            data.get("browser", {}).get("userDataDir")
            or data.get("resolvedConfig", {}).get("browser", {}).get("launchOptions", {}).get("userDataDir")
        )
        if isinstance(user_data_dir, str) and os.path.isdir(user_data_dir):
            candidates.append((os.path.getmtime(user_data_dir), user_data_dir))
    for path in glob(os.path.join(LEGACY_DAEMON_ROOT, "*", f"ud-{session}-chrome")):
        if os.path.isdir(path):
            candidates.append((os.path.getmtime(path), path))
    return [path for _, path in sorted(candidates, reverse=True)]


def _process_exists(pid: int) -> bool:
    try:
        os.kill(pid, 0)
    except OSError:
        return False
    return True


def _terminate_pid(pid: int, timeout_s: float = 5.0) -> None:
    try:
        os.kill(pid, signal.SIGTERM)
    except OSError:
        return
    deadline = time.monotonic() + timeout_s
    while time.monotonic() < deadline:
        if not _process_exists(pid):
            return
        time.sleep(0.1)
    try:
        os.kill(pid, signal.SIGKILL)
    except OSError:
        pass


def _profile_has_live_chrome_lock(user_data_dir: str) -> bool:
    lock = os.path.join(user_data_dir, "SingletonLock")
    if not os.path.lexists(lock):
        return False
    try:
        target = os.readlink(lock)
    except OSError:
        return True
    try:
        pid = int(target.rsplit("-", 1)[1])
    except (IndexError, ValueError):
        return True
    return _process_exists(pid)


def _copy_profile_tree(source: str, target: str) -> bool:
    if _profile_has_live_chrome_lock(source):
        return False
    os.makedirs(os.path.dirname(target), exist_ok=True)
    try:
        shutil.copytree(source, target, ignore=PROFILE_COPY_IGNORE)
    except FileExistsError:
        pass
    except OSError:
        return False
    return True


def _close_runtime_profile(profile: str) -> None:
    """Close a running profile and remove its runtime file if present."""
    pid = _read_direct_pid(profile)
    if pid:
        _terminate_pid(pid)
    sf = _session_file(profile)
    try:
        os.remove(sf)
    except OSError:
        pass


def _ensure_user_data_dir(profile: str, session: str) -> str:
    """Return the stable Chrome profile dir, cloning default when safe."""
    target = paths.chrome_user_data_dir(profile)
    if target.exists():
        return str(target)

    if profile != "default":
        default_dir = paths.chrome_user_data_dir("default")
        if default_dir.exists():
            # A Chrome profile must not be copied while Chrome has it open.
            # If the default profile is currently running under our runtime,
            # close it first, then clone the now-stable user-data-dir.
            if _profile_has_live_chrome_lock(str(default_dir)):
                _close_runtime_profile("default")
            if _copy_profile_tree(str(default_dir), str(target)):
                return str(target)

    for legacy in _legacy_user_data_dirs(session):
        if _copy_profile_tree(legacy, str(target)):
            return str(target)

    target.mkdir(parents=True, exist_ok=True)
    return str(target)


def _free_port() -> int:
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as s:
        s.bind(("127.0.0.1", 0))
        return int(s.getsockname()[1])


def _ping_cdp(port: int, timeout_s: float = 1.0) -> bool:
    """Return True if the chrome CDP endpoint at `port` responds."""
    url = f"http://127.0.0.1:{port}/json/version"
    try:
        with urllib.request.urlopen(url, timeout=timeout_s) as resp:
            return resp.status == 200
    except (urllib.error.URLError, OSError):
        return False


class Browser:
    """A profile-bound view of the profile-managed chrome.

    Lazy: no CDP connection until the first method that needs the page.
    Cleanup: disconnects + stops the playwright runtime on process exit.
    Cheap to construct.
    """

    def __init__(self, profile: str = "default") -> None:
        self.profile = paths.validate_profile(profile)
        self.session = session_name(self.profile)
        # Lazy-initialized:
        self._pw = None  # SyncPlaywright runtime
        self._browser = None  # connected Browser (CDP)
        self._context = None  # the persistent BrowserContext
        self._page = None  # currently-selected Page
        self._cleanup_registered = False

    # ------------------------------------------------------- chrome glue

    def is_running(self) -> bool:
        """Chrome is open for this profile AND the CDP port responds."""
        port = _read_cdp_port(self.profile)
        if port is None:
            return False
        return _ping_cdp(port)

    def open_session(self, url: str = "https://chatgpt.com/") -> None:
        """Spawn a persistent headed Chrome with a CDP port for this profile."""
        if self.is_running():
            return
        chrome = (
            shutil.which("google-chrome")
            or shutil.which("chromium")
            or shutil.which("chromium-browser")
        )
        if not chrome:
            raise BrowserError("cannot find google-chrome/chromium on PATH")

        self._clear_stale_runtime()
        user_data_dir = _ensure_user_data_dir(self.profile, self.session)
        port = _free_port()
        cmd = [
            chrome,
            f"--user-data-dir={user_data_dir}",
            f"--remote-debugging-port={port}",
            "--remote-debugging-address=127.0.0.1",
            "--no-first-run",
            "--no-default-browser-check",
            "--disable-blink-features=AutomationControlled",
            url,
        ]
        proc = subprocess.Popen(
            cmd,
            stdin=subprocess.DEVNULL,
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
            start_new_session=True,
        )
        deadline = time.monotonic() + 20
        while time.monotonic() < deadline:
            if proc.poll() is not None:
                raise BrowserError(f"chrome exited during startup (code={proc.returncode})")
            if _ping_cdp(port):
                data = {
                    "profile": self.profile,
                    "name": self.session,
                    "pid": proc.pid,
                    "browser": {"userDataDir": user_data_dir},
                    "resolvedConfig": {
                        "browser": {
                            "launchOptions": {
                                "cdpPort": port,
                                "userDataDir": user_data_dir,
                            }
                        }
                    },
                }
                with open(_session_file(self.profile), "w", encoding="utf-8") as f:
                    json.dump(data, f, indent=2)
                return
            time.sleep(0.25)
        proc.terminate()
        raise BrowserError(f"chrome did not expose CDP on port {port}")

    def close_session(self) -> None:
        """Close this session's chrome (cookies persist in user-data-dir).

        Safe to call when not running.
        """
        self._disconnect()
        _close_runtime_profile(self.profile)

    def _clear_stale_runtime(self) -> None:
        """Remove a stale runtime file before launching a fresh Chrome."""
        data = _read_session_data(self.profile)
        if not data:
            return
        port = _read_cdp_port(self.profile)
        if port is not None and _ping_cdp(port):
            return
        pid = _read_direct_pid(self.profile)
        if pid and _process_exists(pid):
            _terminate_pid(pid)
        sf = _session_file(self.profile)
        try:
            os.remove(sf)
        except OSError:
            pass

    # ---------------------------------------------------- CDP connection

    def _connect(self) -> None:
        """Lazy CDP connect. Selects (or creates) a chatgpt.com page.

        Idempotent — calling when already connected is a no-op.
        """
        if self._page is not None:
            return
        port = _read_cdp_port(self.profile)
        if port is None:
            raise NotRunningError(
                f"chrome session {self.session!r} is not running "
                "(no runtime session file). run: chatgpt-agent login"
            )
        if not _ping_cdp(port):
            raise NotRunningError(
                f"chrome session {self.session!r}: CDP port {port} not responding. "
                "run: chatgpt-agent logout && chatgpt-agent login"
            )
        # Import inside to keep cold-start tiny when never used (e.g. --help).
        from playwright.sync_api import sync_playwright  # noqa: PLC0415
        self._pw = sync_playwright().start()
        try:
            self._browser = self._pw.chromium.connect_over_cdp(
                f"http://127.0.0.1:{port}"
            )
        except Exception as e:
            self._pw.stop()
            self._pw = None
            raise BrowserError(f"connect_over_cdp failed: {e}") from e
        # The persistent Chrome profile context is the first (only) one.
        ctxs = self._browser.contexts
        if not ctxs:
            raise BrowserError(
                "connected to chrome but no BrowserContext exists"
            )
        self._context = ctxs[0]
        # Pick a chatgpt.com page; create one if none exists.
        for pg in self._context.pages:
            if "chatgpt.com" in pg.url:
                self._page = pg
                break
        if self._page is None:
            self._page = self._context.new_page()
            self._page.goto("https://chatgpt.com/", wait_until="domcontentloaded")
        if not self._cleanup_registered:
            atexit.register(self._disconnect)
            self._cleanup_registered = True

    def _disconnect(self) -> None:
        """Close the CDP connection (does NOT close chrome). Safe to call
        multiple times.
        """
        try:
            if self._browser is not None:
                self._browser.close()
        except Exception:
            pass
        try:
            if self._pw is not None:
                self._pw.stop()
        except Exception:
            pass
        self._browser = None
        self._context = None
        self._page = None
        self._pw = None

    # ------------------------------------------------------- page ops

    # Default per-evaluate timeout used in the absence of an explicit
    # `timeout_s` arg. Matches playwright's own default; we restore to
    # this value after temporarily-bumped calls (e.g. wire.send for
    # long replies) to avoid leaking long timeouts to later evaluate
    # calls on the same page.
    _DEFAULT_TIMEOUT_MS = 30_000

    def evaluate(self, js: str, arg: Any = None, *, timeout_s: float | None = None) -> Any:
        """Run JS in the current page; return the parsed result.

        `js` is a JS expression — typically `() => ...` or
        `async () => ...`. `arg` is passed as the function's argument
        when present (must be JSON-serializable).

        `timeout_s` overrides the per-page default for THIS call only;
        the previous value is restored on exit. Use it for evaluates
        that legitimately take a long time (long model replies); leave
        it None for fast operations so a hung page surfaces quickly.
        """
        self._connect()
        if timeout_s is not None:
            self._page.set_default_timeout(int(timeout_s * 1000))
        try:
            if arg is None:
                return self._page.evaluate(js)
            return self._page.evaluate(js, arg)
        except Exception as e:
            # Strip playwright's verbose JS stack — keep only the first
            # line of the message (which has the useful "http 404: ..."
            # or similar). The full trace is still in the original
            # exception's `__cause__` for debugging.
            short = str(e).splitlines()[0] if str(e) else repr(e)
            short = short.removeprefix("Page.evaluate: Error: ")
            raise BrowserError(short) from e
        finally:
            if timeout_s is not None:
                self._page.set_default_timeout(self._DEFAULT_TIMEOUT_MS)

    def goto(self, url: str) -> None:
        self._connect()
        # ChatGPT is a long-running SPA: the `load` event may never fire
        # (service workers, long-poll keep-alives). `domcontentloaded` is
        # enough for our purposes — we drive everything through fetch in
        # the page context, not on rendered DOM.
        self._page.goto(url, wait_until="domcontentloaded")

    def current_url(self) -> str:
        self._connect()
        return self._page.url

    # ----------------------------------------------------------- tabs

    def tabs(self) -> list[dict]:
        """Return [{index, current, title, url}, ...] for the persistent
        context's pages. `current` marks the page this Browser is bound to
        (ie. the one evaluate/goto/etc. operate on).
        """
        self._connect()
        out = []
        for i, pg in enumerate(self._context.pages):
            try:
                title = pg.title()
            except Exception:
                title = ""
            out.append({
                "index": i,
                "current": pg is self._page,
                "title": title,
                "url": pg.url,
            })
        return out

    def tab_close(self, index: int) -> None:
        self._connect()
        pages = self._context.pages
        if not (0 <= index < len(pages)):
            raise BrowserError(f"tab index {index} out of range (have {len(pages)})")
        target = pages[index]
        if target is self._page:
            # We're closing our active page. Pick another chatgpt page,
            # otherwise leave self._page None — caller will reconnect.
            self._page = next(
                (p for p in self._context.pages if p is not target and "chatgpt.com" in p.url),
                None,
            )
        target.close()

    def find_tab_for_conv(self, conv_id: str) -> int | None:
        """Return index of a tab whose URL contains /c/<conv_id>, else None."""
        self._connect()
        for i, pg in enumerate(self._context.pages):
            if f"/c/{conv_id}" in pg.url:
                return i
        return None

    # ---------------------------------------------------------------- HTTP

    # Bundle metadata baked into the page; we send these on every
    # backend-api call so requests look identical to ones the React
    # app issues. Server-side anti-abuse can flag requests that omit
    # them — a thin "Authorization-only" header set is enough for the
    # happy path but is more likely to attract 429 / "unusual activity"
    # responses under load. Update both values when the page bundle
    # version rolls; they're trivially recoverable from any captured
    # /backend-api/* request via DevTools.
    _OAI_CLIENT_VERSION = "prod-5d86787f9f8d1f6b6e7e021b6aa4d6b14a14445c"
    _OAI_CLIENT_BUILD = "6232230"

    _HTTP_GET_JS = r"""
async ({ path, oai_client_version, oai_client_build }) => {
  function getCookie(name) {
    for (const c of document.cookie.split('; ')) {
      const i = c.indexOf('=');
      if (c.slice(0, i) === name) return decodeURIComponent(c.slice(i+1));
    }
    return null;
  }
  const sess = await (await fetch('/api/auth/session')).json();
  if (!sess || !sess.accessToken) throw new Error('not_logged_in');
  const oaiDid = JSON.parse(localStorage.getItem('oai-did') || '""');
  // statsig stores the session id under a per-host key; pluck whichever exists.
  let sessionId = null;
  for (let i = 0; i < localStorage.length; i++) {
    const k = localStorage.key(i);
    if (k && k.startsWith('statsig.session_id.')) {
      try { sessionId = JSON.parse(localStorage.getItem(k)).sessionID; break; } catch (_) {}
    }
  }
  if (!sessionId) sessionId = crypto.randomUUID();
  // Strip query for the target-path / target-route hints; the page
  // itself sends only the path component.
  const pathOnly = path.split('?')[0];
  const headers = {
    'authorization': 'Bearer ' + sess.accessToken,
    'oai-client-build-number': oai_client_build,
    'oai-client-version': oai_client_version,
    'oai-device-id': oaiDid,
    'oai-language': 'en-US',
    'oai-session-id': sessionId,
    'x-oai-is': getCookie('__Secure-oai-is'),
    'x-openai-target-path': pathOnly,
    'x-openai-target-route': pathOnly,
  };
  const r = await fetch(path, { headers });
  if (!r.ok) {
    const t = await r.text().catch(() => '');
    throw new Error('http ' + r.status + ': ' + t.slice(0, 300));
  }
  return await r.json();
}
"""

    def http_get(self, path: str) -> Any:
        """GET `path` (e.g. '/backend-api/conversations?...') from the page,
        attaching the Bearer + OAI-* identity headers the React app uses.

        Raises BrowserError on any non-2xx response.
        """
        if not path.startswith("/"):
            raise BrowserError(f"http_get path must be absolute, got: {path!r}")
        return self.evaluate(
            self._HTTP_GET_JS,
            {
                "path": path,
                "oai_client_version": self._OAI_CLIENT_VERSION,
                "oai_client_build": self._OAI_CLIENT_BUILD,
            },
        )
