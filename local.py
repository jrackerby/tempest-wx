"""The station's own radio: a UDP listener on this component's socket.

WHY THIS EXISTS. A cloud-only weather entity and a local-only one each trade
away the other's strength: the first keeps its forecast through a WAN outage
and loses its TEMPERATURE, the second keeps every reading and has no forecast
or condition at all. A display that goes blank because the internet blinked is
worse than one showing a stale forecast.

So measurements come from the local radio and fall back to the cloud, while
condition and forecast stay cloud-only because the radio cannot produce them.
Losing the WAN degrades this entity instead of emptying it.

THE COUPLING THIS REPLACES. This module used to read nine `sensor.tempest_*`
entity ids belonging to a SEPARATE integration, which made the two a pair: pull
the other one and every local reading here fell through to the cloud. It now
opens its own socket, so the readings are this component's own and there is
nothing left to keep installed alongside it. The unit-conversion layer that
coupling needed is gone with it — the radio emits °C, millibars and m/s, which
are already this component's natives, so no value is converted on the way in
and none can be converted twice.

THE PORT IS EXCLUSIVE IN PRACTICE, AND THAT IS A CORRECTION. This paragraph
said the opposite for one release: that the socket's `SO_REUSEADDR` and
`SO_REUSEPORT` let it bind alongside the integration it replaces, so the two
could be compared side by side before either was removed. Both options are
still set here and they are still right — port sharing needs BOTH binders to
have opted into the SAME one, and which the other chose is not ours to decide.
What was wrong was the claim about the other one.

It was read from `pyweatherflowudp` 1.6.1, which does opt in and says so in its
own source. The integration that ships today PINS 1.4.5, which predates that
opt-in entirely — and a pinned dependency is the live fact, not whatever its
upstream has since become. So on a real install there is no sharing: whichever
listener starts first binds the port and the other fails with EADDRINUSE. It
was measured the expensive way, by shipping it and watching the neighbour go to
`setup_retry` with "Could not open a local UDP endpoint".

The consequence is that the cutover is ORDERED, not parallel: remove the other
integration, then restart. Backing that out is reinstalling it, which is a
HACS click — the earlier worry that an uninstall-first order could not be
backed out was overstated. What the code does about it is refuse to guess:
`udp.bind_failure_advice` names the port collision as the cause when the errno
says so, because the one thing worse than this failure is the misleading log
line it used to print, which sent the reader to the network instead.

A STATION THAT HAS GONE QUIET SAYS SO. Nothing drives an entity's state but an
arriving datagram, so with no other machinery a radio that stopped would leave
its last reading on the glass for ever, aging silently. The heartbeat below
exists to make the silence itself an event: freshness is re-judged on a timer
and the entities are told when the verdict changes, so losing sight of the
source reads as unavailable rather than as calm weather.
"""

from __future__ import annotations

import asyncio
import socket
import time
from datetime import timedelta
from typing import Any

from homeassistant.core import HomeAssistant, callback
from homeassistant.helpers.dispatcher import async_dispatcher_send
from homeassistant.helpers.event import async_track_time_interval

from .const import DOMAIN, LOGGER
from .udp import SOURCE_OF, UDP_PORT, bind_failure_advice, decode, parse

# How long a source's last message stays good for. Each is several times the
# interval the vendor documents for that message, so an ordinary dropped
# datagram does not flap an entity — losing sight of a source is a FALL, and a
# fall is what these windows are sized to catch rather than to argue with.
#
# `obs_st` is the exception: its own payload carries the report interval the
# station is configured for, so the window is computed from that and this
# number is only the floor for a station that has not reported one yet.
STALE_AFTER: dict[str, float] = {
    "obs_st": 240.0,
    "rapid_wind": 180.0,
    "device_status": 900.0,
    "hub_status": 900.0,
}

HEARTBEAT = timedelta(seconds=30)


@callback
def signal_update(entry_id: str) -> str:
    """The dispatcher signal one entry's local entities listen on."""
    return f"{DOMAIN}_local_{entry_id}"


class TempestLocalStation(asyncio.DatagramProtocol):
    """Everything the station has broadcast, and how long ago it said it."""

    def __init__(
        self,
        hass: HomeAssistant,
        entry_id: str,
        device_serial: str | None = None,
        hub_serial: str | None = None,
    ) -> None:
        """Bind to one entry, optionally filtered to one station's hardware."""
        self.hass = hass
        self.entry_id = entry_id
        self.device_serial = device_serial
        self.hub_serial = hub_serial
        self.listening = False

        self._readings: dict[str, Any] = {}
        self._seen: dict[str, float] = {}
        self._fresh: dict[str, bool] = {}
        self._transport: asyncio.DatagramTransport | None = None
        self._unsub_heartbeat: Any = None
        self._logged_unfiltered = False

    # -- lifecycle ---------------------------------------------------------

    async def async_start(self) -> None:
        """Open the socket. A refusal degrades this component; it never fails it.

        The local half is an enhancement over a working cloud entry, not a
        precondition for one: Home Assistant in a container without host
        networking never sees a LAN broadcast at all, and there is nothing the
        user can do about it from here. Raising would take a perfectly good
        forecast down with it, so a bind failure is reported once, at WARNING
        because it needs an edit somewhere and nobody can wait it out, and the
        entry sets up cloud-only.
        """
        try:
            sock = await self.hass.async_add_executor_job(_open_socket)
        except OSError as err:
            # The CAUSE decides the wording. A port already held and a Home
            # Assistant that cannot see the broadcast at all are different
            # faults with different fixes, and printing one message for both
            # sends half the readers to the wrong place.
            LOGGER.warning(
                "Could not open UDP port %s for the local Tempest radio (%s): "
                "%s. This station will run on the cloud API alone",
                UDP_PORT,
                err,
                bind_failure_advice(err.errno),
            )
            return

        self._transport, _ = await self.hass.loop.create_datagram_endpoint(
            lambda: self, sock=sock
        )
        self.listening = True
        self._unsub_heartbeat = async_track_time_interval(
            self.hass, self._async_heartbeat, HEARTBEAT
        )
        LOGGER.debug(
            "Listening for Tempest broadcasts on UDP %s (device=%s hub=%s)",
            UDP_PORT,
            self.device_serial or "any",
            self.hub_serial or "any",
        )

    @callback
    def async_stop(self) -> None:
        """Close the socket and stop the heartbeat. Safe to call twice."""
        if self._unsub_heartbeat is not None:
            self._unsub_heartbeat()
            self._unsub_heartbeat = None
        if self._transport is not None:
            self._transport.close()
            self._transport = None
        self.listening = False

    # -- reading -----------------------------------------------------------

    @callback
    def get(self, key: str) -> Any:
        """One reading, or None when the station has not reported it.

        None is never a zero. A caller that turned it into one would publish a
        calm, dry, windless day for a hub that has been unplugged.
        """
        return self._readings.get(key)

    @callback
    def is_fresh(self, key: str) -> bool:
        """Whether the message that carries this reading arrived recently."""
        source = SOURCE_OF.get(key)
        return False if source is None else self._source_is_fresh(source)

    @property
    def answering(self) -> bool:
        """Whether the radio is producing conditions at all.

        The once-a-minute observation is the test, not the three-second wind
        sample: a station sending wind and nothing else has lost the sensor
        every other reading comes from, and an entity with a wind speed and no
        temperature is not worth keeping a wall lit for.
        """
        return self._source_is_fresh("obs_st")

    def _source_is_fresh(self, source: str) -> bool:
        """Whether one message type has been heard inside its own window."""
        seen = self._seen.get(source)
        if seen is None:
            return False
        # MONOTONIC, not wall clock. Staleness is an elapsed-time question, and
        # the wall clock is at its least trustworthy exactly when this matters:
        # a box that boots without a network gets its time corrected by NTP
        # minutes later, and a backwards step would make every reading look
        # like it arrived from the future and stay "fresh" indefinitely.
        return time.monotonic() - seen < self._window(source)

    def _window(self, source: str) -> float:
        """The staleness window for one message type.

        `obs_st` is paced by the station's own configured report interval, read
        off the payload rather than assumed: a hub set to report every five
        minutes would otherwise be called dead four minutes into every
        perfectly healthy cycle.
        """
        floor = STALE_AFTER.get(source, 900.0)
        if source != "obs_st":
            return floor
        interval = self._readings.get("report_interval")
        if not interval:
            return floor
        return max(floor, float(interval) * 60.0 * 3.0)

    # -- the wire ----------------------------------------------------------

    def datagram_received(self, data: bytes, addr: tuple[str, int]) -> None:
        """One broadcast. Runs on the event loop, so it dispatches directly."""
        message = decode(data)
        if message is None or not self._is_ours(message):
            return

        parsed = parse(message)
        if parsed is None:
            return
        message_type, readings = parsed

        self._readings.update(readings)
        self._seen[message_type] = time.monotonic()
        self._fresh[message_type] = True
        async_dispatcher_send(self.hass, signal_update(self.entry_id), message_type)

    def error_received(self, exc: Exception) -> None:
        """A socket-level error on a connectionless socket. Note it, keep going."""
        LOGGER.debug("UDP error on port %s: %s", UDP_PORT, exc)

    def _is_ours(self, message: dict[str, Any]) -> bool:
        """Whether this datagram belongs to the station this entry configures.

        UNFILTERED IS A REAL STATE, NOT A BUG. The serials are learned from the
        cloud station record, and an entry created before that lookup existed —
        or one whose first setup happened with the WAN down — has none stored.
        Refusing every packet in that case would silently disable the local
        half on exactly the installs that already work; accepting everything is
        correct for the one-station LAN this component is built around, and
        the log line says which of the two is in force.
        """
        if self.device_serial is None and self.hub_serial is None:
            if not self._logged_unfiltered:
                self._logged_unfiltered = True
                LOGGER.info(
                    "No station serial stored for this entry, so every Tempest "
                    "broadcast on the LAN is accepted. Reload the entry with "
                    "the API reachable to learn the serials and filter"
                )
            return True

        serial = message.get("serial_number")
        hub = message.get("hub_sn")
        if self.device_serial is not None and serial == self.device_serial:
            return True
        # A hub_status message carries the hub's serial in `serial_number` and
        # no `hub_sn` at all, so both fields are checked against it.
        return self.hub_serial is not None and self.hub_serial in (serial, hub)

    # -- silence -----------------------------------------------------------

    @callback
    def _async_heartbeat(self, _now: Any) -> None:
        """Publish the freshness verdict when, and only when, it CHANGES.

        Dispatching on every tick would rewrite every local entity's state
        twice a minute for a station that has not moved. Dispatching never
        would leave a dead station's last reading on the glass indefinitely.
        The edge is the event worth sending.
        """
        for source in self._seen:
            fresh = self._source_is_fresh(source)
            if fresh != self._fresh.get(source):
                self._fresh[source] = fresh
                if not fresh:
                    LOGGER.info(
                        "No %s message from the Tempest radio for %.0fs; its "
                        "readings are now unavailable",
                        source,
                        self._window(source),
                    )
                async_dispatcher_send(
                    self.hass, signal_update(self.entry_id), source
                )


def _open_socket() -> socket.socket:
    """A bound, non-blocking UDP socket on the vendor's broadcast port.

    Opened in an executor because `bind` is a blocking syscall, and a blocking
    syscall on the event loop is the kind of thing that is invisible until the
    day it is slow.
    """
    sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    try:
        # BOTH, not either. A second binder shares the port only when it set
        # the same option this one did, and which one that is belongs to the
        # other process. Setting both costs nothing when this is the only
        # listener and is the only thing that COULD enable sharing when it is
        # not — though today it does not, because the other Tempest
        # integration pins a `pyweatherflowudp` that predates the opt-in.
        sock.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        # Absent on Windows, and defined-but-unimplemented on some kernels.
        # Attribute-checked AND caught, because those are two different
        # failures and only the first is knowable in advance.
        if hasattr(socket, "SO_REUSEPORT"):
            try:
                sock.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEPORT, 1)
            except OSError:  # pragma: no cover - kernel policy, not our call
                LOGGER.debug("SO_REUSEPORT refused; one listener per port here")
        sock.setsockopt(socket.SOL_SOCKET, socket.SO_BROADCAST, 1)
        sock.bind(("0.0.0.0", UDP_PORT))
        sock.setblocking(False)
    except OSError:
        sock.close()
        raise
    return sock
