"""Offline-Regressionstests für die Verhaltens-Fixes vom 08.09.2026 (kein Server nötig).

  * provider_worker.repeat_guard / DecisionWorker: identische Aufrufe mit
    Fortschritt (explore_step an neuen Orten) laufen weiter; festgefahrene
    Aufrufe beenden den Zug regulär statt als Backend-Fehler.
  * tactics Drop-Ledger: merken, verfallen, vergessen, Umkreis-Regel.
  * tactics.loot_area und survival.choose_action lassen eigene Ablagen liegen.
  * bridge.wait_status bricht bei neu geschriebener urgent_event.json ab;
    interrupt_reason unterscheidet Spieler-Funk und kritisches Ereignis.
  * run_agent._is_abortable / write_urgent_event.
  * tactics.equip_by_classname: Klon-Nachprüfung bei "Equip-Ziel verschwunden".

Start: python -B -m unittest discover -s daemon -p test_behavior_fixes_0908.py -v
"""

import copy
import json
import os
import tempfile
import threading
import time
import unittest
from unittest import mock

import bridge as bridge_mod
import survival
import tactics
from provider_worker import DecisionWorker, repeat_guard
from test_llm_backends import FakePlanner, decision


class RepeatGuardContracts(unittest.TestCase):
    def test_progress_allows_repeats_up_to_cap(self):
        history = []
        for i in range(4):
            self.assertEqual(repeat_guard("explore_step", history), "", i)
            history.append((False, f"Angekommen bei x={i}. Eingesammelt: nichts."))
        self.assertEqual(repeat_guard("explore_step", history), "")
        history.append((False, "Angekommen bei x=9."))
        self.assertIn("called 5 times", repeat_guard("explore_step", history))

    def test_two_failures_stop(self):
        history = [(True, "Fehlgeschlagen: A"), (True, "Fehlgeschlagen: B")]
        self.assertIn("failed twice", repeat_guard("equip_melee", history))

    def test_identical_results_stop(self):
        history = [(False, "Angekommen. nichts"), (False, "Angekommen. nichts")]
        self.assertIn("same result twice", repeat_guard("explore_step", history))

    def test_single_failure_then_success_continues(self):
        self.assertEqual(repeat_guard("pickup", [(True, "x"), (False, "y")]), "")

    def test_read_only_tools_stay_strict(self):
        history = [(False, "Lage 10:04"), (False, "Lage 10:05")]
        self.assertIn("repeated 2 times", repeat_guard("observe", history))
        self.assertEqual(repeat_guard("observe", history[:1]), "")


class ExploreTools:
    catalog = [{"name": "explore_step", "inputSchema": {"type": "object"}}]

    def __init__(self, results):
        self.results, self.calls = list(results), []

    async def call(self, name, arguments):
        self.calls.append((name, arguments))
        text = self.results.pop(0)
        return text, text.startswith("Fehlgeschlagen")


class WorkerRepeatContracts(unittest.IsolatedAsyncioTestCase):
    def worker(self, planner, tools):
        events = []
        worker = DecisionWorker("moonshot/kimi-k3", planner, tools, "Survive.",
                                emit_fn=events.append, max_steps=8)
        return worker, events

    async def test_explore_steps_with_progress_are_not_stopped(self):
        tools = ExploreTools([f"Angekommen bei x={i}. Eingesammelt: nichts." for i in range(4)])
        planner = FakePlanner(*[decision("explore_step")] * 4, decision())
        worker, events = self.worker(planner, tools)
        result = await worker.turn("ROUTINE-TICK")
        self.assertEqual(len(tools.calls), 4)
        self.assertEqual(result["subtype"], "success")
        self.assertFalse(any("[HINWEIS]" in json.dumps(e) for e in events))

    async def test_stuck_explore_ends_turn_without_backend_error(self):
        tools = ExploreTools(["Angekommen bei x=1. Eingesammelt: nichts."] * 3)
        planner = FakePlanner(*[decision("explore_step")] * 4)
        worker, events = self.worker(planner, tools)
        result = await worker.turn("ROUTINE-TICK")
        self.assertEqual(len(tools.calls), 2)
        self.assertEqual(result["subtype"], "success")
        self.assertFalse(result["is_error"])
        self.assertEqual(worker.failure_count, 0)
        self.assertTrue(any("[HINWEIS]" in json.dumps(e) for e in events))
        self.assertIn("Turn ended by the worker", json.dumps(worker.turns))


class DropLedgerContracts(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.path = os.path.join(self.tmp.name, "dropped_items.json")

    def tearDown(self):
        self.tmp.cleanup()

    def test_remember_load_forget(self):
        tactics.remember_drop("Mag_AKM_30Rnd", 100.0, 200.0, path=self.path, now=1000.0)
        drops = tactics.load_drops(self.path, now=1010.0)
        self.assertEqual([d["classname"] for d in drops], ["Mag_AKM_30Rnd"])
        self.assertEqual(tactics.forget_drop("Mag_AKM_30Rnd", path=self.path, now=1010.0), 1)
        self.assertEqual(tactics.load_drops(self.path, now=1010.0), [])

    def test_entries_expire(self):
        tactics.remember_drop("Wellies_Grey", 0, 0, path=self.path, now=1000.0)
        later = 1000.0 + tactics.DROP_IGNORE_SECONDS + 1
        self.assertEqual(tactics.load_drops(self.path, now=later), [])

    def test_is_own_drop_uses_classname_and_radius(self):
        drops = [{"classname": "AmmoBox_22_50Rnd", "x": 100.0, "z": 100.0, "t": 0.0}]
        near = {"classname": "AmmoBox_22_50Rnd", "x": 120.0, "z": 100.0}
        far = {"classname": "AmmoBox_22_50Rnd", "x": 300.0, "z": 100.0}
        other = {"classname": "TunaCan", "x": 100.0, "z": 100.0}
        self.assertTrue(tactics.is_own_drop(near, drops))
        self.assertFalse(tactics.is_own_drop(far, drops))
        self.assertFalse(tactics.is_own_drop(other, drops))
        # Ohne Weltposition: NPC-Abstand zum Ablageort minus Item-Distanz.
        no_pos = {"classname": "AmmoBox_22_50Rnd", "distance": 5}
        self.assertTrue(tactics.is_own_drop(no_pos, drops, {"pos_x": 130.0, "pos_z": 100.0}))
        self.assertFalse(tactics.is_own_drop(no_pos, drops, {"pos_x": 500.0, "pos_z": 100.0}))

    def test_corrupt_ledger_reads_as_empty(self):
        with open(self.path, "w", encoding="utf-8") as f:
            f.write("{kein json")
        self.assertEqual(tactics.load_drops(self.path), [])


class LootBridge:
    def __init__(self, state):
        self.state, self.runs = state, []

    def read_state(self):
        return copy.deepcopy(self.state)

    def run(self, action, **kw):
        self.runs.append((action, kw.get("text", "")))
        if action == "pickup":
            self.state["nearby"] = [e for e in self.state["nearby"]
                                    if e["classname"] != kw.get("text")]
        return {"status": "done", "detail": kw.get("text", "")}


class LootAreaIgnoresOwnDrops(unittest.TestCase):
    def test_own_drop_is_skipped_and_reported(self):
        with tempfile.TemporaryDirectory() as tmp:
            ledger = os.path.join(tmp, "dropped_items.json")
            tactics.remember_drop("Mag_AKM_30Rnd", 1000.0, 1000.0, path=ledger)
            state = {"npc": {"alive": True, "pos_x": 1000.0, "pos_z": 1000.0},
                     "inventory": [],
                     "nearby": [{"kind": "item", "classname": "Mag_AKM_30Rnd",
                                 "distance": 3, "x": 1002.0, "z": 1000.0},
                                {"kind": "item", "classname": "BandageDressing",
                                 "distance": 6, "x": 1006.0, "z": 1000.0}]}
            bridge = LootBridge(state)
            with mock.patch.object(tactics, "DROP_LEDGER", ledger), \
                    mock.patch.object(tactics.time, "sleep", lambda s: None):
                result = tactics.loot_area(bridge, max_items=4, time_budget=10,
                                           log=lambda m: None)
            self.assertEqual(result["haul"], ["BandageDressing"])
            self.assertEqual(result["ignored"], ["Mag_AKM_30Rnd"])
            self.assertEqual([r for r in bridge.runs if r[0] == "pickup"],
                             [("pickup", "BandageDressing")])


def _plan_target(plan):
    params = plan.get("params") or {}
    return params.get("text") or (plan.get("loot_target") or {}).get("classname") or ""


class SurvivalIgnoresOwnDrops(unittest.TestCase):
    @staticmethod
    def state():
        npc = {"spawned": True, "alive": True, "name": "Viktor", "health": 100,
               "blood": 5000, "water": 4000, "energy": 5000, "stomach_volume": 0,
               "heat_comfort": 0, "pos_x": 1000, "pos_z": 1000, "in_hands": "",
               "bleeding": False, "disease": {"agents": {}, "sick": False}}
        ammo = {"classname": "Ammo_762x39", "kind": "item", "distance": 4,
                "x": 1000, "z": 1004, "quantity": 20, "health": 100}
        return {"seq": 1, "uptime": 100, "bridge_version": "0.9.0", "npc": npc,
                "inventory": [], "nearby": [ammo],
                "command": {"id": "old", "status": "idle"}}

    def test_pickup_plan_disappears_for_own_drop(self):
        with tempfile.TemporaryDirectory() as tmp:
            ledger = os.path.join(tmp, "dropped_items.json")
            with mock.patch.object(tactics, "DROP_LEDGER", ledger):
                memory = survival._new_memory()
                plan = survival.choose_action(self.state(), memory, time.time(),
                                              allow_explore=False)
                self.assertEqual(_plan_target(plan), "Ammo_762x39")
                tactics.remember_drop("Ammo_762x39", 1000.0, 1000.0, path=ledger)
                plan = survival.choose_action(self.state(), memory, time.time(),
                                              allow_explore=False)
                self.assertNotEqual(_plan_target(plan), "Ammo_762x39")


class BridgeUrgentInterrupt(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.bridge = bridge_mod.Bridge(self.tmp.name, "t")
        os.makedirs(self.bridge.dir, exist_ok=True)
        with open(self.bridge.state_file, "w", encoding="utf-8") as f:
            json.dump({"command": {"id": "c1", "status": "running"}}, f)
        self.flag = os.path.join(self.tmp.name, "urgent_event.json")

    def tearDown(self):
        self.tmp.cleanup()

    def test_flag_written_during_wait_interrupts(self):
        out = {}
        worker = threading.Thread(target=lambda: out.update(
            self.bridge.wait_status("c1", timeout=8, urgent_flag=self.flag)))
        worker.start()
        time.sleep(1.3)
        with open(self.flag, "w", encoding="utf-8") as f:
            json.dump({"event": "DU BLUTEST! Verbinde dich", "t": time.time()}, f)
        worker.join(timeout=8)
        self.assertEqual(out.get("status"), "interrupted")
        self.assertTrue(str(out.get("detail")).startswith("kritisch: DU BLUTEST"))

    def test_stale_flag_does_not_interrupt(self):
        with open(self.flag, "w", encoding="utf-8") as f:
            json.dump({"event": "GEFAHR: alt", "t": 0}, f)
        time.sleep(0.05)
        out = self.bridge.wait_status("c1", timeout=2.2, urgent_flag=self.flag)
        self.assertEqual(out.get("status"), "running")

    def test_interrupt_reason_distinguishes_sources(self):
        critical = {"status": "interrupted", "detail": "kritisch: DU BLUTEST!"}
        radio = {"status": "interrupted", "detail": "neuer Funk"}
        self.assertIn("kritisches Ereignis: DU BLUTEST", bridge_mod.interrupt_reason(critical))
        self.assertIn("funkt", bridge_mod.interrupt_reason(radio))
        self.assertTrue(bridge_mod.interrupt_text(radio).startswith("ABGEBROCHEN: "))


class RunAgentUrgentContracts(unittest.TestCase):
    def test_abortable_events(self):
        import run_agent
        self.assertTrue(run_agent._is_abortable("DU BLUTEST! Verbinde dich"))
        self.assertTrue(run_agent._is_abortable("GEFAHR: Raubtier (Wolf) nur noch 20 m!"))
        self.assertTrue(run_agent._is_abortable("DU BIST UMGEKIPPT (bewusstlos)."))
        self.assertFalse(run_agent._is_abortable("DU NIMMST SCHADEN: HP 90, Blut 4900."))
        self.assertFalse(run_agent._is_abortable("REISE: angekommen"))
        self.assertTrue(run_agent._is_critical("DU BLUTEST und hast KEIN Verbandsmaterial!"))

    def test_write_urgent_event_is_json(self):
        import run_agent
        with tempfile.TemporaryDirectory() as tmp:
            path = os.path.join(tmp, "urgent_event.json")
            self.assertTrue(run_agent.write_urgent_event(path, "DU BLUTEST!"))
            with open(path, encoding="utf-8") as f:
                self.assertEqual(json.load(f)["event"], "DU BLUTEST!")


class EquipCloneCheck(unittest.TestCase):
    class CloneBridge:
        def __init__(self, hands):
            self.hands, self.runs = hands, 0

        def read_state(self):
            return {"npc": {"in_hands": self.hands},
                    "inventory": [{"classname": "KitchenKnife", "health": 100}]}

        def run(self, action, **kw):
            self.runs += 1
            return {"status": "failed", "detail": "Equip-Ziel verschwunden"}

    def test_clone_in_hands_counts_as_success(self):
        bridge = self.CloneBridge("KitchenKnife")
        with mock.patch.object(tactics.time, "sleep", lambda s: None):
            result = tactics.equip_by_classname(bridge, "KitchenKnife")
        self.assertEqual(result["status"], "done")
        self.assertEqual(bridge.runs, 1)

    def test_missing_weapon_retries_once(self):
        bridge = self.CloneBridge("")
        with mock.patch.object(tactics.time, "sleep", lambda s: None):
            result = tactics.equip_by_classname(bridge, "KitchenKnife")
        self.assertEqual(result["status"], "failed")
        self.assertEqual(bridge.runs, 2)


if __name__ == "__main__":
    unittest.main()
