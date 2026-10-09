"""Folder switcher GUI.

The GUI only edits profile metadata through ProfileStore.  All target-folder,
snapshot, and transaction operations are delegated to folder-switcher.ps1.
"""
from __future__ import annotations

import hashlib
import json
import os
import queue
import subprocess
import sys
import threading
import time
import traceback
import tkinter as tk
from pathlib import Path
from tkinter import filedialog, messagebox, simpledialog, ttk
from typing import Any, Callable

# Resources stay beside the source files, or beside the frozen executable.  All
# mutable metadata, snapshots, journals and logs live in the per-user data root.
if getattr(sys, "frozen", False):
    # onefile extracts bundled payload into a one-shot temp dir (_MEIPASS);
    # onedir keeps it next to the executable. Prefer _MEIPASS when present.
    RESOURCE_ROOT = Path(getattr(sys, "_MEIPASS", Path(sys.executable).resolve().parent))
else:
    RESOURCE_ROOT = Path(__file__).resolve().parent

_DATA_ROOT_OVERRIDE = os.environ.get("ANYTESTTOOLS_DATA_ROOT")
if _DATA_ROOT_OVERRIDE and _DATA_ROOT_OVERRIDE.strip():
    DATA_ROOT = Path(_DATA_ROOT_OVERRIDE).expanduser().resolve(strict=False)
else:
    DATA_ROOT = (Path(os.environ.get("LOCALAPPDATA", str(Path.home())))
                 / "AnyTestTools" / "FolderSwitcher").resolve(strict=False)

ROOT = RESOURCE_ROOT  # Backward-compatible name for integrations.
SCRIPT = RESOURCE_ROOT / "folder-switcher.ps1"
CONFIG = DATA_ROOT / "folder-config.json"
STATE_ROOT = DATA_ROOT / "folder-data"
WORKSPACE_ROOT = STATE_ROOT
LOG_ROOT = DATA_ROOT / "logs"
EMPTY_CONFIG: dict[str, Any] = {"schemaVersion": 2, "defaultProfileId": "", "profiles": []}

try:  # profile_store is supplied by the metadata layer.
    from profile_store import ProfileStore  # type: ignore
except ImportError:  # Keeps the GUI importable while that layer is being deployed.
    ProfileStore = None  # type: ignore


def _hash_file(path: Path) -> str | None:
    try:
        return hashlib.sha256(path.read_bytes()).hexdigest()
    except OSError:
        return None


def _new_id() -> str:
    if ProfileStore is not None and hasattr(ProfileStore, "new_id"):
        return str(ProfileStore.new_id())
    import uuid
    return uuid.uuid4().hex


class _FallbackStore:
    """Import-time fallback only; normal installations use ProfileStore."""
    def __init__(self, path: Path):
        self.path = path

    def load(self) -> dict[str, Any]:
        return json.loads(self.path.read_text(encoding="utf-8-sig"))

    def save(self, data: dict[str, Any], expected_hash: str | None = None) -> None:
        if expected_hash is not None and _hash_file(self.path) != expected_hash:
            raise RuntimeError("配置已被其他窗口修改，请重新加载")
        tmp = self.path.with_name(self.path.name + f".tmp.{os.getpid()}")
        tmp.write_text(json.dumps(data, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
        os.replace(tmp, self.path)


class PowerShellRunner:
    """Runs one backend operation at a time and reports only from its worker."""
    QUERY_ACTIONS = {"status", "scan", "diff"}

    def __init__(self, callback: Callable[[dict[str, Any]], None] | None = None,
                 script_path: str | Path = SCRIPT, config_path: str | Path = CONFIG,
                 state_root: str | Path = STATE_ROOT, query_timeout: float = 45.0):
        self.callback = callback or (lambda result: None)
        self.script_path = Path(script_path)
        self.config_path = Path(config_path)
        self.state_root = Path(state_root)
        self.query_timeout = query_timeout
        self.running = False
        self._lock = threading.Lock()
        self._process: subprocess.Popen[str] | None = None

    def run(self, args: list[str] | tuple[str, ...], callback: Callable[[dict[str, Any]], None] | None = None) -> None:
        with self._lock:
            if self.running:
                raise RuntimeError("已有目录操作正在执行")
            self.running = True
        threading.Thread(target=self._worker, args=(list(args), callback), daemon=True).start()

    def _worker(self, args: list[str], callback: Callable[[dict[str, Any]], None] | None) -> None:
        started = time.time()
        command = ["powershell.exe", "-NoLogo", "-NoProfile", "-NonInteractive",
                   "-ExecutionPolicy", "Bypass", "-File", str(self.script_path),
                   "-Config", str(self.config_path), "-StateRoot", str(self.state_root)] + args
        action = ""
        if "-Action" in args:
            action = str(args[args.index("-Action") + 1]).lower()
        try:
            completed = subprocess.run(command, capture_output=True, text=True,
                                       encoding="utf-8", errors="replace",
                                       timeout=self.query_timeout if action in self.QUERY_ACTIONS else None)
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
            self._process = None
        (callback or self.callback)(result)


def _as_text(value: Any) -> str:
    if value is None:
        return ""
    if isinstance(value, bytes):
        return value.decode("utf-8", errors="replace")
    return str(value)


# Backward-compatible names used by small integrations.
Runner = PowerShellRunner


class FolderApp(tk.Tk):
    def __init__(self, config_path: str | Path = CONFIG, state_root: str | Path | None = None,
                 script_path: str | Path = SCRIPT, runner: Any = None,
                 log_root: str | Path | None = None, auto_refresh: bool = True):
        super().__init__()
        self.config_path = Path(config_path)
        self.state_root = Path(state_root) if state_root is not None else STATE_ROOT
        self.script_path = Path(script_path)
        self.log_root = Path(log_root) if log_root is not None else LOG_ROOT
        self.title("AnyTestTools · 文件夹状态切换")
        self.geometry("1180x790")
        self.minsize(960, 650)
        self.events: queue.Queue[dict[str, Any]] = queue.Queue()
        self.runner = runner or PowerShellRunner(lambda result: self.events.put(result), self.script_path, self.config_path, self.state_root)
        self._fallback_store = _FallbackStore(self.config_path)
        self.store = ProfileStore(self.config_path) if ProfileStore is not None else self._fallback_store
        self._using_fallback_store = ProfileStore is None
        self.config_data: dict[str, Any] = {}
        self._config_hash: str | None = None
        self._closing = False
        self._poll_id: str | None = None
        self._busy = False
        self._pending_action = ""
        self._pending_capture: tuple[str, str | None] | None = None
        self._pending_apply = False
        self._apply_requested = False
        self._last_plan: dict[str, Any] | None = None
        self._last_status: dict[str, Any] | None = None
        self._controls: list[tk.Widget] = []
        self.protocol("WM_DELETE_WINDOW", self.close)
        self.report_callback_exception = self._report_callback_exception
        self._build_style()
        self._build_ui()
        self.load_config(show_error=False)
        self._poll_id = self.after(80, self.poll)
        if auto_refresh:
            self.refresh_status()

    # ---------- construction ----------
    def _build_style(self) -> None:
        style = ttk.Style(self)
        try:
            style.theme_use("vista")
        except tk.TclError:
            pass
        style.configure("Title.TLabel", font=("Segoe UI", 18, "bold"))
        style.configure("State.TLabel", font=("Segoe UI", 13, "bold"))
        style.configure("Good.TLabel", foreground="#087f23")
        style.configure("Warn.TLabel", foreground="#a15c00")
        style.configure("Danger.TLabel", foreground="#b00020")

    def _button(self, parent: tk.Widget, text: str, command: Callable[[], None], **kw: Any) -> ttk.Button:
        button = ttk.Button(parent, text=text, command=command, **kw)
        self._controls.append(button)
        return button

    def _combo(self, parent: tk.Widget, variable: tk.StringVar, **kw: Any) -> ttk.Combobox:
        combo = ttk.Combobox(parent, textvariable=variable, state="readonly", **kw)
        self._controls.append(combo)
        return combo

    def _build_ui(self) -> None:
        self._build_simple_home()
        self.advanced_frame = ttk.Frame(self)
        self._build_advanced(self.advanced_frame)
        self._show_simple()

    def _show_simple(self) -> None:
        if self.advanced_frame.winfo_ismapped():
            self.advanced_frame.pack_forget()
        self.simple_frame.pack(fill="both", expand=True)
        self._refresh_simple()

    def _show_advanced(self) -> None:
        self.simple_frame.pack_forget()
        self.advanced_frame.pack(fill="both", expand=True)

    def _build_simple_home(self) -> None:
        self.simple_frame = ttk.Frame(self, padding=24)
        ttk.Label(self.simple_frame, text="文件版本切换", style="Title.TLabel").pack(anchor="w")
        ttk.Label(self.simple_frame, text="在你保存的几个版本之间一键切换。只有你勾选的文件会被替换，其它文件不动。",
                  wraplength=640).pack(anchor="w", pady=(4, 14))
        self.simple_current = ttk.Label(self.simple_frame, text="正在读取…", style="State.TLabel")
        self.simple_current.pack(anchor="w", pady=(0, 12))
        self.simple_buttons = ttk.Frame(self.simple_frame)
        self.simple_buttons.pack(fill="x", pady=(0, 16))
        bottom = ttk.Frame(self.simple_frame)
        bottom.pack(fill="x")
        self._button(bottom, "设置 / 添加版本", self.start_wizard).pack(side="left")
        self._button(bottom, "高级管理", self._show_advanced).pack(side="right")

    def _refresh_simple(self) -> None:
        for child in self.simple_buttons.winfo_children():
            child.destroy()
        profile = self.selected_profile()
        states = (profile.get("states") or []) if profile else []
        ready = [s for s in states if s.get("snapshotRoot") and not s.get("needsCapture")]
        if not ready:
            self.simple_current.configure(text="还没有可用的版本。点下面“设置 / 添加版本”开始。", style="Warn.TLabel")
            return
        current_name = "未匹配任何版本"
        if self._last_status:
            mode = str(self._last_status.get("currentMode", ""))
            match = next((s for s in states if str(s.get("id")) == mode), None)
            if match:
                current_name = match.get("name", mode)
        self.simple_current.configure(text=f"当前：{current_name}", style="Good.TLabel")
        for state in ready:
            name = state.get("name", state.get("id"))
            ttk.Button(self.simple_buttons, text=f"切换到：{name}",
                       command=lambda s=state: self._simple_switch(s)).pack(fill="x", pady=4)

    def _simple_switch(self, state: dict[str, Any]) -> None:
        name = state.get("name", state.get("id"))
        if not messagebox.askyesno("确认切换", f"切换到“{name}”？\n只会替换你勾选管理的文件，其它文件保持不动。", parent=self):
            return
        label = f"{state.get('name', state.get('id'))} [{state.get('id')}]"
        self.state_var.set(label)
        self.apply_selected_state()

    def _build_advanced(self, parent: ttk.Frame) -> None:
        header = ttk.Frame(parent, padding=(16, 13, 16, 5)); header.pack(fill="x")
        ttk.Label(header, text="高级管理", style="Title.TLabel").pack(side="left")
        self.state_label = ttk.Label(header, text="尚未读取", style="State.TLabel"); self.state_label.pack(side="right")
        self._button(header, "返回简单模式", self._show_simple).pack(side="right", padx=8)

        selectors = ttk.Frame(parent, padding=(16, 0, 16, 8)); selectors.pack(fill="x")
        ttk.Label(selectors, text="方案：").pack(side="left")
        self.profile_var = tk.StringVar(); self.profile_box = self._combo(selectors, self.profile_var, width=33)
        self.profile_box.pack(side="left", padx=(6, 14)); self.profile_box.bind("<<ComboboxSelected>>", self.on_profile_changed)
        ttk.Label(selectors, text="版本：").pack(side="left")
        self.state_var = tk.StringVar(); self.state_box = self._combo(selectors, self.state_var, width=25)
        self.state_box.pack(side="left", padx=(6, 14)); self.state_box.bind("<<ComboboxSelected>>", self.on_state_changed)
        self._button(selectors, "重新加载", self.load_config).pack(side="right")
        self._button(selectors, "添加版本向导", self.start_wizard).pack(side="right", padx=8)

        self.tabs = ttk.Notebook(parent); self.tabs.pack(fill="both", expand=True, padx=14, pady=4)
        self.status_tab = ttk.Frame(self.tabs, padding=12); self.scan_tab = ttk.Frame(self.tabs, padding=12)
        self.diff_tab = ttk.Frame(self.tabs, padding=12); self.capture_tab = ttk.Frame(self.tabs, padding=12)
        self.log_tab = ttk.Frame(self.tabs, padding=12)
        for tab, title in ((self.status_tab, "状态"), (self.scan_tab, "扫描与受管文件"),
                           (self.diff_tab, "差异与切换"), (self.capture_tab, "采集状态"), (self.log_tab, "日志与事务")):
            self.tabs.add(tab, text=title)
        self._build_status(); self._build_scan(); self._build_diff(); self._build_capture(); self._build_log()

    def _build_status(self) -> None:
        row = ttk.Frame(self.status_tab); row.pack(fill="x")
        self.status_mode = ttk.Label(row, text="当前状态：尚未读取", style="State.TLabel"); self.status_mode.pack(side="left")
        self._button(row, "刷新状态", self.refresh_status).pack(side="right")
        self.status_info = ttk.Label(self.status_tab, text=""); self.status_info.pack(anchor="w", pady=9)
        actions = ttk.Frame(self.status_tab); actions.pack(fill="x", pady=(0, 8))
        self._button(actions, "预览所选状态", self.preview_selected_state).pack(side="left")
        self._button(actions, "应用所选状态", self.apply_selected_state).pack(side="left", padx=7)
        self._button(actions, "去采集所选状态", self.capture_selected_state).pack(side="left")
        self.status_tree = ttk.Treeview(self.status_tab, columns=("id", "name", "exact", "files", "error"), show="headings")
        self.status_tree.bind("<<TreeviewSelect>>", self.on_status_tree_select)
        for col, title, width in (("id", "状态 ID", 170), ("name", "名称", 210), ("exact", "当前匹配", 100), ("files", "快照文件数", 100), ("error", "快照错误", 430)):
            self.status_tree.heading(col, text=title); self.status_tree.column(col, width=width, anchor="w")
        self.status_tree.pack(fill="both", expand=True)
        bottom = ttk.Frame(self.status_tab); bottom.pack(fill="x", pady=(9, 0))
        self.cache_label = ttk.Label(bottom, text="缓存："); self.cache_label.pack(side="left")
        self.process_label = ttk.Label(bottom, text="占用进程："); self.process_label.pack(side="right")

    def _build_scan(self) -> None:
        row = ttk.Frame(self.scan_tab); row.pack(fill="x")
        self._button(row, "扫描目标目录", self.scan).pack(side="left")
        self._button(row, "选择目录并扫描", self.scan_external).pack(side="left", padx=7)
        self._button(row, "选择单文件纳管", self.choose_single_file).pack(side="left", padx=7)
        self._button(row, "纳管选中", self.manage_selected).pack(side="right")
        self._button(row, "取消纳管选中", self.unmanage_selected).pack(side="right", padx=7)
        self._button(row, "纳管目录前缀", self.manage_directory_prefix).pack(side="right", padx=7)
        self.scan_info = ttk.Label(self.scan_tab, text="扫描结果会显示在这里。可多选后纳管。"); self.scan_info.pack(anchor="w", pady=8)
        self.scan_tree = ttk.Treeview(self.scan_tab, columns=("path", "managed", "size", "sha256"), show="headings", selectmode="extended")
        for col, title, width in (("path", "相对路径", 420), ("managed", "受管", 75), ("size", "大小", 90), ("sha256", "SHA-256", 500)):
            self.scan_tree.heading(col, text=title); self.scan_tree.column(col, width=width, anchor="w")
        self.scan_tree.pack(fill="both", expand=True)
        manage = ttk.Frame(self.scan_tab); manage.pack(fill="x", pady=(8, 0))
        self._button(manage, "新增方案", self.add_profile).pack(side="left")
        self._button(manage, "重命名方案", self.rename_profile).pack(side="left", padx=5)
        self._button(manage, "删除方案（仅元数据）", self.delete_profile).pack(side="left", padx=5)
        self._button(manage, "新增状态", self.add_state).pack(side="right")
        self._button(manage, "复制状态", self.copy_state).pack(side="right", padx=5)
        self._button(manage, "重命名状态", self.rename_state).pack(side="right", padx=5)
        self._button(manage, "删除状态（仅元数据）", self.delete_state).pack(side="right", padx=5)

    def _build_diff(self) -> None:
        row = ttk.Frame(self.diff_tab); row.pack(fill="x")
        self._button(row, "生成差异预览", self.diff).pack(side="left")
        self.dry_var = tk.BooleanVar(value=True); dry = ttk.Checkbutton(row, text="仅预览，不修改文件", variable=self.dry_var, command=self.invalidate_plan); dry.pack(side="left", padx=12); self._controls.append(dry)
        self.unknown_var = tk.BooleanVar(value=False); unk = ttk.Checkbutton(row, text="允许未知覆盖", variable=self.unknown_var, command=self.invalidate_plan); unk.pack(side="left", padx=7); self._controls.append(unk)
        self.delete_var = tk.BooleanVar(value=False); dele = ttk.Checkbutton(row, text="允许受管删除", variable=self.delete_var, command=self.invalidate_plan); dele.pack(side="left", padx=7); self._controls.append(dele)
        self.apply_button = self._button(row, "应用（先预览）", self.apply); self.apply_button.pack(side="right")
        self.apply_button.configure(state="disabled")
        self.plan_label = ttk.Label(self.diff_tab, text="尚未生成计划。", wraplength=1050); self.plan_label.pack(anchor="w", pady=9)
        self.diff_tree = ttk.Treeview(self.diff_tab, columns=("path", "kind", "current", "expected"), show="headings")
        for col, title, width in (("path", "相对路径", 400), ("kind", "变化", 100), ("current", "当前 SHA-256", 270), ("expected", "期望 SHA-256", 270)):
            self.diff_tree.heading(col, text=title); self.diff_tree.column(col, width=width, anchor="w")
        self.diff_tree.pack(fill="both", expand=True)

    def _build_capture(self) -> None:
        ttk.Label(self.capture_tab, text="快照只写入 folder-data，不直接替换目标目录。采集前会扫描并提示缺失文件。", wraplength=1050).pack(anchor="w", pady=(0, 14))
        self._button(self.capture_tab, "从当前目标目录采集", self.capture_current).pack(anchor="w", pady=5)
        self._button(self.capture_tab, "选择外部目录采集", self.capture_external).pack(anchor="w", pady=5)
        self._button(self.capture_tab, "采集当前状态（样例确认）", self.capture_current).pack(anchor="w", pady=5)
        self.capture_info = ttk.Label(self.capture_tab, text=""); self.capture_info.pack(anchor="w", pady=12)

    def _build_log(self) -> None:
        row = ttk.Frame(self.log_tab); row.pack(fill="x")
        self._button(row, "刷新事务", self.refresh_transactions).pack(side="left")
        self._button(row, "打开事务目录", self.open_transaction_dir).pack(side="left", padx=7)
        self.rollback_button = self._button(row, "预览并回滚", self.rollback_selected); self.rollback_button.pack(side="right")
        self._button(row, "打开旧版工具", self.open_legacy).pack(side="right", padx=7)
        self.tx_tree = ttk.Treeview(self.log_tab, columns=("id", "status", "profile", "state", "path"), show="headings")
        for col, title, width in (("id", "事务 ID", 245), ("status", "状态", 145), ("profile", "方案", 150), ("state", "状态", 130), ("path", "目录", 430)):
            self.tx_tree.heading(col, text=title); self.tx_tree.column(col, width=width, anchor="w")
        self.tx_tree.pack(fill="both", expand=True, pady=(9, 8)); self.tx_tree.bind("<<TreeviewSelect>>", self.show_transaction)
        self.tx_detail = tk.Text(self.log_tab, height=9, wrap="none", font=("Consolas", 10), state="disabled"); self.tx_detail.pack(fill="x")
        self.refresh_transactions()

    # ---------- metadata ----------
    def load_config(self, show_error: bool = True) -> None:
        try:
            self.config_data = self.store.load()
            if ProfileStore is not None:
                self.store = ProfileStore(self.config_path)
            self._using_fallback_store = False
            self._config_hash = _hash_file(self.config_path)
            self._fill_profiles()
            if not self.config_data.get("profiles"):
                self._show_unconfigured("还没有方案，请使用“首次设置向导”。")
        except Exception as exc:
            # A missing user config is a normal first-run state.  Keep the empty
            # schema-2 document in memory only; the wizard writes it after the
            # user has selected real paths.  Never create metadata in resources.
            if not self.config_path.exists():
                self.config_data = dict(EMPTY_CONFIG)
                self._config_hash = None
                self._show_unconfigured("没有配置，请使用“首次设置向导”。")
                return
            # Older Cocos metadata commonly used relative snapshotRoot values.
            # The backend resolves those relative to the config directory, so
            # keep the GUI usable without rewriting the user's config on load.
            try:
                self.config_data = self._fallback_store.load()
                self.store = self._fallback_store
                self._using_fallback_store = True
                self._config_hash = _hash_file(self.config_path)
                self._fill_profiles()
                self._show_unconfigured("已读取配置；相对快照会按配置目录解析，未采集时请使用“去采集”。")
            except Exception:
                self.config_data = {}
                self._show_unconfigured("没有可用配置，请使用“首次设置向导”。")
                if show_error:
                    messagebox.showerror("配置错误", str(exc))

    def _show_unconfigured(self, text: str) -> None:
        if hasattr(self, "state_label"):
            self.state_label.configure(text=text, style="Warn.TLabel")
        if hasattr(self, "status_mode"):
            self.status_mode.configure(text="当前状态：未配置", style="Warn.TLabel")
        if hasattr(self, "status_info"):
            self.status_info.configure(text=text)

    def _fill_profiles(self) -> None:
        profiles = self.config_data.get("profiles") or []
        labels = [f"{p.get('name', p.get('id'))} [{p.get('id')}]" for p in profiles]
        self.profile_box["values"] = labels
        wanted = next((f"{p.get('name', p.get('id'))} [{p.get('id')}]" for p in profiles if p.get("id") == self.config_data.get("defaultProfileId")), "")
        if self.profile_var.get() not in labels:
            self.profile_var.set(wanted or (labels[0] if labels else ""))
        self.update_states()

    def selected_profile(self) -> dict[str, Any] | None:
        selected = self.profile_var.get()
        for profile in self.config_data.get("profiles") or []:
            label = f"{profile.get('name', profile.get('id'))} [{profile.get('id')}]"
            if label == selected or profile.get("id") == selected:
                return profile
        return None

    def selected_profile_id(self) -> str | None:
        profile = self.selected_profile(); return str(profile["id"]) if profile and profile.get("id") else None

    def update_states(self) -> None:
        profile = self.selected_profile(); states = profile.get("states", []) if profile else []
        labels = [f"{s.get('name', s.get('id'))} [{s.get('id')}]" for s in states]
        self.state_box["values"] = labels
        if self.state_var.get() not in labels:
            self.state_var.set(labels[0] if labels else "")
        self._update_apply_button()

    def selected_state(self) -> dict[str, Any] | None:
        profile = self.selected_profile(); selected = self.state_var.get()
        for state in profile.get("states", []) if profile else []:
            if selected in (state.get("id"), f"{state.get('name', state.get('id'))} [{state.get('id')}]" ):
                return state
        return None

    def selected_state_id(self) -> str | None:
        state = self.selected_state(); return str(state["id"]) if state and state.get("id") else None

    def on_profile_changed(self, _event: Any = None) -> None:
        self.update_states(); self.invalidate_plan(); self.state_label.configure(text="尚未读取", style="State.TLabel")
        if not self._busy: self.refresh_status()

    def on_state_changed(self, _event: Any = None) -> None:
        self.invalidate_plan()
        self._update_apply_button()
        if self._last_status is not None: self.state_label.configure(text="状态选择已改变", style="Warn.TLabel")

    def _update_apply_button(self) -> None:
        if not hasattr(self, "apply_button"):
            return
        state = self.selected_state()
        valid_snapshot = bool(state and state.get("snapshotRoot") and not state.get("needsCapture"))
        if self._last_status:
            row = next((x for x in self._last_status.get("states", []) or [] if state and str(x.get("id")) == str(state.get("id"))), None)
            valid_snapshot = valid_snapshot and not bool(row and row.get("error"))
        try:
            self.apply_button.configure(state="normal" if valid_snapshot and not self._busy else "disabled")
        except tk.TclError:
            pass

    def on_status_tree_select(self, _event: Any = None) -> None:
        selected = self.status_tree.selection()
        if selected:
            values = self.status_tree.item(selected[0], "values")
            if values:
                sid = str(values[0])
                self.state_var.set(next((f"{s.get('name', s.get('id'))} [{s.get('id')}]" for s in (self.selected_profile() or {}).get("states", []) if str(s.get("id")) == sid), sid))

    def preview_selected_state(self) -> None:
        if self.selected_state_id(): self.diff()

    def apply_selected_state(self) -> None:
        if self.selected_state_id(): self.apply()

    def capture_selected_state(self) -> None:
        if self.selected_state_id(): self.capture_current()

    def _save_metadata(self, data: dict[str, Any] | None = None) -> bool:
        try:
            payload = data if data is not None else self.config_data
            self.store.save(payload, expected_hash=self._config_hash)
            self.config_data = payload
            self._using_fallback_store = False
            self._config_hash = _hash_file(self.config_path)
            self._fill_profiles(); self.invalidate_plan()
            return True
        except Exception as exc:
            messagebox.showerror("保存配置失败", str(exc)); return False

    def _invalidate_states(self, profile: dict[str, Any]) -> None:
        # Keep old snapshots for audit/recovery.  Their manifest no longer
        # matches the managed revision, so the backend will refuse to apply it.
        for state in profile.get("states", []):
            state["needsCapture"] = True
        self.capture_info.configure(text="受管清单已改变：旧快照已保留但不可应用，请重新采集。")

    def start_wizard(self) -> None:
        """Create a profile in memory; only Finish writes metadata."""
        win = tk.Toplevel(self)
        win.title("首次设置"); win.transient(self); win.grab_set(); win.geometry("820x600")
        target_var = tk.StringVar(); source_var = tk.StringVar()
        ttk.Label(win, text="1. 选择目标目录").pack(anchor="w", padx=12, pady=(12, 2))
        target_row = ttk.Frame(win); target_row.pack(fill="x", padx=12)
        ttk.Entry(target_row, textvariable=target_var).pack(side="left", fill="x", expand=True)
        ttk.Button(target_row, text="选择…", command=lambda: self._wizard_choose_dir(target_var)).pack(side="left", padx=6)
        ttk.Label(win, text="2. 扫描候选文件（可多选后纳管）").pack(anchor="w", padx=12, pady=(10, 2))
        tree = ttk.Treeview(win, columns=("path", "size"), show="headings", selectmode="extended", height=15)
        tree.heading("path", text="相对路径"); tree.heading("size", text="大小"); tree.column("path", width=650); tree.column("size", width=100)
        tree.pack(fill="both", expand=True, padx=12)
        ttk.Label(win, text="来源路径（可选；用于之后采集样例）").pack(anchor="w", padx=12, pady=(8, 2))
        source_row = ttk.Frame(win); source_row.pack(fill="x", padx=12)
        ttk.Entry(source_row, textvariable=source_var).pack(side="left", fill="x", expand=True)
        ttk.Button(source_row, text="选择…", command=lambda: self._wizard_choose_dir(source_var)).pack(side="left", padx=6)
        names_row = ttk.Frame(win); names_row.pack(fill="x", padx=12, pady=8)
        ttk.Label(names_row, text="状态 1").pack(side="left"); n1 = tk.StringVar(value="日常"); ttk.Entry(names_row, textvariable=n1, width=18).pack(side="left", padx=5)
        ttk.Label(names_row, text="状态 2").pack(side="left"); n2 = tk.StringVar(value="自动化"); ttk.Entry(names_row, textvariable=n2, width=18).pack(side="left", padx=5)
        hint = ttk.Label(win, text="取消不会写入配置。完成后请到“采集状态”建立快照。", style="Warn.TLabel"); hint.pack(anchor="w", padx=12)
        def scan_target() -> None:
            for item in tree.get_children(): tree.delete(item)
            raw = target_var.get().strip()
            if not raw: return
            root = Path(raw).expanduser()
            if not root.is_dir(): hint.configure(text="目标目录不存在或不是目录。", style="Danger.TLabel"); return
            try:
                for path in sorted(root.rglob("*")):
                    if path.is_file() and not any(part.startswith(".") for part in path.relative_to(root).parts):
                        try: tree.insert("", "end", iid=str(path.relative_to(root)), values=(str(path.relative_to(root)), path.stat().st_size))
                        except OSError: pass
                hint.configure(text="请选择受管文件；未选择的文件只统计为未管理。", style="Warn.TLabel")
            except OSError as exc: hint.configure(text=f"扫描失败：{exc}", style="Danger.TLabel")
        ttk.Button(target_row, text="扫描", command=scan_target).pack(side="left")
        def finish() -> None:
            target = Path(target_var.get().strip()).expanduser().resolve()
            names = [n1.get().strip(), n2.get().strip()]
            if not target.is_dir() or any(not name for name in names) or names[0].casefold() == names[1].casefold():
                hint.configure(text="请提供有效目标目录和两个不同的状态名。", style="Danger.TLabel"); return
            selected = [str(tree.item(i, "values")[0]).replace("/", "\\") for i in tree.selection()]
            if not selected:
                hint.configure(text="至少纳管一个候选文件。", style="Danger.TLabel"); return
            profile = {"id": _new_id(), "name": target.name or "新方案", "targetRoot": str(target),
                       "managed": [{"path": p} for p in selected], "states": [], "guards": [], "cacheDirectories": []}
            for name in names:
                state = {"id": _new_id(), "name": name, "snapshotRoot": None}
                if source_var.get().strip(): state["sourceRoot"] = str(Path(source_var.get().strip()).expanduser().resolve())
                profile["states"].append(state)
            data = {"schemaVersion": 2, "defaultProfileId": profile["id"], "profiles": [profile]}
            if self._save_metadata(data):
                win.grab_release(); win.destroy(); self.tabs.select(self.status_tab)
                self._show_unconfigured("方案已保存，请先采集两个状态快照。")
                self.refresh_status()
        buttons = ttk.Frame(win); buttons.pack(fill="x", padx=12, pady=10)
        ttk.Button(buttons, text="取消", command=lambda: (win.grab_release(), win.destroy())).pack(side="right")
        ttk.Button(buttons, text="完成并去采集", command=finish).pack(side="right", padx=7)

    @staticmethod
    def _wizard_choose_dir(variable: tk.StringVar) -> None:
        chosen = filedialog.askdirectory(title="选择目录")
        if chosen: variable.set(chosen)

    def add_profile(self) -> None:
        name = simpledialog.askstring("新增方案", "方案名称：", parent=self)
        if not name: return
        target = filedialog.askdirectory(title="选择目标目录")
        if not target: return
        profile = {"id": _new_id(), "name": name.strip(), "targetRoot": str(Path(target).resolve()), "managed": [], "states": [], "guards": [], "cacheDirectories": []}
        profile["states"].append({"id": _new_id(), "name": "默认状态", "snapshotRoot": None})
        self.config_data.setdefault("profiles", []).append(profile)
        self.config_data["defaultProfileId"] = profile["id"]
        self._save_metadata()

    def rename_profile(self) -> None:
        p = self.selected_profile()
        if not p: return
        name = simpledialog.askstring("重命名方案", "新名称：", initialvalue=p.get("name", ""), parent=self)
        if name and name.strip(): p["name"] = name.strip(); self._save_metadata()

    def delete_profile(self) -> None:
        p = self.selected_profile()
        if not p or len(self.config_data.get("profiles", [])) <= 1: messagebox.showwarning("无法删除", "至少保留一个方案。"); return
        if not messagebox.askyesno("确认删除", "只删除方案元数据，不删除目标目录或快照。继续吗？", parent=self): return
        self.config_data["profiles"] = [x for x in self.config_data["profiles"] if x.get("id") != p.get("id")]
        self.config_data["defaultProfileId"] = self.config_data["profiles"][0].get("id")
        self._save_metadata()

    def _ask_state_name(self, title: str, initial: str = "") -> str | None:
        value = simpledialog.askstring(title, "状态名称：", initialvalue=initial, parent=self)
        return value.strip() if value and value.strip() else None

    def add_state(self) -> None:
        p = self.selected_profile(); name = self._ask_state_name("新增状态") if p else None
        if p and name:
            p.setdefault("states", []).append({"id": _new_id(), "name": name, "snapshotRoot": None}); self._save_metadata()

    def rename_state(self) -> None:
        s = self.selected_state(); name = self._ask_state_name("重命名状态", str(s.get("name", ""))) if s else None
        if s and name: s["name"] = name; self._save_metadata()

    def copy_state(self) -> None:
        p = self.selected_profile(); s = self.selected_state(); name = self._ask_state_name("复制状态", str(s.get("name", "") + " 副本")) if s else None
        if p and s and name:
            p.setdefault("states", []).append({"id": _new_id(), "name": name, "snapshotRoot": None}); self._save_metadata()

    def delete_state(self) -> None:
        p = self.selected_profile(); s = self.selected_state()
        if not p or not s: return
        if len(p.get("states", [])) <= 1: messagebox.showwarning("无法删除", "至少保留一个状态。"); return
        if messagebox.askyesno("确认删除", "只删除状态元数据，不删除旧快照或目标文件。继续吗？", parent=self):
            p["states"] = [x for x in p["states"] if x.get("id") != s.get("id")]; self._save_metadata()

    # ---------- backend calls ----------
    def _args(self, action: str, *extra: str) -> list[str]:
        profile_id = self.selected_profile_id()
        args = ["-Action", action]
        if profile_id: args += ["-ProfileId", profile_id]
        args.extend(str(x) for x in extra if x is not None)
        return args

    def call(self, args: list[str], action: str, pending_apply: bool = False) -> bool:
        if self._busy or getattr(self.runner, "running", False):
            messagebox.showwarning("操作进行中", "请等待当前操作结束。"); return False
        self._busy = True; self._pending_action = action; self._pending_apply = pending_apply
        self._set_busy(True); self._append_log(f"\n> {action}: {' '.join(args)}\n")
        try:
            try:
                self.runner.run(args, lambda result: self.events.put(result))
            except TypeError:  # fake runners commonly expose run(args) only.
                self.runner.run(args)
            return True
        except Exception as exc:
            self._set_busy(False); self._busy = False; messagebox.showerror("启动失败", str(exc)); return False

    def refresh_status(self) -> None:
        if self.selected_profile_id(): self.call(self._args("Status"), "status")

    def scan(self) -> None:
        if self.selected_profile_id(): self.call(self._args("Scan"), "scan")

    def scan_external(self) -> None:
        root = filedialog.askdirectory(title="选择要扫描的目录")
        if root: self.call(self._args("Scan", "-SourceRoot", root), "scan")

    def diff(self) -> None:
        state = self.selected_state_id()
        if not state: messagebox.showwarning("没有状态", "请先选择状态。"); return
        extras = ["-StateId", state]
        if self.unknown_var.get(): extras.append("-AllowUnknown")
        if self.delete_var.get(): extras.append("-AllowManagedDelete")
        self.call(self._args("Diff", *extras), "diff")

    def apply(self) -> None:
        state = self.selected_state_id()
        if not state:
            messagebox.showwarning("没有状态", "请先选择状态。")
            return
        # Apply is always digest-guarded and starts with a fresh Diff.
        self._apply_requested = True
        self._pending_apply = True
        self.diff()

    def _apply_plan_after_preview(self) -> None:
        plan = self._last_plan
        if not plan: return
        blockers = plan.get("blockers") or []
        if blockers:
            messagebox.showerror("计划被阻止", "\n".join(str(x) for x in blockers)); self._pending_apply = False; self._apply_requested = False; return
        if not plan.get("planDigest"):
            messagebox.showwarning("无法应用", "计划没有 digest；请重新生成差异预览。", parent=self)
            self._pending_apply = False
            self._apply_requested = False
            return
        if self.dry_var.get() and not self._apply_requested:
            self.plan_label.configure(text=self.plan_label.cget("text") + "\n当前为仅预览，未修改文件。")
            self._pending_apply = False
            self._apply_requested = False
            return
        if not messagebox.askyesno("确认应用", "将按上方计划修改受管文件；未管理文件不受影响。继续吗？", parent=self):
            self._pending_apply = False; self._apply_requested = False; return
        state = self.selected_state_id(); extras = ["-StateId", state or "", "-ExpectedPlanDigest", str(plan.get("planDigest", ""))]
        if self.unknown_var.get(): extras.append("-AllowUnknown")
        if self.delete_var.get(): extras.append("-AllowManagedDelete")
        self._apply_requested = False
        self.call(self._args("Apply", *extras), "apply")

    def capture_current(self) -> None:
        state = self.selected_state_id()
        if state: self._begin_capture(state, None)

    def capture_external(self) -> None:
        state = self.selected_state_id(); source = filedialog.askdirectory(title="选择样例目录") if state else ""
        if state and source: self._begin_capture(state, source)

    def _begin_capture(self, state: str, source: str | None) -> None:
        self._pending_capture = (state, source)
        extras = ["-StateId", state]
        if source: extras += ["-SourceRoot", source]
        self.call(self._args("Scan", *extras), "capture-scan")

    # ---------- scan/managed files ----------
    @staticmethod
    def _safe_relative(path: str) -> str:
        p = Path(path)
        if p.is_absolute() or ".." in p.parts or "." in p.parts or not path.strip(): raise ValueError("受管路径必须是安全的相对文件路径")
        return str(p).replace("/", "\\")

    def _target_root(self) -> Path | None:
        p = self.selected_profile()
        if not p: return None
        root = Path(str(p.get("targetRoot", "")))
        return root if root.is_absolute() else self.config_path.parent / root

    def _managed_paths(self) -> set[str]:
        return {str(x.get("path")).replace("/", "\\").lower() for x in (self.selected_profile() or {}).get("managed", [])}

    def render_scan(self, data: dict[str, Any]) -> None:
        for item in self.scan_tree.get_children(): self.scan_tree.delete(item)
        managed = self._managed_paths()
        for row in data.get("files", []) or []:
            path = str(row.get("path", "")); self.scan_tree.insert("", "end", iid=path, values=(path, "是" if path.lower() in managed else "否", row.get("size", ""), row.get("sha256", "")))
        self.scan_info.configure(text=f"扫描文件 {len(data.get('files', []) or [])} 个；受管 {data.get('managedCount', len(managed))}；未管理 {data.get('unmanagedCount', '')}")

    def _selected_scan_paths(self) -> list[str]:
        return [str(self.scan_tree.item(i, "values")[0]) for i in self.scan_tree.selection()]

    def manage_selected(self) -> None:
        paths = self._selected_scan_paths(); self.add_managed_paths(paths)

    def unmanage_selected(self) -> None:
        paths = {p.lower() for p in self._selected_scan_paths()}; profile = self.selected_profile()
        if not profile: return
        profile["managed"] = [x for x in profile.get("managed", []) if str(x.get("path", "")).lower() not in paths]
        if paths: self._invalidate_states(profile); self._save_metadata()

    def add_managed_paths(self, paths: list[str]) -> None:
        profile = self.selected_profile()
        if not profile: return
        existing = self._managed_paths(); additions = []
        try:
            for raw in paths:
                clean = self._safe_relative(raw)
                if clean.lower() not in existing:
                    if Path(clean).suffix == "" and self._target_root() and (self._target_root() / clean).is_dir(): raise ValueError("请选择文件而非目录")
                    additions.append({"path": clean}); existing.add(clean.lower())
        except ValueError as exc:
            messagebox.showerror("受管路径无效", str(exc)); return
        if additions:
            profile.setdefault("managed", []).extend(additions); self._invalidate_states(profile); self._save_metadata()

    def manage_directory_prefix(self) -> None:
        prefix = filedialog.askdirectory(title="选择目标目录中的文件夹")
        root = self._target_root()
        if not prefix or not root: return
        try: rel = Path(prefix).resolve().relative_to(root.resolve())
        except ValueError: messagebox.showerror("目录无效", "只能选择目标根目录内的文件夹。"); return
        prefix_text = str(rel).replace("/", "\\").strip("\\")
        paths = [str(self.scan_tree.item(i, "values")[0]) for i in self.scan_tree.get_children() if str(self.scan_tree.item(i, "values")[0]).replace("/", "\\").lower().startswith(prefix_text.lower() + "\\")]
        self.add_managed_paths(paths)

    def choose_single_file(self) -> None:
        root = self._target_root(); path = filedialog.askopenfilename(title="选择目标文件", initialdir=str(root) if root else None)
        if not path or not root: return
        try: rel = Path(path).resolve().relative_to(root.resolve())
        except ValueError: messagebox.showerror("文件无效", "只能选择目标根目录内的单文件。"); return
        self.add_managed_paths([str(rel)])

    # ---------- result processing ----------
    def poll(self) -> None:
        if self._closing: return
        try:
            while True: self.handle(self.events.get_nowait())
        except queue.Empty: pass
        self._poll_id = self.after(80, self.poll)

    def handle(self, result: dict[str, Any]) -> None:
        self._set_busy(False); self._busy = False
        raw = str(result.get("stdout", "") or ""); stderr = str(result.get("stderr", "") or "")
        self._append_log(raw + ("\n[stderr]\n" + stderr if stderr else "") + f"\n[exit={result.get('returncode')} ]\n")
        try: payload = json.loads(raw.strip())
        except Exception:
            messagebox.showerror("协议错误", stderr or "后端没有返回合法 JSON"); self.state_label.configure(text="协议错误", style="Danger.TLabel"); return
        valid = isinstance(payload, dict) and payload.get("schemaVersion") == 2 and isinstance(payload.get("ok"), bool) and "action" in payload and "data" in payload and "error" in payload
        if not valid:
            messagebox.showerror("协议错误", "后端 JSON 不符合 schemaVersion 2 合同"); self.state_label.configure(text="协议错误", style="Danger.TLabel"); return
        ok = bool(payload["ok"]); code = int(result.get("returncode", -1) or 0)
        if (ok and code != 0) or ((not ok) and code == 0):
            messagebox.showerror("协议错误", "后端 ok 与进程退出码不一致"); self.state_label.configure(text="协议错误", style="Danger.TLabel"); return
        action = str(payload.get("action", "")).lower(); data = payload.get("data") or {}
        if not ok:
            error = payload.get("error") or {}; messagebox.showerror("操作失败", str(error.get("message", "目录操作失败")))
            self.state_label.configure(text="操作失败", style="Danger.TLabel"); return
        if action == "status": self.render_status(data)
        elif action == "scan":
            self.render_scan(data)
            if self._pending_capture:
                state, source = self._pending_capture; missing = data.get("missingCount")
                if missing is None:
                    listed = {str(x.get("path", "")).lower() for x in data.get("files", []) or []}; missing = sum(1 for p in self._managed_paths() if p not in listed)
                if messagebox.askyesno("确认采集", f"扫描发现缺失受管文件 {missing} 个。\n将采集当前选定目录为状态快照，是否继续？", parent=self):
                    extras = ["-StateId", state]
                    if source: extras += ["-SourceRoot", source]
                    self._pending_capture = None; self.call(self._args("Capture", *extras), "capture")
                else: self._pending_capture = None
        elif action == "diff": self.render_diff(data); self._last_plan = data; self._last_status = None; self.state_label.configure(text="计划已生成，当前状态需重新确认", style="Warn.TLabel"); self._pending_action = ""; self._apply_plan_after_preview() if self._pending_apply else None
        elif action == "capture":
            self.capture_info.configure(text=f"采集完成：{data.get('fileCount', 0)} 个文件，缺失 {data.get('missingCount', 0)} 个。快照可供状态查看。")
            self._pending_capture = None; self.refresh_status()
        elif action == "apply":
            self._last_plan = None; self.state_label.configure(text="已应用，正在刷新状态…", style="Warn.TLabel"); self.refresh_status(); self.refresh_transactions()
        elif action == "rollback": self.refresh_status(); self.refresh_transactions()
        else: self.refresh_status()

    def render_status(self, data: dict[str, Any]) -> None:
        self._last_status = data
        current = str(data.get("currentMode", "unknown")); good = current not in {"unknown", "ambiguous", "mixed", "recovery-required"}
        display_current = next((str(s.get("name")) for s in data.get("states", []) or [] if str(s.get("id")) == current), current)
        self.state_label.configure(text=f"当前：{display_current}", style="Good.TLabel" if good else "Warn.TLabel")
        self.status_mode.configure(text=f"当前状态：{display_current}", style="Good.TLabel" if good else "Warn.TLabel")
        self.status_info.configure(text=f"目标目录：{data.get('targetRoot', '')}；受管 {data.get('managedCount', 0)}；未管理 {data.get('unmanagedCount', 0)}")
        for item in self.status_tree.get_children(): self.status_tree.delete(item)
        for state in data.get("states", []) or []:
            error = state.get("error", "")
            if error:
                error = f"{error}；请去采集"
            self.status_tree.insert("", "end", values=(state.get("id", ""), state.get("name", ""), "是" if state.get("exact") else "否", state.get("fileCount", "-"), error))
        self.cache_label.configure(text="缓存目录：" + ", ".join(map(str, data.get("cacheDirectories", []) or [])))
        self.process_label.configure(text="占用进程：" + ", ".join(map(str, data.get("blockedProcesses", []) or [])) if data.get("blockedProcesses") else "占用进程：无")
        self._refresh_simple()
        self._update_apply_button()

    def render_diff(self, data: dict[str, Any]) -> None:
        for item in self.diff_tree.get_children(): self.diff_tree.delete(item)
        for change in data.get("changes", []) or []:
            self.diff_tree.insert("", "end", values=(change.get("path", ""), change.get("kind", ""), change.get("currentHash") or "-", change.get("expectedHash") or "-"))
        counts = data.get("counts") or {}; blockers = data.get("blockers") or []
        self.plan_label.configure(text=f"计划摘要：新增 {counts.get('add', 0)}，修改 {counts.get('modify', 0)}，删除 {counts.get('remove', 0)}，不变 {counts.get('unchanged', 0)}；digest={data.get('planDigest', '')}\n阻止原因：" + ("；".join(map(str, blockers)) if blockers else "无"))
        self.tabs.select(self.diff_tab)

    # ---------- transactions / utilities ----------
    def refresh_transactions(self) -> None:
        for item in self.tx_tree.get_children(): self.tx_tree.delete(item)
        if not self.state_root.exists(): return
        for journal in sorted(self.state_root.glob("**/journal.json"), reverse=True):
            try:
                data = json.loads(journal.read_text(encoding="utf-8-sig")); txid = journal.parent.name
                self.tx_tree.insert("", "end", iid=str(journal.parent), values=(txid, data.get("status", ""), data.get("profileId", ""), data.get("stateId", ""), str(journal.parent)))
            except Exception: self.tx_tree.insert("", "end", iid=str(journal.parent), values=(journal.parent.name, "journal-invalid", "", "", str(journal.parent)))

    def show_transaction(self, _event: Any = None) -> None:
        selected = self.tx_tree.selection()
        if not selected: return
        path = Path(selected[0]) / "journal.json"
        try: value = path.read_text(encoding="utf-8-sig")
        except Exception as exc: value = str(exc)
        self.tx_detail.configure(state="normal"); self.tx_detail.delete("1.0", "end"); self.tx_detail.insert("1.0", value); self.tx_detail.configure(state="disabled")

    def rollback_selected(self) -> None:
        selected = self.tx_tree.selection()
        if not selected: messagebox.showwarning("未选择事务", "请先选择一个事务并查看预览。"); return
        path = Path(selected[0]) / "journal.json"
        try: journal = json.loads(path.read_text(encoding="utf-8-sig"))
        except Exception as exc: messagebox.showerror("日志错误", str(exc)); return
        txid = path.parent.name; status = journal.get("status", "")
        if status == "recovery-required": messagebox.showwarning("需要恢复", "该事务处于 recovery-required，回滚前请确认目标目录没有被其他工具修改。")
        if not messagebox.askyesno("确认回滚", f"将按事务 {txid} 的 hash guard 回滚；这是预览后的确认步骤。继续吗？", parent=self): return
        self.call(self._args("Rollback", "-TransactionId", txid), "rollback")

    def open_transaction_dir(self) -> None:
        selected = self.tx_tree.selection(); path = Path(selected[0]) if selected else self.state_root
        self._open_path(path)

    def open_tool_dir(self) -> None: self._open_path(RESOURCE_ROOT)

    @staticmethod
    def _open_path(path: Path) -> None:
        try:
            if hasattr(os, "startfile"): os.startfile(str(path))
            else: subprocess.Popen(["xdg-open", str(path)])
        except Exception: pass

    def open_legacy(self) -> None:
        legacy = RESOURCE_ROOT / "gui" / "app.py"
        if legacy.exists(): subprocess.Popen([sys.executable, str(legacy)])
        else: messagebox.showwarning("旧版工具不存在", str(legacy))

    def invalidate_plan(self) -> None:
        self._last_plan = None
        if hasattr(self, "plan_label"): self.plan_label.configure(text="计划已失效，请重新生成差异预览。")

    def _set_busy(self, busy: bool) -> None:
        self._busy = busy
        for widget in self._controls:
            try: widget.configure(state="disabled" if busy else ("readonly" if isinstance(widget, ttk.Combobox) else "normal"))
            except (tk.TclError, AttributeError): pass
        try: self.tabs.configure(state="disabled" if busy else "normal")
        except tk.TclError: pass

    def _append_log(self, text: str) -> None:
        self._write_text_log(text)
        try:
            self.log_root.mkdir(parents=True, exist_ok=True); stamp = time.strftime("%Y%m%d")
            with (self.log_root / f"folder-switcher-{stamp}.log").open("a", encoding="utf-8") as handle: handle.write(text)
        except OSError: pass

    def _write_text_log(self, text: str) -> None:
        if not hasattr(self, "tx_detail"): return
        self.tx_detail.configure(state="normal"); self.tx_detail.insert("end", text); self.tx_detail.see("end"); self.tx_detail.configure(state="disabled")

    def _report_callback_exception(self, exc_type: type[BaseException], exc: BaseException, tb: Any) -> None:
        text = "".join(traceback.format_exception(exc_type, exc, tb)); self._append_log(text)
        try: messagebox.showerror("程序错误", str(exc), parent=self)
        except tk.TclError: pass

    def close(self) -> None:
        if self._busy or getattr(self.runner, "running", False):
            messagebox.showwarning("操作进行中", "写入事务不可中断，请等待操作完成后再关闭窗口。", parent=self); return
        self._closing = True
        if self._poll_id:
            try: self.after_cancel(self._poll_id)
            except tk.TclError: pass
            self._poll_id = None
        self.destroy()


App = FolderApp


def main() -> None:
    try:
        app = FolderApp(); app.mainloop()
    except Exception as exc:
        try: messagebox.showerror("文件夹工具启动失败", str(exc))
        except Exception: print(f"folder_app startup failure: {exc}", file=sys.stderr)


if __name__ == "__main__": main()
