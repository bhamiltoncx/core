"""Fixtures for ViCare integration tests."""

from collections.abc import AsyncGenerator, Generator
from dataclasses import dataclass
import re
import time
from typing import Any
from unittest.mock import AsyncMock, Mock, patch

import pytest
from pytest_homeassistant.common import MockConfigEntry, load_json_object_fixture
from PyViCare.PyViCareDeviceConfig import PyViCareDeviceConfig
from PyViCare.PyViCareEvent import ViCareEventPage
from PyViCare.PyViCareService import ViCareDeviceAccessor, readFeature

from homeassistant.components.application_credentials import (
    ClientCredential,
    async_import_client_credential,
)
from homeassistant.components.vicare.const import DOMAIN
from homeassistant.core import HomeAssistant
from homeassistant.setup import async_setup_component

from . import MODULE, setup_integration


@dataclass
class Fixture:
    """Fixture representation with the assigned roles and dummy data location."""

    roles: set[str]
    data_file: str
    # Opt-in shared gateway serial; defaults to a per-fixture gateway when unset.
    gateway_id: str | None = None
    online: bool = True
    # Event history of the device, a saved events-history response.
    error_events: str | None = None


class MockPyViCare:
    """Mocked PyVicare class based on a json dump."""

    def __init__(self, fixtures: list[Fixture]) -> None:
        """Init devices from json dumps, sharing one service per gateway."""
        self.loadViaGateway = Mock()
        self.setCacheDuration = Mock()
        self.initWithExternalOAuth = Mock()
        self.devices = []
        self.services: dict[str, MockViCareService] = {}
        for idx, fixture in enumerate(fixtures):
            gateway_id = fixture.gateway_id or f"gateway{idx}"
            device_id = f"deviceId{idx}"
            service = self.services.setdefault(
                gateway_id, MockViCareService(fixture.roles)
            )
            service.add_device(device_id, fixture)
            self.devices.append(
                PyViCareDeviceConfig(
                    ViCareDeviceAccessor(f"installation{idx}", gateway_id, device_id),
                    service,
                    "Vitovalor"
                    if fixture.data_file.endswith("VitoValor.json")
                    else "Vitoligno300C"
                    if fixture.data_file.endswith("Vitoligno300C.json")
                    else f"model{idx}",
                    "Online" if fixture.online else "Offline",
                    roles=list(fixture.roles),
                )
            )
        # Simulate a device with an unsupported deviceType that PyViCare's
        # `devices` filter would drop but should still appear in `all_devices`
        # (used by diagnostics).
        unsupported_fixture = Fixture(set(), "vicare/dummy-device-no-serial.json")
        unsupported_service = MockViCareService(set())
        unsupported_service.add_device("deviceId_unsupported", unsupported_fixture)
        self.all_devices = [
            *self.devices,
            PyViCareDeviceConfig(
                ViCareDeviceAccessor(
                    "installation_unsupported",
                    "gateway_unsupported",
                    "deviceId_unsupported",
                ),
                unsupported_service,
                "unsupported_model",
                "Online",
                roles=[],
            ),
        ]


class MockViCareService:
    """Mock of the gateway-wide service PyViCare shares in viaGateway mode.

    One instance serves every device on the gateway: `fetch_all_features`
    returns the bulk payload for all of them, and `getProperty` filters by
    `accessor.device_id`, like `ViCareCachedServiceViaGateway` does.
    """

    def __init__(self, roles: set[str]) -> None:
        """Initialize an empty gateway service."""
        self._features: dict[str, list] = {}
        # Newest first, like the events-history API returns them.
        self.events: list[dict[str, Any]] = []
        self.fetch_all_features = Mock(side_effect=self._fetch_all_features)
        self.fetch_events = Mock(side_effect=self._fetch_events)
        self.setProperty = Mock()
        self.clear_cache = Mock()
        self.roles = roles

    def add_device(self, device_id: str, fixture: Fixture) -> None:
        """Add a device's features to the gateway payload."""
        features = load_json_object_fixture(fixture.data_file)["data"]
        # In the real bulk payload every feature carries its own device in the
        # uri, which is what consumers filter on. The fixtures all say device 0.
        for feature in features:
            if "uri" in feature:
                feature["uri"] = re.sub(
                    r"/devices/[^/]+/", f"/devices/{device_id}/", feature["uri"]
                )
        self._features[device_id] = features
        if fixture.error_events:
            for event in load_json_object_fixture(fixture.error_events)["data"]:
                event["body"]["deviceId"] = device_id
                self.events.append(event)

    def add_error_event(
        self, device_id: str, error_code: str, active: bool, timestamp: str
    ) -> None:
        """Record a device error event, as the API would on a raise or clear."""
        self.events.insert(
            0,
            {
                "eventType": "device-error",
                "body": {
                    "errorCode": error_code,
                    "deviceId": device_id,
                    "active": active,
                    "errorEventType": "Error",
                    "errorDescription": f"Description of {error_code}",
                },
                "createdAt": timestamp,
                "eventTimestamp": timestamp,
            },
        )

    def _fetch_events(
        self,
        accessor: ViCareDeviceAccessor,
        event_type: str | None = None,
        *,
        limit: int | None = None,
        **kwargs: Any,
    ) -> ViCareEventPage:
        """Return the gateway's events, filtered by type, newest first."""
        events = [
            event
            for event in self.events
            if event_type is None or event["eventType"] == event_type
        ]
        return ViCareEventPage.from_payload(
            {"cursor": {"next": ""}, "data": events[:limit]}
        )

    def _fetch_all_features(self, accessor: ViCareDeviceAccessor):
        """Return the features of every device on the gateway."""
        return {"data": [f for features in self._features.values() for f in features]}

    def hasRoles(self, requested_roles: list[str]) -> bool:
        """Return true if requested roles are assigned."""
        return requested_roles and set(requested_roles).issubset(self.roles)

    def getProperty(self, accessor: ViCareDeviceAccessor, property_name: str):
        """Read a property of one device from the gateway payload."""
        return readFeature(self._features[accessor.device_id], property_name)


@pytest.fixture(autouse=True)
async def setup_credentials(hass: HomeAssistant) -> None:
    """Fixture to setup credentials."""
    assert await async_setup_component(hass, "application_credentials", {})
    await async_import_client_credential(
        hass,
        DOMAIN,
        ClientCredential("mock-client-id", ""),
        DOMAIN,
    )


@pytest.fixture
def mock_config_entry() -> MockConfigEntry:
    """Return the default mocked config entry."""
    return MockConfigEntry(
        domain=DOMAIN,
        unique_id="ViCare",
        entry_id="1234",
        version=2,
        minor_version=1,
        data={
            "auth_implementation": DOMAIN,
            "token": {
                "access_token": "mock-access-token",
                "refresh_token": "mock-refresh-token",
                "expires_at": time.time() + 3600,
                "scope": "IoT User offline_access",
                "token_type": "Bearer",
            },
        },
    )


@pytest.fixture
async def mock_vicare_gas_boiler(
    hass: HomeAssistant, mock_config_entry: MockConfigEntry
) -> AsyncGenerator[MockConfigEntry]:
    """Return a mocked ViCare API representing a single gas boiler device."""
    fixtures: list[Fixture] = [Fixture({"type:boiler"}, "vicare/Vitodens300W.json")]
    with (
        patch(
            "homeassistant.helpers.config_entry_oauth2_flow.OAuth2Session.async_ensure_token_valid",
        ),
        patch(f"{MODULE}.PyViCare", return_value=MockPyViCare(fixtures)),
    ):
        await setup_integration(hass, mock_config_entry)

        yield mock_config_entry


@pytest.fixture
async def mock_vicare_error_events(
    hass: HomeAssistant, mock_config_entry: MockConfigEntry
) -> AsyncGenerator[MockViCareService]:
    """Set up a heat pump without errors.raw, whose errors come from the history.

    Yields the gateway service, whose event history the test can add to. The
    history holds three F.380 faults, the last one cleared.
    """
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
        await setup_integration(hass, mock_config_entry)

        yield vicare.services["gateway0"]


@pytest.fixture
async def mock_vicare_room_sensors(
    hass: HomeAssistant, mock_config_entry: MockConfigEntry
) -> AsyncGenerator[MockConfigEntry]:
    """Return a mocked ViCare API representing multiple room sensor devices."""
    fixtures: list[Fixture] = [
        Fixture({"type:climateSensor"}, "vicare/RoomSensor1.json"),
        Fixture({"type:climateSensor"}, "vicare/RoomSensor2.json"),
    ]
    with (
        patch(
            "homeassistant.helpers.config_entry_oauth2_flow.OAuth2Session.async_ensure_token_valid",
        ),
        patch(f"{MODULE}.PyViCare", return_value=MockPyViCare(fixtures)),
    ):
        await setup_integration(hass, mock_config_entry)

        yield mock_config_entry


@pytest.fixture
def mock_setup_entry() -> Generator[AsyncMock]:
    """Mock setting up a config entry."""
    with patch(f"{MODULE}.async_setup_entry", return_value=None) as mock_setup_entry:
        yield mock_setup_entry
