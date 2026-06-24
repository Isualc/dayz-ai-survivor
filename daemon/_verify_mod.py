"""End-to-End-Verifikation der neuen Mod-Features ohne LLM.

Testet direkt ueber die Bridge: Spawn mit IsuSurvivorLoadout (warme
Kleidung, keine Waffen), heat_comfort im State, Dose oeffnen + trinken,
Apfel essen, equip_best ohne Waffen, wear-Fehlerpfad. Despawnt am Ende.
"""

import sys
import time

sys.path.insert(0, r"daemon")
from bridge import Bridge, DEFAULT_PROFILE, format_observation

bridge = Bridge(DEFAULT_PROFILE, "verify")
ok = True


def check(label, cond, detail=""):
    global ok
    mark = "OK  " if cond else "FAIL"
    if not cond:
        ok = False
    print(f"[{mark}] {label} {detail}".rstrip())


print("ping...")
r = bridge.run("ping", timeout=30)
check("ping", r.get("status") == "done", r.get("detail") or "")

print("spawn (Default-Loadout)...")
r = bridge.run("spawn", x=4450.0, z=2450.0, text="Pruefling",
               faction="civilian", timeout=40)
check("spawn", r.get("status") == "done", r.get("detail") or "")
time.sleep(3)

state = bridge.read_state() or {}
npc = state.get("npc", {})
inv = [i.get("classname", "") for i in state.get("inventory", [])]

check("heat_comfort im State", "heat_comfort" in npc,
      f"Wert: {npc.get('heat_comfort')}")
check("warme Jacke an", any(c.startswith("QuiltedJacket") for c in inv),
      ", ".join(c for c in inv if "Jacket" in c))
check("Muetze an", any(c.startswith("BeanieHat") for c in inv))
check("Handschuhe an", any(c.startswith("WorkingGloves") for c in inv))
check("Rucksack an", any(c.startswith("TaloonBag") for c in inv))
check("KEINE Waffe im Loadout",
      not any(c in ("AKM", "M4A1", "Mosin9130", "SKS") or c.startswith("Mag_")
              for c in inv), ", ".join(inv[:12]))
check("Apfel dabei", "Apple" in inv)
check("Pipsi-Dose dabei", "SodaCan_Pipsi" in inv)

obs, _ = format_observation(state)
check("Waerme in observe-Text", "Waerme:" in obs,
      [l for l in obs.splitlines() if "VITALS" in l][0] if "VITALS" in obs else "")

print("drink (Dose)...")
r = bridge.run("drink", timeout=30)
check("drink Dose", r.get("status") == "done", r.get("detail") or "")
check("Dose wurde aufgemacht", "aufgemacht" in (r.get("detail") or "") or
      "geoeffnet" in (r.get("detail") or ""), r.get("detail") or "")

print("eat (Apfel)...")
r = bridge.run("eat", timeout=30)
check("eat", r.get("status") == "done", r.get("detail") or "")

# Apfel-Reste wegraeumen, damit eat garantiert die Konserve angeht
bridge.run("drop", text="Apple", timeout=20)
bridge.run("drop", text="Apple", timeout=20)

print("Konserve ohne Werkzeug...")
r = bridge.run("give_item", text="BakedBeansCan", timeout=20)
check("give_item Konserve", r.get("status") == "done", r.get("detail") or "")
r = bridge.run("eat", timeout=30)
check("Konserve ohne Werkzeug verweigert",
      r.get("status") == "failed" and "Dosenoeffner" in (r.get("detail") or ""),
      r.get("detail") or "")

print("Konserve mit Dosenoeffner...")
r = bridge.run("give_item", text="CanOpener", timeout=20)
check("give_item CanOpener", r.get("status") == "done", r.get("detail") or "")
r = bridge.run("eat", timeout=45)
detail = r.get("detail") or ""
check("Konserve geoeffnet", r.get("status") == "done" and "geoeffnet" in detail,
      detail)
if "nochmal eat" in detail:
    time.sleep(2)
    r = bridge.run("eat", timeout=45)
    check("geoeffnete Konserve gegessen", r.get("status") == "done" and
          "_Opened" in (r.get("detail") or ""), r.get("detail") or "")

print("equip_best ohne Waffen...")
r = bridge.run("equip_best", timeout=20)
check("equip_best meldet sauber", r.get("status") == "failed" and
      "brauchbare" in (r.get("detail") or ""), r.get("detail") or "")

print("wear: Tausch bei belegtem Slot...")
r = bridge.run("give_item", text="BeanieHat_Green", timeout=20)
check("give_item Testmuetze", r.get("status") == "done", r.get("detail") or "")
time.sleep(2)
r = bridge.run("wear", text="BeanieHat_Green", timeout=20)
check("wear tauscht belegten Slot", r.get("status") == "done",
      f"{r.get('status')}: {r.get('detail') or ''}")

print("wear: direkt vom Boden (ohne pickup)...")
r = bridge.run("spawn_item", text="HuntingJacket_Winter", timeout=20)
check("spawn_item Jacke am Boden", r.get("status") == "done",
      r.get("detail") or "")
time.sleep(2)
r = bridge.run("wear", text="HuntingJacket_Winter", timeout=20)
if r.get("status") == "failed" and "nochmal" in (r.get("detail") or ""):
    time.sleep(2)
    r = bridge.run("wear", text="HuntingJacket_Winter", timeout=20)
check("wear vom Boden + Tausch", r.get("status") == "done",
      f"{r.get('status')}: {r.get('detail') or ''}")

print("despawn...")
r = bridge.run("despawn", timeout=20)
check("despawn", r.get("status") == "done")

print()
if ok:
    print("=== ALLE CHECKS BESTANDEN ===")
else:
    print("=== ES GAB FEHLSCHLAEGE (siehe FAIL-Zeilen) ===")
sys.exit(0 if ok else 1)
