# Publishing the mod to the Steam Workshop

The mod has two halves — **@IsuSurvivor** (server-side bridge) and **@IsuVoice** (client-side menu / voice / nameplates). They are published as **two separate Workshop items** with the DayZ Tools **Publisher**.

> **Both halves are only the in-game layer.** Without the companion Python + Claude Code daemon (this repository) the mod does nothing useful for a subscriber, and it requires **DayZ-Expansion-AI**. The Workshop description below makes that explicit and links back here — keep it.

## Prerequisites

- **DayZ Tools** installed (Steam → Library → Tools → *DayZ Tools*, app 830640).
- **Steam running and logged in.**
- The publish-ready mod folders, each containing `addons\*.pbo`, `mod.cpp`, and `preview.jpg`:
  - `build\@IsuSurvivor`
  - `build\@IsuVoice`
  (These are prepared in the working project. The PBOs come from `tools\pack_mod.ps1`.)
- You must **accept the Steam Workshop Legal Agreement once**: <https://steamcommunity.com/sharedfiles/workshoplegalagreement> (the Publisher also prompts you on the first upload). This is a personal/legal step — only you can accept it.

## Steps (per mod)

1. Start **Steam** and log in.
2. Launch the **Publisher**: Steam → Library → Tools → *DayZ Tools* → **Launch** → *Publisher*; or run it directly:
   `…\DayZ Tools\Bin\Publisher\Publisher.exe`
3. **New** → choose **Mod**.
4. **Directory**: point it at the mod folder (the one with `addons\` and `mod.cpp`):
   - `…\dayz-ai-survivor\build\@IsuSurvivor`
5. **Title**: `ISU Survivor Agent Bridge`.
6. **Preview image**: select `build\@IsuSurvivor\preview.jpg`.
7. **Visibility**: **Hidden / Unlisted** (or Private) for the first upload — verify the page before going Public.
8. **Description**: paste the prepared text below.
9. **Tags**: tick **Mod** (plus any that apply).
10. **Publish**. Accept the Workshop Legal Agreement if prompted (you do this), then wait for the upload to finish.
11. Repeat 3–10 for **@IsuVoice**: directory `build\@IsuVoice`, title `ISU Survivor Voice`, preview `build\@IsuVoice\preview.jpg`.

## After publishing

- The Publisher writes a **`meta.cpp`** into each mod folder (it holds the `publishedid`). **Keep it** — future updates reuse it to update the *same* Workshop item instead of creating a duplicate.
- The items start Hidden/Unlisted. Open each Workshop page, check the preview, description and required-items note, then flip to **Public** (Workshop page → *Edit* → *Change Visibility*) when you're happy.
- **Updating later**: rebuild the PBO (`tools\pack_mod.ps1` / `pack_mod.ps1 -ModName IsuVoice`), open the Publisher, select the existing item, **Publish** — it updates via the `meta.cpp` `publishedid`.
- Optional: the DayZ Launcher prefers a `.paa` logo. To show a logo in the launcher, convert `preview.jpg` with **ImageToPAA** to `logo.paa` and point `mod.cpp` `picture`/`logo` at it. Not needed for the Workshop itself.

---

## Ready-to-paste Workshop descriptions

**@IsuSurvivor — "ISU Survivor Agent Bridge"**

```
ISU Survivor Agent Bridge — the SERVER-SIDE half of an autonomous DayZ survivor driven by Claude AI.

IMPORTANT: this mod is only the bridge / motor / sensing layer. On its own it does nothing
visible — it needs the companion Python + Claude Code daemon to act as the "brain", plus the
client mod @IsuVoice for the in-game menu, voice and nameplates.

Full project, source code and step-by-step setup:
https://github.com/Isualc/dayz-ai-survivor

Required: DayZ-Expansion-AI (and its chain: CF, Dabs Framework, Expansion-Core).
Also use with: @IsuVoice.

Open-source (MIT). Intended for local / private servers.
```

**@IsuVoice — "ISU Survivor Voice"**

```
ISU Survivor Voice — the CLIENT-SIDE half of the ISU Survivor AI project: the in-game setup
menu (Insert key), the command wheel, floating NPC nameplates, and 3D voice lines for the
Claude-driven survivors.

Use together with @IsuSurvivor and the companion Python + Claude Code daemon.

Full project, source code and setup:
https://github.com/Isualc/dayz-ai-survivor

Open-source (MIT).
```
