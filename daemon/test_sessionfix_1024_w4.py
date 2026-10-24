"""Sessionfix 24.10. - Paket W4 (Server-Mod IsuSurvivor, EnforceScript).

Die Mod lässt sich hier nicht kompilieren, und ein Kompilierfehler legt den
ganzen Server lahm. Diese Tests prüfen deshalb statisch:

* EnforceScript-Regeln in isubridge.c, IsuEAIPatches.c und IsuProtocol.c:
  kein Ternary, keine umgebrochenen Bedingungen, keine doppelt deklarierten
  lokalen Variablen pro Funktion ("multiple declaration"), keine Nicht-ASCII-
  Zeichen in String-Literalen (Stock-Font).
* Die Schnittstellen, auf die Python und die Client-Mod parallel bauen:
  C3 engage-Detail "|SCHUSS=|HAND=|ESK=", C4 npc.shots/last_shot_weapon,
  C5 RPC_AGENTPOS ("ISUP", Param3<int, int, vector>, ungarantiert),
  C6 side_command für say/intent/ping, C7 "am Boden: ", C8 NpcReadyOnFoot.
* Die benutzten Expansion-/Vanilla-Signaturen stehen so in reference\
  (Guard(), EEFired, m_eAI_LastFiredTime, eAI_WeaponSelection ...).

Start: python -m pytest -q test_sessionfix_1024_w4.py
"""
import re
import unittest
from pathlib import Path

REPO = Path(__file__).resolve().parents[1]
SURV = REPO / "mod" / "IsuSurvivor" / "scripts"
BRIDGE_C = SURV / "5_Mission" / "IsuSurvivor" / "isubridge.c"
PROTOCOL_C = SURV / "5_Mission" / "IsuSurvivor" / "IsuProtocol.c"
PATCHES_C = SURV / "4_World" / "IsuSurvivor" / "IsuEAIPatches.c"
RECEIVER_C = REPO / "mod" / "IsuVoice" / "scripts" / "4_World" / "IsuVoice" / "IsuVoiceReceiver.c"
W4_FILES = (BRIDGE_C, PROTOCOL_C, PATCHES_C)

EXP_AI = REPO / "reference" / "DayZ-Expansion-Scripts" / "DayZExpansion" / "AI" / "Scripts" / "4_World" / "DayZExpansion_AI"
VANILLA = REPO / "reference" / "dayz-vanilla" / "scripts"


def _read(path: Path) -> str:
    return path.read_text(encoding="utf-8")


def _blank_comments(src: str, keep_strings: bool) -> str:
    """Kommentare durch Leerzeichen ersetzen (Zeilenumbrüche bleiben), String-
    Inhalte optional ebenso. Die Länge bleibt gleich, damit Positionen aus der
    geleerten Fassung auf die Fassung mit Strings passen."""
    out = []
    i, n = 0, len(src)
    while i < n:
        if src.startswith("//", i):
            j = src.find("\n", i)
            j = n if j < 0 else j
            out.append(" " * (j - i))
            i = j
            continue
        if src.startswith("/*", i):
            j = src.find("*/", i + 2)
            j = n if j < 0 else j + 2
            out.append("".join("\n" if ch == "\n" else " " for ch in src[i:j]))
            i = j
            continue
        c = src[i]
        if c == '"':
            j = i + 1
            while j < n and src[j] != '"':
                j += 2 if src[j] == "\\" else 1
            literal = src[i:j + 1]
            if keep_strings:
                out.append(literal)
            else:
                out.append('"' + " " * max(0, len(literal) - 2) + '"')
            i = j + 1
            continue
        out.append(c)
        i += 1
    return "".join(out)


def code_only(src: str) -> str:
    """Code ohne Kommentare, String-Inhalte geleert (für die Regel-Prüfungen)."""
    return _blank_comments(src, keep_strings=False)


def code_with_strings(src: str) -> str:
    """Code ohne Kommentare, Strings bleiben (für die Inhalts-Prüfungen)."""
    return _blank_comments(src, keep_strings=True)


def string_literals(src: str) -> list:
    return re.findall(r'"((?:[^"\\]|\\.)*)"', code_with_strings(src))


_TYPE = r"(?:[A-Za-z_]\w*<[^;=()]*>|[A-Z]\w*|int|float|bool|string|vector|auto|typename)"
_DECL = re.compile(r"^\s*(?:(?:static|const|ref|autoptr)\s+)*" + _TYPE + r"\s+(\w+(?:\s*,\s*\w+)*)\s*(?:=|;)")
_FOR = re.compile(r"\bfor\s*\(\s*(?:int|float)\s+(\w+)")
_FOREACH = re.compile(r"\bforeach\s*\(([^:]*):")


def _split_commas(text: str) -> list:
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


def methods(src: str, keep_strings: bool = False):
    """(Name, Kopf, Rumpf) jeder Methode. Klassen öffnen Tiefe 0->1,
    Methodenrümpfe 1->2; verschachtelte Blöcke bleiben im Rumpf. Klammern
    werden auf der geleerten Fassung gezählt (keine Klammern aus Strings)."""
    clean = code_only(src)
    text = code_with_strings(src) if keep_strings else clean
    depth, boundary, start, header = 0, 0, None, ""
    for idx, ch in enumerate(clean):
        if ch == "{":
            if depth == 1:
                header = text[boundary:idx]
                start = idx + 1
            depth += 1
            if depth == 1:
                boundary = idx + 1
        elif ch == "}":
            depth -= 1
            if depth == 1 and start is not None:
                names = re.findall(r"(\w+)\s*\(", header)
                yield (names[-1] if names else header.strip()[:40]), header, text[start:idx]
                start = None
                boundary = idx + 1
            elif depth == 0:
                boundary = idx + 1
        elif ch == ";" and depth == 1:
            boundary = idx + 1


def local_names(header: str, body: str) -> list:
    names = []
    m = re.search(r"\(([^()]*)\)\s*$", header.strip())
    if m and m.group(1).strip():
        for part in _split_commas(m.group(1)):
            ident = re.findall(r"\w+", part.split("=")[0])
            if ident:
                names.append(ident[-1])
    for line in body.splitlines():
        dm = _DECL.match(line)
        if dm and not line.strip().startswith(("return", "else", "case")):
            names.extend(v.strip() for v in dm.group(1).split(","))
        names.extend(fm.group(1) for fm in _FOR.finditer(line))
        for fe in _FOREACH.finditer(line):
            for part in _split_commas(fe.group(1)):
                ident = re.findall(r"\w+", part)
                if ident:
                    names.append(ident[-1])
    return names


def duplicate_locals(src: str) -> dict:
    found = {}
    for name, header, body in methods(src):
        names = local_names(header, body)
        dupes = sorted({n for n in names if names.count(n) > 1})
        if dupes:
            found[name] = dupes
    return found


def method_body(src: str, name: str) -> str:
    for mname, _header, body in methods(src, keep_strings=True):
        if mname == name:
            return body
    raise AssertionError(f"Methode {name} nicht gefunden")


class LintSelfTest(unittest.TestCase):
    """Der Lint muss echte Verstöße finden, sonst prüfen die Regeltests nichts."""

    def test_duplicate_in_sibling_blocks_and_loops(self):
        src = ("class A\n{\n\tvoid F(int x)\n\t{\n\t\tforeach (EntityAI e : xs)\n\t\t{\n"
               "\t\t\tstring note = \"\";\n\t\t}\n\t\tstring note = \"\";\n\t\tint x = 1;\n\t}\n}\n")
        self.assertEqual(duplicate_locals(src), {"F": ["note", "x"]})

    def test_members_comments_strings_ignored(self):
        src = ("class A\n{\n\tprivate int m_A;\n\tvoid F()\n\t{\n\t\t// int z = 1;\n"
               "\t\tPrint(\"int z = 2;\");\n\t\tint z = 3;\n\t}\n\tvoid G()\n\t{\n\t\tint z = 4;\n\t}\n}\n")
        self.assertEqual(duplicate_locals(src), {})


class EnforceRulesW4(unittest.TestCase):

    def test_no_duplicate_locals_per_function(self):
        for path in W4_FILES:
            with self.subTest(path=path.name):
                self.assertEqual(duplicate_locals(_read(path)), {})

    def test_no_ternary_operator(self):
        for path in W4_FILES:
            with self.subTest(path=path.name):
                self.assertNotIn("?", code_only(_read(path)))

    def test_conditions_on_one_line(self):
        for path in W4_FILES:
            for no, line in enumerate(code_only(_read(path)).splitlines(), 1):
                if re.match(r"^\s*(?:\}\s*)?(?:else\s+)?(?:if|while)\s*\(", line):
                    with self.subTest(path=path.name, line=no):
                        self.assertEqual(line.count("("), line.count(")"), line.strip())

    def test_string_literals_are_ascii(self):
        for path in W4_FILES:
            for lit in string_literals(_read(path)):
                with self.subTest(path=path.name, literal=lit[:40]):
                    self.assertTrue(all(ord(ch) < 128 for ch in lit), lit)

    def test_no_server_swap_entities(self):
        # Ausdrücklich ausgeschlossen (an eAI ungetestet, Hand-Anim-Zustände)
        self.assertNotIn("ServerSwapEntities", code_only(_read(BRIDGE_C)))


class InterfaceContractsW4(unittest.TestCase):

    def setUp(self):
        self.bridge = _read(BRIDGE_C)
        self.bridge_code = code_only(self.bridge)
        self.bridge_strs = code_with_strings(self.bridge)   # ohne Kommentare
        self.protocol = code_only(_read(PROTOCOL_C))
        self.patches = code_only(_read(PATCHES_C))

    def test_c5_agentpos_rpc(self):
        m = re.search(r"static const int RPC_AGENTPOS = (0x[0-9A-Fa-f]+);", self.bridge_code)
        self.assertIsNotNone(m)
        self.assertEqual(int(m.group(1), 16), 0x49535550)
        self.assertEqual(bytes.fromhex(m.group(1)[2:]).decode("ascii"), "ISUP")
        # Wert darf mit keiner anderen IsuSurvivor-RPC-Konstante kollidieren
        others = re.findall(r"0x4953[0-9A-Fa-f]{4}", code_only(_read(SURV / "4_World" / "IsuSurvivor" / "IsuArenaControl.c")))
        self.assertNotIn(m.group(1).upper().replace("0X", "0x"), [o.upper().replace("0X", "0x") for o in others])
        client = re.search(r"const int ISU_RPC_AGENTPOS = (0x[0-9A-Fa-f]+);", code_only(_read(RECEIVER_C)))
        self.assertIsNotNone(client, "Client-Konstante fehlt (Paket W5)")
        self.assertEqual(int(client.group(1), 16), int(m.group(1), 16))
        body = method_body(self.bridge, "BroadcastNametags")
        self.assertIn("new Param3<int, int, vector>(low, high, ai.GetPosition())", body)
        self.assertIn("RPCSingleParam(pb, RPC_AGENTPOS, posData, false, pb.GetIdentity())", body)

    def test_hud6_intent_always_sent_and_read(self):
        body = method_body(self.bridge, "BroadcastNametags")
        self.assertIn("new Param3<int, int, string>(low, high, inst.m_Intent)", body)
        self.assertNotIn('intentLine != ""', body)
        read = method_body(self.bridge, "ReadIntent")
        self.assertIn("m_Intent = line;", read)
        self.assertNotIn('if (line != "")', read)

    def test_hud4_remove_before_body_swap(self):
        for fn, new in (("CmdSpawn", "ai"), ("CmdAdoptNearest", "nearest")):
            body = method_body(self.bridge, fn)
            with self.subTest(fn=fn):
                rm = body.index("SendNametagRemove(m_Npc, m_NpcName)")
                reset = body.index("ResetBodyState()")
                swap = body.index(f"m_Npc = {new};")
                self.assertLess(rm, swap)
                self.assertLess(reset, swap)
        reset_body = method_body(self.bridge, "ResetBodyState")
        for stmt in ("m_Following = false;", "m_Slinged = false;", 'm_Intent = "";', "ReleaseMeleeLock();"):
            self.assertIn(stmt, reset_body)

    def test_hud7_fight_label_only_while_running(self):
        body = method_body(self.bridge, "ActionLabelId")
        self.assertIn('if (m_CmdAction == "engage" && m_CmdStatus == "running")', body)

    def test_c3_engage_detail_format(self):
        detail = method_body(self.bridge, "EngageResultDetail")
        self.assertIn('return text + "|SCHUSS=" + fired + "|HAND=" + handType + "|ESK=" + m_EngageEscalated;',
                      detail)
        self.assertIn("Isu_FiredSince(m_EngageStartTime)", detail)
        engage = method_body(self.bridge, "CmdEngage")
        self.assertIn('if (cmd.text == "melee")', engage)
        self.assertIn("IsuAgentRegistry.SetMeleeLock(m_Npc, true)", engage)
        update = method_body(self.bridge, "UpdateEngage")
        self.assertIn('EngageResultDetail("Ziel eliminiert")', self.bridge_strs)
        self.assertIn("now - m_EngageStartTime > 175", update)
        self.assertNotIn("m_CmdStartTime", update)
        # Eskalation vor dem Fighting-FSM-Return, sonst liefe sie im Kampf nie
        self.assertLess(update.index("EscalateEngage("), update.index("if (m_Npc.m_eAI_IsFightingFSM)"))
        self.assertIn("m_Npc.m_eAI_AcuteDangerTargetCount >= 3", update)
        self.assertIn('m_Npc.GetHealth01("", "") < 0.5', update)

    def test_melee_lock_released_on_every_exit(self):
        update = method_body(self.bridge, "UpdateEngage")
        self.assertEqual(update.count("ReleaseMeleeLock();"), 3)   # Tod, Ziel tot, Timeout
        dispatch = method_body(self.bridge, "Dispatch")
        self.assertLess(dispatch.index("ReleaseMeleeLock();"), dispatch.index("m_EngageTarget = null;"))
        for fn in ("PlayerHalt", "PlayerGoto", "ResetForPlayerCmd", "EscalateEngage"):
            with self.subTest(fn=fn):
                self.assertIn("ReleaseMeleeLock();", method_body(self.bridge, fn))
        forget = method_body(_read(PATCHES_C), "ForgetBody")
        self.assertIn("s_MeleeLock.Remove(midx);", forget)
        self.assertIn("ForgetBody(ai);", method_body(_read(PATCHES_C), "Unregister"))

    def test_guard_patch_shape(self):
        self.assertIn("modded class eAIState_TakeItemToHands", self.patches)
        guard = method_body(_read(PATCHES_C), "Guard")   # erster Guard: Dormant
        self.assertIn("return eAITransition.FAIL;", guard)
        block = self.patches[self.patches.index("modded class eAIState_TakeItemToHands"):]
        self.assertIn("override int Guard()", block)
        self.assertIn("IsuAgentRegistry.HasMeleeLock(unit)", block)
        self.assertIn("unit.eAI_ShouldBandage()", block)
        self.assertIn("return super.Guard();", block)

    def test_c4_shot_counter(self):
        self.assertRegex(self.protocol, r"\bint shots;")
        self.assertRegex(self.protocol, r"\bstring last_shot_weapon;")
        state = method_body(self.bridge, "WriteState")
        self.assertIn("state.npc.shots = IsuAgentRegistry.ShotCount(m_Npc);", state)
        self.assertIn("state.npc.last_shot_weapon = IsuAgentRegistry.LastShotWeapon(m_Npc);", state)
        block = self.patches[self.patches.index("modded class Weapon_Base"):]
        self.assertIn("override void EEFired(int muzzleType, int mode, string ammoType)", block)
        self.assertLess(block.index("super.EEFired(muzzleType, mode, ammoType);"), block.index("CountShot("))
        self.assertIn("GetGame().IsServer()", block)
        self.assertIn("IsuAgentRegistry.IsAgent(isuShooter)", block)

    def test_c6_side_command(self):
        m = re.search(r"class IsuSideCommand\s*\{(.*?)\}", self.protocol, re.S)
        self.assertIsNotNone(m)
        for field in ("string id;", "string action;", "string status;", "string detail;"):
            self.assertIn(field, m.group(1))
        self.assertIn("ref IsuSideCommand side_command;", self.protocol)
        self.assertIn("side_command = new IsuSideCommand();", self.protocol)
        side = method_body(self.bridge, "IsSideAction")
        # C6-Kern (say/intent/ping) plus die ebenso laufzeitlosen RPC-Aktionen
        self.assertEqual(sorted(re.findall(r'action == "(\w+)"', side)),
                         ["bubble", "intent", "ping", "say", "say_voice"])
        dispatch = method_body(self.bridge, "Dispatch")
        self.assertLess(dispatch.index("DispatchSide(cmd);"), dispatch.index("verdraengt"))
        self.assertIn('if (m_CmdStatus == "running" && IsSideAction(cmd.action))', dispatch)
        ds = method_body(self.bridge, "DispatchSide")
        self.assertNotIn("m_CmdId =", ds)
        self.assertNotIn("m_CmdAction =", ds)
        self.assertIn("m_CmdStatus = mainStatus;", ds)
        state = method_body(self.bridge, "WriteState")
        for key in ("id", "action", "status", "detail"):
            self.assertIn(f"state.side_command.{key} = m_SideCmd", state)

    def test_c7_give_item_ground_fallback(self):
        body = method_body(self.bridge, "CmdGiveItem")
        self.assertIn('m_CmdDetail = "am Boden: " + cmd.text;', body)
        self.assertLess(body.index("CreateInInventory"), body.index("CreateObjectEx"))

    def test_c8_equip_on_foot_and_vehicle_abort(self):
        for fn in ("CmdEquip", "CmdEquipBest"):
            with self.subTest(fn=fn):
                body = method_body(self.bridge, fn)
                self.assertIn("if (!NpcReadyOnFoot())", body)
                self.assertNotIn("if (!NpcReady())", body)
        retry = method_body(self.bridge, "UpdateEquipRetry")
        self.assertIn("m_Npc.IsInTransport()", retry)

    def test_hands_free_never_drops_firearms(self):
        body = method_body(self.bridge, "EnsureHandsFree")
        self.assertLess(body.index("Weapon_Base.Cast(inHands)"), body.index("eAI_DropItem(inHands, true)"))
        self.assertLess(body.index("m_Npc.IsInTransport()"), body.index("eAI_DropItem(inHands, true)"))
        blocked = method_body(self.bridge, "HandsBlockedDetail")
        self.assertIn("kein Platz", blocked)
        self.assertIn("m_HandsBlockedType", blocked)
        self.assertIn("vorher abgelegt: ", method_body(self.bridge, "AppendHandDropNote"))

    def test_python_substring_contracts_kept(self):
        # Prüfstellen in daemon\ hängen an diesen Teilstrings
        self.assertIn('" haengt im Inventar fest (Reservierung)"', self.bridge_strs)
        self.assertIn('"Ziel eliminiert"', self.bridge_strs)
        self.assertIn('"kein Container mit Stauraum', self.bridge_strs)
        self.assertIn('"keine brauchbare Waffe im Inventar', self.bridge_strs)

    def test_fighting_flag_filters_item_targets(self):
        state = method_body(self.bridge, "WriteState")
        self.assertIn("m_Npc.TargetCount() > 0", state)
        self.assertIn("!isuFightTgt.IsItem()", state)
        self.assertNotIn("state.npc.fighting = m_Npc.m_eAI_IsFightingFSM;", state)

    def test_reload_skips_busy_weapons(self):
        reload_body = method_body(self.bridge, "CmdReload")
        self.assertLess(reload_body.index("IsWeaponBusy(w)"), reload_body.index("ReloadWeaponFromPiles("))
        self.assertIn("laedt gerade selbst nach", self.bridge_strs)
        piles = method_body(self.bridge, "ReloadWeaponFromPiles")
        self.assertIn("if (wpn.IsIdle())\n\t\t\twpn.RandomizeFSMState();", piles.replace("\r\n", "\n"))
        self.assertNotIn("oder alles ist schon voll", self.bridge_strs)

    def test_pickup_arrival_and_regroup_retarget(self):
        walk = method_body(self.bridge, "UpdateWalk")
        self.assertIn("arriveDist = 1.4;", walk)
        self.assertIn("rnow - m_RegroupStart > 300", walk)
        self.assertIn("Dist2D(m_RegroupTarget.GetPosition(), m_MoveTarget) > 10.0", walk)
        regroup = method_body(self.bridge, "CmdRegroup")
        self.assertIn("IsuAgentRegistry.FindByName(cmd.text)", regroup)
        # NIE GetIdentity() auf das (evtl. eAI-)Ziel: nur noch auf echte Spieler
        self.assertNotIn("target.GetIdentity()", regroup)

    def test_regroup_retarget_keeps_progress_clock(self):
        # Das Nachführen (alle 5 s bei bewegtem Ziel) darf den Fortschritts-
        # Zeitstempel nicht zurücksetzen, sonst greifen auto-unstick (20 s) und
        # der 45-s-Abbruch bei einem regroup zu einem laufenden Ziel nie.
        walk = method_body(self.bridge, "UpdateWalk").replace("\r\n", "\n")
        self.assertRegex(
            walk,
            r"float rKeepProgress = m_LastProgressTime;\s*"
            r"m_LastRegroupRetarget = rnow;\s*"
            r"StartWalk\(m_RegroupTarget\.GetPosition\(\)\);\s*"
            r"m_LastProgressTime = rKeepProgress;\s*return;",
        )
        # Selbstheilung und Abbruch hängen weiter an m_LastProgressTime
        self.assertIn("now - m_LastProgressTime > 20 && !m_TriedUnstick", walk)
        self.assertIn("now - m_LastProgressTime > 45", walk)
        # m_TriedUnstick fasst StartWalk nicht an (sonst Endlos-Unstick)
        self.assertNotIn("m_TriedUnstick", method_body(self.bridge, "StartWalk"))


# reference\ (DayZ-/Expansion-Fremdquellen) liegt nur im privaten Projekt, die
# Public-Kopie schließt den Ordner aus - dort überspringen statt scheitern.
@unittest.skipUnless(EXP_AI.is_dir() and VANILLA.is_dir(),
                     "reference/ nicht vorhanden (Fremdquellen, separat geklont)")
class ReferenceSignaturesW4(unittest.TestCase):
    """Die neu genutzten Engine-/Expansion-APIs stehen so in reference\\."""

    def _ref(self, rel: str) -> str:
        return (REPO / "reference" / rel).read_text(encoding="utf-8", errors="replace")

    def test_expansion_apis(self):
        tih = (EXP_AI / "Classes" / "FSM" / "states" / "eaistate_takeitemtohands.c").read_text(encoding="utf-8")
        self.assertIn("class eAIState_TakeItemToHands: eAIState_TakeItem_Base", tih)
        self.assertIn("int Guard()", tih)
        self.assertIn("unit.eAI_ShouldBandage()", tih)
        state = (EXP_AI / "Classes" / "FSM" / "eAIState.c").read_text(encoding="utf-8")
        self.assertIn("eAIBase unit;", state)
        wpn = (EXP_AI / "Entities" / "Weapons" / "Firearms" / "Weapon_Base.c").read_text(encoding="utf-8")
        self.assertIn("float m_eAI_LastFiredTime;", wpn)
        self.assertIn("override void EEFired(int muzzleType, int mode, string ammoType)", wpn)
        base = (EXP_AI / "Entities" / "AI" / "eAIBase.c").read_text(encoding="utf-8", errors="replace")
        for sig in ("bool eAI_WeaponSelection(ItemBase currentItem, ItemBase compareItem)",
                    "int m_eAI_AcuteDangerTargetCount;", "bool m_eAI_IsFightingFSM;",
                    "eAITarget GetTarget(int index = 0)", "int TargetCount()",
                    "bool eAI_ShouldBandage()", "class eAIBase: PlayerBase"):
            with self.subTest(sig=sig):
                self.assertIn(sig, base)
        target = (EXP_AI / "Classes" / "eAITarget.c").read_text(encoding="utf-8")
        self.assertIn("bool IsItem()", target)

    def test_vanilla_apis(self):
        wb = (VANILLA / "4_world" / "entities" / "firearms" / "weapon_base.c").read_text(encoding="utf-8", errors="replace")
        for sig in ("void EEFired(int muzzleType, int mode, string ammoType)", "bool IsIdle()",
                    "void ValidateAndRepair()", "void RandomizeFSMState()"):
            with self.subTest(sig=sig):
                self.assertIn(sig, wb)
        wm = (VANILLA / "4_world" / "classes" / "weapons" / "weaponmanager.c").read_text(encoding="utf-8", errors="replace")
        self.assertIn("bool IsRunning()", wm)
        obj = (VANILLA / "3_game" / "entities" / "object.c").read_text(encoding="utf-8", errors="replace")
        self.assertIn("proto native float  GetHealth01(string zoneName, string healthType);", obj)
        game = (VANILLA / "3_game" / "global" / "game.c").read_text(encoding="utf-8", errors="replace")
        self.assertRegex(game, r"proto native bool\s+IsServer\(\);")


if __name__ == "__main__":
    unittest.main()
