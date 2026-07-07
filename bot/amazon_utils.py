"""
كل الدوال المتعلقة بأمازون: استخراج ASIN، بناء رابط أفلييت، وجلب أقل سعر.
الأسعار تُجلب مباشرة من صفحات أمازون (بدون PA API).
"""
import logging
import re
import requests
from bs4 import BeautifulSoup
from config import AFFILIATE_TAG, AMAZON_DOMAIN

logger = logging.getLogger(__name__)

# ترويسات تحاكي متصفح حقيقي
BROWSER_HEADERS = {
    "User-Agent": (
        "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
        "AppleWebKit/537.36 (KHTML, like Gecko) "
        "Chrome/124.0.0.0 Safari/537.36"
    ),
    "Accept-Language": "ar-SA,ar;q=0.9,en-US;q=0.8,en;q=0.7",
    "Accept": (
        "text/html,application/xhtml+xml,application/xml;"
        "q=0.9,image/avif,image/webp,*/*;q=0.8"
    ),
    "Accept-Encoding": "gzip, deflate, br",
    "Connection": "keep-alive",
    "DNT": "1",
    "Upgrade-Insecure-Requests": "1",
}

# ---- خريطة الأرقام العربية ----
# المصدر (13 حرف): أرقام عربية ٠-٩ + الفاصلة العشرية ٫ + الفاصلة الآلاف ٬ + الفاصلة العربية ،
# الهدف  (13 حرف): أرقام إنجليزية 0-9 + نقطة + فاصلة + فاصلة
_AR_NUM_MAP = str.maketrans("٠١٢٣٤٥٦٧٨٩٫٬،", "0123456789.,,")


def _make_session(domain: str = "amazon.sa") -> requests.Session:
    """يُنشئ جلسة HTTP بترويسات متصفح وcookies أمازون المناسبة."""
    s = requests.Session()
    s.headers.update(BROWSER_HEADERS)
    s.cookies.set("i18n-prefs", "SAR", domain=f".{domain}")
    s.cookies.set("lc-acbsa", "ar_SA",  domain=f".{domain}")
    return s


def _parse_price(text: str) -> float | None:
    """
    يحول نص سعر (عربي أو إنجليزي) إلى رقم عشري.
    مثال: '١٢٣٫٤٥ ر.س' → 123.45 | '1,234.00 SAR' → 1234.0
    """
    if not text:
        return None
    # 1. تحويل الأرقام العربية + فواصلها إلى لاتينية
    normalized = text.translate(_AR_NUM_MAP)
    # 2. ابحث عن أول رقم مع فواصله العشرية/الآلاف فقط — يتجاهل أي نص بعده مثل "ر.س"
    #    النمط: رقم اختياري (فاصلة/نقطة + أرقام)*
    m = re.search(r"\d[\d,\.]*", normalized)
    if not m:
        return None
    cleaned = m.group(0).rstrip(".,")  # احذف النقطة/الفاصلة من النهاية
    if not cleaned:
        return None
    # 3. حدد إذا الفاصلة عشرية أو آلاف
    if "." in cleaned and "," in cleaned:
        if cleaned.rindex(".") > cleaned.rindex(","):
            cleaned = cleaned.replace(",", "")          # 1,234.00 → 1234.00
        else:
            cleaned = cleaned.replace(".", "").replace(",", ".")  # 1.234,00 → 1234.00
    elif cleaned.count(".") > 1:
        # نقطتان أو أكثر → كل النقاط عدا الأخيرة هي فواصل آلاف
        parts = cleaned.rsplit(".", 1)
        cleaned = parts[0].replace(".", "") + "." + parts[1]
    else:
        cleaned = cleaned.replace(",", "")
    # 4. تحويل إلى float
    try:
        val = float(cleaned)
        return val if val > 0 else None
    except ValueError:
        return None


def _is_page_blocked(soup: BeautifulSoup, html: str) -> bool:
    """يكتشف إذا أمازون أعاد صفحة CAPTCHA أو حظر الطلب."""
    title = soup.title.string if soup.title else ""
    indicators = [
        "robot check" in title.lower(),
        "captcha" in html.lower(),
        "automated access" in html.lower(),
        "api.amazon" in html.lower() and "captcha" in html.lower(),
    ]
    return any(indicators)


def _extract_price_from_soup(soup: BeautifulSoup) -> tuple[float | None, str]:
    """
    يستخرج السعر من صفحة المنتج.
    يرجع (القيمة الرقمية, نص السعر للعرض).
    الـ selectors مرتبة من الأكثر موثوقية للأقل.
    """
    selectors = [
        # السعر الرئيسي — Buy Box
        ".priceToPay .a-offscreen",
        "#corePrice_feature_div .a-offscreen",
        "#corePrice_desktop .a-offscreen",
        "#apex_desktop .a-offscreen",
        # أسعار قديمة أو بديلة
        "#priceblock_ourprice",
        "#priceblock_dealprice",
        "#price_inside_buybox",
        "#price",
    ]
    for sel in selectors:
        el = soup.select_one(sel)
        if not el:
            continue
        raw = el.get_text(strip=True)
        # تجاهل السعر الأصلي المشطوب (القيمة الصغيرة جداً أو الكبيرة جداً تشير لخطأ)
        val = _parse_price(raw)
        if val and 1 < val < 500_000:
            return val, raw
    return None, ""


def _scrape_product_page(asin: str, domain: str) -> dict | None:
    """يجلب صفحة المنتج الرئيسية ويستخرج العنوان والسعر والبائع."""
    url = f"https://www.{domain}/dp/{asin}"
    try:
        session = _make_session(domain)
        resp = session.get(url, timeout=15, allow_redirects=True)
        if resp.status_code != 200:
            logger.warning("صفحة المنتج أرجعت %s للـ ASIN %s", resp.status_code, asin)
            return None

        soup = BeautifulSoup(resp.text, "lxml")

        if _is_page_blocked(soup, resp.text):
            logger.warning("أمازون طلب CAPTCHA للـ ASIN %s", asin)
            return {"blocked": True}

        # العنوان
        title_el = soup.select_one("#productTitle")
        title = title_el.get_text(strip=True) if title_el else None

        # السعر
        price_val, price_text = _extract_price_from_soup(soup)

        # البائع المباشر
        seller_el = (
            soup.select_one("#sellerProfileTriggerId")
            or soup.select_one("#merchant-info a")
        )
        seller_name = seller_el.get_text(strip=True) if seller_el else "Amazon.sa"

        # Prime
        is_prime = bool(soup.select_one(".a-icon-prime, [aria-label*='Prime']"))

        # حالة التوفر
        avail_el = soup.select_one("#availability span")
        availability = avail_el.get_text(strip=True) if avail_el else ""
        in_stock = bool(price_val) and "غير" not in availability and "unavailable" not in availability.lower()

        return {
            "title": title,
            "price_val": price_val,
            "price_text": price_text,
            "seller_name": seller_name,
            "is_prime": is_prime,
            "in_stock": in_stock,
        }
    except Exception as e:
        logger.error("خطأ في جلب صفحة المنتج %s: %s", asin, e)
        return None


def _scrape_offer_listing(asin: str, domain: str) -> list[dict]:
    """
    يجلب صفحة قائمة البائعين ويرجع قائمة مرتبة من الأرخص للأغلى.
    كل عنصر: {"price_val", "price_text", "seller_name", "is_prime"}
    """
    url = f"https://www.{domain}/gp/offer-listing/{asin}?condition=new&sort=price"
    offers = []
    try:
        session = _make_session(domain)
        resp = session.get(url, timeout=15, allow_redirects=True)
        if resp.status_code != 200:
            return offers

        soup = BeautifulSoup(resp.text, "lxml")
        if _is_page_blocked(soup, resp.text):
            logger.warning("CAPTCHA في offer-listing للـ ASIN %s", asin)
            return offers

        # كل صف بائع في الصفحة — الـ selector الثابت لصفحة offer-listing
        for row in soup.select("div.olpOffer"):
            # السعر — يكون داخل span.olpOfferPrice أو .a-color-price
            price_el = row.select_one(".olpOfferPrice")
            if not price_el:
                continue
            raw = price_el.get_text(strip=True)
            val = _parse_price(raw)
            if not val or not (1 < val < 500_000):
                continue

            # البائع
            seller_el = row.select_one(".olpSellerName")
            if seller_el:
                seller_name = seller_el.get_text(strip=True) or "بائع خارجي"
            else:
                # أمازون نفسه يظهر كصورة وليس نصاً
                seller_name = "Amazon.sa" if row.select_one("img[src*='amazon']") else "بائع خارجي"

            is_prime = bool(row.select_one(".a-icon-prime, [aria-label*='Prime']"))

            offers.append({
                "price_val": val,
                "price_text": raw,
                "seller_name": seller_name,
                "is_prime": is_prime,
            })

        offers.sort(key=lambda x: x["price_val"])
    except Exception as e:
        logger.error("خطأ في جلب offer-listing للـ ASIN %s: %s", asin, e)

    return offers


def resolve_short_link(url: str) -> str:
    """يحل أي رابط مختصر (amzn.to، a.co، amzn.eu) عبر متابعة إعادة التوجيه."""
    try:
        session = _make_session()
        response = session.get(url, allow_redirects=True, timeout=12)
        return response.url
    except requests.RequestException as e:
        logger.error("فشل فك تتبع الرابط المختصر: %s", e)
        return url


def extract_asin(url: str) -> str | None:
    """مستخرج ASIN يدعم كافة الأنماط: روابط طويلة، مختصرة، وصيغ تطبيق الجوال."""
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
        match = re.search(pattern, url, re.IGNORECASE)
        if match:
            return match.group(1).upper()
    return None


def extract_domain(url: str) -> str:
    """يستخرج نطاق أمازون من الرابط. يرجع الافتراضي لو ما عُرف."""
    match = re.search(r"://(?:www\.)?(amazon\.[a-z.]+)", url, re.IGNORECASE)
    if match:
        return match.group(1).lower()
    return AMAZON_DOMAIN


def build_affiliate_link(asin: str, domain: str = AMAZON_DOMAIN) -> str:
    """يبني رابط أفلييت نظيف للمنتج."""
    return f"https://www.{domain}/dp/{asin}?tag={AFFILIATE_TAG}"


def get_lowest_offer(asin: str, domain: str = AMAZON_DOMAIN) -> dict | None:
    """
    يجلب أرخص سعر متاح للمنتج مباشرة من صفحات أمازون (بدون PA API).
    الخطوات:
      1. صفحة المنتج الرئيسية  → عنوان + سعر البائع الرئيسي.
      2. صفحة قائمة البائعين   → يقارن ويختار الأرخص.
      3. يجمع النتيجة ويرجعها جاهزة.
    """
    affiliate_link = build_affiliate_link(asin, domain)

    # الخطوة 1: صفحة المنتج
    page_data = _scrape_product_page(asin, domain)
    if not page_data:
        return None

    if page_data.get("blocked"):
        return {"blocked": True, "affiliate_link": affiliate_link}

    title           = page_data.get("title")
    main_price_val  = page_data.get("price_val")
    main_price_text = page_data.get("price_text", "")
    main_seller     = page_data.get("seller_name", "Amazon.sa")
    main_is_prime   = page_data.get("is_prime", False)

    # الخطوة 2: قائمة البائعين
    offers = _scrape_offer_listing(asin, domain)

    if offers:
        cheapest = offers[0]
        if main_price_val and main_price_val <= cheapest["price_val"]:
            best_price_val  = main_price_val
            best_price_text = main_price_text
            best_seller     = main_seller
            best_is_prime   = main_is_prime
        else:
            best_price_val  = cheapest["price_val"]
            best_price_text = cheapest["price_text"]
            best_seller     = cheapest["seller_name"]
            best_is_prime   = cheapest["is_prime"]
        offer_count = len(offers)
    elif main_price_val:
        best_price_val  = main_price_val
        best_price_text = main_price_text
        best_seller     = main_seller
        best_is_prime   = main_is_prime
        offer_count     = 1
    else:
        logger.warning("ما لقينا سعراً للـ ASIN %s", asin)
        return None

    # تأكد من وجود SAR في نص السعر المعروض
    display_price = best_price_text.strip()
    if display_price and "SAR" not in display_price and "ر.س" not in display_price:
        display_price = f"{display_price} SAR"

    return {
        "asin": asin,
        "title": title,
        "price": display_price,
        "price_val": best_price_val,
        "currency": "SAR",
        "seller_name": best_seller,
        "condition": "جديد",
        "is_prime": best_is_prime,
        "offer_count": offer_count,
        "affiliate_link": affiliate_link,
    }


def format_offer_message(offer: dict) -> str:
    """يبني رسالة تيليجرام تعرض أرخص سعر متاح."""
    if not offer:
        return (
            "❌ ما قدرت ألقى عروض متاحة لهذا المنتج حاليًا.\n"
            "تأكد من توفر المنتج في المتجر أو جرّب لاحقًا."
        )

    if offer.get("blocked"):
        return (
            "⚠️ أمازون طلب تحقق من هوية الطلب (CAPTCHA).\n"
            f"شوف السعر مباشرة من هنا:\n{offer['affiliate_link']}"
        )

    title_part  = f"📦 *{offer['title'][:70]}*\n\n" if offer.get("title") else ""
    prime_badge = " 🔵 Prime" if offer.get("is_prime") else ""
    offer_count = offer.get("offer_count", 1)
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
