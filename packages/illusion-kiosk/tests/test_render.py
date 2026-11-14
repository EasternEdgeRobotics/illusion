"""The kiosk's terminal rendering of command results."""

from illusion_core.commands import DuplicateScan, Rows
from illusion_kiosk.__main__ import queue_text, render


def test_strings_pass_through():
    assert render("Added Tape") == "Added Tape"


def test_rows_become_a_table_without_excluded_fields():
    table = render(Rows({"SKU": "EER-000001", "NAME": "Tape"}, exclude=["NAME"]))

    assert "EER-000001" in table
    assert "Tape" not in table


def test_duplicate_scan_shows_the_item_then_the_warning():
    text = render(DuplicateScan("bag", "Already counted", Rows({"SKU": "EER-000001"})))

    assert text.index("EER-000001") < text.index("Already counted")


def test_queue_text():
    idle = {"title": "Print Queue: Idle", "description": "Nothing waiting to print.", "jobs": []}
    busy = {"title": "Print Queue: Printing", "description": "1 job(s)", "jobs": [
        {"HEADER": "Job 1", "JOB_ID": "1", "DESCRIPTION": "Tape", "LABELS": "1/1", "SOURCE": "kiosk", "STATE": "Printing"},
    ]}

    assert queue_text(idle) == "Print Queue: Idle\nNothing waiting to print."
    assert "| Job | Label |" in queue_text(busy)
    # An outage message from the command layer comes through untouched
    assert queue_text("Unable to reach lipgloss.") == "Unable to reach lipgloss."
