import discord
from discord import app_commands
from discord.ext import commands
from inventory_reader import SpreadsheetManager
import asyncio
import yaml
import tomllib
from pathlib import Path
import illusion_helpers
from PIL import Image
import os, io, platform, signal
from digikey_client import DigiKeyClient
import time
from datetime import timedelta
from label_maker import LabelMaker

try:
    import readline
except:
    print("readline not installed")

shutdown_event = asyncio.Event()
shutdown_started = False

pyproject_path = Path(__file__).resolve().parents[0] / "./pyproject.toml"

with pyproject_path.open("rb") as f:
    pyproject = tomllib.load(f)

illusion_version = pyproject["project"]["version"]
intents = discord.Intents.default()

bot = commands.Bot(command_prefix="!", intents=intents)

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

class DB_Commands:
    async def handler_add_item(self, item_name, priority, order_quantity, tracking_mode="KANBAN", quantity_on_hand=None, 
                               low_threshold=None, unit=None, decrease_amount=None, vendor_1 = None, link_1 = None, 
                               vendor_2 = None, link_2 = None, vendor_3 = None, link_3 = None, 
                               vendor_4 = None, link_4 = None, vendor_5 = None, link_5 = None,): 
        global inventory

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
            "LOW": "FALSE"
        }
        
        new_sku = inventory.add_item(new_item)

        inventory.save()

        response_message = f"Added {item_name} to inventory, SKU: {new_sku}"
        return response_message
    
    async def handler_delete_item(self, sku):
        global inventory
        sku = illusion_helpers.clean_sku(sku)
        if inventory.validate_sku(sku):
            item = inventory.get_item(sku)
            inventory.delete_item(sku)
            inventory.save()
            response_message = f"Removed {item['NAME']} from inventory, SKU: {sku}"
        else:
            response_message = f"Invalid sku: {sku}"
        
        return response_message
    
    async def handler_info(self, sku, hide_ext=True):
        global inventory

        sku = illusion_helpers.clean_sku(sku)
        if inventory.validate_sku(sku):
            item = inventory.get_item(sku)
            if hide_ext:
                exclude = ["PRIORITY", "TRACKING_MODE", "LOW_THRESHOLD", "UNIT", "LOW_THREAD_ID", "DECREASE_AMOUNT", 
                            "VENDOR_1", "LINK_1", "VENDOR_2", "LINK_2", "VENDOR_3", "LINK_3", "VENDOR_4", "LINK_4", "VENDOR_5", "LINK_5"]

                if item["TRACKING_MODE"] == "KANBAN":
                    exclude.append("QUANTITY_ON_HAND")
            else:
                exclude = []
            
            response_message = illusion_helpers.make_table(item, exclude)
        else:
            response_message = f"Invalid sku: {sku}"
        
        return response_message
    
    async def handler_resolve(self, sku, archive_thread=False):
        global inventory

        sku = illusion_helpers.clean_sku(sku)
        if inventory.validate_sku(sku):
            item = inventory.get_item(sku)

            if item["LOW"] == True:
                inventory.update_item(sku, {"LOW": "FALSE"})
                inventory.save()
                response_message = f"{sku} no longer marked as low"
                if archive_thread:
                    thread_message = await self.archive_low_thread(sku)
                    response_message += f"\n{thread_message}"
            else:
                response_message = f"{sku} not marked as low"
        else:
            response_message = f"Invalid sku: {sku}"
        
        return response_message

    async def handler_search(self, name: str):
        global inventory

        results = inventory.search_items(name, limit=10)

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
        ]

        return illusion_helpers.make_table(results, exclude=exclude)
    
    async def handler_decrease(self, sku, amount=None):
        global inventory
        global channel

        sku = illusion_helpers.clean_sku(sku)

        if amount != None and int(amount) <= 0:
            return f"Quantity must be greater than 0"
        elif inventory.validate_sku(sku):
            result = inventory.decrease_item(sku, amount)
            item = result["item"]

            thread_name = None

            if result["low_changed"]:
                thread_with_message = await channel.create_thread(
                    name=f"{item['NAME']}: {item['SKU']}",
                    content=illusion_helpers.make_low_thread_content(item),
                    view=illusion_helpers.make_vendor_buttons(item),
                )
                thread_name = thread_with_message.thread.name

                inventory.update_item(sku, {"LOW_THREAD_ID": thread_with_message.thread.id,},)
                inventory.save()

            if item["TRACKING_MODE"] == "KANBAN":
                if result["low_changed"]:
                    return f"{sku} marked as low, thread: {thread_name} created"

                return f"{sku} already marked as low"

            unit = item["UNIT"] or "units"

            response_message = (
                f"{sku} decreased by "
                f"{illusion_helpers.format_quantity(result['decrease_amount'])} {unit}: "
                f"{illusion_helpers.format_quantity(result['old_quantity'])} -> "
                f"{illusion_helpers.format_quantity(result['new_quantity'])}"
            )

            if result["low_changed"]:
                response_message += f"\nLow threshold reached, thread: {thread_name} created"
            elif item["LOW"]:
                response_message += "\nItem is already marked as low."

            inventory.save()
        else:
            response_message = f"Invalid sku: {sku}"

        return response_message
    
    async def handler_increase(self, sku, amount=1):
        global inventory

        sku = illusion_helpers.clean_sku(sku)

        if inventory.validate_sku(sku):
            item = inventory.increase_item(sku, float(amount))
            inventory.save()

            unit = item["UNIT"] or "units"

            response_message = (
                f"{sku} increased by {illusion_helpers.format_quantity(amount)} {unit}. "
                f"New stock: {illusion_helpers.format_quantity(item['QUANTITY_ON_HAND'])} {unit}. "
                f"Low: {item['LOW']}"
            )
        else:
            response_message = f"Invalid sku: {sku}"

        return response_message
    
    async def handler_set_stock(self, sku, quantity):
        global inventory

        sku = illusion_helpers.clean_sku(sku)

        if inventory.validate_sku(sku):
            item = inventory.set_stock(sku, float(quantity))
            inventory.save()

            unit = item["UNIT"] or "units"

            response_message = (
                f"{sku} stock set to "
                f"{illusion_helpers.format_quantity(item['QUANTITY_ON_HAND'])} {unit}. "
                f"Low: {item['LOW']}"
            )
        else:
            response_message = f"Invalid sku: {sku}"

        return response_message
    
    async def archive_low_thread(self, sku):
        global inventory
        global bot

        if not inventory.validate_sku(sku):
            return "Invalid SKU"

        item = inventory.get_item(sku)

        if item is None:
            return "No item found."

        thread_id = item.get("LOW_THREAD_ID")

        if not thread_id:
            return "No low-stock thread was stored for this item."

        try:
            thread = bot.get_channel(int(thread_id))

            if thread is None:
                thread = await bot.fetch_channel(int(thread_id))

        except discord.NotFound:
            inventory.update_item(sku, {"LOW_THREAD_ID": None})
            inventory.save()
            return "Stored thread no longer exists."

        if not isinstance(thread, discord.Thread):
            return "Stored channel is not a thread."

        await thread.edit(
            archived=True,
            reason=f"{sku} resolved",
        )

        inventory.update_item(sku, {"LOW_THREAD_ID": None})
        inventory.save()

        return "Low-stock thread archived."
    
    async def handler_generate_barcode(self, sku):
        return labelmaker.render_label(style_name="classic_barcode", sku=sku, width=350, height=280, rotate=0)
    
    async def handler_niimbot_barcode(self, sku, text):
        sku = illusion_helpers.clean_sku(sku)
        serial_port = config["illusion"]["printer"]["niimbot"]["port"] 

        if text == None:
            bc_path = labelmaker.render_label(style_name="slim_barcode", sku=sku, width=320, height=96)
        else:
            bc_path = labelmaker.render_label(style_name="label_barcode", sku=sku, input_text_1=text, width=320, height=96)

        result = illusion_helpers.niimbot_print(bc_path, serial_port, "d110")
        return result

    async def handler_bulk_print_niimbot(self, sku_lower, sku_upper):
        serial_port = config["illusion"]["printer"]["niimbot"]["port"] 
        
        response_message = await illusion_helpers.bulk_niimbot_print(serial_port, "d110", labelmaker, sku_lower, sku_upper)
        return response_message
    
    async def handler_print_label(self, line_1, line_2):
        serial_port = config["illusion"]["printer"]["niimbot"]["port"]

        if line_2 == None:
            output = labelmaker.render_label(style_name="label_1_line", input_text_1=line_1, width=320, height=96)
        else:
            output = labelmaker.render_label(style_name="label_2_line", input_text_1=line_1, input_text_2=line_2, width=320, height=96)
        
        return illusion_helpers.niimbot_print(output, serial_port, "d110")
    
    async def handler_update_item(self, sku, updates: dict[str, object]):
        global inventory
        sku = illusion_helpers.clean_sku(sku)

        if not inventory.validate_sku(sku):
            return f"Invalid sku: {sku}"
        
        # Automatically adds a digikey link if a digikey link was added
        if updates["DIGIKEY_PART_NUMBER"] != None:
            digikey_link = f"https://www.digikey.ca/en/products/result?keywords={updates['DIGIKEY_PART_NUMBER']}"
            inventory.add_vendor(sku, "Digikey", digikey_link)

        cleaned = {}
        
        for key, value in updates.items():
            if value != None:
                cleaned[key] = value

        updates = cleaned

        if not updates:
            return "No updates provided."

        inventory.update_item(sku, updates)
        inventory.save()

        changed_fields = ", ".join(updates.keys())

        return f"Updated {sku}: {changed_fields}"
    
    async def handler_digikey_scan(self, barcode_text: str):
        global inventory

        try:
            data = await asyncio.to_thread(dk.lookup_barcode, barcode_text)
        except Exception as e:
            return f"DigiKey lookup failed: {e}"

        dkpn = data.get("DigiKeyPartNumber")
        quantity = data.get("Quantity") or 0
        description = data.get("ProductDescription")

        if not dkpn:
            return "Barcode didn't contain a DigiKey part number"

        existing = inventory.get_item_by_dkpn(dkpn)

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
            "PRIORITY": "NORMAL",
            "ORDER_QUANTITY": None,
            "TRACKING_MODE": "QUANTITY",
            "QUANTITY_ON_HAND": quantity,
            "DECREASE_AMOUNT": 1,
            "DIGIKEY_PART_NUMBER": dkpn,
            "VENDOR_1": "DigiKey",
            "LINK_1": f"https://www.digikey.ca/en/products/result?keywords={dkpn}",
            "LOW": "FALSE",
        }

        new_sku = inventory.add_item(new_item)
        inventory.save()
        return f"New item {new_sku} created from {dkpn} with {quantity} on hand"

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
                ]
            )

        return f"\n<sku> required argument\n[amount] optional argument\n\n{illusion_helpers.make_table(command_list)}\n"

async def terminal_loop():
    await bot.wait_until_ready()

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
                    print(f"{hat_lines[i].ljust(hat_width + gap)}{text_lines[i]}")
                else:
                    print(f"{hat_lines[i].ljust(hat_width + gap)}")
            
            response_message = ""
            
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
                response_message = await command_handler.handler_niimbot_barcode(parts[1], None)
            else:
                response_message = await command_handler.handler_niimbot_barcode(parts[1], parts[2])
        elif command == "printer_info" and config["illusion"]["printer"]["niimbot"]["enabled"] and len(parts) >= 1:
            serial_port = config["illusion"]["printer"]["niimbot"]["port"]
            response_message = illusion_helpers.niimbot_printer_info(serial_port)
        elif command == "print_label" and config["illusion"]["printer"]["niimbot"]["enabled"] and len(parts) >= 2:
            # Awful, Awful, Awful
            # I hate this code
            # Can't be replaced by shlex without breaking non qouted strings
            if len(parts) == 3:
                cleaned_text = text.replace("print_label ", "")
                if '"' in cleaned_text:
                    lines = cleaned_text.split('"')
                    if len(lines) >= 4:
                        line_2 = lines[3]
                        line_1 = lines[1]
                    else:
                        response_message = "Invalid Qoutes"
                else:
                    line_1 = f"{parts[1]} {parts[2]}"
                    line_2 = None
            else:
                line_1 = parts[1]
                line_2 = None
            
            if response_message == None:
                response_message = await command_handler.handler_print_label(line_1, line_2)
        elif command == "bulk_print" and config["illusion"]["printer"]["niimbot"]["enabled"] and len(parts) == 3:
            response_message = await command_handler.handler_bulk_print_niimbot(parts[1], parts[2])
        elif command == "set" and len(parts) == 3:
            response_message = await command_handler.handler_set_stock(parts[1], parts[2])
        else:
            response_message = f"Invalid Command: {command}\n\nHelp:{await command_handler.handler_command_help()}"

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

    cleaned_sku = illusion_helpers.clean_sku(sku)
    item = inventory.get_item(cleaned_sku)
    if item["LOW"] == False and item["LOW_THREAD_ID"] != None:
        await command_handler.archive_low_thread(cleaned_sku)

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

    cleaned_sku = illusion_helpers.clean_sku(sku)
    item = inventory.get_item(cleaned_sku)
    if item["LOW"] == False and item["LOW_THREAD_ID"] != None:
        await command_handler.archive_low_thread(cleaned_sku)

@bot.tree.command(name="info", description="Get info about an item")
@app_commands.describe(sku="Item Sku", hide_ext="Show or hide extra values")
async def info(interaction: discord.Interaction, sku: str, hide_ext: bool = False):
    response_message = await command_handler.handler_info(sku, hide_ext)

    if response_message.startswith("Invalid sku"):
        await interaction.response.send_message(response_message)
    else:
        cleaned_sku = illusion_helpers.clean_sku(sku)
        item = inventory.get_item(cleaned_sku)

        await interaction.response.send_message(f"```{response_message}```", view=illusion_helpers.make_vendor_buttons(item),)

@bot.tree.command(name="delete", description="Delete an item")
@app_commands.describe(sku="Item Sku")
async def delete(interaction: discord.Interaction, sku: str):
    response_message = await command_handler.handler_delete_item(sku)
    await interaction.response.send_message(response_message)

@bot.tree.command(name="add_item", description="Add item to inventory w/ per unit tracking")
@app_commands.describe(item_name="Item Name",
                       priority="Item Priority, 1-10",
                       order_quantity="Number of units to order when stock low", unit="Unit name",
                       quantity="Number of units on hand", low_threshold="Minimum Stock",
                       vendor_1="Source 1 for Item", link_1="Source 1 Purchase Link",
                       vendor_2="Source 2 for Item", link_2="Source 2 Purchase Link",
                       vendor_3="Source 3 for Item", link_3="Source 3 Purchase Link",
                       vendor_4="Source 4 for Item", link_4="Source 4 Purchase Link",
                       vendor_5="Source 5 for Item", link_5="Source 5 Purchase Link",
                       )

async def add_item(interaction: discord.Interaction, item_name: str, priority: int, 
                   quantity: float, order_quantity: float, low_threshold: float, unit: str,
                   vendor_1: str | None = None, link_1: str | None = None, vendor_2: str | None = None, link_2: str | None = None, 
                   vendor_3: str | None = None, link_3: str | None = None, vendor_4: str | None = None, 
                   link_4: str | None = None, vendor_5: str | None = None, link_5: str | None = None):

    response_message = await command_handler.handler_add_item(item_name, priority, order_quantity, "QUANTITY", quantity, low_threshold, unit, "1", vendor_1, link_1, 
                                                              vendor_2, link_2, vendor_3, link_3, vendor_4, link_4, vendor_5, link_5,)

    await interaction.response.send_message(response_message)

@bot.tree.command(name="add_kanban", description="Add item to inventory w/ kanban tracking")
@app_commands.describe(item_name="Item Name",
                       priority="Item Priority, 1-10",
                       order_quantity="Number of units to order when stock low",
                       vendor_1="Source 1 for Item", link_1="Source 1 Purchase Link",
                       vendor_2="Source 2 for Item", link_2="Source 2 Purchase Link",
                       vendor_3="Source 3 for Item", link_3="Source 3 Purchase Link",
                       vendor_4="Source 4 for Item", link_4="Source 4 Purchase Link",
                       vendor_5="Source 5 for Item", link_5="Source 5 Purchase Link",
                       )

async def add_kanban(interaction: discord.Interaction, item_name: str, priority: int, order_quantity: float,
                   vendor_1: str | None = None, link_1: str | None = None, vendor_2: str | None = None, link_2: str | None = None, 
                   vendor_3: str | None = None, link_3: str | None = None, vendor_4: str | None = None, 
                   link_4: str | None = None, vendor_5: str | None = None, link_5: str | None = None):

    response_message = await command_handler.handler_add_item(item_name, priority, order_quantity, "KANBAN", None, None, None, None, vendor_1, link_1, 
                                                              vendor_2, link_2, vendor_3, link_3, vendor_4, link_4, vendor_5, link_5,)

    await interaction.response.send_message(response_message)

@bot.tree.command(name="add_hybrid", description="Add item to inventory w/ hybrid tracking")
@app_commands.describe(item_name="Item Name",
                       priority="Item Priority, 1-10",
                       order_quantity="Number of units to order when stock low", unit="Unit name",
                       quantity="Number of units on hand", low_threshold="Minimum Stock", decrease_amount="Amount to decrease by",
                       vendor_1="Source 1 for Item", link_1="Source 1 Purchase Link",
                       vendor_2="Source 2 for Item", link_2="Source 2 Purchase Link",
                       vendor_3="Source 3 for Item", link_3="Source 3 Purchase Link",
                       vendor_4="Source 4 for Item", link_4="Source 4 Purchase Link",
                       vendor_5="Source 5 for Item", link_5="Source 5 Purchase Link",
                       )

async def add_hybrid(interaction: discord.Interaction, item_name: str, priority: int, 
                   quantity: float, order_quantity: float, low_threshold: float, unit: str, decrease_amount: float,
                   vendor_1: str | None = None, link_1: str | None = None, vendor_2: str | None = None, link_2: str | None = None, 
                   vendor_3: str | None = None, link_3: str | None = None, vendor_4: str | None = None, 
                   link_4: str | None = None, vendor_5: str | None = None, link_5: str | None = None):

    response_message = await command_handler.handler_add_item(item_name, priority, order_quantity, "HYBRID", 
                                                              quantity, low_threshold, unit, decrease_amount, vendor_1, link_1, 
                                                              vendor_2, link_2, vendor_3, link_3, vendor_4, link_4, vendor_5, link_5,)

    await interaction.response.send_message(response_message)

# Something about search makes discord hate it, no clue why -PC
@bot.tree.command(name="search", description="Search inventory by item name")
@app_commands.describe(name="Item name")
async def search(interaction: discord.Interaction, name: str):
    await interaction.response.defer()
    response_message = await command_handler.handler_search(name)

    if response_message.startswith("No items found"):
        await interaction.followup.send(response_message)
    else:
        if len(response_message) <= 2000:
            await interaction.followup.send(f"```{response_message}```")
        else:
            await interaction.followup.send(f"I didnt feel like handling searches with > 2000 chars, if thix happens from a real search, please ping me -PC")

@bot.tree.command(name="generate_barcode", description="Generate a barcode")
@app_commands.describe(sku="Item Sku")
async def generate_barcode(interaction: discord.Interaction, sku: str):
    sku = illusion_helpers.clean_sku(sku)
    
    file_path = await command_handler.handler_generate_barcode(sku)
    file = discord.File(file_path)

    await interaction.response.send_message(f"Barcode", file=file)

@bot.tree.command(name="print_barcode", description="Print a barcode")
@app_commands.describe(sku="Item Sku", text="Additional text")
async def print_barcode(interaction: discord.Interaction, sku: str, text: str | None = None):
    if not config["illusion"]["printer"]["niimbot"]["enabled"]:
        await interaction.response.send_message(f"Printer not enabled")
        return
    
    await interaction.response.defer()
    sku = illusion_helpers.clean_sku(sku)
    
    response_message = await command_handler.handler_niimbot_barcode(sku, text)
    await interaction.followup.send(response_message)

@bot.tree.command(name="print_image", description="Print an image")
@app_commands.describe(image="Image to print", rotate="Degrees to rotate by")
async def print_image(interaction: discord.Interaction, image: discord.Attachment, rotate: int = 0):
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

        os.makedirs("/tmp/illusion/imgs/", exist_ok=True)

        output_path = os.path.join(
            "/tmp/illusion/imgs/",
            f"resized_{image.filename}",
        )
        
        resized.save(output_path)

    serial_port = config["illusion"]["printer"]["niimbot"]["port"]
    response_message = illusion_helpers.niimbot_print(output_path, serial_port, "d110")
    await interaction.followup.send(response_message)

@bot.tree.command(name="print_label", description="Print a label")
@app_commands.describe(line_1="Line 1", line_2="Line 2")
async def print_label(interaction: discord.Interaction, line_1: str, line_2: str | None = None):
    if not config["illusion"]["printer"]["niimbot"]["enabled"]:
        await interaction.response.send_message(f"Printer not enabled")
        return
    await interaction.response.defer()
    
    response_message = await command_handler.handler_print_label(line_1, line_2)
    await interaction.followup.send(response_message)

@bot.tree.command(name="printer_info", description="Get info about the printer")
async def printer_info(interaction: discord.Interaction):
    if not config["illusion"]["printer"]["niimbot"]["enabled"]:
        await interaction.response.send_message(f"Printer not enabled")
        return
    
    await interaction.response.defer()
    
    serial_port = config["illusion"]["printer"]["niimbot"]["port"]
    response_message = illusion_helpers.niimbot_printer_info(serial_port)
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
                       )

async def update_item(interaction: discord.Interaction, sku: str, 
                      item_name: str | None = None, priority: str | None = None, quantity: str | None = None, order_quantity: str | None = None, 
                      low_threshold: str | None = None, unit: str | None = None, decrease_amount: str | None = None, digikey_part_number: str | None = None,
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
            "DIGIKEY_PART_NUMBER": digikey_part_number
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
        inventory.save()
    except Exception as e:
        print(f"Error saving inventory: {e}")

    try:
        close_method = getattr(inventory, "close", None)
        if callable(close_method):
            close_method()
    except Exception as e:
        print(f"Error closing inventory: {e}")

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

with open("./config.yaml", "r") as file:
    config = yaml.safe_load(file)

TOKEN = config["illusion"]["discord"]["token"]
GUILD_ID = config["illusion"]["discord"]["server_id"]
FORUM_CHANNEL_ID = config["illusion"]["discord"]["fourm_id"] 

command_handler = DB_Commands()
inventory = SpreadsheetManager(config["illusion"]["database_location"])
labelmaker = LabelMaker(config["illusion"]["printer"]["niimbot"]["font_path"])

# Digikey support
if config["illusion"]["digikey"]["enabled"] == True:
    dk = DigiKeyClient("./config.yaml")

bot.run(TOKEN)