## Context

Dự án sử dụng thư viện `content-core` (v1.14.1) để xử lý tài liệu. Thư viện này là dependency cài từ pip và không thể sửa trực tiếp source code. Hai processor liên quan:

- **`office.py`** (`extract_docx_content_detailed`): Chỉ xử lý `doc.paragraphs`, bỏ qua `doc.tables` hoàn toàn — bảng biến mất.
- **`pdf.py`** (`_extract_text_from_pdf`): Có logic detect tables (`page.find_tables()`) và `convert_table_to_markdown()` nhưng sau đó toàn bộ text (kể cả Markdown table) được đưa qua `clean_pdf_text()`. Hàm này có step `re.sub(r"[ \t]+", " ", text)` và các regex khác làm xẹp whitespace và xóa ký tự `|` bằng cách gộp spaces — chuỗi `| col1 | col2 |` bị biến thành plain text.

Frontend (`SourceDetailContent.tsx`) đã tích hợp `react-markdown` với `remarkGfm` và custom components cho `table/th/td` — phía hiển thị đã sẵn sàng.

## Goals / Non-Goals

**Goals:**
- Word (.docx): Trích xuất bảng theo đúng thứ tự xuất hiện xen kẽ với paragraphs và chuyển thành Markdown GFM table.
- PDF: Bảo toàn cấu trúc Markdown table sau quá trình làm sạch văn bản.
- Không phụ thuộc vào việc sửa `content-core` — giải pháp nằm hoàn toàn trong project.
- Frontend: Đảm bảo CSS prose dark mode hiển thị bảng đúng.

**Non-Goals:**
- Hỗ trợ bảng lồng nhau (nested tables).
- Xử lý bảng trong `.doc` (Word 97-2003, không phải `.docx`).
- Upgrade `content-core` lên phiên bản mới hơn (nếu có).
- Xử lý bảng trong PPTX.

## Decisions

### D1: Override thay vì fork content-core

**Quyết định**: Tạo custom processor trong project thay vì patch `content-core`.

**Lý do**: `content-core` là dependency pip — patch trực tiếp sẽ bị overwrite khi cập nhật. Custom processor được đặt tại `open_notebook/utils/document_processor.py` và được gọi trực tiếp từ `graphs/source.py` trước khi gọi `extract_content()`.

**Thay thế xem xét**: Monkey-patch `content_core.processors.office` và `content_core.processors.pdf` — phức tạp và dễ vỡ.

### D2: Word — Xử lý bảng xen kẽ theo document order

**Quyết định**: Duyệt qua `doc.element.body` XML để lấy thứ tự thực sự của paragraphs và tables, không chỉ dựa vào `doc.paragraphs`.

**Lý do**: `python-docx` cung cấp `doc.paragraphs` và `doc.tables` riêng biệt, không giữ thứ tự tương đối. Để bảng xuất hiện đúng vị trí trong output, cần duyệt qua `doc.element.body` và phân biệt `CT_P` (paragraph) và `CT_Tbl` (table).

**Markdown table format**:
```
| Header 1 | Header 2 |
| --- | --- |
| Cell 1 | Cell 2 |
```

### D3: PDF — Tách bảng ra trước khi clean, merge sau

**Quyết định**: Thay vì gọi `clean_pdf_text()` trên toàn bộ nội dung, tách các đoạn Markdown table (nhận dạng bằng pattern `[Table N from page M]`) ra, chỉ clean phần text thông thường, rồi ghép lại.

**Lý do**: `clean_pdf_text()` có nhiều regex cần thiết cho text thường nhưng phá vỡ Markdown table. Tách và bảo vệ các đoạn bảng là cách an toàn nhất.

**Thay thế xem xét**: Sửa `clean_pdf_text()` để skip các dòng bắt đầu bằng `|` — dễ nhưng không an toàn với edge cases (text bình thường chứa `|`).

### D4: Implement là custom pre/post processor, gọi từ source graph

**Quyết định**: `open_notebook/utils/docx_table_extractor.py` và `open_notebook/utils/pdf_table_preserver.py` là các module standalone. `graphs/source.py` sẽ detect loại file và gọi custom processor thay vì để `content-core` xử lý khi cần.

## Risks / Trade-offs

- **[Risk] python-docx table API phức tạp**: Cells có thể merge (colspan/rowspan), cần xử lý `cell.text` đơn giản trước, không map đầy đủ colspan → Mitigation: Chỉ extract text, không cố reproduce layout phức tạp.
- **[Risk] PDF table detection sai**: `page.find_tables()` của PyMuPDF có thể nhận sai cấu trúc bảng trong PDF scan → Mitigation: Đã có sẵn fallback, nếu detect fail thì skip, không crash.
- **[Risk] Marker `[Table N from page M]` xuất hiện trong text thường**: Khó xảy ra nhưng có thể → Mitigation: Dùng UUID-based markers nếu cần, hoặc chấp nhận edge case này.
- **[Trade-off]**: Giải pháp override sẽ không tự update khi `content-core` fix bug — cần review khi upgrade thư viện.

## Migration Plan

1. Thêm custom processor modules (không ảnh hưởng code hiện tại).
2. Cập nhật `graphs/source.py` để route docx/pdf qua custom processor.
3. Test với file docx và pdf có bảng.
4. Deploy — không cần migration data, chỉ ảnh hưởng file mới upload.
5. Rollback: Xóa routing custom processor, revert về `extract_content()` thuần.
