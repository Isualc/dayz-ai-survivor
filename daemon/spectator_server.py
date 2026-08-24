"""IsuSurvivor Zuschauer-Server — read-only Webserver fuer Twitch/OBS.

Aggregiert die state_<id>.json / intent_<id>.txt der Bridge sowie die letzten
Journal-Zeilen jedes aktiven Agenten und liefert sie ueber ein einfaches
JSON-API plus ein eingebettetes Single-File-HTML-Dashboard aus. Schreibt
NIEMALS in commands_<id>.json oder sonst irgendeine Steuerdatei - reiner
Beobachter, wie der Orchestrator selbst.

Nebenbei: Track-Logger, der alle 5s Position/HP/Blut jedes Agenten nach
arena/tracks_<session>.jsonl anhaengt (Grundlage fuer match_story.py und
league_report.py).

Aufruf:
    python daemon\\spectator_server.py [--port 8090] [--session <name>]

Port ueber Env ISU_SPECTATOR_PORT ueberschreibbar (Default 8090).
Nur stdlib, keine Abhaengigkeiten.
"""

import argparse
import glob
import json
import os
import re
import sys
import threading
import time
from datetime import datetime
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib.parse import urlparse

REPO_DIR = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
ARENA_DIR = os.path.join(REPO_DIR, "arena")
AGENTS_FILE = os.path.join(ARENA_DIR, "agents.json")

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
try:
    from bridge import DEFAULT_PROFILE
except Exception:
    DEFAULT_PROFILE = os.path.join(os.environ.get("DAYZ_SERVER_DIR", r"C:\Program Files (x86)\Steam\steamapps\common\DayZServer"), "profiles")

BRIDGE_DIR = os.path.join(DEFAULT_PROFILE, "IsuSurvivor")

TOKENS_ZUG_RE = re.compile(
    r"\[TOKENS ZUG\] ([^:]+): .*?\| Session: .*?, ([\d.]+) USD")
SAY_RE = re.compile(r"^\[(\d\d:\d\d:\d\d)\] \[([A-Z]+)\] (.*)$")
LINE_RE = re.compile(r"^\[(\d\d:\d\d:\d\d)\] (.*)$")


def _load_roster() -> list:
    """id/name/model aus arena/agents.json; tolerant bei Fehlern/Fehlfeldern."""
    try:
        with open(AGENTS_FILE, "r", encoding="utf-8") as f:
            data = json.load(f)
    except Exception:
        return []
    out = []
    for a in data.get("agents", []) if isinstance(data, dict) else []:
        if not isinstance(a, dict) or not a.get("id"):
            continue
        out.append({
            "id": a["id"],
            "name": a.get("name", a["id"].capitalize()),
            "model": a.get("model", "?"),
        })
    return out


def _read_json(path: str):
    try:
        with open(path, "r", encoding="utf-8", errors="replace") as f:
            return json.load(f)
    except Exception:
        return None


def _read_text(path: str) -> str:
    try:
        with open(path, "r", encoding="utf-8", errors="replace") as f:
            return f.read().strip()
    except Exception:
        return ""


def _agent_home_dirs(npc_id: str) -> list:
    """Journal-Verzeichnis(se) eines Agenten. Viktor legacy in agent_home/,
    alle anderen (und ggf. neuere Viktor-Sessions) in agent_homes/<id>/."""
    dirs = []
    specific = os.path.join(REPO_DIR, "agent_homes", npc_id, "journal")
    if os.path.isdir(specific):
        dirs.append(specific)
    legacy = os.path.join(REPO_DIR, "agent_home", "journal")
    if npc_id == "viktor" and os.path.isdir(legacy):
        dirs.append(legacy)
    return dirs


def _latest_journal(npc_id: str) -> str:
    best_path, best_mtime = "", -1.0
    for d in _agent_home_dirs(npc_id):
        for p in glob.glob(os.path.join(d, "journal_*.log")):
            try:
                m = os.path.getmtime(p)
            except OSError:
                continue
            if m > best_mtime:
                best_mtime, best_path = m, p
    return best_path


def _tail_lines(path: str, n: int = 400) -> list:
    """Simples Tail: bei Journal-Groessen im Bereich weniger MB reicht ein
    kompletter Read + Slice, kein Seek-Backwards-Gefrickel noetig."""
    if not path:
        return []
    try:
        with open(path, "r", encoding="utf-8", errors="replace") as f:
            lines = f.readlines()
    except OSError:
        return []
    return lines[-n:]


def _last_meaningful(npc_id: str) -> dict:
    """Letzte Aktion/Gedanke/Kosten aus dem juengsten Journal ableiten.
    Prioritaet fuer 'Gedanke': [<NAME>]-Sagezeile > [WECKRUF]. Aktion: letzte
    [TOOL]-Zeile. Kosten: letzte [TOKENS ZUG]-Zeile (Modell, kumulierte USD)."""
    path = _latest_journal(npc_id)
    lines = _tail_lines(path, 300)
    thought, action, model, usd = "", "", "", None
    for raw in lines:
        line = raw.rstrip("\n")
        m = SAY_RE.match(line)
        if not m:
            continue
        _, tag, rest = m.groups()
        if tag == "TOOL":
            action = rest.strip()
        elif tag not in ("WELT", "TOKENS", "TOD"):
            # z.B. [VIKTOR]/[BIRGIT]/[IGOR]/[KONRAD]/[WECKRUF]
            # ("ZUG ENDE" hat ein Leerzeichen im Tag und matcht SAY_RE eh nicht)
            thought = rest.strip()
    for raw in lines:
        line = raw.rstrip("\n")
        mt = TOKENS_ZUG_RE.search(line)
        if mt:
            model = mt.group(1).strip()
            try:
                usd = float(mt.group(2))
            except ValueError:
                usd = None
    return {"thought": thought, "action": action, "model": model, "usd": usd,
            "journal": os.path.basename(path) if path else ""}


def build_state() -> dict:
    roster = _load_roster()
    agents = []
    for r in roster:
        npc_id = r["id"]
        state = _read_json(os.path.join(BRIDGE_DIR, f"state_{npc_id}.json")) or {}
        npc = state.get("npc", {}) if isinstance(state, dict) else {}
        intent = _read_text(os.path.join(BRIDGE_DIR, f"intent_{npc_id}.txt"))
        extra = _last_meaningful(npc_id)
        agents.append({
            "id": npc_id,
            "name": npc.get("name") or r["name"],
            "model": extra["model"] or r["model"],
            "spawned": bool(npc.get("spawned")),
            "alive": bool(npc.get("alive")),
            "x": npc.get("pos_x"),
            "z": npc.get("pos_z"),
            "health": npc.get("health"),
            "blood": npc.get("blood"),
            "in_hands": npc.get("in_hands", ""),
            "fighting": bool(npc.get("fighting")),
            "action": extra["action"],
            "thought": intent or extra["thought"],
            "usd": extra["usd"],
        })
    return {"t": time.time(), "agents": agents}


class TrackLogger(threading.Thread):
    """Haengt alle INTERVAL Sekunden einen Positions-Snapshot pro Agent an
    arena/tracks_<session>.jsonl an. Reiner Beobachter, keine Steuerdatei."""

    INTERVAL = 5.0

    def __init__(self, session: str):
        super().__init__(daemon=True)
        self._stop = threading.Event()
        os.makedirs(ARENA_DIR, exist_ok=True)
        self.path = os.path.join(ARENA_DIR, f"tracks_{session}.jsonl")

    def run(self):
        while not self._stop.is_set():
            try:
                snap = build_state()
                with open(self.path, "a", encoding="utf-8") as f:
                    f.write(json.dumps(snap, ensure_ascii=False) + "\n")
            except Exception:
                pass  # Track-Logging darf den Server nie mitreissen
            self._stop.wait(self.INTERVAL)

    def stop(self):
        self._stop.set()


DASHBOARD_HTML = r"""<!DOCTYPE html>
<html lang="de">
<head>
<meta charset="utf-8">
<title>IsuSurvivor - Zuschauer</title>
<style>
  * { box-sizing: border-box; }
  body {
    margin: 0; padding: 16px; background: #0b0e14; color: #d8dee9;
    font-family: "Segoe UI", Consolas, monospace;
  }
  h1 { font-size: 18px; color: #88c0d0; margin: 0 0 12px 0; letter-spacing: 1px; }
  #layout { display: flex; gap: 16px; align-items: flex-start; }
  #cards { display: flex; flex-direction: column; gap: 10px; flex: 0 0 380px; }
  .card {
    background: #151a24; border: 1px solid #2a3140; border-radius: 8px;
    padding: 10px 12px; position: relative; overflow: hidden;
  }
  .card.dead { opacity: 0.45; filter: grayscale(0.8); }
  .card .bar { position: absolute; left: 0; top: 0; bottom: 0; width: 4px; }
  .name { font-size: 15px; font-weight: 600; }
  .model { font-size: 11px; color: #6b7688; margin-left: 6px; }
  .row { font-size: 12px; color: #a8b2c0; margin-top: 3px; }
  .thought { font-size: 12px; color: #e5c07b; margin-top: 5px; font-style: italic; }
  .action { font-size: 11px; color: #7ec699; margin-top: 3px; font-family: Consolas, monospace; }
  .hpwrap { background: #262b38; border-radius: 4px; height: 6px; margin-top: 6px; overflow: hidden; }
  .hpfill { background: #a3be8c; height: 100%; }
  .hpfill.low { background: #d08770; }
  .hpfill.crit { background: #bf616a; }
  #canvaswrap {
    background: #10141c; border: 1px solid #2a3140; border-radius: 8px;
    padding: 8px; flex: 1 1 auto;
  }
  canvas { width: 100%; height: 640px; display: block; background: #0d1119; border-radius: 4px; }
  #legend { font-size: 11px; color: #6b7688; margin-top: 6px; }
  #legend span { margin-right: 14px; }
  #ts { font-size: 11px; color: #4b5566; margin-top: 10px; }
</style>
</head>
<body>
<h1>ISUSURVIVOR &mdash; ZUSCHAUER-DASHBOARD</h1>
<div id="layout">
  <div id="cards"></div>
  <div id="canvaswrap">
    <canvas id="plot" width="900" height="640"></canvas>
    <div id="legend"></div>
  </div>
</div>
<div id="ts"></div>
<script>
const COLORS = ["#88c0d0", "#d08770", "#a3be8c", "#b48ead", "#ebcb8b", "#bf616a"];
const trail = {};
const TRAIL_MAX = 120;

function colorFor(id, idx) { return COLORS[idx % COLORS.length]; }

function hpClass(hp) {
  if (hp === null || hp === undefined) return "";
  if (hp < 30) return "crit";
  if (hp < 60) return "low";
  return "";
}

function renderCards(agents) {
  const wrap = document.getElementById("cards");
  wrap.innerHTML = "";
  agents.forEach((a, i) => {
    const col = colorFor(a.id, i);
    const div = document.createElement("div");
    div.className = "card" + (a.alive ? "" : " dead");
    const hp = (a.health === null || a.health === undefined) ? 0 : a.health;
    const usd = (a.usd === null || a.usd === undefined) ? "n/a" : ("$" + a.usd.toFixed(2));
    div.innerHTML = `
      <div class="bar" style="background:${col}"></div>
      <span class="name">${a.name || a.id}</span><span class="model">${a.model || "?"}</span>
      <div class="row">${a.alive ? (a.spawned ? "lebt" : "wartet") : "gefallen"} &middot;
        HP ${hp.toFixed ? hp.toFixed(0) : hp} &middot; Blut ${(a.blood||0).toFixed ? a.blood.toFixed(0) : a.blood}
        &middot; ${a.in_hands || "leere Haende"}</div>
      <div class="row">Pos: ${a.x != null ? a.x.toFixed(0) : "?"} / ${a.z != null ? a.z.toFixed(0) : "?"}
        &middot; Kosten: ${usd}</div>
      <div class="hpwrap"><div class="hpfill ${hpClass(hp)}" style="width:${Math.max(0,Math.min(100,hp))}%"></div></div>
      ${a.action ? `<div class="action">${a.action}</div>` : ""}
      ${a.thought ? `<div class="thought">&ldquo;${a.thought}&rdquo;</div>` : ""}
    `;
    wrap.appendChild(div);
  });
}

function renderPlot(agents) {
  const cvs = document.getElementById("plot");
  const ctx = cvs.getContext("2d");
  ctx.clearRect(0, 0, cvs.width, cvs.height);

  const pts = agents.filter(a => a.x != null && a.z != null);
  if (pts.length === 0) return;
  let minX = Math.min(...pts.map(a => a.x)), maxX = Math.max(...pts.map(a => a.x));
  let minZ = Math.min(...pts.map(a => a.z)), maxZ = Math.max(...pts.map(a => a.z));
  const pad = 60;
  minX -= pad; maxX += pad; minZ -= pad; maxZ += pad;
  if (maxX - minX < 1) { maxX += 50; minX -= 50; }
  if (maxZ - minZ < 1) { maxZ += 50; minZ -= 50; }

  function toScreen(x, z) {
    const sx = (x - minX) / (maxX - minX) * (cvs.width - 20) + 10;
    const sz = cvs.height - ((z - minZ) / (maxZ - minZ) * (cvs.height - 20) + 10);
    return [sx, sz];
  }

  agents.forEach((a, i) => {
    if (a.x == null || a.z == null) return;
    const col = colorFor(a.id, i);
    if (!trail[a.id]) trail[a.id] = [];
    trail[a.id].push([a.x, a.z]);
    if (trail[a.id].length > TRAIL_MAX) trail[a.id].shift();

    ctx.strokeStyle = col;
    ctx.globalAlpha = 0.5;
    ctx.lineWidth = 1.5;
    ctx.beginPath();
    trail[a.id].forEach((p, j) => {
      const [sx, sz] = toScreen(p[0], p[1]);
      if (j === 0) ctx.moveTo(sx, sz); else ctx.lineTo(sx, sz);
    });
    ctx.stroke();
    ctx.globalAlpha = 1.0;

    const [sx, sz] = toScreen(a.x, a.z);
    ctx.fillStyle = a.alive ? col : "#555";
    ctx.beginPath();
    ctx.arc(sx, sz, a.alive ? 7 : 5, 0, Math.PI * 2);
    ctx.fill();
    ctx.strokeStyle = "#0d1119";
    ctx.lineWidth = 2;
    ctx.stroke();
    ctx.fillStyle = "#d8dee9";
    ctx.font = "11px Consolas, monospace";
    ctx.fillText(a.name || a.id, sx + 10, sz + 4);
  });

  const legend = document.getElementById("legend");
  legend.innerHTML = agents.map((a, i) =>
    `<span style="color:${colorFor(a.id, i)}">&#9679; ${a.name || a.id}</span>`).join("");
}

async function poll() {
  try {
    const res = await fetch("/api/state");
    const data = await res.json();
    renderCards(data.agents || []);
    renderPlot(data.agents || []);
    document.getElementById("ts").textContent =
      "aktualisiert " + new Date(data.t * 1000).toLocaleTimeString("de-AT");
  } catch (e) {
    document.getElementById("ts").textContent = "Verbindung verloren...";
  }
}

poll();
setInterval(poll, 1000);
</script>
</body>
</html>
"""


class Handler(BaseHTTPRequestHandler):
    server_version = "IsuSpectator/1.0"

    def log_message(self, fmt, *args):
        pass  # Konsole nicht mit jedem Poll zuspammen

    def _send(self, code: int, body: bytes, content_type: str):
        self.send_response(code)
        self.send_header("Content-Type", content_type)
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Cache-Control", "no-store")
        self.send_header("Access-Control-Allow-Origin", "*")
        self.end_headers()
        try:
            self.wfile.write(body)
        except (BrokenPipeError, ConnectionAbortedError, ConnectionResetError):
            pass

    def do_GET(self):
        path = urlparse(self.path).path
        if path in ("/", "/index.html"):
            self._send(200, DASHBOARD_HTML.encode("utf-8"), "text/html; charset=utf-8")
        elif path == "/api/state":
            try:
                body = json.dumps(build_state(), ensure_ascii=False).encode("utf-8")
                self._send(200, body, "application/json; charset=utf-8")
            except Exception as e:
                err = json.dumps({"error": str(e)}).encode("utf-8")
                self._send(500, err, "application/json; charset=utf-8")
        else:
            self._send(404, b"not found", "text/plain; charset=utf-8")

    def do_POST(self):
        # Read-only Server: POST/PUT/DELETE gibt es nicht, niemals auf
        # commands_<id>.json oder sonst etwas schreiben.
        self._send(405, b"read-only", "text/plain; charset=utf-8")


def main():
    parser = argparse.ArgumentParser(description="IsuSurvivor Zuschauer-Server")
    parser.add_argument("--port", type=int,
                         default=int(os.environ.get("ISU_SPECTATOR_PORT", "8090")))
    parser.add_argument("--session", default=datetime.now().strftime("%Y%m%d_%H%M%S"),
                         help="Session-ID fuer die tracks_<session>.jsonl")
    parser.add_argument("--no-tracks", action="store_true",
                         help="Track-Logger deaktivieren (nur Dashboard/API)")
    args = parser.parse_args()

    tracker = None
    if not args.no_tracks:
        tracker = TrackLogger(args.session)
        tracker.start()
        print(f"[spectator] Track-Log: {tracker.path}")

    httpd = ThreadingHTTPServer(("0.0.0.0", args.port), Handler)
    print(f"[spectator] Dashboard: http://127.0.0.1:{args.port}/")
    print(f"[spectator] API:       http://127.0.0.1:{args.port}/api/state")
    print("[spectator] read-only - schreibt nie in commands_*.json. Strg+C zum Beenden.")
    try:
        httpd.serve_forever()
    except KeyboardInterrupt:
        pass
    finally:
        if tracker:
            tracker.stop()
        httpd.server_close()


if __name__ == "__main__":
    main()
