# Working in this repo
illusion is Eastern Edge Robotics's inventory system: a parts database, a Discord bot, a terminal kiosk with a barcode scanner, a label printer service and a desktop label app.

## Naming
In prose the names are written `illusion`, `claws`, `Lipgloss` and `SGUMI`: illusion and claws fully lowercase, even at the start of a sentence, Lipgloss capitalised. All four come from songs and follow how those are written. SGUMI also stands for Super Graphic Ultra Modern Interface. Package names, module names, file names, config keys and URLs stay lowercase (`lipgloss`, `illusion-bot`, `illusion_core`, `sgumi`). Existing text is not consistent about Lipgloss. Leave it unless you are already rewriting that sentence.

illusion is both the whole system and what users see: the bot and the kiosk both present themselves as illusion. Only the package names tell them apart.

The possessive is "Eastern Edge Robotics's" and "Eastern Edge's".

## Layout
| Path | What | Runs on |
|---|---|---|
| `packages/illusion-core` | Shared helpers, config loader, the command layer both frontends run, and the HTTP clients for claws and Lipgloss | Everywhere |
| `packages/claws` | Inventory service: the SQLite database, DigiKey scans, low stock events | VM |
| `packages/illusion-bot` | Discord bot | VM |
| `packages/lipgloss` | Label rendering, print queue, the Niimbot over USB serial | Kiosk laptop |
| `packages/illusion-kiosk` | Terminal kiosk and barcode scanner input | Kiosk laptop |
| `sgumi/` | SGUMI, the C++ desktop frontend for Lipgloss. Its own docs are in `sgumi/README.md` and `sgumi/docs/` | Any desktop |
| `deploy/` | OpenRC services and the database backup script | |
| `docs/` | What belongs to no single package: versioning, the printer udev rule, the split design | |
| `assets/` | Icons. SGUMI picks its icon from here at build time | |

The VM and the kiosk laptop both run Alpine and are joined by Tailscale.

## Running
```sh
uv sync
uv run claws & uv run lipgloss & uv run illusion-bot & uv run illusion-kiosk
```
That runs everything on one machine from one checkout. A host in production installs only its own packages, `uv sync --package claws` and so on, so the kiosk never installs discord.py and the VM never installs pyserial.

Each service reads its own config from the working directory: `claws.yaml`, `bot.yaml`, `lipgloss.yaml`, `kiosk.yaml`. The real files are gitignored and hold secrets. Never commit one, and never print their contents into a reply or a log. Each has a committed `.example.yaml` beside it, and a new config field goes into the example too.

SGUMI builds from the repository root:
```sh
git submodule update --init --recursive
cmake -S sgumi -B sgumi/build
cmake --build sgumi/build -j
```

## Tests
```sh
uv run pytest
```
Each package keeps its tests in its own `tests/`. They use a temporary database and a fake Niimbot, so they need no config, hardware or network. The queue and service tests fake the printer at the functions in `lipgloss/printer.py` that open the serial port. `test_printer.py` fakes it one level lower, at niimprint's serial transport, so niimprint's own parsing and the error messages Lipgloss matches on are real. The illusion-core command tests run `DB_Commands` through the real `ClawsClient` into an in-process claws app, so a change to an endpoint shows up there.

A known bug is pinned as a test marked `xfail(strict=True)` with the reason. Fixing the bug makes that test pass, which fails the run until the marker is removed.

The tests do not replace running the services. Run what a change touches, and for SGUMI build and run it, then say what was run and what was not. The fake printer cannot show what the real Niimbot or the scanner does, so a change to either still needs the hardware, and say so when it was not checked.

## Services
The boundaries between the packages are the design. Keep them.

- claws owns the database. Nothing else opens it, and every read or write goes through claws's HTTP API.
- Lipgloss never touches the database. A caller that wants an item's name on a label looks it up in claws first and passes the text.
- illusion-core holds no rendering and never imports discord. Commands return data, and each frontend turns it into an embed or a terminal table. Code both frontends need goes in illusion-core. Code only one needs stays in that frontend.
- `illusion_core/clients.py` is the reference for every endpoint. SGUMI's `Claws.cpp` and `Lipgloss.cpp` mirror it, so an endpoint that changes there changes in SGUMI as well.
- Events go out on each service's `/events` SSE stream. The bot owns the low stock forum threads and reacts to claws's events, so claws never knows about Discord.
- The bot matters more than the kiosk. Where a choice trades one against the other, the bot wins. The bot never waits on Lipgloss at startup.
- Only idempotent GETs are retried. A stock decrease is never retried automatically.
- Services bind the tailnet address or loopback, never `0.0.0.0`. The kiosk laptop sits on a building network that is not ours.
- A required config value that is missing is reported by name at startup, through `illusion_core/config.py`, rather than failing at first use.

All five packages share one version and are bumped together, as `docs/versioning.md` describes. Two different versions in `about` means a host was not redeployed.

## Complexity
The most proper solution is not automatically the best one here. A thorough design that nobody on the team understands is how code becomes unmaintainable, and the team turns over most of its members every year.

That does not mean doing everything the simplest possible way. A crude solution that has to be worked around is its own maintenance cost. Aim between the two: as much structure as the problem needs, and no more than the people maintaining it can follow.

- When the proper version and the plain version are close, take the plain one. When the plain one would be fragile or repeated in several places, do it properly and keep it readable.
- Add an abstraction, an option or a platform branch when something needs it, not in case something might.
- If a design needs a long explanation before the code makes sense, treat that as a sign it is too much.

## Code
Python 3.14, managed with uv. 4 space indent, `snake_case` functions and modules, `PascalCase` classes, a docstring on each module saying what it is for. New dependencies go in the `pyproject.toml` of the one package that needs them, never the workspace root. The exception is test tooling such as pytest, which belongs to no package and sits in the root `dev` group.

SGUMI is C++20 with `#pragma once` and 4 space indent. `PascalCase` types and files, `camelCase` functions, `kConstant` constants. CMake uses 2 spaces and `SGUMI_` prefixed options. Warnings (`-Wall -Wextra -Wpedantic`) apply to SGUMI's own target, and the build is kept free of them.

`sgumi/third_party/` is never edited. Update a dependency by bumping its checkout. SGUMI forked from Eastern Edge's 2027 ROV frontend, and nothing here tracks that repo, so do not copy changes across unasked.

Lipgloss uses a fork of niimprint, [Peyton-C/niimprint](https://github.com/Peyton-C/niimprint), not the upstream package. The fork adds `label_capacity` to `get_rfid()`, because the printer's own `total_len` is wrong for some rolls, so Lipgloss breaks on upstream. A roll that reports the wrong count is fixed by adding its barcode to `KNOWN_LABEL_CAPACITIES` in the fork. Fixes to the printer protocol go in the fork too, never patched around in Lipgloss. `uv.lock` pins a fork commit, so run `uv lock --upgrade-package niimprint` after pushing one.

Deploy scripts run under busybox `ash` on Alpine: keep them POSIX, no `#!/bin/bash`. Alpine uses `doas` and OpenRC, not `sudo` and systemd.

## Comments
Short, plain, and only where the code does not explain itself. Do not explain a for loop.

A comment earns its place by saying why: the failure that motivated a line, the obvious approach that does not work, the thing that breaks if this is reordered. State it in a sentence or two and stop. The comments in `deploy/openrc/claws` are the model.

- No em dashes, and no `--` standing in for one. Use a comma or a second sentence. Older comments use `--`, so fix them when you are already rewriting the code they describe, not in passing.
- No banner comments or restating the function name.

## What goes where
`README.md` is what someone needs to install illusion and find their way around it, nothing more. It does not carry the depth.

Each component keeps its own docs beside it: SGUMI in `sgumi/`, deployment in `deploy/`. The top-level `docs/` is for what belongs to no single component. A doc should stand on its own rather than assume the reader came through another one.

Technical detail is welcome in docs, more so than in the README. Which endpoint a frontend calls and how the print queue pauses on a fault are mechanism and can be documented. The measurements and debugging that found them are evidence, and stay out under the rule below.

`docs/split-plan.md` is the design record for splitting illusion into services. Read it for why the boundaries are where they are. It is written as a plan and the code has moved past it in places, so where they disagree the code is right.

## Documentation voice
Docs follow the same measure as comments. Most of the docs here were written before this file and do not follow it yet. `docs/versioning.md` and `deploy/backups.md` are closest. Bring a doc into line when you rewrite it, rather than restyling one in passing.

- Be short. Roughly half the length of a first draft. State what the thing does and, if it is not obvious, why. Then stop.
- Cut the evidence, keep the consequence. The test is whether it changes what the reader does. A caveat that would change someone's choice stays in, even when it is unflattering: stop claws before restoring a backup, or the database is corrupted.
- Describe what a thing is now. Leave out where it came from, what it replaced and what is planned.
- Name the tool as the subject: "claws records...", "Lipgloss pauses...". Prefer that to passive voice.
- Address the reader directly for anything they do: "Set `printer.port`", "Stop claws first".
- Use a table for any fixed set a reader might look up: config fields, endpoints, packages, status indicators. Prose gets trimmed, tables do not.
- No em dashes, and no `--` standing in for one.
- No hard wrapping, one line per paragraph. No blank line between a heading and its text. No bold or italics in prose. Headings are short noun phrases.

Drafts written without this file in mind tend to need the same fixes: they run about twice the necessary length, wrap lines, lean on em dashes for asides, and explain the reasoning behind a decision where stating the decision would do.

## Commits
Work on whichever branch is checked out, check it with `git branch --show-current` rather than trusting an earlier snapshot, and never create one. Commit only when asked.

A subject line in the imperative, then a sentence or two on why, only when the diff does not make it obvious. Prefix with `SGUMI:` when a change touches only SGUMI. No essays, and no lists of touched files. Reasoning that needs more room belongs in a code comment beside the thing it explains.
