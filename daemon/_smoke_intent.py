#!/usr/bin/env python3
"""Smoke-Test fuer das intent()-Tool (Gedanken-HUD).

Ruft intent() ueber den MCP-Server auf und prueft, dass intent_<id>.txt
atomar mit dem erwarteten UTF-8-Inhalt (inkl. Umlaut) geschrieben wird.
Braucht den DayZ-Server NICHT - testet nur den Daemon-/Datei-Pfad.
"""

import json
import os
import subprocess
import sys

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from bridge import Bridge, DEFAULT_PROFILE

DAEMON_DIR = os.path.dirname(os.path.abspath(__file__))


def rpc(proc, payload):
    proc.stdin.write(json.dumps(payload) + "\n")
    proc.stdin.flush()


def read_response(proc, want_id):
    while True:
        line = proc.stdout.readline()
        if line == "":
            raise RuntimeError("MCP-Server hat stdout geschlossen")
        line = line.strip()
        if not line:
            continue
        msg = json.loads(line)
        if msg.get("id") == want_id:
            return msg


def main() -> int:
    test_text = "Zur Huette im Sueden, dort uebernachten"  # ASCII-Variante des Klartexts
    test_text = "Zur Hütte im Süden, dort übernachten"  # echte Umlaute
    intent_path = os.path.join(Bridge(DEFAULT_PROFILE, "viktor").dir, "intent_viktor.txt")

    try:
        os.remove(intent_path)
    except OSError:
        pass

    proc = subprocess.Popen(
        [sys.executable, os.path.join(DAEMON_DIR, "dayz_mcp.py")],
        stdin=subprocess.PIPE, stdout=subprocess.PIPE,
        stderr=subprocess.DEVNULL, text=True, encoding="utf-8", bufsize=1,
    )
    try:
        rpc(proc, {"jsonrpc": "2.0", "id": 1, "method": "initialize", "params": {
            "protocolVersion": "2024-11-05",
            "capabilities": {},
            "clientInfo": {"name": "smoke-intent", "version": "0.0.1"},
        }})
        read_response(proc, 1)
        rpc(proc, {"jsonrpc": "2.0", "method": "notifications/initialized"})
        rpc(proc, {"jsonrpc": "2.0", "id": 2, "method": "tools/call", "params": {
            "name": "intent", "arguments": {"text": test_text},
        }})
        res = read_response(proc, 2)
        reply = res["result"]["content"][0]["text"]
        print("intent-Tool-Antwort:", reply)
    finally:
        proc.stdin.close()
        proc.wait(timeout=10)

    if not os.path.exists(intent_path):
        print(f"[FAIL] {intent_path} wurde nicht geschrieben")
        return 1
    with open(intent_path, "r", encoding="utf-8") as f:
        content = f.read().strip()
    print(f"intent_viktor.txt: '{content}'")
    if content == test_text:
        print("SMOKE INTENT OK")
        return 0
    print("[FAIL] Inhalt weicht vom gesendeten Text ab")
    return 1


if __name__ == "__main__":
    sys.exit(main())
