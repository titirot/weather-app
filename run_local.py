"""Run the local app with settings from .env and a Windows-compatible server."""

import os
from pathlib import Path

from waitress import serve


def main():
    root = Path(__file__).resolve().parent
    for line in (root / ".env").read_text(encoding="utf-8-sig").splitlines():
        line = line.strip()
        if not line or line.startswith("#"):
            continue
        key, separator, value = line.partition("=")
        if not separator or not key.strip():
            raise ValueError("Invalid .env entry; expected NAME=value.")
        os.environ[key.strip()] = value.strip()

    from app import app

    print("Weather app: http://127.0.0.1:8765/ (Ctrl+C to stop)", flush=True)
    serve(app, host="127.0.0.1", port=8765)


if __name__ == "__main__":
    main()
