import os
import time
import html
from html.parser import HTMLParser
from datetime import datetime
import pytz
import requests

# قراءة الإعدادات الخاصة بك
BOT_TOKEN = os.getenv("BOT_TOKEN")
CHAT_ID = os.getenv("CHAT_ID")

FINVIZ_HEADERS = {
    "User-Agent": "Mozilla/5.0 (iPhone; CPU iPhone OS 17_0 like Mac OS X) AppleWebKit/605.1.15 (KHTML, like Gecko) Version/17.0 Mobile/15E148 Safari/604.1"
}

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
    """جلب أفضل الأسهم لـ Signal معيّن"""
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

def send_telegram_message(message, parse_mode="HTML"):
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

def to_float(value):
    try:
        return float(str(value).replace(",", "").replace("%", "").strip())
    except (TypeError, ValueError):
        return None

def is_excluded_sector(stock):
    """استبعاد أسهم البنوك والمطاعم والمشروبات والتأمين"""
    sector = str(stock.get('sector', '')).lower()
    industry = str(stock.get('industry', '')).lower()
    exclude_keywords = ['bank', 'insurance', 'restaurant', 'beverage']
    for kw in exclude_keywords:
        if kw in sector or kw in industry:
            return True
    return False

def get_market_session_and_name():
    """تحديد الجلسة الحالية وتوقيت السعودية (للعرض)"""
    ny_tz = pytz.timezone('America/New_York')
    now_ny = datetime.now(ny_tz)
    
    if now_ny.weekday() >= 5:
        return None, None
        
    current_time = now_ny.time()
    
    pre_start = datetime.strptime("04:00", "%H:%M").time()
    main_start = datetime.strptime("09:30", "%H:%M").time()
    main_end = datetime.strptime("16:00", "%H:%M").time()
    post_end = datetime.strptime("20:00", "%H:%M").time()
    
    session_key = None
    session_title = ""
    
    if pre_start <= current_time < main_start:
        session_key = "Pre-Market"
        session_title = "ما قبل السوق"
    elif main_start <= current_time < main_end:
        session_key = "Main Session"
        session_title = "الجلسة الرئيسية"
    elif main_end <= current_time <= post_end:
        session_key = "Post-Market"
        session_title = "ما بعد السوق"
        
    return session_key, session_title

def process_and_build_report(session_key, session_title):
    """جلب وتصفية الأسهم وبناء تقرير لأعلى 7 أسهم"""
    top_gainers = fetch_finviz_signal('ta_topgainers', limit=20)
    
    valid_stocks = []
    for stock in top_gainers:
        ticker = str(stock.get('row_key', '')).strip().upper()
        price = to_float(stock.get('price'))
        change_val = to_float(stock.get('change'))
        volume = to_float(stock.get('volume'))
        
        if not ticker or price is None or price <= 0:
            continue
        if volume is None or volume <= 10000:
            continue
        if is_excluded_sector(stock):
            continue
        if change_val is None or change_val <= 0:
            continue
            
        rel_vol = str(stock.get('revol', '')).strip() or str(stock.get('rel_volume', '')).strip()
        
        valid_stocks.append({
            "ticker": ticker,
            "price": price,
            "change": change_val,
            "change_str": str(stock.get('change', '0%')),
            "rvol": rel_vol if rel_vol and rel_vol not in ["-", "nan", "None", ""] else "1x"
        })
    
    if not valid_stocks:
        return None

    # ترتيب حسب الأعلى نسبة تغير واختيار أفضل 7 أسهم (أو ما هو متوفر حتى 7)
    valid_stocks.sort(key=lambda x: x["change"], reverse=True)
    top_7 = valid_stocks[:7]

    # التوقيت المحلي (بتوقيت السعودية)
    sa_tz = pytz.timezone('Asia/Riyadh')
    time_sa = datetime.now(sa_tz).strftime("%I:%M %p")
    
    lines = [
        f"⏱️ <b>أقوى المتحركين · {session_title} · آخر 30 دقيقة · {time_sa} بتوقيت السعودية</b>\n"
    ]

    for idx, item in enumerate(top_7, 1):
        t = item["ticker"]
        tv_url = f"https://www.tradingview.com/chart/?symbol={t}"
        ch_str = item["change_str"]
        if not ch_str.startswith("+") and not ch_str.startswith("-"):
            ch_str = "+" + ch_str
            
        rvol_clean = item["rvol"].replace("x", "")
        
        lines.append(f"{idx}. <a href='{tv_url}'>{t}</a> {ch_str} · ${item['price']:.2f} · RVOL {rvol_clean}x")
        lines.append("• 🆕\n")

    return "\n".join(lines)

def main_loop():
    print("🚀 تم تشغيل الرادار بالتقرير المجمع (أعلى 7 أسهم)...")
    was_open = False
    
    while True:
        session_key, session_title = get_market_session_and_name()
        
        if session_key:
            if not was_open:
                send_telegram_message(f"🟢 <b>بدء رادار التداول الحركي لـ {session_title}</b>")
                was_open = True
                
            print(f"⏳ جاري فحص وإنشاء التقرير المجمع لـ ({session_title})...")
            report_message = process_and_build_report(session_key, session_title)
            
            if report_message:
                send_telegram_message(report_message, parse_mode="HTML")
                print("✅ تم إرسال التقرير المجمع بنجاح.")
            else:
                print("⚠️ لا توجد أسهم مطابقة للشروط في هذا التحديث.")
                
            print("💤 التحديث القادم بعد 15 دقيقة...")
            time.sleep(900)  # التحديث كل 15 دقيقة
        else:
            if was_open:
                send_telegram_message("🔴 <b>انتهت فترات التداول بالكامل (إغلاق السوق). يتوقف الرادار الآن مؤقتاً لحين الجلسة القادمة.</b>")
                was_open = False
                
            print("😴 السوق مغلق حالياً. الفحص التنشيطي القادم بعد 15 دقيقة...")
            time.sleep(900)

if __name__ == "__main__":
    main_loop()
