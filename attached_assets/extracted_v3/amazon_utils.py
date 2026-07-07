"""
كل الدوال المتعلقة بأمازون: استخراج ASIN، بناء رابط أفلييت، وجلب أقل سعر.
"""
import re
import random
import requests
from config import AFFILIATE_TAG, AMAZON_DOMAIN, MOCK_MODE


def resolve_short_link(url: str) -> str:
    """
    يحل الروابط المختصرة (مثل amzn.to أو a.co) لمعرفة الرابط الحقيقي
    الكامل خلفها. شائع جدًا لو نسخ المستخدم الرابط من زر "مشاركة"
    داخل تطبيق أمازون نفسه.
    إذا فشل الاتصال أو الرابط مو مختصر أصلاً، يرجع نفس الرابط الأصلي.
    """
    if "amzn.to" not in url and "a.co" not in url:
        return url  # مو رابط مختصر، رجّعه كما هو

    try:
        response = requests.head(url, allow_redirects=True, timeout=8)
        return response.url
    except requests.RequestException:
        try:
            # بعض الروابط ما تدعم HEAD، نجرب GET كبديل
            response = requests.get(url, allow_redirects=True, timeout=8)
            return response.url
        except requests.RequestException:
            return url  # فشل الحل، نرجّع الأصلي ونخلي extract_asin يحاول عليه


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


def extract_domain(url: str) -> str:
    """
    يستخرج نطاق أمازون الفعلي من الرابط الأصلي (مثل amazon.sa أو amazon.com).
    هذا مهم جدًا: لو المنتج من amazon.com وبنينا رابط بنطاق amazon.sa،
    أمازون يطلع صفحة خطأ (404) لأن المنتج غير موجود بذاك النطاق.
    """
    match = re.search(r"://(?:www\.)?(amazon\.[a-z.]+)", url, re.IGNORECASE)
    if match:
        return f"www.{match.group(1).lower()}"
    return AMAZON_DOMAIN  # احتياطي لو ما قدرنا نتعرف على النطاق


def build_affiliate_link(asin: str, domain: str = AMAZON_DOMAIN) -> str:
    """يبني رابط منتج نظيف بتاق الأفلييت الخاص بك، بنفس نطاق أمازون الصحيح."""
    return f"https://{domain}/dp/{asin}?tag={AFFILIATE_TAG}"


def get_lowest_offer(asin: str, domain: str = AMAZON_DOMAIN) -> dict | None:
    """
    يرجع أقل سعر متاح لمنتج معين بين كل البائعين.

    ⚠️ حاليًا في وضع تجريبي (Mock) — السعر وهمي للاختبار فقط،
    لكن الرابط حقيقي وشغّال لأنه يستخدم نفس ASIN ونفس نطاق أمازون
    اللي أرسله المستخدم فعليًا (مو amazon.sa دايمًا بشكل ثابت).

    لما تجهز وصولك لـ Creators API الرسمي، استبدل الكود بالداخل
    بالاستدعاء الحقيقي لـ OffersV2 وارجع نفس شكل القاموس أدناه.
    """
    if MOCK_MODE:
        # السعر وهمي، لكن الرابط حقيقي 100% (نفس المنتج ونفس النطاق الصحيح)
        base_price = random.randint(50, 900)
        return {
            "asin": asin,
            "price": base_price,
            "currency": "SAR",
            "seller_name": "(⚠️ سعر تجريبي وهمي)",
            "condition": "جديد",
            "affiliate_link": build_affiliate_link(asin, domain=domain),
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
