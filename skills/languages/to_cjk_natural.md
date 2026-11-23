---
id: to_cjk_natural
name: "Dịch ➔ Nhật / Trung / Hàn"
layer: languages
icon: "🌏"
description: "Chuyển ngữ cô đọng sang tiếng Nhật, Trung Phồn/Giản hoặc Hàn chuẩn ngữ khí bản địa."
source_langs:
  - "*"
target_langs:
  - ja
  - zh
  - zh-TW
  - ko
is_builtin: true
---
### Quy tắc
- Output exclusively in {target_lang_name} with compact phrasing fit for game UI and natural dialogue register (desu/masu, casual, or polite).
- Match punctuation to {target_lang_name} conventions and keep every __TAG_x__ and __NUM_x__ token untouched.

### Ví dụ mẫu
1|Start Game -> 1|ゲーム開始
