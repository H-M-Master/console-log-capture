"""The application window: views, backend calls, and result handling.

Three things are worth knowing before editing this file.

**One result queue, one handler.**  Every backend call reports through
``self.events`` and is drained by ``poll`` on the Tk thread.  Views never touch
backend results directly.

**Intents, not flags.**  A click sets exactly one ``_intent`` describing what to
do with the *next* result (``preview`` / ``switch`` / ``capture``).  This
replaces the old pile of booleans whose interaction caused one-click switching
to silently do nothing.

**Diffs always precede writes.**  Switching runs Diff → confirm → Apply with the
plan digest, so the files touched are known before anything is written.
"""
from __future__ import annotations

import json
import os
import queue
import subprocess
import sys
import time
import traceback
import tkinter as tk
from pathlib import Path
from tkinter import filedialog, simpledialog, ttk
from typing import Any, Callable

from .. import backend, paths
from ..model import Config
from ..store import ConfigStore, ProfileStoreError, new_id
from . import dialogs
from .advanced import AdvancedView
from .home import HomeView
from .theme import Theme
from .wizard import Wizard

# What to do once the pending Diff result arrives.
INTENT_NONE = ""
INTENT_PREVIEW = "preview"
INTENT_SWITCH = "switch"
INTENT_CAPTURE_SCAN = "capture-scan"


class App(tk.Tk):
    """Main window: a simple home screen plus an advanced management screen."""

    def __init__(self, config_path: str | Path = paths.CONFIG,
                 state_root: str | Path | None = None,
                 script_path: str | Path = paths.SCRIPT,
                 runner: Any = None,
                 log_root: str | Path | None = None,
                 auto_refresh: bool = True):
        super().__init__()
        self.config_path = Path(config_path)
        self.state_root = Path(state_root) if state_root is not None else paths.STATE_ROOT
        self.log_root = Path(log_root) if log_root is not None else paths.LOG_ROOT

        self.title("AnyTestTools · 文件版本切换")
        self.theme = Theme(self)
        self.geometry(f"{self.theme.scale(1080)}x{self.theme.scale(720)}")
        self.minsize(self.theme.scale(900), self.theme.scale(600))

        self.events: queue.Queue[dict[str, Any]] = queue.Queue()
        self.runner = runner or backend.PowerShellRunner(
            lambda result: self.events.put(result),
            script_path, self.config_path, self.state_root)
        self.store = ConfigStore(self.config_path)

        self.config_data: dict[str, Any] = {}
        self.config = Config()
        self._config_hash: str | None = None
        self._busy = False
        self._closing = False
        self._poll_id: str | None = None
        self._intent = INTENT_NONE
        self._pending_capture: tuple[str, str | None] | None = None
        self._last_plan: dict[str, Any] | None = None
        self._last_status: dict[str, Any] | None = None
        self._active_profile_id = ""
        self._active_state_id = ""
        # A home-screen click has already been confirmed, so the apply step
        # must not ask a second time.
        self._switch_confirmed = False

        self.protocol("WM_DELETE_WINDOW", self.close)
        self.report_callback_exception = self._report_callback_exception

        self._build_ui()
        self.load_config(show_error=False)
        self._poll_id = self.after(80, self.poll)
        if auto_refresh:
            self.refresh_status()

    # ---------- construction ----------
    def _build_ui(self) -> None:
        self.home = HomeView(self, on_switch=self.switch_to, on_wizard=self.start_wizard,
                             on_advanced=self.show_advanced, on_undo=self.undo_last_switch)
        self.advanced = AdvancedView(self, app=self)
        self.home.pack(fill="both", expand=True)

    def show_home(self) -> None:
        if self.advanced.winfo_ismapped():
            self.advanced.pack_forget()
        self.home.pack(fill="both", expand=True)
        self.home.show(self.config, self._last_status)
        self._update_undo()

    def show_advanced(self) -> None:
        self.home.pack_forget()
        self.advanced.pack(fill="both", expand=True)
        self.advanced.fill_profiles(self.config, self._active_profile_id or self.config.initial_profile_id())
        self.advanced.fill_states(self.config, self.active_profile_id(), self._active_state_id)
        self._update_apply_button()

    # ---------- configuration ----------
    def load_config(self, show_error: bool = True) -> None:
        """Load metadata, or fall back to an empty document on first run.

        Keeps the current selection when it still exists in the reloaded
        document, so a background reload (after a capture, say) does not move
        the user to a different version.
        """
        wanted_profile, wanted_state = self._active_profile_id, self._active_state_id
        try:
            self.config_data = self.store.load()
            self._config_hash = self.store.current_hash()
            self.config = Config(self.config_data, self.config_path.parent)
            if not self.config.profile(wanted_profile):
                wanted_profile = self.config.initial_profile_id()
            self._active_profile_id = wanted_profile
            if not self.config.is_empty:
                states = self.config.states(self._active_profile_id)
                if not any(s.id == wanted_state for s in states):
                    captured = [s for s in states
                                if self.config.is_captured(self._active_profile_id, s.id)]
                    wanted_state = (captured or states)[0].id if (captured or states) else ""
                self._active_state_id = wanted_state
            else:
                self._active_state_id = ""
            self._sync_selectors()
            self.home.show(self.config, self._last_status)
            self._update_apply_button()
            self._update_undo()
        except Exception as exc:
            if not self.config_path.exists():
                # First run: no config yet.  Stay in memory and let the wizard write.
                self.config_data = dict(paths.EMPTY_CONFIG)
                self._config_hash = None
                self.config = Config(self.config_data, self.config_path.parent)
                self._active_profile_id = ""
                self._active_state_id = ""
                self._sync_selectors()
                self.home.show(self.config, None)
                self.advanced.unconfigured("还没有设置。点“设置 / 添加版本”开始。")
                return
            self.config_data = {}
            self.config = Config({}, self.config_path.parent)
            self._sync_selectors()
            self.home.show(self.config, None)
            self.advanced.unconfigured("配置无法使用，请重新设置。")
            if show_error:
                dialogs.show_error(self, {"error": {
                    "code": "config-schema",
                    "message": str(exc)}})

    def _sync_selectors(self) -> None:
        self.advanced.fill_profiles(self.config, self._active_profile_id)
        self.advanced.fill_states(self.config, self.active_profile_id(), self._active_state_id)

    def _save_metadata(self, data: dict[str, Any] | None = None) -> bool:
        """Write metadata, refusing to clobber an edit made elsewhere."""
        payload = data if data is not None else self.config_data
        try:
            self.store.save(payload, expected_hash=self._config_hash)
        except ProfileStoreError as exc:
            dialogs.show_error(self, {"error": {"code": "config-schema", "message": str(exc)}},
                               headline="设置无法保存")
            return False
        except Exception as exc:
            dialogs.show_error(self, {"error": {"code": "operation-failed",
                                                "message": str(exc)}},
                               headline="设置无法保存")
            return False
        self.config_data = payload
        self._config_hash = self.store.current_hash()
        self.config = Config(self.config_data, self.config_path.parent)
        self._sync_selectors()
        self.home.show(self.config, self._last_status)
        self.invalidate_plan()
        return True

    # ---------- selection ----------
    def active_profile_id(self) -> str:
        """The profile the advanced screen is pointed at."""
        if self.advanced.profile_var.get():
            item = next((p for p in self.config.profiles
                         if p.label == self.advanced.profile_var.get()), None)
            if item:
                self._active_profile_id = item.id
        if not self._active_profile_id:
            self._active_profile_id = self.config.initial_profile_id()
        return self._active_profile_id

    def selected_profile_id(self) -> str | None:
        return self.active_profile_id() or None

    def active_state_id(self) -> str:
        label = self.advanced.state_var.get()
        states = self.config.states(self.active_profile_id())
        item = next((s for s in states if s.label == label), None)
        if item:
            self._active_state_id = item.id
        elif not any(s.id == self._active_state_id for s in states):
            self._active_state_id = states[0].id if states else ""
        return self._active_state_id

    def selected_state_id(self) -> str | None:
        return self.active_state_id() or None

    def selected_profile(self) -> dict[str, Any] | None:
        item = self.config.profile(self.active_profile_id())
        return item.raw if item else None

    def selected_state(self) -> dict[str, Any] | None:
        item = self.config.state(self.active_profile_id(), self.active_state_id())
        return item.raw if item else None

    def on_profile_changed(self, _event: Any = None) -> None:
        self._active_state_id = ""
        profile_id = self.active_profile_id()
        states = self.config.states(profile_id)
        self._active_state_id = states[0].id if states else ""
        self.advanced.fill_states(self.config, profile_id, self._active_state_id)
        self.invalidate_plan()
        self.advanced.status("尚未读取")
        self.refresh_status()

    def on_state_changed(self, _event: Any = None) -> None:
        self.invalidate_plan()
        self._update_apply_button()
        if self._last_status is not None:
            self.advanced.status("已切换选中的版本，需重新比较", "Warn.TLabel")

    def on_status_tree_select(self, _event: Any = None) -> None:
        """Picking a row in the overview jumps the version selector to it."""
        selection = self.advanced.status_tree.selection()
        if not selection:
            return
        values = self.advanced.status_tree.item(selection[0], "values")
        if not values:
            return
        name = str(values[0])
        state = next((s for s in self.config.states(self.active_profile_id())
                      if s.display_name == name or s.id == name), None)
        if state:
            self._active_state_id = state.id
            self.advanced.state_var.set(state.label)
            self._update_apply_button()

    def _update_apply_button(self) -> None:
        profile_id = self.active_profile_id()
        state_id = self.active_state_id()
        enabled = self.config.is_captured(profile_id, state_id)
        if enabled and self._last_status:
            row = next((x for x in (self._last_status.get("states") or [])
                        if str(x.get("id")) == state_id), None)
            enabled = not bool(row and row.get("error"))
        self.advanced.update_apply_button(enabled, self._busy)

    # ---------- backend calls ----------
    def _args(self, action: str, *extra: str) -> list[str]:
        profile_id = self.selected_profile_id()
        args = ["-Action", action]
        if profile_id:
            args += ["-ProfileId", profile_id]
        args.extend(str(x) for x in extra if x is not None)
        return args

    def call(self, args: list[str], intent: str = INTENT_NONE) -> bool:
        if self._busy or getattr(self.runner, "running", False):
            dialogs.show_message(self, "已有操作在进行", "请等当前操作结束后再试。")
            return False
        self._busy = True
        self._set_busy(True)
        self._append_log(f"\n> {intent or args[1] if len(args) > 1 else ''}: {' '.join(args)}\n")
        try:
            self.runner.run(args, lambda result: self.events.put(result))
            return True
        except Exception as exc:
            self._set_busy(False)
            self._busy = False
            dialogs.show_error(self, {"error": {"code": "backend-start-failed",
                                                "message": str(exc)}})
            return False

    def refresh_status(self) -> None:
        profile_id = self.selected_profile_id()
        if not profile_id:
            # Nothing configured: the home screen already explains what to do.
            self.home.show(self.config, None)
            return
        self.call(self._args("Status"))

    def scan(self) -> None:
        if self.selected_profile_id():
            self.call(self._args("Scan"))

    def scan_external(self) -> None:
        root = filedialog.askdirectory(title="选择要查看的文件夹", parent=self)
        if root:
            self.call(self._args("Scan", "-SourceRoot", root))

    # ---------- switching ----------
    def switch_to(self, state_id: str, name: str) -> None:
        """Home-screen entry point: confirm, then Diff → Apply."""
        if self._busy or getattr(self.runner, "running", False):
            dialogs.show_message(self, "已有操作在进行", "请等当前操作结束。")
            return
        if not dialogs.confirm_switch(self, name):
            return
        self._set_active_state(state_id)
        self._switch_confirmed = True
        self._intent = INTENT_SWITCH
        self._request_diff()

    def _set_active_state(self, state_id: str) -> None:
        state = self.config.state(self.active_profile_id(), state_id)
        if state:
            self._active_state_id = state.id
            self.advanced.state_var.set(state.label)
            self._update_apply_button()

    def preview_selected_state(self) -> None:
        if self.selected_state_id():
            self._intent = INTENT_PREVIEW
            self._request_diff()

    def apply_selected_state(self) -> None:
        if self.selected_state_id():
            self._intent = INTENT_SWITCH
            self._request_diff()

    def diff(self) -> None:
        """Advanced screen: preview only, honouring the 'preview only' checkbox."""
        self._intent = INTENT_PREVIEW
        self._request_diff()

    def apply(self) -> None:
        """Advanced screen: apply, still guarded by a fresh Diff + digest."""
        self._intent = INTENT_SWITCH
        self._request_diff()

    def _request_diff(self) -> None:
        state = self.selected_state_id()
        if not state:
            dialogs.show_message(self, "还没有选择版本", "请先选择一个版本。")
            return
        extras = ["-StateId", state]
        if self.advanced.unknown_var.get():
            extras.append("-AllowUnknown")
        if self.advanced.delete_var.get():
            extras.append("-AllowManagedDelete")
        self.call(self._args("Diff", *extras))

    def _apply_plan(self, confirmed: bool) -> None:
        """Apply the plan from the last Diff.

        ``confirmed`` is True when the caller already asked the user (the home
        screen does, before it even runs the diff).
        """
        plan = self._last_plan
        if not plan:
            return
        blockers = plan.get("blockers") or []
        if blockers:
            dialogs.show_error(self, {"error": {
                "code": plan_code_for(blockers),
                "message": "Apply blocked: " + ", ".join(map(str, blockers))}})
            return
        digest = plan.get("planDigest")
        if not digest:
            dialogs.show_error(self, {"error": {
                "code": "digest-required",
                "message": "计划没有 digest，请重新生成预览。"}})
            return
        if not confirmed and self.advanced.dry_var.get():
            # Advanced screen with "preview only" still ticked: stop here.
            self.advanced.plan_label.configure(
                text=self.advanced.plan_label.cget("text")
                     + "\n（当前是“只看不改”，没有修改任何文件。）")
            self.advanced.status("仅预览，未修改文件", "Warn.TLabel")
            return

        changed = sum(int((plan.get("counts") or {}).get(kind, 0))
                      for kind in ("add", "modify", "remove"))
        if not confirmed and not dialogs.confirm(
                self, "确认切换",
                f"将修改 {changed} 个会一起切换的文件；其它文件不受影响。"):
            return
        state = self.selected_state_id()
        extras = ["-StateId", state or "", "-ExpectedPlanDigest", str(digest)]
        if self.advanced.unknown_var.get():
            extras.append("-AllowUnknown")
        if self.advanced.delete_var.get():
            extras.append("-AllowManagedDelete")
        self.call(self._args("Apply", *extras))

    # ---------- capture ----------
    def capture_current(self) -> None:
        state = self.selected_state_id()
        if state:
            self._begin_capture(state, None)

    def capture_external(self) -> None:
        state = self.selected_state_id()
        if not state:
            return
        source = filedialog.askdirectory(title="选择内容来源文件夹", parent=self)
        if source:
            self._begin_capture(state, source)

    def capture_selected_state(self) -> None:
        self.capture_current()

    def _begin_capture(self, state: str, source: str | None) -> None:
        """Scan first so the confirmation can name how many files are missing."""
        self._pending_capture = (state, source)
        extras = ["-StateId", state]
        if source:
            extras += ["-SourceRoot", source]
        self._intent = INTENT_CAPTURE_SCAN
        self.call(self._args("Scan", *extras))

    # ---------- transactions ----------
    def undo_last_switch(self) -> None:
        """Home-screen undo: roll back the most recent completed switch."""
        journal = self._latest_rollback_candidate()
        if journal is None:
            dialogs.show_message(self, "没有可以撤销的切换",
                                 "还没有成功的切换记录。")
            return
        tx_id = journal.parent.name
        if not dialogs.confirm(self, "撤销上一次切换",
                               f"将把文件恢复成切换之前的内容（记录 {tx_id}）。",
                               "只有那次切换改过的文件会被还原。"):
            return
        self.call(self._args("Rollback", "-TransactionId", tx_id))

    def _latest_rollback_candidate(self) -> Path | None:
        if not self.state_root.exists():
            return None
        candidates = []
        for journal in self.state_root.glob("**/journal.json"):
            try:
                data = json.loads(journal.read_text(encoding="utf-8-sig"))
            except Exception:
                continue
            if str(data.get("status")) == "completed":
                candidates.append((journal.stat().st_mtime, journal))
        if not candidates:
            return None
        return max(candidates, key=lambda pair: pair[0])[1]

    def refresh_transactions(self) -> None:
        tree = self.advanced.tx_tree
        tree.delete(*tree.get_children())
        if not self.state_root.exists():
            return
        for journal in sorted(self.state_root.glob("**/journal.json"), reverse=True):
            try:
                data = json.loads(journal.read_text(encoding="utf-8-sig"))
                tree.insert("", "end", iid=str(journal.parent),
                            values=(journal.parent.name, status_text(data.get("status")),
                                    data.get("profileId", ""), data.get("stateId", ""),
                                    str(journal.parent)))
            except Exception:
                tree.insert("", "end", iid=str(journal.parent),
                            values=(journal.parent.name, "记录损坏", "", "",
                                    str(journal.parent)))

    def show_transaction(self, _event: Any = None) -> None:
        selection = self.advanced.tx_tree.selection()
        if not selection:
            return
        path = Path(selection[0]) / "journal.json"
        try:
            value = path.read_text(encoding="utf-8-sig")
        except Exception as exc:
            value = str(exc)
        detail = self.advanced.tx_detail
        detail.configure(state="normal")
        detail.delete("1.0", "end")
        detail.insert("1.0", value)
        detail.configure(state="disabled")

    def rollback_selected(self) -> None:
        selection = self.advanced.tx_tree.selection()
        if not selection:
            dialogs.show_message(self, "还没有选择记录", "请先在列表里选一条记录。")
            return
        path = Path(selection[0]) / "journal.json"
        try:
            journal = json.loads(path.read_text(encoding="utf-8-sig"))
        except Exception as exc:
            dialogs.show_error(self, {"error": {"code": "transaction-not-found",
                                                "message": str(exc)}})
            return
        tx_id = path.parent.name
        if str(journal.get("status")) == "recovery-required":
            dialogs.show_error(self, {"error": {
                "code": "transaction-incomplete",
                "message": "该事务处于 recovery-required 状态。"}})
            return
        if not dialogs.confirm(self, "撤销这次切换",
                               f"将把文件恢复成记录 {tx_id} 之前的内容。",
                               "只有那次切换改过的文件会被还原。"):
            return
        self.call(self._args("Rollback", "-TransactionId", tx_id))

    def open_transaction_dir(self) -> None:
        selection = self.advanced.tx_tree.selection()
        self._open_path(Path(selection[0]) if selection else self.state_root)

    @staticmethod
    def _open_path(path: Path) -> None:
        try:
            if hasattr(os, "startfile"):
                os.startfile(str(path))
            else:
                subprocess.Popen(["xdg-open", str(path)])
        except Exception:
            pass

    # ---------- managed files ----------
    def manage_selected(self) -> None:
        self.add_managed_paths(self._selected_scan_paths())

    def unmanage_selected(self) -> None:
        paths_to_drop = {p.casefold() for p in self._selected_scan_paths()}
        profile = self.selected_profile()
        if not profile or not paths_to_drop:
            return
        profile["managed"] = [m for m in profile.get("managed", [])
                              if str(m.get("path", "")).casefold() not in paths_to_drop]
        self._invalidate_states(profile)
        self._save_metadata()

    def _selected_scan_paths(self) -> list[str]:
        tree = self.advanced.scan_tree
        return [str(tree.item(i, "values")[0]) for i in tree.selection()]

    def add_managed_paths(self, candidates: list[str]) -> None:
        profile = self.selected_profile()
        if not profile:
            return
        existing = self.config.managed_paths(self.active_profile_id())
        target_root = self.config.target_root(self.active_profile_id())
        additions = []
        try:
            for raw in candidates:
                clean = safe_relative(raw)
                if clean.casefold() in existing:
                    continue
                if target_root is not None and (target_root / clean).is_dir():
                    raise ValueError("只能管理单个文件，不能管理整个文件夹。")
                additions.append({"path": clean})
                existing.add(clean.casefold())
        except ValueError as exc:
            dialogs.show_error(self, {"error": {"code": "config-managed-directory",
                                                "message": str(exc)}},
                               headline="这个文件不能被管理")
            return
        if additions:
            profile.setdefault("managed", []).extend(additions)
            self._invalidate_states(profile)
            self._save_metadata()

    def manage_directory_prefix(self) -> None:
        prefix = filedialog.askdirectory(title="选择目标文件夹里的子文件夹", parent=self)
        target_root = self.config.target_root(self.active_profile_id())
        if not prefix or target_root is None:
            return
        try:
            relative = Path(prefix).resolve().relative_to(target_root.resolve())
        except ValueError:
            dialogs.show_error(self, {"error": {"code": "path-escape",
                                                "message": "只能选择目标文件夹内的文件夹。"}},
                               headline="这个文件夹不在目标文件夹里")
            return
        prefix_text = str(relative).replace("/", "\\").strip("\\").casefold()
        tree = self.advanced.scan_tree
        paths = [str(tree.item(i, "values")[0]) for i in tree.get_children()
                 if str(tree.item(i, "values")[0]).replace("/", "\\").casefold()
                 .startswith(prefix_text + "\\")]
        self.add_managed_paths(paths)

    def choose_single_file(self) -> None:
        target_root = self.config.target_root(self.active_profile_id())
        chosen = filedialog.askopenfilename(
            title="选择要一起切换的文件",
            initialdir=str(target_root) if target_root else None, parent=self)
        if not chosen or target_root is None:
            return
        try:
            relative = Path(chosen).resolve().relative_to(target_root.resolve())
        except ValueError:
            dialogs.show_error(self, {"error": {"code": "path-escape",
                                                "message": "只能选择目标文件夹内的文件。"}},
                               headline="这个文件不在目标文件夹里")
            return
        self.add_managed_paths([str(relative)])

    def _invalidate_states(self, profile: dict[str, Any]) -> None:
        """Mark saved versions stale after the managed list changes.

        The old snapshots stay on disk for recovery, but the backend refuses
        them because their manifest no longer matches the managed list.
        """
        for state in profile.get("states", []):
            state["needsCapture"] = True
        self.advanced.capture_info.configure(
            text="文件清单变了：之前保存的内容需要重新保存一次才能使用。")

    # ---------- metadata editing ----------
    def start_wizard(self) -> None:
        Wizard(self, on_finish=self._wizard_finish)

    def _wizard_finish(self, profile: dict[str, Any], state: dict[str, Any],
                       source: str) -> None:
        profile["id"] = new_id()
        state["id"] = new_id()
        profile["states"] = [state]
        if source:
            state["sourceRoot"] = str(Path(source).expanduser().resolve())

        # Adding a version to an existing target reuses that profile's settings.
        existing = next((p for p in self.config_data.get("profiles", [])
                         if str(p.get("targetRoot", "")).casefold()
                         == profile["targetRoot"].casefold()), None)
        if existing is not None:
            merged = dict(existing)
            known = {str(m.get("path", "")).casefold() for m in merged.get("managed", [])}
            for entry in profile["managed"]:
                if entry["path"].casefold() not in known:
                    merged.setdefault("managed", []).append(entry)
            # A changed file list invalidates the versions already saved.
            if len(merged.get("managed", [])) != len(existing.get("managed", [])):
                for saved in merged.get("states", []):
                    saved["needsCapture"] = True
            merged.setdefault("states", []).append(state)
            profiles = [merged if p.get("id") == existing.get("id") else p
                        for p in self.config_data.get("profiles", [])]
            payload = {**self.config_data, "profiles": profiles,
                       "defaultProfileId": merged["id"]}
        else:
            payload = {
                "schemaVersion": 2,
                "defaultProfileId": profile["id"],
                "profiles": list(self.config_data.get("profiles", [])) + [profile],
            }

        if not self._save_metadata(payload):
            return
        self._active_profile_id = profile["id"] if existing is None else str(existing.get("id"))
        self._active_state_id = state["id"]
        self._sync_selectors()
        self.show_home()
        self._capture_new_version(self._active_state_id, source)

    def _capture_new_version(self, state_id: str, source: str) -> None:
        """Save the new version's content right away, so its button works."""
        self._begin_capture(state_id, source or None)

    def add_profile(self) -> None:
        name = simpledialog.askstring("新建目标设置", "给这个设置起个名字：", parent=self)
        if not name:
            return
        target = filedialog.askdirectory(title="选择要管理的文件夹", parent=self)
        if not target:
            return
        profile = {"id": new_id(), "name": name.strip(),
                   "targetRoot": str(Path(target).resolve()),
                   "managed": [], "states": [], "guards": [], "cacheDirectories": []}
        profile["states"].append({"id": new_id(), "name": "默认版本", "snapshotRoot": None})
        payload = {**self.config_data,
                   "profiles": list(self.config_data.get("profiles", [])) + [profile],
                   "defaultProfileId": profile["id"]}
        if self._save_metadata(payload):
            self._active_profile_id = profile["id"]
            self._active_state_id = profile["states"][0]["id"]
            self._sync_selectors()

    def rename_profile(self) -> None:
        profile = self.selected_profile()
        if not profile:
            return
        name = simpledialog.askstring("重命名设置", "新名字：",
                                      initialvalue=profile.get("name", ""), parent=self)
        if name and name.strip():
            profile["name"] = name.strip()
            self._save_metadata()

    def delete_profile(self) -> None:
        profile = self.selected_profile()
        if not profile:
            return
        if len(self.config_data.get("profiles", [])) <= 1:
            dialogs.show_message(self, "不能删除", "至少要保留一个目标设置。")
            return
        if not dialogs.confirm(self, "删除这个目标设置",
                               "只会删除这里的设置记录，不会删除你的文件夹或已保存的内容。",
                               "其它设置不受影响。"):
            return
        remaining = [p for p in self.config_data["profiles"]
                     if p.get("id") != profile.get("id")]
        payload = {**self.config_data, "profiles": remaining,
                   "defaultProfileId": remaining[0].get("id")}
        if self._save_metadata(payload):
            self._active_profile_id = remaining[0].get("id", "")
            states = self.config.states(self._active_profile_id)
            self._active_state_id = states[0].id if states else ""
            self._sync_selectors()

    def _ask_name(self, title: str, initial: str = "") -> str | None:
        value = simpledialog.askstring(title, "版本名字：", initialvalue=initial, parent=self)
        return value.strip() if value and value.strip() else None

    def add_state(self) -> None:
        profile = self.selected_profile()
        name = self._ask_name("新建版本") if profile else None
        if profile and name:
            profile.setdefault("states", []).append(
                {"id": new_id(), "name": name, "snapshotRoot": None})
            if self._save_metadata():
                self._active_state_id = profile["states"][-1]["id"]

    def rename_state(self) -> None:
        state = self.selected_state()
        name = self._ask_name("重命名版本", str(state.get("name", ""))) if state else None
        if state and name:
            state["name"] = name
            self._save_metadata()

    def copy_state(self) -> None:
        state = self.selected_state()
        name = self._ask_name("复制版本", str(state.get("name", "")) + " 副本") if state else None
        profile = self.selected_profile()
        if profile and state and name:
            profile.setdefault("states", []).append(
                {"id": new_id(), "name": name, "snapshotRoot": None})
            self._save_metadata()

    def delete_state(self) -> None:
        profile = self.selected_profile()
        state = self.selected_state()
        if not profile or not state:
            return
        if len(profile.get("states", [])) <= 1:
            dialogs.show_message(self, "不能删除", "至少要保留一个版本。")
            return
        if dialogs.confirm(self, "删除这个版本",
                           "只会删除这里的版本设置，不会删除已保存的内容或你的文件夹。",
                           "其它版本不受影响。"):
            profile["states"] = [s for s in profile.get("states", [])
                                 if s.get("id") != state.get("id")]
            if self._save_metadata():
                states = self.config.states(self.active_profile_id())
                self._active_state_id = states[0].id if states else ""

    # ---------- result handling ----------
    def poll(self) -> None:
        if self._closing:
            return
        try:
            while True:
                self.handle(self.events.get_nowait())
        except queue.Empty:
            pass
        self._poll_id = self.after(80, self.poll)

    def handle(self, result: dict[str, Any]) -> None:
        """Consume one backend result.  All failures funnel through dialogs."""
        self._set_busy(False)
        self._busy = False

        raw = str(result.get("stdout", "") or "")
        stderr = str(result.get("stderr", "") or "")
        self._append_log(raw + (f"\n[stderr]\n{stderr}" if stderr else "")
                         + f"\n[exit={result.get('returncode')}]\n")

        try:
            payload = backend.parse_result(result)
        except backend.BackendError as exc:
            self._fail(exc.as_payload())
            return

        action = str(payload.get("action", "")).lower()
        intent, self._intent = self._intent, INTENT_NONE

        if not payload.get("ok"):
            self._fail(payload)
            return

        data = payload.get("data") or {}
        if action == "status":
            self.render_status(data)
        elif action == "scan":
            self._render_scan(data)
            if intent == INTENT_CAPTURE_SCAN and self._pending_capture:
                self._confirm_capture(data)
        elif action == "diff":
            self.render_diff(data)
            self._last_plan = data
            if intent == INTENT_SWITCH:
                self._apply_plan(confirmed=self._switch_confirmed)
            else:
                self.advanced.status("仅预览，未修改文件", "Warn.TLabel")
            self._switch_confirmed = False
        elif action == "capture":
            self.advanced.capture_info.configure(
                text=f"已保存 {data.get('fileCount', 0)} 个文件"
                     f"（缺失 {data.get('missingCount', 0)} 个）。现在可以切换到它了。")
            self._pending_capture = None
            # Capture writes the snapshot path into the config file, so the
            # in-memory copy is stale until it is read back.  Without this the
            # home screen keeps showing "no saved content" after saving.
            self.load_config(show_error=False)
            self.refresh_status()
            self.show_home()
        elif action == "apply":
            self._last_plan = None
            self.advanced.status("已切换，正在刷新…", "Warn.TLabel")
            self.refresh_status()
            self.refresh_transactions()
        elif action == "rollback":
            self.advanced.status("已撤销，正在刷新…", "Warn.TLabel")
            self.refresh_status()
            self.refresh_transactions()
        else:
            self.refresh_status()

    def _fail(self, payload: dict[str, Any]) -> None:
        dialogs.show_error(self, payload)
        self.advanced.status("操作失败", "Danger.TLabel")
        self._update_apply_button()

    def _confirm_capture(self, data: dict[str, Any]) -> None:
        state_id, source = self._pending_capture or ("", None)
        missing = data.get("missingCount")
        if missing is None:
            listed = {str(x.get("path", "")).casefold()
                      for x in (data.get("files") or [])}
            missing = sum(1 for p in self.config.managed_paths(self.active_profile_id())
                          if p not in listed)
        if not dialogs.confirm(self, "保存这个版本的内容",
                               f"将保存 {data.get('fileCount', 0)} 个文件"
                               f"（其中 {missing} 个文件当前不存在）。",
                               "只会把内容存进工具的数据目录，不会改动你的文件。"):
            self._pending_capture = None
            return
        extras = ["-StateId", state_id]
        if source:
            extras += ["-SourceRoot", source]
        self._pending_capture = None
        self.call(self._args("Capture", *extras))

    # ---------- rendering ----------
    def render_status(self, data: dict[str, Any]) -> None:
        self._last_status = data
        current = str(data.get("currentMode", "unknown"))
        row = next((s for s in (data.get("states") or []) if str(s.get("id")) == current), None)
        display = row.get("name") if row else None
        if display:
            text, style = f"当前版本：{display}", "Good.TLabel"
        elif current == "ambiguous":
            text, style = "当前版本：内容与多个版本都相符", "Warn.TLabel"
        else:
            text, style = "当前版本：无法确定", "Warn.TLabel"
        self.advanced.state_label.configure(text=text, style=style)
        self.advanced.status_mode.configure(text=text, style=style)
        self.advanced.status_info.configure(
            text=f"文件夹：{data.get('targetRoot', '')}；"
                 f"会一起切换的文件 {data.get('managedCount', 0)} 个；"
                 f"其它文件 {data.get('unmanagedCount', 0)} 个")

        tree = self.advanced.status_tree
        tree.delete(*tree.get_children())
        for state in data.get("states") or []:
            note = state.get("error") or ""
            if note:
                note = f"{note}（需要重新保存一次内容）"
            tree.insert("", "end", values=(state.get("name") or state.get("id", ""),
                                           "是" if state.get("exact") else "否",
                                           state.get("fileCount", "-"), note))
        cache = data.get("cacheDirectories") or []
        self.advanced.cache_label.configure(
            text="缓存目录：" + ("、".join(map(str, cache)) if cache else "无"))
        blocked = data.get("blockedProcesses") or []
        self.advanced.process_label.configure(
            text="正在占用文件的程序：" + ("、".join(map(str, blocked)) if blocked else "无"))
        self.home.show(self.config, data)
        self._update_apply_button()
        self._update_undo()

    def render_diff(self, data: dict[str, Any]) -> None:
        tree = self.advanced.diff_tree
        tree.delete(*tree.get_children())
        for change in data.get("changes") or []:
            tree.insert("", "end", values=(change.get("path", ""),
                                           kind_text(change.get("kind")),
                                           change.get("currentHash") or "—",
                                           change.get("expectedHash") or "—"))
        counts = data.get("counts") or {}
        blockers = data.get("blockers") or []
        summary = (f"结果：新增 {counts.get('add', 0)}，替换 {counts.get('modify', 0)}，"
                   f"删除 {counts.get('remove', 0)}，不变 {counts.get('unchanged', 0)}")
        if blockers:
            summary += "\n有内容被别的程序改过：" + "、".join(
                str(b).split(":", 1)[-1] for b in blockers)
        summary += f"\n（计划指纹 {data.get('planDigest', '')}）"
        self.advanced.plan_label.configure(text=summary)
        self.advanced.select_tab(self.advanced.diff_tab)

    def _render_scan(self, data: dict[str, Any]) -> None:
        tree = self.advanced.scan_tree
        tree.delete(*tree.get_children())
        managed = self.config.managed_paths(self.active_profile_id())
        for row in data.get("files") or []:
            path = str(row.get("path", ""))
            tree.insert("", "end", iid=path,
                        values=(path, "是" if path.casefold() in managed else "否",
                                row.get("size", ""), row.get("sha256", "")))
        self.advanced.scan_info.configure(
            text=f"共 {len(data.get('files') or [])} 个文件；"
                 f"会一起切换 {data.get('managedCount', len(managed))} 个；"
                 f"不会被动 {data.get('unmanagedCount', '')} 个。")

    # ---------- busy / logging ----------
    def _set_busy(self, busy: bool) -> None:
        self._busy = busy
        self.home.set_busy(busy)
        self.advanced.set_busy(busy)

    def _update_undo(self) -> None:
        self.home.set_undo_enabled(self._latest_rollback_candidate() is not None)

    def _append_log(self, text: str) -> None:
        try:
            self.log_root.mkdir(parents=True, exist_ok=True)
            stamp = time.strftime("%Y%m%d")
            with (self.log_root / f"folder-switcher-{stamp}.log").open(
                    "a", encoding="utf-8") as handle:
                handle.write(text)
        except OSError:
            pass
        try:
            detail = self.advanced.tx_detail
            detail.configure(state="normal")
            detail.insert("end", text)
            detail.see("end")
            detail.configure(state="disabled")
        except (tk.TclError, AttributeError):
            pass

    def invalidate_plan(self) -> None:
        self._last_plan = None
        try:
            self.advanced.plan_label.configure(text="还没有生成比较结果。")
        except tk.TclError:
            pass

    def _report_callback_exception(self, exc_type: type[BaseException],
                                   exc: BaseException, tb: Any) -> None:
        self._append_log("".join(traceback.format_exception(exc_type, exc, tb)))
        try:
            dialogs.show_error(self, {"error": {"code": "operation-failed",
                                                "message": str(exc)}})
        except tk.TclError:
            pass

    def close(self) -> None:
        if self._busy or getattr(self.runner, "running", False):
            dialogs.show_message(self, "操作进行中",
                                 "正在写入文件，请等这次操作结束后再关闭窗口。")
            return
        self._closing = True
        if self._poll_id:
            try:
                self.after_cancel(self._poll_id)
            except tk.TclError:
                pass
            self._poll_id = None
        self.destroy()


def kind_text(kind: Any) -> str:
    """Backend change kinds, in words."""
    return {"add": "新增", "modify": "替换", "remove": "删除",
            "unchanged": "不变"}.get(str(kind), str(kind))


def status_text(status: Any) -> str:
    """Transaction statuses, in words."""
    return {"completed": "已完成", "failed-rolled-back": "失败已回滚",
            "recovery-required": "需要恢复", "started": "进行中",
            "applying": "进行中", "rolled-back": "已撤销"}.get(str(status), str(status))


def plan_code_for(blockers: list[Any]) -> str:
    """Which explanation fits a set of plan blockers."""
    text = " ".join(str(b) for b in blockers)
    if "target-conflict:" in text:
        return "plan-target-conflict"
    if "delete-not-authorized:" in text:
        return "plan-delete-unauthorized"
    if "delete-hash-unrecognized:" in text:
        return "plan-delete-unrecognized"
    return "plan-blocked"


def safe_relative(path: str) -> str:
    """Reject anything that is not a clean relative file path."""
    candidate = Path(path)
    if candidate.is_absolute() or ".." in candidate.parts \
            or "." in candidate.parts or not path.strip():
        raise ValueError("受管路径必须是目标文件夹内的安全相对路径。")
    return str(candidate).replace("/", "\\")
