"""
Streamlit dashboard for filtering Hebrew real estate WhatsApp listings.
Listings are stored in and queried from Elasticsearch.

Run: streamlit run realtor/app.py
"""

import hashlib
import io
import json
import random
import string
import uuid
from datetime import datetime, timezone, timedelta
from pathlib import Path

import streamlit as st
import streamlit.components.v1 as components
import elastic
import whatsapp
import ai_parser
from main import Listing, split_listings

PAGE_SIZE = 10
STATS_FILE = Path(__file__).parent / "data" / "visits.json"


# ── Visitor tracking ──────────────────────────────────────────────────────────

def _load_visits() -> list[dict]:
    if STATS_FILE.exists():
        try:
            return json.loads(STATS_FILE.read_text(encoding="utf-8"))
        except Exception:
            pass
    return []


def _save_visits(visits: list[dict]):
    STATS_FILE.write_text(json.dumps(visits, ensure_ascii=False), encoding="utf-8")


def _record_visit():
    if "visitor_id" not in st.session_state:
        st.session_state.visitor_id = str(uuid.uuid4())
        st.session_state.visit_recorded = False
    if not st.session_state.get("visit_recorded"):
        visits = _load_visits()
        visits.append({"id": st.session_state.visitor_id, "ts": datetime.now(timezone.utc).isoformat()})
        _save_visits(visits)
        st.session_state.visit_recorded = True


def _visitor_stats() -> dict:
    visits = _load_visits()
    now = datetime.now(timezone.utc)
    today = now.date()
    week_ago = now - timedelta(days=7)
    total = len(visits)
    unique = len({v["id"] for v in visits})
    today_count = sum(1 for v in visits if datetime.fromisoformat(v["ts"]).date() == today)
    week_count  = sum(1 for v in visits if datetime.fromisoformat(v["ts"]) >= week_ago)
    # visits per day (last 30 days)
    cutoff = now - timedelta(days=30)
    by_day: dict[str, int] = {}
    for v in visits:
        ts = datetime.fromisoformat(v["ts"])
        if ts >= cutoff:
            day = ts.strftime("%Y-%m-%d")
            by_day[day] = by_day.get(day, 0) + 1
    return {"total": total, "unique": unique, "today": today_count, "week": week_count, "by_day": by_day}


# ── Shared styles ─────────────────────────────────────────────────────────────

def _inject_styles():
    st.markdown("""
<style>
    .listing-card {
        background: #1e1e2e;
        color: #e8e8ef;
        border: 1px solid #3a3a4d;
        border-radius: 10px;
        padding: 16px 20px;
        margin-bottom: 16px;
        white-space: pre-wrap;
        font-family: monospace;
        direction: rtl;
        text-align: right;
    }
    .listing-meta {
        background: #2a2a3e;
        color: #f0f0f5;
        border: 1px solid #3a3a4d;
        border-radius: 8px;
        padding: 10px 14px;
        margin-bottom: 10px;
        font-size: 15px;
        direction: rtl;
        text-align: right;
    }
    .listing-meta strong { color: #ffd479; }
    div[data-testid="stHorizontalBlock"] > div[data-testid="stColumn"] > div[data-testid="stVerticalBlockBorderWrapper"] button {
        background: #2a2a3e;
        color: #e8e8ef;
        border: 1px solid #3a3a4d;
        border-radius: 6px;
    }
    div[data-testid="stHorizontalBlock"] > div[data-testid="stColumn"] > div[data-testid="stVerticalBlockBorderWrapper"] button:hover {
        border-color: #ffd479;
        color: #ffd479;
    }
</style>
""", unsafe_allow_html=True)


def _fmt_phone(raw: str) -> str:
    digits = "".join(c for c in (raw or "") if c.isdigit())
    if len(digits) == 10:
        return f"{digits[:3]}-{digits[3:6]}-{digits[6:]}"
    return raw


# ── Pages ─────────────────────────────────────────────────────────────────────

def page_main(es_ok: bool, is_admin: bool = False):
    st.title("🏡 סינון מודעות נדל\"ן")

    if is_admin:
        if es_ok:
            total_docs = elastic.count_total()
            st.success(f"✅ Elasticsearch מחובר — {total_docs:,} מודעות באינדקס ({elastic.INDEX})")
        else:
            st.error("❌ Elasticsearch לא זמין — ודא שהשרת פועל על localhost:9200")

    with st.sidebar:
        st.header("⚙️ סינון")

        keywords_input = st.text_input("🔍 מילות מפתח (מופרדות בפסיק)", placeholder="ממ״ד, מרפסת, חניה")
        location_filter = st.text_input("📍 עיר / שכונה", placeholder="קריית מוצקין")
        phone_filter = st.text_input("📞 מספר טלפון", placeholder="052-822-7351")

        st.markdown("**💰 טווח מחיר (₪)**")
        pc1, pc2 = st.columns(2)
        with pc1:
            min_price = st.number_input("מינימום", min_value=0, value=0, step=100_000, format="%d")
        with pc2:
            max_price = st.number_input("מקסימום", min_value=0, value=0, step=100_000, format="%d",
                                        help="0 = ללא הגבלה")

        keywords = [k.strip() for k in keywords_input.split(",") if k.strip()] if keywords_input else None
        min_p    = min_price if min_price > 0 else None
        max_p    = max_price if max_price > 0 else None
        location = location_filter.strip() if location_filter.strip() else None
        phone    = phone_filter.strip() if phone_filter.strip() else None

        if es_ok:
            base_hits = elastic.search_listings(
                keywords=keywords, min_price=min_p, max_price=max_p, location=location,
            )
        else:
            base_hits = []

        available_rooms = sorted({h["rooms"] for h in base_hits if h.get("rooms")})
        rooms_options   = ["הכל"] + available_rooms

        if st.session_state.get("rooms_filter") not in rooms_options:
            st.session_state.rooms_filter = "הכל"

        rooms_filter = st.selectbox("🛏️ מספר חדרים", rooms_options, key="rooms_filter")

        st.markdown("**↕️ מיון**")
        sort_options = {
            "שלמות נתונים": "completeness",
            "מחיר ↑":       "price_asc",
            "מחיר ↓":       "price_desc",
            "חדרים ↑":      "rooms_asc",
            "חדרים ↓":      "rooms_desc",
            "תאריך הוספה ↓": "date_desc",
            "תאריך הוספה ↑": "date_asc",
        }
        sort_label = st.selectbox("מיון לפי", list(sort_options.keys()), label_visibility="collapsed")
        sort_by = sort_options[sort_label]

        st.divider()
        st.markdown("**📲 שליחה לווטסאפ**")
        _raw_phone = st.session_state.get("username", "")
        if _raw_phone.startswith("0") and _raw_phone.isdigit():
            _default_wa = "972" + _raw_phone[1:]
        else:
            _default_wa = ""
        wa_number = st.text_input("מספר טלפון", value=_default_wa, help="ללא + , לדוגמה: 972546546855")

    if not es_ok:
        st.info("הפעל את Elasticsearch כדי לחפש.")
        return

    hits = [
        h for h in base_hits
        if (rooms_filter == "הכל" or h.get("rooms") == rooms_filter)
        and (not phone or "".join(c for c in phone if c.isdigit()) in "".join(c for c in (h.get("phone") or "") if c.isdigit()))
    ]
    _BIG = 10**9
    sort_key = {
        "completeness": lambda h: -(bool(h.get("phone")) + bool(h.get("rooms")) + bool(h.get("price")) + bool(h.get("location"))),
        "price_asc":    lambda h:  (h.get("price") or _BIG),
        "price_desc":   lambda h: -(h.get("price") or 0),
        "rooms_asc":    lambda h:  (h.get("rooms") or _BIG),
        "rooms_desc":   lambda h: -(h.get("rooms") or 0),
        "date_desc":    lambda h: -(h.get("ingested_at") or ""),
        "date_asc":     lambda h:  (h.get("ingested_at") or ""),
    }[sort_by]
    hits.sort(key=sort_key)

    c1, c2, c3 = st.columns(3)
    c1.metric("תוצאות", len(hits))
    prices = [h["price"] for h in hits if h.get("price")]
    if prices:
        c2.metric("מחיר מינימלי", f"{min(prices):,} ₪")
        c3.metric("מחיר מקסימלי", f"{max(prices):,} ₪")

    st.divider()

    if not hits:
        st.warning("לא נמצאו מודעות התואמות את הסינון.")
        return

    total_pages = max(1, (len(hits) + PAGE_SIZE - 1) // PAGE_SIZE)

    if "page" not in st.session_state or st.session_state.get("_total_pages") != total_pages:
        st.session_state.page = 1
    st.session_state._total_pages = total_pages

    page = st.session_state.page

    if total_pages > 1:
        cols = st.columns(total_pages)
        for i, col in enumerate(cols, 1):
            with col:
                label = f"**{i}**" if i == page else str(i)
                if st.button(label, key=f"pg_{i}", use_container_width=True):
                    st.session_state.page = i
                    st.rerun()
        page = st.session_state.page

    page_hits = hits[(page - 1) * PAGE_SIZE : page * PAGE_SIZE]

    for i, doc in enumerate(page_hits, (page - 1) * PAGE_SIZE + 1):
        price_str = f"{doc['price']:,} ₪" if doc.get("price") else "לא צוין"
        rooms_str = f"{doc['rooms']} חדרים" if doc.get("rooms") else "לא צוין"
        loc_str   = doc.get("location") or "לא צוין"
        phone_str = _fmt_phone(doc.get("phone")) if doc.get("phone") else "לא צוין"
        st.markdown(f"""
<div class="listing-meta">
  <strong>מודעה {i}</strong> &nbsp;|&nbsp;
  🛏️ {rooms_str} &nbsp;|&nbsp;
  💰 {price_str} &nbsp;|&nbsp;
  📍 {loc_str} &nbsp;|&nbsp;
  📞 {phone_str}
</div>
""", unsafe_allow_html=True)

        with st.expander("📄 הצג טקסט מלא"):
            st.markdown(
                f'<div class="listing-card">{doc.get("raw", "")}</div>',
                unsafe_allow_html=True,
            )

        if st.button(f"📲 שלח לווטסאפ", key=f"wa_{i}"):
            try:
                whatsapp.send_text(wa_number, doc.get("raw", ""))
                st.success("נשלח!")
            except Exception as e:
                st.error(f"שגיאה: {e}")

        st.divider()


def page_admin(es_ok: bool):
    st.title("🔧 ניהול וסטטיסטיקות")

    # ── Visitor stats ─────────────────────────────────────────────────────────
    st.subheader("👥 מבקרים")
    vs = _visitor_stats()
    v1, v2, v3, v4 = st.columns(4)
    v1.metric("סה\"כ ביקורים",   vs["total"])
    v2.metric("מבקרים ייחודיים", vs["unique"])
    v3.metric("היום",             vs["today"])
    v4.metric("השבוע",            vs["week"])

    if vs["by_day"]:
        st.markdown("**ביקורים לפי יום (30 ימים אחרונים)**")
        st.bar_chart(vs["by_day"])

    st.divider()

    # ── File upload ───────────────────────────────────────────────────────────
    st.subheader("📂 העלאת קובץ הודעות")
    uploaded = st.file_uploader("בחר קובץ טקסט", type=["txt"], label_visibility="collapsed")
    use_ai = st.toggle("🤖 עיבוד AI למודעות מרובות", value=True, help="מפצל מודעות המכילות 2+ דירות באמצעות Claude Haiku")
    if uploaded:
        if st.button("📥 ייבא מודעות"):
            text = uploaded.read().decode("utf-8", errors="replace")
            listings = [Listing(raw) for raw in split_listings(text)]
            total = len(listings)
            bar = st.progress(0, text=f"מנתח... 0/{total}")

            if use_ai:
                processed, ai_count = [], 0
                flagged_set = {id(l) for l in listings if ai_parser.needs_ai(l.raw)}

                for i, listing in enumerate(listings, 1):
                    pct = int(i / total * 90)
                    if id(listing) in flagged_set:
                        bar.progress(pct, text=f"🤖 AI מעבד מודעה {i}/{total}...")
                        expanded = ai_parser.process_one(listing)
                        if len(expanded) != 1 or expanded[0] is not listing:
                            ai_count += 1
                        processed.extend(expanded)
                    else:
                        bar.progress(pct, text=f"מנתח {i}/{total}...")
                        processed.append(listing)

                listings = processed
                if ai_count:
                    st.info(f"🤖 AI פיצל {ai_count} מודעות מרובות")

            bar.progress(95, text="שומר ל-Elasticsearch...")
            ok, err = elastic.ingest_listings(listings, source_file=uploaded.name)
            bar.progress(100, text="✅ הושלם")
            if err:
                st.warning(f"יובאו {ok} מודעות, {err} שגיאות")
            else:
                st.success(f"יובאו {ok} מודעות בהצלחה")
            st.rerun()

    st.divider()

    # ── Data management ───────────────────────────────────────────────────────
    st.subheader("🗑️ ניהול נתונים")

    if not es_ok:
        st.warning("Elasticsearch לא זמין.")
        return

    col_date, col_btn = st.columns([2, 1])
    with col_date:
        cutoff_date = st.date_input("מחק מודעות שנוספו לפני", value=datetime.now(timezone.utc).date())
        cutoff_iso = datetime.combine(cutoff_date, datetime.min.time()).replace(tzinfo=timezone.utc).isoformat()
        preview = elastic.count_before(cutoff_iso)
        if preview:
            st.warning(f"⚠️ {preview:,} מודעות יימחקו")
        else:
            st.info("אין מודעות לפני תאריך זה")
    with col_btn:
        st.markdown("<br>", unsafe_allow_html=True)
        if st.button("🗑️ מחק", type="primary", disabled=preview == 0):
            deleted = elastic.delete_before(cutoff_iso)
            st.success(f"נמחקו {deleted:,} מודעות")
            st.rerun()

    st.divider()

    # ── Registered users ──────────────────────────────────────────────────────
    st.subheader("👥 משתמשים רשומים")
    users = _load_users()
    if users:
        for phone, u in list(users.items()):
            c1, c2, c3, c4, c5 = st.columns([2, 2, 3, 1, 1])
            c1.write(phone)
            c2.write(u.get("name", ""))
            c3.write(u.get("email", ""))
            c4.write(u.get("role", "user"))
            if c5.button("🗑️", key=f"del_user_{phone}", help="מחק משתמש"):
                del users[phone]
                _save_users(users)
                st.rerun()
    else:
        st.info("אין משתמשים רשומים עדיין.")

    st.divider()

    # ── Listing stats ─────────────────────────────────────────────────────────
    st.subheader("🏠 סטטיסטיקות מודעות")

    total = elastic.count_total()
    stats = elastic.get_stats()

    if not stats:
        st.info("אין נתונים באינדקס.")
        return

    m1, m2, m3, m4 = st.columns(4)
    m1.metric("סה\"כ מודעות", f"{total:,}")
    if stats.get("avg_price"):
        m2.metric("מחיר ממוצע",   f"{int(stats['avg_price']):,} ₪")
    if stats.get("min_price"):
        m3.metric("מחיר מינימלי", f"{int(stats['min_price']):,} ₪")
    if stats.get("max_price"):
        m4.metric("מחיר מקסימלי", f"{int(stats['max_price']):,} ₪")

    st.divider()

    col_l, col_r = st.columns(2)

    with col_l:
        if stats.get("rooms_dist"):
            st.markdown("**🛏️ התפלגות חדרים**")
            rooms_data = {f"{k} חד'": v for k, v in sorted(stats["rooms_dist"].items())}
            st.bar_chart(rooms_data)

    with col_r:
        if stats.get("top_locations"):
            st.markdown("**📍 10 ערים מובילות**")
            st.bar_chart(stats["top_locations"])

    st.divider()

    if stats.get("by_day"):
        st.markdown("**📅 מודעות שנוספו לפי יום**")
        st.bar_chart(stats["by_day"])

    if stats.get("by_source"):
        st.markdown("**📂 מקורות קבצים**")
        rows = [{"קובץ": name, "מודעות": count} for name, count in sorted(stats["by_source"].items(), key=lambda x: -x[1])]
        st.dataframe(rows, use_container_width=True, hide_index=True)


# ── Auth ──────────────────────────────────────────────────────────────────────

_ADMIN_USERS = {
    "admin": {"hash": hashlib.sha256("REDACTED".encode()).hexdigest(), "role": "admin"},
}
_USERS_FILE = Path(__file__).parent / "data" / "users.json"


def _load_users() -> dict:
    if _USERS_FILE.exists():
        try:
            return json.loads(_USERS_FILE.read_text(encoding="utf-8"))
        except Exception:
            pass
    return {}


def _save_users(users: dict):
    _USERS_FILE.write_text(json.dumps(users, ensure_ascii=False, indent=2), encoding="utf-8")


def _normalize_phone(phone: str) -> str:
    return "".join(c for c in phone if c.isdigit())


def _check_login(identifier: str, password: str) -> str | None:
    pw_hash = hashlib.sha256(password.encode()).hexdigest()
    # Admin logs in by username
    u = identifier.strip().lower()
    if u in _ADMIN_USERS and _ADMIN_USERS[u]["hash"] == pw_hash:
        return "admin"
    # Regular users log in by phone number
    phone_key = _normalize_phone(identifier)
    users = _load_users()
    if phone_key in users and users[phone_key]["hash"] == pw_hash:
        return users[phone_key]["role"]
    return None


def _register_user(name: str, phone: str, email: str, password: str) -> str | None:
    """Returns error message or None on success."""
    phone_key = _normalize_phone(phone)
    if not phone_key:
        return "מספר טלפון לא תקין"
    if len(phone_key) < 9:
        return "מספר טלפון לא תקין"
    if len(password) < 4:
        return "הסיסמה חייבת להכיל לפחות 4 תווים"
    users = _load_users()
    if phone_key in users:
        return "מספר טלפון כבר רשום במערכת"
    users[phone_key] = {
        "hash":  hashlib.sha256(password.encode()).hexdigest(),
        "role":  "user",
        "name":  name.strip(),
        "email": email.strip().lower(),
    }
    _save_users(users)
    return None


def _new_captcha() -> str:
    return "".join(random.choices(string.ascii_uppercase + string.digits, k=5))


def _captcha_image(text: str) -> bytes:
    from PIL import Image, ImageDraw, ImageFont, ImageFilter
    import random as _rnd
    img = Image.new("RGB", (200, 60), color=(30, 30, 46))
    draw = ImageDraw.Draw(img)
    try:
        font = ImageFont.truetype("arial.ttf", 36)
    except Exception:
        font = ImageFont.load_default(size=36)
    # Draw noise lines
    for _ in range(5):
        x1, y1 = _rnd.randint(0, 200), _rnd.randint(0, 60)
        x2, y2 = _rnd.randint(0, 200), _rnd.randint(0, 60)
        draw.line([(x1, y1), (x2, y2)], fill=(80, 80, 100), width=1)
    # Draw each character with slight offset
    x = 15
    for ch in text:
        y = _rnd.randint(5, 15)
        draw.text((x, y), ch, font=font, fill=(
            _rnd.randint(180, 255), _rnd.randint(180, 255), _rnd.randint(180, 255)
        ))
        x += 35
    img = img.filter(ImageFilter.SMOOTH)
    buf = io.BytesIO()
    img.save(buf, format="PNG")
    return buf.getvalue()


_CAPTCHA_TTL = 200  # seconds


def _render_captcha(key: str) -> bool:
    """Renders captcha image + input. Returns True if answer is correct."""
    import time
    ts_key = f"captcha_ts_{key}"
    code_key = f"captcha_{key}"

    now = time.time()
    if code_key not in st.session_state or now - st.session_state.get(ts_key, 0) >= _CAPTCHA_TTL:
        st.session_state[code_key] = _new_captcha()
        st.session_state[ts_key] = now

    code = st.session_state[code_key]
    elapsed = int(now - st.session_state[ts_key])
    remaining = _CAPTCHA_TTL - elapsed

    c1, c2 = st.columns([1, 1])
    with c1:
        st.image(_captcha_image(code), width=200)
    with c2:
        st.markdown("<br>", unsafe_allow_html=True)
        if st.button("🔄", key=f"captcha_refresh_{key}", help="קוד חדש"):
            st.session_state[code_key] = _new_captcha()
            st.session_state[ts_key] = time.time()
            st.rerun()
        components.html(f"""
<style>
  body {{margin:0;background:transparent}}
  .timer {{font-family:sans-serif;font-size:13px;color:#aaa;direction:rtl}}
  .bar {{height:4px;background:#3a3a4d;border-radius:2px;margin-top:4px}}
  .fill {{height:4px;background:#ffd479;border-radius:2px;transition:width 1s linear}}
</style>
<div class="timer" id="txt">⏱️ מתחדש בעוד {remaining} שניות</div>
<div class="bar"><div class="fill" id="fill" style="width:{int(remaining/_CAPTCHA_TTL*100)}%"></div></div>
<script>
  var s = {remaining};
  var total = {_CAPTCHA_TTL};
  function tick() {{
    s--;
    document.getElementById('txt').textContent = '⏱️ מתחדש בעוד ' + s + ' שניות';
    document.getElementById('fill').style.width = Math.max(0, s/total*100) + '%';
    if (s > 0) setTimeout(tick, 1000);
    else document.getElementById('txt').textContent = 'הקוד פג תוקף — לחץ על הכפתור לרענון';
  }}
  setTimeout(tick, 1000);
</script>
""", height=35)

    answer = st.text_input("הקלד את הקוד בתמונה", key=f"captcha_input_{key}")
    return answer.strip().upper() == code


def _login_page():
    st.markdown("<style>div.block-container{padding-top:0rem!important;margin-top:-3rem}</style>", unsafe_allow_html=True)
    col = st.columns([1, 2, 1])[1]
    with col:
        logo_path = Path(__file__).parent / "img" / "davinci_real_estate_photo_for_the_site.png"
        if logo_path.exists():
            st.image(str(logo_path), use_container_width=True)

        if st.session_state.pop("registered", False):
            st.success("נרשמת בהצלחה! כעת תוכל להתחבר")

        tab_login, tab_register = st.tabs(["כניסה", "הרשמה"])

        with tab_login:
            st.markdown("### 🏡 כניסה למערכת")
            identifier = st.text_input("טלפון", placeholder="050-000-0000", key="login_user")
            password   = st.text_input("סיסמה", type="password", key="login_pass")
            captcha_ok = _render_captcha("login")
            if st.button("כניסה", use_container_width=True, type="primary", key="btn_login"):
                if not captcha_ok:
                    st.error("קוד האימות שגוי")
                else:
                    role = _check_login(identifier, password)
                    if role:
                        st.session_state.logged_in = True
                        st.session_state.username = _normalize_phone(identifier) or identifier.strip().lower()
                        st.session_state.role = role
                        st.rerun()
                    else:
                        st.session_state["captcha_login"] = _new_captcha()
                        st.error("מספר טלפון או סיסמה שגויים")

        with tab_register:
            st.markdown("### 📝 הרשמה")
            reg_name  = st.text_input("שם מלא", key="reg_name")
            reg_phone = st.text_input("טלפון", placeholder="050-000-0000", key="reg_phone")
            reg_email = st.text_input("אימייל", placeholder="you@example.com", key="reg_email")
            reg_pass  = st.text_input("סיסמה", type="password", key="reg_pass")
            reg_pass2 = st.text_input("אימות סיסמה", type="password", key="reg_pass2")
            captcha_ok_reg = _render_captcha("register")
            if st.button("הרשמה", use_container_width=True, type="primary", key="btn_register"):
                if not captcha_ok_reg:
                    st.error("קוד האימות שגוי")
                elif reg_pass != reg_pass2:
                    st.error("הסיסמאות אינן תואמות")
                else:
                    err = _register_user(reg_name, reg_phone, reg_email, reg_pass)
                    if err:
                        st.error(err)
                    else:
                        st.session_state.registered = True
                        st.rerun()


# ── App entry point ───────────────────────────────────────────────────────────

st.set_page_config(page_title="נדל״ן - סינון מודעות", layout="wide", page_icon="🏡")
_inject_styles()

if not st.session_state.get("logged_in"):
    _login_page()
    st.stop()

_record_visit()
es_ok = elastic.ping()
is_admin = st.session_state.get("role") == "admin"

with st.sidebar:
    nav_options = ["🏡 מודעות", "🔧 ניהול"] if is_admin else ["🏡 מודעות"]
    nav = st.radio("ניווט", nav_options, label_visibility="collapsed")
    st.divider()
    st.caption(f"👤 {st.session_state.get('username')}")
    if st.button("יציאה", use_container_width=True):
        for key in ["logged_in", "username", "role", "visit_recorded", "visitor_id"]:
            st.session_state.pop(key, None)
        st.rerun()

if nav == "🏡 מודעות":
    page_main(es_ok, is_admin=is_admin)
else:
    page_admin(es_ok)
