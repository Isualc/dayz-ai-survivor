#!/usr/bin/env python3
"""Sprechbar-Machen von Zahlen fuer ElevenLabs-TTS (Deutsch).

ElevenLabs verhaspelt sich an rohen Koordinaten (x=4540 z=2525, 3755/9013)
und Kaliberangaben (9x39, 7,62x39, 12ga) und liefert Kauderwelsch. Diese
Funktion wandelt genau diese Muster VOR dem TTS-Call in gut sprechbaren
deutschen Text um:

  Koordinaten -> ziffernweise (Funk-Standard, eindeutig):
      "x=4540 z=2525"   -> "vier fuenf vier null, zwei fuenf zwei fuenf"
      "3755/9013"       -> "drei sieben fuenf fuenf, neun null eins drei"
  Kaliber:
      "9x39"            -> "neun mal neununddreissig"
      "7,62x39"         -> "sieben sechs zwei mal neununddreissig"
      "12ga"            -> "Kaliber zwoelf"
      ".308" / ".45"    -> "Punkt drei null acht" / "Punkt vier fuenf"
      "9mm"             -> "neun Millimeter"
      "2x SmallStone"   -> "zwei mal SmallStone"

Bewusst UNANGETASTET (kein Kauderwelsch, ElevenLabs kann das):
  - normale Mengen: "36 Meter", "15 Items", "HP 65", "100 Chemlights"
  - Waffen-/Item-Namen mit Ziffern: SSG82, CZ527, Izh18, AK74, Saiga12
  - der Slang "x0" (kaputte Waffe), bleibt stehen

Echte deutsche Umlaute im Output (TTS liest sie korrekt); die Docstrings hier
nutzen ae/oe/ue nur, damit die Datei auf jedem Editor sauber bleibt.
"""
import re

_ONES = ["null", "eins", "zwei", "drei", "vier", "fünf", "sechs", "sieben",
         "acht", "neun", "zehn", "elf", "zwölf", "dreizehn", "vierzehn",
         "fünfzehn", "sechzehn", "siebzehn", "achtzehn", "neunzehn"]
_TENS = ["", "", "zwanzig", "dreißig", "vierzig", "fünfzig", "sechzig",
         "siebzig", "achtzig", "neunzig"]
_DIG = {"0": "null", "1": "eins", "2": "zwei", "3": "drei", "4": "vier",
        "5": "fünf", "6": "sechs", "7": "sieben", "8": "acht", "9": "neun"}


def _below100(n: int) -> str:
    if n < 20:
        return _ONES[n]
    t, o = divmod(n, 10)
    if o == 0:
        return _TENS[t]
    ones = "ein" if o == 1 else _ONES[o]
    return f"{ones}und{_TENS[t]}"


def _below1000(n: int) -> str:
    h, r = divmod(n, 100)
    out = ""
    if h:
        out += ("ein" if h == 1 else _ONES[h]) + "hundert"
    if r:
        out += _below100(r)
    return out or "null"


def num_de(n) -> str:
    """Ganzzahl -> deutsche Kardinalzahl (0..999999)."""
    n = int(n)
    if n < 0:
        return "minus " + num_de(-n)
    if n < 1000:
        return _below1000(n)
    if n < 1000000:
        th, r = divmod(n, 1000)
        out = ("ein" if th == 1 else _below1000(th)) + "tausend"
        if r:
            out += _below1000(r)
        return out
    return str(n)


def digits_de(s: str) -> str:
    """Ziffernfolge -> ziffernweise gesprochen ('4540' -> 'vier fuenf vier null')."""
    return " ".join(_DIG[c] for c in s if c in _DIG)


def _coord_pair(m: "re.Match") -> str:
    return f"{digits_de(m.group(1))}, {digits_de(m.group(2))}"


# Achsbuchstabe nur als eigenstaendiges x/z werten (nicht in "max=", "Holz=")
_AX = r"(?<![A-Za-zÄÖÜäöü])"

# Abkuerzungen, die ElevenLabs falsch liest ("ca." als "ka", "z.B." buchstabiert).
# Der Punkt macht sie eindeutig -> gefahrlos ausschreibbar. (?i) = Gross/Klein egal.
_ABBREV = [
    (re.compile(r"(?i)\bz\.\s?b\."), "zum Beispiel"),
    (re.compile(r"(?i)\bd\.\s?h\."), "das heißt"),
    (re.compile(r"(?i)\bu\.\s?a\."), "unter anderem"),
    (re.compile(r"(?i)\bs\.\s?o\."), "siehe oben"),
    (re.compile(r"(?i)\busw\."), "und so weiter"),
    (re.compile(r"(?i)\bbzw\."), "beziehungsweise"),
    (re.compile(r"(?i)\betc\."), "et cetera"),
    (re.compile(r"(?i)\bggf\."), "gegebenenfalls"),
    (re.compile(r"(?i)\bevtl\."), "eventuell"),
    (re.compile(r"(?i)\bvgl\."), "vergleiche"),
    (re.compile(r"(?i)\binkl\."), "inklusive"),
    (re.compile(r"(?i)\bungef\."), "ungefähr"),
    (re.compile(r"(?i)\bNr\."), "Nummer"),
    (re.compile(r"(?i)\bMio\."), "Millionen"),
    (re.compile(r"(?i)\bMrd\."), "Milliarden"),
    (re.compile(r"(?i)\bStd\."), "Stunden"),
    # "ca." oder blankes "ca" vor Leerzeichen -> circa. "Camp"/"circa" bleiben
    # unberuehrt (kein Wortanfang-"ca" + Punkt/Leerzeichen dahinter).
    (re.compile(r"(?i)\bca\.?(?=\s)"), "circa"),
]
# Sonderzeichen, die TTS verschluckt oder falsch liest.
_SYMBOLS = [
    (re.compile(r"~\s*"), "circa "),
    (re.compile(r"\s*°\s*C\b"), " Grad Celsius"),
    (re.compile(r"\s*°"), " Grad"),
    (re.compile(r"\s*%"), " Prozent"),
    (re.compile(r"\s*&\s*"), " und "),
]


def normalize_for_tts(text: str) -> str:
    if not text:
        return text
    t = text

    # ---------- Abkuerzungen & Sonderzeichen ausschreiben (ZUERST, damit die
    # Punkte in "z.B." nicht spaeter in die Kaliber-/Koordinaten-Regex geraten) --
    for _pat, _rep in _ABBREV:
        t = _pat.sub(_rep, t)
    for _pat, _rep in _SYMBOLS:
        t = _pat.sub(_rep, t)

    # ---------- Kaliber zuerst (brauchen x/Komma/Punkt zwischen Ziffern) -------
    # 7,62x39 / 5.56x45 -> Praefix ziffernweise, Rest Kardinal
    t = re.sub(
        r"(?<!\d)(\d{1,2})[.,](\d{2})\s*[x×]\s*(\d{2,3})(?!\d)",
        lambda m: f"{digits_de(m.group(1) + m.group(2))} mal {num_de(m.group(3))}",
        t)
    # 9x39 / 7x57 (ohne Komma) -> "neun mal neununddreissig"
    t = re.sub(
        r"(?<![\w.,])(\d{1,2})\s*[x×]\s*(\d{2})(?!\d)",
        lambda m: f"{num_de(m.group(1))} mal {num_de(m.group(2))}",
        t)
    # 12ga / 12 ga -> "Kaliber zwoelf"
    t = re.sub(
        r"(?<!\d)(\d{1,2})\s*ga\b",
        lambda m: f"Kaliber {num_de(m.group(1))}",
        t, flags=re.IGNORECASE)
    # .308 / .45 / .357 -> "Punkt drei null acht"
    t = re.sub(
        r"(?<!\d)\.(\d{2,3})\b",
        lambda m: f"Punkt {digits_de(m.group(1))}",
        t)
    # 9mm / 12 mm -> "neun Millimeter"
    t = re.sub(
        r"(?<!\d)(\d{1,2})\s*mm\b",
        lambda m: f"{num_de(m.group(1))} Millimeter",
        t, flags=re.IGNORECASE)
    # 2x SmallStone -> "zwei mal SmallStone" (Ziffer x Leerzeichen Wort)
    t = re.sub(
        r"(?<!\d)(\d{1,2})\s*x\s+(?=[A-Za-zÄÖÜäöü])",
        lambda m: f"{num_de(m.group(1))} mal ",
        t)

    # ---------------------------- Einheiten -----------------------------------
    # Abgekuerzte Einheiten hinter einer Zahl ausschreiben: "300m" -> "300 Meter",
    # "2km" -> "2 Kilometer". km VOR m. Der \b-Anker schuetzt "mm" (oben schon
    # Millimeter), die Lookbehind (_AX, kein Buchstabe davor) schuetzt Waffen-
    # namen wie "Izh18m". Die Zahl bleibt Ziffer (ElevenLabs liest "300 Meter"
    # sauber); ausgeschriebenes "Meter" wird durch das kleine "m" NICHT getroffen.
    t = re.sub(_AX + r"(\d{1,4})\s*km\b", lambda m: f"{m.group(1)} Kilometer", t)
    t = re.sub(_AX + r"(\d{1,4})\s*m\b", lambda m: f"{m.group(1)} Meter", t)

    # ---------------------------- Koordinaten ---------------------------------
    # x=4540 z=2525 / x=6919, z=2817 (Paar)
    t = re.sub(
        _AX + r"[xX]\s*=\s*(\d{3,5})\s*[, ]+\s*[zZ]\s*=\s*(\d{3,5})",
        _coord_pair, t)
    # 3755/9013 (Slash-Paar, 3-5 Ziffern je Seite)
    t = re.sub(
        r"(?<!\d)(\d{3,5})\s*/\s*(\d{3,5})(?!\d)",
        _coord_pair, t)
    # Cue + zwei 4-5-stellige Zahlen: "Koordinaten: 4432 8356", "bei 4540 2525"
    t = re.sub(
        r"(?i)(koordinat\w*|position|\bpos\b|\bbei\b)([\s:,\-]+)(\d{4,5})\s+(\d{4,5})",
        lambda m: f"{m.group(1)}{m.group(2)}{digits_de(m.group(3))}, "
                  f"{digits_de(m.group(4))}",
        t)
    # Einzelnes x=6300 / z=2817 (Rest nach dem Paar-Fall)
    t = re.sub(_AX + r"[xX]\s*=\s*(\d{3,5})", lambda m: digits_de(m.group(1)), t)
    t = re.sub(_AX + r"[zZ]\s*=\s*(\d{3,5})", lambda m: digits_de(m.group(1)), t)

    return t


if __name__ == "__main__":
    # Selbsttest gegen echte Journal-Beispiele + Randfaelle. Aufruf:
    #   python daemon/tts_normalize.py
    CASES = [
        # --- echte Journal-Zeilen (mcp__dayz__say) ---
        "Spawne seit acht Leben in einer Todeszone bei x=4540 z=2525.",
        "Ich bin gerade ostwärts unterwegs, irgendwo bei x=6300.",
        "Treffen klingt vernünftig. Bin bei x=6919, z=2817.",
        "Viktor — Koordinaten: 4432 8356. Camp-Bereich. Konrad ist hier.",
        "Konrad — verstanden! 3755/9013 — Medizin. FirstAidKit.",
        "Nördlich vom Lager, x=4510 z=2308. Magen kritisch.",
        "Konrad, komm zum Lager zurück! x=4524 z=2440. Fleisch kommt.",
        "Bin bei x=4552 z=8299, Osten vom Lager.",
        "Merk mir: Vikhr = 9x39, EINZELFEUER, jede Patrone zählt.",
        "5,45er Tracer-Munition gefunden. Nicht für meine Waffen.",
        "Hab 12ga Slugs. Leg ich dir hin.",
        "Rezept: 2x SmallStone = Steinmesser. Lass mich Steine sammeln.",
        # --- darf NICHT angefasst werden ---
        "die Burschen bringen SSG82 mit. Gute Waffen.",
        "CZ527 — hab ich wohl nicht gesehen. Ist drin.",
        "Viktor, meine Shotgun auch x0. Nur Messer funktioniert.",
        "Infizierter, 36 Meter südlich. Vorsicht.",
        "Versorgung im Zelt (15 Items), Feuer läuft.",
        "Ich hab 100 Chemlights! Zurück zum Lager.",
        "HP 65 ist zu wenig. Komm her.",
        "Saiga12 und AK74 sind im Zelt.",
        # --- zusätzliche Kaliber/Rand ---
        "7,62x39 und 5.56x45 Munition gefunden.",
        ".308 Win und .45 ACP liegen hier.",
        "Hab 9mm und ein paar Patronen.",
        "Treffen bei 4540 2525 oder am Punkt 3700/9000.",
        "max=500 Liter Wasser im Tank.",  # Falle: darf nicht zu Koordinate werden
        # --- Abkuerzungen & Symbole (sollen ausgeschrieben werden) ---
        "Treffpunkt ca. 300 Meter weiter, z.B. beim Brunnen.",
        "HP bei 65%, Kälte usw. - bzw. später nachschauen.",
        "~500 Meter östlich, ggf. Loot.",
        "Wasser & Feuer, d.h. überleben.",
        "Temperatur 4°C, Nr. 3 im Zelt.",
        # --- Camp darf NICHT zu "circamp" werden ---
        "Zurück zum Camp, ca. zehn Minuten.",
        # --- Einheiten m/km hinter Zahl ausschreiben ---
        "~300m östlich, noch 2km bis zum Lager.",
        "Infizierter 36m südlich, komm runter auf 5m.",
        # --- diese duerfen NICHT angefasst werden ---
        "Infizierter, 36 Meter südlich. Vorsicht.",   # ausgeschrieben bleibt
        "Merk mir: Vikhr = 9x39. Hab auch 9mm dabei.",  # 9mm bleibt Millimeter
    ]
    for c in CASES:
        out = normalize_for_tts(c)
        flag = "   " if out != c else " = "
        print(f"{flag}IN : {c}")
        print(f"    OUT: {out}\n")
