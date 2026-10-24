#!/usr/bin/env python3
"""IsuSurvivor Taktik-Schicht — Loot-Urteilsvermögen und Ausrüstungs-Logik.

Komponiert die dummen Mod-Primitive (pickup, equip) zu schlauem Verhalten.
Wird vom MCP-Server genutzt; für Tests auch direkt aufrufbar:

  python tactics.py loot           # Umgebung looten
  python tactics.py equip          # beste Waffe in die Hand
  python tactics.py score          # Bewertung der sichtbaren Items anzeigen
"""

import json
import math
import os
import random
import re
import sys
import time

from bridge import Bridge, DEFAULT_PROFILE, interrupt_reason, interrupt_text

LEARNED_FILE = os.path.join(
    os.path.dirname(os.path.dirname(os.path.abspath(__file__))),
    "agent_home", "learned_recipes.json")

# Eigene Ablagen (drop): Classname + Ort + Zeit, pro Agent im agent_home.
# Damit sammeln loot_area/explore_step/survival NICHT wieder ein, was der NPC
# gerade bewusst weggeworfen hat (Viktor 08.09.: Mag, AmmoBox und Wellies
# abgelegt, zwei Minuten später vom explore_step wieder aufgehoben - ein
# Ballast-Kreisverkehr). dayz_mcp UND survival/run_agent lesen dieselbe
# Datei; Einträge verfallen nach DROP_IGNORE_SECONDS, ein gezieltes
# pickup(classname) des Gehirns hebt die Sperre für diesen Classname auf.
DROP_LEDGER = os.path.join(os.path.dirname(LEARNED_FILE), "dropped_items.json")
DROP_IGNORE_SECONDS = 3 * 3600
DROP_RADIUS_M = 40.0


def load_drops(path: str = "", now: float | None = None) -> list[dict]:
    """Gültige (nicht verfallene) eigene Ablagen lesen; bei Fehlern leer."""
    path = path or DROP_LEDGER
    now = time.time() if now is None else now
    try:
        with open(path, "r", encoding="utf-8") as f:
            data = json.load(f)
    except (OSError, ValueError):
        return []
    if not isinstance(data, list):
        return []
    drops = []
    for e in data:
        if not isinstance(e, dict) or not e.get("classname"):
            continue
        try:
            if now - float(e.get("t") or 0.0) > DROP_IGNORE_SECONDS:
                continue
            drops.append({"classname": str(e["classname"]), "x": float(e.get("x", 0.0)),
                          "z": float(e.get("z", 0.0)), "t": float(e.get("t") or 0.0)})
        except (TypeError, ValueError):
            continue
    return drops


def _save_drops(path: str, drops: list[dict]) -> None:
    drops = drops[-64:]
    tmp = f"{path}.{os.getpid()}.tmp"
    try:
        os.makedirs(os.path.dirname(path) or ".", exist_ok=True)
        with open(tmp, "w", encoding="utf-8") as f:
            json.dump(drops, f, ensure_ascii=False)
        os.replace(tmp, path)
    except OSError:
        pass


def remember_drop(classname: str, x: float, z: float, path: str = "",
                  now: float | None = None) -> None:
    """Bewusste Ablage merken (Ort = NPC-Position beim Drop)."""
    if not classname:
        return
    path = path or DROP_LEDGER
    now = time.time() if now is None else now
    drops = load_drops(path, now)
    drops.append({"classname": str(classname), "x": float(x or 0.0),
                  "z": float(z or 0.0), "t": now})
    _save_drops(path, drops)


def forget_drop(classname: str, path: str = "", now: float | None = None) -> int:
    """Sperre für einen Classname aufheben (das Gehirn will ihn wieder).
    Liefert die Zahl der entfernten Einträge."""
    path = path or DROP_LEDGER
    drops = load_drops(path, now)
    kept = [d for d in drops if d["classname"] != classname]
    if len(kept) != len(drops):
        _save_drops(path, kept)
    return len(drops) - len(kept)


def is_own_drop(entry: dict, drops: list[dict], npc: dict | None = None) -> bool:
    """Ist dieses Bodenitem eine eigene Ablage? Gleicher Classname und
    höchstens DROP_RADIUS_M vom Ablageort entfernt. Ohne Weltposition im
    Eintrag zählt NPC-Abstand zum Ablageort minus gemeldeter Item-Distanz."""
    cn = entry.get("classname", "")
    if not cn or not drops:
        return False
    ex, ez = entry.get("x"), entry.get("z")
    for d in drops:
        if d["classname"] != cn:
            continue
        if ex is None or ez is None:
            if npc is None:
                return True
            npc_dist = math.hypot(d["x"] - float(npc.get("pos_x", 0.0) or 0.0),
                                  d["z"] - float(npc.get("pos_z", 0.0) or 0.0))
            if npc_dist - float(entry.get("distance", 0.0) or 0.0) <= DROP_RADIUS_M:
                return True
            continue
        if math.hypot(float(ex) - d["x"], float(ez) - d["z"]) <= DROP_RADIUS_M:
            return True
    return False

# Crafting-Rezepte: Materialien sind ENTITIES im Inventar (keine Stack-Mengen).
# Die Rezeptlogik lebt hier; die Mod kennt nur consume_item/give_item/spawn_item.
RECIPES = {
    "fireplace": {"mats": {"WoodenStick": 2, "Rag": 1}, "result": "Fireplace",
                  "place": True, "desc": "Lagerfeuer (wird vor dir platziert)"},
    "stone_knife": {"mats": {"SmallStone": 2}, "result": "StoneKnife",
                    "place": False, "desc": "Steinmesser"},
    "splint": {"mats": {"WoodenStick": 2, "Rag": 1}, "result": "Splint",
               "place": False, "desc": "Schiene (gegen gebrochene Beine)"},
    "torch": {"mats": {"WoodenStick": 1, "Rag": 1}, "result": "Torch",
              "place": False, "desc": "Fackel (Licht)"},
    "fishing_rod": {"mats": {"LongWoodenStick": 1, "Rope": 1}, "result": "FishingRod",
                    "place": False, "desc": "Angelrute"},
    "hand_drill": {"mats": {"Bark_Oak": 1, "WoodenStick": 1}, "result": "HandDrillKit",
                   "place": False, "desc": "Feuerbohrer (Zuendmittel ohne Streichhoelzer)"},
}

# Waffen-Ranking: (Muster im Classname, Punktzahl). Grob, aber wirksam.
WEAPON_TIERS = [
    ("M4A1", 90), ("AKM", 88), ("AK101", 86), ("AK74", 84), ("FAL", 92),
    ("SCARH", 91),   # SCR17 aus 1.29 "Road to Badlands" (7.62x51, 20-Schuss)
    ("SVD", 95), ("VSS", 85), ("ASVAL", 85), ("Aug", 82), ("FAMAS", 82),
    ("Pioneer", 78), ("SKS", 72),   # häufige Vanilla-Halbautomaten (fehlten:
    ("Repeater", 66),               # Score 0 = wurden nie gelootet/equippt)
    ("Mosin", 70), ("Winchester", 68), ("Blaze", 66), ("CZ527", 64),
    ("CZ550", 72), ("SSG82", 65), ("Vaiga", 60), ("Saiga", 60),
    ("BK43", 45), ("BK18", 35), ("Mp133", 50), ("UMP", 58), ("MP5", 56),
    # Izh18-Familie fehlte (Score 0): loot_area meldete "Nichts Lohnendes",
    # obwohl die Flinte 37 m entfernt lag (Viktor 08.09.). Shotgun VOR Izh18,
    # der erste Treffer zählt (Substring-Match).
    ("Izh18Shotgun", 40), ("Izh18", 30),
    # Izh43 (Doppelflinte) und Ruger1022 fehlten (Score 0): loot_area wertete
    # sie nie als Upgrade, Python- und Mod-Ranking widersprachen sich (MOD-5,
    # 24.10.). Die P1 steht NICHT hier: der Teilstring "p1" träfe Mp133 und
    # PP19 - classify_weapon prüft sie exakt per Classname. Bekommt die Mod
    # eine eigene Rangliste für CmdEquipBest (MOD-5-Vorschlag IsuWeaponScore),
    # beide Tabellen synchron halten.
    ("Izh43", 42), ("Ruger1022", 25),
    ("Bizon", 54), ("CZ61", 48), ("PP19", 54),
    ("FNX", 30), ("Glock", 28), ("Mkii", 22), ("IJ70", 20), ("Makarov", 20),
    ("CZ75", 26), ("Deagle", 34), ("Magnum", 32), ("Longhorn", 24),
    ("Derringer", 10), ("Sporter", 18),
]

# Waffenfamilie -> Magazin-Namensbestandteile, wenn der Waffenname NICHT im
# Magazinnamen steckt (M4A1 lädt STANAG/CMAG usw.). Für alle anderen greift
# der Namens-Match (Mag_AKM_* zu AKM).
MAG_ALIASES = {
    "m4a1": ("stanag", "cmag"),
    "asval": ("val",),
    "vaiga": ("saiga",),
    "pp19": ("pp19", "bizon"),
    "aks74u": ("ak74", "aks74u"),   # AKS74U lädt die AK74-Magazine
}

MELEE_PATTERNS = [
    ("FirefighterAxe", 40), ("WoodAxe", 36), ("Sword", 38), ("Machete", 34),
    ("Hatchet", 30), ("Cleaver", 24), ("HuntingKnife", 22), ("CombatKnife", 26),
    ("KitchenKnife", 16), ("SteakKnife", 12), ("BaseballBat", 20),
    ("NailedBaseballBat", 28), ("Crowbar", 22), ("PipeWrench", 18),
    ("Shovel", 20), ("FieldShovel", 22), ("Pickaxe", 20), ("SledgeHammer", 26),
    ("Kukri", 30),   # fehlte: Viktor hielt 24.10. ein Kukri, Score 0 (MOD-5)
]

# Fahrzeug-Sperre (C8/DR-3): Im Fahrzeug greift eAI_TakeItemToHands nicht -
# equip lief 16 Retry-Ticks bis "Hand blockiert?", engage meldete "nichts in
# der Hand" (Birgit 24.10. auf dem Motorrad des Spielers, 35 s verbrannt, dann tot).
# Die MCP-Tools geben im Fahrzeug sofort diesen Text zurück, ohne Mod-Befehl.
VEHICLE_BLOCK_TEXT = (
    "ABGEBROCHEN: Du sitzt als MITFAHRER in einem Fahrzeug - Waffe ziehen, "
    "kämpfen, laufen und looten geht dort nicht. Bleib sitzen, der Fahrer "
    "bringt dich weg, oder steig mit vehicle_exit aus, wenn das Fahrzeug steht.")

MEDICAL_PATTERNS = ["Bandage", "Rag", "Morphine", "Epinephrine", "SalineBag",
                    "BloodBag", "TetracyclineAntibiotics", "CharcoalTablets",
                    "PainkillerTablets", "VitaminBottle", "DisinfectantSpray",
                    "DisinfectantAlcohol", "Splint", "FirstAidKit", "ChelatingTablets"]

USEFUL_PATTERNS = ["Matchbox", "Lighter", "Canteen", "WaterBottle", "CanOpener",
                   "Flashlight", "Battery9V", "Compass", "Map", "Rope",
                   "FishingRod", "CookingPot", "FryingPan", "Sharpening"]

# Fleisch/Fisch, das roh aus hunt/harvest/fish kommt und vor dem Essen ans
# Feuer muss. Classname unterscheidet roh/gegart NICHT (FoodStage) - die Mod
# meldet beim cook selbst "nichts Rohes", wenn schon alles gegart ist.
RAW_FOOD_PATTERNS = ("SteakMeat", "LegMeat", "BreastMeat", "Fillet",
                     "Carp", "Mackerel", "Lard")

FOOD_PATTERNS = ["Apple", "Pear", "Plum", "Tomato", "Potato", "Zucchini",
                 "GreenBellPepper", "Banana", "Orange", "Kiwi",
                 "SpaghettiCan", "BakedBeansCan", "SardinesCan", "TunaCan",
                 "PeachesCan", "UnknownFoodCan", "DogFoodCan", "CatFoodCan",
                 "PorkCan", "Lunchmeat", "BrisketSpread", "PowderedMilk",
                 "Cereal", "Rice", "Crackers", "Chips", "Chocolate", "Honey",
                 "Marmalade", "Zagorky", "SodaCan", "Pipsi", "Cola", "Spite",
                 "Kvass", "Fronta"]


# Anbauteile tragen den Waffennamen als Substring (SSG82Optic, AK74_WoodBttstck)
# - das sind KEINE Waffen. Ohne diesen Filter heben sich die Bots gegenseitig
# ihre weggeworfenen Optiken/Schäfte auf und tanzen umeinander.
# "Light"/"Rail" bewusst NICHT: fängt sonst Chemlight/Flashlight (sollen
# aufgehoben werden). Waffenlampe gezielt über "TLRLight".
ATTACHMENT_MARKERS = ("Optic", "Bttstck", "Hndgrd", "Buttstock", "Handguard",
                      "Suppressor", "Compensator", "Bayonet", "TLRLight",
                      "Mag_", "Ammo")


def classify_weapon(classname: str) -> int:
    if classname.startswith("Mag_") or classname.startswith("Ammo"):
        return 0
    if any(m in classname for m in ATTACHMENT_MARKERS):
        return 0
    # P1 exakt (MOD-5): als Teilstring-Muster träfe "p1" auch Mp133/PP19.
    if classname.lower() == "p1":
        return 24
    for pattern, score in WEAPON_TIERS:
        if pattern.lower() in classname.lower():
            return score
    return 0


def classify_melee(classname: str) -> int:
    for pattern, score in MELEE_PATTERNS:
        if pattern.lower() in classname.lower():
            return score
    return 0


# Reine Bau-/Grab-/Farm-Werkzeuge: taugen als Notnagel-Melee, sollen aber NICHT
# als "beste Waffe" gelten (sonst stand der NPC mit einer Pickaxe am Lager rum
# und equip_best meldete fälschlich "schon bewaffnet", statt sich ein Gewehr/
# Messer zu holen). Aus der Waffen-/Melee-WAHL ausschließen; Looten/Bauen
# bleibt davon unberührt (classify_melee ändert sich nicht).
TOOL_PATTERNS = ("Pickaxe", "Shovel", "FieldShovel", "Hoe")


def is_tool(classname: str) -> bool:
    return any(t.lower() in classname.lower() for t in TOOL_PATTERNS)


def mag_fits_weapon(mag_cn: str, weapon_cn: str) -> bool:
    """Passt das Magazin zur Waffe? Gleiche Namenslogik wie pick_best_weapon
    (Waffenfamilie im Magazinnamen, M4/STANAG usw. über MAG_ALIASES), dazu
    der umgekehrte Fall: Magazin-Kürzel als Anfang des Waffennamens
    (Mag_Glock_15Rnd zu Glock19, Mag_MP5_30Rnd zu MP5K) bzw. als längeres
    Teilstück darin (Mag_IJ70_8Rnd zu MakarovIJ70)."""
    mag_l = (mag_cn or "").lower()
    family = (weapon_cn or "").split("_")[0].lower()
    if not mag_l.startswith("mag_") or not family:
        return False
    fams = MAG_ALIASES.get(family, (family,))
    if any(f in mag_l for f in fams):
        return True
    parts = mag_l.split("_")
    tok = parts[1] if len(parts) > 1 else ""
    if not tok:
        return False
    if family.startswith(tok):
        return True
    # Kurze Kürzel ("p1") nur am Wortanfang - sonst passte Mag_P1 zur Mp133.
    return len(tok) >= 4 and tok in family


def score_ground_item(entry: dict, inventory: list[dict],
                      human_near: bool = False) -> int:
    """Wie lohnend ist ein Bodenitem? 0 = liegen lassen.

    human_near: ein menschlicher Spieler steht <= 10 m vom NPC (vom Aufrufer
    aus dem Snapshot bestimmt). Nur dann gilt Kleidung direkt vor den Füßen
    als Übergabe. Optional, damit survival.py und der score-Testmodus
    unverändert weiterlaufen."""
    cn = entry.get("classname", "")
    kind_guess = ""

    # Liegt das Item bei einem ANDEREN Bot (>5 m entfernt), ist es wohl dessen
    # frische Ablage (die Mod markiert das in 'near') - nicht drum kämpfen.
    # Direkt vor den eigenen Füßen (<=5 m) aber NICHT unterdrücken: das ist
    # meist eine Spieler-Übergabe am Lager, die sonst fälschlich als "fremde
    # Ablage" gewertet und nie aufgehoben würde.
    if entry.get("near") and entry.get("distance", 99) > 5.0:
        return 0
    # Anbauteile, die die Mod als solche erkannt hat, gar nicht erst werten
    if entry.get("item_kind") == "attachment":
        return 0
    # Kleidung am Boden (Mod-Klassifikation item_kind="clothing"): NUR noch
    # als Spieler-Übergabe einsammeln - ein Mensch steht <= 10 m nah und das
    # Teil liegt <= 5 m vor den Füßen ("Spieler legt Klamotten hin, NPC nimmt
    # sie nicht"). Vorher galt jede Kleidung bis 20 m pauschal 30: Viktor
    # hortete 24.10. 49-51 Teile, scheiterte 10x an "Inventar voll" und warf
    # dafür Magazine und seine Izh43 weg (TOOL-2). Echte Upgrades erledigt
    # dress_best (prüft Bodenkleidung in 10 m slotgenau).
    if entry.get("item_kind") == "clothing":
        if human_near and entry.get("distance", 99) <= 5.0:
            return 30
        return 0

    weapon = classify_weapon(cn)
    if weapon:
        best_owned = max([classify_weapon(i.get("classname", "")) for i in inventory] or [0])
        if weapon > best_owned:
            return 100 + weapon  # Upgrade: höchste Priorität
        return 0  # schlechtere Waffe: liegen lassen

    if cn.startswith("Mag_"):
        # Magazine nur hoch werten, wenn eine passende Waffe dabei ist (Viktor
        # sammelte zwei Mag_Glock ohne Glock: "Munition für Waffen, die ich
        # nicht habe"). Lose Munition bleibt 80 - die Kaliberzuordnung kennt
        # nur die Mod.
        guns = [i.get("classname", "") for i in inventory
                if classify_weapon(i.get("classname", ""))]
        if any(mag_fits_weapon(cn, g) for g in guns):
            return 80
        return 10
    if cn.startswith("Ammo"):
        return 80

    melee = classify_melee(cn)
    if melee:
        best_melee = max([classify_melee(i.get("classname", "")) for i in inventory] or [0])
        has_firearm = any(classify_weapon(i.get("classname", "")) for i in inventory)
        if melee > best_melee and not has_firearm:
            return 60 + melee
        if melee > best_melee:
            return 20 + melee
        return 0

    for pattern in MEDICAL_PATTERNS:
        if pattern.lower() in cn.lower():
            return 70

    for pattern in FOOD_PATTERNS:
        if pattern.lower() in cn.lower():
            return 65

    for pattern in USEFUL_PATTERNS:
        if pattern.lower() in cn.lower():
            return 40

    return 0


def loot_area(bridge: Bridge, max_items: int = 6, time_budget: float = 180.0,
              log=print) -> dict:
    """Sichtbare lohnende Bodenitems einsammeln (nächstes/bestes zuerst).

    Bricht ab bei Gefahr in der Nähe (Infizierter/Raubtier < 40 m) und bei
    Spieler-Funk (interruptible) - vorher sammelte der NPC bis zu 240 s stur
    weiter und war für den Spieler taub."""
    deadline = time.monotonic() + time_budget
    haul: list[str] = []
    failed: set[str] = set()
    aborted = ""
    drops = load_drops()          # eigene Ablagen liegen lassen (kein Kreisverkehr)
    ignored: set[str] = set()

    while len(haul) < max_items and time.monotonic() < deadline:
        state = bridge.read_state() or {}
        inventory = state.get("inventory", [])
        npc = state.get("npc", {})
        if not npc.get("alive"):
            break

        danger = ""
        for e in state.get("nearby", []):
            # Passive deer/chickens are food opportunities, not enemies.
            predator = e.get("kind") == "animal" and any(
                name in e.get("classname", "").lower()
                for name in ("wolf", "bear", "canislupus", "ursus"))
            if (e.get("kind") == "infected" or predator) and e.get("distance", 99) < 40.0:
                danger = e.get("classname") or e.get("kind")
                break
        if danger:
            aborted = f"Gefahr in der Naehe ({danger}) - Looten abgebrochen."
            log("  " + aborted)
            break

        # Mensch <= 10 m: nur dann zählt Kleidung vor den Füßen als Übergabe.
        human_near = any(e.get("kind") == "player" and e.get("distance", 99) <= 10.0
                         for e in state.get("nearby", []))
        candidates = []
        for e in state.get("nearby", []):
            if e.get("kind") != "item":
                continue
            cn = e.get("classname", "")
            if cn in failed:
                continue
            if is_own_drop(e, drops, npc):
                ignored.add(cn)
                continue
            score = score_ground_item(e, inventory, human_near=human_near)
            if score > 0:
                candidates.append((score, e.get("distance", 99), cn))

        if not candidates:
            break

        candidates.sort(key=lambda c: (-c[0], c[1]))
        score, dist, target = candidates[0]
        log(f"  loot: {target} (score {score}, {dist:.0f} m)")

        remaining = max(20.0, deadline - time.monotonic())
        result = bridge.run("pickup", text=target, timeout=min(150.0, remaining),
                            interruptible=True)
        if result.get("status") == "done":
            haul.append(target)
            # Aufnahme-Animation + 1-Hz-State abwarten, sonst sieht die
            # nächste Iteration das Item noch am Boden (Doppelzählung)
            time.sleep(3.0)
        elif result.get("status") == "interrupted":
            aborted = "Looten sofort abgebrochen: " + interrupt_reason(result)
            break
        else:
            failed.add(target)
            log(f"  loot fehlgeschlagen: {target}: {result.get('detail')}")

    return {"haul": haul, "failed": sorted(failed), "aborted": aborted,
            "ignored": sorted(ignored)}


def equip_by_classname(bridge: Bridge, classname: str,
                       action: str = "equip") -> dict:
    """Mod-equip mit Klon-Nachprüfung. Expansions eAI_TakeItemToHandsImpl
    KLONT die Waffe in die Hand und löscht das Original; ältere Mod-Builds
    verlieren damit ihren Zeiger und melden "Equip-Ziel verschwunden",
    obwohl die Waffe schon in der Hand liegt (Igor/Konrad 08.09.). Darum bei
    dieser Meldung kurz warten, den Handinhalt prüfen und erst dann einmal
    neu versuchen. Liefert das (ggf. korrigierte) Bridge-Ergebnis.
    action="equip_melee_lock" (engage mode="melee") setzt im Mod zusätzlich
    sofort den Nahkampf-Lock; ein älterer Mod-Build kennt die Aktion nicht,
    dann einmal mit dem normalen "equip" weiter."""
    result = bridge.run(action, text=classname, timeout=30)
    if (action != "equip" and result.get("status") != "done"
            and "unbekannte action" in str(result.get("detail") or "")):
        action = "equip"
        result = bridge.run(action, text=classname, timeout=30)
    if result.get("status") == "done":
        return result
    if "verschwunden" not in str(result.get("detail") or "").lower():
        return result
    time.sleep(2.0)
    hands = (bridge.read_state() or {}).get("npc", {}).get("in_hands", "")
    if hands == classname:
        return {"id": result.get("id"), "status": "done", "detail": classname}
    return bridge.run(action, text=classname, timeout=30)


def pick_best_weapon(inventory: list[dict]) -> str:
    """Beste Waffe im Inventar, Munitions-Heuristik inklusive."""
    # Ruinierte Waffen ("Geisterwaffen") blockieren eAI_TakeItemToHands und
    # fallen komplett raus
    usable = [i for i in inventory if i.get("health", 100.0) > 5.0]
    mags = [i.get("classname", "") for i in usable
            if i.get("classname", "").startswith("Mag_")]

    best_cn, best_score = "", 0
    best_loaded = False
    for item in usable:
        cn = item.get("classname", "")
        score = classify_weapon(cn)
        if not score:
            continue
        # Geladen (quantity = echte Munition aus der Mod) oder Magazin der
        # gleichen Waffenfamilie im Gepäck? (Mag_AKM_* zu AKM; M4/STANAG
        # u.ä. über MAG_ALIASES, sonst galt die M4 immer als leer)
        family = cn.split("_")[0].lower()
        fams = MAG_ALIASES.get(family, (family,))
        has_mag = any(f in m.lower() for f in fams for m in mags)
        loaded = item.get("quantity", 0) > 0 or has_mag
        if loaded:
            score += 50
        if score > best_score:
            best_cn, best_score = cn, score
            best_loaded = loaded

    # Feuerwaffe nur bevorzugen, wenn sie WIRKLICH schussbereit ist. Die alte
    # Schwelle "best_score >= 50" war wirkungslos: schon der Basis-Tier einer
    # LEEREN AKM (88) riss sie - der NPC zog das leere Gewehr statt der
    # Machete, exakt der Fall, den der Kommentar unten als gefixt beschreibt.
    if best_cn and best_loaded:
        return best_cn

    melee_cn, melee_score = "", 0
    for item in usable:
        cn = item.get("classname", "")
        if is_tool(cn):
            continue   # Pickaxe/Shovel etc. sind keine Waffe
        score = classify_melee(cn)
        if score > melee_score:
            melee_cn, melee_score = cn, score

    # Echte Nahkampfwaffe schlägt leere Feuerwaffe: mit einer AKM ohne
    # Magazin rumstehen statt zuzuschlagen hat im Battle Royale Leben gekostet
    if melee_cn:
        return melee_cn
    return best_cn  # leere Waffe schlägt blanke Fäuste


def equip_best(bridge: Bridge, log=print) -> str:
    state = bridge.read_state() or {}
    inventory = state.get("inventory", [])
    in_hands = state.get("npc", {}).get("in_hands", "")

    best = pick_best_weapon(inventory)
    if not best:
        return "Keine Waffe im Inventar."
    if best == in_hands:
        return f"Beste Waffe ist schon in der Hand: {best}"

    result = equip_by_classname(bridge, best)
    if result.get("status") == "done":
        return f"Ausgerüstet: {best} (vorher: {in_hands or 'leer'})"
    return f"Ausrüsten fehlgeschlagen: {result.get('detail')}"


def pick_best_melee(inventory: list[dict]) -> str:
    """Beste NAHKAMPFWAFFE im Inventar (Machete, Axt, Messer ...), UNABHÄNGIG
    davon, ob eine geladene Feuerwaffe da ist. Für den Solo-Modus gegen
    Infizierte: leise zuschlagen statt schießen (spart Muni, lockt keine Horde)."""
    usable = [i for i in inventory if i.get("health", 100.0) > 5.0]
    melee_cn, melee_score = "", 0
    for item in usable:
        cn = item.get("classname", "")
        if is_tool(cn):
            continue   # Pickaxe/Shovel etc. sind keine Kampfwaffe
        score = classify_melee(cn)
        if score > melee_score:
            melee_cn, melee_score = cn, score
    return melee_cn


def equip_melee(bridge: Bridge, log=print, lock: bool = False) -> str:
    """Gezielt die beste Nahkampfwaffe ziehen, auch wenn eine geladene
    Feuerwaffe vorhanden wäre - das Gegenstück zu equip_best für den
    leisen Solo-Kampf gegen einzelne Infizierte. Nutzt auch dayz_mcp.engage
    (mode="melee") - dessen Prüfung erwartet die Präfixe "Nahkampfwaffe
    gezogen" bzw. "Nahkampfwaffe ist schon" als Erfolg.
    lock=True (nur engage mode="melee"): Mod-Aktion equip_melee_lock setzt
    den Nahkampf-Lock schon beim Ziehen. Ohne ihn griff Expansion in den 1-2 s
    bis zum engage-Befehl (noch kein Ziel) wieder zur Schusswaffe - auch wenn
    das Messer schon in der Hand liegt, darum dann trotzdem einmal senden."""
    state = bridge.read_state() or {}
    inventory = state.get("inventory", [])
    npc = state.get("npc", {})
    in_hands = npc.get("in_hands", "")
    if npc.get("in_vehicle"):
        return VEHICLE_BLOCK_TEXT   # C8: im Fahrzeug kein Mod-equip

    melee = pick_best_melee(inventory)
    if not melee:
        return ("Keine Nahkampfwaffe im Inventar - notfalls equip_best "
                "(Schusswaffe) oder flee.")
    action = "equip_melee_lock" if lock else "equip"
    if melee == in_hands:
        if lock:
            equip_by_classname(bridge, melee, action=action)
        return f"Nahkampfwaffe ist schon in der Hand: {melee}"

    result = equip_by_classname(bridge, melee, action=action)
    if result.get("status") == "done":
        return f"Nahkampfwaffe gezogen: {melee} (vorher: {in_hands or 'leer'})"
    return f"Ausrüsten fehlgeschlagen: {result.get('detail')}"


# Materialien, deren quantity = Stückzahl ist (Piles)
PILE_MATS = {"WoodenStick", "LongWoodenStick", "Rag", "SmallStone", "Stone",
             "Paper", "Bark_Oak", "Bark_Birch", "Nail"}


def load_learned_recipes() -> dict:
    try:
        with open(LEARNED_FILE, "r", encoding="utf-8") as f:
            return json.load(f)
    except (FileNotFoundError, json.JSONDecodeError):
        return {}


def all_recipes() -> dict:
    merged = dict(RECIPES)
    merged.update(load_learned_recipes())
    return merged


def learn_recipe(name: str, materials: str, result: str,
                 place: bool = False, desc: str = "") -> str:
    """Neues Rezept dauerhaft lernen. materials z.B. '2x WoodenPlank + 4x Nail'."""
    name = re.sub(r"[^a-z0-9_]", "_", name.strip().lower())
    if not name or not result.strip():
        return "Brauche name und result (Classname des Ergebnisses)."

    mats: dict[str, int] = {}
    for count, classname in re.findall(r"(\d+)\s*[xX]?\s*([A-Za-z_]\w*)", materials):
        mats[classname] = mats.get(classname, 0) + int(count)
    if not mats:
        return ("Material nicht verstanden. Format: '2x WoodenPlank + 4x Nail' "
                "(Classnames!).")

    learned = load_learned_recipes()
    learned[name] = {
        "mats": mats,
        "result": result.strip(),
        "place": bool(place),
        "desc": desc.strip() or "selbst gelernt",
        "learned": True,
    }
    os.makedirs(os.path.dirname(LEARNED_FILE), exist_ok=True)
    with open(LEARNED_FILE, "w", encoding="utf-8") as f:
        json.dump(learned, f, indent=2, ensure_ascii=False)

    mats_text = ", ".join(f"{n}x {m}" for m, n in mats.items())
    return f"Rezept gelernt: {name} = {mats_text} -> {result} (bleibt dauerhaft)"


def _inventory_counts(state: dict) -> dict[str, int]:
    counts: dict[str, int] = {}
    for item in state.get("inventory", []):
        cn = item.get("classname", "")
        if cn in PILE_MATS:
            amount = max(1, int(round(item.get("quantity", 1))))
        else:
            amount = 1
        counts[cn] = counts.get(cn, 0) + amount
    return counts


def craft(bridge: Bridge, recipe_name: str, log=print) -> str:
    recipes_map = all_recipes()
    recipe = recipes_map.get(recipe_name)
    if not recipe:
        return ("Unbekanntes Rezept '" + recipe_name + "'. Verfügbar: "
                + ", ".join(sorted(recipes_map)))

    state = bridge.read_state() or {}
    counts = _inventory_counts(state)
    missing = []
    for mat, needed in recipe["mats"].items():
        if counts.get(mat, 0) < needed:
            missing.append(f"{needed - counts.get(mat, 0)}x {mat}")
    if missing:
        return "Fehlt für " + recipe_name + ": " + ", ".join(missing)

    for mat, needed in recipe["mats"].items():
        result = bridge.run("consume_item", text=mat, y=needed, timeout=20)
        if result.get("status") != "done":
            return f"Materialverbrauch fehlgeschlagen: {result.get('detail')}"

    if recipe.get("place"):
        result = bridge.run("spawn_item", text=recipe["result"], timeout=20)
    else:
        result = bridge.run("give_item", text=recipe["result"], timeout=20)

    if result.get("status") != "done":
        return f"Herstellung fehlgeschlagen: {result.get('detail')}"
    return f"Hergestellt: {recipe['result']} ({recipe['desc']})"


def _retreat_from_fire(bridge: Bridge, dist: float = 6.0) -> None:
    """Nach dem Garen vom Feuer wegtreten. Das Feuer macht Verbrennungsschaden
    und die eAI bleibt sonst drin stehen (2 NPCs starben so am 2026-06-16).
    Bewegt sich entlang des Vektors Feuer->NPC auf 'dist' m vom Feuer weg - also
    garantiert WEG vom Feuer, ohne riskante Blickrichtungs-Annahme. Best effort;
    jeder Fehler wird verschluckt."""
    try:
        state = bridge.read_state() or {}
        npc = state.get("npc", {})
        px = npc.get("pos_x")
        pz = npc.get("pos_z")
        if px is None or pz is None:
            return
        fire = None
        for e in state.get("nearby", []):
            if e.get("kind") in ("fire_burning", "fire"):
                fire = e
                break
        if not fire:
            return
        fx = fire.get("x")
        fz = fire.get("z")
        if fx is None or fz is None:
            return
        dx = px - fx
        dz = pz - fz
        d = math.hypot(dx, dz)
        if d < 0.1:           # steht direkt im Feuer -> beliebige Richtung raus
            dx, dz, d = 1.0, 0.0, 1.0
        rx = fx + (dx / d) * dist
        rz = fz + (dz / d) * dist
        bridge.run("move_to", x=rx, z=rz, timeout=25)
    except Exception:
        pass


def cook_meal(bridge: Bridge, log=print) -> str:
    """Komplette Koch-Kette: Feuer finden/bauen/anzünden, dann garen."""
    steps: list[str] = []
    state = bridge.read_state() or {}
    nearby = state.get("nearby", [])

    has_burning = any(e.get("kind") == "fire_burning" for e in nearby)
    has_cold = any(e.get("kind") == "fire" for e in nearby)

    if not has_burning:
        if not has_cold:
            # Feuerstelle bauen (+3 Sticks Brennholz ehrlich verbrauchen)
            counts = _inventory_counts(state)
            if counts.get("WoodenStick", 0) < 5 or counts.get("Rag", 0) < 1:
                return ("Kein Feuer in der Naehe und zu wenig Material: brauche "
                        "5x WoodenStick + 1x Rag + Zuendmittel.")
            result = craft(bridge, "fireplace", log)
            steps.append(result)
            if not result.startswith("Hergestellt"):
                return "\n".join(steps)
            bridge.run("consume_item", text="WoodenStick", y=3, timeout=20)
            steps.append("3x WoodenStick als Brennholz verbraucht.")
            time.sleep(2.0)

        result = bridge.run("light_fire", timeout=20)
        if result.get("status") != "done":
            steps.append(f"Anzuenden fehlgeschlagen: {result.get('detail')}")
            return "\n".join(steps)
        steps.append("Feuer brennt.")
        time.sleep(2.0)

    result = bridge.run("cook", timeout=30)
    if result.get("status") == "done":
        steps.append("Gegart: " + (result.get("detail") or ""))
        # Vom Feuer zurücktreten - es verbrennt, und die eAI bleibt sonst drin
        # stehen (2 NPCs starben so am 2026-06-16).
        _retreat_from_fire(bridge)
        steps.append("Vom Feuer zurueckgetreten. WARNUNG: Feuer verbrennt - "
                     "haltet mindestens 5 m Abstand, lauft nicht hinein.")
    else:
        steps.append(f"Garen fehlgeschlagen: {result.get('detail')}")
    return "\n".join(steps)


def _has_raw_food(state: dict) -> bool:
    """Rohes Fleisch/Fisch im Inventar (Classname-Heuristik, s. RAW_FOOD_PATTERNS)."""
    for item in state.get("inventory", []):
        cn = item.get("classname", "")
        if item.get("food_stage") in (2, 3, 4, 5, 6):
            continue  # cooked/ruined food must not trigger an endless cook loop
        if any(p.lower() in cn.lower() for p in RAW_FOOD_PATTERNS):
            return True
    return False


def hunt(bridge: Bridge, animal: str = "", log=print) -> str:
    """Jagd-Folgekette: Waffe bereit machen -> Tier anpirschen und erlegen
    (Mod-Kommando hunt) -> Kadaver zerlegen (harvest). Mit schussbereiter
    Feuerwaffe fällt das Tier auf 35 m; ohne muss der NPC auf 4 m heran,
    was nur bei Huhn/Hase realistisch klappt."""
    state = bridge.read_state() or {}
    prey = [e for e in state.get("nearby", [])
            if e.get("kind") == "animal"
            and not any(p in e.get("classname", "")
                        for p in ("Wolf", "Bear", "CanisLupus", "Ursus"))]
    if not prey:
        return ("Kein lebendes Beutetier in Sicht (kind=animal in observe). "
                "Wild steht auf Wiesen und an Waldraendern - explore_step oder "
                "travel_to in offenes Gelaende, dann erneut hunt. Raubtiere "
                "(Wolf/Baer) sind Kampf, kein Jagdwild: engage.")
    prey.sort(key=lambda e: e.get("distance", 999))
    quarry = prey[0]

    steps: list[str] = []
    # Schussbereite Waffe in die Hand: quantity ist bei Waffen die ECHTE
    # Munition (Magazin + Kammer). Ohne geladene Waffe in der Hand probiert
    # equip_best es (die Mod kennt den Munitionsstand); klappt auch das nicht,
    # bleibt die 4-m-Pirsch.
    held = None
    for it in state.get("inventory", []):
        if it.get("in_hands"):
            held = it
            break
    held_loaded = (held is not None
                   and classify_weapon(held.get("classname", "")) > 0
                   and held.get("quantity", 0) > 0)
    if not held_loaded:
        has_loaded = any(classify_weapon(i.get("classname", "")) > 0
                         and i.get("quantity", 0) > 0
                         for i in state.get("inventory", []))
        if has_loaded:
            eq = bridge.run("equip_best", timeout=30)
            if eq.get("status") == "done":
                steps.append("Waffe bereit: " + (eq.get("detail") or ""))
        else:
            steps.append("Keine geladene Feuerwaffe - Pirsch auf 4 m "
                         "(nur bei Huhn/Hase realistisch).")

    result = bridge.run("hunt", text=animal, timeout=150, interruptible=True)
    status = result.get("status")
    detail = result.get("detail") or ""
    if status == "interrupted":
        return "ABGEBROCHEN bei der Jagd: " + interrupt_reason(result)
    if status != "done":
        steps.append(f"Jagd fehlgeschlagen: {detail}")
        return "\n".join(steps)
    steps.append("Erlegt: " + detail)

    # Folgekette: gleich zerlegen (braucht Schneidwerkzeug im Inventar)
    time.sleep(1.5)
    hv = bridge.run("harvest", timeout=90, interruptible=True)
    if hv.get("status") == "done":
        steps.append("Zerlegt: " + (hv.get("detail") or "")
                     + " Rohes Fleisch VOR dem Essen garen: cook_meal "
                       "(oder process_food erledigt alles).")
    else:
        steps.append(f"Zerlegen offen ({hv.get('detail') or 'Fehler'}) - "
                     f"harvest nachholen, sobald es geht.")
    return "\n".join(steps)


def process_food(bridge: Bridge, log=print) -> str:
    """Nahrungs-Verarbeitungs-Kette: alle Tierkadaver in 50 m zerlegen
    (harvest), dann alles Rohe am Feuer garen (cook_meal baut/zündet das
    Feuer selbst). Der eine Aufruf nach Jagd oder Angeln."""
    steps: list[str] = []
    for _ in range(3):
        state = bridge.read_state() or {}
        corpses = [e for e in state.get("nearby", [])
                   if e.get("kind") == "animal_corpse"
                   and e.get("distance", 99) <= 50.0]
        if not corpses:
            break
        result = bridge.run("harvest", timeout=90, interruptible=True)
        status = result.get("status")
        if status == "interrupted":
            steps.append(interrupt_text(result))
            return "\n".join(steps)
        if status != "done":
            steps.append(f"Zerlegen: {result.get('detail')}")
            break
        steps.append("Zerlegt: " + (result.get("detail") or ""))
        time.sleep(2.0)

    state = bridge.read_state() or {}
    if _has_raw_food(state):
        steps.append(cook_meal(bridge, log))
    elif not steps:
        return ("Nichts zu verarbeiten: kein Tierkadaver in 50 m und nichts "
                "Rohes im Inventar. Erst jagen (hunt) oder fischen (fish).")
    return "\n".join(steps)


# Körper-Slots, die die Kleidungswahl systematisch besetzt. Schulter/Waffe
# und Deko-Slots (Armband, Eyewear) bleiben außen vor.
CLOTHING_SLOTS = ("Body", "Legs", "Feet", "Headgear", "Gloves", "Vest", "Back")


def _clothing_score(item: dict, freezing: bool) -> float:
    """Wert eines Kleidungsstücks nach Lage: beim Frieren zählt Wärme
    (heatIsolation aus der Mod), sonst Stauraum (Cargo-Slots). Der jeweils
    andere Faktor bleibt als Tiebreaker drin. Ruinierte Stücke zählen nicht."""
    if item.get("health", 100.0) <= 5.0:
        return -1.0
    warmth = float(item.get("warmth", 0.0) or 0.0)
    cargo = float(item.get("cargo_size", 0) or 0)
    if freezing:
        return warmth * 1000.0 + cargo
    return cargo * 100.0 + warmth * 10.0


def rescue_wear_leftovers(bridge: Bridge, detail: str, old_classname: str = "") -> str:
    """Nach einem Kleidungstausch alles zurückholen, was die Mod als am Boden
    liegend meldet. Die Mod (CmdWear seit 08.09.2026) hängt ans Ergebnis
    "AM BODEN: a, b" (einzeln abgelegte Items) und/oder "ACHTUNG: X liegt
    mit N Item(s) am Boden" (altes Stück samt Inhalt). Vorher lag Viktors
    Sanitätskram im Wald, bis das Modell es zufällig bemerkte. Liefert eine
    Kurzbeschreibung des Nachgesammelten oder ""."""
    if not detail:
        return ""
    got: list[str] = []
    m = re.search(r"ACHTUNG: (\S+) liegt mit (\d+) Item", detail)
    if m:
        garment = m.group(1)
        r = bridge.run("loot_container", text=garment, timeout=25)
        if r.get("status") == "done":
            got.append(f"Inhalt von {garment} ({r.get('detail') or ''})")
        else:
            got.append(f"{garment} NICHT ausgeräumt ({r.get('detail') or ''})")
    m2 = re.search(r"AM BODEN: (.+)$", detail)
    if m2:
        for cn in [c.strip() for c in m2.group(1).split(",") if c.strip()]:
            r = bridge.run("pickup", text=cn, timeout=20)
            if r.get("status") == "done":
                got.append(cn)
            else:
                got.append(f"{cn} NICHT aufgehoben")
    return ", ".join(got)


def dress_best(bridge: Bridge, log=print) -> str:
    """Kleidungs-Folgekette: pro Körper-Slot (Body/Legs/Feet/Kopf/Handschuhe/
    Weste/Rucksack) das beste verfügbare Stück anziehen. FRIERT der NPC
    (heat_comfort unter -0.15), zählt Wärme (heatIsolation); ist die Lage
    moderat, zählt Stauraum (Cargo-Slots). Kandidaten: unangelegte Kleidung
    im Inventar UND am Boden in 10 m (wear holt sie selbst)."""
    state = bridge.read_state() or {}
    npc = state.get("npc", {})
    heat = float(npc.get("heat_comfort", 0.0) or 0.0)
    freezing = heat < -0.15

    worn: dict[str, dict] = {}
    candidates: dict[str, list[dict]] = {}
    have_stats = False
    for item in state.get("inventory", []):
        if item.get("kind") != "clothing":
            continue
        slot = item.get("slot") or ""
        if item.get("warmth") is not None or item.get("cargo_size") is not None:
            have_stats = True
        if slot not in CLOTHING_SLOTS:
            continue
        if item.get("worn"):
            worn[slot] = item
        elif not item.get("in_hands"):
            candidates.setdefault(slot, []).append(item)
    for e in state.get("nearby", []):
        if e.get("kind") != "item" or e.get("item_kind") != "clothing":
            continue
        if e.get("distance", 99) > 10.0:
            continue
        if e.get("near"):
            continue   # frische Ablage eines anderen Bots
        slot = e.get("slot") or ""
        if slot in CLOTHING_SLOTS:
            candidates.setdefault(slot, []).append(e)
            have_stats = True

    if not have_stats:
        return ("Der Server liefert (noch) keine Kleidungswerte (warmth/slot) - "
                "die Servermod ist aelter als dieses Werkzeug. Nutze wear von "
                "Hand: beim Frieren dicke Sachen (Jacke, Muetze, Handschuhe), "
                "sonst Kleidung mit viel Stauraum.")

    if freezing:
        mode = f"FRIERT (Waerme {heat:.2f}) - Waerme zaehlt"
    else:
        mode = f"Lage moderat (Waerme {heat:.2f}) - Stauraum zaehlt"

    changes: list[str] = []
    swaps = 0
    for slot in CLOTHING_SLOTS:
        cands = candidates.get(slot)
        if not cands or swaps >= 4:
            continue
        best = max(cands, key=lambda c: _clothing_score(c, freezing))
        best_score = _clothing_score(best, freezing)
        cur = worn.get(slot)
        cur_score = -1.0
        if cur is not None:
            cur_score = _clothing_score(cur, freezing)
        if best_score <= cur_score or best_score <= 0.0:
            continue
        result = bridge.run("wear", text=best.get("classname", ""), timeout=25)
        if result.get("status") == "done":
            was = "leer"
            if cur is not None:
                was = cur.get("classname", "leer")
            changes.append(f"{slot}: {best.get('classname')} (vorher {was})")
            swaps += 1
            time.sleep(1.5)
            rescued = rescue_wear_leftovers(bridge, result.get("detail") or "",
                                            was if cur is not None else "")
            if rescued:
                changes.append("nachgesammelt: " + rescued)
        else:
            changes.append(f"{slot}: {best.get('classname')} fehlgeschlagen "
                           f"({result.get('detail') or ''})")

    if not changes:
        return (f"{mode}. Du traegst bereits die beste verfuegbare Kombination - "
                f"nichts getauscht.")
    return mode + ". Getauscht/angezogen:\n- " + "\n- ".join(changes)


def combine_items(bridge: Bridge, a: str, b: str, log=print) -> str:
    """Zwei Gegenstände kombinieren: sucht das Rezept, dessen Materialliste
    genau aus den beiden Classnames besteht, und craftet es. Kennt kein
    Rezept die Kombination, werden passende Rezepte mit einem der beiden
    Materialien vorgeschlagen."""
    a = (a or "").strip()
    b = (b or "").strip()
    if not a or not b:
        return "Bitte beide Gegenstaende angeben, z.B. combine(a=\"WoodenStick\", b=\"Rag\")."
    want = {a.lower(), b.lower()}
    recipes_map = all_recipes()
    exact: list[str] = []
    for name, r in recipes_map.items():
        mats = {m.lower() for m in r["mats"].keys()}
        if mats == want:
            exact.append(name)
        elif len(mats) == 1 and a.lower() == b.lower() and a.lower() in mats:
            exact.append(name)
    if len(exact) == 1:
        return craft(bridge, exact[0], log)
    if len(exact) > 1:
        # Mehrere Rezepte aus demselben Materialpaar (Stick+Rag = Fackel,
        # Schiene ODER Feuerstelle) - Entscheidung gehört dem Gehirn.
        opts = []
        for name in sorted(exact):
            r = recipes_map[name]
            mats_text = ", ".join(f"{n}x {m}" for m, n in r["mats"].items())
            opts.append(f"{name} ({mats_text} -> {r['result']}, {r['desc']})")
        return ("Aus diesen beiden Teilen geht MEHRERES - wähle mit "
                "craft(recipe=...): " + "; ".join(opts))
    related = []
    for name, r in sorted(recipes_map.items()):
        mats_l = {m.lower() for m in r["mats"].keys()}
        if a.lower() in mats_l or b.lower() in mats_l:
            mats_text = ", ".join(f"{n}x {m}" for m, n in r["mats"].items())
            related.append(f"{name}: {mats_text} -> {r['result']}")
    if related:
        return ("Keine bekannte Kombination aus genau diesen beiden Teilen. "
                "Rezepte mit einem der Materialien:\n- " + "\n- ".join(related)
                + "\nNeue Kombination von einem Spieler gelernt? learn_recipe.")
    return ("Keine bekannte Kombination und kein Rezept mit diesen Materialien. "
            "recipes zeigt alles Bekannte; Neues lernst du mit learn_recipe.")


def water_run(bridge: Bridge, log=print) -> str:
    """Zum nächsten sichtbaren Brunnen laufen, trinken, Flasche füllen."""
    state = bridge.read_state() or {}
    npc = state.get("npc", {})
    wells = [e for e in state.get("nearby", []) if e.get("kind") == "water"]
    if not wells:
        return ("Kein Brunnen in 100 m sichtbar. In ein Dorf gehen (move_to) "
                "und erneut schauen - Brunnen erscheinen als kind=water.")

    wells.sort(key=lambda e: e.get("distance", 999))
    well = wells[0]
    steps: list[str] = []

    if well.get("distance", 0) > 3.5:
        result = bridge.run("move_to", x=well.get("x"), z=well.get("z"), timeout=90,
                            interruptible=True)
        if result.get("status") == "interrupted":
            return interrupt_text(result)
        if result.get("status") != "done":
            return f"Komme nicht zum Brunnen: {result.get('detail')}"
        steps.append("Am Brunnen.")

    result = bridge.run("drink_well", timeout=20)
    steps.append("Getrunken." if result.get("status") == "done"
                 else f"Trinken: {result.get('detail')}")

    result = bridge.run("fill_container", timeout=20)
    if result.get("status") == "done":
        steps.append(result.get("detail") or "Behaelter gefuellt.")
    else:
        steps.append(f"Abfuellen: {result.get('detail')}")
    return "\n".join(steps)


def find_item(bridge: Bridge, pattern: str) -> str:
    state = bridge.read_state() or {}
    pattern_l = pattern.lower()
    in_inv = [i.get("classname") for i in state.get("inventory", [])
              if pattern_l in i.get("classname", "").lower()]
    on_ground = [(e.get("classname"), e.get("distance", 0))
                 for e in state.get("nearby", [])
                 if e.get("kind") == "item" and pattern_l in e.get("classname", "").lower()]

    lines = []
    if in_inv:
        lines.append("Im Inventar: " + ", ".join(in_inv))
    if on_ground:
        lines.append("Am Boden: " + ", ".join(f"{c} ({d:.0f} m)" for c, d in on_ground))
    if not lines:
        lines.append(f"'{pattern}' weder im Inventar noch in Sichtweite. "
                     "Mit explore_step weitersuchen.")
    return "\n".join(lines)


def explore_step(bridge: Bridge, log=print) -> str:
    """Ein Such-Schritt: in eine zufällige Richtung ~100 m, dort looten."""
    state = bridge.read_state() or {}
    npc = state.get("npc", {})
    x, z = npc.get("pos_x", 0.0), npc.get("pos_z", 0.0)

    bearing = random.uniform(0, 2 * math.pi)
    dist = random.uniform(90, 130)
    tx = x + math.sin(bearing) * dist
    tz = z + math.cos(bearing) * dist

    result = bridge.run("move_to", x=tx, z=tz, timeout=90, interruptible=True)
    if result.get("status") == "interrupted":
        return interrupt_text(result)
    move_note = "Angekommen" if result.get("status") == "done" \
        else f"Bewegung: {result.get('detail')}"

    loot = loot_area(bridge, max_items=4, time_budget=60, log=log)
    haul = ", ".join(loot["haul"]) if loot["haul"] else "nichts Lohnendes"
    text = f"{move_note} bei x={tx:.0f} z={tz:.0f}. Eingesammelt: {haul}."
    if loot.get("ignored"):
        text += " Eigene Ablagen liegen gelassen: " + ", ".join(loot["ignored"]) + "."
    if loot.get("aborted"):
        # Früher ging der Loot-Abbruch (Gefahr/Funk/kritisches Ereignis)
        # hier verloren - das Gehirn sah nur "nichts Lohnendes".
        text += " ABGEBROCHEN: " + loot["aborted"]
    return text


def main() -> int:
    cmd = sys.argv[1] if len(sys.argv) > 1 else "score"
    bridge = Bridge(DEFAULT_PROFILE)

    if cmd == "loot":
        result = loot_area(bridge)
        print(f"Eingesammelt: {result['haul'] or 'nichts'}")
        if result["failed"]:
            print(f"Nicht erreichbar: {result['failed']}")
        print(equip_best(bridge))
        return 0

    if cmd == "equip":
        print(equip_best(bridge))
        return 0

    if cmd == "craft":
        print(craft(bridge, sys.argv[2] if len(sys.argv) > 2 else ""))
        return 0

    if cmd == "cook":
        print(cook_meal(bridge))
        return 0

    if cmd == "water":
        print(water_run(bridge))
        return 0

    if cmd == "find":
        print(find_item(bridge, sys.argv[2] if len(sys.argv) > 2 else ""))
        return 0

    if cmd == "explore":
        print(explore_step(bridge))
        return 0

    if cmd == "score":
        state = bridge.read_state() or {}
        inventory = state.get("inventory", [])
        for e in state.get("nearby", []):
            if e.get("kind") != "item":
                continue
            s = score_ground_item(e, inventory)
            print(f"{s:4}  {e.get('classname')}  ({e.get('distance', 0):.0f} m)")
        return 0

    print("Unbekanntes Kommando. loot | equip | score")
    return 1


if __name__ == "__main__":
    sys.exit(main())
