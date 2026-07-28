from io import BytesIO
from pathlib import Path
from typing import Optional
import os

from barcode import Code128
from barcode.writer import ImageWriter
from PIL import Image, ImageDraw, ImageFont

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

    
    # +------------------------+-----+
    # | Barcode                |  S  |
    # |                        |  K  |
    # |                        |  U  |
    # +------------------------+-----+
    def label_barcode(self, sku, width, height, output="label", rotate=90):        
        sku_label_w = height - (self._padding * 2)
        sku_label_h = 30
        sku_label_x = width - sku_label_h + self._padding
        sku_label_y = 0 + self._padding

        barcode_w = width - sku_label_h - (self._padding * 2)
        barcode_h = int((height) - (self._padding * 2))
        barcode_x = 0 + self._padding
        barcode_y = 0 + self._padding

        label = Image.new("RGB", (width, height), "white")

        barcode = self._barcode(sku, barcode_w, barcode_h)
        sku_label = self._text(sku, sku_label_w, sku_label_h - (self._padding * 2))

        sku_label = sku_label.rotate(90, expand=True)

        label.paste(barcode, (barcode_x, barcode_y))
        label.paste(sku_label, (sku_label_x, sku_label_y))

        label = label.rotate(rotate, expand=True)
        
        output_path = self._png_filename(output)
        label.save(output_path)

        return output_path
    
    # +------------------------+-----+
    # | Text                   |  S  |
    # |------------------------|  K  |
    # | Barcode                |  U  |
    # +------------------------+-----+
    def label_text_barcode(self, sku, input_text, width, height, output="label", rotate=90, text_split = 0.5, barcode_split = 0.5):
        if text_split + barcode_split != 1.0:
            raise ValueError("Split values must be equal to 1.0")
        
        sku_label_w = height - (self._padding * 2)
        sku_label_h = 30
        sku_label_x = width - sku_label_h + self._padding
        sku_label_y = 0 + self._padding

        text_w = width - sku_label_h - (self._padding * 2)
        text_h = int((height * text_split) - (self._padding * 2))
        text_x = 0 + self._padding
        text_y = 0 + self._padding

        barcode_w = width - sku_label_h - (self._padding * 2)
        barcode_h = int((height * barcode_split) - (self._padding * 2))
        barcode_x = 0 + self._padding
        barcode_y = text_h + 1 + self._padding

        label = Image.new("RGB", (width, height), "white")

        barcode = self._barcode(sku, barcode_w, barcode_h)
        text = self._text(input_text, text_w, text_h)
        sku_label = self._text(sku, sku_label_w, sku_label_h - (self._padding * 2))

        sku_label = sku_label.rotate(90, expand=True)

        label.paste(barcode, (barcode_x, barcode_y))
        label.paste(text, (text_x, text_y))
        label.paste(sku_label, (sku_label_x, sku_label_y))

        label = label.rotate(rotate, expand=True)
        
        output_path = self._png_filename(output)
        label.save(output_path)

        return output_path

    # +------------------------------+
    # | Text                         |
    # |                              |
    # |                              |
    # +------------------------------+
    def label_text(self, input_text, width, height, output="label", rotate=90):

        text_w = width - (self._padding * 2)
        text_h = int(height - (self._padding * 2))
        text_x = 0 + self._padding
        text_y = 0 + self._padding

        label = Image.new("RGB", (width, height), "white")

        text1 = self._text(input_text, text_w, text_h)

        label.paste(text1, (text_x, text_y))

        label = label.rotate(rotate, expand=True)
        
        output_path = self._png_filename(output)
        label.save(output_path)

        return output_path

    # +------------------------------+
    # | Text 1                       |
    # |------------------------------|
    # | Text 2                       |
    # +------------------------------+
    def label_text_text(self, input_text_1, input_text_2, width, height, output="label", rotate=90, text_1_split = 0.5, text_2_split = 0.5):
        if text_1_split + text_2_split != 1.0:
            raise ValueError("Split values must be equal to 1.0")

        text1_w = width - (self._padding * 2)
        text1_h = int((height * text_1_split) - (self._padding * 2))
        text1_x = 0 + self._padding
        text1_y = 0 + self._padding

        text2_w = width - (self._padding * 2)
        text2_h = int((height * text_2_split) - (self._padding * 2))
        text2_x = 0 + self._padding
        text2_y = text1_h + 1 + self._padding

        label = Image.new("RGB", (width, height), "white")

        text1 = self._text(input_text_1, text1_w, text1_h)
        text2 = self._text(input_text_2, text2_w, text2_h)

        label.paste(text1, (text1_x, text1_y))
        label.paste(text2, (text2_x, text2_y))

        label = label.rotate(rotate, expand=True)
        
        output_path = self._png_filename(output)
        label.save(output_path)

        return output_path

    # Standard size barcodes, mostly here because I had it in the old barcode_generator.py
    def generate_barcode(self, text: str, output_file: str = "barcode") -> str:
        barcode = Code128(text, writer=ImageWriter())
        output_path = self._png_filename(output_file)
        filename = barcode.save(output_path)

        return filename