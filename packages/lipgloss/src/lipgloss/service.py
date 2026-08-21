"""lipgloss: label rendering and printing, over HTTP.

Runs on whichever machine the Niimbot is plugged into. It holds no database and
never talks to claws about inventory: callers that want an item name on a label
resolve it themselves and pass literal text, which keeps this service's only
dependency the printer in front of it.
"""

import asyncio
import json
import time
from importlib.metadata import version
from pathlib import Path

from fastapi import Depends, FastAPI, Header, HTTPException, UploadFile, Form
from fastapi.responses import StreamingResponse
from pydantic import BaseModel, Field

from illusion_core import config as illusion_config
from illusion_core.uptime import service_uptime_ms, system_uptime_ms
from lipgloss import printer
from illusion_core.events import EventBus
from lipgloss.label_maker import LabelMaker, LABEL_STYLES
from lipgloss.print_queue import MAX_COPIES, PrintQueue
from lipgloss.printer import PrinterUnavailable

# From package metadata, never hardcoded: the fleet status flags a version
# mismatch across services, and a constant left behind by a release bump would
# report a mismatch that does not exist.
VERSION = version("lipgloss")

LABEL_WIDTH = 320
LABEL_HEIGHT = 96
BARCODE_WIDTH = 350
BARCODE_HEIGHT = 280


class PrintRequest(BaseModel):
    style: str
    sku: str | None = None
    line_1: str | None = None
    line_2: str | None = None
    copies: int = Field(default=1, ge=1, le=MAX_COPIES)
    source: str = "unknown"
    reply_to: str | None = None


class BarcodeRangeRequest(BaseModel):
    lower: int
    upper: int
    source: str = "unknown"
    reply_to: str | None = None


class RenderRequest(BaseModel):
    style: str = "classic_barcode"
    sku: str | None = None
    line_1: str | None = None
    line_2: str | None = None
    width: int = BARCODE_WIDTH
    height: int = BARCODE_HEIGHT
    rotate: int = 0


def create_app(config_path="./lipgloss.yaml"):
    config = illusion_config.load(
        config_path,
        required=[
            "lipgloss.token",
            "lipgloss.printer.port",
            "lipgloss.printer.font_path",
        ],
    )

    token = illusion_config.get(config, "lipgloss.token")
    model = illusion_config.get(config, "lipgloss.printer.model", "d110")
    output_dir = Path(
        illusion_config.get(config, "lipgloss.labels.output_dir", "/tmp/illusion-labels")
    )
    output_dir.mkdir(parents=True, exist_ok=True)

    events = EventBus()
    labelmaker = LabelMaker(illusion_config.get(config, "lipgloss.printer.font_path"))
    printqueue = PrintQueue(
        illusion_config.get(config, "lipgloss.printer.port"), model, events=events
    )

    started_at = time.time()

    async def require_token(authorization: str = Header(default="")):
        if authorization != f"Bearer {token}":
            raise HTTPException(status_code=401, detail="bad or missing bearer token")

    app = FastAPI(title="lipgloss", version=VERSION)
    app.state.queue = printqueue
    app.state.events = events

    @app.on_event("startup")
    async def _start():
        printqueue.start()

    @app.on_event("shutdown")
    async def _stop():
        await printqueue.stop()

    def _label_path(prefix):
        # Every job needs its own file, otherwise a label queued later would
        # overwrite one still waiting to be printed
        return str(output_dir / f"{prefix}_{time.time_ns()}")

    # Health is deliberately unauthenticated so claws can report liveness even
    # if the shared token is rotated on one side only
    @app.get("/health")
    async def health():
        return {
            "service": "lipgloss",
            "version": VERSION,
            "uptime_ms": service_uptime_ms(started_at),
            "system_uptime_ms": system_uptime_ms(),
            "printer_port": illusion_config.get(config, "lipgloss.printer.port"),
            "model": model,
        }

    @app.get("/printer", dependencies=[Depends(require_token)])
    async def printer_info():
        return {"message": await printqueue.printer_info()}

    @app.get("/queue", dependencies=[Depends(require_token)])
    async def queue():
        return printqueue.status()

    @app.post("/print", dependencies=[Depends(require_token)])
    async def print_label(request: PrintRequest):
        if request.style not in LABEL_STYLES:
            raise HTTPException(status_code=400, detail=f"unknown style: {request.style}")

        output = labelmaker.render_label(
            style_name=request.style,
            input_text_1=request.line_1,
            input_text_2=request.line_2,
            sku=request.sku,
            width=LABEL_WIDTH,
            height=LABEL_HEIGHT,
            output=_label_path("label"),
        )

        if request.sku and request.line_1:
            description = f"{request.line_1} ({request.sku})"
        elif request.sku:
            description = request.sku
        elif request.line_1:
            description = request.line_1
        else:
            description = request.style

        job, message = printqueue.add(
            output,
            description[:60],
            copies=request.copies,
            reply_to=request.reply_to,
            source=request.source,
        )

        return {"job_id": job.job_id if job else None, "message": message}

    @app.post("/print/barcodes", dependencies=[Depends(require_token)])
    async def print_barcodes(request: BarcodeRangeRequest):
        if request.upper < request.lower:
            return {"job_id": None, "message": f"{request.lower} is higher than {request.upper}"}

        total_prints = request.upper - request.lower + 1

        # Warn about a roll that cant fit the job before printing any of it,
        # the queue itself will stop if we run out part way through anyway
        try:
            media_info = await printqueue.printer_media()
        except PrinterUnavailable as e:
            return {"job_id": None, "message": f"Unable to print, {e}"}

        remaining_media = printer.media_remaining(media_info)

        if total_prints > int(media_info["total_len"]):
            return {
                "job_id": None,
                "message": (
                    "This exceeds the max amount of prints possible on a single roll.\n"
                    "Please split this into smaller jobs. \n"
                    f"{total_prints} requested, {media_info['total_len']} possible"
                ),
            }

        if total_prints > remaining_media:
            return {
                "job_id": None,
                "message": (
                    "This exceeds the amounts of prints left on the current roll.\n"
                    "Please split this into smaller jobs. \n"
                    f"{total_prints} requested, {remaining_media} available"
                ),
            }

        pages = [
            labelmaker.render_label(
                style_name="slim_barcode",
                width=LABEL_WIDTH,
                height=LABEL_HEIGHT,
                output=str(output_dir / f"barcode_EER-{number:06d}"),
                sku=f"EER-{number:06d}",
            )
            for number in range(request.lower, request.upper + 1)
        ]

        description = f"barcodes EER-{request.lower:06d} to EER-{request.upper:06d}"

        job, message = printqueue.add(
            pages, description, reply_to=request.reply_to, source=request.source
        )

        return {"job_id": job.job_id if job else None, "message": message}

    @app.post("/print/image", dependencies=[Depends(require_token)])
    async def print_image(
        file: UploadFile,
        description: str = Form(...),
        copies: int = Form(default=1),
        source: str = Form(default="unknown"),
        reply_to: str | None = Form(default=None),
    ):
        path = Path(_label_path("image") + ".png")
        path.write_bytes(await file.read())

        job, message = printqueue.add(
            str(path), description[:60], copies=copies, reply_to=reply_to, source=source
        )

        return {"job_id": job.job_id if job else None, "message": message}

    @app.post("/render", dependencies=[Depends(require_token)])
    async def render(request: RenderRequest):
        """Render a label and hand back the PNG, without printing it."""
        if request.style not in LABEL_STYLES:
            raise HTTPException(status_code=400, detail=f"unknown style: {request.style}")

        output = labelmaker.render_label(
            style_name=request.style,
            input_text_1=request.line_1,
            input_text_2=request.line_2,
            sku=request.sku,
            width=request.width,
            height=request.height,
            rotate=request.rotate,
            output=_label_path("render"),
        )

        return StreamingResponse(open(output, "rb"), media_type="image/png")

    @app.post("/queue/resume", dependencies=[Depends(require_token)])
    async def resume():
        return {"message": await printqueue.resume()}

    @app.post("/queue/clear", dependencies=[Depends(require_token)])
    async def clear():
        return {"message": printqueue.clear()}

    @app.delete("/queue/{job_id}", dependencies=[Depends(require_token)])
    async def cancel(job_id: int):
        return {"message": printqueue.cancel(job_id)}

    @app.get("/events", dependencies=[Depends(require_token)])
    async def event_stream():
        subscription = events.subscribe()

        async def stream():
            try:
                while not subscription.dropped.is_set():
                    try:
                        event = await asyncio.wait_for(subscription.queue.get(), timeout=20)
                    except TimeoutError:
                        # Keeps the connection from being reaped by anything in
                        # between while the printer is idle
                        yield ": keepalive\n\n"
                        continue

                    yield f"data: {json.dumps(event)}\n\n"
            finally:
                events.unsubscribe(subscription)

        return StreamingResponse(stream(), media_type="text/event-stream")

    return app


def main():
    import os

    import uvicorn

    config_path = os.environ.get("LIPGLOSS_CONFIG", "./lipgloss.yaml")

    try:
        config = illusion_config.load(config_path)
    except illusion_config.ConfigError as e:
        print(e)
        raise SystemExit(1)

    uvicorn.run(
        create_app(config_path),
        host=illusion_config.get(config, "lipgloss.bind.host", "127.0.0.1"),
        port=illusion_config.get(config, "lipgloss.bind.port", 8081),
        log_level="info",
    )


if __name__ == "__main__":
    main()
