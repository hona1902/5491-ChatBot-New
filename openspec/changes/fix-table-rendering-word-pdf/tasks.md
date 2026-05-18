## 1. DOCX Table Extractor (Backend Custom Processor)

- [x] 1.1 Tạo file `open_notebook/utils/docx_table_extractor.py` với hàm `extract_docx_with_tables(file_path: str) -> str` — duyệt `doc.element.body` để lấy đúng thứ tự paragraph và table
- [x] 1.2 Implement hàm `_table_to_markdown(table) -> str` chuyển đổi `python-docx Table` object sang Markdown GFM table (header row + separator + data rows)
- [x] 1.3 Xử lý edge cases: ô rỗng → chuỗi rỗng, bảng không có hàng → bỏ qua, text trong ô có newline → replace thành space
- [x] 1.4 Giữ lại toàn bộ logic paragraph hiện tại (heading, bold, italic, list) từ `office.py` — copy và mở rộng, không thay thế hoàn toàn

## 2. PDF Table Preserver (Backend Custom Processor)

- [x] 2.1 Tạo file `open_notebook/utils/pdf_table_preserver.py` với hàm `extract_pdf_with_tables(file_path: str) -> str`
- [x] 2.2 Implement logic: với mỗi page, extract text, detect tables, build page content với table markers đặc biệt (dùng `<<<TABLE_START>>>` / `<<<TABLE_END>>>` thay vì `[Table N...]` để dễ parse hơn)
- [x] 2.3 Implement `_preserve_tables_in_clean(text: str) -> str`: tách các block bảng ra, chỉ gọi `clean_pdf_text()` trên phần text thường, ghép lại sau
- [x] 2.4 Import và gọi `convert_table_to_markdown` và `clean_pdf_text` từ `content_core.processors.pdf`

## 3. Tích Hợp vào Source Processing Graph

- [x] 3.1 Trong `open_notebook/graphs/source.py`, thêm logic detect file type từ `content_state`: nếu là `.docx` → gọi custom DOCX extractor; nếu là `.pdf` → gọi custom PDF extractor
- [x] 3.2 Đảm bảo custom extractor output được gán vào `content_state.content` (hoặc `processed_state.content`) — cần review đúng field name của `ProcessSourceState`
- [x] 3.3 Fallback: nếu custom extractor raise exception, log warning và fallback về `extract_content()` mặc định của content-core

## 4. Frontend CSS Verification

- [x] 4.1 Kiểm tra `frontend/src/app/globals.css` hoặc file CSS chính xem có đủ styles cho `.prose table`, `.prose th`, `.prose td` trong dark mode không
- [x] 4.2 Nếu thiếu, bổ sung CSS variables hoặc Tailwind prose config để bảng hiển thị đúng border, padding, alternating row colors trong cả light và dark mode
- [ ] 4.3 Verify trong browser: upload file docx/pdf có bảng, mở Source Detail, chuyển sang tab Content, xác nhận bảng hiển thị đúng

## 5. Testing

- [x] 5.1 Tạo file test `tests/test_docx_table_extractor.py` với test case: DOCX có bảng đơn giản → output chứa `| --- |`
- [x] 5.2 Tạo test case: DOCX có paragraph → bảng → paragraph → output đúng thứ tự
- [x] 5.3 Tạo test case: DOCX không có bảng → output không bị lỗi
- [x] 5.4 Tạo file test `tests/test_pdf_table_preserver.py` với test case cơ bản (mock page.find_tables() output)
- [ ] 5.5 Manual test: upload thực tế file Word và PDF có bảng vào hệ thống đang chạy và verify UI

## 6. Documentation

- [x] 6.1 Cập nhật CHANGELOG.md: thêm entry "Fix: Word tables no longer disappear when uploading DOCX; PDF tables now render correctly as Markdown tables"
