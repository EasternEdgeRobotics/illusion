"""illusion, the terminal kiosk.

Runs on the closet laptop next to the barcode scanner and the printer. It is a
pure client: it holds no database, drives no Discord connection, and does not
listen on a port except for the health endpoint the fleet asks about. When claws
is unreachable a command simply is not applied, and says so -- nothing is
buffered for a replay that could double count a scan later.
"""

import asyncio
import collections
import os
import signal
import time
from importlib.metadata import version

from illusion_core import config as illusion_config
from illusion_core import helpers as illusion_helpers
from illusion_core.clients import ClawsClient, LipglossClient, ServiceUnavailable
from illusion_core.commands import DB_Commands, Rows

illusion_version = version("illusion-kiosk")

boot_time = time.time()

shutdown_event = asyncio.Event()
shutdown_started = False

# Mirrors lipgloss's own limit
MAX_COPIES = 100

# Had to include at least 1 other reference
joanne_hat = r"""
      ▆▅▄▃▃▃▃▃▃▄▅▆      
      ▆▆▆▆▆▆▆▆▆▆▆▆      
     ▕░░░░░░░░░░░░▏     
 ▆▅▄▄▄▆▆▆▆▆▆▆▆▆▆▆▆▄▄▄▅▆ 
 ▆▆▆▆▆▆▆▆▆▆▆▆▆▆▆▆▆▆▆▆▆▆ 
  ▀▀▀▀▀▀▀▀▀▀▀▀▀▀▀▀▀▀▀▀ 
""".strip("\n")


def render(result):
    """Terminal rendering: strings pass through, Rows becomes a table."""
    if isinstance(result, Rows):
        return illusion_helpers.make_table(result.data, exclude=result.exclude)

    return result


def queue_text(status):
    """Render lipgloss's queue status as a terminal table."""
    if not isinstance(status, dict):
        return status

    if not status["jobs"]:
        return f"{status['title']}\n{status['description']}"

    table = illusion_helpers.make_table(
        status["jobs"],
        exclude=["HEADER"],
        field_names=illusion_helpers.QUEUE_FIELD_NAMES,
        vertical=False,
    )

    return f"{status['title']}\n{status['description']}\n{table}"


async def command_help():
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

    if PRINTING_ENABLED:
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
    register_notifier(TERMINAL_REPLY_TO, terminal_notify)

    print(f"illusion {illusion_version}")
    print("ready")

    while not shutdown_event.is_set():
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
                response_message = await command_help()

            elif command == "about" and len(parts) >= 1:
                bot_uptime, system_uptime = await command_handler.handler_uptime()
                text = f"""illusion \nversion: {illusion_version}\nkiosk uptime: {bot_uptime}\nsystem uptime: {system_uptime}""".strip("\n")
            
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
                response_message = render(await command_handler.handler_get_tags())
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
                response_message = render(await command_handler.handler_info(parts[1]))
            elif command == "search" and len(parts) >= 2:
                response_message = render(await command_handler.handler_search(parts[1]))
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
            elif command == "print_barcode" and PRINTING_ENABLED and len(parts) >= 2:
                if len(parts) != 3:
                    style = "slim_barcode"
                    sku = parts[1]
                    line_1 = None
                else:
                    style = "label_2_line"
                    sku = parts[1]
                    line_1 = parts[2]

                response_message = await command_handler.handler_print(style=style, text_line_1=line_1, sku=sku, reply_to=TERMINAL_REPLY_TO)
            elif command == "printer_info" and PRINTING_ENABLED and len(parts) >= 1:
                response_message = await command_handler.handler_printer_info()
            elif command == "print_label" and PRINTING_ENABLED and len(parts) >= 2:
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
            elif command == "bulk_print" and PRINTING_ENABLED and len(parts) == 3:
                response_message = await command_handler.handler_bulk_print_niimbot(parts[1], parts[2], reply_to=TERMINAL_REPLY_TO)
            elif command == "print_queue" and PRINTING_ENABLED and len(parts) >= 1:
                response_message = queue_text(await command_handler.handler_print_queue())
            elif command == "print_resume" and PRINTING_ENABLED and len(parts) >= 1:
                response_message = await command_handler.handler_print_resume()
            elif command == "print_clear" and PRINTING_ENABLED and len(parts) >= 1:
                response_message = await command_handler.handler_print_clear()
            elif command == "print_cancel" and PRINTING_ENABLED and len(parts) >= 2:
                response_message = await command_handler.handler_print_cancel(parts[1])
            elif command == "set" and len(parts) == 3:
                response_message = await command_handler.handler_set_stock(parts[1], parts[2])
            else:
                response_message = f"Invalid Command: {command}\n\nHelp:{await command_help()}"
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



# reply_to token -> handler. Bounded: a token is registered per print command
# and only jobs that finish cleanly remove theirs, so a printer left broken for
# a week must not grow this without limit.
notifiers = collections.OrderedDict()

MAX_NOTIFIERS = 100


def register_notifier(token, handler):
    notifiers[token] = handler
    notifiers.move_to_end(token)

    while len(notifiers) > MAX_NOTIFIERS:
        notifiers.popitem(last=False)


async def lipgloss_event_loop():
    """Route print updates back to the terminal.

    Reconnects on its own, because lipgloss restarting must not silently end
    print notifications for the rest of the session.
    """
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


async def graceful_exit(reason: str = "unknown"):
    global shutdown_started

    if shutdown_started:
        return

    shutdown_started = True
    print(f"Graceful exit requested: {reason}")

    shutdown_event.set()

    for client in (claws, lipgloss):
        try:
            await client.aclose()
        except Exception as e:
            print(f"Error closing client: {e}")


def install_signal_handlers():
    loop = asyncio.get_running_loop()

    for sig in (signal.SIGTERM, signal.SIGINT):
        loop.add_signal_handler(
            sig,
            lambda s=sig: asyncio.create_task(graceful_exit(s.name)),
        )


CONFIG_PATH = os.environ.get("ILLUSION_KIOSK_CONFIG", "./kiosk.yaml")

try:
    config = illusion_config.load(CONFIG_PATH)

    required = ["kiosk.claws.url", "kiosk.claws.token"]

    if illusion_config.get(config, "kiosk.printer.enabled"):
        required += ["kiosk.lipgloss.url", "kiosk.lipgloss.token"]

    illusion_config.require(config, required, source=CONFIG_PATH)
except illusion_config.ConfigError as e:
    print(e)
    raise SystemExit(1)

PRINTING_ENABLED = bool(illusion_config.get(config, "kiosk.printer.enabled"))

claws = ClawsClient(
    illusion_config.get(config, "kiosk.claws.url"),
    illusion_config.get(config, "kiosk.claws.token"),
)

lipgloss = LipglossClient(
    illusion_config.get(config, "kiosk.lipgloss.url"),
    illusion_config.get(config, "kiosk.lipgloss.token"),
)

command_handler = DB_Commands(claws, lipgloss, boot_time)


async def run():
    install_signal_handlers()

    if PRINTING_ENABLED:
        asyncio.create_task(lipgloss_event_loop())

    await terminal_loop()


def main():
    try:
        asyncio.run(run())
    except KeyboardInterrupt:
        pass


if __name__ == "__main__":
    main()
