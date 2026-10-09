"""The brand images are where Home Assistant looks for them and have the right size."""

from __future__ import annotations

from pathlib import Path
import struct

import pytest
from pytest_homeassistant_custom_component.typing import ClientSessionGenerator

from custom_components.jura_wifi.const import DOMAIN
from homeassistant.core import HomeAssistant
from homeassistant.setup import async_setup_component

BRAND = Path(__file__).parents[1] / "custom_components" / DOMAIN / "brand"

# The names that Home Assistant accepts; a file with any other name is ignored.
ALLOWED = {
    "icon.png",
    "dark_icon.png",
    "icon@2x.png",
    "dark_icon@2x.png",
    "logo.png",
    "dark_logo.png",
    "logo@2x.png",
    "dark_logo@2x.png",
}

# The file that answers each request: the image itself, or the one Home Assistant
# falls back to when there is none. There is no dark icon: the badge looks the same
# on both themes.
SERVED_FROM = {
    "icon.png": "icon.png",
    "icon@2x.png": "icon@2x.png",
    "logo.png": "logo.png",
    "logo@2x.png": "logo@2x.png",
    "dark_logo.png": "dark_logo.png",
    "dark_logo@2x.png": "dark_logo@2x.png",
    "dark_icon.png": "icon.png",
    "dark_icon@2x.png": "icon@2x.png",
}


def _png_size(path: Path) -> tuple[int, int]:
    """Return the size of a PNG image from its header."""
    header = path.read_bytes()[:24]
    assert header[:8] == b"\x89PNG\r\n\x1a\n", f"{path.name} is not a PNG file"
    width, height = struct.unpack(">II", header[16:24])
    return width, height


def test_only_names_that_home_assistant_knows() -> None:
    """A misspelled file would be ignored without a hint."""
    assert {path.name for path in BRAND.iterdir()} <= ALLOWED


def test_the_icons_are_square_in_the_two_resolutions() -> None:
    """The icon is 256 px, the high-resolution one 512 px."""
    assert _png_size(BRAND / "icon.png") == (256, 256)
    assert _png_size(BRAND / "icon@2x.png") == (512, 512)


@pytest.mark.parametrize("name", ["logo", "dark_logo"])
def test_the_logos_are_wide_and_the_size_of_the_specification(name: str) -> None:
    """The shortest side is up to 256 px, up to 512 px for the high resolution."""
    for suffix, shortest in (("", range(128, 257)), ("@2x", range(256, 513))):
        width, height = _png_size(BRAND / f"{name}{suffix}.png")
        assert width > height, name
        assert height in shortest, f"{name}{suffix}.png is {height} px high"


@pytest.mark.parametrize("image", sorted(SERVED_FROM))
async def test_home_assistant_serves_the_brand_images(
    hass: HomeAssistant, hass_client: ClientSessionGenerator, image: str
) -> None:
    """Ask Home Assistant itself, as the frontend does."""
    # the brands integration exists since Home Assistant 2026.3
    pytest.importorskip("homeassistant.components.brands")
    assert await async_setup_component(hass, "brands", {})
    client = await hass_client()

    # without the placeholder a missing image is a 404 instead of a grey box
    response = await client.get(
        f"/api/brands/integration/{DOMAIN}/{image}?placeholder=no"
    )

    assert response.status == 200
    assert response.content_type == "image/png"
    assert await response.read() == (BRAND / SERVED_FROM[image]).read_bytes()
