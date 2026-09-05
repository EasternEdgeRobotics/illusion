# Stable printer device on the kiosk laptop

The Niimbot enumerates as `/dev/ttyACM0`, but that number is not stable across replugs. A udev rule pins it to `/dev/niimbot` so `lipgloss.yaml` can point at something that does not move.

## The rule

Write it to a file. Do not paste the rule into a shell: `sh` reads `SUBSYSTEM=="tty",` as an assignment and then tries to run the next word as a command, giving `ATTRS{idVendor}==3513,: not found`.

```sh
cat > /etc/udev/rules.d/99-niimbot.rules <<'EOF'
SUBSYSTEM=="tty", ATTRS{idVendor}=="3513", ATTRS{idProduct}=="0001", SYMLINK+="niimbot", GROUP="dialout", MODE="0660"
EOF

udevadm control --reload-rules
udevadm trigger
```

## Verify

```sh
ls -l /dev/niimbot        # -> ../ttyACM0
```

If it is missing, unplug and replug the printer. `udevadm trigger` does not always re-fire for a device that is already attached.

To confirm the ids match this hardware:

```sh
udevadm info -a -n /dev/ttyACM0 | grep -m2 -E 'idVendor|idProduct'
```

Expect `3513` and `0001`. Note the pattern has no braces: busybox grep rejects `{}` as an interval expression, so `ATTRS{serial}` in a pattern fails.

## Use it

Set `printer.port: /dev/niimbot` in `lipgloss.yaml`, then
`rc-service lipgloss restart`.

`GROUP="dialout"` in the rule is what makes the symlink readable by lipgloss, which runs as `illusion:dialout` (see `deploy/openrc/lipgloss`). Without it the symlink appears but lipgloss cannot open it.