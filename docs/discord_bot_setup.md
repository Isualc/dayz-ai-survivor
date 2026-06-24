# Discord-Bot für Viktors Stimme — Schritt-für-Schritt

Diese Anleitung richtet den Discord-Bot ein, über den Viktor in deinem Sprachkanal spricht. Stand: Juni 2026. Discord zeigt das Developer-Portal je nach Konto-Sprache teils deutsch, teils englisch, deshalb stehen beide Bezeichnungen dabei.

Wichtig vorab: Dein normaler Einladungslink (discord.gg/...) funktioniert für Bots **nicht**. Bots kommen ausschließlich über den OAuth2-Weg unten auf einen Server, und nur du als Serverbesitzer (oder jemand mit "Server verwalten"-Recht) kannst sie einladen.

## Schritt 1: Anwendung anlegen

1. Öffne <https://discord.com/developers/applications> und melde dich mit deinem Discord-Konto an.
2. Oben rechts auf **"New Application"** ("Neue Anwendung") klicken.
3. Name eingeben: `Viktor` (das wird der angezeigte Bot-Name). Nutzungsbedingungen abhaken, **"Create"** ("Erstellen").

## Schritt 2: Bot-Token holen

1. In der linken Seitenleiste auf **"Bot"** klicken. Discord hat den Bot-Benutzer beim Anlegen automatisch erstellt.
2. Beim Feld Token auf **"Reset Token"** ("Token zurücksetzen") klicken und bestätigen (ggf. 2FA-Code).
3. Den angezeigten Token **sofort kopieren**, er wird nur einmal angezeigt. Das ist quasi das Passwort des Bots: niemals in Code oder Chats einfügen, nur in die Umgebungsvariable (Schritt 5).
4. Auf derselben Seite unter "Privileged Gateway Intents": **alle drei Schalter können AUS bleiben** (Presence, Server Members, Message Content). Für reines Sprechen im Voice-Kanal braucht der Bot keine privilegierten Intents.

## Schritt 3: Einladungs-URL bauen (OAuth2 URL Generator)

1. Links auf **"OAuth2"** klicken, dann zum Abschnitt **"OAuth2 URL Generator"** scrollen.
2. Unter **"Scopes"** genau eines anhaken: `bot`.
3. Darunter erscheint **"Bot Permissions"** ("Bot-Berechtigungen"). Anhaken:
   - `View Channels` ("Kanäle anzeigen")
   - `Connect` ("Verbinden")
   - `Speak` ("Sprechen")
4. Unter der generierten URL sitzt ein Dropdown **"Integration Type"**: auf **"Guild Install"** stellen. Steht es auf "User Install", lehnt Discord die URL später mit "Ungültige Bereiche für die Benutzerinstallation bereitgestellt" ab, weil der `bot`-Scope nur für Server-Installationen existiert.
5. Ganz unten die **generierte URL kopieren**. Kontrolle: In der URL muss `integration_type=0` stehen (oder der Parameter fehlt ganz), nicht `integration_type=1`.

Falls der URL Generator "Guild Install" gar nicht anbietet: Links auf **"Installation"** klicken und unter **"Installation Contexts"** das Häkchen bei **"Guild Install"** setzen, speichern, dann zurück zum URL Generator.

## Schritt 4: Bot auf deinen Server einladen

1. Die kopierte URL im Browser öffnen (im selben Browser, in dem du bei Discord angemeldet bist).
2. Im Dialog deinen Server auswählen → **"Weiter"** → Berechtigungen prüfen → **"Autorisieren"** ("Authorize"), ggf. Captcha.
3. Der Bot erscheint jetzt offline in deiner Mitgliederliste. Das ist korrekt, online geht er erst, wenn `run_agent.py` läuft.

## Schritt 5: Token und Keys auf dem Rechner setzen

Für die aktuelle PowerShell-Sitzung:

```powershell
$env:DISCORD_BOT_TOKEN = "DEIN-BOT-TOKEN"
$env:ELEVENLABS_API_KEY = "DEIN-ELEVENLABS-KEY"
```

Dauerhaft (überlebt Neustarts, gilt für neue Terminals):

```powershell
setx DISCORD_BOT_TOKEN "DEIN-BOT-TOKEN"
setx ELEVENLABS_API_KEY "DEIN-ELEVENLABS-KEY"
```

Optional, falls dein Sprachkanal nicht "DayZ" heißt:

```powershell
setx ISU_DISCORD_CHANNEL "Name-deines-Sprachkanals"
```

## Schritt 6: Starten und testen

```powershell
python daemon\run_agent.py
```

Im Runner-Journal muss "Discord-Voice gestartet" stehen. Details und Fehler landen in `agent_home\journal\discord_voice.log`, dort gehören diese Zeilen hin:

```
[discord-voice] Eingeloggt als Viktor#1234
[discord-voice] Im Sprachkanal: DeinServer / DayZ
[discord-voice] ElevenLabs-TTS aktiv.
```

Schnelltest ohne Gehirn: `python daemon\test_driver.py say --text "Funkprobe"`, die Zeile muss im Sprachkanal zu hören sein (sofern echte Voice-Lines generiert wurden, sonst Stille bei Katalog-Phrasen, Live-TTS über `say` geht aber immer).

## Fehlerbilder

| Symptom | Ursache / Fix |
|---|---|
| "Die Dienstressource wird begrenzt" beim Autorisieren | Discord-Rate-Limit auf dem OAuth2-Endpoint, gilt pro IP/Account (Browserwechsel hilft NICHT) → alle Authorize-Tabs schließen, 30 bis 60 Min gar nichts versuchen, dann EINMAL frisch öffnen (jeder Versuch verlängert die Sperre). Brave-Shields/Adblocker für discord.com ausschalten, VPN aus. Sofort-Workaround: URL am Smartphone über Mobilfunkdaten öffnen (WLAN am Handy aus = andere IP) und dort autorisieren, die Einladung ist geräteunabhängig |
| "Ungültige Bereiche für die Benutzerinstallation bereitgestellt" | In der URL steht `integration_type=1` (User Install) zusammen mit Scope `bot` → im URL Generator das Dropdown "Integration Type" auf "Guild Install" stellen (Schritt 3) oder in der URL `integration_type=0` setzen |
| `Improper token has been passed` im Log | Token falsch kopiert oder zurückgesetzt → in Schritt 2 neu generieren, Variable neu setzen |
| `Sprachkanal 'DayZ' auf keinem Server gefunden` | Bot wurde nicht eingeladen (Schritt 4) oder der Kanal heißt anders → `ISU_DISCORD_CHANNEL` setzen. Großschreibung egal, aber der Name muss stimmen |
| Bot ist im Kanal, aber stumm | Kein `ELEVENLABS_API_KEY` (dann kein Live-TTS) oder die Katalog-Oggs sind noch stille Platzhalter → `python voice\generate_voice.py --force` + IsuVoice neu packen |
| Bot verbindet und fliegt sofort raus | Berechtigungen im Kanal: Rolle des Bots braucht Verbinden + Sprechen auch in den Kanal-Overrides |
| `setx`-Variablen wirken nicht | `setx` gilt erst für NEUE Terminals, aktuelles Fenster schließen und neu öffnen |

## Sicherheit

Der Bot-Token gehört wie ein API-Key behandelt: nicht in Git, nicht in Screenshots, nicht in Discord-Nachrichten. Falls er doch einmal leakt: Developer Portal → Bot → "Reset Token", der alte ist damit sofort ungültig.

Quellen: [Discord Developer Portal](https://discord.com/developers/applications), [Discord OAuth2-Dokumentation](https://docs.discord.com/developers/topics/oauth2), [discord.js OAuth2-Guide](https://discordjs.guide/legacy/oauth2/oauth2)
