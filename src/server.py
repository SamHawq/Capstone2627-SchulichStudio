"""Start the local web UI and REST API.

Usage (from the repo root, with the venv active):
    python src/server.py                 # http://127.0.0.1:8000  (API docs at /docs)
    python src/server.py --port 8080
    python src/server.py --reload        # restart automatically when code under src/ changes

The web pages are read from disk on every load, but Python code only changes when the server
restarts: after pulling or editing code, restart it (or run with --reload), or a new page may
talk to an old API.

Binds to 127.0.0.1 only: there is no login and the pages show real candidate IDs, so don't
expose it on a network. Needs OPENAI_API_KEY in the shell, .env, or environment_vars.env. Logging uses
LOG_FILE_LEVEL / LOG_CONSOLE_LEVEL as for the CLI.
"""

import argparse
import webbrowser
from pathlib import Path

import uvicorn
from dotenv import load_dotenv

from hiring_agents.api import create_app
from hiring_agents.logging_config import setup_logging

REPO = Path(__file__).resolve().parents[1]
load_dotenv(REPO / ".env")
load_dotenv(REPO / "environment_vars.env")


def main() -> None:
    parser = argparse.ArgumentParser(description="Run the local hiring UI and API.")
    parser.add_argument("--port", type=int, default=8000)
    parser.add_argument("--no-open", action="store_true", help="don't open the browser")
    parser.add_argument("--reload", action="store_true",
                        help="restart automatically when code under src/ changes (for development)")
    args = parser.parse_args()

    log_path = setup_logging(REPO / "logs")
    if log_path:
        print(f"Log: {log_path}")
    url = f"http://127.0.0.1:{args.port}"
    print(f"Open {url}  (API docs: {url}/docs). Ctrl+C to stop.")
    if not args.no_open:
        webbrowser.open(url)
    if args.reload:
        # Reload needs the app as an import string; the factory builds it in the worker process.
        uvicorn.run("hiring_agents.api:create_app", factory=True, reload=True,
                    reload_dirs=[str(REPO / "src")], host="127.0.0.1", port=args.port, log_level="warning")
    else:
        uvicorn.run(create_app(), host="127.0.0.1", port=args.port, log_level="warning")


if __name__ == "__main__":
    main()
