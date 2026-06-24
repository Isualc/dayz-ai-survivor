"""Generiert die Spawn-Loadouts der Agenten - eines pro Hintergrund.

IsuSurvivorLoadout: neutraler Default (warme Zivilkleidung, keine Waffen).
IsuViktorLoadout:   Jaeger     - Jagdkleidung, Jagdmesser, Streichhoelzer.
IsuBirgitLoadout:   Schwester  - warme Kleidung + Erste-Hilfe-Ausstattung
                                 (bewusst keine schluckbare Medizin: eat
                                 nimmt sonst die Tabletten statt des Apfels).
IsuIgorLoadout:     Bauer      - Arbeitskleidung, Feldhacke, Saatgut.
IsuKonradLoadout:   Ex-Militaer - TTsKO, Kampfstiefel, Makarov + 2 Magazine.

Alle bekommen Apfel + Pipsi-Dose als Startration. Schreibt ins Repo
(mod/loadouts) und ins Server-Profil (ExpansionMod/Loadouts).
"""

import json
import os

REPO = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
_SERVER_DIR = os.environ.get("DAYZ_SERVER_DIR", r"C:\Program Files (x86)\Steam\steamapps\common\DayZServer")
SERVER_LOADOUTS = os.path.join(_SERVER_DIR, "profiles", "ExpansionMod", "Loadouts")


def item(classname, health=(0.7, 0.9), cargo=None, attachments=None):
    return {
        "ClassName": classname,
        "Include": "",
        "Chance": 1.0,
        "Quantity": {"Min": 0.0, "Max": 0.0},
        "Health": [{"Min": health[0], "Max": health[1], "Zone": ""}],
        "InventoryAttachments": attachments or [],
        "InventoryCargo": cargo or [],
        "ConstructionPartsBuilt": [],
        "Sets": [],
    }


def slot(name, classnames):
    return {"SlotName": name, "Items": [item(cn) for cn in classnames]}


def loadout(slots, cargo_items, extra_cargo=None):
    cargo = [item(cn, health=(0.85, 1.0)) for cn in cargo_items]
    if extra_cargo:
        cargo += extra_cargo
    return {
        "ClassName": "",
        "Include": "",
        "Chance": 1.0,
        "Quantity": {"Min": 0.0, "Max": 0.0},
        "Health": [],
        "InventoryAttachments": slots,
        "InventoryCargo": cargo,
        "ConstructionPartsBuilt": [],
        "Sets": [],
    }


RATION = ["Apple", "SodaCan_Pipsi"]

LOADOUTS = {
    # Neutraler Default (Mod-Fallback bei spawn ohne loadout)
    "IsuSurvivorLoadout": loadout(
        [slot("Body", ["QuiltedJacket_Blue", "QuiltedJacket_Green", "QuiltedJacket_Grey"]),
         slot("Legs", ["CargoPants_Beige", "CargoPants_Blue", "CargoPants_Green"]),
         slot("Feet", ["WorkingBoots_Grey", "WorkingBoots_Brown"]),
         slot("Headgear", ["BeanieHat_Blue", "BeanieHat_Grey"]),
         slot("Gloves", ["WorkingGloves_Black", "WorkingGloves_Brown"]),
         slot("Back", ["TaloonBag_Blue", "TaloonBag_Green"])],
        RATION),

    # Viktor - Jaeger
    "IsuViktorLoadout": loadout(
        [slot("Body", ["HuntingJacket_Autumn", "HuntingJacket_Brown"]),
         slot("Legs", ["HunterPants_Autumn", "HunterPants_Brown"]),
         slot("Feet", ["HikingBoots_Brown", "HikingBoots_Black"]),
         slot("Headgear", ["BoonieHat_Olive"]),
         slot("Gloves", ["WorkingGloves_Brown"]),
         slot("Back", ["HuntingBag"])],
        RATION + ["HuntingKnife", "Matchbox"]),

    # Birgit - Krankenschwester (warm angezogen, Sanikasten im Gepaeck)
    "IsuBirgitLoadout": loadout(
        [slot("Body", ["QuiltedJacket_Red", "QuiltedJacket_Violet"]),
         slot("Legs", ["CargoPants_Blue", "CargoPants_Grey"]),
         slot("Feet", ["WorkingBoots_Grey"]),
         slot("Headgear", ["BeanieHat_Red", "BeanieHat_Blue"]),
         slot("Gloves", ["WorkingGloves_Black"]),
         slot("Back", ["TaloonBag_Orange"])],
        RATION + ["BandageDressing", "BandageDressing", "Epinephrine",
                  "Morphine", "SalineBagIV", "DisinfectantSpray"]),

    # Igor - Bauer (Feldhacke auf dem Ruecken, Saatgut im Gepaeck)
    "IsuIgorLoadout": loadout(
        [slot("Body", ["Sweater_Gray", "Sweater_Blue"]),
         slot("Legs", ["CanvasPants_Beige", "CanvasPants_Blue"]),
         slot("Feet", ["WorkingBoots_Brown"]),
         slot("Headgear", ["FlatCap_Brown", "FlatCap_Grey"]),
         slot("Gloves", ["WorkingGloves_Brown"]),
         slot("Back", ["TaloonBag_Green"]),
         slot("Shoulder", ["FarmingHoe"])],
        RATION + ["TomatoSeedsPack", "PepperSeedsPack", "ZucchiniSeedsPack"]),

    # Konrad - Ex-Militaer (TTsKO, GELADENE Makarov + Reserve-Magazin)
    "IsuKonradLoadout": loadout(
        [slot("Body", ["TTsKOJacket_Camo"]),
         slot("Legs", ["TTsKOPants"]),
         slot("Feet", ["CombatBoots_Black", "CombatBoots_Brown"]),
         slot("Headgear", ["MilitaryBeret_CDF"]),
         slot("Gloves", ["TacticalGloves_Black", "TacticalGloves_Beige"]),
         slot("Back", ["AssaultBag_Ttsko"])],
        RATION + ["Mag_IJ70_8Rnd"],
        extra_cargo=[item("MakarovIJ70", health=(0.85, 1.0),
                          attachments=[slot("magazine", ["Mag_IJ70_8Rnd"])])]),
}

# --- Winter-Varianten fuer Sakhal (Frostline) ---
# Dicke, gegen die Sakhal-types.xml verifizierte Winterkleidung, damit die
# NPCs nicht sofort erfrieren. Der Supervisor waehlt bei map=sakhal die
# "<Name>_Winter"-Variante. Identitaet bleibt (Jaeger/Sani/Bauer/Militaer),
# nur warm. Mask-Slot gibt Extra-Waerme.
WINTER_LOADOUTS = {
    "IsuSurvivorLoadout_Winter": loadout(
        [slot("Body", ["QuiltedJacket_Blue", "QuiltedJacket_Grey"]),
         slot("Legs", ["HunterPants_Winter"]),
         slot("Feet", ["ColdOperationBoots_Green", "ColdOperationBoots_Grey"]),
         slot("Headgear", ["Ushanka_Blue", "Ushanka_Black"]),
         slot("Gloves", ["SkiGloves_Blue"]),
         slot("Mask", ["BalaclavaMask_Blue"]),
         slot("Back", ["TaloonBag_Blue", "TaloonBag_Green"])],
        RATION),

    # Viktor - Jaeger (Daunenjacke, Fell-Ushanka)
    "IsuViktorLoadout_Winter": loadout(
        [slot("Body", ["DownJacket_Green", "DownJacket_Red"]),
         slot("Legs", ["HunterPants_Winter"]),
         slot("Feet", ["ColdOperationBoots_Green"]),
         slot("Headgear", ["SnowstormUshanka_Olive", "SnowstormUshanka_Brown"]),
         slot("Gloves", ["PaddedGloves_Brown"]),
         slot("Mask", ["BalaclavaMask_Green"]),
         slot("Back", ["HuntingBag"])],
        RATION + ["HuntingKnife", "Matchbox"]),

    # Angie/Birgit - Sanitaeterin
    "IsuBirgitLoadout_Winter": loadout(
        [slot("Body", ["QuiltedJacket_Red", "QuiltedJacket_Violet"]),
         slot("Legs", ["HunterPants_Winter"]),
         slot("Feet", ["ColdOperationBoots_Grey"]),
         slot("Headgear", ["Ushanka_Black"]),
         slot("Gloves", ["SkiGloves_Red"]),
         slot("Mask", ["BalaclavaMask_White"]),
         slot("Back", ["TaloonBag_Orange"])],
        RATION + ["BandageDressing", "BandageDressing", "Epinephrine",
                  "Morphine", "SalineBagIV", "DisinfectantSpray"]),

    # Igor - Bauer
    "IsuIgorLoadout_Winter": loadout(
        [slot("Body", ["QuiltedJacket_Green", "QuiltedJacket_Yellow"]),
         slot("Legs", ["HunterPants_Winter"]),
         slot("Feet", ["ColdOperationBoots_Grey"]),
         slot("Headgear", ["Ushanka_Green"]),
         slot("Gloves", ["PaddedGloves_Beige"]),
         slot("Mask", ["BalaclavaMask_Beige"]),
         slot("Back", ["TaloonBag_Green"]),
         slot("Shoulder", ["FarmingHoe"])],
        RATION + ["TomatoSeedsPack", "PepperSeedsPack", "ZucchiniSeedsPack"]),

    # Konrad - Ex-Militaer (Wintermilitaer-Mantel, GELADENE Makarov)
    "IsuKonradLoadout_Winter": loadout(
        [slot("Body", ["WinterMilitaryCoat_DarkGrey", "WinterMilitaryCoat_Grey"]),
         slot("Legs", ["HunterPants_Winter"]),
         slot("Feet", ["ColdOperationBoots_Camo"]),
         slot("Headgear", ["SnowstormUshanka_Navy", "SnowstormUshanka_White"]),
         slot("Gloves", ["PaddedGloves_Threat"]),
         slot("Mask", ["BalaclavaMask_Black"]),
         slot("Back", ["AssaultBag_Ttsko"])],
        RATION + ["Mag_IJ70_8Rnd"],
        extra_cargo=[item("MakarovIJ70", health=(0.85, 1.0),
                          attachments=[slot("magazine", ["Mag_IJ70_8Rnd"])])]),
}

repo_dir = os.path.join(REPO, "mod", "loadouts")
os.makedirs(repo_dir, exist_ok=True)
all_loadouts = dict(LOADOUTS)
all_loadouts.update(WINTER_LOADOUTS)
for name, data in all_loadouts.items():
    for base in (repo_dir, SERVER_LOADOUTS):
        path = os.path.join(base, name + ".json")
        with open(path, "w", encoding="utf-8") as f:
            json.dump(data, f, indent=4)
    print("geschrieben:", name)
