"""Offline regression checks for the native Arena UI contract.

Run ``python tools/check_ui_contract.py`` from any directory. Only repository
sources/layouts are read; no game, server, model, account or settings are used.
The geometry model covers exact/relative layout coordinates and a configurable
engine load scale. It evaluates the scalar normalization expressions in the
actual Enforce source. It is deliberately NOT an Enforce compiler or a native
renderer: font metrics, engine skins and input delivery still need in-game QA.
"""

from __future__ import annotations

import ast
import copy
from dataclasses import dataclass, field
import math
from pathlib import Path
import re
import shlex
import unittest


REPO = Path(__file__).resolve().parents[1]
GUI = REPO / "mod/IsuVoice/GUI"
SCRIPTS = REPO / "mod/IsuVoice/scripts"
ARENA = SCRIPTS / "5_Mission/IsuVoice/IsuArenaMenu.c"
BASELINE = REPO / "_BACKUP_20260908_menu_redesign/mod/IsuVoice/scripts/5_Mission/IsuVoice/IsuArenaMenu.c"
ANTIGRAVITY_BASELINE = REPO / "_BACKUP_20260908_antigravity/mod/IsuVoice/scripts/5_Mission/IsuVoice/IsuArenaMenu.c"
SOURCE = ARENA.read_text(encoding="utf-8-sig")
RESOLUTIONS = ((1280, 720), (1366, 768), (1920, 1080), (2560, 1440))
ENGINE_SCALES = (1.0, 4.0 / 3.0, 1.5)
# Confirmed by the installed `agy models` catalog on 2026-09-08.
ANTIGRAVITY_MODELS = (
    "default", "gemini-3.8-flash-high", "gemini-3.8-flash-medium", "gemini-3.8-flash-low",
    "gemini-3.7-flash-high", "gemini-3.7-flash-medium", "gemini-3.7-flash-low",
    "gemini-3.6-flash-high", "gemini-3.6-flash-medium", "gemini-3.6-flash-low",
    "gemini-3.1-pro-high", "gemini-3.1-pro-low", "claude-sonnet-4-6",
    "claude-opus-4-6-thinking", "gpt-oss-120b-medium",
)
CAPTION_FIELDS = {
    "RowLblActive": "BtnAgent", "RowLblName": "NameBg",
    "RowLblProvider": "ProviderHead", "RowLblModel": "ModelHead",
    "RowLblRole": "RoleHead", "RowLblLoadout": "LoadoutHead",
    "RowLblVoice": "VoiceHead", "RowLblLang": "LangHead",
    "RowLblLive": "LiveText",
}
WIRE_ARRAYS = (
    "s_AgentIds", "s_DefaultNames", "s_Names", "s_Providers",
    "s_AnthropicModels", "s_OpenAIModels", "s_GoogleModels", "s_XaiModels",
    "s_LocalModels", "s_CodexModels", "s_GeminiCliModels", "s_MistralModels",
    "s_MoonshotModels", "s_PersonaKeys", "s_LoadoutFiles", "s_VoiceNames",
    "s_LangCodes", "s_MissionIds", "s_IdleValues", "s_TurnValues", "s_SafeKeyCodes",
)


def without_comments(source: str) -> str:
    # Keep string literals intact, including // in paths and escaped quotes.
    return re.sub(r'"(?:\\.|[^"\\])*"|//[^\n]*|/\*[\s\S]*?\*/',
                  lambda m: m[0] if m[0].startswith('"') else " " * len(m[0]), source)


def matching(text: str, start: int, opening: str = "(", closing: str = ")") -> int:
    depth = 0
    quoted = escaped = False
    for pos in range(start, len(text)):
        char = text[pos]
        if quoted:
            if escaped:
                escaped = False
            elif char == "\\":
                escaped = True
            elif char == '"':
                quoted = False
        elif char == '"':
            quoted = True
        elif char == opening:
            depth += 1
        elif char == closing:
            depth -= 1
            if depth == 0:
                return pos
    raise AssertionError(f"Unclosed {opening!r} at character {start}")


def function(source: str, name: str) -> str:
    source = without_comments(source)
    hit = re.search(r"\b" + re.escape(name) + r"\([^;{}]*\)\s*\{", source)
    if not hit:
        raise AssertionError(f"Required source function {name} is missing")
    start = source.index("{", hit.start())
    return source[start + 1:matching(source, start, "{", "}")]


def arguments(text: str) -> list[str]:
    result, start, depth = [], 0, 0
    for pos, char in enumerate(text):
        depth += char in "(["
        depth -= char in ")]"
        if char == "," and depth == 0:
            result.append(text[start:pos].strip())
            start = pos + 1
    return result + [text[start:].strip()]


def calls(body: str, name: str):
    for hit in re.finditer(r"\b" + re.escape(name) + r"\s*\(", body):
        start = body.index("(", hit.start())
        end = matching(body, start)
        yield hit.start(), arguments(body[start + 1:end])


def array_values(source: str, name: str) -> list[str]:
    hit = re.search(r"\b" + re.escape(name) + r"\s*=\s*\{([^}]+)\}", without_comments(source))
    if not hit:
        raise AssertionError(f"Required initialized array {name} is missing")
    # These arrays contain scalar IDs/numbers, never expressions with commas.
    return [part.strip() for part in re.findall(r'"(?:\\.|[^"\\])*"|[^,\s]+', hit[1])]


def arithmetic(expression: str, env: dict[str, float]) -> float:
    """Evaluate a whitelist of arithmetic AST nodes; never eval project code."""
    expression = expression.replace("IsuArenaMenu.", "")
    tree = ast.parse(expression, mode="eval")

    def visit(node):
        if isinstance(node, ast.Expression):
            return visit(node.body)
        if isinstance(node, ast.Constant) and isinstance(node.value, (int, float)):
            return node.value
        if isinstance(node, ast.Name):
            return env[node.id]
        if isinstance(node, ast.UnaryOp) and isinstance(node.op, ast.USub):
            return -visit(node.operand)
        if isinstance(node, ast.UnaryOp) and isinstance(node.op, ast.Not):
            return not visit(node.operand)
        if isinstance(node, ast.Compare) and len(node.ops) == 1:
            left, right = visit(node.left), visit(node.comparators[0])
            if isinstance(node.ops[0], ast.Lt): return left < right
            if isinstance(node.ops[0], ast.Gt): return left > right
        if isinstance(node, ast.BinOp):
            left, right = visit(node.left), visit(node.right)
            if isinstance(node.op, ast.Add): return left + right
            if isinstance(node.op, ast.Sub): return left - right
            if isinstance(node.op, ast.Mult): return left * right
            if isinstance(node.op, ast.Div): return left / right
        if isinstance(node, ast.Call) and isinstance(node.func, ast.Attribute):
            if isinstance(node.func.value, ast.Name) and node.func.value.id == "Math":
                functions = {"Min": min, "Max": max, "Floor": math.floor, "Round": round}
                if node.func.attr in functions:
                    return functions[node.func.attr](*(visit(arg) for arg in node.args))
        raise AssertionError(f"Unsupported source arithmetic: {expression!r}")

    return float(visit(tree))


@dataclass
class Widget:
    name: str
    kind: str
    props: dict[str, list[str]] = field(default_factory=dict)
    children: list["Widget"] = field(default_factory=list)

    def walk(self):
        yield self
        for child in self.children:
            yield from child.walk()

    def find(self, name: str) -> "Widget":
        found = next((node for node in self.walk() if node.name == name), None)
        if found is None:
            raise AssertionError(f"Widget {name!r} missing beneath {self.name}")
        return found

    def pair(self, key: str) -> tuple[float, float]:
        return tuple(map(float, self.props.get(key, ["0", "0"])))

    def flag(self, key: str) -> bool:
        return self.props.get(key, ["0"])[0] == "1"

    def set_pair(self, key: str, pair) -> None:
        self.props[key] = [str(value) for value in pair]


def layout(filename: str) -> Widget:
    path = GUI / filename
    stack, roots = [], []
    for number, line in enumerate(path.read_text(encoding="utf-8-sig").splitlines(), 1):
        tokens = shlex.split(line, comments=False)
        if not tokens or tokens[0].startswith("//"):
            continue
        if tokens[0].endswith("WidgetClass"):
            if tokens[-1] != "{":
                raise AssertionError(f"{path}:{number}: unsupported widget declaration")
            node = Widget(tokens[1], tokens[0])
            parent = next((entry for entry in reversed(stack) if entry), None)
            (parent.children if parent else roots).append(node)
            stack.append(node)
        elif tokens == ["{"]:
            stack.append(None)
        elif tokens == ["}"]:
            if not stack:
                raise AssertionError(f"{path}:{number}: unmatched closing brace")
            stack.pop()
        else:
            node = next((entry for entry in reversed(stack) if entry), None)
            if node is None:
                raise AssertionError(f"{path}:{number}: property outside widget")
            node.props[tokens[0]] = tokens[1:]
    if stack or len(roots) != 1:
        raise AssertionError(f"{path}: malformed widget tree")
    return roots[0]


def scale_tree(root: Widget, ratio: float) -> None:
    for node in root.walk():
        x, y = node.pair("position")
        width, height = node.pair("size")
        node.set_pair("position", (x * ratio if node.flag("hexactpos") else x,
                                    y * ratio if node.flag("vexactpos") else y))
        node.set_pair("size", (width * ratio if node.flag("hexactsize") else width,
                                height * ratio if node.flag("vexactsize") else height))


def rect(node: Widget, parent_size: tuple[float, float]) -> tuple[float, float, float, float]:
    values = (*node.pair("position"), *node.pair("size"))
    flags = ("hexactpos", "vexactpos", "hexactsize", "vexactsize")
    return tuple(value if node.flag(flag) else value * parent_size[i % 2]
                 for i, (value, flag) in enumerate(zip(values, flags)))


class SourceGeometry:
    """Only the numerical geometry subset, extracted from the current source.

    Guards in these functions reject null/zero widgets and tiny screens. All
    modeled widgets have positive size and every screen exceeds those guards.
    Unsupported geometry arithmetic fails loudly rather than assuming success.
    """

    def __init__(self, source: str):
        self.source = source
        self.constants = {name: float(value) for name, value in re.findall(
            r"static const float\s+(\w+)\s*=\s*([\d.]+)\s*;", source)}
        for name in ("MENU_BASE_W", "MENU_BASE_H", "ROW_W", "ROW_H", "ROW_PITCH", "ROW_VIEW_H"):
            if name not in self.constants:
                raise AssertionError(f"Missing geometry constant {name}")

    def ratio(self, method: str, target: str, root: Widget, ui_scale: float,
              screen: tuple[int, int] = (1920, 1080)) -> tuple[float, float]:
        body = function(self.source, method)
        hits = [(pos, args) for pos, args in calls(body, "ScaleWidgetTree") if args[0] == target]
        if len(hits) != 1:
            raise AssertionError(f"{method}: expected one ScaleWidgetTree({target},...) call")
        pos, args = hits[0]
        prefix = body[:pos]
        env = {**self.constants, "m_UiScale": ui_scale,
               "screenW": screen[0], "screenH": screen[1]}
        for getter, getter_args in re.findall(
                re.escape(target) + r"\.(GetSize|GetScreenSize)\(([^)]+)\)", prefix):
            names = arguments(getter_args)
            env[names[0]], env[names[1]] = root.pair("size")
        # Assignments are evaluated in source order. GetSize outputs are already
        # available; positive-dimension fallback assignments are intentionally
        # superseded by the following guarded normal-path assignment.
        for hit in re.finditer(r"\b(\w+)\s*=\s*([^;{}]+);", prefix):
            name, expression = hit.groups()
            try:
                env[name] = arithmetic(expression, env)
            except (KeyError, SyntaxError, AssertionError):
                # UI objects, strings and state assignments are outside this
                # numerical model. A needed skipped value fails at ratio eval.
                continue
        return arithmetic(args[1], env), arithmetic(args[2], env)

    def initialize(self, root: Widget) -> float:
        ratio, target = self.ratio("Init", "layoutRoot", root, 1.0)
        scale_tree(root, ratio)
        return target

    def fit(self, root: Widget, scale: float, screen: tuple[int, int]) -> float:
        ratio, target = self.ratio("FitToScreen", "layoutRoot", root, scale, screen)
        scale_tree(root, ratio)
        return target

    def new_row(self, template: Widget, engine_scale: float, ui_scale: float) -> Widget:
        row = copy.deepcopy(template)
        scale_tree(row, engine_scale)
        ratio, _ = self.ratio("MakeRow", "row.m_Root", row, ui_scale)
        scale_tree(row, ratio)
        return row

    def relayout(self, root: Widget, rows: list[Widget], scale: float) -> None:
        body = function(self.source, "RelayoutRows")
        pos = re.search(r"m_Rows\[i\]\.m_Root\.SetPos\(([^;]+)\);", body)
        size = re.search(r"m_Rows\[i\]\.m_Root\.SetSize\(([^;]+)\);", body)
        if not pos or not size:
            raise AssertionError("RelayoutRows: source setters changed; update the numerical adapter")
        env = {**self.constants, "m_UiScale": scale}
        host = root.find("RowsHost")
        host.children = rows
        for index, row in enumerate(rows):
            env["i"] = index
            row.set_pair("position", [arithmetic(e, env) for e in arguments(pos[1])])
            row.set_pair("size", [arithmetic(e, env) for e in arguments(size[1])])
        height = re.search(r"float contentH\s*=\s*([^;]+);", body)
        guard = re.search(r"if\s*\((contentH[^)]+)\)\s*contentH\s*=\s*([^;]+);", body)
        host_size = re.search(r"m_RowsHost\.SetSize\(([^;]+)\);", body)
        if not height or not guard or not host_size:
            raise AssertionError("RelayoutRows: source content-height contract changed")
        env["count"] = len(rows)
        env["contentH"] = arithmetic(height[1].replace("m_Rows.Count()", "count"), env)
        if arithmetic(guard[1], env):
            env["contentH"] = arithmetic(guard[2], env)
        host.set_pair("size", [arithmetic(e, env) for e in arguments(host_size[1])])


class UiContract(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.menu = layout("isu_arena_menu.layout")
        cls.row = layout("isu_arena_row.layout")
        cls.popup = layout("isu_dd_item.layout")
        cls.geometry = SourceGeometry(SOURCE)

    def assert_close(self, actual, expected, message=""):
        self.assertTrue(math.isclose(actual, expected, abs_tol=1e-5),
                        message or f"{actual} != {expected}")

    def test_layout_sizes_match_runtime_constants(self):
        c = self.geometry.constants
        self.assertEqual(self.menu.pair("size"), (c["MENU_BASE_W"], c["MENU_BASE_H"]))
        self.assertEqual(self.row.pair("size"), (c["ROW_W"], c["ROW_H"]))
        self.assertEqual(self.menu.find("ArenaScroll").pair("size"), (c["ROW_SCROLL_W"], c["ROW_VIEW_H"]))
        self.assertGreater(c["ROW_PITCH"], c["ROW_H"])
        self.assertGreaterEqual(c["ROW_VIEW_H"], c["ROW_H"] * 3 + (c["ROW_PITCH"] - c["ROW_H"]) * 2)

    def test_roster_enables_native_vertical_scroll_and_reserves_gutter(self):
        scroll = self.menu.find("ArenaScroll")
        self.assertEqual(scroll.props["Scrollbar V"], ["1"])
        self.assertEqual(scroll.props["ignorepointer"], ["0"])
        self.assertEqual(scroll.props["style"], ["blank"])
        self.assertGreaterEqual(scroll.pair("size")[0] - self.row.pair("size")[0], 24)

    def test_scroll_refresh_preserves_position_and_reveals_added_survivor(self):
        rebuild = function(SOURCE, "BuildRows")
        self.assertLess(rebuild.index("GetVScrollPos01"), rebuild.index(".Destroy()"))
        self.assertLess(rebuild.index("RelayoutRows()"), rebuild.index("VScrollToPos01"))
        relayout = function(SOURCE, "RelayoutRows")
        self.assertLess(relayout.index("m_RowsHost.SetSize"), relayout.index("m_RowsHost.Update()"))
        self.assertLess(relayout.index("m_RowsHost.Update()"), relayout.index("m_ArenaScroll.Update()"))
        self.assertLess(relayout.index("m_ArenaScroll.Update()"), relayout.index("VScrollToPos01"))
        add = function(SOURCE, "AddNextSlot")
        self.assertLess(add.index("BuildRows()"), add.index("VScrollToPos01(1.0)"))

    def test_mouse_wheel_reaches_every_survivor_without_overscrolling(self):
        body = function(SOURCE, "OnMouseWheel")
        self.assertIn("parent != m_ArenaScroll", body)
        self.assertIn("parent.GetParent()", body)
        self.assertIn("return true", body)
        self.assertIn("maxScroll <= 0", body)
        self.assertIn("Math.Clamp(next, 0.0, 1.0)", body)
        max_expr = re.search(r"float maxScroll = ([^;]+);", body)[1].replace("m_Rows.Count()", "count")
        next_expr = re.search(r"float next = ([^;]+);", body)[1].replace("m_ArenaScroll.GetVScrollPos01()", "position")
        c = self.geometry.constants
        for count in range(4, 11):
            env = {**c, "count": count, "position": 0, "wheel": -1}
            env["maxScroll"] = arithmetic(max_expr, env)
            self.assertGreater(env["maxScroll"], 0)
            for _ in range(count + 1):
                env["position"] = max(0, min(1, arithmetic(next_expr, env)))
            self.assertEqual(env["position"], 1)
            bottom = (count - 1) * c["ROW_PITCH"] + c["ROW_H"] - env["maxScroll"] * env["position"]
            self.assert_close(bottom, c["ROW_VIEW_H"])
            env["wheel"] = 1
            for _ in range(count + 1):
                env["position"] = max(0, min(1, arithmetic(next_expr, env)))
            self.assertEqual(env["position"], 0)

    def test_caption_columns_and_name_inset(self):
        for caption, field_name in CAPTION_FIELDS.items():
            with self.subTest(field=field_name):
                label, control = self.row.find(caption), self.row.find(field_name)
                self.assertEqual(label.pair("position")[0], control.pair("position")[0])
                self.assertEqual(label.pair("size")[0], control.pair("size")[0])
                self.assertLessEqual(sum((label.pair("position")[1], label.pair("size")[1])), control.pair("position")[1])
        x, y, width, height = rect(self.row.find("NameBg"), self.row.pair("size"))
        ex, ey, ew, eh = rect(self.row.find("EditName"), self.row.pair("size"))
        self.assertGreaterEqual(ex, x)
        self.assertLessEqual(ex + ew, x + width)
        self.assertEqual((ey, eh), (y, height))

    def test_all_fields_within_their_layout_and_no_duplicates(self):
        def inspect(node):
            for child in node.children:
                x, y, w, h = rect(child, node.pair("size"))
                if child.props.get("visible") != ["0"]:
                    self.assertGreaterEqual(min(x, y), -0.01, child.name)
                    self.assertLessEqual(x + w, node.pair("size")[0] + 0.01, child.name)
                    self.assertLessEqual(y + h, node.pair("size")[1] + 0.01, child.name)
                inspect(child)
        for template in (self.menu, self.row, self.popup):
            names = [node.name for node in template.walk()]
            self.assertEqual(len(names), len(set(names)), template.name)
            inspect(template)

    def check_row(self, row: Widget, scale: float, viewport_width: float):
        base_nodes = {node.name: node for node in self.row.walk()}
        for child in row.walk():
            if child is row:
                continue
            base = base_nodes[child.name]
            for actual, expected in zip((*child.pair("position"), *child.pair("size")),
                                        (*base.pair("position"), *base.pair("size"))):
                self.assert_close(actual, expected * scale, f"{child.name}: {actual} != {expected} * {scale}; fresh-row scaling drift")
            x, _, width, _ = rect(child, row.pair("size"))
            self.assertLessEqual(x + width, viewport_width + 1e-5, child.name)

    def test_engine_scale_init_add_remove_and_reopen(self):
        for engine_scale in ENGINE_SCALES:
            for screen in RESOLUTIONS:
                with self.subTest(engine_scale=engine_scale, screen=screen):
                    root = copy.deepcopy(self.menu)
                    scale_tree(root, engine_scale)
                    scale = self.geometry.initialize(root)
                    rows = [self.geometry.new_row(self.row, engine_scale, scale)]
                    self.geometry.relayout(root, rows, scale)
                    scale = self.geometry.fit(root, scale, screen)
                    self.geometry.relayout(root, rows, scale)
                    self.check_row(rows[0], scale, root.find("ArenaScroll").pair("size")[0])
                    for count in (2, 3, 10, 9, 3, 1, 2):
                        # BuildRows recreates ALL rows for both add and remove.
                        rows = [self.geometry.new_row(self.row, engine_scale, scale) for _ in range(count)]
                        self.geometry.relayout(root, rows, scale)
                        for row in rows:
                            self.check_row(row, scale, root.find("ArenaScroll").pair("size")[0])
                        host_h = root.find("RowsHost").pair("size")[1]
                        self.assertLessEqual(rows[-1].pair("position")[1] + rows[-1].pair("size")[1], host_h + 1e-5)
                        # At maximal scroll the last row must be fully visible.
                        view_h = root.find("ArenaScroll").pair("size")[1]
                        offset = max(0, host_h - view_h)
                        self.assertLessEqual(rows[-1].pair("position")[1] - offset + rows[-1].pair("size")[1], view_h + 1e-5)
                    before = root.pair("size")
                    for _ in range(4):
                        scale = self.geometry.fit(root, scale, screen)
                        self.geometry.relayout(root, rows, scale)
                        self.check_row(rows[0], scale, root.find("ArenaScroll").pair("size")[0])
                    for actual, expected in zip(root.pair("size"), before):
                        self.assert_close(actual, expected, "Reopening changed menu dimensions")
                    self.assertLessEqual(root.pair("size")[0], screen[0] - 32 + 1e-5)
                    self.assertLessEqual(root.pair("size")[1], screen[1] - 32 + 1e-5)

    def test_negative_control_detects_original_fresh_row_bug(self):
        body = function(SOURCE, "MakeRow")
        call = next((args for _, args in calls(body, "ScaleWidgetTree") if args[0] == "row.m_Root"), None)
        self.assertIsNotNone(call)
        mutated = SOURCE.replace("ScaleWidgetTree(" + ", ".join(call) + ")",
                                 "ScaleWidgetTree(row.m_Root, m_UiScale, m_UiScale)")
        self.assertNotEqual(mutated, SOURCE, "Could not inject the historical bug into the in-memory source")
        broken = SourceGeometry(mutated).new_row(self.row, 4 / 3, 1)
        with self.assertRaises(AssertionError):
            self.check_row(broken, 1, self.row.pair("size")[0])

    def test_normalization_happens_before_children_are_rebuilt(self):
        init = function(SOURCE, "Init")
        normalize = next(pos for pos, args in calls(init, "ScaleWidgetTree") if args[0] == "layoutRoot")
        self.assertLess(normalize, init.index("BuildRows()"))
        make = function(SOURCE, "MakeRow")
        measure = make.index("row.m_Root.GetSize(")
        normalize = next(pos for pos, args in calls(make, "ScaleWidgetTree") if args[0] == "row.m_Root")
        self.assertLess(make.index("CreateWidgets("), measure)
        self.assertLess(measure, normalize)
        self.assertNotIn("row.m_Root.SetSize(", make[:measure],
                         "Factory size was overwritten before measuring the engine load factor")
        self.assertIn("FitToScreen()", function(SOURCE, "OnShow"))
        self.assertIn("RelayoutRows()", function(SOURCE, "FitToScreen"))

    def test_resolution_changes_preserve_existing_and_new_rows(self):
        for engine_scale in ENGINE_SCALES:
            root = copy.deepcopy(self.menu)
            scale_tree(root, engine_scale)
            scale = self.geometry.initialize(root)
            rows = [self.geometry.new_row(self.row, engine_scale, scale) for _ in range(3)]
            self.geometry.relayout(root, rows, scale)
            for screen in (*RESOLUTIONS, *reversed(RESOLUTIONS), RESOLUTIONS[2]):
                with self.subTest(engine_scale=engine_scale, resize=screen):
                    scale = self.geometry.fit(root, scale, screen)
                    self.geometry.relayout(root, rows, scale)
                    for row in rows:
                        self.check_row(row, scale, root.find("ArenaScroll").pair("size")[0])
                    rows.append(self.geometry.new_row(self.row, engine_scale, scale))
                    self.geometry.relayout(root, rows, scale)
                    self.check_row(rows[-1], scale, root.find("ArenaScroll").pair("size")[0])
                    rows.pop(0)

    def test_explicit_npc_language_survives_ui_language_changes(self):
        body = function(SOURCE, "ApplyChoiceLanguage")
        initial = re.search(r"int defaultLanguage\s*=\s*([^;]+);", body)
        german = re.search(r"if\s*\(IsuUiText.IsGerman\(\)\)\s*defaultLanguage\s*=\s*([^;]+);", body)
        rule = re.search(r"if\s*\((!?s_LangExplicit\[i\])\)\s*s_LangIdx\[i\]\s*=\s*([^;]+);", body)
        self.assertIsNotNone(initial, "Missing default NPC speech language")
        self.assertIsNotNone(german, "Missing German-server default")
        self.assertIsNotNone(rule, "Default speech language must respect explicit per-NPC selection")
        flags = array_values(SOURCE, "s_LangExplicit")
        codes = array_values(SOURCE, "s_LangCodes")
        self.assertEqual(len(flags), len(array_values(SOURCE, "s_AgentIds")))
        self.assertTrue(all(flag == "false" for flag in flags))
        pick = re.search(r"case DD_LANG:\s*\{([^}]+)\}", without_comments(SOURCE))
        self.assertIsNotNone(pick)
        self.assertRegex(pick[1], r"s_LangIdx\[slot\]\s*=\s*sel;")
        self.assertRegex(pick[1], r"s_LangExplicit\[slot\]\s*=\s*true;")
        # Interleave explicit German/English/third-language choices with slots
        # whose language should keep following the server's UI default.
        explicit = [index % 2 == 0 for index in range(len(flags))]
        choices = [index % len(codes) for index in range(len(flags))]
        preserved = choices.copy()
        condition = rule[1].replace("!", "not ").replace("s_LangExplicit[i]", "explicit")
        for is_german in (False, True, False, True, False):
            default = arithmetic(german[1] if is_german else initial[1], {})
            self.assertEqual(codes[int(default)], '"de"' if is_german else '"en"')
            for index in range(len(choices)):
                env = {"explicit": explicit[index], "defaultLanguage": default}
                if arithmetic(condition, env):
                    choices[index] = int(arithmetic(rule[2], env))
                self.assertEqual(choices[index], preserved[index] if explicit[index] else int(default))

    def test_wire_arrays_and_command_stay_compatible(self):
        if not BASELINE.exists():
            # Baseline = privates Backup-Verzeichnis, in der Public-Kopie nicht vorhanden.
            self.skipTest(f"Compatibility baseline missing (private backup dir): {BASELINE}")
        baseline = BASELINE.read_text(encoding="utf-8-sig")
        for name in WIRE_ARRAYS:
            with self.subTest(array=name):
                current, previous = array_values(SOURCE, name), array_values(baseline, name)
                if name == "s_Providers":
                    # The new backend is appended at index 9, never inserted.
                    self.assertEqual(current, previous + ['"Antigravity CLI"'])
                elif name == "s_GeminiCliModels":
                    # 2026-09-08: requested model choices append to CLI default.
                    # Existing selection indices must keep their old meaning.
                    self.assertEqual(current[:len(previous)], previous)
                    self.assertGreater(len(current), len(previous))
                    self.assertEqual(len(current), len(set(current)))
                    self.assertTrue(all(value.startswith('"gemini-cli/') for value in current))
                else:
                    self.assertEqual(current, previous)
        token = lambda text: re.findall(r'"(?:\\.|[^"\\])*"|\w+|[^\s]', text)
        self.assertEqual(token(function(SOURCE, "BuildCommand")), token(function(baseline, "BuildCommand")),
                         "Wire command changed: review compatibility with the supervisor")

    def test_antigravity_model_catalog_and_provider_selection(self):
        ids = array_values(SOURCE, "s_AntigravityModels")
        labels = array_values(SOURCE, "s_AntigravityLabels")
        self.assertEqual(ids, [f'"antigravity/{model}"' for model in ANTIGRAVITY_MODELS])
        self.assertEqual(len(ids), len(set(ids)))
        self.assertEqual(len(ids), len(labels))
        self.assertEqual(array_values(SOURCE, "s_Providers")[9], '"Antigravity CLI"')
        for method, array in (("ProviderModelIds", "s_AntigravityModels"),
                              ("ProviderModelLabels", "s_AntigravityLabels")):
            self.assertRegex(function(SOURCE, method), r"if\s*\(p == 9\)\s*return " + array + ";")
        self.assertIn('CheckPair("AntigravityModels/Labels", s_AntigravityModels.Count(), s_AntigravityLabels.Count())',
                      function(SOURCE, "Init"))
        self.assertIn("Thinking", labels[12], "Sonnet's Thinking mode must be visible")
        # This change must preserve the expanded Gemini catalog added earlier today.
        if not ANTIGRAVITY_BASELINE.exists():
            self.skipTest(f"Antigravity baseline missing (private backup dir): {ANTIGRAVITY_BASELINE}")
        baseline = ANTIGRAVITY_BASELINE.read_text(encoding="utf-8-sig")
        for name in ("s_GeminiCliModels", "s_GeminiCliLabels"):
            self.assertEqual(array_values(SOURCE, name), array_values(baseline, name))
        self.assertIn("ProviderModelIds(s_ProviderIdx[idx])", function(SOURCE, "BuildCommand"))
        self.assertIn("pmids.Get(ClampIdx(s_ModelIdx[idx], pmids.Count()))", function(SOURCE, "BuildCommand"))

    def test_antigravity_dropdown_all_fifteen_choices_fit_and_keep_indices(self):
        body = function(SOURCE, "BuildItemsInto")
        rows_body = function(SOURCE, "RowCount")
        row_expr = re.search(r"int rows = ([^;]+);", rows_body)[1]
        model_dd = next(args for _, args in calls(function(SOURCE, "MakeRow"), "MakeRowDd")
                        if args[1] == '"ModelHead"')
        columns = int(arithmetic(model_dd[4], {}))
        count = len(array_values(SOURCE, "s_AntigravityModels"))
        rows = int(arithmetic(row_expr, {"count": count, "m_Cols": columns}))
        self.assertEqual(count, 15)
        self.assertGreater(rows, 0)
        row_h = float(re.search(r"ITEM_ROW_H = ([\d.]+);", SOURCE)[1])
        minimum_width = float(re.search(r"colW = ([\d.]+) \* uiScale;", body)[1])
        expressions = {name: re.search(r"(?:int|float) " + name + r" = ([^;]+);", body)[1]
                       for name in ("rowH", "listW", "listH", "total", "c", "r")}
        self.assertIn("if (y + listH > menuH)", body)
        self.assertIn("y = headY - listH", body)
        self.assertIn("if (y < 0)", body)
        self.assertIn("if (x + listW > menuW)", body)
        self.assertIn("x = menuW - listW", body)
        self.assertIn("txt = m_Items[i]", body)
        self.assertIn("if (i < count)", body)
        self.assertIn("m_ItemBtns.Insert(b)", body)
        self.assertIn("m_ItemBtns[i] == w", function(SOURCE, "ItemIndex"))
        for engine_scale in ENGINE_SCALES:
            for screen in RESOLUTIONS:
                root = copy.deepcopy(self.menu)
                scale_tree(root, engine_scale)
                scale = self.geometry.initialize(root)
                scale = self.geometry.fit(root, scale, screen)
                menu_w, menu_h = root.pair("size")
                row = self.geometry.new_row(self.row, engine_scale, scale)
                head_x, head_y, head_w, head_h = rect(row.find("ModelHead"), row.pair("size"))
                scroll_x, scroll_y = root.find("ArenaScroll").pair("position")
                env = {"rows": rows, "m_Cols": columns, "uiScale": scale, "ITEM_ROW_H": row_h,
                       "colW": max(head_w, minimum_width * scale)}
                for name in ("rowH", "listW", "listH", "total"):
                    env[name] = arithmetic(expressions[name], env)
                for visible_row in range(3):
                    with self.subTest(engine=engine_scale, screen=screen, row=visible_row):
                        x = min(scroll_x + head_x, menu_w - env["listW"])
                        y = scroll_y + head_y + visible_row * self.geometry.constants["ROW_PITCH"] * scale
                        y = y - env["listH"] if y + head_h + env["listH"] > menu_h else y + head_h
                        x, y = max(0, x), max(0, y)
                        self.assertLessEqual(x + env["listW"], menu_w + 1e-5)
                        self.assertLessEqual(y + env["listH"], menu_h + 1e-5)
                        selectable = []
                        cells = set()
                        for i in range(int(env["total"])):
                            cell_env = {**env, "i": i}
                            col = int(arithmetic(expressions["c"], cell_env))
                            # Enforce integer modulo; the arithmetic whitelist deliberately excludes it.
                            self.assertEqual(expressions["r"].strip(), "i % rows")
                            r = i % rows
                            cells.add((col, r))
                            self.assertLessEqual(x + (col + 1) * env["colW"], menu_w + 1e-5)
                            self.assertLessEqual(y + (r + 1) * env["rowH"], menu_h + 1e-5)
                            if i < count:
                                selectable.append(i)
                        self.assertEqual(len(cells), int(env["total"]))
                        self.assertEqual(selectable, list(range(count)))

    def test_translation_helpers_reference_real_widgets(self):
        source = (SCRIPTS / "5_Mission/IsuVoice/IsuArenaText.c").read_text(encoding="utf-8-sig")
        entries = re.findall(r'Label\(root,\s*"([^"]+)",\s*"([^"\\]*)",\s*"([^"\\]*)"\)', source)
        self.assertGreater(len(entries), 30, "Expected bilingual static menu/caption coverage")
        names = {node.name for root in (self.menu, self.row) for node in root.walk()}
        translated = set()
        for name, english, german in entries:
            self.assertIn(name, names, f"Translation points to missing widget {name}")
            self.assertTrue(english and german, name)
            self.assertNotIn(name, translated, f"Duplicate translation for {name}")
            translated.add(name)
        for caption in CAPTION_FIELDS:
            self.assertIn(caption, translated)
        for name in ("Subtitle", "RosterNote", "HintLine1", "BtnAddNpc", "BtnStop", "CostLabel"):
            self.assertIn(name, translated)

    def test_choice_translations_and_live_refresh_are_complete(self):
        source = (SCRIPTS / "5_Mission/IsuVoice/IsuArenaText.c").read_text(encoding="utf-8-sig")
        entries = re.findall(r'IsuArenaMenu\.(s_\w+)\.Set\((\d+),\s*IsuUiText.Choose\("([^"\\]*)",\s*"([^"\\]*)"\)\);', source)
        coverage = {}
        for name, index, english, german in entries:
            self.assertTrue(english and german, name)
            self.assertLess(int(index), len(array_values(SOURCE, name)), name)
            coverage.setdefault(name, set()).add(int(index))
        for name in ("s_PersonaLabels", "s_LoadoutLabels", "s_MissionLabels", "s_LangLabels", "s_AntigravityLabels"):
            self.assertEqual(coverage.get(name), set(range(len(array_values(SOURCE, name)))), name)
        for name in ("s_CodexLabels", "s_GeminiCliLabels"):
            self.assertIn(0, coverage.get(name, set()), f"{name}: CLI default label is untranslated")
        refresh = function(SOURCE, "RefreshUiLanguage")
        for call in ("CloseAllDropdowns()", "ApplyChoiceLanguage()", "IsuArenaText.Apply(layoutRoot)", "BuildRows()"):
            self.assertIn(call, refresh)
        for field_name in ("m_DdIdle", "m_DdTurns", "m_DdMission"):
            self.assertIn(field_name + ".Rebuild(", refresh)
        self.assertIn("IsuUiText.s_Revision", refresh)
        self.assertIn("IsuUiText.Tick(timeslice)", function(SOURCE, "OnUpdate"))

    def test_radial_metrics_remove_engine_scale_and_use_screen_pixels(self):
        source = (SCRIPTS / "5_Mission/IsuVoice/IsuRadialMenu.c").read_text(encoding="utf-8-sig")
        radial = layout("isu_radial_menu.layout")
        constants = {name: float(value) for name, value in re.findall(
            r"static const float\s+(\w+)\s*=\s*([\d.]+)\s*;", source)}
        self.assertEqual(radial.pair("size"), (constants["BASE_W"], constants["BASE_H"]))
        capture = function(source, "CaptureMetrics")
        expressions = {name: re.search(r"metric\." + name + r"\s*=\s*([^;]+);", capture)[1]
                       for name in ("x", "y", "width", "height")}
        design = re.findall(r"designFactor\s*=\s*([^;]+);", function(source, "Init"))[-1]
        fit = function(source, "FitToScreen")
        scale_expression = re.search(r"m_UiScale\s*=\s*([^;]+);", fit)[1]
        pos_call = next(args for _, args in calls(fit, "SetScreenPos"))
        size_call = next(args for _, args in calls(fit, "SetScreenSize"))
        self.assertNotRegex(fit, r"metric\.(?:x|y|width|height)\s*=", "Captured radial metrics must remain immutable")
        for node in radial.walk():
            self.assertTrue(all(node.flag(flag) for flag in ("hexactpos", "vexactpos", "hexactsize", "vexactsize")), node.name)
            self.assertNotIn("halign", node.props, node.name)
            self.assertNotIn("valign", node.props, node.name)
        for engine_scale in ENGINE_SCALES:
            factor = arithmetic(design, {**constants, "createdRootW": radial.pair("size")[0] * engine_scale})
            metrics = []

            def capture_tree(node, parent=(0.0, 0.0)):
                values = dict(zip(("x", "y", "width", "height"), (*node.pair("position"), *node.pair("size"))))
                env = {key: value * engine_scale for key, value in values.items()}
                env.update(designFactor=factor, parentX=parent[0], parentY=parent[1])
                result = {key: arithmetic(expression.replace("metric.", ""), env) for key, expression in expressions.items()}
                for key in ("width", "height"):
                    self.assert_close(result[key], values[key], node.name)
                for index, key in enumerate(("x", "y")):
                    self.assert_close(result[key], values[key] + parent[index], node.name)
                metrics.append(result)
                for child in node.children:
                    capture_tree(child, (result["x"], result["y"]))

            capture_tree(radial)
            for screen in (*RESOLUTIONS, *reversed(RESOLUTIONS)):
                env = {**constants, "screenW": screen[0], "screenH": screen[1]}
                env["m_UiScale"] = arithmetic(scale_expression, env)
                for key in ("originX", "originY"):
                    expression = re.search(r"float " + key + r"\s*=\s*([^;]+);", fit)[1]
                    env[key] = arithmetic(expression, env)
                for metric in metrics:
                    geometry_env = {**env, **metric}
                    x, y = (arithmetic(expression.replace("metric.", ""), geometry_env) for expression in pos_call)
                    width, height = (arithmetic(expression.replace("metric.", ""), geometry_env) for expression in size_call)
                    self.assertGreaterEqual(min(x, y), -1e-5)
                    self.assertLessEqual(x + width, screen[0] + 1e-5)
                    self.assertLessEqual(y + height, screen[1] + 1e-5)
        self.assertEqual(array_values(source, "s_Actions"),
                         ['"follow"', '"halt"', '"comehere"', '"loot"', '"engage"', '"gotoaim"'])
        for name in ("ActionLabel", "ActionNote"):
            self.assertEqual(function(source, name).count("IsuUiText.Choose("), 7,
                             "Six radial actions plus aimed-item alternative need both languages")

    def test_script_delimiters_and_public_widget_apis(self):
        paths = [ARENA, SCRIPTS / "5_Mission/IsuVoice/IsuArenaText.c",
                 SCRIPTS / "4_World/IsuVoice/IsuUiText.c",
                 SCRIPTS / "5_Mission/IsuVoice/IsuRadialMenu.c"]
        for path in paths:
            code = without_comments(path.read_text(encoding="utf-8-sig"))
            masked = re.sub(r'"(?:\\.|[^"\\])*"', '""', code)
            stack = []
            for char in masked:
                if char in "({[": stack.append(char)
                elif char in ")}]":
                    self.assertTrue(stack, str(path))
                    self.assertEqual(stack.pop(), dict(zip(")}]", "({["))[char], str(path))
            self.assertFalse(stack, str(path))
        api_path = REPO / "reference/dayz-vanilla/scripts/1_core/proto/enwidgets.c"
        if not api_path.exists():
            self.skipTest("reference/dayz-vanilla not present (cloned separately, see docs)")
        api = api_path.read_text(encoding="utf-8-sig")
        for name in ("GetSize", "SetSize", "GetScreenSize", "GetScreenPos", "SetTextExactSize", "SetTextProportion", "GetUserID", "SetUserID"):
            self.assertRegex(api, r"\b" + name + r"\(", f"Public DayZ API missing: {name}")
        self.assertNotRegex(SOURCE, r"\.SetScaled\(", "DayZ exposes no SetScaled API")


if __name__ == "__main__":
    print("Offline UI contract: source expressions + layout geometry; no Enforce compile or native rendering claim.", flush=True)
    print(f"Lifecycle matrix: {len(ENGINE_SCALES)} engine load scales x {len(RESOLUTIONS)} resolutions; init/add/remove/reopen.", flush=True)
    unittest.main(verbosity=2)
