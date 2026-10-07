import streamlit as st

st.set_page_config(
    page_title="شعْتَلة",
    page_icon="🚌",
    layout="wide",
    initial_sidebar_state="expanded"
)

from streamlit_folium import st_folium
import folium
import json
import math
import os
import time
import requests
import sqlite3

try:
    import libsql_client
    HAS_TURSO = True
except ImportError:
    HAS_TURSO = False

st.markdown("""
<style>
@import url('https://fonts.googleapis.com/css2?family=Cairo:wght@400;600;800&display=swap');
html, body, [class*="css"] {
    font-family: 'Cairo', sans-serif;
    direction: rtl;
    text-align: right;
}
.stMetric {
    background-color: #f8f9fa;
    border-radius: 8px;
    padding: 10px;
}
</style>
""", unsafe_allow_html=True)

JORDAN_BOUNDS = {
    'min_lat': 29.18,
    'max_lat': 33.38,
    'min_lon': 34.90,
    'max_lon': 39.30
}

def is_within_jordan(lat, lon):
    return (JORDAN_BOUNDS['min_lat'] <= lat <= JORDAN_BOUNDS['max_lat']) and \
           (JORDAN_BOUNDS['min_lon'] <= lon <= JORDAN_BOUNDS['max_lon'])

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
    ("جامعة جرش الأهلية", 32.2530, 35.8920)
]

UNIVERSITIES = [
    "جامعة اليرموك", 
    "الجامعة الأردنية", 
    "جامعة العلوم والتكنولوجيا", 
    "جامعة البلقاء التطبيقية", 
    "الجامعة الهاشمية", 
    "جامعة آل البيت", 
    "أخرى"
]

def use_turso():
    return HAS_TURSO and ("TURSO_DATABASE_URL" in st.secrets) and ("TURSO_AUTH_TOKEN" in st.secrets)

def get_db():
    conn = sqlite3.connect("masar_database.db", check_same_thread=False)
    conn.row_factory = sqlite3.Row
    return conn

def db_execute(query, params=(), fetchall=False, commit=False):
    if use_turso():
        client = libsql_client.create_client_sync(
            url=st.secrets["TURSO_DATABASE_URL"],
            auth_token=st.secrets["TURSO_AUTH_TOKEN"]
        )
        q_clean = query.replace("?", "$?")
        res = client.execute(query, params)
        if fetchall:
            cols = res.columns
            return [dict(zip(cols, row)) for row in res.rows]
        return None
    else:
        conn = get_db()
        c = conn.cursor()
        c.execute(query, params)
        data = None
        if commit:
            conn.commit()
        if fetchall:
            rows = c.fetchall()
            data = [dict(r) for r in rows]
        conn.close()
        return data

def init_db():
    if use_turso():
        client = libsql_client.create_client_sync(
            url=st.secrets["TURSO_DATABASE_URL"],
            auth_token=st.secrets["TURSO_AUTH_TOKEN"]
        )
        client.execute("""
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
            )
        """)
        client.execute("""
            CREATE TABLE IF NOT EXISTS hubs (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                name TEXT UNIQUE NOT NULL,
                lat REAL NOT NULL,
                lon REAL NOT NULL
            )
        """)
        res = client.execute("SELECT COUNT(*) FROM hubs")
        if res.rows[0][0] == 0:
            for h in DEFAULT_HUBS:
                client.execute("INSERT OR IGNORE INTO hubs (name, lat, lon) VALUES (?, ?, ?)", h)
    else:
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
            )
        """)
        c.execute("""
            CREATE TABLE IF NOT EXISTS hubs (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                name TEXT UNIQUE NOT NULL,
                lat REAL NOT NULL,
                lon REAL NOT NULL
            )
        """)
        conn.commit()
        c.execute("SELECT COUNT(*) FROM hubs")
        if c.fetchone()[0] == 0:
            c.executemany("INSERT OR IGNORE INTO hubs (name, lat, lon) VALUES (?, ?, ?)", DEFAULT_HUBS)
            conn.commit()
        conn.close()

init_db()

def get_all_hubs():
    rows = db_execute("SELECT * FROM hubs ORDER BY id ASC", fetchall=True) or []
    return {r['name']: (r['lat'], r['lon']) for r in rows}

def upsert_hub(name, lat, lon):
    if use_turso():
        client = libsql_client.create_client_sync(
            url=st.secrets["TURSO_DATABASE_URL"],
            auth_token=st.secrets["TURSO_AUTH_TOKEN"]
        )
        client.execute("""
            INSERT INTO hubs (name, lat, lon) VALUES (?, ?, ?)
            ON CONFLICT(name) DO UPDATE SET lat=excluded.lat, lon=excluded.lon
        """, (name, float(lat), float(lon)))
    else:
        db_execute("""
            INSERT INTO hubs (name, lat, lon) VALUES (?, ?, ?)
            ON CONFLICT(name) DO UPDATE SET lat=excluded.lat, lon=excluded.lon
        """, (name, float(lat), float(lon)), commit=True)

def get_routes(status='approved'):
    return db_execute("SELECT * FROM routes WHERE status = ?", (status,), fetchall=True) or []

def add_route(route_name, university, fare, distance_km, duration_min, coords_json, notes, status='approved'):
    db_execute("""
        INSERT INTO routes (route_name, university, fare, distance_km, duration_min, coordinates, notes, status)
        VALUES (?, ?, ?, ?, ?, ?, ?, ?)
    """, (route_name, university, fare, distance_km, duration_min, coords_json, notes, status), commit=True)

def update_route_fare_and_details(route_id, new_fare, new_name=None, new_notes=None):
    if new_name and new_notes is not None:
        db_execute("""
            UPDATE routes 
            SET fare = ?, route_name = ?, notes = ?
            WHERE id = ?
        """, (float(new_fare), str(new_name), str(new_notes), int(route_id)), commit=True)
    else:
        db_execute("UPDATE routes SET fare = ? WHERE id = ?", (float(new_fare), int(route_id)), commit=True)

def set_route_status(route_id, status, fare=None):
    if fare is not None:
        db_execute("UPDATE routes SET status = ?, fare = ? WHERE id = ?", (status, float(fare), int(route_id)), commit=True)
    else:
        db_execute("UPDATE routes SET status = ? WHERE id = ?", (status, int(route_id)), commit=True)

def delete_route(route_id):
    db_execute("DELETE FROM routes WHERE id = ?", (int(route_id),), commit=True)

def haversine_km(lat1, lon1, lat2, lon2):
    r = 6371.0
    phi1, phi2 = math.radians(lat1), math.radians(lat2)
    dphi = math.radians(lat2 - lat1)
    dlambda = math.radians(lon2 - lon1)
    a = math.sin(dphi / 2)**2 + math.cos(phi1) * math.cos(phi2) * math.sin(dlambda / 2)**2
    c = 2 * math.atan2(math.sqrt(a), math.sqrt(1 - a))
    return r * c

@st.cache_data(ttl=3600)
def fetch_osrm_route(start_lat, start_lon, end_lat, end_lon):
    try:
        url = f"https://router.project-osrm.org/route/v1/driving/{start_lon},{start_lat};{end_lon},{end_lat}?overview=full&geometries=geojson"
        res = requests.get(url, timeout=5)
        if res.status_code == 200:
            data = res.json()
            if data.get('routes'):
                r = data['routes'][0]
                dist = round(r['distance'] / 1000.0, 2)
                dur = round(r['duration'] / 60.0, 1)
                coords = [[pt[1], pt[0]] for pt in r['geometry']['coordinates']]
                return dist, dur, coords
    except Exception:
        pass
    dist = round(haversine_km(start_lat, start_lon, end_lat, end_lon), 2)
    dur = round((dist / 40.0) * 60, 1)
    return dist, dur, [[start_lat, start_lon], [end_lat, end_lon]]

# الشريط الجانبي
st.sidebar.image("https://img.icons8.com/color/96/bus.png", width=70)
st.sidebar.title("شعْتَلة 🚌")
st.sidebar.caption("مسارات باصات الجامعات الأردنية")

app_mode = st.sidebar.radio("التنقل:", ["تتبع ومسارات الباصات", "اقتراح خط جديد", "بوابة الإدارة"])
hubs_dict = get_all_hubs()

# 1. شاشة استعراض ومحاكاة تتبع حركة الباص
if app_mode == "تتبع ومسارات الباصات":
    st.title("🗺️ استعراض وتتبع مسار الباص مباشرة")
    
    routes = get_routes('approved')
    if not routes:
        st.info("لا توجد مسارات مسجلة حالياً. يمكنك اقتراح مسار جديد من القائمة الجانبية.")
    else:
        col_sel, col_stat = st.columns([2, 1])
        with col_sel:
            r_names = {f"{r['route_name']} ({r['university']})": r for r in routes}
            chosen_label = st.selectbox("اختر المسار للاستعراض والتتبع:", list(r_names.keys()))
            cur_route = r_names[chosen_label]
        
        with col_stat:
            st.metric("الأجرة المعتمدة", f"{cur_route['fare']:.2f} د.أ")
            st.metric("المسافة التقديرية", f"{cur_route['distance_km']} كم")
            st.caption(f"⏱️ زمن الرحلة التقريبي: {cur_route['duration_min']} دقيقة")
            if cur_route.get('notes'):
                st.info(f"ملاحظات: {cur_route['notes']}")

        coords = json.loads(cur_route['coordinates'])
        
        c_sim1, c_sim2 = st.columns([1, 3])
        with c_sim1:
            run_tracking = st.button("🚍 محاكاة تتبع حركة الباص من الانطلاق")
        
        map_placeholder = st.empty()
        
        def render_bus_map(bus_position_idx=0):
            m = folium.Map(location=coords[0], zoom_start=12, tiles="CartoDB positron")
            folium.PolyLine(coords, color="#2A75D3", weight=5, opacity=0.8).add_to(m)
            folium.Marker(coords[0], tooltip="نقطة الانطلاق", icon=folium.Icon(color="green", icon="play")).add_to(m)
            folium.Marker(coords[-1], tooltip=cur_route['university'], icon=folium.Icon(color="red", icon="flag")).add_to(m)
            
            bus_loc = coords[bus_position_idx]
            folium.Marker(
                bus_loc,
                tooltip="موقع الباص الحالي",
                icon=folium.Icon(color="orange", icon="bus", prefix="fa")
            ).add_to(m)
            return m

        if run_tracking:
            prog_bar = st.progress(0)
            status_text = st.empty()
            step_stride = max(1, len(coords) // 10)
            sim_points = list(range(0, len(coords), step_stride))
            if sim_points[-1] != len(coords) - 1:
                sim_points.append(len(coords) - 1)
            
            for idx, pt_idx in enumerate(sim_points):
                pct = int(((idx + 1) / len(sim_points)) * 100)
                prog_bar.progress(pct)
                rem_mins = max(0.0, round(cur_route['duration_min'] * (1 - (pct / 100.0)), 1))
                status_text.markdown(f"**حالة الرحلة:** الباص في الطريق 🚍 | المسار المنجز: **{pct}%** | الزمن المتبقي للوصول: **{rem_mins} دقيقة**")
                with map_placeholder.container():
                    st_folium(render_bus_map(pt_idx), width=900, height=450, key=f"sim_{pt_idx}")
                time.sleep(0.6)
            st.success("🏁 وصل الباص إلى المحطة النهائية بنجاح!")
        else:
            with map_placeholder.container():
                st_folium(render_bus_map(0), width=900, height=450, key="static_map")

# 2. شاشة اقتراح خط جديد
elif app_mode == "اقتراح خط جديد":
    st.title("➕ اقتراح مسار باص جديد")
    st.write("يمكنك تحديد المحطات من القوائم الجاهزة، أو **النقر المباشر على الخريطة لتثبيت دبوس البداية والنهاية** بدقة.")

    if "user_start_pin" not in st.session_state:
        st.session_state.user_start_pin = None
    if "user_end_pin" not in st.session_state:
        st.session_state.user_end_pin = None

    c_mode1, c_mode2 = st.columns(2)
    with c_mode1:
        input_type = st.radio("طريقة تحديد المواقع:", ["اختيار محطات ومجمعات جاهزة", "تثبيت الدبوس يدوياً على الخريطة"], horizontal=True)

    s_lat, s_lon, e_lat, e_lon = None, None, None, None

    if input_type == "تثبيت الدبوس يدوياً على الخريطة":
        st.caption("👇 انقر على الخريطة لتثبيت الدبوس الأخضر (بداية)، ثم انقر مرة أخرى لتثبيت الدبوس الأحمر (وجهة):")
        
        pin_map = folium.Map(location=[32.2, 35.9], zoom_start=9, tiles="CartoDB positron")
        if st.session_state.user_start_pin:
            folium.Marker(st.session_state.user_start_pin, tooltip="نقطة البداية المحددة", icon=folium.Icon(color="green")).add_to(pin_map)
        if st.session_state.user_end_pin:
            folium.Marker(st.session_state.user_end_pin, tooltip="نقطة الوصول المحددة", icon=folium.Icon(color="red")).add_to(pin_map)
        
        map_clicks = st_folium(pin_map, width=900, height=350, key="click_map_suggest")
        
        if map_clicks and map_clicks.get("last_clicked"):
            click_pt = [map_clicks["last_clicked"]["lat"], map_clicks["last_clicked"]["lng"]]
            if not st.session_state.user_start_pin:
                st.session_state.user_start_pin = click_pt
                st.rerun()
            elif not st.session_state.user_end_pin:
                st.session_state.user_end_pin = click_pt
                st.rerun()

        col_rst, col_pins = st.columns([1, 3])
        with col_rst:
            if st.button("🔄 إعادة ضبط وتعديل الدبابيس"):
                st.session_state.user_start_pin = None
                st.session_state.user_end_pin = None
                st.rerun()
        with col_pins:
            if st.session_state.user_start_pin:
                st.success("🟢 تم تثبيت دبوس نقطة الانطلاق.")
            if st.session_state.user_end_pin:
                st.success("🔴 تم تثبيت دبوس نقطة الوصول.")

    with st.form("suggest_form"):
        c1, c2 = st.columns(2)
        with c1:
            name = st.text_input("اسم الخط (مثال: مجمع الأغوار - جامعة اليرموك):")
            uni = st.selectbox("الجامعة الوجهة:", UNIVERSITIES)
        with c2:
            suggested_fare = st.number_input("الأجرة المتوقعة (د.أ):", min_value=0.10, value=0.50, step=0.05, format="%.2f")
            notes = st.text_area("أماكن التوقف أو ملاحظات:")

        if input_type == "اختيار محطات ومجمعات جاهزة":
            hub_list = list(hubs_dict.keys())
            cc1, cc2 = st.columns(2)
            with cc1:
                start_hub = st.selectbox("نقطة الانطلاق:", hub_list, index=0)
            with cc2:
                end_hub = st.selectbox("نقطة الوصول:", hub_list, index=min(2, len(hub_list)-1))

        submitted = st.form_submit_button("إرسال المقترح للإدارة")
        if submitted:
            if input_type == "اختيار محطات ومجمعات جاهزة":
                if start_hub == end_hub:
                    st.error("نقطة الانطلاق والوصول متطابقتان، اختر نقطتين مختلفتين.")
                else:
                    s_lat, s_lon = hubs_dict[start_hub]
                    e_lat, e_lon = hubs_dict[end_hub]
            else:
                if not st.session_state.user_start_pin or not st.session_state.user_end_pin:
                    st.error("يرجى النقر على الخريطة لتثبيت دبوس البداية ودبوس النهاية.")
                else:
                    s_lat, s_lon = st.session_state.user_start_pin
                    e_lat, e_lon = st.session_state.user_end_pin

            if s_lat and e_lat:
                if not name:
                    st.warning("يرجى إدخال اسم المسار.")
                elif not is_within_jordan(s_lat, s_lon) or not is_within_jordan(e_lat, e_lon):
                    st.error("❌ النقاط المحددة تقع خارج حدود الأردن.")
                else:
                    dist, dur, pts = fetch_osrm_route(s_lat, s_lon, e_lat, e_lon)
                    add_route(name, uni, suggested_fare, dist, dur, json.dumps(pts), notes, status='pending')
                    st.session_state.user_start_pin = None
                    st.session_state.user_end_pin = None
                    st.success("✅ تم إرسال المقترح بنجاح لمراجعة واعتماد الإدارة.")

# 3. بوابة الإدارة والتحكم
elif app_mode == "بوابة الإدارة":
    st.title("🔒 بوابة الإدارة والتحكم")
    
    if "admin_logged_in" not in st.session_state:
        st.session_state.admin_logged_in = False
        st.session_state.admin_role = None

    if not st.session_state.admin_logged_in:
        role_choice = st.radio("رتبة الدخول:", ["مساعد آدمن (Assistant)", "الآدمن الرئيسي (Master Admin)"], horizontal=True)
        pwd = st.text_input("كلمة المرور الإدارية:", type="password")
        
        if st.button("تسجيل الدخول"):
            master_secret = st.secrets.get("ADMIN_PASSWORD", "Desert#94-Galaxy!Amman_82")
            asst_secret = st.secrets.get("ASST_PASSWORD", "Asst@Sha3tala#2026")
            
            if role_choice == "الآدمن الرئيسي (Master Admin)":
                if pwd == master_secret:
                    st.session_state.admin_logged_in = True
                    st.session_state.admin_role = "master"
                    st.rerun()
                else:
                    st.error("كلمة مرور الآدمن الرئيسي غير صحيحة.")
            elif role_choice == "مساعد آدمن (Assistant)":
                if pwd == asst_secret:
                    st.session_state.admin_logged_in = True
                    st.session_state.admin_role = "assistant"
                    st.rerun()
                else:
                    st.error("كلمة مرور المساعد غير صحيحة.")
    else:
        st.sidebar.success(f"مرحباً بك ({'الآدمن الرئيسي' if st.session_state.admin_role == 'master' else 'مساعد'})")
        if st.sidebar.button("تسجيل الخروج"):
            st.session_state.admin_logged_in = False
            st.session_state.admin_role = None
            st.rerun()

        tabs_list = [
            "🛠️ تعديل أسعار وإدارة المسارات", 
            "⏳ تدقيق الطلبات الجديدة", 
            "➕ إضافة مسار مباشر"
        ]
        if st.session_state.admin_role == "master":
            tabs_list.append("📍 إدارة المجمعات والإحداثيات")

        tabs = st.tabs(tabs_list)

        with tabs[0]:
            st.subheader("تعديل الأجرة والبيانات أو إزالة المسار")
            routes = get_routes('approved')
            if routes:
                r_map = {f"#{r['id']} - {r['route_name']} (السعر الحالي: {r['fare']:.2f} د.أ)": r for r in routes}
                sel_lbl = st.selectbox("اختر المسار للتعديل أو الحذف:", list(r_map.keys()))
                sel_r = r_map[sel_lbl]
                
                with st.form(f"edit_form_{sel_r['id']}"):
                    col1, col2 = st.columns(2)
                    with col1:
                        up_name = st.text_input("اسم المسار:", value=sel_r['route_name'])
                        up_fare = st.number_input(
                            "الأجرة المعتمدة (د.أ):", 
                            min_value=0.10, 
                            max_value=20.0, 
                            value=float(sel_r['fare']), 
                            step=0.05, 
                            format="%.2f"
                        )
                    with col2:
                        up_notes = st.text_area("ملاحظات / تردد الخط:", value=sel_r.get('notes') or "")
                    
                    btn_save = st.form_submit_button("💾 حفظ التعديلات وتحديث السعر")
                    if btn_save:
                        update_route_fare_and_details(sel_r['id'], up_fare, up_name, up_notes)
                        st.success("✅ تم تحديث بيانات المسار والسعر بنجاح!")
                        st.rerun()
                
                st.divider()
                st.subheader("⚠️ خيار إزالة المسار نهائياً")
                c_del1, c_del2 = st.columns([1, 2])
                with c_del1:
                    confirm_del = st.checkbox("تأكيد حذف المسار نهائياً", key=f"c_del_{sel_r['id']}")
                with c_del2:
                    if st.button("🗑️ إزالة المسار نهائياً من المنظومة", key=f"btn_del_{sel_r['id']}", type="primary"):
                        if confirm_del:
                            delete_route(sel_r['id'])
                            st.success(f"✅ تم حذف مسار '{sel_r['route_name']}' نهائياً.")
                            st.rerun()
                        else:
                            st.warning("يرجى تفعيل علامة التأكيد أولاً قبل الضغط على الحذف.")
            else:
                st.info("لا توجد خطوط معتمدة حالياً.")

        with tabs[1]:
            st.subheader("الطلبات المقترحة من الطلاب بانتظار الاعتماد")
            pending = get_routes('pending')
            if not pending:
                st.success("لا توجد طلبات معلقة حالياً.")
            else:
                for req in pending:
                    with st.expander(f"طلب: {req['route_name']} - الوجهة: {req['university']}"):
                        st.write(f"المسافة: {req['distance_km']} كم | الزمن المقدر: {req['duration_min']} دقيقة")
                        st.write(f"ملاحظات: {req.get('notes') or 'لا يوجد'}")
                        
                        app_fare = st.number_input(
                            f"تحديد السعر المعتمد النهائي (د.أ) لطلب #{req['id']}:", 
                            min_value=0.10, 
                            value=float(req['fare']), 
                            step=0.05, 
                            format="%.2f", 
                            key=f"p_fare_{req['id']}"
                        )
                        
                        col_a, col_r = st.columns(2)
                        with col_a:
                            if st.button("✅ اعتماد الخط فوراً", key=f"acc_{req['id']}"):
                                set_route_status(req['id'], 'approved', app_fare)
                                st.success("تم اعتماد الخط بالسعر المحدد!")
                                st.rerun()
                        with col_r:
                            if st.button("❌ رفض وحذف الطلب", key=f"rej_{req['id']}"):
                                delete_route(req['id'])
                                st.warning("تم رفض المقترح.")
                                st.rerun()

        with tabs[2]:
            st.subheader("إضافة مسار معتمد مباشرة")
            with st.form("admin_add_route"):
                a_name = st.text_input("اسم الخط:")
                a_uni = st.selectbox("الجامعة:", UNIVERSITIES)
                a_fare = st.number_input("السعر المعتمد (د.أ):", min_value=0.10, value=0.60, step=0.05, format="%.2f")
                a_notes = st.text_area("تفاصيل وملاحظات إضافية:")
                
                h_list = list(hubs_dict.keys())
                ac1, ac2 = st.columns(2)
                with ac1:
                    a_start = st.selectbox("نقطة الانطلاق المعتمدة:", h_list, index=0)
                with ac2:
                    a_end = st.selectbox("نقطة الوصول المعتمدة:", h_list, index=min(1, len(h_list)-1))
                
                if st.form_submit_button("إضافة الخط فوراً إلى الخدمة"):
                    if a_name and a_start != a_end:
                        al1, on1 = hubs_dict[a_start]
                        al2, on2 = hubs_dict[a_end]
                        dist, dur, pts = fetch_osrm_route(al1, on1, al2, on2)
                        add_route(a_name, a_uni, a_fare, dist, dur, json.dumps(pts), a_notes, status='approved')
                        st.success("✅ تمت إضافة المسار بنجاح إلى شبكة الخطوط!")
                        st.rerun()
                    else:
                        st.error("يرجى كتابة اسم الخط واختيار محطتين مختلفتين.")

        if st.session_state.admin_role == "master":
            with tabs[3]:
                st.subheader("📍 إدارة إحداثيات ومواقع المجمعات والجامعات")
                st.caption("خاص بالمشرف الرئيسي: تعديل خطوط الطول والعرض أو إضافة نقاط ومجمعات جديدة إلى النظام.")
                
                c_edit_hub, c_new_hub = st.columns(2)
                with c_edit_hub:
                    st.markdown("#### ✏️ تعديل إحداثيات محطة قائمة")
                    selected_hub_to_edit = st.selectbox("اختر المحطة أو المجمع:", list(hubs_dict.keys()))
                    curr_lat, curr_lon = hubs_dict[selected_hub_to_edit]
                    
                    with st.form("edit_hub_coords_form"):
                        new_lat = st.number_input("خط العرض (Latitude):", value=curr_lat, format="%.6f")
                        new_lon = st.number_input("خط الطول (Longitude):", value=curr_lon, format="%.6f")
                        
                        btn_update_hub = st.form_submit_button("💾 تحديث الإحداثيات")
                        if btn_update_hub:
                            if is_within_jordan(new_lat, new_lon):
                                upsert_hub(selected_hub_to_edit, new_lat, new_lon)
                                st.success(f"✅ تم تحديث إحداثيات '{selected_hub_to_edit}' بنجاح!")
                                st.rerun()
                            else:
                                st.error("❌ الإحداثيات المدخلة تقع خارج حدود المملكة الأردنية الهاشمية.")

                with c_new_hub:
                    st.markdown("#### ➕ إضافة مجمع / محطة جديدة للنظام")
                    with st.form("add_new_hub_form"):
                        new_hub_name = st.text_input("اسم المجمع أو النقطة (مثال: دوار الثقافة - إربد):")
                        add_lat = st.number_input("خط العرض:", value=32.5500, format="%.6f")
                        add_lon = st.number_input("خط الطول:", value=35.8500, format="%.6f")
                        
                        btn_add_hub = st.form_submit_button("➕ حفظ وإضافة النقطة للقائمة")
                        if btn_add_hub:
                            if not new_hub_name:
                                st.warning("يرجى كتابة اسم النقطة أو المجمع.")
                            elif not is_within_jordan(add_lat, add_lon):
                                st.error("❌ الإحداثيات المدخلة خارج حدود الأردن.")
                            else:
                                upsert_hub(new_hub_name, add_lat, add_lon)
                                st.success(f"✅ تمت إضافة '{new_hub_name}' بنجاح وأصبحت متاحة فوراً لجميع المستخدمين.")
                                st.rerun()
