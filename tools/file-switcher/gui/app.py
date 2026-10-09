import json
import os
import queue
import subprocess
import threading
import time
import tkinter as tk
from pathlib import Path
from tkinter import filedialog, messagebox, ttk

TOOL_ROOT = Path(__file__).resolve().parent.parent
SCRIPT = TOOL_ROOT / "switch-files.ps1"
CONFIG = TOOL_ROOT / "config.json"
STATE = TOOL_ROOT / "state"
LOG_ROOT = Path(os.environ.get("LOCALAPPDATA", str(Path.home()))) / "AnyTestTools" / "gui" / "logs"


class PowerShellRunner:
    def __init__(self, on_done, script_path=SCRIPT, config_path=CONFIG, state_root=STATE):
        self.on_done = on_done
        self.script_path = Path(script_path)
        self.config_path = Path(config_path)
        self.state_root = Path(state_root)
        self.running = False
        self._lock = threading.Lock()

    def run(self, args):
        with self._lock:
            if self.running:
                raise RuntimeError("已有操作正在执行")
            self.running = True
        thread = threading.Thread(target=self._worker, args=(list(args),), daemon=True)
        thread.start()

    def _worker(self, args):
        started = time.time()
        command = [
            "powershell.exe", "-NoLogo", "-NoProfile", "-NonInteractive",
            "-ExecutionPolicy", "Bypass", "-File", str(self.script_path),
            "-Config", str(self.config_path), "-StateRoot", str(self.state_root),
        ] + args
        try:
            completed = subprocess.run(command, capture_output=True, text=True, encoding="utf-8", errors="replace")
            result = {
                "returncode": completed.returncode,
                "stdout": completed.stdout,
                "stderr": completed.stderr,
                "duration": time.time() - started,
                "command": command[0:12] + args,
            }
        except Exception as exc:
            result = {"returncode": -1, "stdout": "", "stderr": str(exc), "duration": time.time() - started, "command": command[0:12] + args}
        with self._lock:
            self.running = False
        self.on_done(result)


class App(tk.Tk):
    def __init__(self, config_path=CONFIG, script_path=SCRIPT, state_root=STATE, log_root=LOG_ROOT, runner=None):
        super().__init__()
        self.config_path = Path(config_path)
        self.script_path = Path(script_path)
        self.state_root = Path(state_root)
        self.log_root = Path(log_root)
        self.title("AnyTestTools · 文件版本切换器")
        self.geometry("1120x760")
        self.minsize(900, 620)
        self.events = queue.Queue()
        self.last_result = None
        self.config_data = {}
        self._closing = False
        self._poll_id = None
        self.runner = runner or PowerShellRunner(lambda result: self.events.put(result), self.script_path, self.config_path, self.state_root)
        self.protocol("WM_DELETE_WINDOW", self.close_app)
        self._build_style()
        self._build_ui()
        self.load_config()
        self._poll_id = self.after(100, self._poll_events)
        self.refresh_status()

    def _build_style(self):
        style = ttk.Style(self)
        try:
            style.theme_use("vista")
        except tk.TclError:
            pass
        style.configure("Title.TLabel", font=("Segoe UI", 18, "bold"))
        style.configure("State.TLabel", font=("Segoe UI", 13, "bold"))
        style.configure("Danger.TLabel", foreground="#b00020")
        style.configure("Good.TLabel", foreground="#087f23")

    def _build_ui(self):
        header = ttk.Frame(self, padding=(18, 14, 18, 4))
        header.pack(fill="x")
        ttk.Label(header, text="文件版本切换器", style="Title.TLabel").pack(side="left")
        self.header_state = ttk.Label(header, text="读取中…", style="State.TLabel")
        self.header_state.pack(side="right")

        self.notebook = ttk.Notebook(self)
        self.notebook.pack(fill="both", expand=True, padx=14, pady=10)
        self.status_tab = ttk.Frame(self.notebook, padding=12)
        self.switch_tab = ttk.Frame(self.notebook, padding=12)
        self.config_tab = ttk.Frame(self.notebook, padding=12)
        self.log_tab = ttk.Frame(self.notebook, padding=12)
        self.notebook.add(self.status_tab, text="状态首页")
        self.notebook.add(self.switch_tab, text="一键切换")
        self.notebook.add(self.config_tab, text="文件与模式")
        self.notebook.add(self.log_tab, text="日志与事务")
        self._build_status_tab()
        self._build_switch_tab()
        self._build_config_tab()
        self._build_log_tab()

    def _build_status_tab(self):
        top = ttk.Frame(self.status_tab)
        top.pack(fill="x")
        self.status_mode = ttk.Label(top, text="当前模式：读取中…", style="State.TLabel")
        self.status_mode.pack(side="left")
        ttk.Button(top, text="刷新状态", command=self.refresh_status).pack(side="right")
        self.status_info = ttk.Label(self.status_tab, text="")
        self.status_info.pack(anchor="w", pady=(8, 10))
        self.status_tree = ttk.Treeview(self.status_tab, columns=("target", "current", "match", "sources"), show="headings", height=14)
        for col, text, width in (("target", "目标文件", 370), ("current", "当前 SHA-256", 170), ("match", "匹配模式", 160), ("sources", "源文件状态", 250)):
            self.status_tree.heading(col, text=text)
            self.status_tree.column(col, width=width, anchor="w")
        self.status_tree.pack(fill="both", expand=True)
        bottom = ttk.Frame(self.status_tab)
        bottom.pack(fill="x", pady=(10, 0))
        self.cache_label = ttk.Label(bottom, text="缓存：读取中…")
        self.cache_label.pack(side="left")
        self.creator_label = ttk.Label(bottom, text="Creator：读取中…")
        self.creator_label.pack(side="right")

    def _build_switch_tab(self):
        row = ttk.Frame(self.switch_tab)
        row.pack(fill="x")
        ttk.Label(row, text="目标模式：").pack(side="left")
        self.mode_var = tk.StringVar()
        self.mode_combo = ttk.Combobox(row, textvariable=self.mode_var, state="readonly", width=28)
        self.mode_combo.pack(side="left", padx=(8, 18))
        self.dry_var = tk.BooleanVar(value=True)
        ttk.Checkbutton(row, text="先预演（推荐）", variable=self.dry_var).pack(side="left")
        self.unknown_var = tk.BooleanVar(value=False)
        ttk.Checkbutton(row, text="允许覆盖未知/混合状态（高级）", variable=self.unknown_var).pack(side="left", padx=18)
        self.preview_btn = ttk.Button(row, text="预览", command=self.preview_switch)
        self.preview_btn.pack(side="right")
        self.switch_btn = ttk.Button(row, text="执行切换", command=self.execute_switch)
        self.switch_btn.pack(side="right", padx=8)
        self.switch_summary = ttk.Label(self.switch_tab, text="选择目标模式后点击预览。", wraplength=1000)
        self.switch_summary.pack(anchor="w", pady=14)
        self.switch_log = self._make_text(self.switch_tab)
        self.switch_log.pack(fill="both", expand=True)

    def _build_config_tab(self):
        toolbar = ttk.Frame(self.config_tab)
        toolbar.pack(fill="x")
        ttk.Button(toolbar, text="重新加载", command=self.load_config).pack(side="left")
        ttk.Button(toolbar, text="选择配置文件", command=self.choose_config).pack(side="left", padx=8)
        ttk.Button(toolbar, text="保存配置", command=self.save_config).pack(side="left")
        ttk.Label(toolbar, text="可编辑 JSON：entries 支持任意数量和扩展名；保存前会校验。", foreground="#555").pack(side="left", padx=18)
        self.config_text = self._make_text(self.config_tab)
        self.config_text.pack(fill="both", expand=True, pady=(10, 0))

    def _build_log_tab(self):
        row = ttk.Frame(self.log_tab)
        row.pack(fill="x")
        ttk.Button(row, text="刷新事务", command=self.refresh_transactions).pack(side="left")
        ttk.Button(row, text="打开事务目录", command=self.open_transaction_dir).pack(side="left", padx=8)
        self.tx_tree = ttk.Treeview(self.log_tab, columns=("id", "status", "from", "to", "path"), show="headings", height=10)
        for col, text, width in (("id", "事务 ID", 230), ("status", "状态", 130), ("from", "原模式", 120), ("to", "目标模式", 120), ("path", "目录", 470)):
            self.tx_tree.heading(col, text=text)
            self.tx_tree.column(col, width=width, anchor="w")
        self.tx_tree.pack(fill="both", expand=True, pady=(10, 8))
        self.tx_detail = self._make_text(self.log_tab, height=8)
        self.tx_detail.pack(fill="x")
        self.tx_tree.bind("<<TreeviewSelect>>", self.show_transaction)
        self.refresh_transactions()

    def _make_text(self, parent, height=20):
        frame = ttk.Frame(parent)
        text = tk.Text(frame, height=height, wrap="none", font=("Consolas", 10), undo=False)
        ybar = ttk.Scrollbar(frame, orient="vertical", command=text.yview)
        xbar = ttk.Scrollbar(frame, orient="horizontal", command=text.xview)
        text.configure(yscrollcommand=ybar.set, xscrollcommand=xbar.set)
        text.grid(row=0, column=0, sticky="nsew")
        ybar.grid(row=0, column=1, sticky="ns")
        xbar.grid(row=1, column=0, sticky="ew")
        frame.rowconfigure(0, weight=1)
        frame.columnconfigure(0, weight=1)
        frame.text = text
        return frame

    def _text(self, container):
        return container.text

    def load_config(self):
        try:
            self.config_data = json.loads(self.config_path.read_text(encoding="utf-8-sig"))
            self._text(self.config_text).delete("1.0", "end")
            self._text(self.config_text).insert("1.0", json.dumps(self.config_data, ensure_ascii=False, indent=2))
            modes = list((self.config_data.get("modes") or {}).keys())
            self.mode_combo["values"] = modes
            if modes and self.mode_var.get() not in modes:
                self.mode_var.set(modes[0])
        except Exception as exc:
            messagebox.showerror("配置错误", str(exc))

    def choose_config(self):
        messagebox.showinfo("固定配置", f"当前 GUI 使用固定配置：\n{self.config_path}\n\n如需更换工具实例，请复制整个 file-switcher 目录后启动对应 GUI。")

    def save_config(self):
        try:
            value = json.loads(self._text(self.config_text).get("1.0", "end-1c"))
            self.validate_config(value)
            backup = self.config_path.with_suffix(".json.bak")
            if self.config_path.exists():
                backup.write_bytes(self.config_path.read_bytes())
            temp = self.config_path.with_suffix(".json.tmp")
            temp.write_text(json.dumps(value, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
            os.replace(temp, self.config_path)
            self.config_data = value
            self.load_config()
            self.refresh_status()
            messagebox.showinfo("保存成功", "配置已保存。")
        except Exception as exc:
            messagebox.showerror("保存失败", str(exc))

    def validate_config(self, value):
        if value.get("schemaVersion") != 1:
            raise ValueError("schemaVersion 必须为 1")
        target_root = str(value.get("targetRoot", ""))
        if not target_root:
            raise ValueError("targetRoot 不能为空")
        if not os.path.isabs(target_root):
            target_root_path = TOOL_ROOT / target_root
        else:
            target_root_path = Path(target_root)
        if not target_root_path.is_dir():
            raise ValueError(f"targetRoot 不存在或不是目录：{target_root_path}")
        modes = list((value.get("modes") or {}).keys())
        if len(modes) < 2 or len({m.lower() for m in modes}) != len(modes):
            raise ValueError("至少需要两个模式，且模式名不能大小写冲突")
        entries = value.get("entries") or []
        if not entries:
            raise ValueError("entries 不能为空")
        targets = set()
        for entry in entries:
            target = str(entry.get("target", ""))
            if not target or os.path.isabs(target):
                raise ValueError(f"target 必须是相对路径：{target}")
            normalized = os.path.normcase(os.path.normpath(target))
            if normalized in targets:
                raise ValueError(f"目标文件重复：{target}")
            targets.add(normalized)
            sources = entry.get("sources") or {}
            for mode in modes:
                source = sources.get(mode)
                if not source:
                    raise ValueError(f"{target} 缺少模式 {mode} 的源文件")
                source_path = Path(source) if os.path.isabs(source) else TOOL_ROOT / source
                if not source_path.is_file():
                    raise ValueError(f"源文件不存在：{source_path}")

    def refresh_status(self):
        self._run(["-Status", "-OutputFormat", "Json"], "status")

    def preview_switch(self):
        mode = self.mode_var.get()
        if not mode:
            messagebox.showwarning("请选择模式", "请先选择目标模式。")
            return
        args = ["-Mode", mode, "-DryRun", "-OutputFormat", "Json"]
        if self.unknown_var.get():
            args.append("-AllowUnknown")
        self._run(args, "preview")

    def execute_switch(self):
        mode = self.mode_var.get()
        if not mode:
            messagebox.showwarning("请选择模式", "请先选择目标模式。")
            return
        if self.dry_var.get():
            self.preview_switch()
            return
        if self.unknown_var.get() and not messagebox.askyesno("确认覆盖", "当前启用了允许覆盖未知/混合状态。仍要继续吗？"):
            return
        if not messagebox.askyesno("确认切换", f"将切换到 {mode}。Cocos Creator 必须已关闭，是否继续？"):
            return
        args = ["-Mode", mode, "-OutputFormat", "Json"]
        if self.unknown_var.get():
            args.append("-AllowUnknown")
        self._run(args, "switch")

    def _run(self, args, kind):
        if self.runner.running:
            messagebox.showwarning("操作进行中", "请等待当前操作完成。")
            return
        self._set_busy(True)
        self._append_log(f"\n> {kind}: {' '.join(args)}\n")
        try:
            self.runner.run(args)
        except Exception as exc:
            self._set_busy(False)
            messagebox.showerror("启动失败", str(exc))

    def close_app(self):
        if self.runner.running:
            messagebox.showwarning("操作进行中", "当前 PowerShell 操作尚未结束，请等待完成后再关闭窗口。")
            return
        self._closing = True
        if self._poll_id is not None:
            try:
                self.after_cancel(self._poll_id)
            except tk.TclError:
                pass
            self._poll_id = None
        self.destroy()

    def _poll_events(self):
        if self._closing:
            return
        try:
            while True:
                result = self.events.get_nowait()
                self._handle_result(result)
        except queue.Empty:
            pass
        self._poll_id = self.after(100, self._poll_events)

    def _handle_result(self, result):
        self._set_busy(False)
        self.last_result = result
        if not result.get("stdout", "").strip() and result.get("returncode") == 0:
            self._append_log("\n[协议错误：PowerShell 未返回 JSON]\n")
            messagebox.showerror("协议错误", "PowerShell 返回成功，但没有返回结果。")
            return
        raw = result.get("stdout", "")
        if result.get("stderr"):
            raw += "\n[stderr]\n" + result["stderr"]
        self._append_log(raw + f"\n[exit={result['returncode']}, {result['duration']:.1f}s]\n")
        parsed = None
        try:
            parsed = json.loads(result.get("stdout", "").strip())
        except Exception:
            pass
        if parsed and parsed.get("schemaVersion") == 1 and parsed.get("ok") is True:
            data = parsed.get("data") or {}
            if parsed.get("action") == "status":
                self.render_status(data)
            else:
                self.refresh_status()
                self.refresh_transactions()
            self.switch_summary.configure(text="操作成功。" if parsed.get("action") != "status" else self.switch_summary.cget("text"))
        else:
            self.switch_summary.configure(text="操作失败，请查看日志。")
            if parsed and isinstance(parsed.get("error"), dict):
                message = parsed["error"].get("message", "PowerShell 操作失败")
            elif parsed:
                message = "PowerShell 返回了无效的结果协议。"
            else:
                message = result.get("stderr") or result.get("stdout") or "PowerShell 操作失败"
            messagebox.showerror("操作失败", message)

    def render_status(self, data):
        mode = data.get("currentMode", "unknown/mixed")
        self.header_state.configure(text=mode)
        self.status_mode.configure(text=f"当前模式：{mode}", style="Good.TLabel" if mode not in ("unknown/mixed", "ambiguous") else "Danger.TLabel")
        self.status_info.configure(text=f"目标根目录：{data.get('targetRoot', '')}    文件数：{data.get('fileCount', 0)}")
        for item in self.status_tree.get_children():
            self.status_tree.delete(item)
        for entry in data.get("entries", []):
            hashes = entry.get("hashes", {})
            matches = [name for name, value in hashes.items() if value == entry.get("currentHash")]
            self.status_tree.insert("", "end", values=(entry.get("target", ""), entry.get("currentHash", "")[:16], ", ".join(matches) or "未知/混合", "; ".join(hashes.keys())))
        cache = data.get("cacheDirectories", [])
        self.cache_label.configure(text=f"缓存：{sum(1 for x in cache if x.get('exists'))}/{len(cache)} 个目录存在")
        self.creator_label.configure(text=f"Creator：{'运行中' if data.get('creatorRunning') else '未运行'}")
        self.mode_combo["values"] = data.get("modeNames", list((self.config_data.get("modes") or {}).keys()))

    def _set_busy(self, busy):
        state = "disabled" if busy else "normal"
        self.preview_btn.configure(state=state)
        self.switch_btn.configure(state=state)

    def _append_log(self, text):
        widget = self._text(self.switch_log)
        widget.insert("end", text)
        widget.see("end")
        try:
            self.log_root.mkdir(parents=True, exist_ok=True)
            stamp = time.strftime("%Y%m%d")
            with (self.log_root / f"file-switcher-{stamp}.log").open("a", encoding="utf-8") as handle:
                handle.write(text)
        except OSError:
            # The visible UI log remains usable when the optional disk log is unavailable.
            pass

    def refresh_transactions(self):
        for item in self.tx_tree.get_children():
            self.tx_tree.delete(item)
        if not self.state_root.exists():
            return
        for directory in sorted((p for p in self.state_root.iterdir() if p.is_dir()), reverse=True):
            journal = directory / "journal.json"
            if not journal.is_file():
                continue
            try:
                data = json.loads(journal.read_text(encoding="utf-8-sig"))
                self.tx_tree.insert("", "end", iid=str(directory), values=(data.get("id", directory.name), data.get("status", ""), data.get("from", ""), data.get("to", ""), str(directory)))
            except Exception:
                self.tx_tree.insert("", "end", iid=str(directory), values=(directory.name, "journal-invalid", "", "", str(directory)))

    def show_transaction(self, _event=None):
        selected = self.tx_tree.selection()
        if not selected:
            return
        path = Path(selected[0]) / "journal.json"
        detail = self._text(self.tx_detail)
        detail.delete("1.0", "end")
        try:
            detail.insert("1.0", path.read_text(encoding="utf-8-sig"))
        except Exception as exc:
            detail.insert("1.0", str(exc))

    def open_transaction_dir(self):
        selected = self.tx_tree.selection()
        path = Path(selected[0]) if selected else self.state_root
        if path.exists():
            os.startfile(str(path))


if __name__ == "__main__":
    App().mainloop()
