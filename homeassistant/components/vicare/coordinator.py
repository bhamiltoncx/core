"""DataUpdateCoordinator for the ViCare integration."""

from collections import defaultdict
from collections.abc import Callable
from datetime import UTC, datetime, timedelta
import logging
from typing import override

from PyViCare.PyViCareDevice import Device as PyViCareDevice
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
from homeassistant.helpers.update_coordinator import DataUpdateCoordinator, UpdateFailed

from .const import DEFAULT_CACHE_DURATION, DOMAIN
from .types import ViCareConfigEntry
from .utils import retry_after_from

_LOGGER = logging.getLogger(__name__)


BACKOFF_STEPS = (300, 600, 900)  # 5min, 10min, 15min

# Fetch the error event history with every Nth feature refresh. The feature
# interval stretches by 1/N so both together stay within the daily call budget.
ERROR_EVENTS_EVERY = 10
# Newest error events of the gateway per fetch. The newest event of an error
# code tells whether it stands, so this bounds how many raises and clears can
# follow an error that never cleared before it drops out of view.
ERROR_EVENTS_LIMIT = 100

type _ErrorEventKey = tuple[str | None, str | None, bool, str]


def _error_event_key(event: ViCareEvent) -> _ErrorEventKey:
    """Identify an error event across fetches."""
    return (
        event.device_id,
        event.error_code,
        event.active,
        event.raw.get("eventTimestamp") or event.raw.get("createdAt") or "",
    )


def event_time(event: ViCareEvent) -> datetime:
    """Return when the event happened, for ordering."""
    return event.event_timestamp or event.created_at or datetime.min.replace(tzinfo=UTC)


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
    ) -> None:
        """Initialise the coordinator for one gateway."""
        super().__init__(
            hass,
            _LOGGER,
            config_entry=config_entry,
            name=f"{DOMAIN}_{accessor.serial}",
            update_interval=timedelta(
                seconds=DEFAULT_CACHE_DURATION
                * gateway_count
                * (ERROR_EVENTS_EVERY + 1)
                / ERROR_EVENTS_EVERY
            ),
        )
        self._device = device
        self._accessor = accessor
        self._value_readers: list[Callable[[], None]] = []
        self._consecutive_offline_failures: int = 0
        # False once the API refused the event history (not in the plan).
        self.error_events_supported = True
        # Raising events of the errors that stand, by device id.
        self.active_errors: dict[str, list[ViCareEvent]] = {}
        # Error events that arrived with this refresh, oldest first, by device.
        self.new_error_events: dict[str, list[ViCareEvent]] = {}
        # Every error event of the last fetch, oldest first, by device.
        self.error_history: dict[str, list[ViCareEvent]] = {}
        self._seen_error_events: set[_ErrorEventKey] | None = None
        self._refreshes_until_error_events = 0

    @callback
    def async_add_value_reader(self, reader: Callable[[], None]) -> Callable[[], None]:
        """Register a reader to run in the executor after each fetch."""
        self._value_readers.append(reader)

        def remove_value_reader() -> None:
            self._value_readers.remove(reader)

        return remove_value_reader

    @override
    async def _async_update_data(self) -> None:
        """Refresh the gateway's feature payload."""
        await self.hass.async_add_executor_job(self._refresh)

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
        self.new_error_events = {}
        if not self.error_events_supported:
            return
        if self._refreshes_until_error_events > 0:
            self._refreshes_until_error_events -= 1
            return
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
            # Keep the last known errors and try again with the next refresh.
            _LOGGER.debug(
                "Unable to fetch the error events of gateway %s: %s",
                self._accessor.serial,
                err,
            )
            return
        self._refreshes_until_error_events = ERROR_EVENTS_EVERY - 1
        self._process_error_events(page.events)

    def _process_error_events(self, events: list[ViCareEvent]) -> None:
        """Derive the standing errors and the new events from a fetch."""
        errors = sorted(
            (
                (event.device_id, event.error_code, event)
                for event in events
                if event.device_id and event.error_code
            ),
            key=lambda error: event_time(error[2]),
        )
        newest = {(device_id, code): event for device_id, code, event in errors}
        history: defaultdict[str, list[ViCareEvent]] = defaultdict(list)
        for device_id, _, event in errors:
            history[device_id].append(event)
        self.error_history = dict(history)
        active: defaultdict[str, list[ViCareEvent]] = defaultdict(list)
        for (device_id, _), event in newest.items():
            if event.active:
                active[device_id].append(event)
        self.active_errors = dict(active)

        keys = {_error_event_key(event) for _, _, event in errors}
        # The first fetch is the past, only later arrivals are new.
        if self._seen_error_events is not None:
            new: defaultdict[str, list[ViCareEvent]] = defaultdict(list)
            for device_id, _, event in errors:
                if _error_event_key(event) not in self._seen_error_events:
                    new[device_id].append(event)
            self.new_error_events = dict(new)
        # The page always holds the newest events, so what drops off its end
        # cannot come back and need not be remembered.
        self._seen_error_events = keys
