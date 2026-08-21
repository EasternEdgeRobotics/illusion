# illusion
A janky python based inventory system + discord bot.
Supports Linux and macOS only.

## Layout
illusion is a uv workspace split across four packages:

| Package | Contents |
|---|---|
| `packages/illusion-core` | Shared helpers, formatting, field names |
| `packages/claws` | Inventory database and DigiKey client |
| `packages/lipgloss` | Label rendering, print queue, Niimbot transport |
| `packages/illusion` | Discord bot and terminal kiosk |

They currently run as a single process. See `docs/split-plan.md` for the
migration that separates them into services.

## Installation
illusion is only supported with uv, if you have it installed, run:
```
uv sync
uv run illusion
```


## Label Printing
Currently, only the Niimbot D110 is supported over USB, but other models could work with minor changes.


## Digikey Support
IF a bar code scanner supporting 2D data matrixes is used (such as the Tera D5100), illusion can automatically add items and increase the stock. This requires digikey API access.

To get the access token and refresh token, run `uv run digikey_client.py` on a seperate system to authenticate.