// IsuVoice - Kompakte Squad-Uebersicht am Bildschirmrand (Client).
//
// Ein festes Panel mit einer Zeile pro bekanntem Agenten: Farb-Chip in
// Identitaetsfarbe, Name, HP-Balken, Distanz zum lokalen Spieler, aktuelle
// Aktion und Gedankenzeile (zweizeilig, umbrechend). Datenquelle ist
// AUSSCHLIESSLICH der bestehende IsuNametagStore (gefuellt von
// RPC_NAMETAG/RPC_INTENT/RPC_AGENTPOS) - kein eigener Server-RPC, reine
// Zweitverwertung der Nameplate-Daten. Agenten ausserhalb der Netzwerk-
// Bubble (Objekt auf dem Client nicht repliziert) erscheinen mit der per
// RPC_AGENTPOS gefunkten Position und "(fern)" hinter dem Namen; ohne
// diesen RPC (alter Server) nur replizierte Agenten. Getrieben von
// MissionGameplay.OnUpdate (IsuArenaMenu.c), Dreifach-Schalter "HUD" im Arena-Menue
// (IsuArenaMenu.s_SquadHudMode: 0=OFF, 1=LEFT, 2=RIGHT - Default rechts
// oben, damit das Discord-Overlay links oben frei bleibt). Rein Client-seitig.

class IsuSquadHud
{
	static ref IsuSquadHud s_Inst;
	const int ROWS = 6;
	// Muss zur Zeilenbreite im Layout (isu_squad_hud.layout, SqRow/SqBg)
	// passen - wird fuer die Rechts-Ausrichtung (RIGHT) gebraucht.
	const float PANEL_W = 340.0;
	const float EDGE_MARGIN = 16.0;

	protected Widget m_Root;
	protected ref array<Widget> m_Rows;
	protected ref array<ImageWidget> m_Chips;
	protected ref array<TextWidget> m_Names;
	protected ref array<TextWidget> m_Dists;
	protected ref array<TextWidget> m_Acts;
	protected ref array<TextWidget> m_Intents;
	protected ref array<ImageWidget> m_HpFills;

	static void Tick()
	{
		if (!s_Inst)
			s_Inst = new IsuSquadHud();
		s_Inst.Update();
	}

	protected void Ensure()
	{
		if (m_Root)
			return;

		m_Root = GetGame().GetWorkspace().CreateWidgets("IsuVoice/GUI/isu_squad_hud.layout");
		if (!m_Root)
			return;

		m_Rows = new array<Widget>();
		m_Chips = new array<ImageWidget>();
		m_Names = new array<TextWidget>();
		m_Dists = new array<TextWidget>();
		m_Acts = new array<TextWidget>();
		m_Intents = new array<TextWidget>();
		m_HpFills = new array<ImageWidget>();

		for (int i = 0; i < ROWS; i++)
		{
			Widget row = m_Root.FindAnyWidget("SqRow" + i.ToString());
			m_Rows.Insert(row);
			m_Chips.Insert(ImageWidget.Cast(m_Root.FindAnyWidget("SqChip" + i.ToString())));
			m_Names.Insert(TextWidget.Cast(m_Root.FindAnyWidget("SqName" + i.ToString())));
			m_Dists.Insert(TextWidget.Cast(m_Root.FindAnyWidget("SqDist" + i.ToString())));
			m_Acts.Insert(TextWidget.Cast(m_Root.FindAnyWidget("SqAct" + i.ToString())));
			// SqIntent ist im Layout ein MultilineTextWidgetClass (wrap 1, zwei
			// Zeilen); MultilineTextWidget erbt von TextWidget, der Cast auf
			// TextWidget reicht (gleiches Muster wie Speech in IsuNameplateHud).
			m_Intents.Insert(TextWidget.Cast(m_Root.FindAnyWidget("SqIntent" + i.ToString())));
			m_HpFills.Insert(ImageWidget.Cast(m_Root.FindAnyWidget("SqHpFill" + i.ToString())));
			if (row)
				row.Show(false);

			// Dunkler 1px-Umriss (SDF-Font) EINMALIG beim Anlegen - gleiche
			// Lesbarkeits-Massnahme wie bei den Namensschildern; das halb-
			// transparente Zeilen-Bg allein reicht bei Tageslicht nicht.
			if (m_Names[i])
				m_Names[i].SetOutline(1, 0xC0000000);
			if (m_Dists[i])
				m_Dists[i].SetOutline(1, 0xC0000000);
			if (m_Acts[i])
				m_Acts[i].SetOutline(1, 0xC0000000);
			if (m_Intents[i])
				m_Intents[i].SetOutline(1, 0xC0000000);
		}
	}

	// Gleiche Zuordnung wie IsuNameplateHud.ActionText (actionId aus RPC_NAMETAG).
	protected string ActionText(int actionId)
	{
		if (actionId == 0) return IsuUiText.Choose("fighting", "kämpft");
		if (actionId == 1) return IsuUiText.Choose("looting", "lootet");
		if (actionId == 2) return IsuUiText.Choose("following", "folgt");
		if (actionId == 3) return IsuUiText.Choose("moving", "geht");
		return IsuUiText.Choose("waiting", "wartet");
	}

	protected int SlotColor(int slot)
	{
		if (slot >= 0 && slot <= 3)
			return ARGBF(1.0, IsuArenaMenu.s_ColR[slot], IsuArenaMenu.s_ColG[slot], IsuArenaMenu.s_ColB[slot]);
		return ARGBF(1.0, 0.80, 0.82, 0.85);
	}

	protected int HpColor(int hp)
	{
		if (hp >= 60) return ARGBF(1.0, 0.48, 0.78, 0.30);
		if (hp >= 30) return ARGBF(1.0, 0.94, 0.70, 0.25);
		return ARGBF(1.0, 0.88, 0.30, 0.28);
	}

	void Update()
	{
		// Schalter OFF -> nur verstecken, keine Widgets anlegen.
		if (IsuArenaMenu.s_SquadHudMode == 0)
		{
			if (m_Root)
				m_Root.Show(false);
			return;
		}

		Ensure();
		if (!m_Root)
			return;

		// Position jeden Update-Tick neu setzen (billig): LEFT = bisherige
		// linke Position, RIGHT = rechts oben buendig. So folgt das Panel
		// auch einem Aufloesungswechsel ohne eigenes Event. Das Root-Panel
		// ist ein normales screen-space FrameWidget (EXACTPOS im Layout),
		// SetPos funktioniert hier - anders als bei den projizierten
		// Nameplates.
		int scrW, scrH;
		GetScreenSize(scrW, scrH);
		float px = EDGE_MARGIN;
		float py = 180.0;
		if (IsuArenaMenu.s_SquadHudMode == 2)
		{
			px = scrW - PANEL_W - EDGE_MARGIN;
			py = EDGE_MARGIN;
		}
		m_Root.SetPos(px, py);

		// Erst alle Zeilen verstecken, dann sichtbare Agenten ihrem festen
		// Slot zuweisen (0..3 Identitaet, 4..5 Ueberlauf) - gleiche Slot-
		// Logik wie die Namensschilder, kein Zeilen-Springen zwischen Frames.
		for (int h = 0; h < ROWS; h++)
		{
			if (m_Rows[h])
				m_Rows[h].Show(false);
		}

		vector pp = vector.Zero;
		bool hasPlayer = false;
		Man player = GetGame().GetPlayer();
		if (player)
		{
			pp = player.GetPosition();
			hasPlayer = true;
		}

		int overflow = 4;
		int visible = 0;
		int nowMs = GetGame().GetTime();

		foreach (string key, IsuAgentTag t : IsuNametagStore.s_Agents)
		{
			// Abgelaufen (kein Server-RPC seit 10 s) oder noch ohne Namen
			// (Position/Intent kam vor dem ersten Nametag-RPC): still
			// ueberspringen. Das Abraeumen abgelaufener Keys uebernimmt
			// IsuNameplateHud (laeuft im selben Tick davor).
			if (IsuNametagStore.IsStale(t, nowMs))
				continue;
			if (t.name == "")
				continue;

			// Repliziert (in der Netzwerk-Bubble): Live-Position vom Objekt.
			// Sonst die letzte per RPC_AGENTPOS gefunkte Position, Zeile mit
			// Zusatz "(fern)". Ein alter Server ohne RPC_AGENTPOS liefert keine
			// Position -> wie bisher nur replizierte Agenten anzeigen.
			Object obj = GetGame().GetObjectByNetworkId(t.low, t.high);
			if (!obj && !t.hasPos)
				continue;
			bool isFar = true;
			vector apos = t.pos;
			if (obj)
			{
				isFar = false;
				apos = obj.GetPosition();
			}

			int row;
			if (t.slot >= 0 && t.slot <= 3)
			{
				row = t.slot;
			}
			else
			{
				row = overflow;
				if (overflow < ROWS - 1)
					overflow++;
			}
			if (row < 0 || row >= ROWS)
				continue;

			Widget rw = m_Rows[row];
			if (!rw)
				continue;
			rw.Show(true);
			visible++;

			ImageWidget chip = m_Chips[row];
			if (chip)
				chip.SetColor(SlotColor(t.slot));

			// "(fern)" steht hinter dem Namen statt hinter der Distanz: SqDist
			// ist im Layout nur 70 px breit ("1460 m" fuellt es), ein Zusatz
			// liefe in die Aktionsspalte. SqName hat 130 px.
			TextWidget nm = m_Names[row];
			if (nm)
			{
				string nameTxt = t.name;
				if (isFar)
					nameTxt = nameTxt + " " + IsuUiText.Choose("(far)", "(fern)");
				nm.SetText(nameTxt);
				nm.SetColor(SlotColor(t.slot));
			}

			TextWidget dt = m_Dists[row];
			if (dt)
			{
				if (hasPlayer)
				{
					// Ferne Agenten koennen ueber 10 km weg sein ("12345 m"
					// sprengt die 70-px-Spalte) -> ab 10 km ganze Kilometer.
					int dist = Math.Round(vector.Distance(pp, apos));
					string distTxt = dist.ToString() + " m";
					if (dist >= 10000)
					{
						int distKm = dist / 1000;
						distTxt = distKm.ToString() + " km";
					}
					dt.SetText(distTxt);
				}
				else
				{
					dt.SetText("-");
				}
			}

			TextWidget ac = m_Acts[row];
			if (ac)
				ac.SetText(ActionText(t.actionId) + "   " + t.hp.ToString() + "%");

			ImageWidget hf = m_HpFills[row];
			if (hf)
			{
				// Track ist im Layout 60 px breit (SqHpBg) - Fill skaliert darauf.
				float w = 60.0 * Math.Clamp(t.hp, 0, 100) / 100.0;
				hf.SetSize(w, 5);
				hf.SetColor(HpColor(t.hp));
			}

			TextWidget it = m_Intents[row];
			if (it)
				// Generated intentions keep their original language and content.
				it.SetText(t.intent);
		}

		// Ohne aktive Agenten verschwindet das Panel komplett (kein leerer Kasten).
		m_Root.Show(visible > 0);
	}
}
