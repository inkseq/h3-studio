"""A film made of same-size shots keeps their frame (#1252).

Shots are generated at the model's own canvas (832x480 for 16:9 at 480P),
while an export box is a broadcast size (854x480, 1280x720). Exporting a
film of 832x480 shots at the old default, 720P, scaled every shot up, added
thin bars left and right and encoded the whole film again: 30-54 s of the
export on a 2-vCPU host (staging 2026-11-18), a larger file and no more
detail. When no size is asked for and every shot's selected take has the
same frame, that frame is the film's; the renderer then joins the shots as
they are (render_copy).
"""

from fractions import Fraction

# render_request.validate_render_request's render_dimensions_limit.
MINIMUM, MAXIMUM, MAXIMUM_AREA = 256, 1280, 1280 * 720
ASPECT_TOLERANCE = Fraction(5, 100)


def valid(frame):
    """A size the CPU renderer accepts as its output."""
    try:
        width, height = frame
    except (TypeError, ValueError):
        return False
    return (
        type(width) is int
        and type(height) is int
        and MINIMUM <= width <= MAXIMUM
        and MINIMUM <= height <= MAXIMUM
        and not width % 2
        and not height % 2
        and width * height <= MAXIMUM_AREA
    )


def label(frame):
    """The resolution name shown for a frame."""
    return "480P" if min(frame) <= 480 else "720P"


def shared_frame(project, shots, source_resolver, aspect, missing=(ValueError,)):
    """The one frame every shot's selected take has, or None.

    `shots` are the whole film's, so two chapters of different sizes share
    none. None also when a take's size is unknown (an upload), when the size
    is outside what the renderer writes (768P takes: 1344x768), or when it
    is not the story's aspect; the export box is used then, as before.
    `missing` are the resolver's own "no such source" errors, which the
    compile reports; anything else (a database error) is raised.
    """
    lookup = {entity["id"]: entity for entity in project["entities"]}
    sizes = set()
    for shot in shots:
        entity = lookup.get(shot.get("data", {}).get("selectedAssetId"))
        if entity is None or entity.get("type") != "video":
            return None
        try:
            source = source_resolver(entity, "video")
        except missing:
            return None
        sizes.add((source.get("width"), source.get("height")))
        if len(sizes) > 1:
            return None
    if not sizes:
        return None
    frame = sizes.pop()
    if not valid(frame):
        return None
    try:
        across, down = (int(part) for part in aspect.split(":"))
        wanted = Fraction(across, down)
    except (AttributeError, ValueError, ZeroDivisionError):
        return None
    if abs(Fraction(*frame) - wanted) > wanted * ASPECT_TOLERANCE:
        return None
    return frame


def choose(frame, box):
    """(width, height, resolution name) for a chapter render.

    `frame` None is the export `box` as asked; a (width, height) is the
    frame its film chose (film_runs), never a client's.
    """
    if frame is None:
        return box
    if not valid(frame):
        raise ValueError("粗剪画面尺寸无效")
    return frame[0], frame[1], label(frame)
