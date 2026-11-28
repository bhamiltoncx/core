"""Test the ViCare API session."""

from unittest.mock import patch

from freezegun.api import FrozenDateTimeFactory
from pytest_homeassistant.common import (
    MockConfigEntry,
    async_fire_time_changed,
    async_load_json_object_fixture,
)
from PyViCare.PyViCareAbstractOAuthManager import API_BASE_URL
import requests_mock

from homeassistant.components.vicare.const import DOMAIN
from homeassistant.core import HomeAssistant

from . import setup_integration

INSTALLATIONS_URL = f"{API_BASE_URL}/equipment/installations?includeGateways=true"
FEATURES_URL = (
    f"{API_BASE_URL}/features/installations/1234/gateways/gateway0"
    "/features?includeDevicesFeatures=true"
)
INSTALLATIONS = {
    "data": [
        {
            "id": 1234,
            "gateways": [
                {
                    "serial": "gateway0",
                    "devices": [
                        {
                            "id": "0",
                            "modelId": "E3_Vitodens_300W",
                            "status": "Online",
                            "deviceType": "heating",
                            "roles": ["type:boiler"],
                        }
                    ],
                }
            ],
        }
    ]
}


async def test_requests_ask_for_the_configured_language(
    hass: HomeAssistant,
    mock_config_entry: MockConfigEntry,
    requests_mock: requests_mock.Mocker,
    freezer: FrozenDateTimeFactory,
) -> None:
    """Test the API is asked for texts in the language of Home Assistant.

    The API localizes texts such as error descriptions by Accept-Language. A
    change of the language applies to the next refresh, without a reload.
    """
    requests_mock.get(INSTALLATIONS_URL, json=INSTALLATIONS)
    features = requests_mock.get(
        FEATURES_URL,
        json=await async_load_json_object_fixture(hass, "Vitodens300W.json", DOMAIN),
    )
    hass.config.language = "de"
    with patch(
        "homeassistant.helpers.config_entry_oauth2_flow.OAuth2Session.async_ensure_token_valid",
    ):
        await setup_integration(hass, mock_config_entry)

    assert features.called
    assert {
        request.headers["Accept-Language"] for request in requests_mock.request_history
    } == {"de"}
    assert features.last_request.headers["Authorization"] == "Bearer mock-access-token"

    hass.config.language = "pt-BR"
    coordinator = mock_config_entry.runtime_data.devices[0].coordinator
    calls = features.call_count
    freezer.tick(coordinator.update_interval)
    async_fire_time_changed(hass)
    await hass.async_block_till_done(wait_background_tasks=True)

    assert features.call_count == calls + 1
    assert features.last_request.headers["Accept-Language"] == "pt-BR"
