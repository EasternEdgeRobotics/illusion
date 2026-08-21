"""HTTP clients for the illusion services.

Timeouts are short and deliberate. The kiosk reaches claws across the closet
wifi, and a request that either succeeds or fails cleanly in a few seconds is a
far better dependency than one that hangs. Only idempotent reads are retried:
a decrease that timed out may well have applied, so retrying it silently would
risk double counting.
"""

import json

import httpx

CONNECT_TIMEOUT = 5
READ_TIMEOUT = 15
READ_RETRIES = 2


class ServiceUnavailable(Exception):
    """The service could not be reached, or answered with an error.

    status_code is the HTTP status when the service did answer, and None when
    it could not be reached at all. Callers distinguish "no such item" from
    "the inventory service is down" on that, never on the message text.
    """

    def __init__(self, message, status_code=None, service=None):
        super().__init__(message)
        self.status_code = status_code
        self.service = service


class BaseClient:
    def __init__(self, url, token, name):
        self._url = url.rstrip("/")
        self._name = name
        self._client = httpx.AsyncClient(
            base_url=self._url,
            headers={"Authorization": f"Bearer {token}"} if token else {},
            timeout=httpx.Timeout(READ_TIMEOUT, connect=CONNECT_TIMEOUT),
        )

    async def aclose(self):
        await self._client.aclose()

    async def _request(self, method, path, retry=False, **kwargs):
        attempts = READ_RETRIES if retry else 1
        last_error = None

        for _ in range(attempts):
            try:
                response = await self._client.request(method, path, **kwargs)
            except httpx.HTTPError as e:
                last_error = e
                continue

            if response.status_code >= 400:
                raise ServiceUnavailable(
                    f"{self._name} returned {response.status_code}: {response.text[:200]}",
                    status_code=response.status_code,
                    service=self._name,
                )

            return response

        raise ServiceUnavailable(
            f"{self._name} unreachable at {self._url} ({last_error})", service=self._name
        )

    async def get(self, path, **kwargs):
        return (await self._request("GET", path, retry=True, **kwargs)).json()

    async def or_none(self, method, path, **kwargs):
        """None for a 404, but still raises if the service itself is unreachable."""
        try:
            return await self._json(method, path, **kwargs)
        except ServiceUnavailable as e:
            if e.status_code == 404:
                return None

            raise

    async def post(self, path, **kwargs):
        return (await self._request("POST", path, **kwargs)).json()

    async def delete(self, path, **kwargs):
        return (await self._request("DELETE", path, **kwargs)).json()

    async def _json(self, method, path, **kwargs):
        return (await self._request(method, path, **kwargs)).json()

    async def health(self):
        return await self.get("/health")

    async def events(self):
        """Yield events off the service's SSE stream until the connection drops."""
        async with self._client.stream("GET", "/events", timeout=None) as response:
            async for line in response.aiter_lines():
                if line.startswith("data: "):
                    yield json.loads(line[6:])


class LipglossClient(BaseClient):
    def __init__(self, url, token):
        super().__init__(url, token, "lipgloss")

    async def print_label(self, style, sku=None, line_1=None, line_2=None,
                          copies=1, source="unknown", reply_to=None):
        return await self.post("/print", json={
            "style": style, "sku": sku, "line_1": line_1, "line_2": line_2,
            "copies": copies, "source": source, "reply_to": reply_to,
        })

    async def print_barcodes(self, lower, upper, source="unknown", reply_to=None):
        return await self.post("/print/barcodes", json={
            "lower": lower, "upper": upper, "source": source, "reply_to": reply_to,
        })

    async def print_image(self, image_bytes, description, copies=1,
                          source="unknown", reply_to=None):
        return await self.post(
            "/print/image",
            files={"file": ("label.png", image_bytes, "image/png")},
            data={
                "description": description, "copies": str(copies),
                "source": source, **({"reply_to": reply_to} if reply_to else {}),
            },
        )

    async def render(self, style="classic_barcode", sku=None, line_1=None,
                     line_2=None, width=350, height=280, rotate=0):
        """Returns PNG bytes."""
        response = await self._request("POST", "/render", json={
            "style": style, "sku": sku, "line_1": line_1, "line_2": line_2,
            "width": width, "height": height, "rotate": rotate,
        })

        return response.content

    async def queue(self):
        return await self.get("/queue")

    async def printer_info(self):
        return (await self.get("/printer"))["message"]

    async def resume(self):
        return (await self.post("/queue/resume"))["message"]

    async def clear(self):
        return (await self.post("/queue/clear"))["message"]

    async def cancel(self, job_id):
        return (await self.delete(f"/queue/{job_id}"))["message"]


class ClawsClient(BaseClient):
    def __init__(self, url, token):
        super().__init__(url, token, "claws")

    async def get_item(self, sku):
        """None when there is no such item.

        Every mutating call below is also 404-tolerant, which folds away the old
        validate_sku round trip: checking existence and then acting used to be
        two database hits, and over the closet link two round trips per command
        is worth avoiding.
        """
        return await self.or_none("GET", f"/items/{sku}")

    async def read_all(self):
        return await self.get("/items")

    async def add_item(self, item):
        return (await self.post("/items", json={"item": item}))["sku"]

    async def update_item(self, sku, updates):
        return await self.or_none("PATCH", f"/items/{sku}", json={"updates": updates})

    async def delete_item(self, sku):
        return await self.or_none("DELETE", f"/items/{sku}")

    async def decrease(self, sku, amount=None):
        return await self.or_none("POST", f"/items/{sku}/decrease", json={"amount": amount})

    async def increase(self, sku, amount=1):
        return await self.or_none("POST", f"/items/{sku}/increase", json={"amount": amount})

    async def set_stock(self, sku, quantity):
        return await self.or_none("PUT", f"/items/{sku}/stock", json={"quantity": quantity})

    async def resolve(self, sku):
        return await self.or_none("POST", f"/items/{sku}/resolve")

    async def set_low_thread(self, sku, thread_id):
        return await self._json("PUT", f"/items/{sku}/low-thread", json={"thread_id": thread_id})

    async def add_tag(self, sku, tag):
        return await self.or_none("POST", f"/items/{sku}/tags", json={"tag": tag})

    async def add_vendor(self, sku, vendor_name, link):
        return (await self.post(
            f"/items/{sku}/vendors", json={"vendor_name": vendor_name, "link": link}
        ))["added"]

    async def search(self, name, limit=10):
        return await self.get("/search", params={"name": name, "limit": limit})

    async def tags(self):
        return await self.get("/tags")

    async def items_by_tag(self, tag):
        return await self.get(f"/tags/{tag}/items")

    async def low_threads(self):
        """Items whose low-stock thread state needs attention, tagged with the action."""
        return await self.get("/low-threads")

    async def item_tags(self, sku):
        """None when there is no such item, a list otherwise."""
        return await self.or_none("GET", f"/items/{sku}/tags")

    async def item_by_dkpn(self, dkpn):
        return await self.get(f"/items/by-dkpn/{dkpn}")

    async def digikey_part(self, part_number):
        return await self.get(f"/digikey/part/{part_number}")

    async def digikey_scan(self, barcode):
        return await self.post("/digikey/scan", json={"barcode": barcode})
