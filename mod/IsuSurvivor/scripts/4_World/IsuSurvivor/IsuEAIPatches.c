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
			s_Npcs.Remove(s);

		s_Npcs.Set(ai, name);
	}

	static void Unregister(eAIBase ai)
	{
		if (!ai)
			return;
		s_Npcs.Remove(ai);
		int idx = s_VehicleExitAllowed.Find(ai);
		if (idx > -1)
			s_VehicleExitAllowed.Remove(idx);
		int bidx = s_BrAgents.Find(ai);
		if (bidx > -1)
			s_BrAgents.Remove(bidx);
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
					// Fremder NPC: ist seine Fraktion UNS (civilian) gegenueber
					// FEINDLICH, behandeln wir ihn als Feind. Sonst wuerde ein
					// ziviler Begleiter (civilian.IsFriendly->true zu allen) auch
					// Banditen/feindliche Patrouillen ignorieren. Friedliche
					// Survivor wurden zu spaet als Bedrohung erkannt. Jetzt: alle ausser Trader.
					// Kein blindes Vertrauen: jeder fremde NPC ist potenziell feindlich.
					if (!otherAI.GetType().Contains("Trader"))   // Fremder (ausser Trader) = Feind
					{
						friendly = false;
						targeted = true;
						return true;
					}
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
