#!/usr/bin/env python3
"""FO3 session relay v4 (UDP 28000) — a real protocol participant.

Protocol (from upstream tools/re/relay_emu.py, RE of fifazf.exe
0xa65640 / 0xa651b0 / 0xa66c40 / 0xa682b0; verified byte-for-byte
against our own captures 2026-11-13):

  packet = u16 checksum | u16 key | obfuscated payload (all LE)
  checksum: h = 0x17; for b in packet[2:]: h = (h*0x103 + b) & 0xffff
  decrypt: chained XOR/subtract with dl = (k>>8)+(k&0xff), then
           de-rotate by k; encrypt is the inverse.

  Plaintext header (24 B): [4] i8 src slot, [5] i8 dst slot
  (the relay is slot 0x14), [6:10] u32 matchid, [0xa:0xe] u32
  reliable seq (type 1) / sub-message count (type 2),
  [0xe:0x12] u32 cumulative ack, [0x12:0x16] u32 token,
  [0x16:0x18] u16 type.
  Types: 1 = reliable chunk, 2 = reliable aggregate,
  0x4001 = aggregate wanting an ack -> answer 0x4002,
  0x4005 = ping {u32 ts, u32 ip, u16 port} -> answer 0x4006 pong
  echoing the body (the pong sets the slot's connected flag).
  Reliable chunks reassemble into a stream of {u16 len, u16
  msgid, body} messages; msgid 0x100 = JOIN {u32 matchid,
  u8 users, u8 slot, u8 flag} -> answer with reliable messages
  0x100 {u32 token} then 0x101 (inited).

Why v1-v3 failed: the packets are addressed to dst slot 0x14 —
the relay itself. Forwarding them to the peer (plain or
source-spoofed) delivers a packet meant for the relay, and
nobody ever pongs, assigns a token, or terminates the reliable
stream, so the clients re-ping forever and the session never
connects.

v4 behaviour: emu-faithful per-client handling (pong, ack,
JOIN -> token + inited, resend-unacked on every packet), plus
routing on the recipient's relay stream.

v5 (2026-11-13, from the full-memory-dump read of the client
stream handler at fifazf 0x9D4E20): message 0x400 ("loading
finished") is addressed to the session server and has NO
handler case on the client — forwarding it crashed both
engines in the handler's deliberate unknown-msgid trap at
0x9D5898. The relay now consumes 0x400 from both players and
answers 0x302 ("start now") to both; all other stream
messages are forwarded on the recipient's relay stream.
Everything is logged decoded to /root/fo3-capture.log.
"""
import json
import random
import socket
import urllib.request
import struct
import threading
import time

RELAY_SLOT = 0x14
LOG_PATH = "/root/fo3-capture.log"
_log_lock = threading.Lock()


def log(msg):
    ts = time.strftime("%Y-%m-%d %H:%M:%S", time.gmtime()) + \
        f".{int((time.time() % 1) * 1000):03d}"
    with _log_lock:
        with open(LOG_PATH, "a") as f:
            f.write(f"{ts} RELAY4 {msg}\n")


def checksum(b):
    h = 0x17
    for x in b:
        h = (h * 0x103 + x) & 0xFFFF
    return h


def decrypt(p):
    k = int.from_bytes(p[2:4], "little")
    n = len(p) - 4
    dl = ((k >> 8) + (k & 0xFF)) & 0xFF
    prev, r = p[3], []
    for x in p[4:]:
        r.append(((x ^ prev) - dl) & 0xFF)
        prev = x
    pay = [0] * n
    for i in range(n):
        pay[(k + i) % n] = r[i]
    return p[:4] + bytes(pay)


def encrypt(plain, k=None):
    k = k or random.randint(1, 0xFFFF)
    pay = plain[4:]
    n = len(pay)
    dl = ((k >> 8) + (k & 0xFF)) & 0xFF
    out = [pay[(k + i) % n] for i in range(n)]
    prev, c = k >> 8, []
    for x in out:
        prev = ((x + dl) & 0xFF) ^ prev
        c.append(prev)
    rest = struct.pack("<H", k) + bytes(c)
    return struct.pack("<H", checksum(rest)) + rest


def header(src, dst, mid, ack, typ, rseq=0, token=0):
    return b"\0" * 4 + struct.pack("<bbIIIIH", src, dst, mid, rseq, ack, token, typ)


def parse(plain):
    src, dst, mid, rseq, ack, token, typ = struct.unpack_from("<bbIIIIH", plain, 4)
    return dict(src=src, dst=dst, mid=mid, rseq=rseq, ack=ack,
                token=token, type=typ, body=plain[24:])


class Client:
    """Reliable stream state of one client (emu-faithful)."""

    def __init__(self, addr):
        self.addr = addr
        self.ack, self.got, self.stream, self.seq, self.out = 0, {}, b"", 0, {}
        self.mid = None        # matchid from JOIN
        self.slot = None       # slot from JOIN / packet src
        self.joined = False
        self.last = time.time()
        self.last105 = None    # latest short 0x105 frame (ghost template)
        self.big_blob = None   # latest big 0x105 blob (start stats/AI blob)

    def take(self, m):
        for q in [q for q in self.out if q <= m["ack"]]:
            del self.out[q]
        subs = []
        if m["type"] == 1:
            subs = [(m["rseq"], m["body"])]
        elif m["type"] in (2, 0x4001, 0x4004) and m["body"] and m["body"][0] == 0:
            p = 1
            for _ in range(m["rseq"]):
                if p + 6 > len(m["body"]):
                    break
                q, n = struct.unpack_from("<IH", m["body"], p)
                subs.append((q, m["body"][p + 6:p + 6 + n]))
                p += 6 + n
        for q, chunk in subs:
            if q > self.ack:
                self.got.setdefault(q, chunk)
        while self.ack + 1 in self.got:
            self.ack += 1
            self.stream += self.got.pop(self.ack)
        msgs = []
        while len(self.stream) >= 4:
            n, msgid = struct.unpack_from("<HH", self.stream)
            if len(self.stream) < 4 + n:
                break
            msgs.append((msgid, self.stream[4:4 + n]))
            self.stream = self.stream[4 + n:]
        return msgs

    def queue(self, msgid, body=b""):
        self.seq += 1
        self.out[self.seq] = struct.pack("<HH", len(body), msgid) + body


clients = {}          # addr -> Client
by_match = {}         # (mid, slot) -> addr
match_start = {}      # mid -> {"ready": set of slots, "started": bool}
pending200 = {}       # mid -> due time for the 0x200 user-announce (v5.5)
match_sval = {}       # mid -> u32 session value for 0x102 (v5.6)
pending300 = {}       # mid -> {slot: body} held at the 0x300 barrier (v5.8)
blob2seen = {}        # mid -> set of slots whose start blob #2 was routed (v5.12)
pending200b = {}      # mid -> due time for the post-start 0x200 re-announce (v5.12)
ghost = {}            # mid -> {"slot", "template", "counter"} (v5.14)
match_users = {}      # mid -> expected participant count from JOIN (v5.17)
announced200 = set()  # mids whose 0x200 matrix already went out (v5.17)
muted = {}            # addr -> expiry; packets dropped entirely (v5.14)
try:
    INTERNAL_KEY = open("/etc/fo3/internal.key").read().strip()
except OSError:
    INTERNAL_KEY = ""
GO_FORFEIT_URL = "http://127.0.0.1:8080/internal/relay/forfeit"
GO_JOINAUTH_URL = "http://127.0.0.1:8080/internal/relay/joinauth"
auth_cache = {}       # (mid, slot, ip) -> (ok, expiry) (v5.18)
_auth_checks = []     # timestamps of uncached joinauth calls (flood cap)
clients_lock = threading.Lock()


def joinauth(mid, slot, ip):
    """B1 (v5.18): ask Go whether (mid, slot) may be claimed from ip.

    Verdicts are cached per (mid, slot, ip) — positives for the
    join window, negatives briefly. Fail-closed: an unreachable
    or erroring Go denies the JOIN, and uncached checks are
    capped so a JOIN flood cannot serialize the packet loop.
    """
    key = (mid, slot, ip)
    now = time.time()
    hit = auth_cache.get(key)
    if hit and hit[1] > now:
        return hit[0]
    while _auth_checks and _auth_checks[0] < now - 10:
        _auth_checks.pop(0)
    if len(_auth_checks) >= 20:
        log(f"JOIN DENIED mid {mid} slot {slot} ip {ip} (joinauth flood cap)")
        auth_cache[key] = (False, now + 30)
        return False
    _auth_checks.append(now)
    ok = False
    try:
        data = json.dumps({"mid": str(mid), "slot": slot, "ip": ip}).encode()
        rq = urllib.request.Request(
            GO_JOINAUTH_URL, data=data,
            headers={"Content-Type": "application/json",
                     "X-Internal-Key": INTERNAL_KEY})
        with urllib.request.urlopen(rq, timeout=2) as resp:
            ok = bool(json.loads(resp.read().decode() or "{}").get("ok"))
    except Exception as e:
        log(f"joinauth mid {mid} slot {slot} ip {ip} failed: {e} -> deny")
    if not ok:
        log(f"JOIN DENIED mid {mid} slot {slot} ip {ip} (joinauth)")
    auth_cache[key] = (ok, now + (600 if ok else 30))
    return ok


def members_of(mid):
    """All joined clients of a match, ordered by slot (v5.17).

    A match is no longer necessarily two engines: 2v2/3v3
    sessions seat 4/6. Every mechanism that used to talk to
    "the peer" now talks to this list.
    """
    out = []
    for (m, _sl), addr in list(by_match.items()):
        if m == mid:
            cc = clients.get(addr)
            if cc is not None and cc.joined:
                out.append(cc)
    out.sort(key=lambda cc: cc.slot if cc.slot is not None else 0)
    return out


def expected_users(mid):
    """How many engines the match should seat (from the JOIN
    bodies); 2 when unknown (the historical session size)."""
    return match_users.get(mid) or 2


def peer_of(c):
    """The other joined client of the same match, if any."""
    if c.mid is None:
        return None
    for (mid, slot), addr in by_match.items():
        if mid == c.mid and addr != c.addr:
            p = clients.get(addr)
            if p is not None and p.joined:
                return p
    return None


def flush_out(sock, c):
    """(Re)send everything the client has not acked yet."""
    for q, msg in sorted(c.out.items()):
        try:
            sock.sendto(encrypt(header(RELAY_SLOT, c.slot if c.slot is not None else 0,
                                       c.mid or 0, c.ack, 1, rseq=q) + msg), c.addr)
            if q > getattr(c, "max_sent", 0):
                c.max_sent = q
                log(f"-> {c.addr} seq {q} msg {msg[2:4].hex()} ({len(msg)}B, first send)")
        except OSError as e:
            log(f"flush_out to {c.addr} seq {q} failed: {e}")


ping_counts = {}


def handle_packet(sock, data, addr):
    """Process one decoded packet; never raises."""
    if muted.get(addr, 0) > time.time():
        return  # v5.14: forfeited side / released survivor: total silence
    m = parse(decrypt(data))
    with clients_lock:
        c = clients.setdefault(addr, Client(addr))
        c.last = time.time()
        if m["type"] == 0x4005:
            n = ping_counts[addr] = ping_counts.get(addr, 0) + 1
            if n == 1 or n % 15 == 0:
                log(f"{addr} ping x{n} (mid {m['mid']}, slot {m['src']}, "
                    f"stream ack {m['ack']}, joined {c.joined})")
        if m["type"] not in (0x4005, 0x4006):
            log(f"{addr} <- src {m['src']} dst {m['dst']} mid {m['mid']} "
                f"rseq {m['rseq']} ack {m['ack']} token {m['token']} "
                f"type {m['type']:#06x} body {m['body'].hex()}")
        for msgid, body in c.take(m):
            log(f"{addr} <- msg {msgid:#06x} {body.hex()}")
            if msgid == 0x105 and 7 <= len(body) <= 64:
                c.last105 = body
            if msgid == 0x105 and len(body) > 100:
                c.big_blob = body
            if msgid == 0x100:  # JOIN {u32 matchid, u8 users, u8 slot, u8 flag}
                if len(body) >= 7:
                    jmid, _users, jslot, _flag = struct.unpack_from("<IBBB", body)
                else:
                    jmid, jslot = m["mid"], m["src"]
                # B1 (v5.18): the first JOIN of a (match, slot)
                # must come from the IP the match recorded for
                # that slot's coach; Go arbitrates via joinauth.
                # A denied JOIN is dropped silently — no token,
                # no inited; the engine waits at its loading
                # screen and the slot stays unclaimed.
                if (not c.joined or c.mid != jmid or c.slot != jslot) \
                        and not joinauth(jmid, jslot, addr[0]):
                    continue
                c.mid, c.slot = jmid, jslot
                c.joined = True
                by_match[(c.mid, c.slot)] = addr
                if len(body) >= 7 and _users:
                    match_users[c.mid] = max(match_users.get(c.mid, 0), _users)
                # Token = slot + 1, unique per session user. The
                # upstream emu hard-coded 1 for its single-client
                # tests; with two users in one session both
                # engines got token 1, and the start serializer
                # traps on both engines (dumps 2026-11-13 21:30
                # and 22:01, fifazf+0x5D5898) — if the token is
                # the session user index, identical tokens leave
                # the second user unresolvable.
                c.token = c.slot + 1
                c.queue(0x100, struct.pack("<I", c.token))
                # 0x102: session value -> client field +0xCC. The
                # field is written ONLY by this message (handler
                # case 0x102, fifazf 0x9D4F00) and is READ by a
                # periodic session sender in the same class
                # (0x9DBFxx), which appends it to a message for
                # the peer — a shared session value (seed/clock)
                # the engines cross-check while the synch runs.
                # The relay has never sent it: both engines ran
                # with +0xCC = 0 while every start round stalled
                # at the loading screen (BE_BLACKSCREEN +57s).
                # One value per match (unix seconds at first
                # JOIN), identical for both engines.
                sv = match_sval.setdefault(c.mid, int(time.time()))
                c.queue(0x102, struct.pack("<I", sv))
                c.queue(0x101)                          # inited
                log(f"{addr} JOIN mid {c.mid} slot {c.slot} -> token {c.token} + inited queued")
                mem = members_of(c.mid)
                if len(mem) >= 2:
                    log(f"match {c.mid}: {len(mem)}/{expected_users(c.mid)} "
                        f"engines joined (latest {addr} slot {c.slot})")
                if len(mem) >= expected_users(c.mid) \
                        and c.mid not in announced200 \
                        and c.mid not in pending200:
                    pending200[c.mid] = time.time() + 0.5
            elif msgid == 0x400:
                # "Loading finished, ready to play" — addressed to
                # the SESSION SERVER, never forwarded: the client
                # stream handler (fifazf 0x9D4E20, read from the
                # 2026-11-13 full memory dump) has NO case for
                # 0x400; unhandled msgids fall into a deliberate
                # trap at 0x9D5898 — that was the start crash in
                # every round through 22:23. When BOTH players of
                # the match have sent 0x400, the relay answers
                # 0x302 ("start now": the engine replies 0x303 and
                # initialises gameplay) to both.
                st = match_start.setdefault(c.mid, {"ready": set(),
                                                    "started": False})
                st["ready"].add(c.slot)
                # v5.7: announce this user's readiness to BOTH
                # engines as 0x303 {slot}. Handler case 0x303
                # forwards {value} into the game layer; case 0x302
                # builds only a LOCAL 0x303 for the receiving
                # engine itself. If the game layer's loading
                # completion counts one 0x303 per user, the peer's
                # 0x303 can only arrive via the server — the relay
                # never sent it, and every start round died at the
                # fixed +57s loading deadline (BE_BLACKSCREEN)
                # while synch frames flowed normally.
                mem = members_of(c.mid)
                for cc in mem:
                    cc.queue(0x303, bytes([c.slot]))
                    flush_out(sock, cc)
                log(f"match {c.mid}: 0x303 ready-announce slot {c.slot} sent")
                log(f"{addr} 0x400 ready (slot {c.slot}); match {c.mid} "
                    f"ready slots {sorted(st['ready'])}")
                if not st["started"] and len(mem) >= expected_users(c.mid) \
                        and all(cc.slot in st["ready"] for cc in mem):
                    st["started"] = True
                    st["sent_at"] = time.time()
                    st["sends"] = 1
                    for cc in mem:
                        cc.queue(0x302)
                        flush_out(sock, cc)
                    # v5.11: 0x202 REMOVED entirely. Verdict from
                    # two rounds: at JOIN time (v5.9) it wedged
                    # the prep phase (auto-ready loop); after
                    # 0x302 (v5.10) both engines went completely
                    # silent on receipt — not one blob or synch
                    # frame followed, one client stopped
                    # responding, and both showed the "weak
                    # connection to the match host" cancellation.
                    # Case 0x202 registers the user into the
                    # session map at +0x18, which in this session
                    # flavour is never constructed (0xCDCDCDCD
                    # in the hung-match dump): the insert
                    # corrupts the session. Never send 0x202.
                    log(f"match {c.mid}: all ready -> 0x302 sent to all")
            elif msgid == 0x300:
                # RENDEZVOUS BARRIER (v5.8). The 0x300 handshake
                # is order-fragile by design: receiving the peer's
                # 0x300 sets the receiver's +0x32D flag, and the
                # engine's own 0x300 sender (fifazf 0x9D7B70) is
                # gated on +0x32D == 0 — so whichever engine's
                # 0x300 is delivered SECOND can never send its
                # own, the peer never answers 0x301, and that
                # side's game layer stalls until the +57s loading
                # deadline (BE_BLACKSCREEN). Proven by the hung-
                # match dump 2026-11-14: GM's session object had
                # +0x32C (sent 0x300) = 0, +0x32D = 1, +0x32F = 1,
                # and the wire log shows only GM2's 0x300 and only
                # GM's answering 0x301. Instant forwarding made
                # the faster engine win the race every round.
                # Hold each 0x300 until BOTH have arrived, then
                # release both: each engine then processes the
                # peer's 0x300 with its own already sent.
                held = pending300.setdefault(c.mid, {})
                held[c.slot] = body
                log(f"{addr} 0x300 held at barrier (match {c.mid}, "
                    f"slots {sorted(held)})")
                # v5.17: the barrier covers EVERY seated engine:
                # release when all of them have checked in, each
                # receiving every OTHER engine's 0x300 (the 1v1
                # case reduces to the old both-ways release).
                mem = members_of(c.mid)
                if len(mem) >= expected_users(c.mid) \
                        and all(cc.slot in held for cc in mem):
                    for cc in mem:
                        for slot, b in held.items():
                            if slot != cc.slot:
                                cc.queue(0x300, b)
                        flush_out(sock, cc)
                    pending300.pop(c.mid, None)
                    log(f"match {c.mid}: 0x300 barrier released "
                        f"to all {len(mem)} engines")
            else:
                mem = members_of(c.mid)
                if len(mem) >= 2:
                    for cc in mem:
                        if cc is not c:
                            cc.queue(msgid, body)
                            flush_out(sock, cc)
                    log(f"route msg {msgid:#06x} {addr} -> "
                        f"{len(mem) - 1} peers ({len(body)}B)")
                    # v5.13 ECHO: also deliver the message back to
                    # its SENDER. The session synch module feeds its
                    # lockstep ring ONLY from inbound redistribution:
                    # BE_CATCHUPQUEUE telemetry from a hung match
                    # showed recvseq [2, 0] — the posting engine had
                    # the peer's start sequences (0, 1) but ZERO of
                    # its OWN, because the relay never sent its own
                    # stream back. With no echo both engines wait
                    # forever for their own inputs and the game
                    # declares BE_BLACKSCREEN at +57s even when both
                    # engines emit frames in perfect lockstep
                    # (match 600807830, both stepping, both died).
                    # The original session server redistributed
                    # every participant message to ALL participants
                    # including the sender; mirror that here.
                    c.queue(msgid, body)
                    log(f"echo msg {msgid:#06x} -> sender {addr} ({len(body)}B)")
                    flush_out(sock, c)
                    # v5.15 GHOST: track the survivor's step
                    # counter and top up fabricated ghost frames.
                    g = ghost.get(c.mid)
                    if g is not None and msgid == 0x105 \
                            and c.slot != g["slot"] and len(body) >= 7:
                        wc = int.from_bytes(body[3:7], "little")
                        if wc > g["winner_counter"]:
                            g["winner_counter"] = wc
                        pump_ghost(sock, c.mid)
                    # v5.12 bookkeeping: watch for each engine's
                    # second start blob (0x105 subtype 0x02,
                    # counter 1, >100B). When both have crossed,
                    # arm the post-start 0x200 re-announce.
                    if msgid == 0x105 and len(body) > 100 \
                            and body[0] == 0x02 \
                            and int.from_bytes(body[3:7], "little") == 1:
                        seen = blob2seen.setdefault(c.mid, set())
                        seen.add(c.slot)
                        if len(seen) >= expected_users(c.mid) \
                                and c.mid not in pending200b:
                            pending200b[c.mid] = time.time() + 1.0
                            log(f"match {c.mid}: all start blobs "
                                f"crossed; 0x200 re-announce armed")
                else:
                    log(f"no peer for msg {msgid:#06x} from {addr} (mid {c.mid})")
        if m["type"] == 0x4005:  # ping -> pong, same body
            sock.sendto(encrypt(header(RELAY_SLOT, m["src"], m["mid"],
                                       c.ack, 0x4006) + m["body"]), addr)
        if m["type"] == 0x4001:  # wants an ack -> 0x4002, empty body
            sock.sendto(encrypt(header(RELAY_SLOT, m["src"], m["mid"],
                                       c.ack, 0x4002)), addr)
        flush_out(sock, c)
        # GC clients silent for 5 minutes.
        now = time.time()
        for a in [a for a, cc in clients.items() if now - cc.last > 300]:
            cc = clients.pop(a)
            if cc.mid is not None:
                by_match.pop((cc.mid, cc.slot), None)
                if not any(m == cc.mid for (m, _s) in by_match):
                    match_start.pop(cc.mid, None)
                    match_users.pop(cc.mid, None)
                    announced200.discard(cc.mid)
                    match_sval.pop(cc.mid, None)
                    ghost.pop(cc.mid, None)
                    blob2seen.pop(cc.mid, None)
                    pending200b.pop(cc.mid, None)
                    pending300.pop(cc.mid, None)
            log(f"GC client {a}")


def tick_announce(sock):
    """v5.5 experiment: 0x200 user-announce, ~0.5s after both JOINs.

    Case 0x200 in the client handler (fifazf 0x9D4E20, from the
    2026-11-13 full dump) is the server telling an engine "user S
    is in the session": the engine registers S's session profile
    locally and answers 0x201, which the peer's handler forwards
    into its game layer as 0x201. The relay has never sent 0x200 —
    every match so far ran the synch frames (0x105) yet stalled
    at the loading screen and flagged BE_BLACKSCREEN at +57s, as
    if the game layer still waits for the opponent's profile
    delivery. Announce each user to the OTHER engine once.
    """
    now = time.time()
    for mid in [m for m, due in pending200.items() if now >= due]:
        pending200.pop(mid, None)
        mem = members_of(mid)
        if len(mem) < 2:
            continue
        announced200.add(mid)
        # v5.17: the announce is a MATRIX — every engine must
        # register every OTHER user (case 0x200 registers one
        # user per message). The 1v1 case is the 2x2 subset.
        for cc in mem:
            for other in mem:
                if other.slot != cc.slot:
                    cc.queue(0x200, bytes([other.slot]))
            flush_out(sock, cc)
        log(f"match {mid}: 0x200 user-announce matrix sent "
            f"({len(mem)} engines)")
    # v5.12: post-start re-announce. Millisecond timing from the
    # 2026-11-14 rounds shows exactly ONE engine emitting synch
    # frames on a precise 500ms timer while the other never emits
    # any; which side steps flips between rounds, and the stepper
    # is consistently the engine that had already received the
    # peer's start blobs when it sent its own second blob. The
    # silent engine appears to run a one-shot start check on its
    # next outbound stream message, which never comes. A fresh
    # 0x200 makes each engine answer 0x201 — an outbound stream
    # message — after the blob exchange, which should trip that
    # check on the silent side without changing any game data.
    for mid in [m for m, due in pending200b.items() if now >= due]:
        pending200b.pop(mid, None)
        mem = members_of(mid)
        if len(mem) < 2:
            continue
        for cc in mem:
            for other in mem:
                if other.slot != cc.slot:
                    cc.queue(0x200, bytes([other.slot]))
            flush_out(sock, cc)
        log(f"match {mid}: 0x200 RE-announce matrix sent (post-start)")


def notify_forfeit(mid, loser_slot, can_ghost):
    try:
        data = json.dumps({"mid": str(mid), "loser_slot": loser_slot,
                           "ghost": can_ghost}).encode()
        rq = urllib.request.Request(
            GO_FORFEIT_URL, data=data,
            headers={"Content-Type": "application/json",
                     "X-Internal-Key": INTERNAL_KEY})
        with urllib.request.urlopen(rq, timeout=5) as resp:
            return resp.status == 200
    except Exception as e:
        log(f"forfeit notify failed for match {mid}: {e}")
        return False


GHOST_AHEAD = 15  # fabricated frames may lead the survivor's step by at most this


def pump_ghost(sock, mid):
    """Queue fabricated ghost frames on the survivor's stream.

    v5.14 paced fabrication 1:1 off the survivor's own frames —
    but a stalled engine emits NOTHING (its sim loop blocks
    waiting for the peer's input), so not a single ghost frame
    was ever produced and the freeze persisted (match 602710141:
    after ghosting started the survivor sent only pings).
    v5.15 also produces frames from the tick, bounded so the
    ghost never leads the survivor's own step counter by more
    than GHOST_AHEAD: enough to unblock the sim, never enough
    to overrun its synch ring buffer. Once the survivor steps
    again its counter advances and production follows it.
    """
    g = ghost.get(mid)
    if g is None or g.get("blob_mode"):
        return
    n = min(g["winner_counter"] + GHOST_AHEAD - g["counter"], GHOST_AHEAD)
    if n <= 0:
        return
    for (m, sl), addr in list(by_match.items()):
        if m != mid or sl == g["slot"]:
            continue
        wc = clients.get(addr)
        if wc is None:
            return
        for _ in range(n):
            g["counter"] += 1
            frame = bytearray(g["template"])
            frame[3:7] = g["counter"].to_bytes(4, "little")
            wc.queue(0x105, bytes(frame))
        flush_out(sock, wc)
        return


def tick_forfeit(sock):
    """v5.14 quit detection + ghost takeover (user requirement).

    A started match where one engine is totally silent for 10s
    while the other was seen within 5s means that player quit or
    killed the client (in-match traffic is constant; even the
    loading phase pings every ~2s). The Go server settles the
    quitter as a 0-5 forfeit loss. When the quitter left a usable
    synch-frame template, the relay ghosts them: the survivor
    keeps playing against fabricated frames until the final
    whistle, and Go records the official 5-0 regardless of the
    played score. Without a template (quit before the first
    frame), Go settles both sides at once and both addresses are
    muted, so the survivor's frozen client is released by its
    own session watchdog (~12s without relay contact).
    """
    now = time.time()
    for a in [a for a, exp in muted.items() if now >= exp]:
        muted.pop(a, None)
    for mid in list(ghost):
        pump_ghost(sock, mid)
    if not INTERNAL_KEY:
        return
    for mid, ms in list(match_start.items()):
        if not ms.get("started"):
            continue
        if now < ms.get("notify_after", 0):
            continue
        members = members_of(mid)
        if len(members) < 2:
            continue
        multi = len(members) > 2
        if not multi and ms.get("resolved"):
            continue
        resolved_slots = ms.setdefault("resolved_slots", set())
        silent = [c for c in members
                  if now - c.last >= 10 and c.slot not in resolved_slots]
        active = [c for c in members if now - c.last <= 5]
        if not silent or not active:
            continue
        loser = silent[0]
        winner = active[0]
        can_ghost = loser.last105 is not None
        log(f"match {mid}: quit detected - slot {loser.slot} silent "
            f"{now - loser.last:.0f}s, slot {winner.slot} active; "
            f"ghost={can_ghost} multi={multi}")
        if notify_forfeit(mid, loser.slot, True if multi else can_ghost):
            resolved_slots.add(loser.slot)
            if multi:
                # v5.17 multi matches: Go settles ONLY the
                # quitter (ghost=true = loser-only settlement);
                # the match plays on for the rest. Offer the
                # synthesized end blob to every remaining
                # engine (they may hand the departed team to
                # their local AI); no ghost frames, no muting —
                # the survivors still need the relay.
                if loser.big_blob is not None and can_ghost:
                    nxt = int.from_bytes(loser.last105[3:7], "little") + 1
                    blob = bytearray(loser.big_blob)
                    blob[3:7] = nxt.to_bytes(4, "little")
                    for cc in members:
                        if cc is not loser:
                            cc.queue(0x105, bytes(blob))
                            flush_out(sock, cc)
                    log(f"match {mid}: end-blob for slot "
                        f"{loser.slot} sent to the survivors")
                continue
            ms["resolved"] = True
            if can_ghost and loser.big_blob is not None:
                # v5.16 END-BLOB: a client that quits through the
                # in-game menu emits one final big 0x105 stats blob,
                # and the peer engine — on receiving it — hands the
                # departed team to its local AI and plays on without
                # the relay (verified live 2026-11-14, match
                # 605023747: the survivor's relay frames stopped the
                # same second the blob arrived, its gameplay events
                # kept flowing). A killed client sends no blob, so
                # synthesize it from the quitter's own start blob:
                # same family (0110fb00 signature), step counter
                # patched to the next step after their last frame.
                # Fabricated frames are NOT used in this mode —
                # they would tell the engine the peer still lives.
                nxt = int.from_bytes(loser.last105[3:7], "little") + 1 \
                    if loser.last105 is not None else 1
                blob = bytearray(loser.big_blob)
                blob[3:7] = nxt.to_bytes(4, "little")
                winner.queue(0x105, bytes(blob))
                flush_out(sock, winner)
                ghost[mid] = {
                    "slot": loser.slot,
                    "blob_mode": True,
                    "winner_counter": int.from_bytes(
                        winner.last105[3:7], "little")
                    if winner.last105 is not None
                    and len(winner.last105) >= 7 else 0,
                }
                log(f"match {mid}: end-blob sent to slot "
                    f"{winner.slot} (counter {nxt})")
            elif can_ghost:
                start = int.from_bytes(loser.last105[3:7], "little")
                ghost[mid] = {
                    "slot": loser.slot,
                    "template": loser.last105,
                    "counter": start,
                    "winner_counter": int.from_bytes(
                        winner.last105[3:7], "little")
                    if winner.last105 is not None
                    and len(winner.last105) >= 7 else start,
                }
                log(f"match {mid}: ghosting slot {loser.slot} "
                    f"from counter {ghost[mid]['counter']}")
            else:
                muted[loser.addr] = now + 180
                muted[winner.addr] = now + 180
                log(f"match {mid}: no ghost template; both muted, "
                    f"survivor released by watchdog")
        else:
            ms["notify_after"] = now + 5


def tick_resend(sock):
    """Re-send 0x302 once, ~2s after the first send (kept from v5.1).

    The 0x106 experiments are CLOSED (v5.2/v5.3.1, rounds
    2026-11-13 23:11 / 23:14 / 2026-11-14 01:13 / 01:18): in
    every round where 0x106 was delivered — before 0x302, or
    1s after — both engines went completely silent on the
    relay within a second (no frames, no pings). Reading the
    client further: the 0x106 handler's transition (fifazf
    0x9DBAB0) is also invoked by an internal watchdog when the
    session server has been silent >12s (0x9CED30) — it is a
    session-finalising transition, not a start signal.
    v5.4 returns to the best-known behaviour: 0x302 only.
    """
    now = time.time()
    with clients_lock:
        for mid, st in match_start.items():
            if not st.get("started"):
                continue
            if st.get("sends", 0) >= 2:
                continue
            if now - st.get("sent_at", now) < 2.0:
                continue
            st["sends"] += 1
            for (m, _slot), addr in list(by_match.items()):
                if m != mid:
                    continue
                cc = clients.get(addr)
                if cc is not None and cc.joined:
                    cc.queue(0x302)
                    flush_out(sock, cc)
            log(f"match {mid}: 0x302 re-sent (attempt {st['sends']})")


def udp_relay():
    sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    sock.bind(("0.0.0.0", 28000))
    sock.settimeout(0.4)
    log("UDP/28000 listening (session relay v5.18)")
    while True:
        try:
            data, addr = sock.recvfrom(65536)
        except socket.timeout:
            try:
                tick_resend(sock)
                tick_announce(sock)
                tick_forfeit(sock)
            except Exception as e:  # noqa: BLE001
                log(f"tick error: {e!r}")
            continue
        except OSError:
            continue
        if len(data) < 24 or checksum(data[2:]) != int.from_bytes(data[:2], "little"):
            log(f"{addr} bad packet {data.hex()}")
            continue
        try:
            handle_packet(sock, data, addr)
        except Exception as e:  # noqa: BLE001 - one bad packet must not kill the relay
            log(f"handler error for {addr}: {e!r}")
        # The delayed sends must not depend on socket timeouts:
        # during a live match the packet stream is dense enough
        # (>4 pkt/s combined) that a 0.4s timeout may never fire —
        # in the 2026-11-13 23:14 round the scheduled 0x106 was
        # starved and never sent. Tick on every packet too.
        try:
            tick_resend(sock)
            tick_announce(sock)
            tick_forfeit(sock)
        except Exception as e:  # noqa: BLE001
            log(f"tick error: {e!r}")


def udp_log_only(port):
    sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    sock.bind(("0.0.0.0", port))
    while True:
        try:
            data, addr = sock.recvfrom(4096)
        except OSError:
            continue
        hexs = " ".join(f"{b:02x}" for b in data)
        log(f"UDP/{port} {addr[0]}:{addr[1]} {len(data)}B: {hexs}")


def tcp_log_only(port):
    srv = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    srv.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
    srv.bind(("0.0.0.0", port))
    srv.listen(64)
    while True:
        try:
            conn, addr = srv.accept()
        except OSError:
            continue
        threading.Thread(target=_tcp_conn, args=(conn, addr, port),
                         daemon=True).start()


def _tcp_conn(conn, addr, port):
    try:
        conn.settimeout(10)
        log(f"TCP/{port} CONNECT from {addr[0]}:{addr[1]}")
        while conn.recv(4096):
            pass
    except OSError:
        pass
    finally:
        conn.close()


if __name__ == "__main__":
    threading.Thread(target=udp_log_only, args=(28001,), daemon=True).start()
    threading.Thread(target=tcp_log_only, args=(28000,), daemon=True).start()
    threading.Thread(target=tcp_log_only, args=(28001,), daemon=True).start()
    udp_relay()
