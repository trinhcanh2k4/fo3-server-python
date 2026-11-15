"""Admin page for the FO3 private server: http://127.0.0.1:8088/ (and via Nginx reverse proxy).

Started by http_config.py in its own thread (env FO3_ADMIN_PORT, default 8088, 0 = off). The
page (admin/index.html) calls the JSON API below, which reads and edits per-user state in
gamedata/fo3_users.db (or gamedata/state.json) through http_config's helpers: EP, players
(add / remove / grade / Main Team-Reserves), packs, Bag gifts, daily login rewards, scout
cooldowns, and server logs.
"""
import re, sqlite3, hashlib
import http.server
import json
import os
import threading
import time
import urllib.parse
from contextlib import closing

HERE = os.path.dirname(os.path.abspath(__file__))
PAGE = os.path.join(HERE, "admin", "index.html")
POS_NAMES = "GK SW RWB RB RCB CB LCB LB LWB RDM CDM LDM RM RCM CM LCM LM RAM CAM LAM RF CF LF RW RS ST LS LW".split()
API = {}
READ_ONLY = {"enhancement_settings", "duplicate_player_settings", "events", "summary", "players", "search", "player_seasons", "staff_search", "staff_tasks", "packs", "daily", "log", "users"}
hc = None
try:
    import http_config as _hc
    hc = _hc
except ImportError:
    pass


class AdminError(Exception):
    pass


def api(name):
    def reg(fn):
        API[name] = fn
        return fn
    return reg


def _target_user(p):
    u = str(p.get("user") or "").strip()
    return u if u else None


def _ensure_user_coach(st, uname=None):
    """Ensure the user's state has at least one coach and team slot so Admin can grant items/EP/players anytime."""
    if not st.get("coaches"):
        slot = 0
        cid = hc.coach_id(slot)
        cname = uname or "Manager"
        st["coaches"] = [{
            "_id": cid,
            "slot": slot,
            "name": cname,
            "teamid": 1,
            "ep": hc.START_EP,
            "level": 1,
            "exp": 0,
            "createdat": hc.w3c(time.time()),
            "items": [],
            "bag": [],
        }]
        st["defaultcoachid"] = cid
    slot = hc._default_slot(st)
    st.setdefault("teams", {})
    if str(slot) not in st["teams"]:
        st["teams"][str(slot)] = hc.new_manager_team(1)
    return hc.my_coach(st)


def coach(st, uname=None):
    return _ensure_user_coach(st, uname)


def team(st, uname=None):
    _ensure_user_coach(st, uname)
    return st["teams"][str(hc._default_slot(st))]


def names(spids):
    """{spid: player name} from fifadb."""
    spids = sorted({int(s) for s in spids})
    if not spids:
        return {}
    with closing(hc._db()) as c:
        return {s: n for s, n in c.execute(
            f"""SELECT p.spid, COALESCE(n.full_eng, n.full, n.short) FROM players p JOIN nameseng n ON n.playerid = p.playerid
                WHERE p.spid IN ({','.join('?' * len(spids))})""", spids)}


def player(st, pid, uname=None):
    p = next((x for x in team(st, uname)["players"] if hc.iid(x) == str(pid)), None)
    if p is None:
        raise AdminError("Player not found")
    return p


def as_int(v, lo, hi, what):
    try:
        v = int(v)
    except (TypeError, ValueError):
        raise AdminError(f"{what} must be a number")
    if not lo <= v <= hi:
        raise AdminError(f"{what} must be between {lo} and {hi}")
    return v


@api("users")
def api_users(p):
    if hasattr(hc, "list_db_users"):
        users = hc.list_db_users()
    else:
        users = []
    return {"users": users}


@api("summary")
def summary(p):
    uname = _target_user(p)
    st = hc._state(uname)
    c = coach(st, uname)
    slot = hc._default_slot(st)
    d = c.get("daily") or {"last": "", "streak": 0, "total": 0}
    rk = st.get("ranked") or {"elo": 1400, "wins": 0, "losses": 0, "draws": 0}
    return {
        "user": uname or "__default__",
        "coach": {"name": c.get("name"), "ep": c.get("ep", hc.START_EP), "teamid": c.get("teamid")},
        "ranked": rk,
        "vip_until": time.strftime("%Y-%m-%d", time.gmtime(hc.VIP_UNTIL / 1000)),
        "igr": True,
        "players": len(team(st, uname)["players"]),
        "max_players": hc.MAX_PLAYERS,
        "packs": sum(i["remains"] for i in c.get("items", [])),
        "bag": len(c.get("bag", [])),
        "daily": {**d, "today": hc.today_wib(), "granted_today": d.get("last") == hc.today_wib()},
        "scouts": hc.coach_scouters(st, slot),
        "server_time": time.strftime("%Y-%m-%d %H:%M:%S", time.gmtime(time.time() + hc.WIB)) + " WIB",
    }


@api("players")
def players(p):
    uname = _target_user(p)
    st = hc._state(uname)
    t = team(st, uname)
    pos = {str(s.get("id") or s["spid"]): s["pos"] for s in t["squad"]["positions"]}
    nm = names(x["spid"] for x in t["players"])
    out = []
    for x in t["players"]:
        i, g = hc.iid(x), hc.grade_of(x)
        out.append({
            "id": i, "spid": x["spid"], "name": nm.get(x["spid"], "?"), "season": x["spid"] // 1000000,
            "ovr": hc.card_ovr(x["spid"]) + hc.GRADE_BOOST[g], "grade": g, "price": hc.card_price(x["spid"], g),
            "role": "MAIN" if pos.get(i, 99) < hc.POS_SUB else "SUB" if i in pos else "RES",
            "team2": bool(x.get("team2")),
        })
    return {"players": out}


@api("search")
def search(p):
    q = str(p.get("q") or "").strip()
    if len(q) < 2:
        return {"rows": []}
    with closing(hc._db()) as c:
        rows = c.execute(f"""SELECT p.spid, COALESCE(n.full_eng, n.full, n.short), {hc.POSOVR}, p.preferredposition1
                             FROM players p JOIN nameseng n ON n.playerid = p.playerid
                             WHERE n.full LIKE ?1 OR n.short LIKE ?1 OR n.full_eng LIKE ?1 OR n.short_eng LIKE ?1
                             ORDER BY 3 DESC LIMIT 60""", (f"%{q}%",)).fetchall()
    return {"rows": [{"spid": s, "name": n, "season": s // 1000000, "ovr": o, "pos": POS_NAMES[pp] if 0 <= pp < 28 else "?",
                      "market": hc.on_market(s), "price": hc.card_price(s)} for s, n, o, pp in rows]}





@api("packs")
def packs(p):
    return {"packs": [{"itemid": i, "name": n, "price": hc.pack_price(i)} for i, (n, _) in hc.PACKS.items()]}


@api("ep")
def set_ep(p):
    uname = _target_user(p)
    st = hc._state(uname)
    c = coach(st, uname)
    if "set" in p:
        c["ep"] = as_int(p["set"], 0, 10 ** 13, "EP")
    else:
        hc.add_ep(st, as_int(p.get("add"), -10 ** 13, 10 ** 13, "EP"))
        c["ep"] = max(0, c["ep"])
    hc._save(st, uname)
    return {"ep": c["ep"], "user": uname or "__default__"}


@api("player_add")
def player_add(p):
    uname = _target_user(p)
    st = hc._state(uname)
    t = team(st, uname)
    spid, grade = as_int(p.get("spid"), 1, 10 ** 9, "spid"), as_int(p.get("grade", 1), 1, 10, "grade")
    if not names([spid]):
        raise AdminError("SPID not found in database")
    if len(t["players"]) >= hc.MAX_PLAYERS:
        raise AdminError(f"Club is full ({hc.MAX_PLAYERS} players)")
    x = hc.add_player(t, spid, grade)
    hc._save(st, uname)
    return {"id": hc.iid(x), "user": uname or "__default__"}


@api("player_seasons")
def player_seasons(p):
    with closing(hc._db()) as c:
        seasons = c.execute("SELECT DISTINCT CAST(spid / 1000000 AS INTEGER) FROM players "
                            "WHERE CAST(spid / 1000000 AS INTEGER) NOT IN (0) ORDER BY 1 DESC").fetchall()
    names_by_code = {}
    try:
        with open(os.path.join(HERE, "responses", "static_roster.json"), encoding="utf-8") as stream:
            roster = json.load(stream)
        names_by_code = {int(code): entry["season"] for entry in roster.get("seasons", [])
                         for code in entry.get("value", []) if str(code).isdigit()}
    except (OSError, ValueError, KeyError, TypeError):
        pass
    return {"seasons": [{"id": int(row[0]),
                         "name": names_by_code.get(int(row[0]), "Base Season" if int(row[0]) == 0 else f"Season {int(row[0])}")}
                        for row in seasons if row[0] is not None]}


@api("user_add")
def user_add(p):
    """Add a designated user account to the SQLite fo3_users database."""
    username = str(p.get("username") or "").strip().lower()
    password = str(p.get("password") or "123456").strip()
    coach_name = str(p.get("coach_name") or username).strip()
    teamid = as_int(p.get("teamid", 1), 1, 100000, "teamid")
    ep_val = as_int(p.get("ep", 100000000), 0, 10 ** 13, "EP")
    if not username or not re.fullmatch(r"[a-z0-9_]{3,24}", username):
        raise AdminError("Tên tài khoản phải từ 3-24 ký tự (chữ thường, số, dấu gạch dưới)")

    import sqlite3, hashlib
    db_path = os.path.join(hc.HERE, "gamedata", "fo3_users.db")
    os.makedirs(os.path.dirname(db_path), exist_ok=True)
    with sqlite3.connect(db_path, timeout=10) as db:
        db.row_factory = sqlite3.Row
        db.execute("""
            CREATE TABLE IF NOT EXISTS users (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                username TEXT UNIQUE NOT NULL,
                password_hash TEXT NOT NULL,
                token TEXT,
                last_ip TEXT,
                coach_name TEXT DEFAULT '',
                elo INTEGER DEFAULT 1400,
                wins INTEGER DEFAULT 0,
                losses INTEGER DEFAULT 0,
                draws INTEGER DEFAULT 0,
                state_json TEXT NOT NULL,
                created_at INTEGER NOT NULL
            )
        """)
        row = db.execute("SELECT id FROM users WHERE username = ?", (username,)).fetchone()
        if row:
            raise AdminError(f"Tài khoản '{username}' đã tồn tại trong database!")
        pw_hash = hashlib.sha256(password.encode("utf-8")).hexdigest()
        token = hashlib.sha256(f"{username}:{time.time()}".encode("utf-8")).hexdigest()[:32]

        cid = hc.coach_id(0)
        init_st = {
            "coaches": [{
                "_id": cid, "slot": 0, "name": coach_name, "teamid": teamid,
                "ep": ep_val, "level": 1, "exp": 0, "createdat": hc.w3c(time.time()),
                "items": [], "bag": []
            }],
            "defaultcoachid": cid,
            "teams": {"0": hc.new_manager_team(teamid)}
        }
        db.execute("""
            INSERT INTO users (username, password_hash, token, last_ip, coach_name, elo, wins, losses, draws, state_json, created_at)
            VALUES (?, ?, ?, '127.0.0.1', ?, 1400, 0, 0, 0, ?, ?)
        """, (username, pw_hash, token, coach_name, json.dumps(init_st), int(time.time())))
        db.commit()

    try:
        hc._save(init_st, username)
    except Exception:
        pass

    return {"result": True, "username": username, "coach_name": coach_name}


@api("staff_search")
def staff_search(p):
    """Search verified staff cards available in the bundled catalog."""
    import staff_service
    q = str(p.get("q") or "").strip().casefold()
    typ = str(p.get("type") or "").strip().upper()
    valid_types = {row[0] for row in staff_service.TYPES}
    if typ and typ not in valid_types:
        raise AdminError("Unknown coach duty")
    rows = [{"stfid": int(stfid), "name": info.get("name", ""), "position": info.get("position", ""),
             "boost_groups": info.get("boost_groups", [])}
            for stfid, info in staff_service.CATALOG.items()
            if not info.get("material") and info.get("verified", True)
            and not info.get("local_variant_of")
            and info.get("position") in valid_types
            and (not typ or info.get("position") == typ)
            and (not q or q in str(info.get("name", "")).casefold())]
    rows.sort(key=lambda row: (row["name"].casefold(), row["stfid"]))
    return {"rows": rows[:100]}


@api("staff_tasks")
def staff_tasks(p):
    """List available coach duties from the verified bundled catalog."""
    import staff_service
    valid = {info.get("position") for info in staff_service.CATALOG.values()
             if not info.get("material") and info.get("verified", True)}
    return {"tasks": [{"code": code, "label": label} for code, label, _, _ in staff_service.TYPES if code in valid]}


@api("staff_add")
def staff_add(p):
    """Legacy direct-add endpoint retained for compatibility with existing admin clients."""
    import staff_service
    stfid = as_int(p.get("stfid"), 1, 10 ** 9, "staff id")
    grade = as_int(p.get("grade", 2), 2, 10, "grade")
    if grade % 2:
        raise AdminError("Coach stars must be between 1 and 5 in whole-star steps")
    info = staff_service.CATALOG.get(str(stfid))
    valid_types = {row[0] for row in staff_service.TYPES}
    if (info is None or info.get("material") or not info.get("verified", True)
            or info.get("local_variant_of")
            or info.get("position") not in valid_types):
        raise AdminError("Coach not found in verified catalog")
    uname = _target_user(p)
    st = hc._state(uname)
    slot, t = staff_service.team_state(st)
    if len(t["staffs"]) >= t["maxstaff"]:
        raise AdminError(f"Staff list is full ({t['maxstaff']} cards)")
    selected_effects = p.get("selected_effects")
    expected_effects = len(info.get("boost_groups", [])[:min(3, grade)])
    if selected_effects is not None:
        if (not isinstance(selected_effects, list) or len(selected_effects) != expected_effects
                or any(not isinstance(value, str) or value not in info["boost_groups"][index]
                       for index, value in enumerate(selected_effects))):
            raise AdminError("Invalid selected coach boosts")
    staff = {"_id": hc.new_iid(), "stfid": stfid, "commonname": info["name"],
             "position": info["position"], "grade": grade, "exp": 0, "gradeupto": 1000,
             "life": 100, "status": "idle", "effect": staff_service.recruit_effects(info, grade, selected_effects),
             "study": {"history": {}}, "material": False, "contractfee": 0, "new": True}
    t["staffs"].append(staff)
    hc._save(st, uname)
    return {"id": staff["_id"], "stfid": stfid, "name": info["name"], "grade": grade,
            "staff_count": len(t["staffs"]), "slot": slot}


@api("staff_pack_send")
def staff_pack_send(p):
    """Put one selected coach card into the in-game Bag as a designated coach gift."""
    import staff_service
    stfid = as_int(p.get("stfid"), 1, 10 ** 9, "staff id")
    grade = as_int(p.get("grade", 2), 2, 10, "grade")
    if grade % 2:
        raise AdminError("Coach stars must be between 1 and 5 in whole-star steps")
    info = staff_service.CATALOG.get(str(stfid))
    valid_types = {row[0] for row in staff_service.TYPES}
    if (info is None or info.get("material") or not info.get("verified", True)
            or info.get("local_variant_of") or info.get("position") not in valid_types):
        raise AdminError("Coach not found in verified catalog")
    uname = _target_user(p)
    st = hc._state(uname)
    slot, team = staff_service.team_state(st)
    if len(team["staffs"]) + sum(len(entry.get("staffs") or []) for entry in hc.my_coach(st).get("bag", [])) >= team["maxstaff"]:
        raise AdminError(f"Staff list and pending coach gifts are full ({team['maxstaff']} cards)")
    staff = {"_id": hc.new_iid(), "stfid": stfid, "commonname": info["name"],
             "position": info["position"], "grade": grade, "exp": 0, "gradeupto": 1000,
             "life": 100, "status": "idle", "effect": staff_service.recruit_effects(info, grade),
             "study": {"history": {}}, "material": False, "contractfee": 0, "new": True}
    entry = hc.bag_add(st, f"Gói chỉ định HLV - {info['name']}",
                       msg=f"HLV đã chọn: {info['name']} ({info['position']}), {grade // 2} star(s). Nhận gói để thêm vào đội.")
    entry["staffs"] = [staff]
    entry["bundleicon"] = "staffpack/goldall"
    hc._save(st, uname)
    return {"id": entry["_id"], "stfid": stfid, "name": info["name"], "grade": grade,
            "bag_count": len(hc.my_coach(st).get("bag", [])), "slot": slot}


@api("player_grade")
def player_grade(p):
    uname = _target_user(p)
    st = hc._state(uname)
    player(st, p.get("id"), uname)["grade"] = as_int(p.get("grade"), 0, 10, "grade")
    hc._save(st, uname)
    return {"ok": True}


@api("player_remove")
def player_remove(p):
    uname = _target_user(p)
    st = hc._state(uname)
    t = team(st, uname)
    x = player(st, p.get("id"), uname)
    if hc.in_squad(t, x):
        raise AdminError("Player is in starting 18 squad; swap in Formation first")
    hc.remove_player(t, x)
    hc._save(st, uname)
    return {"ok": True}


@api("player_team")
def player_team(p):
    uname = _target_user(p)
    st = hc._state(uname)
    t = team(st, uname)
    x = player(st, p.get("id"), uname)
    if p.get("team") == "second":
        if hc.in_squad(t, x):
            raise AdminError("Starting 18 squad players must remain in Main Team")
        x["team2"] = True
    else:
        x.pop("team2", None)
    hc._save(st, uname)
    return {"ok": True}


def give_pack(st, itemid, count, uname=None):
    inv = coach(st, uname).setdefault("items", [])
    stack = next((i for i in inv if i["itemid"] == itemid), None)
    if stack is None:
        stack = {"_id": hc.new_iid(), "itemid": itemid, "remains": 0}
        inv.append(stack)
    stack["remains"] += count


@api("give_pack")
def api_give_pack(p):
    uname = _target_user(p)
    st = hc._state(uname)
    itemid = as_int(p.get("itemid"), 1, 10 ** 10, "pack")
    if itemid not in hc.PACKS:
        raise AdminError("Unknown pack")
    give_pack(st, itemid, as_int(p.get("count", 1), 1, 999, "quantity"), uname)
    hc._save(st, uname)
    return {"ok": True, "user": uname or "__default__"}


@api("bag_send")
def bag_send(p):
    """A gift in the Bag (Item saya > Items): EP and/or a pack and/or a player, sent to the target user."""
    uname = _target_user(p)
    st = hc._state(uname)
    _ensure_user_coach(st, uname)
    items = hc.reward_items({
        "ep": as_int(p.get("ep") or 0, 0, 10 ** 12, "EP"),
        "pack": as_int(p["pack"], 1, 10 ** 10, "pack") if p.get("pack") else None,
        "count": as_int(p.get("count") or 1, 1, 999, "quantity"),
    })
    if any(i["itemid"] != hc.EP_ITEM and i["itemid"] not in hc.PACKS for i in items):
        raise AdminError("Unknown pack")
    players_ = []
    if p.get("spid"):
        spid = as_int(p["spid"], 1, 10 ** 9, "spid")
        if not names([spid]):
            raise AdminError("SPID not found in database")
        players_ = [{"spid": spid, "grade": as_int(p.get("grade") or 1, 1, 10, "grade")}]
    if not items and not players_:
        raise AdminError("Please provide EP, pack, or player")
    e = hc.bag_add(st, str(p.get("title") or "Admin Gift")[:60], items, players_, msg="Gift from server admin.")
    hc._save(st, uname)
    return {"id": e["_id"], "user": uname or "__default__"}


@api("daily")
def daily(p):
    uname = _target_user(p)
    st = hc._state(uname)
    return {
        "table": hc.daily_table(),
        "status": coach(st, uname).get("daily") or {"last": "", "streak": 0, "total": 0},
        "today": hc.today_wib(),
        "file": os.path.relpath(hc.DAILY_FILE, HERE),
    }


@api("daily_set")
def daily_set(p):
    table = []
    for i, r in enumerate(p.get("table") or []):
        ep = as_int(r.get("ep") or 0, 0, 10 ** 12, f"Day {i + 1} EP")
        pack = as_int(r["pack"], 1, 10 ** 10, f"Day {i + 1} pack") if r.get("pack") else None
        if pack is not None and pack not in hc.PACKS:
            raise AdminError(f"Day {i + 1} pack unknown")
        if not ep and pack is None:
            raise AdminError(f"Day {i + 1} is empty")
        table.append({**({"ep": ep} if ep else {}),
                      **({"pack": pack, "count": as_int(r.get("count") or 1, 1, 99, "quantity")} if pack else {})})
    if not 1 <= len(table) <= 31:
        raise AdminError("Table must contain 1..31 days")
    with open(hc.DAILY_FILE, "w", encoding="utf-8") as f:
        json.dump(table, f, indent=1)
    return {"table": table}


@api("daily_reset")
def daily_reset(p):
    uname = _target_user(p)
    st = hc._state(uname)
    coach(st, uname)["daily"] = {"last": "", "streak": 0, "total": 0}
    hc._save(st, uname)
    return {"ok": True}


@api("daily_grant")
def daily_grant(p):
    """Give the next day's reward now (as if logging in tomorrow), for testing."""
    uname = _target_user(p)
    st = hc._state(uname)
    d = coach(st, uname).setdefault("daily", {"last": "", "streak": 0, "total": 0})
    if d["last"] == hc.today_wib():
        d["last"] = hc.today_wib(time.time() - 86400)
    e = hc.daily_login(st)
    hc._save(st, uname)
    return {"title": e["title"] if e else None}


@api("scouts_reset")
def scouts_reset(p):
    uname = _target_user(p)
    st = hc._state(uname)
    coach(st, uname)["scout_used"] = {}
    hc._save(st, uname)
    return {"ok": True}


@api("log")
def log(p):
    n = as_int(p.get("n", 80), 1, 1000, "n")
    try:
        with open(hc.LOG, encoding="utf-8", errors="replace") as f:
            lines = f.readlines()[-n:]
    except OSError:
        lines = []
    return {"lines": [ln.rstrip("\n")[:300] for ln in lines]}


@api("enhancement_settings")
def enhancement_settings_get(p):
    import enhancement_settings
    return {"success_percent": enhancement_settings.get_rate()}


@api("enhancement_settings_set")
def enhancement_settings_set(p):
    import enhancement_settings
    value = p.get("success_percent")
    if "success_percent" not in p:
        raise AdminError("Thiếu tỷ lệ thành công.")
    try:
        enhancement_settings.set_rate(value)
    except ValueError as e:
        raise AdminError(str(e))
    return {"success_percent": value}


@api("duplicate_player_settings")
def duplicate_player_settings_get(p):
    import duplicate_player_settings
    return {"allow_duplicates": duplicate_player_settings.get_allowed()}


@api("duplicate_player_settings_set")
def duplicate_player_settings_set(p):
    import duplicate_player_settings
    if type(p.get("allow_duplicates")) is not bool:
        raise AdminError("allow_duplicates must be true or false")
    duplicate_player_settings.set_allowed(p["allow_duplicates"])
    return {"allow_duplicates": p["allow_duplicates"]}


@api("events")
def events_get(p):
    uname = _target_user(p)
    st = hc._state(uname)
    ev = st.get("events") or {}
    return {
        "ladder_season_end": ev.get("ladder_season_end", "2026-12-31"),
        "active_cups": ev.get("active_cups", ["1", "2", "10001"]),
        "worldcup_teamid": int(ev.get("worldcup_teamid") or 0)
    }


@api("events_set")
def events_set(p):
    st = hc._state()
    ev = st.setdefault("events", {})
    if "ladder_season_end" in p:
        ev["ladder_season_end"] = str(p["ladder_season_end"]).strip()
    if "active_cups" in p:
        if isinstance(p["active_cups"], list):
            ev["active_cups"] = [str(x) for x in p["active_cups"]]
    if "worldcup_teamid" in p:
        ev["worldcup_teamid"] = int(p["worldcup_teamid"])
    hc._save(st, uname)
    return {
        "ladder_season_end": ev.get("ladder_season_end", "2026-12-31"),
        "active_cups": ev.get("active_cups", ["1", "2", "10001"]),
        "worldcup_teamid": int(ev.get("worldcup_teamid") or 0)
    }


class Admin(http.server.BaseHTTPRequestHandler):
    def _send(self, code, data, ctype="application/json"):
        self.send_response(code)
        self.send_header("Content-Type", ctype)
        self.send_header("Content-Length", str(len(data)))
        self.send_header("Cache-Control", "no-store")
        self.end_headers()
        self.wfile.write(data)

    def _route(self):
        url = urllib.parse.urlparse(self.path)
        if url.path == "/notice.html":
            with open(os.path.join(HERE, "admin", "notice.html"), "rb") as f:
                return self._send(200, f.read(), "text/html; charset=utf-8")
        if url.path == "/notice-qr.png":
            with open(os.path.join(HERE, "admin", "vietinbank-qr.png"), "rb") as f:
                return self._send(200, f.read(), "image/png")
        if url.path in ("/", "/index.html"):
            with open(PAGE, "rb") as f:
                return self._send(200, f.read(), "text/html; charset=utf-8")
        name = url.path[len("/api/"):] if url.path.startswith("/api/") else ""
        fn = API.get(name)
        if fn is None:
            return self._send(404, b'{"error": "not found"}')
        if name not in READ_ONLY and (self.command != "POST" or self.headers.get("X-FO3-Admin") != "1"):
            return self._send(403, b'{"error": "changes need POST with the X-FO3-Admin header"}')
        params = dict(urllib.parse.parse_qsl(url.query))
        n = int(self.headers.get("Content-Length") or 0)
        try:
            if n:
                params.update(json.loads(self.rfile.read(n) or b"{}"))
            with hc.STATE_LOCK:
                out = fn(params)
        except AdminError as e:
            out = {"error": str(e)}
        except Exception as e:
            out = {"error": f"{type(e).__name__}: {e}"}
        self._send(200, json.dumps(out).encode())

    do_GET = _route
    do_POST = _route

    def log_message(self, *a):
        pass


def start(module, port):
    """Serve the admin page on 0.0.0.0:<port> in a daemon thread."""
    global hc
    hc = module
    srv = http.server.ThreadingHTTPServer(("0.0.0.0", port), Admin)
    threading.Thread(target=srv.serve_forever, daemon=True, name="admin").start()
    return srv
