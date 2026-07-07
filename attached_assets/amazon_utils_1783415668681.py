"""
كل الدوال المتعلقة بأمازون: استخراج ASIN، بناء رابط أفلييت، وجلب أقل سعر متاح.
"""
import re
import random
from config import AFFILIATE_TAG, AMAZON_DOMAIN, MOCK_MODE


def extract_asin(url: str) -> str | None:
    """يستخرج ASIN من أي شكل رابط أمازون شائع."""
    patterns = [
        r"/dp/([A-Z0-9]{10})",
        r"/gp/product/([A-Z0-9]{10})",
        r"/product/([A-Z0-9]{10})",
        r"[?&]asin=([A-Z0-9]{10})",
    ]
    for pattern in patterns:
        match = re.search(pattern, url, re.IGNORECASE)
        if match:
            return match.group(1).upper()
    return None


def build_affiliate_link(asin: str, domain: str = AMAZON_DOMAIN) -> str:
    """يبني رابط منتج نظيف بتاق الأفلييت الخاص بك."""
    return f"https://{domain}/dp/{asin}?tag={AFFILIATE_TAG}"


def get_lowest_offer(asin: str) -> dict | None:
    """
    يرجع أقل سعر متاح لمنتج معين بين كل البائعين.

    ⚠️ حاليًا في وضع تجريبي (Mock) — يرجع بيانات وهمية للاختبار فقط.
    لما تجهز وصولك لـ Creators API الرسمي، استبدل الكود بالداخل
    بالاستدعاء الحقيقي لـ OffersV2 وارجع نفس شكل القاموس أدناه.
    """
    if MOCK_MODE:
        # بيانات وهمية عشوائية للتجربة فقط
        base_price = random.randint(50, 900)
        return {
            "asin": asin,
            "price": base_price,
            "currency": "SAR",
            "seller_name": "Amazon.sa (تجريبي)",
            "condition": "جديد",
            "affiliate_link": build_affiliate_link(asin),
        }

    # ============================================================
    # TODO: استبدل هذا الجزء بالاتصال الحقيقي بـ Creators API
    # مثال تقريبي (يحتاج مفاتيح ومصادقة حقيقية):
    #
    # response = creators_api_client.get_offers(asin=asin)
    # offers = response["offers"]
    # if not offers:
    #     return None
    # lowest = min(offers, key=lambda o: o["price"])
    # return {
    #     "asin": asin,
    #     "price": lowest["price"],
    #     "currency": lowest["currency"],
    #     "seller_name": lowest["seller_name"],
    #     "condition": lowest["condition"],
    #     "affiliate_link": build_affiliate_link(asin),
    # }
    # ============================================================
    return None


def format_offer_message(offer: dict) -> str:
    """يبني رسالة جاهزة للإرسال في تيليجرام."""
    if not offer:
        return "❌ ما قدرت ألقى عروض متاحة لهذا المنتج حاليًا."

    return (
        f"💰 أقل سعر متاح حاليًا:\n\n"
        f"السعر: {offer['price']} {offer['currency']}\n"
        f"البائع: {offer['seller_name']}\n"
        f"الحالة: {offer['condition']}\n\n"
        f"🛒 اشترِ الآن:\n{offer['affiliate_link']}\n\n"
        f"_(رابط تسويق بالعمولة)_"
    )
