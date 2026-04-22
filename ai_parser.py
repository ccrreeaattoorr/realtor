"""
AI-assisted listing parser using Claude Haiku.
Only applied to listings that appear to contain multiple apartments.
"""

import json
import re
import anthropic

_client = None

def _get_client() -> anthropic.Anthropic:
    global _client
    if _client is None:
        _client = anthropic.Anthropic()
    return _client


_MULTI_PRICE = re.compile(r'מחיר', re.IGNORECASE)
_MULTI_ROOMS = re.compile(r'\d+(?:\.\d+)?\s*חדרים')


def needs_ai(raw: str) -> bool:
    return _MULTI_PRICE.findall(raw).__len__() >= 2 or _MULTI_ROOMS.findall(raw).__len__() >= 2


_SYSTEM = """אתה מנתח מודעות נדל"ן בעברית. קבל טקסט גולמי של הודעת וואטסאפ והחזר JSON בלבד.

אם ההודעה מכילה מספר דירות, פצל אותן. לכל דירה החזר:
{
  "apartments": [
    {
      "rooms": <מספר שלם או null>,
      "price": <מחיר בשקלים כמספר שלם בין 200000 ל-50000000, או null>,
      "location": <עיר/שכונה כמחרוזת או null>,
      "phone": <מספר טלפון ישראלי כמחרוזת או null>,
      "raw": <הטקסט הרלוונטי לדירה זו>
    }
  ]
}

כללים:
- החזר JSON בלבד, ללא טקסט נוסף
- אם יש רק דירה אחת, החזר מערך עם פריט אחד
- מחיר: המר קיצורים (1.8 → 1800000, "מיליון וחצי" → 1500000)
- חדרים: המר שברים (3.5 → 3 או 4 לפי הגיון)
- טלפון: ספרות בלבד עם מקף (052-822-7351)"""


def ai_parse(raw: str) -> list[dict] | None:
    """Parse a listing with Claude Haiku. Returns list of apartment dicts, or None on failure."""
    try:
        resp = _get_client().messages.create(
            model="claude-haiku-4-5-20251001",
            max_tokens=512,
            system=_SYSTEM,
            messages=[{"role": "user", "content": raw}],
        )
        data = json.loads(resp.content[0].text)
        return data.get("apartments")
    except Exception:
        return None


def process_one(listing) -> list:
    """Process a single Listing. Returns a list of Listing objects (split if multi-apartment)."""
    from main import Listing

    if not needs_ai(listing.raw):
        return [listing]

    parsed = ai_parse(listing.raw)
    if not parsed:
        return [listing]

    result = []
    for apt in parsed:
        raw_text = apt.get("raw") or listing.raw
        new = Listing(raw_text)
        if apt.get("rooms") and not new.rooms:
            new.rooms = apt["rooms"]
        if apt.get("price") and not new.price:
            new.price = apt["price"]
        if apt.get("location") and not new.location:
            new.location = apt["location"]
        if apt.get("phone") and not new.phone:
            new.phone = apt["phone"]
        result.append(new)
    return result


def process_listings(listings) -> tuple[list, int]:
    result = []
    ai_count = 0
    for listing in listings:
        expanded = process_one(listing)
        if len(expanded) > 1 or (expanded and expanded[0] is not listing):
            ai_count += 1
        result.extend(expanded)
    return result, ai_count
