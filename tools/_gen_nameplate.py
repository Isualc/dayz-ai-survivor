#!/usr/bin/env python3
# Generiert isu_nameplate.layout: 6 Slots mit Name/Act/HP-Balken/Intent + Comic-
# Sprechblase (SpeechBg + Speech) ueber dem Kopf. clipchildren 0 pro Tag, damit
# die Blase oberhalb (negative y) sichtbar bleibt. Die Blase wird vom HUD
# (IsuNameplateHud) per GetTextSize responsiv dimensioniert.
import os

OUT = os.path.join(os.path.dirname(__file__), "..", "mod", "IsuVoice", "GUI",
                   "isu_nameplate.layout")

SLOT = """  FrameWidgetClass Tag__I__ {
   position 100 __Y__
   size 360 72
   hexactpos 1
   vexactpos 1
   hexactsize 1
   vexactsize 1
   clipchildren 0
   {
    TextWidgetClass Name__I__ {
     ignorepointer 1
     position 0 0
     size 360 22
     hexactpos 1
     vexactpos 1
     hexactsize 1
     vexactsize 1
     text "Name"
     font "gui/fonts/sdf_MetronBook24"
     color 0.9 0.9 0.9 1
    }
    TextWidgetClass Act__I__ {
     ignorepointer 1
     position 0 22
     size 360 16
     hexactpos 1
     vexactpos 1
     hexactsize 1
     vexactsize 1
     text ""
     font "gui/fonts/sdf_MetronBook24"
     color 0.7 0.7 0.72 1
    }
    ImageWidgetClass HpBg__I__ {
     ignorepointer 1
     color 0.10 0.11 0.13 0.0
     position 4 42
     size 160 6
     hexactpos 1
     vexactpos 1
     hexactsize 1
     vexactsize 1
     mode blend
    }
    ImageWidgetClass HpFill__I__ {
     ignorepointer 1
     color 0.48 0.78 0.30 1
     position 4 42
     size 160 6
     hexactpos 1
     vexactpos 1
     hexactsize 1
     vexactsize 1
     mode blend
    }
    MultilineTextWidgetClass Intent__I__ {
     ignorepointer 1
     position 0 50
     size 360 44
     hexactpos 1
     vexactpos 1
     hexactsize 1
     vexactsize 1
     text ""
     font "gui/fonts/sdf_MetronBook24"
     "text color" 0.55 0.57 0.60 1
     wrap 1
    }
    ImageWidgetClass SpeechBg__I__ {
     ignorepointer 1
     color 0.05 0.05 0.07 0.0
     position 0 -64
     size 360 58
     hexactpos 1
     vexactpos 1
     hexactsize 1
     vexactsize 1
     mode blend
    }
    MultilineTextWidgetClass Speech__I__ {
     ignorepointer 1
     position 10 -60
     size 340 50
     hexactpos 1
     vexactpos 1
     hexactsize 1
     vexactsize 1
     text ""
     font "gui/fonts/sdf_MetronBook24"
     "text color" 0.08 0.08 0.10 1
     wrap 1
    }
   }
  }"""

HEAD = """FrameWidgetClass IsuNameplateRoot {
 position 0 0
 size 2560 1440
 hexactpos 1
 vexactpos 1
 hexactsize 1
 vexactsize 1
 clipchildren 0
 {"""
TAIL = """ }
}"""

blocks = []
for i in range(6):
    blocks.append(SLOT.replace("__I__", str(i)).replace("__Y__", str(100 + i * 100)))

content = HEAD + "\n" + "\n".join(blocks) + "\n" + TAIL + "\n"
with open(os.path.abspath(OUT), "w", encoding="utf-8") as f:
    f.write(content)
print("geschrieben:", os.path.abspath(OUT))
print("Zeilen:", content.count("\n"), "| { :", content.count("{"), "| } :", content.count("}"))
