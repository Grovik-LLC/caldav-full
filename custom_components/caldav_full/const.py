"""Constants for the CalDAV Full custom integration.

Adapted from homeassistant/components/caldav/const.py in Home Assistant Core
2026.10.0. Copyright The Home Assistant Authors. Licensed under the Apache
License, Version 2.0.
"""

from typing import Final

DOMAIN: Final = "caldav_full"
TIMEOUT: Final = 30

CONF_CALENDARS: Final = "calendars"
SUPPORTED_COMPONENT: Final = "VEVENT"
CONFIG_ENTRY_DEFAULT_DAYS: Final = 7
ENTITY_ID_SUFFIX: Final = "_full"

# Non-recurring edits, single-occurrence edits, and whole-series
# metadata update and delete are implemented. Range edits are not.
NONRECURRING_MUTATION_AUTHORIZED: Final = True
OCCURRENCE_MUTATION_AUTHORIZED: Final = True
RECURRING_MUTATION_AUTHORIZED: Final = True
