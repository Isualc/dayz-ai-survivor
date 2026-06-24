// IsuSurvivor — Bridge zwischen DayZ-Server und Agent-Daemon. Protokoll v0.2.
//
// Mailbox-Protokoll:
//   Daemon schreibt  $profile:IsuSurvivor/commands.json  (nur wenn Datei nicht existiert)
//   Mod liest + loescht die Datei im naechsten Tick und fuehrt die Befehle aus
//   Mod schreibt    $profile:IsuSurvivor/state.json      einmal pro Tick (1 s)
//
// Alle Expansion-/Vanilla-APIs gegen Quellcode verifiziert (siehe reference/):
//   Spawn:     eAICommandManagerImpl.SpawnAIEx (CreateObject + ExpansionHumanLoadout.Apply)
//   Bewegung:  eAIGroup.ClearWaypoints/AddWaypoint/SetWaypointBehaviour
//   Aufnahme:  eAIBase.eAI_TakeItemToInventory / eAI_TakeItemToHands
//   Essen:     PlayerBase.Consume(item, amount, EConsumeType.ITEM_SINGLE_TIME)
//              -> Magen; Verdauung via StomachMdfr (laeuft im eAI-ModifiersManager)
//   Kampf:     eAITargetInformation.GetTargetInformation(entity).InsertAI(ai)

class IsuBridge
{
	// Muss zur Konstante in IsuVoice/IsuVoiceReceiver.c passen ("ISUV")
	static const int RPC_PLAY_VOICE = 0x49535556;

	// Muss zu ISU_RPC_NAMETAG in IsuVoice/IsuVoiceReceiver.c passen ("ISUT")
	static const int RPC_NAMETAG = 0x49535554;

	// Gedanken-HUD: aktuelle Absicht eines Agenten an die Clients ("ISUN")
	static const int RPC_INTENT = 0x4953554E;

	// Comic-Sprechblase: gesagter Text eines Agenten an die Clients ("ISUO")
	static const int RPC_SAY = 0x4953554F;

	// Arena: eine Bridge-Instanz pro Agent (Id = Dateisuffix)
	private static ref map<string, ref IsuBridge> s_Instances = new map<string, ref IsuBridge>();

	private string m_Id;
	private string m_Faction;
	private string m_Dir;
	private string m_StateFile;
	private string m_CmdFile;

	private eAIBase m_Npc;

	private int m_Seq;
	private int m_ChatSeq;
	private bool m_Started;

	private ref array<ref IsuChatMsg> m_Chat;
	private ref array<string> m_Errors;

	// Zustand des aktuellen Befehls
	private string m_CmdId;
	private string m_CmdAction;
	private string m_CmdStatus;
	private string m_CmdDetail;
	private vector m_MoveTarget;
	private float m_CmdStartTime;
	private float m_LastProgressTime;
	private float m_BestDist;
	private ItemBase m_PickupItem;
	private bool m_PickupWalking;
	private string m_PickupFilter;        // pickup: Filter merken fuer Neu-Suche
	private EntityAI m_ClaimedItem;       // pickup: von DIESEM Bot beansprucht
	private EntityAI m_EngageTarget;
	private EntityAI m_CorpseTarget;
	private string m_StoreFilter;   // store_container: optionaler Item-Classname-Filter
	private AnimalBase m_HarvestTarget;
	private int m_FactionCheckTicks;
	private bool m_Slinged;            // sling: Waffe geschultert -> Sprinttempo
	private string m_NpcName;
	private string m_Intent;        // letzte vom Gehirn gesetzte Absicht (Gedanken-HUD)
	// Loot-Claims aller Bridge-Instanzen: verhindert, dass mehrere Bots
	// dasselbe Bodenitem anlaufen (das "Tanzen umeinander").
	private static ref map<EntityAI, IsuBridge> s_PickupClaims = new map<EntityAI, IsuBridge>();
	private bool m_Following;
	private bool m_TriedUnstick;
	private ItemBase m_WearPendingItem;   // wear: Anziehen nach Slot-Tausch
	private int m_WearPendingTries;
	private string m_WearDiag;            // wear: Slot-Diagnose fuer Fehler
	private ItemBase m_EquipPendingItem;  // equip_best: Waffe-in-Hand-Retry ueber Ticks
	private int m_EquipPendingTries;

	static IsuBridge GetInstance(string id = "viktor")
	{
		IsuBridge instance;
		if (s_Instances.Find(id, instance))
			return instance;

		instance = new IsuBridge();
		s_Instances.Insert(id, instance);
		instance.Start(id);
		return instance;
	}

	// Neue Slots automatisch erkennen: jede commands_<id>.json erzeugt eine Instanz
	static void TickDiscovery()
	{
		string fileName;
		FileAttr attr;
		FindFileHandle handle = FindFile("$profile:IsuSurvivor/commands_*.json", fileName, attr, FindFileFlags.ALL);
		if (handle)
		{
			bool more = true;
			while (more && fileName != "")
			{
				// commands_<id>.json -> id
				string id = fileName;
				id.Replace("commands_", "");
				id.Replace(".json", "");
				if (id != "")
					GetInstance(id);
				more = FindNextFile(handle, fileName, attr);
			}
			CloseFindFile(handle);
		}
	}

	// Chat an alle Agenten verteilen (Spieler-Nachrichten); Agenten-eigene
	// Aussagen werden in CmdSay direkt mit Reichweite zugestellt
	static void OnChatAll(int channel, string sender, string text)
	{
		// Stammt die Nachricht von einem unserer Agenten? Dann nicht doppeln.
		foreach (string instId, IsuBridge inst : s_Instances)
		{
			if (inst.m_NpcName == sender)
				return;
		}

		// Spieler-Chat erreicht ALLE Agenten, egal wie weit weg ("Funkgeraet").
		// Die 60-m-Realismus-Grenze gilt nur fuer Agent-zu-Agent-Gespraeche
		// (DeliverAgentChat) - sonst verliert man weggelaufene NPCs komplett.
		foreach (string id2, IsuBridge inst2 : s_Instances)
		{
			inst2.OnChat(channel, sender, text);
		}
	}

	void Start(string id)
	{
		if (m_Started)
			return;
		m_Started = true;

		m_Id = id;
		m_Faction = "civilian";

		m_Dir = "$profile:IsuSurvivor";
		m_StateFile = m_Dir + "/state_" + id + ".json";
		m_CmdFile = m_Dir + "/commands_" + id + ".json";

		if (!FileExist(m_Dir))
			MakeDirectory(m_Dir);

		m_Chat = new array<ref IsuChatMsg>();
		m_Errors = new array<string>();
		m_CmdStatus = "idle";
		m_CmdId = "";
		m_CmdAction = "";
		m_CmdDetail = "";
		m_Following = false;
		m_Intent = "";

		// Default-Anzeigename aus der Id ("viktor" -> "Viktor")
		m_NpcName = id;
		if (m_NpcName.Length() > 0)
		{
			string first = m_NpcName.Get(0);
			first.ToUpper();
			m_NpcName = first + m_NpcName.Substring(1, m_NpcName.Length() - 1);
		}

		GetGame().GetCallQueue(CALL_CATEGORY_SYSTEM).CallLater(Tick, 1000, true);

		Print("[IsuSurvivor] bridge v0.12-coop slot '" + id + "' started");
	}

	void Tick()
	{
		ReadCommands();
		ReadIntent();
		AutoFollowDriver();
		m_FactionCheckTicks++;
		if (m_FactionCheckTicks >= 5)
		{
			m_FactionCheckTicks = 0;
			EnforceFaction();
		}
		UpdateRunningCommand();
		WriteState();
	}

	// Gedanken-HUD: aktuelle Absicht aus intent_<id>.txt lesen (vom MCP-Tool
	// intent() gesetzt). Die Datei wird NICHT geloescht - die Absicht bleibt
	// gueltig bis zur naechsten Setzung. Muster wie TickArenaStatus.
	private void ReadIntent()
	{
		FileHandle fh = OpenFile("$profile:IsuSurvivor/intent_" + m_Id + ".txt", FileMode.READ);
		if (fh == 0)
			return;
		string line;
		FGets(fh, line);
		CloseFile(fh);
		line = line.Trim();
		if (line != "")
			m_Intent = line;
	}

	// Fraktions-Watchdog: Expansions Spiel-Interaktion "Entlassen" wirft den
	// Agenten aus der Spielergruppe und steckt ihn in eine NEUE Gruppe mit
	// Expansion-Default-Fraktion - die kann feindlich sein (Igor fing nach
	// dem Entlassen an, wild zu ballern; 2026-06-12). Deshalb wird die
	// Soll-Fraktion (m_Faction, vom Spawn) alle 5 s erzwungen, sobald der
	// Agent nicht mehr in einer von einem Menschen gefuehrten Gruppe ist.
	private void EnforceFaction()
	{
		if (!m_Npc || !m_Npc.IsAlive())
			return;

		eAIGroup group = m_Npc.GetGroup();
		if (!group)
			return;

		// Menschlich gefuehrte Gruppe (follow aktiv): Fraktion ist Sache des
		// Spielers, nichts anfassen
		PlayerBase leader = PlayerBase.Cast(group.GetLeader());
		if (leader && leader.GetIdentity())
			return;

		// Kein menschlicher Leader mehr: ein Spiel-seitiges "Entlassen" hat
		// das Folgen beendet, ohne dass unser stop_follow lief - Flag syncen
		if (m_Following && !m_Npc.IsInTransport())
			m_Following = false;

		eAIFaction want = CreateFactionByName(m_Faction);
		eAIFaction current = group.GetFaction();
		if (current && current.Type() == want.Type())
			return;

		group.SetFaction(want);
		Print("[IsuSurvivor] Fraktions-Watchdog: Gruppe von '" + m_NpcName + "' zurueck auf '" + m_Faction + "'");
	}

	// Fahrzeug-Regel Teil 1: Sitzt der Agent in einem Fahrzeug mit menschlichem
	// Fahrer, tritt er automatisch dessen Gruppe bei. Damit greift die native
	// eAI-Logik "Mitfahrer bleiben sitzen, solange der Leader im Fahrzeug ist".
	private void AutoFollowDriver()
	{
		if (!m_Npc || !m_Npc.IsAlive())
			return;

		if (!m_Npc.IsInTransport())
		{
			// Ausstiegs-Freigabe zuruecknehmen, sobald er draussen ist
			IsuAgentRegistry.SetVehicleExit(m_Npc, false);
			return;
		}

		if (m_Following)
			return;

		Transport transport = Transport.Cast(m_Npc.GetParent());
		if (!transport)
			return;

		for (int i = 0; i < transport.CrewSize(); i++)
		{
			PlayerBase crew = PlayerBase.Cast(transport.CrewMember(i));
			if (!crew || crew == m_Npc || !crew.GetIdentity())
				continue;

			eAIGroup group = eAIGroup.GetGroupByLeader(crew);
			if (!group)
				continue;

			m_Npc.SetGroup(group);
			m_Following = true;
			Print("[IsuSurvivor] Fahrzeug erkannt - folge Fahrer " + crew.GetIdentity().GetName());
			return;
		}
	}

	// ------------------------------------------------------------------ Chat

	void OnChat(int channel, string sender, string text)
	{
		m_ChatSeq++;

		IsuChatMsg msg = new IsuChatMsg();
		msg.id = m_ChatSeq;
		msg.channel = channel;
		msg.sender = sender;
		msg.text = text;
		msg.uptime = GetGame().GetTickTime();

		m_Chat.Insert(msg);
		while (m_Chat.Count() > 30)
			m_Chat.RemoveOrdered(0);
	}

	// -------------------------------------------------------------- Commands

	private void ReadCommands()
	{
		if (!FileExist(m_CmdFile))
			return;

		IsuCommandFile cmdFile = new IsuCommandFile();
		JsonFileLoader<IsuCommandFile>.JsonLoadFile(m_CmdFile, cmdFile);
		DeleteFile(m_CmdFile);

		if (!cmdFile || !cmdFile.commands)
		{
			LogError("commands.json unlesbar oder leer");
			return;
		}

		foreach (IsuCommand cmd : cmdFile.commands)
		{
			Dispatch(cmd);
		}
	}

	private void Dispatch(IsuCommand cmd)
	{
		if (!cmd || cmd.id == "")
		{
			LogError("Befehl ohne id ignoriert");
			return;
		}

		if (m_CmdStatus == "running")
			LogError("Befehl " + m_CmdId + " durch " + cmd.id + " verdraengt");

		m_CmdId = cmd.id;
		m_CmdAction = cmd.action;
		m_CmdDetail = "";
		ReleaseClaim();
		m_PickupItem = null;
		m_PickupWalking = false;
		m_PickupFilter = "";
		m_EngageTarget = null;
		m_CorpseTarget = null;
		m_HarvestTarget = null;
		m_TriedUnstick = false;
		m_WearPendingItem = null;
		m_WearPendingTries = 0;
		m_EquipPendingItem = null;
		m_EquipPendingTries = 0;
		m_StoreFilter = "";

		switch (cmd.action)
		{
			case "ping":
				m_CmdStatus = "done";
				break;

			case "spawn":
				CmdSpawn(cmd);
				break;

			case "move_to":
				CmdMoveTo(cmd);
				break;

			case "stop":
				CmdStop();
				break;

			case "despawn":
				CmdDespawn();
				break;

			case "pickup":
				CmdPickup(cmd);
				break;

			case "loot_corpse":
				CmdLootContainer(cmd, true);
				break;

			case "harvest":
				CmdHarvest(cmd);
				break;

			case "loot_container":
				CmdLootContainer(cmd, false);
				break;

			case "door":
				CmdDoor(cmd);
				break;

			case "eat":
				CmdConsume(false, cmd.text);
				break;

			case "drink":
				CmdConsume(true, cmd.text);
				break;

			case "equip_best":
				CmdEquipBest();
				break;

			case "equip":
				CmdEquip(cmd);
				break;

			case "wear":
				CmdWear(cmd);
				break;

			case "engage":
				CmdEngage(cmd);
				break;

			case "flee":
				CmdFlee(cmd);
				break;

			case "adopt_nearest":
				CmdAdoptNearest();
				break;

			case "teleport_player":
				CmdTeleportPlayer(cmd);
				break;

			case "spawn_item":
				CmdSpawnItem(cmd);
				break;

			case "spawn_infected":
				CmdSpawnInfected(cmd);
				break;

			case "say":
				CmdSay(cmd);
				break;

			case "bubble":
				CmdBubble(cmd);
				break;

			case "say_voice":
				CmdSayVoice(cmd);
				break;

			case "follow":
				CmdFollow(cmd);
				break;

			case "unfollow":
				CmdUnfollow();
				break;

			case "unstick":
				CmdUnstick();
				break;

			case "vehicle_exit":
				CmdVehicleExit();
				break;

			case "give_item":
				CmdGiveItem(cmd);
				break;

			case "hand_over":
				CmdHandOver(cmd);
				break;

			case "sling":
				CmdSling();
				break;

			case "unsling":
				CmdUnsling();
				break;

			case "regroup":
				CmdRegroup(cmd);
				break;

			case "drop":
				CmdDrop(cmd);
				break;

			case "store_container":
				CmdStore(cmd);
				break;

			case "drink_well":
				CmdDrinkWell();
				break;

			case "fill_container":
				CmdFillContainer();
				break;

			case "consume_item":
				CmdConsumeItem(cmd);
				break;

			case "light_fire":
				CmdLightFire();
				break;

			case "cook":
				CmdCook();
				break;

			case "build_fence_frame":
				CmdBuildFenceFrame();
				break;

			case "clean_weapon":
				CmdCleanWeapon();
				break;

			case "unpack_ammo":
				CmdUnpackAmmo(cmd);
				break;

			default:
				m_CmdStatus = "failed";
				m_CmdDetail = "unbekannte action: " + cmd.action;
				break;
		}

		Print("[IsuSurvivor] cmd " + cmd.id + " (" + cmd.action + ") -> " + m_CmdStatus);
	}

	// ------------------------------------------------------- Basis-Kommandos

	private void CmdSpawn(IsuCommand cmd)
	{
		if (m_Npc && m_Npc.IsAlive())
		{
			m_CmdStatus = "failed";
			m_CmdDetail = "npc existiert bereits";
			return;
		}

		vector pos = ResolvePos(cmd);

		if (cmd.text != "")
			m_NpcName = cmd.text;

		string loadout = cmd.loadout;
		if (loadout == "")
		{
			// Eigenes Default-Loadout: warme Zivilkleidung, kleiner Rucksack,
			// KEINE Zufallswaffen. HumanLoadout.json gab leere/kaputte Waffen
			// ("Geisterwaffen") und nur T-Shirts (Agenten froren).
			loadout = "IsuSurvivorLoadout.json";
		}

		eAIBase ai;
		if (!Class.CastTo(ai, GetGame().CreateObject(eAISurvivor.GetQuasiRandom(), pos)))
		{
			m_CmdStatus = "failed";
			m_CmdDetail = "CreateObject lieferte keine eAIBase - laeuft DayZ-Expansion-AI?";
			return;
		}

		ai.SetPosition(pos);
		ExpansionHumanLoadout.Apply(ai, loadout, true);

		// NPC-Waffen vor Ladehemmung schuetzen: alle Waffen im Loadout auf pristine.
		array<EntityAI> spawnItems = new array<EntityAI>();
		ai.GetInventory().EnumerateInventory(InventoryTraversalType.PREORDER, spawnItems);
		foreach (EntityAI spawnEnt : spawnItems)
		{
			Weapon_Base spawnWpn = Weapon_Base.Cast(spawnEnt);
			if (spawnWpn)
			{
				spawnWpn.SetHealth01("", "Health", 1.0);
				if (spawnWpn.IsJammed())
					spawnWpn.SetJammed(false);
			}
		}

		eAIGroup group = ai.GetGroup();
		if (group)
		{
			// Zivilist: greift Spieler nicht von sich aus an.
			if (cmd.faction != "")
				m_Faction = cmd.faction;
			group.SetFaction(CreateFactionByName(m_Faction));
			group.SetWaypointBehaviour(eAIWaypointBehavior.HALT);
		}

		ai.SetMovementSpeedLimits(2.0, 3.0);

		m_Npc = ai;
		IsuAgentRegistry.Register(ai, m_NpcName);
		// Battle-Royale PER AGENT setzen: jeder Spawn traegt seinen eigenen Modus
		// (br==0 = coop). Kein globaler Schalter -> keine Cross-Contamination,
		// kein Server-Neustart zum Umschalten noetig.
		IsuAgentRegistry.SetBrMode(ai, cmd.br == "1");
		m_CmdStatus = "done";
	}

	private void CmdMoveTo(IsuCommand cmd)
	{
		if (!NpcReadyOnFoot())
			return;

		if (!StartWalk(ResolvePos(cmd)))
			return;

		m_CmdStatus = "running";
	}

	private void CmdStop()
	{
		if (!NpcReady())
			return;

		m_Slinged = false;   // Stehenbleiben beendet den Sprint-Marsch
		// ZUERST aus der Spielergruppe loesen: ein HALT-Wegpunkt in der vom
		// Spieler gefuehrten Gruppe friert sonst ALLE KI dauerhaft ein (eAI
		// macht die erste KI zum Formations-Anker, der Wegpunkt bleibt ewig).
		EnsureOwnGroup();

		eAIGroup group = m_Npc.GetGroup();
		if (group)
		{
			group.ClearWaypoints();
			group.AddWaypoint(m_Npc.GetPosition());
			group.SetWaypointBehaviour(eAIWaypointBehavior.HALT);
		}

		m_Npc.SetMovementSpeedLimits(2.0, 3.0);
		m_CmdStatus = "done";
	}

	// --- Spieler-Direktbefehle ---------------------------------------------
	// Vom Spieler per Taste oder Radialmenue ausgeloest. Umgehen die
	// Gehirn-Schleife (kein Umweg ueber commands_<id>.json), damit Stopp und
	// "geh dorthin" ohne Latenz greifen. Das Gehirn uebernimmt beim naechsten
	// Tick wieder, falls es eine eigene Bewegung will.

	bool MatchesNetworkId(int low, int high)
	{
		if (!m_Npc)
			return false;
		int l, h;
		m_Npc.GetNetworkID(l, h);
		return (l == low && h == high);
	}

	// Sofort stehenbleiben - gleiche Mechanik wie CmdStop.
	void PlayerHalt()
	{
		if (!NpcReady())
			return;
		ReleaseClaim();
		m_PickupItem = null;
		m_PickupWalking = false;
		m_EngageTarget = null;
		m_CorpseTarget = null;
		m_HarvestTarget = null;
		m_CmdId = "player_halt";
		m_CmdAction = "stop";
		CmdStop();
	}

	// Zum angegebenen Punkt gehen. y aus der Bodenhoehe, wenn nicht gesetzt.
	void PlayerGoto(vector pos)
	{
		if (!NpcReadyOnFoot())
			return;
		ReleaseClaim();
		m_PickupItem = null;
		m_PickupWalking = false;
		m_EngageTarget = null;
		m_CorpseTarget = null;
		m_HarvestTarget = null;
		m_CmdId = "player_goto";
		m_CmdAction = "move_to";
		m_CmdDetail = "";
		if (pos[1] <= 0)
			pos[1] = GetGame().SurfaceY(pos[0], pos[2]);
		if (StartWalk(pos))
			m_CmdStatus = "running";
	}

	// Einen NPC per NetworkID ansprechen (Einzelsteuerung)
	static void RouteHalt(int low, int high)
	{
		foreach (string idH, IsuBridge instH : s_Instances)
		{
			if (instH.MatchesNetworkId(low, high))
			{
				instH.PlayerHalt();
				return;
			}
		}
	}

	static void RouteGoto(int low, int high, vector pos)
	{
		foreach (string idG, IsuBridge instG : s_Instances)
		{
			if (instG.MatchesNetworkId(low, high))
			{
				instG.PlayerGoto(pos);
				return;
			}
		}
	}

	// Alle Agenten gemeinsam (Gruppensteuerung)
	static void RouteHaltAll()
	{
		foreach (string idHA, IsuBridge instHA : s_Instances)
			instHA.PlayerHalt();
	}

	static void RouteGotoAll(vector pos)
	{
		foreach (string idGA, IsuBridge instGA : s_Instances)
			instGA.PlayerGoto(pos);
	}

	// Naechster Agent zur Spielerposition (Einzelsteuerung, wenn der Spieler
	// nicht praezise auf einen NPC zielt, z.B. bei "geh dorthin").
	static IsuBridge FindNearestInstance(vector ppos)
	{
		IsuBridge best = null;
		float bestD = 1.0e10;
		foreach (string idN, IsuBridge instN : s_Instances)
		{
			if (!instN.m_Npc || !instN.m_Npc.IsAlive())
				continue;
			float d = vector.Distance(instN.m_Npc.GetPosition(), ppos);
			if (d < bestD)
			{
				bestD = d;
				best = instN;
			}
		}
		return best;
	}

	static void RouteHaltNearest(vector ppos)
	{
		IsuBridge b = FindNearestInstance(ppos);
		if (b)
			b.PlayerHalt();
	}

	static void RouteGotoNearest(vector ppos, vector dest)
	{
		IsuBridge b = FindNearestInstance(ppos);
		if (b)
			b.PlayerGoto(dest);
	}

	// --- Radialmenue-Aktionen: nutzen die bestehenden Cmd*-Handler ueber ein
	// synthetisiertes IsuCommand wieder, mit demselben Inline-Reset wie
	// PlayerHalt/PlayerGoto (umgehen die Gehirn-Schleife). ---

	private void ResetForPlayerCmd()
	{
		ReleaseClaim();
		m_PickupItem = null;
		m_PickupWalking = false;
		m_EngageTarget = null;
		m_CorpseTarget = null;
		m_HarvestTarget = null;
	}

	// Dem ausloesenden Spieler in Formation folgen (Gruppenbeitritt).
	// Stern = kein Namensfilter (CmdFollow nimmt dann den naechsten Spieler).
	void PlayerFollow(string playerName)
	{
		if (!NpcReady())
			return;
		if (playerName == "*")
			playerName = "";
		ResetForPlayerCmd();
		m_CmdId = "player_follow";
		m_CmdAction = "follow";
		IsuCommand c = new IsuCommand();
		c.action = "follow";
		c.text = playerName;
		CmdFollow(c);
	}

	// Naechste Leiche/Behaelter in 50 m ausraeumen.
	void PlayerLoot()
	{
		if (!NpcReadyOnFoot())
			return;
		ResetForPlayerCmd();
		m_CmdId = "player_loot";
		m_CmdAction = "loot_container";
		IsuCommand c = new IsuCommand();
		c.action = "loot_container";
		c.text = "";
		CmdLootContainer(c, false);
	}

	// Naechsten Infizierten in 100 m angreifen.
	void PlayerEngage()
	{
		if (!NpcReadyOnFoot())
			return;
		ResetForPlayerCmd();
		m_CmdId = "player_engage";
		m_CmdAction = "engage";
		IsuCommand c = new IsuCommand();
		c.action = "engage";
		CmdEngage(c);
	}

	static void RouteFollow(int low, int high, string name)
	{
		foreach (string idFo, IsuBridge instFo : s_Instances)
		{
			if (instFo.MatchesNetworkId(low, high))
			{
				instFo.PlayerFollow(name);
				return;
			}
		}
	}

	static void RouteFollowNearest(vector ppos, string name)
	{
		IsuBridge b = FindNearestInstance(ppos);
		if (b)
			b.PlayerFollow(name);
	}

	static void RouteLoot(int low, int high)
	{
		foreach (string idLo, IsuBridge instLo : s_Instances)
		{
			if (instLo.MatchesNetworkId(low, high))
			{
				instLo.PlayerLoot();
				return;
			}
		}
	}

	static void RouteLootNearest(vector ppos)
	{
		IsuBridge b = FindNearestInstance(ppos);
		if (b)
			b.PlayerLoot();
	}

	static void RouteEngage(int low, int high)
	{
		foreach (string idEn, IsuBridge instEn : s_Instances)
		{
			if (instEn.MatchesNetworkId(low, high))
			{
				instEn.PlayerEngage();
				return;
			}
		}
	}

	static void RouteEngageNearest(vector ppos)
	{
		IsuBridge b = FindNearestInstance(ppos);
		if (b)
			b.PlayerEngage();
	}

	// Befehlsrad/Direkttasten: der 4_World-RPC-Handler (IsuArenaControl) legt
	// den Befehl in npc_command.txt ab; dieser Tick (alle 0,5 s, einmal pro
	// Mission - nicht pro Agent) liest ihn und fuehrt ihn aus. Die Datei wird
	// beim Mission-Start geleert, daher startet s_LastNpcSeq bei 0.
	static int s_LastNpcSeq = 0;
	static void TickNpcCommand()
	{
		if (!FileExist("$profile:IsuSurvivor/npc_command.txt"))
			return;
		FileHandle fh = OpenFile("$profile:IsuSurvivor/npc_command.txt", FileMode.READ);
		if (fh == 0)
			return;
		string seqLine;
		string cmdLine;
		FGets(fh, seqLine);
		FGets(fh, cmdLine);
		CloseFile(fh);
		int seq = seqLine.ToInt();
		if (seq <= s_LastNpcSeq || cmdLine == "")
			return;
		s_LastNpcSeq = seq;
		OnPlayerNpcCommand(cmdLine);
	}

	// Parser fuer den Spieler-Direktbefehl-RPC. Formate (mode = single|nearest|all):
	//   halt|single|<low>|<high>
	//   halt|nearest|<px>|<pz>
	//   halt|all
	//   goto|single|<low>|<high>|<x>|<z>
	//   goto|nearest|<px>|<pz>|<x>|<z>
	//   goto|all|<x>|<z>
	static void OnPlayerNpcCommand(string line)
	{
		Print("[IsuSurvivor] Spieler-Direktbefehl: " + line);

		array<string> p = new array<string>();
		line.Split("|", p);
		if (p.Count() < 2)
			return;

		string action = p[0];
		string mode = p[1];

		if (action == "halt")
		{
			if (mode == "all")
				RouteHaltAll();
			else if (mode == "nearest" && p.Count() >= 4)
				RouteHaltNearest(Vector(p[2].ToFloat(), 0, p[3].ToFloat()));
			else if (mode == "single" && p.Count() >= 4)
				RouteHalt(p[2].ToInt(), p[3].ToInt());
			return;
		}

		if (action == "goto")
		{
			if (mode == "all" && p.Count() >= 4)
				RouteGotoAll(Vector(p[2].ToFloat(), 0, p[3].ToFloat()));
			else if (mode == "nearest" && p.Count() >= 6)
				RouteGotoNearest(Vector(p[2].ToFloat(), 0, p[3].ToFloat()), Vector(p[4].ToFloat(), 0, p[5].ToFloat()));
			else if (mode == "single" && p.Count() >= 6)
				RouteGoto(p[2].ToInt(), p[3].ToInt(), Vector(p[4].ToFloat(), 0, p[5].ToFloat()));
			return;
		}

		// Radialmenue-Aktionen. follow traegt zusaetzlich den Spielernamen als
		// letztes Feld (Client sanitisiert das '|' weg).
		if (action == "follow")
		{
			if (mode == "single" && p.Count() >= 5)
				RouteFollow(p[2].ToInt(), p[3].ToInt(), p[4]);
			else if (mode == "nearest" && p.Count() >= 5)
				RouteFollowNearest(Vector(p[2].ToFloat(), 0, p[3].ToFloat()), p[4]);
			return;
		}

		if (action == "loot")
		{
			if (mode == "single" && p.Count() >= 4)
				RouteLoot(p[2].ToInt(), p[3].ToInt());
			else if (mode == "nearest" && p.Count() >= 4)
				RouteLootNearest(Vector(p[2].ToFloat(), 0, p[3].ToFloat()));
			return;
		}

		if (action == "engage")
		{
			if (mode == "single" && p.Count() >= 4)
				RouteEngage(p[2].ToInt(), p[3].ToInt());
			else if (mode == "nearest" && p.Count() >= 4)
				RouteEngageNearest(Vector(p[2].ToFloat(), 0, p[3].ToFloat()));
			return;
		}
	}

	private void CmdDespawn()
	{
		if (!m_Npc)
		{
			m_CmdStatus = "failed";
			m_CmdDetail = "kein npc vorhanden";
			return;
		}

		ReleaseClaim();
		IsuAgentRegistry.Unregister(m_Npc);
		if (s_MarkerModule)
			s_MarkerModule.RemoveServerMarker("isu_agent_" + m_NpcName);
		GetGame().ObjectDelete(m_Npc);
		m_Npc = null;
		m_CmdStatus = "done";
	}

	// Fraktions-Fabrik fuer den Arena-Hostilitaetsmodus
	private eAIFaction CreateFactionByName(string name)
	{
		switch (name)
		{
			case "west":
				return new eAIFactionWest();
			case "east":
				return new eAIFactionEast();
			case "mercenaries":
				return new eAIFactionMercenaries();
			case "raiders":
				return new eAIFactionRaiders();
		}
		return new eAIFactionCivilian();
	}

	// ----------------------------------------------------- Survival-Kommandos

	private void CmdPickup(IsuCommand cmd)
	{
		if (!NpcReadyOnFoot())
			return;

		m_PickupFilter = cmd.text;
		ItemBase item = FindNearestGroundItem(cmd.text, 50.0);
		if (!item)
		{
			m_CmdStatus = "failed";
			m_CmdDetail = "kein passendes Bodenitem in 50 m (filter: " + cmd.text + ")";
			return;
		}

		m_PickupItem = item;
		ClaimItem(item);

		float dist = Dist2D(m_Npc.GetPosition(), item.GetPosition());
		if (dist <= 2.0)
		{
			DoTakePickupItem();
			return;
		}

		if (!StartWalk(item.GetPosition()))
			return;

		m_PickupWalking = true;
		m_CmdStatus = "running";
	}

	private void DoTakePickupItem()
	{
		if (!m_PickupItem)
		{
			m_CmdStatus = "failed";
			m_CmdDetail = "Item verschwunden";
			return;
		}

		// Hat es sich jemand anders inzwischen geschnappt (steckt jetzt in
		// einem Inventar)? Dann nicht aus fremder Tasche ziehen.
		if (m_PickupItem.GetHierarchyParent())
		{
			m_CmdStatus = "failed";
			m_CmdDetail = "wurde gerade von jemandem aufgehoben";
			m_PickupItem = null;
			m_PickupWalking = false;
			ReleaseClaim();
			return;
		}

		string type = m_PickupItem.GetType();
		// Hand frei machen - ein Hand-Item blockiert sonst das stille
		// Rausfallen von Loot (eAI-Loot-Pfad braucht die freie Hand)
		EnsureHandsFree(null);
		if (m_Npc.eAI_TakeItemToInventory(m_PickupItem, true))
		{
			m_CmdStatus = "done";
			m_CmdDetail = type;
		}
		else
		{
			m_CmdStatus = "failed";
			m_CmdDetail = "Inventar voll - kein Platz fuer " + type + " (erst etwas droppen/anziehen)";
		}

		m_PickupItem = null;
		m_PickupWalking = false;
		ReleaseClaim();
	}

	// --- Loot-Claims: ein Bodenitem gehoert dem Bot, der es zuerst anlaeuft ---
	private void ClaimItem(EntityAI item)
	{
		ReleaseClaim();
		if (item)
		{
			s_PickupClaims.Set(item, this);
			m_ClaimedItem = item;
		}
	}

	private void ReleaseClaim()
	{
		if (m_ClaimedItem)
		{
			if (s_PickupClaims.Contains(m_ClaimedItem))
				s_PickupClaims.Remove(m_ClaimedItem);
			m_ClaimedItem = null;
		}
	}

	private bool IsClaimedByOther(EntityAI item)
	{
		if (!s_PickupClaims.Contains(item))
			return false;
		IsuBridge owner = s_PickupClaims.Get(item);
		// Selbstheilend: ist der Eigentuemer tot/weg (Bot starb mitten im
		// pickup), verfaellt der Claim sofort - sonst bliebe das Item ewig
		// fuer alle gesperrt.
		if (!owner || !owner.m_Npc || !owner.m_Npc.IsAlive())
		{
			s_PickupClaims.Remove(item);
			return false;
		}
		return (owner != this);
	}

	// Tierkadaver verwerten (Jagd): braucht ein Schneidwerkzeug, laeuft zum
	// naechsten toten Tier (<=50 m), zerlegt es zu Fleisch und loescht den
	// Kadaver. cmd.text = optionaler Classname-Filter (z.B. "Capra").
	private void CmdHarvest(IsuCommand cmd)
	{
		if (!NpcReadyOnFoot())
			return;

		if (!FindKnife())
		{
			m_CmdStatus = "failed";
			m_CmdDetail = "kein Schneidwerkzeug (Messer/Machete/Axt) im Inventar";
			return;
		}

		AnimalBase target = null;
		float nearest = 51.0;
		array<Object> objects = new array<Object>();
		array<CargoBase> cargos = new array<CargoBase>();
		GetGame().GetObjectsAtPosition3D(m_Npc.GetPosition(), 50.0, objects, cargos);

		foreach (Object obj : objects)
		{
			AnimalBase animal = AnimalBase.Cast(obj);
			if (!animal || animal.IsAlive())
				continue;
			if (cmd.text != "" && !animal.GetType().Contains(cmd.text))
				continue;
			float dist = vector.Distance(m_Npc.GetPosition(), animal.GetPosition());
			if (dist < nearest)
			{
				nearest = dist;
				target = animal;
			}
		}

		if (!target)
		{
			m_CmdStatus = "failed";
			m_CmdDetail = "kein Tierkadaver in 50 m (kind=animal_corpse in der Umgebung)";
			return;
		}

		m_HarvestTarget = target;

		if (Dist2D(m_Npc.GetPosition(), target.GetPosition()) <= 3.0)
		{
			DoHarvest();
			return;
		}

		if (!StartWalk(target.GetPosition()))
			return;

		m_CmdStatus = "running";
	}

	private void DoHarvest()
	{
		if (!m_HarvestTarget)
		{
			m_CmdStatus = "failed";
			m_CmdDetail = "Tierkadaver verschwunden";
			return;
		}

		if (m_HarvestTarget.IsAlive())
		{
			m_HarvestTarget = null;
			m_CmdStatus = "failed";
			m_CmdDetail = "das Tier lebt noch - erst erlegen";
			return;
		}

		int count;
		string meat = MeatFor(m_HarvestTarget.GetType(), count);
		string animalName = m_HarvestTarget.GetType();
		int inInv = 0;
		int onGround = 0;
		vector pos = m_Npc.GetPosition();

		for (int i = 0; i < count; i++)
		{
			EntityAI created = m_Npc.GetInventory().CreateInInventory(meat);
			if (created)
			{
				inInv++;
			}
			else
			{
				GetGame().CreateObjectEx(meat, pos, ECE_PLACE_ON_SURFACE);
				onGround++;
			}
		}

		GetGame().ObjectDelete(m_HarvestTarget);
		m_HarvestTarget = null;
		m_CmdStatus = "done";
		m_CmdDetail = animalName + " verwertet: " + inInv.ToString() + "x " + meat + " im Inventar";
		if (onGround > 0)
			m_CmdDetail = m_CmdDetail + ", " + onGround.ToString() + "x am Boden (Inventar voll)";
	}

	// Schneidwerkzeug im Inventar des NPC (fuers Zerlegen)
	private ItemBase FindKnife()
	{
		array<EntityAI> knifeItems = new array<EntityAI>();
		m_Npc.GetInventory().EnumerateInventory(InventoryTraversalType.PREORDER, knifeItems);
		foreach (EntityAI ent : knifeItems)
		{
			ItemBase it = ItemBase.Cast(ent);
			if (!it)
				continue;
			string t = it.GetType();
			if (t.Contains("Knife") || t.Contains("Machete") || t.Contains("Cleaver") || t.Contains("Sickle") || t.Contains("Axe") || t.Contains("Hatchet") || t.Contains("Sword") || t.Contains("Bayonet"))
				return it;
		}
		return null;
	}

	// Tierart -> Fleischsorte + Stueckzahl (vereinfachtes Zerlegen)
	private string MeatFor(string animalType, out int count)
	{
		count = 2;
		if (animalType.Contains("BosTaurus"))
		{
			count = 6;
			return "BeefSteakMeat";
		}
		if (animalType.Contains("SusScrofa") || animalType.Contains("SusDomesticus"))
		{
			count = 4;
			return "BoarSteakMeat";
		}
		if (animalType.Contains("Cervus") || animalType.Contains("DamaDama") || animalType.Contains("Capreolus"))
		{
			count = 4;
			return "DeerSteakMeat";
		}
		if (animalType.Contains("CapraHircus"))
		{
			count = 3;
			return "GoatSteakMeat";
		}
		if (animalType.Contains("OvisAries"))
		{
			count = 3;
			return "MuttonSteakMeat";
		}
		if (animalType.Contains("Lepus"))
		{
			count = 2;
			return "RabbitLegMeat";
		}
		if (animalType.Contains("CanisLupus"))
		{
			count = 3;
			return "WolfSteakMeat";
		}
		if (animalType.Contains("Ursus"))
		{
			count = 6;
			return "BearSteakMeat";
		}
		return "ChickenBreastMeat";
	}

	// Naechsten lootbaren Behaelter ausraeumen: Leichen (tote Infizierte,
	// Spieler, AI) und - wenn corpsesOnly false - alles am Boden mit Inhalt
	// (Rucksaecke, Kleidung, Kisten). cmd.text = optionaler Classname-Filter.
	private void CmdLootContainer(IsuCommand cmd, bool corpsesOnly)
	{
		if (!NpcReadyOnFoot())
			return;

		EntityAI target = null;
		float nearest = 51.0;

		array<Object> objects = new array<Object>();
		array<CargoBase> cargos = new array<CargoBase>();
		GetGame().GetObjectsAtPosition3D(m_Npc.GetPosition(), 50.0, objects, cargos);

		foreach (Object obj : objects)
		{
			EntityAI body = null;

			// Infizierte sind KEINE Man-Ableger (DayZCreatureAI) - beide Typen pruefen
			DayZInfected zombie = DayZInfected.Cast(obj);
			if (zombie && !zombie.IsAlive())
				body = zombie;

			if (!body)
			{
				Man man = Man.Cast(obj);
				if (man && man != m_Npc && !man.IsAlive())
					body = man;
			}

			if (!body && !corpsesOnly)
			{
				ItemBase ground = ItemBase.Cast(obj);
				if (ground && !ground.GetHierarchyParent() && CountContents(ground) > 0)
					body = ground;
			}

			if (!body)
				continue;

			if (cmd.text != "" && !body.GetType().Contains(cmd.text))
				continue;

			float dist = vector.Distance(m_Npc.GetPosition(), body.GetPosition());
			if (dist < nearest)
			{
				nearest = dist;
				target = body;
			}
		}

		if (!target)
		{
			m_CmdStatus = "failed";
			if (corpsesOnly)
				m_CmdDetail = "keine Leiche in 50 m (kind=corpse in der Umgebung)";
			else
				m_CmdDetail = "nichts Lootbares mit Inhalt in 50 m (Leichen/Rucksaecke/Kisten)";
			return;
		}

		m_CorpseTarget = target;

		if (Dist2D(m_Npc.GetPosition(), target.GetPosition()) <= 3.0)
		{
			DoLootContainer();
			return;
		}

		if (!StartWalk(target.GetPosition()))
			return;

		m_CmdStatus = "running";
	}

	private void DoLootContainer()
	{
		if (!m_CorpseTarget)
		{
			m_CmdStatus = "failed";
			m_CmdDetail = "Lootziel verschwunden";
			return;
		}

		int taken = 0;
		int lootable = 0;   // wie viele echte Items lagen drin (Kleidung der
		                    // Zombies zaehlt nicht als lootbar)
		array<EntityAI> items = new array<EntityAI>();
		m_CorpseTarget.GetInventory().EnumerateInventory(InventoryTraversalType.PREORDER, items);

		foreach (EntityAI ent : items)
		{
			if (ent == m_CorpseTarget)
				continue;

			ItemBase item = ItemBase.Cast(ent);
			if (!item || item.IsClothing())
				continue;

			lootable++;
			if (taken >= 12)
				continue;

			// Bulk-Uebernahme ohne Einzel-Animationen
			if (m_Npc.eAI_TakeItemToInventory(item, false))
				taken++;
		}

		m_CorpseTarget = null;

		if (taken == 0)
		{
			m_CmdStatus = "failed";
			// Zwei sehr verschiedene Ursachen klar trennen, sonst rennt der
			// Agent dieselbe Leiche immer wieder an
			if (lootable == 0)
				m_CmdDetail = "nichts Brauchbares drin (nur getragene Kleidung) - durchsucht, weitergehen";
			else
				m_CmdDetail = "Inventar voll - " + lootable.ToString() + " Item(s) blieben liegen (erst Platz schaffen)";
			return;
		}

		m_CmdStatus = "done";
		m_CmdDetail = taken.ToString() + " Item(s) uebernommen";
	}

	// store_container: Gegenstueck zu loot. Items aus dem NPC-Inventar IN einen
	// nahen Container (Zelt/Kiste/Fass/Rucksack am Boden) legen. Genau das fehlte -
	// bisher konnten die Agenten nur nehmen oder auf den BODEN werfen (drop), nicht
	// verstauen, darum dumpten sie am Zelt alles auf den Boden.
	private void CmdStore(IsuCommand cmd)
	{
		if (!NpcReadyOnFoot())
			return;

		EntityAI target = null;
		float nearest = 51.0;

		array<Object> objects = new array<Object>();
		array<CargoBase> cargos = new array<CargoBase>();
		GetGame().GetObjectsAtPosition3D(m_Npc.GetPosition(), 50.0, objects, cargos);

		foreach (Object obj : objects)
		{
			ItemBase cont = ItemBase.Cast(obj);
			if (!cont || cont == m_Npc)
				continue;
			// Eigenstaendiger Container am Boden MIT Cargo-Bereich (Zelt, Kiste,
			// Fass, Rucksack am Boden) - keine getragenen, keine Waffen ohne Cargo.
			if (cont.GetHierarchyParent() || !cont.GetInventory() || !cont.GetInventory().GetCargo())
				continue;

			float dist = vector.Distance(m_Npc.GetPosition(), cont.GetPosition());
			if (dist < nearest)
			{
				nearest = dist;
				target = cont;
			}
		}

		if (!target)
		{
			m_CmdStatus = "failed";
			m_CmdDetail = "kein Container mit Stauraum in 50 m (Zelt/Kiste/Fass am Boden)";
			return;
		}

		m_CorpseTarget = target;     // Lauf-Ziel (mit dem Loot-Zielfeld geteilt)
		m_StoreFilter = cmd.text;    // optionaler Item-Classname-Filter (leer = alles Lose)

		if (Dist2D(m_Npc.GetPosition(), target.GetPosition()) <= 3.0)
		{
			DoStore();
			return;
		}

		if (!StartWalk(target.GetPosition()))
			return;

		m_CmdStatus = "running";
	}

	private void DoStore()
	{
		if (!m_CorpseTarget)
		{
			m_CmdStatus = "failed";
			m_CmdDetail = "Container verschwunden";
			return;
		}

		array<EntityAI> items = new array<EntityAI>();
		m_Npc.GetInventory().EnumerateInventory(InventoryTraversalType.PREORDER, items);

		int stored = 0;
		int full = 0;
		foreach (EntityAI ent : items)
		{
			if (stored >= 12)
				break;

			ItemBase item = ItemBase.Cast(ent);
			if (!item || item.IsRuined())
				continue;

			// NUR lose Cargo-Items verstauen (Rucksack-/Westen-/Hosen-Inhalt) -
			// niemals getragene Ausruestung (ATTACHMENT) oder die Waffe in der
			// Hand (HANDS), sonst zieht sich der NPC selbst aus.
			InventoryLocation loc = new InventoryLocation();
			item.GetInventory().GetCurrentInventoryLocation(loc);
			if (loc.GetType() != InventoryLocationType.CARGO)
				continue;

			if (m_StoreFilter != "" && !item.GetType().Contains(m_StoreFilter))
				continue;

			if (m_CorpseTarget.GetInventory().TakeEntityToInventory(InventoryMode.SERVER, FindInventoryLocationType.CARGO, item))
				stored++;
			else
				full++;
		}

		EntityAI storeTarget = m_CorpseTarget;   // vor dem Nullen fuer die Diagnose merken
		m_CorpseTarget = null;
		m_StoreFilter = "";

		if (stored == 0)
		{
			m_CmdStatus = "failed";
			if (full > 0)
			{
				// "voll" vs. "nicht bereit" unterscheiden: ein FRISCH gestelltes,
				// noch nicht fertig aufgebautes Zelt gibt seinen Cargo erst beim
				// ersten Oeffnen frei - dann schlaegt TakeEntityToInventory fehl,
				// OBWOHL nichts drin ist. Das pauschale "Container voll" fuehrte
				// die Agenten zur Fehldiagnose "Zelt-Bug". GetItemCount==0 heisst:
				// leer, aber hat trotzdem nichts angenommen -> nicht voll.
				int cargoCount = -1;
				if (storeTarget != null && storeTarget.GetInventory() != null && storeTarget.GetInventory().GetCargo() != null)
					cargoCount = storeTarget.GetInventory().GetCargo().GetItemCount();

				if (cargoCount == 0)
					m_CmdDetail = "Container nahm nichts an, ist aber leer - das Zelt ist evtl. noch nicht fertig aufgebaut. Kurz warten oder das Zelt einmal oeffnen, dann erneut versuchen (kein Bug).";
				else
					m_CmdDetail = "Container voll (" + cargoCount.ToString() + " drin) - " + full.ToString() + " Item(s) passten nicht rein";
			}
			else
				m_CmdDetail = "nichts Loses zum Verstauen im Inventar (nur Getragenes/Waffe)";
			return;
		}

		m_CmdStatus = "done";
		m_CmdDetail = stored.ToString() + " Item(s) verstaut";
	}

	// Konserven oeffnen braucht Werkzeug (wie beim Spieler); Getraenkedosen
	// gehen von Hand.
	private bool HasCanOpeningTool()
	{
		// Enumeriert bewusst SELBST: ein geteiltes Array aus der foreach des
		// Aufrufers heraus nochmal zu iterieren liefert in EnforceScript
		// nicht alle Elemente (verschachtelte Iteration, selbes Objekt).
		array<EntityAI> tools = new array<EntityAI>();
		m_Npc.GetInventory().EnumerateInventory(InventoryTraversalType.PREORDER, tools);

		foreach (EntityAI ent : tools)
		{
			string t = ent.GetType();
			if (t == "CanOpener")
				return true;
			if (t.Contains("Knife"))
				return true;
			if (t.Contains("Machete"))
				return true;
			if (t.Contains("Cleaver"))
				return true;
			if (t.Contains("Screwdriver"))
				return true;
			if (t.Contains("Hacksaw"))
				return true;
		}
		return false;
	}

	// Getraenkedosen (SodaCan_ColorBase) zaehlen als Getraenk, sind aber kein
	// IsLiquidContainer - gleiche Logik wie Expansions ExpansionIsLiquidItem().
	private bool IsDrinkItem(Edible_Base edible)
	{
		if (edible.IsLiquidContainer())
			return true;
		return edible.IsKindOf("SodaCan_ColorBase");
	}

	// Verschlossen-Erkennung. Vanilla ist da uneinheitlich: manche Konserven
	// ueberschreiben IsOpen() (DogFoodCan), andere nicht (BakedBeansCan) -
	// die verlaessliche Konvention ist das "<Classname>_Opened"-Klassenpaar,
	// in das Open() die Dose verwandelt.
	private bool IsSealedCan(Edible_Base edible)
	{
		if (!edible.IsOpen())
			return true;
		string type = edible.GetType();
		if (type.Contains("_Opened"))
			return false;
		return GetGame().ConfigIsExisting("CfgVehicles " + type + "_Opened");
	}

	// Erstes offenes, konsumierbares Item der Kategorie; meldet nebenbei,
	// ob ein verschlossenes Exemplar gesehen wurde.
	private Edible_Base FindConsumable(bool liquid, string filter, out bool sealedSeen)
	{
		array<EntityAI> items = new array<EntityAI>();
		m_Npc.GetInventory().EnumerateInventory(InventoryTraversalType.PREORDER, items);

		Edible_Base firstAny = null;
		Edible_Base exact = null;
		Edible_Base sub = null;

		foreach (EntityAI ent : items)
		{
			Edible_Base edible = Edible_Base.Cast(ent);
			if (!edible)
				continue;
			if (IsDrinkItem(edible) != liquid)
				continue;
			if (IsSealedCan(edible))
			{
				sealedSeen = true;
				continue;
			}
			if (!edible.CanBeConsumed())
				continue;
			if (!firstAny)
				firstAny = edible;
			if (filter != "")
			{
				string t = edible.GetType();
				if (t == filter && !exact)
					exact = edible;
				else if (!sub && t.IndexOf(filter) > -1)
					sub = edible;
			}
		}

		// Mit Filter: exakter Treffer vor Teilstring, KEIN Fallback aufs erste
		// beliebige (sonst isst er irgendwas - genau der Bug eat(Pear)->Rice).
		// Ohne Filter: erstes Essbare wie bisher.
		if (filter != "")
		{
			if (exact)
				return exact;
			return sub;
		}
		return firstAny;
	}

	// Verschlossene Dose/Konserve der Kategorie oeffnen (Konserven nur mit
	// Werkzeug). ACHTUNG: Open() ERSETZT Konserven durch die "_Opened"-
	// Variante - die Rueckgabe taugt nur als Erfolgs-Marker, danach neu suchen.
	private bool OpenSealedConsumable(bool liquid, string filter)
	{
		array<EntityAI> items = new array<EntityAI>();
		m_Npc.GetInventory().EnumerateInventory(InventoryTraversalType.PREORDER, items);

		foreach (EntityAI ent : items)
		{
			Edible_Base edible = Edible_Base.Cast(ent);
			if (!edible)
				continue;
			if (IsDrinkItem(edible) != liquid)
				continue;
			if (!IsSealedCan(edible))
				continue;
			if (filter != "" && edible.GetType().IndexOf(filter) == -1)
				continue;
			if (!liquid && !HasCanOpeningTool())
				return false;

			// Open() ersetzt nur Items IN DER HAND (Spieler-Action) und
			// verpufft bei Cargo-Items der eAI - deshalb selbst in die
			// "_Opened"-Variante tauschen (gleiche Inventar-Position).
			string openedType = edible.GetType() + "_Opened";
			if (GetGame().ConfigIsExisting("CfgVehicles " + openedType))
				MiscGameplayFunctions.TurnItemIntoItem(edible, openedType, m_Npc);
			else
				edible.Open();
			return true;
		}
		return false;
	}

	private void CmdConsume(bool liquid, string filter)
	{
		if (!NpcReady())
			return;

		string opened = "";
		bool sealedSeen = false;
		Edible_Base best = FindConsumable(liquid, filter, sealedSeen);

		// Nichts Offenes da, aber Verschlossenes: erst oeffnen (die
		// Interaktion, die ein Spieler auch machen muss), dann neu suchen.
		if (!best && sealedSeen && OpenSealedConsumable(liquid, filter))
		{
			opened = "geoeffnet und ";
			bool dummy = false;
			best = FindConsumable(liquid, filter, dummy);
			if (!best)
			{
				// Open() ersetzt Konserven u.U. erst am Frame-Ende durch die
				// "_Opened"-Variante - dann ist sie jetzt noch nicht greifbar.
				// Das Oeffnen war trotzdem ein Erfolg.
				m_CmdStatus = "done";
				m_CmdDetail = "Konserve geoeffnet - nochmal eat zum Verzehren";
				return;
			}
		}

		if (!best)
		{
			m_CmdStatus = "failed";
			if (filter != "")
				m_CmdDetail = "kein passendes Essbares im Inventar (filter: " + filter + ")";
			else if (sealedSeen && !liquid)
				m_CmdDetail = "nur verschlossene Konserven - Dosenoeffner oder Messer noetig";
			else if (liquid)
				m_CmdDetail = "nichts Trinkbares im Inventar";
			else
				m_CmdDetail = "nichts Essbares im Inventar";
			return;
		}

		// Getraenkedose: das Aufreissen gehoert zur Handlung (die Engine
		// dieser DayZ-Version kennt fuer Dosen keinen Verschluss-Zustand)
		if (liquid && opened == "" && best.IsKindOf("SodaCan_ColorBase"))
			opened = "aufgemacht und ";

		float amount = best.GetQuantity();
		if (amount <= 0)
			amount = 1.0;

		string type = best.GetType();
		if (m_Npc.Consume(best, amount, EConsumeType.ITEM_SINGLE_TIME))
		{
			m_CmdStatus = "done";
			m_CmdDetail = opened + type + " (" + amount.ToString() + ")";
		}
		else
		{
			m_CmdStatus = "failed";
			m_CmdDetail = "Consume fehlgeschlagen: " + type;
		}
	}

	private void CmdEquipBest()
	{
		if (!NpcReady())
			return;

		m_Slinged = false;   // Waffe in der Hand = kein Schulter-Marsch mehr

		array<EntityAI> items = new array<EntityAI>();
		m_Npc.GetInventory().EnumerateInventory(InventoryTraversalType.PREORDER, items);

		// Prioritaet: geladene Feuerwaffe > Nahkampfwaffe > leere Feuerwaffe.
		// Ruinierte Items blockieren eAI_TakeItemToHands ("Geisterwaffen")
		// und werden komplett uebersprungen.
		ItemBase bestLoaded = null;
		ItemBase bestMelee = null;
		ItemBase bestEmpty = null;

		foreach (EntityAI ent : items)
		{
			ItemBase item = ItemBase.Cast(ent);
			if (!item || item.IsRuined())
				continue;

			Weapon_Base weapon = Weapon_Base.Cast(ent);
			if (weapon)
			{
				if (weapon.Expansion_HasAmmo())
				{
					if (!bestLoaded)
						bestLoaded = weapon;
				}
				else if (!bestEmpty)
				{
					bestEmpty = weapon;
				}
				continue;
			}

			// Angebaute Bajonette (GetHierarchyParent = Waffe) ausschliessen: sie
			// lassen sich nicht eigenstaendig in die Hand nehmen. Sonst waehlt
			// equip_best das Bajonett statt der (leeren) Waffe und scheitert.
			if (!bestMelee && item.Expansion_IsMeleeWeapon() && !Weapon_Base.Cast(item.GetHierarchyParent()))
				bestMelee = item;
		}

		ItemBase best = bestLoaded;
		if (!best)
			best = bestMelee;
		if (!best)
			best = bestEmpty;

		if (!best)
		{
			m_CmdStatus = "failed";
			m_CmdDetail = "keine brauchbare Waffe im Inventar (ruinierte zaehlen nicht)";
			return;
		}

		string type = best.GetType();
		// NPC-Waffen sollen nicht klemmen: gewaehlte Waffe auf pristine setzen und
		// eine eventuelle Ladehemmung loesen (gelootete Waffen kommen oft beschaedigt).
		best.SetHealth01("", "Health", 1.0);
		Weapon_Base bestWpn = Weapon_Base.Cast(best);
		if (bestWpn && bestWpn.IsJammed())
			bestWpn.SetJammed(false);
		EnsureHandsFree(best);
		m_Npc.eAI_TakeItemToHands(best, true);
		// NICHT sofort "done" melden: eAI_TakeItemToHands gibt true zurueck, sobald
		// die Aktion ANGENOMMEN ist - NICHT wenn die Waffe wirklich in der Hand
		// liegt (async, ein paar Frames spaeter, scheitert manchmal stillschweigend).
		// Sonst meldet equip_best "Ausgeruestet: X", waehrend der NPC noch den
		// Holzstab haelt (Konrad 2026-06-16; kostete Viktor davor das Leben).
		// UpdateEquipRetry verifiziert per Tick GetEntityInHands und meldet erst
		// dann "done" - oder "failed", wenn die Waffe nicht in die Hand kommt.
		m_EquipPendingItem = best;
		m_EquipPendingTries = 0;
		m_CmdDetail = type;
		m_CmdStatus = "running";
	}

	// Zusaetzliche Langwaffen (nicht in der Hand, keine Pistole, nicht ruiniert)
	// auf einen freien Schulter-/Melee-Slot haengen, solange Platz ist - sonst
	// bleiben sie unsichtbar im Cargo statt sichtbar geschultert.
	private void SlingSecondaryWeapons(ItemBase primaryInHands)
	{
		array<EntityAI> items = new array<EntityAI>();
		m_Npc.GetInventory().EnumerateInventory(InventoryTraversalType.PREORDER, items);
		foreach (EntityAI ent : items)
		{
			Weapon_Base w = Weapon_Base.Cast(ent);
			if (!w || w == primaryInHands || w.IsRuined())
				continue;
			if (w.IsKindOf("Pistol_Base"))
				continue;
			// Schon an einem Koerper-Slot (geschultert)? Dann in Ruhe lassen.
			InventoryLocation il_cur = new InventoryLocation();
			if (w.GetInventory().GetCurrentInventoryLocation(il_cur) && il_cur.GetType() == InventoryLocationType.ATTACHMENT)
				continue;
			// TakeToBodySlot probiert die Schulter-/Melee-Slots der Waffe durch
			// und haengt sie an den ersten freien (oder tut nichts, wenn keiner frei).
			TakeToBodySlot(w);
		}
	}

	// Waffe in der Hand reinigen: auf pristine reparieren und Ladehemmung loesen.
	// Verbraucht eine Ladung WeaponCleaningKit aus dem Inventar, falls vorhanden.
	private void CmdCleanWeapon()
	{
		if (!NpcReady())
			return;

		Weapon_Base wpn = Weapon_Base.Cast(m_Npc.GetHumanInventory().GetEntityInHands());
		if (!wpn)
		{
			m_CmdStatus = "failed";
			m_CmdDetail = "keine Waffe in der Hand (erst equip_best)";
			return;
		}

		bool wasJammed = wpn.IsJammed();
		if (wasJammed)
			wpn.SetJammed(false);

		int levelBefore = wpn.GetHealthLevel();

		// WeaponCleaningKit suchen und (falls vorhanden) eine Ladung verbrauchen.
		ItemBase kit = null;
		array<EntityAI> kitItems = new array<EntityAI>();
		m_Npc.GetInventory().EnumerateInventory(InventoryTraversalType.PREORDER, kitItems);
		foreach (EntityAI ent : kitItems)
		{
			if (ent && ent.IsInherited(WeaponCleaningKit) && !ent.IsDamageDestroyed())
			{
				kit = ItemBase.Cast(ent);
				break;
			}
		}

		wpn.SetHealth01("", "Health", 1.0);
		if (kit)
		{
			float q = kit.GetQuantity();
			if (q > 0)
				kit.SetQuantity(q - 1);
		}

		m_CmdStatus = "done";
		m_CmdDetail = wpn.GetType() + " gereinigt (Level " + levelBefore.ToString() + "->0)";
		if (wasJammed)
			m_CmdDetail = m_CmdDetail + ", Ladehemmung geloest";
		if (kit)
			m_CmdDetail = m_CmdDetail + ", Kit benutzt";
		else
			m_CmdDetail = m_CmdDetail + ", ohne Kit";
	}

	// Munitionskiste (AmmoBox_*) im Inventar aufmachen: Inhalt aus
	// "CfgVehicles <box> Resources" erzeugen (ins Inventar, sonst auf den Boden)
	// und die Box vernichten. eAI kann die Vanilla-Unpack-Action nicht nutzen
	// (ServerReplaceItemInHandsWithNew ist Player-only), daher hier nachgebaut.
	private void CmdUnpackAmmo(IsuCommand cmd)
	{
		if (!NpcReady())
			return;

		ItemBase box = null;
		array<EntityAI> items = new array<EntityAI>();
		m_Npc.GetInventory().EnumerateInventory(InventoryTraversalType.PREORDER, items);
		foreach (EntityAI ent : items)
		{
			ItemBase it = ItemBase.Cast(ent);
			if (!it)
				continue;
			string t = it.GetType();
			if (!t.Contains("AmmoBox"))
				continue;
			if (cmd.text != "" && t != cmd.text)
				continue;
			box = it;
			break;
		}

		if (!box)
		{
			m_CmdStatus = "failed";
			m_CmdDetail = "keine AmmoBox im Inventar";
			return;
		}

		string boxType = box.GetType();
		string resPath = "CfgVehicles " + boxType + " Resources";
		if (!GetGame().ConfigIsExisting(resPath) || GetGame().ConfigGetChildrenCount(resPath) == 0)
		{
			m_CmdStatus = "failed";
			m_CmdDetail = "kein Inhalt definiert fuer " + boxType;
			return;
		}

		string childName = "";
		GetGame().ConfigGetChildName(resPath, 0, childName);
		int itemCount = GetGame().ConfigGetInt(resPath + " " + childName + " value");

		string where = "Inventar";
		EntityAI created = m_Npc.GetInventory().CreateInInventory(childName);
		if (!created)
		{
			created = GetGame().CreateObjectEx(childName, m_Npc.GetPosition(), ECE_PLACE_ON_SURFACE);
			where = "Boden (Inventar voll)";
		}
		if (created)
		{
			Magazine pile = Magazine.Cast(created);
			if (pile)
				pile.ServerSetAmmoCount(itemCount);
			else
			{
				ItemBase ib = ItemBase.Cast(created);
				if (ib)
					ib.SetQuantity(itemCount);
			}
		}

		GetGame().ObjectDelete(box);

		m_CmdStatus = "done";
		m_CmdDetail = boxType + " entpackt: " + childName + " x" + itemCount.ToString() + " (" + where + ")";
	}

	// Hand freiraeumen, bevor eine Waffe gezogen wird: eAI_TakeItemToHands
	// bricht bei belegter Hand hart ab (Ursache vieler "fehlgeschlagen"-
	// Schleifen). Das Hand-Item wandert per eAI_TakeItemToInventory auf einen
	// freien Slot (Waffen auf Schulter/Ruecken, Rest ins Cargo); klappt das
	// nicht, wird es notfalls abgelegt.
	private void EnsureHandsFree(ItemBase keep)
	{
		if (!m_Npc)
			return;
		ItemBase inHands = ItemBase.Cast(m_Npc.GetHumanInventory().GetEntityInHands());
		if (!inHands || inHands == keep)
			return;
		if (!m_Npc.eAI_TakeItemToInventory(inHands, false))
			m_Npc.eAI_DropItem(inHands, true);
	}

	private bool TakeToBodySlot(ItemBase item)
	{
		// Dem eAI-Loot-Pfad nachgebaut (eAI_TakeItemToInventoryImpl,
		// IsClothing-Zweig). Die Slot-IDs kommen vom ITEM selbst
		// (GetSlotIdCount/GetSlotId) - das String-Mapping via CfgSlots
		// kennt etliche Koerper-Slots nicht ("Headgear" usw.).
		m_WearDiag = "";

		int n = item.GetInventory().GetSlotIdCount();
		if (n == 0)
		{
			m_WearDiag = "Item hat keine Attachment-Slots";
			return false;
		}

		for (int i = 0; i < n; i++)
		{
			int slotId = item.GetInventory().GetSlotId(i);
			string slotName = InventorySlots.GetSlotName(slotId);

			if (!m_Npc.GetInventory().HasAttachmentSlot(slotId))
			{
				m_WearDiag = m_WearDiag + slotName + ":kein-Koerperslot ";
				continue;
			}
			EntityAI worn = m_Npc.GetInventory().FindAttachment(slotId);
			if (worn)
			{
				m_WearDiag = m_WearDiag + slotName + ":belegt(" + worn.GetType() + ") ";
				continue;
			}

			InventoryLocation il_dst = new InventoryLocation();
			il_dst.SetAttachment(m_Npc, item, slotId);
			if (m_Npc.eAI_TakeItemToLocation(item, il_dst))
				return true;
			m_WearDiag = m_WearDiag + slotName + ":move-fehlgeschlagen ";
		}
		return false;
	}

	// Welches getragene Stueck blockiert die Koerper-Slots des neuen Items?
	// Slot-IDs kommen vom Item selbst (kein CfgSlots-String-Mapping).
	private ItemBase FindBlockingAttachment(ItemBase wanted)
	{
		// (1) Bisheriger Weg ueber die Slot-IDs des Items - deckt Kleidung/Headgear.
		int n = wanted.GetInventory().GetSlotIdCount();
		for (int i = 0; i < n; i++)
		{
			int slotId = wanted.GetInventory().GetSlotId(i);
			if (!m_Npc.GetInventory().HasAttachmentSlot(slotId))
				continue;
			ItemBase worn = ItemBase.Cast(m_Npc.GetInventory().FindAttachment(slotId));
			if (worn && worn != wanted)
				return worn;
		}
		// (2) Ergaenzung ueber die CONFIG-Body-Slots (inventorySlot[]) + Slot-Name.
		// Noetig fuer RUCKSAECKE: deren GetInventory().GetSlotId() liefert die
		// Innentaschen, NICHT den "Back"-Body-Slot - darum wurde ein getragener
		// Rucksack bisher nie als Blocker erkannt (wear AssaultBag scheiterte mit
		// "nichts zum Tauschen gefunden"). FindAttachmentBySlotName loest den
		// getragenen Slot zuverlaessig auf (DayZ-Standard, vgl. eAIBase).
		TStringArray cfgSlots = new TStringArray();
		wanted.ConfigGetTextArray("inventorySlot", cfgSlots);
		foreach (string slotName : cfgSlots)
		{
			ItemBase wornCfg = ItemBase.Cast(m_Npc.FindAttachmentBySlotName(slotName));
			if (wornCfg && wornCfg != wanted)
				return wornCfg;
		}
		return null;
	}

	// Kleidungsstueck anziehen - aus dem Inventar oder direkt vom Boden
	// (10 m; nach pickup mit vollen Slots faellt Kleidung dorthin zurueck,
	// pickup meldet trotzdem done). Belegte Slots werden GETAUSCHT: das
	// alte Stueck landet am Boden.
	// True, wenn das Item keinen Cargo-Inhalt hat (zum gefahrlosen Ausziehen/
	// Fallenlassen ohne Inhaltsverlust).
	private bool CargoIsEmpty(EntityAI item)
	{
		if (!item || !item.GetInventory())
			return true;
		CargoBase cargo = item.GetInventory().GetCargo();
		if (!cargo)
			return true;
		return cargo.GetItemCount() == 0;
	}

	private void CmdWear(IsuCommand cmd)
	{
		if (!NpcReady())
			return;

		if (cmd.text == "")
		{
			m_CmdStatus = "failed";
			m_CmdDetail = "wear braucht text=Classname";
			return;
		}

		ItemBase wanted = null;
		array<EntityAI> items = new array<EntityAI>();
		m_Npc.GetInventory().EnumerateInventory(InventoryTraversalType.PREORDER, items);

		foreach (EntityAI ent : items)
		{
			ItemBase item = ItemBase.Cast(ent);
			if (item && item.GetType() == cmd.text)
			{
				wanted = item;
				break;
			}
		}

		if (!wanted)
			wanted = FindNearestGroundItem(cmd.text, 10.0);

		if (!wanted)
		{
			m_CmdStatus = "failed";
			m_CmdDetail = "weder im Inventar noch am Boden (10 m): " + cmd.text;
			return;
		}

		if (TakeToBodySlot(wanted))
		{
			m_CmdStatus = "done";
			m_CmdDetail = cmd.text + " angezogen";
			return;
		}

		// Die eAI-Heuristik wollte nicht selbst tauschen (haelt das getragene
		// Stueck fuer gleichwertig): Tausch erzwingen - der Agent hat es so
		// entschieden. Blocker ausziehen, anziehen; wird der Slot erst im
		// naechsten Frame frei, uebernimmt der 1-Hz-Tick (UpdateWearRetry).
		ItemBase blocker = FindBlockingAttachment(wanted);
		if (blocker)
		{
			string blockerType = blocker.GetType();
			// Das alte Kleidungsstueck NIE einfach fallenlassen - ist es gefuellt,
			// faellt es MIT Inhalt zu Boden und der Inhalt geht verloren (der
			// bekannte Kleidungswechsel-Bug, der z.B. Igor 150 Birnen + Bandagen
			// kostete). Erst KOMPLETT mit Inhalt ins eigene Cargo stauen; geht das
			// nicht, nur ein LEERES Stueck fallenlassen, ein gefuelltes lieber den
			// Wechsel abbrechen lassen (Inhalt bleibt sicher am Koerper).
			string disposal;
			bool moved = false;
			if (m_Npc.GetInventory().TakeEntityToInventory(InventoryMode.SERVER, FindInventoryLocationType.CARGO, blocker))
				moved = true;
			// Verifizieren: sitzt der Blocker NOCH im selben Body-Slot, war der
			// Move nur scheinbar erfolgreich (bekanntes eAI-Muster - TakeEntity
			// liefert true, das attached Stueck bleibt aber im Slot). Dann NICHT
			// faelschlich "verstaut" melden, sonst laeuft TakeToBodySlot unten in
			// die aussichtslose Retry-Schleife "Slot wurde nicht frei".
			bool stillWorn = false;
			if (FindBlockingAttachment(wanted) == blocker)
				stillWorn = true;
			if (moved && !stillWorn)
			{
				disposal = blockerType + " verstaut";
			}
			else if (CargoIsEmpty(blocker))
			{
				if (!m_Npc.eAI_DropItem(blocker, true))
				{
					m_CmdStatus = "failed";
					m_CmdDetail = "konnte " + blockerType + " nicht ausziehen";
					return;
				}
				disposal = blockerType + " liegt am Boden";
			}
			else
			{
				m_CmdStatus = "failed";
				m_CmdDetail = "kein Platz fuers gefuellte " + blockerType + " - erst Inhalt per store_container ins Zelt/Kiste, dann " + cmd.text + " anziehen";
				return;
			}

			if (TakeToBodySlot(wanted))
			{
				m_CmdStatus = "done";
				m_CmdDetail = cmd.text + " angezogen, " + disposal;
				return;
			}

			m_WearPendingItem = wanted;
			m_WearPendingTries = 0;
			m_CmdStatus = "running";
			m_CmdDetail = disposal + ", ziehe " + cmd.text + " an...";
			return;
		}

		// Kein tauschbares Stueck gefunden: Slots zur Diagnose mit ausgeben
		string slotInfo = "";
		TStringArray dbgSlots = new TStringArray();
		wanted.ConfigGetTextArray("inventorySlot", dbgSlots);
		foreach (string dbgSlot : dbgSlots)
			slotInfo = slotInfo + dbgSlot + " ";

		m_CmdStatus = "failed";
		m_CmdDetail = "kein freier Slot fuer " + cmd.text + " und nichts zum Tauschen gefunden (slots: " + slotInfo.Trim() + ")";
	}

	// Bestimmtes Inventar-Item in die Hand nehmen (die Auswahl-Intelligenz
	// liegt in der Taktik-Schicht des Daemons)
	private void CmdEquip(IsuCommand cmd)
	{
		if (!NpcReady())
			return;

		if (cmd.text == "")
		{
			m_CmdStatus = "failed";
			m_CmdDetail = "equip braucht text=Classname";
			return;
		}

		ItemBase wanted = null;
		array<EntityAI> items = new array<EntityAI>();
		m_Npc.GetInventory().EnumerateInventory(InventoryTraversalType.PREORDER, items);

		foreach (EntityAI ent : items)
		{
			ItemBase item = ItemBase.Cast(ent);
			if (item && item.GetType() == cmd.text)
			{
				wanted = item;
				break;
			}
		}

		if (!wanted)
		{
			m_CmdStatus = "failed";
			m_CmdDetail = "nicht im Inventar: " + cmd.text;
			return;
		}

		// Wie CmdEquipBest: gewaehlte Waffe entklemmen/pristine, dann den Async-
		// Retry-Pfad nutzen. Der synchrone Rueckgabewert von eAI_TakeItemToHands
		// ist KEIN Erfolgsbeleg (die Hand wird oft erst im naechsten Frame nach
		// EnsureHandsFree frei) - darum verifiziert UpdateEquipRetry per Tick und
		// meldet erst dann done/failed. Vorher scheiterte equip sofort hart
		// ("eAI_TakeItemToHands fehlgeschlagen"), waehrend equip_best klappte.
		wanted.SetHealth01("", "Health", 1.0);
		Weapon_Base wantedWpn = Weapon_Base.Cast(wanted);
		if (wantedWpn && wantedWpn.IsJammed())
			wantedWpn.SetJammed(false);
		EnsureHandsFree(wanted);
		m_Npc.eAI_TakeItemToHands(wanted, true);
		m_EquipPendingItem = wanted;
		m_EquipPendingTries = 0;
		m_CmdDetail = cmd.text;
		m_CmdStatus = "running";
	}

	private void CmdEngage(IsuCommand cmd)
	{
		if (!NpcReadyOnFoot())
			return;

		// Naechster Infizierter wird Ziel: als Target registrieren (Awareness)
		// UND hinlaufen — erst auf kurze Distanz steigt der Threat-Level genug,
		// dass das eAI-Kampfsystem uebernimmt.
		EntityAI target = FindNearestHostile(100.0);
		if (!target)
		{
			m_CmdStatus = "failed";
			m_CmdDetail = "kein Gegner (Infizierter/Raubtier) in 100 m";
			return;
		}

		m_Slinged = false;   // Kampf laeuft mit eAI-Default-Tempo, nicht Dauer-Sprint

		eAITargetInformation info = eAITargetInformation.GetTargetInformation(target);
		if (info)
			info.InsertAI(m_Npc);

		m_EngageTarget = target;

		if (!StartWalk(target.GetPosition()))
			return;

		m_CmdStatus = "running";
		m_CmdDetail = target.GetType();
	}

	private void CmdFlee(IsuCommand cmd)
	{
		if (!NpcReadyOnFoot())
			return;

		vector from = vector.Zero;
		bool hasFrom = false;

		EntityAI threat = FindNearestHostile(100.0);
		if (threat)
		{
			from = threat.GetPosition();
			hasFrom = true;
		}
		else if (cmd.x != 0 || cmd.z != 0)
		{
			from = ResolvePos(cmd);
			hasFrom = true;
		}

		vector myPos = m_Npc.GetPosition();
		vector away;
		if (!hasFrom)
		{
			// keine Bedrohung bekannt: in Blickrichtung 150 m
			away = myPos + m_Npc.GetDirection() * 150.0;
		}
		else
		{
			vector dir = myPos - from;
			dir[1] = 0;
			dir.Normalize();
			away = myPos + dir * 150.0;
		}

		away[1] = GetGame().SurfaceY(away[0], away[2]);

		if (!StartWalk(away))
			return;

		// Sprinten bis zur Ankunft, UpdateRunningCommand setzt zurueck
		m_Npc.SetMovementSpeedLimits(3.0, 3.0);
		m_CmdStatus = "running";
	}

	private void CmdAdoptNearest()
	{
		if (m_Npc && m_Npc.IsAlive())
		{
			m_CmdStatus = "failed";
			m_CmdDetail = "npc existiert bereits";
			return;
		}

		array<eAIBase> allAI = eAIBase.eAI_GetAll();
		eAIBase nearest = null;
		foreach (eAIBase ai : allAI)
		{
			if (!ai || !ai.IsAlive())
				continue;
			// Nur herrenlose Survivor uebernehmen - keine Trader-/Quest-NPCs
			// (z.B. ExpansionP2PTraderAI* vom Market-Modul)
			if (!ai.GetType().Contains("eAI_Survivor"))
				continue;
			// und keine Agenten anderer Slots!
			if (IsuAgentRegistry.IsAgent(ai))
				continue;
			nearest = ai;
			break;
		}

		if (!nearest)
		{
			m_CmdStatus = "failed";
			m_CmdDetail = "keine lebende eAI auf dem Server";
			return;
		}

		m_Npc = nearest;
		IsuAgentRegistry.Register(nearest, m_NpcName);
		m_CmdStatus = "done";
		m_CmdDetail = nearest.GetType();
	}

	// ------------------------------------------------------------ Sozialles

	private void CmdSay(IsuCommand cmd)
	{
		if (!NpcReady())
			return;

		if (cmd.text == "")
		{
			m_CmdStatus = "failed";
			m_CmdDetail = "say braucht text";
			return;
		}

		// Comic-Sprechblase: gesagten Text an alle Clients funken (das HUD zeigt
		// ihn als Blase ueber dem Kopf, wenn Comic-Chat im Menue aktiv ist).
		BroadcastSpeech(cmd.text);

#ifdef EXPANSIONMODCHAT
		// Expansion-Chat: erscheint bei Spielern in Rufweite (60 m) als normale
		// Direct-Chat-Zeile. ChatMP (vanilla) stellt seit Jahren nicht zu (T150586).
		ExpansionGlobalChatModule chatModule = ExpansionGlobalChatModule.s_Instance;
		if (!chatModule)
		{
			m_CmdStatus = "failed";
			m_CmdDetail = "ExpansionGlobalChatModule nicht initialisiert";
			return;
		}

		ExpansionChatMessageEventParams data = new ExpansionChatMessageEventParams(
			ExpansionChatChannels.CCDirect, m_NpcName, cmd.text, "");

		auto rpc = chatModule.Expansion_CreateRPC("RPC_AddChatMessage");
		rpc.Write(data);
		// GROSSER Radius = an ALLE Spieler auf der Karte zustellen, nicht nur
		// in 60 m. So siehst du die ganze NPC-Konversation im Chat, auch von
		// weit verstreuten NPCs (frueher waren weggelaufene NPCs unsichtbar).
		m_Npc.Expansion_SendNear(rpc, m_Npc.GetPosition(), 1000000.0, null, true);

		// Serverseitig in die Mission funneln (Logging/ADM wie Expansion selbst)
		g_Game.GetMission().OnEvent(ChatMessageEventTypeID, data);

		// Agenten in Hoerweite (60 m) bekommen die Aussage in ihren Chat-Ring -
		// so hoeren sich die NPCs gegenseitig (inkl. des Sprechers selbst)
		DeliverAgentChat(cmd.text);

		m_CmdStatus = "done";
		m_CmdDetail = "chat an alle Spieler (global)";
#else
		// Fallback ohne Expansion-Chat: engine-natives ChatMP (Zustellung in
		// DayZ 1.29 unbestaetigt, Bohemia-Ticket T150586)
		string line = m_NpcName + ": " + cmd.text;
		vector myPos = m_Npc.GetPosition();

		array<Man> players = new array<Man>();
		GetGame().GetPlayers(players);

		int sent = 0;
		foreach (Man man : players)
		{
			PlayerBase pb = PlayerBase.Cast(man);
			if (!pb || !pb.GetIdentity())
				continue;
			if (vector.Distance(myPos, pb.GetPosition()) > 60.0)
				continue;

			GetGame().ChatMP(man, line, "colorAction");
			sent++;
		}

		DeliverAgentChat(cmd.text);

		m_CmdStatus = "done";
		m_CmdDetail = "an " + sent.ToString() + " Spieler in Rufweite (ChatMP)";
#endif
	}

	// Comic-Sprechblase: NetworkID + gesagter Text an alle Clients (RPC_SAY).
	// Das IsuVoice-HUD speichert es zeitgestempelt und zeigt eine Blase fuer ~6s.
	private void BroadcastSpeech(string text)
	{
		int low, high;
		m_Npc.GetNetworkID(low, high);
		if (low == 0 && high == 0)
			return;
		Param3<int, int, string> data = new Param3<int, int, string>(low, high, text);
		array<Man> players = new array<Man>();
		GetGame().GetPlayers(players);
		foreach (Man man : players)
		{
			PlayerBase pb = PlayerBase.Cast(man);
			if (pb && pb.GetIdentity())
				GetGame().RPCSingleParam(pb, RPC_SAY, data, true, pb.GetIdentity());
		}
	}

	// Sprechblase-only: nur den Text als RPC funken, KEIN Chat. Der Daemon ruft
	// das auf, wenn Discord-Voice aktiv ist - dann spricht der NPC per Voice und
	// die Comic-Blase erscheint trotzdem (Comic-Chat an = hoeren UND sehen).
	private void CmdBubble(IsuCommand cmd)
	{
		if (!NpcReady())
			return;
		if (cmd.text == "")
		{
			m_CmdStatus = "failed";
			m_CmdDetail = "bubble braucht text";
			return;
		}
		BroadcastSpeech(cmd.text);
		m_CmdStatus = "done";
		m_CmdDetail = "bubble gefunkt";
	}

	private void CmdSayVoice(IsuCommand cmd)
	{
		if (!NpcReady())
			return;

		if (cmd.text == "")
		{
			m_CmdStatus = "failed";
			m_CmdDetail = "say_voice braucht text=SoundSet-Name";
			return;
		}

		// RPC an alle Spieler in Hoerweite: der IsuVoice-Client-Receiver spielt
		// das SoundSet 3D an der NPC-Position ab
		vector pos = m_Npc.GetPosition();
		Param2<string, vector> data = new Param2<string, vector>(cmd.text, pos);

		array<Man> players = new array<Man>();
		GetGame().GetPlayers(players);

		int sent = 0;
		foreach (Man man : players)
		{
			PlayerBase pb = PlayerBase.Cast(man);
			if (!pb || !pb.GetIdentity())
				continue;
			if (vector.Distance(pos, pb.GetPosition()) > 80.0)
				continue;

			GetGame().RPCSingleParam(pb, RPC_PLAY_VOICE, data, true, pb.GetIdentity());
			sent++;
		}

		m_CmdStatus = "done";
		m_CmdDetail = cmd.text + " an " + sent.ToString() + " Spieler";
	}

	static ExpansionMarkerModule s_MarkerModule;

	// Agentennamen + Marker pflegen (alle 3 s via CallLater aus
	// MissionServer.OnInit):
	//  1. NetworkID + Name an alle Clients funken - fuettert im IsuVoice-
	//     Client GetDisplayName(), damit die Expansion-Nametags beim
	//     Anvisieren "Viktor" statt des Skin-Namens zeigen.
	//  2. Expansion-Server-Marker (Karte + 3D-Welt, wie Trader-Marker) auf
	//     jeden lebenden Agenten setzen. Feste UID pro Name: Remove+Create
	//     wirkt als Positions-Update.
	// Slot-Index aus der Agenten-Id (feste Roster-Reihenfolge). 7 = unbekannt
	// (dynamisch entdeckter Slot) -> Client nutzt eine neutrale Farbe.
	static int SlotIndex(string id)
	{
		if (id == "viktor") return 0;
		if (id == "birgit") return 1;
		if (id == "igor") return 2;
		if (id == "konrad") return 3;
		return 7;
	}

	// Bridge-Instanz zu einem NPC finden (fuer Aktion/Slot im Nameplate-Feed).
	static IsuBridge FindByNpc(eAIBase ai)
	{
		foreach (string idB, IsuBridge instB : s_Instances)
		{
			if (instB.m_Npc == ai)
				return instB;
		}
		return null;
	}

	// Kurzes Aktions-Label fuers Namensschild. 0=kaempft 1=lootet 2=folgt
	// 3=geht 4=wartet. Der Client mappt die Id auf den Anzeigetext.
	int ActionLabelId()
	{
		if (m_CmdAction == "engage")
			return 0;
		if (m_CmdStatus == "running" && (m_CmdAction == "loot_corpse" || m_CmdAction == "loot_container" || m_CmdAction == "pickup" || m_CmdAction == "harvest"))
			return 1;
		if (m_Following || m_CmdAction == "follow")
			return 2;
		if (m_CmdStatus == "running" && (m_CmdAction == "move_to" || m_CmdAction == "flee" || m_CmdAction == "regroup"))
			return 3;
		return 4;
	}

	static void BroadcastNametags()
	{
		if (IsuAgentRegistry.s_Npcs.Count() == 0)
			return;

		if (!s_MarkerModule)
			CF_Modules<ExpansionMarkerModule>.Get(s_MarkerModule);

		// Tote/verschwundene Koerper aus der Registry fegen (ihr Marker
		// geht mit; der Respawn registriert den Nachfolger und der
		// naechste Tick setzt den Marker neu)
		array<eAIBase> dead = new array<eAIBase>();
		foreach (eAIBase ai0, string name0 : IsuAgentRegistry.s_Npcs)
		{
			if (!ai0 || !ai0.IsAlive())
				dead.Insert(ai0);
		}
		if (dead.Count() > 0)
		{
			array<Man> rplayers = new array<Man>();
			GetGame().GetPlayers(rplayers);
			foreach (eAIBase d : dead)
			{
				string dname = IsuAgentRegistry.AgentName(d);
				if (s_MarkerModule)
					s_MarkerModule.RemoveServerMarker("isu_agent_" + dname);
				// Client: Namensschild der Leiche entfernen (packed = -1 = remove),
				// sonst klebt es an der Leiche, bis sie despawnt, statt auf den
				// Respawn ueberzugehen.
				if (d)
				{
					int dlow, dhigh;
					d.GetNetworkID(dlow, dhigh);
					if (!(dlow == 0 && dhigh == 0))
					{
						Param5<int, int, string, int, int> rm = new Param5<int, int, string, int, int>(dlow, dhigh, dname, -1, 4);
						foreach (Man rman : rplayers)
						{
							PlayerBase rpb = PlayerBase.Cast(rman);
							if (rpb && rpb.GetIdentity())
								GetGame().RPCSingleParam(rpb, RPC_NAMETAG, rm, true, rpb.GetIdentity());
						}
					}
				}
				IsuAgentRegistry.s_Npcs.Remove(d);
			}
		}

		array<Man> players = new array<Man>();
		GetGame().GetPlayers(players);

		foreach (eAIBase ai, string name : IsuAgentRegistry.s_Npcs)
		{
			if (s_MarkerModule)
			{
				string uid = "isu_agent_" + name;
				s_MarkerModule.RemoveServerMarker(uid);
				if (ai && ai.IsAlive())
				{
					// Exakt das Muster der Default-Trader-Marker: das
					// Convenience-CreateServerMarker setzt KEINE Visibility,
					// der Marker bleibt damit unsichtbar (Bitmaske 0).
					ExpansionMapSettings mapSettings = GetExpansionSettings().GetMap();
					if (mapSettings)
					{
						ExpansionServerMarkerData marker = new ExpansionServerMarkerData(uid);
						marker.Set3D(true);
						marker.SetName(name);
						marker.SetIconName("Persona");
						marker.SetColor(ARGB(255, 80, 220, 120));
						marker.SetPosition(ai.GetPosition());
						marker.SetVisibility(EXPANSION_MARKER_VIS_WORLD | EXPANSION_MARKER_VIS_MAP);
						mapSettings.AddServerMarker(marker);
					}
				}
			}

			if (!ai || !ai.IsAlive())
				continue;
			int low, high;
			ai.GetNetworkID(low, high);  // proto void - kein Rueckgabewert
			if (low == 0 && high == 0)
				continue;

			// HP (0..100), Slot (Identitaetsfarbe) und Aktion fuers Namensschild.
			// hp und slot in EINEN int gepackt (Param hat max 5 Felder):
			// packed = hp*8 + slot, slot 0..7. Client entpackt: hp=packed/8,
			// slot=packed%8.
			float hpF = ai.GetHealth("GlobalHealth", "Health");
			int hp = Math.Round(hpF);
			if (hp < 0)
				hp = 0;
			if (hp > 100)
				hp = 100;
			int slot = 7;
			int actionId = 4;
			IsuBridge inst = FindByNpc(ai);
			if (inst)
			{
				slot = SlotIndex(inst.m_Id);
				actionId = inst.ActionLabelId();
			}
			int packed = hp * 8 + slot;

			Param5<int, int, string, int, int> data = new Param5<int, int, string, int, int>(low, high, name, packed, actionId);

			// Gedanken-HUD: Absicht (falls gesetzt) zusaetzlich per RPC_INTENT
			string intentLine = "";
			if (inst)
				intentLine = inst.m_Intent;
			Param3<int, int, string> intentData = null;
			if (intentLine != "")
				intentData = new Param3<int, int, string>(low, high, intentLine);

			foreach (Man man : players)
			{
				PlayerBase pb = PlayerBase.Cast(man);
				if (!pb || !pb.GetIdentity())
					continue;
				GetGame().RPCSingleParam(pb, RPC_NAMETAG, data, true, pb.GetIdentity());
				if (intentData)
					GetGame().RPCSingleParam(pb, RPC_INTENT, intentData, true, pb.GetIdentity());
			}
		}
	}

	// Supervisor-Status (arena_status.txt, geschrieben von
	// arena_supervisor.py) bei Aenderung an alle Clients funken -
	// das In-Game-Menue zeigt ihn in der Statuszeile.
	static string s_LastArenaStatus = "";

	static void TickArenaStatus()
	{
		FileHandle fh = OpenFile("$profile:IsuSurvivor/arena_status.txt", FileMode.READ);
		if (fh == 0)
			return;
		string line;
		FGets(fh, line);
		CloseFile(fh);

		if (line == "" || line == s_LastArenaStatus)
			return;
		s_LastArenaStatus = line;

		array<Man> players = new array<Man>();
		GetGame().GetPlayers(players);
		Param1<string> data = new Param1<string>(line);
		foreach (Man man : players)
		{
			PlayerBase pb = PlayerBase.Cast(man);
			if (!pb || !pb.GetIdentity())
				continue;
			GetGame().RPCSingleParam(pb, ISUSRV_RPC_ARENA_STATUS, data, true, pb.GetIdentity());
		}
	}

	// Verwaiste Agenten-Marker entfernen (z.B. nach Server-Crash aus den
	// persistierten MapSettings) - einmalig beim Missionsstart.
	static void CleanupAgentMarkers()
	{
		if (!s_MarkerModule)
			CF_Modules<ExpansionMarkerModule>.Get(s_MarkerModule);
		if (!s_MarkerModule)
			return;

		ExpansionMapSettings mapSettings = GetExpansionSettings().GetMap();
		if (!mapSettings)
			return;

		TStringArray stale = new TStringArray();
		foreach (ExpansionMarkerData marker : mapSettings.ServerMarkers)
		{
			if (marker && marker.GetUID().IndexOf("isu_agent_") == 0)
				stale.Insert(marker.GetUID());
		}
		foreach (string uid : stale)
			s_MarkerModule.RemoveServerMarker(uid);

		if (stale.Count() > 0)
			Print("[IsuSurvivor] " + stale.Count().ToString() + " verwaiste Agenten-Marker entfernt");
	}

	// Eigene Aussage an ALLE Agenten zustellen - Funkgeraete-Fiktion, wie
	// beim Spieler-Chat (OnChatAll). Mit dem alten 60-m-Limit verstummte
	// die Gruppe, sobald sie sich zum Looten verteilte.
	private void DeliverAgentChat(string text)
	{
		if (!m_Npc)
			return;

		foreach (string otherId, IsuBridge other : s_Instances)
		{
			if (!other.m_Npc || !other.m_Npc.IsAlive())
				continue;
			other.OnChat(4, m_NpcName, text);
		}
	}

	private void CmdFollow(IsuCommand cmd)
	{
		if (!NpcReady())
			return;

		PlayerBase target = null;
		float nearest = 181.0;   // bis ~180 m: eine wandernde Loot-Kolonne verliert follow sonst staendig (100 m zu eng)

		array<Man> players = new array<Man>();
		GetGame().GetPlayers(players);

		foreach (Man man : players)
		{
			PlayerBase pb = PlayerBase.Cast(man);
			if (!pb || !pb.GetIdentity())
				continue;
			if (cmd.text != "" && pb.GetIdentity().GetName() != cmd.text)
				continue;

			float d = vector.Distance(m_Npc.GetPosition(), pb.GetPosition());
			if (d < nearest)
			{
				nearest = d;
				target = pb;
			}
		}

		if (!target)
		{
			// Kein (menschlicher) Spieler gefunden: einem NPC-KAMERADEN per Name
			// folgen. eAI-NPCs haben keine Identity und fielen oben raus - darum
			// scheiterte "follow <NPC-Name>" bisher ausnahmslos und der Squad
			// konnte ohne Menschen nicht zusammenbleiben. Wir treten der Gruppe
			// des Kameraden bei (eAI-Formation laesst uns ihm in Formation folgen).
			if (cmd.text != "")
			{
				eAIBase mate = null;
				float nearestMate = 181.0;
				foreach (eAIBase ai, string nm : IsuAgentRegistry.s_Npcs)
				{
					if (!ai || ai == m_Npc || !ai.IsAlive())
						continue;
					if (nm != cmd.text)
						continue;
					float dm = vector.Distance(m_Npc.GetPosition(), ai.GetPosition());
					if (dm < nearestMate)
					{
						nearestMate = dm;
						mate = ai;
					}
				}

				if (mate)
				{
					eAIGroup mgroup = mate.GetGroup();
					if (!mgroup)
						mgroup = eAIGroup.GetGroupByLeader(mate, true, CreateFactionByName("civilian"));
					if (mgroup)
					{
						m_Npc.DisableSimulation(false);
						m_Npc.SetGroup(mgroup);
						m_Npc.SetMovementSpeedLimits(2.0, 3.0);
						m_Following = true;
						m_CmdStatus = "done";
						m_CmdDetail = "folge Kamerad " + cmd.text;
						return;
					}
				}
			}

			// Kein exakter Namensmatch und kein Kamerad. Weicht der Funk-/Voice-
			// Name vom DayZ-Profilnamen ab (Voice-Name vs. Profilname im Spiel),
			// scheiterte follow hier. Ist genau EIN menschlicher
			// Spieler (mit Identity; eAI haben keine) in Reichweite, ist er
			// eindeutig gemeint - ihm folgen statt zu scheitern.
			PlayerBase soleHuman = null;
			int humanCount = 0;
			foreach (Man man2 : players)
			{
				PlayerBase pb2 = PlayerBase.Cast(man2);
				if (!pb2 || !pb2.GetIdentity())
					continue;
				if (vector.Distance(m_Npc.GetPosition(), pb2.GetPosition()) > 180.0)
					continue;
				humanCount = humanCount + 1;
				soleHuman = pb2;
			}

			if (humanCount == 1 && soleHuman)
			{
				target = soleHuman;
			}
			else
			{
				m_CmdStatus = "failed";
				m_CmdDetail = "weder Spieler noch Kamerad '" + cmd.text + "' in 180 m";
				return;
			}
		}

		// Gruppenbeitritt: der NPC wird Mitglied der Spielergruppe und folgt
		// dem Leader in Formation (eAI FollowFormation-FSM). Faction explizit
		// auf civilian: hat der Spieler noch KEINE Gruppe, faellt
		// GetGroupByLeader sonst auf den eAIFactionRaiders-Default
		// (eAIGroup.c) zurueck und die Spielergruppe waere feindlich.
		eAIGroup group = eAIGroup.GetGroupByLeader(target, true, CreateFactionByName("civilian"));
		if (!group)
		{
			m_CmdStatus = "failed";
			m_CmdDetail = "keine Gruppe fuer Spieler erzeugbar";
			return;
		}

		m_Npc.DisableSimulation(false);
		m_Npc.SetGroup(group);
		// Alte HALT-Wegpunkte abraeumen (von einem frueheren stop), sonst
		// folgt der NPC zwar laut Status, bewegt sich aber keinen Meter.
		group.ClearWaypoints();
		m_Npc.SetMovementSpeedLimits(2.0, 3.0);
		m_Following = true;

		m_CmdStatus = "done";
		m_CmdDetail = "folge " + target.GetIdentity().GetName();
	}

	private void CmdUnfollow()
	{
		if (!NpcReady())
			return;

		EnsureOwnGroup();

		eAIGroup group = m_Npc.GetGroup();
		if (group)
		{
			group.ClearWaypoints();
			group.AddWaypoint(m_Npc.GetPosition());
		}

		m_CmdStatus = "done";
	}

	// Fahrzeug-Regel Teil 2: gewollter Ausstieg. Gibt den Ausstieg im
	// 4_World-Patch frei und verlaesst die Fahrergruppe, damit die
	// Sitz-Bleib-Bedingung faellt und das FSM den Ausstieg faehrt.
	private void CmdVehicleExit()
	{
		if (!NpcReady())
			return;

		if (!m_Npc.IsInTransport())
		{
			m_CmdStatus = "failed";
			m_CmdDetail = "nicht in einem Fahrzeug";
			return;
		}

		IsuAgentRegistry.SetVehicleExit(m_Npc, true);
		EnsureOwnGroup();

		m_CmdStatus = "done";
		m_CmdDetail = "Ausstieg eingeleitet";
	}

	// Item direkt ins Inventar erzeugen (Inventar-Wiederherstellung nach
	// Respawn; die Steuerung liegt im Runner)
	private void CmdGiveItem(IsuCommand cmd)
	{
		if (!NpcReady())
			return;

		if (cmd.text == "")
		{
			m_CmdStatus = "failed";
			m_CmdDetail = "give_item braucht text=Classname";
			return;
		}

		EntityAI created = m_Npc.GetInventory().CreateInInventory(cmd.text);
		if (!created)
		{
			m_CmdStatus = "failed";
			m_CmdDetail = "kein Platz oder unbekannt: " + cmd.text;
			return;
		}

		m_CmdStatus = "done";
		m_CmdDetail = cmd.text;
	}

	// Item direkt an einen anderen Survivor uebergeben (Inventar-zu-Inventar,
	// ohne Boden-Zwischenstation - viel zuverlaessiger als drop+pickup).
	// cmd.text = "Zielname|Classname".
	private void CmdHandOver(IsuCommand cmd)
	{
		if (!NpcReady())
			return;

		// Am LETZTEN "|" trennen: Classnames enthalten nie "|", Agentennamen
		// koennten es (Menue erlaubt freie Namen) - so bleibt der Name intakt.
		int sep = cmd.text.LastIndexOf("|");
		if (sep < 0)
		{
			m_CmdStatus = "failed";
			m_CmdDetail = "hand_over braucht text=Zielname|Classname";
			return;
		}
		string targetName = cmd.text.Substring(0, sep);
		string wantedType = cmd.text.Substring(sep + 1, cmd.text.Length() - sep - 1);

		eAIBase receiver = IsuAgentRegistry.FindByName(targetName);
		if (!receiver)
		{
			m_CmdStatus = "failed";
			m_CmdDetail = "kein lebender Survivor namens '" + targetName + "'";
			return;
		}
		if (vector.Distance(m_Npc.GetPosition(), receiver.GetPosition()) > 12.0)
		{
			m_CmdStatus = "failed";
			m_CmdDetail = targetName + " ist zu weit weg (max 12 m fuer Uebergabe)";
			return;
		}

		ItemBase wanted = null;
		array<EntityAI> items = new array<EntityAI>();
		m_Npc.GetInventory().EnumerateInventory(InventoryTraversalType.PREORDER, items);
		foreach (EntityAI ent : items)
		{
			ItemBase it = ItemBase.Cast(ent);
			if (it && it.GetType() == wantedType && !it.GetHierarchyParent())
			{
				wanted = it;
				break;
			}
		}
		if (!wanted)
		{
			m_CmdStatus = "failed";
			m_CmdDetail = "nicht (frei) im Inventar: " + wantedType;
			return;
		}

		if (receiver.eAI_TakeItemToInventory(wanted, false))
		{
			m_CmdStatus = "done";
			m_CmdDetail = wantedType + " an " + targetName + " uebergeben";
		}
		else
		{
			m_CmdStatus = "failed";
			m_CmdDetail = targetName + " hat keinen Platz fuer " + wantedType;
		}
	}

	// Waffe schultern (in den Ruecken/Schulter-Slot) und auf Sprinttempo
	// gehen - fuer lange Wege ohne Gefahr. Bei Gefahr zieht der Daemon per
	// "unsling" (= equip_best) automatisch wieder die Waffe.
	private void CmdSling()
	{
		if (!NpcReadyOnFoot())
			return;

		ItemBase inHands = ItemBase.Cast(m_Npc.GetHumanInventory().GetEntityInHands());
		if (inHands && !Weapon_Base.Cast(inHands))
		{
			// nichts (Waffen-)maessiges in der Hand - nur Tempo hochsetzen
			inHands = null;
		}

		bool stowed = false;
		if (inHands)
		{
			if (!m_Npc.eAI_TakeItemToInventory(inHands, false))
			{
				m_CmdStatus = "failed";
				m_CmdDetail = "kein Platz zum Schultern (Ruecken/Schulter belegt?)";
				return;
			}
			stowed = true;
		}

		m_Slinged = true;                        // haelt das Sprinttempo ueber Folge-Wege
		m_Npc.SetMovementSpeedLimits(3.0, 3.0);  // Sprint freigeben
		m_CmdStatus = "done";
		if (stowed)
			m_CmdDetail = "Waffe geschultert, schnelles Tempo";
		else
			m_CmdDetail = "Haende frei, schnelles Tempo";
	}

	// Gegenstueck zu sling: Waffe wieder ziehen UND das Sprinttempo
	// zuruecknehmen (sonst rennt der NPC dauerhaft, hoher Verbrauch).
	private void CmdUnsling()
	{
		m_Slinged = false;
		CmdEquipBest();
		if (m_Npc)
			m_Npc.SetMovementSpeedLimits(2.0, 3.0);
	}

	// Zur aktuellen Position des gefolgten Spielers laufen (gegen
	// Koordinaten-Tippfehler). Nimmt den menschlichen Gruppenleader, sonst
	// den im Namen genannten / naechsten Spieler.
	private void CmdRegroup(IsuCommand cmd)
	{
		if (!NpcReadyOnFoot())
			return;

		PlayerBase target = null;
		eAIGroup group = m_Npc.GetGroup();
		if (group)
		{
			PlayerBase leader = PlayerBase.Cast(group.GetLeader());
			if (leader && leader.GetIdentity() && leader != m_Npc)
				target = leader;
		}

		if (!target)
		{
			float nearest = 100000.0;
			array<Man> players = new array<Man>();
			GetGame().GetPlayers(players);
			foreach (Man man : players)
			{
				PlayerBase pb = PlayerBase.Cast(man);
				if (!pb || !pb.GetIdentity() || pb == m_Npc)
					continue;
				if (cmd.text != "" && pb.GetIdentity().GetName() != cmd.text)
					continue;
				float d = vector.Distance(m_Npc.GetPosition(), pb.GetPosition());
				if (d < nearest)
				{
					nearest = d;
					target = pb;
				}
			}
		}

		if (!target)
		{
			m_CmdStatus = "failed";
			m_CmdDetail = "kein Spieler zum Sammeln gefunden";
			return;
		}

		if (Dist2D(m_Npc.GetPosition(), target.GetPosition()) <= 4.0)
		{
			m_CmdStatus = "done";
			m_CmdDetail = "schon bei " + target.GetIdentity().GetName();
			return;
		}

		if (!StartWalk(target.GetPosition()))
			return;
		m_CmdStatus = "running";
		m_CmdDetail = "auf dem Weg zu " + target.GetIdentity().GetName();
	}

	// Naechste Tuer oeffnen oder schliessen. text = "open" (Default) | "close".
	// Hinweis: Beim Pathfinding oeffnet die eAI Tueren selbst; dieser Befehl
	// ist fuer gezieltes Oeffnen/Schliessen (z.B. "mach die Tuer zu").
	private void CmdDoor(IsuCommand cmd)
	{
		if (!NpcReadyOnFoot())
			return;

		bool wantOpen = (cmd.text != "close");

		Building bestBuilding = null;
		int bestIndex = -1;
		float bestDist = 5.1;

		array<Object> objects = new array<Object>();
		array<CargoBase> cargos = new array<CargoBase>();
		GetGame().GetObjectsAtPosition3D(m_Npc.GetPosition(), 15.0, objects, cargos);

		foreach (Object obj : objects)
		{
			Building building = Building.Cast(obj);
			if (!building || building.GetDoorCount() == 0)
				continue;

			int doorIndex = building.GetNearestDoorBySoundPos(m_Npc.GetPosition());
			if (doorIndex < 0)
				continue;

			vector doorPos = building.GetDoorSoundPos(doorIndex);
			float dist = vector.Distance(m_Npc.GetPosition(), doorPos);
			if (dist < bestDist)
			{
				bestDist = dist;
				bestBuilding = building;
				bestIndex = doorIndex;
			}
		}

		if (!bestBuilding)
		{
			m_CmdStatus = "failed";
			m_CmdDetail = "keine Tuer in 5 m - erst zum Gebaeude laufen";
			return;
		}

		if (bestBuilding.IsDoorLocked(bestIndex))
		{
			m_CmdStatus = "failed";
			m_CmdDetail = "Tuer ist versperrt";
			return;
		}

		bool isOpen = bestBuilding.IsDoorOpen(bestIndex);

		if (wantOpen)
		{
			if (isOpen)
			{
				m_CmdStatus = "done";
				m_CmdDetail = "Tuer war schon offen";
				return;
			}
			bestBuilding.OpenDoor(bestIndex);
			m_CmdStatus = "done";
			m_CmdDetail = "Tuer geoeffnet";
		}
		else
		{
			if (!isOpen)
			{
				m_CmdStatus = "done";
				m_CmdDetail = "Tuer war schon zu";
				return;
			}
			bestBuilding.CloseDoor(bestIndex);
			m_CmdStatus = "done";
			m_CmdDetail = "Tuer geschlossen";
		}
	}

	// ------------------------------------------------- Survival: Wasser/Feuer

	private Object FindNearestWell(float radius)
	{
		array<Object> objects = new array<Object>();
		array<CargoBase> cargos = new array<CargoBase>();
		GetGame().GetObjectsAtPosition3D(m_Npc.GetPosition(), radius, objects, cargos);

		foreach (Object obj : objects)
		{
			if (obj.GetType().Contains("Well_Pump"))
				return obj;
		}
		return null;
	}

	private FireplaceBase FindNearestFireplace(float radius, bool mustBurn)
	{
		array<Object> objects = new array<Object>();
		array<CargoBase> cargos = new array<CargoBase>();
		GetGame().GetObjectsAtPosition3D(m_Npc.GetPosition(), radius, objects, cargos);

		foreach (Object obj : objects)
		{
			FireplaceBase fireplace = FireplaceBase.Cast(obj);
			if (!fireplace)
				continue;
			if (mustBurn && !fireplace.IsBurning())
				continue;
			return fireplace;
		}
		return null;
	}

	// Anzahl Items in einem Objekt (Cargo + Attachments, ohne das Objekt selbst)
	private int CountContents(EntityAI ent)
	{
		if (!ent || !ent.GetInventory())
			return 0;

		int count = 0;
		array<EntityAI> items = new array<EntityAI>();
		ent.GetInventory().EnumerateInventory(InventoryTraversalType.PREORDER, items);
		foreach (EntityAI child : items)
		{
			if (child != ent)
				count++;
		}
		return count;
	}

	private int CountItemType(string classname)
	{
		int count = 0;
		array<EntityAI> items = new array<EntityAI>();
		m_Npc.GetInventory().EnumerateInventory(InventoryTraversalType.PREORDER, items);
		foreach (EntityAI ent : items)
		{
			if (ent.GetType() == classname)
				count++;
		}
		return count;
	}

	private void CmdDrinkWell()
	{
		if (!NpcReadyOnFoot())
			return;

		if (!FindNearestWell(4.0))
		{
			m_CmdStatus = "failed";
			m_CmdDetail = "kein Brunnen in 4 m - erst zum Brunnen laufen (kind=water in der Umgebung)";
			return;
		}

		PlayerConsumeData data = new PlayerConsumeData();
		data.m_Type = EConsumeType.ENVIRO_WELL;
		data.m_Amount = 900;
		data.m_LiquidType = LIQUID_WATER;
		data.m_Agents = 0;

		if (m_Npc.Consume(data))
		{
			m_CmdStatus = "done";
			m_CmdDetail = "am Brunnen getrunken (+900)";
		}
		else
		{
			m_CmdStatus = "failed";
			m_CmdDetail = "Trinken fehlgeschlagen";
		}
	}

	private void CmdFillContainer()
	{
		if (!NpcReadyOnFoot())
			return;

		if (!FindNearestWell(4.0))
		{
			m_CmdStatus = "failed";
			m_CmdDetail = "kein Brunnen in 4 m";
			return;
		}

		Bottle_Base bottle = null;
		array<EntityAI> items = new array<EntityAI>();
		m_Npc.GetInventory().EnumerateInventory(InventoryTraversalType.PREORDER, items);
		foreach (EntityAI ent : items)
		{
			bottle = Bottle_Base.Cast(ent);
			if (bottle)
				break;
		}

		if (!bottle)
		{
			m_CmdStatus = "failed";
			m_CmdDetail = "kein Fluessigkeitsbehaelter im Inventar";
			return;
		}

		bottle.SetLiquidType(LIQUID_WATER);
		bottle.SetQuantity(bottle.GetQuantityMax());

		m_CmdStatus = "done";
		m_CmdDetail = bottle.GetType() + " mit Wasser gefuellt";
	}

	// Material verbrauchen (Crafting-Grundbaustein; Rezeptlogik im Daemon).
	// text = Classname, y = Anzahl Entities (Default 1)
	private void CmdConsumeItem(IsuCommand cmd)
	{
		if (!NpcReady())
			return;

		int wanted = Math.Round(cmd.y);
		if (wanted < 1)
			wanted = 1;

		int removed = 0;
		array<EntityAI> items = new array<EntityAI>();
		m_Npc.GetInventory().EnumerateInventory(InventoryTraversalType.PREORDER, items);
		foreach (EntityAI ent : items)
		{
			if (removed >= wanted)
				break;
			if (ent.GetType() != cmd.text)
				continue;

			ItemBase item = ItemBase.Cast(ent);
			// Piles (stackedUnit "pcs"): quantity = Stueckzahl, teilweise abziehen
			if (item && item.ConfigGetString("stackedUnit") == "pcs" && item.GetQuantity() > 1)
			{
				int avail = Math.Round(item.GetQuantity());
				int take = wanted - removed;
				if (take >= avail)
				{
					GetGame().ObjectDelete(ent);
					removed += avail;
				}
				else
				{
					item.AddQuantity(-take);
					removed += take;
				}
			}
			else
			{
				GetGame().ObjectDelete(ent);
				removed++;
			}
		}

		if (removed < wanted)
		{
			m_CmdStatus = "failed";
			m_CmdDetail = "nur " + removed.ToString() + "/" + wanted.ToString() + "x " + cmd.text;
			return;
		}

		m_CmdStatus = "done";
		m_CmdDetail = removed.ToString() + "x " + cmd.text + " verbraucht";
	}

	private void CmdLightFire()
	{
		if (!NpcReadyOnFoot())
			return;

		FireplaceBase fireplace = FindNearestFireplace(3.0, false);
		if (!fireplace)
		{
			m_CmdStatus = "failed";
			m_CmdDetail = "kein Lagerfeuer in 3 m (erst craft fireplace)";
			return;
		}

		if (fireplace.IsBurning())
		{
			m_CmdStatus = "done";
			m_CmdDetail = "brennt schon";
			return;
		}

		if (CountItemType("Matchbox") == 0 && CountItemType("PetrolLighter") == 0)
		{
			m_CmdStatus = "failed";
			m_CmdDetail = "kein Zuendmittel (Matchbox/PetrolLighter) im Inventar";
			return;
		}

		// Brennmaterial direkt ins Feuer legen (die Taktik-Schicht hat dem NPC
		// dafuer vorher Sticks per consume_item abgezogen)
		fireplace.GetInventory().CreateInInventory("WoodenStick");
		fireplace.GetInventory().CreateInInventory("WoodenStick");
		fireplace.GetInventory().CreateInInventory("WoodenStick");
		fireplace.GetInventory().CreateInInventory("Rag");

		fireplace.StartFire(true);

		if (fireplace.IsBurning())
		{
			m_CmdStatus = "done";
			m_CmdDetail = "Feuer brennt";
		}
		else
		{
			m_CmdStatus = "failed";
			m_CmdDetail = "zuendet nicht (Regen? Brennmaterial?)";
		}
	}

	private void CmdCook()
	{
		if (!NpcReadyOnFoot())
			return;

		if (!FindNearestFireplace(4.0, true))
		{
			m_CmdStatus = "failed";
			m_CmdDetail = "kein brennendes Feuer in 4 m";
			return;
		}

		int cooked = 0;
		array<EntityAI> items = new array<EntityAI>();
		m_Npc.GetInventory().EnumerateInventory(InventoryTraversalType.PREORDER, items);
		foreach (EntityAI ent : items)
		{
			Edible_Base edible = Edible_Base.Cast(ent);
			if (!edible || !edible.GetFoodStage())
				continue;
			if (edible.GetFoodStage().GetFoodStageType() == FoodStageType.RAW)
			{
				edible.GetFoodStage().ChangeFoodStage(FoodStageType.BAKED);
				cooked++;
			}
		}

		if (cooked == 0)
		{
			m_CmdStatus = "failed";
			m_CmdDetail = "nichts Rohes im Inventar";
			return;
		}

		m_CmdStatus = "done";
		m_CmdDetail = cooked.ToString() + " Item(s) gegart";
	}

	// EXPERIMENTELL: Zaun-Rahmen bauen (Basis fuer Basebuilding-Ausbau)
	private void CmdBuildFenceFrame()
	{
		if (!NpcReadyOnFoot())
			return;

		// Staemme aus Inventar ODER vom Boden in 5 m (Logs sind sperrig und
		// liegen in DayZ ohnehin meist am Boden)
		array<EntityAI> logs = new array<EntityAI>();

		array<EntityAI> invItems = new array<EntityAI>();
		m_Npc.GetInventory().EnumerateInventory(InventoryTraversalType.PREORDER, invItems);
		foreach (EntityAI invEnt : invItems)
		{
			if (invEnt.GetType() == "WoodenLog" && logs.Count() < 2)
				logs.Insert(invEnt);
		}

		if (logs.Count() < 2)
		{
			array<Object> objects = new array<Object>();
			array<CargoBase> cargos = new array<CargoBase>();
			GetGame().GetObjectsAtPosition3D(m_Npc.GetPosition(), 5.0, objects, cargos);
			foreach (Object obj : objects)
			{
				ItemBase ground = ItemBase.Cast(obj);
				if (ground && ground.GetType() == "WoodenLog" && !ground.GetHierarchyParent() && logs.Count() < 2)
					logs.Insert(ground);
			}
		}

		if (logs.Count() < 2)
		{
			m_CmdStatus = "failed";
			m_CmdDetail = "braucht 2x WoodenLog (Inventar oder Boden in 5 m)";
			return;
		}

		vector pos = m_Npc.GetPosition() + m_Npc.GetDirection() * 2.0;
		pos[1] = GetGame().SurfaceY(pos[0], pos[2]);

		Fence fence = Fence.Cast(GetGame().CreateObjectEx("Fence", pos, ECE_PLACE_ON_SURFACE));
		if (!fence)
		{
			m_CmdStatus = "failed";
			m_CmdDetail = "Fence-Objekt nicht erzeugbar";
			return;
		}

		foreach (EntityAI usedLog : logs)
		{
			GetGame().ObjectDelete(usedLog);
			fence.GetInventory().CreateAttachment("WoodenLog");
		}

		fence.OnPartBuiltServer(m_Npc, "frame", AT_BUILD_PART);

		m_CmdStatus = "done";
		m_CmdDetail = "Zaun-Rahmen platziert (experimentell)";
	}

	// Gegenstand auf den Boden legen (native eAI-Drop-Routine mit Animation).
	// text = Classname; leer = Item in der Hand.
	private void CmdDrop(IsuCommand cmd)
	{
		if (!NpcReady())
			return;

		if (cmd.text == "")
		{
			if (m_Npc.eAI_DropItemInHands(true))
			{
				m_CmdStatus = "done";
				m_CmdDetail = "Item aus der Hand abgelegt";
			}
			else
			{
				m_CmdStatus = "failed";
				m_CmdDetail = "nichts in der Hand";
			}
			return;
		}

		ItemBase wanted = null;
		array<EntityAI> items = new array<EntityAI>();
		m_Npc.GetInventory().EnumerateInventory(InventoryTraversalType.PREORDER, items);

		foreach (EntityAI ent : items)
		{
			ItemBase item = ItemBase.Cast(ent);
			if (item && item.GetType() == cmd.text)
			{
				wanted = item;
				break;
			}
		}

		if (!wanted)
		{
			m_CmdStatus = "failed";
			m_CmdDetail = "nicht im Inventar: " + cmd.text;
			return;
		}

		// Steckt das Item in einer Waffe (Magazin/Optik)? Dann verlaesst es das
		// Inventar nie - frueher meldete drop trotzdem "done" ("Mag klebt im
		// Inventar"). Klar abweisen statt zu luegen.
		if (Weapon_Base.Cast(wanted.GetHierarchyParent()))
		{
			m_CmdStatus = "failed";
			m_CmdDetail = cmd.text + " steckt in einer Waffe - erst entladen/abbauen";
			return;
		}

		if (m_Npc.eAI_DropItem(wanted, true))
		{
			// Verifizieren: liegt das Item noch im NPC, war der Drop nur
			// scheinbar erfolgreich
			if (wanted && wanted.GetHierarchyRootPlayer() == m_Npc)
			{
				// Reservierung loesen (z.B. Izh43 nach haengender Waffen-Action)
				// und direkt synchron erneut droppen - die Action-Variante oben
				// bleibt sonst stecken.
				InventoryLocation il_w = new InventoryLocation();
				if (wanted.GetInventory().GetCurrentInventoryLocation(il_w))
					m_Npc.GetHumanInventory().ClearInventoryReservationEx(wanted, il_w);
				m_Npc.eAI_DropItemImpl(wanted);
			}
			if (wanted && wanted.GetHierarchyRootPlayer() == m_Npc)
			{
				m_CmdStatus = "failed";
				m_CmdDetail = cmd.text + " haengt im Inventar fest (Reservierung)";
				return;
			}
			// Bot-Drops bekommen sonst nur 300 s Lifetime und despawnen mitten
			// in der Uebergabe - auf 4 h anheben, damit Depots/Uebergaben halten
			if (wanted)
				wanted.SetLifetime(14400);
			m_CmdStatus = "done";
			m_CmdDetail = cmd.text + " abgelegt";
		}
		else
		{
			m_CmdStatus = "failed";
			m_CmdDetail = "eAI_DropItem fehlgeschlagen: " + cmd.text;
		}
	}

	// Befreiung aus festgefahrenen Zustaenden: liegend verkeilt, haengende
	// Action, vergessenes Halt-Flag, Geometrie-Klemmer.
	private void PerformUnstick()
	{
		m_Npc.DisableSimulation(false);
		m_Npc.eAI_SetHalt(false);

		ActionManagerBase am = m_Npc.GetActionManager();
		if (am)
			am.Interrupt();

		m_Npc.Expansion_GetUp();

		// Sanfter Nudge auf die Oberflaeche loest Geometrie-Verkeilungen
		vector pos = m_Npc.GetPosition();
		pos[1] = GetGame().SurfaceY(pos[0], pos[2]) + 0.3;
		m_Npc.SetPosition(pos);
	}

	private void CmdUnstick()
	{
		if (!m_Npc)
		{
			m_CmdStatus = "failed";
			m_CmdDetail = "kein npc vorhanden";
			return;
		}

		PerformUnstick();
		EnsureOwnGroup();

		eAIGroup group = m_Npc.GetGroup();
		if (group)
		{
			group.ClearWaypoints();
			group.AddWaypoint(m_Npc.GetPosition());
			group.SetWaypointBehaviour(eAIWaypointBehavior.HALT);
		}

		m_CmdStatus = "done";
		m_CmdDetail = "aufgerichtet und befreit";
	}

	// Eigene Gruppe sicherstellen (beendet ein laufendes follow). Noetig vor
	// jeder Wegpunkt-Steuerung, sonst wuerden wir die Spielergruppe dirigieren.
	private void EnsureOwnGroup()
	{
		if (!m_Npc)
			return;

		eAIGroup group = m_Npc.GetGroup();
		if (group && group.GetLeader() == m_Npc && !m_Following)
			return;

		eAIGroup own = eAIGroup.CreateGroup(CreateFactionByName(m_Faction));
		m_Npc.SetGroup(own);
		own.SetWaypointBehaviour(eAIWaypointBehavior.HALT);
		m_Following = false;
	}

	// ----------------------------------------------------------- Dev-Helfer

	private void CmdTeleportPlayer(IsuCommand cmd)
	{
		if (!NpcReady())
			return;

		array<Man> players = new array<Man>();
		GetGame().GetPlayers(players);

		PlayerBase target = null;
		foreach (Man man : players)
		{
			PlayerBase pb = PlayerBase.Cast(man);
			if (!pb || !pb.GetIdentity())
				continue;
			if (cmd.text != "" && pb.GetIdentity().GetName() != cmd.text)
				continue;
			target = pb;
			break;
		}

		if (!target)
		{
			m_CmdStatus = "failed";
			m_CmdDetail = "kein verbundener Spieler gefunden (filter: " + cmd.text + ")";
			return;
		}

		vector pos = m_Npc.GetPosition() + "2 0 2";
		pos[1] = GetGame().SurfaceY(pos[0], pos[2]);
		target.SetPosition(pos);

		m_CmdStatus = "done";
		m_CmdDetail = target.GetIdentity().GetName() + " -> npc";
	}

	private void CmdSpawnItem(IsuCommand cmd)
	{
		if (cmd.text == "")
		{
			m_CmdStatus = "failed";
			m_CmdDetail = "spawn_item braucht text=Classname";
			return;
		}

		vector pos;
		if (cmd.x != 0 || cmd.z != 0)
		{
			pos = ResolvePos(cmd);
		}
		else
		{
			if (!NpcReady())
				return;
			pos = m_Npc.GetPosition() + m_Npc.GetDirection() * 1.5;
		}

		Object obj = GetGame().CreateObjectEx(cmd.text, pos, ECE_PLACE_ON_SURFACE);
		if (!obj)
		{
			m_CmdStatus = "failed";
			m_CmdDetail = "CreateObjectEx fehlgeschlagen: " + cmd.text;
			return;
		}

		m_CmdStatus = "done";
		m_CmdDetail = cmd.text;
	}

	private void CmdSpawnInfected(IsuCommand cmd)
	{
		string classname = cmd.text;
		if (classname == "")
			classname = "ZmbM_HermitSkinny_Beige";

		vector pos;
		if (cmd.x != 0 || cmd.z != 0)
		{
			pos = ResolvePos(cmd);
		}
		else
		{
			if (!NpcReady())
				return;
			pos = m_Npc.GetPosition() + m_Npc.GetDirection() * 25.0;
			pos[1] = GetGame().SurfaceY(pos[0], pos[2]);
		}

		// initAI = true, sonst steht der Infizierte hirnlos herum
		Object obj = GetGame().CreateObject(classname, pos, false, true);
		if (!obj)
		{
			m_CmdStatus = "failed";
			m_CmdDetail = "CreateObject fehlgeschlagen: " + classname;
			return;
		}

		m_CmdStatus = "done";
		m_CmdDetail = classname;
	}

	// ------------------------------------------------------ Befehls-Updates

	private void UpdateRunningCommand()
	{
		if (m_CmdStatus != "running")
			return;

		if (m_CmdAction == "move_to" || m_CmdAction == "flee" || m_CmdAction == "pickup" || m_CmdAction == "loot_corpse" || m_CmdAction == "loot_container" || m_CmdAction == "store_container" || m_CmdAction == "harvest" || m_CmdAction == "regroup")
			UpdateWalk();
		else if (m_CmdAction == "engage")
			UpdateEngage();
		else if (m_CmdAction == "wear")
			UpdateWearRetry();
		else if (m_CmdAction == "equip_best" || m_CmdAction == "equip")
			UpdateEquipRetry();
	}

	// equip_best: eAI_TakeItemToHands scheitert oft nur, weil die Hand nach
	// EnsureHandsFree erst im naechsten Frame frei wird - hier (1 Hz) den
	// Waffe-in-die-Hand-Versuch wiederholen, statt sofort aufzugeben.
	private void UpdateEquipRetry()
	{
		// Item WIRKLICH weg (verloren/zerstoert) -> sofort sauber abbrechen.
		if (!m_EquipPendingItem)
		{
			m_CmdStatus = "failed";
			m_CmdDetail = "Equip-Ziel verschwunden";
			return;
		}
		// NPC nur KURZ nicht bereit (frisch gespawnt/animiert direkt nach loot):
		// NICHT faelschlich als "verschwunden" abbrechen, sondern ein paar Ticks
		// warten - genau das liess Konrads vorhandene CZ527 (Logs 20:28) als
		// "Equip-Ziel verschwunden" scheitern, obwohl die Waffe im Inventar lag.
		if (!NpcReady())
		{
			m_EquipPendingTries++;
			if (m_EquipPendingTries >= 12)
			{
				m_CmdStatus = "failed";
				m_CmdDetail = "NPC wurde nicht rechtzeitig bereit fuers Waffe-Ziehen";
				m_EquipPendingItem = null;
			}
			return;
		}

		// ENTSCHEIDEND: verifizieren, dass die Waffe WIRKLICH in der Hand liegt -
		// nicht nur, dass eAI_TakeItemToHands die Aktion angenommen hat. Erst dann
		// "done", sonst meldet equip_best Erfolg mit dem Holzstab in der Hand.
		ItemBase inHands = ItemBase.Cast(m_Npc.GetHumanInventory().GetEntityInHands());
		if (inHands && inHands == m_EquipPendingItem)
		{
			SlingSecondaryWeapons(m_EquipPendingItem);
			m_CmdStatus = "done";
			m_CmdDetail = m_EquipPendingItem.GetType();
			m_EquipPendingItem = null;
			return;
		}

		m_EquipPendingTries++;
		// Noch nicht in der Hand: das Ziehen nur ALLE PAAR Ticks neu anstossen
		// (nicht jeden), damit eine laufende Zieh-Aktion abschliessen kann statt
		// staendig neu zu starten - genau das liess Konrads CZ527 nie in die Hand.
		if (m_EquipPendingTries % 3 == 0)
		{
			EnsureHandsFree(m_EquipPendingItem);
			m_Npc.eAI_TakeItemToHands(m_EquipPendingItem, true);
		}

		// Schwelle 16 (war 8): die Nicht-bereit-Warteticks oben zaehlen denselben
		// Zaehler hoch, also mehr Budget lassen - bleibt unter dem 30s-Python-Timeout.
		if (m_EquipPendingTries >= 16)
		{
			m_CmdStatus = "failed";
			m_CmdDetail = "Waffe liess sich nicht in die Hand nehmen (Hand blockiert?): " + m_EquipPendingItem.GetType();
			m_EquipPendingItem = null;
		}
	}

	// wear: nach dem Ausziehen wird der Koerper-Slot oft erst im naechsten
	// Frame frei - hier (1 Hz) den Anzieh-Versuch wiederholen
	private void UpdateWearRetry()
	{
		if (!m_WearPendingItem)
		{
			m_CmdStatus = "failed";
			m_CmdDetail = "wear-Item verschwunden";
			return;
		}

		if (TakeToBodySlot(m_WearPendingItem))
		{
			m_CmdStatus = "done";
			m_CmdDetail = m_WearPendingItem.GetType() + " angezogen (Slot getauscht)";
			m_WearPendingItem = null;
			return;
		}

		m_WearPendingTries++;
		if (m_WearPendingTries >= 5)
		{
			m_CmdStatus = "failed";
			m_CmdDetail = "Slot wurde nicht frei fuer " + m_WearPendingItem.GetType() + " (" + m_WearDiag.Trim() + ")";
			m_WearPendingItem = null;
		}
	}

	private void UpdateEngage()
	{
		if (!m_Npc || !m_Npc.IsAlive())
		{
			m_CmdStatus = "failed";
			m_CmdDetail = "npc gestorben";
			return;
		}

		if (!m_EngageTarget || !m_EngageTarget.IsAlive())
		{
			m_CmdStatus = "done";
			m_CmdDetail = "Ziel eliminiert";
			m_EngageTarget = null;
			return;
		}

		float now = GetGame().GetTickTime();
		if (now - m_CmdStartTime > 180)
		{
			m_CmdStatus = "failed";
			m_CmdDetail = "Ziel lebt nach 180s noch";
			m_EngageTarget = null;
			return;
		}

		// Waehrend die Kampf-FSM aktiv ist, nicht in die Bewegung pfuschen
		if (m_Npc.m_eAI_IsFightingFSM)
			return;

		// Ziel bewegt sich: Wegpunkt nachziehen, sobald er > 5 m daneben liegt
		vector targetPos = m_EngageTarget.GetPosition();
		if (Dist2D(m_MoveTarget, targetPos) > 5.0)
			StartWalk(targetPos);
	}

	private void UpdateWalk()
	{
		if (!m_Npc || !m_Npc.IsAlive())
		{
			m_CmdStatus = "failed";
			m_CmdDetail = "npc gestorben oder verschwunden";
			ReleaseClaim();   // sonst bleibt das geclaimte Item fuer alle gesperrt
			return;
		}

		// Pickup-Ziel kann zwischenzeitlich verschwinden: jedes eAI-Aufheben
		// ist Clone+Delete, was unsere Referenz nullt, obwohl ein gleiches
		// Item oft weiter daliegt. EINMAL neu suchen, bevor wir aufgeben.
		if (m_CmdAction == "pickup" && !m_PickupItem)
		{
			ItemBase again = FindNearestGroundItem(m_PickupFilter, 50.0);
			if (again)
			{
				m_PickupItem = again;
				ClaimItem(again);
				StartWalk(again.GetPosition());
				return;
			}
			m_CmdStatus = "failed";
			m_CmdDetail = "Item verschwunden (evtl. von jemandem aufgehoben)";
			return;
		}

		float now = GetGame().GetTickTime();
		float dist = Dist2D(m_Npc.GetPosition(), m_MoveTarget);

		float arriveDist = 3.0;
		if (m_CmdAction == "pickup")
			arriveDist = 2.0;

		if (dist < arriveDist)
		{
			OnWalkArrived();
			return;
		}

		if (dist < m_BestDist - 0.5)
		{
			m_BestDist = dist;
			m_LastProgressTime = now;
		}

		// Selbstheilung: nach 20 s ohne Fortschritt EINMAL aufrichten/befreien
		// und den Wegpunkt neu setzen, bevor wir nach 45 s aufgeben
		if (now - m_LastProgressTime > 20 && !m_TriedUnstick)
		{
			m_TriedUnstick = true;
			Print("[IsuSurvivor] auto-unstick (kein Fortschritt, dist=" + dist.ToString() + ")");
			PerformUnstick();
			StartWalk(m_MoveTarget);
			return;
		}

		if (now - m_LastProgressTime > 45)
		{
			m_CmdStatus = "failed";
			m_CmdDetail = "45s ohne Fortschritt, dist=" + dist.ToString();
			RestoreSpeed();
			return;
		}

		if (now - m_CmdStartTime > 600)
		{
			m_CmdStatus = "failed";
			m_CmdDetail = "Timeout nach 600s";
			RestoreSpeed();
		}
	}

	private void OnWalkArrived()
	{
		RestoreSpeed();

		if (m_CmdAction == "pickup")
		{
			DoTakePickupItem();
			return;
		}

		if (m_CmdAction == "loot_corpse" || m_CmdAction == "loot_container")
		{
			DoLootContainer();
			return;
		}

		if (m_CmdAction == "store_container")
		{
			DoStore();
			return;
		}

		if (m_CmdAction == "harvest")
		{
			DoHarvest();
			return;
		}

		m_CmdStatus = "done";
	}

	private void RestoreSpeed()
	{
		if (m_Npc && m_CmdAction == "flee")
			m_Npc.SetMovementSpeedLimits(2.0, 3.0);
	}

	// ----------------------------------------------------------------- State

	private void WriteState()
	{
		m_Seq++;

		IsuState state = new IsuState();
		state.seq = m_Seq;
		state.uptime = GetGame().GetTickTime();
		state.bridge_version = "0.7.1";

		if (m_Npc)
		{
			state.npc.spawned = true;
			state.npc.alive = m_Npc.IsAlive();
			state.npc.classname = m_Npc.GetType();

			vector pos = m_Npc.GetPosition();
			state.npc.pos_x = pos[0];
			state.npc.pos_y = pos[1];
			state.npc.pos_z = pos[2];
			state.npc.heading = m_Npc.GetOrientation()[0];

			state.npc.health = m_Npc.GetHealth("GlobalHealth", "Health");
			state.npc.blood = m_Npc.GetHealth("GlobalHealth", "Blood");
			state.npc.water = m_Npc.GetStatWater().Get();
			state.npc.energy = m_Npc.GetStatEnergy().Get();
			state.npc.heat_comfort = m_Npc.GetStatHeatComfort().Get();
			if (m_Npc.GetStomach())
				state.npc.stomach_volume = m_Npc.GetStomach().GetStomachVolume();
			state.npc.fighting = m_Npc.m_eAI_IsFightingFSM;
			state.npc.name = m_NpcName;
			state.npc.following = m_Following;
			state.npc.unconscious = m_Npc.IsUnconscious();
			state.npc.in_vehicle = m_Npc.IsInTransport();

			EntityAI inHands = m_Npc.GetHumanInventory().GetEntityInHands();
			if (inHands)
				state.npc.in_hands = inHands.GetType();

			CollectInventory(state);
			CollectNearby(state, pos);
		}

		state.command.id = m_CmdId;
		state.command.action = m_CmdAction;
		state.command.status = m_CmdStatus;
		state.command.detail = m_CmdDetail;
		if (m_CmdStatus == "running" && m_Npc)
			state.command.dist_to_target = Dist2D(m_Npc.GetPosition(), m_MoveTarget);

		state.chat = m_Chat;
		state.errors = m_Errors;

		JsonFileLoader<IsuState>.JsonSaveFile(m_StateFile, state);
	}

	private void CollectInventory(IsuState state)
	{
		array<EntityAI> items = new array<EntityAI>();
		m_Npc.GetInventory().EnumerateInventory(InventoryTraversalType.PREORDER, items);

		EntityAI inHands = m_Npc.GetHumanInventory().GetEntityInHands();

		foreach (EntityAI ent : items)
		{
			if (ent == m_Npc)
				continue;

			ItemBase item = ItemBase.Cast(ent);
			if (!item)
				continue;

			IsuItemInfo info = new IsuItemInfo();
			info.classname = item.GetType();
			info.quantity = item.GetQuantity();
			info.health = item.GetHealth("", "");
			info.in_hands = (ent == inHands);
			info.kind = ClassifyItem(item);

			// Steckt das Item IN EINER WAFFE (Magazin/Optik)? Dann den Traeger
			// melden - sonst sieht ein gestecktes Magazin wie ein freies Item
			// aus und drop darauf scheitert lautlos. Items in Taschen/Rucksack
			// sind dagegen normal ablegbar, die NICHT markieren.
			EntityAI parent = item.GetHierarchyParent();
			if (parent && Weapon_Base.Cast(parent))
				info.parent = parent.GetType();

			// Waffen haben keine GetQuantity (stand immer "x0", obwohl
			// geladen) - stattdessen die echte Munition zaehlen:
			// Magazin + internes Magazin + Kammer.
			Weapon_Base weaponItem = Weapon_Base.Cast(item);
			if (weaponItem)
				info.quantity = Isu_WeaponAmmoCount(weaponItem);

			state.inventory.Insert(info);
		}
	}

	// Gesamte Munition einer Waffe: eingestecktes Magazin + internes
	// Magazin (Mosin, Flinten) + Patrone in der Kammer, ueber alle Laeufe.
	private int Isu_WeaponAmmoCount(Weapon_Base weapon)
	{
		int total = 0;
		for (int mi = 0; mi < weapon.GetMuzzleCount(); mi++)
		{
			Magazine mag = weapon.GetMagazine(mi);
			if (mag)
				total += mag.GetAmmoCount();
			total += weapon.GetInternalMagazineCartridgeCount(mi);
			if (weapon.IsChamberFull(mi) && !weapon.IsChamberFiredOut(mi))
				total += 1;
		}
		return total;
	}

	private string ClassifyItem(ItemBase item)
	{
		Edible_Base edible = Edible_Base.Cast(item);
		if (edible)
		{
			if (edible.IsLiquidContainer())
				return "drink";
			return "food";
		}

		if (Weapon_Base.Cast(item))
			return "firearm";

		if (Magazine.Cast(item))
			return "ammo";

		if (Clothing.Cast(item))
			return "clothing";

		return "other";
	}

	private void CollectNearby(IsuState state, vector center)
	{
		array<Object> objects = new array<Object>();
		array<CargoBase> cargos = new array<CargoBase>();
		GetGame().GetObjectsAtPosition3D(center, 100, objects, cargos);

		int itemCount = 0;

		foreach (Object obj : objects)
		{
			if (obj == m_Npc)
				continue;

			IsuEntityInfo info = new IsuEntityInfo();
			info.classname = obj.GetType();

			vector p = obj.GetPosition();
			info.x = p[0];
			info.y = p[1];
			info.z = p[2];
			info.distance = vector.Distance(center, p);

			eAIBase otherAi;
			PlayerBase player;
			DayZInfected infected;
			AnimalBase animal;
			CarScript car;
			FireplaceBase fireplace;
			ItemBase item;

			if (obj.GetType().Contains("Well_Pump"))
			{
				info.kind = "water";
			}
			else if (Class.CastTo(fireplace, obj))
			{
				if (fireplace.IsBurning())
					info.kind = "fire_burning";
				else
					info.kind = "fire";
			}
			else if (Class.CastTo(otherAi, obj) && otherAi.IsAlive())
			{
				info.kind = "ai";
				// Mit-Agenten sind namentlich erkennbar (Arena-Sozialverhalten)
				info.name = IsuAgentRegistry.AgentName(otherAi);
			}
			else if (Class.CastTo(player, obj))
			{
				if (!player.IsAlive())
				{
					info.kind = "corpse";
				}
				else
				{
					info.kind = "player";
					if (player.GetIdentity())
						info.name = player.GetIdentity().GetName();
				}
			}
			else if (Class.CastTo(infected, obj))
			{
				// Tote Infizierte sind lootbare Leichen
				if (!infected.IsAlive())
					info.kind = "corpse";
				else
					info.kind = "infected";
			}
			else if (Class.CastTo(animal, obj))
			{
				// Tote Tiere sind verwertbare Beute (Befehl: harvest)
				if (!animal.IsAlive())
					info.kind = "animal_corpse";
				else
					info.kind = "animal";
			}
			else if (Class.CastTo(car, obj))
			{
				info.kind = "vehicle";
			}
			else if (Class.CastTo(item, obj))
			{
				if (info.distance > 40 || itemCount >= 30)
					continue;
				// nur loses Bodenitem, nichts in Haenden/Inventaren
				if (item.GetHierarchyParent())
					continue;
				info.kind = "item";
				info.item_kind = GroundItemKind(item);
				info.cargo = CountContents(item);
				itemCount++;
			}
			else
			{
				continue;
			}

			if (info.kind == "corpse")
				info.cargo = CountContents(EntityAI.Cast(obj));

			state.nearby.Insert(info);

			if (state.nearby.Count() >= 40)
				break;
		}

		// Nachlauf: Bodenitems markieren, die direkt bei einem anderen Bot
		// liegen (wahrscheinlich dessen frische Ablage, kein freies Loot) -
		// haelt die Bots davon ab, sich gegenseitig zu umkreisen.
		// WICHTIG: Index-Schleifen statt verschachtelter foreach ueber
		// DASSELBE Array - foreach teilt in EnforceScript den Array-Cursor,
		// die innere Schleife wuerde die aeussere verstuempeln.
		int nearCnt = state.nearby.Count();
		for (int oi = 0; oi < nearCnt; oi++)
		{
			IsuEntityInfo it = state.nearby.Get(oi);
			if (it.kind != "item")
				continue;
			for (int ii = 0; ii < nearCnt; ii++)
			{
				IsuEntityInfo other = state.nearby.Get(ii);
				if (other.kind != "ai" || other.name == "")
					continue;
				float dx = it.x - other.x;
				float dz = it.z - other.z;
				if (dx * dx + dz * dz < 4.0)   // < 2 m
				{
					it.near = other.name;
					break;
				}
			}
		}
	}

	// Grob-Klassifikation eines Bodenitems fuers Priorisieren auf der
	// LLM-Seite. Attachments (Optik, Schaft...) extra ausweisen, damit sie
	// nicht als "Waffe" zwischen den Bots zirkulieren.
	private string GroundItemKind(ItemBase item)
	{
		string t = item.GetType();
		// "Light"/"Rail" bewusst NICHT als Marker: faengt sonst Chemlight,
		// Flashlight u.ae. ab (die sollen aufgehoben werden). Waffenlampe
		// gezielt ueber "TLRLight".
		if (t.Contains("Optic") || t.Contains("Bttstck") || t.Contains("Hndgrd") || t.Contains("Suppressor") || t.Contains("Compensator") || t.Contains("Bayonet") || t.Contains("TLRLight") || t.Contains("Buttstock") || t.Contains("Handguard"))
			return "attachment";
		if (Weapon_Base.Cast(item))
			return "firearm";
		if (Magazine.Cast(item))
			return "ammo";
		return ClassifyItem(item);
	}

	// --------------------------------------------------------------- Helpers

	private bool StartWalk(vector target)
	{
		m_MoveTarget = target;

		// Falls der NPC aus einem alten Dormant-Zustand eingefroren ist
		m_Npc.DisableSimulation(false);

		// Wegpunkte steuern immer die EIGENE Gruppe (beendet ein follow)
		EnsureOwnGroup();

		eAIGroup group = m_Npc.GetGroup();
		if (!group)
		{
			m_CmdStatus = "failed";
			m_CmdDetail = "npc hat keine eAIGroup";
			return false;
		}

		group.ClearWaypoints();
		group.AddWaypoint(m_MoveTarget);
		group.SetWaypointBehaviour(eAIWaypointBehavior.ONCE);

		// Tempo nach sling-Zustand: geschultert -> Sprint, sonst normal.
		// (flee/sling ueberschreiben danach selbst, wo Sprint gewollt ist.)
		if (m_Slinged)
			m_Npc.SetMovementSpeedLimits(3.0, 3.0);
		else
			m_Npc.SetMovementSpeedLimits(2.0, 3.0);

		m_CmdStartTime = GetGame().GetTickTime();
		m_LastProgressTime = m_CmdStartTime;
		m_BestDist = Dist2D(m_Npc.GetPosition(), m_MoveTarget);
		return true;
	}

	private ItemBase FindNearestGroundItem(string classnameFilter, float radius)
	{
		array<Object> objects = new array<Object>();
		array<CargoBase> cargos = new array<CargoBase>();
		GetGame().GetObjectsAtPosition3D(m_Npc.GetPosition(), radius, objects, cargos);

		// EXAKTER Classname-Treffer hat Vorrang, sonst ein Teilstring-Treffer
		// (z.B. "WolfSteak" -> "WolfSteakMeat"). Frueher war es ein exakter
		// Vollname-Vergleich: ein ungefaehrer Name fand NICHTS, und ein leerer
		// Filter (Gehirn vertippt den Parameter) griff das naechste BELIEBIGE
		// Item - daher der Bug "pickup(WolfSteakMeat)" -> FieldShovel/LargeTent.
		ItemBase nearestExact = null;
		float nearestExactDist = radius + 1;
		ItemBase nearestSub = null;
		float nearestSubDist = radius + 1;

		foreach (Object obj : objects)
		{
			ItemBase item = ItemBase.Cast(obj);
			if (!item || item.GetHierarchyParent())
				continue;
			// Beansprucht ein anderer Bot dieses Item schon, nicht hinlaufen
			// (verhindert das Tanzen mehrerer Bots um dieselbe Beute)
			if (IsClaimedByOther(item))
				continue;

			float dist = vector.Distance(m_Npc.GetPosition(), item.GetPosition());
			string t = item.GetType();
			if (classnameFilter == "")
			{
				// Kein Filter: naechstes beliebiges Item (nur bewusstes pickup())
				if (dist < nearestSubDist)
				{
					nearestSubDist = dist;
					nearestSub = item;
				}
				continue;
			}
			if (t == classnameFilter)
			{
				if (dist < nearestExactDist)
				{
					nearestExactDist = dist;
					nearestExact = item;
				}
			}
			else if (t.IndexOf(classnameFilter) > -1)
			{
				if (dist < nearestSubDist)
				{
					nearestSubDist = dist;
					nearestSub = item;
				}
			}
		}

		if (nearestExact)
			return nearestExact;
		return nearestSub;
	}

	// Raubtier-Heuristik (Classname, case-sensitiv wie die Loot-Filter): nur
	// Wolf/Baer zaehlen als Gegner, passive Tiere (Kuh, Reh, Ziege) NICHT. Live-
	// Wolf = Animal_CanisLupus_*, Baer = Animal_UrsusArctos bzw. modded Bear_*.
	private bool IsPredator(string classname)
	{
		return classname.IndexOf("Bear") > -1 || classname.IndexOf("Wolf") > -1 || classname.IndexOf("CanisLupus") > -1 || classname.IndexOf("Ursus") > -1;
	}

	// Naechstes feindliches Ziel: Infizierte ODER lebende Raubtiere (Wolf/Baer).
	// Im Battle-Royale ZUSAETZLICH rivalisierende Agenten und der menschliche
	// Spieler (Free-for-all) - damit das explizite engage des Gehirns nicht ins
	// Leere laeuft, waehrend das eAI-Auto-Gefecht ohnehin auf Sicht feuert.
	// Passive Tiere bleiben aussen vor, Spieler/Agenten nur im BR.
	private EntityAI FindNearestHostile(float radius)
	{
		array<Object> objects = new array<Object>();
		array<CargoBase> cargos = new array<CargoBase>();
		GetGame().GetObjectsAtPosition3D(m_Npc.GetPosition(), radius, objects, cargos);

		EntityAI nearest = null;
		float nearestDist = radius + 1;

		foreach (Object obj : objects)
		{
			EntityAI cand = null;
			DayZInfected infected = DayZInfected.Cast(obj);
			if (infected && infected.IsAlive())
			{
				cand = infected;
			}
			else
			{
				AnimalBase animal = AnimalBase.Cast(obj);
				if (animal && animal.IsAlive() && IsPredator(animal.GetType()))
					cand = animal;
			}

			if (!cand)
				continue;

			float dist = vector.Distance(m_Npc.GetPosition(), cand.GetPosition());
			if (dist < nearestDist)
			{
				nearestDist = dist;
				nearest = cand;
			}
		}

		// Battle-Royale: rivalisierende Agenten + menschlicher Spieler als Ziel.
		// Spieler/AI tauchen NICHT zuverlaessig in GetObjectsAtPosition3D auf,
		// daher ueber die Spielerliste suchen (so macht es auch die Engine).
		if (IsuAgentRegistry.IsBrMode(m_Npc))
		{
			array<Man> players = new array<Man>();
			GetGame().GetPlayers(players);
			foreach (Man man : players)
			{
				PlayerBase pb = PlayerBase.Cast(man);
				if (!pb || !pb.IsAlive() || pb == m_Npc)
					continue;
				eAIBase otherAI = eAIBase.Cast(man);
				bool isHuman = (otherAI == null && pb.GetIdentity() != null);
				bool isRivalAgent = (otherAI != null && IsuAgentRegistry.IsAgent(otherAI));
				if (!isHuman && !isRivalAgent)
					continue;
				float pdist = vector.Distance(m_Npc.GetPosition(), pb.GetPosition());
				if (pdist <= radius && pdist < nearestDist)
				{
					nearestDist = pdist;
					nearest = pb;
				}
			}
		}

		return nearest;
	}

	// Wie NpcReady, aber zusaetzlich: nicht im Fahrzeug (fuer Bewegungsbefehle)
	private bool NpcReadyOnFoot()
	{
		if (!NpcReady())
			return false;

		if (m_Npc.IsInTransport())
		{
			m_CmdStatus = "failed";
			m_CmdDetail = "du sitzt in einem Fahrzeug - erst vehicle_exit";
			return false;
		}

		return true;
	}

	private bool NpcReady()
	{
		if (!m_Npc)
		{
			m_CmdStatus = "failed";
			m_CmdDetail = "kein npc gespawnt";
			return false;
		}

		if (!m_Npc.IsAlive())
		{
			m_CmdStatus = "failed";
			m_CmdDetail = "npc ist tot";
			return false;
		}

		return true;
	}

	private vector ResolvePos(IsuCommand cmd)
	{
		float y = cmd.y;
		if (y <= 0)
			y = GetGame().SurfaceY(cmd.x, cmd.z);
		return Vector(cmd.x, y, cmd.z);
	}

	private float Dist2D(vector a, vector b)
	{
		a[1] = 0;
		b[1] = 0;
		return vector.Distance(a, b);
	}

	private void LogError(string msg)
	{
		Print("[IsuSurvivor] FEHLER: " + msg);
		m_Errors.Insert(msg);
		while (m_Errors.Count() > 10)
			m_Errors.RemoveOrdered(0);
	}
}

// HINWEIS: Der Server-RPC-Empfang fuer den Spieler-Direktbefehl liegt jetzt in
// 4_World (IsuArenaControl, modded PlayerBase OnRPC -> RelayNpcCommand schreibt
// npc_command.txt). Hier liest IsuBridge.TickNpcCommand die Datei und ruft
// OnPlayerNpcCommand. Ein modded PlayerBase in 5_Mission warf zuvor
// "Unknown type 'PlayerBase'" - darum der Umweg ueber 4_World wie beim
// Arena-Befehl.
