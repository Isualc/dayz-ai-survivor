// IsuSurvivor — In-Game-Arena-Steuerung (Server-Seite).
//
// Das IsuVoice-Menue (Client, Taste Pos1/Home) schickt Befehle als RPC an
// den Server. Hier werden sie in arena_request.txt geschrieben; der
// arena_supervisor.py auf dem Host pollt die Datei und verwaltet die
// run_agent-Prozesse. Der Lagerpunkt (Zelt + Feuerstelle) wird direkt von
// der Mod umgesetzt (IsuBaseCamp).

// Muss zu den Konstanten in IsuVoice passen ("ISUA" / "ISUS" / "ISUC")
const int ISUSRV_RPC_ARENA_CMD = 0x49535541;
const int ISUSRV_RPC_ARENA_STATUS = 0x49535553;
const int ISUSRV_RPC_NPC_CMD = 0x49535543;   // Befehlsrad/Direkttasten -> Server

// Basislager der Agenten: Position kommt aus $profile:IsuSurvivor/camp.txt
// (zwei Zeilen: x, z), Default 4233.7/8512.2. Idempotentes Spawnen,
// Umzug per Menue ("Lager = meine Position").
class IsuBaseCamp
{
	static float s_X = 4233.7;
	static float s_Z = 8512.2;
	static bool s_Loaded = false;

	static void Load()
	{
		if (s_Loaded)
			return;
		s_Loaded = true;

		FileHandle fh = OpenFile("$profile:IsuSurvivor/camp.txt", FileMode.READ);
		if (fh == 0)
			return;
		string lineX;
		string lineZ;
		FGets(fh, lineX);
		FGets(fh, lineZ);
		CloseFile(fh);
		if (lineX != "" && lineZ != "")
		{
			s_X = lineX.ToFloat();
			s_Z = lineZ.ToFloat();
		}
	}

	static void Save()
	{
		FileHandle fh = OpenFile("$profile:IsuSurvivor/camp.txt", FileMode.WRITE);
		if (fh == 0)
			return;
		FPrintln(fh, s_X.ToString());
		FPrintln(fh, s_Z.ToString());
		CloseFile(fh);
	}

	// Zelt + Feuerstelle am aktuellen Punkt sicherstellen (spawnt nur Fehlendes)
	static void Ensure()
	{
		Load();

		vector tentPos = Vector(s_X, 0, s_Z);
		tentPos[1] = GetGame().SurfaceY(tentPos[0], tentPos[2]);
		vector firePos = Vector(s_X + 5.3, 0, s_Z - 4.2);
		firePos[1] = GetGame().SurfaceY(firePos[0], firePos[2]);

		bool hasTent = false;
		bool hasFire = false;

		array<Object> objects = new array<Object>();
		array<CargoBase> cargos = new array<CargoBase>();
		GetGame().GetObjectsAtPosition3D(tentPos, 30.0, objects, cargos);

		foreach (Object obj : objects)
		{
			if (obj.IsKindOf("LargeTent"))
				hasTent = true;
			if (obj.IsKindOf("Fireplace"))
				hasFire = true;
		}

		// PERSISTENZ: ECE_SETUP laesst die Central Economy das Objekt
		// registrieren und in storage_1 speichern - es ueberlebt damit den
		// Server-Neustart MIT Inhalt (wie ein vom Spieler aufgestelltes Zelt).
		// Nur ECE_PLACE_ON_SURFACE reichte NICHT (kein Persistenz-Bit -> Zelt
		// bekam die 45-s-Default-Lifetime und despawnte). ECE_NOLIFETIME und
		// ECE_DYNAMIC_PERSISTENCY duerfen NICHT gesetzt sein - die schalten die
		// Persistenz gerade aus. SetLifetime garantiert die volle Lebensdauer.
		int persistFlags = ECE_SETUP | ECE_UPDATEPATHGRAPH | ECE_CREATEPHYSICS | ECE_PLACE_ON_SURFACE;

		if (!hasTent)
		{
			Object tent = GetGame().CreateObjectEx("LargeTent", tentPos, persistFlags);
			ItemBase tentItem = ItemBase.Cast(tent);
			if (tentItem)
				tentItem.SetLifetime(3888000);   // 45 Tage statt 45 Sekunden
			if (tent)
				Print("[IsuSurvivor] Basislager: Militaerzelt PERSISTENT gespawnt bei " + tentPos.ToString());
		}
		if (!hasFire)
		{
			Object fire = GetGame().CreateObjectEx("Fireplace", firePos, persistFlags);
			ItemBase fireItem = ItemBase.Cast(fire);
			if (fireItem)
				fireItem.SetLifetime(3888000);
			if (fire)
				Print("[IsuSurvivor] Basislager: Feuerstelle PERSISTENT gespawnt bei " + firePos.ToString());
		}
	}

	// Lager umziehen: alte Camp-Objekte am bisherigen Punkt entfernen,
	// neuen Punkt speichern, neu aufbauen.
	static void Relocate(float x, float z)
	{
		Load();

		// Punkt UNVERAENDERT? Dann NICHT loeschen/neu bauen. Der normale Arena-
		// Start traegt immer "camp:DEFAULT" mit, also lief Relocate bei JEDEM
		// Start - und loeschte dabei das aus storage geladene, PERSISTIERTE Zelt
		// MIT NPC-Inhalt, nur um ein frisches leeres zu spawnen. Genau das war
		// "Zelt verschwindet trotz graceful shutdown". Bei gleichem Punkt nur das
		// Fehlende sicherstellen (Ensure ist idempotent), nichts loeschen.
		if (vector.Distance(Vector(s_X, 0, s_Z), Vector(x, 0, z)) < 1.0)
		{
			Ensure();
			return;
		}

		vector oldPos = Vector(s_X, 0, s_Z);
		oldPos[1] = GetGame().SurfaceY(oldPos[0], oldPos[2]);

		array<Object> objects = new array<Object>();
		array<CargoBase> cargos = new array<CargoBase>();
		GetGame().GetObjectsAtPosition3D(oldPos, 35.0, objects, cargos);

		foreach (Object obj : objects)
		{
			if (obj.IsKindOf("LargeTent") || obj.IsKindOf("Fireplace"))
				GetGame().ObjectDelete(obj);
		}

		s_X = x;
		s_Z = z;
		Save();
		Ensure();
		Print("[IsuSurvivor] Basislager umgezogen nach " + x.ToString() + "/" + z.ToString());
	}
}

// Befehls-Annahme vom Client-Menue
class IsuArenaControl
{
	static int s_Seq = 0;
	static int s_NpcSeq = 0;

	// Befehlsrad/Direkttasten (Spieler -> NPC). In 4_World angenommen (hier
	// funktioniert modded PlayerBase), an die Bridge in 5_Mission ueber eine
	// Datei weitergereicht (IsuBridge.TickNpcCommand liest sie) - denn nur
	// 5_Mission erreicht IsuBridge.OnPlayerNpcCommand.
	static void RelayNpcCommand(string line)
	{
		s_NpcSeq++;
		FileHandle fh = OpenFile("$profile:IsuSurvivor/npc_command.txt", FileMode.WRITE);
		if (fh != 0)
		{
			FPrintln(fh, s_NpcSeq.ToString());
			FPrintln(fh, line);
			CloseFile(fh);
		}
		Print("[IsuSurvivor] NPC-Befehl #" + s_NpcSeq.ToString() + ": " + line);
	}

	static void HandleCommand(string cmd)
	{
		s_Seq++;

		FileHandle fh = OpenFile("$profile:IsuSurvivor/arena_request.txt", FileMode.WRITE);
		if (fh != 0)
		{
			FPrintln(fh, s_Seq.ToString());
			FPrintln(fh, cmd);
			CloseFile(fh);
		}
		Print("[IsuSurvivor] Arena-Befehl #" + s_Seq.ToString() + ": " + cmd);

		// Lagerpunkt wendet die Mod selbst an (camp:x,z im Befehl)
		array<string> parts = new array<string>();
		cmd.Split("|", parts);
		foreach (string part : parts)
		{
			if (part.IndexOf("camp:") != 0)
				continue;
			string coords = part.Substring(5, part.Length() - 5);
			array<string> xz = new array<string>();
			coords.Split(",", xz);
			if (xz.Count() == 2)
				IsuBaseCamp.Relocate(xz[0].ToFloat(), xz[1].ToFloat());
		}
	}
}

modded class PlayerBase
{
	override void OnRPC(PlayerIdentity sender, int rpc_type, ParamsReadContext ctx)
	{
		super.OnRPC(sender, rpc_type, ctx);

		// Nur auf dem Server verarbeiten
		if (!GetGame().IsDedicatedServer())
			return;

		if (rpc_type == ISUSRV_RPC_ARENA_CMD)
		{
			Param1<string> data = new Param1<string>("");
			if (ctx.Read(data))
				IsuArenaControl.HandleCommand(data.param1);
		}
		else if (rpc_type == ISUSRV_RPC_NPC_CMD)
		{
			Param1<string> npc = new Param1<string>("");
			if (ctx.Read(npc))
				IsuArenaControl.RelayNpcCommand(npc.param1);
		}
	}
}
