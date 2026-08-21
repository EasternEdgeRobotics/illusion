"""The shared command layer.

Both frontends run the same commands against the same services, so this is
where they live. It holds no rendering: handlers that produce tabular output
return a Rows carrying the data and which fields to leave out, and the caller
turns that into a terminal table or a Discord embed. That is the whole reason
this module can sit in illusion-core -- the moment it formatted an embed it
would drag discord back into a package that claws and lipgloss depend on.
"""

import functools
from dataclasses import dataclass, field

from illusion_core import helpers as illusion_helpers
from illusion_core.clients import ServiceUnavailable
from illusion_core.uptime import format_duration, service_uptime_ms, system_uptime_ms


@dataclass
class Rows:
    """Data for the caller to render, with the fields it should leave out."""

    data: object
    exclude: list = field(default_factory=list)


def reports_service_errors(handler):
    """Turn an unreachable service into a message instead of a traceback.

    Fail fast by design: nothing is buffered locally for a retry later, the
    caller is simply told plainly which service could not be reached. A scan
    made during an outage is not recorded, and says so.
    """
    @functools.wraps(handler)
    async def wrapper(*args, **kwargs):
        try:
            return await handler(*args, **kwargs)
        except ServiceUnavailable as e:
            return f"Unable to reach {e.service or 'a service'}.\n{e}"

    return wrapper



class DB_Commands:
    def __init__(self, claws, lipgloss, started_at):
        self.claws = claws
        self.lipgloss = lipgloss
        self.started_at = started_at

    @reports_service_errors
    async def handler_add_item(self, item_name, priority, order_quantity, tracking_mode="KANBAN", quantity_on_hand=None, 
                               low_threshold=None, unit=None, decrease_amount=None, vendor_1 = None, link_1 = None, 
                               vendor_2 = None, link_2 = None, vendor_3 = None, link_3 = None, 
                               vendor_4 = None, link_4 = None, vendor_5 = None, link_5 = None, 
                               digikey_part_number = None, tags = None, notes = None,): 
        # Digikey part numbers are unique, so we need to make sure that there isnt an existing item with the sane dkpn
        if digikey_part_number != None:
            digikey_test = await self.claws.item_by_dkpn(digikey_part_number)
            if digikey_test != None:
                return f"DKPN {digikey_part_number} is already in use by {digikey_test['SKU']}"

        new_item = {
            "NAME": item_name,
            "PRIORITY": priority,
            "ORDER_QUANTITY": order_quantity,
            "TRACKING_MODE": tracking_mode,
            "QUANTITY_ON_HAND": quantity_on_hand,
            "LOW_THRESHOLD": low_threshold,
            "LOW_THREAD_ID": None,
            "UNIT": unit,
            "DECREASE_AMOUNT": decrease_amount,
            "LINK_1": link_1,
            "VENDOR_1": vendor_1,
            "LINK_2": link_2,
            "VENDOR_2": vendor_2,
            "LINK_3": link_3,
            "VENDOR_3": vendor_3,
            "LINK_4": link_4,
            "VENDOR_4": vendor_4,
            "LINK_5": link_5,
            "VENDOR_5": vendor_5,
            "LOW": "FALSE",
            "DIGIKEY_PART_NUMBER": digikey_part_number,
            "TAGS": tags,
            "NOTES": notes,
        }
        
        new_sku = await self.claws.add_item(new_item)

        if digikey_part_number != None:
            digikey_link = f"https://www.digikey.ca/en/products/result?keywords={digikey_part_number}"
            await self.claws.add_vendor(new_sku, "Digikey", digikey_link)

        response_message = f"Added {item_name} to inventory, SKU: {new_sku}"
        return response_message

    @reports_service_errors
    async def handler_delete_item(self, sku):
        sku = illusion_helpers.clean_sku(sku)
        item = await self.claws.delete_item(sku)

        if item is None:
            return f"Invalid sku: {sku}"

        return f"Removed {item['NAME']} from inventory, SKU: {sku}"

    @reports_service_errors
    async def handler_info(self, sku, hide_ext=True):
        sku = illusion_helpers.clean_sku(sku)
        item = await self.claws.get_item(sku)

        if item is not None:
            if hide_ext:
                exclude = ["PRIORITY", "TRACKING_MODE", "LOW_THRESHOLD", "UNIT", "LOW_THREAD_ID", "DECREASE_AMOUNT", 
                            "VENDOR_1", "LINK_1", "VENDOR_2", "LINK_2", "VENDOR_3", "LINK_3", "VENDOR_4", "LINK_4", "VENDOR_5", "LINK_5"]

                if item["TRACKING_MODE"] == "KANBAN":
                    exclude.append("QUANTITY_ON_HAND")
            else:
                exclude = []

            return Rows(item, exclude)

        return f"Invalid sku: {sku}"

    @reports_service_errors
    async def handler_resolve(self, sku, archive_thread=False):
        sku = illusion_helpers.clean_sku(sku)
        result = await self.claws.resolve(sku)

        if result is None:
            return f"Invalid sku: {sku}"

        if not result["changed"]:
            return f"{sku} not marked as low"

        # The thread is archived by whoever is listening for item.resolved, so
        # archive_thread no longer gates anything here
        return f"{sku} no longer marked as low"

    @reports_service_errors
    async def handler_search(self, name: str):
        results = await self.claws.search(name, limit=10)

        if not results:
            return f"No items found matching: {name}"

        exclude = [
            "LINK_1",
            "VENDOR_1",
            "LINK_2",
            "VENDOR_2",
            "LINK_3",
            "VENDOR_3",
            "LINK_4",
            "VENDOR_4",
            "LINK_5",
            "VENDOR_5",
            "PRIORITY", 
            "LOW_THREAD_ID",
            "TRACKING_MODE", 
            "LOW_THRESHOLD", 
            "UNIT", 
            "DECREASE_AMOUNT",
            "ORDER_QUANTITY",
            "LOW",
            "NOTES",
            "TAGS",
        ]
        return Rows(results, exclude)

    @reports_service_errors
    async def handler_decrease(self, sku, amount=None):
        sku = illusion_helpers.clean_sku(sku)

        if amount != None and float(amount) <= 0:
            return f"Quantity must be greater than 0"

        result = await self.claws.decrease(sku, float(amount) if amount != None else None)

        if result is None:
            return f"Invalid sku: {sku}"

        item = result["item"]
        went_low = result["transition"] == "low"

        if item["TRACKING_MODE"] == "KANBAN":
            if went_low:
                return f"{sku} marked as low, a low-stock thread is on its way"

            return f"{sku} already marked as low"

        unit = item["UNIT"] or "units"

        response_message = (
            f"{sku} decreased by "
            f"{illusion_helpers.format_quantity(result['decrease_amount'])} {unit}: "
            f"{illusion_helpers.format_quantity(result['old_quantity'])} -> "
            f"{illusion_helpers.format_quantity(result['new_quantity'])}"
        )

        # The thread is created by whoever is listening for item.low, which may
        # not be this process, so its name is not available to report here
        if went_low:
            response_message += "\nLow threshold reached, a low-stock thread is on its way"
        elif item["LOW"]:
            response_message += "\nItem is already marked as low."

        return response_message

    @reports_service_errors
    async def handler_increase(self, sku, amount=1):
        sku = illusion_helpers.clean_sku(sku)

        result = await self.claws.increase(sku, float(amount))

        if result is None:
            return f"Invalid sku: {sku}"

        item = result["item"]
        unit = item["UNIT"] or "units"

        response_message = (
            f"{sku} increased by {illusion_helpers.format_quantity(amount)} {unit}. "
            f"New stock: {illusion_helpers.format_quantity(item['QUANTITY_ON_HAND'])} {unit}. "
            f"Low: {item['LOW']}"
        )

        if result["transition"] == "low":
            response_message += "\nLow threshold reached, a low-stock thread is on its way"
        elif result["transition"] == "resolved":
            response_message += "\nNo longer low, the low-stock thread is being archived"

        return response_message

    @reports_service_errors
    async def handler_set_stock(self, sku, quantity):
        sku = illusion_helpers.clean_sku(sku)

        result = await self.claws.set_stock(sku, float(quantity))

        if result is None:
            return f"Invalid sku: {sku}"

        item = result["item"]
        unit = item["UNIT"] or "units"

        response_message = (
            f"{sku} stock set to "
            f"{illusion_helpers.format_quantity(item['QUANTITY_ON_HAND'])} {unit}. "
            f"Low: {item['LOW']}"
        )

        if result["transition"] == "low":
            response_message += "\nLow threshold reached, a low-stock thread is on its way"
        elif result["transition"] == "resolved":
            response_message += "\nNo longer low, the low-stock thread is being archived"

        return response_message

    async def handler_generate_barcode(self, sku):
        return await self.lipgloss.render(style="classic_barcode", sku=sku, width=350, height=280, rotate=0)

    @reports_service_errors
    async def handler_print(self, style, sku = None, text_line_1 = None, text_line_2 = None, quantity = 1, reply_to = None, source = "terminal"):
        if sku != None:
            sku = illusion_helpers.clean_sku(sku)

        result = await self.lipgloss.print_label(
            style=style, sku=sku, line_1=text_line_1, line_2=text_line_2,
            copies=quantity, source=source, reply_to=reply_to,
        )

        return result["message"]

    @reports_service_errors
    async def handler_print_image(self, image_bytes, description, quantity = 1, reply_to = None, source = "terminal"):
        result = await self.lipgloss.print_image(
            image_bytes, description[:60], copies=quantity, source=source, reply_to=reply_to,
        )

        return result["message"]

    @reports_service_errors
    async def handler_bulk_print_niimbot(self, sku_lower, sku_upper, reply_to = None, source = "terminal"):
        try:
            lower = int(sku_lower)
            upper = int(sku_upper)
        except ValueError:
            return "Bulk print needs two sku numbers, ex: bulk_print 1 20"

        # The roll length check lives in lipgloss now, since only it can see the printer
        result = await self.lipgloss.print_barcodes(lower, upper, source=source, reply_to=reply_to)

        return result["message"]

    @reports_service_errors
    async def handler_printer_info(self):
        return await self.lipgloss.printer_info()

    @reports_service_errors
    async def handler_print_queue(self):
        return await self.lipgloss.queue()

    @reports_service_errors
    async def handler_print_resume(self):
        return await self.lipgloss.resume()

    @reports_service_errors
    async def handler_print_clear(self):
        return await self.lipgloss.clear()

    @reports_service_errors
    async def handler_print_cancel(self, job_id):
        try:
            job_id = int(job_id)
        except ValueError:
            return f"Invalid job id: {job_id}"

        return await self.lipgloss.cancel(job_id)

    @reports_service_errors
    async def handler_update_item(self, sku, updates: dict[str, object]):
        sku = illusion_helpers.clean_sku(sku)

        cleaned = {}

        for key, value in updates.items():
            if value != None:
                cleaned[key] = value

        updates = cleaned

        if not updates:
            return "No updates provided."

        result = await self.claws.update_item(sku, updates)

        if result is None:
            return f"Invalid sku: {sku}"

        # Automatically adds a digikey link if a digikey part number was added.
        # Only after the update lands, so an invalid sku does not leave a vendor
        # row behind on an item that was never touched.
        if updates.get("DIGIKEY_PART_NUMBER") is not None:
            digikey_link = f"https://www.digikey.ca/en/products/result?keywords={updates['DIGIKEY_PART_NUMBER']}"
            await self.claws.add_vendor(sku, "Digikey", digikey_link)

        changed_fields = ", ".join(updates.keys())

        return f"Updated {sku}: {changed_fields}"

    @reports_service_errors
    async def handler_digikey_scan(self, barcode_text: str):
        try:
            data = await self.claws.digikey_scan(barcode_text)
        except ServiceUnavailable as e:
            return f"DigiKey lookup failed: {e}"

        dkpn = data.get("DigiKeyPartNumber")
        quantity = data.get("Quantity") or 0
        description = data.get("ProductDescription")

        if not dkpn:
            return "Barcode didn't contain a DigiKey part number"

        existing = await self.claws.item_by_dkpn(dkpn)

        if existing is not None:
            sku = existing["SKU"]
            if existing["TRACKING_MODE"] == "KANBAN":
                return f"{sku} matched {dkpn}, but item is KANBAN tracked"
            if quantity > 0:
                return await self.handler_increase(sku, quantity)
            return f"{sku} matched {dkpn}, but barcode had no quantity"

        # New part: create a QUANTITY-tracked item pre-filled from DigiKey
        new_item = {
            "NAME": description or dkpn,
            "PRIORITY": 5,
            "ORDER_QUANTITY": None,
            "TRACKING_MODE": "QUANTITY",
            "QUANTITY_ON_HAND": quantity,
            "DECREASE_AMOUNT": 1,
            "DIGIKEY_PART_NUMBER": dkpn,
            "VENDOR_1": "DigiKey",
            "LINK_1": f"https://www.digikey.ca/en/products/result?keywords={dkpn}",
            "LOW": "FALSE",
            "TAGS": "per_item_tracking, digikey_scan, digikey",
            "NOTES": None,
        }

        new_sku = await self.claws.add_item(new_item)

        return f"New item {new_sku} created from {dkpn} with {quantity} on hand"

    @reports_service_errors
    async def handler_get_tags(self):
        tags = await self.claws.tags()

        if not tags:
            return "No tags found."

        return Rows(tags)

    @reports_service_errors
    async def handler_search_tag(self, tag):
        results = await self.claws.items_by_tag(tag)

        if not results:
            return f"No items found with tag: {tag}"

        exclude = [
            "LINK_1",
            "VENDOR_1",
            "LINK_2",
            "VENDOR_2",
            "LINK_3",
            "VENDOR_3",
            "LINK_4",
            "VENDOR_4",
            "LINK_5",
            "VENDOR_5",
            "PRIORITY",
            "LOW_THREAD_ID",
            "TRACKING_MODE",
            "LOW_THRESHOLD",
            "UNIT",
            "DECREASE_AMOUNT",
            "ORDER_QUANTITY",
            "LOW",
            "NOTES",
        ]

        return Rows(results, exclude)

    @reports_service_errors
    async def handler_add_tag(self, sku: str, tag: str):
        sku = illusion_helpers.clean_sku(sku)
        tag = tag.strip()

        if not tag:
            return "Tag cannot be empty."

        if "," in tag:
            return "Tag cannot contain commas."

        existing_tags = await self.claws.item_tags(sku)

        if existing_tags is None:
            return f"Invalid sku: {sku}"

        existing_keys = {existing_tag.casefold() for existing_tag in existing_tags}

        if tag.casefold() in existing_keys:
            return f"{sku} already has tag: {tag}"

        await self.claws.add_tag(sku, tag)

        return f"Added tag `{tag}` to {sku}"

    async def handler_uptime(self):
        """Both as human readable durations, computed on this machine's clock."""
        return (
            format_duration(service_uptime_ms(self.started_at)),
            format_duration(system_uptime_ms()),
        )
