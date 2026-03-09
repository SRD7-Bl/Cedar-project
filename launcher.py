import argparse
import subprocess
import sys
import time
import traceback
import urllib.error
import urllib.request
import webbrowser
from pathlib import Path
from tkinter import Tk, messagebox, ttk


HOST = "127.0.0.1"
PORT = 8787
HEALTH_URL = f"http://{HOST}:{PORT}/health"
APP_URLS = {
    "pms": f"http://{HOST}:{PORT}/app/pms",
    "gms": f"http://{HOST}:{PORT}/app/gms",
}
APP_LABELS = {
    "pms": "PMS",
    "gms": "GMS",
}


def runtime_dir() -> Path:
    if getattr(sys, "frozen", False):
        return Path(sys.executable).resolve().parent
    return Path(__file__).resolve().parent


BACKEND_LOG_PATH = runtime_dir() / "launcher_backend.log"


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(add_help=False)
    parser.add_argument("app", nargs="?")
    parser.add_argument("--backend", action="store_true")
    return parser.parse_args()


def infer_app(explicit_app: str | None) -> str:
    if explicit_app:
        app = explicit_app.strip().lower()
        if app in APP_URLS:
            return app

    stem = Path(sys.argv[0]).stem.lower()
    if "pms" in stem:
        return "pms"
    if "gms" in stem:
        return "gms"
    return "gms"


def run_backend() -> None:
    try:
        import backend
        import uvicorn

        if BACKEND_LOG_PATH.exists():
            BACKEND_LOG_PATH.unlink()

        uvicorn.run(
            backend.app,
            host=HOST,
            port=PORT,
            log_level="warning",
            log_config=None,
            access_log=False,
        )
    except Exception:
        BACKEND_LOG_PATH.write_text(traceback.format_exc(), encoding="utf-8")
        raise


def healthcheck(timeout: float = 1.0) -> bool:
    try:
        with urllib.request.urlopen(HEALTH_URL, timeout=timeout) as response:
            return response.status == 200
    except (urllib.error.URLError, TimeoutError):
        return False


def wait_for_backend(proc: subprocess.Popen[bytes] | None = None, timeout: float = 20.0) -> bool:
    deadline = time.time() + timeout
    while time.time() < deadline:
        if healthcheck():
            return True
        if proc is not None and proc.poll() is not None:
            return False
        time.sleep(0.25)
    return False


def start_backend_process() -> subprocess.Popen[bytes]:
    if getattr(sys, "frozen", False):
        cmd = [sys.executable, "--backend"]
        workdir = Path(sys.executable).resolve().parent
    else:
        cmd = [sys.executable, str(Path(__file__).resolve()), "--backend"]
        workdir = Path(__file__).resolve().parent

    creationflags = getattr(subprocess, "CREATE_NO_WINDOW", 0)
    return subprocess.Popen(
        cmd,
        cwd=str(workdir),
        creationflags=creationflags,
    )


def stop_backend_process(proc: subprocess.Popen[bytes] | None) -> None:
    if proc is None or proc.poll() is not None:
        return

    proc.terminate()
    try:
        proc.wait(timeout=5)
    except subprocess.TimeoutExpired:
        proc.kill()
        proc.wait(timeout=5)


def open_app(app_key: str) -> None:
    webbrowser.open(APP_URLS[app_key], new=1)


def show_control_window(app_key: str, backend_proc: subprocess.Popen[bytes] | None, owns_backend: bool) -> None:
    root = Tk()
    root.title(f"Cedar {APP_LABELS[app_key]} Launcher")
    root.resizable(False, False)

    frame = ttk.Frame(root, padding=16)
    frame.grid(row=0, column=0, sticky="nsew")

    title = ttk.Label(frame, text=f"{APP_LABELS[app_key]} 已启动", font=("Helvetica", 14, "bold"))
    title.grid(row=0, column=0, sticky="w")

    url_label = ttk.Label(frame, text=APP_URLS[app_key], foreground="#444444")
    url_label.grid(row=1, column=0, pady=(6, 0), sticky="w")

    status_text = "后端由当前启动器管理" if owns_backend else "检测到已有后端，当前启动器只负责打开页面"
    status_label = ttk.Label(frame, text=status_text, wraplength=360)
    status_label.grid(row=2, column=0, pady=(10, 0), sticky="w")

    tips_label = ttk.Label(
        frame,
        text="点击“重新打开页面”可再次打开浏览器；关闭这个窗口会结束当前启动器。",
        wraplength=360,
    )
    tips_label.grid(row=3, column=0, pady=(10, 0), sticky="w")

    button_row = ttk.Frame(frame)
    button_row.grid(row=4, column=0, pady=(16, 0), sticky="ew")

    def close_window() -> None:
        if owns_backend:
            stop_backend_process(backend_proc)
        root.destroy()

    open_button = ttk.Button(button_row, text="重新打开页面", command=lambda: open_app(app_key))
    open_button.grid(row=0, column=0, padx=(0, 8))

    quit_button = ttk.Button(button_row, text="关闭并退出", command=close_window)
    quit_button.grid(row=0, column=1)

    root.protocol("WM_DELETE_WINDOW", close_window)
    root.mainloop()


def main() -> int:
    args = parse_args()
    if args.backend:
        run_backend()
        return 0

    app_key = infer_app(args.app)
    owns_backend = False
    backend_proc: subprocess.Popen[bytes] | None = None

    if not healthcheck():
        backend_proc = start_backend_process()
        owns_backend = True

    if not wait_for_backend(backend_proc):
        stop_backend_process(backend_proc)
        root = Tk()
        root.withdraw()
        extra = ""
        if BACKEND_LOG_PATH.exists():
            extra = f"\n\n请查看日志文件：\n{BACKEND_LOG_PATH}"
        messagebox.showerror(
            "Cedar Launcher",
            "后端启动失败。请检查依赖是否安装完整，以及 8787 端口是否被占用。" + extra,
        )
        root.destroy()
        return 1

    open_app(app_key)
    show_control_window(app_key, backend_proc, owns_backend)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
