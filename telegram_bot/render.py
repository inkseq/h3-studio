"""Local Pillow/ffmpeg rendering for the demo adapter. No network, no GPU."""

from __future__ import annotations

from pathlib import Path
import shutil
import subprocess

from PIL import Image, ImageDraw, ImageFont, ImageOps

FONT_CANDIDATES = [
    "C:/Windows/Fonts/msyh.ttc",
    "C:/Windows/Fonts/simhei.ttf",
    "/usr/share/fonts/opentype/noto/NotoSansCJK-Regular.ttc",
    "/usr/share/fonts/noto-cjk/NotoSansCJK-Regular.ttc",
    "/usr/share/fonts/truetype/wqy/wqy-microhei.ttc",
]
SIZES = {"9:16": (720, 1280), "16:9": (1280, 720)}
FPS = 25


def font(size):
    for candidate in FONT_CANDIDATES:
        if Path(candidate).exists():
            return ImageFont.truetype(candidate, size)
    return ImageFont.load_default(size)


def clock(seconds):
    seconds = int(round(seconds))
    return f"{seconds // 60}:{seconds % 60:02d}"


def wrap(draw, text, face, width):
    lines, line = [], ""
    for char in text:
        if draw.textlength(line + char, font=face) > width and line:
            lines.append(line)
            line = char
        else:
            line += char
    if line:
        lines.append(line)
    return lines


def cover(path, size):
    with Image.open(path) as image:
        return ImageOps.fit(ImageOps.exif_transpose(image).convert("RGB"), size)


def grid(board, out):
    cols, tile_w, tile_h, pad = 3, 300, 400, 16
    if board.aspect == "16:9":
        tile_h = 169
    rows = (len(board.cards) + cols - 1) // cols
    text_h, head = 96, 84
    width = pad + cols * (tile_w + pad)
    height = head + rows * (tile_h + text_h + pad) + pad
    sheet = Image.new("RGB", (width, height), (16, 18, 17))
    draw = ImageDraw.Draw(sheet)
    draw.text((pad, 18), board.title, font=font(30), fill=(238, 241, 236))
    draw.text(
        (pad, 56),
        f"{len(board.cards)} 个镜头 · {clock(board.duration)} · {board.aspect}",
        font=font(18),
        fill=(154, 163, 156),
    )
    small, label = font(18), font(22)
    for number, card, start, end in board.timeline():
        index = number - 1
        x = pad + (index % cols) * (tile_w + pad)
        y = head + (index // cols) * (tile_h + text_h + pad)
        sheet.paste(cover(board.photos[card.photo - 1], (tile_w, tile_h)), (x, y))
        draw.rectangle((x, y, x + 54, y + 34), fill=(199, 243, 107))
        draw.text((x + 8, y + 4), f"#{number}", font=label, fill=(11, 12, 12))
        draw.text(
            (x, y + tile_h + 6),
            f"{clock(start)}–{clock(end)}  {card.title}",
            font=label,
            fill=(238, 241, 236),
        )
        for line_no, line in enumerate(
            wrap(draw, card.description, small, tile_w)[:2]
        ):
            draw.text(
                (x, y + tile_h + 38 + line_no * 24),
                line,
                font=small,
                fill=(154, 163, 156),
            )
    sheet.save(out, quality=90)
    return out


def card_still(board, number, card, out):
    """One full-frame still per card, slightly oversized so the clip can pan."""
    w, h = SIZES[board.aspect]
    big = (int(w * 1.12), int(h * 1.12))
    image = cover(board.photos[card.photo - 1], big)
    draw = ImageDraw.Draw(image, "RGBA")
    bar = int(big[1] * 0.2)
    draw.rectangle((0, big[1] - bar, big[0], big[1]), fill=(0, 0, 0, 150))
    title = font(int(w * 0.06))
    body = font(int(w * 0.035))
    margin = int(w * 0.16)  # stays inside the frame for any pan offset
    draw.text(
        (margin, big[1] - bar + 20), card.title, font=title, fill=(255, 255, 255)
    )
    for line_no, line in enumerate(
        wrap(draw, card.description, body, big[0] - 2 * margin)[:2]
    ):
        draw.text(
            (margin, big[1] - bar + 30 + int(w * 0.075) + line_no * int(w * 0.045)),
            line,
            font=body,
            fill=(230, 230, 230),
        )
    tag, label = font(int(w * 0.04)), f"#{number} · v{card.version} · DEMO"
    pad = int(w * 0.02)
    right = margin + int(draw.textlength(label, font=tag)) + 2 * pad
    draw.rectangle((margin, margin, right, margin + int(w * 0.04) + 2 * pad),
                   fill=(199, 243, 107, 230))  # fmt: skip
    draw.text((margin + pad, margin + pad // 2), label, font=tag, fill=(11, 12, 12))
    image.save(out)
    return out


def ffmpeg():
    binary = shutil.which("ffmpeg")
    if not binary:
        raise RuntimeError("ffmpeg is required for the demo film")
    return binary


def run(args):
    subprocess.run(
        [ffmpeg(), "-hide_banner", "-loglevel", "error", "-y", *args],
        check=True,
        stdin=subprocess.DEVNULL,
    )


def clip(board, number, card, folder):
    """Pan across the still; direction changes with card number and version."""
    folder = Path(folder)
    still = card_still(board, number, card, folder / f"card{number}-v{card.version}.png")
    out = folder / f"card{number}-v{card.version}.mp4"
    w, h = SIZES[board.aspect]
    d = card.seconds
    direction = (number + card.version) % 4
    x = {
        0: f"(iw-{w})*t/{d}",
        1: f"(iw-{w})*(1-t/{d})",
        2: f"(iw-{w})/2",
        3: f"(iw-{w})/2",
    }[direction]
    y = {
        0: f"(ih-{h})/2",
        1: f"(ih-{h})/2",
        2: f"(ih-{h})*t/{d}",
        3: f"(ih-{h})*(1-t/{d})",
    }[direction]
    run(
        [
            "-loop", "1", "-framerate", str(FPS), "-t", f"{d:.3f}", "-i", str(still),
            "-vf", f"crop={w}:{h}:{x}:{y},format=yuv420p",
            "-r", str(FPS), "-c:v", "libx264", "-preset", "veryfast",
            "-crf", "23", "-an", str(out),
        ]
    )  # fmt: skip
    return str(out)


def concat(clips, out):
    listing = Path(out).with_suffix(".txt")
    listing.write_text(
        "".join(f"file '{Path(c).as_posix()}'\n" for c in clips), encoding="utf-8"
    )
    run(["-f", "concat", "-safe", "0", "-i", str(listing), "-c", "copy",
         "-movflags", "+faststart", str(out)])  # fmt: skip
    return str(out)


def frame(film_path, second, out):
    run(["-ss", f"{max(second, 0):.2f}", "-i", str(film_path),
         "-frames:v", "1", "-q:v", "3", str(out)])  # fmt: skip
    return str(out)
