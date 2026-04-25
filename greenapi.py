"""
Green API helper — list chats and read history from a WhatsApp group.
Credentials are parsed from realtor/data/data.txt.
"""

import re
import sys
import requests
from datetime import datetime, timezone
from functools import lru_cache
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent))
from elastic import get_client, ensure_index, INDEX
from main import Listing

_DATA_FILE = Path(__file__).parent / "data" / "data.txt"


@lru_cache(maxsize=1)
def _credentials() -> dict:
    import os
    api_url   = os.getenv("GREEN_API_URL")
    instance  = os.getenv("GREEN_API_INSTANCE")
    token     = os.getenv("GREEN_API_TOKEN")
    if api_url and instance and token:
        return {"apiUrl": api_url, "idInstance": instance, "apiTokenInstance": token}
    text = _DATA_FILE.read_text(encoding="utf-8")
    out = {}
    for key in ("apiUrl", "idInstance", "apiTokenInstance"):
        m = re.search(rf"Green API {key}\s+(\S+)", text)
        if not m:
            raise RuntimeError(f"Green API {key} not found in {_DATA_FILE}")
        out[key] = m.group(1)
    return out


def _base_url() -> str:
    c = _credentials()
    return f"{c['apiUrl']}/waInstance{c['idInstance']}"


def _token() -> str:
    return _credentials()["apiTokenInstance"]


def send_text(to: str, message: str) -> dict:
    """Send a plain text WhatsApp message via Green API.
    `to` is E.164 without +, e.g. '972546546855'. Converted to chatId internally.
    """
    digits = "".join(c for c in to if c.isdigit())
    if digits.startswith("0"):
        digits = "972" + digits[1:]
    chat_id = f"{digits}@c.us"
    r = requests.post(
        f"{_base_url()}/sendMessage/{_token()}",
        json={"chatId": chat_id, "message": message},
        timeout=30,
    )
    if not r.ok:
        try:
            detail = r.json()
        except Exception:
            detail = r.text
        raise RuntimeError(f"HTTP {r.status_code}: {detail}")
    return r.json()


def get_state() -> dict:
    r = requests.get(f"{_base_url()}/getStateInstance/{_token()}", timeout=30)
    r.raise_for_status()
    return r.json()


def get_chats() -> list:
    r = requests.get(f"{_base_url()}/getChats/{_token()}", timeout=30)
    r.raise_for_status()
    return r.json()


def get_chat_history(chat_id: str, count: int = 50) -> list:
    r = requests.post(
        f"{_base_url()}/getChatHistory/{_token()}",
        json={"chatId": chat_id, "count": count},
        timeout=60,
    )
    r.raise_for_status()
    return r.json()


def get_group_data(group_id: str) -> dict:
    r = requests.post(
        f"{_base_url()}/getGroupData/{_token()}",
        json={"groupId": group_id},
        timeout=30,
    )
    r.raise_for_status()
    return r.json()


def find_group(name: str) -> str | None:
    """Scan all group chats and return the first whose subject contains `name` (case-insensitive)."""
    target = name.lower()
    for c in get_chats():
        cid = c.get("id", "")
        if not cid.endswith("@g.us"):
            continue
        cname = (c.get("name") or "").lower()
        if target in cname:
            return cid
        # Fallback: name may be empty in getChats; query group data.
        try:
            gd = get_group_data(cid)
            if target in (gd.get("subject") or "").lower():
                return cid
        except requests.HTTPError:
            continue
    return None


def _extract_text(msg: dict) -> str:
    t = msg.get("typeMessage", "")
    if t == "textMessage":
        return msg.get("textMessage", "")
    if t == "extendedTextMessage":
        return msg.get("extendedTextMessage", {}).get("text", "")
    return f"(non-text: {t})"


def get_settings() -> dict:
    r = requests.get(f"{_base_url()}/getSettings/{_token()}", timeout=30)
    r.raise_for_status()
    return r.json()


def set_settings(settings: dict) -> dict:
    r = requests.post(f"{_base_url()}/setSettings/{_token()}", json=settings, timeout=30)
    r.raise_for_status()
    return r.json()


def receive_notification() -> dict | None:
    r = requests.get(f"{_base_url()}/receiveNotification/{_token()}", timeout=60)
    r.raise_for_status()
    return r.json() if r.text else None


def delete_notification(receipt_id: int) -> None:
    r = requests.delete(f"{_base_url()}/deleteNotification/{_token()}/{receipt_id}", timeout=30)
    r.raise_for_status()


def _save_message_to_es(es, text: str, sender: str, ts_unix: int, group_id: str) -> None:
    listing = Listing(text)
    ingested_at = (
        datetime.fromtimestamp(ts_unix, tz=timezone.utc).isoformat()
        if ts_unix else datetime.now(timezone.utc).isoformat()
    )
    es.index(index=INDEX, document={
        "raw":         listing.raw,
        "rooms":       listing.rooms,
        "price":       listing.price,
        "location":    listing.location,
        "phone":       listing.phone,
        "sender":      sender,
        "source_file": f"whatsapp_green:{group_id}",
        "ingested_at": ingested_at,
    })


def poll_group(group_id: str) -> None:
    """Drain the notification queue; print and index text messages from `group_id`."""
    es = get_client()
    ensure_index(es)
    print(f"Polling for messages in {group_id} (Ctrl+C to stop)...")
    while True:
        note = receive_notification()
        if note is None:
            continue
        receipt = note["receiptId"]
        body = note.get("body", {})
        try:
            if body.get("typeWebhook") != "incomingMessageReceived":
                continue
            sender = body.get("senderData", {})
            if sender.get("chatId") != group_id:
                continue

            md = body.get("messageData", {})
            t = md.get("typeMessage", "")
            if t == "textMessage":
                text = md.get("textMessageData", {}).get("textMessage", "")
            elif t == "extendedTextMessage":
                text = md.get("extendedTextMessageData", {}).get("text", "")
            else:
                continue  # skip non-text

            who = sender.get("senderName") or sender.get("sender") or ""
            ts  = int(body.get("timestamp") or 0)
            print(f"[{ts}] {who}: {text[:200]}")
            try:
                _save_message_to_es(es, text, who, ts, group_id)
            except Exception as e:
                print(f"  ES save failed: {e}")
        finally:
            delete_notification(receipt)


if __name__ == "__main__":
    import sys

    state = get_state()
    print(f"Instance state: {state}")
    if state.get("stateInstance") != "authorized":
        print("Instance is not authorized — scan the QR in Green API console first.")
        raise SystemExit(1)

    group_id = find_group("Nadlan")
    if not group_id:
        print("Group 'Nadlan' not found. All groups visible to this instance:")
        for c in get_chats():
            if c.get("id", "").endswith("@g.us"):
                subj = c.get("name") or ""
                if not subj:
                    try:
                        subj = get_group_data(c["id"]).get("subject", "")
                    except Exception:
                        subj = "?"
                print(f"  {c['id']}  {subj}")
        raise SystemExit(1)

    print(f"Found group: {group_id}")

    mode = sys.argv[1] if len(sys.argv) > 1 else "history"

    if mode == "settings":
        import json
        print(json.dumps(get_settings(), indent=2, ensure_ascii=False))
    elif mode == "raw":
        import json
        print("Draining notification queue — print everything, delete as we go. Ctrl+C to stop.")
        while True:
            note = receive_notification()
            if note is None:
                continue
            print(json.dumps(note, indent=2, ensure_ascii=False))
            delete_notification(note["receiptId"])
    elif mode == "enable":
        print(set_settings({
            "incomingWebhook": "yes",
            "outgoingMessageWebhook": "yes",
            "outgoingAPIMessageWebhook": "yes",
        }))
    elif mode == "poll":
        poll_group(group_id)
    else:
        msgs = get_chat_history(group_id, count=20)
        for m in msgs:
            sender = m.get("senderName") or m.get("senderId") or m.get("chatId")
            ts = m.get("timestamp", "")
            print(f"[{ts}] {sender}: {_extract_text(m)[:160]}")
