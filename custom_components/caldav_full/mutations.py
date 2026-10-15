"""Non-recurring CalDAV event update and delete.

python-caldav 3.3.0a1 looks up an event with
``Calendar.get_event_by_uid(uid)``. That method uses ``error.assert_`` to
require one match. With this library version the default debug mode is
PRODUCTION, and ``assert_`` then logs and continues, so the method can return
the first of several matches. Lookup here uses the same ``Calendar.search``
arguments and then requires exactly one exact UID match.

A calendar-query result from Radicale can name the right object and still
omit ``getetag``. ``Event.load`` is not used to fill that gap: on 404 it can
replace the resource URL through another UID lookup, and the HTTP session
follows redirects. After the unique match, lookup GETs that resource once
with redirects disabled and keeps the ICS and ETag from that response.

``Event.save`` sends ``If-Match`` when an ETag is cached, but a failed PUT is
retried through vobject, which can drop properties. ``Event.delete`` and the
synchronous ``DAVClient.delete`` do not accept an ``If-Match`` header.
Conditional writes therefore use ``DAVClient.put`` and ``DAVClient.request``
directly, once, with the ETag returned by the lookup. Those requests use
``text/calendar`` and do not follow redirects.

A UID ``calendar-query`` that returns HTTP 412 is followed by one GET of
``{uid}.ics`` under the selected collection. That file name is a naming
convention observed on Apple calendar collections. It is not a CalDAV
requirement, and a query that succeeds or fails with any other status does
not use it. The fetched resource may be one VEVENT or one supported series:
a single master plus overrides that share its UID. Any other multi-event
body stays rejected.
"""

from collections.abc import Callable
from datetime import date, datetime, timedelta
from typing import Any
from urllib.parse import quote, unquote, urlsplit

import icalendar
from caldav.base_client import ICALH
from caldav.calendarobjectresource import Event
from caldav.elements import dav
from caldav.lib.error import NotFoundError, ReportError
from caldav.lib.http_sync import requests as caldav_requests
from homeassistant.exceptions import HomeAssistantError

from .api import collection_url
from .recurrence import format_recurrence_id, is_supported_series

_RECURRENCE_PROPERTIES = ("rrule", "rdate", "exdate", "exrule", "recurrence-id")
_ALLOWED_COMPONENTS = {"VEVENT", "VTIMEZONE"}
_SUCCESS_PUT = {200, 201, 204}
_SUCCESS_DELETE = {200, 204}
_REDIRECT_STATUSES = {301, 302, 303, 307, 308}
_DEFAULT_PORTS = {"https": 443, "http": 80}


def reject_recurrence_request(
    recurrence_id: str | None, recurrence_range: str | None
) -> None:
    """Refuse occurrence and range edits before any CalDAV call."""
    if recurrence_id or recurrence_range:
        raise HomeAssistantError(
            "Recurring event changes are not supported for this calendar"
        )


def load_unique_event(calendar: Any, uid: str) -> Any:
    """Reload the one stored object whose UID is exactly ``uid``."""
    if not uid:
        raise HomeAssistantError("Calendar event uid is missing")
    try:
        found = calendar.search(
            uid=uid,
            comp_class=Event,
            post_filter=True,
            _hacks="insist",
            include_completed=True,
        )
    except NotFoundError as err:
        raise HomeAssistantError(f"Calendar event {uid} was not found") from err
    except ReportError as err:
        if _report_status(err) != 412:
            raise
        return _load_uid_named_resource(calendar, uid)
    matches = [item for item in found if getattr(item, "id", None) == uid]
    if not matches:
        raise HomeAssistantError(f"Calendar event {uid} was not found")
    if len(matches) != 1:
        raise HomeAssistantError(f"Calendar event {uid} is ambiguous")
    event = matches[0]
    _require_collection_resource(calendar, event, uid)
    _refresh_authoritative(event, uid)
    return event


def _require_collection_resource(calendar: Any, event: Any, uid: str) -> None:
    """Refuse a resource URL that is not inside the selected collection."""
    collection = collection_url(calendar)
    resource = getattr(event, "url", None)
    if (
        collection is None
        or resource is None
        or not _url_inside(collection, str(resource))
    ):
        raise HomeAssistantError(
            f"Calendar event {uid} is outside the selected calendar"
        )


def _url_inside(collection: str, resource: str) -> bool:
    """Return whether ``resource`` is a child of the collection URL."""
    try:
        collection_parts = urlsplit(collection)
        resource_parts = urlsplit(resource)
    except ValueError:
        return False
    if (
        resource_parts.scheme != collection_parts.scheme
        or (resource_parts.hostname or "").lower()
        != (collection_parts.hostname or "").lower()
        or _comparable_port(resource_parts) != _comparable_port(collection_parts)
        or resource_parts.username is not None
        or resource_parts.password is not None
    ):
        return False
    collection_path = unquote(collection_parts.path or "/")
    resource_path = unquote(resource_parts.path or "")
    if not collection_path.endswith("/"):
        collection_path += "/"
    segments = [segment for segment in resource_path.split("/") if segment]
    if any(segment in {".", ".."} for segment in segments):
        return False
    return (
        resource_path.startswith(collection_path) and resource_path != collection_path
    )


def _comparable_port(parts: Any) -> int | None:
    """Return the port used for an origin comparison, including defaults."""
    if parts.port is not None:
        return int(parts.port)
    return _DEFAULT_PORTS.get(parts.scheme)


def _report_status(err: ReportError) -> int | None:
    """Return the HTTP status carried by a REPORT error, when it has one."""
    for text in (err.reason, err.url):
        if not text:
            continue
        token = str(text).strip().split(None, 1)[0].rstrip(",")
        if token.isdigit():
            return int(token)
    return None


def _load_uid_named_resource(calendar: Any, uid: str) -> Any:
    """GET ``{uid}.ics`` once after Apple rejects the UID calendar-query.

    The file name is an observed Apple collection convention. The ICS body
    and ETag still have to identify this UID. A filename match is not enough.
    """
    _require_safe_uid(uid)
    collection = collection_url(calendar)
    if collection is None:
        raise HomeAssistantError(
            f"Calendar event {uid} is outside the selected calendar"
        )
    candidate = _uid_resource_url(collection, uid)
    if not _url_inside(collection, candidate):
        raise HomeAssistantError(
            f"Calendar event {uid} is outside the selected calendar"
        )
    try:
        event = Event(
            client=getattr(calendar, "client", None),
            url=candidate,
            parent=calendar,
        )
    except ValueError as err:
        raise HomeAssistantError(
            f"Calendar event {uid} is outside the selected calendar"
        ) from err
    if not _url_inside(collection, str(event.url)):
        raise HomeAssistantError(
            f"Calendar event {uid} is outside the selected calendar"
        )
    _refresh_authoritative(event, uid)
    _require_fallback_resource(event, uid)
    return event


def _require_safe_uid(uid: str) -> None:
    """Reject a UID that is not one safe path segment."""
    if _unsafe_uid(uid):
        raise HomeAssistantError("Calendar event uid is invalid")


def _unsafe_uid(uid: str) -> bool:
    """Return whether a UID could leave the collection as a path segment."""
    current = uid
    for _step in range(8):
        if _bad_uid_text(current):
            return True
        decoded = unquote(current)
        if decoded == current:
            return False
        current = decoded
    return True


def _bad_uid_text(value: str) -> bool:
    """Return whether this decoded text is empty or a path escape."""
    if not value or any(ord(char) < 32 or char == "\x7f" for char in value):
        return True
    if any(char in value for char in "/\\?#"):
        return True
    segments = value.replace("\\", "/").split("/")
    return any(segment in {".", ".."} for segment in segments)


def _uid_resource_url(collection: str, uid: str) -> str:
    """Return the candidate child URL for one validated UID."""
    base = collection if collection.endswith("/") else f"{collection}/"
    return f"{base}{quote(uid, safe='')}.ics"


def _require_fallback_resource(event: Any, uid: str) -> None:
    """Accept one event, or one supported series, from the filename fallback.

    A series is the shape occurrence edit already allows: one master and
    overrides that share its UID and use distinct original recurrence ids.
    """
    if str(event.data or "").upper().count("BEGIN:VCALENDAR") != 1:
        raise HomeAssistantError(f"Calendar event {uid} is ambiguous")
    try:
        calendar = icalendar.Calendar.from_ical(event.data)
    except Exception as err:
        raise HomeAssistantError(f"Calendar event {uid} could not be read") from err
    vevents = [
        component for component in calendar.subcomponents if component.name == "VEVENT"
    ]
    if len(vevents) == 1 and _plain_event(vevents[0]):
        return
    if not is_supported_series(calendar):
        if _unsupported_recurrence(vevents):
            raise HomeAssistantError(
                "Recurring event changes are not supported for this calendar"
            )
        raise HomeAssistantError(f"Calendar event {uid} is ambiguous")
    master = next(
        component
        for component in vevents
        if "RRULE" in component and "RECURRENCE-ID" not in component
    )
    seen: set[str] = set()
    for component in vevents:
        if component is master:
            continue
        if "RECURRENCE-ID" not in component:
            raise HomeAssistantError(f"Calendar event {uid} is ambiguous")
        try:
            identity = format_recurrence_id(component["RECURRENCE-ID"].dt)
        except (AttributeError, TypeError, ValueError) as err:
            raise HomeAssistantError(f"Calendar event {uid} is ambiguous") from err
        if not identity or identity in seen:
            raise HomeAssistantError(f"Calendar event {uid} is ambiguous")
        seen.add(identity)


def _plain_event(component: Any) -> bool:
    """Return whether one VEVENT has no recurrence properties."""
    return not any(name in component for name in _RECURRENCE_PROPERTIES)


def _unsupported_recurrence(vevents: list[Any]) -> bool:
    """Return whether a VEVENT uses a recurrence form mutation refuses."""
    if any(name in component for component in vevents for name in ("rdate", "exrule")):
        return True
    return any(_rrule_count(component) > 1 for component in vevents)


def _rrule_count(component: Any) -> int:
    """Count RRULE properties on one component."""
    try:
        items = component.property_items(recursive=False)
    except TypeError:
        items = component.property_items()
    return sum(1 for key, _value in items if str(key).upper() == "RRULE")


def _refresh_authoritative(event: Any, uid: str) -> None:
    """Replace the search body and ETag with one direct GET of the same URL."""
    client = event.client
    if client is None or getattr(client, "session", None) is None:
        raise HomeAssistantError("Calendar event has no CalDAV client")
    session = client.session
    previous_redirects = session.max_redirects
    session.max_redirects = 0
    try:
        try:
            response = client.request(str(event.url))
        except caldav_requests.exceptions.TooManyRedirects as err:
            raise HomeAssistantError(
                f"Calendar event {uid} is outside the selected calendar"
            ) from err
    finally:
        session.max_redirects = previous_redirects
    status = getattr(response, "status", None)
    if status is None:
        status = getattr(response, "status_code", None)
    if status in _REDIRECT_STATUSES:
        raise HomeAssistantError(
            f"Calendar event {uid} is outside the selected calendar"
        )
    if status == 404:
        raise HomeAssistantError(f"Calendar event {uid} was not found")
    if status in {401, 403}:
        raise HomeAssistantError("CalDAV authentication failed")
    if status != 200:
        raise HomeAssistantError(f"Calendar event {uid} could not be read")
    etag = _usable_etag(_header(getattr(response, "headers", None), "ETag"))
    body = _response_text(getattr(response, "raw", None))
    if not body:
        raise HomeAssistantError(f"Calendar event {uid} could not be read")
    if etag is None:
        raise HomeAssistantError(
            f"Calendar event {uid} cannot be changed safely without an ETag"
        )
    try:
        event.data = body
    except Exception as err:
        raise HomeAssistantError(f"Calendar event {uid} could not be read") from err
    if getattr(event, "props", None) is None:
        event.props = {}
    event.props[dav.GetEtag.tag] = etag
    _require_fetched_uid(event, uid)


def _header(headers: Any, name: str) -> Any:
    """Return one response header without treating a missing map as a value."""
    if headers is None or not hasattr(headers, "get"):
        return None
    return headers.get(name)


def _response_text(raw: Any) -> str | None:
    """Return a GET body only when that body is text from the same response."""
    if isinstance(raw, bytes):
        return raw.decode("utf-8", errors="replace")
    if isinstance(raw, str) and raw.strip():
        return raw
    return None


def _usable_etag(value: Any) -> str | None:
    """Return a specific ETag, or None when the header cannot condition a write."""
    if not isinstance(value, str):
        return None
    if "\n" in value or "\r" in value:
        return None
    etag = value.strip()
    if not etag or etag == "*":
        return None
    opaque = etag.removeprefix("W/")
    if len(opaque) < 3 or not opaque.startswith('"') or not opaque.endswith('"'):
        return None
    if '"' in opaque[1:-1] or not opaque[1:-1]:
        return None
    return etag


def _require_fetched_uid(event: Any, uid: str) -> None:
    """Require the fetched calendar to parse and still carry only this UID."""
    try:
        calendar = icalendar.Calendar.from_ical(event.data)
    except Exception as err:
        raise HomeAssistantError(f"Calendar event {uid} could not be read") from err
    components = list(getattr(calendar, "subcomponents", []))
    if any(component.name not in _ALLOWED_COMPONENTS for component in components):
        raise HomeAssistantError(f"Calendar event {uid} is ambiguous")
    vevents = [component for component in components if component.name == "VEVENT"]
    if not vevents or any(
        str(component.get("uid", "")) != uid for component in vevents
    ):
        raise HomeAssistantError(f"Calendar event {uid} is ambiguous")


def assert_nonrecurring(event: Any, uid: str) -> None:
    """Reject a resource that is recurring, overridden, or ambiguous."""
    calendar = event.icalendar_instance
    components = list(calendar.subcomponents)
    if any(component.name not in _ALLOWED_COMPONENTS for component in components):
        raise HomeAssistantError(f"Calendar event {uid} is ambiguous")
    vevents = [component for component in components if component.name == "VEVENT"]
    if len(vevents) != 1:
        raise HomeAssistantError(f"Calendar event {uid} is ambiguous")
    vevent = vevents[0]
    if str(vevent.get("uid", "")) != uid:
        raise HomeAssistantError(f"Calendar event {uid} is ambiguous")
    if any(name in vevent for name in _RECURRENCE_PROPERTIES):
        raise HomeAssistantError(
            "Recurring event changes are not supported for this calendar"
        )
    has_end = "dtend" in vevent
    has_duration = "duration" in vevent
    if has_end == has_duration:
        raise HomeAssistantError(f"Calendar event {uid} is ambiguous")


def apply_changes(event: Any, changes: dict[str, Any]) -> None:
    """Replace supported properties on the stored VEVENT."""
    apply_component_changes(_vevent(event), changes)


def apply_component_changes(vevent: Any, changes: dict[str, Any]) -> None:
    """Replace supported properties on one VEVENT component."""
    if changes.get("rrule"):
        raise HomeAssistantError(
            "Recurring event changes are not supported for this calendar"
        )
    start = _coerce_time(vevent["dtstart"].dt, changes["dtstart"], "Start")
    end = _coerce_time(_end_value(vevent), changes["dtend"], "End")
    if end <= start:
        raise HomeAssistantError("Calendar event end must be after its start")
    _replace_text(vevent, "summary", changes["summary"])
    if "description" in changes and changes["description"] is not None:
        _replace_text(vevent, "description", changes["description"])
    if "location" in changes and changes["location"] is not None:
        _replace_text(vevent, "location", changes["location"])
    _replace_time(vevent, "dtstart", start)
    if "dtend" in vevent:
        _replace_time(vevent, "dtend", end)
    else:
        _replace_duration(vevent, end - start)
    if "sequence" in vevent:
        sequence = int(vevent["sequence"])
        vevent.pop("sequence")
        vevent.add("sequence", sequence + 1)


def conditional_put(event: Any) -> None:
    """PUT the stored object once with its current ETag."""
    client = event.client
    if client is None:
        raise HomeAssistantError("Calendar event has no CalDAV client")
    response = _call_without_redirects(
        client,
        "update",
        lambda: client.put(
            str(event.url),
            event.data,
            {"If-Match": event.etag, **ICALH},
        ),
    )
    _raise_for_write(response, "update", _SUCCESS_PUT)


def conditional_delete(event: Any) -> None:
    """DELETE the stored object once with its current ETag."""
    client = event.client
    if client is None:
        raise HomeAssistantError("Calendar event has no CalDAV client")
    response = _call_without_redirects(
        client,
        "delete",
        lambda: client.request(
            str(event.url),
            "DELETE",
            "",
            {"If-Match": event.etag},
        ),
    )
    _raise_for_write(response, "delete", _SUCCESS_DELETE)


def _call_without_redirects(client: Any, action: str, send: Callable[[], Any]) -> Any:
    """Send one mutation and do not follow a redirect."""
    session = getattr(client, "session", None)
    if session is None or not hasattr(session, "max_redirects"):
        return send()
    previous_redirects = session.max_redirects
    session.max_redirects = 0
    try:
        try:
            return send()
        except caldav_requests.exceptions.TooManyRedirects as err:
            status = getattr(getattr(err, "response", None), "status_code", None)
            if status is None:
                status = getattr(getattr(err, "response", None), "status", None)
            shown = status if status is not None else "redirect"
            raise HomeAssistantError(f"CalDAV {action} failed: {shown}") from err
    finally:
        session.max_redirects = previous_redirects


def _vevent(event: Any) -> Any:
    """Return the single VEVENT already accepted by ``assert_nonrecurring``."""
    return next(
        component
        for component in event.icalendar_instance.subcomponents
        if component.name == "VEVENT"
    )


def _end_value(vevent: Any) -> datetime | date:
    """Return the stored end as a date or datetime."""
    if "dtend" in vevent:
        return vevent["dtend"].dt
    start = vevent["dtstart"].dt
    return start + vevent["duration"].dt


def _coerce_time(original: datetime | date, new: Any, label: str) -> datetime | date:
    """Keep the stored all-day or time-zone shape."""
    if isinstance(original, datetime):
        if not isinstance(new, datetime):
            raise HomeAssistantError(f"{label} must stay a timed value")
        if (original.tzinfo is None) != (new.tzinfo is None):
            raise HomeAssistantError(
                f"{label} time zone does not match the stored event"
            )
        if original.tzinfo is not None:
            return new.astimezone(original.tzinfo)
        return new
    if isinstance(new, datetime) or not isinstance(new, date):
        raise HomeAssistantError(f"{label} must stay an all-day date")
    return new


def _replace_text(component: Any, name: str, value: str) -> None:
    """Replace one text property, or remove it when value is empty."""
    if name in component:
        component.pop(name)
    if value:
        component.add(name, value)


def _replace_time(component: Any, name: str, value: datetime | date) -> None:
    """Replace a date property and keep its parameters, including TZID."""
    params = dict(component[name].params)
    component.pop(name)
    component.add(name, value, parameters=params)


def _replace_duration(component: Any, value: timedelta) -> None:
    """Replace DURATION and keep its parameters."""
    params = dict(component["duration"].params)
    component.pop("duration")
    component.add("duration", value, parameters=params)


def _raise_for_write(response: Any, action: str, success: set[int]) -> None:
    """Map one HTTP response. Callers do not retry."""
    status = getattr(response, "status", None)
    if status == 412:
        raise HomeAssistantError(
            f"CalDAV {action} conflict: the event changed on the server"
        )
    if status in (401, 403):
        raise HomeAssistantError(f"CalDAV {action} authentication failed")
    if status not in success:
        raise HomeAssistantError(f"CalDAV {action} failed: {status}")
