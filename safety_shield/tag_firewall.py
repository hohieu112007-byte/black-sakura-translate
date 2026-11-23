"""Layer 4: Tag Firewall (__TAG_x__ / __NL__) + Template Folding (__NUM_x__) + Validation."""

from __future__ import annotations

import re
from collections import Counter
from typing import Dict, Optional, Tuple

# Ordered carefully so compound tags match before sub-patterns.
# Square-bracket pattern protects markup/variables ([color=#ff0], [b], [0], [DOOR_A], [player_name], [player])
# while allowing natural accented/multi-word UI bracket badges like [Étiquette] to be translated normally.
_TECH_BRACKET_TAG = (
    r"\[(?!g:)(?:"
    r"/?(?:b|i|u|s|color|size|sprite|align|link|url|sub|sup|indent|margin|line-height|nobr|noparse|lowercase|uppercase|allcaps|smallcaps|voffset|space|cspace|mspace|pos|width|font|material|style)(?:[= ][^\]\r\n]*)?"
    r"|\d+"
    r"|[A-Za-z0-9_.-]*(?:[_=#/\\:0-9]|[a-z][A-Z])[A-Za-z0-9_.-]*"
    r")\]"
)
_GENDER_TAG_RE = re.compile(r"\[g:([^|\]]*)\|([^\]]*)\]")

TAG_PATTERNS = [
    r"</?[a-zA-Z0-9_=#\"'\-\.: ]+>",                     # Unity / TMP / Ren'Py HTML-like tags (<color=#FFF>, </b>, <br>)
    r"\{[^{}\r\n]{1,64}\}",                             # {player_name}, {0}, {color=#fff}, {w=0.5}
    r"\\(?:[A-Za-z]{1,4}|[.$|^!><{}\\]|\[[^\]]{1,32}\])",  # RPG Maker \C[1], \N[1], \V[2], \. \!
    _TECH_BRACKET_TAG,                                  # [DOOR_A], [KEY_E], [color=#ff0], [0], [heroName]
    r"%(?:\d+\$)?[+\-#0]*\d*(?:\.\d+)?[hlLzjt]*(?:[diuoxXfFeEgGaAcspn](?![A-Za-zÀ-ỹ0-9_])|%)",  # printf %s, %d, %1$s, %.2f (never '% pour')
]
COMBINED_TAG_RE = re.compile("|".join(f"({p})" for p in TAG_PATTERNS))

# Step 2 Template Folding regex: standalone integers, decimals, percentages, signed modifiers outside tags
STANDALONE_NUM_RE = re.compile(r"(?<![A-Za-z0-9_])[+-]?\d+(?:[.,]\d+)?%?(?![A-Za-z0-9_])")

TOKEN_RE = re.compile(r"__TAG_(\d+)__|__NUM_(\d+)__|__NL__")
_PLURAL_LOCALIZED_KEY_RE = re.compile(r"(\{\d+:)(?:số lượng|số nhiều|số ít|plural)(:)", re.IGNORECASE)


def flatten_romance_gender_tags(text: str) -> str:
    """Resolve French/Romance [g:masc|fem] or Citoyen[g:|ne] gender tags cleanly to readable form."""
    if not text or "[g:" not in text:
        return text
    return _GENDER_TAG_RE.sub(lambda m: (m.group(1) or "").strip(), text)


def mask_tags(text: str) -> Tuple[str, Dict[str, str]]:
    """Mask inline control codes / markup tags into __TAG_x__ and newlines into __NL__."""
    if not text:
        return "", {}

    tag_map: Dict[str, str] = {}
    counter = 0

    normalized = flatten_romance_gender_tags(text.replace("\r\n", "\n").replace("\r", "\n"))

    def _replace_match(m: re.Match) -> str:
        nonlocal counter
        token = f"__TAG_{counter}__"
        tag_map[token] = m.group(0)
        counter += 1
        return token

    masked = COMBINED_TAG_RE.sub(_replace_match, normalized)
    masked = masked.replace("\n", " __NL__ ").strip()
    masked = re.sub(r" {2,}", " ", masked)
    return masked, tag_map


def mask_tags_and_numbers(text: str) -> Tuple[str, Dict[str, str], Dict[str, str]]:
    """2-Step Masking:
    Step 1: Mask tags & newlines -> __TAG_x__ / __NL__.
    Step 2: Mask standalone numbers outside tags -> __NUM_x__.
    """
    if not text:
        return "", {}, {}

    tag_masked, tag_map = mask_tags(text)
    num_map: Dict[str, str] = {}
    n_counter = 0

    def _replace_num(m: re.Match) -> str:
        nonlocal n_counter
        tok = f"__NUM_{n_counter}__"
        num_map[tok] = m.group(0)
        n_counter += 1
        return tok

    folded_masked = STANDALONE_NUM_RE.sub(_replace_num, tag_masked)
    return folded_masked, tag_map, num_map


def fold_source_numbers(text: str) -> Tuple[str, str, Dict[str, str], Dict[str, str]]:
    """Return (folded_source_text, folded_masked_for_llm, tag_map, num_map).

    folded_source_text keeps original newlines & tags while folding numbers into __NUM_x__
    so repeated templates like 'Deals 15 fire damage' & 'Deals 20 fire damage' share one hash.
    """
    if not text:
        return "", "", {}, {}

    folded_masked, tag_map, num_map = mask_tags_and_numbers(text)
    if not num_map:
        return text, folded_masked, tag_map, {}

    folded_source = re.sub(r" ?__NL__ ?", "\n", folded_masked)
    if tag_map:
        folded_source = re.sub(
            r"__TAG_\d+__",
            lambda m: tag_map.get(m.group(0), m.group(0)),
            folded_source,
        )
    return folded_source, folded_masked, tag_map, num_map


def unfold_numbers(text: str, num_map: Optional[Dict[str, str]]) -> str:
    """Restore __NUM_x__ tokens back to exact location numbers cleanly."""
    if not text or not num_map:
        return text
    return re.sub(r"__NUM_\d+__", lambda m: num_map.get(m.group(0), m.group(0)), text)


def _expected_tokens(masked_src: str) -> Counter:
    return Counter(m.group(0) for m in TOKEN_RE.finditer(masked_src))


def validate_and_unmask(
    masked_src: str,
    translated_masked: str,
    tag_map: Dict[str, str],
    original_src: str,
    num_map: Optional[Dict[str, str]] = None,
    keep_num_tokens: bool = False,
) -> Tuple[bool, str, str]:
    """Verify exact __TAG_x__, __NUM_x__, and __NL__ integrity; unmask cleanly.

    When keep_num_tokens=True, __NUM_x__ remains intact for template storage in unique_strings
    and is unfolded per-location via num_map during export/application.
    """
    if translated_masked is None or not str(translated_masked).strip():
        return False, original_src, "EMPTY_OUTPUT"

    candidate = str(translated_masked).strip()

    # Repair accidental whitespace inserted inside tokens by LLM
    candidate = re.sub(r"__\s*TAG_(\d+)\s*__", r"__TAG_\1__", candidate)
    candidate = re.sub(r"__\s*NUM_(\d+)\s*__", r"__NUM_\1__", candidate)
    candidate = re.sub(r"__\s*NL\s*__", "__NL__", candidate)

    # Reject corrupted __NL__ artifacts like "\n__\n" or isolated "__" not in source
    if "__" not in original_src and re.search(r"(?:\\n|\s|^)__(?:\\n|\s|$)", candidate):
        return False, original_src, "STRAY_NL_ARTIFACT"

    expected = _expected_tokens(masked_src)
    actual = _expected_tokens(candidate)

    if expected != actual:
        return False, original_src, f"TAG_MISMATCH expected={dict(expected)} actual={dict(actual)}"

    # Protect nested SmartString formatters like {0:plural:{} ngày|{} ngày}
    if ":plural:" in masked_src:
        candidate = _PLURAL_LOCALIZED_KEY_RE.sub(r"\1plural\2", candidate)
        if candidate.count("{") != masked_src.count("{") or candidate.count("}") != masked_src.count("}"):
            return False, original_src, "PLURAL_BRACE_MISMATCH"
        if candidate.count("|") != masked_src.count("|"):
            return False, original_src, "PLURAL_PIPE_MISMATCH"

    # Reject stray pipe injection (e.g. QA split artifact "{} crédits}. |Tớ...") when source lacks pipes
    if "|" in candidate and "|" not in masked_src:
        return False, original_src, "STRAY_PIPE_ARTIFACT"

    # Restore numbers first (unless storing template in DB), then newlines and tags
    restored = candidate
    if num_map and not keep_num_tokens:
        restored = unfold_numbers(restored, num_map)
    restored = re.sub(r" ?__NL__ ?", "\n", restored)
    if tag_map:
        restored = re.sub(
            r"__TAG_\d+__",
            lambda m: tag_map.get(m.group(0), m.group(0)),
            restored,
        )

    return True, restored, "OK"
