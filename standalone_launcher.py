import os
import sys
import threading
import time


def _install_data_environment():
    os.environ.setdefault("SCOUTBOT_STANDALONE", "1")
    if sys.platform == "win32":
        root = os.path.join(os.environ.get("LOCALAPPDATA", os.path.expanduser("~")), "ScoutBot", "Data")
    else:
        root = os.path.join(os.path.expanduser("~"), ".scoutbot", "data")
    root = os.path.abspath(os.environ.setdefault("DASHBOARD_SCOUT_DATA_DIR", root))
    os.environ["DASHBOARD_SCOUT_DATA_DIR"] = root
    os.makedirs(root, exist_ok=True)
    return root


def _resource_base():
    if getattr(sys, "frozen", False) and hasattr(sys, "_MEIPASS"):
        return sys._MEIPASS
    return os.path.dirname(os.path.abspath(__file__))


def _wait_for_server(host, port, timeout=30, server=None):
    import urllib.request
    import urllib.error
    client_host = "127.0.0.1" if host in ("0.0.0.0", "::") else host
    if ":" in client_host: client_host = f"[{client_host}]"
    url = f"http://{client_host}:{port}"
    deadline = time.time() + timeout
    while time.time() < deadline:
        try:
            if server is not None and not server.started:
                time.sleep(.25)
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
    data_root = _install_data_environment()
    os.environ.setdefault("WEBVIEW2_USER_DATA_FOLDER", os.path.join(data_root, "webview_data"))
    os.makedirs(os.environ["WEBVIEW2_USER_DATA_FOLDER"], exist_ok=True)
    os.chdir(_resource_base())

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

    logger.info("ScoutBot standalone desktop build starting")
    logger.info("Persistent data directory: %s", data_root)

    server_config = uvicorn.Config(app, host=host, port=port, log_level="info", log_config=None)
    server = uvicorn.Server(server_config)
    server_thread = threading.Thread(target=server.run, daemon=True)
    server_thread.start()

    if not _wait_for_server(host, port, server=server):
        server.should_exit = True
        server_thread.join(timeout=5)
        raise RuntimeError("ScoutBot backend did not start; check bot.log and the configured port")

    window = webview.create_window(
        "ScoutBot",
        url,
        width=1400,
        height=900,
        min_size=(900, 600),
        text_select=True,
    )

    def _on_closed():
        logger.info("ScoutBot window closed")
        server.should_exit = True

    window.events.closed += _on_closed
    def _shutdown():
        server.should_exit = True
        window.destroy()
    app.state.shutdown_callback = _shutdown
    webview.settings["OPEN_EXTERNAL_LINKS_IN_BROWSER"] = True

    try:
        webview.start(
            gui="edgechromium" if sys.platform == "win32" else None,
            private_mode=False,
            storage_path=os.path.join(data_root, "webview_data"),
        )
    finally:
        server.should_exit = True
        server_thread.join(timeout=20)


if __name__ == "__main__":
    main()
