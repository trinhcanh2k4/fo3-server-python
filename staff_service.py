"""Offline staff recruitment and assignment endpoints."""
import json
import os
import random
import re

hc = None
PACKS = (
    {"recruitid": 1, "icon": "broall", "recruittype": "normal", "staffpack_name": "Offline Staff - Basic",
     "staffpack_desc": "Recruit one staff member (1-2 stars). Free.", "ep": 0, "item": 0, "grades": (2, 4)},
    {"recruitid": 2, "icon": "silall", "recruittype": "normal", "staffpack_name": "Offline Staff - Advanced",
     "staffpack_desc": "Recruit one staff member (3-4 stars). 50,000 EP.", "ep": 50000, "item": 0, "grades": (6, 8)},
    {"recruitid": 3, "icon": "goldall", "recruittype": "normal", "staffpack_name": "All Staff - Elite",
     "staffpack_desc": "Recruit one staff member (5 stars). 150,000 EP.", "ep": 150000, "item": 0, "grades": (10,)},
)
# Names, portrait IDs and roles are sourced from the bundled catalog.
with open(os.path.join(os.path.dirname(__file__), 'gamedata', 'staff_catalog.json'), encoding='utf-8') as stream:
    CATALOG = json.load(stream)['staff']
TYPE_LABELS = (('FRONT', 'Technical'), ('PREMIUM', 'Head Coach'),
               ('MEDICAL', 'Medical'), ('FW', 'FW'), ('MF', 'MF'), ('DF', 'DF'), ('GK', 'GK'))
TYPES = tuple((typ, label, tuple(int(pid) for pid, info in CATALOG.items()
                               if info['position'] == typ and not info['material']), ())
              for typ, label in TYPE_LABELS)
STAT_KEYS = dict(zip(
    ('Acceleration|Aggresion|Agility|Balance|Ball Control|Cross|Dribble|Finishing|Free Kick|GK Diving|GK Goal Kick|GK Handling|GK Positioning|GK Reaction|Heading|Interception|Jump|Long Pass|Long Shot|Marking|Overall Rating|Penalty Kick|Positioning|Reaction|Shoot Curve|Shoot Power|Short Pass|Sliding Tackle|Sprint Speed|Stamina|Strength|Tackle|Vision|Volley').split('|'),
    ('acceleration|aggression|agility|balance|ballcontrol|crossing|dribbling|finishing|freekickaccuracy|gkdiving|gkkicking|gkhandling|gkpositioning|gkreflexes|headingaccuracy|interceptions|jumping|longpassing|longshots|marking|ovr|penalties|positioning|reactions|curve|shotpower|shortpassing|slidingtackle|sprintspeed|stamina|strength|standingtackle|vision|volleys').split('|')))

def recruit_effects(info, grade, selected_effects=None):
    # Option groups are sourced; numeric values remain offline balancing values.
    effects = []
    for index, group in enumerate(info['boost_groups'][:min(3, grade)], 1):
        choices = [STAT_KEYS[label] for label in group if label in STAT_KEYS]
        if choices:
            selected = None if selected_effects is None else selected_effects[index - 1]
            effect = random.choice(choices) if selected is None else STAT_KEYS.get(selected)
            if effect not in choices:
                raise ValueError('Selected coach boost is not in its catalog group')
            effects.append({'effectidx': index, 'effect': effect,
                            'amount': max(1, (grade + 1) // 2), 'grade': 1, 'open': True})
    return effects

SLOT_TYPES = {i + 1: row[0] for i, row in enumerate(TYPES)}
TYPE_BY_PORTRAIT = {pid: row[0] for row in TYPES for pid in row[2]}
ALL_PACKS = list(PACKS)
for i, (typ, label, portraits, effects) in enumerate(TYPES, 1):
    for tier, base in enumerate(PACKS, 1):
        icon = ('bro' + typ.lower()) if tier == 1 and typ in ('FW', 'MF', 'DF', 'GK') else base['icon']
        ALL_PACKS.append({**base, 'recruitid': 100 + i * 10 + tier, 'position': typ,
                          'icon': icon, 'staffpack_name': label + ' - ' + ('Basic', 'Advanced', 'Elite')[tier - 1]})
# Keep only five-star recruitment bundles in the offline client.
PACKS = tuple(p for p in ALL_PACKS if tuple(p.get("grades", ())) == (10,))
for pack in PACKS:
    pack['icon'] = 'staffpack/' + pack['icon']


def normalize_team(team):
    """Migrate legacy ALL staff and relocate appointments without losing owned cards."""
    staffs = team.setdefault('staffs', [])
    for staff in staffs:
        info = CATALOG.get(str(staff.get('stfid')))
        if info is not None:
            staff['commonname'] = info['name']
            staff['position'] = info['position']
            staff['material'] = info['material']
        elif staff.get('position') not in SLOT_TYPES.values():
            staff['position'] = 'FRONT'
        if str(staff.get('stfid')) in ('9502', '9505'):
            staff['commonname'] = 'Staff #' + str(staff['stfid']) + ' (unverified)'
    by_id = {p['_id']: p for p in staffs}
    old = team.get('staffroles') or []
    roles = [{'roleidx': 1, 'slotidx': i, 'open': True, 'staffid': None} for i in SLOT_TYPES]
    for previous in old:
        staff = by_id.get(previous.get('staffid'))
        if staff is None:
            continue
        target = next(r for r in roles if SLOT_TYPES[r['slotidx']] == staff['position'])
        if target['staffid'] is None:
            target['staffid'] = staff['_id']
    active = {r['staffid'] for r in roles if r['staffid']}
    for staff in staffs:
        if staff.get('status') != 'studying':
            staff['status'] = 'appointed' if staff['_id'] in active else 'idle'
        if not isinstance(staff.get('study'), dict):
            staff['study'] = {'history': {}}
    team['staffroles'] = roles
    team['staffroleidx'] = 1
    team['maxstaff'] = max(30, team.get('maxstaff') or 0)


def team_state(st):
    slot = hc._default_slot(st)
    team = st["teams"][str(slot)]
    normalize_team(team)
    return slot, team


def reply(st, slot, **extra):
    return hc._json({"result": True, **extra, "resources": hc.coach_me_resources(st, slot)})


def fail(reason):
    return hc._json({"result": False, "reason": reason})


def recruit_list(query, body):
    return hc._json([{k: v for k, v in p.items() if k != "grades"} for p in PACKS])


def recruit(recruitid, query, body):
    pack = next((p for p in PACKS if p["recruitid"] == int(recruitid)), None)
    if pack is None:
        return fail("INVALID_STAFF_PACK")
    req = json.loads(body or b"{}")
    if req.get("feetype", "ep") != "ep" or req.get("eventtype"):
        return fail("INVALID_STAFF_PAYMENT")
    st = hc._state()
    slot, team = team_state(st)
    if len(team["staffs"]) >= team["maxstaff"]:
        return fail("STAFF_LIMIT_EXCEEDED")
    coach = hc.my_coach(st)
    if coach.get("ep", hc.START_EP) < pack["ep"]:
        return fail("STAFF_EP_NOTENOUGH")
    category = next((r for r in TYPES if r[0] == pack.get('position')), None) or random.choice(TYPES)
    typ, label, pool, effects_pool = category
    portraits = [p for p in pool if os.path.isfile(os.path.join(hc.ASSETS, "externalAssets", "staffs", str(p) + ".png"))]
    if not portraits:
        return fail("STAFF_PORTRAITS_MISSING")
    grade, stfid = random.choice(pack["grades"]), random.choice(portraits)
    info = CATALOG[str(stfid)]
    effects = recruit_effects(info, grade)
    staff = {"_id": hc.new_iid(), "stfid": stfid, "commonname": info['name'],
             "position": typ, "grade": grade, "exp": 0, "gradeupto": 1000,
             "life": 100, "status": "idle", "effect": effects, "study": None,
             "material": False, "contractfee": 0, "new": True}
    team["staffs"].append(staff)
    coach["ep"] = coach.get("ep", hc.START_EP) - pack["ep"]
    hc._save(st)
    return reply(st, slot, staff=staff)


def appoint(staffid, query, body):
    req = json.loads(body or b"{}")
    idx, role = req.get("slot"), req.get("staffrole", 1)
    if type(idx) is not int or not 1 <= idx <= 7 or type(role) is not int or role != 1:
        return fail("INVALID_STAFF_SLOT")
    st = hc._state()
    slot, team = team_state(st)
    staff = next((p for p in team["staffs"] if p["_id"] == staffid), None)
    if staff is None or staff.get("life", 0) <= 0 or staff.get("status") == "studying":
        return fail("INVALID_STAFF")
    target = next((r for r in team["staffroles"] if r["roleidx"] == role and r["slotidx"] == idx), None)
    if target is None or not target.get("open"):
        return fail("INVALID_STAFF_SLOT")
    if staff['position'] != SLOT_TYPES[idx]:
        return fail('STAFF_POSITION_MISMATCH')
    previous_id = target.get('staffid')
    for r in team["staffroles"]:
        if r.get("staffid") == staffid:
            r["staffid"] = None
    target["staffid"] = staffid
    active = {r.get("staffid") for r in team["staffroles"] if r.get("open")}
    for p in team["staffs"]:
        p["status"] = "appointed" if p["_id"] in active else "idle"
    hc._save(st)
    previous = next((p for p in team['staffs'] if p['_id'] == previous_id), None)
    return reply(st, slot, appointed={'appointedstaff': staff, 'prevstaff': previous})


def unappoint(staffid, query, body):
    st = hc._state()
    slot, team = team_state(st)
    staff = next((p for p in team["staffs"] if p["_id"] == staffid), None)
    if staff is None:
        return fail("INVALID_STAFF")
    for r in team["staffroles"]:
        if r.get("staffid") == staffid:
            r["staffid"] = None
    staff["status"] = "idle"
    hc._save(st)
    return reply(st, slot)


def enhance(staffid, query, body):
    """Consume selected staff cards and apply their EXP to the target staff card.

    The client sends this as POST with ``_method: PUT``.  It then reads the
    refreshed /coach/me resource and the ``predictStaff``/``jackpot`` fields to
    animate the result screen.
    """
    try:
        req = json.loads(body or b'{}')
    except (TypeError, ValueError, UnicodeDecodeError):
        return fail('INVALID_STAFF_MATERIAL')
    material_ids = req.get('materials')
    if (not isinstance(material_ids, list) or not material_ids
            or any(not isinstance(sid, str) for sid in material_ids)
            or len(set(material_ids)) != len(material_ids)
            or staffid in material_ids):
        return fail('INVALID_STAFF_MATERIAL')

    st = hc._state()
    slot, team = team_state(st)
    staffs = team['staffs']
    target = next((p for p in staffs if p.get('_id') == staffid), None)
    materials = [next((p for p in staffs if p.get('_id') == sid), None)
                 for sid in material_ids]
    if target is None or target.get('status') == 'studying' or target.get('material'):
        return fail('INVALID_STAFF')
    if any(p is None or p.get('status') != 'idle' or p.get('material') for p in materials):
        return fail('INVALID_STAFF_MATERIAL')

    # Base EXP follows the material card's grade. Keep the original extra bonus
    # for special materials, while allowing ordinary coaches from another role
    # to contribute base EXP in this offline server (the stock UI previews these
    # as zero, which otherwise lets the user consume coaches for no progress).
    exp_gain = 0
    for material in materials:
        grade = max(0, int(material.get('grade') or 0))
        base_exp = grade * 100
        if material.get('position') == target.get('position'):
            multiplier = 3 if material.get('material') else 1
        else:
            multiplier = 2 if material.get('material') else 1
        exp_gain += base_exp * multiplier

    target['exp'] = max(0, int(target.get('exp') or 0)) + exp_gain
    target['gradeupto'] = max(1, int(target.get('gradeupto') or 1000))
    # The client static table uses grade thresholds; locally generated staff
    # cards use 1,000 EXP per grade. Carry any overflow into the next grade.
    while target['grade'] < 10 and target['exp'] >= target['gradeupto']:
        target['exp'] -= target['gradeupto']
        target['grade'] += 1

    consumed = set(material_ids)
    for role in team.get('staffroles', []):
        if role.get('staffid') in consumed:
            role['staffid'] = None
    team['staffs'] = [p for p in staffs if p.get('_id') not in consumed]
    hc._save(st)
    return reply(st, slot, predictStaff=target, jackpot=None,
                 consumedStaffIds=material_ids, exp=exp_gain)


# Offline Study rules: each location targets one existing boost group.
STUDY = {str(i): {'location': 'LOCATION_NAME_%02d' % i, 'grade': 1,
                 'position': [t[0] for t in TYPE_LABELS], 'matchcount': 1,
                 'fee': 1000 if i <= 3 else 2000,
                 'effectstring': 'Change one bonus within its original option group.' if i <= 3 else 'Improve one bonus by +1 (maximum +5).',
                 'effectidx': (i - 1) % 3 + 1,
                 'boosttype': 'change' if i <= 3 else 'enhance'} for i in range(1, 7)}
STUDY_MAX_FEE = 100000

def study_boost(staff, rule):
    info = CATALOG.get(str(staff.get('stfid')))
    # Try the location's normal benefit first. If it cannot be applied (for
    # example, all three of a coach's bonuses are already +5 at an enhance
    # location), use the other study benefit so the coach is not permanently
    # blocked from studying. Never exceed the +5 bonus cap.
    preferred_type = rule['boosttype']
    types = [preferred_type, 'change' if preferred_type == 'enhance' else 'enhance']
    for boosttype in types:
        if boosttype == 'enhance':
            indices = [rule['effectidx']] + [idx for idx in range(1, 4)
                                             if idx != rule['effectidx']]
            for idx in indices:
                current = next((e for e in staff.get('effect', [])
                                if e.get('effectidx') == idx and e.get('open')), None)
                if current is not None and current.get('amount', 0) < 5:
                    return {'effectidx': idx, 'boosttype': 'enhance',
                            'effect': current['effect'], 'inc': 1}
            continue

        if info is None:
            continue
        # Prefer the effect slot mapped to this location. Some coaches have a
        # singleton option group there (e.g. Park Hang-Seo's Sprint Speed), so
        # try other open slots with a valid alternative before falling back.
        preferred = rule['effectidx']
        indices = [preferred] + [idx for idx in range(1, 4) if idx != preferred]
        for idx in indices:
            current = next((e for e in staff.get('effect', [])
                            if e.get('effectidx') == idx and e.get('open')), None)
            if current is None or idx > len(info.get('boost_groups', [])):
                continue
            choices = [STAT_KEYS[label] for label in info['boost_groups'][idx - 1]
                       if label in STAT_KEYS and STAT_KEYS[label] != current['effect']]
            if choices:
                return {'effectidx': idx, 'boosttype': 'change',
                        'effect': random.choice(choices), 'inc': 0}
    return None

def study_start(lid, query, body):
    rule = STUDY.get(lid)
    if rule is None:
        return fail('ITEM_USE_ERROR')
    req = json.loads(body or b'{}')
    ids = req.get('staffs')
    if not isinstance(ids, list) or len(ids) != 1 or not isinstance(ids[0], str):
        return fail('INVALID_STAFF')
    st = hc._state()
    slot, team = team_state(st)
    if any(p.get('status') == 'studying' and (p.get('study') or {}).get('lid') == lid for p in team['staffs']):
        return fail('ITEM_USE_ERROR')
    staff = next((p for p in team['staffs'] if p['_id'] == ids[0]), None)
    if staff is None or staff.get('status') != 'idle' or staff.get('material') or staff.get('life', 0) <= 0:
        return fail('INVALID_STAFF')
    if staff.get('grade', 0) < rule['grade'] or staff.get('position') not in rule['position']:
        return fail('INVALID_STAFF')
    boost = study_boost(staff, rule)
    if boost is None:
        return fail('ITEM_USE_ERROR')
    history = (staff.get('study') or {}).get('history') or {}
    count = len(history.get(lid, []))
    fee = min(STUDY_MAX_FEE, rule['fee'] * 2 ** min(count, 16))
    coach = hc.my_coach(st)
    if coach.get('ep', hc.START_EP) < fee:
        return fail('STAFF_EP_NOTENOUGH')
    coach['ep'] = coach.get('ep', hc.START_EP) - fee
    staff['status'] = 'studying'
    staff['study'] = {'lid': lid, 'mcdate': int(team.get('staffsmc') or 0),
                      'history': history, 'boost': boost}
    hc._save(st)
    return reply(st, slot, staffs={staff['_id']: staff})

def study_complete(lid, query, body):
    rule = STUDY.get(lid)
    if rule is None:
        return fail('ITEM_USE_ERROR')
    st = hc._state()
    slot, team = team_state(st)
    # Older profiles can serialize a location id as an integer, and some client
    # paths update the status before issuing the return request. The study record
    # itself is the durable ownership marker, so use it to find the coach.
    staff = next((p for p in team['staffs']
                  if isinstance(p.get('study'), dict)
                  and str(p['study'].get('lid')) == lid), None)
    if staff is None:
        return fail('INVALID_STAFF')
    study = staff['study']
    # The retail client enables the Return button from its local timer/counter and
    # does not handle an ITEM_USE_ERROR response here (it dereferences the missing
    # staff result and raises ActionScript Error #1010). In this offline server
    # there is no reliable client-side match counter for study progress, so treat
    # Return as the completion action and apply the saved result immediately.
    boost = study.get('boost')
    if not isinstance(boost, dict):
        # Repair legacy/incomplete study records from the location rule. No EP
        # is charged again when returning a coach.
        boost = study_boost(staff, rule)
        if boost is None:
            return fail('INVALID_STAFF')
        study['boost'] = boost
    effect = next((e for e in staff.get('effect', [])
                   if e.get('effectidx') == boost.get('effectidx') and e.get('open')), None)
    if effect is None:
        return fail('INVALID_STAFF')
    if boost.get('boosttype') == 'change':
        effect['effect'] = boost.get('effect', effect.get('effect'))
    else:
        effect['amount'] = min(5, effect.get('amount', 0) + boost.get('inc', 1))
    history = study.get('history')
    if not isinstance(history, dict):
        history = {}
        study['history'] = history
    history.setdefault(lid, []).append(dict(boost))
    staff['status'] = 'idle'
    # Keep the result for the client's return popup, while freeing the location.
    study['lid'] = None
    hc._save(st)
    return reply(st, slot, staffs={staff['_id']: staff})


def install(module):
    global hc
    hc = module
    hc.ENDPOINTS["/staff/recruit"] = recruit_list
    hc.ROUTES[:0] = [
        (re.compile(r"/staff/study/(\d+)/complete"), study_complete),
        (re.compile(r"/staff/study/(\d+)"), study_start),
        (re.compile(r"/staff/recruit/(\d+)"), recruit),
        (re.compile(r"/staff/appoint/([0-9a-f]{24})"), appoint),
        (re.compile(r"/staff/unappoint/([0-9a-f]{24})"), unappoint),
        (re.compile(r"/staff/enhance/([0-9a-f]{24})"), enhance),
    ]
