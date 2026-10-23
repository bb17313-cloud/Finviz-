import os
import time
import html
from html.parser import HTMLParser
from datetime import datetime
import pytz
import requests

# قراءة الإعدادات بالصيغة الخاصة بك
BOT_TOKEN = os.getenv("BOT_TOKEN")
CHAT_ID = os.getenv("CHAT_ID")

FINVIZ_HEADERS = {
    "User-Agent": "Mozilla/5.0 (iPhone; CPU iPhone OS 17_0 like Mac OS X) AppleWebKit/605.1.15 (KHTML, like Gecko) Version/17.0 Mobile/15E148 Safari/604.1"
}

# سجل التنبيهات: يمنع التكرار إذا لم يتغير السعر ويرقّم التنبيهات لكل سهم
alerts_state = {"date": None, "stocks": {}}

class FinvizRowsParser(HTMLParser):
    """يقرأ صفوف جدول الفاحص مباشرة: كل صف data-row-key وكل خلية data-column-id"""
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

def fetch_finviz_signal(signal, limit=20):
    """جلب أسهم Signal معيّن من صفحة الفاحص مباشرة (بدون مكتبة finviz)"""
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

def send_telegram_message(message, parse_mode="Markdown"):
    """إرسال التقرير المنسق إلى التلغرام"""
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
    """إرجاع سجل التنبيهات وتصفيره عند بداية يوم جديد (توقيت نيويورك)"""
    today = datetime.now(pytz.timezone('America/New_York')).strftime("%Y-%m-%d")
    if alerts_state["date"] != today:
        alerts_state["date"] = today
        alerts_state["stocks"] = {}
    return alerts_state["stocks"]

def to_float(value):
    try:
        return float(str(value).replace(",", "").replace("%", "").strip())
    except (TypeError, ValueError):
        return None

# بنود نموذج 8-K كما تعرّفها SEC (تأتي مع الإفصاح نفسه في حقل items)
SEC_8K_ITEMS = {
    "1.01": "اتفاقية جوهرية",
    "1.02": "إنهاء اتفاقية جوهرية",
    "1.03": "إفلاس / حراسة قضائية",
    "2.01": "استحواذ / دمج أو بيع أصول",
    "2.02": "إعلان نتائج مالية",
    "2.03": "التزام مالي / دين جديد",
    "2.04": "تسريع التزام مالي",
    "2.05": "تكاليف إعادة هيكلة",
    "2.06": "انخفاض جوهري في قيمة أصول",
    "3.01": "إشعار شطب / مخالفة شروط الإدراج",
    "3.02": "إصدار أسهم غير مسجل",
    "3.03": "تعديل حقوق المساهمين",
    "4.01": "تغيير المدقق",
    "4.02": "إعادة صياغة قوائم مالية",
    "5.01": "تغيّر السيطرة",
    "5.02": "تغييرات الإدارة",
    "5.03": "حوكمة الشركة",
    "5.05": "حوكمة الشركة",
    "5.07": "تصويت المساهمين",
    "5.08": "ترشيحات مجلس الإدارة",
    "7.01": "إفصاح عادل (Reg FD)",
    "8.01": "أحداث أخرى",
    "9.01": "مرفقات مالية",
}

# أنواع الإفصاحات الأخرى (النوع نفسه يحدد معناه)
SEC_FORM_NAMES = {
    "10-K": "التقرير السنوي",
    "10-Q": "التقرير الفصلي",
    "20-F": "التقرير السنوي (مصدر أجنبي)",
    "6-K": "تقرير مصدر أجنبي",
    "3": "بيان ملكية مبدئي لمطلع",
    "4": "تغيّر ملكية مطلع",
    "5": "بيان ملكية سنوي لمطلع",
    "144": "إشعار بيع أسهم لمطلع",
    "S-1": "طرح / تسجيل أوراق مالية",
    "S-3": "طرح / تسجيل أوراق مالية",
    "F-1": "طرح / تسجيل أوراق مالية",
    "F-3": "طرح / تسجيل أوراق مالية",
    "S-8": "تسجيل أسهم برامج الموظفين",
    "DEF 14A": "بيان الجمعية العمومية (حوكمة)",
    "PRE 14A": "بيان الجمعية العمومية (حوكمة)",
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
                            filing_url = ("https://www.sec.gov/Archives/edgar/data/" f"{cik_raw}/{acc_clean}/{p_doc}")
                        else:
                            filing_url = ("https://www.sec.gov/Archives/edgar/data/" f"{cik_raw}/{acc_clean}/{acc_num}-index.htm")
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
    """استبعاد أسهم البنوك والمطاعم والمشروبات والتأمين"""
    sector = str(stock.get('sector', '')).lower()
    industry = str(stock.get('industry', '')).lower()
    exclude_keywords = ['bank', 'insurance', 'restaurant', 'beverage']
    for kw in exclude_keywords:
        if kw in sector or kw in industry:
            return True
    return False

def send_stock_cards(session_name, stocks, signal_label, positive_only=False, skip_tickers=None):
    """إرسال كل سهم في بطاقة مستقلة، مع ترقيم التنبيهات ومنع التكرار عند ثبات السعر"""
    state = get_alert_state()
    sent = 0
    skip_tickers = skip_tickers or set()
    
    for stock in stocks:
        ticker = str(stock.get('row_key', '')).strip().upper()
        price = to_float(stock.get('price'))
        if not ticker or price is None or price <= 0:
            continue
        
        # تخطي السهم إذا كان موجوداً مسبقاً في Signals أخرى
        if ticker in skip_tickers:
            continue
        
        # شرط الحجم: أكبر من 10 آلاف سهم
        volume = to_float(stock.get('volume'))
        if volume is None or volume <= 10000:
            continue
        
        # استبعاد البنوك والمطاعم والمشروبات والتأمين
        if is_excluded_sector(stock):
            continue
        
        # Most Active / Unusual Volume: إرسال الأسهم الموجبة فقط (ارتفاع عن الإغلاق السابق)
        if positive_only:
            change_val = to_float(stock.get('change'))
            if change_val is None or change_val <= 0:
                continue
        
        # مفتاح موحد للسهم داخل الجلسة (يمنع التكرار عبر الـ Signals المختلفة)
        key = f"{session_name}:{ticker}"
        last = state.get(key)
        
        # بعد التنبيه الأول: لا تكرار إلا إذا ارتفع السعر عن سعر آخر تنبيه
        if last and price <= last["price"]:
            continue
        
        alert_no = (last["alert_no"] + 1) if last else 1
        sec_summary, cik_val, _ = fetch_sec_filings_only(ticker)
        tv_url = f"https://www.tradingview.com/chart/?symbol={ticker}"
        sec_page_url = (
            "https://www.sec.gov/cgi-bin/browse-edgar?action=getcompany"
            f"&CIK={cik_val or ticker}&type=&dateb=&owner=include&count=40"
        )
        
        message = (
            f"🚨 <b>تنبيه {alert_no}</b> · {html.escape(session_name)}\n"
            f"📡 Signal: <b>{html.escape(signal_label)}</b>\n\n"
            f"🔹 <b>{html.escape(ticker)}</b>\n"
            f"💵 <code>${price:.2f}</code> · 📈 <code>{html.escape(str(stock.get('change', '-')))}</code>\n"
            f"📊 Vol: <code>{html.escape(str(stock.get('volume', '-')))}</code>\n"
            f"🏭 القطاع: <code>{html.escape(str(stock.get('sector', '-')))}</code>\n"
            f"🏷️ الصناعة: <code>{html.escape(str(stock.get('industry', '-')))}</code>\n\n"
            f"📄 <b>آخر إفصاحات SEC</b>\n"
            f"{sec_summary}\n\n"
            f"📈 <a href='{tv_url}'>TradingView</a>\n"
            f"🏛️ <a href='{html.escape(sec_page_url)}'>SEC</a>"
        )
        
        if send_telegram_message(message, parse_mode="HTML"):
            state[key] = {"price": price, "alert_no": alert_no}
            sent += 1
            time.sleep(1)
    
    return sent

def get_market_session():
    """تحديد الجلسة الحالية بناءً على توقيت نيويورك وإرجاع اسمها، أو None إذا كان السوق مغلقاً"""
    ny_tz = pytz.timezone('America/New_York')
    now_ny = datetime.now(ny_tz)
    
    # السبت والأحد إجازة
    if now_ny.weekday() >= 5:
        return None
        
    current_time = now_ny.time()
    
    pre_start = datetime.strptime("04:00", "%H:%M").time()
    main_start = datetime.strptime("09:30", "%H:%M").time()
    main_end = datetime.strptime("16:00", "%H:%M").time()
    post_end = datetime.strptime("20:00", "%H:%M").time()
    
    if pre_start <= current_time < main_start:
        return "Pre-Market 🌅"
    elif main_start <= current_time < main_end:
        return "Main Session 🔔"
    elif main_end <= current_time <= post_end:
        return "Post-Market 🌆"
        
    return None

def fetch_and_alert(session_name):
    seen_tickers = set()  # لتجميع الأسهم المرسلة من Top Gainers / New High / Most Active

    # 1. تصفية وجلب الـ Top Gainers
    try:
        print(f"⏳ [{session_name}] فحص الأسهم الأكثر ارتفاعاً...")
        top_gainers = fetch_finviz_signal('ta_topgainers')
        if top_gainers:
            print(f"🔎 الأعمدة المتاحة: {list(top_gainers[0].keys())}")
        else:
            print("⚠️ لم يتم العثور على أسهم في Top Gainers.")
        
        sent = send_stock_cards(session_name, top_gainers, "Top Gainers")
        print(f"✅ Top Gainers: تم إرسال {sent} بطاقة.")
        for s in top_gainers:
            t = str(s.get('row_key', '')).strip().upper()
            if t:
                seen_tickers.add(t)
    except Exception as e:
        print(f"❌ خطأ في جلب الـ Gainers: {e}")

    time.sleep(4)  # فاصل أمان لحماية الـ IP

    # 2. تصفية وجلب الـ New High
    try:
        print(f"⏳ [{session_name}] فحص أسهم القمم الجديدة...")
        new_highs = fetch_finviz_signal('ta_newhigh')
        if not new_highs:
            print("⚠️ لم يتم العثور على أسهم في New High.")
        
        sent = send_stock_cards(session_name, new_highs, "New High")
        print(f"✅ New High: تم إرسال {sent} بطاقة.")
        for s in new_highs:
            t = str(s.get('row_key', '')).strip().upper()
            if t:
                seen_tickers.add(t)
    except Exception as e:
        print(f"❌ خطأ في جلب الـ New Highs: {e}")

    time.sleep(4)  # فاصل أمان لحماية الـ IP

    # 3. تصفية وجلب الـ Most Active
    try:
        print(f"⏳ [{session_name}] فحص الأسهم الأكثر نشاطاً...")
        most_active = fetch_finviz_signal('ta_mostactive')
        if not most_active:
            print("⚠️ لم يتم العثور على أسهم في Most Active.")
        
        sent = send_stock_cards(session_name, most_active, "Most Active", positive_only=True)
        print(f"✅ Most Active: تم إرسال {sent} بطاقة.")
        for s in most_active:
            t = str(s.get('row_key', '')).strip().upper()
            if t:
                seen_tickers.add(t)
    except Exception as e:
        print(f"❌ خطأ في جلب الـ Most Active: {e}")

    time.sleep(4)  # فاصل أمان لحماية الـ IP

    # 4. تصفية وجلب الـ Unusual Volume (موجب فقط + بدون تكرار مع الثلاثة السابقة)
    try:
        print(f"⏳ [{session_name}] فحص الأسهم ذات الحجم غير المعتاد...")
        unusual_volume = fetch_finviz_signal('ta_unusualvolume')
        if not unusual_volume:
            print("⚠️ لم يتم العثور على أسهم في Unusual Volume.")
        
        sent = send_stock_cards(session_name, unusual_volume, "Unusual Volume", positive_only=True, skip_tickers=seen_tickers)
        print(f"✅ Unusual Volume: تم إرسال {sent} بطاقة.")
    except Exception as e:
        print(f"❌ خطأ في جلب الـ Unusual Volume: {e}")

def main_loop():
    print("🚀 تم تشغيل الرادار بنظام الجلسات الثلاث (Pre / Main / Post)...")
    
    was_open = False
    
    while True:
        session_name = get_market_session()
        
        if session_name:
            if not was_open:
                send_telegram_message(f"🟢 *بدء رادار التداول الحركي لـ {session_name}*")
                was_open = True
                
            fetch_and_alert(session_name)
            print("💤 تم إرسال البيانات المحدثة. التحديث القادم بعد 5 دقائق...")
            time.sleep(300)  # إعادة الفحص كل 5 دقائق
        else:
            if was_open:
                send_telegram_message("🔴 *انتهت فترات التداول بالكامل (إغلاق الـ Post-Market). يتوقف الرادار الآن مؤقتاً لحين الجلسة القادمة.*")
                was_open = False
                
            print("😴 السوق مغلق حالياً (خارج أوقات الجلسات الثلاث أو إجازة). الفحص التنشيطي القادم بعد 15 دقيقة...")
            time.sleep(900)

if __name__ == "__main__":
    main_loop()

