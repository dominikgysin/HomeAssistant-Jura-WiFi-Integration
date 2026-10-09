"""Tests for the blocking client wrapper (the library is replaced by a fake)."""

from __future__ import annotations

import socket
import threading
from types import SimpleNamespace
from unittest.mock import MagicMock, patch

from jura_connect import Machine, PairingTimeout, list_profile_codes
from jura_connect.client import (
    HandshakeResult,
    MachineInfo,
    MachineStatus,
    MaintenanceCounters,
    MaintenancePercent,
    ProductCounters,
)
from jura_connect.profile import _catalogue
import pytest

from custom_components.jura_wifi import api
from custom_components.jura_wifi.api import (
    JuraWifiAuthError,
    JuraWifiClient,
    JuraWifiConnectionError,
    JuraWifiError,
    JuraWifiPairingTimeout,
    MachineIdentity,
    discover_machine,
)

MACHINE_INFO = MachineInfo(
    conn_id="homeassistant-12345678",
    auth_hash="f" * 64,
    handshake_state="CORRECT",
    status=MachineStatus(
        raw=b"",
        active_alerts=("fill_water", "coffee_ready"),
        errors=("fill_water",),
        info=("coffee_ready",),
        process=(),
        blocked_products=("espresso",),
    ),
    maintenance_counters=MaintenanceCounters(
        counters=(("cleaning", 0), ("descale", 3)), raw=b""
    ),
    maintenance_percent=MaintenancePercent(
        percent=(("cleaning", 10), ("filter_change", 255), ("descale", 0)), raw=b""
    ),
)


@pytest.fixture(autouse=True)
def no_sleep(monkeypatch: pytest.MonkeyPatch) -> MagicMock:
    """Never really wait for the session gap."""
    sleep = MagicMock()
    monkeypatch.setattr(api.time, "sleep", sleep)
    return sleep


@pytest.fixture(autouse=True)
def fresh_gates() -> None:
    """Start every test with open session gates."""
    api._GATES.clear()


@pytest.fixture
def fake() -> MagicMock:
    """Return the fake library client used by every JuraWifiClient."""
    with patch.object(api, "JuraClient") as client_cls:
        fake = client_cls.return_value
        fake.connect.return_value = HandshakeResult("@hp4", "CORRECT", None)
        fake.pair.return_value = HandshakeResult("@hp4:aa", "CORRECT", "a" * 64)
        fake.read_machine_info.return_value = MACHINE_INFO
        fake.read_product_counters.return_value = ProductCounters(
            total=20, by_name={"espresso": 7}, by_code={}, raw_slots=()
        )
        fake.brew.return_value = "@tp"
        yield fake


def _client() -> JuraWifiClient:
    return JuraWifiClient("192.0.2.10", 51515, "ha-1", "f" * 64, "", "EF1120")


def test_fetch_builds_snapshot(fake: MagicMock) -> None:
    """The snapshot carries everything the entities need."""
    snapshot = _client().fetch()

    assert snapshot.active_alerts == {"fill_water", "coffee_ready"}
    assert snapshot.errors == {"fill_water"}
    assert snapshot.blocked_products == {"espresso"}
    assert snapshot.maintenance_counters == {"cleaning": 0, "descale": 3}
    # 0xFF means "not reported" and is dropped.
    assert snapshot.maintenance_percent == {"cleaning": 10, "descale": 0}
    assert snapshot.total_brews == 20
    assert snapshot.product_counts == {"espresso": 7}
    fake.close.assert_called_once()


def test_fetch_without_product_counters(fake: MagicMock) -> None:
    """A machine without the counter bank still yields a snapshot."""
    fake.read_product_counters.side_effect = ValueError("no @TR:32")
    snapshot = _client().fetch()
    assert snapshot.total_brews is None
    assert snapshot.product_counts == {}


@pytest.mark.parametrize(
    "error", [ConnectionRefusedError("refused"), TimeoutError("timed out"), OSError()]
)
def test_unreachable_machine(fake: MagicMock, error: Exception) -> None:
    """Socket errors during connect mean the machine is not reachable."""
    fake.connect.side_effect = error
    with pytest.raises(JuraWifiConnectionError):
        _client().fetch()
    fake.close.assert_called_once()


def test_silent_handshake_is_a_connection_error(fake: MagicMock) -> None:
    """A dongle that never answers the handshake counts as unreachable."""
    fake.connect.side_effect = PairingTimeout("no @hp4/@hp5 reply")
    with pytest.raises(JuraWifiConnectionError):
        _client().fetch()


def test_connection_lost_while_reading(fake: MagicMock) -> None:
    """Errors during the reads are connection errors and close the session."""
    fake.read_machine_info.side_effect = TimeoutError("no pushed @TF: status frame")
    with pytest.raises(JuraWifiConnectionError):
        _client().fetch()
    fake.close.assert_called_once()


def test_unexpected_reply(fake: MagicMock) -> None:
    """Garbled replies are reported as generic errors, not as an outage."""
    fake.read_machine_info.side_effect = RuntimeError("bad frame")
    with pytest.raises(JuraWifiError) as err:
        _client().fetch()
    assert not isinstance(err.value, JuraWifiConnectionError)


@pytest.mark.parametrize("state", ["WRONG_HASH", "WRONG_PIN", "ABORTED"])
def test_rejected_credentials(fake: MagicMock, state: str) -> None:
    """A handshake other than CORRECT is an authentication error."""
    fake.connect.return_value = HandshakeResult("@hp5", state, None)
    with pytest.raises(JuraWifiAuthError) as err:
        _client().fetch()
    assert err.value.reason == state
    fake.close.assert_called_once()


def test_pair_returns_new_hash(fake: MagicMock) -> None:
    """Pairing hands back the hash issued by the machine."""
    assert _client().pair() == "a" * 64
    fake.close.assert_called_once()


def test_pair_timeout(fake: MagicMock) -> None:
    """Not confirming the prompt in time is reported separately."""
    fake.pair.side_effect = PairingTimeout("no reply")
    with pytest.raises(JuraWifiPairingTimeout):
        _client().pair()


def test_pair_refused(fake: MagicMock) -> None:
    """A refusal such as a wrong PIN is an authentication error."""
    fake.pair.return_value = HandshakeResult("@hp5", "WRONG_PIN", None)
    with pytest.raises(JuraWifiAuthError) as err:
        _client().pair()
    assert err.value.reason == "WRONG_PIN"


def test_pair_without_hash(fake: MagicMock) -> None:
    """A CORRECT handshake that issued no hash cannot be used."""
    fake.pair.return_value = HandshakeResult("@hp4", "CORRECT", None)
    with pytest.raises(JuraWifiAuthError):
        _client().pair()


def test_brew_accepted(fake: MagicMock) -> None:
    """An @tp reply means the machine accepted the recipe."""
    _client().brew("espresso")
    fake.brew.assert_called_once_with("espresso", retry=True)
    fake.close.assert_called_once()


@pytest.mark.parametrize("reply", ["@tp:00", "@an:error", ""])
def test_brew_rejected(fake: MagicMock, reply: str) -> None:
    """Anything but a plain @tp is a rejection."""
    fake.brew.return_value = reply
    with pytest.raises(JuraWifiError, match="rejected"):
        _client().brew("espresso")


def test_brew_unknown_product(fake: MagicMock) -> None:
    """The library refuses unknown products before anything is sent."""
    fake.brew.side_effect = ValueError("unknown product 'tea'")
    with pytest.raises(JuraWifiError, match="unknown product"):
        _client().brew("tea")


def test_session_gap_is_enforced(fake: MagicMock, no_sleep: MagicMock) -> None:
    """A second session waits until the dongle had time to settle."""
    client = _client()
    client.fetch()
    no_sleep.assert_not_called()
    client.fetch()
    no_sleep.assert_called_once()
    assert 0 < no_sleep.call_args.args[0] <= api.SESSION_GAP_SECONDS


def test_session_gap_is_shared_between_client_objects(
    fake: MagicMock, no_sleep: MagicMock
) -> None:
    """Polling, config flow and reloads create separate clients for one dongle."""
    _client().pair()
    no_sleep.assert_not_called()
    # e.g. the reload right after a successful pairing
    _client().fetch()
    no_sleep.assert_called_once()
    _client().check()
    assert no_sleep.call_count == 2
    _client().brew("espresso")
    assert no_sleep.call_count == 3


def test_other_dongles_do_not_wait(fake: MagicMock, no_sleep: MagicMock) -> None:
    """The gap is per dongle."""
    _client().fetch()
    JuraWifiClient("192.0.2.11", 51515, "ha-1", "f" * 64, "", "EF1120").fetch()
    no_sleep.assert_not_called()


def test_a_failed_attempt_also_restarts_the_gap(
    fake: MagicMock, no_sleep: MagicMock
) -> None:
    """A rejected connection attempt counts as a session."""
    fake.connect.side_effect = ConnectionResetError("peer closed the connection")
    with pytest.raises(JuraWifiConnectionError):
        _client().fetch()
    fake.connect.side_effect = None
    _client().fetch()
    no_sleep.assert_called_once()


def test_sessions_with_one_dongle_never_overlap(fake: MagicMock) -> None:
    """A second thread waits for the first to finish before it connects."""
    in_session = threading.Event()
    release = threading.Event()
    overlaps: list[int] = []
    running: list[int] = []

    def slow_read(*args, **kwargs):
        running.append(1)
        in_session.set()
        release.wait(5)
        running.pop()
        return MACHINE_INFO

    def connect(*args, **kwargs):
        if running:
            overlaps.append(1)
        return HandshakeResult("@hp4", "CORRECT", None)

    fake.read_machine_info.side_effect = slow_read
    fake.connect.side_effect = connect

    first = threading.Thread(target=_client().fetch)
    second = threading.Thread(target=_client().fetch)
    first.start()
    assert in_session.wait(5)
    second.start()
    threading.Event().wait(0.2)  # time.sleep is patched in this module
    assert not overlaps
    release.set()
    first.join(5)
    second.join(5)
    assert not overlaps


def test_cancel_closes_the_pairing_socket(fake: MagicMock) -> None:
    """Closing the dialog hangs up the waiting pairing session."""
    started = threading.Event()
    hang_up = threading.Event()
    errors: list[Exception] = []

    def wait_for_user(*args, **kwargs):
        started.set()
        hang_up.wait(5)
        raise OSError("socket closed")

    fake.pair.side_effect = wait_for_user
    fake.conn.close.side_effect = hang_up.set
    client = _client()

    def run() -> None:
        try:
            client.pair()
        except JuraWifiError as err:
            errors.append(err)

    thread = threading.Thread(target=run)
    thread.start()
    assert started.wait(5)
    client.cancel()
    thread.join(5)

    assert not thread.is_alive()
    fake.conn.close.assert_called_once()
    assert len(errors) == 1
    assert isinstance(errors[0], JuraWifiConnectionError)


def test_cancel_before_the_pairing_starts(fake: MagicMock) -> None:
    """A pairing that was cancelled while queued never talks to the dongle."""
    client = _client()
    client.cancel()
    with pytest.raises(JuraWifiError, match="cancelled"):
        client.pair()
    fake.pair.assert_not_called()


def test_unknown_machine_type() -> None:
    """A machine type without a bundled profile is reported."""
    with pytest.raises(JuraWifiError, match="unknown machine type"):
        _ = JuraWifiClient("h", 51515, "c", "a", "", "EF0").profile


def test_pairing_does_not_need_the_model(fake: MagicMock) -> None:
    """The model is read after the pairing, so no profile can be loaded for it."""
    client = JuraWifiClient("192.0.2.10", 51515, "ha-1", "", "", "")
    assert client.pair() == "a" * 64
    assert api.JuraClient.call_args.kwargs["profile"] is None


def _machine(address: str = "192.0.2.10", article: int = 15833) -> Machine:
    return Machine(
        address=address,
        name="Coffeemaker",
        fw="TT237W V06.11",
        hw_id="",
        article_number=article,
        machine_number=1,
        serial_number=2,
        production_date=None,
        uchi_production_date=None,
        status_flags=0x10,
        status_hex="",
        raw=b"",
    )


@pytest.fixture
def udp() -> SimpleNamespace:
    """Replace the UDP functions of the library; nothing answers by default."""
    with (
        patch.object(api, "probe", return_value=None) as probe,
        patch.object(api, "discover", side_effect=lambda **_: iter(())) as discover,
        patch.object(api, "_broadcast_targets", return_value=["255.255.255.255"]),
    ):
        yield SimpleNamespace(probe=probe, discover=discover)


def test_discovery_reads_the_identity_from_the_broadcast_reply(
    udp: SimpleNamespace,
) -> None:
    """The J.O.E. app learns the article number the same way."""
    udp.discover.side_effect = lambda **_: iter(
        [_machine("192.0.2.99", 15713), _machine()]
    )
    assert discover_machine("192.0.2.10") == MachineIdentity(
        article_number=15833,
        firmware="TT237W V06.11",
        ef_code="EF1120",
        model_name="E8 (SDS)",
    )
    assert udp.discover.call_args.kwargs["targets"] == ["255.255.255.255"]


def test_discovery_uses_a_unicast_reply_when_there_is_one(udp: SimpleNamespace) -> None:
    """Dongles that answer a direct probe need no broadcast."""
    udp.probe.return_value = _machine()
    identity = discover_machine("192.0.2.10")
    assert identity is not None
    assert identity.model_name == "E8 (SDS)"
    udp.discover.assert_not_called()


def test_discovery_ignores_other_machines(udp: SimpleNamespace) -> None:
    """A reply from another dongle in the network must not be taken over."""
    udp.discover.side_effect = lambda **_: iter([_machine("192.0.2.99")])
    assert discover_machine("192.0.2.10") is None


def test_discovery_without_a_reply(udp: SimpleNamespace) -> None:
    """Another network or a silent dongle gives no identity."""
    assert discover_machine("192.0.2.10") is None


def test_discovery_with_an_article_the_catalogue_does_not_know(
    udp: SimpleNamespace,
) -> None:
    """The article number and firmware are still reported."""
    udp.probe.return_value = _machine(article=19999)
    assert discover_machine("192.0.2.10") == MachineIdentity(
        article_number=19999, firmware="TT237W V06.11", ef_code=None, model_name=None
    )


def test_discovery_with_a_machine_type_without_a_profile(udp: SimpleNamespace) -> None:
    """A catalogue entry the library has no profile for cannot be used."""
    supported = set(list_profile_codes())
    entry = next(e for e in _catalogue() if e.ef_code not in supported)
    udp.probe.return_value = _machine(article=entry.article_number)
    identity = discover_machine("192.0.2.10")
    assert identity is not None
    assert identity.article_number == entry.article_number
    assert identity.ef_code is None
    assert identity.model_name is None


@pytest.mark.parametrize("failing", ["probe", "discover"])
def test_discovery_survives_network_errors(udp: SimpleNamespace, failing: str) -> None:
    """A port that cannot be bound is not an error for the setup."""
    getattr(udp, failing).side_effect = OSError("address in use")
    assert discover_machine("192.0.2.10") is None


def test_discovery_with_an_unresolvable_host(udp: SimpleNamespace) -> None:
    """A host name that does not resolve gives no identity."""
    with patch.object(api.socket, "gethostbyname", side_effect=socket.gaierror):
        assert discover_machine("no-such-host.invalid") is None
    udp.probe.assert_not_called()


def test_broadcast_targets_cover_every_local_network() -> None:
    """Every IPv4 network of the host is scanned, plus the one of the dongle."""

    def ip(address: object, prefix: int, *, v4: bool = True) -> SimpleNamespace:
        return SimpleNamespace(ip=address, network_prefix=prefix, is_IPv4=v4)

    adapters = [
        SimpleNamespace(
            ips=[
                ip("198.51.100.20", 24),
                ip(("fe80::1", 0, 0), 64, v4=False),
                ip("127.0.0.1", 8),
                ip("169.254.3.4", 16),
                ip("10.0.5.9", 16),
                ip("10.9.9.9", 32),
            ]
        )
    ]
    with patch.object(api.ifaddr, "get_adapters", return_value=adapters):
        assert api._broadcast_targets("172.16.4.7") == [
            "10.0.255.255",
            "172.16.4.255",
            "198.51.100.255",
            "255.255.255.255",
        ]


def test_broadcast_targets_without_adapter_information() -> None:
    """The global broadcast and the network of the dongle always remain."""
    with patch.object(api.ifaddr, "get_adapters", side_effect=OSError("denied")):
        assert api._broadcast_targets("172.16.4.7") == [
            "172.16.4.255",
            "255.255.255.255",
        ]
