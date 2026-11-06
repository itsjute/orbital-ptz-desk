#!/usr/bin/env python3
"""
PTZ Desk as a desktop app: starts the server in the background and shows the control page in
its own window (pywebview, i.e. WebView2 on Windows). No console window, no browser tab.

    python app.py           (needs: pip install pywebview)

If pywebview or the WebView2 runtime is missing, the page opens in an Edge or Chrome app window
instead; if neither exists, in the default browser.
The installer from the GitHub releases bundles all of this into "PTZ Desk.exe".
"""
import os
import shutil
import subprocess
import sys
import threading
import time
import webbrowser

SELFTEST = "--selftest" in sys.argv
if SELFTEST:   # build check: own throw-away data folder
    import tempfile
    os.environ["PTZ_DESK_DATA"] = tempfile.mkdtemp(prefix="ptz-desk-selftest-")

import server
from server import log

TITLE = "PTZ Desk"


# --------------------------------------------------------------------------- small platform helpers

def message(text, error=False):
    """A message the user sees even without a console."""
    log(text)
    if os.name == "nt":
        try:
            import ctypes
            ctypes.windll.user32.MessageBoxW(None, text, TITLE, 0x10 if error else 0x40)
            return
        except Exception:  # noqa
            pass
    print(text, file=sys.__stderr__ or sys.stderr)


def single_instance():
    """Windows: if PTZ Desk is already running, bring its window to the front and return False."""
    if os.name != "nt":
        return True
    try:
        import ctypes
        k32, u32 = ctypes.windll.kernel32, ctypes.windll.user32
        single_instance.mutex = k32.CreateMutexW(None, False, "Local\\PTZDesk.SingleInstance")
        if k32.GetLastError() != 183:  # ERROR_ALREADY_EXISTS
            return True
        hwnd = u32.FindWindowW(None, TITLE)
        if hwnd:
            u32.ShowWindow(hwnd, 9)  # SW_RESTORE
            u32.SetForegroundWindow(hwnd)
        return False
    except Exception:  # noqa
        return True


def taskbar_identity():
    """Windows: own taskbar entry (not grouped with Python) when run from source."""
    if os.name == "nt":
        try:
            import ctypes
            ctypes.windll.shell32.SetCurrentProcessExplicitAppUserModelID("PTZDesk.App")
        except Exception:  # noqa
            pass


# --------------------------------------------------------------------------- window

class Api:
    """Exposed to the page as window.pywebview.api.*"""

    # pywebview exposes every public attribute to the page and walks into it – so the
    # window and the state are kept in private attributes (leading underscore).
    def __init__(self):
        self._window = None
        self._force = False
        self._full = False

    def pick_folder(self, start=""):
        import webview
        kind = getattr(getattr(webview, "FileDialog", None), "FOLDER", None) or webview.FOLDER_DIALOG
        start = start if start and os.path.isdir(start) else os.path.expanduser("~")
        res = self._window.create_file_dialog(kind, directory=start)
        if not res:
            return ""
        return res[0] if isinstance(res, (list, tuple)) else str(res)

    def fullscreen(self, on=True):
        if bool(on) != self._full:
            self._full = bool(on)
            self._window.toggle_fullscreen()
        return self._full

    def quit(self):
        """Called by the page after the user confirmed closing during a recording."""
        self._force = True
        threading.Thread(target=self._window.destroy, daemon=True).start()

    def version(self):
        return server.VERSION


def unblock_bundle():
    """Windows marks files unpacked from a downloaded ZIP as coming from the internet, and .NET then
    refuses to load the DLLs the app window needs (pythonnet, WebView2). Remove that mark."""
    if os.name != "nt" or not server.FROZEN:
        return
    n = 0
    for d, _dirs, names in os.walk(server.RES_DIR):
        for name in names:
            if name.lower().endswith((".dll", ".pyd", ".exe")):
                try:
                    os.remove(os.path.join(d, name) + ":Zone.Identifier")
                    n += 1
                except OSError:
                    pass
    if n:
        log(f"Unblocked {n} downloaded program files.")


def run_window(url):
    """Own window via pywebview. Returns False if that is not possible on this computer."""
    unblock_bundle()
    try:
        import webview
    except Exception as e:  # noqa
        log(f"App window not available ({e}) – trying an Edge/Chrome app window.")
        return False
    api = Api()
    try:
        win = webview.create_window(TITLE, url, js_api=api, width=1440, height=900, min_size=(960, 600),
                                    maximized=True, background_color="#0E1217", text_select=True)
    except TypeError:  # older pywebview without some of the options
        win = webview.create_window(TITLE, url, js_api=api, width=1440, height=900, min_size=(960, 600))
    api._window = win

    def closing():
        if server.App.rec and server.App.rec.active and not api._force:
            # let the page ask (styled dialog); it calls api.quit() when confirmed
            threading.Thread(target=lambda: win.evaluate_js("window.ptzDeskCloseRequest && window.ptzDeskCloseRequest()"),
                             daemon=True).start()
            return False
        return True

    win.events.closing += closing
    try:
        webview.settings["ALLOW_DOWNLOADS"] = True
        webview.settings["OPEN_EXTERNAL_LINKS_IN_BROWSER"] = True
    except Exception:  # noqa
        pass
    kwargs = {"private_mode": False, "storage_path": os.path.join(server.DATA_DIR, "webview"),
              "debug": bool(os.environ.get("PTZ_DESK_DEBUG"))}
    if os.name == "nt":
        kwargs["gui"] = "edgechromium"   # never fall back to the old Internet Explorer engine
    server.APP_WINDOW["kind"] = "window"
    try:
        webview.start(**kwargs)
    except TypeError:
        kwargs.pop("private_mode", None)
        kwargs.pop("storage_path", None)
        webview.start(**kwargs)
    except Exception as e:  # noqa – e.g. WebView2 runtime missing
        log(f"App window failed ({type(e).__name__}: {e}) – trying an Edge/Chrome app window.")
        server.APP_WINDOW["kind"] = "browser"
        return False
    return True


def browser_candidates():
    names = []
    if os.name == "nt":
        for env in ("ProgramFiles(x86)", "ProgramFiles", "LOCALAPPDATA"):
            base = os.environ.get(env)
            if base:
                names += [os.path.join(base, "Microsoft", "Edge", "Application", "msedge.exe"),
                          os.path.join(base, "Google", "Chrome", "Application", "chrome.exe")]
    elif sys.platform == "darwin":
        names += ["/Applications/Microsoft Edge.app/Contents/MacOS/Microsoft Edge",
                  "/Applications/Google Chrome.app/Contents/MacOS/Google Chrome",
                  "/Applications/Chromium.app/Contents/MacOS/Chromium"]
    for n in ("msedge", "microsoft-edge", "google-chrome", "chrome", "chromium", "chromium-browser"):
        p = shutil.which(n)
        if p:
            names.append(p)
    return [n for n in names if os.path.isfile(n)]


def run_browser_app(url):
    """Edge/Chrome in app mode with an own profile: a window without tabs and address bar.
    Waits until it is closed. Returns False if no such browser was found."""
    for exe in browser_candidates():
        profile = os.path.join(server.DATA_DIR, "browser")
        try:
            p = subprocess.Popen([exe, f"--app={url}", f"--user-data-dir={profile}", "--no-first-run",
                                  "--no-default-browser-check", "--start-maximized", "--autoplay-policy=no-user-gesture-required"])
        except OSError:
            continue
        log(f"Showing the page in {os.path.basename(exe)} (app mode).")
        server.APP_WINDOW["kind"] = "app-browser"
        # The started process may hand the window over to another Edge/Chrome process and exit
        # at once, so its end says nothing. The page pings every few seconds instead; once the
        # process is gone and the page has been silent for a while, the window is closed.
        server.App.last_request = time.monotonic()
        while True:
            time.sleep(1)
            if p.poll() is not None and time.monotonic() - server.App.last_request > 12:
                break
        log("App window closed.")
        return True
    return False


# --------------------------------------------------------------------------- main

def selftest(out):
    """Used by the installer build: start the server, check what it finds, write JSON, quit."""
    import json
    import urllib.request
    res = {"ok": False}
    try:
        srv, url = server.boot()
        threading.Thread(target=srv.serve_forever, daemon=True).start()
        for _ in range(100):
            if not server.App.booting:
                break
            time.sleep(0.1)
        op = urllib.request.build_opener(urllib.request.ProxyHandler({}))
        get = lambda p: json.loads(op.open(url.rstrip("/") + p, timeout=5).read())
        st, sm = get("/api/status"), get("/api/stabilize")
        page = op.open(url, timeout=5).read()
        res.update(version=server.VERSION, app=st["app"], video=st["video"]["state"], page_bytes=len(page),
                   ffmpeg=server.App.smooth.ff, packages=server.App.smooth.deps_info, stabilize=server.App.smooth.deps_ok)
        try:
            import webview  # noqa
            res["webview"] = getattr(webview, "__version__", "yes")
            if os.name == "nt":
                from webview.platforms import winforms  # noqa – loads pythonnet and the WebView2 wrapper
                res["webview_gui"] = "winforms"
        except Exception as e:  # noqa
            res["webview_error"] = f"{type(e).__name__}: {e}"
        res["ok"] = page.startswith(b"<!DOCTYPE") or page.lower().startswith(b"<!doctype")
        server.shutdown()
    except Exception as e:  # noqa
        import traceback
        res["error"] = traceback.format_exc()
    with open(out, "w", encoding="utf-8") as f:
        json.dump(res, f, indent=2, default=str)
    os._exit(0 if res["ok"] else 1)


def main():
    if SELFTEST:
        i = sys.argv.index("--selftest")
        selftest(sys.argv[i + 1] if len(sys.argv) > i + 1 else "selftest.json")
    taskbar_identity()
    if not single_instance():
        return 0
    try:
        srv, url = server.boot()
    except server.PortInUse as e:
        message(str(e), error=True)
        return 1
    except Exception as e:  # noqa
        message(f"PTZ Desk could not start:\n{type(e).__name__}: {e}\n\nDetails: {server.LOG_PATH}", error=True)
        return 1
    server.on_window_close(server.shutdown)   # when started from a console
    threading.Thread(target=srv.serve_forever, name="http", daemon=True).start()
    try:
        if not run_window(url) and not run_browser_app(url):
            webbrowser.open(url)
            if os.name == "nt":
                message("PTZ Desk is running and opened in your browser.\n\nClick OK to quit PTZ Desk "
                        "(this stops a running recording).")
            else:
                log("Opened in your browser. Quit with Ctrl+C.")
                try:
                    while True:
                        time.sleep(1)
                except KeyboardInterrupt:
                    pass
    finally:
        server.shutdown()
        threading.Thread(target=srv.shutdown, daemon=True).start()
        time.sleep(0.3)
    os._exit(0)   # the window toolkit may keep threads alive


if __name__ == "__main__":
    sys.exit(main())
