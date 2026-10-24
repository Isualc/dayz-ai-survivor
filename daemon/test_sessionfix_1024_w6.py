"""Offline-Tests für Paket W6 der Session-Fixes vom 24.10.2026 (Persona).

  * persona_de.md enthält die neuen Regeln: Belegpflicht für Kampfberichte
    (MVS-5), engage(mode="melee") statt equip_melee+engage und kein
    Parallelaufruf (MVS-4), Positionen als Momentaufnahmen (RESP-1, VI-2,
    VI-5), Respawn-Treffpunkt und Waffensuche (RESP-6), Warten nur mit wait
    und kein Pendeln (RUN-2, RESP-5), regroup auf Kameraden (MOD-9/TOOL-1).
  * Review-Nachzug: Warten-Regel ohne "sonst Zug beenden", Lagemeldung über
    dich ist kein Ziel (ein Befehl bleibt ein Befehl), weit per travel_to auf
    die Live-Koordinate (passt zum Respawn-Weckruf "Squad jetzt", W1).
  * Rollen-Presets (characters/*.md) fordern die alte Zwei-Schritt-Kette
    equip_melee+engage nicht mehr (bauer.md wird nach der Persona angehängt).
  * Die bestehende Abschnittsstruktur der Persona bleibt erhalten.
  * Vertrags-Abgleich: Die Persona zitiert Texte, die andere Pakete erzeugen
    (C3 engage-Ergebnis, KAMERADEN-Zeile, Lagezentrum-Neuaufbau). Sobald der
    jeweilige Code-Teil eingebaut ist, müssen die zitierten Texte dort auch
    wirklich vorkommen; vorher wird der Abgleich übersprungen.

Gelesen wird nur Quelltext, kein Modul-Import (dayz_mcp startet sonst MCP).

Start: python -B -m pytest -q test_sessionfix_1024_w6.py
"""

import os
import unittest

DAEMON_DIR = os.path.dirname(os.path.abspath(__file__))


def _read(name: str) -> str:
    with open(os.path.join(DAEMON_DIR, name), "r", encoding="utf-8") as f:
        return f.read()


class PersonaRulesTest(unittest.TestCase):
    def setUp(self):
        self.persona = _read("persona_de.md")

    def test_belegpflicht_zitiert_engage_texte(self):
        for phrase in ("Kampfberichte nur mit Beleg",
                       '"GESCHOSSEN mit"',
                       '"Ob du geschossen hast, ist unbekannt"',
                       '"leise"', '"mit dem Messer"', '"kein Schuss"',
                       '"Nach dem Kampf nachgeladen"',
                       "nicht selbst per `engage` geführt"):
            self.assertIn(phrase, self.persona)

    def test_nahkampf_regel_ein_gegner_und_engage_mode(self):
        self.assertIn("Allein gegen EINEN nahen Infizierten", self.persona)
        self.assertIn('`engage(mode="melee")`', self.persona)
        # alte Zwei-Schritt-Kette und "ein, zwei Infizierte" sind weg
        self.assertNotIn("(`equip_melee`, dann `engage`)", self.persona)
        self.assertNotIn("ein, zwei Infizierte", self.persona)
        self.assertIn("nie parallel im selben Schritt", self.persona)

    def test_positionen_sind_momentaufnahmen(self):
        for phrase in ('"KAMERADEN (live)"', '"Lagezentrum (Stand jetzt)"',
                       '"Squad jetzt" im Weckruf',
                       "weit per `travel_to` auf diese Live-Koordinate",
                       "deine alte Position, kein Ziel",
                       "bleibt ein Befehl",
                       "nur mit Zeitbezug weitergeben",
                       "am Satzanfang angesprochen"):
            self.assertIn(phrase, self.persona)
        # Review: "nie dein Ziel" kollidierte mit Spielerbefehlen, und
        # "per follow/regroup statt travel_to" mit dem Respawn-Weckruf (W1).
        self.assertNotIn("nie dein Ziel", self.persona)
        self.assertNotIn("Zu ihm per `follow`/`regroup`", self.persona)

    def test_respawn_und_warten(self):
        self.assertIn("**Nach einem Respawn**", self.persona)
        self.assertIn("NICHT der Aufwachort", self.persona)
        self.assertIn("unterwegs eine Waffe suchen", self.persona)
        self.assertIn("dann mit `wait`", self.persona)
        self.assertIn("der Runner weckt dich", self.persona)
        # Review: "sonst Zug beenden" las sich als Freibrief fürs Stehen
        # und widersprach "Beende keinen Zug im Stehen".
        self.assertNotIn("sonst Zug beenden", self.persona)
        self.assertIn("Sonst endet dein Zug, sobald eine Aktion läuft", self.persona)
        self.assertIn("nicht um denselben Treffpunkt pendeln", self.persona)

    def test_regroup_auf_kameraden(self):
        self.assertIn('`regroup(player_name="Igor")`', self.persona)
        self.assertIn('"Läuft noch" erneut aufrufen', self.persona)

    def test_struktur_bleibt(self):
        for header in ("# Rahmen (Meta, nicht Teil der Rolle)", "# Rolle",
                       "## Wer du bist", "## Deine Lage", "## Wie du arbeitest",
                       "## Survival-Wissen und Folgeketten", "## Funk (Sprachkanal)",
                       "## Fahrzeuge", "## Befehle von Spielern",
                       "## Dein Gedächtnis und Lernen", "## Sprechen und Chat",
                       "## Zusammenhalt und Bündnisse",
                       "## Beziehungen, Trauer und Reibung", "## Wetter-Routinen",
                       "## Lagerfeuer-Runde", "## Harte Regeln"):
            self.assertIn(header, self.persona)
        # Bestehende Leitregeln, auf die die neuen Zeilen aufbauen
        for rule in ("**Treffpunkt-Leine:**", "**Leerlauf gibt es nicht.**",
                     "**Beende keinen Zug im Stehen.**", "**Taktik statt Sturheit:**"):
            self.assertIn(rule, self.persona)


class CharacterPresetTest(unittest.TestCase):
    """Rollen-Presets werden in spawn_claude NACH persona_de.md angehängt und
    dürfen die neuen Kampf- und Respawn-Regeln nicht aushebeln."""

    CHAR_DIR = os.path.join(DAEMON_DIR, "characters")

    def test_keine_zwei_schritt_nahkampfkette(self):
        names = sorted(n for n in os.listdir(self.CHAR_DIR) if n.endswith(".md"))
        self.assertTrue(names)
        for name in names:
            text = _read(os.path.join("characters", name))
            self.assertNotIn("(`equip_melee`, dann `engage`)", text, name)
            self.assertNotIn("im Getümmel", text, name)

    def test_bauer_nahkampf_mit_beleg(self):
        text = _read(os.path.join("characters", "bauer.md"))
        self.assertIn('`engage(mode="melee")`', text)
        self.assertIn("solange das Ergebnis keinen Schuss meldet", text)

    def test_sanitaeter_respawn_squad_ist_station(self):
        text = _read(os.path.join("characters", "sanitaeter.md"))
        self.assertIn("Nach einem Respawn ist der Squad die Station", text)


class PersonaContractTest(unittest.TestCase):
    """Zitierte Tool-Texte müssen im erzeugenden Code stehen, sobald er da ist."""

    def setUp(self):
        self.persona = _read("persona_de.md")
        self.mcp = _read("dayz_mcp.py")
        self.bridge = _read("bridge.py")
        self.runner = _read("run_agent.py")

    def test_engage_ergebnistexte_c3(self):
        if "def engage(mode" not in self.mcp:
            self.skipTest("engage(mode=...) aus C3 noch nicht eingebaut")
        for phrase in ("Kampf beendet:", "GESCHOSSEN mit", "Von dir kein Schuss",
                       "Ob du geschossen hast, ist unbekannt"):
            self.assertIn(phrase, self.mcp)
        self.assertIn('"melee"', self.mcp)

    def test_kameraden_zeile_in_observe(self):
        if "def squad_lines" not in self.bridge or "squad_lines" not in self.mcp:
            self.skipTest("squad_lines (C2) in dayz_mcp noch nicht eingebaut")
        self.assertIn("KAMERADEN (live)", self.mcp)

    def test_lagezentrum_neuaufbau_im_runner(self):
        if "squad_lines" not in self.runner:
            self.skipTest("squad_lines (C2/C9) in run_agent noch nicht eingebaut")
        self.assertIn("Lagezentrum (Stand jetzt)", self.runner)
        # Persona nennt "Squad jetzt" als Live-Quelle im Respawn-Weckruf.
        self.assertIn("Squad jetzt: ", self.runner)


if __name__ == "__main__":
    unittest.main()
