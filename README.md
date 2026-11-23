# Black Sakura Translate

[![Tải App Windows (Latest Release)](https://img.shields.io/badge/TẢI_APP_NGAY-Release_v1.0.0_(Windows)-ff5fa8?style=for-the-badge&logo=github)](https://github.com/hohieu112007-byte/black-sakura-translate/releases/latest)
[![License: MIT](https://img.shields.io/badge/License-MIT-blue.svg?style=for-the-badge)](LICENSE)

**Black Sakura Translate** là ứng dụng Việt hóa / đa ngôn ngữ hóa game PC **hoàn toàn miễn phí, chạy 100% AI cục bộ (Local AI qua Ollama)** với giao diện phong cách **Steam Desktop** hiện đại. Không cần API trả phí, không gửi dữ liệu ra máy chủ ngoài, bảo vệ file game gốc bằng hệ thống an toàn 6 lớp.

> **👉 Tải nhanh cho người dùng (không cần biết code):** Bấm nút **[TẢI APP NGAY (Release v1.0.0)](https://github.com/hohieu112007-byte/black-sakura-translate/releases/latest)** hoặc nhìn cột **Releases** bên phải trang GitHub -> tải file `.zip`, giải nén và chạy luôn!

---

## Tính năng nổi bật

- **100% AI Local (Ollama) & Tự động cân chỉnh phần cứng:** Tự nhận diện GPU/VRAM và RAM để tự tinh chỉnh số luồng song song, `num_ctx`, KV-Cache và kích thước batch. Hỗ trợ chế độ **Dual-Model** (dùng model nhẹ quét nhanh UI/Menu rồi xả VRAM cho model chính dịch Cốt truyện) và **Chế độ Êm ái cho Laptop / USB Audio-Safe** (giới hạn luồng + nghỉ nhịp 65ms chống nóng máy & nhiễu USB DAC/tai nghe).
- **Hỗ trợ nhiều Engine & định dạng game phổ biến:**
  - **Unity** (Mono / IL2CPP, Addressables `catalog.bin` & `catalog.json`, `StringTable` / `SharedTableData`, tự đổi nhãn ngôn ngữ trong game thành *Tiếng Việt* và vá Font Unicode).
  - **Unreal Engine 4/5** (`.locres`, `.pak` mod riêng qua `Content/Paks/~mods/`).
  - **Ren'Py** (`.rpy`, `.rpa`), **JSON**, **CSV**, **PO**, **YAML**, **XML**, **TXT**.
- **Kho Skill 2 Lớp (Language Bridge + Style Skill):** Đi kèm 17 Skill dựng sẵn (11 cầu ngôn ngữ như `EN → VI`, `FR → VI`, `JA → VI`, `ZH → VI`, `KO → VI`... và 6 phong cách văn phong như *Chuẩn Tự Nhiên*, *Kinh Dị*, *Khoa Học Viễn Tưởng*, *Kiếm Hiệp / Huyền Huyễn*, *Visual Novel / Anime*, *Co-op Hài Hước*). Hỗ trợ tạo, chỉnh sửa No-Code hoặc nhập/xuất `.md` / `.zip`.
- **3 Mức độ dịch thông minh & Suy luận Pha 0 (Pre-Think):** Tự lập Từ điển Thuật ngữ (Glossary) và thẻ văn phong (Style Card) cố định trên đỉnh KV-Cache, kèm chống trùng lặp (Deduplication + Template Folding `__NUM_x__`) và Global Translation Memory (0ms).
- **Trình sửa bản dịch trực tiếp (Live Editor):** Tìm kiếm toàn bộ cơ sở dữ liệu SQLite, lọc theo loại câu (`UI`, `Sub`, `Đã sửa tay`, `Lỗi`), **Thay thế hàng loạt có khóa thẻ code**, và **Ghi nóng (Hot-Apply)** thẳng vào game trong vài giây không cần dịch lại.
- **Chia sẻ cộng đồng `.sakugapack` (1-Click):** Xuất và nhập gói bản dịch + từ điển thuật ngữ chỉ bằng một cú bấm.
- **Theo dõi cập nhật Steam (Delta Update):** Tự phát hiện khi game vừa cập nhật hoặc Verify Files trên Steam, giữ nguyên toàn bộ câu đã dịch trước đó và chỉ dịch bổ sung các câu mới.
- **Hệ thống Bảo vệ 6 Lớp:**
  1. **Anti-Cheat Ban Protection:** Tự quét EAC / BattlEye / Vanguard / Ricochet... và khóa chỉnh sửa file game online có Anti-Cheat để bảo vệ tài khoản.
  2. **Clean External Backup (SHA-256):** Sao lưu file gốc ra thư mục `backups/` bên ngoài thư mục game; khôi phục 100% nguyên bản chỉ với 1 nút bấm.
  3. **Native Slot Injection:** Gợi ý chèn vào slot ngôn ngữ an toàn (ưu tiên các slot Latin ít dùng để giữ nguyên tiếng Anh gốc).
  4. **Tag Firewall (`__TAG_x__`):** Khóa toàn bộ thẻ màu, biến số, xuống dòng (`<color>`, `{0}`, `%s`, `\n`, `[g:...]`) chống hỏng game.
  5. **Encoding & Font Guard:** Giữ nguyên UTF-8 BOM / CRLF, tự vá font Tiếng Việt chống ô vuông/lỗi dấu.
  6. **Atomic Writes & Crash Recovery:** Ghi file nguyên tử và lưu nhật ký SQLite theo từng batch — lỡ mất điện hay tắt máy vẫn chạy nối tiếp đúng chỗ đang dở.

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
1. Mở trang **[Releases mới nhất](https://github.com/hohieu112007-byte/black-sakura-translate/releases/latest)** và tải file **`Black-Sakura-Translate-v1.0.0-Windows-Standalone.zip`**.
2. Giải nén ra bất kỳ thư mục nào và nháy đúp **`BlackSakuraTranslate.exe`**.
3. Cài **[Ollama](https://ollama.com/download)** (nếu máy chưa có) — ngay khi mở app, thanh thiết lập 1-Click sẽ tự hiện để bạn bấm **▶ Mở Ollama ngay** và **⬇ Tải ngay `qwen3.5:4b` & Tự kết nối**.

### Cách 2: Chạy bản Portable / Mã nguồn (Nếu máy có Python 3.10+)
1. Tải **`Black-Sakura-Translate-v1.0.0-Source.zip`** tại mục Releases (hoặc `git clone` bên dưới) rồi giải nén.
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
