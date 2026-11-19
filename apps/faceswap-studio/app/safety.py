"""Content safeguards: prompt filtering, NSFW image check, AI-generated watermark."""

from __future__ import annotations

import logging
import re
from collections.abc import Iterable

from PIL import Image, ImageDraw, ImageFont, PngImagePlugin

log = logging.getLogger("image_studio.safety")

WATERMARK_TEXT = "AI-generated"

# Explicit sexual content and anything involving minors is refused outright.
# Suggestive but non-explicit wording (sexy, lingerie, сексуальная) is allowed: the image
# classifier still rejects nudity. This list is a coarse first line of defence only.
_BASE_BLOCKED = (
    "nude",
    "naked",
    "nsfw",
    "porn",
    "sex",
    "sexual",
    "erotic",
    "topless",
    "undress",
    "hentai",
    "fetish",
    "explicit",
    "genital",
    "nipple",
    "голый",
    "голая",
    "обнаж",
    "порно",
    "секс",
    "секса",
    "сексе",
    "сексом",
    "сексу",
    "эрот",
    "раздет",
    "интим",
    "child",
    "kid",
    "minor",
    "underage",
    "teen",
    "loli",
    "schoolgirl",
    "ребен",
    "ребён",
    "детск",
    "дети",
    "детей",
    "несовершеннолет",
    "школьниц",
)


class ContentBlocked(Exception):
    """Raised when a request or a result violates the content policy."""


# Matched as whole words only, so that "sexy" / "сексуальная" are not caught by them
_WHOLE_WORD = frozenset({"sex", "sexual", "секс", "секса", "сексе", "сексом", "сексу"})


def check_prompt(prompt: str, extra_terms: Iterable[str] = ()) -> None:
    text = prompt.lower()
    for term in (*_BASE_BLOCKED, *extra_terms):
        # Whole-word terms match exactly; other Latin terms match on word start to avoid
        # false hits ("sussex", "kidney" stay ok-ish); Cyrillic stems match as substrings.
        if term in _WHOLE_WORD:
            pattern = rf"(?<!\w){re.escape(term)}(?!\w)"
        elif term.isascii():
            pattern = rf"\b{re.escape(term)}"
        else:
            pattern = re.escape(term)
        if re.search(pattern, text):
            raise ContentBlocked("Запрос нарушает правила контента и отклонён.")


class NsfwClassifier:
    """Optional transformers image classifier. Disabled if the model id is empty."""

    def __init__(self, model_id: str, threshold: float, device: str = "cpu") -> None:
        self.model_id = model_id
        self.threshold = threshold
        self.device = device
        self._pipe = None

    @property
    def enabled(self) -> bool:
        return bool(self.model_id)

    def _load(self):
        if self._pipe is None:
            from transformers import pipeline  # heavy import, deferred

            dev = 0 if self.device == "cuda" else -1
            self._pipe = pipeline("image-classification", model=self.model_id, device=dev)
        return self._pipe

    def check(self, image: Image.Image) -> None:
        if not self.enabled:
            return
        results = self._load()(image.convert("RGB"))
        for item in results:
            if item["label"].lower() in {"nsfw", "porn", "hentai"} and (item["score"] >= self.threshold):
                log.warning("NSFW result blocked (score=%.2f)", item["score"])
                raise ContentBlocked("Результат распознан как недопустимый контент.")


def apply_watermark(image: Image.Image, visible: bool = True) -> Image.Image:
    """Stamp a visible label in the corner. Metadata is added separately on save."""
    img = image.convert("RGB")
    if not visible:
        return img
    draw = ImageDraw.Draw(img, "RGBA")
    size = max(12, img.width // 40)
    try:
        font = ImageFont.load_default(size=size)
    except TypeError:  # Pillow < 10.1
        font = ImageFont.load_default()
    left, top, right, bottom = draw.textbbox((0, 0), WATERMARK_TEXT, font=font)
    w, h = right - left, bottom - top
    pad = max(4, size // 3)
    x = img.width - w - pad * 3
    y = img.height - h - pad * 3
    draw.rectangle((x - pad, y - pad, x + w + pad, y + h + pad * 2), fill=(0, 0, 0, 140))
    draw.text((x, y), WATERMARK_TEXT, font=font, fill=(255, 255, 255, 230))
    return img


def provenance_metadata(kind: str, prompt: str | None = None) -> PngImagePlugin.PngInfo:
    info = PngImagePlugin.PngInfo()
    info.add_text("ai_generated", "true")
    info.add_text("generator", "image-studio")
    info.add_text("operation", kind)
    if prompt:
        info.add_text("prompt", prompt[:1000])
    return info
