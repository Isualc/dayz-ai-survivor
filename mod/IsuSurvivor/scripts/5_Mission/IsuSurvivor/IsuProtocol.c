// IsuSurvivor — JSON-Protokollklassen fuer die File-Bridge.
// Serialisiert/deserialisiert mit dem Vanilla-JsonFileLoader.
// Schema-Doku: docs/protocol.md im Projekt-Repo.

class IsuNpcState
{
	bool spawned;
	bool alive;
	string classname;
	float pos_x;
	float pos_y;
	float pos_z;
	float heading;
	float health;          // GlobalHealth/Health, 0..100
	float blood;           // GlobalHealth/Blood, 0..5000
	float water;           // PlayerStat Water, ca. 0..5000
	float energy;          // PlayerStat Energy, ca. 0..20000
	float stomach_volume;  // Mageninhalt gesamt (PlayerStomach.GetStomachVolume)
	float heat_comfort;    // Waerme -1..+1 (unter ca. -0.5 friert er ernsthaft)
	string in_hands;       // Classname des Items in der Hand, leer = nichts
	bool fighting;         // eAI-Kampf-FSM aktiv
	string name;           // Anzeigename des Survivors (Chat-Absender)
	bool following;        // folgt gerade einem Spieler (Gruppenbeitritt)
	bool unconscious;      // bewusstlos (Schock) - liegt und kann nicht handeln
	bool in_vehicle;       // sitzt in einem Fahrzeug (Bewegungsbefehle gesperrt)
}

class IsuItemInfo
{
	string classname;
	string kind;       // "food" | "drink" | "firearm" | "ammo" | "clothing" | "other"
	float quantity;
	float health;
	bool in_hands;
	string parent;     // Classname des Tragers, wenn das Item an etwas steckt
	                   // (z.B. Magazin IN der Waffe) - sonst leer
}

class IsuCommandStatus
{
	string id;
	string action;
	string status;          // "idle" | "running" | "done" | "failed"
	string detail;          // Fehlergrund bei "failed"
	float dist_to_target;   // nur bei move_to sinnvoll
}

class IsuEntityInfo
{
	string kind;        // player | ai | infected | corpse | animal | item |
	                    // vehicle | water | fire | fire_burning
	string classname;
	string name;        // Spielername bei kind=="player", sonst leer
	float x;
	float y;
	float z;
	float distance;
	int cargo;          // Anzahl Items IM Objekt (Rucksack, Kleidung, Leiche...)
	string item_kind;   // nur bei kind=="item": firearm | ammo | attachment |
	                    // clothing | food | other - hilft beim Priorisieren
	string near;        // bei kind=="item": Name eines KI-Survivors <2 m -
	                    // wahrscheinlich dessen frische Ablage, nicht freies Loot
}

class IsuChatMsg
{
	int id;             // monoton steigend, fuer Dedup im Daemon
	int channel;
	string sender;
	string text;
	float uptime;       // Serverlaufzeit in Sekunden beim Empfang
}

class IsuState
{
	int seq;                // monoton steigend pro Tick
	float uptime;           // Serverlaufzeit in Sekunden
	string bridge_version;
	ref IsuNpcState npc;
	ref IsuCommandStatus command;
	ref array<ref IsuItemInfo> inventory;
	ref array<ref IsuEntityInfo> nearby;
	ref array<ref IsuChatMsg> chat;       // Ringpuffer der letzten Nachrichten
	ref array<string> errors;             // Bridge-Fehlermeldungen (Ringpuffer)

	void IsuState()
	{
		npc = new IsuNpcState();
		command = new IsuCommandStatus();
		inventory = new array<ref IsuItemInfo>();
		nearby = new array<ref IsuEntityInfo>();
		chat = new array<ref IsuChatMsg>();
		errors = new array<string>();
	}
}

class IsuCommand
{
	string id;          // eindeutig, vom Daemon vergeben
	string action;      // ping | spawn | move_to | stop | despawn | pickup | eat |
	                    // drink | equip_best | engage | flee | adopt_nearest |
	                    // teleport_player | spawn_item | spawn_infected |
	                    // say | follow | unfollow
	float x;
	float y;            // y <= 0 bedeutet: SurfaceY automatisch ermitteln
	float z;
	string loadout;     // nur fuer "spawn", leer = HumanLoadout.json
	string text;        // generischer Parameter: Classname-Filter (pickup, spawn_item,
	                    // spawn_infected), Spielername (teleport_player)
	string faction;     // nur fuer "spawn": civilian (Default) | west | east |
	                    // insurgent | raiders - bestimmt Feind/Freund-Verhalten
	string br;          // nur fuer "spawn": "1" = Battle-Royale (Free-for-all,
	                    // jeder gegen jeden inkl. Spieler), sonst normal/coop.
	                    // String statt int: JsonFileLoader deserialisiert Strings
	                    // zuverlaessig (wie faction/text); int kam als 0 an.
}

class IsuCommandFile
{
	ref array<ref IsuCommand> commands;

	void IsuCommandFile()
	{
		commands = new array<ref IsuCommand>();
	}
}
