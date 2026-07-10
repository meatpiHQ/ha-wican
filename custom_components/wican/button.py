"""Button platform for MeatPi devices.

Control buttons exist only on devices whose capability probe found the V6
HTTP API; a description can additionally require a specific firmware
component. Buttons are added dynamically when the probe completes, so a
device that is offline at startup — or gets OTA-updated to V6 later — grows
its buttons without a reload (same pattern as the dynamic PID sensors).
"""

from __future__ import annotations

from dataclasses import dataclass
import logging
from typing import TYPE_CHECKING, Any

from homeassistant.components.button import ButtonDeviceClass, ButtonEntity, ButtonEntityDescription
from homeassistant.const import EntityCategory
from homeassistant.core import HomeAssistant, callback
from homeassistant.exceptions import HomeAssistantError
from homeassistant.helpers.dispatcher import async_dispatcher_connect

from .catalog import async_refresh_device_catalog
from .const import DOMAIN, SIGNAL_CAPABILITIES_UPDATED
from .entity import WiCANEntity
from .exceptions import MeatPiApiConnectionError, MeatPiApiError
from .param_loader import async_force_params_refresh

if TYPE_CHECKING:
    from collections.abc import Callable, Coroutine

    from homeassistant.helpers.entity_platform import AddEntitiesCallback

    from . import WiCANConfigEntry
    from .api import MeatPiApiClient

_LOGGER = logging.getLogger(__name__)
PARALLEL_UPDATES = 0


@dataclass(frozen=True, kw_only=True)
class MeatPiButtonEntityDescription(ButtonEntityDescription):
    """Describes a MeatPi control button and its capability requirements."""

    press_fn: Callable[[MeatPiApiClient], Coroutine[Any, Any, None]]
    required_component: str | None = None
    """Firmware component that must be present; None means any device with
    the control HTTP API supports this button."""


BUTTON_DESCRIPTIONS: tuple[MeatPiButtonEntityDescription, ...] = (
    MeatPiButtonEntityDescription(
        key="restart",
        translation_key="restart",
        device_class=ButtonDeviceClass.RESTART,
        entity_category=EntityCategory.CONFIG,
        press_fn=lambda api: api.async_restart(),
    ),
    MeatPiButtonEntityDescription(
        key="sync_time",
        translation_key="sync_time",
        entity_category=EntityCategory.CONFIG,
        required_component="rtc_manager",
        press_fn=lambda api: api.async_sync_time(),
    ),
)


REFRESH_DEFINITIONS_DESCRIPTION = ButtonEntityDescription(
    key="refresh_definitions",
    translation_key="refresh_definitions",
    entity_category=EntityCategory.CONFIG,
)


async def async_setup_entry(
    hass: HomeAssistant,
    config_entry: WiCANConfigEntry,
    async_add_entities: AddEntitiesCallback,
) -> None:
    """Set up MeatPi control buttons for capabilities as they are discovered."""
    # The refresh-definitions button exists on every device (it talks
    # to GitHub, not to the device, so no capability gating applies).
    # Guarded like everything else against unload races.
    if getattr(config_entry, "runtime_data", None) is not None:
        async_add_entities(
            [
                WiCANRefreshDefinitionsButton(
                    config_entry, REFRESH_DEFINITIONS_DESCRIPTION,
                ),
            ],
        )

    added_keys: set[str] = set()

    @callback
    def _async_add_supported_buttons() -> None:
        # The platform may outlive the runtime data during unload races.
        runtime = getattr(config_entry, "runtime_data", None)
        if runtime is None:
            return
        capabilities = runtime.capabilities
        if not capabilities.has_http_api:
            return
        new_entities: list[WiCANButtonEntity] = []
        for description in BUTTON_DESCRIPTIONS:
            if description.key in added_keys:
                continue
            if description.required_component and not capabilities.has_component(
                description.required_component,
            ):
                continue
            new_entities.append(WiCANButtonEntity(config_entry, description))
            added_keys.add(description.key)
        if new_entities:
            _LOGGER.debug(
                "Adding %d control button(s) for %s",
                len(new_entities),
                config_entry.title,
            )
            async_add_entities(new_entities)

    # Capabilities may already be known (probe finished before the platform
    # loaded, e.g. after a reload) — add immediately, then follow updates.
    _async_add_supported_buttons()
    config_entry.async_on_unload(
        async_dispatcher_connect(
            hass,
            f"{SIGNAL_CAPABILITIES_UPDATED}_{config_entry.entry_id}",
            _async_add_supported_buttons,
        ),
    )


class WiCANRefreshDefinitionsButton(WiCANEntity, ButtonEntity):
    """Refresh PID parameter definitions and the device catalog from GitHub.

    The one-click support flow: "your parameter was merged — press
    Refresh integration definitions". Fetches the latest params.json
    (bypassing the setup dedupe window) and the device catalog, then
    reloads the config entry so refreshed definitions apply to existing
    entities. Named to make clear it refreshes downloaded definition
    data — it does not synchronize or touch the device itself.
    """

    __slots__ = ()

    @property
    def available(self) -> bool:
        """The refresh talks to GitHub, not the device: always available."""
        return True

    @callback
    def _async_handle_event(self, webhook_id: str, data: dict[str, str]) -> None:
        """Handle webhook event (this button carries no push state)."""

    async def async_press(self) -> None:
        """Fetch fresh definitions and reload the entry to apply them."""
        params_result = await async_force_params_refresh(self.hass)
        catalog_updated = await async_refresh_device_catalog(self.hass)
        if params_result is None and not catalog_updated:
            raise HomeAssistantError(
                translation_domain=DOMAIN,
                translation_key="definitions_refresh_failed",
            )
        _LOGGER.info(
            "Definitions refresh: parameters %s, catalog %s; reloading %s",
            {True: "updated", False: "up to date", None: "fetch failed"}[
                params_result
            ],
            "updated" if catalog_updated else "unchanged",
            self.config_entry.title,
        )
        # Reload out-of-band: awaiting our own entry's reload from inside
        # one of its entities would deadlock on the unload.
        self.hass.async_create_task(
            self.hass.config_entries.async_reload(self.config_entry.entry_id),
        )


class WiCANButtonEntity(WiCANEntity, ButtonEntity):
    """A control button backed by the device HTTP API."""

    __slots__ = ()

    entity_description: MeatPiButtonEntityDescription

    @callback
    def _async_handle_event(self, webhook_id: str, data: dict[str, str]) -> None:
        """Handle webhook event (buttons carry no push state)."""

    async def async_press(self) -> None:
        """Send the button's command to the device."""
        api = self.config_entry.runtime_data.api
        if api is None:
            raise HomeAssistantError(
                translation_domain=DOMAIN,
                translation_key="api_not_available",
            )
        try:
            await self.entity_description.press_fn(api)
        except MeatPiApiConnectionError as err:
            # The bound endpoint is unreachable (e.g. the car left home).
            # Re-probe so the endpoint can rebind — typically to the
            # device's VPN tunnel address — for the next press.
            from . import _async_request_capability_probe  # noqa: PLC0415 — avoid import cycle at module load

            self.hass.async_create_task(
                _async_request_capability_probe(self.hass, self.config_entry),
            )
            raise HomeAssistantError(
                translation_domain=DOMAIN,
                translation_key="api_command_failed",
                translation_placeholders={"error": str(err)},
            ) from err
        except MeatPiApiError as err:
            raise HomeAssistantError(
                translation_domain=DOMAIN,
                translation_key="api_command_failed",
                translation_placeholders={"error": str(err)},
            ) from err
