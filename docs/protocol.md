# IsuSurvivor File-Bridge — Protokoll v0.2 (Phase 2)

Zwei JSON-Dateien im Server-Profilordner verbinden Mod und Daemon:

```
<DayZServer>\profiles\IsuSurvivor\
    state.json      Mod -> Daemon   (jede Sekunde komplett neu geschrieben)
    commands.json   Daemon -> Mod   (Mailbox: Daemon legt ab, Mod konsumiert + löscht)
```

## Mailbox-Semantik

1. Der Daemon schreibt `commands.json` nur, wenn die Datei **nicht existiert** (vorher atomar über `.tmp` + `os.replace`).
2. Die Mod liest die Datei im nächsten Tick, löscht sie sofort und führt die Befehle der Reihe nach aus.
3. Befehls-Ergebnisse erscheinen in `state.json` unter `command` (gematcht über `id`).
4. Ein neuer Befehl verdrängt einen laufenden (der alte taucht in `errors` auf). Phase 1 kennt keine Befehls-Queue, der Daemon serialisiert selbst.

`state.json` wird von der Mod ohne Rename direkt geschrieben. Der Daemon muss Parse-Fehler tolerieren und kurz später erneut lesen (passiert bei 1-Hz-Polling selten).

## state.json

```json
{
  "seq": 1234,
  "uptime": 567.0,
  "bridge_version": "0.1.0",
  "npc": {
    "spawned": true,
    "alive": true,
    "classname": "eAI_SurvivorF_Linda",
    "pos_x": 4525.0, "pos_y": 12.3, "pos_z": 2470.0,
    "heading": 90.0,
    "health": 100.0,
    "blood": 5000.0,
    "water": 3000.0,
    "energy": 4000.0,
    "stomach_volume": 120.0,
    "in_hands": "AKM",
    "fighting": false
  },
  "inventory": [
    { "classname": "Apple", "kind": "food", "quantity": 125.0,
      "health": 100.0, "in_hands": false },
    { "classname": "QuiltedJacket_Blue", "kind": "clothing", "quantity": 0.0,
      "health": 95.0, "in_hands": false, "worn": true,
      "warmth": 0.85, "cargo_size": 20, "slot": "Body" }
  ],
  "command": {
    "id": "a1b2c3d4e5f6",
    "action": "move_to",
    "status": "running",
    "detail": "",
    "dist_to_target": 42.5
  },
  "nearby": [
    { "kind": "player", "classname": "SurvivorM_Boris", "name": "Clausi",
      "x": 4530.0, "y": 12.0, "z": 2468.0, "distance": 5.4 }
  ],
  "chat": [
    { "id": 3, "channel": 0, "sender": "Clausi", "text": "hallo bot", "uptime": 555.0 }
  ],
  "errors": []
}
```

- `command.status`: `idle` → noch nie ein Befehl | `running` | `done` | `failed` (Grund in `detail`)
- `inventory[].worn`: am Körper getragen (Kleidungs-/Schulter-Slot, nicht Hand, nicht Cargo). `warmth`/`cargo_size`/`slot` liefert die Mod nur für `kind=="clothing"` aus der Item-Config (heatIsolation, Cargo-Slots Breite×Höhe, erster inventorySlot) — Grundlage für `dress_best`. Boden-Kleidung (`nearby` mit `item_kind=="clothing"`) trägt dieselben drei Felder.
- `nearby.kind`: `player`, `ai`, `infected`, `animal`, `vehicle`, `item`. Radius 100 m, Items nur bis 20 m und max. 25 Stück, gesamt max. 40 Einträge.
- `chat`: Ringpuffer der letzten 30 Nachrichten. `id` ist monoton, der Daemon dedupliziert darüber.
- `errors`: Ringpuffer der letzten 10 Bridge-Fehler.

## commands.json

```json
{
  "commands": [
    { "id": "a1b2c3d4e5f6", "action": "pickup",
      "x": 0.0, "y": 0.0, "z": 0.0, "loadout": "", "text": "Apple" }
  ]
}
```

Alle Felder immer mitschicken (der EnforceScript-JsonFileLoader mag keine Überraschungen). `y <= 0` heißt: Mod ermittelt die Bodenhöhe selbst. `text` ist der generische String-Parameter (Classname-Filter oder Spielername).

### Aktionen v0.2

| action | Parameter | Wirkung | Endstatus |
|---|---|---|---|
| `ping` | — | Lebenszeichen | sofort `done` |
| `spawn` | `x`, `z`, (`y`), (`loadout`) | Spawnt eAI-Survivor (Fraktion Civilian, default `HumanLoadout.json`) | `done` / `failed` |
| `move_to` | `x`, `z`, (`y`) | Wegpunkt, Verhalten ONCE | `running` → `done` < 3 m; `failed` bei 45 s ohne Fortschritt / 600 s |
| `stop` | — | Wegpunkte löschen, HALT, Tempo normal | `done` |
| `despawn` | — | NPC entfernen | `done` / `failed` |
| `pickup` | (`text` = Classname-Filter) | Nächstes Bodenitem (50 m): hinlaufen + `eAI_TakeItemToInventory` | `running` → `done` (detail = Classname) |
| `eat` | — | Erstes Essbares im Inventar komplett konsumieren (Magen-Pipeline inkl. Agents) | `done` / `failed` |
| `drink` | — | Wie `eat`, aber Flüssigkeitsbehälter | `done` / `failed` |
| `equip_best` | — | Erste Feuerwaffe aus dem Inventar in die Hand | `done` / `failed` |
| `engage` | — | Nächster Infizierter (100 m): Target registrieren + hinlaufen, eAI kämpft ab kurzer Distanz selbst | `running` → `done` wenn Ziel tot; `failed` nach 180 s |
| `flee` | (`x`, `z` = wovon weg) | 150 m vom nächsten Infizierten (oder Koordinate) weg, Sprint | `running` → `done` / `failed` |
| `adopt_nearest` | — | Verwaiste lebende eAI übernehmen (nach Server-Restart) | `done` / `failed` |
| `teleport_player` | (`text` = Spielername) | Verbundenen Spieler neben den NPC teleportieren (Dev-Helfer) | `done` / `failed` |
| `spawn_item` | `text` = Classname, (`x`, `z`) | Item vor dem NPC oder an Koordinate spawnen (Dev-Helfer) | `done` / `failed` |
| `spawn_infected` | (`text`), (`x`, `z`) | Infizierten spawnen, default `ZmbM_HermitSkinny_Beige`, 25 m voraus (Dev-Helfer) | `done` / `failed` |
| `say` | `text` | Chat an Spieler in Rufweite (60 m) via engine-nativem `ChatMP` + Loopback ins eigene Chat-Log | `done` / `failed` |
| `follow` | (`text` = Spielername) | Gruppenbeitritt beim Spieler (näheste bei leerem Filter), eAI-Formation folgt automatisch. Endet bei eigener Bewegung (move_to/flee/engage/stop) | `done` / `failed` |
| `unfollow` | — | Zurück in die eigene Gruppe, stehenbleiben | `done` |

Seit v0.3 trägt `state.npc` zusätzlich `name` (Chat-Absendername, via `spawn.text` setzbar, Default "Viktor") und `following` (bool). `spawn.text` = Anzeigename des Survivors. Seit v0.4: `unconscious`, `in_vehicle` plus Befehle `unstick`, `vehicle_exit`, `give_item`, `drop`. Seit v0.5 (Survival-Tiefe): `drink_well`, `fill_container` (Brunnen in 4 m nötig, kind=water), `consume_item` (text+y, pile-bewusst über stackedUnit=pcs), `light_fire`, `cook` (brennendes Feuer in 4 m, gart alles Rohe), `build_fence_frame` (2x WoodenLog aus Inventar oder Boden in 5 m, experimentell); neue nearby-Kinds `water`, `fire`, `fire_burning`. Die Rezept-/Ketten-Logik (craft, cook_meal, drink_at_well, find_item, explore_step) lebt in `daemon/tactics.py`.

### Seit v0.8 (Welt & Medizin)

`state.json` trägt zusätzlich einen `world`-Block (Top-Level, neben `npc`) sowie `wet` und `disease` in `npc`:

```json
"world": { "time": "14:30", "sun": "day", "rain": 0.2, "fog": 0.05, "wind": 4.3, "temp_c": 18.5 },
"npc": { "wet": 0.0, "disease": { "agents": { "cholera": 180 }, "sick": true } }
```

- `world.time`: In-Game-Uhrzeit "HH:MM" (`GetWorld().GetDate`).
- `world.sun`: `dawn` | `day` | `dusk` | `night` — Heuristik nach Jahreszeit (Sommer ca. 5–21 Uhr, Übergangszeit 6–19, Winter 8–17), keine echte Sonnenstandsberechnung.
- `world.rain` / `world.fog`: 0..1 (`Weather.GetRain()/GetFog().GetActual()`), `world.wind` in m/s, `world.temp_c` = Umgebungstemperatur am NPC in °C (Environment, rechnet Höhe/Innenraum/Wärmequellen ein).
- `npc.wet`: 0..1 (Vanilla-Stat WET kennt nur 0/1).
- `npc.disease.agents`: NUR Erreger über der Melde-Schwelle (halbe Vanilla-Aktivierungsschwelle: cholera 125, salmonella 30, influenza 300, wound 50, brain 1000), Wert = Agent-Count. `sick` = mindestens ein Erreger drüber. Der Daemon liest tolerant: fehlender Block = gesund.

Neue Aktionen:

| action | Parameter | Wirkung | Endstatus |
|---|---|---|---|
| `find_water` | (`max_dist` = Suchradius m, Default 300) | Radiales Sampling (8 Richtungen, 25-m-Schritte) nach offenem Wasser (`SurfaceIsPond`/`SurfaceIsSea`); liefert den letzten LANDpunkt vor dem Wassertreffer, damit der NPC am Ufer steht statt zu schwimmen | `done` (detail = `x z pond\|sea`) / `failed` |
| `treat_other` | `target` = Name, `item` = Classname | Patient (Spieler oder Agent) in 3 m behandeln, Item wird aus dem NPC-Inventar verbraucht: BandageDressing/Rag = stärkste Blutungsquelle schließen, Splint = Beinbruch schienen, SalineBagIV/BloodBagIV = +500 Blut (Blutgruppen-Check bewusst weggelassen) | `done` / `failed` |

`spawn_infected` versteht jetzt zusätzlich `x`, `z` und `count` (1..10, hart gekappt): mit Koordinaten spawnen die Infizierten mit 5–10 m Streuung um den Punkt (detail = `<classname> x<n>` bei mehr als einem), ohne Koordinaten bleibt exakt das alte Verhalten (25 m voraus, 1 Stück). `count` und `max_dist` sind in der Mod float-Felder (der EnforceScript-JsonFileLoader deserialisiert ints unzuverlässig, siehe `IsuCommand.br`).

## Geplant für Phase 3+

MCP-Server im Daemon + Claude-Code-Session als Gehirn; `say` (Chat senden, braucht Client-RPC oder Expansion-Chat); gehörte Schüsse als Events; `loot_area` als komponierte Taktik im Daemon (Serie aus pickup); Basebuilding.
