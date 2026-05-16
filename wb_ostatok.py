import streamlit as st
import requests
import pandas as pd
from datetime import datetime, timedelta
import io
import time

st.set_page_config(page_title="Wildberries Отчёт", page_icon="📦", layout="wide")

st.markdown("""
<style>
  .block-container { padding-top: 1.5rem; }
  .stDataFrame { border-radius: 8px; }
</style>
""", unsafe_allow_html=True)

def check_password():
    if st.session_state.get("authenticated"):
        return True
    st.title("🔐 Wildberries Отчёт")
    st.markdown("---")
    col1, col2, col3 = st.columns([1, 1.5, 1])
    with col2:
        st.markdown("### Вход")
        pwd = st.text_input("Пароль", type="password", placeholder="••••••••")
        if st.button("Войти →", use_container_width=True):
            if pwd == st.secrets.get("PASSWORD", "director2024"):
                st.session_state.authenticated = True
                st.rerun()
            else:
                st.error("Неверный пароль!")
    return False

if not check_password():
    st.stop()

def days_ago_str(n):
    return (datetime.now() - timedelta(days=n)).strftime("%Y-%m-%dT00:00:00")

def wb_get_retry(url, key, params={}, max_retries=3):
    for attempt in range(max_retries):
        r = requests.get(url, headers={"Authorization": key}, params=params, timeout=60)
        if r.status_code == 429:
            st.sidebar.info(f"⏳ WB API лимит — 65 сек күтілуде ({attempt+1}/{max_retries})...")
            time.sleep(65)
            continue
        r.raise_for_status()
        return r.json()
    raise Exception("Лимит запросов WB API превышен. Попробуйте через минуту.")

def status_label(q):
    if q == 0:    return "Ноль"
    if q <= 200:  return "Мало"
    if q <= 500:  return "Хорошо"
    return "Достаточно"

with st.sidebar:
    st.header("⚙️ Настройки")
    api_key = st.secrets.get("WB_API_KEY", "")
    if api_key:
        st.success("✅ API ключ подключён")
    else:
        api_key = st.text_input("API ключ", type="password", placeholder="eyJ...")
    fetch_btn = st.button("🔄 Обновить данные", use_container_width=True)
    st.divider()
    st.markdown("**Фильтры**")
    filter_status = st.selectbox("Статус остатка", [
        "Все", "Ноль", "Мало (1–200)", "Хорошо (201–500)", "Достаточно (500+)"
    ])
    search = st.text_input("🔍 Поиск по артикулу")
    st.markdown("""
    <div style='font-size:11px;color:#854F0B;background:#FAEEDA;padding:8px;border-radius:6px;margin-top:12px;'>
    🔴 Красная ячейка = оборачиваемость ≤ 10 дней
    </div>
    """, unsafe_allow_html=True)
    if st.button("🚪 Выйти"):
        st.session_state.authenticated = False
        st.rerun()

if "df" not in st.session_state:
    st.session_state.df = None

if fetch_btn:
    if not api_key:
        st.sidebar.error("Введите API ключ!")
    else:
        errors = []
        with st.spinner("Загружаем данные из Wildberries..."):

            # 1. Остатки
            try:
                stocks_raw = wb_get_retry(
                    "https://statistics-api.wildberries.ru/api/v1/supplier/stocks",
                    api_key, {"dateFrom": "2019-01-01"}
                )
                s_df = pd.DataFrame(stocks_raw)
                agg = s_df.groupby("supplierArticle").agg(
                    name=("subject", "first"),
                    qty=("quantity", "sum"),
                    in_way_client=("inWayToClient", "sum"),

                ).reset_index()
            except Exception as e:
                errors.append(f"Остатки: {e}")
                agg = pd.DataFrame()

            time.sleep(3)

            # 2. Продажи за 20 дней
            try:
                sales_raw = wb_get_retry(
                    "https://statistics-api.wildberries.ru/api/v1/supplier/sales",
                    api_key, {"dateFrom": days_ago_str(20), "flag": 0}
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

            # 3. FBO поставки в пути (incomes)
            fbo_transit = pd.DataFrame(columns=["supplierArticle", "fbo_way"])
            try:
                time.sleep(2)
                inc_raw = wb_get_retry(
                    "https://statistics-api.wildberries.ru/api/v1/supplier/incomes",
                    api_key, {"dateFrom": days_ago_str(90)}
                )
                inc_df = pd.DataFrame(inc_raw) if inc_raw else pd.DataFrame()
                if not inc_df.empty and "status" in inc_df.columns:
                    in_tr = inc_df[~inc_df["status"].isin(["Принято", "Отклонён", "Отклонен"])]
                    if not in_tr.empty and "supplierArticle" in in_tr.columns:
                        fbo_transit = in_tr.groupby("supplierArticle")["quantity"].sum().reset_index()
                        fbo_transit.columns = ["supplierArticle", "fbo_way"]
            except Exception as e:
                errors.append(f"FBO поставки: {e}")

            # 4. Итоговая таблица
            if not agg.empty:
                df = agg.copy()
                df = df.merge(daily, on="supplierArticle", how="left")
                df = df.merge(fbo_transit, on="supplierArticle", how="left")
                df["daily_avg"] = df["daily_avg"].fillna(0).round(1)
                df["fbo_way"] = df["fbo_way"].fillna(0).astype(int)
                df["total_qty"] = df["qty"] + df["in_way_client"] + df["fbo_way"]
                df["turnover"] = df.apply(
                    lambda r: round(r["total_qty"] / r["daily_avg"]) if r["daily_avg"] > 0 else None,
                    axis=1
                )
                df["status"] = df["qty"].apply(status_label)
                if active:
                    df = df[df["supplierArticle"].isin(active)]
                st.session_state.df = df
                st.sidebar.success(f"✅ Загружено {len(df)} позиций")

            for e in errors:
                st.sidebar.warning(f"⚠️ {e}")

# Заголовок
st.title("📦 Wildberries отчёт")
st.caption(f"Обновлено: {datetime.now().strftime('%d.%m.%Y %H:%M')}")

if st.session_state.df is not None:
    df_full = st.session_state.df
    df = df_full.copy()

    if filter_status == "Ноль":
        df = df[df["qty"] == 0]
    elif filter_status == "Мало (1–200)":
        df = df[(df["qty"] >= 1) & (df["qty"] <= 200)]
    elif filter_status == "Хорошо (201–500)":
        df = df[(df["qty"] > 200) & (df["qty"] <= 500)]
    elif filter_status == "Достаточно (500+)":
        df = df[df["qty"] > 500]
    if search:
        df = df[df["supplierArticle"].astype(str).str.contains(search, case=False, na=False)]

    # Метрики
    total_qty = int(df_full["total_qty"].sum())
    zero_count = int((df_full["qty"] == 0).sum())
    critical = int(df_full["turnover"].dropna().apply(lambda x: x <= 10).sum())
    positions = len(df_full)

    c1, c2, c3, c4 = st.columns(4)
    c1.metric("📦 Общий остаток", f"{total_qty:,} шт".replace(",", " "))
    c2.metric("📊 Всего позиций", f"{positions}")
    c3.metric("🔴 Критические", f"{critical}", help="Оборачиваемость ≤ 10 дней")
    c4.metric("⚫ Ноль остаток", f"{zero_count}")

    st.divider()

    show = df[[
        "supplierArticle", "qty", "in_way_client", "fbo_way",
        "total_qty", "daily_avg", "turnover", "status"
    ]].copy()
    show.columns = [
        "Артикул", "Остаток", "В пути к клиенту", "FBO в пути",
        "Общий остаток", "Ср. продаж/день", "Оборачиваемость", "Статус"
    ]

    def style_turnover(val):
        if pd.isna(val): return ""
        return "background-color: #FCEBEB; color: #A32D2D; font-weight: bold" if val <= 10 else ""

    def style_status(val):
        m = {
            "Ноль":       "background-color: #FCEBEB; color: #A32D2D",
            "Мало":       "background-color: #FAEEDA; color: #854F0B",
            "Хорошо":     "background-color: #EAF3DE; color: #3B6D11",
            "Достаточно": "background-color: #E6F1FB; color: #185FA5",
        }
        return m.get(val, "")

    styled = (
        show.style
        .map(style_turnover, subset=["Оборачиваемость"])
        .map(style_status, subset=["Статус"])
    )

    st.dataframe(styled, use_container_width=True, height=500,
        column_config={
            "Остаток":          st.column_config.NumberColumn(format="%d шт"),
            "В пути к клиенту": st.column_config.NumberColumn(format="%d шт"),
            "FBO в пути":       st.column_config.NumberColumn(format="%d шт"),
            "Общий остаток":    st.column_config.NumberColumn(format="%d шт"),
            "Ср. продаж/день":  st.column_config.NumberColumn(format="%.1f"),
            "Оборачиваемость":  st.column_config.NumberColumn(format="%d дн"),
        }
    )
    st.caption(f"Показано: {len(df)} позиций (с продажами за последние 20 дней)")

    buf = io.BytesIO()
    with pd.ExcelWriter(buf, engine="openpyxl") as writer:
        show.to_excel(writer, index=False, sheet_name="Отчёт WB")
    st.download_button(
        "⬇️ Скачать Excel",
        data=buf.getvalue(),
        file_name=f"WB_Отчёт_{datetime.now().strftime('%Y%m%d_%H%M')}.xlsx",
        mime="application/vnd.openxmlformats-officedocument.spreadsheetml.sheet"
    )

else:
    st.info("👈 Нажмите **«Обновить данные»** для загрузки отчёта")
