"""
ScoutBot Web - packaged desktop launcher entry point.

This is only used by the PyInstaller build (see build/scoutbot.spec); running
the app from source still uses `uvicorn main:app` directly, per README.md.

Frozen into ScoutBot.exe, this starts the same FastAPI app used from source
(`main.app`) with uvicorn, then opens the dashboard in the user's default
browser once the server is actually accepting connections - there is no
separate GUI window, the browser tab IS the app.

🪟 The packaged build now runs fully windowed (PyInstaller `console=False`
in build/scoutbot.spec, the --windowed equivalent) — no cmd window ever
appears, not even briefly. There's nothing to minimise any more, so this
no longer touches the console at all. The app is meant to be closed from
inside the webapp itself (Settings > General > Exit ScoutBot, which calls
POST /api/system/exit) — that endpoint sends this same process a clean
shutdown signal. All log output still goes to bot.log next to the exe
(see logger.py) since there is no console to print to.
"""
import os
import sys
import threading
import time
import webbrowser


def _resource_base():
    """Resolve the base dir whether running from source or from a
    PyInstaller-frozen bundle (where files are unpacked under sys._MEIPASS)."""
    if getattr(sys, "frozen", False) and hasattr(sys, "_MEIPASS"):
        return sys._MEIPASS
    return os.path.dirname(os.path.abspath(__file__))


def _wait_and_open_browser(host, port):
    import urllib.request

    url = f"http://{host}:{port}"
    deadline = time.time() + 20
    while time.time() < deadline:
        try:
            with urllib.request.urlopen(url, timeout=1):
                break
        except Exception:
            time.sleep(0.25)
    webbrowser.open(url)


def main():
    # Frozen builds run with cwd unset to the exe's folder by default in
    # some launch contexts (e.g. double-click from a pinned shortcut) -
    # pin it explicitly so streamers.db and .env are read/written next to
    # the exe, matching the source-run behavior of `uvicorn main:app`.
    base = _resource_base()
    os.chdir(base)

    import config
    import uvicorn
    from main import app
    from logger import logger

    host = config.WEB_HOST
    port = config.WEB_PORT

    threading.Thread(target=_wait_and_open_browser, args=(host, port), daemon=True).start()

    # 🪟 Windowed build has no console — sys.stdout is None, so print()
    # would raise. logger already writes to bot.log regardless.
    logger.info(f"ScoutBot Web starting at http://{host}:{port}")
    logger.info("Running windowed (no console) — use Settings > General > Exit ScoutBot in the webapp to stop the server.")
    # 🪟 uvicorn.run() normally builds its own logging config (log_config=
    # uvicorn's default dict), which attaches a StreamHandler pointed at
    # sys.stdout/sys.stderr and has its formatter probe stream.isatty()
    # for color support. In this windowed build sys.stdout/sys.stderr are
    # None (no console), so that probe raises
    # "AttributeError: 'NoneType' object has no attribute 'isatty'"
    # during startup (see logging/config.py configure_formatter ->
    # uvicorn/logging.py). Passing log_config=None skips that setup
    # entirely and leaves the logging module as already configured above
    # by logger.py, which already guards the None-stdout case safely.
    uvicorn.run(app, host=host, port=port, log_level="info", log_config=None)


if __name__ == "__main__":
    main()
