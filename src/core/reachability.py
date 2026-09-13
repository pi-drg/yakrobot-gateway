"""Track whether each robot's control server is reachable, for the ``/`` index.

Not safety state — see ``core.ws_proxy``'s module docstring for the distinction this
repo draws between safety state (deadman, duty caps, the driver slot — stays on the
robot) and admission/reporting state (who may open a socket, or what the index says —
already this gateway's job). This module only remembers whether the last connect
attempt succeeded, which changes what a caller is told, never what a robot allows.

Complements, not replaces, ``ws_proxy``'s own reactive ``offline_until`` cache: a real
proxy connect attempt marks the result immediately (sharper than this module's poll
interval), and the periodic probe here only covers the gap where nobody has tried
lately — which matters because with video fully gated behind a paid lease (parent plan
§5.3), a buyer has no other way to tell a robot is switched off before paying for it.
"""

import asyncio
import logging
from urllib.parse import urlparse

from core.plugin import RobotPlugin

logger = logging.getLogger(__name__)

# Read as a module attribute on every loop iteration, never captured once at function
# entry, so a test can shrink it (`reachability.PROBE_INTERVAL_S = 0.2`) and see the
# effect on the very next sleep rather than needing to rebuild the probe loop.
PROBE_INTERVAL_S = 15.0

_CONNECT_TIMEOUT_S = 2.0


class Reachability:
    """In-process, per-robot online/offline cache shared across the gateway."""

    def __init__(self):
        self._online: dict[str, bool] = {}

    def mark(self, robot: str, online: bool) -> None:
        self._online[robot] = online

    def get(self, robot: str) -> bool | None:
        """True/False once observed at least once, else None (never probed)."""
        return self._online.get(robot)


def _candidates_to_host_port(urls: list[str]) -> list[tuple[str, int]]:
    out = []
    for base_url in urls:
        parts = urlparse(base_url)
        if not parts.hostname:
            continue
        port = parts.port or (443 if parts.scheme == "https" else 80)
        out.append((parts.hostname, port))
    return out


async def _probe_one(host: str, port: int) -> bool:
    try:
        _reader, writer = await asyncio.wait_for(
            asyncio.open_connection(host, port), timeout=_CONNECT_TIMEOUT_S
        )
    except (OSError, asyncio.TimeoutError):
        return False
    writer.close()
    try:
        await writer.wait_closed()
    except Exception:
        # The far end may already be gone; a probe socket closing badly is not an
        # incident worth a traceback, same reasoning as ws_proxy's own best-effort close.
        pass
    return True


async def probe_forever(plugins: dict[str, RobotPlugin], reachability: Reachability) -> None:
    """TCP-probe every drivable robot's control-server candidates, forever.

    Runs as a lifespan task for the life of the gateway; the caller cancels it on
    shutdown. Probes once immediately, then every ``PROBE_INTERVAL_S`` — a caller that
    reads the index right after startup must not see ``online: null`` for a full
    interval just because nobody has connected yet.
    """
    targets = {
        name: _candidates_to_host_port(p.control_base_urls())
        for name, p in plugins.items()
        if p.control_base_urls()
    }
    if not targets:
        return

    while True:
        for name, candidates in targets.items():
            online = False
            for host, port in candidates:
                if await _probe_one(host, port):
                    online = True
                    break
            reachability.mark(name, online)
        await asyncio.sleep(PROBE_INTERVAL_S)
