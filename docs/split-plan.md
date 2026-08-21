# Splitting illusion into claws / lipgloss / illusion

Status: phases 0 to 4 done. The split itself is finished. Phase 5
(fleet status and deployment) is what remains. Target version 1.5.0.

Not 2.0.0: every slash command and kiosk command survives the split unchanged,
so from a user's seat nothing happens. The upheaval is entirely in deployment,
and the bot has only been live a day with almost nobody having used it yet.
All five packages version in lockstep at 1.5.0 -- independent per-package
versions are not worth the bookkeeping when the hosts are always deployed
together.

## Why

Everything currently runs in one process on the closet laptop: Discord gateway,
SQLite, terminal kiosk, and the USB label printer. The closet has poor wifi, and
a Discord gateway websocket is the single most latency- and dropout-sensitive
thing in the stack. Moving the bot and the database to a NAS VM puts the
long-lived network connection on good wifi and leaves only the two things that
must be physically in the closet -- the barcode scanner and the Niimbot -- on
the laptop.

The coupling is worse than "they share a process". `bot.run(TOKEN)` is the only
entry point, `setup_hook` fires only once the gateway connects, and
`terminal_loop` then waits on `wait_until_ready()` on top of that. The kiosk
accepts no input until Discord has completed a handshake, and `terminal_loop`
runs `while not bot.is_closed()`, so a dropped gateway ends the REPL. The print
queue worker starts in that same `setup_hook`. Today, bad wifi in the closet
takes out inventory scanning and label printing along with the bot.

## Topology

    NAS VM (Linux)                       Closet laptop (Alpine + sway)
    +-----------------------+            +------------------------------+
    | claws        :8080    |            | lipgloss          :8081      |
    |   inventory.db        |            |   label rendering            |
    |   DigiKey client      |            |   print queue                |
    |   SSE /events         |            |   niimbot over /dev/ttyACM0  |
    +-----------------------+            |   SSE /events                |
    | illusion-bot          |            +------------------------------+
    |   discord.py          |            | illusion-kiosk               |
    |   no listener         |            |   REPL + barcode scanner     |
    +-----------------------+            +------------------------------+
              |                                        |
              +----------------- tailscale ------------+

Link matrix:

| From           | To       | Over      | Notes                                  |
|----------------|----------|-----------|----------------------------------------|
| illusion-bot   | claws    | localhost | same VM, no network hop                |
| illusion-bot   | lipgloss | tailscale | print commands from Discord            |
| illusion-kiosk | claws    | tailscale | the only bad-wifi dependency           |
| illusion-kiosk | lipgloss | localhost | same laptop, printing survives outages |

Every component's availability improves; see Availability below. Where they
conflict, bot availability outranks kiosk availability.

## Repo layout

One repo, uv workspace. Each host clones the same repo and runs only its own
services.

    illusion/
    |-- pyproject.toml            [tool.uv.workspace] members
    |-- uv.lock                   single lockfile for everything
    |-- docs/split-plan.md
    `-- packages/
        |-- illusion-core/        shared: sku helpers, wire models, HTTP clients, config loader
        |-- claws/                inventory service          -> NAS VM
        |-- lipgloss/             print service              -> laptop
        |-- illusion-bot/         discord frontend           -> NAS VM
        `-- illusion-kiosk/       terminal frontend          -> laptop

Both frontends present as "illusion" to users: the bot's activity status and the
kiosk's startup banner both read `illusion 1.5.0`. Only the package names
distinguish them.

Deploy is `uv sync --package claws` etc. on each host, so the laptop never
installs discord.py and the VM never installs pyserial.

## Where the current code goes

| File today            | Goes to        | Notes                                                        |
|-----------------------|----------------|--------------------------------------------------------------|
| inventory_reader.py   | claws          | Pure SQLite, zero Discord/printer imports. Moves as-is.       |
| digikey_client.py     | claws          | Needs internet; NAS has the good link.                        |
| label_maker.py        | lipgloss       | Pure PIL. Moves as-is.                                        |
| print_queue.py        | lipgloss       | Strip the `discord` import; return JSON, not embeds.          |
| illusion_helpers.py   | split 3 ways   | see below                                                     |
| illusion.py           | split 3 ways   | see below                                                     |

illusion_helpers.py splits:

- `connect_printer`, `close_printer`, `check_printer*`, `niimbot_print`,
  `niimbot_printer_info`, `media_remaining`, `check_label`,
  `PrinterUnavailable`, `LabelError` -> lipgloss
- `make_embed`, `make_vendor_buttons`, `make_low_thread_content`,
  `EMBED_COLOUR`, `ALERT_COLOUR` -> illusion-bot
- `make_table` -> illusion-kiosk
- `clean_sku`, `format_quantity`, `get_vendor_links`, `FIELD_NAMES` -> illusion-core

illusion.py splits:

- `DB_Commands` handlers that touch `inventory` -> claws service layer, behind HTTP
- `DB_Commands` handlers that touch `printqueue` -> lipgloss service layer, behind HTTP
- slash commands, `on_ready`, `make_notifier`, thread create/archive -> illusion-bot
- `terminal_loop`, `terminal_print`, `terminal_notify`, the joanne hat -> illusion-kiosk
- `graceful_exit` / `install_signal_handlers` -> one copy per service, each
  shutting down only what it owns

## The coupling that has to break

`handler_decrease` currently sets `LOW` on the item and then calls
`create_low_thread`, which posts to a Discord forum channel. The backend cannot
do that once it lives on the NAS with no Discord knowledge.

Replacement: claws emits domain events on an SSE stream. The bot subscribes and
owns the thread lifecycle.

    claws:  POST /items/EER-000123/decrease
            -> stock crosses LOW_THRESHOLD
            -> responds to the caller with the new item state
            -> publishes {"event": "item.low", "sku": "EER-000123", ...} on /events

    bot:    holds an SSE connection to /events
            -> sees item.low
            -> creates the forum thread with vendor buttons
            -> PUT /items/EER-000123/low-thread {"thread_id": 123...}

`LOW_THREAD_ID` stays as a column (no schema migration needed) but is only ever
written through that one dedicated endpoint, so it is clear the bot owns it.
`item.resolved` drives archiving the same way.

Same pattern for print notifications. `notify=terminal_notify` and
`make_notifier(interaction)` become a `reply_to` token passed at submit time:

    kiosk:  POST /print {"style": "slim_barcode", "sku": "EER-000123",
                         "reply_to": "kiosk"}
    bot:    POST /print {..., "reply_to": "bot:<interaction_id>"}

lipgloss tags every job event with that token; each client filters its own off
the shared `/events` stream. No inbound connections to the kiosk needed.

## Fleet status (the `about` command)

`about` reports version, service uptime and host uptime for all four components,
from either frontend.

claws knows the fleet from static config -- three service URLs, since the set is
fixed and this is not dynamic service discovery. That static list is what makes
`about` survive a **claws restart**: an in-memory registry alone would forget
every service that had already announced itself, and a kiosk that has been idle
since before the restart would never re-announce until someone restarted it.

On top of that, each service still announces itself on connect, which warms the
cache immediately and makes a restart visible without waiting for someone to run
`about`:

    POST /register
    {"service": "illusion-kiosk", "version": "1.5.0", "host": "eer-inventory",
     "url": "http://eer-inventory.tailnet.ts.net:8082",
     "started_at": 1755739200000, "system_boot_at": 1755700000000}

That single message is enough for claws to report **version, service uptime and
system uptime indefinitely** -- uptime is just `now - started_at`, computed at
read time. No polling is needed to keep any of it current. The only thing
registration cannot tell you later is whether the service is still running.

So `about` resolves liveness on demand. claws fans out to every registered
service's `/health` concurrently with a ~3s budget:

    results = await asyncio.gather(*(probe(s) for s in registered),
                                   return_exceptions=True)

Anything that answers is `ok` and reports live values. Anything that times out,
refuses, or errors falls back to the last successful probe, rendered as assumed
rather than blanked:

    illusion-kiosk   1.5.0   uptime (assumed): 34d 4h 1s   last reached 12m ago

This is more useful than `Unknown`, which throws away real information. The
`(assumed)` label plus the last-reached time is what keeps it honest -- the
reader can see both the figure and how much to trust it. Only a service claws
has never successfully reached shows `Unknown`.

One slow host cannot stretch the command past the budget, because the probes run
in parallel.

Two details that make the assumed figure trustworthy:

- **Send durations, not timestamps.** `/health` returns `uptime_ms` and
  `system_uptime_ms` computed on the service's own clock, never an absolute
  `started_at` for claws to subtract from its own clock. The laptop is an Alpine
  machine that gets shut down and carried around, so its clock can drift from the
  VM's; a duration is immune to skew between hosts, an absolute timestamp is not.
  This is also exactly what the existing `handler_uptime` already computes. claws
  derives the assumed value by adding elapsed time since the last good probe.
- **Stop assuming after 24h.** Past that, drop the figure and show
  `Unknown -- last reached 3d ago`. An assumed uptime is only worth as much as
  the probe it extrapolates from, and claws adding elapsed time to a months-old
  probe produces a number that looks like data but is really just a stale cache
  entry -- which is the only realistic way to see an absurd value here, since
  real uptimes top out in the low hundreds of days. The cutoff removes that
  failure mode entirely, and means `format()` never needs a years unit.

For claws to probe a service, that service must be reachable. This is only
awkward for one of them:

- **lipgloss** is already an HTTP server. claws GETs its `/health` directly.
- **illusion-bot** is co-located with claws on the VM. Its health endpoint binds
  `127.0.0.1` and never touches the network.
- **illusion-kiosk** is otherwise a pure client with nothing listening. It gets a
  minimal listener -- a single read-only `GET /health`, bearer-gated, bound to
  its tailnet address, no other routes. FastAPI is already a dependency, so this
  is about ten lines. Tailscale provides the inbound path with no port
  forwarding.

Giving the kiosk one read-only endpoint is a smaller cost than the alternative,
which is plumbing request/response back down the SSE stream to avoid a listener.
It also keeps `/status` uniform: four services, one probe shape, no special
cases.

Two things this has to get right:

- **Never show a frozen uptime as healthy.** A service that has died must render
  as `Unknown`, not as a plausible-looking uptime that keeps counting. This is
  what lets `about` from Discord answer "is the closet machine alive?" --
  genuinely useful when the laptop is in a closet you are not standing next to.
- **Version skew.** All five packages version in lockstep, so two different
  numbers in the list means a host did not get redeployed. Call that out rather
  than quietly printing both.

If claws itself is unreachable, `about` still prints the local service's own
version and uptime and queries lipgloss directly -- both frontends already hold
a lipgloss client -- marking the rest `Unknown`.

The `platform.system() != "Linux"` branch in the current `handler_uptime` stays.
The services all run on Linux in production, but development happens on macOS,
where `/proc/uptime` does not exist.

## Service APIs

Both services are FastAPI. The auto-generated `/docs` page is worth the
dependency when you are debugging a service two hops away over tailscale, and
the pydantic models double as the wire contract that illusion-core shares.

### claws :8080

    GET    /health
    GET    /status                       fleet: probes every registered service
    POST   /register                     announce on startup (version, started_at, url)
    GET    /items                        read_all
    GET    /items/{sku}
    POST   /items                        -> {"sku": "EER-000123"}
    PATCH  /items/{sku}
    DELETE /items/{sku}
    POST   /items/{sku}/decrease         {"amount": null}
    POST   /items/{sku}/increase         {"amount": 1}
    PUT    /items/{sku}/stock            {"quantity": 12}
    POST   /items/{sku}/resolve
    PUT    /items/{sku}/low-thread       {"thread_id": 123}   bot only
    POST   /items/{sku}/tags             {"tag": "resistors"}
    GET    /items/by-dkpn/{dkpn}
    GET    /search?name=&limit=10
    GET    /tags
    GET    /tags/{tag}/items
    POST   /digikey/scan                 {"barcode": "[)>..."}
    GET    /events                       SSE: item.low, item.resolved, item.changed

### lipgloss :8081

    GET    /health                       {service, version, uptime_s, system_uptime_s}
    GET    /printer                      {"labels_left": 87, "labels_total": 120, "battery": 3}
    POST   /print                        {style, sku?, line_1?, line_2?, copies, source, reply_to}
    POST   /print/image                  multipart: file, description, copies, rotate
    POST   /print/barcodes               {"lower": 1, "upper": 20, source, reply_to}
    GET    /queue                        structured job list
    POST   /queue/resume
    POST   /queue/clear
    DELETE /queue/{job_id}
    GET    /events                       SSE: job.queued, job.progress, job.done, printer.fault

lipgloss needs no database access at all. Callers that want an item's name on a
label resolve it against claws first and pass literal text -- which is what the
`get_text_from_sku` slash-command option already does today. Keep it that way;
it is the cleanest boundary in the whole design.

## Transport and auth

Tailscale is the network, but do not rely on it as the only control:

- Bind both services to the tailnet address only, never `0.0.0.0`. On the
  laptop, lipgloss can bind `127.0.0.1` plus the tailnet IP.
- Static bearer token per service, in each host's config, checked by a FastAPI
  dependency. Cheap, and it means a misconfigured bind is not instantly fatal.
- Tailscale ACLs restricting which nodes may reach 8080 and 8081.

Client timeouts tuned for the bad link: 5s connect, 15s read, and retry only
idempotent GETs (2 attempts, backoff). Never auto-retry a decrease.

### Idempotency

With fail-fast chosen, a decrease that times out may or may not have applied,
and the human will retry it -- which can double-decrement. Cheap insurance:
claws accepts an optional `Idempotency-Key` header on mutating endpoints and
keeps a `key -> response` table for 24h. The kiosk generates one key per scan
and reuses it across manual retries.

## Availability

Priority: the bot matters more than the kiosk. Where a design choice trades one
against the other, the bot wins.

Every component ends up strictly more available than it is today:

| Failure                | Today                          | After the split                        |
|------------------------|--------------------------------|----------------------------------------|
| Discord unreachable    | kiosk and printing both dead   | kiosk and printing unaffected          |
| Closet wifi drops      | bot drops, kiosk dies with it  | bot fine on the NAS; kiosk scans fail  |
| NAS down               | n/a                            | kiosk scans fail, printing still works |
| Printer unplugged      | queue pauses, rest fine        | unchanged                              |

The kiosk trades a hard dependency on the Discord gateway for a dependency on
short HTTP calls to claws over tailscale. That is a better dependency in every
way: a request/response with a 15s timeout either succeeds or fails cleanly,
where a websocket that must stay up for hours degrades in ways that are much
harder to recover from. Printing stops depending on the network entirely, since
lipgloss runs standalone on the laptop.

On a claws outage: fail fast. The kiosk retries twice, then prints
`claws unreachable, scan not recorded` and returns to the prompt. Nothing is
buffered, nothing can silently diverge from the database. Label styles needing
no name lookup (`slim_barcode`, plain text) keep working throughout.

If outages turn out frequent enough to be painful, the upgrade path is a local
journal of stock changes replayed on reconnect -- the `Idempotency-Key` support
above is what makes that safe to add later.

## Migration phases

Each phase ends with a working system. Do not start the next until the current
one has run for a day.

**Phase 0 -- config handling. DONE.** `config.yaml` is tracked with empty secret
fields and kept out of commits with `git update-index --assume-unchanged`.
History is clean -- no commit has ever contained a real token. That works today
for one file, but the split turns one config into four, and assume-unchanged is
per-clone local state: a fresh checkout on the NAS VM silently gets the empty
template with no indication anything is missing, and the flag has to be re-set
by hand on every clone. Switch to gitignored real files alongside committed
`*.example.yaml` templates before creating the other three, and have each
service fail at startup with a named-field error if a required value is empty.

**Phase 1 -- restructure, no behaviour change. DONE.** 99.2% of lines kept
their original git blame; only genuinely rewritten lines moved to the
restructure commits.

Original text: Create the uv workspace and move
files into the five packages. `illusion` still runs as one process, importing
from the new packages. Nothing splits yet. Verify the kiosk, the bot, and a test
print all still work.

**Phase 2 -- lipgloss becomes a service. DONE.** Also dropped discord from
illusion-core entirely, which was listed as a Phase 2 side effect and turned out
to be the thing that made the rest clean.

Original text: Wrap the print queue in FastAPI, write
`LipglossClient` in illusion-core, point the still-monolithic illusion at
`http://127.0.0.1:8081`. Same machine, so a failure here is a bug in the client,
not the network. Deploy under OpenRC on the laptop.

**Phase 3 -- claws becomes a service. DONE.** Two things emerged that were not
in the original plan: responses can no longer name the low-stock thread they
triggered, and the bot has to reconcile orphaned threads at startup because
events are not replayed.

Original text: Same shape: FastAPI over
`SpreadsheetManager`, `ClawsClient` in illusion-core, illusion runs against
`http://127.0.0.1:8080` first, still on the laptop. Then move the database (see
below) and repoint at the tailnet address. This is the phase where the low-stock
event stream replaces the direct `create_low_thread` call.

**Phase 4 -- split the frontends. DONE.** The shared command layer moved to
illusion-core first, with the handlers that used to render returning data for
each frontend to format, since formatting an embed in core would have dragged
discord back into a package claws and lipgloss depend on.

Original text: illusion.py finally splits into
illusion-bot and illusion-kiosk, both now thin HTTP clients. Bot moves to the
NAS VM; kiosk stays on the laptop.

**Phase 5 -- harden.** Health checks, service restart policies, database backups.

### Moving the database

SQLite is in WAL mode, so do not copy the `.db` out from under a running
process. Stop illusion, then:

    sqlite3 inventory.db "PRAGMA wal_checkpoint(TRUNCATE);"
    ls inventory.db-wal inventory.db-shm    # should be gone or zero length
    scp inventory.db claws-vm:/var/lib/claws/

## Deployment specifics

### The closet network is hostile

The laptop sits on a building network that is not ours, with working
client-to-client LAN. Anything it binds on a non-loopback interface is reachable
by whoever else is on that network, and the bearer tokens are the only thing
between them and the print server.

- **Bind the tailnet address, never `0.0.0.0`.** lipgloss and the kiosk health
  endpoint have to be reachable from the VM, so they cannot be loopback, but
  `100.x.y.z` is reachable only over the tailnet while `0.0.0.0` is reachable
  from the whole building. Same for claws on the VM.
- **Services must start after tailscaled**, or the bind fails at boot because
  the interface does not exist yet. OpenRC: `depend() { need tailscaled }`. If
  that proves fragile, bind `0.0.0.0` and restrict the ports to the `tailscale0`
  interface with a firewall instead -- more robust, since it does not depend on
  start order or on a hardcoded address.
- **Tailscale SSH does not protect you here.** It claims port 22 on the tailnet
  IP only and leaves `sshd_config` untouched, so the system sshd keeps listening
  on the LAN with whatever config it has. Enabling Tailscale SSH and assuming
  you are covered is the trap.
- On the laptop, the simplest answer is to disable system sshd entirely
  (`rc-update del sshd`) and use Tailscale SSH, since physical access is the
  fallback and we have it. If sshd stays, `PasswordAuthentication no` is not
  enough on its own -- set `KbdInteractiveAuthentication no` as well, or PAM can
  still allow passwords through keyboard-interactive.

### SSH between the hosts (Alpine quirks)

Both hosts are Alpine, which splits OpenSSH into pieces and defaults differently
from most distros:

- **`scp` needs `openssh-sftp-server` on the receiving host.** OpenSSH 9.0+ runs
  scp over the SFTP protocol, and Alpine packages the server side separately.
  The `openssh` meta-package depends on it, so anything installed with
  `setup-alpine` or `apk add openssh` already has it -- scp to the kiosk laptop
  works today. Only an install that took `openssh-server` on its own would be
  missing it. Check with `apk info -e openssh-sftp-server` before relying on the
  database copy in "Moving the database"; `scp -O` forces the legacy protocol if
  it ever bites.
- **`rsync` is not installed either**, and the nightly backup needs it on both
  ends. It is unaffected by the sftp issue -- rsync speaks its own protocol over
  ssh.
- **`ssh-copy-id` lives in `openssh-client-common`**, not the server package. If
  it is missing, append the key by hand:

        ssh user@host 'mkdir -p ~/.ssh && chmod 700 ~/.ssh && \
            cat >> ~/.ssh/authorized_keys && chmod 600 ~/.ssh/authorized_keys' \
            < ~/.ssh/id_ed25519.pub

- **`doas`, not `sudo`**, and OpenRC, not systemd: `rc-service sshd restart`,
  `rc-update add sshd default`.
- **Default shell is busybox `ash`.** Keep scripts POSIX; no `#!/bin/bash`.

Decision: ordinary keys and system sshd, not Tailscale SSH. The macOS App Store
build of Tailscale ships no CLI on PATH, so every tailnet command would mean
digging out the binary inside the app bundle, and Tailscale SSH would not have
removed the need to harden sshd anyway.

Three key relationships, not one:

| From | To | For |
|------|----|-----|
| laptop (dev) | VM, kiosk | ordinary admin |
| VM | kiosk | the nightly backup rsync in "Backups" |

The VM to kiosk key is easy to forget until the backup silently fails. Consider
restricting it in the kiosk's `authorized_keys` with `restrict,command=...` so a
key sitting on the VM cannot do anything else with it.

If sshd is locked to key-only, confirm key login works first, in a second
terminal, before closing the first. The escape hatches differ: the VM has a
TrueNAS console, the closet laptop needs someone physically in the closet.


### Closet laptop (Alpine, sway autologin)

- **lipgloss** runs under OpenRC with `supervise-daemon` so it comes back after a
  crash and starts before the sway session. It must not depend on the graphical
  session.
- **Serial port.** `/dev/ttyACM*` on Alpine, not the `/dev/cu.usbmodem*` in the
  current config. The laptop runs eudev (`udevadm` present, `udev-postmount`
  started), so a udev rule is available if needed.

  niimprint's `SerialTransport("auto")` is not an option: it calls
  `list_comports()` and raises unless there is exactly one port. The laptop
  reports five -- `/dev/ttyS0` through `/dev/ttyS3` (legacy motherboard UARTs
  that always enumerate on x86, regardless of anything being attached) plus the
  printer on `/dev/ttyACM0`. The Tera D5100 scanner correctly does not appear;
  it is HID keyboard emulation, which is why the kiosk reads it through a plain
  `input()` call.

  So pin a stable symlink. The printer identifies as
  `YICHIP FS USB, VID:PID=3513:0001, SER=00000000050C` -- that serial is also
  the source of the `/dev/cu.usbmodem00000000050C1` in the current macOS config.

        # /etc/udev/rules.d/99-niimbot.rules
        SUBSYSTEM=="tty", ATTRS{idVendor}=="3513", ATTRS{idProduct}=="0001", \
            SYMLINK+="niimbot", GROUP="dialout", MODE="0660"

        udevadm control --reload-rules && udevadm trigger
        ls -l /dev/niimbot        # -> ../ttyACM0

  Then `port: /dev/niimbot` in lipgloss.yaml. If the team ever runs a second
  Niimbot, add `ATTRS{serial}=="00000000050C"` to disambiguate -- the VID/PID
  alone will match both.

  The lipgloss user also needs to be in `dialout` (`adduser lipgloss dialout`),
  since OpenRC runs it as a non-login user rather than inheriting your sway
  session. `/dev/ttyACM0` is already `crw-rw---- root dialout`, so this is the
  only permission change needed.
- **Kiosk** keeps the existing sway startup script, repointed at the
  illusion-kiosk entry point and wrapped so a crash restarts it rather than
  leaving an empty foot window:
  `while true; do illusion-kiosk; sleep 2; done`
- **Font.** The current config points at `/Users/peyton/Downloads/Roboto_Mono/...`,
  which does not exist on the laptop. Vendor the TTF into
  `packages/lipgloss/assets/` and make `font_path` an optional override.
- **Python 3.14 on musl** is already proven -- the current illusion runs on this
  laptop. No action needed.

### NAS VM

- Also Alpine, so OpenRC on both hosts and the same `supervise-daemon` pattern
  as lipgloss. One less thing to keep straight between the two machines.
- claws and illusion-bot as two services. illusion-bot waits on claws'
  `GET /health` at startup rather than relying on an OpenRC ordering directive,
  so it also survives claws restarting underneath it.
- illusion-bot must **not** wait on lipgloss at startup. The bot is the
  higher-priority service and the closet link is the unreliable one; a print
  command issued while lipgloss is unreachable fails on its own with a clear
  message, and every other slash command keeps working.
- Database at `/var/lib/claws/inventory.db`, owned by the claws user.
- **Backups.** Nightly cron on the VM, one script, three destinations. Take a
  `sqlite3 .backup` rather than snapshotting the live file -- a ZFS snapshot of a
  database mid-write is only crash-consistent, and `.backup` yields a clean file
  that opens with no WAL replay.

        DB=/var/lib/claws/inventory.db
        STAMP=$(date +%F)
        OUT=/backups/inventory-$STAMP.db

        sqlite3 "$DB" ".backup $OUT"          # 1. VM, on the TrueNAS dataset
        rsync -a "$OUT" laptop:/var/backups/claws/ \
            || logger -t claws-backup "laptop copy failed"   # 2. closet laptop
        find /backups -name 'inventory-*.db' -mtime +30 -delete

  1. `/backups` is a TrueNAS dataset mounted into the VM (virtio-fs or NFS), so
     ZFS snapshots and replication pick it up for free -- that is destination
     one and two, since the dataset is snapshotted independently of the VM.
  2. The laptop copy is the one that matters for the failure you are worried
     about. The VM runs *on* the NAS, so NAS hardware death takes the live
     database and every NAS-side backup with it. A copy on a physically separate
     machine in a different room is the only thing that survives that. Keep 7
     days there rather than 30; it is redundancy, not an archive.

  The `||` matters: the laptop is not always reachable (it is at home right now),
  and a failed copy must not abort the script or spam failures. It logs and moves
  on. If you want more than that later, have claws expose the age of the last
  successful laptop copy on `/health`.

  The database is 36 KB today, so none of this costs anything.

  Test a restore once, before you need it: `sqlite3` the backup, run
  `PRAGMA integrity_check;` and a `SELECT count(*) FROM items;`. An untested
  backup is a guess.

## Config

Each host gets its own file; the shared loader lives in illusion-core.

    claws.yaml       db path, digikey creds, bind address, bearer token
    lipgloss.yaml    bind address, bearer token, serial port, font override, model
    bot.yaml         discord token, guild/forum ids, claws url + token, lipgloss url + token
    kiosk.yaml       claws url + token, lipgloss url + token, prompt settings

No secret is duplicated across hosts except the two bearer tokens, and those are
per-service rather than one global shared secret.

## Remaining work

- **Fleet status.** The `about` command still reports only the local service.
  The design is written up above and unchanged; it is the last feature.
- **Deployment.** OpenRC services on both hosts, the udev rule, the backup cron,
  and moving the database onto the VM. None of it is written yet.

## Known cost

The kiosk package kept only 16% of its git blame, against 100% for the files
that were pure moves. `terminal_loop` came through intact, but
`handler_command_help` was dedented from a method to a module function, and
dedenting rewrites every line so copy detection cannot follow it. Doing the
extraction as two commits -- copy verbatim, then dedent -- would have preserved
it. Roughly 106 lines of static help text, so it was not worth rewriting
history to recover.

## Open items

None. Every prerequisite is confirmed.

Tailscale runs on both the TrueNAS host and inside the VM, and both can reach
the laptop. claws and illusion-bot bind the **VM's own** tailnet address, not
the host's -- the VM is a first-class tailnet node, so there is no reason to
route through the host or to port-forward anything. The host's tailscale stays
what it is: NAS management, and a path for backups that does not go through the
VM.

A relayed DERP path instead of a direct connection is acceptable and not worth
engineering around. Scans are episodic, not rapid-fire: a KANBAN item hitting
low (screws), a threshold crossing on a consumable (filament), or parts pulled
for an ROV. None of those is someone standing at the terminal running a queue of
barcodes where latency compounds. A slow kiosk is a much better failure than a
slow bot, and the bot is on the good link now. Worth running
`tailscale ping <vm>` from the closet once out of curiosity, but nothing hinges
on the answer.
