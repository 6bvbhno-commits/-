"""
دوال التعرف على المنتج من صورة، والبحث عنه داخل أمازون.
الأولوية:
  1. OpenAI GPT-4o-mini vision (Replit integration)
  2. SerpAPI Google Lens (إذا وُجد SERPAPI_KEY)
  3. Gemini API (إذا وُجد GEMINI_API_KEY)
  4. Hugging Face BLIP (مجاني بلا مفتاح — يعمل دائماً)
"""
import base64
import logging
import re
import threading
import requests
from amazon_utils import build_affiliate_link, build_affiliate_search_link, tag_amazon_url
from config import GEMINI_API_KEY, SERPAPI_KEY, OPENAI_BASE_URL, OPENAI_API_KEY, AFFILIATE_TAG, AMAZON_DOMAIN

# حد Gemini: طلبان متزامنان — ضغط عالي يستحق 2 (429 تُعالج بالتجربة التالية)
_GEMINI_SEM = threading.Semaphore(2)

# حد OpenAI Vision: أقصى 5 طلبات متزامنة (رُفع من 3 لاستيعاب الضغط العالي)
_OPENAI_SEM = threading.Semaphore(5)

logger = logging.getLogger(__name__)

# برومبت موحّد للتعرف على المنتج — مُحسّن لإخراج عبارة بحث دقيقة تصلح لأمازون مباشرة
_VISION_PROMPT = (
    "حلل الصورة بدقة وأعطني أفضل عبارة بحث للعثور على هذا المنتج بالضبط في أمازون. "
    "اذكر ما يظهر منها فقط بهذا الترتيب: العلامة التجارية (Brand) + الموديل أو رقم المنتج + "
    "نوع المنتج + أهم مواصفة مميزة (اللون/الحجم/السعة/العدد). "
    "إن كانت العلامة أو الموديل بالإنجليزية فاكتبها بالإنجليزية. "
    "أخرِج العبارة فقط في سطر واحد، بدون أي شرح أو مقدمات أو ترقيم."
)

_HEADERS = {
    "User-Agent": (
        "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
        "AppleWebKit/537.36 (KHTML, like Gecko) "
        "Chrome/120.0.0.0 Safari/537.36"
    ),
    "Accept-Language": "ar,en-US;q=0.9,en;q=0.8",
}


def _call_openai_vision(image_bytes: bytes) -> str | None:
    """
    يستخدم GPT-4o-mini عبر Replit OpenAI Integration للتعرف على المنتج.
    لا يحتاج مفتاح خاص — مدار عبر Replit.
    """
    if not OPENAI_BASE_URL or not OPENAI_API_KEY:
        return None
    # حد الطلبات المتزامنة — timeout 30s منعاً للانتظار الأبدي
    if not _OPENAI_SEM.acquire(timeout=30):
        logger.warning("OpenAI SEM timeout — تخطي التحليل")
        return None
    try:
        return _call_openai_vision_inner(image_bytes)
    finally:
        _OPENAI_SEM.release()


def _call_openai_vision_inner(image_bytes: bytes) -> str | None:
    """الجسم الفعلي لطلب OpenAI — يُستدعى داخل السيمافور فقط."""
    try:
        image_b64 = base64.b64encode(image_bytes).decode("utf-8")
        payload = {
            "model": "gpt-4o-mini",
            "messages": [{
                "role": "user",
                "content": [
                    {"type": "text", "text": _VISION_PROMPT},
                    {"type": "image_url", "image_url": {
                        "url": f"data:image/jpeg;base64,{image_b64}",
                        "detail": "high",
                    }},
                ],
            }],
            "max_tokens": 100,
        }
        resp = requests.post(
            f"{OPENAI_BASE_URL.rstrip('/')}/chat/completions",
            headers={
                "Authorization": f"Bearer {OPENAI_API_KEY}",
                "Content-Type": "application/json",
            },
            json=payload,
            timeout=20,
        )
        if resp.status_code != 200:
            logger.warning("OpenAI vision HTTP %s: %s", resp.status_code, resp.text[:200])
            return None
        text = (
            resp.json()
            .get("choices", [{}])[0]
            .get("message", {})
            .get("content", "")
            .strip()
        )
        if text:
            logger.info("OpenAI vision نجح: %s", text[:60])
        return text or None
    except Exception as exc:
        logger.error("OpenAI vision exception: %s", exc)
        return None


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
        "gemini-2.0-flash",       # مستقر ومتاح — الأولوية
        "gemini-2.0-flash-lite",  # أخف وأسرع
        "gemini-1.5-flash",       # احتياط قديم لكن موثوق
        "gemini-2.5-flash",       # قد يكون متاحاً في بعض المناطق
    ]

    image_b64 = base64.b64encode(image_bytes).decode("utf-8")
    payload = {
        "contents": [{
            "parts": [
                {"text": _VISION_PROMPT},
                {"inlineData": {"mimeType": "image/jpeg", "data": image_b64}},
            ]
        }]
    }

    # semaphore — timeout 40s منعاً للانتظار الأبدي
    if not _GEMINI_SEM.acquire(timeout=40):
        logger.warning("Gemini SEM timeout — تخطي التحليل")
        return None
    try:
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
    finally:
        _GEMINI_SEM.release()


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

        # إعادة المحاولة مرة واحدة بعد تأخير لو جاء 503 أو 429
        if response.status_code in (503, 429):
            logger.warning("Search status %s — إعادة المحاولة بعد 2 ثانية للاستعلام: %s",
                           response.status_code, query)
            time.sleep(2)
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
                affiliate_link = build_affiliate_link(asin, domain)
            elif link_el:
                raw_href = link_el.get("href", "")
                affiliate_link = tag_amazon_url(
                    f"https://www.{domain}{raw_href.split('?')[0]}",
                    domain,
                )
            else:
                continue

            # استخراج السعر — محاولة عدة selectors
            price = "غير محدد"
            price_selectors = [
                "span.a-price .a-offscreen",
                ".a-price-whole",
                "span.a-offscreen",
                ".a-color-price",
            ]
            for sel in price_selectors:
                el = item.select_one(sel)
                if el:
                    raw = el.get_text(strip=True)
                    price_clean = re.sub(r"[^\d.,٠-٩]", "", raw)
                    if price_clean and any(c.isdigit() for c in price_clean):
                        price = f"{price_clean} ريال"
                        break

            # صورة المنتج
            img_el = item.select_one("img.s-image") or item.select_one("img")
            image_url = img_el.get("src", "") if img_el else ""

            results.append({
                "title": title[:60] + ("..." if len(title) > 60 else ""),
                "price": price,
                "link": affiliate_link,
                "image": image_url,
            })

    except Exception as e:
        logger.error("خطأ أثناء كشط البحث: %s", e)

    return results


# =============================================================
# الدوال العامة (تُستدعى من bot.py)
# =============================================================

def _call_huggingface_vision(image_bytes: bytes) -> str | None:
    """
    يستخدم Hugging Face Inference API للتعرف على المنتج.
    مجاني تماماً — لا يحتاج أي مفتاح API.
    يستخدم نموذج BLIP لوصف الصورة ثم يحوّله لاسم منتج.
    """
    try:
        # نموذج BLIP لوصف الصور — مجاني بالكامل
        resp = requests.post(
            "https://api-inference.huggingface.co/models/Salesforce/blip-image-captioning-large",
            headers={"Content-Type": "application/octet-stream"},
            data=image_bytes,
            timeout=30,
        )
        if resp.status_code == 503:
            # النموذج يتحمّل — انتظر وأعد المحاولة
            import time
            logger.info("HuggingFace: النموذج يتحمّل، انتظار 10 ثوانٍ...")
            time.sleep(10)
            resp = requests.post(
                "https://api-inference.huggingface.co/models/Salesforce/blip-image-captioning-large",
                headers={"Content-Type": "application/octet-stream"},
                data=image_bytes,
                timeout=30,
            )
        if resp.status_code != 200:
            logger.warning("HuggingFace BLIP HTTP %s: %s", resp.status_code, resp.text[:100])
            return None

        data = resp.json()
        if isinstance(data, list) and data:
            caption = data[0].get("generated_text", "").strip()
            if caption:
                logger.info("HuggingFace BLIP نجح: %s", caption[:60])
                # BLIP يرجع وصفاً إنجليزياً — نستخدمه مباشرة للبحث
                return caption
        logger.warning("HuggingFace BLIP: نتيجة فارغة")
        return None
    except Exception as exc:
        logger.error("HuggingFace BLIP exception: %s", exc)
        return None


def identify_product_from_image(image_bytes: bytes, image_url: str = "") -> str | None:
    """
    يتعرف على المنتج من الصورة.
    الأولوية:
      1. OpenAI GPT-4o-mini vision (Replit integration — موثوق وسريع)
      2. SerpAPI Google Lens (إذا وُجد SERPAPI_KEY + image_url)
      3. Gemini API (إذا وُجد GEMINI_API_KEY)
      4. Hugging Face BLIP (مجاني بلا مفتاح — يعمل دائماً كـ fallback)
    استدعاء متزامن — يجب تشغيله عبر run_in_executor.
    """
    # ── OpenAI Vision (الأولوية الأولى) ──────────────────────────────────────
    result = _call_openai_vision(image_bytes)
    if result:
        return result
    logger.info("OpenAI vision لم يُنتج نتيجة — أنتقل للخطوة التالية")

    # ── Google Lens (الأولوية الثانية) ───────────────────────────────────────
    if image_url and SERPAPI_KEY:
        result = _google_lens(image_url)
        if result:
            return result
        logger.info("Google Lens لم يتعرف — أنتقل لـ Gemini")

    # ── Gemini (الأولوية الثالثة) ─────────────────────────────────────────────
    result = _call_gemini(image_bytes)
    if result:
        return result
    logger.info("Gemini لم يُنتج نتيجة — أنتقل لـ HuggingFace")

    # ── Hugging Face BLIP (fallback مجاني — لا يحتاج مفتاح) ─────────────────
    return _call_huggingface_vision(image_bytes)


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
                        "image": r.get("image", ""),
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


import random as _random

_SEARCH_TEASERS = [
    "🔥 *لقيت لك أفضل عروض هذا المنتج على أمازون!*",
    "⚡ *عروض قوية متوفرة الحين على هذا المنتج!*",
    "🎯 *أفضل الأسعار لقيتها لك — تفضّل!*",
    "🛍️ *وجدت لك خيارات ممتازة بأسعار مغرية!*",
]

_SEARCH_CTA = [
    "اضغط الزر تحت 👇 وشوف العروض واطلب مباشرة من أمازون",
    "افتح النتائج الحين 👇 واختر الأنسب لك",
    "اضغط للأسفل 👇 وتصفّح العروض بنفسك على أمازون",
]


def format_search_results(product_name: str, offers: list[dict]) -> tuple[str, str, str]:
    """يبني رسالة تيليجرام تعرض نتائج البحث مع روابط الأفلييت.

    يرجع (النص، رابط البحث، رابط صورة المنتج). رابط الصورة قد يكون فارغاً.
    """
    safe_name = _escape_md(product_name)
    search_url = build_affiliate_search_link(product_name, AMAZON_DOMAIN)

    # صورة أول عرض يحتوي على رابط صورة صالح
    image_url = ""
    for off in (offers or []):
        img = (off.get("image") or "").strip()
        if img.startswith("http"):
            image_url = img
            break

    teaser = (
        f"🔍 تم التعرف على: *{safe_name}*\n\n"
        f"{_random.choice(_SEARCH_TEASERS)}\n\n"
        f"{_random.choice(_SEARCH_CTA)}\n\n"
        f"🔒 _شراء آمن من أمازون — رابط تسويق بالعمولة_"
    )
    return teaser, search_url, image_url
