"""Refresh, the start of an AI learning run, and the delete of the targets
the FP2 tracks, in the room and in each detection zone."""

from __future__ import annotations

from typing import TYPE_CHECKING

from homeassistant.components.button import ButtonEntity
from homeassistant.core import HomeAssistant, callback
from homeassistant.helpers import entity_registry as er
from homeassistant.helpers.dispatcher import async_dispatcher_connect
from homeassistant.helpers.entity_platform import AddConfigEntryEntitiesCallback

from .companion import slugify
from .const import DOMAIN
from .entity import Fp2Entity, async_setup_devices, unique_id
from .runtime import signal_changed

if TYPE_CHECKING:
    from . import Fp2CompanionConfigEntry
    from .companion import Fp2Device
    from .runtime import HassFp2Companion

PARALLEL_UPDATES = 0


async def async_setup_entry(
    hass: HomeAssistant,
    entry: Fp2CompanionConfigEntry,
    async_add_entities: AddConfigEntryEntitiesCallback,
) -> None:
    """Add the buttons of every FP2, and keep one per detection zone."""
    app = entry.runtime_data

    def entities(device: Fp2Device) -> list[ButtonEntity]:
        _ZoneButtons(app, device, async_add_entities).start(entry)
        return [
            RefreshButton(app, device),
            AiLearningButton(app, device),
            DeleteTargetsButton(app, device),
        ]

    entry.async_on_unload(async_setup_devices(app, async_add_entities, entities))


class RefreshButton(Fp2Entity, ButtonEntity):
    """Read the settings, the zones and the zone types again. The app never
    polls the cloud, so this is how a change made in the Aqara app shows."""

    _attr_translation_key = "refresh"
    _needs_cloud = False

    def __init__(self, app: HassFp2Companion, device: Fp2Device) -> None:
        """Set up the button."""
        super().__init__(app, device, "button", "refresh")

    async def async_press(self) -> None:
        """Read the device again."""
        self._run(self._device.refresh)


class AiLearningButton(Fp2Entity, ButtonEntity):
    """Start the AI learning run the Aqara app offers; it lasts a minute."""

    _attr_translation_key = "ai_learning"

    def __init__(self, app: HassFp2Companion, device: Fp2Device) -> None:
        """Set up the button."""
        super().__init__(app, device, "button", "ai_learning")

    async def async_press(self) -> None:
        """Start the run."""
        self._run(self._device.start_ai_learning)


class DeleteTargetsButton(Fp2Entity, ButtonEntity):
    """Drop every person the FP2 tracks, wherever in the room. A person who
    is really there is found again when they move."""

    _attr_translation_key = "delete_targets"

    def __init__(self, app: HassFp2Companion, device: Fp2Device) -> None:
        """Set up the button."""
        super().__init__(app, device, "button", "delete_targets")

    async def async_press(self) -> None:
        """Delete the targets."""
        self._run(self._device.targets_delete)


class ZoneDeleteTargetsButton(Fp2Entity, ButtonEntity):
    """Drop every person the FP2 tracks in one detection zone, which clears
    a zone that a ghost keeps occupied. Keyed by the zone's rangeId so a
    rename in the Aqara app keeps the entity."""

    _attr_translation_key = "zone_delete_targets"

    def __init__(
        self, app: HassFp2Companion, device: Fp2Device, range_id: int, name: str
    ) -> None:
        """Set up the button. Its entity id carries the zone's name, which
        reads better than the rangeId; the unique id keeps the rangeId."""
        slug = slugify(name) or f"zone_{range_id}"
        super().__init__(
            app,
            device,
            "button",
            f"zone_{range_id}_delete_targets",
            entity_key=f"{slug}_delete_targets",
        )
        self.range_id = range_id
        self.zone_name = name
        self._attr_translation_placeholders = {"zone": name}

    @property
    def available(self) -> bool:
        """Only while the zone is there and the cloud answers."""
        return self._device.reachable and any(
            z["rangeId"] == self.range_id for z in self._device.detection_zones()
        )

    async def async_press(self) -> None:
        """Delete the zone's targets."""
        self._run(self._device.targets_delete, self.range_id)


class _ZoneButtons:
    """Adds a button for each detection zone that appears and removes the
    button of a zone that is gone, after every read of the device. It
    follows the zones the way select.py's _ZoneSelects does."""

    def __init__(
        self,
        app: HassFp2Companion,
        device: Fp2Device,
        async_add_entities: AddConfigEntryEntitiesCallback,
    ) -> None:
        self._app = app
        self._device = device
        self._add = async_add_entities
        self._shown: dict[int, ZoneDeleteTargetsButton] = {}
        self._pruned = False

    def start(self, entry: Fp2CompanionConfigEntry) -> None:
        entry.async_on_unload(
            async_dispatcher_connect(
                self._app.hass,
                signal_changed(self._app.entry_id, self._device.key),
                self._sync,
            )
        )
        self._sync()

    async def _async_replace(
        self, old: ZoneDeleteTargetsButton, new: ZoneDeleteTargetsButton
    ) -> None:
        await old.async_remove()
        self._add([new])

    @callback
    def _sync(self) -> None:
        device = self._device
        # Before the first read the zones are unknown, not gone.
        if device.last_read is None:
            return
        zones = {z["rangeId"]: z.get("name") or "" for z in device.detection_zones()}
        for rid, entity in list(self._shown.items()):
            if rid in zones and zones[rid] != entity.zone_name:
                # Renamed in the Aqara app: added again for its new name, and
                # the registry keeps its entity id by its unique id.
                renamed = ZoneDeleteTargetsButton(self._app, device, rid, zones[rid])
                self._shown[rid] = renamed
                self._app.hass.async_create_task(
                    self._async_replace(entity, renamed), "fp2_companion zone rename"
                )
        new = [
            ZoneDeleteTargetsButton(self._app, device, rid, name)
            for rid, name in zones.items()
            if rid not in self._shown
        ]
        for entity in new:
            self._shown[entity.range_id] = entity
        if new:
            self._add(new)
        registry = er.async_get(self._app.hass)
        gone = {
            self._shown.pop(rid).unique_id or ""
            for rid in list(self._shown)
            if rid not in zones
        }
        if not self._pruned:
            # A zone deleted while Home Assistant was down left its button in
            # the registry; the first read finds it.
            self._pruned = True
            mine = unique_id(device, "zone_")
            gone |= {
                e.unique_id
                for e in er.async_entries_for_config_entry(
                    registry, self._app.entry_id
                )
                if e.domain == "button"
                and e.unique_id.startswith(mine)
                and e.unique_id not in {b.unique_id for b in self._shown.values()}
            }
        for unique in gone:
            entity_id = registry.async_get_entity_id("button", DOMAIN, unique)
            if entity_id is not None:
                registry.async_remove(entity_id)
