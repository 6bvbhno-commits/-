"""
كل الدوال المتعلقة بأمازون: استخراج ASIN، بناء رابط أفلييت، وجلب أقل سعر.
"""
import datetime
import hashlib
import hmac
import json
import logging
import re
import random
import requests
from config import AFFILIATE_TAG, AMAZON_DOMAIN, MOCK_MODE, AMAZON_ACCESS_KEY, AMAZON_SECRET_KEY

logger = logging.getLogger(__name__)


def sign_amazon_request(
    host: str, uri: str, payload: str, access_key: str, secret_key: str
) -> dict:
    """يوقّع الطلب رقمياً بـ AWS Signature V4 ويجهّز الترويسات للاتصال بـ PA API v5."""
    now = datetime.datetime.utcnow()
    amz_date = now.strftime("%Y%m%dT%H%M%SZ")
    datestamp = now.strftime("%Y%m%d")

    region = "eu-west-1" if "sa" in host or "ae" in host else "us-east-1"
    service = "ProductAdvertisingAPI"

    canonical_headers = (
        f"content-type:application/json; charset=utf-8\nhost:{host}\n"
        f"x-amz-date:{amz_date}\nx-amz-target:com.amazon.paapi5.v1.ProductAdvertisingAPIv1.GetItems\n"
    )
    signed_headers = "content-type;host;x-amz-date;x-amz-target"

    payload_hash = hashlib.sha256(payload.encode("utf-8")).hexdigest()
    canonical_request = (
        f"POST\n{uri}\n\n{canonical_headers}\n{signed_headers}\n{payload_hash}"
    )

    credential_scope = f"{datestamp}/{region}/{service}/aws4_request"
    string_to_sign = (
        f"AWS4-HMAC-SHA256\n{amz_date}\n{credential_scope}\n"
        f"{hashlib.sha256(canonical_request.encode('utf-8')).hexdigest()}"
    )

    def _sign(key, msg):
        return hmac.new(key, msg.encode("utf-8"), hashlib.sha256).digest()

    k_date = _sign(f"AWS4{secret_key}".encode("utf-8"), datestamp)
    k_region = _sign(k_date, region)
    k_service = _sign(k_region, service)
    k_signing = _sign(k_service, "aws4_request")

    signature = hmac.new(
        k_signing, string_to_sign.encode("utf-8"), hashlib.sha256
    ).hexdigest()
    authorization_header = (
        f"AWS4-HMAC-SHA256 Credential={access_key}/{credential_scope}, "
        f"SignedHeaders={signed_headers}, Signature={signature}"
    )

    return {
        "Content-Type": "application/json; charset=utf-8",
        "Host": host,
        "X-Amz-Date": amz_date,
        "X-Amz-Target": "com.amazon.paapi5.v1.ProductAdvertisingAPIv1.GetItems",
        "Authorization": authorization_header,
    }


def resolve_short_link(url: str) -> str:
    """
    يحل أي رابط مختصر (amzn.to، a.co، amzn.eu، أو أي شكل ثاني) عبر
    متابعة إعادة التوجيه ومعرفة الرابط الحقيقي الكامل خلفه.

    مهم: أمازون يرفض أو يتجاهل الطلبات اللي ما تشبه متصفح حقيقي،
    فنرسل نفس ترويسة (User-Agent) اللي يرسلها متصفح حقيقي حتى
    يكمّل التحويل بشكل طبيعي. نستخدم Session لدعم ملفات تعريف
    الارتباط والتتبع التلقائي لكل التحويلات.
    """
    headers = {
        "User-Agent": (
            "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
            "(KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36"
        ),
        "Accept-Language": "ar,en-US;q=0.9,en;q=0.8",
    }
    try:
        session = requests.Session()
        response = session.get(
            url, headers=headers, allow_redirects=True, timeout=12
        )
        return response.url
    except requests.RequestException as e:
        logger.error("فشل فك تتبع الرابط المختصر بسبب: %s", e)
        return url  # فشل الحل، نرجّع الأصلي ونخلي extract_asin يحاول عليه


def extract_asin(url: str) -> str | None:
    """مستخرج ASIN ذكي يدعم كافة الأنماط الطويلة، المختصرة، وصيغ روابط مشاركة التطبيقات."""
    # تنظيف الرابط من أي نصوص زائدة قد تأتي من نسخ التطبيق
    url_match = re.search(r"https?://\S+", url)
    if url_match:
        url = url_match.group(0)

    patterns = [
        r"/dp/([A-Z0-9]{10})",
        r"/gp/product/([A-Z0-9]{10})",
        r"/product/([A-Z0-9]{10})",
        r"[?&]asin=([A-Z0-9]{10})",
        r"/aw/d/([A-Z0-9]{10})",   # روابط تصفح الجوال
        r"/d/([A-Z0-9]{10})",
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
    يُرجع النطاق بدون www (مطلوب لصحة عنوان PAAPI: paapi.amazon.sa).
    """
    match = re.search(r"://(?:www\.)?(amazon\.[a-z.]+)", url, re.IGNORECASE)
    if match:
        return match.group(1).lower()
    return AMAZON_DOMAIN  # احتياطي لو ما قدرنا نتعرف على النطاق


def build_affiliate_link(asin: str, domain: str = AMAZON_DOMAIN) -> str:
    """يبني رابط منتج نظيف بتاق الأفلييت الخاص بك، بنفس نطاق أمازون الصحيح."""
    return f"https://{domain}/dp/{asin}?tag={AFFILIATE_TAG}"


def get_lowest_offer(asin: str, domain: str = AMAZON_DOMAIN) -> dict | None:
    """
    يبحث عن أرخص بائع متاح للمنتج عبر PA API v5 الرسمي.
    يقارن كل العروض المتاحة ويختار الأقل سعراً من البائعين الجدد.
    يرجع إلى بيانات وهمية إذا كان MOCK_MODE مفعّلاً أو المفاتيح غير مدخلة.
    """
    if MOCK_MODE or not AMAZON_ACCESS_KEY:
        base_price = random.randint(100, 500)
        currency = "SAR" if "sa" in domain else "USD"
        return {
            "asin": asin,
            "title": "منتج تجريبي ذكي",
            "price": f"{base_price}.00",
            "currency": currency,
            "seller_name": "Amazon.sa",
            "condition": "جديد",
            "is_prime": True,
            "affiliate_link": build_affiliate_link(asin, domain=domain),
        }

    # الـ endpoint الصحيح لـ PA API v5: webservices.{domain}
    host = f"webservices.{domain}"
    uri = "/paapi5/getitems"

    payload_dict = {
        "ItemIds": [asin],
        "Resources": [
            "ItemInfo.Title",
            "Offers.Listings.Price",
            "Offers.Listings.MerchantInfo",
            "Offers.Listings.Condition",
            "Offers.Listings.IsBuyBoxWinner",
            "Offers.Listings.DeliveryInfo.IsPrimeEligible",
            "Offers.Summaries.LowestPrice",   # ملخص أقل سعر كلي
            "Offers.Summaries.OfferCount",
        ],
        "PartnerTag": AFFILIATE_TAG,
        "PartnerType": "Associates",
        "Marketplace": f"www.{domain}",
    }
    payload_str = json.dumps(payload_dict)

    try:
        headers = sign_amazon_request(
            host, uri, payload_str, AMAZON_ACCESS_KEY, AMAZON_SECRET_KEY
        )
        response = requests.post(
            f"https://{host}{uri}", data=payload_str, headers=headers, timeout=12
        )

        if response.status_code != 200:
            logger.error("PA API returned %s: %s", response.status_code, response.text)
            return None

        res_data = response.json()
        items = res_data.get("ItemsResult", {}).get("Items", [])
        if not items:
            return None

        item = items[0]
        title = item.get("ItemInfo", {}).get("Title", {}).get("DisplayValue")

        # --- البحث عن أرخص بائع جديد من قائمة الـ Listings ---
        listings = item.get("Offers", {}).get("Listings", [])

        # فلترة العروض: جديدة فقط وعندها سعر
        new_listings = [
            l for l in listings
            if l.get("Price", {}).get("Amount")
            and "used" not in (l.get("Condition", {}).get("Value", "")).lower()
        ]

        if new_listings:
            # ترتيب تصاعدي حسب السعر → أول واحد هو الأرخص
            cheapest = min(new_listings, key=lambda x: x["Price"]["Amount"])
            seller_name = cheapest.get("MerchantInfo", {}).get("Name", "أمازون")
            condition   = cheapest.get("Condition", {}).get("DisplayValue", "جديد")
            is_prime    = cheapest.get("DeliveryInfo", {}).get("IsPrimeEligible", False)
            display_price = cheapest["Price"]["DisplayAmount"]
            currency      = cheapest["Price"]["Currency"]
            offer_count   = (
                item.get("Offers", {})
                .get("Summaries", [{}])[0]
                .get("OfferCount", 1)
            )
        else:
            # احتياطي: ملخص أقل سعر من Summaries
            summaries = item.get("Offers", {}).get("Summaries", [])
            lowest_summary = next(
                (s for s in summaries if s.get("Condition", {}).get("Value") == "New"),
                summaries[0] if summaries else None,
            )
            if not lowest_summary or not lowest_summary.get("LowestPrice"):
                return None
            lp = lowest_summary["LowestPrice"]
            display_price = lp.get("DisplayAmount", "—")
            currency      = lp.get("Currency", "SAR")
            seller_name   = "أمازون"
            condition     = "جديد"
            is_prime      = False
            offer_count   = lowest_summary.get("OfferCount", 1)

        return {
            "asin": asin,
            "title": title,
            "price": display_price,
            "currency": currency,
            "seller_name": seller_name,
            "condition": condition,
            "is_prime": is_prime,
            "offer_count": offer_count,
            "affiliate_link": build_affiliate_link(asin, domain=domain),
        }
    except Exception as e:
        logger.error("Error fetching PA API price for %s: %s", asin, e)
        return None


def format_offer_message(offer: dict) -> str:
    """يبني رسالة جاهزة للإرسال في تيليجرام تعرض أرخص بائع متاح."""
    if not offer:
        return (
            "❌ ما قدرت ألقى عروض متاحة لهذا المنتج حاليًا.\n"
            "تأكد من توفر المنتج في المتجر أو جرّب لاحقًا."
        )

    title_part = f"📦 *{offer['title'][:70]}*\n\n" if offer.get("title") else ""

    prime_badge = " 🔵 Prime" if offer.get("is_prime") else ""
    offer_count = offer.get("offer_count")
    sellers_note = (
        f"_(من بين {offer_count} بائع متاح)_\n" if offer_count and offer_count > 1 else ""
    )

    return (
        f"{title_part}"
        f"🏷️ *أرخص سعر متاح الآن:*\n"
        f"• السعر: `{offer['price']}`\n"
        f"• البائع: {offer['seller_name']}{prime_badge}\n"
        f"• الحالة: {offer['condition']}\n"
        f"{sellers_note}\n"
        f"🛒 *رابط الشراء:*\n{offer['affiliate_link']}\n\n"
        f"_(رابط تسويق بالعمولة)_"
    )
