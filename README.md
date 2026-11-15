# FIFA Online 3 Server — Python

Server riêng (private server) cho game **FIFA Online 3** viết bằng
**Python 3** — tương thích nguyên bản client FO3 (không cần sửa client,
chỉ trỏ lại địa chỉ server trong `fo3_server.ini`). Repo này chứa toàn bộ
mã nguồn để bạn tự vận hành và tự phát triển tiếp.

> ⚠️ **CHỈ DÙNG ĐỂ TEST HOẶC CHƠI OFFLINE — KHÔNG DÙNG CHO
> PRODUCTION.** Bản Python này là bản triển khai tham chiếu/đầu tiên:
> lưu dữ liệu bằng SQLite một file, chưa có các lớp bảo vệ kinh tế và
> bảo mật cần cho một server mở công cộng (chống giả mạo kết quả trận,
> khóa đăng nhập, kiểm soát lạm phát EP…). Hãy chỉ chạy nó trên
> localhost/mạng nội bộ để thử nghiệm, học cách client–server giao
> tiếp, hoặc chơi offline một mình. Muốn vận hành server cho nhiều
> người chơi, hãy dùng bản Go + Gin (MariaDB + Redis, đã có các lớp
> bảo vệ kể trên).
>
> ⚠️ **FOR TESTING OR OFFLINE PLAY ONLY — NOT FOR PRODUCTION.** This
> Python implementation is the original/reference build: single-file
> SQLite storage and none of the economy/security hardening a public
> server needs (match-result forgery protection, login lockouts, EP
> inflation control…). Run it on localhost/a LAN only, to experiment,
> learn the client–server protocol, or play offline solo. For a
> multi-player server, use the Go + Gin implementation instead
> (MariaDB + Redis, with those protections built in).

> Đây là dự án fan-made phục vụ học tập và chơi cá nhân/nhóm nhỏ.
> Không liên kết với EA, Garena hay Nexon. Repo để **private** vì các
> thư mục dữ liệu đi kèm được trích xuất từ client game — hãy tự trích
> dữ liệu từ bản client của chính bạn (xem mục 3).

## Nguồn gốc

Repo này được fork từ hai dự án mã nguồn mở đi trước:

- [dtknd123/FO3-Server-Offline](https://github.com/dtknd123/FO3-Server-Offline)
- [soloplayer24/FIFA-ONLINE-3-Private-Server](https://github.com/soloplayer24/FIFA-ONLINE-3-Private-Server)

*This repository is a fork of [dtknd123/FO3-Server-Offline](https://github.com/dtknd123/FO3-Server-Offline) and [soloplayer24/FIFA-ONLINE-3-Private-Server](https://github.com/soloplayer24/FIFA-ONLINE-3-Private-Server).*

---

## 1. Thành phần

| File / thư mục | Vai trò |
|---|---|
| `http_config.py` | API game chính (HTTP): dữ liệu tĩnh, TTCN, giải đấu, cup, shop |
| `fo3_ext.py` | Lớp mở rộng: clan/club, bạn bè, phòng đấu, xếp hạng, staff, gia hạn HĐ… |
| `messenger.py` | Server Messenger TCP (chat, hiện diện online) — cổng **28002** |
| `relay_session.py` | Relay UDP cho PvP người–người — cổng **28000** |
| `staff_service.py` | Logic gói HLV huấn luyện (staff) |
| `gamedata/` | Dữ liệu game trích từ client: `fifadb.sqlite`, giá thẻ, formations… |
| `responses/` | JSON phản hồi tĩnh (`account_*.json`, `static_db_*.json`…) |
| `assets/` | Ảnh tĩnh: emblem CLB, ảnh vật phẩm/cầu thủ (externalAssets)… |
| `tools/re/` | Công cụ reverse engineering: đọc `.big`/`.gfx`, phân tích ABC (ActionScript 3), đọc EA DB |

Dữ liệu người dùng nằm trong SQLite `fo3_users.db` (tự tạo khi chạy lần
đầu); tiến độ người chơi lưu ngay trong cùng file DB.

## 2. Yêu cầu

- Python **3.12** (không cần thư viện ngoài — chỉ dùng thư viện chuẩn).
- Hệ điều hành: Windows hoặc Linux đều chạy được.
- Mở tường lửa 3 cổng: **8080** (API game, hoặc 80/443 nếu đặt sau
  reverse proxy), **28002** (Messenger TCP), **28000** (relay UDP).

## 3. Dữ liệu game

Server cần các thư mục `gamedata/`, `responses/`, `assets/` đặt cạnh mã
nguồn. Các bảng dữ liệu dạng text trong `gamedata/` (formations, bảng
giá, catalog…) và toàn bộ `responses/` **đã kèm trong repo** — server
đọc chúng lúc chạy, thiếu là lỗi. Hai thứ repo không chứa: **CSDL cầu
thủ dạng nhị phân** (`gamedata/fifadb.sqlite`) và **thư mục ảnh**
`assets/` (emblem, ảnh cầu thủ/vật phẩm trích từ client) — mỗi người
vận hành tự trích hai phần này từ bản client của mình bằng các công cụ
trong `tools/re/` (đọc `data_*.big`, `fifa_ng_db.db` của client);
thiếu ảnh server vẫn chạy, chỉ có một số hình hiển thị trống.

## 4. Chạy server

Đặt biến môi trường địa chỉ public của máy bạn (IP hoặc domain người
chơi kết nối tới). Mã nguồn để sẵn giá trị mẫu `203.0.113.10`
(documentation placeholder) ở các mặc định `FO3_SERVER_IP`,
`REDIR_HOST`/`NOTI_HOST` (`http_config.py`) và `DEFAULT_SERVER_IP`
(`fo3_ext.py`) — đặt biến môi trường như dưới, hoặc sửa thẳng các mặc
định đó thành địa chỉ của bạn:

```bash
# Linux
export FO3_SERVER_IP=<IP_HOAC_DOMAIN_CUA_BAN>

python3 http_config.py 8080     # API game (HTTP)
python3 messenger.py            # Messenger — cổng 28002
python3 relay_session.py        # Relay PvP — cổng 28000/UDP
```

```powershell
# Windows PowerShell
$env:FO3_SERVER_IP = "<IP_HOAC_DOMAIN_CUA_BAN>"
python http_config.py 8080
python messenger.py
python relay_session.py
```

Chạy bằng systemd trên Linux: tạo 3 unit tương ứng cho 3 tiến trình
trên, `WorkingDirectory` trỏ vào thư mục mã nguồn, biến môi trường
`FO3_SERVER_IP` đặt trong unit. Đặt Nginx làm reverse proxy trước cổng
8080 nếu muốn dùng domain + HTTPS — **tắt `proxy_cache` cho API game**,
chỉ cache thư mục ảnh tĩnh.

## 5. Cấu hình phía client

Trong thư mục client FO3, sửa file `fo3_server.ini`:

```ini
[Server]
IP=<IP_HOAC_DOMAIN_CUA_BAN>
```

⚠️ Chỉ ghi **domain hoặc IP trần, tuyệt đối không kèm cổng** — có
`:port` sẽ lỗi `COMMON_ERROR_CURL` và kẹt ở màn hình tải.

Tài khoản người chơi và tài khoản quản trị được tạo/quản lý qua trang
admin đi kèm (`admin.py`, thư mục `admin/`) hoặc trực tiếp trong
`fo3_users.db`.

## 6. Phát triển tiếp

- Toàn bộ logic game nằm ở `http_config.py` + `fo3_ext.py` (~244 route);
  route theo path, không phân biệt method ở phần lớn endpoint — khi sửa
  hành vi, hãy đối chiếu cách client thật gọi (log HTTP của server) thay
  vì đoán định dạng request.
- Dữ liệu tĩnh client nằm ở `responses/static_db_*.json`; bảng giá và
  dữ liệu cầu thủ ở `gamedata/`.
- Khi fork để phát triển: giữ nguyên định dạng phản hồi JSON mà client
  đang đọc (envelope `{"result": true, "resources": [...]}` ở đa số
  endpoint) — client rất nhạy với thiếu/thừa trường dữ liệu.
