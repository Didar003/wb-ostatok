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

# Тұрақты деректер қоймасы — app директориясында
DATA_DIR = os.path.join(os.path.dirname(os.path.abspath(__file__)), "wb_data")
os.makedirs(DATA_DIR, exist_ok=True)

FBO_FILE    = os.path.join(DATA_DIR, "fbo_data.json")
SEBEST_FILE = os.path.join(DATA_DIR, "sebest_data.json")

def _tmp(name):
    """Уақытша файлдар (автожауап, жалоб) — сессия ішінде ғана"""
    return os.path.join(DATA_DIR, name)

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
        os.makedirs(os.path.dirname(path), exist_ok=True)
        with open(path, "w", encoding="utf-8") as f:
            json.dump(data, f, ensure_ascii=False, indent=2)
    except Exception as e:
        st.warning(f"Сақталмады: {e}")

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
        feedback_key = st.secrets.get(f"STORE_{i}_FEEDBACK", "")
        if stats_key:
            stores.append({
                "name": name, "idx": i,
                "stats_key": stats_key,
                "analytics_key": analytics_key,
                "finance_key": finance_key,
                "feedback_key": feedback_key,
            })
    return stores

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
    if not rows:
        return {}
    result = {
        "for_pay": 0,
        "ads": 0,
        "storage": 0,
        "penalty": 0,
        "logistic": 0,
        "priemka": 0,
        "vozvrat": 0,
        "vozvrat_qty": 0,
        "total_qty": 0,
        "by_article": {}
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
            result["logistic"] += abs(delivery_rub)
        elif "ОБРАБОТКА" in oper_up:
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
            result["ads"] += abs(deduct) + abs(ppvz)

    if result["logistic"] == 0:
        for row in rows:
            result["logistic"] += abs(float(row.get("delivery_rub", 0) or 0))
    return result

def status_label(q):
    if q == 0:    return "Ноль"
    if q <= 200:  return "Мало"
    if q <= 500:  return "Хорошо"
    return "Достаточно"

def load_store_data(store):
    idx = store["idx"]
    stats_key = store["stats_key"]
    analytics_key = store["analytics_key"]
    name = store["name"]
    errors = []

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


FEEDBACK_BASE = "https://feedbacks-api.wildberries.ru"

def fetch_feedbacks(fb_key, is_answered=False, take=20):
    try:
        r = requests.get(
            f"{FEEDBACK_BASE}/api/v1/feedbacks",
            headers={"Authorization": fb_key},
            params={"isAnswered": str(is_answered).lower(), "take": take, "skip": 0, "order": "dateDesc"},
            timeout=30
        )
        if r.status_code not in (200, 201, 204):
            st.error(f"Feedbacks API қатесі: {r.status_code} — {r.text[:200]}")
            return []
        data = r.json()
        feedbacks = data.get("data", {}).get("feedbacks", []) or []
        return feedbacks
    except Exception as e:
        st.error(f"Feedbacks қатесі: {e}")
        return []

def fetch_questions(fb_key, is_answered=False, take=20):
    try:
        r = requests.get(
            f"{FEEDBACK_BASE}/api/v1/questions",
            headers={"Authorization": fb_key},
            params={"isAnswered": str(is_answered).lower(), "take": take, "skip": 0, "order": "dateDesc"},
            timeout=30
        )
        if r.status_code not in (200, 201, 204):
            st.error(f"Questions API қатесі: {r.status_code} — {r.text[:200]}")
            return []
        data = r.json()
        return data.get("data", {}).get("questions", []) or data.get("questions", []) or []
    except Exception as e:
        st.error(f"Questions қатесі: {e}")
        return []

def send_feedback_reply(fb_key, feedback_id, text):
    try:
        r = requests.patch(
            f"{FEEDBACK_BASE}/api/v1/feedbacks/answer",
            headers={"Authorization": fb_key, "Content-Type": "application/json"},
            json={"id": feedback_id, "text": text},
            timeout=30
        )
        if r.status_code not in (200, 201, 204):
            st.error(f"WB жауап қатесі {r.status_code}: {r.text[:300]}")
        return r.status_code in (200, 201, 204)
    except Exception as e:
        st.error(f"send_feedback_reply қатесі: {e}")
        return False

def send_question_reply(fb_key, question_id, text):
    try:
        r = requests.patch(
            f"{FEEDBACK_BASE}/api/v1/questions",
            headers={"Authorization": fb_key, "Content-Type": "application/json"},
            json={"id": question_id, "answer": {"text": text}, "state": "wbRu"},
            timeout=30
        )
        if r.status_code not in (200, 201, 204):
            st.error(f"WB сұрақ қатесі {r.status_code}: {r.text[:300]}")
        return r.status_code in (200, 201, 204)
    except Exception as e:
        st.error(f"send_question_reply қатесі: {e}")
        return False

def send_feedback_complaint(fb_key, feedback_id):
    try:
        r = requests.post(
            f"{FEEDBACK_BASE}/api/v1/feedbacks/report",
            headers={"Authorization": fb_key, "Content-Type": "application/json"},
            json={"id": feedback_id, "reason": "incorrect_goods_description"},
            timeout=30
        )
        return r.status_code in (200, 201, 204)
    except:
        return False

def ai_generate_reply(product_name, review_text, rating, reply_type="feedback", pros="", cons="", bables="", order_status=""):
    """Claude API арқылы ИИ жауап жасау — 529 retry қосылған"""
    try:
        if reply_type == "feedback":
            is_rejected = order_status in ("rejected", "cancelled", "canceled")
            system = (
                "Ты — вежливый менеджер магазина косметики на Wildberries.\n"
                "Правила:\n"
                "- Отвечай строго по содержанию отзыва\n"
                "- 2-3 предложения максимум\n"
                "- Не копируй текст отзыва обратно\n"
                "- Не используй: Спасибо за отзыв, Мы рады, Будем рады видеть вас снова\n"
                + ("- Покупатель ОТКАЗАЛСЯ от товара или сделал ВОЗВРАТ. Не предлагай замену или другой заказ. Извинись и предложи обратиться в поддержку WB.\n" if is_rejected else
                   "- Если отзыв положительный — коротко подтверди и порекомендуй другие товары\n"
                   "- Если негативный — извинись, предложи решение (замену, возврат, связаться с поддержкой)\n"
                   "- Если смешанный — отреагируй на минус и похвали плюс\n")
                + "- Пиши только на русском языке"
            )
            parts = ["Товар: " + product_name, "Оценка: " + str(rating) + " из 5"]
            if order_status in ("rejected", "cancelled", "canceled"):
                parts.append("Статус заказа: ОТКАЗ/ВОЗВРАТ")
            if pros:
                parts.append("Плюсы: " + pros)
            if cons:
                parts.append("Минусы: " + cons)
            if bables:
                parts.append("Жалобы покупателя: " + bables)
            if review_text:
                parts.append("Комментарий: " + review_text)
            parts.append("\nНапиши ответ продавца:")
            prompt = "\n".join(parts)
        else:
            system = (
                "Ты — компетентный менеджер магазина косметики на Wildberries.\n"
                "Правила:\n"
                "- Отвечай КОНКРЕТНО на заданный вопрос\n"
                "- Если вопрос про конкретный товар — дай полезный ответ про этот товар\n"
                "- Если не знаешь точный ответ — направь к описанию товара или предложи связаться\n"
                "- 2-3 предложения максимум\n"
                "- Не используй шаблонные фразы\n"
                "- Пиши только на русском языке"
            )
            prompt = "Товар: " + product_name + "\nВопрос покупателя: " + review_text + "\n\nНапиши ответ продавца на вопрос:"

        anthropic_key = st.secrets.get("ANTHROPIC_API_KEY", "")
        if not anthropic_key:
            return "⚠️ ANTHROPIC_API_KEY Secrets-ке қосылмаған"

        # 529 қатесі болса 3 рет қайталайды
        for attempt in range(3):
            r = requests.post(
                "https://api.anthropic.com/v1/messages",
                headers={
                    "Content-Type": "application/json",
                    "x-api-key": anthropic_key,
                    "anthropic-version": "2023-06-01"
                },
                json={
                    "model": "claude-sonnet-4-5",
                    "max_tokens": 300,
                    "system": system,
                    "messages": [{"role": "user", "content": prompt}]
                },
                timeout=30
            )
            if r.status_code == 529:
                time.sleep(10)
                continue
            r.raise_for_status()
            return r.json()["content"][0]["text"].strip()
        return "⚠️ Anthropic сервері бос емес, кейінірек қайталаңыз"

    except Exception as e:
        return f"ИИ қатесі: {e}"

def render_stars(rating):
    filled = "★" * rating
    empty = "☆" * (5 - rating)
    color = "#E24B4A" if rating <= 3 else "#F0C040"
    return f'<span style="color:{color};font-size:15px;">{filled}{empty}</span>'

def show_feedback_tab(store):
    idx = store["idx"]
    fb_key = store.get("feedback_key", "")

    if not fb_key:
        st.warning("⚠️ Secrets-ке STORE_{n}_FEEDBACK токенін қосыңыз")
        return

    # Деректерді жүктеу
    load_key = f"fb_data_{idx}"
    if load_key not in st.session_state:
        st.session_state[load_key] = None

    if st.button("🔄 Жүктеу", key=f"fb_load_{idx}", use_container_width=False):
        with st.spinner("Жүктелуде..."):
            feedbacks = fetch_feedbacks(fb_key, is_answered=False, take=30)
            questions = fetch_questions(fb_key, is_answered=False, take=30)
            st.session_state[load_key] = {
                "feedbacks": feedbacks,
                "questions": questions,
            }
            st.rerun()

    data = st.session_state[load_key]
    if data is None:
        st.info("👆 **«Жүктеу»** батырмасын басыңыз")
        return

    feedbacks = data.get("feedbacks", [])
    questions = data.get("questions", [])
    auto_replied = load_json(_tmp(f"auto_replied_{idx}.json"))

    # Метрикалар
    low_star = [f for f in feedbacks if f.get("productValuation", 5) <= 3]
    c1, c2, c3, c4 = st.columns(4)
    c1.metric("⭐ Жаңа отзыв", len(feedbacks))
    c2.metric("❓ Авто вопросы", len(questions), help="Сұрақтарға қолмен жауап береді")
    c3.metric("🤖 Жауап жіберілді", len(auto_replied))
    c4.metric("🔴 1-3 жұлдыз", len(low_star))

    st.divider()

    # ── 2 ТАБ ──
    t1, t2 = st.tabs([
        f"⭐ Отзывы ({len(feedbacks)})",
        f"❓ Авто вопросы ({len(questions)})",
    ])

    # ОТЗЫВЫ
    with t1:
        if not feedbacks:
            st.success("✅ Жауапсыз отзыв жоқ!")
        for fb in feedbacks:
            fb_id = fb.get("id", "")
            rating = fb.get("productValuation") or fb.get("rating") or 0
            pd_ = fb.get("productDetails", {}) or {}
            text = fb.get("text", "") or ""
            bables = fb.get("bables", []) or []
            bables_text = ", ".join(bables) if bables else ""
            pros = fb.get("pros", "") or ""
            cons = fb.get("cons", "") or ""
            order_status = fb.get("orderStatus", "") or ""
            product = pd_.get("productName", "") or fb.get("productName", "") or ""
            created = fb.get("createdDate", "")[:10] if fb.get("createdDate") else ""

            preview_key_fb = f"preview_fb_{fb_id}"

            with st.container():
                col1, col2 = st.columns([6, 2])
                with col1:
                    st.markdown(render_stars(rating) + f' &nbsp; <span style="font-size:12px;color:gray;">{product} · {created}</span>', unsafe_allow_html=True)
                    if bables_text:
                        st.caption(f"⚠️ {bables_text}")
                    if text:
                        st.caption(f'"{text[:200]}{"..." if len(text)>200 else ""}"')
                with col2:
                    if rating <= 3:
                        st.caption("🔴 1-3 жұлдыз")

                # Жіберілген жауап
                if fb_id in auto_replied:
                    reply_text = auto_replied[fb_id]
                    if "ИИ қатесі" in reply_text or "Error" in reply_text or "401" in reply_text or "404" in reply_text:
                        del auto_replied[fb_id]
                        save_json(_tmp(f"auto_replied_{idx}.json"), auto_replied)
                        st.warning("⚠️ Қате жауап тазаланды")
                    else:
                        st.success("✅ Опубликован")
                        with st.expander("Жауапты көру"):
                            st.caption(reply_text)

                # Preview режимі
                elif preview_key_fb in st.session_state:
                    preview_text = st.session_state[preview_key_fb]
                    edited = st.text_area("✏️ ИИ жауабы — өзгертуге болады:", value=preview_text, key=f"edit_fb_{fb_id}", height=100)
                    c1, c2 = st.columns(2)
                    with c1:
                        if st.button("📤 Опубликовать", key=f"pub_fb_{fb_id}", use_container_width=True):
                            with st.spinner("Жіберілуде..."):
                                time.sleep(1.1)
                                ok = send_feedback_reply(fb_key, fb_id, edited)
                                if ok:
                                    auto_replied[fb_id] = edited
                                    save_json(_tmp(f"auto_replied_{idx}.json"), auto_replied)
                                    del st.session_state[preview_key_fb]
                                    st.rerun()
                                else:
                                    st.error("Жіберілмеді")
                    with c2:
                        if st.button("🗑 Жою", key=f"del_fb_{fb_id}", use_container_width=True):
                            del st.session_state[preview_key_fb]
                            st.rerun()

                # ИИ жауап батырмасы
                else:
                    if st.button("🤖 ИИ жауап жасау", key=f"ai_fb_{fb_id}", use_container_width=True):
                        with st.spinner("ИИ жазып жатыр..."):
                            reply = ai_generate_reply(product, text, rating, "feedback", pros, cons, bables_text, order_status)
                            st.session_state[preview_key_fb] = reply
                            st.rerun()

                st.divider()

    # ВОПРОСЫ
    with t2:
        if not questions:
            st.success("✅ Жауапсыз сұрақ жоқ!")
        for q in questions:
            q_id = q.get("id", "")
            q_text = q.get("text", "") or ""
            pd_q = q.get("productDetails", {}) or {}
            product = pd_q.get("productName", "") or q.get("productName", "") or ""
            created = q.get("createdDate", "")[:10] if q.get("createdDate") else ""
            preview_key_q = f"preview_q_{q_id}"

            with st.container():
                col1, col2 = st.columns([6, 2])
                with col1:
                    st.markdown(f'❓ <span style="font-size:12px;color:gray;">{product} · {created}</span>', unsafe_allow_html=True)
                    if q_text:
                        st.caption(f'"{q_text[:200]}{"..." if len(q_text)>200 else ""}"')
                with col2:
                    pass

                # Жіберілген жауап
                if q_id in auto_replied:
                    reply_text = auto_replied[q_id]
                    if "ИИ қатесі" in reply_text or "Error" in reply_text or "401" in reply_text:
                        del auto_replied[q_id]
                        save_json(_tmp(f"auto_replied_{idx}.json"), auto_replied)
                        st.warning("⚠️ Қате жауап тазаланды")
                    else:
                        st.success("✅ Опубликован")
                        with st.expander("Жауапты көру"):
                            st.caption(reply_text)

                # Preview режимі
                elif preview_key_q in st.session_state:
                    preview_text = st.session_state[preview_key_q]
                    edited = st.text_area("✏️ ИИ жауабы — өзгертуге болады:", value=preview_text, key=f"edit_q_{q_id}", height=100)
                    c1, c2 = st.columns(2)
                    with c1:
                        if st.button("📤 Опубликовать", key=f"pub_q_{q_id}", use_container_width=True):
                            with st.spinner("Жіберілуде..."):
                                time.sleep(1.1)
                                ok = send_question_reply(fb_key, q_id, edited)
                                if ok:
                                    auto_replied[q_id] = edited
                                    save_json(_tmp(f"auto_replied_{idx}.json"), auto_replied)
                                    del st.session_state[preview_key_q]
                                    st.rerun()
                                else:
                                    st.error("Жіберілмеді")
                    with c2:
                        if st.button("🗑 Жою", key=f"del_q_{q_id}", use_container_width=True):
                            del st.session_state[preview_key_q]
                            st.rerun()

                # ИИ жауап батырмасы
                else:
                    if st.button("🤖 ИИ жауап жасау", key=f"ai_q_{q_id}", use_container_width=True):
                        with st.spinner("ИИ жазып жатыр..."):
                            reply = ai_generate_reply(product, q_text, 5, "question")
                            st.session_state[preview_key_q] = reply
                            st.rerun()

                st.divider()


def show_finance_tab(store, df):
    idx = store["idx"]
    stats_key = store["stats_key"]
    finance_key = store["finance_key"]
    name = store["name"]
    role = st.session_state.get("role", "manager")

    st.markdown("#### 💰 Финансы отчет")

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

    period_key = f"fin_period_{idx}"
    current_period = f"{date_from}_{date_to}"
    if st.session_state.get(period_key) != current_period:
        if fin_key in st.session_state:
            del st.session_state[fin_key]
        st.session_state[period_key] = current_period

    if load_fin:
        with st.spinner(f"[{name}] Финансы отчеті жүктелуде..."):
            try:
                rows = fetch_report_detail(
                    use_key,
                    date_from.strftime("%Y-%m-%d"),
                    date_to.strftime("%Y-%m-%d"),
                    store_name=name
                )
                fin = parse_finance(rows)
                st.session_state[fin_key] = fin
                st.success("✅ Жүктелді!")
            except Exception as e:
                st.error(f"Қате: {e}")

    if fin_key not in st.session_state:
        st.info("👆 Период таңдап **«Финансы жүктеу»** батырмасын басыңыз")
        return

    fin = st.session_state[fin_key]

    man_key = f"fin_manual_{idx}"
    if man_key not in st.session_state:
        st.session_state[man_key] = {"logistic": 0, "samovykup": 0, "reklama_napay": 0}
    man = st.session_state[man_key]

    for_pay = fin.get("for_pay", 0)
    ads = fin.get("ads", 0)
    storage = fin.get("storage", 0)
    penalty = fin.get("penalty", 0)
    logistic_auto = fin.get("logistic", 0)
    vozvrat = fin.get("vozvrat", 0)
    vozvrat_qty = fin.get("vozvrat_qty", 0)
    total_qty = fin.get("total_qty", 0)
    vozvrat_shygyn = vozvrat * 2

    logistic = man["logistic"]
    samovykup = man["samovykup"]
    reklama_napay = man["reklama_napay"]

    seb_key = f"sebest_{idx}"
    all_seb = load_json(SEBEST_FILE)
    seb_data = all_seb.get(str(idx), {})

    articles = []
    if not df.empty:
        articles = df["supplierArticle"].tolist()
    by_article = fin.get("by_article", {})

    tot_seb = sum(seb_data.get(a, 0) * by_article.get(a, {}).get("qty", 0) for a in by_article)
    tot_qty_sold = sum(by_article.get(a, {}).get("qty", 0) for a in by_article)
    upakovka = tot_qty_sold * 100

    priemka = fin.get("priemka", 0)
    napay = for_pay - ads - logistic_auto - storage - priemka - penalty - vozvrat_shygyn
    ndv_rate = 16/116
    ndv_total = napay * ndv_rate
    ndv_prikhod = tot_seb * ndv_rate
    ndv_nashe = ndv_total - ndv_prikhod
    do_ipn = napay - tot_seb
    ipn = do_ipn * 0.10 if do_ipn > 0 else 0
    profit = do_ipn - ipn - ndv_nashe - logistic - upakovka - samovykup - reklama_napay

    def fmt(n): return f"{round(n):,} ₸".replace(",", " ")
    def fmtN(n): return f"{round(n):,}".replace(",", " ")

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

        if role == "manager":
            new_log = st.number_input("[қол] Логистика до склада (₸)", value=float(man["logistic"]), min_value=0.0, step=1000.0, key=f"log_{idx}")
            new_samo = st.number_input("[қол] Самовыкуп (₸)", value=float(man["samovykup"]), min_value=0.0, step=1000.0, key=f"samo_{idx}")
            new_rek = st.number_input("[қол] Реклама на пэй (сыртқы, ₸)", value=float(man["reklama_napay"]), min_value=0.0, step=1000.0, key=f"rek_{idx}")
            if new_log != man["logistic"] or new_samo != man["samovykup"] or new_rek != man["reklama_napay"]:
                st.session_state[man_key] = {"logistic": new_log, "samovykup": new_samo, "reklama_napay": new_rek}
                st.rerun()
        else:
            r1, r2 = st.columns([3, 2])
            r1.caption("[қол] Логистика до склада")
            r2.markdown(f"<p style='text-align:right;'>- {fmt(logistic)}</p>", unsafe_allow_html=True)
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
    if by_article:
        excel_rows = []
        for art, data in by_article.items():
            qty_a = data.get("qty", 0)
            wb_a = data.get("for_pay", 0)
            vozvrat_a = data.get("vozvrat", 0)
            sebest_a = seb_data.get(art, 0)
            reklama_a = 0
            share = wb_a / for_pay if for_pay > 0 else 0
            napay_a = wb_a - (storage*share) - (priemka*share) - (vozvrat_a*2)
            ndv_a = napay_a * ndv_rate
            ndvpr_a = (sebest_a * qty_a) * ndv_rate
            ndvn_a = ndv_a - ndvpr_a
            pack_a = qty_a * 100
            doipn_a = napay_a - ndvn_a - (sebest_a*qty_a) - pack_a - (logistic*share) - (samovykup*share) - reklama_a
            ipn_a = doipn_a * 0.10 if doipn_a > 0 else 0
            profit_a = doipn_a - ipn_a
            pct_a = profit_a / wb_a * 100 if wb_a > 0 else 0
            excel_rows.append({
                "Артикул": art,
                "Сатылды (шт)": qty_a,
                "WB түскен (₸)": round(wb_a),
                "Себест/шт (₸)": sebest_a,
                "Реклама (₸)": reklama_a,
                "Таза пайда (₸)": round(profit_a),
                "Рентабельность (%)": round(pct_a, 1),
            })

        summary = {
            "Артикул": "ЖАЛПЫ",
            "Сатылды (шт)": total_qty,
            "WB түскен (₸)": round(for_pay),
            "Себест/шт (₸)": "",
            "Реклама (₸)": sum(r["Реклама (₸)"] for r in excel_rows),
            "Таза пайда (₸)": round(profit),
            "Рентабельность (%)": round(profit/for_pay*100, 1) if for_pay > 0 else 0,
        }
        excel_rows.append(summary)

        buf = io.BytesIO()
        import openpyxl as _oxl
        from openpyxl.styles import Font, PatternFill, Border, Side
        _wb2 = _oxl.Workbook()
        ws = _wb2.active
        ws.title = "Финансы отчет"

        GREEN_FILL       = PatternFill("solid", fgColor="92D050")
        LIGHT_GREEN_FILL = PatternFill("solid", fgColor="E2EFDA")
        BLUE_FILL        = PatternFill("solid", fgColor="BDD7EE")
        RED_FILL         = PatternFill("solid", fgColor="FF7F7F")
        BOLD   = Font(bold=True)
        BOLD12 = Font(bold=True, size=12)

        def thin_border():
            s = Side(style="thin")
            return Border(left=s, right=s, top=s, bottom=s)

        def apply_border(ws, min_row, max_row, min_col, max_col):
            for r in range(min_row, max_row+1):
                for c in range(min_col, max_col+1):
                    ws.cell(r, c).border = thin_border()

        row = 1
        ws.cell(row, 1, "ЖАЛПЫ ОТЧЕТ").font = BOLD12
        row += 1
        general_data = [
            ("авто", "К перечислению",      round(for_pay),         None,        False),
            ("авто", "Удержания (реклама)", -round(ads),             None,        False),
            ("авто", "Логистика WB",        -round(logistic_auto),  None,        False),
            ("авто", "Хранение",            -round(storage),        None,        False),
            ("авто", "Операции на приёмке", -round(priemka),        None,        False),
            ("авто", "Штраф",               -round(penalty),        None,        False),
            ("авто", "Возврат × 2",         -round(vozvrat_shygyn), None,        False),
            ("авто", "На пэй",              round(napay),           BLUE_FILL,   True),
            ("авто", "НДС наше",            -round(ndv_nashe),      None,        False),
            ("авто", "Себестоимость",       -round(tot_seb),        None,        False),
            ("авто", "Упаковка",            -round(upakovka),       None,        False),
            ("қол",  "Логистика до склада", -round(logistic),       None,        False),
            ("қол",  "Самовыкуп",           -round(samovykup),      None,        False),
            ("қол",  "Реклама на пэй",      -round(reklama_napay),  None,        False),
            ("авто", "До ИПН",              round(do_ipn),          None,        False),
            ("авто", "ИПН 10%",             -round(ipn),            None,        False),
            ("авто", "ТАЗА ПАЙДА",          round(profit),          "profit",    True),
            ("авто", "Рентабельность",      f"{profit/for_pay*100:.1f}%" if for_pay > 0 else "0%", "profit", False),
        ]
        gen_start = row
        for tag, label, val, fill, bold in general_data:
            c1 = ws.cell(row, 1, f"[{tag}] {label}")
            c2 = ws.cell(row, 2, val)
            actual_fill = (GREEN_FILL if profit >= 0 else RED_FILL) if fill == "profit" else fill
            if actual_fill:
                c1.fill = actual_fill
                c2.fill = actual_fill
            if bold:
                c1.font = BOLD
                c2.font = BOLD
            row += 1
        apply_border(ws, gen_start, row-1, 1, 2)

        row += 1
        ws.cell(row, 1, "ТАУАР БОЙЫНША ТАЗА ПАЙДА").font = BOLD12
        row += 1
        prod_headers = ["Артикул", "Сатылды (шт)", "WB түскен (₸)", "Себест/шт (₸)", "Реклама (₸)", "Таза пайда (₸)", "Рентабельность (%)"]
        prod_hdr_row = row
        for col, h in enumerate(prod_headers, 1):
            c = ws.cell(row, col, h)
            c.font = BOLD
            c.fill = LIGHT_GREEN_FILL
        row += 1
        for r in excel_rows:
            vals = [r["Артикул"], r["Сатылды (шт)"], r["WB түскен (₸)"],
                    r["Себест/шт (₸)"], r["Реклама (₸)"], r["Таза пайда (₸)"], r["Рентабельность (%)"]]
            for col, v in enumerate(vals, 1):
                ws.cell(row, col, v)
            pval = r["Таза пайда (₸)"]
            if r["Артикул"] == "ЖАЛПЫ":
                pf = GREEN_FILL if (profit >= 0) else RED_FILL
                for col in range(1, 8):
                    ws.cell(row, col).font = BOLD
                    ws.cell(row, col).fill = pf
            elif isinstance(pval, (int, float)) and pval < 0:
                ws.cell(row, 6).fill = RED_FILL
                ws.cell(row, 7).fill = RED_FILL
            row += 1
        apply_border(ws, prod_hdr_row, row-1, 1, 7)

        row += 1
        ws.cell(row, 1, "СЕБЕСТОИМОСТЬ").font = BOLD12
        row += 1
        seb_headers = ["Артикул", "Сатылды (шт)", "Себест/шт (₸)", "Упаковка (₸)", "Жалпы (₸)"]
        seb_hdr_row = row
        for col, h in enumerate(seb_headers, 1):
            c = ws.cell(row, col, h)
            c.font = BOLD
            c.fill = LIGHT_GREEN_FILL
        row += 1
        for art, data in by_article.items():
            qty_a = data.get("qty", 0)
            sebest_a = seb_data.get(art, 0)
            ws.cell(row, 1, art)
            ws.cell(row, 2, qty_a)
            ws.cell(row, 3, sebest_a)
            ws.cell(row, 4, qty_a * 100)
            ws.cell(row, 5, qty_a * sebest_a)
            row += 1
        apply_border(ws, seb_hdr_row, row-1, 1, 5)

        ws.column_dimensions["A"].width = 28
        for col_ltr in ["B","C","D","E","F","G"]:
            ws.column_dimensions[col_ltr].width = 18

        _wb2.save(buf)

        period_str = f"{date_from.strftime('%d.%m')}-{date_to.strftime('%d.%m.%Y')}"
        st.download_button(
            f"⬇️ Excel жүктеу — {store['name']} ({period_str})",
            data=buf.getvalue(),
            file_name=f"Финансы_{store['name']}_{period_str}.xlsx",
            mime="application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
            key=f"fin_dl_{idx}"
        )

    st.divider()

    ads_key = f"ads_by_art_{idx}"
    if ads_key not in st.session_state:
        st.session_state[ads_key] = {}
    ads_by_art = st.session_state[ads_key]

    if by_article:
        with st.expander("📦 Тауар бойынша таза пайда", expanded=True):
            prod_rows = []
            for art, data in by_article.items():
                qty_a = data.get("qty", 0)
                wb_a = data.get("for_pay", 0)
                vozvrat_a = data.get("vozvrat", 0)
                sebest_a = seb_data.get(art, 0)
                reklama_a = ads_by_art.get(art, 0)
                share = wb_a / for_pay if for_pay > 0 else 0
                napay_a = wb_a - (storage*share) - (priemka*share) - (vozvrat_a*2)
                ndv_a = napay_a * ndv_rate
                ndvpr_a = (sebest_a * qty_a) * ndv_rate
                ndvn_a = ndv_a - ndvpr_a
                pack_a = qty_a * 100
                doipn_a = napay_a - (sebest_a*qty_a)
                ipn_a = doipn_a * 0.10 if doipn_a > 0 else 0
                pack_a = qty_a * 100
                ndvn_a = (napay_a * (16/116)) - ((sebest_a * qty_a) * (16/116))
                profit_a = doipn_a - ipn_a - ndvn_a - (logistic*share) - pack_a - (samovykup*share) - reklama_a
                pct_a = profit_a / wb_a * 100 if wb_a > 0 else 0
                prod_rows.append({
                    "Артикул": art,
                    "Сатылды (шт)": qty_a,
                    "WB түскен (₸)": round(wb_a),
                    "Себест/шт (₸)": sebest_a,
                    "Реклама (₸)": reklama_a,
                    "Таза пайда (₸)": round(profit_a),
                    "%": round(pct_a, 1),
                })
            prod_df = pd.DataFrame(prod_rows)

            def style_profit(val):
                if pd.isna(val): return ""
                return "color: #3B6D11; font-weight: bold" if val >= 0 else "color: #A32D2D; font-weight: bold"

            styled = prod_df.style.map(style_profit, subset=["Таза пайда (₸)", "%"])
            st.dataframe(styled, use_container_width=True, height=400,
                column_config={
                    "WB түскен (₸)": st.column_config.NumberColumn(format="%d ₸"),
                    "Реклама (₸)": st.column_config.NumberColumn(format="%d ₸"),
                    "Таза пайда (₸)": st.column_config.NumberColumn(format="%d ₸"),
                    "%": st.column_config.NumberColumn(format="%.1f%%"),
                })

            if role == "manager":
                st.divider()
                st.caption("✏️ Реклама — тауар бойынша қолмен енгізіңіз")
                ads_tbl = pd.DataFrame([
                    {"Артикул": art, "Реклама (₸)": ads_by_art.get(art, 0)}
                    for art in by_article.keys()
                ])
                ads_edited = st.data_editor(
                    ads_tbl, use_container_width=True, height=400,
                    key=f"ads_editor_{idx}",
                    column_config={
                        "Артикул": st.column_config.TextColumn(disabled=True),
                        "Реклама (₸)": st.column_config.NumberColumn(format="%d ₸", min_value=0),
                    }
                )
                new_ads = dict(zip(ads_edited["Артикул"], ads_edited["Реклама (₸)"]))
                if new_ads != ads_by_art:
                    st.session_state[ads_key] = new_ads
                    st.rerun()

    st.divider()

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
                new_seb = dict(zip(edited_seb["Артикул"], edited_seb["Себест/шт (₸)"]))
                if new_seb != seb_data:
                    all_seb[str(idx)] = new_seb
                    save_json(SEBEST_FILE, all_seb)
                    st.rerun()
            else:
                st.dataframe(seb_df, use_container_width=True, height=400)

def show_store(store, df, sales30, filter_status, search):
    idx = store["idx"]
    role = st.session_state.get("role", "manager")

    if df.empty:
        st.warning("Деректер жоқ немесе жүктелмеді")
        return

    tab_ostatok, tab_analytic, tab_finance, tab_feedback = st.tabs([
        "📦 Остатки", "📊 Аналитика — 30 күн", "💰 Финансы", "💬 Отзывы & Вопросы"
    ])

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

    with tab_finance:
        show_finance_tab(store, df)

    with tab_feedback:
        show_feedback_tab(store)

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
            pd.DataFrame().to_excel(writer, sheet_name="_tmp", index=False)
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

# ── SIDEBAR ──
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
    store_count = len(visible_stores)
    st.markdown(f"""
    <div style='background:linear-gradient(135deg,#185FA5,#2E86DE);color:white;
    padding:10px 14px;border-radius:8px;margin-bottom:4px;'>
    <div style='font-size:13px;font-weight:700;letter-spacing:1px;'>📊 MAGKEIN отчеті</div>
    <div style='font-size:11px;opacity:0.85;margin-top:2px;'>
    {store_count} дүкен біріктірілген · соңғы таб ↗</div>
    </div>""", unsafe_allow_html=True)
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

# ── НЕГІЗГІ ──
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
# Тек менеджер MAGKEIN табын көреді
_show_magkein = st.session_state.get("role", "manager") == "manager" and len(visible_stores) >= 1
if _show_magkein:
    tab_names = tab_names + ["📊 MAGKEIN"]

tabs = st.tabs(tab_names)

store_tabs = tabs[:len(visible_stores)]
for tab, store in zip(store_tabs, visible_stores):
    with tab:
        df_key = f"df_{store['idx']}"
        if df_key not in st.session_state or st.session_state[df_key] is None:
            st.info("👈 **«Барлығын жүктеу»** батырмасын басыңыз")
        else:
            sales30 = st.session_state.get(f"sales30_{store['idx']}", pd.DataFrame())
            show_store(store, st.session_state[df_key], sales30, filter_status, search)

# ── MAGKEIN ЖАЛПЫ ОТЧЕТ ──
if _show_magkein:
    with tabs[-1]:
        st.markdown("## 📊 MAGKEIN — Жалпы қаржы отчеті")
        st.markdown("Барлық дүкендердің біріктірілген қаржы нәтижесі")
        st.divider()

        col1, col2, col3 = st.columns([1, 1, 1])
        with col1:
            mg_date_from = st.date_input("Басталу күні", value=date.today() - timedelta(days=7), key="mg_from")
        with col2:
            mg_date_to = st.date_input("Аяқталу күні", value=date.today(), key="mg_to")
        with col3:
            st.markdown("<br>", unsafe_allow_html=True)
            mg_load = st.button("🔄 Барлығын жүктеу", key="mg_load", use_container_width=True)

        mg_key = "magkein_data"
        mg_period_key = "magkein_period"
        current_mg_period = f"{mg_date_from}_{mg_date_to}"
        if st.session_state.get(mg_period_key) != current_mg_period:
            if mg_key in st.session_state:
                del st.session_state[mg_key]
            st.session_state[mg_period_key] = current_mg_period

        if mg_load:
            mg_results = {}
            for s in visible_stores:
                use_key = s["finance_key"] if s["finance_key"] else s["stats_key"]
                with st.spinner(f"[{s['name']}] жүктелуде..."):
                    try:
                        rows = fetch_report_detail(
                            use_key,
                            mg_date_from.strftime("%Y-%m-%d"),
                            mg_date_to.strftime("%Y-%m-%d"),
                            store_name=s["name"]
                        )
                        fin = parse_finance(rows)
                        mg_results[s["name"]] = fin
                    except Exception as e:
                        st.error(f"[{s['name']}] қате: {e}")
                        mg_results[s["name"]] = {}
            st.session_state[mg_key] = mg_results
            st.rerun()

        if mg_key not in st.session_state:
            st.info("👆 Период таңдап **«Барлығын жүктеу»** батырмасын басыңыз")
        else:
            mg_results = st.session_state[mg_key]
            all_seb = load_json(SEBEST_FILE)

            def fmt(n): return f"{round(n):,} ₸".replace(",", " ")
            def fmtN(n): return f"{round(n):,}".replace(",", " ")
            ndv_rate = 16 / 116

            # ── ӘР ДҮКЕН БОЙЫНША ЖОЛДАР ──
            summary_rows = []
            total_for_pay = total_napay = total_profit = total_qty = total_vozvrat_qty = 0

            for s in visible_stores:
                fin = mg_results.get(s["name"], {})
                if not fin:
                    continue
                idx = s["idx"]
                seb_data = all_seb.get(str(idx), {})
                by_article = fin.get("by_article", {})

                for_pay   = fin.get("for_pay", 0)
                ads       = fin.get("ads", 0)
                storage   = fin.get("storage", 0)
                priemka   = fin.get("priemka", 0)
                penalty   = fin.get("penalty", 0)
                logistic_auto = fin.get("logistic", 0)
                vozvrat   = fin.get("vozvrat", 0)
                vozvrat_qty = fin.get("vozvrat_qty", 0)
                t_qty     = fin.get("total_qty", 0)
                vozvrat_sh = vozvrat * 2

                tot_seb   = sum(seb_data.get(a, 0) * by_article.get(a, {}).get("qty", 0) for a in by_article)
                tot_qty_sold = sum(by_article.get(a, {}).get("qty", 0) for a in by_article)
                upakovka  = tot_qty_sold * 100

                napay     = for_pay - ads - logistic_auto - storage - priemka - penalty - vozvrat_sh
                ndv_nashe = (napay * ndv_rate) - (tot_seb * ndv_rate)
                do_ipn    = napay - tot_seb
                ipn       = do_ipn * 0.10 if do_ipn > 0 else 0
                profit    = do_ipn - ipn - ndv_nashe - upakovka

                # Қолмен енгізілген шығындарды алу
                man = st.session_state.get(f"fin_manual_{idx}", {"logistic": 0, "samovykup": 0, "reklama_napay": 0})
                profit -= man["logistic"] + man["samovykup"] + man["reklama_napay"]

                summary_rows.append({
                    "Дүкен": s["name"],
                    "К перечислению (₸)": round(for_pay),
                    "На пэй (₸)": round(napay),
                    "Себестоимость (₸)": round(tot_seb),
                    "Сатылды (шт)": t_qty,
                    "Возврат (шт)": vozvrat_qty,
                    "Таза пайда (₸)": round(profit),
                    "Рентабельность (%)": round(profit / for_pay * 100, 1) if for_pay > 0 else 0,
                })
                total_for_pay     += for_pay
                total_napay       += napay
                total_profit      += profit
                total_qty         += t_qty
                total_vozvrat_qty += vozvrat_qty

            if not summary_rows:
                st.warning("Деректер жоқ")
            else:
                # ЖАЛПЫ МЕТРИКАЛАР
                c1, c2, c3, c4 = st.columns(4)
                c1.metric("💰 К перечислению", fmt(total_for_pay))
                c2.metric("📊 На пэй (жалпы)", fmt(total_napay))
                c3.metric("✅ Таза пайда (жалпы)", fmt(total_profit),
                          delta=f"{total_profit/total_for_pay*100:.1f}%" if total_for_pay > 0 else "0%")
                c4.metric("📦 Сатылды (жалпы)", f"{fmtN(total_qty)} шт")

                st.divider()

                # ДҮКЕН БОЙЫНША КЕСТЕ
                st.markdown("#### 🏪 Дүкен бойынша салыстыру")

                # Жалпы жол қосу
                summary_rows.append({
                    "Дүкен": "🔷 ЖАЛПЫ (MAGKEIN)",
                    "К перечислению (₸)": round(total_for_pay),
                    "На пэй (₸)": round(total_napay),
                    "Себестоимость (₸)": sum(r["Себестоимость (₸)"] for r in summary_rows),
                    "Сатылды (шт)": total_qty,
                    "Возврат (шт)": total_vozvrat_qty,
                    "Таза пайда (₸)": round(total_profit),
                    "Рентабельность (%)": round(total_profit / total_for_pay * 100, 1) if total_for_pay > 0 else 0,
                })

                mg_df = pd.DataFrame(summary_rows)

                def style_mg(row):
                    if row["Дүкен"].startswith("🔷"):
                        return ["font-weight:bold; background-color:#E6F1FB"] * len(row)
                    profit_val = row["Таза пайда (₸)"]
                    if isinstance(profit_val, (int, float)) and profit_val < 0:
                        return ["background-color:#FCEBEB"] * len(row)
                    return [""] * len(row)

                styled_mg = mg_df.style.apply(style_mg, axis=1)
                st.dataframe(styled_mg, use_container_width=True, height=200,
                    column_config={
                        "К перечислению (₸)": st.column_config.NumberColumn(format="%d ₸"),
                        "На пэй (₸)": st.column_config.NumberColumn(format="%d ₸"),
                        "Себестоимость (₸)": st.column_config.NumberColumn(format="%d ₸"),
                        "Сатылды (шт)": st.column_config.NumberColumn(format="%d шт"),
                        "Возврат (шт)": st.column_config.NumberColumn(format="%d шт"),
                        "Таза пайда (₸)": st.column_config.NumberColumn(format="%d ₸"),
                        "Рентабельность (%)": st.column_config.NumberColumn(format="%.1f%%"),
                    })

                st.divider()

                # ДИАГРАММА — таза пайда салыстыру
                st.markdown("#### 📊 Дүкендер бойынша таза пайда")
                chart_df = mg_df[mg_df["Дүкен"] != "🔷 ЖАЛПЫ (MAGKEIN)"][["Дүкен", "Таза пайда (₸)"]].set_index("Дүкен")
                st.bar_chart(chart_df, height=300)

                # EXCEL ЖҮКТЕУ
                st.divider()
                buf_mg = io.BytesIO()
                import openpyxl as _oxl2
                from openpyxl.styles import Font as _Font2, PatternFill as _Fill2
                _wb_mg = _oxl2.Workbook()
                _ws_mg = _wb_mg.active
                _ws_mg.title = "MAGKEIN отчет"

                headers_mg = ["Дүкен", "К перечислению (₸)", "На пэй (₸)", "Себестоимость (₸)",
                              "Сатылды (шт)", "Возврат (шт)", "Таза пайда (₸)", "Рентабельность (%)"]
                for col, h in enumerate(headers_mg, 1):
                    c = _ws_mg.cell(1, col, h)
                    c.font = _Font2(bold=True)
                    c.fill = _Fill2("solid", fgColor="BDD7EE")

                for r_idx, row in enumerate(summary_rows, 2):
                    vals = [row[h] for h in headers_mg]
                    for col, v in enumerate(vals, 1):
                        cell = _ws_mg.cell(r_idx, col, v)
                        if row["Дүкен"].startswith("🔷"):
                            cell.font = _Font2(bold=True)
                            cell.fill = _Fill2("solid", fgColor="92D050")

                for col_ltr in ["A","B","C","D","E","F","G","H"]:
                    _ws_mg.column_dimensions[col_ltr].width = 22
                _wb_mg.save(buf_mg)

                period_str = f"{mg_date_from.strftime('%d.%m')}-{mg_date_to.strftime('%d.%m.%Y')}"
                st.download_button(
                    f"⬇️ Excel — MAGKEIN ({period_str})",
                    data=buf_mg.getvalue(),
                    file_name=f"MAGKEIN_{period_str}.xlsx",
                    mime="application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
                    key="mg_dl"
                )
