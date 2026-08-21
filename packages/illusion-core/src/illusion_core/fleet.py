"""Fleet status: what is running, which version, and for how long.

Every service answers GET /health with its own version and uptime. claws probes
them on demand and assembles the picture, so `about` is one round trip from
either frontend.

Two rules make the numbers trustworthy:

Durations, never timestamps. /health reports uptime_ms computed on the
service's own clock. The kiosk laptop gets shut down and carried around, so its
clock can drift from the VM's; a duration is immune to skew between hosts,
while an absolute start time subtracted against the reader's clock is not.

Health is unauthenticated. It carries only a name, a version and an uptime, it
is bound to the tailnet, and keeping it open means the fleet can still report
liveness when a shared token has been rotated on one side but not the other --
which is exactly the moment you want to be able to see what is running.
"""

from fastapi import FastAPI

from illusion_core.uptime import format_duration, service_uptime_ms, system_uptime_ms

# How long an unreachable service's last known uptime is still worth showing.
# Past this it is a stale cache entry rather than information, and real uptimes
# top out in the low hundreds of days, so an implausible figure here would only
# ever be an artefact.
ASSUME_LIMIT_S = 86400

PROBE_TIMEOUT_S = 3


def health_payload(service, version, started_at, extra=None):
    return {
        "service": service,
        "version": version,
        "uptime_ms": service_uptime_ms(started_at),
        "system_uptime_ms": system_uptime_ms(),
        **(extra or {}),
    }


def make_health_app(service, version, started_at, extra=None):
    """A one route app, for services that are otherwise pure clients."""
    app = FastAPI(title=service, version=version)

    @app.get("/health")
    async def health():
        return health_payload(service, version, started_at, extra)

    return app


def make_health_server(app, host, port):
    """A uvicorn server for a health app, to run as a background task.

    Handed back rather than started here so the caller can ask it to stop:
    cancelling uvicorn mid-serve leaves its lifespan task to die unhandled and
    print a CancelledError traceback, which on the kiosk lands on a screen
    people are looking at.
    """
    import uvicorn

    return uvicorn.Server(
        uvicorn.Config(app, host=host, port=port, log_level="warning")
    )


def cached_entry(name, cached, age_ms):
    """Report a service that could not be reached, from what it last told us.

    An assumed uptime is only worth as much as the probe it extrapolates from,
    so past ASSUME_LIMIT_S the figure is dropped: claws adding elapsed time to a
    months-old probe produces a number that looks like data but is really just a
    stale cache entry. That is the only realistic way to see an implausible
    uptime here, since real ones top out in the low hundreds of days.
    """
    if cached is None:
        return {"service": name, "state": "unknown", "version": None}

    payload = cached["payload"]

    if age_ms > ASSUME_LIMIT_S * 1000:
        return {
            "service": name,
            "state": "unknown",
            "version": payload.get("version"),
            "last_reached_ms": age_ms,
        }

    def extend(value):
        return None if value is None else value + age_ms

    return {
        "service": name,
        "state": "assumed",
        "version": payload.get("version"),
        "uptime_ms": extend(payload.get("uptime_ms")),
        "system_uptime_ms": extend(payload.get("system_uptime_ms")),
        "last_reached_ms": age_ms,
    }


def fleet_rows(status):
    """Turn a /status payload into rows for a table or an embed."""
    rows = []

    for entry in status["services"]:
        state = entry["state"]

        if state == "ok":
            uptime = format_duration(entry.get("uptime_ms"))
        elif state == "assumed":
            uptime = f"{format_duration(entry.get('uptime_ms'))} (assumed)"
        else:
            uptime = "Unknown"

        if state == "ok":
            note = ""
        elif entry.get("last_reached_ms") is not None:
            note = f"last reached {format_duration(entry['last_reached_ms'])} ago"
        else:
            note = "never reached"

        rows.append(
            {
                "SERVICE": entry["service"],
                "VERSION": entry.get("version") or "?",
                "UPTIME": uptime,
                "SYSTEM": format_duration(entry.get("system_uptime_ms")),
                "STATE": note or "ok",
            }
        )

    return rows


FLEET_FIELD_NAMES = {
    "SERVICE": "Service",
    "VERSION": "Version",
    "UPTIME": "Uptime",
    "SYSTEM": "Host up",
    "STATE": "State",
}


def version_skew(status):
    """Differing versions mean a host did not get redeployed. Say so."""
    versions = {
        entry.get("version")
        for entry in status["services"]
        if entry["state"] in ("ok", "assumed") and entry.get("version")
    }

    if len(versions) > 1:
        return f"Version mismatch across the fleet: {', '.join(sorted(versions))}"

    return None


async def announce(claws, service, version, started_at, host=None):
    """Tell claws we exist. Best effort: a failed announce is not fatal.

    claws probes on demand anyway, so this only warms the cache and makes a
    restart visible without waiting for someone to run `about`.
    """
    try:
        await claws.register(health_payload(service, version, started_at, {"host": host}))
    except Exception as e:
        print(f"Could not announce to claws: {e}")
