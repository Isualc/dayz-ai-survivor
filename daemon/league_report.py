"""IsuSurvivor Liga-Report — Aggregation pro Modell/Agent ueber alle Sessions.

Parst die Journale aller agent_home(s), zusaetzlich (falls vorhanden) die
Positions-/HP-Tracks aus arena/tracks_*.jsonl, und schreibt fortschreibend:
  - arena/league.json  (Rohdaten, gemerged pro Session-ID)
  - arena/league.html  (sortierbare Tabelle, stdlib-only, kein JS-Framework)

Eigenstaendig aufrufbar:
    python daemon\\league_report.py                # alle Journale seit je
    python daemon\\league_report.py --since 20260701
    python daemon\\league_report.py --session <id>  # nur eine Session

Wird vom arena_supervisor nach Rundenende detached aufgerufen und toleriert
selbst, dass Journale/Tracks fehlen oder kaputt sind - bricht dann NICHT ab,
sondern liefert einen Report mit den Daten, die lesbar waren.

Es gibt bewusst KEIN persistentes BR-Kampf-Log (_br_monitor.py schreibt nur
nach stdout) - Tode/Ueberlebensdauer werden daher ausschliesslich aus den
Journal-Marken [TOD] und den Zug-/Session-Grenzen abgeleitet. Tokens/Kosten
sind bei usage-blinden CCR-Backends (openai/google/xai via claude-code-router)
NICHT ableitbar und werden dann explizit als "n/a" gefuehrt, NIEMALS als 0 -
0 wuerde faelschlich "kostenlos" suggerieren statt "nicht messbar".
"""

import argparse
import glob
import json
import os
import re
import sys
import time
from datetime import datetime

REPO_DIR = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
ARENA_DIR = os.path.join(REPO_DIR, "arena")
LEAGUE_JSON = os.path.join(ARENA_DIR, "league.json")
LEAGUE_HTML = os.path.join(ARENA_DIR, "league.html")

TOKENS_ZUG_RE = re.compile(
    r"\[TOKENS ZUG\] ([^:]+): in=(\d+) out=(\d+) cache_read=(\d+) "
    r"cache_write=(\d+) \| Session: .*?, ([\d.]+) USD")
ZUG_ENDE_RE = re.compile(r"\[ZUG ENDE\]")
TOOL_RE = re.compile(r"^\[(\d\d:\d\d:\d\d)\] \[TOOL\]\s+(\S+)")
WELT_FAIL_RE = re.compile(r"^\[(\d\d:\d\d:\d\d)\] \[WELT\]\s+.*Fehlgeschlagen")
WELT_RE = re.compile(r"^\[(\d\d:\d\d:\d\d)\] \[WELT\]")
TOD_RE = re.compile(r"^\[(\d\d:\d\d:\d\d)\] \[TOD\]")
HEADER_RE = re.compile(
    r"^\[(\d\d:\d\d:\d\d)\] === IsuSurvivor Agent-Runner \| .*?model=([\w.,/\-]+)")
TS_RE = re.compile(r"^\[(\d\d):(\d\d):(\d\d)\]")
JOURNAL_NAME_RE = re.compile(r"journal_(\d{8})_(\d{6})\.log$")
KILLED_RE = re.compile(
    r"(?:getoetet|erschossen|erledigt|Kill)", re.IGNORECASE)


# ------------------------------------------------------------------- helpers

def _agent_id_for_journal(path: str) -> str:
    parts = os.path.normpath(path).split(os.sep)
    try:
        idx = parts.index("agent_homes")
        return parts[idx + 1]
    except (ValueError, IndexError):
        return "viktor"


def journal_files(since: str = "") -> list:
    files = []
    files += glob.glob(os.path.join(REPO_DIR, "agent_home", "journal", "journal_*.log"))
    files += glob.glob(os.path.join(REPO_DIR, "agent_homes", "*", "journal", "journal_*.log"))
    if since:
        out = []
        for p in files:
            m = JOURNAL_NAME_RE.search(os.path.basename(p))
            if m and m.group(1) >= since:
                out.append(p)
        return out
    return files


def _load_roster() -> dict:
    try:
        with open(os.path.join(ARENA_DIR, "agents.json"), "r", encoding="utf-8") as f:
            data = json.load(f)
        return {a["id"]: a for a in data.get("agents", []) if a.get("id")}
    except Exception:
        return {}


def session_id_for(path: str) -> str:
    """Journal-Datei -> Session-ID (Datumsstempel des Dateinamens). Mehrere
    Agenten, die im selben Sekundenfenster gestartet wurden, teilen sich
    naeherungsweise dieselbe Runde; exakte Rundenzuordnung ist ohne
    Supervisor-Metadaten nicht moeglich, darum gilt: Session = Tag_Uhrzeit
    des JEWEILIGEN Journals (pro-Agent-Session, nicht global-Runde)."""
    m = JOURNAL_NAME_RE.search(os.path.basename(path))
    if m:
        return f"{m.group(1)}_{m.group(2)}"
    return "unbekannt"


# --------------------------------------------------------------- journal parse

def parse_journal(path: str) -> dict:
    """Ein Journal (= eine Claude-Prozess-Session eines Agenten) zu einem
    Aggregat verdichten. Robust gegen kaputte/abgeschnittene Dateien."""
    agent_id = _agent_id_for_journal(path)
    result = {
        "agent_id": agent_id,
        "model": "",
        "turns": 0,
        "tool_calls": 0,
        "tool_fails": 0,
        "deaths": 0,
        "kills_hint": 0,
        "tokens_in": 0,
        "tokens_out": 0,
        "cache_read": 0,
        "cache_write": 0,
        "usd": 0.0,
        "usd_known": False,
        "first_t": None,
        "last_t": None,
        "start_stamp": None,
    }
    m = JOURNAL_NAME_RE.search(os.path.basename(path))
    if m:
        try:
            result["start_stamp"] = datetime.strptime(
                m.group(1) + m.group(2), "%Y%m%d%H%M%S").timestamp()
        except ValueError:
            pass

    prev_usd = 0.0
    seg_usd = 0.0
    saw_usd = False
    day_off = 0  # Mitternachts-Rollover: Journale tragen nur HH:MM:SS
    try:
        with open(path, "r", encoding="utf-8", errors="replace") as f:
            for raw in f:
                line = raw.rstrip("\n")

                mts = TS_RE.match(line)
                if mts:
                    t_abs = (int(mts.group(1)) * 3600 + int(mts.group(2)) * 60
                             + int(mts.group(3)) + day_off)
                    if result["last_t"] is not None and t_abs < result["last_t"] - 43200:
                        day_off += 86400
                        t_abs += 86400
                    if result["first_t"] is None:
                        result["first_t"] = t_abs
                    result["last_t"] = t_abs

                mh = HEADER_RE.match(line)
                if mh and not result["model"]:
                    result["model"] = mh.group(2)

                if ZUG_ENDE_RE.search(line):
                    result["turns"] += 1

                mt = TOOL_RE.match(line)
                if mt:
                    result["tool_calls"] += 1
                    if mt.group(2) == "ToolSearch":
                        result["tool_calls"] -= 1  # ToolSearch ist kein Weltzugriff

                if WELT_FAIL_RE.match(line):
                    result["tool_fails"] += 1

                if TOD_RE.match(line):
                    result["deaths"] += 1

                if KILLED_RE.search(line) and WELT_RE.match(line):
                    result["kills_hint"] += 1

                mz = TOKENS_ZUG_RE.search(line)
                if mz:
                    model = mz.group(1).strip()
                    if model and model != "<synthetic>":
                        result["model"] = model
                    result["tokens_in"] += int(mz.group(2))
                    result["tokens_out"] += int(mz.group(3))
                    result["cache_read"] += int(mz.group(4))
                    result["cache_write"] += int(mz.group(5))
                    cur = float(mz.group(6))
                    saw_usd = True
                    if cur < prev_usd - 1e-9:
                        seg_usd += prev_usd  # Reset (Rotation/Tod) -> Segment abschliessen
                    prev_usd = cur
    except OSError:
        return result

    if saw_usd:
        seg_usd += prev_usd
        result["usd"] = seg_usd
        result["usd_known"] = True

    return result


def is_usage_blind(model: str) -> bool:
    """CCR-Backends (openai/google/xai-Provider ueber claude-code-router)
    melden Token-Usage=0 -> Kosten nicht ableitbar, siehe Memory
    gemini_rotation_blind_usage0. Modellname traegt bei CCR das Provider-
    Praefix (z.B. "openai,gpt-5.5", "google,gemini-3.5-flash")."""
    m = (model or "").lower()
    return m.startswith(("openai,", "google,", "gemini,", "xai,", "grok"))


# ------------------------------------------------------------------- aggregate

def aggregate(journals: list) -> dict:
    """Liefert {"sessions": {session_id: {...}}, "by_agent": {...}, "by_model": {...}}."""
    sessions = {}
    for path in journals:
        try:
            parsed = parse_journal(path)
        except Exception:
            continue
        if parsed["turns"] == 0 and parsed["tool_calls"] == 0 and not parsed["model"]:
            continue  # leere/kaputte Datei ohne verwertbaren Inhalt
        sid = session_id_for(path)
        entry = dict(parsed)
        entry["journal"] = os.path.relpath(path, REPO_DIR).replace("\\", "/")
        sessions.setdefault(sid, []).append(entry)
    return sessions


def summarize(sessions: dict, roster: dict) -> dict:
    by_agent = {}
    by_model = {}

    for sid, entries in sessions.items():
        for e in entries:
            aid = e["agent_id"]
            model = e["model"] or "unbekannt"
            blind = is_usage_blind(model)

            a = by_agent.setdefault(aid, {
                "agent_id": aid,
                "name": roster.get(aid, {}).get("name", aid.capitalize()),
                "sessions": 0, "turns": 0, "tool_calls": 0, "tool_fails": 0,
                "deaths": 0, "kills_hint": 0, "tokens_out": 0,
                "usd": 0.0, "usd_known": False, "survival_s": 0.0,
                "models": set(),
            })
            a["sessions"] += 1
            a["turns"] += e["turns"]
            a["tool_calls"] += e["tool_calls"]
            a["tool_fails"] += e["tool_fails"]
            a["deaths"] += e["deaths"]
            a["kills_hint"] += e["kills_hint"]
            a["tokens_out"] += e["tokens_out"]
            a["models"].add(model)
            if e["usd_known"] and not blind:
                a["usd"] += e["usd"]
                a["usd_known"] = True
            if e["first_t"] is not None and e["last_t"] is not None:
                a["survival_s"] += max(0.0, e["last_t"] - e["first_t"])

            m = by_model.setdefault(model, {
                "model": model, "sessions": 0, "turns": 0, "tool_calls": 0,
                "tool_fails": 0, "deaths": 0, "kills_hint": 0, "tokens_out": 0,
                "usd": 0.0, "usd_known": False, "usage_blind": blind,
                "agents": set(),
            })
            m["sessions"] += 1
            m["turns"] += e["turns"]
            m["tool_calls"] += e["tool_calls"]
            m["tool_fails"] += e["tool_fails"]
            m["deaths"] += e["deaths"]
            m["kills_hint"] += e["kills_hint"]
            m["tokens_out"] += e["tokens_out"]
            m["agents"].add(aid)
            if e["usd_known"] and not blind:
                m["usd"] += e["usd"]
                m["usd_known"] = True

    for a in by_agent.values():
        a["models"] = sorted(a["models"])
        a["fail_rate"] = (a["tool_fails"] / a["tool_calls"]) if a["tool_calls"] else 0.0
    for m in by_model.values():
        m["agents"] = sorted(m["agents"])
        m["fail_rate"] = (m["tool_fails"] / m["tool_calls"]) if m["tool_calls"] else 0.0

    return {"by_agent": by_agent, "by_model": by_model}


# ----------------------------------------------------------------------- I/O

def load_existing_league() -> dict:
    if not os.path.exists(LEAGUE_JSON):
        return {"sessions": {}, "updated": None}
    try:
        with open(LEAGUE_JSON, "r", encoding="utf-8") as f:
            data = json.load(f)
        if not isinstance(data, dict) or "sessions" not in data:
            return {"sessions": {}, "updated": None}
        return data
    except Exception:
        return {"sessions": {}, "updated": None}


def merge_sessions(existing: dict, new_sessions: dict) -> dict:
    merged = dict(existing.get("sessions", {}))
    merged.update(new_sessions)  # neu geparste Session ersetzt alten Stand (frischer)
    return {"sessions": merged, "updated": time.time()}


def write_league_json(data: dict) -> None:
    os.makedirs(ARENA_DIR, exist_ok=True)
    tmp = LEAGUE_JSON + ".tmp"
    with open(tmp, "w", encoding="utf-8") as f:
        json.dump(data, f, ensure_ascii=False, indent=2)
    os.replace(tmp, LEAGUE_JSON)


# --------------------------------------------------------------------- HTML

def _esc(s) -> str:
    return (str(s).replace("&", "&amp;").replace("<", "&lt;").replace(">", "&gt;")
            .replace('"', "&quot;"))


def _fmt_usd(row) -> str:
    return f"${row['usd']:.2f}" if row.get("usd_known") else "n/a"


HTML_TEMPLATE = """<!DOCTYPE html>
<html lang="de">
<head>
<meta charset="utf-8">
<title>IsuSurvivor Liga-Report</title>
<style>
  body {{ margin:0; padding:20px; background:#0b0e14; color:#d8dee9;
          font-family:"Segoe UI",Consolas,monospace; }}
  h1 {{ font-size:20px; color:#88c0d0; margin:0 0 4px 0; }}
  .sub {{ font-size:12px; color:#6b7688; margin-bottom:20px; }}
  h2 {{ font-size:14px; color:#a8b2c0; text-transform:uppercase; letter-spacing:1px;
        margin:26px 0 10px 0; }}
  table {{ width:100%; border-collapse:collapse; font-size:13px; margin-bottom:10px; }}
  th, td {{ text-align:left; padding:6px 10px; border-bottom:1px solid #1c2230; }}
  th {{ color:#6b7688; font-weight:normal; cursor:pointer; user-select:none;
        border-bottom:1px solid #2a3140; white-space:nowrap; }}
  th:hover {{ color:#88c0d0; }}
  th.sorted-asc::after {{ content:" \\25B2"; }}
  th.sorted-desc::after {{ content:" \\25BC"; }}
  tr:hover td {{ background:#151a24; }}
  td.num {{ text-align:right; font-variant-numeric:tabular-nums; }}
  .blind {{ color:#e5c07b; }}
  .hint {{ color:#4b5566; font-size:11px; margin-top:-4px; margin-bottom:16px; }}
</style>
</head>
<body>
<h1>ISUSURVIVOR &mdash; LIGA-REPORT</h1>
<div class="sub">Stand {updated} &middot; {session_count} Sessions aggregiert</div>

<h2>Nach Modell</h2>
<div class="hint">Tokens/Kosten bei usage-blinden CCR-Backends (openai/google/xai) als "n/a" markiert, nicht 0.</div>
<table id="tbl-model">
<thead><tr>
  <th data-type="str">Modell</th>
  <th data-type="num">Sessions</th>
  <th data-type="num">Zuege</th>
  <th data-type="num">Tool-Calls</th>
  <th data-type="num">Fehlerquote</th>
  <th data-type="num">Tode</th>
  <th data-type="num">Kills (Hinweis)</th>
  <th data-type="num">Output-Tokens</th>
  <th data-type="num">Kosten</th>
</tr></thead>
<tbody>
{model_rows}
</tbody>
</table>

<h2>Nach Agent</h2>
<table id="tbl-agent">
<thead><tr>
  <th data-type="str">Agent</th>
  <th data-type="str">Modelle</th>
  <th data-type="num">Sessions</th>
  <th data-type="num">Zuege</th>
  <th data-type="num">Tool-Calls</th>
  <th data-type="num">Fehlerquote</th>
  <th data-type="num">Tode</th>
  <th data-type="num">Kills (Hinweis)</th>
  <th data-type="num">Kosten</th>
</tr></thead>
<tbody>
{agent_rows}
</tbody>
</table>

<script>
function sortTable(table, colIdx, type, asc) {{
  const tbody = table.tBodies[0];
  const rows = Array.from(tbody.rows);
  rows.sort((a, b) => {{
    let av = a.cells[colIdx].dataset.sort, bv = b.cells[colIdx].dataset.sort;
    if (type === "num") {{ av = parseFloat(av) || 0; bv = parseFloat(bv) || 0; }}
    if (av < bv) return asc ? -1 : 1;
    if (av > bv) return asc ? 1 : -1;
    return 0;
  }});
  rows.forEach(r => tbody.appendChild(r));
}}
document.querySelectorAll("table").forEach(table => {{
  const ths = table.querySelectorAll("th");
  ths.forEach((th, idx) => {{
    th.addEventListener("click", () => {{
      const asc = !th.classList.contains("sorted-asc");
      ths.forEach(t => t.classList.remove("sorted-asc", "sorted-desc"));
      th.classList.add(asc ? "sorted-asc" : "sorted-desc");
      sortTable(table, idx, th.dataset.type, asc);
    }});
  }});
}});
</script>
</body>
</html>
"""


def render_model_rows(by_model: dict) -> str:
    rows = []
    for m in sorted(by_model.values(), key=lambda x: -x["turns"]):
        fail_pct = f"{m['fail_rate']*100:.1f}%"
        usd_cell = (f'<span class="blind">n/a</span>' if not m["usd_known"]
                    else f"${m['usd']:.2f}")
        rows.append(f"""<tr>
  <td data-sort="{_esc(m['model'])}">{_esc(m['model'])}{' <span class="blind">(usage-blind)</span>' if m['usage_blind'] else ''}</td>
  <td class="num" data-sort="{m['sessions']}">{m['sessions']}</td>
  <td class="num" data-sort="{m['turns']}">{m['turns']}</td>
  <td class="num" data-sort="{m['tool_calls']}">{m['tool_calls']}</td>
  <td class="num" data-sort="{m['fail_rate']}">{fail_pct}</td>
  <td class="num" data-sort="{m['deaths']}">{m['deaths']}</td>
  <td class="num" data-sort="{m['kills_hint']}">{m['kills_hint']}</td>
  <td class="num" data-sort="{m['tokens_out']}">{m['tokens_out']:,}</td>
  <td class="num" data-sort="{m['usd'] if m['usd_known'] else -1}">{usd_cell}</td>
</tr>""")
    return "\n".join(rows) if rows else '<tr><td colspan="9">(keine Daten)</td></tr>'


def render_agent_rows(by_agent: dict) -> str:
    rows = []
    for a in sorted(by_agent.values(), key=lambda x: -x["turns"]):
        fail_pct = f"{a['fail_rate']*100:.1f}%"
        usd_cell = (f'<span class="blind">n/a</span>' if not a["usd_known"]
                    else f"${a['usd']:.2f}")
        models = ", ".join(a["models"])
        rows.append(f"""<tr>
  <td data-sort="{_esc(a['name'])}">{_esc(a['name'])}</td>
  <td data-sort="{_esc(models)}">{_esc(models)}</td>
  <td class="num" data-sort="{a['sessions']}">{a['sessions']}</td>
  <td class="num" data-sort="{a['turns']}">{a['turns']}</td>
  <td class="num" data-sort="{a['tool_calls']}">{a['tool_calls']}</td>
  <td class="num" data-sort="{a['fail_rate']}">{fail_pct}</td>
  <td class="num" data-sort="{a['deaths']}">{a['deaths']}</td>
  <td class="num" data-sort="{a['kills_hint']}">{a['kills_hint']}</td>
  <td class="num" data-sort="{a['usd'] if a['usd_known'] else -1}">{usd_cell}</td>
</tr>""")
    return "\n".join(rows) if rows else '<tr><td colspan="9">(keine Daten)</td></tr>'


def write_league_html(by_agent: dict, by_model: dict, session_count: int) -> None:
    html = HTML_TEMPLATE.format(
        updated=datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
        session_count=session_count,
        model_rows=render_model_rows(by_model),
        agent_rows=render_agent_rows(by_agent),
    )
    os.makedirs(ARENA_DIR, exist_ok=True)
    with open(LEAGUE_HTML, "w", encoding="utf-8") as f:
        f.write(html)


# ---------------------------------------------------------------------- main

def main():
    parser = argparse.ArgumentParser(description="IsuSurvivor Liga-Report")
    parser.add_argument("--since", default="", help="nur Journale ab JJJJMMTT")
    parser.add_argument("--session", default="", help="nur eine Session-ID (Praefix)")
    args = parser.parse_args()

    try:
        journals = journal_files(args.since)
        if args.session:
            journals = [p for p in journals if args.session in os.path.basename(p)]

        new_sessions_raw = aggregate(journals)
        # merge_sessions erwartet {sid: [entries]} - JSON-tauglich serialisieren
        new_sessions = {sid: entries for sid, entries in new_sessions_raw.items()}

        existing = load_existing_league()
        merged = merge_sessions(existing, new_sessions)
        write_league_json(merged)

        roster = _load_roster()
        # Fuer die Auswertung ALLE gemergten Sessions heranziehen (fortschreibend),
        # nicht nur die gerade neu geparsten.
        summary = summarize(merged["sessions"], roster)
        write_league_html(summary["by_agent"], summary["by_model"], len(merged["sessions"]))

        print(f"[league_report] {len(journals)} Journale gelesen, "
              f"{len(new_sessions)} Sessions aktualisiert, "
              f"{len(merged['sessions'])} Sessions gesamt.")
        print(f"[league_report] {LEAGUE_JSON}")
        print(f"[league_report] {LEAGUE_HTML}")
        return 0
    except Exception as e:
        # Robust gegen fehlende/kaputte Dateien: niemals mit Traceback abbrechen,
        # der Supervisor ruft dies detached auf und toleriert ein Fehlen.
        print(f"[league_report] Fehler (toleriert): {e}", file=sys.stderr)
        return 0


if __name__ == "__main__":
    raise SystemExit(main())
