"""
WhatsApp messaging via Meta Cloud API.
Token is read from realtor/data/data.txt.
"""

import re
import requests
from pathlib import Path

_DATA_DIR = Path(__file__).parent / "data"
_TOKEN_FILE = _DATA_DIR / "data.txt"
_PHONE_NUMBER_ID = "1114358961760681"
_API_URL = f"https://graph.facebook.com/v20.0/{_PHONE_NUMBER_ID}/messages"


def _token() -> str:
    text = _TOKEN_FILE.read_text(encoding="utf-8")
    m = re.search(r"WhatsApp token\s+(\S+)", text)
    if not m:
        raise ValueError("WhatsApp token not found in data.txt")
    return m.group(1)


def send_text(to: str, message: str) -> dict:
    """Send a plain text WhatsApp message. `to` is E.164 without +, e.g. '972546546855'."""
    resp = requests.post(
        _API_URL,
        headers={"Authorization": f"Bearer {_token()}", "Content-Type": "application/json"},
        json={
            "messaging_product": "whatsapp",
            "to": to,
            "type": "text",
            "text": {"body": message},
        },
    )
    if not resp.ok:
        try:
            detail = resp.json()
        except Exception:
            detail = resp.text
        raise RuntimeError(f"HTTP {resp.status_code}: {detail}")
    return resp.json()


def send_listing(to: str, listing) -> dict:
    """Format a Listing object as a WhatsApp message and send it."""
    price_str = f"{listing.price:,} ₪" if listing.price else "לא צוין"
    rooms_str = f"{listing.rooms} חדרים" if listing.rooms else "לא צוין"
    loc_str   = listing.location or "לא צוין"
    phone_str = listing.phone or "לא צוין"

    text = (
        f"🏡 *{rooms_str}* | {price_str}\n"
        f"📍 {loc_str}\n"
        f"📞 {phone_str}\n\n"
        f"{listing.raw}"
    )
    return send_text(to, text)
