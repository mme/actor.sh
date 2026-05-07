"""mDNS / zeroconf discovery for actord (Phase 4 / issue #35 stage 7+8).

Each running daemon advertises itself on the LAN as a
`_actor._tcp.local.` service and runs an `AsyncServiceBrowser` to
collect peer records. `actor servers` queries the daemon's in-memory
peer set via the `ListServers` RPC.

Phase 4 is read-only — visibility, no connections. The cert
fingerprint is advertised in the TXT record so Phase 6 can pin against
it without a separate exchange.

Failures (mDNS blocked, network down, port collision) log a warning
but never fail daemon startup; discovery is non-essential to local
operation.
"""
from __future__ import annotations

import asyncio
import getpass
import logging
import os
import socket
import time
from dataclasses import dataclass, field
from typing import Dict, Optional

from zeroconf import IPVersion
from zeroconf.asyncio import (
    AsyncServiceBrowser,
    AsyncServiceInfo,
    AsyncZeroconf,
)


log = logging.getLogger("actor.discovery")


SERVICE_TYPE = "_actor._tcp.local."

# We advertise the canonical local-listener port (2204) even though
# Phase 4 doesn't bind a TCP listener — clients learn from this record
# what *intent* a daemon has, and Phase 6 will swap the value for the
# real ephemeral inter-daemon port without reshaping the TXT keys.
ADVERTISED_PORT = 2204


@dataclass
class ServerRecord:
    """In-memory representation of a discovered (or local) daemon.
    Mirrors `pb.ServerRecord` 1:1; the wire converters live in
    `actor.wire`."""
    instance_name: str
    host: str
    port: int
    fingerprint: str
    version: str
    user: str
    pid: int
    is_self: bool = False
    last_seen: float = 0.0


def _safe_instance_name(raw: str) -> str:
    """Sanitize a string for use as the mDNS service-instance label.

    `python-zeroconf` rejects names that don't fit the local-network
    instance-name shape (no leading dots, length cap, ASCII-ish). We
    don't try to normalize aggressively — most hostnames are fine —
    but we do drop characters that would explode the registration."""
    if not raw:
        return "actord"
    cleaned = "".join(ch for ch in raw if ch.isprintable() and ch != ".")
    cleaned = cleaned.strip()
    return cleaned or "actord"


def _resolve_instance_name(
    configured: Optional[str], hostname: Optional[str] = None,
) -> str:
    """Pick the advertised instance-name. `configured` wins when set
    (settings.kdl `daemon { name "..." }`); otherwise use the system
    hostname; otherwise a literal fallback so we still register."""
    base = configured or hostname or socket.gethostname() or "actord"
    return _safe_instance_name(base)


def _build_txt(
    *,
    fingerprint: str,
    version: str,
    user: str,
    pid: int,
) -> Dict[str, str]:
    """TXT record contents. Total payload stays well under the ~1KB
    UDP-friendly cap with realistic values."""
    return {
        "version": version,
        "fingerprint": fingerprint,
        "user": user,
        "pid": str(pid),
    }


def _parse_txt(properties: Dict[bytes, Optional[bytes]]) -> Dict[str, str]:
    """Convert zeroconf's bytes-keyed TXT dict into a plain str dict.
    Unknown / missing fields become empty strings (callers should
    treat them as "couldn't decode" rather than asserting)."""
    out: Dict[str, str] = {}
    for k, v in properties.items():
        try:
            key = k.decode("utf-8")
        except UnicodeDecodeError:
            continue
        if v is None:
            out[key] = ""
            continue
        try:
            out[key] = v.decode("utf-8")
        except UnicodeDecodeError:
            out[key] = ""
    return out


def _record_from_info(info: AsyncServiceInfo) -> Optional[ServerRecord]:
    """Translate an `AsyncServiceInfo` (with addresses already
    resolved) into a `ServerRecord`. Returns None if the record is
    too incomplete to be useful."""
    if info is None:
        return None
    name = info.name or ""
    if name.endswith("." + SERVICE_TYPE) or name.endswith("." + SERVICE_TYPE.rstrip(".")):
        instance_name = name[: -(len(SERVICE_TYPE) + 1)]
    else:
        instance_name = name.rstrip(".")
    if not instance_name:
        return None
    server = info.server.rstrip(".") if info.server else instance_name + ".local"
    txt = _parse_txt(dict(info.properties or {}))
    pid_raw = txt.get("pid", "0")
    try:
        pid = int(pid_raw)
    except ValueError:
        pid = 0
    return ServerRecord(
        instance_name=instance_name,
        host=server,
        port=int(info.port or 0),
        fingerprint=txt.get("fingerprint", ""),
        version=txt.get("version", ""),
        user=txt.get("user", ""),
        pid=pid,
        is_self=False,
        last_seen=time.time(),
    )


class DiscoveryService:
    """Owns the daemon's `AsyncZeroconf` instance plus the publishing
    + browsing state. Lifetime is the daemon's lifetime — start in
    `daemon.main` after the listener is bound, stop on shutdown.

    Failures during start log a warning and leave the service in a
    "degraded" state where the public methods still work but return
    only the local self-record. This is intentional: the daemon's
    primary job is local actor management, and discovery being broken
    on the LAN must not block that."""

    def __init__(
        self,
        *,
        instance_name: str,
        fingerprint: str,
        version: str,
        pid: int,
        port: int = ADVERTISED_PORT,
        user: Optional[str] = None,
    ) -> None:
        self._instance_name = instance_name
        self._fingerprint = fingerprint
        self._version = version
        self._pid = pid
        self._port = port
        self._user = user if user is not None else _current_user()
        self._aiozc: Optional[AsyncZeroconf] = None
        self._info: Optional[AsyncServiceInfo] = None
        self._browser: Optional[AsyncServiceBrowser] = None
        self._peers: Dict[str, ServerRecord] = {}
        self._published = False
        self._fqsn: Optional[str] = None  # fully-qualified service name
        # Tracks resolve tasks so shutdown can await them cleanly
        # rather than leaving them hanging.
        self._resolve_tasks: set[asyncio.Task] = set()

    @property
    def published(self) -> bool:
        return self._published

    @property
    def instance_name(self) -> str:
        return self._instance_name

    async def start(self) -> None:
        """Bring up the AsyncZeroconf, register the service, and start
        browsing. All steps are best-effort — failures degrade to
        "self only"."""
        try:
            self._aiozc = AsyncZeroconf(ip_version=IPVersion.V4Only)
        except Exception as e:
            log.warning("zeroconf init failed; discovery disabled: %s", e)
            self._aiozc = None
            return

        await self._publish()
        await self._start_browser()

    async def _publish(self) -> None:
        assert self._aiozc is not None
        fqsn = f"{self._instance_name}.{SERVICE_TYPE}"
        local_host = socket.gethostname()
        if not local_host.endswith(".local"):
            host_for_record = f"{local_host}.local."
        else:
            host_for_record = f"{local_host}."

        info = AsyncServiceInfo(
            type_=SERVICE_TYPE,
            name=fqsn,
            port=self._port,
            properties=_build_txt(
                fingerprint=self._fingerprint,
                version=self._version,
                user=self._user,
                pid=self._pid,
            ),
            server=host_for_record,
        )
        try:
            await self._aiozc.async_register_service(info, allow_name_change=True)
        except Exception as e:
            log.warning("zeroconf publish failed: %s", e)
            return
        self._info = info
        self._published = True
        # `allow_name_change=True` may have appended a suffix like
        # " (2)"; re-read the canonical name from the registered
        # info so we filter ourselves out of the browser correctly.
        self._fqsn = info.name
        canonical = info.name
        if canonical.endswith("." + SERVICE_TYPE.rstrip(".") + "."):
            self._instance_name = canonical[: -(len(SERVICE_TYPE) + 1)]
        elif canonical.endswith(SERVICE_TYPE):
            self._instance_name = canonical[: -(len(SERVICE_TYPE) + 1)]
        log.info(
            "zeroconf: published %s on port %d", canonical, self._port,
        )

    async def _start_browser(self) -> None:
        assert self._aiozc is not None
        try:
            self._browser = AsyncServiceBrowser(
                self._aiozc.zeroconf,
                SERVICE_TYPE,
                handlers=[self._on_state_change],
            )
        except Exception as e:
            log.warning("zeroconf browse failed: %s", e)
            self._browser = None

    def _on_state_change(
        self, *,
        zeroconf,
        service_type,
        name,
        state_change,
    ) -> None:
        """Synchronous callback dispatched by zeroconf (keyword-only
        per the upstream Signal contract). We just kick off an async
        resolve task; the resolved record gets stored once we have the
        addresses + TXT."""
        from zeroconf import ServiceStateChange  # local import: cheap

        if state_change is ServiceStateChange.Removed:
            self._remove_record_by_name(name)
            return
        if state_change in (ServiceStateChange.Added, ServiceStateChange.Updated):
            try:
                loop = asyncio.get_running_loop()
            except RuntimeError:
                # Browser fired outside the loop (shouldn't happen
                # for AsyncServiceBrowser, but defensive).
                return
            task = loop.create_task(self._resolve(name))
            self._resolve_tasks.add(task)
            task.add_done_callback(self._resolve_tasks.discard)

    async def _resolve(self, name: str) -> None:
        if self._aiozc is None:
            return
        info = AsyncServiceInfo(SERVICE_TYPE, name)
        try:
            ok = await info.async_request(self._aiozc.zeroconf, 3000)
        except Exception as e:
            log.debug("zeroconf resolve failed for %s: %s", name, e)
            return
        if not ok:
            return
        record = _record_from_info(info)
        if record is None:
            return
        # Skip our own record — we always include `self` in `snapshot`
        # via the synthetic local entry.
        if self._fqsn is not None and name == self._fqsn:
            return
        self._peers[record.instance_name] = record

    def _remove_record_by_name(self, fqsn: str) -> None:
        # Strip trailing service-type from the fully-qualified name.
        if fqsn.endswith("." + SERVICE_TYPE.rstrip(".") + "."):
            instance = fqsn[: -(len(SERVICE_TYPE) + 1)]
        elif fqsn.endswith(SERVICE_TYPE):
            instance = fqsn[: -(len(SERVICE_TYPE) + 1)]
        else:
            instance = fqsn.rstrip(".")
        self._peers.pop(instance, None)

    def snapshot(self) -> list[ServerRecord]:
        """Return the discovered peer set plus the local self-record.

        Self-first, then peers alphabetical-by-instance — same order
        the CLI renders. Callers shouldn't rely on rendering order
        from the daemon, but determinism makes tests easier."""
        out: list[ServerRecord] = []
        out.append(self._self_record())
        peers = [v for v in self._peers.values()]
        peers.sort(key=lambda r: r.instance_name)
        out.extend(peers)
        return out

    def _self_record(self) -> ServerRecord:
        local_host = socket.gethostname()
        host = local_host if local_host.endswith(".local") else f"{local_host}.local"
        return ServerRecord(
            instance_name=self._instance_name,
            host=host,
            port=self._port,
            fingerprint=self._fingerprint,
            version=self._version,
            user=self._user,
            pid=self._pid,
            is_self=True,
            last_seen=0.0,
        )

    async def stop(self) -> None:
        """Unregister + tear down. Idempotent."""
        # Cancel pending resolves so they don't fight with shutdown.
        for t in list(self._resolve_tasks):
            t.cancel()
        for t in list(self._resolve_tasks):
            try:
                await t
            except (asyncio.CancelledError, Exception):
                pass
        self._resolve_tasks.clear()

        if self._browser is not None:
            try:
                await self._browser.async_cancel()
            except Exception as e:
                log.debug("zeroconf browser cancel failed: %s", e)
            self._browser = None

        if self._aiozc is not None:
            try:
                if self._info is not None:
                    await self._aiozc.async_unregister_service(self._info)
            except Exception as e:
                log.debug("zeroconf unregister failed: %s", e)
            try:
                await self._aiozc.async_close()
            except Exception as e:
                log.debug("zeroconf close failed: %s", e)
            self._aiozc = None
            self._info = None
            self._published = False


def _current_user() -> str:
    try:
        return getpass.getuser()
    except Exception:
        return os.environ.get("USER") or os.environ.get("LOGNAME") or "unknown"


__all__ = [
    "ADVERTISED_PORT",
    "DiscoveryService",
    "SERVICE_TYPE",
    "ServerRecord",
]
