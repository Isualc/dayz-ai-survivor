#!/usr/bin/env python3
"""Messlauf: Promptgroesse + Erstzug-Dauer eines local/-Backends, OHNE DayZ.

Startet Claude Code headless exakt wie run_agent (gleiche MCP-Tools, gleiche
Persona, gleiche Tool-Diaet) gegen den lokalen llama-server und misst die
Zeit bis zum result-Event. Die Promptgroesse steht danach im llama-Log
("prompt eval" / n_prompt_tokens).

Aufruf: python daemon\\_measure_local.py [modell]   (Default local/gemma-4-E4B-it)
"""
import json
import os
import sys
import time

DAEMON = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, DAEMON)
import run_agent as ra  # noqa: E402

MODEL = sys.argv[1] if len(sys.argv) > 1 else "local/gemma-4-E4B-it"
_SERVER_DIR = os.environ.get("DAYZ_SERVER_DIR", r"C:\Program Files (x86)\Steam\steamapps\common\DayZServer")
PROFILE = os.path.join(_SERVER_DIR, "profiles")

ra.AGENT_HOME = os.path.join(os.path.dirname(DAEMON), "agent_homes", "birgit")
ra.AGENT_NAME = "Birgit"
os.makedirs(os.path.join(ra.AGENT_HOME, "journal"), exist_ok=True)

mcp_cfg = ra.build_mcp_config(PROFILE, "birgit", "")
character = os.path.join(DAEMON, "characters", "sanitaeter.md")
proc = ra.spawn_claude(mcp_cfg, MODEL, character, 2)
print(f"[mess] Claude Code PID {proc.pid}, Modell {MODEL} - sende Prompt...",
      flush=True)
ra.send_user(proc, "Antworte NUR mit dem Wort OK. Benutze keine Werkzeuge.")
t0 = time.time()

try:
    for line in proc.stdout:
        line = line.strip()
        if not line:
            continue
        try:
            msg = json.loads(line)
        except json.JSONDecodeError:
            continue
        mtype = msg.get("type")
        if mtype == "system" and msg.get("subtype") == "init":
            ntools = len(msg.get("tools") or [])
            print(f"[mess] init nach {time.time() - t0:.0f}s, "
                  f"{ntools} Tools in der Session", flush=True)
        elif mtype == "result":
            dt = time.time() - t0
            usage = msg.get("usage") or {}
            print(f"[mess] RESULT nach {dt:.0f}s "
                  f"({dt / 60:.1f} min)", flush=True)
            print("[mess] usage: " + json.dumps(usage), flush=True)
            print("[mess] text: " + str(msg.get("result"))[:200], flush=True)
            break
finally:
    try:
        proc.stdin.close()
    except Exception:
        pass
    try:
        proc.wait(timeout=10)
    except Exception:
        proc.kill()
print("[mess] fertig.", flush=True)
