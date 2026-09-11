# Tempest Weather

Current conditions, **forecast** and the full local sensor set from a
WeatherFlow Tempest station — the station's own UDP broadcast for measurements,
and the same cloud endpoint the Tempest phone app renders from for everything
the radio cannot produce.

A Tempest broadcasts its measurements over the local network by UDP, but the
radio carries no *condition* and no *forecast* — the station's icon and its
10-day outlook exist only in WeatherFlow's cloud. So a local-only setup has
readings and no weather entity worth the name, and a cloud-only one goes dark
the moment the internet does.

**This integration is the hybrid, and as of 0.2.0 it is the whole of it.** It
opens its own socket on UDP 50222 and publishes the local readings as its own
entities; condition and forecast come from the cloud, because the radio cannot
produce them. A WAN outage therefore *ages* the weather entity instead of
emptying it — the forecast goes stale while every display keeps its current
temperature.

Nothing else needs to be installed alongside it.

## What it publishes

### From the station's radio, over your LAN

Temperature, humidity, station pressure; wind speed, average, gust and lull,
with both the three-second bearing and the interval average; illuminance, UV
and solar radiation; rain for the report interval and the rate that implies;
precipitation type; lightning strikes and average distance for the interval.

Four values the radio does not send but the Tempest app shows are computed from
that same observation, so they survive a WAN outage along with the measurements
they come from: **dew point** (Magnus-Tetens), **wet-bulb temperature** (Stull
2011, withheld outside the band its fit covers), **feels like** (the NWS heat
index and wind chill, each applied only in the range it is defined for), and
**air density** — computed for *moist* air, which is about 1 % lower than the
dry-air figure and always in that direction, because water vapour is lighter
than the air it displaces.

The hardware's own health is published as diagnostics: battery voltage, both
signal strengths, both firmware revisions, when each device last booted, and a
`problem` binary sensor carrying the failure bits out of `sensor_status` — named
rather than counted, so it tells you *which* sensor failed.

### From the cloud, because nothing else can answer it

- **A real weather entity** with 10-day daily and hourly forecast.
- **The station's own condition string**, from the API's `icon` field. Nothing
  is derived. Deriving a condition locally means a ladder — precipitation, then
  fog, then a day/night split, then cloud cover inferred from solar radiation
  against a clear-sky model — and the dusk band in that ladder is where such
  implementations go wrong, because there is no clean threshold to pick. With
  the station's own icon there is nothing to derive and no band to argue about.
- **Windowed lightning**: strikes in the last hour and last three hours, last
  distance, last strike time — already windowed by the vendor. The radio's own
  counter is per report interval and says nothing about the hour, and nothing
  here classifies a `lightning` *condition* from it: reading a per-interval
  counter as "strikes now" would latch the entity into `lightning` for ever
  after the first strike.
- **Daily rain totals** — today and yesterday, in millimetres and in minutes —
  which the radio reports only per report interval.
- **WeatherFlow's own rain check** as two binary sensors. A Tempest's rain
  sensor fires on vibration, so a slammed door can register rain that never
  fell; the rain check is the vendor cross-checking its own gauge against
  nearby stations. Published separately so `0 mm, measured` stays distinct
  from `0 mm, do not trust this`.
- Sea-level pressure, pressure trend, wet-bulb globe temperature, cardinal
  wind direction, today's forecast high/low and rain chance, sunrise and
  sunset.

No quantity appears in both sets. Publishing one reading from two sources gives
you two entities that disagree whenever the WAN blinks, and no way to tell
which one a dashboard is reading.

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

### Networking

The local half needs Home Assistant to be on the **same broadcast domain** as
the hub — the station broadcasts to the LAN broadcast address on UDP 50222 and
nothing routes that. Home Assistant OS, Supervised and a container on host
networking all see it. A container on a *bridge* network does not, and there is
nothing this integration can do about that from inside: it logs one warning and
runs cloud-only.

The socket is opened with both `SO_REUSEADDR` and `SO_REUSEPORT`, so it binds
even while another integration is already listening on the same port. That is
deliberate — see *Replacing the HACS `tempest` integration* below.

## Configuration

One field: a **personal access token**, created at
[tempestwx.com](https://tempestwx.com) under Settings → Data Authorizations →
Create Token. Then pick a station.

The flow calls `better_forecast` against the station you picked, with the token
you pasted, before it creates the entry — the same endpoint and credential the
coordinator will poll a minute later. A station that answers with no forecast
is refused at setup rather than configured green and found empty later.

It also records that station's hardware serials, so the UDP listener can tell
your station's broadcasts from a neighbour's on a LAN with more than one
Tempest. An entry created before 0.2.0 has none stored; setup fills them in on
the next load, and until it does the listener accepts every Tempest broadcast
it sees and says so in the log.

Poll cadence for the cloud half comes from the payload's own
`refresh_interval_seconds`, floored at 60s. Nothing here picks a number. The
local half is not polled at all — the station pushes.

## Replacing the HACS `tempest` integration

Until 0.2.0 this integration read its local readings out of
[`julianbow/TempestHomeAssistant`](https://github.com/julianbow/TempestHomeAssistant)
by entity id, and needed it installed. It no longer does, and it now publishes
everything that integration publishes.

**Both can run at once, and that is the recommended way to cut over.** Port
sharing needs both listeners to have opted in, and they have: that integration
listens through `pyweatherflowudp`, which sets `SO_REUSEPORT`, and this one sets
`SO_REUSEADDR` and `SO_REUSEPORT` both. Because the station *broadcasts*, the
kernel then delivers each datagram to every socket bound to the port rather than
balancing between them, so the two read the same packets and neither starves the
other. Install this one, compare the readings you care about for as long as you
like, and only then remove the other. A listener that refused to start while
something else held the port would force the uninstall to come first — the one
order in which a bad cutover cannot be backed out.

Two things to know before you repoint anything:

- **The entity ids are different**, and deliberately so: these are
  `sensor.<your station>_*`, not `sensor.tempest_sensor_*`. Anything reading the
  old ids — dashboards, automations, templates — has to be repointed. Grep for
  `tempest_sensor` before you uninstall, not after.
- **Home Assistant does not release an entity id when its config is deleted.**
  The registry row survives, so deleting the old integration does not free its
  ids — it leaves them locked, and anything still reading one goes
  `unavailable`. If you want to *reuse* an old id here, rename the old registry
  row out of the way **first**; that is what actually frees it.

Two readings are not reproduced, on purpose:

- `sensor.tempest_sensor_pressure`, which is that integration's sea-level
  pressure and reads wrong — 0.905 inHg against a station pressure of 29.36 on
  the station this was developed against, out by a factor of about 32. Sea-level
  pressure is published here from the cloud, where it is correct, and the local
  `station_pressure` is published under a name that says which one it is.
- `sensor.tempest_hub_uptime` and the station uptime, which are durations: a
  duration changes every time it is read, so the sensor writes a new state once
  a minute for a device that has not moved. Published here as **last boot**
  instead — a constant that changes when the device actually reboots, which is
  the event anyone watching it wants.

## Known limitations

- **Setup needs the internet and a token, and will retry until it has them.**
  The condition and the forecast are cloud-only, so an entry that loaded
  without them would be a weather entity in name only. Home Assistant started
  during a WAN outage retries this entry rather than loading it half-alive —
  which means the local readings are not published during that window either.
  Once loaded, a WAN outage only ages the forecast.
- `iot_class` is declared `cloud_polling` even though most of what is published
  now arrives by local push. That is the honest answer to the question the
  badge is actually asked — *does this need the cloud?* — and it does: no
  token, no entry.
- **One station per config entry.** Add a second entry for a second station.
- **Sea-level pressure is cloud-only.** Deriving it locally needs the station's
  elevation, which is not in the broadcast.
- The lightning fields in the *cloud* payload are looked up under **both**
  vendor spellings — WeatherFlow's documentation names them `lighting_*` (no
  first `n`) while `weatherflow4py` declares them `lightning_*`. Whichever the
  API emits is the one that answers. See `forecast.pick`.
- The older two-piece **Air and Sky** hardware is not supported: this reads
  `obs_st`, and those broadcast `obs_air` and `obs_sky`, which are ignored by
  name.

## Removal

Settings → Devices & Services → Tempest Weather → Delete. Entities, the station
device and the UDP socket all go with the entry.

## Tests

```
./tools/run_tests.sh
```

100 tests over the layers that import nothing from Home Assistant — the cloud
transform, the UDP wire format, the derived quantities — plus static joins over
the platform declarations, parsed by `ast` rather than imported. No Home
Assistant, no network, no token.

Several are self-tests: they re-implement the defects this component was written
to fix — a transposed observation index, a coupling to another integration's
entity ids, a translation left behind by a deleted sensor — and assert the
checks would have caught them. An assertion set needs a self-test proving it can
fail.

## Status

In production use, verified against a live station: the vendor's own condition
rather than a derived one, 10 daily and 218 hourly forecast rows through
`weather.get_forecasts`, and readings served from the local radio with the cloud
as fallback.
