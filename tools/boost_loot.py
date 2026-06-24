#!/usr/bin/env python3
"""Loot-Boost fuer DayZ: multipliziert alle <nominal>/<min> in einer
types.xml mit einem Faktor, cappt nominal, haelt min <= nominal und setzt
optional restock=0 (schneller Nachschub). 0-Werte (deaktivierte Items)
bleiben unangetastet. Schreibt vorher ein .bak.

Aufruf:
  python tools\\boost_loot.py <pfad-zur-types.xml> [faktor] [cap] [restock0]
Default: faktor=2.0  cap=50  restock0=1

Beispiel:
  python tools\\boost_loot.py "D:\\...\\dayzOffline.chernarusplus\\db\\types.xml" 2 50 1
"""
import math
import os
import shutil
import sys
import xml.etree.ElementTree as ET


def main() -> int:
    if len(sys.argv) < 2:
        print(__doc__)
        return 1
    src = sys.argv[1]
    factor = float(sys.argv[2]) if len(sys.argv) > 2 else 2.0
    cap = int(sys.argv[3]) if len(sys.argv) > 3 else 50
    restock0 = (sys.argv[4] != "0") if len(sys.argv) > 4 else True

    if not os.path.exists(src):
        print(f"types.xml nicht gefunden: {src}")
        return 1

    bak = src + ".bak"
    shutil.copy(src, bak)

    tree = ET.parse(src)
    root = tree.getroot()

    changed = 0
    sum_before = 0
    sum_after = 0
    for t in root.findall("type"):
        nom_el = t.find("nominal")
        min_el = t.find("min")
        if nom_el is None or min_el is None:
            continue
        try:
            nom = int(nom_el.text)
            mn = int(min_el.text)
        except (TypeError, ValueError):
            continue
        sum_before += nom
        if nom <= 0:            # deaktivierte Items in Ruhe lassen
            sum_after += nom
            continue
        new_nom = math.ceil(nom * factor)   # aufrunden, sonst geht nominal=1 unter
        if cap > 0:
            new_nom = min(new_nom, cap)
        new_min = math.ceil(mn * factor)
        new_min = min(new_min, new_nom)     # Garantie: min <= nominal
        nom_el.text = str(new_nom)
        min_el.text = str(new_min)
        sum_after += new_nom
        if restock0:
            rs = t.find("restock")
            if rs is not None:
                rs.text = "0"
        changed += 1

    tree.write(src, encoding="utf-8", xml_declaration=True)
    print(f"Loot-Boost fertig: Faktor {factor}, Cap {cap}, restock0={restock0}")
    print(f"  {changed} Eintraege angepasst")
    print(f"  nominal-Summe {sum_before} -> {sum_after} "
          f"(x{sum_after / max(sum_before, 1):.2f})")
    print(f"  Backup: {bak}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
