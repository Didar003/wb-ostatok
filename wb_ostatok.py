import streamlit as st
import requests
import pandas as pd
from datetime import datetime
import io

st.set_page_config(
    page_title="WB Остатоктар",
    page_icon="📦",
    layout="wide"
)

st.markdown("""
<style>
    .block-container { padding-top: 1.5rem; }
    .metric-card {
        background: #f8f9fa;
        border-radius: 10px;
        padding: 1rem 1.2rem;
        border: 1px solid #e9ecef;
    }
    .stDataFrame { border-radius: 8px; }
</style>
""", unsafe_allow_html=True)

st.title("📦 Wildberries — Остатоктар дашборды")
st.caption(f"Жаңартылды: {datetime.now().strftime('%d.%m.%Y %H:%M')}")

with st.sidebar:
    st.header("⚙️ Баптаулар")
    api_key = st.text_input("API кілті", type="password", placeholder="eyJ...")
    fetch_btn = st.button("🔄 Деректерді жүктеу", use_container_width=True)
    st.divider()
    st.markdown("**Фильтрлер**")
    filter_type = st.selectbox("Остаток күйі", [
        "Барлығы", "Нөлдік (=0)", "Аз (1–10)", "Қалыпты (11–100)", "Артық (>100)"
    ])
    warehouse_filter = st.selectbox("Склад", ["Барлығы"])

if "df" not in st.session_state:
    st.session_state.df = None
if "warehouses" not in st.session_state:
    st.session_state.warehouses = []

def fetch_stocks(key):
    url = "https://statistics-api.wildberries.ru/api/v1/supplier/stocks"
    params = {"dateFrom": "2019-01-01"}
    headers = {"Authorization": key}
    resp = requests.get(url, headers=headers, params=params, timeout=60)
    resp.raise_for_status()
    return resp.json()

def status(q):
    if q == 0: return "🔴 Нөл"
    if q <= 10: return "🟡 Аз"
    if q <= 100: return "🟢 Қалыпты"
    return "🔵 Артық"

if fetch_btn:
    if not api_key:
        st.sidebar.error("API кілтін енгізіңіз!")
    else:
        with st.spinner("WB API-дан деректер жүктелуде..."):
            try:
                data = fetch_stocks(api_key)
                df = pd.DataFrame(data)
                df = df.rename(columns={
                    "supplierArticle": "Артикул",
                    "barcode": "Баркод",
                    "subject": "Категория",
                    "brand": "Бренд",
                    "warehouseName": "Склад",
                    "quantity": "Қалдық",
                    "inWayToClient": "Жолда (клиентке)",
                    "inWayFromClient": "Жолда (қайтарым)",
                    "techSize": "Өлшем",
                    "Price": "Баға",
                    "Discount": "Жеңілдік %"
                })
                keep = ["Артикул", "Баркод", "Категория", "Бренд", "Склад",
                        "Қалдық", "Жолда (клиентке)", "Жолда (қайтарым)", "Өлшем"]
                df = df[[c for c in keep if c in df.columns]]
                df["Күй"] = df["Қалдық"].apply(status)
                st.session_state.df = df
                st.session_state.warehouses = ["Барлығы"] + sorted(df["Склад"].dropna().unique().tolist())
                st.sidebar.success(f"✅ {len(df)} жазба жүктелді")
            except requests.exceptions.HTTPError as e:
                st.sidebar.error(f"API қатесі: {e.response.status_code} — кілтті тексеріңіз")
            except Exception as e:
                st.sidebar.error(f"Қате: {str(e)}")

if st.session_state.df is not None:
    df = st.session_state.df.copy()

    if st.session_state.warehouses:
        wh = st.sidebar.selectbox("Склад", st.session_state.warehouses)
    else:
        wh = "Барлығы"

    if wh != "Барлығы":
        df = df[df["Склад"] == wh]

    if filter_type == "Нөлдік (=0)":
        df = df[df["Қалдық"] == 0]
    elif filter_type == "Аз (1–10)":
        df = df[(df["Қалдық"] >= 1) & (df["Қалдық"] <= 10)]
    elif filter_type == "Қалыпты (11–100)":
        df = df[(df["Қалдық"] >= 11) & (df["Қалдық"] <= 100)]
    elif filter_type == "Артық (>100)":
        df = df[df["Қалдық"] > 100]

    search = st.text_input("🔍 Іздеу (артикул, категория...)", placeholder="Артикул немесе категория атауы")
    if search:
        mask = df["Артикул"].astype(str).str.contains(search, case=False, na=False) | \
               df["Категория"].astype(str).str.contains(search, case=False, na=False)
        df = df[mask]

    col1, col2, col3, col4 = st.columns(4)
    total = st.session_state.df if wh == "Барлығы" else st.session_state.df[st.session_state.df["Склад"] == wh]
    col1.metric("📦 Барлық SKU", f"{len(total):,}")
    col2.metric("🔴 Нөлдік", f"{(total['Қалдық'] == 0).sum():,}")
    col3.metric("🔵 Артық (>100)", f"{(total['Қалдық'] > 100).sum():,}")
    col4.metric("📊 Жалпы қалдық", f"{total['Қалдық'].sum():,.0f}")

    st.divider()

    tab1, tab2 = st.tabs(["📋 Кесте", "📊 Диаграмма"])

    with tab1:
        st.dataframe(
            df.reset_index(drop=True),
            use_container_width=True,
            height=450,
            column_config={
                "Қалдық": st.column_config.NumberColumn(format="%d дана"),
                "Жолда (клиентке)": st.column_config.NumberColumn(format="%d"),
            }
        )
        st.caption(f"Көрсетілуде: {len(df):,} жазба")

        buf = io.BytesIO()
        with pd.ExcelWriter(buf, engine="openpyxl") as writer:
            df.to_excel(writer, index=False, sheet_name="Остатки")
        st.download_button(
            "⬇️ Excel жүктеу",
            data=buf.getvalue(),
            file_name=f"WB_Остатки_{datetime.now().strftime('%Y%m%d_%H%M')}.xlsx",
            mime="application/vnd.openxmlformats-officedocument.spreadsheetml.sheet"
        )

    with tab2:
        st.subheader("Склад бойынша бөліну")
        wh_df = st.session_state.df.groupby("Склад")["Қалдық"].sum().sort_values(ascending=False).head(15)
        st.bar_chart(wh_df)

        st.subheader("Күй бойынша")
        status_df = st.session_state.df["Күй"].value_counts()
        st.bar_chart(status_df)

else:
    st.info("👈 Сол жақта API кілтін енгізіп, **«Деректерді жүктеу»** батырмасын басыңыз")
    st.markdown("""
    **Қалай жұмыс істейді:**
    1. WB Кабинет → Настройки → Доступ к API → жаңа токен жасаңыз (**Статистика** рұқсатымен)
    2. Токенді сол жақ өріске енгізіңіз
    3. Деректерді жүктеңіз — директор нәтижені осы экранда көреді
    """)
