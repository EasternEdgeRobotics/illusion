"""Label rendering: every style fits the head, and the image helpers keep their promises."""

from io import BytesIO

import pytest
from PIL import Image

from lipgloss import printer
from lipgloss.label_maker import LABEL_STYLES, LabelMaker, dithered_png, flattened_for_print, missing_values
from lipgloss.service import LABEL_HEIGHT, LABEL_WIDTH


@pytest.mark.parametrize("style", LABEL_STYLES)
def test_every_style_renders_at_print_size_and_fits_a_d110(style, tmp_path):
    path = LabelMaker(None).render_label(
        style_name=style,
        width=LABEL_WIDTH,
        height=LABEL_HEIGHT,
        output=str(tmp_path / style),
        sku="EER-000123",
        input_text_1="A fairly long first line of text to wrap",
        input_text_2="Second",
    )

    with Image.open(path) as label:
        assert label.size == (LABEL_HEIGHT, LABEL_WIDTH)

    printer.check_label(path, "d110")


def test_render_needs_every_value_the_style_uses(tmp_path):
    with pytest.raises(ValueError, match="input_text_2"):
        LabelMaker(None).render_label(
            "label_2_line", LABEL_WIDTH, LABEL_HEIGHT, output=str(tmp_path / "x"), input_text_1="a",
        )


def test_missing_values_uses_wire_names():
    assert missing_values("label_2_line", {"input_text_1": "a", "input_text_2": ""}) == ["line_2"]
    assert missing_values("cable_label_sku", {}) == ["sku", "line_1", "line_2"]
    assert missing_values("slim_barcode", {"sku": "EER-000001"}) == []


def test_transparent_images_print_on_white():
    buffer = BytesIO()
    Image.new("RGBA", (4, 4), (0, 0, 0, 0)).save(buffer, format="PNG")

    flattened = flattened_for_print(buffer.getvalue())

    assert flattened.mode == "RGB"
    assert flattened.getpixel((0, 0)) == (255, 255, 255)


def test_dither_preview_turns_and_scales():
    buffer = BytesIO()
    Image.new("RGB", (96, 320), "grey").save(buffer, format="PNG")

    with Image.open(BytesIO(dithered_png(buffer.getvalue(), scale=2, rotate=270))) as preview:
        # Scaled, then a one pixel frame on each side
        assert preview.size == (320 * 2 + 2, 96 * 2 + 2)


class TestPrinterChecks:
    def test_open_lid(self):
        with pytest.raises(printer.PrinterUnavailable, match="open"):
            printer.check_printer_ready({"closingstate": 0}, {"label_capacity": 50, "used_len": 0})

    def test_no_roll(self):
        with pytest.raises(printer.PrinterUnavailable, match="load a roll"):
            printer.check_printer_ready({"closingstate": 1}, None)

    def test_empty_roll(self):
        with pytest.raises(printer.PrinterUnavailable, match="no labels left"):
            printer.check_printer_ready({"closingstate": 1}, {"label_capacity": 50, "used_len": 50})

    def test_ready(self):
        printer.check_printer_ready({"closingstate": 1}, {"label_capacity": 50, "used_len": 49})

    def test_unknown_model_is_not_width_checked(self, make_image):
        printer.check_label(make_image(width=1000, height=10), "some_future_model")
