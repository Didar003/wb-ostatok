import streamlit as st
import requests
import pandas as pd
from datetime import datetime, timedelta, date
import io
import time
import json
import os

st.set_page_config(page_title="Wildberries Отчёт", page_icon="📦", layout="wide")
st.markdown("<style>.block-container{padding-top:1.5rem;}</style>", unsafe_allow_html=True)

FBO_FILE = "/tmp/wb_fbo_data.json"
SEBEST_FILE = "/tmp/wb_sebest_data.json"

def load_json(path):
    try:
        if os.path.exists(path):
            with open(path, "r", encoding="utf-8") as f:
                return json.load(f)
    except:
        pass
    return {}

def save_json(path, data):
    try:
        with open(path, "w", encoding="utf-8") as f:
            json.dump(data, f, ensure_ascii=False)
    except Exception as e:
        st.warning(f"Сақталмады: {e}")

# ──────────────────────────────────────────────
# КІРУ
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
            if manager_pwd and pwd == manager_pwd:
                st.session_state.role = "manager"
                st.session_state.store_access = None
                st.rerun()
                return True
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
    names_str = st.secrets.get("STORE_NAMES", "")
    if not names_str:
        return []
    names = [n.strip() for n in names_str.split(",") if n.strip()]
    stores = []
    for i, name in enumerate(names, 1):
        stats_key = st.secrets.get(f"STORE_{i}_STATS", "")
        analytics_key = st.secrets.get(f"STORE_{i}_ANALYTICS", "")
        finance_key = st.secrets.get(f"STORE_{i}_FINANCE", "")
        if stats_key:
            stores.append({
                "name": name, "idx": i,
                "stats_key": stats_key,
                "analytics_key": analytics_key,
                "finance_key": finance_key,
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
            st.info(f"⏳ [{store_name}] WB API лимит — 65 сек...")
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
            raise Exception(f"Қате: {status}")
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

def fetch_report_detail(stats_key, date_from, date_to, store_name=""):
    """reportDetailByPeriod — финансы отчеті"""
    # v5 — жаңа отчёттар үшін (27 қарашадан бастап)
    # v1 — ескі отчёттар үшін
    urls = [
        "https://statistics-api.wildberries.ru/api/v5/supplier/reportDetailByPeriod",
        "https://statistics-api.wildberries.ru/api/v1/supplier/reportDetailByPeriod",
    ]
    all_rows = []
    for url in urls:
        try:
            rrdid = 0
            while True:
                params = {
                    "dateFrom": date_from,
                    "dateTo": date_to,
                    "rrdid": rrdid,
                    "limit": 100000
                }
                r = requests.get(url, headers={"Authorization": stats_key},
                                 params=params, timeout=60)
                if r.status_code == 404:
                    break
                if r.status_code == 429:
                    time.sleep(65)
                    continue
                r.raise_for_status()
                data = r.json()
                if not data:
                    break
                all_rows.extend(data)
                rrdid = data[-1].get("rrd_id", 0)
                if len(data) < 100000:
                    break
                time.sleep(2)
            if all_rows:
                break
        except Exception:
            continue
    return all_rows

def parse_finance(rows):
    """Финансы деректерін парсинг"""
    if not rows:
        return {}
    result = {
        "for_pay": 0,        # К перечислению (продажа ppvz_for_pay)
        "ads": 0,            # Удержания (реклама WB)
        "storage": 0,        # Хранение
        "penalty": 0,        # Штраф
        "logistic": 0,       # Логистика (доставка)
        "vozvrat": 0,        # Возврат ppvz
        "vozvrat_qty": 0,    # Возврат дана саны
        "total_qty": 0,      # Жалпы сатылды
        "by_article": {}     # Артикул бойынша
    }

    for row in rows:
        oper = str(row.get("supplier_oper_name", "")).strip()
        oper_up = oper.upper()
        ppvz = float(row.get("ppvz_for_pay", 0) or 0)
        deduct = float(row.get("deduction", 0) or 0)
        storage_fee = float(row.get("storage_fee", 0) or 0)
        penalty_val = float(row.get("penalty", 0) or 0)
        delivery_rub = float(row.get("delivery_rub", 0) or 0)
        qty = int(row.get("quantity", 0) or 0)
        article = str(row.get("sa_name", "") or "").strip()

        if oper_up in ("ПРОДАЖА", "ДОБРОВОЛЬНАЯ КОМПЕНСАЦИЯ ПРИ ВОЗВРАТЕ"):
            result["for_pay"] += ppvz
            result["total_qty"] += qty
            if article:
                if article not in result["by_article"]:
                    result["by_article"][article] = {"qty": 0, "for_pay": 0, "vozvrat": 0}
                result["by_article"][article]["qty"] += qty
                result["by_article"][article]["for_pay"] += ppvz

        elif oper_up == "ВОЗВРАТ":
            result["vozvrat"] += abs(ppvz)
            result["vozvrat_qty"] += qty
            if article and article in result["by_article"]:
                result["by_article"][article]["vozvrat"] += abs(ppvz)

        elif oper_up == "ЛОГИСТИКА" or "ДОСТАВК" in oper_up:
            # "Услуги по доставке товара покупателю"
            result["logistic"] += abs(delivery_rub)

        elif "ОБРАБОТКА" in oper_up:
            # "Операции на приемке" — assembly_id емес, сандық өрістен аламыз
            # Excel: "Операции на приемке" → API: "acceptance" немесе "assembly_id"
            priemka_val = (
                abs(float(row.get("acceptance", 0) or 0)) or
                abs(float(row.get("assembly_id", 0) or 0)) or
                abs(float(row.get("ppvz_for_pay", 0) or 0)) or
                abs(float(row.get("deduction", 0) or 0))
            )
            result["priemka"] += priemka_val

        elif "ХРАНЕНИЕ" in oper_up:
            result["storage"] += abs(storage_fee) + abs(deduct)

        elif "ШТРАФ" in oper_up:
            result["penalty"] += abs(penalty_val) + abs(deduct)

        elif "УДЕРЖАНИЕ" in oper_up:
            # Реклама WB удержания
            result["ads"] += abs(deduct) + abs(ppvz)

    # Егер логистика API-да жеке операция ретінде келмесе
    # барлық жолдан delivery_rub жалпысын аламыз (резервтік)
    if result["logistic"] == 0:
        for row in rows:
            result["logistic"] += abs(float(row.get("delivery_rub", 0) or 0))
    return result

def status_label(q):
    if q == 0:    return "Ноль"
    if q <= 200:  return "Мало"
    if q <= 500:  return "Хорошо"
    return "Достаточно"

# ──────────────────────────────────────────────
# ОСТАТКИ ЖҮКТЕУ
# ──────────────────────────────────────────────
def load_store_data(store):
    idx = store["idx"]
    stats_key = store["stats_key"]
    analytics_key = store["analytics_key"]
    name = store["name"]
    errors = []

    # Остатки
    if analytics_key:
        try:
            with st.spinner(f"[{name}] Остатки және FBO жүктелуде..."):
                remains_data = fetch_warehouse_remains(analytics_key)
                agg = parse_remains(remains_data)
        except Exception as e:
            errors.append(f"Остатки: {e}")
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

    # Продажи
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

    time.sleep(3)

    # Аналитика 30 күн
    sales30 = pd.DataFrame()
    try:
        with st.spinner(f"[{name}] Аналитика жүктелуде..."):
            orders_raw = wb_get_retry(
                "https://statistics-api.wildberries.ru/api/v1/supplier/orders",
                stats_key, {"dateFrom": days_ago_str(90), "flag": 0}, store_name=name
            )
            o_df = pd.DataFrame(orders_raw) if orders_raw else pd.DataFrame()
            if not o_df.empty and "date" in o_df.columns:
                o_df["date"] = pd.to_datetime(o_df["date"], errors="coerce")
                cutoff30 = datetime.now() - timedelta(days=30)
                o_df = o_df[o_df["date"] >= cutoff30]
                if "isCancel" in o_df.columns:
                    o_df = o_df[o_df["isCancel"] == False]
                o_df["date_only"] = o_df["date"].dt.date
                o_df["priceWithDisc"] = pd.to_numeric(o_df.get("priceWithDisc", 0), errors="coerce").fillna(0)
                sales30 = o_df.groupby("date_only").agg(
                    qty=("date_only", "count"),
                    revenue=("priceWithDisc", "sum")
                ).reset_index()
                sales30.columns = ["Дата", "Заказ (шт)", "Выручка (₸)"]
                sales30 = sales30.sort_values("Дата")
    except Exception as e:
        errors.append(f"Аналитика: {e}")

    # Итог
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
# ФИНАНСЫ ТАБИ
# ──────────────────────────────────────────────
def show_finance_tab(store, df):
    idx = store["idx"]
    stats_key = store["stats_key"]
    finance_key = store["finance_key"]
    name = store["name"]
    role = st.session_state.get("role", "manager")

    st.markdown("#### 💰 Финансы отчет")

    # Период таңдау
    col1, col2, col3 = st.columns([1, 1, 1])
    with col1:
        date_from = st.date_input("Басталу күні", value=date.today() - timedelta(days=7),
                                   key=f"fin_from_{idx}")
    with col2:
        date_to = st.date_input("Аяқталу күні", value=date.today(),
                                 key=f"fin_to_{idx}")
    with col3:
        st.markdown("<br>", unsafe_allow_html=True)
        load_fin = st.button("🔄 Финансы жүктеу", key=f"fin_load_{idx}", use_container_width=True)

    fin_key = f"finance_{idx}"
    use_key = finance_key if finance_key else stats_key

    # Период өзгерсе кэшті тазала
    period_key = f"fin_period_{idx}"
    current_period = f"{date_from}_{date_to}"
    if st.session_state.get(period_key) != current_period:
        if fin_key in st.session_state:
            del st.session_state[fin_key]
        st.session_state[period_key] = current_period

    # Ескі кэште priemka жоқ болса тазала
    if fin_key in st.session_state and "priemka" not in st.session_state[fin_key]:
        del st.session_state[fin_key]

    if load_fin:
        with st.spinner(f"[{name}] Финансы отчеті жүктелуде..."):
            try:
                rows = fetch_report_detail(
                    use_key,
                    date_from.strftime("%Y-%m-%d"),
                    date_to.strftime("%Y-%m-%d"),
                    store_name=name
                )
                st.sidebar.info(f"📊 Жолдар саны: {len(rows)}")
                if rows:
                    st.sidebar.caption(f"Бірінші жол өрістері: {list(rows[0].keys())}")
                    opers = list(set(str(r.get("supplier_oper_name","")) for r in rows))
                    st.sidebar.caption(f"Операция түрлері: {opers}")
                    # Барлық өріс атауларын көрсет
                    all_opers = list(set(str(r.get("supplier_oper_name","")) for r in rows))
                    st.sidebar.caption(f"Операциялар: {all_opers}")
                    # Барлық delivery өрістерін тап
                    del_rows = [r for r in rows if float(r.get("delivery_rub", 0) or 0) != 0]
                    del_amt_rows = [r for r in rows if float(r.get("delivery_amount", 0) or 0) != 0]
                    st.sidebar.caption(f"delivery_rub > 0: {len(del_rows)} жол, delivery_amount > 0: {len(del_amt_rows)} жол")
                    if del_rows:
                        r0 = del_rows[0]
                        st.sidebar.caption(f"delivery_rub жолы: oper='{r0.get('supplier_oper_name')}', val={r0.get('delivery_rub')}")
                    if del_amt_rows:
                        r0 = del_amt_rows[0]
                        st.sidebar.caption(f"delivery_amount жолы: oper='{r0.get('supplier_oper_name')}', val={r0.get('delivery_amount')}")
                    # AK бағаны delivery_amount болуы мүмкін — жалпы сомасын есепте
                    total_del = sum(abs(float(r.get("delivery_rub", 0) or 0)) for r in rows)
                    total_del_amt = sum(abs(float(r.get("delivery_amount", 0) or 0)) for r in rows)
                    st.sidebar.caption(f"delivery_rub жалпы: {total_del:.0f}, delivery_amount жалпы: {total_del_amt:.0f}")
                fin = parse_finance(rows)
                # Кэшті толық жаңарту үшін алдымен өшіреміз
                if fin_key in st.session_state:
                    del st.session_state[fin_key]
                st.session_state[fin_key] = fin
                log_count = fin.get("_log_count", 0)
                log_sum = fin.get("logistic", 0)
                # Логистика жолының барлық өрістерін тексер
                log_rows = [r for r in rows if str(r.get("supplier_oper_name","")).strip().upper() == "ЛОГИСТИКА"]
                if log_rows:
                    r0 = log_rows[0]
                    fields = {k: v for k, v in r0.items() if isinstance(v, (int, float)) and v != 0}
                    st.sidebar.write(f"Лог жол өрістері (0 емес): {fields}")
                st.sidebar.success(f"✅ {len(rows)} жол | Лог: {log_count} жол | Логистика: {log_sum:,.0f} ₸")
                st.rerun()
            except Exception as e:
                st.error(f"Қате: {e}")

    if fin_key not in st.session_state:
        st.info("👆 Период таңдап **«Финансы жүктеу»** батырмасын басыңыз")
        return

    fin = st.session_state[fin_key]

    # Қолмен енгізу мәндерін сақтау
    man_key = f"fin_manual_{idx}"
    if man_key not in st.session_state:
        st.session_state[man_key] = {"logistic": 0, "samovykup": 0, "reklama_napay": 0}
    man = st.session_state[man_key]

    # Есептеу
    for_pay = fin.get("for_pay", 0)
    ads = fin.get("ads", 0)
    storage = fin.get("storage", 0)
    penalty = fin.get("penalty", 0)
    logistic_auto = fin.get("logistic", 0)  # WB API-дан автоматты
    vozvrat = fin.get("vozvrat", 0)
    vozvrat_qty = fin.get("vozvrat_qty", 0)
    total_qty = fin.get("total_qty", 0)
    vozvrat_shygyn = vozvrat * 2

    logistic = man["logistic"]  # қолмен — складқа дейінгі жеткізу
    samovykup = man["samovykup"]
    reklama_napay = man["reklama_napay"]

    # Себестоимость
    seb_key = f"sebest_{idx}"
    all_seb = load_json(SEBEST_FILE)
    seb_data = all_seb.get(str(idx), {})

    # Тауарлар тізімі
    articles = []
    if not df.empty:
        articles = df["supplierArticle"].tolist()
    by_article = fin.get("by_article", {})

    # Жалпы себестоимость
    tot_seb = sum(seb_data.get(a, 0) * by_article.get(a, {}).get("qty", 0) for a in by_article)
    tot_qty_sold = sum(by_article.get(a, {}).get("qty", 0) for a in by_article)
    upakovka = tot_qty_sold * 100

    priemka = fin.get("priemka", 0)
    napay = for_pay - ads - logistic_auto - storage - priemka - penalty - vozvrat_shygyn
    ndv_rate = 16/116
    ndv_total = napay * ndv_rate
    ndv_prikhod = tot_seb * ndv_rate
    ndv_nashe = ndv_total - ndv_prikhod
    do_ipn = napay - ndv_nashe - tot_seb - upakovka - logistic - samovykup - reklama_napay
    ipn = do_ipn * 0.10 if do_ipn > 0 else 0
    profit = do_ipn - ipn

    def fmt(n): return f"{round(n):,} ₸".replace(",", " ")
    def fmtN(n): return f"{round(n):,}".replace(",", " ")

    # МЕТРИКАЛАР
    c1, c2, c3 = st.columns(3)
    c1.metric("К перечислению", fmt(for_pay))
    c2.metric("На пэй", fmt(napay))
    c3.metric("Таза пайда", fmt(profit),
              delta=f"{profit/for_pay*100:.1f}%" if for_pay > 0 else "0%")

    st.divider()

    col_left, col_right = st.columns(2)

    with col_left:
        st.markdown("**🧮 Кіріс / Шығыс**")
        rows_ui = [
            ("авто", "К перечислению", fmt(for_pay), "blue"),
            ("авто", "Удержания (реклама WB)", f"- {fmt(ads)}", "red"),
            ("авто", "Логистика WB (жеткізу)", f"- {fmt(logistic_auto)}", "red"),

            ("авто", "Хранение", f"- {fmt(storage)}", "red"),
            ("авто", "Операции на приёмке", f"- {fmt(priemka)}", "red"),
            ("авто", "Штраф", f"- {fmt(penalty)}", "red"),
            ("авто", f"Возврат × 2 ({fmtN(vozvrat_qty)} шт)", f"- {fmt(vozvrat_shygyn)}", "red"),
        ]
        for tag, label, value, color in rows_ui:
            r1, r2 = st.columns([3, 2])
            r1.caption(f"[{tag}] {label}")
            if color == "blue":
                r2.markdown(f"<p style='text-align:right;color:#185FA5;font-weight:500;'>{value}</p>", unsafe_allow_html=True)
            else:
                r2.markdown(f"<p style='text-align:right;color:#A32D2D;font-weight:500;'>{value}</p>", unsafe_allow_html=True)

        st.markdown(f"**На пэй: :blue[{fmt(napay)}]**")
        st.divider()

        r1, r2 = st.columns([3, 2])
        r1.caption("[авто] НДС наше (16/116)")
        r2.markdown(f"<p style='text-align:right;color:#A32D2D;font-weight:500;'>- {fmt(ndv_nashe)}</p>", unsafe_allow_html=True)

        r1, r2 = st.columns([3, 2])
        r1.caption("[авто] Себестоимость")
        r2.markdown(f"<p style='text-align:right;color:#A32D2D;font-weight:500;'>- {fmt(tot_seb)}</p>", unsafe_allow_html=True)

        r1, r2 = st.columns([3, 2])
        r1.caption(f"[авто] Упаковка ({fmtN(tot_qty_sold)} × 100₸)")
        r2.markdown(f"<p style='text-align:right;color:#A32D2D;font-weight:500;'>- {fmt(upakovka)}</p>", unsafe_allow_html=True)

        # Қолмен
        if role == "manager":
            new_log_wb = st.number_input("[қол] Логистика WB — жеткізу (₸)", value=float(man.get("logistic_wb", 0)), min_value=0.0, step=1000.0, key=f"log_wb_{idx}", help="WB кабинетіндегі логистика сомасы")
            new_log = st.number_input("[қол] Логистика до склада (₸)", value=float(man["logistic"]), min_value=0.0, step=1000.0, key=f"log_{idx}")
            new_samo = st.number_input("[қол] Самовыкуп (₸)", value=float(man["samovykup"]), min_value=0.0, step=1000.0, key=f"samo_{idx}")
            new_rek = st.number_input("[қол] Реклама на пэй (сыртқы, ₸)", value=float(man["reklama_napay"]), min_value=0.0, step=1000.0, key=f"rek_{idx}")
            if new_log != man["logistic"] or new_samo != man["samovykup"] or new_rek != man["reklama_napay"] or new_log_wb != man.get("logistic_wb", 0):
                st.session_state[man_key] = {"logistic": new_log, "logistic_wb": new_log_wb, "samovykup": new_samo, "reklama_napay": new_rek}
                st.rerun()
        else:
            r1, r2 = st.columns([3, 2])
            r1.caption("[қол] Логистика до склада"); r2.markdown(f"<p style='text-align:right;'>- {fmt(logistic)}</p>", unsafe_allow_html=True)
            r1, r2 = st.columns([3, 2])
            r1.caption("[қол] Самовыкуп"); r2.markdown(f"<p style='text-align:right;'>- {fmt(samovykup)}</p>", unsafe_allow_html=True)
            r1, r2 = st.columns([3, 2])
            r1.caption("[қол] Реклама на пэй"); r2.markdown(f"<p style='text-align:right;'>- {fmt(reklama_napay)}</p>", unsafe_allow_html=True)

        st.divider()
        st.markdown(f"**До ИПН: {fmt(do_ipn)}**")
        r1, r2 = st.columns([3, 2])
        r1.caption("[авто] ИПН 10%")
        r2.markdown(f"<p style='text-align:right;color:#A32D2D;font-weight:500;'>- {fmt(ipn)}</p>", unsafe_allow_html=True)
        st.divider()
        st.markdown(f"### Таза пайда: :green[{fmt(profit)}]")
        if for_pay > 0:
            st.caption(f"Рентабельность: {profit/for_pay*100:.1f}%")

    with col_right:
        st.markdown("**🧾 НДС есебі**")
        st.markdown(f"- НДС жалпы: {fmt(ndv_total)}")
        st.markdown(f"- НДС приход (себест×16/116): {fmt(ndv_prikhod)}")
        st.markdown(f"- **НДС наше: :red[-{fmt(ndv_nashe)}]**")
        st.divider()

        st.markdown("**📊 Сатылым**")
        st.markdown(f"- Жалпы сатылды: **{fmtN(total_qty)} шт**")
        st.markdown(f"- Возврат: :red[**{fmtN(vozvrat_qty)} шт**]")
        st.markdown(f"- Нақты: **{fmtN(total_qty - vozvrat_qty)} шт**")
        st.markdown(f"- Хранение: :red[{fmt(storage)}]")
        st.markdown(f"- Операции на приёмке: :red[{fmt(priemka)}]")
        st.markdown(f"- Штраф: :red[{fmt(penalty)}]")
        st.markdown(f"- Упаковка жалпы: :red[{fmt(upakovka)}]")

    st.divider()

    # ТАУАР БОЙЫНША
    if by_article:
        with st.expander("📦 Тауар бойынша таза пайда", expanded=True):
            prod_rows = []
            for art, data in by_article.items():
                qty_a = data.get("qty", 0)
                wb_a = data.get("for_pay", 0)
                vozvrat_a = data.get("vozvrat", 0)
                sebest_a = seb_data.get(art, 0)
                share = wb_a / for_pay if for_pay > 0 else 0
                napay_a = wb_a - (ads*share) - (storage*share) - (vozvrat_a*2)
                ndv_a = napay_a * ndv_rate
                ndvpr_a = (sebest_a * qty_a) * ndv_rate
                ndvn_a = ndv_a - ndvpr_a
                pack_a = qty_a * 100
                doipn_a = napay_a - ndvn_a - (sebest_a*qty_a) - pack_a - (logistic*share) - (samovykup*share) - (reklama_napay*share)
                ipn_a = doipn_a * 0.10 if doipn_a > 0 else 0
                profit_a = doipn_a - ipn_a
                pct_a = profit_a / wb_a * 100 if wb_a > 0 else 0
                prod_rows.append({
                    "Артикул": art,
                    "Сатылды (шт)": qty_a,
                    "WB түскен (₸)": round(wb_a),
                    "Себест/шт (₸)": sebest_a,
                    "Упаковка (₸)": pack_a,
                    "Таза пайда (₸)": round(profit_a),
                    "%": round(pct_a, 1),
                })
            prod_df = pd.DataFrame(prod_rows)

            def style_profit(val):
                if pd.isna(val): return ""
                return "color: #3B6D11; font-weight: bold" if val >= 0 else "color: #A32D2D; font-weight: bold"

            styled = prod_df.style.map(style_profit, subset=["Таза пайда (₸)", "%"])
            st.dataframe(styled, use_container_width=True, height=400)

    st.divider()

    # СЕБЕСТОИМОСТЬ КЕСТЕСІ — тек менеджер өзгерте алады
    with st.expander("🗂️ Себестоимость — тауар бойынша", expanded=False):
        if by_article:
            seb_rows = []
            for art, data in by_article.items():
                qty_a = data.get("qty", 0)
                sebest_a = seb_data.get(art, 0)
                seb_rows.append({
                    "Артикул": art,
                    "Сатылды (шт)": qty_a,
                    "Себест/шт (₸)": sebest_a,
                    "Упаковка (₸)": qty_a * 100,
                    "Жалпы (₸)": qty_a * sebest_a,
                })
            seb_df = pd.DataFrame(seb_rows)

            if role == "manager":
                st.caption("✏️ Себест/шт бағанын өзгертіп Enter басыңыз — барлығы автоматты жаңарады")
                edited_seb = st.data_editor(
                    seb_df, use_container_width=True, height=400,
                    column_config={
                        "Артикул": st.column_config.TextColumn(disabled=True),
                        "Сатылды (шт)": st.column_config.NumberColumn(format="%d шт", disabled=True),
                        "Себест/шт (₸)": st.column_config.NumberColumn(format="%d ₸", min_value=0),
                        "Упаковка (₸)": st.column_config.NumberColumn(format="%d ₸", disabled=True),
                        "Жалпы (₸)": st.column_config.NumberColumn(format="%d ₸", disabled=True),
                    }
                )
                # Себест өзгерсе сақтаймыз
                new_seb = dict(zip(edited_seb["Артикул"], edited_seb["Себест/шт (₸)"]))
                if new_seb != seb_data:
                    all_seb[str(idx)] = new_seb
                    save_json(SEBEST_FILE, all_seb)
                    st.rerun()
            else:
                st.dataframe(seb_df, use_container_width=True, height=400)

# ──────────────────────────────────────────────
# БІР МАГАЗИН КӨРСЕТУ
# ──────────────────────────────────────────────
def show_store(store, df, sales30, filter_status, search):
    idx = store["idx"]
    role = st.session_state.get("role", "manager")

    if df.empty:
        st.warning("Деректер жоқ немесе жүктелмеді")
        return

    tab_ostatok, tab_analytic, tab_finance = st.tabs([
        "📦 Остатки", "📊 Аналитика — 30 күн", "💰 Финансы"
    ])

    # ── АНАЛИТИКА ──
    with tab_analytic:
        if sales30 is None or sales30.empty:
            st.info("Деректер жоқ")
        else:
            total_qty = int(sales30["Заказ (шт)"].sum())
            total_rev = sales30["Выручка (₸)"].sum()
            avg_day = total_qty / 30
            best_day = sales30.loc[sales30["Заказ (шт)"].idxmax()]
            c1, c2, c3, c4 = st.columns(4)
            c1.metric("📦 Жалпы заказ", f"{total_qty:,} шт".replace(",", " "))
            c2.metric("💰 Жалпы выручка", f"{total_rev:,.0f} ₸".replace(",", " "))
            c3.metric("📈 Күндік орта", f"{avg_day:.1f} шт")
            c4.metric("🏆 Ең жақсы күн", f"{best_day['Дата']} — {best_day['Заказ (шт)']} шт")
            st.divider()
            st.markdown("#### 📊 Күн бойынша заказдар (соңғы 30 күн)")
            st.bar_chart(sales30.set_index("Дата")[["Заказ (шт)"]], height=300)
            st.markdown("#### 💰 Күн бойынша выручка")
            st.line_chart(sales30.set_index("Дата")[["Выручка (₸)"]], height=250)
            st.divider()
            st.markdown("#### 📋 Күнделікті кесте")
            disp = sales30.copy()
            disp["Выручка (₸)"] = disp["Выручка (₸)"].round(0).astype(int)
            st.dataframe(disp.sort_values("Дата", ascending=False).reset_index(drop=True),
                         use_container_width=True, height=400)

    # ── ФИНАНСЫ ──
    with tab_finance:
        show_finance_tab(store, df)

    # ── ОСТАТКИ ──
    with tab_ostatok:
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

        c1, c2, c3, c4 = st.columns(4)
        c1.metric("📦 Общий остаток", f"{int(df['total'].sum()):,} шт".replace(",", " "))
        c2.metric("📊 Позиций", len(df))
        c3.metric("🔴 Критические", int(df["turnover"].dropna().apply(lambda x: x <= 10).sum()))
        c4.metric("⚫ Ноль остаток", int((df["qty"] == 0).sum()))
        st.divider()
        st.markdown("#### 📊 Итоговый отчёт")

        fbo_key = f"fbo_{idx}"
        all_fbo = load_json(FBO_FILE)
        fbo_data = all_fbo.get(str(idx), {})
        st.session_state[fbo_key] = fbo_data

        result = dff[["supplierArticle", "qty", "in_way_client", "daily_avg", "status"]].copy()
        result["FBO в пути"] = result["supplierArticle"].map(fbo_data).fillna(0).astype(int)
        result["Общий остаток"] = result["qty"] + result["in_way_client"] + result["FBO в пути"]
        result["Оборачиваемость"] = result.apply(
            lambda r: round(r["Общий остаток"] / r["daily_avg"]) if r["daily_avg"] > 0 else None, axis=1
        )
        result = result.rename(columns={
            "supplierArticle": "Артикул", "qty": "Остаток",
            "in_way_client": "В пути к клиенту", "daily_avg": "Ср. продаж/день", "status": "Статус"
        })
        result = result[["Артикул", "Остаток", "В пути к клиенту", "FBO в пути",
                         "Общий остаток", "Ср. продаж/день", "Оборачиваемость", "Статус"]]

        def style_turn(val):
            if pd.isna(val): return ""
            return "background-color:#FCEBEB;color:#A32D2D;font-weight:bold" if val <= 10 else ""
        def style_stat(val):
            m = {"Ноль":"background-color:#FCEBEB;color:#A32D2D","Мало":"background-color:#FAEEDA;color:#854F0B",
                 "Хорошо":"background-color:#EAF3DE;color:#3B6D11","Достаточно":"background-color:#E6F1FB;color:#185FA5"}
            return m.get(val, "")

        styled = result.style.map(style_turn, subset=["Оборачиваемость"]).map(style_stat, subset=["Статус"])
        st.dataframe(styled, use_container_width=True, height=460,
            column_config={
                "Остаток": st.column_config.NumberColumn(format="%d шт"),
                "В пути к клиенту": st.column_config.NumberColumn(format="%d шт"),
                "FBO в пути": st.column_config.NumberColumn(format="%d шт"),
                "Общий остаток": st.column_config.NumberColumn(format="%d шт"),
                "Ср. продаж/день": st.column_config.NumberColumn(format="%.1f"),
                "Оборачиваемость": st.column_config.NumberColumn(format="%d дн"),
            })
        st.caption(f"Показано: {len(result)} позиций")

        buf = io.BytesIO()
        with pd.ExcelWriter(buf, engine="openpyxl") as writer:
            result.to_excel(writer, index=False, sheet_name="Остатки WB")
        st.download_button(f"⬇️ Excel — {store['name']}", data=buf.getvalue(),
            file_name=f"WB_{store['name']}_{datetime.now().strftime('%Y%m%d_%H%M')}.xlsx",
            mime="application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
            key=f"dl_{idx}")

        if role == "manager":
            st.divider()
            st.markdown("#### ✏️ FBO в пути")
            fbo_tbl = dff[["supplierArticle"]].copy()
            fbo_tbl.columns = ["Артикул"]
            fbo_tbl["FBO в пути"] = fbo_tbl["Артикул"].map(fbo_data).fillna(0).astype(int)
            fbo_edited = st.data_editor(fbo_tbl, use_container_width=True, height=400, key=f"fbo_editor_{idx}",
                column_config={
                    "Артикул": st.column_config.TextColumn(disabled=True),
                    "FBO в пути": st.column_config.NumberColumn(format="%d шт", min_value=0),
                })
            new_fbo = dict(zip(fbo_edited["Артикул"], fbo_edited["FBO в пути"]))
            if new_fbo != fbo_data:
                all_fbo[str(idx)] = new_fbo
                save_json(FBO_FILE, all_fbo)
                st.rerun()

# ──────────────────────────────────────────────
# SIDEBAR
# ──────────────────────────────────────────────
stores = get_stores()
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
            has_fin = "✅" if s["finance_key"] else "⚠️"
            st.markdown(f"**{s['name']}** {has_fin}")
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

# ──────────────────────────────────────────────
# НЕГІЗГІ
# ──────────────────────────────────────────────
st.title("📦 Wildberries отчёт")
st.caption(f"Обновлено: {datetime.now().strftime('%d.%m.%Y %H:%M')}")

if not visible_stores:
    st.info("👈 Secrets-ке магазин деректерін қосыңыз")
    st.stop()

if fetch_btn:
    for s in visible_stores:
        df, sales30, errors = load_store_data(s)
        st.session_state[f"df_{s['idx']}"] = df
        st.session_state[f"sales30_{s['idx']}"] = sales30
        for e in errors:
            st.warning(f"[{s['name']}] ⚠️ {e}")
    st.success("✅ Барлық магазин жүктелді!")
    st.rerun()

tab_names = [s["name"] for s in visible_stores]
tabs = st.tabs(tab_names)

for tab, store in zip(tabs, visible_stores):
    with tab:
        df_key = f"df_{store['idx']}"
        if df_key not in st.session_state or st.session_state[df_key] is None:
            st.info("👈 **«Барлығын жүктеу»** батырмасын басыңыз")
        else:
            sales30 = st.session_state.get(f"sales30_{store['idx']}", pd.DataFrame())
            show_store(store, st.session_state[df_key], sales30, filter_status, search)
