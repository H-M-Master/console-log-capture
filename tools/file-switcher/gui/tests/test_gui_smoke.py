import json
import sys
from pathlib import Path

GUI = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(GUI))
import app


class FakeRunner:
    def __init__(self, callback):
        self.callback = callback
        self.running = False
        self.calls = []

    def run(self, args):
        if self.running:
            raise RuntimeError("busy")
        self.running = True
        self.calls.append(list(args))
        self.callback({
            "returncode": 0,
            "stdout": json.dumps({
                "schemaVersion": 1,
                "ok": True,
                "action": "status",
                "data": {
                    "currentMode": "daily",
                    "modeNames": ["daily", "automation"],
                    "targetRoot": "test",
                    "fileCount": 0,
                    "entries": [],
                    "creatorRunning": False,
                    "cacheDirectories": [],
                },
                "transaction": None,
                "error": None,
            }),
            "stderr": "",
            "duration": 0.01,
            "command": args,
        })
        self.running = False


def test_real_tk_smoke():
    root = app.App(
        config_path=GUI.parent / "config.json",
        script_path=GUI.parent / "switch-files.ps1",
        state_root=GUI.parent / "state-test-do-not-use",
        log_root=Path.home() / "AppData" / "Local" / "Temp" / "AnyTestToolsGuiTest",
        runner=None,
    )
    root.after(250, root.close_app)
    root.mainloop()


def test_fake_tk_config_and_transaction():
    runner_holder = {}
    original_error = app.messagebox.showerror
    app.messagebox.showerror = lambda *args, **kwargs: (_ for _ in ()).throw(AssertionError(args))
    try:
        root = app.App(
            config_path=GUI.parent / "config.json",
            script_path=GUI.parent / "switch-files.ps1",
            state_root=GUI.parent / "state-test-do-not-use",
            log_root=Path.home() / "AppData" / "Local" / "Temp" / "AnyTestToolsGuiTest",
            runner=None,
        )
        root.update_idletasks()
        assert root._text(root.config_text).get("1.0", "end-1c").strip()
        root.show_transaction()
        root.close_app()
    finally:
        app.messagebox.showerror = original_error


if __name__ == "__main__":
    test_real_tk_smoke()
    test_fake_tk_config_and_transaction()
    print("GUI smoke tests passed")
