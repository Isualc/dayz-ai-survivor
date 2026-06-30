// IsuVoice — In-Game-Arena-Setup (Taste Einfg).
//
// Modell, Rolle, Idle-Takt, Zug-Limit und die drei Direkttasten sind echte
// Aufklapp-Dropdowns: ein head-Button zeigt den Wert, ein Klick blendet ein
// Panel mit den Optionen als gestapelte Buttons ein (zuverlaessiges Rendern,
// kein Listbox-Style noetig). Auswahl per Klick uebernimmt und schliesst. Die
// Zwei-Wert-Schalter (Gesinnung, Spawn, Ziel) plus Agent an/aus, Mikrofon und
// Comic-Chat sind Toggle-Buttons. Name wird frei eingetippt. "Starten"/
// "Stoppen" schickt den Befehl als RPC an den Server (IsuSurvivor schreibt ihn
// fuer den arena_supervisor.py in eine Datei). Statuszeile = Supervisor-Antwort.

// Ein Aufklapp-Dropdown: head-Button + Options-Panel mit dynamisch erzeugten
// Item-Buttons (gestapelt via SetPos, Muster wie vanilla actionmenu/chat). Nur
// ein Dropdown ist gleichzeitig offen (das Menue schliesst die anderen).
class IsuDropdown
{
	protected ButtonWidget m_Head;
	protected Widget m_Container;             // Options-Panel (opak, visible 0)
	protected ref array<ButtonWidget> m_ItemBtns;
	protected ref TStringArray m_Items;
	protected int m_Current;
	protected int m_Cols;
	protected bool m_Open;
	protected bool m_ShowArrow = true;   // Pfeil "v"/"^" am Kopf; in engen Spalten aus

	void Setup(Widget root, string headName, string containerName, TStringArray items, int current, int cols)
	{
		m_Head = ButtonWidget.Cast(root.FindAnyWidget(headName));
		m_Container = root.FindAnyWidget(containerName);
		m_Items = items;
		m_Current = current;
		m_Cols = cols;
		if (m_Cols < 1)
			m_Cols = 1;
		BuildItems();
		Close();
	}

	// Item-Buttons ins Panel erzeugen und im Raster (Spalten m_Cols) anordnen.
	protected void BuildItems()
	{
		m_ItemBtns = new array<ButtonWidget>();
		if (!m_Container || !m_Items)
			return;
		int count = m_Items.Count();
		int rows = (count + m_Cols - 1) / m_Cols;
		if (rows < 1)
			rows = 1;
		float rowH = 40.0;
		float cw, ch;
		m_Container.GetSize(cw, ch);
		float colW = cw / m_Cols;
		for (int i = 0; i < count; i++)
		{
			Widget iw = GetGame().GetWorkspace().CreateWidgets("IsuVoice/GUI/isu_dd_item.layout", m_Container);
			ButtonWidget b = ButtonWidget.Cast(iw);
			if (!b)
				continue;
			b.SetText(m_Items[i]);
			b.SetTextColor(ARGB(255, 246, 248, 252));   // hell: auf dunklem Item-Bg gut lesbar
			// EXACTPOS/EXACTSIZE erzwingen, sonst interpretiert SetPos/SetSize die
			// Werte RELATIV (0..1) und alle Items landen uebereinander.
			b.SetFlags(WidgetFlags.EXACTPOS | WidgetFlags.EXACTSIZE);
			int c = i / rows;
			int r = i % rows;
			b.SetPos(c * colW, r * rowH);
			b.SetSize(colW, rowH);
			m_ItemBtns.Insert(b);
		}
		m_Container.SetSize(cw, rows * rowH);
	}

	void UpdateHead()
	{
		if (!m_Head)
			return;
		string arrow = "";
		if (m_ShowArrow)
		{
			arrow = "  v";
			if (m_Open)
				arrow = "  ^";
		}
		string label = "";
		if (m_Items && m_Current >= 0 && m_Current < m_Items.Count())
			label = m_Items[m_Current];
		m_Head.SetText(label + arrow);
	}

	void Open()
	{
		if (!m_Container)
			return;
		m_Open = true;
		m_Container.Show(true);
		UpdateHead();
	}

	void Close()
	{
		if (m_Container)
			m_Container.Show(false);
		m_Open = false;
		UpdateHead();
	}

	bool IsHead(Widget w)
	{
		return w == m_Head;
	}

	bool IsOpen()
	{
		return m_Open;
	}

	// Index, falls w einer der Item-Buttons ist; sonst -1.
	int ItemIndex(Widget w)
	{
		if (!m_ItemBtns)
			return -1;
		for (int i = 0; i < m_ItemBtns.Count(); i++)
		{
			if (m_ItemBtns[i] == w)
				return i;
		}
		return -1;
	}

	void SelectByItem(int idx)
	{
		m_Current = idx;
		Close();
	}

	int GetCurrent()
	{
		return m_Current;
	}

	// Pfeil-Affordance ein/aus (in engen Tabellenspalten aus, um Breite zu sparen).
	void SetShowArrow(bool b)
	{
		m_ShowArrow = b;
		UpdateHead();
	}

	// Items komplett neu setzen (Provider-Wechsel -> Modell-Dropdown zeigt jetzt
	// die Modelle des neuen Providers). Alte Item-Buttons abraeumen, neu bauen.
	void Rebuild(TStringArray items, int current)
	{
		if (m_ItemBtns)
		{
			for (int i = 0; i < m_ItemBtns.Count(); i++)
			{
				if (m_ItemBtns[i])
					m_ItemBtns[i].Unlink();
			}
		}
		m_Items = items;
		m_Current = current;
		BuildItems();
		Close();
	}
}

class IsuArenaMenu extends UIScriptedMenu
{
	// Auswahl bleibt ueber Menue-Sessions erhalten
	static ref TStringArray s_AgentIds = {"viktor", "birgit", "igor", "konrad"};
	static ref TStringArray s_DefaultNames = {"Viktor", "Birgit", "Igor", "Konrad"};
	static ref TStringArray s_Names = {"Viktor", "Birgit", "Igor", "Konrad"};
	// Modellwahl ZWEISTUFIG: erst Provider, dann Modell. Praefix = Backend
	// (resolve_backend): ohne = Anthropic Max-Plan, api/ = Anthropic-API,
	// openai/ google/ xai/ = claude-code-router, local/ = llama-server.
	// Modelle 2026-06-20 gegen die Provider-APIs verifiziert (echte Calls -> 200).
	static ref TStringArray s_Providers = {"Anthropic", "OpenAI", "Google", "xAI", "Local"};
	static ref TStringArray s_AnthropicModels = {"sonnet", "haiku", "opus", "claude-sonnet-5", "claude-opus-4-7", "claude-opus-4-8", "api/sonnet", "api/haiku", "api/opus"};
	static ref TStringArray s_AnthropicLabels = {"Sonnet 4.6", "Haiku 4.5", "Opus (auto)", "Sonnet 5", "Opus 4.7", "Opus 4.8", "Sonnet (API)", "Haiku (API)", "Opus (API)"};
	static ref TStringArray s_OpenAIModels = {"openai/gpt-5.5", "openai/gpt-5.4", "openai/gpt-5.4-mini", "openai/gpt-5.1", "openai/gpt-5-mini", "openai/gpt-4.1-mini", "openai/gpt-4o-mini"};
	static ref TStringArray s_OpenAILabels = {"GPT-5.5", "GPT-5.4", "GPT-5.4-mini", "GPT-5.1", "GPT-5-mini", "GPT-4.1-mini", "GPT-4o-mini"};
	static ref TStringArray s_GoogleModels = {"google/gemini-3.5-flash", "google/gemini-3.1-pro-preview", "google/gemini-3.1-flash-lite", "google/gemini-2.5-pro", "google/gemini-2.5-flash"};
	// Labels OHNE "Gemini"/"Grok"-Praefix: die Provider-Spalte zeigt schon Google/xAI,
	// das spart Breite in der engen Modell-Spalte.
	static ref TStringArray s_GoogleLabels = {"3.5 Flash", "3.1 Pro", "3.1 Lite", "2.5 Pro", "2.5 Flash"};
	static ref TStringArray s_XaiModels = {"xai/grok-4.3", "xai/grok-4.20-0309-reasoning", "xai/grok-4.20-0309-non-reasoning"};
	static ref TStringArray s_XaiLabels = {"4.3", "4.20 reason", "4.20 fast"};
	static ref TStringArray s_LocalModels = {"local/gemma-4-E4B-it"};
	static ref TStringArray s_LocalLabels = {"Gemma local"};

	// Backend-IDs bzw. Anzeige-Labels der Modelle eines Providers (Index in s_Providers).
	static TStringArray ProviderModelIds(int p)
	{
		if (p == 1) return s_OpenAIModels;
		if (p == 2) return s_GoogleModels;
		if (p == 3) return s_XaiModels;
		if (p == 4) return s_LocalModels;
		return s_AnthropicModels;
	}
	static TStringArray ProviderModelLabels(int p)
	{
		if (p == 1) return s_OpenAILabels;
		if (p == 2) return s_GoogleLabels;
		if (p == 3) return s_XaiLabels;
		if (p == 4) return s_LocalLabels;
		return s_AnthropicLabels;
	}
	static ref TStringArray s_PersonaKeys = {"jaeger", "bauer", "sanitaeter", "exmilitaer", "kampfmaschine"};
	static ref TStringArray s_PersonaLabels = {"Hunter", "Farmer", "Medic", "Ex-military", "Fighter"};
	// ElevenLabs-Stimmen (Name = Teilstring, discord_voice loest ihn gegen das
	// Konto auf; unbekannte fallen sicher auf die Default-Stimme zurueck). Index
	// 0-3 = die bisherigen Defaults pro Slot, danach die aktuellen ElevenLabs-
	// Standardstimmen (in jedem Konto vorhanden), multilingual nutzbar.
	static ref TStringArray s_VoiceNames = {"Helmut - German Epic", "Sarah", "George", "Liam", "Aria", "Roger", "Laura", "Charlie", "Callum", "River", "Charlotte", "Alice", "Matilda", "Will", "Jessica", "Eric", "Chris", "Brian", "Daniel", "Lily", "Bill"};
	// Kurz-Labels fuer Kopf + Liste (Spalte schmal). Index-gleich zu s_VoiceNames;
	// gesendet wird s_VoiceNames (volle Aufloesung gegen ElevenLabs), angezeigt s_VoiceLabels.
	static ref TStringArray s_VoiceLabels = {"Helmut", "Sarah", "George", "Liam", "Aria", "Roger", "Laura", "Charlie", "Callum", "River", "Charlotte", "Alice", "Matilda", "Will", "Jessica", "Eric", "Chris", "Brian", "Daniel", "Lily", "Bill"};
	// Default-Stimme je Slot (Index in s_VoiceNames): Viktor=Helmut, Birgit=Sarah,
	// Igor=George, Konrad=Liam - genau die bisherigen agents.json-Stimmen.
	static ref array<int> s_VoiceIdx = {0, 1, 2, 3};
	// Ausgabe-Sprache der NPC. Codes MUESSEN mit run_agent.LANG_NAMES uebereinstimmen.
	// Labels bewusst ASCII (EnforceScript-Datei-Encoding sicher).
	static ref TStringArray s_LangCodes = {"de", "en", "fr", "es", "it", "pt", "nl", "pl", "ru", "uk", "tr", "sv", "cs", "da", "fi", "el", "ro", "hu", "no", "hr", "sk", "ja", "ko", "zh", "ar", "hi", "fil"};
	static ref TStringArray s_LangLabels = {"Deutsch", "English", "Francais", "Espanol", "Italiano", "Portugues", "Nederlands", "Polski", "Russian", "Ukrainian", "Turkce", "Svenska", "Cestina", "Dansk", "Suomi", "Greek", "Romana", "Magyar", "Norsk", "Hrvatski", "Slovak", "Japanese", "Korean", "Chinese", "Arabic", "Hindi", "Filipino"};
	static ref array<int> s_LangIdx = {0, 0, 0, 0};   // alle Default Deutsch
	static ref array<bool> s_Enabled = {true, true, true, true};
	// Default-Tiering: Viktor=Sonnet, Birgit=Haiku, Igor=Haiku, Konrad=Sonnet.
	// s_ProviderIdx = gewaehlter Provider je Slot (0=Anthropic), s_ModelIdx =
	// Modell-Index INNERHALB des Providers (s_AnthropicModels: sonnet=0, haiku=1).
	static ref array<int> s_ProviderIdx = {0, 0, 0, 0};
	static ref array<int> s_ModelIdx = {0, 1, 1, 0};
	static ref array<int> s_PersonaIdx = {0, 2, 1, 3};
	static bool s_Hostile = false;
	static float s_CampX = 4233.7;
	static float s_CampZ = 8512.2;
	static bool s_CampFromPlayer = false;
	static ref array<int> s_IdleValues = {60, 120, 180, 300};
	static int s_IdleIdx = 1;
	static ref array<int> s_TurnValues = {6, 10, 15, 20, 0};
	static int s_TurnsIdx = 1;
	static bool s_Mic = true;
	static bool s_GroupSpawn = false;   // false = getrennt spawnen, true = eng als Gruppe
	static bool s_ComicChat = true;     // true = Comic-Sprechblasen ueber NPC-Koepfen (Client-HUD)
	// Orchestrator (Schiedsrichter/Lagezentrum) an/aus. AUS = die vier NPCs
	// laufen unabhaengig (sauberer Modell-Benchmark, jedes Modell loest die
	// Lage allein). AN = der Supervisor startet daemon/orchestrator.py:
	// gemeinsames Squad-Lagebild + ratenbegrenzter Funk, ohne den NPCs Befehle
	// zu erteilen (sie entscheiden weiter selbst).
	static bool s_Orchestrator = false;
	// Ambient-AI-Patrouillen (Expansion AIPatrolSettings) der aktiven Karte.
	// AUS = saubere Arena/BR (nur die vier + Spieler). Wirkt ab Server-Neustart.
	static bool s_Patrols = false;

	// Identitaetsfarben je Slot (RGB 0..1): Viktor bernstein, Birgit tuerkis,
	// Igor gruen, Konrad blau. Auch fuer die Namensschilder im Spiel gedacht,
	// damit Menue und Kopf-Tag dieselbe Farbe tragen.
	static ref array<float> s_ColR = {0.94, 0.36, 0.59, 0.22};
	static ref array<float> s_ColG = {0.62, 0.79, 0.77, 0.54};
	static ref array<float> s_ColB = {0.15, 0.65, 0.35, 0.87};

	// Direktsteuerung im Spiel: frei waehlbare Tasten aus einer kollisionsarmen
	// Liste (die meisten Tasten sind von Spiel und Mods belegt) plus Zielmodus.
	// MissionGameplay.OnKeyPress liest s_KeyStopIdx/s_KeyGotoIdx/s_TargetAll.
	static ref array<int> s_SafeKeyCodes = {KeyCode.KC_NUMPAD5, KeyCode.KC_NUMPAD0, KeyCode.KC_NUMPAD1, KeyCode.KC_NUMPAD2, KeyCode.KC_NUMPAD3, KeyCode.KC_NUMPAD4, KeyCode.KC_NUMPAD6, KeyCode.KC_NUMPAD7, KeyCode.KC_NUMPAD8, KeyCode.KC_NUMPAD9, KeyCode.KC_DECIMAL, KeyCode.KC_DIVIDE, KeyCode.KC_MULTIPLY, KeyCode.KC_SUBTRACT, KeyCode.KC_ADD, KeyCode.KC_DELETE, KeyCode.KC_PRIOR, KeyCode.KC_NEXT};
	static ref array<string> s_SafeKeyLabels = {"Num 5", "Num 0", "Num 1", "Num 2", "Num 3", "Num 4", "Num 6", "Num 7", "Num 8", "Num 9", "Num ,", "Num /", "Num *", "Num -", "Num +", "Del", "PgUp", "PgDn"};
	static int s_KeyStopIdx = 0;   // Default: Num 5
	static int s_KeyGotoIdx = 1;   // Default: Num 0
	static int s_KeyRadialIdx = 10; // Default: Num , (Radialmenue oeffnen)
	static bool s_TargetAll = false;

	protected ButtonWidget m_BtnAgent0;
	protected ButtonWidget m_BtnAgent1;
	protected ButtonWidget m_BtnAgent2;
	protected ButtonWidget m_BtnAgent3;
	protected EditBoxWidget m_EditName0;
	protected EditBoxWidget m_EditName1;
	protected EditBoxWidget m_EditName2;
	protected EditBoxWidget m_EditName3;
	protected ButtonWidget m_BtnMode;
	protected ButtonWidget m_BtnSpawn;
	protected ButtonWidget m_BtnTarget;
	protected ButtonWidget m_BtnMic;
	protected ButtonWidget m_BtnComic;
	protected ButtonWidget m_BtnOrch;
	protected ButtonWidget m_BtnPatrol;
	protected ButtonWidget m_BtnCamp;
	protected ButtonWidget m_BtnStart;
	protected ButtonWidget m_BtnStop;
	protected ButtonWidget m_BtnMission;
	protected ButtonWidget m_BtnClose;
	protected TextWidget m_StatusText;
	protected ImageWidget m_StatusDot;
	protected ImageWidget m_StatusPill;
	protected TextWidget m_CostText;
	protected TextWidget m_ModeNote;
	protected ref array<ImageWidget> m_Cards;
	protected ref array<ImageWidget> m_Accents;
	protected ref array<TextWidget> m_Lives;

	// Dropdowns
	protected ref array<ref IsuDropdown> m_DdProvider;   // Stufe 1: Provider
	protected ref array<ref IsuDropdown> m_DdModel;      // Stufe 2: Modell des Providers
	protected ref array<ref IsuDropdown> m_DdRole;
	protected ref array<ref IsuDropdown> m_DdVoice;   // Stimme pro Slot (grosses Panel = "Fenster")
	protected ref array<ref IsuDropdown> m_DdLang;    // Ausgabe-Sprache pro Slot
	protected ref IsuDropdown m_DdIdle;
	protected ref IsuDropdown m_DdTurns;
	protected ref IsuDropdown m_DdKeyStop;
	protected ref IsuDropdown m_DdKeyGoto;
	protected ref IsuDropdown m_DdKeyRadial;
	protected ref array<ref IsuDropdown> m_All;   // fuer "alle schliessen"

	override Widget Init()
	{
		layoutRoot = GetGame().GetWorkspace().CreateWidgets("IsuVoice/GUI/isu_arena_menu.layout");

		m_BtnAgent0 = ButtonWidget.Cast(layoutRoot.FindAnyWidget("BtnAgent0"));
		m_BtnAgent1 = ButtonWidget.Cast(layoutRoot.FindAnyWidget("BtnAgent1"));
		m_BtnAgent2 = ButtonWidget.Cast(layoutRoot.FindAnyWidget("BtnAgent2"));
		m_BtnAgent3 = ButtonWidget.Cast(layoutRoot.FindAnyWidget("BtnAgent3"));
		m_EditName0 = EditBoxWidget.Cast(layoutRoot.FindAnyWidget("EditName0"));
		m_EditName1 = EditBoxWidget.Cast(layoutRoot.FindAnyWidget("EditName1"));
		m_EditName2 = EditBoxWidget.Cast(layoutRoot.FindAnyWidget("EditName2"));
		m_EditName3 = EditBoxWidget.Cast(layoutRoot.FindAnyWidget("EditName3"));
		m_BtnMode = ButtonWidget.Cast(layoutRoot.FindAnyWidget("BtnMode"));
		m_BtnSpawn = ButtonWidget.Cast(layoutRoot.FindAnyWidget("BtnSpawn"));
		m_BtnTarget = ButtonWidget.Cast(layoutRoot.FindAnyWidget("BtnTarget"));
		m_BtnMic = ButtonWidget.Cast(layoutRoot.FindAnyWidget("BtnMic"));
		m_BtnComic = ButtonWidget.Cast(layoutRoot.FindAnyWidget("BtnComic"));
		m_BtnOrch = ButtonWidget.Cast(layoutRoot.FindAnyWidget("BtnOrch"));
		m_BtnPatrol = ButtonWidget.Cast(layoutRoot.FindAnyWidget("BtnPatrol"));
		m_BtnCamp = ButtonWidget.Cast(layoutRoot.FindAnyWidget("BtnCamp"));
		m_BtnStart = ButtonWidget.Cast(layoutRoot.FindAnyWidget("BtnStart"));
		m_BtnStop = ButtonWidget.Cast(layoutRoot.FindAnyWidget("BtnStop"));
		m_BtnMission = ButtonWidget.Cast(layoutRoot.FindAnyWidget("BtnMission"));
		m_BtnClose = ButtonWidget.Cast(layoutRoot.FindAnyWidget("BtnClose"));
		m_StatusText = TextWidget.Cast(layoutRoot.FindAnyWidget("StatusText"));
		m_StatusDot = ImageWidget.Cast(layoutRoot.FindAnyWidget("StatusDot"));
		m_StatusPill = ImageWidget.Cast(layoutRoot.FindAnyWidget("StatusPill"));
		m_CostText = TextWidget.Cast(layoutRoot.FindAnyWidget("CostText"));
		m_ModeNote = TextWidget.Cast(layoutRoot.FindAnyWidget("ModeNote"));

		m_Cards = new array<ImageWidget>();
		m_Accents = new array<ImageWidget>();
		m_Lives = new array<TextWidget>();
		for (int ci = 0; ci < 4; ci++)
		{
			m_Cards.Insert(ImageWidget.Cast(layoutRoot.FindAnyWidget("Card" + ci.ToString())));
			m_Accents.Insert(ImageWidget.Cast(layoutRoot.FindAnyWidget("Accent" + ci.ToString())));
			m_Lives.Insert(TextWidget.Cast(layoutRoot.FindAnyWidget("Live" + ci.ToString())));
		}

		for (int idx = 0; idx < 4; idx++)
		{
			EditBoxWidget eb = NameBox(idx);
			if (eb)
				eb.SetText(s_Names[idx]);
		}

		// Dropdowns aufbauen. Lange Listen (Tasten) zweispaltig, damit sie nicht
		// ueber den Bildschirmrand wachsen.
		m_All = new array<ref IsuDropdown>();
		m_DdProvider = new array<ref IsuDropdown>();
		m_DdModel = new array<ref IsuDropdown>();
		m_DdRole = new array<ref IsuDropdown>();
		m_DdVoice = new array<ref IsuDropdown>();
		m_DdLang = new array<ref IsuDropdown>();
		for (int slot = 0; slot < 4; slot++)
		{
			// Stufe 1: Provider. Stufe 2: Modell des aktuell gewaehlten Providers.
			m_DdProvider.Insert(MakeDd("ProviderHead" + slot.ToString(), "ProviderList" + slot.ToString(), s_Providers, s_ProviderIdx[slot], 1));
			m_DdModel.Insert(MakeDd("ModelHead" + slot.ToString(), "ModelList" + slot.ToString(), ProviderModelLabels(s_ProviderIdx[slot]), s_ModelIdx[slot], 1));
			m_DdRole.Insert(MakeDd("RoleHead" + slot.ToString(), "RoleList" + slot.ToString(), s_PersonaLabels, s_PersonaIdx[slot], 1));
			// Stimme: das Listen-Panel ist gross + zentriert (s. Layout) und wirkt
			// wie ein Fenster ueber dem Menue. 3 Spalten, damit 21 Stimmen passen.
			m_DdVoice.Insert(MakeDd("VoiceHead" + slot.ToString(), "VoiceList" + slot.ToString(), s_VoiceLabels, s_VoiceIdx[slot], 3));
			// Sprache: normales Dropdown, 26 Eintraege zweispaltig.
			m_DdLang.Insert(MakeDd("LangHead" + slot.ToString(), "LangList" + slot.ToString(), s_LangLabels, s_LangIdx[slot], 2));
			// Die fuenf Tabellen-Dropdowns ohne Pfeil (Spalten zu eng); der Hinweis
			// unten erklaert, dass ein Klick die Liste oeffnet. Klickbar bleiben sie
			// (Button-Hover). Idle/Turns/Tasten behalten ihren Pfeil (genug Platz).
			m_DdProvider[slot].SetShowArrow(false);
			m_DdModel[slot].SetShowArrow(false);
			m_DdRole[slot].SetShowArrow(false);
			m_DdVoice[slot].SetShowArrow(false);
			m_DdLang[slot].SetShowArrow(false);
		}
		m_DdIdle = MakeDd("IdleHead", "IdleList", IdleLabels(), s_IdleIdx, 1);
		m_DdTurns = MakeDd("TurnsHead", "TurnsList", TurnLabels(), s_TurnsIdx, 1);
		m_DdKeyStop = MakeDd("KeyStopHead", "KeyStopList", s_SafeKeyLabels, s_KeyStopIdx, 2);
		m_DdKeyGoto = MakeDd("KeyGotoHead", "KeyGotoList", s_SafeKeyLabels, s_KeyGotoIdx, 2);
		m_DdKeyRadial = MakeDd("KeyRadialHead", "KeyRadialList", s_SafeKeyLabels, s_KeyRadialIdx, 2);

		UpdateLabels();
		return layoutRoot;
	}

	protected IsuDropdown MakeDd(string headName, string containerName, TStringArray items, int current, int cols)
	{
		IsuDropdown dd = new IsuDropdown();
		dd.Setup(layoutRoot, headName, containerName, items, current, cols);
		m_All.Insert(dd);
		return dd;
	}

	protected TStringArray IdleLabels()
	{
		TStringArray a = new TStringArray();
		for (int i = 0; i < s_IdleValues.Count(); i++)
			a.Insert(s_IdleValues[i].ToString() + "s");
		return a;
	}

	protected TStringArray TurnLabels()
	{
		TStringArray a = new TStringArray();
		for (int i = 0; i < s_TurnValues.Count(); i++)
		{
			if (s_TurnValues[i] == 0)
				a.Insert("OFF");
			else
				a.Insert(s_TurnValues[i].ToString() + " turns");
		}
		return a;
	}

	override bool UseMouse()
	{
		return true;
	}

	override bool UseKeyboard()
	{
		return true;
	}

	override void Update(float timeslice)
	{
		super.Update(timeslice);
		if (!m_StatusText)
			return;

		// Supervisor-Status -> Ampel. Die Schluesselwoerter kommen aus
		// arena_supervisor.write_status (LAEUFT/GESTOPPT/FEHLER/WARTE/...).
		string raw = IsuArenaStatusStore.s_Text;
		string disp = raw;
		if (disp.Length() > 40)
			disp = disp.Substring(0, 37) + "...";
		m_StatusText.SetText(disp);

		int dotCol = ARGBF(1.0, 0.45, 0.45, 0.48);
		int pillCol = ARGBF(0.95, 0.09, 0.11, 0.14);
		int txtCol = ARGBF(1.0, 0.85, 0.88, 0.90);

		if (raw.IndexOf("RUNNING") > -1)
		{
			dotCol = ARGBF(1.0, 0.48, 0.78, 0.30);
			pillCol = ARGBF(0.95, 0.06, 0.13, 0.06);
			txtCol = ARGBF(1.0, 0.66, 0.90, 0.58);
		}
		else if (raw.IndexOf("ERROR") > -1 || raw.IndexOf("ABORTED") > -1)
		{
			dotCol = ARGBF(1.0, 0.88, 0.30, 0.28);
			pillCol = ARGBF(0.95, 0.16, 0.06, 0.06);
			txtCol = ARGBF(1.0, 0.95, 0.62, 0.60);
		}
		else if (raw.IndexOf("WAIT") > -1 || raw.IndexOf("STARTING") > -1 || raw.IndexOf("STOPPING") > -1 || raw.IndexOf("sent") > -1)
		{
			dotCol = ARGBF(1.0, 0.94, 0.70, 0.25);
			pillCol = ARGBF(0.95, 0.14, 0.11, 0.04);
			txtCol = ARGBF(1.0, 0.96, 0.85, 0.55);
		}

		if (m_StatusDot)
			m_StatusDot.SetColor(dotCol);
		if (m_StatusPill)
			m_StatusPill.SetColor(pillCol);
		m_StatusText.SetColor(txtCol);
	}

	override void OnShow()
	{
		super.OnShow();
		// Spiel-Eingaben sperren: Charakter steht still, Maus steuert nur
		// das Menue, Tippen im Namensfeld loest keine Spielaktionen aus
		SetFocus(layoutRoot);
		GetGame().GetInput().ChangeGameFocus(1);
		GetGame().GetUIManager().ShowUICursor(true);
		GetGame().GetMission().PlayerControlDisable(INPUT_EXCLUDE_ALL);
	}

	override void OnHide()
	{
		// Auch bei ESC (ohne Schliessen-Button) die getippten Namen sichern
		ReadNames();
		CloseAllDropdowns();
		GetGame().GetInput().ChangeGameFocus(-1);
		GetGame().GetUIManager().ShowUICursor(false);
		GetGame().GetMission().PlayerControlEnable(true);
		super.OnHide();
	}

	protected ButtonWidget AgentButton(int idx)
	{
		if (idx == 0) return m_BtnAgent0;
		if (idx == 1) return m_BtnAgent1;
		if (idx == 2) return m_BtnAgent2;
		return m_BtnAgent3;
	}

	protected EditBoxWidget NameBox(int idx)
	{
		if (idx == 0) return m_EditName0;
		if (idx == 1) return m_EditName1;
		if (idx == 2) return m_EditName2;
		return m_EditName3;
	}

	protected string CleanName(string raw, int idx)
	{
		string n = raw;
		n.Replace("|", "");
		n.Replace(":", "");
		n.Replace("\"", "");
		n = n.Trim();
		if (n == "")
			n = s_DefaultNames[idx];
		return n;
	}

	// Gewuenschten Tasten-Index zurueckgeben, falls frei; sonst den naechsten
	// freien (gegen die beiden anderen Tasten). So fallen Stopp/Geh/Radial nie
	// auf dieselbe Taste (sonst wuerde der Radial-Zweig in OnKeyPress die
	// anderen beiden stumm ueberdecken).
	static int ResolveFreeKey(int want, int other1, int other2)
	{
		int count = s_SafeKeyCodes.Count();
		int n = want;
		for (int i = 0; i < count; i++)
		{
			if (n != other1 && n != other2)
				return n;
			n = (n + 1) % count;
		}
		return want;
	}

	protected void CloseAllDropdowns()
	{
		if (!m_All)
			return;
		for (int d = 0; d < m_All.Count(); d++)
			m_All[d].Close();
	}

	// EditBoxen -> s_Names (persistente Statics), bereinigt
	protected void ReadNames()
	{
		for (int idx = 0; idx < 4; idx++)
		{
			EditBoxWidget eb = NameBox(idx);
			if (!eb)
				continue;
			s_Names[idx] = CleanName(eb.GetText(), idx);
			eb.SetText(s_Names[idx]);
		}
	}

	// Nur die Nicht-Dropdown-Anzeigen aktualisieren (Karten, Akzente, Live,
	// Toggle-Buttons). Die Dropdown-Koepfe pflegen ihren Text selbst.
	protected void UpdateLabels()
	{
		for (int idx = 0; idx < 4; idx++)
		{
			string state = "OFF";
			if (s_Enabled[idx])
				state = "ON";
			ButtonWidget ab = AgentButton(idx);
			if (ab)
				ab.SetText(state);

			ImageWidget acc = null;
			ImageWidget card = null;
			TextWidget live = null;
			if (m_Accents && idx < m_Accents.Count())
				acc = m_Accents[idx];
			if (m_Cards && idx < m_Cards.Count())
				card = m_Cards[idx];
			if (m_Lives && idx < m_Lives.Count())
				live = m_Lives[idx];

			if (s_Enabled[idx])
			{
				if (acc)
					acc.SetColor(ARGBF(1.0, s_ColR[idx], s_ColG[idx], s_ColB[idx]));
				if (card)
					card.SetColor(ARGBF(0.95, 0.075, 0.090, 0.120));
				if (live)
				{
					live.SetText("selected");
					live.SetColor(ARGBF(1.0, s_ColR[idx], s_ColG[idx], s_ColB[idx]));
				}
			}
			else
			{
				if (acc)
					acc.SetColor(ARGBF(0.35, s_ColR[idx], s_ColG[idx], s_ColB[idx]));
				if (card)
					card.SetColor(ARGBF(0.85, 0.045, 0.050, 0.065));
				if (live)
				{
					live.SetText("off");
					live.SetColor(ARGBF(1.0, 0.45, 0.45, 0.48));
				}
			}
		}

		if (m_BtnMode)
		{
			if (s_Hostile)
				m_BtnMode.SetText("Hostile");
			else
				m_BtnMode.SetText("Neutral");
		}
		if (m_ModeNote)
		{
			if (s_Hostile)
				m_ModeNote.SetText("(battle royale)");
			else
				m_ModeNote.SetText("(co-op)");
		}
		if (m_BtnSpawn)
		{
			if (s_GroupSpawn)
				m_BtnSpawn.SetText("Group");
			else
				m_BtnSpawn.SetText("Separate");
		}
		if (m_BtnTarget)
		{
			if (s_TargetAll)
				m_BtnTarget.SetText("All NPCs");
			else
				m_BtnTarget.SetText("Aimed NPC");
		}
		if (m_BtnCamp)
			m_BtnCamp.SetText(Math.Round(s_CampX).ToString() + " / " + Math.Round(s_CampZ).ToString());
		if (m_BtnMic)
		{
			if (s_Mic)
				m_BtnMic.SetText("ON");
			else
				m_BtnMic.SetText("OFF");
		}
		if (m_BtnComic)
		{
			if (s_ComicChat)
				m_BtnComic.SetText("ON");
			else
				m_BtnComic.SetText("OFF");
		}
		if (m_BtnOrch)
		{
			if (s_Orchestrator)
				m_BtnOrch.SetText("ON");
			else
				m_BtnOrch.SetText("OFF");
		}
		if (m_BtnPatrol)
		{
			if (s_Patrols)
				m_BtnPatrol.SetText("ON");
			else
				m_BtnPatrol.SetText("OFF");
		}

		// Der Start-Button sagt IMMER, was er tun wird - eine versehentliche
		// Hostile-Wahl soll kein unbemerktes BR ausloesen.
		if (m_BtnStart)
		{
			if (s_Hostile)
				m_BtnStart.SetText("START: HOSTILE (BR)");
			else
				m_BtnStart.SetText("START (neutral)");
		}
	}

	protected string BuildCommand()
	{
		ReadNames();
		string cmd = "start";
		for (int idx = 0; idx < 4; idx++)
		{
			string flag = "0";
			if (s_Enabled[idx])
				flag = "1";
			TStringArray pmids = ProviderModelIds(s_ProviderIdx[idx]);
			int midx = s_ModelIdx[idx];
			if (midx < 0 || midx >= pmids.Count())
				midx = 0;
			string modelId = pmids.Get(midx);
			cmd = cmd + "|" + s_AgentIds[idx] + ":" + flag + ":" + modelId + ":" + s_PersonaKeys[s_PersonaIdx[idx]] + ":" + s_Names[idx];
			// Stimme + Sprache als eigenes, entkoppeltes Segment (der Name oben
			// darf ':' enthalten - darum NICHT ins Slot-Tupel quetschen).
			cmd = cmd + "|av:" + s_AgentIds[idx] + ":" + s_VoiceNames[s_VoiceIdx[idx]] + ":" + s_LangCodes[s_LangIdx[idx]];
		}
		string hostile = "0";
		if (s_Hostile)
			hostile = "1";
		string mic = "0";
		if (s_Mic)
			mic = "1";
		cmd = cmd + "|hostile:" + hostile;
		cmd = cmd + "|camp:" + s_CampX.ToString() + "," + s_CampZ.ToString();
		cmd = cmd + "|idle:" + s_IdleValues[s_IdleIdx].ToString();
		cmd = cmd + "|turns:" + s_TurnValues[s_TurnsIdx].ToString();
		cmd = cmd + "|mic:" + mic;
		string grp = "0";
		if (s_GroupSpawn)
			grp = "1";
		cmd = cmd + "|group:" + grp;
		string orch = "0";
		if (s_Orchestrator)
			orch = "1";
		cmd = cmd + "|orch:" + orch;
		string patrols = "0";
		if (s_Patrols)
			patrols = "1";
		cmd = cmd + "|patrols:" + patrols;
		return cmd;
	}

	protected void SendCommand(string cmd)
	{
		PlayerBase pb = PlayerBase.Cast(GetGame().GetPlayer());
		if (!pb)
			return;
		Param1<string> data = new Param1<string>(cmd);
		GetGame().RPCSingleParam(pb, ISU_RPC_ARENA_CMD, data, true);
		IsuArenaStatusStore.s_Text = "Command sent, waiting for supervisor...";
	}

	// Klick auf einen Dropdown-Item-Button -> Auswahl uebernehmen, in die
	// passende Static schreiben, Liste schliessen. true, wenn w ein Item war.
	protected bool ApplyIfItem(Widget w)
	{
		for (int slot = 0; slot < 4; slot++)
		{
			int pi = m_DdProvider[slot].ItemIndex(w);
			if (pi >= 0)
			{
				s_ProviderIdx[slot] = pi;
				s_ModelIdx[slot] = 0;
				m_DdProvider[slot].SelectByItem(pi);
				// Modell-Dropdown daneben mit den Modellen des neuen Providers fuellen
				m_DdModel[slot].Rebuild(ProviderModelLabels(pi), 0);
				return true;
			}
			int mi = m_DdModel[slot].ItemIndex(w);
			if (mi >= 0)
			{
				s_ModelIdx[slot] = mi;
				m_DdModel[slot].SelectByItem(mi);
				return true;
			}
			int ri = m_DdRole[slot].ItemIndex(w);
			if (ri >= 0)
			{
				s_PersonaIdx[slot] = ri;
				m_DdRole[slot].SelectByItem(ri);
				return true;
			}
			int vi = m_DdVoice[slot].ItemIndex(w);
			if (vi >= 0)
			{
				s_VoiceIdx[slot] = vi;
				m_DdVoice[slot].SelectByItem(vi);
				return true;
			}
			int li = m_DdLang[slot].ItemIndex(w);
			if (li >= 0)
			{
				s_LangIdx[slot] = li;
				m_DdLang[slot].SelectByItem(li);
				return true;
			}
		}
		int ii = m_DdIdle.ItemIndex(w);
		if (ii >= 0)
		{
			s_IdleIdx = ii;
			m_DdIdle.SelectByItem(ii);
			return true;
		}
		int ti = m_DdTurns.ItemIndex(w);
		if (ti >= 0)
		{
			s_TurnsIdx = ti;
			m_DdTurns.SelectByItem(ti);
			return true;
		}
		int ks = m_DdKeyStop.ItemIndex(w);
		if (ks >= 0)
		{
			int rks = ResolveFreeKey(ks, s_KeyGotoIdx, s_KeyRadialIdx);
			s_KeyStopIdx = rks;
			m_DdKeyStop.SelectByItem(rks);
			return true;
		}
		int kg = m_DdKeyGoto.ItemIndex(w);
		if (kg >= 0)
		{
			int rkg = ResolveFreeKey(kg, s_KeyStopIdx, s_KeyRadialIdx);
			s_KeyGotoIdx = rkg;
			m_DdKeyGoto.SelectByItem(rkg);
			return true;
		}
		int kr = m_DdKeyRadial.ItemIndex(w);
		if (kr >= 0)
		{
			int rkr = ResolveFreeKey(kr, s_KeyStopIdx, s_KeyGotoIdx);
			s_KeyRadialIdx = rkr;
			m_DdKeyRadial.SelectByItem(rkr);
			return true;
		}
		return false;
	}

	override bool OnClick(Widget w, int x, int y, int button)
	{
		// Dropdown-Koepfe: oeffnen/schliessen (immer nur einer offen)
		for (int d = 0; d < m_All.Count(); d++)
		{
			if (m_All[d].IsHead(w))
			{
				bool wasOpen = m_All[d].IsOpen();
				CloseAllDropdowns();
				if (!wasOpen)
					m_All[d].Open();
				return true;
			}
		}
		// Klick auf einen Options-Eintrag -> uebernehmen + schliessen
		if (ApplyIfItem(w))
			return true;
		// alles andere schliesst offene Listen
		CloseAllDropdowns();

		for (int idx = 0; idx < 4; idx++)
		{
			if (w == AgentButton(idx))
			{
				s_Enabled[idx] = !s_Enabled[idx];
				UpdateLabels();
				return true;
			}
		}

		if (w == m_BtnMode)
		{
			s_Hostile = !s_Hostile;
			UpdateLabels();
			return true;
		}
		if (w == m_BtnSpawn)
		{
			s_GroupSpawn = !s_GroupSpawn;
			UpdateLabels();
			return true;
		}
		if (w == m_BtnTarget)
		{
			s_TargetAll = !s_TargetAll;
			UpdateLabels();
			return true;
		}
		if (w == m_BtnMic)
		{
			s_Mic = !s_Mic;
			UpdateLabels();
			return true;
		}
		if (w == m_BtnComic)
		{
			s_ComicChat = !s_ComicChat;
			UpdateLabels();
			return true;
		}
		if (w == m_BtnOrch)
		{
			s_Orchestrator = !s_Orchestrator;
			UpdateLabels();
			return true;
		}
		if (w == m_BtnPatrol)
		{
			s_Patrols = !s_Patrols;
			UpdateLabels();
			return true;
		}
		if (w == m_BtnCamp)
		{
			Man player = GetGame().GetPlayer();
			if (player)
			{
				vector pos = player.GetPosition();
				s_CampX = pos[0];
				s_CampZ = pos[2];
				s_CampFromPlayer = true;
			}
			UpdateLabels();
			return true;
		}
		if (w == m_BtnStart)
		{
			SendCommand(BuildCommand());
			return true;
		}
		if (w == m_BtnMission)
		{
			// Skript-Mission "Birgit befreien": wie START, aber mit Mission-Flag.
			// Der Supervisor setzt dann Spawn (Lukow), Rally (Kopa), Briefings
			// und die Gefangene; die Banditen sind eine feste Patrouille auf Livonia.
			SendCommand(BuildCommand() + "|mission:birgit");
			return true;
		}
		if (w == m_BtnStop)
		{
			SendCommand("stop");
			return true;
		}
		if (w == m_BtnClose)
		{
			ReadNames();
			GetGame().GetUIManager().HideScriptedMenu(this);
			return true;
		}

		return super.OnClick(w, x, y, button);
	}
}

modded class MissionGameplay
{
	ref IsuArenaMenu m_IsuArenaMenu;
	ref IsuRadialMenu m_IsuRadialMenu;

	// Schwebende Namensschilder jeden Frame ueber die Agenten-Koepfe setzen
	// (Client). Die Render-/Projektionslogik liegt in IsuNameplateHud.
	override void OnUpdate(float timeslice)
	{
		super.OnUpdate(timeslice);
		if (GetGame() && !GetGame().IsDedicatedServer())
			IsuNameplateHud.Tick();
	}

	override void OnKeyPress(int key)
	{
		super.OnKeyPress(key);

		// Einfg/Insert - Pos1 und Ende gehoeren dem VPP-Adminpanel
		if (key == KeyCode.KC_INSERT)
		{
			Isu_ToggleArenaMenu();
			return;
		}

		// Radialmenue-Taste: togglet auch bei offenem Radial (zum Schliessen),
		// daher VOR dem Menue-Guard.
		int radialKey = IsuArenaMenu.s_SafeKeyCodes[IsuArenaMenu.s_KeyRadialIdx];
		if (key == radialKey)
		{
			Isu_ToggleRadialMenu();
			return;
		}

		// Direktbefehle an die NPCs nur, wenn kein Menue offen ist (Inventar,
		// Karte, Einfg-Setup fangen die Taste sonst selbst ab).
		if (GetGame().GetUIManager().GetMenu())
			return;

		int stopKey = IsuArenaMenu.s_SafeKeyCodes[IsuArenaMenu.s_KeyStopIdx];
		int gotoKey = IsuArenaMenu.s_SafeKeyCodes[IsuArenaMenu.s_KeyGotoIdx];
		if (key == stopKey)
			IsuNpcCommand.SendHalt(IsuArenaMenu.s_TargetAll);
		else if (key == gotoKey)
			IsuNpcCommand.SendGoto(IsuArenaMenu.s_TargetAll);
	}

	void Isu_ToggleArenaMenu()
	{
		UIScriptedMenu current = GetGame().GetUIManager().GetMenu();

		if (m_IsuArenaMenu && current == m_IsuArenaMenu)
		{
			GetGame().GetUIManager().HideScriptedMenu(m_IsuArenaMenu);
			return;
		}

		// Anderes Menue offen (Inventar, Karte...) - nicht dazwischenfunken
		if (current)
			return;

		if (!m_IsuArenaMenu)
			m_IsuArenaMenu = new IsuArenaMenu();

		GetGame().GetUIManager().ShowScriptedMenu(m_IsuArenaMenu, null);
	}

	void Isu_ToggleRadialMenu()
	{
		UIScriptedMenu current = GetGame().GetUIManager().GetMenu();

		if (m_IsuRadialMenu && current == m_IsuRadialMenu)
		{
			GetGame().GetUIManager().HideScriptedMenu(m_IsuRadialMenu);
			return;
		}

		// Anderes Menue offen (Inventar, Karte, Einfg-Setup) - nicht stoeren
		if (current)
			return;

		// Ziel beim Oeffnen einfrieren: anvisierter eAIBase, sonst kein Ziel
		// (dann faellt das Radial serverseitig auf "naechster NPC" zurueck).
		vector hitPos;
		Object aimedObj;
		IsuNpcCommand.AimRaycast(hitPos, aimedObj);
		eAIBase ai = eAIBase.Cast(aimedObj);
		if (ai)
		{
			int low, high;
			ai.GetNetworkID(low, high);
			IsuRadialMenu.s_HasTarget = true;
			IsuRadialMenu.s_TargetLow = low;
			IsuRadialMenu.s_TargetHigh = high;
			IsuRadialMenu.s_TargetName = ai.GetDisplayName();
		}
		else
		{
			IsuRadialMenu.s_HasTarget = false;
			IsuRadialMenu.s_TargetLow = 0;
			IsuRadialMenu.s_TargetHigh = 0;
			IsuRadialMenu.s_TargetName = "next NPC";
		}

		if (!m_IsuRadialMenu)
			m_IsuRadialMenu = new IsuRadialMenu();

		GetGame().GetUIManager().ShowScriptedMenu(m_IsuRadialMenu, null);
	}
}
