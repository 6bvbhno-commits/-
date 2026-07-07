"""
دوال التعرف على المنتج من صورة، والبحث عنه داخل أمازون.
يستخدم Gemini 1.5 Flash (مجاني) للتعرف على المنتج،
ثم يقشط نتائج البحث المباشر من أمازون السعودية.
"""
import base64
import logging
import re
import requests
from config import GEMINI_API_KEY, AFFILIATE_TAG, AMAZON_DOMAIN

logger = logging.getLogger(__name__)

_HEADERS = {
    "User-Agent": (
        "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
        "AppleWebKit/537.36 (KHTML, like Gecko) "
        "Chrome/120.0.0.0 Safari/537.36"
    ),
    "Accept-Language": "ar,en-US;q=0.9,en;q=0.8",
}


def _call_gemini(image_bytes: bytes) -> str | None:
    """
    استدعاء متزامن لـ Gemini 1.5 Flash — يُنفَّذ عبر run_in_executor.
    يرجع اسم المنتج للبحث، أو None إذا فشل.
    """
    if not GEMINI_API_KEY:
        return None

    url = (
        "https://generativelanguage.googleapis.com/v1/models/"
        f"gemini-2.5-flash-lite:generateContent?key={GEMINI_API_KEY}"
    )
    image_b64 = base64.b64encode(image_bytes).decode("utf-8")
    payload = {
        "contents": [
            {
                "parts": [
                    {
                        "text": (
                            "ما هو هذا المنتج بدقة؟ "
                            "أعطني فقط اسم المنتج والموديل باللغة الإنجليزية أو العربية "
                            "لكي أبحث عنه في موقع أمازون. "
                            "لا تكتب أي جمل أخرى، فقط اسم المنتج للبحث المباشر."
                        )
                    },
                    {
                        "inlineData": {
                            "mimeType": "image/jpeg",
                            "data": image_b64,
                        }
                    },
                ]
            }
        ]
    }

    import time
    for attempt in range(3):
        try:
            response = requests.post(url, json=payload, timeout=20)
            if response.status_code == 200:
                result = response.json()
                text = (
                    result.get("candidates", [{}])[0]
                    .get("content", {})
                    .get("parts", [{}])[0]
                    .get("text", "")
                    .strip()
                )
                return text or None
            elif response.status_code == 429:
                wait = 5 * (attempt + 1)
                logger.warning("Gemini rate limit (429)، انتظار %ss ثم إعادة المحاولة %s/3", wait, attempt + 1)
                time.sleep(wait)
                continue
            else:
                logger.error("Gemini API status %s: %s", response.status_code, response.text[:200])
                return None
        except Exception as e:
            logger.error("خطأ في Gemini: %s", e)
            return None
    logger.error("Gemini: فشل بعد 3 محاولات بسبب rate limit")
    return None


def _scrape_amazon_search(query: str, domain: str = AMAZON_DOMAIN) -> list[dict]:
    """
    يقشط أول 3 نتائج من صفحة البحث في أمازون.
    يستخدم session مع كوكيز وزيارة الصفحة الرئيسية أولاً لتجاوز الحجب.
    """
    import time
    from bs4 import BeautifulSoup

    results = []
    try:
        session = requests.Session()
        session.headers.update({
            "User-Agent": (
                "Mozilla/5.0 (Linux; Android 13; Pixel 7) "
                "AppleWebKit/537.36 (KHTML, like Gecko) "
                "Chrome/124.0.0.0 Mobile Safari/537.36"
            ),
            "Accept-Language": "ar-SA,ar;q=0.9,en-US;q=0.8",
            "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
        })
        session.cookies.set("i18n-prefs", "SAR", domain=f".{domain}")
        session.cookies.set("lc-acbsa", "ar_SA", domain=f".{domain}")

        # زيارة الصفحة الرئيسية أولاً للحصول على كوكيز طبيعية
        session.get(f"https://www.{domain}", timeout=10, allow_redirects=True)
        time.sleep(0.8)

        search_url = f"https://www.{domain}/s?k={requests.utils.quote(query)}"
        response = session.get(search_url, timeout=15)

        if response.status_code != 200:
            logger.warning("Search status %s للاستعلام: %s", response.status_code, query)
            return results

        html = response.text
        if any(kw in html.lower() for kw in ("captcha", "robot check", "automated access", "captchacharacters")):
            logger.warning("صفحة البحث محجوبة (CAPTCHA) للاستعلام: %s", query)
            return results

        soup = BeautifulSoup(html, "html.parser")
        items = soup.select('[data-component-type="s-search-result"]')[:3]

        for item in items:
            # العنوان: h2 span أكثر موثوقية من h2 a span
            title_el = item.select_one("h2 span") or item.select_one("h2 a span")
            link_el  = item.select_one("h2 a")
            price_el = item.select_one("span.a-price-whole")

            if not title_el:
                continue

            title = title_el.get_text(strip=True)
            if not title:
                continue

            # استخراج ASIN من href أو data-asin
            asin = item.get("data-asin", "")
            if not asin and link_el:
                raw_href = link_el.get("href", "")
                m = re.search(r"/dp/([A-Z0-9]{10})", raw_href, re.IGNORECASE)
                if m:
                    asin = m.group(1).upper()

            if asin:
                affiliate_link = f"https://www.{domain}/dp/{asin}?tag={AFFILIATE_TAG}"
            elif link_el:
                affiliate_link = f"https://www.{domain}" + link_el.get("href", "").split("?")[0]
            else:
                continue

            price_text = price_el.get_text(strip=True) if price_el else ""
            # تنظيف السعر من الأحرف الزائدة وإضافة الوحدة
            price_clean = re.sub(r"[^\d.,٠-٩]", "", price_text)
            price = f"{price_clean} ريال" if price_clean else "غير محدد"

            results.append({
                "title": title[:60] + ("..." if len(title) > 60 else ""),
                "price": price,
                "link": affiliate_link,
            })

    except Exception as e:
        logger.error("خطأ أثناء كشط البحث: %s", e)

    return results


# =============================================================
# الدوال العامة (تُستدعى من bot.py)
# =============================================================

def identify_product_from_image(image_bytes: bytes) -> str | None:
    """
    يستخدم Gemini 1.5 Flash لتحديد اسم المنتج من الصورة.
    يرجع نص اسم المنتج، أو None إذا لم يُتعرف عليه.
    استدعاء متزامن — يجب تشغيله عبر run_in_executor.
    """
    return _call_gemini(image_bytes)


def search_amazon_by_keywords(product_name: str, domain: str = AMAZON_DOMAIN) -> list[dict]:
    """
    يبحث في أمازون بالاسم ويرجع قائمة عروض حقيقية مع أسعار وروابط أفلييت.
    استدعاء متزامن — يجب تشغيله عبر run_in_executor.
    """
    if not product_name:
        return []
    return _scrape_amazon_search(product_name, domain=domain)


def _escape_md(text: str) -> str:
    """يهرّب أحرف Markdown الخاصة في النص الديناميكي."""
    # الأحرف التي تحتاج هروب في Markdown v1 لتيليجرام
    for ch in r"_*`[":
        text = text.replace(ch, f"\\{ch}")
    return text


def format_search_results(product_name: str, offers: list[dict]) -> str:
    """يبني رسالة تيليجرام تعرض نتائج البحث مع روابط الأفلييت."""
    safe_name = _escape_md(product_name)

    if not offers:
        return (
            f"🔍 تم التعرف على: *{safe_name}*\n\n"
            "❌ لم نجد نتائج مطابقة حالياً في أمازون السعودية.\n"
            "جرّب إرسال رابط المنتج مباشرة."
        )

    lines = [f"🔍 تم التعرف على: *{safe_name}*\n\n💰 *أفضل الأسعار في أمازون:*\n"]
    for i, offer in enumerate(offers, start=1):
        safe_title = _escape_md(offer["title"])
        safe_price = _escape_md(offer["price"])
        lines.append(
            f"{i}. *{safe_title}*\n"
            f"   • السعر: `{safe_price}`\n"
            f"   • الرابط: {offer['link']}\n"
        )
    lines.append("_(روابط تسويق بالعمولة)_")

    return "\n".join(lines)
