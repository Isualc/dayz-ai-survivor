"""Offline-Tests für Paket W1 (run_agent.py) der Session-Analyse 24.10.2026.

Keine Spiel-, Server- oder Modellaufrufe: Reader, Bridge und Zeitbasis sind
gemockt. Ausführen aus daemon/: python -m pytest -q test_sessionfix_1024_w1.py
"""
from __future__ import annotations

import io
import json
import os
from pathlib import Path
import tempfile
import time
import types
import unittest
from unittest.mock import Mock, patch

import run_agent


def fake_reader(turn_open=False, last_result_at=0.0, last_activity=0.0):
    return types.SimpleNamespace(turn_open=turn_open, last_result_at=last_result_at,
                                 last_activity=last_activity)


class TurnGateTests(unittest.TestCase):
    """VI-1/RUN-1/RESP-4: Zug-Erkennung statt pending-Zähler."""

    def test_turn_end_becomes_idle_after_quiet(self):
        gate = run_agent.TurnGate()
        reader = fake_reader(last_activity=100.0)
        gate.note_send(reader, now=100.0)
        reader.turn_open = True
        reader.last_activity = 110.0
        self.assertFalse(gate.idle(reader, 2.0, now=130.0), "Zug offen")
        # result um 120 s
        reader.turn_open = False
        reader.last_result_at = 120.0
        reader.last_activity = 120.0
        self.assertFalse(gate.idle(reader, 2.0, now=121.0), "Ruhefenster noch nicht vorbei")
        self.assertTrue(gate.idle(reader, 2.0, now=122.5))
        self.assertFalse(gate.idle(reader, run_agent.IDLE_QUIET_ROTATE, now=130.0))
        self.assertTrue(gate.idle(reader, run_agent.IDLE_QUIET_ROTATE, now=136.0))

    def test_trailing_follow_up_turn_is_not_idle(self):
        # Eine kurz vor dem Zugende geschriebene Nachricht startet als eigener
        # Folgezug: nach dem ersten result kommt wieder Ausgabe.
        gate = run_agent.TurnGate()
        reader = fake_reader(last_activity=100.0)
        gate.note_send(reader, now=100.0)
        reader.turn_open = True
        gate.note_send(reader, now=118.0)            # Mid-Turn-Nachricht
        reader.turn_open = False
        reader.last_result_at = reader.last_activity = 120.0
        reader.turn_open = True                      # Folgezug: erstes Event
        reader.last_activity = 123.0
        self.assertFalse(gate.idle(reader, 2.0, now=126.0))
        self.assertFalse(gate.idle(reader, run_agent.IDLE_QUIET_ROTATE, now=200.0))

    def test_send_after_result_is_not_idle_even_when_quiet(self):
        gate = run_agent.TurnGate()
        reader = fake_reader(last_result_at=50.0, last_activity=50.0)
        gate.note_send(reader, now=60.0)
        self.assertFalse(gate.idle(reader, 2.0, now=80.0),
                         "result liegt VOR der Sendung - das Gehirn hat noch nicht geantwortet")

    def test_mid_turn_urgent_allowed_routine_not(self):
        gate = run_agent.TurnGate()
        reader = fake_reader(last_activity=100.0)
        gate.note_send(reader, now=100.0)
        reader.turn_open = True
        reader.last_activity = 104.0
        self.assertFalse(gate.may_deliver(reader, urgent=False, rotate_pending=False, now=115.0))
        self.assertFalse(gate.may_deliver(reader, urgent=True, rotate_pending=False, now=105.0),
                         "12-s-Bremse seit der letzten Sendung")
        self.assertTrue(gate.may_deliver(reader, urgent=True, rotate_pending=False, now=115.0))
        mid = gate.note_send(reader, now=115.0)
        self.assertTrue(mid)
        self.assertFalse(gate.may_deliver(reader, urgent=True, rotate_pending=False, now=120.0))
        self.assertTrue(gate.may_deliver(reader, urgent=True, rotate_pending=False, now=128.0))

    def test_session_opening_message_does_not_start_brake(self):
        # Start/Handover/Respawn: eine Gefahr 2 s nach dem Start muss in den
        # (oft langen) ersten Zug, nicht erst nach dessen Ende.
        gate = run_agent.TurnGate()
        reader = fake_reader(last_activity=1000.0)
        gate.reset(now=1000.0)
        gate.note_send(reader, now=1000.0, brake=False)
        reader.turn_open = True
        reader.last_activity = 1001.0
        self.assertTrue(gate.may_deliver(reader, urgent=True, rotate_pending=False, now=1002.0))
        self.assertFalse(gate.may_deliver(reader, urgent=False, rotate_pending=False, now=1002.0))
        gate.note_send(reader, now=1002.0)           # Mid-Turn-Dispatch startet die Bremse
        self.assertFalse(gate.may_deliver(reader, urgent=True, rotate_pending=False, now=1005.0))

    def test_rotation_pending_blocks_routine_but_not_urgent(self):
        gate = run_agent.TurnGate()
        reader = fake_reader(last_result_at=10.0, last_activity=10.0)
        self.assertTrue(gate.may_deliver(reader, urgent=False, rotate_pending=False, now=50.0))
        self.assertFalse(gate.may_deliver(reader, urgent=False, rotate_pending=True, now=50.0))
        self.assertTrue(gate.may_deliver(reader, urgent=True, rotate_pending=True, now=50.0))

    def test_watchdog_resets_open_turn_after_long_silence(self):
        gate = run_agent.TurnGate()
        reader = fake_reader(last_activity=0.0)
        gate.note_send(reader, now=10.0)
        reader.turn_open = True
        reader.last_activity = 20.0
        self.assertEqual(gate.watchdog(reader, now=200.0), 0.0, "langes Tool ist legal")
        quiet = gate.watchdog(reader, now=20.0 + run_agent.STUCK_QUIET_SEC + 1)
        self.assertGreaterEqual(quiet, run_agent.STUCK_QUIET_SEC)
        self.assertFalse(reader.turn_open)
        self.assertTrue(gate.idle(reader, 0.0, now=400.0))

    def test_native_backend_keeps_exact_counter(self):
        gate = run_agent.TurnGate(native=True)
        reader = fake_reader(last_activity=0.0)
        gate.note_send(reader, now=1.0)
        gate.note_send(reader, now=2.0)
        self.assertFalse(gate.idle(reader, 0.0, now=50.0))
        self.assertEqual(gate.note_result(), "")
        self.assertFalse(gate.idle(reader, 0.0, now=50.0))
        gate.note_result()
        self.assertTrue(gate.idle(reader, 0.0, now=50.0))
        self.assertIn("[SALDO]", gate.note_result())
        self.assertEqual(gate.watchdog(reader, now=10_000.0), 0.0)

    def test_saldo_notes(self):
        gate = run_agent.TurnGate()
        reader = fake_reader()
        gate.note_send(reader, now=1.0)
        gate.note_send(reader, now=2.0)
        self.assertIn("2 Weckrufe", gate.note_result())
        self.assertIn("ohne neue Sendung", gate.note_result())
        gate.note_send(reader, now=3.0)
        self.assertEqual(gate.note_result(), "")


class BrainReaderStreamTests(unittest.TestCase):
    def run_reader(self, events, tracker=None):
        process = Mock(stdout=io.StringIO("".join(json.dumps(e) + "\n" for e in events)))
        journal = Mock()
        reader = run_agent.BrainReader(process, journal, tracker or run_agent.TokenTracker())
        # result-Events schreiben round_cost.txt ins AGENT_HOME - ohne Patch
        # landete die Testzahl im echten agent_home/ des Repos.
        with tempfile.TemporaryDirectory() as folder, \
                patch.object(run_agent, "AGENT_HOME", folder), \
                patch.object(run_agent, "_set_nameplate_intent"):
            reader.run()
        return reader, [c.args[0] for c in journal.log.call_args_list]

    def test_turn_flag_follows_stream_and_tools_are_logged(self):
        events = [
            {"type": "system", "subtype": "init",
             "tools": ["Read", "Write", "Edit", "mcp__dayz__observe", "mcp__dayz__say"]},
            {"type": "assistant", "message": {"content": [{"type": "text", "text": "Ich schaue."}]}},
        ]
        reader, logs = self.run_reader(events)
        self.assertTrue(reader.turn_open)
        self.assertEqual(reader.last_result_at, 0.0)
        tools = [l for l in logs if l.startswith("[TOOLS]")]
        self.assertEqual(len(tools), 1)
        self.assertIn("MCP: 2", tools[0])
        self.assertFalse(any("unerwünschte" in l for l in logs))

    def test_result_closes_turn_and_trailing_system_event_does_not_reopen(self):
        events = [
            {"type": "assistant", "message": {"content": []}},
            {"type": "result", "subtype": "success", "duration_ms": 1000,
             "num_turns": 1, "total_cost_usd": 0.1},
            {"type": "system", "subtype": "status", "status": None},
        ]
        reader, _logs = self.run_reader(events)
        self.assertFalse(reader.turn_open)
        self.assertGreater(reader.last_result_at, 0.0)

    def test_scheduled_wakeup_in_tool_list_is_flagged(self):
        reader, logs = self.run_reader([{"type": "system", "subtype": "init",
                                         "tools": ["Read", "ScheduleWakeup"]}])
        self.assertTrue(any("unerwünschte" in l and "ScheduleWakeup" in l for l in logs))

    def test_cost_baseline_per_session_no_negative_turn_cost(self):
        # DR-8/COST-1: alte Session endet bei 13.72 USD, neue Session startet
        # bei 0 - der erste Zug der neuen Session (0.3962) muss gebucht werden.
        tracker = run_agent.TokenTracker()
        with tempfile.TemporaryDirectory() as folder, \
                patch.object(run_agent, "AGENT_HOME", folder):
            _r1, logs1 = self.run_reader([
                {"type": "result", "total_cost_usd": 13.3224, "duration_ms": 1},
                {"type": "result", "total_cost_usd": 13.7186, "duration_ms": 1}], tracker)
            _r2, logs2 = self.run_reader([
                {"type": "result", "total_cost_usd": 0.3962, "duration_ms": 1}], tracker)
        self.assertAlmostEqual(tracker.round_cost, 13.7186 + 0.3962, places=4)
        zug = [l for l in logs2 if l.startswith("[ZUG ENDE]")][0]
        self.assertIn("+0.3962 USD", zug)
        self.assertNotIn("+-", zug)


class SpawnClaudeToolTests(unittest.TestCase):
    def test_exclusive_builtin_tools_and_extended_disallow_list(self):
        with tempfile.TemporaryDirectory() as folder:
            persona = Path(folder) / "persona.md"
            persona.write_text("Survivor", encoding="utf-8")
            (Path(folder) / "journal").mkdir()
            with patch.object(run_agent, "PERSONA_FILE", str(persona)), \
                    patch.object(run_agent, "AGENT_HOME", folder), \
                    patch.object(run_agent.subprocess, "Popen") as popen:
                run_agent.spawn_claude("mcp.json", "sonnet")
                args, kwargs = popen.call_args
                kwargs["stderr"].close()
        cmd = args[0]
        i = cmd.index("--tools")
        self.assertEqual(cmd[i + 1], "Read,Write,Edit")
        self.assertTrue(cmd[i + 2].startswith("-"), "--tools ist variadisch")
        disallowed = {cmd[k + 1] for k, a in enumerate(cmd) if a == "--disallowedTools"}
        for tool in ("ScheduleWakeup", "CronCreate", "CronDelete", "CronList", "Monitor",
                     "RemoteTrigger", "PushNotification", "SendMessage", "EnterWorktree",
                     "ExitWorktree", "Agent", "ToolSearch"):
            self.assertIn(tool, disallowed)


class FakeBridge:
    def __init__(self, folder, results=None, state=None, npc_id="birgit"):
        self.dir = folder
        self.npc_id = npc_id
        self.cmd_file = os.path.join(folder, f"commands_{npc_id}.json")
        self.results = results or {}
        self.state = state if state is not None else {
            "npc": {"spawned": True, "alive": True, "in_hands": "", "pos_x": 0.0, "pos_z": 0.0}}
        self.calls = []

    def run(self, action, timeout=0, **kw):
        self.calls.append((action, kw.get("text", ""), kw))
        if action == "spawn":   # neuer Körper lebt sofort (kein 8-s-Warten im Test)
            self.state = {"npc": {"spawned": True, "alive": True, "in_hands": "",
                                  "pos_x": kw.get("x", 0.0), "pos_z": kw.get("z", 0.0)}}
        res = self.results.get((action, kw.get("text", ""))) or self.results.get(action)
        return dict(res or {"status": "done", "detail": ""})

    def read_state(self):
        return self.state


class RestoreTests(unittest.TestCase):
    """DR-2, HUDM-RESP-2, RESTORE-1, RESTORE-2, RUN-3, C7."""

    def test_rank_worn_ground_lost_and_equip_best(self):
        snapshot = {"hands": "", "items": [
            {"classname": "SodaCan_Fronta", "kind": "drink"},
            {"classname": "Crowbar", "kind": "other"},
            {"classname": "TShirt_Blue", "kind": "clothing", "worn": False},
            {"classname": "Jeans_Brown", "kind": "clothing", "worn": True},
            {"classname": "BandageDressing", "kind": "medical"},
            {"classname": "Hacksaw", "kind": "other"},
            {"classname": "Pickaxe", "kind": "other"},
        ]}
        with tempfile.TemporaryDirectory() as folder:
            bridge = FakeBridge(folder, results={
                ("give_item", "SodaCan_Fronta"): {"status": "done",
                                                  "detail": "am Boden: SodaCan_Fronta"},
                ("give_item", "Hacksaw"): {"status": "failed", "detail": "kein Platz"},
            })
            journal = Mock()
            run_agent.restore_inventory(bridge, journal, snapshot)
        order = [(a, t) for a, t, _k in bridge.calls]
        self.assertEqual(order[0], ("give_wear", "Jeans_Brown"))
        self.assertLess(order.index(("give_item", "Crowbar")),
                        order.index(("give_item", "SodaCan_Fronta")),
                        "Nahkampfwaffe vor Getränkedose")
        self.assertIn(("give_item", "TShirt_Blue"), order, "Ersatzkleidung als Item")
        self.assertNotIn(("give_wear", "TShirt_Blue"), order)
        self.assertGreater(order.index(("give_item", "Pickaxe")),
                           order.index(("give_item", "Crowbar")), "Werkzeug bleibt Rang 5")
        self.assertEqual(order.count(("give_item", "Hacksaw")), 2, "ein Retry")
        self.assertIn(("equip_best", ""), order, "leere Hände -> beste Waffe ziehen")
        self.assertEqual(run_agent.LAST_RESTORE_LOST, ["Hacksaw"])
        self.assertEqual(run_agent.LAST_RESTORE_GROUND, ["SodaCan_Fronta"])
        msg = journal.log.call_args_list[-1].args[0]
        self.assertIn("am Boden: SodaCan_Fronta", msg)
        self.assertIn("Hacksaw (failed: kein Platz)", msg)

    def test_old_snapshot_without_worn_still_wears_clothing(self):
        snapshot = {"items": [{"classname": "Jeans_Brown", "kind": "clothing"}],
                    "hands": "Crowbar"}
        with tempfile.TemporaryDirectory() as folder:
            bridge = FakeBridge(folder, state={"npc": {"in_hands": "Crowbar"}})
            run_agent.restore_inventory(bridge, Mock(), snapshot)
        actions = [(a, t) for a, t, _k in bridge.calls]
        self.assertIn(("give_wear", "Jeans_Brown"), actions)
        self.assertNotIn(("equip_best", ""), actions, "Hand ist belegt")

    def test_snapshot_keeps_worn_and_slot(self):
        state = {"npc": {"alive": True, "in_hands": ""}, "inventory": [
            {"classname": "TShirt_Blue", "kind": "clothing", "worn": 0, "slot": "Body"},
            {"classname": "CombatBoots_Black", "kind": "clothing", "worn": 1, "slot": "Feet"}]}
        with tempfile.TemporaryDirectory() as folder:
            path = os.path.join(folder, "last_inventory.json")
            with patch.object(run_agent, "SNAPSHOT_FILE", path):
                run_agent.save_inventory_snapshot(state)
                data = json.load(open(path, encoding="utf-8"))
        self.assertEqual(data["items"][0], {"classname": "TShirt_Blue", "kind": "clothing",
                                            "worn": False, "slot": "Body"})
        self.assertTrue(data["items"][1]["worn"])

    def test_restore_loadout_only_when_file_exists(self):
        with tempfile.TemporaryDirectory() as folder:
            bdir = os.path.join(folder, "IsuSurvivor")
            os.makedirs(bdir)
            bridge = FakeBridge(bdir)
            self.assertEqual(run_agent._restore_loadout(bridge), "FreshSpawnLoadout")
            ldir = os.path.join(folder, "ExpansionMod", "Loadouts")
            os.makedirs(ldir)
            Path(ldir, "IsuRestoreLoadout.json").write_text("{}", encoding="utf-8")
            self.assertEqual(run_agent._restore_loadout(bridge), "IsuRestoreLoadout")

    def test_ensure_body_spawns_at_spawn_pos(self):
        with tempfile.TemporaryDirectory() as folder:
            bridge = FakeBridge(folder, results={"adopt_nearest": {"status": "failed"}},
                                state={"npc": {"spawned": True, "alive": False}})
            with patch.object(run_agent, "SPAWN_POS", (100.0, 200.0)):
                run_agent.ensure_body(bridge, Mock(), restore=False, spawn_pos=(5000.0, 6000.0))
        spawn = [k for a, _t, k in bridge.calls if a == "spawn"][0]
        self.assertEqual((spawn["x"], spawn["z"]), (5000.0, 6000.0))


class Watched:
    """Minimaler Watcher-Ersatz für format_buffered."""

    def __init__(self, stand=0.0, infected=9999.0, hostiles=0, predator=9999.0, uncon=False):
        self.infected_min_now = infected
        self.hostiles_now = hostiles
        self.predator_min_now = predator
        self.uncon_now = uncon
        self._stand = stand

    def stationary_secs(self):
        return self._stand


class BufferedEventTests(unittest.TestCase):
    """VI-9 (Alter/Neuprüfung/Dubletten) und RESP-5 (STILLSTAND-Dauer)."""

    def test_age_dedupe_and_recheck(self):
        events = ["GEFAHR: Infizierter nur noch 23 m entfernt!",
                  "GEFAHR: Infizierter nur noch 23 m entfernt!",
                  "GEFAHR: 3 Gegner dicht beieinander voraus (Horde/Camp).",
                  "DU BIST UMGEKIPPT (bewusstlos). Warte ab.",
                  "SPIELER GESICHTET: 'Rookie' ist in deiner Naehe aufgetaucht.",
                  "DURST: Wasser nur noch 800."]
        stamps = {events[0]: 100.0, events[2]: 100.0, events[3]: 100.0,
                  events[4]: 160.0, events[5]: 140.0}
        out = run_agent.format_buffered(events, stamps, 162.0,
                                        Watched(infected=80.0, hostiles=1, uncon=False))
        self.assertEqual(len(out), 5, "Dublette zusammengefasst")
        self.assertTrue(out[0].startswith("(vor 62 s gemeldet, inzwischen vorbei) GEFAHR: Infiz"))
        self.assertIn("inzwischen vorbei", out[1])
        self.assertEqual(out[2], "(vor 62 s) DU WARST KURZ BEWUSSTLOS, jetzt wieder wach.")
        self.assertTrue(out[3].startswith("SPIELER GESICHTET"), "frisch: kein Präfix")
        self.assertTrue(out[4].startswith("(vor 22 s) DURST"))

    def test_danger_still_present_is_not_marked_over(self):
        e = "GEFAHR: Infizierter nur noch 23 m entfernt!"
        out = run_agent.format_buffered([e], {e: 0.0}, 30.0, Watched(infected=25.0))
        self.assertEqual(out, ["(vor 30 s) " + e])

    def test_stillstand_uses_real_duration_or_is_dropped(self):
        e = "STILLSTAND: Du stehst seit 150 Sekunden unbewegt bei x=1 z=2 und nichts läuft."
        out = run_agent.format_buffered([e], {e: 0.0}, 300.0, Watched(stand=380.0))
        self.assertEqual(out, [e.replace("seit 150 Sekunden", "seit 380 Sekunden")])
        self.assertEqual(run_agent.format_buffered([e], {e: 0.0}, 300.0, Watched(stand=3.0)), [])

    def test_fold_wakeup_keeps_one_message(self):
        msg = run_agent.fold_wakeup("KONTEXT-WECHSEL.", info="Lagezentrum (Stand jetzt): X",
                                    events=["REISE: angekommen"], smalltalk="Igor: hallo",
                                    events_title="SEIT DEINEM LETZTEN ZUG")
        self.assertTrue(msg.startswith("KONTEXT-WECHSEL."))
        self.assertIn("LAGE (nur zur Kenntnis): Lagezentrum (Stand jetzt): X", msg)
        self.assertIn("SEIT DEINEM LETZTEN ZUG:\n- REISE: angekommen", msg)
        self.assertEqual(run_agent.fold_wakeup("A"), "A")


class WatcherTestBase(unittest.TestCase):
    ROSTER = ["Viktor", "Birgit", "Igor", "Konrad"]

    def make_watcher(self, state, own="Birgit"):
        self.folder = tempfile.TemporaryDirectory()
        self.addCleanup(self.folder.cleanup)
        self.bridge = FakeBridge(self.folder.name, state=state, npc_id=own.lower())
        registry = types.SimpleNamespace(all_players=lambda: [
            {"funk": "Player", "dayz": "Rookie", "aliases": ["Rook", "Play"]}])
        patches = [patch.object(run_agent, "load_roster_names", return_value=list(self.ROSTER)),
                   patch.object(run_agent, "load_roster_agents", return_value=[]),
                   patch.object(run_agent, "players_registry", registry),
                   patch.object(run_agent, "AGENT_NAME", own),
                   patch.object(run_agent, "AGENT_BR", 0),
                   patch.object(run_agent, "RADIO_NATIVE", os.path.join(self.folder.name, "none"))]
        for p in patches:
            p.start()
            self.addCleanup(p.stop)
        return run_agent.EventWatcher(self.bridge)

    @staticmethod
    def base_state(**npc):
        n = {"spawned": True, "alive": True, "name": "Birgit", "pos_x": 10391.0,
             "pos_z": 11938.0, "health": 100.0, "blood": 5000.0, "water": 5000.0,
             "energy": 5000.0}
        n.update(npc)
        return {"npc": n, "chat": [], "nearby": [], "inventory": [], "uptime": 1000.0}


class ChatAndSitrepTests(WatcherTestBase):
    """VI-10, HUDM-RESP-1, RESP-1, VI-2, RESP-10."""

    def test_chat_addressee_age_and_own_position(self):
        state = self.base_state()
        w = self.make_watcher(state)
        state["chat"] = [
            {"id": 1, "sender": "Viktor", "uptime": 990.0,
             "text": "Rookie, bin drin. Birgit ist neu bei x elftausendachtundachtzig "
                     "z zwölftausendhundertzwölf, ohne Waffe."},
            {"id": 2, "sender": "Rookie", "uptime": 995.0, "text": "Viktor, komm her"},
            {"id": 3, "sender": "Rookie", "uptime": 970.0, "text": "Birgit, komm her"},
            {"id": 4, "sender": "Rookie", "uptime": 999.0, "text": "Gut, ich komme"},
        ]
        events, _st = w.poll()
        chats = [e for e in events if e.startswith("CHAT von ")]
        self.assertEqual(len(chats), 3, chats)
        self.assertTrue(chats[0].startswith("CHAT von Viktor an Rookie (erwähnt dich nur): "))
        self.assertIn("[deine eigene Position jetzt: x10391 z11938]", chats[0])
        self.assertEqual(chats[1], 'CHAT von Rookie: (vor 30 s) "Birgit, komm her"')
        self.assertEqual(chats[2], 'CHAT von Rookie: "Gut, ich komme"')
        self.assertFalse(run_agent._is_player_priority(chats[0], self.ROSTER))
        self.assertTrue(run_agent._is_player_priority(chats[1], self.ROSTER))
        self.assertTrue(run_agent._is_player_priority(
            'CHAT von Rookie an Viktor (erwähnt dich nur): "Viktor, sag Birgit Bescheid"',
            self.ROSTER))

    def test_mate_smalltalk_names_addressee(self):
        state = self.base_state()
        w = self.make_watcher(state)
        state["chat"] = [{"id": 1, "sender": "Igor", "uptime": 999.0,
                          "text": "Rookie, ich komme zu dir"}]
        events, _st = w.poll()
        self.assertEqual([e for e in events if e.startswith("CHAT")], [])
        self.assertEqual(w.drain_smalltalk(), "Igor an Rookie: Rookie, ich komme zu dir")

    def sitrep(self, w, age, head="Squad-Lage (Stand 18:31:50):"):
        text = (f"{head} Birgit x11088 z12112 HP100; Konrad x10236 z11882 HP70 im KAMPF. "
                "Ausloeser: Konrad ist im Kampf. (Lageinfo, kein Befehl - du entscheidest selbst.)")
        ev = w.route_funk({"user": "Lagezentrum", "text": text, "prio": False,
                           "t": time.time() - age})
        self.assertEqual(ev, "")

    def test_sitrep_rebuilt_from_squad_state(self):
        w = self.make_watcher(self.base_state())
        self.sitrep(w, 40)
        with patch.object(run_agent, "_squad_lines",
                          return_value="Konrad 390 m W (x10000 z11950, HP70, im KAMPF)") as sq:
            info = w.drain_info()
        sq.assert_called_once_with("birgit", 10391.0, 11938.0, details=True)
        self.assertTrue(info.startswith("Lagezentrum (Stand jetzt): Konrad 390 m W"))
        self.assertRegex(info, r"Auslöser vor 4\d s: Konrad ist im Kampf$")
        self.assertEqual(w.drain_info(), "", "Digest geleert")

    def test_sitrep_fallback_marks_self_and_age_both_heads(self):
        for head in ("Squad-Lage (Stand 18:31:50):", "Squad-Lage:"):
            w = self.make_watcher(self.base_state())
            self.sitrep(w, 65, head)
            with patch.object(run_agent, "_squad_lines", return_value=""):
                info = w.drain_info()
            self.assertRegex(info, r"^Lagezentrum \(Meldung vor 6\d s, Positionen evtl\. überholt\): "
                                   r"Squad-Lage: DU \(Birgit\) x11088")
            self.assertNotIn("Stand 18:31:50", info)

    def test_old_sitrep_is_dropped(self):
        w = self.make_watcher(self.base_state())
        self.sitrep(w, 200)
        with patch.object(run_agent, "_squad_lines", return_value="Konrad 5 m N (x1 z2, HP1)"):
            self.assertEqual(w.drain_info(), "")

    def test_helpers(self):
        self.assertEqual(run_agent._sitrep_trigger("Squad-Lage: a. (Lageinfo, x)"), "")
        self.assertEqual(run_agent._sitrep_trigger(
            "Squad-Lage: a. Ausloeser: A | B. (Lageinfo, kein Befehl)"), "A | B")
        self.assertTrue(run_agent._has_coords("bin bei x=11088 z=12112"))
        self.assertTrue(run_agent._has_coords("x elftausendachtundachtzig"))
        self.assertFalse(run_agent._has_coords("Box mit 12 Dosen"))
        known = {"rookie", "viktor"}
        self.assertEqual(run_agent._chat_addressee("Rookie, komm", known, "birgit"), "Rookie")
        self.assertEqual(run_agent._chat_addressee("Gut, ich komme", known, "birgit"), "")
        self.assertEqual(run_agent._chat_addressee("Birgit, komm", known | {"birgit"}, "birgit"), "")
        mate = run_agent._nearest_mate("Viktor TOT; Igor 420 m W (x10020 z12009, HP100); "
                                       "Konrad 190 m SW (x10912 z12039, HP90, im KAMPF); "
                                       "Rookie nicht im Spiel")
        self.assertEqual((mate["name"], mate["dist"], mate["dir"]), ("Konrad", 190.0, "SW"))
        self.assertIsNone(run_agent._nearest_mate(""))


class SquadLinesContractTests(unittest.TestCase):
    """C2: run_agent nutzt bridge.squad_lines und parst daraus den nächsten
    Kameraden (STILLSTAND-Vorschlag, Respawn-Plan)."""

    def test_nearest_mate_from_real_bridge_squad_lines(self):
        import bridge
        if not hasattr(bridge, "squad_lines"):
            self.skipTest("bridge.squad_lines (C2) noch nicht vorhanden")
        payload = {"t": time.time(), "agents": [
            {"id": "birgit", "name": "Birgit", "online": True, "spawned": True, "alive": True,
             "x": 11088, "z": 12112, "health": 100},
            {"id": "igor", "name": "Igor", "online": True, "spawned": True, "alive": True,
             "x": 10912, "z": 12039, "health": 90, "fighting": True},
            {"id": "viktor", "name": "Viktor", "online": True, "spawned": True, "alive": True,
             "x": 9510, "z": 12150, "health": 70},
            {"id": "konrad", "name": "Konrad", "online": True, "spawned": True, "alive": False}]}
        with tempfile.TemporaryDirectory() as folder:
            path = os.path.join(folder, "squad_state.json")
            Path(path).write_text(json.dumps(payload), encoding="utf-8")
            with patch.object(bridge, "SQUAD_STATE_FILE", path):
                line = run_agent._squad_lines("birgit", 11088.0, 12112.0)
        self.assertIn("Konrad TOT", line)
        mate = run_agent._nearest_mate(line)
        self.assertEqual(mate["name"], "Igor")
        self.assertAlmostEqual(mate["dist"], 191.0, delta=1.0)
        self.assertEqual((mate["x"], mate["z"]), (10912.0, 12039.0))


class DangerAndShotTests(WatcherTestBase):
    """VI-M3 (Hysterese), DR-3 (Fahrzeug), MVS-6/C4 (Schüsse)."""

    @staticmethod
    def infected(n, dist=20.0):
        return [{"kind": "infected", "classname": "ZmbM", "distance": dist,
                 "x": 0.0, "z": 0.0} for _ in range(n)]

    def test_cluster_rearms_only_after_45_s(self):
        state = self.base_state()
        w = self.make_watcher(state)
        state["nearby"] = self.infected(3, dist=40.0)
        ev, _ = w.poll()
        self.assertEqual(sum("dicht beieinander" in e for e in ev), 1)
        state["nearby"] = self.infected(2, dist=40.0)
        w.poll()
        state["nearby"] = self.infected(3, dist=40.0)
        ev, _ = w.poll()
        self.assertFalse(any("dicht beieinander" in e for e in ev), "Wackeln 3->2->3")
        state["nearby"] = self.infected(2, dist=40.0)
        w.poll()
        w.cluster_clear_since -= run_agent.CLUSTER_REARM_SEC + 1
        w.poll()
        self.assertFalse(w.cluster_warned)
        state["nearby"] = self.infected(3, dist=40.0)
        ev, _ = w.poll()
        self.assertEqual(sum("dicht beieinander" in e for e in ev), 1)

    def test_vehicle_suffix_on_danger(self):
        state = self.base_state(in_vehicle=True)
        w = self.make_watcher(state)
        state["nearby"] = self.infected(1, dist=15.0)
        ev, _ = w.poll()
        danger = [e for e in ev if e.startswith("GEFAHR: Infizierter")]
        self.assertEqual(len(danger), 1)
        self.assertTrue(danger[0].endswith(
            "DU SITZT ALS MITFAHRER IM FAHRZEUG: nicht kämpfen, sitzen bleiben."))

    def test_shots_info_line_aggregated_and_reanchored(self):
        state = self.base_state(shots=2, last_shot_weapon="")
        w = self.make_watcher(state)
        state["npc"].update(shots=4, last_shot_weapon="Izh18")
        w.poll()
        state["npc"].update(shots=5, last_shot_weapon="Izh18")
        ev, _ = w.poll()
        self.assertFalse(any(e.startswith("SCHÜSSE") for e in ev), "weckt nicht")
        info = w.drain_info()
        self.assertIn("SCHÜSSE: Du hast seit dem letzten Weckruf 3x geschossen (Izh18)", info)
        self.assertNotIn("SCHÜSSE", w.drain_info())
        state["npc"].update(shots=0)               # neuer Körper nach Respawn
        w.poll()
        self.assertNotIn("SCHÜSSE", w.drain_info())
        state["npc"].update(shots=1, last_shot_weapon="CZ75")
        w.poll()
        self.assertIn("1x geschossen (CZ75)", w.drain_info())

    def test_stillstand_suggests_far_mate_first(self):
        state = self.base_state()
        w = self.make_watcher(state)
        w.poll()
        w.still_since -= run_agent.STILL_WARN_SEC + 10
        with patch.object(run_agent, "_squad_lines",
                          return_value="Konrad 190 m SW (x10250 z11800, HP90)"):
            ev, _ = w.poll()
        still = [e for e in ev if e.startswith("STILLSTAND:")]
        self.assertEqual(len(still), 1)
        self.assertIn("ZUERST travel_to zum nächsten Kameraden (Konrad, 190 m SW", still[0])
        self.assertRegex(still[0], r"seit \d+ Sekunden")

    def test_urgent_note_is_honest(self):
        note = run_agent._urgent_note({"command": {"action": "engage", "status": "running"}})
        self.assertIn("liest es aber NICHT", note)
        self.assertIn("laufend: loot_area", run_agent._urgent_note(
            {"command": {"action": "loot_area", "status": "running"}}))
        self.assertIn("kein Mod-Befehl aktiv", run_agent._urgent_note({}))


class RespawnTests(unittest.TestCase):
    """HUD-2/DR-4 (Respawn-Ort), RESP-6/VI-M6/DR-7 (Weckruf)."""

    def dead_state(self, nearby):
        return {"npc": {"spawned": True, "alive": False, "pos_x": 9733.0, "pos_z": 11908.0},
                "nearby": nearby}

    def test_spawn_next_to_nearest_human(self):
        near = [{"kind": "player", "name": "Rookie", "x": 9700.0, "z": 11908.0, "distance": 33},
                {"kind": "player", "name": "Fremder", "x": 9800.0, "z": 11908.0, "distance": 67},
                {"kind": "player", "name": "Viktor", "x": 9733.0, "z": 11909.0, "distance": 1}]
        pos, human = run_agent.respawn_spawn_point(self.dead_state(near), ["Viktor", "Birgit"])
        self.assertEqual(human["name"], "Rookie")
        d = ((pos[0] - 9700.0) ** 2 + (pos[1] - 11908.0) ** 2) ** 0.5
        self.assertAlmostEqual(d, run_agent.RESPAWN_NEAR_PLAYER_M, places=3)
        self.assertGreater(pos[0], 9700.0, "Richtung Leiche")

    def test_no_human_or_threat_near_human_keeps_camp(self):
        self.assertEqual(run_agent.respawn_spawn_point(self.dead_state([]), []), (None, None))
        near = [{"kind": "player", "name": "Rookie", "x": 9700.0, "z": 11908.0, "distance": 33},
                {"kind": "infected", "classname": "ZmbM", "x": 9650.0, "z": 11908.0, "distance": 83}]
        self.assertEqual(run_agent.respawn_spawn_point(self.dead_state(near), []), (None, None))
        near[1] = {"kind": "animal", "classname": "Animal_CanisLupus", "x": 9690.0,
                   "z": 11900.0, "distance": 45}
        self.assertEqual(run_agent.respawn_spawn_point(self.dead_state(near), []), (None, None))

    def test_wakeup_content(self):
        new_state = {"npc": {"spawned": True, "alive": True, "pos_x": 11297.0,
                             "pos_z": 12157.0, "in_hands": "SodaCan_Fronta"},
                     "inventory": [{"classname": "SodaCan_Fronta", "health": 100},
                                   {"classname": "Pickaxe", "health": 100}]}
        thoughts = ["A" * 150, "Ich sitze in einem Fahrzeug mit Rookie.", "Gleich geht es los."]
        text = run_agent.build_respawn_wakeup(
            new_state, {"spawned": True, "pos_x": 9733.0, "pos_z": 11908.0}, thoughts,
            died_in_vehicle=True, squad_line="Igor 1290 m W (x10021 z12009, HP100)",
            lost=["Crowbar", "SteakKnife"], ground=["Rice"], lang="de")
        self.assertTrue(text.startswith("DU BIST GESTORBEN UND NEU ERWACHT. Denke und funke auf Deutsch."))
        self.assertIn("Todesumstand: als Mitfahrer im Fahrzeug (Unfall/Sturz).", text)
        self.assertIn("A" * 100 + " |", text)
        self.assertNotIn("A" * 101, text)
        self.assertIn("Du bist am Lager erwacht (x=11297 z=12157).", text)
        self.assertIn("Squad jetzt: Igor 1290 m W", text)
        self.assertIn("Bewaffnet: NEIN", text, "Pickaxe ist keine Waffe")
        self.assertIn("Nicht wiederhergestellt (liegt bei deiner Leiche): Crowbar, SteakKnife.", text)
        self.assertIn("Am Boden neben dir (aufheben!): Rice.", text)
        self.assertIn("Deine Leiche: x=9733 z=11908", text)
        self.assertIn("travel_to zum nächsten Kameraden (Igor, x=10021 z=12009)", text)
        self.assertIn("NICHT regroup", text)
        self.assertIn("Unbewaffnet heißt nicht warten", text)

    def test_wakeup_br_and_armed_and_next_to_human(self):
        state = {"npc": {"pos_x": 100.0, "pos_z": 100.0, "in_hands": "Crowbar"}, "inventory": []}
        br = run_agent.build_respawn_wakeup(state, {}, [], squad_line="Igor 5 m N (x1 z2, HP1)",
                                            br=True, lang="en")
        self.assertNotIn("Squad jetzt", br)
        self.assertNotIn("Denke und funke auf Deutsch", br)
        self.assertIn("Bewaffnet: ja (Crowbar)", br)
        near = run_agent.build_respawn_wakeup(state, {}, [],
                                              woke_near={"name": "Rookie", "x": 110.0, "z": 100.0})
        self.assertIn("Du bist neben Rookie erwacht", near)
        self.assertIn("Plan: bleib bei Rookie", near)


class DeathAndIntentTests(unittest.TestCase):
    """DR-1/HUD-3/RUN-3/RESP-7 (Tod), RUN-4 (Stop), HUD-5 (native Absicht)."""

    def test_death_epoch_written(self):
        with tempfile.TemporaryDirectory() as folder, \
                patch.object(run_agent, "AGENT_HOME", folder):
            before = time.time()
            run_agent._write_death_epoch()
            value = float(Path(folder, "death_epoch.txt").read_text(encoding="utf-8"))
        self.assertGreaterEqual(value, before - 0.01)

    def test_kill_brain_skips_finished_process(self):
        proc = Mock()
        proc.poll.return_value = 0
        with patch.object(run_agent.subprocess, "run") as run:
            run_agent._kill_brain(proc)
        run.assert_not_called()
        proc.kill.assert_not_called()

    def test_kill_brain_uses_hidden_taskkill_tree_on_windows(self):
        proc = Mock(pid=4711)
        proc.poll.return_value = None
        with patch.object(run_agent.os, "name", "nt"), \
                patch.object(run_agent.subprocess, "run") as run:
            run_agent._kill_brain(proc)
        args, kwargs = run.call_args
        self.assertEqual(args[0], ["taskkill", "/PID", "4711", "/T", "/F"])
        self.assertEqual(kwargs["creationflags"],
                         getattr(run_agent.subprocess, "CREATE_NO_WINDOW", 0))
        proc.wait.assert_called_once()

    def test_intent_files_native_always_and_cleared_on_death(self):
        with tempfile.TemporaryDirectory() as folder:
            intent = os.path.join(folder, "intent_birgit.txt")
            native = os.path.join(folder, "intent_native_birgit.txt")
            with patch.object(run_agent, "INTENT_FILE", intent), \
                    patch.object(run_agent, "AGENT_LANG", "de"):
                run_agent._set_nameplate_intent("Mit Igor bei Birgits Treffpunkt angekommen, "
                                                "Viktor ist auf dem Weg mit siebzig HP.")
                run_agent._set_nameplate_intent("Folge Igor.")
                self.assertEqual(Path(native).read_text(encoding="utf-8"), "Folge Igor.",
                                 "kurze Folgeabsicht ersetzt die alte native Fassung")
                run_agent._clear_intent_files()
                self.assertEqual(Path(intent).read_text(encoding="utf-8"), "")
                self.assertFalse(os.path.exists(native))


class MapBriefingTests(unittest.TestCase):
    """MAP-1: Lager aus der Runde statt fester Koordinate, Kartenwechsel."""

    def test_briefing_uses_camp_of_round(self):
        with patch.object(run_agent, "active_map", return_value="nasdara"), \
                patch.object(run_agent, "CAMP_POS", (11294.8, 12159.1)):
            txt = run_agent.map_briefing()
        self.assertIn("Euer Lager/Treffpunkt in dieser Runde: x11295 z12159.", txt)
        self.assertNotIn("Dein Lager liegt in der grossen Stadt", txt)
        self.assertNotIn("{CAMP}", txt)
        for key in run_agent._MAP_BRIEFINGS:
            self.assertIn("{CAMP}", run_agent._MAP_BRIEFINGS[key], key)

    def test_map_change_notice(self):
        with tempfile.TemporaryDirectory() as folder, \
                patch.object(run_agent, "CAMP_POS", (1.0, 2.0)):
            with patch.object(run_agent, "active_map", return_value="enoch"):
                self.assertEqual(run_agent.map_change_notice(folder), "", "erster Lauf")
                self.assertEqual(run_agent.map_change_notice(folder), "", "gleiche Karte")
            with patch.object(run_agent, "active_map", return_value="nasdara"):
                note = run_agent.map_change_notice(folder)
            self.assertIn("KARTENWECHSEL: Du bist jetzt auf Nasdara (vorher Livonia).", note)
            self.assertIn("als überholt markieren", note)
            self.assertEqual(Path(folder, "last_map.txt").read_text(encoding="utf-8").strip(),
                             "nasdara")


class ReviewFollowUpTests(WatcherTestBase):
    """Review-Runde W1: native Dringend-Bremse, Nicht-Sitrep-Meldungen des
    Lagezentrums, Mehrfach-Anrede im Chat."""

    def test_native_urgent_mid_turn_only_one_extra_send(self):
        # Native Backends: jede Nachricht = eigener Planner-Zug. Während eines
        # 300-s-Werkzeugs darf höchstens EINE Nachricht hinter dem laufenden
        # Zug warten (früher pending <= 1), nicht alle 12 s eine weitere.
        gate = run_agent.TurnGate(native=True)
        reader = fake_reader(last_activity=100.0)
        gate.note_send(reader, now=100.0, brake=False)
        sends = 0
        t = 100.0
        while t < 400.0:
            t += 1.0
            if gate.may_deliver(reader, urgent=True, rotate_pending=False, now=t):
                gate.note_send(reader, now=t)
                sends += 1
        self.assertEqual(sends, 1)
        self.assertEqual(gate.pending, 2)
        self.assertFalse(gate.may_deliver(reader, urgent=False, rotate_pending=False, now=t),
                         "Routine erst im Leerlauf")
        gate.note_result()                           # erster Zug fertig
        self.assertTrue(gate.may_deliver(reader, urgent=True, rotate_pending=False, now=t))

    def test_claude_backend_urgent_still_every_12_s(self):
        # Gegenprobe: für Claude Code (ein result für mehrere Nachrichten)
        # bleibt die 12-s-Bremse die einzige Grenze.
        gate = run_agent.TurnGate()
        reader = fake_reader(last_activity=100.0)
        gate.note_send(reader, now=100.0, brake=False)
        reader.turn_open = True
        sends = 0
        t = 100.0
        while t < 160.0:
            t += 1.0
            if gate.may_deliver(reader, urgent=True, rotate_pending=False, now=t):
                gate.note_send(reader, now=t)
                sends += 1
        self.assertEqual(sends, 5)

    def test_non_sitrep_lagezentrum_message_kept_verbatim(self):
        w = self.make_watcher(self.base_state())
        mission = "Mission: Rettet Birgit aus dem Gefängnis bei x5968 z9102!"
        self.assertEqual(w.route_funk({"user": "Lagezentrum", "text": mission,
                                       "prio": False, "t": time.time() - 30}), "")
        # Der nachfolgende Squad-Sitrep überschreibt die Ansage nicht.
        sitrep = ("Squad-Lage (Stand 18:31:50): Viktor x10500 z12000 HP90. "
                  "Ausloeser: Routine-Lagebericht. (Lageinfo, kein Befehl - du entscheidest selbst.)")
        self.assertEqual(w.route_funk({"user": "Lagezentrum", "text": sitrep,
                                       "prio": False, "t": time.time() - 10}), "")
        with patch.object(run_agent, "_squad_lines",
                          return_value="Viktor 141 m NO (x100 z100, HP90)"):
            info = w.drain_info()
        lines = info.split("\n")
        self.assertTrue(lines[0].startswith("Lagezentrum (Stand jetzt): Viktor 141 m NO"))
        self.assertRegex(lines[1], r"^Lagezentrum \(Meldung vor [23]\d s\): "
                                   r"Mission: Rettet Birgit aus dem Gefängnis bei x5968 z9102!$")
        self.assertEqual(w.drain_info(), "", "Digest geleert")
        # Kein Positions-Schnappschuss: auch nach SITREP_MAX_AGE_SEC nicht verworfen
        # und nie durch die Squad-Zeile ersetzt.
        with patch.object(run_agent, "_squad_lines", return_value="Viktor 5 m N (x1 z2, HP1)"):
            old = w._sitrep_line({"t": 1000.0, "text": mission}, now=1500.0)
        self.assertEqual(old, f"Lagezentrum (Meldung vor 500 s): {mission}")

    def test_multi_addressee_includes_me(self):
        known = {"viktor", "birgit", "igor", "konrad", "rookie"}
        self.assertEqual(run_agent._chat_addressee("Viktor, Birgit, folgt mir", known, "birgit"), "")
        self.assertEqual(run_agent._chat_addressee("Viktor, Birgit, folgt mir", known, "igor"),
                         "Viktor")
        self.assertEqual(run_agent._chat_addressee("Viktor und Birgit, folgt mir", known,
                                                   "birgit"), "")
        self.assertEqual(run_agent._chat_addressee("Viktor und Birgit, folgt mir", known,
                                                   "konrad"), "Viktor")
        self.assertEqual(run_agent._chat_addressee("Rookie, Birgit ist verletzt", known,
                                                   "birgit"), "Rookie")
        self.assertEqual(run_agent._chat_addressee("Viktor, Birgit", known, "birgit"), "")

    def test_multi_addressee_chat_reaches_me_as_direct_command(self):
        state = self.base_state()
        w = self.make_watcher(state)
        state["chat"] = [{"id": 1, "sender": "Rookie", "uptime": 999.0,
                          "text": "Viktor, Birgit, folgt mir"}]
        events, _st = w.poll()
        chats = [e for e in events if e.startswith("CHAT von ")]
        self.assertEqual(chats, ['CHAT von Rookie: "Viktor, Birgit, folgt mir"'])
        self.assertTrue(run_agent._is_player_priority(chats[0], self.ROSTER))


if __name__ == "__main__":
    unittest.main()
