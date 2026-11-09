"""Run the Command Center: python -m command_center [--host 127.0.0.1] [--port 8765] [--config path].

Refuses to start if the port is already in use (another instance) and refuses a non-loopback bind unless an access
token is configured (KRONOS_CC_TOKEN) and --allow-remote-bind is given. V1 is meant for 127.0.0.1 only; a future VPS
deployment keeps it on 127.0.0.1 behind an authenticating HTTPS reverse proxy (docs/command-center.md section 5).
"""
from __future__ import annotations

import argparse
import ipaddress
import os
import socket
import sys
import threading
import time
from pathlib import Path

# Minimal resources: the calendar library pulls in numpy; one BLAS thread is plenty for a read-only dashboard.
for _var in ("OPENBLAS_NUM_THREADS", "OMP_NUM_THREADS", "MKL_NUM_THREADS"):
    os.environ.setdefault(_var, "1")

from command_center.services import load_config  # noqa: E402

EXIT_PORT_IN_USE = 3
EXIT_UNSAFE_BIND = 4
# The dashboard's own runtime folder (never a runner's state directory): pid file, stop request, logs.
RUNTIME_DIR = Path(os.environ.get("LOCALAPPDATA", Path.home())) / "KronosCommandCenter"


def _watch_stop_file(server, stop_file: Path) -> None:
    """Graceful shutdown on request: the launcher creates `stop_file`; uvicorn then finishes in-flight requests
    and exits. (There is deliberately no HTTP shutdown endpoint - the API is GET-only.)"""
    while not server.should_exit:
        if stop_file.exists():
            server.should_exit = True
            break
        time.sleep(1.0)


def _is_loopback(host: str) -> bool:
    if host == "localhost":
        return True
    try:
        return ipaddress.ip_address(host).is_loopback
    except ValueError:
        return False


def _port_free(host: str, port: int) -> bool:
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as s:
        s.settimeout(0.5)
        return s.connect_ex((host if host != "0.0.0.0" else "127.0.0.1", port)) != 0


def _prewarm() -> None:
    """Build the (cached) holiday calendar in the background so the first Overview request is fast."""
    try:
        from datetime import datetime, timezone

        from command_center import decisions, services
        decisions.xau_calendar(datetime.now(timezone.utc).date(), services.CACHE)
    except Exception:  # noqa: BLE001 - the request path reports any problem itself
        pass


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--config", type=Path, default=None)
    parser.add_argument("--host", default=None, help="default from config (127.0.0.1)")
    parser.add_argument("--port", type=int, default=None)
    parser.add_argument("--allow-remote-bind", action="store_true", help="only with KRONOS_CC_TOKEN set")
    parser.add_argument("--runtime-dir", type=Path, default=RUNTIME_DIR, help="pid file and stop-request file")
    args = parser.parse_args()
    cfg = load_config(args.config)
    host = args.host or cfg["bind"]["host"]
    port = args.port or cfg["bind"]["port"]
    token_env = cfg.get("security", {}).get("token_env", "KRONOS_CC_TOKEN")
    if not _is_loopback(host) and not (args.allow_remote_bind and os.environ.get(token_env)):
        print(f"Refusing to bind {host}: the Command Center is local-only unless {token_env} is set and "
              "--allow-remote-bind is given (and it should sit behind an HTTPS reverse proxy).", file=sys.stderr)
        raise SystemExit(EXIT_UNSAFE_BIND)
    if not _port_free(host, port):
        print(f"Port {port} is already in use (is the Command Center already running?). Not starting a second "
              "instance.", file=sys.stderr)
        raise SystemExit(EXIT_PORT_IN_USE)
    import uvicorn

    from command_center.api import create_app
    runtime = Path(args.runtime_dir)
    runtime.mkdir(parents=True, exist_ok=True)
    stop_file, pid_file = runtime / "stop.request", runtime / "server.pid"
    stop_file.unlink(missing_ok=True)
    config = uvicorn.Config(create_app(args.config), host=host, port=port, workers=1, log_level="warning",
                            access_log=False, server_header=False, date_header=False)
    server = uvicorn.Server(config)
    threading.Thread(target=_watch_stop_file, args=(server, stop_file), daemon=True).start()
    threading.Thread(target=_prewarm, daemon=True).start()
    pid_file.write_text(f"{os.getpid()} {host} {port}", encoding="utf-8")
    print(f"Kronos Command Center (read-only) on http://{host}:{port} (pid {os.getpid()})", flush=True)
    try:
        server.run()
    finally:
        pid_file.unlink(missing_ok=True)
        stop_file.unlink(missing_ok=True)
        print("Kronos Command Center stopped.", flush=True)


if __name__ == "__main__":
    main()
