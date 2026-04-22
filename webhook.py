"""
WhatsApp webhook server for receiving group messages via Meta Cloud API.

Setup:
1. Run this server:          python realtor/webhook.py
2. Expose it publicly:       ngrok http 5000
3. In Meta console → WhatsApp → Configuration → Webhook:
   - Callback URL:  https://<ngrok-url>/webhook
   - Verify token:  realtor_bot  (or set WEBHOOK_VERIFY_TOKEN env var)
   - Subscribe to:  messages
"""

import os
import json
import logging
from datetime import datetime, timezone
from flask import Flask, request, jsonify
from pathlib import Path
import sys

sys.path.insert(0, str(Path(__file__).parent))
from elastic import get_client, ensure_index, INDEX

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
log = logging.getLogger(__name__)

app = Flask(__name__)

VERIFY_TOKEN = os.getenv("WEBHOOK_VERIFY_TOKEN", "realtor_bot")


# ── Webhook verification (GET) ────────────────────────────────────────────────
@app.get("/webhook")
def verify():
    mode  = request.args.get("hub.mode")
    token = request.args.get("hub.verify_token")
    challenge = request.args.get("hub.challenge")

    if mode == "subscribe" and token == VERIFY_TOKEN:
        log.info("Webhook verified.")
        return challenge, 200
    return "Forbidden", 403


# ── Receive messages (POST) ───────────────────────────────────────────────────
@app.post("/webhook")
def receive():
    data = request.get_json(silent=True) or {}
    log.info("Incoming: %s", json.dumps(data, ensure_ascii=False)[:500])

    try:
        for entry in data.get("entry", []):
            for change in entry.get("changes", []):
                value = change.get("value", {})
                _process_value(value)
    except Exception as e:
        log.exception("Error processing webhook: %s", e)

    # Always return 200 so Meta doesn't retry
    return jsonify({"status": "ok"}), 200


def _process_value(value: dict):
    messages  = value.get("messages", [])
    metadata  = value.get("metadata", {})
    contacts  = {c["wa_id"]: c.get("profile", {}).get("name", c["wa_id"])
                 for c in value.get("contacts", [])}

    for msg in messages:
        if msg.get("type") != "text":
            continue

        text      = msg["text"]["body"]
        sender_id = msg.get("from", "")
        sender    = contacts.get(sender_id, sender_id)
        timestamp = datetime.fromtimestamp(int(msg.get("timestamp", 0)), tz=timezone.utc).isoformat()
        phone_num = metadata.get("display_phone_number", "")

        log.info("Message from %s: %s", sender, text[:100])
        _save_to_es(text, sender, timestamp, phone_num)


def _save_to_es(text: str, sender: str, timestamp: str, phone_number: str):
    try:
        es = get_client()
        ensure_index(es)
        es.index(index=INDEX, document={
            "raw":         text,
            "sender":      sender,
            "rooms":       _extract_rooms(text),
            "price":       _extract_price(text),
            "location":    _extract_location(text),
            "phone":       _extract_contact_phone(text),
            "source_file": f"whatsapp_live:{phone_number}",
            "ingested_at": timestamp,
        })
        log.info("Saved to Elasticsearch.")
    except Exception as e:
        log.error("ES save failed: %s", e)


# ── Field extractors (same logic as main.py Listing class) ───────────────────
import re

def _extract_rooms(text: str):
    m = re.search(r'(\d+(?:\.\d+)?)\s*חדרים', text)
    return int(float(m.group(1))) if m else None

def _extract_price(text: str):
    m = re.search(r'מחיר[^:\n]*[:\s]+([\d,.]+)\s*(₪|♠️)?', text)
    if not m:
        return None
    price = float(m.group(1).replace(',', ''))
    if price < 10000:
        price *= 1_000_000
    return int(price)

def _extract_location(text: str):
    for pat in [r'רח[׳\']\s+([^\n]+)', r'רחוב\s+([^\n]+)',
                r'בשכונת\s+([^\n]+)', r'שכונת\s+([^\n]+)',
                r'ב(קרית[^\s,\n]+|קריית[^\s,\n]+|חיפה|נשר|טירת|עכו|נהריה)']:
        m = re.search(pat, text)
        if m:
            return m.group(1).strip()
    return None

def _extract_contact_phone(text: str):
    m = re.search(r'(0\d[\d\-]{7,10})', text)
    return m.group(1) if m else None


if __name__ == "__main__":
    port = int(os.getenv("PORT", 5000))
    log.info("Starting webhook server on port %d", port)
    app.run(host="0.0.0.0", port=port, debug=False)
