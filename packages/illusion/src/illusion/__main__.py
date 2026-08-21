import discord
from discord import app_commands
from discord.ext import commands
import asyncio
from importlib.metadata import version
from illusion_core import config as illusion_config
from illusion_core import helpers as illusion_helpers

from illusion import presentation
from PIL import Image
import collections
import functools
import os, io, platform, signal
import time
from datetime import timedelta
from illusion_core.clients import ClawsClient, LipglossClient, ServiceUnavailable

try:
    import readline
except:
    print("readline not installed")

# Mirrors lipgloss's own limit. Slash command ranges are evaluated when the
# decorators run at import time, so this has to be defined before them.
MAX_COPIES = 100

shutdown_event = asyncio.Event()

# bot.wait_until_ready() can return before on_ready has finished, and on_ready
# awaits a channel fetch partway through. Anything that needs the forum channel
# waits on this instead, so a reconcile at startup cannot race past it.
channel_ready = asyncio.Event()
shutdown_started = False

illusion_version = version("illusion")
intents = discord.Intents.default()

bot = commands.Bot(command_prefix="!", intents=intents, activity=discord.Game(name=f"illusion {illusion_version}"), status=discord.Status.online,)

boot_time = round(time.time() * 1000)

# Had to include at least 1 other reference
joanne_hat = r"""
      ▆▅▄▃▃▃▃▃▃▄▅▆      
      ▆▆▆▆▆▆▆▆▆▆▆▆      
     ▕░░░░░░░░░░░░▏     
 ▆▅▄▄▄▆▆▆▆▆▆▆▆▆▆▆▆▄▄▄▅▆ 
 ▆▆▆▆▆▆▆▆▆▆▆▆▆▆▆▆▆▆▆▆▆▆ 
  ▀▀▀▀▀▀▀▀▀▀▀▀▀▀▀▀▀▀▀▀ 
""".strip("\n")

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
    @reports_service_errors
    async def handler_add_item(self, item_name, priority, order_quantity, tracking_mode="KANBAN", quantity_on_hand=None, 
                               low_threshold=None, unit=None, decrease_amount=None, vendor_1 = None, link_1 = None, 
                               vendor_2 = None, link_2 = None, vendor_3 = None, link_3 = None, 
                               vendor_4 = None, link_4 = None, vendor_5 = None, link_5 = None, 
                               digikey_part_number = None, tags = None, notes = None,): 
        # Digikey part numbers are unique, so we need to make sure that there isnt an existing item with the sane dkpn
        if digikey_part_number != None:
            digikey_test = await claws.item_by_dkpn(digikey_part_number)
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
        
        new_sku = await claws.add_item(new_item)

        if digikey_part_number != None:
            digikey_link = f"https://www.digikey.ca/en/products/result?keywords={digikey_part_number}"
            await claws.add_vendor(new_sku, "Digikey", digikey_link)

        response_message = f"Added {item_name} to inventory, SKU: {new_sku}"
        return response_message
    
    @reports_service_errors
    async def handler_delete_item(self, sku):
        sku = illusion_helpers.clean_sku(sku)
        item = await claws.delete_item(sku)

        if item is None:
            return f"Invalid sku: {sku}"

        return f"Removed {item['NAME']} from inventory, SKU: {sku}"
    
    @reports_service_errors
    async def handler_info(self, sku, hide_ext=True, discord=False):
        sku = illusion_helpers.clean_sku(sku)
        item = await claws.get_item(sku)

        if item is not None:
            if hide_ext:
                exclude = ["PRIORITY", "TRACKING_MODE", "LOW_THRESHOLD", "UNIT", "LOW_THREAD_ID", "DECREASE_AMOUNT", 
                            "VENDOR_1", "LINK_1", "VENDOR_2", "LINK_2", "VENDOR_3", "LINK_3", "VENDOR_4", "LINK_4", "VENDOR_5", "LINK_5"]

                if item["TRACKING_MODE"] == "KANBAN":
                    exclude.append("QUANTITY_ON_HAND")
            else:
                exclude = []

            if discord:
                response_message = presentation.make_embed(item, exclude)
            else:
                response_message = illusion_helpers.make_table(item, exclude)
        else:
            response_message = f"Invalid sku: {sku}"
        
        return response_message
    
    @reports_service_errors
    async def handler_resolve(self, sku, archive_thread=False):
        sku = illusion_helpers.clean_sku(sku)
        result = await claws.resolve(sku)

        if result is None:
            return f"Invalid sku: {sku}"

        if not result["changed"]:
            return f"{sku} not marked as low"

        # The thread is archived by whoever is listening for item.resolved, so
        # archive_thread no longer gates anything here
        return f"{sku} no longer marked as low"

    @reports_service_errors
    async def handler_search(self, name: str, discord=False):
        results = await claws.search(name, limit=10)

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
        if discord:
            return presentation.make_embed(results, exclude=exclude)
        else:
            return illusion_helpers.make_table(results, exclude=exclude)
    
    @reports_service_errors
    async def handler_decrease(self, sku, amount=None):
        sku = illusion_helpers.clean_sku(sku)

        if amount != None and float(amount) <= 0:
            return f"Quantity must be greater than 0"

        result = await claws.decrease(sku, float(amount) if amount != None else None)

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

        result = await claws.increase(sku, float(amount))

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

        result = await claws.set_stock(sku, float(quantity))

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

    async def create_low_thread(self, sku, item=None):
        """Open a forum thread for an item that just went low.

        Driven by claws' item.low event rather than called inline by whichever
        command changed the stock: once the kiosk is its own process it has no
        Discord connection at all, so it cannot be the one to do this.
        """
        global channel

        # Belt and braces: the callers wait on channel_ready, but a thread
        # cannot be opened without somewhere to open it
        if channel is None:
            return None

        if item is None:
            item = await claws.get_item(sku)

        if item is None:
            return None

        thread_with_message = await channel.create_thread(
            name=f"{item['NAME']}: {item['SKU']}",
            content=presentation.make_low_thread_content(item),
            view=presentation.make_vendor_buttons(item),
        )

        await claws.set_low_thread(sku, thread_with_message.thread.id)

        return thread_with_message.thread.name

    async def archive_low_thread(self, sku, item=None):
        global bot

        # channel is only set once Discord is connected, so it doubles as the
        # check for whether there is any point looking a thread up
        if channel is None:
            return False, "Not connected to Discord."

        if item is None:
            item = await claws.get_item(sku)

        if item is None:
            return False, "No item found."

        thread_id = item.get("LOW_THREAD_ID")

        if not thread_id:
            return False, "No low-stock thread was stored for this item."

        try:
            thread = bot.get_channel(int(thread_id))

            if thread is None:
                thread = await bot.fetch_channel(int(thread_id))

        except discord.NotFound:
            # The pointer is cleared, so the drift is gone either way
            await claws.set_low_thread(sku, None)
            return True, "Stored thread no longer exists, cleared the reference."

        if not isinstance(thread, discord.Thread):
            return False, "Stored channel is not a thread."

        await thread.edit(
            archived=True,
            reason=f"{sku} resolved",
        )

        await claws.set_low_thread(sku, None)

        return True, "Low-stock thread archived."

    async def handler_generate_barcode(self, sku):
        return await lipgloss.render(style="classic_barcode", sku=sku, width=350, height=280, rotate=0)

    @reports_service_errors
    async def handler_print(self, style, sku = None, text_line_1 = None, text_line_2 = None, quantity = 1, reply_to = None, source = "terminal"):
        if sku != None:
            sku = illusion_helpers.clean_sku(sku)

        result = await lipgloss.print_label(
            style=style, sku=sku, line_1=text_line_1, line_2=text_line_2,
            copies=quantity, source=source, reply_to=reply_to,
        )

        return result["message"]

    @reports_service_errors
    async def handler_print_image(self, image_bytes, description, quantity = 1, reply_to = None, source = "terminal"):
        result = await lipgloss.print_image(
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
        result = await lipgloss.print_barcodes(lower, upper, source=source, reply_to=reply_to)

        return result["message"]

    @reports_service_errors
    async def handler_printer_info(self):
        return await lipgloss.printer_info()

    @reports_service_errors
    async def handler_print_queue(self, discord=False):
        status = await lipgloss.queue()

        if discord:
            return presentation.queue_embed(status)

        return presentation.queue_text(status)

    @reports_service_errors
    async def handler_print_resume(self):
        return await lipgloss.resume()

    @reports_service_errors
    async def handler_print_clear(self):
        return await lipgloss.clear()

    @reports_service_errors
    async def handler_print_cancel(self, job_id):
        try:
            job_id = int(job_id)
        except ValueError:
            return f"Invalid job id: {job_id}"

        return await lipgloss.cancel(job_id)

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

        result = await claws.update_item(sku, updates)

        if result is None:
            return f"Invalid sku: {sku}"

        # Automatically adds a digikey link if a digikey part number was added.
        # Only after the update lands, so an invalid sku does not leave a vendor
        # row behind on an item that was never touched.
        if updates.get("DIGIKEY_PART_NUMBER") is not None:
            digikey_link = f"https://www.digikey.ca/en/products/result?keywords={updates['DIGIKEY_PART_NUMBER']}"
            await claws.add_vendor(sku, "Digikey", digikey_link)

        changed_fields = ", ".join(updates.keys())

        return f"Updated {sku}: {changed_fields}"
    
    @reports_service_errors
    async def handler_digikey_scan(self, barcode_text: str):
        try:
            data = await claws.digikey_scan(barcode_text)
        except ServiceUnavailable as e:
            return f"DigiKey lookup failed: {e}"

        dkpn = data.get("DigiKeyPartNumber")
        quantity = data.get("Quantity") or 0
        description = data.get("ProductDescription")

        if not dkpn:
            return "Barcode didn't contain a DigiKey part number"

        existing = await claws.item_by_dkpn(dkpn)

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

        new_sku = await claws.add_item(new_item)

        return f"New item {new_sku} created from {dkpn} with {quantity} on hand"

    @reports_service_errors
    async def handler_get_tags(self, discord=False):
        tags = await claws.tags()

        if not tags:
            return "No tags found."

        if discord:
            return presentation.make_embed(tags)
        else:
            return illusion_helpers.make_table(tags)

    @reports_service_errors
    async def handler_search_tag(self, tag, discord=False):
        results = await claws.items_by_tag(tag)

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

        if discord:
            return presentation.make_embed(results, exclude=exclude)
        else:
            return illusion_helpers.make_table(results, exclude=exclude)

    @reports_service_errors
    async def handler_add_tag(self, sku: str, tag: str):
        sku = illusion_helpers.clean_sku(sku)
        tag = tag.strip()

        if not tag:
            return "Tag cannot be empty."

        if "," in tag:
            return "Tag cannot contain commas."

        existing_tags = await claws.item_tags(sku)

        if existing_tags is None:
            return f"Invalid sku: {sku}"

        existing_keys = {existing_tag.casefold() for existing_tag in existing_tags}

        if tag.casefold() in existing_keys:
            return f"{sku} already has tag: {tag}"

        await claws.add_tag(sku, tag)

        return f"Added tag `{tag}` to {sku}"

    async def handler_uptime(self):
        def format(uptime):
            td = timedelta(milliseconds=uptime)
            
            days = td.days
            hours, remainder = divmod(td.seconds, 3600)
            minutes, seconds = divmod(remainder, 60)
    
            parts = []
    
            if days:
                parts.append(f"{days}d")
            if hours:
                parts.append(f"{hours}h")
            if minutes:
                parts.append(f"{minutes}m")
            if seconds or not parts:
                parts.append(f"{seconds}s")

            return " ".join(parts)
        
        current_time = round(time.time() * 1000)
        bot_uptime_raw = current_time - boot_time
        bot_uptime = format(bot_uptime_raw)

        if platform.system() == "Linux":
            with open("/proc/uptime", "r") as f:
                system_uptime_raw = float(f.readline().split()[0]) * 1000
                system_uptime = format(system_uptime_raw)
        else:
            system_uptime = "Unknown"
            
        return bot_uptime, system_uptime

    async def handler_command_help(self):
        command_list = [
            {
                "COMMAND": "about",
                "USAGE": "about",
                "DESCRIPTION": "Info about illusion",
            },
            {
                "COMMAND": "exit",
                "USAGE": "exit",
                "DESCRIPTION": "Exit illusion",
            },
            {
                "COMMAND": "resolve",
                "USAGE": "resolve <sku>",
                "DESCRIPTION": "Mark an item as not low",
            },
            {
                "COMMAND": "delete",
                "USAGE": "delete <sku>",
                "DESCRIPTION": "Delete an item",
            },
            {
                "COMMAND": "info",
                "USAGE": "info <sku>",
                "DESCRIPTION": "Get info about an item",
            },
            {
                "COMMAND": "search",
                "USAGE": "search <item name>",
                "DESCRIPTION": "Search for items",
            },
            {
                "COMMAND": "get_tags",
                "USAGE": "get_tags",
                "DESCRIPTION": "List all item tags",
            },
            {
                "COMMAND": "add_tag",
                "USAGE": "add_tag <sku> <tag>",
                "DESCRIPTION": "Add a tag to an item",
            },
            {
                "COMMAND": "increase",
                "USAGE": "increase <sku> [amount]",
                "DESCRIPTION": "Increase item stock",
            },
            {
                "COMMAND": "decrease",
                "USAGE": "decrease <sku> [amount]",
                "DESCRIPTION": "Decrease item stock",
            },
            {
                "COMMAND": "set",
                "USAGE": "set <sku> <quantity>",
                "DESCRIPTION": "Set item stock",
            },
        ]

        if config["illusion"]["printer"]["niimbot"]["enabled"]:
            command_list.extend(
                [
                    {
                        "COMMAND": "print_barcode",
                        "USAGE": "print <sku>",
                        "DESCRIPTION": "Print a barcode with the printer",
                    },
                    {
                        "COMMAND": "printer_info",
                        "USAGE": "printer_info",
                        "DESCRIPTION": "Get info about the printer",
                    },
                    {
                        "COMMAND": "print_label",
                        "USAGE": 'print_label <line 1> ["line 2"]',
                        "DESCRIPTION": "Print a label with the specified text",
                    },
                    {
                        "COMMAND": "bulk_print",
                        "USAGE": 'bulk_print [lower sku] [upper sku]',
                        "DESCRIPTION": "Print barcodes for a range of skus",
                    },
                    {
                        "COMMAND": "print_queue",
                        "USAGE": "print_queue",
                        "DESCRIPTION": "Show the print queue",
                    },
                    {
                        "COMMAND": "print_resume",
                        "USAGE": "print_resume",
                        "DESCRIPTION": "Resume the print queue after fixing the printer",
                    },
                    {
                        "COMMAND": "print_cancel",
                        "USAGE": "print_cancel <job id>",
                        "DESCRIPTION": "Cancel a queued print job",
                    },
                    {
                        "COMMAND": "print_clear",
                        "USAGE": "print_clear",
                        "DESCRIPTION": "Clear every job from the print queue",
                    },
                ]
            )

        return f"\n<sku> required argument\n[amount] optional argument\n\n{illusion_helpers.make_table(command_list)}\n"

TERMINAL_REPLY_TO = "kiosk"


def terminal_print(message):
    # The input prompt has no trailing newline, so anything printed from the
    # background lands on top of it, reprint it to keep the input line intact
    print(f"\n{message}\n> ", end="", flush=True)

async def terminal_notify(event):
    # The terminal gets the plain text; the title and embed are for discord
    terminal_print(event["message"])

async def terminal_loop():
    await bot.wait_until_ready()

    register_notifier(TERMINAL_REPLY_TO, terminal_notify)

    print(f"illusion {illusion_version}")
    print("ready")

    while not bot.is_closed() and not shutdown_event.is_set():        
        try:
            text = await asyncio.to_thread(input, "> ")
        except EOFError:
            await graceful_exit("terminal EOF")
            break
        except Exception as e:
            print(f"Terminal input error: {e}")
            await asyncio.sleep(1)
            continue

        text = text.strip()

        if not text:
            continue

        parts = text.split(maxsplit=2) # Make sure to update this if commands w/ 3+ fields are added
        command = parts[0].lower()
        response_message = None

        try:
            if command == "exit" and len(parts) >= 1:
                response_message = "Exiting"
                print(response_message)
                await graceful_exit("terminal exit")
                break

            elif command == "help" and len(parts) >= 1:
                response_message = await command_handler.handler_command_help()

            elif command == "about" and len(parts) >= 1:
                bot_uptime, system_uptime = await command_handler.handler_uptime()
                text = f"""illusion \nversion: {illusion_version}\nbot uptime: {bot_uptime}\nsystem uptime: {system_uptime}""".strip("\n")
            
                hat_lines = joanne_hat.splitlines()
                text_lines = text.splitlines()

                hat_width = max(len(line) for line in hat_lines)
                gap = 4

                for i in range(len(hat_lines)):
                    if len(text_lines) > i:
                        if i != 2:
                            print(f"\033[38;2;192;140;149m{hat_lines[i].ljust(hat_width + gap)}\033[0m{text_lines[i]}")
                        else:
                            print(f"\033[38;2;230;222;208m{hat_lines[i].ljust(hat_width + gap)}\033[0m{text_lines[i]}")
                    else:
                        if i != 2:
                            print(f"\033[38;2;192;140;149m{hat_lines[i].ljust(hat_width + gap)}\033[0m")
                        else:
                            print(f"\033[38;2;230;222;208m{hat_lines[i].ljust(hat_width + gap)}\033[0m")
            
                response_message = ""

            elif command == "get_tags" and len(parts) >= 1:
                response_message = await command_handler.handler_get_tags()
            elif command == "add_tag" and len(parts) == 3:
                response_message = await command_handler.handler_add_tag(parts[1], parts[2])
            elif parts[0].startswith("EER-") and len(parts) >= 1: # Basic bar code scanner support
                response_message = await command_handler.handler_decrease(parts[0])
            elif text.startswith("[)>") or (text.isdigit() and len(text) > 8): # Digikey data matrix
                response_message = await command_handler.handler_digikey_scan(text.strip().replace("|", "\u241d"))
            elif command == "resolve" and len(parts) >= 2:
                response_message = await command_handler.handler_resolve(parts[1])
            elif command == "delete" and len(parts) >= 2:
                response_message = await command_handler.handler_delete_item(parts[1])
            elif command == "info" and len(parts) >= 2:
                response_message = await command_handler.handler_info(parts[1])
            elif command == "search" and len(parts) >= 2:
                response_message = await command_handler.handler_search(parts[1])
            elif command == "decrease" and len(parts) >= 2:
                if len(parts) == 3:
                    response_message = await command_handler.handler_decrease(parts[1], parts[2])
                else:
                    response_message = await command_handler.handler_decrease(parts[1])
            elif command == "increase" and len(parts) >= 2:
                if len(parts) == 3:
                    response_message = await command_handler.handler_increase(parts[1], parts[2])
                else:
                    response_message = await command_handler.handler_increase(parts[1])
            elif command == "print_barcode" and config["illusion"]["printer"]["niimbot"]["enabled"] and len(parts) >= 2:
                if len(parts) != 3:
                    style = "slim_barcode"
                    sku = parts[1]
                    line_1 = None
                else:
                    style = "label_2_line"
                    sku = parts[1]
                    line_1 = parts[2]

                response_message = await command_handler.handler_print(style=style, text_line_1=line_1, sku=sku, reply_to=TERMINAL_REPLY_TO)
            elif command == "printer_info" and config["illusion"]["printer"]["niimbot"]["enabled"] and len(parts) >= 1:
                response_message = await command_handler.handler_printer_info()
            elif command == "print_label" and config["illusion"]["printer"]["niimbot"]["enabled"] and len(parts) >= 2:
                # Awful, Awful, Awful
                # I hate this code
                # Can't be replaced by shlex without breaking non qouted strings
                if len(parts) == 3:
                    cleaned_text = text.replace("print_label ", "")
                    if '"' in cleaned_text:
                        lines = cleaned_text.split('"')
                        if len(lines) >= 4:
                            line_1 = lines[1]
                            line_2 = lines[3] # Why did i flip this order before????????? -PC
                            style = "label_2_line"
                        else:
                            response_message = "Invalid Qoutes"
                    else:
                        line_1 = f"{parts[1]} {parts[2]}"
                        line_2 = None
                        style = "label_1_line"
                else:
                    line_1 = parts[1]
                    style = "label_1_line"
                    line_2 = None
            
                if response_message == None:
                    response_message = await command_handler.handler_print(style=style, text_line_1=line_1, text_line_2=line_2, reply_to=TERMINAL_REPLY_TO)
            elif command == "bulk_print" and config["illusion"]["printer"]["niimbot"]["enabled"] and len(parts) == 3:
                response_message = await command_handler.handler_bulk_print_niimbot(parts[1], parts[2], reply_to=TERMINAL_REPLY_TO)
            elif command == "print_queue" and config["illusion"]["printer"]["niimbot"]["enabled"] and len(parts) >= 1:
                response_message = await command_handler.handler_print_queue()
            elif command == "print_resume" and config["illusion"]["printer"]["niimbot"]["enabled"] and len(parts) >= 1:
                response_message = await command_handler.handler_print_resume()
            elif command == "print_clear" and config["illusion"]["printer"]["niimbot"]["enabled"] and len(parts) >= 1:
                response_message = await command_handler.handler_print_clear()
            elif command == "print_cancel" and config["illusion"]["printer"]["niimbot"]["enabled"] and len(parts) >= 2:
                response_message = await command_handler.handler_print_cancel(parts[1])
            elif command == "set" and len(parts) == 3:
                response_message = await command_handler.handler_set_stock(parts[1], parts[2])
            else:
                response_message = f"Invalid Command: {command}\n\nHelp:{await command_handler.handler_command_help()}"
        except ServiceUnavailable as e:
            # Fail fast: the command is simply not applied, and says so
            response_message = f"Service unavailable, command not applied.\n{e}"
        except Exception as e:
            # One bad command must never take the terminal down with it. The
            # kiosk is the only way to touch inventory from the closet, and a
            # dead prompt there means someone has to go find a keyboard.
            response_message = f"Command failed: {e}"

        if response_message != None:
            print(response_message)


@bot.event
async def on_ready():

    global channel
    print(f"Logged in as {bot.user}")

    guild = discord.Object(id=GUILD_ID)

    channel = bot.get_channel(FORUM_CHANNEL_ID)

    if channel is None:
        channel = await bot.fetch_channel(FORUM_CHANNEL_ID)

    if not isinstance(channel, discord.ForumChannel):
        print("That channel is not a forum channel")
        await bot.close()
        return

    bot.tree.copy_global_to(guild=guild)
    await bot.tree.sync(guild=guild)

    channel_ready.set()



@bot.tree.command(name="ping", description="Check bot latency")
async def ping(interaction: discord.Interaction):
    latency = round(bot.latency * 1000)
    await interaction.response.send_message(f"Pong! `{latency}ms`")

@bot.tree.command(name="about", description="About illusion")
async def about(interaction: discord.Interaction):
    bot_uptime, system_uptime = await command_handler.handler_uptime()
    await interaction.response.send_message(f"illusion\nversion: {illusion_version}\nbot uptime: {bot_uptime}\nsystem uptime: {system_uptime}")

@bot.tree.command(name="resolve", description="Mark low stock warnings as resolved")
@app_commands.describe(sku="Item Sku")
async def resolve(interaction: discord.Interaction, sku: str | None = None):
    channel = interaction.channel

    if not isinstance(channel, discord.Thread) and sku == None:
        await interaction.response.send_message(
            "This command requires a sku if you aren't inside a low-stock thread.",
            ephemeral=True,
        )
        return
    elif isinstance(channel, discord.Thread) and sku == None:
        sku = channel.name.split(": ")[1]
    
    response_message = await command_handler.handler_resolve(sku, False)
    await interaction.response.send_message(response_message)

    cleaned_sku = illusion_helpers.clean_sku(sku)
    await command_handler.archive_low_thread(cleaned_sku)

@bot.tree.command(name="set_stock", description="Set current stock")
@app_commands.describe(sku="Item Sku", value="Stock amount")
async def set_stock(interaction: discord.Interaction, sku: str, value: str):
    response_message = await command_handler.handler_set_stock(sku, value)
    await interaction.response.send_message(response_message)

@bot.tree.command(name="decrease", description="Decrease current stock")
@app_commands.describe(sku="Item Sku", amount="Amount to decrease by")
async def decrease(interaction: discord.Interaction, sku: str, amount: str | None = "1"):
    response_message = await command_handler.handler_decrease(sku, amount)
    await interaction.response.send_message(response_message)

@bot.tree.command(name="increase", description="Increase current stock")
@app_commands.describe(sku="Item Sku", amount="Amount to increase by")
async def increase(interaction: discord.Interaction, sku: str, amount: str | None = "1"):
    response_message = await command_handler.handler_increase(sku, amount)
    await interaction.response.send_message(response_message)

@bot.tree.command(name="info", description="Get info about an item")
@app_commands.describe(sku="Item Sku", hide_ext="Show or hide extra values")
async def info(interaction: discord.Interaction, sku: str, hide_ext: bool = True):
    await interaction.response.defer()
    cleaned_sku = illusion_helpers.clean_sku(sku)
    item = await claws.get_item(cleaned_sku)

    if item is None:
        await interaction.followup.send("Invalid sku")
        return

    response_message = await command_handler.handler_info(sku, hide_ext, discord=True)
    view = presentation.make_vendor_buttons(item)
    if view != None:
        await interaction.followup.send(embed=response_message, view=view,)
    else:
        await interaction.followup.send(embed=response_message,)

@bot.tree.command(name="delete", description="Delete an item")
@app_commands.describe(sku="Item Sku")
async def delete(interaction: discord.Interaction, sku: str):
    response_message = await command_handler.handler_delete_item(sku)
    await interaction.response.send_message(response_message)

@bot.tree.command(name="add_item", description="Add item to inventory w/ per unit tracking")
@app_commands.describe(item_name="Item Name",
                       priority="Item Priority, 1-10",
                       order_quantity="Number of units to order when stock low", unit="Unit name",
                       quantity="Number of units on hand", low_threshold="Minimum Stock", digikey_part_number="Digikey Part Number",
                       vendor_1="Source 1 for Item", link_1="Source 1 Purchase Link",
                       vendor_2="Source 2 for Item", link_2="Source 2 Purchase Link",
                       vendor_3="Source 3 for Item", link_3="Source 3 Purchase Link",
                       vendor_4="Source 4 for Item", link_4="Source 4 Purchase Link",
                       vendor_5="Source 5 for Item", link_5="Source 5 Purchase Link",
                       tags="Comma-separated tags", notes="Notes about this item",
                       )

async def add_item(interaction: discord.Interaction, item_name: str, priority: int, 
                   quantity: float, order_quantity: float, low_threshold: float, unit: str, 
                   digikey_part_number: str | None = None, tags: str | None = None, notes: str | None = None,
                   vendor_1: str | None = None, link_1: str | None = None, vendor_2: str | None = None, link_2: str | None = None, 
                   vendor_3: str | None = None, link_3: str | None = None, vendor_4: str | None = None, 
                   link_4: str | None = None, vendor_5: str | None = None, link_5: str | None = None):

    if tags == None:
        tags = "per_item_tracking"
    else:
        tags = f"per_item_tracking, {tags}"

    response_message = await command_handler.handler_add_item(item_name, priority, order_quantity, "QUANTITY", quantity, low_threshold, unit, "1", vendor_1, link_1, 
                                                              vendor_2, link_2, vendor_3, link_3, vendor_4, link_4, vendor_5, link_5, digikey_part_number, tags, notes,)

    await interaction.response.send_message(response_message)

@bot.tree.command(name="add_kanban", description="Add item to inventory w/ kanban tracking")
@app_commands.describe(item_name="Item Name",
                       priority="Item Priority, 1-10",
                       order_quantity="Number of units to order when stock low", digikey_part_number="Digikey Part Number",
                       vendor_1="Source 1 for Item", link_1="Source 1 Purchase Link",
                       vendor_2="Source 2 for Item", link_2="Source 2 Purchase Link",
                       vendor_3="Source 3 for Item", link_3="Source 3 Purchase Link",
                       vendor_4="Source 4 for Item", link_4="Source 4 Purchase Link",
                       vendor_5="Source 5 for Item", link_5="Source 5 Purchase Link",
                       tags="Comma-separated tags", notes="Notes about this item",
                       )

async def add_kanban(interaction: discord.Interaction, item_name: str, priority: int, order_quantity: float, 
                     digikey_part_number: str | None = None, tags: str | None = None, notes: str | None = None,
                   vendor_1: str | None = None, link_1: str | None = None, vendor_2: str | None = None, link_2: str | None = None, 
                   vendor_3: str | None = None, link_3: str | None = None, vendor_4: str | None = None, 
                   link_4: str | None = None, vendor_5: str | None = None, link_5: str | None = None):

    if tags == None:
        tags = "kanban_tracking"
    else:
        tags = f"kanban_tracking, {tags}"

    response_message = await command_handler.handler_add_item(item_name, priority, order_quantity, "KANBAN", None, None, None, None, vendor_1, link_1, 
                                                              vendor_2, link_2, vendor_3, link_3, vendor_4, link_4, vendor_5, link_5, digikey_part_number, tags, notes,)

    await interaction.response.send_message(response_message)

@bot.tree.command(name="add_hybrid", description="Add item to inventory w/ hybrid tracking")
@app_commands.describe(item_name="Item Name",
                       priority="Item Priority, 1-10",
                       order_quantity="Number of units to order when stock low", unit="Unit name", digikey_part_number="Digikey Part Number",
                       quantity="Number of units on hand", low_threshold="Minimum Stock", decrease_amount="Amount to decrease by",
                       vendor_1="Source 1 for Item", link_1="Source 1 Purchase Link",
                       vendor_2="Source 2 for Item", link_2="Source 2 Purchase Link",
                       vendor_3="Source 3 for Item", link_3="Source 3 Purchase Link",
                       vendor_4="Source 4 for Item", link_4="Source 4 Purchase Link",
                       vendor_5="Source 5 for Item", link_5="Source 5 Purchase Link",
                       tags="Comma-separated tags", notes="Notes about this item",
                       )

async def add_hybrid(interaction: discord.Interaction, item_name: str, priority: int, 
                   quantity: float, order_quantity: float, low_threshold: float, unit: str, decrease_amount: float, 
                   digikey_part_number: str | None = None, tags: str | None = None, notes: str | None = None,
                   vendor_1: str | None = None, link_1: str | None = None, vendor_2: str | None = None, link_2: str | None = None, 
                   vendor_3: str | None = None, link_3: str | None = None, vendor_4: str | None = None, 
                   link_4: str | None = None, vendor_5: str | None = None, link_5: str | None = None):

    if tags == None:
        tags = "hybrid_tracking"
    else:
        tags = f"hybrid_tracking, {tags}"

    response_message = await command_handler.handler_add_item(item_name, priority, order_quantity, "HYBRID", 
                                                              quantity, low_threshold, unit, decrease_amount, vendor_1, link_1, 
                                                              vendor_2, link_2, vendor_3, link_3, vendor_4, link_4, vendor_5, link_5, digikey_part_number, tags, notes,)

    await interaction.response.send_message(response_message)

@bot.tree.command(name="add_with_dkpn", description="Add item to inventory w/ per item tracking, getting info using a Digikey part number")
@app_commands.describe(item_name="Item Name", priority="Item Priority, 1-10",
                       order_quantity="Number of units to order when stock low", 
                       unit="Unit name", digikey_part_number="Digikey Part Number",
                       quantity="Number of units on hand", low_threshold="Minimum Stock", 
                       tags="Comma-separated tags", notes="Notes about this item",
                       )

async def add_with_dkpn(interaction: discord.Interaction, digikey_part_number: str, priority: int, 
                   quantity: float, order_quantity: float, low_threshold: float, unit: str, item_name: str | None = None, tags: str | None = None, notes: str | None = None):

    dkpn_info = dk.lookup_part_number(digikey_part_number)
    if item_name == None:
        item_name = f"{dkpn_info["Product"]["Manufacturer"]["Name"]} {dkpn_info["Product"]["Description"]["ProductDescription"]}"

    if tags == None:
        tags = "per_item_tracking, digikey_dkpn"
    else:
        tags = f"per_item_tracking, digikey_dkpn, {tags}"

    response_message = await command_handler.handler_add_item(item_name, priority, order_quantity, "HYBRID", 
                                                              quantity, low_threshold, unit, 1, None, None, 
                                                              None, None, None, None, None, None, None, None, digikey_part_number, tags, notes,)

    await interaction.response.send_message(response_message)

# Something about search makes discord hate it, no clue why -PC
@bot.tree.command(name="search", description="Search inventory by item name")
@app_commands.describe(name="Item name")
async def search(interaction: discord.Interaction, name: str):
    await interaction.response.defer()
    response_message = await command_handler.handler_search(name, discord=True)

    await interaction.followup.send(embed=response_message)

@bot.tree.command(name="search_tag", description="Search inventory by tag")
@app_commands.describe(tag="Tag to search for")
async def search_tag(interaction: discord.Interaction, tag: str):
    await interaction.response.defer()

    response_message = await command_handler.handler_search_tag(
        tag,
        discord=True,
    )

    if isinstance(response_message, discord.Embed):
        await interaction.followup.send(embed=response_message)
    else:
        await interaction.followup.send(response_message)


@bot.tree.command(name="get_tags", description="List all item tags")
async def get_tags(interaction: discord.Interaction):
    await interaction.response.defer()

    response_message = await command_handler.handler_get_tags(discord=True)

    if isinstance(response_message, discord.Embed):
        await interaction.followup.send(embed=response_message)
    else:
        await interaction.followup.send(response_message)

@bot.tree.command(name="add_tag", description="Add a tag to an item")
@app_commands.describe(sku="Item SKU", tag="Tag to add")
async def add_tag(interaction: discord.Interaction, sku: str, tag: str):
    response_message = await command_handler.handler_add_tag(sku, tag)
    await interaction.response.send_message(response_message)

@bot.tree.command(name="generate_barcode", description="Generate a barcode")
@app_commands.describe(sku="Item Sku")
async def generate_barcode(interaction: discord.Interaction, sku: str):
    sku = illusion_helpers.clean_sku(sku)
    
    try:
        barcode_bytes = await command_handler.handler_generate_barcode(sku)
    except ServiceUnavailable as e:
        await interaction.response.send_message(f"Unable to reach the print server.\n{e}")
        return

    file = discord.File(io.BytesIO(barcode_bytes), filename=f"{sku}.png")

    await interaction.response.send_message(f"Barcode", file=file)

def make_notifier(interaction: discord.Interaction):
    """Print jobs finish long after the slash command is answered, so updates go to the channel.

    Returns a reply_to token rather than a callback: lipgloss is a separate
    process now and cannot hold a reference to a coroutine in this one. It
    echoes the token on every event about the job, and the dispatcher below
    turns it back into a channel message.
    """
    channel = interaction.channel
    user = interaction.user
    token = f"bot:{interaction.id}"

    async def notify(event):
        if channel is None:
            return

        embed = presentation.notice_embed(
            event["title"], event["message"], urgent=event["urgent"]
        )

        # The mention has to sit outside the embed to actually ping
        await channel.send(user.mention if event["urgent"] else None, embed=embed)

    register_notifier(token, notify)

    return token


# reply_to token -> coroutine handling that submitter's events. Bounded, because
# a token is registered per print command and only the ones whose jobs finish
# cleanly get removed; a printer left broken for a week must not grow this
# without limit.
notifiers = collections.OrderedDict()

MAX_NOTIFIERS = 100


def register_notifier(token, handler):
    notifiers[token] = handler
    notifiers.move_to_end(token)

    while len(notifiers) > MAX_NOTIFIERS:
        notifiers.popitem(last=False)


# Events are the fast path, reconciliation is the correctness guarantee. Nothing
# is replayed on reconnect, and a subscriber that falls far enough behind is
# evicted outright, so the bot never assumes the stream told it everything.
RECONCILE_INTERVAL = 900


async def claws_event_loop():
    """Own the low-stock thread lifecycle, driven by claws.

    This is the coupling the split exists to break. Creating the thread used to
    happen inline inside handler_decrease, which only worked because the bot and
    the kiosk shared a process. Now claws reports the transition and whoever is
    connected to Discord reacts to it, so a scan at the kiosk still opens a
    thread even though the kiosk cannot talk to Discord at all.
    """
    await channel_ready.wait()

    while not shutdown_event.is_set():
        # Every reconnect starts with a reconcile, since whatever happened while
        # the stream was down was never queued for us
        await reconcile_low_threads()

        try:
            async for event in claws.events():
                try:
                    if event["event"] == "item.low":
                        await command_handler.create_low_thread(event["sku"], event["item"])
                    elif event["event"] == "item.resolved":
                        await command_handler.archive_low_thread(event["sku"], event["item"])
                except Exception as e:
                    print(f"Unable to handle {event['event']} for {event['sku']}: {e}")
        except Exception as e:
            if shutdown_event.is_set():
                return

            print(f"claws event stream dropped ({e}), reconnecting")

        await asyncio.sleep(5)


async def reconcile_loop():
    """Periodic safety net against a silently missed event."""
    await channel_ready.wait()

    while not shutdown_event.is_set():
        await asyncio.sleep(RECONCILE_INTERVAL)

        if shutdown_event.is_set():
            return

        await reconcile_low_threads(quiet=True)


async def reconcile_low_threads(quiet=False):
    """Bring Discord back in line with claws, which is the source of truth.

    claws decides what needs doing; this only carries it out. Catches
    transitions that happened while the bot was down, and any event lost because
    the stream dropped or this subscriber was evicted for falling behind.
    """
    try:
        rows = await claws.low_threads()
    except ServiceUnavailable as e:
        print(f"Could not reconcile low-stock threads: {e}")
        return

    fixed = 0

    for row in rows:
        item = row["item"]
        sku = item["SKU"]

        try:
            if row["action"] == "create":
                thread_name = await command_handler.create_low_thread(sku, item)

                if thread_name:
                    print(f"Reconciled {sku}: was low with no thread, opened {thread_name}")
                    fixed += 1
                else:
                    print(f"Could not reconcile {sku}: thread was not created")
            elif row["action"] == "archive":
                archived, detail = await command_handler.archive_low_thread(sku, item)

                if archived:
                    print(f"Reconciled {sku}: no longer low. {detail}")
                    fixed += 1
                else:
                    print(f"Could not reconcile {sku}: {detail}")
        except Exception as e:
            print(f"Could not reconcile {sku}: {e}")

    if not quiet or fixed:
        print(f"Low-stock threads reconciled: {len(rows)} tracked, {fixed} corrected")


async def lipgloss_event_loop():
    """Route print events back to whoever asked for the job.

    Reconnects on its own: lipgloss restarting, or the link dropping, must not
    silently end print notifications for the rest of the session.
    """
    await bot.wait_until_ready()

    while not shutdown_event.is_set():
        try:
            async for event in lipgloss.events():
                handler = notifiers.get(event.get("reply_to"))

                if handler is None:
                    continue

                try:
                    await handler(event)
                except Exception as e:
                    print(f"Unable to deliver print update: {e}")

                if event["event"] == "job.done":
                    notifiers.pop(event["reply_to"], None)
        except Exception as e:
            if shutdown_event.is_set():
                return

            print(f"lipgloss event stream dropped ({e}), reconnecting")

        await asyncio.sleep(5)

@bot.tree.command(name="print", description="Print a label")
@app_commands.describe(style="Label style", text_line_1="Text Line 1", text_line_2="Text Line 2", sku="Item Sku", get_text_from_sku="Get the item name from the provided sku", quantity="Number of copies to print",)
@app_commands.choices(
    style=[
        app_commands.Choice(name="Barcode (Requires sku)", value="slim_barcode"),
        app_commands.Choice(name="Label w/ Barcode (Requires sku and text_line_1)", value="label_barcode"),
        app_commands.Choice(name="Label w/ QR Code (Requires sku and text_line_1, optionally text_line_2)", value="label_qr"),
        app_commands.Choice(name="Label (Requires text_line_1, optionally text_line_2)", value="label"),
        app_commands.Choice(name="Cable Label (Requires text_line_1, optionally text_line_2)", value="cable_label"),
        app_commands.Choice(name="Cable Label w/ SKU (Requires text_line_1 and sku, optionally text_line_2)", value="cable_label_sku"),
        app_commands.Choice(name="Cable Label w/ QR Code (Requires sku and text_line_1)", value="cable_label_qr"),
    ]
)
async def print_niimbot(interaction: discord.Interaction, style: app_commands.Choice[str], sku: str | None = None, 
                        text_line_1: str | None = None, text_line_2: str | None = None, get_text_from_sku: bool = False,
                        quantity: app_commands.Range[int, 1, MAX_COPIES] = 1,):
    if not config["illusion"]["printer"]["niimbot"]["enabled"]:
        await interaction.response.send_message(f"Printer not enabled")
        return

    style_name = style.value

    if sku == None and get_text_from_sku == True:
        await interaction.response.send_message(f"SKU required to get item text from sku")
        return

    if get_text_from_sku == True:
        sku = illusion_helpers.clean_sku(sku)
        item = await claws.get_item(sku)

        if item is None:
            await interaction.response.send_message(f"Invalid sku: {sku}")
            return

        text_line_1 = item["NAME"]

    # Make sure we have all required values for each style
    if text_line_1 == None and (style_name == "label" or style_name == "label_barcode" or style_name == "cable_label" or style_name == "cable_label_barcode" or style_name == "label_qr" or style_name == "cable_label_sku"):
        await interaction.response.send_message(f"Style: {style_name} requires text_line_1")
        return
    if text_line_2 == None and (style_name == "cable_label" or style_name == "cable_label_sku"):
        text_line_2 = text_line_1
    if sku == None and (style_name == "slim_barcode" or style_name == "label_barcode" or style_name == "cable_label_barcode" or style_name == "label_qr" or style_name == "cable_label_sku"):
        await interaction.response.send_message(f"Style: {style_name} requires sku")
        return

    await interaction.response.defer()

    if style_name == "label":
        if text_line_2 == None:
            style_name = "label_1_line"
        else:
            style_name = "label_2_line"

    if style_name == "label_qr":
        if text_line_2 == None:
            style_name = "label_1_line_qr"
        else:
            style_name = "label_2_line_qr"

    response_message = await command_handler.handler_print(style=style_name, sku=sku, text_line_1=text_line_1, text_line_2=text_line_2,
                                                          quantity=quantity, reply_to=make_notifier(interaction), source=f"discord/{interaction.user.display_name}",)
    await interaction.followup.send(response_message)

@bot.tree.command(name="print_image", description="Print an image")
@app_commands.describe(image="Image to print", rotate="Degrees to rotate by", quantity="Number of copies to print")
async def print_image(interaction: discord.Interaction, image: discord.Attachment, rotate: int = 0,
                      quantity: app_commands.Range[int, 1, MAX_COPIES] = 1,):
    if not config["illusion"]["printer"]["niimbot"]["enabled"]:
        await interaction.response.send_message(f"Printer not enabled")
        return
    
    if image.content_type is None or not image.content_type.startswith("image/"):
        await interaction.response.send_message("Please upload a valid image.", ephemeral=True)
        return
    
    await interaction.response.defer()

    image_bytes = await image.read()
    with Image.open(io.BytesIO(image_bytes)) as img:
        rotated = img.rotate(rotate, expand=True)
        resized = rotated.resize((96, 320))

        # lipgloss is a separate process and may be on another machine, so the
        # rendered image travels with the request instead of by path
        buffer = io.BytesIO()
        resized.save(buffer, format="PNG")
        resized_bytes = buffer.getvalue()

    response_message = await command_handler.handler_print_image(resized_bytes, image.filename, quantity=quantity,
                                                                reply_to=make_notifier(interaction), source=f"discord/{interaction.user.display_name}",)
    await interaction.followup.send(response_message)

@bot.tree.command(name="print_queue", description="Show what the printer is working through")
async def print_queue_status(interaction: discord.Interaction):
    if not config["illusion"]["printer"]["niimbot"]["enabled"]:
        await interaction.response.send_message(f"Printer not enabled")
        return

    response_message = await command_handler.handler_print_queue(discord=True)

    if isinstance(response_message, discord.Embed):
        await interaction.response.send_message(embed=response_message)
    else:
        await interaction.response.send_message(response_message)

@bot.tree.command(name="print_resume", description="Resume the print queue after fixing the printer")
async def print_resume(interaction: discord.Interaction):
    if not config["illusion"]["printer"]["niimbot"]["enabled"]:
        await interaction.response.send_message(f"Printer not enabled")
        return

    await interaction.response.defer()

    response_message = await command_handler.handler_print_resume()
    await interaction.followup.send(response_message)

@bot.tree.command(name="print_cancel", description="Cancel a queued print job")
@app_commands.describe(job_id="Job id from /print_queue")
async def print_cancel(interaction: discord.Interaction, job_id: int):
    if not config["illusion"]["printer"]["niimbot"]["enabled"]:
        await interaction.response.send_message(f"Printer not enabled")
        return

    response_message = await command_handler.handler_print_cancel(job_id)
    await interaction.response.send_message(response_message)

@bot.tree.command(name="print_clear", description="Clear every job from the print queue")
async def print_clear(interaction: discord.Interaction):
    if not config["illusion"]["printer"]["niimbot"]["enabled"]:
        await interaction.response.send_message(f"Printer not enabled")
        return

    response_message = await command_handler.handler_print_clear()
    await interaction.response.send_message(response_message)

@bot.tree.command(name="printer_info", description="Get info about the printer")
async def printer_info(interaction: discord.Interaction):
    if not config["illusion"]["printer"]["niimbot"]["enabled"]:
        await interaction.response.send_message(f"Printer not enabled")
        return
    
    await interaction.response.defer()

    response_message = await command_handler.handler_printer_info()
    await interaction.followup.send(response_message)

@bot.tree.command(name="update_item", description="Update an existing item")
@app_commands.describe(sku="Item SKU", item_name="Item Name", priority="Item Priority",
                       order_quantity="Number of units to order when stock low", unit="Unit name",
                       quantity="Number of units on hand", low_threshold="Minimum Stock", decrease_amount="Amount to decrease by",
                       digikey_part_number="Digikey Part Number",
                       vendor_1="Source 1 for Item", link_1="Source 1 Purchase Link",
                       vendor_2="Source 2 for Item", link_2="Source 2 Purchase Link",
                       vendor_3="Source 3 for Item", link_3="Source 3 Purchase Link",
                       vendor_4="Source 4 for Item", link_4="Source 4 Purchase Link",
                       vendor_5="Source 5 for Item", link_5="Source 5 Purchase Link",
                       tags="Comma-separated tags", notes="Notes about this item",
                       )

async def update_item(interaction: discord.Interaction, sku: str, 
                      item_name: str | None = None, priority: str | None = None, quantity: str | None = None, order_quantity: str | None = None, 
                      low_threshold: str | None = None, unit: str | None = None, decrease_amount: str | None = None, 
                      digikey_part_number: str | None = None, tags: str | None = None, notes: str | None = None,
                      vendor_1: str | None = None, link_1: str | None = None, vendor_2: str | None = None, link_2: str | None = None, 
                      vendor_3: str | None = None, link_3: str | None = None, vendor_4: str | None = None, 
                      link_4: str | None = None, vendor_5: str | None = None, link_5: str | None = None):

    updates = {
            "NAME": item_name,
            "PRIORITY": priority,
            "ORDER_QUANTITY": order_quantity,
            "TRACKING_MODE": None,
            "QUANTITY_ON_HAND": quantity,
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
            "LOW": None,
            "DIGIKEY_PART_NUMBER": digikey_part_number,
            "NOTES": notes,
            "TAGS": tags,
        }
            
    response_message = await command_handler.handler_update_item(sku, updates)
    await interaction.response.send_message(response_message)

async def graceful_exit(reason: str = "unknown"):
    global shutdown_started

    if shutdown_started:
        return

    shutdown_started = True
    print(f"Graceful exit requested: {reason}")

    shutdown_event.set()

    try:
        await lipgloss.aclose()
    except Exception as e:
        print(f"Error closing lipgloss client: {e}")

    try:
        await claws.aclose()
    except Exception as e:
        print(f"Error closing claws client: {e}")

    try:
        await bot.close()
    except Exception as e:
        print(f"Error closing bot: {e}")

def install_signal_handlers():
    loop = asyncio.get_running_loop()

    for sig in (signal.SIGTERM, signal.SIGINT):
        loop.add_signal_handler(
            sig,
            lambda s=sig: asyncio.create_task(graceful_exit(s.name)),
        )

@bot.event
async def setup_hook():
    install_signal_handlers()
    bot.loop.create_task(terminal_loop())

    bot.loop.create_task(claws_event_loop())
    bot.loop.create_task(reconcile_loop())

    if PRINTING_ENABLED:
        bot.loop.create_task(lipgloss_event_loop())

CONFIG_PATH = os.environ.get("ILLUSION_CONFIG", "./config.yaml")

try:
    config = illusion_config.load(CONFIG_PATH)

    required = [
        "illusion.claws.url",
        "illusion.claws.token",
        "illusion.discord.token",
        "illusion.discord.server_id",
        "illusion.discord.fourm_id",
    ]

    # Feature specific fields are only demanded when the feature is switched on,
    # so a machine with no printer or no DigiKey access still starts. Everything
    # is collected into one list first so a single run reports every missing
    # field, rather than turning startup into fix-one-discover-another.
    # The printer port and font moved to lipgloss.yaml, since only lipgloss
    # touches the hardware. The frontend just needs to know where it is.
    if illusion_config.get(config, "illusion.printer.niimbot.enabled"):
        required += [
            "illusion.lipgloss.url",
            "illusion.lipgloss.token",
        ]

    illusion_config.require(config, required, source=CONFIG_PATH)
except illusion_config.ConfigError as e:
    print(e)
    raise SystemExit(1)

PRINTING_ENABLED = bool(illusion_config.get(config, "illusion.printer.niimbot.enabled"))

TOKEN = config["illusion"]["discord"]["token"]
GUILD_ID = config["illusion"]["discord"]["server_id"]
FORUM_CHANNEL_ID = config["illusion"]["discord"]["fourm_id"] 

channel = None

command_handler = DB_Commands()
claws = ClawsClient(
    illusion_config.get(config, "illusion.claws.url"),
    illusion_config.get(config, "illusion.claws.token"),
)
lipgloss = LipglossClient(
    illusion_config.get(config, "illusion.lipgloss.url"),
    illusion_config.get(config, "illusion.lipgloss.token"),
)

# DigiKey lookups happen inside claws now: it is the service with the good
# internet connection and the one that owns the tokens it has to refresh.


def main():
    bot.run(TOKEN)


if __name__ == "__main__":
    main()
