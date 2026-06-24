#!/usr/bin/env python3
"""Erzeugt die Elite-Loadouts der Birgit-Mission (suppressierte Waffen, Muni,
Ausruestung) und deployt sie nach mod/loadouts UND ins Server-Loadout-Verzeichnis.
Anbauteile (Magazin/Optik/Schalldaempfer) gehen mit LEEREM SlotName in
InventoryAttachments - die Engine weist sie automatisch dem richtigen Slot zu
(so wie EastLoadout.json es macht; kein Risiko falscher Slot-Namen).

Suppressor-Zuordnung (gegen Vanilla-Referenz verifiziert): SVD nimmt AK_Suppressor,
M4A1/AUG nehmen M4_Suppressor, UMP45/PP19 nehmen PistolSuppressor.
"""
import json
import os

TOOLS = os.path.dirname(os.path.abspath(__file__))
REPO = os.path.dirname(TOOLS)
SRC = os.path.join(REPO, "mod", "loadouts")
_SERVER_DIR = os.environ.get("DAYZ_SERVER_DIR", r"C:\Program Files (x86)\Steam\steamapps\common\DayZServer")
DST = os.path.join(_SERVER_DIR, "profiles", "ExpansionMod", "Loadouts")


def itm(cn, health=(0.85, 1.0), q=None):
    return {
        "ClassName": cn, "Include": "", "Chance": 1.0,
        "Quantity": {"Min": float(q[0]), "Max": float(q[1])} if q else {"Min": 0.0, "Max": 0.0},
        "Health": [{"Min": health[0], "Max": health[1], "Zone": ""}] if health else [],
        "InventoryAttachments": [], "InventoryCargo": [],
        "ConstructionPartsBuilt": [], "Sets": [],
    }


def gun(cn, *attachments):
    """Waffe mit Anbauteilen - leerer SlotName => Engine sortiert automatisch."""
    w = itm(cn)
    w["InventoryAttachments"] = [{"SlotName": "", "Items": list(attachments)}]
    return w


def slot(name, *items):
    return {"SlotName": name, "Items": list(items)}


def loadout(slots, cargo):
    return {
        "ClassName": "", "Include": "", "Chance": 1.0,
        "Quantity": {"Min": 0.0, "Max": 0.0}, "Health": [],
        "InventoryAttachments": slots, "InventoryCargo": cargo,
        "ConstructionPartsBuilt": [], "Sets": [],
    }


def common_cargo():
    return [
        itm("BandageDressing"), itm("BandageDressing"), itm("BandageDressing"),
        itm("TetracyclineAntibiotics"), itm("Morphine"),
        itm("TacticalBaconCan"), itm("SodaCan_Cola"),
        itm("Matchbox"), itm("CombatKnife"),
    ]


def mil_clothes():
    return [
        slot("Body", itm("TTsKOJacket_Camo")),
        slot("Legs", itm("TTsKOPants")),
        slot("Feet", itm("CombatBoots_Beige")),
        slot("Headgear", itm("BoonieHat_Olive")),
        slot("Gloves", itm("TacticalGloves_Black")),
        slot("Vest", itm("PlateCarrierVest")),
        slot("Back", itm("AssaultBag_Ttsko")),
    ]


# Viktor - suppressierter Scharfschuetze
viktor = mil_clothes()
viktor.append(slot("Shoulder", gun("SVD",
    itm("Mag_SVD_10Rnd"), itm("PSO1Optic"), itm("AK_Suppressor"))))
viktor_cargo = [
    itm("Mag_SVD_10Rnd"), itm("Mag_SVD_10Rnd"), itm("Mag_SVD_10Rnd"),
    itm("Ammo_762x54", q=(20, 20)), itm("Ammo_762x54", q=(20, 20)),
    itm("Rangefinder"),
] + common_cargo()

# Konrad - suppressiertes Sturmgewehr, Muni-Spezialist
konrad = mil_clothes()
konrad.append(slot("Shoulder", gun("M4A1",
    itm("Mag_STANAG_30Rnd"), itm("ACOGOptic"), itm("M4_Suppressor"),
    itm("M4_OEBttstck"), itm("M4_RISHndgrd"))))
konrad_cargo = [
    itm("Mag_STANAG_30Rnd"), itm("Mag_STANAG_30Rnd"), itm("Mag_STANAG_30Rnd"),
    itm("Mag_STANAG_30Rnd"),
    itm("Ammo_556x45", q=(30, 30)), itm("Ammo_556x45", q=(30, 30)),
] + common_cargo()

# Igor - Nahkampf-Berserker (Sledgehammer) + suppressierte UMP45 als Backup
igor = mil_clothes()
igor.append(slot("Shoulder", itm("Sledgehammer")))
igor_cargo = [
    itm("Machete"),
    gun("UMP45", itm("Mag_UMP_25Rnd"), itm("PistolSuppressor")),
    itm("Mag_UMP_25Rnd"), itm("Mag_UMP_25Rnd"),
    itm("Ammo_45ACP", q=(25, 25)), itm("Ammo_45ACP", q=(25, 25)),
] + common_cargo()

FILES = {
    "IsuMissionViktor.json": loadout(viktor, viktor_cargo),
    "IsuMissionKonrad.json": loadout(konrad, konrad_cargo),
    "IsuMissionIgor.json": loadout(igor, igor_cargo),
}

for name, data in FILES.items():
    txt = json.dumps(data, indent=4)
    for d in (SRC, DST):
        os.makedirs(d, exist_ok=True)
        with open(os.path.join(d, name), "w", encoding="utf-8") as f:
            f.write(txt)
    print("wrote", name, "->", SRC, "+", DST)
print("done")
