"""Fixtures for Lipgloss: a fake Niimbot, test images, and the app.

The fake replaces the three functions in lipgloss.printer that open the serial
port. Everything above them, the queue, pausing and the label checks, is real.
"""

import threading

import pytest
import yaml
from fastapi.testclient import TestClient
from PIL import Image, ImageFont

from illusion_core.events import EventBus
from lipgloss import printer
from lipgloss.label_maker import LabelMaker

TOKEN = "test-token"


class FakePrinter:
    def __init__(self):
        self.printed = []
        # Set to a reason and every print or check raises PrinterUnavailable with it
        self.fault = None
        # Pages that raise LabelError when printed, as if the head refused them
        self.bad_pages = set()
        # Cleared to hold a print in progress until the test sets it again
        self.go = threading.Event()
        self.go.set()
        self.media = {"label_capacity": 50, "used_len": 0, "total_len": 50}

    def niimbot_print(self, img, addr, model, density=3):
        self.go.wait(timeout=5)

        if self.fault:
            raise printer.PrinterUnavailable(self.fault)

        if img in self.bad_pages:
            raise printer.LabelError("refused by the head")

        self.printed.append(img)

    def check_printer(self, addr):
        if self.fault:
            raise printer.PrinterUnavailable(self.fault)

        return self.media

    def niimbot_printer_info(self, addr):
        return "Labels left: 50/50"


class RecordingBus(EventBus):
    def __init__(self):
        super().__init__()
        self.published = []

    def publish(self, event):
        self.published.append(event)
        super().publish(event)


@pytest.fixture
def anyio_backend():
    return "asyncio"


@pytest.fixture
def fake_printer(monkeypatch):
    fake = FakePrinter()

    monkeypatch.setattr(printer, "niimbot_print", fake.niimbot_print)
    monkeypatch.setattr(printer, "check_printer", fake.check_printer)
    monkeypatch.setattr(printer, "niimbot_printer_info", fake.niimbot_printer_info)

    return fake


@pytest.fixture(autouse=True)
def default_font(monkeypatch):
    # No TTF ships with the repo, and Pillow's built in font scales the same way
    monkeypatch.setattr(LabelMaker, "_load_font", lambda self, size: ImageFont.load_default(size))


@pytest.fixture
def make_image(tmp_path):
    """Write a PNG and return its path. The default fits a d110 label."""
    def make(name="label", width=96, height=320, mode="RGB", colour="white"):
        path = tmp_path / f"{name}.png"
        Image.new(mode, (width, height), colour).save(path)
        return str(path)

    return make


@pytest.fixture
def lipgloss_config(tmp_path):
    path = tmp_path / "lipgloss.yaml"
    path.write_text(yaml.safe_dump({"lipgloss": {
        "token": TOKEN,
        "printer": {"port": "/dev/null", "model": "d110", "font_path": "unused"},
        "labels": {"output_dir": str(tmp_path / "labels")},
    }}))

    return path


@pytest.fixture
def client(lipgloss_config, fake_printer):
    from lipgloss import service

    app = service.create_app(lipgloss_config)
    app.state.queue._delay = 0

    with TestClient(app, headers={"Authorization": f"Bearer {TOKEN}"}) as test_client:
        yield test_client
