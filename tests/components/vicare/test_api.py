"""Test the ViCare API session."""

from pytest_homeassistant.common import MockConfigEntry
from PyViCare.PyViCareAbstractOAuthManager import API_BASE_URL
import requests_mock

from homeassistant.components.vicare.api import ConfigEntryAuth
from homeassistant.core import HomeAssistant
from homeassistant.helpers import config_entry_oauth2_flow

URL = "/equipment/installations"


async def test_requests_ask_for_the_configured_language(
    hass: HomeAssistant,
    mock_config_entry: MockConfigEntry,
    requests_mock: requests_mock.Mocker,
) -> None:
    """Test the API is asked for texts in the language of Home Assistant.

    The API localizes texts such as error descriptions by Accept-Language. A
    change of the language applies to the next request, without a reload.
    """
    mock_config_entry.add_to_hass(hass)
    implementation = (
        await config_entry_oauth2_flow.async_get_config_entry_implementation(
            hass, mock_config_entry
        )
    )
    auth = ConfigEntryAuth(
        hass,
        config_entry_oauth2_flow.OAuth2Session(hass, mock_config_entry, implementation),
    )
    requests_mock.get(f"{API_BASE_URL}{URL}", json={"data": []})

    hass.config.language = "de"
    await hass.async_add_executor_job(auth.get, URL)
    assert requests_mock.last_request.headers["Accept-Language"] == "de"

    hass.config.language = "pt-BR"
    await hass.async_add_executor_job(auth.get, URL)
    assert requests_mock.last_request.headers["Accept-Language"] == "pt-BR"
    assert (
        requests_mock.last_request.headers["Authorization"]
        == "Bearer mock-access-token"
    )
