"""The CalDAV Full custom integration.

Adapted from homeassistant/components/caldav/__init__.py in Home Assistant
Core 2026.10.0. Copyright The Home Assistant Authors. Licensed under the
Apache License, Version 2.0.

This module sets up a calendar platform only. It does not set up to-do.
"""

import logging
from functools import partial

from caldav.davclient import DAVClient
from caldav.lib.error import AuthorizationError, DAVError
from caldav.lib.http_sync import requests as caldav_requests
from homeassistant.config_entries import ConfigEntry
from homeassistant.const import (
    CONF_PASSWORD,
    CONF_URL,
    CONF_USERNAME,
    CONF_VERIFY_SSL,
    Platform,
)
from homeassistant.core import HomeAssistant
from homeassistant.exceptions import ConfigEntryAuthFailed, ConfigEntryNotReady

from .const import DOMAIN, TIMEOUT

type CalDavConfigEntry = ConfigEntry[DAVClient]

_LOGGER = logging.getLogger(__name__)

PLATFORMS: list[Platform] = [Platform.CALENDAR]


async def async_setup_entry(hass: HomeAssistant, entry: CalDavConfigEntry) -> bool:
    """Set up CalDAV Full from a config entry."""
    client = await hass.async_add_executor_job(
        partial(
            DAVClient,
            entry.data[CONF_URL],
            username=entry.data[CONF_USERNAME],
            password=entry.data[CONF_PASSWORD],
            ssl_verify_cert=entry.data[CONF_VERIFY_SSL],
            timeout=TIMEOUT,
        )
    )
    try:
        await hass.async_add_executor_job(client.get_principal)
    except AuthorizationError as err:
        if err.reason == "Unauthorized":
            raise ConfigEntryAuthFailed("Credentials error from CalDAV server") from err
        # AuthorizationError can be raised if the url is incorrect or
        # on some other unexpected server response.
        _LOGGER.warning("Unexpected CalDAV server response: %s", err)
        return False
    except caldav_requests.exceptions.Timeout as err:
        raise ConfigEntryNotReady("Timeout connecting to CalDAV server") from err
    except caldav_requests.exceptions.ConnectionError as err:
        raise ConfigEntryNotReady("Connection error from CalDAV server") from err
    except DAVError as err:
        raise ConfigEntryNotReady("CalDAV client error") from err

    entry.runtime_data = client
    await hass.config_entries.async_forward_entry_setups(entry, PLATFORMS)
    return True


async def async_unload_entry(hass: HomeAssistant, entry: ConfigEntry) -> bool:
    """Unload a config entry."""
    return await hass.config_entries.async_unload_platforms(entry, PLATFORMS)


__all__ = ["DOMAIN", "CalDavConfigEntry"]
