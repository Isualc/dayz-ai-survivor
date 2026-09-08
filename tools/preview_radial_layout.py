"""Native radial UI preview, not a screenshot or a DayZ runtime test.

Reuses the arena preview's layout parser and Pillow renderer without modifying
them. Text is extracted from the native radial script so the DE/EN variants
remain tied to the authored UI. Names and item classes are illustrative data.
"""

from __future__ import annotations

import argparse
import json
from pathlib import Path
import re
import sys

sys.dont_write_bytecode = True
from preview_arena_layout import Renderer, dimensions, parse_layout, set_text, template_diagnostics


REPO = Path(__file__).resolve().parents[1]
LAYOUT = REPO / "mod/IsuVoice/GUI/isu_radial_menu.layout"
SCRIPT = REPO / "mod/IsuVoice/scripts/5_Mission/IsuVoice/IsuRadialMenu.c"


def method_pairs(source: str, method: str) -> list[tuple[str, str]]:
    match = re.search(r"protected string " + method + r"\([^)]*\)\s*\{(.*?)\n\t\}", source, re.S)
    if not match:
        raise ValueError(f"Native text method {method} not found")
    return re.findall(r'IsuUiText\.Choose\("([^"\\]*)", "([^"\\]*)"\)', match[1])


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--size", type=dimensions, default=(1920, 1080))
    parser.add_argument("--lang", choices=("de", "en"), default="de")
    parser.add_argument("--hover", type=int, choices=range(-1, 6), default=-1)
    parser.add_argument("--target", default="Viktor", help="Illustrative target name; never translated")
    parser.add_argument("--nearest", action="store_true")
    parser.add_argument("--item", default="", help="Illustrative aimed item class, e.g. Canteen")
    parser.add_argument("--output", type=Path)
    args = parser.parse_args()
    root = parse_layout(LAYOUT)
    source = SCRIPT.read_text(encoding="utf-8-sig")
    diagnostics = template_diagnostics(root, LAYOUT.name)
    if any(item["kind"] == "duplicate_id" for item in diagnostics):
        raise ValueError("Duplicate widget IDs in radial template")
    language_index = 1 if args.lang == "de" else 0
    for name, english, german in re.findall(r'SetLabel\("([^"\\]*)", "([^"\\]*)", "([^"\\]*)"\)', source):
        set_text(root, name, (english, german)[language_index])
    labels = method_pairs(source, "ActionLabel")
    notes = method_pairs(source, "ActionNote")
    for index in range(6):
        pair_index = index if index < 5 else (5 if args.item else 6)
        set_text(root, "ActLabel" + str(index), labels[pair_index][language_index])
        set_text(root, "ActNote" + str(index), notes[pair_index][language_index])
    pairs = dict(re.findall(r'IsuUiText\.Choose\("([^"\\]*)", "([^"\\]*)"\)', source))
    def translated(english: str) -> str:
        return pairs[english] if args.lang == "de" else english
    target = translated("Nearest survivor") if args.nearest else args.target
    if len(target) > 24:
        target = target[:21] + "..."
    set_text(root, "RadialCenter", target)
    detail = translated("One command. One clear objective.")
    if args.item:
        item = args.item if len(args.item) <= 28 else args.item[:25] + "..."
        detail = translated("Item: ") + item
    set_text(root, "RadialTargetDetail", detail)
    if args.hover >= 0:
        root.find("ActBg" + str(args.hover)).set("color", 56 / 255, 73 / 255, 46 / 255, 1)
        root.find("ActLabel" + str(args.hover)).set("color", 241 / 255, 247 / 255, 231 / 255, 1)
        root.find("ActNote" + str(args.hover)).set("color", 199 / 255, 218 / 255, 183 / 255, 1)
        head = root.find("BtnAct" + str(args.hover))
        selector = root.find("Selector")
        selector.set("position", *head.pair("position"))
        selector.set("size", 3, head.pair("size")[1])
        selector.set("visible", 1)
    renderer = Renderer(root, args.size)
    result = renderer.render()
    output = args.output or REPO / "build/menu-preview" / f"radial-{args.lang}-{args.size[0]}x{args.size[1]}.png"
    output = output.resolve()
    output.parent.mkdir(parents=True, exist_ok=True)
    result.save(output)
    report = {"purpose": "Native UI-layout preview; not a DayZ screenshot or runtime validation",
              "layout": str(LAYOUT), "script": str(SCRIPT), "viewport": list(args.size),
              "base_size": list(root.pair("size")), "preview_scale": renderer.scale,
              "language": args.lang, "hovered_action": args.hover,
              "sample_target": target, "sample_item": args.item,
              "diagnostics": diagnostics + renderer.text_warnings}
    output.with_suffix(".json").write_text(json.dumps(report, indent=2, ensure_ascii=False) + "\n", encoding="utf-8")
    print(json.dumps({"png": str(output), "diagnostic_count": len(report["diagnostics"]),
                      "scale": renderer.scale}, indent=2))


if __name__ == "__main__":
    main()
