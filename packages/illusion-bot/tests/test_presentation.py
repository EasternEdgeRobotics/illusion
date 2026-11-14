"""The bot's Discord rendering, mostly the limits Discord rejects a message over."""

import pytest

from illusion_bot import presentation


def queue_status(jobs, paused=False):
    return {
        "title": "Print Queue: Paused" if paused else "Print Queue: Printing",
        "description": "",
        "paused": paused,
        "jobs": [
            {"HEADER": f"Job {n}", "JOB_ID": str(n), "DESCRIPTION": "x", "LABELS": "1/1", "SOURCE": "bot", "STATE": "Waiting"}
            for n in range(jobs)
        ],
    }


def test_single_item_stays_under_the_field_limit():
    embed = presentation.make_embed({f"FIELD_{n}": n for n in range(40)})

    assert len(embed.fields) == presentation.MAX_EMBED_FIELDS


def test_blank_values_show_as_na():
    embed = presentation.make_embed({"NAME": "Tape", "NOTES": None})

    assert [(field.name, field.value) for field in embed.fields] == [("Name", "Tape"), ("NOTES", "N/A")]


def test_lists_are_paged():
    rows = [{"SKU": f"EER-{n:06d}"} for n in range(25)]

    last = presentation.make_embed(rows, page=2)

    assert presentation.total_pages(rows) == 3
    assert [field.name for field in last.fields] == [f"Result {n}" for n in range(21, 26)]
    assert last.footer.text == "Page 3 of 3"


@pytest.mark.xfail(strict=True, reason="make_embed pages at 10 and queue_embed never turns the page")
def test_long_queue_is_cut_and_says_so():
    embed = presentation.queue_embed(queue_status(30))

    assert len(embed.fields) == presentation.MAX_EMBED_JOBS
    assert "10 more behind them" in embed.description


def test_paused_queue_is_red():
    assert presentation.queue_embed(queue_status(0, paused=True)).colour == presentation.ALERT_COLOUR


def test_vendor_links_get_a_scheme():
    view = presentation.make_vendor_buttons({"VENDOR_1": "Pololu", "LINK_1": "pololu.com", "LINK_2": "https://x.com"})

    assert [button.url for button in view.children] == ["http://pololu.com", "https://x.com"]
    assert presentation.make_vendor_buttons({}) is None


def test_low_thread_content():
    item = {
        "NAME": "Tape", "SKU": "EER-000001", "LOCATION": None, "TRACKING_MODE": "QUANTITY",
        "ORDER_QUANTITY": "2", "QUANTITY_ON_HAND": 1.0, "LOW_THRESHOLD": 2.0,
    }

    content = presentation.make_low_thread_content(item)

    assert "Location" not in content
    assert "Current Stock: 1" in content
    assert "Low Threshold: 2" in content
