"""Launch the AERUPT React UI (backend + built client).

    py run_ui.py [--port 8000] [--host 127.0.0.1]

Requires the client to be built once:
    cd ui/client && npm install && npm run build
"""
from __future__ import annotations

import argparse
import sys

import uvicorn

from aerupt.config import load_env

if __name__ == "__main__":
    load_env()
    ap = argparse.ArgumentParser()
    ap.add_argument("--host", default="127.0.0.1")
    ap.add_argument("--port", type=int, default=8000)
    ap.add_argument("--reload", action="store_true")
    args = ap.parse_args()

    sys.path.insert(0, ".")
    print(f"AERUPT UI -> http://{args.host}:{args.port}")
    uvicorn.run("ui.server.main:app",
                host=args.host, port=args.port, reload=args.reload)