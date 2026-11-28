"""Test the ViCare error event entity."""

from typing import Any
from unittest.mock import patch

from freezegun.api import FrozenDateTimeFactory
import pytest
from pytest_homeassistant.common import (
    MockConfigEntry,
    mock_restore_cache_with_extra_data,
)
from syrupy.assertion import SnapshotAssertion

from homeassistant.components.event import ATTR_EVENT_TYPE
from homeassistant.components.vicare.coordinator import ERROR_EVENTS_EVERY
from homeassistant.const import STATE_UNKNOWN
from homeassistant.core import (
    Event,
    EventStateChangedData,
    HomeAssistant,
    State,
    callback,
)
from homeassistant.helpers import entity_registry as er
from homeassistant.helpers.event import async_track_state_change_event

from . import MODULE, async_poll, setup_integration
from .conftest import Fixture, MockPyViCare, MockViCareService

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
    assert state.attributes["error_time"] == "2026-11-28T10:00:00+00:00"

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


def _track_fired(hass: HomeAssistant) -> list[tuple[str, str, str]]:
    """Record the event type, code and time of each event the entity fires."""
    fired: list[tuple[str, str, str]] = []

    @callback
    def record(event: Event[EventStateChangedData]) -> None:
        new_state = event.data["new_state"]
        assert new_state is not None
        # The first state is the restored one, not a fired event.
        if event.data["old_state"] is None:
            return
        fired.append(
            (
                new_state.attributes[ATTR_EVENT_TYPE],
                new_state.attributes["error_code"],
                new_state.attributes["error_time"],
            )
        )

    async_track_state_change_event(hass, ENTITY_ID, record)
    return fired


async def _setup_with_restored_event(
    hass: HomeAssistant,
    config_entry: MockConfigEntry,
    last_event_attributes: dict[str, Any],
) -> None:
    """Set up the heat pump after a restart, its last fired event restored."""
    mock_restore_cache_with_extra_data(
        hass,
        (
            (
                State(ENTITY_ID, "2026-11-21T22:07:30.000+00:00"),
                {
                    "last_event_type": "cleared",
                    "last_event_attributes": last_event_attributes,
                },
            ),
        ),
    )
    vicare = MockPyViCare(
        [
            Fixture(
                {"type:heatpump"},
                "vicare/Vitocal250A.json",
                error_events="vicare/Vitodens100_error_events.json",
            )
        ]
    )
    with (
        patch(
            "homeassistant.helpers.config_entry_oauth2_flow.OAuth2Session.async_ensure_token_valid",
        ),
        patch(f"{MODULE}.PyViCare", return_value=vicare),
    ):
        await setup_integration(hass, config_entry)


async def test_fires_events_missed_while_stopped(
    hass: HomeAssistant,
    mock_config_entry: MockConfigEntry,
    freezer: FrozenDateTimeFactory,
) -> None:
    """Test the errors that came after the last fired one fire after startup.

    The entity last fired the clear of the November 21 evening fault. The
    November 26 fault came and went while Home Assistant was stopped.
    """
    fired = _track_fired(hass)
    await _setup_with_restored_event(
        hass,
        mock_config_entry,
        {
            "error_code": "F.380",
            "error_description": "Flame loss immediately after flame formation",
            "error_time": "2026-11-21T22:06:55+00:00",
        },
    )
    # They fire with the first refresh after the entity was added.
    assert fired == []
    await async_poll(hass, freezer, mock_config_entry)
    assert fired == [
        ("raised", "F.380", "2026-11-26T01:17:32+00:00"),
        ("cleared", "F.380", "2026-11-26T15:29:59+00:00"),
    ]

    # The next history fetch fires them no second time.
    await async_poll(hass, freezer, mock_config_entry, ERROR_EVENTS_EVERY)
    assert len(fired) == 2


async def test_restored_event_without_time_fires_nothing(
    hass: HomeAssistant,
    mock_config_entry: MockConfigEntry,
    freezer: FrozenDateTimeFactory,
) -> None:
    """Test a restored event without its error time replays no history."""
    fired = _track_fired(hass)
    await _setup_with_restored_event(
        hass,
        mock_config_entry,
        {"error_code": "F.380", "error_description": "Flame loss"},
    )
    await async_poll(hass, freezer, mock_config_entry)
    assert fired == []
