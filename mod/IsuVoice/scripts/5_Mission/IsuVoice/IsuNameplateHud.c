// IsuVoice - Schwebende Namensschilder ueber den Agenten-Koepfen (Client).
//
// Ein Pool aus 6 Schild-Rahmen am Workspace-Root, jeden Frame neu positioniert:
// pro bekanntem Agenten (IsuNametagStore) wird das Objekt per NetworkID
// aufgeloest, der Kopf per GetScreenPos projiziert und das Schild dorthin
// gesetzt (Name in Identitaetsfarbe, HP-Balken, Aktion, Gedanken-Zeile).
// Getrieben von MissionGameplay.OnUpdate (IsuArenaMenu.c), rein Client-seitig.

class IsuNameplateHud
{
	static ref IsuNameplateHud s_Inst;
	const int POOL = 6;
	const float MAX_DIST = 120.0;

	protected Widget m_Root;
	protected ref array<Widget> m_Tags;
	protected ref array<TextWidget> m_Names;
	protected ref array<TextWidget> m_Acts;
	protected ref array<TextWidget> m_Intents;
	protected ref array<ImageWidget> m_HpFills;
	protected ref array<ImageWidget> m_SpeechBgs;
	protected ref array<TextWidget> m_Speeches;

	static void Tick()
	{
		if (!s_Inst)
			s_Inst = new IsuNameplateHud();
		s_Inst.Update();
	}

	protected void Ensure()
	{
		if (m_Root)
			return;

		m_Root = GetGame().GetWorkspace().CreateWidgets("IsuVoice/GUI/isu_nameplate.layout");
		if (!m_Root)
			return;

		m_Tags = new array<Widget>();
		m_Names = new array<TextWidget>();
		m_Acts = new array<TextWidget>();
		m_Intents = new array<TextWidget>();
		m_HpFills = new array<ImageWidget>();
		m_SpeechBgs = new array<ImageWidget>();
		m_Speeches = new array<TextWidget>();

		for (int i = 0; i < POOL; i++)
		{
			Widget tag = m_Root.FindAnyWidget("Tag" + i.ToString());
			m_Tags.Insert(tag);
			m_Names.Insert(TextWidget.Cast(m_Root.FindAnyWidget("Name" + i.ToString())));
			m_Acts.Insert(TextWidget.Cast(m_Root.FindAnyWidget("Act" + i.ToString())));
			m_Intents.Insert(TextWidget.Cast(m_Root.FindAnyWidget("Intent" + i.ToString())));
			m_HpFills.Insert(ImageWidget.Cast(m_Root.FindAnyWidget("HpFill" + i.ToString())));
			m_SpeechBgs.Insert(ImageWidget.Cast(m_Root.FindAnyWidget("SpeechBg" + i.ToString())));
			m_Speeches.Insert(TextWidget.Cast(m_Root.FindAnyWidget("Speech" + i.ToString())));
			if (tag)
				tag.Show(false);
		}
	}

	protected string ActionText(int actionId)
	{
		if (actionId == 0) return "kämpft";
		if (actionId == 1) return "lootet";
		if (actionId == 2) return "folgt";
		if (actionId == 3) return "geht";
		return "wartet";
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
		Ensure();
		if (!m_Root)
			return;

		// Erst alle Schilder verstecken, dann sichtbare Agenten ihrem festen
		// Slot-Tag zuweisen. Fester Slot = kein Flackern, falls die Map-
		// Reihenfolge zwischen Frames wechselt.
		for (int hidx = 0; hidx < POOL; hidx++)
		{
			if (m_Tags[hidx])
				m_Tags[hidx].Show(false);
			if (m_SpeechBgs[hidx])
				m_SpeechBgs[hidx].Show(false);
			if (m_Speeches[hidx])
				m_Speeches[hidx].Show(false);
		}

		vector cam = GetGame().GetCurrentCameraPosition();
		ref array<string> stale = new array<string>();
		int overflow = 4;

		foreach (string key, IsuAgentTag t : IsuNametagStore.s_Agents)
		{
			Object obj = GetGame().GetObjectByNetworkId(t.low, t.high);
			if (!obj)
			{
				// Despawnt oder ausserhalb der Netzwerk-Bubble: Key vormerken
				// und nach der Iteration entfernen (kein Leak, kein Iterator-
				// Bruch). Lebende Agenten kommen per Nametag-RPC wieder rein.
				stale.Insert(key);
				continue;
			}

			vector head = obj.GetPosition();
			head[1] = head[1] + 2.0;

			if (vector.Distance(cam, head) > MAX_DIST)
				continue;

			vector sp = GetGame().GetScreenPos(head);
			if (sp[2] <= 0)
				continue;

			// Fester Pool-Slot pro Identitaet (0..3); unbekannte Slots 4..5.
			int poolIdx;
			if (t.slot >= 0 && t.slot <= 3)
			{
				poolIdx = t.slot;
			}
			else
			{
				poolIdx = overflow;
				if (overflow < POOL - 1)
					overflow++;
			}
			if (poolIdx < 0 || poolIdx >= POOL)
				continue;

			Widget tag = m_Tags[poolIdx];
			if (tag)
			{
				tag.SetScreenPos(sp[0] - 100, sp[1] - 80);
				tag.Show(true);
			}

			TextWidget nm = m_Names[poolIdx];
			if (nm)
			{
				nm.SetText(t.name);
				nm.SetColor(SlotColor(t.slot));
			}

			TextWidget ac = m_Acts[poolIdx];
			if (ac)
				ac.SetText(ActionText(t.actionId) + "   " + t.hp.ToString() + "%");

			ImageWidget hf = m_HpFills[poolIdx];
			if (hf)
			{
				float w = 160.0 * Math.Clamp(t.hp, 0, 100) / 100.0;
				hf.SetSize(w, 6);
				hf.SetColor(HpColor(t.hp));
			}

			TextWidget it = m_Intents[poolIdx];
			if (it)
				it.SetText(t.intent);

			// Comic-Sprechblase: zeigt was der NPC gesagt hat, ~6 s lang, nur wenn
			// im Menue eingeschaltet (s_ComicChat). Feste Box aus dem Layout, der
			// Text bricht per "wrap 1" um. Position/Groesse kommen aus dem Layout -
			// KEIN SetPos/SetSize (das machte die Box unsichtbar / in die Ecke).
			ImageWidget sbg = m_SpeechBgs[poolIdx];
			TextWidget spt = m_Speeches[poolIdx];
			bool showSpeech = IsuArenaMenu.s_ComicChat && t.speech != "" && GetGame().GetTime() < t.speechExpiry;
			if (showSpeech && spt && sbg)
			{
				spt.SetText(t.speech);
				sbg.Show(true);
				spt.Show(true);
			}
			else
			{
				if (sbg)
					sbg.Show(false);
				if (spt)
					spt.Show(false);
			}
		}

		for (int s = 0; s < stale.Count(); s++)
			IsuNametagStore.s_Agents.Remove(stale[s]);
	}
}
