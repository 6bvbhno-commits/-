"""
دوال التعرف على المنتج من صورة، والبحث عنه داخل أمازون.
"""
import base64
import requests
from urllib.parse import quote_plus
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


def build_search_link(keywords: list[str], domain: str = AMAZON_DOMAIN) -> str:
    """
    يبني رابط بحث حقيقي وشغّال داخل أمازون (صفحة نتائج بحث فعلية،
    وليس منتج محدد وهمي). هذا رابط حقيقي 100% حتى بدون أي API متقدم،
    لأنه يستخدم رابط البحث العادي لأمازون + تاق الأفلييت.
    """
    query = " ".join(keywords[:3]) if keywords else "منتج"
    encoded_query = quote_plus(query)
    return f"https://{domain}/s?k={encoded_query}&tag={AFFILIATE_TAG}"


def search_amazon_by_keywords(keywords: list[str], domain: str = AMAZON_DOMAIN) -> dict:
    """
    يبني رابط بحث حقيقي داخل أمازون بناءً على الكلمات المستخرجة من الصورة.

    ملاحظة مهمة: هذا رابط بحث (يفتح صفحة نتائج)، وليس تأكيد مباشر
    "متوفر / غير متوفر" لمنتج محدد بالضبط — لأن التحقق الدقيق من التوفر
    لمنتج بعينه يحتاج Creators API معتمد. الرابط حقيقي وشغّال، لكن
    المستخدم لازم يتأكد بنفسه من نتائج البحث إذا كانت مطابقة تمامًا.
    """
    if not keywords:
        return {"query": None, "search_link": None}

    query = " ".join(keywords[:3])
    return {
        "query": query,
        "search_link": build_search_link(keywords, domain=domain),
    }


def format_search_results(result: dict) -> str:
    """يبني رسالة تعرض رابط بحث حقيقي بناءً على الكلمات المستخرجة."""
    if not result or not result.get("search_link"):
        return "❌ ما قدرت أطلّع كلمات واضحة من الصورة. جرب صورة أوضح فيها شعار أو نص على المنتج."

    return (
        f"🔍 بناءً على الصورة، هذي الكلمات اللي تعرّفت عليها:\n"
        f"«{result['query']}»\n\n"
        f"🛒 هذا رابط بحث حقيقي داخل أمازون لنفس الكلمات:\n"
        f"{result['search_link']}\n\n"
        f"افتح الرابط وشوف بنفسك إذا فيه تطابق دقيق مع المنتج ووش حالة توفره.\n\n"
        f"_(رابط تسويق بالعمولة)_"
    )
