import os
import time
import html
from html.parser import HTMLParser
from datetime import datetime
import pytz
import requests
from googletrans import Translator  # مكتبة الترجمة

# قراءة الإعدادات بالصيغة الخاصة بك
BOT_TOKEN = os.getenv("BOT_TOKEN")
CHAT_ID = os.getenv("CHAT_ID")

FINVIZ_HEADERS = {
    "User-Agent": "Mozilla/5.0 (iPhone; CPU iPhone OS 17_0 like Mac OS X) AppleWebKit/605.1.15 (KHTML, like Gecko) Version/17.0 Mobile/15E148 Safari/604.1"
}

# تهيئة مترجم جوجل
translator = Translator()

def translate_to_arabic(text):
    """ترجمة النص إلى اللغة العربية تلقائياً"""
    if not text or text == "لايوجد":
        return "لايوجد"
    try:
        translated = translator.translate(text, dest='ar')
        return translated.text
    except Exception as e:
        print(f"⚠️ خطأ في الترجمة: {e}")
        return text

# سجل التنبيهات الفردية
alerts_state = {"date": None, "stocks": {}}
last_summary_tickers = set()

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
    res = requests.get(url, headers=FINVIZ_HEADERS, timeout=20)
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
        res = requests.get(url, headers=FINVIZ_HEADERS, timeout=10)
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
        "disable_web_page_preview": True
    }
    try:
        response = requests.post(url, json=payload)
        if response.status_code != 200:
            print(f"❌ فشل إرسال رسالة التلغرام: {response.text}")
            return False
        return True
    except Exception as e:
        print(f"❌ خطأ أثناء الاتصال بالتلغرام: {e}")
        return False

def get_alert_state():
    today = datetime.now(pytz.timezone('America/New_York')).strftime("%Y-%m-%d")
    if alerts_state["date"] != today:
        alerts_state["date"] = today
        alerts_state["stocks"] = {}
    return alerts_state["stocks"]

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
        tickers_res = requests.get("https://www.sec.gov/files/company_tickers.json", headers=sec_headers, timeout=5)
        if tickers_res.status_code == 200:
            tickers_data = tickers_res.json()
            cik = None
            cik_raw = None
            for idx, val in tickers_data.items():
                if val.get("ticker", "").upper() == symbol:
                    cik_raw = str(val.get("cik_str"))
                    cik = cik_raw.zfill(10)
                    cik_str_val = cik_raw
                    break
            if cik:
                sub_url = f"https://data.sec.gov/submissions/CIK{cik}.json"
                sub_res = requests.get(sub_url, headers=sec_headers, timeout=5)
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
                        filings.append({"form": f_form, "date": f_date, "url": filing_url, "accession": acc_num, "brief": get_sec_description(f_form, f_items)})
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
            sec_lines.append(f"• <a href='{f['url']}'>{html.escape(str(f['form']))}</a> · <code>{f['date']}</code> — {html.escape(f['brief'])}")
            if len(sec_lines) >= 4:
                break
    sec_summary = "\n".join(sec_lines) if sec_lines else "• لا توجد إفصاحات حديثة"
    return sec_summary, cik_str_val, latest_accession

def is_excluded_sector(stock):
    sector = str(stock.get('sector', '')).lower()
    industry = str(stock.get('industry', '')).lower()
    exclude_keywords = ['bank', 'insurance', 'restaurant', 'beverage']
    for kw in exclude_keywords:
        if kw in sector or kw in industry:
            return True
    return False

def send_stock_cards(session_name, stocks, signal_label, positive_only=False, skip_tickers=None):
    state = get_alert_state()
    sent = 0
    skip_tickers = skip_tickers or set()
    
    # 🌟 التخفيف في pre فقط: خفض الحد الأدنى للحجم إلى 1,000 سهم بدلاً من 10,000
    min_volume = 1000 if session_name == "Pre-Market" else 10000
    
    for stock in stocks:
        ticker = str(stock.get('row_key', '')).strip().upper()
        price = to_float(stock.get('price'))
        if not ticker or price is None or price <= 0:
            continue
        
        if ticker in skip_tickers:
            continue
        
        volume = to_float(stock.get('volume'))
        if volume is None or volume < min_volume:
            continue
        
        if is_excluded_sector(stock):
            continue
        
        if positive_only:
            change_val = to_float(stock.get('change'))
            if change_val is None or change_val <= 0:
                continue
        
        key = f"{session_name}:{ticker}"
        last = state.get(key)
        
        if last and price <= last["price"]:
            continue
        
        alert_no = (last["alert_no"] + 1) if last else 1
        
        stock_news = fetch_stock_news(ticker)
        sec_summary, cik_val, _ = fetch_sec_filings_only(ticker)
        
        rel_vol = str(stock.get('revol', '')).strip() or str(stock.get('rel_volume', '')).strip()
        float_val = str(stock.get('float', '')).strip()
        
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
            f"📰 أخبار اليوم: {html.escape(stock_news)}"
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
            f"🏛️ <a href='{html.escape(sec_page_url)}'>SEC</a>"
        ])
        
        message = "\n".join(message_lines)
        
        if send_telegram_message(message, parse_mode="HTML"):
            state[key] = {"price": price, "alert_no": alert_no}
            sent += 1
            time.sleep(1)
    
    return sent

def get_market_session_and_name():
    ny_tz = pytz.timezone('America/New_York')
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

def collect_signal_stocks(signal_name, positive_only=False, skip_tickers=None):
    skip_tickers = skip_tickers or set()
    session_key, _ = get_market_session_and_name()
    
    # 🌟 التخفيف في pre فقط: خفض الحد الأدنى للحجم في التقرير المجمع أيضاً
    min_volume = 1000 if session_key == "Pre-Market" else 10000

    try:
        raw_stocks = fetch_finviz_signal(signal_name, limit=30)
        valid = []
        for stock in raw_stocks:
            ticker = str(stock.get('row_key', '')).strip().upper()
            if not ticker or ticker in skip_tickers:
                continue
                
            price = to_float(stock.get('price'))
            if price is None or price <= 0:
                continue
                
            volume = to_float(stock.get('volume'))
            if volume is None or volume < min_volume:
                continue
                
            if is_excluded_sector(stock):
                continue
                
            change_val = to_float(stock.get('change'))
            if positive_only and (change_val is None or change_val <= 0):
                continue

            change_str = str(stock.get('change', '0%'))
            if not change_str.startswith("+") and not change_str.startswith("-") and change_val and change_val > 0:
                change_str = "+" + change_str

            valid.append({
                "ticker": ticker,
                "price": price,
                "change": change_val if change_val is not None else 0.0,
                "change_str": change_str
            })
        return valid
    except Exception as e:
        print(f"❌ خطأ في جلب إشارة {signal_name}: {e}")
        return []

def build_summary_report(session_title):
    global last_summary_tickers
    seen_tickers = set()
    
    gainers = collect_signal_stocks('ta_topgainers', positive_only=True)
    for s in gainers:
        seen_tickers.add(s["ticker"])
        
    new_highs = collect_signal_stocks('ta_newhigh', positive_only=True, skip_tickers=seen_tickers)
    for s in new_highs:
        seen_tickers.add(s["ticker"])
        
    most_active = collect_signal_stocks('ta_mostactive', positive_only=True, skip_tickers=seen_tickers)
    for s in most_active:
        seen_tickers.add(s["ticker"])
        
    unusual_vol = collect_signal_stocks('ta_unusualvolume', positive_only=True, skip_tickers=seen_tickers)

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
    top_12 = final_list[:12]

    sa_tz = pytz.timezone('Asia/Riyadh')
    time_sa = datetime.now(sa_tz).strftime("%I:%M %p")
    
    current_tickers = {item["ticker"] for item in top_12}
    
    lines = [
        f"⏱️ <b>{session_title} · آخر 15 دقيقة · {time_sa} بتوقيت السعودية</b>\n"
    ]

    for idx, item in enumerate(top_12, 1):
        t = item["ticker"]
        tv_url = f"https://www.tradingview.com/chart/?symbol={t}"
        ch_str = item["change_str"]
        
        is_new = t not in last_summary_tickers
        new_mark = " 🆕" if is_new else ""
        
        lines.append(f"{idx}. <a href='{tv_url}'>{t}</a> {ch_str} · ${item['price']:.2f}{new_mark}")

    last_summary_tickers = current_tickers
    return "\n".join(lines)

def run_5min_individual_alerts(session_name):
    seen_tickers = set()

    try:
        top_gainers = fetch_finviz_signal('ta_topgainers', limit=20)
        send_stock_cards(session_name, top_gainers, "Top Gainers")
        for s in top_gainers:
            t = str(s.get('row_key', '')).strip().upper()
            if t:
                seen_tickers.add(t)
    except Exception as e:
        print(f"❌ خطأ في Gainers: {e}")

    time.sleep(2)

    try:
        new_highs = fetch_finviz_signal('ta_newhigh', limit=20)
        send_stock_cards(session_name, new_highs, "New High", skip_tickers=seen_tickers)
        for s in new_highs:
            t = str(s.get('row_key', '')).strip().upper()
            if t:
                seen_tickers.add(t)
    except Exception as e:
        print(f"❌ خطأ في New High: {e}")

    time.sleep(2)

    try:
        most_active = fetch_finviz_signal('ta_mostactive', limit=20)
        send_stock_cards(session_name, most_active, "Most Active", positive_only=True, skip_tickers=seen_tickers)
        for s in most_active:
            t = str(s.get('row_key', '')).strip().upper()
            if t:
                seen_tickers.add(t)
    except Exception as e:
        print(f"❌ خطأ في Most Active: {e}")

    time.sleep(2)

    try:
        unusual_volume = fetch_finviz_signal('ta_unusualvolume', limit=20)
        send_stock_cards(session_name, unusual_volume, "Unusual Volume", positive_only=True, skip_tickers=seen_tickers)
    except Exception as e:
        print(f"❌ خطأ في Unusual Volume: {e}")

def main_loop():
    print("🚀 تم تشغيل الرادار (التنبيهات الفردية كل 5 دقائق + التقرير المجمع كل 15 دقيقة)...")
    was_open = False
    last_summary_time = 0
    
    while True:
        session_key, session_title = get_market_session_and_name()
        current_time_epoch = time.time()
        
        if session_key:
            if not was_open:
                send_telegram_message(f"🟢 <b>بدء رادار التداول الحركي لـ {session_title}</b>")
                was_open = True
            
            if current_time_epoch - last_summary_time >= 900:
                print(f"⏳ جاري إنشاء وإرسال التقرير المجمع لـ ({session_title})...")
                report_message = build_summary_report(session_title)
                if report_message:
                    send_telegram_message(report_message, parse_mode="HTML")
                    print("✅ تم إرسال التقرير المجمع بنجاح.")
                last_summary_time = time.time()

            print(f"⏳ فحص وإرسال التنبيهات الفردية لـ ({session_title})...")
            run_5min_individual_alerts(session_key)
            
            print("💤 التحديث القادم بعد 5 دقائق...")
            time.sleep(300)
        else:
            if was_open:
                send_telegram_message("🔴 <b>انتهت فترات التداول بالكامل (إغلاق السوق). يتوقف الرادار الآن مؤقتاً لحين الجلسة القادمة.</b>")
                was_open = False
                last_summary_time = 0
                
            print("😴 السوق مغلق حالياً. الفحص التنشيطي القادم بعد 15 دقيقة...")
            time.sleep(900)

if __name__ == "__main__":
    main_loop()
