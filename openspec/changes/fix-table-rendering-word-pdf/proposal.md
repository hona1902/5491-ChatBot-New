## Why

Khi người dùng tải lên file Word (.docx) hoặc PDF có chứa bảng biểu, nội dung bảng bị mất hoặc hiển thị sai: Word làm bảng biến mất hoàn toàn, PDF gộp các ô thành văn bản liên tục. Điều này ảnh hưởng nghiêm trọng đến trải nghiệm sử dụng tài liệu có cấu trúc dữ liệu dạng bảng.

## What Changes

- **Word (.docx)**: Hàm `extract_docx_content_detailed` trong `content_core/processors/office.py` hiện chỉ xử lý `doc.paragraphs`, bỏ qua hoàn toàn `doc.tables`. Cần bổ sung logic duyệt qua `doc.tables` và chuyển đổi sang Markdown table syntax.
- **PDF**: Hàm `_extract_text_from_pdf` trong `content_core/processors/pdf.py` đã có `convert_table_to_markdown()` và `page.find_tables()`, nhưng bảng Markdown được nối sau văn bản trang (`page_text +=`) rồi toàn bộ được đưa qua `clean_pdf_text()` — hàm này xóa các ký tự `|` dùng trong bảng Markdown thông qua regex làm sạch ký tự đặc biệt, phá vỡ cấu trúc bảng.
- **Frontend CSS**: `prose` class hiện thiếu một số style cho `table`, `th`, `td` khi dark mode; cần đảm bảo bảng hiển thị đúng visual.
- Không thay đổi API công khai, không breaking change.

## Capabilities

### New Capabilities
- `word-table-extraction`: Trích xuất và chuyển đổi bảng từ file DOCX sang Markdown GFM table syntax sử dụng `python-docx` API.
- `pdf-table-preservation`: Bảo toàn Markdown table sau khi `clean_pdf_text()` — tách bảng ra trước khi làm sạch, làm sạch phần text thường, rồi ghép lại.

### Modified Capabilities
- (Không có thay đổi yêu cầu cấp spec hiện tại)

## Impact

- **Backend**: `content_core` là thư viện cài từ pip (v1.14.1). Không thể sửa trực tiếp. Cần triển khai giải pháp **override/patch** tại tầng `open_notebook/graphs/source.py` hoặc tạo custom processor thay thế trong project.
- **Xử lý thứ tự**: Word — cần duyệt `doc.tables` xen kẽ với `doc.paragraphs` theo thứ tự xuất hiện trong tài liệu. PDF — cần tách bảng Markdown trước khi clean, clean phần text còn lại, rồi merge lại.
- **Frontend**: `SourceDetailContent.tsx` đã import `remarkGfm` và có custom `table/th/td` components — không cần thay đổi lớn, chỉ cần xác nhận CSS prose dark mode ổn.
- **Test**: trong thư mục tests, đã có test file VB TEST BANG BIEU.docx và VB TEST BANG BIEU.pdf hãy dùng để test nó.
