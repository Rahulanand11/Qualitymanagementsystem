"""Start the dashboard/API and the background worker together (simple single-machine deployment).

    python run.py            # web on :8000 + worker
    python run.py web        # web only
    python run.py worker     # worker only
"""
import sys
import threading

import uvicorn


def web():
    uvicorn.run("agency.api.app:get_app", factory=True, host="0.0.0.0", port=8000, proxy_headers=True)


def worker():
    from agency.worker import main

    main()


if __name__ == "__main__":
    mode = sys.argv[1] if len(sys.argv) > 1 else "all"
    from agency.db import init_db

    init_db()  # once, before the web and worker threads start
    if mode == "web":
        web()
    elif mode == "worker":
        worker()
    else:
        threading.Thread(target=worker, daemon=True).start()
        web()
