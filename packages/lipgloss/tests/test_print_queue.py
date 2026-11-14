"""PrintQueue against the fake printer: ordering, pausing on a fault, and resuming."""

import asyncio

import pytest

from lipgloss.print_queue import MAX_COPIES, PrintQueue

from .conftest import RecordingBus

pytestmark = pytest.mark.anyio


async def until(condition, timeout=2):
    async with asyncio.timeout(timeout):
        while not condition():
            await asyncio.sleep(0.01)


@pytest.fixture
async def queue(fake_printer):
    events = RecordingBus()
    print_queue = PrintQueue("/dev/null", "d110", delay=0, log=lambda message: None, events=events)
    print_queue.events = events
    print_queue.start()
    yield print_queue
    await print_queue.stop()


def event_names(queue):
    return [event["event"] for event in queue.events.published]


async def test_prints_pages_in_order_with_copies(queue, fake_printer, make_image):
    first, second = make_image("first"), make_image("second")

    job, _ = queue.add([first, second], "Two labels", copies=2, reply_to="abc")
    await until(lambda: queue.pending_jobs == 0)

    assert fake_printer.printed == [first, second, first, second]
    assert event_names(queue) == ["job.done"]
    assert queue.events.published[0]["reply_to"] == "abc"


async def test_a_lone_single_label_is_not_announced(queue, fake_printer, make_image):
    queue.add(make_image(), "One label")
    await until(lambda: fake_printer.printed)
    await until(lambda: queue.pending_jobs == 0)

    assert event_names(queue) == []


@pytest.mark.parametrize("copies, message", [
    (0, "at least 1"), (MAX_COPIES + 1, "too high"),
])
async def test_copies_are_bounded(queue, make_image, copies, message):
    job, reply = queue.add(make_image(), "x", copies=copies)

    assert job is None
    assert message in reply


async def test_too_wide_is_refused_up_front(queue, make_image):
    job, reply = queue.add(make_image(width=400, height=96), "Wide")

    assert job is None
    assert "too wide" in reply


async def test_fault_pauses_and_resume_carries_on(queue, fake_printer, make_image):
    pages = [make_image(f"page{n}") for n in range(3)]
    fake_printer.fault = "no labels left, please replace roll"

    queue.add(pages, "Three labels")
    await until(lambda: queue.paused)

    assert queue.pending_labels == 3
    assert event_names(queue) == ["printer.fault"]
    assert queue.events.published[0]["urgent"] is True

    _, reply = queue.add(make_image("later"), "Later")
    assert "paused" in reply

    assert "staying paused" in await queue.resume()

    fake_printer.fault = None
    assert (await queue.resume()).startswith("Print queue resumed, 4 label(s) to print")

    await until(lambda: queue.pending_jobs == 0)

    assert fake_printer.printed == [*pages, make_image("later")]


async def test_a_bad_label_is_skipped_not_paused_on(queue, fake_printer, make_image):
    good, bad = make_image("good"), make_image("bad")
    fake_printer.bad_pages.add(bad)

    queue.add([bad, good], "Mixed")
    await until(lambda: queue.pending_jobs == 0)

    assert fake_printer.printed == [good]
    assert not queue.paused
    assert "job.skipped" in event_names(queue)


async def test_cancelling_the_printing_job_does_not_drop_the_next(queue, fake_printer, make_image):
    fake_printer.go.clear()
    first, _ = queue.add(make_image("first"), "First")
    queue.add(make_image("second"), "Second")
    await asyncio.sleep(0.05)

    assert queue.cancel(first.job_id) == (True, f"Cancelled job {first.job_id}: First")

    fake_printer.go.set()
    await until(lambda: len(fake_printer.printed) == 2)

    assert fake_printer.printed == [make_image("first"), make_image("second")]


async def test_cancel_clear_and_status(fake_printer, make_image):
    # Not started, so nothing leaves the queue while it is inspected
    queue = PrintQueue("/dev/null", "d110", delay=0)
    queue.add([make_image("a"), make_image("b")], "Pair", source="kiosk")
    second, _ = queue.add(make_image("c"), "Single")

    status = queue.status()
    assert status["title"] == "Print Queue: Printing"
    assert [row["STATE"] for row in status["jobs"]] == ["Printing", "Waiting"]
    assert status["jobs"][0]["LABELS"] == "2/2"
    assert status["jobs"][0]["SOURCE"] == "kiosk"

    assert queue.cancel(99) == (False, "No queued job with id 99")
    assert queue.cancel(second.job_id)[0] is True
    assert queue.clear() == "Cleared 1 job(s), 2 label(s) never printed"
    assert queue.clear() == "The print queue is already empty"
    assert queue.status()["title"] == "Print Queue: Idle"
