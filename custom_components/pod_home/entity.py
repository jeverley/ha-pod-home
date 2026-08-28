"""Base entity for the Pod Home integration."""
from __future__ import annotations

from typing import TYPE_CHECKING

from homeassistant.helpers.device_registry import DeviceInfo
from homeassistant.helpers.entity_platform import AddEntitiesCallback
from homeassistant.helpers.update_coordinator import CoordinatorEntity

from .const import ATTRIBUTION, DOMAIN, MANUFACTURER
from .coordinator import PodHomeCharger, PodHomeDataUpdateCoordinator

if TYPE_CHECKING:
    from . import PodHomeConfigEntry


class PodHomeEntity(CoordinatorEntity[PodHomeDataUpdateCoordinator]):
    """Common base for all Pod Home entities - one charger (by ppid) per entity."""

    _attr_has_entity_name = True
    _attr_attribution = ATTRIBUTION

    def __init__(self, coordinator: PodHomeDataUpdateCoordinator, ppid: str) -> None:
        super().__init__(coordinator)
        self.ppid = ppid

    @property
    def charger(self) -> PodHomeCharger | None:
        return self.coordinator.data.get(self.ppid)

    @property
    def available(self) -> bool:
        return super().available and self.charger is not None

    @property
    def device_info(self) -> DeviceInfo:
        charger = self.charger
        model = charger.model_style.upper() if charger and charger.model_style else None
        return DeviceInfo(
            identifiers={(DOMAIN, self.ppid)},
            name=self.ppid,
            manufacturer=MANUFACTURER,
            model=model,
        )


def async_setup_dynamic_chargers(
    entry: "PodHomeConfigEntry",
    coordinator: PodHomeDataUpdateCoordinator,
    async_add_entities: AddEntitiesCallback,
    entity_classes: list[type[PodHomeEntity]],
) -> None:
    """Create `entity_classes` for every ppid currently known, and keep creating them for any
    new ppid that shows up in a later coordinator update (e.g. a charger added to the account)
    - without requiring an HA restart/reload. Shared by sensor.py and binary_sensor.py so the
    diff-and-add logic exists in exactly one place.

    Note: this can only be code-reviewed, not end-to-end verified, against a single-charger
    account - there's nothing to add a second device from to prove it live.
    """
    known_ppids: set[str] = set()

    def _async_add_new_chargers() -> None:
        new_ppids = set(coordinator.data) - known_ppids
        if not new_ppids:
            return
        known_ppids.update(new_ppids)
        async_add_entities(
            [cls(coordinator, ppid) for ppid in new_ppids for cls in entity_classes]
        )

    _async_add_new_chargers()
    entry.async_on_unload(coordinator.async_add_listener(_async_add_new_chargers))
