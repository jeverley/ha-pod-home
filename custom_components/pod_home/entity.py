"""Base entity for the Pod Home integration."""
from __future__ import annotations

from collections.abc import Callable
from typing import TYPE_CHECKING, Generic, NoReturn, TypeVar

from homeassistant.const import UnitOfLength
from homeassistant.exceptions import HomeAssistantError
from homeassistant.helpers.device_registry import DeviceInfo
from homeassistant.helpers.entity_platform import AddEntitiesCallback
from homeassistant.helpers.update_coordinator import CoordinatorEntity

from .const import ATTRIBUTION, DOMAIN, MANUFACTURER
from .coordinator import PodHomeCharge, PodHomeCharger, PodHomeDataUpdateCoordinator, PodHomeVehicle
from .helpers import (
    boostable,
    humanize_model_style,
    is_momentarily_unplugged,
    select_last_charge,
    smart_mode_available,
)
from .podpoint_mobile_api import PodHomeAuthError

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
    def last_charge(self) -> PodHomeCharge | None:
        """The active session if one's in progress, else the last finished one. See
        select_last_charge() (helpers.py)."""
        charger = self.charger
        if not charger:
            return None
        return select_last_charge(charger.current_charge, charger.latest_charge)

    @property
    def available(self) -> bool:
        return self._available_charger is not None

    @property
    def _available_charger(self) -> PodHomeCharger | None:
        """self.charger, narrowed non-None whenever available holds."""
        charger = self.charger
        return charger if super().available and charger is not None else None

    @property
    def _cable_connected(self) -> bool:
        """Shared by every entity/action that only makes sense with a cable plugged in (the
        boost buttons, Boost duration) - only meaningful once `available` above has already
        confirmed a charger exists."""
        charger = self.charger
        return not is_momentarily_unplugged(charger.charging_state if charger else None)

    @property
    def _boostable(self) -> bool:
        """Shared by both boost-start buttons (button.py) - see helpers.boostable()."""
        charger = self._available_charger
        return charger is not None and boostable(charger.charging_state, charger.always_on_active)

    @property
    def device_info(self) -> DeviceInfo:
        charger = self.charger
        model = humanize_model_style(charger.model_style) if charger else None
        return DeviceInfo(
            identifiers={(DOMAIN, self.ppid)},
            name=model or self.ppid,
            manufacturer=MANUFACTURER,
            model=model,
            # ppid ("PSL number") is Pod Point's consumer-facing unit identifier, not the
            # internal serial (see firmware.serial_number, surfaced on Firmware Version instead).
            serial_number=self.ppid,
        )


class PodHomeVehicleEntity(CoordinatorEntity[PodHomeDataUpdateCoordinator]):
    """Common base for vehicle entities - keyed by vehicle_id, not by the charger it's currently
    linked to. Standalone device, not via_device-linked to a charger.

    The linked charger is re-derived from live coordinator data on every access.
    """

    _attr_has_entity_name = True
    _attr_attribution = ATTRIBUTION

    def __init__(self, coordinator: PodHomeDataUpdateCoordinator, vehicle_id: str) -> None:
        super().__init__(coordinator)
        self.vehicle_id = vehicle_id

    def _charger_for_vehicle(self) -> PodHomeCharger | None:
        for charger in self.coordinator.data.values():
            if charger.vehicle and charger.vehicle.id == self.vehicle_id:
                return charger
        return None

    @property
    def vehicle(self) -> PodHomeVehicle | None:
        charger = self._charger_for_vehicle()
        return charger.vehicle if charger else None

    @property
    def ppid(self) -> str | None:
        """ppid of whichever charger this vehicle is currently linked to, re-derived live.
        Needed for the intents write endpoint, which is scoped by ppid."""
        charger = self._charger_for_vehicle()
        return charger.ppid if charger else None

    @property
    def available(self) -> bool:
        return super().available and self.vehicle is not None

    @property
    def _smart_mode_available(self) -> bool:
        """Shared by every Smart-Charging-gated vehicle entity (Ready By, Target Charge,
        Expected Charge) - resolves the linked charger once here, and is only meaningful once
        `available` above has already confirmed a linked vehicle/charger exists."""
        charger = self._charger_for_vehicle()
        return charger is not None and smart_mode_available(charger.delegated_control_status)

    @property
    def _suggested_distance_unit(self) -> str | None:
        """Shared by every km-native distance sensor (Estimated range, Odometer) - switches the
        default display to miles per the account's preferred distance unit."""
        return UnitOfLength.MILES if self.coordinator.unit_of_distance == "mi" else None

    @property
    def device_info(self) -> DeviceInfo:
        vehicle = self.vehicle
        name = (vehicle.display_name if vehicle else None) or "Vehicle"
        return DeviceInfo(
            identifiers={(DOMAIN, self.vehicle_id)},
            name=name,
            manufacturer=vehicle.brand if vehicle else None,
            model=vehicle.model if vehicle else None,
        )


class PodHomeAccountEntity(CoordinatorEntity[PodHomeDataUpdateCoordinator]):
    """Common base for account-level entities - not tied to any specific charger or vehicle
    (e.g. the rewards balance). Grouped under a "Pod Point" device (one per config entry)."""

    _attr_has_entity_name = True
    _attr_attribution = ATTRIBUTION

    @property
    def config_entry(self) -> "PodHomeConfigEntry":
        # Always constructed with a real config_entry (coordinator.py) - HA's own typing allows
        # None here since a coordinator can in principle exist without one, this one never does.
        assert self.coordinator.config_entry is not None
        return self.coordinator.config_entry

    @property
    def device_info(self) -> DeviceInfo:
        return DeviceInfo(
            identifiers={(DOMAIN, self.config_entry.entry_id)},
            name="Pod Point",
            manufacturer=MANUFACTURER,
        )


_OptimisticT = TypeVar("_OptimisticT")


class PodHomeOptimisticWriteMixin(
    CoordinatorEntity[PodHomeDataUpdateCoordinator], Generic[_OptimisticT]
):
    """Masks the read-your-own-write race: the just-written value is shown until the second
    coordinator update after the write."""

    _optimistic_value: _OptimisticT | None = None
    _optimistic_polls_remaining: int = 0

    def _set_optimistic_value(self, value: _OptimisticT) -> None:
        self._optimistic_value = value
        self._optimistic_polls_remaining = 2

    def _read_optimistic_value(self) -> _OptimisticT | None:
        return self._optimistic_value

    def _handle_coordinator_update(self) -> None:
        if self._optimistic_polls_remaining > 0:
            self._optimistic_polls_remaining -= 1
            if self._optimistic_polls_remaining == 0:
                self._optimistic_value = None
        super()._handle_coordinator_update()


async def async_handle_write_auth_error(
    coordinator: PodHomeDataUpdateCoordinator, exc: PodHomeAuthError
) -> NoReturn:
    """Shared by every write entity's async_press/async_set_*/async_lock/async_unlock -
    requests a coordinator refresh so a genuinely-expired auth session triggers the usual reauth
    flow (coordinator.py's own PodHomeAuthError handling), then fails this write clearly instead
    of leaving a raw PodHomeAuthError as the visible error."""
    await coordinator.async_request_refresh()
    raise HomeAssistantError(f"Pod Point rejected the request: {exc}") from exc


def async_setup_dynamic_chargers(
    entry: "PodHomeConfigEntry",
    coordinator: PodHomeDataUpdateCoordinator,
    async_add_entities: AddEntitiesCallback,
    entity_classes: list[type[PodHomeEntity]],
    predicate: Callable[[PodHomeCharger], bool] | None = None,
) -> None:
    """Create `entity_classes` for every ppid currently known, and keep creating them for any
    new ppid that appears in a later coordinator update, without requiring an HA restart.

    `predicate`, if given, additionally gates WHICH known chargers get these entities - e.g. only
    ones with confirmed hardware support for a capability (see lock.py's Remote Lock). A ppid
    that doesn't pass yet is never permanently skipped - re-evaluated every coordinator update
    via the same `known_ppids` dedup, so it gets the entity the moment a later poll passes it."""
    known_ppids: set[str] = set()

    def _async_add_new_chargers() -> None:
        eligible_ppids = set(coordinator.data)
        if predicate is not None:
            eligible_ppids = {ppid for ppid in eligible_ppids if predicate(coordinator.data[ppid])}
        new_ppids = eligible_ppids - known_ppids
        if not new_ppids:
            return
        known_ppids.update(new_ppids)
        async_add_entities(
            [cls(coordinator, ppid) for ppid in new_ppids for cls in entity_classes]
        )

    _async_add_new_chargers()
    entry.async_on_unload(coordinator.async_add_listener(_async_add_new_chargers))


def async_setup_dynamic_vehicles(
    entry: "PodHomeConfigEntry",
    coordinator: PodHomeDataUpdateCoordinator,
    async_add_entities: AddEntitiesCallback,
    entity_classes: list[type[PodHomeVehicleEntity]],
) -> None:
    """Same pattern as async_setup_dynamic_chargers. Keyed by vehicle_id only, since a vehicle's
    linked charger can change (see PodHomeVehicleEntity)."""
    known_vehicle_ids: set[str] = set()

    def _async_add_new_vehicles() -> None:
        current_ids = {
            charger.vehicle.id for charger in coordinator.data.values() if charger.vehicle
        }
        new_ids = current_ids - known_vehicle_ids
        if not new_ids:
            return
        known_vehicle_ids.update(new_ids)
        async_add_entities(
            [
                cls(coordinator, vehicle_id)
                for vehicle_id in new_ids
                for cls in entity_classes
            ]
        )

    _async_add_new_vehicles()
    entry.async_on_unload(coordinator.async_add_listener(_async_add_new_vehicles))


