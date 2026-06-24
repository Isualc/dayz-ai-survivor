// IsuVoice - Rundes Befehls-Rad fuer die NPCs.
//
// Eigenes UIScriptedMenu im Look des Vanilla-/Expansion-Rads: ein radial8-Ring
// als Backdrop, fuenf Befehls-Chips kreisfoermig darauf, und ein heller
// Selector-Slice (radial_selector), der per SetRotation dem anvisierten Chip
// folgt (OnMouseEnter). Klick waehlt. Geoeffnet wird per konfigurierbarer Taste
// in MissionGameplay (IsuArenaMenu.c); das Ziel wird beim Oeffnen per
// AimRaycast in die Statics gelegt. Der Klick baut denselben Befehlsstring wie
// die Direkttasten und schickt ihn ueber IsuNpcCommand an den Server.

class IsuRadialMenu extends UIScriptedMenu
{
	// Ziel-Snapshot, vom Oeffner (MissionGameplay) vor ShowScriptedMenu gesetzt.
	static bool s_HasTarget = false;
	static int s_TargetLow = 0;
	static int s_TargetHigh = 0;
	static string s_TargetName = "next NPC";

	// Button-Reihenfolge muss zum Layout (BtnAct0..BtnAct4) passen.
	static ref TStringArray s_Actions = {"follow", "halt", "comehere", "loot", "engage"};
	// Winkel (Grad, im Uhrzeigersinn ab oben) je Chip - deckt sich mit den
	// Chip-Positionen im Layout. Der Selector-Slice sitzt oben und wird hierhin
	// gedreht.
	static ref array<float> s_Angles = {0, 72, 144, 216, 288};

	protected ButtonWidget m_Act0;
	protected ButtonWidget m_Act1;
	protected ButtonWidget m_Act2;
	protected ButtonWidget m_Act3;
	protected ButtonWidget m_Act4;
	protected TextWidget m_Center;
	protected Widget m_Selector;

	override Widget Init()
	{
		layoutRoot = GetGame().GetWorkspace().CreateWidgets("IsuVoice/GUI/isu_radial_menu.layout");

		m_Act0 = ButtonWidget.Cast(layoutRoot.FindAnyWidget("BtnAct0"));
		m_Act1 = ButtonWidget.Cast(layoutRoot.FindAnyWidget("BtnAct1"));
		m_Act2 = ButtonWidget.Cast(layoutRoot.FindAnyWidget("BtnAct2"));
		m_Act3 = ButtonWidget.Cast(layoutRoot.FindAnyWidget("BtnAct3"));
		m_Act4 = ButtonWidget.Cast(layoutRoot.FindAnyWidget("BtnAct4"));
		m_Center = TextWidget.Cast(layoutRoot.FindAnyWidget("RadialCenter"));
		m_Selector = layoutRoot.FindAnyWidget("Selector");

		if (m_Center)
			m_Center.SetText("Target: " + s_TargetName);

		return layoutRoot;
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
		return m_Act4;
	}

	// Selector-Slice auf den Winkel des anvisierten Chips drehen und einblenden.
	protected void HighlightSlice(int idx)
	{
		if (!m_Selector)
			return;
		m_Selector.SetRotation(0, 0, s_Angles[idx]);
		m_Selector.Show(true);
	}

	override bool OnMouseEnter(Widget w, int x, int y)
	{
		for (int idx = 0; idx < 5; idx++)
		{
			if (w == ActButton(idx))
			{
				HighlightSlice(idx);
				return true;
			}
		}
		return super.OnMouseEnter(w, x, y);
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

		IsuNpcCommand.SendTargeted(action, s_HasTarget, s_TargetLow, s_TargetHigh, extra);
		GetGame().GetUIManager().HideScriptedMenu(this);
	}

	override bool OnClick(Widget w, int x, int y, int button)
	{
		for (int idx = 0; idx < 5; idx++)
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
