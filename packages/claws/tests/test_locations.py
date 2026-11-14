"""The location catalogue and the lookups built from it."""

import pytest

from claws import locations


@pytest.mark.parametrize("location", locations.CATALOGUE, ids=lambda location: location.name)
def test_every_name_in_the_catalogue_resolves_to_its_own_shelf(location):
    # Fails when an edit to the catalogue gives two shelves the same alias, or an
    # alias that compacts to another shelf's name. The first one listed would
    # otherwise win without anyone noticing.
    for name in (location.name, *location.aliases):
        assert locations.resolve(name) == location.name, name

        for group in location.groups:
            assert locations.resolve(f"{name}: {group}") == locations.full_name(location.name, group)


@pytest.mark.parametrize("value", [None, "", "   ", "Nowhere in particular"])
def test_unknown_resolves_to_none(value):
    assert locations.resolve(value) is None
    assert not locations.is_known(value)


def test_clean_tidies_whitespace_and_separators():
    assert locations.clean("  Shelf 4B :Soldering   Equipment ") == "Shelf 4B: Soldering Equipment"


def test_search_ranks_exact_then_prefix_then_substring():
    results = [name for name, _ in locations.search("shelf 4b")]

    assert results[0] == "Shelf 4B"
    assert all(name.startswith("Shelf 4B") for name in results)


def test_search_names_the_alias_only_when_it_is_why_something_matched():
    assert ("Wall Storage", "Pink Floyd") in locations.search("floyd")
    assert ("Shelf 4A", None) in locations.search("4")


def test_empty_search_lists_everything_in_order():
    assert [name for name, _ in locations.search("")] == list(locations.KNOWN_LOCATIONS)
