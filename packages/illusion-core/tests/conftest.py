"""Fixtures for illusion-core: clients wired to in-process apps instead of the network."""

import httpx
import pytest
import yaml

from illusion_core.clients import ClawsClient
from illusion_core.commands import DB_Commands


def use_transport(client, transport):
    """Point a BaseClient at a transport, keeping its base url and token."""
    client._client = httpx.AsyncClient(
        transport=transport, base_url=client._url, headers=client._client.headers
    )
    return client


class FakeDigiKey:
    """Stands in for claws's DigiKeyClient. Tests fill `barcodes` with what to answer."""

    barcodes = {}

    def __init__(self, *args, **kwargs):
        pass

    def lookup_barcode(self, barcode):
        return self.barcodes[barcode]


@pytest.fixture
def anyio_backend():
    return "asyncio"


@pytest.fixture
def claws_app(tmp_path, monkeypatch):
    from claws import service

    monkeypatch.setattr(service, "DigiKeyClient", FakeDigiKey)
    monkeypatch.setattr(FakeDigiKey, "barcodes", {})

    path = tmp_path / "claws.yaml"
    path.write_text(yaml.safe_dump({"claws": {
        "token": "test-token",
        "database_location": str(tmp_path / "inventory.db"),
        "digikey": {"enabled": True},
    }}))

    return service.create_app(path)


@pytest.fixture
async def claws(claws_app):
    client = use_transport(
        ClawsClient("http://claws", "test-token"), httpx.ASGITransport(app=claws_app)
    )
    yield client
    await client.aclose()


@pytest.fixture
def commands(claws):
    return DB_Commands(claws, lipgloss=None, started_at=0)
