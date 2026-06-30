// IsuVoice — Client-Receiver: spielt Voice-Lines positional ab und kennt
// die Agentennamen.
//
// Der Server (IsuSurvivor-Mod) sendet RPCs:
//   ISU_RPC_PLAY_VOICE: SoundSet-Name + Position -> 3D-Sound abspielen
//   ISU_RPC_NAMETAG:    NetworkID + Name         -> IsuNametagStore
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

// Letzter vom Server gefunkter Supervisor-Status (Anzeige im Arena-Menue)
class IsuArenaStatusStore
{
	static string s_Text = "unbekannt (Server meldet sich gleich)";
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
}

// Vom Server gemeldete Agenten (Schluessel: "low_high" der NetworkID).
class IsuNametagStore
{
	static ref map<string, ref IsuAgentTag> s_Agents = new map<string, ref IsuAgentTag>();
	static bool s_LoggedFirst;
	static bool s_LoggedIntent;

	static IsuAgentTag GetOrCreate(int low, int high)
	{
		string key = low.ToString() + "_" + high.ToString();
		IsuAgentTag t;
		if (!s_Agents.Find(key, t))
		{
			t = new IsuAgentTag();
			t.low = low;
			t.high = high;
			t.intent = "";
			t.speech = "";
			s_Agents.Set(key, t);
		}
		return t;
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
	}

	static void UpdateIntent(int low, int high, string intent)
	{
		if (!s_LoggedIntent)
		{
			s_LoggedIntent = true;
			Print("[IsuVoice] Erster Intent-RPC empfangen: " + intent);
		}
		IsuAgentTag t = GetOrCreate(low, high);
		t.intent = intent;
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
