"""Dynamic Multi-Language System Prompts powered by 2-Layer Composable Skill Hub."""

from __future__ import annotations

from typing import Any, Dict, List, Optional

from core.skill_manager import SkillManager

QWEN35_SUPPORTED_LANGS: List[Dict[str, str]] = [
    {"code": "vi", "label": "Tiếng Việt (Vietnamese)"},
    {"code": "en", "label": "English"},
    {"code": "zh", "label": "简体中文 (Simplified Chinese)"},
    {"code": "zh-TW", "label": "繁體中文 (Traditional Chinese)"},
    {"code": "ja", "label": "日本語 (Japanese)"},
    {"code": "ko", "label": "한국어 (Korean)"},
    {"code": "fr", "label": "Français (French)"},
    {"code": "es", "label": "Español (Spanish)"},
    {"code": "de", "label": "Deutsch (German)"},
    {"code": "pt", "label": "Português (Portuguese)"},
    {"code": "pt-BR", "label": "Português do Brasil"},
    {"code": "it", "label": "Italiano (Italian)"},
    {"code": "ru", "label": "Русский (Russian)"},
    {"code": "th", "label": "ไทย (Thai)"},
    {"code": "id", "label": "Bahasa Indonesia"},
    {"code": "tr", "label": "Türkçe (Turkish)"},
    {"code": "ar", "label": "العربية (Arabic)"},
    {"code": "nl", "label": "Nederlands (Dutch)"},
]

QWEN35_SUPPORTED_CODES = frozenset(item["code"] for item in QWEN35_SUPPORTED_LANGS)

_LANG_ALIAS_TO_CANONICAL: Dict[str, str] = {
    "vi": "vi", "vi-vn": "vi", "vietnamese": "vi",
    "en": "en", "en-us": "en", "en-gb": "en", "english": "en",
    "zh": "zh", "zh-cn": "zh", "zh-hans": "zh", "zh-sg": "zh", "schinese": "zh", "chinese": "zh", "chs": "zh",
    "zh-tw": "zh-TW", "zh-hk": "zh-TW", "zh-hant": "zh-TW", "tchinese": "zh-TW", "cht": "zh-TW",
    "ja": "ja", "ja-jp": "ja", "jp": "ja", "japanese": "ja",
    "ko": "ko", "ko-kr": "ko", "kr": "ko", "korean": "ko", "koreana": "ko",
    "fr": "fr", "fr-fr": "fr", "french": "fr",
    "es": "es", "es-es": "es", "es-419": "es", "spanish": "es", "latam": "es",
    "de": "de", "de-de": "de", "german": "de",
    "pt": "pt", "pt-pt": "pt", "portuguese": "pt",
    "pt-br": "pt-BR", "ptbr": "pt-BR", "brazilian": "pt-BR",
    "it": "it", "it-it": "it", "italian": "it",
    "ru": "ru", "ru-ru": "ru", "russian": "ru",
    "th": "th", "th-th": "th", "thai": "th",
    "id": "id", "id-id": "id", "indonesian": "id",
    "tr": "tr", "tr-tr": "tr", "turkish": "tr",
    "ar": "ar", "ar-sa": "ar", "arabic": "ar",
    "nl": "nl", "nl-nl": "nl", "dutch": "nl",
}


def normalize_qwen_lang_code(code: str) -> str:
    """Normalize a game slot/BCP-47 code to canonical Qwen3.5:4B code, or '' if unsupported."""
    raw = (code or "").strip()
    if not raw:
        return ""
    key = raw.lower().replace("_", "-")
    if key in _LANG_ALIAS_TO_CANONICAL:
        return _LANG_ALIAS_TO_CANONICAL[key]
    base = key.split("-")[0]
    if base in _LANG_ALIAS_TO_CANONICAL and base != "zh":
        return _LANG_ALIAS_TO_CANONICAL[base]
    return ""


def is_qwen_supported_lang(code: str) -> bool:
    return bool(normalize_qwen_lang_code(code))

TONE_PRESET_INFO: Dict[str, Dict[str, str]] = {
    "natural": {
        "label": "Tự nhiên",
        "hint": "Tự nhiên, chuẩn ngữ cảnh game gốc; cân bằng giữa trang trọng và đời thường.",
        "pronouns": "Tôi / Cậu / Bạn (linh hoạt theo vai vế nhân vật)",
    },
    "wuxia": {
        "label": "Cổ trang / Kiếm hiệp",
        "hint": "Văn phong cổ phong Hán-Việt trang trọng, kiếm hiệp/tiên hiệp, khí chất giang hồ.",
        "pronouns": "Tại hạ / Các hạ, Ta / Ngươi, Sư huynh / Tiền bối",
    },
    "casual": {
        "label": "Đời thường / Hài hước",
        "hint": "Trẻ trung, khẩu ngữ tự nhiên, dí dỏm vừa phải hợp chất đời thường/hài hước.",
        "pronouns": "Tôi / Ông / Bà / Cậu / Mình",
    },
    "horror": {
        "label": "Kinh dị / U ám",
        "hint": "Căng thẳng, u ám, câu chữ gọn sắc lạnh, giữ không khí sinh tồn / rùng rợn.",
        "pronouns": "Tôi / Anh / Cô / Hắn / Thứ đó",
    },
    "scifi": {
        "label": "Sci-Fi / Kỹ thuật",
        "hint": "Mạch lạc, chuẩn thuật ngữ khoa học viễn tưởng, quân sự/kỹ thuật và thiết bị tương lai.",
        "pronouns": "Tôi / Chỉ huy / Ngài / Hệ thống",
    },
}


def build_fallback_style_card(tone_preset: str = "natural", game_description: str = "") -> str:
    """Build a compact <=4-line (~80-110 tokens) Style Card when Phase 0 has not run or omitted STYLE_CARD."""
    key = (tone_preset or "natural").lower()
    info = TONE_PRESET_INFO.get(key, TONE_PRESET_INFO["natural"])
    desc = " ".join((game_description or "").split())[:160]
    ctx_line = f"Bối cảnh: {desc}" if desc else f"Bối cảnh: Game phong cách {info['label']}."
    return "\n".join(
        [
            ctx_line,
            f"Giọng điệu: {info['hint']}",
            f"Xưng hô mặc định: {info['pronouns']}",
            "Lưu ý thuật ngữ: Giữ tên riêng/kỹ năng gốc, từ game quen thuộc lồng tự nhiên.",
        ]
    )

LANGUAGE_NAMES: Dict[str, str] = {
    "en": "English",
    "vi": "Vietnamese (Tiếng Việt)",
    "fr": "French (Français)",
    "es": "Spanish (Español)",
    "de": "German (Deutsch)",
    "pl": "Polish (Polski)",
    "pt": "Portuguese (Português)",
    "pt-BR": "Brazilian Portuguese (Português do Brasil)",
    "it": "Italian (Italiano)",
    "ja": "Japanese (日本語)",
    "ko": "Korean (한국어)",
    "ru": "Russian (Русский)",
    "zh": "Simplified Chinese (简体中文)",
    "zh-TW": "Traditional Chinese (繁體中文)",
    "th": "Thai (ไทย)",
    "id": "Indonesian (Bahasa Indonesia)",
    "tr": "Turkish (Türkçe)",
    "ar": "Arabic (العربية)",
    "nl": "Dutch (Nederlands)",
}


def get_lang_display_name(code: str) -> str:
    if not code:
        return "English"
    canon = normalize_qwen_lang_code(code) or code
    return LANGUAGE_NAMES.get(canon, code.upper())


def _vi_style_rules(target_lang: str, is_ui: bool = False, source_lang: str = "en") -> str:
    if (target_lang or "").lower() != "vi":
        return f"Translate naturally and idiomatically into {get_lang_display_name(target_lang)}."
    is_en_source = normalize_qwen_lang_code(source_lang) == "en"
    if is_en_source:
        loanword_rule = (
            "CHỈ được giữ nguyên một ít TỪ ĐƠN tiếng Anh thông dụng và đơn giản (1 từ đơn như Video, Camera, Server, Lobby, Host, Micro, Ping, Game, Boss, Selfie, FPS, VSync) khi ghép câu tự nhiên hơn; "
            "TUYỆT ĐỐI KHÔNG để nguyên từ ghép / cụm từ hay vật phẩm thông thường bằng tiếng Anh (phải dịch hết thành tiếng Việt)."
        )
    else:
        loanword_rule = (
            f"Nguồn đang là {get_lang_display_name(source_lang)} (KHÔNG phải tiếng Anh): "
            "PHẢI dịch 100% sang tiếng Việt, TUYỆT ĐỐI KHÔNG giữ nguyên bất kỳ từ nào của ngôn ngữ gốc (trừ thẻ __TAG_x__ và tên riêng nhân vật)."
        )
    if is_ui:
        return (
            "Quy tắc Việt hóa UI kiểu Cánh Cụt Team: ngắn gọn, súc tích, chuẩn thuật ngữ game "
            "(Settings -> Cài đặt, Options -> Tùy chọn, Host / Host Game -> Tạo phòng / Chủ phòng [KHÔNG dịch là Truyền tải], "
            "Hosting Game -> Đang tạo phòng, Inventory -> Túi đồ / Kho đồ [KHÔNG dịch Hồi tài sản], "
            "Matchmaking -> Ghép trận [KHÔNG dịch Tìm kép], Sit -> Ngồi, Sleep -> Đi ngủ, Submerge -> Lặn xuống, "
            "Hat -> Mũ, Save -> Lưu / Ô lưu, Back -> Quay lại, Quit -> Thoát, Credits -> Đội ngũ, "
            "Private -> Riêng tư, Join Random -> Tham gia ngẫu nhiên, Wake Up -> Thức dậy, "
            "Controls -> Điều khiển, Invert Look -> Đảo ngược góc nhìn, Master Volume -> Âm lượng tổng, "
            "Claim -> Nhận, Close -> Đóng, Interact -> Tương tác, Offline -> Ngoại tuyến). "
            f"{loanword_rule} "
            "Giữ khoảng trắng chuẩn giữa các từ và quanh thẻ biến, không ghép dính chữ, không thêm dấu câu thừa."
        )
    return (
        "Quy tắc Việt hóa hội thoại kiểu Cánh Cụt Team: tự nhiên như người bản xứ, giữ đúng sắc thái nhân vật "
        "và đồng nhất ngôi xưng Tiếng Việt (tôi/cậu, anh/em, ta/ngươi) theo Từ điển. "
        f"{loanword_rule} "
        "Luôn giữ nguyên khoảng trắng chuẩn và giữ đúng thứ tự 100% số ID từng dòng."
    )


GLOSSARY_PROMPT = """{skill_prefix}Bạn là Trưởng nhóm Biên dịch Game chuyên nghiệp.
{context_block}
Phân tích tối đa 35 câu mẫu từ game ({source_lang_name} -> {target_lang_name}) để lập BẢNG THUẬT NGỮ & NÉN STYLE CARD.

Yêu cầu bắt buộc:
1. Trích xuất tối đa 25 tên riêng nhân vật, địa danh, vật phẩm đặc trưng theo định dạng:
TERM|SOURCE_TEXT|TARGET_TEXT|NOTE
- QUAN TRỌNG: Với mọi TÊN NHÂN VẬT / TÊN NGƯỜI / NPC / SPEAKER, TARGET_TEXT BẮT BUỘC GIỮ NGUYÊN 100% Y HỆT SOURCE_TEXT (TUYỆT ĐỐI KHÔNG dịch nghĩa, KHÔNG phiên âm tiếng Việt — VD: TERM|Arthur Lafleur|Arthur Lafleur|Tên nhân vật — giữ nguyên).
2. Nén bối cảnh & giọng văn thành ĐÚNG 4 DÒNG ngắn gọn (tổng dưới 100 tokens) nằm trong thẻ sau:
STYLE_START
Bối cảnh: <tóm tắt thể loại & thế giới game trong 1 câu ngắn>
Giọng điệu: <phong cách dịch bám sát Bộ Văn Phong & mô tả>
Xưng hô mặc định: <cặp ngôi xưng chính>
Lưu ý thuật ngữ: <quy tắc giữ nguyên 100% tên nhân vật & dịch tự nhiên vật phẩm/hội thoại>
STYLE_END
3. Không giải thích lan man ngoài 2 phần trên."""

UI_HUD_PROMPT = """{skill_prefix}{style_card_prefix}You are a Senior Game Localization Engineer translating short game UI/HUD labels from {source_lang_name} into {target_lang_name}.
{extra_rules}
{proper_noun_rule}
{glossary_block}

STRICT OUTPUT PROTOCOL:
1. Input lines follow `ID|Text`. Output MUST match every `ID|TranslatedText` line 1-to-1 (NEVER shift, merge, or skip IDs).
2. PRESERVE EVERY `__TAG_0__`, `__NUM_0__`, ..., and `__NL__` token verbatim in natural positions. NEVER rename, drop, fuse `__NL__`, or invent `__`.
3. NEVER translate character/person names or speaker labels. Keep UI concise; no markdown, quotes added, or explanations."""

STORY_PROMPT = """{skill_prefix}{style_card_prefix}You are a Literary Game Translator translating narrative and dialogue from {source_lang_name} into {target_lang_name}.
{extra_rules}
{proper_noun_rule}
{glossary_block}

STRICT OUTPUT PROTOCOL:
1. Input lines follow `ID|Text`. Output MUST match every `ID|TranslatedText` line one-for-one (NEVER shift, merge, or skip an ID).
2. PRESERVE EVERY `__TAG_0__`, `__NUM_0__`, ..., and `__NL__` token verbatim. NEVER invent `__` artifacts.
3. Keep tone, emotion, and speaker pronouns consistent across the batch.
4. No markdown, no explanations."""

SUB_ONLY_PROMPT = """{skill_prefix}{style_card_prefix}You are a Subtitle & Cutscene Dialogue Translator translating spoken lines and choices from {source_lang_name} into {target_lang_name}.
{extra_rules}
{proper_noun_rule}
{glossary_block}

STRICT OUTPUT PROTOCOL:
1. Input lines follow `ID|Text`. Output MUST match every `ID|TranslatedText` line cleanly.
2. PRESERVE EVERY `__TAG_0__`, `__NUM_0__`, ..., and `__NL__` token verbatim.
3. Concise subtitle pacing suitable for in-game reading speed. No markdown or commentary."""

_skill_mgr_singleton: Optional[SkillManager] = None


def _get_skill_mgr(skill_manager: Optional[SkillManager] = None) -> SkillManager:
    global _skill_mgr_singleton
    if skill_manager is not None:
        return skill_manager
    if _skill_mgr_singleton is None:
        _skill_mgr_singleton = SkillManager()
    return _skill_mgr_singleton


def build_glossary_system_prompt(
    source_lang: str,
    target_lang: str,
    game_description: str = "",
    tone_preset: str = "natural",
    style_skill_id: Optional[str] = None,
    lang_skill_override: str = "auto",
    skill_manager: Optional[SkillManager] = None,
) -> str:
    effective_style = style_skill_id or tone_preset or "natural_standard"
    resolved = _get_skill_mgr(skill_manager).resolve_active_skills(
        source_lang=source_lang,
        target_lang=target_lang,
        style_skill_id=effective_style,
        lang_skill_override=lang_skill_override,
        source_lang_name=get_lang_display_name(source_lang),
        target_lang_name=get_lang_display_name(target_lang),
    )
    style_obj = resolved.get("style_skill")
    style_label = style_obj["name"] if style_obj else (TONE_PRESET_INFO.get((tone_preset or "natural").lower(), TONE_PRESET_INFO["natural"])["label"])
    desc = (game_description or "").strip()[:900]
    ctx_parts = [f"Bộ Văn Phong đang dùng: {style_label}"]
    if desc:
        ctx_parts.append(f"Mô tả bối cảnh & ghi chú của người dùng:\n{desc}")
    context_block = "\n".join(ctx_parts)
    skill_prefix = f"{resolved['combined_prefix']}\n\n" if resolved.get("combined_prefix") else ""
    return GLOSSARY_PROMPT.format(
        skill_prefix=skill_prefix,
        context_block=context_block,
        source_lang_name=get_lang_display_name(source_lang),
        target_lang_name=get_lang_display_name(target_lang),
    )


DEEP_THINK_GUIDE = """

DEEP-THINK REASONING PROTOCOL (keep internal <think> concise, under 120 words):
- Read all lines in this batch as one coherent scene to lock speaker relationships and consistent pronouns.
- NEVER translate, localize, or phoneticize character/person names or speaker labels.
- Adapt idioms, humor, cultural nuance, and emotional subtext naturally instead of translating word-for-word.
- Verify every __TAG_x__, __NUM_x__, and __NL__ before outputting ONLY clean `ID|TranslatedText` lines."""


def build_translation_system_prompt(
    mode: str,  # "UI_HUD" | "STORY" | "SUB_ONLY"
    source_lang: str,
    target_lang: str,
    glossary_terms: list[dict],
    keep_proper_nouns: bool = True,
    compiled_style_card: str = "",
    tone_preset: str = "natural",
    style_skill_id: Optional[str] = None,
    lang_skill_override: str = "auto",
    is_short_ui: bool = False,
    skill_manager: Optional[SkillManager] = None,
    deep_think: bool = False,
) -> str:
    is_ui = mode == "UI_HUD" or is_short_ui
    effective_style = style_skill_id or tone_preset or "natural_standard"
    resolved = _get_skill_mgr(skill_manager).resolve_active_skills(
        source_lang=source_lang,
        target_lang=target_lang,
        style_skill_id=effective_style,
        lang_skill_override=lang_skill_override,
        source_lang_name=get_lang_display_name(source_lang),
        target_lang_name=get_lang_display_name(target_lang),
    )

    # FIXED KV-CACHE PREFIX AT THE VERY TOP: [Skill Hub (Layer 1 + Layer 2)] + [Style Card]
    skill_prefix = f"{resolved['combined_prefix']}\n\n" if resolved.get("combined_prefix") else ""
    card_txt = "" if is_short_ui else (compiled_style_card or "").strip()
    if not card_txt and not is_short_ui and (tone_preset or "natural").lower() != "natural":
        card_txt = build_fallback_style_card(tone_preset)
    style_card_prefix = f"[GAME STYLE CARD - FIXED PREFIX]\n{card_txt}\n\n" if card_txt else ""

    extra_rules = (
        ""
        if resolved.get("combined_prefix")
        else _vi_style_rules(target_lang, is_ui=is_ui, source_lang=source_lang)
    )

    if glossary_terms:
        lines = [f"- {t['source_term']} => {t['target_term']}" for t in glossary_terms[:18]]
        glossary_block = "Glossary (MUST follow strictly):\n" + "\n".join(lines)
    else:
        glossary_block = ""

    if keep_proper_nouns:
        proper_noun_rule = (
            f"CHARACTER NAME PROTECTION (STRICT): NEVER translate, localize, or phoneticize any character/person names, surnames, nicknames, or speaker prefixes from {get_lang_display_name(source_lang)} "
            "(even when a name looks like an ordinary word, e.g. Pierre, Claire, Rose, Grace, Hope, Noël, Moreau, Blackwood, Arthur Lafleur — keep exact original spelling). "
            "ALWAYS translate generic items, tools, furniture, containers, actions, and common descriptions into target language."
        )
    else:
        proper_noun_rule = "Keep personal character names intact; translate descriptive item or place names naturally where appropriate."

    src_name = get_lang_display_name(source_lang)
    tgt_name = get_lang_display_name(target_lang)

    if is_ui:
        return UI_HUD_PROMPT.format(
            skill_prefix=skill_prefix,
            style_card_prefix=style_card_prefix,
            source_lang_name=src_name,
            target_lang_name=tgt_name,
            extra_rules=extra_rules,
            proper_noun_rule=proper_noun_rule,
            glossary_block=glossary_block,
        )
    if mode == "SUB_ONLY":
        base = SUB_ONLY_PROMPT.format(
            skill_prefix=skill_prefix,
            style_card_prefix=style_card_prefix,
            source_lang_name=src_name,
            target_lang_name=tgt_name,
            extra_rules=extra_rules,
            proper_noun_rule=proper_noun_rule,
            glossary_block=glossary_block,
        )
        return base + (DEEP_THINK_GUIDE if deep_think else "")
    base = STORY_PROMPT.format(
        skill_prefix=skill_prefix,
        style_card_prefix=style_card_prefix,
        source_lang_name=src_name,
        target_lang_name=tgt_name,
        extra_rules=extra_rules,
        proper_noun_rule=proper_noun_rule,
        glossary_block=glossary_block,
    )
    return base + (DEEP_THINK_GUIDE if deep_think else "")
