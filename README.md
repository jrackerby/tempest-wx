# Tempest Weather

Current conditions and **forecast** from a WeatherFlow Tempest station, read
from the same endpoint the Tempest phone app renders from.

Replaces the HACS `tempest` component (`julianbow/TempestHomeAssistant`).
The verified defect list is short: run in local-UDP mode it forwards only the
sensor platform, so it publishes no weather entity and no forecast at all — and its two condition maps disagree
with each other on 6 of the vendor's 19 icons.

## What it adds

The **sensors** here are values the local UDP broadcast does not carry;
temperature, humidity, station pressure, wind, UV, illuminance and solar
radiation keep arriving over the local radio and are deliberately **not**
republished from the cloud as duplicate sensors.

The **weather entity** is a hybrid, and that is the point: its measurements
prefer the local radio and fall back to the cloud, while condition and forecast
are cloud-only because the radio cannot produce them. A WAN outage therefore
ages this entity instead of emptying it — the forecast goes stale but every
wall panel keeps its temperature. See `local.py`.

- **A real weather entity** with 10-day daily and hourly forecast.
- **The station's own condition string**, from the API's `icon` field. Nothing
  is derived. The template entity this replaced derived one — precipitation,
  then fog, then a day/night split, then cloud cover inferred from solar
  radiation against a clear-sky model — and its dusk band was a standing
  defect. With the station's own icon there is nothing to
  derive and no band to rule on.
- **Windowed lightning**: strikes in the last hour and last three hours, last
  distance, last strike time. `weather_home.yaml` refuses to classify a
  `lightning` condition at all because the local strike counter's reset
  behaviour was never measured and reading it as "strikes now" would latch the
  entity into `lightning` forever after the first strike. These counters are
  already windowed by the vendor.
- **Daily rain totals** — today and yesterday, in millimetres and in minutes —
  which UDP reports only per observation.
- **WeatherFlow's own rain check** as two binary sensors. A Tempest's rain
  sensor fires on vibration, so a slammed door can register rain that never
  fell; the rain check is the vendor cross-checking its own gauge against
  nearby stations. Published separately so `0 mm, measured` stays distinct
  from `0 mm, do not trust this`.
- Sea-level pressure, pressure trend, wet-bulb globe temperature, cardinal
  wind direction, today's forecast high/low and rain chance, sunrise and
  sunset.

## Installation

### HACS

1. In Home Assistant: **HACS → ⋮ → Custom repositories**.
2. Add `https://github.com/jrackerby/tempest-wx` with category **Integration**.
3. Install **Tempest Weather**, then restart Home Assistant.
4. **Settings → Devices & Services → Add Integration → "Tempest Weather"**.

### Manual

The integration lives at the repository **root**, not under
`custom_components/` — `hacs.json` declares `content_in_root: true`. To install
by hand, copy this repository's contents into
`config/custom_components/tempest_wx/` and restart Home Assistant.

Either way a `custom_components/` change needs a **full Home Assistant
restart**; `homeassistant.reload_core_config` does not re-import a custom
component.

## Configuration

One field: a **personal access token**, created at
[tempestwx.com](https://tempestwx.com) under Settings → Data Authorizations →
Create Token. Then pick a station.

The flow calls `better_forecast` against the station you picked, with the token
you pasted, before it creates the entry — the same endpoint and credential the
coordinator will poll a minute later. A station that answers with no forecast
is refused at setup rather than configured green and found empty later.

Poll cadence comes from the payload's own `refresh_interval_seconds`, floored
at 60s. Nothing here picks a number.

## Known limitations

- **The forecast and the cloud-only sensors need the internet and a token.**
  The weather entity does not go with them: its readings fall back to the local
  radio, so a WAN outage leaves current conditions intact and only the forecast
  and the derived sensors go unavailable.
- **The weather entity reads the HACS `tempest` integration's sensor ids**
  (`sensor.tempest_sensor_*`) for its local half. That coupling is declared in
  `local.py` rather than hidden. If that integration is removed the lookups
  find nothing and every reading falls through to the cloud, which is the
  documented fallback and not a failure.
- **One station per config entry.** Add a second entry for a second station.
- The lightning fields are looked up under **both** vendor spellings —
  WeatherFlow's documentation names them `lighting_*` (no first `n`) while
  `weatherflow4py` declares them `lightning_*`. Whichever the API emits is the
  one that answers. See `forecast.pick`.

## Removal

Settings → Devices & Services → Tempest Weather → Delete. Entities and the
station device go with the entry. The local `tempest` integration is separate
and is unaffected.

## Tests

```
cd custom_components/tempest_wx/tests && pytest
```

Tests over `forecast.py`, which imports nothing from `homeassistant` and so
runs with no Home Assistant, no network and no token. Three of them are
self-tests that re-implement the defects this component was written to fix and
assert the checks would have caught them — an assertion set needs a self-test
proving it can fail.

## Status

In production use. Verified against a live station: the vendor's own condition
rather than a derived one, 10 daily and 218 hourly forecast rows through
`weather.get_forecasts`, and readings served from the local radio with the
cloud as fallback.

### Replacing an existing weather entity

Worth knowing if you are cutting over from a template or another integration:
**Home Assistant does not release an entity id when its config is deleted.**
The registry row survives, so deleting the old config does not free the id — it
leaves it locked, and anything reading it goes `unavailable`. Rename the old
registry row out of the way *first*; that is what actually frees the id for
this integration to claim.
