"""Discord rendering for illusion.

Everything here needs the discord library, which is why it lives in the
frontend rather than illusion-core: claws and lipgloss must never depend on
discord, and lipgloss now returns plain data for callers to render.
"""

import discord

from illusion_core.helpers import FIELD_NAMES, format_quantity, get_vendor_links, make_table

# Discord allows 25 fields per embed, and a queue that long is unreadable anyway
MAX_EMBED_JOBS = 20

QUEUE_FIELD_NAMES = {
    "JOB_ID": "Job",
    "DESCRIPTION": "Label",
    "LABELS": "Labels",
    "STATE": "State",
    "SOURCE": "Source",
}


EMBED_COLOUR = discord.Colour.from_rgb(r=192, g=140, b=149) #C08C95
ALERT_COLOUR = discord.Colour.from_rgb(r=176, g=74, b=74) #B04A4A


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


def notice_embed(title, description, urgent=False):
    """A plain title and description embed, for print updates that arent a list of jobs."""
    return discord.Embed(
        title=title,
        description=description,
        color=ALERT_COLOUR if urgent else EMBED_COLOUR,
    )


def queue_text(status):
    """Render lipgloss's queue status as a terminal table."""
    if not status["jobs"]:
        return f"{status['title']}\n{status['description']}"

    table = make_table(
        status["jobs"], exclude=["HEADER"], field_names=QUEUE_FIELD_NAMES, vertical=False
    )

    return f"{status['title']}\n{status['description']}\n{table}"


def queue_embed(status):
    """Render lipgloss's queue status as a Discord embed."""
    rows = list(status["jobs"])
    description = status["description"]
    paused = status["paused"]

    hidden = len(rows) - MAX_EMBED_JOBS

    if hidden > 0:
        rows = rows[:MAX_EMBED_JOBS]
        description = f"{description}\nOnly the first {MAX_EMBED_JOBS} are listed, {hidden} more behind them."

    if not rows:
        return notice_embed(status["title"], description, urgent=paused)

    # Job id and state are already in each field name
    return make_embed(
        rows,
        exclude=["JOB_ID", "STATE"],
        field_names=QUEUE_FIELD_NAMES,
        title=status["title"],
        description=description,
        colour=ALERT_COLOUR if paused else EMBED_COLOUR,
        row_name="HEADER",
        vertical=False,
    )
