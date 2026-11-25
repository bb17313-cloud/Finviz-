import os
import json
import time
import random
import html
from html.parser import HTMLParser
from datetime import datetime
import pytz
import requests
from requests.adapters import HTTPAdapter
from urllib3.util.retry import Retry
from googletrans import Translator

BOT_TOKEN = os.getenv("BOT_TOKEN")
CHAT_ID = os.getenv("CHAT_ID")
STATE_FILE = os.getenv("RADAR_STATE_FILE", "radar_state.json")
INTERVAL_SECONDS = int(os.getenv("RADAR_INTERVAL_SECONDS", "180"))  # كل 3 دقائق
MAX_RUN_SECONDS = int(os.getenv("RADAR_MAX_RUN_SECONDS", "3300"))    # 55 دقيقة كحد أقصى لكل تشغيل

FINVIZ_HEADERS = {
    "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/131.0.0.0 Safari/537.36",
    "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,image/avif,image/webp,*/*;q=0.8",
    "Accept-Language": "en-US,en;q=0.9",
    "Referer": "https://finviz.com/",
    "Connection": "keep-alive",
    "Upgrade-Insecure-Requests": "1",
}


def create_finviz_session():
    session = requests.Session()
    retry = Retry(
        total=3,
        backoff_factor=2,
        status_forcelist=[403, 429, 500, 502, 503, 504],
        allowed_methods=["GET"],
        raise_on_status=False,
    )
    adapter = HTTPAdapter(max_retries=retry)
    session.mount("https://", adapter)
    session.mount("http://", adapter)
    session.headers.update(FINVIZ_HEADERS)
    return session


finviz_session = create_finviz_session()
translator = Translator()


def load_state():
    today_ny = datetime.now(pytz.timezone("America/New_York")).strftime("%Y-%m-%d")
    default_state = {
        "date": today_ny,
        "stocks": {},
        "last_summary_tickers": [],
        "was_open": False,
    }
    if not os.path.exists(STATE_FILE):
        return default_state
    try:
        with open(STATE_FILE, "r", encoding="utf-8") as f:
            data = json.load(f)
        if data.get("date") != today_ny:
            default_state["was_open"] = bool(data.get("was_open", False))
            return default_state
        return {
            "date": today_ny,
            "stocks": data.get("stocks", {}),
            "last_summary_tickers": data.get("last_summary_tickers", []),
            "was_open": bool(data.get("was_open", False)),
        }
    except Exception as e:
        print(f"⚠️ تعذر قراءة ملف الحالة {STATE_FILE}: {e}")
        return default_state


def save_state(state):
    try:
        with open(STATE_FILE, "w", encoding="utf-8") as f:
            json.dump(state, f, ensure_ascii=False, indent=2)
    except Exception as e:
        print(f"⚠️ تعذر حفظ ملف الحالة {STATE_FILE}: {e}")


def translate_to_arabic(text):
    if not text or text == "لايوجد":
        return "لايوجد"
    try:
        translated = translator.translate(text, dest="ar")
        return translated.text
    except Exception as e:
        print(f"⚠️ خطأ في الترجمة: {e}")
        return text


class FinvizRowsParser(HTMLParser):
    def __init__(self):
        super().__init__()
        self.rows = []
        self.current_row = None
        self.cell_id = None
        self.cell_depth = 0
        self.cell_text = []

    def handle_starttag(self, tag, attrs):
        if tag != "div":
            return
        a = dict(attrs)
        if self.cell_id is not None:
            self.cell_depth += 1
            return
        if "data-row-key" in a:
            self.current_row = {"row_key": a["data-row-key"]}
            self.rows.append(self.current_row)
        elif self.current_row is not None and "data-column-id" in a:
            self.cell_id = a["data-column-id"]
            self.cell_depth = 1
            self.cell_text = []

    def handle_endtag(self, tag):
        if tag != "div" or self.cell_id is None:
            return
        self.cell_depth -= 1
        if self.cell_depth == 0:
            self.current_row[self.cell_id] = "".join(self.cell_text).strip()
            self.cell_id = None

    def handle_data(self, data):
        if self.cell_id is not None:
            self.cell_text.append(data)


class FinvizNewsParser(HTMLParser):
    def __init__(self):
        super().__init__()
        self.headlines = []
        self.in_news_table = False
        self.in_a = False
        self.current_headline = []

    def handle_starttag(self, tag, attrs):
        a = dict(attrs)
        if tag == "table" and a.get("id") == "news-table":
            self.in_news_table = True
        elif self.in_news_table and tag == "a":
            self.in_a = True
            self.current_headline = []

    def handle_endtag(self, tag):
        if tag == "table" and self.in_news_table:
            self.in_news_table = False
        elif self.in_a and tag == "a":
            self.in_a = False
            headline = "".join(self.current_headline).strip()
            if headline and headline not in self.headlines:
                self.headlines.append(headline)

    def handle_data(self, data):
        if self.in_a:
            self.current_headline.append(data)


def fetch_finviz_signal(signal, limit=30):
    url = f"https://finviz.com/screener.ashx?v=111&s={signal}"
    time.sleep(random.uniform(1.5, 3.0))
    res = finviz_session.get(url, timeout=20)
    res.raise_for_status()
    parser = FinvizRowsParser()
    parser.feed(res.text)

    rows = []
    seen_keys = set()
    for row in parser.rows:
        key = row.get("row_key")
        if not key or key in seen_keys:
            continue
        seen_keys.add(key)
        rows.append(row)
    return rows[:limit]


def fetch_stock_news(ticker):
    try:
        url = f"https://finviz.com/quote.ashx?t={ticker}"
        time.sleep(random.uniform(1.0, 2.0))
        res = finviz_session.get(url, timeout=12)
        if res.status_code == 200:
            parser = FinvizNewsParser()
            parser.feed(res.text)
            if parser.headlines:
                latest_news = parser.headlines[0]
                if len(latest_news) > 90:
                    latest_news = latest_news[:87] + "..."
                return translate_to_arabic(latest_news)
    except Exception as e:
        print(f"⚠️ خطأ في جلب أخبار السهم لـ {ticker}: {e}")
    return "لايوجد"


def send_telegram_message(message, parse_mode="HTML"):
    if not BOT_TOKEN or not CHAT_ID:
        print("❌ خطأ: لم يتم العثور على إعدادات BOT_TOKEN أو CHAT_ID.")
        return False

    url = f"https://api.telegram.org/bot{BOT_TOKEN}/sendMessage"
    payload = {
        "chat_id": CHAT_ID,
        "text": message,
        "parse_mode": parse_mode,
        "disable_web_page_preview": True,
    }
    try:
        response = requests.post(url, json=payload, timeout=15)
        if response.status_code != 200:
            print(f"❌ فشل إرسال رسالة التلغرام: {response.text}")
            return False
        return True
    except Exception as e:
        print(f"❌ خطأ أثناء الاتصال بالتلغرام: {e}")
        return False


def to_float(value):
    try:
        return float(str(value).replace(",", "").replace("%", "").replace("x", "").strip())
    except (TypeError, ValueError):
        return None


def get_rel_vol_status(rvol_value):
    val = to_float(rvol_value)
    if val is None:
        return ""
    if val > 3.0:
        return "استثنائي"
    elif val >= 1.5:
        return "اعلى"
    elif val >= 0.8:
        return "عادي"
    else:
        return "ظعيف"


SEC_8K_ITEMS = {
    "1.01": "اتفاقية جوهرية", "1.02": "إنهاء اتفاقية جوهرية", "1.03": "إفلاس / حراسة قضائية",
    "2.01": "استحواذ / دمج أو بيع أصول", "2.02": "إعلان نتائج مالية", "2.03": "التزام مالي / دين جديد",
    "2.04": "تسريع التزام مالي", "2.05": "تكاليف إعادة هيكلة", "2.06": "انخفاض جوهري في قيمة أصول",
    "3.01": "إشعار شطب / مخالفة شروط الإدراج", "3.02": "إصدار أسهم غير مسجل", "3.03": "تعديل حقوق المساهمين",
    "4.01": "تغيير المدقق", "4.02": "إعادة صياغة قوائم مالية", "5.01": "تغيّر السيطرة",
    "5.02": "تغييرات الإدارة", "5.03": "حوكمة الشركة", "5.05": "حوكمة الشركة", "5.07": "تصويت المساهمين",
    "5.08": "ترشيحات مجلس الإدارة", "7.01": "إفصاح عادل (Reg FD)", "8.01": "أحداث أخرى", "9.01": "مرفقات مالية",
}

SEC_FORM_NAMES = {
    "10-K": "التقرير السنوي", "10-Q": "التقرير الفصلي", "20-F": "التقرير السنوي (مصدر أجنبي)",
    "6-K": "تقرير مصدر أجنبي", "3": "بيان ملكية مبدئي لمطلع", "4": "تغيّر ملكية لمطلع",
    "5": "بيان ملكية سنوي لمطلع", "144": "إشعار بيع أسهم لمطلع", "S-1": "طرح / تسجيل أوراق مالية",
    "S-3": "طرح / تسجيل أوراق مالية", "F-1": "طرح / تسجيل أوراق مالية", "F-3": "طرح / تسجيل أوراق مالية",
    "S-8": "تسجيل أسهم برامج الموظفين", "DEF 14A": "بيان الجمعية العمومية (حوكمة)", "PRE 14A": "بيان الجمعية العمومية (حوكمة)",
}


def get_sec_description(f_form, f_items=""):
    f_upper = str(f_form).upper().strip()
    is_amend = f_upper.endswith("/A")
    base = f_upper[:-2] if is_amend else f_upper
    prefix = "تعديل: " if is_amend else ""

    if base == "8-K":
        names = []
        for code in str(f_items or "").split(","):
            name = SEC_8K_ITEMS.get(code.strip())
            if name and name not in names:
                names.append(name)
        if len(names) > 1 and "مرفقات مالية" in names:
            names.remove("مرفقات مالية")
        if names:
            return prefix + " / ".join(names[:2])
        return prefix + "إفصاح جوهري"

    if "13D" in base:
        return prefix + "ملكية جوهرية 5%+ (نية نشطة)"
    if "13G" in base:
        return prefix + "ملكية جوهرية 5%+ (استثمار سلبي)"
    if base.startswith("424B"):
        return prefix + "طرح / تسجيل أوراق مالية"
    if base.startswith("NT "):
        return "إشعار تأخر تقديم التقرير"
    if base in SEC_FORM_NAMES:
        return prefix + SEC_FORM_NAMES[base]
    return prefix + "إفصاح رسمي"


def fetch_sec_filings_only(symbol):
    symbol = symbol.upper().strip()
    sec_headers = {"User-Agent": "StockRadarBot/1.0 (contact@stockradar.com)"}
    filings = []
    cik_str_val = None
    try:
        tickers_res = requests.get("https://www.sec.gov/files/company_tickers.json", headers=sec_headers, timeout=8)
        if tickers_res.status_code == 200:
            tickers_data = tickers_res.json()
            cik = None
            cik_raw = None
            for _, val in tickers_data.items():
                if val.get("ticker", "").upper() == symbol:
                    cik_raw = str(val.get("cik_str"))
                    cik = cik_raw.zfill(10)
                    cik_str_val = cik_raw
                    break
            if cik:
                sub_url = f"https://data.sec.gov/submissions/CIK{cik}.json"
                sub_res = requests.get(sub_url, headers=sec_headers, timeout=8)
                if sub_res.status_code == 200:
                    recent = sub_res.json().get("filings", {}).get("recent", {})
                    forms = recent.get("form", [])
                    accession_numbers = recent.get("accessionNumber", [])
                    primary_docs = recent.get("primaryDocument", [])
                    filing_dates = recent.get("filingDate", [])
                    filing_items = recent.get("items", [])
                    for i in range(min(5, len(forms))):
                        f_form = forms[i]
                        f_items = filing_items[i] if i < len(filing_items) else ""
                        f_date = filing_dates[i]
                        acc_num = accession_numbers[i]
                        acc_clean = acc_num.replace("-", "")
                        p_doc = primary_docs[i]
                        if p_doc:
                            filing_url = f"https://www.sec.gov/Archives/edgar/data/{cik_raw}/{acc_clean}/{p_doc}"
                        else:
                            filing_url = f"https://www.sec.gov/Archives/edgar/data/{cik_raw}/{acc_clean}/{acc_num}-index.htm"
                        filings.append({
                            "form": f_form,
                            "date": f_date,
                            "url": filing_url,
                            "accession": acc_num,
                            "brief": get_sec_description(f_form, f_items),
                        })
    except Exception as e:
        print(f"⚠️ خطأ في جلب إفصاحات SEC لـ {symbol}: {e}")

    sec_lines = []
    seen_accessions = set()
    latest_accession = ""
    if filings:
        latest_accession = filings[0]["accession"]
        for f in filings:
            if f["accession"] in seen_accessions:
                continue
            seen_accessions.add(f["accession"])
            sec_lines.append(
                f"• <a href='{f['url']}'>{html.escape(str(f['form']))}</a> · <code>{f['date']}</code> — {html.escape(f['brief'])}"
            )
            if len(sec_lines) >= 4:
                break
    sec_summary = "\n".join(sec_lines) if sec_lines else "• لا توجد إفصاحات حديثة"
    return sec_summary, cik_str_val, latest_accession


def is_excluded_sector(stock):
    sector = str(stock.get("sector", "")).lower()
    industry = str(stock.get("industry", "")).lower()
    exclude_keywords = ["bank", "insurance", "restaurant", "beverage"]
    for kw in exclude_keywords:
        if kw in sector or kw in industry:
            return True
    return False


def send_stock_cards(session_name, stocks, signal_label, stock_state, positive_only=False, skip_tickers=None):
    sent = 0
    skip_tickers = skip_tickers or set()
    min_volume = 1000 if session_name == "Pre-Market" else 10000

    for stock in stocks:
        ticker = str(stock.get("row_key", "")).strip().upper()
        price = to_float(stock.get("price"))
        if not ticker or price is None or price <= 0:
            continue

        if ticker in skip_tickers:
            continue

        volume = to_float(stock.get("volume"))
        if volume is None or volume < min_volume:
            continue

        if is_excluded_sector(stock):
            continue

        if positive_only:
            change_val = to_float(stock.get("change"))
            if change_val is None or change_val <= 0:
                continue

        key = f"{session_name}:{ticker}"
        last = stock_state.get(key)

        if last and price <= last["price"]:
            continue

        alert_no = (last["alert_no"] + 1) if last else 1

        stock_news = fetch_stock_news(ticker)
        sec_summary, cik_val, _ = fetch_sec_filings_only(ticker)

        rel_vol = str(stock.get("revol", "")).strip() or str(stock.get("rel_volume", "")).strip()
        float_val = str(stock.get("float", "")).strip()

        tv_url = f"https://www.tradingview.com/chart/?symbol={ticker}"
        sec_page_url = f"https://www.sec.gov/cgi-bin/browse-edgar?action=getcompany&CIK={cik_val or ticker}&type=&dateb=&owner=include&count=40"

        message_lines = [
            f"🚨 <b>تنبيه {alert_no}</b> · {html.escape(session_name)}",
            f"📡 Signal: <b>{html.escape(signal_label)}</b>\n",
            f"🔹 <b>{html.escape(ticker)}</b>",
            f"💵 <code>${price:.2f}</code> · 📈 <code>{html.escape(str(stock.get('change', '-')))}</code>",
            f"📊 Vol: <code>{html.escape(str(stock.get('volume', '-')))}</code>",
            f"🏭 القطاع: <code>{html.escape(str(stock.get('sector', '-')))}</code>",
            f"🏷️ الصناعة: <code>{html.escape(str(stock.get('industry', '-')))}</code>",
            f"📰 أخبار اليوم: {html.escape(stock_news)}",
        ]

        if rel_vol and rel_vol not in ["-", "nan", "None", ""]:
            status_text = get_rel_vol_status(rel_vol)
            status_suffix = f" ({status_text})" if status_text else ""
            message_lines.append(f"🔥 الحجم النسبي: <code>{html.escape(rel_vol)}</code>{status_suffix}")

        if float_val and float_val not in ["-", "nan", "None", ""]:
            message_lines.append(f"🌊 الفلوت: <code>{html.escape(float_val)}</code>")

        message_lines.extend([
            f"\n📄 <b>آخر إفصاحات SEC</b>",
            f"{sec_summary}\n",
            f"📈 <a href='{tv_url}'>TradingView</a>",
            f"🏛️ <a href='{html.escape(sec_page_url)}'>SEC</a>",
        ])

        message = "\n".join(message_lines)

        if send_telegram_message(message, parse_mode="HTML"):
            stock_state[key] = {"price": price, "alert_no": alert_no}
            sent += 1
            time.sleep(1.5)

    return sent


def get_market_session_and_name():
    ny_tz = pytz.timezone("America/New_York")
    now_ny = datetime.now(ny_tz)

    if now_ny.weekday() >= 5:
        return None, None

    current_time = now_ny.time()

    pre_start = datetime.strptime("04:00", "%H:%M").time()
    main_start = datetime.strptime("09:30", "%H:%M").time()
    main_end = datetime.strptime("16:00", "%H:%M").time()
    post_end = datetime.strptime("20:00", "%H:%M").time()

    if pre_start <= current_time < main_start:
        return "Pre-Market", "الأسهم الفعالة ماقبل السوق"
    elif main_start <= current_time < main_end:
        return "Main Session", "الأسهم الفعالة للجلسة الرئيسة"
    elif main_end <= current_time <= post_end:
        return "Post-Market", "الأسهم الفعالة لجلسة post"

    return None, None


def filter_signal_stocks(raw_stocks, session_key, positive_only=False, skip_tickers=None):
    skip_tickers = skip_tickers or set()
    min_volume = 1000 if session_key == "Pre-Market" else 10000
    valid = []

    for stock in raw_stocks:
        ticker = str(stock.get("row_key", "")).strip().upper()
        if not ticker or ticker in skip_tickers:
            continue

        price = to_float(stock.get("price"))
        if price is None or price <= 0:
            continue

        volume = to_float(stock.get("volume"))
        if volume is None or volume < min_volume:
            continue

        if is_excluded_sector(stock):
            continue

        change_val = to_float(stock.get("change"))
        if positive_only and (change_val is None or change_val <= 0):
            continue

        change_str = str(stock.get("change", "0%"))
        if not change_str.startswith("+") and not change_str.startswith("-") and change_val and change_val > 0:
            change_str = "+" + change_str

        valid.append({
            "ticker": ticker,
            "price": price,
            "change": change_val if change_val is not None else 0.0,
            "change_str": change_str,
        })
    return valid


def fetch_all_signals():
    signals = ["ta_topgainers", "ta_newhigh", "ta_mostactive", "ta_unusualvolume"]
    data = {}
    for sig in signals:
        try:
            data[sig] = fetch_finviz_signal(sig, limit=25)
        except Exception as e:
            print(f"❌ خطأ في جلب إشارة {sig}: {e}")
            data[sig] = []
    return data


def build_summary_report(session_key, session_title, signals_data, state):
    last_summary_set = set(state.get("last_summary_tickers", []))
    seen_tickers = set()

    gainers = filter_signal_stocks(signals_data.get("ta_topgainers", []), session_key, positive_only=True)
    for s in gainers:
        seen_tickers.add(s["ticker"])

    new_highs = filter_signal_stocks(signals_data.get("ta_newhigh", []), session_key, positive_only=True, skip_tickers=seen_tickers)
    for s in new_highs:
        seen_tickers.add(s["ticker"])

    most_active = filter_signal_stocks(signals_data.get("ta_mostactive", []), session_key, positive_only=True, skip_tickers=seen_tickers)
    for s in most_active:
        seen_tickers.add(s["ticker"])

    unusual_vol = filter_signal_stocks(signals_data.get("ta_unusualvolume", []), session_key, positive_only=True, skip_tickers=seen_tickers)

    all_combined = gainers + new_highs + most_active + unusual_vol

    unique_dict = {}
    for item in all_combined:
        t = item["ticker"]
        if t not in unique_dict or item["change"] > unique_dict[t]["change"]:
            unique_dict[t] = item

    final_list = list(unique_dict.values())
    if not final_list:
        return None

    final_list.sort(key=lambda x: x["change"], reverse=True)
    top_20 = final_list[:20]

    sa_tz = pytz.timezone("Asia/Riyadh")
    time_sa = datetime.now(sa_tz).strftime("%I:%M %p")

    current_tickers = [item["ticker"] for item in top_20]

    lines = [
        f"⏱️ <b>{session_title} · آخر 3 دقائق · {time_sa} بتوقيت السعودية</b>\n"
    ]

    for idx, item in enumerate(top_20, 1):
        t = item["ticker"]
        tv_url = f"https://www.tradingview.com/chart/?symbol={t}"
        ch_str = item["change_str"]

        is_new = t not in last_summary_set
        new_mark = " 🆕" if is_new else ""

        lines.append(f"{idx}. <a href='{tv_url}'>{t}</a> {ch_str} · ${item['price']:.2f}{new_mark}")

    state["last_summary_tickers"] = current_tickers
    return "\n".join(lines)


def run_individual_alerts(session_name, signals_data, stock_state):
    seen_tickers = set()

    top_gainers = signals_data.get("ta_topgainers", [])
    send_stock_cards(session_name, top_gainers, "Top Gainers", stock_state)
    for s in top_gainers:
        t = str(s.get("row_key", "")).strip().upper()
        if t:
            seen_tickers.add(t)

    new_highs = signals_data.get("ta_newhigh", [])
    send_stock_cards(session_name, new_highs, "New High", stock_state, skip_tickers=seen_tickers)
    for s in new_highs:
        t = str(s.get("row_key", "")).strip().upper()
        if t:
            seen_tickers.add(t)

    most_active = signals_data.get("ta_mostactive", [])
    send_stock_cards(session_name, most_active, "Most Active", stock_state, positive_only=True, skip_tickers=seen_tickers)
    for s in most_active:
        t = str(s.get("row_key", "")).strip().upper()
        if t:
            seen_tickers.add(t)

    unusual_volume = signals_data.get("ta_unusualvolume", [])
    send_stock_cards(session_name, unusual_volume, "Unusual Volume", stock_state, positive_only=True, skip_tickers=seen_tickers)


def run_session_batch():
    print("🚀 تشغيل الرادار (تحديث كل 3 دقائق ضمن الجلسات الثلاث)...")
    start_time = time.time()
    state = load_state()

    while True:
        elapsed = time.time() - start_time
        if elapsed >= MAX_RUN_SECONDS:
            print("⏱️ تم الوصول للحد الزمني الآمن للدفعة الحالية؛ سيتم استئنافها عبر الجدولة.")
            break

        session_key, session_title = get_market_session_and_name()

        if session_key:
            if not state.get("was_open"):
                send_telegram_message(f"🟢 <b>بدء رادار التداول الحركي لـ {session_title}</b>")
                state["was_open"] = True

            signals_data = fetch_all_signals()

            print(f"⏳ جاري إنشاء وإرسال التقرير المجمع لـ ({session_title})...")
            report_message = build_summary_report(session_key, session_title, signals_data, state)
            if report_message:
                if send_telegram_message(report_message, parse_mode="HTML"):
                    print("✅ تم إرسال التقرير المجمع بنجاح.")

            print(f"⏳ فحص وإرسال التنبيهات الفردية لـ ({session_title})...")
            run_individual_alerts(session_key, signals_data, state.setdefault("stocks", {}))
            save_state(state)

            remaining_to_cap = MAX_RUN_SECONDS - (time.time() - start_time)
            if remaining_to_cap <= 0:
                break

            sleep_time = min(INTERVAL_SECONDS, remaining_to_cap)
            print(f"💤 التحديث القادم بعد {int(sleep_time)} ثانية...")
            time.sleep(sleep_time)
        else:
            if state.get("was_open"):
                send_telegram_message("🔴 <b>انتهت فترات التداول بالكامل (إغلاق السوق). يتوقف الرادار الآن مؤقتاً لحين الجلسة القادمة.</b>")
                state["was_open"] = False
                state["last_summary_tickers"] = []
                save_state(state)
            print("😴 السوق مغلق حالياً. إنهاء التشغيل بانتظار الجلسة القادمة.")
            break

    save_state(state)
    print("✅ انتهى التشغيل بنجاح.")


if __name__ == "__main__":
    run_session_batch()
