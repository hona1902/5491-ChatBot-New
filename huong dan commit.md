Cách an toàn nhất trước khi commit Phase 1

Trước tiên xem commit hiện tại:

git status
git log --oneline -5

Tạo branch backup tại trạng thái trước khi commit Phase 1:

git branch backup-before-table-aware-qa-phase1

Hoặc tạo tag:

git tag before-table-aware-qa-phase1

Sau đó mới commit Phase 1:

git status
git diff --stat
git add .
git commit -m "feat: add phase 1 table-aware QA"
Sau này muốn quay lại code trước Phase 1

Nếu bạn tạo branch backup:

git checkout backup-before-table-aware-qa-phase1

Nếu bạn tạo tag:

git checkout before-table-aware-qa-phase1

Lưu ý: checkout tag sẽ vào trạng thái detached HEAD. Muốn tạo branch từ đó:

git checkout -b restore-before-phase1 before-table-aware-qa-phase1
Nếu muốn hủy Phase 1 trên branch hiện tại

Cách an toàn nhất là dùng revert, vì nó tạo commit mới để đảo ngược Phase 1:

git revert <commit_hash_phase1>

Lấy commit hash bằng:

git log --oneline

Không nên dùng git reset --hard nếu bạn đã push hoặc chưa chắc chắn, vì nó làm lịch sử branch thay đổi mạnh.

Quy trình mình khuyên dùng
git status
git branch backup-before-table-aware-qa-phase1
git tag before-table-aware-qa-phase1
git add .
git commit -m "feat: add phase 1 table-aware QA"

Như vậy bạn có cả branch backup và tag mốc thời gian. Sau này có lỗi nghiêm trọng, bạn luôn quay lại được trạng thái trước Phase 1.