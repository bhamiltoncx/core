"""Test for ViCare."""

from typing import Final

from freezegun.api import FrozenDateTimeFactory
from pytest_homeassistant.common import MockConfigEntry, async_fire_time_changed

from homeassistant.const import CONF_CLIENT_ID, CONF_PASSWORD, CONF_USERNAME
from homeassistant.core import HomeAssistant

MODULE = "homeassistant.components.vicare"

ENTRY_CONFIG: Final[dict[str, str]] = {
    CONF_USERNAME: "foo@bar.com",
    CONF_PASSWORD: "1234",
    CONF_CLIENT_ID: "5678",
}

MOCK_MAC = "B874241B7B9"


async def setup_integration(hass: HomeAssistant, config_entry: MockConfigEntry) -> None:
    """Fixture for setting up the component."""
    config_entry.add_to_hass(hass)

    await hass.config_entries.async_setup(config_entry.entry_id)
    await hass.async_block_till_done()


async def async_poll(
    hass: HomeAssistant,
    freezer: FrozenDateTimeFactory,
    config_entry: MockConfigEntry,
    refreshes: int = 1,
) -> None:
    """Advance time through the given number of coordinator refreshes."""
    coordinator = config_entry.runtime_data.devices[0].coordinator
    assert coordinator is not None
    assert coordinator.update_interval is not None
    for _ in range(refreshes):
        freezer.tick(coordinator.update_interval)
        async_fire_time_changed(hass)
        await hass.async_block_till_done(wait_background_tasks=True)
