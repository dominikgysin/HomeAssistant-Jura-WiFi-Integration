"""Tests for the blocking client wrapper (the library is replaced by a fake)."""

from __future__ import annotations

import dataclasses
import datetime
import logging
import socket
import threading
from types import SimpleNamespace
from unittest.mock import MagicMock, patch

from jura_connect import (
    Machine,
    PairingTimeout,
    iter_profiles,
    list_profile_codes,
    load_profile,
)
from jura_connect.client import (
    HandshakeResult,
    MachineInfo,
    MachineStatus,
    MaintenanceCounters,
    MaintenancePercent,
    PModeProduct,
    ProductCounters,
)
from jura_connect.process import ProcessError
from jura_connect.profile import _catalogue
import pytest

from custom_components.jura_wifi import api
from custom_components.jura_wifi.api import (
    BrewOptionError,
    JuraWifiAuthError,
    JuraWifiBusy,
    JuraWifiClient,
    JuraWifiConnectionError,
    JuraWifiError,
    JuraWifiPairingTimeout,
    MachineActivity,
    MachineIdentity,
    ProfileExtras,
    brew_arguments,
    describe_activity,
    discover_machine,
    load_profile_extras,
    recipe_from_stored,
    type_plate_serial,
)

from .conftest import (
    E8_ADJUSTED_DRINKS,
    E8_RECIPE_ARGUMENTS,
    E8_STORED_RECIPES,
    P_MODE_FRAME,
    SERIAL,
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
        # A machine that does not hand out its recipes: the factory ones are used.
        fake.read_pmode_product.return_value = None
        fake.status_history = []
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


def test_fetch_without_product_counters(
    fake: MagicMock, caplog: pytest.LogCaptureFixture
) -> None:
    """A machine without the counter bank still yields a snapshot; why is logged."""
    caplog.set_level(logging.DEBUG, logger=api.__name__)
    fake.read_product_counters.side_effect = ValueError("no @TR:32")
    snapshot = _client().fetch()
    assert snapshot.total_brews is None
    assert snapshot.product_counts == {}
    assert "The product counters could not be read: no @TR:32" in caplog.text


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


def test_progress_frames_instead_of_a_status_frame_mean_busy(fake: MagicMock) -> None:
    """A machine in its menu answers, it just cannot report its status."""
    fake.read_machine_info.side_effect = TimeoutError("no pushed @TF: status frame")
    fake.status_history = ["@TB", P_MODE_FRAME, P_MODE_FRAME]
    with pytest.raises(JuraWifiBusy) as err:
        _client().fetch()
    assert err.value.activity == MachineActivity("programming")
    assert not isinstance(err.value, JuraWifiConnectionError)
    fake.read_product_counters.assert_not_called()
    fake.close.assert_called_once()


def test_silence_is_not_activity(fake: MagicMock) -> None:
    """Without any pushed frame the machine counts as unreachable."""
    fake.read_machine_info.side_effect = TimeoutError("no pushed @TF: status frame")
    fake.status_history = []
    with pytest.raises(JuraWifiConnectionError):
        _client().fetch()


def test_a_timeout_after_the_status_frame_is_not_activity(fake: MagicMock) -> None:
    """Progress frames next to a status frame do not make the machine busy."""
    fake.read_machine_info.side_effect = TimeoutError("no reply to '@TG:43'")
    fake.status_history = ["@TV:3C0200", "@TF:0123"]
    with pytest.raises(JuraWifiConnectionError):
        _client().fetch()


def test_a_dropped_connection_is_not_activity(fake: MagicMock) -> None:
    """Only a missing status frame counts, not a connection that went away."""
    fake.read_machine_info.side_effect = ConnectionResetError("reset by peer")
    fake.status_history = [P_MODE_FRAME]
    with pytest.raises(JuraWifiConnectionError):
        _client().fetch()


@pytest.mark.parametrize(
    ("frames", "expected"),
    [
        ([P_MODE_FRAME], MachineActivity("programming")),
        # State FF is the menu even if the next byte happens to be a product code.
        (["@TV:FF0200"], MachineActivity("programming")),
        (["@TV:3C0200"], MachineActivity("brewing", "espresso")),
        (["@TV:3E28"], MachineActivity("brewing", "americano")),
        (["@TV:7424"], MachineActivity("maintenance", "cleaning")),
        (["@TV:E1"], MachineActivity("busy", "warning")),
        (["@TV:84,0102"], MachineActivity("busy")),
        # The latest progress frame decides; language-download frames are skipped.
        ([P_MODE_FRAME, "@TV:3C0200"], MachineActivity("brewing", "espresso")),
        (["@TV:3C0200", "@TV:84,0102"], MachineActivity("brewing", "espresso")),
        # A status frame means that the machine reports as usual.
        (["@TV:3C0200", "@TF:0123"], None),
        (["@TB", "@TS"], None),
        ([], None),
    ],
)
def test_describe_activity(frames: list[str], expected: MachineActivity | None) -> None:
    """The progress frames name what the machine is doing."""
    assert describe_activity(frames, load_profile("EF1120")) == expected


@pytest.mark.parametrize("frame", [P_MODE_FRAME, "@TV:FF0200"])
def test_the_menu_is_recognized_without_a_profile(frame: str) -> None:
    """While pairing there is no profile yet, but the menu is what matters."""
    assert describe_activity([frame], None) == MachineActivity("programming")


def test_unexpected_reply(fake: MagicMock) -> None:
    """Garbled replies are reported as generic errors, not as an outage."""
    fake.read_machine_info.side_effect = RuntimeError("bad frame")
    with pytest.raises(JuraWifiError) as err:
        _client().fetch()
    assert not isinstance(err.value, JuraWifiConnectionError)


@pytest.mark.parametrize("state", ["WRONG_HASH", "WRONG_PIN"])
def test_rejected_credentials(fake: MagicMock, state: str) -> None:
    """A wrong hash or PIN is the only thing that is an authentication error."""
    fake.connect.return_value = HandshakeResult("@hp5", state, None)
    with pytest.raises(JuraWifiAuthError) as err:
        _client().fetch()
    assert err.value.reason == state
    fake.close.assert_called_once()


@pytest.mark.parametrize("state", ["ABORTED", "REJECTED:07", "REJECTED:FF"])
def test_a_refusal_that_is_no_credential_problem_is_transient(
    fake: MagicMock, state: str
) -> None:
    """The machine aborting or rejecting with a code is not about the credentials.

    Home Assistant stops polling after an authentication error until the machine is
    paired again, so these count as an unreachable machine, which is tried again.
    """
    fake.connect.return_value = HandshakeResult("@hp5", state, None)
    with pytest.raises(JuraWifiConnectionError, match=state) as err:
        _client().fetch()
    assert not isinstance(err.value, JuraWifiAuthError)
    fake.close.assert_called_once()


@pytest.mark.parametrize("state", ["ABORTED", "REJECTED:07", "WRONG_HASH"])
def test_the_check_of_the_address_tells_a_refusal_from_bad_credentials(
    fake: MagicMock, state: str
) -> None:
    """The reconfiguration shows 'invalid credentials' only for the credentials."""
    fake.connect.return_value = HandshakeResult("@hp5", state, None)
    expected = JuraWifiAuthError if state == "WRONG_HASH" else JuraWifiConnectionError
    with pytest.raises(expected):
        _client().check()


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


def _stored(product: str, blob: str) -> PModeProduct:
    code = int(blob[:2], 16)
    return PModeProduct(product_code=code, blob=blob, arguments={}, name=product)


@pytest.mark.parametrize("product", sorted(E8_STORED_RECIPES))
def test_brew_uses_the_recipe_stored_on_the_machine(
    fake: MagicMock, product: str
) -> None:
    """The drink comes out the way it was set up at the display."""
    fake.read_pmode_product.return_value = _stored(product, E8_STORED_RECIPES[product])

    _client().brew(product)

    fake.read_pmode_product.assert_called_once()
    assert fake.read_pmode_product.call_args.args == (product,)
    fake.brew.assert_called_once_with(
        product, retry=True, **E8_RECIPE_ARGUMENTS[product]
    )


@pytest.mark.parametrize("product", sorted(E8_STORED_RECIPES))
def test_the_stored_recipe_survives_the_round_trip_to_the_brew_blob(
    product: str,
) -> None:
    """Brewing with the translated values puts the stored bytes on the wire."""
    definition = next(p for p in load_profile("EF1120").products if p.name == product)
    stored = E8_STORED_RECIPES[product]
    keyword_to_kind = {v: k for k, v in api.BREW_ARGUMENT_BY_KIND.items()}

    arguments = recipe_from_stored(definition, stored)
    assert arguments == E8_RECIPE_ARGUMENTS[product]
    blob = definition.build_recipe_hex(
        {keyword_to_kind[key]: value for key, value in arguments.items()}
    )

    for param in definition.params:
        start = param.offset * 2
        assert blob[start : start + 2] == stored[start : start + 2], param.kind
    # Everything else is what the library sends for any drink.
    assert blob.startswith(f"{definition.code:02X}")
    assert blob[16:18] == "01"
    assert len(blob) == 32
    # Some drinks were adjusted at the display and differ from the factory recipe,
    # so the round trip is not just the defaults coming back.
    assert (blob != definition.build_recipe_hex({})) == (product in E8_ADJUSTED_DRINKS)


@pytest.mark.parametrize(
    "failure",
    [
        None,
        ValueError("checksum mismatch"),
        TimeoutError("no reply to '@TM:41,02'"),
    ],
)
def test_brew_falls_back_to_the_factory_recipe(
    fake: MagicMock, failure: Exception | None
) -> None:
    """A machine that keeps its recipes to itself must still be able to brew."""
    if failure is None:
        fake.read_pmode_product.return_value = None
    else:
        fake.read_pmode_product.side_effect = failure

    _client().brew("espresso")

    fake.brew.assert_called_once_with("espresso", retry=True)


@pytest.mark.parametrize("product", ["cortado", "americano"])
def test_brew_asks_the_machine_also_for_drinks_the_profile_calls_not_programmable(
    fake: MagicMock, product: str
) -> None:
    """The profile says these have no product settings; the E8 has them anyway."""
    definition = next(p for p in load_profile("EF1120").products if p.name == product)
    assert not definition.product_settings
    fake.read_pmode_product.return_value = _stored(product, E8_STORED_RECIPES[product])

    _client().brew(product)

    fake.read_pmode_product.assert_called_once()
    fake.brew.assert_called_once_with(
        product, retry=True, **E8_RECIPE_ARGUMENTS[product]
    )


def test_values_the_profile_does_not_accept_are_left_out() -> None:
    """A byte that is not a valid value never reaches the machine."""
    definition = next(p for p in load_profile("EF1120").products if p.name == "coffee")
    # strength 0x0B is above the ten levels, 0x01 ml ticks are below the 25 ml minimum
    stored = "03000B01000001000000000000"
    assert recipe_from_stored(definition, stored) == {"temperature": 1}


def test_a_stored_recipe_that_is_not_hex_is_ignored() -> None:
    """Garbage from the machine means the factory recipe."""
    definition = next(p for p in load_profile("EF1120").products if p.name == "coffee")
    assert recipe_from_stored(definition, "not hex") == {}
    assert recipe_from_stored(definition, "") == {}


def test_a_short_stored_recipe_is_read_as_far_as_it_goes() -> None:
    """Parameters past the end of the stored bytes are left to the library."""
    definition = next(
        p for p in load_profile("EF1120").products if p.name == "cappuccino"
    )
    assert recipe_from_stored(definition, "0400080C") == {"strength": 8, "ml": 60}


def test_brew_timeout_is_not_an_outage(fake: MagicMock) -> None:
    """The session worked, only the answer to the brew is missing."""
    fake.brew.side_effect = TimeoutError("no reply")
    with pytest.raises(JuraWifiError) as err:
        _client().brew("espresso")
    assert not isinstance(err.value, JuraWifiConnectionError)
    fake.close.assert_called_once()


def test_brew_connection_loss_is_an_outage(fake: MagicMock) -> None:
    """A session that breaks off counts like an unreachable machine."""
    fake.brew.side_effect = ConnectionResetError("reset by peer")
    with pytest.raises(JuraWifiConnectionError):
        _client().brew("espresso")


def test_start_process(fake: MagicMock) -> None:
    """Only the start is sent; the machine continues on its display."""
    _client().start_process("cappu_rinse")

    fake.process_runner.assert_called_once_with("cappu_rinse")
    fake.process_runner.return_value.start.assert_called_once()
    fake.close.assert_called_once()


@pytest.mark.parametrize(
    ("failure", "expected"),
    [
        (ProcessError("machine refused to start 'cleaning'"), JuraWifiError),
        (TimeoutError("no reply"), JuraWifiError),
        (ConnectionResetError("reset"), JuraWifiConnectionError),
    ],
)
def test_start_process_failures(
    fake: MagicMock, failure: Exception, expected: type[Exception]
) -> None:
    """A refusal or a missing answer is a failed command, a lost session an outage."""
    fake.process_runner.return_value.start.side_effect = failure
    with pytest.raises(expected) as err:
        _client().start_process("cleaning")
    if expected is JuraWifiError:
        assert not isinstance(err.value, JuraWifiConnectionError)
    fake.close.assert_called_once()


def test_start_process_the_machine_does_not_declare(fake: MagicMock) -> None:
    """The library refuses a program the profile does not know; nothing is sent."""
    fake.process_runner.side_effect = ProcessError("does not declare 'cappu_clean'")
    with pytest.raises(JuraWifiError, match="does not declare"):
        _client().start_process("cappu_clean")
    fake.request.assert_not_called()
    fake.close.assert_called_once()


def test_the_coffee_system_rinse_is_started_without_a_declaration(
    fake: MagicMock,
) -> None:
    """The E8 leaves the rinse out of its profile but runs it: its verb is sent."""
    fake.process_runner.side_effect = ProcessError("does not declare 'coffee_rinse'")
    fake.request.return_value = "@tg:22"

    _client().start_process("coffee_rinse")

    assert fake.request.call_args.args == ("@TG:22",)
    fake.close.assert_called_once()


@pytest.mark.parametrize(
    ("failure", "reply", "expected"),
    [
        (None, "@an:error", JuraWifiError),
        (TimeoutError("no reply"), "", JuraWifiError),
        (ConnectionResetError("reset"), "", JuraWifiConnectionError),
    ],
)
def test_the_coffee_system_rinse_failures(
    fake: MagicMock, failure: Exception | None, reply: str, expected: type[Exception]
) -> None:
    """A machine that does not run the rinse says so; a lost session is an outage."""
    fake.process_runner.side_effect = ProcessError("does not declare 'coffee_rinse'")
    fake.request.return_value = reply
    fake.request.side_effect = failure

    with pytest.raises(expected) as err:
        _client().start_process("coffee_rinse")

    if expected is JuraWifiError:
        assert not isinstance(err.value, JuraWifiConnectionError)
    fake.close.assert_called_once()


def test_cancel_step(fake: MagicMock) -> None:
    """The cancel verb is sent and its echo accepted."""
    fake.request.return_value = "@tg:FF"
    _client().cancel_step()

    assert fake.request.call_args.args == ("@TG:FF",)
    fake.close.assert_called_once()


@pytest.mark.parametrize(
    ("failure", "reply"),
    [(None, "@an:error"), (TimeoutError("no reply"), "")],
)
def test_cancel_step_failures(
    fake: MagicMock, failure: Exception | None, reply: str
) -> None:
    """A refusal or silence is a failed command, not an outage."""
    fake.request.return_value = reply
    fake.request.side_effect = failure
    with pytest.raises(JuraWifiError) as err:
        _client().cancel_step()
    assert not isinstance(err.value, JuraWifiConnectionError)


@pytest.mark.parametrize("state", ["ABORTED", "REJECTED:07", "WRONG_HASH"])
def test_pairing_refused_in_the_menu(fake: MagicMock, state: str) -> None:
    """A refusal while the machine sits in its menu is reported as such."""
    fake.pair.return_value = HandshakeResult("@hp5", state, None)
    fake.status_history = [P_MODE_FRAME]
    with pytest.raises(JuraWifiBusy) as err:
        _client().pair()
    assert err.value.activity == MachineActivity("programming")


def test_pairing_timeout_in_the_menu(fake: MagicMock) -> None:
    """A machine that cannot show the prompt never answers, but pushes its state."""
    fake.pair.side_effect = PairingTimeout("no @hp4/@hp5 reply")
    fake.status_history = [P_MODE_FRAME, P_MODE_FRAME]
    with pytest.raises(JuraWifiBusy) as err:
        _client().pair()
    assert err.value.activity.kind == "programming"
    fake.close.assert_called_once()


def test_pairing_while_the_machine_brews(fake: MagicMock) -> None:
    """Without a profile the drink cannot be named, but the machine is busy."""
    fake.pair.return_value = HandshakeResult("@hp5", "ABORTED", None)
    fake.status_history = ["@TB", "@TV:41020000000000000000000000FF00000000"]
    with pytest.raises(JuraWifiBusy) as err:
        _client().pair()
    assert err.value.activity == MachineActivity("busy", "hotwater_volume")


def test_a_wrong_pin_stays_a_wrong_pin_in_the_menu(fake: MagicMock) -> None:
    """The PIN is certain, whatever the machine is doing."""
    fake.pair.return_value = HandshakeResult("@hp5", "WRONG_PIN", None)
    fake.status_history = [P_MODE_FRAME]
    with pytest.raises(JuraWifiAuthError) as err:
        _client().pair()
    assert err.value.reason == "WRONG_PIN"


def test_pairing_refused_without_activity_stays_a_refusal(fake: MagicMock) -> None:
    """Without pushed frames there is nothing to tell about the machine."""
    fake.pair.return_value = HandshakeResult("@hp5", "ABORTED", None)
    fake.status_history = []
    with pytest.raises(JuraWifiAuthError) as err:
        _client().pair()
    assert err.value.reason == "ABORTED"


def test_pairing_succeeds_even_if_the_machine_was_busy(fake: MagicMock) -> None:
    """Pushed frames next to a successful pairing are of no interest."""
    fake.status_history = [P_MODE_FRAME]
    assert _client().pair() == "a" * 64


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
    # there is no recipe to ask the machine for
    fake.read_pmode_product.assert_not_called()


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
    """Return a discovery reply; its type plate reads SERIAL."""
    return Machine(
        address=address,
        name="Coffeemaker",
        fw="TT237W V06.11",
        hw_id="",
        article_number=article,
        machine_number=1234,
        serial_number=321,
        production_date=datetime.date(2024, 1, 17),
        uchi_production_date=datetime.date(2023, 11, 20),
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
        serial_number=SERIAL,
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
        article_number=19999,
        firmware="TT237W V06.11",
        ef_code=None,
        model_name=None,
        serial_number=SERIAL,
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


@pytest.mark.parametrize(
    ("production_date", "machine_number", "expected"),
    [
        (datetime.date(2024, 1, 17), 1234, "20240117001234"),
        (datetime.date(2024, 1, 17), 7, "20240117000007"),
        (datetime.date(2031, 12, 1), 0xFFFE, "20311201065534"),
        (datetime.date(2024, 1, 17), 0, None),
        (datetime.date(2024, 1, 17), 0xFFFF, None),
        (None, 1234, None),
    ],
)
def test_the_serial_number_is_the_one_on_the_type_plate(
    udp: SimpleNamespace,
    production_date: datetime.date | None,
    machine_number: int,
    expected: str | None,
) -> None:
    """The plate shows the production date and the machine number with six digits.

    An erased machine number reads as all zeros or all ones, and without the
    production date the reply does not hold the serial number at all.
    """
    udp.probe.return_value = dataclasses.replace(
        _machine(), production_date=production_date, machine_number=machine_number
    )
    identity = discover_machine("192.0.2.10")
    assert identity is not None
    assert identity.serial_number == expected


@pytest.mark.parametrize("field", [0, 321, 0xFFFF])
def test_the_field_the_library_calls_serial_number_is_not_used(field: int) -> None:
    """It is another number of the reply, which 0.5.0 and 0.5.1 took by mistake."""
    machine = dataclasses.replace(_machine(), serial_number=field)
    assert type_plate_serial(machine) == SERIAL


E8_SETTING_VALUES = {
    "02": "10",
    "13": "1E",
    "08": "00",
    "09": "02",
    "65": "00",
    "7E": "01",
}


def test_fetch_reads_the_settings_in_the_same_session(fake: MagicMock) -> None:
    """The settings need no session of their own, so no pause for the dongle."""
    fake.read_setting.side_effect = lambda arg, **_: E8_SETTING_VALUES[arg]

    snapshot = _client().fetch(with_settings=True)

    assert snapshot.settings == E8_SETTING_VALUES
    assert [call.args[0] for call in fake.read_setting.call_args_list] == [
        "02",
        "13",
        "08",
        "09",
        "65",
        "7E",
    ]
    assert fake.connect.call_count == 1
    fake.close.assert_called_once()


def test_fetch_leaves_the_settings_alone_unless_asked(fake: MagicMock) -> None:
    """Settings rarely change, so a plain poll does not read them."""
    snapshot = _client().fetch()
    assert snapshot.settings is None
    fake.read_setting.assert_not_called()


def test_a_setting_that_cannot_be_read_is_left_out(fake: MagicMock) -> None:
    """One setting that fails does not cost the others."""

    def read(arg: str, **_) -> str:
        if arg == "09":
            raise ValueError("checksum mismatch")
        if arg == "65":
            return ""  # what a machine without that setting answers
        return E8_SETTING_VALUES[arg]

    fake.read_setting.side_effect = read

    snapshot = _client().fetch(with_settings=True)

    assert snapshot.settings == {"02": "10", "13": "1E", "08": "00", "7E": "01"}


def test_the_settings_are_not_read_on_when_the_machine_stops_answering(
    fake: MagicMock,
) -> None:
    """A machine that ignores the requests does not hold up the poll."""
    asked: list[str] = []

    def read(arg: str, **_) -> str:
        asked.append(arg)
        if arg == "13":
            raise TimeoutError("no reply to '@TM:13'")
        return E8_SETTING_VALUES[arg]

    fake.read_setting.side_effect = read

    snapshot = _client().fetch(with_settings=True)

    assert asked == ["02", "13"]
    assert snapshot.settings == {"02": "10"}
    assert snapshot.total_brews == 20


def test_a_connection_lost_while_reading_the_settings_keeps_the_poll(
    fake: MagicMock,
) -> None:
    """Status and counters were read already; they are not thrown away."""
    fake.read_setting.side_effect = ConnectionResetError("reset by peer")

    snapshot = _client().fetch(with_settings=True)

    assert snapshot.settings == {}
    assert snapshot.total_brews == 20
    fake.close.assert_called_once()


def test_write_setting(fake: MagicMock) -> None:
    """The library writes with its checksum and checks the value; then it is read."""
    fake.write_setting.return_value = "@tm:02"
    fake.read_setting.return_value = "0F"

    assert _client().write_setting("02", "0F") == "0F"

    fake.write_setting.assert_called_once_with("02", "0F", timeout=api.SETTING_TIMEOUT)
    fake.close.assert_called_once()


def test_write_setting_returns_what_the_machine_stores(fake: MagicMock) -> None:
    """The switch-off time is written as 213C and read back as 3C."""
    fake.write_setting.return_value = "@tm:13"
    fake.read_setting.return_value = "3c"
    assert _client().write_setting("13", "213C") == "3C"


def test_write_setting_without_a_readback_returns_the_written_value(
    fake: MagicMock,
) -> None:
    """The write is verified by the library; a failing second read is no failure."""
    fake.write_setting.return_value = "@tm:13"
    fake.read_setting.side_effect = ValueError("checksum mismatch")
    assert _client().write_setting("13", "213c") == "213C"


@pytest.mark.parametrize(
    ("failure", "expected"),
    [
        (ValueError("dongle ACK'd but read-back is '10'"), JuraWifiError),
        (TimeoutError("no reply to '@TM:02,0F00'"), JuraWifiError),
        (ConnectionResetError("reset by peer"), JuraWifiConnectionError),
    ],
)
def test_write_setting_failures(
    fake: MagicMock, failure: Exception, expected: type[Exception]
) -> None:
    """A value the machine did not store is a failed command, a lost session an outage."""
    fake.write_setting.side_effect = failure
    with pytest.raises(expected) as err:
        _client().write_setting("02", "0F")
    if expected is JuraWifiError:
        assert not isinstance(err.value, JuraWifiConnectionError)
    fake.close.assert_called_once()


def test_a_setting_the_machine_refuses_is_reported(fake: MagicMock) -> None:
    """The library returns the refusal of the machine instead of raising."""
    fake.write_setting.return_value = "@an:error"
    with pytest.raises(JuraWifiError, match="refused"):
        _client().write_setting("02", "0F")


@pytest.mark.parametrize(
    ("locked", "method"), [(True, "lock_screen"), (False, "unlock_screen")]
)
def test_set_front_panel_lock(fake: MagicMock, locked: bool, method: str) -> None:
    """The lock and the release are two commands of the library."""
    getattr(fake, method).return_value = "@ts"

    _client().set_front_panel_lock(locked)

    getattr(fake, method).assert_called_once_with()
    fake.close.assert_called_once()


@pytest.mark.parametrize(
    ("failure", "expected"),
    [
        (TimeoutError("no reply to '@TS:01'"), JuraWifiError),
        (ConnectionResetError("reset by peer"), JuraWifiConnectionError),
    ],
)
def test_front_panel_lock_failures(
    fake: MagicMock, failure: Exception, expected: type[Exception]
) -> None:
    """Silence is a failed command, a lost session an outage."""
    fake.lock_screen.side_effect = failure
    with pytest.raises(expected) as err:
        _client().set_front_panel_lock(True)
    if expected is JuraWifiError:
        assert not isinstance(err.value, JuraWifiConnectionError)
    fake.close.assert_called_once()


def test_a_refused_front_panel_lock_is_reported(fake: MagicMock) -> None:
    """An @an:error reply means that the machine did not lock."""
    fake.lock_screen.return_value = "@an:error"
    with pytest.raises(JuraWifiError, match="refused"):
        _client().set_front_panel_lock(True)


def test_brew_options_replace_the_stored_recipe(fake: MagicMock) -> None:
    """Only what is given changes; the rest is what the machine has stored."""
    fake.read_pmode_product.return_value = _stored(
        "espresso", E8_STORED_RECIPES["espresso"]
    )

    _client().brew("espresso", {"ml": 60, "temperature": "high"})

    fake.brew.assert_called_once_with(
        "espresso", retry=True, strength=8, ml=60, temperature="high"
    )


def test_brew_options_without_a_stored_recipe_leave_the_rest_to_the_profile(
    fake: MagicMock,
) -> None:
    """A machine that keeps its recipes to itself gets the factory recipe."""
    _client().brew("espresso", {"ml": 60})
    fake.brew.assert_called_once_with("espresso", retry=True, ml=60)


def _product(name: str):
    return next(p for p in load_profile("EF1120").products if p.name == name)


def test_every_option_of_the_brew_action_has_its_argument() -> None:
    """The actions options are exactly the six that were asked for."""
    assert {option.key: option.argument for option in api.BREW_OPTIONS} == {
        "strength": "strength",
        "water_amount": "ml",
        "temperature": "temperature",
        "milk_foam_time": "milk_foam",
        "milk_break": "milk_break",
        "bypass": "bypass",
    }


def test_the_options_of_a_drink_become_brew_arguments() -> None:
    """Names are case-insensitive and the keys are those of ``JuraClient.brew``."""
    arguments = brew_arguments(
        _product("americano"),
        {"strength": 6, "water_amount": 70, "temperature": "High", "bypass": 45},
    )
    assert arguments == {"strength": 6, "ml": 70, "temperature": "high", "bypass": 45}


def test_the_milk_options_of_a_milk_drink() -> None:
    """The latte macchiato defines foam time and milk break."""
    arguments = brew_arguments(
        _product("latte_macchiato"), {"milk_foam_time": 30, "milk_break": 0}
    )
    assert arguments == {"milk_foam": 30, "milk_break": 0}


def test_no_options_make_no_arguments() -> None:
    """Whatever is not given stays as the machine has it."""
    assert brew_arguments(_product("espresso"), {}) == {}


@pytest.mark.parametrize(
    ("product", "option"),
    [
        ("espresso", "bypass"),
        ("espresso", "milk_foam_time"),
        ("coffee", "milk_break"),
        ("milk_foam", "strength"),
        ("hotwater_portion_normal", "strength"),
        ("cappuccino", "bypass"),
    ],
)
def test_an_option_the_profile_does_not_define_for_the_drink_is_refused(
    product: str, option: str
) -> None:
    """The profile decides which parameters a drink has."""
    with pytest.raises(BrewOptionError) as err:
        brew_arguments(_product(product), {option: 1})
    assert err.value.translation_key == "brew_option_unsupported"
    assert err.value.placeholders == {"option": option}


@pytest.mark.parametrize(
    ("product", "option", "value", "key", "placeholders"),
    [
        # espresso: 15 to 80 ml in steps of 5
        ("espresso", "water_amount", 10, "brew_option_range", {"minimum": "15"}),
        ("espresso", "water_amount", 85, "brew_option_range", {"maximum": "80"}),
        ("espresso", "water_amount", 17, "brew_option_step", {"step": "5"}),
        # latte macchiato: milk break of 0 to 60 s, milk foam of 1 to 45 s
        ("latte_macchiato", "milk_break", 61, "brew_option_range", {"maximum": "60"}),
        ("latte_macchiato", "milk_foam_time", 0, "brew_option_range", {"minimum": "1"}),
        # americano: bypass in steps of 5 ml
        ("americano", "bypass", 42, "brew_option_step", {"step": "5", "minimum": "0"}),
        # the strength is one of the ten levels, the temperature one of three names
        ("espresso", "strength", 11, "brew_option_choice", {"allowed": "1, 2"}),
        ("espresso", "strength", 0, "brew_option_choice", {}),
        ("espresso", "temperature", "boiling", "brew_option_choice", {}),
    ],
)
def test_a_value_that_does_not_fit_the_drink_is_refused(
    product: str,
    option: str,
    value: int | str,
    key: str,
    placeholders: dict[str, str],
) -> None:
    """The range, the step and the items come from the profile of the drink."""
    with pytest.raises(BrewOptionError) as err:
        brew_arguments(_product(product), {option: value})
    assert err.value.translation_key == key
    assert err.value.placeholders["option"] == option
    for name, expected in placeholders.items():
        assert expected in err.value.placeholders[name]


def test_the_choices_are_named_in_the_refusal() -> None:
    """The message lists what is allowed."""
    with pytest.raises(BrewOptionError) as err:
        brew_arguments(_product("espresso"), {"temperature": "boiling"})
    assert err.value.placeholders["allowed"] == "low, normal, high"
    with pytest.raises(BrewOptionError) as err:
        brew_arguments(_product("espresso"), {"strength": 11})
    assert err.value.placeholders["allowed"] == "1, 2, 3, 4, 5, 6, 7, 8, 9, 10"


def test_the_limits_of_a_drink_are_accepted() -> None:
    """The ends of the range are part of it."""
    espresso = _product("espresso")
    assert brew_arguments(espresso, {"water_amount": 15})["ml"] == 15
    assert brew_arguments(espresso, {"water_amount": 80})["ml"] == 80
    assert brew_arguments(espresso, {"strength": 1})["strength"] == 1
    assert brew_arguments(espresso, {"strength": 10})["strength"] == 10


def test_the_profile_extras_of_the_e8() -> None:
    """The E8 declares the lock banks and the predictive maintenance at 80 percent."""
    extras = load_profile_extras(load_profile("EF1120"))
    assert extras.front_panel_lock is True
    assert extras.predictive_thresholds == {
        "cleaning": 80,
        "descale": 80,
        "filter_change": 80,
    }


def test_the_profile_extras_of_a_machine_without_predictive_maintenance() -> None:
    """Thresholds are only there where the profile gives them."""
    profile = next(
        profile
        for profile in iter_profiles()
        if not load_profile_extras(profile).predictive_thresholds
    )
    extras = load_profile_extras(profile)
    assert extras.predictive_thresholds == {}
    assert extras.front_panel_lock is True


def test_the_profile_extras_survive_an_xml_that_cannot_be_read() -> None:
    """The extras are an addition; the machine works without them."""
    profile = dataclasses.replace(load_profile("EF1120"), version="0.0")
    assert load_profile_extras(profile) == ProfileExtras()


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
