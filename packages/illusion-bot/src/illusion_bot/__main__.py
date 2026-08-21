"""illusion, the Discord bot.

Runs on the NAS VM beside claws, on the good side of the closet wifi. It owns
everything Discord: slash commands, embeds, and the low-stock forum threads.

The thread lifecycle is driven by claws' events rather than by whichever command
changed the stock, because the kiosk has no Discord connection at all. Events
are the fast path; the reconcile pass is what makes it correct when one is
missed.
"""

import asyncio
import collections
import io
import os
import signal
import socket
import time
from importlib.metadata import version

import discord
from discord import app_commands
from discord.ext import commands
from PIL import Image

from illusion_core import config as illusion_config
from illusion_core import helpers as illusion_helpers
from illusion_core.clients import ClawsClient, LipglossClient, ServiceUnavailable
from illusion_core.commands import DB_Commands, Rows
from illusion_core import fleet
from illusion_bot import presentation

illusion_version = version("illusion-bot")

boot_time = time.time()

shutdown_event = asyncio.Event()
shutdown_started = False

health_server = None
health_task = None

# bot.wait_until_ready() can return before on_ready has finished, and on_ready
# awaits a channel fetch partway through. Anything needing the forum channel
# waits on this instead.
channel_ready = asyncio.Event()

channel = None

# Mirrors lipgloss's own limit. Slash command ranges are evaluated when the
# decorators run at import time, so this has to be defined before them.
MAX_COPIES = 100

intents = discord.Intents.default()

bot = commands.Bot(
    command_prefix="!",
    intents=intents,
    activity=discord.Game(name=f"illusion {illusion_version}"),
    status=discord.Status.online,
)


def render(result):
    """Discord rendering: strings pass through, Rows becomes an embed."""
    if isinstance(result, Rows):
        return presentation.make_embed(result.data, exclude=result.exclude)

    return result


async def create_low_thread(sku, item=None):
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

async def archive_low_thread(sku, item=None):
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
    # Probing every service takes a few seconds of budget, which would outrun
    # Discord's three second interaction window
    await interaction.response.defer()

    try:
        status = await claws.status()
    except ServiceUnavailable as e:
        local = fleet.health_payload(SERVICE_NAME, illusion_version, boot_time)
        embed = presentation.make_embed(
            fleet.fleet_rows({"services": [{"state": "ok", **local}]}),
            field_names=fleet.FLEET_FIELD_NAMES,
            title="illusion",
            description=f"Could not reach claws, showing only this machine.\n{e}",
            colour=presentation.ALERT_COLOUR,
            vertical=False,
        )
        await interaction.followup.send(embed=embed)
        return

    skew = fleet.version_skew(status)

    embed = presentation.make_embed(
        fleet.fleet_rows(status),
        field_names=fleet.FLEET_FIELD_NAMES,
        title="illusion",
        description=skew or f"version {illusion_version}",
        colour=presentation.ALERT_COLOUR if skew else presentation.EMBED_COLOUR,
        vertical=False,
    )

    await interaction.followup.send(embed=embed)

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
    await archive_low_thread(cleaned_sku)

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

    response_message = render(await command_handler.handler_info(sku, hide_ext))
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

    await interaction.response.defer()

    if item_name == None:
        try:
            dkpn_info = await claws.digikey_part(digikey_part_number)
        except ServiceUnavailable as e:
            await interaction.followup.send(f"Could not look up {digikey_part_number}.\n{e}")
            return

        item_name = f"{dkpn_info["Product"]["Manufacturer"]["Name"]} {dkpn_info["Product"]["Description"]["ProductDescription"]}"

    if tags == None:
        tags = "per_item_tracking, digikey_dkpn"
    else:
        tags = f"per_item_tracking, digikey_dkpn, {tags}"

    response_message = await command_handler.handler_add_item(item_name, priority, order_quantity, "HYBRID", 
                                                              quantity, low_threshold, unit, 1, None, None, 
                                                              None, None, None, None, None, None, None, None, digikey_part_number, tags, notes,)

    await interaction.followup.send(response_message)

# Something about search makes discord hate it, no clue why -PC
@bot.tree.command(name="search", description="Search inventory by item name")
@app_commands.describe(name="Item name")
async def search(interaction: discord.Interaction, name: str):
    await interaction.response.defer()
    response_message = render(await command_handler.handler_search(name))

    await interaction.followup.send(embed=response_message)

@bot.tree.command(name="search_tag", description="Search inventory by tag")
@app_commands.describe(tag="Tag to search for")
async def search_tag(interaction: discord.Interaction, tag: str):
    await interaction.response.defer()

    response_message = render(await command_handler.handler_search_tag(
        tag,
    ))

    if isinstance(response_message, discord.Embed):
        await interaction.followup.send(embed=response_message)
    else:
        await interaction.followup.send(response_message)


@bot.tree.command(name="get_tags", description="List all item tags")
async def get_tags(interaction: discord.Interaction):
    await interaction.response.defer()

    response_message = render(await command_handler.handler_get_tags())

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
        try:
            async for event in claws.events():
                # Reconcile once subscribed, never before: anything that happens
                # while we catch up is queued rather than lost
                if event["event"] == "stream.connected":
                    await reconcile_low_threads(quiet=True)
                    continue

                try:
                    if event["event"] == "item.low":
                        await create_low_thread(event["sku"], event["item"])
                    elif event["event"] == "item.resolved":
                        await archive_low_thread(event["sku"], event["item"])
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
                thread_name = await create_low_thread(sku, item)

                if thread_name:
                    print(f"Reconciled {sku}: was low with no thread, opened {thread_name}")
                    fixed += 1
                else:
                    print(f"Could not reconcile {sku}: thread was not created")
            elif row["action"] == "archive":
                archived, detail = await archive_low_thread(sku, item)

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
                if event["event"] == "stream.connected":
                    continue

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
    if not PRINTING_ENABLED:
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
    if not PRINTING_ENABLED:
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
    if not PRINTING_ENABLED:
        await interaction.response.send_message(f"Printer not enabled")
        return

    response_message = presentation.queue_embed(await command_handler.handler_print_queue())

    if isinstance(response_message, discord.Embed):
        await interaction.response.send_message(embed=response_message)
    else:
        await interaction.response.send_message(response_message)

@bot.tree.command(name="print_resume", description="Resume the print queue after fixing the printer")
async def print_resume(interaction: discord.Interaction):
    if not PRINTING_ENABLED:
        await interaction.response.send_message(f"Printer not enabled")
        return

    await interaction.response.defer()

    response_message = await command_handler.handler_print_resume()
    await interaction.followup.send(response_message)

@bot.tree.command(name="print_cancel", description="Cancel a queued print job")
@app_commands.describe(job_id="Job id from /print_queue")
async def print_cancel(interaction: discord.Interaction, job_id: int):
    if not PRINTING_ENABLED:
        await interaction.response.send_message(f"Printer not enabled")
        return

    response_message = await command_handler.handler_print_cancel(job_id)
    await interaction.response.send_message(response_message)

@bot.tree.command(name="print_clear", description="Clear every job from the print queue")
async def print_clear(interaction: discord.Interaction):
    if not PRINTING_ENABLED:
        await interaction.response.send_message(f"Printer not enabled")
        return

    response_message = await command_handler.handler_print_clear()
    await interaction.response.send_message(response_message)

@bot.tree.command(name="printer_info", description="Get info about the printer")
async def printer_info(interaction: discord.Interaction):
    if not PRINTING_ENABLED:
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

    # Ask uvicorn to stop and give it a moment. Letting the loop tear it down
    # instead leaves its lifespan task to die unhandled, which prints a
    # CancelledError traceback on the way out.
    if health_server is not None:
        health_server.should_exit = True

        try:
            await asyncio.wait_for(health_task, timeout=3)
        except (TimeoutError, asyncio.CancelledError, Exception):
            pass

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

CONFIG_PATH = os.environ.get("ILLUSION_BOT_CONFIG", "./bot.yaml")

try:
    config = illusion_config.load(CONFIG_PATH)

    required = [
        "bot.claws.url",
        "bot.claws.token",
        "bot.discord.token",
        "bot.discord.server_id",
        "bot.discord.fourm_id",
    ]

    if illusion_config.get(config, "bot.printer.enabled"):
        required += ["bot.lipgloss.url", "bot.lipgloss.token"]

    illusion_config.require(config, required, source=CONFIG_PATH)
except illusion_config.ConfigError as e:
    print(e)
    raise SystemExit(1)

PRINTING_ENABLED = bool(illusion_config.get(config, "bot.printer.enabled"))

TOKEN = illusion_config.get(config, "bot.discord.token")
GUILD_ID = illusion_config.get(config, "bot.discord.server_id")
FORUM_CHANNEL_ID = illusion_config.get(config, "bot.discord.fourm_id")

claws = ClawsClient(
    illusion_config.get(config, "bot.claws.url"),
    illusion_config.get(config, "bot.claws.token"),
)

lipgloss = LipglossClient(
    illusion_config.get(config, "bot.lipgloss.url"),
    illusion_config.get(config, "bot.lipgloss.token"),
)

command_handler = DB_Commands(claws, lipgloss, boot_time)

SERVICE_NAME = "illusion-bot"
HOSTNAME = socket.gethostname()

# Co-located with claws on the VM, so this binds loopback and never touches the
# network
HEALTH_HOST = illusion_config.get(config, "bot.health.host", "127.0.0.1")
HEALTH_PORT = illusion_config.get(config, "bot.health.port", 8090)


@bot.event
async def setup_hook():
    install_signal_handlers()

    global health_server, health_task

    if HEALTH_PORT:
        health_server = fleet.make_health_server(
            fleet.make_health_app(SERVICE_NAME, illusion_version, boot_time,
                                  {"host": HOSTNAME}),
            HEALTH_HOST,
            HEALTH_PORT,
        )
        health_task = bot.loop.create_task(health_server.serve())

    bot.loop.create_task(fleet.announce(claws, SERVICE_NAME, illusion_version, boot_time, HOSTNAME))
    bot.loop.create_task(claws_event_loop())
    bot.loop.create_task(reconcile_loop())

    if PRINTING_ENABLED:
        bot.loop.create_task(lipgloss_event_loop())


def main():
    bot.run(TOKEN)


if __name__ == "__main__":
    main()
