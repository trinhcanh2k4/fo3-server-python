# Bug history / Lịch sử lỗi

Known fixed bugs and open issues for this FIFA Online 3 private-server
codebase (Python implementation first, then the Go + Gin rewrite).
Listed so operators and contributors don't chase the same ghosts twice.

Danh sách lỗi đã sửa và lỗi còn tồn đọng của codebase server FIFA
Online 3 này (bản Python trước, bản viết lại Go + Gin sau) — để người
vận hành và người phát triển tiếp không phải đào lại cùng một vấn đề.

---

## Fixed — English

### Matches, rooms and the PvP relay
- Ranked match crashed when a match side carried an empty club object;
  sides now always carry a club block with a name string.
- Human PvP hung on the loading screen: the relay must re-originate
  peer messages on the recipient's stream **and echo every message
  back to its sender** — the client's lockstep session starves
  otherwise. Two relay packet types that poison the session were
  removed; match ids must stay numeric (< 2^31).
- The bot opponent in room matches never readied up (a bot flag was
  set for human matches too).
- Skipping the squad-preparation screen was impossible in human
  matches: stage advance now requires every participant's ready.
- Room invites moved from a web page to in-game letters; accepting
  checks the inviter's live presence and joins the room directly.
- Referee (center) seat: moving side → center → side desynced the
  room view. Root cause: the room JSON exceeded the Messenger frame
  cap (65,000 bytes) because guest entries embedded full rosters —
  entries now carry stub teams and oversized pushes trim team data.
- Forfeits are recorded as a 0–5 loss. League/cup forfeits once
  recorded the quitter as the 5–0 **winner** (score orientation was
  derived from a field the sweeper never sends).
- Match history recorded the opponent as yourself when you played as
  the guest; the win-streak panel showed NaN (statistics entries
  missed the `lose` key the client renders).
- Stadium changes crashed the client: a raw tagged stepper index
  reached the engine — settings are decoded to real stadium ids on
  store. Exhibition bot-team picking was broken end-to-end (the
  by-id SORT lists were stubs); the league "abandon" button failed
  because the client POSTs to a route that only accepted PUT.

### Clubs
- The Club Friendly mode was invisible/unusable behind **two**
  missing feature gates (room creation + club-page visibility).
- Club rooms: the client always addresses its room as id `1`; the
  server now resolves the caller's own hosted room instead of
  trusting the sent id, merges rooms for random matchmaking, and
  exempts club friendlies from the full-seats ready rule.
- A missing `records.wins` field in the club block made the ready
  screen throw an error dialog (the result screen reads the same
  block — see open issues).
- Kicked/accepted members kept seeing their old club until relog:
  membership changes now push a fresh club view to the affected
  coach.

### Friends and Messenger
- Coach data bled across accounts (a shared "current user" object);
  presence is now derived from live Messenger connections.
- Accepting a friend request acted on the **wrong pair**: the lookup
  returned the other party of the requester's first friendship, so
  accepts were no-ops (and a decline could have deleted a different
  friendship). Accept/decline now use the exact pair.

### Transfer market and economy
- The old market was fully synthetic (server listed infinite NPC
  offers and instantly bought anything listed). It is now a real
  player-to-player market: listings escrow the player for 24h,
  prices must sit within 80–100% of the reference price, sales pay
  out minus a 10% fee through the collect flow.
- Reference prices stabilize daily (00:00 local) from **real** sales:
  median ratio, clamped to ±10% per reset and [0.2×, 5×] overall,
  wash trades (same account / same IP) excluded. Hot cards activate
  their new price at a randomized moment within 12h after the reset
  to defeat reset-time sniping. The same reference price feeds the
  market, Trade and enhancement; Trade's EP payout is capped at
  100% of the cards' reference value.
- Daily EP caps for ranked and for non-ranked match rewards, plus an
  EP ledger recording every balance change for audit.
- Match settlement hardening: results only count once a match
  actually reached the playing stage (or a forfeit path), scores are
  range-validated — forged results at the preparation stage are
  discarded as no-contest.

### Players, staff and Team Color
- Generated players have no `_id`; matching/filtering players by
  `_id` silently wiped generated squads. Player identity is the
  roster id helper everywhere.
- Player lock: the client sends an identical empty POST for lock and
  unlock, so the endpoint toggles; locking can require a separate
  lock password with a 5-minute authorization window.
- Staff contracts: cap lowered 100 → 40 matches, one match burned
  per match played (appointed staff only, like players). The staff
  recontract dialog showed "maximum: NaN matches" and filtered out
  every staff member — the static staff data lacked the
  `max_staff_life` key the client reads. Staff pack purchase
  quantity cap raised 3 → 10.
- Team Color: season colors for Europe Legend, NHD and CSL 82/85/87
  were missing; unreachable colors were disabled (Inter Treble —
  two required players don't exist in the database; UB/UL/TL/ML —
  too few cards to ever reach the lowest tier); club-history
  counting now unions every card printing's club with the career
  history table; season colors carry their season code. The client
  applies only the single best base color per player, so Season
  World Legend (+3) could never beat Special Player (+5) on a full
  WL squad — the WL season boost was merged to +8 at 9–11 players.
- Formations: 33 "-SW" sweeper variants and 6 team rows were missing
  (a generator filter dropped them); the SW slot is position id 1 —
  mapping it like other slots produced a second goalkeeper.

### Accounts, security, website
- Predictable legacy session tokens (`FO3-<user>-<uid>`) are only
  honored on direct loopback/internal calls; an identity fallback
  chain that could resolve token-less requests to another account
  was removed entirely.
- Login lockout per (IP, account) pair; registration modes
  open/invite/closed; relay join authorization (the server vouches
  for match id + slot + source IP) and per-IP UDP rate limiting.
- Shop prices rescaled by real card value into a fixed EP band;
  quest catalogue completed (134 quests / 34 groups) with weekly
  GP rewards; Nginx API caching caused stale shop/cup/clan reads —
  the deployment guides now mandate `proxy_cache off` for the API.

---

## Đã sửa — Tiếng Việt

### Trận đấu, phòng và relay PvP
- Trận xếp hạng crash khi một phía của trận mang object CLB rỗng;
  nay mọi phía đều có khối CLB kèm tên dạng chuỗi.
- PvP người thật treo ở màn hình loading: relay phải phát lại tin
  nhắn của đối thủ trên luồng của người nhận **và echo mọi tin nhắn
  về cho chính người gửi** — thiếu thì session lockstep của client
  chết đói. Đã gỡ hai loại gói tin làm hỏng session; id trận phải là
  số (< 2^31).
- Bot trong phòng đấu không bao giờ sẵn sàng (cờ bot bị đặt cả cho
  trận người thật).
- Không bỏ qua được màn chuẩn bị đội hình ở trận người–người:
  chuyển stage nay đòi đủ ready của mọi người tham gia.
- Thư mời vào phòng chuyển từ trang web sang thư trong game; nhấn
  đồng ý kiểm tra trạng thái online thật rồi vào thẳng phòng.
- Ghế trọng tài (giữa): di chuyển phía → giữa → phía làm lệch view
  phòng. Gốc rễ: JSON phòng vượt trần frame Messenger (65.000 byte)
  vì entry khách nhúng cả roster — nay entry chỉ mang team rút gọn
  và push quá khổ tự cắt dữ liệu team.
- Bỏ cuộc ghi là thua 0–5. Bỏ cuộc ở giải/cup từng ghi người bỏ cuộc
  là **thắng** 5–0 (chiều tỉ số suy từ một trường sweeper không gửi).
- Lịch sử đấu ghi đối thủ thành chính mình khi mình là khách; chuỗi
  thắng hiện NaN (thiếu key `lose` mà client dùng để hiển thị).
- Đổi sân làm crash client: index thô có tag của stepper lọt xuống
  engine — nay giải mã thành id sân thật ngay khi lưu. Chọn đội bot
  giao hữu hỏng toàn tuyến (danh sách SORT theo id là stub); nút bỏ
  cuộc giải đấu lỗi vì client gửi POST vào route chỉ nhận PUT.

### Câu lạc bộ
- Chế độ đá clan (Club Friendly) bị ẩn/khóa bởi **hai** cổng tính
  năng thiếu (tạo phòng + hiển thị ở trang CLB).
- Phòng clan: client luôn gọi phòng của mình bằng id `1`; server nay
  tự tra phòng do chính người gọi làm chủ thay vì tin id gửi lên, tự
  ghép phòng khi đấu ngẫu nhiên, và miễn quy tắc đủ ghế cho đá clan.
- Thiếu trường `records.wins` trong khối CLB làm màn sẵn sàng bật
  hộp thoại lỗi (màn tổng kết cũng đọc khối này — xem tồn đọng).
- Thành viên bị kick/được duyệt vẫn thấy CLB cũ tới khi relog: thay
  đổi thành viên nay push giao diện CLB mới cho HLV bị ảnh hưởng.

### Bạn bè và Messenger
- Dữ liệu HLV bị lẫn giữa các tài khoản (object "người dùng hiện
  tại" dùng chung); trạng thái online nay suy từ kết nối Messenger
  thật.
- Nhấn đồng ý kết bạn tác động lên **nhầm cặp**: hàm tra cứu trả về
  đối tác của mối bạn bè đầu tiên của người gửi, nên đồng ý thành
  no-op (và từ chối có thể xóa nhầm một tình bạn khác). Nay đồng
  ý/từ chối dùng đúng cặp hai người.

### Thị trường chuyển nhượng và kinh tế
- TTCN cũ hoàn toàn là bot (server tự rao vô hạn và tự mua ngay khi
  HLV rao). Nay là thị trường người–người thật: rao bán ký gửi cầu
  thủ 24h, giá phải nằm trong 80–100% giá tham chiếu, bán được nhận
  EP trừ phí 10% qua luồng thu tiền cũ.
- Giá tham chiếu bình ổn mỗi ngày (00:00 giờ địa phương) từ giá bán
  **thật**: trung vị, kẹp ±10%/lần reset và [0,2×, 5×] tổng thể,
  loại giao dịch rửa (cùng tài khoản/cùng IP). Thẻ hot kích hoạt giá
  mới ở thời điểm ngẫu nhiên trong 12h sau reset để chống canh giờ.
  Giá tham chiếu dùng chung cho TTCN, Trade và cường hóa; EP trả của
  Trade kẹp tối đa 100% tổng giá trị thẻ.
- Trần EP/ngày cho thưởng trận xếp hạng và trận thường, kèm sổ cái
  EP ghi mọi biến động số dư để kiểm toán.
- Siết kết toán trận: kết quả chỉ tính khi trận đã vào giai đoạn thi
  đấu (hoặc qua đường xử thua), tỉ số được kiểm tra trong biên hợp
  lệ — kết quả bịa ở khâu chuẩn bị bị hủy thành no-contest.

### Cầu thủ, HLV (staff) và Màu đội (Team Color)
- Cầu thủ được tạo (generated) không có `_id`; lọc cầu thủ theo
  `_id` từng xóa lặng lẽ cả đội hình generated. Danh tính cầu thủ
  nay dùng một hàm id thống nhất.
- Khóa cầu thủ: client gửi POST rỗng y hệt cho khóa và mở khóa nên
  endpoint hoạt động dạng toggle; khóa có thể đòi mật khẩu khóa
  riêng với cửa sổ ủy quyền 5 phút.
- Hợp đồng HLV: trần giảm 100 → 40 trận, mỗi trận đã đá trừ 1 trận
  (chỉ HLV đang bổ nhiệm, như cầu thủ). Hộp thoại tái kí HLV hiện
  "tối đa: NaN trận" và lọc sạch mọi HLV — dữ liệu tĩnh của staff
  thiếu key `max_staff_life` mà client đọc. Giới hạn số lượng mua
  gói HLV nâng 3 → 10.
- Màu đội: thiếu màu mùa Europe Legend, NHD, CSL 82/85/87; tắt các
  màu không bao giờ đạt được (Cú ăn 3 Inter — hai cầu thủ yêu cầu
  không tồn tại trong CSDL; UB/UL/TL/ML — quá ít thẻ để đạt bậc
  thấp nhất); lịch sử CLB nay tính theo hợp nhất mọi bản in thẻ với
  bảng lịch sử sự nghiệp; màu mùa mang theo mã mùa. Client chỉ áp
  đúng một màu nền tốt nhất cho mỗi cầu thủ nên Season World Legend
  (+3) không bao giờ thắng Special Player (+5) ở đội hình toàn WL —
  đã gộp số: màu mùa WL nâng lên +8 ở bậc 9–11 người.
- Sơ đồ đội hình: thiếu 33 biến thể "-SW" (hậu vệ quét) và 6 dòng
  đội (bộ lọc của generator loại bỏ); ô SW là vị trí id 1 — map như
  các ô khác sẽ sinh thủ môn thứ hai.

### Tài khoản, bảo mật, website
- Token phiên legacy đoán được (`FO3-<user>-<uid>`) chỉ còn hiệu
  lực cho request loopback/nội bộ trực tiếp; đã gỡ hẳn chuỗi fallback
  định danh có thể gán request không token cho tài khoản khác.
- Khóa đăng nhập theo cặp (IP, tài khoản); chế độ đăng ký
  open/invite/closed; khóa ủy quyền vào relay (server xác nhận
  match id + slot + IP nguồn) và giới hạn tốc độ UDP theo IP.
- Giá shop map lại theo giá trị thật của thẻ trong một khoảng EP cố
  định; danh mục nhiệm vụ hoàn thiện (134 nhiệm vụ / 34 nhóm) kèm
  thưởng GP tuần; cache API của Nginx từng gây đọc cũ ở shop/cup/
  clan — tài liệu triển khai nay bắt buộc `proxy_cache off` cho API.

---

## Open issues — English

- **Club Ranked is not implemented** (season/division/WP/rewards) —
  only Club Friendly works.
- **2v2/3v3 rooms** are implemented server-side (multi-seat rooms,
  queue assembly, N-player relay) but **not yet verified with real
  game clients** — that needs 4–6 clients playing a full match.
- **Club record room intentionally shows empty shells.** The client
  throws an error when full record data is delivered on first entry
  (a page-lifecycle race the server cannot time its way out of);
  records are still accumulated server-side from real club matches.
- **Post-match summary screen may close the client** after club
  friendly matches. One suspected cause (a missing records field in
  the club block) is fixed; a full retest is still outstanding.
- **Placement W-D-L sequence**: results are tracked server-side, but
  the client has no UI element that renders the per-match sequence,
  so it cannot be displayed without patching the client.
- **Referee seat in multi-seat rooms**: the matched-stage room push
  can still exceed the Messenger frame cap and be dropped (possible
  view desync in 2v2/3v3).
- A few cosmetic assets still return 404 (some shop icons, trophy
  and crest placeholders).
- The match-length (halfLength) stepper on the setting screen never
  sends its changes — not investigated yet.
- The game folder still needs `version.dll` + `fo3_server.ini`
  visible next to the client; there is no supported way to hide
  them.
- Python implementation only: the transfer market there is the old
  synthetic one and there are no economy/security guardrails — see
  the production warning in its README.

## Tồn đọng — Tiếng Việt

- **Chưa làm Club Ranked** (mùa giải/hạng/WP/thưởng) — mới có đá
  clan giao hữu (Club Friendly).
- **Phòng 2v2/3v3** đã dựng xong phía server (phòng nhiều ghế, hàng
  đợi gom người, relay N đầu mối) nhưng **chưa nghiệm thu bằng
  client game thật** — cần 4–6 client đá trọn một trận.
- **Phòng thành tích CLB để trống có chủ đích.** Client báo lỗi khi
  nhận dữ liệu thành tích đầy đủ ngay lần vào đầu tiên (lỗi vòng
  đời trang phía client, server không né bằng thời gian được);
  thành tích vẫn được ghi nhận phía server từ các trận clan thật.
- **Màn tổng kết có thể làm client tự đóng** sau trận đá clan. Một
  nghi phạm (thiếu trường records trong khối CLB) đã sửa; vẫn chờ
  test lại trọn luồng.
- **Chuỗi Thắng-Hòa-Thua phân hạng**: server có ghi nhận nhưng
  client không có thành phần giao diện nào hiển thị chuỗi từng
  trận, nên không hiển thị được nếu không vá client.
- **Ghế trọng tài ở phòng nhiều ghế**: push phòng ở giai đoạn đã
  ghép trận vẫn có thể vượt trần frame Messenger và bị bỏ (có thể
  lệch view ở 2v2/3v3).
- Một số ảnh trang trí vẫn 404 (vài icon shop, ảnh cúp/cờ hiệu giữ
  chỗ).
- Stepper thời lượng trận (halfLength) ở màn cài đặt không gửi thay
  đổi — chưa điều tra.
- Thư mục game vẫn phải để lộ `version.dll` + `fo3_server.ini` cạnh
  client; chưa có cách hỗ trợ để ẩn chúng.
- Riêng bản Python: TTCN ở đó vẫn là thị trường bot cũ và không có
  lớp bảo vệ kinh tế/bảo mật — xem cảnh báo production trong README
  của nó.
