"""Base entity for the Pod Home integration."""
from __future__ import annotations

import logging
from typing import TYPE_CHECKING

from homeassistant.core import HomeAssistant, callback
from homeassistant.exceptions import HomeAssistantError
from homeassistant.helpers import entity_registry as er
from homeassistant.helpers.device_registry import DeviceInfo
from homeassistant.helpers.entity_platform import AddEntitiesCallback
from homeassistant.helpers.update_coordinator import CoordinatorEntity

from .const import ATTRIBUTION, DAY_OF_WEEK_OPTIONS, DOMAIN, MANUFACTURER, SCHEDULE_MODE_SMART_CHARGING
from .coordinator import PodHomeCharge, PodHomeCharger, PodHomeDataUpdateCoordinator, PodHomeVehicle
from .helpers import humanize_model_style, is_single_rate_tariff, schedule_mode, select_last_charge

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
) -> None:
    """Create `entity_classes` for every ppid currently known, and keep creating them for any
    new ppid that appears in a later coordinator update, without requiring an HA restart."""
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


# Entities that only make sense in Smart Charging mode. (platform_domain, unique_id_suffix,
# scope) - required_mode is always Smart Charging here, so it's not stored per-entry; add a
# required_mode column if a Basic-only entity is ever added.
_MODE_GATED_ENTITIES: list[tuple[str, str, str]] = [
    ("time", "_ready_by", "vehicle"),
    ("number", "_target_charge", "vehicle"),
    ("sensor", "_expected_charge", "vehicle"),
    ("sensor", "_electricity_rate", "charger"),
]


@callback
def async_sync_mode_gated_entities(
    hass: HomeAssistant, coordinator: PodHomeDataUpdateCoordinator
) -> None:
    """Enable/disable (via the entity registry, not deletion or `available`) each entity in
    _MODE_GATED_ENTITIES to match its charger's/vehicle's current Charging Mode. Entities stay
    registered either way. Called once after initial platform setup and again on every
    coordinator update, since mode can change any time from the app.

    Vehicle-scoped entities are resolved using the FIRST charger each vehicle is linked to in
    coordinator.data iteration order - must match PodHomeVehicleEntity._charger_for_vehicle()'s
    own resolution, or mode-gating could disagree with which charger's mode the vehicle-scoped
    entity actually reads."""
    registry = er.async_get(hass)
    # None = this vehicle's first-linked charger's mode is unresolved this poll - recorded
    # separately from "skip" so a vehicle's gating always tracks the SAME charger
    # _charger_for_vehicle() would resolve to (first match, unconditionally), never falling
    # through to a later charger just because the first one's mode happened to be unresolved.
    vehicle_wants_enabled: dict[str, bool | None] = {}
    for ppid, charger in coordinator.data.items():
        mode = schedule_mode(charger.delegated_control_status)
        if mode is None:
            # Unrecognized/unknown status - leave this charger's own entities alone, but still
            # fall through below to record it for its vehicle if it's the first match.
            wants_enabled = None
        else:
            wants_enabled = mode == SCHEDULE_MODE_SMART_CHARGING
            for platform_domain, suffix, scope in _MODE_GATED_ENTITIES:
                if scope == "charger":
                    _async_apply_disabled_state(registry, platform_domain, f"{ppid}{suffix}", wants_enabled)
        vehicle_id = charger.vehicle.id if charger.vehicle else None
        if vehicle_id and vehicle_id not in vehicle_wants_enabled:
            vehicle_wants_enabled[vehicle_id] = wants_enabled

    for vehicle_id, wants_enabled in vehicle_wants_enabled.items():
        if wants_enabled is None:
            continue  # first-linked charger's mode unresolved - leave existing state alone
        for platform_domain, suffix, scope in _MODE_GATED_ENTITIES:
            if scope != "charger":
                _async_apply_disabled_state(
                    registry, platform_domain, f"{vehicle_id}{suffix}", wants_enabled
                )


def _async_apply_disabled_state(
    registry: er.EntityRegistry, platform_domain: str, unique_id_body: str, wants_enabled: bool
) -> None:
    unique_id = f"{DOMAIN}_{unique_id_body}"
    entity_id = registry.async_get_entity_id(platform_domain, DOMAIN, unique_id)
    if entity_id is None:
        # Expected in the common case (e.g. no vehicle linked yet); also what a stale manifest
        # suffix looks like, so kept at debug rather than silent.
        _LOGGER.debug(
            "No registered entity for %s.%s (unique_id=%s) - skipping mode-gate reconciliation",
            platform_domain,
            DOMAIN,
            unique_id,
        )
        return
    entry = registry.entities.get(entity_id)
    if entry is None:
        return
    is_disabled_by_us = entry.disabled_by == er.RegistryEntryDisabler.INTEGRATION
    if wants_enabled and is_disabled_by_us:
        registry.async_update_entity(entity_id, disabled_by=None)
    elif not wants_enabled and entry.disabled_by is None:
        # Only disable if nothing else already disabled it.
        registry.async_update_entity(entity_id, disabled_by=er.RegistryEntryDisabler.INTEGRATION)


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
    """Enable/disable (same mechanism as async_sync_mode_gated_entities) each entity in
    _TARIFF_GATED_ENTITIES to match its charger's tariff shape. A charger whose tariff isn't
    known yet is left alone rather than guessed either way."""
    registry = er.async_get(hass)
    for ppid, charger in coordinator.data.items():
        is_single_rate = is_single_rate_tariff(charger.tariff_windows)
        if is_single_rate is None:
            continue
        wants_enabled = not is_single_rate
        for platform_domain, suffix in _TARIFF_GATED_ENTITIES:
            _async_apply_disabled_state(registry, platform_domain, f"{ppid}{suffix}", wants_enabled)


# Entities gated on whether THIS charger's hardware actually supports the feature at all - not
# Charging Mode or tariff shape, a per-model capability. Only Remote Lock so far.
_SUPPORT_GATED_ENTITIES: list[tuple[str, str]] = [
    ("lock", "_remote_lock"),
]


@callback
def async_sync_support_gated_entities(
    hass: HomeAssistant, coordinator: PodHomeDataUpdateCoordinator
) -> None:
    """Enable/disable (same mechanism as async_sync_mode_gated_entities) each entity in
    _SUPPORT_GATED_ENTITIES, based on whether its charger has ever reported a real value for the
    underlying capability. Unlike mode/tariff gating, "unresolved" (never seen a real value) is
    treated as "disable", not "leave alone" - `remote_lock_off_mode` has only ever been observed
    `None` on a charger model confirmed NOT to support Remote Lock at all (a Solo 3 - see
    DECISIONS.md), never confirmed null-but-supported on a Solo 3S. Until an account with 3S
    hardware can confirm that distinction, staying disabled whenever a real bool has never been
    seen is the safer default - hiding it until the first real toggle beats cluttering every
    unsupported install with a permanently `unknown` entity. Once a real value IS seen, it's
    cached on the coordinator (see coordinator.py) and stays enabled from then on, even across a
    later poll where the fetch is skipped (staleness caching, not re-fetched every poll)."""
    registry = er.async_get(hass)
    for ppid, charger in coordinator.data.items():
        wants_enabled = charger.remote_lock_off_mode is not None
        for platform_domain, suffix in _SUPPORT_GATED_ENTITIES:
            _async_apply_disabled_state(registry, platform_domain, f"{ppid}{suffix}", wants_enabled)
