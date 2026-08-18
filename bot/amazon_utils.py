"""
كل الدوال المتعلقة بأمازون: استخراج ASIN، بناء رابط أفلييت، وجلب أقل سعر.
استراتيجية الأسعار:
  1. PA API الرسمي (إذا وُجدت المفاتيح) — أسرع وأدق وبدون حجب
  2. كشط مباشر (fallback) — Desktop → Mobile → offer-listing
  ملاحظة: الكشط معطّل على Railway لأن Amazon يحجبه فوراً.
"""
import json
import logging
import os
import re
import time
import threading
import requests
from bs4 import BeautifulSoup
from config import AFFILIATE_TAG, AMAZON_DOMAIN

# هل نعمل على Railway؟ — Amazon يحجب scraping من سيرفراتهم
_ON_RAILWAY = bool(os.getenv("RAILWAY_ENVIRONMENT") or os.getenv("RAILWAY_SERVICE_NAME"))

logger = logging.getLogger(__name__)

# ---- كاش الأسعار: ASIN → (timestamp, offer_dict) ----
_CACHE: dict[str, tuple[float, dict]] = {}
_CACHE_TTL  = 3 * 60 * 60  # 3 ساعات للعروض الكاملة
_CACHE_TTL_WEAK = 10 * 60  # 10 دقائق إذا الاسم أو الصورة ناقصين
_CACHE_MAX  = 500
_CACHE_LOCK = threading.Lock()

# ---- حد الطلبات المتزامنة لأمازون ----
_SCRAPE_SEMAPHORE = threading.Semaphore(10)  # رُفع من 6 → 10 لاستيعاب الضغط العالي

# ---- خريطة الأرقام العربية (13 حرف مصدر ↔ 13 هدف) ----
_AR_NUM_MAP = str.maketrans("٠١٢٣٤٥٦٧٨٩٫٬،", "0123456789.,,")

# ---- User-Agents مُدوَّرة — تتغير لكل طلب لتقليل الحجب ----
import random as _random

_UA_POOL_DESKTOP = [
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/125.0.0.0 Safari/537.36",
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36",
    "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/125.0.0.0 Safari/537.36",
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64; rv:126.0) Gecko/20100101 Firefox/126.0",
    "Mozilla/5.0 (Macintosh; Intel Mac OS X 14_4_1) AppleWebKit/605.1.15 (KHTML, like Gecko) Version/17.4.1 Safari/605.1.15",
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/123.0.0.0 Safari/537.36 Edg/123.0.0.0",
    "Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/125.0.0.0 Safari/537.36",
]

_UA_POOL_MOBILE = [
    "Mozilla/5.0 (Linux; Android 14; Pixel 8) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/125.0.0.0 Mobile Safari/537.36",
    "Mozilla/5.0 (Linux; Android 13; SM-S918B) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/124.0.0.0 Mobile Safari/537.36",
    "Mozilla/5.0 (iPhone; CPU iPhone OS 17_4_1 like Mac OS X) AppleWebKit/605.1.15 (KHTML, like Gecko) Version/17.4.1 Mobile/15E148 Safari/604.1",
    "Mozilla/5.0 (Linux; Android 14; SM-A546B) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/125.0.0.0 Mobile Safari/537.36",
    "Mozilla/5.0 (iPhone; CPU iPhone OS 17_5 like Mac OS X) AppleWebKit/605.1.15 (KHTML, like Gecko) CriOS/125.0.6422.80 Mobile/15E148 Safari/604.1",
]

_ACCEPT_LANGS = [
    "ar-SA,ar;q=0.9,en-US;q=0.8,en;q=0.7",
    "ar-SA,ar;q=0.9,en;q=0.8",
    "ar,en-US;q=0.9,en;q=0.8",
]


# =============================================================
# دوال مساعدة
# =============================================================

def _make_session(mobile: bool = False, domain: str = "amazon.sa") -> requests.Session:
    """يُنشئ جلسة HTTP بترويسات مُدوَّرة عشوائياً لتجنب الحجب."""
    ua   = _random.choice(_UA_POOL_MOBILE if mobile else _UA_POOL_DESKTOP)
    lang = _random.choice(_ACCEPT_LANGS)
    s = requests.Session()
    base = {
        "User-Agent":              ua,
        "Accept-Language":         lang,
        "Accept":                  "text/html,application/xhtml+xml,application/xml;q=0.9,image/avif,image/webp,*/*;q=0.8",
        "Accept-Encoding":         "gzip, deflate, br",
        "Connection":              "keep-alive",
        "Upgrade-Insecure-Requests": "1",
        "DNT": "1",
    }
    if not mobile:
        base.update({
            "sec-fetch-dest": "document",
            "sec-fetch-mode": "navigate",
            "sec-fetch-site": "none",
            "sec-fetch-user": "?1",
            "sec-ch-ua": '"Chromium";v="125", "Not.A/Brand";v="24"',
            "sec-ch-ua-mobile": "?0",
            "sec-ch-ua-platform": '"Windows"',
        })
    s.headers.update(base)
    s.cookies.set("i18n-prefs", "SAR",   domain=f".{domain}")
    s.cookies.set("lc-acbsa",   "ar_SA", domain=f".{domain}")
    s.cookies.set("sp-cdn",     '"L5Z9:SA"', domain=f".{domain}")
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


def _extract_product_image(soup) -> str:
    """يستخرج رابط صورة المنتج الرئيسية من صفحة أمازون.

    يجرّب عدة مصادر بالترتيب: og:image (الأكثر موثوقية) ثم عناصر الصور
    المعروفة. يرجع سلسلة فارغة إذا لم يجد صورة صالحة.
    """
    try:
        og = soup.select_one('meta[property="og:image"]')
        if og and (og.get("content") or "").startswith("http"):
            return og["content"]
        for sel in ("#landingImage", "#imgTagWrapperId img", "#main-image", "img#main-image-container img"):
            el = soup.select_one(sel)
            if not el:
                continue
            src = el.get("src") or el.get("data-old-hires") or ""
            if src.startswith("http"):
                return src
    except Exception:
        pass
    return ""


def _parse_product_details_html(html: str) -> dict:
    """يستخرج العنوان والوصف والصورة من HTML صفحة أمازون."""
    out: dict = {}
    if not html:
        return out
    try:
        soup = BeautifulSoup(html, "html.parser")

        title_el = soup.find(id="productTitle")
        if title_el:
            out["title"] = title_el.get_text(strip=True)[:200]
        if not out.get("title"):
            og = soup.select_one('meta[property="og:title"]')
            if og and og.get("content"):
                out["title"] = og["content"].strip()[:200]

        bullets: list[str] = []
        for li in soup.select("#feature-bullets li, #poExpander li"):
            t = li.get_text(" ", strip=True)
            if t and len(t) > 4 and t not in bullets:
                bullets.append(t)
        if bullets:
            out["description"] = " • ".join(bullets[:5])[:500]
        else:
            for sel in ("#productDescription p", "#aplus_feature_div p"):
                el = soup.select_one(sel)
                if el:
                    txt = el.get_text(" ", strip=True)
                    if txt and len(txt) > 15:
                        out["description"] = txt[:500]
                        break
            if not out.get("description"):
                meta = soup.select_one('meta[name="description"]')
                if meta and meta.get("content"):
                    out["description"] = meta["content"].strip()[:500]

        img = _extract_product_image(soup)
        if img:
            out["image"] = img

        price_val, price_text = _extract_price_desktop(soup)
        if not price_val:
            price_val, price_text = _extract_price_from_json(html)
        if price_val:
            out["price_val"] = price_val
            out["price"] = price_text or f"{price_val:.2f} SAR"
    except Exception as exc:
        logger.info("_parse_product_details_html: %s", exc)
    return out


def _scrape_desktop(asin: str, domain: str) -> dict | None:
    """يجلب نسخة سطح المكتب ويستخرج البيانات."""
    url = f"https://www.{domain}/dp/{asin}"
    try:
        session = _make_session(mobile=False, domain=domain)
        # زيارة الصفحة الرئيسية أولاً للحصول على كوكيز طبيعية
        session.get(f"https://www.{domain}", timeout=6, allow_redirects=True)
        time.sleep(0.5)
        resp = session.get(url, timeout=12, allow_redirects=True)

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
        details     = _parse_product_details_html(resp.text)

        return {
            "title": title or details.get("title"),
            "price_val": price_val,
            "price_text": price_text,
            "seller_name": seller_name,
            "is_prime": is_prime,
            "image": _extract_product_image(soup) or details.get("image", ""),
            "description": details.get("description", ""),
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
        resp = session.get(url, timeout=12, allow_redirects=True)

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
        details  = _parse_product_details_html(resp.text)

        return {
            "title": title or details.get("title"),
            "price_val": price_val,
            "price_text": price_text,
            "seller_name": "Amazon.sa",
            "is_prime": is_prime,
            "image": _extract_product_image(soup) or details.get("image", ""),
            "description": details.get("description", ""),
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

_SHORTENER_HOSTS = {
    "amzn.to", "amzn.eu", "a.co",
    "ty.gl", "bit.ly", "tinyurl.com", "t.co", "rb.gy",
}

# regex: يقبل أي نطاق أمازون بكل أشكاله (amazon.sa / link.amazon.com / link.amazon / ...)
_AMAZON_HOST_RE = re.compile(
    r"(?:^|\.)amazon(?:\.[a-z]{2,6}(?:\.[a-z]{2,3})?)?$"
)


def _is_allowed_host(host: str) -> bool:
    """يتحقق إذا كان النطاق مسموحاً به (أمازون أو مختصر معروف)."""
    h = host.lower().removeprefix("www.")
    return h in _SHORTENER_HOSTS or bool(_AMAZON_HOST_RE.search(h))


def resolve_short_link(url: str) -> str:
    """
    يحل أي رابط مختصر عبر متابعة إعادة التوجيه.
    مقيّد بنطاقات أمازون + روابط مختصرة معتمدة فقط (SSRF protection).
    """
    from urllib.parse import urlparse
    host = urlparse(url).netloc.lower()
    if not _is_allowed_host(host):
        logger.warning("resolve_short_link: نطاق غير مسموح به '%s' — تم الرفض", host)
        return url
    try:
        session = _make_session()
        response = session.get(url, allow_redirects=True, timeout=12)
        return response.url
    except requests.RequestException as e:
        logger.error("فشل فك تتبع الرابط: %s", e)
        return url


def _clean_url(raw: str) -> str:
    """يُزيل الترقيم الزائد من نهاية الرابط (كالأقواس والنقاط)."""
    return raw.rstrip(".,;:!?)\"']}")


def extract_asin(url: str) -> str | None:
    """يستخرج ASIN من الرابط — يدعم كل الأنماط."""
    url_match = re.search(r"https?://\S+", url)
    if url_match:
        url = _clean_url(url_match.group(0))
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


def _normalize_domain(domain: str) -> str:
    """يُعيد نطاق أمازون بدون www."""
    d = (domain or AMAZON_DOMAIN).lower().strip().removeprefix("www.")
    return d or AMAZON_DOMAIN


def build_affiliate_link(asin: str, domain: str = AMAZON_DOMAIN) -> str:
    """
    رابط عمولة Associates بنفس عناصر SiteStripe النصي:
    tag= (تاق التتبع — هذا اللي يحدد العمولة)
    linkCode=ll2 + ref_=as_li_ss_tl (نوع الرابط النصي)

    مثال أمازون:
    https://www.amazon.sa/dp/B0BRK8PR48?...&linkCode=ll2&tag=rashedalhano-21&...&ref_=as_li_ss_tl
    """
    from urllib.parse import urlencode

    asin = (asin or "").upper().strip()
    d = _normalize_domain(domain)
    qs = urlencode(
        {
            "linkCode": "ll2",
            "tag": AFFILIATE_TAG,
            "ref_": "as_li_ss_tl",
        }
    )
    url = f"https://www.{d}/dp/{asin}?{qs}"
    logger.info("🔗 AFFILIATE_LINK | ASIN=%s | tag=%s | url=%s", asin, AFFILIATE_TAG, url)
    return url


def _with_fresh_affiliate_link(offer: dict, asin: str, domain: str) -> dict:
    """يُحدّث affiliate_link دائماً — حتى عند قراءة الكاش — لضمان الصيغة الحالية."""
    if not offer or not asin:
        return offer
    result = dict(offer)
    result["affiliate_link"] = build_affiliate_link(asin, domain)
    return result


def build_affiliate_search_link(keyword: str, domain: str = AMAZON_DOMAIN) -> str:
    """رابط بحث أمازون مع تاق العمولة."""
    import urllib.parse

    d = _normalize_domain(domain)
    k = urllib.parse.quote_plus(keyword.strip())
    return (
        f"https://www.{d}/s?k={k}"
        f"&linkCode=ll2&tag={AFFILIATE_TAG}&ref_=as_li_ss_tl"
    )


def tag_amazon_url(raw_link: str, domain: str = AMAZON_DOMAIN) -> str:
    """يضيف أو يستبدل tag= على رابط أمازون موجود."""
    from urllib.parse import parse_qs, urlencode, urlparse, urlunparse

    if not raw_link:
        return build_affiliate_search_link("", domain)

    asin = extract_asin(raw_link)
    if asin:
        return build_affiliate_link(asin, extract_domain(raw_link) or domain)

    try:
        parsed = urlparse(raw_link if "://" in raw_link else f"https://{raw_link.lstrip('/')}")
        if "amazon." not in (parsed.netloc or "").lower():
            return build_affiliate_search_link("", domain)

        d = _normalize_domain(extract_domain(raw_link) or domain)
        params = parse_qs(parsed.query, keep_blank_values=True)
        for bad in ("tag", "linkCode", "ref_", "ref"):
            params.pop(bad, None)
        params["linkCode"] = ["ll2"]
        params["tag"] = [AFFILIATE_TAG]
        params["ref_"] = ["as_li_ss_tl"]
        new_query = urlencode({k: v[0] for k, v in params.items() if v})
        return urlunparse(parsed._replace(netloc=f"www.{d}", query=new_query))
    except Exception:
        return build_affiliate_search_link("", domain)


def _offer_is_weak(value: dict | None) -> bool:
    """عرض ضعيف = بدون اسم حقيقي أو بدون صورة."""
    if not value:
        return True
    title = (value.get("title") or "").strip()
    asin = (value.get("asin") or "").strip().upper()
    weak_title = (not title) or (asin and title.upper() == asin) or title.startswith("منتج ")
    weak_image = not (value.get("image") or "").startswith("http")
    return weak_title or weak_image


def _attach_cdn_image(offer: dict, asin: str, domain: str = AMAZON_DOMAIN) -> dict:
    """يضمن وجود رابط صورة CDN حتى لو المصدر الأصلي فشل."""
    if not offer:
        return offer
    if (offer.get("image") or "").startswith("http"):
        return offer
    asin = (asin or offer.get("asin") or "").upper().strip()
    if not asin:
        return offer
    offer = dict(offer)
    offer["image"] = f"https://m.media-amazon.com/images/P/{asin}.01._SCLZZZZZZZ_SX500_.jpg"
    return offer


def _cache_set(key: str, value: dict) -> None:
    """يحفظ في الكاش — يتجاهل النتائج الفاضية (بدون عنوان ولا صورة)."""
    if not value:
        return
    has_signal = bool(
        (value.get("title") or "").strip()
        or (value.get("image") or "").strip()
        or value.get("price_val")
    )
    if not has_signal and value.get("blocked"):
        return
    with _CACHE_LOCK:
        if len(_CACHE) >= _CACHE_MAX:
            to_delete = sorted(_CACHE, key=lambda k: _CACHE[k][0])[: max(1, _CACHE_MAX // 10)]
            for k in to_delete:
                del _CACHE[k]
        _CACHE[key] = (time.time(), value)


def clear_offer_cache() -> int:
    """يمسح كاش العروض — يُستدعى عند الإقلاع لمنع نتائج قديمة فاضية."""
    with _CACHE_LOCK:
        n = len(_CACHE)
        _CACHE.clear()
    return n


def get_lowest_offer(
    asin: str,
    domain: str = AMAZON_DOMAIN,
    source_url: str = "",
) -> dict | None:
    """
    يجلب أرخص سعر متاح للمنتج.
    الأولوية:
      1. SerpAPI ثم PA API ثم كشط/preview
    نتيجة ناجحة تُخزَّن في الـ cache (TTL أقصر للعروض الناقصة).
    """
    cache_key = f"{domain}:{asin}"

    # ── تحقق من الـ cache ─────────────────────────────────────────────────────
    with _CACHE_LOCK:
        if cache_key in _CACHE:
            ts, cached = _CACHE[cache_key]
            ttl = _CACHE_TTL_WEAK if _offer_is_weak(cached) else _CACHE_TTL
            if time.time() - ts < ttl:
                # لا نعيد عروض فاضية من الكاش — نجبر إعادة الجلب
                if _offer_is_weak(cached) and not (cached.get("title") or cached.get("image")):
                    logger.info("Cache miss قسري (عرض فاضي) للـ ASIN %s", asin)
                else:
                    logger.info("Cache hit للـ ASIN %s", asin)
                    return _attach_cdn_image(
                        _with_fresh_affiliate_link(cached, asin, domain), asin, domain
                    )
            else:
                _CACHE.pop(cache_key, None)

    # ── دالة مساعدة: تسجيل + cache + إعادة ─────────────────────────────────
    def _record_and_return(res: dict) -> dict:
        res = dict(res or {})
        res.setdefault("asin", asin)
        res = _attach_cdn_image(res, asin, domain)
        # لا نخزّن عروض بلا اسم نهائياً إن وُجد عنوان في الرابط
        if not (res.get("title") or "").strip() and source_url:
            t = extract_product_title(source_url, asin)
            if t:
                res["title"] = t
        _cache_set(cache_key, res)
        try:
            from price_history import record_price
            if res.get("price_val"):
                record_price(asin, domain, res["price_val"], res.get("seller_name", ""))
        except Exception as _ph:
            logger.warning("price_history: فشل التسجيل — %s", _ph)
        return res

    # ── SerpAPI (الأولوية الأولى) — يدعم أي domain ──────────────────────────────
    try:
        from serpapi_utils import get_item_by_asin as serp_get, serpapi_available
        if serpapi_available():
            logger.info("SerpAPI: أطلب ASIN %s", asin)
            result = serp_get(asin, domain=domain)
            if result:
                return _record_and_return(result)
            logger.info("SerpAPI: لا نتيجة، أنتقل للخطوة التالية — ASIN %s", asin)
    except Exception as e:
        logger.warning("SerpAPI exception: %s — أنتقل للخطوة التالية", e)

    # ── PA API (الأولوية الثانية) — فقط لأمازون السعودية ────────────────────
    if domain == "amazon.sa":
        try:
            from paapi_utils import get_item_by_asin as pa_get, paapi_available
            if paapi_available():
                logger.info("PA API: أطلب ASIN %s", asin)
                result = pa_get(asin)
                if result:
                    return _record_and_return(result)
                logger.info("PA API: لا نتيجة، أنتقل للكشط — ASIN %s", asin)
        except Exception as e:
            logger.warning("PA API exception: %s — أنتقل للكشط", e)

    affiliate_link = build_affiliate_link(asin, domain)

    # ── على Railway: كشط خفيف ثم preview ───────────────────────────────────
    if _ON_RAILWAY:
        logger.info("Railway — محاولة جلب بيانات ASIN %s", asin)
        page_data = _scrape_mobile(asin, domain)
        if page_data and not page_data.get("blocked"):
            price_val = page_data.get("price_val")
            affiliate_link = build_affiliate_link(asin, domain)
            display_price = (page_data.get("price_text") or "").strip()
            if price_val and display_price and "SAR" not in display_price:
                display_price = f"{display_price} SAR"
            result = {
                "asin": asin,
                "title": page_data.get("title"),
                "description": page_data.get("description", ""),
                "price": display_price or (f"{price_val:.2f} SAR" if price_val else ""),
                "price_val": price_val,
                "currency": "SAR",
                "seller_name": page_data.get("seller_name", "Amazon.sa"),
                "is_prime": page_data.get("is_prime", False),
                "offer_count": 1,
                "affiliate_link": affiliate_link,
                "image": page_data.get("image", ""),
            }
            if price_val:
                return _record_and_return(result)
            if result.get("title") or result.get("description"):
                result["blocked"] = True
                return result
        return _railway_product_preview(asin, domain, source_url=source_url)

    # ── كشط مباشر (fallback) — semaphore يحد الطلبات المتزامنة ──────────────
    if not _SCRAPE_SEMAPHORE.acquire(timeout=45):
        logger.warning("SCRAPE_SEMAPHORE timeout للـ ASIN %s — جاري محاولة stale cache", asin)
        with _CACHE_LOCK:
            if cache_key in _CACHE:
                stale_ts, stale_data = _CACHE[cache_key]
                result = _with_fresh_affiliate_link(stale_data, asin, domain)
                result["stale"] = True
                result["stale_age_min"] = max(1, int((time.time() - stale_ts) / 60))
                return result
        return {"blocked": True, "affiliate_link": build_affiliate_link(asin, domain)}
    try:

        # --- المحاولة الأولى: الجوال (أقل حجباً من Desktop) ---
        logger.info("كشط موبايل للـ ASIN %s", asin)
        page_data = _scrape_mobile(asin, domain)

        # --- المحاولة الثانية: سطح المكتب إذا فشل الجوال ---
        if not page_data or page_data.get("blocked"):
            logger.info("تجربة Desktop للـ ASIN %s", asin)
            time.sleep(_random.uniform(1.0, 2.5))
            page_data = _scrape_desktop(asin, domain)

        # --- المحاولة الثالثة: offer-listing مباشرة ---
        if not page_data or page_data.get("blocked"):
            logger.info("تجربة offer-listing مباشرة للـ ASIN %s", asin)
            time.sleep(_random.uniform(1.5, 3.0))
            offers_direct = _scrape_offer_listing(asin, domain)
            if offers_direct:
                best_direct = offers_direct[0]
                page_data = {
                    "title":       None,
                    "price_val":   best_direct["price_val"],
                    "price_text":  best_direct["price_text"],
                    "seller_name": best_direct["seller_name"],
                    "is_prime":    best_direct["is_prime"],
                }

        # --- كلاهما محجوب أو فشل الاتصال ---
        if not page_data or page_data.get("blocked"):
            logger.warning("كلا الوضعين محجوبان للـ ASIN %s — محاولة stale cache", asin)
            # Stale cache: أعد آخر نتيجة محفوظة حتى لو انتهت صلاحيتها
            with _CACHE_LOCK:
                if cache_key in _CACHE:
                    stale_ts, stale_data = _CACHE[cache_key]
                    age_min = max(1, int((time.time() - stale_ts) / 60))
                    result  = _with_fresh_affiliate_link(stale_data, asin, domain)
                    result["stale"]         = True
                    result["stale_age_min"] = age_min
                    logger.info("Stale cache للـ ASIN %s (عمر %d دقيقة)", asin, age_min)
                    return result
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
            "image":       page_data.get("image", ""),
            "description": page_data.get("description", ""),
        }

        return _record_and_return(result)
    finally:
        _SCRAPE_SEMAPHORE.release()


def _esc(text: str) -> str:
    """يهرّب أحرف Markdown v1 الخاصة في النص الديناميكي."""
    for ch in r"_*`[":
        text = text.replace(ch, f"\\{ch}")
    return text


# عبارات إغراء مُدوَّرة — تمنع تكرار نفس الرسالة عند إرسال كمية كبيرة من الروابط
_OFFER_TEASERS_NAMED = [
    "🔥 *{name}* — لقيت لك أقوى سعر على أمازون السعودية!",
    "⚡ *{name}* متوفر الحين بعرض يستاهل الطلب!",
    "💥 فرصة على *{name}* — السعر ممتاز والكمية محدودة!",
    "🎯 *{name}* بأفضل عرض لقيته لك — لا تفوّتها!",
    "🛍️ *{name}* بسعر مغري — اطلبه قبل ما يرتفع!",
    "✨ صفقة اليوم: *{name}* — جاهز للشراء من أمازون!",
    "🏆 *{name}* — وفّرت لك وقت البحث وجبت لك الأرخص!",
]

_OFFER_TEASERS = [
    "🔥 *لقيت لك صفقة قوية على هذا المنتج!*",
    "⚡ *عرض ممتاز متوفر الحين — يستاهل الطلب!*",
    "💥 *السعر حلو — والكمية ما تدوم طويل!*",
    "🎯 *أفضل سعر لقيته لك على أمازون السعودية!*",
    "🛍️ *منتج مطلوب وبعرض يستاهل — لا تتردد!*",
]

_OFFER_CTA = [
    "👇 اضغط *اشتري من أمازون* وكمّل طلبك بثواني",
    "👇 افتح الرابط تحت واطلب مباشرة — سريع وآمن",
    "👇 السعر يتغيّر بسرعة — اطلبه الحين قبل ما يرتفع",
    "👇 شوف التفاصيل واطلب من أمازون بضغطة واحدة",
]

_ALERT_HINTS = [
    "🔔 *نصيحة:* اضغط زر *نبّهني* تحت — وأرسلك إشعار فور نزول السعر!",
    "🔔 ما تبي تفوّت الخصم؟ فعّل *نبّهني* وأخبرك أول ما ينزل السعر 👇",
    "🔔 فعّل تنبيه السعر بزر *نبّهني* — أراقبه لك كل يوم وأبلغك فور الانخفاض!",
]

_BLOCKED_CTA = [
    "👇 افتح الرابط وشوف السعر والعروض مباشرة من أمازون",
    "👇 اضغط تحت وتصفّح المنتج — السعر قدامك على أمازون",
]


def build_product_image_url(asin: str, domain: str = AMAZON_DOMAIN, offer: dict | None = None) -> str:
    """أفضل رابط صورة متاح — من العرض أو ويدجت أمازون كبديل."""
    if offer:
        img = (offer.get("image") or "").strip()
        if img.startswith("http"):
            return img
    mp = domain if "amazon." in domain else AMAZON_DOMAIN
    return (
        f"https://ws-eu.amazon-adsystem.com/widgets/q?"
        f"ServiceVersion=20070822&MarketPlace=www.{mp}&ASIN={asin}"
        f"&Format=_SL500_&ID=AsinImage&tag={AFFILIATE_TAG}"
    )


def extract_product_title(url: str, asin: str = "") -> str:
    """يستخرج اسم المنتج من slug الرابط — مثل /اسم-المنتج/dp/ASIN."""
    if not url:
        return ""
    from urllib.parse import unquote
    asin = (asin or extract_asin(url) or "").upper()
    patterns = [
        rf"/([^/?#]+)/dp/{re.escape(asin)}" if asin else r"/([^/?#]+)/dp/[A-Z0-9]{10}",
        rf"/([^/?#]+)/gp/product/{re.escape(asin)}" if asin else r"/([^/?#]+)/gp/product/[A-Z0-9]{10}",
    ]
    for pat in patterns:
        m = re.search(pat, url, re.IGNORECASE)
        if not m:
            continue
        slug = unquote(m.group(1).strip())
        if slug.lower() in ("dp", "gp", "product", "www.amazon.sa", "www.amazon.com"):
            continue
        if re.fullmatch(r"[A-Z0-9]{10}", slug.upper()):
            continue
        title = slug.replace("-", " ").replace("_", " ").strip()
        title = re.sub(r"\s+", " ", title)
        if len(title) >= 3:
            return title[:120]
    return ""


def _fetch_og_meta(asin: str, domain: str, source_url: str = "") -> dict:
    """يجلب og:title و og:image من صفحة المنتج — يعمل حتى على Railway أحياناً."""
    mp = _normalize_domain(domain)
    page_urls = []
    if source_url and "amazon." in source_url:
        page_urls.append(source_url.split("?")[0].rstrip("/") )
    page_urls.append(f"https://www.{mp}/dp/{asin}")
    page_urls.append(f"https://www.{mp}/gp/aw/d/{asin}")  # نسخة الجوال

    out: dict = {}
    headers = {
        "User-Agent": (
            "Mozilla/5.0 (iPhone; CPU iPhone OS 17_0 like Mac OS X) "
            "AppleWebKit/605.1.15 (KHTML, like Gecko) Version/17.0 "
            "Mobile/15E148 Safari/604.1"
        ),
        "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
        "Accept-Language": "ar-SA,ar;q=0.9,en;q=0.8",
    }
    for page_url in page_urls:
        try:
            resp = requests.get(page_url, timeout=8, allow_redirects=True, headers=headers)
            if resp.status_code != 200:
                continue
            html = resp.text or ""
            if "captcha" in html.lower()[:5000] or "robot check" in html.lower()[:5000]:
                continue
            details = _parse_product_details_html(html)
            if details.get("title"):
                out["title"] = details["title"]
            if details.get("image"):
                out["image"] = details["image"]
            if details.get("description") and not out.get("description"):
                out["description"] = details["description"]
            if details.get("price_val") and not out.get("price_val"):
                out["price_val"] = details["price_val"]
                out["price"] = details.get("price")
            # og fallbacks مباشرة
            if not out.get("title"):
                m = re.search(
                    r'<meta[^>]+property=["\']og:title["\'][^>]+content=["\']([^"\']+)',
                    html, re.I,
                ) or re.search(
                    r'<meta[^>]+content=["\']([^"\']+)["\'][^>]+property=["\']og:title',
                    html, re.I,
                )
                if m:
                    out["title"] = m.group(1).strip()[:200]
            if not out.get("title"):
                # وسم <title> في الصفحة
                m = re.search(r"<title[^>]*>([^<]+)</title>", html, re.I)
                if m:
                    raw = re.sub(r"\s+", " ", m.group(1)).strip()
                    raw = re.split(r"\s+[-:|]\s+Amazon", raw, maxsplit=1, flags=re.I)[0].strip()
                    if raw and "captcha" not in raw.lower() and len(raw) > 4:
                        out["title"] = raw[:200]
            if not out.get("image"):
                m = re.search(
                    r'<meta[^>]+property=["\']og:image["\'][^>]+content=["\']([^"\']+)',
                    html, re.I,
                ) or re.search(
                    r'<meta[^>]+content=["\']([^"\']+)["\'][^>]+property=["\']og:image',
                    html, re.I,
                )
                if m:
                    out["image"] = m.group(1).strip()
            if out.get("title") or out.get("image"):
                logger.info("OG meta OK للـ ASIN %s من %s", asin, page_url[:60])
                return out
        except Exception as exc:
            logger.info("OG meta فشل %s: %s", page_url[:50], exc)
    return out


def _fetch_associates_widget_meta(asin: str, domain: str = AMAZON_DOMAIN) -> dict:
    """يجلب عنوان وصورة من ويدجت Associates (غالباً يشتغل من السيرفرات)."""
    mp = _normalize_domain(domain)
    # رمز السوق لـ SA وغيرها
    market = "SA" if mp.endswith(".sa") else ("AE" if mp.endswith(".ae") else "US")
    url = (
        f"https://ws-eu.amazon-adsystem.com/widgets/q?"
        f"ServiceVersion=20070822&OneJS=1&Operation=GetAdHtml"
        f"&MarketPlace={market}&source=ss&ref=as_ss_li_til"
        f"&ad_type=product_link&tracking_id={AFFILIATE_TAG}"
        f"&marketplace=amazon&region={market}&placement={asin}"
        f"&asins={asin}&linkId=&show_border=false&link_opens_in_new_window=true"
    )
    out: dict = {}
    try:
        resp = requests.get(
            url,
            timeout=8,
            headers={
                "User-Agent": (
                    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
                    "AppleWebKit/537.36 (KHTML, like Gecko) "
                    "Chrome/120.0.0.0 Safari/537.36"
                ),
                "Accept": "text/html,*/*",
            },
        )
        if resp.status_code != 200 or not resp.text:
            return out
        html = resp.text
        # العنوان — عدة أنماط شائعة في ويدجت Associates
        for pat in (
            r'class="[^"]*\btitle\b[^"]*"[^>]*>([^<]+)',
            r'<a[^>]+class="[^"]*\btitle\b[^"]*"[^>]*>([^<]+)',
            r'<span[^>]+class="[^"]*\btitle\b[^"]*"[^>]*>([^<]+)',
            r'title=["\']([^"\']{8,})["\']',
            r'<a[^>]+href="[^"]*/dp/[A-Z0-9]{10}[^"]*"[^>]*>([^<]{8,})</a>',
        ):
            m = re.search(pat, html, re.I)
            if not m:
                continue
            title = re.sub(r"\s+", " ", m.group(1)).strip()
            title = _clean_product_title(title, asin)
            if title:
                out["title"] = title
                break
        # الصورة
        for pat in (
            r'<img[^>]+src=["\'](https?://[^"\']+)["\']',
            r'src=["\'](https?://[^"\']*(?:media-amazon|ssl-images-amazon)[^"\']+)["\']',
        ):
            for im in re.finditer(pat, html, re.I):
                img = im.group(1)
                if "pixel" in img.lower() or "spacer" in img.lower() or "transparent" in img.lower():
                    continue
                if "amazon" in img.lower() or "media" in img.lower():
                    out["image"] = img
                    break
            if out.get("image"):
                break
        if out:
            logger.info("Widget meta OK للـ ASIN %s title=%s", asin, (out.get("title") or "")[:40])
    except Exception as exc:
        logger.info("Widget meta فشل: %s", exc)
    return out


def _clean_product_title(title: str | None, asin: str = "") -> str:
    """ينظّف عنوان المنتج ويرفض الأكواد/العناوين الفارغة."""
    if not title:
        return ""
    t = re.sub(r"\s+", " ", str(title)).strip()
    # أزل بادئات أمازون الشائعة
    t = re.sub(
        r"^(amazon(?:\.[a-z.]+)?|أمازون)\s*[:|\-–]\s*",
        "",
        t,
        flags=re.IGNORECASE,
    ).strip()
    t = re.sub(r"\s*[:|\-–]\s*amazon(?:\.[a-z.]+)?\s*$", "", t, flags=re.IGNORECASE).strip()
    asin_u = (asin or "").upper().strip()
    if not t:
        return ""
    # ارفض لو العنوان مجرد كود ASIN أو "منتج XXX"
    if asin_u and t.upper() == asin_u:
        return ""
    if re.fullmatch(r"[A-Z0-9]{10}", t.upper()):
        return ""
    if re.fullmatch(rf"منتج\s*{re.escape(asin_u)}", t, flags=re.IGNORECASE):
        return ""
    if t.lower() in ("amazon", "amazon.sa", "amazon.ae", "amazon.com", "أمازون"):
        return ""
    if len(t) < 3:
        return ""
    return t[:200]


def _is_weak_title(title: str | None, asin: str = "") -> bool:
    return not _clean_product_title(title, asin)


def enrich_offer_display(
    offer: dict | None,
    asin: str,
    domain: str = AMAZON_DOMAIN,
    source_url: str = "",
) -> dict:
    """
    يضمن اسم حقيقي + صورة للمنتج.
    لا يُرجع كود ASIN كاسم منتج.
    """
    asin = (asin or "").upper().strip()
    offer = dict(offer or {})
    offer.setdefault("asin", asin)
    offer.setdefault("affiliate_link", build_affiliate_link(asin, domain))

    # نظّف أي عنوان موجود مسبقاً (قد يكون كود)
    cleaned = _clean_product_title(offer.get("title"), asin)
    if cleaned:
        offer["title"] = cleaned
    else:
        offer.pop("title", None)

    def _need_title() -> bool:
        return _is_weak_title(offer.get("title"), asin)

    def _need_image() -> bool:
        return not (offer.get("image") or "").startswith("http")

    def _apply_title(raw: str | None) -> bool:
        t = _clean_product_title(raw, asin)
        if t:
            offer["title"] = t
            return True
        return False

    # 1) SerpAPI — المنتج ثم البحث (خذ أول نتيجة عند البحث بالـ ASIN)
    if _need_title() or _need_image():
        try:
            from serpapi_utils import get_item_by_asin as serp_get, search_items, serpapi_available
            if serpapi_available():
                for try_domain in (domain, "amazon.com" if domain != "amazon.com" else None):
                    if not try_domain:
                        continue
                    serp = serp_get(asin, domain=try_domain)
                    if not serp:
                        continue
                    if _need_title():
                        _apply_title(serp.get("title"))
                    if _need_image() and (serp.get("image") or "").startswith("http"):
                        offer["image"] = serp["image"]
                    if not offer.get("seller_name") and serp.get("seller_name"):
                        offer["seller_name"] = serp["seller_name"]
                    if offer.get("price_val") is None and serp.get("price_val"):
                        offer["price_val"] = serp["price_val"]
                        offer["price"] = serp.get("price")
                        offer.pop("blocked", None)
                    if not _need_title() and not _need_image():
                        break

                if _need_title() or _need_image():
                    results = search_items(asin, domain=domain, max_results=8) or []
                    exact = None
                    first = None
                    for item in results:
                        if first is None:
                            first = item
                        item_asin = (item.get("asin") or "").upper()
                        link = (item.get("link") or "").upper()
                        if item_asin == asin or asin in link:
                            exact = item
                            break
                    chosen = exact or first  # البحث بكود ASIN → أول نتيجة غالباً هي المنتج
                    if chosen:
                        if _need_title():
                            _apply_title(chosen.get("title"))
                        if _need_image() and (chosen.get("image") or "").startswith("http"):
                            offer["image"] = chosen["image"]
                        if not offer.get("seller_name") and chosen.get("seller_name"):
                            offer["seller_name"] = chosen["seller_name"]

                # Google عبر SerpAPI — مصدر قوي لاسم المنتج
                if _need_title() or _need_image():
                    from serpapi_utils import fetch_title_via_google
                    g_title, g_image = fetch_title_via_google(asin, domain)
                    if _need_title():
                        _apply_title(g_title)
                    if _need_image() and g_image.startswith("http"):
                        offer["image"] = g_image
        except Exception as exc:
            logger.warning("enrich SerpAPI: %s", exc)

    # 2) PA API
    if _need_title() or _need_image():
        try:
            from paapi_utils import get_item_by_asin as pa_get, paapi_available
            if paapi_available():
                pa = pa_get(asin)
                if pa:
                    if _need_title():
                        _apply_title(pa.get("title"))
                    if _need_image() and (pa.get("image") or "").startswith("http"):
                        offer["image"] = pa["image"]
                    if not offer.get("seller_name") and pa.get("seller_name"):
                        offer["seller_name"] = pa["seller_name"]
        except Exception as exc:
            logger.warning("enrich PA API: %s", exc)

    # 3) ويدجت Associates
    if _need_title() or _need_image():
        w = _fetch_associates_widget_meta(asin, domain)
        if _need_title():
            _apply_title(w.get("title"))
        if _need_image() and w.get("image"):
            offer["image"] = w["image"]

    # 4) OG من صفحة المنتج
    if _need_title() or _need_image():
        og = _fetch_og_meta(asin, domain, source_url)
        if _need_title():
            _apply_title(og.get("title"))
        if _need_image() and og.get("image"):
            offer["image"] = og["image"]
        if offer.get("price_val") is None and og.get("price_val"):
            offer["price_val"] = og["price_val"]
            offer["price"] = og.get("price")
            offer.pop("blocked", None)

    # 5) اسم من slug الرابط (بعد التنظيف)
    if _need_title():
        _apply_title(extract_product_title(source_url, asin))

    # 6) لا تعرض كود ASIN للجمهور — اسم عام أفضل من كود
    if _need_title():
        offer["title"] = "منتج من أمازون"
        logger.warning("TITLE_FALLBACK عام للـ ASIN %s (ما قدرنا نجيب الاسم الحقيقي)", asin)

    if _need_image():
        offer["image"] = (
            f"https://m.media-amazon.com/images/P/{asin}.01._SCLZZZZZZZ_SX500_.jpg"
        )

    # تأكيد أخير: نظّف العنوان قبل الإرجاع — لا كود ASIN أبداً
    final = _clean_product_title(offer.get("title"), asin)
    offer["title"] = final or "منتج من أمازون"
    logger.info(
        "TITLE_FINAL ASIN=%s title=%s serpapi_needed_was_checked",
        asin,
        offer["title"][:80],
    )
    return offer


def _image_candidate_urls(asin: str, domain: str, offer: dict | None, source_url: str = "") -> list[str]:
    """قائمة روابط صور نجرّبها بالترتيب — السريع أولاً."""
    urls: list[str] = []
    seen: set[str] = set()

    def _add(u: str) -> None:
        u = (u or "").strip()
        if u.startswith("//"):
            u = "https:" + u
        if u.startswith("http") and u not in seen:
            seen.add(u)
            urls.append(u)

    if offer:
        _add(offer.get("image") or "")

    asin = (asin or "").upper().strip()
    mp = _normalize_domain(domain)

    if asin:
        for u in (
            f"https://m.media-amazon.com/images/P/{asin}.01._SCLZZZZZZZ_SX500_.jpg",
            f"https://images-eu.ssl-images-amazon.com/images/P/{asin}.01.LZZZZZZZ.jpg",
            f"https://images-na.ssl-images-amazon.com/images/P/{asin}.01.LZZZZZZZ.jpg",
            f"https://images-eu.ssl-images-amazon.com/images/P/{asin}.01._SCLZZZZZZZ_.jpg",
        ):
            _add(u)

    for host in ("ws-eu", "ws-na"):
        _add(
            f"https://{host}.amazon-adsystem.com/widgets/q?"
            f"ServiceVersion=20070822&MarketPlace=www.{mp}&ASIN={asin}"
            f"&Format=_SL500_&ID=AsinImage&tag={AFFILIATE_TAG}"
        )

    return urls


def list_product_image_urls(
    asin: str,
    domain: str = AMAZON_DOMAIN,
    offer: dict | None = None,
    source_url: str = "",
) -> list[str]:
    """روابط صور المنتج بالترتيب — للإرسال المباشر في تيليجرام."""
    return _image_candidate_urls(asin, domain, offer, source_url)


def fetch_product_image_bytes(
    asin: str,
    domain: str = AMAZON_DOMAIN,
    offer: dict | None = None,
    source_url: str = "",
) -> bytes | None:
    """يجرب عدة مصادر ويرجع بايتات الصورة."""
    for url in _image_candidate_urls(asin, domain, offer, source_url):
        data = download_image_bytes(url, timeout=10)
        if data and len(data) >= 400:
            logger.info("صورة المنتج OK من: %s", url[:90])
            return data
    return None


def download_image_bytes(url: str, timeout: float = 15.0) -> bytes | None:
    """يحمّل بايتات الصورة — متساهل مع content-type."""
    if not url or not url.startswith("http"):
        return None
    if url.startswith("//"):
        url = "https:" + url
    try:
        headers = {
            "User-Agent": (
                "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
                "AppleWebKit/537.36 (KHTML, like Gecko) "
                "Chrome/120.0.0.0 Safari/537.36"
            ),
            "Accept": "image/avif,image/webp,image/apng,image/*,*/*;q=0.8",
            "Accept-Language": "ar,en;q=0.9",
        }
        if "amazon" in url.lower() or "ssl-images-amazon" in url.lower() or "media-amazon" in url.lower():
            headers["Referer"] = f"https://www.{AMAZON_DOMAIN}/"
            headers["Origin"] = f"https://www.{AMAZON_DOMAIN}"
        resp = requests.get(
            url,
            timeout=timeout,
            allow_redirects=True,
            headers=headers,
        )
        if resp.status_code != 200 or not resp.content or len(resp.content) < 300:
            return None
        ctype = (resp.headers.get("content-type") or "").lower()
        if ctype.startswith("text/") or "html" in ctype:
            return None
        if ctype and "image" not in ctype and "octet-stream" not in ctype:
            head = resp.content[:16]
            if not (
                head[:3] == b"GIF"
                or head[:8] == b"\x89PNG\r\n\x1a\n"
                or head[:2] == b"\xff\xd8"
                or (head[:4] == b"RIFF" and b"WEBP" in resp.content[:16])
            ):
                return None
        return resp.content
    except Exception as exc:
        logger.warning("download_image_bytes فشل: %s", exc)
        return None


def _railway_product_preview(asin: str, domain: str = AMAZON_DOMAIN, source_url: str = "") -> dict:
    """على Railway: جلب صورة/عنوان خفيف قبل إرجاع رابط الأفلييت فقط."""
    affiliate_link = build_affiliate_link(asin, domain)
    title = extract_product_title(source_url, asin)
    result: dict = {
        "blocked": True,
        "affiliate_link": affiliate_link,
        "asin": asin,
    }
    if title:
        result["title"] = title

    img_bytes = fetch_product_image_bytes(asin, domain, None, source_url)
    candidates = _image_candidate_urls(asin, domain, None, source_url)
    if img_bytes and candidates:
        result["image"] = candidates[0]

    mp = _normalize_domain(domain)
    try:
        page_url = source_url if source_url and "amazon." in source_url else f"https://www.{mp}/dp/{asin}"
        resp = requests.get(
            page_url,
            timeout=12,
            allow_redirects=True,
            headers={
                "User-Agent": (
                    "Mozilla/5.0 (iPhone; CPU iPhone OS 17_0 like Mac OS X) "
                    "AppleWebKit/605.1.15 (KHTML, like Gecko) Version/17.0 "
                    "Mobile/15E148 Safari/604.1"
                ),
                "Accept-Language": "ar,en;q=0.9",
            },
        )
        if resp.status_code == 200 and "captcha" not in resp.text.lower()[:8000]:
            details = _parse_product_details_html(resp.text)
            if not result.get("title") and details.get("title"):
                result["title"] = details["title"]
            if details.get("description"):
                result["description"] = details["description"]
            if not result.get("image") and details.get("image"):
                result["image"] = details["image"]
            if details.get("price_val"):
                result["price_val"] = details["price_val"]
                result["price"] = details.get("price") or f"{details['price_val']:.2f} SAR"
                result["blocked"] = False
    except Exception as exc:
        logger.info("railway preview: %s", exc)

    return result


def format_product_reply_plain(
    offer: dict | None,
    *,
    fallback_title: str = "",
    asin: str = "",
    version: str = "",
) -> str:
    """رسالة قصيرة تحت صورة المنتج — بدون سعر ولا وصف طويل."""
    _ = version
    if not offer:
        return "❌ ما لقيت المنتج — جرّب رابط ثاني."

    title = (offer.get("title") or fallback_title or "").strip()
    title = _clean_product_title(title, asin) or _clean_product_title(fallback_title, asin)
    if not title:
        title = "منتج من أمازون"
    if len(title) > 90:
        title = title[:87] + "…"

    cta_lines = [
        "✨ لقيته لك بأقل سعر — اضغط «اشتري الآن» 👇",
        "🏷️ أرخص عرض من الرابط — اضغط الزر تحت 👇",
        "🔥 جاهز بأقل سعر — اضغط «اشتري الآن» وشوف التفاصيل 👇",
    ]
    cta = _random.choice(cta_lines)
    if offer.get("blocked"):
        cta = "🔗 المنتج جاهز — اضغط «اشتري الآن» وشوف السعر 👇"

    seller = (offer.get("seller_name") or "").strip()
    seller_line = f"🏪 البائع: {seller[:40]}\n" if seller else ""

    return (
        f"📦 {title}\n"
        f"{seller_line}\n"
        f"{cta}\n"
        f"🔔 انخفض السعر؟ اضغط «نبّهني عند انخفاض السعر»"
    )


def format_offer_message(offer: dict | None, *, include_alert_hint: bool = True, fallback_title: str = "") -> str:
    """واجهة متوافقة — تُعيد نصاً عادياً بدون Markdown."""
    _ = include_alert_hint
    return format_product_reply_plain(offer, fallback_title=fallback_title)
