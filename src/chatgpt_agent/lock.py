"""Per-profile advisory file lock.

Wraps fcntl.flock so the OS-level lock is automatically released when our
process dies (no stale lockfiles). The lockfile records the holder's pid +
command name as a courtesy for diagnostics.
"""
from __future__ import annotations

import contextlib
import errno
import fcntl
import os
import time
from pathlib import Path

from . import paths


class LockError(RuntimeError):
    pass


@contextlib.contextmanager
def acquire(profile: str, command: str, *, wait_s: float = 60.0):
    """Hold the per-profile lock for the duration of the with-block.

    If wait_s == 0, fail immediately when contended.
    Otherwise spin (with 0.2s sleeps) up to wait_s seconds.
    """
    path = paths.lock_file(profile)
    fp = path.open("a+")
    deadline = time.monotonic() + max(wait_s, 0.0)
    held_by = None
    while True:
        try:
            fcntl.flock(fp.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
            break
        except OSError as e:
            if e.errno not in (errno.EWOULDBLOCK, errno.EAGAIN):
                fp.close()
                raise
            held_by = _peek_holder(path)
            if wait_s == 0 or time.monotonic() >= deadline:
                fp.close()
                raise LockError(
                    f"another chatgpt-agent instance is running"
                    + (f" ({held_by})" if held_by else "")
                ) from None
            time.sleep(0.2)
    try:
        # Replace contents with our identity for diagnostics.
        fp.seek(0)
        fp.truncate()
        fp.write(f"pid={os.getpid()} cmd={command}\n")
        fp.flush()
        yield
    finally:
        try:
            fcntl.flock(fp.fileno(), fcntl.LOCK_UN)
        finally:
            fp.close()


def _peek_holder(path: Path) -> str | None:
    try:
        return path.read_text().strip() or None
    except OSError:
        return None
