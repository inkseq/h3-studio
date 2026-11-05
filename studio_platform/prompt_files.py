"""Prompts live as plain files in ``prompts/`` so they can be read without the code.

``docs/AI-FLOWS.md`` shows where each file is used. A file holds its text plus one final
newline. A file of named parts uses ``## name`` headings. A missing file stops the app at
import, so a release cannot run without its prompts.
"""

import re
from pathlib import Path

ROOT = Path(__file__).with_name("prompts")
_HEADING = re.compile(r"^## (\w+)\n", re.M)


def text(name):
    return (ROOT / f"{name}.md").read_text(encoding="utf-8").removesuffix("\n")


def sections(name):
    parts = _HEADING.split(text(name))
    if parts[0].strip():
        raise ValueError("prompt_sections_invalid")
    return {
        key: value.strip("\n")
        for key, value in zip(parts[1::2], parts[2::2], strict=True)
    }
