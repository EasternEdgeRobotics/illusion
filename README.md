# illusion
A janky python based inventory system + discord bot.
Supports Linux and macOS only.

## Layout
illusion is a uv workspace of five packages, deployed as four services across
two machines. See `docs/split-plan.md` for why.

| Package | Runs on | Contents |
|---|---|---|
| `packages/illusion-core` | everywhere | Shared helpers, command layer, HTTP clients |
| `packages/claws` | NAS VM | Inventory database, DigiKey, low-stock events |
| `packages/lipgloss` | closet laptop | Label rendering, print queue, Niimbot |
| `packages/illusion-bot` | NAS VM | Discord bot |
| `packages/illusion-kiosk` | closet laptop | Terminal kiosk |

Each host installs only what it runs, so the laptop never pulls in discord.py
and the VM never pulls in pyserial:

```
uv sync --package claws          # then: uv run claws
uv sync --package illusion-bot   # then: uv run illusion-bot
uv sync --package lipgloss       # then: uv run lipgloss
uv sync --package illusion-kiosk # then: uv run illusion-kiosk
```

Every service reads its own config file (`claws.yaml`, `bot.yaml`,
`lipgloss.yaml`, `kiosk.yaml`), each with a committed `.example.yaml` alongside
it. Missing required values are reported by name at startup.

## Development
All four on one machine, from a single checkout:
```
uv sync
uv run claws & uv run lipgloss & uv run illusion-bot &
uv run illusion-kiosk
```

## Label Printing
Currently, only the Niimbot D110 is supported over USB, but other models could work with minor changes.


## Digikey Support
IF a bar code scanner supporting 2D data matrixes is used (such as the Tera D5100), illusion can automatically add items and increase the stock. This requires digikey API access.

To get the access token and refresh token, run `uv run digikey_client.py` on a seperate system to authenticate.