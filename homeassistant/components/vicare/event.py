"""Device error events raised by the ViCare integration."""

from datetime import datetime
from typing import override

from PyViCare.PyViCareEvent import ViCareEvent

from homeassistant.components.event import EventEntity
from homeassistant.core import HomeAssistant, callback
from homeassistant.helpers.entity_platform import AddConfigEntryEntitiesCallback
from homeassistant.util import dt as dt_util

from .coordinator import ViCareCoordinator, event_time
from .entity import ViCareCoordinatorEntity
from .types import ViCareConfigEntry, ViCareDevice
from .utils import has_error_history

EVENT_RAISED = "raised"
EVENT_CLEARED = "cleared"
ATTR_ERROR_TIME = "error_time"


def _build_entities(device_list: list[ViCareDevice]) -> list[ViCareErrorEvent]:
    """Create an error event entity for each device that reports errors."""
    return [
        ViCareErrorEvent(device.coordinator, device)
        for device in device_list
        if device.coordinator is not None and has_error_history(device)
    ]


async def async_setup_entry(
    hass: HomeAssistant,
    config_entry: ViCareConfigEntry,
    async_add_entities: AddConfigEntryEntitiesCallback,
) -> None:
    """Create the ViCare error event entities."""
    async_add_entities(
        await hass.async_add_executor_job(
            _build_entities, config_entry.runtime_data.devices
        )
    )


class ViCareErrorEvent(ViCareCoordinatorEntity, EventEntity):
    """Fires when a device error is raised or cleared.

    An error that comes and goes between two history fetches fires both. After
    a restart the errors that came after the last fired one fire with the next
    refresh, so a fault raised or cleared while Home Assistant was stopped is
    not lost.
    """

    _attr_translation_key = "error"
    _attr_event_types = [EVENT_RAISED, EVENT_CLEARED]

    def __init__(self, coordinator: ViCareCoordinator, device: ViCareDevice) -> None:
        """Initialize the entity."""
        super().__init__(coordinator, "error", device)
        self._vicare_device_id = device.config.getConfig().device_id
        self._pending: list[ViCareEvent] = []
        # When the last fired error happened, until the first read catches up.
        self._catch_up_after: datetime | None = None

    @override
    async def async_added_to_hass(self) -> None:
        """Note when the last fired error happened, to catch up from there."""
        if (
            (last_event := await self.async_get_last_event_data())
            and last_event.last_event_attributes
            and isinstance(
                error_time := last_event.last_event_attributes.get(ATTR_ERROR_TIME),
                str,
            )
        ):
            self._catch_up_after = dt_util.parse_datetime(error_time)
        # Primes the first read, which picks up what is to catch up on.
        await super().async_added_to_hass()

    @override
    def _read_value(self) -> None:
        """Pick up the error events of this device that are still to fire."""
        if (catch_up_after := self._catch_up_after) is not None:
            self._catch_up_after = None
            self._pending.extend(
                event
                for event in self.coordinator.error_history.get(
                    self._vicare_device_id, ()
                )
                if event_time(event) > catch_up_after
            )
        else:
            self._pending.extend(
                self.coordinator.new_error_events.get(self._vicare_device_id, ())
            )

    @callback
    @override
    def _handle_coordinator_update(self) -> None:
        """Fire the events picked up so far, oldest first."""
        # A failed refresh notifies too without reading, so take them once.
        pending, self._pending = self._pending, []
        for event in pending:
            self._trigger_event(
                EVENT_RAISED if event.active else EVENT_CLEARED,
                {
                    "error_code": event.error_code,
                    "error_description": event.error_description,
                    ATTR_ERROR_TIME: event.event_timestamp.isoformat()
                    if event.event_timestamp
                    else None,
                },
            )
            self.async_write_ha_state()
        if not pending:
            super()._handle_coordinator_update()
