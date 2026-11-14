"""Lipgloss's HTTP API, with the fake printer behind the queue."""

import time
from io import BytesIO

import pytest
from PIL import Image


def wait_for_idle(client, timeout=2):
    deadline = time.monotonic() + timeout

    while client.get("/queue").json()["pending_jobs"]:
        assert time.monotonic() < deadline, "queue never drained"
        time.sleep(0.01)


def png(width=96, height=320, mode="RGB", colour="white"):
    buffer = BytesIO()
    Image.new(mode, (width, height), colour).save(buffer, format="PNG")
    return buffer.getvalue()


def test_auth(client):
    assert client.get("/queue", headers={"Authorization": "Bearer wrong"}).status_code == 401
    assert client.get("/health", headers={"Authorization": ""}).json()["service"] == "lipgloss"


class TestPrint:
    def test_print(self, client, fake_printer):
        body = client.post("/print", json={"style": "label_1_line", "line_1": "Tape", "sku": "EER-000001"}).json()

        assert body["message"].startswith(f"Queued job {body['job_id']}: Tape (EER-000001)")
        assert body["paused"] is False

        wait_for_idle(client)
        assert len(fake_printer.printed) == 1

    @pytest.mark.parametrize("request_body, detail", [
        ({"style": "nope"}, "unknown style: nope"),
        ({"style": "label_2_line", "line_1": "a"}, "label_2_line needs line_2"),
        ({"style": "slim_barcode"}, "slim_barcode needs sku"),
    ])
    def test_bad_requests_name_the_problem(self, client, request_body, detail):
        response = client.post("/print", json=request_body)

        assert response.status_code == 400
        assert response.json()["detail"] == detail

    def test_copies_are_bounded(self, client):
        assert client.post("/print", json={"style": "label_1_line", "line_1": "a", "copies": 101}).status_code == 422

    def test_printing_onto_a_fault_pauses_and_says_so(self, client, fake_printer):
        fake_printer.fault = "the printer seems to be open"
        client.post("/print", json={"style": "label_1_line", "line_1": "a"})

        deadline = time.monotonic() + 2
        while not client.get("/queue").json()["paused"]:
            assert time.monotonic() < deadline
            time.sleep(0.01)

        assert client.post("/print", json={"style": "label_1_line", "line_1": "b"}).json()["paused"] is True
        assert "staying paused" in client.post("/queue/resume").json()["message"]

        fake_printer.fault = None
        assert client.post("/queue/resume").json()["message"].startswith("Print queue resumed")

        wait_for_idle(client)
        assert len(fake_printer.printed) == 2


class TestBarcodeRange:
    def test_range_with_per_sku_text(self, client, fake_printer):
        body = client.post("/print/barcodes", json={
            "lower": 1, "upper": 3, "style": "label_barcode",
            "line_1": "Spare", "line_1_by_sku": {"EER-000002": "Servo"},
        }).json()

        assert body["job_id"] is not None
        wait_for_idle(client)
        assert len(fake_printer.printed) == 3

    @pytest.mark.parametrize("request_body, message", [
        ({"lower": 5, "upper": 1}, "5 is higher than 1"),
        ({"lower": 1, "upper": 2, "style": "label_1_line"}, "does not put the SKU on the label"),
        ({"lower": 1, "upper": 2, "style": "label_barcode", "line_1_by_sku": {"EER-000001": "a"}}, "needs line_1 for EER-000002"),
        ({"lower": 1, "upper": 60}, "exceeds the max amount"),
    ])
    def test_refusals(self, client, fake_printer, request_body, message):
        body = client.post("/print/barcodes", json=request_body).json()

        assert body["job_id"] is None
        assert message in body["message"]
        assert fake_printer.printed == []

    def test_refuses_more_than_is_left_on_the_roll(self, client, fake_printer):
        fake_printer.media["used_len"] = 48

        body = client.post("/print/barcodes", json={"lower": 1, "upper": 3}).json()

        assert "3 requested, 2 available" in body["message"]

    def test_printer_down(self, client, fake_printer):
        fake_printer.fault = "printer is likely disconnected"

        assert client.post("/print/barcodes", json={"lower": 1, "upper": 2}).json()["message"] == (
            "Unable to print, printer is likely disconnected"
        )


class TestImages:
    def test_print_image_is_flattened(self, client, fake_printer):
        response = client.post(
            "/print/image",
            files={"file": ("logo.png", png(mode="RGBA", colour=(0, 0, 0, 0)), "image/png")},
            data={"description": "Logo"},
        )

        assert response.json()["job_id"] is not None
        wait_for_idle(client)

        with Image.open(fake_printer.printed[0]) as printed:
            assert printed.mode == "RGB"
            assert printed.getpixel((0, 0)) == (255, 255, 255)

    def test_unreadable_image(self, client):
        response = client.post(
            "/print/image", files={"file": ("x.png", b"not an image", "image/png")}, data={"description": "x"},
        )

        assert response.status_code == 422

    def test_too_wide_image_is_refused(self, client):
        response = client.post(
            "/print/image", files={"file": ("x.png", png(width=400, height=96), "image/png")}, data={"description": "x"},
        )

        assert "too wide" in response.json()["message"]


class TestPreview:
    def test_preview_is_unrotated_and_leaves_nothing_behind(self, client, tmp_path):
        response = client.post("/preview", json={"style": "label_1_line", "line_1": "Tape", "scale": 2})

        assert response.headers["content-type"] == "image/png"

        with Image.open(BytesIO(response.content)) as preview:
            assert preview.size == (320 * 2 + 2, 96 * 2 + 2)

        assert list((tmp_path / "labels").iterdir()) == []

    def test_render(self, client):
        response = client.post("/render", json={"sku": "EER-000001"})

        with Image.open(BytesIO(response.content)) as rendered:
            assert rendered.size == (350, 280)


def test_cancel_a_waiting_job(client, fake_printer):
    fake_printer.go.clear()

    try:
        client.post("/print", json={"style": "label_1_line", "line_1": "a"})
        waiting = client.post("/print", json={"style": "label_1_line", "line_1": "b"}).json()["job_id"]

        assert client.delete(f"/queue/{waiting}").json()["cancelled"] is True
        assert client.delete(f"/queue/{waiting}").json()["cancelled"] is False
    finally:
        fake_printer.go.set()

    wait_for_idle(client)
    assert len(fake_printer.printed) == 1
