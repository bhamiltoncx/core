"""Test the ViCare error event entity."""

from datetime import timedelta
from typing import Any

from freezegun.api import FrozenDateTimeFactory
import pytest
from pytest_homeassistant.common import (
    MockConfigEntry,
    async_fire_time_changed,
    load_json_object_fixture,
)
from PyViCare.PyViCareErrorHistory import ViCareErrorHistory
from PyViCare.PyViCareEvent import ViCareEvent
from PyViCare.PyViCareUtils import PyViCareInternalServerError, PyViCareNotPaidForError
from syrupy.assertion import SnapshotAssertion

from homeassistant.components.event import ATTR_EVENT_TYPE
from homeassistant.components.vicare.coordinator import (
    ERROR_EVENTS_EVERY,
    ERROR_HISTORY_SAVE_DELAY,
    ERROR_HISTORY_STORAGE_VERSION,
    error_history_store_key,
)
from homeassistant.const import STATE_UNAVAILABLE, STATE_UNKNOWN
from homeassistant.core import Event, EventStateChangedData, HomeAssistant, callback
from homeassistant.helpers import entity_registry as er
from homeassistant.helpers.event import async_track_state_change_event

from . import async_poll
from .conftest import MockViCareService, async_setup_error_events, error_events_pyvicare

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


async def test_entity(
    hass: HomeAssistant,
    mock_vicare_error_events: MockViCareService,
    snapshot: SnapshotAssertion,
    entity_registry: er.EntityRegistry,
) -> None:
    """Test the event entity of a device that reports messages."""
    assert entity_registry.async_get(ENTITY_ID) == snapshot(name="entry")
    assert hass.states.get(ENTITY_ID) == snapshot(name="state")


def _track_fired(hass: HomeAssistant) -> list[tuple[str, str, str]]:
    """Record the event type, code and time of each event a trigger sees.

    Like the event received trigger, skip a state that comes straight from
    unavailable, and the first state.
    """
    fired: list[tuple[str, str, str]] = []

    @callback
    def record(event: Event[EventStateChangedData]) -> None:
        old_state, new_state = event.data["old_state"], event.data["new_state"]
        if old_state is None or old_state.state == STATE_UNAVAILABLE:
            return
        assert new_state is not None
        fired.append(
            (
                new_state.attributes[ATTR_EVENT_TYPE],
                new_state.attributes["error_code"],
                new_state.attributes["error_time"],
            )
        )

    async_track_state_change_event(hass, ENTITY_ID, record)
    return fired


def _save_history_of_previous_run(hass_storage: dict[str, Any]) -> None:
    """Save the history as a run that stopped before the November 26 fault."""
    page = load_json_object_fixture("vicare/Vitodens100_error_events.json")["data"]
    history = ViCareErrorHistory()
    # Newest first: the raise and clear of November 26 lead the page.
    history.update(
        ViCareEvent({**raw, "body": {**raw["body"], "deviceId": DEVICE_ID}})
        for raw in page[2:]
    )
    hass_storage[error_history_store_key("1234")] = {
        "version": ERROR_HISTORY_STORAGE_VERSION,
        "key": error_history_store_key("1234"),
        "data": {"gateway0": history.as_dict()},
    }


NOVEMBER_26 = [
    ("raised", "F.380", "2026-11-26T01:17:32+00:00"),
    ("cleared", "F.380", "2026-11-26T15:29:59+00:00"),
]


async def test_fires_events_missed_while_stopped(
    hass: HomeAssistant,
    hass_storage: dict[str, Any],
    mock_config_entry: MockConfigEntry,
    freezer: FrozenDateTimeFactory,
) -> None:
    """Test the errors of the history that the saved one lacks fire after startup.

    The November 26 fault came and went while Home Assistant was stopped. The
    entity never fired before, the saved history is what tells them apart.
    """
    _save_history_of_previous_run(hass_storage)
    fired = _track_fired(hass)
    await async_setup_error_events(hass, mock_config_entry, error_events_pyvicare())

    # They fire with the first refresh after the entity was added.
    await async_poll(hass, freezer, mock_config_entry)
    assert fired == NOVEMBER_26

    await async_poll(hass, freezer, mock_config_entry, ERROR_EVENTS_EVERY)
    assert fired == NOVEMBER_26


async def test_holds_events_while_unavailable(
    hass: HomeAssistant,
    hass_storage: dict[str, Any],
    mock_config_entry: MockConfigEntry,
    freezer: FrozenDateTimeFactory,
) -> None:
    """Test events wait for the entity to be available, so triggers see them."""
    _save_history_of_previous_run(hass_storage)
    fired = _track_fired(hass)
    service = await async_setup_error_events(
        hass, mock_config_entry, error_events_pyvicare()
    )

    service.fetch_all_features.side_effect = PyViCareInternalServerError(
        {"statusCode": 500, "message": "boom", "viErrorId": "1"}
    )
    await async_poll(hass, freezer, mock_config_entry)
    assert hass.states.get(ENTITY_ID).state == STATE_UNAVAILABLE
    assert fired == []

    service.fetch_all_features.side_effect = service._fetch_all_features
    await async_poll(hass, freezer, mock_config_entry)
    assert fired == NOVEMBER_26


async def test_saves_the_history(
    hass: HomeAssistant,
    hass_storage: dict[str, Any],
    mock_vicare_error_events: MockViCareService,
    mock_config_entry: MockConfigEntry,
    freezer: FrozenDateTimeFactory,
) -> None:
    """Test the history is saved after a fetch, for the next start."""
    mock_vicare_error_events.add_error_event(
        DEVICE_ID, "F.380", True, "2026-11-28T10:00:00.000Z"
    )
    await async_poll(hass, freezer, mock_config_entry, ERROR_EVENTS_EVERY)
    freezer.tick(timedelta(seconds=ERROR_HISTORY_SAVE_DELAY))
    async_fire_time_changed(hass)
    await hass.async_block_till_done()

    saved = hass_storage[error_history_store_key("1234")]["data"]["gateway0"]
    history = ViCareErrorHistory.from_dict(saved)
    assert [event.error_code for event in history.active_errors(DEVICE_ID)] == ["F.380"]


async def test_unavailable_when_the_history_is_refused(
    hass: HomeAssistant,
    mock_vicare_error_events: MockViCareService,
    mock_config_entry: MockConfigEntry,
    freezer: FrozenDateTimeFactory,
) -> None:
    """Test the error entities go unavailable when the history is refused later."""
    mock_vicare_error_events.fetch_events.side_effect = PyViCareNotPaidForError(
        {"extendedPayload": {"monetization": "basic"}}
    )
    await async_poll(hass, freezer, mock_config_entry, ERROR_EVENTS_EVERY)

    assert hass.states.get(ENTITY_ID).state == STATE_UNAVAILABLE
    assert hass.states.get("binary_sensor.model0_problem").state == STATE_UNAVAILABLE
