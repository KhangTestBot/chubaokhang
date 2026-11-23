# Locket 60s AI + Photobooth + YouTube — Duy Khánh

![Ảnh giới thiệu Duy Khánh](assets/zalo-intro.png)

Module Shadowrocket cá nhân của **Duy Khánh** (hoạt động độc lập hoàn toàn trên repo `KhangTestBot/chubaokhang` và backend Vercel `https://chubaokhang.vercel.app`):

- **Locket Gold & quay 60 giây**: mở khóa tính năng Gold, thời lượng video 60s, ảnh/popup chào mừng Duy Khánh.
- **Bộ caption tùy chỉnh**: bộ caption Duy Khánh + nhóm **⏳ Đếm ngược ngày lễ 🎉** tự động tính số ngày theo giờ Việt Nam (UTC+7).
- **Viền Giáng sinh cho tiện ích (Widget)**: phát trực tiếp từ Vercel CDN (`https://chubaokhang.vercel.app/midautumn_2026.png`, tự rewrite đầy đủ các biến thể `_systemSmall@3x.png`, `_systemLarge@3x.png` mà Locket yêu cầu).
- **105 bộ lọc & Photobooth**: gồm **78 hiệu ứng AI** (bao gồm 22 hiệu ứng độc quyền & vui nhộn trong nhóm **Duy Khánh Exclusive ✨**) (chạy trên Cloudflare Workers AI `@cf/black-forest-labs/flux-2-klein-4b`) và **27 khung ảnh / Photobooth** (ghép thật bằng Python + Pillow trên Vercel).
- **YouTube**: tích hợp sẵn quy tắc chặn quảng cáo YouTube.

---

## Hướng dẫn cài đặt trên iPhone (dùng backend cố định)

### Bước 1: Cài Module vào Shadowrocket
1. Mở **Shadowrocket** → vào mục **Cấu hình (Config)** → **Mô-đun (Module)** → bấm dấu **+** (Thêm mô-đun).
2. Dán đường dẫn module:
   ```text
   https://raw.githubusercontent.com/KhangTestBot/chubaokhang/refs/heads/main/module/locket-60s-ai-youtube.module
   ```
3. Bật module vừa thêm (**chỉ bật duy nhất 1 module Locket** để tránh xung đột).
4. Vào **Cấu hình** → chọn file cấu hình đang dùng → **Giải mã HTTPS (HTTPS Decryption)** → bật **HTTPS Decryption**, tạo/cài chứng chỉ CA rồi vào **Cài đặt iPhone → Cài đặt chung → Giới thiệu → Cài đặt tin cậy chứng nhận** để bật tin cậy chứng chỉ Shadowrocket.

### Bước 2: Nhập Backend URL và Mã kết nối (chỉ làm 1 lần duy nhất)
Hai thông số kết nối là **cố định** và được Shadowrocket lưu lại cục bộ trên máy (cập nhật module hay đẩy code mới không làm mất):

1. **Bật VPN** trong Shadowrocket.
2. Mở **Safari** trên iPhone và truy cập:
   ```text
   http://locket-ai-setup.invalid/
   ```
3. Nhập 2 thông tin sau rồi bấm **Lưu**:
   - **Backend URL (Đường dẫn máy chủ)**:
     ```text
     https://chubaokhang.vercel.app
     ```
   - **Mã kết nối (`BACKEND_ACCESS_TOKEN`)**:
     ```text
     <nhập mã BACKEND_ACCESS_TOKEN lấy từ Vercel>
     ```
     > **Cách xem lại Mã kết nối:** Vào [Vercel Dashboard](https://vercel.com/) → chọn dự án **`chubaokhang`** → **Settings** → **Environment Variables** → tìm biến **`BACKEND_ACCESS_TOKEN`** và bấm biểu tượng con mắt (**Reveal**) để sao chép. *(Không công khai chuỗi mã thật trên README vì đây là repo công khai — giữ kín mã để tránh người lạ dùng hết hạn mức AI trong ngày.)*
4. Vuốt tắt hẳn app **Locket** rồi mở lại (giữ Shadowrocket bật vài giây để Locket tải cấu hình mới).
5. Kiểm tra nhanh backend đang sống bằng cách mở: `https://chubaokhang.vercel.app/health` — trả về `{"ok":true,"configured":true,"compositionConfigured":true}` là hoạt động bình thường.

> **Lưu ý về hạn mức AI:** Tài khoản Cloudflare Workers AI gói Free có hạn mức **10.000 Neurons/ngày** và tự động reset về `0` vào **07:00 sáng giờ Việt Nam (00:00 UTC)**. Khi hết hạn mức trong ngày, các bộ lọc AI sẽ tạm báo lỗi nhưng các khung **Photobooth** vẫn hoạt động bình thường.

### Bước 3: Hiện khung viền Giáng sinh trên tiện ích (Widget)
1. Đảm bảo đã cập nhật module bản mới nhất trong Shadowrocket (`duykhanh-ai-ptb-15` trở lên).
2. Giữ Shadowrocket **BẬT**, đóng hẳn rồi mở lại app **Locket**.
3. Gửi/nhận 1 ảnh mới hoặc **xóa tiện ích Locket ngoài màn hình chính rồi thêm lại** để iOS tải khung viền mới.

### Bước 4 (Tùy chọn): Tự động bật/tắt Shadowrocket khi mở/đóng Locket
Dùng ứng dụng **Phím tắt (Shortcuts)** sẵn có trên iPhone → chuyển sang tab **Tự động hóa (Automation)** ở thanh dưới cùng:
1. **Tự động bật**: Bấm **+** → **Ứng dụng (App)** → chọn **Locket** → chọn **Được mở (Is Opened)** + **Chạy ngay (Run Immediately)** (tắt *Thông báo khi chạy*) → thêm tác vụ Shadowrocket **Kết nối VPN (Connect VPN)**.
2. **Tự động tắt**: Tạo thêm 1 tự động hóa tương tự nhưng chọn **Được đóng (Is Closed)** + **Chạy ngay** → thêm tác vụ Shadowrocket **Ngắt kết nối VPN (Disconnect VPN)**.

---

## Cấu hình biến môi trường backend

| Biến | Mặc định / Ý nghĩa |
| --- | --- |
| `CLOUDFLARE_ACCOUNT_ID` | Account ID Cloudflare Workers AI. |
| `CLOUDFLARE_API_TOKEN` | API Token Cloudflare Workers AI (giữ bí mật trong Environment Variables). |
| `BACKEND_ACCESS_TOKEN` | Mã kết nối cố định nhập trên trang `http://locket-ai-setup.invalid/` (tối thiểu 24 ký tự). |
| `BLOB_READ_WRITE_TOKEN` | Tự cấp khi gắn Vercel Blob Storage (dùng lưu ảnh kết quả tạm thời). |
| `OPENAI_API_KEY` | Tùy chọn nếu chạy bằng provider OpenAI thay vì Cloudflare. |
| `PUBLIC_BASE_URL` | HTTPS origin công khai (tự nhận trên Vercel). |
| `IMAGE_TTL_SECONDS` | `900` (ảnh kết quả tồn tại tối đa 15 phút). |
| `RATE_LIMIT_PER_MINUTE` / `MAX_CONCURRENT` | `3` yêu cầu/phút và `1` yêu cầu đồng thời (áp dụng khi chạy máy chủ local). |

---

## Chạy cục bộ (Windows / Docker) & Kiểm thử

- **Windows**: chạy `Start-Locket-Backend.cmd` (dùng Python + `cloudflared` tunnel tạm thời, không lưu khóa ra tệp).
- **Docker**: repo có sẵn `Dockerfile` — truyền biến môi trường và đặt sau reverse proxy HTTPS.
- **Chạy bộ test JavaScript** (Node.js 22+):
  ```bash
  npm test
  ```
- **Chạy bộ test Python**:
  ```bash
  python3 -m unittest discover -s test_python -v
  ```

Nguồn tham khảo: [SOURCES.md](SOURCES.md) · Mã kiểm tra toàn vẹn tệp: [SHA256SUMS.txt](SHA256SUMS.txt).
