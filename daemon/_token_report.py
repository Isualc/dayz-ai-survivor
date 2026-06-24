#!/usr/bin/env python3
"""Token-Tagesreport: summiert [TOKENS ZUG]-Zeilen aller Agenten-Journale
eines Tages, gruppiert nach Phase (vor/nach einem Schnitt-Zeitpunkt) und
Modell. Zeigt Zuege, Tokens pro Zug und USD.

WICHTIG zur USD-Summe: Der 'Session: ..., X USD'-Wert im Journal ist
Claude-Code-sessionkumulativ und FAELLT bei jeder Kontext-Rotation und nach
jedem Tod zurueck auf 0 (frischer Claude-Prozess = neuer total_cost_usd). Nur
das letzte Segment zu nehmen unterschaetzt die echten Kosten teils um die
Haelfte. Darum erkennen wir Resets (USD-Wert sinkt) und summieren die
Segment-Spitze VOR jedem Reset plus das Schluss-Segment. Die Token-Zahlen
selbst sind Pro-Zug-Deltas und brauchen keine Reset-Behandlung.

Aufruf: python daemon\\_token_report.py [JJJJMMTT] [SchnittHHMM]
        Default: heute, Schnitt 1400.
"""
import glob
import os
import re
import sys
import time

REPO = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
DAY = sys.argv[1] if len(sys.argv) > 1 else time.strftime("%Y%m%d")
CUT = sys.argv[2] if len(sys.argv) > 2 else "1400"

TURN_RE = re.compile(
    r"\[TOKENS ZUG\] ([^:]+): in=(\d+) out=(\d+) cache_read=(\d+) "
    r"cache_write=(\d+)")
USD_RE = re.compile(r"Session: .*?, ([\d.]+) USD")

journals = []
journals += glob.glob(os.path.join(REPO, "agent_home", "journal",
                                   f"journal_{DAY}_*.log"))
journals += glob.glob(os.path.join(REPO, "agent_homes", "*", "journal",
                                   f"journal_{DAY}_*.log"))

# (phase, model) -> [zuege, in, out, cache_read, cache_write]
agg: dict = {}
usd: dict = {}   # (phase, model) -> USD (Summe der Session-Endwerte)

for path in sorted(journals):
    stamp = os.path.basename(path).split("_")[2].split(".")[0]  # HHMMSS
    phase = "vorher"
    if stamp[:4] >= CUT:
        phase = "nachher"

    model_of_file = ""
    # Reset-bewusste USD-Summe: prev = laufender Sessionwert; faellt er, war
    # das vorige Segment komplett (Rotation/Tod) und wird verbucht.
    file_usd = 0.0
    prev_usd = 0.0
    with open(path, "r", encoding="utf-8", errors="replace") as f:
        for line in f:
            m = TURN_RE.search(line)
            if m:
                model = m.group(1).strip()
                if model == "<synthetic>":
                    continue
                model_of_file = model
                key = (phase, model)
                a = agg.setdefault(key, [0, 0, 0, 0, 0])
                a[0] += 1
                for i in range(1, 5):
                    a[i] += int(m.group(i + 1))
            mu = USD_RE.search(line)
            if mu:
                cur = float(mu.group(1))
                if cur < prev_usd - 1e-9:   # Reset: voriges Segment war komplett
                    file_usd += prev_usd
                prev_usd = cur
    file_usd += prev_usd                    # letztes (oder einziges) Segment
    if model_of_file:
        k = (phase, model_of_file)
        usd[k] = usd.get(k, 0.0) + file_usd

print(f"Tag {DAY}, Schnitt {CUT[:2]}:{CUT[2:]} Uhr "
      f"({len(journals)} Journale)\n")
for phase in ("vorher", "nachher"):
    print(f"=== {phase.upper()} ===")
    rows = sorted(k for k in agg if k[0] == phase)
    if not rows:
        print("  (keine Daten)")
    for key in rows:
        z, ti, to, cr, cw = agg[key]
        print(f"  {key[1]:24} Zuege={z:4}  out={to:>7,}  "
              f"cache_read={cr:>12,} ({cr // max(z, 1):>7,}/Zug)  "
              f"cache_write={cw:>9,} ({cw // max(z, 1):>6,}/Zug)  "
              f"USD={usd.get(key, 0.0):7.2f}")
    print()
