"""Run the gateway: API and metrics on separate ports, so the firewall can restrict metrics."""

from __future__ import annotations

import argparse
import asyncio
import logging
import sys

import uvicorn

from .app import create_app
from .config import ConfigError, load


def main() -> None:
    ap = argparse.ArgumentParser(prog="llm-gateway")
    ap.add_argument("--config", default="/etc/llm-gateway/config.yml")
    ap.add_argument("--host", default="0.0.0.0")
    ap.add_argument("--port", type=int, default=8000)
    ap.add_argument("--metrics-port", type=int, default=9400)
    ap.add_argument("--check", action="store_true", help="validate the configuration and exit")
    args = ap.parse_args()
    logging.basicConfig(level=logging.INFO, format="%(message)s", stream=sys.stdout)
    logging.getLogger("httpx").setLevel(logging.WARNING)  # one access line per request is enough
    try:
        settings = load(args.config)
    except ConfigError as e:
        print(f"llm-gateway: invalid configuration: {e}", file=sys.stderr)
        sys.exit(2)
    if args.check:
        print(f"ok: {len(settings.backends)} backends, {len(settings.teams)} teams, models {list(settings.models)}")
        return
    api, metrics_app, _ = create_app(settings)

    async def serve() -> None:
        servers = [
            uvicorn.Server(uvicorn.Config(api, host=args.host, port=args.port, access_log=False,
                                          log_level="warning", timeout_graceful_shutdown=20)),
            uvicorn.Server(uvicorn.Config(metrics_app, host=args.host, port=args.metrics_port,
                                          access_log=False, log_level="warning")),
        ]
        await asyncio.gather(*(s.serve() for s in servers))

    asyncio.run(serve())


if __name__ == "__main__":
    main()
