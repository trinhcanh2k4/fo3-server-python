"""Notification ("MessengerConnection") server for fifazf.exe + Real-time Global Chat Push IPC.

Protocol (Blowfish-CBC encrypted frames):
    frame = <u16 LE body length> <u16 LE type=2> <body>
    body  = Blowfish-CBC(key b"kueec", IV 0) of JSON + "\\n", PKCS#5 padded
The client opens with {"cmd":"auth","sessionid":...} then pings. The UI
(AS3 MessengerConnection.handlePacket) accepts cmd auth/sign/push/push_all/ping;
auth needs "result" truthy (no "reason") to reach NOTIFICATION_SERVER_CONNECTED.

Zero external dependencies: uses pure-Python Blowfish-CBC (key schedule precomputed once).
"""
import json
import os
import socket
import socketserver
import struct
import sys
import threading
import time

_args = [a for a in sys.argv[1:] if not a.startswith("--")]
PORT = int(_args[0]) if _args else 28002
IPC_PORT = 28003
KEY = b"kueec"
LOG = os.path.join(os.path.dirname(os.path.abspath(__file__)), "captures", "messenger.log")

ACTIVE_CLIENTS = set()
CLIENTS = ACTIVE_CLIENTS
CLIENTS_LOCK = threading.Lock()
SOCKET_COACH = {}  # socket -> coachid from its signon, for targeted pushes


# Presence publishing for the Go API (its /contact/list status): the
# messenger is the only component that truly knows who is connected,
# so it mirrors its live coach set into Redis (msgonline:<coachid>),
# plus a heartbeat key the API uses to tell "messenger publishing"
# from "messenger down" (then the API falls back to last-seen).
_REDIS_PASS = None


def _redis_pass():
    global _REDIS_PASS
    if _REDIS_PASS is None:
        _REDIS_PASS = ""
        try:
            with open("/etc/fo3/redis.env", encoding="utf-8") as f:
                for line in f:
                    if line.startswith("FO3_REDIS_PASS="):
                        _REDIS_PASS = line.split("=", 1)[1].strip().strip('"').strip("'")
                        break
        except OSError:
            pass
    return _REDIS_PASS


def _redis_cmd(*args):
    try:
        s = socket.create_connection(("127.0.0.1", 6379), timeout=0.5)
        cmds = []
        pw = _redis_pass()
        if pw:
            cmds.append(("AUTH", pw))
        cmds.append(args)
        out = b""
        for c in cmds:
            out += ("*%d\r\n" % len(c)).encode()
            for a in c:
                a = str(a).encode()
                out += ("$%d\r\n" % len(a)).encode() + a + b"\r\n"
        s.sendall(out)
        s.recv(4096)
        s.close()
    except Exception:
        pass


def presence_online(coachid):
    _redis_cmd("SET", "msgonline:" + str(coachid), "1")


def presence_offline(coachid):
    with CLIENTS_LOCK:
        still = any(c == str(coachid) for c in SOCKET_COACH.values())
    if not still:
        _redis_cmd("DEL", "msgonline:" + str(coachid))


def _presence_heartbeat():
    while True:
        try:
            _redis_cmd("SETEX", "msgonline:heartbeat", "90", str(int(time.time())))
            with CLIENTS_LOCK:
                coaches = set(SOCKET_COACH.values())
            for cid in coaches:
                _redis_cmd("SET", "msgonline:" + cid, "1")
        except Exception:
            pass
        time.sleep(30)


_PRESENCE_THREAD = None


def start_presence():
    global _PRESENCE_THREAD
    if _PRESENCE_THREAD is None:
        _PRESENCE_THREAD = threading.Thread(target=_presence_heartbeat, daemon=True, name="presence")
        _PRESENCE_THREAD.start()


def _init_blowfish(key: bytes):
    """Initialize Blowfish P-array (18 words) and 4 S-boxes (256 words each) from fractional pi and key."""
    def atan_inv(x, one):
        s = t = one // x
        x2 = x * x
        k = 1
        while t:
            t //= x2
            k += 2
            s = s - t // k if (k // 2) % 2 else s + t // k
        return s

    B = 1 << (4176 * 8)
    pi_bytes = (16 * atan_inv(5, B) - 4 * atan_inv(239, B) - 3 * B).to_bytes(4176, "big")[:4168]
    words = list(struct.unpack(">1042I", pi_bytes))
    P = words[:18]
    S0, S1, S2, S3 = words[18:274], words[274:530], words[530:786], words[786:1042]

    def enc_block(L, R):
        for i in range(16):
            L ^= P[i]
            R ^= (((S0[L >> 24] + S1[(L >> 16) & 0xFF]) & 0xFFFFFFFF) ^ S2[(L >> 8) & 0xFF]) + S3[L & 0xFF] & 0xFFFFFFFF
            L, R = R, L
        L, R = R, L
        R ^= P[16]
        L ^= P[17]
        return L, R

    def dec_block(L, R):
        for i in range(17, 1, -1):
            L ^= P[i]
            R ^= (((S0[L >> 24] + S1[(L >> 16) & 0xFF]) & 0xFFFFFFFF) ^ S2[(L >> 8) & 0xFF]) + S3[L & 0xFF] & 0xFFFFFFFF
            L, R = R, L
        L, R = R, L
        R ^= P[1]
        L ^= P[0]
        return L, R

    klen = len(key)
    for i in range(18):
        w = 0
        for j in range(4):
            w = (w << 8) | key[(i * 4 + j) % klen]
        P[i] ^= w

    L = R = 0
    for i in range(0, 18, 2):
        L, R = enc_block(L, R)
        P[i], P[i + 1] = L, R
    for Sbox in (S0, S1, S2, S3):
        for i in range(0, 256, 2):
            L, R = enc_block(L, R)
            Sbox[i], Sbox[i + 1] = L, R

    return enc_block, dec_block


_ENC_BLOCK, _DEC_BLOCK = _init_blowfish(KEY)


def encode(obj):
    pt = (json.dumps(obj, ensure_ascii=False, separators=(",", ":")) + "\n").encode("utf-8")
    pad = 8 - (len(pt) % 8)
    pt += bytes([pad]) * pad
    ct = bytearray()
    pl = pr = 0
    for i in range(0, len(pt), 8):
        bl, br = struct.unpack_from(">II", pt, i)
        pl, pr = _ENC_BLOCK(bl ^ pl, br ^ pr)
        ct += struct.pack(">II", pl, pr)
    return struct.pack("<HH", len(ct), 2) + bytes(ct)


def decode(body):
    out = bytearray()
    pl = pr = 0
    for i in range(0, len(body), 8):
        cl, cr = struct.unpack_from(">II", body, i)
        dl, dr = _DEC_BLOCK(cl, cr)
        out += struct.pack(">II", dl ^ pl, dr ^ pr)
        pl, pr = cl, cr
    pad = out[-1]
    return json.loads(bytes(out[:-pad]).decode("utf-8", "replace"))


def log(line):
    os.makedirs(os.path.dirname(LOG), exist_ok=True)
    line = f"{time.strftime('%H:%M:%S')} {line}"
    print(line, flush=True)
    try:
        with open(LOG, "a", encoding="utf-8") as f:
            f.write(line + "\n")
    except Exception:
        pass


def broadcast_push(uri, body, coachid=None):
    """Encrypt and push a real-time frame to connected game clients. With a
    coachid, only that coach's connections receive it (a contact list built
    for one coach must never land in another coach's window)."""
    try:
        raw_json = json.dumps({"cmd": "push", "uri": uri, "body": body}, ensure_ascii=False, separators=(",", ":"))
        if len(raw_json.encode("utf-8")) > 65000:
            log(f"broadcast_push skipped: payload too large ({len(raw_json)} bytes) for {uri}")
            return
        pkt = encode({"cmd": "push", "uri": uri, "body": body})
    except Exception as e:
        log(f"broadcast_push encode error: {e!r}")
        return
    dead = []
    with CLIENTS_LOCK:
        if coachid:
            clients = [s for s, c in SOCKET_COACH.items()
                       if c == str(coachid) and s in ACTIVE_CLIENTS]
            if not clients:
                log(f"broadcast_push: no live connection for coach {coachid} ({uri})")
                return
        else:
            clients = list(ACTIVE_CLIENTS)
    for req in clients:
        try:
            req.sendall(pkt)
        except Exception:
            dead.append(req)
    if dead:
        _gone = []
        with CLIENTS_LOCK:
            for d in dead:
                ACTIVE_CLIENTS.discard(d)
                _c = SOCKET_COACH.pop(d, None)
                if _c:
                    _gone.append(_c)
        for _c in _gone:
            presence_offline(_c)


def replies(msg):
    """Server answers for one client message (possibly none)."""
    cmd = msg.get("cmd")
    if cmd == "auth":
        return [{"cmd": "auth", "result": True}]
    if cmd == "ping":
        return [{"cmd": "ping"}]  # "pong" -> native "unknown cmd" TCP error, client quits
    if cmd == "signon":
        return [{"cmd": "sign", "result": True}]
    return []


class H(socketserver.BaseRequestHandler):
    def handle(self):
        log(f"connect {self.client_address}")
        with CLIENTS_LOCK:
            ACTIVE_CLIENTS.add(self.request)
        buf = b""
        try:
            while True:
                data = self.request.recv(65536)
                if not data:
                    break
                buf += data
                while len(buf) >= 4:
                    ln, ty = struct.unpack_from("<HH", buf)
                    if len(buf) < 4 + ln:
                        break
                    body, buf = buf[4:4 + ln], buf[4 + ln:]
                    try:
                        msg = decode(body) if ty == 2 else {"raw": body.hex(), "type": ty}
                    except Exception as e:
                        msg = {"undecodable": body.hex(), "error": repr(e)}
                    if msg.get("cmd") == "signon" and msg.get("coachid"):
                        with CLIENTS_LOCK:
                            SOCKET_COACH[self.request] = str(msg["coachid"])
                        presence_online(msg["coachid"])
                    if msg.get("cmd") != "ping":
                        log(f"<- {msg}")
                    for out in replies(msg):
                        self.request.sendall(encode(out))
                        if out["cmd"] != "ping":
                            log(f"-> {str(out)[:300]}")
        except Exception as e:
            log(f"err {self.client_address}: {e!r}")
        finally:
            with CLIENTS_LOCK:
                ACTIVE_CLIENTS.discard(self.request)
                _cid = SOCKET_COACH.pop(self.request, None)
            if _cid:
                presence_offline(_cid)
            log(f"closed {self.client_address}")


class IPCHandler(socketserver.StreamRequestHandler):
    def handle(self):
        try:
            line = self.rfile.readline()
            if not line:
                return
            data = json.loads(line.decode("utf-8", "replace"))
            uri = data.get("uri", "/chat/my/channel/messages/latest")
            body = data.get("body", {})
            broadcast_push(uri, body, data.get("coachid"))
            self.wfile.write(b'{"ok":true}\n')
        except Exception as e:
            log(f"IPC ERR {e!r}")


class ThreadedTCPServer(socketserver.ThreadingMixIn, socketserver.TCPServer):
    allow_reuse_address = os.name != "nt"
    daemon_threads = True


def start_background(port=PORT, ipc_port=IPC_PORT):
    """Start messenger and IPC servers in background daemon threads if not already bound."""
    def _run_ipc():
        try:
            ipc_srv = ThreadedTCPServer(("127.0.0.1", ipc_port), IPCHandler)
            log(f"messenger IPC on 127.0.0.1:{ipc_port}")
            ipc_srv.serve_forever()
        except OSError:
            pass
        except Exception as e:
            log(f"IPC server error: {e!r}")

    def _run_main():
        try:
            srv = ThreadedTCPServer(("0.0.0.0", port), H)
            log(f"messenger on 0.0.0.0:{port}")
            srv.serve_forever()
        except OSError:
            pass
        except Exception as e:
            log(f"messenger server error: {e!r}")

    threading.Thread(target=_run_ipc, daemon=True, name="messenger-ipc").start()
    threading.Thread(target=_run_main, daemon=True, name="messenger-main").start()
    start_presence()


def selftest():
    m = {"cmd": "auth", "sessionid": ""}
    f = encode(m)
    ln, ty = struct.unpack_from("<HH", f)
    assert ty == 2 and ln == len(f) - 4 and ln % 8 == 0
    assert decode(f[4:]) == m
    real = bytes.fromhex("8b412a58950cd8431cb7b2e7473b61a8534a576ac50317310d047b5400622951")
    assert decode(real) == m, decode(real)
    print("messenger selftest OK")


if __name__ == "__main__":
    if "--selftest" in sys.argv:
        selftest()
    else:
        os.makedirs(os.path.dirname(LOG), exist_ok=True)
        def run_ipc():
            try:
                ipc_srv = ThreadedTCPServer(("127.0.0.1", IPC_PORT), IPCHandler)
                log(f"messenger IPC on 127.0.0.1:{IPC_PORT}")
                ipc_srv.serve_forever()
            except Exception as e:
                log(f"IPC server failed: {e!r}")
        threading.Thread(target=run_ipc, daemon=True, name="messenger-ipc").start()
        start_presence()
        print(f"messenger on 0.0.0.0:{PORT}, log -> {LOG}", flush=True)
        ThreadedTCPServer(("0.0.0.0", PORT), H).serve_forever()
