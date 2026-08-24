"""IsuSurvivor Match-Story — rendert eine Session als SVG-Nacherzaehlung.

Liest die neueste tracks_<session>.jsonl (Positions-/HP-Snapshots aus
spectator_server.py) sowie die Journale aller agent_home(s) und schreibt
arena/story_<session>.html: Laufwege als SVG-Polylines in Identitaetsfarbe,
eine Zeitleiste markanter Ereignisse (Spawn, Tod, Treffer, Kontakt) und
Zitate (say-Zeilen) aus den Journalen.

Aufruf:
    python daemon\\match_story.py                    # neueste tracks_*.jsonl
    python daemon\\match_story.py tracks_20260701.jsonl
    python daemon\\match_story.py D:\\...\\tracks_x.jsonl

Nur stdlib, keine Abhaengigkeiten.
"""

import glob
import json
import os
import re
import sys
from datetime import datetime

REPO_DIR = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
ARENA_DIR = os.path.join(REPO_DIR, "arena")

COLORS = ["#88c0d0", "#d08770", "#a3be8c", "#b48ead", "#ebcb8b", "#bf616a"]

SAY_RE = re.compile(r"^\[(\d\d:\d\d:\d\d)\] \[([A-Z]+)\] (.*)$")
TOOL_SAY_RE = re.compile(r'^\[(\d\d:\d\d:\d\d)\] \[TOOL\]\s+mcp__dayz__say '
                          r'\{"text":\s*"(.*)"\}\s*$')
TOD_RE = re.compile(r"^\[(\d\d:\d\d:\d\d)\] \[TOD\] (.*)$")
MISSION_RE = re.compile(r"mission|Mission", re.IGNORECASE)


# --------------------------------------------------------------------- tracks

def find_latest_tracks() -> str:
    cands = glob.glob(os.path.join(ARENA_DIR, "tracks_*.jsonl"))
    if not cands:
        return ""
    return max(cands, key=lambda p: os.path.getmtime(p))


def resolve_tracks_arg(arg: str) -> str:
    if os.path.isabs(arg) and os.path.exists(arg):
        return arg
    cand = os.path.join(ARENA_DIR, arg)
    if os.path.exists(cand):
        return cand
    if os.path.exists(arg):
        return arg
    return ""


def load_tracks(path: str) -> list:
    """Liste von Snapshots {"t": epoch, "agents": [...]}, tolerant gegen
    kaputte/abgeschnittene Zeilen (Server kann waehrend Schreiben killed sein)."""
    snaps = []
    if not path or not os.path.exists(path):
        return snaps
    with open(path, "r", encoding="utf-8", errors="replace") as f:
        for line in f:
            line = line.strip()
            if not line:
                continue
            try:
                snaps.append(json.loads(line))
            except json.JSONDecodeError:
                continue
    return snaps


def session_id_from_path(path: str) -> str:
    base = os.path.basename(path)
    if base.startswith("tracks_") and base.endswith(".jsonl"):
        return base[len("tracks_"):-len(".jsonl")]
    return datetime.now().strftime("%Y%m%d_%H%M%S")


# -------------------------------------------------------------------- journals

def journal_files() -> list:
    files = []
    files += glob.glob(os.path.join(REPO_DIR, "agent_home", "journal", "journal_*.log"))
    files += glob.glob(os.path.join(REPO_DIR, "agent_homes", "*", "journal", "journal_*.log"))
    return files


def _agent_id_for_journal(path: str) -> str:
    """agent_homes/<id>/journal/... -> id; agent_home/journal/... -> viktor
    (Legacy-Konvention: der einzige agent_home ohne Suffix ist Viktor)."""
    parts = os.path.normpath(path).split(os.sep)
    try:
        idx = parts.index("agent_homes")
        return parts[idx + 1]
    except (ValueError, IndexError):
        return "viktor"


def collect_events(t_start: float, t_end: float) -> dict:
    """events: agent_id -> Liste von {"t_str","kind","text"} innerhalb des
    Session-Zeitfensters (grob per mtime der Journal-Datei gefiltert, dann
    zeilenweise ohne Datumsbezug - Journale sind ohnehin sessionlokal)."""
    events = {}
    quotes = {}
    for path in journal_files():
        try:
            mtime = os.path.getmtime(path)
        except OSError:
            continue
        # Grobfilter: Datei muss ungefaehr im Session-Fenster liegen (+-2h Puffer,
        # weil Journal-Dateiname/-mtime nicht exakt Session-Grenzen trifft).
        if t_start and t_end and not (t_start - 7200 <= mtime <= t_end + 7200):
            continue
        agent_id = _agent_id_for_journal(path)
        ev_list = events.setdefault(agent_id, [])
        q_list = quotes.setdefault(agent_id, [])
        try:
            with open(path, "r", encoding="utf-8", errors="replace") as f:
                for raw in f:
                    line = raw.rstrip("\n")
                    mt = TOD_RE.match(line)
                    if mt:
                        ev_list.append({"t_str": mt.group(1), "kind": "tod",
                                         "text": mt.group(2).strip()})
                        continue
                    ms = TOOL_SAY_RE.match(line)
                    if ms:
                        q_list.append({"t_str": ms.group(1), "text": ms.group(2)})
                        continue
                    mm = SAY_RE.match(line)
                    if mm and mm.group(2) not in ("WELT", "TOOL", "TOKENS"):
                        text = mm.group(3).strip()
                        if MISSION_RE.search(text):
                            ev_list.append({"t_str": mm.group(1), "kind": "mission",
                                             "text": text})
        except OSError:
            continue
    return {"events": events, "quotes": quotes}


# ------------------------------------------------------------------- rendering

def _color_for(idx: int) -> str:
    return COLORS[idx % len(COLORS)]


def _agent_ids(snaps: list) -> list:
    seen = []
    for s in snaps:
        for a in s.get("agents", []):
            if a.get("id") not in seen:
                seen.append(a.get("id"))
    return seen


def build_svg(snaps: list, agent_ids: list, width: int = 900, height: int = 640) -> str:
    xs, zs = [], []
    for s in snaps:
        for a in s.get("agents", []):
            if a.get("x") is not None and a.get("z") is not None:
                xs.append(a["x"]); zs.append(a["z"])
    if not xs:
        return '<svg width="%d" height="%d"><text x="20" y="40" fill="#d8dee9">Keine Positionsdaten in dieser Session.</text></svg>' % (width, height)

    min_x, max_x = min(xs) - 60, max(xs) + 60
    min_z, max_z = min(zs) - 60, max(zs) + 60
    if max_x - min_x < 1:
        min_x -= 50; max_x += 50
    if max_z - min_z < 1:
        min_z -= 50; max_z += 50

    def to_screen(x, z):
        sx = (x - min_x) / (max_x - min_x) * (width - 20) + 10
        sz = height - ((z - min_z) / (max_z - min_z) * (height - 20) + 10)
        return sx, sz

    paths = {}
    lastpos = {}
    for s in snaps:
        for a in s.get("agents", []):
            aid = a.get("id")
            if aid is None or a.get("x") is None or a.get("z") is None:
                continue
            sx, sz = to_screen(a["x"], a["z"])
            paths.setdefault(aid, []).append((sx, sz))
            lastpos[aid] = (sx, sz, bool(a.get("alive")), a.get("name") or aid)

    parts = ['<svg xmlns="http://www.w3.org/2000/svg" width="%d" height="%d" '
             'viewBox="0 0 %d %d" style="background:#0d1119;border-radius:6px">'
             % (width, height, width, height)]

    for i, aid in enumerate(agent_ids):
        pts = paths.get(aid, [])
        if len(pts) < 2:
            continue
        col = _color_for(i)
        d = " ".join(f"{x:.1f},{z:.1f}" for x, z in pts)
        parts.append(f'<polyline points="{d}" fill="none" stroke="{col}" '
                      f'stroke-width="2" stroke-opacity="0.75" '
                      f'stroke-linejoin="round" stroke-linecap="round"/>')
        # Startmarker (offener Kreis)
        sx0, sz0 = pts[0]
        parts.append(f'<circle cx="{sx0:.1f}" cy="{sz0:.1f}" r="5" '
                      f'fill="none" stroke="{col}" stroke-width="2"/>')

    for i, aid in enumerate(agent_ids):
        if aid not in lastpos:
            continue
        sx, sz, alive, name = lastpos[aid]
        col = _color_for(i) if alive else "#555"
        parts.append(f'<circle cx="{sx:.1f}" cy="{sz:.1f}" r="7" fill="{col}" '
                      f'stroke="#0d1119" stroke-width="2"/>')
        parts.append(f'<text x="{sx + 10:.1f}" y="{sz + 4:.1f}" fill="#d8dee9" '
                      f'font-family="Consolas,monospace" font-size="12">'
                      f'{_esc(name)}{"" if alive else " (gefallen)"}</text>')

    parts.append("</svg>")
    return "\n".join(parts)


def _esc(s: str) -> str:
    return (str(s).replace("&", "&amp;").replace("<", "&lt;").replace(">", "&gt;")
            .replace('"', "&quot;"))


def build_timeline_html(events: dict, quotes: dict, agent_ids: list, roster_names: dict) -> str:
    rows = []
    for i, aid in enumerate(agent_ids):
        col = _color_for(i)
        name = roster_names.get(aid, aid.capitalize())
        for ev in events.get(aid, []):
            kind_label = {"tod": "TOD", "mission": "MISSION"}.get(ev["kind"], ev["kind"].upper())
            rows.append((ev["t_str"], col, name, kind_label, ev["text"]))
    rows.sort(key=lambda r: r[0])
    if not rows:
        return '<p class="empty">Keine markanten Ereignisse gefunden.</p>'
    out = ['<table class="timeline"><thead><tr><th>Zeit</th><th>Wer</th>'
           '<th>Was</th><th>Detail</th></tr></thead><tbody>']
    for t_str, col, name, kind, text in rows:
        out.append(f'<tr><td class="t">{t_str}</td>'
                    f'<td style="color:{col}">{_esc(name)}</td>'
                    f'<td class="kind">{kind}</td><td>{_esc(text)}</td></tr>')
    out.append("</tbody></table>")
    return "\n".join(out)


def build_quotes_html(quotes: dict, agent_ids: list, roster_names: dict) -> str:
    rows = []
    for i, aid in enumerate(agent_ids):
        col = _color_for(i)
        name = roster_names.get(aid, aid.capitalize())
        for q in quotes.get(aid, [])[-12:]:  # letzte Zitate reichen fuer eine Story
            rows.append((q["t_str"], col, name, q["text"]))
    rows.sort(key=lambda r: r[0])
    if not rows:
        return '<p class="empty">Keine Zitate im Session-Fenster gefunden.</p>'
    out = ['<ul class="quotes">']
    for t_str, col, name, text in rows:
        out.append(f'<li><span class="t">[{t_str}]</span> '
                    f'<span style="color:{col}">{_esc(name)}:</span> '
                    f'&ldquo;{_esc(text)}&rdquo;</li>')
    out.append("</ul>")
    return "\n".join(out)


def _load_roster_names() -> dict:
    try:
        with open(os.path.join(ARENA_DIR, "agents.json"), "r", encoding="utf-8") as f:
            data = json.load(f)
        return {a["id"]: a.get("name", a["id"].capitalize())
                for a in data.get("agents", []) if a.get("id")}
    except Exception:
        return {}


def render_html(session: str, svg: str, timeline_html: str, quotes_html: str,
                 agent_ids: list, roster_names: dict, snap_count: int) -> str:
    legend = " ".join(
        f'<span style="color:{_color_for(i)}">&#9679; {_esc(roster_names.get(a, a))}</span>'
        for i, a in enumerate(agent_ids))
    return f"""<!DOCTYPE html>
<html lang="de">
<head>
<meta charset="utf-8">
<title>IsuSurvivor Story - {_esc(session)}</title>
<style>
  body {{ margin:0; padding:20px; background:#0b0e14; color:#d8dee9;
          font-family:"Segoe UI",Consolas,monospace; }}
  h1 {{ font-size:20px; color:#88c0d0; margin:0 0 4px 0; }}
  .sub {{ font-size:12px; color:#6b7688; margin-bottom:16px; }}
  .legend {{ font-size:12px; margin:10px 0 18px 0; }}
  .legend span {{ margin-right:16px; }}
  .panel {{ background:#151a24; border:1px solid #2a3140; border-radius:8px;
            padding:14px; margin-bottom:18px; }}
  h2 {{ font-size:14px; color:#a8b2c0; margin:0 0 10px 0; text-transform:uppercase;
        letter-spacing:1px; }}
  table.timeline {{ width:100%; border-collapse:collapse; font-size:12px; }}
  table.timeline th {{ text-align:left; color:#6b7688; font-weight:normal;
        border-bottom:1px solid #2a3140; padding:4px 8px; }}
  table.timeline td {{ padding:4px 8px; border-bottom:1px solid #1c2230; }}
  table.timeline td.t {{ color:#6b7688; white-space:nowrap; }}
  table.timeline td.kind {{ color:#e5c07b; white-space:nowrap; }}
  ul.quotes {{ list-style:none; margin:0; padding:0; font-size:13px; }}
  ul.quotes li {{ padding:5px 0; border-bottom:1px solid #1c2230; }}
  ul.quotes .t {{ color:#4b5566; margin-right:6px; }}
  .empty {{ color:#4b5566; font-size:12px; }}
</style>
</head>
<body>
<h1>ISUSURVIVOR &mdash; MATCH-STORY</h1>
<div class="sub">Session {_esc(session)} &middot; {snap_count} Positions-Snapshots</div>
<div class="legend">{legend}</div>
<div class="panel">
  <h2>Laufwege</h2>
  {svg}
</div>
<div class="panel">
  <h2>Zeitleiste</h2>
  {timeline_html}
</div>
<div class="panel">
  <h2>Zitate</h2>
  {quotes_html}
</div>
</body>
</html>
"""


def main():
    arg = sys.argv[1] if len(sys.argv) > 1 else ""
    tracks_path = resolve_tracks_arg(arg) if arg else find_latest_tracks()
    if not tracks_path:
        print("Keine tracks_*.jsonl gefunden (weder Argument noch arena/tracks_*.jsonl).")
        return 1

    snaps = load_tracks(tracks_path)
    session = session_id_from_path(tracks_path)
    agent_ids = _agent_ids(snaps)
    roster_names = _load_roster_names()

    t_start = snaps[0]["t"] if snaps else 0.0
    t_end = snaps[-1]["t"] if snaps else 0.0
    ev_data = collect_events(t_start, t_end)

    svg = build_svg(snaps, agent_ids)
    timeline_html = build_timeline_html(ev_data["events"], ev_data["quotes"], agent_ids, roster_names)
    quotes_html = build_quotes_html(ev_data["quotes"], agent_ids, roster_names)
    html = render_html(session, svg, timeline_html, quotes_html, agent_ids, roster_names, len(snaps))

    os.makedirs(ARENA_DIR, exist_ok=True)
    out_path = os.path.join(ARENA_DIR, f"story_{session}.html")
    with open(out_path, "w", encoding="utf-8") as f:
        f.write(html)
    print(f"Story geschrieben: {out_path} ({len(snaps)} Snapshots, "
          f"{len(agent_ids)} Agenten)")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
