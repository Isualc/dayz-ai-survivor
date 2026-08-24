// IsuVoice — In-Game-Arena-Setup (Taste Einfg).
//
// NPC-Zeilen sind DYNAMISCH: Start mit einer Zeile, [+ NPC] haengt bis zu 10
// an (Slots 0-3 = viktor/birgit/igor/konrad, 4-9 = npc5..npc10), X entfernt
// eine Zeile; die Zeilen kommen aus isu_arena_row.layout und stapeln sich in
// einem ScrollWidget (ArenaScroll/RowsHost). Modell, Rolle, Stimme, Sprache,
// Idle-Takt, Zug-Limit und die drei Direkttasten sind Aufklapp-Dropdowns, die
// sich alle EIN geteiltes DdPopup-Panel teilen (klappt nach oben, wenn unten
// kein Platz ist). Gesinnung (Neutral/Hostile/Free) und Squad-HUD sind
// zyklische Dreifach-Schalter; Spawn, Ziel, Agent an/aus, Mikrofon und
// Comic-Chat bleiben Zwei-Wert-Toggles. Name wird frei eingetippt.
// Mission/Event ist ein Dropdown (none/birgit/horde); bei Auswahl != none
// haengt "Starten" das Segment "mission:<id>" an. "Starten"/"Stoppen" schickt
// den Befehl als RPC an den Server (IsuSurvivor schreibt ihn fuer den
// arena_supervisor.py in eine Datei). Statuszeile = Supervisor-Antwort.
// Protokoll v2 (seit 23.08.): "start|v:2|count:<n>|..." - Stamm-Slots 0-3 im
// Alt-Format, Zusatz-Slots als "npc:<id>:..."-Segmente; der Supervisor
// validiert IDs per Regex und verteilt Zusatz-Spawns auf einem Ring.

// Ein Aufklapp-Dropdown: head-Button + EIN geteiltes Popup-Panel (DdPopup im
// Layout-Root), das beim Oeffnen an den Kopf gesetzt und mit Item-Buttons
// befuellt wird. Es existiert immer nur die Liste des gerade offenen Dropdowns
// (statt frueher 26 vorgebauter Panels mit ~1060 Widgets); klappt nach oben,
// wenn unten kein Platz ist. Nur ein Dropdown ist gleichzeitig offen.
class IsuDropdown
{
	// Zeilenhoehe der Listen-Items. 32 statt 40, damit die langen Listen
	// (Sprachen, Tasten) innerhalb der Menuegrenzen bleiben. Position/Groesse
	// im isu_dd_item.layout werden zur Laufzeit ohnehin ueberschrieben.
	static const float ITEM_ROW_H = 32.0;

	protected ButtonWidget m_Head;
	protected Widget m_Popup;                 // geteiltes DdPopup, nur gesetzt solange DIESES Dropdown offen ist
	protected ref array<ButtonWidget> m_ItemBtns;   // nur befuellt, solange offen
	protected ref array<Widget> m_AllCells;   // Items + Fueller, fuer Cleanup beim Schliessen
	protected ref TStringArray m_Items;
	protected int m_Current;
	protected int m_Cols;
	protected float m_ColW;   // Spaltenbreite der Liste; 0 = Kopfbreite uebernehmen
	protected bool m_Open;
	protected bool m_ShowArrow = true;   // Pfeil "v"/"^" am Kopf; in engen Spalten aus
	// Rueckadresse fuer ApplyIfItem: welches Feld (IsuArenaMenu.DD_*) und
	// welcher NPC-Slot (-1 bei globalen Dropdowns wie Idle/Turns).
	protected int m_KindTag = -1;
	protected int m_SlotTag = -1;

	void SetTags(int kind, int slot)
	{
		m_KindTag = kind;
		m_SlotTag = slot;
	}

	int GetKindTag()
	{
		return m_KindTag;
	}

	int GetSlotTag()
	{
		return m_SlotTag;
	}

	void Setup(Widget root, string headName, TStringArray items, int current, int cols, float colW)
	{
		m_Head = ButtonWidget.Cast(root.FindAnyWidget(headName));
		// Layout-Tippfehler frueh sichtbar machen statt lautlos totes Dropdown
		if (!m_Head)
			Print("[IsuArena] Dropdown-Kopf fehlt: " + headName);
		m_Items = items;
		m_Current = current;
		m_Cols = cols;
		if (m_Cols < 1)
			m_Cols = 1;
		m_ColW = colW;
		m_ItemBtns = new array<ButtonWidget>();
		m_AllCells = new array<Widget>();
		m_Open = false;
		UpdateHead();
	}

	// Zebra-Schattierung: gerade/ungerade Zeile, aktueller Eintrag gruen.
	protected int ShadeFor(int i, int rows)
	{
		if (i == m_Current)
			return ARGB(255, 30, 58, 38);
		int r = i % rows;
		if (r % 2 == 1)
			return ARGB(255, 30, 36, 47);
		return ARGB(255, 22, 27, 36);
	}

	protected int RowCount()
	{
		int count = 0;
		if (m_Items)
			count = m_Items.Count();
		int rows = (count + m_Cols - 1) / m_Cols;
		if (rows < 1)
			rows = 1;
		return rows;
	}

	// Alle Zellen des Popups abraeumen (beim Schliessen; das Popup wird beim
	// naechsten Oeffnen ohnehin frisch befuellt).
	protected void ClearCells()
	{
		if (m_AllCells)
		{
			for (int i = 0; i < m_AllCells.Count(); i++)
			{
				if (m_AllCells[i])
					m_AllCells[i].Unlink();
			}
		}
		m_ItemBtns = new array<ButtonWidget>();
		m_AllCells = new array<Widget>();
	}

	// Popup an den Kopf setzen, dimensionieren und mit Item-Buttons befuellen.
	// menuRoot = Menue-Root (liefert Referenzgroesse und Screen-Rahmen).
	protected void BuildItemsInto(Widget popup, Widget menuRoot)
	{
		ClearCells();
		if (!popup || !m_Items || !m_Head || !menuRoot)
			return;
		int count = m_Items.Count();
		int rows = RowCount();

		// Geometrie: unter dem Kopf aufklappen; wenn unten kein Platz ist, nach
		// oben; seitlich an die Menuekante clampen. Die Koepfe leben seit dem
		// Zeilen-Umbau in gescrollten Sub-Baeumen - darum ueber SCREEN-Position
		// in den Root-Raum zurueckrechnen (beruecksichtigt Scroll-Offset und
		// UI-Skalierung), statt GetPos relativ zum unbekannten Parent zu nehmen.
		float menuW, menuH;
		menuRoot.GetSize(menuW, menuH);
		float rootSX, rootSY, rootSW, rootSH;
		menuRoot.GetScreenPos(rootSX, rootSY);
		menuRoot.GetScreenSize(rootSW, rootSH);
		float scale = 1.0;
		if (menuW > 0 && rootSW > 0)
			scale = rootSW / menuW;
		float headSX, headSY, headSW, headSH;
		m_Head.GetScreenPos(headSX, headSY);
		m_Head.GetScreenSize(headSW, headSH);
		float headX = (headSX - rootSX) / scale;
		float headY = (headSY - rootSY) / scale;
		float headW = headSW / scale;
		float headH = headSH / scale;
		float colW = m_ColW;
		if (colW <= 0)
			colW = headW;
		float listW = colW * m_Cols;
		float listH = rows * ITEM_ROW_H;
		float x = headX;
		float y = headY + headH;
		if (y + listH > menuH)
			y = headY - listH;
		if (y < 0)
			y = 0;
		if (x + listW > menuW)
			x = menuW - listW;
		if (x < 0)
			x = 0;
		popup.SetFlags(WidgetFlags.EXACTPOS | WidgetFlags.EXACTSIZE);
		popup.SetPos(x, y);
		popup.SetSize(listW, listH);

		// PanelWidget/ButtonWidget ohne Textur rendern ihre color-Flaeche nicht -
		// jedes Item bringt deshalb ein eigenes opakes ImageWidget (DdItemBg)
		// mit; Zellen ueber count hinaus werden als leere Fueller erzeugt, damit
		// die letzte Spalte kein transparentes Loch hat.
		int total = rows * m_Cols;
		for (int i = 0; i < total; i++)
		{
			Widget iw = GetGame().GetWorkspace().CreateWidgets("IsuVoice/GUI/isu_dd_item.layout", popup);
			// Sofort registrieren, damit ClearCells die Zelle auch dann abraeumt,
			// wenn der Cast darunter fehlschlaegt.
			m_AllCells.Insert(iw);
			ButtonWidget b = ButtonWidget.Cast(iw);
			if (!b)
				continue;
			// EXACTPOS/EXACTSIZE erzwingen, sonst interpretiert SetPos/SetSize die
			// Werte RELATIV (0..1) und alle Items landen uebereinander.
			b.SetFlags(WidgetFlags.EXACTPOS | WidgetFlags.EXACTSIZE);
			int c = i / rows;
			int r = i % rows;
			b.SetPos(c * colW, r * ITEM_ROW_H);
			b.SetSize(colW, ITEM_ROW_H);
			ImageWidget bg = ImageWidget.Cast(iw.FindAnyWidget("DdItemBg"));
			if (bg)
			{
				bg.SetFlags(WidgetFlags.EXACTPOS | WidgetFlags.EXACTSIZE);
				bg.SetPos(0, 0);
				bg.SetSize(colW, ITEM_ROW_H);
				bg.SetColor(ShadeFor(i, rows));
			}
			TextWidget lbl = TextWidget.Cast(iw.FindAnyWidget("DdItemLabel"));
			string txt = "";
			if (i < count)
				txt = m_Items[i];
			if (lbl)
			{
				lbl.SetFlags(WidgetFlags.EXACTPOS | WidgetFlags.EXACTSIZE);
				lbl.SetPos(12, 0);
				lbl.SetSize(colW - 12, ITEM_ROW_H);
				lbl.SetText(txt);
				lbl.SetColor(ARGB(255, 246, 248, 252));
			}
			else
			{
				b.SetText(txt);
				b.SetTextColor(ARGB(255, 246, 248, 252));
			}
			// Fueller-Zellen sind nicht auswaehlbar (kein Eintrag in m_ItemBtns).
			if (i < count)
				m_ItemBtns.Insert(b);
		}
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

	// Liste im geteilten Popup oeffnen. Der Aufrufer schliesst vorher alle
	// anderen Dropdowns (nur eines darf das Popup halten).
	void OpenIn(Widget popup, Widget menuRoot)
	{
		if (!popup || !m_Head)
			return;
		m_Popup = popup;
		m_Open = true;
		BuildItemsInto(popup, menuRoot);
		popup.Show(true);
		UpdateHead();
	}

	void Close()
	{
		if (m_Open)
			ClearCells();
		if (m_Popup)
		{
			m_Popup.Show(false);
			m_Popup = null;
		}
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

	// Index, falls w einer der Item-Buttons ist; sonst -1. Geschlossene
	// Dropdowns haben keine Items - liefern also immer -1.
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

	// Kopf-Textfarbe (OFF-Zeilen werden ausgegraut)
	void SetHeadTextColor(int color)
	{
		if (m_Head)
			m_Head.SetTextColor(color);
	}

	// Items komplett neu setzen (Provider-Wechsel -> Modell-Dropdown zeigt jetzt
	// die Modelle des neuen Providers). Beim geschlossenen Dropdown reine
	// Datenpflege ohne Widget-Arbeit - der fruehere Unlink-im-Klick-Handler-
	// Pfad (Engine haelt danach freigegebene Pointer) entfaellt damit.
	void Rebuild(TStringArray items, int current)
	{
		if (m_Open)
			Close();
		m_Items = items;
		m_Current = current;
		UpdateHead();
	}
}

// Eine NPC-Zeile im Menue: aus isu_arena_row.layout instanziiert (generische
// Widget-Namen, FindAnyWidget loest im Zeilen-Subtree auf), traegt ihren
// Slot-Index in die s_*-Statics des Menues. Zeilen werden dynamisch erzeugt/
// entfernt ([+ NPC] / X) und im RowsHost des ScrollWidgets gestapelt.
class IsuArenaRow
{
	Widget m_Root;
	ImageWidget m_Card;
	ImageWidget m_Accent;
	ButtonWidget m_BtnAgent;
	EditBoxWidget m_EditName;
	ButtonWidget m_BtnRemove;
	TextWidget m_Live;
	ref IsuDropdown m_DdProvider;
	ref IsuDropdown m_DdModel;
	ref IsuDropdown m_DdRole;
	ref IsuDropdown m_DdLoadout;
	ref IsuDropdown m_DdVoice;
	ref IsuDropdown m_DdLang;
	int m_Slot;   // Index in die s_*-Arrays (0..9), NICHT die Anzeigeposition

	void Destroy()
	{
		if (m_Root)
			m_Root.Unlink();
		m_Root = null;
	}
}

class IsuArenaMenu extends UIScriptedMenu
{
	// Auswahl bleibt ueber Menue-Sessions erhalten.
	// Slots 0-3 = die vier klassischen Agenten; 4-9 = Zusatz-Slots (npc5..npc10,
	// gehen als v2-"npc:"-Segmente raus; Roster-Defaults in arena/agents.json).
	static ref TStringArray s_AgentIds = {"viktor", "birgit", "igor", "konrad", "npc5", "npc6", "npc7", "npc8", "npc9", "npc10"};
	static ref TStringArray s_DefaultNames = {"Viktor", "Birgit", "Igor", "Konrad", "Anna", "Boris", "Elena", "Franz", "Mila", "Sergej"};
	static ref TStringArray s_Names = {"Viktor", "Birgit", "Igor", "Konrad", "Anna", "Boris", "Elena", "Franz", "Mila", "Sergej"};
	// Sichtbare/konfigurierte Zeilen als Slot-Index-Liste, Reihenfolge =
	// Anzeige-Reihenfolge. Start: nur ein NPC; [+ NPC] haengt den kleinsten
	// unbenutzten Slot an, X entfernt die Zeile (mindestens eine bleibt).
	static ref array<int> s_VisibleSlots = {0};
	// Modellwahl ZWEISTUFIG: erst Provider, dann Modell. Praefix = Backend
	// (resolve_backend): ohne = Anthropic Max-Plan, api/ = Anthropic-API,
	// openai/ google/ xai/ = claude-code-router, local/ = llama-server.
	// Modelle 2026-08-23 gegen die Provider-Doku aktualisiert (Anthropic: Opus 5
	// seit 24.07.; OpenAI: GPT-5.6 Sol/Terra/Luna; Google: Gemini 3.6 Flash GA;
	// xAI: Grok 4.6, die 4.20-Varianten sind retired und redirecten auf 4.3).
	static ref TStringArray s_Providers = {"Anthropic", "OpenAI", "Google", "xAI", "Local"};
	static ref TStringArray s_AnthropicModels = {"sonnet", "haiku", "opus", "claude-fable-5", "claude-opus-5", "claude-sonnet-5", "claude-opus-4-8", "api/sonnet", "api/haiku", "api/opus"};
	static ref TStringArray s_AnthropicLabels = {"Sonnet (auto)", "Haiku 4.5", "Opus (auto)", "Fable 5", "Opus 5", "Sonnet 5", "Opus 4.8", "Sonnet (API)", "Haiku (API)", "Opus (API)"};
	static ref TStringArray s_OpenAIModels = {"openai/gpt-5.6-sol", "openai/gpt-5.6-terra", "openai/gpt-5.6-luna", "openai/gpt-5.5", "openai/gpt-5.4", "openai/gpt-5.4-mini"};
	static ref TStringArray s_OpenAILabels = {"GPT-5.6 Sol", "GPT-5.6 Terra", "GPT-5.6 Luna", "GPT-5.5", "GPT-5.4", "GPT-5.4-mini"};
	static ref TStringArray s_GoogleModels = {"google/gemini-3.6-flash", "google/gemini-3.5-flash", "google/gemini-3.5-flash-lite", "google/gemini-3.1-pro", "google/gemini-3.1-pro-preview"};
	// Labels OHNE "Gemini"/"Grok"-Praefix: die Provider-Spalte zeigt schon Google/xAI,
	// das spart Breite in der engen Modell-Spalte.
	static ref TStringArray s_GoogleLabels = {"3.6 Flash", "3.5 Flash", "3.5 Lite", "3.1 Pro", "3.1 Pro prev"};
	static ref TStringArray s_XaiModels = {"xai/grok-4.6", "xai/grok-4.5", "xai/grok-4.3"};
	static ref TStringArray s_XaiLabels = {"4.6", "4.5", "4.3"};
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

	// Anzahl der NPC-Slots. Einzige Wahrheit fuer alle Schleifen - der geplante
	// Umbau auf dynamische Slots muss dann nur noch hier ansetzen.
	static int SlotCount()
	{
		return s_AgentIds.Count();
	}

	// Index sicher in [0, count) zwingen. Schuetzt BuildCommand vor
	// Out-of-Bounds, wenn Label- und Werte-Listen mal ungleich lang sind.
	static int ClampIdx(int v, int count)
	{
		if (v < 0 || v >= count)
			return 0;
		return v;
	}

	// Parallel-Arrays muessen gleich lang sein (Label-Liste treibt den Index,
	// gelesen wird die Werte-Liste). Bei Pflegefehlern laut werden.
	protected static void CheckPair(string what, int a, int b)
	{
		if (a != b)
			Print("[IsuArena] WARNUNG: Listenlaengen ungleich (" + what + "): " + a.ToString() + " vs " + b.ToString());
	}
	static ref TStringArray s_PersonaKeys = {"jaeger", "bauer", "sanitaeter", "exmilitaer", "kampfmaschine"};
	static ref TStringArray s_PersonaLabels = {"Hunter", "Farmer", "Medic", "Ex-military", "Fighter"};
	// Loadout-Wahl pro Slot (Phase 4): Index 0 = Rollen-Default (KEIN ld:-
	// Segment, agents.json entscheidet wie bisher). Die Dateien liegen in
	// mod/loadouts/ und werden vom Supervisor beim Start nach
	// ExpansionMod/Loadouts gespiegelt (ensure_loadouts). Immer BASIS-Namen
	// ohne _Winter - die Winter-Variante waehlt der Supervisor selbst.
	static ref TStringArray s_LoadoutFiles = {"", "IsuPresetScout.json", "IsuPresetAssault.json", "IsuPresetMedic.json", "IsuPresetSniper.json", "IsuViktorLoadout.json", "IsuIgorLoadout.json", "IsuKonradLoadout.json", "IsuSurvivorLoadout.json"};
	// Index 0 "(keep)" = bisheriges Verhalten: Inventar der letzten Runde
	// kommt zurueck (Restore/Adopt), beim allerersten Spawn Rollen-Loadout.
	// Jede andere Wahl = FRISCH mit diesem Loadout equippen (Supervisor setzt
	// --fresh-loadout, Alt-Koerper/Snapshot werden verworfen).
	static ref TStringArray s_LoadoutLabels = {"(keep)", "Scout", "Assault", "Medic", "Sniper", "Hunter", "Farmer", "Military", "Survivor"};
	static ref array<int> s_LoadoutIdx = {0, 0, 0, 0, 0, 0, 0, 0, 0, 0};
	// ElevenLabs-Stimmen (Name = Teilstring, discord_voice loest ihn gegen das
	// Konto auf; unbekannte fallen sicher auf die Default-Stimme zurueck). Index
	// 0-3 = die bisherigen Defaults pro Slot, danach die aktuellen ElevenLabs-
	// Standardstimmen (in jedem Konto vorhanden), multilingual nutzbar.
	static ref TStringArray s_VoiceNames = {"Helmut - German Epic", "Sarah", "George", "Liam", "Aria", "Roger", "Laura", "Charlie", "Callum", "River", "Charlotte", "Alice", "Matilda", "Will", "Jessica", "Eric", "Chris", "Brian", "Daniel", "Lily", "Bill", "Adam"};
	// Kurz-Labels fuer Kopf + Liste (Spalte schmal). Index-gleich zu s_VoiceNames;
	// gesendet wird s_VoiceNames (volle Aufloesung gegen ElevenLabs), angezeigt s_VoiceLabels.
	static ref TStringArray s_VoiceLabels = {"Helmut", "Sarah", "George", "Liam", "Aria", "Roger", "Laura", "Charlie", "Callum", "River", "Charlotte", "Alice", "Matilda", "Will", "Jessica", "Eric", "Chris", "Brian", "Daniel", "Lily", "Bill", "Adam"};
	// Default-Stimme je Slot (Index in s_VoiceNames): Viktor=Helmut, Birgit=Sarah,
	// Igor=George, Konrad=Liam - genau die bisherigen agents.json-Stimmen;
	// Zusatz-Slots kriegen die naechsten ElevenLabs-Standardstimmen.
	static ref array<int> s_VoiceIdx = {0, 1, 2, 3, 4, 5, 6, 7, 8, 9};
	// Ausgabe-Sprache der NPC. Codes MUESSEN mit run_agent.LANG_NAMES uebereinstimmen.
	// Labels bewusst ASCII (EnforceScript-Datei-Encoding sicher).
	static ref TStringArray s_LangCodes = {"de", "en", "fr", "es", "it", "pt", "nl", "pl", "ru", "uk", "tr", "sv", "cs", "da", "fi", "el", "ro", "hu", "no", "hr", "sk", "ja", "ko", "zh", "ar", "hi", "fil"};
	static ref TStringArray s_LangLabels = {"Deutsch", "English", "Francais", "Espanol", "Italiano", "Portugues", "Nederlands", "Polski", "Russian", "Ukrainian", "Turkce", "Svenska", "Cestina", "Dansk", "Suomi", "Greek", "Romana", "Magyar", "Norsk", "Hrvatski", "Slovak", "Japanese", "Korean", "Chinese", "Arabic", "Hindi", "Filipino"};
	static ref array<int> s_LangIdx = {0, 0, 0, 0, 0, 0, 0, 0, 0, 0};   // alle Default Deutsch
	static ref array<bool> s_Enabled = {true, true, true, true, true, true, true, true, true, true};
	// Default-Tiering: Viktor=Sonnet, Birgit=Haiku, Igor=Haiku, Konrad=Sonnet;
	// Zusatz-Slots starten guenstig auf Haiku.
	// s_ProviderIdx = gewaehlter Provider je Slot (0=Anthropic), s_ModelIdx =
	// Modell-Index INNERHALB des Providers (s_AnthropicModels: sonnet=0, haiku=1).
	static ref array<int> s_ProviderIdx = {0, 0, 0, 0, 0, 0, 0, 0, 0, 0};
	static ref array<int> s_ModelIdx = {0, 1, 1, 0, 1, 1, 1, 1, 1, 1};
	static ref array<int> s_PersonaIdx = {0, 2, 1, 3, 4, 0, 1, 2, 3, 4};
	// Gesinnung/Modus der Runde: 0 = Neutral (co-op), 1 = Hostile (BR),
	// 2 = Free (Survival). Geht als "hostile:<n>" an den Supervisor
	// (Schnittstelle: Werte 0/1 verhalten sich exakt wie der alte Bool).
	static int s_Mode = 0;
	static float s_CampX = 4233.7;
	static float s_CampZ = 8512.2;
	static ref array<int> s_IdleValues = {60, 120, 180, 300};
	static int s_IdleIdx = 1;
	static ref array<int> s_TurnValues = {6, 10, 15, 20, 0};
	static int s_TurnsIdx = 1;
	static bool s_Mic = true;
	static bool s_GroupSpawn = false;   // false = getrennt spawnen, true = eng als Gruppe
	static bool s_ComicChat = true;     // true = Comic-Sprechblasen ueber NPC-Koepfen (Client-HUD)
	// Squad-Uebersichts-Panel (IsuSquadHud, Client) als Dreifach-Schalter:
	// 0 = OFF, 1 = LEFT (bisherige Position links), 2 = RIGHT (rechts oben,
	// Default - weicht dem Discord-Overlay links oben aus).
	static int s_SquadHudMode = 2;
	// Mission/Event der Runde. Die IDs MUESSEN mit den Dateien in
	// arena/missions/*.json uebereinstimmen (Konvention wie s_LangCodes ==
	// LANG_NAMES); Index 0 = "none" schickt KEIN mission-Segment mit (alter
	// Start-Fluss, rueckwaertskompatibel).
	static ref TStringArray s_MissionIds = {"none", "birgit", "horde"};
	static ref TStringArray s_MissionLabels = {"No mission", "Mission: Birgit", "Event: Horde"};
	static int s_MissionIdx = 0;
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
	// Igor gruen, Konrad blau; danach rosa, violett, rot, hellcyan, oliv,
	// graublau fuer die Zusatz-Slots. Auch fuer die Namensschilder im Spiel
	// gedacht, damit Menue und Kopf-Tag dieselbe Farbe tragen.
	static ref array<float> s_ColR = {0.94, 0.36, 0.59, 0.22, 0.85, 0.62, 0.85, 0.35, 0.65, 0.55};
	static ref array<float> s_ColG = {0.62, 0.79, 0.77, 0.54, 0.35, 0.40, 0.30, 0.75, 0.70, 0.60};
	static ref array<float> s_ColB = {0.15, 0.65, 0.35, 0.87, 0.55, 0.85, 0.25, 0.85, 0.30, 0.75};

	// Direktsteuerung im Spiel: frei waehlbare Tasten aus einer kollisionsarmen
	// Liste (die meisten Tasten sind von Spiel und Mods belegt) plus Zielmodus.
	// MissionGameplay.OnKeyPress liest s_KeyStopIdx/s_KeyGotoIdx/s_TargetAll.
	static ref array<int> s_SafeKeyCodes = {KeyCode.KC_NUMPAD5, KeyCode.KC_NUMPAD0, KeyCode.KC_NUMPAD1, KeyCode.KC_NUMPAD2, KeyCode.KC_NUMPAD3, KeyCode.KC_NUMPAD4, KeyCode.KC_NUMPAD6, KeyCode.KC_NUMPAD7, KeyCode.KC_NUMPAD8, KeyCode.KC_NUMPAD9, KeyCode.KC_DECIMAL, KeyCode.KC_DIVIDE, KeyCode.KC_MULTIPLY, KeyCode.KC_SUBTRACT, KeyCode.KC_ADD, KeyCode.KC_DELETE, KeyCode.KC_PRIOR, KeyCode.KC_NEXT};
	static ref array<string> s_SafeKeyLabels = {"Num 5", "Num 0", "Num 1", "Num 2", "Num 3", "Num 4", "Num 6", "Num 7", "Num 8", "Num 9", "Num ,", "Num /", "Num *", "Num -", "Num +", "Del", "PgUp", "PgDn"};
	static int s_KeyStopIdx = 0;   // Default: Num 5
	static int s_KeyGotoIdx = 1;   // Default: Num 0
	static int s_KeyRadialIdx = 10; // Default: Num , (Radialmenue oeffnen)
	static bool s_TargetAll = false;

	// Feld-Typen der Dropdowns (IsuDropdown.SetTags) - ApplyIfItem schaltet
	// darueber, statt alle Dropdowns durchzuprobieren.
	static const int DD_PROVIDER = 0;
	static const int DD_MODEL = 1;
	static const int DD_ROLE = 2;
	static const int DD_VOICE = 3;
	static const int DD_LANG = 4;
	static const int DD_IDLE = 5;
	static const int DD_TURNS = 6;
	static const int DD_KEYSTOP = 7;
	static const int DD_KEYGOTO = 8;
	static const int DD_KEYRADIAL = 9;
	static const int DD_MISSION = 10;
	static const int DD_LOADOUT = 11;

	static const float ROW_PITCH = 52.0;   // Zeilenabstand im RowsHost (40 Karte + 12 Luft)

	protected ref array<ref IsuArenaRow> m_Rows;
	protected Widget m_RowsHost;             // Kind des ArenaScroll, traegt die Zeilen
	protected ButtonWidget m_BtnAddNpc;
	protected TextWidget m_RowCountText;
	protected ButtonWidget m_BtnMode;
	protected ButtonWidget m_BtnSpawn;
	protected ButtonWidget m_BtnTarget;
	protected ButtonWidget m_BtnMic;
	protected ButtonWidget m_BtnComic;
	protected ButtonWidget m_BtnHud;
	protected ButtonWidget m_BtnOrch;
	protected ButtonWidget m_BtnPatrol;
	protected ButtonWidget m_BtnCamp;
	protected ButtonWidget m_BtnStart;
	protected ButtonWidget m_BtnStop;
	protected ButtonWidget m_BtnClose;
	protected TextWidget m_StatusText;
	protected ImageWidget m_StatusDot;
	protected ImageWidget m_StatusPill;
	protected TextWidget m_CostText;
	protected TextWidget m_ModeNote;
	protected ImageWidget m_Background;      // Klick darauf schliesst offene Listen
	protected string m_LastStatusRaw;        // Dirty-Check: Update() nur bei Aenderung
	protected float m_StartSentAt = -100.0;  // Entprellung des Start-Buttons
	protected float m_LiveAccum;             // Takt der Live-Spalte (0.5 s)

	// Globale Dropdowns (Zeilen-Dropdowns leben in m_Rows)
	protected ref IsuDropdown m_DdIdle;
	protected ref IsuDropdown m_DdTurns;
	protected ref IsuDropdown m_DdKeyStop;
	protected ref IsuDropdown m_DdKeyGoto;
	protected ref IsuDropdown m_DdKeyRadial;
	protected ref IsuDropdown m_DdMission;   // Mission/Event (ersetzt den festen "Mission: Birgit"-Knopf)
	protected ref array<ref IsuDropdown> m_GlobalDds;   // Idle/Turns/Keys/Mission
	protected ref array<ref IsuDropdown> m_All;   // globale + Zeilen-Dropdowns (nach jedem BuildRows neu)
	protected IsuDropdown m_OpenDd;          // das gerade offene Dropdown (haelt das Popup)
	protected Widget m_DdPopup;              // das EINE geteilte Listen-Panel aller Dropdowns

	override Widget Init()
	{
		// Reentrance-Guard: ein zweiter Init() wuerde den kompletten Widget-Baum
		// (~1100 Widgets) neu erzeugen, ohne den alten freizugeben.
		if (layoutRoot)
			return layoutRoot;

		layoutRoot = GetGame().GetWorkspace().CreateWidgets("IsuVoice/GUI/isu_arena_menu.layout");
		m_Background = ImageWidget.Cast(layoutRoot.FindAnyWidget("Background"));
		m_DdPopup = layoutRoot.FindAnyWidget("DdPopup");
		if (!m_DdPopup)
			Print("[IsuArena] DdPopup fehlt im Layout - Dropdowns koennen nicht oeffnen.");

		// Pflege-Asserts: Label-Listen treiben die Indizes, Werte-Listen werden
		// gelesen - Laengen-Drift faellt sonst erst beim START-Klick auf.
		CheckPair("AnthropicModels/Labels", s_AnthropicModels.Count(), s_AnthropicLabels.Count());
		CheckPair("OpenAIModels/Labels", s_OpenAIModels.Count(), s_OpenAILabels.Count());
		CheckPair("GoogleModels/Labels", s_GoogleModels.Count(), s_GoogleLabels.Count());
		CheckPair("XaiModels/Labels", s_XaiModels.Count(), s_XaiLabels.Count());
		CheckPair("LocalModels/Labels", s_LocalModels.Count(), s_LocalLabels.Count());
		CheckPair("VoiceNames/Labels", s_VoiceNames.Count(), s_VoiceLabels.Count());
		CheckPair("LangCodes/Labels", s_LangCodes.Count(), s_LangLabels.Count());
		CheckPair("PersonaKeys/Labels", s_PersonaKeys.Count(), s_PersonaLabels.Count());
		CheckPair("LoadoutFiles/Labels", s_LoadoutFiles.Count(), s_LoadoutLabels.Count());
		CheckPair("SafeKeyCodes/Labels", s_SafeKeyCodes.Count(), s_SafeKeyLabels.Count());
		CheckPair("MissionIds/Labels", s_MissionIds.Count(), s_MissionLabels.Count());

		m_RowsHost = layoutRoot.FindAnyWidget("RowsHost");
		m_BtnAddNpc = ButtonWidget.Cast(layoutRoot.FindAnyWidget("BtnAddNpc"));
		m_RowCountText = TextWidget.Cast(layoutRoot.FindAnyWidget("RowCountText"));
		if (!m_RowsHost)
			Print("[IsuArena] RowsHost fehlt im Layout - keine NPC-Zeilen moeglich.");
		m_BtnMode = ButtonWidget.Cast(layoutRoot.FindAnyWidget("BtnMode"));
		m_BtnSpawn = ButtonWidget.Cast(layoutRoot.FindAnyWidget("BtnSpawn"));
		m_BtnTarget = ButtonWidget.Cast(layoutRoot.FindAnyWidget("BtnTarget"));
		m_BtnMic = ButtonWidget.Cast(layoutRoot.FindAnyWidget("BtnMic"));
		m_BtnComic = ButtonWidget.Cast(layoutRoot.FindAnyWidget("BtnComic"));
		m_BtnHud = ButtonWidget.Cast(layoutRoot.FindAnyWidget("BtnHud"));
		m_BtnOrch = ButtonWidget.Cast(layoutRoot.FindAnyWidget("BtnOrch"));
		m_BtnPatrol = ButtonWidget.Cast(layoutRoot.FindAnyWidget("BtnPatrol"));
		m_BtnCamp = ButtonWidget.Cast(layoutRoot.FindAnyWidget("BtnCamp"));
		m_BtnStart = ButtonWidget.Cast(layoutRoot.FindAnyWidget("BtnStart"));
		m_BtnStop = ButtonWidget.Cast(layoutRoot.FindAnyWidget("BtnStop"));
		m_BtnClose = ButtonWidget.Cast(layoutRoot.FindAnyWidget("BtnClose"));
		m_StatusText = TextWidget.Cast(layoutRoot.FindAnyWidget("StatusText"));
		m_StatusDot = ImageWidget.Cast(layoutRoot.FindAnyWidget("StatusDot"));
		m_StatusPill = ImageWidget.Cast(layoutRoot.FindAnyWidget("StatusPill"));
		m_CostText = TextWidget.Cast(layoutRoot.FindAnyWidget("CostText"));
		m_ModeNote = TextWidget.Cast(layoutRoot.FindAnyWidget("ModeNote"));

		// Globale Dropdowns (Slot -1). Lange Listen (Tasten) zweispaltig.
		m_GlobalDds = new array<ref IsuDropdown>();
		m_DdIdle = MakeGlobalDd("IdleHead", IdleLabels(), s_IdleIdx, 1, 0, DD_IDLE);
		m_DdTurns = MakeGlobalDd("TurnsHead", TurnLabels(), s_TurnsIdx, 1, 0, DD_TURNS);
		m_DdKeyStop = MakeGlobalDd("KeyStopHead", s_SafeKeyLabels, s_KeyStopIdx, 2, 130, DD_KEYSTOP);
		m_DdKeyGoto = MakeGlobalDd("KeyGotoHead", s_SafeKeyLabels, s_KeyGotoIdx, 2, 130, DD_KEYGOTO);
		m_DdKeyRadial = MakeGlobalDd("KeyRadialHead", s_SafeKeyLabels, s_KeyRadialIdx, 2, 130, DD_KEYRADIAL);
		// Mission/Event: der Kopf sitzt in der Start-Zeile am unteren Menuerand -
		// das Popup klappt dort automatisch nach oben (Y-Clamping in OpenIn).
		m_DdMission = MakeGlobalDd("MissionHead", s_MissionLabels, s_MissionIdx, 1, 0, DD_MISSION);

		// NPC-Zeilen dynamisch aufbauen (eine je Eintrag in s_VisibleSlots)
		BuildRows();
		return layoutRoot;
	}

	protected IsuDropdown MakeGlobalDd(string headName, TStringArray items, int current, int cols, float colW, int kind)
	{
		IsuDropdown dd = new IsuDropdown();
		dd.Setup(layoutRoot, headName, items, current, cols, colW);
		dd.SetTags(kind, -1);
		m_GlobalDds.Insert(dd);
		return dd;
	}

	protected IsuDropdown MakeRowDd(Widget rowRoot, string headName, TStringArray items, int current, int cols, float colW, int kind, int slot)
	{
		IsuDropdown dd = new IsuDropdown();
		dd.Setup(rowRoot, headName, items, current, cols, colW);
		dd.SetTags(kind, slot);
		// Tabellen-Spalten sind eng - Pfeil aus (Klick oeffnet trotzdem).
		dd.SetShowArrow(false);
		return dd;
	}

	// Zeile fuer einen Slot bauen (Widgets aus isu_arena_row.layout, Dropdowns
	// im Zeilen-Subtree aufloesen - FindAnyWidget sucht nur dort).
	protected ref IsuArenaRow MakeRow(int slot)
	{
		IsuArenaRow row = new IsuArenaRow();
		row.m_Slot = slot;
		row.m_Root = GetGame().GetWorkspace().CreateWidgets("IsuVoice/GUI/isu_arena_row.layout", m_RowsHost);
		if (!row.m_Root)
		{
			Print("[IsuArena] isu_arena_row.layout konnte nicht geladen werden.");
			return row;
		}
		row.m_Root.SetFlags(WidgetFlags.EXACTPOS | WidgetFlags.EXACTSIZE);
		row.m_Card = ImageWidget.Cast(row.m_Root.FindAnyWidget("RowCard"));
		row.m_Accent = ImageWidget.Cast(row.m_Root.FindAnyWidget("RowAccent"));
		row.m_BtnAgent = ButtonWidget.Cast(row.m_Root.FindAnyWidget("BtnAgent"));
		row.m_EditName = EditBoxWidget.Cast(row.m_Root.FindAnyWidget("EditName"));
		row.m_BtnRemove = ButtonWidget.Cast(row.m_Root.FindAnyWidget("BtnRemove"));
		row.m_Live = TextWidget.Cast(row.m_Root.FindAnyWidget("LiveText"));
		if (row.m_EditName)
			row.m_EditName.SetText(s_Names[slot]);
		row.m_DdProvider = MakeRowDd(row.m_Root, "ProviderHead", s_Providers, s_ProviderIdx[slot], 1, 190, DD_PROVIDER, slot);
		row.m_DdModel = MakeRowDd(row.m_Root, "ModelHead", ProviderModelLabels(s_ProviderIdx[slot]), s_ModelIdx[slot], 1, 230, DD_MODEL, slot);
		row.m_DdRole = MakeRowDd(row.m_Root, "RoleHead", s_PersonaLabels, s_PersonaIdx[slot], 1, 160, DD_ROLE, slot);
		row.m_DdLoadout = MakeRowDd(row.m_Root, "LoadoutHead", s_LoadoutLabels, s_LoadoutIdx[slot], 1, 160, DD_LOADOUT, slot);
		row.m_DdVoice = MakeRowDd(row.m_Root, "VoiceHead", s_VoiceLabels, s_VoiceIdx[slot], 3, 180, DD_VOICE, slot);
		row.m_DdLang = MakeRowDd(row.m_Root, "LangHead", s_LangLabels, s_LangIdx[slot], 3, 160, DD_LANG, slot);
		return row;
	}

	// Alle Zeilen neu aufbauen (nach [+ NPC], X oder beim Init). Baut auch
	// m_All (globale + Zeilen-Dropdowns) neu auf.
	protected void BuildRows()
	{
		CloseAllDropdowns();
		if (m_Rows)
		{
			ReadNames();
			for (int i = 0; i < m_Rows.Count(); i++)
			{
				if (m_Rows[i])
					m_Rows[i].Destroy();
			}
		}
		m_Rows = new array<ref IsuArenaRow>();
		if (m_RowsHost && s_VisibleSlots)
		{
			for (int v = 0; v < s_VisibleSlots.Count(); v++)
				m_Rows.Insert(MakeRow(s_VisibleSlots[v]));
		}
		RelayoutRows();

		m_All = new array<ref IsuDropdown>();
		for (int g = 0; g < m_GlobalDds.Count(); g++)
			m_All.Insert(m_GlobalDds[g]);
		for (int r = 0; r < m_Rows.Count(); r++)
		{
			IsuArenaRow row = m_Rows[r];
			if (!row)
				continue;
			m_All.Insert(row.m_DdProvider);
			m_All.Insert(row.m_DdModel);
			m_All.Insert(row.m_DdRole);
			m_All.Insert(row.m_DdLoadout);
			m_All.Insert(row.m_DdVoice);
			m_All.Insert(row.m_DdLang);
		}
		UpdateLabels();
	}

	// Zeilen im RowsHost stapeln und die Content-Hoehe fuers Scrollen setzen.
	protected void RelayoutRows()
	{
		if (!m_RowsHost || !m_Rows)
			return;
		for (int i = 0; i < m_Rows.Count(); i++)
		{
			if (m_Rows[i] && m_Rows[i].m_Root)
			{
				m_Rows[i].m_Root.SetPos(0, i * ROW_PITCH);
				m_Rows[i].m_Root.SetSize(1584, 40);
			}
		}
		float hostW, hostH;
		m_RowsHost.GetSize(hostW, hostH);
		float contentH = m_Rows.Count() * ROW_PITCH;
		if (contentH < 208)
			contentH = 208;
		m_RowsHost.SetSize(hostW, contentH);
		if (m_RowCountText)
			m_RowCountText.SetText(m_Rows.Count().ToString() + " / " + SlotCount().ToString());
	}

	// [+ NPC]: kleinsten noch unbenutzten Slot anhaengen.
	protected void AddNextSlot()
	{
		if (s_VisibleSlots.Count() >= SlotCount())
			return;
		for (int slot = 0; slot < SlotCount(); slot++)
		{
			if (s_VisibleSlots.Find(slot) < 0)
			{
				s_VisibleSlots.Insert(slot);
				BuildRows();
				return;
			}
		}
	}

	// X an einer Zeile: Slot aus der sichtbaren Liste nehmen (mindestens
	// eine Zeile bleibt stehen).
	protected void RemoveSlot(int slot)
	{
		if (s_VisibleSlots.Count() <= 1)
			return;
		int at = s_VisibleSlots.Find(slot);
		if (at < 0)
			return;
		// RemoveOrdered statt Remove: Remove tauscht das letzte Element an die
		// Stelle und wuerde die Anzeige-Reihenfolge der Zeilen zerwuerfeln.
		s_VisibleSlots.RemoveOrdered(at);
		BuildRows();
	}

	// Zeile zu einem Slot (fuer ApplyIfItem-Folgeaktionen wie Modell-Rebuild).
	protected IsuArenaRow RowForSlot(int slot)
	{
		if (!m_Rows)
			return null;
		for (int i = 0; i < m_Rows.Count(); i++)
		{
			if (m_Rows[i] && m_Rows[i].m_Slot == slot)
				return m_Rows[i];
		}
		return null;
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

	// Taetigkeits-Kuerzel der Live-Spalte (actionId aus dem Nametag-RPC)
	protected static string ActionVerb(int actionId)
	{
		if (actionId == 0)
			return "fighting";
		if (actionId == 1)
			return "looting";
		if (actionId == 2)
			return "following";
		if (actionId == 3)
			return "moving";
		return "waiting";
	}

	// Nametag-Eintrag eines NPC ueber den (effektiven) Namen finden - der
	// Store ist NetID-basiert, das Menue kennt nur Namen.
	protected static IsuAgentTag FindTagByName(string name)
	{
		foreach (string key, IsuAgentTag t : IsuNametagStore.s_Agents)
		{
			if (t && t.name == name)
				return t;
		}
		return null;
	}

	// Live-Spalte: HP + Taetigkeit aus dem Nametag-Store (dieselbe Quelle wie
	// Namensschilder/Squad-HUD, kein zusaetzlicher RPC-Verkehr). Kein Eintrag
	// im Store = (noch) nicht gespawnt -> der UpdateLabels-Text bleibt stehen.
	protected void UpdateLiveCells()
	{
		if (!m_Rows)
			return;
		for (int i = 0; i < m_Rows.Count(); i++)
		{
			IsuArenaRow row = m_Rows[i];
			if (!row || !row.m_Live)
				continue;
			int idx = row.m_Slot;
			if (!s_Enabled[idx])
				continue;
			IsuAgentTag tag = FindTagByName(s_Names[idx]);
			if (tag)
			{
				row.m_Live.SetText(tag.hp.ToString() + " HP  " + ActionVerb(tag.actionId));
				row.m_Live.SetColor(ARGBF(1.0, s_ColR[idx], s_ColG[idx], s_ColB[idx]));
			}
		}
	}

	override void Update(float timeslice)
	{
		super.Update(timeslice);

		// Live-Spalte im 0,5-s-Takt - unabhaengig vom Status-Dirty-Check unten.
		m_LiveAccum += timeslice;
		if (m_LiveAccum >= 0.5)
		{
			m_LiveAccum = 0;
			UpdateLiveCells();
		}

		if (!m_StatusText)
			return;

		// Supervisor-Status -> Ampel. Die Schluesselwoerter kommen aus
		// arena_supervisor.write_status (LAEUFT/GESTOPPT/FEHLER/WARTE/...).
		// Dirty-Check: der Status aendert sich selten, Substring/SetText/SetColor
		// jeden Frame waeren nur Garbage.
		string raw = IsuArenaStatusStore.s_Text;
		if (raw == m_LastStatusRaw)
			return;
		m_LastStatusRaw = raw;
		string disp = raw;
		// Kosten-Anhang (" | cost 1.23 USD", vom Supervisor alle ~30 s) in das
		// eigene Feld unten abspalten, damit die Status-Pill lesbar bleibt.
		int ci = disp.IndexOf(" | cost ");
		if (ci > -1)
		{
			if (m_CostText)
				m_CostText.SetText("Round " + disp.Substring(ci + 3, disp.Length() - ci - 3));
			disp = disp.Substring(0, ci);
		}
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

	protected string CleanName(string raw, int idx)
	{
		string n = raw;
		n.Replace("|", "");
		n.Replace(":", "");
		n.Replace("\"", "");
		// Zeilenumbrueche zerlegen das zeilenbasierte Dateiprotokoll des
		// Supervisors; Laenge kappen, damit kein Roman im RPC landet.
		n.Replace("\n", "");
		n.Replace("\r", "");
		n = n.Trim();
		if (n.Length() > 24)
			n = n.Substring(0, 24);
		if (n == "")
			n = s_DefaultNames[idx];
		return n;
	}

	// Tasten-Konflikt sichtbar machen: frueher wurde still umgebogen und der
	// Spieler wunderte sich, warum eine andere Taste im Kopf stand (X4).
	protected void KeyConflictHint(int wanted, int got)
	{
		if (wanted < 0 || wanted >= s_SafeKeyLabels.Count())
			return;
		if (got < 0 || got >= s_SafeKeyLabels.Count())
			return;
		IsuArenaStatusStore.s_Text = s_SafeKeyLabels[wanted] + " already in use - using " + s_SafeKeyLabels[got];
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
		m_OpenDd = null;
		if (!m_All)
			return;
		for (int d = 0; d < m_All.Count(); d++)
		{
			if (m_All[d])
				m_All[d].Close();
		}
	}

	// EditBoxen der sichtbaren Zeilen -> s_Names (persistente Statics), bereinigt
	protected void ReadNames()
	{
		if (!m_Rows)
			return;
		for (int i = 0; i < m_Rows.Count(); i++)
		{
			IsuArenaRow nrow = m_Rows[i];
			if (!nrow || !nrow.m_EditName)
				continue;
			s_Names[nrow.m_Slot] = CleanName(nrow.m_EditName.GetText(), nrow.m_Slot);
			nrow.m_EditName.SetText(s_Names[nrow.m_Slot]);
		}
	}

	// Nur die Nicht-Dropdown-Anzeigen aktualisieren (Karten, Akzente, Live,
	// Toggle-Buttons). Die Dropdown-Koepfe pflegen ihren Text selbst.
	protected void UpdateLabels()
	{
		if (m_Rows)
		{
			for (int i = 0; i < m_Rows.Count(); i++)
			{
				IsuArenaRow row = m_Rows[i];
				if (!row)
					continue;
				int idx = row.m_Slot;
				string state = "OFF";
				if (s_Enabled[idx])
					state = "ON";
				if (row.m_BtnAgent)
					row.m_BtnAgent.SetText(state);
				// Dropdown-Koepfe der Zeile ausgrauen, wenn der Slot OFF ist
				// (Klicks sind dann in OnClick gesperrt).
				int headCol = ARGB(255, 255, 255, 255);
				if (!s_Enabled[idx])
					headCol = ARGB(255, 105, 110, 118);
				if (row.m_DdProvider)
					row.m_DdProvider.SetHeadTextColor(headCol);
				if (row.m_DdModel)
					row.m_DdModel.SetHeadTextColor(headCol);
				if (row.m_DdRole)
					row.m_DdRole.SetHeadTextColor(headCol);
				if (row.m_DdLoadout)
					row.m_DdLoadout.SetHeadTextColor(headCol);
				if (row.m_DdVoice)
					row.m_DdVoice.SetHeadTextColor(headCol);
				if (row.m_DdLang)
					row.m_DdLang.SetHeadTextColor(headCol);
				if (s_Enabled[idx])
				{
					if (row.m_Accent)
						row.m_Accent.SetColor(ARGBF(1.0, s_ColR[idx], s_ColG[idx], s_ColB[idx]));
					if (row.m_Card)
						row.m_Card.SetColor(ARGBF(0.95, 0.075, 0.090, 0.120));
					if (row.m_Live)
					{
						row.m_Live.SetText("selected");
						row.m_Live.SetColor(ARGBF(1.0, s_ColR[idx], s_ColG[idx], s_ColB[idx]));
					}
				}
				else
				{
					if (row.m_Accent)
						row.m_Accent.SetColor(ARGBF(0.35, s_ColR[idx], s_ColG[idx], s_ColB[idx]));
					if (row.m_Card)
						row.m_Card.SetColor(ARGBF(0.85, 0.045, 0.050, 0.065));
					if (row.m_Live)
					{
						row.m_Live.SetText("off");
						row.m_Live.SetColor(ARGBF(1.0, 0.45, 0.45, 0.48));
					}
				}
			}
		}

		if (m_BtnMode)
		{
			if (s_Mode == 1)
				m_BtnMode.SetText("Hostile");
			else if (s_Mode == 2)
				m_BtnMode.SetText("Free");
			else
				m_BtnMode.SetText("Neutral");
		}
		if (m_ModeNote)
		{
			if (s_Mode == 1)
				m_ModeNote.SetText("(battle royale)");
			else if (s_Mode == 2)
				m_ModeNote.SetText("(survival)");
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
		if (m_BtnHud)
		{
			if (s_SquadHudMode == 1)
				m_BtnHud.SetText("LEFT");
			else if (s_SquadHudMode == 2)
				m_BtnHud.SetText("RIGHT");
			else
				m_BtnHud.SetText("OFF");
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
		// Hostile-Wahl soll kein unbemerktes BR ausloesen (Warnlabel nur
		// bei Modus 1; Free ist harmlos, kriegt aber ein eigenes Label).
		if (m_BtnStart)
		{
			if (s_Mode == 1)
				m_BtnStart.SetText("START: HOSTILE (BR)");
			else if (s_Mode == 2)
				m_BtnStart.SetText("START: FREE (survival)");
			else
				m_BtnStart.SetText("START (neutral)");
		}
	}

	protected string BuildCommand()
	{
		ReadNames();
		// Protokoll v2: Versions-Tag + Slot-Anzahl (der Supervisor erkennt damit
		// zerrissene/unvollstaendige Kommandos, statt still weniger zu starten).
		string cmd = "start|v:2|count:" + s_VisibleSlots.Count().ToString();
		// Nur die sichtbaren Zeilen mitsenden - nicht angezeigte Slots existieren
		// fuer diese Runde nicht (der Supervisor startet nur gesehene Segmente).
		for (int v = 0; v < s_VisibleSlots.Count(); v++)
		{
			int idx = s_VisibleSlots[v];
			string flag = "0";
			if (s_Enabled[idx])
				flag = "1";
			TStringArray pmids = ProviderModelIds(s_ProviderIdx[idx]);
			string modelId = pmids.Get(ClampIdx(s_ModelIdx[idx], pmids.Count()));
			// Alle Indizes clampen: die Label-Listen treiben die Auswahl, gelesen
			// werden die Werte-Listen - Laengen-Drift darf hier nicht crashen.
			string persona = s_PersonaKeys[ClampIdx(s_PersonaIdx[idx], s_PersonaKeys.Count())];
			string voice = s_VoiceNames[ClampIdx(s_VoiceIdx[idx], s_VoiceNames.Count())];
			string lang = s_LangCodes[ClampIdx(s_LangIdx[idx], s_LangCodes.Count())];
			// Stamm-Slots 0-3 im Alt-Format (aeltere Supervisor verstehen sie
			// weiter), Zusatz-Slots als v2-"npc:"-Segment mit freier ID.
			if (idx <= 3)
				cmd = cmd + "|" + s_AgentIds[idx] + ":" + flag + ":" + modelId + ":" + persona + ":" + s_Names[idx];
			else
				cmd = cmd + "|npc:" + s_AgentIds[idx] + ":" + flag + ":" + modelId + ":" + persona + ":" + s_Names[idx];
			// Stimme + Sprache als eigenes, entkoppeltes Segment (der Name oben
			// darf ':' enthalten - darum NICHT ins Slot-Tupel quetschen).
			cmd = cmd + "|av:" + s_AgentIds[idx] + ":" + voice + ":" + lang;
			// Loadout-Wahl (Phase 4): nur mitschicken, wenn NICHT Rollen-Default
			// (Index 0) - fehlendes Segment = altes Verhalten (agents.json).
			int ldi = ClampIdx(s_LoadoutIdx[idx], s_LoadoutFiles.Count());
			if (ldi > 0)
				cmd = cmd + "|ld:" + s_AgentIds[idx] + ":" + s_LoadoutFiles[ldi];
		}
		string mic = "0";
		if (s_Mic)
			mic = "1";
		// Modus-Encoding (Schnittstelle zum Supervisor): das bestehende Feld
		// "hostile" traegt jetzt drei Werte - 0 = Neutral (co-op),
		// 1 = Hostile (BR), 2 = Free (Survival). 0/1 unveraendert alt.
		cmd = cmd + "|hostile:" + s_Mode.ToString();
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
		// Mission/Event nur mitschicken, wenn eine gewaehlt ist ("No mission" =
		// klassischer Start ohne Segment). Format unveraendert "mission:<id>" -
		// der Supervisor-Parser bleibt rueckwaertskompatibel.
		if (s_MissionIdx > 0 && s_MissionIdx < s_MissionIds.Count())
			cmd = cmd + "|mission:" + s_MissionIds.Get(s_MissionIdx);
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
	// Nur das offene Dropdown hat Items - ueber dessen Tags (Feld + Slot) wird
	// direkt verzweigt, statt alle Dropdowns durchzuprobieren.
	protected bool ApplyIfItem(Widget w)
	{
		if (!m_OpenDd)
			return false;
		int sel = m_OpenDd.ItemIndex(w);
		if (sel < 0)
			return false;
		int slot = m_OpenDd.GetSlotTag();
		int kind = m_OpenDd.GetKindTag();
		IsuDropdown dd = m_OpenDd;
		m_OpenDd = null;

		switch (kind)
		{
			case DD_PROVIDER:
			{
				s_ProviderIdx[slot] = sel;
				s_ModelIdx[slot] = 0;
				dd.SelectByItem(sel);
				// Modell-Dropdown der Zeile mit den Modellen des neuen Providers fuellen
				IsuArenaRow prow = RowForSlot(slot);
				if (prow && prow.m_DdModel)
					prow.m_DdModel.Rebuild(ProviderModelLabels(sel), 0);
				return true;
			}
			case DD_MODEL:
			{
				s_ModelIdx[slot] = sel;
				dd.SelectByItem(sel);
				return true;
			}
			case DD_ROLE:
			{
				s_PersonaIdx[slot] = sel;
				dd.SelectByItem(sel);
				return true;
			}
			case DD_LOADOUT:
			{
				s_LoadoutIdx[slot] = sel;
				dd.SelectByItem(sel);
				return true;
			}
			case DD_VOICE:
			{
				s_VoiceIdx[slot] = sel;
				dd.SelectByItem(sel);
				return true;
			}
			case DD_LANG:
			{
				s_LangIdx[slot] = sel;
				dd.SelectByItem(sel);
				return true;
			}
			case DD_IDLE:
			{
				s_IdleIdx = sel;
				dd.SelectByItem(sel);
				return true;
			}
			case DD_TURNS:
			{
				s_TurnsIdx = sel;
				dd.SelectByItem(sel);
				return true;
			}
			case DD_KEYSTOP:
			{
				int rks = ResolveFreeKey(sel, s_KeyGotoIdx, s_KeyRadialIdx);
				if (rks != sel)
					KeyConflictHint(sel, rks);
				s_KeyStopIdx = rks;
				dd.SelectByItem(rks);
				return true;
			}
			case DD_KEYGOTO:
			{
				int rkg = ResolveFreeKey(sel, s_KeyStopIdx, s_KeyRadialIdx);
				if (rkg != sel)
					KeyConflictHint(sel, rkg);
				s_KeyGotoIdx = rkg;
				dd.SelectByItem(rkg);
				return true;
			}
			case DD_KEYRADIAL:
			{
				int rkr = ResolveFreeKey(sel, s_KeyStopIdx, s_KeyGotoIdx);
				if (rkr != sel)
					KeyConflictHint(sel, rkr);
				s_KeyRadialIdx = rkr;
				dd.SelectByItem(rkr);
				return true;
			}
			case DD_MISSION:
			{
				s_MissionIdx = sel;
				dd.SelectByItem(sel);
				return true;
			}
		}
		dd.SelectByItem(sel);
		return true;
	}

	// Klick auf die freie Menueflaeche (Background) schliesst offene Listen.
	// OnClick reicht dafuer nicht: die Engine routet es nur fuer ButtonWidgets,
	// das ImageWidget liefert nur OnMouseButtonDown (ignorepointer 0 im Layout).
	override bool OnMouseButtonDown(Widget w, int x, int y, int button)
	{
		if (w == m_Background)
		{
			CloseAllDropdowns();
			return true;
		}
		// Rechtsklick auf Camp: zurueck auf den Karten-Default (der Supervisor
		// ersetzt die Default-Koordinate je Karte durch ihren Landpunkt).
		if (w == m_BtnCamp && button == MouseState.RIGHT)
		{
			s_CampX = 4233.7;
			s_CampZ = 8512.2;
			IsuArenaStatusStore.s_Text = "Camp reset to map default.";
			UpdateLabels();
			return true;
		}
		return super.OnMouseButtonDown(w, x, y, button);
	}

	override bool OnClick(Widget w, int x, int y, int button)
	{
		if (!m_All)
			return super.OnClick(w, x, y, button);
		// Dropdown-Koepfe: oeffnen/schliessen (immer nur einer offen, alle teilen
		// sich das eine DdPopup-Panel)
		for (int d = 0; d < m_All.Count(); d++)
		{
			if (m_All[d] && m_All[d].IsHead(w))
			{
				// OFF-Zeilen sind gesperrt: Dropdowns wuerden Wirkung
				// suggerieren, die der Start gar nicht sendet (X8).
				int hslot = m_All[d].GetSlotTag();
				if (hslot >= 0 && hslot < s_Enabled.Count() && !s_Enabled[hslot])
					return true;
				bool wasOpen = m_All[d].IsOpen();
				CloseAllDropdowns();
				if (!wasOpen)
				{
					m_All[d].OpenIn(m_DdPopup, layoutRoot);
					m_OpenDd = m_All[d];
				}
				return true;
			}
		}
		// Klick auf einen Options-Eintrag -> uebernehmen + schliessen
		if (ApplyIfItem(w))
			return true;
		// alles andere schliesst offene Listen
		CloseAllDropdowns();

		// Zeilen-Buttons: ON/OFF-Toggle und X (Zeile entfernen)
		if (m_Rows)
		{
			for (int ri = 0; ri < m_Rows.Count(); ri++)
			{
				IsuArenaRow crow = m_Rows[ri];
				if (!crow)
					continue;
				if (w == crow.m_BtnAgent)
				{
					s_Enabled[crow.m_Slot] = !s_Enabled[crow.m_Slot];
					UpdateLabels();
					return true;
				}
				if (w == crow.m_BtnRemove)
				{
					RemoveSlot(crow.m_Slot);
					return true;
				}
			}
		}
		if (w == m_BtnAddNpc)
		{
			AddNextSlot();
			return true;
		}

		if (w == m_BtnMode)
		{
			// Zyklisch Neutral -> Hostile -> Free -> Neutral
			s_Mode = s_Mode + 1;
			if (s_Mode > 2)
				s_Mode = 0;
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
		if (w == m_BtnHud)
		{
			// Zyklisch OFF -> LEFT -> RIGHT -> OFF
			s_SquadHudMode = s_SquadHudMode + 1;
			if (s_SquadHudMode > 2)
				s_SquadHudMode = 0;
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
			}
			UpdateLabels();
			return true;
		}
		if (w == m_BtnStart)
		{
			// Entprellung: Doppelklick darf nicht zwei Supervisor-Requests
			// ausloesen (jeder Start faehrt sonst die komplette Startsequenz an).
			float now = GetGame().GetTickTime();
			if (now - m_StartSentAt < 3.0)
				return true;
			// Plausibilitaet: ohne aktive sichtbare Zeile gibt es nichts zu starten.
			bool anyOn = false;
			for (int e = 0; e < s_VisibleSlots.Count(); e++)
			{
				if (s_Enabled[s_VisibleSlots[e]])
					anyOn = true;
			}
			if (!anyOn)
			{
				IsuArenaStatusStore.s_Text = "No NPC enabled - nothing to start.";
				return true;
			}
			m_StartSentAt = now;
			// Mission/Event haengt BuildCommand selbst an (Dropdown, IDs siehe
			// s_MissionIds): "birgit" = Rettungsmission (Spawn Lukow, Rally Kopa,
			// Banditen-Patrouille), "horde" = Horden-Event, "none" = ohne Segment.
			SendCommand(BuildCommand());
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

	// Schwebende Namensschilder + Squad-Uebersicht jeden Frame aktualisieren
	// (Client). Render-/Projektionslogik in IsuNameplateHud bzw. IsuSquadHud;
	// beide lesen denselben IsuNametagStore (kein zusaetzlicher RPC-Verkehr).
	override void OnUpdate(float timeslice)
	{
		super.OnUpdate(timeslice);
		if (GetGame() && !GetGame().IsDedicatedServer())
		{
			IsuNameplateHud.Tick();
			IsuSquadHud.Tick();
		}
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
		bool aimHit = IsuNpcCommand.AimRaycast(hitPos, aimedObj);
		IsuRadialMenu.s_HasAimPos = aimHit;
		if (aimHit)
		{
			IsuRadialMenu.s_AimX = hitPos[0];
			IsuRadialMenu.s_AimZ = hitPos[2];
		}
		// Anvisiertes loses Item (fuer den "Hol das"-Zweig des "Go to"-Chips).
		string aimItemClass;
		if (IsuNpcCommand.AimRaycastItem(aimItemClass))
		{
			IsuRadialMenu.s_HasAimItem = true;
			IsuRadialMenu.s_AimItemClass = aimItemClass;
		}
		else
		{
			IsuRadialMenu.s_HasAimItem = false;
			IsuRadialMenu.s_AimItemClass = "";
		}
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
