"""The small shared modules: config, events, fleet, uptime and helpers."""

import asyncio
import os

import pytest
import yaml

from illusion_core import config, fleet, helpers
from illusion_core.events import EventBus
from illusion_core.uptime import format_duration


class TestConfig:
    def test_missing_file(self, tmp_path):
        with pytest.raises(config.ConfigError, match="not found"):
            config.load(tmp_path / "claws.yaml")

    def test_bad_yaml(self, tmp_path):
        path = tmp_path / "claws.yaml"
        path.write_text("claws: [")

        with pytest.raises(config.ConfigError, match="not valid YAML"):
            config.load(path)

    def test_every_missing_value_is_named(self, tmp_path):
        path = tmp_path / "bot.yaml"
        path.write_text(yaml.safe_dump({"bot": {"discord": {"token": ""}, "claws": {"url": "x"}}}))

        with pytest.raises(config.ConfigError) as caught:
            config.load(path, required=["bot.discord.token", "bot.claws.url", "bot.claws.token"])

        message = str(caught.value)
        assert "bot.discord.token" in message
        assert "bot.claws.token" in message
        assert "bot.claws.url" not in message

    def test_get(self):
        values = {"a": {"b": {"c": 1}}, "flat": "x"}

        assert config.get(values, "a.b.c") == 1
        assert config.get(values, "a.missing", "default") == "default"
        assert config.get(values, "flat.deeper") is None

    def test_add_defaults_writes_only_missing_options(self, tmp_path):
        path = tmp_path / "kiosk.yaml"
        path.write_text(yaml.safe_dump({"kiosk": {"terminal": {"idle_clear_minutes": 5}}}))
        path.chmod(0o600)
        values = config.load(path)

        config.add_defaults(values, path, {
            "kiosk.terminal.idle_clear_minutes": 30,
            "kiosk.health.port": 8082,
        })

        on_disk = yaml.safe_load(path.read_text())
        assert on_disk["kiosk"]["terminal"]["idle_clear_minutes"] == 5
        assert on_disk["kiosk"]["health"]["port"] == 8082
        assert values == on_disk
        assert path.stat().st_mode & 0o777 == 0o600

    @pytest.mark.skipif(os.geteuid() == 0, reason="root can write to a read-only directory")
    def test_add_defaults_survives_a_read_only_directory(self, tmp_path, capsys):
        directory = tmp_path / "locked"
        directory.mkdir()
        path = directory / "kiosk.yaml"
        path.write_text("kiosk: {}\n")
        directory.chmod(0o500)

        try:
            values = config.load(path)
            config.add_defaults(values, path, {"kiosk.health.port": 8082})
        finally:
            directory.chmod(0o700)

        assert values["kiosk"]["health"]["port"] == 8082
        assert path.read_text() == "kiosk: {}\n"
        assert "Could not add new options" in capsys.readouterr().out


class TestEventBus:
    def test_fan_out(self):
        bus = EventBus()
        first, second = bus.subscribe(), bus.subscribe()

        bus.publish({"event": "x"})

        assert first.queue.get_nowait() == second.queue.get_nowait() == {"event": "x"}

    def test_slow_subscriber_is_evicted_and_told(self):
        bus = EventBus()
        slow = bus.subscribe()
        slow.queue = asyncio.Queue(maxsize=1)

        bus.publish({"event": "1"})
        bus.publish({"event": "2"})

        assert slow.dropped.is_set()
        assert bus.subscriber_count == 0

    def test_unsubscribe(self):
        bus = EventBus()
        bus.unsubscribe(bus.subscribe())
        bus.publish({"event": "x"})

        assert bus.subscriber_count == 0


class TestFleet:
    def test_never_reached(self):
        assert fleet.cached_entry("lipgloss", None, 0) == {"service": "lipgloss", "state": "unknown", "version": None}

    def test_assumed_uptime_is_extended_by_the_age(self):
        cached = {"payload": {"version": "1.6.0", "uptime_ms": 1000, "system_uptime_ms": None}}

        entry = fleet.cached_entry("lipgloss", cached, 500)

        assert entry["state"] == "assumed"
        assert entry["uptime_ms"] == 1500
        assert entry["system_uptime_ms"] is None

    def test_stale_cache_drops_the_uptime(self):
        cached = {"payload": {"version": "1.6.0", "uptime_ms": 1000}}

        entry = fleet.cached_entry("lipgloss", cached, (fleet.ASSUME_LIMIT_S + 1) * 1000)

        assert entry["state"] == "unknown"
        assert "uptime_ms" not in entry

    def test_rows_and_skew(self):
        status = {"services": [
            {"service": "claws", "state": "ok", "version": "1.6.0", "uptime_ms": 61000},
            {"service": "lipgloss", "state": "assumed", "version": "1.5.0", "uptime_ms": 1000, "last_reached_ms": 3_600_000},
            {"service": "illusion-kiosk", "state": "unknown", "version": None},
        ]}

        rows = {row["SERVICE"]: row for row in fleet.fleet_rows(status)}

        assert rows["claws"]["UPTIME"] == "1m 1s"
        assert rows["lipgloss"]["STATE"] == "last reached 1h ago"
        assert rows["illusion-kiosk"]["STATE"] == "never reached"
        assert fleet.version_skew(status) == "Version mismatch across the fleet: 1.5.0, 1.6.0"

    def test_no_skew_ignores_unknown(self):
        status = {"services": [
            {"service": "claws", "state": "ok", "version": "1.6.0"},
            {"service": "lipgloss", "state": "unknown", "version": "1.5.0"},
        ]}

        assert fleet.version_skew(status) is None


@pytest.mark.parametrize("ms, text", [
    (None, "Unknown"), (0, "0s"), (59_000, "59s"), (3_600_000, "1h"), (90_061_000, "1d 1h 1m 1s"),
])
def test_format_duration(ms, text):
    assert format_duration(ms) == text


class TestHelpers:
    @pytest.mark.parametrize("given, sku", [
        ("1", "EER-000001"), (42, "EER-000042"), ("EER-000042", "EER-000042"), ("123456", "EER-123456"),
    ])
    def test_clean_sku(self, given, sku):
        assert helpers.clean_sku(given) == sku

    @pytest.mark.parametrize("value, text", [(None, "N/A"), (3.0, "3"), (2.5, "2.5"), ("4", "4")])
    def test_format_quantity(self, value, text):
        assert helpers.format_quantity(value) == text

    def test_vendor_links_skip_slots_without_a_link(self):
        item = {"VENDOR_1": "Pololu", "LINK_1": "pololu.com", "VENDOR_2": "No link", "LINK_3": "x.com"}

        assert helpers.get_vendor_links(item) == [
            {"label": "Pololu", "url": "pololu.com"},
            {"label": "Vendor 3", "url": "x.com"},
        ]

    def test_single_row_table_is_vertical(self):
        table = helpers.make_table({"SKU": "EER-000001", "NAME": "Tape", "NOTES": "x"}, exclude=["NOTES"])

        assert table.splitlines() == [
            "| Field | Value      |",
            "| ----- | ---------- |",
            "| SKU   | EER-000001 |",
            "| Name  | Tape       |",
        ]

    def test_many_rows_are_horizontal(self):
        table = helpers.make_table([{"SKU": "EER-000001", "NAME": "Tape"}, {"SKU": "EER-000002"}])

        assert table.splitlines() == [
            "| SKU        | Name |",
            "| ---------- | ---- |",
            "| EER-000001 | Tape |",
            "| EER-000002 | N/A  |",
        ]

    def test_empty(self):
        assert helpers.make_table([]) == ""
        assert helpers.make_table({"A": 1}, exclude=["A"]) == ""
