_LAST_REQUEST_HOST = '127.0.0.1'
"""HTTP config/publisher service stub for FO3ID.

The client requests these relative paths (found in client memory):
    /info/server/config        -> expected: server list / endpoints config
    /game/my/ipaddress         -> expected: client's public IP echo
    /publisher/garena/sign     -> publisher token sign/verify
    /publisher/nexon/2ndauth   -> secondary auth
    /telemetry/gameconnection  -> telemetry sink (can be a no-op)
    /shop/cash/charge/url, /shop/cash/history/url -> cash shop URLs

Phase 1: log every request (method, path, query, headers, body) and return a minimal
placeholder so we can see which endpoint the client hits first and what it expects.
The real response shapes are unknown until captured — every handler is a guess.

Usage: python -I http_config.py [port=8080] [--tls]
Default port 8080 to avoid colliding with a local Apache on :80.

--tls serves HTTPS with certs/server.pem. fifazf.exe calls a hardcoded
https://APSEODESK172 (port 443) for /info/server/locale etc. — no launch arg
overrides it — so run `http_config.py 443 --tls` and map APSEODESK172 to
127.0.0.1 in the hosts file.
"""
import bisect, collections, functools, gzip, http.server, math, os, random, re, sqlite3, ssl, statistics, sys, threading, time
from contextlib import closing

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, HERE)  # `python -I` leaves the script dir off sys.path
import gen_teams
TLS = "--tls" in sys.argv
_args = [a for a in sys.argv[1:] if a != "--tls"]
PORT = int(_args[0]) if _args else 8080
LOG = os.path.join(HERE, "captures", "http_config.log")
CERT = os.path.join(HERE, "certs", "server.pem")  # self-signed, CN/SAN APSEODESK172
os.makedirs(os.path.dirname(LOG), exist_ok=True)

import json

# Where this service tells the client the game servers live.
REDIR_HOST = os.environ.get("FO3_SERVER_IP", "203.0.113.10")
REDIR_PORT = 28000
NOTI_HOST = os.environ.get("FO3_SERVER_IP", "203.0.113.10")
NOTI_PORT = 28001

# --------------------------------------------------------------------------- #
# Endpoint bodies — TENTATIVE. The real response format (XML vs JSON, field
# names, the token/sign scheme) is unknown: the client has not been captured
# reaching these (XignCode blocks it earlier — see README). These return minimal
# plausible JSON so the client has *something* to parse; correct each shape from
# a capture. Every request is logged regardless, which is the point for now.
# --------------------------------------------------------------------------- #

def _json(obj):
    return ("application/json", json.dumps(obj).encode())


def ep_server_config(query, body):
    return _json({
        "redirector": {"host": REDIR_HOST, "port": REDIR_PORT},
        "notification": {"host": NOTI_HOST, "port": NOTI_PORT},
        "region": "VN",
        "status": "ok",
    })


def ep_locale(query, body):
    # First call fifazf.exe makes. Body "OK" -> JSON_PARSING_ERROR in-game; {"result": true}
    # passes. `language` must be a WebCache.setBaseUrl key (kor eng chi vie tha ind jpn,
    # default kor): "id" crashed the UI (the old black screen); "ind" = Indonesian, its
    # externalAssets/language_ind.big is in the client cache. Publisher (Garena) is chosen
    # by launch args `region id token <t>`, not by this response.
    # `url` = WebCache base for externalAssets (crests, uniforms, player cards...). Without
    # it the client uses the dead Garena CDN, every refresh fails and crests show
    # "PLACE HOLDER" even though the files sit in its own _cache. Served by cdn_file().
    st = _state()
    ev = st.get("events") or {}
    cdn_url = os.environ.get("FO3_CDN_URL") or ev.get("cdn_url") or f"https://APSEODESK172/{CDN_PREFIX}"
    if not cdn_url.endswith("/"):
        cdn_url += "/"
    return _json({"result": True, "language": LANGUAGE, "url": cdn_url})


LANGUAGE = os.environ.get("FO3_LANGUAGE", "vie")


def _detect_game_dir():
    if "FO3_GAME" in os.environ and os.path.isdir(os.environ["FO3_GAME"]):
        return os.path.abspath(os.environ["FO3_GAME"])
    for candidate in [
        r"C:\FO3TH",
        os.path.join(HERE, ".."),
        os.path.join(HERE, "..", "Game"),
        os.path.join(HERE, "Game"),
        r"E:\FO3TH - Copy",
    ]:
        if os.path.isfile(os.path.join(candidate, "fifazf.exe")):
            return os.path.abspath(candidate)
    return os.path.abspath(os.path.join(HERE, ".."))

GAME = _detect_game_dir()


def _detect_assets_dir():
    if "FO3_ASSETS" in os.environ and os.path.isdir(os.environ["FO3_ASSETS"]):
        return os.path.abspath(os.environ["FO3_ASSETS"])
    for candidate in [
        os.path.join(HERE, "assets"),
        r"C:\FO3TH\_cache\games\fo3sg",
        os.path.join(GAME, "_cache", "games", "fo3sg"),
        os.path.join(GAME, "_cache", "games", "fo3th"),
        os.path.join(GAME, "_cache", "games", "fo3id"),
        os.path.join(GAME, "_cache", "games", "fo3vn"),
        os.path.join(GAME, "_cache"),
    ]:
        if os.path.isdir(candidate):
            return os.path.abspath(candidate)
    return os.path.join(GAME, "_cache")

# The client's own downloaded copy of the Garena CDN (externalAssets/...).
ASSETS = _detect_assets_dir()
CDN_PREFIX = "cdn/"


def cdn_file(rel):
    """(content-type, bytes) of ASSETS/rel, or None (-> 404, client shows its default)."""
    if rel.endswith("_timestamp") or rel.endswith("timestamp"):
        return ("text/plain", b"1700000000")
    f = None
    roots = [
        ASSETS,
        os.path.join(HERE, "assets"),
        os.path.join(HERE, "assets", "externalAssets"),
        r"C:\FO3TH\_cache\games\fo3sg",
        r"C:\FO3TH\_cache\games\fo3sg\externalAssets",
    ]
    for root in roots:
        if not os.path.isdir(root):
            continue
        cand = os.path.normpath(os.path.join(root, rel))
        if os.path.isfile(cand):
            f = cand
            break
        if rel.startswith("externalAssets/") or rel.startswith("externalAssets\\"):
            sub_rel = rel[15:]
            cand2 = os.path.normpath(os.path.join(root, sub_rel))
            if os.path.isfile(cand2):
                f = cand2
                break

    if f is None:
        rel_clean = rel.replace("\\", "/").lower()
        if "badge/" in rel_clean:
            preferred_sz = ["large.png", "small.png", "normal.png"] if "large" in rel_clean else ["small.png", "large.png", "normal.png"]
            for root in roots:
                for fallback_bid in ["900001", "1001"]:
                    for sz in preferred_sz:
                        cand = os.path.join(root, "externalAssets", "badge", fallback_bid, sz)
                        if os.path.isfile(cand):
                            f = cand
                            break
                        cand2 = os.path.join(root, "badge", fallback_bid, sz)
                        if os.path.isfile(cand2):
                            f = cand2
                            break
                    if f: break
                if f: break
        elif "trophies/" in rel_clean:
            for root in roots:
                cand = os.path.join(root, "externalAssets", "trophies", "t13.png")
                if os.path.isfile(cand):
                    f = cand
                    break
                cand2 = os.path.join(root, "trophies", "t13.png")
                if os.path.isfile(cand2):
                    f = cand2
                    break
        elif "crests/small/" in rel_clean:
            for root in roots:
                cand = os.path.join(root, "externalAssets", "crests", "small", "s241.png")
                if os.path.isfile(cand):
                    f = cand
                    break
                cand2 = os.path.join(root, "crests", "small", "s241.png")
                if os.path.isfile(cand2):
                    f = cand2
                    break
        elif "crests/large/" in rel_clean:
            for root in roots:
                cand = os.path.join(root, "externalAssets", "crests", "large", "s241.png")
                if os.path.isfile(cand):
                    f = cand
                    break
                cand2 = os.path.join(root, "crests", "large", "s241.png")
                if os.path.isfile(cand2):
                    f = cand2
                    break

    if f is None:
        return None
    ctype = {".png": "image/png", ".jpg": "image/jpeg", ".swf": "application/x-shockwave-flash", ".txt": "text/plain"}
    with open(f, "rb") as fh:
        return (ctype.get(os.path.splitext(f)[1].lower(), "application/octet-stream"), fh.read())


def ep_server_time(query, body):
    # TimerUtil.getTime: retries every second (and holds the bootflow) until `timestamp`
    # (ms, fed to new Date()) is present. timezone guessed as JS getTimezoneOffset() for
    # WIB (UTC+7) — the backend looked like Node/Express.
    return _json({"timestamp": int(time.time() * 1000), "timezone": -420})


def ep_my_ip(query, body):
    # real HTTP PUT from the lobby; every reply is JSON-parsed
    return _json({"result": True, "ipaddress": "127.0.0.1"})


def ep_garena_sign(query, body):
    # Accept any token for local offline use; echo a success + a fake signed token.
    return _json({"result": "ok", "token": "LOCAL-SIGNED-TOKEN", "uid": 1})


def ep_nexon_2ndauth(query, body):
    return _json({"result": "ok", "passport": "LOCAL-PASSPORT"})


def ep_telemetry(query, body):
    # Client JSON-parses every reply; plain "OK" -> JSON_PARSING_ERROR in-game.
    return _json({"result": True})


# The starter song (music/starter.mp3) lives in the game's own EA Trax playlist as TrackID 1,
# put there by tools/eatrax_song.py; the server does not play music.


def ep_empty(query, body):
    return _json({})


def ep_shop_url(query, body):
    return _json({"url": "about:blank"})


# ponytail: one local account kept in a JSON file; a real multi-user store comes later.
STATE = os.path.join(HERE, "gamedata", "state.json")
# Handlers read-modify-write state.json; game requests and the admin page (admin.py) take this
# so one never reads a half-written file or overwrites the other's change.
STATE_LOCK = threading.Lock()


def _state():
    try:
        with open(STATE) as f:
            return json.load(f)
    except FileNotFoundError:
        return {"coaches": [], "defaultcoachid": "", "teams": {}}


def _save(st):
    os.makedirs(os.path.dirname(STATE), exist_ok=True)
    with open(STATE, "w") as f:
        json.dump(st, f)


def coach_id(slot):
    return f"{slot + 1:024x}"  # Mongo-style _id


def ep_coaches(query, body):
    # StartupCoachCreationScreen: coachlist entries need _id, slot, name, state "active"
    # ("postpone" = deleted, restorable until hideat; recoveryperiod s goes in the delete notice).
    # initializeMyCoach also shows team name/crest from summary and teamvalue.val, same
    # values as the lobby HUD (coach_me).
    st = _state()
    if purge_deleted_coaches(st):
        _save(st)
    def entry(c):
        me = coach_me(st, c["slot"])
        team = me["team"]
        starters = {p["id"] for p in team["squad"]["positions"] if 0 < p["pos"] < POS_SUB}
        candidates = [p for p in team["players"] if p["id"] in starters]
        front = next((p for p in candidates if p["id"] == team["captain"]),
                     max(candidates, key=player_ovr) if candidates else None)
        # UpdateCoaches reads frontplayer.kit.home/goalie.id, not kit.availables.
        preview = ({"frontplayer": dict(front, kit=team_kit(c["teamid"])["availables"],
                                        lr=front.get("lr") or {})} if front else {})
        return dict(c, **preview, summary=me["summary"], teamvalue={"val": sum(card_price(p.get("spid", 0), max(1, int(p.get("grade") or 1))) for p in me["team"]["players"])},
                    levelinfo=me["levelinfo"],
                    recoveryperiod=COACH_RECOVERY, **({"hideat": w3c(c["deleteat"])} if "deleteat" in c else {}))
    usable = {c["_id"] for c in st["coaches"] if c.get("state", "active") == "active"}
    return _json({"coachlist": [entry(c) for c in st["coaches"]],
                  "defaultcoachid": st["defaultcoachid"] if st["defaultcoachid"] in usable else ""})


def coach_name_valid(st, name):
    if not isinstance(name, str) or not 2 <= len(name.strip()) <= 12 or any(ord(c) < 32 for c in name):
        return 'invalid name'
    if any(c['name'].casefold() == name.strip().casefold() for c in st['coaches']):
        return 'busy'
    return None


def ep_coach_reserve(query, body):
    req = json.loads(body or b'{}')
    st = _state()
    reason = coach_name_valid(st, req.get('name'))
    if len(st['coaches']) >= 2:
        reason = 'busy'
    return _json({'result': False, 'reason': reason} if reason else {'result': True})


def new_manager_team(teamid, chosen=()):
    import copy
    team = copy.deepcopy(roster(teamid)['team'])
    if chosen:
        chosen = {int(p.get('spid')) if isinstance(p, dict) else int(p) for p in chosen}
        selected = [p for p in team['players'] if int(p['spid']) in chosen]
        # Keep the requested cards and supply the bench required by the native client.
        selected += [p for p in team['players'] if p not in selected][:max(0,18-len(selected))]
        team['players'] = selected
    fix_formation(team, teamid)
    repair_squad(team)
    return team


def ep_new_team(slot, query, body):
    st = _state()
    if slot not in (0, 1) or any(c['slot'] == slot for c in st['coaches']):
        return _json({'result': False, 'reason': 'busy'})
    req = json.loads(body or b'{}')
    try:
        teamid = int(req.get('id') or 0)
        team = new_manager_team(teamid, req.get('spids') or [])
    except (ValueError, TypeError):
        return _json({'result': False, 'reason': 'invalid team'})
    if len(team['players']) < 18:
        return _json({'result': False, 'reason': 'invalid team'})
    st.setdefault('pendingteams', {})[str(slot)] = {'teamid': teamid, 'team': team}
    _save(st)
    return _json({'result': True, 'team': owned_team(team)})


def ep_create_coach(slot, query, body):
    req = json.loads(body or b'{}')
    st = _state()
    if slot not in (0, 1) or any(c['slot'] == slot for c in st['coaches']):
        return _json({'result': False, 'reason': 'busy'})
    name = req.get('coachName')
    reason = coach_name_valid(st, name)
    if reason:
        return _json({'result': False, 'reason': reason})
    try:
        teamid = int(req.get('id') or 0)
        pending = st.get('pendingteams', {}).get(str(slot))
        team = pending['team'] if pending and pending['teamid'] == teamid else new_manager_team(teamid)
    except (ValueError, TypeError):
        return _json({'result': False, 'reason': 'invalid team'})
    if len(team['players']) < 18:
        return _json({'result': False, 'reason': 'invalid team'})
    st['teams'][str(slot)] = team
    st.setdefault('pendingteams', {}).pop(str(slot), None)
    st['coaches'].append({'_id': coach_id(slot), 'slot': slot, 'name': name.strip(), 'state': 'active',
        'teamid': teamid, 'teamvalue': {'val': 0}, 'levelinfo': {'level': 1}, 'summary': {}, 'createdat': w3c(time.time())})
    _save(st)
    return _json({'result': True})


# Deleting a manager (StartupCoachCreationScreen > Delete): PUT /coach/<slot>/postponed puts it in
# state "postpone" until hideat; "Kembalikan manager" = DELETE /coach/<_id>/postponed undoes it.
# Once hideat has passed CoachUtil.shouldDeleteCoach shows the slot as free ("Buat manager baru").
COACH_RECOVERY = 300  # s a deleted manager can still be restored (the real game: 72 h)


def purge_deleted_coaches(st):
    """Drop managers whose restore time is over, with their club. The account-wide club data
    (league, cups, market offers, last match, trade) belongs to the default manager's club."""
    gone = [c for c in st["coaches"] if c.get("state") == "postpone" and c.get("deleteat", 0) <= time.time()]
    for c in gone:
        st["coaches"].remove(c)
        st["teams"].pop(str(c["slot"]), None)
        if c["_id"] == st["defaultcoachid"]:
            st["defaultcoachid"] = ""
            for k in ("league2", "cups", "offers", "match", "trade"):
                st.pop(k, None)
    return gone


def ep_coach_postponed(key, query, body):
    """PUT /coach/<slot>/postponed: delete a manager; DELETE /coach/<_id>/postponed: restore it."""
    st = _state()
    purge_deleted_coaches(st)
    c = next((c for c in st["coaches"] if key in (str(c["slot"]), c["_id"])), None)
    if c is None:
        reply = {"result": False, "reason": "already removed"}  # restore: COACH_CREATION_RESTORE_ERROR
    elif json.loads(body or b"{}").get("_method") == "DELETE":
        c["state"] = "active"
        c.pop("deleteat", None)
        reply = {"result": True}
    else:
        c["state"] = "postpone"
        c["deleteat"] = time.time() + COACH_RECOVERY
        reply = {"result": True}
    _save(st)
    return _json(reply)


POS_SUB = 28  # Enums.PLAYERPOSITION: 0..27 pitch positions, 28 SUB, 29.. RESERVE


# OptionManager.applySoundSetting hands native volume * Master (each 0..10) for Commentary,
# PAVolume, PlayerCalls, SFX, Crowd and BGM; a missing key is NaN = silent (no music).
SOUND_DEFAULTS = {"Master": 10, "Commentary": 10, "PAVolume": 10, "PlayerCalls": 10, "SFX": 10, "Crowd": 10, "BGM": 10}

# OptionCameraSettingScreen reads these fields as soon as its Customize row opens.
# Old/local profiles often contain only Camera and CamDetail, which leaves the
# numeric controls undefined in Scaleform and breaks the whole options UI.
CAMERA_DEFAULTS = {"Camera": 2, "CamDetail": 1, "CamHeight": 0, "CamZoom": 0,
                   "CamProZoom": 0, "CamProSpeed": 0, "CamProSwing": 0}


def camera_options(saved=None):
    """Complete legacy camera settings without replacing valid saved values."""
    saved = saved if isinstance(saved, dict) else {}
    out = dict(CAMERA_DEFAULTS)
    for key, default in CAMERA_DEFAULTS.items():
        value = saved.get(key)
        if type(value) is int and 0 <= value <= 100:
            out[key] = value
    return out


# Complete client-native keyboard/pad layouts. Missing controlOption makes
# OptionManager.applyControlSetting return before assigning the controller.
CONTROL_DEFAULTS = {'Controller': 0, 'Type': 0, 'JoyAttType1': [4, 3, 2, 1, 5, 7, 6, 8], 'JoyDefType1': [4, 3, 2, 1, 5, 7, 6, 8], 'JoyMovType1': [9, 11, 10], 'JoyAttType2': [4, 2, 3, 1, 5, 6, 8, 7], 'JoyDefType2': [4, 2, 3, 1, 5, 6, 8, 7], 'JoyMovType2': [9, 11, 10], 'JoyAttType3': [4, 3, 2, 1, 5, 7, 6, 8], 'JoyDefType3': [4, 3, 2, 1, 5, 7, 6, 8], 'JoyMovType3': [11, 9, 10], 'KeyAttType1': [87, 65, 68, 83, 81, 90, 67, 69], 'KeyDefType1': [87, 65, 68, 83, 81, 90, 67, 69], 'KeyMovType1': [38, 40, 37, 39, 256, 257, 258], 'KeyAttType2': [87, 65, 68, 83, 81, 90, 67, 69], 'KeyDefType2': [87, 65, 83, 81, 68, 90, 67, 69], 'KeyMovType2': [38, 40, 37, 39, 256, 257, 258], 'KeyAttType3': [69, 65, 68, 83, 81, 90, 67, 87], 'KeyDefType3': [69, 65, 68, 83, 81, 90, 67, 87], 'KeyMovType3': [38, 40, 37, 39, 256, 257, 258], 'KeyAttType4': [80, 75, 186, 76, 73, 190, 191, 79], 'KeyDefType4': [80, 75, 186, 76, 73, 190, 191, 79], 'KeyMovType4': [87, 83, 65, 68, 256, 257, 258], 'KeyDefType5': [87, 65, 68, 32, 81, 90, 67, 69], 'KeyDefType6': [87, 65, 83, 32, 68, 90, 67, 69], 'KeyDefType7': [69, 65, 68, 32, 81, 90, 67, 87], 'KeyDefType8': [80, 75, 186, 32, 73, 190, 191, 79], 'KeyAttType5': [87, 65, 68, 83, 81, 90, 67, 69], 'KeyAttType6': [87, 65, 68, 83, 81, 90, 67, 69], 'KeyAttType7': [69, 65, 68, 83, 81, 90, 67, 87], 'KeyAttType8': [80, 75, 186, 76, 73, 190, 191, 79], 'KeyMovType5': [38, 40, 37, 39, 256, 257, 258], 'KeyMovType6': [38, 40, 37, 39, 256, 257, 258], 'KeyMovType7': [38, 40, 37, 39, 256, 257, 258], 'KeyMovType8': [87, 83, 65, 68, 256, 257, 258]}


def control_options(saved=None):
    """Complete partial profiles; never hand native input an empty key map."""
    saved = saved if isinstance(saved, dict) else {}
    out = {}
    for key, default in CONTROL_DEFAULTS.items():
        value = saved.get(key)
        if isinstance(default, list):
            valid = (isinstance(value, list) and len(value) == len(default)
                     and all(type(n) is int and 0 <= n <= 65535 for n in value))
            out[key] = list(value if valid else default)
    controller = saved.get("Controller", 0)
    out["Controller"] = controller if type(controller) is int and controller in (0, 1) else 0
    layout = saved.get("Type", 0)
    count = 8 if out["Controller"] == 0 else 3
    out["Type"] = layout if type(layout) is int and 0 <= layout < count else 0
    return out


def ep_account(query, body):
    """GET /account (AccountProxy.accountData): responses/account_base.json, with the options
    the client saved through PUT /account/option layered over `option`."""
    with open(os.path.join(RESPONSES, "account_base.json"), encoding="utf-8") as f:
        acc = json.load(f)
    acc["option"].update(_state().get("option", {}))
    acc["option"]["controlOption"] = control_options(acc["option"].get("controlOption"))
    acc["option"]["soundOption"] = {**SOUND_DEFAULTS, **(acc["option"].get("soundOption") or {})}
    acc["option"]["cameraOption"] = camera_options(acc["option"].get("cameraOption"))
    return _json(acc)


def ep_account_option(query, body):
    """Save complete control profiles and return the option resource expected by the UI."""
    req = json.loads(body or b"{}")
    st = _state()
    options = st.setdefault("option", {})
    options.update({k: v for k, v in req.items() if k.endswith("Option") and isinstance(v, dict)})
    options["controlOption"] = control_options(options.get("controlOption"))
    options["cameraOption"] = camera_options(options.get("cameraOption"))
    _save(st)
    return _json({"result": True, "resources": [{"resource": "/account/option", "uri": "/account/option", "body": options}]})


def lineup_duplicate_error(positions):
    if not duplicate_lineups_allowed() and has_duplicate_players(positions):
        return 'DUPLICATE_PLAYERS_NOT_ALLOWED'
    return None


def ep_coach_squad(query, body):
    """PUT /coach/me/squad {squad:{positions,formation,...}, kickers, captain} from Tim > Formasi.
    Player ids are instance ids (iid)."""
    req = json.loads(body or b"{}")
    st = _state(); slot = _default_slot(st); team = st["teams"].get(str(slot))
    sq = req.get("squad") or {}
    if not duplicate_lineups_allowed() and has_duplicate_players(sq.get("positions") or []):
        return _json({'result': False, 'reason': 'DUPLICATE_PLAYERS_NOT_ALLOWED'})
    spid_of = {iid(p): p["spid"] for p in team["players"]} if team is not None else {}
    positions = [{"id": str(p["id"]), "spid": spid_of[str(p["id"])], "pos": p["pos"],
                  **({"starting": p["starting"]} if p.get("starting") else {})}
                 for p in sq.get("positions") or [] if str(p.get("id")) in spid_of and p.get("pos", 99) <= POS_SUB]
    # a client still holding old instance ids (e.g. from before a card migration) must not
    # wipe the XI: keep the stored squad unless the new one has 11 known starters
    duplicate_error = lineup_duplicate_error(positions)
    if duplicate_error:
        return _json({"result": False, "reason": duplicate_error})
    if (sum(p["pos"] < POS_SUB for p in positions) == 11
            and len({p["id"] for p in positions}) == len(positions)
            and len({p["pos"] for p in positions if p["pos"] < POS_SUB}) == 11):
        squad_presets(team)
        selected = req.get('presetidx', team.get('presetidx', 0))
        if type(selected) is not int or not 0 <= selected < 3:
            return _json({'result': False, 'reason': 'INVALID_SQUAD'})
        team['presetidx'] = selected
        team["squad"]["positions"] = positions
        for k in ("formation", "workrate"):
            if k in sq:
                team["squad"][k] = sq[k]
        if str(req.get("captain")) in spid_of:
            team["squad"]["captain"] = str(req["captain"])
        if req.get("kickers") and all(str(k) in spid_of for k in req["kickers"] if k):
            team["squad"]["kickers"] = [str(k) for k in req["kickers"] if k]
        squad_presets(team)
        _save(st)
    return _json({"result": True, "resources": coach_me_resources(st, slot)})


def ep_team_role(role, query, body):
    """Native Role tab saves one role at a time, separately from the formation."""
    if role != 'captain' and role not in ('0', '1', '2', '3', '4'):
        return _json({'result': False, 'reason': 'INVALID_SQUAD'})
    req = json.loads(body or b'{}')
    playerid = req.get('id')
    if not isinstance(playerid, (str, int)) or isinstance(playerid, bool):
        return _json({'result': False, 'reason': 'INVALID_SQUAD'})
    playerid = str(playerid)
    st = _state()
    slot = _default_slot(st)
    team = st['teams'].get(str(slot))
    if team is None:
        return _json({'result': False, 'reason': 'INVALID_SQUAD'})
    repair_squad(team)
    starters = {p['id'] for p in team['squad']['positions'] if p['pos'] < POS_SUB}
    if playerid not in starters:
        return _json({'result': False, 'reason': 'INVALID_SQUAD'})
    if role == 'captain':
        team['squad']['captain'] = playerid
    else:
        team['squad']['kickers'][int(role)] = playerid
    team['captain'] = team['squad']['captain']
    team['kickers'] = list(team['squad']['kickers'])
    squad_presets(team)
    _save(st)
    return _json({'result': True, 'resources': coach_me_resources(st, slot)})


def _ensure_player_staticdata(r):
    if not r or not isinstance(r, dict):
        return r
    team = r.get("team") if "team" in r else r
    if not team or not isinstance(team, dict):
        return r
    players = team.get("players") or []
    for p in players:
        p["id"] = str(p.get("spid") or p.get("id") or "")
        p["spid"] = int(p.get("spid") or p.get("id") or 0)
        p.setdefault("expandme", "player")
        p["applyliveboost"] = 0
        if "staticdata" in p and isinstance(p["staticdata"], dict):
            sd = p["staticdata"]
            sd.setdefault("boosts", {"ovr": 0})
            sd.setdefault("stats", [75] * 40)
            sd.setdefault("statusAttribute", {})
            sd.setdefault("preferredposovr", int(p.get("overallrating") or p.get("ovr") or 75))
            sd.setdefault("names", {"short": p.get("name") or "Cầu thủ", "full": p.get("name") or "Cầu thủ"})
            sd["applyliveboost"] = 0
    return r

def roster(teamid):
    """{summary, team} of a club: the gen_teams.py roster file if there is one, else built
    the same way straight from fifadb (league opponents)."""
    if teamid in _NATIONAL_TEAMS:
        with closing(_db()) as c:
            r = gen_teams.team(c, teamid, nationality=_NATIONAL_TEAMS[teamid])
        if r is not None:
            info = _CLUB_TEAMS[teamid]
            nation = _CLUB_NATIONS[_NATIONAL_TEAMS[teamid]]
            r["summary"].update(name=info["teamname"], league={"id":78},
                                nation={"id":nation["nationid"], "name":nation["nationname"]}, rival={"name":""})
            return _ensure_player_staticdata(r)
    f = os.path.join(RESPONSES, f"static_info_authentic_team_all_{teamid}.json")
    if os.path.isfile(f):
        with open(f) as fh:
            return json.load(fh)
    with closing(_db()) as c:
        res = gen_teams.team(c, teamid) or {"summary": {}, "team": {"players": [], "squad": {"positions": [], "kickers": []}}}
        return _ensure_player_staticdata(res)


def roster_team(teamid):
    """A club as a stored team, for AI opponents."""
    return roster(teamid)["team"]


def _match_unique_duplicate_spids(team):
    """Give repeated copies of the exact same card distinct static player IDs in a match.

    The game client indexes its in-match player models by SPID. Duplicate owned-card IDs
    and jersey numbers are not enough to keep those models separate, so repeated copies
    can disappear. Use another season row for the same database player, only in the
    transient match payload; the saved cards and squad remain untouched.
    """
    players = team.get("players") or []
    if len({int(p.get("spid") or 0) for p in players}) == len(players):
        return
    spids = [int(p.get("spid") or 0) for p in players]
    card_ratings(spids)
    playerids = {spid: _PLAYER_IDS.get(spid) for spid in set(spids)}
    duplicates = {spid for spid in spids if spids.count(spid) > 1}
    if not duplicates:
        return
    with closing(_db()) as c:
        ids = sorted({playerids.get(spid) for spid in duplicates if playerids.get(spid)})
        rows = []
        for offset in range(0, len(ids), 500):
            chunk = ids[offset:offset + 500]
            if chunk:
                marks = ",".join("?" for _ in chunk)
                rows.extend(c.execute(
                    f"SELECT spid, playerid, overallrating FROM players WHERE playerid IN ({marks})", chunk
                ).fetchall())
    variants = collections.defaultdict(list)
    for row in rows:
        variants[int(row["playerid"])].append((int(row["spid"]), int(row["overallrating"] or 0)))

    used = set(spids)
    replacement_by_instance = {}
    for original in duplicates:
        playerid = playerids.get(original)
        if not playerid:
            continue
        group = [p for p in players if int(p.get("spid") or 0) == original]
        # Keep one card's original season and map only its additional copies.
        for player in group[1:]:
            target_rating = card_ratings([original])[original][1]
            candidates = sorted(
                ((spid, rating) for spid, rating in variants[playerid] if spid not in used and spid != original),
                key=lambda pair: (abs(pair[1] - target_rating), pair[0]),
            )
            if not candidates:
                continue
            replacement, _ = candidates[0]
            used.add(replacement)
            old_id = str(player.get("id") or player.get("_id") or original)
            replacement_by_instance[old_id] = replacement
            player["spid"] = replacement

    if replacement_by_instance:
        for position in (team.get("squad") or {}).get("positions", []):
            key = str(position.get("id") or "")
            if key in replacement_by_instance:
                position["spid"] = replacement_by_instance[key]


# gamesettings.halfLength, in 1/10 minute per half (the game setting screen shows
# halfLength/10*2 "menit"): 30 = a 6-minute match.
HALF_LENGTH = int(os.environ.get("FO3_HALF_LENGTH", "30"))


def match_side(raw, teamid, user=None, tactics=None):
    """match.side.<left|right>: PlayerListUtil.generateCaptainFromMatchInfo reads side.team
    {teamid, squad, players}, side.players (the users: id, name, levelinfo), MatchProxy
    fetchMatchSide* read squad, tactics, ready. tactics is the preset ARRAY (as
    /game/my/tactic/all): native 0x949170 iterates it ({id, bus, cc, def, teammentality})."""
    # Match kits use five low bits for the type (native fallback: teamid << 5).
    # Lobby/freeroam resources retain their separate ten-bit encoding.
    kit = {"availables": {k: {"id": int(teamid) << 5 | t}
                          for k, t in (("home", 0), ("away", 1), ("goalie", 2))}}
    team = owned_team(raw)
    _match_unique_duplicate_spids(team)
    team.update(teamid=teamid, summary=team_summary(team, teamid), kit=kit)
    # team color for the user's side only: an AI side is a whole club (+4 for free)
    team["teamcolors"] = active_teamcolors(team) if user else []
    if user:  # the pre-match squad screen's formation list (side.<mine>.team.formations;
        # owned_team only defaults it to [], so the formation menu/tab rendered
        # as empty black boxes -- Bonus 1, 2026-11-12)
        team["formations"] = team_formations(teamid)
    # ready: a user side starts unready -- MatchController.sendReady skips the PUT .../ready
    # (no kick-off) when isMySideReady is already true; AI sides are always ready.
    return {"team": team, "squad": team["squad"], "players": [user] if user else [],
            "tactics": match_tactic_payload(tactics), "ready": user is None, "kit": kit,
            # club must carry a clubname STRING: MultiplayGameSettingScreen
            # .onPlaying builds the versus record's clubName as
            # (side.club === undefined) ? "" : side.club.clubname, and the
            # native gameplay init reads that clubName through the hard GFx
            # AsString accessor -- an empty {} here made clubname undefined
            # and trapped the client at 0xAB2E48 after the loading screen
            # (Issue 2m; league never reads it, which is why league worked).
            "club": {"clubname": ""}}


# gamesettings.halfLength, in 1/10 minute per half (the game setting screen shows
# halfLength/10*2 "menit"): 30 = a 6-minute match.
HALF_LENGTH = int(os.environ.get("FO3_HALF_LENGTH", "30"))


def new_match(mtype, left, right, **extra):
    """A /match/<id> resource (MatchProxy, base uri /match). MatchController.onMatchChangeReceived
    runs processMatch* over: side.X {ready, club, players, squad, team{teamid, players, captain,
    kit, summary.league}}, elo.X.rating, gamesettings {kit, ball, stadium, tod, weather,
    halfLength, difficulty}, result, reward, boostweekid, stage (created|squad|playing|canceled).
    These run inside a native->AS3 callback: a TypeError there aborts the client (0xDEAD)."""
    mid = f"{int(time.time()):08x}{os.urandom(8).hex()}"  # Mongo-style id
    lt, rt = left["team"]["teamid"], right["team"]["teamid"]
    match = {"id": mid, "type": mtype, "created": time.strftime("%Y-%m-%dT%H:%M:%S.000Z", time.gmtime()),
             "stage": "created", "side": {"left": left, "right": right},
             "elo": {"left": {"rating": 1000}, "right": {"rating": 1000}},
             "result": None, "reward": None, "boostweekid": 0,
             # native ApplyGameSettings gets this object as-is.
             # kit: [left uniform id, right uniform id] as STRINGS: AS3 reads them in
             # processMatchSideStandingPlayerResource, native (0x9480D0) with asCString
             # home: Boolean (MatchProxy.isLeftTeamHome = gamesettings.home as Boolean)
        "gamesettings": {"home": True, "simulated": False, "canHaveReferee": False,
                              "stadiumchange": False, "timechange": False, "weatherchange": False,
                              "matchtimechange": False, "cardstrictness": 0, "foulstrictness": 0,
                              # kit: per side the KIT TYPE ("home"/"away"); native (0x948260) loads
                              # side kit.availables[<type>].id -- uniform ids here = checkerboard kits.
                              # ball.availables feeds MatchProxy.ballsDataProvider; empty -> the
                              # game setting screen's ball stepper reads item 0 = null (UI error).
                              # 0 = the ball the practice match loads (id -> db.ball model).
                              "kit": ["home", "away"],
                              "ball": {"id": 0, "availables": [{"id": 0}]},
                              # stadium 112 = the lobby stadium (data/sceneassets/stadium/stadium_112.rx3
                              # is known to load); null -> crash while loading the GAME scene
                              "stadium": 112, "tod": 0, "weather": 0, "difficulty": 0,
                              "halfLength": HALF_LENGTH,
                              # the rest is what native ApplyGameSettings (0x946FD0) reads; values are
                              # its own defaults (0x959050). Missing gameSpeed broke the match clock.
                              "gameSpeed": 1, "crowd": 100, "pitchWear": 1, "refereeIdSelector": 845,
                              "subRefereeIds": [0, 0], "disableMomentum": False, "bookingsOn": True,
                              "offsidesOn": True, "injuriesOn": True, "foulsOn": True, "fatigueOn": True,
                              "subOnBench": 5, "availableSubs": 3, "drawMode": 0, "gameType": mtype,
                              "shootoutmode": False, "prevScore": {"home": 0, "away": 0}}}
    match.update(extra)
    return match


def ep_match_campaign(query, body):
    """POST /match/campaign (tutorial > Training Ground): MatchController
    .onMatchPracticeResponseReceived registers resources[] and fires ON_PRACTICE_STARTED with
    the match body -> ApplyGameSettings + StartSingleMatch (a local match in the client)."""
    st = _state(); slot = _default_slot(st); me = coach_me(st, slot)
    user = {"id": me["id"], "name": me["name"], "levelinfo": me["levelinfo"]}
    ai = 243 if me["teamid"] != 243 else 241
    # type must be a non-PvP one (MatchController.isPvPMatchType: League_league, Cup_PvE,
    # WorldCup_PvE, WorldCup_Live, League_2_league, League_2_league_MGR); anything else makes
    # stage "created" call connectToTheSession -> native LobbyManager::ConnectSession, which
    # needs relayServer.address (crash without it).
    match = new_match("League_league", match_side(st["teams"][str(slot)], me["teamid"], user, _tactics(st)[1]),
                      match_side(roster_team(ai), ai))
    return _json({"result": True, "resources": [{"resource": f"/match/{match['id']}", "uri": f"/match/{match['id']}", "body": match}]})


def roster_summary(teamid):
    """Static team info (name, league, nation, rival) written by gen_teams.py."""
    s = club_summary_response(teamid)["summary"]
    return {k: s[k] for k in ("name", "league", "nation", "rival") if k in s}


# --------------------------------------------------------------------------- #
# Player cards. Ratings come from the client's own fifadb. World Legend EP prices use
# FIFAaddict FO4 Korea market references (gamedata/legend_fo4_prices.json); other cards keep
# their existing transfer-value/OVR pricing rules.
# --------------------------------------------------------------------------- #
# all-attribute boost by grade +0..+10 (= responses/static_db_PLAYERGRADE.json): fifaaddict's
# playerEnchantList, e.g. World Best Neymar 83 base shows 88 at +1 and 93 at +5
GRADE_BOOST = (0, 5, 6, 7, 8, 10, 12, 14, 17, 20, 24)
# EP price of grade g relative to +1: geometric mean over 16 fifaaddict cards (Indonesia server)
GRADE_PRICE = (0, 1, 2.65, 7.1, 19.6, 53, 162, 420, 1090, 2680, 6290)
FA_PRICES = os.path.join(HERE, "gamedata", "fa_prices.json")
WORLD_MARKET_VALUES = os.path.join(HERE, "gamedata", "world_market_values.json")
LEGEND_FO4_PRICES = os.path.join(HERE, "gamedata", "legend_fo4_prices.json")
_FA = None
_WORLD_VALUES = None
_LEGEND_PEAK_VALUES = None
_LEGEND_FO4_VALUES = None
_OVR = {}
_PLAYER_IDS = {}


def fa_prices():
    """{spid: Indonesia-server EP price at +1 (0 = listed on fifaaddict without a price)};
    {} when the file is missing."""
    global _FA
    if _FA is None:
        try:
            with open(FA_PRICES) as f:
                _FA = {int(k): int(v) for k, v in json.load(f).items()}
        except OSError:
            _FA = {}
    return _FA


# The rating the client shows for a card ("Atribut"/OVR, as on fifaaddict) is Enums.calcPosOvr
# at its preferredposition1: floor(sum(stat * weight) / 100) with STAT_POSOVRS weights below
# (fifadb's overallrating column runs ~5 higher for 2017 cards). POS_ROLE[Enums.PLAYERPOSITION].
ROLE_WEIGHTS = {
    "GK": {"gkdiving": 21, "gkhandling": 21, "gkkicking": 5, "gkpositioning": 21, "gkreflexes": 21, "reactions": 11},
    "SW": {"aggression": 8, "ballcontrol": 5, "headingaccuracy": 10, "jumping": 4, "marking": 15, "reactions": 5,
           "shortpassing": 5, "slidingtackle": 15, "standingtackle": 15, "strength": 10, "interceptions": 8},
    "WB": {"acceleration": 4, "ballcontrol": 8, "crossing": 12, "dribbling": 4, "marking": 7, "reactions": 8, "shortpassing": 10,
           "slidingtackle": 11, "sprintspeed": 6, "stamina": 10, "standingtackle": 8, "interceptions": 12},
    "FB": {"acceleration": 5, "ballcontrol": 7, "crossing": 9, "headingaccuracy": 4, "marking": 8, "reactions": 8, "shortpassing": 7,
           "slidingtackle": 14, "sprintspeed": 7, "stamina": 8, "standingtackle": 11, "interceptions": 12},
    "CB": {"aggression": 7, "ballcontrol": 4, "headingaccuracy": 10, "jumping": 3, "marking": 14, "reactions": 5, "shortpassing": 5,
           "slidingtackle": 17, "sprintspeed": 2, "standingtackle": 10, "strength": 10, "interceptions": 13},
    "DM": {"aggression": 5, "ballcontrol": 10, "longpassing": 10, "marking": 9, "reactions": 7, "shortpassing": 14, "slidingtackle": 5,
           "stamina": 6, "standingtackle": 12, "strength": 4, "interceptions": 14, "vision": 4},
    "WM": {"acceleration": 7, "ballcontrol": 13, "crossing": 10, "dribbling": 15, "finishing": 6, "longpassing": 5, "positioning": 8,
           "reactions": 7, "shortpassing": 11, "sprintspeed": 6, "stamina": 5, "vision": 7},
    "CM": {"ballcontrol": 14, "dribbling": 7, "finishing": 2, "longpassing": 13, "longshots": 4, "positioning": 6, "reactions": 8,
           "shortpassing": 17, "stamina": 6, "standingtackle": 5, "interceptions": 5, "vision": 13},
    "AM": {"acceleration": 4, "agility": 3, "ballcontrol": 15, "dribbling": 13, "finishing": 7, "longpassing": 4, "longshots": 5,
           "positioning": 9, "reactions": 7, "shortpassing": 16, "sprintspeed": 3, "vision": 14},
    "CF": {"acceleration": 5, "ballcontrol": 15, "dribbling": 14, "finishing": 11, "headingaccuracy": 2, "longshots": 4, "positioning": 13,
           "reactions": 9, "shortpassing": 9, "shotpower": 5, "sprintspeed": 5, "vision": 8},
    "W": {"acceleration": 7, "agility": 3, "ballcontrol": 14, "crossing": 9, "dribbling": 16, "finishing": 10, "longshots": 4,
          "positioning": 9, "reactions": 7, "shortpassing": 9, "sprintspeed": 6, "vision": 6},
    "ST": {"acceleration": 4, "ballcontrol": 10, "dribbling": 7, "finishing": 18, "headingaccuracy": 10, "longshots": 3, "positioning": 13,
           "reactions": 8, "shortpassing": 5, "shotpower": 10, "sprintspeed": 5, "strength": 5, "volleys": 2},
}
POS_ROLE = "GK SW WB FB CB CB CB FB WB DM DM DM WM CM CM CM WM AM AM AM CF CF CF W ST ST ST W".split()
POSOVR = "(CASE p.preferredposition1 {} END)".format(" ".join(  # SQL: client rating of row p
    f"WHEN {i} THEN ({' + '.join(f'{w} * p.{k}' for k, w in ROLE_WEIGHTS[r].items())}) / 100" for i, r in enumerate(POS_ROLE)))



def world_market_values():
    """{playerid: current estimated EUR market value}; missing file means use legacy prices."""
    global _WORLD_VALUES, _LEGEND_PEAK_VALUES
    if _WORLD_VALUES is None or _LEGEND_PEAK_VALUES is None:
        try:
            with open(WORLD_MARKET_VALUES, encoding="utf-8") as f:
                data = json.load(f)
            _WORLD_VALUES = {int(k): int(v) for k, v in data.get("player_values", {}).items()}
            _LEGEND_PEAK_VALUES = {int(k): int(v) for k, v in data.get("legend_peak_values", {}).items()}
        except (OSError, ValueError, TypeError):
            _WORLD_VALUES, _LEGEND_PEAK_VALUES = {}, {}
    return _WORLD_VALUES


def legend_peak_values():
    """{playerid: Transfermarkt recorded career peak EUR value} for World Legend cards."""
    world_market_values()  # load both maps from the shared file
    return _LEGEND_PEAK_VALUES


def legend_fo4_prices():
    """{playerid: FO4 base market price in EP} for World Legend cards."""
    global _LEGEND_FO4_VALUES
    if _LEGEND_FO4_VALUES is None:
        try:
            with open(LEGEND_FO4_PRICES, encoding="utf-8") as f:
                data = json.load(f)
            # Only use transferable ICON THE MOMENT prices. Season code 101 entries in
            # this snapshot are untradeable/sentinel listings near 1,000 BP, not market value.
            _LEGEND_FO4_VALUES = {
                int(playerid): int(item["price"])
                for playerid, item in data.get("players", {}).items()
                if item.get("price") and str(item.get("season_code")) == "100"
            }
        except (OSError, ValueError, TypeError, KeyError):
            _LEGEND_FO4_VALUES = {}
    return _LEGEND_FO4_VALUES


def card_ratings(spids):
    """{spid: (client rating, fifadb overallrating)} (cached)."""
    spids = [int(s) for s in spids]
    need = [s for s in set(spids) if s not in _OVR]
    if need:
        with closing(_db()) as c:
            for i in range(0, len(need), 500):
                chunk = need[i:i + 500]
                rows = {s: (pid, o, d) for s, pid, o, d in c.execute(
                    f"SELECT p.spid, p.playerid, {POSOVR}, p.overallrating FROM players p WHERE p.spid IN ({','.join('?' * len(chunk))})", chunk)}
                _PLAYER_IDS.update({s: rows[s][0] for s in chunk if s in rows})
                _OVR.update({s: (rows[s][1], rows[s][2]) if s in rows else (0, 0) for s in chunk})
    return {s: _OVR[s] for s in spids}


def card_ovrs(spids):
    """{spid: rating the client shows}."""
    return {s: r[0] for s, r in card_ratings(spids).items()}


def card_ovr(spid):
    return card_ovrs([spid])[int(spid)]


def grade_of(p):
    """+1..+10. All active cards in FO3 are at least +1."""
    return min(10, max(1, int(p.get("grade") or 1)))


def player_ovr(p):
    """Card rating plus its enhancement boost."""
    return card_ovr(p["spid"]) + GRADE_BOOST[grade_of(p)]


def sig3(x):
    x = int(x)
    return round(x, 2 - len(str(x))) if x >= 1000 else x


def price_formula(ovr):
    """Fallback without gamedata/fa_prices.json: an early least-squares fit of fifaaddict's
    Indonesia-server prices on the client rating: exp(2.53 + 0.1113 * ovr), 65 ~ 17k, 85 ~ 160k."""
    return sig3(max(1000, math.exp(2.53 + 0.1113 * ovr)))


def hidden_missing_miniface_spids():
    """Card IDs in Thailand, China, and Korea without a locally available miniface.

    The Transfer Market search is local to the client; this server-side list keeps
    those cards out of market offers as well. The client search page uses the same
    missing-face list baked into TransferPurchaseTabPage.big.
    """
    nationalities = (155, 167, 188)  # China PR, Korea Republic, Thailand
    with closing(_db()) as c:
        spids = [int(row[0]) for row in c.execute(
            "SELECT spid FROM players WHERE nationality IN (?, ?, ?)", nationalities)]
    roots = (
        os.path.join(ASSETS, "externalAssets", "players", "player"),
        os.path.join(HERE, "assets", "externalAssets", "players", "player"),
    )
    return frozenset(spid for spid in spids
                     if not any(os.path.isfile(os.path.join(root, f"p{spid}.png")) for root in roots))


@functools.lru_cache(maxsize=1)


def on_market(spid):
    """Tradeable card: listed on fifaaddict (season-0 engine cards are not real cards)."""
    fa = fa_prices()
    return int(spid) in fa if fa else int(spid) >= 1000000


@functools.lru_cache(maxsize=None)
def rating_prices():
    """{client rating: EP price at +1}: the median fifaaddict price of all listed cards with that
    rating (ratings with 5+ priced cards), each at least 5% above the rating below; {} without
    fa_prices.json."""
    fa = fa_prices()
    by = {}
    for s, o in card_ovrs([s for s, p in fa.items() if p]).items():
        if o:
            by.setdefault(o, []).append(fa[s])
    med = {o: statistics.median(v) for o, v in by.items() if len(v) >= 5}
    table, top = {}, 0
    for o in range(min(med), max(med) + 1) if med else ():
        top = max(top * 1.05, med.get(o, 0))
        table[o] = top
    return table


def stat_price(ovr):
    """EP price at +1 of a card rated ovr."""
    t = rating_prices()
    if len(t) < 6:
        return price_formula(ovr)
    lo, hi = min(t), max(t)
    if ovr > hi:  # above every listed rating: keep the top five ratings' growth per point
        return t[hi] * (t[hi] / t[hi - 5]) ** ((ovr - hi) / 5)
    return t[max(lo, ovr)]


LEGEND_PRICE_MULTIPLIER = 10
LEGEND_PRICE_FLOOR = 100_000_000


def card_price(spid, grade=1):
    """EP market price. Legend cards use transferable FO4 prices scaled to this server's
    EP economy, with a floor for Legends missing a tradable FO4 listing. Other cards keep
    their existing market-value/OVR pricing. Grade multipliers remain unchanged."""
    spid = int(spid)
    if spid not in _PLAYER_IDS:
        card_ratings([spid])
    playerid = _PLAYER_IDS.get(spid, 0)
    if spid // 1000000 == 93:  # World Legend: ignore untradeable listings and scale to EP
        market_base = legend_fo4_prices().get(playerid, 0)
        peak_base = legend_peak_values().get(playerid, 0)
        base = max(LEGEND_PRICE_FLOOR, max(market_base, peak_base) * LEGEND_PRICE_MULTIPLIER)
    else:
        base = world_market_values().get(playerid)
        base = base or stat_price(card_ovr(spid))
    return sig3(base * GRADE_PRICE[min(10, max(0, int(grade)))])  # +0 = 0 EP


def iid(p):
    """Player instance id: str(spid) for squads made by gen_teams, a hex id for cards bought,
    opened or upgraded here (so two cards of one player can coexist, e.g. for upgrades)."""
    return str(p.get("_id") or p["spid"])


def new_iid():
    return os.urandom(12).hex()  # Mongo-style


def duplicate_lineups_allowed():
    import duplicate_player_settings
    return duplicate_player_settings.get_allowed()


def has_duplicate_players(positions):
    """Check player identity, not card instance or season, so alternate seasons collide."""
    spids = [int(p.get('spid') or 0) for p in positions]
    if len(spids) < 2:
        return False
    card_ratings(spids)  # populates canonical SPID -> playerid mapping from fifadb
    identities = [_PLAYER_IDS.get(spid, spid % 1_000_000) for spid in spids]
    return len(identities) != len(set(identities))


# Levels (user: "akun dan pemain belum bisa dilevel up"). Both levelinfo = {level, exp within
# the level, to = exp the level needs}. Players: the client adds level - 1 to every attribute
# (PlayerListUtil.fillPlayerStaticData: totalBonus = level - 1 + grade boost; fifaaddict lists
# LV.1-23); EXPPLAYER {level: {requiredplayerexp}}, max level = rows - 1 (getMaxLevel). Coach:
# EXPCOACH {level: {totalexp}} = exp to finish levels 1..level (TeamManagementCoachExpScreen),
# max from STATICVALUES/max_coachlevel. The original tables were server data; curves made up.
PLAYER_MAX_LEVEL, COACH_MAX_LEVEL = 23, 100
EXP_WIN, EXP_DRAW, EXP_LOSS = (300, 150), (200, 100), (100, 60)  # (coach, each player who played)


def player_need(level):
    return 100 * level


def coach_need(level):
    return 500 + 100 * (level - 1)


EXPPLAYER = {str(lv): {"requiredplayerexp": player_need(lv)} for lv in range(1, PLAYER_MAX_LEVEL + 2)}
EXPCOACH = {str(lv): {"totalexp": sum(coach_need(k) for k in range(1, lv + 1))} for lv in range(1, COACH_MAX_LEVEL + 1)}


def make_levelinfo(info, need, top):
    level = min(top, max(1, int((info or {}).get("level") or 1)))
    return {"level": level, "exp": 0 if level >= top else int((info or {}).get("exp") or 0), "to": need(level)}


def player_levelinfo(p):
    return make_levelinfo(p.get("levelinfo"), player_need, PLAYER_MAX_LEVEL)


def coach_levelinfo(coach):
    return make_levelinfo(coach.get("levelinfo"), coach_need, COACH_MAX_LEVEL)


def gain_exp(owner, n, need, top):
    """Add n exp to owner["levelinfo"] (levels up, stops at top); (prev, current) levelinfo."""
    prev = make_levelinfo(owner.get("levelinfo"), need, top)
    level, exp = prev["level"], prev["exp"] + n
    while level < top and exp >= need(level):
        exp -= need(level)
        level += 1
    owner["levelinfo"] = {"level": level, "exp": 0 if level >= top else exp}
    return prev, make_levelinfo(owner["levelinfo"], need, top)


# Tim > Formasi combobox = coach team.formations (SquadScreenHandler.initializeFormationComboBox:
# entry 0 is shown with the club's name; picking one runs changeFormation: positions[i] -> squad
# slot i, offsets[i] -> starting x/y, then SquadUtil.getBestSquadPositions). Entries come from the
# client's own formations table (data_0.big data/db/fifa_ng_db.db): teamid 1 holds the 33 common
# shapes (their sweeper variants left out, like FORMATIONS_COMMON_WITHOUT_SWEEPER), other teams
# their own. That table numbers positions without SW: client Enums.PLAYERPOSITION = code + 1 (GK 0).
# GAME directory is auto-detected above
FORMATIONS_CACHE = os.path.join(HERE, "gamedata", "formations.json")
_FORMATIONS = None


def formation_table():
    """{"common": [entry], "teams": {teamid: entry}}; entry = {formationid, formationname,
    positions, offsets}. Read once from the client, then cached in gamedata/formations.json."""
    global _FORMATIONS
    if _FORMATIONS is None:
        try:
            with open(FORMATIONS_CACHE) as f:
                _FORMATIONS = json.load(f)
        except OSError:
            _FORMATIONS = {"common": [], "teams": {}}
            try:
                sys.path.insert(0, os.path.join(HERE, "tools", "re"))
                import bigdir, eadb
                big = os.path.join(GAME, "data", "data_0.big")
                rows = eadb.rows(bigdir.read(big, "data/db/fifa_ng_db.db"),
                                 bigdir.read(big, "data/db/fifa_ng_db-meta.xml").decode("utf-8", "replace"), "formations")
            except (OSError, KeyError, ImportError) as e:
                print("formations unavailable:", e, flush=True)
                return _FORMATIONS
            entry = lambda r: {"formationid": r["formationid"], "formationname": r["formationname"],
                               "positions": [r[f"position{i}"] + (r[f"position{i}"] > 0) for i in range(11)],
                               "offsets": [{"x": round(r[f"offset{i}x"], 4), "y": round(r[f"offset{i}y"], 4)} for i in range(11)]}
            _FORMATIONS["common"] = sorted((entry(r) for r in rows if r["teamid"] == 1 and not r["issweeper"]),
                                           key=lambda e: e["formationname"])
            _FORMATIONS["teams"] = {str(r["teamid"]): entry(r) for r in rows if r["teamid"] != 1 and not r["issweeper"]}
            os.makedirs(os.path.dirname(FORMATIONS_CACHE), exist_ok=True)
            with open(FORMATIONS_CACHE, "w") as f:
                json.dump(_FORMATIONS, f)
    return _FORMATIONS


def team_formations(teamid):
    """The club's own formation first (else 4-4-2(1), the shape of the gen_teams XI), then the
    common ones."""
    t = formation_table()
    own = t["teams"].get(str(teamid)) or next((e for e in t["common"] if e["formationname"] == "4-4-2(1)"), None)
    return ([own] if own else []) + [e for e in t["common"] if own is None or e["formationid"] != own["formationid"]]


def fix_formation(team, teamid):
    """A stored team built by gen_teams carries formations = [its 4-4-2, id 1] and squad
    .formation 1, which is 3-4-1-2 in the client's table: point it at the club's own entry."""
    if team.pop("formations", None) is not None and team["squad"].get("formation") in (None, 1):
        forms = team_formations(teamid)
        if forms:
            team["squad"]["formation"] = forms[0]["formationid"]


# Team color (user: "belum ada bonus stats pada saat memenuhi syarat tim/negara/ sesion").
# TeamColorController (data/ui/libs/Module.big) runs only with features.teamcolor
# (responses/account_base.json) and GET /static/info/teamcolor {teamColors, teamColorSpecials}.
# A color: {teamcolorid, type club|nation|special|grade|feature, displaytype, name, description,
# teamid, boostgroups [{count_min, count_max, boosts [{type "ovr" = every attribute, amount}]}]}.
# Mini colors keep type=feature for independent boosts and displaytype=special for grouping with
# player categories. The client filter must use displaytype while boost application uses type.
# The server picks colors the starting XI meets: team.teamcolors [{teamcolorid, boostindex,
# teamid}] in coach/me and the user's match side. The client applies one best base (club/nation/
# special) color per player, plus one grade color and one feature color independently.
# Built-in TEAMCOLOR_ATTRIBUTES_TEAM ids map to localized names in the client.
# Each club value is (player database teamid, display name, (attribute, amount) bonuses).
TC_CLUB, TC_NATION, TC_CARD, TC_SEASON = -1, -2, -3, 50000
TC_SEASON_GROUPS = [{"count_min": lo, "count_max": hi, "boosts": [{"type": "ovr", "amount": amount}]} for lo, hi, amount in ((5, 6, 1), (7, 8, 2), (9, 11, 3))]

def tc_seasons():
    return (("07", (7,)),) + tuple((label, codes) for label, codes, _ in FA_SEASONS)

def tc_season_of(code):
    return next((TC_SEASON + codes[0] for _, codes in tc_seasons() if code in codes), None)
TC_CLUB_COLORS = {
    5: (11, "Manchester United", (("crossing", 2), ("standingtackle", 3))),
    1: (243, "Real Madrid", (("dribbling", 2), ("shortpassing", 1))),
    7: (5, "Chelsea", (("strength", 2), ("standingtackle", 1))),
    2: (241, "Barcelona", (("shortpassing", 2), ("vision", 1))),
    11: (21, "Bayern Munich", (("longshots", 2), ("ballcontrol", 1))),
    6: (1, "Arsenal", (("sprintspeed", 2), ("positioning", 1))),
    12: (47, "AC Milan", (("marking", 2), ("interceptions", 1))),
    13: (45, "Juventus", (("stamina", 2), ("aggression", 1))),
    3: (9, "Liverpool", (("reactions", 2), ("sprintspeed", 1))),
    4: (10, "Manchester City", (("longpassing", 2), ("ballcontrol", 1))),
    43: (240, "Atletico Madrid", (("aggression", 2), ("stamina", 1))),
    14: (44, "Inter Milan", (("reactions", 2), ("balance", 1))),
    9: (22, "Dortmund", (("acceleration", 2), ("reactions", 1))),
    15: (73, "Paris Saint-Germain", (("ballcontrol", 2), ("strength", 1))),
    8: (18, "Tottenham Hotspur", (("acceleration", 2), ("dribbling", 1))),
}
TC_CLUB_COLORS_BY_TEAM = {teamid: (colorid, name, boosts)
                          for colorid, (teamid, name, boosts) in TC_CLUB_COLORS.items()}
# nationality code: (localized color ID, display name, boosts at 6-10, boosts at 11)
TC_NATION_COLORS = {
    21: (102, "Germany", (("positioning", 2), ("gkreflexes", 3)), (("positioning", 3), ("gkreflexes", 4))),
    45: (101, "Spain", (("shortpassing", 2), ("ballcontrol", 3)), (("shortpassing", 3), ("ballcontrol", 4))),
    54: (103, "Brazil", (("dribbling", 2), ("agility", 3)), (("dribbling", 3), ("agility", 4))),
    52: (105, "Argentina", (("balance", 2), ("strength", 1)), (("balance", 2), ("strength", 1))),
    27: (30001, "Italy", (("standingtackle", 2), ("aggression", 3)), (("standingtackle", 3), ("aggression", 4))),
    18: (113, "France", (("stamina", 2), ("strength", 3)), (("stamina", 3), ("strength", 4))),
    14: (109, "England", (("crossing", 2), ("headingaccuracy", 3)), (("crossing", 3), ("headingaccuracy", 4))),
    7: (110, "Belgium", (("ballcontrol", 2), ("balance", 2), ("gkreflexes", 3)),
        (("ballcontrol", 2), ("balance", 2), ("gkreflexes", 3))),
    34: (112, "Netherlands", (("sprintspeed", 2), ("shotpower", 3)), (("sprintspeed", 3), ("shotpower", 4))),
    38: (104, "Portugal", (("dribbling", 2), ("reactions", 3)), (("dribbling", 3), ("reactions", 4))),
    60: (106, "Uruguay", (("aggression", 2), ("acceleration", 3)), (("aggression", 3), ("acceleration", 4))),
    192: (30007, "Vietnam", (), (("stamina", 3),)),
}
TC_NATION_OVR_BONUSES = {192: (1, 2)}  # Vietnam: +1 at 6-10, +2 at 11
TC_NATION_COLORS_BY_NATION = {nation: (colorid, name, boosts6, boosts11)
                              for nation, (colorid, name, boosts6, boosts11) in TC_NATION_COLORS.items()}

# Card-category colors, grouped and displayed before enhancement-grade colors.
TC_PLAYER_CATEGORY_COLORS = {
    "best": (41, "TEAMCOLOR_ATTRIBUTES_TEAM41", "Top 100 cầu thủ các mùa thường", ((6, 11, 1),)),
    "premium": (30002, "TEAMCOLOR_ATTRIBUTES_TEAM40002", "Top 100 mùa LP, CP và CC", ((6, 11, 2),)),
    "special": (30003, "SPECIAL PLAYER", "Thẻ WL, WB/XI, UL và VNL",
                ((3, 5, 2), (6, 10, 3), (11, 11, 5))),
}
TC_PREMIUM_CARD_SEASONS = {
    22, 23, 24,  # CP, LP, CC
    31, 33,      # CEL
    40,          # NHD
    49,          # TCO92
    55,          # MUA
    95,          # KL02
    27, 28, 30, 48, 51, 61, 71, 89, 91, 93
}
TC_SPECIAL_CARD_SEASONS = {27, 28, 30, 48, 51, 61, 71, 89, 91, 93}  # EL, UB, UL, MUL, TL, VL, ML, CL, WB, WL

# Duo/trio special colors; membership uses stable player IDs from fifadb.
TC_MINI_TEAM_COLORS = {
    "msn": (30008, "Bộ ba MSN", "Messi, Suarez và Neymar",
            {158023, 176580, 190871}, ((3, 3, (("ovr", 2), ("ballcontrol", 2), ("dribbling", 1))),)),
    "bbc": (30009, "Bộ ba BBC", "Bale, Benzema và Cristiano Ronaldo",
            {173731, 165153, 20801}, ((3, 3, (("ovr", 2), ("sprintspeed", 2), ("finishing", 1))),)),
    "milan_trio": (30010, "Mũi đinh ba Milan", "Kaka, Shevchenko và Crespo",
                   {138449, 13128, 7512}, ((3, 3, (("ovr", 1), ("positioning", 2), ("acceleration", 1))),)),
    "torres_gerrard": (30011, "Cặp đôi Hoàn hảo", "Torres và Gerrard",
                       {49369, 13743}, ((2, 2, (("ovr", 1), ("positioning", 2), ("longpassing", 2))),)),
    "rooney_ronaldo": (30012, "Song sát Quỷ đỏ", "Rooney và Cristiano Ronaldo",
                       {54050, 20801}, ((2, 2, (("ovr", 2), ("sprintspeed", 2), ("shotpower", 1))),)),
    "etoo_ronaldinho": (30013, "Vũ điệu và Tốc độ", "Ronaldinho và Eto",
                        {9676, 28130}, ((2, 2, (("ovr", 1), ("acceleration", 2), ("dribbling", 2))),)),
    "drogba_lampard": (30014, "Trục dọc Stamford Bridge", "Drogba và Lampard",
                        {31432, 5471}, ((2, 2, (("ovr", 1), ("strength", 2), ("longshots", 2))),)),
    "milan_dutch_trio": (30015, "Bộ ba Hà Lan bay", "Gullit, van Basten và Rijkaard",
                         {214100, 192181, 214098}, ((3, 3, (("ovr", 2), ("strength", 2), ("finishing", 2))),)),
    "arsenal_invincibles": (30016, "Trục dọc bất bại", "Henry, Vieira và Campbell",
                            {1625, 1419, 388}, ((3, 3, (("ovr", 1), ("sprintspeed", 2), ("aggression", 2))),)),
    "barcelona_tikitaka": (30017, "Trục giữa Tiki-Taka", "Xavi, Iniesta và Busquets",
                           {10535, 41, 189511}, ((3, 3, (("ovr", 1), ("shortpassing", 2), ("ballcontrol", 2))),)),
    "manutd_wall": (30018, "Bức tường Old Trafford", "Ferdinand, Vidic và van der Sar",
                    {7289, 140601, 51539}, ((3, 3, (("ovr", 1), ("standingtackle", 2), ("slidingtackle", 2),
                                                     ("gkdiving", 2), ("gkreflexes", 2))),)),
    "juventus_bbc": (30020, "Hàng thủ BBC Juventus", "Buffon, Bonucci, Barzagli và Chiellini",
                     {1179, 184344, 137186, 138956}, ((4, 4, (("ovr", 1), ("marking", 2), ("standingtackle", 2), ("gkdiving", 2))),)),
    "chelsea_spine": (30021, "Xương sống The Blues", "Cech, Terry, Lampard và Drogba",
                      {48717, 13732, 5471, 31432}, ((4, 4, (("ovr", 2), ("headingaccuracy", 2), ("shotpower", 2))),)),
    "bayern_robbery": (30022, "Đôi cánh Robbery", "Robben và Ribery",
                       {9014, 156616}, ((2, 2, (("ovr", 1), ("acceleration", 2), ("dribbling", 2), ("curve", 2))),)),
    "inter_treble": (30023, "Cú ăn 3 Vĩ đại Inter", "Milito, Sneijder, Zanetti và Cesar",
                     {53050, 18507, 9600, 53302}, ((4, 4, (("ovr", 2), ("stamina", 2), ("positioning", 2))),)),
    "ronaldo_rooney_tevez": (30024, "Mũi đinh ba R-R-T", "Cristiano Ronaldo, Wayne Rooney và Carlos Tevez",
                            {20801, 54050, 143001}, ((3, 3, (("ovr", 2), ("sprintspeed", 2), ("shotpower", 2))),)),
    "galacticos": (30019, "Dải ngân hà Galacticos", "Ronaldo, Beckham, Figo, Zidane, Carlos và Raul",
                   {20801, 250, 1088, 1397, 1040, 240}, ((3, 6, (("ovr", 2), ("dribbling", 2), ("shortpassing", 2))),)),
}

# Enhancement-grade colors use the client-native IDs that have matching icons.
TC_GRADE_COLORS = {
    "copper": (24, "TEAMCOLOR_ATTRIBUTES_EVOLVE24", "Thẻ +2 đến +4", ((6, 11, 1),)),
    "silver": (10011, "TEAMCOLOR_ATTRIBUTES_EVOLVE10011", "Thẻ +5 đến +7", ((6, 10, 1), (11, 11, 2))),
    "gold": (810, "TEAMCOLOR_ATTRIBUTES_EVOLVE810", "Thẻ +8 đến +10", ((6, 10, 2), (11, 11, 3))),
}
TC_GRADE_RANGES = {"copper": (2, 4), "silver": (5, 7), "gold": (8, 10)}



TC_CARD_COLOR_TIERS_BY_ID = {
    colorid: (colorid, tiers)
    for colors in (TC_PLAYER_CATEGORY_COLORS, TC_GRADE_COLORS)
    for colorid, _, _, tiers in colors.values()
}
TC_CARD_COLOR_TIERS_BY_ID.update({
    colorid: (colorid, tuple((lo, hi, boosts) for lo, hi, boosts in tiers))
    for colorid, _, _, _, tiers in TC_MINI_TEAM_COLORS.values()
})
def _tc_groups(*tiers):
    return [{"count_min": lo, "count_max": hi, "boosts": [{"type": "ovr", "amount": n}]} for lo, hi, n in tiers]


def _tc_club_groups(boosts):
    specific = [{"type": stat, "amount": amount} for stat, amount in boosts]
    return [{"count_min": 6, "count_max": 10, "boosts": specific},
            {"count_min": 11, "count_max": 11,
             "boosts": [{"type": "ovr", "amount": 1}, *specific]}]


def _tc_nation_groups(boosts6, boosts11, ovr6=0, ovr11=1):
    return [{"count_min": 6, "count_max": 10,
             "boosts": ([{"type": "ovr", "amount": ovr6}] if ovr6 else []) +
                       [{"type": stat, "amount": amount} for stat, amount in boosts6]},
            {"count_min": 11, "count_max": 11,
             "boosts": [{"type": "ovr", "amount": ovr11},
                        *({"type": stat, "amount": amount} for stat, amount in boosts11)]}]


def tc_grade_card_sets(players):
    """Group owned card IDs by enhancement grade; omit IDs owned at conflicting grades."""
    grades_by_spid = {}
    for player in players:
        grade = grade_of(player)
        category = next((key for key, (lo, hi) in TC_GRADE_RANGES.items()
                         if lo <= grade <= hi), None)
        if category:
            grades_by_spid.setdefault(int(player["spid"]), set()).add(category)

    result = {key: set() for key in TC_GRADE_COLORS}
    for spid, categories in grades_by_spid.items():
        if len(categories) == 1:
            result[next(iter(categories))].add(spid)
    return result


@functools.lru_cache(maxsize=1)
def tc_player_category_card_sets():
    """Card IDs in top-100 ordinary/premium groups and the special legend group."""
    with closing(_db()) as c:
        rows = c.execute("SELECT spid, playerid, overallrating FROM players WHERE spid >= 1000000").fetchall()
    by_season = {}
    for spid, playerid, rating in rows:
        by_season.setdefault(int(spid) // 1000000, []).append((int(spid), int(playerid), int(rating)))

    result = {key: set() for key in TC_PLAYER_CATEGORY_COLORS}
    for season, cards in by_season.items():
        if season in TC_SPECIAL_CARD_SEASONS:
            result["special"].update(spid for spid, _, _ in cards)
            continue
        category = "premium" if season in TC_PREMIUM_CARD_SEASONS else "best"
        ranked = sorted(cards, key=lambda card: (-card[2], card[0]))
        seen_players = set()
        for spid, playerid, _ in ranked:
            if playerid in seen_players:
                continue
            seen_players.add(playerid)
            if len(seen_players) > 100:
                break
            result[category].add(spid)
    return result


def _tc_mini_groups(*tiers):
    return [{"count_min": lo, "count_max": hi,
             "boosts": [{"type": stat, "amount": amount} for stat, amount in boosts]}
            for lo, hi, boosts in tiers]


def tc_colors():
    clubs = [{"teamcolorid": colorid, "type": "club", "teamid": teamid,
              "name": name,
              "description": f"Câu lạc bộ {name}", "boostgroups": _tc_club_groups(boosts)}
             for colorid, (teamid, name, boosts) in TC_CLUB_COLORS.items()]
    nations = [{"teamcolorid": colorid, "type": "nation", "teamid": nationality,
                "name": name,
                "description": f"Đội tuyển {name}",
                "boostgroups": _tc_nation_groups(
                    boosts6, boosts11, *TC_NATION_OVR_BONUSES.get(nationality, (0, 1)))}
               for nationality, (colorid, name, boosts6, boosts11) in TC_NATION_COLORS.items()]
    categories = [{"teamcolorid": colorid, "type": "special", "teamid": str(colorid), "name": name,
                   "description": description, "boostgroups": _tc_groups(*tiers)}
                  for colorid, name, description, tiers in TC_PLAYER_CATEGORY_COLORS.values()]
    grades = [{"teamcolorid": colorid, "type": "grade", "teamid": str(colorid), "name": name,
               "description": description, "grade": TC_GRADE_RANGES[key][0],
               "boostgroups": _tc_groups(*tiers)}
              for key, (colorid, name, description, tiers) in TC_GRADE_COLORS.items()]
    mini = [{"teamcolorid": colorid, "type": "feature", "displaytype": "special",
             "teamid": str(colorid), "name": name, "description": description,
             "boostgroups": _tc_mini_groups(*tiers)}
            for colorid, name, description, _, tiers in TC_MINI_TEAM_COLORS.values()]
    seasons = [{"teamcolorid": TC_SEASON + codes[0], "type": "special", "teamid": str(TC_SEASON + codes[0]),
                "name": f"Season {label}", "description": f"Cầu thủ mùa giải {label}", "boostgroups": TC_SEASON_GROUPS}
               for label, codes in tc_seasons()]
    return clubs + nations + categories + grades + mini + seasons


def ep_teamcolor(query, body):
    """GET /static/info/teamcolor (once per login). Grade colors list owned card IDs in each
    enhancement band; newly acquired or upgraded cards are included on the next login."""
    st = _state()
    players = st["teams"].get(str(_default_slot(st)), {}).get("players", [])
    specials = {str(c["teamid"]): [] for c in tc_colors() if c["type"] in ("special", "feature")}
    for color in tc_colors():
        if color["teamcolorid"] >= TC_SEASON:
            specials.setdefault(str(color["teamcolorid"]), [])
    for spid in sorted(owned := {int(player["spid"]) for player in players}):
        season_id = tc_season_of(spid // 1000000)
        if season_id:
            specials.setdefault(str(season_id), []).append(spid)
    for key, spids in tc_grade_card_sets(players).items():
        colorid = TC_GRADE_COLORS[key][0]
        specials[str(colorid)] = sorted(spids)
    for key, spids in tc_player_category_card_sets().items():
        colorid = TC_PLAYER_CATEGORY_COLORS[key][0]
        specials[str(colorid)] = sorted(spids & owned)
    cards, _ = tc_static()
    for colorid, _, _, player_ids, _ in TC_MINI_TEAM_COLORS.values():
        specials[str(colorid)] = sorted(spid for spid in owned
                                        if cards.get(spid, (None,))[0] in player_ids)
    return _json({"teamColors": tc_colors(), "teamColorSpecials": specials})


@functools.lru_cache(maxsize=None)
def tc_static():
    """({spid: (playerid, teamid, nationality)}, {playerid: {former club ids}}) from fifadb."""
    with closing(_db()) as c:
        cards = {s: (p, t, n) for s, p, t, n in c.execute("SELECT spid, playerid, teamid, nationality FROM players")}
        hist = {}
        for p, t in c.execute("SELECT playerid, teamid FROM playerhistory"):
            hist.setdefault(p, set()).add(t)
    return cards, hist


def active_teamcolors(team):
    """team.teamcolors for eligible base, grade, and feature colors in the starting XI."""
    cards, hist = tc_static()
    counts, seen = {}, set()
    grade_card_sets = tc_grade_card_sets(team.get("players", []))
    player_category_card_sets = tc_player_category_card_sets()
    mini_player_ids = {colorid: player_ids
                       for colorid, _, _, player_ids, _ in TC_MINI_TEAM_COLORS.values()}
    instances = {iid(player): player for player in team.get("players", [])}
    for p in team["squad"]["positions"]:
        spid = int(p["spid"])
        c = cards.get(spid) if p.get("pos", POS_SUB) < POS_SUB else None
        if c is None or c[0] in seen:
            continue
        seen.add(c[0])
        pid, teamid, nation = c
        keys = [(TC_CLUB, clubid) for clubid in {teamid} | hist.get(pid, set())]
        keys.append((TC_NATION, nation))
        instance = instances.get(str(p.get("id") or spid))
        grade = grade_of(instance) if instance else 0
        category = next((key for key, (lo, hi) in TC_GRADE_RANGES.items()
                         if lo <= grade <= hi), None)
        if category and spid in grade_card_sets[category]:
            keys.append((TC_CARD, TC_GRADE_COLORS[category][0]))
        keys += [(TC_CARD, TC_PLAYER_CATEGORY_COLORS[key][0])
                 for key, spids in player_category_card_sets.items() if spid in spids]
        keys += [(TC_CARD, colorid) for colorid, player_ids in mini_player_ids.items() if pid in player_ids]
        season_id = tc_season_of(spid // 1000000)
        if season_id:
            keys.append((TC_SEASON, season_id))
        for k in keys:
            counts[k] = counts.get(k, 0) + 1
    out = []
    for (cid, teamid), n in sorted(counts.items()):
        if cid == TC_CLUB:
            club = TC_CLUB_COLORS_BY_TEAM.get(teamid)
            if club and n >= 6:
                colorid, _, _ = club
                out.append({"teamcolorid": colorid, "boostindex": 1 if n >= 11 else 0, "teamid": teamid})
            continue
        if cid == TC_NATION:
            country = TC_NATION_COLORS_BY_NATION.get(teamid)
            if country and n >= 6:
                colorid, _, _, _ = country
                out.append({"teamcolorid": colorid, "boostindex": 1 if n >= 11 else 0, "teamid": teamid})
            continue
        if cid == TC_SEASON:
            i = next((i for i, group in enumerate(TC_SEASON_GROUPS)
                      if group["count_min"] <= n <= group["count_max"]), None)
            if i is not None:
                out.append({"teamcolorid": teamid, "boostindex": i, "teamid": str(teamid)})
            continue
        if cid == TC_CARD:
            color = TC_CARD_COLOR_TIERS_BY_ID.get(teamid)
            if color:
                colorid, tiers = color
                i = next((i for i, (lo, hi, _) in enumerate(tiers) if lo <= n <= hi), None)
                if i is not None:
                    out.append({"teamcolorid": colorid, "boostindex": i, "teamid": str(teamid)})
            continue
    return out


def teamcolor_player_boosts(team):
    """Selected client-equivalent Team Color boosts by starting card and assigned position."""
    active = active_teamcolors(team)
    if not active:
        return {}
    colors = {color["teamcolorid"]: color for color in tc_colors()}
    cards, history = tc_static()
    grade_sets = tc_grade_card_sets(team.get("players", []))
    category_sets = tc_player_category_card_sets()
    category_colors = {TC_PLAYER_CATEGORY_COLORS[key][0]: spids for key, spids in category_sets.items()}
    mini_colors = {colorid: player_ids
                   for colorid, _, _, player_ids, _ in TC_MINI_TEAM_COLORS.values()}
    instances = {iid(player): player for player in team.get("players", [])}
    selected = {}

    for position in team["squad"]["positions"]:
        pos = int(position.get("pos", POS_SUB))
        if not 0 <= pos < POS_SUB:
            continue
        spid = int(position["spid"])
        card = cards.get(spid)
        if card is None:
            continue
        pid, clubid, nationality = card
        instance = instances.get(str(position.get("id") or spid))
        grade = grade_of(instance) if instance else 0
        grade_category = next((key for key, (lo, hi) in TC_GRADE_RANGES.items()
                               if lo <= grade <= hi), None)
        best = {}
        for item in active:
            color = colors.get(item["teamcolorid"])
            if color is None:
                continue
            kind, colorid = color["type"], color["teamcolorid"]
            if kind == "club":
                matches = item["teamid"] in ({clubid} | history.get(pid, set()))
                bucket = "base"
            elif kind == "nation":
                matches = item["teamid"] == nationality
                bucket = "base"
            elif kind == "grade":
                matches = (grade_category is not None and
                           colorid == TC_GRADE_COLORS[grade_category][0] and
                           spid in grade_sets[grade_category])
                bucket = "grade"
            elif kind == "special" and colorid in mini_colors:
                matches = pid in mini_colors[colorid]
                bucket = "feature"
            elif kind == "special":
                matches = spid in category_colors.get(colorid, set())
                bucket = "base"
            elif kind == "feature":
                matches = pid in mini_colors.get(colorid, set())
                bucket = "feature"
            else:
                matches, bucket = False, "base"
            if not matches:
                continue
            boosts = color["boostgroups"][item["boostindex"]]["boosts"]
            role = POS_ROLE[pos].lower()
            score = sum(b["amount"] * (33 if b["type"] == "ovr" else 1)
                        for b in boosts if b.get("target") is None or b["target"] == role)
            if bucket not in best or score > best[bucket][0]:
                best[bucket] = (score, boosts)
        selected[(spid, pos)] = [boost for _, boosts in best.values() for boost in boosts]
    return selected


def repair_squad(team):
    """Restore missing starters and keep all squad references on owned instances.

    Older squad snapshots can refer to a card by its SPID after that card has been
    replaced with an owned instance id. When that copy is later consumed by an
    upgrade, resolve the stale reference to another owned copy of the same SPID
    before treating the squad slot as empty.
    """
    sq = team["squad"]
    cards = {iid(p): p for p in team["players"]}
    by_spid = {}
    for card in team["players"]:
        by_spid.setdefault(str(card["spid"]), []).append(card)
    used, slots, positions = set(), set(), []
    for entry in sq.get("positions", []):
        pid = str(entry.get("id") or entry.get("spid"))
        pos = entry.get("pos", POS_SUB)
        if not 0 <= pos <= POS_SUB:
            continue
        card = cards.get(pid)
        if card is None or pid in used:
            spid = str(entry.get("spid") or (pid if pid.isdigit() else ""))
            candidates = [p for p in by_spid.get(spid, []) if iid(p) not in used]
            if not candidates:
                continue
            card = max(candidates, key=lambda p: (not p.get("team2"), grade_of(p), player_ovr(p)))
            pid = iid(card)
        if pos < POS_SUB and (pos in slots or len(slots) >= 11):
            continue
        positions.append(dict(entry, id=pid, spid=card["spid"]))
        used.add(pid)
        if pos < POS_SUB:
            slots.add(pos)
    # Preserve every valid starter. Fill vacancies from the existing bench/reserves.
    starters = [p for p in positions if p["pos"] < POS_SUB]
    starting_ids = {p["id"] for p in starters}
    for pos, x, y in gen_teams.FORMATION:
        if len(starters) >= 11:
            break
        if pos in slots:
            continue
        candidates = [p for pid, p in cards.items() if pid not in starting_ids and not p.get("team2")]
        if not candidates:
            break
        chosen = max(candidates, key=player_ovr)
        pid = iid(chosen)
        starters.append({"id": pid, "spid": chosen["spid"], "pos": pos,
                         "starting": [1, 1, x, y]})
        slots.add(pos)
        starting_ids.add(pid)
    bench = [p for p in positions if p["pos"] == POS_SUB and p["id"] not in starting_ids][:7]
    selected = starting_ids | {p["id"] for p in bench}
    for p in sorted(cards.values(), key=player_ovr, reverse=True):
        if len(bench) >= 7:
            break
        if iid(p) not in selected and not p.get("team2"):
            bench.append({"id": iid(p), "spid": p["spid"], "pos": POS_SUB})
            selected.add(iid(p))
    sq["positions"] = starters + bench
    if str(sq.get("captain")) not in starting_ids:
        sq["captain"] = next((p["id"] for p in starters if p["pos"] != 0), "")
    else:
        sq["captain"] = str(sq["captain"])
    fallback = [p["id"] for p in starters if p["pos"] != 0]
    kickers = sq.get("kickers", [])
    sq["kickers"] = [str(kickers[i]) if i < len(kickers) and str(kickers[i]) in starting_ids
                     else (fallback[i % len(fallback)] if fallback else "") for i in range(5)]


def squad_presets(team):
    """Native SquadUtil needs team.teamsquadlist.preset, even on opening the first preset."""
    import copy
    stored = team.get('teamsquadlist')
    entries = stored.get('preset', []) if isinstance(stored, dict) else []
    if not isinstance(entries, list):
        entries = []
    active = team.get('presetidx', 0)
    active = active if type(active) is int and 0 <= active < 3 else 0
    presets = []
    for index in range(3):
        candidate = entries[index] if index < len(entries) and isinstance(entries[index], dict) else team['squad']
        temp = {'players': team['players'], 'squad': copy.deepcopy(candidate)}
        repair_squad(temp)
        presets.append(temp['squad'])
    presets[active] = copy.deepcopy(team['squad'])
    team['presetidx'] = active
    team['teamsquadlist'] = {'preset': presets}
    return team['teamsquadlist']


PLAYER_CONTRACT_MAX = 40  # a player card's contract length in matches (the recontract cap)


def _player_life(p):
    """Contract matches left, for display and for the per-match decrement: the
    stored value clamped to the cap; a card never stamped (life None -- every
    stored card before 2026-11-12) counts as a fresh full contract."""
    v = p.get("life")
    if v is None:
        return PLAYER_CONTRACT_MAX
    try:
        return max(0, min(PLAYER_CONTRACT_MAX, int(v)))
    except (TypeError, ValueError):
        return PLAYER_CONTRACT_MAX


def owned_team(raw):
    """A stored team ({players:[{spid,...}], squad:{positions,captain,kickers,...}}) in the
    shape the client expects for a club it can show and play with (coach.team, match side.team)."""
    import copy
    team = copy.deepcopy(raw)
    repair_squad(team)
    # An owned club refers to player *instances*: PlayerListUtil.getBestEleven maps
    # squad.positions[].id -> players[]._id (spid alone gives an empty XI -> null -> the
    # MenuScreen constructor throws = black FREEROAM).
    # CoachProxy.expandTeam: teamvalue = sum(price) of players with state "first" and grade > 0
    # getBestEleven matches positions[].id on players[]._id, attachSquadInfo and
    # generateFormatedPlayerList (Tim > Formasi / Peran pemain) on players[].id: send both.
    # Instance defaults: life = remaining contract matches (SquadUtil.needToReject drops <= 0),
    # levelinfo.level (FirstTeamInfo.updateAvgInfo average level). price = EP market value.
    # state "first" = Tim inti; no state = Tim Cadangan (Enums.getPlayerState: undefined ->
    # SECONDTEAM), set by /player/<id>/move/secondteam (stored as team2); Tukar Scout only
    # lists Tim Cadangan cards.
    team["players"] = [{**{k: v for k, v in p.items() if k != "team2"},
                        **({} if p.get("team2") else {"state": "first"}), "grade": grade_of(p), "life": _player_life(p),
                        "levelinfo": player_levelinfo(p), "_id": iid(p), "id": iid(p), "price": card_price(p["spid"], grade_of(p))}
                       for p in team["players"]]
    sq = dict(team["squad"])
    sq["positions"] = [dict(p, id=str(p.get("id") or p["spid"])) for p in sq["positions"]]
    if not any(p["pos"] == POS_SUB for p in sq["positions"]):
        # 11 starters + 7 subs (Enums.PLAYERPOSITION "SUB"); without a bench the squad screen
        # says "Tidak cukup pemain". Best remaining players by rating.
        used = {p["id"] for p in sq["positions"]}
        bench = sorted((p for p in team["players"] if p["id"] not in used), key=lambda p: -player_ovr(p))
        sq["positions"] += [{"spid": p["spid"], "pos": POS_SUB, "id": p["id"]} for p in bench[:7]]
    sq["captain"] = str(sq["captain"]) if sq.get("captain") else ""
    sq["kickers"] = [str(k) for k in sq.get("kickers", [])]
    team["squad"] = sq
    team.update({"staffs": team.get("staffs", []),
                 "staffroles": team.get("staffroles") or [
                     {"roleidx": 1, "slotidx": i, "open": True, "staffid": None}
                     for i in range(1, 8)],
                 "staffroleidx": team.get("staffroleidx") or 1,
                 "maxstaff": team.get("maxstaff") or 30, "staffsmc": team.get("staffsmc") or 0})
    # PlayerListUtil.generateCaptainFromCoachInfo (Tim > Formasi) reads these at team level
    team.update({"captain": sq["captain"], "kickers": sq["kickers"]})
    squad_presets(team)
    staff_service.normalize_team(team)
    # Staff-study Return UI indexes the effect array directly (0-based), while
    # persisted effectidx values are 1-based slot IDs. Present a client-only
    # zero-based copy so study locations using effect slot 3 don't dereference
    # a nonexistent fourth array entry. The stored state and service rules stay
    # 1-based for start/complete processing.
    for staff in team.get("staffs", []):
        study = staff.get("study") or {}
        boost = study.get("boost") or {}
        if isinstance(boost.get("effectidx"), int) and boost["effectidx"] > 0:
            boost["effectidx"] -= 1
    for p in team.get("players", []):
        p.setdefault("expandme", "player")
        if "staticdata" in p and isinstance(p["staticdata"], dict):
            sd = p["staticdata"]
            sd.setdefault("boosts", {"ovr": 0})
            sd.setdefault("stats", [75] * 40)
            sd.setdefault("statusAttribute", {})
            sd.setdefault("preferredposovr", int(p.get("overallrating") or p.get("ovr") or 75))
            sd.setdefault("names", {"short": p.get("name") or "Cầu thủ", "full": p.get("name") or "Cầu thủ"})
            sd["applyliveboost"] = 0
    team.setdefault("formations", [])
    return team


def team_summary(team, teamid):
    # MenuScreen TeamInfoNew.setData: TEAMNAME(summary.id) + loadTeamCrest(summary.id), setOVR(ovr);
    # TeamStaticInfo (Tim > Klub) also reads name, league.id, nation{id,name}, rival.name.
    # TeamInfoNew.setOVR reads ovr.FW/MF/DF (rating bars); Enums.PLAYERPOSITION index ranges.
    ovr = {iid(p): player_ovr(p) for p in team["players"]}
    lines = {"DF": range(1, 9), "MF": range(9, 20), "FW": range(20, 28)}
    def line_ovr(r):
        v = [ovr.get(str(p.get("id") or p["spid"]), 0) for p in team["squad"]["positions"] if p.get("pos") in r]
        return round(sum(v) / len(v)) if v else 0
    return dict(roster_summary(teamid), id=teamid, ovr={k: line_ovr(r) for k, r in lines.items()})


def team_kit(teamid):
    # kit ids go to native as-is (freeroam kicker, 3D previews); {} = default goalie kit.
    # kit id = teamid << 10 | kittype (0 home, 1 away, 2 goalie): Enums.GOALIE_DEFAULT_KIT_ID
    # 45058 = 44 << 10 | 2; checked in FREEROAM (Barca home kit). id = teamid alone renders
    # a missing-texture checkerboard.
    return {"availables": {k: {"id": teamid << 10 | t} for k, t in (("home", 0), ("away", 1), ("goalie", 2))}}


START_EP = 1000000
# Enums.VIP_DETAIL_*: NONE 0, VIP 1<<1, VVIP 1<<2, VVIP_SEA 1<<4
VIP_DETAIL = 2
VIP_UNTIL = 2051222400000  # 2035-01-01, ms


def add_ep(st, n):
    """Credit match/season/cup EP to the active coach (shown by /coach/me money.ep)."""
    coach = next(c for c in st["coaches"] if c["slot"] == _default_slot(st))
    coach["ep"] = coach.get("ep", START_EP) + int(n)


CUP_LEVELS = 5  # statistics.cup.cupwins [AI level 0..4]


def coach_record(coach, part):
    """A coach's record of one kind (league, cup, ladder, market), created empty."""
    return coach.setdefault("record", {}).setdefault(part, {})


def record_match(st, part, gf, ga):
    """A finished league or cup match of mine (my goals gf, theirs ga)."""
    r = coach_record(my_coach(st), part)
    for k in ("wins", "draws", "losses", "goals", "goalsconceded", "streak", "maxstreak"):
        r.setdefault(k, 0)
    res = "wins" if gf > ga else "losses" if gf < ga else "draws"
    r[res] += 1
    r["goals"] += gf
    r["goalsconceded"] += ga
    r["streak"] = r["streak"] + 1 if res == "wins" else 0
    r["maxstreak"] = max(r["maxstreak"], r["streak"])


def default_coach_statistics():
    return {
        "club": {
            "ranked": {"total": 0, "wins": 0, "losses": 0, "forfeitwins": 0},
            "friendly": {"total": 0, "wins": 0, "losses": 0, "forfeitwins": 0}
        },
        "league": {
            "wins": 0, "draws": 0, "losses": 0, "goals": 0, "goalsconceded": 0,
            "winningstreak": {"max": 0, "curr": 0},
            "leaguewins": [0, 0, 0, 0],
            "normalwins": 0,
            "noramlbest": 0,
            "normalbest": 0,
            "ultimatebest": 0,
            "ultimatewins": 0
        },
        # Per-format arrays are indexed 0..4 (1v1..5v5) by the friendly
        # hub panels (MultiplayQuickMatchScreen sums wins[] and renders
        # wins[4] as the 5v5 row); at length 4 that row read undefined
        # and rendered NaN.
        # winningstreak.max is an ARRAY: CoachTotalRecordTabPage.setFriendRecord
        # reads Friend.quick.winningstreak.max.length and max[0]; the scalar 0
        # threw AS3 #1069 "Property length not found on 0" and killed the whole
        # Data tab (friendly/vscom panels never rendered).
        "friendly": {
            "custom": {"wins": [0, 0, 0, 0, 0], "draws": [0, 0, 0, 0, 0], "losses": [0, 0, 0, 0, 0], "total": [0, 0, 0, 0, 0], "rate": [0, 0, 0, 0, 0], "winningstreak": {"max": [0], "curr": 0}},
            "quick": {"wins": [0, 0, 0, 0, 0], "draws": [0, 0, 0, 0, 0], "losses": [0, 0, 0, 0, 0], "total": [0, 0, 0, 0, 0], "rate": [0, 0, 0, 0, 0], "winningstreak": {"max": [0], "curr": 0}},
            "vscom": {"wins": [0, 0, 0, 0, 0], "draws": [0, 0, 0, 0, 0], "losses": [0, 0, 0, 0, 0], "total": [0, 0, 0, 0, 0], "rate": [0, 0, 0, 0, 0], "winningstreak": {"max": [0], "curr": 0}},
            "roleplay": {"wins": [0, 0, 0, 0, 0], "draws": [0, 0, 0, 0, 0], "losses": [0, 0, 0, 0, 0], "total": [0, 0, 0, 0, 0], "rate": [0, 0, 0, 0, 0], "winningstreak": {"max": [0], "curr": 0}}
        },
        "simulation": {
            "custom": {"wins": 0, "draws": 0, "losses": 0, "total": 0, "rate": 0},
            "quick": {"wins": 0, "draws": 0, "losses": 0, "total": 0, "rate": 0},
            "vscom": {"wins": 0, "draws": 0, "losses": 0, "total": 0, "rate": 0}
        },
        "ladder": {
            "elo": 1000,
            "highelo": 1000,
            "division": {"name": "DIVISION_AMATEUR_C", "rank": 17},
            "rank": 0,
            "ranking": 0,
            "wins": 0,
            "draws": 0,
            "losses": 0,
            "forfeits": 0,
            "winningstreak": 0,
            "seasons": [],
            "highrecords": {
                "count": 0,
                "elo": [1000] * 10,
                "rank": [0] * 10,
                "division": [{"name": "DIVISION_AMATEUR_C", "rank": 17}] * 10,
                "winningstreak": [0] * 10,
                "wins": [0] * 10,
                "losses": [0] * 10,
                "date": "2026-11-01T00:00:00.000Z"
            }
        },
        "winstreak": [{"win": 0}, {"win": 0}, {"win": 0}],
        "cup": {
            "total": 0, "wins": 0, "draws": 0, "losses": 0, "abandonments": 0,
            "cupwins": [0, 0, 0, 0, 0], "totalwins": 0, "wincups": 0, "totalprize": 0
        },
        "boxingday": {
            "wins": 0, "losses": 0, "draws": 0, "total": 0,
            "consecutive_wins": 0, "max_consecutive_wins": 0,
            "pvp": {"maxstreak": 0, "currstreak": 0, "wins": 0, "draws": 0, "losses": 0, "bestrank": 0},
            "pve": {"maxstreak": 0, "currstreak": 0, "wins": 0, "draws": 0, "losses": 0, "bestrank": 0}
        }
    }


def _week_end_ts(now=None):
    """Next Monday 00:00 (UTC+7) as a unix timestamp: the weekly quest reset."""
    import datetime as _dt
    now = time.time() if now is None else now
    tz = _dt.timezone(_dt.timedelta(hours=7))
    d = _dt.datetime.fromtimestamp(now, tz).date()
    days = (7 - d.weekday()) % 7 or 7
    return _dt.datetime.combine(d + _dt.timedelta(days=days), _dt.time.min, tz).timestamp()


# The Quest window's reward boxes: the first entries of /shop/list/GPSHOP,
# bought with GP (quest points) instead of EP. A purchase whose shopid is
# GP_SHOPID + itemid takes the GP path (see ep_shop_purchase).
GP_SHOPID = 920_000_000
# itemid: GP price. The Quest window's reward boxes look their itemids
# up in /shop/list/GPSHOP and render bunches[0].price as the GP cost --
# served with their pack EP prices instead, the boxes showed the EP
# figures (97M-180M, clipped) against a maximum weekly income of 150 GP
# (user reports 2026-11-12). The WEEKLY window's five boxes are packs
# 45054/45055/45056/45061/45062 (names verified in ITEMNAMES and against
# the user's screenshot 2026-11-12 23:00: shown 1800000/1100000/9700000/
# 1200000/9800000 = the packs' 180M/110M/97M/120M/98M EP prices clipped
# by the box). The 45095 set priced earlier came from a misread dump
# section; it stays priced (harmless if some other window uses it).
# Priced as a ladder: a perfect week (150 GP) buys the best.
GP_SHOP_PRICES = {50021: 40, 501: 20, 28001: 50, 910003: 30, 45013: 60,
                  82214028: 100, 45122: 120, 45121: 130, 45095: 140,
                  45120: 150,
                  45056: 100, 45055: 120, 45062: 130, 45054: 140,
                  45061: 150}


def _gp_balance(st):
    """GP still valid this week; the balance lapses at the weekly reset."""
    q = st.get("quest") or {}
    return int(q.get("gp") or 0) if q.get("week") == _week_end_ts() else 0


def coach_me(st, slot):
    """Body of the /coach/me resource (CoachProxy). Field paths are the ones its
    fetchCoach* methods read; values are a fresh account. startFreeRoam waits for this
    resource to validate before signing on."""
    coaches = st.get("coaches") or []
    coach = next((c for c in coaches if c.get("slot") == slot), None)
    if coach is None:
        coach = coaches[0] if coaches else {"slot": slot, "name": "Manager", "teamid": 241, "currentbadge": 1001, "hiderank": False}

    teamid = coach.get("teamid", 241)
    team = owned_team(st["teams"].get(str(slot), {"squad": {"positions": [], "kickers": []}, "players": []}))
    team["formations"] = team_formations(teamid)
    team["teamcolors"] = active_teamcolors(team)
    if team["formations"] and team["squad"].get("formation") is None:
        team["squad"]["formation"] = team["formations"][0]["formationid"]
    default_stats = default_coach_statistics()
    coach_stats = coach.get("statistics") or default_stats
    # Cup statistics come from the coach's cup record (kept by the cup
    # lifecycle: ep_cup_new / cup_play_round / ep_cup_leave). The Data tab's
    # cup section reads wins/draws/losses (CupProxy.cupStatistics) plus
    # abandonments, cupwins per AI level and the best-prize highrecords;
    # the old static {total, wins, losses, wincups, totalprize} left draws
    # undefined and the section rendered NaN / -1% / undefined.
    _cup_rec = (coach.get("record") or {}).get("cup") or {}
    _cupwins = list(_cup_rec.get("cupwins") or [0] * CUP_LEVELS)
    _cupwins = (_cupwins + [0] * CUP_LEVELS)[:CUP_LEVELS]
    coach_stats["cup"] = {
        "wins": int(_cup_rec.get("wins", 0)), "draws": int(_cup_rec.get("draws", 0)),
        "losses": int(_cup_rec.get("losses", 0)),
        "abandonments": int(_cup_rec.get("abandonments", 0)),
        "cupwins": _cupwins,
        "totalwins": int(_cup_rec.get("totalwins", 0)),
        "wincups": int(_cup_rec.get("totalwins", 0)),
        "totalprize": int(_cup_rec.get("totalprize", 0)),
        "total": int(_cup_rec.get("wins", 0)) + int(_cup_rec.get("draws", 0)) + int(_cup_rec.get("losses", 0)),
    }
    if _cup_rec.get("best"):
        coach_stats["cup"]["highrecords"] = _cup_rec["best"]
    created_at_val = coach.get("createdat") or "2026-11-01T00:00:00.000Z"
    play_time_val = int(coach.get("playtime") or 3600)
    career_val = coach.get("career") or {
        "clubs": 1,
        "maxplayers": len(team.get("players", [])),
        "buyplayers": 0,
        "sellplayers": 0,
        "maxbuy": None,
        "maxsell": None,
        "appat": created_at_val
    }
    team_val = sum(card_price(p.get("spid", 0), max(1, int(p.get("grade") or 1))) for p in team.get("players", []))
    return {
        "id": coach.get("_id", "1"), "name": coach.get("name", "Manager"), "slot": slot, "teamid": teamid, "team": team, "hiderank": False,
        "profile": coach.get("profile") or {"description": "Xin chào! Chào mừng đến với FIFA Online 3.", "picture": 0},
        # CoachProxy.expandMoney parses money.mpdata.until as a W3C date; an exception there
        # silently aborts onChangeCoachMeResource before squad/players are read.
        "money": {"ep": coach.get("ep", START_EP), "cash": 0, "mp": 0, "useable_mp": 0,
                  "mpdata": {"point": 0, "until": "2030-01-01T00:00:00.000Z"}},
        # always a VIP member playing from an official PC warnet (IGR / GCA): CoachProxy.igr,
        # isVip() = vipDetail > 0 and vipExpireAt in the future; vipStatus.VIP1 = VIP expiry
        # after a VVIP period (updateVipExpireAt)
        "igr": True, "vipDetail": VIP_DETAIL, "vipExpireAt": VIP_UNTIL, "vipStatus": {"VIP1": VIP_UNTIL},
        "sp": {"point": 0}, "gpdata": {"point": _gp_balance(st), "until": w3c(_week_end_ts())}, "badges": [], "currentbadge": max(1001, int(coach.get("currentbadge") or 1001)),
        # league.leagueid -> CoachProxy.leagueId: Single > Liga builds the league from it
        "levelinfo": coach_levelinfo(coach), "statistics": coach_stats,
        "createdat": created_at_val, "playtime": play_time_val, "career": career_val,
        "teamvalue": {"val": team_val, "rank": 1, "updown": 0},
        "league": {"leagueid": roster_summary(teamid).get("league", {}).get("id", 0)},
        # limits.cups -> CupProxy.playMax (cups you can run at once); limits.players/baseplayers
        # -> the market's player-slot check (TransferPurchaseTabPage._limitPlayerCount)
        # limits.firstTeam: Skuad tab moveToFirstTeam reads it first and silently gives up when
        # it is missing, so Tim Cadangan cards (packs, trades) could never reach Tim inti/Formasi
        "club": {}, "uimode": 0, "limits": {"cups": 3, "players": MAX_PLAYERS, "baseplayers": MAX_PLAYERS,
                                             "firstTeam": FIRST_TEAM_MAX},
        "transferfavorites": [], "currdivision": "",
        # playlimit {modeid: {playcount}}: Liga's Mulai/Simulasi buttons compare playcount with
        # PLAYLIMIT.maxplay; without the key playcount stays undefined and they stay disabled
        "playlimit": {},
        "kit": team_kit(teamid), "summary": dict(team_summary(team, teamid),
            stadium={"id": int(coach.get('stadiumid') or _CLUB_STADIUM_LINKS.get(coach.get('teamid'), 1))},
            appat=coach.get('createdat') or w3c(os.path.getctime(os.path.join(HERE, 'gamedata', 'state.json')))),
        "stadium": {
            "id": int(coach.get('stadiumid') or _CLUB_STADIUM_LINKS.get(coach.get('teamid'), 1)),
            "name": _CLUB_STADIUMS.get(int(coach.get('stadiumid') or _CLUB_STADIUM_LINKS.get(coach.get('teamid'), 1)), {}).get("name") or "Sân vận động",
            "yearbuilt": _CLUB_STADIUMS.get(int(coach.get('stadiumid') or _CLUB_STADIUM_LINKS.get(coach.get('teamid'), 1)), {}).get("yearbuilt") or 1990,
            "capacity": _CLUB_STADIUMS.get(int(coach.get('stadiumid') or _CLUB_STADIUM_LINKS.get(coach.get('teamid'), 1)), {}).get("capacity") or 50000
        },
        "career": career_val,
        # cups -> CupProxy.cupPlayList: {metaid, cupid} of cups not left yet (front screen resumes
        # them; an ended one shows its won/eliminated screen with the reward button -> /leave)
        "items": coach_items(st, slot), "matchstats": [], "multiplay": coach.get("multiplay") or [], "scouters": coach_scouters(st, slot),
        "cups": [{"metaid": c["metaid"], "cupid": c["_id"]} for c in (st.get("cups") or {}).values()],
        # SquadScreenHandler: formation combobox reads /coach/me?path=formations
        "formations": team["formations"], "teamsquadlist": team["teamsquadlist"],
    }


def coach_me_resources(st, slot):
    """The /coach/me resource for a reply's `resources`. CoachProxy's fetchCoach* read
    getData("/coach/me?path=X"), which NetEvent.getDataEx resolves by walking this body; pushing
    every ?path= part too re-ran onChangeCoachMeResource per part and nested the client's
    resource chain until AS3 "Error #1023: Stack overflow" at login."""
    return [{"uri": "/coach/me", "body": coach_me(st, slot)}]


def _default_slot(st):
    return next((c["slot"] for c in st["coaches"] if c["_id"] == st["defaultcoachid"]),
                st["coaches"][0]["slot"] if st["coaches"] else 0)  # default manager deleted: the other one


def ep_coach_me(query, body):
    st = _state()
    c = coach_me(st, _default_slot(st))
    if query:
        try:
            import urllib.parse
            qs = urllib.parse.parse_qs(query.decode('utf-8', errors='ignore') if isinstance(query, bytes) else str(query))
            if "path" in qs:
                p = qs["path"][0]
                if p in c:
                    return _json(c[p])
        except Exception:
            pass
    return _json(c)


def ep_coach_profile(query, body):
    st = _state()
    slot = _default_slot(st)
    coach = next((c for c in (st.get("coaches") or []) if c.get("slot") == slot), None)
    if coach is None and st.get("coaches"):
        coach = st["coaches"][0]
    if coach is None:
        return _json({"result": False})
    if body:
        try:
            req = json.loads(body.decode("utf-8") if isinstance(body, bytes) else str(body))
            prof = coach.setdefault("profile", {})
            if "description" in req:
                prof["description"] = req["description"]
            if "picture" in req:
                prof["picture"] = req["picture"]
            _save(st)
        except Exception:
            pass
    prof = coach.get("profile") or {"description": "Xin chào! Chào mừng đến với FIFA Online 3.", "picture": 0}
    return _json(prof)


_NOTE_MS = [0]


def _inbox_notification(category, kind, details):
    """A system notification (SystemMessageProxy: /inbox/my/notifications);
    dates strictly increase. details.title is the dropdown entry's text
    (a localization key or text). Shape ported from upstream's note()."""
    ms = _NOTE_MS[0] = max(int(time.time() * 1000), _NOTE_MS[0] + 1)
    date = time.strftime("%Y-%m-%dT%H:%M:%S", time.gmtime(ms // 1000)) + f".{ms % 1000:03d}Z"
    return {"_id": new_iid(), "category": category, "type": kind, "date": date, "details": details}


def ep_coach_onready(query, body):
    # POST /coach/me/onready comes right after CoachProxy.invalidate() -> reset() (which nulls
    # _squad); without /coach/me in this reply the menu is built with squad null (black FREEROAM).
    # /inbox/my/notifications (an Array) readies SystemMessageProxy, its only feed: until then
    # MenuScreenMediator.onReady never runs, so the Messenger screen is never attached and the
    # Messenger button does nothing (upstream issue #30 — the two dead top-bar buttons).
    # It carries the login notice of the original game while a sold offer waits to be collected.
    st = _state()
    notes = []
    if any(o.get("status") == "sold" for o in (st.get("offers") or [])):
        notes = [_inbox_notification("transfermarket", "list",
                                     {"title": "TRANSFERMARKET_LOGIN_ALARM"})]
    return _json({"result": True, "resources": coach_me_resources(st, _default_slot(st))
                  + [{"uri": "/inbox/my/notifications", "body": notes}]})


def ep_default_coach(slot, query, body):
    # PUT /coach/<slot>/default: the client calls setLocalCoachId(body.resources[0].body.id);
    # resources are also fed to cached resources, so /coach/me (CoachProxy) goes first.
    st = _state()
    if not any(c['slot'] == slot and c.get('state', 'active') == 'active' for c in st['coaches']):
        return _json({'result': False, 'reason': 'INVALID_COACH'})
    import copy
    previous = _default_slot(st)
    if previous != slot:
        context_keys = ('league2', 'cups', 'offers', 'match', 'trade', 'cup_pending')
        contexts = st.setdefault('coachcontexts', {})
        contexts[str(previous)] = {k: copy.deepcopy(st[k]) for k in context_keys if k in st}
        for key in context_keys:
            st.pop(key, None)
        st.update(copy.deepcopy(contexts.get(str(slot), {})))
    st['defaultcoachid'] = coach_id(slot)
    daily_login(st)  # first login of the day: today's reward goes to the Bag
    if str(slot) in st["teams"]:
        balance_first_team(st["teams"][str(slot)])
        fix_formation(st["teams"][str(slot)], next(c["teamid"] for c in st["coaches"] if c["slot"] == slot))
    _save(st)
    return _json({"result": True, "resources": coach_me_resources(st, slot)})


TACTIC_SLOTS = 6


def default_tactic(i):
    # 6 Tactical Presets: Tấn Công, Phòng Thủ, Bẫy Việt Vị, Pressing, Phản Công Nhanh, Tạt Cánh Đánh Đầu
    presets = [
        {
            "name": "Tấn Công",
            "teammentality": 4,
            "bus": {"buildupspeed": 75, "passing": 65, "positioning": 1},
            "cc": {"passing": 70, "crossing": 60, "shooting": 75, "positioning": 1},
            "def": {"mentality": 65, "aggression": 65, "teamwidth": 55, "defenderline": 0}
        },
        {
            "name": "Phòng Thủ",
            "teammentality": 2,
            "bus": {"buildupspeed": 35, "passing": 40, "positioning": 0},
            "cc": {"passing": 40, "crossing": 45, "shooting": 40, "positioning": 0},
            "def": {"mentality": 30, "aggression": 40, "teamwidth": 35, "defenderline": 0}
        },
        {
            "name": "Bẫy Việt Vị",
            "teammentality": 3,
            "bus": {"buildupspeed": 60, "passing": 55, "positioning": 0},
            "cc": {"passing": 60, "crossing": 50, "shooting": 55, "positioning": 0},
            "def": {"mentality": 60, "aggression": 50, "teamwidth": 45, "defenderline": 1}
        },
        {
            "name": "Pressing",
            "teammentality": 4,
            "bus": {"buildupspeed": 80, "passing": 60, "positioning": 0},
            "cc": {"passing": 65, "crossing": 55, "shooting": 70, "positioning": 0},
            "def": {"mentality": 85, "aggression": 85, "teamwidth": 50, "defenderline": 0}
        },
        {
            "name": "Phản Công Nhanh",
            "teammentality": 3,
            "bus": {"buildupspeed": 85, "passing": 75, "positioning": 0},
            "cc": {"passing": 80, "crossing": 65, "shooting": 65, "positioning": 1},
            "def": {"mentality": 35, "aggression": 50, "teamwidth": 40, "defenderline": 0}
        },
        {
            "name": "Tạt Cánh Đánh Đầu",
            "teammentality": 4,
            "bus": {"buildupspeed": 70, "passing": 60, "positioning": 0},
            "cc": {"passing": 50, "crossing": 85, "shooting": 75, "positioning": 0},
            "def": {"mentality": 50, "aggression": 60, "teamwidth": 65, "defenderline": 0}
        }
    ]
    idx = int(i) if 0 <= int(i) < len(presets) else 0
    t = dict(presets[idx])
    t["id"] = int(i)
    return t

def _tactics(st):
    slot = str(_default_slot(st))
    saved = st.setdefault("tactics", {}).setdefault(slot, {})
    tactics = []
    for i in range(TACTIC_SLOTS):
        tactic = dict(saved.get(str(i)) or default_tactic(i))
        tname = str(tactic.get("name") or "").strip()
        if not tname or tname in (f"Strategy {i + 1}", f"Tactic {i + 1}"):
            def_t = default_tactic(i)
            tactic["name"] = def_t["name"]
            if not tactic.get("bus"):
                tactic.update(def_t)
        tactics.append(tactic)
    return saved, tactics


def match_tactic_payload(tactics):
    """Give each match side all six independent tactic profiles, filling only missing fields."""
    indexed = {}
    for tactic in tactics if isinstance(tactics, list) else []:
        if not isinstance(tactic, dict):
            continue
        try:
            idx = int(tactic.get("id"))
        except (TypeError, ValueError):
            continue
        if 0 <= idx < TACTIC_SLOTS and idx not in indexed:
            indexed[idx] = tactic
    result = []
    for i in range(TACTIC_SLOTS):
        base = default_tactic(i)
        saved = indexed.get(i)
        if saved:
            for key, value in saved.items():
                if key in ("bus", "cc", "def") and isinstance(value, dict):
                    base[key].update(value)
                else:
                    base[key] = value
        if not str(base.get("name") or "").strip():
            base["name"] = f"Strategy {i + 1}"
        base["id"] = i
        result.append(base)
    return result


def ep_tactic_all(query, body):
    # GET /game/my/tactic/all -> Array of 6 tactics (TeamManagementTeamTacticsTabPage.onReceived)
    return _json(_tactics(_state())[1])


def ep_tactic(i, query, body):
    """Save (PUT, body = tactic) or reset (DELETE -> {result, resources:[{body: default}]})."""
    req = json.loads(body or b"{}")
    st = _state(); saved, _ = _tactics(st)
    if req.get("_method") == "DELETE":
        saved.pop(str(i), None)
        _save(st)
        return _json({"result": True, "resources": [{"uri": f"/game/my/tactic/{i}", "body": default_tactic(i)}]})
    if "bus" in req:
        saved[str(i)] = dict({k: v for k, v in req.items() if k != "_method"}, id=i)
        _save(st)
        return _json({"result": True})
    saved.pop(str(i), None); _save(st)
    return _json({"result": True, "resources": [{"uri": f"/game/my/tactic/{i}", "body": default_tactic(i)}]})


# --------------------------------------------------------------------------- #
# Single > Liga (League 2.0). LeagueCupProxy builds the whole calendar client-side from
# /league2/me: league.teams + openingday, one match per round from createSchedule (ported
# below as league_round). The server keeps results and the table:
#   creation screen: GET /league2/me (difficultybest/difficultyselect), /league2/reward,
#     POST /league2/sponsor/<difficulty>, POST /league2/calendar {difficulty, sponsor}
#   a round: POST /league2/match (-> /match/<id> resource, stage created) ->
#     PUT /match/<id>/side/<s>/ready (-> stage playing -> StartSingleMatch) ->
#     PUT /match/<id>/end {home:{id,score}, away:{id,score}} -> leagueparam + /league2/me
#   or POST /league2/match/sim (no play), POST /league2/sleep (bye round)
# laststatus "league:<round>" tells the client which day it is (LeagueCupProxy.setToday).
# --------------------------------------------------------------------------- #
LEAGUE_SIZE = 10  # fallback only when a league has no roster data


def _db():
    c = sqlite3.connect(gen_teams.DB); c.row_factory = sqlite3.Row
    return c


def league_teams(leagueid, mine):
    """Use the same valid domestic/national rosters as the team selector."""
    choices = next((ts for _, leagues in club_index() for lid, ts in leagues if lid == leagueid), [])
    return [mine] + [tid for tid in choices if tid != mine]


def league_team_count(leagueid=None, teams=None):
    """Return the actual participant count, preferring a running season's roster."""
    if teams:
        return len(teams)
    if leagueid is not None:
        choices = next((ts for _, leagues in club_index() for lid, ts in leagues if lid == leagueid), [])
        if choices:
            return len(choices)
    return LEAGUE_SIZE


def league_round(teams, rnd):
    """Fixtures [(home, away)] of round rnd: LeagueCupProxy.createSchedule/getRotatedIndexAt
    (circle method; second half swaps home/away). None = bye (odd team count)."""
    n = len(teams); size = n + n % 2; per = size - 1
    flip, r = (rnd // per) % 2 == 1, rnd % per

    def rot(i):
        k = 0 if i == 0 else (i - 1 + r) % per + 1
        return teams[k] if k < n else None
    pairs = []
    for k in range(size // 2):
        a, b = rot(k), rot(size - 1 - k)
        f = (not flip) if (k == 0 and r % 4 > 1) else flip
        pairs.append((b, a) if f else (a, b))
    return pairs


def _poisson(rng, lam):
    lim, k, p = math.exp(-lam), 0, 1.0
    while True:
        p *= rng.random()
        if p <= lim:
            return k
        k += 1


def team_strength(st, teamid):
    """Average rating (+ grade boost) of the best 11."""
    raw = st["teams"].get(str(_default_slot(st))) if teamid == my_teamid(st) else None
    ovrs = sorted((player_ovr(p) for p in (raw or roster_team(teamid))["players"]), reverse=True)[:11]
    return sum(ovrs) / len(ovrs) if ovrs else 60


def sim_score(rng, home, away):
    """ponytail: Poisson goals from the rating gap (+3 home edge); no match engine."""
    d = (home + 3 - away) / 10
    return _poisson(rng, max(0.3, 1.4 + 0.6 * d)), _poisson(rng, max(0.2, 1.1 - 0.6 * d))


def league_stats(teams, records, prev=None):
    """league.statistics {teamid: {wins, draws, losses, goals, goalsconceded, awaygoals,
    point, rank, before{rank, point}}} (LeagueCupProxy.makeLeagueRanking)."""
    s = {str(t): {"wins": 0, "draws": 0, "losses": 0, "goals": 0, "goalsconceded": 0,
                  "awaygoals": 0, "point": 0} for t in teams}
    for m in records:
        h, a = s[str(m["home"])], s[str(m["away"])]
        gh, ga = m["goals"]["home"], m["goals"]["away"]
        h["goals"] += gh; h["goalsconceded"] += ga
        a["goals"] += ga; a["goalsconceded"] += gh; a["awaygoals"] += ga
        if gh != ga:
            win, lose = (h, a) if gh > ga else (a, h)
            win["wins"] += 1; win["point"] += 3; lose["losses"] += 1
        else:
            h["draws"] += 1; a["draws"] += 1; h["point"] += 1; a["point"] += 1
    order = sorted(teams, key=lambda t: tuple(-v for v in (
        s[str(t)]["point"], s[str(t)]["goals"] - s[str(t)]["goalsconceded"], s[str(t)]["goals"], s[str(t)]["awaygoals"])))
    for i, t in enumerate(order):
        s[str(t)]["rank"] = i + 1
    for t, v in s.items():
        p = (prev or {}).get(t) or v
        v["before"] = {"rank": p["rank"], "point": p["point"]}
    return s


def my_teamid(st):
    return next(c["teamid"] for c in st["coaches"] if c["slot"] == _default_slot(st))


def l2_me(st):
    """GET /league2/me body; LeagueCupProxy.fetch* read each key as a path."""
    teamid = my_teamid(st)
    lg = st.get("league2") or {}
    me = {"teamid": teamid, "leagueid": lg.get("leagueid") or roster_summary(teamid).get("league", {}).get("id", 0),
          # creation screen: selectable = difficultybest - 10 (24 unlocks all 10 normal + 5
          # ultimate levels), preselected = difficultyselect - 10
          "difficultybest": 24, "difficultyselect": lg.get("difficultyselect", 10),
          # no cupqualifier/cupfinal keys: any object there makes fetchCupQualifier build
          # cup groups (makeLeagueCupGroupTeams -> TypeError on missing teamgroups)
          "laststatus": lg.get("laststatus", ""), "cupinfo": {"entered": False},
          "leader": {"league": {}, "cup": {}},
          # MenuScreen: season.status started/finished -> league front, else creation
          "sponsoroffice": lg.get("sponsoroffice", {"season": {"status": "none"}})}
    if lg.get("league"):
        me.update(openingday=lg["openingday"], league=lg["league"])
    return me


def l2_resources(st):
    return [{"uri": "/league2/me", "body": l2_me(st)}]


def _league_record_rows(st):
    """GET /league2/history/players/league rows, in the shape
    LeagueCupRecordTabPage.joinPlayerData consumes (proven by the
    upstream server): FLAT records [{id: card _id, spid, role, matches,
    ratings (SUM -- the page averages), goals, assists, passes,
    passtries, tackles, saves}] joined to the squad's cards by id.
    Ours used to nest the stats under "record" with an averaged rating
    and a pass fraction, so the page joined nothing and the league Data
    tab showed no stats at all (user report 2026-11-12 23:00). Stored
    records keep the running average + pass success/attempted counts;
    the conversion happens here. role = the squad slot the card plays
    (Enums.PLAYERPOSITION index), when it is in the current squad."""
    lg = st.get("league2") or {}
    records = lg.get("playerrecords") or {}
    slot = _default_slot(st)
    team = (st.get("teams") or {}).get(str(slot)) or {}
    positions = (team.get("squad") or {}).get("positions") or []
    role_by_id = {str(p.get("id")): i for i, p in enumerate(positions)}
    spid_by_hexid = {}
    for p in team.get("players") or []:
        if p.get("_id"):
            spid_by_hexid[str(p["_id"])] = int(p.get("spid") or 0)
    rows = []
    for player_id, value in records.items():
        matches = int(value.get("matches", 0) or 0)
        if matches <= 0:
            continue
        pid = str(player_id)
        rows.append({
            "id": pid,
            "spid": int(pid) if pid.isdigit() else spid_by_hexid.get(pid, 0),
            "role": role_by_id.get(pid, 0),
            "matches": matches,
            "ratings": round(float(value.get("ratings", 0) or 0) * matches, 2),
            "goals": int(value.get("goals", 0) or 0),
            "assists": int(value.get("assists", 0) or 0),
            # "passes" is the success RATE as a 0..1 fraction: the
            # client's joinPlayerData renders int(passes * 100) + "%"
            # (serving the count showed e.g. 13300%).
            "passes": round(int(value.get("passes_success", 0) or 0) / int(value.get("passes_attempted", 0) or 0), 4) if int(value.get("passes_attempted", 0) or 0) > 0 else 0,
            "passtries": int(value.get("passes_attempted", 0) or 0),
            "tackles": int(value.get("tackles", 0) or 0),
            "saves": int(value.get("saves", 0) or 0),
        })
    return rows


def record_league_match_players(st, result, match_key):
    """Aggregate the user's actual match stats once for the current League 2 season."""
    lg = st.get("league2") or {}
    if not lg.get("league") or not isinstance(result, dict):
        return
    seen = lg.setdefault("playerrecord_matchkeys", [])
    if match_key in seen:
        return

    slot = _default_slot(st)
    team = (st.get("teams") or {}).get(str(slot)) or {}
    players = team.get("players") or []
    squad = team.get("squad") or {}
    registered_ids = {str(p.get("id")) for p in squad.get("positions") or [] if p.get("id")}
    registered_spids = {int(p.get("spid") or 0) % 1000000 for p in squad.get("positions") or []}
    active = [p for p in players if iid(p) in registered_ids or
              int(p.get("spid") or 0) % 1000000 in registered_spids]
    if not active:
        active = [p for p in players if not p.get("team2")]
    by_spid = {}
    for player in active:
        by_spid.setdefault(int(player.get("spid") or 0) % 1000000, []).append(player)

    mine = my_teamid(st)
    side = None
    my_side = result.get("myTeam")
    if my_side in ("home", "away"):
        side = result.get(my_side)
    if side is None:
        for name in ("home", "away"):
            candidate = result.get(name) or {}
            if str(candidate.get("id")) == str(mine):
                side = candidate
                break
    if side is None:
        match = st.get("match") or {}
        home_is_mine = (match.get("gamesettings") or {}).get("home", True)
        side = result.get("home" if home_is_mine else "away")
    side = side or {}
    stat_players = side.get("players") or []
    if not stat_players:
        return

    records = lg.setdefault("playerrecords", {})
    for stat in stat_players:
        try:
            spid = int(stat.get("spid") or stat.get("dbid") or stat.get("id") or 0) % 1000000
        except (TypeError, ValueError):
            continue
        candidates = by_spid.get(spid) or []
        if not candidates:
            continue
        player = candidates.pop(0)
        row = records.setdefault(iid(player), {"matches": 0, "ratings": 0.0, "goals": 0,
            "assists": 0, "passes_success": 0, "passes_attempted": 0, "tackles": 0, "saves": 0})
        old_matches = int(row.get("matches", 0) or 0)
        rating = float(stat.get("rating", 0) or 0)
        row["ratings"] = ((float(row.get("ratings", 0) or 0) * old_matches + rating) /
                          (old_matches + 1))
        row["matches"] = old_matches + 1
        for field in ("goals", "assists", "saves"):
            row[field] = int(row.get(field, 0) or 0) + int(stat.get(field, 0) or 0)
        tackles = stat.get("tackles") or 0
        passes = stat.get("passes") or 0
        row["tackles"] = int(row.get("tackles", 0) or 0) + (int(tackles[0]) if isinstance(tackles, (list, tuple)) and tackles else int(tackles) if isinstance(tackles, (int, float)) else 0)
        if isinstance(passes, (list, tuple)) and len(passes) >= 2:
            row["passes_success"] = int(row.get("passes_success", 0) or 0) + int(passes[0] or 0)
            row["passes_attempted"] = int(row.get("passes_attempted", 0) or 0) + int(passes[1] or 0)
        elif isinstance(passes, (int, float)):
            row["passes_success"] = int(row.get("passes_success", 0) or 0) + int(passes)
            row["passes_attempted"] = int(row.get("passes_attempted", 0) or 0) + int(passes)
    seen.append(match_key)


def seed_latest_league_player_stats(st):
    """Preserve the last completed league match when upgrading an older profile."""
    lg = st.get("league2") or {}
    match = st.get("match") or {}
    param = match.get("leagueparam") or {}
    if (not lg.get("league") or lg.get("playerrecords") or lg.get("playerrecord_matchkeys") or
            match.get("stage") != "closed" or not str(match.get("type", "")).startswith("League_2_") or
            param.get("matchtype") != "league"):
        return False
    try:
        last_round = int(param.get("round"))
    except (TypeError, ValueError):
        return False
    me = my_teamid(st)
    if not any(int(record.get("round", -1)) == last_round and
               me in (int(record.get("home", -1)), int(record.get("away", -1)))
               for record in (lg["league"].get("matchrecords") or [])):
        return False
    result = match.get("result") or {}
    if not any((result.get(side) or {}).get("players") for side in ("home", "away")):
        return False
    record_league_match_players(st, result, f"league:{param.get('round')}")
    return bool(lg.get("playerrecords"))


def ep_l2_player_history(kind, query, body):
    # Cup records are tracked separately; returning an Array is required even before any cup data.
    st = _state()
    if kind == "league" and seed_latest_league_player_stats(st):
        _save(st)
    return _json(_league_record_rows(st) if kind == "league" else [])


def ep_l2_leader(kind, query, body):
    st = _state()
    if kind == "cup":
        return _json({"FW": [], "MF": [], "DF": [], "GK": [], "ratings": [],
                      "goals": [], "assists": [], "tackles": [], "passes": [], "saves": []})
    if seed_latest_league_player_stats(st):
        _save(st)
    league = (st.get("league2") or {}).get("league") or {}
    stats = league.get("statistics") or {}
    me = my_teamid(st)
    rank = (stats.get(str(me)) or {}).get("rank", 1)
    goals = (stats.get(str(me)) or {}).get("goals", 0)
    return _json(season_leaders(st, me, rank, goals))


def repair_league_context(st):
    lg = st.get("league2") or {}
    league = lg.get("league") or {}
    if not league:
        return False
    teamid = my_teamid(st)
    teams = league.get("teams", [])
    owner = lg.get("teamid", teams[0] if teams else None)
    leagueid = roster_summary(teamid).get("league", {}).get("id", 0)
    if owner == teamid and lg.get("leagueid") == leagueid and teamid in teams:
        return False
    # Preserve the previous season rather than relabel its fixtures as a different team.
    st.setdefault("league2_archives", []).append({"slot": _default_slot(st),
        "reason": "team_changed", "archivedat": int(time.time()*1000), "season": lg})
    st["league2"] = {"difficultyselect": lg.get("difficultyselect", 10)}
    return True


def ep_l2_me(query, body):
    st = _state()
    if repair_league_context(st):
        _save(st)
    elif seed_latest_league_player_stats(st):
        _save(st)
    return _json(l2_me(st))


def ep_l2_reward(query, body):
    # LeagueCupCreationScreen.onCalendarRewardsResourceReceived: Array indexed by difficulty
    # level (0-9 normal, 10-14 ultimate) of {ep, items[]}
    return _json([{"ep": 20000 * (i + 1), "items": []} for i in range(15)])


# sponsorid -> its mission id; LEAGUE2SPONSOR.mission[mid] = League2_sponsor_title_<mid> with
# the target in "lv1": 2 = finish in the top N, 3 = N wins in a row, 4 = N clean sheets in a row
SPONSORS = {"1": "2", "2": "3", "3": "4"}
MISSION_TARGET = {"2": 3, "3": 3, "4": 2}  # keep in sync with LEAGUE2SPONSOR lv1


def ep_l2_sponsor(difficulty, query, body):
    """POST /league2/sponsor/<difficulty>: offers. OnReceivedSponsorList reads level, maxround
    and sponsors[] {_id, sponsorid, missions[{mid}]} (names/logos/goals from LEAGUE2SPONSOR)."""
    st = _state()
    teamid = my_teamid(st)
    leagueid = roster_summary(teamid).get("league", {}).get("id", 0)
    count = league_team_count(leagueid)
    return _json({"result": True, "level": 1, "maxround": 2 * (count - 1) - 1,
                  "sponsors": [{"_id": sid, "sponsorid": sid, "missions": [{"mid": mid}]} for sid, mid in SPONSORS.items()]})


def mission_progress(st, mid):
    """(progress args, achieved) of a sponsor mission from my league results so far."""
    lg = st.get("league2") or {}; league = lg.get("league") or {}
    me = my_teamid(st)
    mine = sorted((m for m in league.get("matchrecords", []) if me in (m["home"], m["away"])), key=lambda m: m["round"])
    goals = [(m["goals"]["home"], m["goals"]["away"]) if m["home"] == me else (m["goals"]["away"], m["goals"]["home"]) for m in mine]

    def best_run(ok):
        best = run = 0
        for g in goals:
            run = run + 1 if ok(g) else 0; best = max(best, run)
        return best
    target = MISSION_TARGET.get(mid, 1)
    if mid == "2":
        rank = league.get("statistics", {}).get(str(me), {}).get("rank", 0)
        done = len(mine) > league.get("maxround", 0)  # season over
        return [rank], done and 0 < rank <= target
    value = best_run((lambda g: g[0] > g[1]) if mid == "3" else (lambda g: g[1] == 0))
    return [value], value >= target


def ep_l2_achievement(query, body):
    """PUT /league2/sponsor/achievement -> the chosen sponsor's goals for the calendar tab
    (setSponsorGoalList: missions[{mid, achievement, progress[args]}]; getProgressString fills
    LEAGUE2SPONSOR.mission[mid].progress with progress)."""
    st = _state(); lg = st.get("league2") or {}
    mid = SPONSORS.get(str(lg.get("sponsor")), "2")
    progress, done = mission_progress(st, mid)
    return _json({"result": True, "level": 1, "missions": [{"mid": mid, "achievement": done, "progress": progress}]})


# Club creation > "Tim lain" (StartupTeamCreationScreen + TeamSelectionUtil): nation -> league ->
# club steppers from SORTNATION [nationid], SORTLEAGUE {nationid: [leagueid]} and SORTTEAM
# {leagueid: [teamid]}; the picked club's squad is /static/info/authentic/team/licensed/<id>
# ({teamid, summary, team}, built by gen_teams.team like the six recommended clubs).
CLUB_MIN_PLAYERS = 18
_CLUBS = []
with open(os.path.join(HERE, "gamedata", "club_catalog.json"), encoding="utf-8") as _f:
    _CLUB_CATALOG = json.load(_f)
_CLUB_TEAMS = {t["teamid"]: t for t in _CLUB_CATALOG["teams"]}
_CLUB_LEAGUES = {t["leagueid"]: t for t in _CLUB_CATALOG["leagues"]}
_CLUB_NATIONS = {t["nationid"]: t for t in _CLUB_CATALOG["nations"]}
_CLUB_LINKS = {t["teamid"]: t["leagueid"] for t in _CLUB_CATALOG["leagueteamlinks"]}
_NATIONAL_TEAMS = {n["teamid"]: n["nationid"] for n in _CLUB_NATIONS.values()
                   if n["teamid"] in _CLUB_TEAMS and n["teamid"] > 0}
_CLUB_LINKS.update({tid: 78 for tid in _NATIONAL_TEAMS})

_CLUB_STADIUMS = {t["stadiumid"]: t for t in _CLUB_CATALOG["stadiums"]}
_CLUB_STADIUM_LINKS = {t["teamid"]: t["stadiumid"] for t in _CLUB_CATALOG["teamstadiumlinks"]}


def club_summary_response(teamid):
    r = roster(teamid)
    summary = dict(r["summary"])
    info = _CLUB_TEAMS.get(teamid, {})
    leagueid = _CLUB_LINKS.get(teamid, summary.get("league", {}).get("id", 0))
    league = _CLUB_LEAGUES.get(leagueid, {})
    nationid = league.get("countryid", 0)
    # National squads and Rest of World must not inherit the league's placeholder nation.
    if leagueid == 78:
        nationid = next((n["nationid"] for n in _CLUB_NATIONS.values() if n["teamid"] == teamid), 0)
    elif leagueid == 76:
        nationid = next((n["nationid"] for n in _CLUB_CATALOG.get("rowteamnationlinks", []) if n["teamid"] == teamid), 0)
    summary.update(id=teamid, teamid=teamid,
                   name=info.get("teamname") or summary.get("name", ""),
                   league={"id": leagueid, "name": league.get("leaguename", ""),
                           "nationid": nationid},
                   nation={"id": nationid, "name": _CLUB_NATIONS.get(nationid, {}).get("nationname", "")},
                   rival={"name": _CLUB_TEAMS.get(info.get("rivalteam"), {}).get("teamname", "")})
    stadiumid = _CLUB_STADIUM_LINKS.get(teamid)
    stadium = _CLUB_STADIUMS.get(stadiumid, {})
    stadium = {"id": stadiumid or 0, "name": stadium.get("name", "Sân vận động Quốc gia Mỹ Đình"),
               "yearbuilt": stadium.get("yearbuilt", 2008), "capacity": stadium.get("capacity", 68000)}
    return {**summary, "summary": summary, "stadium": stadium, "kit": team_kit(teamid)}


def club_index():
    """Native country/league links; alphabetical clubs and top division first."""
    if not _CLUBS:
        with closing(_db()) as c:
            rows = c.execute("""SELECT p.teamid, COUNT(DISTINCT p.playerid),
                MAX(p.preferredposition1 = 0) FROM players p
                JOIN permid m ON m.spid=p.spid AND m.licensed=1
                WHERE p.spid / 1000000 = ? GROUP BY p.teamid""",
                (gen_teams.CURRENT_SEASON,)).fetchall()
        leagues = {}
        for tid, count, goalie in rows:
            lid = _CLUB_LINKS.get(tid)
            if count < CLUB_MIN_PLAYERS or not goalie or lid not in _CLUB_LEAGUES or lid in (76, 78, 2028):
                continue
            leagues.setdefault(lid, []).append(tid)
        nations = {}
        for lid, teams in leagues.items():
            nation = _CLUB_LEAGUES[lid]["countryid"]
            teams.sort(key=lambda tid: (_CLUB_TEAMS[tid]["teamname"].casefold(), tid))
            nations.setdefault(nation, []).append((lid, teams))
        # International is a dedicated category, never mixed with domestic leagues.
        with closing(_db()) as c:
            counts = {n:(total,goalie) for n,total,goalie in c.execute("""
                SELECT p.nationality, COUNT(DISTINCT p.playerid), MAX(p.preferredposition1=0)
                FROM players p JOIN permid m ON m.spid=p.spid AND m.licensed=1
                WHERE p.spid / 1000000 = ? GROUP BY p.nationality""",
                (gen_teams.CURRENT_SEASON,))}
        internationals = [tid for tid,n in _NATIONAL_TEAMS.items()
                          if counts.get(n,(0,0))[0]>=CLUB_MIN_PLAYERS and counts[n][1]]
        if internationals:
            nations[75] = [(78, sorted(internationals, key=lambda tid:_CLUB_TEAMS[tid]["teamname"].casefold()))]
        for nation in sorted(nations, key=lambda n: _CLUB_NATIONS.get(n, {}).get("nationname", "")):
            ls = sorted(nations[nation], key=lambda x: (_CLUB_LEAGUES[x[0]]["level"], x[0]))
            _CLUBS.append((nation, ls))
    return _CLUBS


def ep_sortnation(query, body):
    # Put International first so the generic Cup Mode team selector opens on
    # national teams instead of inheriting the manager's domestic club nation.
    # Domestic nations remain available for ordinary club creation/team changes.
    nations = [n for n, _ in club_index()]
    return _json(([75] if 75 in nations else []) + [n for n in nations if n != 75])


def ep_sortleague(query, body):
    return _json({str(n): [lid for lid, _ in ls] for n, ls in club_index()})


def ep_sortteam(query, body):
    return _json({str(lid): ts for _, ls in club_index() for lid, ts in ls})


def ep_sortworldcupteam(query, body):
    """World Cup team picker: confederation id -> playable national team ids."""
    teams = {}
    for nation in _CLUB_NATIONS.values():
        teamid = int(nation.get("teamid") or 0)
        confederation = int(nation.get("confederation") or 0)
        if teamid in _NATIONAL_TEAMS and confederation:
            teams.setdefault(str(confederation), []).append(teamid)
    for teamids in teams.values():
        teamids.sort(key=lambda tid: (_CLUB_TEAMS.get(tid, {}).get("teamname", "").casefold(), tid))
    return _json(teams)


def ep_worldcup_authentic_team(teamid, query, body):
    """Native WC team preview expects {team, kit, extra} for the selected nation."""
    teamid = int(teamid)
    if teamid not in _NATIONAL_TEAMS:
        return _json({"result": False, "reason": "select a valid national team"})
    r = roster(teamid)
    raw_team = r.get("team") or {}
    # Full owned instances (grade/life/levelinfo/real price): the creation
    # screen's player carousel reads those fields; the thin raw roster rows
    # ({spid, price: a season code}) left the carousel showing the coach's
    # own club players (user report 2026-11-12: Buffon for Belgium).
    team = owned_team(raw_team)
    captain = str(team.get("captain") or (raw_team.get("squad") or {}).get("captain") or "")
    team.update(teamId=teamid, teamid=teamid, captain=captain)
    summary = r.get("summary") or {}
    grade = int(round(float(summary.get("teamgrade") or summary.get("grade") or 3)))
    extra = {
        "captain": captain,
        "fifarank": 0,
        "preliminary_result": "",
        "worldcup_results": "",
        "qualification": "",
        "coach": "",
        "grade": max(0, min(5, grade)),
    }
    return _json({"team": team, "kit": team_kit(teamid), "extra": extra})


def ep_authentic_team(kind, teamid, query, body):
    """/static/info/authentic/team/licensed|all/<teamid>: a club's starter squad {teamid, summary,
    team} (responses/ files override for the recommended clubs). StartupPlayerSelectionScreen
    (after "Tim lain") looks up team.captain among team.players by id and dresses its 3D player
    in kit.availables.home/goalie."""
    r = roster(int(teamid))
    # Full owned instances, like a cup team gets (see
    # ep_worldcup_authentic_team): on teamselect "authentic" the cup
    # creation carousel swaps to these players; thin roster rows left it
    # showing the coach's club squad. owned_team also fills the bench.
    team = owned_team(r["team"])
    return _json({**r, **club_summary_response(int(teamid)), "team": team, "teamid": int(teamid)})


def ep_l2_calendar(query, body):
    """POST /league2/calendar {difficulty, sponsor}: start a season."""
    req = json.loads(body or b"{}")
    st = _state(); teamid = my_teamid(st)
    lid = roster_summary(teamid).get("league", {}).get("id", 0)
    teams = league_teams(lid, teamid)
    day = int(time.time() // 86400 * 86400 * 1000)  # today 00:00 UTC, ms
    st["league2"] = {"teamid": teamid, "leagueid": lid, "difficultyselect": int(req.get("difficulty") or 10),
                     "sponsor": req.get("sponsor"), "laststatus": "opening", "openingday": day,
                     "sponsoroffice": {"season": {"status": "started"}},
                     "league": {"teams": teams, "maxround": 2 * (len(teams) - 1) - 1, "nextround": 0,
                                "matchrecords": [], "statistics": league_stats(teams, [])}}
    _save(st)
    return _json({"result": True, "resources": l2_resources(st)})


def league_play_round(st, rnd, mine=None, seed=None):
    """Play league round rnd: my fixture gets `mine` (home, away goals) or is simulated,
    the rest is simulated. Updates table + laststatus, returns the client's leagueparam."""
    lg = st["league2"]; league = lg["league"]
    if any(m["round"] == rnd for m in league["matchrecords"]):
        return {"matchtype": "league", "round": rnd, "leaguematches": [m for m in league["matchrecords"] if m["round"] == rnd]}
    rng = random.Random(seed)
    me = my_teamid(st); played = []
    for h, a in league_round(league["teams"], rnd):
        if h is None or a is None:
            continue
        if mine is not None and me in (h, a):
            gh, ga = mine
        else:
            gh, ga = sim_score(rng, team_strength(st, h), team_strength(st, a))
        played.append({"type": "league", "matchtype": "league", "round": rnd, "home": h, "away": a,
                       "goals": {"home": gh, "away": ga}})
    league["matchrecords"] += played
    league["statistics"] = league_stats(league["teams"], league["matchrecords"], league["statistics"])
    league["nextround"] = rnd + 1
    lg["laststatus"] = f"league:{rnd}"
    return {"matchtype": "league", "round": rnd, "leaguematches": played}


def league_match_body(st, req, playtype):
    me = coach_me(st, _default_slot(st)); teamid = me["teamid"]
    opp = req["away"] if req["home"] == teamid else req["home"]
    user = {"id": me["id"], "name": me["name"], "levelinfo": me["levelinfo"]}
    lg = st["league2"]
    match = new_match("League_2_league_MGR" if playtype == "mgr" else "League_2_league",
                      match_side(st["teams"][str(_default_slot(st))], teamid, user, _tactics(st)[1]),
                      match_side(roster_team(opp), opp))
    gs = match["gamesettings"]
    gs["home"] = req["home"] == teamid  # I am always the left side
    gs["kit"] = ["home", "away"] if gs["home"] else ["away", "home"]
    # league level 10..24: the in-match overlay shows LEAGUE2_DIFFICULTY_<difficulty - 9>
    gs["difficulty"] = lg.get("difficultyselect", 10)
    # MatchProxy.leagueparam: LeagueCupGameSettingScreen.onPlaying reads leagueparam.matchtype
    # unguarded (`p != null && t == "cupqualifier" || p.matchtype == "cupfinal"`) -> required
    match["leagueparam"] = {"matchtype": req.get("matchtype", "league"), "round": req.get("round", 0),
                            "home": req["home"], "away": req["away"], "leagueid": lg.get("leagueid")}
    return match


def sim_events(match, mine, opp, rng=None):
    """Build eventlist consumed by LeagueCupFrontScreenSimulation.rebuildSimulationEvent.

    Team 0 is the user's left-hand side and team 1 the opponent. GOAL_EVENT entries drive the
    on-screen score; time-ordered shots, saves and cards animate the minute-by-minute match.
    """
    rng = rng or random
    xi = [[(int(p["spid"]), p["pos"]) for p in match["side"][s]["team"]["squad"]["positions"]
           if p["pos"] < POS_SUB] for s in ("left", "right")]
    keeper = [next((spid for spid, pos in team if pos == 0), team[0][0]) for team in xi]

    def outfield(team, weights=(1, 3, 6)):
        field = [(spid, weights[0] if pos <= 8 else weights[1] if pos <= 19 else weights[2])
                 for spid, pos in xi[team] if pos]
        return rng.choices([spid for spid, _ in field], [weight for _, weight in field])[0]

    events = []
    for team, goals in ((0, int(mine)), (1, int(opp))):
        for _ in range(max(0, goals)):
            minute, scorer = rng.randint(1, 90), outfield(team)
            if rng.random() < .6:
                events.append({"time": minute, "team": team, "player": outfield(team, (2, 4, 2)),
                               "event": "ASSIST_EVENT"})
            events.append({"time": minute, "team": team, "player": scorer, "event": "GOAL_EVENT"})
    for _ in range(rng.randint(3, 7)):
        team, minute = rng.randint(0, 1), rng.randint(1, 90)
        events.append({"time": minute, "team": team, "player": outfield(team), "event": "SHOT_ON_GOAL_EVENT"})
        if rng.random() < .6:
            events.append({"time": minute, "team": 1 - team, "player": keeper[1 - team], "event": "SAVE_EVENT"})
    for _ in range(rng.randint(0, 3)):
        team = rng.randint(0, 1)
        events.append({"time": rng.randint(1, 90), "team": team, "player": outfield(team, (4, 3, 1)),
                       "event": "YELLOW_CARD_EVENT"})
    return sorted(events, key=lambda event: event["time"])


def sim_result(match, events, my_home, rng=None):
    """Create match.result from the same events so timeline, score, stats and reward agree."""
    rng = rng or random
    pair = lambda lo, hi, rate: (lambda n: [round(n * rate), n])(rng.randint(lo, hi))
    teams = []
    for team_idx, side_name in ((0, "left"), (1, "right")):
        positions = match["side"][side_name]["team"]["squad"]["positions"]
        players = []
        for pos in (p for p in positions if p["pos"] < POS_SUB):
            player_events = [e["event"] for e in events
                             if e["team"] == team_idx and e["player"] == int(pos["spid"])]
            goals = player_events.count("GOAL_EVENT")
            assists = player_events.count("ASSIST_EVENT")
            shots = player_events.count("SHOT_ON_GOAL_EVENT")
            saves = player_events.count("SAVE_EVENT")
            yellow = player_events.count("YELLOW_CARD_EVENT")
            pid, start = int(pos["spid"]) % 1000000, pos.get("starting") or [1, 1, .5, .5]
            players.append({
                "id": str(pid), "spid": pid, "dbid": pid, "role": int(pos["pos"]),
                "x": start[2], "y": round(1 - start[3], 4),
                "rating": round(min(10.0, max(4.0, 6.0 + goals + .5 * assists + .3 * saves
                                                   - .4 * yellow + rng.uniform(-.4, .9))), 1),
                "goals": goals, "assists": assists, "saves": saves, "shotsOnGoal": goals + shots,
                "shotAttempt": goals + shots + rng.randint(0, 1) * bool(pos["pos"]),
                "passes": pair(20, 50, rng.uniform(.7, .92)) if pos["pos"] else pair(5, 15, .8),
                "dribbles": pair(0, 6, .6), "tackles": pair(0, 5, .6) if pos["pos"] else [0, 0],
                "cards": [min(yellow, 1), int(yellow >= 2)], "injure": [0, 0],
                "runDistanceMeter": round(rng.uniform(8000, 12000) if pos["pos"] else rng.uniform(3000, 5000), 1),
                "longTermStamina": 1})
        total = lambda key: sum(p[key] for p in players)
        sums = lambda key: [sum(p[key][0] for p in players), sum(p[key][1] for p in players)]
        teams.append({
            "id": match["side"][side_name]["team"]["teamid"], "score": total("goals"), "goals": total("goals"),
            "assists": total("assists"), "saves": total("saves"), "shotsOnGoal": total("shotsOnGoal"),
            "shotAttempt": total("shotAttempt"), "passes": sums("passes"), "dribbles": sums("dribbles"),
            "tackles": sums("tackles"), "cards": sums("cards"), "crossPasses": pair(5, 20, .35),
            "throughPasses": pair(2, 10, .4), "blocks": pair(0, 5, .5),
            "fouls": sums("cards")[0] + rng.randint(2, 8), "offsides": rng.randint(0, 3),
            "clearances": rng.randint(5, 20), "intercepts": rng.randint(5, 20),
            "tackles_inbox": 0, "passes_inbox": 0, "intercepts_inbox": 0, "dribble_inbox": 0,
            "rating": round(sum(p["rating"] for p in players) / max(1, len(players)), 2),
            "runDistanceKm": round(sum(p["runDistanceMeter"] for p in players) / 1000, 1),
            "players": players, "substitutes": [], "users": [], "shootout": 0, "injuredPlayers": 0,
            "reserved": [{"id": str(int(p["spid"]) % 1000000), "rating": 0,
                          "dbid": int(p["spid"]) % 1000000} for p in positions if p["pos"] == POS_SUB]})
    share = rng.randint(40, 60)
    for team, possession in zip(teams, (share, 100 - share)):
        left = rng.randint(15, 45); center = rng.randint(20, 100 - left - 10)
        team["possession"] = {"total": possession, "left": left, "center": center, "right": 100 - left - center,
                              "df": rng.randint(10, 30), "mf": rng.randint(40, 60), "fw": rng.randint(10, 30)}
    mine, opponent = ("home", "away") if my_home else ("away", "home")
    sides = {mine: teams[0], opponent: teams[1]}
    team_of, best = max(((name, p) for name, team in sides.items() for p in team["players"]),
                        key=lambda item: item[1]["rating"])
    return {"myTeam": mine, "opponentTeam": opponent, "home": sides["home"], "away": sides["away"],
            "matchFinishedTime": 5400, "forfeits": 0,
            "motm": {"team": team_of, "id": best["id"], "spid": best["spid"], "dbid": best["dbid"],
                     "rating": best["rating"], "name": "XXDISPLAYNAMEXX"}}


def ep_l2_match(query, body, sim=False):
    """POST /league2/match {matchtype, playtype, round, home, away, date} -> a /match/<id>
    resource (MatchProxy.registerByResource). /match/sim plays the round right away."""
    req = json.loads(body or b"{}")
    st = _state(); lg = st.get("league2")
    if not lg or not lg.get("league"):
        return _json({"result": False, "reason": "LEAGUE_FAIL"})
    ticket = None
    if sim:
        coach = my_coach(st)
        ticket = next((item for item in coach.get('items', []) if item['itemid'] == 2001 and item.get('remains', 0) > 0), None)
        if ticket is None:
            return _json({'result': False, 'reason': 'ITEM_USE_ERROR'})
    match = league_match_body(st, req, req.get("playtype"))
    st["match"] = match
    lg["pending"] = {"id": match["id"], "round": req.get("round", 0), "home": req["home"], "away": req["away"]}
    out = {"result": True}
    if sim:
        param = league_play_round(st, req.get("round", 0))
        mine = next((m for m in param["leaguematches"] if my_teamid(st) in (m["home"], m["away"])), None)
        match.update(stage="closed", simulated=True)
        match["gamesettings"]["simulated"] = True
        if mine:
            is_home = int(req["home"]) == int(my_teamid(st))
            home_goals = int(mine["goals"]["home"])
            away_goals = int(mine["goals"]["away"])
            mine_goals, opponent_goals = (home_goals, away_goals) if is_home else (away_goals, home_goals)
            rng = random.Random(int(match["id"][:8], 16))
            events = sim_events(match, mine_goals, opponent_goals, rng)
            match["gamesettings"]["prevScore"] = {"home": home_goals, "away": away_goals}
            match["result"] = sim_result(match, events, is_home, rng)
            # A simmed league match is a played match for the squad: fold
            # its per-player lines into the season records, like a played
            # match's /match/<id>/end does. Without this the league Data
            # tab's player table stayed all "-" for anyone who sims
            # (user report 2026-11-12); sim_result already builds the
            # exact home/away player arrays the recorder consumes.
            record_league_match_players(st, match["result"], f"league:{req.get('round', 0)}")
            match["reward"] = match_reward(st, match["result"])
            add_ep(st, match["reward"]["money"]["reward"]["total"])
        else:
            events = []
        out.update(leagueparam=param, eventlist=events, score=mine and mine["goals"])
        ticket['remains'] -= 1
        if ticket['remains'] == 0:
            coach['items'].remove(ticket)
    if sim:
        # A simulated league match is still a played match for the squad
        # that took the field: contracts tick down (user 2026-11-12 --
        # after a sim match everyone stayed at 40).
        consume_match_contracts(st)
    _save(st)
    out["resources"] = [{"uri": f"/match/{match['id']}", "body": match}] + (l2_resources(st) if sim else [])
    if sim:
        out['resources'] += coach_me_resources(st, _default_slot(st))
    return _json(out)


def ep_l2_sleep(query, body):
    """POST /league2/sleep {matchtype, round}: my bye round; the others still play."""
    req = json.loads(body or b"{}")
    st = _state(); lg = st.get("league2")
    param = league_play_round(st, req.get("round", 0)) if lg and lg.get("league") else {}
    _save(st)
    return _json({"result": True, "leagueparam": param, "resources": l2_resources(st)})


def ep_l2_finish(query, body):
    """PUT /league2/abandon: give up the season (back to the creation screen)."""
    st = _state()
    lg = st.get("league2") or {}
    st["league2"] = {"difficultyselect": lg.get("difficultyselect", 10)}
    _save(st)
    return _json({"result": True, "resources": l2_resources(st)})


def season_leaders(st, me, rank, goals):
    """leaderRewards for LeagueCupLeagueRewardScreen: best XI by line (FW/MF/DF/GK, 11 in
    all) + one leader per stat. ponytail: no per-player season stats are kept, so these
    come from my own squad (rating = card rating, stat values derived from team goals)."""
    team = owned_team(st["teams"][str(_default_slot(st))])
    rating = {p["spid"]: player_ovr(p) for p in team["players"]}
    lines = {"GK": range(0, 1), "DF": range(1, 9), "MF": range(9, 20), "FW": range(20, 28)}
    starters = [p for p in team["squad"]["positions"] if p["pos"] < POS_SUB]
    entry = lambda spid, value, ep=0: {"spid": spid, "teamid": me, "value": value, "reward": {"count": ep}}
    out = {line: [entry(p["spid"], round(min(9.9, 6 + (rating.get(p["spid"], 80) - 80) / 25), 1))
                  for p in starters if p["pos"] in r] for line, r in lines.items()}
    best = lambda line: max((p["spid"] for p in starters if p["pos"] in lines[line]),
                            key=lambda s: rating.get(s, 0), default=0)
    played = 2 * (len((st.get("league2") or {}).get("league", {}).get("teams", [])) - 1)
    out.update(ratings=[entry(best("FW"), 7.5, 3000)], goals=[entry(best("FW"), max(1, goals // 2), 3000)],
               assists=[entry(best("MF"), max(1, goals // 3), 2000)], tackles=[entry(best("DF"), played * 3, 2000)],
               passes=[entry(best("MF"), 0.85, 1000)],  # pass success rate; the UI shows value*100 %
               saves=[entry(best("GK"), played * 2, 1000)])
    return out


def ep_l2_end(query, body):
    """PUT /league2/end (closing day): season result for LeagueCupLeagueRewardScreen
    {promotion{current,next}, rewards{coach,money.reward{total,detail}}, sponsorRewards,
    leaderRewards, seasonRewards{items}}. The season stays (the reward screen reads it) but
    is marked finished: back on the front, the closing day then has seasonoff -> ON_CLOSED
    -> the creation screen, whose POST /league2/calendar starts the next season."""
    st = _state(); lg = st.get("league2") or {}; league = lg.get("league")
    if not league:
        return _json({"result": False, "reason": "LEAGUE_FAIL"})
    me = my_teamid(st); stats = league["statistics"][str(me)]; rank = stats["rank"]
    cur = lg.get("difficultyselect", 10)
    league_size = league_team_count(lg.get("leagueid"), league.get("teams"))
    if cur >= 20:  # ultimate: promote top 3, demote bottom 3
        nxt = min(24, cur + 1) if rank <= 3 else max(20, cur - 1) if rank > league_size - 3 else cur
    else:
        nxt = min(19, cur + 1) if rank <= 3 else cur
    mid = SPONSORS.get(str(lg.get("sponsor")), "2")
    progress, done = mission_progress(st, mid)
    with open(os.path.join(RESPONSES, "static_db_LEAGUE2REWARD.json")) as f:
        sponsor_ep = json.load(f)["sponsor"].get(str(int(mid) - 1), {}).get(str(cur), {}).get("count", 0) if done else 0
    leaders = season_leaders(st, me, rank, stats["goals"])
    leader_ep = sum(e["reward"]["count"] for k in ("ratings", "goals", "assists", "tackles", "passes", "saves")
                    for e in leaders[k])
    base_ep = (league_size - rank + 1) * 2000
    season_exp = (league_size - rank + 1) * 100
    cprev, ccur = gain_exp(my_coach(st), season_exp, coach_need, COACH_MAX_LEVEL)
    out = {"result": True, "promotion": {"current": cur, "next": nxt},
           # setRewardRankExp: coach.prev/current/reward.total; setRewardRankEP: money.prev +
           # reward.detail.season; the sponsor/leader scenes read detail.sponsor/.leader
           "rewards": {"coach": {"prev": cprev, "current": ccur, "reward": {"total": season_exp}},
                       "money": {"prev": coach_me(st, _default_slot(st))["money"]["ep"],
                                 "reward": {"total": base_ep + sponsor_ep + leader_ep,
                                            "detail": {"season": base_ep, "sponsor": sponsor_ep, "leader": leader_ep}}}},
           "sponsorRewards": {"sponsorid": str(lg.get("sponsor")), "level": 1, "achievement": done,
                              "missions": [{"mid": mid, "achievement": done, "progress": progress}]},
           "leaderRewards": leaders, "seasonRewards": {"items": []}}
    lg.update(difficultyselect=nxt, sponsoroffice={"season": {"status": "finished"}})
    add_ep(st, out["rewards"]["money"]["reward"]["total"])
    _save(st)
    out["resources"] = l2_resources(st)
    return _json(out)


def match_step(mid, body, step):
    """Apply step(match, request) to the live league match (state["match"]) and answer with
    the updated /match/<id> resource; MatchController re-runs processMatchStageResource."""
    req = json.loads(body or b"{}")
    st = _state(); match = st.get("match")
    if not match or match["id"] != mid:
        return _json({"result": False})
    step(match, req)
    _save(st)
    return _json({"result": True, "resources": [{"uri": f"/match/{mid}", "body": match}]})



def ep_match_squad(mid, side, query, body):
    """Save the user's pre-match XI; instance ids are authoritative after a swap."""
    import copy
    st = _state()
    match = st.get("match")
    if not match or match.get("id") != mid or match.get("stage") not in ("created", "squad", "setting"):
        return _json({"result": False})
    current = match.get("side", {}).get(side)
    if not current or not current.get("players"):
        return _json({"result": False})
    req = json.loads(body or b"{}")
    sq = req.get("squad") or {}
    cards = {iid(p): p for p in current["team"]["players"]}
    positions = []
    for entry in sq.get("positions") or []:
        pid, pos = str(entry.get("id")), entry.get("pos")
        if pid not in cards or type(pos) is not int or not 0 <= pos <= POS_SUB:
            return _json({"result": False})
        positions.append({**entry, "id": pid, "spid": cards[pid]["spid"]})
    starters = [p for p in positions if p["pos"] < POS_SUB]
    if (len(starters) != 11 or len({p["pos"] for p in starters}) != 11
            or len({p["id"] for p in positions}) != len(positions)
            or len(positions) > 18 or not any(p["pos"] == 0 for p in starters)):
        return _json({"result": False})
    team = copy.deepcopy(current["team"])
    team["squad"].update({k: sq[k] for k in ("formation", "workrate") if k in sq})
    team["squad"]["positions"] = positions
    for key in ("captain", "kickers"):
        if key in req or key in sq:
            team["squad"][key] = req.get(key, sq.get(key))
    repair_squad(team)
    team["captain"] = team["squad"]["captain"]
    team["kickers"] = team["squad"]["kickers"]
    team["teamcolors"] = active_teamcolors(team)
    team["summary"] = team_summary(team, team["teamid"])
    current["team"] = team
    current["squad"] = team["squad"]
    _save(st)
    return _json({"result": True, "resources": [{"uri": f"/match/{mid}", "body": match}]})


def ep_match_gamesettings(mid, query, body):
    """Persist the pre-match shirt selection and return the updated match resource."""
    req = json.loads(body or b"{}")
    st = _state()
    match = st.get("match")
    side, kit = req.get("side"), req.get("kit")
    if (not match or match["id"] != mid or match.get("stage") != "setting"
            or type(side) is not int or side not in (0, 1) or kit not in ("home", "away")):
        return _json({"result": False})
    match["gamesettings"]["kit"][side] = kit
    _save(st)
    return _json({"result": True, "resources": [{"uri": f"/match/{mid}", "body": match}]})


def _side_ready(side):
    def step(match, req):
        if side in match["side"]:
            match["side"][side]["ready"] = bool(req.get("ready", True))
        if req.get("ready", True):
            match["stage"] = "playing"
    return step


def ep_match_ready(mid, side, query, body):
    """PUT /match/<id>/side/<side>/ready {ready, stage}: single-player, so the match starts
    (stage playing -> LeagueCupGameSettingScreen.onPlaying -> StartSingleMatch)."""
    return match_step(mid, body, _side_ready(side))


NEXT_STAGE = {"created": "squad", "squad": "setting"}


def _player_ready(match, req):
    if req.get("ready", True) and req.get("stage", match["stage"]) == match["stage"]:
        match["stage"] = NEXT_STAGE.get(match["stage"], match["stage"])


def ep_match_player_ready(mid, query, body):
    """PUT /match/<id>/player/ready {ready, stage}: created -> squad (ON_SQUADING: squad screen),
    squad -> setting (LeagueCupSquadSettingHandler.updateStage: game setting screen)."""
    return match_step(mid, body, _player_ready)


# Post-match reward cards (Bug 2b): PostMatchRewardDefaultScene deals the
# cards from reward.extra.availables and reveals reward.extra.selected --
# with both empty the scene skipped the card step entirely. The pick is the
# server's: 3 cards, index 0 is the player's and is credited here (every
# match_reward caller settles a match exactly once). Card 4 in the scene
# is the PC-bang slot, not server data.
MATCH_CARD_POOL = [(30, ("ep", 25000)), (25, ("ep", 75000)), (15, ("ep", 125000)),
                   (8, ("ep", 250000)), (10, 28001), (7, 45013), (5, 82214180)]


def _match_cards_reward(st):
    pool = [(w, p) for w, p in MATCH_CARD_POOL
            if isinstance(p, tuple) or p in PACKS or p in ITEMS]
    if not pool:
        return {"extra": {"selected": {}, "availables": []}, "selected": {}}
    cards = random.choices([p for _, p in pool], [w for w, _ in pool], k=3)
    if isinstance(cards[0], tuple):
        add_ep(st, cards[0][1])
    else:
        inventory_add(my_coach(st), cards[0], 1)
    pick = {"normal": [0]}
    availables = [
        {"itemid": EP_ITEM, "count": p[1], "staticdata": EP_ITEM_DEF}
        if isinstance(p, tuple) else
        {"itemid": p, "count": 1, "staticdata": item_def(p)}
        for p in cards]
    return {"extra": {"selected": pick, "availables": availables}, "selected": pick}


def match_reward(st, stats):
    """match.reward for PostMatchRewardDefaultScene.onRewardReceived (paths it reads) and its
    player list (PlayerRewardListItemRenderer: id, spid, prev, current, reward.detail.extra).
    Credits EP, coach exp and exp for every player of mine in matchStats (in-match spid = the
    card's player id, i.e. spid % 1e6). ponytail: flat exp by result + goals/assists."""
    me, opp = stats.get("myTeam", "home"), stats.get("opponentTeam", "away")
    g = lambda s: int((stats.get(s) or {}).get("score") or 0)
    won, drew = g(me) > g(opp), g(me) == g(opp)
    ep = 50_000 if won else 25_000 if drew else 12_500  # x25 scale (user, 2026-11-12)
    cexp, pexp = EXP_WIN if won else EXP_DRAW if drew else EXP_LOSS
    reward = lambda n: {"total": n, "detail": {"base": n, "extra": 0}}
    # Bug 2b: the EP panel animates the ACCOUNT balance money.prev ->
    # money.current (startEPLabelEffect); with both missing it rendered
    # blank. Read the balance before this match's credits land (the card
    # credit below and the caller's add_ep(money total) come after).
    ep_before = int(my_coach(st).get("ep") or 0)
    prev, cur = gain_exp(my_coach(st), cexp, coach_need, COACH_MAX_LEVEL)
    team, done, players = st["teams"].get(str(_default_slot(st))) or {"players": []}, set(), []
    team['staffsmc'] = int(team.get('staffsmc') or 0) + 1
    for s in (stats.get(me) or {}).get("players") or []:
        pid = int(s.get("spid") or 0) % 1000000
        mine = [p for p in team["players"] if int(p["spid"]) % 1000000 == pid and iid(p) not in done]
        p = next((p for p in mine if in_squad(team, p)), mine[0] if mine else None)
        if p is None:
            continue
        n = pexp + 20 * int(s.get("goals") or 0) + 10 * int(s.get("assists") or 0)
        pprev, pcur = gain_exp(p, n, player_need, PLAYER_MAX_LEVEL)
        done.add(iid(p))
        players.append({"id": iid(p), "spid": p["spid"], "prev": pprev, "current": pcur, "reward": reward(n)})
    return {"matchRewardBackType": 0, **_match_cards_reward(st),
            "coach": {"reward": reward(cexp), "prev": prev, "current": cur},
            "money": {"reward": reward(ep), "prev": ep_before, "current": ep_before + ep},
            "players": players, "missions": [], "exptankinfo": {}}


def ep_match_get(mid, query, body):
    """GET /match/<id>: the post-match screens re-read result/reward from the match."""
    match = _state().get("match") or {}
    return _json(match if match.get("id") == mid else {})


def consume_match_contracts(st):
    """Consume one contract match for each player registered in the user's active squad."""
    slot = _default_slot(st)
    team = (st.get("teams") or {}).get(str(slot)) or {}
    players = team.get("players") or []
    squad = team.get("squad") or {}
    registered = {str(pos.get("id")) for pos in squad.get("positions") or [] if pos.get("id")}
    if not registered:
        registered = {iid(p) for p in players if not p.get("team2")}
    consumed = 0
    for player in players:
        if iid(player) not in registered:
            continue
        # life None must mean a fresh 40-match contract: the old
        # .get("life", 100) returned the stored None and `or 0` zeroed it,
        # so no contract ever ticked down (user report 2026-11-12).
        remaining = _player_life(player)
        if remaining:
            player["life"] = remaining - 1
            consumed += 1
    return consumed


def ep_match_end(mid, query, body):
    """PUT /match/<id>/end matchStats {home:{id, score, players...}, away:{...}, myTeam,
    opponentTeam, motm, ...}. It comes back as match.result (PostMatchResultDefaultScene
    .onResultReceived reads exactly these fields) plus a match.reward."""
    req = json.loads(body or b"{}")
    st = _state(); lg = st.get("league2") or {}; p = lg.get("pending")
    match = st.get("match") if (st.get("match") or {}).get("id") == mid else None
    out = {"result": True}
    if match is not None and match.get("stage") != "closed":  # settle once, even if client retries
        consume_match_contracts(st)
        match["result"] = {k: v for k, v in req.items() if k != "_method"}
        match["reward"] = match_reward(st, req)
        add_ep(st, match["reward"]["money"]["reward"]["total"])
        if p and p.get("id") == mid:
            record_league_match_players(st, req, f"league:{p.get('round')}")
    cp = st.get("cup_pending") or {}
    if cp.get("id") == mid and cp.get("cupid") in (st.get("cups") or {}):
        h, a = req.get("home") or {}, req.get("away") or {}
        gh, ga = int(h.get("score") or 0), int(a.get("score") or 0)
        sh, sa = int(h.get("shootout") or 0), int(a.get("shootout") or 0)
        if str(h.get("id")) == str(cp["away"]):
            gh, ga, sh, sa = ga, gh, sa, sh
        st["cups"][cp["cupid"]]["_mine"] = (gh, ga, sh, sa)  # applied by PUT /cup/<id>/next
        st.pop("cup_pending")
    if p and p["id"] == mid:
        h, a = req.get("home") or {}, req.get("away") or {}
        gh, ga = int(h.get("score") or 0), int(a.get("score") or 0)
        if str(h.get("id")) == str(p["away"]):  # matchStats home/away are match sides
            gh, ga = ga, gh
        out["leagueparam"] = league_play_round(st, p["round"], (gh, ga))
        lg.pop("pending", None)
        out["resources"] = l2_resources(st)
    if match is not None:
        match["stage"] = "closed"
        out["resources"] = [{"uri": f"/match/{mid}", "body": match}] + out.get("resources", [])
    _save(st)
    return _json(out)


# --------------------------------------------------------------------------- #
# Single > Cup (PvE cups; also the "Eropa/Amerika Selatan" shortcut). CupProxy/CupController.
#   GET /cup/list -> metas the front screen lists; GET /cup/record -> my cup records.
# --------------------------------------------------------------------------- #
def cup_meta(metaid, name, trophy, quality, matchcount, teams, reward):
    """A /cup/list entry: CupFrontScreen.onCupDetailInternal + CupProxy.onCupListReceived
    fields. difficulty = minimum AI level index (0 amateur .. 4 legendary)."""
    # visible: Boolean (isVisibleValue indexes it per condition otherwise); joinable.allteams
    # skips the teamids/leagueids whitelist ("Tim ini tidak dapat bergabung")
    return {"metaid": metaid, "_id": str(metaid), "name": name, "type": "pve", "teamselect": "user", "visible": True,
            "difficulty": 0, "quality": quality, "matchcount": matchcount, "teamcount_desc": teams,
            "proviso_desc": "PVECUP_INFO_OPTION_NO", "trophy": trophy, "skin": str(metaid),
            # onCupDetailInternal sums bestrewards[-1][*].count where itemid == 1 (EP)
            "bestrewards": [[{"itemid": 1, "count": reward}]],
            "startat": "2026-11-01T00:00:00.000Z", "endat": "2030-12-31T00:00:00.000Z",
            "joinable": {"fee": 0, "igr": False, "vip": 0, "allteams": True, "dayofweek": [1, 1, 1, 1, 1, 1, 1]}}


CUPS = [cup_meta(1, "European Champions Cup", "66", 4, 6, 16, 60000),  # 3 group + QF/SF/F
        cup_meta(2, "South American Cup", "300000", 3, 6, 16, 40000)]
# The client has native World Cup cup metadata/localization IDs 10001-10004
# (PVE_CUP_MATCH_TITLE_10001..10004). 1001 is not a recognized native ID and
# leaves the World Cup entry without its expected client metadata.
WORLD_CUP_METAID = 10001
WORLD_CUP = cup_meta(WORLD_CUP_METAID, "World Cup", "105", 4, 7, 32, 500000)
WORLD_CUP["teamselect"] = "authentic"
WORLD_CUP["joinable"]["allteams"] = False
WORLD_CUP["joinable"]["leagueids"] = [78]
WORLD_CUP["joinable"]["teamids"] = list(_NATIONAL_TEAMS.keys())  # World Cup screen supplies the chosen national teamid.
WORLD_CUP["type"] = "pve"


def ep_cup_list(query, body):
    st = _state()
    ev = st.get("events") or {}
    season_end = str(ev.get("ladder_season_end", "2026-12-31")).strip()
    end_iso = f"{season_end}T23:59:59.000Z"
    for c in [WORLD_CUP, *CUPS]:
        c["endat"] = end_iso
    active_cups = set(str(c) for c in ev.get("active_cups", ["1", "2", "10001"]))
    all_cups = [WORLD_CUP, *CUPS]
    filtered = [c for c in all_cups if str(c.get("metaid")) in active_cups]
    if not filtered:
        filtered = [WORLD_CUP] if "type=wcpve" in (query or "") else [WORLD_CUP, *CUPS]
    return _json([c for c in filtered if c.get("metaid") == WORLD_CUP_METAID] if "type=wcpve" in (query or "") else filtered)


def ep_worldcup(query, body):
    """World Cup status resource consumed by the client's native WCFrontScreen."""
    st = _state()
    ongoing = next((cup for cup in (st.get("cups") or {}).values()
                    if cup.get("mode") == "worldcup" and not cup.get("finished")
                    and any(c.get("state") == "playing" for c in cup.get("coaches", []))), None)
    pve = {"ongoing": ongoing is not None}
    if ongoing is not None:
        pve["cupid"] = ongoing["_id"]
    return _json({"_id": _real_coach_id(st), "pve": pve,
                  "pvp": {"state": "idle", "coach": None},
                  "live": {}, "livematch": None})


def cup_pool(metaid, mine):
    """AI opponents for club cups, or other playable national teams in World Cup PvE."""
    if metaid == WORLD_CUP_METAID:
        return [tid for tid in sorted(_NATIONAL_TEAMS)
                if tid != mine and roster(tid)["team"].get("players")][:31]
    with closing(_db()) as c:
        rows = c.execute("""SELECT p.teamid FROM players p JOIN permid m ON m.spid = p.spid AND m.licensed = 1
                            WHERE p.spid < 1000000 GROUP BY p.teamid HAVING COUNT(*) >= 16
                            ORDER BY MAX(p.overallrating) DESC LIMIT 60""").fetchall()
    ids = [r[0] for r in rows if r[0] != mine]
    if metaid == 1:
        return ids[:15]
    # South American Cup: the old code dealt the NEXT slice of the same
    # global top-60, so the "South American" cup was full of European
    # clubs. Draw from the South American leagues in the data instead
    # (353 = Argentina, 336 = Colombia; 47 clubs meet the roster bar).
    with closing(_db()) as c:
        sa = c.execute("""SELECT p.teamid FROM players p JOIN permid m ON m.spid = p.spid AND m.licensed = 1
                            WHERE p.spid < 1000000 AND p.leagueid IN (353, 336)
                            GROUP BY p.teamid HAVING COUNT(*) >= 16
                            ORDER BY MAX(p.overallrating) DESC LIMIT 40""").fetchall()
    sa_ids = [r[0] for r in sa if r[0] != mine]
    return (sa_ids + [i for i in ids if i not in sa_ids])[:15]


def _real_coach_id(st):
    """The coach's stored _id. fo3_ext migrates coach ids to per-user
    values (65f1...), while coach_id(slot) is only the pre-migration
    default (000...001). Client proxies match a cup's 'coach' markers
    against the /coach/me id, so cups must carry the real one --
    with the default id CupProxy.myTeam stays null and the cup draw
    screens render blank and never enable their Next button."""
    slot = _default_slot(st)
    coaches = st.get("coaches") or []
    for c in coaches:
        if int(c.get("slot", 0)) == slot and c.get("_id"):
            return str(c["_id"])
    for c in coaches:
        if c.get("_id"):
            return str(c["_id"])
    return coach_id(slot)


def heal_cup_coach_ids(st, cup):
    """Rewrite a stored cup's owner markers to the real coach id
    (cups created before the fix carry 000...001). Single-human cups:
    every non-null coach marker belongs to the default coach."""
    real = _real_coach_id(st)
    for c in cup.get("coaches") or []:
        if c.get("_id") != real:
            c["_id"] = real
    def fix(t):
        if isinstance(t, dict) and t.get("coach") and t["coach"] != real:
            t["coach"] = real
    for t in cup.get("teams") or []:
        fix(t)
    for phase in cup.get("phases") or []:
        for g in phase.get("groups") or []:
            for t in g.get("teams") or []:
                fix(t)


def cup_team(st, teamid, user_teamid=None):
    me = my_teamid(st) if user_teamid is None else user_teamid
    return {"teamid": teamid, "coach": _real_coach_id(st) if teamid == me else None}


def new_cup(st, meta, difficulty, selected_teamid=None):
    """Create group + knockout cup fixtures. World Cup uses 32 national squads (8 groups);
    the regular cup mode keeps its 16-club format (4 groups)."""
    is_worldcup = meta["metaid"] == WORLD_CUP_METAID
    me = int(selected_teamid or my_teamid(st)) if is_worldcup else my_teamid(st)
    teams = [me] + cup_pool(meta["metaid"], me)
    target_count = 32 if is_worldcup else 16
    teams = teams[:target_count]
    random.shuffle(teams)
    group_count = len(teams) // 4
    groups = [{"teams": [cup_team(st, t, me if is_worldcup else None) for t in teams[g * 4:g * 4 + 4]],
               "rounds": [{"shuffle": r == 0, "pairs": [{"legs": [{"homeidx": h, "awayidx": a}]}
                                                         for h, a in league_round([0, 1, 2, 3], r)]}
                          for r in range(3)]} for g in range(group_count)]
    cupid = f"{int(time.time()):08x}{os.urandom(8).hex()}"
    wc_roster = owned_team(roster_team(me)) if is_worldcup else None
    cup = {"_id": cupid, "metaid": meta["metaid"], "name": meta["name"], "type": "pve",
           "difficulty": difficulty, "mode": "worldcup" if is_worldcup else "clubcup",
           "skin": str(meta.get("skin", meta["metaid"])), "trophy": str(meta.get("trophy", "0")),
           "selectedteamid": me, "current": {"phase": 0, "round": 0, "leg": 0},
           "phases": [{"system": "round-robin", "name": "Vòng bảng", "maxrounds": 3, "groups": groups}],
           "coaches": [{"_id": _real_coach_id(st), "state": "playing"}],
           "teams": [cup_team(st, t, me if is_worldcup else None) for t in teams]}
    if is_worldcup and wc_roster:
        cup["team"] = wc_roster
        cup["squad"] = wc_roster.get("squad")
    cup_refresh_stats(cup)
    return cup


def cup_my_pair(cup):
    """(group, leg) of my fixture in the current round, or (None, None)."""
    cur = cup["current"]
    for g in cup["phases"][cur["phase"]]["groups"]:
        for pair in g["rounds"][cur["round"]]["pairs"]:
            leg = pair["legs"][cur["leg"]]
            idx = [leg.get("homeidx"), leg.get("awayidx")]
            if any(i is not None and g["teams"][i]["coach"] for i in idx):
                return g, leg
    return None, None


def group_table(g):
    """[(team index, points, goal diff, goals)] best first."""
    t = {i: [0, 0, 0] for i in range(len(g["teams"]))}
    for rnd in g["rounds"]:
        for pair in rnd["pairs"]:
            leg = pair["legs"][0]
            if "score" not in leg:
                continue
            gh, ga = leg["score"]["home"], leg["score"]["away"]
            for i, f, a in ((leg["homeidx"], gh, ga), (leg["awayidx"], ga, gh)):
                t[i][0] += 3 if f > a else 1 if f == a else 0
                t[i][1] += f - a; t[i][2] += f
    return sorted(t, key=lambda i: tuple(-v for v in t[i]))


def cup_refresh_stats(cup):
    """Per-team W/D/L over the whole cup + group points/rank on every team object (group
    tables, "Juara - <rank> (W/D/L)" next-match panel, "Grup C - Peringkat <rank>")."""
    wdl = {}
    goals = {}
    for phase in cup["phases"]:
        for g in phase["groups"]:
            for rnd in g["rounds"]:
                for pair in rnd["pairs"]:
                    leg = pair["legs"][0]
                    if "score" not in leg:
                        continue
                    for i, f, a in ((leg["homeidx"], leg["score"]["home"], leg["score"]["away"]),
                                    (leg["awayidx"], leg["score"]["away"], leg["score"]["home"])):
                        r = wdl.setdefault(g["teams"][i]["teamid"], [0, 0, 0])
                        r[0 if f > a else 1 if f == a else 2] += 1
                        pair_ga = goals.setdefault(g["teams"][i]["teamid"], [0, 0])
                        pair_ga[0] += f; pair_ga[1] += a
    for phase in cup["phases"]:
        for g in phase["groups"]:
            order = group_table(g) if phase["system"] == "round-robin" else range(len(g["teams"]))
            for rank, i in enumerate(order):
                t = g["teams"][i]; w, d, l = wdl.get(t["teamid"], [0, 0, 0])
                # The group table renders goal difference as goals -
                # goalsconceded (the league statistics dialect); without
                # the pair it showed NaN for every team.
                gf, ga = goals.get(t["teamid"], [0, 0])
                t.update(win=w, draw=d, lose=l, point=3 * w + d, rank=rank + 1,
                         goals=gf, goalsconceded=ga)


def cup_play_round(st, cup, mine=None):
    """Play the current round (my fixture = `mine` (home, away) if given, the rest simulated),
    then advance current / build the knock-out / settle my state."""
    cur = cup["current"]; phase = cup["phases"][cur["phase"]]; ko = phase["system"] == "knock-out"
    rng = random.Random()
    for g in phase["groups"]:
        for pair in g["rounds"][cur["round"]]["pairs"]:
            leg = pair["legs"][cur["leg"]]
            if "score" in leg or leg.get("homeidx") is None:
                continue
            h, a = g["teams"][leg["homeidx"]], g["teams"][leg["awayidx"]]
            played = mine is not None and (h["coach"] or a["coach"])
            if played:
                gh, ga = mine[:2]
            else:
                gh, ga = sim_score(rng, team_strength(st, h["teamid"]), team_strength(st, a["teamid"]))
            leg["score"] = {"home": gh, "away": ga}
            if h["coach"] or a["coach"]:
                record_match(st, "cup", *((gh, ga) if h["coach"] else (ga, gh)))
            if ko and gh == ga and played and len(mine) > 3 and mine[2] != mine[3]:  # shootout played in game
                leg["shootout"] = {"home": mine[2], "away": mine[3]}
            elif ko and gh == ga:  # penalties
                ph = rng.randint(3, 5); pa = ph + rng.choice((-1, 1)) if ph < 5 else ph - 1
                leg["shootout"] = {"home": ph, "away": pa}
    me_state = cup["coaches"][0]
    if not ko:
        if cur["round"] + 1 < len(phase["groups"][0]["rounds"]):
            cur["round"] += 1
            return
        # Each adjacent pair of groups supplies two qualifiers to the knockout bracket.
        tables = [group_table(g) for g in phase["groups"]]
        qual = []
        for x in range(0, len(phase["groups"]), 2):
            y = x + 1
            qual += [phase["groups"][x]["teams"][tables[x][0]], phase["groups"][y]["teams"][tables[y][1]],
                     phase["groups"][y]["teams"][tables[y][0]], phase["groups"][x]["teams"][tables[x][1]]]
        knockout_rounds = int(math.log2(len(qual)))
        rounds = [{"shuffle": True, "pairs": [{"legs": [{"homeidx": i, "awayidx": i + 1}]}
                                                     for i in range(0, len(qual), 2)]}]
        rounds.extend({"pairs": [{"legs": [{}]} for _ in range(len(qual) // (2 ** (r + 1)))]}
                      for r in range(1, knockout_rounds))
        cup["phases"].append({"system": "knock-out", "name": "World Cup knockout" if cup.get("mode") == "worldcup" else "Vòng loại trực tiếp",
                              "maxrounds": knockout_rounds, "groups": [{"teams": qual, "rounds": rounds}]})
        cup.update(current={"phase": cur["phase"] + 1, "round": 0, "leg": 0})
        if not any(t["coach"] for t in qual):
            me_state["state"] = "eliminated"
        return
    g = phase["groups"][0]
    winners = []
    for pair in g["rounds"][cur["round"]]["pairs"]:
        leg = pair["legs"][0]; s = leg.get("shootout") or leg["score"]
        winners.append(leg["homeidx"] if s["home"] > s["away"] else leg["awayidx"])
    my_idx = next(i for i, t in enumerate(g["teams"]) if t["coach"]) if any(t["coach"] for t in g["teams"]) else None
    if my_idx is not None and me_state["state"] == "playing" and my_idx not in winners and \
            any(my_idx in (p["legs"][0].get("homeidx"), p["legs"][0].get("awayidx")) for p in g["rounds"][cur["round"]]["pairs"]):
        me_state["state"] = "eliminated"
    if cur["round"] + 1 < len(g["rounds"]):
        for k, pair in enumerate(g["rounds"][cur["round"] + 1]["pairs"]):
            pair["legs"][0] = {"homeidx": winners[2 * k], "awayidx": winners[2 * k + 1]}
        cur["round"] += 1
    else:  # final played
        cup["finished"] = True
        if me_state["state"] == "playing":
            me_state["state"] = "won" if my_idx in winners else "eliminated"  # CupMainScreen._cupState


def cup_resources(cup):
    uri = f"/cup/{cup['_id']}"
    return [{"resource": uri, "uri": uri, "body": cup}]


def ep_cup_new(query, body):
    """POST /cup/new {metaid, difficulty, teamid, lineup}."""
    req = json.loads(body or b"{}")
    metaid = int(req.get("metaid") or 0)
    meta = WORLD_CUP if metaid == WORLD_CUP_METAID else next((m for m in CUPS if m["metaid"] == metaid), None)
    if meta is None:
        return _json({"result": False, "reason": "invalid metaid"})
    st = _state()
    ev = st.get("events") or {}
    configured_wc_team = int(ev.get("worldcup_teamid") or 0)
    selected_teamid = int(req.get("teamid") or 0) if metaid == WORLD_CUP_METAID else None
    if metaid == WORLD_CUP_METAID:
        if configured_wc_team in _NATIONAL_TEAMS:
            selected_teamid = configured_wc_team
        elif selected_teamid not in _NATIONAL_TEAMS:
            # Resolve club or unknown teamid to its corresponding national team (fallback to Spain 1362)
            s = roster_summary(selected_teamid) if selected_teamid else {}
            nid = s.get("nation", {}).get("id")
            ntid = _CLUB_NATIONS.get(nid, {}).get("teamid") if nid else None
            selected_teamid = ntid if ntid in _NATIONAL_TEAMS else 1362
    # One ongoing run per cup type: if this coach already has an unfinished
    # cup with the same metaid, hand THAT cup back instead of dealing a
    # second one. The hub intermittently fails to mark the run as joined
    # (user report 2026-11-12: three World Cups created in eight minutes,
    # all stored fine server-side), and every retry used to pile up another
    # duplicate run; resuming here keeps the flow moving either way.
    existing = next((c for c in (st.get("cups") or {}).values()
                     if c.get("metaid") == metaid and not c.get("finished")), None)
    if existing is not None:
        heal_cup_coach_ids(st, existing)
        _save(st)
        return _json({"result": True, "resources": cup_resources(existing)})
    cup = new_cup(st, meta, int(req.get("difficulty") or 0), selected_teamid)
    _cup_r = coach_record(my_coach(st), "cup")
    # Per-meta participation record: CupFrontScreen reads the hub's
    # wins/challenges + joincount from /cup/record's recordsbymetaid --
    # a new run counts as one challenge and one join for its meta.
    _bm = _cup_bymetaid(st, _cup_r)
    _ce = _bm.setdefault(str(metaid), {"joincount": 0, "wins": 0, "challenges": 0})
    _ce["challenges"] = int(_ce.get("challenges", 0)) + 1
    _ce["joincount"] = int(_ce.get("joincount", 0)) + 1
    st.setdefault("cups", {})[cup["_id"]] = cup
    _cup_r["totalchallenges"] = _cup_r.get("totalchallenges", 0) + 1
    _save(st)
    return _json({"result": True, "resources": cup_resources(cup)})


def cup_step(cupid, fn):
    st = _state(); cup = (st.get("cups") or {}).get(cupid)
    if cup is None:
        return _json({"result": False, "reason": "not exist cupid"})
    heal_cup_coach_ids(st, cup)
    out = fn(st, cup) or {}
    _save(st)
    return _json(dict({"result": True, "resources": cup_resources(cup)}, **out))


def ep_cup_resume(cupid, query, body):
    return cup_step(cupid, lambda st, cup: None)


def ep_cup_next(cupid, query, body):
    """PUT /cup/<id>/next: play out the current round (my result from the match just played,
    if any) and advance (CupController.onMatchEndReceived -> ON_CUP_SCHEDULE_NEXT_ROUND)."""
    def step(st, cup):
        if cup.get("finished"):
            return
        cup_play_round(st, cup, cup.pop("_mine", None))
        cup_refresh_stats(cup)
    return cup_step(cupid, step)


def cup_reward_ep(cup):
    """EP for an ended cup: the meta's prize for the champion, a share of it by the round
    reached otherwise (group exit 10%, QF 20%, SF 35%, final 50%); 0 while still playing."""
    state = cup["coaches"][0]["state"]
    all_cups = [WORLD_CUP, *CUPS]
    meta = next((m for m in all_cups if m["metaid"] == cup["metaid"]), CUPS[0])
    prize = meta["bestrewards"][-1][0]["count"]
    if state == "won":
        return prize
    if state != "eliminated":
        return 0
    ko = cup["phases"][1]["groups"][0] if len(cup["phases"]) > 1 else None
    mine = next((i for i, t in enumerate(ko["teams"]) if t["coach"]), None) if ko else None
    if mine is None:
        return prize // 10
    played = sum(1 for r in ko["rounds"] for p in r["pairs"]
                 if mine in (p["legs"][0].get("homeidx"), p["legs"][0].get("awayidx")) and "score" in p["legs"][0])
    shares = (20, 35, 50, 75) if len(ko["rounds"]) == 4 else (20, 35, 50)
    return prize * shares[max(0, min(played, len(shares)) - 1)] // 100


def ep_cup_leave(cupid, query, body):
    """PUT /cup/<id>/leave: give up, or collect an ended cup's reward. CupReward*Scene.onItemExpand
    reads body.rewards[]: itemid 1 = EP (count), anything else is an item card."""
    st = _state(); cup = (st.get("cups") or {}).pop(cupid, None)
    ep = cup_reward_ep(cup) if cup else 0
    add_ep(st, ep)
    if cup:  # Info Manager's cup record: given up, won (by AI level), prize money and the best
        r, state = coach_record(my_coach(st), "cup"), cup["coaches"][0]["state"]
        r["abandonments"] = r.get("abandonments", 0) + (1 if state == "playing" else 0)
        _ce = _cup_bymetaid(st, r).setdefault(str(cup.get("metaid")),
                                              {"joincount": 0, "wins": 0, "challenges": 0})
        if state == "won":
            _ce["wins"] = int(_ce.get("wins", 0)) + 1
            r["totalwins"] = r.get("totalwins", 0) + 1
            wins = r.setdefault("cupwins", [0] * CUP_LEVELS)
            wins = (list(wins) + [0] * CUP_LEVELS)[:CUP_LEVELS]
            wins[min(CUP_LEVELS - 1, max(0, int(cup.get("difficulty", 0))))] += 1
            r["cupwins"] = wins
        r["totalprize"] = r.get("totalprize", 0) + ep
        if ep > (r.get("best") or {}).get("prize", 0):
            r["best"] = {"prize": ep, "metaid": cup["metaid"], "date": w3c(time.time())}
    _save(st)
    out = {"result": True, "rewards": [{"itemid": 1, "count": ep}] if ep else []}
    if cup:
        # Refresh /coach/me (its cup list no longer contains this cup) so
        # the hub drops the tile -- but do NOT push the left cup itself as
        # a resource: the client registered it as an existing cup and
        # resumed the deleted cupid seconds later, which is exactly the
        # "Cup ID khong ton tai" the user hit after leaving (2026-11-12).
        # Upstream returns no cup resource here either.
        out["resources"] = coach_me_resources(st, _default_slot(st))
    return _json(out)


def ep_cup_match(cupid, query, body):
    """POST /cup/<id>/match -> my fixture of the current round as a /match/<id> resource."""
    st = _state(); cup = (st.get("cups") or {}).get(cupid)
    g, leg = cup_my_pair(cup) if cup else (None, None)
    if leg is None or "score" in leg:
        return _json({"result": False, "reason": "already played match"})
    me = coach_me(st, _default_slot(st))
    worldcup = cup.get("mode") == "worldcup"
    teamid = int(cup.get("selectedteamid") or me["teamid"]) if worldcup else me["teamid"]
    home = g["teams"][leg["homeidx"]]["teamid"]; away = g["teams"][leg["awayidx"]]["teamid"]
    opp = away if home == teamid else home
    user = {"id": me["id"], "name": me["name"], "levelinfo": me["levelinfo"]}
    user_team = owned_team(roster_team(teamid)) if worldcup else st["teams"][str(_default_slot(st))]
    match = new_match("WorldCup_PvE" if worldcup else "Cup_PvE",
                      match_side(user_team, teamid, user, _tactics(st)[1]),
                      match_side(roster_team(opp), opp))
    gs = match["gamesettings"]
    gs["home"] = home == teamid
    gs["kit"] = ["home", "away"] if gs["home"] else ["away", "home"]
    gs["difficulty"] = cup.get("difficulty", 0)  # AI level 0..4
    if cup["phases"][cup["current"]["phase"]]["system"] == "knock-out":
        # native maps drawMode 1/2/3 to end rules 9/6/7 (0 = a draw stands). 2 plays extra time
        # (FIRST_EXTRA_HALF seen in game); a draw after it is settled by cup_play_round
        gs["drawMode"] = 2
    st["match"] = match
    st["cup_pending"] = {"id": match["id"], "cupid": cupid, "home": home, "away": away}
    _save(st)
    return _json({"result": True, "resources": [{"resource": f"/match/{match['id']}", "uri": f"/match/{match['id']}", "body": match}]})


def _cup_bymetaid(st, r):
    """Per-meta cup participation {str(metaid): {joincount, wins,
    challenges}} inside record.cup -- the data CupFrontScreen renders as
    the hub's wins/challenges counters (it findObject()s recordsbymetaid
    by metaid). Only global totals were tracked before, so the served
    recordsbymetaid stayed [] and the hub showed 0 / 0 -- "never
    joined" -- for every cup (user report 2026-11-12, RCA proven from
    the CupFrontScreen dump). Seeded from the currently stored ongoing
    cups so existing runs count immediately; finished runs from before
    per-meta tracking are not attributable per meta (the global totals
    still carry them)."""
    bm = r.get("bymetaid")
    if not isinstance(bm, dict):
        bm = r["bymetaid"] = {}
    ongoing = {}
    for c in (st.get("cups") or {}).values():
        if not c.get("finished"):
            ongoing[c.get("metaid")] = ongoing.get(c.get("metaid"), 0) + 1
    for m, n in ongoing.items():
        e = bm.setdefault(str(m), {"joincount": 0, "wins": 0, "challenges": 0})
        e["challenges"] = max(int(e.get("challenges", 0)), n)
        e["joincount"] = max(int(e.get("joincount", 0)), n)
    return bm


def ep_cup_record(query, body):
    """GET /cup/record: CupProxy.onCupRecordResourceChanged takes resources[] entries whose
    `resource` is /cup/record and body._id is my coach id."""
    st = _state(); coach = my_coach(st)
    r = coach_record(coach, "cup")
    bm = _cup_bymetaid(st, r)
    recordsbymetaid = [
        {"metaid": int(m), "joincount": int(d.get("joincount", 0)),
         "records": [{"wins": int(d.get("wins", 0)),
                      "challenges": int(d.get("challenges", 0))}]}
        for m, d in sorted(bm.items(), key=lambda kv: int(kv[0]))
        if int(d.get("challenges", 0)) > 0]
    body = {"_id": coach["_id"], "recordsbymetaid": recordsbymetaid,
            **{k: int(r.get(k, 0)) for k in ("totalchallenges", "totalwins", "totalprize", "abandonments")}}
    if r.get("best"):
        body["highrecords"] = r["best"]
    # The hub re-registers ongoing cups from pushed resources: a cup created
    # this session whose draw was left without a resume call never reached
    # CupProxy, and the hub then claimed the coach had not joined (user
    # report 2026-11-12). /cup/record is fetched on every hub visit, so the
    # refresh rides along here.
    resources = [{"resource": "/cup/record", "uri": "/cup/record", "body": body}]
    resources += coach_me_resources(st, _default_slot(st))
    for c in (st.get("cups") or {}).values():
        heal_cup_coach_ids(st, c)
        resources += cup_resources(c)
    _save(st)
    return _json({"result": True, "resources": resources})


# --------------------------------------------------------------------------- #
# Market (Bursa transfer): an NPC market over the cards fifaaddict lists, at their Indonesia-
# server prices (card_price). Offer rows are {spid, expandme: "player", grade, price, count};
# the client fills staticdata (names, position, ratings) from its own DB via `expandme`.
# --------------------------------------------------------------------------- #
LINES = {"GK": range(0, 1), "DF": range(1, 9), "MF": range(9, 20), "FW": range(20, 28)}  # Enums.PLAYERPOSITION
MAX_PLAYERS = 1000  # coach.limits.players (squad size; user: "max skuad naikan", was 60)
# Tim inti (player state "first": Formasi lists only these) holds 36 ("Tim inti x/36" in Skuad);
# cards beyond that wait in Tim Cadangan, which Tukar Scout trades from.
FIRST_TEAM_MAX = 36


def first_team_count(team):
    return sum(1 for p in team["players"] if not p.get("team2"))


def balance_first_team(team):
    """Tim inti over FIRST_TEAM_MAX (clubs from before the cap): the least valuable cards outside
    the 18 of the squad move to Tim Cadangan."""
    extra = first_team_count(team) - FIRST_TEAM_MAX
    spare = [p for p in team["players"] if not p.get("team2") and not in_squad(team, p)]
    for p in sorted(spare, key=lambda p: card_price(p["spid"], grade_of(p)))[:max(0, extra)]:
        p["team2"] = True


def market_offer(spid, grade=1):
    return {"spid": spid, "expandme": "player", "grade": grade, "price": card_price(spid, grade), "count": 1}


def ep_transfer_top(position, query, body):
    """GET /transfer/top/50[/position/<GK|DF|MF|FW>]: the 50 priciest tradeable cards."""
    rng = LINES.get(str(position or "").upper())
    where = f"AND p.preferredposition1 BETWEEN {rng.start} AND {rng.stop - 1}" if rng else ""
    with closing(_db()) as c:
        both = c.execute(f"""SELECT p.spid, {POSOVR}, p.overallrating FROM players p
                             WHERE 1 {where}""").fetchall()  # every Market card, licensed or not
    _OVR.update((s, (o, d)) for s, o, d in both)
    rows = [(s, o) for s, o, _ in both]
    spids = [s for s, ovr in sorted((r for r in rows if on_market(r[0])), key=lambda r: (-r[1], r[0]))[:50]]
    return _json([market_offer(s) for s in spids])


def ep_transfer_search(spid, query, body):
    """GET /transfer/search/<spid>: that card's offers (grades 1..3); none if not tradeable."""
    return _json([market_offer(int(spid), g) for g in (1, 2, 3)] if on_market(spid) else [])


def add_player(team, spid, grade=1):
    """A new card instance in my club (reserves); returns it."""
    with closing(_db()) as c:
        r = c.execute("SELECT playerid FROM players WHERE spid = ?", (int(spid),)).fetchone()
        name = gen_teams.jersey_name(c, r[0]) if r else ""
    used = {p.get("jersey") for p in team["players"]}
    p = {"_id": new_iid(), "spid": int(spid), "expandme": "player", "grade": int(grade),
         "jersey": next((n for n in range(12, 100) if n not in used), 99), "jerseyname": name}
    if first_team_count(team) >= FIRST_TEAM_MAX:
        p["team2"] = True  # Tim inti full
    team["players"].append(p)
    return p


def remove_player(team, p):
    """Drop a card instance from my club and from the squad, captain and kickers."""
    team["players"].remove(p)
    sq, pid = team["squad"], iid(p)
    sq["positions"] = [s for s in sq["positions"] if str(s.get("id") or s["spid"]) != pid]
    sq["kickers"] = [k for k in sq.get("kickers", []) if str(k) != pid]
    repair_squad(team)
    if str(sq.get("captain")) == pid:
        sq["captain"] = next((str(s.get("id") or s["spid"]) for s in sq["positions"] if s["pos"]), "")


def is_starter(team, p):
    return any(str(s.get("id") or s["spid"]) == iid(p) and s["pos"] < POS_SUB for s in team["squad"]["positions"])


def in_squad(team, p):
    """Starter or sub (the 18 on the team sheet); reserves are not."""
    return any(str(s.get("id") or s["spid"]) == iid(p) for s in team["squad"]["positions"])


def ep_transfer_purchase(spid, grade, price, query, body):
    """POST /transfer/purchase/<spid>/<grade>/<price>/purchase: pay the market price (not the
    path's) and add a new instance of the card to my players (reserves)."""
    spid, grade = int(spid), int(grade)
    st = _state(); slot = _default_slot(st); team = st["teams"][str(slot)]
    if not on_market(spid):
        return _json({"result": False, "reason": "NOT_EXIST_OFFER"})
    if len(team["players"]) >= MAX_PLAYERS:
        return _json({"result": False, "reason": "Exceed plyer limit"})
    cost = card_price(spid, grade)
    if coach_me(st, slot)["money"]["ep"] < cost:
        return _json({"result": False, "reason": "NOT_ENOUGH_EP"})
    add_ep(st, -cost)
    add_player(team, spid, grade)
    _save(st)
    return _json({"result": True, "resources": coach_me_resources(st, slot)})


# Selling: the NPC market buys at once. The offer shows as "sold" in Daftar jual saya until
# its EP is collected (price minus SALE_FEE). Asking more than the market price is refused,
# so buying back is never cheaper than selling.
SALE_FEE = 0.10
# transfer-fee discounts (TransferMarketProxy.modifyOffersFee: floor(factor * completefee.base)):
# PC warnet (IGR) 20%, VIP 30% -- fifaaddict's FO3 tax calculator; both always active here
NO_BOOST = {"igr": 0.2, "vip": 0.3, "event": 0, "item": 0, "discountlimit": 0}


def sale_net(o):
    fee = o["completefee"]["base"]
    return o["price"] - fee + int(NO_BOOST["igr"] * fee) + int(NO_BOOST["vip"] * fee)


def offer_list(st):
    """/transfer/my/offer/list (TransferMarketProxy.fetch*): offers[] {_id, status
    selling|sold|expired, issuedat, completefee.base (fee in EP), spid, grade, price}."""
    offers = st.get("offers")
    if not isinstance(offers, list):
        offers = []
    return {"offerexpireoffset": 86400000, "boost": NO_BOOST, "offers": offers, "soldedoffers": []}
def offer_resources(st):
    return [{"uri": "/transfer/my/offer/list", "body": offer_list(st)}] + coach_me_resources(st, _default_slot(st))


def ep_transfer_price(spid, grade, query, body):
    """GET /transfer/price/<spid>/<grade>: the Jual tab's price band and default."""
    p = card_price(spid, int(grade))
    return _json({"lower": p * 8 // 10, "upper": p, "default": p, "lowest": p, "registrationFee": 0})


def ep_transfer_offer(query, body):
    """POST /transfer/offer {playerobjectid, price}: sell one of my players (not a starter)."""
    req = json.loads(body or b"{}")
    st = _state(); team = st["teams"][str(_default_slot(st))]
    pid = str(req.get("playerobjectid"))
    p = next((p for p in team["players"] if iid(p) == pid), None)
    price = int(req.get("price") or 0)
    if p is None or is_starter(team, p):
        return _json({"result": False, "reason": "TRANSFERMARKET_SELECT_SELLABLE_PLAYER"})
    if not 0 < price <= card_price(p["spid"], grade_of(p)):
        return _json({"result": False, "reason": "TRANSFERMARKET_LOWERLIMIT"})
    remove_player(team, p)
    now = time.strftime("%Y-%m-%dT%H:%M:%S.000Z", time.gmtime())
    st.setdefault("offers", []).append({
        "_id": f"{int(time.time()):08x}{os.urandom(8).hex()}", "status": "sold", "issuedat": now, "soldat": now,
        "spid": p["spid"], "expandme": "player", "grade": grade_of(p), "price": price,
        "playerobjectid": pid, "completefee": {"base": int(price * SALE_FEE)}})
    _save(st)
    return _json({"result": True, "resources": offer_resources(st)})


def ep_offer_collect(offerid, coupon, query, body):
    """DELETE /transfer/my/offer/completed/<id|all>[/<coupon>]: collect sold offers' EP."""
    st = _state(); offers = st.get("offers", [])
    done = [o for o in offers if o["status"] == "sold" and offerid in ("all", o["_id"])]
    if not done:
        return _json({"result": False, "reason": "NOT_EXIST_OFFER"})
    ep = sum(sale_net(o) for o in done)
    add_ep(st, ep)
    st["offers"] = [o for o in offers if o not in done]
    _save(st)
    return _json({"result": True, "ep": ep, "resources": offer_resources(st)})


# --------------------------------------------------------------------------- #
# Tim > Manajemen pemain > Upgrade (enhancement v1, TeamManagementPlayerEnhancementOldTabPage
# + TeamManagementPlayerEnhancingPopupScreen): two cards of the same player, the lower-grade
# one (material) is consumed. Buy a second copy on the Market to have a pair.
# --------------------------------------------------------------------------- #
ENHANCE_RATE = (1.0, 1.0, 0.81, 0.64, 0.50, 0.26, 0.15, 0.07, 0.04, 0.02, 0)  # +g -> +(g+1) chance
ENHANCE_DROP_FROM = 5  # a failed upgrade from +5 or higher drops one grade
# The enhancement UI selects its card-back glow (effect1..effect6) from the resulting
# market price. The client requests this table from /static/db/PE2STATICVALUES; without it,
# enhanced cards fall back to the plain effect even when their value qualifies for lightning.
PE2STATICVALUES = {
    "bonus": {"use": 1000},
    "simplify_step": {"use": 0},
    "effect_price": {str(i): value for i, value in enumerate((5_000_000_000, 10**9, 10**8, 10**7, 10**6))},
}


def enhance_pair(team, a, b):
    pa = next((p for p in team["players"] if iid(p) == str(a)), None)
    pb = next((p for p in team["players"] if iid(p) == str(b)), None)
    if pa is None or pb is None or pa is pb or pa["spid"] != pb["spid"] or grade_of(pa) >= 10:
        return None, None
    return pa, pb


def enhance_rate(target, material):
    import enhancement_settings
    override = enhancement_settings.get_rate()
    if override is not None:
        return override / 100.0
    return default_enhance_rate(target, material)


def default_enhance_rate(target, material):
    """Base chance by target grade times the bonus the Upgrade tab shows as "Rasio bonus
    keberhasilan" (TeamManagementPlayerEnhancementOldTabPage.updatePreviewText): material +1 or
    lower = Kosong, at least 2/3 of the target grade = High, at least 1/3 = Normal, else Low.
    The multipliers are ours (user: "Ya, samakan"); the original ones were server data."""
    tg, mg = grade_of(target), grade_of(material)
    bonus = 1.0 if mg <= 1 else 1.5 if mg >= tg * 2 / 3 else 1.25 if mg >= tg / 3 else 1.1
    return min(1.0, ENHANCE_RATE[tg] * bonus)


def enhance_cost(target):
    return max(1000, sig3(card_price(target["spid"], grade_of(target)) // 50))  # 2% of its value


def failed_enhance_grade(target, material):
    """On failure, recover uniformly from +1 through the lower of the two card grades.

    A +1 target has no recovery and drops to +0. A +10 target cannot be enhanced (checked
    by enhance_pair), so every valid attempt has a recoverable range only when its target
    starts above +1.
    """
    before, material_grade = grade_of(target), grade_of(material)
    if before <= 1:
        return 0
    return random.randint(1, max(1, min(before, material_grade)))


def merge_into(team, keep, gone):
    """The consumed card's squad slot (starter > sub) passes to the kept card of the same
    player, so an upgrade never leaves a hole in the XI; then the consumed card goes."""
    sq, k, g = team["squad"], iid(keep), iid(gone)
    slot = {str(s.get("id") or s["spid"]): s for s in sq["positions"]}
    if g in slot and (k not in slot or slot[g]["pos"] < POS_SUB <= slot[k]["pos"]):
        if k in slot:
            sq["positions"].remove(slot[k])
        slot[g]["id"] = k
        if str(sq.get("captain")) == g:
            sq["captain"] = k
        sq["kickers"] = [k if str(x) == g else x for x in sq.get("kickers", [])]
    remove_player(team, gone)


def ep_enhance_preview(a, b, query, body):
    """GET /player/<target>/enhance/<material>/preview -> {grade, levelinfo, price, enhancepay}."""
    st = _state(); team = st["teams"][str(_default_slot(st))]
    pa, pb = enhance_pair(team, a, b)
    if pa is None:
        return _json({"result": False, "reason": "INVALID_PARAM"})
    g = grade_of(pa) + 1
    return _json({"result": True, "grade": g, "levelinfo": pa.get("levelinfo", {"level": 1, "exp": 0}),
                  "price": card_price(pa["spid"], g), "enhancepay": enhance_cost(pa),
                  "probability": round(enhance_rate(pa, pb) * 100)})


def ep_enhance(a, b, boost, guard, query, body):
    """PUT /player/<target>/enhance/<material>[/<boostitem>/<guarditem>] -> {success, grade,
    rgrade, rprice, attribPlus} (TeamManagementPlayerEnhancingPopupScreen.onEnhanceComplete)."""
    st = _state(); slot = _default_slot(st); team = st["teams"][str(slot)]
    pa, pb = enhance_pair(team, a, b)
    if pa is None:
        return _json({"result": False, "reason": "INVALID_PARAM"})
    cost = enhance_cost(pa)
    if coach_me(st, slot)["money"]["ep"] < cost:
        return _json({"result": False, "reason": "Invalid target ep price"})
    add_ep(st, -cost)
    before = grade_of(pa)
    success = random.random() < enhance_rate(pa, pb)
    after = before + 1 if success else failed_enhance_grade(pa, pb)
    merge_into(team, pa, pb)
    pa["grade"] = after
    if success:
        # Bug 3: career counter feeding the upgrade achievements.
        _ups = st.setdefault("career", {}).setdefault("upgrades", {})
        _ups[str(after)] = int(_ups.get(str(after), 0)) + 1
    _save(st)
    return _json({"result": True, "success": success, "grade": after, "rgrade": after,
                  "rprice": card_price(pa["spid"], after), "attribPlus": GRADE_BOOST[after] - GRADE_BOOST[before],
                  "resources": coach_me_resources(st, slot)})


# --------------------------------------------------------------------------- #
# Tim > Manajemen pemain > Tukar (player trade v1, TeamManagementPlayerRecycleTabPage +
# TeamManagementPlayerRecyclePopupScreen). Give 2..10 reserves; clubs offer Market cards
# rated at least avg(card OVR) + playertrade_cardovrbonus[n] (the tab's "expected OVR"):
# playertrade_normal[n] offers plus one each for PC warnet and VIP (the popup has 5 slots).
# An offer is a card, a card plus EP, or EP only (no spid). Tables: static_db_STATICVALUES.json.
# --------------------------------------------------------------------------- #
_TRADE_POOL = {}


def static_values():
    with open(os.path.join(RESPONSES, "static_db_STATICVALUES.json"), encoding="utf-8") as f:
        return json.load(f)


def trade_grades(gsum):
    """PLAYERTRADE.playergrade[sum of the given cards' grades] -> {grade: weight}: +1 always,
    one more possible grade per 8 grade points traded (the tab shows its first..last key)."""
    return {str(g): 0.5 ** (g - 1) for g in range(1, min(10, 1 + int(gsum) // 8) + 1)}


def trade_pool():
    """{client rating: [(spid, teamid)]} of every Market card, any season (cached). It used to
    take only cards licensed for the ID region, which left out most old and special seasons
    (user: "trading munculkan semua playernya")."""
    if not _TRADE_POOL:
        with closing(_db()) as c:
            for s, t, o in c.execute(f"SELECT p.spid, p.teamid, {POSOVR} FROM players p"):
                if on_market(s):
                    _TRADE_POOL.setdefault(o, []).append((s, t))
    return _TRADE_POOL


def trade_props(give, rng):
    sv = static_values()
    lim, n = sv["playertrade_limit"], len(give)
    avg = sum(card_ovr(p["spid"]) for p in give) / n
    lo = int(min(max(avg + sv["playertrade_cardovrbonus"][str(n)], lim["CardMinValue"]), lim["CardMaxValue"]))
    total = sum(card_price(p["spid"], grade_of(p)) for p in give)
    weights = trade_grades(sum(grade_of(p) for p in give))
    pool = trade_pool()
    props = []
    for i, boost in enumerate([None] * int(sv["playertrade_normal"][str(n)]) + ["igr", "vip"]):
        ovr = lo
        while ovr < min(lo + 8, lim["CardMaxValue"]) and rng.random() < 0.55:  # sometimes better
            ovr += 1
        s, t = rng.choice(pool[min(pool, key=lambda r: (abs(r - ovr), -r))])
        if i == 1 and boost is None:  # a club buys the lot for EP
            props.append({"teamid": t, "ep": sig3(total * rng.uniform(0.9, 1.15))})
            continue
        g = int(rng.choices(list(weights), list(weights.values()))[0])
        offer = {"spid": s, "grade": g, "teamid": t, "price": card_price(s, g)}
        if i == 2 and boost is None:  # card plus a top-up
            offer["ep"] = sig3(total * rng.uniform(0.05, 0.2))
        if boost:
            offer["boost"] = boost  # always a PC warnet + VIP coach here
        props.append(offer)
    return props


def ep_trade_proposal(query, body):
    """POST /players/trade/proposal {players: [iid...]} -> {result, props}."""
    req = json.loads(body or b"{}")
    st = _state(); team = st["teams"][str(_default_slot(st))]
    ids = [str(i) for i in req.get("players") or []]
    give = [p for p in team["players"] if iid(p) in ids]
    if not 2 <= len(give) <= 10 or len(give) != len(set(ids)) or any(in_squad(team, p) for p in give):
        return _json({"result": False, "reason": "invalid playerid"})  # the tab lists reserves only
    props = trade_props(give, random.Random())
    st["trade"] = {"players": ids, "props": props}
    _save(st)
    return _json({"result": True, "props": props})


def ep_trade_contract(query, body):
    """POST /players/trade/contract {select: offer index}: the given players leave, the
    offer's card and/or EP arrive. With the trade3 feature on, the popup (shared with the
    Scout tab) posts /trade3/contract {slotnum, selectnum} instead, also for this tab."""
    req = json.loads(body or b"{}")
    st = _state(); slot = _default_slot(st); team = st["teams"][str(slot)]
    tr = st.get("trade") or {}
    props, ids = tr.get("props") or [], tr.get("players") or []
    sel = int(req.get("select", req.get("selectnum", -1)))
    give = [p for p in team["players"] if iid(p) in ids]
    if not 0 <= sel < len(props) or len(give) != len(ids):
        return _json({"result": False, "reason": "invalid playerid"})
    for p in give:
        remove_player(team, p)
    offer = props[sel]
    if "spid" in offer:
        add_player(team, offer["spid"], offer["grade"])
    add_ep(st, offer.get("ep", 0))
    st.pop("trade")
    _save(st)
    return _json({"result": True, "resources": coach_me_resources(st, slot)})


# --------------------------------------------------------------------------- #
# Tim > Manajemen pemain > Tukar #2 (trade3 "Scout": TeamManagementPlayerTrade3TabPage +
# ScouterController). Pick a scout, give 2..10 reserves: offers are Market cards worth about
# their total value (fifaaddict prices) times the scout's range. Scouts (coach/me `scouters`)
# differ in range and cooldown; meeting a scout's quest shifts the range up. Same popup and
# /trade3/contract as Tukar v1 (the shared popup runs in trade3 mode while the feature is on).
# --------------------------------------------------------------------------- #
NO_LIMIT = 9_000_000_000_000_000  # "unlimited": below 2^53, so exact as an AS3 Number
TRADE3_STATIC = {  # GET /trade3/staticvalues -> ScouterController.staticvalues
    # total EP given / reward cap; user: "buat tidak terbatas" (was 30 / 10 billion EP: the tab
    # warned "Hanya dapat memperoleh pemain maximum 10,000,000,000 EP")
    "playertrade3_limit": {"max_trade": NO_LIMIT, "max": NO_LIMIT},
    "vvip_playertrade3_limit": {"max_trade": NO_LIMIT, "max": NO_LIMIT},
    "playertrade3_normal": {str(n): 1 + (n >= 4) + (n >= 7) for n in range(2, 11)},  # offers by #players
    "playertrade3_effect_price": [10_000_000, 3_000_000, 1_000_000],  # popup premium effect tiers
}
SCOUTS = (  # (key = scoutid, string id, cooldown s, offer value x total, quest); always PC + VIP here
    # no cooldowns (user: "buat scout tidak ada reset"; were 1 day / 1 hour / 1 hour)
    ("default_A01", "STR_SCOUTER_KR_DEFAULT_A01", 0, (0.8, 1.1), None),
    ("default_B01", "STR_SCOUTER_KR_DEFAULT_B01", 0, (0.9, 1.3), {"require_value": 100000}),
    ("igr", "STR_SCOUTER_KR_IGR_A01", 0, (0.9, 1.4), {"require_grade": 2, "require_grade_count": 2}),
    ("vip", "STR_SCOUTER_KR_VIP_A01", 0, (1.0, 1.5), {"require_season": "88", "require_season_count": 2}),
)
QUEST_BONUS = 0.2  # quest met: the offer value range moves up by this x total
_T3_POOL = []


def w3c(ts):
    return time.strftime("%Y-%m-%dT%H:%M:%S.000Z", time.gmtime(ts))


def coach_scouters(st, slot):
    """coach/me `scouters` (CoachProxy.tradeScouters): a scout on cooldown is locked and carries
    `cooltime` (W3C) for the countdown (ScouterController re-asks /trade3/info when it ends)."""
    used, now = next(c for c in st["coaches"] if c["slot"] == slot).get("scout_used", {}), time.time()
    out = []
    for key, sid, cool, _, quest in SCOUTS:
        until = used.get(key, 0) + cool
        out.append({"key": key, "scoutid": key, "scoutername": sid + "_NAME", "scouterdesc": sid + "_DESC",
                    "lock": until > now, **({"cooltime": w3c(until)} if until > now else {}),
                    **({"quest": quest} if quest else {})})
    return out


def quest_met(quest, give):
    """The tab's quest checks: total value, cards of grade >= G, cards of the listed seasons."""
    if not quest:
        return False
    if sum(card_price(p["spid"], grade_of(p)) for p in give) < quest.get("require_value", 0):
        return False
    if "require_grade" in quest and sum(grade_of(p) >= quest["require_grade"] for p in give) < quest["require_grade_count"]:
        return False
    seasons = {int(s) for s in str(quest.get("require_season", "")).split(",") if s}
    return not seasons or sum(int(p["spid"]) // 1000000 in seasons for p in give) >= quest["require_season_count"]


def trade3_pool():
    """[(price, spid, teamid, grade)] of Market cards at every grade +1..+10, cheapest first
    (cached). Grade +1 only capped an offer at the dearest +1 card (~20 billion EP), so a trade
    of ~71 billion got far less back (user: "pertukaran tidak sebanding dengan nilai yang diawal")."""
    if not _T3_POOL:
        rows = [r for o in trade_pool().values() for r in o]
        card_ovrs([s for s, _ in rows])  # one bulk query instead of one per unpriced card
        _T3_POOL.extend(sorted((card_price(s, g), s, t, g) for s, t in rows for g in range(1, 11)))
    return _T3_POOL


def trade3_props(give, scout, rng):
    """playertrade3_normal[n] cards worth total x the scout's range (capped at the reward max;
    the last of them adds an EP top-up) + PC warnet and VIP extras."""
    lo, hi = scout[3]
    if quest_met(scout[4], give):
        lo, hi = lo + QUEST_BONUS, hi + QUEST_BONUS
    total = sum(card_price(p["spid"], grade_of(p)) for p in give)
    pool, normal = trade3_pool(), TRADE3_STATIC["playertrade3_normal"][str(len(give))]
    props = []
    for i, boost in enumerate([None] * normal + ["igr", "vip"]):
        target = min(TRADE3_STATIC["playertrade3_limit"]["max"], total * rng.uniform(lo, hi))
        k = bisect.bisect_left(pool, (target,))
        price, s, t, gr = rng.choice(pool[max(0, k - 10):k + 10])  # one of the 20 cards nearest in value
        offer = {"spid": s, "grade": gr, "teamid": t, "price": price}
        if i == normal - 1 and normal > 1:
            offer["ep"] = sig3(max(1000, total * rng.uniform(0.05, 0.15)))
        if boost:
            offer["boost"] = boost
        props.append(offer)
    return props


def ep_trade3_proposal(query, body):
    """POST /trade3/proposal {id: scout key, slotnum, playerids: [{id, spid, grade, price}]}
    -> {result, props} (tab.onResponseRecycle opens the popup; it posts /trade3/contract)."""
    req = json.loads(body or b"{}")
    st = _state(); slot = _default_slot(st); team = st["teams"][str(slot)]; coach = my_coach(st)
    scout = next((s for s in SCOUTS if s[0] == req.get("id")), None)
    sent = {str(p.get("id")): p for p in req.get("playerids") or []}
    give = [p for p in team["players"] if iid(p) in sent]
    if (scout is None or not 2 <= len(give) <= 10 or len(give) != len(sent)
            or any(in_squad(team, p) or not p.get("team2") for p in give)):  # Tim Cadangan only
        return _json({"result": False, "reason": "invalid playerid"})
    if any(int(sent[iid(p)].get("price") or 0) != card_price(p["spid"], grade_of(p)) for p in give):
        return _json({"result": False, "reason": "TRADE3_NOTICE_PLAYERPRICE03"})  # client shows a recheck notice
    used = coach.setdefault("scout_used", {})
    if used.get(scout[0], 0) + scout[2] > time.time():
        return _json({"result": False, "reason": "TRADE2_ERROR_TOO_MANY_TRY"})  # still cooling down
    if sum(card_price(p["spid"], grade_of(p)) for p in give) >= TRADE3_STATIC["playertrade3_limit"]["max_trade"]:
        return _json({"result": False, "reason": "invalid playerid"})  # the tab blocks this first
    props = trade3_props(give, scout, random.Random())
    if scout[2]:
        used[scout[0]] = time.time()  # a negotiation uses the scout, picked or not
    st["trade"] = {"players": [iid(p) for p in give], "props": props}
    _save(st)
    return _json({"result": True, "props": props, "resources": coach_me_resources(st, slot)})


def ep_player_move(pid, where, query, body):
    """POST /player/<_id>/move/firstteam|secondteam (Tim > Manajemen pemain > Skuad): Tim inti <->
    Tim Cadangan. Players in the 18 of the squad stay in Tim inti."""
    st = _state(); slot = _default_slot(st); team = st["teams"][str(slot)]
    p = next((x for x in team["players"] if iid(x) == pid), None)
    if p is None or (where == "secondteam" and in_squad(team, p)):
        return _json({"result": False, "reason": "TRANSFERMARKET_SELL_ERROR_FIRSTTEAM"})
    if where == "firstteam" and p.get("team2") and first_team_count(team) >= FIRST_TEAM_MAX:
        return _json({"result": False, "reason": "FIRSTTEAM_REGISTRATION_FAIL_POPUP_TITLE"})  # Tim inti full
    if where == "secondteam":
        p["team2"] = True
    else:
        p.pop("team2", None)
    _save(st)
    return _json({"result": True, "resources": coach_me_resources(st, slot)})


def ep_trade3_info(query, body):
    """POST /trade3/info {}: the client only reads the pushed resources (scouters)."""
    st = _state()
    return _json({"result": True, "resources": coach_me_resources(st, _default_slot(st))})


# --------------------------------------------------------------------------- #
# Shop > Pemain sells player packs for EP; Item saya > Pemain opens them one card at a time
# (TeamManagementPlayerCardScreen: POST /item/<_id>/use -> {player}). Icons are the real FO3 ID
# shop images in the client's CDN cache (externalAssets/shop/<itemid>.png). A pack draws a
# Market card from its pool, cheaper cards more likely (weight price^-0.75: ~0.5% for a card
# worth 10x the pool median); its price is the expected card value at fifaaddict prices.
# --------------------------------------------------------------------------- #
POSITIONS = {"GK": (0, 0), "DF": (1, 8), "MF": (9, 19), "FW": (20, 27)}  # preferredposition1
PACKS = {  # itemid (= shop icon): (name, pool)
    28001: ("Premier League 65+", {"league": 13, "ovr": 65}),
    28002: ("Bundesliga 65+", {"league": 19, "ovr": 65}),
    28003: ("LaLiga 65+", {"league": 53, "ovr": 65}),
    28004: ("Serie A 65+", {"league": 31, "ovr": 65}),
    28005: ("Ligue 1 65+", {"league": 16, "ovr": 65}),
    28006: ("FW 60+", {"pos": "FW", "ovr": 60}),
    28007: ("MF 60+", {"pos": "MF", "ovr": 60}),
    28008: ("DF 60+", {"pos": "DF", "ovr": 60}),
    28009: ("GK 60+", {"pos": "GK", "ovr": 60}),
    8223600: ("FW 65+", {"pos": "FW", "ovr": 65}),
    8223601: ("MF 65+", {"pos": "MF", "ovr": 65}),
    8223602: ("DF 65+", {"pos": "DF", "ovr": 65}),
    8223603: ("GK 65+", {"pos": "GK", "ovr": 65}),
    8223508: ("FW NEW 70+ 2017", {"pos": "FW", "ovr": 70, "season": 88}),
    8223509: ("MF NEW 70+ 2017", {"pos": "MF", "ovr": 70, "season": 88}),
    8223510: ("DF NEW 70+ 2017", {"pos": "DF", "ovr": 70, "season": 88}),
    82214068: ("World Best 300 2017", {"top": 300, "season": 88}),
    82214180: ("World Best 50 2017", {"top": 50, "season": 88}),
    # This Season = 2017 (spid season 88 = the client's DEFAULT_SEASON_ID, badge '17)
    45011: ("This Season Top 10", {"top": 10, "season": 88}),
    45012: ("This Season Top 20", {"top": 20, "season": 88}),
    45013: ("This Season Top 30", {"top": 30, "season": 88}),  # (Top 50 = World Best 50 2017)
    45097: ("This Season Top 30 +8", {"top": 30, "season": 88, "grade": 8}),
    # older seasons; fifaaddict season codes: 2006..2011 = 6..11, 2014 = 14, 2015 = 15
    45004: ("06-11 Player Top 10", {"top": 10, "seasons": (6, 7, 8, 9, 10, 11)}),
    45006: ("06-11 Player Top 30", {"top": 30, "seasons": (6, 7, 8, 9, 10, 11)}),
    45007: ("06-11 Player Top 50", {"top": 50, "seasons": (6, 7, 8, 9, 10, 11)}),
    82214356: ("All '07", {"season": 7}),
    739: ("'08 Top 3", {"top": 3, "season": 8}),
    15031: ("'14 Top 30 +5", {"top": 30, "season": 14, "grade": 5}),
    16001: ("'15 Top 30 +5", {"top": 30, "season": 15, "grade": 5}),
    # special seasons: wc2010 = 47, ucl2010 = 69, wc2014 = 77, tots2014 = 53, 16ec = 35, lp = 23,
    # wb (World Best) = 91, wl (World Legend) = 93
    7622: ("World Cup '10 Top 30", {"top": 30, "season": 47}),
    7618: ("Europe '10 Top 30 +3", {"top": 30, "season": 69, "grade": 3}),
    880047: ("World Cup '14 Top 50", {"top": 50, "season": 77}),
    45020: ("Special Season '14 Top 30", {"top": 30, "season": 77}),
    45063: ("Special Season '14 Top 50 +5", {"top": 50, "season": 77, "grade": 5}),
    45026: ("Team of the Season '14 Top 20", {"top": 20, "season": 53}),
    45027: ("Team of the Season '14 Top 30", {"top": 30, "season": 53}),
    45028: ("Team of the Season '14 Top 50", {"top": 50, "season": 53}),
    45130: ("Team of the Season '14 All", {"season": 53}),
    8157: ("EC Top 10", {"top": 10, "season": 35}),
    82232124: ("EC Top 30 +3", {"top": 30, "season": 35, "grade": 3}),
    8135: ("Best EC 30 +5", {"top": 30, "season": 35, "grade": 5}),
    8329: ("LP Top 50 +3", {"top": 50, "season": 23, "grade": 3}),
    8334: ("World Best 5", {"top": 5, "season": 91}),
    8336: ("World Best 1", {"top": 1, "season": 91}),
    82231307: ("World Best +5", {"season": 91, "grade": 5}),
    8194: ("World Legend Top 30", {"top": 30, "season": 93}),
    8335: ("World Legend Top 3", {"top": 3, "season": 93}),
    # Captain Player packs from FIFAAddict's FO3 Platinum pack simulator; CP season = 22.
    8366: ("CP Top 20", {"top": 20, "season": 22, "icon": 8366, "all": True}),
    8367: ("CP Top 30", {"top": 30, "season": 22, "icon": 8367, "all": True}),
    7535: ("CP Top 50", {"top": 50, "season": 22, "icon": 7535, "all": True}),
    # every season
    82214007: ("Top 300", {"top": 300}),
    8223562: ("Best 30", {"top": 30}),
    # user: "pack baru khusus all session grade 1-8", then "all session pack  top 50 ganti menjadi top 200"
    7675: ("All Season Top 200 +8", {"top": 200, "grade": 8, "all": True}),
}
# "All <season>" like All '07 for every season fifaaddict lists (user: "yang keluar cuma 07,
# saya mau semua session"): (fifaaddict label, card season codes, closest shop icon). Codes
# checked against fifaaddict's own listings (e.g. tco92 = 49, mufclegend = 55, cel = 31 + 33).
FA_SEASONS = (
    ("06", (6,), 45004), ("06U", (63,), 7618), ("06WC", (65,), 7622), ("08", (8,), 739), ("08E", (67,), 8157),
    ("09", (9,), 45006), ("10", (10,), 45007), ("10U", (69,), 7618), ("10WC", (47,), 7622), ("11", (11,), 45004),
    ("14", (14,), 15031), ("14WC", (77,), 880047), ("14T", (53,), 45026), ("15", (15,), 16001), ("16", (16,), 16001),
    ("17", (88,), 45011), ("16EC", (35,), 8157), ("16TH", (45,), 8545), ("WB", (91,), 8334), ("WL", (93,), 8194),
    ("UB", (28,), 8336), ("UL", (30,), 8335), ("EL", (27,), 8194), ("LP", (23,), 8329), ("CP", (22,), 8549),
    ("CC", (24,), 8547), ("17TH", (44,), 8545), ("TCO92", (49,), 8546), ("MUA", (55,), 8547), ("ML", (71,), 8549),
    ("TL", (51,), 8549), ("VL", (61,), 8549), ("CL", (89,), 8549), ("KL02", (95,), 8546), ("CEL", (31, 33), 8335),
    ("U23", (29,), 8545), ("KXI", (97,), 8546), ("MUL", (48,), 8547), ("VS", (73,), 2000006),
)
# each one the season's 200 best cards (user: "all session pack  top 50 ganti menjadi top 200")
PACKS.update({92000000 + codes[0]: (f"{label} Top 200", {"seasons": codes, "icon": icon, "top": 200, "all": True})
              for label, codes, icon in FA_SEASONS})
PACKS[82214356] = ("07 Top 200", {"season": 7, "top": 200, "all": True})  # the original All '07 item

# Additional packs identified from this client's externalAssets/shop icons.
# Each pool follows the season, Top-N and enhancement grade printed on the item art.
PACKS.update({
    1201: ("'11 Top 1", {"top": 1, "season": 11, "all": True}),
    1202: ("'11 Top 3", {"top": 3, "season": 11, "all": True}),
    1322: ("'06 Top 30 +6", {"top": 30, "season": 6, "grade": 6, "all": True}),
    1323: ("'06 Top 50 +6", {"top": 50, "season": 6, "grade": 6, "all": True}),
    8134: ("Best EC Top 10", {"top": 10, "season": 35, "all": True}),
    8136: ("Best EC Top 50", {"top": 50, "season": 35, "all": True}),
    8481: ("World Best Top 100", {"top": 100, "season": 91, "all": True}),
    8518: ("Premium LP/EC Top 20", {"top": 20, "seasons": (23, 35), "all": True}),
    8538: ("World Best Top 10", {"top": 10, "season": 91, "all": True}),
    12011: ("'06 Top 1 +3", {"top": 1, "season": 6, "grade": 3, "all": True}),
    1900004: ("'09 Top 3", {"top": 3, "season": 9, "all": True}),
    1900112: ("'14 Top 30 +3", {"top": 30, "season": 14, "grade": 3, "all": True}),
    200003: ("Top 100", {"top": 100, "all": True}),
    200004: ("Top 200", {"top": 200, "all": True}),
    200005: ("Top 300", {"top": 300, "all": True}),
    200008: ("Top 300 +10", {"top": 300, "grade": 10, "all": True}),
    200009: ("'09 Top 30", {"top": 30, "season": 9, "all": True}),
    200010: ("'09 Top 50", {"top": 50, "season": 9, "all": True}),
    200017: ("'07 Top 300", {"top": 300, "season": 7, "all": True}),
    400004: ("'07 Top 1", {"top": 1, "season": 7, "all": True}),
    45047: ("'11 Top 20 +5", {"top": 20, "season": 11, "grade": 5, "all": True}),
    45048: ("'11 Top 30 +5", {"top": 30, "season": 11, "grade": 5, "all": True}),
    45052: ("This Season Top 7 +5", {"top": 7, "season": 88, "grade": 5, "all": True}),
    45054: ("This Season Top 20 +5", {"top": 20, "season": 88, "grade": 5, "all": True}),
    45055: ("This Season Top 30 +5", {"top": 30, "season": 88, "grade": 5, "all": True}),
    45056: ("This Season Top 50 +5", {"top": 50, "season": 88, "grade": 5, "all": True}),
    45061: ("Special Season '14 Top 20 +5", {"top": 20, "season": 77, "grade": 5, "all": True}),
    45062: ("Special Season '14 Top 30 +5", {"top": 30, "season": 77, "grade": 5, "all": True}),
    45095: ("This Season Top 10 +8", {"top": 10, "season": 88, "grade": 8, "all": True}),
    45120: ("World Best Top 3 +8", {"top": 3, "season": 91, "grade": 8, "all": True}),
    45121: ("World Best Top 5 +8", {"top": 5, "season": 91, "grade": 8, "all": True}),
    45122: ("World Best Top 7 +8", {"top": 7, "season": 91, "grade": 8, "all": True}),
    82214028: ("Top 50", {"top": 50, "all": True}),
    82214070: ("World Best Top 100", {"top": 100, "season": 91, "all": True}),
    82214071: ("World Best Top 50", {"top": 50, "season": 91, "all": True}),
    82214076: ("World Best Top 50 +10", {"top": 50, "season": 91, "grade": 10, "all": True}),
    82214082: ("'09 Top 30", {"top": 30, "season": 9, "all": True}),
    82214086: ("'07 Top 50", {"top": 50, "season": 7, "all": True}),
    82231002: ("'09 Top 1", {"top": 1, "season": 9, "all": True}),
    82231014: ("'10 Top 1", {"top": 1, "season": 10, "all": True}),
    82231016: ("'06 Top 1", {"top": 1, "season": 6, "all": True}),
    82231023: ("'06 Top 300", {"top": 300, "season": 6, "all": True}),
    82231025: ("'08 Top 1", {"top": 1, "season": 8, "all": True}),
    82231042: ("'14 Top 300", {"top": 300, "season": 14, "all": True}),
    82231044: ("'11 Top 1", {"top": 1, "season": 11, "all": True}),
    82231051: ("'11 Top 300", {"top": 300, "season": 11, "all": True}),
    82231209: ("'06 Top 300 +?", {"top": 300, "season": 6, "all": True}),
    82231211: ("'06 Top 500", {"top": 500, "season": 6, "all": True}),
    82231715: ("EC Top 50", {"top": 50, "season": 35, "all": True}),
    82231716: ("EC Top 100", {"top": 100, "season": 35, "all": True}),
    82231717: ("EC Top 200", {"top": 200, "season": 35, "all": True}),
    82231718: ("EC Top 300", {"top": 300, "season": 35, "all": True}),
    890014: ("'14 Top 30 +5", {"top": 30, "season": 14, "grade": 5, "all": True}),
    417042610: ("World Best Top 10", {"top": 10, "season": 91, "all": True}),
    417070602: ("World Legend", {"season": 93, "all": True}),
    417073102: ("World Best Top 15", {"top": 15, "season": 91, "all": True}),
    417073103: ("World Best Top 10", {"top": 10, "season": 91, "all": True}),
    417081801: ("World Best", {"season": 91, "all": True}),
    417081808: ("World Legend", {"season": 93, "all": True}),
    418012203: ("CP Premium Top 30", {"top": 30, "season": 22, "all": True}),
    418012207: ("CP Premium Top 100", {"top": 100, "season": 22, "all": True}),
    418012208: ("CP Premium Top 50", {"top": 50, "season": 22, "all": True}),
    418012209: ("CP Premium Top 30 +3", {"top": 30, "season": 22, "grade": 3, "all": True}),
    418012210: ("CP Premium Top 20 +3", {"top": 20, "season": 22, "grade": 3, "all": True}),
    418012214: ("CP Premium Top 50 +5", {"top": 50, "season": 22, "grade": 5, "all": True}),
    418012215: ("CP Premium Top 30 +5", {"top": 30, "season": 22, "grade": 5, "all": True}),
    418012216: ("CP Premium Top 20 +5", {"top": 20, "season": 22, "grade": 5, "all": True}),
    418012217: ("CP Premium Top 10 +5", {"top": 10, "season": 22, "grade": 5, "all": True}),
    418012902: ("Best CP Top 50 +5", {"top": 50, "season": 22, "grade": 5, "all": True}),
    418012903: ("Best CP Top 30 +5", {"top": 30, "season": 22, "grade": 5, "all": True}),
    418012910: ("Best CP Top 20 +8", {"top": 20, "season": 22, "grade": 8, "all": True}),
    418012915: ("Best CP Top 30", {"top": 30, "season": 22, "all": True}),
    418012916: ("Best CP Top 20 +5", {"top": 20, "season": 22, "grade": 5, "all": True}),
    418012920: ("Best CP Top 50 +8", {"top": 50, "season": 22, "grade": 8, "all": True}),
    418012921: ("Best CP Top 30 +8", {"top": 30, "season": 22, "grade": 8, "all": True}),
    418012922: ("Best CP Top 20 +8", {"top": 20, "season": 22, "grade": 8, "all": True}),
    418013002: ("Competitive Champ EC Top 100 +3", {"top": 100, "season": 35, "grade": 3, "all": True}),
    418013003: ("Competitive Champ EC Top 50 +3", {"top": 50, "season": 35, "grade": 3, "all": True}),
    418013004: ("Competitive Champ EC Top 30 +3", {"top": 30, "season": 35, "grade": 3, "all": True}),
    418013005: ("Competitive Champ EC Top 20 +3", {"top": 20, "season": 35, "grade": 3, "all": True}),
    418013011: ("Competitive Champ EC Top 50 +5", {"top": 50, "season": 35, "grade": 5, "all": True}),
    418013013: ("Competitive Champ EC Top 20 +5", {"top": 20, "season": 35, "grade": 5, "all": True}),
    418013014: ("Competitive Champ EC Top 10 +5", {"top": 10, "season": 35, "grade": 5, "all": True}),
    418032501: ("Best EC Top 50 +5", {"top": 50, "season": 35, "grade": 5, "all": True}),
    418060601: ("New Legend", {"season": 93, "all": True}),
    418060602: ("Legend FW", {"season": 93, "pos": "FW", "all": True}),
    418060603: ("Legend MF", {"season": 93, "pos": "MF", "all": True}),
    418060604: ("Legend DF", {"season": 93, "pos": "DF", "all": True}),
    418060605: ("Legend GK", {"season": 93, "pos": "GK", "all": True}),
    418060606: ("Legend Top 50", {"top": 50, "season": 93, "all": True}),
    418060607: ("Legend Top 30", {"top": 30, "season": 93, "all": True}),
    418060610: ("Legend Top 50 +3", {"top": 50, "season": 93, "grade": 3, "all": True}),
    418060611: ("Legend Top 30 +3", {"top": 30, "season": 93, "grade": 3, "all": True}),
    418060612: ("Legend Top 20 +3", {"top": 20, "season": 93, "grade": 3, "all": True}),
    418060613: ("Legend Top 10 +3", {"top": 10, "season": 93, "grade": 3, "all": True}),
    # These six packs contain only European National Legend cards (FO3 season 32).
    # The previous unrestricted pool could return cards from any season.
    418060816: ("Midas Top 100 +3", {"top": 100, "season": 32, "grade": 3, "all": True}),
    418060817: ("Midas Top 50 +3", {"top": 50, "season": 32, "grade": 3, "all": True}),
    418060818: ("Midas Top 30 +3", {"top": 30, "season": 32, "grade": 3, "all": True}),
    418060825: ("Midas Top 30 +5", {"top": 30, "season": 32, "grade": 5, "all": True}),
    418060826: ("Midas Top 20 +5", {"top": 20, "season": 32, "grade": 5, "all": True}),
    418060827: ("Midas Top 10 +5", {"top": 10, "season": 32, "grade": 5, "all": True}),
})


# FIFAAddict Platinum simulator packs. The public pack pages list each contained
# season and its Top-N selection. Keep each item as its own pool so a pack never
# draws a player from an unrelated season. `count` records repeated selections
# shown on the page and influences the relative weight of each component.
FA_PLATINUM_PACKS = {
    950063003: ("Black Box Captain Player", "TH517063003", [(22, 20, 2), (22, 30, 3), (22, 50, 3)]),
    950063005: ("Platinum Captain Player", "TH517063005", [(22, 30, 3), (22, 50, 3), (22, 100, 3)]),
    950092809: ("Diamond EC & 16TH", "TH516092809", [(35, 5, 1), (35, 7, 2), (35, 10, 2), (35, 30, 3), (45, 11, 5)]),
    950083001: ("Platinum September 2016", "TH516083001", [(35, 7, 1), (35, 10, 2), (35, 30, 3), (35, 50, 4), (None, 100, 1)]),
    950072801: ("Diamond 3rd Anniversary", "TH516072801", [(63, 3, 1), (63, 5, 1), (63, 10, 2), (63, 30, 2), (47, 3, 1), (47, 5, 1), (47, 10, 1), (47, 30, 2)]),
    950072803: ("Platinum 3rd Anniversary", "TH516072803", [(63, 5, 1), (63, 10, 2), (63, 30, 2), (63, 50, 1), (47, 5, 1), (47, 10, 1), (47, 30, 2), (47, 50, 1)]),
    950072001: ("3rd Anniversary 06U Premium", "TH516072001", [(63, 7, 1), (63, 10, 2), (63, 30, 3), (63, 50, 4), (63, 100, 4)]),
    950071801: ("EACC 10WC Premium", "TH516071801", [(47, 7, 1), (47, 10, 2), (47, 30, 3), (47, 50, 4), (47, 100, 4)]),
    950061701: ("Platinum July 2016", "TH516061701", [(35, 30, 4), (35, 50, 4), (35, 100, 4), (None, 100, 1)]),
    950053102: ("Platinum June 2016", "TH516053102", [(35, 30, 3), (35, 50, 3), (35, 100, 3)]),
    950042801: ("Platinum May 2016", "TH516042801", [(53, 10, 1), (53, 30, 4), (53, 50, 5), (53, 90, 5), (65, 30, 1), (69, 30, 1), (67, 30, 1)]),
    950032801: ("Platinum April 2016", "TH516032801", [(53, 10, 1), (53, 30, 3), (53, 50, 4), (53, 90, 4), (65, 30, 1), (69, 30, 1), (67, 30, 1)]),
    950022401: ("Platinum March 2016", "TH516022401", [(53, 10, 1), (53, 30, 3), (53, 50, 3), (53, 90, 3), (65, 30, 1), (69, 30, 1), (67, 30, 1)]),
    950012501: ("Platinum February 2016", "TH516012501", [(65, 30, 5), (65, 50, 5), (65, 100, 5)]),
    950123101: ("Platinum January 2016", "TH515123101", [(65, 30, 4), (65, 50, 4), (65, 100, 4)]),
    950112401: ("Platinum December 2015", "TH515112401", [(65, 30, 3), (65, 50, 3), (65, 100, 3)]),
    950103001: ("Platinum November 2015", "TH515103001", [(67, 30, 5), (67, 50, 5), (67, 100, 5)]),
    950092801: ("Platinum October 2015", "TH515092801", [(67, 30, 4), (67, 50, 4), (67, 100, 4)]),
    950083101: ("Platinum September 2015", "TH515083101", [(67, 30, 3), (67, 50, 3), (67, 100, 3)]),
}
for _itemid, (_name, _icon, _groups) in FA_PLATINUM_PACKS.items():
    PACKS[_itemid] = (_name, {"groups": _groups, "icon": _icon, "all": True})

PACK_BUNCHES = (1, 10)  # buy 1 or 10 (5% off)
_PACK_POOL = {}


def pack_pool(itemid):
    """[(spid, weight)] of the Market cards a pack can give (cached)."""
    if itemid not in _PACK_POOL:
        f = PACKS[itemid][1]
        lo, hi = POSITIONS.get(f.get("pos"), (0, 27))
        groups = f.get("groups")
        seasons = (tuple(dict.fromkeys(g[0] for g in groups if g[0] is not None)) if groups else
                   f.get("seasons") or ((f["season"],) if "season" in f else None))
        # "65+" = the rating the card shows once opened (POSOVR base + its grade's boost, as on fifaaddict)
        # "all" packs take every Market card; the others only cards licensed for the ID region
        # (permid), which leaves e.g. 37 of the 395 '06 cards
        def _query(skip_licensed):
            with closing(_db()) as c:
                return c.execute(f"""SELECT p.spid, {POSOVR} AS ovr FROM players p LEFT JOIN permid m ON m.spid = p.spid
                                 WHERE ovr >= ?1 AND p.preferredposition1 BETWEEN ?2 AND ?3
                                 AND (?4 IS NULL OR p.leagueid = ?4) AND (?5 OR m.licensed = 1)""",
                                 (f.get("ovr", 0) - GRADE_BOOST[1], lo, hi, f.get("league"), skip_licensed)).fetchall()

        def _select(raw, require_market):
            return sorted(((s, o) for s, o in raw
                           if (not require_market or on_market(s))
                           and (not seasons or s // 1000000 in seasons)),
                          key=lambda r: -r[1])

        # The licensed/on-market filters are economy filters; when they
        # empty a pack completely (e.g. '14 Top 30 +5: all 227 licensed
        # season-14 cards are off-market) the pack could not be opened
        # at all and /item/use crashed on the empty pool. Relax them in
        # stages so any pack with real player data stays openable.
        raw = _query(bool(f.get("all")))
        rows = _select(raw, True)
        if not rows:
            rows = _select(raw, False)
        if not rows and not f.get("all"):
            rows = _select(_query(True), False)
        if groups:
            # Each simulator component has its own season and Top-N cutoff.
            # Repeated components count more often in the overall Platinum pack.
            selected = {}
            for season, top, count in groups:
                candidates = [row for row in rows if season is None or row[0] // 1000000 == season]
                for spid, _ovr in candidates[:top]:
                    selected[spid] = selected.get(spid, 0) + count
            ovr_by_spid = dict(rows)
            rows = [(spid, ovr_by_spid[spid]) for spid in selected]
        else:
            rows = rows[:f.get("top")]
        # a price far below the pool's (odd fifaaddict data, e.g. a 120k World Best card among 20M+
        # ones) would win almost every draw: weigh anything under a tenth of the median as that tenth
        prices = [card_price(s) for s, _ in rows]
        if not prices:
            _PACK_POOL[itemid] = []
            return []
        floor = sorted(prices)[len(prices) // 2] / 10
        _PACK_POOL[itemid] = [(s, max(p, floor) ** -0.75 * (selected[s] if groups else 1))
                              for (s, _), p in zip(rows, prices)]
    return _PACK_POOL[itemid]


def pack_grades(itemid):
    """Return the guaranteed enhancement printed on the pack; unmarked packs give +1."""
    n = PACKS[itemid][1].get("grade", 1)
    return [(n, 1)]


def pack_grade(itemid):
    return PACKS[itemid][1].get("grade", 1)


@functools.lru_cache(maxsize=None)
def pack_price(itemid):
    pool, grades = pack_pool(itemid), pack_grades(itemid)
    if not pool:
        return 1000
    gw = sum(w for _, w in grades)
    return sig3(sum(card_price(s, g) * w * gwt for s, w in pool for g, gwt in grades) / sum(w for _, w in pool) / gw)


def warm_packs():
    """Build every pack pool and price and the trade pools once (~20 s) so the first Shop or
    Tukar request does not stall the client."""
    for itemid in PACKS:
        pack_price(itemid)
    trade3_pool()


SHOP_FREE = False  # user 2026-11-12: price everything by value (was free: "shop jadi 0 semua")
LOCKER_ITEM_ID = 801  # LockerRoomController._lockerRoomItemID: the Locker Room ticket's itemid
# Craft (the Kho > Trao doi vat pham tab, upstream's "Kontrak pemain"): constants live
# here because ITEMS below already references them at import time.
CRAFT_CONTRACT, CRAFT_BOX = 970_000_000, 970_000_001  # icons: a signed sheet, a crate of balls
CRAFT_PIECE = 971_000_000  # + player id: that player's pieces
CRAFT_FILE = os.path.join(HERE, "gamedata", "craft.json")
CRAFT_DEFAULT = {"players": [], "count": 30, "pieces": 10, "fee": 0.05, "grade": 1}
CRAFT_BOX_EP = 5_000_000  # the piece box's shop price
CRAFT_LEGEND_SEASONS = (93, 30, 27, 71, 51, 61, 89, 48, 55, 49)  # WL UL EL ML TL VL CL MUL MUA TCO92


def shop_price(value, n):
    return 0 if SHOP_FREE else sig3(value * n * (0.95 if n > 1 else 1))


def use_item_price(itemid):
    """A USE item's EP price from what it can give (user 2026-11-12: price by
    value, the highest-value items priced highest). EP lotteries cost 95% of
    the expected payout (the triangular draw's mean is (2*lo + hi)/3; fixed
    cards pay face value), lottery boxes half the weighted prize value, EXP
    capsules by amount, the rest flat by kind."""
    _name, _icon, typ, eff, _desc = ITEMS[itemid]
    if itemid in (LOCKER_ITEM_ID, CRAFT_CONTRACT):
        return 1_000_000
    if "pieces" in eff:
        return CRAFT_BOX_EP
    if "ep" in eff:
        lo, hi = eff["ep"]
        return sig3((lo if lo == hi else (2 * lo + hi) / 3) * 0.95)
    if "prizes" in eff:
        tot = sum(w for w, _ in eff["prizes"])
        val = sum(w * (p[1] if isinstance(p, tuple) else pack_price(p)) for w, p in eff["prizes"]) / tot
        return sig3(val * 0.5)
    if "exp" in eff:
        return sig3(eff["exp"] * 20)
    return {"COACHNAME_CHANGE": 1_000_000, "TEAM_CHANGE": 2_000_000,
            "LEAGUE_SIMULATION": 100_000}.get(typ, 500_000)


def package_value(itemid):
    """A package/monthly product's EP value: its contents at their own prices
    (a monthly counts its daily items at half over the period)."""
    def val(i, n):
        if i == EP_ITEM:
            return n
        if i in PACKS:
            return pack_price(i) * n
        if i in ITEMS:
            return use_item_price(i) * n
        return 0
    if itemid in MONTHLY:
        _n, _ic, days, now, daily, _d = MONTHLY[itemid]
        return sum(val(i, n) for i, n in now) + sum(val(i, n) for i, n in daily) * days // 2
    return sum(val(i, n) for i, n in PACKAGES[itemid][2])


def item_def(itemid):
    """Item staticdata (POST /static/cache/item, shop products, coach items)."""
    if itemid == EP_ITEM:
        return EP_ITEM_DEF
    if itemid in ITEMS:
        return use_item_def(itemid)
    if is_piece(itemid):
        return piece_def(itemid)
    if itemid in PACKAGES or itemid in MONTHLY:
        return package_def(itemid)
    name, f = PACKS[itemid]
    return {"itemid": itemid, "productid": itemid, "name": name, "category": "PLAYERPACK",
            "type": "LEAGUE" if "league" in f else "POSITION" if "pos" in f else "CUSTOM",
            "unit": "COUNT", "itemicon": f.get("icon", itemid), "currency": "ep",
            **({"bundleicon": f["icon"]} if "icon" in f else {}),  # several packs share one icon
            "bunches": [{"bunch": n, "price": shop_price(pack_price(itemid), n)} for n in PACK_BUNCHES],
            "draw": {"year": int(f.get("season") or 2017)}}


# --------------------------------------------------------------------------- #
# Shop items besides player packs (icons = the real FO3 ID shop images in the client cache).
# Category USE (Shop > Dapat dikonsumsi; Item saya > Dapat dikonsumsi): "Gunakan" opens the
# item's screen (GeneralInventoryTileItemRenderer.useUseItem by staticdata.type), which posts
# /item/<_id>/use (ItemUtil.postItemUse):
#   EP_LOTTERY (TeamManagementEPCardScreen) {id} -> {result, ep}
#   ITEM_LOTTERY (TeamManagementRandomCardScreen) {id} -> {result, ep} | {result, item:{itemid, count}}
#   COACHNAME_CHANGE {name} -> {result} (the name check first posts /coach/reservename {name})
#   TEAM_CHANGE (nation/league/club steppers, /static/info/team/summary/<id>) {teamid} -> {result}
# PACKAGE (Shop > Paket) bundles drop their items into the inventory on purchase; MILEAGE
# (MP Shop) sells copies of some items. All free while SHOP_FREE.
# --------------------------------------------------------------------------- #
EP_ITEM = 1  # externalAssets/shop/1.png = EP coin: EP in Bag entries and package contents
EP_ITEM_DEF = {"itemid": EP_ITEM, "productid": EP_ITEM, "name": "EP", "category": "EP", "type": "EP",
               "unit": "EP", "itemicon": EP_ITEM}
ITEMS = {  # itemid: (name, icon, type, effect, description)
    501: ("Thẻ May Mắn EP (10K - 1M)", 501, "EP_LOTTERY", {"ep": (10_000, 1_000_000)}, "Mở thẻ: Nhận ngẫu nhiên 10.000 - 1.000.000 EP."),
    50029: ("Thẻ May Mắn EP (Tối đa 20M)", "ID50029", "EP_LOTTERY", {"ep": (100_000, 20_000_000)},
            "Mở thẻ: Nhận ngẫu nhiên 100.000 - 20.000.000 EP."),
    50028: ("Thẻ May Mắn EP (Tối đa 50M)", "ID50028", "EP_LOTTERY", {"ep": (200_000, 50_000_000)},
            "Mở thẻ: Nhận ngẫu nhiên 200.000 - 50.000.000 EP."),
    908: ("Thẻ May Mắn EP Triệu Phú", 908, "EP_LOTTERY", {"ep": (1_000_000, 10_000_000)}, "Mở thẻ: Nhận ngẫu nhiên 1.000.000 - 10.000.000 EP."),
    50019: ("Thẻ 200.000 EP", "ID50019", "EP_LOTTERY", {"ep": (200_000, 200_000)}, "Nhận ngay 200.000 EP."),
    50020: ("Thẻ 500.000 EP", "ID50020", "EP_LOTTERY", {"ep": (500_000, 500_000)}, "Nhận ngay 500.000 EP."),
    50021: ("Thẻ 1.000.000 EP", "ID50021", "EP_LOTTERY", {"ep": (1_000_000, 1_000_000)}, "Nhận ngay 1.000.000 EP."),
    50840: ("Túi May Mắn EP (500K - 5M)", "ID50840", "EP_LOTTERY", {"ep": (500_000, 5_000_000)}, "Mở túi: Nhận ngẫu nhiên 500.000 - 5.000.000 EP."),
    50841: ("Rương EP Đặc Biệt (5M - 30M)", "ID50841", "EP_LOTTERY", {"ep": (5_000_000, 30_000_000)}, "Mở rương: Nhận ngẫu nhiên 5.000.000 - 30.000.000 EP."),
    119082304: ("Vòng Quay Jackpot EP", 119082304, "EP_LOTTERY", {"ep": (100_000, 100_000_000)},
                "Vòng quay may mắn: Nhận ngẫu nhiên 100.000 - 100.000.000 EP."),
    # random boxes: one prize from the table, (weight, EP amount | pack itemid)
    50121: ("Hộp Bạch Kim Siêu Sao", "ID50121GP", "ITEM_LOTTERY",
            {"prizes": [(10, 8334), (5, 8194), (25, 82214007), (30, 45011), (30, ("ep", 10_000_000))]},
            "Quà ngẫu nhiên: World Best 5, World Legend Top 30, Top 300 hoặc 10.000.000 EP."),
    50122: ("Hộp Vàng Cao Cấp", "ID50122GP", "ITEM_LOTTERY",
            {"prizes": [(30, 82214180), (40, 45012), (30, ("ep", 3_000_000))]},
            "Quà ngẫu nhiên: World Best 50, Top 20 mùa giải hoặc 3.000.000 EP."),
    50123: ("Hộp Bạc May Mắn", "ID50123GP", "ITEM_LOTTERY",
            {"prizes": [(25, 8223600), (25, 8223601), (25, 8223602), (25, ("ep", 1_000_000))]},
            "Quà ngẫu nhiên: Gói FW/MF/DF 65+ hoặc 1.000.000 EP."),
    50124: ("Hộp Đồng May Mắn", "ID50124GP", "ITEM_LOTTERY",
            {"prizes": [(25, 28006), (25, 28007), (25, 28008), (25, ("ep", 300_000))]},
            "Quà ngẫu nhiên: Gói FW/MF/DF 60+ hoặc 300.000 EP."),
    50125: ("Hộp Kim Loại Cơ Bản", "ID50125GP", "ITEM_LOTTERY",
            {"prizes": [(50, 82214356), (50, ("ep", 100_000))]}, "Quà ngẫu nhiên: Gói Top 200 hoặc 100.000 EP."),
    200130000: ("Hộp Quà Bạc May Mắn", 200130000, "ITEM_LOTTERY",
                {"prizes": [(40, ("ep", 200_000)), (30, 28009), (30, 82214068)]},
                "Quà ngẫu nhiên: 200.000 EP, Gói GK 60+ hoặc World Best 300."),
    200130001: ("Hộp Quà Vàng May Mắn", 200130001, "ITEM_LOTTERY",
                {"prizes": [(40, ("ep", 1_000_000)), (30, 45007), (30, 45013)]},
                "Quà ngẫu nhiên: 1.000.000 EP, Gói Top 50 hoặc Top 30 mùa hiện tại."),
    3002: ("Thẻ Đổi Tên HLV", 3002, "COACHNAME_CHANGE", {}, "Đổi tên Huấn Luyện Viên của bạn."),
    3001: ("Thẻ Đổi Đội Bóng (CLB)", 3001, "TEAM_CHANGE", {}, "Đổi sang câu lạc bộ khác (logo, áo đấu, giải đấu); cầu thủ và đội hình được giữ nguyên."),
}
# Player EXP capsules use the client's native player selection screen.
ITEMS.update({
    910001: ('Thẻ EXP Cầu Thủ +1.000', 300, 'PLAYER_EXP_CAPSULE', {'exp': 1000},
             'Cộng 1.000 kinh nghiệm cho 1 cầu thủ. Cấp tối đa: 23.'),
    910002: ('Thẻ EXP Cầu Thủ +5.000', 300, 'PLAYER_EXP_CAPSULE', {'exp': 5000},
             'Cộng 5.000 kinh nghiệm cho 1 cầu thủ. Cấp tối đa: 23.'),
    910003: ('Thẻ EXP Cầu Thủ +30.000', 300, 'PLAYER_EXP_CAPSULE', {'exp': 30000},
             'Cộng 30.000 kinh nghiệm cho 1 cầu thủ. Cấp tối đa: 23.'),
})
# Additional consumables supported by the native inventory screens.
ITEMS.update({
    910004: ('Thẻ EXP Cầu Thủ +2.000', 300, 'PLAYER_EXP_CAPSULE', {'exp': 2000}, 'Cộng 2.000 EXP cầu thủ.'),
    910005: ('Thẻ EXP Cầu Thủ +10.000', 300, 'PLAYER_EXP_CAPSULE', {'exp': 10000}, 'Cộng 10.000 EXP cầu thủ.'),
    910006: ('Thẻ EXP Cầu Thủ +20.000', 300, 'PLAYER_EXP_CAPSULE', {'exp': 20000}, 'Cộng 20.000 EXP cầu thủ.'),
    911001: ('Thẻ EXP HLV +1.000', 200, 'COACH_EXP_CAPSULE', {'exp': 1000}, 'Cộng 1.000 EXP HLV. Cấp tối đa: 100.'),
    911002: ('Thẻ EXP HLV +5.000', 200, 'COACH_EXP_CAPSULE', {'exp': 5000}, 'Cộng 5.000 EXP HLV. Cấp tối đa: 100.'),
    911003: ('Thẻ EXP HLV +30.000', 200, 'COACH_EXP_CAPSULE', {'exp': 30000}, 'Cộng 30.000 EXP HLV. Cấp tối đa: 100.'),
})
ITEMS[2001] = ('Vé Mô Phỏng Giả Lập', 2001, 'LEAGUE_SIMULATION', {}, '1 vé mô phỏng nhanh trận đấu chế độ Giải Đấu.')
# The Locker Room ticket is a Shop item now (user 2026-11-12: sell it in the
# Shop instead of granting it free at account creation). Its itemid must stay
# 801 -- the client finds its ticket as the first itemid-801 coach item with
# createat/expireat -- so the starter package moved to 50883 above.
ITEMS[LOCKER_ITEM_ID] = ("Vé Phòng Thay Đồ", 801, "LOCKERROOM", {},
                         "Vé Phòng Thay Đồ: tùy chỉnh áo đấu, số áo, giày và phụ kiện cho cầu thủ.")
ITEMS[CRAFT_CONTRACT] = ("Hợp Đồng Cầu Thủ", 119110705, "ITEM_USE_CRAFT", {},
                         "Dùng tại Kho > Trao đổi vật phẩm, cùng mảnh cầu thủ và EP, để ký một cầu thủ huyền thoại.")
ITEMS[CRAFT_BOX] = ("Hộp Mảnh Cầu Thủ", 120013101, "ITEM_LOTTERY", {"pieces": (1, 3)},
                    "Chứa 1-3 mảnh của một cầu thủ huyền thoại (dùng cho Hợp Đồng Cầu Thủ).")
ITEMS.update({
    117121308: ("Thẻ 1 Tỷ EP", 117121308, "EP_LOTTERY", {"ep": (1_000_000_000, 1_000_000_000)}, "Nhận ngay 1.000.000.000 EP."),
    117121309: ("Thẻ 2 Tỷ EP", 117121309, "EP_LOTTERY", {"ep": (2_000_000_000, 2_000_000_000)}, "Nhận ngay 2.000.000.000 EP."),
    117121310: ("Thẻ 5 Tỷ EP", 117121310, "EP_LOTTERY", {"ep": (5_000_000_000, 5_000_000_000)}, "Nhận ngay 5.000.000.000 EP."),
    117121311: ("Thẻ 10 Tỷ EP", 117121311, "EP_LOTTERY", {"ep": (10_000_000_000, 10_000_000_000)}, "Nhận ngay 10.000.000.000 EP."),
    8223734: ("Thẻ 1 Triệu EP", 8223734, "EP_LOTTERY", {"ep": (1_000_000, 1_000_000)}, "Nhận ngay 1.000.000 EP."),
    8223735: ("Thẻ 5 Triệu EP", 8223735, "EP_LOTTERY", {"ep": (5_000_000, 5_000_000)}, "Nhận ngay 5.000.000 EP."),
    8223736: ("Thẻ 10 Triệu EP", 8223736, "EP_LOTTERY", {"ep": (10_000_000, 10_000_000)}, "Nhận ngay 10.000.000 EP."),
})
PACKAGES = {  # itemid: (name, icon, [(itemid, count)], description); bought = contents to Item saya
    50883: ("Gói Tân Thủ Khởi Đầu", 801, [(28006, 1), (28007, 1), (28008, 1), (28009, 1), (50020, 2)],
          "Gói FW/MF/DF/GK 60+ và 2 thẻ EP +500.000."),
    50880: ("Hộp Quà Tri Ân HLV", "ID50880", [(50021, 1), (50122, 1), (501, 3)],
            "1.000.000 EP, Hộp Vàng và 3 Thẻ May Mắn EP."),
    50881: ("Hộp Quà World Best Đỉnh Cao", "ID50881", [(8334, 1), (82214180, 3), (82214068, 5)],
            "World Best 5, 3 thẻ World Best 50 và 5 thẻ World Best 300."),
    50882: ("Hộp Quà Huyền Thoại 06-11", "ID50882", [(45004, 1), (45006, 2), (45007, 3)],
            "Gói Top 10, 2 gói Top 30 và 3 gói Top 50 các mùa 06-11."),
    118012902: ("Hộp Quà Nâng Cấp Thẻ", 118012902, [(15031, 1), (7618, 1), (82232124, 1)],
                "Gói '14 Top 30 +5, Châu Âu '10 Top 30 +3, EC Top 30 +3."),
    80000001: ("Thẻ VIP Đặc Quyền Siêu Cấp", "vipterbaru", [(50028, 1), (50121, 1), (50021, 5)],
               "Thẻ May Mắn EP 50M, Hộp Bạch Kim, 5 thẻ 1.000.000 EP."),
}
PACKAGES.update({  # Serial product steps (CHAINED below), bought in order
    70000001: ("Gói Chuỗi Sự Kiện 1/3", 200130000, [(501, 3), (28007, 1)], "Giai đoạn 1: 3 Thẻ May Mắn EP và gói MF 60+."),
    70000002: ("Gói Chuỗi Sự Kiện 2/3", 200130001, [(50122, 1), (50021, 1)], "Giai đoạn 2: Hộp Vàng và 1.000.000 EP."),
    70000003: ("Gói Chuỗi Sự Kiện 3/3", "ID50121GP", [(50121, 1), (8334, 1)], "Giai đoạn 3: Hộp Bạch Kim và gói World Best 5."),
})
PACKAGES.update({
    912001: ('Gói EXP Cầu Thủ Đặc Biệt', 300, [(910001, 5), (910002, 3), (910003, 1)],
             '5 thẻ EXP +1.000, 3 thẻ +5.000 và 1 thẻ +30.000.'),
    912002: ('Gói EXP HLV Đặc Biệt', 200, [(911001, 5), (911002, 3), (911003, 1)],
             '5 thẻ EXP HLV +1.000, 3 thẻ +5.000 và 1 thẻ +30.000.'),
})
PACKAGES.update({
    913001: ('Gói 10 Vé Mô Phỏng', 2001, [(2001, 10)], '10 vé mô phỏng nhanh trận đấu.'),
    913002: ('Gói 50 Vé Mô Phỏng', 2001, [(2001, 50)], '50 vé mô phỏng nhanh trận đấu.'),
    913003: ('Gói 100 Vé Mô Phỏng', 2001, [(2001, 100)], '100 vé mô phỏng nhanh trận đấu.'),
})
MP_SHOP = (501, 50123, 50122, 45013, 45006, 3002)  # MP Shop (shopcategory MILEAGE) copies
MP_SHOPID = 900_000_000  # MP Shop entries: shopid = MP_SHOPID + itemid (productid stays itemid)
CHAINED = {70000001: (70000001, 70000002, 70000003)}  # first step's itemid: all steps
SPECIAL = (8335, 8194, 50841, 50881)
STSHOPID = 910_000_000
MONTHLY = {  # itemid: (name, icon, days, items now, daily items, description)
    81000001: ("Gói EP Hàng Tháng (30 Ngày)", "ID50840", 30, [(50021, 1)], [(EP_ITEM, 200_000)],
               "Nhận ngay 1.000.000 EP, sau đó nhận 200.000 EP mỗi ngày trong 30 ngày."),
    81000002: ("Gói Thẻ Cầu Thủ Hàng Tháng (30 Ngày)", 200130001, 30, [(8223601, 1)], [(28007, 1), (EP_ITEM, 50_000)],
               "Nhận ngay gói MF 65+, sau đó nhận gói MF 60+ và 50.000 EP mỗi ngày trong 30 ngày."),
    81000003: ("Gói Kim Cương Hàng Tháng (30 Ngày)", "ID50841", 30, [(50121, 1)], [(45013, 1), (EP_ITEM, 500_000)],
               "Nhận ngay Hộp Bạch Kim, sau đó nhận Top 30 mùa hiện tại và 500.000 EP mỗi ngày trong 30 ngày."),
}


def use_item_def(itemid):
    name, icon, typ, eff, desc = ITEMS[itemid]
    return {"itemid": itemid, "productid": itemid, "name": name, "category": "USE", "type": typ, "unit": "COUNT",
            "itemicon": icon, "bundleicon": icon, "currency": "ep", "stackable": True, "description": desc,
            **({'capsuleexp': eff['exp'], 'capsuleseason': None, 'capsulelv': None}
               if typ == 'PLAYER_EXP_CAPSULE' else {}),
            # TeamManagementEPCardScreen.onQuery sizes the coin counter from count.max
            **({"count": {"min": eff["ep"][0], "max": eff["ep"][1]}} if "ep" in eff else {}),
            "bunches": [{"bunch": n, "price": shop_price(use_item_price(itemid), n)} for n in PACK_BUNCHES]}


def package_def(itemid):
    if itemid in MONTHLY:
        name, icon, days, now, daily, desc = MONTHLY[itemid]
        contents, extra = now, {"daily_period": days, "monthly_platform": "all",
                                "dailyitems": [{"itemid": i, "count": n, "staticdata": item_def(i)} for i, n in daily]}
    else:
        name, icon, contents, desc = PACKAGES[itemid]
        extra = {}
    return {"itemid": itemid, "productid": itemid, "name": name, "category": "PACKAGE", "type": "ITEM", "draw": {"year": 2017},
            "unit": "COUNT", "itemicon": icon, "bundleicon": icon, "currency": "ep", "description": desc,
            "bunches": [{"bunch": 1, "price": shop_price(package_value(itemid), 1)}], **extra,
            "items": [{"itemid": i, "count": n, "staticdata": item_def(i)} for i, n in contents]}


SHOP_NEW = (45011, 45004, 50121, 50881, 501)  # Rekomendasi: "new" order
SHOP_BEST = (82214180, 8223600, 28001, 28003, 8223601)  # Rekomendasi: Terlaris rank


def shop_entry(itemid, category="PLAYERPACK", shopid=None):
    d = item_def(itemid)
    rank = {k: t.index(itemid) + 1 for k, t in (("new", SHOP_NEW), ("best", SHOP_BEST)) if itemid in t and not shopid}
    icon = d.get("bundleicon", itemid)
    items = d["items"] if category == "PACKAGE" else [{"itemid": itemid, "count": 1, "staticdata": d}]
    return {"_id": str(shopid or itemid), "shopid": shopid or itemid, "shopcategory": category, "forceenable": True,
            "visible": True, **rank, "salestart": "2014-01-01T00:00:00.000Z", "saleend": "2035-01-01T00:00:00.000Z",
            "product": {"itemid": itemid, "staticdata": {**d, "bundleicon": icon, "items": items}}}


CHAIN_STEPS = {i: (first, k) for first, steps in CHAINED.items() for k, i in enumerate(steps)}


def chained_entry(st, first):
    """A Serial product at my progress: step = steps bought (capped at the last)."""
    steps, done = CHAINED[first], my_coach(st).get("chain", {}).get(str(first), 0)
    parts = [dict(shop_entry(i, "CHAINED"), remainedperaccount=0 if k < done else 1, buy=k < done)
             for k, i in enumerate(steps)]
    return {**parts[0], "step": min(done, len(steps) - 1), "chained": parts[1:]}


def shop_list(st):
    """GET /shop/list: curated Shop products limited strictly to:
    - Player Packs: Top 100, 200, 300 across all seasons and World Best / World Legend
    - Consumables / USE: EP items, Player EXP capsules, Coach EXP capsules, Simulation tickets
    - Packages: EXP bundles, EP packages, Simulation tickets
    - Monthly & Chained packages"""
    # Filter PACKS: only Top 100, 200, 300 or WB/WL top packs
    def is_allowed_pack(pid):
        name, f = PACKS.get(pid, ("", {}))
        nm = name.lower()
        top = f.get("top")
        if top in (100, 200, 300):
            return True
        if any(kw in nm for kw in ["top 100", "top 200", "top 300"]):
            return True
        return False

    filtered_packs = [i for i in PACKS if is_allowed_pack(i)]
    return ([shop_entry(i) for i in filtered_packs] + [shop_entry(i, "USE") for i in ITEMS]
            + [shop_entry(i, "PACKAGE") for i in PACKAGES if i not in CHAIN_STEPS]
            + [shop_entry(i, "MILEAGE", MP_SHOPID + i) for i in MP_SHOP]
            + [shop_entry(i, "MONTHLYPACK") for i in MONTHLY]
            + [chained_entry(st, first) for first in CHAINED])


def product_category(itemid):
    return "PLAYERPACK" if itemid in PACKS else "USE" if itemid in ITEMS else "PACKAGE"


def ep_specifictarget(query, body):
    """GET /specifictarget: Disable Special Bonus popup dialog."""
    return _json({"issell": False, "targets": []})

def ep_specifictarget_old(query, body):
    """GET /specifictarget: Special Bonus is open for me (ShopController.getSpecialShopInfo)."""
    return _json({"issell": True, "targets": [{"stid": STSHOPID + i, "endtime": "2035-01-01T00:00:00.000Z"}
                                              for i in SPECIAL]})


def ep_stshop(query, body):
    """GET /shop/list/STSHOP: the Special Bonus products."""
    return _json([dict(shop_entry(i, product_category(i), STSHOPID + i), stid=STSHOPID + i) for i in SPECIAL])


def _gp_shop_entry(itemid):
    """A GPSHOP entry priced in GP: shopid GP_SHOPID + itemid, a single bunch
    whose price is the GP cost (the Quest window's reward boxes read these).
    Built exactly like the upstream server's gp_shop_list: the category is
    "GPSHOP" (not the item's own shop category) and the bundleicon carries
    the "quest_" prefix -- without those two markers the CN client's
    AchievementProxy did not pick the entries up as quest rewards at all
    and rendered five catalogue packs with their EP prices instead
    (billions, one overflowing int32 -- user screenshot 2026-11-12)."""
    e = shop_entry(itemid, "GPSHOP", GP_SHOPID + itemid)
    sd = e["product"]["staticdata"]
    sd["bunches"] = [{"bunch": 1, "price": GP_SHOP_PRICES[itemid]}]
    sd["bundleicon"] = "quest_" + str(sd.get("bundleicon") or sd.get("itemicon") or itemid)
    return e


def ep_gpshop(query, body):
    """GET /shop/list/GPSHOP: the GP (EP-priced) shop catalogue. This was a
    hard-coded [] while the client fetches it FIRST on every shop visit,
    so the player-pack shop rendered empty. The GP shop's merchandise is
    the full pack range plus the consumables -- /shop/list deliberately
    curates only Top 100/200/300 packs, the GP shop sells all of them."""
    # GP-priced items appear ONCE, as their GP entry: a second EP-priced
    # copy of the same itemid (45095 & co. also live in PACKS) is what the
    # Quest window's controller found first for its reward boxes.
    return _json([_gp_shop_entry(i) for i in GP_SHOP_PRICES]
                 + [shop_entry(i) for i in PACKS if i not in GP_SHOP_PRICES]
                 + [shop_entry(i, "USE") for i in ITEMS
                    if i not in GP_SHOP_PRICES])


def monthly_list(st):
    """GET /monthlypack body: my active monthly packages."""
    return [{"_id": f"{i:024x}", "productid": i, "product": {"itemid": i, "staticdata": item_def(i)},
             "purchasedat": w3c(m["since"]), "expireat": w3c(m["expire"]), "rewardedat": w3c(m["rewarded"])}
            for i, m in ((int(k), v) for k, v in my_coach(st).get("monthly", {}).items()) if m["expire"] > time.time()]


def ep_monthlypack(query, body):
    """GET /monthlypack: my packages; POST: today's daily items of each (once a day) to the Bag."""
    st = _state()
    if body:  # POST (ShopMonthlyPackageProxy.requestReward)
        today = today_wib()
        for k, m in my_coach(st).get("monthly", {}).items():
            if m["expire"] > time.time() and today_wib(m["rewarded"]) != today:
                name, _, _, _, daily, _ = MONTHLY[int(k)]
                e = bag_add(st, f"{name} - Phần thưởng hàng ngày", [{"itemid": i, "count": n} for i, n in daily],
                            msg=f"Phần thưởng hàng ngày {name}.")
                e["type"] = "monthlypack"
                m["rewarded"] = time.time()
        _save(st)
        return _json({"result": True, "resources": [{"uri": "/monthlypack", "body": monthly_list(st)},
                                                    {"uri": "/bag", "body": bag_list(st)}]})
    return _json(monthly_list(st))


def inventory_add(coach, itemid, count):
    inv = coach.setdefault("items", [])
    it = next((i for i in inv if i["itemid"] == itemid), None)
    if it is None:
        it = {"_id": new_iid(), "itemid": itemid, "remains": 0}
        inv.append(it)
    it["remains"] += count
    return it


def my_coach(st):
    return next(c for c in st["coaches"] if c["slot"] == _default_slot(st))


# Locker Room catalog uses assets present in this client, not shop products.
LOCKER_SHOE_ASSETS = [0, 15, 16, 17, 18, 19, 20, 21, 22, 23, 24, 25, 26, 27, 28, 29, 30, 31, 32, 33, 34, 35, 36, 37, 38, 39, 40, 41, 42, 43, 44, 45, 46, 47, 48, 49, 50, 51, 52, 53, 54, 55, 56, 57, 58, 59, 60, 61, 62, 63, 64, 65, 66, 67, 68, 69, 70, 71, 72, 73, 74, 75, 76, 77, 78, 79, 80, 81, 82, 83, 84, 85, 86, 87, 88, 89, 90, 91, 92, 93, 94, 95, 96, 97, 98, 99, 100, 101, 102, 103, 104, 105, 106, 107, 108, 109, 110, 111, 112, 113, 114, 115, 116, 117, 123, 126, 127, 128, 129, 130, 131, 132, 133, 134, 135, 136, 137, 138, 139, 140, 141, 142, 143, 144, 145, 146, 147, 148, 149, 150, 151, 152, 153, 154, 155, 156, 157, 158, 159, 160, 161, 162, 163, 164, 165, 166, 167, 168, 169, 170, 171, 172, 173, 174, 175, 176, 177, 178, 179, 180, 181, 182, 183, 184, 185, 186, 187, 188, 189, 190, 191, 192, 193, 194, 195, 196, 197, 198, 199, 200, 201, 202, 203, 204, 205, 206, 207, 208, 209, 210, 211, 212, 213, 214, 215, 216, 217, 218, 219, 220, 221, 222, 223, 224, 225, 226, 227, 232, 233, 234, 235, 236, 237, 238, 240, 241, 242, 243, 244, 245, 246, 247, 248, 249, 250, 251, 252, 253, 13126, 13127, 13128, 13129, 13130, 13131, 13132, 13133, 13134, 13135, 13136, 13137, 13138, 13139, 13140, 13141, 13142, 13143, 13144, 13145, 13146, 13147, 13148, 13149, 13150, 13152, 13153, 13154, 13155, 13156, 13157, 13158, 13159, 13160, 13170, 13171, 13172, 13173, 13174, 13175, 13176, 13177, 13178, 13182, 13183, 13185, 13186, 13188, 13197, 13198, 13199, 13201, 13202, 13203, 13204, 13205, 13207, 13208, 13209, 13210, 13212, 13213, 13214, 13215, 13216, 13217, 13218, 13219, 15000, 15015, 15016, 15017, 15018, 15019, 15020, 15021, 15022, 15023, 15024, 15025, 15026, 15027, 15028, 15029, 15030, 15031, 15032, 15033, 15034, 15035, 15036, 15037, 15038, 15039, 15040, 15041, 15042, 15043, 15044, 15045, 15046, 15047, 15048, 15049, 15050, 15051, 15052, 15053, 15054, 15055, 15056, 15057, 15058, 15059, 15060, 15061, 15062, 15063, 15064, 15065, 15066, 15067, 15068, 15069, 15070, 15071, 15072, 15073, 15074, 15075, 15076, 15077, 15078, 15079, 15080, 15081, 15082, 15083, 15084, 15085, 15086, 15087, 15088, 15089, 15090, 15091, 15092, 15093, 15094, 15095, 15096, 15097, 15098, 15099, 15100, 15101, 15102, 15103, 15104, 15105, 15106, 15107, 15108, 15109, 15110, 15111, 15112, 15113, 15114, 15115, 15116, 15117, 15126, 15127, 15128, 15129, 15130, 15131, 15132, 15133, 15134, 15135, 15136, 15137, 15138, 15139, 15140, 15141, 15142, 15143, 15144, 15145, 15146, 15147, 15148, 15149, 15150, 15151, 15152, 15153, 15154, 15155, 15156, 15157, 15158, 15159, 15160, 15161, 15162, 15164, 15167, 15168, 15169, 15170, 15171, 15172, 15173, 15174, 15175, 15176, 15177, 15178, 15179, 15180, 15181, 15182, 15183, 15184, 15185, 15186, 15187, 15188, 15189, 15190, 15191, 15192, 15193, 15194, 15195, 15196, 15197, 15198, 15199, 15200, 15201, 15202, 15203, 15204, 15205, 15206, 15207, 15208, 15209, 15210, 15211, 15212, 15213, 15215, 15216, 15217, 15218, 15219, 15220, 15221, 15222, 15223, 15224, 15225, 15226, 15227, 15228, 15229, 15230, 15231, 15232, 15233, 15234, 15235, 15236, 15237, 15238, 15239, 15240, 15241, 15242, 15243, 15244, 15245, 15246, 15247, 15248, 15249, 15250, 15251, 15252, 15253, 15254, 15255, 16131, 16147, 16148]


def locker_catalog():
    rows = {}
    def add(key, kind, category, asset, label, option="", colors=None):
        rows[str(key)] = {"id": str(key), "lockerroomid": str(key), "type": kind,
                          "category": category, "asset": asset, "label": label,
                          "itemname": label, "option": option, "order": len(rows),
                          "visible": True, "slot": [], "color": list(colors or [])}
    add(1001, "TOP", "UNIFORMKIND", 0, "Ao thuong")
    add(1002, "TOP", "UNIFORMKIND", 1, "So vin")
    add(1003, "TOP", "UNIFORMKIND", 1, "Ao om", "FIT")
    add(1101, "TOP", "UNIFORMARM", 0, "Tay ngan")
    add(1102, "TOP", "UNIFORMARM", 1, "Tay dai")
    add(1201, "TOP", "TATTOO", 0, "Mac dinh")
    add(1301, "TOP", "WRIST", 0, "Khong bang co tay")
    add(1302, "TOP", "WRIST", 6, "Bang co tay", colors=range(14))
    add(1401, "TOP", "GLOVE", 0, "Khong gang tay", "PLAYERGLOVE")
    add(1402, "TOP", "GLOVE", 18, "Gang thu mon", "GKGLOVE", [1])
    add(2001, "PANTS", "SOCKS", 0, "Tat mac dinh")
    add(2002, "PANTS", "SOCKS", 1, "Tat dai")
    add(2003, "PANTS", "SOCKS", 2, "Tat ngan")
    add(2101, "PANTS", "KNEE", 0, "Khong bang dau goi")
    add(2102, "PANTS", "KNEE", 10, "Knee pad", colors=range(14))
    add(2103, "PANTS", "KNEE", 12, "Bang dau goi", colors=range(14))
    # The client opens ADIDAS initially. A neutral default row initializes the
    # grid, while actual variants stay in ETC rather than guessing their brand.
    add(99999, "SHOES", "SHOES", 0, "Mac dinh", "ADIDAS")
    for order, asset in enumerate(LOCKER_SHOE_ASSETS):
        add(100000 + asset, "SHOES", "SHOES", asset,
            "Mac dinh" if asset == 0 else f"Mẫu {order:03d}", "ETC")
    return rows


def ep_locker_jersey(player_id, query, body):
    """Change the shared shirt/shorts number, swapping an occupied number if needed."""
    st = _state()
    slot = _default_slot(st)
    players = st.get("teams", {}).get(str(slot), {}).get("players", [])
    player = next((p for p in players if iid(p) == str(player_id)), None)
    if player is None:
        return _json({"result": False, "reason": "INVALID_PLAYER"})
    if not body:
        return _json({"jersey": player.get("jersey", 0)})
    try:
        req = json.loads(body)
    except (ValueError, TypeError):
        return _json({"result": False, "reason": "INVALID_JERSEY"})
    desired = req.get("desired") if isinstance(req, dict) else None
    if type(desired) is not int or not 1 <= desired <= 99:
        return _json({"result": False, "reason": "INVALID_JERSEY"})
    holder = next((p for p in players if p is not player and p.get("jersey") == desired), None)
    giver = req.get("giverplayerid")
    if giver is not None and (holder is None or iid(holder) != str(giver)):
        return _json({"result": False, "reason": "INVALID_GIVER"})
    if holder is not None:
        old = player.get("jersey", 0)
        used = {p.get("jersey") for p in players if p is not player and p is not holder}
        if type(old) is not int or not 1 <= old <= 99 or old in used:
            old = next((n for n in range(1, 100) if n != desired and n not in used), None)
        if old is None:
            return _json({"result": False, "reason": "NO_FREE_JERSEY"})
        holder["jersey"] = old
    player["jersey"] = desired
    _save(st)
    return _json({"result": True, "jersey": desired, "resources": coach_me_resources(st, slot)})


def ep_locker_itemset(player_id, query, body):
    """Read/save one owned player's customization; an empty set restores defaults."""
    st = _state()
    slot = _default_slot(st)
    team = st.get("teams", {}).get(str(slot), {})
    player = next((p for p in team.get("players", []) if iid(p) == str(player_id)), None)
    if player is None:
        return _json({"result": False, "reason": "INVALID_PLAYER"})
    if not body:
        return _json(player.get("lr", {}))
    try:
        req = json.loads(body)
    except (ValueError, TypeError):
        return _json({"result": False, "reason": "INVALID_ITEMSET"})
    selected = req.get("itemset") if isinstance(req, dict) else None
    if not isinstance(selected, dict):
        return _json({"result": False, "reason": "INVALID_ITEMSET"})
    catalog = locker_catalog()
    normalized = {}
    categories = set()
    for key, value in selected.items():
        row = catalog.get(str(key))
        if row is None or not isinstance(value, dict) or str(value.get("id")) != str(key):
            return _json({"result": False, "reason": "INVALID_ITEMSET"})
        group = (row["category"], row["option"] if row["category"] == "GLOVE" else "")
        if group in categories:
            return _json({"result": False, "reason": "INVALID_ITEMSET"})
        categories.add(group)
        item = {"id": str(key)}
        if "pos" in value:
            if value["pos"] not in ("", "L", "R", "LR"):
                return _json({"result": False, "reason": "INVALID_ITEMSET"})
            item["pos"] = value["pos"]
        if "clr" in value:
            color = value["clr"]
            if str(color) != "-1":
                if str(color) not in {str(n) for n in row["color"]}:
                    return _json({"result": False, "reason": "INVALID_ITEMSET"})
                item["clr"] = int(color)
        normalized[str(key)] = item
    player["lr"] = normalized
    _save(st)
    return _json({"result": True, "resources": coach_me_resources(st, slot)
                  + [{"uri": f"/lockerroom/{player_id}/itemset", "body": normalized}]})


def locker_room_access(slot):
    """The Locker Room ticket's coach-item shape (createat/expireat are what
    LockerRoomController checks). Since 2026-11-12 the ticket is a bought
    Shop item; coach_items presents an owned stack in this shape."""
    now = int(time.time() * 1000)
    access_id = f"{0x801000 + int(slot):024x}"
    return {"_id": access_id, "id": access_id, "itemid": 801,
            "createat": now - 86400000, "expireat": now + 3650 * 86400000,
            "remains": 1, "count": 1, "expireable": True, "expired": False,
            "staticdata": {"itemid": 801, "name": "Vé Phòng Thay Đồ",
                           "category": "LOCKERROOM", "type": "LOCKERROOM", "itemicon": 801}}


def coach_items(st, slot):
    """/coach/me items: one stack per pack ({_id, itemid, remains}); expireable marks an owned
    item (GeneralInventoryTileItemRenderer: Gunakan button instead of Beli). An owned Locker
    Room ticket is presented in the ticket shape; craft pieces ride along (the craft tab
    counts them from here) though the bag itself hides them."""
    coach = next(c for c in st["coaches"] if c["slot"] == slot)
    out = []
    for i in coach.get("items", []):
        if i["itemid"] == LOCKER_ITEM_ID:
            t = locker_room_access(slot)
            out.append({**t, "_id": i["_id"], "id": i["_id"],
                        "remains": i["remains"], "count": i["remains"]})
        elif i["itemid"] in PACKS or i["itemid"] in ITEMS or is_piece(i["itemid"]):
            out.append({"_id": i["_id"], "id": i["_id"], "itemid": i["itemid"], "remains": i["remains"], "count": i["remains"],
                        "expireable": True, "expired": False, "staticdata": item_def(i["itemid"])})
    return out


def ep_shop_purchase(query, body):
    """POST /shop/purchase {shopid, productid, bunch: price option index}: pay EP; packs and
    items go to Item saya, a package's contents too."""
    req = json.loads(body or b"{}")
    shopid = int(req.get("shopid") or 0)
    if shopid >= GP_SHOPID:
        # A Quest-window reward box: charged in GP (quest points), not EP.
        gitem = shopid - GP_SHOPID
        gprice = GP_SHOP_PRICES.get(gitem)
        if gprice is None:
            return _json({"result": False, "reason": "NOT_EXIST_PRODUCT"})
        st = _state(); slot = _default_slot(st); coach = my_coach(st)
        if _gp_balance(st) < gprice:
            return _json({"result": False, "reason": "NOT_ENOUGH_GP"})
        st["quest"]["gp"] = int(st["quest"].get("gp") or 0) - gprice
        inventory_add(coach, gitem, 1)
        _save(st)
        res = coach_me_resources(st, slot)
        money = (res[0]["body"].get("money") if res else None) or {}
        return _json({"result": True, "money": money, "ep": money.get("ep"),
                      "resources": res + [{"uri": "/bag", "body": bag_list(st)}]})
    itemid = int(req.get("productid") or 0) or int(req.get("shopid") or 0) % MP_SHOPID % STSHOPID
    if not any(itemid in t for t in (PACKS, ITEMS, PACKAGES, MONTHLY)):
        return _json({"result": False, "reason": "NOT_EXIST_PRODUCT"})
    bunches = item_def(itemid)["bunches"]
    b = bunches[min(max(int(req.get("bunch") or 0), 0), len(bunches) - 1)]
    st = _state(); slot = _default_slot(st); coach = my_coach(st)
    if coach_me(st, slot)["money"]["ep"] < b["price"]:
        return _json({"result": False, "reason": "NOT_ENOUGH_EP"})
    add_ep(st, -b["price"])
    contents = PACKAGES[itemid][2] if itemid in PACKAGES else MONTHLY[itemid][3] if itemid in MONTHLY else [(itemid, 1)]
    for i, n in contents:
        if i == EP_ITEM:
            add_ep(st, n * b["bunch"])
        else:
            inventory_add(coach, i, n * b["bunch"])
    if itemid in MONTHLY:  # a month of daily items (renewing extends it)
        m = coach.setdefault("monthly", {}).setdefault(str(itemid), {"since": time.time(), "expire": time.time(), "rewarded": 0})
        m["expire"] = max(m["expire"], time.time()) + MONTHLY[itemid][2] * 86400
    if itemid in CHAIN_STEPS:
        first, k = CHAIN_STEPS[itemid]
        chain = coach.setdefault("chain", {})
        chain[str(first)] = max(chain.get(str(first), 0), k + 1)
    _save(st)
    res = coach_me_resources(st, slot)
    money = (res[0]["body"].get("money") if res else None) or {}
    # The bare /coach/me resource alone did not repaint the shop's EP label
    # promptly (user 2026-11-12: balance looked stale until the next buy);
    # carry the post-purchase balance at the top level too.
    return _json({"result": True, "money": money, "ep": money.get("ep"),
                  "resources": res + [{"uri": "/bag", "body": bag_list(st)}]
                  + ([{"uri": "/monthlypack", "body": monthly_list(st)}] if itemid in MONTHLY else [])})


def use_item(st, slot, coach, it, req):
    """/item/<_id>/use for a USE item (see ITEMS): its effect, one used up."""
    _, _, typ, eff, _ = ITEMS[it["itemid"]]
    out = {"result": True}
    consume = 1
    if typ == 'LEAGUE_SIMULATION':
        return {'result': False, 'reason': 'ITEM_USE_ERROR'}
    if typ in ('LOCKERROOM', 'ITEM_USE_CRAFT'):
        # A permanent pass (the client opens the Locker Room locally) and a
        # craft material (spent by /mixin, not by "use"): succeed without
        # consuming the stack.
        return {"result": True}
    if typ == 'COACH_EXP_CAPSULE':
        count = req.get('count', 1)
        if type(count) is not int or count < 1 or count > it['remains']:
            return {'result': False, 'reason': 'ITEM_USE_ERROR'}
        if coach_levelinfo(coach)['level'] >= COACH_MAX_LEVEL:
            return {'result': False, 'reason': 'ITEM_USE_ERROR'}
        # Stop at max level without spending additional unused capsules.
        consume = 0
        while consume < count and coach_levelinfo(coach)['level'] < COACH_MAX_LEVEL:
            gain_exp(coach, eff['exp'], coach_need, COACH_MAX_LEVEL)
            consume += 1
        out['exp'] = eff['exp'] * consume
    elif typ == 'PLAYER_EXP_CAPSULE':
        targetid = req.get('targetid')
        player = next((p for p in st['teams'][str(slot)]['players']
                       if isinstance(targetid, str) and iid(p) == targetid), None)
        if player is None or player_levelinfo(player)['level'] >= PLAYER_MAX_LEVEL:
            return {'result': False, 'reason': 'ITEM_USE_ERROR'}
        before, after = gain_exp(player, eff['exp'], player_need, PLAYER_MAX_LEVEL)
        out.update({'targetid': iid(player), 'before': before, 'after': after})
    elif typ == "EP_LOTTERY":  # lower amounts more likely
        lo, hi = eff["ep"]
        out["ep"] = lo if lo == hi else max(lo, round(random.triangular(lo, hi, lo), -4))
        add_ep(st, out["ep"])
    elif typ == "ITEM_LOTTERY" and "pieces" in eff:  # craft box: pieces of one recipe's player
        if not craft_cards():
            return {"result": False, "reason": "ITEM_USE_ERROR"}
        piece, n = CRAFT_PIECE + random.choice(craft_cards())[1], random.randint(*eff["pieces"])
        inventory_add(coach, piece, n)
        out["item"] = {"itemid": piece, "count": n, "staticdata": piece_def(piece)}
    elif typ == "ITEM_LOTTERY":
        prize = random.choices([p for _, p in eff["prizes"]], [w for w, _ in eff["prizes"]])[0]
        if isinstance(prize, tuple):
            out["ep"] = prize[1]
            add_ep(st, prize[1])
        else:
            inventory_add(coach, prize, 1)
            out["item"] = {"itemid": prize, "count": 1, "staticdata": item_def(prize)}
    elif typ == "COACHNAME_CHANGE":
        name = str(req.get("name") or "").strip()
        if not 2 <= len(name) <= 12:
            return {"result": False, "reason": "COACH_CREATION_ERROR_COACH_TOO_SHORT"}
        coach["name"] = name
    elif typ == "TEAM_CHANGE":
        teamid = int(req.get("teamid") or 0)
        if not roster(teamid)["team"]["players"]:
            return {"result": False, "reason": "ITEM_USE_ERROR"}
        coach["teamid"] = teamid
        repair_league_context(st)
    it["remains"] -= consume
    if not it["remains"]:
        coach["items"].remove(it)
    return out


def ep_item_use(item_id, query, body):
    """POST /item/<_id>/use: open one pack -> {result, player: {spid, grade, price}}; USE items
    see use_item."""
    st = _state(); slot = _default_slot(st); coach = my_coach(st); team = st["teams"][str(slot)]
    it = next((i for i in coach.get("items", []) if i["_id"] == item_id and i["remains"] > 0), None)
    if it is not None and it["itemid"] in ITEMS:
        out = use_item(st, slot, coach, it, json.loads(body or b"{}"))
        if out["result"]:
            _save(st)
        return _json({**out, "resources": coach_me_resources(st, slot)})
    if it is None or it["itemid"] not in PACKS:
        return _json({"result": False, "reason": "INVALID_ITEM"})
    if len(team["players"]) >= MAX_PLAYERS:
        return _json({"result": False, "reason": "Exceed plyer limit"})  # client shows "squad full"
    pool = pack_pool(it["itemid"])
    if not pool:
        return _json({"result": False, "reason": "INVALID_ITEM"})
    spid = random.choices([s for s, _ in pool], [w for _, w in pool])[0]
    g = pack_grade(it["itemid"])
    p = add_player(team, spid, g)
    it["remains"] -= 1
    if not it["remains"]:
        coach["items"].remove(it)
    _save(st)
    return _json({"result": True, "player": {"_id": p["_id"], "spid": spid, "grade": g, "price": card_price(spid, g)},
                  "resources": coach_me_resources(st, slot)})


def ep_static_item(query, body):
    """POST /static/cache/item {itemids} -> {itemid: staticdata} (ItemUtil cache)."""
    ids = []
    for i in json.loads(body or b"{}").get("itemids") or []:
        try:
            ids.append(int(i))
        except (TypeError, ValueError):
            pass
    return _json({str(i): EP_ITEM_DEF if i == EP_ITEM else item_def(i) for i in ids
                  if i == EP_ITEM or i in PACKS or i in ITEMS or i in PACKAGES or is_piece(i)})


# --------------------------------------------------------------------------- #
# Craft (Kho > Trao doi vat pham; upstream's "Kontrak pemain", ported
# 2026-11-12 -- the tab was empty because /mixin/recipes did not exist).
# TeamManagementPlayerCraftTabPage lists GET /mixin/recipes {recipeid: recipe},
# category "player" only: {recipeid, category, materials [the player's pieces,
# the contract], result.normal {id: spid, grade, price}, money.ep (the fee)};
# what I have comes from my items (CoachProxy.items, by itemid). "Kontrak"
# posts /mixin/<recipeid> and on {result: true, results: [{spid, grade}]}
# shows the signed card. A piece is an item of type ITEM_PLAYER_CRAFT with
# staticdata spid / enhance. Recipes (gamedata/craft.json): its players
# [spid], or none = the `count` best Legend-season players (one card each, on
# the Market); each at +`grade` for `pieces` of his pieces, one contract and
# `fee` of the card's price. Pieces come from the craft box item (CRAFT_BOX:
# 1-3 pieces of a random recipe), the contract from the Shop.
# --------------------------------------------------------------------------- #
def is_piece(itemid):
    try:
        return CRAFT_PIECE < int(itemid) < CRAFT_PIECE + 1_000_000
    except (TypeError, ValueError):
        return False


def craft_cfg():
    """CRAFT_DEFAULT with gamedata/craft.json's settings (read at every use)."""
    try:
        with open(CRAFT_FILE, encoding="utf-8") as f:
            cfg = json.load(f)
    except (OSError, ValueError):
        cfg = {}
    return {**CRAFT_DEFAULT, **{k: v for k, v in (cfg if isinstance(cfg, dict) else {}).items() if k in CRAFT_DEFAULT}}


def craft_cards():
    """[(spid, player id, short name)] of the recipes, in the list's order (the default list: best first)."""
    cfg = craft_cfg()
    return _craft_cards(tuple(int(s) for s in cfg["players"]), int(cfg["count"]))


@functools.lru_cache(maxsize=8)
def _craft_cards(players, count):
    with closing(_db()) as c:
        if players:
            q = "SELECT p.spid, p.playerid, n.short FROM players p JOIN namesind n ON n.playerid = p.playerid WHERE p.spid = ?"
            rows = [r for r in (c.execute(q, (s,)).fetchone() for s in players) if r]
        else:
            rows = c.execute(f"""SELECT p.spid, p.playerid, n.short FROM players p JOIN namesind n ON n.playerid = p.playerid
                                 WHERE p.spid / 1000000 IN ({",".join(map(str, CRAFT_LEGEND_SEASONS))}) ORDER BY {POSOVR} DESC, p.spid""").fetchall()
    seen, out = set(), []
    for spid, pid, name in rows:
        if (players or on_market(spid)) and pid not in seen:
            seen.add(pid)
            out.append((spid, pid, name))
    return out if players else out[:count]


# Item-exchange recipes (Kho > Trao doi vat pham, ItemExchangeTabPage): that
# tab renders ONLY recipes with category "item" (the player-craft tab takes
# "player"), so the craft port alone left it empty (user 2026-11-12).
# Consolidation trades: small consumables in, the bigger one out.
ITEM_EXCHANGE_RECIPES = {
    980000001: {"recipeid": 980000001, "category": "item", "group": "USE",
                "materials": [{"itemid": 910001, "count": 3}],
                "result": {"normal": {"id": 910002, "count": 1}}, "money": {"ep": 0}},
    980000002: {"recipeid": 980000002, "category": "item", "group": "USE",
                "materials": [{"itemid": 910002, "count": 3}],
                "result": {"normal": {"id": 910006, "count": 1}}, "money": {"ep": 0}},
    980000003: {"recipeid": 980000003, "category": "item", "group": "USE",
                "materials": [{"itemid": 910005, "count": 2}],
                "result": {"normal": {"id": 910003, "count": 1}}, "money": {"ep": 0}},
    980000004: {"recipeid": 980000004, "category": "item", "group": "USE",
                "materials": [{"itemid": 911001, "count": 3}],
                "result": {"normal": {"id": 911002, "count": 1}}, "money": {"ep": 0}},
    980000005: {"recipeid": 980000005, "category": "item", "group": "USE",
                "materials": [{"itemid": 911002, "count": 3}],
                "result": {"normal": {"id": 911003, "count": 1}}, "money": {"ep": 0}},
    980000006: {"recipeid": 980000006, "category": "item", "group": "USE",
                "materials": [{"itemid": 50019, "count": 2}],
                "result": {"normal": {"id": 50020, "count": 1}}, "money": {"ep": 0}},
    980000007: {"recipeid": 980000007, "category": "item", "group": "USE",
                "materials": [{"itemid": 50020, "count": 2}],
                "result": {"normal": {"id": 50021, "count": 1}}, "money": {"ep": 0}},
    980000008: {"recipeid": 980000008, "category": "item", "group": "USE",
                "materials": [{"itemid": 501, "count": 3}],
                "result": {"normal": {"id": 50840, "count": 1}}, "money": {"ep": 0}},
}


def craft_recipes():
    """GET /mixin/recipes: {recipeid (the player id): recipe}."""
    cfg, out = craft_cfg(), {}
    for spid, pid, _ in craft_cards():
        price = card_price(spid, cfg["grade"])
        out[str(pid)] = {"recipeid": pid, "category": "player",
                         "materials": [{"itemid": CRAFT_PIECE + pid, "count": cfg["pieces"]}, {"itemid": CRAFT_CONTRACT, "count": 1}],
                         "result": {"normal": {"id": spid, "grade": cfg["grade"], "price": price}},
                         "money": {"ep": sig3(price * cfg["fee"])}}
    for rid, rec in ITEM_EXCHANGE_RECIPES.items():
        out[str(rid)] = rec
    return out


def piece_def(itemid):
    """A piece's staticdata: the card of its recipe (else the player's best card)."""
    pid = int(itemid) - CRAFT_PIECE
    spid, name = next(((s, n) for s, p, n in craft_cards() if p == pid), (None, None))
    if spid is None:
        with closing(_db()) as c:
            r = c.execute(f"""SELECT p.spid, n.short FROM players p JOIN namesind n ON n.playerid = p.playerid
                              WHERE p.playerid = ? AND p.spid >= 1000000 ORDER BY {POSOVR} DESC""",
                          (pid,)).fetchone()
        spid = r[0] if r else pid
        name = r[1] if r else str(pid)
    cfg = craft_cfg()
    return {"itemid": int(itemid), "productid": int(itemid), "name": f"Mảnh cầu thủ {name}", "category": "USE",
            "type": "ITEM_PLAYER_CRAFT", "unit": "COUNT", "itemicon": ITEMS[CRAFT_BOX][1], "stackable": True,
            "spid": spid, "enhance": cfg["grade"],
            "description": f"Thu thập {cfg['pieces']} mảnh để ký hợp đồng với {name} (Kho > Trao đổi vật phẩm)."}


def ep_mixin(rid, query, body):
    """POST /mixin/<recipeid>: sign the recipe's card for my pieces of him, a contract and the fee
    -> {result, results [{spid, grade}]} and /coach/me (items, EP, players) for the list's counts."""
    if str(rid) == "recipes":  # in case the regex route wins over ENDPOINTS
        return _json(craft_recipes())
    st = _state(); slot = _default_slot(st); coach = my_coach(st); team = st["teams"][str(slot)]
    r = craft_recipes().get(str(rid))
    if r is None:
        return _json({"result": False, "reason": "CRAFTSYSTEM_ERROR_03"})
    if r.get("category") == "item":
        # Item exchange (Kho tab): consume the material stacks, hand back
        # the result item. Same error vocabulary as the player craft.
        need = {m["itemid"]: m["count"] for m in r["materials"]}
        have = {i["itemid"]: i for i in coach.get("items", []) if i["itemid"] in need}
        if any(have.get(i, {}).get("remains", 0) < n for i, n in need.items()):
            return _json({"result": False, "reason": "CRAFTSYSTEM_ERROR_02"})
        fee = int((r.get("money") or {}).get("ep", 0))
        if coach.get("ep", START_EP) < fee:
            return _json({"result": False, "reason": "CRAFTSYSTEM_ERROR_05"})
        for i, n in need.items():
            have[i]["remains"] -= n
            if not have[i]["remains"]:
                coach["items"].remove(have[i])
        if fee:
            add_ep(st, -fee)
        res = r["result"]["normal"]
        inventory_add(coach, res["id"], int(res.get("count", 1)))
        _save(st)
        return _json({"result": True, "results": [{"itemid": res["id"], "count": int(res.get("count", 1))}],
                      "resources": coach_me_resources(st, slot)})
    need = {m["itemid"]: m["count"] for m in r["materials"]}
    have = {i["itemid"]: i for i in coach.get("items", []) if i["itemid"] in need}
    if len(team["players"]) >= MAX_PLAYERS:
        return _json({"result": False, "reason": "CRAFTSYSTEM_ERROR_01"})
    if any(have.get(i, {}).get("remains", 0) < n for i, n in need.items()):
        return _json({"result": False, "reason": "CRAFTSYSTEM_ERROR_02"})
    if coach.get("ep", START_EP) < r["money"]["ep"]:
        return _json({"result": False, "reason": "CRAFTSYSTEM_ERROR_05"})
    for i, n in need.items():
        have[i]["remains"] -= n
        if not have[i]["remains"]:
            coach["items"].remove(have[i])
    add_ep(st, -r["money"]["ep"])
    card = r["result"]["normal"]
    add_player(team, card["id"], card["grade"])
    _save(st)
    return _json({"result": True, "results": [{"spid": card["id"], "grade": card["grade"]}],
                  "resources": coach_me_resources(st, slot)})


# --------------------------------------------------------------------------- #
# Daily login reward: the first login of a day (WIB, at PUT /coach/<slot>/default) drops that
# day's reward into the Bag (Item saya > Items, ItemBagTabPage: GET /bag; "Terima" = POST
# /bag/<id>/unpack, "Terima semua" = POST /bag/unpackall {reqUnpackItems}). 7-day cycle on a
# consecutive-login streak; the table is gamedata/daily_rewards.json (admin page edits it).
# Bag entries are event packs (ItemUtil.isEventItempackData: no product, type "event",
# items [{itemid, count}] expanded via /static/cache/item, players [{spid, grade}]).
# --------------------------------------------------------------------------- #
DAILY_FILE = os.path.join(HERE, "gamedata", "daily_rewards.json")
DAILY_DEFAULT = [{"ep": 10000}, {"ep": 20000}, {"pack": 28007}, {"ep": 30000}, {"pack": 28006},
                 {"ep": 50000}, {"ep": 100000, "pack": 82214068}]  # day 1..7
WIB = 7 * 3600


def daily_table():
    try:
        with open(DAILY_FILE, encoding="utf-8") as f:
            return json.load(f) or DAILY_DEFAULT
    except (OSError, ValueError):
        return DAILY_DEFAULT


def today_wib(ts=None):
    return time.strftime("%Y-%m-%d", time.gmtime((time.time() if ts is None else ts) + WIB))


def reward_items(reward):
    return ([{"itemid": EP_ITEM, "count": int(reward["ep"])}] if reward.get("ep") else []) + \
           ([{"itemid": int(reward["pack"]), "count": int(reward.get("count") or 1)}] if reward.get("pack") else [])


def bag_add(st, title, items=(), players=(), msg=""):
    entry = {"_id": new_iid(), "title": title, "msg": msg, "items": list(items), "players": list(players),
             "createdat": int(time.time())}
    my_coach(st).setdefault("bag", []).append(entry)
    return entry


def daily_login(st, now=None):
    """Grant today's reward once per WIB day; returns the new bag entry or None."""
    now = time.time() if now is None else now
    d = my_coach(st).setdefault("daily", {"last": "", "streak": 0, "total": 0})
    if d["last"] == today_wib(now):
        return None
    d["streak"] = d["streak"] + 1 if d["last"] == today_wib(now - 86400) else 1
    d["total"] += 1
    d["last"] = today_wib(now)
    table, n = daily_table(), d["streak"]
    return bag_add(st, f"Điểm danh hàng ngày - Ngày thứ {n}", reward_items(table[(n - 1) % len(table)]),
                   msg=f"Đã điểm danh {n} ngày liên tiếp. Hãy đăng nhập vào ngày mai để nhận quà ngày {n + 1}.")


def bag_list(st):
    out = []
    for e in my_coach(st).get("bag", []):
        icon = next((PACKS.get(i["itemid"], ("", {}))[1].get("icon", i["itemid"])
                     for i in e["items"] if i["itemid"] != EP_ITEM), EP_ITEM)
        # ShopItemDescription.MakeEventItemDesc tooltip: msg = text, date (W3C) = "Tanggal diterima"
        out.append({"_id": e["_id"], "id": e["_id"], "type": e.get("type", "event"), "title": e["title"],
                    "msg": e.get("msg") or "Phần thưởng từ hệ thống máy chủ.", "date": w3c(e["createdat"]),
                    "items": e["items"], "players": e["players"], "bundleicon": icon})
    return out


def bag_apply(st, entry):
    """Credit a bag entry: EP, packs and items to Item saya, players to the club."""
    coach = my_coach(st); team = st["teams"][str(_default_slot(st))]
    for it in entry["items"]:
        if it["itemid"] == EP_ITEM:
            add_ep(st, it["count"])
        elif it["itemid"] in PACKS or it["itemid"] in ITEMS:
            inventory_add(coach, it["itemid"], it["count"])
    for p in entry["players"]:
        add_player(team, p["spid"], p.get("grade", 1))
    coach["bag"].remove(entry)


def bag_reply(st):
    return _json({"result": True, "overdueitems": [],
                  "resources": coach_me_resources(st, _default_slot(st)) + [{"uri": "/bag", "body": bag_list(st)}]})


def ep_bag_unpack(bid, query, body):
    """POST /bag/<id>/unpack: take one bag entry."""
    st = _state()
    entry = next((e for e in my_coach(st).get("bag", []) if e["_id"] == bid), None)
    if entry is None:
        return _json({"result": False, "reason": "ITEM_USE_ERROR"})
    if len(st["teams"][str(_default_slot(st))]["players"]) + len(entry["players"]) > MAX_PLAYERS:
        return _json({"result": False, "reason": "TRANSFERMARKET_FULLSLOT_DESC"})
    bag_apply(st, entry)
    _save(st)
    return bag_reply(st)


def ep_bag_unpackall(query, body):
    """POST /bag/unpackall {reqUnpackItems: [id...]}: take them all (club slots permitting)."""
    ids = set(map(str, json.loads(body or b"{}").get("reqUnpackItems") or []))
    st = _state(); team = st["teams"][str(_default_slot(st))]
    for e in [e for e in my_coach(st).get("bag", []) if e["_id"] in ids]:
        if len(team["players"]) + len(e["players"]) <= MAX_PLAYERS:
            bag_apply(st, e)
    _save(st)
    return bag_reply(st)


# Path-pattern routes (ids in the path); checked after the exact ENDPOINTS.
ROUTES = [
    (re.compile(r"/worldcup/info/authentic/team/(\d+)"), ep_worldcup_authentic_team),
    (re.compile(r"/lockerroom/(\w+)/itemset"), ep_locker_itemset),
    (re.compile(r"/lockerroom/(\w+)/jersey"), ep_locker_jersey),
    (re.compile(r"/item/(\w+)/use"), ep_item_use),
    (re.compile(r"/bag/(\w+)/unpack"), ep_bag_unpack),
    (re.compile(r"/coach/(\w+)/postponed"), ep_coach_postponed),
    (re.compile(r"/static/info/authentic/team/(licensed|all)/(\d+)"), ep_authentic_team),
    # Ganti tim screen: a club's summary for its TeamInfoNew (name/crest id, ovr, teamgrade)
    (re.compile(r"/static/info/team/summary/(\d+)"), lambda t, q, b: _json(club_summary_response(int(t)))),
    (re.compile(r"/player/(\w+)/move/(firstteam|secondteam)"), ep_player_move),
    # The v2.5 enhancement screen appends usePoint, boostItemId and boostEventId to
    # the path.  The local implementation does not consume those optional items,
    # but must accept the route so ordinary card upgrades still work.
    (re.compile(r"/player/(\w+)/enhance2\.5/(\w+)/preview(?:/[^/]+){0,3}"), ep_enhance_preview),
    (re.compile(r"/player/(\w+)/enhance2\.5/(\w+)(?:/(-?\w+)/(-?\w+)(?:/[^/]+)?)?"), ep_enhance),
    (re.compile(r"/player/(\w+)/enhance/(\w+)/preview"), ep_enhance_preview),
    (re.compile(r"/player/(\w+)/enhance/(\w+)(?:/(-?\w+)/(-?\w+))?"), ep_enhance),
    (re.compile(r"/transfer/top/50(?:/position/(\w+))?"), ep_transfer_top),
    (re.compile(r"/transfer/search/(\d+)"), ep_transfer_search),
    (re.compile(r"/transfer/purchase/(\d+)/(\d+)/(\d+)/purchase"), ep_transfer_purchase),
    (re.compile(r"/transfer/price/(\d+)/(\d+)"), ep_transfer_price),
    (re.compile(r"/transfer/my/offer/completed/(\w+)(?:/(\w+))?"), ep_offer_collect),
    (re.compile(r"/match/([0-9a-f]+)/gamesettings"), ep_match_gamesettings),
    (re.compile(r"/match/([0-9a-f]+)/side/(left|right)/squad"), ep_match_squad),
    (re.compile(r"/match/([0-9a-f]+)/player/ready"), ep_match_player_ready),
    (re.compile(r"/match/([0-9a-f]+)/side/(\w+)/ready"), ep_match_ready),
    (re.compile(r"/match/([0-9a-f]+)/end"), ep_match_end),
    (re.compile(r"/league2/history/players/(league|cup)"), ep_l2_player_history),
    (re.compile(r"/league2/leader/(league|cup)"), ep_l2_leader),
    (re.compile(r"/match/([0-9a-f]{24})"), ep_match_get),
    (re.compile(r"/league2/sponsor/(\d+)"), ep_l2_sponsor),
    (re.compile(r"/mixin/(\w+)"), ep_mixin),  # (/mixin/recipes: ENDPOINTS, matched first)
    (re.compile(r"/cup/([0-9a-f]{24})/match"), ep_cup_match),
    (re.compile(r"/cup/([0-9a-f]{24})/next"), ep_cup_next),
    (re.compile(r"/cup/([0-9a-f]{24})/(?:resume|test)"), ep_cup_resume),
    (re.compile(r"/cup/([0-9a-f]{24})/leave"), ep_cup_leave),
    (re.compile(r"/cup/([0-9a-f]{24})"), lambda cupid, q, b: _json((_state().get("cups") or {}).get(cupid, {}))),
]


RECONTRACT_PLAYER_MAX_LIFE = 40
RECONTRACT_STAFF_MAX_LIFE = 100


def _recontract_fee(player, remaining):
    """Offline fallback rate when the original BASECONTRACTFEE table is unavailable."""
    if remaining <= 0:
        return 0
    try:
        ovr = max(1, int(player_ovr(player)))
    except (TypeError, ValueError, KeyError):
        ovr = 50
    return max(1, round(ovr * 0.13)) * remaining


# Renewal fee discount: PC warnet (IGR) 20% + VIP 30% -- the same boost
# rates this server already serves for transfer fees (NO_BOOST, from
# fifaaddict's FO3 tax calculator; both are always active here). The
# preview used to hardcode boost/discount 0, so the renewal dialog
# showed the PC/VIP badges with NO discounted figure (user report
# 2026-11-12 23:00). The charge applies the identical discount, so the
# dialog's "Gia sau giam" and the EP actually deducted always agree.
RECONTRACT_BOOST = {"igr": 0.2, "vip": 0.3, "tgp": 0, "discountlimit": 0}


def _recontract_discount(total):
    return int(RECONTRACT_BOOST["igr"] * total) + int(RECONTRACT_BOOST["vip"] * total)


def _recontract_cost_info(total):
    return {"total": total, "boost": dict(RECONTRACT_BOOST),
            "discount": _recontract_discount(total), "discountlimit": 0}


def ep_recontract_preview(kind, query, body):
    """Preview selected player/staff contract renewals without changing profile state."""
    req = json.loads(body or b"{}")
    key = "players" if kind == "player" else "staffs"
    requested = req.get(key) or []
    if not isinstance(requested, list):
        requested = []
    requested = list(dict.fromkeys(map(str, requested)))
    st = _state()
    team = st.get("teams", {}).get(str(_default_slot(st)), {})
    collection = team.get(key, [])
    # Key by iid(): starter-squad players carry NO _id/id at all (only spid),
    # and the client addresses players by iid -- keying on _id made every
    # lookup miss, so the preview returned an empty list and the client
    # showed "Khong the lam moi HD" (user 2026-11-12, GM3's Buffon 39/40).
    by_id = {str(p.get("_id") or p.get("spid") or p.get("id") or ""): p for p in collection}
    cap = RECONTRACT_PLAYER_MAX_LIFE if kind == "player" else RECONTRACT_STAFF_MAX_LIFE
    selected, selected_rows, total = [], [], 0
    for ident in requested:
        item = by_id.get(ident)
        if item is None:
            continue
        life = _player_life(item) if kind == "player" else max(0, int(item.get("life", cap) or 0))
        if life >= cap:
            continue
        copy = dict(item)
        copy["id"] = str(item.get("_id") or item.get("spid") or item.get("id") or "")  # the popup collects row.id for the renew POST
        if kind == "player":
            fee = _recontract_fee(item, cap - life)
        else:
            fee = max(0, int(item.get("contractfee", 0) or 0))
            if fee == 0:
                fee = max(1, int(item.get("grade", 1))) * (cap - life)
        copy["contractfee"] = fee
        selected.append(ident)
        selected_rows.append(copy)
        total += fee
    ep = int(my_coach(st).get("ep", START_EP) or 0)
    return _json({
        "result": True, key: selected_rows, "selected": selected,
        "MaximumRemainingMatch": cap, "StaffMaximumRemainingMatch": cap,
        "accountInfo": {"ep": ep},
        "costInfo": _recontract_cost_info(total),
        "type": "single",
    })


def ep_recontract(kind, query, body):
    """Apply a confirmed renewal atomically; invalid IDs or insufficient EP leave data intact."""
    req = json.loads(body or b"{}")
    key = "players" if kind == "player" else "staffs"
    ids = req.get(key) or []
    if not isinstance(ids, list) or not ids:
        return _json({"result": False, "reason": "INVALID_RECONTRACT_REQUEST"})
    ids = list(dict.fromkeys(map(str, ids)))
    st = _state()
    slot = _default_slot(st)
    team = st.get("teams", {}).get(str(slot), {})
    collection = team.get(key, [])
    by_id = {str(p.get("_id") or p.get("spid") or p.get("id") or ""): p for p in collection}
    if any(ident not in by_id for ident in ids):
        return _json({"result": False, "reason": "INVALID_RECONTRACT_TARGET"})
    cap = RECONTRACT_PLAYER_MAX_LIFE if kind == "player" else RECONTRACT_STAFF_MAX_LIFE
    targets = [by_id[i] for i in ids]
    fees = []
    for item in targets:
        life = _player_life(item) if kind == "player" else max(0, int(item.get("life", cap) or 0))
        if life >= cap:
            fees.append(0)
        elif kind == "player":
            fees.append(_recontract_fee(item, cap - life))
        else:
            fee = max(0, int(item.get("contractfee", 0) or 0))
            fees.append(fee if fee else max(1, int(item.get("grade", 1))) * (cap - life))
    total = sum(fees) - _recontract_discount(sum(fees))
    coach = my_coach(st)
    balance = int(coach.get("ep", START_EP) or 0)
    if balance < total:
        return _json({"result": False, "reason": "PLAYER_RECONTRACT_ERROR_POPUP_INFO_NOTENOUGHTMONEY",
                      "ep": balance, "required": total, "lack": total - balance})
    for item in targets:
        item["life"] = cap
        if kind == "staff":
            item["contractfee"] = 0
    coach["ep"] = balance - total
    _save(st)
    # Was {"result": False} on SUCCESS (and True on failures) -- inverted vs
    # every other handler; the popup's completion handler reads it as a fail.
    return _json({"result": True, "ep": coach["ep"], key: targets,
                  "resources": coach_me_resources(st, slot)})


def ep_recontract_base_fee(query, body):
    """Fallback BASECONTRACTFEE lookup indexed by preferred-position OVR.
    The team screen's estimate is computed client-side as
    sum(BASECONTRACTFEE(ovr) * remaining) over the selected players,
    while the renewal dialog's total is the server preview, which uses
    _recontract_fee = round(ovr * 0.13) per remaining match. The two can
    only agree if this table carries the SAME per-match rate: a 10x
    experiment here (round(ovr * 1.3)) inverted the mismatch instead --
    the screen quoted 9,785 EP for a renewal the dialog charged 895 EP
    (user report 2026-11-12)."""
    return _json({str(ovr): max(1, round(ovr * 0.13)) for ovr in range(1, 201)})


def route(path):
    for pat, fn in ROUTES:
        m = pat.fullmatch(path)
        if m:
            return lambda q, b, g=m.groups(): fn(*g, q, b)


def ep_info_my_services_messenger_2(query, body):
    req_h = (_LAST_REQUEST_HOST or "").split(":")[0].strip()
    if req_h in ("127.0.0.1", "localhost", "APSEODESK172", ""):
        # Behind the VPS's local Nginx reverse proxy the request looks
        # local, but the client is remote: answering 127.0.0.1 here made
        # the client dial the messenger on its OWN machine (error 10061,
        # boot blocked -- Bug 4, 2026-11-11). Answer the public address
        # instead (the user's production fix, folded back; REDIR_HOST is
        # FO3_SERVER_IP, the public domain on the VPS).
        host = REDIR_HOST or "203.0.113.10"
    else:
        host = req_h or REDIR_HOST or "203.0.113.10"
    return _json({
        "result": True,
        "address": host,
        "port": 28002,
        "SERVER": {
            "address": host,
            "port": 28002
        }
    })

ENDPOINTS = {
    "/info/my/services/messenger/2": ep_info_my_services_messenger_2,
    "/info/server/config": ep_server_config,
    "/info/server/locale": ep_locale,
    "/info/server/time": ep_server_time,
    "/game/my/ipaddress": ep_my_ip,
    "/publisher/garena/sign": ep_garena_sign,
    "/publisher/nexon/2ndauth": ep_nexon_2ndauth,
    "/telemetry/gameconnection": ep_telemetry,
    "/shop/cash/charge/url": ep_shop_url,
    "/shop/cash/history/url": ep_shop_url,
}
ENDPOINTS["/coaches"] = ep_coaches
ENDPOINTS["/coach/me"] = ep_coach_me
ENDPOINTS["/coach/me/profile"] = ep_coach_profile
ENDPOINTS["/shop/remained/limited"] = lambda q, b: _json([])
ENDPOINTS["/shop/remained/cash"] = lambda q, b: _json([])
ENDPOINTS["/shop/remained/limited/STSHOP"] = lambda q, b: _json([])
ENDPOINTS["/shop/list/GPSHOP"] = ep_gpshop
ENDPOINTS["/coach/me/onready"] = ep_coach_onready
for _slot in (0, 1):  # two coach slots
    ENDPOINTS[f"/coach/{_slot}"] = lambda q, b, s=_slot: ep_create_coach(s, q, b)
    ENDPOINTS[f"/coach/{_slot}/new/step/3"] = lambda q, b, s=_slot: ep_new_team(s, q, b)
    ENDPOINTS[f"/coach/{_slot}/default"] = lambda q, b, s=_slot: ep_default_coach(s, q, b)
ENDPOINTS["/account"] = ep_account
ENDPOINTS["/account/option"] = ep_account_option
ENDPOINTS["/coach/me/squad"] = ep_coach_squad
ENDPOINTS["/match/campaign"] = ep_match_campaign
ENDPOINTS["/game/my/tactic/all"] = ep_tactic_all
for _i in range(TACTIC_SLOTS):
    ENDPOINTS[f"/game/my/tactic/{_i}"] = lambda q, b, i=_i: ep_tactic(i, q, b)
ENDPOINTS.update({
    "/league2/me": ep_l2_me, "/league2/reward": ep_l2_reward, "/league2/calendar": ep_l2_calendar,
    "/league2/match": ep_l2_match, "/league2/match/sim": lambda q, b: ep_l2_match(q, b, sim=True),
    "/league2/sleep": ep_l2_sleep, "/league2/end": ep_l2_end, "/league2/abandon": ep_l2_finish,
    "/league2/back": ep_telemetry, "/static/db/SORTTEAM": ep_sortteam,
    "/static/db/SORTNATION": ep_sortnation, "/static/db/SORTLEAGUE": ep_sortleague,
    "/static/db/SORTWORLDCUPTEAM": ep_sortworldcupteam,
    "/league2/sponsor/achievement": ep_l2_achievement,
    "/cup/list": ep_cup_list, "/cup/record": ep_cup_record, "/cup/new": ep_cup_new,
    "/mixin/recipes": lambda q, b: _json(craft_recipes()),  # craft tab's list (before /mixin/<id>)
    "/worldcup": ep_worldcup,
    "/transfer/offer": ep_transfer_offer, "/transfer/my/offer/list": lambda q, b: _json(offer_list(_state())),
    "/transfer/my/boostfactor": lambda q, b: _json(NO_BOOST),
    "/players/trade/proposal": ep_trade_proposal, "/players/trade/contract": ep_trade_contract,
    "/trade3/contract": ep_trade_contract, "/trade3/proposal": ep_trade3_proposal,
    "/players/recontract/preview": lambda q, b: ep_recontract_preview("player", q, b),
    "/players/recontract": lambda q, b: ep_recontract("player", q, b),
    "/staff/recontract/preview": lambda q, b: ep_recontract_preview("staff", q, b),
    "/staff/recontract": lambda q, b: ep_recontract("staff", q, b),
    "/static/db/BASECONTRACTFEE": ep_recontract_base_fee,
    "/trade3/staticvalues": lambda q, b: _json(TRADE3_STATIC), "/trade3/info": ep_trade3_info,
    "/trade3/notification": lambda q, b: _json({"result": True}),
    "/static/db/PLAYERTRADE": lambda q, b: _json({"playergrade": {str(s): trade_grades(s) for s in range(2, 101)}}),
    "/static/db/EXPPLAYER": lambda q, b: _json(EXPPLAYER), "/static/db/EXPCOACH": lambda q, b: _json(EXPCOACH),
    "/static/db/LOCKERROOMITEM": lambda q, b: _json(locker_catalog()),
    "/static/db/STATICVALUES/max_coachlevel": lambda q, b: _json(COACH_MAX_LEVEL),
    "/static/db/PE2STATICVALUES": lambda q, b: _json(PE2STATICVALUES),
    "/static/info/teamcolor": ep_teamcolor,
    "/club/me": lambda q, b: _json({"result": True, "clubid": 0, "name": ""}),
    "/shop/list": lambda q, b: _json(shop_list(_state())), "/shop/list/STSHOP": ep_stshop,
    "/specifictarget": ep_specifictarget, "/monthlypack": ep_monthlypack,
    "/shop/purchase": ep_shop_purchase, "/static/cache/item": ep_static_item,
    "/bag": lambda q, b: _json(bag_list(_state())), "/bag/unpackall": ep_bag_unpackall,
    "/players/trade/proposal/resend": lambda q, b: _json({"result": False, "reason": "NOT_SUPPORTED_REQUEST"}),
})


RESPONSES = os.path.join(HERE, "responses")


def from_file(path):
    """responses/<path, '/' -> '_'>.json overrides the handler; re-read every request so
    response shapes can be iterated against the live client without a restart."""
    f = os.path.join(RESPONSES, path.strip("/").replace("/", "_") + ".json")
    if os.path.isfile(f):
        with open(f, "rb") as fh:
            return ("application/json", fh.read())


class H(http.server.BaseHTTPRequestHandler):
    def _log(self, body):
        line = (f"{time.strftime('%H:%M:%S')} {self.command} {self.path} "
                f"from {self.client_address[0]} host={self.headers.get('Host')} "
                f"len={len(body)} body={body[:2000]!r}")  # UI error stack traces run long
        print(line, flush=True)
        with open(LOG, "a", encoding="utf-8") as f:
            f.write(line + "\n")

    def _respond(self):
        global _LAST_REQUEST_HOST
        _req_h = (self.headers.get("Host") or "").split(":")[0].strip()
        if _req_h:
            _LAST_REQUEST_HOST = _req_h
        length = int(self.headers.get("Content-Length") or 0)
        body = self.rfile.read(length) if length else b""
        if body[:2] == b"\x1f\x8b":  # the client gzips larger bodies (telemetry, /coach/me/squad)
            body = gzip.decompress(body)
        self._log(body)
        path = self.path.split("?")[0]
        query = self.path.split("?", 1)[1] if "?" in self.path else ""
        if self.command == "DELETE":
            if not query:
                query = "_method=DELETE"
            elif "_method=" not in query:
                query += "&_method=DELETE"
        # Unknown GETs get {}: a wrong shape in AS3 only raises a logged UI error, but []
        # where native code expects an object crashes the client (seen with
        # /static/db/PLAYER_EXTRA_GROWTH), and tables are id->row maps whose values get
        # iterated, so a stray "result": true becomes a bogus row. List endpoints get a
        # responses/ file with []. Unknown POSTs get {"result": true}.
        fallback = ep_telemetry if self.command == "POST" else ep_empty
        if path.startswith("/" + CDN_PREFIX):
            hit = cdn_file(path[len(CDN_PREFIX) + 1:])
            if hit is None:
                self.send_error(404)
                return
            ctype, data = hit
        else:
            try:
                with STATE_LOCK:
                    if path in browser_links.URLS:
                        if self.client_address[0] not in ("127.0.0.1", "::1"):
                            ctype, data = _json({"result": False})
                        else:
                            ctype, data = _json({"result": browser_links.open_link(path)})
                    else:
                        ctype, data = from_file(path) or (ENDPOINTS.get(path) or route(path) or fallback)(query, body)
            except Exception:
                # an empty reply is a curl error and the client quits (CLIENT_CLOSE); answer
                # something parseable and keep the traceback in the log instead
                import traceback
                self._log(traceback.format_exc().encode())
                ctype, data = _json({"result": False})
        self.send_response(200)
        self.send_header("Content-Type", ctype)
        self.send_header("Content-Length", str(len(data)))
        self.end_headers()
        self.wfile.write(data)

    do_GET = _respond
    do_POST = _respond
    do_PUT = _respond  # /game/my/ipaddress is a real PUT; 501 there made the client quit
    do_DELETE = _respond

    def log_message(self, *a):
        pass  # we do our own logging


# Offline overview stadium metadata (unknown real details are shown explicitly).

def ep_match_rules(query, body):
    rules = {"yellow_max": 2, "red_max": 1}
    return _json({
        "cards": {
            "default": rules,
            "ladder": rules,
            "Regional_League": rules,
            "league2": rules,
            "Cup_PvE": rules,
            "WorldCup_PvE": rules,
            "Exhibition": rules,
            "Friendly": rules,
            "Club_Match": rules,
            "ranking_1v1": rules,
            "ranking_2v2": rules,
            "ranking_3v3": rules,
            "custom": rules,
            "quick": rules,
            "roleplay": rules,
            "Ladder_PvP_1v1": rules,
            "Ladder_MGR_PvP_1v1": rules,
            "Friendly_PvP_1v1": rules,
        }
    })

POS_NAMES = [
    "GK", "SW", "RWB", "RB", "RCB", "CB", "LCB", "LB", "LWB",
    "RDM", "CDM", "LDM", "RM", "RCM", "CM", "LCM", "LM",
    "RAM", "CAM", "LAM", "RF", "CF", "LF", "RW", "RS", "ST", "LS", "LW"
]

_LIVEBOOST_CACHE = None

def get_liveboost_data():
    """Live Boost disabled as requested by user."""
    return {"expiretime": "1970-01-01T00:00:00Z", "liveplayers": {"top": []}}

def ep_liveboost_top(query, body):
    return _json(get_liveboost_data())

def ep_sortleague_common(nid, query, body):
    if nid is None or str(nid).strip() == "":
        return ep_sortleague(query, body)
    if str(nid) == "75":
        return _json([78])
    with closing(_db()) as c:
        rows = c.execute("SELECT DISTINCT leagueid FROM teams WHERE nationid = ?", (int(nid),)).fetchall()
        if rows:
            return _json([r[0] for r in rows])
    return _json([13, 14, 19, 31, 53, 16])

def ep_sortteam_common(lid, query, body):
    if lid is None or str(lid).strip() == "":
        return ep_sortteam(query, body)
    if str(lid) == "78":
        return _json(list(_NATIONAL_TEAMS.keys()))
    with closing(_db()) as c:
        rows = c.execute("SELECT teamid FROM teams WHERE leagueid = ? LIMIT 50", (int(lid),)).fetchall()
        if rows:
            return _json([r[0] for r in rows])
    return _json(list(_CLUB_TEAMS.keys())[:30])

ENDPOINTS['/static/db/MATCH_RULES'] = ep_match_rules
ENDPOINTS['/static/db/LIVEBOOST'] = ep_liveboost_top
ROUTES.insert(0, (re.compile(r'/static/db/MATCH_RULES.*'), ep_match_rules))
ROUTES.insert(0, (re.compile(r'/static/db/LIVEBOOST.*'), ep_liveboost_top))
ROUTES.insert(0, (re.compile(r'/static/db/SORTLEAGUE(?:/(\d+))?'), lambda nid, q, b: ep_sortleague_common(nid, q, b)))
ROUTES.insert(0, (re.compile(r'/static/db/SORTTEAM(?:/(\d+))?'), lambda lid, q, b: ep_sortteam_common(lid, q, b)))

def ep_static_stadium_dynamic(sid, query, body):
    sid_int = int(sid or 1)
    s = _CLUB_STADIUMS.get(sid_int)
    if not s:
        stadium_id_from_team = _CLUB_STADIUM_LINKS.get(sid_int)
        if stadium_id_from_team:
            s = _CLUB_STADIUMS.get(stadium_id_from_team, {})
    s = s or {}
    sname = s.get("name") or "Sân vận động"
    if "Bernab" in sname:
        sname = "Santiago Bernabéu"
    yb = int(s.get("yearbuilt") or s.get("buildyear") or s.get("year") or 1990)
    cap = int(s.get("capacity") or 50000)
    return _json({
        "id": sid_int,
        "stadiumid": sid_int,
        "name": sname,
        "stadiumname": sname,
        "yearbuilt": yb,
        "buildyear": yb,
        "year": yb,
        "capacity": cap
    })

ROUTES.insert(0, (re.compile(r'/(?:static/)?stadium(?:/(\d+))?'), ep_static_stadium_dynamic))
ROUTES.insert(0, (re.compile(r'/exhibition/info/authentic/team/(\d+)'), lambda tid, q, b: ep_authentic_team('all', tid, q, b)))

ENDPOINTS['/static/stadium/1'] = lambda q,b: _json({'id':1, 'name':'Sân vận động Quốc gia Mỹ Đình', 'yearbuilt':2008, 'capacity':68000})
ENDPOINTS['/stadium/1'] = ENDPOINTS['/static/stadium/1']
ENDPOINTS['/coach/new/reserve'] = ep_coach_reserve
ENDPOINTS['/game/my/team/captain'] = lambda q,b: ep_team_role('captain', q, b)
ROUTES.insert(0, (re.compile(r'/game/my/team/kickers/(\d+)'), ep_team_role))
ENDPOINTS['/shop/products/2001'] = lambda q,b: _json([shop_entry(2001, 'USE')])
import browser_links
import staff_service
staff_service.install(sys.modules[__name__]); import fo3_ext; fo3_ext.install(sys.modules[__name__])


if __name__ == "__main__":
    import argparse
    parser = argparse.ArgumentParser(description="FO3 Server Backend")
    parser.add_argument("--ip", "--host", default=os.environ.get("FO3_SERVER_IP", "203.0.113.10"), help="Server Public IP")
    parser.add_argument("--port", type=int, default=int(os.environ.get("FO3_PORT", PORT)), help="Server Port")
    parser.add_argument("--log-dir", default=os.environ.get("FO3_LOG_DIR", ""), help="Log directory")
    cli_args, _ = parser.parse_known_args()

    SERVER_IP = cli_args.ip
    REDIR_HOST = SERVER_IP
    NOTI_HOST = SERVER_IP
    if cli_args.port:
        PORT = cli_args.port
    if cli_args.log_dir and os.path.isdir(cli_args.log_dir):
        LOG = os.path.join(cli_args.log_dir, os.path.basename(LOG))

    http.server.ThreadingHTTPServer.allow_reuse_address = os.name != "nt"
    srv = http.server.ThreadingHTTPServer(("0.0.0.0", PORT), H)
    if TLS:
        ctx = ssl.SSLContext(ssl.PROTOCOL_TLS_SERVER)
        ctx.load_cert_chain(CERT)
        # Lazy handshake: a failed handshake (e.g. client rejects the cert) then
        # surfaces as a logged traceback in the handler thread instead of vanishing.
        srv.socket = ctx.wrap_socket(srv.socket, server_side=True, do_handshake_on_connect=False)
    print(f"http config stub on 0.0.0.0:{PORT}{' (https)' if TLS else ''}, log -> {LOG}", flush=True)
    launcher_port = int(os.environ.get("FO3_LAUNCHER_PORT", "8080"))
    if launcher_port:
        try:
            launcher_srv = http.server.ThreadingHTTPServer(("0.0.0.0", launcher_port), H)
            threading.Thread(target=launcher_srv.serve_forever, daemon=True, name="launcher_http").start()
            print(f"launcher auth server on http://0.0.0.0:{launcher_port}/", flush=True)
        except Exception as e:
            print(f"Warning: could not bind launcher port {launcher_port}: {e}", flush=True)
    admin_port = int(os.environ.get("FO3_ADMIN_PORT", "8088"))  # 0 = no admin page
    if admin_port:
        import admin
        admin.start(sys.modules[__name__], admin_port)
        print(f"admin page on http://127.0.0.1:{admin_port}/", flush=True)
    threading.Thread(target=warm_packs, daemon=True, name="packs").start()
    srv.serve_forever()
