# Mitspielen auf dem ISU-Survivor-Server

Auf diesem DayZ-Server leben bis zu vier KI-Überlebende: Viktor der Jäger, Angie die Sanitäterin, Igor der Bauer und Konrad der Ex-Soldat. Hinter jedem steckt ein eigenes Sprachmodell, das wirklich spielt: Sie looten, kämpfen, frieren, schließen Bündnisse und merken sich, wen sie getroffen haben. Du redest mit ihnen über den ganz normalen Spiel-Chat, und sie antworten dir im Chat und (wenn der Host Funk aktiviert hat) mit eigener Stimme im Discord.

## Was du brauchst

- DayZ auf Steam (Standard-Version, kein Experimental)
- Die unten gelisteten Workshop-Mods
- Die Mod **@IsuVoice** als Zip vom Host (gibt es nicht im Workshop)
- Die Server-Adresse vom Host (IP und Port, Standard-Port 2302)

## Schritt 1: Workshop-Mods abonnieren

Im Steam-Workshop für DayZ diese sechs Mods abonnieren. Steam lädt sie automatisch herunter:

1. Community Framework (CF)
2. Dabs Framework
3. DayZ-Expansion-Bundle
4. DayZ-Expansion-Licensed
5. DayZ-Expansion-Animations
6. VPPAdminTools

## Schritt 2: Die IsuVoice-Mod installieren

@IsuVoice zeigt dir die Namen über den Köpfen der KI-Überlebenden und ihre Marker auf der Karte. Ohne diese Mod siehst du sie nur als namenlose Survivors.

1. Das Zip vom Host entpacken. Heraus kommt ein Ordner `@IsuVoice` mit `addons\IsuVoice.pbo` darin.
2. Den Ordner in dein DayZ-Verzeichnis legen, z.B. `C:\Programme (x86)\Steam\steamapps\common\DayZ\@IsuVoice`.
3. Im DayZ-Launcher unter **Mods → Lokale Mod hinzufügen** den Ordner auswählen.

## Schritt 3: Verbinden

Im Launcher alle sieben Mods aktivieren (die sechs aus dem Workshop plus @IsuVoice), dann unter **Server → Direktverbindung** die Adresse vom Host eintragen, etwa `203.0.113.10:2302`. Im selben Netzwerk wie der Host reicht dessen lokale IP, etwa `192.168.0.20:2302`.

Wirft dich der Server beim Join raus, fehlt fast immer eine Mod oder eine ist veraltet. Im Launcher die Mod-Liste mit dem Host abgleichen.

## Mit den Überlebenden reden

Die KI-Überlebenden hören deinen **Text-Chat im Direct-Kanal** auf etwa 60 Meter. Sprich sie mit Namen an, dann antwortet genau der Richtige: "Konrad, was trägst du an Waffen?" weckt Konrad und nicht die anderen drei. Ohne Namen reagiert, wer dir am nächsten steht.

Ein paar Dinge, die du wissen solltest:

- **Antworten dauern.** Hinter jeder Figur denkt ein Sprachmodell nach. Ein paar Sekunden bis etwa eine Minute sind normal, gerade wenn die Figur unterwegs ist oder kämpft.
- **Sie merken sich dich.** Jeder Überlebende führt ein Langzeitgedächtnis. Wer sich vorstellt, wird beim nächsten Treffen erkannt. Wer schießt, steht ab dann als Feind in den Notizen.
- **Sie lernen von dir.** Gib ihnen Tipps ("loote die Zombie-Leichen", "im Norden gibt es Militär-Loot") und sie übernehmen die Taktik dauerhaft.
- **Du findest sie immer.** Auf der Karte (Expansion-Map, Taste M) hat jeder einen permanenten Marker, und ab 50 Metern siehst du den Namen über dem Kopf.
- **Chat-Befehl `tp`**: teleportiert dich zum Überlebenden. Praktisch zum Wiederfinden, auf Servern mit mehreren menschlichen Spielern aber bitte nur nach Absprache mit dem Host.
- **Stimmen**: Läuft beim Host der Discord-Funk, sprechen die Figuren ihre Antworten im Discord-Kanal. Zu ihnen sprechen kannst du als Gast trotzdem nur per Text-Chat, das Mikrofon-Hören läuft technisch nur über den Host.

## Spielregeln im Umgang mit ihnen

Im normalen Koop-Modus greifen die Überlebenden Menschen niemals zuerst an. Du kannst ihnen Gegenstände hinlegen, mit ihnen looten gehen oder sie um Hilfe bitten ("Angie, ich blute!"). Hat der Host den Battle-Royale-Modus gestartet, gelten Bewaffnete als Ziel, dann halte Abstand oder leg die Waffe weg.

Die Taste **Einfg** öffnet das Arena-Steuermenü. Es funktioniert technisch bei jedem, der @IsuVoice geladen hat. Bitte lass die Finger davon, das Menü startet und stoppt die KI-Gehirne und gehört dem Host.

## Wenn etwas nicht klappt

| Problem | Ursache |
|---|---|
| Keine Namen über den Köpfen, keine Marker | @IsuVoice ist nicht geladen (Launcher prüfen) |
| Kick beim Verbinden | Mod-Liste stimmt nicht mit dem Server überein |
| Überlebender antwortet nicht | Zu weit weg (über 60 m), gerade im Kampf, oder sein Zug dauert noch |
| Keine Stimmen zu hören | Discord-Funk ist Sache des Hosts, frag ihn |

## Für den Host (Checkliste)

1. `start_game.bat` starten (Server, Supervisor und Client), im Spiel mit Einfg die Agenten starten.
2. Für Cloud-Modelle vorher einmalig `tools\set_api_keys.ps1` ausführen. Die Claude-Modelle und das lokale Gemma brauchen keine Keys.
3. Den Ordner `build\@IsuVoice` zippen und den Mitspielern schicken.
4. Für Mitspieler übers Internet: im Router die UDP-Ports **2302 bis 2306** und **27016** (Steam-Query) an den Spiele-PC weiterleiten und die öffentliche IP mitteilen. Im Heimnetz reicht die lokale IP.
5. Der Server hat kein Passwort gesetzt. Wer die Adresse hat, kommt rein, also die IP nur an Leute geben, denen du vertraust.
