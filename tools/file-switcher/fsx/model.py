"""Read-only views over the loaded configuration.

The old UI recovered the selected profile and state by re-parsing combo-box
labels of the form ``"名字 [id]"``, which broke whenever a name contained
brackets and had to be repeated at every call site.  These views keep the id as
the single source of truth and derive the label only for display.

Nothing here writes to disk; mutations stay in the UI layer, which also owns
the decision to save.
"""
from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
from typing import Any


@dataclass(frozen=True)
class Item:
    """One profile or state, with its display label kept alongside the id."""
    id: str
    name: str
    raw: dict[str, Any]

    @property
    def label(self) -> str:
        return f"{self.name} [{self.id}]" if self.name else self.id

    @property
    def display_name(self) -> str:
        return self.name or self.id


def label_for(entry: dict[str, Any]) -> str:
    """The combo-box label for a profile or state entry."""
    return Item(str(entry.get("id") or ""),
                str(entry.get("name") or ""),
                entry).label


def _items(entries: Any) -> list[Item]:
    return [Item(str(e.get("id") or ""), str(e.get("name") or ""), e)
            for e in (entries or []) if isinstance(e, dict)]


class Config:
    """A loaded configuration document, queried by id."""

    def __init__(self, data: dict[str, Any] | None = None, base: Path | None = None):
        self.data = data or {}
        self.base = base or Path(".")

    # ---------- profiles ----------
    @property
    def profiles(self) -> list[Item]:
        return _items(self.data.get("profiles"))

    def profile(self, profile_id: str | None) -> Item | None:
        if not profile_id:
            return None
        return next((p for p in self.profiles if p.id == profile_id), None)

    @property
    def default_profile_id(self) -> str:
        return str(self.data.get("defaultProfileId") or "")

    def initial_profile_id(self) -> str:
        """The default when it still exists, otherwise the first profile."""
        if self.profile(self.default_profile_id):
            return self.default_profile_id
        return self.profiles[0].id if self.profiles else ""

    # ---------- states ----------
    def states(self, profile_id: str | None) -> list[Item]:
        profile = self.profile(profile_id)
        return _items(profile.raw.get("states")) if profile else []

    def state(self, profile_id: str | None, state_id: str | None) -> Item | None:
        if not state_id:
            return None
        return next((s for s in self.states(profile_id) if s.id == state_id), None)

    def is_captured(self, profile_id: str | None, state_id: str | None) -> bool:
        """True when a version has saved content that the backend may still use."""
        state = self.state(profile_id, state_id)
        if not state:
            return False
        return bool(state.raw.get("snapshotRoot")) and not state.raw.get("needsCapture")

    # ---------- managed paths ----------
    def managed_paths(self, profile_id: str | None) -> set[str]:
        """Managed paths, normalized to backslash form and compared case-folded."""
        profile = self.profile(profile_id)
        if not profile:
            return set()
        return {str(m.get("path", "")).replace("/", "\\").casefold()
                for m in (profile.raw.get("managed") or []) if isinstance(m, dict)}

    def target_root(self, profile_id: str | None) -> Path | None:
        """Absolute target directory, resolving a relative one against the config."""
        profile = self.profile(profile_id)
        if not profile:
            return None
        raw = str(profile.raw.get("targetRoot") or "")
        if not raw:
            return None
        root = Path(raw)
        return root if root.is_absolute() else (self.base / root)

    @property
    def is_empty(self) -> bool:
        return not self.data.get("profiles")
