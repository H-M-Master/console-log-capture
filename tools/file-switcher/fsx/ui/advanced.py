"""The advanced screen: every capability the tool has, for whoever wants it.

Kept deliberately complete — multiple profiles, managed-file editing, diff
details, capture, transactions, rollback, log.  What changed from the old UI is
the vocabulary and the surrounding safety: labels say what they do, hashes are
labelled as such, and the states the backend can report are shown in Chinese.
"""
from __future__ import annotations

import tkinter as tk
from pathlib import Path
from tkinter import ttk
from typing import Any, Callable

from ..model import Config, label_for


class AdvancedView(ttk.Frame):
    """Selectors, five tabs, and the callbacks the app implements."""

    def __init__(self, parent: tk.Misc, *, app: Any):
        super().__init__(parent)
        self.app = app
        self._controls: list[tk.Widget] = []

        header = ttk.Frame(self, padding=(16, 14, 16, 6))
        header.pack(fill="x")
        ttk.Label(header, text="高级管理", style="Title.TLabel").pack(side="left")
        self.state_label = ttk.Label(header, text="尚未读取", style="State.TLabel")
        self.state_label.pack(side="right")
        self._button(header, "返回简单模式", app.show_home, style="Home.TButton") \
            .pack(side="right", padx=8)

        selectors = ttk.Frame(self, padding=(16, 0, 16, 10))
        selectors.pack(fill="x")
        ttk.Label(selectors, text="目标设置：").pack(side="left")
        self.profile_var = tk.StringVar()
        self.profile_box = self._combo(selectors, self.profile_var, width=32)
        self.profile_box.pack(side="left", padx=(6, 14))
        self.profile_box.bind("<<ComboboxSelected>>", app.on_profile_changed)
        ttk.Label(selectors, text="版本：").pack(side="left")
        self.state_var = tk.StringVar()
        self.state_box = self._combo(selectors, self.state_var, width=24)
        self.state_box.pack(side="left", padx=(6, 14))
        self.state_box.bind("<<ComboboxSelected>>", app.on_state_changed)
        self._button(selectors, "重新加载", app.load_config, style="Home.TButton") \
            .pack(side="right")
        self._button(selectors, "添加新版本…", app.start_wizard, style="Home.TButton") \
            .pack(side="right", padx=8)

        self.tabs = ttk.Notebook(self)
        self.tabs.pack(fill="both", expand=True, padx=14, pady=4)
        self.status_tab = ttk.Frame(self.tabs, padding=12)
        self.scan_tab = ttk.Frame(self.tabs, padding=12)
        self.diff_tab = ttk.Frame(self.tabs, padding=12)
        self.capture_tab = ttk.Frame(self.tabs, padding=12)
        self.log_tab = ttk.Frame(self.tabs, padding=12)
        for tab, title in ((self.status_tab, "概览"),
                           (self.scan_tab, "文件管理"),
                           (self.diff_tab, "比较与切换"),
                           (self.capture_tab, "保存版本内容"),
                           (self.log_tab, "历史与撤销")):
            self.tabs.add(tab, text=title)
        self._build_status()
        self._build_scan()
        self._build_diff()
        self._build_capture()
        self._build_log()

    # ---------- helpers ----------
    def _button(self, parent: tk.Misc, text: str, command: Callable[[], None],
                style: str = "TButton") -> ttk.Button:
        button = ttk.Button(parent, text=text, command=command, style=style)
        self._controls.append(button)
        return button

    def _combo(self, parent: tk.Misc, variable: tk.StringVar, **kw: Any) -> ttk.Combobox:
        combo = ttk.Combobox(parent, textvariable=variable, state="readonly", **kw)
        self._controls.append(combo)
        return combo

    def _check(self, parent: tk.Misc, text: str, variable: tk.BooleanVar,
               command: Callable[[], None]) -> ttk.Checkbutton:
        check = ttk.Checkbutton(parent, text=text, variable=variable, command=command)
        self._controls.append(check)
        return check

    # ---------- tabs ----------
    def _build_status(self) -> None:
        row = ttk.Frame(self.status_tab)
        row.pack(fill="x")
        self.status_mode = ttk.Label(row, text="当前版本：尚未读取", style="State.TLabel")
        self.status_mode.pack(side="left")
        self._button(row, "刷新", self.app.refresh_status, style="Home.TButton").pack(side="right")
        self.status_info = ttk.Label(self.status_tab, text="", style="Muted.TLabel")
        self.status_info.pack(anchor="w", pady=9)

        actions = ttk.Frame(self.status_tab)
        actions.pack(fill="x", pady=(0, 8))
        self._button(actions, "看看会改哪些文件", self.app.preview_selected_state,
                     style="Home.TButton").pack(side="left")
        self._button(actions, "切换到这个版本", self.app.apply_selected_state,
                     style="Home.TButton").pack(side="left", padx=7)
        self._button(actions, "保存当前内容到该版本", self.app.capture_selected_state,
                     style="Home.TButton").pack(side="left")

        self.status_tree = ttk.Treeview(
            self.status_tab,
            columns=("name", "exact", "files", "error"), show="headings")
        self.status_tree.bind("<<TreeviewSelect>>", self.app.on_status_tree_select)
        for column, title, width in (("name", "版本", 240),
                                     ("exact", "当前是否就是它", 130),
                                     ("files", "已保存文件数", 110),
                                     ("error", "说明", 430)):
            self.status_tree.heading(column, text=title)
            self.status_tree.column(column, width=width, anchor="w")
        self.status_tree.pack(fill="both", expand=True)
        bottom = ttk.Frame(self.status_tab)
        bottom.pack(fill="x", pady=(9, 0))
        self.cache_label = ttk.Label(bottom, text="缓存目录：", style="Muted.TLabel")
        self.cache_label.pack(side="left")
        self.process_label = ttk.Label(bottom, text="占用进程：", style="Muted.TLabel")
        self.process_label.pack(side="right")

    def _build_scan(self) -> None:
        row = ttk.Frame(self.scan_tab)
        row.pack(fill="x")
        self._button(row, "列出目标文件夹内容", self.app.scan, style="Home.TButton").pack(side="left")
        self._button(row, "换个文件夹看看…", self.app.scan_external,
                     style="Home.TButton").pack(side="left", padx=7)
        self._button(row, "挑一个文件加入…", self.app.choose_single_file,
                     style="Home.TButton").pack(side="left")
        self._button(row, "加入选中的文件", self.app.manage_selected,
                     style="Home.TButton").pack(side="right")
        self._button(row, "移出选中的文件", self.app.unmanage_selected,
                     style="Home.TButton").pack(side="right", padx=7)
        self._button(row, "按文件夹批量加入…", self.app.manage_directory_prefix,
                     style="Home.TButton").pack(side="right", padx=7)

        self.scan_info = ttk.Label(
            self.scan_tab, text="“加入”表示这个文件会随版本一起切换；没加入的文件不会被改动。",
            style="Muted.TLabel", wraplength=1000, justify="left")
        self.scan_info.pack(anchor="w", pady=8)

        self.scan_tree = ttk.Treeview(
            self.scan_tab, columns=("path", "managed", "size", "sha256"),
            show="headings", selectmode="extended")
        for column, title, width in (("path", "相对路径", 400),
                                     ("managed", "会一起切换", 100),
                                     ("size", "大小", 90),
                                     ("sha256", "文件指纹（SHA-256）", 460)):
            self.scan_tree.heading(column, text=title)
            self.scan_tree.column(column, width=width, anchor="w")
        self.scan_tree.pack(fill="both", expand=True)

        manage = ttk.Frame(self.scan_tab)
        manage.pack(fill="x", pady=(8, 0))
        self._button(manage, "新建一个目标设置", self.app.add_profile,
                     style="Home.TButton").pack(side="left")
        self._button(manage, "重命名设置", self.app.rename_profile,
                     style="Home.TButton").pack(side="left", padx=5)
        self._button(manage, "删除设置", self.app.delete_profile,
                     style="Home.TButton").pack(side="left", padx=5)
        self._button(manage, "新建版本", self.app.add_state,
                     style="Home.TButton").pack(side="right")
        self._button(manage, "复制版本", self.app.copy_state,
                     style="Home.TButton").pack(side="right", padx=5)
        self._button(manage, "重命名版本", self.app.rename_state,
                     style="Home.TButton").pack(side="right", padx=5)
        self._button(manage, "删除版本", self.app.delete_state,
                     style="Home.TButton").pack(side="right", padx=5)

    def _build_diff(self) -> None:
        row = ttk.Frame(self.diff_tab)
        row.pack(fill="x")
        self._button(row, "看看会改哪些文件", self.app.diff,
                     style="Home.TButton").pack(side="left")
        self.dry_var = tk.BooleanVar(value=True)
        self._check(row, "只看不改（预览）", self.dry_var, self.app.invalidate_plan) \
            .pack(side="left", padx=12)
        self.unknown_var = tk.BooleanVar(value=False)
        self._check(row, "文件被别的程序改过也照换", self.unknown_var,
                    self.app.invalidate_plan).pack(side="left", padx=6)
        self.delete_var = tk.BooleanVar(value=False)
        self._check(row, "允许删除文件", self.delete_var, self.app.invalidate_plan) \
            .pack(side="left", padx=6)
        self.apply_button = self._button(row, "切换到所选版本", self.app.apply,
                                         style="Home.TButton")
        self.apply_button.pack(side="right")
        self.apply_button.configure(state="disabled")

        self.plan_label = ttk.Label(self.diff_tab, text="还没有生成比较结果。",
                                    wraplength=1040, justify="left")
        self.plan_label.pack(anchor="w", pady=9)
        self.diff_tree = ttk.Treeview(
            self.diff_tab, columns=("path", "kind", "current", "expected"),
            show="headings")
        for column, title, width in (("path", "相对路径", 380),
                                     ("kind", "会怎样", 90),
                                     ("current", "现在的指纹", 250),
                                     ("expected", "切换后的指纹", 250)):
            self.diff_tree.heading(column, text=title)
            self.diff_tree.column(column, width=width, anchor="w")
        self.diff_tree.pack(fill="both", expand=True)

    def _build_capture(self) -> None:
        ttk.Label(
            self.capture_tab,
            text="“保存版本内容”会把当前文件存进工具的数据目录，不会改动你的文件。"
                 "保存之后，这个版本才能被切换过去。",
            wraplength=1000, justify="left").pack(anchor="w", pady=(0, 14))
        self._button(self.capture_tab, "把当前文件夹的内容保存为本版本",
                     self.app.capture_current, style="Home.TButton").pack(anchor="w", pady=5)
        self._button(self.capture_tab, "从另一个文件夹导入内容…",
                     self.app.capture_external, style="Home.TButton").pack(anchor="w", pady=5)
        self.capture_info = ttk.Label(self.capture_tab, text="", wraplength=1000,
                                      justify="left")
        self.capture_info.pack(anchor="w", pady=12)

    def _build_log(self) -> None:
        row = ttk.Frame(self.log_tab)
        row.pack(fill="x")
        self._button(row, "刷新", self.app.refresh_transactions,
                     style="Home.TButton").pack(side="left")
        self._button(row, "打开记录文件夹", self.app.open_transaction_dir,
                     style="Home.TButton").pack(side="left", padx=7)
        self.rollback_button = self._button(row, "撤销选中的这次切换",
                                            self.app.rollback_selected,
                                            style="Home.TButton")
        self.rollback_button.pack(side="right")
        ttk.Label(self.log_tab,
                  text="每次切换都会留下记录，可以在这里撤销。",
                  style="Muted.TLabel").pack(anchor="w", pady=(8, 0))
        self.tx_tree = ttk.Treeview(
            self.log_tab, columns=("id", "status", "profile", "state", "path"),
            show="headings")
        for column, title, width in (("id", "记录编号", 230),
                                     ("status", "结果", 130),
                                     ("profile", "目标设置", 140),
                                     ("state", "版本", 120),
                                     ("path", "记录位置", 400)):
            self.tx_tree.heading(column, text=title)
            self.tx_tree.column(column, width=width, anchor="w")
        self.tx_tree.pack(fill="both", expand=True, pady=(9, 8))
        self.tx_tree.bind("<<TreeviewSelect>>", self.app.show_transaction)
        self.tx_detail = tk.Text(self.log_tab, height=9, wrap="none", state="disabled")
        self.tx_detail.pack(fill="x")

    # ---------- population ----------
    def fill_profiles(self, config: Config, selected_id: str) -> None:
        labels = [p.label for p in config.profiles]
        self.profile_box["values"] = labels
        if self.profile_var.get() not in labels:
            target = config.profile(selected_id)
            self.profile_var.set(target.label if target else (labels[0] if labels else ""))

    def fill_states(self, config: Config, profile_id: str, selected_id: str) -> None:
        labels = [s.label for s in config.states(profile_id)]
        self.state_box["values"] = labels
        if self.state_var.get() not in labels:
            target = config.state(profile_id, selected_id)
            self.state_var.set(target.label if target else (labels[0] if labels else ""))

    # ---------- busy ----------
    def set_busy(self, busy: bool) -> None:
        for widget in self._controls:
            try:
                if isinstance(widget, ttk.Combobox):
                    widget.configure(state="disabled" if busy else "readonly")
                else:
                    widget.configure(state="disabled" if busy else "normal")
            except (tk.TclError, AttributeError):
                pass
        try:
            self.tabs.configure(state="disabled" if busy else "normal")
        except tk.TclError:
            pass

    def update_apply_button(self, enabled: bool, busy: bool) -> None:
        try:
            self.apply_button.configure(state="normal" if enabled and not busy else "disabled")
        except tk.TclError:
            pass

    def status(self, text: str, style: str = "State.TLabel") -> None:
        self.state_label.configure(text=text, style=style)

    def select_tab(self, tab: ttk.Frame) -> None:
        try:
            self.tabs.select(tab)
        except tk.TclError:
            pass

    def unconfigured(self, text: str) -> None:
        self.status(text, "Warn.TLabel")
        try:
            self.status_mode.configure(text="当前版本：未配置", style="Warn.TLabel")
            self.status_info.configure(text=text)
        except tk.TclError:
            pass
