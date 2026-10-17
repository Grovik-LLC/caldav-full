"""Single-occurrence update and delete for one stored CalDAV series.

A change edits the stored ICS and relies on the caller for one conditional
PUT. An occurrence delete adds EXDATE and does not HTTP DELETE the resource.
"""

from datetime import UTC, date, datetime, time, timedelta
from typing import Any

import icalendar
import recurring_ical_events
from homeassistant.exceptions import HomeAssistantError
from icalendar.prop import vDDDTypes

from .mutations import (
    _RECURRENCE_PROPERTIES,
    _replace_text,
    _replace_time,
    apply_component_changes,
)
from .recurrence import _end, format_recurrence_id, is_supported_series

_NOT_SUPPORTED = "Recurring event changes are not supported for this calendar"
_TIME_CHANGE = "Changing the time of a whole series is not supported yet"
_DROPPED_FROM_OVERRIDE = ("rrule", "exdate", "exrule", "rdate")
_LOOKUP_MARGIN = timedelta(days=1)
_ALLOWED_COMPONENTS = {"VEVENT", "VTIMEZONE"}


def reject_recurrence_range(recurrence_range: str | None) -> None:
    """Refuse every recurrence range, including THISANDFUTURE."""
    if recurrence_range:
        raise HomeAssistantError(_NOT_SUPPORTED)


def stored_event_kind(event: Any, uid: str) -> str:
    """Return ``plain`` or ``series`` after rejecting an unsafe body."""
    calendar = _parsed_calendar(event, uid)
    vevents = list(calendar.walk("VEVENT"))
    if len(vevents) == 1 and not any(
        name in vevents[0] for name in _RECURRENCE_PROPERTIES
    ):
        return "plain"
    _require_supported_series(calendar, uid, vevents)
    return "series"


def update_series_metadata(event: Any, uid: str, changes: dict[str, Any]) -> None:
    """Change master text when the payload names one displayed occurrence.

    The lookup window runs from one day before the submitted start to one
    day after the submitted end. An unbounded rule is expanded only inside
    that window. A time change that lands on another occurrence's exact
    displayed interval cannot be distinguished from a metadata edit of that
    occurrence, because the request does not carry a recurrence id. That
    request updates text and does not move the series.
    """
    calendar = _parsed_calendar(event, uid)
    vevents = list(calendar.walk("VEVENT"))
    _require_supported_series(calendar, uid, vevents)
    master = _master(calendar)
    _require_one_displayed_interval(calendar, uid, changes["dtstart"], changes["dtend"])
    _apply_master_metadata(master, changes)
    event.icalendar_instance = calendar


def update_occurrence(
    event: Any, uid: str, recurrence_id: str, changes: dict[str, Any]
) -> None:
    """Add or edit one override on the stored calendar object."""
    calendar = _editable_calendar(event, uid)
    original, override = _resolve(calendar, uid, recurrence_id)
    if override is None:
        override = _new_override(calendar, original)
    apply_component_changes(override, changes)
    event.icalendar_instance = calendar


def delete_occurrence(event: Any, uid: str, recurrence_id: str) -> None:
    """Exclude one original occurrence and drop its override, if any."""
    calendar = _editable_calendar(event, uid)
    original, override = _resolve(calendar, uid, recurrence_id)
    if override is not None:
        calendar.subcomponents = [
            component
            for component in calendar.subcomponents
            if component is not override
        ]
    _add_exdate(_master(calendar), original)
    event.icalendar_instance = calendar


def _editable_calendar(event: Any, uid: str) -> icalendar.Calendar:
    """Parse a copy of the stored ICS and require one supported series."""
    try:
        calendar = icalendar.Calendar.from_ical(event.data)
    except ValueError as err:
        raise HomeAssistantError(f"Calendar event {uid} is ambiguous") from err
    if not is_supported_series(calendar):
        raise HomeAssistantError(_NOT_SUPPORTED)
    vevents = list(calendar.walk("VEVENT"))
    if any(str(component.get("uid", "")) != uid for component in vevents):
        raise HomeAssistantError(f"Calendar event {uid} is ambiguous")
    if _duplicate_recurrence_ids(vevents):
        raise HomeAssistantError(f"Calendar event {uid} is ambiguous")
    return calendar


def _resolve(
    calendar: icalendar.Calendar, uid: str, recurrence_id: str
) -> tuple[date | datetime, Any | None]:
    """Return the original start and an existing override, if there is one."""
    parsed = _parse_recurrence_id(recurrence_id)
    master = _master(calendar)
    if not _same_kind(master["dtstart"].dt, parsed):
        raise HomeAssistantError("Recurrence identity does not match the stored event")
    overrides = _matching_overrides(calendar, recurrence_id, parsed)
    excluded = _matching_exdates(master, recurrence_id, parsed)
    if overrides and excluded:
        raise HomeAssistantError(f"Calendar event {uid} is ambiguous")
    if len(overrides) > 1:
        raise HomeAssistantError(f"Calendar event {uid} is ambiguous")
    if overrides:
        return overrides[0]["RECURRENCE-ID"].dt, overrides[0]
    if excluded:
        raise HomeAssistantError(f"Calendar event {uid} was not found")
    generated = _generated_original(calendar, recurrence_id, parsed)
    if generated is None:
        raise HomeAssistantError(f"Calendar event {uid} was not found")
    return generated, None


def _parse_recurrence_id(value: str) -> date | datetime:
    """Parse the Home Assistant recurrence id string."""
    text = value.strip()
    if len(text) == 10 and text[4] == "-" and text[7] == "-":
        try:
            return date.fromisoformat(text)
        except ValueError as err:
            raise HomeAssistantError("Recurrence identity is malformed") from err
    candidate = text.replace(" ", "T", 1)
    try:
        parsed = datetime.fromisoformat(candidate)
    except ValueError as err:
        raise HomeAssistantError("Recurrence identity is malformed") from err
    return parsed


def _same_kind(stored: date | datetime, parsed: date | datetime) -> bool:
    """Require the same all-day or zoned shape as the master."""
    if isinstance(stored, datetime):
        if not isinstance(parsed, datetime):
            return False
        return (stored.tzinfo is None) == (parsed.tzinfo is None)
    return not isinstance(parsed, datetime)


def _ids_equal(
    stored: date | datetime, requested: str, parsed: date | datetime
) -> bool:
    """Match the formatted id or the same instant."""
    if format_recurrence_id(stored) == requested.strip():
        return True
    if isinstance(stored, datetime) and isinstance(parsed, datetime):
        if (stored.tzinfo is None) != (parsed.tzinfo is None):
            return False
        if stored.tzinfo is None:
            return stored == parsed
        return stored.astimezone(UTC) == parsed.astimezone(UTC)
    if not isinstance(stored, datetime) and not isinstance(parsed, datetime):
        return stored == parsed
    return False


def _matching_overrides(
    calendar: icalendar.Calendar, recurrence_id: str, parsed: date | datetime
) -> list[Any]:
    """Return overrides whose original start is the requested occurrence."""
    return [
        component
        for component in calendar.walk("VEVENT")
        if "RECURRENCE-ID" in component
        and _ids_equal(component["RECURRENCE-ID"].dt, recurrence_id, parsed)
    ]


def _matching_exdates(
    master: Any, recurrence_id: str, parsed: date | datetime
) -> list[date | datetime]:
    """Return EXDATE values that already name this original start."""
    return [
        value
        for value in _exdate_values(master)
        if _ids_equal(value, recurrence_id, parsed)
    ]


def _exdate_values(master: Any) -> list[date | datetime]:
    """Flatten every EXDATE value on the master."""
    raw = master.get("exdate")
    if raw is None:
        return []
    props = raw if isinstance(raw, list) else [raw]
    values: list[date | datetime] = []
    for prop in props:
        dts = getattr(prop, "dts", None)
        if dts:
            values.extend(item.dt for item in dts)
        else:
            values.append(prop.dt)
    return values


def _generated_original(
    calendar: icalendar.Calendar, recurrence_id: str, parsed: date | datetime
) -> date | datetime | None:
    """Return the original start when the rule generates exactly one match."""
    if isinstance(parsed, datetime):
        window_start = parsed - timedelta(days=1)
        window_end = parsed + timedelta(days=1)
    else:
        window_start = datetime.combine(parsed, time.min, tzinfo=UTC) - timedelta(
            days=1
        )
        window_end = window_start + timedelta(days=3)
    try:
        occurrences = recurring_ical_events.of(calendar, components=["VEVENT"]).between(
            window_start, window_end
        )
    except (ValueError, TypeError, AttributeError) as err:
        raise HomeAssistantError("Recurrence identity could not be resolved") from err
    matches = []
    for component in occurrences:
        recurrence = component.get("RECURRENCE-ID")
        if recurrence is None:
            continue
        if _ids_equal(recurrence.dt, recurrence_id, parsed):
            matches.append(recurrence.dt)
    if len(matches) != 1:
        return None
    return matches[0]


def _duplicate_recurrence_ids(vevents: list[Any]) -> bool:
    """Return whether two overrides share one original start."""
    seen: set[str] = set()
    for component in vevents:
        if "RECURRENCE-ID" not in component:
            continue
        identity = format_recurrence_id(component["RECURRENCE-ID"].dt)
        if identity in seen:
            return True
        seen.add(identity)
    return False


def _master(calendar: icalendar.Calendar) -> Any:
    """Return the one master VEVENT."""
    return next(
        component
        for component in calendar.walk("VEVENT")
        if "RRULE" in component and "RECURRENCE-ID" not in component
    )


def _new_override(calendar: icalendar.Calendar, original: date | datetime) -> Any:
    """Copy the master into one override anchored at the original start."""
    master = _master(calendar)
    override = master.copy()
    for child in master.subcomponents:
        override.add_component(child.copy())
    for name in _DROPPED_FROM_OVERRIDE:
        while name in override:
            override.pop(name)
    _move_start(override, original)
    params = dict(master["dtstart"].params)
    override.add("recurrence-id", original, parameters=params)
    calendar.add_component(override)
    return override


def _move_start(component: Any, original: date | datetime) -> None:
    """Place the copied master on the original occurrence."""
    current = component["dtstart"].dt
    delta = original - current
    _replace_time(component, "dtstart", original)
    if "dtend" in component:
        _replace_time(component, "dtend", component["dtend"].dt + delta)


def _add_exdate(master: Any, original: date | datetime) -> None:
    """Record one original start on the master without duplicating it."""
    if any(
        _ids_equal(value, format_recurrence_id(original), original)
        for value in _exdate_values(master)
    ):
        return
    params = _exdate_parameters(master, original)
    raw = master.get("exdate")
    props = [] if raw is None else (raw if isinstance(raw, list) else [raw])
    for prop in props:
        if {str(key).upper(): prop.params[key] for key in prop.params} == {
            str(key).upper(): params[key] for key in params
        }:
            prop.dts.append(vDDDTypes(original))
            return
    master.add("exdate", original, parameters=params)


def _exdate_parameters(master: Any, original: date | datetime) -> dict[str, Any]:
    """Keep TZID, and mark an all-day exclusion as DATE."""
    source = master["dtstart"].params
    params = {key: source[key] for key in source if str(key).upper() == "TZID"}
    if isinstance(original, datetime):
        return params
    value = next(
        (source[key] for key in source if str(key).upper() == "VALUE"),
        "DATE",
    )
    params["VALUE"] = value
    return params


def _parsed_calendar(event: Any, uid: str) -> icalendar.Calendar:
    """Return one parsed calendar, or reject a body that cannot be classified."""
    try:
        calendar = icalendar.Calendar.from_ical(event.data)
    except ValueError as err:
        raise HomeAssistantError(f"Calendar event {uid} is ambiguous") from err
    components = list(calendar.subcomponents)
    if not components or any(
        component.name not in _ALLOWED_COMPONENTS for component in components
    ):
        raise HomeAssistantError(f"Calendar event {uid} is ambiguous")
    vevents = [component for component in components if component.name == "VEVENT"]
    if not vevents or any(
        str(component.get("uid", "")) != uid for component in vevents
    ):
        raise HomeAssistantError(f"Calendar event {uid} is ambiguous")
    return calendar


def _require_supported_series(
    calendar: icalendar.Calendar, uid: str, vevents: list[Any]
) -> None:
    """Require one supported series with distinct, unconflicted identities."""
    if not is_supported_series(calendar):
        raise HomeAssistantError(_NOT_SUPPORTED)
    if _duplicate_recurrence_ids(vevents):
        raise HomeAssistantError(f"Calendar event {uid} is ambiguous")
    if _override_matches_exdate(calendar):
        raise HomeAssistantError(f"Calendar event {uid} is ambiguous")
    master = _master(calendar)
    has_end = "dtend" in master
    has_duration = "duration" in master
    if has_end == has_duration:
        raise HomeAssistantError(f"Calendar event {uid} is ambiguous")


def _override_matches_exdate(calendar: icalendar.Calendar) -> bool:
    """Return whether one override names an excluded original start."""
    excluded = _exdate_values(_master(calendar))
    for component in calendar.walk("VEVENT"):
        if "RECURRENCE-ID" not in component:
            continue
        original = component["RECURRENCE-ID"].dt
        if any(
            _ids_equal(value, format_recurrence_id(original), original)
            for value in excluded
        ):
            return True
    return False


def _require_one_displayed_interval(
    calendar: icalendar.Calendar,
    uid: str,
    start: date | datetime,
    end: date | datetime,
) -> None:
    """Accept one displayed occurrence and reject a changed time."""
    if isinstance(start, datetime) != isinstance(end, datetime):
        raise HomeAssistantError(_TIME_CHANGE)
    if end <= start:
        raise HomeAssistantError("Calendar event end must be after its start")
    window_start, window_end = _lookup_window(start, end)
    try:
        occurrences = recurring_ical_events.of(calendar, components=["VEVENT"]).between(
            window_start, window_end
        )
    except (ValueError, TypeError, AttributeError) as err:
        raise HomeAssistantError("Recurrence identity could not be resolved") from err
    matches = [
        component
        for component in occurrences
        if not _cancelled(component) and _same_interval(component, start, end)
    ]
    if not matches:
        raise HomeAssistantError(_TIME_CHANGE)
    if len(matches) > 1:
        raise HomeAssistantError(f"Calendar event {uid} is ambiguous")


def _lookup_window(
    start: date | datetime, end: date | datetime
) -> tuple[datetime, datetime]:
    """Return the bounded expansion window around one submitted interval."""
    if isinstance(start, datetime) and isinstance(end, datetime):
        return start - _LOOKUP_MARGIN, end + _LOOKUP_MARGIN
    start_day = datetime.combine(start, time.min, tzinfo=UTC)
    end_day = datetime.combine(end, time.min, tzinfo=UTC)
    return start_day - _LOOKUP_MARGIN, end_day + _LOOKUP_MARGIN


def _cancelled(component: Any) -> bool:
    """Return whether expansion still surfaced a cancelled occurrence."""
    return str(component.get("STATUS", "")).lower() == "cancelled"


def _same_interval(
    component: Any, start: date | datetime, end: date | datetime
) -> bool:
    """Return whether one displayed occurrence has this start and end."""
    displayed_start = component["DTSTART"].dt
    displayed_end = _end(component, displayed_start)
    return _same_point(displayed_start, start) and _same_point(displayed_end, end)


def _same_point(stored: date | datetime, submitted: date | datetime) -> bool:
    """Compare instants for timed values and calendar dates for all-day values."""
    if isinstance(stored, datetime) and isinstance(submitted, datetime):
        if (stored.tzinfo is None) != (submitted.tzinfo is None):
            return False
        if stored.tzinfo is None:
            return stored == submitted
        return stored.astimezone(UTC) == submitted.astimezone(UTC)
    if isinstance(stored, datetime) or isinstance(submitted, datetime):
        return False
    return stored == submitted


def _apply_master_metadata(master: Any, changes: dict[str, Any]) -> None:
    """Replace master text and leave the recurrence set untouched."""
    if changes.get("rrule") and not _same_rrule(master, str(changes["rrule"])):
        raise HomeAssistantError(_NOT_SUPPORTED)
    _replace_text(master, "summary", changes["summary"])
    if "description" in changes and changes["description"] is not None:
        _replace_text(master, "description", changes["description"])
    if "location" in changes and changes["location"] is not None:
        _replace_text(master, "location", changes["location"])
    if "sequence" in master:
        sequence = int(master["sequence"])
        master.pop("sequence")
        master.add("sequence", sequence + 1)


def _same_rrule(master: Any, text: str) -> bool:
    """Return whether a payload rule is the stored rule."""
    try:
        incoming = icalendar.vRecur.from_ical(text)
    except ValueError:
        return False
    return master["rrule"] == incoming
