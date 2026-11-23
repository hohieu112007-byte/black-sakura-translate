"""Scope & Subtitle Filtering (FULL_PATCH | SUB_ONLY | UI_ONLY)."""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from typing import Any, Dict, List, Optional

SUB_KEY_TOKENS = {
    "SUB", "SUBTITLE", "SUBTITLES", "DLG", "DIALOG", "DIALOGUE", "STORY",
    "CUTSCENE", "TALK", "CHATTER", "NARRATIVE", "QUEST_TEXT", "CONV",
    "CONVERSATION", "SPEECH", "VOICE", "VO_", "LINE_", "CAPTION", "LOG_",
}

CHOICE_KEY_TOKENS = {
    "CHOICE", "CHOICES", "REPLY", "ANSWER", "SELECT_OPTION", "DIALOG_OPT",
}

UI_KEY_TOKENS = {
    "UI", "HUD", "BTN", "BUTTON", "MENU", "OPT", "OPTION", "OPTIONS",
    "SETTING", "SETTINGS", "KEYBIND", "INV", "INVENTORY", "TOOLTIP", "STAT",
    "STATS", "ITEM", "SKILL", "ACHIEVEMENT", "TITLE", "LABEL", "HEADER",
    "TAB", "POPUP", "ERR", "ERROR", "MSG_SYS",
}

TECHNICAL_SKIP_RE = re.compile(
    r"^(?:[A-Za-z0-9_./\\:-]+(?:\.[A-Za-z0-9]{1,5})?|https?://\S+|\d+(?:\.\d+)?%?|[A-Z0-9_]{2,})$"
)
SENTENCE_HINT_RE = re.compile(r"[.?!…,:;\"'—\-]|(?:\b(?:I|you|we|they|he|she|it|is|are|was|were|have|has|do|did|will|can|not|the|a|an|this|that|to|for|with)\b)", re.IGNORECASE)


@dataclass
class LocEntry:
    entry_id: str
    key: str
    text: str
    file_path: str
    lang: str
    fmt: str
    category: str = "UI"  # "SUBTITLE" | "CHOICE" | "UI"
    location_meta: Dict[str, Any] = field(default_factory=dict)


def is_likely_subtitle(text: str) -> bool:
    """Grammatical & structural heuristic fallback when Key ID lacks clear prefixes."""
    if not text:
        return False
    clean = text.strip()
    if len(clean) < 4:
        return False
    if TECHNICAL_SKIP_RE.match(clean):
        return False
    words = clean.split()
    # Multi-word sentence or punctuation ending strongly indicates dialogue/subtitle
    if len(words) >= 5:
        return True
    if any(clean.endswith(ch) for ch in (".", "!", "?", "…", '"', "'")):
        return True
    if len(words) >= 3 and SENTENCE_HINT_RE.search(clean):
        return True
    # 1-2 short Title Case or single words without punctuation are usually UI labels
    return False


def classify_entry_category(key: str, text: str) -> str:
    """Classify an extracted localization entry into SUBTITLE | CHOICE | UI."""
    k_upper = re.sub(r"[^A-Z0-9_]", "_", (key or "").upper())
    tokens = [t for t in k_upper.split("_") if t]

    # Exact choice keys first
    if any(tok in CHOICE_KEY_TOKENS for tok in tokens) or "CHOICE" in k_upper:
        return "CHOICE"

    # Explicit story/subtitle keys
    if any(tok in SUB_KEY_TOKENS for tok in tokens) or any(
        hint in k_upper for hint in ("SUBTITLE", "DIALOG", "CUTSCENE", "STORY", "QUEST_TEXT", "CHATTER")
    ):
        return "SUBTITLE"

    # Explicit UI/HUD/item keys
    if any(tok in UI_KEY_TOKENS for tok in tokens) or any(
        hint in k_upper for hint in ("TOOLTIP", "SETTING", "KEYBIND", "MENU_", "UI_", "HUD_", "BTN_")
    ):
        return "UI"

    return "SUBTITLE" if is_likely_subtitle(text) else "UI"


def is_translatable_string(text: str) -> bool:
    """Reject empty, purely numeric, hex hash/UUID, file-path, placeholder-only, or URL strings."""
    if not text:
        return False
    s = text.strip()
    if not s:
        return False
    # Reject MD5/SHA hex hashes & Unity/Unreal GUIDs (e.g. 'a41c1d89086f1fe47b219ee85f2604f7')
    if re.match(r"^[0-9a-f]{16,}$", s, re.IGNORECASE) and any(ch.isdigit() for ch in s):
        return False
    if re.match(r"^[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}$", s, re.IGNORECASE):
        return False
    # Reject pure lowercase snake_case internal keys without spaces
    if " " not in s and "_" in s and re.match(r"^[a-z0-9_]+$", s):
        return False
    if re.match(r"^https?://\S+$", s, re.IGNORECASE):
        return False
    if re.match(r"^[A-Za-z0-9_./\\-]+\.(png|jpg|jpeg|ogg|wav|mp3|mp4|json|csv|txt|prefab|asset|mat|bundle)$", s, re.IGNORECASE):
        return False
    # Must contain at least one alphabetic letter outside inline markup/placeholders
    prose = re.sub(
        r"</?[^>]+>|\{[^{}\r\n]{1,64}\}|\[(?!g:)(?:/?(?:b|i|u|s|color|size|sprite|align|link|url|sub|sup|indent|margin|line-height|nobr|noparse|lowercase|uppercase|allcaps|smallcaps|voffset|space|cspace|mspace|pos|width|font|material|style)(?:[= ][^\]\r\n]*)?|\d+|[A-Za-z0-9_.-]*(?:[_=#/\\:0-9]|[a-z][A-Z])[A-Za-z0-9_.-]*)\]",
        " ",
        s,
    )
    if not any(ch.isalpha() for ch in prose):
        return False
    return True


def filter_entries_by_scope(
    entries: List[LocEntry],
    scope: str = "FULL_PATCH",
    translate_choices: bool = True,
) -> List[LocEntry]:
    """Filter localization entries according to FULL_PATCH | SUB_ONLY | UI_ONLY."""
    norm_scope = (scope or "FULL_PATCH").upper()
    out: List[LocEntry] = []
    for e in entries:
        if not is_translatable_string(e.text):
            continue
        cat = e.category or classify_entry_category(e.key, e.text)
        e.category = cat
        if norm_scope == "FULL_PATCH":
            out.append(e)
        elif norm_scope == "SUB_ONLY":
            if cat == "SUBTITLE" or (cat == "CHOICE" and translate_choices):
                out.append(e)
        elif norm_scope == "UI_ONLY":
            if cat == "UI":
                out.append(e)
        else:
            out.append(e)
    return out
