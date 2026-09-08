"""Model-independent, nonblocking survival decisions and measured outcome memory.

Call survival_tick every few seconds while the LLM is idle. Each tick sends at
most ONE existing Mod command; subsequent snapshots, never prose from a model,
verify its outcome. No API calls, simulated loot or invented farming actions.
The memory is per survivor, bounded, atomic, and survives model/process changes.
"""

from collections import Counter
from contextlib import contextmanager
import json
import math
import os
import time

import tactics

VERSION = 1
PICKUP_REWARD_VERSION = 2
LOOT_REVISIT_SECONDS = 600
SEARCH_RADIUS = 240
SEARCH_STEPS = 6
MEDICINES = {"cholera": "TetracyclineAntibiotics",
             "influenza": "TetracyclineAntibiotics",
             "wound": "TetracyclineAntibiotics", "salmonella": "CharcoalTablets"}
PREDATORS = ("wolf", "bear", "canislupus", "ursus")
CAPABILITIES = {
    "implemented": ["bleeding", "threat_response", "food", "water", "clothing",
                    "heat", "disease_medication", "inventory", "loot", "hunt",
                    "animal_harvest", "ripe_crop_harvest", "cook", "exploration", "outcome_learning"],
    "limitations": [
        "Saeen und Pflanzenpflege fehlen; harvest_crops erntet ausschliesslich vorhandene reife Pflanzen.",
        "Herstellung nutzt bestehende recipes/craft-Tools; kein atomarer Craft-Mod-Befehl.",
        "Angeln ist im bestehenden fish-Tool simuliert; kein echter Angel-Mod-Befehl.",
        "Ohne food_stage/liquid_safe-Telemetrie bleiben Fleisch/Flascheninhalt ungeprueft.",
        "Lernen passt lokale Handlungswahl und Fehlerversuche an; keine Modellgewichte.",
    ],
}


def default_memory_path(bridge):
    from agent_paths import agent_home_dir
    return os.path.join(agent_home_dir(bridge.npc_id), "survival_learning.json")


def _new_memory():
    return {"version": VERSION, "stats": {}, "episodes": [], "pending": None,
            "places": {}, "visits": {}, "cooldowns": {}, "loot_targets": {},
            "search_steps": 0, "search_returning": False, "search_rest_until": 0,
            "search_sector": 0}


def load_memory(path):
    try:
        with open(path, encoding="utf-8") as stream:
            data = json.load(stream)
        if not isinstance(data, dict) or data.get("version") != VERSION:
            return _new_memory()
        memory = _new_memory()
        memory.update(data)
        for key in ("stats", "places", "visits", "cooldowns", "loot_targets"):
            if not isinstance(memory.get(key), dict):
                memory[key] = {}
        if not isinstance(memory.get("episodes"), list):
            memory["episodes"] = []
        return memory
    except (OSError, ValueError):
        return _new_memory()


def _save(path, memory):
    # Keep long sessions small; statistics persist for the most recent contexts.
    memory["episodes"] = memory["episodes"][-160:]
    for key, limit in (("stats", 512), ("places", 128), ("visits", 256),
                       ("cooldowns", 256), ("loot_targets", 128)):
        memory[key] = dict(list(memory[key].items())[-limit:])
    tmp = path + f".{os.getpid()}.tmp"
    with open(tmp, "w", encoding="utf-8") as stream:
        json.dump(memory, stream, ensure_ascii=False, indent=2)
    os.replace(tmp, path)


def _pid_alive(pid):
    if not isinstance(pid, int) or pid <= 0:
        return False
    if pid == os.getpid():
        return True
    if os.name == "nt":
        import ctypes
        kernel = ctypes.windll.kernel32
        kernel.OpenProcess.restype = ctypes.c_void_p
        handle = kernel.OpenProcess(0x1000, False, pid)
        if not handle:
            return kernel.GetLastError() == 5  # access denied: do not steal lease
        try:
            code = ctypes.c_ulong()
            return bool(kernel.GetExitCodeProcess(ctypes.c_void_p(handle),
                        ctypes.byref(code))) and code.value == 259
        finally:
            kernel.CloseHandle(ctypes.c_void_p(handle))
    try:
        os.kill(pid, 0)
        return True
    except PermissionError:
        return True
    except ProcessLookupError:
        return False


@contextmanager
def _memory_lock(path):
    """Nonblocking cross-process exclusion (Runner and stdio MCP share memory)."""
    os.makedirs(os.path.dirname(os.path.abspath(path)), exist_ok=True)
    lock = path + ".lock"
    acquired = False
    try:
        try:
            fd = os.open(lock, os.O_CREAT | os.O_EXCL | os.O_WRONLY)
        except FileExistsError:
            # Only remove a crashed owner's lock. Never expire an active owner.
            try:
                with open(lock, encoding="ascii") as stream:
                    owner = int(stream.read())
                if not _pid_alive(owner):
                    os.unlink(lock)
            except (OSError, ValueError):
                pass
            yield False
            return
        with os.fdopen(fd, "w", encoding="ascii") as stream:
            stream.write(str(os.getpid()))
        acquired = True
        yield True
    finally:
        if acquired:
            try:
                os.unlink(lock)
            except FileNotFoundError:
                pass


def travel_active(bridge):
    if getattr(bridge, "survival_travel_active", False):
        return True
    path = os.path.join(bridge.dir, f"travel_active_{bridge.npc_id}.json")
    try:
        with open(path, encoding="utf-8") as stream:
            lease = json.load(stream)
        return _pid_alive(lease.get("pid"))
    except (OSError, ValueError, AttributeError):
        return False


def _number(value, default=0.0):
    try:
        result = float(value)
        return result if math.isfinite(result) else default
    except (TypeError, ValueError):
        return default


def _flag(value, default=None):
    """DayZ serializes bools as both true/false and numeric 0/1, never strings."""
    if isinstance(value, bool):
        return value
    if isinstance(value, (int, float)) and value in (0, 1):
        return bool(value)
    return default


def _modern_bridge(state):
    try:
        version = tuple(int(p) for p in str(state.get("bridge_version", "0")).split(".")[:3])
        return version >= (0, 9, 0)
    except ValueError:
        return False


def _pos(npc):
    return (_number(npc.get("pos_x")), _number(npc.get("pos_z")))


def _cell(x, z):
    return f"{math.floor(x / 50)}:{math.floor(z / 50)}"


def _distance(npc, entity):
    return _number(entity.get("distance"), 999)


def _usable(item):
    return _number(item.get("health"), 100) > 5


def _matches(item, patterns):
    cn = str(item.get("classname", "")).lower()
    return any(pattern.lower() in cn for pattern in patterns)


def is_threat(entity):
    return entity.get("kind") == "infected" or (
        entity.get("kind") == "animal" and _matches(entity, PREDATORS))


def safe_food(item):
    if (not _usable(item) or _flag(item.get("frozen", False)) is not False
            or ("food_safe" in item and _flag(item["food_safe"]) is not True)
            or _matches(item, tactics.MEDICAL_PATTERNS)):
        return False
    if "human" in item.get("classname", "").lower():
        return False
    stage = item.get("food_stage")
    if stage in (5, 6, "BURNED", "ROTTEN", "burned", "rotten"):
        return False
    if _matches(item, tactics.RAW_FOOD_PATTERNS):
        return stage in (2, 3, 4, "BAKED", "BOILED", "DRIED", "baked", "boiled", "dried")
    return (item.get("kind") != "drink" and
            _matches(item, tactics.FOOD_PATTERNS) and _number(item.get("quantity"), 1) > 0)


def safe_drink(item):
    if not _usable(item) or _flag(item.get("frozen", False)) is not False or _number(item.get("quantity")) <= 0:
        return False
    if "liquid_safe" in item:
        return _flag(item["liquid_safe"]) is True and item.get("kind") == "drink"
    return _matches(item, ("SodaCan", "Pipsi", "Cola", "Spite", "Kvass", "Fronta"))


def _counts(state):
    counts = Counter()
    for item in state.get("inventory", []):
        counts[item.get("classname", "")] += max(1, _number(item.get("quantity"), 1))
    return dict(counts)


def _snapshot(state):
    npc = state.get("npc", {})
    return {"seq": state.get("seq"), "uptime": state.get("uptime"),
            "command_id": state.get("command", {}).get("id"),
            "npc": {key: npc.get(key) for key in (
                "health", "blood", "water", "energy", "stomach_volume", "heat_comfort",
                "wet", "bleeding", "alive", "pos_x", "pos_z", "in_hands", "disease")},
            "counts": _counts(state), "inventory": state.get("inventory", []),
            "ground": sorted(f"{e.get('classname')}:{e.get('x')}:{e.get('z')}"
                             for e in state.get("nearby", []) if e.get("kind") == "item"),
            "threat_distance": min([_distance(npc, e) for e in state.get("nearby", [])
                                    if is_threat(e)] or [999])}


def _plan(action, goal, reason, priority=50, **params):
    return {"action": action, "goal": goal, "reason": reason,
            "priority": priority, "params": params}


def _key(plan, state):
    params = plan["params"]
    location = _cell(*_pos(state.get("npc", {})))
    target = params.get("text") or params.get("item") or ""
    if "x" in params and "z" in params:
        target = _cell(params["x"], params["z"])
    return f"{plan['goal']}|{plan['action']}|{target}|{location}"


def _available(plan, state, memory, now):
    if plan.get("loot_target") and _loot_blocked(plan["loot_target"], memory, now):
        return False
    stat = memory["stats"].get(_key(plan, state), {})
    if _number(stat.get("retry_after")) > now:
        return False
    # Medication, digestion, etc. cooldowns apply across locations.
    global_key = plan["goal"] + ":" + plan["params"].get("text", "")
    return _number(memory["cooldowns"].get(global_key)) <= now


def _loot_target(item, npc):
    """Remember the observed object position, not the moving player's grid cell."""
    px, pz = _pos(npc)
    return {"classname": item.get("classname", ""),
            "x": _number(item.get("x"), px), "z": _number(item.get("z"), pz)}


def _loot_blocked(target, memory, now):
    for previous in memory.get("loot_targets", {}).values():
        if (previous.get("classname") == target["classname"]
                and _number(previous.get("retry_after")) > now
                and math.hypot(_number(previous.get("x")) - target["x"],
                               _number(previous.get("z")) - target["z"]) <= 8):
            return True
    return False


def _remember_loot(memory, target, now, reason, cooldown=LOOT_REVISIT_SECONDS):
    key = f"{target['classname']}|{round(target['x'])}:{round(target['z'])}"
    memory.setdefault("loot_targets", {})[key] = {
        **target, "retry_after": now + cooldown, "reason": reason}


def _pickup_plan(item, npc, inventory, goal, reason, priority):
    cn = item.get("classname", "")
    if not cn or item.get("parent") or item.get("in_hands") or item.get("worn"):
        return None
    # Near telemetry normally excludes inventory. Reject only a held copy
    # echoed at our feet; merely owning this class must not suppress supplies.
    held_copy = npc.get("in_hands") == cn or any(
        i.get("classname") == cn and _flag(i.get("in_hands")) is True for i in inventory)
    if held_copy and _distance(npc, item) <= 1.25:
        return None
    if _distance(npc, item) > 8:
        plan = _move(item, npc, "loot_route", "Zum sichtbaren Loot gehen", priority,
                     stop_distance=4)
    else:
        plan = _plan("pickup", goal, reason, priority, text=cn)
    if plan:
        plan["loot_target"] = _loot_target(item, npc)
    return plan


def _move(entity, npc, goal, reason, priority=50, stop_distance=2.5):
    tx, tz = _number(entity.get("x"), None), _number(entity.get("z"), None)
    if tx is None or tz is None:
        return None
    px, pz = _pos(npc)
    dx, dz = tx - px, tz - pz
    dist = math.hypot(dx, dz)
    factor = max(0, dist - stop_distance) / max(0.1, dist)
    # Short observable segments, to reconsider danger and needs every few seconds.
    factor = min(factor, 60.0 / max(0.1, dist))
    return _plan("move_to", goal, reason, priority, x=px + dx * factor, z=pz + dz * factor)


def _clothing_plan(state, heat, priority, drops=()):
    inventory, npc = state.get("inventory", []), state.get("npc", {})
    worn = {i.get("slot"): i for i in inventory if i.get("kind") == "clothing" and i.get("worn")}
    candidates = [i for i in inventory if i.get("kind") == "clothing" and not i.get("worn")]
    # Bewusst abgelegte Kleidung (eigene Ablage, z.B. Wellies) bleibt liegen.
    candidates += [e for e in state.get("nearby", []) if e.get("item_kind") == "clothing"
                   and _distance(npc, e) <= 10 and not e.get("near")
                   and not tactics.is_own_drop(e, drops, npc)]
    best, gain = None, 0
    for item in candidates:
        if item.get("slot") not in tactics.CLOTHING_SLOTS or not _usable(item):
            continue
        current = worn.get(item["slot"], {})
        if heat > .6:
            delta = _number(current.get("warmth")) - _number(item.get("warmth"))
        else:
            delta = tactics._clothing_score(item, heat < -.15) - (
                tactics._clothing_score(current, heat < -.15) if current else -1)
        if delta > gain and item.get("classname") != current.get("classname"):
            best, gain = item, delta
    if best:
        return _plan("wear", "clothing", "Kleidung an Wetter und Stauraum anpassen",
                     priority, text=best["classname"])


def choose_action(state, memory, now, agent_name="", allow_explore=True):
    """Pure policy: fixed safety priorities, outcome ranking inside each priority."""
    npc, inventory, nearby = state.get("npc", {}), state.get("inventory", []), state.get("nearby", [])
    available = lambda p: p is not None and _available(p, state, memory, now)
    inv = [i for i in inventory if _usable(i)]
    threats = [e for e in nearby if is_threat(e) and _distance(npc, e) < 25]
    near_threat = min([_distance(npc, e) for e in threats] or [999])
    # Eigene Ablagen (drop-Ledger): Auto-Loot lässt sie liegen, statt den
    # gerade weggeworfenen Ballast wieder einzusammeln (Viktor 08.09.).
    drops = tactics.load_drops(now=now)
    bleeding = _flag(npc.get("bleeding", False)) is True
    if bleeding:
        bandage = next((i for name in ("BandageDressing", "Rag") for i in inv
                        if i.get("classname") == name and _number(i.get("quantity"), 1) > 0), None)
        if bandage:
            p = _plan("treat_other", "bleeding", "Blutung sofort verbinden", 0,
                      target=agent_name or npc.get("name") or "", item=bandage["classname"])
            if available(p) and p["params"]["target"]:
                return p
        ground = [e for e in nearby if e.get("kind") == "item" and
                  _matches(e, ("BandageDressing", "Rag")) and _distance(npc, e) <= 12]
        for e in sorted(ground, key=lambda e: _distance(npc, e)):
            p = _pickup_plan(e, npc, inventory, "bleeding_supply",
                             "Verband fuer aktive Blutung beschaffen", 1)
            if available(p) and near_threat > 8:
                return p
        if not threats:
            return {"needs_llm": True, "reason": "Aktive Blutung: kein nutzbarer Verband erreichbar."}

    if threats:
        melee = tactics.pick_best_melee(inv)
        loaded = next((i for i in inv if tactics.classify_weapon(i.get("classname", "")) and
                       _number(i.get("quantity")) > 0), None)
        injured = _number(npc.get("health"), 100) < 45 or _number(npc.get("blood"), 5000) < 2500
        if bleeding or injured or len(threats) >= 3 or (not melee and not loaded):
            p = _plan("flee", "safety", "Abstand zu unmittelbarer Gefahr gewinnen", 2)
        elif _flag(npc.get("fighting", False)) is True:
            return {"reason": "Kampfsystem arbeitet; Lage weiter beobachten.", "needs_llm": False}
        else:
            preferred = loaded["classname"] if loaded and (
                len(threats) > 1 or any(e.get("kind") == "animal" for e in threats)) else melee
            preferred = preferred or (loaded["classname"] if loaded else "")
            if preferred and npc.get("in_hands") != preferred:
                p = _plan("equip", "safety", "Passende einsatzbereite Waffe ziehen", 2, text=preferred)
            else:
                p = _plan("engage", "safety", "Nahen Infizierten oder Raubtier abwehren", 2)
        if available(p):
            return p
        return {"reason": "Gefahrenreaktion scheiterte wiederholt; neue Taktik noetig.", "needs_llm": True}

    if _flag(npc.get("fighting", False)) is True:
        return {"reason": "Kampf aktiv; keine Routineaktion starten.", "needs_llm": False}
    heat = _number(npc.get("heat_comfort"))
    # Even when not hungry: never idle inside a damaging fire.
    fire = next((e for e in nearby if e.get("kind") == "fire_burning" and _distance(npc, e) < 5), None)
    raw = [i for i in inv if _matches(i, tactics.RAW_FOOD_PATTERNS)
           and i.get("food_stage") in (None, 1, "RAW", "raw")]
    if fire:
        # Cook once from its real 4-m range, then retreat on the following tick.
        cook = _plan("cook", "cook", "Rohes Fleisch am vorhandenen Feuer garen", 8)
        if raw and 2.5 < _distance(npc, fire) < 4 and available(cook):
            return cook
        px, pz = _pos(npc)
        dx, dz = px - _number(fire.get("x")), pz - _number(fire.get("z"))
        length = math.hypot(dx, dz)
        if length < .1:
            dx, dz, length = 1, 0, 1
        return _plan("move_to", "heat_safety", "Aus der Hitze des Feuers zuruecktreten", 4,
                     x=_number(fire.get("x")) + dx / length * 6,
                     z=_number(fire.get("z")) + dz / length * 6)

    water, energy = _number(npc.get("water"), 5000), _number(npc.get("energy"), 20000)
    stomach_ok = _number(npc.get("stomach_volume")) < 1000
    plans = []
    if water < 2200 and stomach_ok:
        priority = 10 if water < 700 else 25
        for item in inv:
            if safe_drink(item):
                plans.append(_plan("drink", "water", "Durst rechtzeitig stillen", priority, text=item["classname"]))
        for well in [e for e in nearby if e.get("kind") == "water"]:
            p = (_plan("drink_well", "water", "Sauberes Brunnenwasser trinken", priority)
                 if _distance(npc, well) <= 3.5 else
                 _move(well, npc, "water_route", "Zum sichtbaren Brunnen gehen", priority + 1))
            if p:
                plans.append(p)
    if energy < 2500 and stomach_ok:
        for item in inv:
            if safe_food(item):
                plans.append(_plan("eat", "food", "Sichere Nahrung essen", 11 if energy < 700 else 26,
                                   text=item["classname"]))
    if energy < 3000:
        for garden in nearby:
            if _modern_bridge(state) and garden.get("kind") == "garden" and _flag(garden.get("harvestable")) is True:
                p = (_plan("harvest_crops", "crop_harvest", "Reife Pflanzen als Nahrung ernten", 31)
                     if _distance(npc, garden) <= 3 else
                     _move(garden, npc, "crop_route", "Zum sichtbaren erntereifen Garten gehen", 32))
                if p:
                    plans.append(p)
    if heat < -.3 or heat > .6:
        p = _clothing_plan(state, heat, 15, drops)
        if p:
            plans.append(p)
    if heat < -.3 and _number(npc.get("health"), 100) >= 45:
        for fire in nearby:
            if fire.get("kind") == "fire_burning" and _distance(npc, fire) > 7:
                p = _move(fire, npc, "warm_route", "Mit Sicherheitsabstand am Feuer waermen", 18,
                          stop_distance=6)
                if p:
                    plans.append(p)
    disease = npc.get("disease") or {}
    for pathogen, amount in (disease.get("agents") or {}).items():
        med = MEDICINES.get(pathogen)
        if _number(amount) > 0 and med and any(i.get("classname") == med for i in inv):
            if _modern_bridge(state):
                plans.append(_plan("take_medicine", "medicine", f"Gemeldeten Erreger {pathogen} gezielt behandeln", 20, text=med))

    # Recovery from a confirmed full-inventory failure: only discard ruined,
    # unworn, empty items. Never bulk-store essential supplies or worn clothing.
    if memory.get("inventory_full"):
        for item in inventory:
            cn = item.get("classname", "")
            # Mod drop addresses a CLASSNAME, not an instance. Ambiguous
            # duplicates could drop the healthy or worn copy first. Unknown
            # containers have no cargo telemetry, so leave those to the LLM.
            simple = (item.get("kind") in ("food", "medicine", "ammo", "firearm") or
                      bool(tactics.classify_melee(cn)))
            ambiguous = any(other.get("classname") == cn and (
                _usable(other) or other.get("worn") or other.get("in_hands") or other.get("parent"))
                for other in inventory)
            attached = any(other.get("parent") == cn for other in inventory)
            if (not _usable(item) and not item.get("worn") and not item.get("in_hands")
                    and not item.get("parent") and not item.get("cargo_size")
                    and simple and not ambiguous and not attached):
                plans.append(_plan("drop", "inventory", "Ruinierte lose Ausruestung schafft Platz", 30,
                                   text=cn))

    # No long-lived inventory assumptions: every candidate is visible NOW.
    owned = Counter(i.get("classname") for i in inventory)
    for item in nearby:
        if memory.get("inventory_full"):
            break
        if item.get("kind") != "item" or item.get("item_kind") == "clothing":
            continue
        if item.get("near") and _distance(npc, item) > 5:
            continue
        if tactics.is_own_drop(item, drops, npc):
            continue
        cn = item.get("classname", "")
        if owned[cn] >= (3 if _matches(item, tactics.FOOD_PATTERNS + tactics.MEDICAL_PATTERNS) else 2):
            continue
        score = tactics.score_ground_item(item, inventory)
        if score <= 0:
            continue
        priority, goal = 60, "loot"
        if water < 2200 and _matches(item, ("SodaCan", "Canteen", "WaterBottle", "Pipsi", "Cola")):
            priority, goal = 28, "water_supply"
        elif energy < 2500 and _matches(item, tactics.FOOD_PATTERNS):
            priority, goal = 29, "food_supply"
        plan = _pickup_plan(item, npc, inventory, goal,
                            "Nuetzliches sichtbares Loot einsammeln", priority)
        if plan:
            plan["utility"] = score - _distance(npc, item) * .5
            plans.append(plan)
    p = _clothing_plan(state, heat, 45, drops)
    if p:
        plans.append(p)
    held = next((i for i in inv if i.get("in_hands")), {})
    if tactics.classify_weapon(held.get("classname", "")) and _number(held.get("quantity")) <= 0:
        if any(i.get("classname", "").startswith("AmmoBox_") for i in inv):
            plans.append(_plan("unpack_ammo", "ammo", "Vorhandene Munitionsschachtel oeffnen", 48))
        elif any(i.get("kind") == "ammo" and _number(i.get("quantity")) > 0 for i in inv):
            plans.append(_plan("reload", "reload", "Leere Waffe mit vorhandener Munition laden", 49))
    knife = any(_matches(i, ("Knife", "Machete", "Cleaver", "Hatchet", "Axe")) for i in inv)
    if energy < 3000 and knife:
        for corpse in nearby:
            if corpse.get("kind") == "animal_corpse" and _distance(npc, corpse) < 50:
                plans.append(_plan("harvest", "harvest", "Erlegte Beute als Nahrung zerlegen", 40,
                                   text=corpse["classname"]))
        if not raw and not any(safe_food(i) for i in inv):
            for prey in nearby:
                if prey.get("kind") != "animal" or is_threat(prey):
                    continue
                loaded = any(tactics.classify_weapon(i.get("classname", "")) and
                             _number(i.get("quantity")) > 0 for i in inv)
                if loaded or _matches(prey, ("Gallus", "Chicken", "Rabbit", "Lepus")):
                    if loaded and not (tactics.classify_weapon(held.get("classname", "")) and
                                       _number(held.get("quantity")) > 0):
                        plans.append(_plan("equip_best", "hunt_prepare", "Geladene Jagdwaffe vorbereiten", 41))
                    else:
                        plans.append(_plan("hunt", "hunt", "Sichtbares Wild fuer Nahrung jagen", 42,
                                           text=prey["classname"]))
    if raw:
        for fire in nearby:
            if fire.get("kind") == "fire_burning" and available(
                    _plan("cook", "cook", "", 35)):
                p = _move(fire, npc, "cook_route", "Fleisch am sichtbaren Feuer zubereiten", 35, stop_distance=3.5)
                if p:
                    plans.append(p)
            elif fire.get("kind") == "fire" and any(_matches(i, ("Matchbox", "PetrolLighter")) for i in inv):
                p = (_plan("light_fire", "fire", "Vorhandenes Lagerfeuer anzuenden", 35)
                     if _distance(npc, fire) <= 2.8 else
                     _move(fire, npc, "fire_route", "Zum vorhandenen Lagerfeuer gehen", 35))
                if p:
                    plans.append(p)
    candidates = [p for p in plans if available(p)]
    if candidates:
        # Learning can choose the better of equally safe alternatives, but can
        # never rank hunting or loot ahead of survival priorities.
        def rank(plan):
            stat = memory["stats"].get(_key(plan, state), {})
            learned = max(-20, min(20, _number(stat.get("mean_reward")) * 5))
            if plan["action"] == "pickup" and stat.get("reward_version") != PICKUP_REWARD_VERSION:
                # Earlier versions rewarded Apple/Pear hand swaps. Keep the
                # historical record, but do not use it to rank fresh pickups.
                learned = 0
            return (plan["priority"], -(_number(plan.get("utility")) + learned))
        return min(candidates, key=rank)
    if memory.get("inventory_full"):
        return {"needs_llm": True, "reason": "Inventar voll: geschuetzte Ausruestung gezielt sortieren oder verstauen."}
    if raw:
        return {"needs_llm": True, "reason": "Rohes Fleisch vorhanden: Feuer benoetigt vorhandenes Material und craft/cook_meal."}
    # Only route to actual reported shelter/entrance coordinates. A building
    # classname or remembered map stereotype does not establish a safe doorway.
    shelter_needed = heat < -.3 or heat > .6 or _number(npc.get("wet")) > .25 or _number(state.get("world", {}).get("rain")) > .2
    if allow_explore and shelter_needed:
        for place in sorted(nearby, key=lambda e: _distance(npc, e)):
            if place.get("kind") == "shelter":
                entrance = place
            elif place.get("kind") == "building" and place.get("entrance_x") is not None and place.get("entrance_z") is not None:
                entrance = {"x": place["entrance_x"], "z": place["entrance_z"]}
            else:
                continue
            if math.hypot(_number(entrance.get("x")) - _pos(npc)[0],
                          _number(entrance.get("z")) - _pos(npc)[1]) <= 4:
                return {"needs_llm": False, "reason": "Am beobachteten Unterstand; Wetter und Zustand beobachten."}
            p = _move(entrance, npc, "shelter_route", "Zum beobachteten Unterstand gehen", 70)
            if available(p):
                return p
    if heat < -.5 or heat > .8:
        return {"needs_llm": True, "reason": "Extreme Temperatur: Schutz/Feuer/Lager planen; kein besseres Kleidungsstueck sichtbar."}
    if not allow_explore:
        return {"needs_llm": water < 700 or energy < 700,
                "reason": "Keine sichere Routineaktion sichtbar; autonome Erkundung ist aus."}
    if any(e.get("kind") == "player" and _distance(npc, e) < 30 for e in nearby):
        return {"needs_llm": True, "reason": "Mensch in der Naehe: Kontakt und Auftrag klaeren."}
    px, pz = _pos(npc)
    anchor = memory.get("anchor") or [px, pz]
    anchor_distance = math.hypot(px - anchor[0], pz - anchor[1])
    if (memory.get("search_returning") or memory.get("search_steps", 0) >= SEARCH_STEPS
            or anchor_distance >= SEARCH_RADIUS):
        if anchor_distance <= 5:
            return {"needs_llm": True, "reason": "Suchkreis beendet; wieder am bekannten Ausgangspunkt."}
        p = _move({"x": anchor[0], "z": anchor[1]}, npc, "return",
                  "Suchkreis beendet: zum bekannten Ausgangspunkt zurueckkehren", 90)
        if available(p):
            return p
        return {"needs_llm": True, "reason": "Bekannter Rueckweg blockiert; neue Route benoetigt."}
    if _number(memory.get("search_rest_until")) > now:
        return {"needs_llm": False, "reason": "Nach Rueckkehr kurz Umgebung pruefen; naechster Suchsektor folgt."}
    # Useful landmarks already in sight beat an arbitrary compass step.
    # Remembered places are not assumed still present or reachable.
    for place in sorted(nearby, key=lambda e: _distance(npc, e)):
        if place.get("kind") != "water" or _distance(npc, place) <= 5:
            continue
        if place.get("x") is None or place.get("z") is None:
            continue
        if math.hypot(_number(place["x"]) - anchor[0], _number(place["z"]) - anchor[1]) > SEARCH_RADIUS:
            continue
        site_key = "site:" + _cell(_number(place["x"]), _number(place["z"]))
        if _number(memory["cooldowns"].get(site_key)) > now:
            continue
        p = _move(place, npc, "explore_site", "Sichtbare Wasserstelle als Versorgungspunkt erkunden", 85,
                  stop_distance=4)
        if available(p):
            p["site_key"] = site_key
            p["site_target"] = [_number(place["x"]), _number(place["z"])]
            return p
    explore = []
    for order in range(8):
        n = (order + int(_number(memory.get("search_sector")))) % 8
        angle = math.radians(n * 45)
        tx, tz = px + math.sin(angle) * 60, pz + math.cos(angle) * 60
        if tx < 0 or tz < 0 or math.hypot(tx - anchor[0], tz - anchor[1]) > 280:
            continue
        p = _plan("move_to", "explore", "Kurzer Suchbogen in wenig erkundetes Gebiet", 90, x=tx, z=tz)
        if available(p):
            stat = memory["stats"].get(_key(p, state), {})
            explore.append((_number(memory["visits"].get(_cell(tx, tz))) -
                            _number(stat.get("mean_reward")) * .1, order, p))
    if explore:
        return min(explore, key=lambda row: row[:2])[2]
    return {"needs_llm": True, "reason": "Bekannte Wege blockiert; neue Route planen."}


def _record_outcome(memory, pending, state, command, now):
    before, after = pending["before"], _snapshot(state)
    old, npc = before["npc"], after["npc"]
    goal, action = pending["goal"], pending["action"]
    cn = pending["params"].get("text") or pending["params"].get("item") or ""
    count_delta = _number(after["counts"].get(cn)) - _number(before["counts"].get(cn))
    deltas = {key: round(_number(npc.get(key)) - _number(old.get(key)), 3)
              for key in ("health", "blood", "water", "energy", "stomach_volume", "heat_comfort")
              if old.get(key) is not None and npc.get(key) is not None}
    verified = None
    evidence = "Kein nachweisbarer Zieleffekt im Snapshot."
    if action == "move_to":
        target = (pending["params"]["x"], pending["params"]["z"])
        progress = math.dist(_pos(old), target) - math.dist(_pos(npc), target)
        arrived = math.dist(_pos(npc), target) <= 2 and progress > .3
        verified, evidence = progress >= 3 or arrived, f"Zielannaeherung {progress:.1f} m"
    elif goal == "bleeding":
        verified = _flag(old.get("bleeding")) is True and _flag(npc.get("bleeding")) is False
        evidence = "Blutung gestoppt" if verified else "Blutung weiterhin gemeldet"
        if not verified and count_delta < 0:
            # The Mod closes one source at a time. A remaining second cut is
            # a reason for another dressing, not a 60-second failure backoff.
            verified = True
            evidence = "Verband verbraucht; weitere Blutung gemeldet, naechste Quelle versorgen"
    elif action == "pickup":
        lost = [name for name, quantity in before["counts"].items()
                if name != cn and _number(after["counts"].get(name)) < _number(quantity)]
        verified = count_delta > 0 and not lost and command.get("status") == "done"
        evidence = f"{cn}-Zuwachs {count_delta:.1f}; verlorene Inventarklassen: " + (", ".join(lost) or "keine")
        target = pending.get("loot_target") or {"classname": cn, "x": _pos(old)[0], "z": _pos(old)[1]}
        _remember_loot(memory, target, now, "aufgenommen" if verified else "kein Nettozuwachs",
                       cooldown=30 if verified else LOOT_REVISIT_SECONDS)
        # The old Mod could drop a held item to pick another one up. Never
        # chase that self-created ground loot and swap the same pair forever.
        for name in lost:
            dropped = next((e for e in state.get("nearby", [])
                            if e.get("kind") == "item" and e.get("classname") == name
                            and _distance(npc, e) <= 8), None)
            if dropped:
                _remember_loot(memory, _loot_target(dropped, npc), now, "beim Einsammeln verloren")
    elif action in ("harvest", "loot_corpse"):
        gain = sum(after["counts"].values()) - sum(before["counts"].values())
        verified, evidence = gain > 0, f"Inventarzuwachs {gain:.1f}"
    elif goal in ("food", "water"):
        vital = "energy" if goal == "food" else "water"
        verified = deltas.get(vital, 0) > 0 or deltas.get("stomach_volume", 0) > 0
        evidence = f"{vital}-Delta {deltas.get(vital, 0):.1f}; Magen-Delta {deltas.get('stomach_volume', 0):.1f}"
    elif goal == "medicine":
        verified = count_delta < 0
        evidence = "Medikamentverbrauch nachgewiesen; Heilung NICHT bestaetigt" if verified else evidence
    elif action == "harvest_crops":
        gained_ground = set(after["ground"]) - set(before.get("ground", []))
        verified = bool(gained_ground) or sum(after["counts"].values()) > sum(before["counts"].values())
        evidence = f"Neue Ernte-Objekte sichtbar: {len(gained_ground)}" if verified else evidence
    elif action == "wear":
        verified = any(i.get("classname") == cn and i.get("worn") for i in state.get("inventory", []))
        evidence = "Kleidung am Koerper bestaetigt" if verified else evidence
    elif action in ("equip", "equip_best"):
        verified = bool(npc.get("in_hands")) and (npc.get("in_hands") == cn if cn else npc.get("in_hands") != old.get("in_hands"))
        evidence = "Handbelegung bestaetigt" if verified else evidence
    elif action == "reload":
        held = npc.get("in_hands")
        ammo = lambda snap: sum(_number(i.get("quantity")) for i in snap["inventory"] if i.get("classname") == held)
        verified = ammo(after) > ammo(before)
        evidence = "Geladene Munition gestiegen" if verified else evidence
    elif action == "drop":
        verified, evidence = count_delta < 0, f"Inventardelta {count_delta:.1f}"
        if verified and command.get("status") == "done":
            # Eigene Ablage merken: Auto-Loot (hier UND in dayz_mcp) lässt sie liegen.
            tactics.remember_drop(cn, _pos(old)[0], _pos(old)[1], now=now)
    elif action == "cook":
        def cooked(snapshot):
            return sum(i.get("food_stage") in (2, 3, 4) for i in snapshot["inventory"])
        verified = cooked(after) > cooked(before) if any("food_stage" in i for i in after["inventory"]) else None
        evidence = "Gegarte FoodStage-Zustaende gestiegen" if verified else "Garen ohne FoodStage-Nachweis"
    elif action == "hunt":
        verified = any(e.get("kind") == "animal_corpse" and e.get("classname") == cn for e in state.get("nearby", []))
        evidence = "Passender Tierkadaver beobachtet" if verified else evidence
    elif action in ("engage", "flee"):
        verified = after["threat_distance"] > before["threat_distance"] + 10
        evidence = "Bedrohungsabstand gestiegen" if verified else evidence
    elif action == "light_fire":
        verified = any(e.get("kind") == "fire_burning" and e.get("distance", 999) < 5 for e in state.get("nearby", []))
        evidence = "Brennendes Feuer beobachtet" if verified else evidence
    if command.get("status") != "done":
        verified = False
    if action == "move_to" and pending.get("loot_target") and not verified:
        _remember_loot(memory, pending["loot_target"], now, "Lootweg nicht erreichbar")
    reward = (1 if verified is True else -.5 if verified is False else 0)
    reward += max(-5, min(1, deltas.get("health", 0) / 10 + deltas.get("blood", 0) / 500))
    if _flag(npc.get("alive")) is False:
        reward -= 10
    key = pending["key"]
    stat = memory["stats"].setdefault(key, {"attempts": 0, "verified": 0, "failures": 0,
                                           "streak": 0, "mean_reward": 0})
    stat["attempts"] += 1
    stat["verified"] += int(verified is True)
    stat["failures"] += int(verified is False)
    stat["streak"] = 0 if verified is True else stat["streak"] + 1
    if action == "pickup":
        if stat.get("reward_version") != PICKUP_REWARD_VERSION:
            stat["reward_version"] = PICKUP_REWARD_VERSION
            stat["reward_samples"] = 0
            stat["mean_reward"] = 0
        stat["reward_samples"] += 1
        samples = stat["reward_samples"]
    else:
        samples = stat["attempts"]
    stat["mean_reward"] += (reward - stat["mean_reward"]) / min(20, samples)
    stat["last_at"] = now
    stat["retry_after"] = now + (min(600, 30 * 2 ** min(stat["streak"], 4)) if stat["streak"] else 3)
    # Drug effect takes time; consuming more every tick is not treatment.
    cooldown = 300 if goal == "medicine" else 30 if goal in ("food", "water", "cook") else 0
    if cooldown:
        memory["cooldowns"][goal + ":" + pending["params"].get("text", "")] = now + cooldown
    detail = str(command.get("detail") or "")[:300]
    if action == "pickup" and "platz" in detail.lower():
        memory["inventory_full"] = True
    elif action in ("drop", "pickup", "wear") and verified:
        memory["inventory_full"] = False
    if goal in ("explore", "explore_site"):
        memory["search_steps"] = memory.get("search_steps", 0) + 1
    if pending.get("site_key") and (not verified or math.dist(_pos(npc), pending["site_target"]) <= 6):
        memory["cooldowns"][pending["site_key"]] = now + 300
    if goal == "return" and verified and math.dist(_pos(npc), memory.get("anchor") or _pos(old)) <= 5:
        memory["search_returning"] = False
        memory["search_steps"] = 0
        memory["search_rest_until"] = now + 45
        memory["search_sector"] = (int(_number(memory.get("search_sector"))) + 2) % 8
    if action in ("pickup", "harvest") and verified:
        memory["search_steps"] = 0
    if action == "move_to" and verified:
        cell = _cell(*_pos(npc))
        memory["visits"][cell] = memory["visits"].get(cell, 0) + 1
    episode = {"at": now, "goal": goal, "action": action, "target": cn,
               "status": command.get("status"), "verified": verified,
               "evidence": evidence, "delta": deltas, "reward": round(reward, 3),
               "detail": detail, "before_seq": before["seq"], "after_seq": after["seq"]}
    memory["episodes"].append(episode)
    memory["pending"] = None
    return episode


def survival_tick(bridge, agent_name="", memory_path=None, allow_explore=True, now=None):
    """Short, nonblocking tick. Caller must serialize it against LLM dispatch.

    Returns status, reason, needs_llm and optionally action/command_id/learning.
    Never takes over an external action, following, a vehicle or active travel.
    A pending command belongs to this controller only if its id is in memory.
    """
    now = time.time() if now is None else now
    memory_path = memory_path or default_memory_path(bridge)
    # Drop-Ledger liegt neben dem Survival-Gedächtnis (ein Agent pro Prozess);
    # dayz_mcp setzt für seinen Prozess denselben Pfad über VOICE_OUTBOX.
    tactics.DROP_LEDGER = os.path.join(os.path.dirname(memory_path), "dropped_items.json")
    try:
        with _memory_lock(memory_path) as acquired:
            if not acquired:
                return {"status": "busy", "needs_llm": False, "reason": "Anderer Survival-Tick aktiv."}
            state = bridge.read_state() or {}
            if not state or state.get("seq") is None:
                return {"status": "unavailable", "needs_llm": False, "reason": "Kein gueltiger Server-Snapshot."}
            state_file = getattr(bridge, "state_file", None)
            if state_file and (not os.path.isfile(state_file) or now - os.path.getmtime(state_file) > 15):
                return {"status": "stale", "needs_llm": False, "reason": "Server-Snapshot ist veraltet."}
            memory = load_memory(memory_path)
            npc, cmd = state.get("npc", {}), state.get("command", {})
            snapshot_id = [state.get("seq"), state.get("uptime")]
            if snapshot_id == memory.get("last_snapshot"):
                return {"status": "stale", "needs_llm": False, "reason": "Warte auf neuen Server-Tick."}
            memory["last_snapshot"] = snapshot_id
            learning = None
            pending = memory.get("pending")
            restarted = (pending and _number(state.get("uptime")) < _number(pending["before"].get("uptime")))
            if restarted:
                memory["episodes"].append({"at": now, "action": pending["action"],
                                           "status": "server_restarted", "verified": None})
                memory["pending"] = None
                pending = None
                memory["anchor"] = None
                memory["search_steps"] = 0
                memory["places"] = {}
                memory["search_returning"] = False
                memory["search_rest_until"] = 0
                memory["loot_targets"] = {}
            elif pending and _flag(npc.get("alive")) is False:
                learning = _record_outcome(memory, pending, state,
                                          {"status": "interrupted", "detail": "Tod"}, now)
                pending = None
                memory["anchor"] = None
                memory["search_steps"] = 0
                memory["search_returning"] = False
                memory["search_rest_until"] = 0
            elif pending and cmd.get("id") == pending.get("command_id") and cmd.get("status") in ("done", "failed"):
                learning = _record_outcome(memory, pending, state, cmd, now)
                pending = None
            elif (pending and cmd.get("id") != pending.get("command_id") and
                  cmd.get("id") != pending["before"].get("command_id")):
                # Another tool took control. That is not evidence the action failed.
                memory["episodes"].append({"at": now, "action": pending["action"],
                                           "status": "superseded", "verified": None})
                memory["pending"] = None
                pending = None

            def finish(status, reason, needs_llm=False, **extra):
                _save(memory_path, memory)
                return {"status": status, "reason": reason, "needs_llm": needs_llm,
                        **({"learning": learning} if learning else {}), **extra}

            for flag in ("alive", "bleeding", "unconscious", "in_vehicle", "following", "fighting"):
                if flag in npc and _flag(npc[flag]) is None:
                    return finish("unavailable", "Unklares Zustandsflag im Snapshot; keine Routineaktion.")
            if _flag(npc.get("alive")) is not True or _flag(npc.get("unconscious")) is True or _flag(npc.get("in_vehicle")) is True:
                return finish("inactive", "Tot, bewusstlos oder im Fahrzeug; keine Routineaktion.")
            if _flag(npc.get("following")) is True or travel_active(bridge):
                return finish("busy", "Folgeauftrag oder Reise bleibt aktiv.")
            if cmd.get("action") == "stop" and not pending:
                return finish("busy", "Ausdruecklicher Halt bleibt aktiv bis zu einem neuen Auftrag.")
            if cmd.get("status") == "running" and not pending:
                return finish("busy", "Anderes Werkzeug fuehrt bereits eine Aktion aus.")
            if memory.get("anchor") is None:
                memory["anchor"] = list(_pos(npc))
            elif math.dist(memory["anchor"], _pos(npc)) > 350 and not pending:
                # A player or LLM moved to a new place; this becomes the local
                # search origin. The controller itself never exceeds 280 m.
                memory["anchor"] = list(_pos(npc))
                memory["search_steps"] = 0
                memory["search_returning"] = False
                memory["search_rest_until"] = 0
            for e in state.get("nearby", []):
                if e.get("kind") in ("water", "fire", "fire_burning"):
                    key = e.get("kind") + ":" + _cell(_number(e.get("x")), _number(e.get("z")))
                    memory["places"][key] = {"x": e.get("x"), "z": e.get("z"), "last_seen": now,
                                             "kind": e.get("kind")}
                    if e.get("kind") == "water" and _distance(npc, e) <= 5:
                        memory["cooldowns"]["site:" + _cell(_number(e.get("x")), _number(e.get("z")))] = now + 300
            decision = choose_action(state, memory, now, agent_name, allow_explore)
            if pending:
                if cmd.get("id") != pending.get("command_id") and now - pending["started_at"] > 10:
                    return finish("handoff", "Befehl noch nicht bestaetigt; keine blinde Wiederholung.", True,
                                  action=pending["action"])
                if now - pending["started_at"] > 180:
                    # Do not report a timeout as a successful action or keep
                    # waiting forever. The LLM may inspect and stop/replan it.
                    return finish("handoff", "Eigene Aktion seit drei Minuten ohne Endergebnis; Lage pruefen und neu planen.", True,
                                  action=pending["action"])
                # Safety can interrupt only our own routine, never player commands.
                urgent = decision.get("action") and decision.get("priority", 99) <= 2
                if not urgent or decision.get("priority", 99) >= pending.get("priority", 99):
                    return finish("running", "Eigene Aktion laeuft; auf Ergebnis warten.", action=pending["action"])
                memory["episodes"].append({"at": now, "action": pending["action"],
                                           "status": "safety_interrupted", "verified": None})
            if not decision.get("action"):
                return finish("handoff" if decision.get("needs_llm") else "idle",
                              decision["reason"], decision.get("needs_llm", False))
            if getattr(bridge, "cmd_file", None) and os.path.exists(bridge.cmd_file):
                return finish("busy", "Mailbox ist belegt.")
            command_id = bridge.send(decision["action"], timeout=.2, **decision["params"])
            if decision["goal"] == "return":
                memory["search_returning"] = True
            memory["pending"] = {**decision, "command_id": command_id, "started_at": now,
                                 "before": _snapshot(state), "key": _key(decision, state)}
            return finish("started", decision["reason"], action=decision["action"], command_id=command_id,
                          goal=decision["goal"])
    except (OSError, TimeoutError, ValueError) as exc:
        return {"status": "error", "needs_llm": False, "reason": f"Survival-Tick: {exc}"}


def memory_summary(path):
    memory = load_memory(path)
    return {"attempts": sum(s.get("attempts", 0) for s in memory["stats"].values()),
            "verified_outcomes": sum(s.get("verified", 0) for s in memory["stats"].values()),
            "recent_outcomes": memory["episodes"][-8:], "places": memory["places"],
            "pending": {k: v for k, v in (memory.get("pending") or {}).items()
                        if k not in ("before",)}, "capabilities": CAPABILITIES}
