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
    JuraWifiBusy,
    JuraWifiClient,
    JuraWifiError,
    MachineActivity,
)
from custom_components.jura_wifi.const import (
    CONF_AUTH_HASH,
    CONF_CONN_ID,
    CONF_ENABLE_BREWING,
    CONF_ENABLE_MAINTENANCE,
    DOMAIN,
)
from homeassistant.const import CONF_HOST, CONF_PORT
from homeassistant.core import HomeAssistant
from homeassistant.helpers import entity_registry as er

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
    """The E8 has no coffee system rinse; nothing is sent."""
    simulator = machine(allow_process=True)
    client = _paired(simulator)
    _commands(simulator)

    with pytest.raises(JuraWifiError, match="does not declare"):
        client.start_process("coffee_rinse")

    assert not [c for c in _commands(simulator) if c.startswith("@TG:2")]


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

    for key in ("brew_cappuccino", "start_cappu_rinse", "cancel"):
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
    assert "@TG:FF" in received
    # In this order: the drink, then the program, then the cancel.
    assert (
        received.index(PERSONAL_CAPPUCCINO)
        < received.index("@TG:23")
        < received.index("@TG:FF")
    )

    assert await hass.config_entries.async_unload(entry.entry_id)
