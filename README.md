# CalDAV Full

CalDAV Full is a custom Home Assistant calendar integration published by Grovik LLC. It connects to a CalDAV account, lists calendar collections, and exposes only the collections you explicitly confirm.

It is a separate integration. It does not replace or modify Home Assistant's built-in CalDAV integration.

Website: https://www.grovik.co  
Support: contact@grovik.co

## Requirements

- Home Assistant Core 2026.10.0
- Python 3.14.2 or newer
- Network access to the CalDAV server

The integration installs these pinned libraries:

- `caldav==3.3.0a1`
- `icalendar==6.3.1`
- `vobject==0.9.9`

Do not install it on an older Home Assistant Core release. Those library pins match Core 2026.10.0.

## Installation

HACS default-store installation is not available. Custom-repository installation requires a GitHub release. That installation path has not been verified.

### HACS custom repository

1. In HACS, open Integrations, then the menu, and choose Custom repositories.
2. Add `https://github.com/Grovik-LLC/caldav-full` as an Integration.
3. Install CalDAV Full from that repository.
4. Restart Home Assistant.
5. Go to Settings, Devices & services, Add integration, and choose CalDAV Full.
6. Enter the CalDAV account, then select the calendars and activate them.

### Manual copy

1. Copy `custom_components/caldav_full` into the Home Assistant `config/custom_components` directory.
2. Restart Home Assistant.
3. Go to Settings, Devices & services, Add integration, and choose CalDAV Full.

## Configuration

The setup form asks for the CalDAV URL, username, password, and whether to verify the SSL certificate.

After sign-in, CalDAV Full lists calendar names. A new setup starts with nothing selected. Newly discovered calendars stay unselected. Nothing is saved until the confirmation step.

- Continue with no calendars. CalDAV Full creates no calendar entity.
- Activate selected calendars. Each selected collection becomes a calendar entity. That entity can create, update, and delete events.

The same confirmation is required when you change the selection later. Clearing every box and confirming removes the exposed calendars. Closing the dialog does not save.

## What you can do

- Discover VEVENT calendars and choose which ones to expose.
- Read events, including all-day events, timed events, and expanded occurrences of a supported series.
- Create an event. A create request can include a recurrence rule, which creates a new series.
- Update or delete one non-recurring event. The write uses the event's ETag.
- Update one occurrence of a recurring series. CalDAV Full writes an override for that original start.
- Exclude one occurrence of a recurring series. CalDAV Full writes an exclusion and keeps the rest of the series.

A selected calendar polls for the next event about every 15 minutes. Opening a date range reads that range from the server.

## What is not supported

These operations are not supported:

- Whole-series recurring edits.
- Whole-series recurring deletion.
- `THISANDFUTURE` recurrence changes.

A request for any of those is rejected. It does not change the stored series.

Also not supported:

- To-do lists.
- YAML setup.
- Custom search calendars.
- Event-level `RDATE` or `EXRULE` changes, and a series with more than one `RRULE`.

## iCloud

Apple CalDAV interoperability is verified for the operations covered by the accepted test record: calendar discovery, event reads, creating a series, conditional create/update/delete of a non-recurring event, and editing or excluding one occurrence of a supported series.

These limits remain part of that record:

- A recurring occurrence that crosses the spring-forward clock change has not been live-validated.
- Live evidence confirms time-zone data is present. It does not prove every individual time-zone line is preserved byte for byte.
- Lookup of an Apple event can use the observed `{uid}.ics` resource name. That naming is not a guarantee for every CalDAV server.
- Home Assistant still advertises create, update, and delete for the whole entity. A whole-series change is rejected when it is requested.

## Support

Questions and defect reports: contact@grovik.co

Grovik LLC: https://www.grovik.co
