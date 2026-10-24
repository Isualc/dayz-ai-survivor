// IsuVoice — Client-Receiver: spielt Voice-Lines positional ab und kennt
// die Agentennamen.
//
// Der Server (IsuSurvivor-Mod) sendet RPCs:
//   ISU_RPC_PLAY_VOICE: SoundSet-Name + Position -> 3D-Sound abspielen
//   ISU_RPC_NAMETAG:    NetworkID + Name         -> IsuNametagStore
//   ISU_RPC_AGENTPOS:   NetworkID + Position     -> IsuNametagStore (Squad-HUD
//                       fuer Agenten ausserhalb der Netzwerk-Bubble)
// Die Namen fliessen in GetDisplayName() der eAI - damit zeigen die
// Expansion-Nametags (NameTagsSettings: ShowNPCTags=1) beim Anvisieren
// "Viktor" statt des Skin-Namens ("Naomi").

const int ISU_RPC_PLAY_VOICE = 0x49535556;   // "ISUV"
const int ISU_RPC_NAMETAG = 0x49535554;      // "ISUT"
const int ISU_RPC_ARENA_CMD = 0x49535541;    // "ISUA" - Menue -> Server
const int ISU_RPC_ARENA_STATUS = 0x49535553; // "ISUS" - Supervisor-Status -> Menue
const int ISU_RPC_NPC_CMD = 0x49535543;      // "ISUC" - Spieler-Direktbefehl -> Server
const int ISU_RPC_INTENT = 0x4953554E;       // "ISUN" - Gedanke/Absicht -> Client (Nameplate)
const int ISU_RPC_SAY = 0x4953554F;          // "ISUO" - gesagter Text -> Client (Comic-Sprechblase)
const int ISU_RPC_UI_LANGUAGE_REQUEST = 0x4953554C; // "ISUL" - client asks for server UI language
const int ISU_RPC_UI_LANGUAGE = 0x49535555;         // "ISUU" - server replies only to requesting client
// Muss zu RPC_AGENTPOS in IsuSurvivor/isubridge.c passen. Param3<int, int, vector>
// (low, high, Position), ungarantiert, alle 3 s mit dem Nametag-Takt. Ein
// alter Server sendet ihn nicht -> Squad-HUD zeigt dann wie bisher nur
// replizierte (sichtbare) Agenten.
const int ISU_RPC_AGENTPOS = 0x49535550;     // "ISUP" - Agenten-Position -> Client (Squad-HUD "fern")

// Letzter vom Server gefunkter Supervisor-Status (Anzeige im Arena-Menue)
class IsuArenaStatusStore
{
	static string s_Text = "";   // Empty = menu's localized initial state.
}

// Spieler-Direktbefehle an die NPCs (Stopp / geh dorthin). Loest das Ziel per
// Kamera-Raycast auf und schickt einen kompakten Befehlsstring an den Server
// (IsuBridge.OnPlayerNpcCommand routet ihn). Client-seitig.
class IsuNpcCommand
{
	static void Send(string line)
	{
		PlayerBase pb = PlayerBase.Cast(GetGame().GetPlayer());
		if (!pb)
			return;
		Param1<string> data = new Param1<string>(line);
		GetGame().RPCSingleParam(pb, ISU_RPC_NPC_CMD, data, true);
	}

	// Raycast 120 m von der Kamera nach vorn. hitPos = Bodentreffer,
	// aimedObj = erster getroffener lebender eAIBase (sonst null).
	static bool AimRaycast(out vector hitPos, out Object aimedObj)
	{
		hitPos = vector.Zero;
		aimedObj = null;

		vector from = GetGame().GetCurrentCameraPosition();
		vector dir = GetGame().GetCurrentCameraDirection();
		vector to = from + dir * 120.0;

		vector cPos;
		vector cDir;
		int cComp;
		set<Object> results = new set<Object>();
		PlayerBase pb = PlayerBase.Cast(GetGame().GetPlayer());

		bool hit = DayZPhysics.RaycastRV(from, to, cPos, cDir, cComp, results, null, pb, true, false, ObjIntersectView);
		if (!hit)
			return false;

		hitPos = cPos;
		foreach (Object o : results)
		{
			eAIBase ai = eAIBase.Cast(o);
			if (ai && ai.IsAlive())
			{
				aimedObj = o;
				break;
			}
		}
		return true;
	}

	// Classname des ersten anvisierten LOSEN Bodenitems (nicht attached, nicht in
	// einem Inventar). Fuer den "Hol das"-Zweig des Radials: zeigt der Spieler auf
	// Loot, holt und traegt der NPC es. Leer, wenn kein Item getroffen wurde.
	static bool AimRaycastItem(out string itemClass)
	{
		itemClass = "";
		vector from = GetGame().GetCurrentCameraPosition();
		vector dir = GetGame().GetCurrentCameraDirection();
		vector to = from + dir * 120.0;

		vector cPos;
		vector cDir;
		int cComp;
		set<Object> results = new set<Object>();
		PlayerBase pb = PlayerBase.Cast(GetGame().GetPlayer());

		if (!DayZPhysics.RaycastRV(from, to, cPos, cDir, cComp, results, null, pb, true, false, ObjIntersectView))
			return false;

		foreach (Object o : results)
		{
			ItemBase it = ItemBase.Cast(o);
			if (it && !it.GetHierarchyParent())
			{
				itemClass = it.GetType();
				return true;
			}
		}
		return false;
	}

	// Sofort stehenbleiben. all -> alle Agenten; sonst anvisierter NPC, und
	// falls keiner anvisiert ist, der naechste zur Spielerposition.
	static void SendHalt(bool all)
	{
		PlayerBase pb = PlayerBase.Cast(GetGame().GetPlayer());
		if (!pb)
			return;

		if (all)
		{
			Send("halt|all");
			return;
		}

		vector hitPos;
		Object aimedObj;
		AimRaycast(hitPos, aimedObj);
		eAIBase ai = eAIBase.Cast(aimedObj);
		if (ai)
		{
			int low, high;
			ai.GetNetworkID(low, high);
			Send("halt|single|" + low.ToString() + "|" + high.ToString());
		}
		else
		{
			vector pp = pb.GetPosition();
			Send("halt|nearest|" + pp[0].ToString() + "|" + pp[2].ToString());
		}
	}

	// Geh dorthin (anvisierter Bodenpunkt). all -> alle; sonst der naechste
	// Agent (beim Zielen auf den Boden ist meist kein NPC anvisiert).
	static void SendGoto(bool all)
	{
		PlayerBase pb = PlayerBase.Cast(GetGame().GetPlayer());
		if (!pb)
			return;

		vector hitPos;
		Object aimedObj;
		if (!AimRaycast(hitPos, aimedObj))
			return;

		string xz = hitPos[0].ToString() + "|" + hitPos[2].ToString();

		if (all)
		{
			Send("goto|all|" + xz);
			return;
		}

		eAIBase ai = eAIBase.Cast(aimedObj);
		if (ai)
		{
			int low, high;
			ai.GetNetworkID(low, high);
			Send("goto|single|" + low.ToString() + "|" + high.ToString() + "|" + xz);
		}
		else
		{
			vector pp = pb.GetPosition();
			Send("goto|nearest|" + pp[0].ToString() + "|" + pp[2].ToString() + "|" + xz);
		}
	}

	// Generischer Sender mit Ziel-Selektor fuers Radialmenue. hasTarget ->
	// "single|<low>|<high>" (der beim Oeffnen anvisierte NPC), sonst
	// "nearest|<px>|<pz>" (Spielerposition). extra wird angehaengt (z.B.
	// Spielername bei follow, oder "<x>|<z>" Zielpunkt bei goto/comehere).
	static void SendTargeted(string action, bool hasTarget, int low, int high, string extra)
	{
		PlayerBase pb = PlayerBase.Cast(GetGame().GetPlayer());
		if (!pb)
			return;

		string sel;
		if (hasTarget)
		{
			sel = "single|" + low.ToString() + "|" + high.ToString();
		}
		else
		{
			vector pp = pb.GetPosition();
			sel = "nearest|" + pp[0].ToString() + "|" + pp[2].ToString();
		}

		string line = action + "|" + sel;
		if (extra != "")
			line = line + "|" + extra;
		Send(line);
	}
}

// Ein Agenten-Eintrag fuer Namensschild + Gedanken-HUD.
class IsuAgentTag
{
	int low;
	int high;
	string name;
	int hp;        // 0..100
	int slot;      // 0..3 Identitaetsfarbe, 7 = unbekannt
	int actionId;  // 0=kaempft 1=lootet 2=folgt 3=geht 4=wartet
	string intent; // einzeilige Absicht (Gedanken-HUD), spaeter gefuellt
	string speech;     // letzte gesagte Zeile (Comic-Sprechblase)
	int speechExpiry;  // GetGame().GetTime()-Zeitpunkt, ab dem die Blase verschwindet
	vector pos;        // letzte per RPC_AGENTPOS gefunkte Position (auch ausserhalb der Bubble)
	bool hasPos;       // pos gueltig; ein alter Server ohne RPC_AGENTPOS setzt es nie
	int lastRpcMs;     // GetGame().GetTime() des letzten Server-RPCs (Nametag/Intent/Position)
}

// Vom Server gemeldete Agenten (Schluessel: "low_high" der NetworkID).
class IsuNametagStore
{
	static ref map<string, ref IsuAgentTag> s_Agents = new map<string, ref IsuAgentTag>();
	static bool s_LoggedFirst;
	static bool s_LoggedIntent;

	// Ablauf eines Eintrags: so lange ohne Server-RPC gilt der Agent als
	// despawnt bzw. nicht mehr gefuehrt. Der Server funkt jeden lebenden
	// Agenten alle 3 s an jeden Spieler (ohne Distanzfilter); 10 s Puffer
	// ueberbruecken verlorene ungarantierte Positions-Pakete und kurze
	// Server-Ruckler. Leichen raeumt das Remove-Signal weiterhin sofort ab.
	static const int STALE_MS = 10000;

	static IsuAgentTag GetOrCreate(int low, int high)
	{
		string key = low.ToString() + "_" + high.ToString();
		IsuAgentTag t;
		if (!s_Agents.Find(key, t))
		{
			t = new IsuAgentTag();
			t.low = low;
			t.high = high;
			// Neutral anlegen: kommt Position/Intent/Sprechblase vor dem
			// Nametag-RPC an, darf der Eintrag keinen festen Pool-Slot (0 =
			// Viktor) belegen und kein namenloses Schild zeigen. Update()
			// setzt Name und Slot beim ersten Nametag-RPC.
			t.name = "";
			t.slot = 7;
			t.actionId = 4;
			t.intent = "";
			t.speech = "";
			t.pos = vector.Zero;
			t.hasPos = false;
			t.lastRpcMs = GetGame().GetTime();
			s_Agents.Set(key, t);
		}
		return t;
	}

	// true = Eintrag abgelaufen (kein RPC seit STALE_MS). Eine deutlich
	// negative Spanne heisst: Missionsuhr neu gestartet (Reconnect), der
	// Eintrag stammt aus der vorigen Mission (s_Agents ist statisch).
	static bool IsStale(IsuAgentTag t, int nowMs)
	{
		if (!t)
			return true;
		int ageMs = nowMs - t.lastRpcMs;
		if (ageMs < -1000)
			return true;
		return ageMs > STALE_MS;
	}

	static void Update(int low, int high, string name, int hp, int slot, int actionId)
	{
		if (!s_LoggedFirst)
		{
			s_LoggedFirst = true;
			Print("[IsuVoice] Erster Nametag-RPC empfangen: " + name + " (id " + low.ToString() + "/" + high.ToString() + ")");
		}
		IsuAgentTag t = GetOrCreate(low, high);
		t.name = name;
		t.hp = hp;
		t.slot = slot;
		t.actionId = actionId;
		t.lastRpcMs = GetGame().GetTime();

		// Gleichnamiger Eintrag unter einer anderen NetID = alter Koerper
		// desselben Agenten (Respawn/Adopt). Der Server meldet nur lebende
		// registrierte Koerper und erzwingt eindeutige Namen. Kam das Remove-
		// Signal der Leiche nicht an (Register() des Nachfolgers lief vor dem
		// naechsten 3-s-Tick), wuerde der Alt-Eintrag sonst Pool-Slot und
		// Squad-Zeile des neuen Koerpers belegen. Erst sammeln, dann loeschen
		// (Map nicht waehrend der Iteration veraendern). Namenlose Eintraege
		// (nur Intent/Sprechblase/Position) bleiben unberuehrt.
		if (name == "")
			return;
		string myKey = low.ToString() + "_" + high.ToString();
		array<string> dupes = new array<string>();
		foreach (string dk, IsuAgentTag dt : s_Agents)
		{
			if (dk != myKey && dt && dt.name == name)
				dupes.Insert(dk);
		}
		for (int ri = 0; ri < dupes.Count(); ri++)
			s_Agents.Remove(dupes[ri]);
	}

	// Absicht IMMER uebernehmen, auch leer: der Server funkt eine geleerte
	// intent-Datei als "" - die Gedankenzeile leert sich dann, statt die
	// letzte (evtl. Vor-Tod-) Absicht stehen zu lassen.
	static void UpdateIntent(int low, int high, string intent)
	{
		if (!s_LoggedIntent && intent != "")
		{
			// Nur die erste NICHT-leere Absicht loggen (Diagnose: welche
			// Zeile kam beim Start an), leere kommen jetzt jeden Takt.
			s_LoggedIntent = true;
			Print("[IsuVoice] Erster Intent-RPC empfangen: " + intent);
		}
		IsuAgentTag t = GetOrCreate(low, high);
		t.intent = intent;
		t.lastRpcMs = GetGame().GetTime();
	}

	// Agenten-Position (RPC_AGENTPOS): kommt auch fuer Agenten ausserhalb der
	// Netzwerk-Bubble (~1 km), deren Objekt der Client nicht kennt. Das
	// Squad-HUD zeigt sie damit weiter mit Distanz und Zusatz "(fern)".
	static void UpdatePos(int low, int high, vector pos)
	{
		IsuAgentTag t = GetOrCreate(low, high);
		t.pos = pos;
		t.hasPos = true;
		t.lastRpcMs = GetGame().GetTime();
	}

	// Comic-Sprechblase: gesagte Zeile mit Ablaufzeit (~8 s) speichern.
	// Danach blendet das HUD sie ueber SPEECH_FADE_MS langsam aus.
	static void UpdateSpeech(int low, int high, string text)
	{
		IsuAgentTag t = GetOrCreate(low, high);
		t.speech = text;
		t.speechExpiry = GetGame().GetTime() + 8000;
	}

	// Namensschild zu einer NetworkID entfernen (Server meldet tote Koerper).
	static void Remove(int low, int high)
	{
		string key = low.ToString() + "_" + high.ToString();
		IsuAgentTag t;
		if (s_Agents.Find(key, t))
			s_Agents.Remove(key);
	}

	static string NameFor(Object obj)
	{
		if (!obj)
			return "";
		int low, high;
		obj.GetNetworkID(low, high);
		string key = low.ToString() + "_" + high.ToString();
		IsuAgentTag t;
		if (s_Agents.Find(key, t))
			return t.name;
		return "";
	}
}

// Agentenname statt Skin-Name ("Naomi") in allen Anzeigen, die
// GetDisplayName nutzen - insbesondere den Expansion-Nametags.
modded class eAIBase
{
	override string GetDisplayName()
	{
		string isuName = IsuNametagStore.NameFor(this);
		if (isuName != "")
			return isuName;
		return super.GetDisplayName();
	}
}

modded class PlayerBase
{
	override void OnRPC(PlayerIdentity sender, int rpc_type, ParamsReadContext ctx)
	{
		super.OnRPC(sender, rpc_type, ctx);

		// Nur auf Clients verarbeiten
		if (GetGame().IsDedicatedServer())
			return;

		if (rpc_type == ISU_RPC_UI_LANGUAGE)
		{
			// Native RPC transport routes client calls to the server and server
			// replies to clients. Accept only our local player as the target;
			// no assumption about an undocumented sender-null convention.
			if (!GetGame().IsClient() || GetGame().GetPlayer() != this)
				return;
			Param1<string> uiLanguage = new Param1<string>("");
			if (ctx.Read(uiLanguage))
				IsuUiText.AcceptServerLanguage(uiLanguage.param1);
			return;
		}

		if (rpc_type == ISU_RPC_PLAY_VOICE)
		{
			Param2<string, vector> data = new Param2<string, vector>("", vector.Zero);
			if (!ctx.Read(data))
				return;

			EffectSound sound = SEffectManager.PlaySound(data.param1, data.param2);
			if (sound)
				sound.SetSoundAutodestroy(true);
			return;
		}

		if (rpc_type == ISU_RPC_NAMETAG)
		{
			// Param5: low, high, name, packed(hp*8+slot), actionId
			Param5<int, int, string, int, int> tag = new Param5<int, int, string, int, int>(0, 0, "", 0, 4);
			if (ctx.Read(tag))
			{
				int packed = tag.param4;
				if (packed < 0)
				{
					// Remove-Signal vom Server: Namensschild der Leiche abraeumen.
					IsuNametagStore.Remove(tag.param1, tag.param2);
				}
				else
				{
					int slot = packed % 8;
					int hp = packed / 8;
					IsuNametagStore.Update(tag.param1, tag.param2, tag.param3, hp, slot, tag.param5);
				}
			}
			return;
		}

		if (rpc_type == ISU_RPC_AGENTPOS)
		{
			// Param3: low, high, Position (Server-Takt 3 s, ungarantiert)
			Param3<int, int, vector> ap = new Param3<int, int, vector>(0, 0, vector.Zero);
			if (ctx.Read(ap))
				IsuNametagStore.UpdatePos(ap.param1, ap.param2, ap.param3);
			return;
		}

		if (rpc_type == ISU_RPC_INTENT)
		{
			Param3<int, int, string> ip = new Param3<int, int, string>(0, 0, "");
			if (ctx.Read(ip))
				IsuNametagStore.UpdateIntent(ip.param1, ip.param2, ip.param3);
			return;
		}

		if (rpc_type == ISU_RPC_SAY)
		{
			Param3<int, int, string> sp = new Param3<int, int, string>(0, 0, "");
			if (ctx.Read(sp))
				IsuNametagStore.UpdateSpeech(sp.param1, sp.param2, sp.param3);
			return;
		}

		if (rpc_type == ISU_RPC_ARENA_STATUS)
		{
			Param1<string> status = new Param1<string>("");
			if (ctx.Read(status))
				IsuArenaStatusStore.s_Text = status.param1;
			return;
		}
	}
}
