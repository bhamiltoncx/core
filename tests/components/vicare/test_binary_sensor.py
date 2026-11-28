"""Test ViCare binary sensors."""

from unittest.mock import MagicMock, patch

from freezegun.api import FrozenDateTimeFactory
import pytest
from pytest_homeassistant.common import MockConfigEntry, snapshot_platform
from PyViCare.PyViCareUtils import PyViCareInternalServerError
from syrupy.assertion import SnapshotAssertion

from homeassistant.components.vicare.coordinator import ERROR_EVENTS_EVERY
from homeassistant.const import STATE_OFF, STATE_ON, STATE_UNKNOWN, Platform
from homeassistant.core import HomeAssistant
from homeassistant.helpers import entity_registry as er
from homeassistant.helpers.entity_component import async_update_entity

from . import MODULE, async_poll, setup_integration
from .conftest import (
    Fixture,
    MockPyViCare,
    MockViCareService,
    async_setup_error_events,
    error_events_pyvicare,
)


@pytest.mark.parametrize(
    "entity_id",
    [
        "burner",
        "circulation_pump",
        "frost_protection",
    ],
)
async def test_binary_sensors(
    hass: HomeAssistant,
    mock_vicare_gas_boiler: MagicMock,
    snapshot: SnapshotAssertion,
    entity_id: str,
) -> None:
    """Test the ViCare binary sensor."""
    assert hass.states.get(f"binary_sensor.model0_{entity_id}") == snapshot


@pytest.mark.usefixtures("entity_registry_enabled_by_default")
async def test_all_entities(
    hass: HomeAssistant,
    snapshot: SnapshotAssertion,
    mock_config_entry: MockConfigEntry,
    entity_registry: er.EntityRegistry,
) -> None:
    """Test all entities."""
    fixtures: list[Fixture] = [
        Fixture({"type:boiler"}, "vicare/Vitodens300W.json"),
        Fixture({"type:radiator"}, "vicare/ZigbeeTRV.json"),
        Fixture({"type:repeater"}, "vicare/ZigbeeRepeater.json"),
        # FHT main and channel are the same physical zigbee node, so they share
        # a gateway; this lets the channel link to the main via via_device.
        Fixture({"type:fhtMain"}, "vicare/FHTMain.json", gateway_id="fht_gateway"),
        Fixture(
            {"type:fhtChannel"}, "vicare/FHTChannel.json", gateway_id="fht_gateway"
        ),
        Fixture({"type:virtual;smartRoomControl"}, "vicare/RoomControl.json"),
    ]
    with (
        patch(
            "homeassistant.helpers.config_entry_oauth2_flow.OAuth2Session.async_ensure_token_valid",
        ),
        patch(f"{MODULE}.PyViCare", return_value=MockPyViCare(fixtures)),
        patch(f"{MODULE}.PLATFORMS", [Platform.BINARY_SENSOR]),
    ):
        await setup_integration(hass, mock_config_entry)

    await snapshot_platform(hass, entity_registry, snapshot, mock_config_entry.entry_id)


async def test_holiday(
    hass: HomeAssistant,
    mock_config_entry: MockConfigEntry,
) -> None:
    """Test the holiday program while it is scheduled."""
    vicare = MockPyViCare([Fixture({"type:heatpump"}, "vicare/Vitocal250A.json")])
    for feature in vicare.services["gateway0"]._features["deviceId0"]:
        if feature["feature"] == "heating.operating.programs.holiday":
            feature["properties"]["active"]["value"] = True
            feature["properties"]["start"]["value"] = "2026-11-02"
            feature["properties"]["end"]["value"] = "2026-11-15"
    with (
        patch(
            "homeassistant.helpers.config_entry_oauth2_flow.OAuth2Session.async_ensure_token_valid",
        ),
        patch(f"{MODULE}.PyViCare", return_value=vicare),
        patch(f"{MODULE}.PLATFORMS", [Platform.BINARY_SENSOR, Platform.SENSOR]),
    ):
        await setup_integration(hass, mock_config_entry)

    for entity_id in (
        "binary_sensor.model0_holiday",
        "binary_sensor.model0_holiday_at_home",
    ):
        await async_update_entity(hass, entity_id)

    assert hass.states.get("binary_sensor.model0_holiday").state == STATE_ON
    assert hass.states.get("sensor.model0_holiday_start").state == "2026-11-02"
    assert hass.states.get("sensor.model0_holiday_end").state == "2026-11-15"
    # the device reports 2000-01-01 for a holiday at home that is not scheduled
    assert hass.states.get("binary_sensor.model0_holiday_at_home").state == STATE_OFF
    assert hass.states.get("sensor.model0_holiday_at_home_start").state == STATE_UNKNOWN


async def test_boiler_internal_pump(
    hass: HomeAssistant,
    mock_config_entry: MockConfigEntry,
) -> None:
    """Test the internal pump of the boiler while it runs."""
    vicare = MockPyViCare([Fixture({"type:heatpump"}, "vicare/Vitocal250A.json")])
    pump = next(
        feature
        for feature in vicare.services["gateway0"]._features["deviceId0"]
        if feature["feature"] == "heating.boiler.pumps.internal"
    )
    pump["properties"]["status"]["value"] = "on"
    with (
        patch(
            "homeassistant.helpers.config_entry_oauth2_flow.OAuth2Session.async_ensure_token_valid",
        ),
        patch(f"{MODULE}.PyViCare", return_value=vicare),
        patch(f"{MODULE}.PLATFORMS", [Platform.BINARY_SENSOR]),
    ):
        await setup_integration(hass, mock_config_entry)

    entity_id = "binary_sensor.model0_internal_pump"
    await async_update_entity(hass, entity_id)

    assert hass.states.get(entity_id).state == STATE_ON


@pytest.mark.parametrize(
    ("properties", "expected_state"),
    [
        pytest.param({"enabled": False}, STATE_OFF, id="disabled"),
        pytest.param({"enabled": True, "active": False}, STATE_OFF, id="idle"),
        pytest.param({"enabled": True, "active": True}, STATE_ON, id="running"),
    ],
)
async def test_dhw_hygiene(
    hass: HomeAssistant,
    mock_config_entry: MockConfigEntry,
    properties: dict[str, bool],
    expected_state: str,
) -> None:
    """Test the legionella protection of the domestic hot water."""
    vicare = MockPyViCare([Fixture({"type:heatpump"}, "vicare/Vitocal250A.json")])
    hygiene = next(
        feature
        for feature in vicare.services["gateway0"]._features["deviceId0"]
        if feature["feature"] == "heating.dhw.hygiene"
    )
    hygiene["properties"] = {
        name: {"type": "boolean", "value": value} for name, value in properties.items()
    }
    with (
        patch(
            "homeassistant.helpers.config_entry_oauth2_flow.OAuth2Session.async_ensure_token_valid",
        ),
        patch(f"{MODULE}.PyViCare", return_value=vicare),
        patch(f"{MODULE}.PLATFORMS", [Platform.BINARY_SENSOR]),
    ):
        await setup_integration(hass, mock_config_entry)

    entity_id = "binary_sensor.model0_dhw_legionella_protection"
    await async_update_entity(hass, entity_id)

    assert hass.states.get(entity_id).state == expected_state


async def test_room_window_and_condensation_risk(
    hass: HomeAssistant,
    mock_config_entry: MockConfigEntry,
) -> None:
    """Test the open window and condensation risk of each room."""
    vicare = MockPyViCare(
        [Fixture({"type:virtual;smartRoomControl"}, "vicare/RoomControl.json")]
    )
    for feature in vicare.services["gateway0"]._features["deviceId0"]:
        if feature["feature"] in (
            "rooms.1.sensors.openWindow",
            "rooms.0.condensationRisk",
        ):
            feature["properties"]["value"]["value"] = True
    with (
        patch(
            "homeassistant.helpers.config_entry_oauth2_flow.OAuth2Session.async_ensure_token_valid",
        ),
        patch(f"{MODULE}.PyViCare", return_value=vicare),
        patch(f"{MODULE}.PLATFORMS", [Platform.BINARY_SENSOR]),
    ):
        await setup_integration(hass, mock_config_entry)

    for entity_id, state in (
        ("binary_sensor.model0_room_0_window", STATE_OFF),
        ("binary_sensor.model0_room_0_condensation_risk", STATE_ON),
        ("binary_sensor.model0_room_1_window", STATE_ON),
        ("binary_sensor.model0_room_1_condensation_risk", STATE_OFF),
    ):
        await async_update_entity(hass, entity_id)
        assert hass.states.get(entity_id).state == state


async def test_problem_from_error_events(
    hass: HomeAssistant,
    mock_vicare_error_events: MockViCareService,
    mock_config_entry: MockConfigEntry,
    freezer: FrozenDateTimeFactory,
) -> None:
    """Test a device without errors.raw reports errors from the event history."""
    # The newest F.380 of the history is cleared.
    assert hass.states.get("binary_sensor.model0_problem").state == STATE_OFF

    mock_vicare_error_events.add_error_event(
        "deviceId0", "F.380", True, "2026-11-28T10:00:00.000Z"
    )
    await async_poll(hass, freezer, mock_config_entry, ERROR_EVENTS_EVERY)
    assert hass.states.get("binary_sensor.model0_problem").state == STATE_ON

    mock_vicare_error_events.add_error_event(
        "deviceId0", "F.380", False, "2026-11-28T10:05:00.000Z"
    )
    await async_poll(hass, freezer, mock_config_entry, ERROR_EVENTS_EVERY)
    assert hass.states.get("binary_sensor.model0_problem").state == STATE_OFF


async def test_problem_from_error_events_active_at_startup(
    hass: HomeAssistant,
    mock_config_entry: MockConfigEntry,
) -> None:
    """Test an error raised before startup and never cleared is reported."""
    vicare = MockPyViCare(
        [
            Fixture(
                {"type:heatpump"},
                "vicare/Vitocal250A.json",
                error_events="vicare/Vitodens100_error_events.json",
            )
        ]
    )
    service = vicare.services["gateway0"]
    service.add_error_event("deviceId0", "F.357", True, "2026-11-27T08:00:00.000Z")
    # A late arriving older clear of another code changes nothing.
    service.add_error_event("deviceId0", "F.380", False, "2026-11-26T15:29:59.000Z")
    with (
        patch(
            "homeassistant.helpers.config_entry_oauth2_flow.OAuth2Session.async_ensure_token_valid",
        ),
        patch(f"{MODULE}.PyViCare", return_value=vicare),
        patch(f"{MODULE}.PLATFORMS", [Platform.BINARY_SENSOR]),
    ):
        await setup_integration(hass, mock_config_entry)

    assert hass.states.get("binary_sensor.model0_problem").state == STATE_ON


async def test_problem_keeps_errors_raw(
    hass: HomeAssistant,
    mock_config_entry: MockConfigEntry,
    entity_registry: er.EntityRegistry,
) -> None:
    """Test a device with errors.raw keeps reading it, with one problem entity."""
    vicare = MockPyViCare(
        [
            Fixture(
                {"type:ventilation"},
                "vicare/ViAir300F.json",
                error_events="vicare/Vitodens100_error_events.json",
            )
        ]
    )
    service = vicare.services["gateway0"]
    service.add_error_event("deviceId0", "F.357", True, "2026-11-27T08:00:00.000Z")
    with (
        patch(
            "homeassistant.helpers.config_entry_oauth2_flow.OAuth2Session.async_ensure_token_valid",
        ),
        patch(f"{MODULE}.PyViCare", return_value=vicare),
        patch(f"{MODULE}.PLATFORMS", [Platform.BINARY_SENSOR]),
    ):
        await setup_integration(hass, mock_config_entry)
        await async_update_entity(hass, "binary_sensor.model0_problem")

    problems = [
        entry
        for entry in er.async_entries_for_config_entry(
            entity_registry, mock_config_entry.entry_id
        )
        if entry.unique_id.endswith("-device_error")
    ]
    assert len(problems) == 1
    # errors.raw of the fixture is empty, the history is not consulted.
    assert hass.states.get("binary_sensor.model0_problem").state == STATE_OFF


async def test_problem_unknown_until_the_history_is_read(
    hass: HomeAssistant,
    mock_config_entry: MockConfigEntry,
    freezer: FrozenDateTimeFactory,
) -> None:
    """Test the problem stays unknown while the history could not be read."""
    vicare = error_events_pyvicare()
    service = vicare.services["gateway0"]
    service.fetch_events.side_effect = PyViCareInternalServerError(
        {"statusCode": 500, "message": "boom", "viErrorId": "1"}
    )
    await async_setup_error_events(hass, mock_config_entry, vicare)
    assert hass.states.get("binary_sensor.model0_problem").state == STATE_UNKNOWN

    service.fetch_events.side_effect = service._fetch_events
    await async_poll(hass, freezer, mock_config_entry, ERROR_EVENTS_EVERY)
    assert hass.states.get("binary_sensor.model0_problem").state == STATE_OFF
