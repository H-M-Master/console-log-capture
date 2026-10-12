"""The only code that talks to ``folder-switcher.ps1``.

Everything the backend can say arrives as one line of JSON on stdout:

    {schemaVersion, ok, action, data, error:{code,message}, transaction}

This module runs the process, validates that envelope, and normalizes failures
into a ``BackendError`` carrying a code.  Nothing above this layer parses
PowerShell output or interprets raw error text.

Note on the execution policy: the backend is a local ``.ps1`` file, which the
``RemoteSigned`` default (the Windows default) permits.  Pass ``bypass_policy``
only if a machine's policy is actually blocking signed-free local scripts.
"""
from __future__ import annotations

import json
import subprocess
import threading
import time
from pathlib import Path
from typing import Any, Callable

from . import errors, paths

# Read-only actions are capped; a large target directory makes them slow, but a
# hung Status should never leave the window stuck forever.
QUERY_ACTIONS = {"status", "scan", "diff"}
QUERY_TIMEOUT = 45.0


def _as_text(value: Any) -> str:
    if value is None:
        return ""
    if isinstance(value, bytes):
        return value.decode("utf-8", errors="replace")
    return str(value)


class BackendError(Exception):
    """A failure with a code the UI can translate.  Never swallowed silently."""

    def __init__(self, code: str, message: str = "", detail: str = ""):
        super().__init__(message or code)
        self.code = code
        self.message = message
        self.detail = detail

    def as_payload(self) -> dict[str, Any]:
        """Shape this error like a backend result, for the UI's one error path."""
        return {"schemaVersion": 2, "ok": False, "action": "",
                "data": None, "error": {"code": self.code, "message": self.message}}


class PowerShellRunner:
    """Runs one backend operation at a time and reports only from its worker.

    ``run`` returns immediately; ``callback`` fires on a worker thread, so the
    UI is expected to hand results back to its own thread (the app uses a queue).
    """

    def __init__(self, callback: Callable[[dict[str, Any]], None] | None = None,
                 script_path: str | Path = paths.SCRIPT,
                 config_path: str | Path = paths.CONFIG,
                 state_root: str | Path = paths.STATE_ROOT,
                 query_timeout: float = QUERY_TIMEOUT,
                 bypass_policy: bool = False):
        self.callback = callback or (lambda result: None)
        self.script_path = Path(script_path)
        self.config_path = Path(config_path)
        self.state_root = Path(state_root)
        self.query_timeout = query_timeout
        self.bypass_policy = bypass_policy
        self.running = False
        self._lock = threading.Lock()

    def command(self, args: list[str]) -> list[str]:
        """The exact argv used, exposed so tests and logs can assert on it."""
        policy = "Bypass" if self.bypass_policy else "RemoteSigned"
        return ["powershell.exe", "-NoLogo", "-NoProfile", "-NonInteractive",
                "-ExecutionPolicy", policy, "-File", str(self.script_path),
                "-Config", str(self.config_path),
                "-StateRoot", str(self.state_root)] + list(args)

    def run(self, args: list[str] | tuple[str, ...],
            callback: Callable[[dict[str, Any]], None] | None = None) -> None:
        with self._lock:
            if self.running:
                raise RuntimeError("已有目录操作正在执行")
            self.running = True
        threading.Thread(target=self._worker, args=(list(args), callback),
                         daemon=True).start()

    def _worker(self, args: list[str],
                callback: Callable[[dict[str, Any]], None] | None) -> None:
        started = time.time()
        command = self.command(args)
        action = ""
        if "-Action" in args:
            action = str(args[args.index("-Action") + 1]).lower()
        try:
            completed = subprocess.run(
                command, capture_output=True, text=True,
                encoding="utf-8", errors="replace",
                timeout=self.query_timeout if action in QUERY_ACTIONS else None)
            result = {"returncode": completed.returncode, "stdout": completed.stdout,
                      "stderr": completed.stderr, "duration": time.time() - started,
                      "command": command}
        except subprocess.TimeoutExpired as exc:
            result = {"returncode": -2, "stdout": _as_text(exc.stdout),
                      "stderr": "查询超时", "duration": time.time() - started,
                      "timeout": True, "command": command}
        except Exception as exc:
            result = {"returncode": -1, "stdout": "", "stderr": str(exc),
                      "duration": time.time() - started, "command": command}
        with self._lock:
            self.running = False
        (callback or self.callback)(result)


def parse_result(result: dict[str, Any]) -> dict[str, Any]:
    """Validate a raw run result and return the backend envelope.

    Raises ``BackendError`` for transport problems and for envelopes that break
    the schema contract; a well-formed envelope that reports ``ok: false`` is
    returned as-is so callers handle it with everything else.
    """
    raw = _as_text(result.get("stdout")).strip()
    stderr = _as_text(result.get("stderr")).strip()
    returncode = result.get("returncode")

    if result.get("timeout"):
        raise BackendError("backend-timeout", stderr or "查询超时",
                           detail=f"命令：{result.get('command')}")
    if returncode == -2:
        raise BackendError("backend-timeout", stderr or "查询超时")
    if returncode == -1:
        raise BackendError("backend-start-failed", stderr or "无法启动后端", detail=stderr)

    lines = [line for line in raw.splitlines() if line.strip()]
    if not lines:
        raise BackendError("backend-no-output", stderr or "后端没有返回结果", detail=stderr)
    try:
        payload = json.loads(lines[-1])
    except json.JSONDecodeError as exc:
        raise BackendError("backend-bad-json", f"后端返回的结果无法解析：{exc}",
                           detail=raw or stderr) from exc

    if not isinstance(payload, dict):
        raise BackendError("backend-contract", "后端返回的 JSON 不是对象", detail=raw)
    required = ("schemaVersion", "ok", "action", "data", "error")
    if payload.get("schemaVersion") != 2 or not isinstance(payload.get("ok"), bool) \
            or any(key not in payload for key in required):
        raise BackendError("backend-contract", "后端 JSON 不符合 schemaVersion 2 合同",
                           detail=raw)
    # Safety net: a self-contradictory answer is not used in either direction.
    ok, code = bool(payload["ok"]), int(returncode or 0)
    if (ok and code != 0) or ((not ok) and code == 0):
        raise BackendError("backend-exit-mismatch", "后端 ok 与进程退出码不一致",
                           detail=raw)
    return payload


def error_from(payload: dict[str, Any] | None) -> BackendError:
    """Build a ``BackendError`` from a backend envelope that reported failure."""
    error = (payload or {}).get("error") or {}
    return BackendError(str(error.get("code") or "operation-failed"),
                        str(error.get("message") or ""))


def explain(result_or_error: Any) -> tuple[str, str, str, str]:
    """Translate anything that went wrong into plain Chinese.

    Accepts a raw runner result, a backend envelope, or a ``BackendError`` so
    the UI has exactly one call to make regardless of where the failure arose.
    """
    if isinstance(result_or_error, BackendError):
        return errors.explain(result_or_error.as_payload())
    if isinstance(result_or_error, dict) and "stdout" in result_or_error:
        try:
            return errors.explain(parse_result(result_or_error))
        except BackendError as exc:
            return errors.explain(exc.as_payload())
    return errors.explain(result_or_error)


# Backward-compatible name used by small integrations.
Runner = PowerShellRunner
