import discord


EMBED_COLOUR = discord.Colour.from_rgb(r=192, g=140, b=149) #C08C95
ALERT_COLOUR = discord.Colour.from_rgb(r=176, g=74, b=74) #B04A4A

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

def make_table(data, exclude=None, field_names=None, vertical=None):
    # vertical=None lays a single row out as Field/Value and anything longer as
    # columns, pass True or False to force one or the other
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
    if vertical == True or (vertical == None and len(rows) == 1):
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


def make_embed(data, exclude=None, field_names=None, title=None, description=None, colour=None, row_name=None, vertical=None):
    # vertical=None lays a single row out one field per column and anything longer
    # as a field per row, pass True or False to force one or the other
    # row_name names each of those fields after that column, instead of "Result 1"
    missing = "N/A"
    inline = False

    if title == None:
        title = "Results:"

    if colour == None:
        colour = EMBED_COLOUR

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
            color=colour,
        )

    def friendly_name(field):
        return field_names.get(field, field)

    embed = discord.Embed(
        title=title,
        description=description,
        color=colour,
    )

    # Vertical Embed
    # Used when there is only one row.
    if vertical == True or (vertical == None and len(rows) == 1):
        row = rows[0]

        added_fields = 0

        for field in row:
            if field in exclude or field == row_name:
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
            if key not in columns and key not in exclude and key != row_name:
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

        # Best attempt at seperators
        lines.append("‎")

        embed.add_field(
            name=row.get(row_name) if row_name else f"Result {index}",
            value="\n".join(lines),
            inline=False,
        )

    return embed
