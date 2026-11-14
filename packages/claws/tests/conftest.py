"""Fixtures for claws: a throwaway database and the app in front of it."""

import pytest
import yaml
from fastapi.testclient import TestClient

from claws import service
from claws.inventory_reader import SpreadsheetManager
from illusion_core.events import EventBus

TOKEN = "test-token"


class RecordingBus(EventBus):
    """An EventBus that also keeps everything published, for asserting on."""

    def __init__(self):
        super().__init__()
        self.published = []

    def publish(self, event):
        self.published.append(event)
        super().publish(event)


@pytest.fixture
def inventory(tmp_path):
    manager = SpreadsheetManager(tmp_path / "inventory.db")
    yield manager
    manager.close()


@pytest.fixture
def claws_config(tmp_path):
    path = tmp_path / "claws.yaml"
    path.write_text(yaml.safe_dump({
        "claws": {"token": TOKEN, "database_location": str(tmp_path / "inventory.db")},
    }))

    return path


@pytest.fixture
def events(monkeypatch):
    """The bus the next create_app builds, captured so tests can read it."""
    buses = []

    def make_bus():
        bus = RecordingBus()
        buses.append(bus)
        return bus

    monkeypatch.setattr(service, "EventBus", make_bus)

    class Published:
        @property
        def events(self):
            return buses[-1].published

        def names(self):
            return [event["event"] for event in self.events]

    return Published()


@pytest.fixture
def client(claws_config, events):
    app = service.create_app(claws_config)

    with TestClient(app, headers={"Authorization": f"Bearer {TOKEN}"}) as test_client:
        yield test_client


@pytest.fixture
def add(client):
    """POST an item and return its sku, failing loudly if claws rejected it."""
    def add_item(name="Widget", **fields):
        response = client.post("/items", json={"item": {"NAME": name, **fields}})
        response.raise_for_status()
        body = response.json()
        assert "sku" in body, body
        return body["sku"]

    return add_item
