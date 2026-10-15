"""CalDAV API helpers for CalDAV Full.

Adapted from homeassistant/components/caldav/api.py in Home Assistant Core
2026.10.0. Copyright The Home Assistant Authors. Licensed under the Apache
License, Version 2.0.
"""

from typing import cast

from caldav.calendarobjectresource import CalendarObjectResource
from caldav.collection import Calendar
from caldav.davclient import DAVClient
from homeassistant.core import HomeAssistant


async def async_get_calendars(
    hass: HomeAssistant, client: DAVClient, component: str
) -> list[tuple[Calendar, str | None]]:
    """Get all calendars that support the specified component."""

    def _get_calendars() -> list[tuple[Calendar, str | None]]:
        calendars = cast(list[Calendar], client.get_principal().get_calendars())
        return [
            (calendar, cast(str | None, calendar.get_display_name()))
            for calendar in calendars
            if component in cast(list[str], calendar.get_supported_components())
        ]

    return await hass.async_add_executor_job(_get_calendars)


def get_attr_value(obj: CalendarObjectResource, attribute: str) -> str | None:
    """Return the value of the CalDav object attribute if defined."""
    if hasattr(obj, attribute):
        return getattr(obj, attribute).value
    return None


def collection_url(calendar: Calendar) -> str | None:
    """Return the canonical collection URL, which is the stable calendar id."""
    try:
        url = calendar.canonical_url
    except ValueError:
        return None
    if not url:
        return None
    return str(url)
