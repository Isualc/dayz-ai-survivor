#!/usr/bin/env python3
"""Offline-Test fuer den pickup-Namensmatch-Fix (2026-06-16). Laeuft OHNE
DayZ-Server, ohne NPC, ohne File-Bridge - reines Python.

Hintergrund: pickup griff oft das FALSCHE Bodenitem (pickup(WolfSteakMeat) ->
FieldShovel). Ursache war ein exakter Vollname-Vergleich plus ein leerer Filter
(vertippter MCP-Parameter), der das naechste beliebige Item nahm.

Zwei Teile:
  1. SPEC - reine Python-Spiegelung der Mod-Regel IsuBridge.FindNearestGroundItem
     (EnforceScript). Prueft exakt-vor-Teilstring, das Leerer-Filter-Verhalten
     und - der eigentliche Bug - dass ein gesuchter, aber nicht vorhandener Name
     NICHT das naechste beliebige Item greift. Der EnforceScript-Code steht als
     Kommentar daneben; aendert sich die Mod-Regel, hier nachziehen. Das testet
     die ALGORITHMUS-Logik, nicht die Kompilierung (die prueft der Server-Start).
  2. REAL - monkeypatcht die Bridge und ruft das ECHTE dayz_mcp.pickup() mit
     vertippten Parametern auf: prueft, dass die Alias-Params item/name den
     richtigen needle erzeugen statt eines leeren.

Live-Vollpruefung (separat, braucht laufenden Server):
  python daemon\\test_driver.py pickup --item Apple

Start: python daemon\\test_pickup_match.py
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


# --------------------------------------------------------------------------
# TEIL 1 - SPEC: Spiegelung von IsuBridge.FindNearestGroundItem (EnforceScript)
#
# Original (mod\\IsuSurvivor\\...\\IsuBridge.c, 2026-06-16):
#   ItemBase nearestExact; float nearestExactDist = radius+1;
#   ItemBase nearestSub;   float nearestSubDist   = radius+1;
#   foreach (obj) {
#     item = ItemBase.Cast(obj);
#     if (!item || item.GetHierarchyParent()) continue;   // in fremdem Inventar
#     if (IsClaimedByOther(item)) continue;                // anderer Bot hat's
#     dist = ...; t = item.GetType();
#     if (classnameFilter == "") { if (dist<sub) sub=item; continue; }
#     if (t == classnameFilter)         { if (dist<exact) exact=item; }
#     else if (t.IndexOf(filter) > -1)  { if (dist<sub)   sub=item;   }
#   }
#   if (nearestExact) return nearestExact; return nearestSub;
# --------------------------------------------------------------------------

def pick_ground_item(needle, candidates):
    """candidates: list of {classname, dist, parent(bool), claimed(bool)}."""
    nearest_exact = None
    nearest_exact_dist = float("inf")
    nearest_sub = None
    nearest_sub_dist = float("inf")
    for c in candidates:
        if c.get("parent") or c.get("claimed"):
            continue
        dist = c["dist"]
        t = c["classname"]
        if needle == "":
            if dist < nearest_sub_dist:
                nearest_sub_dist = dist
                nearest_sub = c
            continue
        if t == needle:
            if dist < nearest_exact_dist:
                nearest_exact_dist = dist
                nearest_exact = c
        elif needle in t:   # EnforceScript: t.IndexOf(needle) > -1 (case-sensitiv)
            if dist < nearest_sub_dist:
                nearest_sub_dist = dist
                nearest_sub = c
    if nearest_exact:
        return nearest_exact
    return nearest_sub


def _cls(item):
    if item:
        return item["classname"]
    return None


print("== TEIL 1: Matching-Regel (Spec-Spiegelung des Mods) ==")

# A) DER BUG: gesuchtes Item da, aber ein naeheres Fremd-Item -> exakt gewinnt
cands = [{"classname": "FieldShovel", "dist": 1},
         {"classname": "WolfSteakMeat", "dist": 3}]
check("A pickup(WolfSteakMeat) -> WolfSteakMeat statt FieldShovel",
      _cls(pick_ground_item("WolfSteakMeat", cands)) == "WolfSteakMeat",
      "(genau der gemeldete Bug)")

# B) Teilstring-Fallback
check("B pickup(WolfSteak) -> WolfSteakMeat (Teilstring)",
      _cls(pick_ground_item("WolfSteak", cands)) == "WolfSteakMeat")

# C) exakter Treffer schlaegt naeheren Teilstring
cands2 = [{"classname": "SharpWoodenStick", "dist": 1},
          {"classname": "WoodenStick", "dist": 4}]
check("C pickup(WoodenStick) -> WoodenStick (exakt vor naeherem Teilstring)",
      _cls(pick_ground_item("WoodenStick", cands2)) == "WoodenStick")

# D) leerer Filter -> naechstes beliebiges (nur fuer bewusstes pickup())
check("D pickup('') -> FieldShovel (naechstes, argloses pickup)",
      _cls(pick_ground_item("", cands)) == "FieldShovel")

# E) ANTI-BUG: gesucht, aber NICHT vorhanden -> None statt falsches Item
check("E pickup(Nonexistent) -> None (greift NICHT das naechste)",
      pick_ground_item("Nonexistent", [{"classname": "FieldShovel", "dist": 1}]) is None)

# F) der Matchbox->Plum-Bug: Matchbox nicht da -> None, nicht Plum
check("F pickup(Matchbox) bei [Plum,Apple] -> None (nicht Plum)",
      pick_ground_item("Matchbox",
                       [{"classname": "Plum", "dist": 1},
                        {"classname": "Apple", "dist": 2}]) is None)

# G) beanspruchtes Item (anderer Bot) wird uebersprungen
gc = [{"classname": "Apple", "dist": 1, "claimed": True},
      {"classname": "Apple", "dist": 5}]
g = pick_ground_item("Apple", gc)
check("G beanspruchtes Apple@1 uebersprungen -> Apple@5",
      g is not None and g["dist"] == 5)

# H) Item in fremdem Inventar (parent) wird uebersprungen
hc = [{"classname": "Apple", "dist": 1, "parent": True},
      {"classname": "Apple", "dist": 5}]
h = pick_ground_item("Apple", hc)
check("H Apple in Inventar (parent) uebersprungen -> Apple@5",
      h is not None and h["dist"] == 5)

# I) mehrere Teilstring-Treffer -> naechster gewinnt
ic = [{"classname": "LongWoodenStick", "dist": 8},
      {"classname": "SharpWoodenStick", "dist": 2}]
check("I pickup(Stick) -> SharpWoodenStick (naechster Teilstring)",
      _cls(pick_ground_item("Stick", ic)) == "SharpWoodenStick")


# --------------------------------------------------------------------------
# TEIL 2 - REAL: echtes dayz_mcp.pickup() mit gemockter Bridge
# --------------------------------------------------------------------------
print()
print("== TEIL 2: echtes dayz_mcp.pickup() (Bridge gemockt) ==")

# argparse von dayz_mcp neutralisieren (es liest sys.argv auf Modulebene)
sys.argv = ["dayz_mcp.py", "--npc-id", "test",
            "--outbox", os.path.join(DAEMON_DIR, "_test_outbox.jsonl")]

dayz_mcp = None
try:
    import dayz_mcp as _dm
    dayz_mcp = _dm
    check("dayz_mcp importierbar", True)
except Exception as e:
    check("dayz_mcp importierbar", False, f"Import-Fehler: {e}")

if dayz_mcp:
    class FakeBridge:
        def __init__(self):
            self.last_text = None
            self.calls = []

        def run(self, action, text="", timeout=0, interruptible=False, **kw):
            self.calls.append((action, text))
            if action == "pickup":
                self.last_text = text
            return {"status": "done", "detail": text or "(leer)"}

    fake = FakeBridge()
    dayz_mcp.BRIDGE = fake

    def needle_for(**kwargs):
        fake.last_text = None
        dayz_mcp.pickup(**kwargs)
        return fake.last_text

    check("classname='Apple' -> needle 'Apple'",
          needle_for(classname="Apple") == "Apple")
    check("item_name='Apple' -> needle 'Apple'",
          needle_for(item_name="Apple") == "Apple")
    check("item='WolfSteakMeat' (Alias-Fix) -> needle 'WolfSteakMeat'",
          needle_for(item="WolfSteakMeat") == "WolfSteakMeat",
          "(Fumble-Param, war frueher leer)")
    check("name='Matchbox' (Alias-Fix) -> needle 'Matchbox'",
          needle_for(name="Matchbox") == "Matchbox")
    check("pickup() ohne Arg -> needle '' (bewusst naechstes)",
          needle_for() == "")
    check("Vorrang classname vor item-Alias",
          needle_for(classname="A", item="B") == "A")

    # Aufraeumen: evtl. angelegte Test-Outbox entfernen
    try:
        os.remove(os.path.join(DAEMON_DIR, "_test_outbox.jsonl"))
    except OSError:
        pass


print()
if _ok:
    print("=== ALLE PICKUP-TESTS BESTANDEN ===")
else:
    print("=== ES GAB FEHLSCHLAEGE (siehe FAIL-Zeilen) ===")
sys.exit(0 if _ok else 1)
