# Survival- und Modell-Upgrade, 8. September 2026

## Was sich im Spielablauf aendert

Der Runner fuehrt zwischen Modellzuegen alle fuenf Sekunden einen kurzen lokalen
Survival-Schritt aus. Ein Schritt startet hoechstens einen echten Mod-Befehl und
kehrt sofort zurueck. Das Modell plant komplexere Vorhaben, waehrend die lokale
Schicht Grundbeduerfnisse abarbeitet. Laufende Befehle, Reisen, Follow-Modus,
Fahrzeuge, Bewusstlosigkeit und veraltete Serverdaten werden beruecksichtigt.
Im Battle-Royale und bei `--once` startet keine zusaetzliche lokale Routine.

Die Prioritaeten umfassen Blutung/Gefahr, sicheren Verzehr, Trinkwasser,
Temperatur/Kleidung, Medizin, Inventar, Loot, Jagd, Tierverwertung und Kochen.
Im freien Modus darf der Controller auch nahe unbesuchte Suchzellen erkunden.
Komplexes Crafting wird weiter ueber die vorhandenen `recipes`/`craft`-Werkzeuge
vom Modell geplant. Es gibt keinen zusaetzlichen kostenpflichtigen Ersatzanbieter.

Die Mod meldet nun Garzustand, gefrorene Nahrung und sicheren Fluessigkeitsinhalt.
Medikamente sind `medical`, Getraenkedosen `drink`. `eat` lehnt Medikamente,
rohes Fleisch, Menschenfleisch, Eingeweide, verdorbenes/verbranntes oder gefrorenes
Essen ab. `drink` akzeptiert nur erkannte sichere Getraenke. Kleine Portionen und
eine Magengrenze verhindern das bisherige Leeren kompletter Vorratsbehaelter.
`take_medicine` nimmt genau eine Dosis eines unterstuetzten oralen Medikaments.

Reife Pflanzen werden an vorhandenen Gaerten erkannt. `harvest_crops` erntet
eine reife Pflanze in drei Metern Reichweite ueber die Vanilla-Harvest-Methode.
Die Fruechte liegen anschliessend als echtes Loot am Boden. Die Pflanze wird
weder kuenstlich gereift noch werden neue Gaerten erzeugt.

`observe(full=true)` zeigt jetzt tatsaechlich das ganze vom Server gelieferte
Inventar und die ganze Umgebungsliste. Zuvor blieb die Inventarliste trotz
dieser Option auf 15 Gegenstaende begrenzt.

## Modellwahl und Start

Im Einfg-Menue stehen die neuen Anbieter zur Auswahl. Bestehende Slots und
gespeicherte Modellentscheidungen werden nicht automatisch umgestellt.

| Auswahl / `--model` | Anbindung | Voraussetzung |
| --- | --- | --- |
| `codex/default` | Codex CLI, Konto-Standard | Installierte CLI, bestehender ChatGPT-Login |
| `codex/gpt-5.6-luna` | Codex CLI, sparsame Modelloption | Freigabe im Konto |
| `codex/gpt-5.6-terra` / `codex/gpt-5.6-sol` | Codex CLI | Freigabe im Konto |
| `codex/gpt-6-astra` | Codex CLI, optional fuer anspruchsvolle Planung | Freigabe im Konto |
| `antigravity/gemini-3.8-flash-low` | Antigravity CLI, explizites Flash-Modell | Installierte `agy` CLI, vorhandener Google-Login |
| `antigravity/default` | Antigravity CLI, Konto-Standard | Installierte `agy` CLI, vorhandener Google-Login |
| `gemini-cli/default` | Bisherige Gemini CLI | Nur weiterhin berechtigter Code-Assist-Zugang; persönlicher Login seit 18.06.2026 eingestellt |
| `mistral/mistral-small-latest` | Direkte Mistral API | `MISTRAL_API_KEY` |
| `mistral/mistral-medium-latest` / `mistral/mistral-large-latest` | Direkte Mistral API | `MISTRAL_API_KEY` |
| `mistral/ministral-3b-latest`, `-8b-latest`, `-14b-latest` | Direkte Mistral API | `MISTRAL_API_KEY` |
| `moonshot/kimi-k2.6` / `moonshot/kimi-k3` | Direkte Moonshot API | `MOONSHOT_API_KEY` |
| `moonshot/kimi-k2.7-code` / `moonshot/kimi-k2.7-code-highspeed` | Direkte Moonshot API | `MOONSHOT_API_KEY` |
| `claude-fable-5-1` | Claude Code mit bestehendem Login | Modellzugang im Plan |
| `api/claude-fable-5-1` | Claude Code mit Anthropic API | `ANTHROPIC_API_KEY` |

Andere Modell-IDs bleiben ueber `--model`, Roster oder Arena-Request moeglich.
`fable-5.1`, `fable-5-1` und deren `api/`-Varianten werden auf
`claude-fable-5-1` aufgeloest. Das bestehende `fable` bleibt Fable 5.
Fable 5.1 hat immer aktives adaptives Denken; der Runner setzt fuer dieses Modell
standardmaessig `effort=low` statt Thinking zu deaktivieren. Die Denkstufe kann
mit `ISU_THINKING_EFFORT` uebersteuert werden.

Beispiel fuer eine freie Survival-Runde nach dem Mod-Update:

```powershell
python daemon/run_agent.py --model codex/default --free --no-restore --no-tp --no-mic --no-voice-procs --idle 300 --turn-limit 6
```

Mit `--no-autonomy` wird die lokale Routine deaktiviert. `--survival-interval 5`
aendert ihren Abstand (Minimum zwei Sekunden). `--free` bedeutet ein Leben;
`--no-restore` vermeidet das Wiederherstellen alter Inventarsnapshots beim Start.
Die vorhandenen Spawn-/Loadout-Regeln bleiben erhalten.

Keys lassen sich mit `tools/set_api_keys.ps1` verdeckt setzen. Danach muss der
Supervisor in einer neuen Konsole starten, damit er die Umgebungsvariablen erbt.
CLI-Pfade lassen sich bei Bedarf mit `ISU_CODEX_CLI` bzw. `ISU_GEMINI_CLI`
angeben. API-Basisadressen sind mit `MISTRAL_BASE_URL` bzw. `MOONSHOT_BASE_URL`
konfigurierbar. Die Defaults verwenden die offiziellen HTTPS-Endpunkte.

## Kosten und Fehlertoleranz

Codex/Gemini CLI verwenden ihre bestehenden Kontologins. Die CLI-Kindprozesse
erhalten keine API-Keys als stillen Ausweichweg. Das spart API-Abrechnung,
garantiert aber keine kostenlose oder unbegrenzte Nutzung: Konto- und
Modelllimits gelten weiterhin. Mistral und Moonshot werden direkt abgerechnet.

Der native Worker begrenzt Kontext, Ausgabe, Werkzeugrunden und Laufzeiten.
Alle neuen Modelle entscheiden ueber dieselben DayZ-Werkzeuge und dieselbe
JSON-Struktur. Die CLIs liefern Entscheidungen; der Worker fuehrt die
Spielwerkzeuge aus. Host-Shells und weitere CLI-Werkzeuge sind deaktiviert.
Modellantworten werden vor der Ausfuehrung validiert.

Standardgrenzen des nativen Workers: sechs Entscheidungen pro Zug; der Runner
uebergibt sein `--turn-limit` (Standard zehn, im Beispiel oben sechs).
Weitere Defaults sind 1.200 API-Ausgabetokens,
30.000 Zeichen rollende Historie, 90 Sekunden je Modellanfrage.
`ISU_LLM_MAX_OUTPUT_TOKENS`, `ISU_LLM_CONTEXT_CHARS`,
`ISU_LLM_REQUEST_TIMEOUT`, `ISU_LLM_TURN_SECONDS` und
`ISU_LLM_TOOL_TIMEOUT` erlauben begrenzte Anpassungen. Bei Quota-, Auth- oder
Netzfehlern greift eine wachsende Pause bis maximal fuenf Minuten; es gibt
keine blinde Sofort-Wiederholung und keinen kostenpflichtigen Anbieterwechsel.

Wo der Anbieter keine Dollarwerte liefert, zeigen Journal und Arena
ausdruecklich unbekannte Kosten. Tokens werden erfasst, soweit geliefert.
Eine Einsparquote oder ein besserer Survival-Score wurde noch nicht gemessen.
Astra und Fable 5.1 sind optionale starke Planer, keine automatischen Eskalationsmodelle.

## Was hier Lernen bedeutet

`survival_learning.json` liegt pro NPC im bisherigen Agenten-Home. Die lokale
Routine speichert begrenzte Episoden mit Vorher-/Nachherzustand, Ergebnis,
Fehlschlaegen, Belohnung und besuchten Suchzellen. Ein `done` allein zaehlt
nicht automatisch als gelungene Versorgung: Inventar, Vitalwerte, Mageninhalt,
Position oder Garzustand muessen die Wirkung stuetzen. Fehlschlaege bremsen
Wiederholungen; Suche beruecksichtigt schon besuchte Zellen. `survival_memory`
macht diese Erfahrung jedem Modell zugaenglich.

Die persoenlichen Notizen in `CLAUDE.md` und `memory/*.md` bleiben erhalten.
Auch die neuen Backends koennen diese begrenzt lesen und schreiben.
Das ist persistentes Erfahrungswissen mit angepasster Handlungswahl, kein
Fine-Tuning und keine Veraenderung der Gewichte des verwendeten Modells.
Es ist nicht garantiert, dass jede zusaetzliche Spielstunde die Leistung steigert.

## Verbleibende Grenzen

- Aussaat, Bewaesserung und Pflanzenpflege brauchen noch echte Mod-Aktionen.
- Das vorhandene Angeln und Teile des Craftings sind abstrahiert bzw. simuliert.
  Sie sind noch keine vollstaendige Nachbildung der Spielerinteraktionen.
- Navigation und Kampf haengen weiterhin von Expansion-AI und der Weltgeometrie ab.
- Die Server-Sicht liefert begrenzte Umgebungsdaten; ein groesseres Modell kann
  fehlende Telemetrie und fehlende Spielaktionen nicht ersetzen.
- Klinische Symptome, Kuehlung und Kleidung werden heuristisch priorisiert.
  Langzeit-Survival muss unter realen DayZ-Bedingungen getestet werden.

Fuer einen belastbaren Modellvergleich gleiche Karte, Spawn, Loadout und
Survival-Dauer verwenden; ueber mehrere Laeufe Ueberlebenszeit, kritische
Vitalzustandsdauer, erfolgreiche Beschaffungsaktionen, Fehlerversuche sowie
Tokens je Spielstunde vergleichen. Ein einzelner guter Lauf beweist wenig.

## Aenderungen pruefen und laden

Originale geaenderter Dateien liegen unter `_BACKUP_20260908_survival_upgrade`.
Dieser Ordner ist eine Dateisicherung; im Projekt war kein Git-Repository vorhanden.
Gepackte PBOs liegen unter `build/survival-upgrade-20260908/`.
Beide PBOs wurden ausserdem in den konfigurierten lokalen Server unter
`%DAYZ_SERVER_DIR%/@IsuSurvivor/addons/` bzw.
`@IsuVoice/addons/` kopiert und ihre SHA256-Hashes mit dem Build verglichen.
Die vorher installierten PBOs liegen in der Sicherung unter `deployed_pbos/`.
Server und Client waren dabei beendet und wurden nicht gestartet.
Die Mod muss als neue PBO geladen werden, damit Telemetrie, Medizin und Ernte
funktionieren. Ein reiner Python-Neustart genuegt dafuer nicht.

```powershell
python daemon/test_offline_tools.py
python daemon/test_pickup_match.py
python daemon/test_runner_integration.py
python daemon/test_preflight_provider_config.py
python daemon/test_llm_backends.py
python daemon/test_survival.py
```

Ein erfolgreicher PBO-Packvorgang ist keine EnforceScript-Laufzeitpruefung.
Die Kombination aus neuem Mod-Build, realem Modellzugang und Spielverhalten
braucht noch einen Server-/Client-Probelauf. Offline-Tests stellen keine
Live-Verifikation der Provider oder eine gemessene Survival-Verbesserung dar.

Pruefergebnis: 74 neue Unittests (7 Runner/Beobachtung, 12 Konfiguration,
25 Provider/Worker, 30 Survival) sowie beide vorhandenen Offline-Testskripte
bestanden. Python-Kompilierung, PowerShell-Syntaxpruefung, CLI-Argumentpruefung
ohne Modellaufruf und beide AddonBuilder-Packvorgaenge erfolgreich.
Der Provider-Test umfasst Fake-CLI/Fake-MCP sowie Kindprozessbereinigung unter
Windows; er sendet keine bezahlten Modellanfragen.

## Gepruefte Anbieterquellen

- [Codex nichtinteraktiv](https://learn.chatgpt.com/docs/non-interactive-mode)
- [Codex Modelle](https://learn.chatgpt.com/docs/models)
- [Gemini CLI Headless](https://geminicli.com/docs/cli/headless/)
- [Mistral Chat API](https://docs.mistral.ai/api)
- [Mistral Modell-IDs](https://docs.mistral.ai/vibe/code/cli/configuration)
- [Aktuelle Kimi-Modelle](https://platform.kimi.ai/docs/models)
- [GPT-6 Astra](https://developers.openai.com/api/docs/models/gpt-6-astra)
- [Fable 5.1](https://platform.claude.com/docs/en/models/fable-5-1/overview)
