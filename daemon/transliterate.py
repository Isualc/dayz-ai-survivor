"""Romanisierung der NPC-BILDSCHIRM-Texte (Sprechblase/Chat/Gedankenzeile).

Hintergrund: Der DayZ-Font `gui/fonts/sdf_MetronBook24` ist ein SDF-Atlas mit
(laut Font-Dump) nur ASCII + £. Nicht-lateinische Schriften (CJK, Arabisch,
Devanagari, Kyrillisch, Griechisch) haben darin keine Glyphen und erscheinen als
leere Kaestchen. Bis ein eigener Font gebaut ist (Phase 2), falten wir den
ON-SCREEN-Text dieser Sprachen auf ASCII.

WICHTIG - nur Bildschirm, nicht Audio: Diese Funktion wird NUR auf den Text
angewandt, der gezeichnet wird (Chat/Comic-Blase/Gedankenzeile). Der TTS-Pfad
(ElevenLabs/Discord) bekommt weiter den ORIGINALTEXT - gesprochenes Japanisch
oder Arabisch soll korrekt klingen, nur die Schrift am Bildschirm wird latinisiert.

WICHTIG - Latein bleibt unangetastet: de/fr/es/it/... (inkl. deutscher Umlaute
ae oe ue ss) werden NICHT gefaltet. Ob der Atlas Latin-1/Latin-Extended kann,
ist offen (ein In-Game-Test klaert es); bis dahin zerstoeren wir keine Umlaute.
Faellt der Test negativ aus, kann man de/... hier nachtraeglich aufnehmen.

Strategie je nicht-lateinischer Sprache (bestes Backend, sonst Fallback):
  ja -> Hepburn-Romaji (cutlet, sonst pykakasi, sonst unidecode)
  zh -> Pinyin mit Tonzahlen (pypinyin Style.TONE3 -> "zhong1"), sonst unidecode
  ko -> Revised Romanization (korean-romanizer), sonst unidecode
  hi -> ITRANS/Harvard-Kyoto (indic_transliteration), sonst unidecode
  ar -> unidecode (grobe Latinisierung; spaeter camel-tools/Buckwalter moeglich)
  ru/uk/el -> unidecode (solide Kyrillisch-/Griechisch-Romanisierung)

Alle Backends sind OPTIONAL und werden lazy importiert. Fehlt eins, faellt die
Sprache sauber auf unidecode zurueck; fehlt auch unidecode, auf einen reinen
ASCII-Strip. Es wird NIE eine Exception nach aussen gereicht - eine kaputte
Transliteration darf den NPC nicht stummschalten.

Am Ende steht immer eine ASCII-Haertung (NFKD + ascii-ignore), weil
Pinyin-Tonzeichen, Romaji-Makrons und IAST-Punkte selbst nicht im Atlas sind.

Baseline-Installation (reicht fuer alle Sprachen, grobe Qualitaet):
  pip install Unidecode
Qualitaets-Upgrades (optional):
  pip install cutlet fugashi unidic-lite   # Japanisch
  pip install pypinyin                      # Chinesisch
  pip install korean-romanizer              # Koreanisch
  pip install indic-transliteration         # Hindi
"""

from __future__ import annotations

import unicodedata

# Sprachen, deren Schrift der Stock-Font NICHT zeichnen kann -> latinisieren.
# Alles andere (Latein-Schrift inkl. de/fr/es/tr/pl/...) bleibt unveraendert.
NONLATIN_LANGS = {"ja", "zh", "ko", "hi", "ar", "ru", "uk", "el"}

# Backend-Caches: None = noch nicht versucht, False = nicht verfuegbar, sonst Objekt.
_CUTLET = None
_PYKAKASI = None
_KOREAN = None
_INDIC = None
_UNIDECODE = None


def _get_unidecode():
    """unidecode-Funktion oder None. Universeller Fallback fuer alle Skripte."""
    global _UNIDECODE
    if _UNIDECODE is None:
        try:
            from unidecode import unidecode  # type: ignore
            _UNIDECODE = unidecode
        except Exception:
            _UNIDECODE = False
    return _UNIDECODE or None


def _harden_ascii(text: str) -> str:
    """Restliche Diakritika/Nicht-ASCII entfernen (Tonzeichen, Makrons, IAST-Punkte
    sind selbst nicht im Atlas). NFKD zerlegt, ascii-ignore wirft den Rest weg."""
    if text.isascii():
        return text
    # Erst grob latinisieren (faengt z.B. Kyrillisch im gemischten Text), dann haerten.
    uni = _get_unidecode()
    if uni:
        try:
            text = uni(text)
        except Exception:
            pass
    norm = unicodedata.normalize("NFKD", text)
    return norm.encode("ascii", "ignore").decode("ascii")


def _romanize_ja(text: str) -> str:
    global _CUTLET, _PYKAKASI
    if _CUTLET is None:
        try:
            import cutlet  # type: ignore
            _CUTLET = cutlet.Cutlet()
        except Exception:
            _CUTLET = False
    if _CUTLET:
        try:
            return _CUTLET.romaji(text)
        except Exception:
            pass
    if _PYKAKASI is None:
        try:
            import pykakasi  # type: ignore
            _PYKAKASI = pykakasi.kakasi()
        except Exception:
            _PYKAKASI = False
    if _PYKAKASI:
        try:
            return " ".join(item["hepburn"] for item in _PYKAKASI.convert(text))
        except Exception:
            pass
    uni = _get_unidecode()
    return uni(text) if uni else text


def _romanize_zh(text: str) -> str:
    try:
        from pypinyin import lazy_pinyin, Style  # type: ignore
        # TONE3 = ASCII-Tonzahlen ("zhong1") statt Diakritika ("zhōng", nicht im Atlas).
        # Nicht-Han-Zeichen (Latein, Ziffern) reicht lazy_pinyin unveraendert durch.
        return " ".join(lazy_pinyin(text, style=Style.TONE3))
    except Exception:
        uni = _get_unidecode()
        return uni(text) if uni else text


def _romanize_ko(text: str) -> str:
    global _KOREAN
    if _KOREAN is None:
        try:
            from korean_romanizer.romanizer import Romanizer  # type: ignore
            _KOREAN = Romanizer
        except Exception:
            _KOREAN = False
    if _KOREAN:
        try:
            return _KOREAN(text).romanize()
        except Exception:
            pass
    uni = _get_unidecode()
    return uni(text) if uni else text


def _romanize_hi(text: str) -> str:
    global _INDIC
    if _INDIC is None:
        try:
            from indic_transliteration import sanscript  # type: ignore
            _INDIC = sanscript
        except Exception:
            _INDIC = False
    if _INDIC:
        try:
            return _INDIC.transliterate(text, _INDIC.DEVANAGARI, _INDIC.HK)
        except Exception:
            pass
    uni = _get_unidecode()
    return uni(text) if uni else text


def _romanize_generic(text: str) -> str:
    """ar/ru/uk/el und alles sonst Nicht-Lateinische: unidecode."""
    uni = _get_unidecode()
    return uni(text) if uni else text


_DISPATCH = {
    "ja": _romanize_ja,
    "zh": _romanize_zh,
    "ko": _romanize_ko,
    "hi": _romanize_hi,
}


def to_screen(text: str, lang: str) -> str:
    """Bildschirm-taugliche (ASCII) Fassung von `text`.

    Latein-Sprachen und schon reiner ASCII-Text kommen unveraendert zurueck.
    Nicht-lateinische Sprachen werden romanisiert und ASCII-gehaertet. Schlaegt
    irgendetwas fehl, wird der unveraenderte Originaltext zurueckgegeben (der
    NPC bleibt nie stumm)."""
    if not text:
        return text
    code = (lang or "").strip().lower()[:2]
    # Latein-Sprache oder bereits ASCII -> nichts tun (Umlaute NICHT zerstoeren).
    if code not in NONLATIN_LANGS:
        return text
    if text.isascii():
        return text
    try:
        fn = _DISPATCH.get(code, _romanize_generic)
        out = fn(text)
        out = _harden_ascii(out)
        out = " ".join(out.split())  # mehrfach-Whitespace aus der Romanisierung glaetten
        if not out.strip():
            # Romanisierung lieferte nichts Brauchbares -> harter Strip des Originals.
            out = _harden_ascii(text)
        return out or text
    except Exception:
        return text
