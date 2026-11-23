"""Smart Language Slot Recommender ('★ ĐỀ XUẤT' ranking for Piggybacking & Direct Slots)."""

from __future__ import annotations

from typing import Any, Dict, List

VI_PRIORITY_SPECS = {
    "vi": (0, True, "★ ĐỀ XUẤT", "★ ĐỀ XUẤT • Slot Tiếng Việt gốc", "Settings game ➔ Language ➔ Tiếng Việt"),
    "pl": (1, True, "★ ĐỀ XUẤT", "★ ĐỀ XUẤT • Font dấu chuẩn nhất, giữ nguyên English gốc", "Settings game ➔ Language ➔ Polski (hiển thị Tiếng Việt)"),
    "fr": (2, True, "★ ĐỀ XUẤT", "★ ĐỀ XUẤT • Sẵn dấu mũ, cực kỳ ổn định, giữ English gốc", "Settings game ➔ Language ➔ Français (hiển thị Tiếng Việt)"),
    "pt": (3, True, "★ ĐỀ XUẤT", "★ ĐỀ XUẤT • Sẵn dấu mũ & dấu ngã, giữ English gốc", "Settings game ➔ Language ➔ Português (hiển thị Tiếng Việt)"),
    "pt-BR": (3, True, "★ ĐỀ XUẤT", "★ ĐỀ XUẤT • Sẵn dấu mũ & dấu ngã, giữ English gốc", "Settings game ➔ Language ➔ Português (Brasil) (hiển thị Tiếng Việt)"),
    "es": (4, True, "★ ĐỀ XUẤT", "★ ĐỀ XUẤT • Hỗ trợ dấu tốt, giữ English gốc", "Settings game ➔ Language ➔ Español (hiển thị Tiếng Việt)"),
    "de": (5, True, "★ ĐỀ XUẤT", "★ ĐỀ XUẤT • Slot phụ hệ Latinh an toàn", "Settings game ➔ Language ➔ Deutsch (hiển thị Tiếng Việt)"),
}

CJK_CYRILLIC = {"ja", "zh", "zh-TW", "ko", "ru", "uk"}
LATIN_SLOTS = {"en", "fr", "pl", "pt", "pt-BR", "es", "de", "it", "nl", "sv", "tr", "vi"}

HUMAN_LANG_NAME = {
    "en": "English",
    "vi": "Tiếng Việt",
    "fr": "Français",
    "pl": "Polski",
    "pt": "Português",
    "pt-BR": "Português (BR)",
    "es": "Español",
    "de": "Deutsch",
    "it": "Italiano",
    "ja": "日本語 (Japanese)",
    "ko": "한국어 (Korean)",
    "ru": "Русский (Russian)",
    "zh": "简体中文 (Chinese)",
    "zh-TW": "繁體中文",
    "tr": "Türkçe",
    "nl": "Nederlands",
    "sv": "Svenska",
    "uk": "Українська",
}


def rank_inject_slots(
    detected_slots: List[Dict[str, Any]],
    source_lang: str,
    target_lang: str,
) -> List[Dict[str, Any]]:
    """Rank detected game slots with '★ ĐỀ XUẤT' recommendations and Settings instructions."""
    tgt = (target_lang or "vi").strip()
    src = (source_lang or "en").strip()
    tgt_low = tgt.lower()
    src_low = src.lower()
    tgt_is_vi = tgt_low == "vi"
    tgt_is_fr = tgt_low == "fr" or tgt_low.startswith("fr-")
    tgt_is_latin = tgt in LATIN_SLOTS or tgt_low.split("-")[0] in ("fr", "es", "de", "pt", "pl", "it", "vi", "en")
    ranked: List[Dict[str, Any]] = []

    for slot in detected_slots:
        sid = slot["slot_id"]
        sid_low = sid.lower()
        is_source_slot = sid_low == src_low
        label = slot.get("label") or HUMAN_LANG_NAME.get(sid, sid.upper())
        entry = dict(slot)
        entry["display_name"] = label

        if tgt_is_vi:
            if sid_low == "vi":
                prio, rec, badge, note, howto = VI_PRIORITY_SPECS["vi"]
            elif is_source_slot:
                prio = 20
                rec = False
                badge = "GHI ĐÈ GỐC"
                note = f"Ghi đè trực tiếp lên ngôn ngữ nguồn ({label}) — không phải slot dự phòng"
                howto = f"Vào game dùng ngay ngôn ngữ mặc định ({label})"
            elif sid in VI_PRIORITY_SPECS:
                prio, rec, badge, note, howto = VI_PRIORITY_SPECS[sid]
            elif sid_low in ("ptbr", "pt-br"):
                _, rec, badge, note, howto = VI_PRIORITY_SPECS["pt-BR"]
                prio = 3
            elif sid in CJK_CYRILLIC:
                prio = 50
                rec = False
                badge = "KHÁC BẢNG MÃ"
                note = "Ít tương thích dấu Tiếng Việt (dễ lệch font)"
                howto = f"Settings game ➔ Language ➔ {label}"
            else:
                prio = 30
                rec = False
                badge = "SLOT PHỤ"
                note = "Slot ngôn ngữ phụ (chèn thay thế ngôn ngữ này)"
                howto = f"Settings game ➔ Language ➔ {label}"
        else:
            # Multi-language target (e.g. en, fr, ja, zh, ko, es, de, ru)
            if sid_low == tgt_low and not is_source_slot:
                prio = 0
                rec = True
                badge = "★ ĐỀ XUẤT"
                note = f"★ ĐỀ XUẤT • Khớp chính xác ngôn ngữ {HUMAN_LANG_NAME.get(tgt, tgt)}"
                howto = f"Settings game ➔ Language ➔ {label}"
            elif tgt_is_fr and not is_source_slot and sid_low in ("fr", "es", "pt", "pt-br", "ptbr", "pl", "de", "it"):
                prio = 1 if sid_low == "fr" else 2
                rec = True
                badge = "★ ĐỀ XUẤT"
                note = f"★ ĐỀ XUẤT • Slot Latinh có dấu chuẩn cho Tiếng Pháp, giữ nguyên {src.upper()}"
                howto = f"Settings game ➔ Language ➔ {label}"
            elif tgt in CJK_CYRILLIC and sid == tgt.split("-")[0] and not is_source_slot:
                prio = 1
                rec = True
                badge = "★ ĐỀ XUẤT"
                note = "★ ĐỀ XUẤT • Cùng hệ chữ cái tương thích tốt"
                howto = f"Settings game ➔ Language ➔ {label}"
            elif tgt_is_latin and sid_low in ("fr", "pl", "pt", "pt-br", "ptbr", "es", "de") and not is_source_slot:
                prio = 2
                rec = True
                badge = "★ ĐỀ XUẤT"
                note = f"★ ĐỀ XUẤT • Slot Latinh dự phòng an toàn, giữ nguyên {src.upper()}"
                howto = f"Settings game ➔ Language ➔ {label}"
            elif is_source_slot:
                prio = 20
                rec = False
                badge = "GHI ĐÈ GỐC"
                note = f"Ghi đè trực tiếp lên ngôn ngữ nguồn ({label})"
                howto = f"Vào game dùng ngay ngôn ngữ mặc định ({label})"
            else:
                prio = 35
                rec = False
                badge = "SLOT PHỤ"
                note = f"Ghi đè thay thế slot {label}"
                howto = f"Settings game ➔ Language ➔ {label}"

        entry["priority"] = prio
        entry["recommended"] = rec
        entry["badge"] = badge
        entry["note"] = note
        entry["settings_guide"] = howto
        ranked.append(entry)

    ranked.sort(key=lambda item: (item["priority"], -item.get("count", 0), item["slot_id"]))
    return ranked
