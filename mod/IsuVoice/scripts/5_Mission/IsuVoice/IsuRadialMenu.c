// IsuVoice - Six direct commands around a shared target, matching the arena.
// Only UI text is translated. Action IDs and the targeted command payload stay
// unchanged. All layout coordinates are captured once; fitting uses screen
// pixels explicitly so DayZ UI scaling is never applied a second time.

class IsuRadialMetric
{
	Widget widget;
	float x;
	float y;
	float width;
	float height;
	int textSize;
}

class IsuRadialMenu extends UIScriptedMenu
{
	// Ziel-Snapshot, vom Oeffner (MissionGameplay) vor ShowScriptedMenu gesetzt.
	static bool s_HasTarget = false;
	static int s_TargetLow = 0;
	static int s_TargetHigh = 0;
	static string s_TargetName = "next NPC";

	// Anvisierter Bodenpunkt beim Oeffnen (fuer den "Go to"-Chip): der NPC laeuft
	// genau dorthin, wohin der Spieler zeigt (z.B. auf gefundenes Loot).
	static bool s_HasAimPos = false;
	static float s_AimX = 0.0;
	static float s_AimZ = 0.0;

	// Anvisiertes loses Item beim Oeffnen. Ist eins da, wird der "Go to"-Chip zum
	// "Hol das": der NPC laeuft hin, hebt es auf und zieht es an / schultert es.
	static bool s_HasAimItem = false;
	static string s_AimItemClass = "";

	// Button-Reihenfolge muss zum Layout (BtnAct0..BtnAct5) passen.
	static ref TStringArray s_Actions = {"follow", "halt", "comehere", "loot", "engage", "gotoaim"};
	static const float BASE_W = 820.0;
	static const float BASE_H = 620.0;

	protected ButtonWidget m_Act0;
	protected ButtonWidget m_Act1;
	protected ButtonWidget m_Act2;
	protected ButtonWidget m_Act3;
	protected ButtonWidget m_Act4;
	protected ButtonWidget m_Act5;
	protected TextWidget m_Center;
	protected TextWidget m_CenterNote;
	protected ButtonWidget m_Close;
	protected Widget m_Selector;
	protected ref array<ref IsuRadialMetric> m_Metrics;
	protected ref array<ImageWidget> m_ActionBgs;
	protected ref array<TextWidget> m_ActionLabels;
	protected ref array<TextWidget> m_ActionNotes;
	protected int m_LastScreenW = -1;
	protected int m_LastScreenH = -1;
	protected int m_LastLanguageRevision = -1;
	protected int m_Hovered = -1;
	protected float m_UiScale = 1.0;

	override Widget Init()
	{
		if (layoutRoot)
			return layoutRoot;
		layoutRoot = GetGame().GetWorkspace().CreateWidgets("IsuVoice/GUI/isu_radial_menu.layout");
		if (!layoutRoot)
			return null;

		m_Act0 = ButtonWidget.Cast(layoutRoot.FindAnyWidget("BtnAct0"));
		m_Act1 = ButtonWidget.Cast(layoutRoot.FindAnyWidget("BtnAct1"));
		m_Act2 = ButtonWidget.Cast(layoutRoot.FindAnyWidget("BtnAct2"));
		m_Act3 = ButtonWidget.Cast(layoutRoot.FindAnyWidget("BtnAct3"));
		m_Act4 = ButtonWidget.Cast(layoutRoot.FindAnyWidget("BtnAct4"));
		m_Act5 = ButtonWidget.Cast(layoutRoot.FindAnyWidget("BtnAct5"));
		m_Center = TextWidget.Cast(layoutRoot.FindAnyWidget("RadialCenter"));
		m_CenterNote = TextWidget.Cast(layoutRoot.FindAnyWidget("RadialTargetDetail"));
		m_Close = ButtonWidget.Cast(layoutRoot.FindAnyWidget("BtnRadialClose"));
		m_Selector = layoutRoot.FindAnyWidget("Selector");
		m_ActionBgs = new array<ImageWidget>();
		m_ActionLabels = new array<TextWidget>();
		m_ActionNotes = new array<TextWidget>();
		for (int idx = 0; idx < 6; idx++)
		{
			m_ActionBgs.Insert(ImageWidget.Cast(layoutRoot.FindAnyWidget("ActBg" + idx.ToString())));
			m_ActionLabels.Insert(TextWidget.Cast(layoutRoot.FindAnyWidget("ActLabel" + idx.ToString())));
			m_ActionNotes.Insert(TextWidget.Cast(layoutRoot.FindAnyWidget("ActNote" + idx.ToString())));
		}
		m_Metrics = new array<ref IsuRadialMetric>();
		float createdRootW, createdRootH;
		layoutRoot.GetSize(createdRootW, createdRootH);
		float designFactor = 1.0;
		if (createdRootW > 0)
			designFactor = BASE_W / createdRootW;
		CaptureMetrics(layoutRoot, 0, 0, designFactor);
		RefreshLabels();
		FitToScreen();

		return layoutRoot;
	}

	// Every widget in this layout uses exact, left/top-relative coordinates.
	// CreateWidgets can already multiply GetPos/GetSize by the engine UI scale.
	// Remove that measured factor from every local rectangle before adding its
	// already-normalized parent offset. Font userIDs remain authored point sizes.
	protected void CaptureMetrics(Widget w, float parentX, float parentY, float designFactor)
	{
		if (!w)
			return;
		IsuRadialMetric metric = new IsuRadialMetric();
		metric.widget = w;
		w.GetPos(metric.x, metric.y);
		metric.x = metric.x * designFactor + parentX;
		metric.y = metric.y * designFactor + parentY;
		if (w == layoutRoot)
		{
			metric.x = 0;
			metric.y = 0;
		}
		w.GetSize(metric.width, metric.height);
		metric.width = metric.width * designFactor;
		metric.height = metric.height * designFactor;
		metric.textSize = w.GetUserID();
		m_Metrics.Insert(metric);
		Widget child = w.GetChildren();
		while (child)
		{
			CaptureMetrics(child, metric.x, metric.y, designFactor);
			child = child.GetSibling();
		}
	}

	protected void FitToScreen()
	{
		if (!layoutRoot || !m_Metrics)
			return;
		int screenW, screenH;
		GetScreenSize(screenW, screenH);
		if (screenW <= 32 || screenH <= 32)
			return;
		if (screenW == m_LastScreenW && screenH == m_LastScreenH)
			return;
		m_LastScreenW = screenW;
		m_LastScreenH = screenH;
		m_UiScale = Math.Min(1.0, Math.Min((screenW - 32) / BASE_W, (screenH - 32) / BASE_H));
		float originX = (screenW - BASE_W * m_UiScale) * 0.5;
		float originY = (screenH - BASE_H * m_UiScale) * 0.5;
		for (int idx = 0; idx < m_Metrics.Count(); idx++)
		{
			IsuRadialMetric metric = m_Metrics[idx];
			metric.widget.SetScreenSize(metric.width * m_UiScale, metric.height * m_UiScale);
			metric.widget.SetScreenPos(originX + metric.x * m_UiScale, originY + metric.y * m_UiScale);
			TextWidget text = TextWidget.Cast(metric.widget);
			if (text && metric.textSize > 0)
				text.SetTextExactSize(Math.Round(metric.textSize * m_UiScale));
		}
		if (m_Close)
			m_Close.SetTextProportion(0.44);
		HighlightSlice(m_Hovered);
	}

	protected void SetLabel(string name, string english, string german)
	{
		TextWidget label = TextWidget.Cast(layoutRoot.FindAnyWidget(name));
		if (label)
			label.SetText(IsuUiText.Choose(english, german));
	}

	protected string ActionLabel(int idx)
	{
		if (idx == 0) return IsuUiText.Choose("Follow me", "Mir folgen");
		if (idx == 1) return IsuUiText.Choose("Hold position", "Position halten");
		if (idx == 2) return IsuUiText.Choose("Regroup", "Zu mir kommen");
		if (idx == 3) return IsuUiText.Choose("Find supplies", "Vorräte suchen");
		if (idx == 4) return IsuUiText.Choose("Engage", "Angreifen");
		if (s_HasAimItem) return IsuUiText.Choose("Pick up item", "Aufheben");
		return IsuUiText.Choose("Go there", "Dorthin gehen");
	}

	protected string ActionNote(int idx)
	{
		if (idx == 0) return IsuUiText.Choose("Stay close to you", "In deiner Nähe bleiben");
		if (idx == 1) return IsuUiText.Choose("Stop and wait", "Anhalten und warten");
		if (idx == 2) return IsuUiText.Choose("Move to your location", "Zu deiner Position laufen");
		if (idx == 3) return IsuUiText.Choose("Search nearby loot", "Umgebung durchsuchen");
		if (idx == 4) return IsuUiText.Choose("Attack nearby threats", "Gegner in der Nähe bekämpfen");
		if (s_HasAimItem) return IsuUiText.Choose("The item you aimed at", "Anvisierten Gegenstand holen");
		return IsuUiText.Choose("The point you aimed at", "Zum anvisierten Punkt");
	}

	protected string ShortLabel(string value, int limit)
	{
		if (value.LengthUtf8() > limit)
			return value.SubstringUtf8(0, limit - 3) + "...";
		return value;
	}

	protected void RefreshLabels()
	{
		if (!layoutRoot)
			return;
		m_LastLanguageRevision = IsuUiText.s_Revision;
		SetLabel("RadialTitle", "SURVIVOR / COMMANDS", "SURVIVOR / BEFEHLE");
		SetLabel("RadialSubtitle", "Direct your group. Stay alive together.", "Führe deine Gruppe. Überlebt gemeinsam.");
		SetLabel("RadialTargetLabel", "COMMAND TARGET", "BEFEHLSZIEL");
		SetLabel("RadialHint", "Click a command   /   Esc to close", "Befehl anklicken   /   Esc zum Schließen");
		for (int idx = 0; idx < 6; idx++)
		{
			if (m_ActionLabels[idx])
				m_ActionLabels[idx].SetText(ActionLabel(idx));
			if (m_ActionNotes[idx])
				m_ActionNotes[idx].SetText(ActionNote(idx));
		}
		if (m_Center)
		{
			if (s_HasTarget)
				m_Center.SetText(ShortLabel(s_TargetName, 24));
			else
				m_Center.SetText(IsuUiText.Choose("Nearest survivor", "Nächster Überlebender"));
		}
		if (m_CenterNote)
		{
			if (s_HasAimItem)
				m_CenterNote.SetText(IsuUiText.Choose("Item: ", "Objekt: ") + ShortLabel(s_AimItemClass, 28));
			else
				m_CenterNote.SetText(IsuUiText.Choose("One command. One clear objective.", "Ein Befehl. Ein klares Ziel."));
		}
	}

	override void Update(float timeslice)
	{
		super.Update(timeslice);
		FitToScreen();
		if (m_LastLanguageRevision != IsuUiText.s_Revision)
			RefreshLabels();
	}

	override bool UseMouse()
	{
		return true;
	}

	override bool UseKeyboard()
	{
		return true;
	}

	override void OnShow()
	{
		super.OnShow();
		RefreshLabels();
		FitToScreen();
		HighlightSlice(-1);
		SetFocus(layoutRoot);
		GetGame().GetInput().ChangeGameFocus(1);
		GetGame().GetUIManager().ShowUICursor(true);
		GetGame().GetMission().PlayerControlDisable(INPUT_EXCLUDE_ALL);
	}

	override void OnHide()
	{
		GetGame().GetInput().ChangeGameFocus(-1);
		GetGame().GetUIManager().ShowUICursor(false);
		GetGame().GetMission().PlayerControlEnable(true);
		super.OnHide();
	}

	protected ButtonWidget ActButton(int idx)
	{
		if (idx == 0) return m_Act0;
		if (idx == 1) return m_Act1;
		if (idx == 2) return m_Act2;
		if (idx == 3) return m_Act3;
		if (idx == 4) return m_Act4;
		return m_Act5;
	}

	// One quiet accent and a brighter surface identify the hovered command.
	protected void HighlightSlice(int idx)
	{
		m_Hovered = idx;
		if (!m_ActionBgs)
			return;
		for (int i = 0; i < 6; i++)
		{
			int bg = ARGB(255, 32, 42, 37);
			int label = ARGB(255, 235, 239, 228);
			int note = ARGB(255, 156, 174, 160);
			if (i == 4)
			{
				bg = ARGB(255, 48, 32, 29);
				label = ARGB(255, 236, 177, 158);
			}
			if (i == idx)
			{
				bg = ARGB(255, 56, 73, 46);
				label = ARGB(255, 241, 247, 231);
				note = ARGB(255, 199, 218, 183);
			}
			if (m_ActionBgs[i]) m_ActionBgs[i].SetColor(bg);
			if (m_ActionLabels[i]) m_ActionLabels[i].SetColor(label);
			if (m_ActionNotes[i]) m_ActionNotes[i].SetColor(note);
		}
		if (m_Selector)
		{
			m_Selector.Show(idx >= 0 && idx < 6);
			if (idx >= 0 && idx < 6)
			{
				float x, y, width, height;
				ActButton(idx).GetScreenPos(x, y);
				ActButton(idx).GetScreenSize(width, height);
				m_Selector.SetScreenPos(x, y);
				m_Selector.SetScreenSize(3 * m_UiScale, height);
			}
		}
	}

	override bool OnMouseEnter(Widget w, int x, int y)
	{
		for (int idx = 0; idx < 6; idx++)
		{
			if (w == ActButton(idx))
			{
				HighlightSlice(idx);
				return true;
			}
		}
		return super.OnMouseEnter(w, x, y);
	}

	override bool OnMouseLeave(Widget w, Widget enterW, int x, int y)
	{
		for (int idx = 0; idx < 6; idx++)
		{
			if (w == ActButton(idx))
			{
				HighlightSlice(-1);
				break;
			}
		}
		return super.OnMouseLeave(w, enterW, x, y);
	}

	protected void DoAction(int idx)
	{
		string action = s_Actions[idx];
		string extra = "";

		PlayerBase pb = PlayerBase.Cast(GetGame().GetPlayer());

		if (action == "follow")
		{
			// Spielername als Folgeziel; '|' rausfiltern (Trennzeichen im Protokoll)
			string n = "";
			if (pb && pb.GetIdentity())
				n = pb.GetIdentity().GetName();
			n.Replace("|", "");
			// Immer ein Feld senden (Stern = kein Namensfilter -> naechster
			// Spieler), damit die follow-Zeile nie zu kurz wird.
			if (n == "")
				n = "*";
			extra = n;
		}
		else if (action == "comehere")
		{
			// "Komm zu mir" = goto auf die Spielerposition (nutzt den goto-Pfad)
			action = "goto";
			if (pb)
			{
				vector pp = pb.GetPosition();
				extra = pp[0].ToString() + "|" + pp[2].ToString();
			}
		}
		else if (action == "gotoaim")
		{
			if (s_HasAimItem)
			{
				// Auf ein Item gezeigt -> "Hol das": hingehen, aufheben, anziehen.
				action = "fetch";
				extra = s_AimItemClass;
			}
			else
			{
				// Auf den Boden gezeigt -> "Go to": zum anvisierten Punkt laufen.
				action = "goto";
				if (s_HasAimPos)
					extra = s_AimX.ToString() + "|" + s_AimZ.ToString();
				else if (pb)
				{
					vector pp2 = pb.GetPosition();
					extra = pp2[0].ToString() + "|" + pp2[2].ToString();
				}
			}
		}

		IsuNpcCommand.SendTargeted(action, s_HasTarget, s_TargetLow, s_TargetHigh, extra);
		GetGame().GetUIManager().HideScriptedMenu(this);
	}

	override bool OnClick(Widget w, int x, int y, int button)
	{
		if (w == m_Close)
		{
			GetGame().GetUIManager().HideScriptedMenu(this);
			return true;
		}
		for (int idx = 0; idx < 6; idx++)
		{
			if (w == ActButton(idx))
			{
				DoAction(idx);
				return true;
			}
		}
		return super.OnClick(w, x, y, button);
	}
}
