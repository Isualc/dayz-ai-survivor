#!/usr/bin/env python3
"""IsuSurvivor Testtreiber (Protokoll v0.2) — manuelle Steuerung der Bridge.

Beispiele:
  python test_driver.py demo            Phase-1-Akzeptanztest (spawn + move)
  python test_driver.py demo2           Phase-2-Akzeptanztest (loot + essen)
  python test_driver.py spawn --x 4525 --z 2470
  python test_driver.py spawn-item --item Apple
  python test_driver.py pickup --item Apple
  python test_driver.py eat / drink / equip / engage / flee / adopt
  python test_driver.py zombie          Infizierten 25 m vor dem NPC spawnen
  python test_driver.py tp              verbundenen Spieler zum NPC teleportieren
  python test_driver.py state / watch

Das autonome Gehirn startet separat: python run_agent.py (siehe README).
"""

import argparse
import os
import sys
import time

from bridge import Bridge, DEFAULT_PROFILE

# Balota-Airstrip: flach, offen, gut zum Zuschauen
DEMO_SPAWN = (4525.0, 2470.0)
DEMO_TARGET = (4605.0, 2470.0)


def run_cmd(bridge: Bridge, action: str, label: str | None = None, **kwargs) -> dict:
    """Befehl senden, Fortschritt anzeigen, Endstatus ausgeben."""
    last_line = [""]

    def on_progress(cmd: dict):
        dist = cmd.get("dist_to_target", -1.0)
        line = f"  running... dist={dist:.1f} m"
        if line != last_line[0]:
            print(line)
            last_line[0] = line

    cmd_id = bridge.send(action, **kwargs)
    result = bridge.wait_status(cmd_id, on_progress=on_progress)
    detail = result.get("detail") or ""
    print(f"{label or action}: {result.get('status')} {detail}".rstrip())
    return result


def print_state(state: dict | None) -> None:
    if state is None:
        print("Kein state.json gefunden. Server gestartet? Pfad korrekt (--profile)?")
        return
    npc = state.get("npc", {})
    cmd = state.get("command", {})
    print(f"seq={state.get('seq')}  uptime={state.get('uptime', 0):.0f}s  "
          f"bridge={state.get('bridge_version')}")
    if npc.get("spawned"):
        flags = []
        if npc.get("fighting"):
            flags.append("FIGHTING")
        if not npc.get("alive"):
            flags.append("TOT")
        print(f"NPC  {npc.get('classname')}  {' '.join(flags)}")
        print(f"     pos=({npc.get('pos_x', 0):.1f}, {npc.get('pos_y', 0):.1f}, "
              f"{npc.get('pos_z', 0):.1f})  hands={npc.get('in_hands') or '-'}")
        print(f"     health={npc.get('health', 0):.0f}  blood={npc.get('blood', 0):.0f}  "
              f"water={npc.get('water', 0):.0f}  energy={npc.get('energy', 0):.0f}")
        print(f"     magen: volume={npc.get('stomach_volume', 0):.0f}")
    else:
        print("NPC  nicht gespawnt")
    print(f"CMD  id={cmd.get('id') or '-'}  action={cmd.get('action') or '-'}  "
          f"status={cmd.get('status')}  {cmd.get('detail') or ''}")
    inventory = state.get("inventory", [])
    if inventory:
        print(f"INV  ({len(inventory)} Items)")
        for it in inventory[:12]:
            hands = " [HAND]" if it.get("in_hands") else ""
            print(f"     {it.get('classname')}  {it.get('kind')}  "
                  f"q={it.get('quantity', 0):.0f}{hands}")
        if len(inventory) > 12:
            print(f"     ... und {len(inventory) - 12} weitere")
    nearby = state.get("nearby", [])
    if nearby:
        kinds: dict[str, int] = {}
        for e in nearby:
            kinds[e.get("kind", "?")] = kinds.get(e.get("kind", "?"), 0) + 1
        print("NEAR " + "  ".join(f"{k}={v}" for k, v in sorted(kinds.items())))
    for msg in state.get("chat", [])[-3:]:
        print(f"CHAT [{msg.get('channel')}] {msg.get('sender')}: {msg.get('text')}")
    for err in state.get("errors", [])[-3:]:
        print(f"ERR  {err}")


def ensure_npc(bridge: Bridge) -> bool:
    npc = (bridge.read_state() or {}).get("npc", {})
    if npc.get("spawned") and npc.get("alive"):
        return True
    print(f"  NPC fehlt, spawne bei {DEMO_SPAWN}...")
    result = run_cmd(bridge, "spawn", label="  spawn", x=DEMO_SPAWN[0], z=DEMO_SPAWN[1])
    return result.get("status") == "done"


def inventory_has(bridge: Bridge, classname: str) -> bool:
    inv = (bridge.read_state() or {}).get("inventory", [])
    return any(it.get("classname") == classname for it in inv)


def cmd_demo(bridge: Bridge) -> int:
    print("=== IsuSurvivor Phase-1 Demo (Bewegung) ===")
    if bridge.state_fresh() is None:
        print("Bridge antwortet nicht - Server + servermod pruefen.")
        return 1
    print("[1/3] Bridge lebt.")
    if run_cmd(bridge, "ping").get("status") != "done":
        return 1
    print("[2/3] ping ok.")
    if not ensure_npc(bridge):
        return 1
    t0 = time.monotonic()
    result = run_cmd(bridge, "move_to", x=DEMO_TARGET[0], z=DEMO_TARGET[1])
    if result.get("status") == "done":
        print(f"=== ERFOLG: angekommen in {time.monotonic() - t0:.0f}s ===")
        return 0
    return 1


def cmd_demo2(bridge: Bridge) -> int:
    print("=== IsuSurvivor Phase-2 Demo (Loot + Essen) ===")

    print("[1/6] Bridge-Check...")
    if bridge.state_fresh() is None:
        print("Bridge antwortet nicht - Server + servermod pruefen.")
        return 1

    print("[2/6] NPC sicherstellen...")
    if not ensure_npc(bridge):
        return 1

    before = (bridge.read_state() or {}).get("npc", {})
    stomach_before = before.get("stomach_volume", 0.0)

    print("[3/6] Apple vor dem NPC spawnen...")
    if run_cmd(bridge, "spawn_item", text="Apple").get("status") != "done":
        return 1

    print("[4/6] pickup Apple (hinlaufen + aufnehmen)...")
    if run_cmd(bridge, "pickup", text="Apple").get("status") != "done":
        return 1

    time.sleep(2.0)
    if inventory_has(bridge, "Apple"):
        print("      Apple ist im Inventar.")
    else:
        print("      WARNUNG: Apple nicht im Inventar-Snapshot gefunden.")

    print("[5/6] eat...")
    if run_cmd(bridge, "eat").get("status") != "done":
        return 1

    print("[6/6] Magen-Check (Volumen muss steigen)...")
    time.sleep(3.0)
    after = (bridge.read_state() or {}).get("npc", {})
    stomach_after = after.get("stomach_volume", 0.0)
    print(f"      stomach_volume: {stomach_before:.0f} -> {stomach_after:.0f}")

    if stomach_after > stomach_before:
        print("=== ERFOLG: Phase-2-Akzeptanztest bestanden "
              "(looten + essen + Verdauung). ===")
        return 0

    print("=== FEHLGESCHLAGEN: stomach_volume ist nicht gestiegen. ===")
    return 1


def cmd_watch(bridge: Bridge) -> int:
    print("Beobachte state.json (Strg+C zum Beenden)...")
    last_seq = -1
    try:
        while True:
            state = bridge.read_state()
            if state and state.get("seq") != last_seq:
                last_seq = state.get("seq")
                os.system("")
                print("\x1b[2J\x1b[H", end="")
                print_state(state)
            time.sleep(1.0)
    except KeyboardInterrupt:
        return 0


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__,
                                     formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--profile", default=DEFAULT_PROFILE,
                        help=f"Server-Profilordner (Default: {DEFAULT_PROFILE})")
    parser.add_argument("--npc-id", default="viktor",
                        help="Agenten-Slot (Arena), Default viktor")

    sub = parser.add_subparsers(dest="cmd", required=True)
    for simple in ("ping", "state", "watch", "demo", "demo2", "stop", "despawn",
                   "eat", "drink", "equip", "engage", "adopt", "unfollow",
                   "unstick"):
        sub.add_parser(simple)

    p_spawn = sub.add_parser("spawn")
    p_spawn.add_argument("--x", type=float, required=True)
    p_spawn.add_argument("--z", type=float, required=True)
    p_spawn.add_argument("--y", type=float, default=0.0)
    p_spawn.add_argument("--loadout", default="")

    p_move = sub.add_parser("move")
    p_move.add_argument("--x", type=float, required=True)
    p_move.add_argument("--z", type=float, required=True)
    p_move.add_argument("--y", type=float, default=0.0)

    p_pickup = sub.add_parser("pickup")
    p_pickup.add_argument("--item", default="", help="Classname-Filter, leer = naechstes Item")

    p_flee = sub.add_parser("flee")
    p_flee.add_argument("--x", type=float, default=0.0, help="optional: wovon weg")
    p_flee.add_argument("--z", type=float, default=0.0)

    p_tp = sub.add_parser("tp")
    p_tp.add_argument("--name", default="", help="Spielername, leer = erster Spieler")

    p_say = sub.add_parser("say")
    p_say.add_argument("--text", required=True)

    p_voice = sub.add_parser("voice")
    p_voice.add_argument("--id", required=True, help="phrase_id aus voice/soundsets.json")
    sub.add_parser("voices")

    p_loot = sub.add_parser("loot")
    p_loot.add_argument("--max", type=int, default=6)
    sub.add_parser("lootscore")

    p_craft = sub.add_parser("craft")
    p_craft.add_argument("--recipe", required=True)
    for t in ("cookmeal", "water", "explore", "buildfence", "lootcorpse", "lootbox"):
        sub.add_parser(t)
    p_find = sub.add_parser("findi")
    p_find.add_argument("--pattern", required=True)

    p_follow = sub.add_parser("follow")
    p_follow.add_argument("--name", default="", help="Spielername, leer = naechster Spieler")

    p_give = sub.add_parser("give")
    p_give.add_argument("--item", required=True, help="Classname, direkt ins Inventar")
    sub.add_parser("vexit")

    p_drop = sub.add_parser("drop")
    p_drop.add_argument("--item", default="", help="Classname; leer = Item in der Hand")

    p_door = sub.add_parser("door")
    p_door.add_argument("--action", default="open", choices=["open", "close"])

    p_si = sub.add_parser("spawn-item")
    p_si.add_argument("--item", required=True, help="z.B. Apple, CanisterGasoline, Mosin9130")
    p_si.add_argument("--x", type=float, default=0.0)
    p_si.add_argument("--z", type=float, default=0.0)

    p_zmb = sub.add_parser("zombie")
    p_zmb.add_argument("--class", dest="classname", default="",
                       help="Default: ZmbM_HermitSkinny_Beige")
    p_zmb.add_argument("--x", type=float, default=0.0)
    p_zmb.add_argument("--z", type=float, default=0.0)

    args = parser.parse_args()
    bridge = Bridge(args.profile, args.npc_id)

    if args.cmd == "state":
        print_state(bridge.read_state())
        return 0
    if args.cmd == "watch":
        return cmd_watch(bridge)
    if args.cmd == "demo":
        return cmd_demo(bridge)
    if args.cmd == "demo2":
        return cmd_demo2(bridge)

    simple_map = {
        "ping": "ping", "stop": "stop", "despawn": "despawn", "eat": "eat",
        "drink": "drink", "equip": "equip_best", "engage": "engage",
        "adopt": "adopt_nearest", "unfollow": "unfollow", "unstick": "unstick",
        "vexit": "vehicle_exit",
    }
    if args.cmd in simple_map:
        return 0 if run_cmd(bridge, simple_map[args.cmd]).get("status") == "done" else 1

    if args.cmd == "spawn":
        r = run_cmd(bridge, "spawn", x=args.x, y=args.y, z=args.z, loadout=args.loadout)
        return 0 if r.get("status") == "done" else 1
    if args.cmd == "move":
        r = run_cmd(bridge, "move_to", x=args.x, y=args.y, z=args.z)
        return 0 if r.get("status") == "done" else 1
    if args.cmd == "pickup":
        r = run_cmd(bridge, "pickup", text=args.item)
        return 0 if r.get("status") == "done" else 1
    if args.cmd == "flee":
        r = run_cmd(bridge, "flee", x=args.x, z=args.z)
        return 0 if r.get("status") == "done" else 1
    if args.cmd == "tp":
        r = run_cmd(bridge, "teleport_player", text=args.name)
        return 0 if r.get("status") == "done" else 1
    if args.cmd == "say":
        r = run_cmd(bridge, "say", text=args.text)
        return 0 if r.get("status") == "done" else 1
    if args.cmd in ("craft", "cookmeal", "water", "explore", "buildfence", "findi",
                    "lootcorpse", "lootbox"):
        import tactics
        if args.cmd == "craft":
            print(tactics.craft(bridge, args.recipe))
        elif args.cmd == "cookmeal":
            print(tactics.cook_meal(bridge))
        elif args.cmd == "water":
            print(tactics.water_run(bridge))
        elif args.cmd == "explore":
            print(tactics.explore_step(bridge))
        elif args.cmd == "buildfence":
            r = run_cmd(bridge, "build_fence_frame")
            return 0 if r.get("status") == "done" else 1
        elif args.cmd == "lootcorpse":
            r = run_cmd(bridge, "loot_corpse")
            return 0 if r.get("status") == "done" else 1
        elif args.cmd == "lootbox":
            r = run_cmd(bridge, "loot_container")
            return 0 if r.get("status") == "done" else 1
        elif args.cmd == "findi":
            print(tactics.find_item(bridge, args.pattern))
        return 0
    if args.cmd in ("loot", "lootscore"):
        import tactics
        if args.cmd == "lootscore":
            state = bridge.read_state() or {}
            inv = state.get("inventory", [])
            for e in state.get("nearby", []):
                if e.get("kind") == "item":
                    s = tactics.score_ground_item(e, inv)
                    print(f"{s:4}  {e.get('classname')}  ({e.get('distance', 0):.0f} m)")
            return 0
        result = tactics.loot_area(bridge, max_items=args.max)
        print(f"Eingesammelt: {result['haul'] or 'nichts'}")
        if result["failed"]:
            print(f"Nicht erreichbar: {result['failed']}")
        print(tactics.equip_best(bridge))
        return 0
    if args.cmd in ("voice", "voices"):
        import json as _json
        ss_file = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))),
                               "voice", "soundsets.json")
        with open(ss_file, "r", encoding="utf-8") as f:
            catalog = _json.load(f)
        if args.cmd == "voices":
            for pid, info in catalog.items():
                print(f"{pid:12} [{info.get('category')}] {info.get('text')}")
            return 0
        info = catalog.get(args.id)
        if not info:
            print(f"Unbekannte phrase_id: {args.id} (Liste: voices)")
            return 1
        r = run_cmd(bridge, "say_voice", text=info["soundset"])
        return 0 if r.get("status") == "done" else 1
    if args.cmd == "follow":
        r = run_cmd(bridge, "follow", text=args.name)
        return 0 if r.get("status") == "done" else 1
    if args.cmd == "give":
        r = run_cmd(bridge, "give_item", text=args.item)
        return 0 if r.get("status") == "done" else 1
    if args.cmd == "drop":
        r = run_cmd(bridge, "drop", text=args.item)
        return 0 if r.get("status") == "done" else 1
    if args.cmd == "door":
        r = run_cmd(bridge, "door", text=args.action)
        return 0 if r.get("status") == "done" else 1
    if args.cmd == "spawn-item":
        r = run_cmd(bridge, "spawn_item", text=args.item, x=args.x, z=args.z)
        return 0 if r.get("status") == "done" else 1
    if args.cmd == "zombie":
        r = run_cmd(bridge, "spawn_infected", text=args.classname, x=args.x, z=args.z)
        return 0 if r.get("status") == "done" else 1

    return 2


if __name__ == "__main__":
    sys.exit(main())
