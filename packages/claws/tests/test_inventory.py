"""SpreadsheetManager against a real SQLite file: stock rules, tags, locations, search."""

import sqlite3

import pytest

from claws.inventory_reader import SpreadsheetManager


def quantity_item(inventory, name="Resistor", on_hand=10, threshold=None, decrease=None, **fields):
    return inventory.add_item({
        "NAME": name,
        "TRACKING_MODE": "QUANTITY",
        "QUANTITY_ON_HAND": on_hand,
        "LOW_THRESHOLD": threshold,
        "DECREASE_AMOUNT": decrease,
        **fields,
    })


class TestAddItem:
    def test_skus_count_up_from_the_highest(self, inventory):
        assert inventory.add_item({"NAME": "A"}) == "EER-000001"
        assert inventory.add_item({"NAME": "B"}) == "EER-000002"

        inventory.delete_item("EER-000001")

        assert inventory.add_item({"NAME": "C"}) == "EER-000003"

    def test_defaults(self, inventory):
        item = inventory.get_item(inventory.add_item({"NAME": "Tape"}))

        assert item["TRACKING_MODE"] == "KANBAN"
        assert item["DECREASE_AMOUNT"] == 1.0
        assert item["LOW"] is False
        assert item["QUANTITY_ON_HAND"] is None

    def test_name_is_required(self, inventory):
        with pytest.raises(ValueError, match="NAME"):
            inventory.add_item({"NAME": ""})

    def test_tracking_mode_is_normalised_and_checked(self, inventory):
        sku = inventory.add_item({"NAME": "Wire", "TRACKING_MODE": " hybrid "})
        assert inventory.get_item(sku)["TRACKING_MODE"] == "HYBRID"

        with pytest.raises(ValueError, match="TRACKING_MODE"):
            inventory.add_item({"NAME": "Wire", "TRACKING_MODE": "BY_WEIGHT"})

    def test_vendors_are_stored_by_slot(self, inventory):
        sku = inventory.add_item({
            "NAME": "Servo", "VENDOR_1": "Pololu", "LINK_1": "pololu.com", "LINK_3": "example.com",
        })
        item = inventory.get_item(sku)

        assert (item["VENDOR_1"], item["LINK_1"]) == ("Pololu", "pololu.com")
        assert (item["VENDOR_3"], item["LINK_3"]) == (None, "example.com")
        assert item["LINK_2"] is None


class TestUpdateItem:
    def test_unknown_sku(self, inventory):
        assert inventory.update_item("EER-999999", {"NAME": "x"}) is False

    def test_unknown_field_is_refused(self, inventory):
        sku = inventory.add_item({"NAME": "A"})

        with pytest.raises(ValueError, match="BOGUS"):
            inventory.update_item(sku, {"BOGUS": 1})

    def test_sku_cannot_be_changed(self, inventory):
        sku = inventory.add_item({"NAME": "A"})
        inventory.update_item(sku, {"SKU": "EER-000099", "NAME": "B"})

        assert inventory.get_item(sku)["NAME"] == "B"
        assert inventory.get_item("EER-000099") is None

    @pytest.mark.parametrize("value, expected", [
        ("TRUE", True), ("yes", True), (1, True), (True, True),
        ("FALSE", False), ("no", False), (0, False), (None, False),
    ])
    def test_low_accepts_the_usual_spellings(self, inventory, value, expected):
        sku = inventory.add_item({"NAME": "A"})
        inventory.update_item(sku, {"LOW": value})

        assert inventory.get_item(sku)["LOW"] is expected

    def test_vendor_half_update_keeps_the_other_half(self, inventory):
        sku = inventory.add_item({"NAME": "A", "VENDOR_2": "Adafruit", "LINK_2": "adafruit.com"})
        inventory.update_item(sku, {"LINK_2": "adafruit.com/new"})

        item = inventory.get_item(sku)
        assert (item["VENDOR_2"], item["LINK_2"]) == ("Adafruit", "adafruit.com/new")


class TestStock:
    def test_kanban_decrease_marks_low_once(self, inventory):
        sku = inventory.add_item({"NAME": "Zip ties"})

        first = inventory.decrease_item(sku)
        second = inventory.decrease_item(sku)

        assert first["low_changed"] is True
        assert first["quantity_changed"] is False
        assert second["low_changed"] is False
        assert inventory.get_item(sku)["LOW"] is True
        assert inventory.get_item(sku)["QUANTITY_ON_HAND"] is None

    def test_decrease_uses_the_items_own_amount(self, inventory):
        sku = quantity_item(inventory, on_hand=10, decrease=3)

        result = inventory.decrease_item(sku)

        assert (result["old_quantity"], result["new_quantity"]) == (10, 7)
        assert result["decrease_amount"] == 3

    def test_decrease_never_goes_negative(self, inventory):
        sku = quantity_item(inventory, on_hand=2)

        assert inventory.decrease_item(sku, 5)["new_quantity"] == 0

    def test_decrease_crosses_the_threshold(self, inventory):
        sku = quantity_item(inventory, on_hand=6, threshold=5)

        assert inventory.decrease_item(sku, 1)["low_changed"] is True
        # Already low, so the next one is not a new crossing
        assert inventory.decrease_item(sku, 1)["low_changed"] is False

    def test_without_a_threshold_low_means_empty(self, inventory):
        sku = quantity_item(inventory, on_hand=2)

        assert inventory.decrease_item(sku, 1)["low_changed"] is False
        assert inventory.decrease_item(sku, 1)["low_changed"] is True

    @pytest.mark.parametrize("amount", [0, -1])
    def test_decrease_amount_must_be_positive(self, inventory, amount):
        sku = quantity_item(inventory)

        with pytest.raises(ValueError):
            inventory.decrease_item(sku, amount)

    def test_set_stock_recomputes_low_both_ways(self, inventory):
        sku = quantity_item(inventory, on_hand=10, threshold=3)

        assert inventory.set_stock(sku, 3)["LOW"] is True
        assert inventory.set_stock(sku, 4)["LOW"] is False

    def test_increase_adds_and_can_clear_low(self, inventory):
        sku = quantity_item(inventory, on_hand=1, threshold=2)
        inventory.decrease_item(sku, 1)

        item = inventory.increase_item(sku, 5)

        assert item["QUANTITY_ON_HAND"] == 5
        assert item["LOW"] is False

    def test_increase_amount_must_be_positive(self, inventory):
        sku = quantity_item(inventory)

        with pytest.raises(ValueError):
            inventory.increase_item(sku, 0)

    def test_missing_sku(self, inventory):
        assert inventory.decrease_item("EER-999999") is None
        assert inventory.increase_item("EER-999999", 1) is None
        assert inventory.set_stock("EER-999999", 1) is None


class TestVendors:
    def test_add_vendor_fills_the_first_free_slot(self, inventory):
        sku = inventory.add_item({"NAME": "A", "LINK_1": "one.com"})

        assert inventory.add_vendor(sku, "Two", "two.com") is True
        assert inventory.get_item(sku)["LINK_2"] == "two.com"

    def test_add_vendor_stops_at_five(self, inventory):
        sku = inventory.add_item({"NAME": "A", **{f"LINK_{n}": f"{n}.com" for n in range(1, 6)}})

        assert inventory.add_vendor(sku, "Six", "six.com") is False


class TestTags:
    def test_add_tag_ignores_case_duplicates(self, inventory):
        sku = inventory.add_item({"NAME": "A", "TAGS": "Fasteners"})

        assert inventory.add_tag(sku, "fasteners") is True
        assert inventory.add_tag(sku, "M3") is True
        assert inventory.get_item_tags(sku) == ["Fasteners", "M3"]

    def test_add_tag_refuses_commas_and_blanks(self, inventory):
        sku = inventory.add_item({"NAME": "A"})

        assert inventory.add_tag(sku, "   ") is False

        with pytest.raises(ValueError):
            inventory.add_tag(sku, "a, b")

    def test_get_tags_counts_across_spellings(self, inventory):
        inventory.add_item({"NAME": "A", "TAGS": "BlueRobotics, M3"})
        inventory.add_item({"NAME": "B", "TAGS": "bluerobotics"})

        assert inventory.get_tags() == [
            {"TAG": "BlueRobotics", "COUNT": 2},
            {"TAG": "M3", "COUNT": 1},
        ]

    def test_items_by_tag_match_whole_tags(self, inventory):
        inventory.add_item({"NAME": "A", "TAGS": "BlueRobotics"})
        inventory.add_item({"NAME": "B", "TAGS": "BlueRoboticsSpares"})

        assert [item["NAME"] for item in inventory.get_items_by_tag("bluerobotics")] == ["A"]

    def test_tag_rename_merges_duplicates(self, inventory):
        sku = inventory.add_item({"NAME": "A", "TAGS": "Blue Robotics, BlueRobotics, M3"})
        other = inventory.add_item({"NAME": "B", "TAGS": "BlueRoboticsSpares"})

        changes = inventory.preview_tag_rename("bluerobotics", "Blue Robotics")

        assert [change["SKU"] for change in changes] == [sku]
        assert changes[0]["NEW_TAGS"] == "Blue Robotics, M3"

        inventory.apply_tag_rename(changes)

        assert inventory.get_item_tags(sku) == ["Blue Robotics", "M3"]
        assert inventory.get_item_tags(other) == ["BlueRoboticsSpares"]


class TestRename:
    def test_preview_is_case_insensitive_by_default(self, inventory):
        inventory.add_item({"NAME": "Capasiter 10uF"})
        inventory.add_item({"NAME": "CAPASITER 1uF"})

        changes = inventory.preview_rename("capasiter", "Capacitor")

        assert sorted(change["NEW_NAME"] for change in changes) == ["Capacitor 10uF", "Capacitor 1uF"]

    def test_case_sensitive(self, inventory):
        inventory.add_item({"NAME": "Capasiter"})
        inventory.add_item({"NAME": "capasiter"})

        assert len(inventory.preview_rename("Capasiter", "Capacitor", case_sensitive=True)) == 1

    def test_preview_writes_nothing_and_apply_does(self, inventory):
        sku = inventory.add_item({"NAME": "Capasiter"})

        changes = inventory.preview_rename("Capasiter", "Capacitor")
        assert inventory.get_item(sku)["NAME"] == "Capasiter"

        inventory.apply_rename(changes)
        assert inventory.get_item(sku)["NAME"] == "Capacitor"

    def test_regex_characters_are_literal(self, inventory):
        inventory.add_item({"NAME": "M3 x 12mm (black)"})

        assert inventory.preview_rename(".", "!") == []
        assert inventory.preview_rename("(black)", "black")[0]["NEW_NAME"] == "M3 x 12mm black"


class TestLocations:
    @pytest.mark.parametrize("typed, stored", [
        ("5a", "Shelf 5A (Archive)"),
        ("shelf4b", "Shelf 4B"),
        ("shelf 4b:soldering equipment", "Shelf 4B: Soldering Equipment"),
        ("Soldering Equipment", "Shelf 4B: Soldering Equipment"),
        ("screw wall", "Wall Storage"),
    ])
    def test_known_locations_are_canonicalised(self, inventory, typed, stored):
        sku = inventory.add_item({"NAME": "A", "LOCATION": typed})

        assert inventory.get_item(sku)["LOCATION"] == stored

    def test_unknown_location_reuses_an_existing_spelling(self, inventory):
        inventory.add_item({"NAME": "A", "LOCATION": "Red Toolbox"})
        sku = inventory.add_item({"NAME": "B", "LOCATION": "red  toolbox"})

        assert inventory.get_item(sku)["LOCATION"] == "Red Toolbox"

    def test_blank_clears(self, inventory):
        sku = inventory.add_item({"NAME": "A", "LOCATION": "4A"})

        assert inventory.set_location(sku, "  ") is True
        assert inventory.get_item(sku)["LOCATION"] is None
        assert inventory.set_location("EER-999999", "4A") is False

    def test_a_shelf_includes_its_sub_groups_but_not_the_reverse(self, inventory):
        inventory.add_item({"NAME": "Shelf only", "LOCATION": "Shelf 4B"})
        inventory.add_item({"NAME": "In the bin", "LOCATION": "Shelf 4B: Soldering Equipment"})
        inventory.add_item({"NAME": "Next shelf", "LOCATION": "Shelf 4C"})

        assert {item["NAME"] for item in inventory.get_items_by_location("4b")} == {"Shelf only", "In the bin"}
        assert [item["NAME"] for item in inventory.get_items_by_location("soldering equipment")] == ["In the bin"]

    def test_free_text_wildcards_are_literal(self, inventory):
        inventory.add_item({"NAME": "Under", "LOCATION": "Bin_1: Top"})
        inventory.add_item({"NAME": "Other", "LOCATION": "BinX1: Top"})

        assert [item["NAME"] for item in inventory.get_items_by_location("Bin_1")] == ["Under"]

    def test_get_locations_lists_empty_shelves_and_groups(self, inventory):
        inventory.add_item({"NAME": "A", "LOCATION": "Shelf 4B: Soldering Equipment"})
        inventory.add_item({"NAME": "B", "LOCATION": "Red Toolbox"})

        rows = {row["LOCATION"]: row for row in inventory.get_locations()}

        assert rows["Shelf 4B"]["COUNT"] == 1
        assert "Soldering Equipment (1)" in rows["Shelf 4B"]["SUB_GROUPS"]
        assert rows["Murphy Desk"]["COUNT"] == 0
        assert rows["Red Toolbox"]["COUNT"] == 1

    def test_suggest_locations_reports_the_alias_that_matched(self, inventory):
        results = inventory.suggest_locations("mastercraft 3")

        assert results[0] == {"LOCATION": "Tool Chest 3", "ALIAS": "Mastercraft 3", "COUNT": 0}


class TestSearch:
    @pytest.fixture
    def stocked(self, inventory):
        for name, tags in [
            ("M3 x 12mm Screw", "fasteners"),
            ("M3 x 10mm Screw", "fasteners"),
            ("Capacitor 10uF", None),
            ("Black PLA Filament", "3d printing"),
            ("PLA", None),
        ]:
            inventory.add_item({"NAME": name, "TAGS": tags})

        return inventory

    def names(self, inventory, query):
        return [item["NAME"] for item in inventory.search_items(query)]

    def test_exact_name_comes_first(self, stocked):
        assert self.names(stocked, "pla")[0] == "PLA"

    def test_words_match_in_any_order(self, stocked):
        assert self.names(stocked, "filament black") == ["Black PLA Filament"]

    def test_every_word_has_to_match(self, stocked):
        assert self.names(stocked, "pla resistor") == []

    def test_typos_are_forgiven(self, stocked):
        assert self.names(stocked, "capacitr") == ["Capacitor 10uF"]

    def test_sizes_are_never_guessed(self, stocked):
        assert self.names(stocked, "m3 12mm") == ["M3 x 12mm Screw"]
        assert self.names(stocked, "11mm") == []

    def test_spaces_can_be_left_out(self, stocked):
        assert "M3 x 12mm Screw" in self.names(stocked, "m3x12")

    def test_tags_are_searched(self, stocked):
        assert set(self.names(stocked, "fasteners")) == {"M3 x 12mm Screw", "M3 x 10mm Screw"}

    def test_blank_query(self, stocked):
        assert stocked.search_items("   ") == []
        assert [row["NAME"] for row in stocked.suggest_items("", limit=2)] == ["Black PLA Filament", "Capacitor 10uF"]


class TestDigiKeyScans:
    def test_separators_match_either_way(self, inventory):
        sku = inventory.add_item({"NAME": "A"})

        assert inventory.record_digikey_scan("[)>\x1e06\x1dP123", sku, "123-ND", 10) is True

        previous = inventory.get_digikey_scan("[)>␞06␝P123")

        assert previous["SKU"] == sku
        assert previous["TIMES_SCANNED"] == 1

    def test_repeat_scans_are_counted(self, inventory):
        sku = inventory.add_item({"NAME": "A"})
        inventory.record_digikey_scan("bag", sku, None, 5)
        inventory.record_digikey_scan("bag", sku, None, 7)

        previous = inventory.get_digikey_scan("bag")

        assert previous["TIMES_SCANNED"] == 2
        assert previous["QUANTITY"] == 7

    def test_deleting_the_item_forgets_its_bags(self, inventory):
        sku = inventory.add_item({"NAME": "A"})
        inventory.record_digikey_scan("bag", sku, None, 5)
        inventory.delete_item(sku)

        assert inventory.get_digikey_scan("bag") is None
        assert inventory.record_digikey_scan("bag", sku, None, 5) is False

    def test_lookup_by_part_number(self, inventory):
        sku = inventory.add_item({"NAME": "A", "DIGIKEY_PART_NUMBER": "296-1234-ND"})

        assert inventory.get_item_by_dkpn(" 296-1234-ND ")["SKU"] == sku
        assert inventory.get_item_by_dkpn("nope") is None


def test_old_database_is_migrated(tmp_path):
    path = tmp_path / "old.db"

    with sqlite3.connect(path) as connection:
        connection.executescript("""
            CREATE TABLE items (
                sku TEXT PRIMARY KEY,
                name TEXT NOT NULL,
                order_quantity TEXT,
                low INTEGER NOT NULL DEFAULT 0,
                tracking_mode TEXT NOT NULL DEFAULT 'KANBAN',
                quantity_on_hand REAL,
                low_threshold REAL,
                decrease_amount REAL NOT NULL DEFAULT 1.0,
                location TEXT,
                tags TEXT,
                priority TEXT,
                unit TEXT,
                created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
                updated_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP
            );
            INSERT INTO items (sku, name, location, tags, priority)
                VALUES ('EER-000001', 'Old', 'screw wall', 'None, Fasteners', 'HIGH');
        """)

    inventory = SpreadsheetManager(path)

    try:
        item = inventory.get_item("EER-000001")

        assert item["LOCATION"] == "Wall Storage"
        assert item["TAGS"] == "Fasteners"
        assert item["NOTES"] is None
        assert item["DIGIKEY_PART_NUMBER"] is None

        columns = {row["name"] for row in inventory.connection.execute("PRAGMA table_info(items)")}
        assert "priority" not in columns
        assert "unit" not in columns
    finally:
        inventory.close()
