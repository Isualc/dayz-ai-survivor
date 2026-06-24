#!/usr/bin/env python3
"""Interaktiver Mikrofon-Check mit Sprechprobe.

Testet alle echten Eingabegeraete nacheinander: Du sprichst 3 Sekunden,
das Tool zeigt pro Geraet den Maximalpegel und ein Urteil. Danach waehlst
du das Geraet, das dich am besten hoert - die Wahl wird in arena/mic.json
gespeichert und gilt fuer mic_listener UND voice_router dauerhaft.

Aufruf:  python mic_select.py          (interaktiv, von start_all/start_arena)
         python mic_select.py --auto   (ohne Fragen: Automatik einstellen)
"""

import argparse
import json
import os
import sys

import numpy as np
import sounddevice as sd

DAEMON_DIR = os.path.dirname(os.path.abspath(__file__))
REPO_DIR = os.path.dirname(DAEMON_DIR)
MIC_CONFIG = os.path.join(REPO_DIR, "arena", "mic.json")

VIRTUAL_HINTS = ("cable", "virtual", "vdvad", "stereomix", "stereo mix",
                 "soundmapper", "sound mapper", "loopback", "voicemeeter",
                 "voicemod", "primaer", "primary", "prim")


def candidates() -> list[tuple[int, str, str]]:
    """Echte Eingabegeraete: (index, name, hostapi). MME pro Geraet reicht."""
    result = []
    hostapis = sd.query_hostapis()
    for idx, dev in enumerate(sd.query_devices()):
        if dev["max_input_channels"] < 1:
            continue
        if any(h in dev["name"].lower() for h in VIRTUAL_HINTS):
            continue
        api = hostapis[dev["hostapi"]]["name"]
        if "MME" not in api:
            continue
        result.append((idx, dev["name"], api))
    return result


def speak_test(idx: int, seconds: float = 3.0) -> float:
    """Nimmt auf und liefert den Maximal-RMS (ueber 100-ms-Fenster)."""
    # 48 kHz ZUERST: manche Headsets (PRO X 2 LIGHTSPEED) oeffnen zwar bei
    # 16 kHz, liefern dort aber Stille - bei 48 kHz kommt der Ton. Frueher
    # gewann 16 kHz und das Geraet galt faelschlich als stumm.
    for rate in (48000, 16000):
        try:
            rec = sd.rec(int(seconds * rate), samplerate=rate, channels=1,
                         dtype="int16", device=idx)
            sd.wait()
            x = rec.reshape(-1).astype(np.float64)
            win = int(rate * 0.1)
            peaks = [float(np.sqrt(np.mean(x[i:i + win] ** 2)))
                     for i in range(0, len(x) - win, win)]
            return max(peaks) if peaks else 0.0
        except Exception:
            continue
    return -1.0


def save(config: dict) -> None:
    os.makedirs(os.path.dirname(MIC_CONFIG), exist_ok=True)
    with open(MIC_CONFIG, "w", encoding="utf-8") as f:
        json.dump(config, f, indent=2, ensure_ascii=False)
    print(f"Gespeichert: {MIC_CONFIG}")


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--auto", action="store_true",
                        help="ohne Fragen auf Automatik stellen")
    args = parser.parse_args()

    if args.auto:
        save({"auto": True})
        return 0

    devs = candidates()
    if not devs:
        print("Keine Eingabegeraete gefunden.")
        return 1

    default_idx = sd.default.device[0]
    print()
    print("=== MIKROFON-CHECK ===")
    print("Gleich testet jedes Geraet 3 Sekunden. SPRICH WAEHREND JEDES TESTS")
    print("NORMAL INS MIKRO (z.B. 'Viktor, hoerst du mich, eins zwei drei').")
    print(f"{len(devs)} Geraete werden getestet. Discord darf offen bleiben.")
    input("ENTER zum Starten...")

    results = []
    for idx, name, api in devs:
        mark = " (Windows-Default)" if idx == default_idx else ""
        print(f"\n--> Teste [{idx}] {name}{mark}")
        print("    SPRICH JETZT (3 Sekunden)...")
        peak = speak_test(idx)
        if peak < 0:
            print("    unbrauchbar (laesst sich nicht oeffnen)")
            continue
        if peak >= 300:
            verdict = f"HOERT DICH LAUT UND DEUTLICH (Pegel {peak:.0f})"
        elif peak >= 60:
            verdict = f"hoert etwas, eher leise (Pegel {peak:.0f})"
        else:
            verdict = f"STILL (Pegel {peak:.0f})"
        print(f"    -> {verdict}")
        results.append((idx, name, peak))

    if not results:
        print("\nKein einziges Geraet war nutzbar.")
        return 1

    results.sort(key=lambda r: -r[2])
    print("\n=== ERGEBNIS (beste zuerst) ===")
    for idx, name, peak in results:
        print(f"  [{idx:2}] Pegel {peak:7.0f}  {name}")
    if results[0][2] < 60:
        print("\nACHTUNG: Kein Geraet hat dich klar gehoert!")
        print("Moegliche Ursachen: Headset-MUTE-Taste, G HUB, Discord-Exklusivmodus,")
        print("oder du warst zu leise. Windows-Soundeinstellungen pruefen.")

    print("\nWelches Mikrofon soll Viktor benutzen?")
    choice = input(f"Index eingeben (ENTER = bestes = {results[0][0]}, a = Automatik): ").strip()

    config = {}
    if choice.lower() == "a":
        config["auto"] = True
    else:
        idx = results[0][0]
        if choice:
            try:
                idx = int(choice)
            except ValueError:
                print("Ungueltig - nehme das beste.")
        config["device_index"] = idx
        config["device_name"] = sd.query_devices(idx)["name"]

    print("\nWie soll gehoert werden?")
    print("  [1] Push-to-Talk: CAPSLOCK (Feststelltaste) gedrueckt halten zum")
    print("      Sprechen - dieselbe Taste wie der DayZ-Funk, Spieler und")
    print("      Agenten hoeren dich gleichzeitig (empfohlen, hoert sonst")
    print("      NICHTS - kein Raum, keine Familie, kein TV)")
    print("  [2] Immer offen: alles ueber der Lautstaerke-Schwelle wird gehoert")
    mode_choice = input("Auswahl (ENTER = 1): ").strip()

    if mode_choice == "2":
        config["mode"] = "open"
    else:
        config["mode"] = "ptt"
        key = input("PTT-Taste (ENTER = CAPSLOCK; auch F1-F12, MAUS4, MAUS5): ").strip().upper()
        config["ptt_key"] = key if key else "CAPSLOCK"

    save(config)
    if config.get("auto"):
        print("Geraet: Automatik.")
    else:
        print(f"Geraet: [{config['device_index']}] {config['device_name']}")
    if config["mode"] == "ptt":
        print(f"Modus: Push-to-Talk mit [{config['ptt_key']}].")
    else:
        print("Modus: immer offen.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
