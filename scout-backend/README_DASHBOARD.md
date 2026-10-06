# Dashboard Scout backend

The Dashboard starts the Scout backend automatically from `main.py` on `http://127.0.0.1:8877`. The backend contains the Twitch discovery, tracking and watchlist functionality used by the Dashboard.

Run it manually from this directory with `python -m uvicorn main:app --host 127.0.0.1 --port 8877`.
