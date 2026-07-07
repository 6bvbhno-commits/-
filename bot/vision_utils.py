"""
دوال التعرف على المنتج من صورة، والبحث عنه داخل أمازون.
"""
import base64
import requests
from config import GOOGLE_VISION_API_KEY, MOCK_MODE, AFFILIATE_TAG, AMAZON_DOMAIN
from amazon_utils import build_affiliate_link


def identify_product_from_image(image_bytes: bytes) -> list[str]:
    """
    يحلل الصورة ويرجع قائمة كلمات وصفية عن المنتج (labels).
    يستخدم Google Cloud Vision API — Web Detection + Label Detection معًا
    لأفضل دقة ممكنة على منتجات فيها شعار أو نص.
    """
    if MOCK_MODE or not GOOGLE_VISION_API_KEY:
        # نتيجة وهمية للتجربة بدون مفتاح API حقيقي
        return ["حذاء رياضي", "أبيض", "sneaker"]

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

    response = requests.post(url, json=payload, timeout=15)
    response.raise_for_status()
    data = response.json()

    labels = []
    result = data.get("responses", [{}])[0]

    # أولوية لنتائج Web Detection لأنها أدق لأسماء منتجات فعلية
    web_entities = result.get("webDetection", {}).get("webEntities", [])
    for entity in web_entities:
        if "description" in entity:
            labels.append(entity["description"])

    # إضافة Label Detection كدعم إضافي
    for label in result.get("labelAnnotations", []):
        labels.append(label["description"])

    return labels[:5] if labels else []


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
