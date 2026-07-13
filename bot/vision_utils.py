"""
دوال التعرف على المنتج من صورة، والبحث عنه داخل أمازون.
الأولوية على Railway:
  1. OpenAI GPT-4o-mini (OPENAI_API_KEY / ChatGPT)
  2. DeepSeek V4 Vision (DEEPSEEK_API_KEY)
  3. Gemini API (GEMINI_API_KEY)
  4. SerpAPI Google Lens
  5. Hugging Face BLIP
"""
import base64
import logging
import re
import threading
import requests
from amazon_utils import build_affiliate_link, build_affiliate_search_link, tag_amazon_url
import os

from config import (
    get_gemini_api_key,
    get_deepseek_api_key,
    get_openai_vision_config,
    SERPAPI_KEY,
    AFFILIATE_TAG,
    AMAZON_DOMAIN,
)

# على Railway لا يوجد Replit OpenAI — Gemini/SerpAPI أولوية للصور
_ON_RAILWAY = bool(os.getenv("RAILWAY_ENVIRONMENT") or os.getenv("RAILWAY_SERVICE_NAME"))

# حد Gemini: طلبان متزامنان — ضغط عالي يستحق 2 (429 تُعالج بالتجربة التالية)
_GEMINI_SEM = threading.Semaphore(2)

# حد DeepSeek Vision: طلبان متزامنان
_DEEPSEEK_SEM = threading.Semaphore(2)
_DEEPSEEK_URLS = (
    "https://api.deepseek.com/chat/completions",
    "https://api.deepseek.com/v1/chat/completions",
)

# صورة PNG صغيرة لاختبار vision في /debug
_TINY_PNG = (
    b"\x89PNG\r\n\x1a\n\x00\x00\x00\rIHDR\x00\x00\x00\x01\x00\x00\x00\x01"
    b"\x08\x02\x00\x00\x00\x90wS\xde\x00\x00\x00\x0cIDATx\x9cc\xf8\xcf"
    b"\xc0\x00\x00\x03\x01\x01\x00\xc4\xfe\xc6\xdb\x00\x00\x00\x00IEND\xaeB`\x82"
)


def _deepseek_post(api_key: str, payload: dict) -> requests.Response | None:
    headers = {
        "Authorization": f"Bearer {api_key}",
        "Content-Type": "application/json",
    }
    for url in _DEEPSEEK_URLS:
        try:
            resp = requests.post(url, headers=headers, json=payload, timeout=25)
            if resp.status_code != 404:
                return resp
        except Exception as exc:
            logger.warning("DeepSeek POST %s: %s", url, exc)
    return None

# حد OpenAI Vision: أقصى 5 طلبات متزامنة (رُفع من 3 لاستيعاب الضغط العالي)
_OPENAI_SEM = threading.Semaphore(5)

logger = logging.getLogger(__name__)


def _image_mime_type(image_bytes: bytes) -> str:
    """يحدد نوع الصورة من البايتات الأولى — Gemini يرفض mimeType خاطئ."""
    if image_bytes[:8] == b"\x89PNG\r\n\x1a\n":
        return "image/png"
    if image_bytes[:3] == b"GIF":
        return "image/gif"
    if image_bytes[:4] == b"RIFF" and len(image_bytes) > 11 and image_bytes[8:12] == b"WEBP":
        return "image/webp"
    return "image/jpeg"

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
    """GPT-4o-mini vision — يدعم OPENAI_API_KEY على Railway أو Replit integration."""
    base_url, api_key = get_openai_vision_config()
    if not api_key:
        return None
    if not _OPENAI_SEM.acquire(timeout=30):
        logger.warning("OpenAI SEM timeout — تخطي التحليل")
        return None
    try:
        return _call_openai_vision_inner(image_bytes, base_url, api_key)
    finally:
        _OPENAI_SEM.release()


def _call_openai_vision_inner(image_bytes: bytes, base_url: str, api_key: str) -> str | None:
    """الجسم الفعلي لطلب OpenAI — يُستدعى داخل السيمافور فقط."""
    try:
        image_b64 = base64.b64encode(image_bytes).decode("utf-8")
        mime_type = _image_mime_type(image_bytes)
        payload = {
            "model": "gpt-4o-mini",
            "messages": [{
                "role": "user",
                "content": [
                    {"type": "text", "text": _VISION_PROMPT},
                    {"type": "image_url", "image_url": {
                        "url": f"data:{mime_type};base64,{image_b64}",
                        "detail": "high",
                    }},
                ],
            }],
            "max_tokens": 100,
        }
        resp = requests.post(
            f"{base_url}/chat/completions",
            headers={
                "Authorization": f"Bearer {api_key}",
                "Content-Type": "application/json",
            },
            json=payload,
            timeout=25,
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


def test_openai_vision() -> str:
    """اختبار OpenAI/ChatGPT vision — يُستخدم في /debug."""
    base_url, api_key = get_openai_vision_config()
    if not api_key:
        return "❌ المفتاح غير موجود"
    try:
        resp = requests.post(
            f"{base_url}/chat/completions",
            headers={
                "Authorization": f"Bearer {api_key}",
                "Content-Type": "application/json",
            },
            json={
                "model": "gpt-4o-mini",
                "messages": [{"role": "user", "content": "قل: ok"}],
                "max_tokens": 10,
            },
            timeout=15,
        )
        if resp.status_code == 200:
            return "✅ يعمل (gpt-4o-mini)"
        return f"❌ HTTP {resp.status_code}: {resp.text[:100]}"
    except Exception as exc:
        return f"❌ خطأ: {exc}"


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


def _deepseek_vision_models() -> list[str]:
    return [
        "deepseek-v4-flash",
        "deepseek-v4-pro",
        "deepseek-chat",
    ]


def _call_deepseek_vision(image_bytes: bytes) -> str | None:
    """التعرف على المنتج عبر DeepSeek V4 Vision (OpenAI-compatible)."""
    api_key = get_deepseek_api_key()
    if not api_key:
        return None

    if not _DEEPSEEK_SEM.acquire(timeout=40):
        logger.warning("DeepSeek vision SEM timeout")
        return None
    try:
        image_b64 = base64.b64encode(image_bytes).decode("utf-8")
        mime_type = _image_mime_type(image_bytes)
        payload_base = {
            "messages": [{
                "role": "user",
                "content": [
                    {"type": "text", "text": _VISION_PROMPT},
                    {
                        "type": "image_url",
                        "image_url": {
                            "url": f"data:{mime_type};base64,{image_b64}",
                            "detail": "high",
                        },
                    },
                ],
            }],
            "max_tokens": 120,
            "stream": False,
        }

        for model in _deepseek_vision_models():
            resp = _deepseek_post(api_key, {**payload_base, "model": model})
            if resp is None:
                continue
            try:
                if resp.status_code == 200:
                    text = (
                        resp.json()
                        .get("choices", [{}])[0]
                        .get("message", {})
                        .get("content", "")
                        .strip()
                    )
                    if text:
                        logger.info("DeepSeek vision نجح: %s", model)
                        return text
                    logger.warning("DeepSeek vision رد فارغ (%s)", model)
                elif resp.status_code in (402, 429):
                    logger.warning("DeepSeek vision %s (%s)", resp.status_code, model)
                else:
                    logger.warning(
                        "DeepSeek vision HTTP %s (%s): %s",
                        resp.status_code, model, resp.text[:200],
                    )
            except Exception as exc:
                logger.error("DeepSeek vision parse (%s): %s", model, exc)

        return None
    finally:
        _DEEPSEEK_SEM.release()


def test_deepseek_vision() -> str:
    """اختبار DeepSeek vision — نص + صورة صغيرة."""
    api_key = get_deepseek_api_key()
    if not api_key:
        return "❌ المفتاح غير موجود"

    # 1) اختبار نص
    resp = _deepseek_post(api_key, {
        "model": "deepseek-v4-flash",
        "messages": [{"role": "user", "content": "قل: ok"}],
        "max_tokens": 10,
        "stream": False,
    })
    if resp is None:
        return "❌ لا اتصال بـ DeepSeek"
    if resp.status_code != 200:
        return f"❌ نص HTTP {resp.status_code}: {resp.text[:100]}"

    # 2) اختبار صورة
    img_b64 = base64.b64encode(_TINY_PNG).decode("utf-8")
    resp2 = _deepseek_post(api_key, {
        "model": "deepseek-v4-flash",
        "messages": [{
            "role": "user",
            "content": [
                {"type": "text", "text": "ما لون الصورة؟ جاوب كلمة واحدة"},
                {
                    "type": "image_url",
                    "image_url": {"url": f"data:image/png;base64,{img_b64}"},
                },
            ],
        }],
        "max_tokens": 20,
        "stream": False,
    })
    if resp2 is None:
        return "✅ نص يعمل | ❌ صورة: لا اتصال"
    if resp2.status_code == 200:
        return "✅ يعمل (نص + صور)"
    return f"✅ نص يعمل | ❌ صور HTTP {resp2.status_code}: {resp2.text[:80]}"


def _gemini_models() -> list[str]:
    return [
        "gemini-3.5-flash",
        "gemini-2.5-flash",
        "gemini-2.0-flash",
        "gemini-2.0-flash-lite",
        "gemini-1.5-flash",
    ]


def _call_gemini_sdk(image_bytes: bytes, api_key: str) -> str | None:
    """Gemini عبر Interactions API (google-genai SDK) — الطريقة الجديدة."""
    try:
        from google import genai
    except ImportError:
        logger.warning("google-genai غير مثبت — أنتقل لـ REST")
        return None

    image_b64 = base64.b64encode(image_bytes).decode("utf-8")
    mime_type = _image_mime_type(image_bytes)
    client = genai.Client(api_key=api_key)

    for model in _gemini_models():
        try:
            interaction = client.interactions.create(
                model=model,
                input=[
                    {"type": "text", "text": _VISION_PROMPT},
                    {
                        "type": "image",
                        "mime_type": mime_type,
                        "data": image_b64,
                        "resolution": "high",
                    },
                ],
            )
            text = (getattr(interaction, "output_text", None) or "").strip()
            if text:
                logger.info("Gemini SDK نجح: %s", model)
                return text
            logger.warning("Gemini SDK بنص فارغ (%s)", model)
        except Exception as exc:
            logger.warning("Gemini SDK (%s): %s", model, exc)

    return None


def _call_gemini_rest(image_bytes: bytes, api_key: str) -> str | None:
    """Gemini عبر REST — احتياط إذا فشل SDK."""
    import time

    image_b64 = base64.b64encode(image_bytes).decode("utf-8")
    mime_type = _image_mime_type(image_bytes)
    payload = {
        "contents": [{
            "parts": [
                {"text": _VISION_PROMPT},
                {"inlineData": {"mimeType": mime_type, "data": image_b64}},
            ]
        }]
    }

    for model in _gemini_models():
        url = (
            "https://generativelanguage.googleapis.com/v1beta/models/"
            f"{model}:generateContent?key={api_key}"
        )
        try:
            response = requests.post(url, json=payload, timeout=20)
            if response.status_code == 200:
                data = response.json()
                candidates = data.get("candidates") or []
                if not candidates:
                    logger.warning(
                        "Gemini REST 200 بدون candidates (%s): %s",
                        model, str(data)[:300],
                    )
                    continue
                cand = candidates[0]
                finish = cand.get("finishReason", "")
                if finish and finish not in ("STOP", "MAX_TOKENS"):
                    logger.warning("Gemini REST blocked (%s): finishReason=%s", model, finish)
                    continue
                text = (
                    cand.get("content", {})
                    .get("parts", [{}])[0]
                    .get("text", "")
                    .strip()
                )
                if text:
                    logger.info("Gemini REST نجح: %s", model)
                    return text
                logger.warning("Gemini REST 200 بنص فارغ (%s)", model)
            elif response.status_code == 429:
                logger.warning("Gemini REST 429 (%s)", model)
                time.sleep(1)
            else:
                logger.warning(
                    "Gemini REST %s (%s): %s",
                    response.status_code, model, response.text[:300],
                )
        except Exception as e:
            logger.error("خطأ في Gemini REST (%s): %s", model, e)

    return None


def _call_gemini(image_bytes: bytes) -> str | None:
    """
    التعرف على المنتج عبر Gemini.
    الأولوية: Interactions API (SDK) ثم REST.
    """
    api_key = get_gemini_api_key()
    if not api_key:
        return None

    if not _GEMINI_SEM.acquire(timeout=40):
        logger.warning("Gemini SEM timeout — تخطي التحليل")
        return None
    try:
        result = _call_gemini_sdk(image_bytes, api_key)
        if result:
            return result
        logger.info("Gemini SDK لم يُنتج نتيجة — أنتقل لـ REST")
        return _call_gemini_rest(image_bytes, api_key)
    finally:
        _GEMINI_SEM.release()


def test_gemini_connection() -> str:
    """اختبار سريع لصلاحية GEMINI_API_KEY — يُستخدم في /debug."""
    api_key = get_gemini_api_key()
    if not api_key:
        return "❌ المفتاح غير موجود"
    try:
        from google import genai
        client = genai.Client(api_key=api_key)
        interaction = client.interactions.create(
            model="gemini-3.5-flash",
            input="قل: ok",
        )
        text = (getattr(interaction, "output_text", None) or "").strip()
        if text:
            return "✅ يعمل (Interactions API)"
    except ImportError:
        pass
    except Exception as exc:
        return f"❌ SDK: {exc}"

    try:
        url = (
            "https://generativelanguage.googleapis.com/v1beta/models/"
            f"gemini-2.0-flash:generateContent?key={api_key}"
        )
        resp = requests.post(
            url,
            json={"contents": [{"parts": [{"text": "قل: ok"}]}]},
            timeout=15,
        )
        if resp.status_code == 200:
            return "✅ يعمل (REST)"
        body = resp.text[:180].replace("\n", " ")
        return f"❌ HTTP {resp.status_code}: {body}"
    except Exception as exc:
        return f"❌ خطأ: {exc}"


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
    على Railway: OpenAI → DeepSeek → Gemini → SerpAPI Lens → BLIP
    على Replit:  OpenAI → SerpAPI Lens → Gemini → DeepSeek → BLIP
    استدعاء متزامن — يجب تشغيله عبر run_in_executor.
    """
    if _ON_RAILWAY:
        result = _call_openai_vision(image_bytes)
        if result:
            return result
        logger.info("OpenAI vision لم يُنتج نتيجة — أنتقل لـ DeepSeek")

        result = _call_deepseek_vision(image_bytes)
        if result:
            return result
        logger.info("DeepSeek vision لم يُنتج نتيجة — أنتقل لـ Gemini")

        result = _call_gemini(image_bytes)
        if result:
            return result
        logger.info("Gemini لم يُنتج نتيجة — أنتقل لـ Google Lens")

        if image_url and SERPAPI_KEY:
            result = _google_lens(image_url)
            if result:
                return result
            logger.info("Google Lens لم يتعرف — أنتقل لـ HuggingFace")
    else:
        result = _call_openai_vision(image_bytes)
        if result:
            return result
        logger.info("OpenAI vision لم يُنتج نتيجة — أنتقل للخطوة التالية")

        if image_url and SERPAPI_KEY:
            result = _google_lens(image_url)
            if result:
                return result
            logger.info("Google Lens لم يتعرف — أنتقل لـ Gemini")

        result = _call_gemini(image_bytes)
        if result:
            return result
        logger.info("Gemini لم يُنتج نتيجة — أنتقل لـ DeepSeek")

        result = _call_deepseek_vision(image_bytes)
        if result:
            return result
        logger.info("DeepSeek vision لم يُنتج نتيجة — أنتقل لـ HuggingFace")

    # ── Hugging Face BLIP (fallback مجاني — غير موثوق على Railway) ───────────
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
    "🔥 *لقيت لك أفضل عروض هذا المنتج — تفضّل!*",
    "⚡ *عروض قوية بأسعار مغرية — لا تفوّتها!*",
    "🎯 *قارنا الأسعار وجبت لك الأفضل على أمازون!*",
    "🛍️ *خيارات ممتازة بانتظارك — اطلب الحين!*",
]

_SEARCH_CTA = [
    "👇 اضغط الزر وشوف العروض واطلب مباشرة من أمازون",
    "👇 افتح النتائج الحين واختر الأنسب لك — سريع وآمن",
    "👇 السعر يتغيّر — شوف العروض واطلب قبل ما تفوتك",
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
        f"🔍 *بحثت لك عن:* {safe_name}\n\n"
        f"{_random.choice(_SEARCH_TEASERS)}\n\n"
        f"{_random.choice(_SEARCH_CTA)}\n\n"
        f"🔒 _شراء آمن من أمازون — رابط تسويق بالعمولة_"
    )
    return teaser, search_url, image_url
