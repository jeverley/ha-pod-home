"""Base entity for the Pod Home integration."""
from __future__ import annotations

import logging
from collections.abc import Callable
from typing import TYPE_CHECKING

from homeassistant.core import HomeAssistant, callback
from homeassistant.exceptions import HomeAssistantError
from homeassistant.helpers import entity_registry as er
from homeassistant.helpers.device_registry import DeviceInfo
from homeassistant.helpers.entity_platform import AddEntitiesCallback
from homeassistant.helpers.update_coordinator import CoordinatorEntity

from .const import ATTRIBUTION, DAY_OF_WEEK_OPTIONS, DOMAIN, MANUFACTURER
from .coordinator import PodHomeCharge, PodHomeCharger, PodHomeDataUpdateCoordinator, PodHomeVehicle
from .helpers import humanize_model_style, is_single_rate_tariff, select_last_charge

if TYPE_CHECKING:
    from . import PodHomeConfigEntry

_LOGGER = logging.getLogger(__name__)


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
        return super().available and self.charger is not None

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

    The linked charger is re-derived from live coordinator data on every access rather than
    fixed at construction, since a vehicle can move between chargers on a multi-charger account.
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
    (e.g. the rewards balance). Grouped under a "Pod Point" device (one per config entry) rather
    than going device-less."""

    _attr_has_entity_name = True
    _attr_attribution = ATTRIBUTION

    @property
    def device_info(self) -> DeviceInfo:
        return DeviceInfo(
            identifiers={(DOMAIN, self.coordinator.config_entry.entry_id)},
            name="Pod Point",
            manufacturer=MANUFACTURER,
        )


class PodHomeVehicleIntentsWriteMixin:
    """Write helper for the Ready By time entity (time.py). NOT YET TESTED against a real
    account.

    PUT .../intents requires both chargeByTime and chargeKWh on every entry, fanned identically
    across all 7 days (see DAY_OF_WEEK_OPTIONS in const.py)."""

    async def _async_write_intents(self, *, charge_by_time: str, charge_kwh: float) -> None:
        vehicle = self.vehicle  # type: ignore[attr-defined]
        ppid = self.ppid  # type: ignore[attr-defined]
        if not vehicle or not ppid:
            raise HomeAssistantError("No linked vehicle to write Ready By for")
        intent_details = [
            {"dayOfWeek": day, "chargeByTime": charge_by_time, "chargeKWh": round(charge_kwh, 2)}
            for day in DAY_OF_WEEK_OPTIONS
        ]
        await self.coordinator.api.async_set_vehicle_intents(  # type: ignore[attr-defined]
            ppid, vehicle.id, intent_details
        )
        await self.coordinator.async_request_refresh()  # type: ignore[attr-defined]


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
    ones that have confirmed hardware support for a capability (see lock.py's Remote Lock, the
    only current user). A ppid that doesn't pass yet is simply never given the entity this poll,
    not permanently skipped - `_async_add_new_chargers` re-evaluates every coordinator update via
    the same `known_ppids` dedup as the no-predicate case, so a ppid that starts failing the
    predicate (a transient fetch hiccup on its first poll, say) still gets the entity the moment
    a later poll passes it. This is deliberately NOT the same mechanism as entity-registry
    disable/enable (_async_apply_disabled_state below) - a capability that's permanently absent
    should mean the entity never exists at all, not exist-but-disabled forever; see DECISIONS.md
    for why Remote Lock moved off registry-gating to this instead."""
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
    """Same pattern as async_setup_dynamic_chargers, keyed purely by vehicle_id rather than
    (ppid, vehicle_id) - a vehicle's associated charger can change (see PodHomeVehicleEntity),
    and keying by ppid too would create a second, duplicate set of entities whenever it does."""
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


def _async_apply_disabled_state(
    coordinator: PodHomeDataUpdateCoordinator,
    registry: er.EntityRegistry,
    platform_domain: str,
    unique_id_body: str,
    wants_enabled: bool,
) -> None:
    unique_id = f"{DOMAIN}_{unique_id_body}"
    entity_id = registry.async_get_entity_id(platform_domain, DOMAIN, unique_id)
    if entity_id is None:
        # Expected in the common case (e.g. no vehicle linked yet); also what a stale manifest
        # suffix looks like, so kept at debug rather than silent.
        _LOGGER.debug(
            "No registered entity for %s.%s (unique_id=%s) - skipping gate reconciliation",
            platform_domain,
            DOMAIN,
            unique_id,
        )
        return
    entry = registry.entities.get(entity_id)
    if entry is None:
        return

    # If the registry's current enabled/disabled state no longer matches what WE last wrote for
    # this entity_id, something else changed it since - a user's own manual toggle in the UI,
    # since disabled_by=None looks identical whether it's "still what we set" or "the user just
    # re-enabled it". Defer to that rather than fighting it: skip silently, and deliberately
    # don't update our tracking, so we keep deferring on every later poll too, not just this one.
    last_applied = coordinator.entity_gate_applied_state.get(entity_id)
    if last_applied is not None:
        expected_disabled_by = None if last_applied else er.RegistryEntryDisabler.INTEGRATION
        if entry.disabled_by != expected_disabled_by:
            return

    is_disabled_by_us = entry.disabled_by == er.RegistryEntryDisabler.INTEGRATION
    if wants_enabled and is_disabled_by_us:
        registry.async_update_entity(entity_id, disabled_by=None)
        coordinator.entity_gate_applied_state[entity_id] = True
    elif not wants_enabled and entry.disabled_by is None:
        # Only disable if nothing else already disabled it.
        registry.async_update_entity(entity_id, disabled_by=er.RegistryEntryDisabler.INTEGRATION)
        coordinator.entity_gate_applied_state[entity_id] = False


# Entities gated on the account's tariff shape, not Charging Mode - see is_single_rate_tariff()
# (helpers.py). (platform_domain, unique_id_suffix) - always charger-scoped so far, no scope
# column needed yet.
_TARIFF_GATED_ENTITIES: list[tuple[str, str]] = [
    ("select", "_charging_strategy"),
]


@callback
def async_sync_tariff_gated_entities(
    hass: HomeAssistant, coordinator: PodHomeDataUpdateCoordinator
) -> None:
    """Enable/disable (via the entity registry, not `available` - see DECISIONS.md for why
    tariff-shape gating stays on this mechanism while Charging-Mode gating moved off it) each
    entity in _TARIFF_GATED_ENTITIES to match its charger's tariff shape. A charger whose tariff
    isn't known yet is left alone rather than guessed either way."""
    registry = er.async_get(hass)
    for ppid, charger in coordinator.data.items():
        is_single_rate = is_single_rate_tariff(charger.tariff_windows)
        if is_single_rate is None:
            continue
        wants_enabled = not is_single_rate
        for platform_domain, suffix in _TARIFF_GATED_ENTITIES:
            _async_apply_disabled_state(
                coordinator, registry, platform_domain, f"{ppid}{suffix}", wants_enabled
            )
