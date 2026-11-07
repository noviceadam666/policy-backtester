"""Where Hermes keeps each profile's history, and where this plugin keeps its index."""

from __future__ import annotations

import os
from dataclasses import dataclass
from pathlib import Path

DEFAULT_PROFILE = "default"


@dataclass(frozen=True)
class Profile:
    name: str
    home: Path

    @property
    def state_db(self) -> Path:
        return self.home / "state.db"


def default_root() -> Path:
    """The Hermes root that holds every profile, even when run under ``hermes -p <name>``."""
    try:
        from hermes_constants import get_default_hermes_root
    except ImportError:
        home = os.environ.get("HERMES_HOME")
        if not home:
            return Path.home() / ".hermes"
        path = Path(home).expanduser()
        return path.parent.parent if path.parent.name == "profiles" else path
    return Path(get_default_hermes_root())


def default_index(root: Path) -> Path:
    return root / "backtest" / "index.db"


def all_profiles(root: Path, only: list[str] | None = None) -> list[Profile]:
    """The default profile plus every named one that isn't tombstoned, with or without history."""
    found = [Profile(DEFAULT_PROFILE, root)]
    profiles_dir = root / "profiles"
    if profiles_dir.is_dir():
        tombstones = profiles_dir / ".deleted"
        for child in sorted(profiles_dir.iterdir()):
            if child.name.startswith(".") or not child.is_dir():
                continue
            if (tombstones / child.name).exists():
                continue
            found.append(Profile(child.name, child))
    if only:
        found = [p for p in found if p.name in only]
    return found


def discover_profiles(root: Path, only: list[str] | None = None) -> list[Profile]:
    """The profiles that have a ``state.db`` to read."""
    return [p for p in all_profiles(root, only) if p.state_db.is_file()]
