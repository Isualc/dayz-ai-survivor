"""Render a UI-layout preview from the native DayZ arena layout files.

This is a Pillow approximation of layout geometry, not a screenshot from DayZ.
It deliberately uses the checked-in widget positions, sizes, colours and exact
text sizes. Font metrics, focus/hover skins and engine UI scaling can differ.

Examples (run from the repository root):
    python tools/preview_arena_layout.py --size 1920x1080
    python tools/preview_arena_layout.py --size 1366x768 --dropdown model
    python tools/preview_arena_layout.py --rows 10 --scroll 280 --dropdown lang

The PNG is accompanied by JSON with dimensions, scaling and layout diagnostics.
No game, server, API or credentials are accessed.
"""

from __future__ import annotations

import argparse
import ast
import copy
from dataclasses import dataclass, field
import json
import math
from pathlib import Path
import re
import shlex
from typing import Iterator

from PIL import Image, ImageDraw, ImageFont


REPO = Path(__file__).resolve().parents[1]
GUI = REPO / "mod/IsuVoice/GUI"
SCRIPT = REPO / "mod/IsuVoice/scripts/5_Mission/IsuVoice/IsuArenaMenu.c"


@dataclass
class Widget:
    kind: str
    name: str
    props: dict[str, list[str]] = field(default_factory=dict)
    children: list["Widget"] = field(default_factory=list)
    line: int = 0

    def value(self, name: str, default: str = "") -> str:
        return " ".join(self.props.get(name, [default]))

    def number(self, name: str, default: float = 0) -> float:
        try:
            return float(self.value(name, str(default)))
        except ValueError:
            return default

    def pair(self, name: str, default: tuple[float, float] = (0, 0)) -> tuple[float, float]:
        values = self.props.get(name)
        if values and len(values) >= 2:
            return float(values[0]), float(values[1])
        return default

    def set(self, name: str, *values: object) -> None:
        self.props[name] = [str(value) for value in values]

    def walk(self) -> Iterator["Widget"]:
        yield self
        for child in self.children:
            yield from child.walk()

    def find(self, name: str) -> "Widget | None":
        return next((node for node in self.walk() if node.name == name), None)


def parse_layout(path: Path) -> Widget:
    """Parse line-oriented native layout properties and anonymous child blocks.

    shlex preserves quoted property names such as ``"exact text size"`` and
    quoted text values. Braces are parsed separately from those quoted values.
    """
    roots: list[Widget] = []
    stack: list[Widget | None] = []
    pending: Widget | None = None
    for lineno, raw in enumerate(path.read_text(encoding="utf-8-sig").splitlines(), 1):
        stripped = raw.strip()
        if not stripped or stripped.startswith("//"):
            continue
        lexer = shlex.shlex(stripped, posix=True, punctuation_chars="{}")
        lexer.whitespace_split = True
        lexer.commenters = ""
        tokens = list(lexer)
        if tokens and tokens[0].startswith("//"):
            continue
        # Inline comments occur only after complete property values in these
        # layouts. A quoted URL remains one token and is left intact.
        for index, token in enumerate(tokens):
            if token == "//":
                tokens = tokens[:index]
                break
        if not tokens:
            continue
        if tokens[0].endswith("WidgetClass"):
            if len(tokens) < 2:
                raise ValueError(f"{path}:{lineno}: widget name missing")
            node = Widget(tokens[0], tokens[1], line=lineno)
            parent = next((entry for entry in reversed(stack) if entry is not None), None)
            (parent.children if parent else roots).append(node)
            if "{" in tokens[2:]:
                stack.append(node)
            else:
                pending = node
            continue
        if all(set(token) <= {"{", "}"} for token in tokens):
            for token in tokens:
                for char in token:
                    if char == "{":
                        stack.append(pending)
                        pending = None
                    elif stack:
                        stack.pop()
                    else:
                        raise ValueError(f"{path}:{lineno}: unmatched closing brace")
            continue
        current = next((entry for entry in reversed(stack) if entry is not None), None)
        if current is None:
            raise ValueError(f"{path}:{lineno}: property outside a widget")
        current.props[tokens[0]] = tokens[1:]
    if stack or pending:
        raise ValueError(f"{path}: unclosed widget block")
    if len(roots) != 1:
        raise ValueError(f"{path}: expected one root widget, found {len(roots)}")
    return roots[0]


def local_rect(node: Widget, parent_size: tuple[float, float]) -> tuple[float, float, float, float]:
    """Resolve exact/relative sizes and common reference alignments."""
    pw, ph = parent_size
    x, y = node.pair("position")
    width, height = node.pair("size")
    if not node.number("hexactsize", 0):
        width *= pw
    if not node.number("vexactsize", 0):
        height *= ph
    if not node.number("hexactpos", 0):
        x *= pw
    if not node.number("vexactpos", 0):
        y *= ph
    if node.value("halign") == "center_ref":
        x += (pw - width) / 2
    elif node.value("halign") == "right_ref":
        x += pw - width
    if node.value("valign") == "center_ref":
        y += (ph - height) / 2
    elif node.value("valign") == "bottom_ref":
        y += ph - height
    return x, y, width, height


def template_diagnostics(root: Widget, filename: str) -> list[dict]:
    warnings: list[dict] = []
    seen: set[str] = set()
    for node in root.walk():
        if node.name in seen:
            warnings.append({"kind": "duplicate_id", "template": filename, "widget": node.name})
        seen.add(node.name)

    def inspect(node: Widget, size: tuple[float, float]) -> None:
        for child in node.children:
            x, y, width, height = local_rect(child, size)
            overflow = x < -0.1 or y < -0.1 or x + width > size[0] + 0.1 or y + height > size[1] + 0.1
            if overflow and child.name != "RowsHost" and child.number("visible", 1):
                warnings.append({"kind": "child_outside_parent", "template": filename,
                                 "widget": child.name, "parent": node.name,
                                 "rect": [x, y, width, height], "parent_size": list(size)})
            inspect(child, (width, height))

    inspect(root, root.pair("size"))
    return warnings


def source_number(source: str, name: str, default: float) -> float:
    match = re.search(r"\b" + re.escape(name) + r"\s*=\s*([0-9.]+)\s*;", source)
    return float(match[1]) if match else default


def source_strings(source: str, name: str, default: list[str]) -> list[str]:
    match = re.search(r"\b" + re.escape(name) + r"\s*=\s*\{([^}]+)\}", source)
    return re.findall(r'"([^"\\]*)"', match[1]) if match else default


def set_text(root: Widget, name: str, text: str) -> None:
    node = root.find(name)
    if node:
        node.set("text", text)


def language_data(language: str) -> tuple[dict[str, str], dict[str, list[str]]]:
    """Read generated widget translations and literal CHOICES without executing
    the layout generator or changing any native artifacts.
    """
    language_index = 1 if language == "de" else 0
    translation_path = GUI / "isu_arena_translations.json"
    translations = json.loads(translation_path.read_text(encoding="utf-8-sig"))
    generator = ast.parse((REPO / "tools/build_arena_layout.py").read_text(encoding="utf-8-sig"))
    choices = {}
    for node in generator.body:
        if isinstance(node, ast.Assign) and any(isinstance(target, ast.Name) and target.id == "CHOICES" for target in node.targets):
            choices = ast.literal_eval(node.value)
            break
    return ({name: values[language_index] for name, values in translations.items()},
            {name: list(values[language_index]) for name, values in choices.items()})


def apply_translations(root: Widget, translations: dict[str, str]) -> None:
    for node in root.walk():
        if node.name in translations:
            node.set("text", translations[node.name])


def native_translation(source: str, english: str, language: str) -> str:
    if language == "en":
        return english
    pairs = dict(re.findall(r'IsuUiText\.Choose\("([^"\\]*)", "([^"\\]*)"\)', source))
    if english not in pairs:
        raise ValueError(f"Native UI translation not found for {english!r}")
    return pairs[english]


def populate(root: Widget, row_template: Widget, args: argparse.Namespace, source: str) -> tuple[list[Widget], dict]:
    translations, choices = language_data(args.language)
    apply_translations(root, translations)
    apply_translations(row_template, translations)
    def labels(name: str, fallback: list[str]) -> list[str]:
        return choices.get(name, source_strings(source, name, fallback))
    def tr(english: str) -> str:
        return native_translation(source, english, args.language)
    host = root.find("RowsHost")
    scroll = root.find("ArenaScroll")
    if host is None or scroll is None:
        raise ValueError("ArenaScroll/RowsHost missing from menu template")
    row_width, row_height = row_template.pair("size")
    pitch = source_number(source, "ROW_PITCH", row_height + 8)
    # Native content ends at the last card, without an extra trailing gutter.
    content_height = max(scroll.pair("size")[1], (args.rows - 1) * pitch + row_height)
    max_scroll = max(0, content_height - scroll.pair("size")[1])
    offset = max_scroll if args.scroll == "max" else min(max(0, float(args.scroll)), max_scroll)
    host.set("size", host.pair("size")[0], content_height)
    host.set("position", host.pair("position")[0], -offset)
    rows: list[Widget] = []
    names = ["Viktor", "Birgit", "Igor", "Konrad", "Anna", "Boris", "Elena", "Franz", "Mila", "Sergej"]
    providers = ["Codex CLI", "Anthropic", "Mistral API", "Moonshot API", "Gemini CLI"]
    codex = labels("s_CodexLabels", ["CLI default", "6 Astra"])
    anthropic = source_strings(source, "s_AnthropicLabels", ["Fable 5.1"])
    mistral = source_strings(source, "s_MistralLabels", ["Small (latest)"])
    moonshot = source_strings(source, "s_MoonshotLabels", ["Kimi K2.6"])
    gemini = labels("s_GeminiCliLabels", ["CLI default"])
    models = [next((s for s in codex if "Astra" in s), codex[-1]),
              next((s for s in anthropic if "5.1" in s and "API" not in s), anthropic[-1]),
              mistral[0], moonshot[0], gemini[0]]
    roles = labels("s_PersonaLabels", ["Hunter", "Farmer", "Medic", "Veteran", "Fighter"])
    loadouts = labels("s_LoadoutLabels", ["Keep", "Scout", "Assault", "Medic", "Sniper", "Hunter", "Farmer", "Military", "Survivor"])
    speech_languages = labels("s_LangLabels", ["German", "English"])
    colors = [(0.94, 0.62, 0.15), (0.36, 0.79, 0.65), (0.59, 0.77, 0.35),
              (0.22, 0.54, 0.87), (0.85, 0.35, 0.55), (0.62, 0.40, 0.85),
              (0.85, 0.30, 0.25), (0.35, 0.75, 0.85), (0.65, 0.70, 0.30), (0.55, 0.60, 0.75)]
    for index in range(args.rows):
        row = copy.deepcopy(row_template)
        row.set("position", 0, index * pitch)
        # Native CreateWidgets dimensions are normalized by the game code;
        # this preview already starts in the authored 1696x80 design space.
        # Do not apply an additional engine/native scale to these dimensions.
        set_text(row, "BtnAgent", tr("ON"))
        set_text(row, "EditName", names[index])
        set_text(row, "ProviderHead", providers[index % 5])
        set_text(row, "ModelHead", models[index % 5])
        set_text(row, "RoleHead", roles[index % len(roles)])
        loadout_index = [5, 6, 3, 7, 8][index % 5]
        set_text(row, "LoadoutHead", loadouts[min(loadout_index, len(loadouts) - 1)])
        set_text(row, "VoiceHead", ["Helmut", "Sarah", "George", "Liam", "Aria"][index % 5])
        set_text(row, "LangHead", speech_languages[0 if args.language == "de" else 1])
        set_text(row, "LiveText", tr("Ready"))
        accent = row.find("RowAccent")
        if accent:
            accent.set("color", *colors[index], 1)
        host.children.append(row)
        rows.append(row)
    set_text(root, "RowCountText", f"{args.rows} / 10")
    set_text(root, "StatusText", tr("Ready"))
    set_text(root, "CostText", tr("Cost: --"))
    for name, value in {"BtnMode": "Cooperative", "BtnSpawn": "Separate", "BtnTarget": "Targeted NPC",
                        "BtnMic": "ON", "BtnComic": "ON", "BtnHud": "RIGHT", "BtnOrch": "OFF",
                        "BtnPatrol": "OFF", "BtnNpcTts": "OFF", "BtnStart": "START CO-OP"}.items():
        set_text(root, name, tr(value))
    set_text(root, "ModeNote", tr("Start together and survive as a team."))
    set_text(root, "IdleHead", "120s")
    set_text(root, "TurnsHead", "10" + tr(" decisions"))
    set_text(root, "MissionHead", labels("s_MissionLabels", ["No mission"])[0])
    key_labels = source_strings(source, "s_SafeKeyLabels", ["Num 5", "Num 0", "Num ,"])
    for name, value in (("KeyStopHead", "Num 5"), ("KeyGotoHead", "Num 0"), ("KeyRadialHead", "Num ,")):
        set_text(root, name, next((label for label in key_labels if label == value), value))
    for name in ("LblNpcTts", "BtnNpcTts", "BtnNpcTtsBg", "NpcTtsNote"):
        node = root.find(name)
        if node:
            node.set("visible", int(args.dev))
    return rows, {"rows": args.rows, "row_size": [row_width, row_height], "row_pitch": pitch,
                  "scroll_offset": offset, "max_scroll_offset": max_scroll, "content_height": content_height,
                  "sample_values": "Illustrative active survivors ready to start; no live session data was read.",
                  "dimension_normalization": "Authored template dimensions used directly; no additional simulated engine scaling."}


def absolute_rects(root: Widget) -> dict[int, tuple[float, float, float, float]]:
    width, height = root.pair("size")
    result = {id(root): (0, 0, width, height)}

    def visit(parent: Widget) -> None:
        px, py, pw, ph = result[id(parent)]
        for child in parent.children:
            x, y, width, height = local_rect(child, (pw, ph))
            result[id(child)] = (px + x, py + y, width, height)
            visit(child)

    visit(root)
    return result


def add_dropdown(root: Widget, rows: list[Widget], template: Widget, kind: str, source: str, language: str = "en") -> dict | None:
    if kind == "none":
        return None
    popup = root.find("DdPopup")
    if popup is None:
        raise ValueError("DdPopup missing from menu template")
    positions = absolute_rects(root)
    scroll = root.find("ArenaScroll")
    scroll_rect = positions[id(scroll)] if scroll else (0, 0, *root.pair("size"))
    visible_rows = [index for index, row in enumerate(rows)
                    if positions[id(row)][1] >= scroll_rect[1] - 0.1
                    and positions[id(row)][1] + positions[id(row)][3] <= scroll_rect[1] + scroll_rect[3] + 0.1]
    if not visible_rows:
        visible_rows = [0]
    row_index = visible_rows[0] if kind == "model" else visible_rows[min(4, len(visible_rows) - 1)]
    head = rows[row_index].find("ModelHead" if kind == "model" else "LangHead")
    if head is None:
        raise ValueError("Dropdown head missing from row template")
    x, head_y, head_width, head_height = positions[id(head)]
    _, choices = language_data(language)
    option_name = "s_CodexLabels" if kind == "model" else "s_LangLabels"
    options = choices.get(option_name, source_strings(source, option_name, ["CLI default"]))
    # Match the actual MakeRowDd call where possible, including changed widths.
    head_name = "ModelHead" if kind == "model" else "LangHead"
    setup = re.search(r'MakeRowDd\(row\.m_Root, "' + head_name + r'",[^;]*,\s*(\d+),\s*([0-9.]+),\s*DD_\w+,\s*slot\)', source)
    columns = int(setup[1]) if setup else (1 if kind == "model" else 3)
    col_width = float(setup[2]) if setup else (head_width if kind == "model" else 160)
    if col_width <= 0:
        col_width = head_width
    if kind == "model":
        minimum = re.search(r"DD_MODEL && colW < ([0-9.]+) \* uiScale", source)
        col_width = max(col_width, float(minimum[1]) if minimum else 320)
    item_height = source_number(source, "ITEM_ROW_H", template.pair("size")[1])
    count_per_column = max(1, (len(options) + columns - 1) // columns)
    width = columns * col_width
    height = count_per_column * item_height
    menu_width, menu_height = root.pair("size")
    y = head_y + head_height
    if y + height > menu_height:
        y = head_y - height
    x = max(0, min(x, menu_width - width))
    y = max(0, y)
    popup.set("position", x, y)
    popup.set("size", width, height)
    popup.set("visible", 1)
    popup.children.clear()
    current = head.value("text")
    shade_source = re.search(r"protected int ShadeFor\([^}]+\}", source)
    shades = re.findall(r"ARGB\(255,\s*(\d+),\s*(\d+),\s*(\d+)\)", shade_source[0]) if shade_source else []
    if len(shades) != 3:
        shades = [(50, 66, 44), (33, 43, 37), (27, 36, 32)]
    for index in range(count_per_column * columns):
        item = copy.deepcopy(template)
        item.set("position", (index // count_per_column) * col_width, (index % count_per_column) * item_height)
        item.set("size", col_width, item_height)
        bg = item.find("DdItemBg")
        if bg:
            bg.set("position", 0, 0)
            bg.set("size", col_width, item_height)
            selected = index < len(options) and options[index] == current
            shade = shades[0] if selected else (shades[1] if index % count_per_column % 2 else shades[2])
            bg.set("color", *(int(value) / 255 for value in shade), 1)
        label = item.find("DdItemLabel")
        if label:
            label.set("position", 12, 0)
            label.set("size", col_width - 24, item_height)
            label.set("text", options[index] if index < len(options) else "")
        popup.children.append(item)
    return {"kind": kind, "row": row_index + 1, "items": len(options), "columns": columns,
            "rect": [x, y, width, height], "head_rect": list(positions[id(head)])}


def dropdown_arrows(root: Widget, rows: list[Widget], dropdown: dict | None, source: str) -> None:
    """Approximate the current native UpdateHead affordance and label limit."""
    formula = re.search(r"Math\.Floor\(\(headW\s*-\s*headH\s*\*\s*([0-9.]+)\)\s*/\s*"
                        r"\(headH\s*\*\s*([0-9.]+)\s*\*\s*([0-9.]+)\)\)\s*-\s*arrow.Length\(\)", source)
    margin, proportion, glyph_width = (tuple(float(value) for value in formula.groups())
                                       if formula else (0.35, 0.44, 0.52))
    heads: list[tuple[Widget, bool]] = []
    for row_index, row in enumerate(rows):
        for name in ("ProviderHead", "ModelHead", "RoleHead", "LoadoutHead", "VoiceHead", "LangHead"):
            head = row.find(name)
            if head:
                open_name = "ModelHead" if dropdown and dropdown["kind"] == "model" else "LangHead"
                is_open = bool(dropdown and dropdown["row"] == row_index + 1 and name == open_name)
                heads.append((head, is_open))
    for name in ("IdleHead", "TurnsHead", "KeyStopHead", "KeyGotoHead", "KeyRadialHead", "MissionHead"):
        head = root.find(name)
        if head:
            heads.append((head, False))
    for head, is_open in heads:
        label = head.value("text")
        arrow = "  ^" if is_open else "  v"
        width, height = head.pair("size")
        if height > 0:
            chars = max(4, math.floor((width - height * margin) / (height * proportion * glyph_width)) - len(arrow))
            if len(label) > chars:
                label = label[:chars - 3] + "..."
        head.set("text", label + arrow)


def rgba(node: Widget, default: tuple[int, int, int, int] = (228, 235, 239, 255)) -> tuple[int, int, int, int]:
    values = node.props.get("color")
    if not values or len(values) != 4:
        return default
    return tuple(max(0, min(255, round(float(value) * 255))) for value in values)  # type: ignore[return-value]


def intersect(a: tuple[int, int, int, int], b: tuple[int, int, int, int]) -> tuple[int, int, int, int]:
    return max(a[0], b[0]), max(a[1], b[1]), min(a[2], b[2]), min(a[3], b[3])


class Renderer:
    def __init__(self, root: Widget, viewport: tuple[int, int]):
        self.root = root
        self.viewport = viewport
        self.image = Image.new("RGBA", viewport, (14, 17, 20, 255))
        menu_width, menu_height = root.pair("size")
        # Mirror the current native fit policy at a nominal engine pixelScale=1.
        self.scale = min(1.0, (viewport[0] - 32) / menu_width, (viewport[1] - 32) / menu_height)
        self.origin = ((viewport[0] - menu_width * self.scale) / 2, (viewport[1] - menu_height * self.scale) / 2)
        self.rects = absolute_rects(root)
        self.fonts: dict[int, ImageFont.FreeTypeFont | ImageFont.ImageFont] = {}
        self.text_warnings: list[dict] = []

    def font(self, size: float) -> ImageFont.FreeTypeFont | ImageFont.ImageFont:
        pixels = max(1, round(size * self.scale))
        if pixels not in self.fonts:
            paths = [Path("C:/Windows/Fonts/segoeui.ttf"),
                     Path("/usr/share/fonts/truetype/dejavu/DejaVuSans.ttf")]
            path = next((candidate for candidate in paths if candidate.exists()), None)
            self.fonts[pixels] = ImageFont.truetype(str(path), pixels) if path else ImageFont.load_default(size=pixels)
        return self.fonts[pixels]

    def pixel_rect(self, node: Widget) -> tuple[int, int, int, int]:
        x, y, width, height = self.rects[id(node)]
        ox, oy = self.origin
        return (round(ox + x * self.scale), round(oy + y * self.scale),
                round(ox + (x + width) * self.scale), round(oy + (y + height) * self.scale))

    def composite(self, layer: Image.Image, clip: tuple[int, int, int, int]) -> None:
        clip = intersect(clip, (0, 0, *self.viewport))
        if clip[2] > clip[0] and clip[3] > clip[1]:
            self.image.alpha_composite(layer.crop(clip), (clip[0], clip[1]))

    def text(self, node: Widget, clip: tuple[int, int, int, int]) -> None:
        text = node.value("text")
        if not text:
            return
        rect = self.pixel_rect(node)
        size = node.number("exact text size", 0)
        if size <= 0 and node.kind == "ButtonWidgetClass":
            proportion = 0.36 if node.name in {"BtnStart", "BtnStop"} else 0.44
            size = node.pair("size")[1] * proportion
        if size <= 0:
            match = re.search(r"(\d+)$", node.value("font"))
            size = float(match[1]) if match else 20
        font = self.font(size)
        bbox = font.getbbox(text)
        width, height = bbox[2] - bbox[0], bbox[3] - bbox[1]
        is_control = node.kind in {"ButtonWidgetClass", "EditBoxWidgetClass"}
        halign = node.value("text halign", "center" if node.kind == "ButtonWidgetClass" else "left")
        valign = node.value("text valign", "center" if is_control else "top")
        x = rect[0] + (max(0, round(8 * self.scale)) if node.kind == "EditBoxWidgetClass" else 0)
        y = rect[1]
        if halign == "center":
            x = (rect[0] + rect[2] - width) / 2
        elif halign == "right":
            x = rect[2] - width
        if valign == "center":
            y = (rect[1] + rect[3] - height) / 2
        elif valign == "bottom":
            y = rect[3] - height
        if width > rect[2] - rect[0] + 1 or height > rect[3] - rect[1] + 1:
            self.text_warnings.append({"kind": "approximate_text_overflow", "widget": node.name,
                                       "text": text, "pixel_text_size": [width, height],
                                       "pixel_widget_size": [rect[2] - rect[0], rect[3] - rect[1]]})
        layer = Image.new("RGBA", self.viewport)
        fill = rgba(node)
        ImageDraw.Draw(layer).text((x - bbox[0], y - bbox[1]), text, font=font, fill=fill)
        self.composite(layer, intersect(clip, rect))

    def node(self, node: Widget, clip: tuple[int, int, int, int]) -> None:
        if not node.number("visible", 1):
            return
        rect = self.pixel_rect(node)
        own_clip = intersect(clip, rect)
        if node.kind == "ImageWidgetClass":
            layer = Image.new("RGBA", self.viewport)
            ImageDraw.Draw(layer).rectangle(rect, fill=rgba(node))
            self.composite(layer, clip)
        elif node.kind == "ButtonWidgetClass" and node.value("style") != "Empty" and not any(child.kind == "ImageWidgetClass" for child in node.children):
            # Approximate the engine's default skin only when no explicit
            # background was authored in the native layout.
            layer = Image.new("RGBA", self.viewport)
            ImageDraw.Draw(layer).rectangle(rect, fill=(29, 35, 40, 255), outline=(73, 83, 87, 255))
            self.composite(layer, clip)
        child_clip = own_clip if node.number("clipchildren", 0) else clip
        # Button background children must be drawn before the button's text.
        for child in sorted(node.children, key=lambda item: item.number("priority", 0)):
            self.node(child, child_clip)
        if node.kind in {"TextWidgetClass", "MultilineTextWidgetClass", "ButtonWidgetClass", "EditBoxWidgetClass"}:
            self.text(node, clip)

    def render(self) -> Image.Image:
        self.node(self.root, (0, 0, *self.viewport))
        font = self.font(12 / self.scale)
        ImageDraw.Draw(self.image).text((12, self.viewport[1] - 20),
            "UI-LAYOUT PREVIEW | Pillow approximation | Not a DayZ screenshot", font=font, fill=(120, 130, 137, 255))
        return self.image.convert("RGB")


def dimensions(value: str) -> tuple[int, int]:
    try:
        width, height = (int(item) for item in value.lower().split("x"))
    except ValueError as error:
        raise argparse.ArgumentTypeError("Use WIDTHxHEIGHT, e.g. 1920x1080") from error
    if width < 320 or height < 240:
        raise argparse.ArgumentTypeError("Preview viewport must be at least 320x240")
    return width, height


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--size", type=dimensions, default=(1920, 1080))
    parser.add_argument("--rows", type=int, choices=range(1, 11), default=3)
    parser.add_argument("--language", choices=("en", "de"), default="en")
    parser.add_argument("--scroll", default="0", help="Scroll offset in native layout units, or max")
    parser.add_argument("--dropdown", choices=("none", "model", "lang"), default="none")
    parser.add_argument("--dev", action="store_true", help="Show developer NPC-TTS widgets")
    parser.add_argument("--output", type=Path, help="PNG path; default build/menu-preview/arena-<state>.png")
    args = parser.parse_args()
    paths = [GUI / name for name in ("isu_arena_menu.layout", "isu_arena_row.layout", "isu_dd_item.layout")]
    templates = [parse_layout(path) for path in paths]
    template_counts = {path.name: sum(1 for _ in template.walk())
                       for path, template in zip(paths, templates)}
    diagnostics = [warning for path, template in zip(paths, templates)
                   for warning in template_diagnostics(template, path.name)]
    duplicates = [warning for warning in diagnostics if warning["kind"] == "duplicate_id"]
    if duplicates:
        raise ValueError(f"Duplicate template widget IDs: {duplicates}")
    root, row_template, item_template = templates
    source = SCRIPT.read_text(encoding="utf-8-sig")
    rows, roster = populate(root, row_template, args, source)
    dropdown = add_dropdown(root, rows, item_template, args.dropdown, source, args.language)
    dropdown_arrows(root, rows, dropdown, source)
    renderer = Renderer(root, args.size)
    result = renderer.render()
    output = args.output or REPO / "build/menu-preview" / (
        f"arena-{args.language}-{args.size[0]}x{args.size[1]}-{args.rows}rows-scroll{int(roster['scroll_offset'])}-{args.dropdown}.png")
    output = output.resolve()
    output.parent.mkdir(parents=True, exist_ok=True)
    result.save(output)
    report = {"purpose": "UI-layout preview; not a DayZ screenshot or engine validation",
              "source_templates": [str(path) for path in paths], "output": str(output),
              "viewport": list(args.size), "language": args.language, "menu_size": list(root.pair("size")),
              "preview_scale": renderer.scale, "preview_origin": list(renderer.origin),
              "scaling_note": "Preview fits source geometry into the viewport; this does not verify DayZ scaling.",
              "template_widget_counts": template_counts,
              "roster": roster, "dropdown": dropdown,
              "diagnostics": diagnostics + renderer.text_warnings}
    report_path = output.with_suffix(".json")
    report_path.write_text(json.dumps(report, indent=2, ensure_ascii=False) + "\n", encoding="utf-8")
    print(json.dumps({"png": str(output), "report": str(report_path), "menu_size": report["menu_size"],
                      "preview_scale": renderer.scale, "diagnostic_count": len(report["diagnostics"])}, indent=2))


if __name__ == "__main__":
    main()
