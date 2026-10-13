"""Serve this repository locally and open the judge-verdict viewer.

Browsers do not let a page opened from disk read neighbouring JSON files, so the viewer
needs a small local server. Run from anywhere:

    python viewer/serve.py            # http://127.0.0.1:8000/viewer/
    python viewer/serve.py --port 0   # any free port
"""
import argparse
import functools
import http.server
import threading
import webbrowser
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]


class QuietHandler(http.server.SimpleHTTPRequestHandler):
    def log_message(self, fmt, *args):  # keep the terminal readable
        pass


def main():
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument('--port', type=int, default=8000)
    ap.add_argument('--no-browser', action='store_true', help='do not open a browser window')
    args = ap.parse_args()
    handler = functools.partial(QuietHandler, directory=str(ROOT))
    try:
        server = http.server.ThreadingHTTPServer(('127.0.0.1', args.port), handler)
    except OSError:
        server = http.server.ThreadingHTTPServer(('127.0.0.1', 0), handler)  # port taken: use any free one
    url = f'http://127.0.0.1:{server.server_address[1]}/viewer/'
    print(f'Into the Unknown Leaderboard: {url}  (Ctrl+C to stop)', flush=True)
    if not args.no_browser:
        threading.Timer(0.4, webbrowser.open, [url]).start()
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        pass
    finally:
        server.server_close()


if __name__ == '__main__':
    main()
