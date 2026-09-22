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

The token must match `lipgloss.token` in `lipgloss.yaml` on the printer host. On the kiosk itself lipgloss is on the same machine, so the default URL is already right and only the token needs filling in.

The settings window opens automatically on first launch, when no token is set.

## Reading the status line
`/health` is unauthenticated and `/queue` is not, which is deliberate over in lipgloss and is what lets the status line tell three failures apart:

| What you see | What it means |
|---|---|
| 🔴 lipgloss unreachable | Nothing answered. Wrong URL, wrong port, service down, or the tailnet is not up |
| 🟠 lipgloss up, token rejected | The service is running and healthy; the token does not match `lipgloss.yaml` |
| 🟠 queue paused | Connected and fine. The printer needs attention — the reason is under the queue heading |
| 🟢 connected | Working |

Without that split, a token typo and a dead service look identical, which on a kiosk means someone power-cycles a laptop that was never the problem.

## Keyboard

| Key | Does |
|---|---|
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

1. **Printing.** `POST /print` and `POST /print/barcodes`. The client class
   already has the shape for it; see `LipglossClient` in
   [`clients.py`](../packages/illusion-core/src/illusion_core/clients.py), which
   is the reference for every endpoint.
2. **Label preview.** `POST /preview` returns a PNG. `stb` is already vendored
   and wired into the build for exactly this — decode it and upload it as a
   texture.
3. **Queue control.** Resume, clear, and cancelling a single job.
4. **SSE.** `GET /events` replaces the one-second poll, so the queue updates the
   moment something changes rather than up to a second later.