"""Shared MyMemory translation for Media-Buddy V2; no deep_translator dependency."""
from __future__ import annotations
from dataclasses import dataclass
import html
import re
import time
import requests
from pathlib import Path

def automatic_translation_enabled():
    """Read the global setting on demand; default to the legacy enabled behavior."""
    try:
        from configuration_store import get_value
        config = Path(__file__).resolve().parent / "Media-Buddy.json"
        return get_value(config, ("Metadata", "AutomaticTranslation"), True) is not False
    except (OSError, ValueError, TypeError):
        return True


URL = "https://api.mymemory.translated.net/get"

def _contact_email():
    try:
        from configuration_store import get_value
        value = get_value(Path(__file__).resolve().parent / 'Media-Buddy.json',
                          ('Metadata', 'MyMemoryEmail'), '')
        value = str(value or '').strip()
        # Reject malformed input rather than sending it to the service.
        if re.fullmatch(r"[^\s@]+@[^\s@]+\.[^\s@]+", value):
            return value
    except (OSError, ValueError, TypeError):
        pass
    return ''


@dataclass(frozen=True)
class TranslationResult:
    success: bool
    text: str
    provider: str = "MyMemory"
    chunks: int = 0
    error: str = ""

LANGUAGE_CODES_3_TO_2 = {
    "eng":"en", "deu":"de", "ger":"de", "fra":"fr", "fre":"fr",
    "spa":"es", "ita":"it", "por":"pt", "nld":"nl", "dut":"nl",
    "swe":"sv", "nor":"no", "dan":"da", "fin":"fi", "pol":"pl",
    "ces":"cs", "cze":"cs", "slk":"sk", "hun":"hu", "ron":"ro",
    "rum":"ro", "bul":"bg", "hrv":"hr", "srp":"sr", "slv":"sl",
    "ell":"el", "gre":"el", "tur":"tr", "rus":"ru", "ukr":"uk",
    "ara":"ar", "heb":"he", "hin":"hi", "jpn":"ja", "kor":"ko",
    "zho":"zh", "chi":"zh", "tha":"th", "vie":"vi", "ind":"id",
    "msa":"ms", "may":"ms", "cat":"ca", "eus":"eu", "baq":"eu",
    "glg":"gl", "isl":"is", "ice":"is", "est":"et", "lav":"lv",
    "lit":"lt", "fas":"fa", "per":"fa", "ben":"bn", "tam":"ta",
}

def normalize_language(value):
    if not isinstance(value, str):
        return ""
    code = value.strip().lower().replace("_", "-").split("-", 1)[0]
    return LANGUAGE_CODES_3_TO_2.get(code, code if re.fullmatch(r"[a-z]{2}", code) else "")

def split_text(text, max_bytes=450):
    """Exactly reproduce movie scraper UTF-8-safe chunking."""
    if max_bytes < 4:
        raise ValueError("max_bytes must be at least 4")
    chunks, current = [], ""
    for token in re.findall(r"\S+\s*|\s+", text):
        for char in token:
            if len((current + char).encode("utf-8")) > max_bytes:
                if current:
                    chunks.append(current)
                current = ""
            current += char
    if current:
        chunks.append(current)
    return chunks

def translate_plot(text, source, target="de", *, timeout=(8, 12),
                   max_bytes=450, delay_seconds=0.3, user_agent="Media-Buddy-V2/44",
                   session=None):
    """Translate with film-scraper semantics. All-or-nothing, original on error."""
    if not text or not text.strip() or not automatic_translation_enabled():
        return TranslationResult(True, text)
    src, dst = normalize_language(source), normalize_language(target)
    if not src or not dst:
        return TranslationResult(False, text, error="Unknown source or target language")
    if src == dst:
        return TranslationResult(True, text)
    if isinstance(timeout, (tuple, list)):
        limited_timeout = (min(timeout[0], 8), min(timeout[1], 12))
    else:
        limited_timeout = min(timeout, 12)
    chunks = split_text(text, max_bytes)
    results = []
    email = _contact_email()
    try:
        for index, chunk in enumerate(chunks):
            params = {"q": chunk, "langpair": f"{src}|{dst}"}
            if email:
                params["de"] = email
            response = (session or requests).get(
                URL, params=params,
                timeout=limited_timeout, headers={"User-Agent": user_agent})
            response.raise_for_status()
            data = response.json()
            if int(data.get("responseStatus", 0)) != 200:
                raise ValueError(f"MyMemory status {data.get('responseStatus')}")
            translated = html.unescape(str((data.get("responseData") or {}).get("translatedText") or ""))
            if not translated.strip():
                raise ValueError(f"Empty translation in segment {index + 1}/{len(chunks)}")
            # MyMemory sometimes reports HTTP/responseStatus 200 while returning
            # the untranslated input. Do not report this as a translation.
            if translated.strip() == chunk.strip():
                raise ValueError(f"Unchanged translation in segment {index + 1}/{len(chunks)} (responseStatus=200)")
            results.append(translated)
            if index + 1 < len(chunks):
                time.sleep(delay_seconds)
        return TranslationResult(True, "".join(results).strip(), chunks=len(chunks))
    except (requests.RequestException, ValueError, TypeError, KeyError) as exc:
        return TranslationResult(False, text, error=str(exc).replace(email, "[redacted]") if email else str(exc))
