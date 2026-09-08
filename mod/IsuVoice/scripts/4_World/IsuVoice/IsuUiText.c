// Server-selected language for the Isu UI only. NPC speech/persona languages
// and the player's DayZ settings are independent of this shared menu state.
class IsuUiText
{
	protected static string s_Language = "en";
	protected static PlayerBase s_Player;
	protected static bool s_Received;
	protected static float s_RequestElapsed;
	static int s_Revision = 0;

	static bool IsGerman()
	{
		return s_Language == "de";
	}

	static string Choose(string english, string german)
	{
		if (IsGerman())
			return german;
		return english;
	}

	static string GetLanguage()
	{
		return s_Language;
	}

	static void SetLanguage(string language)
	{
		language = language.Trim();
		language.ToLower();
		if (language != "de" && language != "en")
			return;
		if (s_Language == language)
			return;
		s_Language = language;
		s_Revision++;
	}

	// Called from the existing MissionGameplay.OnUpdate. There are no timers
	// attached to every replicated player and no client settings file writes.
	static void Tick(float timeslice)
	{
		if (!GetGame() || GetGame().IsDedicatedServer())
			return;
		PlayerBase player = PlayerBase.Cast(GetGame().GetPlayer());
		if (player != s_Player)
		{
			s_Player = player;
			s_Received = false;
			s_RequestElapsed = 0;
			SetLanguage("en");
		}
		if (!player)
			return;
		s_RequestElapsed += timeslice;
		float interval = 2.0;
		if (s_Received)
			interval = 30.0;
		if (s_RequestElapsed < interval)
			return;
		s_RequestElapsed = 0;
		// Protocol 1 is a read-only request; it never carries a chosen language.
		Param1<int> request = new Param1<int>(1);
		GetGame().RPCSingleParam(player, ISU_RPC_UI_LANGUAGE_REQUEST, request, true);
	}

	static void AcceptServerLanguage(string language)
	{
		if (language != "de" && language != "en")
			return;
		SetLanguage(language);
		s_Received = true;
		s_RequestElapsed = 0;
	}
}
