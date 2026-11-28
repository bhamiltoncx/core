"""Test the ViCare error event entity."""

from freezegun.api import FrozenDateTimeFactory
import pytest
from pytest_homeassistant.common import MockConfigEntry
from syrupy.assertion import SnapshotAssertion

from homeassistant.components.event import ATTR_EVENT_TYPE
from homeassistant.components.vicare.coordinator import ERROR_EVENTS_EVERY
from homeassistant.const import STATE_UNKNOWN
from homeassistant.core import Event, EventStateChangedData, HomeAssistant, callback
from homeassistant.helpers import entity_registry as er
from homeassistant.helpers.event import async_track_state_change_event

from . import async_poll
from .conftest import MockViCareService

ENTITY_ID = "event.model0_error"
DEVICE_ID = "deviceId0"


@pytest.mark.usefixtures("mock_vicare_error_events")
async def test_history_does_not_fire(hass: HomeAssistant) -> None:
    """Test the errors already in the history fire no event at startup."""
    state = hass.states.get(ENTITY_ID)
    assert state is not None
    assert state.state == STATE_UNKNOWN
    assert state.attributes["event_types"] == ["raised", "cleared"]


async def test_fires_raise_and_clear(
    hass: HomeAssistant,
    mock_vicare_error_events: MockViCareService,
    mock_config_entry: MockConfigEntry,
    freezer: FrozenDateTimeFactory,
) -> None:
    """Test a new error fires when raised and again when cleared."""
    mock_vicare_error_events.add_error_event(
        DEVICE_ID, "F.380", True, "2026-11-28T10:00:00.000Z"
    )
    await async_poll(hass, freezer, mock_config_entry, ERROR_EVENTS_EVERY)

    state = hass.states.get(ENTITY_ID)
    assert state is not None
    assert state.state != STATE_UNKNOWN
    assert state.attributes[ATTR_EVENT_TYPE] == "raised"
    assert state.attributes["error_code"] == "F.380"
    assert state.attributes["error_description"] == "Description of F.380"

    mock_vicare_error_events.add_error_event(
        DEVICE_ID, "F.380", False, "2026-11-28T10:05:00.000Z"
    )
    await async_poll(hass, freezer, mock_config_entry, ERROR_EVENTS_EVERY)

    state = hass.states.get(ENTITY_ID)
    assert state is not None
    assert state.attributes[ATTR_EVENT_TYPE] == "cleared"
    assert state.attributes["error_code"] == "F.380"


async def test_fires_each_event_once_in_order(
    hass: HomeAssistant,
    mock_vicare_error_events: MockViCareService,
    mock_config_entry: MockConfigEntry,
    freezer: FrozenDateTimeFactory,
) -> None:
    """Test an error raised and cleared between two fetches fires both, once."""
    fired: list[tuple[str, str]] = []

    @callback
    def record(event: Event[EventStateChangedData]) -> None:
        new_state = event.data["new_state"]
        assert new_state is not None
        fired.append(
            (
                new_state.attributes[ATTR_EVENT_TYPE],
                new_state.attributes["error_code"],
            )
        )

    async_track_state_change_event(hass, ENTITY_ID, record)

    mock_vicare_error_events.add_error_event(
        DEVICE_ID, "F.357", True, "2026-11-28T10:00:00.000Z"
    )
    mock_vicare_error_events.add_error_event(
        DEVICE_ID, "F.357", False, "2026-11-28T10:02:00.000Z"
    )
    await async_poll(hass, freezer, mock_config_entry, ERROR_EVENTS_EVERY)
    assert fired == [("raised", "F.357"), ("cleared", "F.357")]

    # Further refreshes, including the next history fetch, fire nothing new.
    await async_poll(hass, freezer, mock_config_entry, ERROR_EVENTS_EVERY)
    assert fired == [("raised", "F.357"), ("cleared", "F.357")]


async def test_other_devices_events_are_ignored(
    hass: HomeAssistant,
    mock_vicare_error_events: MockViCareService,
    mock_config_entry: MockConfigEntry,
    freezer: FrozenDateTimeFactory,
) -> None:
    """Test an error of another device on the gateway fires nothing here."""
    mock_vicare_error_events.add_error_event(
        "another-device", "F.380", True, "2026-11-28T10:00:00.000Z"
    )
    await async_poll(hass, freezer, mock_config_entry, ERROR_EVENTS_EVERY)

    state = hass.states.get(ENTITY_ID)
    assert state is not None
    assert state.state == STATE_UNKNOWN


@pytest.mark.usefixtures("mock_vicare_error_events")
async def test_all_entities(
    hass: HomeAssistant,
    snapshot: SnapshotAssertion,
    mock_config_entry: MockConfigEntry,
    entity_registry: er.EntityRegistry,
) -> None:
    """Test the event entities."""
    entries = er.async_entries_for_config_entry(
        entity_registry, mock_config_entry.entry_id
    )
    for entry in entries:
        if entry.domain == "event":
            assert entry == snapshot(name=f"{entry.entity_id}-entry")
            assert hass.states.get(entry.entity_id) == snapshot(
                name=f"{entry.entity_id}-state"
            )
