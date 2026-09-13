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

**Only one listener can hold UDP 50222.** Sharing a port requires *both*
binders to opt into it, and the integration this replaces does not (see below),
so whichever starts first wins and the other runs cloud-only. This socket sets
`SO_REUSEADDR` and `SO_REUSEPORT` anyway — they cost nothing alone and are the
only thing that could ever enable sharing — but do not plan around them.

You can also decline the contest: the listener has an off switch (see
**Options**), so which integration holds the port is a decision rather than a
race decided by Home Assistant's setup order.

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

That 60s floor holds on every path to the API, not only the schedule: a
`homeassistant.update_entity` call (or an automation looping one) coalesces
into at most one poll per minute. When a poll fails, the next attempt is
paced by the *kind* of failure. A **rate limit** (HTTP 429, or a 503 naming a
`Retry-After`) is retried when the vendor said to, or, with no header, at a
wait that doubles from the normal cadence on each consecutive refusal, up to
an hour. A **connection that never answered** — a timeout, a WAN outage — is
retried sooner than the cadence, from the floor, doubling back up to the
cadence and never past it, so a blip recovers in about a minute and a long
outage costs the vendor nothing beyond a healthy day's polling. A successful
poll resets the count and the schedule.

### Options

One option, on the integration's **Configure** button, changeable after setup:

| Option | Default | Effect |
| --- | --- | --- |
| Listen to the station's local broadcast | **on** | Opens the UDP 50222 socket. Switched **off**, the entry runs cloud-only: the forecast, the condition and every cloud reading carry on, and the local readings go `unavailable`. |

Changing it **reloads the entry**, because that is what opens or closes the
socket — the setting on its own changes nothing already running.

The entity set does not change either way. Switched off, the local entities are
exactly as unavailable as they are when the bind is refused, on purpose: one
code path, and cloud-only means the same thing and looks the same way however
you arrived at it.

Why it exists: UDP 50222 is exclusive in practice, so without an off switch the
listener that loses is chosen by Home Assistant's setup order rather than by
anyone. Switching it off is how you ask for the cloud half deliberately — to run
this alongside another Tempest integration, or on a Home Assistant that cannot
see the broadcast at all.

## Replacing the HACS `tempest` integration

Until 0.2.0 this integration read its local readings out of
[`julianbow/TempestHomeAssistant`](https://github.com/julianbow/TempestHomeAssistant)
by entity id, and needed it installed. It no longer does, and it now publishes
everything that integration publishes.

**Their two listeners cannot run side by side — but you can switch this one's
off, and then they can.** The cutover is a sequence you choose rather than a
race:

1. Install this integration with **Listen to the station's local broadcast**
   switched off. Nothing contends for UDP 50222, and the cloud half is live
   immediately.
2. Compare it against the readings you already trust, for as long as you like.
3. Remove the HACS `tempest` integration.
4. Switch the listener on. The entry reloads and the local readings appear.

Backing out at any point is reinstalling the other integration from HACS, which
is a click.

The reason step 1 needs the switch at all is below, and 0.2.0 claimed the
opposite and was wrong, so here is the measurement. Port
sharing needs *both* binders to opt in. This one does. That one listens through
`pyweatherflowudp`, and while **1.6.1** opts into `SO_REUSEPORT` — which is what
0.2.0's claim was read from — the integration as shipped **pins 1.4.5**, which
predates the opt-in entirely. A pinned dependency is the fact; its upstream is
not. So on a real install the second listener to start fails with `EADDRINUSE`
and reports `Could not open a local UDP endpoint`, and which one that is depends
on Home Assistant's setup order, not on anything you chose.

0.2.0 also called an uninstall-first order unrecoverable, and that was overstated
too — it is one HACS click either way.

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
- **The off switch declines the contest; it does not win it.** With the listener
  on, UDP 50222 is still exclusive in practice. If another Tempest listener is
  also running, whichever Home Assistant starts first binds the port and the
  other logs the collision and runs cloud-only — which one that is is still not
  yours to choose. What is yours is whether to enter at all.
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

## Icon and logo

The `t°` mark in `brand/` is what Home Assistant shows for this integration —
on the Devices & Services card, on the station device page, and in the HACS
panel once the component is installed.

Home Assistant 2026.3 made that a local file. `homeassistant.loader` sets
`has_branding` from the presence of a top-level `brand` directory, and the
`brands` integration reads the images out of it **ahead of** the
`brands.home-assistant.io` CDN — so no pull request against
`home-assistant/brands` is involved, and the `custom_integrations/` folder over
there is now legacy.

Two files ship, both square:

| file | size |
| --- | --- |
| `brand/icon.png` | 256×256 |
| `brand/icon@2x.png` | 512×512 |

No `logo.png`, and no `dark_` variants, deliberately. The artwork is square, so
the brands specification says to ship the icon alone; core then walks its own
fallback chain (`IMAGE_FALLBACKS`) inside `brand/` and answers a request for
`logo@2x.png` or `dark_logo.png` from the icon without ever reaching the CDN.
`tests/test_brand.py` encodes that chain and asserts all eight servable names
resolve to a file that is actually here, which is what makes shipping two
correct rather than lucky.

Two things this does **not** reach:

- **The HACS store listing for a repository nobody has installed yet.** HACS
  cannot read a `brand/` directory out of a component that is not on disk, so
  the store entry falls back to the CDN and gets the placeholder. Only an
  accepted PR against `home-assistant/brands` would change that.
- **Anything outside the Home Assistant frontend.** An external dashboard has
  two options: fetch
  `/api/brands/integration/tempest_wx/icon.png` from Home Assistant with its
  own bearer token, or vendor the PNG into the app's own `public/` and serve it
  from there. The file here is the canonical copy either way.

## Removal

Settings → Devices & Services → Tempest Weather → Delete. Entities, the station
device and the UDP socket all go with the entry.

## Tests

```
./tools/run_tests.sh
```

120 tests over the layers that import nothing from Home Assistant — the cloud
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
