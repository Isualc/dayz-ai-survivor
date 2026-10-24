"""Nacharbeiten des Hauptagenten zur Session-Analyse 24.10.2026:
Nahkampf-Lock beim Ziehen, Lagezentrum-Details, Munitions-Fehlalarm,
Roster-Schreibweise und das "Ausgerüstet"-Präfix der Gedankenzeile."""
import json
import os
import tempfile
import time
import unittest
from unittest.mock import patch

import bridge
import run_agent
import survival
import tactics


class FakeBridge:
    def __init__(self, state, results):
        self.state = state
        self.results = list(results)
        self.calls = []

    def read_state(self):
        return self.state

    def run(self, action, text="", timeout=0, **kw):
        self.calls.append((action, text))
        if self.results:
            return self.results.pop(0)
        return {"status": "done", "detail": text}


def _state(in_hands="CZ75"):
    return {"npc": {"in_hands": in_hands, "in_vehicle": False},
            "inventory": [{"classname": "SteakKnife", "kind": "other", "health": 100.0},
                          {"classname": "CZ75", "kind": "firearm", "health": 100.0,
                           "quantity": 8}]}


class MeleeLockTests(unittest.TestCase):
    def test_lock_uses_equip_melee_lock_action(self):
        fb = FakeBridge(_state(), [{"status": "done", "detail": "SteakKnife"}])
        res = tactics.equip_melee(fb, log=lambda m: None, lock=True)
        self.assertTrue(res.startswith("Nahkampfwaffe gezogen"), res)
        self.assertEqual(fb.calls[0], ("equip_melee_lock", "SteakKnife"))

    def test_old_mod_falls_back_to_plain_equip(self):
        fb = FakeBridge(_state(), [
            {"status": "failed", "detail": "unbekannte action: equip_melee_lock"},
            {"status": "done", "detail": "SteakKnife"}])
        res = tactics.equip_melee(fb, log=lambda m: None, lock=True)
        self.assertTrue(res.startswith("Nahkampfwaffe gezogen"), res)
        self.assertEqual([c[0] for c in fb.calls], ["equip_melee_lock", "equip"])

    def test_knife_in_hand_still_sets_lock(self):
        fb = FakeBridge(_state(in_hands="SteakKnife"), [{"status": "done", "detail": "SteakKnife"}])
        res = tactics.equip_melee(fb, log=lambda m: None, lock=True)
        self.assertTrue(res.startswith("Nahkampfwaffe ist schon"), res)
        self.assertEqual(fb.calls, [("equip_melee_lock", "SteakKnife")])

    def test_without_lock_unchanged(self):
        fb = FakeBridge(_state(in_hands="SteakKnife"), [])
        res = tactics.equip_melee(fb, log=lambda m: None)
        self.assertTrue(res.startswith("Nahkampfwaffe ist schon"), res)
        self.assertEqual(fb.calls, [])

    def test_mod_wiring_present(self):
        src = os.path.join(os.path.dirname(os.path.abspath(__file__)), "..", "mod",
                           "IsuSurvivor", "scripts", "5_Mission", "IsuSurvivor", "isubridge.c")
        with open(src, "r", encoding="utf-8", errors="replace") as f:
            code = f.read()
        self.assertIn('case "equip_melee_lock":', code)
        self.assertIn("m_EquipMeleeLock = false;", code)
        self.assertIn("if (m_EquipMeleeLock && wanted.Expansion_IsMeleeWeapon())", code)


class SquadDetailsTests(unittest.TestCase):
    def _write(self, folder, agents, age=1.0):
        path = os.path.join(folder, "squad_state.json")
        with open(path, "w", encoding="utf-8") as f:
            json.dump({"t": time.time() - age, "agents": agents}, f)
        return path

    def test_details_add_heading_and_intent_age(self):
        agents = [
            {"id": "viktor", "name": "Viktor", "online": True, "spawned": True, "alive": True,
             "x": 100, "z": 0, "health": 90, "heading": "O", "intent": "Wasser holen",
             "intent_age": 10},
            {"id": "igor", "name": "Igor", "online": True, "spawned": True, "alive": True,
             "x": 0, "z": 300, "health": 100, "intent": "alter Plan", "intent_age": 120},
            {"id": "konrad", "name": "Konrad", "online": True, "spawned": True, "alive": True,
             "x": 0, "z": -50, "health": 100, "intent": "uralt", "intent_age": 500},
        ]
        with tempfile.TemporaryDirectory() as folder:
            path = self._write(folder, agents)
            with patch.object(bridge, "SQUAD_STATE_FILE", path):
                plain = bridge.squad_lines("birgit", 0, 0)
                rich = bridge.squad_lines("birgit", 0, 0, details=True)
        self.assertNotIn("will:", plain)
        self.assertNotIn("unterwegs", plain)
        self.assertIn("Viktor 100 m", rich)
        self.assertIn("unterwegs Richtung O", rich)
        self.assertIn("(will: Wasser holen)", rich)
        self.assertIn("(dachte vor 12", rich)   # 120 s + Dateialter ~1 s
        self.assertNotIn("uralt", rich)

    def test_runner_wrapper_passes_details(self):
        with patch.object(run_agent.bridge_mod, "squad_lines",
                          side_effect=lambda *a, **k: "D" if k.get("details") else "P"):
            self.assertEqual(run_agent._squad_lines("x", 0, 0), "P")
            self.assertEqual(run_agent._squad_lines("x", 0, 0, details=True), "D")


class AmmoLossTests(unittest.TestCase):
    def test_partial_ammo_decrease_is_not_loss(self):
        before = {"counts": {"Ammo_9x19": 30, "Rag": 2}}
        after = {"counts": {"Ammo_9x19": 22, "Rag": 2, "Apple": 1}}
        # Nachbau der Bedingung aus survival (pickup-Zweig)
        lost = [name for name, q in before["counts"].items()
                if name != "Apple" and survival._number(after["counts"].get(name)) < survival._number(q)
                and not (name.startswith(("Ammo_", "Mag_"))
                         and survival._number(after["counts"].get(name)) > 0)]
        self.assertEqual(lost, [])

    def test_source_has_ammo_exception(self):
        src = os.path.join(os.path.dirname(os.path.abspath(__file__)), "survival.py")
        with open(src, "r", encoding="utf-8") as f:
            self.assertIn('name.startswith(("Ammo_", "Mag_"))', f.read())


class ResultIntentTests(unittest.TestCase):
    def test_both_equip_prefixes(self):
        self.assertEqual(run_agent._result_intent("Ausgerüstet: AKM (vorher: leer)"), "AKM gezogen")
        self.assertEqual(run_agent._result_intent("Ausgeruestet: CZ75"), "CZ75 gezogen")


if __name__ == "__main__":
    unittest.main()
