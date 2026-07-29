from io import BytesIO
from pathlib import Path
from typing import Optional
import os

from barcode import Code128
from barcode.writer import ImageWriter
from PIL import Image, ImageDraw, ImageFont

LABEL_STYLES = {
    "slim_barcode": {
        "columns": [1, 30], # Values > 1 directly map to pixels
        "rows": [1],
        "cells": [
            {"type": "barcode", "value": "sku", "col": 0,"row": 0,},
            {"type": "text", "value": "sku", "col": 1, "row": 0, "rotate": 90,},
        ],
    },

    "label_barcode": {
        "columns": [1, 30],
        "rows": [0.5, 0.5],
        "cells": [
            {"type": "text", "value": "input_text_1", "col": 0, "row": 0,},
            {"type": "barcode", "value": "sku", "col": 0, "row": 1,},
            {"type": "text", "value": "sku", "col": 1, "row": 0, "rowspan": 2, "rotate": 90,},
        ],
    },

    "label_1_line": {
        "columns": [1],
        "rows": [1],
        "cells": [
            {"type": "text", "value": "input_text_1", "col": 0, "row": 0,},
        ],
    },

    "label_2_line": {
        "columns": [1],
        "rows": [0.5, 0.5],
        "cells": [
            {"type": "text", "value": "input_text_1", "col": 0, "row": 0,},
            {"type": "text", "value": "input_text_2", "col": 0, "row": 1,},
        ],
    },

    "classic_barcode": {
        "columns": [1], # Values > 1 directly map to pixels
        "rows": [0.8, 0.2],
        "cells": [
            {"type": "barcode", "value": "sku", "col": 0,"row": 0,},
            {"type": "text", "value": "sku", "col": 0, "row": 1,},
        ],
    },
}

class LabelMaker:    
    def __init__(self, font):
        self._font = font
        self._min_font_size = 6
        self._max_font_size = 96
        self._padding = 2

        # niimbot d110 specific stuff
        self._dpi = 203
        self._px_per_mm = self._dpi / 25.4

    def _text(self, text, width, height) -> Image.Image:
        temp_image = Image.new("RGB", [width, height])
        draw = ImageDraw.Draw(temp_image)

        def measure_text(font):
            bbox = draw.textbbox((0, 0), text, font=font)
            left, top, right, bottom = bbox

            return {
                "bbox": bbox,
                "width": right - left,
                "height": bottom - top,
            }

        def font_fits(font_size):
            selected_font = self._load_font(font_size)
            box = measure_text(selected_font)

            return box["width"] <= width and box["height"] <= height

        # Binary search for the largest font size that fits
        low = self._min_font_size
        high = self._max_font_size
        best_size = self._min_font_size

        while low <= high:
            mid = (low + high) // 2
    
            if font_fits(mid):
                best_size = mid
                low = mid + 1
            else:
                high = mid - 1

        selected_font = self._load_font(best_size)
        box = measure_text(selected_font)

        image = Image.new("RGB", [width, height], "white")
        draw = ImageDraw.Draw(image)

        left, top, right, bottom = box["bbox"]
        text_width = box["width"]
        text_height = box["height"]

        x = (width - text_width) / 2 - left
        y = (height - text_height) / 2 - top

        draw.text((x, y), text, font=selected_font, fill="black")

        return image
    
    def _barcode(self, text, width, height) -> Image.Image:
        barcode = Code128(text, writer=ImageWriter())

        modules = len(barcode.build()[0])
        quiet_modules = 10
        total_modules = modules + quiet_modules * 2

        module_px = width / total_modules
        module_width_mm = module_px / self._px_per_mm
        quiet_zone_mm = quiet_modules * module_width_mm

        vertical_margin_px = 6
        barcode_height_px = height - vertical_margin_px * 2
        module_height_mm = barcode_height_px / self._px_per_mm

        options = {
            "dpi": self._dpi,
            "module_width": module_width_mm,
            "module_height": module_height_mm,
            "quiet_zone": quiet_zone_mm,
            "write_text": False,
            "margin_top": vertical_margin_px / self._px_per_mm,
            "margin_bottom": vertical_margin_px / self._px_per_mm,
            "background": "white",
            "foreground": "black",
        }

        buffer = BytesIO()
        barcode.write(buffer, options)
        buffer.seek(0)

        image = Image.open(buffer).convert("RGB")

        return image.resize((width, height), Image.Resampling.NEAREST)

    def _load_font(self, font_size: int) -> ImageFont.ImageFont:
        if self._font is not None and self._font != "":
            return ImageFont.truetype(self._font, font_size)
        else:
            return ImageFont.load_default()

    def _png_filename(self, output_file: str) -> str:
        os.makedirs("/tmp/illusion/imgs/", exist_ok=True)

        output_path = os.path.join(
            "/tmp/illusion/imgs/",
            f"{output_file}",
        )

        if not output_path.endswith(".png"):
            output_path = f"{output_path}.png"

        return str(output_path)

    def _resolve_tracks(self, tracks, total_size):
        fixed_total = sum(track for track in tracks if track > 1)
        flexible_total = sum(track for track in tracks if track <= 1)

        remaining = total_size - fixed_total

        sizes = []

        for track in tracks:
            if track > 1:
                sizes.append(int(track))
            else:
                sizes.append(int(remaining * (track / flexible_total)))

        diff = total_size - sum(sizes)

        if sizes:
            sizes[-1] += diff

        return sizes

    def _grid_rect(self, style, width, height, cell):
        columns = self._resolve_tracks(style["columns"], width)
        rows = self._resolve_tracks(style["rows"], height)

        col = cell["col"]
        row = cell["row"]
        colspan = cell.get("colspan", 1)
        rowspan = cell.get("rowspan", 1)

        x = sum(columns[:col])
        y = sum(rows[:row])
        w = sum(columns[col : col + colspan])
        h = sum(rows[row : row + rowspan])

        return x, y, w, h

    def render_label(self, style_name, width, height, output="label", rotate=90, **values,):
        style = LABEL_STYLES[style_name]

        label = Image.new("RGB", (width, height), "white")

        for cell in style["cells"]:
            x, y, w, h = self._grid_rect(style, width, height, cell)

            padded_x = x + self._padding
            padded_y = y + self._padding
            padded_w = max(1, w - self._padding * 2)
            padded_h = max(1, h - self._padding * 2)

            value_key = cell["value"]

            if value_key not in values:
                raise ValueError(f"Missing value for label field: {value_key}")

            value = values[value_key]

            if cell["type"] == "text":
                if cell.get("rotate") in [90, 270]:
                    image = self._text(value, padded_h, padded_w)
                else:
                    image = self._text(value, padded_w, padded_h)
            elif cell["type"] == "barcode":
                image = self._barcode(value, padded_w, padded_h)

            if cell.get("rotate"):
                image = image.rotate(cell["rotate"], expand=True)

            paste_x = padded_x + (padded_w - image.width) // 2
            paste_y = padded_y + (padded_h - image.height) // 2

            label.paste(image, (paste_x, paste_y))

        if rotate:
            label = label.rotate(rotate, expand=True)

        output_path = self._png_filename(output)
        label.save(output_path)

        return output_path