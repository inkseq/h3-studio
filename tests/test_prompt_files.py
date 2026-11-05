"""Prompts are files, and the flow page stays in step with them."""

from pathlib import Path
import re
import unittest

from studio_platform import prompt_files

REPO = Path(__file__).resolve().parents[1]
FLOWS = (REPO / "docs" / "AI-FLOWS.zh-CN.md").read_text(encoding="utf-8")


class PromptFileTests(unittest.TestCase):
    def test_every_file_is_on_the_flow_page_and_every_link_exists(self):
        files = {path.name for path in prompt_files.ROOT.glob("*.md")}
        linked = set(re.findall(r"studio_platform/prompts/(\w+\.md)\)", FLOWS))
        self.assertEqual(files - linked, set(), "add these to AI-FLOWS.zh-CN.md")
        self.assertEqual(linked - files, set(), "these links point at nothing")

    def test_every_name_in_the_code_has_a_file(self):
        code = "\n".join(
            path.read_text(encoding="utf-8")
            for path in (REPO / "studio_platform").glob("*.py")
        )
        names = set(re.findall(r'prompt_(?:text|sections)\(\s*"(\w+)"', code))
        self.assertTrue(names)
        for name in names:
            self.assertTrue((prompt_files.ROOT / f"{name}.md").is_file(), name)

    def test_releases_carry_the_files(self):
        for ignore in (".gitignore", ".dockerignore"):
            text = (REPO / ignore).read_text(encoding="utf-8")
            self.assertIn("!studio_platform/prompts/*.md", text, ignore)

    def test_text_drops_only_the_final_newline(self):
        self.assertTrue(prompt_files.text("story_assistant").endswith("\n"))
        self.assertFalse(prompt_files.text("story_title").endswith("\n"))

    def test_sections_are_named_parts(self):
        parts = prompt_files.sections("first_frame")
        self.assertIn("{number}", parts["character"])
        self.assertEqual(
            set(prompt_files.sections("card_picture_styles")),
            {"character", "location", "prop"},
        )
