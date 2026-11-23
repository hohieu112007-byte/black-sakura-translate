# Black Sakura Translate

[![Tải App Windows (Latest Release)](https://img.shields.io/badge/TẢI_APP_NGAY-Release_v1.0.1_(Windows)-ff5fa8?style=for-the-badge&logo=github)](https://github.com/hohieu112007-byte/black-sakura-translate/releases/latest)
[![License: MIT](https://img.shields.io/badge/License-MIT-blue.svg?style=for-the-badge)](LICENSE)

**Black Sakura Translate** là ứng dụng Việt hóa / đa ngôn ngữ hóa game PC **hoàn toàn miễn phí, chạy 100% AI cục bộ (Local AI qua Ollama)** với giao diện phong cách **Steam Desktop** hiện đại. Không cần API trả phí, không gửi dữ liệu ra máy chủ ngoài, bảo vệ file game gốc bằng hệ thống an toàn 6 lớp.

> **👉 Tải nhanh cho người dùng (không cần biết code):** Bấm nút **[TẢI APP NGAY (Release v1.0.1)](https://github.com/hohieu112007-byte/black-sakura-translate/releases/latest)** hoặc nhìn cột **Releases** bên phải trang GitHub -> tải **`Black-Sakura-Translate-v1.0.1-Windows-Standalone.zip`**, giải nén và nháy đúp **`BlackSakuraTranslate.exe`**!

---

## Ảnh chụp giao diện thực tế

### 1. Màn hình Điều khiển Game & Dịch nhanh (Theme *Obsidian Đêm*)
![Màn hình chính Black Sakura Translate](docs/screenshots/01-overview-obsidian.png)

### 2. Bảng Dịch Trực Tiếp, Thay thế Hàng loạt & Từ điển Thuật ngữ
![Bảng Dịch Trực Tiếp & Từ điển](docs/screenshots/02-live-editor.png)

<details>
<summary><b>📸 Xem thêm ảnh: Giao diện Tiếng Anh (VI / EN), Thư viện Steam & Kho Skill 2 Lớp</b></summary>

#### Chế độ Tiếng Anh (nút chuyển nhanh `VI | EN` trên thanh trên cùng)
![Giao diện Tiếng Anh Black Sakura Translate](docs/screenshots/05-english-obsidian.png)

#### Thư viện Game phong cách Steam Poster 2:3
![Thư viện Game Steam-style](docs/screenshots/04-library-obsidian.png)

#### Kho Skill 2 Lớp & Quản lý Bộ công cụ GitHub (Theme *Sakuga Indigo*)
![Kho Skill 2 Lớp](docs/screenshots/03-skill-hub-indigo.png)

</details>

---

## Tính năng nổi bật

- **100% AI Local (Ollama) & Tự tối ưu phần cứng:** Tự chỉnh luồng/VRAM theo GPU; hỗ trợ **Dual-Model** (model nhẹ quét UI, model chính dịch thoại) và **Quiet Mode** (giảm nóng laptop & bảo vệ tai nghe USB).
- **Hỗ trợ đa Engine:** Unity (Mono/IL2CPP, Addressables `catalog.bin/.json`, tự đổi nhãn *Tiếng Việt*), Unreal Engine (`.locres`, `.pak`), Ren'Py (`.rpy`, `.rpa`), JSON, CSV, PO, YAML, XML, TXT.
- **Kho Skill 2 Lớp & Suy luận Pha 0:** 17 Skill dựng sẵn (Cầu ngôn ngữ + Văn phong), tự lập Từ điển Thuật ngữ, chống trùng lặp + Global TM 0ms.
- **Live Editor & Ghi nóng:** Tìm kiếm toàn DB, lọc theo loại câu, thay thế hàng loạt bảo vệ thẻ code và ghi thẳng vào game trong vài giây.
- **Chia sẻ `.sakugapack` & Delta Update:** Xuất/nhập bản dịch 1-Click; tự giữ bản dịch cũ và chỉ dịch câu mới khi Steam cập nhật game.
- **An toàn 6 Lớp & Giao diện song ngữ `VI / EN`:** Chặn game online có Anti-Cheat, sao lưu gốc SHA-256 khôi phục 1-Click, Tag Firewall, vá Font Unicode, chạy ngầm khay hệ thống.

---

## App làm được gì trong tác vụ hàng ngày?

- **Quản lý thư viện game gọn như Steam:** Kéo thả thư mục game, file `.exe`, shortcut `.url/.lnk` hoặc bấm **Quét Steam Library** để tự nạp danh sách game kèm ảnh bìa dọc 2:3 và nút **CHƠI NGAY**.
- **Dịch mới & Chạy ngầm không lo gián đoạn:** Chọn 1 trong 3 mức độ (*Siêu Tốc* / *Cân Bằng* / *Cốt Truyện Sâu*), thu gọn xuống khay hệ thống để máy tự dịch; nếu tạm dừng hoặc tắt đột ngột, mọi câu đã dịch đã lưu trong SQLite để chạy tiếp ngay đoạn dở.
- **Biên tập & Chuốt bản dịch trực tiếp (Live Editor):** Tìm kiếm toàn bộ câu trong DB, lọc nhanh câu chưa dịch hoặc câu có dấu hiệu lỗi, **Thay thế hàng loạt** (Bulk Replace) bảo vệ thẻ code, khóa thuật ngữ nhân vật vào Từ điển và **Ghi nóng (Hot Apply)** thẳng vào game.
- **Bảo trì bản dịch khi game update & Chia sẻ:** Khi Steam cập nhật game có thêm câu mới, bấm **Delta Update** để giữ nguyên 100% câu cũ đã sửa và chỉ dịch phần mới; xuất/nhập gói `.sakugapack` gửi bạn bè hoặc **Khôi phục bản gốc** trong 1 giây.

---

## App dịch game như thế nào & kết quả ra sao?

Ứng dụng xử lý theo quy trình 5 bước tự động ngay trên máy bạn:

1. **Quét Anti-Cheat & Sao lưu gốc:** Tự chặn các game Online có hệ thống chống gian lận (*EasyAntiCheat, BattlEye, Vanguard...*) để bảo vệ tài khoản; sao lưu file ngôn ngữ gốc kèm mã kiểm tra **SHA-256**.
2. **Nhận diện Engine & Bóc tách chuỗi (< 1 giây):**
   - Hỗ trợ **Unity** (Mono/IL2CPP, StringTable & Addressables `catalog.bin/.json`), **Unreal Engine** (`.locres`, `.pak`), **Ren'Py** (`.rpy`, `.rpa`), **RPG Maker** (`data/` / `www/data/`) và các định dạng text phổ biến (`.json`, `.csv`, `.tsv`, `.po/.pot`, `.xml`, `.yaml/.yml`, `.ini`, `.txt`).
   - Tự động phân loại từng dòng thành **UI / Menu ngắn**, **Cốt truyện / Hội thoại** và bỏ qua biến hệ thống, đường dẫn code.
3. **Dịch theo ngữ cảnh (Pha 0 + Tag Firewall + Global TM):**
   - Câu trùng khớp 100% được lấy ngay từ **Global TM (0ms)**; các mẫu lặp cấu trúc được gộp dịch một lần.
   - **Pha 0** đọc thông tin game để tự tạo *Style Card* & *Từ điển Thuật ngữ* giúp đồng nhất đại từ xưng hô (Tôi/Ta, Anh/Em, Huynh/Đệ...).
   - **Tag Firewall** khóa nguyên vẹn biến lập trình (`{0}`, `%s`, `<color=...>`, `[tag]`, `\n`) tránh văng game.
4. **Chèn theo Slot phụ (Piggyback) & Vá Font:** Ưu tiên ghi đè vào một slot ngôn ngữ phụ ít dùng (như `fr`, `pt-BR`, `es`...) để bạn giữ nguyên Tiếng Anh gốc trong game, tự đổi nhãn hiển thị thành *Tiếng Việt* (Unity) và tự vá font Unicode nếu game thiếu dấu tiếng Việt.
5. **Tự kiểm lỗi QA sau khi dịch:** Tự rà soát các dòng còn sót chữ gốc/CJK, lỗi mã hóa hoặc thiếu tag để bạn bấm **Fix Lỗi** sửa lại chỉ bằng một click.

---

## Khuyến cáo chọn Game & Model AI (Ollama) phù hợp

Ứng dụng tự nhận diện GPU/RAM để đề xuất cấu hình luồng chạy an toàn, đồng thời bạn có thể chọn bất kỳ model Ollama nào tùy theo **độ nặng của game**:

| Loại Game & Khối lượng chữ | Phần cứng gợi ý | Model Ollama khuyên dùng | Chế độ & Mẹo tối ưu |
| :--- | :--- | :--- | :--- |
| **Game nhẹ / Indie / Co-op / Nhiều nhãn UI** *(ít thoại dài, chủ yếu menu, vật phẩm, thông báo)* | CPU-Only hoặc GPU ≤ 4GB–6GB VRAM | **`qwen3:1.7b`**, **`qwen2.5:3b`** hoặc **`qwen3.5:4b`** | Chọn **Cấp 1 (Siêu Tốc)** hoặc **Cấp 2 (Cân Bằng)**; chạy cực nhanh chỉ vài chục giây. |
| **Game tầm trung / Hành động - Phiêu lưu / Mô phỏng** *(vài nghìn câu thoại, quest vừa phải)* | GPU ~8GB VRAM *(RTX 3060/4060...)* | **`qwen3.5:4b`** (nhanh, chuẩn) hoặc **`qwen3:8b`** (văn phong mượt hơn) | Bật **Dual-Model** (`qwen3:1.7b` quét UI + `qwen3:8b` dịch thoại) để vừa nhanh vừa mượt. |
| **Game nặng chữ / RPG / Visual Novel / CRPG cốt truyện dày** *(hàng chục nghìn câu, nhiều ẩn dụ, lore phức tạp)* | GPU ≥ 12GB–16GB+ VRAM *(RTX 3060 12GB, 4070 Ti, 4080, 4090...)* | Model cỡ lớn **12B – 14B+** như **`gemma4:12b`**, **`qwen3:14b`** / **`qwen2.5:14b`** (hoặc 27B+ nếu có ≥ 24GB VRAM) | Dùng **Cấp 3 (Cốt Truyện Sâu)** + **Dual-Model** (`qwen3:1.7b` lo UI ngắn, model **12B/14B+** gánh 100% thoại dài) để giữ mạch văn tự nhiên và xưng hô chuẩn. |

> **⚠️ Những game KHÔNG khuyến cáo / không hỗ trợ dịch trực tiếp:**
> - **Game Online / PvP có Anti-Cheat nghiêm ngặt** (*EasyAntiCheat, BattlEye, Vanguard, Ricochet...*): App chủ động khóa nút dịch để tránh rủi ro khóa tài khoản.
> - **Game đóng gói mã hóa độc quyền** không lộ file ngôn ngữ, hoặc game vẽ chữ trực tiếp lên ảnh texture (không chứa chuỗi văn bản).
> - **Mẹo cho Laptop mỏng nhẹ:** Nếu chạy model 8B–14B khiến máy nóng hoặc rè tai nghe USB, hãy bật **Quiet Mode** trong phần *Cấu hình Model AI* để hạ luồng và cho GPU nghỉ nhịp giữa các batch.

---

## Yêu cầu hệ thống

- **Hệ điều hành:** Windows 10 / 11 (64-bit).
- **Python:** 3.10 trở lên (tải tại [python.org](https://www.python.org/downloads/), nhớ tích chọn *Add Python to PATH*).
- **Ollama:** Đã cài đặt và đang chạy cục bộ (tải tại [ollama.com](https://ollama.com/)).
- **Phần cứng khuyến nghị:** GPU NVIDIA 6GB–8GB+ VRAM (hoặc RAM 16GB+ khi chạy CPU).
  - Model khuyến nghị: `qwen3.5:4b` (model chính) và tùy chọn `qwen3:1.7b` (model phụ cho Dual-Model UI).

---

## Cài đặt & Khởi chạy nhanh

### Cách 1: Tải bản Độc lập `.EXE` (Không cần cài Python — Khuyên dùng!)
1. Mở trang **[Releases mới nhất](https://github.com/hohieu112007-byte/black-sakura-translate/releases/latest)** và tải file **`Black-Sakura-Translate-v1.0.1-Windows-Standalone.zip`**.
2. Giải nén ra bất kỳ thư mục nào và nháy đúp **`BlackSakuraTranslate.exe`**.
3. Cài **[Ollama](https://ollama.com/download)** (nếu máy chưa có) — khi Ollama đã cài, app sẽ **tự bật ngầm máy chủ Ollama** và tự kết nối mà bạn không cần mở Ollama thủ công; nếu chưa có model, bấm **⬇ Tải ngay `qwen3.5:4b`** ngay trong app.

### Cách 2: Chạy bản Portable / Mã nguồn (Nếu máy có Python 3.10+)
1. Tải **`Black-Sakura-Translate-v1.0.1-Source.zip`** tại mục Releases (hoặc `git clone` bên dưới) rồi giải nén.
2. Nháy đúp **`Start_Black_Sakura_Translate.bat`** — script tự cài các thư viện trong `requirements.txt` nếu thiếu và mở ứng dụng.

### Cách 3: Chạy bằng dòng lệnh (CLI cho Developer)
```bash
git clone https://github.com/hohieu112007-byte/black-sakura-translate.git
cd black-sakura-translate
pip install -r requirements.txt
python app.py
```

#### Tùy chọn dòng lệnh nâng cao
- Chạy trên trình duyệt web mặc định thay vì cửa sổ Desktop PyWebView:
  ```bash
  python app.py --web
  ```
- Chọn cổng cục bộ ưu tiên (mặc định `8765`):
  ```bash
  python app.py --port 8765
  ```

> **Lưu ý lần chạy đầu:** Ứng dụng sẽ tự động tạo các thư mục làm việc (`backups/`, `cache/`, `covers/`, `tools/`) và tải các công cụ mã nguồn mở cần thiết (`repak`, `UnrealLocres`, `rpatool.py`, font Unicode `NotoSans` / `BeVietnamPro`) từ GitHub Releases về thư mục `tools/`.

---

## Tài liệu hướng dẫn sử dụng chi tiết

Xem file văn bản tiếng Việt chi tiết đi kèm ngay trong thư mục gốc:
- **[HUONG_DAN_SU_DUNG.txt](HUONG_DAN_SU_DUNG.txt)** — Hướng dẫn từng bước từ cài đặt, thêm game, chọn chế độ dịch, sửa tay, chia sẻ `.sakugapack` đến xử lý lỗi thường gặp.

---

## Cấu trúc dự án

```text
.
├── app.py                          # FastAPI backend, SSE realtime stream & PyWebView launcher
├── requirements.txt                # Danh sách thư viện Python
├── Start_Black_Sakura_Translate.bat# Script khởi chạy nhanh trên Windows
├── HUONG_DAN_SU_DUNG.txt           # File văn bản hướng dẫn sử dụng chi tiết (Tiếng Việt)
├── core/                           # AI Engine (Ollama), Hardware Profiler, SQLite DB, Skill Manager, Tray
├── extractors/                     # Bộ trích xuất & đóng gói Unity, Unreal (.locres/.pak), Ren'Py, JSON/CSV/PO/YAML
├── safety_shield/                  # 6 lớp bảo vệ: Anti-Cheat, Backup SHA-256, Tag Firewall, QA Guard, Font Patcher
├── skills/                         # 17 Skill dựng sẵn (languages/ + styles/)
└── frontend/                       # Giao diện Steam-Style (5 theme, Live Editor, Skill Hub, Dual-Model UI)
```

---

## Miễn trừ trách nhiệm

Công cụ này phục vụ mục đích học tập, nghiên cứu và cá nhân hóa ngôn ngữ chơi game offline/single-player. Không sử dụng để can thiệp file của các trò chơi trực tuyến có hệ thống chống gian lận (Anti-Cheat). Người dùng tự chịu trách nhiệm tuân thủ điều khoản sử dụng của từng trò chơi.

## Giấy phép

Phát hành theo giấy phép **MIT License** — xem chi tiết tại file [LICENSE](LICENSE).
