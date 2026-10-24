"""Gemeinsame Pfad-Auflösung für Agenten-Homes.

Bis 2026-08 lag diese Funktion als identische Kopie in fünf Dateien
(arena_supervisor, dayz_mcp, missions, orchestrator, voice_router) - vor der
Erweiterung auf dynamische NPC-Slots hier konsolidiert, damit ein neuer
Sonderfall nie wieder an fünf Stellen gepflegt werden muss.
"""

import os

REPO_DIR = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))


def agent_home_dir(aid: str) -> str:
    """Arbeitsverzeichnis eines Agenten (CLAUDE.md, Journale, Voice-Dateien).

    Historischer Sonderfall: Viktor wohnt in agent_home/ (Singular), alle
    anderen - auch dynamisch angelegte npc5..npc10 - in agent_homes/<id>.
    run_agent.py legt das Verzeichnis beim ersten Start selbst an.
    """
    if aid == "viktor":
        return os.path.join(REPO_DIR, "agent_home")
    return os.path.join(REPO_DIR, "agent_homes", aid)
