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
    """The service could not be reached, or answered with an error."""


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
                    f"{self._name} returned {response.status_code}: {response.text[:200]}"
                )

            return response

        raise ServiceUnavailable(f"{self._name} unreachable at {self._url} ({last_error})")

    async def get(self, path, **kwargs):
        return (await self._request("GET", path, retry=True, **kwargs)).json()

    async def post(self, path, **kwargs):
        return (await self._request("POST", path, **kwargs)).json()

    async def delete(self, path, **kwargs):
        return (await self._request("DELETE", path, **kwargs)).json()

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
