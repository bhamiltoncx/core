"""DataUpdateCoordinator for the ViCare integration."""

from collections import defaultdict
from collections.abc import Callable
from datetime import timedelta
import logging
from typing import Any, override

from PyViCare.PyViCareDevice import Device as PyViCareDevice
from PyViCare.PyViCareErrorHistory import ViCareErrorHistory
from PyViCare.PyViCareEvent import EVENT_TYPE_DEVICE_ERROR, ViCareEvent
from PyViCare.PyViCareService import ViCareDeviceAccessor
from PyViCare.PyViCareUtils import (
    PyViCareDeviceCommunicationError,
    PyViCareInternalServerError,
    PyViCareInvalidCredentialsError,
    PyViCareInvalidDataError,
    PyViCareNotPaidForError,
    PyViCareNotSupportedFeatureError,
    PyViCareRateLimitError,
)
import requests

from homeassistant.core import HomeAssistant, callback
from homeassistant.exceptions import ConfigEntryAuthFailed
from homeassistant.helpers.storage import Store
from homeassistant.helpers.update_coordinator import DataUpdateCoordinator, UpdateFailed

from .const import DEFAULT_CACHE_DURATION, DOMAIN
from .types import ViCareConfigEntry
from .utils import retry_after_from

_LOGGER = logging.getLogger(__name__)


BACKOFF_STEPS = (300, 600, 900)  # 5min, 10min, 15min

# Fetch the error event history with every Nth feature refresh. The feature
# interval stretches by 1/N so both together stay within the daily call budget.
ERROR_EVENTS_EVERY = 10
# Newest error events of the gateway per fetch.
ERROR_EVENTS_LIMIT = 100

ERROR_HISTORY_STORAGE_VERSION = 1
ERROR_HISTORY_SAVE_DELAY = 10


def feature_interval(gateway_count: int) -> int:
    """Return the seconds between feature fetches of one gateway.

    The PyViCare cache must last as long, or entities polling it in between
    would refill it with fetches of their own.
    """
    return (
        DEFAULT_CACHE_DURATION
        * gateway_count
        * (ERROR_EVENTS_EVERY + 1)
        // ERROR_EVENTS_EVERY
    )


def error_history_store_key(entry_id: str) -> str:
    """Return the storage key of the error history of a config entry."""
    return f"{DOMAIN}.{entry_id}.error_history"


class ViCareErrorHistoryStore:
    """The saved error history of each gateway of a config entry.

    Lets a restart fire the errors raised or cleared while Home Assistant was
    stopped, and no error twice.
    """

    def __init__(self, hass: HomeAssistant, entry_id: str) -> None:
        """Initialize the store."""
        self._store: Store[dict[str, dict[str, Any]]] = Store(
            hass, ERROR_HISTORY_STORAGE_VERSION, error_history_store_key(entry_id)
        )
        self._histories: dict[str, dict[str, Any]] = {}

    async def async_load(self) -> None:
        """Load the saved histories."""
        if isinstance(data := await self._store.async_load(), dict):
            self._histories = data

    def history(self, gateway_serial: str) -> ViCareErrorHistory:
        """Return the saved history of a gateway, or a new one."""
        if isinstance(saved := self._histories.get(gateway_serial), dict):
            return ViCareErrorHistory.from_dict(saved)
        return ViCareErrorHistory()

    @callback
    def async_save(self, gateway_serial: str, history: dict[str, Any]) -> None:
        """Save the history of a gateway, as returned by its as_dict()."""
        self._histories[gateway_serial] = history
        self._store.async_delay_save(
            lambda: dict(self._histories), ERROR_HISTORY_SAVE_DELAY
        )


class ViCareCoordinator(DataUpdateCoordinator[None]):
    """Coordinator for a single ViCare gateway.

    In viaGateway mode all devices behind a gateway share one service, so a
    single feature fetch refreshes every device on that gateway. The fetch takes
    the accessor of a representative device. Carries no payload of its own;
    freshness is signalled via ``last_update_success``.

    It also reads the device errors of the gateway from the event history,
    which keeps them for devices without ``device.messages.errors.raw``.
    """

    config_entry: ViCareConfigEntry

    def __init__(
        self,
        hass: HomeAssistant,
        config_entry: ViCareConfigEntry,
        device: PyViCareDevice,
        accessor: ViCareDeviceAccessor,
        gateway_count: int,
        error_history_store: ViCareErrorHistoryStore,
    ) -> None:
        """Initialise the coordinator for one gateway."""
        super().__init__(
            hass,
            _LOGGER,
            config_entry=config_entry,
            name=f"{DOMAIN}_{accessor.serial}",
            update_interval=timedelta(seconds=feature_interval(gateway_count)),
        )
        self._device = device
        self._accessor = accessor
        self._value_readers: list[Callable[[], None]] = []
        self._consecutive_offline_failures: int = 0
        # False once the API refused the event history (not in the plan).
        self.error_events_supported = True
        # True once the history was fetched since setup.
        self.error_history_fetched = False
        self._error_history_store = error_history_store
        self._error_history = error_history_store.history(accessor.serial)
        self._error_history_to_save: dict[str, Any] | None = None
        # Error events not yet handed to the device's event entity.
        self._new_error_events: dict[str, list[ViCareEvent]] = {}
        self._refreshes_until_error_events = 0

    @callback
    def async_add_value_reader(self, reader: Callable[[], None]) -> Callable[[], None]:
        """Register a reader to run in the executor after each fetch."""
        self._value_readers.append(reader)

        def remove_value_reader() -> None:
            self._value_readers.remove(reader)

        return remove_value_reader

    def active_errors(self, device_id: str) -> list[ViCareEvent]:
        """Return the raises of the errors of the device that stand."""
        return self._error_history.active_errors(device_id)

    def take_new_error_events(self, device_id: str) -> list[ViCareEvent]:
        """Return the device's new error events, oldest first, only once."""
        return self._new_error_events.pop(device_id, [])

    @override
    async def _async_update_data(self) -> None:
        """Refresh the gateway's feature payload."""
        try:
            await self.hass.async_add_executor_job(self._refresh)
        finally:
            if (history := self._error_history_to_save) is not None:
                self._error_history_to_save = None
                self._error_history_store.async_save(self._accessor.serial, history)

    def _refresh(self) -> None:
        """Force a fresh fetch from the Viessmann API."""
        try:
            self._device.service.clear_cache()
            self._device.service.fetch_all_features(self._accessor)
        except PyViCareNotSupportedFeatureError:
            # PACKAGE_NOT_PAID_FOR: load with no features instead of retrying setup.
            _LOGGER.debug(
                "No accessible features for gateway %s", self._accessor.serial
            )
            self._consecutive_offline_failures = 0
        except PyViCareInvalidCredentialsError as err:
            raise ConfigEntryAuthFailed from err
        except PyViCareRateLimitError as err:
            raise UpdateFailed(
                str(err),
                retry_after=retry_after_from(
                    err,
                    self.update_interval or timedelta(seconds=DEFAULT_CACHE_DURATION),
                ),
            ) from err
        except PyViCareDeviceCommunicationError as err:
            error_str = str(err)
            if getattr(err, "reason", None) == "GATEWAY_OFFLINE":
                self._consecutive_offline_failures += 1
                idx = min(
                    self._consecutive_offline_failures - 1,
                    len(BACKOFF_STEPS) - 1,
                )
                normal_interval = int(
                    (
                        self.update_interval
                        or timedelta(seconds=DEFAULT_CACHE_DURATION)
                    ).total_seconds()
                )
                backoff = max(BACKOFF_STEPS[idx], normal_interval)
                _LOGGER.debug(
                    "ViCare gateway %s is offline (consecutive failures: %d). "
                    "Backing off next refresh for %d seconds",
                    self._accessor.serial,
                    self._consecutive_offline_failures,
                    backoff,
                )
                raise UpdateFailed(
                    error_str,
                    retry_after=backoff,
                ) from err
            raise UpdateFailed(error_str) from err
        except (
            PyViCareInternalServerError,
            PyViCareInvalidDataError,
            requests.RequestException,
        ) as err:
            raise UpdateFailed(str(err)) from err
        else:
            self._consecutive_offline_failures = 0
            self._refresh_error_events()
            # Only after a successful fetch: the cache was emptied above, and a
            # reader must not be the one to refill it from the event loop.
            # Iterate a copy, entities deregister from the event loop thread.
            for reader in list(self._value_readers):
                try:
                    reader()
                except Exception:
                    # One unreadable value must not take the whole device down.
                    _LOGGER.exception("Error reading a ViCare entity value")

    def _refresh_error_events(self) -> None:
        """Fetch the error history when due. Runs in the executor."""
        # Events the entity did not take, such as of a disabled one, lapse.
        self._new_error_events = {}
        if not self.error_events_supported:
            return
        if self._refreshes_until_error_events > 0:
            self._refreshes_until_error_events -= 1
            return
        # A failed fetch waits its turn too, or it would spend a call each refresh.
        self._refreshes_until_error_events = ERROR_EVENTS_EVERY - 1
        try:
            page = self._device.service.fetch_events(
                self._accessor,
                EVENT_TYPE_DEVICE_ERROR,
                gateway_serials=self._accessor.serial,
                limit=ERROR_EVENTS_LIMIT,
            )
        except PyViCareNotPaidForError:
            _LOGGER.debug(
                "The event history is not available for gateway %s",
                self._accessor.serial,
            )
            self.error_events_supported = False
            return
        except (
            PyViCareDeviceCommunicationError,
            PyViCareInternalServerError,
            PyViCareInvalidDataError,
            PyViCareRateLimitError,
            requests.RequestException,
        ) as err:
            # Keep the last known errors until the next fetch.
            _LOGGER.debug(
                "Unable to fetch the error events of gateway %s: %s",
                self._accessor.serial,
                err,
            )
            return
        new: defaultdict[str, list[ViCareEvent]] = defaultdict(list)
        for event in self._error_history.update(page.events):
            if event.device_id:
                new[event.device_id].append(event)
        self._new_error_events = dict(new)
        self.error_history_fetched = True
        self._error_history_to_save = self._error_history.as_dict()
