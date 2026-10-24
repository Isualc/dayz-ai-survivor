// IsuSurvivor — 4_World-Patches an der Expansion-AI.
//
// Hintergrund (Quellcode-Analyse 2026-06-10, FSM Master.xml + eaistate_dormant.c):
// Expansion-AI schickt AI in den Zustand "Dormant" (DisableSimulation), wenn kein
// Spieler in Sichtweite ist. Der Aufwach-Guard prueft GetWaypoints().Count() > 1 —
// ein einzelner neuer Wegpunkt (unser move_to) weckt die AI also NIE auf.
// Unser Agent-NPC muss aber ohne Spieler in der Naehe agieren koennen.
// Loesung: nur der registrierte Agent-NPC wird vom Dormant-Zustand ausgenommen.

class IsuAgentRegistry
{
	// Arena: mehrere Agenten gleichzeitig. eAIBase -> Anzeigename.
	static ref map<eAIBase, string> s_Npcs = new map<eAIBase, string>();

	// Fahrzeug-Regel: Ausstieg nur fuer explizit freigegebene Agenten
	static ref set<eAIBase> s_VehicleExitAllowed = new set<eAIBase>();

	// Battle-Royale: PER AGENT (kein globaler Schalter -> keine Cross-
	// Contamination, falls je BR- und Coop-Agenten gleichzeitig laufen wuerden).
	// Ist ein Agent hier eingetragen, behandelt er jeden anderen Agenten UND den
	// menschlichen Spieler als Feind (Free-for-all). Gesetzt bei jedem Spawn (cmd.br).
	static ref set<eAIBase> s_BrAgents = new set<eAIBase>();

	// Nahkampf-Lock (engage mode="melee"): solange ein Agent hier steht und
	// eine Nahkampfwaffe haelt, darf Expansions TakeItemToHands-Zustand nicht
	// auf die Schusswaffe zurueckwechseln (siehe modded eAIState_TakeItemToHands
	// unten). Die Bridge setzt/loest den Lock (CmdEngage/UpdateEngage/Dispatch).
	static ref set<eAIBase> s_MeleeLock = new set<eAIBase>();

	// Schusszaehler pro Koerper seit Spawn (modded Weapon_Base.EEFired unten)
	// plus Typ der zuletzt abgefeuerten Waffe - State npc.shots /
	// npc.last_shot_weapon. Expansion kaempft selbststaendig (follow, regroup,
	// loot), ohne Zaehler erfuhr das Gehirn davon nichts.
	static ref map<eAIBase, int> s_Shots = new map<eAIBase, int>();
	static ref map<eAIBase, string> s_ShotWeapon = new map<eAIBase, string>();

	static bool IsAgent(eAIBase ai)
	{
		return ai != null && s_Npcs.Contains(ai);
	}

	static string AgentName(eAIBase ai)
	{
		string name;
		if (ai && s_Npcs.Find(ai, name))
			return name;
		return "";
	}

	// Lebenden Agenten ueber seinen Anzeigenamen finden (Item-Uebergabe)
	static eAIBase FindByName(string name)
	{
		foreach (eAIBase ai, string n : s_Npcs)
		{
			if (n == name && ai && ai.IsAlive())
				return ai;
		}
		return null;
	}

	static void Register(eAIBase ai, string name)
	{
		if (!ai)
			return;

		// Alte Eintraege desselben Agenten-Namens entfernen (toter Koerper
		// nach Respawn) - sonst klebt der Name an der Leiche: Nametags
		// zeigen auf den falschen Koerper und der Marker-Tick loescht mit
		// der Namens-UID den Marker des lebenden Nachfolgers.
		array<eAIBase> stale = new array<eAIBase>();
		foreach (eAIBase other, string otherName : s_Npcs)
		{
			if (otherName == name && other != ai)
				stale.Insert(other);
		}
		foreach (eAIBase s : stale)
		{
			s_Npcs.Remove(s);
			ForgetBody(s);
		}

		s_Npcs.Set(ai, name);
	}

	static void Unregister(eAIBase ai)
	{
		if (!ai)
			return;
		s_Npcs.Remove(ai);
		ForgetBody(ai);
	}

	// Neben-Sets/-Maps eines Koerpers aufraeumen (Despawn, Tod, Respawn-
	// Nachfolger). s_Npcs selbst pflegen die Aufrufer.
	static void ForgetBody(eAIBase ai)
	{
		if (!ai)
			return;
		int idx = s_VehicleExitAllowed.Find(ai);
		if (idx > -1)
			s_VehicleExitAllowed.Remove(idx);
		int bidx = s_BrAgents.Find(ai);
		if (bidx > -1)
			s_BrAgents.Remove(bidx);
		int midx = s_MeleeLock.Find(ai);
		if (midx > -1)
			s_MeleeLock.Remove(midx);
		if (s_Shots.Contains(ai))
			s_Shots.Remove(ai);
		if (s_ShotWeapon.Contains(ai))
			s_ShotWeapon.Remove(ai);
	}

	static bool HasMeleeLock(eAIBase ai)
	{
		return ai != null && s_MeleeLock.Find(ai) > -1;
	}

	static void SetMeleeLock(eAIBase ai, bool on)
	{
		if (!ai)
			return;
		int idx = s_MeleeLock.Find(ai);
		if (on && idx == -1)
			s_MeleeLock.Insert(ai);
		else if (!on && idx > -1)
			s_MeleeLock.Remove(idx);
	}

	static void CountShot(eAIBase ai, string weaponType)
	{
		if (!ai)
			return;
		int n = 0;
		s_Shots.Find(ai, n);
		s_Shots.Set(ai, n + 1);
		s_ShotWeapon.Set(ai, weaponType);
	}

	static int ShotCount(eAIBase ai)
	{
		int n = 0;
		if (ai)
			s_Shots.Find(ai, n);
		return n;
	}

	static string LastShotWeapon(eAIBase ai)
	{
		string w = "";
		if (ai)
			s_ShotWeapon.Find(ai, w);
		return w;
	}

	static bool MayExitVehicle(eAIBase ai)
	{
		return s_VehicleExitAllowed.Find(ai) > -1;
	}

	static void SetVehicleExit(eAIBase ai, bool allowed)
	{
		int idx = s_VehicleExitAllowed.Find(ai);
		if (allowed && idx == -1)
			s_VehicleExitAllowed.Insert(ai);
		else if (!allowed && idx > -1)
			s_VehicleExitAllowed.Remove(idx);
	}

	static bool IsBrMode(eAIBase ai)
	{
		return ai != null && s_BrAgents.Find(ai) > -1;
	}

	static void SetBrMode(eAIBase ai, bool on)
	{
		if (!ai)
			return;
		int idx = s_BrAgents.Find(ai);
		if (on && idx == -1)
			s_BrAgents.Insert(ai);
		else if (!on && idx > -1)
			s_BrAgents.Remove(idx);
	}
}

modded class eAIState_Dormant
{
	override int Guard()
	{
		// Agent-NPC darf nie einschlafen: Guard-FAIL blockiert den Eintritt in
		// Dormant und beendet einen bereits aktiven Dormant-Zustand.
		if (IsuAgentRegistry.IsAgent(eAIBase.Cast(unit)))
			return eAITransition.FAIL;

		return super.Guard();
	}
}

// FRIENDLY-FIRE-SCHUTZ: Unsere zivilen Begleiter-NPCs greifen einen
// MENSCHLICHEN Spieler NIE an - egal in welcher Gruppe sie gerade stecken.
//
// Hintergrund (Quellcode-Analyse Expansion 2026-06-13): Sobald ein
// rekrutierter NPC aus der Spielergruppe in eine eigene Gruppe wechselt
// (EnsureOwnGroup bei halt/comehere/move), faellt der eAI-Retaliation-Schutz
// "otherGroup == aggressorGroup" (eAITargetInformation.c:425) weg. Ein
// einziger (auch versehentlicher) Treffer des Spielers macht den NPC dann
// ueber EEHitBy -> AddFriendlyAI(threat=1.0) feindlich; "civilian"
// (IsFriendly->true) hilft NICHT, weil der Retaliation-/Threat-Pfad die
// Fraktion ueberspringt. Auch ein aus frueheren Sessions gecachter Threat
// (PlayerIsEnemy gibt "return targeted" zurueck) laesst den NPC ballern.
//
// PlayerIsEnemy ist DIE Stelle, an der die Threat-Berechnung (CalculateThreat,
// eAIPlayerTargetInformation.c:123/182-191) ueber die Out-Parameter friendly
// und targeted entscheidet, ob der NPC in den Kampfzustand geht. Setzen wir
// fuer einen zivilen Agenten gegenueber einem echten Menschen friendly=true /
// targeted=false und Rueckgabe false, geht er nie auf den Spieler. Arena-
// Hostilitaets-Agenten (Fraktion != civilian) bleiben unberuehrt.
modded class eAIBase
{
	override bool PlayerIsEnemy(EntityAI other, bool track = false, out bool isPlayerMoving = false, out bool friendly = false, out bool targeted = false)
	{
		if (IsuAgentRegistry.IsAgent(this))
		{
			// Battle-Royale (Free-for-all): VOR dem Zivilisten-Gate, also
			// fraktions-unabhaengig und deterministisch. Jeder andere registrierte
			// Agent UND jeder menschliche Spieler ist Feind. PlayerIsEnemy ist die
			// Stelle, die ueber den Kampfzustand entscheidet -> das eAI-Auto-Gefecht
			// greift das Ziel auf Sicht an (deckt Aggro UND Engage-on-Contact ab).
			if (IsuAgentRegistry.IsBrMode(this))
			{
				eAIBase brOther = eAIBase.Cast(other);
				bool brAgent = (brOther != null && brOther != this && IsuAgentRegistry.IsAgent(brOther));
				bool brHuman = (PlayerBase.Cast(other) != null && brOther == null);
				if (brAgent || brHuman)
				{
					isPlayerMoving = false;
					friendly = false;
					targeted = true;
					return true;
				}
			}

			eAIGroup g = GetGroup();
			if (g && eAIFactionCivilian.Cast(g.GetFaction()))
			{
				eAIBase otherAI = eAIBase.Cast(other);

				// Echter Mensch = ein PlayerBase, der KEIN eAIBase ist ->
				// immer Freund (Friendly-Fire-Schutz).
				if (PlayerBase.Cast(other) && !otherAI)
				{
					isPlayerMoving = false;
					friendly = true;
					targeted = false;
					return false;
				}

				if (otherAI)
				{
					// Anderer Begleiter-Agent -> Freund, nie untereinander schiessen.
					if (IsuAgentRegistry.IsAgent(otherAI))
					{
						isPlayerMoving = false;
						friendly = true;
						targeted = false;
						return false;
					}
					// Fremde NPCs beurteilt Expansion unten anhand ihrer Fraktion
					// und der tatsaechlichen Bedrohung. Insbesondere sind Guards
					// nicht schon wegen ihres NPC-Typs feindlich: sie muessen die
					// Waffe heben/kaempfen und uns als Bedrohung betrachten.
					// Kein kuenstliches targeted=true: das provozierte neutrale
					// Wachen auch ohne Angriffsbefehl. Banditen und echte
					// Gegenwehr bleiben durch super.PlayerIsEnemy erhalten.
				}
			}
		}

		return super.PlayerIsEnemy(other, track, isPlayerMoving, friendly, targeted);
	}
}

modded class eAIState_GetOutVehicle
{
	override void OnEntry(string Event, ExpansionState From)
	{
		// Fahrzeug-Regel: der Agent steigt NICHT eigenmaechtig aus (z.B. um zu
		// kaempfen). Erlaubt ist der Ausstieg nur, wenn die Bridge ihn explizit
		// freigegeben hat (vehicle_exit) oder der Formations-Leader bereits
		// draussen ist (normales Mitfahrer-Verhalten).
		eAIBase agentUnit = eAIBase.Cast(unit);
		if (IsuAgentRegistry.IsAgent(agentUnit) && !IsuAgentRegistry.MayExitVehicle(agentUnit))
		{
			DayZPlayerImplement leader = null;
			eAIGroup group = unit.GetGroup();
			if (group)
				leader = group.GetFormationLeader();

			bool leaderOutside = leader && leader != unit && !leader.IsInTransport();
			if (!leaderOutside)
			{
				Print("[IsuSurvivor] Fahrzeug-Ausstieg unterdrueckt (Fahrzeug-Regel)");
				m_Time = 0;
				return; // super NICHT rufen -> kein GetOutVehicle()
			}
		}

		super.OnEntry(Event, From);
	}
}

// NAHKAMPF-LOCK (engage mode="melee"). Hintergrund (Quellcode-Analyse
// 24.10.2026, eaistate_takeitemtohands.c:45-232 + Master.xml:268-277):
// Expansion prueft im Zustand TakeItemToHands (Transition aus Idle, Flank,
// Fighting, TraversingWaypoints, FollowFormation, TakeItemToInventory) staendig
// neu und nimmt jede geladene Feuerwaffe mit mehr DPS, sobald "preferMelee"
// nicht gilt (kein Ziel, Viability < 2 s, keine Sichtlinie, HP <= 80 % oder
// mehr als ein akuter Gegner, eAIBase.c:6990-6993). equip_melee hielt das
// Messer deshalb oft nur Sekunden - der NPC schoss und funkte "leise".
// Guard() ist die einzige Stelle, an der Expansion die Hand-Waffe tauscht
// (einziger eAI_TakeItemToHands-Aufruf: eaistate_takeitemtohands.c:35).
// Signatur "int Guard()" laut eaistate_takeitemtohands.c:45, Rueckgabe
// eAITransition.FAIL (= ExpansionTransition.FAIL = 0, ExpansionTransition.c:3)
// blockiert den Eintritt. Ruinierte Waffe und Verband laufen weiter ueber super
// (Abwurf bzw. Bandage-Wechsel); geloest wird der Lock von der Bridge
// (Ende, Tod, Timeout, neuer Befehl, Eskalation bei HP < 50 % / 3+ Gegnern).
modded class eAIState_TakeItemToHands
{
	override int Guard()
	{
		// unit ist laut eAIState.c:3 bereits ein eAIBase
		if (IsuAgentRegistry.HasMeleeLock(unit))
		{
			ItemBase lockHands = unit.GetItemInHands();
			bool keepMelee = false;
			if (lockHands && lockHands.Expansion_IsMeleeWeapon())
				keepMelee = true;
			if (keepMelee && lockHands.IsDamageDestroyed())
				keepMelee = false;
			if (keepMelee && unit.eAI_ShouldBandage())
				keepMelee = false;
			if (keepMelee)
				return eAITransition.FAIL;
		}

		return super.Guard();
	}
}

// SCHUSSZAEHLER: jeder Schuss eines registrierten Agenten zaehlt (State
// npc.shots / npc.last_shot_weapon). Vanilla-Signatur EEFired(int muzzleType,
// int mode, string ammoType) laut weapon_base.c:341; Expansion ueberschreibt
// sie selbst mit einem g_Game.IsServer()-Zweig (Expansion Weapon_Base.c:329-341),
// der Hook feuert also auf dem Dedicated Server. super zuerst, damit Effekte
// und Expansions Laerm-System unveraendert bleiben.
modded class Weapon_Base
{
	override void EEFired(int muzzleType, int mode, string ammoType)
	{
		super.EEFired(muzzleType, mode, ammoType);

		if (!GetGame().IsServer())
			return;
		eAIBase isuShooter = eAIBase.Cast(GetHierarchyRootPlayer());
		if (!isuShooter)
			return;
		if (!IsuAgentRegistry.IsAgent(isuShooter))
			return;
		IsuAgentRegistry.CountShot(isuShooter, GetType());
	}
}
