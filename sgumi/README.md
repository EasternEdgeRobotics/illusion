# SGUMI - Super Graphic Ultra Modern Interface
The Super Graphic Ultra Modern Interface, a desktop frontend for [lipgloss](../packages/lipgloss).

## Building
Dependencies per platform are in [docs/Building.md](docs/Building.md). Clone with submodules first, then from the **repository root**:

```sh
cmake -S sgumi -B sgumi/build
cmake --build sgumi/build -j
```

The binary lands at `sgumi/build/sgumi` on Linux and Windows, and at`sgumi/build/sgumi.app` on macOS.

If `third_party/sdl` is empty, configure stops and tells you to run:

```sh
git submodule update --init --recursive
```

## Configuration
Unlike the Python services, SGUMI doesn't read a `.yaml` for it's config, and because it's a proper desktop app that gets launched from a menu, it stores it's config in the proper place for the OS it's running on:

| Platform | Path |
|---|---|
| macOS | `~/Library/Application Support/Eastern Edge/sgumi_config.json` |
| Windows | `%APPDATA%\Eastern Edge\sgumi_config.json` |
| else | `$XDG_CONFIG_HOME/eastern-edge/sgumi_config.json`, which is `~/.config/…` unless the session moved it |

`SGUMI_CONFIG_PATH` overrides all three, which is how you run two instances against two different lipgloss hosts.

Four settings, all editable under **SGUMI → Settings**:

```json
{
    "lipgloss_url": "http://127.0.0.1:8081",
    "lipgloss_token": "",
    "claws_url": "http://127.0.0.1:8080",
    "claws_token": ""
}
```

The tokens are separate secrets: `lipgloss_token` must match `lipgloss.yaml` on the printer host, `claws_token` must match `claws.yaml` on the inventory host.

Without claws every style still prints, you just type the text yourself.

The settings window opens automatically on first launch, when no lipgloss token is set.

## Printing
Seven styles, the same menu the bot's `/print` command offers. The boxes a style doesn't use are greyed out.

| Style                  | Needs       | Line 2   |
|------------------------|-------------|----------|
| Barcode                | sku         | —        |
| Label w/ Barcode       | sku, line 1 | —        |
| Label w/ QR Code       | sku, line 1 | optional |
| Label                  | line 1      | optional |
| Cable Label            | line 1      | optional |
| Cable Label w/ SKU     | sku, line 1 | optional |
| Cable Label w/ QR Code | sku, line 1 | —        |

**From claws** is on by default and fills line 1 with the item's name. It fires on Enter or when you click away, so scanning a SKU fills the name without touching the mouse. A short SKU is padded like [`clean_sku`](../packages/illusion-core/src/illusion_core/helpers.py) pads it, so `421` finds `EER-000421`.

The SKU box follows that toggle rather than the style, so you can look an item up for a style that puts no SKU on the label. Only styles that use one actually send it.

Jobs from here show a source of `sgumi` in the queue.

### Preview
The label at the top is rendered by lipgloss, so it is exactly what will print. It refreshes on its own whenever you leave a field, change the style, or a claws lookup fills line 1 — once per field, not per keystroke. There is no refresh button; if lipgloss was unreachable when something changed, the preview says so and updates on the next edit.

Before anything has been previewed it shows an example label, which is also a real render rather than a drawing. If lipgloss isn't up yet you get a blank label outline instead.

### Range print
Toggling **Range print** swaps the form for a start and end SKU, printing one label per SKU across the span. Both boxes take either `EER-000421` or `421`. It's for labelling a batch of items you've just added, without visiting each SKU one at a time.

Any style that puts the SKU on the label can be used; the ones that don't are left out of the picker, since a range of them would come out identical. For a style with a text line, each label's text is that item's name from claws — so the labels differ by more than their barcode.

SGUMI does that resolution and sends a name per SKU. lipgloss never asks claws anything itself, which is deliberate: its only dependency is the printer in front of it. A SKU claws has never heard of falls back to printing its own SKU, so one missing item doesn't fail a run of two hundred. A coverage line under the fields says how much of the range was named before you print.

lipgloss refuses the job if the roll can't fit the whole run.

## Reading the status line
`/health` is unauthenticated and `/queue` is not, which is deliberate over in lipgloss and is what lets the indicator tell three failures apart:

| What you see      | What it means |
|------------------ |---|
| 🔴 Unreachable    | Nothing answered. Wrong URL, wrong port, service down, or the tailnet is not up |
| 🟠 Token rejected | The service is running and healthy; the token does not match `lipgloss.yaml` |
| 🟠 Queue paused   | Connected and fine. The printer needs attention — the reason is under the queue heading |
| 🟢 Connected      | Working |

Without that split, a token typo and a dead service look identical, which on a kiosk means someone power-cycles a laptop that was never the problem.

While the queue is paused a **Resume queue** button appears under it. lipgloss answers that with its own account of what happened, which is shown verbatim — it returns success even when it could not resume, so "the queue is staying paused" is a normal reply and not an error.

The indicator only describes lipgloss. claws is on the About page instead, since it not being reachable doesn't stop anything printing.

## Keyboard
| Key | Does                          |
|-----|-------------------------------|
| F10 | Toggles the live theme editor |

Configure with `-DSGUMI_THEME_EDITOR=OFF` to leave it out of a build entirely.

## Origin
This is based off components of the [Software_2027](https://github.com/EasternEdgeRobotics/Software_2027) ROV frontend, at commit `ff6a7f6`. The window, the frame loop, the config-path resolution, the theme format, and the packaging rules were taken from it, while everything related to the ROV was dropped.

That is a **fork point, not a subscription**. Nothing here tracks that repo automatically, and the two are expected to diverge. To see what has changed in the bones since, run this in a Software_2027 checkout:

```sh
git diff ff6a7f6..HEAD -- apps/frontend/src/main.cpp \
                          apps/frontend/CMakeLists.txt \
                          libs/eer_gfx/src/Theme.cpp
```

The four vendored checkouts under `third_party/` are pinned to the same commits Software_2027 uses, so a bug reproduced there reproduces here.

## What's next
In rough order:

1. **The rest of queue control.** Resume is wired up; `POST /queue/clear` and `DELETE /queue/{id}` are not.
2. **SSE.** `GET /events` replaces the one-second poll.
3. **Printing an image.** `POST /print/image`, the one print endpoint SGUMI doesn't reach yet.
4. **Range styles in the bot.** The wire now carries `style` and per-SKU text, so the bot could offer what SGUMI does.

[`clients.py`](../packages/illusion-core/src/illusion_core/clients.py) is the reference for every endpoint.
