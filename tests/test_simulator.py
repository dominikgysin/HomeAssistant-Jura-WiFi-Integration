"""End-to-end tests against the dongle simulator that ships with the library.

The simulator speaks the real wire protocol over TCP (framing, obfuscation,
handshake, pairing), so these tests cover what the other tests replace by mocks:
the sequence of commands that actually reaches the machine.
"""

from __future__ import annotations

from collections.abc import Callable, Iterator

from jura_connect.simulator import Simulator, SimulatorConfig
import pytest
from pytest_homeassistant_custom_component.common import MockConfigEntry

from custom_components.jura_wifi import api
from custom_components.jura_wifi.api import (
    JuraWifiAuthError,
    JuraWifiBusy,
    JuraWifiClient,
    JuraWifiConnectionError,
    JuraWifiError,
    MachineActivity,
)
from custom_components.jura_wifi.const import (
    CONF_AUTH_HASH,
    CONF_CONN_ID,
    CONF_ENABLE_BREWING,
    CONF_ENABLE_MAINTENANCE,
    CONF_ENABLE_SETTINGS,
    DOMAIN,
    SERVICE_BREW,
)
from homeassistant.config_entries import ConfigEntryState
from homeassistant.const import CONF_HOST, CONF_PORT
from homeassistant.core import HomeAssistant
from homeassistant.helpers import entity_registry as er

from .common import E8_SETTINGS, call, entity_id, state
from .conftest import E8_STORED_RECIPES, P_MODE_FRAME

CONN_ID = "ha-simulated"
QUIET_PROCESSES = ("@TG:21", "@TG:22", "@TG:23", "@TG:24", "@TG:25", "@TG:26")

# What the simulated machine has stored for its drinks (keyed by product code).
STORED = {int(blob[:2], 16): blob for blob in E8_STORED_RECIPES.values()}

# The recipes that reach the machine: the cappuccino as adjusted at the display
# (20 s of foam, byte 5) and the factory one (14 s) for a drink without a stored recipe.
PERSONAL_CAPPUCCINO = "@TP:0400080C001401000100000000000000"
FACTORY_CAPPUCCINO = "@TP:0400080C000E01000100000000000000"
FACTORY_COFFEE = "@TP:03000514000001000100000000000000"


@pytest.fixture(autouse=True)
def quick_sessions(monkeypatch: pytest.MonkeyPatch) -> None:
    """Do not wait for the pause the real dongle needs between two sessions."""
    monkeypatch.setattr(api, "SESSION_GAP_SECONDS", 0.0)
    monkeypatch.setattr(api, "READ_TIMEOUT", 1.5)
    api._GATES.clear()


@pytest.fixture
def machine() -> Iterator[Callable[..., Simulator]]:
    """Start simulated machines; they are stopped after the test.

    The progress frames that a machine pushes after a start are switched off:
    the client hangs up right after the acknowledgement, as it should, and the
    simulator would log a reset connection when it pushes them anyway.
    """
    started: list[Simulator] = []

    def start(**config) -> Simulator:
        settings = {
            "status_interval": 0.1,
            "brew_script": (),
            "process_sequences": dict.fromkeys(QUIET_PROCESSES, ()),
            **config,
        }
        simulator = Simulator(SimulatorConfig(**settings))
        simulator.start()
        started.append(simulator)
        return simulator

    yield start
    for simulator in started:
        simulator.stop()


def _address(simulator: Simulator) -> tuple[str, int]:
    host, port = simulator.address
    return host, port


def _client(simulator: Simulator, auth_hash: str = "") -> JuraWifiClient:
    host, port = _address(simulator)
    return JuraWifiClient(host, port, CONN_ID, auth_hash, "", "EF1120")


def _paired(simulator: Simulator) -> JuraWifiClient:
    return _client(simulator, _client(simulator).pair())


def _commands(simulator: Simulator) -> list[str]:
    """Return the commands received so far and forget them.

    The handshakes and the empty frame that ends every session are left out.
    """
    received = [
        frame.decode("ascii", errors="replace").rstrip("\r\n")
        for frame in simulator.sent_commands
    ]
    simulator.sent_commands.clear()
    return [command for command in received if command and command[:4] != "@HP:"]


def test_pairing_and_polling(machine: Callable[..., Simulator]) -> None:
    """Pairing hands out a hash that the next session authenticates with."""
    simulator = machine()
    auth_hash = _client(simulator).pair()
    assert len(auth_hash) == 64

    snapshot = _client(simulator, auth_hash).fetch()

    assert snapshot.total_brews == 3229
    assert snapshot.active_alerts == {"cleaning_alert", "no_beans"}


def test_brewing_uses_the_recipe_stored_on_the_machine(
    machine: Callable[..., Simulator],
) -> None:
    """The cappuccino comes out with the foam time that was set at the display."""
    simulator = machine(allow_brew=True, pmode_products=dict(STORED))
    client = _paired(simulator)
    _commands(simulator)

    client.brew("cappuccino")

    commands = _commands(simulator)
    assert commands == ["@TM:41,04", PERSONAL_CAPPUCCINO]


def test_brewing_without_stored_recipes_uses_the_factory_recipe(
    machine: Callable[..., Simulator],
) -> None:
    """A machine that answers 'no product programming' still brews."""
    simulator = machine(allow_brew=True, pmode_products=None)
    client = _paired(simulator)
    _commands(simulator)

    client.brew("cappuccino")

    assert _commands(simulator) == ["@TM:41,04", FACTORY_CAPPUCCINO]


def test_brewing_a_drink_the_machine_has_no_recipe_for(
    machine: Callable[..., Simulator],
) -> None:
    """The recipes of the other drinks stay what they are."""
    simulator = machine(allow_brew=True, pmode_products={4: STORED[4]})
    client = _paired(simulator)
    _commands(simulator)

    client.brew("coffee")

    assert _commands(simulator) == ["@TM:41,03", FACTORY_COFFEE]


def test_the_machine_refusing_a_brew_is_reported(
    machine: Callable[..., Simulator],
) -> None:
    """The simulator answers @an:error to a brew it was not told to allow."""
    simulator = machine()
    client = _paired(simulator)

    with pytest.raises(JuraWifiError, match="rejected"):
        client.brew("espresso")


def test_starting_a_maintenance_program(machine: Callable[..., Simulator]) -> None:
    """Only the start verb is sent; the echo of the machine acknowledges it."""
    simulator = machine(allow_process=True)
    client = _paired(simulator)
    _commands(simulator)

    client.start_process("cappu_rinse")

    assert _commands(simulator) == ["@TG:23"]


@pytest.mark.parametrize(
    ("program", "verb"),
    [
        ("cleaning", "@TG:24"),
        ("descale", "@TG:25"),
        ("filter_change", "@TG:26"),
        ("cappu_clean", "@TG:21"),
    ],
)
def test_every_program_of_the_e8_has_its_own_start_verb(
    machine: Callable[..., Simulator], program: str, verb: str
) -> None:
    """The verbs come from the profile of the machine."""
    simulator = machine(allow_process=True)
    client = _paired(simulator)
    _commands(simulator)

    client.start_process(program)

    assert _commands(simulator) == [verb]


def test_a_program_the_machine_refuses_is_reported(
    machine: Callable[..., Simulator],
) -> None:
    """A start that is not echoed must not look like a running program."""
    simulator = machine()
    client = _paired(simulator)

    with pytest.raises(JuraWifiError, match="refused to start"):
        client.start_process("cleaning")


def test_a_program_the_machine_does_not_have(machine: Callable[..., Simulator]) -> None:
    """A machine without a milk system has no milk system cleaning; nothing is sent."""
    simulator = machine(allow_process=True)
    host, port = _address(simulator)
    auth_hash = _client(simulator).pair()
    client = JuraWifiClient(host, port, CONN_ID, auth_hash, "", "EF1089")
    _commands(simulator)

    with pytest.raises(JuraWifiError, match="does not declare"):
        client.start_process("cappu_clean")

    assert not [c for c in _commands(simulator) if c.startswith("@TG:2")]


def test_the_coffee_system_rinse_of_the_e8(machine: Callable[..., Simulator]) -> None:
    """The profile of the E8 does not list the rinse, yet the verb is sent."""
    simulator = machine(allow_process=True)
    client = _paired(simulator)
    _commands(simulator)

    client.start_process("coffee_rinse")

    assert _commands(simulator) == ["@TG:22"]


def test_a_machine_that_ignores_the_coffee_system_rinse_is_reported(
    machine: Callable[..., Simulator],
) -> None:
    """A machine that does not know the verb stays silent: a failed command only."""
    simulator = machine()
    client = _paired(simulator)

    with pytest.raises(JuraWifiError, match="did not answer") as err:
        client.start_process("coffee_rinse")

    assert not isinstance(err.value, api.JuraWifiConnectionError)


def test_cancelling(machine: Callable[..., Simulator]) -> None:
    """The cancel verb reaches the machine and is acknowledged."""
    simulator = machine(allow_process=True)
    client = _paired(simulator)
    client.start_process("cappu_rinse")
    _commands(simulator)

    client.cancel_step()

    assert _commands(simulator) == ["@TG:FF"]


def test_a_machine_in_its_menu_is_busy_not_offline(
    machine: Callable[..., Simulator],
) -> None:
    """In the menu the machine pushes its state instead of status frames."""
    simulator = machine(status_interval=0)
    auth_hash = _client(simulator).pair()
    simulator.config.handshake_pushes = (P_MODE_FRAME,)

    with pytest.raises(JuraWifiBusy) as err:
        _client(simulator, auth_hash).fetch()

    assert err.value.activity == MachineActivity("programming")


def test_pairing_is_refused_while_the_machine_is_in_its_menu(
    machine: Callable[..., Simulator],
) -> None:
    """The machine cannot show the prompt in its menu, so the user is told."""
    simulator = machine(status_interval=0, handshake_pushes=(P_MODE_FRAME,))
    # the first pairing is accepted, the second one for the same identifier is refused
    _client(simulator).pair()

    with pytest.raises(JuraWifiBusy) as err:
        _client(simulator).pair()

    assert err.value.activity == MachineActivity("programming")


def test_pairing_is_refused_without_the_machine_being_busy(
    machine: Callable[..., Simulator],
) -> None:
    """Without pushed frames the refusal stays a plain refusal."""
    simulator = machine()
    _client(simulator).pair()

    with pytest.raises(JuraWifiError) as err:
        _client(simulator).pair()

    assert not isinstance(err.value, JuraWifiBusy)


async def test_the_integration_controls_the_simulated_machine(
    hass: HomeAssistant,
    machine: Callable[..., Simulator],
    mock_config_entry: MockConfigEntry,
) -> None:
    """Set up the entry against the simulator and press every kind of button."""
    simulator = machine(
        allow_brew=True, allow_process=True, pmode_products=dict(STORED)
    )
    host, port = _address(simulator)
    auth_hash = await hass.async_add_executor_job(_client(simulator).pair)
    entry = MockConfigEntry(
        domain=DOMAIN,
        title=mock_config_entry.title,
        unique_id=host,
        data={
            **mock_config_entry.data,
            CONF_HOST: host,
            CONF_PORT: port,
            CONF_CONN_ID: CONN_ID,
            CONF_AUTH_HASH: auth_hash,
        },
        options={
            **mock_config_entry.options,
            CONF_ENABLE_BREWING: True,
            CONF_ENABLE_MAINTENANCE: True,
        },
    )
    entry.add_to_hass(hass)
    assert await hass.config_entries.async_setup(entry.entry_id)
    await hass.async_block_till_done(wait_background_tasks=True)

    registry = er.async_get(hass)
    sensor = registry.async_get_entity_id(
        "sensor", DOMAIN, f"{entry.entry_id}_total_brews"
    )
    assert sensor is not None
    state = hass.states.get(sensor)
    assert state is not None
    assert state.state == "3229"
    _commands(simulator)

    for key in ("brew_cappuccino", "start_cappu_rinse", "start_coffee_rinse", "cancel"):
        button = registry.async_get_entity_id(
            "button", DOMAIN, f"{entry.entry_id}_{key}"
        )
        assert button is not None, key
        await hass.services.async_call(
            "button", "press", {"entity_id": button}, blocking=True
        )
        await hass.async_block_till_done(wait_background_tasks=True)

    received = _commands(simulator)
    assert PERSONAL_CAPPUCCINO in received
    assert "@TG:23" in received
    assert "@TG:22" in received
    assert "@TG:FF" in received
    # In this order: the drink, then the programs, then the cancel.
    assert (
        received.index(PERSONAL_CAPPUCCINO)
        < received.index("@TG:23")
        < received.index("@TG:22")
        < received.index("@TG:FF")
    )

    assert await hass.config_entries.async_unload(entry.entry_id)


# The machine settings, the lock of the front panel and brewing with parameters,
# as they reach the machine. The real machine has not been asked any of this yet.


def test_the_settings_are_read_in_the_session_of_the_poll(
    machine: Callable[..., Simulator],
) -> None:
    """All six settings of the E8 come with the counters, in one session."""
    simulator = machine(settings=dict(E8_SETTINGS))
    client = _paired(simulator)
    _commands(simulator)

    snapshot = client.fetch(with_settings=True)

    assert snapshot.settings == E8_SETTINGS
    reads = [command for command in _commands(simulator) if command[:4] == "@TM:"]
    assert reads == ["@TM:02", "@TM:13", "@TM:08", "@TM:09", "@TM:65", "@TM:7E"]


def test_a_poll_does_not_read_the_settings_unless_asked(
    machine: Callable[..., Simulator],
) -> None:
    """Settings rarely change; the usual poll leaves them alone."""
    simulator = machine(settings=dict(E8_SETTINGS))
    client = _paired(simulator)
    _commands(simulator)

    snapshot = client.fetch()

    assert snapshot.settings is None
    assert not [c for c in _commands(simulator) if c[:4] == "@TM:"]


def test_a_setting_is_written_with_its_checksum_and_read_back(
    machine: Callable[..., Simulator],
) -> None:
    """Lock, write, release, check: the hardness goes from 16 to 12."""
    simulator = machine(settings=dict(E8_SETTINGS))
    client = _paired(simulator)
    _commands(simulator)

    assert client.write_setting("02", "0C") == "0C"

    commands = _commands(simulator)
    assert commands[:3] == ["@TS:01", "@TM:02,0CFE", "@TS:00"]
    assert set(commands[3:]) == {"@TM:02"}
    assert simulator.config.settings["02"] == "0C"
    assert simulator.config.screen_locked is False


def test_the_switch_off_time_is_written_with_its_marker_bytes(
    machine: Callable[..., Simulator],
) -> None:
    """One hour is 213C on the wire."""
    simulator = machine(settings=dict(E8_SETTINGS))
    client = _paired(simulator)
    _commands(simulator)

    client.write_setting("13", "213C")

    assert _commands(simulator)[1] == "@TM:13,213C96"
    assert simulator.config.settings["13"] == "213C"


def test_the_front_panel_is_locked_and_released(
    machine: Callable[..., Simulator],
) -> None:
    """The commands of the Remote Screen and Release Keys banks."""
    simulator = machine()
    client = _paired(simulator)
    _commands(simulator)

    client.set_front_panel_lock(True)
    assert _commands(simulator) == ["@TS:01"]
    assert simulator.config.screen_locked is True

    client.set_front_panel_lock(False)
    assert _commands(simulator) == ["@TS:00"]
    assert simulator.config.screen_locked is False


@pytest.mark.parametrize(
    ("product", "options", "read", "sent"),
    [
        # the foam time of the cappuccino, 20 s as stored, becomes 25 s
        (
            "cappuccino",
            {"milk_foam": 25},
            "@TM:41,04",
            "@TP:0400080C001901000100000000000000",
        ),
        # strength 3, 100 ml and the high temperature replace what is stored
        (
            "cappuccino",
            {"ml": 100, "temperature": "high", "strength": 3},
            "@TM:41,04",
            "@TP:04000314001402000100000000000000",
        ),
        # foam 30 s and a break of 10 s instead of 33 s and 20 s
        (
            "latte_macchiato",
            {"milk_foam": 30, "milk_break": 10},
            "@TM:41,07",
            "@TP:07000809001E020001000A0000000000",
        ),
    ],
)
def test_brewing_with_options_changes_only_those_parameters(
    machine: Callable[..., Simulator],
    product: str,
    options: dict[str, int | str],
    read: str,
    sent: str,
) -> None:
    """Everything that is not given stays as stored on the machine."""
    simulator = machine(allow_brew=True, pmode_products=dict(STORED))
    client = _paired(simulator)
    _commands(simulator)

    client.brew(product, options)

    assert _commands(simulator) == [read, sent]


def test_a_dongle_that_aborts_the_handshake_is_no_pairing_problem(
    machine: Callable[..., Simulator],
) -> None:
    """The machine says no, for a reason of its own; the credentials are fine."""
    simulator = machine()
    _paired(simulator)
    host, port = _address(simulator)
    # the dongle holds a hash for this identifier, and the client asks as if it were new
    asking_as_new = JuraWifiClient(host, port, CONN_ID, "", "", "EF1120")

    with pytest.raises(JuraWifiConnectionError, match="ABORTED") as err:
        asking_as_new.fetch()

    assert not isinstance(err.value, JuraWifiAuthError)


def test_wrong_credentials_are_an_auth_failure(
    machine: Callable[..., Simulator],
) -> None:
    """A wrong hash and a wrong PIN are what pairing again can repair."""
    simulator = machine(pin="1234")
    host, port = _address(simulator)
    auth_hash = JuraWifiClient(host, port, CONN_ID, "", "1234", "EF1120").pair()

    with pytest.raises(JuraWifiAuthError, match="WRONG_HASH"):
        JuraWifiClient(host, port, CONN_ID, "AB" * 32, "1234", "EF1120").fetch()
    with pytest.raises(JuraWifiAuthError, match="WRONG_PIN"):
        JuraWifiClient(host, port, CONN_ID, auth_hash, "9999", "EF1120").fetch()
    JuraWifiClient(host, port, CONN_ID, auth_hash, "1234", "EF1120").fetch()


async def _entry_for(
    hass: HomeAssistant,
    simulator: Simulator,
    template: MockConfigEntry,
    *,
    auth_hash: str | None = None,
    **options: bool,
) -> MockConfigEntry:
    """Add an entry for a simulated machine; by default it is paired."""
    host, port = _address(simulator)
    if auth_hash is None:
        auth_hash = await hass.async_add_executor_job(_client(simulator).pair)
    entry = MockConfigEntry(
        domain=DOMAIN,
        title=template.title,
        unique_id=host,
        data={
            **template.data,
            CONF_HOST: host,
            CONF_PORT: port,
            CONF_CONN_ID: CONN_ID,
            CONF_AUTH_HASH: auth_hash,
        },
        options={**template.options, **options},
    )
    entry.add_to_hass(hass)
    await hass.config_entries.async_setup(entry.entry_id)
    await hass.async_block_till_done(wait_background_tasks=True)
    return entry


async def test_the_integration_changes_the_settings_of_the_simulated_machine(
    hass: HomeAssistant,
    machine: Callable[..., Simulator],
    mock_config_entry: MockConfigEntry,
) -> None:
    """Read the settings, change one of each kind, lock the panel and brew."""
    simulator = machine(
        settings=dict(E8_SETTINGS), allow_brew=True, pmode_products=dict(STORED)
    )
    entry = await _entry_for(
        hass,
        simulator,
        mock_config_entry,
        **{CONF_ENABLE_SETTINGS: True, CONF_ENABLE_BREWING: True},
    )
    assert entry.state is ConfigEntryState.LOADED
    assert float(state(hass, "number", entry, "setting_hardness").state) == 16
    assert state(hass, "select", entry, "setting_auto_off").state == "30min"
    assert state(hass, "select", entry, "setting_language").state == "english"
    assert state(hass, "switch", entry, "setting_quality_assistant").state == "on"
    assert state(hass, "switch", entry, "front_panel_lock").state == "off"
    _commands(simulator)

    await call(
        hass,
        "number",
        "set_value",
        entity_id(hass, "number", entry, "setting_hardness"),
        value=12,
    )
    await hass.async_block_till_done(wait_background_tasks=True)
    assert simulator.config.settings["02"] == "0C"
    assert float(state(hass, "number", entry, "setting_hardness").state) == 12

    await call(
        hass,
        "select",
        "select_option",
        entity_id(hass, "select", entry, "setting_auto_off"),
        option="1h",
    )
    await hass.async_block_till_done(wait_background_tasks=True)
    assert simulator.config.settings["13"] == "213C"
    assert state(hass, "select", entry, "setting_auto_off").state == "1h"

    await call(
        hass,
        "switch",
        "turn_off",
        entity_id(hass, "switch", entry, "setting_quality_assistant"),
    )
    await hass.async_block_till_done(wait_background_tasks=True)
    assert simulator.config.settings["7E"] == "00"
    assert state(hass, "switch", entry, "setting_quality_assistant").state == "off"

    await call(
        hass, "switch", "turn_on", entity_id(hass, "switch", entry, "front_panel_lock")
    )
    await hass.async_block_till_done(wait_background_tasks=True)
    assert simulator.config.screen_locked is True

    await call(
        hass,
        DOMAIN,
        SERVICE_BREW,
        entity_id(hass, "button", entry, "brew_cappuccino"),
        milk_foam_time=25,
    )
    await hass.async_block_till_done(wait_background_tasks=True)
    assert "@TP:0400080C001901000100000000000000" in _commands(simulator)

    assert await hass.config_entries.async_unload(entry.entry_id)


async def test_a_dongle_that_aborts_the_session_leaves_the_entry_alone(
    hass: HomeAssistant,
    machine: Callable[..., Simulator],
    mock_config_entry: MockConfigEntry,
) -> None:
    """The entry stays set up and offline; no pairing is asked for."""
    simulator = machine()
    _paired(simulator)
    entry = await _entry_for(hass, simulator, mock_config_entry, auth_hash="")

    assert entry.state is ConfigEntryState.LOADED
    assert state(hass, "sensor", entry, "status").state == "offline"
    assert not hass.config_entries.flow.async_progress_by_handler(DOMAIN)

    assert await hass.config_entries.async_unload(entry.entry_id)


async def test_a_dongle_that_rejects_the_credentials_asks_for_pairing(
    hass: HomeAssistant,
    machine: Callable[..., Simulator],
    mock_config_entry: MockConfigEntry,
) -> None:
    """A wrong hash is what the pairing repairs."""
    simulator = machine()
    _paired(simulator)
    entry = await _entry_for(hass, simulator, mock_config_entry, auth_hash="AB" * 32)

    assert entry.state is ConfigEntryState.SETUP_ERROR
    flows = hass.config_entries.flow.async_progress_by_handler(DOMAIN)
    assert [flow["context"]["source"] for flow in flows] == ["reauth"]
