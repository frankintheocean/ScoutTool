import os
import tempfile
import sys

from dotenv import load_dotenv, set_key

# BUGFIX: previously anchored to this file's own directory
# (os.path.dirname(os.path.abspath(__file__))), which is correct when
# running from source but not for the packaged build: a frozen
# PyInstaller exe's __file__ resolves inside its bundled/extracted
# location (sys._MEIPASS / the onedir "_internal" folder), NOT
# necessarily the same real, stable folder on every single launch in
# every environment (e.g. antivirus quarantine-and-restore, some
# onefile-style temp extraction, or a bundle folder that gets touched
# by other tooling) - so a Client ID/Secret saved via
# save_twitch_credentials() below could be written to one physical
# location while the *next* launch's load_dotenv() read from a
# different one, making the saved credentials silently vanish on
# restart even though the save itself reported success. sys.executable
# is the actual running .exe's path, which is always the same real
# on-disk file across launches - anchoring here instead guarantees the
# read and every write always agree, matching how streamers.db's own
# location is resolved (see database.py). Running from source (no
# frozen exe) keeps the previous, already-correct __file__-based path.
# Dashboard supplies DASHBOARD_SCOUT_DATA_DIR for packaged launches. This
# points at Electron's stable per-user data directory, which survives an
# installer update. Direct/manual source launches keep the legacy local path.
_data_dir = os.getenv("DASHBOARD_SCOUT_DATA_DIR", "").strip()
if _data_dir:
    _BASE_DIR = os.path.abspath(_data_dir)
elif getattr(sys, "frozen", False):
    _BASE_DIR = os.path.dirname(os.path.abspath(sys.executable))
else:
    _BASE_DIR = os.path.dirname(os.path.abspath(__file__))

ENV_PATH = os.path.join(_BASE_DIR, ".env")

load_dotenv(ENV_PATH)

# Twitch credentials are no longer required to be pre-set in .env: if
# they're missing, the app now starts anyway and the frontend shows a
# one-time setup screen (see /api/settings/twitch in main.py) so the
# person can paste them in from the browser instead of hand-editing a
# file. Blank values here just mean Twitch calls fail until configured -
# twitch_api.py already handles a failed/empty auth response without
# crashing (logs an error, returns no data).
TWITCH_CLIENT_ID = os.getenv("TWITCH_CLIENT_ID", "")
TWITCH_CLIENT_SECRET = os.getenv("TWITCH_CLIENT_SECRET", "")

# Local web UI port
WEB_PORT = int(os.getenv("WEB_PORT", "8877"))
WEB_HOST = os.getenv("WEB_HOST", "127.0.0.1")

# Optional HTTP Basic Auth. Off by default (both blank) since the app is
# usually only reachable on localhost — but if WEB_HOST is changed to
# something other than 127.0.0.1/localhost (i.e. the app is reachable from
# other machines), set both of these so the app isn't wide-open read/write
# to anyone who can reach the port. When both are set, every /api/* route
# and the frontend itself require this username/password.
WEB_USERNAME = os.getenv("WEB_USERNAME", "")
WEB_PASSWORD = os.getenv("WEB_PASSWORD", "")

# Local-launch auth token. Set by Dashboard's Electron main process on this
# service's own process environment at spawn time (never written to .env,
# never persisted to disk) — see electron/main/localServiceAuth.ts. Distinct
# from WEB_USERNAME/WEB_PASSWORD above: this isn't something a person sets,
# it's a fresh random secret generated every app launch specifically to stop
# other local processes on the same machine from reaching this
# 127.0.0.1-bound service with no credential at all. Left unset (and the
# check skipped — see auth.LocalServiceTokenMiddleware) when this module is
# run outside the Dashboard app, e.g. `npm run scout:backend` directly.
LOCAL_AUTH_TOKEN = os.getenv("DASHBOARD_LOCAL_TOKEN", "")


def is_configured():
    return bool(TWITCH_CLIENT_ID and TWITCH_CLIENT_SECRET)


def auth_enabled():
    return bool(WEB_USERNAME and WEB_PASSWORD)


def save_twitch_credentials(client_id, client_secret):
    """Writes TWITCH_CLIENT_ID/SECRET to .env (creating it if needed) and
    updates the in-memory values in this module and in twitch_api, so the
    change takes effect immediately without restarting the server."""
    global TWITCH_CLIENT_ID, TWITCH_CLIENT_SECRET

    client_id = (client_id or "").strip()
    client_secret = (client_secret or "").strip()

    parent = os.path.dirname(os.path.abspath(ENV_PATH))
    os.makedirs(parent, exist_ok=True)
    fd, staged = tempfile.mkstemp(prefix=".credentials-", dir=parent)
    try:
        with os.fdopen(fd, "wb") as out:
            if os.path.exists(ENV_PATH):
                with open(ENV_PATH, "rb") as source:
                    out.write(source.read())
        set_key(staged, "TWITCH_CLIENT_ID", client_id)
        set_key(staged, "TWITCH_CLIENT_SECRET", client_secret)
        os.replace(staged, ENV_PATH)
    finally:
        if os.path.exists(staged):
            os.unlink(staged)

    TWITCH_CLIENT_ID = client_id
    TWITCH_CLIENT_SECRET = client_secret

    import twitch_api
    twitch_api.set_credentials(client_id, client_secret)
