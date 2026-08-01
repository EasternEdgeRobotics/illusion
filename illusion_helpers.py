import discord
import os
import asyncio

from PIL import Image, ImageDraw, ImageFont
from niimprint import BluetoothTransport, PrinterClient, SerialTransport


FIELD_NAMES = {
    "SKU": "SKU",
    "NAME": "Name",
    "PRIORITY": "Priority",
    "ORDER_QUANTITY": "Order Qty",
    "TRACKING_MODE": "Tracking Mode",
    "QUANTITY_ON_HAND": "Quantity",
    "LOW_THRESHOLD": "Low Threshold",
    "LOW_THREAD_ID": "Low Thread",
    "UNIT": "Unit",
    "DECREASE_AMOUNT": "Decrease By",
    "LOW": "Low",
    "VENDOR_1": "Vendor 1",
    "LINK_1": "Link 1",
    "VENDOR_2": "Vendor 2",
    "LINK_2": "Link 2",
    "VENDOR_3": "Vendor 3",
    "LINK_3": "Link 3",
    "VENDOR_4": "Vendor 4",
    "LINK_4": "Link 4",
    "VENDOR_5": "Vendor 5",
    "LINK_5": "Link 5",
    "DIGIKEY_PART_NUMBER": "Digikey Part Num"
}

def format_quantity(value):
    if value is None:
        return "N/A"

    value = float(value)

    return f"{value:g}"


def make_low_thread_content(item):
    stock_lines = []

    if item["TRACKING_MODE"] != "KANBAN":
        stock_lines.extend(
            [
                f"Current Stock: {format_quantity(item['QUANTITY_ON_HAND'])} "
                f"{item['UNIT'] or ''}".strip(),
                f"Low Threshold: {format_quantity(item['LOW_THRESHOLD'])} "
                f"{item['UNIT'] or ''}".strip(),
            ]
        )

    return "\n".join(
        [
            f"We are getting low on: {item['NAME']}",
            f"SKU: {item['SKU']}",
            f"Tracking Mode: {item['TRACKING_MODE']}",
            f"Priority: {item['PRIORITY']}",
            f"Order Quantity: {item['ORDER_QUANTITY']}",
            *stock_lines,
        ]
    )

def get_vendor_links(item):
    vendor_links = []

    for vendor_number in range(1, 6):
        vendor = item.get(f"VENDOR_{vendor_number}")
        link = item.get(f"LINK_{vendor_number}")

        if link:
            label = vendor or f"Vendor {vendor_number}"

            vendor_links.append(
                {
                    "label": label[:80],
                    "url": link,
                }
            )

    return vendor_links


def make_vendor_buttons(item):
    vendor_links = get_vendor_links(item)

    if not vendor_links:
        return None

    view = discord.ui.View()

    for vendor in vendor_links:
        # Discord doesnt allow embeded links without http:// or https://, even though thats a pretty normal thing now, but discord sucks. -PC
        if vendor["url"].startswith("http"):
            url = vendor["url"]
        else:
            url = "http://" + vendor["url"]
        view.add_item(
            discord.ui.Button(
                label=vendor["label"],
                url=url,
                style=discord.ButtonStyle.link,
            )
        )

    return view

def clean_sku(sku):
    if type(sku) != str:
        sku = f"{sku}"
    if len(sku) <= 6:
        sku = "EER-" + ("0" * (6 - len(sku))) + sku
    return sku

def make_table(data, exclude=None, field_names=None):
    missing = "N/A"

    if exclude is None:
        exclude = [""]

    if field_names is None:
        field_names = FIELD_NAMES

    if isinstance(data, dict):
        rows = [data]
    else:
        rows = data

    if not rows:
        return ""

    def friendly_name(field):
        return field_names.get(field, field)

    # Vertical Table
    if len(rows) == 1:
        row = rows[0]

        field_header = "Field"
        value_header = "Value"

        table_data = []

        for field in row:
            if field not in exclude:
                value = row.get(field, missing)
                table_data.append((friendly_name(field), str(value)))

        if not table_data:
            return ""

        field_width = max(
            len(field_header),
            *(len(field) for field, _ in table_data),
        )

        value_width = max(
            len(value_header),
            *(len(value) for _, value in table_data),
        )

        header = (
            f"| {field_header.ljust(field_width)} "
            f"| {value_header.ljust(value_width)} |"
        )

        separator = f"| {'-' * field_width} | {'-' * value_width} |"

        body = []

        for field, value in table_data:
            body.append(
                f"| {field.ljust(field_width)} | {value.ljust(value_width)} |"
            )

        return "\n".join([header, separator] + body)

    # Horizontal Table
    columns = []

    for row in rows:
        for key in row:
            if key not in columns and key not in exclude:
                columns.append(key)

    if not columns:
        return ""

    string_rows = []

    for row in rows:
        string_row = {}

        for column in columns:
            string_row[column] = str(row.get(column, missing))

        string_rows.append(string_row)

    column_widths = {}

    for column in columns:
        display_column = friendly_name(column)
        max_cell_width = max(len(row[column]) for row in string_rows)
        column_widths[column] = max(len(display_column), max_cell_width)

    header_cells = []

    for column in columns:
        display_column = friendly_name(column)
        header_cells.append(display_column.ljust(column_widths[column]))

    header = "| " + " | ".join(header_cells) + " |"

    separator_cells = []

    for column in columns:
        separator_cells.append("-" * column_widths[column])

    separator = "| " + " | ".join(separator_cells) + " |"

    table_rows = []

    for row in string_rows:
        cells = []

        for column in columns:
            cells.append(row[column].ljust(column_widths[column]))

        table_rows.append("| " + " | ".join(cells) + " |")

    return "\n".join([header, separator] + table_rows)


def make_embed(data, exclude=None, field_names=None):
    missing = "N/A"
    title = "Results:"
    description = None
    color = discord.Color.pink()
    inline = False

    if exclude is None:
        exclude = [""]

    if field_names is None:
        field_names = FIELD_NAMES

    if isinstance(data, dict):
        rows = [data]
    else:
        rows = data

    if not rows:
        return discord.Embed(
            title=title or "No Results",
            description=description or "No data found.",
            color=color,
        )

    def friendly_name(field):
        return field_names.get(field, field)

    embed = discord.Embed(
        title=title,
        description=description,
        color=color,
    )

    # Vertical Embed
    # Used when there is only one row.
    if len(rows) == 1:
        row = rows[0]

        added_fields = 0

        for field in row:
            if field in exclude:
                continue

            value = row.get(field, missing)

            if value is None or value == "":
                value = missing

            embed.add_field(
                name=friendly_name(field),
                value=str(value),
                inline=inline,
            )

            added_fields += 1

        if added_fields == 0:
            embed.description = embed.description or "No displayable fields."

        return embed

    # Horizontal/List Embed
    # Used when there are multiple rows.
    columns = []

    for row in rows:
        for key in row:
            if key not in columns and key not in exclude:
                columns.append(key)

    if not columns:
        embed.description = embed.description or "No displayable fields."
        return embed

    for index, row in enumerate(rows, start=1):
        lines = []

        for column in columns:
            value = row.get(column, missing)

            if value is None or value == "":
                value = missing

            lines.append(f"**{friendly_name(column)}:** {value}")

        embed.add_field(
            name=f"Result {index}",
            value="\n".join(lines),
            inline=False,
        )

    return embed

def niimbot_print(img, addr, model):
    try:
        transport = SerialTransport(port=addr)
        printer = PrinterClient(transport)

        heartbeat = printer.heartbeat()
        media_info = printer.get_rfid()
    except Exception as e:
        err = str(e)

        if "could not open port" in err:
            return "Unable to print, printer is likely disconnected"
        elif "AttributeError: 'NoneType' object has no attribute 'data'" in err:
            return "Unable to print, printer is likely asleep"
        else:
            return f"Unable to print, Unknown Error: {err}"
    remaining_media = media_info["total_len"] - media_info["used_len"]

    if heartbeat["closingstate"] == 0:
        return "Unable to print, The printer seems to be open, please close it and try again."
    if remaining_media == 0:
        return "No labels left, please replace roll!"
    
    if model in ("b1", "b18", "b21"):
        max_width = 384
    elif model in ("d11", "d110"):
        max_width = 96

    image = Image.open(img)

    if image.width > max_width:
        return "Unable to print, image too wide"
    
    printer.print_image(image, density=3)
    return f"Printing...\nif this is the first print after returning from sleep it may be blank."

def niimbot_printer_info(addr):
    try:
        transport = SerialTransport(port=addr)
        printer = PrinterClient(transport)

        heartbeat = printer.heartbeat()
        media_info = printer.get_rfid()
    except Exception as e:
        err = str(e)

        if "could not open port" in err:
            return "Unable to get info, printer is likely disconnected"
        elif "AttributeError: 'NoneType' object has no attribute 'data'" in err:
            return "Unable to get info, printer is likely asleep"
        else:
            return f"Unable to get info, Unknown Error: {err}"
        
    if media_info != None:
        remaining_media = media_info["total_len"] - media_info["used_len"]

        return f"Labels left: {remaining_media}/{media_info["total_len"]}\nBattery Level: {heartbeat["powerlevel"]}/4"
    else:
        return "Unable to get printer info, labels might not be loaded."

async def bulk_niimbot_print(addr, model, label_maker, sku_lower, sku_upper):
    try:
        transport = SerialTransport(port=addr)
        printer = PrinterClient(transport)

        heartbeat = printer.heartbeat()
        media_info = printer.get_rfid()
    except Exception as e:
        err = str(e)

        if "could not open port" in err:
            return "Unable to print, printer is likely disconnected"
        elif "AttributeError: 'NoneType' object has no attribute 'data'" in err:
            return "Unable to print, printer is likely asleep"
        else:
            return f"Unable to print, Unknown Error: {err}"
        
    remaining_media = media_info["total_len"] - media_info["used_len"]
    total_prints = len(range(int(sku_lower), (int(sku_upper) + 1)))

    if heartbeat["closingstate"] == 0:
        return "Unable to print, The printer seems to be open, please close it and try again."
    if remaining_media == 0:
        return "No labels left, please replace roll!"

    if total_prints > int(media_info["total_len"]):
        return f"This exceeds the max amount of prints possible on a single roll.\nPlease split this into smaller jobs. \n{total_prints} requested, {media_info["total_len"]} possible"
    elif total_prints > remaining_media:
        return f"This exceeds the amounts of prints left on the current roll.\nPlease split this into smaller jobs. \n{total_prints} requested, {remaining_media} available"

    
    if model in ("b1", "b18", "b21"):
        max_width = 384
    elif model in ("d11", "d110"):
        max_width = 96

    images = {}

    # Pre generate images
    for i in range(int(sku_lower), (int(sku_upper) + 1)):
        sku = clean_sku(i)
        images[sku] = label_maker.render_label(style_name="barcode", width=320, height=96, rotate=90, output=f"barcode_{sku}", sku=sku)

    for key, value in images.items():
        image = Image.open(value)

        if image.width > max_width:
            return "Software Error: Unable to print, image too wide"
    
        printer.print_image(image, density=3)
        print(f"{key}: Printing...")

        # niimbot cant instantly take a new job, so we give it extra time between each one
        await asyncio.sleep(1) 

    return f"Finished"