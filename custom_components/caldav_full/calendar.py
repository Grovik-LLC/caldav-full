"""Calendar platform for CalDAV Full.

Adapted from the config-entry path of
homeassistant/components/caldav/calendar.py in Home Assistant Core 2026.10.0.
Copyright The Home Assistant Authors. Licensed under the Apache License,
Version 2.0.

YAML setup, custom search calendars, and to-do are not implemented.
Update and delete cover one non-recurring event, one occurrence, or a
supported whole-series metadata change or deletion.
"""

import logging
from datetime import datetime
from functools import partial
from typing import Any

from caldav.lib.error import AuthorizationError, DAVError
from caldav.lib.http_sync import requests as caldav_requests
from homeassistant.components.calendar import (
    DOMAIN as CALENDAR_DOMAIN,
)
from homeassistant.components.calendar import (
    CalendarEntity,
    CalendarEntityFeature,
    CalendarEvent,
)
from homeassistant.core import HomeAssistant, callback
from homeassistant.exceptions import HomeAssistantError
from homeassistant.helpers import entity_registry as er
from homeassistant.helpers.entity_platform import AddConfigEntryEntitiesCallback
from homeassistant.helpers.update_coordinator import CoordinatorEntity
from homeassistant.util import slugify

from . import CalDavConfigEntry
from .api import async_get_calendars, collection_url
from .const import (
    CONF_CALENDARS,
    CONFIG_ENTRY_DEFAULT_DAYS,
    DOMAIN,
    ENTITY_ID_SUFFIX,
    SUPPORTED_COMPONENT,
)
from .coordinator import CalDavUpdateCoordinator
from .mutations import (
    apply_changes,
    assert_nonrecurring,
    conditional_delete,
    conditional_put,
    load_unique_event,
)
from .occurrence import (
    delete_occurrence,
    reject_recurrence_range,
    stored_event_kind,
    update_occurrence,
    update_series_metadata,
)

_LOGGER = logging.getLogger(__name__)


async def async_setup_entry(
    hass: HomeAssistant,
    entry: CalDavConfigEntry,
    async_add_entities: AddConfigEntryEntitiesCallback,
) -> None:
    """Set up CalDAV Full calendars for a config entry."""
    selected = set(entry.options.get(CONF_CALENDARS, []))
    calendars = await async_get_calendars(hass, entry.runtime_data, SUPPORTED_COMPONENT)
    discovered: dict[str, tuple[Any, str]] = {}
    for calendar, calendar_name in calendars:
        if not calendar_name:
            continue
        url = collection_url(calendar)
        if url is None:
            _LOGGER.warning(
                "Skipping calendar %s without a collection URL", calendar_name
            )
            continue
        discovered[url] = (calendar, calendar_name)

    for url in sorted(selected - discovered.keys()):
        _LOGGER.warning("Selected calendar is no longer available: %s", url)

    registry = er.async_get(hass)
    entities: list[CalDavCalendarEntity] = []
    for url in selected:
        found = discovered.get(url)
        if found is None:
            continue
        calendar, calendar_name = found
        entity = _entity_for_calendar(
            hass, entry, registry, calendar, calendar_name, url
        )
        if entity is not None:
            entities.append(entity)

    async_add_entities(entities, True)


def _entity_for_calendar(
    hass: HomeAssistant,
    entry: CalDavConfigEntry,
    registry: er.EntityRegistry,
    calendar: Any,
    calendar_name: str,
    url: str,
) -> CalDavCalendarEntity | None:
    """Build one entity, or skip it when the entity id is already owned."""
    object_id = f"{slugify(calendar_name)}{ENTITY_ID_SUFFIX}"
    preferred_entity_id = f"{CALENDAR_DOMAIN}.{object_id}"
    unique_id = f"{DOMAIN}-{entry.entry_id}-{url}"
    registered_entity_id = registry.async_get_entity_id(
        CALENDAR_DOMAIN, DOMAIN, unique_id
    )
    entity_id = registered_entity_id or preferred_entity_id
    if not entity_id.endswith(ENTITY_ID_SUFFIX):
        _LOGGER.error(
            "Refusing to register %s for calendar %s; CalDAV Full entity ids must end with %s",
            entity_id,
            calendar_name,
            ENTITY_ID_SUFFIX,
        )
        return None

    existing = registry.async_get(entity_id)
    if existing is not None and (
        existing.unique_id != unique_id or existing.config_entry_id != entry.entry_id
    ):
        _LOGGER.error(
            "Skipping calendar %s because %s belongs to another entity",
            calendar_name,
            entity_id,
        )
        return None

    coordinator = CalDavUpdateCoordinator(
        hass,
        entry,
        calendar=calendar,
        calendar_name=calendar_name,
        days=CONFIG_ENTRY_DEFAULT_DAYS,
        include_all_day=True,
        search=None,
    )
    return CalDavCalendarEntity(
        calendar_name,
        entity_id if registered_entity_id is None else None,
        coordinator,
        unique_id,
    )


class CalDavCalendarEntity(CoordinatorEntity[CalDavUpdateCoordinator], CalendarEntity):
    """A calendar entity for one selected CalDAV collection."""

    _attr_supported_features = (
        CalendarEntityFeature.CREATE_EVENT
        | CalendarEntityFeature.DELETE_EVENT
        | CalendarEntityFeature.UPDATE_EVENT
    )

    def __init__(
        self,
        name: str | None,
        entity_id: str | None,
        coordinator: CalDavUpdateCoordinator,
        unique_id: str,
    ) -> None:
        """Create the calendar entity."""
        super().__init__(coordinator)
        if entity_id is not None:
            self.entity_id = entity_id
        self._event: CalendarEvent | None = None
        self._attr_name = name
        self._attr_unique_id = unique_id

    @property
    def event(self) -> CalendarEvent | None:
        """Return the next upcoming event."""
        return self._event

    async def async_get_events(
        self, hass: HomeAssistant, start_date: datetime, end_date: datetime
    ) -> list[CalendarEvent]:
        """Get all events in a specific time frame."""
        return await self.coordinator.async_get_events(hass, start_date, end_date)

    async def async_create_event(self, **kwargs: Any) -> None:
        """Create a new event in the calendar."""
        item_data: dict[str, Any] = {
            "summary": kwargs["summary"],
            "dtstart": kwargs["dtstart"],
            "dtend": kwargs["dtend"],
        }
        if description := kwargs.get("description"):
            item_data["description"] = description
        if location := kwargs.get("location"):
            item_data["location"] = location
        if rrule := kwargs.get("rrule"):
            item_data["rrule"] = rrule

        try:
            await self.hass.async_add_executor_job(
                partial(self.coordinator.calendar.add_event, **item_data),
            )
        except (
            caldav_requests.exceptions.ConnectionError,
            caldav_requests.exceptions.Timeout,
            DAVError,
        ) as err:
            raise HomeAssistantError(f"CalDAV save error: {err}") from err

        await self._async_refresh_after_write("create")

    async def async_update_event(
        self,
        uid: str,
        event: dict[str, Any],
        recurrence_id: str | None = None,
        recurrence_range: str | None = None,
    ) -> None:
        """Update one event, one occurrence, or the series master text."""
        reject_recurrence_range(recurrence_range)
        if recurrence_id:
            await self._async_mutate(
                partial(self._update_occurrence, uid, event, recurrence_id),
                "update",
            )
            return
        await self._async_mutate(
            partial(self._update_stored_event, uid, event),
            "update",
        )

    async def async_delete_event(
        self,
        uid: str,
        recurrence_id: str | None = None,
        recurrence_range: str | None = None,
    ) -> None:
        """Delete one event, exclude one occurrence, or delete one series."""
        reject_recurrence_range(recurrence_range)
        if recurrence_id:
            await self._async_mutate(
                partial(self._delete_occurrence, uid, recurrence_id),
                "delete",
            )
            return
        await self._async_mutate(partial(self._delete_stored_event, uid), "delete")

    def _update_stored_event(self, uid: str, event: dict[str, Any]) -> None:
        """Load one resource and update either its event or its series text."""
        stored = load_unique_event(self.coordinator.calendar, uid)
        if stored_event_kind(stored, uid) == "series":
            update_series_metadata(stored, uid, event)
        else:
            assert_nonrecurring(stored, uid)
            apply_changes(stored, event)
        conditional_put(stored)

    def _delete_stored_event(self, uid: str) -> None:
        """Delete one plain event, or the one resource that holds a series."""
        stored = load_unique_event(self.coordinator.calendar, uid)
        if stored_event_kind(stored, uid) != "series":
            assert_nonrecurring(stored, uid)
        conditional_delete(stored)

    def _update_occurrence(
        self, uid: str, event: dict[str, Any], recurrence_id: str
    ) -> None:
        """Load a series and conditionally PUT one occurrence override."""
        stored = load_unique_event(self.coordinator.calendar, uid)
        update_occurrence(stored, uid, recurrence_id, event)
        conditional_put(stored)

    def _delete_occurrence(self, uid: str, recurrence_id: str) -> None:
        """Load a series and conditionally PUT one exclusion."""
        stored = load_unique_event(self.coordinator.calendar, uid)
        delete_occurrence(stored, uid, recurrence_id)
        conditional_put(stored)

    async def _async_mutate(self, operation: Any, action: str) -> None:
        """Run one mutation. A later refresh cannot run it again."""
        try:
            await self.hass.async_add_executor_job(operation)
        except HomeAssistantError:
            raise
        except (
            caldav_requests.exceptions.ConnectionError,
            caldav_requests.exceptions.Timeout,
            AuthorizationError,
            DAVError,
        ) as err:
            raise HomeAssistantError(f"CalDAV {action} error: {err}") from err
        await self._async_refresh_after_write(action)

    async def _async_refresh_after_write(self, action: str) -> None:
        """Refresh views after a successful write without writing again."""
        try:
            await self.coordinator.async_request_refresh()
        except Exception as err:  # noqa: BLE001
            _LOGGER.warning(
                "Calendar refresh after %s failed for %s: %s",
                action,
                self.entity_id,
                err,
            )
        if not self._event_listeners:
            return
        try:
            self.async_update_event_listeners()
        except Exception as err:  # noqa: BLE001
            _LOGGER.warning(
                "Calendar listener update after %s failed for %s: %s",
                action,
                self.entity_id,
                err,
            )

    @callback
    def _handle_coordinator_update(self) -> None:
        """Update event data."""
        self._event = self.coordinator.data
        super()._handle_coordinator_update()

    async def async_added_to_hass(self) -> None:
        """When entity is added to hass update state from existing coordinator data."""
        await super().async_added_to_hass()
        self._handle_coordinator_update()
