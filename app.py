import hashlib
import hmac
import json
import math
import os
import secrets as pysecrets
import sqlite3
import time
import uuid
from datetime import datetime, timedelta, timezone

import folium
import pandas as pd
import requests
import streamlit as st
from streamlit_folium import st_folium

try:  # اختياري: للتتبع الحي عبر GPS المتصفح
    from streamlit_js_eval import get_geolocation
except Exception:
    get_geolocation = None

st.set_page_config(
    page_title="شعْتَلة",
    page_icon="🚌",
    layout="wide",
    initial_sidebar_state="expanded",
)

st.markdown("""
<style>
@import url('https://fonts.googleapis.com/css2?family=Cairo:wght@400;600;800&display=swap');
html, body, [class*="css"] {
    font-family: 'Cairo', sans-serif;
    direction: rtl;
    text-align: right;
}
.stMetric { background-color: #f8f9fa; border-radius: 8px; padding: 10px; }
</style>
""", unsafe_allow_html=True)

# ───────────────────────── ثوابت ─────────────────────────
JO_TZ = timezone(timedelta(hours=3))
JORDAN_BOUNDS = {"min_lat": 29.18, "max_lat": 33.38, "min_lon": 34.90, "max_lon": 39.30}
DB_PATH = os.environ.get("MASAR_DB", "masar_database.db")

DEFAULT_HUBS = [
    ("مجمع الشمال (إربد)", 32.5562, 35.8498),
    ("مجمع عمان الجديد (إربد)", 32.5315, 35.8540),
    ("مجمع الأغوار الجديد (إربد)", 32.5442, 35.8398),
    ("جامعة اليرموك - البوابة الشمالية", 32.5370, 35.8530),
    ("جامعة اليرموك - البوابة الجنوبية", 32.5290, 35.8550),
    ("جامعة العلوم والتكنولوجيا (JUST)", 32.4950, 35.9912),
    ("مجمع صويلح (عمان)", 32.0232, 35.8425),
    ("مجمع الشمال (عمان - طبربور)", 32.0018, 35.9221),
    ("الجامعة الأردنية - البوابة الرئيسية", 32.0155, 35.8700),
    ("جامعة البلقاء التطبيقية (السلط)", 32.0350, 35.7275),
    ("الجامعة الهاشمية (الزرقاء)", 32.1025, 36.1830),
    ("مجمع الأمير راشد (الزرقاء)", 32.0620, 36.0880),
    ("جامعة آل البيت (المفرق)", 32.3420, 36.2390),
    ("جامعة فيلادلفيا", 32.1765, 35.8450),
    ("جامعة جرش الأهلية", 32.2530, 35.8920),
]

UNIVERSITIES = [
    "جامعة اليرموك", "الجامعة الأردنية", "جامعة العلوم والتكنولوجيا",
    "جامعة البلقاء التطبيقية", "الجامعة الهاشمية", "جامعة آل البيت", "أخرى",
]

PAGE_TRACK, PAGE_SUGGEST, PAGE_ADMIN = "تتبع ومسارات الباصات", "اقتراح خط جديد", "بوابة الإدارة"
MODE_VIEW, MODE_SIM, MODE_LOCATE = "🗺️ عرض المسار", "🚍 محاكاة حركة الباص", "📍 تتبع موقعي على المسار"
IN_HUBS, IN_PINS = "اختيار محطات ومجمعات جاهزة", "تثبيت الدبوس يدوياً على الخريطة"


def now_str():
    return datetime.now(JO_TZ).strftime("%Y-%m-%d %H:%M:%S")


def is_within_jordan(lat, lon):
    return (JORDAN_BOUNDS["min_lat"] <= lat <= JORDAN_BOUNDS["max_lat"]
            and JORDAN_BOUNDS["min_lon"] <= lon <= JORDAN_BOUNDS["max_lon"])


# ───────────────────────── قاعدة البيانات ─────────────────────────
def get_db():
    conn = sqlite3.connect(DB_PATH, check_same_thread=False)
    conn.row_factory = sqlite3.Row
    return conn


@st.cache_resource
def init_db():
    conn = get_db()
    c = conn.cursor()
    c.execute("""
        CREATE TABLE IF NOT EXISTS routes (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            route_name TEXT NOT NULL,
            university TEXT NOT NULL,
            fare REAL NOT NULL,
            distance_km REAL,
            duration_min REAL,
            coordinates TEXT NOT NULL,
            notes TEXT,
            status TEXT DEFAULT 'approved'
        )""")
    c.execute("""
        CREATE TABLE IF NOT EXISTS hubs (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            name TEXT UNIQUE NOT NULL,
            lat REAL NOT NULL,
            lon REAL NOT NULL
        )""")
    c.execute("""
        CREATE TABLE IF NOT EXISTS admins (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            username TEXT UNIQUE NOT NULL,
            pw_hash TEXT NOT NULL,
            salt TEXT NOT NULL,
            created_at TEXT
        )""")
    c.execute("""
        CREATE TABLE IF NOT EXISTS events (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            ts TEXT NOT NULL,
            session_id TEXT NOT NULL,
            event TEXT NOT NULL,
            detail TEXT
        )""")
    # ترقية قواعد البيانات القديمة
    cols = {r["name"] for r in c.execute("PRAGMA table_info(routes)").fetchall()}
    if "stops" not in cols:
        c.execute("ALTER TABLE routes ADD COLUMN stops TEXT")
    if "geometry_source" not in cols:
        c.execute("ALTER TABLE routes ADD COLUMN geometry_source TEXT DEFAULT 'osrm'")
    conn.commit()
    if c.execute("SELECT COUNT(*) FROM hubs").fetchone()[0] == 0:
        c.executemany("INSERT OR IGNORE INTO hubs (name, lat, lon) VALUES (?, ?, ?)", DEFAULT_HUBS)
        conn.commit()
    conn.close()
    return True


init_db()


def db_execute(query, params=(), fetchall=False, commit=False):
    conn = get_db()
    try:
        cur = conn.execute(query, params)
        rows = [dict(r) for r in cur.fetchall()] if fetchall else None
        if commit:
            conn.commit()
        return rows
    finally:
        conn.close()


# ── المجمعات ──
def get_all_hubs():
    rows = db_execute("SELECT * FROM hubs ORDER BY id", fetchall=True) or []
    return {r["name"]: (r["lat"], r["lon"]) for r in rows}


def upsert_hub(name, lat, lon):
    db_execute("""
        INSERT INTO hubs (name, lat, lon) VALUES (?, ?, ?)
        ON CONFLICT(name) DO UPDATE SET lat=excluded.lat, lon=excluded.lon
    """, (name, float(lat), float(lon)), commit=True)


# ── الخطوط ──
def get_routes(status="approved"):
    return db_execute("SELECT * FROM routes WHERE status = ? ORDER BY id", (status,), fetchall=True) or []


def haversine_km(lat1, lon1, lat2, lon2):
    r = 6371.0
    p1, p2 = math.radians(lat1), math.radians(lat2)
    dphi, dl = math.radians(lat2 - lat1), math.radians(lon2 - lon1)
    a = math.sin(dphi / 2) ** 2 + math.cos(p1) * math.cos(p2) * math.sin(dl / 2) ** 2
    return r * 2 * math.atan2(math.sqrt(a), math.sqrt(1 - a))


@st.cache_data(ttl=3600, show_spinner=False)
def _osrm(points):
    """يرمي استثناءً عند الفشل، وبالتالي لا يُخزَّن الفشل في الكاش."""
    coords_str = ";".join(f"{lon},{lat}" for lat, lon in points)
    res = requests.get(
        f"https://router.project-osrm.org/route/v1/driving/{coords_str}",
        params={"overview": "full", "geometries": "geojson"}, timeout=8,
    )
    res.raise_for_status()
    data = res.json()
    if data.get("code") != "Ok" or not data.get("routes"):
        raise ValueError("no route")
    r = data["routes"][0]
    return (round(r["distance"] / 1000.0, 2), round(r["duration"] / 60.0, 1),
            [[p[1], p[0]] for p in r["geometry"]["coordinates"]])


def compute_route(points):
    """points: قائمة (lat, lon) بالترتيب: انطلاق، محطات وسيطة، وصول."""
    pts = tuple((round(float(a), 6), round(float(b), 6)) for a, b in points)
    try:
        d, t, c = _osrm(pts)
        return {"distance": d, "duration": t, "coords": c, "source": "osrm"}
    except Exception:
        d = round(sum(haversine_km(*pts[i], *pts[i + 1]) for i in range(len(pts) - 1)), 2)
        return {"distance": d, "duration": round(d / 40.0 * 60, 1),
                "coords": [list(p) for p in pts], "source": "straight"}


def save_route(name, university, fare, stops, notes, status):
    geo = compute_route([(s["lat"], s["lon"]) for s in stops])
    db_execute("""
        INSERT INTO routes (route_name, university, fare, distance_km, duration_min,
                            coordinates, notes, status, stops, geometry_source)
        VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
    """, (name, university, float(fare), geo["distance"], geo["duration"],
          json.dumps(geo["coords"]), notes, status,
          json.dumps(stops, ensure_ascii=False), geo["source"]), commit=True)
    return geo["source"]


def recalc_route(route):
    """يعيد حساب المسار بإحداثيات المجمعات الحالية. يرجع مصدر الهندسة أو None."""
    if not route.get("stops"):
        return None
    hubs = get_all_hubs()
    stops = json.loads(route["stops"])
    for s in stops:
        if s.get("name") in hubs:
            s["lat"], s["lon"] = hubs[s["name"]]
    geo = compute_route([(s["lat"], s["lon"]) for s in stops])
    db_execute("""
        UPDATE routes SET distance_km=?, duration_min=?, coordinates=?, stops=?, geometry_source=?
        WHERE id=?
    """, (geo["distance"], geo["duration"], json.dumps(geo["coords"]),
          json.dumps(stops, ensure_ascii=False), geo["source"], int(route["id"])), commit=True)
    return geo["source"]


def recalc_routes_using_hub(hub_name):
    n = 0
    for status in ("approved", "pending"):
        for r in get_routes(status):
            if r.get("stops") and any(s.get("name") == hub_name for s in json.loads(r["stops"])):
                recalc_route(r)
                n += 1
    return n


def update_route(route_id, name, fare, notes):
    db_execute("UPDATE routes SET route_name=?, fare=?, notes=? WHERE id=?",
               (str(name), float(fare), str(notes or ""), int(route_id)), commit=True)


def set_route_status(route_id, status, fare=None):
    if fare is not None:
        db_execute("UPDATE routes SET status=?, fare=? WHERE id=?", (status, float(fare), int(route_id)), commit=True)
    else:
        db_execute("UPDATE routes SET status=? WHERE id=?", (status, int(route_id)), commit=True)


def delete_route(route_id):
    db_execute("DELETE FROM routes WHERE id=?", (int(route_id),), commit=True)


# ── الأحداث والإحصائيات ──
def log_event(event, detail=""):
    try:
        db_execute("INSERT INTO events (ts, session_id, event, detail) VALUES (?, ?, ?, ?)",
                   (now_str(), st.session_state.sid, event, str(detail)), commit=True)
    except Exception:
        pass


# ── المشرفون وكلمات المرور ──
def hash_pw(pw, salt=None):
    salt = salt or pysecrets.token_hex(16)
    h = hashlib.pbkdf2_hmac("sha256", pw.encode(), bytes.fromhex(salt), 200_000).hex()
    return h, salt


def verify_pw(pw, pw_hash, salt):
    return hmac.compare_digest(hash_pw(pw, salt)[0], pw_hash)


def get_master_secret():
    try:
        v = st.secrets["ADMIN_PASSWORD"]
        if v:
            return str(v)
    except Exception:
        pass
    return os.environ.get("ADMIN_PASSWORD", "")


def list_admins():
    return db_execute("SELECT id, username, created_at FROM admins ORDER BY id", fetchall=True) or []


def get_admin(username):
    rows = db_execute("SELECT * FROM admins WHERE username = ?", (username,), fetchall=True)
    return rows[0] if rows else None


def add_admin(username, pw):
    if get_admin(username):
        return False
    h, s = hash_pw(pw)
    db_execute("INSERT INTO admins (username, pw_hash, salt, created_at) VALUES (?, ?, ?, ?)",
               (username, h, s, now_str()), commit=True)
    return True


def set_admin_pw(username, pw):
    h, s = hash_pw(pw)
    db_execute("UPDATE admins SET pw_hash=?, salt=? WHERE username=?", (h, s, username), commit=True)


def delete_admin(username):
    db_execute("DELETE FROM admins WHERE username=?", (username,), commit=True)


# ───────────────────────── الخرائط ─────────────────────────
def base_map(center=(31.95, 35.93), zoom=8):
    return folium.Map(location=list(center), zoom_start=zoom, tiles="CartoDB positron")


def add_hub_markers(m, hubs, highlight=None):
    for n, (la, lo) in hubs.items():
        on = n == highlight
        folium.CircleMarker(
            [la, lo], radius=9 if on else 6, color="#d9534f" if on else "#2A75D3",
            fill=True, fill_opacity=0.85, tooltip=n,
        ).add_to(m)


def route_map(coords, stops=None, dest_label="الوجهة", bus_idx=None, user_pos=None):
    m = folium.Map(location=coords[0], zoom_start=12, tiles="CartoDB positron")
    lats = [c[0] for c in coords]
    lons = [c[1] for c in coords]
    if user_pos:
        lats.append(user_pos[0])
        lons.append(user_pos[1])
    m.fit_bounds([[min(lats), min(lons)], [max(lats), max(lons)]])
    folium.PolyLine(coords, color="#2A75D3", weight=5, opacity=0.85).add_to(m)

    if stops and len(stops) > 2:
        for s in stops[1:-1]:
            folium.CircleMarker([s["lat"], s["lon"]], radius=6, color="#f0ad4e", fill=True,
                                fill_opacity=0.9, tooltip=s.get("name") or "محطة وسيطة").add_to(m)

    s_pos = [stops[0]["lat"], stops[0]["lon"]] if stops else coords[0]
    e_pos = [stops[-1]["lat"], stops[-1]["lon"]] if stops else coords[-1]
    s_name = (stops[0].get("name") if stops else None) or "نقطة الانطلاق"
    e_name = (stops[-1].get("name") if stops else None) or dest_label
    folium.Marker(s_pos, tooltip=s_name, icon=folium.Icon(color="green", icon="play")).add_to(m)
    folium.Marker(e_pos, tooltip=e_name, icon=folium.Icon(color="red", icon="flag")).add_to(m)

    if bus_idx is not None:
        folium.Marker(coords[bus_idx], tooltip="موقع الباص الحالي",
                      icon=folium.Icon(color="orange", icon="bus", prefix="fa")).add_to(m)
    if user_pos:
        folium.Marker(user_pos, tooltip="موقعك",
                      icon=folium.Icon(color="blue", icon="user", prefix="fa")).add_to(m)
    return m


def new_click(map_data, seen_key):
    """يرجع نقرة جديدة فقط (يتجاهل آخر نقرة قديمة محفوظة في المكوّن)."""
    lc = (map_data or {}).get("last_clicked")
    if not lc:
        return None
    t = (round(lc["lat"], 7), round(lc["lng"], 7))
    if st.session_state.get(seen_key) == t:
        return None
    st.session_state[seen_key] = t
    return [lc["lat"], lc["lng"]]


def locate_on_route(coords, lat, lon):
    """أقرب نقطة على المسار (إسقاط على القطع المستقيمة). يرجع (البعد كم، المسافة المقطوعة كم، الطول الكلي كم)."""
    cum = [0.0]
    for i in range(len(coords) - 1):
        cum.append(cum[-1] + haversine_km(*coords[i], *coords[i + 1]))
    kx, ky = 111.320 * math.cos(math.radians(lat)), 110.574
    best_d, best_along = 1e9, 0.0
    for i in range(len(coords) - 1):
        ax, ay = (coords[i][1] - lon) * kx, (coords[i][0] - lat) * ky
        bx, by = (coords[i + 1][1] - lon) * kx, (coords[i + 1][0] - lat) * ky
        dx, dy = bx - ax, by - ay
        seg2 = dx * dx + dy * dy
        t = 0.0 if seg2 == 0 else max(0.0, min(1.0, -(ax * dx + ay * dy) / seg2))
        d = math.hypot(ax + t * dx, ay + t * dy)
        if d < best_d:
            best_d, best_along = d, cum[i] + t * (cum[i + 1] - cum[i])
    return best_d, best_along, cum[-1]


def show_progress(route, coords, lat, lon):
    off, along, total = locate_on_route(coords, lat, lon)
    frac = min(1.0, max(0.0, along / total)) if total > 0 else 0.0
    c1, c2, c3 = st.columns(3)
    c1.metric("المسار المنجز", f"{frac * 100:.0f}%")
    c2.metric("المسافة المتبقية", f"{(route['distance_km'] or 0) * (1 - frac):.1f} كم")
    c3.metric("الزمن المتبقي", f"{(route['duration_min'] or 0) * (1 - frac):.0f} دقيقة")
    st.progress(frac)
    if off > 0.3:
        st.warning(f"موقعك يبعد حوالي {off * 1000:.0f} متراً عن المسار.")
    else:
        st.success("أنت على المسار ✅")


# ───────────────────────── صفحة التتبع ─────────────────────────
def page_tracking():
    st.title("🗺️ استعراض وتتبع مسار الباص")
    routes = get_routes("approved")
    if not routes:
        st.info("لا توجد مسارات مسجلة حالياً. يمكنك اقتراح مسار جديد من القائمة الجانبية.")
        return

    labels = {f"{r['route_name']} ({r['university']})": r for r in routes}
    route = labels[st.selectbox("اختر المسار:", list(labels.keys()))]

    if st.session_state.get("last_route_logged") != route["id"]:
        st.session_state.last_route_logged = route["id"]
        st.session_state.manual_pos = None
        log_event("route_view", route["id"])

    coords = json.loads(route["coordinates"])
    stops = json.loads(route["stops"]) if route.get("stops") else None

    c1, c2, c3 = st.columns(3)
    c1.metric("الأجرة المعتمدة", f"{route['fare']:.2f} د.أ")
    c2.metric("المسافة التقديرية", f"{route['distance_km']} كم")
    c3.metric("زمن الرحلة التقريبي", f"{route['duration_min']} دقيقة")
    if route.get("notes"):
        st.info(f"ملاحظات: {route['notes']}")
    if route.get("geometry_source") == "straight":
        st.warning("هذا المسار معروض كخط مستقيم تقريبي لتعذّر الاتصال بخدمة الطرق. سيقوم المشرف بتحديثه.")

    mode = st.radio("الوضع:", [MODE_VIEW, MODE_SIM, MODE_LOCATE], horizontal=True)

    if mode == MODE_VIEW:
        st_folium(route_map(coords, stops, route["university"]), width=900, height=480,
                  key=f"view_{route['id']}", returned_objects=[])

    elif mode == MODE_SIM:
        ph = st.empty()

        def draw(idx, key):
            with ph.container():
                st_folium(route_map(coords, stops, route["university"], bus_idx=idx),
                          width=900, height=450, key=key, returned_objects=[])

        if st.button("▶️ ابدأ المحاكاة"):
            log_event("simulation", route["id"])
            n = min(len(coords), 25)
            idxs = sorted({int(i * (len(coords) - 1) / (n - 1)) for i in range(n)})
            bar, txt = st.progress(0), st.empty()
            for k, idx in enumerate(idxs):
                pct = int((k + 1) / len(idxs) * 100)
                bar.progress(pct)
                rem = max(0.0, round((route["duration_min"] or 0) * (1 - pct / 100.0), 1))
                txt.markdown(f"**الباص في الطريق 🚍** | المنجز: **{pct}%** | المتبقي: **{rem} دقيقة**")
                draw(idx, f"sim_{route['id']}_{k}")
                time.sleep(0.5)
            st.success("🏁 وصل الباص إلى المحطة النهائية!")
        else:
            draw(0, f"sim_idle_{route['id']}")

    else:  # تتبع موقع المستخدم
        method = st.radio("طريقة تحديد موقعك:", ["GPS من المتصفح", "النقر على الخريطة"], horizontal=True)
        pos = None
        if method == "GPS من المتصفح":
            if get_geolocation is None:
                st.warning("لتفعيل GPS ثبّت الحزمة: pip install streamlit-js-eval (ويلزم اتصال HTTPS). "
                           "يمكنك الآن استخدام خيار النقر على الخريطة.")
            else:
                if st.button("🔄 تحديث موقعي"):
                    st.session_state.geo_n = st.session_state.get("geo_n", 0) + 1
                loc = get_geolocation(component_key=f"geo_{st.session_state.get('geo_n', 0)}")
                if loc and loc.get("coords"):
                    pos = [loc["coords"]["latitude"], loc["coords"]["longitude"]]
                    acc = loc["coords"].get("accuracy")
                    if acc:
                        st.caption(f"دقة GPS التقريبية: ±{acc:.0f} متر")
                else:
                    st.info("بانتظار السماح للمتصفح بالوصول إلى موقعك...")
            if pos:
                if not st.session_state.get("locate_logged"):
                    st.session_state.locate_logged = True
                    log_event("locate", route["id"])
                show_progress(route, coords, *pos)
            st_folium(route_map(coords, stops, route["university"], user_pos=pos), width=900, height=450,
                      key=f"gps_{route['id']}", returned_objects=[])
        else:
            st.caption("انقر على الخريطة في المكان الذي تقف فيه (أو ينتظر الباص).")
            data = st_folium(route_map(coords, stops, route["university"], user_pos=st.session_state.get("manual_pos")),
                             width=900, height=450, key=f"manual_{route['id']}", returned_objects=["last_clicked"])
            click = new_click(data, "manual_click_seen")
            if click:
                st.session_state.manual_pos = click
                st.rerun()
            pos = st.session_state.get("manual_pos")
            if pos:
                show_progress(route, coords, *pos)


# ───────────────────────── صفحة الاقتراح ─────────────────────────
def page_suggest():
    st.title("➕ اقتراح مسار باص جديد")
    hubs = get_all_hubs()
    if "pins" not in st.session_state:
        st.session_state.pins = {"start": None, "end": None, "via": []}
    pins = st.session_state.pins

    input_type = st.radio("طريقة تحديد المواقع:", [IN_HUBS, IN_PINS], horizontal=True)

    if input_type == IN_PINS:
        st.caption("انقر لتثبيت نقطة الانطلاق (أخضر)، ثم الوصول (أحمر). أي نقرات إضافية تُضاف كمحطات وسيطة بالترتيب (برتقالي).")
        m = base_map()
        add_hub_markers(m, hubs)
        if pins["start"]:
            folium.Marker(pins["start"], tooltip="الانطلاق", icon=folium.Icon(color="green")).add_to(m)
        for i, v in enumerate(pins["via"], 1):
            folium.Marker(v, tooltip=f"محطة وسيطة {i}", icon=folium.Icon(color="orange")).add_to(m)
        if pins["end"]:
            folium.Marker(pins["end"], tooltip="الوصول", icon=folium.Icon(color="red")).add_to(m)

        data = st_folium(m, width=900, height=380, key="pin_map", returned_objects=["last_clicked"])
        click = new_click(data, "pin_click_seen")
        if click:
            if not pins["start"]:
                pins["start"] = click
            elif not pins["end"]:
                pins["end"] = click
            else:
                pins["via"].append(click)
            st.rerun()

        cr, cp = st.columns([1, 3])
        with cr:
            if st.button("🔄 إعادة ضبط الدبابيس"):
                st.session_state.pins = {"start": None, "end": None, "via": []}
                st.rerun()
        with cp:
            st.write(f"🟢 الانطلاق: {'تم' if pins['start'] else '—'} | 🔴 الوصول: {'تم' if pins['end'] else '—'} "
                     f"| 🟠 محطات وسيطة: {len(pins['via'])}")

    with st.form("suggest_form"):
        c1, c2 = st.columns(2)
        with c1:
            name = st.text_input("اسم الخط (مثال: مجمع الأغوار - جامعة اليرموك):")
            uni = st.selectbox("الجامعة الوجهة:", UNIVERSITIES)
        with c2:
            fare = st.number_input("الأجرة المتوقعة (د.أ):", min_value=0.10, value=0.50, step=0.05, format="%.2f")
            notes = st.text_area("أماكن التوقف أو ملاحظات:")
        if input_type == IN_HUBS:
            names = list(hubs.keys())
            cc1, cc2 = st.columns(2)
            with cc1:
                start_hub = st.selectbox("نقطة الانطلاق:", names, index=0)
            with cc2:
                end_hub = st.selectbox("نقطة الوصول:", names, index=min(2, len(names) - 1))
            via_hubs = st.multiselect("محطات وسيطة (اختياري، بالترتيب):", names)
        submitted = st.form_submit_button("إرسال المقترح للإدارة")

    if not submitted:
        return
    if not name.strip():
        st.warning("يرجى إدخال اسم المسار.")
        return
    if input_type == IN_HUBS:
        if start_hub == end_hub:
            st.error("نقطة الانطلاق والوصول متطابقتان.")
            return
        chosen = [start_hub] + via_hubs + [end_hub]
        stops = [{"name": n, "lat": hubs[n][0], "lon": hubs[n][1]} for n in chosen]
    else:
        if not (pins["start"] and pins["end"]):
            st.error("يرجى تثبيت دبوس الانطلاق ودبوس الوصول على الخريطة.")
            return
        pts = [pins["start"]] + pins["via"] + [pins["end"]]
        stops = [{"name": None, "lat": p[0], "lon": p[1]} for p in pts]
    if not all(is_within_jordan(s["lat"], s["lon"]) for s in stops):
        st.error("❌ إحدى النقاط تقع خارج حدود الأردن.")
        return

    with st.spinner("جارٍ حساب المسار على الطرق..."):
        src = save_route(name.strip(), uni, fare, stops, notes, "pending")
    st.session_state.pins = {"start": None, "end": None, "via": []}
    st.success("✅ تم إرسال المقترح لمراجعة الإدارة.")
    if src == "straight":
        st.warning("تعذّر الاتصال بخدمة الطرق؛ سيُحسب المسار بدقة عند مراجعة المشرف.")


# ───────────────────────── بوابة الإدارة ─────────────────────────
def admin_login():
    st.title("🔒 بوابة الإدارة والتحكم")
    master_secret = get_master_secret()
    role = st.radio("رتبة الدخول:", ["مساعد آدمن (Assistant)", "الآدمن الرئيسي (Master Admin)"], horizontal=True)
    is_master = role.startswith("الآدمن")
    if is_master and not master_secret:
        st.error("لم يتم ضبط ADMIN_PASSWORD في secrets.toml أو متغيرات البيئة، لذا لا يمكن دخول الآدمن الرئيسي.")
    username = "" if is_master else st.text_input("اسم المستخدم:")
    pwd = st.text_input("كلمة المرور:", type="password")

    now = time.time()
    lock_left = int(st.session_state.get("lock_until", 0) - now)
    if lock_left > 0:
        st.error(f"محاولات خاطئة كثيرة. انتظر {lock_left} ثانية.")
    if st.button("تسجيل الدخول", disabled=lock_left > 0):
        ok = False
        if is_master:
            ok = bool(master_secret) and hmac.compare_digest(pwd.encode(), master_secret.encode())
        else:
            row = get_admin(username.strip())
            ok = bool(row) and verify_pw(pwd, row["pw_hash"], row["salt"])
        if ok:
            st.session_state.admin_role = "master" if is_master else "assistant"
            st.session_state.admin_user = "المشرف الرئيسي" if is_master else username.strip()
            st.session_state.fails = 0
            log_event("admin_login", st.session_state.admin_role)
            st.rerun()
        else:
            st.session_state.fails = st.session_state.get("fails", 0) + 1
            if st.session_state.fails >= 5:
                st.session_state.lock_until = now + 60
                st.session_state.fails = 0
            st.error("بيانات الدخول غير صحيحة.")


def flash(msg):
    st.session_state["flash"] = msg


def tab_routes():
    routes = get_routes("approved")
    if not routes:
        st.info("لا توجد خطوط معتمدة حالياً.")
        return
    rmap = {f"#{r['id']} - {r['route_name']} ({r['fare']:.2f} د.أ)": r for r in routes}
    r = rmap[st.selectbox("اختر المسار:", list(rmap.keys()))]
    coords = json.loads(r["coordinates"])
    stops = json.loads(r["stops"]) if r.get("stops") else None

    if r.get("geometry_source") == "straight":
        st.warning("هذا المسار خط مستقيم تقريبي. اضغط «إعادة حساب المسار» عند توفر الاتصال.")
    if st.checkbox("عرض المسار على الخريطة", key=f"prev_{r['id']}"):
        st_folium(route_map(coords, stops, r["university"]), width=900, height=380,
                  key=f"adm_prev_{r['id']}", returned_objects=[])

    with st.form(f"edit_form_{r['id']}"):
        c1, c2 = st.columns(2)
        with c1:
            up_name = st.text_input("اسم المسار:", value=r["route_name"])
            up_fare = st.number_input("الأجرة المعتمدة (د.أ):", min_value=0.10, max_value=20.0,
                                      value=float(r["fare"]), step=0.05, format="%.2f")
        with c2:
            up_notes = st.text_area("ملاحظات / تردد الخط:", value=r.get("notes") or "")
        if st.form_submit_button("💾 حفظ التعديلات"):
            update_route(r["id"], up_name, up_fare, up_notes)
            flash("✅ تم تحديث بيانات المسار.")
            st.rerun()

    if r.get("stops"):
        if st.button("🔄 إعادة حساب المسار على الطرق", key=f"recalc_{r['id']}"):
            with st.spinner("جارٍ الحساب..."):
                src = recalc_route(r)
            flash("✅ تم تحديث المسار." if src == "osrm" else "⚠️ تعذّر الاتصال بخدمة الطرق، أُعيد خط مستقيم.")
            st.rerun()
    else:
        st.caption("هذا خط قديم بلا نقاط محفوظة، لذا لا يمكن إعادة حسابه تلقائياً.")

    st.divider()
    st.subheader("⚠️ إزالة المسار نهائياً")
    confirm = st.checkbox("تأكيد الحذف النهائي", key=f"c_del_{r['id']}")
    if st.button("🗑️ حذف المسار", key=f"btn_del_{r['id']}", type="primary"):
        if confirm:
            delete_route(r["id"])
            flash(f"✅ تم حذف مسار '{r['route_name']}'.")
            st.rerun()
        else:
            st.warning("فعّل علامة التأكيد أولاً.")


def tab_pending():
    pending = get_routes("pending")
    if not pending:
        st.success("لا توجد طلبات معلقة.")
        return
    for req in pending:
        with st.expander(f"طلب #{req['id']}: {req['route_name']} — {req['university']}"):
            st.write(f"المسافة: {req['distance_km']} كم | الزمن: {req['duration_min']} دقيقة")
            st.write(f"ملاحظات: {req.get('notes') or 'لا يوجد'}")
            if req.get("geometry_source") == "straight":
                st.warning("المسار خط مستقيم تقريبي (تعذّر الاتصال بخدمة الطرق).")
                if req.get("stops") and st.button("🔄 إعادة حساب المسار", key=f"prc_{req['id']}"):
                    recalc_route(req)
                    st.rerun()
            if st.checkbox("عرض المسار المقترح", key=f"pv_{req['id']}"):
                stops = json.loads(req["stops"]) if req.get("stops") else None
                st_folium(route_map(json.loads(req["coordinates"]), stops, req["university"]),
                          width=800, height=350, key=f"pvmap_{req['id']}", returned_objects=[])
            new_name = st.text_input("اسم الخط:", value=req["route_name"], key=f"pn_{req['id']}")
            fare = st.number_input("السعر المعتمد النهائي (د.أ):", min_value=0.10, value=float(req["fare"]),
                                   step=0.05, format="%.2f", key=f"p_fare_{req['id']}")
            ca, cr = st.columns(2)
            with ca:
                if st.button("✅ اعتماد الخط", key=f"acc_{req['id']}"):
                    update_route(req["id"], new_name, fare, req.get("notes"))
                    set_route_status(req["id"], "approved")
                    flash("✅ تم اعتماد الخط.")
                    st.rerun()
            with cr:
                if st.button("❌ رفض وحذف", key=f"rej_{req['id']}"):
                    delete_route(req["id"])
                    flash("تم رفض المقترح.")
                    st.rerun()


def tab_add():
    hubs = get_all_hubs()
    names = list(hubs.keys())
    with st.form("admin_add_route"):
        a_name = st.text_input("اسم الخط:")
        a_uni = st.selectbox("الجامعة:", UNIVERSITIES)
        a_fare = st.number_input("السعر المعتمد (د.أ):", min_value=0.10, value=0.60, step=0.05, format="%.2f")
        a_notes = st.text_area("تفاصيل وملاحظات:")
        c1, c2 = st.columns(2)
        with c1:
            a_start = st.selectbox("نقطة الانطلاق:", names, index=0)
        with c2:
            a_end = st.selectbox("نقطة الوصول:", names, index=min(1, len(names) - 1))
        a_via = st.multiselect("محطات وسيطة (اختياري، بالترتيب):", names)
        ok = st.form_submit_button("إضافة الخط فوراً")
    if ok:
        if a_name.strip() and a_start != a_end:
            stops = [{"name": n, "lat": hubs[n][0], "lon": hubs[n][1]} for n in [a_start] + a_via + [a_end]]
            with st.spinner("جارٍ حساب المسار..."):
                src = save_route(a_name.strip(), a_uni, a_fare, stops, a_notes, "approved")
            flash("✅ تمت إضافة المسار." + ("" if src == "osrm" else " ⚠️ رُسم كخط مستقيم لتعذّر الاتصال بخدمة الطرق."))
            st.rerun()
        else:
            st.error("اكتب اسم الخط واختر محطتين مختلفتين.")


def tab_hubs():
    """للمشرف الرئيسي فقط: تعديل مواقع المجمعات بالنقر على الخريطة أو بخطوط الطول والعرض."""
    hubs = get_all_hubs()
    names = list(hubs.keys())
    st.caption("اختر محطة ثم انقر على الخريطة لتحديد موقعها الجديد بدقة، أو اكتب الإحداثيات يدوياً. "
               "عند الحفظ تُعاد تلقائياً حسابات كل الخطوط التي تمر بهذه المحطة.")
    sel = st.selectbox("اختر المحطة / المجمع:", names, key="hub_sel")
    click = st.session_state.get("hub_click")

    m = base_map(center=hubs[sel], zoom=12)
    add_hub_markers(m, hubs, highlight=sel)
    if click:
        folium.Marker(click, tooltip="الموقع الجديد المحدد",
                      icon=folium.Icon(color="green", icon="map-marker", prefix="fa")).add_to(m)
    data = st_folium(m, width=900, height=430, key=f"hub_map_{sel}", returned_objects=["last_clicked"])
    c = new_click(data, "hub_click_seen")
    if c:
        st.session_state.hub_click = c
        st.rerun()
    click = st.session_state.get("hub_click")

    cur_lat, cur_lon = hubs[sel]
    d_lat, d_lon = click if click else (cur_lat, cur_lon)
    if click:
        st.info(f"النقطة المحددة على الخريطة: {click[0]:.6f} ، {click[1]:.6f}")
        if st.button("مسح التحديد"):
            st.session_state.hub_click = None
            st.rerun()

    col_edit, col_new = st.columns(2)
    with col_edit:
        st.markdown("#### ✏️ تعديل إحداثيات المحطة المختارة")
        with st.form("edit_hub_form"):
            lat = st.number_input("خط العرض (Latitude):", value=float(d_lat), format="%.6f", step=0.0001)
            lon = st.number_input("خط الطول (Longitude):", value=float(d_lon), format="%.6f", step=0.0001)
            if st.form_submit_button("💾 تحديث الإحداثيات"):
                if not is_within_jordan(lat, lon):
                    st.error("❌ الإحداثيات خارج حدود الأردن.")
                else:
                    upsert_hub(sel, lat, lon)
                    with st.spinner("جارٍ تحديث الخطوط المرتبطة..."):
                        n = recalc_routes_using_hub(sel)
                    st.session_state.hub_click = None
                    flash(f"✅ تم تحديث '{sel}'، وأُعيد حساب {n} خط/خطوط مرتبطة.")
                    st.rerun()
    with col_new:
        st.markdown("#### ➕ إضافة مجمع / محطة جديدة")
        with st.form("add_hub_form"):
            n_name = st.text_input("اسم المجمع أو النقطة:")
            n_lat = st.number_input("خط العرض:", value=float(click[0]) if click else 32.5500, format="%.6f", step=0.0001)
            n_lon = st.number_input("خط الطول:", value=float(click[1]) if click else 35.8500, format="%.6f", step=0.0001)
            if st.form_submit_button("➕ حفظ المحطة"):
                if not n_name.strip():
                    st.warning("اكتب اسم المحطة.")
                elif n_name.strip() in hubs:
                    st.error("الاسم موجود مسبقاً؛ استخدم نموذج التعديل.")
                elif not is_within_jordan(n_lat, n_lon):
                    st.error("❌ الإحداثيات خارج حدود الأردن.")
                else:
                    upsert_hub(n_name.strip(), n_lat, n_lon)
                    st.session_state.hub_click = None
                    flash(f"✅ تمت إضافة '{n_name.strip()}'.")
                    st.rerun()

    with st.expander("جدول كل المحطات وإحداثياتها"):
        st.dataframe(pd.DataFrame([{"المحطة": k, "Lat": v[0], "Lon": v[1]} for k, v in hubs.items()]),
                     use_container_width=True)


def tab_assistants():
    """للمشرف الرئيسي فقط: إضافة وإدارة المساعدين."""
    admins = list_admins()
    if admins:
        st.dataframe(pd.DataFrame(admins).rename(columns={"id": "#", "username": "اسم المستخدم", "created_at": "تاريخ الإضافة"}),
                     use_container_width=True, hide_index=True)
    else:
        st.info("لا يوجد مساعدون بعد.")

    st.markdown("#### ➕ إضافة مساعد آدمن")
    with st.form("add_asst", clear_on_submit=True):
        u = st.text_input("اسم المستخدم:")
        p1 = st.text_input("كلمة المرور (8 أحرف فأكثر):", type="password")
        p2 = st.text_input("تأكيد كلمة المرور:", type="password")
        if st.form_submit_button("إضافة"):
            u = u.strip()
            if len(u) < 3 or " " in u:
                st.error("اسم المستخدم 3 أحرف على الأقل وبدون مسافات.")
            elif len(p1) < 8:
                st.error("كلمة المرور قصيرة (8 أحرف على الأقل).")
            elif p1 != p2:
                st.error("كلمتا المرور غير متطابقتين.")
            elif not add_admin(u, p1):
                st.error("اسم المستخدم موجود مسبقاً.")
            else:
                flash(f"✅ تمت إضافة المساعد '{u}'.")
                st.rerun()

    if admins:
        st.divider()
        target = st.selectbox("اختر مساعداً:", [a["username"] for a in admins])
        c1, c2 = st.columns(2)
        with c1:
            with st.form("reset_pw", clear_on_submit=True):
                np_ = st.text_input("كلمة مرور جديدة:", type="password")
                if st.form_submit_button("🔑 تغيير كلمة المرور"):
                    if len(np_) < 8:
                        st.error("كلمة المرور قصيرة.")
                    else:
                        set_admin_pw(target, np_)
                        flash(f"✅ تم تغيير كلمة مرور '{target}'.")
                        st.rerun()
        with c2:
            ok = st.checkbox("تأكيد حذف المساعد", key=f"del_as_{target}")
            if st.button("🗑️ حذف المساعد", type="primary"):
                if ok:
                    delete_admin(target)
                    flash(f"✅ تم حذف '{target}'.")
                    st.rerun()
                else:
                    st.warning("فعّل التأكيد أولاً.")


def tab_stats():
    """للمشرف الرئيسي فقط: إحصائيات الزوار."""
    conn = get_db()
    try:
        df = pd.read_sql_query("SELECT ts, session_id, event, detail FROM events", conn)
    finally:
        conn.close()
    if df.empty:
        st.info("لا توجد بيانات بعد.")
        return

    df["date"] = df["ts"].str[:10]
    today = datetime.now(JO_TZ).date()
    visits = df[df["event"] == "visit"]
    week_start = (today - timedelta(days=6)).isoformat()

    c1, c2, c3, c4 = st.columns(4)
    c1.metric("إجمالي الزيارات", len(visits))
    c2.metric("زيارات اليوم", int((visits["date"] == today.isoformat()).sum()))
    c3.metric("آخر 7 أيام", int((visits["date"] >= week_start).sum()))
    c4.metric("مشاهدات المسارات", int((df["event"] == "route_view").sum()))

    c5, c6, c7 = st.columns(3)
    c5.metric("مرات تشغيل المحاكاة", int((df["event"] == "simulation").sum()))
    c6.metric("جلسات تتبع الموقع", int((df["event"] == "locate").sum()))
    c7.metric("اقتراحات بانتظار المراجعة", len(get_routes("pending")))

    st.markdown("#### الزيارات اليومية (آخر 14 يوماً)")
    days = [(today - timedelta(days=i)).isoformat() for i in range(13, -1, -1)]
    st.bar_chart(visits.groupby("date").size().reindex(days, fill_value=0))

    rv = df[df["event"] == "route_view"]
    if not rv.empty:
        st.markdown("#### أكثر المسارات مشاهدة")
        names = {str(r["id"]): r["route_name"] for s in ("approved", "pending") for r in get_routes(s)}
        top = rv.groupby("detail").size().sort_values(ascending=False).head(10)
        top.index = [names.get(i, f"#{i} (محذوف)") for i in top.index]
        st.bar_chart(top)

    pv = df[df["event"] == "page"]
    if not pv.empty:
        st.markdown("#### مشاهدات الصفحات")
        st.bar_chart(pv.groupby("detail").size())

    st.caption("ملاحظة: يُحتسب كل فتح للتطبيق في متصفح/تبويب جديد زيارةً واحدة (جلسة)، لذا الرقم تقريبي لعدد الزوار الفعلي.")


def page_admin():
    if not st.session_state.get("admin_role"):
        admin_login()
        return
    role = st.session_state.admin_role
    st.title("🔒 بوابة الإدارة والتحكم")
    st.sidebar.success(f"مرحباً {st.session_state.get('admin_user', '')} ({'آدمن رئيسي' if role == 'master' else 'مساعد'})")
    if st.sidebar.button("تسجيل الخروج"):
        st.session_state.admin_role = None
        st.session_state.admin_user = None
        st.rerun()

    spec = [("🛠️ إدارة المسارات", tab_routes), ("⏳ الطلبات الجديدة", tab_pending), ("➕ إضافة مسار", tab_add)]
    if role == "master":
        spec += [("📍 المواقع والإحداثيات", tab_hubs), ("👥 المساعدون", tab_assistants), ("📊 إحصائيات الزوار", tab_stats)]
    for tab, (_, fn) in zip(st.tabs([t for t, _ in spec]), spec):
        with tab:
            fn()


# ───────────────────────── التشغيل ─────────────────────────
if "sid" not in st.session_state:
    st.session_state.sid = uuid.uuid4().hex
    log_event("visit")

st.sidebar.image("https://img.icons8.com/color/96/bus.png", width=70)
st.sidebar.title("شعْتَلة 🚌")
st.sidebar.caption("مسارات باصات الجامعات الأردنية")
app_mode = st.sidebar.radio("التنقل:", [PAGE_TRACK, PAGE_SUGGEST, PAGE_ADMIN])

if st.session_state.get("last_page") != app_mode:
    st.session_state.last_page = app_mode
    log_event("page", app_mode)

if st.session_state.get("flash"):
    st.success(st.session_state.pop("flash"))

if app_mode == PAGE_TRACK:
    page_tracking()
elif app_mode == PAGE_SUGGEST:
    page_suggest()
else:
    page_admin()
