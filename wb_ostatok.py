import streamlit as st
import requests
import pandas as pd
from datetime import datetime, timedelta
import io

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

def days_ago(n):
    return (datetime.now() - timedelta(days=n)).strftime("%Y-%m-%dT00:00:00")

def wb_get(url, key, params={}):
    r = requests.get(url, headers={"Authorization": key}, params=params, timeout=60)
    r.raise_for_status()
    return r.json()

def fetch_stocks(key):
    return wb_get(
        "https://statistics-api.wildberries.ru/api/v1/supplier/stocks",
        key, {"dateFrom": "2019-01-01"}
    )

def fetch_sales(key, days):
    return wb_get(
        "https://statistics-api.wildberries.ru/api/v1/supplier/sales",
        key, {"dateFrom": days_ago(days), "flag": 0}
    )

def fetch_incomes(key):
    return wb_get(
        "https://statistics-api.wildberries.ru/api/v1/supplier/incomes",
        key, {"dateFrom": days_ago(60)}
    )

def fetch_balance(key):
    try:
        r = requests.get(
            "https://statistics-api.wildberries.ru/api/v1/supplier/balance",
            headers={"Authorization": key}, timeout=30
        )
        if r.ok:
            d = r.json()
            return d.get("balance", None)
    except:
        pass
    return None

def status_label(q):
    if q == 0:   return "Ноль"
    if q <= 200: return "Мало"
    if q <= 500: return "Хорошо"
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

for k in ["df", "balance", "in_process", "total_stock"]:
    if k not in st.session_state:
        st.session_state[k] = None

if fetch_btn:
    if not api_key:
        st.sidebar.error("Введите API ключ!")
    else:
        errors = []
        with st.spinner("Загружаем данные из Wildberries..."):

            # 1. Остатки
            try:
                stocks_raw = fetch_stocks(api_key)
                s_df = pd.DataFrame(stocks_raw)
                agg = s_df.groupby("supplierArticle").agg(
                    name=("subject", "first"),
                    qty=("quantity", "sum"),
                    in_way_client=("inWayToClient", "sum"),
                    in_way_return=("inWayFromClient", "sum"),
                ).reset_index()
            except Exception as e:
                errors.append(f"Остатки: {e}")
                agg = pd.DataFrame()

            # 2. Продажи за 7 дней (средние)
            try:
                s7 = fetch_sales(api_key, 7)
                s7_df = pd.DataFrame(s7) if s7 else pd.DataFrame()
                if not s7_df.empty and "supplierArticle" in s7_df.columns:
                    if "saleID" in s7_df.columns:
                        s7_df = s7_df[~s7_df["saleID"].astype(str).str.startswith("R")]
                    daily = s7_df.groupby("supplierArticle").size().div(7).reset_index()
                    daily.columns = ["supplierArticle", "daily_avg"]
                else:
                    daily = pd.DataFrame(columns=["supplierArticle", "daily_avg"])
            except Exception as e:
                errors.append(f"Продажи 7д: {e}")
                daily = pd.DataFrame(columns=["supplierArticle", "daily_avg"])

            # 3. Продажи за 20 дней (фильтр активных)
            try:
                s20 = fetch_sales(api_key, 20)
                s20_df = pd.DataFrame(s20) if s20 else pd.DataFrame()
                if not s20_df.empty and "supplierArticle" in s20_df.columns:
                    if "saleID" in s20_df.columns:
                        s20_df = s20_df[~s20_df["saleID"].astype(str).str.startswith("R")]
                    active = set(s20_df["supplierArticle"].unique())
                else:
                    active = set()
            except Exception as e:
                errors.append(f"Продажи 20д: {e}")
                active = set()

            # 4. FBO в пути (поставки)
            try:
                inc_raw = fetch_incomes(api_key)
                inc_df = pd.DataFrame(inc_raw) if inc_raw else pd.DataFrame()
                if not inc_df.empty and "status" in inc_df.columns:
                    in_tr = inc_df[~inc_df["status"].isin(["Принято", "Отклонён"])]
                    fbo = in_tr.groupby("supplierArticle")["quantity"].sum().reset_index()
                    fbo.columns = ["supplierArticle", "fbo_way"]
                else:
                    fbo = pd.DataFrame(columns=["supplierArticle", "fbo_way"])
            except Exception as e:
                errors.append(f"Поставки FBO: {e}")
                fbo = pd.DataFrame(columns=["supplierArticle", "fbo_way"])

            # 5. Баланс
            balance = fetch_balance(api_key)
            st.session_state.balance = balance

            # Объединяем всё
            if not agg.empty:
                df = agg.copy()
                df = df.merge(daily, on="supplierArticle", how="left")
                df = df.merge(fbo, on="supplierArticle", how="left")
                df["daily_avg"] = df["daily_avg"].fillna(0).round(1)
                df["fbo_way"] = df["fbo_way"].fillna(0).astype(int)
                df["total_qty"] = df["qty"] + df["in_way_client"] + df["fbo_way"]
                df["turnover"] = df.apply(
                    lambda r: round(r["total_qty"] / r["daily_avg"]) if r["daily_avg"] > 0 else None,
                    axis=1
                )
                df["status"] = df["qty"].apply(status_label)

                # Фильтр: только с продажами за 20 дней
                if active:
                    df = df[df["supplierArticle"].isin(active)]

                st.session_state.df = df
                st.session_state.total_stock = int(df["total_qty"].sum())
                st.sidebar.success(f"✅ Загружено {len(df)} позиций")

            for e in errors:
                st.sidebar.warning(f"⚠️ {e}")

# Заголовок
st.title("📦 Wildberries отчёт")
st.caption(f"Обновлено: {datetime.now().strftime('%d.%m.%Y %H:%M')}")

if st.session_state.df is not None:
    df = st.session_state.df.copy()

    # Фильтры
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
    bal = st.session_state.balance
    c1, c2, c3, c4 = st.columns(4)
    c1.metric("💰 Баланс WB", f"{bal:,.0f} ₸".replace(",", " ") if bal is not None else "—")
    c2.metric("⏳ В обработке", "—", help="Требуется токен Финансы")
    c3.metric("➕ Общая сумма", f"{bal:,.0f} ₸".replace(",", " ") if bal is not None else "—")
    c4.metric("📦 Общий остаток", f"{st.session_state.total_stock:,} шт".replace(",", " "))

    st.divider()

    # Таблица
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
        .applymap(style_turnover, subset=["Оборачиваемость"])
        .applymap(style_status, subset=["Статус"])
    )

    st.dataframe(styled, use_container_width=True, height=500,
        column_config={
            "Остаток":         st.column_config.NumberColumn(format="%d шт"),
            "В пути к клиенту": st.column_config.NumberColumn(format="%d шт"),
            "FBO в пути":      st.column_config.NumberColumn(format="%d шт"),
            "Общий остаток":   st.column_config.NumberColumn(format="%d шт"),
            "Ср. продаж/день": st.column_config.NumberColumn(format="%.1f"),
            "Оборачиваемость": st.column_config.NumberColumn(format="%d дн"),
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
    st.markdown("""
    **Что показывает отчёт:**
    - 💰 Баланс и сумма в обработке
    - 📦 Остатки, товары в пути к клиенту и FBO поставки
    - 📊 Средние продажи в день (за 7 дней) и оборачиваемость
    - 🔴 Красная ячейка — оборачиваемость 10 дней и меньше
    - Товары без продаж за 20 дней не отображаются
    """)
    
