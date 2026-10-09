"""Blocking wrapper around the ``jura_connect`` library.

Every public method of :class:`JuraWifiClient` performs synchronous socket I/O
and has to run in an executor thread, never in the event loop.
"""

from __future__ import annotations

from collections.abc import Callable, Iterator, Mapping
import contextlib
import dataclasses
import ipaddress
import logging
import socket
import threading
import time

import ifaddr
from jura_connect import (
    HandshakeError,
    JuraClient,
    MachineProfile,
    PairingTimeout,
    discover,
    list_profile_codes,
    load_profile,
    lookup_by_article_number,
    probe,
)

from .const import DISCOVERY_TIMEOUT, SESSION_GAP_SECONDS

_LOGGER = logging.getLogger(__name__)

CONNECT_TIMEOUT = 5.0
HANDSHAKE_TIMEOUT = 15.0
READ_TIMEOUT = 10.0
PAIRING_TIMEOUT = 60.0

# The maintenance percent bank reports 0xFF for indicators a machine lacks.
PERCENT_NOT_REPORTED = 0xFF


class JuraWifiError(Exception):
    """Base class for errors raised by this module."""


class JuraWifiConnectionError(JuraWifiError):
    """The machine did not answer (switched off, asleep or busy)."""


class JuraWifiAuthError(JuraWifiError):
    """The machine refused the stored credentials or the PIN."""

    def __init__(self, reason: str) -> None:
        """Initialize with the handshake state reported by the machine."""
        super().__init__(reason)
        self.reason = reason


class JuraWifiPairingTimeout(JuraWifiError):
    """The connect prompt on the machine was not confirmed in time."""


@dataclasses.dataclass(frozen=True, slots=True)
class MachineSnapshot:
    """Result of one successful poll."""

    active_alerts: frozenset[str]
    errors: frozenset[str]
    blocked_products: frozenset[str]
    maintenance_counters: Mapping[str, int]
    maintenance_percent: Mapping[str, int]
    total_brews: int | None
    product_counts: Mapping[str, int]


@dataclasses.dataclass(frozen=True, slots=True)
class MachineIdentity:
    """What the discovery reply of the dongle says about its machine.

    ``ef_code`` and ``model_name`` are ``None`` when the article number is not
    in the catalogue of the J.O.E. app that ships with the library, or when the
    library has no profile for that machine type.
    """

    article_number: int
    firmware: str
    ef_code: str | None
    model_name: str | None


def _broadcast_targets(host_ip: str) -> list[str]:
    """Return the broadcast addresses to scan: global, every local network, host /24."""
    targets = {"255.255.255.255"}
    try:
        for adapter in ifaddr.get_adapters():
            for address in adapter.ips:
                ip = address.ip
                if not address.is_IPv4 or not isinstance(ip, str):
                    continue
                if ip.startswith(("127.", "169.254.")):
                    continue
                network = ipaddress.IPv4Network(
                    f"{ip}/{address.network_prefix}", strict=False
                )
                if network.prefixlen < 31:
                    targets.add(str(network.broadcast_address))
    except OSError as err:
        _LOGGER.debug("Could not list the network adapters: %s", err)
    with contextlib.suppress(ValueError):
        targets.add(
            str(ipaddress.IPv4Network(f"{host_ip}/24", strict=False).broadcast_address)
        )
    return sorted(targets)


def discover_machine(
    host: str, timeout: float = DISCOVERY_TIMEOUT
) -> MachineIdentity | None:
    """Read the machine identity from the UDP discovery reply of the dongle.

    This is how the J.O.E. app learns the article number. It only works when
    Home Assistant is in the same network as the dongle, because the dongle
    answers broadcasts and broadcasts do not cross routers. Returns ``None`` if
    nothing answered. Blocking.
    """
    try:
        host_ip = socket.gethostbyname(host)
    except OSError:
        return None
    machine = None
    try:
        machine = probe(host_ip, timeout=1.5)
        if machine is None:
            for found in discover(timeout=timeout, targets=_broadcast_targets(host_ip)):
                if found.address == host_ip:
                    machine = found
                    break
    except OSError as err:
        _LOGGER.debug("UDP discovery of %s failed: %s", host, err)
        return None
    if machine is None:
        return None
    entry = lookup_by_article_number(machine.article_number)
    known = entry is not None and entry.ef_code in set(list_profile_codes())
    return MachineIdentity(
        article_number=machine.article_number,
        firmware=machine.fw,
        ef_code=entry.ef_code if entry is not None and known else None,
        model_name=entry.friendly_name if entry is not None and known else None,
    )


class _SessionGate:
    """Serialize the sessions with one dongle and keep a pause between them.

    The dongle serves a single TCP session at a time and rejects one that is
    opened right after another was closed. Home Assistant creates several client
    objects for the same dongle (polling, config flow, reloads), so the gate is
    shared per address instead of living in a client.
    """

    def __init__(self) -> None:
        """Initialize an open gate."""
        self._lock = threading.Lock()
        self._last_end: float | None = None

    @contextlib.contextmanager
    def session(self) -> Iterator[None]:
        """Hold the gate for the duration of one session."""
        with self._lock:
            if self._last_end is not None:
                wait = SESSION_GAP_SECONDS - (time.monotonic() - self._last_end)
                if wait > 0:
                    _LOGGER.debug("Waiting %.1fs before opening the next session", wait)
                    time.sleep(wait)
            try:
                yield
            finally:
                self._last_end = time.monotonic()


_GATES: dict[tuple[str, int], _SessionGate] = {}
_GATES_LOCK = threading.Lock()


def _gate_for(host: str, port: int) -> _SessionGate:
    with _GATES_LOCK:
        return _GATES.setdefault((host.lower(), port), _SessionGate())


class JuraWifiClient:
    """Synchronous access to one JURA machine."""

    def __init__(
        self,
        host: str,
        port: int,
        conn_id: str,
        auth_hash: str,
        pin: str,
        machine_type: str,
    ) -> None:
        """Initialize the client; nothing is sent until a method is called."""
        self.host = host
        self.port = port
        self._conn_id = conn_id
        self._auth_hash = auth_hash
        self._pin = pin
        self._machine_type = machine_type
        self._profile: MachineProfile | None = None
        self._gate = _gate_for(host, port)
        self._pairing: JuraClient | None = None
        self._cancelled = threading.Event()

    @property
    def profile(self) -> MachineProfile:
        """Return the machine profile; the first access reads from disk."""
        if self._profile is None:
            try:
                self._profile = load_profile(self._machine_type)
            except KeyError as err:
                raise JuraWifiError(
                    f"unknown machine type {self._machine_type}"
                ) from err
        return self._profile

    def _new_client(self, auth_hash: str, *, with_profile: bool = True) -> JuraClient:
        return JuraClient(
            self.host,
            self.port,
            pin=self._pin,
            conn_id=self._conn_id,
            auth_hash=auth_hash,
            connect_timeout=CONNECT_TIMEOUT,
            read_timeout=READ_TIMEOUT,
            profile=self.profile if with_profile else None,
        )

    def _open(self) -> JuraClient:
        """Open a session and authenticate; the caller holds the session gate."""
        client = self._new_client(self._auth_hash)
        try:
            result = client.connect(timeout=HANDSHAKE_TIMEOUT)
        except (PairingTimeout, OSError) as err:
            client.close()
            raise JuraWifiConnectionError(str(err) or type(err).__name__) from err
        except HandshakeError as err:
            client.close()
            raise JuraWifiConnectionError(f"unexpected handshake reply: {err}") from err
        if result.state != "CORRECT":
            client.close()
            raise JuraWifiAuthError(result.state)
        return client

    def check(self) -> None:
        """Verify that the machine answers and accepts the credentials."""
        with self._gate.session():
            self._open().close()

    def pair(self, on_prompt: Callable[[str], None] | None = None) -> str:
        """Pair with the machine and return the new auth hash.

        Blocks until the connect prompt on the machine was confirmed (or the
        pairing window of 60 seconds ran out). :meth:`cancel` ends it early.
        """
        with self._gate.session():
            if self._cancelled.is_set():
                raise JuraWifiError("pairing was cancelled")
            client = self._new_client("", with_profile=False)
            self._pairing = client
            try:
                result = client.pair(timeout=PAIRING_TIMEOUT, on_user_prompt=on_prompt)
            except PairingTimeout as err:
                raise JuraWifiPairingTimeout(str(err)) from err
            except OSError as err:
                raise JuraWifiConnectionError(str(err) or type(err).__name__) from err
            except HandshakeError as err:
                raise JuraWifiError(str(err)) from err
            finally:
                self._pairing = None
                client.close()
        if result.state != "CORRECT":
            raise JuraWifiAuthError(result.state)
        if not result.new_hash:
            raise JuraWifiAuthError("NO_HASH")
        return result.new_hash

    def cancel(self) -> None:
        """Abort a pairing that is waiting or running (callable from any thread)."""
        self._cancelled.set()
        client = self._pairing
        if client is not None:
            client.conn.close()

    def fetch(self) -> MachineSnapshot:
        """Read status, maintenance data and brew counters in one session."""
        with self._gate.session():
            client = self._open()
            try:
                info = client.read_machine_info(timeout=READ_TIMEOUT)
                try:
                    products = client.read_product_counters()
                except ValueError:
                    products = None
            except OSError as err:
                raise JuraWifiConnectionError(str(err) or type(err).__name__) from err
            except Exception as err:
                raise JuraWifiError(f"unexpected reply from machine: {err}") from err
            finally:
                client.close()

        status = info.status
        return MachineSnapshot(
            active_alerts=frozenset(status.active_alerts),
            errors=frozenset(status.errors),
            blocked_products=frozenset(status.blocked_products),
            maintenance_counters=dict(info.maintenance_counters.counters),
            maintenance_percent={
                name: value
                for name, value in info.maintenance_percent.percent
                if value != PERCENT_NOT_REPORTED
            },
            total_brews=products.total if products is not None else None,
            product_counts=dict(products.by_name) if products is not None else {},
        )

    def brew(self, product: str) -> None:
        """Start a product with the factory-default recipe of the profile."""
        with self._gate.session():
            client = self._open()
            try:
                reply = client.brew(product, retry=True)
            except OSError as err:
                raise JuraWifiConnectionError(str(err) or type(err).__name__) from err
            except Exception as err:
                raise JuraWifiError(str(err)) from err
            finally:
                client.close()
        accepted = reply.strip().lower()
        if not accepted.startswith("@tp") or accepted.startswith("@tp:00"):
            raise JuraWifiError(f"machine rejected the request ({reply!r})")
