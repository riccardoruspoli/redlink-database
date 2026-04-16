import re
import unicodedata

from unidecode import unidecode

SEARCH_NGRAM_SIZE = 3


def normalize_nfc(value: str) -> str:
    """Normalize text to NFC for stable display and JSON output."""

    return unicodedata.normalize("NFC", value)


def _normalize_search_text(value: str) -> str:
    decomposed = unicodedata.normalize("NFKD", value or "")
    no_diacritics = "".join(
        character for character in decomposed if unicodedata.category(character) != "Mn"
    )
    normalized = no_diacritics.casefold().replace("_", " ")
    normalized = re.sub(r"\s+", " ", normalized).strip()
    return normalized


def get_search_ngrams(value: str) -> set[str]:
    """Return normalized search n-grams for one title or redlink string."""

    normalized = _normalize_search_text(value)
    if len(normalized) < SEARCH_NGRAM_SIZE:
        return set()
    return {
        normalized[index : index + SEARCH_NGRAM_SIZE]
        for index in range(len(normalized) - SEARCH_NGRAM_SIZE + 1)
    }


def get_search_ngram_bucket_key(ngram: str) -> str:
    """Map one n-gram to the static bucket prefix used on disk."""

    if not ngram:
        return "__"

    bucket_characters = []
    for index in range(2):
        if index >= len(ngram):
            bucket_characters.append("_")
            continue
        character = ngram[index]
        if character.isascii() and character.isalnum():
            bucket_characters.append(character.lower())
        else:
            bucket_characters.append("_")
    return "".join(bucket_characters)


def get_initial(title: str) -> str:
    """Collapse a page title to the grouped-page initial used by the site."""

    if not title:
        return "other"
    char = title[0]

    if char.isdigit():
        return "number"
    if not char.isalpha():
        return "other"

    try:
        name = unicodedata.name(char)
    except ValueError:
        return "other"
    if "LATIN" not in name:
        return "other"

    decomposed = unicodedata.normalize("NFKD", char)
    no_diacritics = "".join(
        value for value in decomposed if unicodedata.category(value) != "Mn"
    )
    folded = no_diacritics.casefold()
    for value in folded:
        if value.isalpha() and value.isascii():
            return value

    transliterated = unidecode(char)
    if transliterated and transliterated[0].isalpha() and transliterated[0].isascii():
        return transliterated[0].lower()
    return "other"
