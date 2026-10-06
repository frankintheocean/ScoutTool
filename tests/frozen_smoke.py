"""Headless entry for testing the frozen backend and bundled static files.
This test entry is not the desktop application's launcher.
"""
import argparse
import uvicorn
from main import app

if __name__ == '__main__':
    parser = argparse.ArgumentParser()
    parser.add_argument('--port', type=int, required=True)
    arguments = parser.parse_args()
    uvicorn.run(app, host='127.0.0.1', port=arguments.port, log_config=None)
