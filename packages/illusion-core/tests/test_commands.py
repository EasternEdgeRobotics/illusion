"""DB_Commands run end to end: through ClawsClient into a real claws app and database."""

import httpx
import pytest

from illusion_core.clients import ClawsClient
from illusion_core.commands import DB_Commands, DuplicateScan, Rows

from .conftest import FakeDigiKey, use_transport

pytestmark = pytest.mark.anyio


async def add(commands, name="Widget", **fields):
    message = await commands.handler_add_item(name, order_quantity=None, **fields)
    assert message.startswith("Added"), message
    return message.rsplit(" ", 1)[-1]


class TestAddItem:
    async def test_add(self, commands, claws):
        assert await commands.handler_add_item("Tape", "1 roll") == "Added Tape to inventory, SKU: EER-000001"
        assert (await claws.get_item("EER-000001"))["ORDER_QUANTITY"] == "1 roll"

    async def test_digikey_part_number_adds_a_vendor_and_must_be_unique(self, commands, claws):
        sku = await add(commands, digikey_part_number="296-1234-ND")

        assert "digikey.ca" in (await claws.get_item(sku))["LINK_1"]
        assert await commands.handler_add_item("Again", None, digikey_part_number="296-1234-ND") == (
            f"DKPN 296-1234-ND is already in use by {sku}"
        )

    async def test_claws_rejection_is_passed_through(self, commands):
        message = await commands.handler_add_item("A", None, tracking_mode="QUANTITY", quantity_on_hand=1.5)

        assert "whole number" in message


class TestInfo:
    async def test_short_skus_are_expanded(self, commands):
        await add(commands, "Tape")

        result = await commands.handler_info("1")

        assert isinstance(result, Rows)
        assert result.data["NAME"] == "Tape"

    async def test_kanban_hides_quantity(self, commands):
        kanban = await add(commands)
        counted = await add(commands, tracking_mode="QUANTITY", quantity_on_hand=3)

        assert "QUANTITY_ON_HAND" in (await commands.handler_info(kanban)).exclude
        assert "QUANTITY_ON_HAND" not in (await commands.handler_info(counted)).exclude
        assert (await commands.handler_info(kanban, hide_ext=False)).exclude == []

    async def test_missing(self, commands):
        assert await commands.handler_info("EER-999999") == "Invalid sku: EER-999999"


class TestStock:
    async def test_kanban_decrease(self, commands):
        sku = await add(commands)

        assert await commands.handler_decrease(sku) == f"{sku} marked as low, a low-stock thread is on its way"
        assert await commands.handler_decrease(sku) == f"{sku} already marked as low"

    async def test_quantity_decrease(self, commands):
        sku = await add(commands, tracking_mode="QUANTITY", quantity_on_hand=5, low_threshold=2)

        assert await commands.handler_decrease(sku, "2") == f"{sku} decreased by 2 units: 5 -> 3"
        assert (await commands.handler_decrease(sku, "1")).endswith("a low-stock thread is on its way")
        assert (await commands.handler_decrease(sku, "1")).endswith("Item is already marked as low.")

    async def test_decrease_by_nothing_is_refused_before_claws(self, commands):
        sku = await add(commands, tracking_mode="QUANTITY", quantity_on_hand=5)

        assert await commands.handler_decrease(sku, "0") == "Quantity must be greater than 0"

    async def test_increase_and_set_stock(self, commands):
        sku = await add(commands, tracking_mode="QUANTITY", quantity_on_hand=0, low_threshold=1)
        await commands.handler_set_stock(sku, "0")

        increased = await commands.handler_increase(sku, "4")
        assert increased.startswith(f"{sku} increased by 4 units. New stock: 4 units. Low: False")
        assert increased.endswith("the low-stock thread is being archived")

        assert (await commands.handler_set_stock(sku, "1")).endswith("a low-stock thread is on its way")

    async def test_resolve(self, commands):
        sku = await add(commands)

        assert await commands.handler_resolve(sku) == f"{sku} not marked as low"
        await commands.handler_decrease(sku)
        assert await commands.handler_resolve(sku) == f"{sku} no longer marked as low"

    async def test_missing_sku(self, commands):
        for handler in (commands.handler_decrease, commands.handler_resolve, commands.handler_delete_item):
            assert await handler("EER-999999") == "Invalid sku: EER-999999"


class TestUpdate:
    async def test_blank_fields_are_dropped(self, commands, claws):
        sku = await add(commands, "Old")

        assert await commands.handler_update_item(sku, {"NAME": None}) == "No updates provided."
        assert await commands.handler_update_item(sku, {"NAME": "New", "NOTES": None}) == f"Updated {sku}: NAME"
        assert (await claws.get_item(sku))["NAME"] == "New"

    async def test_digikey_part_number_adds_a_vendor(self, commands, claws):
        sku = await add(commands)
        await commands.handler_update_item(sku, {"DIGIKEY_PART_NUMBER": "296-1234-ND"})

        assert (await claws.get_item(sku))["VENDOR_1"] == "Digikey"

    async def test_missing_sku_gets_no_vendor(self, commands):
        assert await commands.handler_update_item("EER-999999", {"DIGIKEY_PART_NUMBER": "x"}) == "Invalid sku: EER-999999"


class TestTagsAndLocations:
    async def test_add_tags(self, commands, claws):
        sku = await add(commands, tags="M3")

        message = await commands.handler_add_tag(sku, "fasteners, m3, Fasteners, ")

        assert message == f"Added tag `fasteners` to {sku}\n{sku} already had `m3`"
        assert await claws.item_tags(sku) == ["M3", "fasteners"]

    async def test_search_tag(self, commands):
        await add(commands, "Screw", tags="fasteners")

        assert [row["NAME"] for row in (await commands.handler_search_tag("Fasteners")).data] == ["Screw"]
        assert await commands.handler_search_tag("nothing") == "No items found with tag: nothing"

    async def test_set_location_reports_the_stored_spelling(self, commands):
        sku = await add(commands)

        assert await commands.handler_set_location(sku, "5a") == f"{sku} is in Shelf 5A (Archive)"
        assert (await commands.handler_set_location(sku, "Kevin's desk")).endswith("not one of the known locations.")
        assert await commands.handler_set_location(sku, "") == f"Cleared the location of {sku}"

    async def test_search_location_with_a_slash(self, commands):
        await add(commands, "Scrap", location="4D: Acrylic / Polycarbonate Scrap")

        result = await commands.handler_search_location("Shelf 4D: Acrylic / Polycarbonate Scrap")

        assert [row["NAME"] for row in result.data] == ["Scrap"]


class TestDigiKeyScan:
    async def test_new_bag_then_the_same_bag_again(self, commands, claws):
        FakeDigiKey.barcodes["bag"] = {
            "DigiKeyPartNumber": "296-1234-ND", "Quantity": 25, "ProductDescription": "Op amp",
        }

        assert await commands.handler_digikey_scan("bag") == "New item EER-000001 created from 296-1234-ND with 25 on hand"

        repeat = await commands.handler_digikey_scan("bag")

        assert isinstance(repeat, DuplicateScan)
        assert "already been counted once" in repeat.message
        assert (await claws.get_item("EER-000001"))["QUANTITY_ON_HAND"] == 25

        counted = await commands.handler_digikey_scan("bag", force=True)

        assert counted.startswith("EER-000001 increased by 25 units. New stock: 50 units.")

    async def test_kanban_match_is_not_counted(self, commands):
        sku = await add(commands, digikey_part_number="296-1234-ND")
        FakeDigiKey.barcodes["bag"] = {"DigiKeyPartNumber": "296-1234-ND", "Quantity": 25}

        assert await commands.handler_digikey_scan("bag") == f"{sku} matched 296-1234-ND, but item is KANBAN tracked"

    async def test_barcode_without_a_part_number(self, commands):
        FakeDigiKey.barcodes["bag"] = {"Quantity": 25}

        assert await commands.handler_digikey_scan("bag") == "Barcode didn't contain a DigiKey part number"


async def test_unreachable_claws_is_a_message_not_a_traceback():
    def refuse(request):
        raise httpx.ConnectError("connection refused", request=request)

    claws = use_transport(ClawsClient("http://claws", "t"), httpx.MockTransport(refuse))
    commands = DB_Commands(claws, lipgloss=None, started_at=0)

    message = await commands.handler_decrease("1")

    assert message.startswith("Unable to reach claws.")
