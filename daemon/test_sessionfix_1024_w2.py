#!/usr/bin/env python3
"""Tests Sessionfix 24.10.2026, Paket W2 (dayz_mcp.py, bridge.py, tactics.py).

Kein Server nötig. Abgedeckt:
  * C1  Tod-Sperre in Bridge.send/wait_status, observe/wait im toten Gehirn.
  * C2  bridge.squad_lines (Format, Sortierung, Alter, Fehlerfälle).
  * C3  engage(mode=auto|melee): Fahrzeug, Nahkampf-Ziehen, Detail-Zerlegung
        (SCHUSS/HAND/ESK, altes Format), Nachladen nach 3 s.
  * C6  wait_status akzeptiert side_command; BRIDGE-1 verdrängte Befehle.
  * C8  Fahrzeug-Sperre equip_best (vor dem Circuit-Breaker), equip_melee.
  * VOICE-1 Funktext in der Unterbrechung, VI-12/RESP-5 wait-Gründe/Anker,
    VI-10 Chat-Alter/Adressat, MVS-M1 Munitionsanzeige, MISS-1/VI-M5
    Positionsangaben, RESP-9 Reise-Hinweis, MOD-7 verzögerter Drop,
    MOD-5/TOOL-2 Waffen-/Loot-Bewertung, HUD-5 native Absicht.

Start: python -m pytest -q test_sessionfix_1024_w2.py
"""

import copy
import json
import math
import os
import shutil
import sys
import tempfile
import threading
import time
import unittest
from unittest import mock

DAEMON_DIR = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, DAEMON_DIR)

import bridge as bridge_mod  # noqa: E402
import tactics  # noqa: E402

_TMP = tempfile.mkdtemp(prefix="w2_sessionfix_")
_saved_argv = sys.argv
sys.argv = ["dayz_mcp.py", "--profile", _TMP, "--npc-id", "w2test",
            "--agent-name", "Birgit",
            "--outbox", os.path.join(_TMP, "voice_outbox.jsonl")]
import dayz_mcp  # noqa: E402
sys.argv = _saved_argv

_ORIG_BRIDGE = dayz_mcp.BRIDGE


def tearDownModule():
    dayz_mcp.BRIDGE = _ORIG_BRIDGE
    shutil.rmtree(_TMP, ignore_errors=True)


def _npc(**kw):
    npc = {"spawned": True, "alive": True, "name": "Birgit", "health": 100,
           "pos_x": 1000.0, "pos_z": 1000.0, "in_hands": ""}
    npc.update(kw)
    return npc


class FakeBridge:
    """Bridge-Attrappe für dayz_mcp/tactics: State per Kopie, Ergebnisse je
    Aktion (dict oder Liste von dicts), optionaler Hook pro Aktion."""

    def __init__(self, state=None, results=None, hooks=None):
        self.state = state or {}
        self.results = results or {}
        self.hooks = hooks or {}
        self.runs = []
        self.npc_id = "w2test"
        self.dir = os.path.join(_TMP, "IsuSurvivor")
        self.voice_inbox = None
        self.urgent_flag = None

    def read_state(self, *a, **k):
        return copy.deepcopy(self.state)

    def run(self, action, timeout=0, interruptible=False, stop_event=None, **kw):
        self.runs.append(dict(kw, action=action, timeout=timeout,
                              interruptible=interruptible))
        hook = self.hooks.get(action)
        if hook:
            hook(self, kw)
        r = self.results.get(action, {"status": "done", "detail": ""})
        if isinstance(r, list):
            r = r.pop(0) if r else {"status": "done", "detail": ""}
        return dict(r)

    def actions(self):
        return [r["action"] for r in self.runs]


class McpCase(unittest.TestCase):
    def setUp(self):
        self._saved_bridge = dayz_mcp.BRIDGE
        dayz_mcp._EQUIP_FAILS = 0
        dayz_mcp._EQUIP_LAST_FAIL = 0.0
        dayz_mcp._LAST_EQUIP.update(t=0.0, ok=True, detail="")
        dayz_mcp._WAIT_ANCHOR = None
        dayz_mcp._WAIT_SINCE = 0.0
        dayz_mcp._WAIT_LAST = 0.0
        self.sleeps = []
        # dayz_mcp.time und tactics.time sind DASSELBE Modul - ein Patch reicht
        p1 = mock.patch.object(dayz_mcp.time, "sleep", lambda s: self.sleeps.append(s))
        # Kameraden-Zeile aus der echten Datei des Repos nie in Tests ziehen
        p3 = mock.patch.object(bridge_mod, "SQUAD_STATE_FILE",
                               os.path.join(_TMP, "no_squad_state.json"))
        for p in (p1, p3):
            p.start()
            self.addCleanup(p.stop)

    def tearDown(self):
        dayz_mcp.BRIDGE = self._saved_bridge


# ===================================================================== C1
class DeathLock(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.b = bridge_mod.Bridge(self.tmp.name, "t")
        os.makedirs(self.b.dir, exist_ok=True)
        self.epoch = os.path.join(self.tmp.name, "death_epoch.txt")

    def tearDown(self):
        self.tmp.cleanup()

    def _write_epoch(self, value):
        with open(self.epoch, "w", encoding="utf-8") as f:
            f.write(f"{value:.3f}")

    def test_send_raises_after_death_and_writes_nothing(self):
        self.b.death_epoch_file = self.epoch
        self.b.session_start = 100.0
        self._write_epoch(150.0)
        with self.assertRaises(RuntimeError) as cm:
            self.b.send("say", text="hallo")
        self.assertEqual(str(cm.exception), bridge_mod.DEATH_TEXT)
        self.assertTrue(str(cm.exception).startswith("DU BIST GESTORBEN - dieses Gehirn"))
        self.assertFalse(os.path.exists(self.b.cmd_file))

    def test_older_epoch_and_runner_bridge_are_free(self):
        self.b.death_epoch_file = self.epoch
        self.b.session_start = 200.0
        self._write_epoch(150.0)      # Tod VOR dem Start dieses Gehirns
        self.assertTrue(self.b.send("say", text="a"))
        os.remove(self.b.cmd_file)
        runner = bridge_mod.Bridge(self.tmp.name, "t")   # ohne death_epoch_file
        self._write_epoch(time.time() + 10)
        self.assertFalse(runner.died_since_start())
        self.assertTrue(runner.send("say", text="b"))

    def test_missing_or_broken_file_is_free(self):
        self.b.death_epoch_file = self.epoch
        self.b.session_start = 1.0
        self.assertFalse(self.b.died_since_start())
        with open(self.epoch, "w", encoding="utf-8") as f:
            f.write("kaputt")
        self.assertFalse(self.b.died_since_start())

    def test_wait_status_ends_on_death(self):
        with open(self.b.state_file, "w", encoding="utf-8") as f:
            json.dump({"command": {"id": "c1", "status": "running"}}, f)
        self.b.death_epoch_file = self.epoch
        self.b.session_start = 100.0
        self._write_epoch(150.0)
        out = self.b.wait_status("c1", timeout=3)
        self.assertEqual(out.get("status"), "failed")
        self.assertEqual(out.get("detail"), bridge_mod.DEATH_TEXT)


class DeathLockMcp(McpCase):
    def test_observe_and_wait_report_death(self):
        b = FakeBridge({"npc": _npc()})
        b.died_since_start = lambda: True
        dayz_mcp.BRIDGE = b
        self.assertEqual(dayz_mcp.observe(), bridge_mod.DEATH_TEXT)
        self.assertEqual(dayz_mcp.wait(5), bridge_mod.DEATH_TEXT)
        self.assertEqual(b.runs, [])

    def test_mcp_bridge_is_configured(self):
        self.assertTrue(_ORIG_BRIDGE.death_epoch_file.endswith("death_epoch.txt"))
        self.assertEqual(os.path.dirname(_ORIG_BRIDGE.death_epoch_file),
                         os.path.dirname(dayz_mcp.VOICE_OUTBOX))
        self.assertGreater(_ORIG_BRIDGE.session_start, 0.0)


# ===================================================================== C2
class SquadLines(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.path = os.path.join(self.tmp.name, "squad_state.json")
        p = mock.patch.object(bridge_mod, "SQUAD_STATE_FILE", self.path)
        p.start()
        self.addCleanup(p.stop)

    def tearDown(self):
        self.tmp.cleanup()

    def _write(self, agents, t=None):
        with open(self.path, "w", encoding="utf-8") as f:
            json.dump({"t": time.time() if t is None else t, "agents": agents}, f)

    def test_format_sorting_and_states(self):
        self._write([
            {"id": "birgit", "online": True, "name": "Birgit", "spawned": True,
             "alive": True, "x": 2000, "z": 1000, "health": 100},
            {"id": "viktor", "online": True, "name": "Viktor", "spawned": True,
             "alive": True, "x": 422, "z": 1000, "health": 82, "fighting": True},
            {"id": "igor", "online": True, "name": "Igor", "spawned": True,
             "alive": True, "x": 1850, "z": 890, "health": 100},
            {"id": "konrad", "online": True, "name": "Konrad", "spawned": True,
             "alive": False, "x": 0, "z": 0, "health": 0},
            {"id": "hans", "online": False},
        ])
        out = bridge_mod.squad_lines("birgit", 2000.0, 1000.0)
        self.assertEqual(out, "Igor 186 m SW (x1850 z890, HP100); "
                              "Viktor 1578 m W (x422 z1000, HP82, im KAMPF); "
                              "Konrad TOT; Hans nicht im Spiel")

    def test_stale_missing_and_broken_file_give_empty(self):
        self.assertEqual(bridge_mod.squad_lines("birgit", 0, 0), "")
        self._write([{"id": "viktor", "online": True, "name": "Viktor",
                      "spawned": True, "alive": True, "x": 1, "z": 1}],
                    t=time.time() - 30)
        self.assertEqual(bridge_mod.squad_lines("birgit", 0, 0), "")
        self.assertNotEqual(bridge_mod.squad_lines("birgit", 0, 0, max_age=60), "")
        with open(self.path, "w", encoding="utf-8") as f:
            f.write("{kaputt")
        self.assertEqual(bridge_mod.squad_lines("birgit", 0, 0), "")

    def test_observe_appends_live_line(self):
        self._write([{"id": "viktor", "online": True, "name": "Viktor",
                      "spawned": True, "alive": True, "x": 1000, "z": 1300,
                      "health": 90}])
        b = FakeBridge({"npc": _npc(), "inventory": [], "nearby": []})
        saved = dayz_mcp.BRIDGE
        dayz_mcp.BRIDGE = b
        try:
            text = dayz_mcp.observe()
        finally:
            dayz_mcp.BRIDGE = saved
        self.assertIn("KAMERADEN (live): Viktor 300 m N (x1000 z1300, HP90)", text)


# ============================================================ C6/BRIDGE-1
class WaitStatus(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.b = bridge_mod.Bridge(self.tmp.name, "t")

    def tearDown(self):
        self.tmp.cleanup()

    def test_side_command_with_own_id_is_accepted(self):
        states = iter([{"command": {"id": "other", "action": "move_to",
                                    "status": "running"},
                        "side_command": {"id": "c1", "action": "say",
                                         "status": "done", "detail": "ok"}}])
        self.b.read_state = lambda *a, **k: next(states)
        out = self.b.wait_status("c1", timeout=5)
        self.assertEqual(out.get("status"), "done")
        self.assertEqual(out.get("action"), "say")

    def test_displaced_command_returns_interrupted_at_once(self):
        seq = [{"command": {"id": "c1", "action": "move_to", "status": "running"}},
               {"command": {"id": "x9", "action": "say", "status": "running"}}]
        it = iter(seq)
        self.b.read_state = lambda *a, **k: next(it, seq[-1])
        t0 = time.monotonic()
        out = self.b.wait_status("c1", timeout=30)
        self.assertLess(time.monotonic() - t0, 5.0)
        self.assertEqual(out, {"id": "c1", "status": "interrupted",
                               "detail": "verdraengt durch say"})
        reason = bridge_mod.interrupt_reason(out)
        self.assertIn("läuft NICHT weiter", reason)
        self.assertIn("say", reason)
        self.assertNotIn("funkt", reason)

    def test_player_wheel_displacement_is_marked(self):
        seq = [{"command": {"id": "c1", "action": "move_to", "status": "running"}},
               {"command": {"id": "player_goto", "action": "move_to",
                            "status": "running"}}]
        it = iter(seq)
        self.b.read_state = lambda *a, **k: next(it, seq[-1])
        out = self.b.wait_status("c1", timeout=30)
        self.assertEqual(out.get("detail"),
                         "verdraengt durch move_to " + bridge_mod.PLAYER_ORDER_MARK)

    def test_foreign_id_before_own_is_no_displacement(self):
        seq = [{"command": {"id": "old", "action": "say", "status": "done"}},
               {"command": {"id": "c1", "action": "move_to", "status": "done",
                            "detail": "da"}}]
        it = iter(seq)
        self.b.read_state = lambda *a, **k: next(it, seq[-1])
        self.assertEqual(self.b.wait_status("c1", timeout=5).get("status"), "done")


# ================================================================ VOICE-1
class InboxText(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.inbox = os.path.join(self.tmp.name, "voice_inbox.jsonl")
        with open(self.inbox, "w", encoding="utf-8") as f:
            f.write(json.dumps({"user": "Lagezentrum", "text": "alt"}) + "\n")
        self.base = os.path.getsize(self.inbox)

    def tearDown(self):
        self.tmp.cleanup()

    def _append(self, entry):
        with open(self.inbox, "a", encoding="utf-8") as f:
            f.write(json.dumps(entry, ensure_ascii=False) + "\n")

    def test_player_text_is_delivered(self):
        self._append({"user": "Player", "text": "Birgit, komm  her"})
        self.assertEqual(bridge_mod._inbox_interrupt_line(self.inbox, self.base),
                         "Birgit, komm her")
        self.assertIs(bridge_mod._inbox_should_interrupt(self.inbox, self.base), True)
        b = bridge_mod.Bridge(self.tmp.name, "t")
        b.read_state = lambda *a, **k: {"command": {"id": "c1", "status": "running"}}
        out = b.wait_status("c1", timeout=3, interrupt_inbox=self.inbox)
        # base_size wird in wait_status beim Start gemessen -> neue Zeile nötig
        self.assertEqual(out.get("status"), "running")

    def test_wait_status_reports_text(self):
        b = bridge_mod.Bridge(self.tmp.name, "t")
        calls = {"n": 0}

        def state(*a, **k):
            calls["n"] += 1
            if calls["n"] == 1:
                self._append({"user": "Player", "text": "Birgit, komm her"})
            return {"command": {"id": "c1", "status": "running"}}
        b.read_state = state
        out = b.wait_status("c1", timeout=5, interrupt_inbox=self.inbox)
        self.assertEqual(out.get("detail"), "neuer Funk: Birgit, komm her")
        reason = bridge_mod.interrupt_reason(out)
        self.assertIn('Der Spieler funkt: "Birgit, komm her"', reason)
        self.assertIn("EREIGNIS", reason)

    def test_lagezentrum_prio_and_routine(self):
        self._append({"user": "Lagezentrum", "text": "Squad-Lage ...", "prio": False})
        self.assertEqual(bridge_mod._inbox_interrupt_line(self.inbox, self.base), "")
        self.assertIs(bridge_mod._inbox_should_interrupt(self.inbox, self.base), False)
        self._append({"user": "Lagezentrum", "text": "Igor ist gefallen", "prio": True})
        said = bridge_mod._inbox_interrupt_line(self.inbox, self.base)
        self.assertEqual(said, "Lagezentrum: Igor ist gefallen")
        reason = bridge_mod.interrupt_reason({"detail": "neuer Funk: " + said})
        self.assertIn('Lagezentrum funkt dringend: "Igor ist gefallen"', reason)
        self.assertNotIn("Der Spieler", reason)

    def test_broken_line_interrupts_with_generic_text(self):
        with open(self.inbox, "a", encoding="utf-8") as f:
            f.write("{kaputt\n")
        self.assertEqual(bridge_mod._inbox_interrupt_line(self.inbox, self.base), "?")
        self.assertIn("funkt dich gerade an",
                      bridge_mod.interrupt_reason({"detail": "neuer Funk"}))


# ======================================================= format_observation
class Observation(unittest.TestCase):
    def test_ammo_shows_rounds_only_with_new_mod_build(self):
        inv = [{"classname": "Mag_Glock_15Rnd", "kind": "ammo", "quantity": 12},
               {"classname": "Ammo_9x19", "kind": "ammo", "quantity": 20}]
        new, _ = bridge_mod.format_observation(
            {"npc": _npc(shots=3), "inventory": inv, "nearby": []})
        self.assertIn("Mag_Glock_15Rnd [ammo] (12 Schuss im Magazin)", new)
        self.assertIn("Ammo_9x19 [ammo] (20 Schuss)", new)
        old, _ = bridge_mod.format_observation(
            {"npc": _npc(), "inventory": inv, "nearby": []})
        self.assertIn("Mag_Glock_15Rnd [ammo] x12", old)

    def _chat(self, text, msg_up=440.0, now_up=500.0):
        state = {"npc": _npc(), "inventory": [], "nearby": [], "uptime": now_up,
                 "chat": [{"id": 5, "sender": "Viktor", "text": text,
                           "uptime": msg_up}]}
        out, max_id = bridge_mod.format_observation(
            state, 0, own_name="Birgit",
            known_names={"rookie", "viktor", "birgit", "igor"})
        self.assertEqual(max_id, 5)
        return out

    def test_chat_age_and_addressee(self):
        self.assertIn("[Viktor an Rookie (nicht an dich), vor 60 s] Rookie, komme",
                      self._chat("Rookie, komme zu dir, fünfunddreißig Meter."))
        self.assertIn("[Viktor an dich, vor 60 s] Birgit, bleib",
                      self._chat("Birgit, bleib wo du bist."))
        self.assertIn("[Viktor, vor 60 s] Gut, ich komme", self._chat("Gut, ich komme."))
        self.assertIn("[Viktor an Rookie, vor 60 s]",
                      self._chat("Rookie, Birgit, kommt her."))
        self.assertIn("[Viktor, vor 0 s]", self._chat("Gut.", msg_up=900.0))

    def test_chat_without_names_keeps_old_tag(self):
        state = {"npc": _npc(), "inventory": [], "nearby": [],
                 "chat": [{"id": 2, "sender": "Viktor", "text": "Rookie, hallo"}]}
        out, _ = bridge_mod.format_observation(state, 0)
        self.assertIn("  [Viktor] Rookie, hallo", out)

    def test_chat_ids_reset_after_server_restart(self):
        state = {"npc": _npc(), "inventory": [], "nearby": [],
                 "chat": [{"id": 1, "sender": "Igor", "text": "eins"},
                          {"id": 2, "sender": "Igor", "text": "zwei"}]}
        out, max_id = bridge_mod.format_observation(state, 50)
        self.assertIn("eins", out)
        self.assertIn("zwei", out)
        self.assertEqual(max_id, 2)

    def test_initial_chat_id(self):
        self.assertEqual(dayz_mcp._max_chat_id({"chat": [{"id": 3}, {"id": 7}]}), 7)
        self.assertEqual(dayz_mcp._max_chat_id(None), 0)
        self.assertEqual(dayz_mcp._max_chat_id({"chat": "kaputt"}), 0)


# ================================================================== tactics
class WeaponTables(unittest.TestCase):
    def test_scores(self):
        self.assertEqual(tactics.classify_weapon("P1"), 24)
        self.assertEqual(tactics.classify_weapon("Mp133Shotgun"), 50)
        self.assertEqual(tactics.classify_weapon("PP19"), 54)
        self.assertEqual(tactics.classify_weapon("Izh43Shotgun"), 42)
        self.assertEqual(tactics.classify_weapon("Ruger1022"), 25)
        self.assertEqual(tactics.classify_weapon("Izh18Shotgun"), 40)
        self.assertEqual(tactics.classify_weapon("Izh18"), 30)
        self.assertEqual(tactics.classify_weapon("Mag_P1_8Rnd"), 0)
        self.assertEqual(tactics.classify_melee("Kukri"), 30)

    def test_mag_fits_weapon(self):
        fits = tactics.mag_fits_weapon
        self.assertTrue(fits("Mag_Glock_15Rnd", "Glock19"))
        self.assertTrue(fits("Mag_IJ70_8Rnd", "MakarovIJ70"))
        self.assertTrue(fits("Mag_MP5_30Rnd", "MP5K"))
        self.assertTrue(fits("Mag_STANAG_30Rnd", "M4A1"))
        self.assertTrue(fits("Mag_AKM_30Rnd", "AKM"))
        self.assertTrue(fits("Mag_AK74_30Rnd", "AKS74U"))
        self.assertTrue(fits("Mag_P1_8Rnd", "P1"))
        self.assertFalse(fits("Mag_P1_8Rnd", "Mp133Shotgun"))
        self.assertFalse(fits("Mag_AKM_30Rnd", "AK74"))
        self.assertFalse(fits("Ammo_9x19", "Glock19"))


class GroundScoring(unittest.TestCase):
    def test_clothing_only_as_handover(self):
        cloth = {"classname": "TShirt_Grey", "item_kind": "clothing", "distance": 4.0}
        self.assertEqual(tactics.score_ground_item(cloth, []), 0)
        self.assertEqual(tactics.score_ground_item(cloth, [], human_near=True), 30)
        far = dict(cloth, distance=8.0)
        self.assertEqual(tactics.score_ground_item(far, [], human_near=True), 0)

    def test_magazines_need_a_matching_gun(self):
        mag = {"classname": "Mag_Glock_15Rnd", "distance": 3.0}
        self.assertEqual(tactics.score_ground_item(mag, [{"classname": "Glock19"}]), 80)
        self.assertEqual(tactics.score_ground_item(mag, [{"classname": "Izh43Shotgun"}]), 10)
        ammo = {"classname": "Ammo_9x19", "distance": 3.0}
        self.assertEqual(tactics.score_ground_item(ammo, []), 80)

    def test_loot_area_passes_human_near(self):
        class LootBridge:
            def __init__(self, nearby):
                self.state = {"npc": {"alive": True, "pos_x": 0.0, "pos_z": 0.0},
                              "inventory": [], "nearby": nearby}
                self.runs = []

            def read_state(self):
                return copy.deepcopy(self.state)

            def run(self, action, **kw):
                self.runs.append((action, kw.get("text", "")))
                self.state["nearby"] = [e for e in self.state["nearby"]
                                        if e["classname"] != kw.get("text")]
                return {"status": "done", "detail": kw.get("text", "")}

        cloth = {"kind": "item", "classname": "TShirt_Grey", "item_kind": "clothing",
                 "distance": 3.0, "x": 3.0, "z": 0.0}
        human = {"kind": "player", "classname": "SurvivorF", "name": "Rookie",
                 "distance": 6.0, "x": 6.0, "z": 0.0}
        with tempfile.TemporaryDirectory() as tmp, \
                mock.patch.object(tactics, "DROP_LEDGER", os.path.join(tmp, "d.json")), \
                mock.patch.object(tactics.time, "sleep", lambda s: None):
            alone = LootBridge([cloth])
            self.assertEqual(tactics.loot_area(alone, time_budget=5,
                                               log=lambda m: None)["haul"], [])
            with_human = LootBridge([cloth, human])
            self.assertEqual(tactics.loot_area(with_human, time_budget=5,
                                               log=lambda m: None)["haul"],
                             ["TShirt_Grey"])

    def test_equip_melee_in_vehicle(self):
        b = FakeBridge({"npc": _npc(in_vehicle=True),
                        "inventory": [{"classname": "SteakKnife"}]})
        self.assertEqual(tactics.equip_melee(b, log=lambda m: None),
                         tactics.VEHICLE_BLOCK_TEXT)
        self.assertEqual(b.runs, [])


# =================================================================== engage
class Engage(McpCase):
    def _make(self, in_hands="Izh43Shotgun", qty=2, engage_detail="Ziel eliminiert",
                reload=None, equip=None, **npc):
        inv = [{"classname": "Izh43Shotgun", "kind": "firearm", "quantity": qty,
                "in_hands": in_hands == "Izh43Shotgun"},
               {"classname": "SteakKnife", "kind": "other", "quantity": 1,
                "in_hands": in_hands == "SteakKnife"}]
        results = {"engage": {"status": "done", "detail": engage_detail},
                   "reload": reload or {"status": "failed",
                                        "detail": "nichts umgeladen"}}
        if equip is not None:
            results["equip"] = equip
            # engage(mode="melee") zieht über equip_melee_lock (Lock beim Ziehen)
            results["equip_melee_lock"] = equip
        b = FakeBridge({"npc": _npc(in_hands=in_hands, **npc), "inventory": inv},
                       results)
        dayz_mcp.BRIDGE = b
        return b

    def test_vehicle_blocks_without_command(self):
        b = self._make(in_vehicle=True)
        self.assertEqual(dayz_mcp.engage(), tactics.VEHICLE_BLOCK_TEXT)
        self.assertEqual(dayz_mcp.engage(mode="melee"), tactics.VEHICLE_BLOCK_TEXT)
        self.assertEqual(b.runs, [])

    def test_melee_draws_knife_then_engages_with_lock(self):
        b = self._make(engage_detail="Ziel eliminiert|SCHUSS=|HAND=SteakKnife|ESK=",
                         equip={"status": "done", "detail": "SteakKnife"})
        out = dayz_mcp.engage(mode="melee")
        self.assertEqual(b.actions(), ["equip_melee_lock", "engage", "reload"])
        self.assertEqual(b.runs[0]["text"], "SteakKnife")
        self.assertEqual(b.runs[1]["text"], "melee")
        self.assertTrue(out.startswith("Kampf beendet: Ziel tot. Von dir kein "
                                       "Schuss (leise). In der Hand: SteakKnife."))
        self.assertIn(3.0, self.sleeps)

    def test_melee_equip_failure_starts_no_fight(self):
        b = self._make(equip={"status": "failed",
                                "detail": "Waffe liess sich nicht in die Hand nehmen"})
        out = dayz_mcp.engage(mode="melee")
        self.assertTrue(out.startswith("ABGEBROCHEN: Nahkampfwaffe ließ sich nicht ziehen"))
        self.assertNotIn("engage", b.actions())
        self.assertFalse(dayz_mcp._LAST_EQUIP["ok"])

    def test_auto_reports_shots_and_reload(self):
        b = self._make(
            engage_detail="Ziel eliminiert|SCHUSS=Izh43Shotgun|HAND=Izh43Shotgun|ESK=",
            reload={"status": "done", "detail": "Izh43Shotgun: 2 Schuss in die Kammer"})
        out = dayz_mcp.engage()
        self.assertEqual(b.runs[0]["action"], "engage")
        self.assertEqual(b.runs[0].get("text", ""), "")
        self.assertTrue(out.startswith("Kampf beendet: Ziel tot (wer es getötet hat, "
                                       "ist nicht geprüft;"))
        self.assertIn("GESCHOSSEN mit: Izh43Shotgun. Das war LAUT", out)
        self.assertIn("Nach dem Kampf nachgeladen (leere Kammern/Magazinplätze "
                      "aufgefüllt): Izh43Shotgun: 2 Schuss in die Kammer", out)
        self.assertNotIn(" Nachgeladen: ", out)
        self.assertIn(3.0, self.sleeps)

    def test_old_mod_format_is_honest(self):
        self._make(engage_detail="Ziel eliminiert")
        out = dayz_mcp.engage()
        self.assertTrue(out.startswith("Kampf beendet: Ziel eliminiert."))
        self.assertIn("Ob du geschossen hast, ist unbekannt - behaupte nicht, "
                      "es sei leise gewesen.", out)

    def test_escalation_is_reported(self):
        self._make(engage_detail="Ziel eliminiert|SCHUSS=Izh43Shotgun|"
                                   "HAND=Izh43Shotgun|ESK=2 Gegner nah")
        out = dayz_mcp.engage(mode="melee")
        self.assertIn("Nahkampf-Lock wurde aufgehoben (2 Gegner nah)", out)

    def test_auto_warns_after_failed_equip(self):
        self._make()
        dayz_mcp._LAST_EQUIP.update(t=time.time(), ok=False, detail="Hand blockiert?")
        out = dayz_mcp.engage()
        self.assertTrue(out.startswith("Kampf beendet:"))
        self.assertIn(" ACHTUNG: equip war eben gescheitert (Hand blockiert?) - du "
                      "hast mit Izh43Shotgun gekämpft.", out)

    def test_no_enemy_keeps_old_text(self):
        b = self._make()
        b.results["engage"] = {"status": "failed", "detail": "kein Gegner in 100 m"}
        out = dayz_mcp.engage()
        self.assertTrue(out.startswith("Kein Gegner"))
        self.assertNotIn("reload", b.actions())

    def test_split_detail(self):
        self.assertEqual(dayz_mcp._split_engage_detail("Ziel eliminiert"),
                         ("Ziel eliminiert", None))
        self.assertEqual(dayz_mcp._split_engage_detail("A|SCHUSS=x,y|HAND=|ESK=z"),
                         ("A", {"SCHUSS": "x,y", "HAND": "", "ESK": "z"}))


# ================================================================ C8 equip
class EquipVehicle(McpCase):
    def test_equip_best_in_vehicle_skips_breaker(self):
        b = FakeBridge({"npc": _npc(in_vehicle=True)})
        dayz_mcp.BRIDGE = b
        for _ in range(3):
            self.assertEqual(dayz_mcp.equip_best(), tactics.VEHICLE_BLOCK_TEXT)
        self.assertEqual(dayz_mcp._EQUIP_FAILS, 0)
        self.assertEqual(dayz_mcp.equip_melee(), tactics.VEHICLE_BLOCK_TEXT)
        self.assertEqual(dayz_mcp.loot_area(), tactics.VEHICLE_BLOCK_TEXT)
        self.assertEqual(dayz_mcp.explore_step(), tactics.VEHICLE_BLOCK_TEXT)
        self.assertEqual(b.runs, [])

    def test_equip_melee_success_hints_engage_melee(self):
        b = FakeBridge({"npc": _npc(in_hands="Izh43Shotgun"),
                        "inventory": [{"classname": "SteakKnife"}]},
                       {"equip": {"status": "done", "detail": "SteakKnife"}})
        dayz_mcp.BRIDGE = b
        out = dayz_mcp.equip_melee()
        self.assertTrue(out.startswith("Nahkampfwaffe gezogen: SteakKnife"))
        self.assertIn('engage(mode="melee")', out)
        self.assertTrue(dayz_mcp._LAST_EQUIP["ok"])


# ===================================================== regroup/travel/follow
class Movement(McpCase):
    def test_regroup_is_short_and_interruptible(self):
        b = FakeBridge({"npc": _npc()},
                       {"regroup": {"status": "done", "detail": "auf dem Weg zu Rookie"}})
        dayz_mcp.BRIDGE = b
        out = dayz_mcp.regroup()
        self.assertEqual(b.runs[-1]["timeout"], 50)
        self.assertTrue(b.runs[-1]["interruptible"])
        self.assertEqual(out, "Wieder bei der Gruppe. bei Rookie")

    def test_running_outcome_has_own_position(self):
        dayz_mcp.BRIDGE = FakeBridge({"npc": _npc(pos_x=10425.4, pos_z=11905.2)})
        out = dayz_mcp._outcome({"status": "running", "dist_to_target": 672.0}, "x")
        self.assertEqual(out, "Laeuft noch (Distanz 672 m, du bist jetzt bei "
                              "x=10425 z=11905). Mit observe() prüfen oder wait() nutzen.")

    def test_travel_start_message_and_abort_hint(self):
        started = threading.Event()

        def fake_worker(gx, gz, stop):
            started.set()
            stop.wait(10)

        b = FakeBridge({"npc": _npc(pos_x=10391.0, pos_z=11938.0)},
                       {"follow": {"status": "done", "detail": "Rookie"}})
        dayz_mcp.BRIDGE = b
        with mock.patch.object(dayz_mcp, "_travel_worker", fake_worker):
            out = dayz_mcp.travel_to(11088, 12112)
            self.assertTrue(started.wait(2))
            self.assertTrue(out.startswith("Reise gestartet: 718 m Richtung O nach "
                                           "x=11088 z=12112 (du stehst bei x=10391 "
                                           "z=11938)"))
            res = dayz_mcp.follow()
        self.assertIn("Hinweis: deine Reise nach x=11088 z=12112 ist damit beendet.", res)
        self.assertIsNone(dayz_mcp._abort_travel())

    def test_engage_mentions_interrupted_travel(self):
        def fake_worker(gx, gz, stop):
            stop.wait(10)

        b = FakeBridge({"npc": _npc(in_hands="SteakKnife"),
                        "inventory": [{"classname": "SteakKnife", "in_hands": True}]},
                       {"engage": {"status": "done", "detail": "Ziel eliminiert"}})
        dayz_mcp.BRIDGE = b
        with mock.patch.object(dayz_mcp, "_travel_worker", fake_worker):
            dayz_mcp.travel_to(2000, 1000)
            out = dayz_mcp.engage()
        self.assertIn("Deine Reise nach x=2000 z=1000 ist unterbrochen - bei Bedarf "
                      "travel_to erneut.", out)

    def test_displaced_segment_is_resent_not_counted_as_block(self):
        class TravelBridge(FakeBridge):
            def __init__(self):
                super().__init__({"npc": _npc(pos_x=0.0, pos_z=0.0),
                                  "command": {"status": "done"}})
                self.moves = 0

            def run(self, action, timeout=0, interruptible=False, stop_event=None, **kw):
                self.runs.append(dict(kw, action=action))
                if action == "move_to":
                    self.moves += 1
                    if self.moves == 1:
                        return {"status": "interrupted",
                                "detail": "verdraengt durch say"}
                    self.state["npc"]["pos_x"] = kw["x"]
                    self.state["npc"]["pos_z"] = kw["z"]
                return {"status": "done", "detail": ""}

        b = TravelBridge()
        os.makedirs(b.dir, exist_ok=True)
        dayz_mcp.BRIDGE = b
        dayz_mcp._travel_body(100.0, 0.0, threading.Event())
        self.assertEqual(b.actions(), ["move_to", "move_to"])
        with open(os.path.join(b.dir, "travel_event_w2test.json"), encoding="utf-8") as f:
            self.assertEqual(json.load(f)["event"], "arrived")


class TravelPlayerWheel(McpCase):
    def test_player_wheel_ends_travel_without_resend(self):
        b = FakeBridge({"npc": _npc(pos_x=0.0, pos_z=0.0),
                        "command": {"status": "running"}},
                       {"move_to": {"status": "interrupted",
                                    "detail": "verdraengt durch move_to "
                                              + bridge_mod.PLAYER_ORDER_MARK}})
        os.makedirs(b.dir, exist_ok=True)
        dayz_mcp.BRIDGE = b
        stop = threading.Event()
        dayz_mcp._travel_body(500.0, 0.0, stop)
        self.assertEqual(b.actions(), ["move_to"])
        self.assertTrue(stop.is_set())
        with open(os.path.join(b.dir, "travel_event_w2test.json"), encoding="utf-8") as f:
            ev = json.load(f)
        self.assertEqual(ev["event"], "aborted")
        self.assertIn("Befehlsrad", ev["detail"])
        self.assertIn("Vorrang", ev["detail"])
        self.assertIn("NICHT neu starten", ev["detail"])

    def test_player_wheel_reason_forbids_restart(self):
        reason = bridge_mod.interrupt_reason(
            {"status": "interrupted",
             "detail": "verdraengt durch move_to " + bridge_mod.PLAYER_ORDER_MARK})
        self.assertTrue(reason.startswith("Der Spieler hat dir per Befehlsrad einen "
                                          "neuen Befehl gegeben (move_to) - der hat "
                                          "Vorrang"))
        self.assertIn("Starte ihn NICHT neu, außer der Spieler bittet darum.", reason)
        self.assertNotIn("bei Bedarf neu", reason)
        self.assertNotIn(bridge_mod.PLAYER_ORDER_MARK, reason)
        # Fremder Befehl ohne Befehlsrad-Marke: weiter der generische Text
        generic = bridge_mod.interrupt_reason(
            {"status": "interrupted", "detail": "verdraengt durch say"})
        self.assertIn("starte ihn bei Bedarf neu", generic)


class TravelAbortWhileWaitingForSlot(McpCase):
    def test_no_stray_move_after_abort_during_slot_wait(self):
        # Segment wird von einem fremden Befehl verdrängt, der Reise-Thread
        # wartet in _wait_slot_free - währenddessen kommt _abort_travel.
        b = FakeBridge({"npc": _npc(pos_x=0.0, pos_z=0.0),
                        "command": {"status": "running"}},
                       {"move_to": {"status": "interrupted",
                                    "detail": "verdraengt durch say"}})
        os.makedirs(b.dir, exist_ok=True)
        ev_path = os.path.join(b.dir, "travel_event_w2test.json")
        if os.path.exists(ev_path):
            os.unlink(ev_path)
        dayz_mcp.BRIDGE = b
        stop = threading.Event()
        real_wait = dayz_mcp._wait_slot_free
        calls = []

        def abort_during_wait(ev, max_wait):
            calls.append(max_wait)
            ev.set()            # _abort_travel aus dem Tool-Thread
            real_wait(ev, max_wait)

        with mock.patch.object(dayz_mcp, "_wait_slot_free", abort_during_wait):
            dayz_mcp._travel_body(500.0, 0.0, stop)
        self.assertEqual(calls, [20.0])
        self.assertEqual(b.actions(), ["move_to"])
        self.assertFalse(os.path.exists(ev_path))


# ====================================================================== wait
class Wait(McpCase):
    def test_hint_after_long_stay_without_humans(self):
        b = FakeBridge({"npc": _npc(pos_x=11088.0, pos_z=12112.0),
                        "inventory": [], "nearby": []})
        dayz_mcp.BRIDGE = b
        dayz_mcp._WAIT_ANCHOR = (11090.0, 12100.0)
        dayz_mcp._WAIT_SINCE = time.monotonic() - 300
        dayz_mcp._WAIT_LAST = time.monotonic()
        with mock.patch.object(dayz_mcp, "_sleep_interruptible", lambda s: ""), \
                mock.patch.object(bridge_mod, "squad_lines",
                                  lambda *a, **k: "Viktor 1578 m W (x9510 z12150, HP82)"):
            out = dayz_mcp.wait(30)
        self.assertTrue(out.startswith("30s gewartet. Du hältst dich seit 5 min im "
                                       "Umkreis von 150 m um x11090 z12100 auf. "
                                       "KAMERADEN (live): Viktor 1578 m W"))
        self.assertEqual(out.count("KAMERADEN (live)"), 1)

    def test_no_hint_with_player_or_short_stay(self):
        b = FakeBridge({"npc": _npc(), "inventory": [],
                        "nearby": [{"kind": "player", "name": "Rookie",
                                    "classname": "SurvivorF", "distance": 30,
                                    "x": 1000, "z": 1030}]})
        dayz_mcp.BRIDGE = b
        dayz_mcp._WAIT_ANCHOR = (1000.0, 1000.0)
        dayz_mcp._WAIT_SINCE = time.monotonic() - 600
        dayz_mcp._WAIT_LAST = time.monotonic()
        with mock.patch.object(dayz_mcp, "_sleep_interruptible", lambda s: ""):
            self.assertNotIn("Du hältst dich", dayz_mcp.wait(10))
            b.state["nearby"] = []
            dayz_mcp._WAIT_ANCHOR = None
            self.assertNotIn("Du hältst dich", dayz_mcp.wait(10))
        self.assertEqual(dayz_mcp._WAIT_ANCHOR, (1000.0, 1000.0))

    def test_anchor_moves_only_outside_150_m(self):
        dayz_mcp._WAIT_ANCHOR = (0.0, 0.0)
        dayz_mcp._WAIT_SINCE = 5.0
        dayz_mcp._WAIT_LAST = time.monotonic()
        dayz_mcp._wait_anchor_update({"npc": _npc(pos_x=120.0, pos_z=0.0)})
        self.assertEqual(dayz_mcp._WAIT_ANCHOR, (0.0, 0.0))
        dayz_mcp._wait_anchor_update({"npc": _npc(pos_x=160.0, pos_z=0.0)})
        self.assertEqual(dayz_mcp._WAIT_ANCHOR, (160.0, 0.0))
        self.assertGreater(dayz_mcp._WAIT_SINCE, 5.0)

    def test_critical_and_radio_reasons(self):
        dayz_mcp.BRIDGE = FakeBridge({"npc": _npc(), "inventory": [], "nearby": []})
        with mock.patch.object(dayz_mcp, "_sleep_interruptible",
                               lambda s: "kritisch: GEFAHR: 3 Gegner dicht beieinander"):
            out = dayz_mcp.wait(10)
        self.assertTrue(out.startswith("Warten ABGEBROCHEN: kritisches Ereignis: GEFAHR"))
        self.assertNotIn("Der Spieler funkt", out)
        with mock.patch.object(dayz_mcp, "_sleep_interruptible",
                               lambda s: "funk: Birgit, komm her"):
            out = dayz_mcp.wait(10)
        self.assertIn('Der Spieler funkt: "Birgit, komm her"', out)

    def test_sleep_interruptible_returns_reason(self):
        inbox = os.path.join(_TMP, "inbox_sleep.jsonl")
        with open(inbox, "w", encoding="utf-8") as f:
            f.write("\n")
        b = FakeBridge()
        b.voice_inbox = inbox
        b.urgent_flag = os.path.join(_TMP, "urgent_sleep.json")
        dayz_mcp.BRIDGE = b
        with mock.patch.object(bridge_mod, "_inbox_interrupt_line",
                               lambda p, s: "Birgit, komm"):
            self.assertEqual(dayz_mcp._sleep_interruptible(5), "funk: Birgit, komm")
        with mock.patch.object(bridge_mod, "_urgent_event", lambda p, m: "DU BLUTEST"):
            self.assertEqual(dayz_mcp._sleep_interruptible(5), "kritisch: DU BLUTEST")
        with mock.patch.object(dayz_mcp.time, "monotonic",
                               side_effect=[0.0, 0.0, 100.0]):
            self.assertEqual(dayz_mcp._sleep_interruptible(5), "")


# ================================================================ drop/intent
class DropAndIntent(McpCase):
    def test_drop_stuck_message_but_item_gone(self):
        def drop_hook(bridge, kw):
            bridge.state["inventory"] = [i for i in bridge.state["inventory"]
                                         if i["classname"] != "Izh18"]

        b = FakeBridge({"npc": _npc(pos_x=50.0, pos_z=60.0),
                        "inventory": [{"classname": "Izh18"}, {"classname": "Apple"}]},
                       {"drop": {"status": "failed",
                                 "detail": "Izh18 haengt im Inventar fest (Reservierung)"}},
                       {"drop": drop_hook})
        dayz_mcp.BRIDGE = b
        ledger = os.path.join(_TMP, "drops_w2.json")
        with mock.patch.object(tactics, "DROP_LEDGER", ledger):
            out = dayz_mcp.drop(classname="Izh18")
            drops = tactics.load_drops(ledger)
        self.assertTrue(out.startswith("Abgelegt: Izh18 (verzögert"))
        self.assertEqual([d["classname"] for d in drops], ["Izh18"])
        self.assertEqual((drops[0]["x"], drops[0]["z"]), (50.0, 60.0))

    def test_drop_really_stuck_stays_failed(self):
        b = FakeBridge({"npc": _npc(), "inventory": [{"classname": "Izh18"}]},
                       {"drop": {"status": "failed",
                                 "detail": "Izh18 haengt im Inventar fest (Reservierung)"}})
        dayz_mcp.BRIDGE = b
        out = dayz_mcp.drop(classname="Izh18")
        self.assertTrue(out.startswith("Fehlgeschlagen: Izh18 haengt im Inventar fest"))

    def test_intent_always_writes_native_file(self):
        b = FakeBridge()
        dayz_mcp.BRIDGE = b
        os.makedirs(b.dir, exist_ok=True)
        dayz_mcp.intent("Folge Igor zu Birgit.")
        npath = os.path.join(b.dir, "intent_native_w2test.txt")
        with open(npath, encoding="utf-8") as f:
            self.assertEqual(f.read().strip(), "Folge Igor zu Birgit.")
        dayz_mcp._write_intent_line("Unterwegs nach 1/2, noch 300 m")
        with open(npath, encoding="utf-8") as f:
            self.assertEqual(f.read().strip(), "Unterwegs nach 1/2, noch 300 m")


if __name__ == "__main__":
    unittest.main()
