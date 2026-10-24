"""Sessionfix 24.10. - Paket W5 (Client-Mod IsuVoice).

Die EnforceScript-Dateien lassen sich hier nicht kompilieren. Diese Tests
prüfen deshalb statisch, was sonst erst beim Serverstart als Kompilierfehler
oder als stilles Protokoll-Missverständnis auffallen würde:

* RPC-Konstante ISU_RPC_AGENTPOS ("ISUP") ist eindeutig und passt, sobald
  vorhanden, zu RPC_AGENTPOS im Server (isubridge.c), inklusive Param3-Typen.
* Die drei geänderten Client-Dateien halten die EnforceScript-Regeln ein:
  kein Ternary, keine umbrochenen Bedingungen, keine doppelt deklarierten
  lokalen Variablen pro Funktion (sonst "multiple declaration").
* Die Kernpunkte von HUD-1/HUD-4/HUD-6 stehen im Code (Ablauf nach 10 s statt
  Löschen pro Frame, ferne Agenten mit "(fern)", Namens-Dedupe, leere
  Absichten werden übernommen).
"""
import re
import unittest
from pathlib import Path

REPO = Path(__file__).resolve().parents[1]
VOICE = REPO / "mod" / "IsuVoice" / "scripts"
RECEIVER = VOICE / "4_World" / "IsuVoice" / "IsuVoiceReceiver.c"
NAMEPLATE = VOICE / "5_Mission" / "IsuVoice" / "IsuNameplateHud.c"
SQUAD = VOICE / "5_Mission" / "IsuVoice" / "IsuSquadHud.c"
BRIDGE = REPO / "mod" / "IsuSurvivor" / "scripts" / "5_Mission" / "IsuSurvivor" / "isubridge.c"

W5_FILES = (RECEIVER, NAMEPLATE, SQUAD)


def _read(path: Path) -> str:
    return path.read_text(encoding="utf-8")


def strip_comments_and_strings(src: str) -> str:
    """Kommentare entfernen und String-Inhalte leeren (Zeilenstruktur bleibt)."""
    out = []
    i, n = 0, len(src)
    while i < n:
        c = src[i]
        if src.startswith("//", i):
            j = src.find("\n", i)
            i = n if j < 0 else j
            continue
        if src.startswith("/*", i):
            j = src.find("*/", i + 2)
            block = src[i:n if j < 0 else j + 2]
            out.append("\n" * block.count("\n"))
            i = n if j < 0 else j + 2
            continue
        if c == '"':
            j = i + 1
            while j < n and src[j] != '"':
                j += 2 if src[j] == "\\" else 1
            out.append('""')
            i = j + 1
            continue
        out.append(c)
        i += 1
    return "".join(out)


_TYPE = r"(?:[A-Za-z_]\w*<[^;=()]*>|[A-Z]\w*|int|float|bool|string|vector|auto|typename)"
_DECL = re.compile(r"^\s*(?:(?:static|const|ref|autoptr|notnull)\s+)*" + _TYPE +
                   r"\s+(\w+(?:\s*,\s*\w+)*)\s*(?:=|;)")
_FOR = re.compile(r"\bfor\s*\(\s*(?:int|float)\s+(\w+)")
_FOREACH = re.compile(r"\bforeach\s*\(([^:]*):")


def _split_top_level(text: str) -> list:
    parts, depth, cur = [], 0, []
    for ch in text:
        if ch == "<":
            depth += 1
        elif ch == ">":
            depth -= 1
        if ch == "," and depth == 0:
            parts.append("".join(cur))
            cur = []
        else:
            cur.append(ch)
    if "".join(cur).strip():
        parts.append("".join(cur))
    return parts


def _param_names(header: str) -> list:
    m = re.search(r"\(([^()]*)\)\s*$", header.strip())
    if not m or not m.group(1).strip():
        return []
    names = []
    for part in _split_top_level(m.group(1)):
        part = part.split("=")[0].strip()
        ident = re.findall(r"\w+", part)
        if ident:
            names.append(ident[-1])
    return names


def function_bodies(src: str):
    """(Kopfzeile, Rumpf) jeder Methode: Bereiche zwischen '{' auf Tiefe 1->2
    und der passenden '}' (Klassen liegen auf Tiefe 0->1)."""
    clean = strip_comments_and_strings(src)
    depth = 0
    boundary = 0
    start = None
    header = ""
    for idx, ch in enumerate(clean):
        if ch == "{":
            if depth == 1:
                header = clean[boundary:idx]
                start = idx + 1
            depth += 1
            if depth == 1:
                boundary = idx + 1
        elif ch == "}":
            depth -= 1
            if depth == 1 and start is not None:
                yield header, clean[start:idx]
                start = None
                boundary = idx + 1
            elif depth == 0:
                boundary = idx + 1
        elif ch == ";" and depth == 1:
            boundary = idx + 1


def local_declarations(header: str, body: str) -> list:
    names = list(_param_names(header))
    for line in body.splitlines():
        m = _DECL.match(line)
        if m:
            names.extend(v.strip() for v in m.group(1).split(","))
        for fm in _FOR.finditer(line):
            names.append(fm.group(1))
        for fe in _FOREACH.finditer(line):
            for part in _split_top_level(fe.group(1)):
                ident = re.findall(r"\w+", part)
                if ident:
                    names.append(ident[-1])
    return names


def duplicate_locals(src: str) -> dict:
    found = {}
    for header, body in function_bodies(src):
        names = local_declarations(header, body)
        dupes = sorted({n for n in names if names.count(n) > 1})
        if dupes:
            fn = re.findall(r"(\w+)\s*\(", header)
            found[fn[-1] if fn else header.strip()[:40]] = dupes
    return found


class EnforceLintSelfTest(unittest.TestCase):
    """Der Lint darf nicht leer laufen: echte Verstöße muss er finden."""

    def test_detects_duplicate_local_in_sibling_blocks(self):
        src = ("class A\n{\n\tvoid F(int x)\n\t{\n\t\tif (x > 0)\n\t\t{\n\t\t\tint y = 1;\n"
               "\t\t}\n\t\telse\n\t\t{\n\t\t\tint y = 2;\n\t\t}\n\t}\n}\n")
        self.assertEqual(duplicate_locals(src), {"F": ["y"]})

    def test_detects_param_redeclared_and_foreach_vars(self):
        src = ("class A\n{\n\tstatic void G(string name)\n\t{\n\t\tstring name = \"\";\n"
               "\t\tforeach (string k, Obj v : m) {}\n\t\tforeach (string k, Obj w : m) {}\n\t}\n}\n")
        self.assertEqual(duplicate_locals(src), {"G": ["k", "name"]})

    def test_ignores_comments_strings_and_members(self):
        src = ("class A\n{\n\tint m;\n\tvoid F()\n\t{\n\t\t// int z = 1;\n\t\tPrint(\"int z = 2;\");\n"
               "\t\tint z = 3;\n\t}\n\tvoid G()\n\t{\n\t\tint z = 4;\n\t}\n}\n")
        self.assertEqual(duplicate_locals(src), {})


class EnforceRulesW5(unittest.TestCase):

    def test_no_duplicate_locals_per_function(self):
        for path in W5_FILES:
            with self.subTest(path=path.name):
                self.assertEqual(duplicate_locals(_read(path)), {})

    def test_no_ternary_operator(self):
        for path in W5_FILES:
            with self.subTest(path=path.name):
                self.assertNotIn("?", strip_comments_and_strings(_read(path)))

    def test_conditions_on_one_line(self):
        for path in W5_FILES:
            clean = strip_comments_and_strings(_read(path))
            for no, line in enumerate(clean.splitlines(), 1):
                if re.match(r"^\s*(?:else\s+)?(?:if|while)\s*\(", line):
                    with self.subTest(path=path.name, line=no):
                        self.assertEqual(line.count("("), line.count(")"), line.strip())

    def test_new_code_is_ascii(self):
        # Bestehende Altlasten (Gedankenstrich im Kopfkommentar, "kämpft" im
        # ActionText) sind erlaubt, sonst nur ASCII (Stock-Font/Encoding).
        allowed = {"—", "ä"}
        for path in W5_FILES:
            with self.subTest(path=path.name):
                extra = {ch for ch in _read(path) if ord(ch) > 127} - allowed
                self.assertEqual(extra, set())


class AgentPosContractC5(unittest.TestCase):

    def test_client_constant_value_and_unique(self):
        src = _read(RECEIVER)
        consts = dict(re.findall(r"const int (ISU_RPC_\w+) = (0x[0-9A-Fa-f]+);", src))
        self.assertEqual(int(consts["ISU_RPC_AGENTPOS"], 16), 0x49535550)
        values = [int(v, 16) for v in consts.values()]
        self.assertEqual(len(values), len(set(values)), consts)

    def test_client_reads_param3_int_int_vector(self):
        src = _read(RECEIVER)
        branch = src.split("if (rpc_type == ISU_RPC_AGENTPOS)", 1)[1].split("return;", 1)[0]
        self.assertIn("Param3<int, int, vector>", branch)
        self.assertIn("IsuNametagStore.UpdatePos(", branch)

    def test_server_constant_matches_if_present(self):
        src = _read(BRIDGE)
        m = re.search(r"static const int RPC_AGENTPOS = (0x[0-9A-Fa-f]+);", src)
        if not m:
            self.skipTest("Server-Paket hat RPC_AGENTPOS (noch) nicht eingebaut")
        self.assertEqual(int(m.group(1), 16), 0x49535550)
        send = re.search(r"Param3<([^>]*)>\s+\w+\s*=\s*new Param3<[^>]*>\([^;]*GetPosition\(\)", src)
        self.assertIsNotNone(send, "Server sendet die Position nicht als Param3")
        self.assertEqual(send.group(1).replace(" ", ""), "int,int,vector")


class HudBehaviourW5(unittest.TestCase):

    def test_store_entries_age_out_instead_of_per_frame_delete(self):
        rec = _read(RECEIVER)
        self.assertRegex(rec, r"static const int STALE_MS = 10000;")
        # Jeder Server-RPC, der den Eintrag lebendig hält, stempelt lastRpcMs.
        for fn in ("static void Update(", "static void UpdateIntent(", "static void UpdatePos("):
            body = rec.split(fn, 1)[1].split("\n\t}\n", 1)[0]
            with self.subTest(fn=fn):
                self.assertIn("t.lastRpcMs = GetGame().GetTime();", body)
        nameplate = strip_comments_and_strings(_read(NAMEPLATE))
        loop = nameplate.split("foreach (string key, IsuAgentTag t : IsuNametagStore.s_Agents)", 1)[1]
        before_obj = loop.split("GetObjectByNetworkId", 1)[0]
        self.assertIn("IsuNametagStore.IsStale(t, nowMs)", before_obj)
        self.assertIn("stale.Insert(key)", before_obj)
        after_obj = loop.split("GetObjectByNetworkId", 1)[1].split("vector head", 1)[0]
        self.assertNotIn("stale.Insert", after_obj)

    def test_get_or_create_is_neutral(self):
        body = _read(RECEIVER).split("static IsuAgentTag GetOrCreate(", 1)[1].split("return t;", 1)[0]
        for line in ('t.name = "";', "t.slot = 7;", "t.hasPos = false;", "t.lastRpcMs = GetGame().GetTime();"):
            with self.subTest(line=line):
                self.assertIn(line, body)

    def test_squad_hud_shows_far_agents_with_rpc_position(self):
        squad = _read(SQUAD)
        self.assertIn("if (!obj && !t.hasPos)", squad)
        self.assertIn('"(fern)"', squad)
        self.assertIn("vector.Distance(pp, apos)", squad)
        self.assertNotIn("obj.GetPosition()));", squad)

    def test_name_dedupe_collects_then_removes(self):
        body = _read(RECEIVER).split("static void Update(", 1)[1].split("static void UpdateIntent(", 1)[0]
        self.assertIn("dk != myKey && dt && dt.name == name", body)
        loop_end = body.index("dupes.Insert(dk);")
        self.assertGreater(body.index("s_Agents.Remove(dupes[ri]);"), loop_end)

    def test_empty_intent_is_taken_over(self):
        body = _read(RECEIVER).split("static void UpdateIntent(", 1)[1].split("static void UpdatePos(", 1)[0]
        assign = body.index("t.intent = intent;")
        # Die Zuweisung hängt an keiner Bedingung auf intent.
        self.assertNotRegex(body[:assign].split("GetOrCreate", 1)[1], r"if\s*\(\s*intent")


if __name__ == "__main__":
    unittest.main()
