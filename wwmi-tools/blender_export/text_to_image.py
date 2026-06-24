from typing import Tuple, List

try:
    from PIL import Image, ImageDraw, ImageFont
except ImportError:
    import sys
    import subprocess
    import site
    target = site.getsitepackages()[1]
    subprocess.check_call([sys.executable, '-m', 'pip', 'install', 'pillow', '--target', target])
    from PIL import Image, ImageDraw, ImageFont


def _wrap_text(text: str, font: ImageFont.FreeTypeFont, max_width: int) -> List[str]:
    """Wrap text to fit within max_width pixels, breaking at word boundaries when possible."""
    lines = []
    for paragraph in text.split('\n'):
        words = paragraph.split(' ')
        current_line = ''
        for word in words:
            test_line = current_line + (' ' if current_line else '') + word
            bbox = font.getbbox(test_line)
            line_width = bbox[2] - bbox[0]
            if line_width <= max_width:
                current_line = test_line
            else:
                if current_line:
                    lines.append(current_line)
                # If a single word exceeds max_width, force-split it
                if font.getbbox(word)[2] - font.getbbox(word)[0] > max_width:
                    current_line = ''
                    for ch in word:
                        test = current_line + ch
                        if font.getbbox(test)[2] - font.getbbox(test)[0] > max_width and current_line:
                            lines.append(current_line)
                            current_line = ch
                        else:
                            current_line = test
                    if current_line:
                        lines.append(current_line)
                    current_line = ''
                else:
                    current_line = word
        if current_line:
            lines.append(current_line)
    return lines


class Text2Image:

    def __init__(
        self,
        font_path: str = "msyh.ttc",
        font_size: int = 32,
        text_color: Tuple[int, int, int, int] = (220, 220, 220, 255),
        padding: Tuple[int, int, int, int] = (8, 8, 8, 8),
        border_thickness: int = 2,
        border_radius: int = 10,
        border_color: Tuple[int, int, int, int] = (100, 100, 100, 255),
        bg_color: Tuple[int, int, int, int] = (18, 24, 35, 128),
    ):
        self.font_path = font_path
        self.font_size = font_size
        self.text_color = text_color
        self.padding = padding
        self.border_thickness = border_thickness
        self.border_radius = border_radius
        self.border_color = border_color
        self.bg_color = bg_color

    def _load_font(self) -> ImageFont.FreeTypeFont:
        try:
            return ImageFont.truetype(self.font_path, self.font_size)
        except IOError:
            print(f"Font file not found: {self.font_path}, fallback to default.")
            return ImageFont.load_default()

    def generate(self, text: str, output_path: str) -> None:
        font = self._load_font()

        bbox = font.getbbox(text)
        text_width = bbox[2] - bbox[0]
        text_height = bbox[3] - bbox[1]

        p_top, p_bottom, p_left, p_right = self.padding
        img_width = p_left + text_width + p_right
        img_height = p_top + text_height + p_bottom

        image = Image.new("RGBA", (img_width, img_height), self.bg_color)
        draw = ImageDraw.Draw(image)

        if self.border_thickness > 0:
            offset = self.border_thickness / 2
            border_box = [
                offset,
                offset,
                img_width - offset,
                img_height - offset,
            ]
            draw.rounded_rectangle(
                border_box,
                radius=self.border_radius,
                outline=self.border_color,
                width=self.border_thickness,
            )

        text_x = p_left - bbox[0]
        text_y = p_top - bbox[1]
        draw.text((text_x, text_y), text, font=font, fill=self.text_color)

        image.save(output_path)
        print(f"Image saved: {output_path}")

    def generate_fixed(self, text: str, output_path: str, width: int, height: int = None, text_align: str = 'left', line_spacing: float = 0.2) -> Tuple[int, int]:
        """Generate image with fixed width (and optionally fixed height).
        Text that exceeds width wraps to new lines.
        If height is given, image is exactly width x height and text clips if it overflows.
        text_align: 'left' or 'right'
        Returns (width, height) of the generated image."""
        font = self._load_font()

        p_top, p_bottom, p_left, p_right = self.padding
        max_text_width = width - p_left - p_right

        lines = _wrap_text(text, font, max_text_width)

        # Calculate total text height
        line_heights = []
        for line in lines:
            bbox = font.getbbox(line)
            line_heights.append(bbox[3] - bbox[1])
        text_height = sum(line_heights)
        if len(lines) > 1:
            text_height += int((line_heights[0] if line_heights else 0) * line_spacing) * (len(lines) - 1)

        if height is not None:
            img_height = height
        else:
            img_height = p_top + text_height + p_bottom

        max_text_height = img_height - p_top - p_bottom

        image = Image.new("RGBA", (width, img_height), self.bg_color)
        draw = ImageDraw.Draw(image)

        if self.border_thickness > 0:
            offset = self.border_thickness / 2
            border_box = [
                offset,
                offset,
                width - offset,
                img_height - offset,
            ]
            draw.rounded_rectangle(
                border_box,
                radius=self.border_radius,
                outline=self.border_color,
                width=self.border_thickness,
            )

        # Draw each line (clip if exceeds max_text_height)
        # Vertical centering: if height is fixed, start offset so text block is centered
        if height is not None:
            vertical_offset = max(0, (max_text_height - text_height) // 2)
        else:
            vertical_offset = 0
        y = p_top + vertical_offset
        for i, line in enumerate(lines):
            if y - p_top >= max_text_height:
                break
            bbox = font.getbbox(line)
            if text_align == 'right':
                line_width = bbox[2] - bbox[0]
                text_x = width - p_right - line_width
            else:
                text_x = p_left - bbox[0]
            draw.text((text_x, y - bbox[1]), line, font=font, fill=self.text_color)
            y += line_heights[i]
            if i < len(lines) - 1:
                y += int(line_heights[0] * line_spacing)

        image.save(output_path)
        print(f"Image saved: {output_path} ({width}x{img_height})")
        return width, img_height


def generate_solid_background(output_path: str, width: int = 200, height: int = 600):
    """Generate a fully transparent background image for the UI."""
    image = Image.new("RGBA", (width, height), (0, 0, 0, 0))
    image.save(output_path)
    print(f"Background image saved: {output_path}")


def generate_button_border(output_path: str, width: int, height: int, border_color=(255, 255, 255, 255), border_thickness=2, border_radius=10):
    """Generate a button border image (white outline on transparent bg, reused for tinting)."""
    image = Image.new("RGBA", (width, height), (0, 0, 0, 0))
    draw = ImageDraw.Draw(image)
    offset = border_thickness / 2
    border_box = [offset, offset, width - offset, height - offset]
    draw.rounded_rectangle(border_box, radius=border_radius, outline=border_color, width=border_thickness)
    image.save(output_path)
    print(f"Button border image saved: {output_path}")


def generate_button_background(output_path: str, width: int, height: int, bg_color=(0, 0, 0, 128), border_radius=10, border_thickness=4):
    """Generate a button background image (semi-transparent fill, no border, reused).
    Fills inside the border area to avoid overflow at rounded corners."""
    image = Image.new("RGBA", (width, height), (0, 0, 0, 0))
    draw = ImageDraw.Draw(image)
    inset = border_thickness
    inner_radius = max(0, border_radius - border_thickness // 2)
    draw.rounded_rectangle(
        [inset, inset, width - inset, height - inset],
        radius=inner_radius,
        fill=bg_color,
    )
    image.save(output_path)
    print(f"Button background image saved: {output_path}")
