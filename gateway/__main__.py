"""Run the gateway agent: ``python -m gateway [--cache-dir DIR] [--fake-output DIR]``.

Configuration comes from the environment (``gateway.config``): ``CORE_URL``,
``GATEWAY_TOKEN`` and ``GATEWAY_DRIVER``. The fake driver reads its tags from the file
named by ``GATEWAY_FAKE_TAGS``.
"""

from __future__ import annotations

import argparse
import asyncio
import logging
import sys
from pathlib import Path

from gateway.agent import Agent, CoreClient, RenderCache, make_http_client
from gateway.config import GatewayConfigError, GatewaySettings, load_gateway_settings
from gateway.drivers.base import TagDriver


def build_driver(settings: GatewaySettings, fake_output: Path) -> TagDriver:
    if settings.gateway_driver == "fake":
        from gateway.drivers.fake import FakeDriver

        return FakeDriver(output_dir=fake_output)
    raise GatewayConfigError("invalid configuration: GATEWAY_DRIVER: unknown driver")


async def _main(args: argparse.Namespace) -> None:
    settings = load_gateway_settings()
    driver = build_driver(settings, args.fake_output)
    stop = asyncio.Event()
    async with make_http_client(settings) as http:
        agent = Agent(CoreClient(http), driver, RenderCache(args.cache_dir))
        try:
            await agent.run(stop)
        finally:
            stop.set()


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="python -m gateway", description=__doc__)
    parser.add_argument("--cache-dir", type=Path, default=Path("gateway_cache"))
    parser.add_argument("--fake-output", type=Path, default=Path("fake_tags"))
    args = parser.parse_args(argv)
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s %(message)s")
    # httpx logs every request URL at INFO; keep the log about the agent.
    logging.getLogger("httpx").setLevel(logging.WARNING)
    try:
        asyncio.run(_main(args))
    except GatewayConfigError as exc:
        print(exc, file=sys.stderr)
        return 2
    except KeyboardInterrupt:
        return 0
    return 0


if __name__ == "__main__":
    sys.exit(main())
