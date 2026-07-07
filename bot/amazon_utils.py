"""
كل الدوال المتعلقة بأمازون: استخراج ASIN، بناء رابط أفلييت، وجلب أقل سعر.
الأسعار تُجلب مباشرة من صفحات أمازون (بدون PA API).
استراتيجية مزدوجة: نسخة سطح المكتب أولاً ثم نسخة الجوال كـ fallback.
"""
import json
import logging
import re
import time
import requests
from bs4 import BeautifulSoup
from config import AFFILIATE_TAG, AMAZON_DOMAIN

logger = logging.getLogger(__name__)

# ---- كاش الأسعار: ASIN → (timestamp, offer_dict) ----
# يخزن النتائج 90 دقيقة لتجنب طلبات متكررة لأمازون
import threading
_CACHE: dict[str, tuple[float, dict]] = {}
_CACHE_TTL = 90 * 60  # 90 دقيقة بالثواني
_CACHE_LOCK = threading.Lock()

# ---- حد الطلبات المتزامنة لأمازون ----
# يمنع إرسال عشرات الطلبات في نفس الوقت للـ IP الواحد
_SCRAPE_SEMAPHORE = threading.Semaphore(4)  # أقصى 4 طلبات متزامنة

# ---- خريطة الأرقام العربية (13 حرف مصدر ↔ 13 هدف) ----
_AR_NUM_MAP = str.maketrans("٠١٢٣٤٥٦٧٨٩٫٬،", "0123456789.,,")

# ---- User-Agents متنوعة ----
_UA_DESKTOP = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
    "AppleWebKit/537.36 (KHTML, like Gecko) "
    "Chrome/124.0.0.0 Safari/537.36"
)
_UA_MOBILE = (
    "Mozilla/5.0 (Linux; Android 13; Pixel 7) "
    "AppleWebKit/537.36 (KHTML, like Gecko) "
    "Chrome/124.0.0.0 Mobile Safari/537.36"
)

_HEADERS_DESKTOP = {
    "User-Agent": _UA_DESKTOP,
    "Accept-Language": "ar-SA,ar;q=0.9,en-US;q=0.8,en;q=0.7",
    "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
    "Accept-Encoding": "gzip, deflate, br",
    "Connection": "keep-alive",
    "DNT": "1",
    "Upgrade-Insecure-Requests": "1",
    "sec-fetch-dest": "document",
    "sec-fetch-mode": "navigate",
    "sec-fetch-site": "none",
    "sec-fetch-user": "?1",
}

_HEADERS_MOBILE = {
    "User-Agent": _UA_MOBILE,
    "Accept-Language": "ar-SA,ar;q=0.9,en-US;q=0.8",
    "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
    "Accept-Encoding": "gzip, deflate, br",
    "Connection": "keep-alive",
}


# =============================================================
# دوال مساعدة
# =============================================================

def _make_session(mobile: bool = False, domain: str = "amazon.sa") -> requests.Session:
    """يُنشئ جلسة HTTP بترويسات مناسبة وكوكيز أمازون."""
    s = requests.Session()
    s.headers.update(_HEADERS_MOBILE if mobile else _HEADERS_DESKTOP)
    s.cookies.set("i18n-prefs", "SAR", domain=f".{domain}")
    s.cookies.set("lc-acbsa", "ar_SA",  domain=f".{domain}")
    return s


def _parse_price(text: str) -> float | None:
    """
    يحول نص سعر (عربي أو إنجليزي) إلى رقم عشري.
    '١٢٣٫٤٥ ر.س' → 123.45  |  '1,234.00 SAR' → 1234.0
    """
    if not text:
        return None
    normalized = text.translate(_AR_NUM_MAP)
    # ابحث عن أول تسلسل رقمي (يتجاهل النص بعده مثل "ر.س")
    m = re.search(r"\d[\d,\.]*", normalized)
    if not m:
        return None
    cleaned = m.group(0).rstrip(".,")
    if not cleaned:
        return None
    if "." in cleaned and "," in cleaned:
        if cleaned.rindex(".") > cleaned.rindex(","):
            cleaned = cleaned.replace(",", "")
        else:
            cleaned = cleaned.replace(".", "").replace(",", ".")
    elif cleaned.count(".") > 1:
        parts = cleaned.rsplit(".", 1)
        cleaned = parts[0].replace(".", "") + "." + parts[1]
    else:
        cleaned = cleaned.replace(",", "")
    try:
        val = float(cleaned)
        return val if val > 0 else None
    except ValueError:
        return None


def _is_blocked(soup: BeautifulSoup, html: str) -> bool:
    """يكتشف صفحات CAPTCHA أو الحجب."""
    title = (soup.title.string or "") if soup.title else ""
    return any([
        "robot" in title.lower(),
        "captcha" in html.lower(),
        "automated access" in html.lower(),
        'id="captchacharacters"' in html.lower(),
    ])


def _extract_price_desktop(soup: BeautifulSoup) -> tuple[float | None, str]:
    """
    يستخرج السعر من صفحة سطح المكتب.
    يجرب selectors متعددة من الأكثر موثوقية للأقل.
    """
    # --- المجموعة الأولى: السعر الكامل مع العملة ---
    full_selectors = [
        ".priceToPay .a-offscreen",
        "span.apexPriceToPay .a-offscreen",
        "#corePrice_feature_div .a-offscreen",
        "#corePrice_desktop .a-offscreen",
        "#apex_desktop .a-offscreen",
        "#priceblock_ourprice",
        "#priceblock_dealprice",
        "#price_inside_buybox",
        "#price",
        "span.a-offscreen",
    ]
    for sel in full_selectors:
        el = soup.select_one(sel)
        if not el:
            continue
        raw = el.get_text(strip=True)
        val = _parse_price(raw)
        if val and 1 < val < 500_000:
            return val, raw

    # --- المجموعة الثانية: السعر مقسّم (whole + fraction) ---
    whole_el = soup.select_one("span.a-price-whole")
    frac_el  = soup.select_one("span.a-price-fraction")
    if whole_el:
        whole = re.sub(r"[^\d]", "", whole_el.get_text())
        frac  = re.sub(r"[^\d]", "", frac_el.get_text()) if frac_el else "00"
        raw   = f"{whole}.{frac}"
        val   = _parse_price(raw)
        if val and 1 < val < 500_000:
            return val, raw

    return None, ""


def _extract_price_from_json(html: str) -> tuple[float | None, str]:
    """
    يحاول استخراج السعر من بيانات JSON المضمّنة في صفحة أمازون.
    أمازون يضمّن أحياناً بيانات المنتج كـ JSON في script tags.
    """
    # نمط 1: "priceAmount":"239.00"
    m = re.search(r'"priceAmount"\s*:\s*"?([\d.]+)"?', html)
    if m:
        val = _parse_price(m.group(1))
        if val and 1 < val < 500_000:
            return val, m.group(1)

    # نمط 2: "price":{"value":"239.00"
    m = re.search(r'"price"\s*:\s*\{[^}]*"value"\s*:\s*"?([\d.]+)"?', html)
    if m:
        val = _parse_price(m.group(1))
        if val and 1 < val < 500_000:
            return val, m.group(1)

    # نمط 3: JSON-LD schema.org
    for script in re.findall(r'<script[^>]*type="application/ld\+json"[^>]*>(.*?)</script>', html, re.DOTALL):
        try:
            data = json.loads(script)
            offers = data.get("offers") or data.get("Offers")
            if isinstance(offers, dict):
                price = offers.get("price") or offers.get("Price")
                if price:
                    val = _parse_price(str(price))
                    if val and 1 < val < 500_000:
                        return val, str(price)
        except Exception:
            continue

    return None, ""


def _scrape_desktop(asin: str, domain: str) -> dict | None:
    """يجلب نسخة سطح المكتب ويستخرج البيانات."""
    url = f"https://www.{domain}/dp/{asin}"
    try:
        session = _make_session(mobile=False, domain=domain)
        # زيارة الصفحة الرئيسية أولاً للحصول على كوكيز طبيعية
        session.get(f"https://www.{domain}", timeout=10, allow_redirects=True)
        time.sleep(0.8)
        resp = session.get(url, timeout=18, allow_redirects=True)

        if resp.status_code != 200:
            logger.warning("Desktop: status %s للـ ASIN %s", resp.status_code, asin)
            return None

        soup = BeautifulSoup(resp.text, "html.parser")

        if _is_blocked(soup, resp.text):
            logger.info("Desktop محجوب بـ CAPTCHA للـ ASIN %s", asin)
            return {"blocked": True}

        title_el = soup.find(id="productTitle")
        title    = title_el.get_text(strip=True) if title_el else None

        price_val, price_text = _extract_price_desktop(soup)
        if not price_val:
            price_val, price_text = _extract_price_from_json(resp.text)

        seller_el   = soup.select_one("#sellerProfileTriggerId") or soup.select_one("#merchant-info a")
        seller_name = seller_el.get_text(strip=True) if seller_el else "Amazon.sa"
        is_prime    = bool(soup.select_one(".a-icon-prime, [aria-label*='Prime']"))

        return {
            "title": title,
            "price_val": price_val,
            "price_text": price_text,
            "seller_name": seller_name,
            "is_prime": is_prime,
        }
    except Exception as e:
        logger.error("Desktop scrape خطأ للـ ASIN %s: %s", asin, e)
        return None


def _scrape_mobile(asin: str, domain: str) -> dict | None:
    """
    يجلب نسخة الجوال كـ fallback — عادةً أقل صرامة من ناحية bot detection.
    """
    url = f"https://www.{domain}/dp/{asin}"
    try:
        session = _make_session(mobile=True, domain=domain)
        resp = session.get(url, timeout=18, allow_redirects=True)

        if resp.status_code != 200:
            return None

        soup = BeautifulSoup(resp.text, "html.parser")

        if _is_blocked(soup, resp.text):
            logger.info("Mobile أيضاً محجوب للـ ASIN %s", asin)
            return {"blocked": True}

        title_el = soup.find(id="productTitle") or soup.select_one("h1")
        title    = title_el.get_text(strip=True) if title_el else None

        # Mobile selectors مختلفة قليلاً
        price_val, price_text = _extract_price_desktop(soup)  # نفس الـ selectors تعمل
        if not price_val:
            price_val, price_text = _extract_price_from_json(resp.text)

        is_prime = bool(soup.select_one(".a-icon-prime, [aria-label*='Prime']"))

        return {
            "title": title,
            "price_val": price_val,
            "price_text": price_text,
            "seller_name": "Amazon.sa",
            "is_prime": is_prime,
        }
    except Exception as e:
        logger.error("Mobile scrape خطأ للـ ASIN %s: %s", asin, e)
        return None


def _scrape_offer_listing(asin: str, domain: str) -> list[dict]:
    """يجلب صفحة قائمة البائعين ويرجع قائمة مرتبة من الأرخص للأغلى."""
    url = f"https://www.{domain}/gp/offer-listing/{asin}?condition=new&sort=price"
    offers = []
    try:
        session = _make_session(mobile=False, domain=domain)
        resp = session.get(url, timeout=15, allow_redirects=True)
        if resp.status_code != 200:
            return offers

        soup = BeautifulSoup(resp.text, "html.parser")
        if _is_blocked(soup, resp.text):
            return offers

        for row in soup.select("div.olpOffer"):
            price_el = row.select_one(".olpOfferPrice")
            if not price_el:
                continue
            raw = price_el.get_text(strip=True)
            val = _parse_price(raw)
            if not val or not (1 < val < 500_000):
                continue

            seller_el   = row.select_one(".olpSellerName")
            seller_name = seller_el.get_text(strip=True) if seller_el else (
                "Amazon.sa" if row.select_one("img[src*='amazon']") else "بائع خارجي"
            )
            is_prime = bool(row.select_one(".a-icon-prime, [aria-label*='Prime']"))

            offers.append({"price_val": val, "price_text": raw,
                           "seller_name": seller_name, "is_prime": is_prime})

        offers.sort(key=lambda x: x["price_val"])
    except Exception as e:
        logger.error("offer-listing خطأ للـ ASIN %s: %s", asin, e)

    return offers


# =============================================================
# الدوال العامة
# =============================================================

def resolve_short_link(url: str) -> str:
    """يحل أي رابط مختصر عبر متابعة إعادة التوجيه."""
    try:
        session = _make_session()
        response = session.get(url, allow_redirects=True, timeout=12)
        return response.url
    except requests.RequestException as e:
        logger.error("فشل فك تتبع الرابط: %s", e)
        return url


def extract_asin(url: str) -> str | None:
    """يستخرج ASIN من الرابط — يدعم كل الأنماط."""
    url_match = re.search(r"https?://\S+", url)
    if url_match:
        url = url_match.group(0)
    patterns = [
        r"/dp/([A-Z0-9]{10})",
        r"/gp/product/([A-Z0-9]{10})",
        r"/product/([A-Z0-9]{10})",
        r"[?&]asin=([A-Z0-9]{10})",
        r"/aw/d/([A-Z0-9]{10})",
        r"/d/([A-Z0-9]{10})",
    ]
    for pattern in patterns:
        m = re.search(pattern, url, re.IGNORECASE)
        if m:
            return m.group(1).upper()
    return None


def extract_domain(url: str) -> str:
    """يستخرج نطاق أمازون من الرابط."""
    m = re.search(r"://(?:www\.)?(amazon\.[a-z.]+)", url, re.IGNORECASE)
    return m.group(1).lower() if m else AMAZON_DOMAIN


def build_affiliate_link(asin: str, domain: str = AMAZON_DOMAIN) -> str:
    return f"https://www.{domain}/dp/{asin}?tag={AFFILIATE_TAG}"


def get_lowest_offer(asin: str, domain: str = AMAZON_DOMAIN) -> dict | None:
    """
    يجلب أرخص سعر متاح للمنتج.
    - يتحقق من الـ cache أولاً (90 دقيقة TTL)
    - يستخدم semaphore لتقييد الطلبات المتزامنة لأمازون
    - الترتيب: Desktop → Mobile (fallback) → offer-listing للمقارنة
    """
    import time
    cache_key = f"{domain}:{asin}"

    # تحقق من الـ cache
    with _CACHE_LOCK:
        if cache_key in _CACHE:
            ts, cached = _CACHE[cache_key]
            if time.time() - ts < _CACHE_TTL:
                logger.info("Cache hit للـ ASIN %s", asin)
                return cached

    affiliate_link = build_affiliate_link(asin, domain)

    # الـ semaphore يمنع أكثر من 4 طلبات متزامنة لأمازون
    with _SCRAPE_SEMAPHORE:
        import time

        # --- المحاولة الأولى: سطح المكتب ---
        page_data = _scrape_desktop(asin, domain)

        # --- المحاولة الثانية: الجوال إذا تم الحجب ---
        if not page_data or page_data.get("blocked"):
            logger.info("تجربة نسخة الجوال للـ ASIN %s", asin)
            page_data = _scrape_mobile(asin, domain)

        # --- كلاهما محجوب ---
        if not page_data:
            return None
        if page_data.get("blocked"):
            return {"blocked": True, "affiliate_link": affiliate_link}

        title           = page_data.get("title")
        main_price_val  = page_data.get("price_val")
        main_price_text = page_data.get("price_text", "")
        main_seller     = page_data.get("seller_name", "Amazon.sa")
        main_is_prime   = page_data.get("is_prime", False)

        # --- قائمة البائعين للمقارنة ---
        offers      = _scrape_offer_listing(asin, domain)
        offer_count = len(offers) if offers else 1

        if offers and offers[0]["price_val"] < (main_price_val or float("inf")):
            best = offers[0]
            best_price_val  = best["price_val"]
            best_price_text = best["price_text"]
            best_seller     = best["seller_name"]
            best_is_prime   = best["is_prime"]
        elif main_price_val:
            best_price_val  = main_price_val
            best_price_text = main_price_text
            best_seller     = main_seller
            best_is_prime   = main_is_prime
        else:
            logger.warning("ما لقينا سعراً للـ ASIN %s", asin)
            return None

        display_price = best_price_text.strip()
        if display_price and "SAR" not in display_price and "ر.س" not in display_price:
            display_price = f"{display_price} SAR"

        result = {
            "asin":        asin,
            "title":       title,
            "price":       display_price,
            "price_val":   best_price_val,
            "currency":    "SAR",
            "seller_name": best_seller,
            "condition":   "جديد",
            "is_prime":    best_is_prime,
            "offer_count": offer_count,
            "affiliate_link": affiliate_link,
        }

        # خزّن في الـ cache
        with _CACHE_LOCK:
            _CACHE[cache_key] = (time.time(), result)

        return result


def format_offer_message(offer: dict) -> str:
    """يبني رسالة تيليجرام تعرض أرخص سعر متاح."""
    if not offer:
        return (
            "❌ ما قدرت ألقى عروض متاحة لهذا المنتج.\n"
            "تأكد من توفر المنتج في المتجر أو جرّب لاحقًا."
        )

    if offer.get("blocked"):
        return (
            "⚠️ أمازون يطلب تحقق من الهوية حالياً.\n"
            f"شوف السعر مباشرة:\n{offer['affiliate_link']}"
        )

    title_part   = f"📦 *{offer['title'][:70]}*\n\n" if offer.get("title") else ""
    prime_badge  = " 🔵 Prime" if offer.get("is_prime") else ""
    offer_count  = offer.get("offer_count", 1)
    sellers_note = f"_(من بين {offer_count} بائع متاح)_\n" if offer_count > 1 else ""

    return (
        f"{title_part}"
        f"🏷️ *أرخص سعر متاح الآن:*\n"
        f"• السعر: `{offer['price']}`\n"
        f"• البائع: {offer['seller_name']}{prime_badge}\n"
        f"• الحالة: {offer['condition']}\n"
        f"{sellers_note}\n"
        f"🛒 *رابط الشراء:*\n{offer['affiliate_link']}\n\n"
        f"_(رابط تسويق بالعمولة)_"
    )
