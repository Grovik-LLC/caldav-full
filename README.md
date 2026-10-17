# CalDAV Full

CalDAV Full extends Home Assistant's built-in CalDAV calendar with event editing, deletion, and supported recurring-event management.

It is a separate integration published by Grovik LLC. It does not replace or modify the built-in CalDAV integration. You choose which calendar collections to expose, and nothing is saved until you confirm that choice.

Website: https://www.grovik.co  
Support: contact@grovik.co

## Compared with Home Assistant CalDAV

| | Home Assistant CalDAV | CalDAV Full |
| --- | --- | --- |
| View events | Yes | Yes |
| Create events | Yes | Yes |
| Edit events | No | Yes |
| Delete events | No | Yes |
| Create a recurring series | Yes | Yes |
| Edit or exclude one occurrence | No | Yes, since 0.1.3 |
| Edit a series title, description, or location | No | Yes, new in 0.1.4 |
| Delete a whole series | No | Yes, new in 0.1.4 |

## New in 0.1.4

| Capability | 0.1.3 | 0.1.4 |
| --- | --- | --- |
| Create, view, edit, and delete a single event | Yes | Yes |
| Create a recurring series | Yes | Yes |
| Edit or exclude one occurrence | Yes | Yes |
| Edit the series title, description, and location | No | Yes |
| Delete a whole series | No | Yes |
| Authentication failures classified during setup | Partial | Improved |

Version 0.1.5 is the prepared release candidate. The latest published release remains 0.1.4 until 0.1.5 is tagged and published.

## Requirements

- Home Assistant Core 2026.10.0 or newer
- Python 3.14.2 or newer
- Network access to the CalDAV server

The integration requires these minimum supported library versions:

- `caldav>=3.3.0a1`
- `icalendar>=6.3.1`
- `vobject>=0.9.9`

These minimum versions match the libraries shipped with Home Assistant Core 2026.10.0. Do not install this release on an older Core.

## Installation

Install CalDAV Full from the HACS custom repository at <https://github.com/Grovik-LLC/caldav-full>.

HACS default-store installation is not available.

### HACS custom repository

1. In HACS, open Integrations, then the menu, and choose Custom repositories.
2. Add `https://github.com/Grovik-LLC/caldav-full` as an Integration.
3. Install CalDAV Full from that repository.
4. Restart Home Assistant.
5. Go to Settings, Devices & services, Add integration, and choose CalDAV Full.
6. Enter the CalDAV account, then select the calendars and activate them.

The icon is included for Home Assistant and appears there. HACS can still show a placeholder. Home Assistant's brand service returns that placeholder for a custom integration that is not in the older brand catalog, and HACS uses that response. No extra metadata in this repository changes it.

### Manual copy

1. Copy `custom_components/caldav_full` into the Home Assistant `config/custom_components` directory.
2. Restart Home Assistant.
3. Go to Settings, Devices & services, Add integration, and choose CalDAV Full.

### Upgrade

Update the custom repository in HACS after 0.1.4 is published, or replace `custom_components/caldav_full` and restart. Existing CalDAV Full config entries stay in place. This update does not rewrite events on the calendar server.

## Configuration

The setup form asks for the CalDAV URL, username, password, and whether to verify the SSL certificate.

For Apple iCloud, enter an app-specific password, not the Apple Account password. The account needs two-factor authentication. Sign in at https://account.apple.com, open Sign-In and Security, then App-Specific Passwords, and generate a password. Apple's steps are at https://support.apple.com/en-us/102654. Other CalDAV servers use their own password.

A sign-in that the server rejects is reported as an authentication error during setup and when the integration reloads.

After sign-in, CalDAV Full lists calendar names. A new setup starts with nothing selected. Newly discovered calendars stay unselected. Nothing is saved until the confirmation step.

- Continue with no calendars. CalDAV Full creates no calendar entity.
- Activate selected calendars. Each selected collection becomes a calendar entity. That entity can create, update, and delete events.

The same confirmation is required when you change the selection later. Clearing every box and confirming removes the exposed calendars. Closing the dialog does not save. Saving a changed selection reloads the integration.

## What you can do

- Discover VEVENT calendars and choose which ones to expose.
- Read events, including all-day events, timed events, and expanded occurrences of a supported series.
- Create an event. A create request can include a recurrence rule, which creates a new series.
- Update or delete one non-recurring event. The write uses the event's current ETag.
- Update one occurrence of a recurring series. CalDAV Full writes an override for that original start.
- Exclude one occurrence of a recurring series. CalDAV Full writes an exclusion and keeps the rest of the series.
- Update the title, description, or location of a whole series without moving its start, end, or repeat rule.
- Delete a whole series with one conditional delete of the series resource.

A selected calendar polls for the next event about every 15 minutes. Opening a date range reads that range from the server.

An omitted description or location on a series update leaves the stored value. Sending an empty description or location clears that field.

## Home Assistant calendar editor

The built-in Home Assistant calendar editor can create events, edit one occurrence, and exclude one occurrence. For a recurring event it offers “Only this event” and “This and all future events.” It does not offer an “All events” choice.

Whole-series title, description, location, and deletion are implemented in CalDAV Full. The built-in editor does not send the request those operations require, so they are not available from that screen. “This and all future events” is rejected and does not change the stored series.

Family Calendar Card v3.1.3 was checked on a disposable Home Assistant against one non-recurring event: create, title and location edit, and delete succeeded. Recurring operations were not part of that test. Do not treat the card as validated for series editing or series deletion.

## What is not supported

These operations are rejected and do not change the stored series:

- Moving a whole series, or changing its duration.
- Changing the repeat rule, including how often it repeats and when it ends.
- Applying a change to this occurrence and all future occurrences (`THISANDFUTURE`).

A submitted series time that is not one of the times already shown for that series is rejected. A time that lands on another occurrence’s exact start and end is treated as a text edit of that occurrence. The series anchor does not move.

Also not supported:

- To-do lists.
- YAML setup.
- Custom search calendars.
- Event-level `RDATE` or `EXRULE` changes, and a series with more than one `RRULE`.

## Calendar servers

| Server | Status |
| --- | --- |
| Apple iCloud Calendar | Validated |
| Nextcloud Calendar | Validated |
| Baïkal | Validated |
| Radicale 3.8.3 | Validated |
| SOGo 5.12.12 | Partially compatible |
| Synology Calendar | Not tested |

Validated means isolated tests covered discovery, single-event create, edit, and delete, one-occurrence edit and exclusion, whole-series title, description, and location changes, and whole-series deletion of the current resource. A stale ETag on update was rejected and the event was kept. A stale ETag on delete was rejected and the event was kept, except on SOGo as described below.

SOGo 5.12.12 completed those same operations when the ETag was current, and a stale ETag on update was rejected. SOGo does not honor `If-Match` on DELETE. A delete with a stale, unknown, or malformed tag still removed the event. SOGo is not fully supported for safe concurrent deletion.

Apple iCloud was validated in isolated tests, including whole-series metadata and deletion. A recurring occurrence that crosses the spring-forward clock change has not been live-validated. Production acceptance of an earlier install covered authentication and single-event create, update, delete, and sync. That production acceptance did not cover recurring events.

## Duplicate calendar names

Home Assistant's built-in CalDAV integration and CalDAV Full are separate integrations. If both are configured for the same account, each calendar can appear twice.

A CalDAV Full entity id ends in `_full`. The built-in CalDAV integration does not use that suffix. On the entities page, the Integration column reads CalDAV Full or CalDAV.

Before removing the built-in integration, search dashboards, automations, and scripts for its entity ids. Update those references to the CalDAV Full entity, or remove them, and then delete the old integration.

Removing that Home Assistant integration does not delete calendars or events on the CalDAV server.

## Release notes — 0.1.4

### What's new

- Edit the title, description, and location of a whole recurring series. The series start, end, and repeat rule stay as stored.
- Delete a whole recurring series with one conditional delete.

### What's improved

- Setup and reload now treat a rejected sign-in as an authentication error, including servers that answer with “Authorization Required.”

### Validated calendar servers

Apple iCloud Calendar, Nextcloud Calendar, Baïkal, and Radicale 3.8.3 passed the isolated compatibility tests for the operations in this release. SOGo 5.12.12 is partially compatible. Synology Calendar was not tested.

### Known limitations

- The built-in Home Assistant calendar editor cannot start a whole-series edit or a whole-series delete.
- Moving a series, changing its duration or repeat rule, and editing this-and-future occurrences are not supported.
- SOGo ignores `If-Match` on DELETE, so a stale delete can still remove the event.
- Refreshing the calendar list from the options screen can still report a connection error when the password is rejected.
- An Apple occurrence that crosses the spring-forward clock change has not been live-validated.
- Family Calendar Card was tested for one non-recurring event only.

### Upgrade guidance

Install or update CalDAV Full from the custom repository after this version is published, then restart Home Assistant. Your existing CalDAV Full entry and the events already on the server stay in place.

## Support

Questions and defect reports: contact@grovik.co

Grovik LLC: https://www.grovik.co
