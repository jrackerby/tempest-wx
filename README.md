# tempest_wx — the estate's WeatherFlow Tempest integration

Current conditions and **forecast** from the backyard Tempest, read from the
same endpoint the Tempest phone app renders from.

Replaces the HACS `tempest` component (`julianbow/TempestHomeAssistant`).
GH-587 carries the verified defect list; the short version is that it runs here
in local-UDP mode, which forwards only the sensor platform, so it publishes no
weather entity and no forecast at all — and its two condition maps disagree
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
  is derived. `packages/weather_home.yaml` derives one today — precipitation,
  then fog, then a day/night split, then cloud cover inferred from solar
  radiation against a clear-sky model — and GH-584 is open against its dusk
  band. With the station's own icon there is nothing to derive.
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

`custom_components/tempest_wx/` in this repo, deployed by `git push ha master`.
`custom_components/` changes need a **full Home Assistant restart** — a core
config reload does not re-import a custom component.

Then: Settings → Devices & Services → Add Integration → "Tempest (estate)".

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

Settings → Devices & Services → Tempest (estate) → Delete. Entities and the
station device go with the entry. The local `tempest` integration is separate
and is unaffected.

## Tests

```
cd custom_components/tempest_wx/tests && pytest
```

Tests over `forecast.py`, which imports nothing from `homeassistant` and so
runs with no Home Assistant, no network and no token. Three of them are
self-tests that re-implement the defects this component was written to fix and
assert the checks would have caught them (LAW.md §4: an assertion set needs a
self-test proving it can fail).

## Status

**Live and verified.** The entity serves `weather.forecast_home` — it took that
id over from the template in `packages/weather_home.yaml`, which is deleted, so
every board and consumer reading that id was cut over without a code change in
any dashboard repo. Forecast confirmed against the live API: 10 daily and 219
hourly rows.
