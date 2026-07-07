"""
Amazon PA API v5 — الواجهة الرسمية لأسعار أمازون.
تستخدم AWS Signature V4 للتوقيع، بدون أي SDK خارجي.
تدعم: GetItems (بـ ASIN) و SearchItems (بالكلمات المفتاحية).

المتطلبات (Replit Secrets):
  AMAZON_ACCESS_KEY  — Access Key ID
  AMAZON_SECRET_KEY  — Secret Access Key
"""

import hashlib
import hmac
import json
import logging
from datetime import datetime, timezone

import requests

from config import AFFILIATE_TAG, AMAZON_ACCESS_KEY, AMAZON_SECRET_KEY, AMAZON_DOMAIN

logger = logging.getLogger(__name__)

# ─── إعدادات المنطقة السعودية ────────────────────────────────────────────────
_HOST         = "webservices.amazon.sa"
_REGION       = "eu-west-1"
_SERVICE      = "ProductAdvertisingAPI"
_PARTNER_TYPE = "Associates"
_MARKETPLACE  = "www.amazon.sa"
_LANG         = "ar_SA"

# الموارد المطلوبة من كل طلب
_RESOURCES = [
    "Images.Primary.Medium",
    "ItemInfo.Title",
    "Offers.Listings.Price",
    "Offers.Listings.DeliveryInfo.IsPrimeEligible",
    "Offers.Listings.MerchantInfo",
    "Offers.Summaries.OfferCount",
]

# خريطة path → اسم العملية الصحيح (CamelCase) المطلوب في X-Amz-Target
_OP_NAMES = {
    "getitems":    "GetItems",
    "searchitems": "SearchItems",
}


# ─── AWS Signature V4 ─────────────────────────────────────────────────────────

def _hmac_sha256(key: bytes, msg: str) -> bytes:
    return hmac.new(key, msg.encode("utf-8"), hashlib.sha256).digest()


def _signing_key(secret: str, date_str: str) -> bytes:
    k = _hmac_sha256(f"AWS4{secret}".encode("utf-8"), date_str)
    k = _hmac_sha256(k, _REGION)
    k = _hmac_sha256(k, _SERVICE)
    k = _hmac_sha256(k, "aws4_request")
    return k


def _sign_request(path: str, payload: dict) -> dict:
    """
    يبني ترويسات HTTP الموقّعة بـ AWS SigV4.
    path مثال: "/paapi5/getitems"
    """
    op_slug   = path.split("/")[-1]          # "getitems"
    op_name   = _OP_NAMES.get(op_slug, op_slug)  # "GetItems"
    target    = f"com.amazon.paapi5.v1.ProductAdvertisingAPIv1.{op_name}"

    now       = datetime.now(timezone.utc)
    amz_date  = now.strftime("%Y%m%dT%H%M%SZ")
    date_str  = now.strftime("%Y%m%d")

    body      = json.dumps(payload, ensure_ascii=False, separators=(",", ":"))
    body_hash = hashlib.sha256(body.encode("utf-8")).hexdigest()

    # --- Canonical Request ---
    canonical_headers = (
        f"content-encoding:amz-1.0\n"
        f"content-type:application/json; charset=utf-8\n"
        f"host:{_HOST}\n"
        f"x-amz-date:{amz_date}\n"
        f"x-amz-target:{target}\n"
    )
    signed_headers = "content-encoding;content-type;host;x-amz-date;x-amz-target"

    canonical_request = "\n".join([
        "POST",
        path,
        "",               # query string فارغ
        canonical_headers,
        signed_headers,
        body_hash,
    ])

    # --- String to Sign ---
    credential_scope = f"{date_str}/{_REGION}/{_SERVICE}/aws4_request"
    string_to_sign   = "\n".join([
        "AWS4-HMAC-SHA256",
        amz_date,
        credential_scope,
        hashlib.sha256(canonical_request.encode("utf-8")).hexdigest(),
    ])

    # --- Signature ---
    sig_key   = _signing_key(AMAZON_SECRET_KEY, date_str)
    signature = hmac.new(sig_key, string_to_sign.encode("utf-8"), hashlib.sha256).hexdigest()

    auth = (
        f"AWS4-HMAC-SHA256 "
        f"Credential={AMAZON_ACCESS_KEY}/{credential_scope}, "
        f"SignedHeaders={signed_headers}, "
        f"Signature={signature}"
    )

    return {
        "Content-Encoding": "amz-1.0",
        "Content-Type":     "application/json; charset=utf-8",
        "Host":             _HOST,
        "X-Amz-Date":       amz_date,
        "X-Amz-Target":     target,
        "Authorization":    auth,
    }


# ─── مساعدات استخراج البيانات ─────────────────────────────────────────────────

def _parse_item(item: dict, asin: str) -> dict | None:
    """يحوّل عنصر PA API إلى نفس شكل نتيجة الكشط."""
    aff_link = f"https://www.{AMAZON_DOMAIN}/dp/{asin}?tag={AFFILIATE_TAG}"

    title = (
        item.get("ItemInfo", {})
            .get("Title", {})
            .get("DisplayValue")
    )

    listings    = item.get("Offers", {}).get("Listings", [])
    summaries   = item.get("Offers", {}).get("Summaries", [])
    offer_count = summaries[0].get("OfferCount", 1) if summaries else 1

    if not listings:
        return None

    best = min(listings,
               key=lambda x: x.get("Price", {}).get("Amount", float("inf")))

    price_info  = best.get("Price", {})
    price_val   = price_info.get("Amount")
    price_text  = price_info.get("FormattedPrice") or (
        f"{price_val:.2f} SAR" if price_val else None
    )
    is_prime    = best.get("DeliveryInfo", {}).get("IsPrimeEligible", False)
    seller_name = best.get("MerchantInfo", {}).get("Name", "Amazon.sa")

    if price_val is None:
        return None

    return {
        "asin":           asin,
        "title":          title,
        "price":          price_text,
        "price_val":      float(price_val),
        "currency":       "SAR",
        "seller_name":    seller_name,
        "condition":      "جديد",
        "is_prime":       is_prime,
        "offer_count":    offer_count,
        "affiliate_link": aff_link,
    }


def _post(path: str, payload: dict) -> dict | None:
    """يرسل طلب POST موقّع ويرجع الـ JSON أو None عند الخطأ."""
    try:
        headers = _sign_request(path, payload)
        url     = f"https://{_HOST}{path}"
        resp    = requests.post(
            url,
            headers=headers,
            data=json.dumps(payload, ensure_ascii=False, separators=(",", ":")),
            timeout=15,
        )
        if resp.status_code == 429:
            logger.warning("PA API: rate limit (429) على %s", path)
            return None
        if resp.status_code != 200:
            logger.error("PA API %s: HTTP %s — %s", path, resp.status_code, resp.text[:400])
            return None
        return resp.json()
    except Exception as exc:
        logger.error("PA API %s: %s", path, exc)
        return None


# ─── الدوال العامة ────────────────────────────────────────────────────────────

def paapi_available() -> bool:
    """هل مفاتيح PA API موجودة؟"""
    return bool(AMAZON_ACCESS_KEY and AMAZON_SECRET_KEY)


def get_item_by_asin(asin: str) -> dict | None:
    """
    يجلب أرخص سعر لمنتج بـ ASIN عبر PA API.
    يرجع None إذا فشل أو ما وُجدت مفاتيح.
    """
    if not paapi_available():
        return None

    path    = "/paapi5/getitems"
    payload = {
        "PartnerTag":             AFFILIATE_TAG,
        "PartnerType":            _PARTNER_TYPE,
        "Marketplace":            _MARKETPLACE,
        "ItemIds":                [asin],
        "Resources":              _RESOURCES,
        "LanguagesOfPreference":  [_LANG],
    }

    data  = _post(path, payload)
    items = (data or {}).get("ItemsResult", {}).get("Items", [])
    if not items:
        logger.info("PA API GetItems: لا نتائج للـ ASIN %s", asin)
        return None

    return _parse_item(items[0], asin)


def search_items(keywords: str, max_results: int = 5) -> list[dict]:
    """
    يبحث بالكلمات المفتاحية عبر PA API.
    يرجع قائمة فارغة إذا فشل.
    """
    if not paapi_available():
        return []

    path    = "/paapi5/searchitems"
    payload = {
        "PartnerTag":            AFFILIATE_TAG,
        "PartnerType":           _PARTNER_TYPE,
        "Marketplace":           _MARKETPLACE,
        "Keywords":              keywords,
        "SearchIndex":           "All",
        "ItemCount":             max_results,
        "Resources":             _RESOURCES,
        "LanguagesOfPreference": [_LANG],
    }

    data  = _post(path, payload)
    items = (data or {}).get("SearchResult", {}).get("Items", [])

    results = []
    for item in items:
        asin = item.get("ASIN")
        if asin:
            parsed = _parse_item(item, asin)
            if parsed:
                results.append(parsed)
    return results
