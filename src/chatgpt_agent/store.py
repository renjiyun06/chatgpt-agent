"""Per-profile local cache of conversations we've created."""
from __future__ import annotations

import json
from dataclasses import asdict, dataclass, field
from datetime import datetime, timezone

from . import paths


@dataclass
class Session:
    id: str
    title: str = ""
    created_at: str = field(default_factory=lambda: datetime.now(timezone.utc).isoformat())
    model: str | None = None
    thinking: str | None = None


def _load_raw(profile: str) -> dict:
    f = paths.sessions_file(profile)
    if not f.exists():
        return {"sessions": []}
    return json.loads(f.read_text())


def _save_raw(profile: str, data: dict) -> None:
    f = paths.sessions_file(profile)
    f.parent.mkdir(parents=True, exist_ok=True)
    f.write_text(json.dumps(data, indent=2, ensure_ascii=False))


def list_sessions(profile: str) -> list[Session]:
    return [Session(**row) for row in _load_raw(profile)["sessions"]]


def add_session(profile: str, s: Session) -> None:
    data = _load_raw(profile)
    data["sessions"] = [r for r in data["sessions"] if r["id"] != s.id]
    data["sessions"].insert(0, asdict(s))
    _save_raw(profile, data)


def get_session(profile: str, session_id: str) -> Session | None:
    for row in _load_raw(profile)["sessions"]:
        if row["id"] == session_id:
            return Session(**row)
    return None
