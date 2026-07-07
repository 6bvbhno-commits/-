"""
دوال التعرف على المنتج من صورة، والبحث عنه داخل أمازون.
الأولوية:
  1. SerpAPI Google Lens (إذا وُجد SERPAPI_KEY) — فوري وبلا حصة مجانية
  2. Gemini API (fallback) — مجاني لكن محدود
"""
import base64
import logging
import re
import threading
import requests
from config import GEMINI_API_KEY, SERPAPI_KEY, AFFILIATE_TAG, AMAZON_DOMAIN

# حد Gemini: طلب واحد في المرة — يمنع كل المستخدمين من ضرب الـ 429 بالتزامن
_GEMINI_SEM = threading.Semaphore(1)

logger = logging.getLogger(__name__)

_HEADERS = {
    "User-Agent": (
        "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
        "AppleWebKit/537.36 (KHTML, like Gecko) "
        "Chrome/120.0.0.0 Safari/537.36"
    ),
    "Accept-Language": "ar,en-US;q=0.9,en;q=0.8",
}


def _google_lens(image_url: str) -> str | None:
    """
    يستخدم SerpAPI Google Lens للتعرف على المنتج من URL الصورة.
    يرجع اسم المنتج أو None.
    """
    if not SERPAPI_KEY:
        return None
    try:
        resp = requests.get(
            "https://serpapi.com/search.json",
            params={"engine": "google_lens", "url": image_url, "api_key": SERPAPI_KEY},
            timeout=15,
        )
        if resp.status_code != 200:
            logger.warning("Google Lens HTTP %s", resp.status_code)
            return None
        data = resp.json()
        if "error" in data:
            logger.warning("Google Lens error: %s", data["error"])
            return None

        # أولاً: اسم المنتج من knowledge graph
        kg = data.get("knowledge_graph", [])
        if kg and isinstance(kg, list):
            name = kg[0].get("title") or kg[0].get("name")
            if name:
                logger.info("Google Lens: عرف المنتج من knowledge_graph: %s", name)
                return name

        # ثانياً: أول تطابق بصري فيه عنوان
        for match in data.get("visual_matches", [])[:5]:
            title = match.get("title", "").strip()
            if title and len(title) > 3:
                logger.info("Google Lens: عرف المنتج من visual_matches: %s", title)
                return title

        logger.info("Google Lens: لا نتيجة واضحة")
        return None
    except Exception as exc:
        logger.error("Google Lens exception: %s", exc)
        return None


def _call_gemini(image_bytes: bytes) -> str | None:
    """
    استدعاء متزامن لـ Gemini — محاولة واحدة فقط لكل موديل بدون انتظار طويل.
    يستخدم semaphore لمنع الطلبات المتزامنة التي تسبب 429.
    """
    if not GEMINI_API_KEY:
        return None

    import time

    models = [
        "gemini-2.5-flash-lite",
        "gemini-2.5-flash",
        "gemini-2.0-flash",
        "gemini-2.0-flash-lite",
    ]

    image_b64 = base64.b64encode(image_bytes).decode("utf-8")
    payload = {
        "contents": [{
            "parts": [
                {"text": (
                    "ما هو هذا المنتج بدقة؟ "
                    "أعطني فقط اسم المنتج والموديل باللغة الإنجليزية أو العربية "
                    "لكي أبحث عنه في موقع أمازون. "
                    "لا تكتب أي جمل أخرى، فقط اسم المنتج للبحث المباشر."
                )},
                {"inlineData": {"mimeType": "image/jpeg", "data": image_b64}},
            ]
        }]
    }

    # semaphore: طلب واحد فقط في الوقت الواحد لتجنب 429 الجماعي
    with _GEMINI_SEM:
        for model in models:
            url = (
                "https://generativelanguage.googleapis.com/v1beta/models/"
                f"{model}:generateContent?key={GEMINI_API_KEY}"
            )
            try:
                response = requests.post(url, json=payload, timeout=20)
                if response.status_code == 200:
                    text = (
                        response.json()
                        .get("candidates", [{}])[0]
                        .get("content", {})
                        .get("parts", [{}])[0]
                        .get("text", "")
                        .strip()
                    )
                    if text:
                        logger.info("Gemini نجح: %s", model)
                        return text
                elif response.status_code == 429:
                    logger.warning("Gemini 429 (%s) — تجربة الموديل التالي فوراً", model)
                    time.sleep(1)   # ثانية واحدة فقط بين الموديلات
                else:
                    logger.warning("Gemini %s للموديل %s", response.status_code, model)
            except Exception as e:
                logger.error("خطأ في Gemini (%s): %s", model, e)

    logger.error("Gemini: فشلت جميع الموديلات")
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

def identify_product_from_image(image_bytes: bytes, image_url: str = "") -> str | None:
    """
    يتعرف على المنتج من الصورة.
    الأولوية:
      1. SerpAPI Google Lens (إذا وُجد SERPAPI_KEY + image_url)
      2. Gemini API (fallback)
    استدعاء متزامن — يجب تشغيله عبر run_in_executor.
    """
    # ── Google Lens (الأسرع والأموثق) ────────────────────────────────────────
    if image_url and SERPAPI_KEY:
        result = _google_lens(image_url)
        if result:
            return result
        logger.info("Google Lens لم يتعرف — أنتقل لـ Gemini")

    # ── Gemini (fallback) ─────────────────────────────────────────────────────
    return _call_gemini(image_bytes)


def search_amazon_by_keywords(product_name: str, domain: str = AMAZON_DOMAIN) -> list[dict]:
    """
    يبحث في أمازون بالاسم ويرجع قائمة عروض حقيقية مع أسعار وروابط أفلييت.
    الأولوية: PA API (إذا وُجدت المفاتيح) → كشط مباشر (fallback).
    استدعاء متزامن — يجب تشغيله عبر run_in_executor.
    """
    if not product_name:
        return []

    # ── SerpAPI (الأولوية الأولى) ────────────────────────────────────────────
    try:
        from serpapi_utils import search_items as serp_search, serpapi_available
        if serpapi_available():
            logger.info("SerpAPI search: %s", product_name)
            results = serp_search(product_name, domain=domain, max_results=5)
            if results:
                return results
            logger.info("SerpAPI: لا نتائج، أنتقل للخطوة التالية")
    except Exception as e:
        logger.warning("SerpAPI search exception: %s — أنتقل للخطوة التالية", e)

    # ── PA API (الأولوية الثانية) ─────────────────────────────────────────────
    try:
        from paapi_utils import search_items as pa_search, paapi_available
        if paapi_available():
            logger.info("PA API SearchItems: %s", product_name)
            pa_results = pa_search(product_name, max_results=5)
            if pa_results:
                return [
                    {
                        "title": r.get("title") or product_name,
                        "price": r.get("price") or "غير محدد",
                        "link":  r.get("affiliate_link", ""),
                    }
                    for r in pa_results
                ]
            logger.info("PA API: لا نتائج للبحث، أنتقل للكشط")
    except Exception as e:
        logger.warning("PA API search exception: %s — أنتقل للكشط", e)

    # ── كشط مباشر (fallback أخير) ────────────────────────────────────────────
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
