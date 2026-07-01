"""Profile-aware filesystem layout for chatgpt-agent."""
from __future__ import annotations

import os
import re
from pathlib import Path

PROFILE_RE = re.compile(r"[A-Za-z0-9][A-Za-z0-9_.-]{0,63}")


def _config_root() -> Path:
    return Path(os.environ.get("XDG_CONFIG_HOME", Path.home() / ".config")) / "chatgpt-agent"


def _data_root() -> Path:
    return Path(os.environ.get("XDG_DATA_HOME", Path.home() / ".local" / "share")) / "chatgpt-agent"


def _cache_root() -> Path:
    return Path(os.environ.get("XDG_CACHE_HOME", Path.home() / ".cache")) / "chatgpt-agent"


def validate_profile(profile: str) -> str:
    """Return a safe profile name, or raise ValueError.

    Profile names become path components and lock identities, so keep them
    boring and portable.
    """
    if not PROFILE_RE.fullmatch(profile):
        raise ValueError(
            "invalid profile name; use 1-64 chars from A-Z, a-z, 0-9, '.', '_' or '-', "
            "starting with a letter or digit"
        )
    return profile


def profile_config_dir(profile: str) -> Path:
    profile = validate_profile(profile)
    return _config_root() / "profiles" / profile


def profile_data_dir(profile: str) -> Path:
    profile = validate_profile(profile)
    return _data_root() / "profiles" / profile


def sessions_file(profile: str) -> Path:
    return profile_config_dir(profile) / "sessions.json"


def lock_file(profile: str) -> Path:
    p = _config_root() / "locks"
    p.mkdir(parents=True, exist_ok=True)
    return p / f"{validate_profile(profile)}.lock"


def images_dir(profile: str, session_id: str) -> Path:
    return profile_data_dir(profile) / "images" / session_id


def chrome_user_data_dir(profile: str) -> Path:
    return profile_data_dir(profile) / "chrome"


def runtime_dir() -> Path:
    p = _cache_root() / "runtime"
    p.mkdir(parents=True, exist_ok=True)
    return p


def runtime_session_file(profile: str) -> Path:
    return runtime_dir() / f"{validate_profile(profile)}.session"
