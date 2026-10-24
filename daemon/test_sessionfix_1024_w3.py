"""Offline-Tests für Paket W3 (Lagezentrum und Funk) des Sessionfix 24.10.2026.

Kein Server, kein Mikrofon nötig. Geprüft wird:
  * orchestrator: isolierte Empfänger, Kurzlage für Gefilterte,
    Mindestversorgung (180 s, eigener Takt), Prio-/Rückkehr-Bypass der
    Funk-Sperre, Zwischenspeicher gedrosselter Ereignisse, Heartbeat,
    Kampf-Entprellung, Grundlinie pro Agent, Treffpunkt nur für Lebende,
    Absichts-Alter (neuere Datei gewinnt), Sitrep-Kopf + Bewegungsrichtung,
    Log mit Datum, squad_state-Felder kompatibel.
  * voice_router: normalisierte Anrede ("Victor Conrad"), keine Fehltreffer
    bei normalen Satzanfängen, Log mit Zeitstempel, Herzschlag + PTT-Flanken.
  * arena_supervisor: --heartbeat aus ISU_ORCH_HEARTBEAT (Default 150, 0 = aus).

Start (aus daemon\\): python -m pytest -q test_sessionfix_1024_w3.py
"""

import os
import re
import sys
import tempfile
import time
import types
import unittest
from unittest import mock

DAEMON_DIR = os.path.dirname(os.path.abspath(__file__))
if DAEMON_DIR not in sys.path:
    sys.path.insert(0, DAEMON_DIR)

import orchestrator as O  # noqa: E402

try:
    import sounddevice  # noqa: F401
except Exception:  # pragma: no cover - nur auf Rechnern ohne PortAudio
    sys.modules["sounddevice"] = types.ModuleType("sounddevice")
import voice_router as V  # noqa: E402
import arena_supervisor as A  # noqa: E402

CAMP = (1000.0, 1000.0)
IDS = ["viktor", "birgit", "igor", "konrad"]


def snap(aid, x, z, *, hp=100, alive=True, spawned=True, fighting=False,
         threat=None, intent="", intent_age=None, online=True):
    if not online:
        return {"id": aid, "online": False}
    s = {"id": aid, "online": True, "name": aid.capitalize(),
         "spawned": spawned, "alive": alive, "x": x, "z": z, "health": hp,
         "blood": 5000, "fighting": fighting, "in_vehicle": False,
         "in_hands": "leer", "seq": 1}
    if threat is not None:
        s["threat"] = {"dist": threat, "what": "ZmbM_Test"}
    if intent:
        s["intent"] = intent
        if intent_age is not None:
            s["intent_age"] = intent_age
    return s


class Clock:
    def __init__(self, t=1000.0):
        self.t = t

    def __call__(self):
        return self.t


class Harness:
    """Lagezentrum mit Fake-Uhr und mitgeschnittenem Funk."""

    def __init__(self, ids=IDS, **kw):
        self.clock = Clock()
        self.sent = []          # (aid, text, prio)
        self.logs = []
        kw.setdefault("min_broadcast", 90.0)
        self.lz = O.Lagezentrum(ids, CAMP, send=self._send, clock=self.clock,
                                logger=self.logs.append, **kw)

    def _send(self, aid, text, prio=False):
        self.sent.append((aid, text, prio))
        return True

    def tick(self, cur, dt=3.0):
        self.clock.t += dt
        self.sent = []
        return self.lz.step(cur)


def spread(**over):
    """Standardlage: Viktor weit im Westen, Birgit/Igor/Konrad zusammen."""
    base = {
        "viktor": snap("viktor", 0, 1000),
        "birgit": snap("birgit", 3000, 1000),
        "igor": snap("igor", 3100, 1000),
        "konrad": snap("konrad", 3150, 1050),
    }
    base.update(over)
    return base


class RoutineRelevanceTests(unittest.TestCase):
    def test_isolated_recipient_is_relevant(self):
        cur = spread()
        # Viktor ist von ALLEN anderen > 800 m weg -> relevant, egal wo der Auslöser steht
        self.assertTrue(O.routine_relevant(cur["viktor"], ["konrad"], cur))

    def test_grouped_recipient_far_from_trigger_is_not_relevant(self):
        cur = spread()
        self.assertFalse(O.routine_relevant(cur["birgit"], ["viktor"], cur))

    def test_dead_others_do_not_count_as_company(self):
        cur = {"viktor": snap("viktor", 0, 1000),
               "birgit": snap("birgit", 100, 1000, alive=False)}
        self.assertTrue(O.routine_relevant(cur["viktor"], ["birgit"], cur))


class BroadcastTests(unittest.TestCase):
    def test_filtered_recipients_get_compact_lage(self):
        h = Harness()
        h.tick(spread())                                  # Grundlinie
        h.tick(spread(viktor=snap("viktor", 0, 1000, threat=40,
                                  intent="laufe nach Westen", intent_age=5)))
        by = {aid: (text, prio) for aid, text, prio in h.sent}
        self.assertEqual(set(by), set(IDS))
        for aid in ("birgit", "igor", "konrad"):
            text, prio = by[aid]
            self.assertFalse(prio)
            self.assertTrue(text.startswith("Squad-Lage (Stand "))
            self.assertNotIn("(will:", text)            # Kurzlage ohne Absicht
            self.assertNotIn("Bedrohung ZmbM", text)    # ... und ohne Bedrohung
            self.assertIn("Ausloeser: Bedrohung nahe Viktor", text)
        self.assertGreater(h.lz.last_broadcast, 0)

    def test_min_supply_every_180s_without_last_broadcast(self):
        h = Harness()
        cur = spread()
        h.tick(cur)
        got = []
        for _ in range(61):                               # 183 s Ruhe
            h.tick(spread())
            got += h.sent
        self.assertEqual(sorted(a for a, _t, _p in got), sorted(IDS))
        for _aid, text, prio in got:
            self.assertFalse(prio)
            self.assertIn("Routine-Lagebericht", text)
        self.assertEqual(h.lz.last_broadcast, 0.0)        # eigener Takt

    def test_death_bypasses_throttle_and_wakes_all(self):
        h = Harness()
        h.tick(spread())
        h.tick(spread(birgit=snap("birgit", 3000, 1000, threat=40)))
        self.assertTrue(h.sent)                           # Sperre startet
        h.tick(spread(birgit=snap("birgit", 3000, 1000, alive=False)))
        prio_to = sorted(a for a, t, p in h.sent if p)
        self.assertEqual(prio_to, ["igor", "konrad", "viktor"])
        self.assertTrue(all("Birgit ist gefallen" in t for _a, t, _p in h.sent))

    def test_return_wakes_only_the_returned_and_no_rally_alarm(self):
        h = Harness()
        h.tick(spread())
        h.tick(spread(birgit=snap("birgit", 3000, 1000, alive=False)))
        # Respawn AM LAGER: kein "Treffpunkt erreicht", Rückkehr umgeht die Sperre
        h.tick(spread(birgit=snap("birgit", CAMP[0], CAMP[1])))
        by = {aid: (text, prio) for aid, text, prio in h.sent}
        self.assertEqual(set(by), set(IDS))
        self.assertTrue(by["birgit"][1])
        for aid in ("viktor", "igor", "konrad"):
            self.assertFalse(by[aid][1])
        text = by["birgit"][0]
        self.assertIn("Birgit ist zurueck im Spiel", text)
        self.assertNotIn("Treffpunkt erreicht", text)
        self.assertIn("Igor x3100 z1000", text)           # volles Lagebild

    def test_throttled_events_are_held_and_delivered_later(self):
        h = Harness()
        h.tick(spread())
        h.tick(spread(igor=snap("igor", 3100, 1000, threat=40)))
        self.assertTrue(h.sent)                           # Sperre läuft ab jetzt
        h.tick(spread(igor=snap("igor", 3100, 1000, hp=70, threat=40)))
        self.assertEqual(h.sent, [])                      # gedrosselt
        self.assertEqual(len(h.lz.held), 1)
        for _ in range(30):
            h.tick(spread(igor=snap("igor", 3100, 1000, hp=70, threat=40)))
            if h.sent:
                break
        texts = [t for a, t, _p in h.sent if a == "birgit"]
        self.assertTrue(texts)
        self.assertRegex(texts[0], r"vor \d+ s: Igor verliert HP \(jetzt 70\)")
        self.assertEqual(h.lz.held, [])

    def test_held_is_capped(self):
        h = Harness()
        h.lz._hold([("igor", f"Igor verliert HP (jetzt {n})") for n in range(10)],
                   5.0)
        self.assertEqual(len(h.lz.held), O.HELD_MAX)
        self.assertIn("jetzt 9", h.lz.held[-1][2])
        h.lz._hold([("igor", "Bedrohung nahe Igor: Zmb 30m")], 6.0)
        self.assertEqual(len(h.lz.held), O.HELD_MAX)       # Zustand, kein Ereignis

    def test_heartbeat_unfiltered_not_waking(self):
        h = Harness(heartbeat=150.0)
        h.tick(spread())
        for _ in range(49):                               # 147 s
            h.tick(spread())
            self.assertEqual(h.sent, [])
        h.tick(spread())                                  # 150 s
        self.assertEqual(sorted(a for a, _t, _p in h.sent), sorted(IDS))
        self.assertTrue(all(not p for _a, _t, p in h.sent))
        self.assertTrue(all("(Stand " in t for _a, t, _p in h.sent))

    def test_heartbeat_silent_when_all_dead(self):
        # Review 24.10.: ohne lebenden Empfänger feuerte der Heartbeat jeden Tick
        h = Harness(heartbeat=150.0)
        h.tick(spread())
        dead = {aid: snap(aid, 3000, 1000, alive=False) for aid in IDS}
        h.tick(dead)                                      # alle fallen (einmal Funk an [])
        h.logs.clear()
        for _ in range(100):                              # 300 s Wipe
            h.tick(dict((a, dict(s)) for a, s in dead.items()))
            self.assertEqual(h.sent, [])
        self.assertLessEqual(sum("[Heartbeat]" in l for l in h.logs), 1)
        self.assertFalse(any(l.startswith("FUNK an []") for l in h.logs))
        self.assertNotIn("Heartbeat", h.lz.thought)

    def test_heartbeat_clock_waits_for_first_living_tick(self):
        h = Harness(heartbeat=150.0)
        gone = {aid: snap(aid, 0, 0, spawned=False, alive=False) for aid in IDS}
        h.tick(dict(gone))                                # Grundlinie, noch niemand da
        for _ in range(70):                               # 210 s Serverstart
            h.tick(dict((a, dict(s)) for a, s in gone.items()))
        h.tick(spread())                                  # Erst-Spawn: Grundlinie, kein Funk
        self.assertEqual(h.sent, [])
        for _ in range(48):                               # bis 147 s nach dem letzten Leer-Tick
            h.tick(spread())
            self.assertEqual(h.sent, [])
        h.tick(spread())                                  # 150 s nach dem letzten Leer-Tick
        self.assertEqual(sorted(a for a, _t, _p in h.sent), sorted(IDS))
        self.assertTrue(all(not p for _a, _t, p in h.sent))

    def test_failed_heartbeat_send_is_not_retried_every_tick(self):
        h = Harness(heartbeat=150.0)
        h.lz.send = lambda aid, text, prio=False: False   # voice_inbox nicht schreibbar
        h.tick(spread())
        for _ in range(90):                               # 270 s
            h.tick(spread())
        self.assertEqual(sum("[Heartbeat]" in l for l in h.logs), 1)

    def test_no_broadcast_mode_sends_nothing(self):
        h = Harness(no_broadcast=True, heartbeat=10.0)
        h.tick(spread())
        for _ in range(70):
            h.tick(spread(birgit=snap("birgit", 3000, 1000, alive=False)))
            self.assertEqual(h.sent, [])


class ChangeDetectionTests(unittest.TestCase):
    def test_fight_flag_needs_two_ticks_with_threat(self):
        h = Harness()
        h.tick(spread())
        h.tick(spread(igor=snap("igor", 3100, 1000, fighting=True)))  # ohne Bedrohung
        self.assertFalse(any("im Kampf" in t for _a, t, _p in h.sent))
        h.clock.t += 100                                  # Sperre sicher abgelaufen
        h.tick(spread(igor=snap("igor", 3100, 1000, fighting=True, threat=30)))
        self.assertFalse(any("ist im Kampf" in t for _a, t, _p in h.sent))
        h.clock.t += 100
        cur = spread(igor=snap("igor", 3100, 1000, fighting=True, threat=30))
        h.tick(cur)
        self.assertTrue(any("Igor ist im Kampf" in t for _a, t, _p in h.sent))
        self.assertTrue(cur["igor"]["fighting"])
        self.assertTrue(cur["igor"]["fighting_raw"])

    @staticmethod
    def _igor_state(fighting, nearby):
        return {"seq": 1, "npc": {"spawned": True, "alive": True, "name": "Igor",
                                  "pos_x": 3100.0, "pos_z": 1000.0,
                                  "health": 100.0, "fighting": fighting},
                "nearby": nearby}

    def _fight_ticks(self, nearby, fighting=True):
        """Drei Ticks (Grundlinie + 2) mit Igor aus einem echten Mod-State."""
        h = Harness()
        h.tick(spread())
        texts = []
        cur = None
        for _ in range(2):
            h.clock.t += 100                              # Sperre sicher abgelaufen
            cur = spread(igor=O.snapshot("igor", self._igor_state(fighting, nearby),
                                         False, 60.0))
            h.tick(cur)
            texts += [t for _a, t, _p in h.sent]
        return cur, texts

    def test_fight_against_foreign_ai_counts_without_hostile(self):
        # Review 24.10.: Banditen (kind ai, leerer Name) sind ohne --hostile keine
        # "threat" - das rohe Flag plus fremde KI in Reichweite ist trotzdem Kampf
        bandit = [{"kind": "ai", "name": "", "classname": "eAI_SurvivorM_Raider",
                   "distance": 40.0}]
        cur, texts = self._fight_ticks(bandit)
        self.assertTrue(cur["igor"]["fighting"])
        self.assertTrue(cur["igor"]["combat_near"])
        self.assertNotIn("threat", cur["igor"])           # threat bleibt unverändert
        self.assertTrue(any("Igor ist im Kampf" in t for t in texts))
        self.assertFalse(any("Bedrohung nahe Igor" in t for t in texts))

    def test_firefight_beyond_threat_dist_counts(self):
        bandit = [{"kind": "ai", "name": "", "distance": 90.0}]
        cur, _texts = self._fight_ticks(bandit)
        self.assertTrue(cur["igor"]["fighting"])

    def test_squad_agent_or_player_nearby_is_no_fight(self):
        friends = [{"kind": "ai", "name": "Konrad", "distance": 10.0},
                   {"kind": "player", "name": "Rookie", "distance": 5.0}]
        cur, texts = self._fight_ticks(friends)
        self.assertFalse(cur["igor"]["fighting"])
        self.assertTrue(cur["igor"]["fighting_raw"])
        self.assertNotIn("combat_near", cur["igor"])
        self.assertFalse(any("im Kampf" in t for t in texts))

    def test_foreign_ai_without_fight_flag_is_no_fight(self):
        bandit = [{"kind": "ai", "name": "", "distance": 40.0}]
        cur, texts = self._fight_ticks(bandit, fighting=False)
        self.assertFalse(cur["igor"]["fighting"])
        self.assertFalse(any("im Kampf" in t for t in texts))

    def test_first_spawn_is_baseline_not_return(self):
        prev = {"konrad": snap("konrad", 0, 0, spawned=False, alive=False)}
        cur = {"konrad": snap("konrad", CAMP[0], CAMP[1], fighting=True,
                              threat=10)}
        reasons = O.detect_changes(prev, cur, 20, CAMP, 60, set(), 20, {}, set())
        self.assertEqual(reasons, [])
        prev_offline = {"konrad": snap("konrad", 0, 0, online=False)}
        reasons = O.detect_changes(prev_offline, cur, 20, CAMP, 60, set(), 20,
                                   {}, set())
        self.assertEqual(reasons, [])

    def test_return_after_despawn_gap_is_reported(self):
        dead = set()
        corpse = {"igor": snap("igor", 5, 5, alive=False)}
        gone = {"igor": snap("igor", 0, 0, spawned=False, alive=False)}
        back = {"igor": snap("igor", 50, 50)}
        lebt = {"igor": snap("igor", 5, 5)}
        self.assertEqual(O.detect_changes(lebt, corpse, 20, CAMP, 60, set(), 20,
                                          {}, dead),
                         [("igor", "Igor ist gefallen")])
        O.detect_changes(corpse, gone, 20, CAMP, 60, set(), 20, {}, dead)
        reasons = O.detect_changes(gone, back, 20, CAMP, 60, set(), 20, {}, dead)
        self.assertEqual(reasons, [("igor", "Igor ist zurueck im Spiel")])
        self.assertEqual(dead, set())

    def test_rally_reached_only_when_alive_before(self):
        prev = {"igor": snap("igor", 0, 0)}
        cur = {"igor": snap("igor", CAMP[0], CAMP[1])}
        reasons = O.detect_changes(prev, cur, 20, CAMP, 60, set(), 20, {})
        self.assertIn(("igor", "Igor hat den Treffpunkt erreicht"), reasons)


class IntentTests(unittest.TestCase):
    def test_newer_file_wins_and_age_is_returned(self):
        with tempfile.TemporaryDirectory() as d:
            native = os.path.join(d, "intent_native_igor.txt")
            screen = os.path.join(d, "intent_igor.txt")
            with open(native, "w", encoding="utf-8") as f:
                f.write("Alter langer Gedanke über Birgits Treffpunkt")
            with open(screen, "w", encoding="utf-8") as f:
                f.write("Folge Konrad.")
            now = time.time()
            os.utime(native, (now - 120, now - 120))
            os.utime(screen, (now - 10, now - 10))
            txt, age = O.read_intent(d, "igor")
            self.assertEqual(txt, "Folge Konrad.")
            self.assertAlmostEqual(age, 10, delta=3)
            os.utime(native, (now - 5, now - 5))          # native neuer -> native
            txt, age = O.read_intent(d, "igor")
            self.assertTrue(txt.startswith("Alter langer Gedanke"))
            self.assertAlmostEqual(age, 5, delta=3)

    def test_missing_files(self):
        with tempfile.TemporaryDirectory() as d:
            self.assertEqual(O.read_intent(d, "igor"), ("", O.INTENT_NONE_AGE))

    def test_sitrep_intent_aging(self):
        s = [snap("igor", 1, 2, intent="Folge Konrad", intent_age=30)]
        self.assertIn("(will: Folge Konrad)", O.build_sitrep(s, []))
        s[0]["intent_age"] = 100
        self.assertIn("(dachte vor 100 s: Folge Konrad)", O.build_sitrep(s, []))
        s[0]["intent_age"] = 200
        self.assertNotIn("Folge Konrad", O.build_sitrep(s, []))


class SitrepFormatTests(unittest.TestCase):
    def test_header_and_trigger_separator(self):
        msg = O.build_sitrep([snap("igor", 1, 2)], ["Igor ist im Kampf"])
        self.assertRegex(msg, r"^Squad-Lage \(Stand \d\d:\d\d:\d\d\): Igor x1 z2 HP100\.")
        self.assertIn(" Ausloeser: Igor ist im Kampf.", msg)
        self.assertTrue(msg.endswith("(Lageinfo, kein Befehl - du entscheidest selbst.)"))

    def test_heading_after_30m_in_20s(self):
        h = Harness()
        x = 3000
        cur = None
        for _ in range(8):                                # 24 s, je 6 m nach Westen
            x -= 6
            cur = spread(birgit=snap("birgit", x, 1000))
            h.tick(cur)
        self.assertEqual(cur["birgit"].get("heading"), "W")
        self.assertNotIn("heading", cur["igor"])
        msg = O.build_sitrep([cur["birgit"]], [])
        self.assertIn(", unterwegs Richtung W", msg)

    def test_squad_state_fields_stay_compatible(self):
        h = Harness()
        cur = spread()
        h.tick(cur)
        for key in ("id", "online", "name", "spawned", "alive", "x", "z",
                    "health", "blood", "fighting", "in_vehicle", "in_hands",
                    "seq"):
            self.assertIn(key, cur["igor"])
        self.assertIsInstance(cur["igor"]["fighting"], bool)

    def test_log_has_date(self):
        with tempfile.TemporaryDirectory() as d:
            path = os.path.join(d, "orchestrator.log")
            with mock.patch.object(O, "LOG_FILE", path), \
                    mock.patch("builtins.print"):
                O.log("Testzeile")
            with open(path, encoding="utf-8") as f:
                line = f.read().strip()
        self.assertRegex(line, r"^\[\d{4}-\d\d-\d\d \d\d:\d\d:\d\d\] "
                               r"\[orchestrator\] Testzeile$")


class MainLoopSmokeTest(unittest.TestCase):
    """main() mit Fake-Bridges: Verdrahtung read_intent -> step -> squad_state."""

    def test_main_writes_squad_state_and_funks(self):
        import json
        with tempfile.TemporaryDirectory() as d:
            states = {
                "viktor": {"seq": 1, "npc": {"spawned": True, "alive": True,
                                             "name": "Viktor", "pos_x": 0.0,
                                             "pos_z": 0.0, "health": 100.0}},
                "birgit": {"seq": 1, "npc": {"spawned": True, "alive": True,
                                             "name": "Birgit", "pos_x": 50.0,
                                             "pos_z": 0.0, "health": 100.0}},
            }
            with open(os.path.join(d, "intent_viktor.txt"), "w",
                      encoding="utf-8") as f:
                f.write("Sichere das Lager.")

            class FakeBridge:
                def __init__(self, _profile, aid):
                    self.aid = aid
                    self.dir = d

                def read_state(self):
                    return states[self.aid]

            ticks = {"n": 0}

            def fake_sleep(_s):
                ticks["n"] += 1
                if ticks["n"] == 3:      # dritter Tick: Birgit fällt
                    states["birgit"]["npc"]["alive"] = False
                if ticks["n"] > 4:
                    raise KeyboardInterrupt

            sent = []
            argv = ["orchestrator.py", "--agents", "viktor,birgit,igor"]
            with mock.patch.object(O, "Bridge", FakeBridge), \
                    mock.patch.object(O, "SQUAD_STATE_FILE",
                                      os.path.join(d, "squad_state.json")), \
                    mock.patch.object(O, "LOG_FILE", os.path.join(d, "o.log")), \
                    mock.patch.object(O, "inbox_append",
                                      lambda a, t, p=False: sent.append((a, t, p)) or True), \
                    mock.patch.object(O.time, "sleep", fake_sleep), \
                    mock.patch.object(sys, "argv", argv), \
                    mock.patch("builtins.print"):
                states["igor"] = None
                self.assertEqual(O.main(), 0)
                with open(os.path.join(d, "squad_state.json"), encoding="utf-8") as f:
                    payload = json.load(f)
        self.assertIn("t", payload)
        agents = {a["id"]: a for a in payload["agents"]}
        self.assertEqual(agents["igor"], {"id": "igor", "online": False,
                                          "name": "Igor"})
        self.assertEqual(agents["viktor"]["intent"], "Sichere das Lager.")
        self.assertIn("intent_age", agents["viktor"])
        self.assertIn("fighting_raw", agents["viktor"])
        self.assertEqual(sent and sent[0][0], "viktor")
        self.assertTrue(sent[0][2])                       # Tod weckt
        self.assertIn("Birgit ist gefallen", sent[0][1])
        self.assertIn("(will: Sichere das Lager.)", sent[0][1])


class VoiceRouterTests(unittest.TestCase):
    ALIASES = {a: a for a in IDS}

    def route(self, text):
        return V.route(IDS, self.ALIASES, text)[0]

    def test_stt_spellings_are_addressed(self):
        self.assertEqual(self.route("Victor, nimm dein Alice back."), ["viktor"])
        self.assertEqual(self.route("Conrad komm her"), ["konrad"])
        self.assertEqual(self.route("Wiktor?"), ["viktor"])
        self.assertEqual(self.route("Victor Conrad tauscht eure Sachen aus."),
                         ["konrad", "viktor"])
        self.assertEqual(self.route("Viktor und Konrad, kommt"), ["konrad", "viktor"])
        self.assertEqual(self.route("Hey Viktor, komm"), ["viktor"])
        self.assertEqual(self.route("Viktor Mienach"), ["viktor"])

    def test_normal_sentence_starts_go_to_all(self):
        for text in ("Holt Igor her", "Folgt Viktor!", "Wo seid ihr?",
                     "Kommt alle zum Lager", "Rookie hier, alles ok",
                     "Okay Viktor", "und Viktor", "Ich brauche Hilfe"):
            self.assertEqual(self.route(text), sorted(IDS), text)

    def test_menu_names_and_multiword_aliases(self):
        aliases = {"viktor": "viktor", "angie lee": "birgit", "birgit": "birgit"}
        self.assertEqual(V.route(IDS, aliases, "Angie Lee, komm")[0], ["birgit"])
        self.assertEqual(V.addressed_agents(aliases, "Angie, komm"), [])

    def test_norm(self):
        self.assertEqual(V._norm("Victor"), "viktor")
        self.assertEqual(V._norm("Conrad"), "konrad")
        self.assertEqual(V._norm("Philipp"), "filip")

    def test_log_has_timestamp(self):
        with tempfile.TemporaryDirectory() as d:
            path = os.path.join(d, "voice_router.log")
            with mock.patch.object(V, "LOG_FILE", path), \
                    mock.patch("builtins.print"):
                V.log("Testzeile")
            with open(path, encoding="utf-8") as f:
                line = f.read().strip()
        self.assertRegex(line, r"^\[\d\d:\d\d:\d\d\] \[router\] Testzeile$")

    def test_audio_heartbeat_and_ptt_edges(self):
        lines = []
        hb = V.AudioHeartbeat(ptt=True, interval=60.0, logger=lines.append)
        t = 0.0
        for i in range(2100):                             # 63 s à 30 ms
            hot = 1000 <= i < 1100
            hb.feed(t, 900.0 if hot else 5.0, hot)
            t += 0.03
        self.assertTrue(any(l.startswith("PTT gedrückt") for l in lines))
        self.assertTrue(any(l.startswith("PTT losgelassen nach 3.0 s, max. RMS 900")
                            for l in lines))
        beats = [l for l in lines if l.startswith("Herzschlag:")]
        self.assertEqual(len(beats), 1)
        self.assertIn("max. RMS 900", beats[0])
        self.assertIn("PTT 1x gedrückt", beats[0])


class SupervisorHeartbeatTests(unittest.TestCase):
    def test_heartbeat_args(self):
        self.assertEqual(A.orch_heartbeat_args({}), ["--heartbeat", "150"])
        self.assertEqual(A.orch_heartbeat_args({"ISU_ORCH_HEARTBEAT": "0"}), [])
        self.assertEqual(A.orch_heartbeat_args({"ISU_ORCH_HEARTBEAT": "120"}),
                         ["--heartbeat", "120"])
        with mock.patch.object(A, "log") as lg:
            self.assertEqual(A.orch_heartbeat_args({"ISU_ORCH_HEARTBEAT": "abc"}),
                             ["--heartbeat", "150"])
            lg.assert_called_once()


if __name__ == "__main__":
    unittest.main()
