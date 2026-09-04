# Telegram Auto Message Scheduler

Tự động gửi tin nhắn định kỳ từ tài khoản Telegram của bạn tới một bot.

## Yêu cầu

- Python 3.11+
- Tài khoản Telegram
- API ID & API Hash từ [my.telegram.org](https://my.telegram.org)

## Cài đặt

```bash
pip install -r requirements.txt
```

## Cấu hình

1. Mở `config.json` và điền:
   - `api_id`: số API ID
   - `api_hash`: chuỗi API Hash
   - `bot`: username bot (ví dụ `@example_bot`)
   - `message`: nội dung tin nhắn (ví dụ `/chaoban`)
   - `interval_hours`: số giờ giữa các lần gửi (mặc định 24)

## Chạy

```bash
python main.py
```

Lần đầu chạy sẽ yêu cầu:
1. Số điện thoại (kèm mã quốc gia, ví dụ `+849xxxxxxxx`)
2. Mã OTP từ Telegram
3. Mật khẩu 2FA (nếu có)

Session sẽ được lưu trong thư mục `session/` để không cần đăng nhập lại.

## Cấu trúc thư mục

```
telegram_auto_scheduler/
├── main.py
├── config.json
├── requirements.txt
├── session/
├── database/
│   └── data.db
├── logs/
│   └── app.log
└── README.md
```

## Nhật ký

Log được ghi vào `logs/app.log` và console.

## Lưu ý bảo mật

- Không chia sẻ file session
- Không commit `config.json` chứa api_hash thật lên Git
- Không ghi số điện thoại / OTP vào log
