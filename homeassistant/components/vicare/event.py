"""Device error events raised by the ViCare integration."""

from typing import override

from PyViCare.PyViCareEvent import ViCareEvent

from homeassistant.components.event import EventEntity
from homeassistant.core import HomeAssistant, callback
from homeassistant.helpers.entity_platform import AddConfigEntryEntitiesCallback

from .coordinator import ViCareCoordinator
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

    An error that comes and goes between two history fetches fires both. The
    coordinator saves the history, so the errors raised or cleared while Home
    Assistant was stopped fire after it starts again.
    """

    _attr_translation_key = "error"
    _attr_event_types = [EVENT_RAISED, EVENT_CLEARED]

    def __init__(self, coordinator: ViCareCoordinator, device: ViCareDevice) -> None:
        """Initialize the entity."""
        super().__init__(coordinator, "error", device)
        self._vicare_device_id = device.config.getConfig().device_id
        self._pending: list[ViCareEvent] = []

    @property
    @override
    def available(self) -> bool:
        """Return whether the event history can still be read."""
        return super().available and self.coordinator.error_events_supported

    @override
    def _read_value(self) -> None:
        """Take the new error events of this device."""
        self._pending.extend(
            self.coordinator.take_new_error_events(self._vicare_device_id)
        )

    @callback
    @override
    def _handle_coordinator_update(self) -> None:
        """Fire the events taken so far, oldest first, once available."""
        # Write the availability first: triggers ignore an event that comes
        # straight from unavailable.
        super()._handle_coordinator_update()
        if not self.available:
            return
        pending, self._pending = self._pending, []
        for event in pending:
            error_time = event.event_timestamp or event.created_at
            self._trigger_event(
                EVENT_RAISED if event.active else EVENT_CLEARED,
                {
                    "error_code": event.error_code,
                    "error_description": event.error_description,
                    ATTR_ERROR_TIME: error_time.isoformat() if error_time else None,
                },
            )
            self.async_write_ha_state()
