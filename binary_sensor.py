"""WeatherFlow's own rain-check verdicts.

`is_precip_local_day_rain_check` is not "the rain gauge moved". It is
WeatherFlow's cross-check of this station's haptic rain sensor against nearby
stations, and it is the field the Tempest app uses to decide whether to show
today's rain total at all. A Tempest's rain sensor fires on vibration, so a
slammed door, a bird, or a branch can register precipitation that never fell;
the rain check is the vendor saying whether it believes its own gauge.

Publishing it separately keeps `ok at zero` distinct from `could not read`
for the two accumulation sensors next door. Zero millimetres with
the check TRUE is a dry day that was measured. Zero with the check FALSE, or
with no check at all, is a number nobody should be reading as weather.
"""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass
from typing import Any

from homeassistant.components.binary_sensor import (
    BinarySensorEntity,
    BinarySensorEntityDescription,
)
from homeassistant.helpers.entity_platform import AddConfigEntryEntitiesCallback

from . import TempestConfigEntry
from .const import DOMAIN
from .entity import TempestEntity
from .forecast import current_conditions

# Every entity on this platform reads an already-fetched coordinator
# payload; nothing here talks to the API on its own, so there is no
# request rate to limit. 0 = unlimited, which is the coordinator-backed
# convention. Declared rather than left to default so the quality scale's
# `parallel-updates` rule is answered explicitly.
PARALLEL_UPDATES = 0


@dataclass(frozen=True, kw_only=True)
class TempestBinarySensorDescription(BinarySensorEntityDescription):
    """A binary sensor plus its reader."""

    value_fn: Callable[[dict[str, Any]], Any]


def _flag(key: str) -> Callable[[dict[str, Any]], Any]:
    """Read one boolean out of current_conditions."""

    def _read(payload: dict[str, Any]) -> Any:
        return current_conditions(payload).get(key)

    return _read


BINARY_SENSORS: tuple[TempestBinarySensorDescription, ...] = (
    TempestBinarySensorDescription(
        key="precip_day_rain_check",
        translation_key="precip_day_rain_check",
        value_fn=_flag("is_precip_local_day_rain_check"),
    ),
    TempestBinarySensorDescription(
        key="precip_yesterday_rain_check",
        translation_key="precip_yesterday_rain_check",
        value_fn=_flag("is_precip_local_yesterday_rain_check"),
    ),
)


async def async_setup_entry(
    hass: Any,
    entry: TempestConfigEntry,
    async_add_entities: AddConfigEntryEntitiesCallback,
) -> None:
    """Add the rain-check sensors."""
    coordinator = entry.runtime_data
    async_add_entities(
        TempestBinarySensor(coordinator, description)
        for description in BINARY_SENSORS
    )


class TempestBinarySensor(TempestEntity, BinarySensorEntity):
    """One vendor verdict."""

    entity_description: TempestBinarySensorDescription

    def __init__(
        self, coordinator: Any, description: TempestBinarySensorDescription
    ) -> None:
        """Bind the description to the station."""
        super().__init__(coordinator)
        self.entity_description = description
        self._attr_unique_id = f"{DOMAIN}_{coordinator.station_id}_{description.key}"

    @property
    def is_on(self) -> bool | None:
        """The verdict, or None when the payload does not carry one.

        None rather than False. A station that did not report a rain check has
        not said "no rain"; it has said nothing, and collapsing those two is
        the same defect as defaulting an absent measurement to zero.
        """
        data = self.coordinator.data
        if not isinstance(data, dict):
            return None
        value = self.entity_description.value_fn(data)
        return None if value is None else bool(value)
