"""
دوال التعرف على المنتج من صورة، والبحث عنه داخل أمازون.
"""
import asyncio
import base64
import logging
import requests
from config import GOOGLE_VISION_API_KEY, MOCK_MODE, AFFILIATE_TAG, AMAZON_DOMAIN
from amazon_utils import build_affiliate_link

logger = logging.getLogger(__name__)


def _call_vision_api(image_bytes: bytes) -> list[str]:
    """
    استدعاء متزامن لـ Google Cloud Vision API.
    يُستدعى من خلال run_in_executor عشان ما يحجب event loop.
    """
    image_b64 = base64.b64encode(image_bytes).decode("utf-8")
    url = f"https://vision.googleapis.com/v1/images:annotate?key={GOOGLE_VISION_API_KEY}"

    payload = {
        "requests": [
            {
                "image": {"content": image_b64},
                "features": [
                    {"type": "WEB_DETECTION", "maxResults": 5},
                    {"type": "LABEL_DETECTION", "maxResults": 5},
                ],
            }
        ]
    }

    try:
        response = requests.post(url, json=payload, timeout=15)
        response.raise_for_status()
        data = response.json()
    except requests.RequestException as e:
        logger.error("Vision API request failed: %s", e)
        return []

    responses = data.get("responses")
    if not responses or not isinstance(responses, list):
        logger.error("Unexpected Vision API response shape: %s", data)
        return []

    result = responses[0]
    if "error" in result:
        logger.error("Vision API returned error: %s", result["error"])
        return []

    labels = []

    # أولوية لنتائج Web Detection لأنها أدق لأسماء منتجات فعلية
    web_entities = result.get("webDetection", {}).get("webEntities", [])
    for entity in web_entities:
        if "description" in entity:
            labels.append(entity["description"])

    # إضافة Label Detection كدعم إضافي
    for label in result.get("labelAnnotations", []):
        labels.append(label.get("description", ""))

    return [l for l in labels if l][:5]


async def identify_product_from_image(image_bytes: bytes) -> list[str]:
    """
    يحلل الصورة ويرجع قائمة كلمات وصفية عن المنتج (labels).
    يستخدم Google Cloud Vision API — Web Detection + Label Detection معًا
    لأفضل دقة ممكنة على منتجات فيها شعار أو نص.
    """
    if MOCK_MODE or not GOOGLE_VISION_API_KEY:
        # نتيجة وهمية للتجربة بدون مفتاح API حقيقي
        return ["حذاء رياضي", "أبيض", "sneaker"]

    loop = asyncio.get_event_loop()
    return await loop.run_in_executor(None, _call_vision_api, image_bytes)


def search_amazon_by_keywords(keywords: list[str]) -> list[dict]:
    """
    يبحث في أمازون عن منتجات مطابقة للكلمات المستخرجة من الصورة.

    ⚠️ حاليًا في وضع تجريبي — يرجع نتائج وهمية.
    استبدل بالاتصال الحقيقي عبر Creators API (SearchItems) عند الجهوزية.
    """
    if MOCK_MODE:
        query = " ".join(keywords[:2]) if keywords else "منتج"
        return [
            {
                "title": f"{query} - نتيجة تجريبية 1",
                "asin": "B0MOCK0001",
                "available": True,
                "affiliate_link": build_affiliate_link("B0MOCK0001"),
            },
            {
                "title": f"{query} - نتيجة تجريبية 2",
                "asin": "B0MOCK0002",
                "available": False,
                "affiliate_link": build_affiliate_link("B0MOCK0002"),
            },
        ]

    # ============================================================
    # TODO: استبدل بالاتصال الحقيقي بـ Creators API SearchItems
    # query_string = " ".join(keywords)
    # response = creators_api_client.search_items(keywords=query_string)
    # return [...]
    # ============================================================
    return []


def format_search_results(results: list[dict]) -> str:
    """يبني رسالة تعرض نتائج البحث بالصورة."""
    if not results:
        return "❌ ما لقيت أي منتج مطابق. جرب صورة أوضح فيها شعار أو نص على المنتج."

    lines = ["🔍 لقيت هذي النتائج المحتملة:\n"]
    for i, item in enumerate(results, start=1):
        status = "✅ متوفر" if item["available"] else "❌ غير متوفر حاليًا"
        lines.append(
            f"{i}. {item['title']}\n"
            f"   الحالة: {status}\n"
            f"   الرابط: {item['affiliate_link']}\n"
        )
    lines.append("_(روابط تسويق بالعمولة)_")
    return "\n".join(lines)
