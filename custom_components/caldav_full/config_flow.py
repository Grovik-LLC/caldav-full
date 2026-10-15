"""Configuration flow for CalDAV Full.

Adapted from homeassistant/components/caldav/config_flow.py in Home Assistant
Core 2026.10.0. Copyright The Home Assistant Authors. Licensed under the
Apache License, Version 2.0.

Calendar selection is stored as canonical collection URLs in entry options.
"""

import logging
from collections.abc import Mapping
from functools import partial
from typing import Any

import probatio
from caldav.davclient import DAVClient
from caldav.lib.error import AuthorizationError, DAVError
from caldav.lib.http_sync import requests as caldav_requests
from homeassistant.config_entries import (
    ConfigEntry,
    ConfigFlow,
    ConfigFlowResult,
    OptionsFlow,
)
from homeassistant.const import CONF_PASSWORD, CONF_URL, CONF_USERNAME, CONF_VERIFY_SSL
from homeassistant.core import HomeAssistant, callback
from homeassistant.helpers import config_validation as cv

from .api import async_get_calendars, collection_url
from .const import CONF_CALENDARS, DOMAIN, SUPPORTED_COMPONENT, TIMEOUT

_LOGGER = logging.getLogger(__name__)

STEP_USER_DATA_SCHEMA = probatio.Schema(
    {
        probatio.Required(CONF_URL): str,
        probatio.Required(CONF_USERNAME): cv.string,
        probatio.Optional(CONF_PASSWORD, default=""): cv.string,
        probatio.Optional(CONF_VERIFY_SSL, default=True): cv.boolean,
    }
)


def _client(user_input: Mapping[str, Any]) -> DAVClient:
    """Build a CalDAV client from form or entry data."""
    return DAVClient(
        user_input[CONF_URL],
        username=user_input[CONF_USERNAME],
        password=user_input[CONF_PASSWORD],
        ssl_verify_cert=user_input[CONF_VERIFY_SSL],
        timeout=TIMEOUT,
    )


async def async_discovered_calendars(
    hass: HomeAssistant, client: DAVClient
) -> list[tuple[str, str]]:
    """Return `(canonical URL, display name)` for named VEVENT collections."""
    found = await async_get_calendars(hass, client, SUPPORTED_COMPONENT)
    calendars: list[tuple[str, str]] = []
    for calendar, name in found:
        if not name:
            continue
        url = collection_url(calendar)
        if url is None:
            _LOGGER.warning("Skipping calendar %s without a collection URL", name)
            continue
        calendars.append((url, name))
    return calendars


def _calendar_schema(choices: dict[str, str], default: list[str]) -> probatio.Schema:
    """Build the calendar multi-select schema."""
    return probatio.Schema(
        {
            probatio.Optional(CONF_CALENDARS, default=default): cv.multi_select(
                choices
            ),
        }
    )


class CalDavConfigFlow(ConfigFlow, domain=DOMAIN):
    """Handle a config flow for CalDAV Full."""

    VERSION = 1

    def __init__(self) -> None:
        """Initialize selection state."""
        self._user_input: dict[str, Any] | None = None
        self._discovered: dict[str, str] = {}
        self._pending_urls: list[str] = []

    @staticmethod
    @callback
    def async_get_options_flow(
        config_entry: ConfigEntry,
    ) -> CalDavOptionsFlow:
        """Return the options flow."""
        return CalDavOptionsFlow()

    async def async_step_user(
        self, user_input: dict[str, Any] | None = None
    ) -> ConfigFlowResult:
        """Handle the initial step."""
        errors: dict[str, str] = {}
        if user_input is not None:
            self._async_abort_entries_match(
                {
                    CONF_URL: user_input[CONF_URL],
                    CONF_USERNAME: user_input[CONF_USERNAME],
                }
            )
            error, discovered = await self._test_connection(user_input)
            if error:
                errors["base"] = error
            else:
                self._user_input = user_input
                self._discovered = dict(discovered)
                return await self.async_step_calendars()

        return self.async_show_form(
            step_id="user",
            data_schema=STEP_USER_DATA_SCHEMA,
            errors=errors,
        )

    async def async_step_calendars(
        self, user_input: dict[str, Any] | None = None
    ) -> ConfigFlowResult:
        """Select which discovered collections to consider."""
        assert self._user_input is not None
        if user_input is not None:
            self._pending_urls = _known_urls(
                user_input.get(CONF_CALENDARS, []), self._discovered
            )
            if self._pending_urls:
                return await self.async_step_confirm_activate()
            return await self.async_step_confirm_none()

        return self.async_show_form(
            step_id="calendars",
            data_schema=_calendar_schema(self._discovered, []),
        )

    async def async_step_confirm_none(
        self, user_input: dict[str, Any] | None = None
    ) -> ConfigFlowResult:
        """Confirm that no calendar will be exposed."""
        del user_input
        return self.async_show_menu(
            step_id="confirm_none",
            menu_options=["save_none", "calendars"],
        )

    async def async_step_confirm_activate(
        self, user_input: dict[str, Any] | None = None
    ) -> ConfigFlowResult:
        """Confirm that the pending calendars will become writable entities."""
        del user_input
        return self.async_show_menu(
            step_id="confirm_activate",
            menu_options=["save_selected", "calendars"],
            description_placeholders={
                "calendars": _calendar_label(self._pending_urls, self._discovered)
            },
        )

    async def async_step_save_none(
        self, user_input: dict[str, Any] | None = None
    ) -> ConfigFlowResult:
        """Store an empty allow-list after the no-calendar confirmation."""
        del user_input
        return self._create_user_entry([])

    async def async_step_save_selected(
        self, user_input: dict[str, Any] | None = None
    ) -> ConfigFlowResult:
        """Store the confirmed allow-list."""
        del user_input
        if not self._pending_urls:
            return await self.async_step_calendars()
        return self._create_user_entry(list(self._pending_urls))

    def _create_user_entry(self, selected: list[str]) -> ConfigFlowResult:
        """Create the entry without changing the stored options contract."""
        assert self._user_input is not None
        return self.async_create_entry(
            title=self._user_input[CONF_USERNAME],
            data=self._user_input,
            options={CONF_CALENDARS: selected},
        )

    async def _test_connection(
        self, user_input: dict[str, Any]
    ) -> tuple[str | None, list[tuple[str, str]]]:
        """Test the connection and return discovered calendars."""
        client = await self.hass.async_add_executor_job(partial(_client, user_input))
        try:
            await self.hass.async_add_executor_job(client.get_principal)
            discovered = await async_discovered_calendars(self.hass, client)
        except AuthorizationError as err:
            _LOGGER.warning("Authorization Error connecting to CalDAV server: %s", err)
            if err.reason == "Unauthorized":
                return "invalid_auth", []
            return "cannot_connect", []
        except caldav_requests.exceptions.Timeout as err:
            _LOGGER.warning("Timeout connecting to CalDAV server: %s", err)
            return "cannot_connect", []
        except caldav_requests.exceptions.ConnectionError as err:
            _LOGGER.warning("Connection Error connecting to CalDAV server: %s", err)
            return "cannot_connect", []
        except DAVError as err:
            _LOGGER.warning("CalDAV client error: %s", err)
            return "cannot_connect", []
        except Exception:
            _LOGGER.exception("Unexpected exception")
            return "unknown", []
        return None, discovered

    async def async_step_reauth(
        self, entry_data: Mapping[str, Any]
    ) -> ConfigFlowResult:
        """Perform reauth upon an API authentication error."""
        return await self.async_step_reauth_confirm()

    async def async_step_reauth_confirm(
        self, user_input: dict[str, str] | None = None
    ) -> ConfigFlowResult:
        """Confirm reauth dialog without changing calendar selection."""
        errors: dict[str, str] = {}
        reauth_entry = self._get_reauth_entry()
        if user_input is not None:
            updated = {**reauth_entry.data, **user_input}
            error, _discovered = await self._test_connection(updated)
            if error:
                errors["base"] = error
            else:
                return self.async_update_reload_and_abort(reauth_entry, data=updated)

        return self.async_show_form(
            description_placeholders={
                CONF_USERNAME: reauth_entry.data[CONF_USERNAME],
            },
            step_id="reauth_confirm",
            data_schema=probatio.Schema(
                {
                    probatio.Required(CONF_PASSWORD): str,
                }
            ),
            errors=errors,
        )


class CalDavOptionsFlow(OptionsFlow):
    """Change the selected calendar collections."""

    def __init__(self) -> None:
        """Initialize pending confirmation state."""
        self._pending_urls: list[str] = []
        self._choices: dict[str, str] = {}

    async def async_step_init(
        self, user_input: dict[str, Any] | None = None
    ) -> ConfigFlowResult:
        """Manage the calendar allow-list."""
        entry = self.config_entry
        current = list(entry.options.get(CONF_CALENDARS, []))
        if user_input is not None:
            self._pending_urls = _known_urls(
                user_input.get(CONF_CALENDARS, []), self._choices
            )
            if self._pending_urls:
                return await self.async_step_confirm_activate()
            return await self.async_step_confirm_none()

        errors: dict[str, str] = {}
        choices = {url: url for url in current}
        try:
            client = await self.hass.async_add_executor_job(
                partial(_client, entry.data)
            )
            await self.hass.async_add_executor_job(client.get_principal)
            discovered = await async_discovered_calendars(self.hass, client)
        except (
            AuthorizationError,
            caldav_requests.exceptions.Timeout,
            caldav_requests.exceptions.ConnectionError,
            DAVError,
        ) as err:
            _LOGGER.warning("Could not refresh calendar list: %s", err)
            errors["base"] = "cannot_connect"
        else:
            choices = dict(discovered)
            for url in current:
                choices.setdefault(url, url)

        self._choices = choices
        return self.async_show_form(
            step_id="init",
            data_schema=_calendar_schema(
                choices, [url for url in current if url in choices]
            ),
            errors=errors,
        )

    async def async_step_confirm_none(
        self, user_input: dict[str, Any] | None = None
    ) -> ConfigFlowResult:
        """Confirm clearing the allow-list."""
        del user_input
        return self.async_show_menu(
            step_id="confirm_none",
            menu_options=["save_none", "init"],
        )

    async def async_step_confirm_activate(
        self, user_input: dict[str, Any] | None = None
    ) -> ConfigFlowResult:
        """Confirm exposing the pending calendars."""
        del user_input
        return self.async_show_menu(
            step_id="confirm_activate",
            menu_options=["save_selected", "init"],
            description_placeholders={
                "calendars": _calendar_label(self._pending_urls, self._choices)
            },
        )

    async def async_step_save_none(
        self, user_input: dict[str, Any] | None = None
    ) -> ConfigFlowResult:
        """Replace the allow-list with an empty list."""
        del user_input
        return self.async_create_entry(data={CONF_CALENDARS: []})

    async def async_step_save_selected(
        self, user_input: dict[str, Any] | None = None
    ) -> ConfigFlowResult:
        """Replace the allow-list with the confirmed collections."""
        del user_input
        if not self._pending_urls:
            return await self.async_step_init()
        return self.async_create_entry(data={CONF_CALENDARS: list(self._pending_urls)})


def _known_urls(submitted: list[str], choices: dict[str, str]) -> list[str]:
    """Keep submitted URLs that were offered on the form."""
    return [url for url in submitted if url in choices]


def _calendar_label(urls: list[str], names: dict[str, str]) -> str:
    """Return display names for a confirmation sentence."""
    labels: list[str] = []
    for url in urls:
        name = names.get(url, "Selected calendar")
        if name.startswith(("http://", "https://")):
            name = "Selected calendar"
        labels.append(name)
    return ", ".join(labels)
