"""claws: the inventory service.

Owns the database and every rule about stock. It knows nothing about Discord or
about printers: when an item crosses its low threshold, claws records the fact
and announces it, and whoever cares about forum threads reacts to that. That is
the coupling this service exists to break -- the low-stock thread used to be
created inline by whichever process happened to change the stock, which stops
working the moment the database lives on a different machine from the bot.
"""

import asyncio
import json
import time

import httpx

from fastapi import Depends, FastAPI, Header, HTTPException
from fastapi.responses import StreamingResponse
from pydantic import BaseModel

from illusion_core import config as illusion_config
from illusion_core.events import EventBus
from illusion_core.fleet import (
    PROBE_TIMEOUT_S,
    cached_entry,
    health_payload,
)
from illusion_core.uptime import service_uptime_ms, system_uptime_ms
from claws.digikey_client import DigiKeyClient
from claws.inventory_reader import SpreadsheetManager

VERSION = "1.5.0"


class NewItem(BaseModel):
    item: dict


class Updates(BaseModel):
    updates: dict


class Amount(BaseModel):
    amount: float | None = None


class Quantity(BaseModel):
    quantity: float


class TagRequest(BaseModel):
    tag: str


class VendorRequest(BaseModel):
    vendor_name: str
    link: str


class LowThread(BaseModel):
    thread_id: int | None = None


class ScanRequest(BaseModel):
    barcode: str


class Registration(BaseModel):
    service: str
    version: str
    uptime_ms: int | None = None
    system_uptime_ms: int | None = None
    host: str | None = None


def create_app(config_path="./claws.yaml"):
    config = illusion_config.load(
        config_path,
        required=["claws.token", "claws.database_location"],
    )

    token = illusion_config.get(config, "claws.token")
    inventory = SpreadsheetManager(illusion_config.get(config, "claws.database_location"))
    events = EventBus()
    started_at = time.time()

    # Which services make up the fleet, from static config. The set is fixed, so
    # this is not service discovery -- and a static list is what lets `about`
    # survive a claws restart. An in memory registry alone would forget every
    # service that had already announced itself, and a kiosk idle since before
    # the restart would never re-announce.
    fleet = illusion_config.get(config, "claws.fleet", {}) or {}

    # service -> {payload, at}. Last successful contact, used to report an
    # assumed uptime when a service cannot be reached right now.
    last_seen = {}

    digikey = None

    if illusion_config.get(config, "claws.digikey.enabled"):
        digikey = DigiKeyClient(config_path, root="claws")

    async def require_token(authorization: str = Header(default="")):
        if authorization != f"Bearer {token}":
            raise HTTPException(status_code=401, detail="bad or missing bearer token")

    app = FastAPI(title="claws", version=VERSION)
    auth = [Depends(require_token)]

    def _item_or_404(sku):
        item = inventory.get_item(sku)

        if item is None:
            raise HTTPException(status_code=404, detail=f"no item with sku {sku}")

        return item

    def _announce(item, was_low):
        """Publish a low-stock transition, if one just happened.

        Keyed on the transition itself, never on whether a thread id happens to
        be set. Archiving is asynchronous now: the bot reacts to item.resolved,
        archives the thread and only then clears LOW_THREAD_ID. Any stock change
        landing in that gap would re-fire a thread-based check every time, so
        this compares before and after directly and fires exactly once.

        Derived here rather than trusted from the caller, because every path
        that changes stock must produce the same events and only this service
        sees both sides of the change.
        """
        if item["LOW"] and not was_low:
            events.publish({"event": "item.low", "sku": item["SKU"], "item": item})
            return "low"

        if was_low and not item["LOW"]:
            events.publish({"event": "item.resolved", "sku": item["SKU"], "item": item})
            return "resolved"

        return None

    # Unauthenticated so the fleet can report liveness even if a token is
    # rotated on one side only
    @app.get("/health")
    async def health():
        return {
            "service": "claws",
            "version": VERSION,
            "uptime_ms": service_uptime_ms(started_at),
            "system_uptime_ms": system_uptime_ms(),
            "items": len(inventory.read_all()),
        }

    async def _probe(name, url):
        try:
            async with httpx.AsyncClient(timeout=PROBE_TIMEOUT_S) as client:
                response = await client.get(f"{url.rstrip('/')}/health")
                response.raise_for_status()
                payload = response.json()
        except Exception:
            return _from_cache(name)

        last_seen[name] = {"payload": payload, "at": time.time()}

        return {"service": name, "state": "ok", **payload}

    def _from_cache(name):
        cached = last_seen.get(name)
        age_ms = 0 if cached is None else round((time.time() - cached["at"]) * 1000)

        return cached_entry(name, cached, age_ms)

    @app.post("/register", dependencies=auth)
    async def register(registration: Registration):
        """Announce on startup. Warms the cache so a restart shows up promptly.

        Not the source of truth for who exists -- that is the static fleet
        config -- just for what they last told us about themselves.
        """
        last_seen[registration.service] = {
            "payload": registration.model_dump(exclude_none=True),
            "at": time.time(),
        }

        return {"registered": registration.service}

    @app.get("/status", dependencies=auth)
    async def status():
        """The whole fleet in one call.

        Probes run concurrently, so the budget is the total rather than per
        service and one dead host cannot stretch `about` past a few seconds.
        """
        results = await asyncio.gather(
            *(_probe(name, url) for name, url in fleet.items()),
            return_exceptions=True,
        )

        services = [
            {"service": "claws", "state": "ok", **health_payload("claws", VERSION, started_at)}
        ]

        for name, result in zip(fleet, results):
            if isinstance(result, BaseException):
                services.append(_from_cache(name))
            else:
                services.append(result)

        return {"services": services}

    @app.get("/items", dependencies=auth)
    async def list_items():
        return inventory.read_all()

    @app.get("/low-threads", dependencies=auth)
    async def low_threads():
        """Every item whose low-stock thread state is worth looking at.

        The source of truth for which threads should exist. Returns the items
        that are low, or that still carry a thread id, each tagged with what
        needs doing:

          create   low, but no thread was ever opened
          archive  no longer low, but its thread is still open
          ok       low with a thread, which is the steady state

        The predicate lives here rather than in the bot because it is a
        statement about inventory, and because the bot is not the only thing
        that might ever want to ask.
        """
        rows = []

        for item in inventory.read_all():
            low = bool(item["LOW"])
            thread_id = item["LOW_THREAD_ID"]

            if not low and not thread_id:
                continue

            if low and not thread_id:
                action = "create"
            elif not low and thread_id:
                action = "archive"
            else:
                action = "ok"

            rows.append({"action": action, "item": item})

        return rows

    @app.get("/items/by-dkpn/{dkpn}", dependencies=auth)
    async def item_by_dkpn(dkpn: str):
        return inventory.get_item_by_dkpn(dkpn)

    @app.get("/items/{sku}", dependencies=auth)
    async def get_item(sku: str):
        return _item_or_404(sku)

    @app.post("/items", dependencies=auth)
    async def add_item(request: NewItem):
        sku = inventory.add_item(request.item)
        inventory.save()

        return {"sku": sku}

    @app.patch("/items/{sku}", dependencies=auth)
    async def update_item(sku: str, request: Updates):
        was_low = bool(_item_or_404(sku)["LOW"])

        inventory.update_item(sku, request.updates)
        inventory.save()

        item = inventory.get_item(sku)

        return {"item": item, "transition": _announce(item, was_low)}

    @app.delete("/items/{sku}", dependencies=auth)
    async def delete_item(sku: str):
        item = _item_or_404(sku)
        inventory.delete_item(sku)
        inventory.save()

        return item

    @app.post("/items/{sku}/decrease", dependencies=auth)
    async def decrease(sku: str, request: Amount):
        _item_or_404(sku)

        result = inventory.decrease_item(sku, request.amount)
        inventory.save()

        # decrease_item already reports the crossing, so this does not re-derive it
        if result["low_changed"]:
            events.publish({"event": "item.low", "sku": sku, "item": result["item"]})

        return {**result, "transition": "low" if result["low_changed"] else None}

    @app.post("/items/{sku}/increase", dependencies=auth)
    async def increase(sku: str, request: Amount):
        was_low = bool(_item_or_404(sku)["LOW"])

        item = inventory.increase_item(sku, float(request.amount or 1))
        inventory.save()

        return {"item": item, "transition": _announce(item, was_low)}

    @app.put("/items/{sku}/stock", dependencies=auth)
    async def set_stock(sku: str, request: Quantity):
        was_low = bool(_item_or_404(sku)["LOW"])

        item = inventory.set_stock(sku, request.quantity)
        inventory.save()

        return {"item": item, "transition": _announce(item, was_low)}

    @app.post("/items/{sku}/resolve", dependencies=auth)
    async def resolve(sku: str):
        item = _item_or_404(sku)

        if not item["LOW"]:
            return {"changed": False, "item": item}

        inventory.update_item(sku, {"LOW": "FALSE"})
        inventory.save()

        item = inventory.get_item(sku)
        events.publish({"event": "item.resolved", "sku": sku, "item": item})

        return {"changed": True, "item": item}

    # The bot owns thread ids; it writes them back through this one endpoint
    # rather than through the general update route, so the ownership is obvious
    @app.put("/items/{sku}/low-thread", dependencies=auth)
    async def set_low_thread(sku: str, request: LowThread):
        _item_or_404(sku)
        inventory.update_item(sku, {"LOW_THREAD_ID": request.thread_id})
        inventory.save()

        return inventory.get_item(sku)

    @app.get("/items/{sku}/tags", dependencies=auth)
    async def item_tags(sku: str):
        tags = inventory.get_item_tags(sku)

        if tags is None:
            raise HTTPException(status_code=404, detail=f"no item with sku {sku}")

        return tags

    @app.post("/items/{sku}/tags", dependencies=auth)
    async def add_tag(sku: str, request: TagRequest):
        _item_or_404(sku)

        return {"added": inventory.add_tag(sku, request.tag)}

    @app.post("/items/{sku}/vendors", dependencies=auth)
    async def add_vendor(sku: str, request: VendorRequest):
        _item_or_404(sku)

        return {"added": inventory.add_vendor(sku, request.vendor_name, request.link)}

    @app.get("/search", dependencies=auth)
    async def search(name: str, limit: int = 10):
        return inventory.search_items(name, limit=limit)

    @app.get("/tags", dependencies=auth)
    async def tags():
        return inventory.get_tags()

    @app.get("/tags/{tag}/items", dependencies=auth)
    async def items_by_tag(tag: str):
        return inventory.get_items_by_tag(tag)

    @app.post("/digikey/scan", dependencies=auth)
    async def digikey_scan(request: ScanRequest):
        if digikey is None:
            raise HTTPException(status_code=503, detail="DigiKey support is not enabled")

        try:
            data = await asyncio.to_thread(digikey.lookup_barcode, request.barcode)
        except Exception as e:
            raise HTTPException(status_code=502, detail=f"DigiKey lookup failed: {e}")

        return data

    @app.get("/digikey/part/{part_number:path}", dependencies=auth)
    async def digikey_part(part_number: str):
        if digikey is None:
            raise HTTPException(status_code=503, detail="DigiKey support is not enabled")

        try:
            return await asyncio.to_thread(digikey.lookup_part_number, part_number)
        except Exception as e:
            raise HTTPException(status_code=502, detail=f"DigiKey lookup failed: {e}")

    @app.get("/events", dependencies=auth)
    async def event_stream():
        subscription = events.subscribe()

        async def stream():
            try:
                while not subscription.dropped.is_set():
                    try:
                        event = await asyncio.wait_for(subscription.queue.get(), timeout=20)
                    except TimeoutError:
                        yield ": keepalive\n\n"
                        continue

                    yield f"data: {json.dumps(event, default=str)}\n\n"
            finally:
                events.unsubscribe(subscription)

        return StreamingResponse(stream(), media_type="text/event-stream")

    return app


def main():
    import os

    import uvicorn

    config_path = os.environ.get("CLAWS_CONFIG", "./claws.yaml")

    try:
        config = illusion_config.load(config_path)
    except illusion_config.ConfigError as e:
        print(e)
        raise SystemExit(1)

    uvicorn.run(
        create_app(config_path),
        host=illusion_config.get(config, "claws.bind.host", "127.0.0.1"),
        port=illusion_config.get(config, "claws.bind.port", 8080),
        log_level="info",
    )


if __name__ == "__main__":
    main()
