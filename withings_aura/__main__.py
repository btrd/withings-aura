"""Run the Withings Aura server: ``python -m withings_aura --aura-host 192.168.1.50``."""

from __future__ import annotations

import argparse
import asyncio
import logging
import os
import signal
from pathlib import Path

from aiohttp import web

from .const import (
    DEFAULT_POLL_INTERVAL,
    DEFAULT_PORT,
    DEFAULT_SLOW_POLL_INTERVAL,
    MIN_POLL_INTERVAL,
    MIN_SLOW_POLL_INTERVAL,
)
from .controller import AuraController
from .device import AuraDevice
from .exceptions import AuraError
from .radios import RadioCatalog
from .server import create_app
from .transport import AuraBridge
from .util import resolve_timezone

DEFAULT_LISTEN = "127.0.0.1"
DEFAULT_HTTP_PORT = 8686
DEFAULT_RADIOS_FILE = Path.home() / ".withings-aura" / "radios.json"
_LOGGER = logging.getLogger("withings_aura")


def parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(prog="withings_aura", description=__doc__)
    parser.add_argument(
        "--aura-host", default=os.environ.get("AURA_HOST"), help="Aura IP or hostname"
    )
    parser.add_argument("--aura-port", type=int, default=DEFAULT_PORT)
    parser.add_argument(
        "--listen",
        default=DEFAULT_LISTEN,
        help="address to serve on; the API has no authentication, so use 0.0.0.0 only on a trusted LAN",
    )
    parser.add_argument("--port", type=int, default=DEFAULT_HTTP_PORT, help="HTTP port")
    parser.add_argument("--poll-interval", type=int, default=DEFAULT_POLL_INTERVAL)
    parser.add_argument("--slow-poll-interval", type=int, default=DEFAULT_SLOW_POLL_INTERVAL)
    parser.add_argument(
        "--radios",
        type=Path,
        default=DEFAULT_RADIOS_FILE,
        help="web-radio catalogue (JSON id -> name); learned from your alarms, editable by hand",
    )
    parser.add_argument("--timezone", help="IANA zone sent on time sync (default: system zone)")
    parser.add_argument("-v", "--verbose", action="store_true")
    args = parser.parse_args(argv)
    if not args.aura_host:
        parser.error("--aura-host (or AURA_HOST) is required")
    if args.poll_interval < MIN_POLL_INTERVAL:
        parser.error(f"--poll-interval must be at least {MIN_POLL_INTERVAL}")
    if args.slow_poll_interval < MIN_SLOW_POLL_INTERVAL:
        parser.error(f"--slow-poll-interval must be at least {MIN_SLOW_POLL_INTERVAL}")
    return args


async def serve(args: argparse.Namespace) -> None:
    bridge = AuraBridge(args.aura_host, args.aura_port)
    controller = AuraController(
        AuraDevice(bridge),
        poll_interval=args.poll_interval,
        slow_poll_interval=args.slow_poll_interval,
    )
    await controller.start()  # fail fast if the Aura is unreachable
    catalog = RadioCatalog(args.radios)
    runner = web.AppRunner(create_app(controller, resolve_timezone(args.timezone), catalog))
    await runner.setup()
    await web.TCPSite(runner, args.listen, args.port).start()
    _LOGGER.info("Serving on http://%s:%s (Aura at %s)", args.listen, args.port, args.aura_host)

    stop = asyncio.Event()
    for sig in (signal.SIGINT, signal.SIGTERM):
        asyncio.get_running_loop().add_signal_handler(sig, stop.set)
    await stop.wait()
    await runner.cleanup()
    await controller.stop()


def main(argv: list[str] | None = None) -> int:
    args = parse_args(argv)
    logging.basicConfig(
        level=logging.DEBUG if args.verbose else logging.INFO,
        format="%(asctime)s %(levelname)s %(name)s: %(message)s",
    )
    try:
        asyncio.run(serve(args))
    except (AuraError, ValueError) as err:
        _LOGGER.error("Cannot start: %s", err)
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
