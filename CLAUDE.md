# CLAUDE.md

This file provides guidance to Claude Code (claude.ai/code) when working with code in this repository.

## Running the Applications

**Streamlit filter dashboard:**
```bash
streamlit run realtor/app.py
```

**CLI listing parser:**
```bash
python realtor/main.py <file.txt>
python realtor/main.py <file.txt> --keywords "ממ״ד,מרפסת" --rooms 4 --max-price 1700000 --location "קריית מוצקין"
```

**Webhook server (receives live WhatsApp messages via Meta Cloud API):**
```bash
python realtor/webhook.py
# Then expose publicly: ngrok http 5000
```

**Green API poller (reads history / polls a WhatsApp group):**
```bash
python realtor/greenapi.py            # show last 20 messages
python realtor/greenapi.py poll       # live-poll and index incoming messages
python realtor/greenapi.py settings   # print instance settings
python realtor/greenapi.py enable     # enable incoming webhooks on the instance
```

## Architecture

The project is a Hebrew real estate listing tool built around two data paths and one shared Elasticsearch index (`realestate_listings`).

### Data ingestion paths

**Path 1 — File-based (offline):** `main.py` parses raw WhatsApp export `.txt` files into `Listing` objects using regex (rooms, price, location, phone). `elastic.py:ingest_listings()` bulk-indexes them.

**Path 2 — Live WhatsApp:** Two independent integrations both write to the same ES index:
- `webhook.py` — Flask server registered as a Meta Cloud API webhook. Receives messages via POST, extracts fields inline (duplicates `Listing` regex logic), and writes directly to ES.
- `greenapi.py` — Polls the Green API notification queue for a named group ("Nadlan"), reuses `Listing` from `main.py` for field extraction, and indexes each message.

### Credentials / configuration

All credentials live in `realtor/data/data.txt` (line-keyed format, e.g. `Green API apiTokenInstance <value>`). `elastic.py` and `greenapi.py` parse this file as fallback when env vars are absent. Elasticsearch connection resolution order: env vars (`ES_URL`, `ES_USER`, `ES_PASSWORD`, `ES_API_KEY`) → `data.txt` → `localhost:9200`.

The WhatsApp token for `whatsapp.py` (Meta Cloud API send) is read directly from `data.txt` as the entire file content — the file must contain only the token for outbound sending, but currently holds all credentials; `whatsapp.py:_token()` reads the whole file as the token string, which is a latent bug if the file has more than one line.

### Streamlit dashboard (`app.py`)

Calls `elastic.search_listings()` on every interaction. Sidebar filters (keywords, location, price range) are applied server-side in ES; rooms filter is applied client-side from the result set. Each result card has a "send to WhatsApp" button that calls `whatsapp.send_text()` via the Meta Cloud API.

### Known duplication

Field extraction regexes (`rooms`, `price`, `location`, `phone`) are defined in `main.py` (`Listing` class) and duplicated inline in `webhook.py`. `greenapi.py` reuses the `Listing` class correctly. If extraction logic changes, `webhook.py` must be updated separately.
