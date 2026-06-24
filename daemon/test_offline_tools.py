#!/usr/bin/env python3
"""Offline-Tests fuer die Effizienz-Fixes vom 2026-06-16 (kein Server noetig):

  * Alias-Params der MCP-Tools (wear/find_item/drop/give_to) - ein vertippter
    Parametername darf den needle nicht mehr leeren.
  * (d) bridge._inbox_should_interrupt - Routine-Funk des Orchestrators
    unterbricht keinen Marsch, Spieler-/Prio-Funk schon.
  * (b) tactics._retreat_from_fire - nach dem Garen garantiert WEG vom Feuer.
  * orchestrator.is_threat - Fleisch/Felle/passive Tiere sind keine Bedrohung.

Start: python daemon\\test_offline_tools.py
"""

import os
import sys

DAEMON_DIR = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, DAEMON_DIR)

_ok = True


def check(label, cond, detail=""):
    global _ok
    mark = "OK  " if cond else "FAIL"
    if not cond:
        _ok = False
    print(f"[{mark}] {label} {detail}".rstrip())


# ===========================================================================
print("== (d) bridge._inbox_should_interrupt ==")
import bridge
import json

_inbox = os.path.join(DAEMON_DIR, "_test_inbox.jsonl")


def _write(lines):
    with open(_inbox, "w", encoding="utf-8") as f:
        for ln in lines:
            f.write(json.dumps(ln, ensure_ascii=False) + "\n")
    return os.path.getsize(_inbox)


def _append(line):
    with open(_inbox, "a", encoding="utf-8") as f:
        f.write(json.dumps(line, ensure_ascii=False) + "\n")


try:
    base = _write([{"user": "Lagezentrum", "text": "alt", "prio": False}])
    # kein Wachstum -> nicht unterbrechen
    check("kein neuer Funk -> nicht unterbrechen",
          bridge._inbox_should_interrupt(_inbox, base) is False)
    # Routine-Lagezentrum-Sitrep -> NICHT unterbrechen
    _append({"user": "Lagezentrum", "text": "Squad-Lage ...", "prio": False})
    check("Routine-Lagezentrum (prio False) -> NICHT unterbrechen",
          bridge._inbox_should_interrupt(_inbox, base) is False)
    # Prio-Lagezentrum (Tod) -> unterbrechen
    base2 = os.path.getsize(_inbox)
    _append({"user": "Lagezentrum", "text": "Igor ist gefallen", "prio": True})
    check("Prio-Lagezentrum (prio True) -> unterbrechen",
          bridge._inbox_should_interrupt(_inbox, base2) is True)
    # Spieler-Funk -> unterbrechen
    base3 = os.path.getsize(_inbox)
    _append({"user": "Player", "text": "kommt her", "prio": False})
    check("Spieler-Funk (user != Lagezentrum) -> unterbrechen",
          bridge._inbox_should_interrupt(_inbox, base3) is True)
    # Unparsbare Zeile -> sicherheitshalber unterbrechen
    base4 = os.path.getsize(_inbox)
    with open(_inbox, "a", encoding="utf-8") as f:
        f.write("{kaputtes json\n")
    check("unparsbare Zeile -> unterbrechen (konservativ)",
          bridge._inbox_should_interrupt(_inbox, base4) is True)
finally:
    try:
        os.remove(_inbox)
    except OSError:
        pass


# ===========================================================================
print()
print("== (b) tactics._retreat_from_fire ==")
import tactics
import math


class FireBridge:
    def __init__(self, npc, nearby):
        self._state = {"npc": npc, "nearby": nearby}
        self.move = None

    def read_state(self, *a, **k):
        return self._state

    def run(self, action, x=0.0, z=0.0, **k):
        if action == "move_to":
            self.move = (x, z)
        return {"status": "done"}


# Feuer 2m oestlich, NPC bei (100,100): Rueckzug muss WEITER weg vom Feuer sein
fb = FireBridge({"pos_x": 100.0, "pos_z": 100.0},
                [{"kind": "fire_burning", "x": 102.0, "z": 100.0}])
tactics._retreat_from_fire(fb, dist=6.0)
moved = fb.move
old_d = math.hypot(100 - 102, 100 - 100)        # 2 m
new_d = math.hypot(moved[0] - 102, moved[1] - 100) if moved else 0
check("Rueckzug ausgefuehrt", moved is not None, str(moved))
check("Rueckzug ist WEITER vom Feuer weg (6 m statt 2 m)",
      moved is not None and abs(new_d - 6.0) < 0.01 and new_d > old_d,
      f"alt={old_d:.1f}m neu={new_d:.1f}m")

# Kein Feuer in der Naehe -> kein move_to
fb2 = FireBridge({"pos_x": 5.0, "pos_z": 5.0}, [{"kind": "item", "x": 6.0, "z": 5.0}])
tactics._retreat_from_fire(fb2)
check("kein Feuer -> kein Rueckzug", fb2.move is None)


# ===========================================================================
print()
print("== orchestrator.is_threat ==")
import orchestrator as O


def thr(cls, kind="", hostile=False):
    return O.is_threat({"classname": cls, "kind": kind}, hostile)


check("WolfSteakMeat (item) -> keine Bedrohung", thr("WolfSteakMeat", "item") is False)
check("WolfSteakMeat (Name) -> keine Bedrohung", thr("WolfSteakMeat") is False)
check("Animal_BosTaurusF (Kuh) -> keine Bedrohung", thr("Animal_BosTaurusF_Brown", "animal") is False)
check("Bear_Beige -> Bedrohung", thr("Bear_Beige", "animal") is True)
check("ZmbF_... -> Bedrohung", thr("ZmbF_SkaterYoung", "") is True)
check("Animal_CanisLupus (Wolf) -> Bedrohung", thr("Animal_CanisLupus_Grey", "animal") is True)


# ===========================================================================
print()
print("== Alias-Params der MCP-Tools (echte Funktionen, Bridge gemockt) ==")
sys.argv = ["dayz_mcp.py", "--npc-id", "test",
            "--outbox", os.path.join(DAEMON_DIR, "_test_outbox.jsonl")]

dayz_mcp = None
try:
    import dayz_mcp as _dm
    dayz_mcp = _dm
    check("dayz_mcp importierbar", True)
except Exception as e:
    check("dayz_mcp importierbar", False, f"{e}")

if dayz_mcp:
    class RecBridge:
        def __init__(self):
            self.last = None

        def run(self, action, text="", **k):
            self.last = (action, text)
            return {"status": "done", "detail": text}

    rec = RecBridge()
    dayz_mcp.BRIDGE = rec
    # find_item delegiert an tactics.find_item -> dort den needle abgreifen
    dayz_mcp.tactics.find_item = lambda b, n: f"FI:{n}"

    dayz_mcp.wear(item_name="BeanieHat")
    check("wear(item_name=) -> needle 'BeanieHat'", rec.last == ("wear", "BeanieHat"))
    dayz_mcp.wear(item="Cap")
    check("wear(item= Alias) -> needle 'Cap'", rec.last == ("wear", "Cap"))
    r = dayz_mcp.wear()
    check("wear() ohne Arg -> klare Fehlermeldung, kein Bridge-Call",
          "classname" in r and rec.last == ("wear", "Cap"))

    dayz_mcp.drop(item="Mosin9130")
    check("drop(item= Alias) -> needle 'Mosin9130'", rec.last == ("drop", "Mosin9130"))

    dayz_mcp.give_to(player="Konrad", item="AmmoBox_545x39")
    check("give_to(player=, item=) -> 'Konrad|AmmoBox_545x39'",
          rec.last == ("hand_over", "Konrad|AmmoBox_545x39"))

    fi = dayz_mcp.find_item(item_type="Nail")
    check("find_item(item_type= Alias) -> needle 'Nail'", fi == "FI:Nail")

    try:
        os.remove(os.path.join(DAEMON_DIR, "_test_outbox.jsonl"))
    except OSError:
        pass


print()
if _ok:
    print("=== ALLE OFFLINE-TESTS BESTANDEN ===")
else:
    print("=== ES GAB FEHLSCHLAEGE (siehe FAIL-Zeilen) ===")
sys.exit(0 if _ok else 1)
