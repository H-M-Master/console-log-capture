"""Schema-2 profile metadata storage for file-switcher.

This module intentionally manages metadata only.  It never copies, creates, or
removes target, cache, or snapshot files/directories.
"""
from __future__ import annotations

import hashlib
import json
import ntpath
import os
import re
import shutil
import stat
import tempfile
import uuid
from pathlib import Path
from typing import Any


_ID_RE = re.compile(r"^[a-zA-Z0-9_-]+$")
_RESERVED_NAMES = {
    "CON", "PRN", "AUX", "NUL",
    *(f"COM{i}" for i in range(1, 10)),
    *(f"LPT{i}" for i in range(1, 10)),
}


class ProfileStoreError(ValueError):
    """Base exception for invalid profile metadata."""


class ConfigConflictError(ProfileStoreError):
    """The config changed since it was read or since the expected hash."""


class ConfigFormatError(ProfileStoreError):
    """The config file is not valid JSON or is not an object."""


def config_hash(path: os.PathLike[str] | str) -> str | None:
    """Return the SHA-256 hash of *path*'s bytes, or ``None`` if absent."""
    file_path = Path(path)
    if not file_path.exists():
        return None
    if not file_path.is_file():
        raise ValueError(f"配置不是文件：{file_path}")
    digest = hashlib.sha256()
    with file_path.open("rb") as stream:
        for chunk in iter(lambda: stream.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def new_id() -> str:
    """Return a stable-id-compatible random identifier."""
    return str(uuid.uuid4())


def _as_path(value: os.PathLike[str] | str) -> Path:
    return Path(value).expanduser().resolve(strict=False)


def _case_key(value: str) -> str:
    return value.replace("/", "\\").rstrip("\\").casefold()


def _relative_file(value: Any, label: str) -> str:
    if not isinstance(value, str) or not value.strip():
        raise ProfileStoreError(f"{label} 必须是非空相对文件路径")
    raw = value.replace("/", "\\")
    drive, tail = ntpath.splitdrive(raw)
    if drive or ntpath.isabs(raw) or raw.startswith(("\\", "/")):
        raise ProfileStoreError(f"{label} 必须是 Windows 相对路径：{value}")
    parts = raw.split("\\")
    if not parts or any(part in ("", ".", "..") for part in parts):
        raise ProfileStoreError(f"{label} 不是严格相对文件路径：{value}")
    for part in parts:
        # A colon anywhere in a component admits an NTFS alternate data stream.
        if ":" in part or any(char in part for char in "*?[]"):
            raise ProfileStoreError(f"{label} 含有非法 Windows 路径字符：{value}")
        if part.endswith((" ", ".")) or part.upper() in _RESERVED_NAMES:
            raise ProfileStoreError(f"{label} 含有非法 Windows 文件名：{value}")
    return value


def _absolute_path(value: Any, label: str, base: Path | None = None) -> Path:
    """Resolve absolute paths as-is and relative paths from the config dir."""
    if not isinstance(value, str) or not value.strip():
        raise ProfileStoreError(f"{label} 必须是路径")
    raw = value
    drive, tail = ntpath.splitdrive(raw)
    if any(":" in part for part in tail.replace("/", "\\").split("\\") if part):
        raise ProfileStoreError(f"{label} 含有 NTFS ADS：{value}")
    if os.path.isabs(raw) or ntpath.isabs(raw):
        candidate = Path(os.path.abspath(os.path.expanduser(raw)))
    else:
        if base is None:
            raise ProfileStoreError(f"{label} 缺少配置目录：{value}")
        candidate = Path(os.path.abspath(os.path.join(str(base), os.path.expanduser(raw))))
    # Keep lexical paths so reparse checks can inspect every parent.
    return candidate


def _canonical(path: Path) -> str:
    return os.path.normcase(str(path.resolve(strict=False))).casefold().rstrip("\\/") or os.path.sep


def _overlap(left: Path, right: Path) -> bool:
    a, b = _canonical(left), _canonical(right)
    try:
        return os.path.commonpath([a, b]) in (a, b)
    except ValueError:
        return False


def _is_reparse(path: Path) -> bool:
    try:
        info = os.lstat(path)
    except FileNotFoundError:
        return False
    if stat.S_ISLNK(info.st_mode):
        return True
    attrs = getattr(info, "st_file_attributes", 0)
    return bool(attrs & getattr(stat, "FILE_ATTRIBUTE_REPARSE_POINT", 0x400))


def _has_reparse_in_parents(path: Path) -> bool:
    current = path
    chain: list[Path] = []
    while True:
        chain.append(current)
        if current.parent == current:
            break
        current = current.parent
    return any(_is_reparse(item) for item in chain)


def _check_target(path: Path, label: str) -> None:
    if not path.exists() or not path.is_dir():
        raise ProfileStoreError(f"{label} 不存在或不是目录：{path}")
    if _has_reparse_in_parents(path):
        raise ProfileStoreError(f"{label} 或其父目录是 reparse/symlink：{path}")
    if path.anchor and _canonical(path) == _canonical(Path(path.anchor)):
        raise ProfileStoreError(f"{label} 不得为盘根：{path}")

    roots: list[Path] = []
    for env_name in ("SystemRoot", "WINDIR"):
        value = os.environ.get(env_name)
        if value:
            roots.append(_as_path(value))
    home = Path.home().resolve(strict=False)
    roots.extend((home, home.parent))
    # On Windows this also covers the usual system and user roots when the
    # corresponding environment variables are absent in a test process.
    if os.name == "nt":
        roots.extend((_as_path(r"C:\Windows"), _as_path(r"C:\Users")))
    if any(_canonical(path) == _canonical(root) for root in roots):
        raise ProfileStoreError(f"{label} 不得为系统根或用户根：{path}")


def _validate_list(value: Any, label: str) -> None:
    if not isinstance(value, list):
        raise ProfileStoreError(f"{label} 必须是数组")
    # Metadata is deliberately not interpreted: callers may evolve guard/cache
    # records without this storage layer rewriting or dropping them.
    try:
        json.dumps(value, ensure_ascii=False)
    except (TypeError, ValueError) as exc:
        raise ProfileStoreError(f"{label} 必须是 JSON 元数据") from exc


def validate_config(
    data: dict[str, Any],
    base: os.PathLike[str] | str,
    require_targets: bool = True,
) -> dict[str, Any]:
    """Validate and return schema-2 metadata without mutating it.

    ``base`` is the resolved config workspace/store directory.  Target roots
    may not be it or anything below it.  ``require_targets=False`` is useful
    for an initial metadata edit before target directories have been created.
    """
    if not isinstance(data, dict):
        raise ProfileStoreError("配置根必须是 JSON 对象")
    if data.get("schemaVersion") != 2:
        raise ProfileStoreError("schemaVersion 必须为 2")
    base_path = _as_path(base)
    default_id = data.get("defaultProfileId")
    if not isinstance(default_id, str) or not _ID_RE.fullmatch(default_id):
        raise ProfileStoreError("defaultProfileId 不是安全稳定 ID")
    profiles = data.get("profiles")
    if not isinstance(profiles, list) or not profiles:
        raise ProfileStoreError("profiles 必须是非空数组")

    profile_ids: set[str] = set()
    profile_names: set[str] = set()
    targets: list[tuple[str, Path]] = []
    for index, profile in enumerate(profiles):
        label = f"profiles[{index}]"
        if not isinstance(profile, dict):
            raise ProfileStoreError(f"{label} 必须是对象")
        pid = profile.get("id")
        if not isinstance(pid, str) or not _ID_RE.fullmatch(pid):
            raise ProfileStoreError(f"{label}.id 不是安全稳定 ID")
        pid_key = pid.casefold()
        if pid_key in profile_ids:
            raise ProfileStoreError(f"profile id 重复：{pid}")
        profile_ids.add(pid_key)
        name = profile.get("name")
        if not isinstance(name, str) or not name.strip():
            raise ProfileStoreError(f"{label}.name 必须是非空字符串")
        if name.casefold() in profile_names:
            raise ProfileStoreError(f"profile name 重复：{name}")
        profile_names.add(name.casefold())

        target_raw = profile.get("targetRoot")
        target = _absolute_path(target_raw, f"{label}.targetRoot", base_path)
        if _overlap(target, base_path):
            raise ProfileStoreError(f"targetRoot 不得与 config 工作区/store 重叠：{target}")
        if require_targets:
            _check_target(target, f"{label}.targetRoot")
        targets.append((pid, target))

        managed = profile.get("managed")
        if not isinstance(managed, list):
            raise ProfileStoreError(f"{label}.managed 必须是数组")
        managed_keys: set[str] = set()
        for mindex, item in enumerate(managed):
            if not isinstance(item, dict):
                raise ProfileStoreError(f"{label}.managed[{mindex}] 必须是对象")
            path = _relative_file(item.get("path"), f"{label}.managed[{mindex}].path")
            key = _case_key(path)
            if key in managed_keys:
                raise ProfileStoreError(f"managed 文件重复：{path}")
            managed_keys.add(key)

        states = profile.get("states")
        if not isinstance(states, list):
            raise ProfileStoreError(f"{label}.states 必须是数组")
        state_ids: set[str] = set()
        state_names: set[str] = set()
        state_roots: list[Path] = []
        for sindex, state in enumerate(states):
            slabel = f"{label}.states[{sindex}]"
            if not isinstance(state, dict):
                raise ProfileStoreError(f"{slabel} 必须是对象")
            sid = state.get("id")
            if not isinstance(sid, str) or not _ID_RE.fullmatch(sid):
                raise ProfileStoreError(f"{slabel}.id 不是安全稳定 ID")
            if sid.casefold() in state_ids:
                raise ProfileStoreError(f"state id 重复：{sid}")
            state_ids.add(sid.casefold())
            sname = state.get("name")
            if not isinstance(sname, str) or not sname.strip():
                raise ProfileStoreError(f"{slabel}.name 必须是非空字符串")
            if sname.casefold() in state_names:
                raise ProfileStoreError(f"同 profile 的 state name 重复：{sname}")
            state_names.add(sname.casefold())
            snapshot = state.get("snapshotRoot")
            if snapshot is not None:
                root = _absolute_path(snapshot, f"{slabel}.snapshotRoot", base_path)
                state_roots.append(root)
                if _overlap(root, target):
                    raise ProfileStoreError(f"snapshotRoot 不得与 targetRoot 重叠：{root}")

        _validate_list(profile.get("guards", []), f"{label}.guards")
        _validate_list(profile.get("cacheDirectories", []), f"{label}.cacheDirectories")

    if default_id.casefold() not in profile_ids:
        raise ProfileStoreError("defaultProfileId 不存在于 profiles")
    for index, (_, target) in enumerate(targets):
        for other_id, other in targets[index + 1 :]:
            if _overlap(target, other):
                raise ProfileStoreError(f"多个 targetRoot 重叠：{target} 与 {other} ({other_id})")
    return data


class ProfileStore:
    """Read and atomically write profile metadata at one config path."""

    def __init__(self, config_path: os.PathLike[str] | str):
        self.config_path = _as_path(config_path)
        self.base = self.config_path.parent.resolve(strict=False)
        self._loaded_hash: str | None = None

    def load(self) -> dict[str, Any]:
        raw = self.config_path.read_bytes()
        try:
            data = json.loads(raw.decode("utf-8"))
        except (UnicodeDecodeError, json.JSONDecodeError) as exc:
            raise ConfigFormatError(f"配置 JSON 无效：{self.config_path}") from exc
        validate_config(data, self.base, require_targets=True)
        self._loaded_hash = hashlib.sha256(raw).hexdigest()
        return data

    def save(self, data: dict[str, Any], expected_hash: str | None = None) -> None:
        validate_config(data, self.base, require_targets=True)
        current = config_hash(self.config_path)
        expected = expected_hash if expected_hash is not None else self._loaded_hash
        if expected is not None and current != expected:
            raise ConfigConflictError(
                f"配置已被外部修改：expected={expected}, actual={current}"
            )

        self.base.mkdir(parents=True, exist_ok=True)
        payload = (json.dumps(data, ensure_ascii=False, indent=2) + "\n").encode("utf-8")
        fd, temp_name = tempfile.mkstemp(prefix=f".{self.config_path.name}.", suffix=".tmp", dir=self.base)
        temp_path = Path(temp_name)
        try:
            with os.fdopen(fd, "wb") as stream:
                stream.write(payload)
                stream.flush()
                os.fsync(stream.fileno())
            backup = self.config_path.with_name(self.config_path.name + ".bak")
            if self.config_path.exists():
                shutil.copy2(self.config_path, backup)
                # Windows rejects fsync on a read-only descriptor; use a
                # writable descriptor solely to flush the copied backup.
                with backup.open("r+b") as stream:
                    os.fsync(stream.fileno())
            os.replace(temp_path, self.config_path)
            try:
                directory_fd = os.open(self.base, os.O_RDONLY)
            except (OSError, TypeError):
                directory_fd = None
            if directory_fd is not None:
                try:
                    os.fsync(directory_fd)
                finally:
                    os.close(directory_fd)
            self._loaded_hash = hashlib.sha256(payload).hexdigest()
        except Exception:
            try:
                temp_path.unlink(missing_ok=True)
            except OSError:
                pass
            raise


def _legacy_absolute(raw: Any, legacy_dir: Path) -> Path:
    if not isinstance(raw, str) or not raw:
        raise ProfileStoreError("v1 路径无效")
    candidate = Path(raw)
    return (candidate if candidate.is_absolute() or ntpath.isabs(raw) else legacy_dir / candidate).resolve(strict=False)


def _safe_legacy_id(value: str, fallback: str) -> str:
    cleaned = re.sub(r"[^a-zA-Z0-9_-]+", "-", value).strip("-")
    return cleaned if _ID_RE.fullmatch(cleaned or "") else fallback


def import_legacy(legacy_path: os.PathLike[str] | str, config_path: os.PathLike[str] | str) -> dict[str, Any]:
    """Read a v1 config and return schema-2 metadata plus migration candidates.

    No output file and no target/snapshot/cache filesystem item is created or
    modified.  Source paths are made absolute for the future snapshot backend.
    """
    source_path = Path(legacy_path).expanduser().resolve(strict=False)
    config_destination = Path(config_path).expanduser().resolve(strict=False)
    try:
        legacy = json.loads(source_path.read_text(encoding="utf-8"))
    except (OSError, UnicodeError, json.JSONDecodeError) as exc:
        raise ConfigFormatError(f"无法读取旧配置：{source_path}") from exc
    if not isinstance(legacy, dict) or legacy.get("schemaVersion") != 1:
        raise ProfileStoreError("旧配置 schemaVersion 必须为 1")
    legacy_dir = source_path.parent
    target_root = _legacy_absolute(legacy.get("targetRoot"), legacy_dir)
    entries = legacy.get("entries") or []
    modes = legacy.get("modes") or {}
    if not isinstance(entries, list) or not isinstance(modes, dict):
        raise ProfileStoreError("旧配置 entries/modes 格式无效")
    mode_names = list(modes.keys())
    if not mode_names:
        mode_names = sorted({mode for entry in entries if isinstance(entry, dict) for mode in (entry.get("sources") or {})})

    managed: list[dict[str, str]] = []
    managed_keys: set[str] = set()
    states: list[dict[str, Any]] = []
    candidates: list[dict[str, Any]] = []
    profile_id = _safe_legacy_id(target_root.name or "profile", "profile")
    for entry in entries:
        if not isinstance(entry, dict) or not isinstance(entry.get("target"), str):
            continue
        path = _relative_file(entry["target"], "旧 entries.target")
        if _case_key(path) not in managed_keys:
            managed.append({"path": path})
            managed_keys.add(_case_key(path))

    for index, mode in enumerate(mode_names):
        if not isinstance(mode, str):
            continue
        state_id = _safe_legacy_id(mode, f"state-{index + 1}")
        used = {item["id"].casefold() for item in states}
        if state_id.casefold() in used:
            state_id = f"{state_id}-{index + 1}"
        source_files: list[Path] = []
        for entry in entries:
            if not isinstance(entry, dict):
                continue
            value = (entry.get("sources") or {}).get(mode)
            if isinstance(value, str):
                source_files.append(_legacy_absolute(value, legacy_dir))
        common_root: Path | None = None
        if source_files:
            try:
                common_root = Path(os.path.commonpath([str(item.parent) for item in source_files])).resolve(strict=False)
            except ValueError:
                common_root = source_files[0].parent
        state = {"id": state_id, "name": mode, "snapshotRoot": str(common_root) if common_root else None}
        states.append(state)
        candidates.append({
            "stateid": state_id,
            "name": mode,
            "sourceRoot": str(common_root) if common_root else None,
            "sourceFiles": [str(item) for item in source_files],
        })

    profile_name = str(legacy.get("name") or target_root.name or "Imported profile")
    result: dict[str, Any] = {
        "schemaVersion": 2,
        "defaultProfileId": profile_id,
        "profiles": [{
            "id": profile_id,
            "name": profile_name,
            "targetRoot": str(target_root),
            "managed": managed,
            "states": states,
            "guards": [],
            "cacheDirectories": list(legacy.get("cacheDirectories") or []),
        }],
        "migration_candidates": candidates,
    }
    # Keep the destination meaningful to callers while remaining read-only:
    # resolving it is intentional, but it is never opened or written here.
    _ = config_destination
    return result


__all__ = [
    "ConfigConflictError",
    "ConfigFormatError",
    "ProfileStore",
    "ProfileStoreError",
    "config_hash",
    "import_legacy",
    "new_id",
    "validate_config",
]
