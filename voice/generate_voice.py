#!/usr/bin/env python3
"""IsuVoice-Generator: Phrasenkatalog -> TTS-Oggs + komplette @IsuVoice-Mod-Config.

Mit gesetztem ELEVENLABS_API_KEY werden echte Stimmen generiert, ohne Key
entstehen stille Platzhalter (0,6 s), damit die Pipeline testbar bleibt.
Bereits generierte Oggs werden uebersprungen (Cache); --force erzwingt neu.

  python generate_voice.py                 # generieren (TTS oder Stille)
  python generate_voice.py --force         # alles neu generieren
  python generate_voice.py --list-voices   # verfuegbare ElevenLabs-Stimmen
"""

import argparse
import json
import os
import subprocess
import sys

import requests

VOICE_DIR = os.path.dirname(os.path.abspath(__file__))
REPO_DIR = os.path.dirname(VOICE_DIR)
PHRASES_FILE = os.path.join(VOICE_DIR, "phrases.json")
SOUNDSETS_FILE = os.path.join(VOICE_DIR, "soundsets.json")
MOD_DIR = os.path.join(REPO_DIR, "mod", "IsuVoice")
API = "https://api.elevenlabs.io/v1"

CONFIG_TEMPLATE = """class CfgPatches
{{
	class IsuVoice
	{{
		units[] = {{}};
		weapons[] = {{}};
		requiredVersion = 0.1;
		requiredAddons[] = {{ "DZ_Data", "DZ_Sounds_Effects" }};
	}};
}};

class CfgMods
{{
	class IsuVoice
	{{
		dir = "IsuVoice";
		name = "ISU Survivor Voice";
		credits = "isualc AI";
		author = "isualc AI";
		version = "0.1.0";
		extra = 0;
		type = "mod";

		dependencies[] = {{"World"}};

		class defs
		{{
			class worldScriptModule
			{{
				value = "";
				files[] = {{"IsuVoice/scripts/4_World"}};
			}};
		}};
	}};
}};

class CfgSoundShaders
{{
{shaders}
}};

class CfgSoundSets
{{
{soundsets}
}};
"""

SHADER_TEMPLATE = """	class {name}_Shader
	{{
		samples[] = {{{{"IsuVoice\\sounds\\{character}\\{id}.ogg", 1}}}};
		volume = 1.8;
		range = 80;
	}};"""

SOUNDSET_TEMPLATE = """	class {name}_SoundSet
	{{
		soundShaders[] = {{"{name}_Shader"}};
		volumeFactor = 1.0;
		spatial = 1;
		doppler = 0;
		loop = 0;
	}};"""


def list_voices(api_key: str) -> None:
    r = requests.get(f"{API}/voices", headers={"xi-api-key": api_key}, timeout=30)
    r.raise_for_status()
    for v in r.json().get("voices", []):
        labels = ", ".join(f"{k}={val}" for k, val in (v.get("labels") or {}).items())
        print(f"{v['name']:24} {v['voice_id']}  {labels}")


def resolve_voice_id(api_key: str, name: str) -> str:
    r = requests.get(f"{API}/voices", headers={"xi-api-key": api_key}, timeout=30)
    r.raise_for_status()
    voices = r.json().get("voices", [])
    for v in voices:
        if name.lower() in v["name"].lower():
            return v["voice_id"]
    if voices:
        print(f"Stimme '{name}' nicht im Konto - nutze '{voices[0]['name']}'. "
              f"(--list-voices zeigt alle; voice_name in phrases.json anpassen)")
        return voices[0]["voice_id"]
    raise SystemExit("Keine Stimmen im ElevenLabs-Konto.")


def tts_mp3(api_key: str, voice_id: str, model_id: str, text: str, out_mp3: str) -> None:
    r = requests.post(
        f"{API}/text-to-speech/{voice_id}?output_format=mp3_44100_128",
        headers={"xi-api-key": api_key, "Content-Type": "application/json"},
        json={"text": text, "model_id": model_id,
              "voice_settings": {"stability": 0.45, "similarity_boost": 0.8}},
        timeout=120,
    )
    r.raise_for_status()
    with open(out_mp3, "wb") as f:
        f.write(r.content)


def mp3_to_ogg(mp3: str, ogg: str) -> None:
    subprocess.run(
        ["ffmpeg", "-y", "-loglevel", "error", "-i", mp3,
         "-ac", "1", "-ar", "44100", "-c:a", "libvorbis", "-q:a", "4", ogg],
        check=True,
    )


def silence_ogg(ogg: str, seconds: float = 0.6) -> None:
    subprocess.run(
        ["ffmpeg", "-y", "-loglevel", "error",
         "-f", "lavfi", "-i", "anullsrc=r=44100:cl=mono",
         "-t", str(seconds), "-c:a", "libvorbis", ogg],
        check=True,
    )


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--force", action="store_true", help="Oggs neu generieren")
    parser.add_argument("--list-voices", action="store_true")
    parser.add_argument("--voice", default="", help="Stimmenname ueberschreiben")
    args = parser.parse_args()

    api_key = os.environ.get("ELEVENLABS_API_KEY", "")

    if args.list_voices:
        if not api_key:
            print("ELEVENLABS_API_KEY nicht gesetzt.")
            return 1
        list_voices(api_key)
        return 0

    with open(PHRASES_FILE, "r", encoding="utf-8") as f:
        catalog = json.load(f)

    character = catalog["character"]
    voice_name = args.voice or catalog["voice_name"]
    model_id = catalog["model_id"]
    phrases = catalog["phrases"]

    voice_id = ""
    if api_key:
        voice_id = resolve_voice_id(api_key, voice_name)
        print(f"TTS aktiv: Stimme '{voice_name}' ({voice_id}), Modell {model_id}")
    else:
        print("ELEVENLABS_API_KEY nicht gesetzt -> stille Platzhalter (0,6 s).")

    sounds_dir = os.path.join(MOD_DIR, "sounds", character)
    os.makedirs(sounds_dir, exist_ok=True)

    shaders, soundsets, mapping = [], [], {}
    generated, skipped = 0, 0

    for p in phrases:
        pid = p["id"]
        name = f"IsuVoice_{character}_{pid}"
        ogg = os.path.join(sounds_dir, f"{pid}.ogg")

        if args.force or not os.path.exists(ogg):
            if api_key:
                mp3 = ogg + ".tmp.mp3"
                print(f"  TTS: {pid}: {p['text']}")
                tts_mp3(api_key, voice_id, model_id, p["text"], mp3)
                mp3_to_ogg(mp3, ogg)
                os.remove(mp3)
            else:
                silence_ogg(ogg)
            generated += 1
        else:
            skipped += 1

        shaders.append(SHADER_TEMPLATE.format(name=name, character=character, id=pid))
        soundsets.append(SOUNDSET_TEMPLATE.format(name=name))
        mapping[pid] = {
            "soundset": f"{name}_SoundSet",
            "text": p["text"],
            "category": p["category"],
            "ogg": f"mod/IsuVoice/sounds/{character}/{pid}.ogg",
        }

    config = CONFIG_TEMPLATE.format(shaders="\n".join(shaders),
                                    soundsets="\n".join(soundsets))
    os.makedirs(MOD_DIR, exist_ok=True)
    with open(os.path.join(MOD_DIR, "config.cpp"), "w", encoding="ascii") as f:
        f.write(config)

    with open(SOUNDSETS_FILE, "w", encoding="utf-8") as f:
        json.dump(mapping, f, indent=2, ensure_ascii=False)

    prefix_file = os.path.join(MOD_DIR, "$PBOPREFIX$")
    if not os.path.exists(prefix_file):
        with open(prefix_file, "w", encoding="ascii") as f:
            f.write("IsuVoice\n")

    print(f"Fertig: {generated} generiert, {skipped} uebersprungen (Cache).")
    print(f"config.cpp + soundsets.json aktualisiert ({len(phrases)} Phrasen).")
    return 0


if __name__ == "__main__":
    sys.exit(main())
