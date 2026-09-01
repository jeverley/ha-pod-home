"""Select platform for pod_home - the Charge Priority control."""
from __future__ import annotations

from typing import TYPE_CHECKING

from homeassistant.components.select import SelectEntity
from homeassistant.core import HomeAssistant
from homeassistant.exceptions import HomeAssistantError
from homeassistant.helpers.entity import EntityCategory
from homeassistant.helpers.entity_platform import AddEntitiesCallback

from .const import CHARGE_PRIORITY_OPTIONS, DOMAIN
from .entity import PodHomeEntity, async_setup_dynamic_chargers
from .helpers import charging_priority_label, max_price_for_charging_priority

if TYPE_CHECKING:
    from . import PodHomeConfigEntry

PARALLEL_UPDATES = 0


async def async_setup_entry(
    hass: HomeAssistant, entry: PodHomeConfigEntry, async_add_entities: AddEntitiesCallback
) -> None:
    async_setup_dynamic_chargers(
        entry,
        entry.runtime_data,
        async_add_entities,
        [PodHomeChargingStrategySelect],
    )


class PodHomeChargingStrategySelect(PodHomeEntity, SelectEntity):
    """Settable Charge Priority - the cost-vs-completion preference, on the charger device since
    preferences are charger-scoped. NOT mode-gated. Tariff-gated: disabled via the entity
    registry on a single-rate tariff (see async_sync_tariff_gated_entities() in entity.py).
    current_option degrades to unknown rather than crashing if shown before gating applies.

    Read and write both go through maxPrice, not chargingStrategy - see
    charging_priority_label()/max_price_for_charging_priority() in helpers.py."""

    _attr_translation_key = "charging_strategy"
    _attr_name = "Charge priority"
    _attr_options = CHARGE_PRIORITY_OPTIONS
    _attr_entity_category = EntityCategory.CONFIG
    _attr_icon = "mdi:sort-variant"

    @property
    def unique_id(self) -> str:
        return f"{DOMAIN}_{self.ppid}_charging_strategy"

    @property
    def current_option(self) -> str | None:
        charger = self.charger
        if not charger:
            return None
        return charging_priority_label(charger.max_price, charger.tariff_windows)

    async def async_select_option(self, option: str) -> None:
        charger = self.charger
        if not charger:
            raise HomeAssistantError("No charger to set Charge Priority for")
        max_price = max_price_for_charging_priority(option, charger.tariff_windows)
        if max_price is None:
            raise HomeAssistantError(
                f"Couldn't determine a maxPrice to write for Charge Priority option {option!r} "
                "- either it's unrecognized, or this charger's tariff data isn't known yet"
            )
        await self.coordinator.api.async_set_charge_priority_max_price(self.ppid, max_price)
        await self.coordinator.async_request_refresh()
