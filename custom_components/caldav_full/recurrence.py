"""Recurrence identity for calendar range reads.

An occurrence is the series UID plus its original start. Expansion uses
``recurring-ical-events``, which python-caldav 3.3.0a1 already uses, on a
parsed copy of the ICS. The stored CalDAV object is not edited or saved.
"""

import logging
from datetime import date, datetime, timedelta
from typing import Any

import icalendar
import recurring_ical_events
from homeassistant.components.calendar import CalendarEvent, CalendarEventStatus

_LOGGER = logging.getLogger(__name__)

_MUTATION_UNSUPPORTED = ("exrule", "rdate")
_READ_UNSUPPORTED = ("exrule",)


def expand_series_events(
    raw_ics: str,
    start: datetime,
    end: datetime,
    *,
    to_local: Any,
) -> list[CalendarEvent] | None:
    """Return occurrences for one supported series.

    ``None`` means this ICS is not a supported series. The caller keeps the
    existing single-event read. An empty list means the series is supported
    and no occurrence falls in the window.
    """
    try:
        calendar = icalendar.Calendar.from_ical(raw_ics)
    except ValueError:
        _LOGGER.debug("Skipping recurrence expansion for unreadable calendar data")
        return None
    if not _is_readable_series(list(calendar.walk("VEVENT"))):
        return None
    try:
        occurrences = recurring_ical_events.of(calendar, components=["VEVENT"]).between(
            start, end
        )
    except ValueError, TypeError, AttributeError:
        _LOGGER.warning("Recurrence expansion failed; occurrence identity was omitted")
        return None
    if any(component.get("RECURRENCE-ID") is None for component in occurrences):
        _LOGGER.warning(
            "Recurrence expansion omitted an original start; occurrence identity was not returned"
        )
        return None
    events = [_event_from_component(component, to_local) for component in occurrences]
    return _without_ambiguous_identity(events)


def is_supported_series(calendar: icalendar.Calendar) -> bool:
    """Return whether occurrence mutation may edit this calendar.

    A VEVENT ``RDATE`` stays unsupported for update and delete. Range reads
    use ``_is_readable_series``, which can expand that same property.
    """
    vevents = list(calendar.walk("VEVENT"))
    if any(
        name in component for component in vevents for name in _MUTATION_UNSUPPORTED
    ):
        return False
    return _is_readable_series(vevents)


def _is_readable_series(vevents: list[Any]) -> bool:
    """Accept one master RRULE, DATE or DATE-TIME RDATE, overrides, and EXDATE.

    ``EXRULE`` and PERIOD ``RDATE`` stay unsupported. Time-zone components are
    not VEVENTs, so historical ``RDATE`` values inside ``VTIMEZONE`` do not
    make a single event look like a series.
    """
    if not vevents:
        return False
    if any(name in component for component in vevents for name in _READ_UNSUPPORTED):
        return False
    if any(_has_period_rdate(component) for component in vevents):
        return False
    if any(_property_count(component, "RRULE") > 1 for component in vevents):
        return False
    masters = [
        component
        for component in vevents
        if "RRULE" in component and "RECURRENCE-ID" not in component
    ]
    return len(masters) == 1


def _has_period_rdate(component: Any) -> bool:
    """Return whether one VEVENT stores a period RDATE."""
    for key, value in _properties(component):
        if str(key).upper() != "RDATE":
            continue
        params = getattr(value, "params", {})
        if str(params.get("VALUE", "")).upper() == "PERIOD":
            return True
        for item in getattr(value, "dts", []):
            if isinstance(getattr(item, "dt", None), tuple):
                return True
    return False


def _properties(component: Any) -> list[tuple[Any, Any]]:
    """Return one component's properties without walking children."""
    try:
        return list(component.property_items(recursive=False))
    except TypeError:
        return list(component.property_items())


def _property_count(component: Any, name: str) -> int:
    """Count repeated properties. One RRULE is normal. Two are ambiguous."""
    return sum(1 for key, _value in _properties(component) if str(key).upper() == name)


def _event_from_component(component: Any, to_local: Any) -> CalendarEvent:
    """Map one expanded occurrence. Display times can differ from the id."""
    start = component["DTSTART"].dt
    recurrence = component.get("RECURRENCE-ID")
    recurrence_id = None
    if recurrence is not None:
        recurrence_id = format_recurrence_id(recurrence.dt)
    return CalendarEvent(
        summary=str(component.get("SUMMARY") or ""),
        start=to_local(start),
        end=to_local(_end(component, start)),
        location=_text(component, "LOCATION"),
        description=_text(component, "DESCRIPTION"),
        uid=_text(component, "UID"),
        recurrence_id=recurrence_id,
        status=_status(component),
    )


def format_recurrence_id(value: date | datetime) -> str:
    """Format an original start the same way a vobject value stringifies."""
    if isinstance(value, datetime):
        return str(value)
    return value.isoformat()


def _end(component: Any, start: date | datetime) -> date | datetime:
    """Return the occurrence end from DTEND or DURATION."""
    if "DTEND" in component:
        end = component["DTEND"].dt
    elif "DURATION" in component:
        end = start + component["DURATION"].dt
    else:
        end = start + timedelta(days=1)
    if not isinstance(end, datetime) and start == end:
        return end + timedelta(days=1)
    return end


def _text(component: Any, name: str) -> str | None:
    """Return one text property."""
    if name not in component:
        return None
    return str(component[name])


def _status(component: Any) -> CalendarEventStatus | None:
    """Return confirmed or tentative, matching the range-read status filter."""
    if "STATUS" not in component:
        return None
    try:
        return CalendarEventStatus(str(component["STATUS"]).lower())
    except ValueError:
        return None


def _without_ambiguous_identity(events: list[CalendarEvent]) -> list[CalendarEvent]:
    """Drop a recurrence id that points at more than one returned occurrence."""
    counts: dict[tuple[str | None, str], int] = {}
    for event in events:
        if event.recurrence_id is None:
            continue
        key = (event.uid, event.recurrence_id)
        counts[key] = counts.get(key, 0) + 1
    if all(count == 1 for count in counts.values()):
        return events
    cleared: list[CalendarEvent] = []
    for event in events:
        if (
            event.recurrence_id is not None
            and counts[(event.uid, event.recurrence_id)] > 1
        ):
            _LOGGER.warning(
                "Omitting ambiguous recurrence identity for %s at %s",
                event.uid,
                event.recurrence_id,
            )
            event.recurrence_id = None
        cleared.append(event)
    return cleared
