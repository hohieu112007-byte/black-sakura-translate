"""Layer 5: Encoding/BOM Guard (charset-normalizer), <think> Stripper, Word-Wrap & Validators."""

from __future__ import annotations

import json
import re
import textwrap
import xml.etree.ElementTree as ET
from pathlib import Path
from typing import Any, Dict, Tuple

from charset_normalizer import from_bytes

THINK_BLOCK_RE = re.compile(r"<think>.*?</think>", re.DOTALL | re.IGNORECASE)
STRAY_THINK_TAG_RE = re.compile(r"</?think>", re.IGNORECASE)


def strip_thinking(text: str) -> str:
    """Remove Qwen <think>...</think> blocks or orphaned tags from translated lines."""
    if not text:
        return ""
    cleaned = THINK_BLOCK_RE.sub("", text)
    cleaned = STRAY_THINK_TAG_RE.sub("", cleaned)
    return cleaned.strip()


def detect_encoding_and_eol(raw_bytes: bytes) -> Dict[str, Any]:
    """Detect exact BOM / charset / line ending of original game file via charset-normalizer."""
    if not raw_bytes:
        return {"encoding": "utf-8", "bom": b"", "newline": "\n", "text": ""}

    bom = b""
    codec = "utf-8"

    if raw_bytes.startswith(b"\xef\xbb\xbf"):
        bom = b"\xef\xbb\xbf"
        codec = "utf-8-sig"
    elif raw_bytes.startswith(b"\xff\xfe\x00\x00"):
        bom = b"\xff\xfe\x00\x00"
        codec = "utf-32-le"
    elif raw_bytes.startswith(b"\x00\x00\xfe\xff"):
        bom = b"\x00\x00\xfe\xff"
        codec = "utf-32-be"
    elif raw_bytes.startswith(b"\xff\xfe"):
        bom = b"\xff\xfe"
        codec = "utf-16-le"
    elif raw_bytes.startswith(b"\xfe\xff"):
        bom = b"\xfe\xff"
        codec = "utf-16-be"
    else:
        best = from_bytes(raw_bytes).best()
        if best is not None:
            codec = best.encoding or "utf-8"

    try:
        text = raw_bytes.decode(codec, errors="replace")
    except Exception:
        codec = "utf-8"
        text = raw_bytes.decode("utf-8", errors="replace")

    # Strip leading BOM char if utf-8-sig wasn't used on decode
    if text.startswith("\ufeff"):
        text = text.lstrip("\ufeff")

    crlf_count = text.count("\r\n")
    lf_count = text.count("\n") - crlf_count
    newline = "\r\n" if crlf_count >= lf_count and crlf_count > 0 else "\n"

    return {
        "encoding": codec,
        "bom": bom,
        "newline": newline,
        "text": text,
    }


def write_preserving_encoding(
    file_path: str | Path,
    text: str,
    encoding: str = "utf-8",
    bom: bytes = b"",
    newline: str = "\n",
) -> None:
    """Write text preserving the exact original encoding, BOM header, and EOL."""
    normalized = text.replace("\r\n", "\n").replace("\r", "\n")
    if newline == "\r\n":
        normalized = normalized.replace("\n", "\r\n")

    enc_lower = encoding.lower()
    if enc_lower == "utf-8-sig":
        payload = b"\xef\xbb\xbf" + normalized.encode("utf-8")
    elif bom and enc_lower in ("utf-16-le", "utf-16-be", "utf-32-le", "utf-32-be"):
        payload = bom + normalized.encode(enc_lower)
    else:
        try:
            encoded = normalized.encode(encoding, errors="strict")
            payload = (bom if not bom else b"") + encoded if enc_lower.startswith("utf-") else encoded
            if bom and not payload.startswith(bom):
                payload = bom + encoded
        except UnicodeEncodeError:
            # If target language (e.g. Vietnamese UTF-8) cannot fit legacy cp1252, upgrade cleanly to UTF-8 (with BOM if original had BOM)
            payload = (bom if bom else b"") + normalized.encode("utf-8")

    Path(file_path).write_bytes(payload)


def smart_word_wrap(text: str, max_line_width: int = 0) -> str:
    """Optionally wrap long single-line dialogue cleanly without breaking XML/format tags."""
    if not text or max_line_width <= 0:
        return text
    if len(text) <= max_line_width or "<" in text or "{" in text:
        return text
    lines = []
    for raw_line in text.split("\n"):
        if len(raw_line) <= max_line_width:
            lines.append(raw_line)
        else:
            lines.extend(textwrap.wrap(raw_line, width=max_line_width, break_long_words=False, break_on_hyphens=False))
    return "\n".join(lines)


def validate_serialized_syntax(ext: str, text: str) -> Tuple[bool, str]:
    """Validate structured file output before overwriting original game files."""
    ext_l = ext.lower()
    try:
        if ext_l == ".json":
            json.loads(text)
        elif ext_l == ".xml":
            ET.fromstring(text)
        return True, "OK"
    except Exception as exc:
        return False, f"Syntax error ({ext_l}): {exc}"


_TAG_STRIP_RE = re.compile(r"</?[^>]+>|\{[^{}\r\n]{1,64}\}|\[[^\[\]\r\n]{1,48}\]|__TAG_\d+__|__NL__")
_CJK_RE = re.compile(r"[\u4e00-\u9fff\u3040-\u30ff\uac00-\ud7af]")
_UNEXPECTED_SCRIPT_RE = re.compile(r"[\u0400-\u04ff\u0600-\u06ff\u0e00-\u0e7f\u0900-\u097f]")
_MOJIBAKE_HINT_RE = re.compile(r"(?:Ã[\x80-\xbf\xa0-\xbf]|áº[\x80-\xbf\u20ac-\u2122]|á»[\x80-\xbf\u20ac-\u2122]|â€[œž™¦¢]|Ä[\x90\x91]|Æ[\xb0\xa1])")
_REPEAT_WORD_RE = re.compile(r"\b([a-zA-ZÀ-ỹ]{2,})\b(?:\s+\1\b){3,}", re.IGNORECASE)
_CONSONANT_GARBLE_RE = re.compile(r"\b[bcdfghjklmnpqrstvwxzđ]{5,}\b")
_VI_ACCENT_CHAR_RE = re.compile(
    r"[ăâêôơưđàáảãạằắẳẵặầấẩẫậèéẻẽẹềếểễệìíỉĩịòóỏõọồốổỗộờớởỡợùúủũụừứửữựỳýỷỹỵ]",
    re.IGNORECASE,
)
_BAD_VI_LETTER_ACCENT_RE = re.compile(r"\b\w*[wjfz]\w*[ăâêôơưđạảãằắặẳẵầấậẩẫẹẻẽềếệểễịỉĩọỏõồốộổỗờớợởỡụủũừứựửữỳỵỷỹ]|\b\w*[ăâêôơưđạảãằắặẳẵầấậẩẫẹẻẽềếệểễịỉĩọỏõồốộổỗờớợởỡụủũừứựửữỳỵỷỹ]\w*[wjfz]\w*", re.IGNORECASE)
_PROMPT_ECHO_RE = re.compile(r"(?:ID\|TranslatedText|Here is the translation|Dưới đây là bản dịch|STRICT OUTPUT PROTOCOL)", re.IGNORECASE)
_QA_PIPE_PREFIX_RE = re.compile(r"^[^|{}]*\}\s*[^|]*\|")
_BROKEN_PLURAL_KEY_RE = re.compile(r"\{\d+:(?:số lượng|số nhiều|số ít):", re.IGNORECASE)
_FR_ONLY_DIAC_RE = re.compile(r"[çÇœŒëËïÏüÜäÄ]")
_FR_LEAK_TOKEN_RE = re.compile(
    r"\b(?:dialogues?|crédits?|jours?|flacons?|étiquettes?|stade|caught|"
    r"vous|vos|dans|avec|cette|votre|sont|toutes|après|avant|pendant|contre|"
    r"rouleau|graine|tristement|bouiboui|trafic|cellule|chimiste|bénévole|usurier|mineur|"
    r"expulsion|arrestation|saisie|lynchage|assassinat|explosion|anonyme|"
    r"piquette|calibre|contrebandier|citoyen|citoyenne|fureteur|fureteuse|mendiant|mendiante|"
    r"chargeur|munitions?|viseur|silencieux|pistolet|puce|remise|basique|spectrale|rouille|"
    r"carcasse|célèbre|éviter|révolutionnaire|désespéré|modifiée|pénurie|volontaire|"
    r"réducteur|stabilisant|scellé|louche|corrompue|instantané|préréglage|addict|chef|eau|less)\b",
    re.IGNORECASE,
)
_FR_MORPH_RE = re.compile(
    r"^(?:[A-Za-zÀ-ÿ]+(?:eau|eaux|ment|tion|sion|iste|aire|age|ette|eur|euse|ien|ienne|ier|ière|ique|ure))$",
    re.IGNORECASE,
)
_VI_UNACCENTED_SYLLABLE_RE = re.compile(
    r"^(?:ngh|ng|nh|ch|tr|th|kh|gh|ph|gi|qu|[bcdghklmnpqrstvx])?(?:[aeiouy]{1,3})(?:ng|nh|ch|[cmnpt])?$",
    re.IGNORECASE,
)
_ALLOWED_LOANWORDS_VI = frozenset(
    {
        "menu", "start", "ok", "vip", "usb", "ram", "cpu", "gpu", "fps", "ui", "hud", "ai", "id",
        "logo", "module", "signal", "laser", "turbo", "nitro", "bonus", "video", "audio", "radio",
        "taxi", "bus", "bar", "club", "hotel", "game", "steam", "discord", "workshop", "email",
        "internet", "camera", "micro", "pizza", "burger", "soda", "cola", "cafe", "cafein",
        "protein", "calo", "cal", "mm", "sr", "compact", "ramen", "preset", "tab", "cyanua",
        "hmm", "hmmm", "hmmmm", "shh", "zzz", "psst",
        "kcal", "cassette", "heh", "mmmm",
    }
)
_FR_STOPWORDS = frozenset(
    {"pour", "les", "des", "une", "est", "qui", "que", "pas", "plus", "moins", "sur", "par", "aux", "ces"}
)


def try_fix_mojibake(text: str) -> str | None:
    """Attempt to recover UTF-8 text that was mistakenly decoded as Latin-1 / cp1252."""
    if not text or not _MOJIBAKE_HINT_RE.search(text):
        return None
    for enc in ("latin1", "cp1252"):
        try:
            recovered = text.encode(enc).decode("utf-8")
            if recovered != text and "\ufffd" not in recovered and not _MOJIBAKE_HINT_RE.search(recovered):
                return recovered
        except Exception:
            continue
    return None


_UPPER_VI_LETTERS = "A-ZÀÁẠẢÃĂẰẮẶẲẴÂẦẤẬẨẪÈÉẸẺẼÊỀẾỆỂỄÌÍỊỈĨÒÓỌỎÕÔỒỐỘỔỖƠỜỚỢỞỠÙÚỤỦŨƯỪỨỰỬỮỲÝỴỶỸĐ"
_LOWER_VI_LETTERS = "a-zàáạảãăằắặẳẵâầấậẩẫèéẹẻẽêềếệểễìíịỉĩòóọỏõôồốộổỗơờớợởỡùúụủũưừứựửữỳýỵỷỹđ"
_WEIRD_CASE_RE = re.compile(rf"^[{_UPPER_VI_LETTERS}]{{2,}}[{_LOWER_VI_LETTERS}]{{2,}}$")
_WORD_VI_RE = re.compile(rf"[{_UPPER_VI_LETTERS}{_LOWER_VI_LETTERS}]+")


_GENDER_TAG_FLAT_RE = re.compile(r"\[g:([^\|\]]*)\|([^\]]*)\]")


def _flatten_gender_tags(text: str) -> str:
    return _GENDER_TAG_FLAT_RE.sub(lambda m: (m.group(1) or m.group(2) or "").strip(), text)


def detect_translation_anomaly(
    source_text: str,
    translated_text: str,
    target_lang: str = "vi",
    source_lang: str = "auto",
) -> Tuple[bool, str]:
    """Detect non-target language leaks, mojibake, gibberish, word repetition, or LLM bloat.

    Returns (has_anomaly, reason_code). Clean translations return (False, "OK").
    """
    if not translated_text or not str(translated_text).strip():
        return True, "EMPTY"

    src_clean = _TAG_STRIP_RE.sub(" ", _flatten_gender_tags(str(source_text or ""))).strip()
    tr_clean = _TAG_STRIP_RE.sub(" ", str(translated_text)).strip()
    tgt = (target_lang or "vi").lower()

    if "\ufffd" in translated_text or re.search(r"[\x00-\x08\x0b\x0c\x0e-\x1f]", translated_text):
        return True, "MOJIBAKE"

    if _MOJIBAKE_HINT_RE.search(translated_text):
        return True, "MOJIBAKE"

    if _PROMPT_ECHO_RE.search(tr_clean):
        return True, "PROMPT_LEAK"

    if (" => " in tr_clean and " => " not in src_clean) or (" -> " in tr_clean and " -> " not in src_clean):
        return True, "PROMPT_LEAK"
    if ("[EN]" in translated_text and "[EN]" not in (source_text or "")) or ("[VI]" in translated_text and "[VI]" not in (source_text or "")):
        return True, "PROMPT_LEAK"

    if "__" not in (source_text or "") and re.search(r"(?:\\n|\s|^)__(?:\\n|\s|$)|__NL__|__TAG_\d+__", translated_text):
        return True, "UNMASK_LEAK"

    if tgt in ("vi", "en", "fr", "es", "de", "pl", "pt", "pt-br", "it", "tr", "nl", "sv"):
        if _CJK_RE.search(tr_clean) and not _CJK_RE.search(src_clean):
            return True, "CJK_LEAK"
        if _UNEXPECTED_SCRIPT_RE.search(tr_clean) and not _UNEXPECTED_SCRIPT_RE.search(src_clean):
            return True, "SCRIPT_LEAK"

    if _REPEAT_WORD_RE.search(tr_clean):
        return True, "REPETITION_LOOP"

    if len(src_clean) >= 4 and len(tr_clean) > max(140, int(len(src_clean) * 4.5)):
        return True, "LENGTH_BLOAT"

    # Single short UI label (<= 6 chars, 1 token) cannot explode into a 4+ word unrelated phrase (catches ID-shift like Sit -> Đảo ngược trục Y)
    if 1 <= len(src_clean) <= 6 and " " not in src_clean and "\n" not in src_clean and len(tr_clean.split()) >= 4:
        return True, "SHORT_LABEL_MISMATCH"

    if tgt == "vi":
        tr_raw = str(translated_text)
        src_raw = str(source_text or "")
        src_l = (source_lang or "auto").split("-")[0].lower()
        if _QA_PIPE_PREFIX_RE.match(tr_raw) or (tr_raw.count("|") > src_raw.count("|")):
            return True, "QA_PIPE_CORRUPTION"
        if _BROKEN_PLURAL_KEY_RE.search(tr_raw):
            return True, "PLURAL_KEY_CORRUPT"
        if "[g:" in tr_raw:
            return True, "GENDER_TAG_LEAK"
        if _FR_ONLY_DIAC_RE.search(tr_clean) and (tr_clean != src_clean or _FR_LEAK_TOKEN_RE.search(tr_clean)):
            return True, "FR_DIACRITIC_LEAK"
        if _FR_LEAK_TOKEN_RE.search(tr_clean):
            return True, "FR_LEAK"
        src_words_lo = {w.lower() for w in _WORD_VI_RE.findall(src_clean)}
        tr_tokens = _WORD_VI_RE.findall(tr_clean)
        if _VI_ACCENT_CHAR_RE.search(tr_clean):
            for idx_t, tok in enumerate(tr_tokens):
                tl = tok.lower()
                if len(tok) >= 5 and tl in src_words_lo and not tok.isupper() and tl not in _ALLOWED_LOANWORDS_VI:
                    # Skip TitleCase surnames preceded by another TitleCase name (e.g. "Arthur Lafleur")
                    if tok[0].isupper() and idx_t > 0 and tr_tokens[idx_t - 1][0].isupper():
                        continue
                    # Skip literal filename identifiers (e.g. "communication.exe")
                    if re.search(rf"\b{re.escape(tok)}\.[A-Za-z0-9]{{2,5}}\b", tr_clean):
                        continue
                    if _FR_MORPH_RE.match(tok):
                        return True, "FR_LEAK"
        else:
            # Zero Vietnamese diacritics: flag English outputs when translating from non-English, or non-VI foreign text
            has_foreign_unacc = any(
                len(tok) >= 3
                and not tok.isupper()
                and tok.lower() not in _ALLOWED_LOANWORDS_VI
                and (
                    not _VI_UNACCENTED_SYLLABLE_RE.match(tok)
                    or bool(re.search(r"[àáâãäåæçèéêëìíîïðñòóôõöøùúûüýþÿ]", tok, re.IGNORECASE))
                )
                for tok in tr_tokens
            )
            if src_l not in ("", "auto", "en", "vi") and has_foreign_unacc:
                translated_away_from_src = any(
                    len(tok) >= 3
                    and not tok.isupper()
                    and tok.lower() not in src_words_lo
                    and tok.lower() not in _ALLOWED_LOANWORDS_VI
                    and not _VI_UNACCENTED_SYLLABLE_RE.match(tok)
                    for tok in tr_tokens
                )
                if translated_away_from_src:
                    return True, "WRONG_TARGET_LANG"
            # Normalized unchanged source (e.g. NBSP stripped '\xa0' -> ' ', or common French nouns)
            src_norm_ws = re.sub(r"\s+", " ", re.sub(r"\[g:([^|\]]*)\|[^\]]*\]", lambda m: m.group(1) or "", src_clean)).strip()
            tr_norm_ws = re.sub(r"\s+", " ", tr_clean).strip()
            is_titlecase_person_name = 2 <= len(tr_tokens) <= 3 and all(t[0].isupper() for t in tr_tokens)
            if src_norm_ws and tr_norm_ws.lower() == src_norm_ws.lower() and has_foreign_unacc and not is_titlecase_person_name:
                if any(tok.islower() for tok in tr_tokens) or any(_FR_MORPH_RE.match(tok) for tok in tr_tokens) or len(tr_tokens) >= 3:
                    return True, "UNTRANSLATED_SOURCE"
        if len(tr_clean.split()) >= 5:
            fr_stops = sum(1 for tok in re.findall(r"[A-Za-zÀ-ỹ]+", tr_clean.lower()) if tok in _FR_STOPWORDS)
            if fr_stops >= 3:
                return True, "FR_STOPWORD_LEAK"
        if (
            len(tr_clean) >= 10
            and not _VI_ACCENT_CHAR_RE.search(tr_clean)
            and re.search(r"\b(?:KHONG|CHUA|DUOC|NGUOI|DUNG\s+CAM|THE\s+TIN|TRO\s+CHOI|HOAN\s+THANH|THAT\s+SU)\b", tr_clean)
        ):
            return True, "UNACCENTED_VI_UPPER"
        if _VI_ACCENT_CHAR_RE.search(tr_clean) and re.search(
            r"(?:^|\n|\\n)\s*(?:The|We|Get|They|You|Your|Without|Because|However|Although)\s+[a-zàáạảãăằắặẳẵâầấậẩẫèéẹẻẽêềếệểễìíịỉĩòóọỏõôồốộổỗơờớợởỡùúụủũưừứựửữỳýỵỷỹđ]",
            tr_clean,
        ):
            return True, "STRAY_EN_GLUE"
        if re.search(r"[àáạảãăằắặẳẵâầấậẩẫèéẹẻẽêềếệểễìíịỉĩòóọỏõôồốộổỗơờớợởỡùúụủũưừứựửữỳýỵỷỹđ][A-Z]", tr_clean):
            return True, "GIBBERISH"
        src_words = {w.lower() for w in _WORD_VI_RE.findall(src_clean)}
        src_raw_tokens = set(_WORD_VI_RE.findall(src_clean))
        garble_hits = 0
        for tok in _WORD_VI_RE.findall(tr_clean):
            tl = tok.lower()
            if tok in src_raw_tokens:
                continue
            if _WEIRD_CASE_RE.match(tok) and not re.match(r"^[A-Z]{2,}s$", tok):
                garble_hits += 2
                continue
            if tl in src_words or tok.isupper():
                continue
            if _CONSONANT_GARBLE_RE.match(tl):
                garble_hits += 1
            elif _BAD_VI_LETTER_ACCENT_RE.match(tl) and _VI_ACCENT_CHAR_RE.search(tl):
                garble_hits += 1
        if garble_hits >= 2 or (garble_hits == 1 and len(tr_clean.split()) <= 4):
            return True, "GIBBERISH"

    return False, "OK"
