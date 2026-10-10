"""Blocking wrapper around the ``jura_connect`` library.

Every public method of :class:`JuraWifiClient` performs synchronous socket I/O
and has to run in an executor thread, never in the event loop.
"""

from __future__ import annotations

from collections.abc import Callable, Iterable, Iterator, Mapping
import contextlib
import dataclasses
import importlib.resources
import ipaddress
import logging
import socket
import threading
import time
from typing import Any
import xml.etree.ElementTree as ET

import ifaddr
from jura_connect import (
    KIND_BYPASS,
    KIND_COFFEE_STRENGTH,
    KIND_MILK_AMOUNT,
    KIND_MILK_BREAK,
    KIND_MILK_FOAM_AMOUNT,
    KIND_TEMPERATURE,
    KIND_WATER_AMOUNT,
    HandshakeError,
    JuraClient,
    Machine,
    MachineProfile,
    PairingTimeout,
    ProcessError,
    ProcessRunner,
    ProductDef,
    ProductParam,
    ProductProgress,
    ProgressState,
    ProgressType,
    available_processes,
    discover,
    is_progress_frame,
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
RECIPE_TIMEOUT = 3.0
SETTING_TIMEOUT = 3.0

# The only handshake states that say the stored credentials are no good. Any other
# refusal (the machine aborted the connection, rejected it with a code) is not about
# the credentials and may be gone at the next try, like an unreachable machine.
AUTH_FAILURE_STATES = frozenset({"WRONG_HASH", "WRONG_PIN"})

# The maintenance percent bank reports 0xFF for indicators a machine lacks.
PERCENT_NOT_REPORTED = 0xFF

# Millilitre parameters travel as one byte of 5 ml ticks.
ML_PER_TICK = 5

# Maintenance programs that are started even if the profile of the machine does not
# list them. Only the GIGA 6 profiles declare the coffee system rinse, yet a real
# E8 (SDS) runs it as well when it gets the same verb (@TG:22).
UNDECLARED_PROGRAMS = frozenset({"coffee_rinse"})

# The recipe parameters of a drink and the keyword with which ``JuraClient.brew``
# takes each of them. The grinder parameters are left to the defaults of the
# library: only one profile is known to use them and they are not verified.
BREW_ARGUMENT_BY_KIND = {
    KIND_COFFEE_STRENGTH: "strength",
    KIND_WATER_AMOUNT: "ml",
    KIND_TEMPERATURE: "temperature",
    KIND_MILK_AMOUNT: "milk",
    KIND_MILK_FOAM_AMOUNT: "milk_foam",
    KIND_MILK_BREAK: "milk_break",
    KIND_BYPASS: "bypass",
}


@dataclasses.dataclass(frozen=True, slots=True)
class BrewOption:
    """A recipe parameter that the brew action lets the caller change."""

    key: str  # the field of the action
    kind: str  # the recipe parameter of the machine profile
    argument: str  # the keyword of ``JuraClient.brew``
    named: bool = False  # the value is the name of an item, not a number


# What the action offers is what the E8 recipes use. The milk amount of the milk
# coffee drinks of other machines and the grinder parameters are left out.
BREW_OPTIONS: tuple[BrewOption, ...] = (
    BrewOption("strength", KIND_COFFEE_STRENGTH, "strength"),
    BrewOption("water_amount", KIND_WATER_AMOUNT, "ml"),
    BrewOption("temperature", KIND_TEMPERATURE, "temperature", named=True),
    BrewOption("milk_foam_time", KIND_MILK_FOAM_AMOUNT, "milk_foam"),
    BrewOption("milk_break", KIND_MILK_BREAK, "milk_break"),
    BrewOption("bypass", KIND_BYPASS, "bypass"),
)

ACTIVITY_BREWING = "brewing"
ACTIVITY_MAINTENANCE = "maintenance"
ACTIVITY_PROGRAMMING = "programming"
ACTIVITY_BUSY = "busy"

_ACTIVITY_BY_PROGRESS_TYPE = {
    ProgressType.PRODUCT: ACTIVITY_BREWING,
    ProgressType.PROCESS: ACTIVITY_MAINTENANCE,
}


@dataclasses.dataclass(frozen=True, slots=True)
class MachineActivity:
    """What a machine is doing while it pushes progress instead of status frames.

    ``kind`` is one of the ``ACTIVITY_*`` values. ``detail`` names the drink or
    the maintenance program, or the raw progress state for anything else.
    """

    kind: str
    detail: str | None = None


class JuraWifiError(Exception):
    """Base class for errors raised by this module."""


class JuraWifiConnectionError(JuraWifiError):
    """The machine did not answer (switched off or asleep)."""


class JuraWifiBusy(JuraWifiError):
    """The machine answers, but only reports what it is doing.

    While it brews, runs a maintenance program or shows its programming menu the
    machine pushes progress frames instead of the status frame that a poll waits
    for. It is reachable, just not able to report its alerts and counters.
    """

    def __init__(self, activity: MachineActivity) -> None:
        """Initialize with the activity derived from the progress frames."""
        super().__init__(activity.kind)
        self.activity = activity


class JuraWifiAuthError(JuraWifiError):
    """The machine refused the stored credentials or the PIN."""

    def __init__(self, reason: str) -> None:
        """Initialize with the handshake state reported by the machine."""
        super().__init__(reason)
        self.reason = reason


class JuraWifiPairingTimeout(JuraWifiError):
    """The connect prompt on the machine was not confirmed in time."""


class BrewOptionError(ValueError):
    """A value for the brew action does not fit the drink.

    ``translation_key`` names the message under ``exceptions`` in the translations
    and ``placeholders`` fill it.
    """

    def __init__(self, translation_key: str, **placeholders: str) -> None:
        """Initialize with the message to show and what goes into it."""
        super().__init__(translation_key)
        self.translation_key = translation_key
        self.placeholders = placeholders


@dataclasses.dataclass(frozen=True, slots=True)
class MachineSnapshot:
    """Result of one successful poll, or what the cache restored after a restart.

    ``restored`` marks a snapshot that was loaded from the cache: only the counters
    and the maintenance values are known then, the alerts are not. ``settings``
    holds the raw values of the machine settings that this poll read, or ``None``
    if it did not read them. ``total_brews`` is ``None`` and ``product_counts`` is
    empty when the counters could not be read.
    """

    active_alerts: frozenset[str]
    errors: frozenset[str]
    blocked_products: frozenset[str]
    maintenance_counters: Mapping[str, int]
    maintenance_percent: Mapping[str, int]
    total_brews: int | None
    product_counts: Mapping[str, int]
    settings: Mapping[str, str] | None = None
    restored: bool = False


@dataclasses.dataclass(frozen=True, slots=True)
class MachineIdentity:
    """What the discovery reply of the dongle says about its machine.

    ``ef_code`` and ``model_name`` are ``None`` when the article number is not
    in the catalogue of the J.O.E. app that ships with the library, or when the
    library has no profile for that machine type. ``serial_number`` is the number
    on the type plate, see :func:`type_plate_serial`, or ``None`` when the reply
    does not hold what it is made of.
    """

    article_number: int
    firmware: str
    ef_code: str | None
    model_name: str | None
    serial_number: str | None = None


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
        serial_number=type_plate_serial(machine),
    )


def type_plate_serial(machine: Machine) -> str | None:
    """Return the serial number on the type plate of the machine, from its reply.

    The type plate shows the production date as YYYYMMDD followed by the machine
    number with six digits, for example 20240117001234; checked against the plate
    of one E8. The discovery reply holds both. The field that the library calls
    ``serial_number`` is another number.
    Returns ``None`` when the reply has no production date, or when the 16 bit
    machine number is erased, which reads as all zeros or all ones.
    """
    if machine.production_date is None or not 0 < machine.machine_number < 0xFFFF:
        return None
    return f"{machine.production_date:%Y%m%d}{machine.machine_number:06d}"


def describe_activity(
    frames: Iterable[str], profile: MachineProfile | None
) -> MachineActivity | None:
    """Say what the machine is doing from the frames it pushed on its own.

    Only a session in which progress frames (``@TV:``) arrived but no status
    frame (``@TF:``) counts as activity. Returns ``None`` otherwise. Without a
    profile the drinks cannot be named, but the menu is still recognized.
    """
    pushed = [frame for frame in frames if frame.startswith(("@TF:", "@TV:"))]
    if not pushed or any(frame.startswith("@TF:") for frame in pushed):
        return None
    for frame in reversed(pushed):
        if not is_progress_frame(frame):
            continue
        progress = ProductProgress.parse(frame, profile)
        # State FF is the settings menu whatever the next byte looks like; it
        # must not turn into a drink because that byte happens to be a product code.
        if progress.state is ProgressState.P_MODE:
            return MachineActivity(ACTIVITY_PROGRAMMING)
        kind = _ACTIVITY_BY_PROGRESS_TYPE.get(progress.progress_type, ACTIVITY_BUSY)
        if kind == ACTIVITY_BUSY:
            return MachineActivity(kind, progress.state_name.lower())
        return MachineActivity(kind, progress.subject)
    # Only language-download or clock-sync frames: alive, but nothing to name.
    return MachineActivity(ACTIVITY_BUSY)


def _decode_parameter(param: ProductParam, raw: int) -> int | None:
    """Return the value in XML units that the library encodes to the byte ``raw``.

    This is the inverse of :meth:`ProductParam.encode`. Millilitre parameters are
    stored in ticks, everything else as it is; trying both and keeping the one
    that encodes back to ``raw`` leaves no room for a wrong guess. ``None`` if the
    byte is not a valid value for the parameter.
    """
    for candidate in (raw, raw * ML_PER_TICK):
        try:
            if param.encode(candidate) == raw:
                return candidate
        except ValueError:
            continue
    return None


def recipe_from_stored(definition: ProductDef, stored: str) -> dict[str, int]:
    """Turn the recipe stored on the machine into ``JuraClient.brew`` arguments.

    ``stored`` is the hex payload the machine returns for a drink (``@TM:41``).
    It uses the byte layout of the brew command, so the byte of every parameter is
    read at the offset the machine profile gives for it. Parameters that are
    missing or hold a value the profile does not accept are left out; the library
    then takes its default for them.
    """
    try:
        blob = bytes.fromhex(stored)
    except ValueError:
        return {}
    arguments: dict[str, int] = {}
    for param in definition.params:
        keyword = BREW_ARGUMENT_BY_KIND.get(param.kind)
        if keyword is None or param.offset >= len(blob):
            continue
        value = _decode_parameter(param, blob[param.offset])
        if value is not None:
            arguments[keyword] = value
    return arguments


def brew_arguments(
    definition: ProductDef, options: Mapping[str, Any]
) -> dict[str, int | str]:
    """Check the options of the brew action against the profile of the drink.

    Returns the arguments for ``JuraClient.brew``. Only the parameters that the
    profile defines for the drink are accepted, and only within the range, the step
    or the items that it declares. Raises :class:`BrewOptionError` for anything else.
    """
    arguments: dict[str, int | str] = {}
    for option in BREW_OPTIONS:
        if option.key not in options:
            continue
        param = definition.param(option.kind)
        if param is None or (option.named and not param.items):
            raise BrewOptionError("brew_option_unsupported", option=option.key)
        arguments[option.argument] = _checked_brew_value(
            option, param, options[option.key]
        )
    return arguments


def _checked_brew_value(
    option: BrewOption, param: ProductParam, value: Any
) -> int | str:
    """Return ``value`` if the parameter accepts it, else raise."""
    if option.named:
        names = [item.name for item in param.items]
        name = str(value).strip().lower()
        if name not in names:
            raise BrewOptionError(
                "brew_option_choice", option=option.key, allowed=", ".join(names)
            )
        return name
    number = int(value)
    if param.items:
        # The levels (coffee strength) are numbers that the profile lists as items.
        allowed = sorted(int(item.value, 16) for item in param.items)
        if number not in allowed:
            raise BrewOptionError(
                "brew_option_choice",
                option=option.key,
                allowed=", ".join(str(level) for level in allowed),
            )
        return number
    minimum = 0 if param.minimum is None else param.minimum
    maximum = 0xFF if param.maximum is None else param.maximum
    if not minimum <= number <= maximum:
        raise BrewOptionError(
            "brew_option_range",
            option=option.key,
            minimum=str(minimum),
            maximum=str(maximum),
        )
    step = param.step or 1
    if step > 1 and (number - minimum) % step:
        raise BrewOptionError(
            "brew_option_step",
            option=option.key,
            minimum=str(minimum),
            step=str(step),
        )
    return number


# The maintenance processes that the profile gives a threshold for, with the field of
# the percent bank that tells how far the machine is.
PREDICTIVE_FIELDS = {
    "Cleaning": "cleaning",
    "Decalc": "descale",
    "FilterChange": "filter_change",
}


@dataclasses.dataclass(frozen=True, slots=True)
class ProfileExtras:
    """What the XML of a machine declares and the library does not expose.

    ``predictive_thresholds`` maps the field of a maintenance percent to the percent
    at which the J.O.E. app recommends the maintenance.
    """

    predictive_thresholds: Mapping[str, int] = dataclasses.field(default_factory=dict)


def load_profile_extras(profile: MachineProfile) -> ProfileExtras:
    """Read the declarations of the machine XML that the profile does not carry.

    Blocking: the XML ships with the library and is read from disk. A machine whose
    XML cannot be read simply has none of them.
    """
    try:
        text = (
            importlib.resources.files("jura_connect")
            .joinpath("data", "xml", profile.code, f"{profile.version}.xml")
            .read_text(encoding="utf-8")
        )
        root = ET.fromstring(text)
    except (OSError, ValueError, ET.ParseError) as err:
        _LOGGER.debug("Could not read the XML of %s: %s", profile.code, err)
        return ProfileExtras()
    thresholds: dict[str, int] = {}
    for button in root.findall(".//{*}PREDICTIVEMAINTENANCE/{*}PREDICTIVEBUTTON"):
        field = PREDICTIVE_FIELDS.get(button.get("Process") or button.get("Name") or "")
        try:
            threshold = int(button.get("Threshold") or "")
        except ValueError:
            continue
        if field is not None:
            thresholds[field] = threshold
    return ProfileExtras(predictive_thresholds=thresholds)


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
        if result.state in AUTH_FAILURE_STATES:
            client.close()
            raise JuraWifiAuthError(result.state)
        if result.state != "CORRECT":
            client.close()
            raise JuraWifiConnectionError(
                f"the machine refused the connection ({result.state})"
            )
        return client

    def check(self) -> None:
        """Verify that the machine answers and accepts the credentials."""
        with self._gate.session():
            self._open().close()

    def pair(self, on_prompt: Callable[[str], None] | None = None) -> str:
        """Pair with the machine and return the new auth hash.

        Blocks until the connect prompt on the machine was confirmed (or the
        pairing window of 60 seconds ran out). :meth:`cancel` ends it early.

        Raises :class:`JuraWifiBusy` when pairing failed while the machine was in
        its menu or busy, because it cannot show the connect prompt then.
        """
        with self._gate.session():
            if self._cancelled.is_set():
                raise JuraWifiError("pairing was cancelled")
            client = self._new_client("", with_profile=False)
            self._pairing = client
            try:
                result = client.pair(timeout=PAIRING_TIMEOUT, on_user_prompt=on_prompt)
            except PairingTimeout as err:
                self._raise_if_busy(client, err)
                raise JuraWifiPairingTimeout(str(err)) from err
            except OSError as err:
                raise JuraWifiConnectionError(str(err) or type(err).__name__) from err
            except HandshakeError as err:
                raise JuraWifiError(str(err)) from err
            finally:
                self._pairing = None
                client.close()
        if result.state != "CORRECT":
            # A wrong PIN is certain; any other refusal may be a machine that is
            # in its menu and cannot ask the user.
            if result.state != "WRONG_PIN":
                self._raise_if_busy(client, None)
            raise JuraWifiAuthError(result.state)
        if not result.new_hash:
            raise JuraWifiAuthError("NO_HASH")
        return result.new_hash

    @staticmethod
    def _raise_if_busy(client: JuraClient, cause: BaseException | None) -> None:
        """Raise :class:`JuraWifiBusy` if the frames of the session show activity."""
        activity = describe_activity(client.status_history, None)
        if activity is not None:
            raise JuraWifiBusy(activity) from cause

    def cancel(self) -> None:
        """Abort a pairing that is waiting or running (callable from any thread)."""
        self._cancelled.set()
        client = self._pairing
        if client is not None:
            client.conn.close()

    def fetch(self, with_settings: bool = False) -> MachineSnapshot:
        """Read status, maintenance data and brew counters in one session.

        With ``with_settings`` the machine settings of the profile are read in the
        same session, so that no second session (and pause) is needed for them.

        Raises :class:`JuraWifiBusy` when the machine answers but pushes progress
        frames instead of a status frame.
        """
        with self._gate.session():
            client = self._open()
            try:
                info = client.read_machine_info(timeout=READ_TIMEOUT)
                try:
                    products = client.read_product_counters()
                except ValueError as err:
                    _LOGGER.debug("The product counters could not be read: %s", err)
                    products = None
                settings = self._read_settings(client) if with_settings else None
            except TimeoutError as err:
                activity = describe_activity(client.status_history, self.profile)
                if activity is not None:
                    raise JuraWifiBusy(activity) from err
                raise JuraWifiConnectionError(str(err) or type(err).__name__) from err
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
            settings=settings,
        )

    def _read_settings(self, client: JuraClient) -> dict[str, str]:
        """Read the raw value of every setting that the profile declares.

        A setting that cannot be read is left out and the others are still read.
        The reading stops where the machine does not answer or the connection breaks,
        so that a machine that ignores the requests does not hold up the poll.
        """
        values: dict[str, str] = {}
        for definition in self.profile.settings:
            try:
                raw = client.read_setting(
                    definition.p_argument, timeout=SETTING_TIMEOUT
                )
            except ValueError as err:
                _LOGGER.debug("Setting %s could not be read: %s", definition.name, err)
                continue
            except OSError as err:
                _LOGGER.debug("Stopped reading the settings: %s", err)
                break
            # The library hands out what it got when the answer is too short to hold
            # a value, which is what a machine without that setting sends.
            if raw:
                values[definition.p_argument.upper()] = raw.upper()
        return values

    def write_setting(self, p_argument: str, value: str) -> str:
        """Write one machine setting and return the value the machine reports for it.

        ``value`` is the value of the setting in the wire format of the machine. The
        library sends the write between ``@TS:01`` and ``@TS:00``, checks the checksum
        of the request and reads the setting back; it raises if the machine did not
        store the value.
        """
        with self._gate.session():
            client = self._open()
            try:
                reply = client.write_setting(p_argument, value, timeout=SETTING_TIMEOUT)
                try:
                    stored = client.read_setting(p_argument, timeout=SETTING_TIMEOUT)
                except (ValueError, OSError):
                    stored = ""
            except TimeoutError as err:
                raise JuraWifiError(
                    "the machine did not answer the setting request"
                ) from err
            except OSError as err:
                raise JuraWifiConnectionError(str(err) or type(err).__name__) from err
            except Exception as err:
                raise JuraWifiError(str(err)) from err
            finally:
                client.close()
        if reply.strip().lower().startswith("@an:error"):
            raise JuraWifiError(f"machine refused the setting ({reply!r})")
        return (stored or value).upper()

    def _stored_recipe(self, client: JuraClient, product: str) -> dict[str, int]:
        """Read the recipe the machine has stored for a drink, as ``brew`` arguments.

        The machine keeps one recipe per drink, including what the user changed at
        its display. The machine is asked for every drink, also for those that the
        profile marks as not programmable: the E8 answers for them as well. An
        empty dict means that the factory recipe of the profile is used: the
        machine does not hand its recipes out, or the answer was unusable.
        """
        definition = next((p for p in self.profile.products if p.name == product), None)
        if definition is None:
            return {}
        try:
            stored = client.read_pmode_product(product, timeout=RECIPE_TIMEOUT)
        except (ValueError, TimeoutError) as err:
            _LOGGER.debug("No usable stored recipe for %s: %s", product, err)
            return {}
        if stored is None:
            return {}
        arguments = recipe_from_stored(definition, stored.blob)
        _LOGGER.debug("Recipe of %s stored on the machine: %s", product, arguments)
        return arguments

    def brew(
        self, product: str, options: Mapping[str, int | str] | None = None
    ) -> None:
        """Start a drink with the recipe that is stored on the machine.

        ``options`` are arguments for ``JuraClient.brew`` that replace the matching
        parameters of the stored recipe; everything else stays as the machine has it.
        Falls back to the factory recipe of the profile if the machine does not
        hand its recipes out.
        """
        with self._gate.session():
            client = self._open()
            try:
                arguments = {**self._stored_recipe(client, product), **(options or {})}
                reply = client.brew(product, retry=True, **arguments)
            except TimeoutError as err:
                raise JuraWifiError(
                    "the machine did not answer the brew request"
                ) from err
            except OSError as err:
                raise JuraWifiConnectionError(str(err) or type(err).__name__) from err
            except Exception as err:
                raise JuraWifiError(str(err)) from err
            finally:
                client.close()
        accepted = reply.strip().lower()
        if not accepted.startswith("@tp") or accepted.startswith("@tp:00"):
            raise JuraWifiError(f"machine rejected the request ({reply!r})")

    @staticmethod
    def _process_runner(client: JuraClient, process: str) -> ProcessRunner:
        """Bind a maintenance program to the session; nothing is sent yet.

        The library refuses a program that the profile of the machine does not
        declare, because sending it would be a guess. The programs in
        ``UNDECLARED_PROGRAMS`` are sent anyway, with the verb of the built-in table.
        """
        try:
            return client.process_runner(process)
        except ProcessError:
            if process not in UNDECLARED_PROGRAMS:
                raise
        fallback = next(p for p in available_processes(None) if p.name == process)
        return ProcessRunner(client, fallback)

    def start_process(self, process: str) -> None:
        """Start a maintenance program (cleaning, descaling, milk system rinse, ...).

        Only the start is sent. The machine then leads the user through the program
        on its display and waits there for the confirmations, e.g. for emptying the
        drip tray or inserting a tablet. The coffee system rinse needs none: it runs
        right away.
        """
        with self._gate.session():
            client = self._open()
            try:
                self._process_runner(client, process).start(timeout=READ_TIMEOUT)
            except TimeoutError as err:
                raise JuraWifiError(
                    "the machine did not answer the start request"
                ) from err
            except OSError as err:
                raise JuraWifiConnectionError(str(err) or type(err).__name__) from err
            except Exception as err:
                raise JuraWifiError(str(err)) from err
            finally:
                client.close()

    def cancel_step(self) -> None:
        """Cancel what the machine is doing: the running drink or maintenance step."""
        with self._gate.session():
            client = self._open()
            try:
                reply = client.request(
                    "@TG:FF", match=r"(?i)^@(tg|an)", timeout=READ_TIMEOUT
                )
            except TimeoutError as err:
                raise JuraWifiError(
                    "the machine did not answer the cancel request"
                ) from err
            except OSError as err:
                raise JuraWifiConnectionError(str(err) or type(err).__name__) from err
            finally:
                client.close()
        if reply.strip().lower().startswith("@an:error"):
            raise JuraWifiError(f"machine refused the request ({reply!r})")
