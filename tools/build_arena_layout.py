"""Generate the native DayZ arena layouts from a shared spacing/type palette.

Run from any directory with Python 3. No third-party dependencies. Widget names
are the public contract with IsuArenaMenu.c; never rename them casually.
"""
from pathlib import Path
import json

GUI = Path(__file__).resolve().parents[1] / "mod/IsuVoice/GUI"
FONT = '"gui/fonts/sdf_MetronBook24"'
BG = "0.067 0.094 0.082 0.99"
FIELD = "0.125 0.165 0.145 1"
LINE = "0.235 0.290 0.255 1"
TEXT = "0.922 0.937 0.894 1"
MUTED = "0.612 0.682 0.627 1"
SAGE = "0.714 0.835 0.604 1"
TRANSLATIONS = {}
CHOICES = {
    "s_PersonaLabels": (["Hunter", "Farmer", "Medic", "Veteran", "Fighter"],
                         ["Jäger", "Bauer", "Sanitäter", "Veteran", "Kämpfer"]),
    "s_LoadoutLabels": (["Keep", "Scout", "Assault", "Medic", "Sniper", "Hunter", "Farmer", "Military", "Survivor"],
                         ["Behalten", "Späher", "Sturm", "Sanitäter", "Scharfschütze", "Jäger", "Bauer", "Militär", "Überlebender"]),
    "s_MissionLabels": (["No mission", "Mission: Birgit", "Event: Horde"],
                         ["Keine Mission", "Mission: Birgit", "Ereignis: Horde"]),
    "s_LangLabels": (["German", "English", "French", "Spanish", "Italian", "Portuguese", "Dutch", "Polish", "Russian", "Ukrainian", "Turkish", "Swedish", "Czech", "Danish", "Finnish", "Greek", "Romanian", "Hungarian", "Norwegian", "Croatian", "Slovak", "Japanese", "Korean", "Chinese", "Arabic", "Hindi", "Filipino"],
                      ["Deutsch", "Englisch", "Französisch", "Spanisch", "Italienisch", "Portugiesisch", "Niederländisch", "Polnisch", "Russisch", "Ukrainisch", "Türkisch", "Schwedisch", "Tschechisch", "Dänisch", "Finnisch", "Griechisch", "Rumänisch", "Ungarisch", "Norwegisch", "Kroatisch", "Slowakisch", "Japanisch", "Koreanisch", "Chinesisch", "Arabisch", "Hindi", "Filipino"]),
    "s_CodexLabels": (["CLI default", "5.6 Luna", "5.6 Terra", "5.6 Sol", "6 Astra"],
                       ["CLI-Standard", "5.6 Luna", "5.6 Terra", "5.6 Sol", "6 Astra"]),
    "s_GeminiCliLabels": (["CLI default", "Flash (auto)", "Pro (auto)", "Flash-Lite (auto)", "3.5 Flash", "3.1 Pro preview", "3 Flash preview", "3.1 Flash-Lite", "2.5 Pro", "2.5 Flash"],
                          ["CLI-Standard", "Flash (auto)", "Pro (auto)", "Flash-Lite (auto)", "3.5 Flash", "3.1 Pro Vorschau", "3 Flash Vorschau", "3.1 Flash-Lite", "2.5 Pro", "2.5 Flash"]),
    "s_AntigravityLabels": (["CLI default", "Gemini 3.8 Flash (High)", "Gemini 3.8 Flash (Medium)", "Gemini 3.8 Flash (Low)", "Gemini 3.7 Flash (High)", "Gemini 3.7 Flash (Medium)", "Gemini 3.7 Flash (Low)", "Gemini 3.6 Flash (High)", "Gemini 3.6 Flash (Medium)", "Gemini 3.6 Flash (Low)", "Gemini 3.1 Pro (High)", "Gemini 3.1 Pro (Low)", "Claude Sonnet 4.6 (Thinking)", "Claude Opus 4.6 (Thinking)", "GPT-OSS 120B (Medium)"],
                            ["CLI-Standard", "Gemini 3.8 Flash (Hoch)", "Gemini 3.8 Flash (Mittel)", "Gemini 3.8 Flash (Niedrig)", "Gemini 3.7 Flash (Hoch)", "Gemini 3.7 Flash (Mittel)", "Gemini 3.7 Flash (Niedrig)", "Gemini 3.6 Flash (Hoch)", "Gemini 3.6 Flash (Mittel)", "Gemini 3.6 Flash (Niedrig)", "Gemini 3.1 Pro (Hoch)", "Gemini 3.1 Pro (Niedrig)", "Claude Sonnet 4.6 (Thinking)", "Claude Opus 4.6 (Thinking)", "GPT-OSS 120B (Mittel)"]),
}


def widget(kind, name, rect, props=(), children=()):
    x, y, w, h = rect
    lines = [f"{kind}WidgetClass {name} {{", f" position {x} {y}",
             f" size {w} {h}", " hexactpos 1", " vexactpos 1",
             " hexactsize 1", " vexactsize 1"]
    lines.extend(f" {prop}" for prop in props)
    if children:
        lines.append(" {")
        for child in children:
            lines.extend("  " + line for line in child.splitlines())
        lines.append(" }")
    return "\n".join(lines + ["}"])


def image(name, rect, color=FIELD, pointer=False):
    return widget("Image", name, rect, [f"ignorepointer {0 if pointer else 1}",
                  f"color {color}", "mode blend"])


def text(name, rect, value, size=14, color=TEXT, de=None):
    # The runtime uses userID as the immutable design font size when fitting
    # the exact-pixel layout to the current screen.
    if de is not None:
        TRANSLATIONS[name] = (value, de)
    return widget("Text", name, rect, ["ignorepointer 1", f"userID {size}",
                  f'text "{value}"', f"font {FONT}", '"exact text" 1',
                  f'"exact text size" {size}', '"text valign" center', f"color {color}"])


def button(name, rect, value="", bg=FIELD, color=TEXT, de=None):
    # Sibling background: a child image can paint over the engine button text.
    if de is not None:
        TRANSLATIONS[name] = (value, de)
    return [image(name + "Bg", rect, bg),
            widget("Button", name, rect, ['style Empty', f'text "{value}"',
                   f"font {FONT}", f"color {color}", '"text halign" center',
                   '"text valign" center'])]


def menu():
    nodes = [image("Background", (0, 0, 1760, 900), BG, pointer=True),
             image("TopRule", (32, 20, 44, 3), SAGE),
             text("Title", (32, 31, 600, 34), "ISU / SURVIVOR", 28),
             text("Subtitle", (32, 71, 1080, 20), "Your survivors. Their models. One fight to stay alive.", 14, MUTED,
                  de="Deine Überlebenden. Ihre Modelle. Ein gemeinsamer Überlebenskampf."),
             image("StatusPill", (1304, 31, 364, 36)),
             image("StatusDot", (1318, 45, 8, 8), SAGE),
             text("StatusText", (1338, 31, 314, 36), "Ready", 14, de="Bereit")]
    nodes += button("BtnClose", (1688, 29, 40, 40), "X")
    nodes += [image("HeaderRule", (32, 102, 1696, 1), LINE),
              text("GrpRoster", (32, 117, 254, 28), "01 / YOUR SURVIVORS", 16, SAGE, de="01 / DEINE GRUPPE"),
              text("RosterNote", (300, 120, 1120, 24), "Every field belongs to its survivor. Scroll to see the rest of your group.", 14, MUTED,
                   de="Alle Einstellungen direkt beim Überlebenden. Für weitere Mitglieder nach unten scrollen."),
              text("RowCountText", (1450, 117, 96, 28), "1 / 10", 14, MUTED)]
    nodes += button("BtnAddNpc", (1560, 113, 168, 36), "+ ADD SURVIVOR", de="+ HINZUFÜGEN")
    # The extra 24 px belong to the native scrollbar, outside the row controls.
    # ScrollWidget does not enable its vertical bar merely from overflowing content.
    nodes.append(widget("Scroll", "ArenaScroll", (32, 164, 1720, 264),
                        ["clipchildren 1", "ignorepointer 0", "style blank", '"Scrollbar V" 1'],
                        [widget("Frame", "RowsHost", (0, 0, 1696, 264), ["clipchildren 0", "ignorepointer 1"])]))
    nodes.append(image("SettingsRule", (32, 446, 1696, 1), LINE))
    sections = [("Round", 32, "02 / SESSION", "02 / SPIELRUNDE"),
                ("Behavior", 460, "03 / BEHAVIOUR", "03 / VERHALTEN"),
                ("Controls", 888, "04 / CONTROLS", "04 / STEUERUNG"),
                ("Audio", 1316, "05 / AUDIO & DISPLAY", "05 / AUDIO & ANZEIGE")]
    for name, x, title, german in sections:
        nodes.append(text("Grp" + name, (x, 458, 412, 24), title, 14, SAGE, de=german))
    fields = [
        (32, "LblMode", "Game mode", "Spielmodus", "BtnMode", "Cooperative"),
        (32, "LblSpawn", "Starting position", "Startposition", "BtnSpawn", "Separate"),
        (32, "LblCamp", "Camp / click to use your position", "Lager / Klick: aktuelle Position", "BtnCamp", "4234 / 8512"),
        (32, "LblMission", "Mission", "Mission", "MissionHead", "No mission"),
        (460, "LblIdle", "Time between model decisions", "Pause zwischen Modellentscheidungen", "IdleHead", "120 s"),
        (460, "LblTurns", "Decisions per round", "Entscheidungen pro Runde", "TurnsHead", "10 decisions"),
        (460, "LblPatrol", "Ambient patrols / applies after server restart", "Umgebungspatrouillen / ab Serverneustart", "BtnPatrol", "OFF"),
        (460, "LblOrch", "Group coordination", "Gruppenkoordination", "BtnOrch", "OFF"),
        (888, "LblTarget", "Send commands to", "Befehle richten an", "BtnTarget", "Targeted NPC"),
        (888, "LblKeyStop", "Hold position", "Anhalten", "KeyStopHead", "Num 5"),
        (888, "LblKeyGoto", "Move to target", "Zum Zielpunkt", "KeyGotoHead", "Num 0"),
        (888, "LblKeyRadial", "Command wheel", "Befehlsrad", "KeyRadialHead", "Num ,"),
        (1316, "LblMic", "Microphone", "Mikrofon", "BtnMic", "ON"),
        (1316, "LblComic", "Speech bubbles", "Sprechblasen", "BtnComic", "ON"),
        (1316, "LblHud", "Squad display (HUD)", "Gruppenanzeige (HUD)", "BtnHud", "RIGHT"),
        (1316, "LblNpcTts", "Developer mode / NPC radio voices", "Entwicklermodus / NPC-Funkstimmen", "BtnNpcTts", "OFF"),
    ]
    for index, (x, label_id, english, german, button_id, value) in enumerate(fields):
        y = 494 + (index % 4) * 60
        nodes.append(text(label_id, (x, y, 412, 18), english, 13, MUTED, de=german))
        nodes += button(button_id, (x, y + 20, 412, 36), value)
    nodes += [text("ModeNote", (32, 738, 412, 20), "Work together and survive as a team.", 13, MUTED,
                   de="Zusammenarbeiten und als Team überleben."),
              text("OrchNote", (460, 738, 412, 20), "Shared goals, independent decisions.", 13, MUTED,
                   de="Gemeinsame Ziele, eigenständige Entscheidungen."),
              text("ControlsNote", (888, 738, 412, 20), "Click a key to change its assignment.", 13, MUTED,
                   de="Klicke auf eine Taste, um sie neu zuzuweisen."),
              text("NpcTtsNote", (1316, 738, 412, 20), "Only available in developer mode.", 13, MUTED,
                   de="Nur im Entwicklermodus verfügbar."),
              image("FooterRule", (32, 778, 1696, 1), LINE),
              text("CostLabel", (32, 792, 1040, 18), "SESSION COST", 13, MUTED, de="RUNDENKOSTEN"),
              text("CostText", (32, 813, 1040, 26), "No cost recorded yet", 16, de="Noch keine Kosten erfasst")]
    nodes += button("BtnStop", (1216, 796, 168, 44), "STOP", "0.220 0.125 0.114 1", "0.941 0.694 0.627 1", de="STOPP")
    nodes += button("BtnStart", (1408, 796, 320, 44), "START CO-OP", SAGE, "0.067 0.106 0.063 1")
    nodes += [text("HintLine1", (32, 861, 1696, 20),
                   "Insert: close menu     /     Right-click camp: reset position     /     Your choices stay selected during this game session.", 13, MUTED,
                   de="Einfg: Menü schließen     /     Rechtsklick auf Lager: Position zurücksetzen     /     Die Auswahl bleibt in dieser Spielsitzung erhalten."),
              widget("Frame", "DdPopup", (0, 0, 320, 36), ["visible 0", "priority 200", "clipchildren 0"])]
    return widget("Frame", "IsuArenaRoot", (0, 0, 1760, 900),
                  ["clipchildren 0", "halign center_ref", "valign center_ref"], nodes)


def row():
    nodes = [image("RowCard", (0, 0, 1696, 80), "0.106 0.141 0.125 1"),
             image("RowAccent", (0, 0, 3, 80), SAGE)]
    # Captions and their controls share the exact same subtree and geometry.
    # No detached table header can drift when the engine scales a new row.
    for name, x, width, en, de in [
        ("Active", 12, 52, "AI", "KI"), ("Name", 76, 176, "NAME", "NAME"),
        ("Provider", 268, 188, "PROVIDER", "ANBIETER"), ("Model", 464, 272, "MODEL", "MODELL"),
        ("Role", 744, 160, "ROLE", "ROLLE"), ("Loadout", 912, 192, "LOADOUT", "AUSRÜSTUNG"),
        ("Voice", 1112, 168, "VOICE", "STIMME"), ("Lang", 1288, 164, "SPEECH LANGUAGE", "AUSGABESPRACHE"),
        ("Live", 1460, 176, "STATUS", "ZUSTAND")]:
        nodes.append(text("RowLbl" + name, (x, 8, width, 18), en, 13, MUTED, de=de))
    nodes += button("BtnAgent", (12, 32, 52, 36), "ON", "0.165 0.231 0.157 1", SAGE)
    nodes += [image("NameBg", (76, 32, 176, 36)),
              widget("EditBox", "EditName", (80, 32, 168, 36),
                     ['text ""', f"font {FONT}", '"exact text" 1', '"exact text size" 16', f"color {TEXT}"])]
    for name, x, width in [("Provider", 268, 188), ("Model", 464, 272),
                           ("Role", 744, 160), ("Loadout", 912, 192),
                           ("Voice", 1112, 168), ("Lang", 1288, 164)]:
        nodes += button(name + "Head", (x, 32, width, 36))
    nodes.append(text("LiveText", (1460, 32, 176, 36), "Ready", 14, MUTED, de="Bereit"))
    nodes += button("BtnRemove", (1648, 32, 32, 36), "X", "0.165 0.153 0.133 1", MUTED)
    return widget("Frame", "IsuArenaRow", (0, 0, 1696, 80), ["clipchildren 0"], nodes)


def write_translations():
    source = ['// Generated by tools/build_arena_layout.py. UTF-8; German glyphs are intentional.',
              'class IsuArenaText', '{', '\tstatic void Apply(Widget root)', '\t{']
    for name, (en, de) in TRANSLATIONS.items():
        args = ', '.join(json.dumps(v, ensure_ascii=False) for v in (name, en, de))
        source.append('\t\tLabel(root, ' + args + ');')
    source += ['\t}', '', '\tstatic void Label(Widget root, string name, string english, string german)',
               '\t{', '\t\tif (!root) return;', '\t\tWidget w = root.FindAnyWidget(name);',
               '\t\tTextWidget label = TextWidget.Cast(w);', '\t\tButtonWidget button = ButtonWidget.Cast(w);',
               '\t\tif (label) label.SetText(IsuUiText.Choose(english, german));',
               '\t\tif (button) button.SetText(IsuUiText.Choose(english, german));', '\t}', '}']
    source.pop()  # class closing brace; append choice translations
    source += ['', '\tstatic void ApplyChoices()', '\t{']
    for name, (english, german) in CHOICES.items():
        assert len(english) == len(german)
        for index, (en, de) in enumerate(zip(english, german)):
            args = ', '.join(json.dumps(v, ensure_ascii=False) for v in (en, de))
            source.append(f'\t\tIsuArenaMenu.{name}.Set({index}, IsuUiText.Choose({args}));')
    source += ['\t}', '}']
    (GUI.parent / 'scripts/5_Mission/IsuVoice/IsuArenaText.c').write_text('\n'.join(source) + '\n', encoding='utf-8')
    (GUI / 'isu_arena_translations.json').write_text(json.dumps(TRANSLATIONS, ensure_ascii=False, indent=2) + '\n', encoding='utf-8')


def dropdown():
    return widget("Button", "IsuDdItem", (0, 0, 320, 36),
                  ['style Empty', 'text ""', f"font {FONT}"],
                  [image("DdItemBg", (0, 0, 320, 36), FIELD),
                   text("DdItemLabel", (12, 0, 296, 36), "", 16)])


if __name__ == "__main__":
    for name, content in [("isu_arena_menu.layout", menu()),
                          ("isu_arena_row.layout", row()),
                          ("isu_dd_item.layout", dropdown())]:
        path = GUI / name
        path.write_text(content + "\n", encoding="utf-8")
        print(path)

    write_translations()
