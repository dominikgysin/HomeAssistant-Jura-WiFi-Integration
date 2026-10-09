"""Tests for the blocking client wrapper (the library is replaced by a fake)."""

from __future__ import annotations

import threading
from unittest.mock import MagicMock, patch

from jura_connect import PairingTimeout
from jura_connect.client import (
    HandshakeResult,
    MachineInfo,
    MachineStatus,
    MaintenanceCounters,
    MaintenancePercent,
    ProductCounters,
)
import pytest

from custom_components.jura_wifi import api
from custom_components.jura_wifi.api import (
    JuraWifiAuthError,
    JuraWifiClient,
    JuraWifiConnectionError,
    JuraWifiError,
    JuraWifiPairingTimeout,
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
