"""
ScoutBot Web - native desktop window entry point (pywebview).

This is used by the PyInstaller build (see build/scoutbot.spec) instead of
launcher.py: running the app from source still uses `uvicorn main:app`
directly, per README.md, and launcher.py (browser-tab mode) is kept as-is
and still buildable on its own if ever needed.

Frozen into ScoutBot.exe, this starts the same FastAPI app used from source
(`main.app`) with uvicorn in a background thread, then opens it in a native
pywebview window (Edge WebView2 on Windows) instead of the system browser -
no address bar, tabs, or other browser chrome. Everything else (FastAPI
routes, SQLite via database.py, the frontend served from FRONTEND_DIR,
SSE at /api/events) is untouched; this only changes how the UI window is
presented.

The app is still meant to be closed from inside the webapp itself
(Settings > General > Exit ScoutBot, which calls POST /api/system/exit) -
that endpoint signals this same process to shut down, and this module's
job is to also close the native window when that happens so nothing is
left behind. Closing the window directly (titlebar X) also stops the
server cleanly. All log output still goes to bot.log next to the exe
(see logger.py) since there is no console to print to.
"""
import os
import sys
import threading
import time


def _install_data_environment():
    """Resolve mutable ScoutBot state to a stable per-user Windows folder."""
    os.environ.setdefault("SCOUTBOT_STANDALONE", "1")
    if sys.platform == "win32":
        root = os.path.join(
            os.environ.get("LOCALAPPDATA", os.path.expanduser("~")),
            "ScoutBot",
            "Data",
        )
    else:
        root = os.path.join(os.path.expanduser("~"), ".scoutbot", "data")
    root = os.path.abspath(os.environ.setdefault("DASHBOARD_SCOUT_DATA_DIR", root))
    os.environ["DASHBOARD_SCOUT_DATA_DIR"] = root
    os.makedirs(root, exist_ok=True)
    return root


def _set_windows_app_identity():
    """Give ScoutBot its own Windows taskbar/application identity."""
    if sys.platform != "win32":
        return
    try:
        import ctypes

        app_id = "ScoutBot.Standalone"
        shell32 = ctypes.windll.shell32
        shell32.SetCurrentProcessExplicitAppUserModelID(app_id)
    except (AttributeError, OSError):
        # The executable icon still provides the visual identity if the
        # AppUserModelID API is unavailable on an older/non-Windows runtime.
        pass


def _resource_base():
    """Resolve the base dir whether running from source or from a
    PyInstaller-frozen bundle (where files are unpacked under sys._MEIPASS)."""
    if getattr(sys, "frozen", False) and hasattr(sys, "_MEIPASS"):
        return sys._MEIPASS
    return os.path.dirname(os.path.abspath(__file__))


def _wait_for_server(host, port, timeout=20, server=None):
    import urllib.request
    import urllib.error

    client_host = "127.0.0.1" if host in ("0.0.0.0", "::") else host
    if ":" in client_host: client_host = f"[{client_host}]"
    url = f"http://{client_host}:{port}"
    deadline = time.time() + timeout
    while time.time() < deadline:
        try:
            if server is not None and not server.started:
                time.sleep(0.25)
                continue
            with urllib.request.urlopen(url, timeout=1):
                return True
        except urllib.error.HTTPError as error:
            status = error.code
            error.close()
            if status in (401, 403): return True
            time.sleep(0.25)
        except Exception:
            time.sleep(0.25)
    return False


def main():
    _set_windows_app_identity()
    data_root = _install_data_environment()

    # Frozen builds run with cwd unset to the exe's folder by default in
    # some launch contexts (e.g. double-click from a pinned shortcut) -
    # pin it explicitly so streamers.db and .env are read/written next to
    # the exe, matching the source-run behavior of `uvicorn main:app`.
    base = _resource_base()
    os.chdir(base)

    # BUGFIX: custom social icons (Settings > Icons — stored client-side
    # in the webview's localStorage) were resetting to defaults on every
    # restart of the packaged build. Same root cause class as the
    # config.py credentials fix: pywebview's edgechromium backend picks
    # its own WebView2 user-data folder (where localStorage actually
    # lives) by default, and that default isn't guaranteed to resolve to
    # the same real on-disk path on every launch of a frozen exe - so a
    # fresh, empty profile could get created each time, silently
    # discarding localStorage (and any other browser-side storage)
    # instead of reusing the previous one. Anchoring it explicitly to a
    # folder next to the exe itself (same anchor as config.py's
    # ENV_PATH / database.py's streamers.db) guarantees every launch
    # reuses the same profile. Must be set before `import webview`
    # creates its WebView2 backend. No-op on non-Windows (no WebView2).
    if getattr(sys, "frozen", False) and sys.platform == "win32":
        webview2_data_dir = os.path.join(data_root, "webview_data")
        os.makedirs(webview2_data_dir, exist_ok=True)
        os.environ.setdefault("WEBVIEW2_USER_DATA_FOLDER", webview2_data_dir)

    import config
    import uvicorn
    import webview
    from main import app
    from logger import logger

    host = config.WEB_HOST
    port = config.WEB_PORT
    client_host = "127.0.0.1" if host in ("0.0.0.0", "::") else host
    if ":" in client_host: client_host = f"[{client_host}]"
    url = f"http://{client_host}:{port}"

    # 🪟 Windowed build has no console — sys.stdout is None, so print()
    # would raise. logger already writes to bot.log regardless (same
    # guard as launcher.py).
    logger.info(f"ScoutBot Web starting at {url}")
    logger.info("Running as a native desktop window (pywebview) — use Settings > General > Exit ScoutBot in the webapp, or close the window, to stop the server.")

    # uvicorn runs in a background thread so this thread is free to hand
    # off to pywebview's own blocking event loop (pywebview must run on
    # the main thread on Windows). log_config=None for the same reason as
    # launcher.py: uvicorn's default logging setup probes sys.stdout for
    # color support, and sys.stdout is None in a windowed build.
    server_config = uvicorn.Config(app, host=host, port=port, log_level="info", log_config=None)
    server = uvicorn.Server(server_config)

    server_thread = threading.Thread(target=server.run, daemon=True)
    server_thread.start()

    if not _wait_for_server(host, port, server=server):
        server.should_exit = True
        server_thread.join(timeout=5)
        raise RuntimeError("ScoutBot server failed to start; check bot.log and the configured port.")

    window = webview.create_window(
        "ScoutBot",
        url,
        width=1400,
        height=900,
        min_size=(900, 600),
        # pywebview defaults text_select to False, which disables text
        # selection AND, as a consequence, the native WebView2/GTK/Qt
        # right-click context menu (Cut/Copy/Paste) - so right-clicking in
        # a text field to paste in, e.g., a Twitch username silently did
        # nothing. Ctrl+V and the frontend's own paste handling (see
        # app.js addUsernameInput paste listener) were never affected
        # since neither depends on text_select - only the right-click
        # path was blocked. There is no separate context_menu kwarg in
        # this pywebview version; text_select=True is what actually
        # restores it.
        text_select=True,
    )

    def _on_closed():
        # Covers both exit paths: the titlebar close button (this event
        # fires first) and Settings > General > Exit ScoutBot, which hits
        # POST /api/system/exit (see main.py) and then closes this same
        # window itself (see below) — either way the uvicorn server
        # (daemon thread) is torn down here so nothing lingers after the
        # window disappears.
        logger.info("ScoutBot window closed — stopping server.")
        server.should_exit = True

    window.events.closed += _on_closed

    def _shutdown():
        server.should_exit = True
        window.destroy()

    app.state.shutdown_callback = _shutdown

    # Opens Twitch/X/Discord links (target="_blank" in the frontend, e.g.
    # "Open on Twitch") in the user's actual default browser instead of a
    # second pywebview window — pywebview's default behavior for new-window
    # navigation already does this, but it's pinned explicitly here since
    # it's required behavior for this app, not just an incidental default.
    webview.settings["OPEN_EXTERNAL_LINKS_IN_BROWSER"] = True

    # gui="edgechromium" pins Windows to Edge WebView2 (needed for
    # SSE/fetch streaming and modern JS; pywebview would otherwise fall
    # back to the legacy MSHTML engine on some systems, which supports
    # neither). No-op on other platforms via the try/except below, since
    # this build only ships a Windows exe (build/scoutbot.spec), but this
    # keeps `python backend/desktop_launcher.py` runnable for local
    # testing on macOS/Linux too.
    # BUGFIX: custom icons set via "Image URL" (Settings > Icons) weren't
    # surviving an app restart, while file-uploaded custom icons mostly
    # did — both are stored identically client-side in localStorage (see
    # frontend app.js saveCustomIcons), so this wasn't a difference in
    # the frontend code at all. Root cause: webview.start() defaults to
    # private_mode=True ("cookies and local storage are not preserved" —
    # pywebview loads the window in an ephemeral/InPrivate WebView2
    # context), which discards localStorage on close regardless of the
    # WEBVIEW2_USER_DATA_FOLDER pin above — that env var only fixes
    # *where* WebView2's profile folder resolves to, it does nothing if
    # the window is never told to persist storage to a profile at all.
    # In practice this mostly showed up for the URL-entry field (typed
    # right before closing/restarting to test it) rather than the file
    # upload (usually set once early on and not immediately restart-
    # tested), but the underlying loss applied to both, and to any other
    # localStorage-backed setting.
    #
    # private_mode=False (with storage_path pinned next to the exe, same
    # anchor as WEBVIEW2_USER_DATA_FOLDER/streamers.db/.env) tells
    # pywebview to actually persist local storage/cookies to disk and
    # reuse it on the next launch.
    storage_path = None
    if getattr(sys, "frozen", False) and sys.platform == "win32":
        storage_path = os.path.join(data_root, "webview_data")

    try:
        webview.start(
            gui="edgechromium" if sys.platform == "win32" else None,
            private_mode=False,
            storage_path=storage_path,
        )
    finally:
        server.should_exit = True
        server_thread.join(timeout=20)

    # webview.start() blocks until the window is closed; make sure the
    # server is signalled even if _on_closed didn't already do it (e.g.
    # exit triggered via /api/system/exit while the window was still
    # being torn down).
    server.should_exit = True
    server_thread.join(timeout=5)


if __name__ == "__main__":
    main()
