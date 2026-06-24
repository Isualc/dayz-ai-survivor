// IsuSurvivor — Einstiegspunkt: startet die Bridge und faengt Chat ab.
//
// Laeuft als -servermod (rein serverseitig); Clients brauchen diese Mod nicht.

modded class MissionServer
{
	override void OnInit()
	{
		super.OnInit();

		// Default-Slot sofort, weitere Slots per Datei-Discovery (Arena)
		IsuBridge.GetInstance("viktor");
		GetGame().GetCallQueue(CALL_CATEGORY_SYSTEM).CallLater(IsuBridge.TickDiscovery, 5000, true);

		// Agentennamen (Nametags) + Server-Marker an die Clients funken
		GetGame().GetCallQueue(CALL_CATEGORY_SYSTEM).CallLater(IsuBridge.BroadcastNametags, 3000, true);
		// Verwaiste Marker frueherer Laeufe aufraeumen (verzoegert, damit
		// die Expansion-Settings sicher geladen sind)
		GetGame().GetCallQueue(CALL_CATEGORY_SYSTEM).CallLater(IsuBridge.CleanupAgentMarkers, 15000, false);

		// Basislager der Agenten aufbauen (verzoegert, bis die Welt steht);
		// Position kommt aus camp.txt, umziehbar per In-Game-Menue
		GetGame().GetCallQueue(CALL_CATEGORY_SYSTEM).CallLater(IsuBaseCamp.Ensure, 12000, false);

		// Supervisor-Status (arena_status.txt) an die Clients funken
		GetGame().GetCallQueue(CALL_CATEGORY_SYSTEM).CallLater(IsuBridge.TickArenaStatus, 2000, true);

		// Befehlsrad/Direkttasten: alten Datei-Rest verwerfen, dann alle 0,5 s
		// auf neue Spielerbefehle pruefen (4_World schreibt, Bridge fuehrt aus)
		DeleteFile("$profile:IsuSurvivor/npc_command.txt");
		GetGame().GetCallQueue(CALL_CATEGORY_SYSTEM).CallLater(IsuBridge.TickNpcCommand, 500, true);
	}

	override void OnEvent(EventType eventTypeId, Param params)
	{
		super.OnEvent(eventTypeId, params);

		if (eventTypeId == ChatMessageEventTypeID)
		{
			// ChatMessageEventParams = Param4<int, string, string, string>
			//   param1 = Kanal, param2 = Absendername, param3 = Text
			ChatMessageEventParams chatParams;
			if (Class.CastTo(chatParams, params))
				IsuBridge.OnChatAll(chatParams.param1, chatParams.param2, chatParams.param3);
		}
	}
}
