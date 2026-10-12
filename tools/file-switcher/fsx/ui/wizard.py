"""A linear three-step wizard for people who have never used this tool.

Step 1 — pick the folder to manage.
Step 2 — tick the files that should switch together.
Step 3 — name this version and say where its content comes from.

The wizard builds a profile and one state, then hands both to the app, which
owns the saving and the capture call.  Cancelling at any point writes nothing.
"""
from __future__ import annotations

import tkinter as tk
from pathlib import Path
from tkinter import filedialog, ttk
from typing import Any, Callable

from . import dialogs

STEPS = ("选择文件夹", "选择文件", "命名版本")


class Wizard(tk.Toplevel):
    """A three-step wizard; ``on_finish`` receives the built profile and state."""

    def __init__(self, parent: tk.Misc, *,
                 on_finish: Callable[[dict[str, Any], dict[str, Any], str], None]):
        super().__init__(parent)
        self._on_finish = on_finish
        self._step = 0
        self._files: list[str] = []

        self.title("设置新版本")
        self.transient(parent)
        self.resizable(True, True)
        self.minsize(720, 520)

        self.header = ttk.Label(self, text="", style="Section.TLabel")
        self.header.pack(anchor="w", padx=20, pady=(18, 2))
        self.hint = ttk.Label(self, text="", wraplength=660, justify="left",
                              style="Note.TLabel")
        self.hint.pack(anchor="w", padx=20)

        self.body = ttk.Frame(self, padding=(20, 14))
        self.body.pack(fill="both", expand=True)
        self.message = ttk.Label(self, text="", wraplength=660, justify="left",
                                 style="Danger.TLabel")
        self.message.pack(anchor="w", padx=20)

        buttons = ttk.Frame(self, padding=(20, 10, 20, 18))
        buttons.pack(fill="x")
        self.back_button = ttk.Button(buttons, text="上一步", style="Home.TButton",
                                      command=self._back)
        self.back_button.pack(side="left")
        self.next_button = ttk.Button(buttons, text="下一步", style="Home.TButton",
                                      command=self._next)
        self.next_button.pack(side="right")
        ttk.Button(buttons, text="取消", style="Home.TButton",
                   command=self._cancel).pack(side="right", padx=8)

        self._build_step(0)
        self._center(parent)
        self.grab_set()

    # ---------- steps ----------
    def _build_step(self, index: int) -> None:
        self._step = index
        for child in self.body.winfo_children():
            child.destroy()
        self.message.configure(text="")
        self.header.configure(text=f"第 {index + 1} 步 / 共 3 步：{STEPS[index]}")
        self.back_button.configure(state="normal" if index > 0 else "disabled")
        self.next_button.configure(text="完成并保存" if index == 2 else "下一步")
        (self._step_target, self._step_files, self._step_name)[index]()

    def _step_target(self) -> None:
        self.hint.configure(
            text="选择要切换文件的那个文件夹。通常是你的游戏目录或工程目录。"
                 "这个文件夹里的其它文件不会被修改。")
        row = ttk.Frame(self.body)
        row.pack(fill="x", pady=(6, 0))
        self.target_var = tk.StringVar(value=getattr(self, "target_var", tk.StringVar()).get())
        entry = ttk.Entry(row, textvariable=self.target_var)
        entry.pack(side="left", fill="x", expand=True)
        ttk.Button(row, text="浏览…", style="Home.TButton",
                   command=self._choose_target).pack(side="left", padx=6)
        ttk.Label(self.body, text="例如：D:\\Games\\我的游戏", style="Muted.TLabel") \
            .pack(anchor="w", pady=(6, 0))

    def _step_files(self) -> None:
        self.hint.configure(
            text="下面是这个文件夹里的文件。勾选需要随版本一起切换的文件——"
                 "没勾选的文件在任何切换中都保持原样，不会被改动。")
        row = ttk.Frame(self.body)
        row.pack(fill="x", pady=(6, 8))
        ttk.Button(row, text="全选", style="Home.TButton",
                   command=lambda: self._select_all(True)).pack(side="left")
        ttk.Button(row, text="全不选", style="Home.TButton",
                   command=lambda: self._select_all(False)).pack(side="left", padx=6)
        self.count_label = ttk.Label(row, text="", style="Muted.TLabel")
        self.count_label.pack(side="right")

        holder = ttk.Frame(self.body)
        holder.pack(fill="both", expand=True)
        self.tree = ttk.Treeview(holder, columns=("path", "size"), show="headings",
                                 selectmode="extended")
        self.tree.heading("path", text="文件（相对路径）")
        self.tree.heading("size", text="大小")
        self.tree.column("path", width=520, anchor="w")
        self.tree.column("size", width=100, anchor="e")
        scroll = ttk.Scrollbar(holder, orient="vertical", command=self.tree.yview)
        self.tree.configure(yscrollcommand=scroll.set)
        self.tree.pack(side="left", fill="both", expand=True)
        scroll.pack(side="right", fill="y")
        self.tree.bind("<<TreeviewSelect>>", lambda _e: self._update_count())
        self._fill_file_tree()

    def _step_name(self) -> None:
        self.hint.configure(
            text="给这个版本起一个你自己认得出的名字，例如“正式版”“测试版”。"
                 "之后再添加别的版本时，会重复这三步。")
        form = ttk.Frame(self.body)
        form.pack(fill="x", pady=(6, 0))
        ttk.Label(form, text="版本名字：").grid(row=0, column=0, sticky="w", pady=6)
        self.name_var = tk.StringVar(value=getattr(self, "name_var", tk.StringVar()).get())
        ttk.Entry(form, textvariable=self.name_var, width=32) \
            .grid(row=0, column=1, sticky="w", pady=6)

        ttk.Label(form, text="这个版本的内容从哪来：").grid(row=1, column=0, sticky="w", pady=6)
        self.source_mode = tk.StringVar(value="current")
        ttk.Radiobutton(form, text="当前文件夹里现在的内容", value="current",
                        variable=self.source_mode).grid(row=1, column=1, sticky="w")
        ttk.Radiobutton(form, text="从另一个文件夹导入（样例/备份）", value="external",
                        variable=self.source_mode).grid(row=2, column=1, sticky="w")

        source_row = ttk.Frame(form)
        source_row.grid(row=3, column=1, sticky="ew", pady=(4, 0))
        self.source_var = tk.StringVar(value=getattr(self, "source_var", tk.StringVar()).get())
        ttk.Entry(source_row, textvariable=self.source_var).pack(side="left", fill="x",
                                                                expand=True)
        ttk.Button(source_row, text="浏览…", style="Home.TButton",
                   command=self._choose_source).pack(side="left", padx=6)
        ttk.Label(self.body, text="保存后，这个版本的按钮会出现在首页。",
                  style="Muted.TLabel").pack(anchor="w", pady=(10, 0))

    # ---------- data ----------
    def _choose_target(self) -> None:
        chosen = filedialog.askdirectory(title="选择要管理的文件夹", parent=self)
        if chosen:
            self.target_var.set(chosen)

    def _choose_source(self) -> None:
        chosen = filedialog.askdirectory(title="选择内容来源文件夹", parent=self)
        if chosen:
            self.source_var.set(chosen)

    def _select_all(self, value: bool) -> None:
        self.tree.selection_set(self.tree.get_children() if value else [])
        self._update_count()

    def _fill_file_tree(self) -> None:
        root = self._target_path()
        if root is None or not root.is_dir():
            self._fail("这个文件夹不存在，或不是一个文件夹。请返回上一步重新选择。")
            return
        self.tree.delete(*self.tree.get_children())
        self._files = []
        try:
            for path in sorted(root.rglob("*")):
                if not path.is_file():
                    continue
                relative = path.relative_to(root)
                # Skip dot-directories and build output nobody wants to switch.
                if any(part.startswith(".") for part in relative.parts):
                    continue
                try:
                    size = path.stat().st_size
                except OSError:
                    continue
                self._files.append(str(relative))
                self.tree.insert("", "end", iid=str(relative),
                                 values=(str(relative), self._human(size)))
        except OSError as exc:
            self._fail(f"无法读取这个文件夹：{exc}")
            return
        if not self._files:
            self._fail("这个文件夹里没有找到任何文件。")
        self._update_count()

    def _update_count(self) -> None:
        chosen = len(self.tree.selection())
        self.count_label.configure(text=f"已勾选 {chosen} / 共 {len(self._files)} 个文件")

    @staticmethod
    def _human(size: int) -> str:
        for unit in ("B", "KB", "MB", "GB"):
            if size < 1024 or unit == "GB":
                return f"{size:.0f} {unit}" if unit == "B" else f"{size:.1f} {unit}"
            size /= 1024.0
        return f"{size:.1f} GB"

    def _target_path(self) -> Path | None:
        raw = self.target_var.get().strip()
        return Path(raw).expanduser() if raw else None

    # ---------- navigation ----------
    def _back(self) -> None:
        if self._step > 0:
            self._remember()
            self._build_step(self._step - 1)

    def _remember(self) -> None:
        """Carry the current step's answers forward into the next screen."""
        if self._step == 1:
            self._selected_now = list(self.tree.selection())

    def _next(self) -> None:
        if self._step == 0:
            root = self._target_path()
            if root is None or not root.is_dir():
                self._fail("请选择一个存在的文件夹。")
                return
        elif self._step == 1:
            if not self.tree.selection():
                self._fail("请至少勾选一个文件——只有勾选的文件会被切换。")
                return
            self._remember()
        else:
            self._finish()
            return
        self._build_step(self._step + 1)

    def _finish(self) -> None:
        name = self.name_var.get().strip()
        if not name:
            self._fail("请给这个版本起一个名字。")
            return
        selected = list(getattr(self, "_selected_now", []))
        if not selected:
            self._fail("没有勾选任何文件，请返回上一步重新选择。")
            return
        source = ""
        if self.source_mode.get() == "external":
            source = self.source_var.get().strip()
            if not source or not Path(source).expanduser().is_dir():
                self._fail("请选择一个存在的内容来源文件夹。")
                return

        root = self._target_path()
        profile = {
            "id": None, "name": name, "targetRoot": str(root.resolve()),
            "managed": [{"path": p.replace("/", "\\")} for p in sorted(selected)],
            "states": [], "guards": [], "cacheDirectories": [],
        }
        state = {"id": None, "name": name, "snapshotRoot": None}
        self._close()
        self._on_finish(profile, state, source)

    def _fail(self, text: str) -> None:
        self.message.configure(text=text)

    def _cancel(self) -> None:
        self._close()

    def _close(self) -> None:
        try:
            self.grab_release()
        except tk.TclError:
            pass
        self.destroy()

    def _center(self, parent: tk.Misc) -> None:
        self.update_idletasks()
        try:
            x = parent.winfo_rootx() + (parent.winfo_width() - self.winfo_width()) // 2
            y = parent.winfo_rooty() + (parent.winfo_height() - self.winfo_height()) // 3
            self.geometry(f"+{max(x, 0)}+{max(y, 0)}")
        except tk.TclError:
            pass
