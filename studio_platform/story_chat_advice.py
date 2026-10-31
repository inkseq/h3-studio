"""Local advice checks; never preflight, submit work, or write a document."""

import copy
import re

from .capabilities import capabilities
from .generation_draft import read_draft

PICTURE = re.compile(
    r"画|绘|立绘|生.*图|draw|redraw|portrait|generate.*(?:image|picture)", re.I
)
GENERATE = re.compile(
    r"生成.*(?:视频|镜头)|制作.*(?:视频|影片)|make.*(?:video|shot|film)|generate|render",
    re.I,
)


def turn_chat(chat, text):
    """The conversation keeps its language until the person clearly writes in another.

    Chinese characters switch to Chinese; at least two English words switch to
    English. A typo, one word, a number or an emoji ('asdf', 'ok', '👍') keeps the
    current language (owner 2026-10-29: 'asdf' flipped a Chinese chat to English).
    """
    value = copy.deepcopy(chat)
    current = chat.get("language", "zh")
    if re.search(r"[㐀-鿿]", text):
        value["language"] = "zh"
    elif len(re.findall(r"[A-Za-z]{2,}", text)) >= 2:
        value["language"] = "en"
    else:
        value["language"] = current
    return value


def instruction(base, enabled):
    return (
        base
        + (
            "\nPictures are enabled. Proposals still require explicit confirmation."
            if enabled
            else "\nPictures are OFF. Never propose_image, promise pictures, or offer drawing chips. "
            "Tell the person to upload a photo and attach it to the card instead."
        )
        + "\nBefore offering video generation, check the shot has usable reference inputs. If missing, explain what to upload or attach first."
    )


def guard(guided, settings, project, actions, plan, proposal, *, enabled, lang):
    preview = copy.deepcopy(project)
    for action in actions:
        guided.apply(preview, action)
    plan, proposal = copy.deepcopy(plan), copy.deepcopy(proposal)
    shots = [e for e in preview["entities"] if e["type"] == "shot"]
    selected = proposal.get("shot_ids", []) if proposal else []
    caps = capabilities(settings)
    blocked = [
        shot
        for shot in shots
        if (not selected or shot["id"] in selected)
        and read_draft(preview, shot["id"], capabilities=caps)[1]
    ]
    # Only a proposal is a request to act. An offered chip that cannot work
    # is just removed below; it never replaces the model's reply (#699).
    picture_refused = not enabled and bool(
        proposal and proposal.get("picture_card_ids")
    )
    generation_refused = bool(blocked) and bool(selected)
    if picture_refused and proposal:
        proposal.pop("picture_card_ids", None)
    if generation_refused and proposal:
        proposal["shot_ids"] = []
        proposal.pop("risk_review", None)
    if proposal and not proposal.get("shot_ids"):
        if proposal.get("picture_card_ids"):
            proposal["kind"] = "image"
        elif proposal.get("new_story"):
            proposal["kind"] = "new_story"
        else:
            proposal = None
    plan["chips"] = [
        chip
        for chip in plan.get("chips", [])
        if not (not enabled and PICTURE.search(chip))
        and not (
            blocked and GENERATE.search(chip) and not (enabled and PICTURE.search(chip))
        )
    ]
    if generation_refused:
        plan["reply"] = (
            "还有镜头未准备好。请先在分镜中补齐参考素材和动作描述，再生成视频。"
            if lang == "zh"
            else "Some shots are not ready. Add usable references and complete their action descriptions in the storyboard before making the video."
        )
    if picture_refused:
        tip = (
            "图片生成目前关闭。请上传照片并添加到对应卡片。"
            if lang == "zh"
            else "Picture generation is off. Upload a photo and attach it to the matching card."
        )
        plan["reply"] = plan["reply"] + " " + tip if generation_refused else tip
    return plan, proposal
