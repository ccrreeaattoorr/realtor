"""
Real estate WhatsApp group message parser for Hebrew listings.
Parses raw listing text files and filters by keywords, rooms, price, location.

Usage:
    python main.py msg.txt
    python main.py msg.txt --keywords "ממ״ד,מרפסת"
    python main.py msg.txt --rooms 4
    python main.py msg.txt --max-price 1700000
    python main.py msg.txt --location "קריית מוצקין"
    python main.py msg.txt --rooms 4 --max-price 1800000 --keywords "חניה"
"""

import re
import argparse
import sys
from pathlib import Path

sys.stdout.reconfigure(encoding='utf-8')

_CITY_FILE = Path(__file__).parent / "data" / "cities_israel.txt"


def _load_city_map() -> dict[str, str]:
    """
    Parse cities_israel.txt into {variant: canonical_name}.
    Handles both plain lines ("נתניה") and lines with an abbreviation
    ("קריית מוצקין - ק. מוצקין").  Adds "תל אביב" as an alias for
    "תל אביב-יפו" automatically.
    """
    city_map: dict[str, str] = {}
    if not _CITY_FILE.exists():
        return city_map
    for line in _CITY_FILE.read_text(encoding="utf-8").splitlines():
        line = line.strip()
        if not line:
            continue
        if " - " in line:
            canonical, abbrev = line.split(" - ", 1)
            canonical = canonical.strip()
            abbrev = abbrev.strip()
            city_map[canonical] = canonical
            city_map[abbrev] = canonical
        else:
            city_map[line] = line
    if "תל אביב-יפו" in city_map:
        city_map.setdefault("תל אביב", "תל אביב-יפו")
    return city_map


_CITY_MAP: dict[str, str] = _load_city_map()


def _build_cities_re(city_map: dict[str, str]) -> re.Pattern:
    """
    Build a regex matching any city variant.
    Sorts longest-first so multi-word names win over their prefixes.
    Optional leading ב handles the Hebrew preposition "in" attached directly
    to the city name (e.g. "בנתניה", "בק. מוצקין").
    """
    variants = sorted(city_map.keys(), key=len, reverse=True)
    escaped = [re.escape(v) for v in variants]
    # (?<![א-ת]) — not mid-word; ב? — optional attached preposition;
    # (?![א-ת])  — not followed by Hebrew letter (prevents partial matches)
    return re.compile(r"(?<![א-ת])ב?(" + "|".join(escaped) + r")(?![א-ת])")


_CITIES: re.Pattern = _build_cities_re(_CITY_MAP)


class Listing:
    def __init__(self, raw: str):
        self.raw = raw.strip()
        self.rooms = self._extract_rooms()
        self.price = self._extract_price()
        self.location = self._extract_location()
        self.phone = self._extract_phone()

    def _extract_rooms(self) -> int | None:
        m = re.search(r'(\d+(?:\.\d+)?)\s*חדרים', self.raw)
        if m:
            return int(float(m.group(1)))
        return None

    def _extract_price(self) -> int | None:
        # Matches: 1,750,000 ₪ or 1.550 (millions shorthand) or 1,750,000
        m = re.search(r'מחיר[^:\n]*[:\s]+([\d,.]+)\s*(₪|♠️)?', self.raw)
        if not m:
            return None
        raw_price = m.group(1).replace(',', '')
        if raw_price.count('.') > 1:
            parts = raw_price.split('.')
            if len(parts[-1]) < 3:
                parts[-1] = parts[-1].ljust(3, '0')
            raw_price = ''.join(parts)
        try:
            price = float(raw_price)
        except ValueError:
            return None
        if price < 100:           # decimal shorthand: 1.8 → 1,800,000
            price = int(price * 1_000_000)
        elif price < 100_000:     # thousands shorthand: 1800 → 1,800,000
            price = int(price * 1_000)
        price = int(price)
        if not (200_000 <= price <= 50_000_000):
            return None
        return price

    def _extract_location(self) -> str:
        _STOP = re.compile(r'[,\-–|\n(]')

        def clean(s: str) -> str:
            m = _STOP.search(s)
            return s[:m.start()].strip() if m else s.strip()

        def normalize(s: str) -> str:
            return _CITY_MAP.get(s.strip(), s.strip())

        # Ordered from most-specific to least-specific
        patterns = [
            r'כתובת[:\s]+([^\n,\-–|]+)',
            r'ממוקמת?\s+ב([^\n,\-–|]+)',
            r'בעיר\s+([^\n,\-–|]+)',
            r'רחוב\s+([^\n,\-–|]+)',
            r'רח[׳\']\s+([^\n,\-–|]+)',
            r'בשכונת\s+([^\n,\-–|]+)',
            r'שכונת\s+([^\n,\-–|]+)',
            r'שכונה[:\s]+([^\n,\-–|]+)',
        ]
        for pat in patterns:
            m = re.search(pat, self.raw)
            if m:
                candidate = clean(m.group(1))
                city_m = _CITIES.search(candidate)
                if city_m:
                    return normalize(city_m.group(1))
                return candidate

        # Fallback: scan for any known city or abbreviation anywhere in the text
        m = _CITIES.search(self.raw)
        if m:
            return normalize(m.group(1))

        return ''

    def _extract_phone(self) -> str:
        m = re.search(r'(0\d[\d\-]{7,10})', self.raw)
        return m.group(1) if m else ''

    def matches(
        self,
        keywords: list[str] | None,
        rooms: int | None,
        min_price: int | None,
        max_price: int | None,
        location: str | None,
    ) -> bool:
        if keywords:
            if not any(kw.lower() in self.raw.lower() for kw in keywords):
                return False
        if rooms is not None:
            if self.rooms != rooms:
                return False
        if min_price is not None:
            if self.price is None or self.price < min_price:
                return False
        if max_price is not None:
            if self.price is None or self.price > max_price:
                return False
        if location:
            if location.lower() not in self.raw.lower():
                return False
        return True

    def summary(self) -> str:
        price_str = f"{self.price:,} ₪" if self.price else "לא צוין"
        rooms_str = str(self.rooms) if self.rooms else "לא צוין"
        loc_str = self.location or "לא צוין"
        phone_str = self.phone or "לא צוין"
        return (
            f"  חדרים : {rooms_str}\n"
            f"  מחיר  : {price_str}\n"
            f"  מיקום : {loc_str}\n"
            f"  טלפון : {phone_str}"
        )

    def __str__(self):
        return self.raw


_WA_TIMESTAMP = re.compile(
    r'(?m)^(?:\[\d{1,2}/\d{1,2}/\d{2,4},\s*\d{1,2}:\d{2}(?::\d{2})?\]'
    r'|\d{1,2}/\d{1,2}/\d{2,4},\s*\d{1,2}:\d{2}\s*-)'
)
_HAS_PRICE    = re.compile(r'(?:מחיר|₪|\d[\d,]{4,})')
_HAS_ROOMS    = re.compile(r'\d+(?:\.\d+)?\s*חדרים')
_HAS_PHONE    = re.compile(r'0\d[\d\-]{7,10}')
_HAS_KEYWORDS = re.compile(r'למכירה|להשכרה|משכירים|מוכרים|דירה|קוטג|פנטהאוז|דופלקס|גג|נכס|חדר\b')


def _is_listing_chunk(chunk: str) -> bool:
    return bool(_HAS_PRICE.search(chunk) or _HAS_ROOMS.search(chunk) or _HAS_PHONE.search(chunk))


def _is_listing_message(message: str) -> bool:
    """A relevant listing message must have a property keyword + at least one data signal."""
    return bool(_HAS_KEYWORDS.search(message)) and _is_listing_chunk(message)


def _split_message(message: str) -> list[str]:
    """Sub-split a single WhatsApp message into individual listings.

    Split on blank lines; chunks that look like listings are kept separate,
    non-listing chunks (footers, headers) are merged into the previous listing.
    """
    chunks = [c.strip() for c in re.split(r'\n{2,}', message) if c.strip()]
    if len(chunks) <= 1:
        return [message.strip()] if message.strip() else []

    merged: list[str] = []
    for chunk in chunks:
        if _is_listing_chunk(chunk):
            merged.append(chunk)
        elif merged:
            merged[-1] += "\n" + chunk  # attach footer/header to previous listing
        else:
            merged.append(chunk)        # leading non-listing text kept as-is
    return merged


def split_listings(text: str) -> list[str]:
    """Split WhatsApp export into individual listings.

    Phase 1: split on WhatsApp timestamps to get per-message blocks.
    Phase 2: sub-split each message on blank lines, merging non-listing
             chunks (footers, captions) into adjacent listings.
    """
    boundaries = [m.start() for m in _WA_TIMESTAMP.finditer(text)]

    if not boundaries:
        # No timestamps found — fall back to blank-line splitting
        chunks = [c.strip() for c in re.split(r'\n{2,}', text) if c.strip()]
        return [c for c in chunks if _is_listing_chunk(c)]

    messages = []
    for i, start in enumerate(boundaries):
        end = boundaries[i + 1] if i + 1 < len(boundaries) else len(text)
        messages.append(text[start:end].strip())

    listings = []
    for msg in messages:
        if _is_listing_message(msg):
            listings.extend(_split_message(msg))

    return listings


def parse_file(filepath: str) -> list[Listing]:
    with open(filepath, encoding='utf-8') as f:
        text = f.read()
    return [Listing(raw) for raw in split_listings(text)]


def main():
    parser = argparse.ArgumentParser(description='Parse Hebrew real estate WhatsApp listings')
    parser.add_argument('file', help='Path to the messages text file')
    parser.add_argument('--keywords', '-k', help='Comma-separated keywords to filter by')
    parser.add_argument('--rooms', '-r', type=int, help='Filter by exact number of rooms')
    parser.add_argument('--min-price', type=int, help='Minimum price in ₪')
    parser.add_argument('--max-price', type=int, help='Maximum price in ₪')
    parser.add_argument('--location', '-l', help='Filter by city or neighborhood (partial match)')
    parser.add_argument('--full', action='store_true', help='Print full listing text (default: summary only)')
    args = parser.parse_args()

    listings = parse_file(args.file)
    keywords = [k.strip() for k in args.keywords.split(',')] if args.keywords else None

    results = [
        l for l in listings
        if l.matches(keywords, args.rooms, args.min_price, args.max_price, args.location)
    ]

    print(f"נמצאו {len(results)} מתוך {len(listings)} מודעות\n" + "=" * 60)
    for i, listing in enumerate(results, 1):
        print(f"\n--- מודעה {i} ---")
        print(listing.summary())
        if args.full:
            print("\n" + listing.raw)
        print()


if __name__ == '__main__':
    main()
