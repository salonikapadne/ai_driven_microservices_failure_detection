"""
Minimal Flask demo — intentional bug for code-heal prototype.
EXPECTED_MAGIC must be 42 for /health to return 200; seed uses 41.
Logs include [code_heal] on failure so the classifier maps to code_heal.
"""
import os
import threading
import time
import urllib.error
import urllib.request

from flask import Flask

# BUG: should be 42 — agent should set this to 42 in live/app.py
EXPECTED_MAGIC = 41

app = Flask(__name__)


@app.route("/health")
def health():
    if EXPECTED_MAGIC != 42:
        msg = (
            f"[code_heal] health failed: EXPECTED_MAGIC={EXPECTED_MAGIC} "
            f"but service contract requires 42"
        )
        print(msg, flush=True)
        return {"status": "unhealthy", "magic": EXPECTED_MAGIC}, 500
    return {"status": "ok"}, 200


@app.route("/")
def root():
    return {"service": "buggy-service", "hint": "fix EXPECTED_MAGIC in app.py"}, 200


def _probe_health_loop():
    """Hit /health periodically so failing magic emits [code_heal] lines to docker logs."""
    port = int(os.environ.get("PORT", "3010"))
    time.sleep(5)
    while True:
        try:
            urllib.request.urlopen(f"http://127.0.0.1:{port}/health", timeout=3)
        except (urllib.error.URLError, OSError):
            pass
        time.sleep(30)


if __name__ == "__main__":
    port = int(os.environ.get("PORT", "3010"))
    threading.Thread(target=_probe_health_loop, daemon=True).start()
    app.run(host="0.0.0.0", port=port, threaded=True)
