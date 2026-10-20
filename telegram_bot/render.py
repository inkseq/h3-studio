"""Local Pillow/ffmpeg rendering for the demo adapter. No network, no GPU."""

from __future__ import annotations

from pathlib import Path
import shutil
import subprocess

from PIL import Image, ImageDraw, ImageEnhance, ImageFont, ImageOps

FONT_CANDIDATES = [
    "C:/Windows/Fonts/msyh.ttc",
    "C:/Windows/Fonts/simhei.ttf",
    "/usr/share/fonts/opentype/noto/NotoSansCJK-Regular.ttc",
    "/usr/share/fonts/noto-cjk/NotoSansCJK-Regular.ttc",
    "/usr/share/fonts/truetype/wqy/wqy-microhei.ttc",
]
SIZES = {"9:16": (720, 1280), "16:9": (1280, 720)}
FPS = 25
INK, PAPER, MUTED, ACCENT = (11, 12, 12), (238, 241, 236), (154, 163, 156), (199, 243, 107)


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


def styled(path, size, look=None):
    """Cover-crop a photo and apply the demo look (brightness, warmth, colour)."""
    with Image.open(path) as image:
        image = ImageOps.fit(ImageOps.exif_transpose(image).convert("RGB"), size)
    look = look or {}
    if look.get("brightness"):
        image = ImageEnhance.Brightness(image).enhance(look["brightness"])
    if look.get("color") is not None:
        image = ImageEnhance.Color(image).enhance(look["color"])
    if look.get("warmth"):
        r, g, b = image.split()
        shift = int(40 * look["warmth"])
        r = r.point(lambda v: min(255, max(0, v + shift)))
        b = b.point(lambda v: min(255, max(0, v - shift)))
        image = Image.merge("RGB", (r, g, b))
    return image


def sheet(tiles, title, subtitle, out, *, tall=True):
    """tiles: [(photo_path, tag, line1, line2, look)] in a three-column sheet."""
    cols, tile_w, pad = 3, 300, 16
    tile_h = 400 if tall else 169
    rows = max(1, (len(tiles) + cols - 1) // cols)
    text_h, head = 96, 84
    canvas = Image.new(
        "RGB",
        (pad + cols * (tile_w + pad), head + rows * (tile_h + text_h + pad) + pad),
        (16, 18, 17),
    )
    draw = ImageDraw.Draw(canvas)
    draw.text((pad, 18), title, font=font(30), fill=PAPER)
    draw.text((pad, 56), subtitle, font=font(18), fill=MUTED)
    small, label = font(18), font(22)
    for index, (path, tag, line1, line2, look) in enumerate(tiles):
        x = pad + (index % cols) * (tile_w + pad)
        y = head + (index // cols) * (tile_h + text_h + pad)
        canvas.paste(styled(path, (tile_w, tile_h), look), (x, y))
        width = int(draw.textlength(tag, font=label)) + 16
        draw.rectangle((x, y, x + width, y + 34), fill=ACCENT)
        draw.text((x + 8, y + 4), tag, font=label, fill=INK)
        draw.text((x, y + tile_h + 6), line1, font=label, fill=PAPER)
        for line_no, line in enumerate(wrap(draw, line2, small, tile_w)[:2]):
            draw.text((x, y + tile_h + 38 + line_no * 24), line, font=small, fill=MUTED)
    canvas.save(out, quality=90)
    return out


def card_still(board, number, card, out):
    """One full-frame still per card, slightly oversized so the clip can pan."""
    w, h = SIZES[board.aspect]
    big = (int(w * 1.12), int(h * 1.12))
    image = styled(board.photos[card.photo - 1], big, card.look)
    draw = ImageDraw.Draw(image, "RGBA")
    bar = int(big[1] * 0.2)
    draw.rectangle((0, big[1] - bar, big[0], big[1]), fill=(0, 0, 0, 150))
    title, body = font(int(w * 0.06)), font(int(w * 0.035))
    margin = int(w * 0.16)  # stays inside the frame for any pan offset
    draw.text((margin, big[1] - bar + 20), card.title, font=title, fill=(255, 255, 255))
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
                   fill=ACCENT + (230,))  # fmt: skip
    draw.text((margin + pad, margin + pad // 2), label, font=tag, fill=INK)
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
    stem = f"card{number}-v{card.version}-p{card.photo}-{board.aspect.replace(':', 'x')}"
    still = card_still(board, number, card, folder / f"{stem}.png")
    out = folder / f"{stem}.mp4"
    w, h = SIZES[board.aspect]
    d = card.seconds
    direction = (number + card.version) % 4
    x = [f"(iw-{w})*t/{d}", f"(iw-{w})*(1-t/{d})", f"(iw-{w})/2", f"(iw-{w})/2"][direction]
    y = [f"(ih-{h})/2", f"(ih-{h})/2", f"(ih-{h})*t/{d}", f"(ih-{h})*(1-t/{d})"][direction]
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
