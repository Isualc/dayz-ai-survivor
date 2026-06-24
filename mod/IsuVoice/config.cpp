class CfgPatches
{
	class IsuVoice
	{
		units[] = {};
		weapons[] = {};
		requiredVersion = 0.1;
		requiredAddons[] = { "DZ_Data", "DZ_Sounds_Effects" };
	};
};

class CfgMods
{
	class IsuVoice
	{
		dir = "IsuVoice";
		name = "ISU Survivor Voice";
		credits = "isualc AI";
		author = "isualc AI";
		version = "0.1.0";
		extra = 0;
		type = "mod";

		dependencies[] = {"World"};

		class defs
		{
			class worldScriptModule
			{
				value = "";
				files[] = {"IsuVoice/scripts/4_World"};
			};
			class missionScriptModule
			{
				value = "";
				files[] = {"IsuVoice/scripts/5_Mission"};
			};
		};
	};
};

class CfgSoundShaders
{
	class IsuVoice_viktor_greet_01_Shader
	{
		samples[] = {{"IsuVoice\sounds\viktor\greet_01.ogg", 1}};
		volume = 1.8;
		range = 80;
	};
	class IsuVoice_viktor_greet_02_Shader
	{
		samples[] = {{"IsuVoice\sounds\viktor\greet_02.ogg", 1}};
		volume = 1.8;
		range = 80;
	};
	class IsuVoice_viktor_greet_03_Shader
	{
		samples[] = {{"IsuVoice\sounds\viktor\greet_03.ogg", 1}};
		volume = 1.8;
		range = 80;
	};
	class IsuVoice_viktor_greet_04_Shader
	{
		samples[] = {{"IsuVoice\sounds\viktor\greet_04.ogg", 1}};
		volume = 1.8;
		range = 80;
	};
	class IsuVoice_viktor_warn_01_Shader
	{
		samples[] = {{"IsuVoice\sounds\viktor\warn_01.ogg", 1}};
		volume = 1.8;
		range = 80;
	};
	class IsuVoice_viktor_warn_02_Shader
	{
		samples[] = {{"IsuVoice\sounds\viktor\warn_02.ogg", 1}};
		volume = 1.8;
		range = 80;
	};
	class IsuVoice_viktor_warn_03_Shader
	{
		samples[] = {{"IsuVoice\sounds\viktor\warn_03.ogg", 1}};
		volume = 1.8;
		range = 80;
	};
	class IsuVoice_viktor_warn_04_Shader
	{
		samples[] = {{"IsuVoice\sounds\viktor\warn_04.ogg", 1}};
		volume = 1.8;
		range = 80;
	};
	class IsuVoice_viktor_warn_05_Shader
	{
		samples[] = {{"IsuVoice\sounds\viktor\warn_05.ogg", 1}};
		volume = 1.8;
		range = 80;
	};
	class IsuVoice_viktor_follow_01_Shader
	{
		samples[] = {{"IsuVoice\sounds\viktor\follow_01.ogg", 1}};
		volume = 1.8;
		range = 80;
	};
	class IsuVoice_viktor_follow_02_Shader
	{
		samples[] = {{"IsuVoice\sounds\viktor\follow_02.ogg", 1}};
		volume = 1.8;
		range = 80;
	};
	class IsuVoice_viktor_follow_03_Shader
	{
		samples[] = {{"IsuVoice\sounds\viktor\follow_03.ogg", 1}};
		volume = 1.8;
		range = 80;
	};
	class IsuVoice_viktor_follow_04_Shader
	{
		samples[] = {{"IsuVoice\sounds\viktor\follow_04.ogg", 1}};
		volume = 1.8;
		range = 80;
	};
	class IsuVoice_viktor_combat_01_Shader
	{
		samples[] = {{"IsuVoice\sounds\viktor\combat_01.ogg", 1}};
		volume = 1.8;
		range = 80;
	};
	class IsuVoice_viktor_combat_02_Shader
	{
		samples[] = {{"IsuVoice\sounds\viktor\combat_02.ogg", 1}};
		volume = 1.8;
		range = 80;
	};
	class IsuVoice_viktor_combat_03_Shader
	{
		samples[] = {{"IsuVoice\sounds\viktor\combat_03.ogg", 1}};
		volume = 1.8;
		range = 80;
	};
	class IsuVoice_viktor_combat_04_Shader
	{
		samples[] = {{"IsuVoice\sounds\viktor\combat_04.ogg", 1}};
		volume = 1.8;
		range = 80;
	};
	class IsuVoice_viktor_combat_05_Shader
	{
		samples[] = {{"IsuVoice\sounds\viktor\combat_05.ogg", 1}};
		volume = 1.8;
		range = 80;
	};
	class IsuVoice_viktor_help_01_Shader
	{
		samples[] = {{"IsuVoice\sounds\viktor\help_01.ogg", 1}};
		volume = 1.8;
		range = 80;
	};
	class IsuVoice_viktor_help_02_Shader
	{
		samples[] = {{"IsuVoice\sounds\viktor\help_02.ogg", 1}};
		volume = 1.8;
		range = 80;
	};
	class IsuVoice_viktor_help_03_Shader
	{
		samples[] = {{"IsuVoice\sounds\viktor\help_03.ogg", 1}};
		volume = 1.8;
		range = 80;
	};
	class IsuVoice_viktor_help_04_Shader
	{
		samples[] = {{"IsuVoice\sounds\viktor\help_04.ogg", 1}};
		volume = 1.8;
		range = 80;
	};
	class IsuVoice_viktor_talk_01_Shader
	{
		samples[] = {{"IsuVoice\sounds\viktor\talk_01.ogg", 1}};
		volume = 1.8;
		range = 80;
	};
	class IsuVoice_viktor_talk_02_Shader
	{
		samples[] = {{"IsuVoice\sounds\viktor\talk_02.ogg", 1}};
		volume = 1.8;
		range = 80;
	};
	class IsuVoice_viktor_talk_03_Shader
	{
		samples[] = {{"IsuVoice\sounds\viktor\talk_03.ogg", 1}};
		volume = 1.8;
		range = 80;
	};
	class IsuVoice_viktor_talk_04_Shader
	{
		samples[] = {{"IsuVoice\sounds\viktor\talk_04.ogg", 1}};
		volume = 1.8;
		range = 80;
	};
	class IsuVoice_viktor_talk_05_Shader
	{
		samples[] = {{"IsuVoice\sounds\viktor\talk_05.ogg", 1}};
		volume = 1.8;
		range = 80;
	};
	class IsuVoice_viktor_bye_01_Shader
	{
		samples[] = {{"IsuVoice\sounds\viktor\bye_01.ogg", 1}};
		volume = 1.8;
		range = 80;
	};
	class IsuVoice_viktor_bye_02_Shader
	{
		samples[] = {{"IsuVoice\sounds\viktor\bye_02.ogg", 1}};
		volume = 1.8;
		range = 80;
	};
	class IsuVoice_viktor_bye_03_Shader
	{
		samples[] = {{"IsuVoice\sounds\viktor\bye_03.ogg", 1}};
		volume = 1.8;
		range = 80;
	};
	class IsuVoice_viktor_yes_01_Shader
	{
		samples[] = {{"IsuVoice\sounds\viktor\yes_01.ogg", 1}};
		volume = 1.8;
		range = 80;
	};
	class IsuVoice_viktor_no_01_Shader
	{
		samples[] = {{"IsuVoice\sounds\viktor\no_01.ogg", 1}};
		volume = 1.8;
		range = 80;
	};
	class IsuVoice_viktor_maybe_01_Shader
	{
		samples[] = {{"IsuVoice\sounds\viktor\maybe_01.ogg", 1}};
		volume = 1.8;
		range = 80;
	};
};

class CfgSoundSets
{
	class IsuVoice_viktor_greet_01_SoundSet
	{
		soundShaders[] = {"IsuVoice_viktor_greet_01_Shader"};
		volumeFactor = 1.0;
		spatial = 1;
		doppler = 0;
		loop = 0;
	};
	class IsuVoice_viktor_greet_02_SoundSet
	{
		soundShaders[] = {"IsuVoice_viktor_greet_02_Shader"};
		volumeFactor = 1.0;
		spatial = 1;
		doppler = 0;
		loop = 0;
	};
	class IsuVoice_viktor_greet_03_SoundSet
	{
		soundShaders[] = {"IsuVoice_viktor_greet_03_Shader"};
		volumeFactor = 1.0;
		spatial = 1;
		doppler = 0;
		loop = 0;
	};
	class IsuVoice_viktor_greet_04_SoundSet
	{
		soundShaders[] = {"IsuVoice_viktor_greet_04_Shader"};
		volumeFactor = 1.0;
		spatial = 1;
		doppler = 0;
		loop = 0;
	};
	class IsuVoice_viktor_warn_01_SoundSet
	{
		soundShaders[] = {"IsuVoice_viktor_warn_01_Shader"};
		volumeFactor = 1.0;
		spatial = 1;
		doppler = 0;
		loop = 0;
	};
	class IsuVoice_viktor_warn_02_SoundSet
	{
		soundShaders[] = {"IsuVoice_viktor_warn_02_Shader"};
		volumeFactor = 1.0;
		spatial = 1;
		doppler = 0;
		loop = 0;
	};
	class IsuVoice_viktor_warn_03_SoundSet
	{
		soundShaders[] = {"IsuVoice_viktor_warn_03_Shader"};
		volumeFactor = 1.0;
		spatial = 1;
		doppler = 0;
		loop = 0;
	};
	class IsuVoice_viktor_warn_04_SoundSet
	{
		soundShaders[] = {"IsuVoice_viktor_warn_04_Shader"};
		volumeFactor = 1.0;
		spatial = 1;
		doppler = 0;
		loop = 0;
	};
	class IsuVoice_viktor_warn_05_SoundSet
	{
		soundShaders[] = {"IsuVoice_viktor_warn_05_Shader"};
		volumeFactor = 1.0;
		spatial = 1;
		doppler = 0;
		loop = 0;
	};
	class IsuVoice_viktor_follow_01_SoundSet
	{
		soundShaders[] = {"IsuVoice_viktor_follow_01_Shader"};
		volumeFactor = 1.0;
		spatial = 1;
		doppler = 0;
		loop = 0;
	};
	class IsuVoice_viktor_follow_02_SoundSet
	{
		soundShaders[] = {"IsuVoice_viktor_follow_02_Shader"};
		volumeFactor = 1.0;
		spatial = 1;
		doppler = 0;
		loop = 0;
	};
	class IsuVoice_viktor_follow_03_SoundSet
	{
		soundShaders[] = {"IsuVoice_viktor_follow_03_Shader"};
		volumeFactor = 1.0;
		spatial = 1;
		doppler = 0;
		loop = 0;
	};
	class IsuVoice_viktor_follow_04_SoundSet
	{
		soundShaders[] = {"IsuVoice_viktor_follow_04_Shader"};
		volumeFactor = 1.0;
		spatial = 1;
		doppler = 0;
		loop = 0;
	};
	class IsuVoice_viktor_combat_01_SoundSet
	{
		soundShaders[] = {"IsuVoice_viktor_combat_01_Shader"};
		volumeFactor = 1.0;
		spatial = 1;
		doppler = 0;
		loop = 0;
	};
	class IsuVoice_viktor_combat_02_SoundSet
	{
		soundShaders[] = {"IsuVoice_viktor_combat_02_Shader"};
		volumeFactor = 1.0;
		spatial = 1;
		doppler = 0;
		loop = 0;
	};
	class IsuVoice_viktor_combat_03_SoundSet
	{
		soundShaders[] = {"IsuVoice_viktor_combat_03_Shader"};
		volumeFactor = 1.0;
		spatial = 1;
		doppler = 0;
		loop = 0;
	};
	class IsuVoice_viktor_combat_04_SoundSet
	{
		soundShaders[] = {"IsuVoice_viktor_combat_04_Shader"};
		volumeFactor = 1.0;
		spatial = 1;
		doppler = 0;
		loop = 0;
	};
	class IsuVoice_viktor_combat_05_SoundSet
	{
		soundShaders[] = {"IsuVoice_viktor_combat_05_Shader"};
		volumeFactor = 1.0;
		spatial = 1;
		doppler = 0;
		loop = 0;
	};
	class IsuVoice_viktor_help_01_SoundSet
	{
		soundShaders[] = {"IsuVoice_viktor_help_01_Shader"};
		volumeFactor = 1.0;
		spatial = 1;
		doppler = 0;
		loop = 0;
	};
	class IsuVoice_viktor_help_02_SoundSet
	{
		soundShaders[] = {"IsuVoice_viktor_help_02_Shader"};
		volumeFactor = 1.0;
		spatial = 1;
		doppler = 0;
		loop = 0;
	};
	class IsuVoice_viktor_help_03_SoundSet
	{
		soundShaders[] = {"IsuVoice_viktor_help_03_Shader"};
		volumeFactor = 1.0;
		spatial = 1;
		doppler = 0;
		loop = 0;
	};
	class IsuVoice_viktor_help_04_SoundSet
	{
		soundShaders[] = {"IsuVoice_viktor_help_04_Shader"};
		volumeFactor = 1.0;
		spatial = 1;
		doppler = 0;
		loop = 0;
	};
	class IsuVoice_viktor_talk_01_SoundSet
	{
		soundShaders[] = {"IsuVoice_viktor_talk_01_Shader"};
		volumeFactor = 1.0;
		spatial = 1;
		doppler = 0;
		loop = 0;
	};
	class IsuVoice_viktor_talk_02_SoundSet
	{
		soundShaders[] = {"IsuVoice_viktor_talk_02_Shader"};
		volumeFactor = 1.0;
		spatial = 1;
		doppler = 0;
		loop = 0;
	};
	class IsuVoice_viktor_talk_03_SoundSet
	{
		soundShaders[] = {"IsuVoice_viktor_talk_03_Shader"};
		volumeFactor = 1.0;
		spatial = 1;
		doppler = 0;
		loop = 0;
	};
	class IsuVoice_viktor_talk_04_SoundSet
	{
		soundShaders[] = {"IsuVoice_viktor_talk_04_Shader"};
		volumeFactor = 1.0;
		spatial = 1;
		doppler = 0;
		loop = 0;
	};
	class IsuVoice_viktor_talk_05_SoundSet
	{
		soundShaders[] = {"IsuVoice_viktor_talk_05_Shader"};
		volumeFactor = 1.0;
		spatial = 1;
		doppler = 0;
		loop = 0;
	};
	class IsuVoice_viktor_bye_01_SoundSet
	{
		soundShaders[] = {"IsuVoice_viktor_bye_01_Shader"};
		volumeFactor = 1.0;
		spatial = 1;
		doppler = 0;
		loop = 0;
	};
	class IsuVoice_viktor_bye_02_SoundSet
	{
		soundShaders[] = {"IsuVoice_viktor_bye_02_Shader"};
		volumeFactor = 1.0;
		spatial = 1;
		doppler = 0;
		loop = 0;
	};
	class IsuVoice_viktor_bye_03_SoundSet
	{
		soundShaders[] = {"IsuVoice_viktor_bye_03_Shader"};
		volumeFactor = 1.0;
		spatial = 1;
		doppler = 0;
		loop = 0;
	};
	class IsuVoice_viktor_yes_01_SoundSet
	{
		soundShaders[] = {"IsuVoice_viktor_yes_01_Shader"};
		volumeFactor = 1.0;
		spatial = 1;
		doppler = 0;
		loop = 0;
	};
	class IsuVoice_viktor_no_01_SoundSet
	{
		soundShaders[] = {"IsuVoice_viktor_no_01_Shader"};
		volumeFactor = 1.0;
		spatial = 1;
		doppler = 0;
		loop = 0;
	};
	class IsuVoice_viktor_maybe_01_SoundSet
	{
		soundShaders[] = {"IsuVoice_viktor_maybe_01_Shader"};
		volumeFactor = 1.0;
		spatial = 1;
		doppler = 0;
		loop = 0;
	};
};
