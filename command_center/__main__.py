"""Run the Command Center: python -m command_center [--host 127.0.0.1] [--port 8765] [--config path]."""
from __future__ import annotations

import argparse
from pathlib import Path

from command_center.services import load_config


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--config", type=Path, default=None)
    parser.add_argument("--host", default=None, help="default from config (127.0.0.1)")
    parser.add_argument("--port", type=int, default=None)
    args = parser.parse_args()
    cfg = load_config(args.config)
    import uvicorn

    from command_center.api import create_app
    uvicorn.run(create_app(args.config), host=args.host or cfg["bind"]["host"], port=args.port or cfg["bind"]["port"],
                workers=1, log_level="warning", access_log=False)


if __name__ == "__main__":
    main()
