"""Which sent reference is which (#505).

H3's reference rule is one clear role per upload: number the references in the
order they are sent and say what each one controls. Unassigned references are a
common cause of the model copying the wrong part (a background person, a prop
from the place picture). The map is English and uses H3's own labels
(``<Picture N>``, ``<Audio N>``, ``<Video N>``), numbered per slot exactly as
``compile_draft`` fills ``inputs``. No line starts with a card name followed by a
colon: H3 voices "Name: text" as a spoken line (2026-11-07 Geryon run).
"""

# Pictures first, then videos, then sounds; numbers count within each slot.
LABELS = {"images": "Picture", "videos": "Video", "audios": "Audio"}


def _named(name, variant=""):
    name = " ".join(str(name or "").split())[:80]
    variant = " ".join(str(variant or "").split())[:40]
    return f"{name} ({variant})" if name and variant else name


# One sentence per role; {label} and {name} are filled in. No imperatives, no colons.
SENTENCES = {
    "character": "{label} is the identity reference for {name}, "
    "for the face, body and clothing.",
    "place": "{label} is the setting reference for the place {name}, "
    "for the environment and light only.",
    "prop": "{label} is the reference for the prop {name}, for its shape and colour.",
    "voice": "{label} is the voice timbre reference for {name}; "
    "no words are copied from it.",
    "ambience": "{label} is the ambient sound reference for the place {name}.",
    "soundtrack": "{label} is the finished dialogue soundtrack of the clip, "
    "reused exactly.",
}
EXTRA = {"Picture": "picture", "Video": "video", "Audio": "sound"}


def sentence(label, role):
    """One English line for one sent reference."""
    name = _named(role.get("name"), role.get("variant"))
    template = SENTENCES.get(role.get("kind"))
    if template:
        return template.format(label=label, name=name)
    what = EXTRA[label[1:].split()[0]]
    return f"{label} is an extra reference {what}" + (f" ({name})." if name else ".")


def role_map(references):
    """The map for ``[(ref, role)]`` in send order; empty when nothing is sent.

    Only references that are actually sent are numbered, so a dropped picture
    (over the model's limit) never shifts the numbers of the ones after it.
    """
    lines = []
    for slot, label in LABELS.items():
        number = 0
        for ref, role in references:
            if ref.get("slot") != slot or not ref.get("sent"):
                continue
            number += 1
            if role is not None:
                lines.append(sentence(f"<{label} {number}>", role))
    return "\n".join(lines)
