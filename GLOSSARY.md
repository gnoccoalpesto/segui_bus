# segui_bus

The shared language of the bus-proximity notifier: what a watch is, how a bus
becomes relevant, and the two strengths of alarm it can produce.

## Watch and candidates

**Watch**:
A line-and-stop pair with the thresholds, earliest arrival and selection
strategy that make a bus relevant. Every watch is stored as a preset; at most
one is followed.
_Avoid_: subscription, config entry

**Watch preset**:
A stored watch, identified by a case-insensitive id, that the service does not
evaluate until it is followed.
_Avoid_: watch item, saved watch

**Followed watch**:
The single watch the service currently evaluates and alerts on.
_Avoid_: active watch, selected watch (when following is meant)

**Monitor mode**:
A way to run the service with one ephemeral watch given on the command line,
without stored presets or remote preset commands.
_Avoid_: inline mode, debug mode

**Auto value**:
A watch parameter computed from the static feed instead of set by the user; it
is recomputed whenever the feed is reloaded.
_Avoid_: default, None

**Candidate**:
A vehicle currently running the watched line, with its relation to the watched
stop.
_Avoid_: vehicle, bus (when the relation to the stop is meant)

**Qualifying bus**:
A candidate whose expected arrival is not before the watch's earliest arrival
and that meets at least one proximity arm.
_Avoid_: matching bus, relevant bus

**Expected arrival**:
The best estimate of when a candidate will reach the watched stop: realtime
estimate first, schedule second.
_Avoid_: ETA time, predicted time

**Earliest arrival**:
The local service-day time before which an arrival at the watched stop is
ignored.
_Avoid_: start time, minimum time

## Proximity and alarms

**Fence**:
The circle around the watched stop; a bus inside it is considered at the stop.
_Avoid_: radius, geofence

**Stops to target**:
The number of scheduled stops between a bus's current progress and the watched
stop.
_Avoid_: stop distance, remaining stops

**ETA**:
The time remaining until a bus reaches the watched stop.
_Avoid_: delay, countdown

**Proximity arm**:
One of the three independent signals that can make a bus qualify: fence, stops
to target, or ETA.
_Avoid_: criterion, trigger (when the arm is meant)

**Real alarm**:
A notification raised by the fence arm, the stops arm, or an on-time ETA; it
raises the global latch.
_Avoid_: alert, warning

**Preavviso**:
A lower-importance notification raised by the ETA arm when the bus is not on
time; it never raises the latch.
_Avoid_: soft alarm, early warning

**Global latch**:
The single active real alarm per watch; it stays raised until the alerted trip
leaves the candidate set and blocks further real alarms meanwhile.
_Avoid_: alarm state, re-arm
_Note_: "re-arm" is what happens on release, not the latch itself.

**On time**:
A realtime arrival estimate whose absolute delay is within the watch's
tolerance. A schedule-only estimate is never on time.
_Avoid_: punctual, accurate

**Notification**:
A delivered message produced by a real alarm or a preavviso.
_Avoid_: alarm, message (when the delivery is meant)

## Degraded data

**Stale feed**:
A realtime snapshot older than the freshness limit.
_Avoid_: old data, outdated feed

**Stale projection**:
The remaining time estimated from a stale snapshot by taking the largest of the
available estimates; it is flagged, never suppressed.
_Avoid_: extrapolation, dead reckoning

**Malformed ride**:
A vehicle whose reported trip identity, stop sequence, or position does not
agree with the static feed; only the fence arm is trusted.
_Avoid_: bad trip, inconsistent bus
