#!/usr/bin/env python3
"""Schneller MCP-Handshake-Test fuer dayz_mcp.py ohne Claude Code.

Spricht rohes JSON-RPC ueber stdio: initialize -> tools/list -> observe.
"""

import json
import os
import subprocess
import sys

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
    proc = subprocess.Popen(
        [sys.executable, os.path.join(DAEMON_DIR, "dayz_mcp.py")],
        stdin=subprocess.PIPE, stdout=subprocess.PIPE,
        stderr=subprocess.DEVNULL, text=True, encoding="utf-8", bufsize=1,
    )
    try:
        rpc(proc, {"jsonrpc": "2.0", "id": 1, "method": "initialize", "params": {
            "protocolVersion": "2024-11-05",
            "capabilities": {},
            "clientInfo": {"name": "smoke", "version": "0.0.1"},
        }})
        init = read_response(proc, 1)
        print("initialize ok:", init["result"]["serverInfo"]["name"])

        rpc(proc, {"jsonrpc": "2.0", "method": "notifications/initialized"})

        rpc(proc, {"jsonrpc": "2.0", "id": 2, "method": "tools/list"})
        tools = read_response(proc, 2)
        names = [t["name"] for t in tools["result"]["tools"]]
        print("tools:", ", ".join(names))

        rpc(proc, {"jsonrpc": "2.0", "id": 3, "method": "tools/call",
                   "params": {"name": "observe", "arguments": {}}})
        obs = read_response(proc, 3)
        text = obs["result"]["content"][0]["text"]
        print("observe:")
        for line in text.splitlines()[:8]:
            print("  " + line)
        print("SMOKE OK")
        return 0
    finally:
        proc.stdin.close()
        proc.wait(timeout=10)


if __name__ == "__main__":
    sys.exit(main())
