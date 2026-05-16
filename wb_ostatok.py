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
</style>
""", unsafe_allow_html=True)

def check_password():
    if "authenticated" not in st.session_state:
        st.session_state.authenticated = False
    if st.session_state.authenticated:
        return True
    st.title("🔐 WB Остатоктар дашборды")
    st.markdown("---")
    col1, col2, col3 = st.columns([1, 1.5, 1])
    with col2:
        st.markdown("### Кіру")
        pwd = st.text_input("Пароль", type="password", placeholder="••••••••")
        if st.button("Кіру →", use_container_width=True):
            if pwd == st.secrets.get("PASSWORD", "director2024"):
                st.session_state.authenticated = True
                st.rerun()
            else:
                st.error("Пароль дұрыс емес!")
    return False

if not check_password():
    st.stop()

st.title("📦 Wildberries — Остатоктар дашборды")
st.caption(f"Жаңартылды: {datetime.now().strftime('%d.%m.%Y %H:%M')}")

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

with st.sidebar:
    st.header("⚙️ Баптаулар")
    api_key = st.secrets.get("WB_API_KEY", "")
    if not api_key:
        api_key = st.text_input("API кілті", type="password", placeholder="eyJ...")
    else:
        st.success("✅ API кілті қосылған")
    fetch_btn = st.button("🔄 Деректерді жүктеу", use_container_width=True)
    st.divider()
    st.markdown("**Фильтрлер**")
    filter_type = st.selectbox("Остаток күйі", [
        "Барлығы", "Нөлдік (=0)", "Аз (1–10)", "Қалыпты (11–100)", "Артық (>100)"
    ])
    if st.button("🚪 Шығу"):
        st.session_state.authenticated = False
        st.rerun()

if "df" not in st.session_state:
    st.session_state.df = None
if "warehouses" not in st.session_state:
    st.session_state.warehouses = []

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
                })
                keep = ["Артикул", "Баркод", "Категория", "Бренд", "Склад",
                        "Қалдық", "Жолда (клиентке)", "Жолда (қайтарым)", "Өлшем"]
                df = df[[c for c in keep if c in df.columns]]
                df["Күй"] = df["Қалдық"].apply(status)
                st.session_state.df = df
                st.session_state.warehouses = ["Барлығы"] + sorted(df["Склад"].dropna().unique().tolist())
                st.sidebar.success(f"✅ {len(df)} жазба жүктелді")
            except requests.exceptions.HTTPError as e:
                st.sidebar.error(f"API қатесі: {e.response.status_code}")
            except Exception as e:
                st.sidebar.error(f"Қате: {str(e)}")

if st.session_state.df is not None:
    df = st.session_state.df.copy()
    wh_options = st.session_state.warehouses if st.session_state.warehouses else ["Барлығы"]
    wh = st.sidebar.selectbox("Склад", wh_options)
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

    search = st.text_input("🔍 Іздеу", placeholder="Артикул немесе категория...")
    if search:
        mask = df["Артикул"].astype(str).str.contains(search, case=False, na=False) | \
               df["Категория"].astype(str).str.contains(search, case=False, na=False)
        df = df[mask]

    col1, col2, col3, col4 = st.columns(4)
    base = st.session_state.df if wh == "Барлығы" else st.session_state.df[st.session_state.df["Склад"] == wh]
    col1.metric("📦 Барлық SKU", f"{len(base):,}")
    col2.metric("🔴 Нөлдік", f"{(base['Қалдық'] == 0).sum():,}")
    col3.metric("🔵 Артық (>100)", f"{(base['Қалдық'] > 100).sum():,}")
    col4.metric("📊 Жалпы қалдық", f"{base['Қалдық'].sum():,.0f}")

    st.divider()
    tab1, tab2 = st.tabs(["📋 Кесте", "📊 Диаграмма"])
    with tab1:
        st.dataframe(df.reset_index(drop=True), use_container_width=True, height=450,
            column_config={"Қалдық": st.column_config.NumberColumn(format="%d дана")})
        st.caption(f"Көрсетілуде: {len(df):,} жазба")
        buf = io.BytesIO()
        with pd.ExcelWriter(buf, engine="openpyxl") as writer:
            df.to_excel(writer, index=False, sheet_name="Остатки")
        st.download_button("⬇️ Excel жүктеу", data=buf.getvalue(),
            file_name=f"WB_Остатки_{datetime.now().strftime('%Y%m%d_%H%M')}.xlsx",
            mime="application/vnd.openxmlformats-officedocument.spreadsheetml.sheet")
    with tab2:
        st.subheader("Склад бойынша бөліну")
        wh_df = st.session_state.df.groupby("Склад")["Қалдық"].sum().sort_values(ascending=False).head(15)
        st.bar_chart(wh_df)
        st.subheader("Күй бойынша")
        st.bar_chart(st.session_state.df["Күй"].value_counts())
else:
    st.info("👈 Сол жақта **«Деректерді жүктеу»** батырмасын басыңыз")
