import streamlit as st
import requests
import pandas as pd
from datetime import datetime, timedelta
import io
import time

import json
import os

st.set_page_config(page_title="Wildberries Отчёт", page_icon="📦", layout="wide")
st.markdown("<style>.block-container{padding-top:1.5rem;}</style>", unsafe_allow_html=True)

FBO_FILE = "/tmp/wb_fbo_data.json"

def load_fbo_all():
    """Барлық FBO деректерін файлдан оқу"""
    try:
        if os.path.exists(FBO_FILE):
            with open(FBO_FILE, "r", encoding="utf-8") as f:
                return json.load(f)
    except:
        pass
    return {}

def save_fbo_all(data):
    """Барлық FBO деректерін файлға сақтау"""
    try:
        with open(FBO_FILE, "w", encoding="utf-8") as f:
            json.dump(data, f, ensure_ascii=False)
    except Exception as e:
        st.warning(f"FBO сақталмады: {e}")

# ──────────────────────────────────────────────
# ШЫҒ / КІРУ
# ──────────────────────────────────────────────
def check_password():
    if st.session_state.get("role"):
        return True
    st.title("🔐 Wildberries Отчёт")
    st.markdown("---")
    _, col, _ = st.columns([1, 1.5, 1])
    with col:
        st.markdown("### Вход")
        pwd = st.text_input("Пароль", type="password", placeholder="••••••••")
        if st.button("Войти →", use_container_width=True):
            manager_pwd = st.secrets.get("MANAGER_PASSWORD", "")
            # Менеджер паролін тексеру
            if manager_pwd and pwd == manager_pwd:
                st.session_state.role = "manager"
                st.session_state.store_access = None  # барлығына рұқсат
                st.rerun()
                return True
            # Магазин иесі паролін тексеру
            stores_str = st.secrets.get("STORE_NAMES", "")
            store_names = [n.strip() for n in stores_str.split(",") if n.strip()]
            for i, name in enumerate(store_names, 1):
                owner_pwd = st.secrets.get(f"STORE_{i}_PASSWORD", "")
                if owner_pwd and pwd == owner_pwd:
                    st.session_state.role = "owner"
                    st.session_state.store_access = i
                    st.rerun()
                    return True
            st.error("Неверный пароль!")
    return False

if not check_password():
    st.stop()

# ──────────────────────────────────────────────
# МАГАЗИН ТІЗІМІ
# ──────────────────────────────────────────────
def get_stores():
    """Secrets-тен магазин тізімін алу"""
    names_str = st.secrets.get("STORE_NAMES", "")
    if not names_str:
        return []
    names = [n.strip() for n in names_str.split(",") if n.strip()]
    stores = []
    for i, name in enumerate(names, 1):
        stats_key = st.secrets.get(f"STORE_{i}_STATS", "")
        analytics_key = st.secrets.get(f"STORE_{i}_ANALYTICS", "")
        if stats_key:
            stores.append({
                "name": name,
                "idx": i,
                "stats_key": stats_key,
                "analytics_key": analytics_key,
            })
    return stores

# ──────────────────────────────────────────────
# API ФУНКЦИЯЛАРЫ
# ──────────────────────────────────────────────
def days_ago_str(n):
    return (datetime.now() - timedelta(days=n)).strftime("%Y-%m-%dT00:00:00")

def wb_get_retry(url, key, params={}, max_retries=3, store_name=""):
    for attempt in range(max_retries):
        r = requests.get(url, headers={"Authorization": key}, params=params, timeout=60)
        if r.status_code == 429:
            st.info(f"⏳ [{store_name}] WB API лимит — 65 сек ({attempt+1}/{max_retries})...")
            time.sleep(65)
            continue
        r.raise_for_status()
        return r.json()
    raise Exception("Лимит запросов превышен.")

def fetch_warehouse_remains(analytics_key):
    base = "https://seller-analytics-api.wildberries.ru"
    r = requests.get(f"{base}/api/v1/warehouse_remains",
                     headers={"Authorization": analytics_key},
                     params={"groupBySa": "true"}, timeout=30)
    r.raise_for_status()
    task_id = r.json()["data"]["taskId"]
    for _ in range(24):
        time.sleep(5)
        r2 = requests.get(f"{base}/api/v1/warehouse_remains/tasks/{task_id}/status",
                          headers={"Authorization": analytics_key}, timeout=30)
        r2.raise_for_status()
        status = r2.json()["data"]["status"]
        if status == "done":
            break
        elif status in ["failed", "error"]:
            raise Exception(f"Задание завершилось с ошибкой: {status}")
    r3 = requests.get(f"{base}/api/v1/warehouse_remains/tasks/{task_id}/download",
                      headers={"Authorization": analytics_key}, timeout=60)
    r3.raise_for_status()
    return r3.json()

def parse_remains(data):
    rows = []
    for item in data:
        sa = item.get("vendorCode", "")
        in_way_to_client = 0
        in_way_return = 0
        total_stock = 0
        for wh in item.get("warehouses", []):
            name = wh.get("warehouseName", "")
            qty = wh.get("quantity", 0)
            if name == "В пути до получателей":
                in_way_to_client = qty
            elif name == "В пути возвраты на склад WB":
                in_way_return = qty
            elif name == "Всего находится на складах":
                total_stock = qty
        rows.append({
            "supplierArticle": sa,
            "qty": total_stock,
            "in_way_client": in_way_to_client + in_way_return,
            "in_way_return": in_way_return,
        })
    return pd.DataFrame(rows)

def status_label(q):
    if q == 0:    return "Ноль"
    if q <= 200:  return "Мало"
    if q <= 500:  return "Хорошо"
    return "Достаточно"

# ──────────────────────────────────────────────
# БІР МАГАЗИН ДЕРЕКТЕРІН ЖҮКТЕУ
# ──────────────────────────────────────────────
def load_store_data(store):
    idx = store["idx"]
    stats_key = store["stats_key"]
    analytics_key = store["analytics_key"]
    name = store["name"]
    errors = []

    # 1. Остатки
    if analytics_key:
        try:
            with st.spinner(f"[{name}] Остатки и FBO жүктелуде (до 2 мин)..."):
                remains_data = fetch_warehouse_remains(analytics_key)
                agg = parse_remains(remains_data)
        except Exception as e:
            errors.append(f"Остатки (Аналитика): {e}")
            agg = pd.DataFrame()
    else:
        try:
            with st.spinner(f"[{name}] Остатки жүктелуде..."):
                stocks_raw = wb_get_retry(
                    "https://statistics-api.wildberries.ru/api/v1/supplier/stocks",
                    stats_key, {"dateFrom": "2019-01-01"}, store_name=name
                )
                s_df = pd.DataFrame(stocks_raw)
                agg = s_df.groupby("supplierArticle").agg(
                    qty=("quantity", "sum"),
                    in_way_client=("inWayToClient", "sum"),
                ).reset_index()
                agg["in_way_return"] = 0
        except Exception as e:
            errors.append(f"Остатки: {e}")
            agg = pd.DataFrame()

    time.sleep(3)

    # 2. Продажи
    try:
        with st.spinner(f"[{name}] Продажи жүктелуде..."):
            sales_raw = wb_get_retry(
                "https://statistics-api.wildberries.ru/api/v1/supplier/sales",
                stats_key, {"dateFrom": days_ago_str(20), "flag": 0}, store_name=name
            )
            sales_df = pd.DataFrame(sales_raw) if sales_raw else pd.DataFrame()
            if not sales_df.empty and "supplierArticle" in sales_df.columns:
                if "saleID" in sales_df.columns:
                    sales_df = sales_df[~sales_df["saleID"].astype(str).str.startswith("R")]
                active = set(sales_df["supplierArticle"].unique())
                cutoff7 = datetime.now() - timedelta(days=7)
                if "date" in sales_df.columns:
                    sales_df["date"] = pd.to_datetime(sales_df["date"], errors="coerce")
                    s7 = sales_df[sales_df["date"] >= cutoff7]
                else:
                    s7 = sales_df
                daily = s7.groupby("supplierArticle").size().div(7).reset_index()
                daily.columns = ["supplierArticle", "daily_avg"]
            else:
                active = set()
                daily = pd.DataFrame(columns=["supplierArticle", "daily_avg"])
    except Exception as e:
        errors.append(f"Продажи: {e}")
        active = set()
        daily = pd.DataFrame(columns=["supplierArticle", "daily_avg"])

    # 3. Продажи за 30 дней (аналитика үшін)
    sales30 = pd.DataFrame()
    try:
        with st.spinner(f"[{name}] Продажи 30 дней..."):
            # Заказдар (клиент жасаған) — 90 күн сұраймыз
            s30_raw = wb_get_retry(
                "https://statistics-api.wildberries.ru/api/v1/supplier/orders",
                stats_key, {"dateFrom": days_ago_str(90), "flag": 0}, store_name=name
            )
            s30_df = pd.DataFrame(s30_raw) if s30_raw else pd.DataFrame()
            if not s30_df.empty and "date" in s30_df.columns:
                s30_df["date"] = pd.to_datetime(s30_df["date"], errors="coerce")
                cutoff30 = pd.Timestamp.now() - pd.Timedelta(days=30)
                # Сырой деректерді debug үшін сақтаймыз
                st.session_state[f"raw_orders_{idx}"] = s30_df.copy()
                # date бойынша сүземіз
                s30_df = s30_df[s30_df["date"] >= cutoff30]
                s30_df["date_only"] = s30_df["date"].dt.date
                s30_df["priceWithDisc"] = pd.to_numeric(
                    s30_df.get("priceWithDisc", 0), errors="coerce").fillna(0)
                # Барлық заказдар (отмена қосқанда) — WB кабинетімен сай келу үшін
                sales30 = s30_df.groupby("date_only").agg(
                    qty_all=("date_only", "count"),
                    qty_active=("isCancel", lambda x: (x == False).sum() if "isCancel" in s30_df.columns else len(x)),
                    revenue=("priceWithDisc", "sum")
                ).reset_index()
                sales30.columns = ["Дата", "Барлық заказ (шт)", "Белсенді заказ (шт)", "Выручка (₸)"]
                sales30 = sales30.sort_values("Дата")
    except Exception as e:
        errors.append(f"Аналитика: {e}")

    # 4. Итог
    df = pd.DataFrame()
    if not agg.empty:
        df = agg.merge(daily, on="supplierArticle", how="left")
        df["daily_avg"] = df["daily_avg"].fillna(0).round(1)
        if "in_way_return" not in df.columns:
            df["in_way_return"] = 0
        df["total"] = df["qty"] + df["in_way_client"]
        df["turnover"] = df.apply(
            lambda r: round((r["qty"] + r["in_way_return"]) / r["daily_avg"])
            if r["daily_avg"] > 0 else None, axis=1
        )
        df["status"] = df["qty"].apply(status_label)
        if active:
            df = df[df["supplierArticle"].isin(active)]
        df = df.reset_index(drop=True)

    return df, sales30, errors

# ──────────────────────────────────────────────
# БІР МАГАЗИН КЕСТЕСІН КӨРСЕТУ
# ──────────────────────────────────────────────
def show_store(store, df, sales30, filter_status, search):
    idx = store["idx"]

    if df.empty:
        st.warning("Деректер жоқ немесе жүктелмеді")
        return

    tab_ostatok, tab_analytic = st.tabs(["📦 Остатки", "📊 Аналитика — 30 күн"])

    with tab_analytic:
        if sales30 is None or sales30.empty:
            st.info("Продажа деректері жоқ")
        else:
            total_qty = int(sales30["Барлық заказ (шт)"].sum())
            total_rev = sales30["Выручка (₸)"].sum()
            avg_day = total_qty / 30
            best_day = sales30.loc[sales30["Барлық заказ (шт)"].idxmax()]

            c1, c2, c3, c4 = st.columns(4)
            c1.metric("📦 Жалпы заказ", f"{total_qty:,} шт".replace(",", " "))
            c2.metric("💰 Жалпы выручка", f"{total_rev:,.0f} ₸".replace(",", " "))
            c3.metric("📈 Күндік орта", f"{avg_day:.1f} шт")
            c4.metric("🏆 Ең жақсы күн", f"{best_day['Дата']} — {best_day['Барлық заказ (шт)']} шт")

            st.divider()
            st.markdown("#### 📊 Күн бойынша заказдар (соңғы 30 күн)")

            chart_df = sales30.set_index("Дата")[["Барлық заказ (шт)"]]
            st.bar_chart(chart_df, height=350)

            st.markdown("#### 💰 Күн бойынша выручка (соңғы 30 күн)")
            rev_df = sales30.set_index("Дата")[["Выручка (₸)"]]
            st.line_chart(rev_df, height=250)

            st.divider()
            st.markdown("#### 📋 Күнделікті кесте")
            display30 = sales30.copy()
            display30["Выручка (₸)"] = display30["Выручка (₸)"].round(0).astype(int)
            st.dataframe(
                display30.sort_values("Дата", ascending=False).reset_index(drop=True),
                use_container_width=True,
                height=400,
                column_config={
                    "Заказ (шт)": st.column_config.NumberColumn(format="%d шт"),
                    "Выручка (₸)": st.column_config.NumberColumn(format="%d ₸"),
                }
            )

    with tab_ostatok:

        # Фильтр
        dff = df.copy()
        if filter_status == "Ноль":
            dff = dff[dff["qty"] == 0]
        elif filter_status == "Мало (1–200)":
            dff = dff[(dff["qty"] >= 1) & (dff["qty"] <= 200)]
        elif filter_status == "Хорошо (201–500)":
            dff = dff[(dff["qty"] > 200) & (dff["qty"] <= 500)]
        elif filter_status == "Достаточно (500+)":
            dff = dff[dff["qty"] > 500]
        if search:
            dff = dff[dff["supplierArticle"].astype(str).str.contains(search, case=False, na=False)]

        # Метрики
        c1, c2, c3, c4 = st.columns(4)
        c1.metric("📦 Общий остаток", f"{int(df['total'].sum()):,} шт".replace(",", " "))
        c2.metric("📊 Позиций", len(df))
        c3.metric("🔴 Критические", int(df["turnover"].dropna().apply(lambda x: x <= 10).sum()),
                  help="Оборачиваемость ≤ 10 дней")
        c4.metric("⚫ Ноль остаток", int((df["qty"] == 0).sum()))

        st.divider()
        st.markdown("#### 📊 Итоговый отчёт")

        # FBO деректерін файлдан оқу (барлық пайдаланушы үшін ортақ)
        fbo_key = f"fbo_{idx}"
        all_fbo = load_fbo_all()
        fbo_data = all_fbo.get(str(idx), {})
        st.session_state[fbo_key] = fbo_data

        result = dff[["supplierArticle", "qty", "in_way_client", "in_way_return", "daily_avg", "status"]].copy()
        result["FBO в пути"] = result["supplierArticle"].map(fbo_data).fillna(0).astype(int)
        result["Общий остаток"] = result["qty"] + result["in_way_client"] + result["FBO в пути"]
        result["Оборачиваемость"] = result.apply(
            lambda r: round((r["qty"] + r["in_way_return"]) / r["daily_avg"])
            if r["daily_avg"] > 0 else None, axis=1
        )
        result = result.rename(columns={
            "supplierArticle": "Артикул",
            "qty": "Остаток",
            "in_way_client": "В пути к клиенту",
            "daily_avg": "Ср. продаж/день",
            "status": "Статус",
        })
        result = result[["Артикул", "Остаток", "В пути к клиенту", "FBO в пути",
                         "Общий остаток", "Ср. продаж/день", "Оборачиваемость", "Статус"]]

        def style_turn(val):
            if pd.isna(val): return ""
            return "background-color:#FCEBEB;color:#A32D2D;font-weight:bold" if val <= 10 else ""

        def style_stat(val):
            m = {
                "Ноль":       "background-color:#FCEBEB;color:#A32D2D",
                "Мало":       "background-color:#FAEEDA;color:#854F0B",
                "Хорошо":     "background-color:#EAF3DE;color:#3B6D11",
                "Достаточно": "background-color:#E6F1FB;color:#185FA5",
            }
            return m.get(val, "")

        styled = result.style.map(style_turn, subset=["Оборачиваемость"]).map(style_stat, subset=["Статус"])

        st.dataframe(styled, use_container_width=True, height=460,
            column_config={
                "Остаток":          st.column_config.NumberColumn(format="%d шт"),
                "В пути к клиенту": st.column_config.NumberColumn(format="%d шт"),
                "FBO в пути":       st.column_config.NumberColumn(format="%d шт"),
                "Общий остаток":    st.column_config.NumberColumn(format="%d шт"),
                "Ср. продаж/день":  st.column_config.NumberColumn(format="%.1f"),
                "Оборачиваемость":  st.column_config.NumberColumn(format="%d дн"),
            }
        )
        st.caption(f"Показано: {len(result)} позиций (с продажами за последние 20 дней)")

        buf = io.BytesIO()
        with pd.ExcelWriter(buf, engine="openpyxl") as writer:
            result.to_excel(writer, index=False, sheet_name="Отчёт WB")
        st.download_button(f"⬇️ Excel жүктеу — {store['name']}", data=buf.getvalue(),
            file_name=f"WB_{store['name']}_{datetime.now().strftime('%Y%m%d_%H%M')}.xlsx",
            mime="application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
            key=f"dl_{idx}")

        # FBO енгізу — тек менеджерге көрінеді
        role = st.session_state.get("role", "manager")
        if role == "manager":
            st.divider()
            st.markdown("#### ✏️ FBO в пути — санды енгізіңіз")
            st.caption("Складқа баратын поставка данасын жазыңыз — магазин иесі де FBO санын көреді")

            fbo_tbl = dff[["supplierArticle"]].copy()
            fbo_tbl.columns = ["Артикул"]
            fbo_tbl["FBO в пути"] = fbo_tbl["Артикул"].map(fbo_data).fillna(0).astype(int)

            fbo_edited = st.data_editor(
                fbo_tbl, use_container_width=True, height=400, key=f"fbo_editor_{idx}",
                column_config={
                    "Артикул":    st.column_config.TextColumn(disabled=True),
                    "FBO в пути": st.column_config.NumberColumn(format="%d шт", min_value=0),
                }
            )
            new_fbo = dict(zip(fbo_edited["Артикул"], fbo_edited["FBO в пути"]))
            if new_fbo != fbo_data:
                # Файлға сақтау
                all_fbo = load_fbo_all()
                all_fbo[str(idx)] = new_fbo
                save_fbo_all(all_fbo)
                st.session_state[fbo_key] = new_fbo
                st.rerun()

# ──────────────────────────────────────────────
# НЕГІЗГІ ИНТЕРФЕЙС
# ──────────────────────────────────────────────
stores = get_stores()

# Рольге байланысты магазин тізімін сүзу (sidebar алдында)
_role = st.session_state.get("role", "manager")
_store_access = st.session_state.get("store_access", None)
if _role == "owner" and _store_access:
    visible_stores = [s for s in stores if s["idx"] == _store_access]
else:
    visible_stores = stores

with st.sidebar:
    st.header("⚙️ Настройки")

    if not visible_stores:
        st.warning("Магазин жоқ!")
    else:
        for s in visible_stores:
            has_analytics = "✅" if s["analytics_key"] else "⚠️"
            st.markdown(f"**{s['name']}** {has_analytics}")

    fetch_btn = st.button("🔄 Барлығын жүктеу", use_container_width=True)
    st.divider()
    st.markdown("**Фильтрлер**")
    filter_status = st.selectbox("Статус остатка", [
        "Все", "Ноль", "Мало (1–200)", "Хорошо (201–500)", "Достаточно (500+)"
    ])
    search = st.text_input("🔍 Поиск по артикулу")
    st.markdown("""
    <div style='font-size:11px;color:#854F0B;background:#FAEEDA;padding:8px;border-radius:6px;margin-top:8px;'>
    🔴 Красная ячейка = оборачиваемость ≤ 10 дней
    </div>""", unsafe_allow_html=True)
    if st.button("🚪 Выйти"):
        st.session_state.role = None
        st.session_state.store_access = None
        st.rerun()

st.title("📦 Wildberries отчёт")
st.caption(f"Обновлено: {datetime.now().strftime('%d.%m.%Y %H:%M')}")

if not stores:
    st.info("👈 Secrets-ке магазин деректерін қосыңыз")
    st.stop()

# Деректерді жүктеу
if fetch_btn:
    for s in visible_stores:
        df, sales30, errors = load_store_data(s)
        st.session_state[f"df_{s['idx']}"] = df
        st.session_state[f"sales30_{s['idx']}"] = sales30
        for e in errors:
            st.warning(f"[{s['name']}] ⚠️ {e}")
    st.success("✅ Барлық магазин жүктелді!")
    st.rerun()

if not visible_stores:
    st.warning("Сізге қолжетімді магазин жоқ")
    st.stop()

# Табтар
tab_names = [s["name"] for s in visible_stores]
tabs = st.tabs(tab_names)

for i, (tab, store) in enumerate(zip(tabs, visible_stores)):
    with tab:
        df_key = f"df_{store['idx']}"
        if df_key not in st.session_state or st.session_state[df_key] is None:
            st.info(f"👈 **«Барлығын жүктеу»** батырмасын басыңыз")
        else:
            sales30 = st.session_state.get(f"sales30_{store['idx']}", pd.DataFrame())
            show_store(store, st.session_state[df_key], sales30, filter_status, search)
