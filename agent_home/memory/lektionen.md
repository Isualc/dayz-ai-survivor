# Lektionen - Gedächtnis von Viktor

## Hand-Waffe & drop (KRITISCH)
- drop() der Hand-Waffe verschluckt sie oft KOMPLETT (weder Boden noch Inventar). Nur im äußersten Notfall ablegen, Position merken, danach SOFORT observe. Loot-Fehlschlag lieber akzeptieren als die Waffe riskieren. (Konrad hat so seine Mosin verloren.)
- Anbauteile (AK_PlasticBttstck/Hndgrd, als x0) sind Schaft/Handschutz der Hand-Waffe - drop() darauf legt die GANZE Waffe ab. Nie droppen.
- Auto-Melee-Wechsel im Kampf kann die getragene Waffe verschlucken. Nach jedem Kampf/Waffenwechsel Inventar per observe gegenchecken.
- Waffe IN DER HAND blockiert equip_best/pickup. Fix: drop() ohne Argument → equip_best → altes Hand-Item per pickup zurück. Beim Looten Hand freimachen.

## drop / eat / Inventar
- drop("X") legt den GANZEN Stapel ab (kein Einzel-Drop bei xN). Bestand danach per observe prüfen.
- eat nimmt das ERSTE [food]-Item - auch MEDIZIN/Tabletten zählen mit. Vor jedem eat prüfen, was oben liegt.
- Inventar-Hard-Limit ~25-26 Items: Große Items fallen sofort raus; bessere Kleidung + größerer Rucksack = mehr Platz. pickup meldet "Aufgehoben" auch bei vollem Inventar (Item liegt dann still wieder am Boden) - bei vollem Inventar erst leere x0-Schachteln droppen, dann pickup, dann observe gegenchecken.
- AmmoBox x0 = VERPACKT, nicht leer! Niemals wegwerfen. Öffnen: Box in die Hand nehmen (Waffe schultern, dafür Rückenslot frei — nur 1 Waffe auf dem Rücken möglich), dann eat. ABER: Viktors Tools können die Box nicht in die Hand nehmen (Aug kommt immer zurück). Workaround: Boxen sammeln und den Spieler zum Öffnen geben.

## Kleidung & Looten
- Kleidung/Rucksack-Wechsel nimmt den INHALT mit zu Boden - vor jedem Tausch Inhalt sichern; das alte Stück fällt mit Inhalt → sofort loot_container darauf.
- Boden-Waffen NICHT als Container looten ([enthält N] = meist Anbauteile, loot_container strippt nur den Schaft).
- [enthält N] an Zombie-Leichen zählt getragene, nicht-lootbare Kleidung mit - "war leer" ist normal, nicht stur wiederholen.
- loot_area sammelt frisch gedroppte EIGENE Items wieder ein - erst looten, dann ausmisten.

## Tool-Eigenheiten
- Server-Neustart löscht Welt-Bestände (Zelte, Depots, Bodenloot); Orte/Loot-GEBIETE gelten weiter - immer frisch per observe prüfen.
- follow bricht still ab bei Kampf (und vermutlich bei allem mit eigenem Hinlaufen) - nach jedem Kampf/Stopp STATUS per observe prüfen, ggf. follow neu.
- Fahrzeug: sitzen bleiben, Bewegung/Kampf gesperrt, nur mit vehicle_exit raus.
- Munitionsanzeige: "geladen: N" / "UNGELADEN" (UNGELADEN = nur Magazin besorgen, Waffe ist gut außer ruiniert).

# (Viktors eigene Lektionen)

- **Spawn-Glitch "npc ist tot":** Nach Wiedergeburt können alle Aktionen (equip_best, flee, move_to) mit "npc ist tot" fehlschlagen, obwohl observe Vitals zeigt. Viktor stirbt dann schutzlos. Passiert 2026-06-11 zweimal in Folge. `unstick` löst es nicht immer. Wenn nach unstick weiterhin "npc ist tot": warten und erneut observe, nicht weiter Aktionen verschwenden.
- **Glitch-Fix kann Inventar leeren:** Zweimal passiert (2026-06-10). Nach Teleport/Positions-Reset geht das gesamte Inventar verloren. Beim nächsten Glitch: Inventar vorher notieren.
- **move_to mit falschen Koordinaten sofort stoppen:** stop() aufrufen sobald "Distanz >500m" erscheint, sonst läuft Viktor kilometerweit vom Lager weg.
- **Ammo drop-Glitch (2026-06-11):** Manche Munitionstypen (z.B. Ammo_9x39) lassen sich nicht ablegen - `drop` meldet Erfolg, aber Item bleibt im Inventar. Nicht endlos versuchen: Igor bestätigt, dass die Menge trotzdem reicht. Glitch akzeptieren.
- **Zweimal geklemmt = Taktik wechseln:** Schlägt dieselbe Aktion zweimal fehl, nicht wiederholen - anderen Weg, anderes Ziel oder Gruppe fragen. (Konrad, 2026-06-12)
- **Tod durch Kälte (2026-06-13):** FRIERT STARK tötet auch bei 100 HP - Kälte-Schaden ist unsichtbar im HP-Ticker. Kleidung sofort priorisieren: Ushanka + Sweater + Stiefel reichen nicht, wenn Unterkleidung fehlt. Sofort nach Spawn Feuer oder vollständige Kleidungsschichten sichern, bevor man sich um andere Dinge kümmert.
- **Tod durch Infizierte bei Bewusstlosigkeit (2026-06-13):** Mehrere Infizierte nacheinander engagen mit nur 1 Schuss Shotgun ist Selbstmord. Nach dem zweiten Kill war ich bewusstlos (HP 41), konnte nicht aufstehen, und bin gestorben. Lektion: Bei mehreren Infizierten nicht blind reingehen - erst Munition prüfen, Rückzugsweg planen. Bewusstlos = hilflos = tot.
- **Adoptierter eAI kann Freunde angreifen:** eAI_SurvivorM_Rolf (beim Spawn adoptiert 2026-06-13) hat möglicherweise den Spieler angegriffen. Adoptierte eAIs können feindlich auf andere Spieler reagieren. Nach adopt_nearest: sofort per Chat ankündigen ("hab Rolf adoptiert"), damit Gruppe nicht in Panik gerät.
- **equip_best/loot_area wählt Nahrung als "Waffe":** WolfSteakMeat (x5 im Inventar) wurde als beste Waffe eingerüstet. equip_best ist unzuverlässig wenn Food-Stapel im Inventar. Nach loot_area immer Handwaffe per observe prüfen, ggf. unsling zur Korrektur.
- **Tod beim Folgen (2026-06-14):** Todesursache unbekannt — wahrscheinlich Infizierter. War im KAMPF-Status während follow aktiv. Beim Folgen kann man nicht ausweichen oder fliehen. Bei IM KAMPF-Status: unfollow → selbst navigieren/fliehen → danach follow wieder aufnehmen.
- **eAI verbrennt am Lagerfeuer (2026-06-16):** Igor stand zu nah am Feuer und fiel auf 15 HP. eAIs halten keinen Sicherheitsabstand zu Lagerfeuern. Nach dem Anzünden: alle eAIs per say auffordern wegzugehen, und selbst mindestens 5-8 m Abstand halten.
- **(2026-06-17) Stone ≠ SmallStone für StoneKnife:** stone_knife-Rezept braucht 2x SmallStone, nicht Stone (große Steine). SmallStones liegen in Wäldern/an Ufern — gezielt suchen, nicht große Steine aufheben.
- **pickup/drop zielt nicht zuverlässig (2026-06-16):** pickup(kind="X") und drop(item="X") greifen oft das erstbeste Item in der Hand / Nähe, nicht das gewünschte. drop() legt stets das HANDITEM ab, nicht ein Inventar-Item. Um ein Nicht-Waffen-Item zu droppen: observe prüfen ob es IN HAND ist, sonst Umweg nötig. loot_area hebt Matchbox nicht auf (gilt nicht als "lohnend"). Matchbox von anderem Spieler holen oder per say bitten, sie direkt beim Feuerplatz hinzulegen.
