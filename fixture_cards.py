"""Generate the split-color fixture/result cards used by the Discord bot."""

from __future__ import annotations

import datetime
import io
import re

from PIL import Image, ImageDraw, ImageFont


WIDTH = 1200
HEIGHT = 700
FONT_PATH = "/usr/share/fonts/truetype/dejavu/DejaVuSans-Bold.ttf"


def _font(size: int) -> ImageFont.FreeTypeFont | ImageFont.ImageFont:
    try:
        return ImageFont.truetype(FONT_PATH, size)
    except OSError:
        return ImageFont.load_default()


def _initials(name: str) -> str:
    words = [word for word in re.split(r"[^A-Za-z0-9]+", name.strip()) if word]
    if not words:
        return "FC"
    if len(words) == 1:
        return words[0][:3].upper()
    return "".join(word[0] for word in words[:3]).upper()


def _fit_text(draw: ImageDraw.ImageDraw, text: str, max_width: int, size: int) -> tuple[str, ImageFont.ImageFont]:
    font = _font(size)
    def text_width(value: str, selected_font: ImageFont.ImageFont) -> int:
        bounds = draw.textbbox((0, 0), value, font=selected_font)
        return bounds[2] - bounds[0]

    if text_width(text, font) <= max_width:
        return text, font
    while size > 20:
        size -= 2
        font = _font(size)
        if text_width(text, font) <= max_width:
            return text, font
    clipped = text
    while clipped and text_width(f"{clipped}…", font) > max_width:
        clipped = clipped[:-1]
    return f"{clipped}…", font


def _centered(draw: ImageDraw.ImageDraw, xy: tuple[int, int], text: str, font: ImageFont.ImageFont, fill: str) -> None:
    bbox = draw.textbbox((0, 0), text, font=font)
    draw.text((xy[0] - (bbox[2] - bbox[0]) / 2, xy[1] - (bbox[3] - bbox[1]) / 2), text, font=font, fill=fill)


def _paste_logo(image: Image.Image, logo_data: bytes | None, box: tuple[int, int, int, int]) -> bool:
    if not logo_data:
        return False
    try:
        logo = Image.open(io.BytesIO(logo_data)).convert("RGBA")
        max_width = box[2] - box[0]
        max_height = box[3] - box[1]
        logo.thumbnail((max_width, max_height), Image.Resampling.LANCZOS)
        x = box[0] + (max_width - logo.width) // 2
        y = box[1] + (max_height - logo.height) // 2
        image.paste(logo, (x, y), logo)
        return True
    except (OSError, ValueError):
        return False


def render_fixture_card(
    team_a: str,
    team_b: str,
    timestamp: int,
    score_a: int | None = None,
    score_b: int | None = None,
    logo_a: bytes | None = None,
    logo_b: bytes | None = None,
) -> io.BytesIO:
    """Render a 1200×700 fixture or result card as a PNG buffer."""

    image = Image.new("RGB", (WIDTH, HEIGHT), "#101216")
    draw = ImageDraw.Draw(image)
    left = (220, 0, 55)
    right = (0, 155, 214)
    for x in range(WIDTH):
        ratio = x / WIDTH
        if ratio < 0.5:
            blend = ratio * 2
            color = tuple(round(left[i] * (1 - blend) + (20, 20, 25)[i] * blend) for i in range(3))
        else:
            blend = (ratio - 0.5) * 2
            color = tuple(round((20, 20, 25)[i] * blend + right[i] * (1 - blend)) for i in range(3))
        draw.line((x, 0, x, HEIGHT), fill=color)

    # Subtle oversized initials imitate the watermarking in the reference graphic.
    watermark_font = _font(270)
    _centered(draw, (230, 190), _initials(team_a), watermark_font, "#ed3c62")
    _centered(draw, (970, 190), _initials(team_b), watermark_font, "#27b5e9")

    # Use real crests when automatic lookup found one; initials remain a reliable fallback.
    draw.ellipse((100, 145, 360, 405), fill="#d9003f", outline="#ff7c99", width=8)
    draw.ellipse((840, 145, 1100, 405), fill="#079bd4", outline="#7ee0ff", width=8)
    if not _paste_logo(image, logo_a, (130, 175, 330, 375)):
        _centered(draw, (230, 275), _initials(team_a), _font(82), "#ffffff")
    if not _paste_logo(image, logo_b, (870, 175, 1070, 375)):
        _centered(draw, (970, 275), _initials(team_b), _font(82), "#ffffff")

    center_x1, center_y1, center_x2, center_y2 = 410, 205, 790, 475
    draw.rounded_rectangle((center_x1, center_y1, center_x2, center_y2), radius=10, fill="#08090b", outline="#25272b", width=3)
    match_time = datetime.datetime.fromtimestamp(timestamp, tz=datetime.timezone.utc)
    date_label = match_time.strftime("%a. %d/%m/%y").upper()
    time_label = match_time.strftime("%H:%M") + " UTC"
    _centered(draw, (600, 255), "FIXTURE" if score_a is None else "FINAL RESULT", _font(28), "#2581ff")
    _centered(draw, (600, 305), date_label, _font(34), "#ffffff")
    _centered(draw, (600, 350), time_label, _font(34), "#ffffff")
    score_label = "VS" if score_a is None or score_b is None else f"{score_a} - {score_b}"
    _centered(draw, (600, 425), score_label, _font(76), "#ffffff")

    team_a_label, team_a_font = _fit_text(draw, team_a.upper(), 290, 32)
    team_b_label, team_b_font = _fit_text(draw, team_b.upper(), 285, 32)
    draw.rounded_rectangle((55, 555, 430, 625), radius=28, fill="#08090b")
    draw.rounded_rectangle((770, 555, 1145, 625), radius=28, fill="#08090b")
    draw.ellipse((70, 568, 112, 610), fill="#d9003f")
    draw.ellipse((785, 568, 827, 610), fill="#079bd4")
    _centered(draw, (91, 589), _initials(team_a)[:2], _font(18), "#ffffff")
    _centered(draw, (806, 589), _initials(team_b)[:2], _font(18), "#ffffff")
    draw.text((128, 576), team_a_label, font=team_a_font, fill="#ffffff")
    bbox = draw.textbbox((0, 0), team_b_label, font=team_b_font)
    draw.text((1125 - (bbox[2] - bbox[0]), 576), team_b_label, font=team_b_font, fill="#ffffff")

    buffer = io.BytesIO()
    image.save(buffer, format="PNG", optimize=True)
    buffer.seek(0)
    return buffer