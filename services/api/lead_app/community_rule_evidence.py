"""Content completeness helpers, never account or community authorization."""

import re
from typing import Literal

from pydantic import BaseModel, ConfigDict, Field, model_validator


class PinReadResult(BaseModel):
    model_config = ConfigDict(extra="forbid")
    status: Literal["complete", "partial", "failed"]
    reader: Literal["carousel", "native_list"]
    reason: Literal["identity_changed", "incomplete_list", "timeout", "unreadable_body", "external_rules_required"] | None = None
    peer_binding: dict[str, str]
    message_ids: list[str] = Field(default_factory=list)
    content_refs: list[str] = Field(default_factory=list)
    total_count: int | None = Field(default=None, ge=0)
    next_cursor: str | None = None

    @model_validator(mode="after")
    def verify_completeness(self):
        if self.status == "complete":
            if (self.reason is not None or self.next_cursor is not None
                    or self.total_count is None or self.total_count != len(self.message_ids)
                    or len(set(self.message_ids)) != len(self.message_ids)
                    or len(self.content_refs) != self.total_count):
                raise ValueError("Complete pins require a unique, fully evidenced list")
        elif self.reason is None:
            raise ValueError("An incomplete read must explain its gap")
        return self


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


def rules_status(*, description: str, pinned_complete: bool, external_rules_unresolved: bool) -> Literal["complete", "unresolved"]:
    if external_rules_unresolved:
        return "unresolved"
    references_pins = re.search(
        r"(?:rules?|群规|群規).{0,20}(?:pinned|置顶|置頂)|"
        r"(?:see|read|查看|参见|參見).{0,18}(?:pinned|置顶|置頂)",
        description, re.I,
    )
    return "unresolved" if references_pins and not pinned_complete else "complete"
