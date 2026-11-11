"""Content completeness helpers, never account or community authorization."""

import re
from typing import Literal


def description_kind(text: str, *, topic_matches: bool) -> Literal["substantive", "title_only", "empty"]:
    content = re.sub(r"https?://\S+|(?:www\.|t\.me/)\S+|(?<!\w)@[\w]+", "", text)
    letters = [char for char in content if char.isalpha()]
    if not letters:
        return "empty"
    if not topic_matches:
        return "title_only"
    words = re.findall(r"[^\W\d_]+", content, re.UNICODE)
    # Keep the existing spaced-language gate. Non-spaced descriptions need a
    # full clause as well as content, so a short community name is not rules.
    nonspaced_clause = len(letters) >= 12 and bool(re.search(r"[。！？；、，]", content))
    return "substantive" if len(words) >= 5 or nonspaced_clause else "title_only"
