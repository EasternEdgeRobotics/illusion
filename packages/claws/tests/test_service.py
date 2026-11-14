"""claws's HTTP API: auth, the stock rules, and the events it publishes."""

import pytest
import yaml
from fastapi.testclient import TestClient

from claws import service
from illusion_core.config import ConfigError


class TestStartup:
    def test_missing_config_values_are_named(self, tmp_path):
        path = tmp_path / "claws.yaml"
        path.write_text(yaml.safe_dump({"claws": {"token": "x"}}))

        with pytest.raises(ConfigError, match="claws.database_location"):
            service.create_app(path)


class TestAuth:
    def test_requests_need_the_token(self, client):
        assert client.get("/items", headers={"Authorization": "Bearer wrong"}).status_code == 401
        assert client.get("/items", headers={"Authorization": ""}).status_code == 401

    def test_health_is_open(self, client):
        body = client.get("/health", headers={"Authorization": ""}).json()

        assert body["service"] == "claws"
        assert body["items"] == 0


class TestRules:
    def test_kanban_items_cannot_carry_a_count(self, client):
        body = client.post("/items", json={"item": {"NAME": "A", "QUANTITY_ON_HAND": 4}}).json()

        assert "KANBAN" in body["rejected"]

    def test_quantity_items_count_whole_units(self, client, add):
        body = client.post("/items", json={"item": {
            "NAME": "A", "TRACKING_MODE": "QUANTITY", "QUANTITY_ON_HAND": 2.5,
        }}).json()
        assert "whole number" in body["rejected"]

        sku = add(TRACKING_MODE="QUANTITY", QUANTITY_ON_HAND=5)
        assert "whole number" in client.post(f"/items/{sku}/decrease", json={"amount": 0.5}).json()["rejected"]
        assert "whole number" in client.put(f"/items/{sku}/stock", json={"quantity": 1.5}).json()["rejected"]

    def test_hybrid_items_take_fractions(self, client, add):
        sku = add(TRACKING_MODE="HYBRID", QUANTITY_ON_HAND=2.5)

        assert client.post(f"/items/{sku}/decrease", json={"amount": 0.25}).json()["new_quantity"] == 2.25

    def test_kanban_items_cannot_be_counted(self, client, add):
        sku = add()

        assert "KANBAN" in client.post(f"/items/{sku}/increase", json={"amount": 1}).json()["rejected"]
        assert "KANBAN" in client.put(f"/items/{sku}/stock", json={"quantity": 1}).json()["rejected"]

    def test_mode_change_is_checked_against_the_new_mode(self, client, add):
        sku = add(TRACKING_MODE="HYBRID", QUANTITY_ON_HAND=2.5)

        body = client.patch(f"/items/{sku}", json={"updates": {"TRACKING_MODE": "QUANTITY", "QUANTITY_ON_HAND": 2.5}}).json()

        assert "whole number" in body["rejected"]

    def test_lowercase_mode_is_still_checked(self, client, add):
        body = client.post("/items", json={"item": {
            "NAME": "A", "TRACKING_MODE": "quantity", "QUANTITY_ON_HAND": 2.5,
        }}).json()
        assert "whole number" in body["rejected"]

        body = client.post("/items", json={"item": {
            "NAME": "A", "TRACKING_MODE": " kanban", "QUANTITY_ON_HAND": 2,
        }}).json()
        assert "KANBAN" in body["rejected"]

        sku = add(TRACKING_MODE="HYBRID", QUANTITY_ON_HAND=2.5)
        body = client.patch(f"/items/{sku}", json={"updates": {"TRACKING_MODE": "quantity"}}).json()
        assert "item" in body

    @pytest.mark.parametrize("path", ["increase", "decrease"])
    @pytest.mark.parametrize("amount", [0, -2])
    def test_amount_must_be_positive(self, client, add, path, amount):
        sku = add(TRACKING_MODE="QUANTITY", QUANTITY_ON_HAND=5)

        body = client.post(f"/items/{sku}/{path}", json={"amount": amount}).json()

        assert body["rejected"] == "The amount has to be greater than 0."
        assert client.get(f"/items/{sku}").json()["QUANTITY_ON_HAND"] == 5

    def test_unknown_tracking_mode_is_refused(self, client, add):
        body = client.post("/items", json={"item": {"NAME": "A", "TRACKING_MODE": "bogus"}}).json()
        assert body["rejected"] == "TRACKING_MODE has to be one of KANBAN, QUANTITY, HYBRID, not BOGUS."

        sku = add()
        assert "rejected" in client.patch(f"/items/{sku}", json={"updates": {"TRACKING_MODE": "weight"}}).json()

    def test_stock_cannot_go_negative(self, client, add):
        sku = add(TRACKING_MODE="QUANTITY", QUANTITY_ON_HAND=5)

        assert client.put(f"/items/{sku}/stock", json={"quantity": -5}).json()["rejected"] == "Stock cannot be set below 0."
        assert client.put(f"/items/{sku}/stock", json={"quantity": 0}).json()["item"]["QUANTITY_ON_HAND"] == 0

    def test_increase_without_an_amount_adds_one(self, client, add):
        sku = add(TRACKING_MODE="QUANTITY", QUANTITY_ON_HAND=5)

        assert client.post(f"/items/{sku}/increase", json={}).json()["item"]["QUANTITY_ON_HAND"] == 6


class TestTransitions:
    def test_decrease_announces_low_once(self, client, add, events):
        sku = add(TRACKING_MODE="QUANTITY", QUANTITY_ON_HAND=2, LOW_THRESHOLD=1)

        assert client.post(f"/items/{sku}/decrease", json={}).json()["transition"] == "low"
        assert client.post(f"/items/{sku}/decrease", json={}).json()["transition"] is None
        assert events.names() == ["item.low"]
        assert events.events[0]["sku"] == sku

    def test_restock_announces_resolved(self, client, add, events):
        sku = add(TRACKING_MODE="QUANTITY", QUANTITY_ON_HAND=0)
        client.put(f"/items/{sku}/stock", json={"quantity": 0})

        assert client.post(f"/items/{sku}/increase", json={"amount": 5}).json()["transition"] == "resolved"
        assert events.names() == ["item.low", "item.resolved"]

    def test_set_stock_announces_both_ways(self, client, add, events):
        sku = add(TRACKING_MODE="QUANTITY", QUANTITY_ON_HAND=10, LOW_THRESHOLD=2)

        assert client.put(f"/items/{sku}/stock", json={"quantity": 1}).json()["transition"] == "low"
        assert client.put(f"/items/{sku}/stock", json={"quantity": 9}).json()["transition"] == "resolved"
        assert events.names() == ["item.low", "item.resolved"]

    def test_resolve(self, client, add, events):
        sku = add()

        assert client.post(f"/items/{sku}/resolve").json()["changed"] is False

        client.post(f"/items/{sku}/decrease", json={})
        body = client.post(f"/items/{sku}/resolve").json()

        assert body["changed"] is True
        assert body["item"]["LOW"] is False
        assert events.names() == ["item.low", "item.resolved"]

    def test_patching_low_announces(self, client, add, events):
        sku = add()

        assert client.patch(f"/items/{sku}", json={"updates": {"LOW": "TRUE"}}).json()["transition"] == "low"
        assert events.names() == ["item.low"]


class TestLowThreads:
    def test_actions(self, client, add):
        needs_thread = add("Needs thread")
        steady = add("Steady")
        to_archive = add("To archive")
        add("Fine")

        for sku in (needs_thread, steady, to_archive):
            client.post(f"/items/{sku}/decrease", json={})

        client.put(f"/items/{steady}/low-thread", json={"thread_id": 111})
        client.put(f"/items/{to_archive}/low-thread", json={"thread_id": 222})
        client.post(f"/items/{to_archive}/resolve")

        actions = {row["item"]["NAME"]: row["action"] for row in client.get("/low-threads").json()}

        assert actions == {"Needs thread": "create", "Steady": "ok", "To archive": "archive"}


class TestItems:
    def test_missing_items_are_404(self, client):
        for method, path in [
            ("GET", "/items/EER-999999"),
            ("DELETE", "/items/EER-999999"),
            ("POST", "/items/EER-999999/resolve"),
            ("GET", "/items/EER-999999/tags"),
        ]:
            assert client.request(method, path).status_code == 404, path

    def test_delete_returns_what_was_deleted(self, client, add):
        sku = add("Gone")

        assert client.delete(f"/items/{sku}").json()["NAME"] == "Gone"
        assert client.get(f"/items/{sku}").status_code == 404

    def test_location_reports_whether_it_is_known(self, client, add):
        sku = add()

        known = client.put(f"/items/{sku}/location", json={"location": "4a"}).json()
        unknown = client.put(f"/items/{sku}/location", json={"location": "Under Kevin's desk"}).json()

        assert (known["item"]["LOCATION"], known["known"]) == ("Shelf 4A", True)
        assert unknown["known"] is False

    def test_location_with_a_slash_in_the_path(self, client, add):
        add("Scrap", LOCATION="Shelf 4D: Acrylic / Polycarbonate Scrap")

        response = client.get("/locations/Shelf%204D%3A%20Acrylic%20%2F%20Polycarbonate%20Scrap/items")

        assert [item["NAME"] for item in response.json()] == ["Scrap"]

    def test_locations_suggest_is_not_taken_for_a_location(self, client):
        assert client.get("/locations/suggest", params={"query": "4a"}).json()[0]["LOCATION"] == "Shelf 4A"


class TestRename:
    def test_refuses_to_blank_a_name(self, client, add):
        add("Capasiter")

        body = client.post("/items/rename/apply", json={"find": "capasiter", "replace": ""}).json()

        assert "blank" in body["rejected"]
        assert client.get("/search", params={"name": "capasiter"}).json()[0]["NAME"] == "Capasiter"

    def test_apply_rematches(self, client, add):
        add("Capasiter")
        client.post("/items/rename/preview", json={"find": "capasiter", "replace": "Capacitor"})
        add("Capasiter 2")

        body = client.post("/items/rename/apply", json={"find": "capasiter", "replace": "Capacitor"}).json()

        assert len(body["changes"]) == 2

    def test_tag_rename_needs_both_sides(self, client):
        assert "rejected" in client.post("/tags/rename/apply", json={"find": "a", "replace": " "}).json()
        assert "rejected" in client.post("/tags/rename/preview", json={"find": "", "replace": "b"}).json()


class TestDigiKey:
    def test_disabled_is_503(self, client):
        assert client.post("/digikey/scan", json={"barcode": "x"}).status_code == 503

    def test_duplicate_bag_is_caught_before_digikey(self, claws_config, events, monkeypatch, add):
        config = yaml.safe_load(claws_config.read_text())
        config["claws"]["digikey"] = {"enabled": True}
        claws_config.write_text(yaml.safe_dump(config))

        lookups = []

        class FakeDigiKey:
            def __init__(self, *args, **kwargs):
                pass

            def lookup_barcode(self, barcode):
                lookups.append(barcode)
                return {"DigiKeyPartNumber": "296-1234-ND", "Quantity": 10}

        monkeypatch.setattr(service, "DigiKeyClient", FakeDigiKey)

        with TestClient(service.create_app(claws_config), headers={"Authorization": "Bearer test-token"}) as client:
            sku = client.post("/items", json={"item": {"NAME": "Chip"}}).json()["sku"]

            assert client.post("/digikey/scan", json={"barcode": "bag"}).json()["DigiKeyPartNumber"] == "296-1234-ND"

            client.post("/digikey/scans", json={"barcode": "bag", "sku": sku, "quantity": 10})

            assert client.post("/digikey/scan", json={"barcode": "bag"}).json()["duplicate"]["SKU"] == sku
            assert "DigiKeyPartNumber" in client.post("/digikey/scan", json={"barcode": "bag", "force": True}).json()
            assert lookups == ["bag", "bag"]


def test_status_reports_unreachable_services(claws_config, events):
    config = yaml.safe_load(claws_config.read_text())
    # Port 9 on loopback refuses straight away, so this does not wait on a timeout
    config["claws"]["fleet"] = {"lipgloss": "http://127.0.0.1:9"}
    claws_config.write_text(yaml.safe_dump(config))

    with TestClient(service.create_app(claws_config), headers={"Authorization": "Bearer test-token"}) as client:
        client.post("/register", json={"service": "lipgloss", "version": "1.6.0", "uptime_ms": 1000})

        services = {entry["service"]: entry for entry in client.get("/status").json()["services"]}

    assert services["claws"]["state"] == "ok"
    assert services["lipgloss"]["state"] == "assumed"
    assert services["lipgloss"]["uptime_ms"] >= 1000
