"""The only path that reads or writes the metadata file.

``profile_store`` owns validation and atomic replacement; this wrapper exists so
the rest of the tool has one import to depend on and one exception type to
catch, and so a missing metadata module is a clear startup error instead of a
silent downgrade to a weaker validator.
"""
from __future__ import annotations

from pathlib import Path
from typing import Any

try:
    from profile_store import (  # type: ignore
        ConfigConflictError,
        ConfigFormatError,
        ProfileStore,
        ProfileStoreError,
        config_hash,
        import_legacy,
        new_id,
        validate_config,
    )
except ImportError as exc:  # pragma: no cover - packaging fault, not user error
    raise ImportError(
        "缺少 profile_store 模块：它负责校验并原子写入配置，必须与主程序一起发布。"
    ) from exc

__all__ = [
    "ConfigConflictError",
    "ConfigFormatError",
    "ConfigStore",
    "ProfileStoreError",
    "config_hash",
    "import_legacy",
    "new_id",
    "validate_config",
]


class ConfigStore:
    """Metadata store bound to one config path.

    ``save`` takes the hash returned by the last ``load`` and refuses to write
    if the file changed underneath, so two open windows can never clobber each
    other's edits.
    """

    def __init__(self, config_path: str | Path):
        self.path = Path(config_path)
        self._store = ProfileStore(self.path)

    def load(self) -> dict[str, Any]:
        return self._store.load()

    def save(self, data: dict[str, Any], expected_hash: str | None = None) -> None:
        self._store.save(data, expected_hash=expected_hash)

    def current_hash(self) -> str | None:
        """Hash of the file on disk, or ``None`` when it does not exist yet."""
        return config_hash(self.path)

    def exists(self) -> bool:
        return self.path.is_file()
