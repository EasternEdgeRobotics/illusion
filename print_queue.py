import asyncio

from collections import deque

import discord

import illusion_helpers
from illusion_helpers import LabelError, PrinterUnavailable

MAX_COPIES = 100

# Discord allows 25 fields per embed, and a queue that long is unreadable anyway
MAX_EMBED_JOBS = 20

QUEUE_FIELD_NAMES = {
    "JOB_ID": "Job",
    "DESCRIPTION": "Label",
    "LABELS": "Labels",
    "STATE": "State",
    "SOURCE": "Source",
}


def notice_embed(title, description, urgent=False):
    """A plain title and description embed, for print updates that arent a list of jobs."""
    return discord.Embed(
        title=title,
        description=description,
        color=illusion_helpers.ALERT_COLOUR if urgent else illusion_helpers.EMBED_COLOUR,
    )


class PrintJob:
    def __init__(self, job_id, description, pages, notify=None, source="unknown", announce_when_done=False):
        self.job_id = job_id
        self.description = description
        self.pages = pages  # Rendered image paths, printed in order
        self.printed = 0
        self.notify = notify
        self.source = source
        self.announce_when_done = announce_when_done

    @property
    def total(self):
        return len(self.pages)

    @property
    def remaining(self):
        return self.total - self.printed

    @property
    def next_page(self):
        return self.pages[self.printed]

    def summary(self):
        if self.total > 1:
            return f"{self.description} ({self.remaining}/{self.total} labels left)"

        return self.description

    async def report(self, message, urgent=False, embed=None):
        if self.notify is None:
            return

        try:
            await self.notify(message, urgent, embed)
        except Exception as e:
            print(f"Unable to send print queue update: {e}")


class PrintQueue:
    """Prints one label at a time in the background.

    The printer is checked before every single label, and any printer problem
    (out of labels, lid open, asleep, unplugged) pauses the whole queue instead
    of burning through the rest of the jobs failing. Nothing is lost: the job
    that failed stays at the front of the queue and picks up where it left off
    once someone resumes it.
    """

    def __init__(self, port, model, delay=1, log=None):
        self._port = port
        self._model = model
        self._delay = delay  # niimbot cant instantly take a new job, so we give it extra time between each one
        self._log = log or print

        self._jobs = deque()
        self._wake = asyncio.Event()
        # Only one thing can hold the serial port at a time, so every trip to the
        # printer, queued or not, goes through here
        self._printer_lock = asyncio.Lock()
        self._paused = False
        self._pause_reason = None
        self._next_job_id = 1
        self._task = None

    # Queue state

    @property
    def paused(self):
        return self._paused

    @property
    def pause_reason(self):
        return self._pause_reason

    @property
    def pending_jobs(self):
        return len(self._jobs)

    @property
    def pending_labels(self):
        return sum(job.remaining for job in self._jobs)

    def start(self):
        if self._task is None or self._task.done():
            self._task = asyncio.create_task(self._worker())

        return self._task

    async def stop(self):
        if self._task is None:
            return

        self._task.cancel()

        try:
            await self._task
        except asyncio.CancelledError:
            pass
        except Exception:
            pass

        self._task = None

    # Adding work

    def add(self, pages, description, copies=1, notify=None, source="unknown"):
        """Queue rendered label images. Returns (job, message)."""
        if isinstance(pages, str):
            pages = [pages]

        if copies < 1:
            return None, "Quantity must be at least 1"

        if copies > MAX_COPIES:
            return None, f"Quantity too high, {MAX_COPIES} is the limit per job"

        pages = list(pages) * copies

        if not pages:
            return None, "Nothing to print"

        try:
            for page in dict.fromkeys(pages):
                illusion_helpers.check_label(page, self._model)
        except LabelError as e:
            return None, f"Unable to print, {e}"

        job = PrintJob(
            job_id=self._next_job_id,
            description=description,
            pages=pages,
            notify=notify,
            source=source,
            announce_when_done=len(pages) > 1 or len(self._jobs) > 0,
        )

        self._next_job_id += 1
        self._jobs.append(job)

        if not self._paused:
            self._wake.set()

        return job, self._queued_message(job)

    def _queued_message(self, job):
        if job.total > 1:
            queued = f"Queued job {job.job_id}: {job.total} labels, {job.description}"
        else:
            queued = f"Queued job {job.job_id}: {job.description}"

        if self._paused:
            return (
                f"{queued}\n"
                f"The print queue is paused: {self._pause_reason}\n"
                f"Nothing will print until the printer is fixed and the queue is resumed."
            )

        ahead = self.pending_labels - job.remaining

        if ahead > 0:
            return f"{queued}\n{ahead} label(s) ahead of it in the queue."

        return f"{queued}\nPrinting...\nif this is the first print after returning from sleep it may be blank."

    # Talking to the printer

    async def printer_media(self):
        """Media info for the loaded roll, raises PrinterUnavailable if it cant print."""
        async with self._printer_lock:
            return await asyncio.to_thread(illusion_helpers.check_printer, self._port)

    async def printer_info(self):
        async with self._printer_lock:
            return await asyncio.to_thread(illusion_helpers.niimbot_printer_info, self._port)

    # Managing the queue

    def cancel(self, job_id):
        for job in self._jobs:
            if job.job_id == job_id:
                self._jobs.remove(job)
                return f"Cancelled job {job_id}: {job.description}"

        return f"No queued job with id {job_id}"

    def clear(self):
        cleared_jobs = len(self._jobs)
        cleared_labels = self.pending_labels

        self._jobs.clear()

        if cleared_jobs == 0:
            return "The print queue is already empty"

        return f"Cleared {cleared_jobs} job(s), {cleared_labels} label(s) never printed"

    async def resume(self):
        if not self._paused:
            if not self._jobs:
                return "The print queue is not paused, and there is nothing waiting to print"

            return "The print queue is not paused"

        try:
            media_info = await self.printer_media()
        except PrinterUnavailable as e:
            self._pause_reason = str(e)
            return f"Still unable to print, {e}\nThe print queue is staying paused."

        remaining_media = illusion_helpers.media_remaining(media_info)

        self._paused = False
        self._pause_reason = None

        if self._jobs:
            self._wake.set()
            return f"Print queue resumed, {self.pending_labels} label(s) to print, {remaining_media} left on the roll"

        return f"Print queue resumed, nothing waiting to print, {remaining_media} labels left on the roll"

    def _status_parts(self):
        """Title, description and one row per job, shared by the text and embed versions."""
        if self._paused:
            title = "Print Queue: Paused"
        elif self._jobs:
            title = "Print Queue: Printing"
        else:
            title = "Print Queue: Idle"

        description = []

        if self._paused:
            description.append(f"{self._pause_reason}\nResume the queue once the printer is sorted out.")

        if self._jobs:
            description.append(f"{len(self._jobs)} job(s), {self.pending_labels} label(s) left")
        else:
            description.append("Nothing waiting to print.")

        rows = []

        for index, job in enumerate(self._jobs):
            if index == 0:
                state = "Paused" if self._paused else "Printing"
            else:
                state = "Waiting"

            rows.append(
                {
                    "HEADER": f"Job {job.job_id} ({state})",
                    "JOB_ID": str(job.job_id),
                    "DESCRIPTION": job.description,
                    "LABELS": f"{job.remaining}/{job.total}",
                    "SOURCE": job.source,
                    "STATE": state,
                }
            )

        return title, "\n".join(description), rows

    def status(self):
        title, description, rows = self._status_parts()

        if not rows:
            return f"{title}\n{description}"

        table = illusion_helpers.make_table(rows, exclude=["HEADER"], field_names=QUEUE_FIELD_NAMES, vertical=False)

        return f"{title}\n{description}\n{table}"

    def status_embed(self):
        title, description, rows = self._status_parts()

        hidden = len(rows) - MAX_EMBED_JOBS

        if hidden > 0:
            rows = rows[:MAX_EMBED_JOBS]
            description = f"{description}\nOnly the first {MAX_EMBED_JOBS} are listed, {hidden} more behind them."

        colour = illusion_helpers.ALERT_COLOUR if self._paused else illusion_helpers.EMBED_COLOUR

        if not rows:
            return notice_embed(title, description, urgent=self._paused)

        # Job id and state are already in each field name
        return illusion_helpers.make_embed(
            rows,
            exclude=["JOB_ID", "STATE"],
            field_names=QUEUE_FIELD_NAMES,
            title=title,
            description=description,
            colour=colour,
            row_name="HEADER",
            vertical=False,
        )

    # Printing

    def _pause(self, reason):
        self._paused = True
        self._pause_reason = reason
        self._wake.clear()

    async def _worker(self):
        while True:
            await self._wake.wait()

            if self._paused or not self._jobs:
                self._wake.clear()
                continue

            job = self._jobs[0]
            page = job.next_page

            try:
                async with self._printer_lock:
                    await asyncio.to_thread(illusion_helpers.niimbot_print, page, self._port, self._model)
            except PrinterUnavailable as e:
                self._pause(str(e))

                self._log(f"Print queue paused, {e}")

                paused_message = (
                    f"Unable to print, {e}\n"
                    f"The print queue is paused with {self.pending_labels} label(s) left "
                    f"(job {job.job_id}: {job.summary()}).\n"
                    f"Fix the printer, then resume the queue."
                )

                await job.report(
                    paused_message,
                    urgent=True,
                    embed=notice_embed("Print Queue Paused", paused_message, urgent=True),
                )

                continue
            except LabelError as e:
                # The printer is fine, this label just cant be printed, so skip it and carry on
                job.pages.pop(job.printed)

                if job.remaining <= 0:
                    self._jobs.popleft()

                self._log(f"Job {job.job_id} skipped a label, {e}")

                skipped_message = f"Skipped a label in job {job.job_id} ({job.description}), {e}"

                await job.report(
                    skipped_message,
                    urgent=True,
                    embed=notice_embed("Label Skipped", skipped_message, urgent=True),
                )

                continue

            job.printed += 1

            if job.remaining <= 0:
                self._jobs.popleft()

                self._log(f"Job {job.job_id} finished, {job.description}")

                if job.announce_when_done:
                    finished_message = f"Finished printing job {job.job_id}: {job.total} label(s), {job.description}"

                    await job.report(
                        finished_message,
                        embed=notice_embed("Print Finished", finished_message),
                    )

            await asyncio.sleep(self._delay)
