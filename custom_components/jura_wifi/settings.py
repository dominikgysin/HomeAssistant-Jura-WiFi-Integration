"""The machine settings that become entities, and how their values are read."""

from __future__ import annotations

from jura_connect import SettingDef, SettingItem

PLATFORM_NUMBER = "number"
PLATFORM_SELECT = "select"
PLATFORM_SWITCH = "switch"

# The settings that have a name in the translations, by the kind of entity that they
# become: the hardness is a number on most machines and a list of steps on a few. A
# setting that a machine declares besides these is named the way its profile names it.
KNOWN_SETTINGS: dict[str, frozenset[str]] = {
    PLATFORM_NUMBER: frozenset({"hardness"}),
    PLATFORM_SELECT: frozenset(
        {"hardness", "auto_off", "units", "language", "brewing_mode"}
    ),
    PLATFORM_SWITCH: frozenset({"quality_assistant"}),
}

# The names of the two items of a setting that is shown as a switch.
ON_ITEMS = frozenset({"on", "active", "enabled", "yes"})
OFF_ITEMS = frozenset({"off", "inactive", "disabled", "no"})


def on_off_items(definition: SettingDef) -> tuple[SettingItem, SettingItem] | None:
    """Return the items for on and for off, if the setting has exactly these two."""
    if len(definition.items) != 2:
        return None
    on = next((item for item in definition.items if item.name in ON_ITEMS), None)
    off = next((item for item in definition.items if item.name in OFF_ITEMS), None)
    if on is None or off is None:
        return None
    return on, off


def setting_platform(definition: SettingDef) -> str | None:
    """Say which kind of entity a setting becomes, or ``None`` if it gets none."""
    if definition.kind == "step_slider":
        has_range = definition.minimum is not None and definition.maximum is not None
        return PLATFORM_NUMBER if has_range else None
    if not definition.items:
        return None
    if on_off_items(definition) is not None:
        return PLATFORM_SWITCH
    return PLATFORM_SELECT


def setting_key(definition: SettingDef) -> str:
    """Return the key of the entity of a setting."""
    return f"setting_{definition.name}"


def setting_translation(definition: SettingDef) -> tuple[str, dict[str, str]]:
    """Return the translation key of the entity and the placeholders of its name."""
    known = KNOWN_SETTINGS.get(setting_platform(definition) or "", frozenset())
    if definition.name in known:
        return setting_key(definition), {}
    return "setting_other", {"setting": definition.raw_name}


def stored_item(definition: SettingDef, raw: str | None) -> SettingItem | None:
    """Return the item that the value read from the machine stands for.

    The library matches an empty value with the first item, so it is not asked then.
    """
    if not raw:
        return None
    return definition.item_from_hex(raw)


def stored_number(definition: SettingDef, raw: str | None) -> int | None:
    """Return the value of a slider, or ``None`` if the machine's answer is no value."""
    if not raw:
        return None
    try:
        number = int(raw, 16)
    except ValueError:
        return None
    low, high = definition.minimum, definition.maximum
    if (low is not None and number < low) or (high is not None and number > high):
        return None
    return number


def number_to_wire(definition: SettingDef, number: int) -> str:
    """Return the value of a slider the way the machine takes it."""
    width = len(definition.mask) if definition.mask else 2
    return f"{number:0{width}X}"
