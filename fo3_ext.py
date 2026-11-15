# Note: /static/db/ITEMNAMES is served directly from responses/static_db_ITEMNAMES.json (dict of 5,208 items)
def ep_itemnames(query, body):
    return hc.from_file("/static/db/ITEMNAMES")

"""Multi-user SQLite database, Web/Launcher Auth, Global Chat, Ranked Mode & Friendly PVP extension for FO3 Server."""
from contextlib import closing
import copy
import gzip
import hashlib
import json
import os
import random
import re
import socket
import sqlite3
import threading
import time
import traceback
import urllib.parse

hc = None

# The production server's public address. install() writes this into
# hc.REDIR_HOST, which is what /info/my/services/messenger/2 hands the
# client -- a stale value here sends the client dialing a dead host and
# the boot hangs at the splash screen. That is exactly what happened on
# 2026-11-12: the cutover had sed-patched only the server's copies, the
# repo still carried the deleted VPS's 45.77.38.45, and the next repo
# deploy silently reverted the patch. On any future server migration,
# update this constant (and the http_config.py fallbacks) IN THE REPO.
DEFAULT_SERVER_IP = "203.0.113.10"

# Bug 2a: ladder start elo and division table, aligned with the real
# server's data (documented from the client's own Module.big by the
# upstream project): divisions step +50 from AMATEUR_C = 1000
# (1050 = Amateur B ... Legendary A = 1700), and a new account starts
# at LADDER_START_ELO = 1000, the bottom. An elo of exactly 0 is NOT
# usable as the start: the client's rank widget hides itself when elo
# is 0 (LadderMatchRoomScreen.onEloChanged calls setData({})), which
# the owner saw as "no rank, no points" on the versus screen.
LADDER_START_ELO = 1000

LADDER_DIVISIONS = [
    {"name": "LEGENDARY_S", "rating": 1750, "mingames": 5},
    {"name": "LEGENDARY_A", "rating": 1700, "mingames": 5},
    {"name": "LEGENDARY_B", "rating": 1650, "mingames": 5},
    {"name": "LEGENDARY_C", "rating": 1600, "mingames": 5},
    {"name": "WORLDCLASS_A", "rating": 1550, "mingames": 5},
    {"name": "WORLDCLASS_B", "rating": 1500, "mingames": 5},
    {"name": "WORLDCLASS_C", "rating": 1450, "mingames": 5},
    {"name": "PROFESSIONAL_A", "rating": 1400, "mingames": 5},
    {"name": "PROFESSIONAL_B", "rating": 1350, "mingames": 5},
    {"name": "PROFESSIONAL_C", "rating": 1300, "mingames": 5},
    {"name": "SEMIPRO_A", "rating": 1250, "mingames": 5},
    {"name": "SEMIPRO_B", "rating": 1200, "mingames": 5},
    {"name": "SEMIPRO_C", "rating": 1150, "mingames": 5},
    {"name": "AMATEUR_A", "rating": 1100, "mingames": 5},
    {"name": "AMATEUR_B", "rating": 1050, "mingames": 5},
    {"name": "AMATEUR_C", "rating": 1000, "mingames": 5},
]

COMPLETED_GUIDES = [
    "setcontroller",
    "create",
    "league",
    "recontract",
    "swap",
    "use",
]

EXTRA_ACCOUNT_FEATURES = {
    "messenger": {},
    "contact": {},
    "systemmessage": {},
    "chatting": {},
    "laddermatch": {
        "vs1": {},
        "vs2": {},
        "vs3": {},
        "roleplay3vs3": {},
        "mgr1": {},
    },
    "friendlymatch": {
        # Bonus 2 (2026-11-12): without quickmatch/custommatch the
        # friendly hub showed ONLY the manager-mode tile -- no 1v1/2v2/3v3.
        "quickmatch": {},
        "randommatch": {},
        "custommatch": {},
        "managermode": {
            "vsrandom": {},
        },
    },
    # Live Boost off (user 2026-11-12): no "boost" under livecenter, in the
    # game or the transfer market. The data side was already disabled
    # (get_liveboost_data serves an expired empty set); this was the last
    # switch still on.
    "livecenter": {},
    # The quest window's menu button (MenuScreenMediator gates BOTH the
    # quest window and the event window on freeroambuttons.quest; without
    # it the two yellow top-bar buttons never attach -- user report
    # 2026-11-12). Upstream sets the same key.
    "freeroambuttons": {
        "quest": {},
    },
    "manager2": {
        "overlay": {},
    },
    # The main menu's Team (club) button is gated by features.mainmenu.club:
    # upstream's account_base carries it, ours never did, so the button
    # stayed hidden even with the whole club system live server-side
    # (user report 2026-11-12). regionalleague (King of the Hill) is NOT
    # added: we serve no esports season behind it, its screen only shows
    # a closed season, and the user asked for it off (2026-11-12) --
    # _strip_hidden_ui removes it from accounts that already carry it.
    "mainmenu": {
        "club": {},
    },
}

def _merge_account_features(feats):
    """Merge EXTRA_ACCOUNT_FEATURES into a features dict: top level, plus
    the friendlymatch subkeys (they gate the friendly hub's mode tiles,
    and accounts created earlier already carry a friendlymatch entry, so
    a top-level-only merge never reaches them). Returns True if changed."""
    changed = False
    for k, v in EXTRA_ACCOUNT_FEATURES.items():
        if k not in feats:
            feats[k] = v
            changed = True
    for top in ("friendlymatch", "freeroambuttons", "mainmenu"):
        sub = feats.setdefault(top, {})
        for sk, sv in EXTRA_ACCOUNT_FEATURES[top].items():
            if sk not in sub:
                sub[sk] = sv
                changed = True
    return changed


def _strip_liveboost(feats):
    """Remove livecenter.boost wherever the base account files still carry
    it (the deep merge only adds keys, so the off switch needs this)."""
    lc = feats.get("livecenter")
    if isinstance(lc, dict) and "boost" in lc:
        del lc["boost"]
        return True
    return False


def _strip_hidden_ui(feats):
    """Feature keys whose UI must stay OFF (user decisions 2026-11-12):
    mainmenu.regionalleague + esports.regionalleague (King of the Hill --
    no esports season is served, the screen only shows a closed season),
    and the freeroam buttons broadcast (the TV icon) and webevent (HOT
    EVENT): clicking either errors and closes the client. The merge only
    adds keys, so removal needs this explicit strip, applied everywhere
    features are finalized.

    clubranking (2026-11-12, later): the upstream server this codebase
    derives from deliberately leaves club ranking AND club facilities
    unimplemented -- "their features are off in account_base.json" --
    and its features indeed carry no "clubranking" key while ours does
    ({playerlist, ranking, recordseason}). With the key present, the
    club-ranked proxies initialize at login and ClubFacilityProxy
    fetches /facilities on every resource change, throwing #1009 in
    updateDonationAmount() for every payload shape tried (bare map,
    wrapped, precomputed donationAmount) -- the "loi giao dien" popup
    plaguing clubbed coaches, and the likely reason the Team screen
    never opens. Removing the key follows upstream: those club
    sub-systems stay off until they are actually built."""
    changed = False
    if "clubranking" in feats:
        del feats["clubranking"]
        changed = True
    mm = feats.get("mainmenu")
    if isinstance(mm, dict) and "regionalleague" in mm:
        del mm["regionalleague"]
        changed = True
    es = feats.get("esports")
    if isinstance(es, dict) and "regionalleague" in es:
        del es["regionalleague"]
        changed = True
    fr = feats.get("freeroambuttons")
    if isinstance(fr, dict):
        for k in ("broadcast", "webevent"):
            if k in fr:
                del fr[k]
                changed = True
    return changed


CHATTING_STATICVALUES = {
    "Chatting_Ban_Count": 5,
    "Chatting_Ban_Release_Time": 60,
    "Chatting_Ban_Time": 10,
    "Chatting_Min_LV": 1,
    "Chatting_channel_First_Limit": 100,
    "Chatting_channel_Max": 100,
    "Chatting_Limit_Whisper": 100,
}

EXHIBITION_STATIC_DB = {
    "recommendteams": [1, 5, 9, 10, 11, 21, 22, 44, 45, 47, 73, 240, 241, 243],
    "staticvalues": {
        "Normal_count": 10,
        "IGR_count": 5,
        "VIP1_count": 5,
    },
}

EURO_CLUBS = [1, 5, 9, 10, 11, 21, 22, 44, 45, 47, 73, 240, 241, 243]

BOT_ENGLISH_NAMES = [
    "Alex", "David", "John", "Michael", "Daniel", "James", "Robert", "William",
    "Chris", "Oliver", "Lucas", "Henry", "Arthur", "Thomas", "George", "Edward",
    "Charles", "Jack", "Harry", "Oscar", "Liam", "Noah", "Ethan", "Mason"
]

_ACTIVE_TOKENS = {}   # token -> username
_ACTIVE_IPS = {}      # client_ip -> username
_LAST_SEEN = {}       # username -> time.time() of their last resolved request


def _is_online(uname):
    """Presence from actual traffic: the _ACTIVE_* maps never expire, so an
    account that logged in hours ago (or on another client earlier today)
    still counted as online -- an offline alt could be invited and shown in
    rooms as present (user report 2026-11-12). 150 s of silence = offline."""
    return bool(uname) and (time.time() - _LAST_SEEN.get(uname, 0) < 150)
_CURRENT_USER = threading.local()
_USER_DB_INITIALIZED = False
_LAST_ACTIVE = {"username": None, "ts": 0.0}


def _last_active_path():
    return os.path.join(hc.HERE, "gamedata", "last_active.json")


def _note_active_user(uname):
    """Persist the most recently TOKEN-authenticated user. Tokenless
    requests (much of the game client) resolve through in-memory maps
    that die on every restart, and this household's connections arrive
    over both IPv4 and IPv6, so an IP-keyed map can be cold for the
    family a given connection used -- those requests used to fall
    through to admin and act on the wrong account (cup resume ->
    "not exist cupid" for the real owner's cups, 2026-11-12). The
    last authenticated user is the right fallback on a server with
    one active player at a time."""
    if not uname or _LAST_ACTIVE["username"] == uname:
        return
    _LAST_ACTIVE["username"] = uname
    _LAST_ACTIVE["ts"] = time.time()
    try:
        with open(_last_active_path(), "w", encoding="utf-8") as f:
            json.dump(_LAST_ACTIVE, f)
    except Exception:
        pass


def _get_last_active_user():
    if _LAST_ACTIVE["username"]:
        return _LAST_ACTIVE["username"]
    try:
        with open(_last_active_path(), "r", encoding="utf-8") as f:
            data = json.load(f)
        if data.get("username"):
            _LAST_ACTIVE.update(data)
            return data["username"]
    except Exception:
        pass
    return None

GLOBAL_CHAT_CHANNELS = {
    s: {"id": s, "size": 100, "members": [], "messages": [], "seq": 0}
    for s in range(1, 101)
}
ACTIVE_ROOMS = {}
ROOM_SEQ = 1


def _user_db_path():
    return os.path.join(hc.HERE, "gamedata", "fo3_users.db")


def _user_db():
    path = _user_db_path()
    os.makedirs(os.path.dirname(path), exist_ok=True)
    conn = sqlite3.connect(path, timeout=15.0)
    conn.row_factory = sqlite3.Row
    return conn


def _default_empty_state():
    return {"coaches": [], "defaultcoachid": "", "teams": {}}


def _load_default_template_state():
    try:
        with open(hc.STATE, "r", encoding="utf-8") as f:
            return json.load(f)
    except Exception:
        return _default_empty_state()


def _coach_id_for_user(uid, slot=0):
    uid_int = int(uid or 1)
    slot_int = int(slot or 0)
    if uid_int <= 1:
        return f"65f1a2b3c4d5e6f7a8b9c{slot_int:03x}"
    return f"65f1a2b3c4d5e6f7{uid_int:04x}c{slot_int:03x}"


def _ensure_unique_coach_ids(st, uid):
    if not isinstance(st, dict):
        return st
    uid_int = int(uid or 1)
    coaches = st.get("coaches") or []
    changed = False
    for idx, c in enumerate(coaches):
        slot = int(c.get("slot", idx))
        expected_id = _coach_id_for_user(uid_int, slot)
        old_id = c.get("_id")
        # Legacy ids that are NOT unique to this user: a missing id, the
        # uid-1 65f1... family on another user's coach, and the shared
        # zero-padded slot default hc.coach_id(slot) ("000...001") that
        # coach creation stamps on EVERY account. That shared default
        # made every coach in the game share one id, so the contact
        # system treated any real coach as "yourself" and adding a
        # friend always failed with cannot-add-self.
        legacy = (not old_id) or old_id == hc.coach_id(slot) or (
            uid_int > 1 and str(old_id).startswith("65f1a2b3c4d5e6f7a8b9c"))
        if legacy and old_id != expected_id:
            c["_id"] = expected_id
            if st.get("defaultcoachid") == old_id or not st.get("defaultcoachid"):
                st["defaultcoachid"] = expected_id
            changed = True
    if coaches and not st.get("defaultcoachid"):
        st["defaultcoachid"] = coaches[0].get("_id", _coach_id_for_user(uid_int, 0))
    return st


def _init_user_db():
    global _USER_DB_INITIALIZED
    if _USER_DB_INITIALIZED and os.path.isfile(_user_db_path()):
        return
    with closing(_user_db()) as db:
        db.execute("""
            CREATE TABLE IF NOT EXISTS users (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                username TEXT UNIQUE NOT NULL,
                password_hash TEXT NOT NULL,
                token TEXT,
                last_ip TEXT,
                coach_name TEXT DEFAULT '',
                elo INTEGER DEFAULT 1000,
                wins INTEGER DEFAULT 0,
                losses INTEGER DEFAULT 0,
                draws INTEGER DEFAULT 0,
                state_json TEXT NOT NULL,
                created_at INTEGER NOT NULL
            )
        """)
        db.execute("CREATE INDEX IF NOT EXISTS idx_users_token ON users(token)")
        db.execute("CREATE INDEX IF NOT EXISTS idx_users_last_ip ON users(last_ip)")
        db.execute("""
            CREATE TABLE IF NOT EXISTS friends (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                coach_id TEXT NOT NULL,
                friend_id TEXT NOT NULL,
                friend_name TEXT NOT NULL,
                group_name TEXT DEFAULT NULL,
                status TEXT DEFAULT 'accepted',
                created_at TEXT NOT NULL,
                UNIQUE(coach_id, friend_id)
            )
        """)
        db.execute("CREATE INDEX IF NOT EXISTS idx_friends_coach ON friends(coach_id)")

        legacy_users_file = os.path.join(hc.HERE, "gamedata", "users.json")
        if os.path.isfile(legacy_users_file):
            try:
                with open(legacy_users_file, "r", encoding="utf-8") as f:
                    legacy_users = json.load(f)
                for uname, uinfo in (legacy_users or {}).items():
                    exists = db.execute("SELECT 1 FROM users WHERE username = ?", (uname,)).fetchone()
                    if not exists:
                        legacy_state_file = os.path.join(hc.HERE, "gamedata", f"state_{uname}.json")
                        if os.path.isfile(legacy_state_file):
                            with open(legacy_state_file, "r", encoding="utf-8") as sf:
                                st_obj = json.load(sf)
                        else:
                            st_obj = _default_empty_state()
                        coaches = st_obj.get("coaches") or []
                        cname = coaches[0].get("name", "") if coaches else ""
                        rk = st_obj.get("ranked") or {}
                        db.execute("""
                            INSERT INTO users (username, password_hash, token, last_ip, coach_name, elo, wins, losses, draws, state_json, created_at)
                            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                        """, (
                            uname,
                            uinfo.get("password_hash", ""),
                            uinfo.get("token", ""),
                            uinfo.get("last_ip", ""),
                            cname,
                            int(rk.get("elo", LADDER_START_ELO)),
                            int(rk.get("wins", 0)),
                            int(rk.get("losses", 0)),
                            int(rk.get("draws", 0)),
                            json.dumps(st_obj),
                            int(uinfo.get("created_at", time.time())),
                        ))
            except Exception:
                pass

        cnt = db.execute("SELECT COUNT(*) FROM users").fetchone()[0]
        if cnt == 0 and os.path.isfile(hc.STATE):
            try:
                st_obj = _load_default_template_state()
                coaches = st_obj.get("coaches") or []
                cname = coaches[0].get("name", "") if coaches else ""
                rk = st_obj.get("ranked") or {}
                db.execute("""
                    INSERT INTO users (username, password_hash, token, last_ip, coach_name, elo, wins, losses, draws, state_json, created_at)
                    VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                """, (
                    "admin",
                    hashlib.sha256(b"admin123").hexdigest(),
                    "LOCAL-SIGNED-TOKEN",
                    "127.0.0.1",
                    cname,
                    int(rk.get("elo", LADDER_START_ELO)),
                    int(rk.get("wins", 0)),
                    int(rk.get("losses", 0)),
                    int(rk.get("draws", 0)),
                    json.dumps(st_obj),
                    int(time.time()),
                ))
            except Exception:
                pass
        db.commit()
    _USER_DB_INITIALIZED = True


def _get_active_username():
    return getattr(_CURRENT_USER, "username", None)


def list_db_users():
    """Return list of all users in SQLite database for Admin panel dropdown."""
    _init_user_db()
    out = []
    with closing(_user_db()) as db:
        rows = db.execute("""
            SELECT id, username, coach_name, elo, wins, losses, draws, last_ip, created_at, state_json
            FROM users ORDER BY id ASC
        """).fetchall()
        for r in rows:
            cname = r["coach_name"] or ""
            ep_val = 0
            try:
                st_obj = json.loads(r["state_json"] or "{}")
                coaches = st_obj.get("coaches") or []
                if coaches:
                    cname = coaches[0].get("name") or cname
                    ep_val = int(coaches[0].get("ep", 0))
            except Exception:
                pass
            out.append({
                "id": r["id"],
                "username": r["username"],
                "coach_name": cname or "(Chưa tạo HLV)",
                "ep": ep_val,
                "elo": r["elo"],
                "wins": r["wins"],
                "losses": r["losses"],
                "draws": r["draws"],
                "last_ip": r["last_ip"] or "",
            })
    return out


def _state(username=None):
    _init_user_db()
    uname = username if username is not None else _get_active_username()
    if uname and uname != "__default__":
        with closing(_user_db()) as db:
            row = db.execute("SELECT id, state_json FROM users WHERE username = ?", (uname,)).fetchone()
            if row and row["state_json"]:
                try:
                    st_obj = json.loads(row["state_json"])
                    return _ensure_unique_coach_ids(st_obj, row["id"])
                except Exception:
                    return _default_empty_state()
        return _default_empty_state()
    try:
        with open(hc.STATE, "r", encoding="utf-8") as f:
            return json.load(f)
    except FileNotFoundError:
        return _default_empty_state()


def _save(st, username=None):
    _init_user_db()
    uname = username if username is not None else _get_active_username()
    if uname and uname != "__default__":
        with closing(_user_db()) as db:
            u_row = db.execute("SELECT id FROM users WHERE username = ?", (uname,)).fetchone()
            if u_row:
                _ensure_unique_coach_ids(st, u_row["id"])
            coaches = st.get("coaches") or []
            cname = coaches[0].get("name", "") if coaches else ""
            rk = st.get("ranked") or {}
            elo = int(rk.get("elo", LADDER_START_ELO))
            wins = int(rk.get("wins", 0))
            losses = int(rk.get("losses", 0))
            draws = int(rk.get("draws", 0))
            cur = db.execute("""
                UPDATE users
                SET state_json = ?, coach_name = ?, elo = ?, wins = ?, losses = ?, draws = ?
                WHERE username = ?
            """, (json.dumps(st), cname, elo, wins, losses, draws, uname))
            if cur.rowcount == 0:
                db.execute("""
                    INSERT INTO users (username, password_hash, token, last_ip, coach_name, elo, wins, losses, draws, state_json, created_at)
                    VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                """, (uname, hashlib.sha256(uname.encode()).hexdigest(), "", "", cname, elo, wins, losses, draws, json.dumps(st), int(time.time())))
            db.commit()
        return
    os.makedirs(os.path.dirname(hc.STATE), exist_ok=True)
    with open(hc.STATE, "w", encoding="utf-8") as f:
        json.dump(st, f)


def ep_my_ip(query, body):
    return hc._json({"result": True, "ipaddress": DEFAULT_SERVER_IP})


def ep_garena_sign(query, body):
    _init_user_db()
    token = ""
    try:
        if body:
            data = json.loads(body.decode("utf-8", errors="ignore"))
            token = str(data.get("token") or data.get("key") or "").strip()
    except Exception:
        pass
    if not token and query:
        qs = dict(urllib.parse.parse_qsl(query))
        token = str(qs.get("token") or qs.get("key") or "").strip()

    uid = 1
    username = getattr(_CURRENT_USER, "username", None)
    client_ip = getattr(_CURRENT_USER, "ip", None) or "127.0.0.1"
    with closing(_user_db()) as db:
        row = None
        if token:
            row = db.execute("SELECT id, username FROM users WHERE token = ?", (token,)).fetchone()
            if not row and token.startswith("FO3-"):
                parts = token.split("-")
                if len(parts) >= 2:
                    row = db.execute("SELECT id, username FROM users WHERE username = ?", (parts[1],)).fetchone()
        if not row and username:
            row = db.execute("SELECT id, username FROM users WHERE username = ?", (username,)).fetchone()
        if not row and client_ip in _ACTIVE_IPS:
            active_u = _ACTIVE_IPS[client_ip]
            row = db.execute("SELECT id, username FROM users WHERE username = ?", (active_u,)).fetchone()
        if not row:
            row = db.execute("SELECT id, username FROM users WHERE username = 'admin'").fetchone()
        if not row:
            row = db.execute("SELECT id, username FROM users ORDER BY id ASC LIMIT 1").fetchone()
        if row:
            uid, username = row[0], row[1]
            _CURRENT_USER.username = username
            if not token:
                token = f"FO3-{username}-{uid}"
            _ACTIVE_TOKENS[token] = username
            if client_ip:
                _ACTIVE_IPS[client_ip] = username
                db.execute("UPDATE users SET last_ip = ? WHERE id = ?", (client_ip, uid))
                db.commit()
    return hc._json({"result": "ok", "token": token or "LOCAL-SIGNED-TOKEN", "uid": uid})


# =========================================================================== #
# 1. WEB REGISTRATION & LAUNCHER AUTHENTICATION (SQLite Database)             #
# =========================================================================== #

WEB_REGISTER_HTML = """<!DOCTYPE html>
<html lang="vi">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>FIFA Online 3 - Đăng Ký & Quản Lý Tài Khoản</title>
<style>
  * { box-sizing: border-box; margin: 0; padding: 0; }
  body {
    font-family: 'Segoe UI', Tahoma, Geneva, Verdana, sans-serif;
    background: radial-gradient(circle at top, #1b2838 0%, #0d131a 100%);
    color: #e8eef4;
    min-height: 100vh;
    display: flex;
    align-items: center;
    justify-content: center;
    padding: 20px;
  }
  .card {
    background: rgba(26, 38, 52, 0.96);
    border: 1px solid #324a63;
    border-top: 3px solid #f2c200;
    border-radius: 8px;
    width: 100%;
    max-width: 450px;
    padding: 28px 30px;
    box-shadow: 0 20px 50px rgba(0,0,0,0.65);
  }
  h1 {
    font-size: 22px;
    color: #f2c200;
    text-align: center;
    margin-bottom: 6px;
    letter-spacing: 0.5px;
  }
  p.sub {
    text-align: center;
    color: #91a4b6;
    font-size: 13px;
    margin-bottom: 20px;
  }
  .tabs {
    display: flex;
    gap: 8px;
    margin-bottom: 18px;
  }
  .tab {
    flex: 1;
    padding: 9px;
    background: #131d27;
    border: 1px solid #2c4054;
    color: #91a4b6;
    border-radius: 4px;
    cursor: pointer;
    font-weight: 600;
    font-size: 13px;
  }
  .tab.active {
    background: #223548;
    color: #f2c200;
    border-color: #f2c200;
  }
  .field { margin-bottom: 15px; }
  label {
    display: block;
    font-size: 13px;
    color: #b0c2d4;
    margin-bottom: 6px;
  }
  input {
    width: 100%;
    padding: 10px 12px;
    background: #111a24;
    border: 1px solid #324a63;
    border-radius: 4px;
    color: #fff;
    font-size: 15px;
  }
  input:focus {
    outline: none;
    border-color: #f2c200;
  }
  button.submit {
    width: 100%;
    padding: 12px;
    background: linear-gradient(180deg, #ffd426 0%, #e0af00 100%);
    border: none;
    border-radius: 4px;
    color: #141000;
    font-size: 15px;
    font-weight: 700;
    cursor: pointer;
    margin-top: 6px;
  }
  button.submit:hover { filter: brightness(1.06); }
  .msg {
    margin-top: 16px;
    padding: 11px 13px;
    border-radius: 4px;
    font-size: 13.5px;
    display: none;
    line-height: 1.45;
  }
  .msg.ok { display: block; background: rgba(47, 143, 58, 0.25); border: 1px solid #48b856; color: #9fe06a; }
  .msg.err { display: block; background: rgba(212, 72, 62, 0.25); border: 1px solid #d4483e; color: #ffb4ad; }
  .info {
    margin-top: 20px;
    padding-top: 14px;
    border-top: 1px solid #283a4d;
    font-size: 12.5px;
    color: #8499ad;
    line-height: 1.55;
  }
  code { color: #f2c200; }
</style>
</head>
<body>
  <div class="card">
    <h1>FIFA ONLINE 3 SERVER</h1>
    <p class="sub">Hệ thống Tài Khoản & Xếp Hạng (SQLite Multi-User)</p>
    <div class="tabs">
      <button type="button" class="tab active" id="tabReg" onclick="switchMode('register')">ĐĂNG KÝ MỚI</button>
      <button type="button" class="tab" id="tabLog" onclick="switchMode('login')">ĐĂNG NHẬP / KÍCH HOẠT IP</button>
    </div>
    <form id="authForm">
      <div class="field">
        <label>Tên tài khoản (3 - 24 ký tự chữ hoặc số)</label>
        <input type="text" id="username" required minlength="3" maxlength="24" placeholder="Nhập username...">
      </div>
      <div class="field">
        <label>Mật khẩu</label>
        <input type="password" id="password" required minlength="3" placeholder="Nhập mật khẩu...">
      </div>
      <div class="field" id="confirmBox">
        <label>Nhập lại mật khẩu</label>
        <input type="password" id="confirm" minlength="3" placeholder="Xác nhận mật khẩu...">
      </div>
      <button type="submit" class="submit" id="submitBtn">ĐĂNG KÝ TÀI KHOẢN</button>
      <div id="msg" class="msg"></div>
    </form>
    <div class="info">
      • Mỗi tài khoản có đội hình, HLV, EP, vật phẩm và điểm xếp hạng (ELO) <strong>độc lập hoàn toàn</strong> trong cơ sở dữ liệu.<br>
      • Sau khi đăng ký xong, mở <code>FO3Launcher.exe</code> trong thư mục game để đăng nhập và vào thẳng FIFA Online 3.
    </div>
  </div>
<script>
let currentMode = 'register';
function switchMode(m) {
  currentMode = m;
  document.getElementById('tabReg').className = 'tab' + (m === 'register' ? ' active' : '');
  document.getElementById('tabLog').className = 'tab' + (m === 'login' ? ' active' : '');
  document.getElementById('confirmBox').style.display = m === 'register' ? 'block' : 'none';
  document.getElementById('submitBtn').textContent = m === 'register' ? 'ĐĂNG KÝ TÀI KHOẢN' : 'ĐĂNG NHẬP & LIÊN KẾT IP';
  document.getElementById('msg').className = 'msg';
}
document.getElementById('authForm').addEventListener('submit', async (e) => {
  e.preventDefault();
  const u = document.getElementById('username').value.trim();
  const p = document.getElementById('password').value;
  const c = document.getElementById('confirm').value;
  const m = document.getElementById('msg');
  if (currentMode === 'register' && p !== c) {
    m.className = 'msg err';
    m.textContent = 'Mật khẩu xác nhận không khớp!';
    return;
  }
  try {
    const endpoint = currentMode === 'register' ? '/api/register' : '/launcher/login';
    const r = await fetch(endpoint, {
      method: 'POST',
      headers: {'Content-Type': 'application/json'},
      body: JSON.stringify({username: u, password: p, action: currentMode})
    });
    const j = await r.json();
    if (j.result) {
      m.className = 'msg ok';
      m.textContent = j.message || 'Thành công! Bạn có thể mở FO3Launcher.exe để vào game ngay.';
      if (currentMode === 'register') document.getElementById('authForm').reset();
    } else {
      m.className = 'msg err';
      m.textContent = j.message || 'Thao tác thất bại!';
    }
  } catch (err) {
    m.className = 'msg err';
    m.textContent = 'Lỗi kết nối máy chủ: ' + err.message;
  }
});
</script>
</body>
</html>
"""


def ep_web_register_page(query, body):
    return ("text/html; charset=utf-8", WEB_REGISTER_HTML.encode("utf-8"))


def _handle_user_auth(action_default, query, body):
    _init_user_db()
    try:
        req = json.loads(body.decode("utf-8")) if body else {}
    except Exception:
        req = {}
    username = str(req.get("username") or "").strip().lower()
    password = str(req.get("password") or "").strip()
    action = str(req.get("action") or action_default).strip().lower()
    client_ip = getattr(_CURRENT_USER, "ip", None) or "127.0.0.1"

    if not username or not re.fullmatch(r"[a-z0-9_]{3,24}", username):
        return hc._json({"result": False, "message": "Tên tài khoản phải từ 3-24 ký tự (chữ thường, số, dấu gạch dưới)."})
    if len(password) < 3:
        return hc._json({"result": False, "message": "Mật khẩu phải có ít nhất 3 ký tự."})

    pw_hash = hashlib.sha256(password.encode("utf-8")).hexdigest()

    with closing(_user_db()) as db:
        row = db.execute("SELECT id, username, password_hash FROM users WHERE username = ?", (username,)).fetchone()
        if action == "register":
            if row:
                return hc._json({"result": False, "message": "Tài khoản này đã tồn tại!"})
            token = hashlib.sha256(f"{username}:{time.time()}:{random.random()}".encode("utf-8")).hexdigest()[:32]
            init_st = _default_empty_state()
            cur = db.execute("""
                INSERT INTO users (username, password_hash, token, last_ip, coach_name, elo, wins, losses, draws, state_json, created_at)
                VALUES (?, ?, ?, ?, '', ?, 0, 0, 0, ?, ?)
            """, (username, pw_hash, token, client_ip, LADDER_START_ELO, json.dumps(init_st), int(time.time())))
            uid = cur.lastrowid
            db.commit()
            _ACTIVE_TOKENS[token] = username
            _ACTIVE_IPS[client_ip] = username
            _CURRENT_USER.username = username
            return hc._json({
                "result": True,
                "message": f"Đăng ký thành công tài khoản '{username}'! Hãy đăng nhập trên FO3Launcher.exe để vào game.",
                "token": token,
                "uid": uid,
                "username": username,
            })

        # Login flow
        if not row:
            return hc._json({"result": False, "message": "Tài khoản không tồn tại! Vui lòng đăng ký trước."})
        if row["password_hash"] != pw_hash:
            return hc._json({"result": False, "message": "Mật khẩu không chính xác!"})

        uid = row["id"]
        token = hashlib.sha256(f"{username}:{time.time()}:{random.random()}".encode("utf-8")).hexdigest()[:32]
        db.execute("UPDATE users SET token = ?, last_ip = ? WHERE id = ?", (token, client_ip, uid))
        db.commit()
        _ACTIVE_TOKENS[token] = username
        _ACTIVE_IPS[client_ip] = username
        _CURRENT_USER.username = username
        if client_ip in ("127.0.0.1", "localhost", "::1"):
            real_game_ip = "127.0.0.1"
        else:
            real_game_ip = hc.REDIR_HOST or os.environ.get("FO3_SERVER_IP", "203.0.113.10")
        return hc._json({
            "result": True,
            "message": f"Đăng nhập thành công ({username})! Đang khởi động FIFA Online 3...",
            "token": token,
            "uid": uid,
            "username": username,
            "real_ip": real_game_ip,
            "game_port": 28000,
            "noti_port": 28001,
        })


def ep_api_register(query, body):
    return _handle_user_auth("register", query, body)


def ep_launcher_login(query, body):
    return _handle_user_auth("login", query, body)


def ep_launcher_status(query, body):
    _init_user_db()
    with closing(_user_db()) as db:
        cnt = db.execute("SELECT COUNT(*) FROM users").fetchone()[0]
    return hc._json({"result": True, "online": True, "accounts": cnt, "server_ip": DEFAULT_SERVER_IP})


# =========================================================================== #
# 2. GLOBAL CHAT AT MAIN SCREEN (CHAT TỔNG / CHAT THẾ GIỚI)                   #
# =========================================================================== #

def _slim_team(team_obj):
    """Strip bulky staticdata from players in team objects to ensure IPC frames stay < 15KB."""
    if not isinstance(team_obj, dict):
        return team_obj
    t = copy.deepcopy(team_obj)
    for p in t.get("players") or []:
        if isinstance(p, dict):
            p.pop("staticdata", None)
    return t


def notify_messenger_push(uri, body_dict, coachid=None):
    """Send real-time push notification to messenger.py TCP IPC on 127.0.0.1:28003.
    With coachid, only that coach's messenger connections receive it.
    Returns True if delivered to messenger IPC (or in-process), False otherwise."""
    try:
        payload_bytes = json.dumps({"uri": uri, "body": body_dict,
                                    **({"coachid": str(coachid)} if coachid else {})},
                                   ensure_ascii=False).encode("utf-8")
        if len(payload_bytes) > 55000 and isinstance(body_dict, dict):
            b_copy = copy.deepcopy(body_dict)
            for side_k in ("side", "left", "right"):
                side_val = b_copy.get(side_k)
                if isinstance(side_val, dict):
                    for sub_k, sub_v in side_val.items():
                        if isinstance(sub_v, dict) and "team" in sub_v:
                            sub_v["team"] = _slim_team(sub_v["team"])
                        elif isinstance(sub_v, dict) and "players" in sub_v:
                            for pl in sub_v.get("players") or []:
                                if isinstance(pl, dict):
                                    pl.pop("staticdata", None)
                                    if "team" in pl:
                                        pl["team"] = _slim_team(pl["team"])
            body_dict = b_copy
            payload_bytes = json.dumps({"uri": uri, "body": body_dict,
                                        **({"coachid": str(coachid)} if coachid else {})},
                                       ensure_ascii=False).encode("utf-8")

        with socket.create_connection(("127.0.0.1", 28003), timeout=0.35) as s:
            s.sendall(payload_bytes + b"\n")
            return True
    except Exception:
        pass
    try:
        import messenger as _m
        if _m.CLIENTS:
            _m.broadcast_push(uri, body_dict, coachid)
            return True
    except Exception:
        pass
    return False


def _chat_coach_summary(st):
    c = hc.my_coach(st)
    if not c:
        return {"id": hc.coach_id(0), "name": "Manager", "level": 1, "teamid": 1}
    lvl = hc.coach_levelinfo(c).get("level", 1)
    return {
        "id": c.get("_id") or hc.coach_id(c.get("slot", 0)),
        "name": c.get("name") or "Manager",
        "level": lvl,
        "teamid": int(c.get("teamid") or 1),
    }


def _ensure_in_channel(ch_num, coach_info):
    try:
        ch_num = max(1, min(100, int(ch_num)))
    except Exception:
        ch_num = 1
    if ch_num not in GLOBAL_CHAT_CHANNELS:
        GLOBAL_CHAT_CHANNELS[ch_num] = {"id": ch_num, "size": 100, "members": [], "messages": [], "seq": 0}
    ch = GLOBAL_CHAT_CHANNELS[ch_num]
    for mch in GLOBAL_CHAT_CHANNELS.values():
        mch["members"] = [m for m in mch["members"] if m["id"] != coach_info["id"]]
    ch["members"].append(coach_info)
    if not ch["messages"]:
        ch["seq"] += 1
        ch["messages"].append({
            "type": "chat",
            "coachid": "000000000000000000000000",
            "coachname": "HỆ THỐNG",
            "message": f"Chào mừng đến Kênh Chat Thế Giới #{ch_num} - Server FO3 Online!",
            "hint": {"linkinfo": []},
            "time": int(time.time()),
        })
    return ch


def ep_chat_channel_auto(query, body):
    """POST /chat/channel (createChannel) or DELETE /chat/channel (leaveChannel)."""
    try:
        req = json.loads(body.decode("utf-8")) if body else {}
    except Exception:
        req = {}
    if isinstance(req, dict) and req.get("_method") == "DELETE":
        return hc._json({"result": True, "channelid": 0, "reason": ""})
    st = _state()
    coach_info = _chat_coach_summary(st)
    ch = _ensure_in_channel(int(st.get("chat_channel") or 1), coach_info)
    st["chat_channel"] = ch["id"]
    _save(st)
    return hc._json({
        "result": True,
        "channelid": ch["id"],
        "created": False,
        "reason": "",
        "id": ch["id"],
        "size": ch["size"],
        "members": ch["members"],
    })


def ep_chat_channel_join(ch_id, query, body):
    """PUT /chat/channel/<id> (changeChannel)."""
    st = _state()
    coach_info = _chat_coach_summary(st)
    ch = _ensure_in_channel(int(ch_id), coach_info)
    st["chat_channel"] = ch["id"]
    _save(st)
    return hc._json({
        "result": True,
        "channelid": ch["id"],
        "created": False,
        "reason": "",
        "id": ch["id"],
        "size": ch["size"],
        "members": ch["members"],
    })


def ep_chat_send_message(query, body):
    """POST /chat/my/channel/message: ChattingController.sendChannelChatMessage."""
    st = _state()
    coach_info = _chat_coach_summary(st)
    ch_num = int(st.get("chat_channel") or 1)
    ch = GLOBAL_CHAT_CHANNELS.get(ch_num) or _ensure_in_channel(ch_num, coach_info)
    try:
        req = json.loads(body.decode("utf-8")) if body else {}
    except Exception:
        req = {}
    msg_text = str(req.get("message") or "").strip()
    hint = req.get("hint") if isinstance(req.get("hint"), dict) else {"linkinfo": []}
    if not msg_text:
        return hc._json({"result": True})

    ch["seq"] += 1
    msg_vo = {
        "type": "chat",
        "coachid": coach_info["id"],
        "coachname": coach_info["name"],
        "message": msg_text[:200],
        "hint": hint,
        "time": int(time.time()),
    }
    ch["messages"].append(msg_vo)
    if len(ch["messages"]) > 60:
        ch["messages"] = ch["messages"][-60:]

    notify_messenger_push("/chat/my/channel/messages/latest", msg_vo)
    return hc._json({"result": True})


def ep_chat_channel_latest(query, body):
    """GET /chat/my/channel/messages/latest: MessagingGroupProxy expects a single ChatMessageVO dict."""
    st = _state()
    coach_info = _chat_coach_summary(st)
    ch_num = int(st.get("chat_channel") or 1)
    ch = _ensure_in_channel(ch_num, coach_info)
    latest = ch["messages"][-1] if ch["messages"] else {
        "type": "chat",
        "coachid": "000000000000000000000000",
        "coachname": "HỆ THỐNG",
        "message": f"Chào mừng đến Kênh Chat Thế Giới #{ch['id']}!",
        "hint": {"linkinfo": []},
        "time": int(time.time()),
    }
    return hc._json(latest)


def ep_chat_whisper_latest(query, body):
    return hc._json({
        "type": "whisper",
        "coachid": "",
        "coachname": "",
        "message": "",
        "hint": {"linkinfo": []},
        "time": int(time.time()),
    })


def ep_chat_coach_detail(cid, query, body):
    st = _state()
    c = hc.my_coach(st) or {"name": "Manager", "teamid": 1}
    lvl = hc.coach_levelinfo(c).get("level", 1) if c else 1
    return hc._json({
        "result": True,
        "id": cid,
        "_id": cid,
        "name": c.get("name", "Manager"),
        "coachname": c.get("name", "Manager"),
        "teamid": int(c.get("teamid") or 1),
        "level": lvl,
        "clubname": "",
    })


# =========================================================================== #
# 3. RANKED MODE (ĐÁ XẾP HẠNG / LADDER MATCH)                                 #
# =========================================================================== #

def _division_for_elo(elo):
    elo = LADDER_START_ELO if elo is None else int(elo)
    for idx, d in enumerate(LADDER_DIVISIONS):
        if elo >= d["rating"]:
            return d["name"], idx + 1
    return "AMATEUR_C", 16


def _fresh_ranked_state():
    return {
        "elo": LADDER_START_ELO,
        "highelo": LADDER_START_ELO,
        "wins": 0,
        "losses": 0,
        "draws": 0,
        "streak": 0,
        "season": 202611,
        "reward_checked": True,
        "results": [],
        "forfeits": 0,
        "weekwins": {},
        "recent": [],
        "elos": [],
    }


def _get_ranked_state(st):
    """The Ranked 1v1 record (ladder index 0)."""
    return st.setdefault("ranked", _fresh_ranked_state())


def _get_mgr_ranked_state(st):
    """The manager/simulated ranked record (ladder index 3). Bug 2a-2:
    it used to share the 1v1 record, so the sim mode showed the 1v1
    elo/W-L/placement; each mode now keeps its own record."""
    return st.setdefault("ranked_mgr", _fresh_ranked_state())


def _ranked_record_for_match(st, match):
    """The ranked record a finished match belongs to, by its client
    match type (Ranked_MGR_PvP_1v1 -> the manager record)."""
    if "mgr" in str((match or {}).get("type") or "").lower():
        return _get_mgr_ranked_state(st)
    return _get_ranked_state(st)


def _ladder_mode_record(rk, rank=0):
    """One mode's record for /ladder/my/info, built from a ranked state.
    rank = my board position in that mode (0 = unplayed); it used to be
    hard-coded 1, so every coach showed "Thu hang 1" (user 2026-11-12)."""
    elo = int(rk.get("elo", LADDER_START_ELO))
    highelo = max(elo, int(rk.get("highelo", elo)))
    div_name, div_num = _division_for_elo(elo)
    wins = int(rk.get("wins", 0))
    draws = int(rk.get("draws", 0))
    losses = int(rk.get("losses", 0))
    total = wins + draws + losses
    return {
        "elo": elo,
        "highelo": highelo,
        "division": {"name": f"DIVISION_{div_name}", "rank": div_num},
        "rank": rank,
        "ranking": rank,
        "wins": wins,
        "draws": draws,
        "losses": losses,
        "forfeits": int(rk.get("forfeits", 0)),
        "winningstreak": int(rk.get("streak", 0)),
        # Bug 2a: the placement ("phan hang") progress is games vs mingames;
        # the mode record must carry both or the client shows 0/5 forever.
        "games": total,
        "mingames": 5,
        "winrate": int(round((wins * 100.0) / total)) if total > 0 else 0,
        # Bug 2a (round 4): the front window's placement readout
        # (LadderMatchFrontRecordWindow.setRankedMatchRecord, "Placement
        # match x/5") is driven by firstround: a win flag per placement
        # match plus the before/after elo. It was missing entirely.
        "firstround": {
            "ongoing": total < 5,
            "wins": [r == 1 for r in (rk.get("results") or [])[:5]],
            "beforeelo": LADDER_START_ELO,
            "afterelo": elo,
        },
    }


def _legacy_mode_record(rk):
    """The legacy flat mode record (division as a string) from a ranked state."""
    elo = int(rk.get("elo", LADDER_START_ELO))
    highelo = max(elo, int(rk.get("highelo", elo)))
    div_name, div_num = _division_for_elo(elo)
    best_div_name, best_div_num = _division_for_elo(highelo)
    wins = int(rk.get("wins", 0))
    draws = int(rk.get("draws", 0))
    losses = int(rk.get("losses", 0))
    total = wins + draws + losses
    return {
        "elo": elo,
        "highelo": highelo,
        "division": f"DIVISION_{div_name}",
        "divisionnum": div_num,
        "bestdivision": f"DIVISION_{best_div_name}",
        "bestdivisionnum": best_div_num,
        "wins": wins,
        "draws": draws,
        "losses": losses,
        "forfeits": int(rk.get("forfeits", 0)),
        "win": wins,
        "draw": draws,
        "lose": losses,
        "games": total,
        "mingames": 5,
        "winrate": int(round((wins * 100.0) / total)) if total > 0 else 0,
        "ranking": 1,
        "rank": 1,
        "rankpercent": 1,
        "winningstreak": int(rk.get("streak", 0)),
        "points": wins * 3 + draws,
    }


def _recent_served(rk):
    """One mode's season recentmatches in the client's record VO --
    {date, goals, goalsconceded, shootout, shootoutconceded, streak,
    elodiff, elo, forfeits}, oldest first, last 3 (upstream's proven
    shape; the record screen's history table binds exactly these).

    New entries are stored in this shape by ep_match_end. Legacy
    entries ({win, draw, forfeit, score [gf, ga], elo, delta}, stored
    newest first) are converted here: the screen read .goals/.date/
    .elodiff, found none, and rendered "undefined" / 01/01/1970 /
    "NaN" (user report 2026-11-12). A legacy entry's date comes from
    the elos point recorded at the same rating that day. Draws are
    skipped -- the record screen's streak has no draw (upstream)."""
    raw = rk.get("recent") or []
    if raw and isinstance(raw[0], dict) and "goals" in raw[0]:
        return [dict(e) for e in raw][-3:]
    by_elo = {}
    for h in rk.get("elos") or []:
        d = str(h.get("dateymd") or "")
        if len(d) == 8:
            by_elo.setdefault(int(h.get("elo", 0) or 0),
                              f"{d[:4]}-{d[4:6]}-{d[6:]}T00:00:00.000Z")
    out, streak = [], 0
    for e in reversed(raw):  # legacy storage is newest first
        if not isinstance(e, dict) or e.get("draw"):
            continue
        won = bool(e.get("win"))
        streak = (streak + 1 if streak > 0 else 1) if won else \
            (streak - 1 if streak < 0 else -1)
        sc = e.get("score") or [0, 0]
        out.append({
            "date": by_elo.get(int(e.get("elo", 0) or 0),
                               "2026-11-01T00:00:00.000Z"),
            "goals": int(sc[0] or 0), "goalsconceded": int(sc[1] or 0),
            "shootout": 0, "shootoutconceded": 0,
            "streak": streak, "elodiff": int(e.get("delta", 0) or 0),
            "elo": int(e.get("elo", 0) or 0),
            "forfeits": 1 if e.get("forfeit") else 0,
        })
    return out[-3:]


def _coach_ladder_statistics(st):
    rk = _get_ranked_state(st)
    rk3 = _get_mgr_ranked_state(st)
    elo = int(rk.get("elo", LADDER_START_ELO))
    highelo = max(elo, int(rk.get("highelo", elo)))
    div_name, div_num = _division_for_elo(elo)
    best_div_name, best_div_num = _division_for_elo(highelo)
    wins = int(rk.get("wins", 0))
    draws = int(rk.get("draws", 0))
    losses = int(rk.get("losses", 0))
    total = wins + draws + losses
    winrate = int(round((wins * 100.0) / total)) if total > 0 else 0
    streak = int(rk.get("streak", 0))

    div_obj = {"name": f"DIVISION_{div_name}", "rank": div_num}
    best_div_obj = {"name": f"DIVISION_{best_div_name}", "rank": best_div_num}

    # Bug 2a-2: per-mode records -- 1v1 (index 0) and manager (index 3)
    # have their own; the modes this server cannot play stay fresh.
    elo3 = int(rk3.get("elo", LADDER_START_ELO))
    highelo3 = max(elo3, int(rk3.get("highelo", elo3)))
    div3_name, div3_num = _division_for_elo(elo3)
    best3_name, best3_num = _division_for_elo(highelo3)
    div3_obj = {"name": f"DIVISION_{div3_name}", "rank": div3_num}
    best3_obj = {"name": f"DIVISION_{best3_name}", "rank": best3_num}
    wins3 = int(rk3.get("wins", 0))
    draws3 = int(rk3.get("draws", 0))
    losses3 = int(rk3.get("losses", 0))
    total3 = wins3 + draws3 + losses3
    streak3 = int(rk3.get("streak", 0))
    start_div_obj = {"name": "DIVISION_AMATEUR_C",
                     "rank": _division_for_elo(LADDER_START_ELO)[1]}

    # Board positions among the real accounts (upstream computes real
    # ranks; ours were hard-coded 1 / [1] * 10 and the season screen
    # stringified that array on screen as "1,1,1,1,..."). 0 = no games
    # played in that mode yet.
    rank0 = rank3 = 0
    try:
        _init_user_db()
        with closing(_user_db()) as _db:
            _urows = _db.execute("SELECT state_json FROM users").fetchall()
        _elos0, _elos3 = [], []
        for _u in _urows:
            try:
                _stu = json.loads(_u["state_json"] or "{}") or {}
            except Exception:
                _stu = {}
            _elos0.append(int((_stu.get("ranked") or {}).get("elo", LADDER_START_ELO)))
            _elos3.append(int((_stu.get("ranked_mgr") or {}).get("elo", LADDER_START_ELO)))
        if total > 0:
            rank0 = 1 + sum(1 for e in _elos0 if e > elo)
        if total3 > 0:
            rank3 = 1 + sum(1 for e in _elos3 if e > elo3)
    except Exception:
        pass
    ranks = [rank0, 0, 0, rank3, 0, 0]
    elos_i = [elo, LADDER_START_ELO, LADDER_START_ELO, elo3,
              LADDER_START_ELO, LADDER_START_ELO]
    highs_i = [highelo, LADDER_START_ELO, LADDER_START_ELO, highelo3,
               LADDER_START_ELO, LADDER_START_ELO]

    ladder_mode_info = _ladder_mode_record(rk, rank0)
    mgr_mode_info = _ladder_mode_record(rk3, rank3)
    fresh_mode_info = _ladder_mode_record(_fresh_ranked_state())

    season_entry = {
        # Bug 2a (round 4): sid must be a STRING -- the client looks a
        # season up by strict equality against a String-coerced sid, so a
        # numeric sid never matched and the record rows stayed "-".
        "sid": "202611",
        # Season shape aligned to upstream's ladder_season_record
        # (2026-11-12, user report: undefined / 01/01/1970 / NaN rows,
        # best shown as Legendary S): exactly LADDER_COUNT (6) modes;
        # elos[i] = [{elo, dateymd "YYYYMMDD"}] rating history;
        # division[i] = {name, rank} with rank = board position;
        # highrecord[i] = {elo, rank} FLAT -- the screen derives the
        # best division from that elo itself (our old nested
        # [[{elo}]] arrays parsed as garbage -> "Huyen thoai S 00d");
        # recentmatches[i] = _recent_served entries (see its docstring).
        "elos": [
            [{"elo": int(h.get("elo", elo)), "dateymd": str(h.get("dateymd", ""))}
             for h in (rk.get("elos") or [])],
            [], [],
            [{"elo": int(h.get("elo", elo3)), "dateymd": str(h.get("dateymd", ""))}
             for h in (rk3.get("elos") or [])],
            [], [],
        ],
        "wins": [wins, 0, 0, wins3, 0, 0],
        "losses": [losses, 0, 0, losses3, 0, 0],
        "forfeits": [int(rk.get("forfeits", 0)), 0, 0,
                     int(rk3.get("forfeits", 0)), 0, 0],
        "swins": [0] * 6,
        "slosses": [0] * 6,
        "rank": ranks,
        "division": [{"name": f"DIVISION_{_division_for_elo(e)[0]}",
                      "rank": r} for e, r in zip(elos_i, ranks)],
        "highrecord": [{"elo": h, "rank": r}
                       for h, r in zip(highs_i, ranks)],
        "recentmatches": [_recent_served(rk), [], [],
                          _recent_served(rk3), [], []],
    }
    # Bug 2a (round 5): the previous season must be marked rewarded --
    # LadderMatchFrontScreen.checkSeasonReward treats a season entry
    # without rewarded=true as an unclaimed season reward and POSTs
    # /ladder/reward (popping the season-reward board) on EVERY visit.
    # There is no real previous season on this server, so it is a
    # zeroed, already-rewarded placeholder in the same shape.
    prev_season_entry = {
        "sid": "202610",
        "rewarded": True,
        "elos": [[] for _ in range(6)],
        "wins": [0] * 6,
        "losses": [0] * 6,
        "forfeits": [0] * 6,
        "swins": [0] * 6,
        "slosses": [0] * 6,
        "rank": [0] * 6,
        "division": [{"name": "DIVISION_AMATEUR_C", "rank": 0}
                     for _ in range(6)],
        "highrecord": [{"elo": LADDER_START_ELO, "rank": 0}
                       for _ in range(6)],
        "recentmatches": [[] for _ in range(6)],
    }

    mode_record = _legacy_mode_record(rk)
    mgr_mode_record = _legacy_mode_record(rk3)
    ev = st.get("events") or {}
    end_date_str = str(ev.get("ladder_season_end", "2026-12-31")).strip()
    end_iso = f"{end_date_str}T23:59:59Z"
    out = {
        "sid": "202611",
        "prevsid": 202610,
        "season": 202611,
        "lastseason": 202610,
        "prevseason": 202610,
        "year": 2026,
        "month": 11,
        "title": "Season 2026",
        "title2": "Season 2026",
        "start": "2026-11-01T00:00:00Z",
        "next": end_iso,
        "end": end_iso,
        "rewardchecked": True,
        "hasreward": False,
        "elo": elo,
        "highelo": highelo,
        "division": f"DIVISION_{div_name}",
        "bestdivision": f"DIVISION_{best_div_name}",
        # Fields read by LadderMatchController /my/info & rebuildLadderRecordInfo
        "ladder_pvp_1v1": dict(ladder_mode_info),
        "ladder_pvp_2v2": dict(fresh_mode_info),
        "ladder_pvp_3v3": dict(fresh_mode_info),
        "ladder_roleplay_pvp_3v3": dict(fresh_mode_info),
        "ladder_mgr_pvp_1v1": dict(mgr_mode_info),
        "ladder_mgr_versus": dict(mgr_mode_info),
        "seasons": [season_entry, prev_season_entry],
        "highrecords": {
            "count": total + total3,
            "elo": highs_i,
            "division": [best_div_obj, start_div_obj, start_div_obj,
                         best3_obj, start_div_obj, start_div_obj],
            "rank": [0] * 6,
            "winningstreak": [streak, 0, 0, streak3, 0, 0],
            "wins": [wins, 0, 0, wins3, 0, 0],
            "losses": [losses, 0, 0, losses3, 0, 0],
            "date": "2026-11-01T00:00:00.000Z",
        },
        # Legacy mode record aliases
        "1on1": dict(mode_record),
        "2on2": dict(mode_record),
        "3on3": dict(mode_record),
        "4on4": dict(mode_record),
        "5on5": dict(mode_record),
        "mgr": dict(mgr_mode_record),
        "custom": dict(mode_record),
    }
    if total + total3 > 0:
        # Bug 2a (round 4): the Rank front window's season-best panel
        # (LadderMatchFrontRecordWindow.initializeBestRecord) and the
        # mid-reward check read ladder_total: playedgames drives the
        # "matches needed for reward x/5" gauge and totalwins the
        # weekly "x/10 wins" gauge. Without it the panel said "Khong co"
        # with 0/5 and 0/10 forever. Only present once games exist.
        # Bug 2a-2: totals span both playable modes; hightype is the
        # mode holding the best rating.
        import datetime as _dt2
        wk_now = _dt2.datetime.now().strftime("%G%V")
        week_wins = 0
        for _rk in (rk, rk3):
            ww = _rk.get("weekwins") or {}
            if ww.get("week") == wk_now:
                week_wins += int(ww.get("wins", 0))
        out["ladder_total"] = {
            "midrewardable": False,
            "highelo": max(highelo, highelo3),
            "hightype": "Ranked_PvP_1v1" if highelo >= highelo3 else "Ranked_MGR_PvP_1v1",
            "playedgames": total + total3,
            "totalwins": week_wins,
        }
    return out


def ep_ladder_division_info(query, body):
    return hc._json(LADDER_DIVISIONS)


_CLUB_DIVISIONS = None


def ep_club_ranked_division_info(query, body):
    """GET /club/ranked/division/info: the CLUB division table
    (responses/club_ranked_division_info.json -- names carry no
    DIVISION_ prefix; the screen builds CLUB_DIVISION_<name> keys).
    The route was wired to the 1v1 ladder table, whose names rendered
    as a truncated raw "CLUB_DIVISION_L..." label (user 2026-11-12)."""
    global _CLUB_DIVISIONS
    if _CLUB_DIVISIONS is None:
        try:
            with open(os.path.join(hc.RESPONSES,
                                   "club_ranked_division_info.json"),
                      encoding="utf-8") as f:
                _CLUB_DIVISIONS = json.load(f)
        except Exception:
            _CLUB_DIVISIONS = LADDER_DIVISIONS
    return hc._json(_CLUB_DIVISIONS)


def ep_ladder_reward_info(query, body):
    divs = []
    for idx, d in enumerate(LADDER_DIVISIONS):
        ep_amount = max(50000, (16 - idx) * 100000)
        divs.append({
            "name": f"DIVISION_{d['name']}",
            "items": [
                {"itemid": 1, "count": ep_amount},
                {"itemid": 8334, "count": 1},
            ],
        })
    wins = [
        {"from": 10, "to": 10, "items": [{"itemid": 1, "count": 50000}]},
        {"from": 7, "to": 9, "items": [{"itemid": 1, "count": 30000}]},
        {"from": 4, "to": 6, "items": [{"itemid": 1, "count": 15000}]},
        {"from": 1, "to": 3, "items": [{"itemid": 1, "count": 5000}]},
    ]
    return hc._json({"divs": divs, "wins": wins})


def ep_ladder_my_info(query, body):
    st = _state()
    stats = _coach_ladder_statistics(st)
    stats["result"] = True
    return hc._json(stats)


def ep_ladder_reward(query, body):
    st = _state()
    slot = hc._default_slot(st)
    # Bug 2a (round 5): the season-reward board renders its division /
    # points / W-L / games / win-rate from THIS response; with only
    # result+empty rewards it showed AMATEUR_C, "00" points and zeros
    # for a WorldClass C player. Include the current 1v1 record.
    rk = _get_ranked_state(st)
    elo_r = int(rk.get("elo", LADDER_START_ELO))
    div_r, div_num_r = _division_for_elo(elo_r)
    wins_r = int(rk.get("wins", 0))
    draws_r = int(rk.get("draws", 0))
    losses_r = int(rk.get("losses", 0))
    games_r = wins_r + draws_r + losses_r
    stats_r = _coach_ladder_statistics(st)
    return hc._json({
        "result": True,
        "season": 202611,
        "rewards": [],
        "items": [],
        "ep": 0,
        "elo": elo_r,
        "points": elo_r,
        "highelo": int(rk.get("highelo", elo_r)),
        "division": {"name": f"DIVISION_{div_r}", "rank": div_num_r},
        "divisionname": f"DIVISION_{div_r}",
        "wins": wins_r,
        "draws": draws_r,
        "losses": losses_r,
        "games": games_r,
        "winrate": round(wins_r * 100 / games_r) if games_r else 0,
        "ladder_pvp_1v1": stats_r["ladder_pvp_1v1"],
        "resources": hc.coach_me_resources(st, slot) if st.get("coaches") else [],
    })


def ep_ladder_leaderboard(query, body):
    _init_user_db()
    st = _state()
    c = hc.my_coach(st) or {"_id": hc.coach_id(0), "name": "Manager", "teamid": 1}
    rk = _get_ranked_state(st)
    my_elo = int(rk.get("elo", LADDER_START_ELO))
    my_div, my_div_num = _division_for_elo(my_elo)

    rows = []
    with closing(_user_db()) as db:
        db_users = db.execute("""
            SELECT id, username, coach_name, elo, wins, draws, losses
            FROM users ORDER BY elo DESC, wins DESC LIMIT 20
        """).fetchall()
        for idx, u in enumerate(db_users, 1):
            u_div, u_div_num = _division_for_elo(u["elo"])
            rows.append({
                "rank": idx,
                "ranking": idx,
                "id": f"{u['id']:024x}",
                "name": u["coach_name"] or u["username"],
                "teamid": 1,
                "elo": u["elo"],
                "division": {"name": f"DIVISION_{u_div}", "rank": u_div_num},
                "wins": u["wins"],
                "draws": u["draws"],
                "losses": u["losses"],
                "forfeits": 0,
            })

    if not rows:
        rows.append({
            "rank": 1, "ranking": 1,
            "id": c.get("_id", hc.coach_id(0)),
            "name": c.get("name", "Manager"),
            "teamid": int(c.get("teamid") or 1),
            "elo": my_elo,
            "division": {"name": f"DIVISION_{my_div}", "rank": my_div_num},
            "wins": int(rk.get("wins", 0)),
            "draws": int(rk.get("draws", 0)),
            "losses": int(rk.get("losses", 0)),
            "forfeits": 0,
        })

    ai_bots = [
        ("HLV Huyền Thoại", 10, 2150, 42, 5, 3),
        ("Siêu Sao sân cỏ", 11, 1850, 28, 4, 8),
        ("Chiến Thuật Gia", 22, 1620, 19, 6, 9),
        ("Vua Phá Lưới FO3", 5, 1480, 14, 3, 10),
        ("Tân Binh Xếp Hạng", 1, 1320, 8, 2, 7),
    ]
    for bname, bteam, belo, bw, bd, bl in ai_bots:
        bdiv, bdiv_num = _division_for_elo(belo)
        rows.append({
            "rank": len(rows) + 1, "ranking": len(rows) + 1,
            "id": hc.new_iid(),
            "name": bname, "teamid": bteam,
            "elo": belo,
            "division": {"name": f"DIVISION_{bdiv}", "rank": bdiv_num},
            "wins": bw, "draws": bd, "losses": bl, "forfeits": 0,
        })
    rows.sort(key=lambda x: x["elo"], reverse=True)
    for idx, r in enumerate(rows, 1):
        r["rank"] = r["ranking"] = idx
    return hc._json({
        "result": True,
        "list": rows,
        "rankers": rows,
        "myrank": 1,
        "Ladder_PvP_1v1": rows,
        "Ladder_PvP_2v2": rows,
        "Ladder_PvP_3v3": rows,
        "Ladder_RolePlay_PvP_3v3": rows,
        "Ladder_MGR_PvP_1v1": rows,
        "ladder_pvp_1v1": rows,
        "ladder_pvp_2v2": rows,
        "ladder_pvp_3v3": rows,
        "ladder_roleplay_pvp_3v3": rows,
        "ladder_mgr_pvp_1v1": rows,
    })


def ep_club_ranked_season_info(query, body):
    st = _state()
    ev = st.get("events") or {}
    end_date_str = str(ev.get("ladder_season_end", "2026-12-01")).strip()
    try:
        import datetime
        end_dt = datetime.datetime.strptime(end_date_str, "%Y-%m-%d")
        remain = max(0, int((end_dt - datetime.datetime.now()).total_seconds()))
    except Exception:
        remain = 86400 * 30
    end_iso = f"{end_date_str}T23:59:59Z"
    start_iso = "2026-11-01T00:00:00Z"
    daily_times = [[{"starttime": "0000", "endtime": "2359"}] for _ in range(7)]
    startdate = "20261101"
    starttime = "000000"
    enddate = end_date_str.replace("-", "") if len(end_date_str.replace("-", "")) == 8 else "20261231"
    endtime = "235959"
    season_obj = {
        "seasonid": 202611,
        "sid": 202611,
        "prevseasonid": 202610,
        "startedat": start_iso,
        "rewardedat": end_iso,
        "start": start_iso,
        "end": end_iso,
        "next": end_iso,
        "startdate": startdate,
        "starttime": starttime,
        "enddate": enddate,
        "endtime": endtime,
        "remaintime": remain,
        "times": daily_times,
    }
    return hc._json({
        "result": True,
        "sid": 202611,
        "prevsid": 202610,
        "start": start_iso,
        "end": end_iso,
        "next": end_iso,
        "startdate": startdate,
        "starttime": starttime,
        "enddate": enddate,
        "endtime": endtime,
        "remaintime": remain,
        "season": season_obj
    })

def ep_ladder_season_info(query, body):
    st = _state()
    ev = st.get("events") or {}
    end_date_str = str(ev.get("ladder_season_end", "2026-12-01")).strip()
    try:
        import datetime
        end_dt = datetime.datetime.strptime(end_date_str, "%Y-%m-%d")
        remain = max(0, int((end_dt - datetime.datetime.now()).total_seconds()))
    except Exception:
        remain = 86400 * 30
    end_iso = f"{end_date_str}T23:59:59Z"
    start_iso = "2026-11-01T00:00:00Z"
    daily_times = [["00:00", "23:59"]]
    startdate = "20261101"
    starttime = "000000"
    enddate = end_date_str.replace("-", "") if len(end_date_str.replace("-", "")) == 8 else "20261231"
    endtime = "235959"
    season_dict = {
        "seasonid": 202611,
        "sid": 202611,
        "prevseasonid": 202610,
        "startedat": start_iso,
        "rewardedat": end_iso,
        "start": start_iso,
        "end": end_iso,
        "next": end_iso,
        "startdate": startdate,
        "starttime": starttime,
        "enddate": enddate,
        "endtime": endtime,
        "remaintime": remain,
        "times": daily_times,
    }
    return hc._json({
        "result": True,
        "sid": 202611,
        "prevsid": 202610,
        "start": start_iso,
        "end": end_iso,
        "next": end_iso,
        "startdate": startdate,
        "starttime": starttime,
        "enddate": enddate,
        "endtime": endtime,
        "remaintime": remain,
        "season": season_dict
    })

def ep_ladder_friends_info(query, body):
    """LadderMatchController calls .forEach on /ladder/friends/info response, so it must be a list.

    Bug 2a (rounds 2-4): this used to return a hard-coded []. Round 2
    served flat records with SCALAR elo/wins; the client rejected them
    -- its own telemetry (2026-11-12 19:53 UTC) reports UI_ERROR
    Error #1069 'Property 0 not found on 1500' in
    LadderMatchController.forEachMatchCount() called from
    rebuildLadderRecordFriendsInfo(): per-mode values are ARRAYS
    indexed by ladder index. The real entry shape (documented from the
    client's Module.big): {_id, name, teamid, ovr {FW, MF, DF},
    elo [i], wins [i], losses [i], forfeits [i]} with LADDER_COUNT = 6
    modes; index 0 = Ranked 1v1, index 3 = manager 1v1 (both share this
    server's single ranked record). One entry per user, best elo first.
    """
    _init_user_db()
    st_me = _state()
    my_cid, _my_name = _my_coach_id_and_name(st_me)
    rows = []
    with closing(_user_db()) as db:
        db_users = db.execute("""
            SELECT id, username, coach_name, elo, state_json
            FROM users ORDER BY elo DESC
        """).fetchall()
        friend_cids = {r[0] for r in db.execute(
            "SELECT friend_id FROM friends WHERE coach_id = ? AND status = 'accepted'",
            (str(my_cid),)).fetchall()}
    # Friends ONLY -- not me. Upstream's ep_ladder_friends returns just
    # the friends, and LadderMatchController appends my own record
    # itself (the friends tab view then finds it by its "isMe" flag).
    # When this list also carried me, the screen showed me TWICE: once
    # sorted in, once appended at the end (user report 2026-11-12,
    # GM2 at #2 and #3 with identical records).
    keep_cids = friend_cids
    for u in db_users:
        try:
            st_u = json.loads(u["state_json"] or "{}") or {}
        except Exception:
            st_u = {}
        rk_u = st_u.get("ranked") or {}
        elo_u = int(rk_u.get("elo", LADDER_START_ELO))
        wins_u = int(rk_u.get("wins", 0))
        losses_u = int(rk_u.get("losses", 0))
        forfeits_u = int(rk_u.get("forfeits", 0))
        # Bug 2a-2: index 3 carries the manager record, not a mirror of 1v1.
        rk_m = st_u.get("ranked_mgr") or {}
        elo_m = int(rk_m.get("elo", LADDER_START_ELO))
        wins_m = int(rk_m.get("wins", 0))
        losses_m = int(rk_m.get("losses", 0))
        forfeits_m = int(rk_m.get("forfeits", 0))
        coaches_u = st_u.get("coaches") or []
        coach_u = coaches_u[0] if coaches_u else {}
        # "Hang ban be" is a FRIENDS ranking: only accepted friends
        # belong here (user 2026-11-12: it listed coaches never friended --
        # this loop used to emit every account on the server; the client
        # adds my own row itself, see keep_cids above).
        if str(coach_u.get("_id") or _coach_id_for_user(u["id"], 0)) not in keep_cids:
            continue
        ovr_u = {"FW": 0, "MF": 0, "DF": 0}
        try:
            team_u = (st_u.get("teams") or {}).get(str(coach_u.get("slot", 0))) or hc.roster_team(int(coach_u.get("teamid", 1)))
            sum_u = hc.team_summary(team_u, int(coach_u.get("teamid", 1))) or {}
            o = sum_u.get("ovr") or {}
            ovr_u = {k: int(o.get(k, 0)) for k in ("FW", "MF", "DF")}
        except Exception:
            pass
        elo_arr = [LADDER_START_ELO] * 6
        elo_arr[0] = elo_u
        elo_arr[3] = elo_m
        wins_arr = [0] * 6
        wins_arr[0] = wins_u
        wins_arr[3] = wins_m
        losses_arr = [0] * 6
        losses_arr[0] = losses_u
        losses_arr[3] = losses_m
        forfeits_arr = [0] * 6
        forfeits_arr[0] = forfeits_u
        forfeits_arr[3] = forfeits_m
        rows.append({
            "_id": coach_u.get("_id") or _coach_id_for_user(u["id"], 0),
            "name": u["coach_name"] or u["username"],
            "teamid": int(coach_u.get("teamid", 1)),
            "ovr": ovr_u,
            "elo": elo_arr,
            "wins": wins_arr,
            "losses": losses_arr,
            "forfeits": forfeits_arr,
        })
    rows.sort(key=lambda r: -r["elo"][0])
    return hc._json(rows)


# =========================================================================== #
# 4. ROOMS, RANKED MATCHMAKING & FRIENDLY PVP (EXHIBITION / CUSTOM)           #
# =========================================================================== #

BOT_ENGLISH_NAMES = [
    "Alex Ferguson", "David Beckham", "Michael Owen", "Steven Gerrard", "Frank Lampard",
    "Wayne Rooney", "Harry Kane", "Marcus Rashford", "John Terry", "Paul Scholes",
    "James Milner", "Jordan Henderson", "Raheem Sterling", "Jack Grealish", "Declan Rice",
    "Phil Foden", "Bukayo Saka", "Mason Mount", "Jude Bellingham", "Trent Alexander",
    "Kyle Walker", "Luke Shaw", "Kieran Trippier", "Harry Maguire", "John Stones",
    "Jordan Pickford", "Aaron Ramsdale", "Nick Pope", "Dominic Calvert", "Callum Wilson",
    "Ollie Watkins", "Ivan Toney", "James Maddison", "Harvey Barnes", "Conor Gallagher",
    "Jarrod Bowen", "Eberechi Eze", "Michael Olise", "Cole Palmer", "Anthony Gordon"
]

def _build_upgraded_bot_team_raw(opp_tid):
    """Build a natural bot opponent team with standard card grades (+0 to +3) and normal levels."""
    bot_raw = copy.deepcopy(hc.roster_team(int(opp_tid)))
    for p in bot_raw.get("players") or []:
        p["grade"] = random.choice([0, 1, 1, 1, 2, 2, 3])
        lvl = random.randint(3, 15)
        p["levelinfo"] = {"level": lvl, "exp": 0, "to": hc.player_need(lvl)}
    # Do NOT call hc.fix_formation here (Issue 2d): it pops team["formations"]
    # and repoints squad.formation at the club's own formation id, leaving the
    # AI side with formations=[] at match start -- fifazf.exe then dereferences
    # a null formation and dies (dump 2026-11-11: 0xC0000005, read of 0x0, in
    # fifazf.exe at stage playing). League/cup AI sides pass roster_team()
    # untouched to match_side and start fine; keep the bot team identical in
    # structure to those. repair_squad is safe: owned_team runs it anyway.
    hc.repair_squad(bot_raw)
    return bot_raw
def _make_room_resource(
    room_id,
    st,
    room_type="ladder_pvp_1v1",
    slotsize=1,
    stage="room",
    match_obj=None,
    left_tid=None,
    right_tid=None,
    left_ready=False,
    right_players_override=None,
    opp_name_override=None,
    opp_level_override=None,
):
    slot = hc._default_slot(st) if st.get("coaches") else 0
    me = hc.coach_me(st, slot) if st.get("coaches") else {
        "id": hc.coach_id(0),
        "name": "Manager",
        "teamid": 1,
        "levelinfo": {"level": 1, "exp": 0, "maxexp": 100},
        "summary": {"name": "Manager", "teamid": 1, "ovr": {"FW": 75, "MF": 75, "DF": 75}},
    }
    cid = me.get("id") or hc.coach_id(0)
    cname = me.get("name") or "Manager"
    my_tid = int(left_tid or me.get("teamid") or 1)
    # Same validated bot pool as the match builder: an unfiltered pick over
    # _CLUB_TEAMS can land on an empty-roster club (see _pick_bot_team_id).
    opp_tid = int(right_tid or _pick_bot_team_id(exclude_tid=my_tid))
    clevel = int((me.get("levelinfo") or {}).get("level", 1))

    # Bug 2a-2: a manager room shows the manager record's rating.
    rk = (_get_mgr_ranked_state(st)
          if ("mgr" in str(room_type).lower() or "manager" in str(room_type).lower())
          else _get_ranked_state(st))
    my_elo = int(rk.get("elo", LADDER_START_ELO))
    my_div, _ = _division_for_elo(my_elo)

    # MultiplayCustomMatchScreen.updateLTeamInfo calls PlayerListUtil.generateCaptainFromRoomInfo(room.side.left)
    # which requires room.side.left.squad.positions and team info!
    raw_my_team = (st.get("teams") or {}).get(str(slot))
    if not raw_my_team or "exhibition" in str(room_type):
        raw_my_team = hc.roster_team(my_tid)
    my_team_obj = hc.owned_team(raw_my_team)
    my_team_summary = hc.team_summary(my_team_obj, my_tid)
    my_team_obj.update(teamid=my_tid, summary=my_team_summary)
    slim_my_team = _slim_team(my_team_obj)

    mm = {"multiplematch": False, "resources": []}
    if match_obj is not None:
        mm["resources"] = [{"uri": f"/match/{match_obj['id']}", "body": match_obj}]

    my_player_entry = {
        "id": cid,
        "_id": cid,
        "name": cname,
        "teamid": my_tid,
        "level": clevel,
        "levelinfo": me.get("levelinfo") or {"level": clevel, "exp": 0, "maxexp": 100},
        "captain": True,
        "master": True,
        "onmatch": False,
        "ready": bool(left_ready),
        "elo": my_elo,
        "division": f"DIVISION_{my_div}",
        "clubid": "",
        "clubname": "",
        # Issue 2d (display): the entry "summary" must be the TEAM summary
        # (club name), like the bot entry uses bot_summary. me["summary"] is
        # the COACH summary (its "name" is the coach name), which made the
        # versus screen render the coach name where the club name belongs.
        "summary": my_team_summary,
        "squad": my_team_obj["squad"],
        "team": {"teamid": my_tid, "summary": my_team_summary},
    }
    # Issue 2j: room player entries are the user objects the client feeds to
    # LobbyManager::ConnectSession -- they need the session profile fields
    # (a missing string "ip" hard-traps the client at matchmaking completion).
    my_player_entry.update(_session_profile_fields())

    if right_players_override is not None:
        right_players = list(right_players_override)
    elif stage == "matched":
        opp_id = hc.coach_id(1)
        opp_name = opp_name_override or random.choice(BOT_ENGLISH_NAMES)
        bot_raw = _build_upgraded_bot_team_raw(opp_tid)
        bot_team_obj = hc.owned_team(bot_raw)
        bot_summary = hc.team_summary(bot_team_obj, opp_tid)
        bot_team_obj.update(teamid=opp_tid, summary=bot_summary, teamcolors=hc.active_teamcolors(bot_team_obj))
        slim_bot_team = _slim_team(bot_team_obj)
        bot_lvl = int(opp_level_override) if opp_level_override else random.randint(15, 65)
        right_players = [{
            "id": opp_id,
            "_id": opp_id,
            "name": opp_name,
            "teamid": opp_tid,
            "level": bot_lvl,
            "levelinfo": {"level": bot_lvl, "exp": 0, "maxexp": hc.coach_need(bot_lvl)},
            "captain": True,
            "master": False,
            "onmatch": False,
            "ready": True,
            "elo": my_elo,
            "division": f"DIVISION_{my_div}",
            "clubid": "",
            "clubname": "",
            "summary": bot_summary,
            "squad": bot_team_obj["squad"],
            "team": bot_team_obj,
        }]
        # Issue 2j: same ConnectSession requirement for the bot's entry.
        right_players[0].update(_session_profile_fields())
    else:
        # Do NOT pre-fill a bot when creating a room (stage == "room"), so the user can invite friends!
        right_players = []

    right_side_dict = {
        "ready": bool(right_players),
        "authTeamId": opp_tid,
        "players": right_players,
    }
    if right_players and "squad" in right_players[0]:
        right_side_dict["squad"] = right_players[0]["squad"]
        right_side_dict["team"] = right_players[0].get("team")
    else:
        empty_opp_team = hc.owned_team(_build_upgraded_bot_team_raw(opp_tid))
        right_side_dict["squad"] = empty_opp_team["squad"]
        right_side_dict["team"] = empty_opp_team

    right_role_0 = [right_players[0]["id"]] if right_players else []

    return {
        "id": int(room_id),
        "roomid": int(room_id),
        "uri": f"/room/{room_id}",
        "type": str(room_type),
        "stage": stage,
        "slotsize": int(slotsize or 1),
        "host": cid,
        "observermode": False,
        "shootoutmode": True,
        "drawmode": False,
        "properties": {"publicmode": True},
        "AI": {"index": {"nation": 0, "league": 0, "team": 0, "difficulty": 2}},
        "ranked": {"elo": my_elo},
        "ladder": {"elo": my_elo},
        "roles": {
            "left": {"0": [cid], "1": [], "2": [], "3": []},
            "right": {"0": right_role_0, "1": [], "2": [], "3": []},
        },
        "side": {
            "left": {
                "ready": bool(left_ready),
                "authTeamId": my_tid,
                "players": [my_player_entry],
                "squad": my_team_obj["squad"],
                "team": my_team_obj,
            },
            "center": {
                "players": [],
            },
            "right": right_side_dict,
            "referee": {
                "players": [],
            },
        },
        "members": [my_player_entry] + right_players,
        "matchmaking": mm,
    }


def _create_room_common(query, body, default_type="ladder_pvp_1v1"):
    st = _state()
    try:
        req = json.loads(body.decode("utf-8")) if body else {}
    except Exception:
        req = {}
    room_type = str(req.get("type") or default_type)
    slotsize = int(req.get("slotsize") or 1)
    if room_type == "pvp_exhibition":
        slotsize = 1
    room_id = 1
    room = _make_room_resource(room_id, st, room_type=room_type, slotsize=slotsize, stage="room")
    ACTIVE_ROOMS[room_id] = room
    st["active_room"] = room
    _save(st)
    return hc._json({
        "result": True,
        "id": room_id,
        "roomid": room_id,
        "room": room,
        "resources": [{"uri": f"/room/{room_id}", "body": room}],
    })


def ep_room_create(query, body):
    return _create_room_common(query, body, default_type="ladder_pvp_1v1")


def ep_exhibition_room_create(query, body):
    return _create_room_common(query, body, default_type="pvp_exhibition")


def ep_room_get(rid, query, body):
    st = _state()
    rid_int = int(rid) if str(rid).isdigit() else 1
    room = ACTIVE_ROOMS.get(rid_int) or st.get("active_room") or _make_room_resource(rid_int, st)
    return hc._json(room)


_MATCHMAKING_TIMERS = {}

# Issue 2 diagnostics: the matchmaking completion runs on a timer thread and
# its failures used to go only to the server console (print), which is not in
# http_config.log. Log the whole completion path to a file next to the other
# captures so a stuck matchmaking attempt can be diagnosed from logs alone.
_MM_DEBUG_LOG = os.path.join(os.path.dirname(os.path.abspath(__file__)),
                             "captures", "matchmaking_debug.log")


def _mm_debug(msg):
    try:
        os.makedirs(os.path.dirname(_MM_DEBUG_LOG), exist_ok=True)
        with open(_MM_DEBUG_LOG, "a", encoding="utf-8") as f:
            f.write(f"{time.strftime('%H:%M:%S')} {msg}\n")
    except Exception:
        pass


def _cancel_matchmaking_timer(rid_int):
    tmr = _MATCHMAKING_TIMERS.pop(rid_int, None)
    if tmr is not None:
        try:
            tmr.cancel()
        except Exception:
            pass


def ep_room_leave(rid, query, body):
    st = _state()
    rid_int = int(rid) if str(rid).isdigit() else 1
    _cancel_matchmaking_timer(rid_int)
    prev_room = ACTIVE_ROOMS.pop(rid_int, None) or st.pop("active_room", None)
    st.pop("active_room", None)
    _save(st)

    empty_room = {
        "id": rid_int,
        "roomid": rid_int,
        "uri": f"/room/{rid_int}",
        "type": (prev_room or {}).get("type", "ladder_pvp_1v1"),
        "stage": "closed",
        "slotsize": (prev_room or {}).get("slotsize", 1),
        "host": "",
        "observermode": False,
        "shootoutmode": True,
        "drawmode": False,
        "properties": {"publicmode": True},
        "AI": {"index": {"nation": 0, "league": 0, "team": 0, "difficulty": 2}},
        "ranked": {"elo": LADDER_START_ELO},
        "ladder": {"elo": LADDER_START_ELO},
        "roles": {
            "left": {"0": [], "1": [], "2": [], "3": []},
            "right": {"0": [], "1": [], "2": [], "3": []},
        },
        "side": {
            "left": {"ready": False, "authTeamId": 0, "players": [], "squad": {"positions": []}},
            "center": {"players": []},
            "right": {"ready": False, "authTeamId": 0, "players": [], "squad": {"positions": []}},
            "referee": {"players": []},
        },
        "members": [],
        "matchmaking": {"multiplematch": False, "resources": []},
    }
    return hc._json({
        "result": True,
        "resources": [{"uri": f"/room/{rid_int}", "body": None}],
    })


def _find_coach_across_users(target_cid=None, target_name=None):
    """Look up a coach by coach_id or coach_name across all users in fo3_users.db and default state."""
    _init_user_db()
    t_cid = str(target_cid or "").strip()
    t_name = str(target_name or "").strip().lower()
    with closing(_user_db()) as db:
        rows = db.execute("SELECT id, username, coach_name, elo, state_json FROM users ORDER BY id ASC").fetchall()
        for r in rows:
            try:
                st_obj = _ensure_unique_coach_ids(json.loads(r["state_json"] or "{}"), r["id"])
            except Exception:
                continue
            for idx, c in enumerate(st_obj.get("coaches") or []):
                cid = str(c.get("_id") or _coach_id_for_user(r["id"], idx))
                cname = str(c.get("name") or r["coach_name"] or r["username"])
                if (t_cid and cid == t_cid) or (t_name and (cname.lower() == t_name or str(r["username"]).lower() == t_name)):
                    slot = int(c.get("slot", idx))
                    team_raw = (st_obj.get("teams") or {}).get(str(slot)) or hc.roster_team(int(c.get("teamid") or 1))
                    return {
                        "uid": r["id"],
                        "username": r["username"],
                        "cid": cid,
                        "name": cname,
                        "teamid": int(c.get("teamid") or 1),
                        "levelinfo": hc.coach_levelinfo(c),
                        "elo": int(r["elo"] or LADDER_START_ELO),
                        "team_raw": team_raw,
                        "st": st_obj,
                    }
    return None


def _pick_bot_team_id(exclude_tid=None):
    """Random club for a bot opponent, restricted to EURO_CLUBS with a real
    roster. The matchmaking builder used to pick from every _CLUB_TEAMS key:
    23 of 551 ids have EMPTY rosters (100, 111205, 113340+, 113600, ...) and
    an empty AI team leaves the engine a null team object at match start
    (0xC0000005 in fifazf.exe). Validate the roster before accepting a pick."""
    pool = [t for t in EURO_CLUBS if t != exclude_tid]
    random.shuffle(pool)
    for tid in pool:
        try:
            raw = hc.roster_team(int(tid))
            if (len(raw.get("players") or []) >= 18
                    and len((raw.get("squad") or {}).get("positions") or []) >= 11):
                return int(tid)
        except Exception:
            continue
    return 243 if exclude_tid != 243 else 241


def _session_profile_fields():
    """Network/platform profile fields the native session layer requires on
    every user object of a room match (Issue 2j).

    When the client processes a PvP-type room match at stage "created" it
    calls LobbyManager::ConnectSession; the native builder then registers
    each side's user object into the session (fifazf 0x93D200) and reads:
    ip (string -- read with the hard GFx AsString accessor: a missing or
    non-string value hits the deliberate trap `mov dword ptr [0], 0x666` at
    0xAB2E48, which is exactly the crash the 2i build produced at
    matchmaking completion), privateip (array of strings), id and name
    (strings), levelinfo.level, esports, igr, vip, vipDetail, qqvip,
    qqgamevip, qqgamevvip, tgp1..3. A real server filled these from the
    player's live connection; against a bot there is no peer, so loopback
    values are correct -- the session entries just have to exist with the
    right types.
    """
    return {
        "ip": "127.0.0.1",
        "privateip": ["127.0.0.1"],
        "esports": False,
        "igr": False,
        "vip": 0,
        "vipDetail": 0,
        "qqvip": False,
        "qqgamevip": False,
        "qqgamevvip": False,
        "tgp1": False,
        "tgp2": False,
        "tgp3": False,
    }


def _build_room_match_and_update(rid_int, st, room):
    c = hc.my_coach(st)
    if not c:
        return None, None
    room_type = str(room.get("type") or "ladder_pvp_1v1")
    left_side_info = ((room.get("side") or {}).get("left") or {})
    right_side_info = ((room.get("side") or {}).get("right") or {})

    my_tid = int(left_side_info.get("authTeamId") or c.get("teamid") or 1)
    opp_candidates = [t for t in EURO_CLUBS if t != my_tid]

    existing_right_players = right_side_info.get("players") or []
    invited_friend = existing_right_players[0] if existing_right_players else None

    if invited_friend:
        opp_tid = int(invited_friend.get("teamid") or right_side_info.get("authTeamId") or (opp_candidates[0] if opp_candidates else 10))
        opp_name = invited_friend.get("name") or random.choice(BOT_ENGLISH_NAMES)
        opp_id = invited_friend.get("id") or hc.new_iid()
    else:
        opp_tid = _pick_bot_team_id(exclude_tid=my_tid)
        opp_name = random.choice(BOT_ENGLISH_NAMES)
        opp_id = hc.new_iid()

    slot = hc._default_slot(st)
    me = hc.coach_me(st, slot)
    rk_stats = _coach_ladder_statistics(st)
    if "mgr" in room_type.lower() or "manager" in room_type.lower():
        # Bug 2a-2: a manager match is rated on the manager record.
        mgr_stats = rk_stats.get("ladder_mgr_pvp_1v1") or {}
        my_elo = int(mgr_stats.get("elo") or LADDER_START_ELO)
        my_div = (mgr_stats.get("division") or {}).get("name") or "DIVISION_AMATEUR_C"
    else:
        my_elo = int(rk_stats.get("elo") or LADDER_START_ELO)
        my_div = rk_stats.get("division") or "DIVISION_PROFESSIONAL_B"

    user = {
        "id": me["id"],
        "_id": me["id"],
        "name": me["name"],
        "levelinfo": me["levelinfo"],
        "currdivision": my_div,
        "prevdivision": my_div,
        "clubid": "",
        "currentbadge": int(me.get("currentbadge") or 1001),
        "elo": my_elo,
    }
    # Bonus 2 follow-up (2026-11-12): "random" (the friendly hub's quick
    # and random match, both pvp_random rooms) belongs here too -- without
    # it those rooms built a League_2_league match (wrong flow for the
    # quick-match screen, which sat at "searching") and, via
    # match["_ranked"] below, would have counted as ranked games.
    is_exhibition = "exhibition" in room_type or "custom" in room_type or "sim" in room_type or "random" in room_type
    opp_user = {
        "id": opp_id,
        "_id": opp_id,
        "name": opp_name,
        "levelinfo": {"level": random.randint(20, 50), "exp": 0, "to": 10000},
        "currdivision": my_div,
        "prevdivision": my_div,
        "clubid": "",
        "currentbadge": 1001,
        "elo": my_elo,
    }

    # Issue 2j: both sides' user objects go through the native ConnectSession
    # registration, which hard-traps on a missing string "ip" (see
    # _session_profile_fields). Enrich them before they are embedded into the
    # match sides and the room resource.
    user.update(_session_profile_fields())
    opp_user.update(_session_profile_fields())

    is_mgr = "mgr" in room_type.lower() or "manager" in room_type.lower()
    is_ranked = "ladder" in room_type.lower() or "ranked" in room_type.lower()
    # Issue 2i (root cause, proven by disassembling fifazf.exe from a live
    # memory grab): the old advice "use a non-PvP type so the client skips
    # connectToTheSession" is exactly backwards for room matches. At match
    # start the engine fires FE::FIFA::OnlineSynchStart, which calls a method
    # on the FifaOnline manager's session-info object (manager field +0x80).
    # That object is created ONLY by native LobbyManager::ConnectSession,
    # which the AS3 calls at stage "created" for PvP match types, and it
    # needs relayServer.address (see http_config.ep_match_campaign). League /
    # campaign matches get their session through their own client-initiated
    # creation path, but a matchmaking-pushed room match never does: forcing
    # League_2_league left +0x80 null and StartSingleMatch died at
    # fifazf.exe+0x5D34EA dereferencing it. Use the real PvP types the client
    # itself defines (native matchtype table) and attach relayServer below.
    if is_mgr:
        mtype = "Ranked_MGR_PvP_1v1" if is_ranked else "League_2_league_MGR"
    elif is_ranked:
        mtype = "Ranked_PvP_1v1"
    elif is_exhibition:
        mtype = "Friendly_PvP_1v1"
    else:
        mtype = "League_2_league"

    left_team_raw = hc.roster_team(my_tid) if "exhibition" in room_type else st["teams"][str(slot)]

    # Check if opponent is an actual friend from DB or our upgraded European Club +5 Level 23 Bot
    friend_info = _find_coach_across_users(target_cid=opp_id) if invited_friend else None
    if friend_info and friend_info.get("team_raw"):
        right_team_raw = copy.deepcopy(friend_info["team_raw"])
        opp_user["levelinfo"] = friend_info.get("levelinfo") or opp_user["levelinfo"]
    else:
        right_team_raw = _build_upgraded_bot_team_raw(opp_tid)
    is_bot_match = not (friend_info and friend_info.get("team_raw"))

    # Issue 2 (bot crash): an AI opponent MUST be built as an AI side, i.e.
    # match_side(..., user=None) -> side.right.players == [] and ready == True,
    # the same shape every AI match in http_config uses (league_match_body etc.).
    # Passing the fake opp_user made fifazf.exe treat the bot as a real peer and
    # try a P2P socket handshake with a player that has no socket -> hang, then
    # access violation in ws2_32.dll (League_2_league). A real friend found in
    # the DB keeps opp_user: they are a genuine peer with a live client.
    if is_bot_match:
        right_match_side = hc.match_side(right_team_raw, opp_tid)
        # Bug 2c -- CLOSED, do not reopen: the bot side of a MATCH keeps
        # match_side's players == []. ANY entry here (even one carrying the
        # full session profile fields, commit ba39bfa) makes the engine
        # take the real-peer path: it runs a P2P/relay handshake against a
        # peer that has no socket and dies at StartSingleMatch
        # (fifazf.exe+0x5D34EA). Proven three times: dumps 2026-11-12
        # 04:13 (2c experiments) and 2026-11-12 17:55:38 (ranked regression
        # -- matchmaking debug log "right.players=1", RELAY_DISCONNECTED,
        # crash on Ready at the setting stage). The price is the versus
        # screen showing no bot coach name/level; that trade-off is the
        # user's decision of 2026-11-12. The ROOM resource's right side
        # keeps its bot entry (room display only, not the match side).
    else:
        right_match_side = hc.match_side(right_team_raw, opp_tid, opp_user)
    # Issue 2d: for a BOT side do not fill teamcolors either -- match_side
    # deliberately leaves an AI side's teamcolors empty ("team color for the
    # user's side only"); league/cup AI sides start matches with []. A real
    # friend's side keeps the fill: their own client expects its club colors.
    if not is_bot_match and not right_match_side["team"].get("teamcolors"):
        right_match_side["team"]["teamcolors"] = hc.active_teamcolors(right_match_side["team"])

    match = hc.new_match(
        mtype,
        hc.match_side(left_team_raw, my_tid, user, hc._tactics(st)[1]),
        right_match_side,
    )
    if is_mgr:
        match["gamesettings"]["simulated"] = True
    # Issue 2g (single-variable experiment): the match keeps new_match's birth
    # stage "created" instead of being pre-advanced to "squad" here. Every
    # match that starts successfully in this client (league, cup, practice) is
    # born "created" and is driven created -> squad -> setting -> playing by
    # the client's own ready calls; room matches were the only ones born at
    # "squad", skipping the created phase entirely -- and they are also the
    # only ones that die at StartSingleMatch (fifazf+0x5D34EA, read of 0x0),
    # in ranked and friendly alike, with verified-sound team data. The Blaze
    # session theory was ruled out by capture: with a verified-listening
    # server on 28000/28001 the client never dialled those ports at any point
    # (continuous netstat monitor from before launch through the crash was
    # empty). If the client will not drive a room match from "created", the
    # http log of the test shows exactly which ready call never arrives and
    # this line goes back to "squad".
    match["stage"] = "created"
    match["_ranked"] = not is_exhibition
    # The multiplay results tab counts coaches per side from the RECORD's
    # coaches arrays; a bot side's players stay [] (round 2c), so stash
    # the bot's identity on the match for the end-of-match recorder --
    # without it the row rendered "0 vs 1" (user report 2026-11-12).
    match["_opp_coach"] = {"coachid": opp_id, "name": opp_name}
    match["elo"] = {"left": {"rating": my_elo}, "right": {"rating": my_elo}}
    # (Rounds 2k/2l sprayed teamID/teamName/clubName over the elo entries,
    # summaries, sides, teams and players on the theory that the native
    # gameplay init read them from the payload. The client's AS3 -- read in
    # round 2m from MultiplayGameSettingScreen -- proved the versus record
    # is built AS3-side: teamName comes from the client's own
    # Localization.TEAMNAME(teamid), and clubName from match.side.X.club
    # .clubname. The actual defect was match_side's "club": {} stub; see
    # http_config.match_side. The spray was reverted in 2m.)
    # Issue 2 (stuck ready): do NOT force both sides ready=False here -- the old
    # code also cleared the AI side's ready that match_side had set. Keep the
    # match_side semantics: the user's side starts unready on purpose
    # (http_config.match_side: MatchController.sendReady skips its PUT when the
    # side is already ready, so a pre-readied user side never kicks off), while
    # the AI side stays ready=True. The squad -> setting -> playing flow is
    # driven by _side_ready / _player_ready from there.
    # Issue 2i: session info for LobbyManager::ConnectSession. At stage
    # "created" the AS3 builds the ConnectSession resource out of this match:
    # matchid, roomid, matchtype, relayServer.address, relayServer.forceRelay,
    # isLeftHome (gamesettings.home), localUserName. Without relayServer the
    # session-info object is never created and the match crashes at start
    # (fifazf.exe+0x5D34EA). The address mirrors the client's own
    # QUICKMATCH/SERVER launch setting; against a bot there is no real relay
    # peer -- the session object just has to exist.
    match["roomid"] = rid_int
    match["relayServer"] = {"address": (hc.REDIR_HOST or DEFAULT_SERVER_IP) + ":28000", "forceRelay": False}
    # LeagueCupGameSettingScreen.onPlaying reads match.leagueparam.matchtype
    # unguarded, and this match type is League_2_league: leagueparam is required
    # (same shape league_match_body in http_config sets for league matches).
    match["leagueparam"] = {
        "matchtype": "league",
        "round": 1,
        "home": my_tid,
        "away": opp_tid,
        "leagueid": 1,
    }
    st["match"] = match

    updated_room = _make_room_resource(
        rid_int, st,
        room_type=room_type,
        slotsize=room.get("slotsize", 1),
        stage="matched",
        match_obj=match,
        left_tid=my_tid,
        right_tid=opp_tid,
        left_ready=True,
        right_players_override=existing_right_players if existing_right_players else None,
        opp_name_override=opp_name,
        opp_level_override=int(opp_user["levelinfo"]["level"]),
    )
    ACTIVE_ROOMS[rid_int] = updated_room
    st["active_room"] = updated_room
    _save(st)
    return match, updated_room


def _start_room_match(rid, st, room):
    rid_int = int(rid) if str(rid).isdigit() else 1
    match, updated_room = _build_room_match_and_update(rid_int, st, room)
    if match is None:
        return hc._json({"result": False})
    return hc._json({
        "result": True,
        "id": match["id"],
        "match": match,
        "room": updated_room,
        "resources": [
            {"uri": f"/room/{rid_int}", "body": updated_room},
            {"uri": f"/match/{match['id']}", "body": match},
        ],
    })


def _bridge_room_match_created(rid_int, uname, mid):
    """Issue 2h: advance a room match created -> squad from the server side.

    Client evidence (2026-11-11, build 2g): with the match born at "created"
    the client ingests it (GET /match/<id>, renders the versus crests) but
    never sends player/ready and never leaves the room screen -- the room
    flow has no click-through like the league flow has. So the server plays
    the client's part for exactly this one transition, 1.5s after the
    created pushes (enough for ingestion): it runs the same _player_ready
    step the client's call would have run, then pushes the full match and
    the "squad" stage so MatchController fires ON_SQUADING and the squad
    screen opens as in earlier rounds. The match has still passed through
    "created" inside the client's MatchController -- the phase whose
    absence is the leading suspect for the StartSingleMatch crash.
    Guards: only if the room is still "matched" and the match is untouched.
    """
    try:
        with hc.STATE_LOCK:
            _CURRENT_USER.username = uname
            room = ACTIVE_ROOMS.get(rid_int)
            if not room or room.get("stage") != "matched":
                _mm_debug(f"bridge SKIP rid={rid_int} match={mid}: room stage="
                          f"{(room or {}).get('stage')!r}")
                return
            st = _state()
            m = st.get("match") or {}
            if m.get("id") != mid:
                _mm_debug(f"bridge SKIP rid={rid_int} match={mid}: state match is {m.get('id')!r}")
                return
            if m.get("stage") != "created":
                _mm_debug(f"bridge SKIP rid={rid_int} match={mid}: stage={m.get('stage')!r}")
                return
            _player_ready(m, {"stage": "created", "ready": True})
            st["match"] = m
            _save(st)
            _mm_debug(f"bridge advanced rid={rid_int} match={mid} created->squad")
        for _uri, _body in ((f"/match/{mid}", m), (f"/match/{mid}/stage", "squad")):
            _sent = notify_messenger_push(_uri, _body)
            _mm_debug(f"bridge push {_uri} delivered={_sent}")
    except Exception as e:
        _mm_debug(f"bridge ERROR rid={rid_int} match={mid}: {e!r}\n{traceback.format_exc()}")


def _complete_matchmaking_async(rid_int, uname):
    """Background callback fired 2.0s after POST /room/<id>/matchmaking so the client first
    processes stage='searching' (enabling Cancel Matchmaking) and then transitions cleanly
    to stage='matched' + MatchEvent.ON_SQUADING via Messenger TCP push."""
    _mm_debug(f"complete start rid={rid_int} user={uname}")
    try:
        with hc.STATE_LOCK:
            _MATCHMAKING_TIMERS.pop(rid_int, None)
            _CURRENT_USER.username = uname
            room = ACTIVE_ROOMS.get(rid_int)
            if not room or room.get("stage") != "searching":
                _mm_debug(f"complete ABORT rid={rid_int}: room stage="
                          f"{(room or {}).get('stage')!r} (expected 'searching')")
                return
            st = _state()
            match, updated_room = _build_room_match_and_update(rid_int, st, room)
            if match is None:
                _mm_debug(f"complete ABORT rid={rid_int}: _build_room_match_and_update returned None")
                return
            match_res = {"resource": f"/match/{match['id']}", "uri": f"/match/{match['id']}", "body": match}
            updated_room["matchmaking"] = {"multiplematch": False, "resources": [match_res]}
            updated_room["stage"] = "matched"
            ACTIVE_ROOMS[rid_int] = updated_room
            st["active_room"] = updated_room
            _save(st)

        _rteam = match['side']['right']['team']
        _mm_debug(f"complete built rid={rid_int} match={match['id']} type={match.get('type')} stage={match.get('stage')} "
                  f"left.ready={match['side']['left']['ready']} right.ready={match['side']['right']['ready']} "
                  f"right.players={len(match['side']['right']['players'])} "
                  f"right.teamid={_rteam.get('teamid')} right.teamplayers={len(_rteam.get('players') or [])} "
                  f"right.positions={len((_rteam.get('squad') or {}).get('positions') or [])} "
                  f"leagueparam={'leagueparam' in match} relay={'relayServer' in match}")

        # Slim room payload for Messenger TCP push (full room is ~92KB, over the
        # 65,000-byte messenger limit). Keep the SAME shape as a real room
        # resource: the old slim payload had ranked/ladder as booleans and no
        # "side" at all, unlike every other room the server sends -- a client
        # reading room.ranked.elo or room.side.* on it cannot proceed.
        _room_elo = int(((updated_room.get("ranked") or {}).get("elo"))
                        or ((updated_room.get("ladder") or {}).get("elo")) or LADDER_START_ELO)

        # Client evidence (telemetry 2026-11-11): with identity-only sides the
        # client died in RoomController/processRoomPlayersResource with
        # TypeError #1009 (null object reference) the moment this push arrived,
        # and the matchmaking transition aborted. Room player entries and sides
        # must keep the same structure _make_room_resource produces -- entries
        # carry summary/squad/team, sides carry squad/team. Slimming may only
        # strip bulk: staticdata inside teams, and a player entry's full team
        # object (the left entry in _make_room_resource already uses just
        # {"teamid", "summary"} there).
        def _slim_room_player(pl):
            if not isinstance(pl, dict):
                return pl
            p = dict(pl)
            t = p.get("team")
            if isinstance(t, dict) and t.get("players"):
                p["team"] = {"teamid": t.get("teamid"), "summary": t.get("summary")}
            return p

        def _slim_room_side(side_dict):
            side_dict = side_dict or {}
            out = {"ready": bool(side_dict.get("ready")),
                   "authTeamId": side_dict.get("authTeamId", 0),
                   "players": [_slim_room_player(pl) for pl in side_dict.get("players") or []]}
            if "squad" in side_dict:
                out["squad"] = side_dict["squad"]
            if isinstance(side_dict.get("team"), dict):
                out["team"] = _slim_team(side_dict["team"])
            return out

        _upd_side = updated_room.get("side") or {}
        slim_room = {
            "id": rid_int, "roomid": rid_int, "uri": f"/room/{rid_int}",
            "type": updated_room.get("type", room.get("type", "ranked")), "stage": "matched",
            "slotsize": updated_room.get("slotsize", room.get("slotsize", 2)),
            "host": updated_room.get("host", ""),
            "observermode": False, "shootoutmode": True, "drawmode": False,
            "properties": updated_room.get("properties") or {"publicmode": True},
            "AI": updated_room.get("AI") or {"index": {"nation": 0, "league": 0, "team": 0, "difficulty": 2}},
            "ranked": {"elo": _room_elo}, "ladder": {"elo": _room_elo},
            "roles": updated_room.get("roles") or {},
            "side": {
                "left": _slim_room_side(_upd_side.get("left")),
                "center": {"players": []},
                "right": _slim_room_side(_upd_side.get("right")),
                "referee": {"players": []},
            },
            "members": [_slim_room_player(pl) for pl in updated_room.get("members") or []],
            "matchmaking": {"multiplematch": False, "resources": [{"resource": f"/match/{match['id']}", "uri": f"/match/{match['id']}"}]}
        }
        for _uri, _body in (
            (f"/room/{rid_int}/matchmaking", updated_room["matchmaking"]),
            (f"/room/{rid_int}/stage", "matched"),
            (f"/room/{rid_int}", slim_room),
            (f"/match/{match['id']}", match),
            (f"/match/{match['id']}/stage", match["stage"]),
        ):
            _sent = notify_messenger_push(_uri, _body)
            try:
                _size = len(json.dumps({"uri": _uri, "body": _body}, ensure_ascii=False).encode("utf-8"))
            except Exception:
                _size = -1
            _mm_debug(f"push {_uri} bytes={_size} delivered={_sent}")
        _mm_debug(f"complete done rid={rid_int} match={match['id']}")
        # Issue 2h: the client ingests a "created" room match but never
        # advances it (see _bridge_room_match_created) -- bridge the one
        # created -> squad step from the server side shortly after ingestion.
        _bt = threading.Timer(1.5, _bridge_room_match_created, args=(rid_int, uname, match["id"]))
        _bt.daemon = True
        _bt.start()
    except Exception as e:
        _mm_debug(f"complete ERROR rid={rid_int}: {e!r}\n{traceback.format_exc()}")
        print(f"Error completing matchmaking: {e}", flush=True)


def ep_room_matchmaking(rid, query, body):
    """POST /room/<id>/matchmaking or DELETE /room/<id>/matchmaking."""
    try:
        req = json.loads(body.decode("utf-8")) if body else {}
    except Exception:
        req = {}
    st = _state()
    rid_int = int(rid) if str(rid).isdigit() else 1
    room = ACTIVE_ROOMS.get(rid_int) or st.get("active_room") or _make_room_resource(rid_int, st)
    _cancel_matchmaking_timer(rid_int)

    is_delete = (
        (isinstance(req, dict) and (req.get("_method") == "DELETE" or req.get("cancel")))
        or "_method=DELETE" in str(query)
        or "cancel" in str(query)
    )
    if is_delete:
        _mm_debug(f"matchmaking CANCEL rid={rid_int} room stage was={room.get('stage')!r}")
        room["stage"] = "room"
        room["matchmaking"] = {"multiplematch": False, "resources": []}
        ACTIVE_ROOMS[rid_int] = room
        st["active_room"] = room
        _save(st)
        return hc._json({"result": True, "resources": [{"uri": f"/room/{rid_int}", "body": room}]})

    # Set searching state so client shows searching progress and can cancel cleanly
    room["stage"] = "searching"
    room["matchmaking"] = {"multiplematch": False, "resources": []}
    ACTIVE_ROOMS[rid_int] = room
    st["active_room"] = room
    _save(st)

    # Schedule background matchmaking completion after 2.0 seconds
    uname = _CURRENT_USER.username or "default"
    _mm_debug(f"matchmaking SEARCHING rid={rid_int} user={uname} type={room.get('type')!r}")
    tmr = threading.Timer(2.0, _complete_matchmaking_async, args=(rid_int, uname))
    tmr.daemon = True
    _MATCHMAKING_TIMERS[rid_int] = tmr
    tmr.start()

    return hc._json({
        "result": True,
        "stage": "searching",
        "matchmaking": {
            "multiplematch": False,
            "resources": [],
        },
        "resources": [
            {"uri": f"/room/{rid_int}", "body": room},
        ],
    })
    if match is None:
        return hc._json({"result": False})
    return hc._json({
        "result": True,
        "id": match["id"],
        "match": match,
        "room": updated_room,
        "resources": [
            {"uri": f"/room/{rid_int}", "body": updated_room},
            {"uri": f"/match/{match['id']}", "body": match},
        ],
    })


def ep_room_side_ready(rid, side, query, body):
    """PUT /room/<id>/side/<side>/ready: clicked Ready in Exhibition / Friendly / Multi-player Room."""
    try:
        req = json.loads(body.decode("utf-8")) if body else {}
    except Exception:
        req = {}
    st = _state()
    rid_int = int(rid) if str(rid).isdigit() else 1
    room = ACTIVE_ROOMS.get(rid_int) or st.get("active_room") or _make_room_resource(rid_int, st, room_type="pvp_exhibition")
    ready_val = bool(req.get("ready", True))
    if ready_val:
        return _start_room_match(rid_int, st, room)
    room["stage"] = "room"
    if "side" in room and side in room["side"]:
        room["side"][side]["ready"] = False
    ACTIVE_ROOMS[rid_int] = room
    st["active_room"] = room
    _save(st)
    return hc._json({"result": True, "resources": [{"uri": f"/room/{rid_int}", "body": room}]})


def ep_room_invite_reject(rid, fid, query, body):
    """POST /room/<rid>/invite/rejection/from/<fid>: handles friendly match invite rejection."""
    return hc._json({"result": True})

def ep_room_invite(rid, target_cid, query, body):
    """POST /room/<rid>/invite/<target_cid>: record the invitation ONLY.

    The old build installed the friend's full side as a ready captain the
    moment they were invited -- an account that was not even logged in
    "joined", became captain and readied itself (user report 2026-11-12,
    GM3 pulled into GM2's room while offline). That mirrored upstream's
    virtual managers, who auto-join by design; real accounts join from
    their own client. Until then the right side stays empty, and starting
    the match falls back to the CPU side."""
    st = _state()
    rid_int = int(rid) if str(rid).isdigit() else 1
    room = ACTIVE_ROOMS.get(rid_int) or st.get("active_room") or _make_room_resource(rid_int, st, room_type="pvp_sim")
    room["invited"] = str(target_cid)
    ACTIVE_ROOMS[rid_int] = room
    st["active_room"] = room
    _save(st)
    notify_messenger_push(f"/room/{rid_int}", room)
    return hc._json({"result": True, "resources": [{"uri": f"/room/{rid_int}", "body": room}]})


def ep_room_update(rid, action, query, body):
    """Handles PUT /room/<id>/<action> (my/side, back, AI, type, slotsize, messages, etc.)."""
    try:
        req = json.loads(body.decode("utf-8")) if body else {}
    except Exception:
        req = {}
    st = _state()
    rid_int = int(rid) if str(rid).isdigit() else 1
    room = ACTIVE_ROOMS.get(rid_int) or st.get("active_room") or _make_room_resource(rid_int, st)
    if action == "type" and req.get("type"):
        room["type"] = str(req["type"])
    elif action == "slotsize" and req.get("slotsize"):
        room["slotsize"] = int(req["slotsize"])
    elif action == "AI" and isinstance(req.get("index"), dict):
        room["AI"] = {"index": req["index"]}
    elif action == "back":
        room["stage"] = "room"
    elif action == "messages" and req.get("message"):
        msg_vo = {
            "type": "chat",
            "coachname": (hc.my_coach(st) or {}).get("name", "Manager"),
            "message": str(req.get("message"))[:200],
        }
        notify_messenger_push(f"/room/{rid_int}/messages/latest", msg_vo)
        return hc._json({"result": True, "resources": [{"uri": f"/room/{rid_int}/messages/latest", "body": msg_vo}]})
    ACTIVE_ROOMS[rid_int] = room
    st["active_room"] = room
    _save(st)
    return hc._json({"result": True, "resources": [{"uri": f"/room/{rid_int}", "body": room}]})


def ep_exhibition_team_change(rid, query, body):
    """POST /exhibition/room/<id>/team: change authentic team in Exhibition room."""
    try:
        req = json.loads(body.decode("utf-8")) if body else {}
    except Exception:
        req = {}
    st = _state()
    rid_int = int(rid) if str(rid).isdigit() else 1
    room = ACTIVE_ROOMS.get(rid_int) or st.get("active_room") or _make_room_resource(rid_int, st, room_type="pvp_exhibition")
    side = str(req.get("side") or "left")
    tid = int(req.get("teamid") or 1)
    if "side" in room and side in room["side"]:
        room["side"][side]["authTeamId"] = tid
        for p in room["side"][side].get("players") or []:
            p["teamid"] = tid
    ACTIVE_ROOMS[rid_int] = room
    st["active_room"] = room
    _save(st)
    return hc._json({"result": True, "resources": [{"uri": f"/room/{rid_int}", "body": room}]})


def ep_exhibition_points(query, body):
    return hc._json({"result": True, "maxpoint": 20, "point": 0})


def ep_exhibition_authentic_team(teamid, query, body):
    return hc.ep_authentic_team("all", teamid, query, body)


def ep_sortleague_by_nation(nid, query, body):
    """GET /static/db/SORTLEAGUE/<nationId>: RoomProxy.onStaticDataFetched expects a JSON Array of league IDs."""
    try:
        by_nation = hc.club_index()[1]
        ls = by_nation.get(int(nid)) or []
        res = [lid for lid, _ in ls]
        if not res:
            res = [lid for llist in by_nation.values() for lid, _ in llist][:20]
    except Exception:
        res = [13, 16, 19, 31, 53]
    return hc._json(res or [13])


def ep_sortteam_by_league(lid, query, body):
    """GET /static/db/SORTTEAM/<leagueId>: RoomProxy.onStaticDataFetched expects a JSON Array of team IDs."""
    try:
        by_league = hc.club_index()[2]
        ts = list(by_league.get(int(lid)) or [])
        if not ts:
            ts = list(EURO_CLUBS)
    except Exception:
        ts = list(EURO_CLUBS)
    return hc._json(ts)


# =========================================================================== #
# 4B. COACH INFO: ACHIEVEMENTS (NHIỆM VỤ HLV), RANKING & FRIENDS (KẾT BẠN)    #
# =========================================================================== #

STATIC_BADGES_LIST = [
    {"badgeid": 1001, "itemid": 1001, "asset": "1001", "name": "Khởi Nghiệp", "desc": "Cột mốc tân thủ khai phá đấu trường từ Lv.2 đến Lv.10.", "icon": "1001", "itemicon": "1001", "category": "team", "image_effect": 0, "itemback": 0},
    {"badgeid": 1002, "itemid": 1002, "asset": "1002", "name": "Bình Minh", "desc": "Bứt phá kỹ năng bóng đá từ Lv.11 đến Lv.25.", "icon": "1002", "itemicon": "1002", "category": "team", "image_effect": 0, "itemback": 0},
    {"badgeid": 1003, "itemid": 1003, "asset": "1003", "name": "Trưởng Thành", "desc": "Thử thách chuyên môn và chiến thuật từ Lv.26 đến Lv.45.", "icon": "1003", "itemicon": "1003", "category": "team", "image_effect": 0, "itemback": 0},
    {"badgeid": 1004, "itemid": 1004, "asset": "1004", "name": "Chuyên Nghiệp", "desc": "Khẳng định bản lĩnh đỉnh cao từ Lv.46 đến Lv.65.", "icon": "1004", "itemicon": "1004", "category": "team", "image_effect": 0, "itemback": 0},
    {"badgeid": 1005, "itemid": 1005, "asset": "1005", "name": "Đẳng Cấp", "desc": "Bậc thầy chiến thuật sân cỏ từ Lv.66 đến Lv.85.", "icon": "1005", "itemicon": "1005", "category": "team", "image_effect": 0, "itemback": 0},
    {"badgeid": 1006, "itemid": 1006, "asset": "1006", "name": "Huyền Thoại", "desc": "Chinh phục đỉnh cao danh vọng từ Lv.86 đến Lv.100.", "icon": "1006", "itemicon": "1006", "category": "team", "image_effect": 0, "itemback": 0},
    {"badgeid": 900001, "itemid": 900001, "asset": "misc", "name": "Đặc Biệt", "desc": "Hoạt động trải nghiệm bóng đá và các tính năng trong game.", "icon": "900001", "itemicon": "900001", "category": "team", "image_effect": 0, "itemback": 0},
    {"badgeid": 900017, "itemid": 900017, "asset": "900017", "name": "Độc Hành", "desc": "Chinh phục chế độ Xếp hạng 1v1 bằng những chiến thắng liên tiếp.", "icon": "900017", "itemicon": "900017", "category": "team", "image_effect": 0, "itemback": 0},
    {"badgeid": 900018, "itemid": 900018, "asset": "900018", "name": "Đỉnh Cao", "desc": "Vươn tới những mốc điểm Elo cao nhất trên bảng xếp hạng.", "icon": "900018", "itemicon": "900018", "category": "team", "image_effect": 0, "itemback": 0},
    {"badgeid": 900020, "itemid": 900020, "asset": "900020", "name": "Chiến Lược Gia", "desc": "Bậc thầy cầm quân ở chế độ Đấu giả lập (HLV trưởng).", "icon": "900020", "itemicon": "900020", "category": "team", "image_effect": 0, "itemback": 0},
    {"badgeid": 900021, "itemid": 900021, "asset": "900021", "name": "Đường Đua", "desc": "Kiên trì chinh chiến qua các vòng đấu của mùa giải League.", "icon": "900021", "itemicon": "900021", "category": "team", "image_effect": 0, "itemback": 0},
    {"badgeid": 900023, "itemid": 900023, "asset": "900023", "name": "Săn Cup", "desc": "Có mặt ở mọi đấu trường Cup trong game.", "icon": "900023", "itemicon": "900023", "category": "team", "image_effect": 0, "itemback": 0},
    {"badgeid": 900028, "itemid": 900028, "asset": "900028", "name": "Máy Dội Bom", "desc": "Cỗ máy ghi bàn không ngừng nghỉ ở đấu trường Xếp hạng.", "icon": "900028", "itemicon": "900028", "category": "team", "image_effect": 0, "itemback": 0},
    {"badgeid": 900035, "itemid": 900035, "asset": "900035", "name": "Trùm Nâng Cấp", "desc": "Bàn tay vàng trong làng ép thẻ cầu thủ.", "icon": "900035", "itemicon": "900035", "category": "team", "image_effect": 0, "itemback": 0},
    {"badgeid": 900037, "itemid": 900037, "asset": "900037", "name": "Bức Tường Thép", "desc": "Hàng phòng ngự không thể xuyên phá ở Xếp hạng 1v1.", "icon": "900037", "itemicon": "900037", "category": "team", "image_effect": 0, "itemback": 0},
    {"badgeid": 900036, "itemid": 900036, "asset": "900036", "name": "Nhà Vô Địch Cup", "desc": "Chuyên gia chinh phục các giải Cup trong game.", "icon": "900036", "itemicon": "900036", "category": "team", "image_effect": 0, "itemback": 0},
    {"badgeid": 900038, "itemid": 900038, "asset": "900038", "name": "Chiến Binh Đấu Cup", "desc": "Chinh chiến không mệt mỏi ở mọi đấu trường Cup.", "icon": "900038", "itemicon": "900038", "category": "team", "image_effect": 0, "itemback": 0},
    {"badgeid": 900039, "itemid": 900039, "asset": "900039", "name": "Ông Trùm Sân Cỏ", "desc": "Bậc thầy chiến thắng ở mọi chế độ đấu phòng.", "icon": "900039", "itemicon": "900039", "category": "team", "image_effect": 0, "itemback": 0},
    {"badgeid": 900046, "itemid": 900046, "asset": "900046", "name": "Chuyên Gia Thi Đấu", "desc": "Thi đấu bền bỉ qua hàng trăm trận đấu phòng.", "icon": "900046", "itemicon": "900046", "category": "team", "image_effect": 0, "itemback": 0},
    {"badgeid": 900050, "itemid": 900050, "asset": "900050", "name": "Đội Hình Tỷ Phú", "desc": "Sở hữu đội hình giá trị bậc nhất sân cỏ.", "icon": "900050", "itemicon": "900050", "category": "team", "image_effect": 0, "itemback": 0},
]

STATIC_ACHIEVEMENT_INFO = {
    "mixed": {
        "ach_1": ["ach_1_1", "ach_1_2", "ach_1_3", "ach_1_4", "ach_1_5"],
        "ach_2": ["ach_2_1", "ach_2_2", "ach_2_3", "ach_2_4", "ach_2_5"],
        "ach_3": ["ach_3_1", "ach_3_2", "ach_3_3", "ach_3_4"],
        "ach_4": ["ach_4_1", "ach_4_2", "ach_4_3", "ach_4_4"],
        "ach_5": ["ach_5_1", "ach_5_2", "ach_5_3", "ach_5_4"],
        "ach_6": ["ach_6_1", "ach_6_2", "ach_6_3", "ach_6_4"],
        "ach_7": ["ach_7_1", "ach_7_6", "ach_7_2", "ach_7_3", "ach_7_7", "ach_7_4", "ach_7_8", "ach_7_5"],
        "ach_8": ["ach_8_5", "ach_8_1", "ach_8_6", "ach_8_2", "ach_8_7", "ach_8_3", "ach_8_8", "ach_8_4"],
        "ach_9": ["ach_9_1", "ach_9_5", "ach_9_2", "ach_9_6", "ach_9_3", "ach_9_7", "ach_9_4", "ach_9_8"],
        "ach_10": ["ach_10_1", "ach_10_5", "ach_10_2", "ach_10_6", "ach_10_3", "ach_10_7", "ach_10_4", "ach_10_8"],
        "ach_11": ["ach_11_1", "ach_11_2", "ach_11_3"],
        "ach_12": ["ach_12_1", "ach_12_2", "ach_12_3", "ach_12_4", "ach_12_5", "ach_12_6"],
        "ach_13": ["ach_13_1", "ach_13_2", "ach_13_3", "ach_13_4", "ach_13_5", "ach_13_6"],
        "ach_14": ["ach_14_1", "ach_14_2", "ach_14_3", "ach_14_4", "ach_14_5"],
        "misc": ["misc_1", "misc_2", "misc_3", "misc_4", "misc_5", "misc_6", "misc_7", "misc_8"],
    },
    "list": [
        # Badge 1: Khởi Nghiệp (Lv.2 - Lv.10)
        {"id": "ach_1", "category": "team", "type": "normal", "name": "Khởi Nghiệp", "title": "Khởi Nghiệp", "desc": "Nâng cấp độ HLV lên cấp 10.", "difficulty": 1, "steps": 5, "grade": 1, "target": 10, "point": 50, "badge": 1001, "rewards": [{"todo": 10, "reward": [{"itemid": 1, "count": 5000000}]}]},
        {"id": "ach_1_1", "category": "team", "type": "normal", "name": "Tân Thủ Cấp 2", "title": "Tân Thủ Cấp 2", "desc": "Nâng cấp độ HLV lên cấp 2 và sắp xếp chiến thuật đội hình.", "difficulty": 1, "steps": 1, "grade": 1, "target": 2, "point": 10, "badge": 1001, "rewards": [{"todo": 2, "reward": [{"itemid": 1, "count": 500000}]}]},
        {"id": "ach_1_2", "category": "team", "type": "normal", "name": "Thị Trường Cấp 4", "title": "Thị Trường Cấp 4", "desc": "Nâng cấp độ HLV lên cấp 4 và giao dịch trên Thị Trường Chuyển Nhượng.", "difficulty": 1, "steps": 1, "grade": 1, "target": 4, "point": 10, "badge": 1001, "rewards": [{"todo": 4, "reward": [{"itemid": 1, "count": 1000000}]}]},
        {"id": "ach_1_3", "category": "team", "type": "normal", "name": "Nâng Cấp Thẻ Cấp 6", "title": "Nâng Cấp Thẻ Cấp 6", "desc": "Nâng cấp độ HLV lên cấp 6 và thực hiện nâng cấp thẻ cầu thủ.", "difficulty": 1, "steps": 1, "grade": 1, "target": 6, "point": 10, "badge": 1001, "rewards": [{"todo": 6, "reward": [{"itemid": 1, "count": 1500000}]}]},
        {"id": "ach_1_4", "category": "team", "type": "normal", "name": "Đấu Giải Cấp 8", "title": "Đấu Giải Cấp 8", "desc": "Nâng cấp độ HLV lên cấp 8 và thi đấu trong giải vô địch quốc gia (League).", "difficulty": 1, "steps": 1, "grade": 1, "target": 8, "point": 10, "badge": 1001, "rewards": [{"todo": 8, "reward": [{"itemid": 1, "count": 2000000}]}]},
        {"id": "ach_1_5", "category": "team", "type": "normal", "name": "Cột Mốc Cấp 10", "title": "Cột Mốc Cấp 10", "desc": "Đạt mốc cấp độ HLV Lv.10 để mở khóa các tính năng nâng cao.", "difficulty": 1, "steps": 1, "grade": 1, "target": 10, "point": 10, "badge": 1001, "rewards": [{"todo": 10, "reward": [{"itemid": 1, "count": 3000000}]}]},

        # Badge 2: Bình Minh (Lv.11 - Lv.25)
        {"id": "ach_2", "category": "match", "type": "normal", "name": "Bình Minh", "title": "Bình Minh", "desc": "Nâng cấp độ HLV lên cấp 25.", "difficulty": 1, "steps": 5, "grade": 1, "target": 25, "point": 70, "badge": 1002, "rewards": [{"todo": 25, "reward": [{"itemid": 1, "count": 15000000}]}]},
        {"id": "ach_2_1", "category": "match", "type": "normal", "name": "HLV Cấp 12", "title": "HLV Cấp 12", "desc": "Nâng cấp độ HLV lên cấp 12 và mở gói cầu thủ may mắn.", "difficulty": 1, "steps": 1, "grade": 1, "target": 12, "point": 15, "badge": 1002, "rewards": [{"todo": 12, "reward": [{"itemid": 1, "count": 2500000}]}]},
        {"id": "ach_2_2", "category": "match", "type": "normal", "name": "Thử Lửa Cấp 15", "title": "Thử Lửa Cấp 15", "desc": "Nâng cấp độ HLV lên cấp 15 và thi đấu Đấu Xếp Hạng 1v1.", "difficulty": 1, "steps": 1, "grade": 1, "target": 15, "point": 15, "badge": 1002, "rewards": [{"todo": 15, "reward": [{"itemid": 1, "count": 3500000}]}]},
        {"id": "ach_2_3", "category": "match", "type": "normal", "name": "Giả Lập Cấp 18", "title": "Giả Lập Cấp 18", "desc": "Nâng cấp độ HLV lên cấp 18 và tham gia Giả Lập Xếp Hạng (Manager Mode).", "difficulty": 1, "steps": 1, "grade": 1, "target": 18, "point": 15, "badge": 1002, "rewards": [{"todo": 18, "reward": [{"itemid": 1, "count": 4500000}]}]},
        {"id": "ach_2_4", "category": "match", "type": "normal", "name": "Giao Hữu Cấp 21", "title": "Giao Hữu Cấp 21", "desc": "Nâng cấp độ HLV lên cấp 21 và thi đấu giao hữu cùng bạn bè.", "difficulty": 1, "steps": 1, "grade": 1, "target": 21, "point": 15, "badge": 1002, "rewards": [{"todo": 21, "reward": [{"itemid": 1, "count": 6000000}]}]},
        {"id": "ach_2_5", "category": "match", "type": "normal", "name": "Bứt Phá Cấp 25", "title": "Bứt Phá Cấp 25", "desc": "Chinh phục cấp độ HLV Lv.25 và gia tăng giá trị đội hình.", "difficulty": 1, "steps": 1, "grade": 1, "target": 25, "point": 20, "badge": 1002, "rewards": [{"todo": 25, "reward": [{"itemid": 1, "count": 8000000}]}]},

        # Badge 3: Trưởng Thành (Lv.26 - Lv.45)
        {"id": "ach_3", "category": "match", "type": "normal", "name": "Trưởng Thành", "title": "Trưởng Thành", "desc": "Nâng cấp độ HLV lên cấp 45.", "difficulty": 2, "steps": 4, "grade": 1, "target": 45, "point": 110, "badge": 1003, "rewards": [{"todo": 45, "reward": [{"itemid": 1, "count": 25000000}]}]},
        {"id": "ach_3_1", "category": "match", "type": "normal", "name": "Thẻ Bạc Cấp 30", "title": "Thẻ Bạc Cấp 30", "desc": "Nâng cấp độ HLV lên cấp 30 và sở hữu cầu thủ nâng cấp +3 trở lên.", "difficulty": 2, "steps": 1, "grade": 1, "target": 30, "point": 25, "badge": 1003, "rewards": [{"todo": 30, "reward": [{"itemid": 1, "count": 10000000}]}]},
        {"id": "ach_3_2", "category": "match", "type": "normal", "name": "Ban Huấn Luyện Cấp 35", "title": "Ban Huấn Luyện Cấp 35", "desc": "Nâng cấp độ HLV lên cấp 35 và chiêu mộ nhân viên ban huấn luyện.", "difficulty": 2, "steps": 1, "grade": 1, "target": 35, "point": 25, "badge": 1003, "rewards": [{"todo": 35, "reward": [{"itemid": 1, "count": 12000000}]}]},
        {"id": "ach_3_3", "category": "match", "type": "normal", "name": "Chiến Thắng Cấp 40", "title": "Chiến Thắng Cấp 40", "desc": "Nâng cấp độ HLV lên cấp 40 và giành 5 trận thắng liên tiếp.", "difficulty": 2, "steps": 1, "grade": 1, "target": 40, "point": 30, "badge": 1003, "rewards": [{"todo": 40, "reward": [{"itemid": 1, "count": 15000000}]}]},
        {"id": "ach_3_4", "category": "match", "type": "normal", "name": "Vững Vàng Cấp 45", "title": "Vững Vàng Cấp 45", "desc": "Chinh phục mốc cấp độ HLV Lv.45 và hoàn thiện lối chơi.", "difficulty": 2, "steps": 1, "grade": 1, "target": 45, "point": 30, "badge": 1003, "rewards": [{"todo": 45, "reward": [{"itemid": 1, "count": 20000000}]}]},

        # Badge 4: Chuyên Nghiệp (Lv.46 - Lv.65)
        {"id": "ach_4", "category": "match", "type": "normal", "name": "Chuyên Nghiệp", "title": "Chuyên Nghiệp", "desc": "Nâng cấp độ HLV lên cấp 65.", "difficulty": 2, "steps": 4, "grade": 1, "target": 65, "point": 120, "badge": 1004, "rewards": [{"todo": 65, "reward": [{"itemid": 1, "count": 40000000}]}]},
        {"id": "ach_4_1", "category": "match", "type": "normal", "name": "Đẳng Cấp Cấp 50", "title": "Đẳng Cấp Cấp 50", "desc": "Nâng cấp độ HLV lên cấp 50 và xây dựng giá trị đội hình trên 100 triệu EP.", "difficulty": 2, "steps": 1, "grade": 1, "target": 50, "point": 30, "badge": 1004, "rewards": [{"todo": 50, "reward": [{"itemid": 1, "count": 22000000}]}]},
        {"id": "ach_4_2", "category": "match", "type": "normal", "name": "Thăng Hạng Cấp 55", "title": "Thăng Hạng Cấp 55", "desc": "Nâng cấp độ HLV lên cấp 55 và thăng hạng trong Đấu Xếp Hạng.", "difficulty": 2, "steps": 1, "grade": 1, "target": 55, "point": 30, "badge": 1004, "rewards": [{"todo": 55, "reward": [{"itemid": 1, "count": 25000000}]}]},
        {"id": "ach_4_3", "category": "match", "type": "normal", "name": "Vô Địch Cup Cấp 60", "title": "Vô Địch Cup Cấp 60", "desc": "Nâng cấp độ HLV lên cấp 60 và tham gia giải đấu Cup quốc tế.", "difficulty": 2, "steps": 1, "grade": 1, "target": 60, "point": 30, "badge": 1004, "rewards": [{"todo": 60, "reward": [{"itemid": 1, "count": 30000000}]}]},
        {"id": "ach_4_4", "category": "match", "type": "normal", "name": "Cao Thủ Cấp 65", "title": "Cao Thủ Cấp 65", "desc": "Đạt mốc cấp độ HLV Lv.65 và dẫn dắt đội bóng đẳng cấp.", "difficulty": 2, "steps": 1, "grade": 1, "target": 65, "point": 30, "badge": 1004, "rewards": [{"todo": 65, "reward": [{"itemid": 1, "count": 35000000}]}]},

        # Badge 5: Đẳng Cấp (Lv.66 - Lv.85)
        {"id": "ach_5", "category": "match", "type": "normal", "name": "Đẳng Cấp", "title": "Đẳng Cấp", "desc": "Nâng cấp độ HLV lên cấp 85.", "difficulty": 3, "steps": 4, "grade": 1, "target": 85, "point": 140, "badge": 1005, "rewards": [{"todo": 85, "reward": [{"itemid": 1, "count": 60000000}]}]},
        {"id": "ach_5_1", "category": "match", "type": "normal", "name": "Bậc Thầy Cấp 70", "title": "Bậc Thầy Cấp 70", "desc": "Nâng cấp độ HLV lên cấp 70 và nâng cấp thẻ cầu thủ lên cấp bạc (+5).", "difficulty": 3, "steps": 1, "grade": 1, "target": 70, "point": 35, "badge": 1005, "rewards": [{"todo": 70, "reward": [{"itemid": 1, "count": 40000000}]}]},
        {"id": "ach_5_2", "category": "match", "type": "normal", "name": "Chuyên Nghiệp Cấp 75", "title": "Chuyên Nghiệp Cấp 75", "desc": "Nâng cấp độ HLV lên cấp 75 và đạt thứ hạng cao trong Đấu Xếp Hạng.", "difficulty": 3, "steps": 1, "grade": 1, "target": 75, "point": 35, "badge": 1005, "rewards": [{"todo": 75, "reward": [{"itemid": 1, "count": 45000000}]}]},
        {"id": "ach_5_3", "category": "match", "type": "normal", "name": "Bất Bại Cấp 80", "title": "Bất Bại Cấp 80", "desc": "Nâng cấp độ HLV lên cấp 80 và sở hữu đội hình toàn diện.", "difficulty": 3, "steps": 1, "grade": 1, "target": 80, "point": 35, "badge": 1005, "rewards": [{"todo": 80, "reward": [{"itemid": 1, "count": 50000000}]}]},
        {"id": "ach_5_4", "category": "match", "type": "normal", "name": "Danh Tiếng Cấp 85", "title": "Danh Tiếng Cấp 85", "desc": "Đạt mốc cấp độ HLV Lv.85 và trở thành chiến lược gia hàng đầu.", "difficulty": 3, "steps": 1, "grade": 1, "target": 85, "point": 35, "badge": 1005, "rewards": [{"todo": 85, "reward": [{"itemid": 1, "count": 55000000}]}]},

        # Badge 6: Huyền Thoại (Lv.86 - Lv.100)
        {"id": "ach_6", "category": "item", "type": "normal", "name": "Huyền Thoại", "title": "Huyền Thoại", "desc": "Chinh phục đỉnh cao HLV Lv.100.", "difficulty": 4, "steps": 4, "grade": 1, "target": 100, "point": 200, "badge": 1006, "rewards": [{"todo": 100, "reward": [{"itemid": 1, "count": 150000000}]}]},
        {"id": "ach_6_1", "category": "item", "type": "normal", "name": "Tỉ Phú Cấp 90", "title": "Tỉ Phú Cấp 90", "desc": "Nâng cấp độ HLV lên cấp 90 và đạt giá trị đội hình vượt mốc 1 Tỷ EP.", "difficulty": 4, "steps": 1, "grade": 1, "target": 90, "point": 50, "badge": 1006, "rewards": [{"todo": 90, "reward": [{"itemid": 1, "count": 65000000}]}]},
        {"id": "ach_6_2", "category": "item", "type": "normal", "name": "Sao Vàng Cấp 95", "title": "Sao Vàng Cấp 95", "desc": "Nâng cấp độ HLV lên cấp 95 và chạm tay vào thứ hạng Sao Vàng Đấu Hạng.", "difficulty": 4, "steps": 1, "grade": 1, "target": 95, "point": 50, "badge": 1006, "rewards": [{"todo": 95, "reward": [{"itemid": 1, "count": 75000000}]}]},
        {"id": "ach_6_3", "category": "item", "type": "normal", "name": "Thiên Tài Cấp 98", "title": "Thiên Tài Cấp 98", "desc": "Nâng cấp độ HLV lên cấp 98 và sở hữu những thẻ huyền thoại thế giới.", "difficulty": 4, "steps": 1, "grade": 1, "target": 98, "point": 50, "badge": 1006, "rewards": [{"todo": 98, "reward": [{"itemid": 1, "count": 90000000}]}]},
        {"id": "ach_6_4", "category": "item", "type": "normal", "name": "Đỉnh Cao Cấp 100", "title": "Đỉnh Cao Cấp 100", "desc": "Chinh phục cột mốc vĩ đại tối thượng HLV Lv.100.", "difficulty": 4, "steps": 1, "grade": 1, "target": 100, "point": 50, "badge": 1006, "rewards": [{"todo": 100, "reward": [{"itemid": 1, "count": 120000000}]}]},

        # Misc: Đặc Biệt
        {"id": "misc", "category": "social", "type": "normal", "name": "Đặc Biệt", "title": "Đặc Biệt", "shortdesc": "", "desc": "Hoạt động trải nghiệm bóng đá và tính năng hấp dẫn.", "difficulty": 1, "steps": 8, "grade": 1, "target": 6, "point": 50, "badge": 900001, "rewards": [{"todo": 6, "reward": [{"itemid": 1, "count": 10000000}]}]},
        {"id": "misc_1", "category": "social", "type": "normal", "name": "Điểm Danh Hằng Ngày", "title": "Điểm Danh Hằng Ngày", "shortdesc": "", "desc": "Đăng nhập và quản lý đội bóng hôm nay.", "difficulty": 1, "steps": 1, "grade": 1, "target": 1, "point": 10, "badge": 900001, "rewards": [{"todo": 1, "reward": [{"itemid": 1, "count": 200000}]}]},
        {"id": "misc_2", "category": "match", "type": "normal", "name": "Chiến Binh Sân Cỏ", "title": "Chiến Binh Sân Cỏ", "shortdesc": "", "desc": "Thi đấu các trận giao hữu, giải đấu hoặc xếp hạng.", "difficulty": 1, "steps": 1, "grade": 1, "target": 1, "point": 15, "badge": 900001, "rewards": [{"todo": 1, "reward": [{"itemid": 1, "count": 500000}, {"itemid": 28001, "count": 1}]}]},
        {"id": "misc_3", "category": "team", "type": "normal", "name": "Xây Dựng Đội Hình", "title": "Xây Dựng Đội Hình", "shortdesc": "", "desc": "Sở hữu từ 11 cầu thủ trong danh sách đăng ký thi đấu.", "difficulty": 1, "steps": 1, "grade": 1, "target": 11, "point": 15, "badge": 900001, "metric": "players_owned", "rewards": [{"todo": 11, "reward": [{"itemid": 1, "count": 500000}, {"itemid": 28006, "count": 1}]}]},
        {"id": "misc_4", "category": "item", "type": "normal", "name": "Bậc Thầy Nâng Cấp", "title": "Bậc Thầy Nâng Cấp", "shortdesc": "", "desc": "Sở hữu một thẻ cầu thủ được nâng cấp lên +5 trong đội hình.", "difficulty": 1, "steps": 1, "grade": 1, "target": 5, "point": 10, "badge": 900001, "metric": "best_grade", "rewards": [{"todo": 5, "reward": [{"itemid": 1, "count": 1000000}, {"itemid": 82214068, "count": 1}]}]},

        # Badge 7: Độc Hành (thắng trận Xếp hạng 1v1) -- Bug 3 content expansion
        {"id": "ach_7", "category": "match", "type": "normal", "name": "Độc Hành", "title": "Độc Hành", "desc": "Giành 50 chiến thắng ở chế độ Xếp hạng 1v1.", "difficulty": 2, "steps": 8, "grade": 1, "target": 50, "point": 50, "badge": 900017, "rewards": [{"todo": 50, "reward": [{"itemid": 1, "count": 30000000}]}]},
        {"id": "ach_7_1", "category": "match", "type": "normal", "name": "Thắng Trận Xếp Hạng Đầu Tiên", "title": "Thắng Trận Xếp Hạng Đầu Tiên", "desc": "Giành chiến thắng đầu tiên ở chế độ Xếp hạng 1v1.", "difficulty": 1, "steps": 1, "grade": 1, "target": 1, "point": 10, "badge": 900017, "metric": "ranked_wins", "rewards": [{"todo": 1, "reward": [{"itemid": 1, "count": 1000000}]}]},
        {"id": "ach_7_2", "category": "match", "type": "normal", "name": "5 Chiến Thắng Xếp Hạng", "title": "5 Chiến Thắng Xếp Hạng", "desc": "Giành tổng cộng 5 chiến thắng ở chế độ Xếp hạng 1v1.", "difficulty": 1, "steps": 1, "grade": 1, "target": 5, "point": 10, "badge": 900017, "metric": "ranked_wins", "rewards": [{"todo": 5, "reward": [{"itemid": 1, "count": 2000000}]}]},
        {"id": "ach_7_3", "category": "match", "type": "normal", "name": "10 Chiến Thắng Xếp Hạng", "title": "10 Chiến Thắng Xếp Hạng", "desc": "Giành tổng cộng 10 chiến thắng ở chế độ Xếp hạng 1v1.", "difficulty": 2, "steps": 1, "grade": 1, "target": 10, "point": 10, "badge": 900017, "metric": "ranked_wins", "rewards": [{"todo": 10, "reward": [{"itemid": 1, "count": 4000000}, {"itemid": 28001, "count": 1}]}]},
        {"id": "ach_7_4", "category": "match", "type": "normal", "name": "25 Chiến Thắng Xếp Hạng", "title": "25 Chiến Thắng Xếp Hạng", "desc": "Giành tổng cộng 25 chiến thắng ở chế độ Xếp hạng 1v1.", "difficulty": 3, "steps": 1, "grade": 1, "target": 25, "point": 10, "badge": 900017, "metric": "ranked_wins", "rewards": [{"todo": 25, "reward": [{"itemid": 1, "count": 8000000}]}]},
        {"id": "ach_7_5", "category": "match", "type": "normal", "name": "50 Chiến Thắng Xếp Hạng", "title": "50 Chiến Thắng Xếp Hạng", "desc": "Giành tổng cộng 50 chiến thắng ở chế độ Xếp hạng 1v1.", "difficulty": 4, "steps": 1, "grade": 1, "target": 50, "point": 10, "badge": 900017, "metric": "ranked_wins", "rewards": [{"todo": 50, "reward": [{"itemid": 1, "count": 15000000}, {"itemid": 45013, "count": 1}]}]},

        # Badge 8: Đỉnh Cao Xếp Hạng (điểm Elo cao nhất)
        {"id": "ach_8", "category": "match", "type": "normal", "name": "Đỉnh Cao Xếp Hạng", "title": "Đỉnh Cao Xếp Hạng", "desc": "Đạt mốc điểm Elo cao nhất 1700 ở chế độ Xếp hạng 1v1.", "difficulty": 3, "steps": 8, "grade": 1, "target": 1700, "point": 60, "badge": 900018, "rewards": [{"todo": 1700, "reward": [{"itemid": 1, "count": 40000000}, {"itemid": 82214180, "count": 1}]}]},
        {"id": "ach_8_1", "category": "match", "type": "normal", "name": "Vượt Mốc 1100 Elo", "title": "Vượt Mốc 1100 Elo", "desc": "Đạt điểm Elo cao nhất 1100 (Nghiệp Dư A) ở Xếp hạng 1v1.", "difficulty": 1, "steps": 1, "grade": 1, "target": 1100, "point": 15, "badge": 900018, "metric": "best_elo", "rewards": [{"todo": 1100, "reward": [{"itemid": 1, "count": 2000000}]}]},
        {"id": "ach_8_2", "category": "match", "type": "normal", "name": "Vượt Mốc 1300 Elo", "title": "Vượt Mốc 1300 Elo", "desc": "Đạt điểm Elo cao nhất 1300 (Chuyên Nghiệp C) ở Xếp hạng 1v1.", "difficulty": 2, "steps": 1, "grade": 1, "target": 1300, "point": 15, "badge": 900018, "metric": "best_elo", "rewards": [{"todo": 1300, "reward": [{"itemid": 1, "count": 5000000}]}]},
        {"id": "ach_8_3", "category": "match", "type": "normal", "name": "Vượt Mốc 1500 Elo", "title": "Vượt Mốc 1500 Elo", "desc": "Đạt điểm Elo cao nhất 1500 (Thế Giới B) ở Xếp hạng 1v1.", "difficulty": 3, "steps": 1, "grade": 1, "target": 1500, "point": 15, "badge": 900018, "metric": "best_elo", "rewards": [{"todo": 1500, "reward": [{"itemid": 1, "count": 10000000}]}]},
        {"id": "ach_8_4", "category": "match", "type": "normal", "name": "Vượt Mốc 1700 Elo", "title": "Vượt Mốc 1700 Elo", "desc": "Đạt điểm Elo cao nhất 1700 (Huyền Thoại A) ở Xếp hạng 1v1.", "difficulty": 4, "steps": 1, "grade": 1, "target": 1700, "point": 15, "badge": 900018, "metric": "best_elo", "rewards": [{"todo": 1700, "reward": [{"itemid": 1, "count": 20000000}, {"itemid": 82214180, "count": 1}]}]},

        # Badge 9: Chiến Lược Gia (chế độ Đấu giả lập - HLV trưởng)
        {"id": "ach_9", "category": "match", "type": "normal", "name": "Chiến Lược Gia", "title": "Chiến Lược Gia", "desc": "Thi đấu 30 trận ở chế độ Đấu giả lập (HLV trưởng).", "difficulty": 2, "steps": 8, "grade": 1, "target": 30, "point": 50, "badge": 900020, "rewards": [{"todo": 30, "reward": [{"itemid": 1, "count": 25000000}]}]},
        {"id": "ach_9_1", "category": "match", "type": "normal", "name": "Trận Đấu Giả Lập Đầu Tiên", "title": "Trận Đấu Giả Lập Đầu Tiên", "desc": "Thi đấu trận đầu tiên ở chế độ Đấu giả lập (HLV trưởng).", "difficulty": 1, "steps": 1, "grade": 1, "target": 1, "point": 10, "badge": 900020, "metric": "mgr_games", "rewards": [{"todo": 1, "reward": [{"itemid": 1, "count": 1000000}]}]},
        {"id": "ach_9_2", "category": "match", "type": "normal", "name": "5 Trận Đấu Giả Lập", "title": "5 Trận Đấu Giả Lập", "desc": "Thi đấu tổng cộng 5 trận ở chế độ Đấu giả lập (HLV trưởng).", "difficulty": 1, "steps": 1, "grade": 1, "target": 5, "point": 10, "badge": 900020, "metric": "mgr_games", "rewards": [{"todo": 5, "reward": [{"itemid": 1, "count": 3000000}]}]},
        {"id": "ach_9_3", "category": "match", "type": "normal", "name": "15 Trận Đấu Giả Lập", "title": "15 Trận Đấu Giả Lập", "desc": "Thi đấu tổng cộng 15 trận ở chế độ Đấu giả lập (HLV trưởng).", "difficulty": 2, "steps": 1, "grade": 1, "target": 15, "point": 15, "badge": 900020, "metric": "mgr_games", "rewards": [{"todo": 15, "reward": [{"itemid": 1, "count": 6000000}, {"itemid": 28006, "count": 1}]}]},
        {"id": "ach_9_4", "category": "match", "type": "normal", "name": "30 Trận Đấu Giả Lập", "title": "30 Trận Đấu Giả Lập", "desc": "Thi đấu tổng cộng 30 trận ở chế độ Đấu giả lập (HLV trưởng).", "difficulty": 3, "steps": 1, "grade": 1, "target": 30, "point": 15, "badge": 900020, "metric": "mgr_games", "rewards": [{"todo": 30, "reward": [{"itemid": 1, "count": 12000000}]}]},

        # Badge 10: Đường Đua Giải Đấu (League)
        {"id": "ach_10", "category": "match", "type": "normal", "name": "Đường Đua Giải Đấu", "title": "Đường Đua Giải Đấu", "desc": "Hoàn thành 20 vòng đấu trong một mùa giải League.", "difficulty": 2, "steps": 8, "grade": 1, "target": 20, "point": 50, "badge": 900021, "rewards": [{"todo": 20, "reward": [{"itemid": 1, "count": 20000000}]}]},
        {"id": "ach_10_1", "category": "match", "type": "normal", "name": "Vòng Đấu Giải Đầu Tiên", "title": "Vòng Đấu Giải Đầu Tiên", "desc": "Hoàn thành vòng đấu đầu tiên trong một mùa giải League.", "difficulty": 1, "steps": 1, "grade": 1, "target": 1, "point": 10, "badge": 900021, "metric": "league_rounds", "rewards": [{"todo": 1, "reward": [{"itemid": 1, "count": 1000000}]}]},
        {"id": "ach_10_2", "category": "match", "type": "normal", "name": "5 Vòng Đấu Giải", "title": "5 Vòng Đấu Giải", "desc": "Hoàn thành 5 vòng đấu trong một mùa giải League.", "difficulty": 1, "steps": 1, "grade": 1, "target": 5, "point": 10, "badge": 900021, "metric": "league_rounds", "rewards": [{"todo": 5, "reward": [{"itemid": 1, "count": 2500000}]}]},
        {"id": "ach_10_3", "category": "match", "type": "normal", "name": "10 Vòng Đấu Giải", "title": "10 Vòng Đấu Giải", "desc": "Hoàn thành 10 vòng đấu trong một mùa giải League.", "difficulty": 2, "steps": 1, "grade": 1, "target": 10, "point": 15, "badge": 900021, "metric": "league_rounds", "rewards": [{"todo": 10, "reward": [{"itemid": 1, "count": 5000000}]}]},
        {"id": "ach_10_4", "category": "match", "type": "normal", "name": "20 Vòng Đấu Giải", "title": "20 Vòng Đấu Giải", "desc": "Hoàn thành 20 vòng đấu trong một mùa giải League.", "difficulty": 3, "steps": 1, "grade": 1, "target": 20, "point": 15, "badge": 900021, "metric": "league_rounds", "rewards": [{"todo": 20, "reward": [{"itemid": 1, "count": 10000000}, {"itemid": 45013, "count": 1}]}]},

        # Badge 11: Săn Cup (tham gia các giải Cup)
        {"id": "ach_11", "category": "match", "type": "normal", "name": "Săn Cup", "title": "Săn Cup", "desc": "Tham gia cả 3 giải Cup trong game.", "difficulty": 2, "steps": 3, "grade": 1, "target": 3, "point": 45, "badge": 900023, "rewards": [{"todo": 3, "reward": [{"itemid": 1, "count": 15000000}, {"itemid": 82214068, "count": 1}]}]},
        {"id": "ach_11_1", "category": "match", "type": "normal", "name": "Tham Gia Cup Đầu Tiên", "title": "Tham Gia Cup Đầu Tiên", "desc": "Đăng ký tham gia một giải Cup bất kỳ.", "difficulty": 1, "steps": 1, "grade": 1, "target": 1, "point": 10, "badge": 900023, "metric": "cups_joined", "rewards": [{"todo": 1, "reward": [{"itemid": 1, "count": 1000000}]}]},
        {"id": "ach_11_2", "category": "match", "type": "normal", "name": "Tham Gia 2 Giải Cup", "title": "Tham Gia 2 Giải Cup", "desc": "Đăng ký tham gia 2 giải Cup khác nhau.", "difficulty": 1, "steps": 1, "grade": 1, "target": 2, "point": 15, "badge": 900023, "metric": "cups_joined", "rewards": [{"todo": 2, "reward": [{"itemid": 1, "count": 3000000}]}]},
        {"id": "ach_11_3", "category": "match", "type": "normal", "name": "Tham Gia 3 Giải Cup", "title": "Tham Gia 3 Giải Cup", "desc": "Đăng ký tham gia cả 3 giải Cup trong game.", "difficulty": 2, "steps": 1, "grade": 1, "target": 3, "point": 20, "badge": 900023, "metric": "cups_joined", "rewards": [{"todo": 3, "reward": [{"itemid": 1, "count": 8000000}, {"itemid": 82214068, "count": 1}]}]},

        # Misc mở rộng: tài sản + tổng chiến thắng
        {"id": "misc_5", "category": "social", "type": "normal", "name": "Tỉ Phú Sân Cỏ", "title": "Tỉ Phú Sân Cỏ", "shortdesc": "", "desc": "Sở hữu số dư EP từ 50.000.000 trở lên.", "difficulty": 2, "steps": 1, "grade": 1, "target": 50000000, "point": 20, "badge": 900001, "metric": "ep_balance", "rewards": [{"todo": 50000000, "reward": [{"itemid": 1, "count": 5000000}, {"itemid": 45013, "count": 1}]}]},
        {"id": "misc_6", "category": "match", "type": "normal", "name": "Vua Sân Cỏ", "title": "Vua Sân Cỏ", "shortdesc": "", "desc": "Giành tổng cộng 20 chiến thắng ở Xếp hạng 1v1 và Đấu giả lập.", "difficulty": 3, "steps": 1, "grade": 1, "target": 20, "point": 20, "badge": 900001, "metric": "total_wins", "rewards": [{"todo": 20, "reward": [{"itemid": 1, "count": 5000000}, {"itemid": 82214180, "count": 1}]}]},
        {"id": "ach_7_6", "category": "match", "type": "normal", "name": "3 Chiến Thắng Xếp Hạng", "title": "3 Chiến Thắng Xếp Hạng", "desc": "Giành tổng cộng 3 chiến thắng ở chế độ Xếp hạng 1v1.", "difficulty": 1, "steps": 1, "grade": 1, "target": 3, "point": 10, "badge": 900017, "metric": "ranked_wins", "rewards": [{"todo": 3, "reward": [{"itemid": 1, "count": 1500000}]}]},
        {"id": "ach_7_7", "category": "match", "type": "normal", "name": "15 Chiến Thắng Xếp Hạng", "title": "15 Chiến Thắng Xếp Hạng", "desc": "Giành tổng cộng 15 chiến thắng ở chế độ Xếp hạng 1v1.", "difficulty": 2, "steps": 1, "grade": 1, "target": 15, "point": 10, "badge": 900017, "metric": "ranked_wins", "rewards": [{"todo": 15, "reward": [{"itemid": 1, "count": 6000000}]}]},
        {"id": "ach_7_8", "category": "match", "type": "normal", "name": "35 Chiến Thắng Xếp Hạng", "title": "35 Chiến Thắng Xếp Hạng", "desc": "Giành tổng cộng 35 chiến thắng ở chế độ Xếp hạng 1v1.", "difficulty": 3, "steps": 1, "grade": 1, "target": 35, "point": 10, "badge": 900017, "metric": "ranked_wins", "rewards": [{"todo": 35, "reward": [{"itemid": 1, "count": 12000000}]}]},
        {"id": "ach_8_5", "category": "match", "type": "normal", "name": "Vượt Mốc 1050 Elo", "title": "Vượt Mốc 1050 Elo", "desc": "Đạt điểm Elo cao nhất 1050 (Nghiệp Dư B) ở Xếp hạng 1v1.", "difficulty": 1, "steps": 1, "grade": 1, "target": 1050, "point": 15, "badge": 900018, "metric": "best_elo", "rewards": [{"todo": 1050, "reward": [{"itemid": 1, "count": 1000000}]}]},
        {"id": "ach_8_6", "category": "match", "type": "normal", "name": "Vượt Mốc 1200 Elo", "title": "Vượt Mốc 1200 Elo", "desc": "Đạt điểm Elo cao nhất 1200 (Bán Chuyên B) ở Xếp hạng 1v1.", "difficulty": 1, "steps": 1, "grade": 1, "target": 1200, "point": 15, "badge": 900018, "metric": "best_elo", "rewards": [{"todo": 1200, "reward": [{"itemid": 1, "count": 3000000}]}]},
        {"id": "ach_8_7", "category": "match", "type": "normal", "name": "Vượt Mốc 1400 Elo", "title": "Vượt Mốc 1400 Elo", "desc": "Đạt điểm Elo cao nhất 1400 (Chuyên Nghiệp A) ở Xếp hạng 1v1.", "difficulty": 2, "steps": 1, "grade": 1, "target": 1400, "point": 15, "badge": 900018, "metric": "best_elo", "rewards": [{"todo": 1400, "reward": [{"itemid": 1, "count": 7000000}]}]},
        {"id": "ach_8_8", "category": "match", "type": "normal", "name": "Vượt Mốc 1600 Elo", "title": "Vượt Mốc 1600 Elo", "desc": "Đạt điểm Elo cao nhất 1600 (Huyền Thoại C) ở Xếp hạng 1v1.", "difficulty": 3, "steps": 1, "grade": 1, "target": 1600, "point": 15, "badge": 900018, "metric": "best_elo", "rewards": [{"todo": 1600, "reward": [{"itemid": 1, "count": 15000000}]}]},
        {"id": "ach_9_5", "category": "match", "type": "normal", "name": "3 Trận Đấu Giả Lập", "title": "3 Trận Đấu Giả Lập", "desc": "Thi đấu tổng cộng 3 trận ở chế độ Đấu giả lập (HLV trưởng).", "difficulty": 1, "steps": 1, "grade": 1, "target": 3, "point": 10, "badge": 900020, "metric": "mgr_games", "rewards": [{"todo": 3, "reward": [{"itemid": 1, "count": 2000000}]}]},
        {"id": "ach_9_6", "category": "match", "type": "normal", "name": "10 Trận Đấu Giả Lập", "title": "10 Trận Đấu Giả Lập", "desc": "Thi đấu tổng cộng 10 trận ở chế độ Đấu giả lập (HLV trưởng).", "difficulty": 1, "steps": 1, "grade": 1, "target": 10, "point": 10, "badge": 900020, "metric": "mgr_games", "rewards": [{"todo": 10, "reward": [{"itemid": 1, "count": 4000000}]}]},
        {"id": "ach_9_7", "category": "match", "type": "normal", "name": "20 Trận Đấu Giả Lập", "title": "20 Trận Đấu Giả Lập", "desc": "Thi đấu tổng cộng 20 trận ở chế độ Đấu giả lập (HLV trưởng).", "difficulty": 2, "steps": 1, "grade": 1, "target": 20, "point": 15, "badge": 900020, "metric": "mgr_games", "rewards": [{"todo": 20, "reward": [{"itemid": 1, "count": 9000000}]}]},
        {"id": "ach_9_8", "category": "match", "type": "normal", "name": "50 Trận Đấu Giả Lập", "title": "50 Trận Đấu Giả Lập", "desc": "Thi đấu tổng cộng 50 trận ở chế độ Đấu giả lập (HLV trưởng).", "difficulty": 3, "steps": 1, "grade": 1, "target": 50, "point": 15, "badge": 900020, "metric": "mgr_games", "rewards": [{"todo": 50, "reward": [{"itemid": 1, "count": 20000000}]}]},
        {"id": "ach_10_5", "category": "match", "type": "normal", "name": "3 Vòng Đấu Giải", "title": "3 Vòng Đấu Giải", "desc": "Hoàn thành 3 vòng đấu trong một mùa giải League.", "difficulty": 1, "steps": 1, "grade": 1, "target": 3, "point": 10, "badge": 900021, "metric": "league_rounds", "rewards": [{"todo": 3, "reward": [{"itemid": 1, "count": 1500000}]}]},
        {"id": "ach_10_6", "category": "match", "type": "normal", "name": "8 Vòng Đấu Giải", "title": "8 Vòng Đấu Giải", "desc": "Hoàn thành 8 vòng đấu trong một mùa giải League.", "difficulty": 1, "steps": 1, "grade": 1, "target": 8, "point": 10, "badge": 900021, "metric": "league_rounds", "rewards": [{"todo": 8, "reward": [{"itemid": 1, "count": 3500000}]}]},
        {"id": "ach_10_7", "category": "match", "type": "normal", "name": "15 Vòng Đấu Giải", "title": "15 Vòng Đấu Giải", "desc": "Hoàn thành 15 vòng đấu trong một mùa giải League.", "difficulty": 2, "steps": 1, "grade": 1, "target": 15, "point": 15, "badge": 900021, "metric": "league_rounds", "rewards": [{"todo": 15, "reward": [{"itemid": 1, "count": 7000000}]}]},
        {"id": "ach_10_8", "category": "match", "type": "normal", "name": "30 Vòng Đấu Giải", "title": "30 Vòng Đấu Giải", "desc": "Hoàn thành 30 vòng đấu trong một mùa giải League.", "difficulty": 3, "steps": 1, "grade": 1, "target": 30, "point": 15, "badge": 900021, "metric": "league_rounds", "rewards": [{"todo": 30, "reward": [{"itemid": 1, "count": 15000000}]}]},
        {"id": "ach_12", "category": "match", "type": "normal", "name": "Máy Dội Bom", "title": "Máy Dội Bom", "desc": "Ghi 200 bàn thắng ở chế độ Xếp hạng 1v1.", "difficulty": 3, "steps": 6, "grade": 1, "target": 200, "point": 50, "badge": 900028, "rewards": [{"todo": 200, "reward": [{"itemid": 1, "count": 30000000}]}]},
        {"id": "ach_12_1", "category": "match", "type": "normal", "name": "Bàn Thắng Xếp Hạng Đầu Tiên", "title": "Bàn Thắng Xếp Hạng Đầu Tiên", "desc": "Ghi bàn thắng đầu tiên ở chế độ Xếp hạng 1v1.", "difficulty": 1, "steps": 1, "grade": 1, "target": 1, "point": 10, "badge": 900028, "metric": "ranked_goals", "rewards": [{"todo": 1, "reward": [{"itemid": 1, "count": 1000000}]}]},
        {"id": "ach_12_2", "category": "match", "type": "normal", "name": "10 Bàn Tại Xếp Hạng", "title": "10 Bàn Tại Xếp Hạng", "desc": "Ghi tổng cộng 10 bàn thắng ở chế độ Xếp hạng 1v1.", "difficulty": 1, "steps": 1, "grade": 1, "target": 10, "point": 10, "badge": 900028, "metric": "ranked_goals", "rewards": [{"todo": 10, "reward": [{"itemid": 1, "count": 2000000}]}]},
        {"id": "ach_12_3", "category": "match", "type": "normal", "name": "30 Bàn Tại Xếp Hạng", "title": "30 Bàn Tại Xếp Hạng", "desc": "Ghi tổng cộng 30 bàn thắng ở chế độ Xếp hạng 1v1.", "difficulty": 2, "steps": 1, "grade": 1, "target": 30, "point": 10, "badge": 900028, "metric": "ranked_goals", "rewards": [{"todo": 30, "reward": [{"itemid": 1, "count": 4000000}]}]},
        {"id": "ach_12_4", "category": "match", "type": "normal", "name": "63 Bàn Tại Xếp Hạng", "title": "63 Bàn Tại Xếp Hạng", "desc": "Ghi tổng cộng 63 bàn thắng ở chế độ Xếp hạng 1v1.", "difficulty": 2, "steps": 1, "grade": 1, "target": 63, "point": 10, "badge": 900028, "metric": "ranked_goals", "rewards": [{"todo": 63, "reward": [{"itemid": 1, "count": 8000000}]}]},
        {"id": "ach_12_5", "category": "match", "type": "normal", "name": "100 Bàn Tại Xếp Hạng", "title": "100 Bàn Tại Xếp Hạng", "desc": "Ghi tổng cộng 100 bàn thắng ở chế độ Xếp hạng 1v1.", "difficulty": 3, "steps": 1, "grade": 1, "target": 100, "point": 10, "badge": 900028, "metric": "ranked_goals", "rewards": [{"todo": 100, "reward": [{"itemid": 1, "count": 15000000}]}]},
        {"id": "ach_12_6", "category": "match", "type": "normal", "name": "200 Bàn Tại Xếp Hạng", "title": "200 Bàn Tại Xếp Hạng", "desc": "Ghi tổng cộng 200 bàn thắng ở chế độ Xếp hạng 1v1.", "difficulty": 4, "steps": 1, "grade": 1, "target": 200, "point": 10, "badge": 900028, "metric": "ranked_goals", "rewards": [{"todo": 200, "reward": [{"itemid": 1, "count": 25000000}]}]},
        {"id": "ach_13", "category": "match", "type": "normal", "name": "Trùm Nâng Cấp", "title": "Trùm Nâng Cấp", "desc": "Ép thẻ cầu thủ lên tới +7.", "difficulty": 3, "steps": 6, "grade": 1, "target": 7, "point": 50, "badge": 900035, "rewards": [{"todo": 7, "reward": [{"itemid": 1, "count": 30000000}]}]},
        {"id": "ach_13_1", "category": "match", "type": "normal", "name": "Mới Tập Ép Thẻ", "title": "Mới Tập Ép Thẻ", "desc": "Ép thành công 5 cầu thủ lên +2.", "difficulty": 1, "steps": 1, "grade": 1, "target": 5, "point": 10, "badge": 900035, "metric": "upg_ge_2", "rewards": [{"todo": 5, "reward": [{"itemid": 1, "count": 1000000}]}]},
        {"id": "ach_13_2", "category": "match", "type": "normal", "name": "Bắt Đầu Biết Sợ", "title": "Bắt Đầu Biết Sợ", "desc": "Ép thành công 5 cầu thủ lên +3.", "difficulty": 1, "steps": 1, "grade": 1, "target": 5, "point": 10, "badge": 900035, "metric": "upg_ge_3", "rewards": [{"todo": 5, "reward": [{"itemid": 1, "count": 2000000}]}]},
        {"id": "ach_13_3", "category": "match", "type": "normal", "name": "Quậy Lớn", "title": "Quậy Lớn", "desc": "Ép thành công 5 cầu thủ lên +4.", "difficulty": 2, "steps": 1, "grade": 1, "target": 5, "point": 10, "badge": 900035, "metric": "upg_ge_4", "rewards": [{"todo": 5, "reward": [{"itemid": 1, "count": 4000000}]}]},
        {"id": "ach_13_4", "category": "match", "type": "normal", "name": "Tập Đổi Màu Thẻ", "title": "Tập Đổi Màu Thẻ", "desc": "Ép thành công 3 cầu thủ lên +5.", "difficulty": 2, "steps": 1, "grade": 1, "target": 3, "point": 10, "badge": 900035, "metric": "upg_ge_5", "rewards": [{"todo": 3, "reward": [{"itemid": 1, "count": 8000000}]}]},
        {"id": "ach_13_5", "category": "match", "type": "normal", "name": "Lên Đời", "title": "Lên Đời", "desc": "Ép thành công 3 cầu thủ lên +6.", "difficulty": 3, "steps": 1, "grade": 1, "target": 3, "point": 10, "badge": 900035, "metric": "upg_ge_6", "rewards": [{"todo": 3, "reward": [{"itemid": 1, "count": 15000000}]}]},
        {"id": "ach_13_6", "category": "match", "type": "normal", "name": "Không Thể Xịt", "title": "Không Thể Xịt", "desc": "Ép thành công 3 cầu thủ lên +7.", "difficulty": 4, "steps": 1, "grade": 1, "target": 3, "point": 10, "badge": 900035, "metric": "upg_ge_7", "rewards": [{"todo": 3, "reward": [{"itemid": 1, "count": 25000000}]}]},
        {"id": "ach_14", "category": "match", "type": "normal", "name": "Bức Tường Thép", "title": "Bức Tường Thép", "desc": "Giữ sạch lưới 20 trận ở chế độ Xếp hạng 1v1.", "difficulty": 3, "steps": 5, "grade": 1, "target": 20, "point": 45, "badge": 900037, "rewards": [{"todo": 20, "reward": [{"itemid": 1, "count": 25000000}]}]},
        {"id": "ach_14_1", "category": "match", "type": "normal", "name": "Giữ Sạch Lưới Đầu Tiên", "title": "Giữ Sạch Lưới Đầu Tiên", "desc": "Giữ sạch lưới trận đầu tiên ở chế độ Xếp hạng 1v1.", "difficulty": 1, "steps": 1, "grade": 1, "target": 1, "point": 10, "badge": 900037, "metric": "ranked_cleansheets", "rewards": [{"todo": 1, "reward": [{"itemid": 1, "count": 1000000}]}]},
        {"id": "ach_14_2", "category": "match", "type": "normal", "name": "3 Trận Sạch Lưới", "title": "3 Trận Sạch Lưới", "desc": "Giữ sạch lưới tổng cộng 3 trận ở chế độ Xếp hạng 1v1.", "difficulty": 1, "steps": 1, "grade": 1, "target": 3, "point": 10, "badge": 900037, "metric": "ranked_cleansheets", "rewards": [{"todo": 3, "reward": [{"itemid": 1, "count": 2500000}]}]},
        {"id": "ach_14_3", "category": "match", "type": "normal", "name": "5 Trận Sạch Lưới", "title": "5 Trận Sạch Lưới", "desc": "Giữ sạch lưới tổng cộng 5 trận ở chế độ Xếp hạng 1v1.", "difficulty": 2, "steps": 1, "grade": 1, "target": 5, "point": 10, "badge": 900037, "metric": "ranked_cleansheets", "rewards": [{"todo": 5, "reward": [{"itemid": 1, "count": 5000000}]}]},
        {"id": "ach_14_4", "category": "match", "type": "normal", "name": "10 Trận Sạch Lưới", "title": "10 Trận Sạch Lưới", "desc": "Giữ sạch lưới tổng cộng 10 trận ở chế độ Xếp hạng 1v1.", "difficulty": 3, "steps": 1, "grade": 1, "target": 10, "point": 10, "badge": 900037, "metric": "ranked_cleansheets", "rewards": [{"todo": 10, "reward": [{"itemid": 1, "count": 10000000}]}]},
        {"id": "ach_14_5", "category": "match", "type": "normal", "name": "20 Trận Sạch Lưới", "title": "20 Trận Sạch Lưới", "desc": "Giữ sạch lưới tổng cộng 20 trận ở chế độ Xếp hạng 1v1.", "difficulty": 4, "steps": 1, "grade": 1, "target": 20, "point": 10, "badge": 900037, "metric": "ranked_cleansheets", "rewards": [{"todo": 20, "reward": [{"itemid": 1, "count": 20000000}]}]},
        {"id": "misc_7", "category": "match", "type": "normal", "name": "Chuỗi Thắng", "title": "Chuỗi Thắng", "shortdesc": "", "desc": "Giành 3 chiến thắng liên tiếp ở chế độ Xếp hạng 1v1.", "difficulty": 2, "steps": 1, "grade": 1, "target": 3, "point": 15, "badge": 900001, "metric": "ranked_best_streak", "rewards": [{"todo": 3, "reward": [{"itemid": 1, "count": 3000000}, {"itemid": 28001, "count": 1}]}]},
        {"id": "misc_8", "category": "match", "type": "normal", "name": "Cỗ Máy Ghi Bàn", "title": "Cỗ Máy Ghi Bàn", "shortdesc": "", "desc": "Ghi tổng cộng 50 bàn thắng ở mọi chế độ đấu phòng (Xếp hạng, Đấu giả lập, Giao hữu).", "difficulty": 2, "steps": 1, "grade": 1, "target": 50, "point": 15, "badge": 900001, "metric": "total_goals", "rewards": [{"todo": 50, "reward": [{"itemid": 1, "count": 3000000}, {"itemid": 45013, "count": 1}]}]},
    ],
}
def _ach_child(cid, name, desc, difficulty, target, point, badge, metric, ep):
    return {"id": cid, "category": "match", "type": "normal", "name": name, "title": name,
            "desc": desc, "difficulty": difficulty, "steps": 1, "grade": 1, "target": target,
            "point": point, "badge": badge, "metric": metric,
            "rewards": [{"todo": target, "reward": [{"itemid": 1, "count": ep}]}]}


def _expand_ach_catalogue():
    """Bug 3 round 4: at 86 missions / 15 groups the user still found
    the catalogue poor. Extend the long-haul groups with higher grades
    and add five groups on metrics the server really keeps (cup
    titles / cup matches from the cup record, total wins, total
    games, squad value), plus four special missions. New children get
    stable ids appended in target order, so existing claims and the
    group-claim order are untouched. Runs at import, before
    _enrich_ach_rewards()."""
    info = STATIC_ACHIEVEMENT_INFO
    mixed, lst = info["mixed"], info["list"]
    by_id = {it["id"]: it for it in lst}

    def extend(gid, badge, metric, rows, parent_target=None, parent_desc=None):
        for suffix, name, desc, diff, target, ep in rows:
            child = _ach_child(f"{gid}_{suffix}", name, desc, diff, target, 10, badge, metric, ep)
            lst.append(child)
            by_id[child["id"]] = child
            mixed[gid].append(child["id"])
        parent = by_id[gid]
        parent["steps"] = len(mixed[gid])
        if parent_target is not None:
            parent["target"] = parent_target
            if parent.get("rewards"):
                parent["rewards"][0]["todo"] = parent_target
        if parent_desc:
            parent["desc"] = parent_desc

    extend("ach_7", 900017, "ranked_wins", [
        (9, "75 Chiến Thắng Xếp Hạng", "Giành tổng cộng 75 chiến thắng ở chế độ Xếp hạng 1v1.", 4, 75, 30000000),
        (10, "100 Chiến Thắng Xếp Hạng", "Giành tổng cộng 100 chiến thắng ở chế độ Xếp hạng 1v1.", 4, 100, 45000000),
    ], 100, "Giành 100 chiến thắng ở chế độ Xếp hạng 1v1.")
    extend("ach_8", 900018, "best_elo", [
        (9, "Đạt Mốc 1800 Elo", "Đạt mốc điểm Elo cao nhất 1800 ở chế độ Xếp hạng 1v1.", 4, 1800, 30000000),
        (10, "Đạt Mốc 1950 Elo", "Đạt mốc điểm Elo cao nhất 1950 ở chế độ Xếp hạng 1v1.", 4, 1950, 45000000),
    ], 1950, "Đạt mốc điểm Elo cao nhất 1950 ở chế độ Xếp hạng 1v1.")
    extend("ach_9", 900020, "mgr_games", [
        (9, "75 Trận Đấu Giả Lập", "Thi đấu tổng cộng 75 trận ở chế độ Đấu giả lập (HLV trưởng).", 4, 75, 30000000),
        (10, "100 Trận Đấu Giả Lập", "Thi đấu tổng cộng 100 trận ở chế độ Đấu giả lập (HLV trưởng).", 4, 100, 45000000),
    ], 100, "Thi đấu tổng cộng 100 trận ở chế độ Đấu giả lập (HLV trưởng).")
    extend("ach_11", 900023, "cups_joined", [
        (4, "Tham Gia 5 Giải Cup", "Tham gia tổng cộng 5 giải Cup trong game.", 2, 5, 12000000),
        (5, "Tham Gia 8 Giải Cup", "Tham gia tổng cộng 8 giải Cup trong game.", 3, 8, 20000000),
        (6, "Tham Gia 12 Giải Cup", "Tham gia tổng cộng 12 giải Cup trong game.", 3, 12, 30000000),
    ], 12, "Tham gia tổng cộng 12 giải Cup trong game.")
    extend("ach_12", 900028, "ranked_goals", [
        (7, "300 Bàn Tại Xếp Hạng", "Ghi tổng cộng 300 bàn thắng ở chế độ Xếp hạng 1v1.", 4, 300, 35000000),
        (8, "500 Bàn Tại Xếp Hạng", "Ghi tổng cộng 500 bàn thắng ở chế độ Xếp hạng 1v1.", 4, 500, 50000000),
    ], 500, "Ghi 500 bàn thắng ở chế độ Xếp hạng 1v1.")
    extend("ach_14", 900037, "ranked_cleansheets", [
        (6, "30 Trận Sạch Lưới", "Giữ sạch lưới tổng cộng 30 trận ở chế độ Xếp hạng 1v1.", 4, 30, 30000000),
        (7, "50 Trận Sạch Lưới", "Giữ sạch lưới tổng cộng 50 trận ở chế độ Xếp hạng 1v1.", 4, 50, 45000000),
    ], 50, "Giữ sạch lưới 50 trận ở chế độ Xếp hạng 1v1.")
    # ach_13 grades carry per-grade metrics (upg_ge_N), so no shared metric.
    for suffix, name, desc, diff, target, ep, metric in [
        (7, "Thẻ Vàng Rực", "Ép thành công 2 cầu thủ lên +8.", 4, 2, 35000000, "upg_ge_8"),
        (8, "Đỉnh Cao Ép Thẻ", "Ép thành công 1 cầu thủ lên +10.", 4, 1, 50000000, "upg_ge_10"),
    ]:
        child = _ach_child(f"ach_13_{suffix}", name, desc, diff, target, 10, 900035, metric, ep)
        lst.append(child)
        by_id[child["id"]] = child
        mixed["ach_13"].append(child["id"])
    by_id["ach_13"]["steps"] = len(mixed["ach_13"])
    by_id["ach_13"]["target"] = 10
    by_id["ach_13"]["desc"] = "Ép thẻ cầu thủ lên tới +10."
    if by_id["ach_13"].get("rewards"):
        by_id["ach_13"]["rewards"][0]["todo"] = 10

    def new_group(gid, name, desc, badge, metric, point, rows, parent_ep):
        parent = {"id": gid, "category": "match", "type": "normal", "name": name, "title": name,
                  "desc": desc, "difficulty": 3, "steps": len(rows), "grade": 1,
                  "target": rows[-1][4], "point": point, "badge": badge,
                  "rewards": [{"todo": rows[-1][4], "reward": [{"itemid": 1, "count": parent_ep}]}]}
        lst.append(parent)
        by_id[gid] = parent
        mixed[gid] = []
        extend(gid, badge, metric, rows)

    new_group("ach_15", "Nhà Vô Địch Cup", "Vô địch các giải Cup trong game 12 lần.", 900036, "cup_wins", 50, [
        (1, "Chiếc Cup Đầu Tiên", "Lần đầu tiên vô địch một giải Cup.", 1, 1, 5000000),
        (2, "2 Lần Vô Địch Cup", "Vô địch tổng cộng 2 giải Cup.", 1, 2, 8000000),
        (3, "3 Lần Vô Địch Cup", "Vô địch tổng cộng 3 giải Cup.", 2, 3, 12000000),
        (4, "5 Lần Vô Địch Cup", "Vô địch tổng cộng 5 giải Cup.", 2, 5, 20000000),
        (5, "8 Lần Vô Địch Cup", "Vô địch tổng cộng 8 giải Cup.", 3, 8, 30000000),
        (6, "12 Lần Vô Địch Cup", "Vô địch tổng cộng 12 giải Cup.", 4, 12, 45000000),
    ], 40000000)
    new_group("ach_16", "Chiến Binh Đấu Cup", "Thi đấu 50 trận trong các giải Cup.", 900038, "cup_matches", 45, [
        (1, "Trận Cup Đầu Tiên", "Thi đấu trận đầu tiên trong một giải Cup.", 1, 1, 3000000),
        (2, "5 Trận Đấu Cup", "Thi đấu tổng cộng 5 trận trong các giải Cup.", 1, 5, 6000000),
        (3, "10 Trận Đấu Cup", "Thi đấu tổng cộng 10 trận trong các giải Cup.", 2, 10, 10000000),
        (4, "20 Trận Đấu Cup", "Thi đấu tổng cộng 20 trận trong các giải Cup.", 2, 20, 18000000),
        (5, "35 Trận Đấu Cup", "Thi đấu tổng cộng 35 trận trong các giải Cup.", 3, 35, 28000000),
        (6, "50 Trận Đấu Cup", "Thi đấu tổng cộng 50 trận trong các giải Cup.", 4, 50, 40000000),
    ], 35000000)
    new_group("ach_17", "Ông Trùm Sân Cỏ", "Giành 300 chiến thắng ở mọi chế độ đấu phòng.", 900039, "total_wins", 50, [
        (1, "10 Chiến Thắng", "Giành tổng cộng 10 chiến thắng ở mọi chế độ đấu phòng.", 1, 10, 4000000),
        (2, "25 Chiến Thắng", "Giành tổng cộng 25 chiến thắng ở mọi chế độ đấu phòng.", 1, 25, 8000000),
        (3, "50 Chiến Thắng", "Giành tổng cộng 50 chiến thắng ở mọi chế độ đấu phòng.", 2, 50, 14000000),
        (4, "100 Chiến Thắng", "Giành tổng cộng 100 chiến thắng ở mọi chế độ đấu phòng.", 3, 100, 22000000),
        (5, "200 Chiến Thắng", "Giành tổng cộng 200 chiến thắng ở mọi chế độ đấu phòng.", 4, 200, 35000000),
        (6, "300 Chiến Thắng", "Giành tổng cộng 300 chiến thắng ở mọi chế độ đấu phòng.", 4, 300, 50000000),
    ], 45000000)
    new_group("ach_18", "Chuyên Gia Thi Đấu", "Thi đấu 500 trận ở mọi chế độ đấu phòng.", 900046, "total_games", 45, [
        (1, "10 Trận Thi Đấu", "Thi đấu tổng cộng 10 trận ở mọi chế độ đấu phòng.", 1, 10, 3000000),
        (2, "25 Trận Thi Đấu", "Thi đấu tổng cộng 25 trận ở mọi chế độ đấu phòng.", 1, 25, 6000000),
        (3, "50 Trận Thi Đấu", "Thi đấu tổng cộng 50 trận ở mọi chế độ đấu phòng.", 2, 50, 10000000),
        (4, "100 Trận Thi Đấu", "Thi đấu tổng cộng 100 trận ở mọi chế độ đấu phòng.", 3, 100, 18000000),
        (5, "250 Trận Thi Đấu", "Thi đấu tổng cộng 250 trận ở mọi chế độ đấu phòng.", 4, 250, 30000000),
        (6, "500 Trận Thi Đấu", "Thi đấu tổng cộng 500 trận ở mọi chế độ đấu phòng.", 4, 500, 45000000),
    ], 40000000)
    new_group("ach_19", "Đội Hình Tỷ Phú", "Xây dựng đội hình có tổng giá trị 50 tỷ EP.", 900050, "team_value", 50, [
        (1, "Đội Hình 100 Triệu", "Tổng giá trị đội hình đạt 100 triệu EP.", 1, 100000000, 5000000),
        (2, "Đội Hình 500 Triệu", "Tổng giá trị đội hình đạt 500 triệu EP.", 2, 500000000, 10000000),
        (3, "Đội Hình 2 Tỷ", "Tổng giá trị đội hình đạt 2 tỷ EP.", 2, 2000000000, 18000000),
        (4, "Đội Hình 10 Tỷ", "Tổng giá trị đội hình đạt 10 tỷ EP.", 3, 10000000000, 30000000),
        (5, "Đội Hình 50 Tỷ", "Tổng giá trị đội hình đạt 50 tỷ EP.", 4, 50000000000, 50000000),
    ], 45000000)

    for cid, name, desc, target, metric, ep, pack in [
        ("misc_9", "Lần Đầu Vô Địch", "Lần đầu tiên vô địch một giải Cup.", 1, "cup_wins", 5000000, 82214068),
        ("misc_10", "Thẻ Thần Thánh", "Sở hữu một cầu thủ ép cấp +10.", 10, "best_grade", 10000000, 82214068),
        ("misc_11", "Kho Thẻ Khủng", "Sở hữu tổng cộng 150 cầu thủ trong đội.", 150, "players_owned", 5000000, 45013),
        ("misc_12", "Săn Bàn Thắng", "Ghi tổng cộng 200 bàn thắng ở mọi chế độ đấu phòng.", 200, "total_goals", 8000000, 45013),
    ]:
        child = {"id": cid, "category": "match", "type": "normal", "name": name, "title": name,
                 "shortdesc": "", "desc": desc, "difficulty": 2, "steps": 1, "grade": 1,
                 "target": target, "point": 15, "badge": 900001, "metric": metric,
                 "rewards": [{"todo": target, "reward": [{"itemid": 1, "count": ep},
                                                         {"itemid": pack, "count": 1}]}]}
        lst.append(child)
        by_id[cid] = child
        mixed["misc"].append(cid)
    if by_id.get("misc"):
        by_id["misc"]["steps"] = len(mixed["misc"])


_expand_ach_catalogue()



# Split plan for groups over the client's 5-row badge table:
# (source group, new group, new group name, new badge id)
_ACH_SPLITS = [
    ("ach_7", "ach_20", "Độc Hành II", 900051),
    ("ach_8", "ach_21", "Đỉnh Cao Xếp Hạng II", 900052),
    ("ach_9", "ach_22", "Chiến Lược Gia II", 900053),
    ("ach_10", "ach_23", "Đường Đua Giải Đấu II", 900054),
    ("ach_11", "ach_24", "Săn Cup II", 900055),
    ("ach_12", "ach_25", "Máy Dội Bom II", 900056),
    ("ach_13", "ach_26", "Trùm Nâng Cấp II", 900057),
    ("ach_14", "ach_27", "Bức Tường Thép II", 900058),
    ("ach_15", "ach_28", "Nhà Vô Địch Cup II", 900059),
    ("ach_16", "ach_29", "Chiến Binh Đấu Cup II", 900060),
    ("ach_17", "ach_30", "Ông Trùm Sân Cỏ II", 900061),
    ("ach_18", "ach_31", "Chuyên Gia Thi Đấu II", 900062),
    ("misc", "ach_32", "Đặc Biệt II", 900063),
    ("ach_32", "ach_33", "Đặc Biệt III", 900064),
]


def _split_ach_groups():
    """Bug 3 round 5: the client's badge page (CoachAchievementBadgePage,
    the DS nhiem vu screen AND the coach popup's Nhiem vu tab) renders a
    group's missions into a table with exactly 5 title slots
    (MAX_ACHIEVEMENT_LIST = 5; the official catalogue is built of
    five-grade groups). Groups with more than 5 children showed only the
    first 5 -- misc displayed misc_1..5 of 12 no matter how complete the
    server data was (user retests 2026-11-12, image-43) -- and overflowed
    the popup into a #1009. Move every child past the 5th into a sibling
    group with its own badge; child ids (and therefore saved claims) are
    untouched. Runs after _expand_ach_catalogue, before normalize/enrich.
    """
    info = STATIC_ACHIEVEMENT_INFO
    mixed, lst = info["mixed"], info["list"]
    by_id = {it["id"]: it for it in lst}
    for src, gid, name, badge in _ACH_SPLITS:
        kids = mixed.get(src) or []
        if len(kids) <= 5:
            continue
        moved, mixed[src] = kids[5:], kids[:5]
        parent_src = by_id[src]
        last = by_id[moved[-1]]
        parent_ep = 20000000
        for e in (parent_src.get("rewards") or [{}])[0].get("reward", []):
            if e.get("itemid") == 1:
                parent_ep = int(e.get("count") or parent_ep)
        parent = {"id": gid, "category": parent_src.get("category", "match"),
                  "type": parent_src.get("type", "normal"), "name": name, "title": name,
                  "desc": parent_src.get("desc", ""), "difficulty": parent_src.get("difficulty", 3),
                  "steps": len(moved), "grade": 1, "target": last.get("target"),
                  "point": parent_src.get("point", 10), "badge": badge,
                  "rewards": [{"todo": last.get("target"),
                               "reward": [{"itemid": 1, "count": parent_ep}]}]}
        lst.append(parent)
        by_id[gid] = parent
        mixed[gid] = list(moved)
        for cid in moved:
            by_id[cid]["badge"] = badge
        # the source group now ends at its 5th child
        fifth = by_id[mixed[src][-1]]
        parent_src["steps"] = len(mixed[src])
        parent_src["target"] = fifth.get("target")
        if parent_src.get("rewards"):
            parent_src["rewards"][0]["todo"] = fifth.get("target")
        if not any(b.get("badgeid") == badge for b in STATIC_BADGES_LIST):
            STATIC_BADGES_LIST.append({
                "badgeid": badge, "itemid": badge, "asset": str(badge), "name": name,
                "desc": parent_src.get("desc", ""), "icon": str(badge),
                "itemicon": str(badge), "category": "team",
                "image_effect": 0, "itemback": 0})


_split_ach_groups()


def _normalize_ach_catalogue():
    """Canonical layout: each group's children sorted by grade, and the
    static "list" rebuilt as contiguous blocks (parent, then its
    children). The expansion rounds appended new groups/children at the
    end of the list, so a group built by scanning the flat list picked up
    other groups' children and lost its own later ones -- the misc group
    only ever showed its original, contiguously-appended misc_1..5
    (user 2026-11-12)."""
    info = STATIC_ACHIEVEMENT_INFO

    def _num(cid):
        try:
            return int(str(cid).rsplit("_", 1)[1])
        except Exception:
            return 0

    order = [f"ach_{i}" for i in range(1, 34)] + ["misc"]
    for gid in order:
        if gid in info["mixed"]:
            info["mixed"][gid] = sorted(info["mixed"][gid], key=_num)
    by_id = {e["id"]: e for e in info["list"]}
    new_list, seen = [], set()
    for gid in order:
        if gid in by_id:
            new_list.append(by_id[gid]); seen.add(gid)
        for cid in info["mixed"].get(gid, []):
            if cid in by_id and cid not in seen:
                new_list.append(by_id[cid]); seen.add(cid)
    for e in info["list"]:
        if e["id"] not in seen:
            new_list.append(e); seen.add(e["id"])
    info["list"] = new_list


_normalize_ach_catalogue()


def _enrich_ach_rewards():
    """Bug 3: official-style multi-item grade rewards. The authored
    catalogue paid EP only; each ach_* grade now also pays player
    packs, escalating with the grade index and the group tier (the
    official system pays several packs per grade). misc entries keep
    their authored rewards. Runs once at import so the static
    endpoint, the payload and the claim path all read the same data."""
    tier_packs = {
        "ach_1": [28001], "ach_2": [28001], "ach_3": [28001, 28006],
        "ach_4": [28006, 45013], "ach_5": [45013, 28001], "ach_6": [45013, 82214180],
        "ach_7": [28001, 45013], "ach_8": [82214180], "ach_9": [28006, 45013],
        "ach_10": [45013], "ach_11": [82214068],
        "ach_12": [45013], "ach_13": [82214068], "ach_14": [28006, 45013],
        "ach_15": [82214068], "ach_16": [45013], "ach_17": [82214180],
        "ach_18": [28001, 45013], "ach_19": [82214068],
        # round-5 split groups inherit their source group's pack tier
        "ach_20": [28001, 45013], "ach_21": [82214180], "ach_22": [28006, 45013],
        "ach_23": [45013], "ach_24": [82214068], "ach_25": [45013],
        "ach_26": [82214068], "ach_27": [28006, 45013], "ach_28": [82214068],
        "ach_29": [45013], "ach_30": [82214180], "ach_31": [28001, 45013],
    }
    mixed = STATIC_ACHIEVEMENT_INFO["mixed"]
    by_id = {it["id"]: it for it in STATIC_ACHIEVEMENT_INFO["list"]}
    for gid, packs in tier_packs.items():
        for idx, cid in enumerate(mixed.get(gid) or [], start=1):
            it = by_id.get(cid)
            if not it or not it.get("rewards"):
                continue
            entries = [e for e in it["rewards"][0].get("reward", []) if e.get("itemid") == 1]
            count = 1 + (1 if idx >= 3 else 0) + (1 if idx >= 5 else 0)
            entries.append({"itemid": packs[(idx - 1) % len(packs)], "count": count})
            if idx >= 4 and len(packs) > 1:
                entries.append({"itemid": packs[idx % len(packs)], "count": 1})
            it["rewards"][0]["reward"] = entries
        parent = by_id.get(gid)
        if parent and parent.get("rewards"):
            entries = [e for e in parent["rewards"][0].get("reward", []) if e.get("itemid") == 1]
            entries.append({"itemid": packs[-1], "count": 2})
            parent["rewards"][0]["reward"] = entries


_enrich_ach_rewards()


def ep_static_badges(query, body):
    # Shape per the upstream server's proven BADGES table: the badge page
    # (CoachAchievementBadgePage.makeBadgeList) joins a badge to its
    # achievement GROUP through "asset" -- asset is the group key, NOT the
    # badge id. Ours served asset=str(badgeid), so the join never matched,
    # the lookup came back null and the page died with AS3 #1009 (user
    # telemetry 2026-11-12 10:35/10:41 UTC, grid view + coach popup alike).
    # category is "BADGE" and itemback the string "defaultblack"; the badge
    # image itself is addressed by itemicon (the badge id), which stays.
    group_by_badge = {}
    for e in STATIC_ACHIEVEMENT_INFO.get("list", []):
        if e.get("badge") is not None and str(e.get("id")) in STATIC_ACHIEVEMENT_INFO.get("mixed", {}):
            group_by_badge[e["badge"]] = str(e["id"])
    out = []
    for b in STATIC_BADGES_LIST:
        bid = b.get("badgeid")
        out.append({**b, "id": bid, "itemid": bid, "category": "BADGE",
                    "itemicon": bid, "itemback": "defaultblack",
                    "asset": group_by_badge.get(bid, b.get("asset"))})
    return hc._json(out)


def ep_static_achievement(query, body):
    # Ensure every achievement item has shortdesc, longdesc, description, and title populated
    data = copy.deepcopy(STATIC_ACHIEVEMENT_INFO)
    for it in data.get("list", []):
        # shortdesc used to be blanked for every entry; the achievement
        # screens read it, so keep the authored text (fall back to desc).
        it["shortdesc"] = it.get("shortdesc") or it.get("desc", "") or it.get("name", "")
        it["longdesc"] = it.get("desc", "")
        it["description"] = it.get("desc", "")
        it["title"] = it.get("name", "")
    return hc._json(data)


# ---------------------------------------------------------------------------
# Weekly quests + GP (the AchievementQuestWindow, opened by the top-bar
# quest button). Modelled on the upstream server's proven design: the window
# reads its quest list from GET /static/info/request ({expire, current,
# next}), progress from /achievement/me "quest" entries ({id = index,
# progress.detail.current, completedat}), and the GP balance from coach
# gpdata {point, until} (CoachProxy). Its 5 reward boxes are the first
# entries of /shop/list/GPSHOP, bought with GP (shopid >= GP_SHOPID, see
# http_config). Quests are drawn deterministically per week (same draw for
# everyone); progress is the delta of career metrics since the week began
# (baseline snapshot at rollover); finishing a quest credits GP at once.
# ---------------------------------------------------------------------------
QUEST_POOL = [("match", 10, 30), ("win", 6, 30), ("goal", 15, 25),
              ("upgrade", 3, 20), ("rank", 5, 25), ("rankwin", 3, 30),
              ("match", 20, 50), ("goal", 30, 45)]  # (metric, target, GP)
QUEST_COUNT = 5
QUEST_TEXT_VI = {"match": "Chơi {0} trận", "win": "Thắng {0} trận",
                 "goal": "Ghi {0} bàn", "rank": "Chơi {0} trận Rank",
                 "rankwin": "Thắng {0} trận Rank",
                 "upgrade": "Nâng cấp cầu thủ {0} lần"}


def _week_quests(week_end):
    """[(name, metric, target, gp)] drawn for the week ending at week_end."""
    draw = random.Random(f"quests/{int(week_end)}").sample(
        QUEST_POOL, min(QUEST_COUNT, len(QUEST_POOL)))
    return [(QUEST_TEXT_VI[s].replace("{0}", f"{t:,}"), s, t, g) for s, t, g in draw]


def _quest_metrics(st):
    rk = st.get("ranked") or {}
    rk_mgr = _get_mgr_ranked_state(st)
    car = st.get("career") or {}
    r_games = int(rk.get("wins", 0)) + int(rk.get("draws", 0)) + int(rk.get("losses", 0))
    m_games = (int(rk_mgr.get("wins", 0)) + int(rk_mgr.get("draws", 0))
               + int(rk_mgr.get("losses", 0)))
    return {
        "match": r_games + m_games,
        "win": int(rk.get("wins", 0)) + int(rk_mgr.get("wins", 0)),
        "goal": int(car.get("total_goals", 0)),
        "rank": r_games,
        "rankwin": int(rk.get("wins", 0)),
        "upgrade": sum(int(v) for v in (car.get("upgrades") or {}).values()),
    }


def _quest_state(st):
    """(quest_state, quests, progress): roll the week over if needed and
    credit GP for quests finished since the last call. Mutates st; the
    caller saves."""
    week = hc._week_end_ts()
    q = st.setdefault("quest", {})
    metrics = _quest_metrics(st)
    if q.get("week") != week:
        q.update(week=week, baseline=metrics, done={}, gp=0)
    base = q.get("baseline") or {}
    quests = _week_quests(week)
    progress = []
    for i, (_name, stat, todo, gp) in enumerate(quests):
        cur = max(0, metrics.get(stat, 0) - int(base.get(stat, 0)))
        progress.append(cur)
        if cur >= todo and str(i) not in (q.get("done") or {}):
            q.setdefault("done", {})[str(i)] = hc.w3c(time.time())
            q["gp"] = int(q.get("gp") or 0) + gp
    return q, quests, progress


def ep_static_info_request(query, body):
    """GET /static/info/request: this week's and next week's quest lists for
    the Quest window; expire = the two Monday 00:00 (UTC+7) resets."""
    week = hc._week_end_ts()

    def _lst(w):
        return [{"name": n, "todo": t, "reward": {"count": g}}
                for n, _s, t, g in _week_quests(w)]
    return hc._json({"expire": [hc.w3c(week), hc.w3c(week + 7 * 86400)],
                     "current": _lst(week), "next": _lst(week + 7 * 86400)})


def _build_achievement_me_payload(st=None):
    if st is None:
        st = _state()
    c = None
    if hasattr(hc, "my_coach") and hc:
        try:
            c = hc.my_coach(st)
        except Exception:
            c = None
    if not c and st and st.get("coaches"):
        c = st["coaches"][0]

    coach_lvl = int((c or {}).get('level') or (c or {}).get('levelinfo', {}).get('level') or 1)

    rk = _get_ranked_state(st)
    wins = int(rk.get("wins", 0))
    total_games = wins + int(rk.get("draws", 0)) + int(rk.get("losses", 0))

    ach_claimed = set((st.get("achievements") or {}).get("claimed") or [])
    gp_claimed = set((st.get("achievements") or {}).get("quest_gp_claimed") or [])

    slot = hc._default_slot(st) if st.get("coaches") else 0
    raw_team = (st.get("teams") or {}).get(str(slot)) or {}
    player_count = len(raw_team.get("players") or [])

    # Bug 3 (content expansion): conditions are metric-driven -- each
    # static child names a metric and a target, and the values come from
    # the records the server actually keeps (ladder records for both
    # modes, the current league season, cups, the squad and the coach).
    rk_mgr = _get_mgr_ranked_state(st)
    mgr_games = int(rk_mgr.get("wins", 0)) + int(rk_mgr.get("draws", 0)) + int(rk_mgr.get("losses", 0))
    team_players = raw_team.get("players") or []
    best_grade = max([int(p.get("grade") or 1) for p in team_players] or [1])
    _cup_rec = ((c or {}).get("record") or {}).get("cup") or {}
    try:
        _team_value = int(_calc_team_value(st, slot))
    except Exception:
        _team_value = 0
    metric_values = {
        "level": coach_lvl,
        "login": 1,
        "ranked_wins": wins,
        "ranked_games": total_games,
        "best_elo": int(rk.get("highelo") or rk.get("elo") or 0),
        "mgr_games": mgr_games,
        "mgr_wins": int(rk_mgr.get("wins", 0)),
        "total_games": total_games + mgr_games,
        "total_wins": wins + int(rk_mgr.get("wins", 0)),
        "players_owned": player_count,
        "best_grade": best_grade,
        "ep_balance": int((c or {}).get("ep") or 0),
        "league_rounds": int(((st.get("league2") or {}).get("league") or {}).get("nextround") or 0),
        "cups_joined": int(_cup_rec.get("totalchallenges", 0)) or len(st.get("cups") or {}),
        "cup_wins": int(_cup_rec.get("totalwins", 0)),
        "cup_matches": int(_cup_rec.get("wins", 0)) + int(_cup_rec.get("draws", 0)) + int(_cup_rec.get("losses", 0)),
        "team_value": _team_value,
        "ranked_goals": int((st.get("career") or {}).get("ranked_goals", 0)),
        "ranked_cleansheets": int((st.get("career") or {}).get("ranked_cleansheets", 0)),
        "ranked_best_streak": int((st.get("career") or {}).get("ranked_best_streak", 0)),
        "mgr_goals": int((st.get("career") or {}).get("mgr_goals", 0)),
        "total_goals": int((st.get("career") or {}).get("total_goals", 0)),
    }
    _ups = (st.get("career") or {}).get("upgrades") or {}
    for _g in range(2, 11):
        metric_values[f"upg_ge_{_g}"] = sum(int(v) for k, v in _ups.items() if int(k) >= _g)
    child_status = {}
    for item in STATIC_ACHIEVEMENT_INFO["list"]:
        aid = item["id"]
        if "_" in aid and aid != "misc":
            target_val = int(item.get("target", 1))
            if aid == "ach_1_1":
                child_status[aid] = (True, target_val, target_val)
                continue
            cur_val = int(metric_values.get(item.get("metric", "level"), 0))
            child_status[aid] = (cur_val >= target_val, min(cur_val, target_val), target_val)

    # Real completion dates, upstream-style: an entry carries
    # "completedat" ONLY once it is complete (the upstream builder omits
    # it otherwise; the coach popup's badge panel lights a badge from
    # its group's {id, complete, completedat}). Ours used to stamp every
    # entry -- complete or not -- with one fake constant date, which is
    # also what the popup's sort-by-completion-date choked on (freeze +
    # crash, user report 2026-11-12 23:00). Dates are stamped the first
    # time an entry is seen complete and kept in the state (ep_achieve-
    # ment_me saves after building); legacy completions get the first
    # stamp date, the true one was never recorded.
    doneat = st.setdefault("achievements", {}).setdefault("doneat", {})
    now_w3c = hc.w3c(time.time())

    all_achs = []
    for p_id, children in STATIC_ACHIEVEMENT_INFO["mixed"].items():
        if p_id.isdigit():
            continue
        done_children_count = 0
        total_children_count = len(children)

        for cid in children:
            is_done, curr_val, target_val = child_status.get(cid, (False, 0, 1))
            if is_done:
                done_children_count += 1
            is_avail = (is_done and cid not in ach_claimed)
            entry = {
                "id": cid,
                "complete": is_done,
                "progress": {
                    "grade": 1,
                    "current": curr_val,
                    "total": target_val,
                    "todo": target_val,
                    "detail": {"current": curr_val, "total": target_val}
                },
                "rewards": {"available": is_avail, "grade": 1}
            }
            if is_done:
                entry["completedat"] = doneat.setdefault(cid, now_w3c)
            all_achs.append(entry)

        is_p_done = done_children_count >= total_children_count
        is_p_avail = (is_p_done and p_id not in ach_claimed)
        parent = {
            # The client claims the misc group's badge reward as id 0
            # (POST /achievement/0/reward/1) -- its static model numbers
            # that group 0, so the payload parent carries 0, not "misc".
            "id": (0 if p_id == "misc" else p_id),
            "complete": is_p_done,
            "progress": {
                "grade": 1,
                "current": done_children_count,
                "total": total_children_count,
                "todo": total_children_count,
                "detail": {"current": done_children_count, "total": total_children_count}
            },
            "rewards": {"available": is_p_avail, "grade": 1}
        }
        if is_p_done:
            parent["completedat"] = doneat.setdefault(p_id, now_w3c)
        all_achs.append(parent)

    # every catalogue group counts toward the header total (the round-5
    # split raised this from 20 to 34 groups; a hardcoded set froze it)
    badge_keys = set(STATIC_ACHIEVEMENT_INFO["mixed"].keys())
    badges_done = sum(1 for a in all_achs if (a["id"] in badge_keys or a["id"] == 0) and a["complete"])

    # (id, name, desc, current, target, done, EP reward, pack id, pack name,
    #  GP points, type) -- the quest window has daily and weekly lists and
    # a GP ("Diem") total; until now every quest was type "challenge" with
    # no GP, so both lists rendered empty (user 2026-11-12).
    QUEST_DEFS = [
        (0, "Điểm Danh Hằng Ngày", "Đăng nhập và quản lý đội bóng hàng ngày.", 1, 1, True, 500000, None, None, 10, "daily"),
        (1, "Thử Sức Thi Đấu", "Tham gia 1 trận đấu bất kỳ (PVP, Giả Lập hoặc Đấu Giải).", min(1, total_games if total_games > 0 else 1), 1, True, 600000, 28001, "Thẻ Chỉ Định Top 100", 15, "daily"),
        (2, "Khát Khao Chiến Thắng", "Giành chiến thắng ít nhất 1 trận đấu.", min(1, wins if wins > 0 else 1), 1, True, 800000, 28006, "Thẻ Chỉ Định Top 30", 20, "daily"),
        (3, "Đấu Trường Xếp Hạng", "Tham gia tranh tài ở chế độ Đấu Xếp Hạng 1v1.", min(1, total_games if total_games > 0 else 1), 1, True, 1000000, None, None, 20, "daily"),
        (4, "Chiến Thuật Bậc Thầy", "Thiết lập và lưu 6 chiến thuật đội bóng phù hợp.", 1, 1, True, 700000, None, None, 15, "daily"),
        (5, "Nâng Cấp Đội Hình", "Nâng cấp và tăng cường sức mạnh cầu thủ trong đội.", min(1, 1 if coach_lvl >= 1 else 0), 1, True, 900000, 28001, "Thẻ Chỉ Định Top 100", 20, "daily"),
        (6, "Mở Rộng Câu Lạc Bộ", "Sở hữu tối thiểu 11 cầu thủ trong danh sách đội bóng.", min(player_count, 11), 11, player_count >= 11, 120000, None, None, 10, "daily"),
        (7, "HLV Triển Vọng Lv.5", "Đạt cấp độ Huấn Luyện Viên Lv.5 để mở khóa kỹ năng.", min(coach_lvl, 5), 5, coach_lvl >= 5, 150000, 28006, "Thẻ Chỉ Định Top 30", 15, "daily"),
        (8, "HLV Chuyên Nghiệp Lv.10", "Đạt cấp độ Huấn Luyện Viên Lv.10 khẳng định vị thế.", min(coach_lvl, 10), 10, coach_lvl >= 10, 200000, 82214068, "Gói Cầu Thủ Đặc Biệt", 25, "daily"),
        (9, "HLV Đẳng Cấp Lv.20", "Đạt cấp độ Huấn Luyện Viên Lv.20 và thống trị giải đấu.", min(coach_lvl, 20), 20, coach_lvl >= 20, 300000, 82214068, "Gói Cầu Thủ Đặc Biệt", 30, "daily"),
        (10, "Chiến Binh Tuần", "Giành chiến thắng 3 trận đấu ở bất kỳ chế độ nào.", min(metric_values["total_wins"], 3), 3, metric_values["total_wins"] >= 3, 300000, 28006, "Thẻ Chỉ Định Top 30", 50, "weekly"),
        (11, "Săn Bàn Tuần", "Ghi 10 bàn thắng ở mọi chế độ thi đấu.", min(metric_values["total_goals"], 10), 10, metric_values["total_goals"] >= 10, 300000, 28001, "Thẻ Chỉ Định Top 100", 50, "weekly"),
        (12, "Đua Xếp Hạng Tuần", "Thi đấu 5 trận Xếp Hạng 1v1.", min(total_games, 5), 5, total_games >= 5, 500000, 45013, "Gói Top 30 Mùa Hiện Tại", 80, "weekly"),
        (13, "Chinh Phục Cup Tuần", "Tham gia 1 giải đấu Cup.", min(metric_values["cups_joined"], 1), 1, metric_values["cups_joined"] >= 1, 500000, 28001, "Thẻ Chỉ Định Top 100", 80, "weekly"),
        (14, "Đại Gia Tuần", "Sở hữu số dư EP từ 10.000.000 trở lên.", min(metric_values["ep_balance"], 10000000), 10000000, metric_values["ep_balance"] >= 10000000, 1000000, 82214068, "Gói Cầu Thủ Đặc Biệt", 100, "weekly"),
    ]

    quests_list = []
    quest_infos = []
    for qid, qname, qdesc, cur_val, tgt_val, is_done, rep, p_id, p_name, q_gp, q_type in QUEST_DEFS:
        is_claimed = (f"quest_{qid}" in ach_claimed or qid in ach_claimed or str(qid) in ach_claimed)
        quests_list.append({
            "id": qid,
            "name": qname,
            "desc": qdesc,
            "type": q_type,
            "platform": "pc",
            "gp": q_gp,
            "point": q_gp,
            "current": cur_val,
            "todo": tgt_val,
            "complete": is_done,
            "completedat": "2026-11-01T00:00:00.000Z",
            "state": "complete" if is_done else "ongoing",
            "reward": {"count": rep},
            "items": ([{"itemid": p_id, "count": 1}] if p_id else []),
            "rewardedmp": 0,
            "received": is_claimed,
        })
        quest_infos.append({
            "id": qid,
            "type": q_type,
            "gp": q_gp,
            "complete": is_done,
            "completedat": "2026-11-01T00:00:00.000Z",
            "progress": {
                "grade": 1,
                "current": cur_val,
                "total": tgt_val,
                "todo": tgt_val,
                "detail": {"current": cur_val, "total": tgt_val}
            },
            "rewards": {
                "available": (is_done and not is_claimed),
                "grade": 1
            }
        })

    _qstate, _wquests, _wprogress = _quest_state(st)
    weekly_quest_infos = []
    for _i, (_qn, _qs, _qtodo, _qgp) in enumerate(_wquests):
        _entry = {"id": _i,
                  "progress": {"detail": {"current": min(_wprogress[_i], _qtodo)}}}
        if str(_i) in (_qstate.get("done") or {}):
            _entry["completedat"] = _qstate["done"][str(_i)]
        weekly_quest_infos.append(_entry)

    return {
        "summary": {"done": badges_done, "total": len(badge_keys)},
        "achievement": all_achs,
        "achievements": all_achs,
        "quest": weekly_quest_infos,
        "quests": quest_infos,
        "questbank": {
            "expire": [hc.w3c(_qstate["week"]), hc.w3c(_qstate["week"] + 7 * 86400)],
            "current": quests_list,
            "next": quests_list,
            "list": quests_list,
        },
        "questRewards": [
            {"gp": 100, "reward": [{"itemid": 1, "count": 1000000}], "received": (100 in gp_claimed)},
            {"gp": 200, "reward": [{"itemid": 1, "count": 2000000}], "received": (200 in gp_claimed)},
            {"gp": 300, "reward": [{"itemid": 1, "count": 5000000}, {"itemid": 28001, "count": 1}], "received": (300 in gp_claimed)},
        ],
    }


def ep_achievement_me(query, body):
    st = _state()
    out = _build_achievement_me_payload(st)
    _save(st)  # the week rollover + GP accrual inside the builder must persist
    return hc._json(out)


def ep_achievement_claim_reward(aid, grade, query, body):
    st = _state()
    achs_claimed = st.setdefault("achievements", {}).setdefault("claimed", [])
    aid_str = str(aid)
    slot = hc._default_slot(st) if st.get("coaches") else 0

    def _refuse():
        # A bare {"result": false} made the client throw a UI error on
        # the claim (telemetry, 2026-11-12): answer with the refresh
        # resources attached so it can simply re-render.
        return hc._json({
            "result": False, "reason": "CAN_NOT_CLAIM_ACHIEVEMENT_REWARDS",
            "resources": [
                {"resource": "/achievement/me", "uri": "/achievement/me",
                 "body": _build_achievement_me_payload(st)},
                {"resource": "/coach/me", "uri": "/coach/me",
                 "body": _wrapped_coach_me(st, slot)},
            ],
        })

    # misc children pay their authored static rewards (EP + pack)
    def _static_entries(item_id):
        for item in STATIC_ACHIEVEMENT_INFO.get("list", []):
            if item.get("id") == item_id:
                rewards = item.get("rewards") or []
                if rewards:
                    return [dict(e) for e in (rewards[0].get("reward") or [])]
        return [{"itemid": 1, "count": 200000}]

    # The client claims BY GROUP: every "Nhận quà" press on this
    # screen is POST /achievement/<group>/reward/1 -- group 0 is the
    # misc group in its model. A group claim pays the group's first
    # completed-but-unclaimed child (or the group reward itself when
    # every child is done); a child id pays that child directly.
    _payload0 = _build_achievement_me_payload(st)
    _complete = {a["id"]: a["complete"] for a in _payload0.get("achievement", [])}
    _mixed = STATIC_ACHIEVEMENT_INFO["mixed"]
    _claimed = set(achs_claimed)

    def _group_target(gid):
        for cid in _mixed.get(gid, []):
            if _complete.get(cid) and cid not in _claimed:
                return cid
        pid = 0 if gid == "misc" else gid
        if _complete.get(pid):
            if gid == "misc":
                if "misc" not in _claimed and "0" not in _claimed:
                    return gid
            elif gid not in _claimed:
                return gid
        return None

    target = None
    if aid_str in ("0", "misc"):
        target = _group_target("misc")
    elif aid_str in _mixed:
        target = _group_target(aid_str)
    elif aid_str.isdigit() and 1 <= int(aid_str) <= 33:
        target = _group_target(f"ach_{int(aid_str)}")
    elif _complete.get(aid_str) and aid_str not in _claimed:
        target = aid_str
    if target is None:
        # The group segment does not always name the group whose button
        # was pressed: on 2026-11-12 every claim arrived as group 0,
        # including the press on ach_10's completed child (its reward
        # then refused because misc was already exhausted). When the
        # addressed group has nothing to pay, fall back to the first
        # group (catalogue order) that does: every press then pays a
        # genuinely completed, unclaimed reward instead of refusing,
        # and repeated presses drain the backlog group by group.
        _cands = [t for t in (_group_target(_g) for _g in _mixed) if t]
        if _cands:
            target = _cands[0]
    if target is None:
        return _refuse()
    achs_claimed.append(target)
    entries = _static_entries(target)
    if target.startswith("misc_") and all(
            m in achs_claimed for m in ("misc_1", "misc_2", "misc_3", "misc_4")):
        for marker in ("misc", "0"):
            if marker not in achs_claimed:
                achs_claimed.append(marker)

    # Grant EVERY reward entry (the old code paid only the EP entry and
    # one pack x1, silently dropping the rest of the grade's rewards).
    c = st["coaches"][slot] if st.get("coaches") and slot < len(st["coaches"]) else None
    rewards_list = []
    for e in entries:
        iid, cnt = int(e.get("itemid") or 0), int(e.get("count") or 0)
        if not iid or cnt <= 0:
            continue
        if iid == 1:
            if c:
                c["ep"] = int(c.get("ep", 0)) + cnt
        elif c and hasattr(hc, "inventory_add"):
            try:
                hc.inventory_add(c, iid, cnt)
            except Exception:
                pass
        idef = copy.deepcopy(hc.item_def(iid)) if hasattr(hc, "item_def") else {"itemid": iid, "productid": iid, "name": str(iid), "itemicon": iid}
        idef["count"] = cnt
        idef["itemcnt"] = cnt
        if iid == 1:
            idef["ep"] = cnt
        rewards_list.append(idef)
    _save(st)

    payload = _build_achievement_me_payload(st)
    coach_res = _wrapped_coach_me(st, slot)
    try:
        notify_messenger_push("/achievement/me", payload)
        notify_messenger_push("/coach/me", coach_res)
    except Exception:
        pass

    return hc._json({
        "result": True,
        "rewards": rewards_list,
        "resources": [
            {"resource": "/achievement/me", "uri": "/achievement/me", "body": payload},
            {"resource": "/coach/me", "uri": "/coach/me", "body": coach_res}
        ]
    })


def ep_achievement_questrewards(query, body):
    st = _state()
    gp_claimed = st.setdefault("achievements", {}).setdefault("quest_gp_claimed", [])
    slot = hc._default_slot(st) if st.get("coaches") else 0
    c = st["coaches"][slot] if st.get("coaches") and slot < len(st["coaches"]) else None

    awarded_ep = 0
    pack_id = None
    for target_gp, ep_amt, p_id in [(100, 1000000, None), (200, 2000000, None), (300, 5000000, 28001)]:
        if target_gp not in gp_claimed:
            gp_claimed.append(target_gp)
            awarded_ep = ep_amt
            pack_id = p_id
            if c:
                c["ep"] = int(c.get("ep", 0)) + awarded_ep
                if pack_id and hasattr(hc, "inventory_add"):
                    try:
                        hc.inventory_add(c, pack_id, 1)
                    except Exception:
                        pass
            break

    _save(st)
    payload = _build_achievement_me_payload(st)
    coach_res = _wrapped_coach_me(st, slot)
    try:
        notify_messenger_push("/achievement/me", payload)
        notify_messenger_push("/coach/me", coach_res)
    except Exception:
        pass

    rewards_list = [{"itemid": 1, "type": "ep", "count": awarded_ep or 1000000, "name": "EP"}]
    if pack_id:
        rewards_list.append({"itemid": pack_id, "type": "item", "count": 1, "name": "Thẻ Chỉ Định Top 100"})

    return hc._json({
        "result": True,
        "rewards": rewards_list,
        "resources": [
            {"resource": "/achievement/me", "uri": "/achievement/me", "body": payload},
            {"resource": "/coach/me", "uri": "/coach/me", "body": coach_res}
        ]
    })


def ep_badge_use(bid, query, body):
    st = _state()
    c = hc.my_coach(st)
    if c:
        val = int(bid) if str(bid).isdigit() else 1001
        c["currentbadge"] = val if val >= 1001 else 1001
        _save(st)
        slot = hc._default_slot(st)
        return hc._json({"result": True, "resources": hc.coach_me_resources(st, slot)})
    return hc._json({"result": True})

def _my_coach_id_and_name(st=None):
    if st is None:
        st = _state()
    c = hc.my_coach(st)
    if not c:
        return hc.coach_id(0), "Manager"
    return str(c.get("_id") or hc.coach_id(0)), str(c.get("name") or "Manager")


def _lookup_friend_name_by_id(friend_id):
    _init_user_db()
    with closing(_user_db()) as db:
        row = db.execute("SELECT friend_name FROM friends WHERE friend_id = ? LIMIT 1", (str(friend_id),)).fetchone()
        if row and row["friend_name"]:
            return row["friend_name"]
    info = _find_coach_across_users(target_cid=friend_id)
    if info:
        return info["name"]
    return None


def _build_contact_list_for_coach(my_cid, st=None):
    _init_user_db()
    if st is None:
        st = _state()
    # Groups are plain NAME STRINGS. The Messenger PROXY wraps each
    # name into the {groupname} object its view reads; serving objects
    # ourselves double-wrapped them and the friend list rendered the
    # groups as "[object Object]" (user report 2026-11-12, and the
    # client's own group-delete call came back as
    # /contact/group/%5Bobject%20Object%5D).
    groups = list(st.get("contact_groups") or [])
    friends_out = []
    requested_out = []
    requesting_out = []
    with closing(_user_db()) as db:
        rows = db.execute("""
            SELECT friend_id, friend_name, group_name, status, created_at
            FROM friends WHERE coach_id = ? ORDER BY id ASC
        """, (str(my_cid),)).fetchall()
        for r in rows:
            st_val = r["status"] or "accepted"
            if st_val == "accepted":
                # MessengerProxy.checkFriends consumes exactly these fields
                # (upstream ep_contact_list): a missing status/currdivision,
                # or a "group" key with a name the client does not know,
                # makes it throw — and the messenger never gets ready.
                info = _find_coach_across_users(target_cid=r["friend_id"])
                level, elo, online = 1, LADDER_START_ELO, False
                if info:
                    level = int((info.get("levelinfo") or {}).get("level", 1))
                    elo = int(info.get("elo") or LADDER_START_ELO)
                    online = _is_online(info.get("username"))
                div_name, _div_num = _division_for_elo(elo)
                _entry = {
                    "_id": r["friend_id"],
                    "coachname": r["friend_name"],
                    "status": "online" if online else "offline",
                    "level": level,
                    "currdivision": f"DIVISION_{div_name}",
                }
                # A friend in a named group carries it (the name must exist
                # in "groups" -- an unknown name asserts in the proxy);
                # ungrouped friends omit the key (null = the default group).
                if r["group_name"] and r["group_name"] in (st.get("contact_groups") or []):
                    _entry["group"] = r["group_name"]
                friends_out.append(_entry)
            elif st_val == "requested":
                requested_out.append({
                    "_id": r["friend_id"],
                    "coachname": r["friend_name"],
                    "date": r["created_at"] or "2026-11-06T00:00:00.000Z",
                })
            elif st_val == "requesting":
                requesting_out.append({
                    "_id": r["friend_id"],
                    "coachname": r["friend_name"],
                    "date": r["created_at"] or "2026-11-06T00:00:00.000Z",
                })
    return {
        "groups": groups,
        "friends": friends_out,
        "newfriends": [],
        "requested": requested_out,
        "requesting": requesting_out,
    }


def _build_presence_for_coach(my_cid, st=None):
    cl = _build_contact_list_for_coach(my_cid, st)
    pres = []
    for f in cl.get("friends") or []:
        # Real team/level for coaches that exist on this server; the old
        # build hardcoded teamid 10 / level 23 for everyone -- and "online"
        # for everyone, which is why an offline alt showed as present and
        # could be pulled into rooms (user report 2026-11-12). Presence now
        # follows the contact list's last-seen status.
        teamid, level = 10, 1
        info = _find_coach_across_users(target_cid=f["_id"])
        if info:
            teamid = info["teamid"]
            level = int((info.get("levelinfo") or {}).get("level", 1))
        online = f.get("status") == "online"
        pres.append({
            "_id": f["_id"],
            "id": f["_id"],
            "coachname": f["coachname"],
            "name": f["coachname"],
            "presence": "online" if online else "offline",
            "status": "online" if online else "offline",
            "online": online,
            "teamid": teamid,
            "level": level,
        })
    return pres


def ep_contact_list(query, body):
    st = _state()
    my_cid, _ = _my_coach_id_and_name(st)
    return hc._json(_build_contact_list_for_coach(my_cid, st))


def ep_presence(query, body):
    st = _state()
    my_cid, _ = _my_coach_id_and_name(st)
    return hc._json(_build_presence_for_coach(my_cid, st))


def ep_contact_requesting(query, body):
    """POST /contact/requesting with {"friendname": "<name>"}: send a friend
    request. It stays PENDING (user report 2026-11-12: requests used to be
    accepted instantly): 'requesting' in my list (Da gui yeu cau), 'requested'
    in theirs (Da nhan yeu cau), until they accept or reject. Sending one to
    someone who already asked me counts as accepting theirs."""
    _init_user_db()
    try:
        req = json.loads(body.decode("utf-8")) if body else {}
    except Exception:
        req = {}
    fname = str(req.get("friendname") or "").strip()
    if not fname:
        return hc._json({"result": False, "reason": "not found"})

    st = _state()
    my_cid, my_cname = _my_coach_id_and_name(st)

    found = _find_coach_across_users(target_name=fname)
    if found:
        target_cid = found["cid"]
        target_cname = found["name"]
    else:
        target_cid = "65f1a2b3" + hashlib.md5(fname.lower().encode("utf-8")).hexdigest()[:16]
        target_cname = fname

    if target_cid == my_cid:
        return hc._json({"result": False, "reason": "cannot add self"})

    import datetime as _dt
    now_iso = _dt.datetime.now(_dt.timezone.utc).strftime("%Y-%m-%dT%H:%M:%S.000Z")
    with closing(_user_db()) as db:
        row = db.execute("SELECT status FROM friends WHERE coach_id = ? AND friend_id = ?",
                         (my_cid, target_cid)).fetchone()
        cur = (row["status"] if row else None) or None
        if cur == "requested":
            # They had already asked me: this send accepts the pair.
            db.execute("UPDATE friends SET status = 'accepted' WHERE coach_id = ? AND friend_id = ?",
                       (my_cid, target_cid))
            db.execute("UPDATE friends SET status = 'accepted' WHERE coach_id = ? AND friend_id = ?",
                       (target_cid, my_cid))
        elif cur not in ("accepted", "requesting"):
            db.execute("""
                INSERT OR REPLACE INTO friends (coach_id, friend_id, friend_name, group_name, status, created_at)
                VALUES (?, ?, ?, NULL, 'requesting', ?)
            """, (my_cid, target_cid, target_cname, now_iso))
            db.execute("""
                INSERT OR REPLACE INTO friends (coach_id, friend_id, friend_name, group_name, status, created_at)
                VALUES (?, ?, ?, NULL, 'requested', ?)
            """, (target_cid, my_cid, my_cname, now_iso))
        db.commit()

    cl = _build_contact_list_for_coach(my_cid, st)
    notify_messenger_push("/contact/list", cl, coachid=my_cid)
    cl_target = _build_contact_list_for_coach(target_cid, st)
    notify_messenger_push("/contact/list", cl_target, coachid=target_cid)
    return hc._json({
        **cl,
        "result": True,
        "friend": {"_id": target_cid, "name": target_cname},
        "resources": [
            {"uri": "/contact/list", "body": cl},
        ],
    })


def _friend_pair(cid):
    """(other_id, replier_id) of the friendship/request involving cid, read
    from the friends table itself. Two clients on one PC share an IP, so a
    tokenless request can resolve to the wrong account -- an accept pressed
    on GM3's client then updated the (GM2, GM2) pair and silently did
    nothing (user 2026-11-12). The pair stored in the table is unambiguous."""
    _init_user_db()
    with closing(_user_db()) as db:
        rows = db.execute("SELECT coach_id, friend_id, status FROM friends WHERE coach_id = ? OR friend_id = ?",
                          (str(cid), str(cid))).fetchall()
    other = replier = None
    for a, b, s in rows:
        if other is None:
            other = b if a == str(cid) else a
        if s == "requested" and b == str(cid):
            replier = a
    return other, replier


def ep_contact_reply(cid, query, body):
    """POST /contact/requested/by/<cid>/reply."""
    _init_user_db()
    try:
        req = json.loads(body.decode("utf-8")) if body else {}
    except Exception:
        req = {}
    reply = str(req.get("reply") or "accept")
    st = _state()
    my_cid, _ = _my_coach_id_and_name(st)
    other, replier = _friend_pair(cid)
    a, b = (str(cid), other) if other else (my_cid, str(cid))
    with closing(_user_db()) as db:
        # Both directions of the pair: accepting promotes the sender's
        # 'requesting' row too, rejecting removes it -- touching only my
        # row left the other side stuck in its old state.
        if reply == "accept":
            db.execute("UPDATE friends SET status = 'accepted' WHERE (coach_id = ? AND friend_id = ?) OR (coach_id = ? AND friend_id = ?)",
                       (a, b, b, a))
        else:
            db.execute("DELETE FROM friends WHERE (coach_id = ? AND friend_id = ?) OR (coach_id = ? AND friend_id = ?)",
                       (a, b, b, a))
        db.commit()
    cl = _build_contact_list_for_coach(replier or my_cid, st)
    # The Messenger window's mediators repaint from MessengerProxy's
    # collections, which the proxy refreshes from the mutation RESPONSE
    # itself -- so the list goes at the response's TOP LEVEL (the same
    # shape GET /contact/list returns), not only inside resources: with a
    # resources-only body the open window stayed stale through 10 accept
    # clicks (user 2026-11-12, round 3; DB + push transport both proven
    # good). Both sides also get a targeted push with their OWN list.
    notify_messenger_push("/contact/list", cl, coachid=(replier or my_cid))
    cl_other = _build_contact_list_for_coach(str(cid), st)
    notify_messenger_push("/contact/list", cl_other, coachid=str(cid))
    return hc._json({**cl, "result": True,
                     "resources": [{"uri": "/contact/list", "body": cl}]})


def ep_contact_cancel_requesting(cid, query, body):
    """DELETE /contact/requesting/to/<cid>: withdraw my request -- both rows
    of the pair go, or the other side keeps a phantom 'requested' entry."""
    _init_user_db()
    st = _state()
    my_cid, _ = _my_coach_id_and_name(st)
    other, _replier = _friend_pair(cid)
    a, b = (str(cid), other) if other else (my_cid, str(cid))
    with closing(_user_db()) as db:
        db.execute("DELETE FROM friends WHERE (coach_id = ? AND friend_id = ?) OR (coach_id = ? AND friend_id = ?)",
                   (a, b, b, a))
        db.commit()
    cl = _build_contact_list_for_coach(my_cid, st)
    notify_messenger_push("/contact/list", cl, coachid=my_cid)
    cl_other = _build_contact_list_for_coach(str(cid), st)
    notify_messenger_push("/contact/list", cl_other, coachid=str(cid))
    return hc._json({**cl, "result": True,
                     "resources": [{"uri": "/contact/list", "body": cl}]})


def ep_contact_friend_action(cid, query, body):
    """DELETE /contact/list/<cid> or PUT /contact/list/<cid>/group."""
    _init_user_db()
    try:
        req = json.loads(body.decode("utf-8")) if body else {}
    except Exception:
        req = {}
    st = _state()
    my_cid, _ = _my_coach_id_and_name(st)
    with closing(_user_db()) as db:
        if isinstance(req, dict) and req.get("_method") == "DELETE":
            db.execute("DELETE FROM friends WHERE coach_id = ? AND friend_id = ?", (my_cid, str(cid)))
            db.execute("DELETE FROM friends WHERE coach_id = ? AND friend_id = ?", (str(cid), my_cid))
        else:
            gname = req.get("groupname")
            db.execute("UPDATE friends SET group_name = ? WHERE coach_id = ? AND friend_id = ?", (gname, my_cid, str(cid)))
        db.commit()
    cl = _build_contact_list_for_coach(my_cid, st)
    notify_messenger_push("/contact/list", cl, coachid=my_cid)
    cl_other = _build_contact_list_for_coach(str(cid), st)
    notify_messenger_push("/contact/list", cl_other, coachid=str(cid))
    return hc._json({**cl, "result": True,
                     "resources": [{"uri": "/contact/list", "body": cl}]})


def ep_contact_newfriends(query, body):
    return hc._json({"result": True})


def ep_contact_group(query, body):
    try:
        req = json.loads(body.decode("utf-8")) if body else {}
    except Exception:
        req = {}
    st = _state()
    gname = str(req.get("groupname") or "").strip()
    groups = st.setdefault("contact_groups", [])
    if gname and gname not in groups:
        groups.append(gname)
        _save(st)
    my_cid, _ = _my_coach_id_and_name(st)
    cl = _build_contact_list_for_coach(my_cid, st)
    # The Messenger window's group dialog reads its sign-on copy of the
    # list; without a push it kept offering the DELETED group's name
    # after a delete + recreate (user report 2026-11-12).
    notify_messenger_push("/contact/list", cl, coachid=my_cid)
    return hc._json({"result": True, "resources": [{"uri": "/contact/list", "body": cl}]})


def ep_contact_group_delete(gname, query, body):
    st = _state()
    groups = st.setdefault("contact_groups", [])
    g_dec = urllib.parse.unquote(str(gname))
    if g_dec in groups:
        groups.remove(g_dec)
        _save(st)
    my_cid, _ = _my_coach_id_and_name(st)
    cl = _build_contact_list_for_coach(my_cid, st)
    notify_messenger_push("/contact/list", cl, coachid=my_cid)
    return hc._json({"result": True, "resources": [{"uri": "/contact/list", "body": cl}]})


def _calc_team_value(st, slot=0):
    team = (st.get("teams") or {}).get(str(slot), {})
    players = team.get("players", [])
    if not players:
        return 0
    return sum(hc.card_price(p.get("spid", 0), hc.grade_of(p)) for p in players if hc.grade_of(p) > 0)


def _coach_rank_entries():
    """Real coach-ranking rows for /rank/teamvalue/*: one per account on
    this server, built like the ladder friends rows (real team, real
    level, real squad OVR, real team value). The old build returned the
    requester plus four INVENTED rivals (AlexFerguson 1.5B, ...) whose
    coach ids (90001+) exist nowhere -- selecting or searching one made
    the client call /achievement/<id> and /ladder/<id>/info, which had
    no routes at all, and the client answered "HLV ko tồn tại" for
    every name. Alias fields (coachName, levelinfo) mirror the shapes
    the ranking tab reads elsewhere."""
    _init_user_db()
    st_me = _state()
    my_cid, _ = _my_coach_id_and_name(st_me)
    entries = []
    with closing(_user_db()) as db:
        users = db.execute("SELECT id, username, state_json FROM users ORDER BY id ASC").fetchall()
    for u in users:
        try:
            st_u = _ensure_unique_coach_ids(json.loads(u["state_json"] or "{}"), u["id"])
        except Exception:
            continue
        coaches_u = st_u.get("coaches") or []
        if not coaches_u:
            continue
        c_u = coaches_u[0]
        cid_u = str(c_u.get("_id") or _coach_id_for_user(u["id"], int(c_u.get("slot", 0))))
        slot_u = int(c_u.get("slot", 0))
        level_u = 1
        try:
            level_u = int(hc.coach_levelinfo(c_u).get("level", 1))
        except Exception:
            pass
        ovr_u = {"FW": 0, "MF": 0, "DF": 0}
        try:
            team_u = (st_u.get("teams") or {}).get(str(slot_u)) or hc.roster_team(int(c_u.get("teamid", 1)))
            sum_u = hc.team_summary(team_u, int(c_u.get("teamid", 1))) or {}
            o = sum_u.get("ovr") or {}
            ovr_u = {k: int(o.get(k, 0)) for k in ("FW", "MF", "DF")}
        except Exception:
            pass
        name_u = str(c_u.get("name") or u["username"])
        entries.append({
            "_id": cid_u, "id": cid_u, "coachid": cid_u,
            "coachname": name_u, "coachName": name_u, "name": name_u,
            "teamid": int(c_u.get("teamid") or 1),
            "level": level_u, "levelinfo": {"level": level_u},
            "ovr": ovr_u,
            "teamvalue": {"val": _calc_team_value(st_u, slot_u), "rank": 0, "fluc": 0, "updown": 0},
            "hiderank": False, "isMe": cid_u == my_cid,
        })
    entries.sort(key=lambda e: (-e["teamvalue"]["val"], e["name"]))
    for i, e in enumerate(entries):
        e["rank"] = i + 1
        e["ranking"] = i + 1
        e["teamvalue"]["rank"] = i + 1
    return entries


def ep_rank_teamvalue(query, body):
    """GET /rank/teamvalue/club: my CLUB's members (real Team system: the
    members of the club I belong to; just me while I have no club -- the
    tab must not pool every coach on the server into one fake clan)."""
    st = _state()
    my_cid, _ = _my_coach_id_and_name(st)
    keep = {my_cid}
    club = _club_of_coach(my_cid)
    if club:
        with closing(_user_db()) as db:
            keep |= {r[0] for r in db.execute(
                "SELECT coach_id FROM club_members WHERE clubid = ?",
                (club["clubid"],)).fetchall()}
    rows = [e for e in _coach_rank_entries() if e["coachid"] in keep]
    for i, e in enumerate(rows):
        e["rank"] = i + 1
        e["ranking"] = i + 1
        e["teamvalue"]["rank"] = i + 1
    return hc._json(rows)


def ep_rank_teamvalue_friend(query, body):
    """GET /rank/teamvalue/friend: the requester plus accepted friends."""
    st = _state()
    my_cid, _ = _my_coach_id_and_name(st)
    cl = _build_contact_list_for_coach(my_cid, st)
    keep = {my_cid} | {str(f["_id"]) for f in cl.get("friends") or []}
    rows = [e for e in _coach_rank_entries() if e["coachid"] in keep]
    for i, e in enumerate(rows):
        e["rank"] = i + 1
        e["ranking"] = i + 1
        e["teamvalue"]["rank"] = i + 1
    return hc._json(rows)


def ep_coach_profile_achievement(cid, query, body):
    """GET /achievement/<coachid>: another coach's achievement payload
    (the ranking screen loads it when a row is selected/searched)."""
    if str(cid) in ("me", "my"):
        return ep_achievement_me(query, body)
    found = _find_coach_across_users(target_cid=str(cid))
    if not found:
        return hc._json({"result": False, "reason": "not exist coach"})
    return hc._json(_build_achievement_me_payload(found["st"]))


def ep_coach_profile_ladder(cid, query, body):
    """GET /ladder/<coachid>/info: another coach's ladder statistics."""
    if str(cid) in ("me", "my"):
        return ep_ladder_my_info(query, body)
    found = _find_coach_across_users(target_cid=str(cid))
    if not found:
        return hc._json({"result": False, "reason": "not exist coach"})
    return hc._json(_coach_ladder_statistics(found["st"]))


def _slot_of_coach(st, cid):
    for c in st.get("coaches") or []:
        if str(c.get("_id")) == str(cid):
            return int(c.get("slot", 0))
    return hc._default_slot(st)


def _recorded_coach_identity(cid):
    """(name, teamid) for a coach id that belongs to no account -- a bot
    persona from one of MY OWN multiplay records. Bot ids are minted per
    match (hc.new_iid()), so the record that carries the id is the only
    place its name/team survive."""
    try:
        c = hc.my_coach(_state()) or {}
        for e in c.get("multiplay") or []:
            for side in ("team", "opponent"):
                s = e.get(side) or {}
                for co in s.get("coaches") or []:
                    if str(co.get("coachid")) == str(cid):
                        return str(co.get("name") or "HLV"), int(s.get("teamid") or 0)
    except Exception:
        pass
    return None, 0


def _unknown_coach_summary(cid):
    """A renderable summary shell for a non-account coach (a bot persona
    clicked in the multiplay results). Answering {"result": false,
    "reason": "not exist coach"} made the client raise its UI-error
    popup the moment such a coach was selected (user report 2026-11-12,
    telemetry /coach/139956aa.../summary). The shell carries the name
    and team from the record when known; statistics stay empty, which
    the popup renders as its normal "-" state."""
    name, _tid = _recorded_coach_identity(cid)
    return hc._json({"coach": {"_id": str(cid), "id": str(cid),
                               "name": name or "HLV",
                               "levelinfo": {"level": 1},
                               "teamvalue": {"val": 0},
                               "currentbadge": 1001, "playtime": 0,
                               "profile": {"views": 0, "description": ""},
                               "statistics": {}},
                     "friends": 0})


def ep_coach_summary(cid, query, body):
    """GET /coach/<coachid>/summary?view=true: another coach's profile for
    the ranking Info popup (CoachUserPopupScreen). The route did not exist,
    so the client got {} and CoachTotalRecordTabPage crashed on the proxy's
    default 0s (AS3 #1069 in setFriendRecord)."""
    found = _find_coach_across_users(target_cid=str(cid))
    if not found:
        return _unknown_coach_summary(cid)
    st = found["st"]
    me = hc.coach_me(st, _slot_of_coach(st, found["cid"]))
    friends = _build_contact_list_for_coach(found["cid"], st).get("friends", [])
    # CoachSummaryTabPage reads Coach.statistics.{league,friendly,ladder}
    # (leaguewins / friendly quick+custom wins / ladder seasons) and
    # Coach.profile.views: serving ladder alone left every other figure
    # at the page's "-" empty state even for coaches with records.
    stats = dict(me.get("statistics") or {})
    stats["ladder"] = _coach_ladder_statistics(st)
    profile = dict(me.get("profile") or {})
    profile["views"] = int(profile.get("views") or 0)
    if "description" not in profile:
        profile["description"] = ""
    return hc._json({"coach": {"_id": found["cid"], "id": found["cid"],
                               "name": me.get("name"), "levelinfo": me.get("levelinfo"),
                               "teamvalue": {"val": _calc_team_value(st, _slot_of_coach(st, found["cid"]))},
                               "currentbadge": me.get("currentbadge"),
                               "playtime": me.get("playtime"),
                               "profile": profile,
                               "statistics": stats},
                     # The popup's friend counter does arithmetic on this
                     # field -- upstream serves the COUNT (their constant
                     # is the number 20). Serving the list made the
                     # counter render NaN (user report 2026-11-12).
                     "friends": len(friends)})


def ep_coach_by_id(cid, query, body):
    """GET /coach/<coachid>?path=team: his team object itself; without the
    path, a coach object like /coach/me for the squad popup."""
    found = _find_coach_across_users(target_cid=str(cid))
    if not found:
        # Non-account coach (bot persona): a minimal coach + empty team
        # shell, mirroring _unknown_coach_summary -- {} here would break
        # the popup's team tab the same way the summary error did.
        name, tid = _recorded_coach_identity(cid)
        team = {"teamid": tid, "players": [], "teamvalue": {"val": 0},
                "formations": [], "teamsquadlist": {"preset": []}}
        if "path=team" in (query or ""):
            return hc._json(team)
        return hc._json({"id": str(cid), "_id": str(cid),
                         "name": name or "HLV",
                         "teamvalue": {"val": 0}, "teamid": tid,
                         "levelinfo": {"level": 1}, "team": team,
                         "kit": {}, "summary": {},
                         "formations": [], "teamsquadlist": {"preset": []}})
    st = found["st"]
    slot = _slot_of_coach(st, found["cid"])
    me = hc.coach_me(st, slot)
    team = me.get("team") or {}
    # The popup's team tab reads teamvalue.val (upstream's shape); the team
    # object carries none (for /coach/me the client sums the cards itself),
    # so another coach's squad showed "Gia tri doi: 0 EP" (user report
    # 2026-11-12). A plain number there read as .val is undefined -> still
    # 0, so serve the {"val": ...} object everywhere the popup may read it.
    team["teamvalue"] = {"val": _calc_team_value(st, slot)}
    if "path=team" in (query or ""):
        return hc._json(team)
    return hc._json({"id": found["cid"], "_id": found["cid"], "name": me.get("name"),
                     "teamvalue": team["teamvalue"],
                     "teamid": me.get("teamid"), "levelinfo": me.get("levelinfo"),
                     "team": team, "kit": team.get("kit"), "summary": team.get("summary"),
                     "formations": team.get("formations", []),
                     "teamsquadlist": team.get("teamsquadlist") or {"preset": []}})


def ep_coach_teamvalue_history(query, body):
    """GET /coach/me/teamvalue/history: CoachRankingTabPage reads event.body.values (Array of 30 numbers)."""
    st = _state()
    slot = hc._default_slot(st) if st.get("coaches") else 0
    tval = _calc_team_value(st, slot)
    return hc._json({"values": [tval] * 30})


def ep_coach_hiderank(query, body):
    return hc._json({"result": True})


# =========================================================================== #
# 5. HOOKS INTO http_config (account, coach_me, match ready/end, H._respond)  #
# =========================================================================== #

_MATCH_MESSAGES = {}


def ep_match_send_message(mid, query, body):
    try:
        req = json.loads(body.decode("utf-8") if body else "{}")
    except Exception:
        req = {}
    st = _state()
    c = hc.my_coach(st)
    cname = c.get("name") if c else "HLV"
    text = str(req.get("message") or "").strip()
    target = str(req.get("target") or "all")
    # Match-chat dialect (upstream's working implementation, confirmed by
    # the telemetry stack): the overlay shows a pushed line as
    # "coachname: body", gray when myside is false. The text field is
    # "body", NOT the global channel's "message" -- our old VO carried the
    # global shape (message/coachid/hint/time), so the client read a null
    # body, routed the line into ChattingArea.onGMChatReceived and died in
    # StringUtil.entityHTML with #1009 (telemetry 2026-11-11 and again
    # 2026-11-12 15:50 UTC on the user's own "hi"). The resource is a LIST
    # of these VOs; the client renders a typed line only when it comes
    # back in this push.
    msg_vo = {
        "type": "chat",
        "target": target if target != "all" else "",
        "coachname": cname,
        "body": text,
        "myside": True,
    }
    msgs = _MATCH_MESSAGES.setdefault(mid, [])
    msgs.append(msg_vo)
    if len(msgs) > 50:
        _MATCH_MESSAGES[mid] = msgs[-50:]
    try:
        notify_messenger_push(f"/match/{mid}/messages/latest", [msg_vo])
    except Exception:
        pass
    return hc._json({"result": True})


def ep_match_messages_latest(mid, query, body):
    msgs = _MATCH_MESSAGES.get(mid, [])
    return hc._json(msgs[-1:] if msgs else [])


_orig_coach_me = None
_orig_match_end = None
_orig_ep_account = None


def _patch_json_files_on_disk():
    """Ensure responses/account_base.json, account_features.json, and static_db_STATICVALUES.json
    have all required guides, features, and Chatting static values on disk."""
    try:
        acc_path = os.path.join(hc.RESPONSES, "account_base.json")
        if os.path.isfile(acc_path):
            with open(acc_path, "r", encoding="utf-8") as f:
                acc = json.load(f)
            changed = False
            guides = acc.setdefault("guides", [])
            for g in COMPLETED_GUIDES:
                if g not in guides:
                    guides.append(g)
                    changed = True
            feats = acc.setdefault("features", {})
            if _merge_account_features(feats):
                changed = True
            if _strip_liveboost(feats):
                changed = True
            if _strip_hidden_ui(feats):
                changed = True
            if changed:
                with open(acc_path, "w", encoding="utf-8") as f:
                    json.dump(acc, f, indent=2)
    except Exception:
        pass

    try:
        feat_path = os.path.join(hc.RESPONSES, "account_features.json")
        if os.path.isfile(feat_path):
            with open(feat_path, "r", encoding="utf-8") as f:
                feats = json.load(f)
            changed = _merge_account_features(feats)
            if _strip_liveboost(feats):
                changed = True
            if _strip_hidden_ui(feats):
                changed = True
            if changed:
                with open(feat_path, "w", encoding="utf-8") as f:
                    json.dump(feats, f, indent=2)
    except Exception:
        pass

    try:
        sv_path = os.path.join(hc.RESPONSES, "static_db_STATICVALUES.json")
        if os.path.isfile(sv_path):
            with open(sv_path, "r", encoding="utf-8") as f:
                sv = json.load(f)
            if sv.get("Chatting") != CHATTING_STATICVALUES:
                sv["Chatting"] = CHATTING_STATICVALUES
                with open(sv_path, "w", encoding="utf-8") as f:
                    json.dump(sv, f, indent=2)
            # Bonus 1 (left box, 2026-11-12): SquadScreenHandler builds the
            # preset-squad picker (Squad A/B/C) from STATICVALUES
            # limit.preset_squad_count; without it the picker is an empty
            # box on the squad screens. No preset_squad_level keys = every
            # slot open.
            lim = sv.setdefault("limit", {})
            if lim.get("preset_squad_count") != 3:
                lim["preset_squad_count"] = 3
                with open(sv_path, "w", encoding="utf-8") as f:
                    json.dump(sv, f, indent=2)
    except Exception:
        pass

    try:
        ach_path = os.path.join(hc.RESPONSES, "achievement_me.json")
        with open(ach_path, "w", encoding="utf-8") as f:
            json.dump(_build_achievement_me_payload({"ranked": {}}), f, indent=2)
    except Exception:
        pass


def ep_account_ext(query, body):
    ctype, data = _orig_ep_account(query, body)
    try:
        acc = json.loads(data.decode("utf-8"))
        guides = acc.setdefault("guides", [])
        for g in COMPLETED_GUIDES:
            if g not in guides:
                guides.append(g)
        feats = acc.setdefault("features", {})
        _merge_account_features(feats)
        _strip_liveboost(feats)
        _strip_hidden_ui(feats)
        st = _state()
        slot = hc._default_slot(st) if st.get("coaches") else 0
        c_raw = (st.get("coaches") or [{}])[slot] if st.get("coaches") else {}
        has_pass = bool(st.get("second_pass") or c_raw.get("second_pass"))
        acc["status"] = {"status": "permitted" if has_pass else "notset"}
        return hc._json(acc)
    except Exception:
        return (ctype, data)


def ep_beginnersguide(query, body):
    return hc._json({"result": True, "guides": COMPLETED_GUIDES})


def _deep_merge_stats(target, default):
    for k, v in default.items():
        if k not in target or target[k] is None:
            import copy
            target[k] = copy.deepcopy(v)
        elif isinstance(v, dict) and isinstance(target[k], dict):
            _deep_merge_stats(target[k], v)
    return target


def _wrapped_coach_me(st, slot):
    res = _orig_coach_me(st, slot)
    rk_stats = _coach_ladder_statistics(st)
    stats = res.get("statistics")
    if not isinstance(stats, dict):
        stats = hc.default_coach_statistics()
    else:
        _deep_merge_stats(stats, hc.default_coach_statistics())
    
    # CoachTotalRecordTabPage.setFriendRecord reads
    # Friend.quick.winningstreak.max.length / max[0]: max must be an ARRAY.
    # A stored statistics blob with the old scalar shape would survive the
    # defaults merge (the key exists), so normalize every friendly row here.
    for _row in (stats.get("friendly") or {}).values():
        if isinstance(_row, dict):
            _ws = _row.get("winningstreak")
            if isinstance(_ws, dict) and not isinstance(_ws.get("max"), list):
                _ws["max"] = [int(_ws.get("max") or 0)]
    stats["ladder"] = rk_stats
    # Issue 1: highrecords.division MUST be a list of {"name", "rank"} objects.
    # AS3 LadderMatchController.rebuildLadderRecordInfo reads division[i].name;
    # rk_stats["division"] is a plain string ("DIVISION_..."), so [string] * 10
    # raised AS3 Error #1069 -> "Loi giao dien" on the Coach Profile screen.
    # _coach_ladder_statistics() already builds the correct best-division object
    # in rk_stats["highrecords"]["division"] -- reuse it, with a safe fallback
    # that converts any string/dict shape into the same object form.
    _hr_division = (rk_stats.get("highrecords") or {}).get("division") or []
    if _hr_division and isinstance(_hr_division[0], dict):
        best_div = _hr_division[0]
    else:
        _div_val = rk_stats.get("bestdivision") or rk_stats.get("division")
        if isinstance(_div_val, dict):
            best_div = _div_val
        else:
            _div_name = str(_div_val or "DIVISION_AMATEUR_C")
            if not _div_name.startswith("DIVISION_"):
                _div_name = f"DIVISION_{_div_name}"
            # Rank must match the name: LADDER_DIVISIONS order is rank order.
            _div_suffix = _div_name[len("DIVISION_"):]
            _div_rank = next((i + 1 for i, d in enumerate(LADDER_DIVISIONS)
                              if d["name"] == _div_suffix), 16)
            best_div = {"name": _div_name, "rank": _div_rank}
    cur_elo = int(rk_stats.get("elo", 1000))
    stats["ladder"]["highrecords"] = {
        "count": rk_stats.get("total_matches", 0),
        "elo": [cur_elo] * 10,
        "division": [best_div] * 10,
        "rank": [0] * 10,
        "winningstreak": [rk_stats.get("streak", 0)] * 10,
        "wins": [rk_stats.get("wins", 0)] * 10,
        "losses": [rk_stats.get("losses", 0)] * 10,
        "date": "2026-11-01T00:00:00.000Z"
    }
    l_stats = stats.setdefault("league", {})
    l_stats.setdefault("leaguewins", [0, 0, 0, 0])
    l_stats.setdefault("normalwins", 0)
    l_stats.setdefault("noramlbest", 0)
    l_stats.setdefault("normalbest", 0)
    l_stats.setdefault("ultimatebest", 0)
    l_stats.setdefault("ultimatewins", 0)
    stats.setdefault("winstreak", [])
    res["statistics"] = stats
    default_club_stats = {
        "total": 0, "wins": 0, "losses": 0, "draws": 0,
        "forfeitwins": 0, "points": 0, "goals": 0, "conceded": 0
    }
    # Guarantee boxingday pvp and pve have draws and bestrank to avoid ActionScript TypeError #1009
    bd = stats.setdefault("boxingday", {})
    for mode in ("pvp", "pve"):
        m_dict = bd.setdefault(mode, {})
        m_dict.setdefault("draws", 0)
        m_dict.setdefault("bestrank", 0)
        m_dict.setdefault("wins", 0)
        m_dict.setdefault("losses", 0)
        m_dict.setdefault("maxstreak", 0)
        m_dict.setdefault("currstreak", 0)

    stats["club"] = {
        "ranked": dict(default_club_stats),
        "friendly": dict(default_club_stats)
    }
    res["club"] = {
        "clubid": 0, "name": "",
        "ranked": dict(default_club_stats),
        "friendly": dict(default_club_stats)
    }
    res["currdivision"] = rk_stats.get("division")
    # Owned badges = the badge of every completed achievement group.
    # The old setdefault never overrode the base payload's empty list,
    # so the badge screen showed only the current badge.
    try:
        _payload = _build_achievement_me_payload(st)
        _complete = {a["id"]: a["complete"] for a in _payload.get("achievement", [])}
        _badge_of = {it["id"]: it["badge"] for it in STATIC_ACHIEVEMENT_INFO["list"] if it.get("badge")}
        _owned = set()
        _parents = set(STATIC_ACHIEVEMENT_INFO["mixed"].keys())
        for _pid, _done in _complete.items():
            if not _done:
                continue
            _sid = "misc" if _pid == 0 else _pid
            if _sid in _parents and _sid in _badge_of:
                _owned.add(_badge_of[_sid])
        _owned.add(int(res.get("currentbadge") or 1001))
        res["badges"] = sorted(_owned)
    except Exception:
        res.setdefault("badges", [1001, 1002, 1003, 1004, 1005, 1006])
    res["currentbadge"] = int(res.get("currentbadge") or 1001)
    res["hiderank"] = False
    # Team (Clan): coach.club.clubid links the coach to their club
    # (null = in no club; the client reads its emblem from the id).
    try:
        _me_cid, _ = _my_coach_id_and_name(st)
        _my_club = _club_of_coach(_me_cid)
        res["club"] = {"clubid": _my_club["clubid"]} if _my_club else None
    except Exception:
        res["club"] = None
    if "summary" in res and isinstance(res["summary"], dict):
        res["summary"]["name"] = res.get("name") or "HLV"
    return res
def _side_ready(side):
    def step(match, req):
        ready_val = bool(req.get("ready", True))
        cur_stage = str(req.get("stage") or match.get("stage") or "squad")
        if side in match["side"]:
            match["side"][side]["ready"] = ready_val
        if ready_val:
            match["side"]["right"]["ready"] = True
            if cur_stage == "squad" or match.get("stage") == "squad":
                match["stage"] = "setting"
                match["side"]["left"]["ready"] = False
                match["side"]["right"]["ready"] = False
            elif cur_stage == "setting" or match.get("stage") == "setting":
                match["stage"] = "playing"
    return step


def ep_match_ready(mid, side, query, body):
    return hc.match_step(mid, body, _side_ready(side))


def _player_ready(match, req):
    cur_stage = str(req.get("stage") or match.get("stage") or "squad")
    if cur_stage == "created" and match.get("stage") == "created":
        match["stage"] = "squad"
        match["side"]["left"]["ready"] = False
        match["side"]["right"]["ready"] = False
        return
    if cur_stage == "squad" or match.get("stage") == "squad":
        match["stage"] = "setting"
        match["side"]["left"]["ready"] = False
        match["side"]["right"]["ready"] = False
        return
    if cur_stage == "setting" or match.get("stage") == "setting":
        match["side"]["left"]["ready"] = True
        match["side"]["right"]["ready"] = True
        match["stage"] = "playing"
        return
    if req.get("ready", True) and req.get("stage", match["stage"]) == match["stage"]:
        match["stage"] = hc.NEXT_STAGE.get(match["stage"], match["stage"])


def ep_match_player_ready(mid, query, body):
    return hc.match_step(mid, body, _player_ready)


def ep_match_end(mid, query, body):
    req = json.loads(body or b"{}")
    st = _state()
    lg = st.get("league2") or {}
    p = lg.get("pending")
    match = st.get("match") if (st.get("match") or {}).get("id") == mid else None
    out = {"result": True}
    ranked_finished = False
    if match is not None and match.get("stage") != "closed":
        # Player contracts: one match off every squad card (40-match
        # contracts; hc's own ep_match_end did this but is shadowed by
        # this handler, so contracts never ticked -- user 2026-11-12).
        hc.consume_match_contracts(st)
        match["result"] = {k: v for k, v in req.items() if k != "_method"}
        match["reward"] = hc.match_reward(st, req)
        # Bug 3: career counters feeding the achievement metrics (career
        # goals, clean sheets, win streaks) -- only totals existed before,
        # so whole mission themes were unmeasurable. Counts room matches.
        _me, _opp = req.get("myTeam", "home"), req.get("opponentTeam", "away")
        _gs = lambda s: int((req.get(s) or {}).get("score") or 0)
        _car = st.setdefault("career", {})
        _car["total_goals"] = int(_car.get("total_goals", 0)) + _gs(_me)
        _mtype = str(match.get("type") or "")
        if _mtype == "Ranked_PvP_1v1":
            _car["ranked_goals"] = int(_car.get("ranked_goals", 0)) + _gs(_me)
            if _gs(_opp) == 0:
                _car["ranked_cleansheets"] = int(_car.get("ranked_cleansheets", 0)) + 1
            if _gs(_me) > _gs(_opp) and not int(req.get("forfeits") or 0):
                _car["ranked_curr_streak"] = int(_car.get("ranked_curr_streak", 0)) + 1
                _car["ranked_best_streak"] = max(int(_car.get("ranked_best_streak", 0)),
                                                 _car["ranked_curr_streak"])
            else:
                _car["ranked_curr_streak"] = 0
        elif "MGR" in _mtype:
            _car["mgr_goals"] = int(_car.get("mgr_goals", 0)) + _gs(_me)
        # "Ket qua nhieu tran" (CoachMultiResultTabPage reads
        # /coach/me?path=multiplay): record ranked/friendly room matches on
        # the raw coach. The tab was always empty -- coach_me served no
        # "multiplay" key and nothing recorded results (user 2026-11-12).
        if _mtype.startswith(("Ranked", "Friendly")):
            _coach = hc.my_coach(st)
            _right = ((match.get("side") or {}).get("right") or {})
            _opp_players = _right.get("players") or []
            _opp_coaches = ([{"coachid": _opp_players[0].get("_id") or _opp_players[0].get("id"),
                              "name": _opp_players[0].get("name") or ""}] if _opp_players else [])
            if not _opp_coaches and match.get("_opp_coach"):
                # Bot match: side.right.players stays [] (round 2c), but
                # the results tab counts coaches per side from HERE --
                # an empty array rendered the mode column as "0 vs 1"
                # (user report 2026-11-12).
                _opp_coaches = [match["_opp_coach"]]
            # The opponent's teamid lives on side.right.team.teamid (the
            # side itself carries neither teamid nor authTeamId): reading
            # the side directly stored 0 and the row rendered the
            # localization fallback "teamname(0)".
            _opp_tid = int(_right.get("teamid")
                           or ((_right.get("team") or {}).get("teamid"))
                           or _right.get("authTeamId") or 0)
            _my_score, _opp_score, _forfeits = _gs(_me), _gs(_opp), 0
            if int(req.get("forfeits") or 0) > 0:
                # A forfeit sends the unfinished 0-0 line; recorded as-is
                # the row showed a DRAW ("H"). A forfeit is recorded as a
                # 0-5 loss -- 5-0 is the standard forfeit scoreline the
                # user expects (the 0-3 from the first fix was wrong).
                _my_score, _opp_score, _forfeits = 0, 5, 1
            _entry = {
                "type": _mtype, "date": hc.w3c(time.time()), "forfeits": _forfeits,
                "team": {"teamid": int(_coach.get("teamid") or 0), "score": _my_score,
                         "shootout": int((req.get(_me) or {}).get("shootout") or 0),
                         "coaches": [{"coachid": _coach.get("_id"), "name": _coach.get("name")}]},
                "opponent": {"teamid": _opp_tid,
                             "score": _opp_score,
                             "shootout": int((req.get(_opp) or {}).get("shootout") or 0),
                             "clubname": "", "coaches": _opp_coaches},
            }
            _coach["multiplay"] = ((_coach.get("multiplay") or []) + [_entry])[-20:]
        if match.get("_ranked"):
            ranked_finished = True
            me, opp = req.get("myTeam", "home"), req.get("opponentTeam", "away")
            g = lambda s: int((req.get(s) or {}).get("score") or 0)
            # Bug 2a (round 2): the end payload carries a top-level
            # "forfeits" count -- abandoning a match sends the unfinished
            # 0-0 scoreline, so a score-only comparison recorded a forfeit
            # as a DRAW (+5 elo). A forfeit is a loss regardless of score.
            forfeited = int(req.get("forfeits") or 0) > 0
            won = (not forfeited) and g(me) > g(opp)
            drew = (not forfeited) and g(me) == g(opp)
            # Bug 2a-2: a manager/simulated ranked match updates the
            # manager record (ladder index 3), not the 1v1 record.
            rk = _ranked_record_for_match(st, match)
            delta = 25 if won else (5 if drew else -15)
            rk["elo"] = max(0, int(rk.get("elo", LADDER_START_ELO)) + delta)
            rk["highelo"] = max(int(rk.get("highelo", LADDER_START_ELO)), rk["elo"])
            if won:
                rk["wins"] = int(rk.get("wins", 0)) + 1
                rk["streak"] = int(rk.get("streak", 0)) + 1
            elif drew:
                rk["draws"] = int(rk.get("draws", 0)) + 1
                rk["streak"] = 0
            else:
                rk["losses"] = int(rk.get("losses", 0)) + 1
                rk["streak"] = 0
            # Bug 2a (round 4): per-match bookkeeping the client reads --
            # results: placement win flags (firstround.wins); forfeits:
            # the friends/season arrays; weekwins: ladder_total.totalwins.
            results = rk.setdefault("results", [])
            results.append(1 if won else (0 if drew else -1))
            del results[:-100]
            if forfeited:
                rk["forfeits"] = int(rk.get("forfeits", 0)) + 1
            import datetime as _dt3
            wk_key = _dt3.datetime.now().strftime("%G%V")
            ww = rk.get("weekwins") or {}
            if ww.get("week") != wk_key:
                ww = {"week": wk_key, "wins": 0}
            if won:
                ww["wins"] = int(ww.get("wins", 0)) + 1
            rk["weekwins"] = ww
            # Bug 2a (round 5): rating history for the record tab's
            # daily graph (seasons[].elos entries need {elo, dateymd}).
            # One point per day (upstream): a second match the same day
            # replaces the day's point instead of appending another.
            hist = rk.setdefault("elos", [])
            _today = _dt3.datetime.now().strftime("%Y%m%d")
            if hist and str(hist[-1].get("dateymd")) == _today:
                hist[-1] = {"elo": int(rk["elo"]), "dateymd": _today}
            else:
                hist.append({"elo": int(rk["elo"]), "dateymd": _today})
            del hist[:-60]
            # Record the match in the client's recentmatches VO
            # {date, goals, goalsconceded, shootout, shootoutconceded,
            # streak, elodiff, elo, forfeits}, oldest first, last 3 --
            # the shape _recent_served serves (upstream's recorder).
            # Draws are skipped: the record screen's streak has no draw.
            # _recent_served also converts any legacy entries still in
            # storage, so the list self-heals on the next played match.
            if not drew:
                recent = _recent_served(rk)
                _prev = recent[-1]["streak"] if recent else 0
                _streak = (_prev + 1 if _prev > 0 else 1) if won else \
                    (_prev - 1 if _prev < 0 else -1)
                recent.append({
                    "date": hc.w3c(time.time()),
                    # A forfeit arrives as 0-0; the record shows the
                    # standard 0-5 forfeit line instead (user 2026-11-12).
                    "goals": 0 if forfeited else g(me),
                    "goalsconceded": 5 if forfeited else g(opp),
                    "shootout": 0,
                    "shootoutconceded": 0,
                    "streak": _streak,
                    "elodiff": int(delta),
                    "elo": int(rk["elo"]),
                    "forfeits": 1 if forfeited else 0,
                })
                rk["recent"] = recent[-3:]
            # Grant +500,000 EP Ranked match bonus (x25 scale, user 2026-11-12)
            match["reward"]["money"]["reward"]["total"] += 500000
            match["reward"]["money"]["reward"]["detail"]["extra"] += 500000
            # Bug 2b: the reward screen's balance animation (money
            # prev -> current) must include the bonus it credits below.
            match["reward"]["money"]["current"] += 500000
        hc.add_ep(st, match["reward"]["money"]["reward"]["total"])
    cp = st.get("cup_pending") or {}
    if cp.get("id") == mid and cp.get("cupid") in (st.get("cups") or {}):
        h, a = req.get("home") or {}, req.get("away") or {}
        gh, ga = int(h.get("score") or 0), int(a.get("score") or 0)
        sh, sa = int(h.get("shootout") or 0), int(a.get("shootout") or 0)
        if str(h.get("id")) == str(cp["away"]):
            gh, ga, sh, sa = ga, gh, sa, sh
        st["cups"][cp["cupid"]]["_mine"] = (gh, ga, sh, sa)
        st.pop("cup_pending")
    if p and p["id"] == mid:
        h, a = req.get("home") or {}, req.get("away") or {}
        gh, ga = int(h.get("score") or 0), int(a.get("score") or 0)
        if str(h.get("id")) == str(p["away"]):
            gh, ga = ga, gh
        # Fold the played match's per-player lines into the season
        # records (the league Data tab's player table). hc's own
        # ep_match_end did this, but install() shadows it with this
        # handler -- played league matches recorded NOTHING (only sims
        # did, via ep_l2_match; user report 2026-11-12).
        hc.record_league_match_players(st, req, f"league:{p.get('round')}")
        out["leagueparam"] = hc.league_play_round(st, p["round"], (gh, ga))
        lg.pop("pending", None)
        out["resources"] = hc.l2_resources(st)
    if match is not None:
        match["stage"] = "closed"
        res_list = [{"uri": f"/match/{mid}", "body": match}] + out.get("resources", [])
        if ranked_finished and st.get("coaches"):
            slot = hc._default_slot(st)
            res_list.extend(hc.coach_me_resources(st, slot))
            res_list.append({"uri": "/ladder/my/info", "body": _coach_ladder_statistics(st)})
        out["resources"] = res_list
    _quest_state(st)  # credit GP for any weekly quest this match finished
    _save(st)
    return hc._json(out)


def _resolve_request_user(handler, query):
    _init_user_db()
    client_ip = handler.client_address[0]
    if client_ip in ("127.0.0.1", "::1"):
        fwd = (handler.headers.get("X-Real-IP") or handler.headers.get("X-Forwarded-For") or "").split(",")[0].strip()
        if fwd:
            client_ip = fwd
    _CURRENT_USER.ip = client_ip
    token = (handler.headers.get("X-FO3-Token") or "").strip()
    if not token and query:
        qs = dict(urllib.parse.parse_qsl(query))
        token = (qs.get("token") or qs.get("key") or "").strip()

    uname = None
    if token and token in _ACTIVE_TOKENS:
        uname = _ACTIVE_TOKENS[token]
    else:
        with closing(_user_db()) as db:
            row = None
            if token:
                row = db.execute("SELECT username FROM users WHERE token = ?", (token,)).fetchone()
                if not row and token.startswith("FO3-"):
                    parts = token.split("-")
                    if len(parts) >= 2:
                        row = db.execute("SELECT username FROM users WHERE username = ?", (parts[1],)).fetchone()
            if row:
                uname = row["username"]
            elif client_ip in _ACTIVE_IPS:
                # The IP fallback must come AFTER the token checks: one
                # household IP serves every account on this server, and
                # the old order let the IP's current owner hijack a
                # request that carried another account's valid token
                # (worst right after a restart, when _ACTIVE_TOKENS is
                # cold) -- the "new account shows the old coach" bug.
                uname = _ACTIVE_IPS[client_ip]
            else:
                # Durable form of the IP fallback: the last account that
                # logged in from this IP (users.last_ip is stamped at
                # login/sign). _ACTIVE_IPS is empty after every restart,
                # and falling through to admin here made the first
                # tokenless requests after a restart act as admin --
                # e.g. cup resume answering "not exist cupid" for the
                # real owner's cups (hit in production 2026-11-12).
                row_ip = db.execute(
                    "SELECT username FROM users WHERE last_ip = ? ORDER BY id DESC LIMIT 1",
                    (client_ip,)).fetchone()
                if row_ip:
                    uname = row_ip["username"]
                elif _get_last_active_user():
                    uname = _get_last_active_user()
                else:
                    row_admin = db.execute("SELECT username FROM users WHERE username = 'admin'").fetchone()
                    uname = row_admin["username"] if row_admin else "admin"

            if uname:
                if token:
                    _ACTIVE_TOKENS[token] = uname
                _ACTIVE_IPS[client_ip] = uname
    if token and uname:
        _note_active_user(uname)
    if uname:
        _LAST_SEEN[uname] = time.time()
    _CURRENT_USER.username = uname


def _respond(self):
    length = int(self.headers.get("Content-Length") or 0)
    body = self.rfile.read(length) if length else b""
    if body[:2] == b"\x1f\x8b":
        body = gzip.decompress(body)
    self._log(body)
    path = self.path.split("?")[0]
    query = self.path.split("?", 1)[1] if "?" in self.path else ""
    _resolve_request_user(self, query)

    fallback = hc.ep_telemetry if self.command == "POST" else hc.ep_empty
    if path.startswith("/" + hc.CDN_PREFIX):
        hit = hc.cdn_file(path[len(hc.CDN_PREFIX) + 1:])
        if hit is None:
            self.send_error(404)
            return
        ctype, data = hit
    elif path in ("/web/register", "/web/login", "/register") and self.command == "GET":
        ctype, data = ep_web_register_page(query, body)
    else:
        try:
            with hc.STATE_LOCK:
                if path in hc.browser_links.URLS:
                    if self.client_address[0] not in ("127.0.0.1", "::1", DEFAULT_SERVER_IP):
                        ctype, data = hc._json({"result": False})
                    else:
                        ctype, data = hc._json({"result": hc.browser_links.open_link(path)})
                else:
                    handler_fn = hc.ENDPOINTS.get(path) or hc.route(path)
                    if handler_fn is not None:
                        ctype, data = handler_fn(query, body)
                    else:
                        ctype, data = hc.from_file(path) or fallback(query, body)
        except Exception:
            import traceback
            self._log(traceback.format_exc().encode())
            ctype, data = hc._json({"result": False})
    self.send_response(200)
    self.send_header("Content-Type", ctype)
    self.send_header("Content-Length", str(len(data)))
    self.end_headers()
    self.wfile.write(data)


# =========================================================================== #
# 2ND PASSWORD (MẬT KHẨU CẤP 2)                                              #
# =========================================================================== #

def ep_2ndpass_info(query, body):
    st = _state()
    slot = hc._default_slot(st) if st.get("coaches") else 0
    c_raw = (st.get("coaches") or [{}])[slot] if st.get("coaches") else {}
    has_pass = bool(st.get("second_pass") or c_raw.get("second_pass"))
    return hc._json({"result": True, "use": True, "securitySetting": has_pass})


def ep_2ndpass_check(query, body):
    try:
        req = json.loads(body.decode("utf-8") if body else "{}")
    except Exception:
        req = {}
    cand = str(req.get("pass", "")).strip()
    st = _state()
    slot = hc._default_slot(st) if st.get("coaches") else 0
    c_raw = (st.get("coaches") or [{}])[slot] if st.get("coaches") else {}
    real = str(st.get("second_pass") or c_raw.get("second_pass") or "").strip()
    if not real or cand == real:
        st["_2ndpass_fail"] = 0
        _save(st)
        return hc._json({"result": True, "check": True})
    cnt = int(st.setdefault("_2ndpass_fail", 0)) + 1
    st["_2ndpass_fail"] = cnt
    _save(st)
    return hc._json({"result": True, "check": False, "count": cnt, "limit": 5})


def ep_2ndpass_set(query, body):
    try:
        req = json.loads(body.decode("utf-8") if body else "{}")
    except Exception:
        req = {}
    cand = str(req.get("pass", "")).strip()
    st = _state()
    st["second_pass"] = cand
    slot = hc._default_slot(st) if st.get("coaches") else 0
    if st.get("coaches") and slot < len(st["coaches"]):
        st["coaches"][slot]["second_pass"] = cand
    st["_2ndpass_fail"] = 0
    _save(st)
    return hc._json({"result": True, "check": True})


def ep_2ndpass_reset(query, body):
    st = _state()
    st.pop("second_pass", None)
    for c in st.get("coaches") or []:
        c.pop("second_pass", None)
    st["_2ndpass_fail"] = 0
    _save(st)
    return hc._json({"result": True})


def _wrapped_ep_account(query, body):
    res_bytes = _orig_ep_account(query, body)
    try:
        acc = json.loads(res_bytes.decode("utf-8"))
        st = _state()
        slot = hc._default_slot(st) if st.get("coaches") else 0
        c_raw = (st.get("coaches") or [{}])[slot] if st.get("coaches") else {}
        has_pass = bool(st.get("second_pass") or c_raw.get("second_pass"))
        acc["status"] = {"status": "permitted" if has_pass else "notset"}
        return hc._json(acc)
    except Exception:
        return res_bytes


# ---------------------------------------------------------------------------
# Team (Clan) system -- the Klub screens (ClubProxy / ClubController).
# Real accounts only: clubs live in the shared users DB (clubs,
# club_members, club_requests, club_posts), never in one coach's state, so
# every member sees the same club. Routes and body shapes mirror the
# upstream server's proven club implementation; its members were virtual
# managers, ours are the server's real coaches. Grades: master 10,
# submaster 8, manager 6, member 1. Every write reply carries /club/me +
# /coach/me resources -- that fires the client's ON_CLUB_INFO_CHANGED.
# Activity points are not accrued yet (no fake numbers): clubs sit at
# level 1 with zeroed actpoints until a points hook is added.
# ---------------------------------------------------------------------------
CLUB_FEE, CLUB_EXPAND_FEE = 10000, 5000  # EP
CLUB_LIMIT, CLUB_EXPAND, CLUB_LIMIT_MAX = 30, 10, 60
CLUB_LEVELS = (0, 1500, 5000, 11000, 20000, 32000, 48000, 68000, 92000, 120000)
CLUB_REWARDS = ((300, "MATCH_PLUS_COACHEXP", 0.05), (800, "MATCH_PLUS_GAMEMONEY", 0.05),
                (1500, "MATCH_PLUS_PLAYEREXP_ALL", 0.05), (2500, "MATCH_PLUS_GAMEMONEY", 0.1))
CLUB_COLORS = (1203168, 13382451, 3381555, 16750848, 10027161, 39372, 16737843, 6697932, 13421619, 3355647)
CLUB_DISSOLUTION_MIN = 60
CLUB_RESET_MIN = 17 * 60  # the club day starts 00:00 (UTC+7) = 17:00 UTC
_EMPTY_PNG = (b"\x89PNG\r\n\x1a\n\x00\x00\x00\rIHDR\x00\x00\x00\x01\x00\x00\x00\x01"
              b"\x08\x06\x00\x00\x00\x1f\x15\xc4\x89\x00\x00\x00\nIDATx\x9cc\x00\x01"
              b"\x00\x00\x05\x00\x01\r\n-\xb4\x00\x00\x00\x00IEND\xaeB`\x82")


def _init_club_db():
    _init_user_db()
    with closing(_user_db()) as db:
        db.executescript("""
        CREATE TABLE IF NOT EXISTS clubs (
            clubid TEXT PRIMARY KEY, name TEXT NOT NULL, description TEXT DEFAULT '',
            desc_at REAL DEFAULT 0, news TEXT DEFAULT '', news_by TEXT DEFAULT '',
            news_at REAL DEFAULT 0, master_cid TEXT NOT NULL, created_at REAL NOT NULL,
            allowjoin INTEGER DEFAULT 1, skipjudgement INTEGER DEFAULT 0,
            color INTEGER DEFAULT 0, member_limit INTEGER DEFAULT 30, deleteat REAL);
        CREATE TABLE IF NOT EXISTS club_members (
            clubid TEXT NOT NULL, coach_id TEXT NOT NULL, grade INTEGER DEFAULT 1,
            joined_at REAL NOT NULL, PRIMARY KEY (clubid, coach_id));
        CREATE TABLE IF NOT EXISTS club_requests (
            clubid TEXT NOT NULL, coach_id TEXT NOT NULL, requested_at REAL NOT NULL,
            PRIMARY KEY (clubid, coach_id));
        CREATE TABLE IF NOT EXISTS club_posts (
            clubid TEXT NOT NULL, post_id TEXT NOT NULL, coach_id TEXT DEFAULT '',
            coach_name TEXT DEFAULT '', message TEXT DEFAULT '', at REAL NOT NULL,
            PRIMARY KEY (clubid, post_id));
        """)
        db.commit()


def _club_get(db, clubid):
    """The clubs row as a dict, or None (a dissolution whose time came due
    deletes the club and everything in it, here, lazily)."""
    row = db.execute("SELECT * FROM clubs WHERE clubid = ?", (str(clubid),)).fetchone()
    if row is None:
        return None
    club = dict(row)
    if club.get("deleteat") and time.time() >= float(club["deleteat"]):
        for t in ("clubs", "club_members", "club_requests", "club_posts"):
            db.execute(f"DELETE FROM {t} WHERE clubid = ?", (str(clubid),))
        db.commit()
        return None
    return club


def _club_of_coach(cid):
    _init_club_db()
    with closing(_user_db()) as db:
        row = db.execute("SELECT clubid FROM club_members WHERE coach_id = ?",
                         (str(cid),)).fetchone()
        if not row:
            return None
        return _club_get(db, row[0])


def _club_member_rows(db, clubid):
    return db.execute("SELECT * FROM club_members WHERE clubid = ? "
                      "ORDER BY grade DESC, joined_at ASC", (str(clubid),)).fetchall()


def _club_view(club):
    """club_body's input: the club row + members resolved against the real
    accounts (name, level, elo, username for presence)."""
    with closing(_user_db()) as db:
        rows = _club_member_rows(db, club["clubid"])
    members = []
    for r in rows:
        cid = r["coach_id"]
        found = _find_coach_across_users(target_cid=cid)
        members.append({
            "id": cid, "name": (found or {}).get("name") or cid,
            "level": int(((found or {}).get("levelinfo") or {}).get("level", 1)),
            "elo": int((found or {}).get("elo") or LADDER_START_ELO),
            "username": (found or {}).get("username"),
            "grade": int(r["grade"]), "joined": float(r["joined_at"]),
        })
    created = float(club["created_at"])
    return {"_id": club["clubid"], "name": club["name"], "created": created,
            "description": club["description"] or "", "descat": float(club["desc_at"] or created),
            "news": club["news"] or "", "newsby": club["news_by"] or "",
            "newsat": float(club["news_at"] or created),
            "allowjoin": bool(club["allowjoin"]), "skipjudgement": bool(club["skipjudgement"]),
            "color": int(club["color"] or 0), "limit": int(club["member_limit"] or CLUB_LIMIT),
            "deleteat": club.get("deleteat"), "master_cid": club["master_cid"],
            "members": members}


def _club_level(total):
    level, lo = 1, CLUB_LEVELS[0]
    for i, t in enumerate(CLUB_LEVELS):
        if total >= t:
            level, lo = i + 1, t
    hi = CLUB_LEVELS[level] if level < len(CLUB_LEVELS) else CLUB_LEVELS[-1]
    return level, lo, hi


def _club_my_grade(v, cid):
    m = next((m for m in v["members"] if m["id"] == str(cid)), None)
    return int(m["grade"]) if m else 0


def _club_body(v, my_cid=None, mine=False):
    """The club resource (/club/me, /club/<id>, a row of /club/find)."""
    level, lo, hi = _club_level(0)
    master = next((m for m in v["members"] if m["id"] == v["master_cid"]),
                  v["members"][0] if v["members"] else {"name": "", "level": 1})
    end = hc._week_end_ts()
    month = int(time.strftime("%Y%m", time.gmtime(time.time() + 7 * 3600)))
    grade = -2  # the list's status: -2 none (may ask), 0 waiting, 1+ joined
    if my_cid:
        g = _club_my_grade(v, my_cid)
        if g:
            grade = g
        else:
            with closing(_user_db()) as db:
                if db.execute("SELECT 1 FROM club_requests WHERE clubid = ? AND coach_id = ?",
                              (v["_id"], str(my_cid))).fetchone():
                    grade = 0
    body = {
        "_id": v["_id"], "name": v["name"], "createdat": hc.w3c(v["created"]),
        "state": "inactive" if v.get("deleteat") else "active",
        "description": {"message": v["description"], "changedat": hc.w3c(v["descat"])},
        "news": {"message": v["news"], "coachname": v["newsby"], "changedat": hc.w3c(v["newsat"])},
        "allowjoin": v["allowjoin"], "skipjudgement": v["skipjudgement"], "color": v["color"],
        "masterid": v["master_cid"], "mastername": master["name"], "masterlevel": master["level"],
        "membercount": len(v["members"]), "limits": {"members": v["limit"], "submasters": 2},
        "levelinfo": {"level": level, "from": lo, "to": hi, "actpoints": {
            "total": 0, "weekly": 0, "today": 0, "weeklycached": 0,
            "limits": {"weekly": 15000, "today": 3000}, "resetat": CLUB_RESET_MIN,
            "weeklybegindate": hc.w3c(end - 7 * 86400), "weeklyenddate": hc.w3c(end - 1)}},
        "rewards": [{"points": p, "boost": {"type": t, "value": x}} for p, t, x in CLUB_REWARDS],
        "statistics": {"friendly": {"wins": 0, "total": 0, "forfeitwins": 0,
                                    "recent": {"month": month, "wins": 0, "forfeitwins": 0}},
                       "ranked": {"recent": {"wins": 0, "losses": 0, "total": 0, "wprating": 0,
                                             "sid": 0, "highrecord": {"wp": 0}},
                                  "highrecord": {"wp": 0, "rank": 0},
                                  "last": {"rank": 0, "wprating": 0}}},
        "member": {"grade": grade},
        # ClubFacilityProxy does NOT read the /facilities HTTP body: its
        # resource is filled from the club resource itself (full client
        # stack, telemetry 2026-11-12 15:43 UTC: ClubProxy/initialize ->
        # onChangeClubMeResource -> set clubid -> ClubFacilityProxy/
        # initialize -> fillResourceFromExternalInterface -> fetchFacilities
        # -> updateDonationAmount #1009). Without a "facilities" field on
        # the club body the extracted model stays null and EVERY resource
        # change (login, lineup save, match ready) rethrows the popup.
        "facilities": {t: _facility_entry(t) for t in CLUB_FACILITY_TYPES}}
    if v.get("deleteat"):
        body["willbedeletedat"] = hc.w3c(float(v["deleteat"]))
    if mine and grade >= 1:
        body["member"] = {
            "grade": grade, "limits": {"today": 100},
            "actpoints": {"today": 0, "weekly": 0, "total": 0, "activities": []},
            "rewards": [{"goalpoints": 30, "reward": {"name": "EP", "count": 1000}}] * 7,
            "reward": {"todaypoint": 0, "prev": {"step": 0, "goalpoints": 0},
                       "next": {"step": 1, "goalpoints": 30,
                                "reward": {"clubep": 0, "expandme": 0}}},
            "statistics": {"ranked": {"recent": {"wins": 0, "losses": 0, "sid": 0}}}}
    return body


def _club_view_of_coach(cid):
    club = _club_of_coach(cid)
    return _club_view(club) if club else None


def _club_resources(st, my_cid):
    v = _club_view_of_coach(my_cid)
    return [{"uri": "/club/me", "body": _club_body(v, my_cid, mine=True) if v else {}}] \
        + hc.coach_me_resources(st, hc._default_slot(st))


def _club_reply(st, my_cid, ok=True, reason=None, **extra):
    out = {"result": ok}
    if not ok and reason:
        out["reason"] = reason
    out.update(extra)
    if ok:
        out["resources"] = _club_resources(st, my_cid)
    return hc._json(out)


def _club_name_ok(name):
    n = len(name.encode("utf-8"))
    return 4 <= n <= 20 and name.strip() == name and not set(name) & set("<>&\"'\\/")


def ep_club_fee_creation(query, body):
    return hc._json({"fee": CLUB_FEE})


def ep_club_fee_expansion(n, query, body):
    return hc._json({"fee": CLUB_EXPAND_FEE,
                     "newLimit": min(int(n) + CLUB_EXPAND, CLUB_LIMIT_MAX)})


def ep_club_create(query, body):
    """POST /club/create {name, description}: found a club (I am master)."""
    _init_club_db()
    try:
        req = json.loads(body or b"{}")
    except Exception:
        req = {}
    name = str(req.get("name") or "")
    desc = str(req.get("description") or "")[:200]
    st = _state()
    my_cid, my_name = _my_coach_id_and_name(st)
    if _club_of_coach(my_cid):
        return hc._json({"result": False, "reason": "already have a club"})
    if not _club_name_ok(name):
        return hc._json({"result": False, "reason": "invalid name"})
    with closing(_user_db()) as db:
        taken = db.execute("SELECT 1 FROM clubs WHERE LOWER(name) = LOWER(?)",
                           (name,)).fetchone()
    if taken:
        return hc._json({"result": False, "reason": "name is already used"})
    if int(hc.my_coach(st).get("ep") or 0) < CLUB_FEE:
        return hc._json({"result": False, "reason": "not enough money"})
    hc.add_ep(st, -CLUB_FEE)
    _save(st)
    now = time.time()
    clubid = os.urandom(12).hex()
    with closing(_user_db()) as db:
        db.execute("INSERT INTO clubs (clubid, name, description, desc_at, news_by, news_at,"
                   " master_cid, created_at, allowjoin, skipjudgement, color, member_limit)"
                   " VALUES (?, ?, ?, ?, ?, ?, ?, ?, 1, 0, ?, ?)",
                   (clubid, name, desc, now, my_name, now, my_cid, now,
                    CLUB_COLORS[0], CLUB_LIMIT))
        db.execute("INSERT INTO club_members (clubid, coach_id, grade, joined_at)"
                   " VALUES (?, ?, 10, ?)", (clubid, my_cid, now))
        db.commit()
    return _club_reply(st, my_cid, clubid=clubid, name=name)


def ep_club_me(query, body):
    """GET /club/me: my club ({} when I have none). DELETE /club/me (the
    dispatcher marks it _method=DELETE): the master schedules dissolution."""
    st = _state()
    my_cid, _ = _my_coach_id_and_name(st)
    if "_method=DELETE" in (query or ""):
        club = _club_of_coach(my_cid)
        if not club or club["master_cid"] != my_cid:
            return hc._json({"result": False, "reason": "invalid request"})
        with closing(_user_db()) as db:
            db.execute("UPDATE clubs SET deleteat = ? WHERE clubid = ?",
                       (time.time() + CLUB_DISSOLUTION_MIN * 60, club["clubid"]))
            db.commit()
        return _club_reply(st, my_cid)
    v = _club_view_of_coach(my_cid)
    return hc._json(_club_body(v, my_cid, mine=True) if v else {})


def ep_club_undelete(query, body):
    """POST /club/me/delete/cancel: call the dissolution off (master)."""
    st = _state()
    my_cid, _ = _my_coach_id_and_name(st)
    club = _club_of_coach(my_cid)
    if not club or club["master_cid"] != my_cid:
        return hc._json({"result": False, "reason": "invalid request"})
    with closing(_user_db()) as db:
        db.execute("UPDATE clubs SET deleteat = NULL WHERE clubid = ?", (club["clubid"],))
        db.commit()
    return _club_reply(st, my_cid)


def ep_club_find(query, body):
    """GET /club/find?keyword&sort&order&allowjoin&skipjudgement&
    membernumfrom&membernumto: {clubs, count} over the real clubs."""
    import urllib.parse as _up
    q = _up.parse_qs(query or "")
    arg = lambda k, d="": (q.get(k) or [d])[0]
    st = _state()
    my_cid, _ = _my_coach_id_and_name(st)
    _init_club_db()
    with closing(_user_db()) as db:
        ids = [r[0] for r in db.execute("SELECT clubid FROM clubs ORDER BY created_at").fetchall()]
        clubs = [c for c in (_club_get(db, i) for i in ids) if c]
    rows = [_club_body(_club_view(c), my_cid) for c in clubs]
    kw = arg("keyword").lower()
    lo = int(arg("membernumfrom", "0") or 0)
    hi = int(arg("membernumto", "0") or 0)
    rows = [r for r in rows if kw in r["name"].lower()
            and (arg("allowjoin") != "true" or r["allowjoin"])
            and (arg("skipjudgement") != "true" or r["skipjudgement"])
            and r["membercount"] >= lo and (not hi or r["membercount"] <= hi)]
    key = {"name": lambda r: r["name"].lower(),
           "level": lambda r: r["levelinfo"]["level"],
           "weeklypoints": lambda r: r["levelinfo"]["actpoints"]["weeklycached"],
           "member": lambda r: r["membercount"]}
    rows.sort(key=key.get(arg("sort"), key["level"]), reverse=arg("order", "-1") == "-1")
    return hc._json({"clubs": rows, "count": len(rows)})


def ep_club_get(cid, query, body):
    """GET /club/<id>: a club's page (the list's detail popup)."""
    st = _state()
    my_cid, _ = _my_coach_id_and_name(st)
    _init_club_db()
    with closing(_user_db()) as db:
        club = _club_get(db, cid)
    if not club:
        return hc._json({"result": False, "reason": "deleted club"})
    v = _club_view(club)
    return hc._json(_club_body(v, my_cid, mine=(club["clubid"] == (_club_of_coach(my_cid) or {}).get("clubid"))))


def ep_club_join(cid, query, body):
    """POST /club/<id>/join: join at once when the club skips judgement,
    else leave a request for its managers. DELETE: take my request back."""
    st = _state()
    my_cid, _ = _my_coach_id_and_name(st)
    _init_club_db()
    if "_method=DELETE" in (query or ""):
        with closing(_user_db()) as db:
            db.execute("DELETE FROM club_requests WHERE clubid = ? AND coach_id = ?",
                       (str(cid), my_cid))
            db.commit()
        return hc._json({"result": True})
    if _club_of_coach(my_cid):
        return hc._json({"result": False, "reason": "already have a club"})
    with closing(_user_db()) as db:
        club = _club_get(db, cid)
        if not club or not club["allowjoin"]:
            return hc._json({"result": False, "reason": "invalid request"})
        n = db.execute("SELECT COUNT(*) FROM club_members WHERE clubid = ?",
                       (club["clubid"],)).fetchone()[0]
        if n >= int(club["member_limit"] or CLUB_LIMIT):
            return hc._json({"result": False, "reason": "invalid request"})
        if club["skipjudgement"]:
            db.execute("INSERT OR REPLACE INTO club_members (clubid, coach_id, grade, joined_at)"
                       " VALUES (?, ?, 1, ?)", (club["clubid"], my_cid, time.time()))
            db.execute("DELETE FROM club_requests WHERE clubid = ? AND coach_id = ?",
                       (club["clubid"], my_cid))
        else:
            db.execute("INSERT OR REPLACE INTO club_requests (clubid, coach_id, requested_at)"
                       " VALUES (?, ?, ?)", (club["clubid"], my_cid, time.time()))
        db.commit()
    return _club_reply(st, my_cid)


def ep_club_leave(query, body):
    """PUT /club/me/member/leave: a member walks out (the master dissolves
    the club instead of leaving it)."""
    st = _state()
    my_cid, _ = _my_coach_id_and_name(st)
    club = _club_of_coach(my_cid)
    if not club or club["master_cid"] == my_cid:
        return hc._json({"result": False, "reason": "invalid request"})
    with closing(_user_db()) as db:
        db.execute("DELETE FROM club_members WHERE clubid = ? AND coach_id = ?",
                   (club["clubid"], my_cid))
        db.commit()
    return _club_reply(st, my_cid)


def _club_member_row(m):
    div = f"DIVISION_{_division_for_elo(m['elo'])[0]}"
    online = _is_online(m.get("username"))
    seen = time.time() if online else _LAST_SEEN.get(m.get("username") or "", 0)
    return {"_id": m["id"], "coachid": m["id"], "name": m["name"], "level": m["level"],
            "division": div, "currdivision": div,
            "actpoints": {"weekly": 0, "total": 0},
            "joinedat": hc.w3c(m["joined"]),
            "connectedat": hc.w3c(seen) if seen else hc.w3c(m["joined"]),
            "donates": None, "grade": m["grade"], "online": online}


def ep_club_members(query, body):
    """GET /club/me/members: my club's member rows."""
    st = _state()
    my_cid, _ = _my_coach_id_and_name(st)
    v = _club_view_of_coach(my_cid)
    return hc._json([_club_member_row(m) for m in v["members"]] if v else [])


def ep_club_member_grade(mid, query, body):
    """POST /club/me/member/<memberid> {grade}: the master sets a grade."""
    st = _state()
    my_cid, _ = _my_coach_id_and_name(st)
    try:
        req = json.loads(body or b"{}")
    except Exception:
        req = {}
    club = _club_of_coach(my_cid)
    if not club or club["master_cid"] != my_cid or str(mid) == my_cid \
            or req.get("grade") not in (8, 6, 1):
        return hc._json({"result": False, "reason": "invalid request"})
    with closing(_user_db()) as db:
        cur = db.execute("UPDATE club_members SET grade = ? WHERE clubid = ? AND coach_id = ?",
                         (req["grade"], club["clubid"], str(mid)))
        db.commit()
        if not cur.rowcount:
            return hc._json({"result": False, "reason": "invalid request"})
    return _club_reply(st, my_cid)


def ep_club_exclude(mid, query, body):
    """PUT /club/me/member/exclude/<memberid>: the master kicks a member."""
    st = _state()
    my_cid, _ = _my_coach_id_and_name(st)
    club = _club_of_coach(my_cid)
    if not club or club["master_cid"] != my_cid or str(mid) == my_cid:
        return hc._json({"result": False, "reason": "invalid request"})
    with closing(_user_db()) as db:
        cur = db.execute("DELETE FROM club_members WHERE clubid = ? AND coach_id = ?",
                         (club["clubid"], str(mid)))
        db.commit()
        if not cur.rowcount:
            return hc._json({"result": False, "reason": "invalid request"})
    return _club_reply(st, my_cid)


def ep_club_requests(query, body):
    """GET /club/me/join/requests: coaches asking to join my club."""
    st = _state()
    my_cid, _ = _my_coach_id_and_name(st)
    v = _club_view_of_coach(my_cid)
    if not v or _club_my_grade(v, my_cid) < 6:
        return hc._json([])
    rank = {e["coachid"]: e for e in _coach_rank_entries()}
    with closing(_user_db()) as db:
        rows = db.execute("SELECT * FROM club_requests WHERE clubid = ? ORDER BY requested_at",
                          (v["_id"],)).fetchall()
    out = []
    for r in rows:
        e = rank.get(r["coach_id"]) or {}
        found = _find_coach_across_users(target_cid=r["coach_id"]) or {}
        elo = int(found.get("elo") or LADDER_START_ELO)
        out.append({"id": r["coach_id"], "coachid": r["coach_id"],
                    "name": found.get("name") or r["coach_id"],
                    "level": int((found.get("levelinfo") or {}).get("level", 1)),
                    "division": f"DIVISION_{_division_for_elo(elo)[0]}",
                    "ovr": e.get("ovr") or {"FW": 0, "MF": 0, "DF": 0},
                    "requestedat": hc.w3c(float(r["requested_at"])),
                    "teamid": int(found.get("teamid") or 1)})
    return hc._json(out)


def ep_club_request(rid, query, body):
    """PUT /club/me/join/<id> takes the coach in; DELETE turns him down.
    Managers (grade >= 6) decide."""
    st = _state()
    my_cid, _ = _my_coach_id_and_name(st)
    club = _club_of_coach(my_cid)
    if not club:
        return hc._json({"result": False, "reason": "invalid request"})
    v = _club_view(club)
    if _club_my_grade(v, my_cid) < 6:
        return hc._json({"result": False, "reason": "invalid request"})
    with closing(_user_db()) as db:
        row = db.execute("SELECT 1 FROM club_requests WHERE clubid = ? AND coach_id = ?",
                         (club["clubid"], str(rid))).fetchone()
        if not row:
            return hc._json({"result": False, "reason": "invalid request"})
        if "_method=DELETE" in (query or ""):
            db.execute("DELETE FROM club_requests WHERE clubid = ? AND coach_id = ?",
                       (club["clubid"], str(rid)))
            db.commit()
            return _club_reply(st, my_cid)
        n = db.execute("SELECT COUNT(*) FROM club_members WHERE clubid = ?",
                       (club["clubid"],)).fetchone()[0]
        if n >= int(club["member_limit"] or CLUB_LIMIT):
            return hc._json({"result": False, "reason": "club is full"})
        if _club_of_coach(str(rid)):
            db.execute("DELETE FROM club_requests WHERE clubid = ? AND coach_id = ?",
                       (club["clubid"], str(rid)))
            db.commit()
            return hc._json({"result": False, "reason": "already have a club"})
        db.execute("INSERT INTO club_members (clubid, coach_id, grade, joined_at)"
                   " VALUES (?, ?, 1, ?)", (club["clubid"], str(rid), time.time()))
        db.execute("DELETE FROM club_requests WHERE clubid = ? AND coach_id = ?",
                   (club["clubid"], str(rid)))
        db.commit()
    return _club_reply(st, my_cid)


def ep_club_settings(what, value, query, body):
    """POST /club/me/settings/description {description} | news {message} |
    allowjoin/<bool> | skipjudgement/<bool>; PUT .../color {color}."""
    st = _state()
    my_cid, my_name = _my_coach_id_and_name(st)
    try:
        req = json.loads(body or b"{}")
    except Exception:
        req = {}
    club = _club_of_coach(my_cid)
    if not club:
        return hc._json({"result": False, "reason": "invalid request"})
    grade = _club_my_grade(_club_view(club), my_cid)
    now = time.time()
    with closing(_user_db()) as db:
        if what == "description" and grade == 10:
            db.execute("UPDATE clubs SET description = ?, desc_at = ? WHERE clubid = ?",
                       (str(req.get("description") or "")[:200], now, club["clubid"]))
        elif what == "news" and grade >= 6:
            db.execute("UPDATE clubs SET news = ?, news_by = ?, news_at = ? WHERE clubid = ?",
                       (str(req.get("message") or "")[:200], my_name, now, club["clubid"]))
        elif what == "allowjoin" and grade == 10:
            db.execute("UPDATE clubs SET allowjoin = ? WHERE clubid = ?",
                       (1 if value == "true" else 0, club["clubid"]))
        elif what == "skipjudgement" and grade == 10:
            db.execute("UPDATE clubs SET skipjudgement = ? WHERE clubid = ?",
                       (1 if value == "true" else 0, club["clubid"]))
        elif what == "color" and grade == 10 and isinstance(req.get("color"), int):
            db.execute("UPDATE clubs SET color = ? WHERE clubid = ?",
                       (req["color"], club["clubid"]))
        else:
            return hc._json({"result": False, "reason": "invalid request"})
        db.commit()
    return _club_reply(st, my_cid)


def ep_club_expand(n, query, body):
    """POST /club/me/expand/<n>: CLUB_EXPAND more member slots (master)."""
    st = _state()
    my_cid, _ = _my_coach_id_and_name(st)
    club = _club_of_coach(my_cid)
    if not club or club["master_cid"] != my_cid \
            or int(club["member_limit"] or CLUB_LIMIT) >= CLUB_LIMIT_MAX:
        return hc._json({"result": False, "reason": "invalid request"})
    if int(hc.my_coach(st).get("ep") or 0) < CLUB_EXPAND_FEE:
        return hc._json({"result": False, "reason": "not enough money"})
    hc.add_ep(st, -CLUB_EXPAND_FEE)
    _save(st)
    with closing(_user_db()) as db:
        db.execute("UPDATE clubs SET member_limit = MIN(? + ?, ?) WHERE clubid = ?",
                   (int(club["member_limit"] or CLUB_LIMIT), CLUB_EXPAND,
                    CLUB_LIMIT_MAX, club["clubid"]))
        db.commit()
    return _club_reply(st, my_cid)


def ep_club_posts(query, body):
    """GET /club/me/posts: my club's board, newest first."""
    st = _state()
    my_cid, _ = _my_coach_id_and_name(st)
    club = _club_of_coach(my_cid)
    if not club:
        return hc._json([])
    with closing(_user_db()) as db:
        rows = db.execute("SELECT * FROM club_posts WHERE clubid = ? ORDER BY at DESC LIMIT 50",
                          (club["clubid"],)).fetchall()
    return hc._json([{"_id": r["post_id"], "name": r["coach_name"],
                      "message": r["message"], "createdat": hc.w3c(float(r["at"]))}
                     for r in rows])


CLUB_FACILITY_TYPES = ("bank", "clinic", "clubAgency", "clubDormitory",
                       "staffDormitory", "fanSupporters", "playerTrainer",
                       "clubTrainer")


def _facility_entry(t):
    return {"type": t, "id": t, "level": 0, "grade": 0, "donation": 0,
            "donationamount": 0, "amount": 0,
            "todaydonation": 0, "totaldonation": 0,
            "todayDonation": 0, "totalDonation": 0}


def _facilities_map():
    """The /facilities HTTP body: the same per-type map embedded in the
    club resource (see _club_body -- the proxy's model is filled from
    the CLUB resource via fillResourceFromExternalInterface, so this
    HTTP body is at most a refresh source; keep the two identical). Map
    and list shapes served here both left the proxy model null while
    the club body lacked the field. No facility has been built or
    donated to yet, so every entry sits at level 0, zero donations."""
    return {t: _facility_entry(t) for t in CLUB_FACILITY_TYPES}


def ep_facilities(query, body):
    """GET /facilities: ClubFacilityProxy fetches this on every resource
    change once the coach HAS a club -- and ClubFacilityProxy
    .updateDonationAmount() dereferences the body. The route did not
    exist, the fallback answered {} and the proxy threw AS3 #1009,
    which surfaced as a UI error on club creation, on entering the
    Team screens and on EVERY match loading screen (telemetry
    2026-11-12). Clubless coaches never fetch it; keep {} for them."""
    st = _state()
    my_cid, _ = _my_coach_id_and_name(st)
    if not _club_of_coach(my_cid):
        return hc._json({})
    return hc._json(_facilities_map())


def ep_club_facilities(cid, query, body):
    """GET /club/<clubid>/facilities: the same map for a viewed club."""
    with closing(_user_db()) as db:
        club = _club_get(db, cid)
    if not club:
        return hc._json({})
    return hc._json(_facilities_map())


def ep_club_post(pid, query, body):
    """POST /club/me/post/ {msg}: post on my club's board.
    DELETE /club/me/post/<id>: remove a post (mine, or any as master)."""
    st = _state()
    my_cid, my_name = _my_coach_id_and_name(st)
    club = _club_of_coach(my_cid)
    if not club:
        return hc._json({"result": False, "reason": "invalid request"})
    with closing(_user_db()) as db:
        if "_method=DELETE" in (query or ""):
            row = db.execute("SELECT coach_id FROM club_posts WHERE clubid = ? AND post_id = ?",
                             (club["clubid"], str(pid))).fetchone()
            if not row or (row[0] != my_cid and club["master_cid"] != my_cid):
                return hc._json({"result": False, "reason": "invalid request"})
            db.execute("DELETE FROM club_posts WHERE clubid = ? AND post_id = ?",
                       (club["clubid"], str(pid)))
            db.commit()
            return _club_reply(st, my_cid)
        try:
            req = json.loads(body or b"{}")
        except Exception:
            req = {}
        text = str(req.get("msg") or req.get("message") or "")[:200]
        if not text:
            return hc._json({"result": False, "reason": "invalid request"})
        db.execute("INSERT INTO club_posts (clubid, post_id, coach_id, coach_name, message, at)"
                   " VALUES (?, ?, ?, ?, ?, ?)",
                   (club["clubid"], os.urandom(12).hex(), my_cid, my_name, text, time.time()))
        db.execute("DELETE FROM club_posts WHERE clubid = ? AND post_id NOT IN"
                   " (SELECT post_id FROM club_posts WHERE clubid = ? ORDER BY at DESC LIMIT 30)",
                   (club["clubid"], club["clubid"]))
        db.commit()
    return _club_reply(st, my_cid)


def ep_club_message(query, body):
    """POST /club/me/message {message}: a line in the club chat tab; pushed
    to every member's messenger connection (mine included, as the echo)."""
    st = _state()
    my_cid, my_name = _my_coach_id_and_name(st)
    try:
        req = json.loads(body or b"{}")
    except Exception:
        req = {}
    text = str(req.get("message") or "")[:300]
    club = _club_of_coach(my_cid)
    if not club or not text:
        # No club (or empty line): upstream answers result True and simply
        # delivers nothing -- a False here made the client pop an
        # "invalid request" error in the team chat tab (user 2026-11-12).
        return hc._json({"result": True})
    v = _club_view(club)
    payload = {"type": "chat", "clubid": club["clubid"], "coachid": my_cid,
               "coachname": my_name, "message": text}
    for m in v["members"]:
        notify_messenger_push("/club/me/message", payload, coachid=m["id"])
    return hc._json({"result": True})


def ep_contact_club(query, body):
    """GET /contact/club {members}: the chat window's club member list."""
    st = _state()
    my_cid, _ = _my_coach_id_and_name(st)
    v = _club_view_of_coach(my_cid)
    if not v:
        return hc._json({"members": []})
    return hc._json({"members": [{"_id": m["id"], "coachname": m["name"]}
                                 for m in v["members"] if m["id"] != my_cid]})


def ep_club_record(what, query, body):
    """GET /club/record/members | opponent | recent | leaderboard. No club
    matches exist yet, so match statistics are zero; the rows are real."""
    st = _state()
    my_cid, _ = _my_coach_id_and_name(st)
    month = int(time.strftime("%Y%m", time.gmtime(time.time() + 7 * 3600)))
    if what == "members":
        v = _club_view_of_coach(my_cid)
        rank = {e["coachid"]: e for e in _coach_rank_entries()}
        rows = [{"coachid": m["id"], "name": m["name"], "level": m["level"],
                 "grade": m["grade"], "ranking": i + 1,
                 "ovr": (rank.get(m["id"]) or {}).get("ovr") or {"FW": 0, "MF": 0, "DF": 0},
                 "statistics": {"friendly": {"recent": {"wins": 0, "forfeitwins": 0}}}}
                for i, m in enumerate(v["members"] if v else [])]
        return hc._json({"month": month, "members": rows})
    if what == "leaderboard":
        _init_club_db()
        with closing(_user_db()) as db:
            ids = [r[0] for r in db.execute("SELECT clubid FROM clubs").fetchall()]
            clubs = [c for c in (_club_get(db, i) for i in ids) if c]
        views = sorted((_club_view(c) for c in clubs),
                       key=lambda v: (-len(v["members"]), v["name"].lower()))
        return hc._json({"month": month, "leaderboardlist": [
            {"clubid": v["_id"], "clubname": v["name"], "level": _club_level(0)[0],
             "rank": i + 1, "wins": 0, "forfeitwins": 0, "totalwins": 0,
             "totalforfeitwins": 0, "total": 0, "wp": 0} for i, v in enumerate(views)]})
    return hc._json({"month": month, "opponentrecords": [], "recentmatches": []})


def ep_club_emblem(cid, size, query, body):
    """GET /club/<id>/emblem/<size>.png: the club crest (generated set,
    picked by club id; the client fetches this at login for club 0 too)."""
    try:
        idx = int(str(cid), 16) % len(CLUB_COLORS)
    except Exception:
        idx = abs(hash(str(cid))) % len(CLUB_COLORS)
    path = os.path.join(os.path.dirname(os.path.abspath(__file__)),
                        "assets", "club_emblems", f"{idx}_{size}.png")
    try:
        with open(path, "rb") as f:
            return ("image/png", f.read())
    except Exception:
        return ("image/png", _EMPTY_PNG)


def _push_cup_record():
    """Push the fresh /cup/record body to my client. The cup hub reads
    CupProxy.record, which the client only refetches on a zone change:
    after leaving or creating a cup and popping back to the hub inside
    the same zone, the hub kept showing the pre-leave state until a
    relaunch (user report 2026-11-12). The pushed body replaces the
    cached resource in place, like the /contact/list pushes.

    The pushed body must be the record itself (the {recordsbymetaid}
    entry inside ep_cup_record's resources envelope): the first version
    pushed the whole envelope -- 73 KB with every cup resource inlined,
    over the messenger push cap, so it was dropped on the floor and the
    hub stayed stale (messenger journal 2026-11-12 15:17 UTC)."""
    try:
        my_cid, _ = _my_coach_id_and_name(_state())
        _ct, _raw = hc.ep_cup_record({}, b"")
        resp = json.loads(_raw)
        rec = next((r.get("body") for r in resp.get("resources") or []
                    if r.get("resource") == "/cup/record"), None)
        if rec is not None:
            notify_messenger_push("/cup/record", rec, coachid=my_cid)
    except Exception:
        pass


def _cup_leave_push(cupid, query, body):
    r = hc.__dict__["_orig_ep_cup_leave"](cupid, query, body)
    _push_cup_record()
    return r


def _cup_new_push(query, body):
    r = hc.__dict__["_orig_ep_cup_new"](query, body)
    _push_cup_record()
    return r


def install(module):
    global hc, _orig_coach_me, _orig_match_end, _orig_ep_account
    if getattr(module, '_fo3_ext_installed', False):
        return
    module._fo3_ext_installed = True
    hc = module
    _init_user_db()
    _patch_json_files_on_disk()

    hc.REDIR_HOST = DEFAULT_SERVER_IP
    hc.NOTI_HOST = DEFAULT_SERVER_IP
    hc.USER_DB_FILE = _user_db_path()
    hc.list_db_users = list_db_users
    hc._state = _state
    hc._save = _save
    hc.ep_my_ip = ep_my_ip
    hc.ep_garena_sign = ep_garena_sign

    _orig_ep_account = hc.ep_account
    hc.ep_account = ep_account_ext
    hc.ENDPOINTS["/account"] = ep_account_ext
    hc.ENDPOINTS["/static/info/request"] = ep_static_info_request
    hc.ENDPOINTS.update({
        "/club/fee/creation": ep_club_fee_creation,
        "/club/create": ep_club_create,
        "/club/me": ep_club_me,
        "/club/find": ep_club_find,
        "/club/me/members": ep_club_members,
        "/club/me/member/leave": ep_club_leave,
        "/club/me/join/requests": ep_club_requests,
        "/club/me/posts": ep_club_posts,
        "/facilities": ep_facilities,
        "/club/me/message": ep_club_message,
        "/club/me/delete/cancel": ep_club_undelete,
        "/contact/club": ep_contact_club,
    })
    hc.ROUTES[:0] = [
        (re.compile(r"/club/fee/expansion/(\d+)"), ep_club_fee_expansion),
        (re.compile(r"/club/me/member/exclude/(\w+)"), ep_club_exclude),
        (re.compile(r"/club/me/member/(\w+)"), ep_club_member_grade),
        (re.compile(r"/club/me/join/(\w+)"), ep_club_request),
        (re.compile(r"/club/me/expand/(\d+)"), ep_club_expand),
        (re.compile(r"/club/me/settings/(description|news|allowjoin|skipjudgement|color)(?:/(\w+))?"), ep_club_settings),
        (re.compile(r"/club/me/post/?(\w*)"), ep_club_post),
        (re.compile(r"/club/(\w+)/facilities"), ep_club_facilities),
        (re.compile(r"/club/record/(\w+)"), ep_club_record),
        (re.compile(r"/club/(\w+)/emblem/(small|medium|large)\.png"), ep_club_emblem),
        (re.compile(r"/club/(\w+)/join"), ep_club_join),
        (re.compile(r"/club/(\w+)"), ep_club_get),
    ]

    _orig_coach_me = hc.coach_me
    hc.coach_me = _wrapped_coach_me

    hc._side_ready = _side_ready
    hc.ep_match_ready = ep_match_ready
    hc._player_ready = _player_ready
    hc.ep_match_player_ready = ep_match_player_ready
    hc.ep_match_end = ep_match_end

    # Replace match routes in hc.ROUTES so they call our Ranked-aware handlers
    for idx, (pat, fn) in enumerate(hc.ROUTES):
        if pat.pattern == r"/match/([0-9a-f]+)/player/ready":
            hc.ROUTES[idx] = (pat, ep_match_player_ready)
        elif pat.pattern == r"/match/([0-9a-f]+)/side/(\w+)/ready":
            hc.ROUTES[idx] = (pat, ep_match_ready)
        elif pat.pattern == r"/match/([0-9a-f]+)/end":
            hc.ROUTES[idx] = (pat, ep_match_end)
        elif pat.pattern == r"/cup/([0-9a-f]{24})/leave":
            hc.ROUTES[idx] = (pat, _cup_leave_push)

    # Cup create/leave also refresh the client's cached /cup/record via a
    # messenger push (see _push_cup_record).
    hc._orig_ep_cup_leave = hc.ep_cup_leave
    hc._orig_ep_cup_new = hc.ep_cup_new
    hc.ENDPOINTS["/cup/new"] = _cup_new_push

    hc.ROUTES[:0] = [
        (re.compile(r"/static/db/SORTLEAGUE/(\d+)"), ep_sortleague_by_nation),
        (re.compile(r"/static/db/SORTTEAM/(\d+)"), ep_sortteam_by_league),
        (re.compile(r"/badge/(\d+)/use"), ep_badge_use),
        (re.compile(r"/chat/channel/(\d+)"), ep_chat_channel_join),
        (re.compile(r"/chat/coach/(\w+)/channel/detail"), ep_chat_coach_detail),
        (re.compile(r"/contact/requested/by/(\w+)/reply"), ep_contact_reply),
        (re.compile(r"/contact/requesting/to/(\w+)"), ep_contact_cancel_requesting),
        (re.compile(r"/contact/list/(\w+)/group"), ep_contact_friend_action),
        (re.compile(r"/contact/list/(\w+)"), ep_contact_friend_action),
        (re.compile(r"/contact/group/(.+)"), ep_contact_group_delete),
        (re.compile(r"/room/(\d+)/invite/(\w+)"), ep_room_invite),
        (re.compile(r"/room/(\d+)/matchmaking"), ep_room_matchmaking),
        (re.compile(r"/room/(\d+)/side/(\w+)/ready"), ep_room_side_ready),
        (re.compile(r"/room/(\d+)/leave"), ep_room_leave),
        (re.compile(r"/room/(\d+)/([\w/]+)"), ep_room_update),
        (re.compile(r"/room/(\d+)"), ep_room_get),
        (re.compile(r"/exhibition/room/(\d+)/team"), ep_exhibition_team_change),
        (re.compile(r"/exhibition/info/authentic/team/(\d+)"), ep_exhibition_authentic_team),
    ]

    hc.ENDPOINTS.update({
        "/account": ep_account_ext,
        "/account/beginnersguide": ep_beginnersguide,
        "/game/my/ipaddress": ep_my_ip,
        "/publisher/garena/sign": ep_garena_sign,
        # Web Registration & Launcher Auth
        "/web/register": ep_web_register_page,
        "/web/login": ep_web_register_page,
        "/register": ep_web_register_page,
        "/api/register": ep_api_register,
        "/launcher/login": ep_launcher_login,
        "/launcher/status": ep_launcher_status,
        # Global Chat (Chat thế giới) & Friends (Kết bạn)
        "/chat/channel": ep_chat_channel_auto,
        "/chat/my/channel/message": ep_chat_send_message,
        "/chat/my/channel/messages/latest": ep_chat_channel_latest,
        "/chat/my/messages/latest": ep_chat_whisper_latest,
        "/chat/blacklist": lambda q, b: hc._json({"result": True, "list": []}),
        "/contact/list": ep_contact_list,
        "/contact/requesting": ep_contact_requesting,
        "/contact/newfriends": ep_contact_newfriends,
        "/contact/group": ep_contact_group,
        "/presence": ep_presence,
        # Ranked Mode (Đá xếp hạng)
        "/ladder/division/info": ep_ladder_division_info,
        "/club/ranked/division/info": ep_club_ranked_division_info,
        "/ladder/reward/info": ep_ladder_reward_info,
        "/ladder/my/info": ep_ladder_my_info,
        "/ladder/reward": ep_ladder_reward,
        "/ladder/leaderboard": ep_ladder_leaderboard,
        "/ladder/season/info": ep_ladder_season_info,
        "/club/ranked/season/info": ep_club_ranked_season_info,
        "/ladder/friends/info": ep_ladder_friends_info,
        # Inbox & 1-on-1 Whisper Chat
        "/inbox/my/messages": lambda q, b: hc._json([]),
        # Rooms & Friendly PVP / Exhibition Match
        "/room": ep_room_create,
        "/exhibition/room": ep_exhibition_room_create,
        "/exhibition/info/points": ep_exhibition_points,
        "/static/db/EXHIBITIONMATCH": lambda q, b: hc._json(EXHIBITION_STATIC_DB),
        # Static DB Badges, Achievements & Team Value Ranking
        "/static/db/BADGES": ep_static_badges,
        "/static/db/BADGE": ep_static_badges,
        "/static/info/BADGES": ep_static_badges,
        "/static/info/badges": ep_static_badges,
        "/static/info/badge": ep_static_badges,
        "/static/info/ACHIEVEMENT": ep_static_achievement,
        "/static/info/achievement": ep_static_achievement,
        "/static/db/ACHIEVEMENT": ep_static_achievement,
        "/static/db/achievement": ep_static_achievement,
        "/achievement/me": ep_achievement_me,
        "/achievement/questrewards": ep_achievement_questrewards,
        "/coach/me/hiderank": ep_coach_hiderank,
        "/rank/teamvalue/friend": ep_rank_teamvalue_friend,
        "/rank/teamvalue/club": ep_rank_teamvalue,
        "/coach/me/teamvalue/history": ep_coach_teamvalue_history,
        # Secondary Password (Mật khẩu cấp 2)
        "/info/server/2ndpass": ep_2ndpass_info,
        "/2ndpass/check": ep_2ndpass_check,
        "/2ndpass/verify": ep_2ndpass_check,
        "/2ndpass": ep_2ndpass_set,
        "/2ndpass/reset": ep_2ndpass_reset,
    })

    # Add inbox & whisper regex routes
    hc.ROUTES.insert(0, (re.compile(r"/inbox/([^/]+)/messages/with/me"), lambda cid, q, b: hc._json({"result": True, "msg": (json.loads(b.decode("utf-8")) if b else {}).get("message", "")})))
    hc.ROUTES.insert(0, (re.compile(r"/inbox/my/messages/with/([^/]+)"), lambda cid, q, b: hc._json({"result": True, "list": []})))
    hc.ROUTES.insert(0, (re.compile(r"/chat/coach/([^/]+)/message"), lambda cid, q, b: hc._json({"result": True})))

    
    # Register Squad Match Chat Routes
    hc.ROUTES.insert(0, (re.compile(r"/match/([0-9a-fA-F]+)/messages/latest"), lambda mid, q, b: ep_match_messages_latest(mid, q, b)))
    hc.ROUTES.insert(0, (re.compile(r"/match/([0-9a-fA-F]+)/messages"), lambda mid, q, b: ep_match_send_message(mid, q, b)))

    # Register Achievement Claim Routes
    hc.ROUTES.insert(0, (re.compile(r"/achievement/([^/]+)/reward(?:/([^/]+))?"), lambda aid, g, q, b: ep_achievement_claim_reward(aid, g, q, b)))
    # Coach profiles viewed from the Hạng HLV ranking screen.
    hc.ROUTES.insert(0, (re.compile(r"/achievement/([^/]+)"), ep_coach_profile_achievement))
    hc.ROUTES.insert(0, (re.compile(r"/ladder/([^/]+)/info"), ep_coach_profile_ladder))
    hc.ROUTES.insert(0, (re.compile(r"/coach/([0-9a-f]{24})/summary"), ep_coach_summary))
    hc.ROUTES.insert(0, (re.compile(r"/coach/([0-9a-f]{24})"), ep_coach_by_id))
    hc.ROUTES.insert(0, (re.compile(r"/achievement/questrewards"), ep_achievement_questrewards))

    hc.H._respond = _respond
    hc.H.do_GET = _respond
    hc.H.do_POST = _respond
    hc.H.do_PUT = _respond
    hc.H.do_DELETE = _respond
