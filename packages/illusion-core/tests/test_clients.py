"""BaseClient's error handling and retry rules, against a fake transport."""

import httpx
import pytest

from illusion_core.clients import ClawsClient, ServiceUnavailable, error_detail

from .conftest import use_transport

pytestmark = pytest.mark.anyio


def client_for(handler):
    calls = []

    def recording(request):
        calls.append(request)
        return handler(request)

    return use_transport(ClawsClient("http://claws/", "t"), httpx.MockTransport(recording)), calls


def flaky(request):
    """Refuses the first connection, then answers."""
    if not getattr(flaky, "refused", False):
        flaky.refused = True
        raise httpx.ConnectError("refused", request=request)

    return httpx.Response(200, json=[])


async def test_reads_are_retried():
    flaky.refused = False
    claws, calls = client_for(flaky)

    assert await claws.read_all() == []
    assert len(calls) == 2


async def test_stock_changes_are_never_retried():
    def refuse(request):
        raise httpx.ReadTimeout("timed out", request=request)

    claws, calls = client_for(refuse)

    with pytest.raises(ServiceUnavailable) as caught:
        await claws.decrease("EER-000001", 1)

    assert len(calls) == 1
    assert caught.value.status_code is None
    assert caught.value.service == "claws"


async def test_404_is_none_but_other_errors_raise():
    claws, _ = client_for(lambda request: httpx.Response(404, json={"detail": "no item"}))
    assert await claws.get_item("EER-000001") is None

    claws, _ = client_for(lambda request: httpx.Response(500, json={"detail": "database is locked"}))

    with pytest.raises(ServiceUnavailable, match="claws returned 500: database is locked") as caught:
        await claws.get_item("EER-000001")

    assert caught.value.status_code == 500


async def test_token_is_sent():
    claws, calls = client_for(lambda request: httpx.Response(200, json={}))
    await claws.health()

    assert calls[0].headers["Authorization"] == "Bearer t"


async def test_location_slashes_are_escaped():
    claws, calls = client_for(lambda request: httpx.Response(200, json=[]))
    await claws.items_by_location("Shelf 4D: Acrylic / Polycarbonate Scrap")

    assert calls[0].url.raw_path == b"/locations/Shelf%204D%3A%20Acrylic%20%2F%20Polycarbonate%20Scrap/items"


async def test_event_stream():
    body = b": keepalive\n\ndata: {\"event\": \"item.low\", \"sku\": \"EER-000001\"}\n\n"
    claws, _ = client_for(lambda request: httpx.Response(200, content=body))

    events = [event async for event in claws.events()]

    assert events == [{"event": "stream.connected"}, {"event": "item.low", "sku": "EER-000001"}]


async def test_event_stream_refused():
    claws, _ = client_for(lambda request: httpx.Response(401))

    with pytest.raises(ServiceUnavailable):
        async for _ in claws.events():
            pass


@pytest.mark.parametrize("response, expected", [
    (httpx.Response(400, json={"detail": "unknown style"}), "unknown style"),
    (httpx.Response(400, json=["not", "a", "dict"]), '["not","a","dict"]'),
    (httpx.Response(502, text="Bad Gateway"), "Bad Gateway"),
])
def test_error_detail(response, expected):
    assert error_detail(response) == expected
