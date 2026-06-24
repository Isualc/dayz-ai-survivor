class CfgPatches
{
	class IsuSurvivor
	{
		units[] = {};
		weapons[] = {};
		requiredVersion = 0.1;
		// Bewusst nur Vanilla-Abhaengigkeiten: die eAI-Klassen werden erst zur
		// Script-Compile-Zeit gebraucht (Module kompilieren in der Reihenfolge
		// 1_Core -> ... -> 4_World -> 5_Mission ueber ALLE geladenen Mods, d.h.
		// Expansion-4_World ist fertig, bevor unser 5_Mission-Modul kompiliert).
		// Voraussetzung: Server laeuft mit -mod=@CF;@DayZ-Expansion-Core;@DayZ-Expansion-AI
		requiredAddons[] =
		{
			"DZ_Data",
			"DZ_Scripts"
		};
	};
};

class CfgMods
{
	class IsuSurvivor
	{
		dir = "IsuSurvivor";
		name = "ISU Survivor Agent Bridge";
		credits = "isualc AI";
		author = "isualc AI";
		version = "0.1.0";
		extra = 0;
		type = "mod";

		dependencies[] = {"World", "Mission"};

		class defs
		{
			class worldScriptModule
			{
				value = "";
				files[] = {"IsuSurvivor/scripts/4_World"};
			};
			class missionScriptModule
			{
				value = "";
				files[] = {"IsuSurvivor/scripts/5_Mission"};
			};
		};
	};
};
