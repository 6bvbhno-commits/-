"""
Amazon PA API v5 — الواجهة الرسمية لأسعار أمازون.

يدعم طريقتَي توثيق:
  1. LWA (Login with Amazon) OAuth2 — التنسيق الجديد
     المفاتيح: AMAZON_LWA_CLIENT_ID + AMAZON_LWA_CLIENT_SECRET
  2. AWS Signature V4 — التنسيق القديم (احتياطي)
     المفاتيح: AMAZON_ACCESS_KEY + AMAZON_SECRET_KEY
"""

import hashlib
import hmac
import json
import logging
import threading
import time
from datetime import datetime, timezone

import requests

from amazon_utils import build_affiliate_link
from config import (
    AFFILIATE_TAG,
    AMAZON_DOMAIN,
    AMAZON_ACCESS_KEY,
    AMAZON_SECRET_KEY,
    AMAZON_LWA_CLIENT_ID,
    AMAZON_LWA_CLIENT_SECRET,
)

logger = logging.getLogger(__name__)

# ─── إعدادات المنطقة السعودية ────────────────────────────────────────────────
_HOST         = "webservices.amazon.sa"
_REGION       = "eu-west-1"
_SERVICE      = "ProductAdvertisingAPI"
_PARTNER_TYPE = "Associates"
_MARKETPLACE  = "www.amazon.sa"
_LANG         = "ar_SA"

_RESOURCES = [
    "Images.Primary.Medium",
    "ItemInfo.Title",
    "Offers.Listings.Price",
    "Offers.Listings.DeliveryInfo.IsPrimeEligible",
    "Offers.Listings.MerchantInfo",
    "Offers.Summaries.OfferCount",
]

# ─── LWA token cache ──────────────────────────────────────────────────────────
_lwa_token: str = ""
_lwa_expires_at: float = 0.0
_lwa_lock = threading.Lock()
_lwa_permanently_failed: bool = False   # True بعد أول 400 — نتخطى LWA نهائياً

LWA_TOKEN_URL = "https://api.amazon.com/auth/o2/token"


def _get_lwa_token() -> str | None:
    """
    يحصل على access_token من LWA ويُخزّنه حتى انتهاء صلاحيته.
    إذا فشل بـ 400 مرة واحدة، يُعطّل LWA نهائياً لهذه الجلسة.
    """
    global _lwa_token, _lwa_expires_at, _lwa_permanently_failed

    with _lwa_lock:
        if _lwa_permanently_failed:
            return None

        # إذا ما زال صالحاً (مع هامش 60 ثانية)
        if _lwa_token and time.time() < _lwa_expires_at - 60:
            return _lwa_token

        try:
            resp = requests.post(
                LWA_TOKEN_URL,
                data={
                    "grant_type":    "client_credentials",
                    "client_id":     AMAZON_LWA_CLIENT_ID,
                    "client_secret": AMAZON_LWA_CLIENT_SECRET,
                    "scope":         "ProductAdvertisingAPI",
                },
                timeout=10,
            )
            if resp.status_code == 400:
                # هذه المفاتيح ليست لـ PA API — عطّل LWA نهائياً
                logger.warning("LWA 400: هذه المفاتيح لا تدعم PA API — سيتم تعطيل LWA نهائياً")
                _lwa_permanently_failed = True
                return None
            if resp.status_code != 200:
                logger.error("LWA token error: HTTP %s", resp.status_code)
                return None

            data = resp.json()
            _lwa_token      = data["access_token"]
            expires_in      = int(data.get("expires_in", 3600))
            _lwa_expires_at = time.time() + expires_in
            logger.info("LWA token تم التحديث، صالح لـ %d ثانية", expires_in)
            return _lwa_token

        except Exception as exc:
            logger.error("LWA token exception: %s", exc)
            return None


# ─── AWS Signature V4 (التنسيق القديم) ───────────────────────────────────────

_OP_NAMES = {
    "getitems":    "GetItems",
    "searchitems": "SearchItems",
}


def _hmac_sha256(key: bytes, msg: str) -> bytes:
    return hmac.new(key, msg.encode("utf-8"), hashlib.sha256).digest()


def _signing_key(secret: str, date_str: str) -> bytes:
    k = _hmac_sha256(f"AWS4{secret}".encode("utf-8"), date_str)
    k = _hmac_sha256(k, _REGION)
    k = _hmac_sha256(k, _SERVICE)
    k = _hmac_sha256(k, "aws4_request")
    return k


def _sign_request_aws(path: str, payload: dict) -> dict:
    """يبني ترويسات AWS SigV4."""
    op_slug  = path.split("/")[-1]
    op_name  = _OP_NAMES.get(op_slug, op_slug)
    target   = f"com.amazon.paapi5.v1.ProductAdvertisingAPIv1.{op_name}"

    now      = datetime.now(timezone.utc)
    amz_date = now.strftime("%Y%m%dT%H%M%SZ")
    date_str = now.strftime("%Y%m%d")

    body      = json.dumps(payload, ensure_ascii=False, separators=(",", ":"))
    body_hash = hashlib.sha256(body.encode("utf-8")).hexdigest()

    canonical_headers = (
        f"content-encoding:amz-1.0\n"
        f"content-type:application/json; charset=utf-8\n"
        f"host:{_HOST}\n"
        f"x-amz-date:{amz_date}\n"
        f"x-amz-target:{target}\n"
    )
    signed_headers = "content-encoding;content-type;host;x-amz-date;x-amz-target"

    canonical_request = "\n".join([
        "POST", path, "",
        canonical_headers, signed_headers, body_hash,
    ])

    credential_scope = f"{date_str}/{_REGION}/{_SERVICE}/aws4_request"
    string_to_sign   = "\n".join([
        "AWS4-HMAC-SHA256", amz_date, credential_scope,
        hashlib.sha256(canonical_request.encode("utf-8")).hexdigest(),
    ])

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


# ─── إرسال الطلب (يختار التوثيق تلقائياً) ────────────────────────────────────

def _post(path: str, payload: dict) -> dict | None:
    """
    يرسل طلب POST بأفضل طريقة توثيق متاحة.
    الترتيب: LWA OAuth2 → AWS SigV4 (fallback دائم عند أي خطأ LWA)
    """
    body = json.dumps(payload, ensure_ascii=False, separators=(",", ":"))
    url  = f"https://{_HOST}{path}"

    # ── الأولوية: LWA OAuth2 ────────────────────────────────────────────────
    lwa_tried = False
    if AMAZON_LWA_CLIENT_ID and AMAZON_LWA_CLIENT_SECRET:
        lwa_tried = True
        token = _get_lwa_token()
        if token:
            op_slug = path.split("/")[-1]
            op_name = _OP_NAMES.get(op_slug, op_slug)
            target  = f"com.amazon.paapi5.v1.ProductAdvertisingAPIv1.{op_name}"
            headers = {
                "Authorization":    f"Bearer {token}",
                "Content-Encoding": "amz-1.0",
                "Content-Type":     "application/json; charset=utf-8",
                "Host":             _HOST,
                "X-Amz-Target":     target,
            }
            try:
                resp = requests.post(url, headers=headers, data=body, timeout=15)

                # Token منتهي — امسحه وجرب مرة واحدة بـ token جديد
                if resp.status_code == 401:
                    with _lwa_lock:
                        global _lwa_token, _lwa_expires_at, _lwa_permanently_failed
                        _lwa_token = ""
                        _lwa_expires_at = 0.0
                    new_token = _get_lwa_token()
                    if new_token:
                        headers["Authorization"] = f"Bearer {new_token}"
                        resp = requests.post(url, headers=headers, data=body, timeout=15)

                if resp.status_code == 429:
                    logger.warning("PA API LWA: rate limit (429)")
                    return None

                if resp.status_code == 200:
                    return resp.json()

                # أي خطأ آخر (401/403/400 scope خاطئ) → جرّب AWS SigV4
                logger.warning(
                    "PA API LWA: HTTP %s — %s — أنتقل لـ AWS SigV4 إذا متوفر",
                    resp.status_code, resp.text[:300],
                )
                # لا نعيد None هنا — نكمل للـ fallback أدناه

            except Exception as exc:
                logger.error("PA API LWA exception: %s — أنتقل لـ AWS SigV4", exc)
                # نكمل للـ fallback

    # ── Fallback: AWS SigV4 ─────────────────────────────────────────────────
    if AMAZON_ACCESS_KEY and AMAZON_SECRET_KEY:
        try:
            headers = _sign_request_aws(path, payload)
            resp    = requests.post(url, headers=headers, data=body, timeout=15)
            if resp.status_code == 429:
                logger.warning("PA API AWS: rate limit (429)")
                return None
            if resp.status_code != 200:
                logger.error("PA API AWS: HTTP %s — %s", resp.status_code, resp.text[:400])
                return None
            return resp.json()
        except Exception as exc:
            logger.error("PA API AWS exception: %s", exc)
            return None

    logger.warning("PA API: لا توجد مفاتيح توثيق")
    return None


# ─── مساعدات استخراج البيانات ─────────────────────────────────────────────────

def _parse_item(item: dict, asin: str) -> dict | None:
    """يحوّل عنصر PA API إلى نفس شكل نتيجة الكشط."""
    aff_link = build_affiliate_link(asin, AMAZON_DOMAIN)

    title = (
        item.get("ItemInfo", {})
            .get("Title", {})
            .get("DisplayValue")
    )

    listings    = item.get("Offers", {}).get("Listings", [])
    summaries   = item.get("Offers", {}).get("Summaries", [])
    offer_count = summaries[0].get("OfferCount", 1) if summaries else 1

    if not listings:
        logger.info("PA API: لا توجد listings للـ ASIN %s", asin)
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

    image_url = (
        item.get("Images", {})
            .get("Primary", {})
            .get("Medium", {})
            .get("URL", "")
    )

    return {
        "asin":           asin,
        "title":          title,
        "price":          price_text,
        "image":          image_url,
        "price_val":      float(price_val),
        "currency":       "SAR",
        "seller_name":    seller_name,
        "condition":      "جديد",
        "is_prime":       is_prime,
        "offer_count":    offer_count,
        "affiliate_link": aff_link,
    }


# ─── الدوال العامة ────────────────────────────────────────────────────────────

def paapi_available() -> bool:
    """هل مفاتيح PA API موجودة (LWA أو AWS)؟"""
    return bool(
        (AMAZON_LWA_CLIENT_ID and AMAZON_LWA_CLIENT_SECRET)
        or (AMAZON_ACCESS_KEY and AMAZON_SECRET_KEY)
    )


def get_item_by_asin(asin: str) -> dict | None:
    """يجلب أرخص سعر لمنتج بـ ASIN عبر PA API."""
    if not paapi_available():
        return None

    payload = {
        "PartnerTag":             AFFILIATE_TAG,
        "PartnerType":            _PARTNER_TYPE,
        "Marketplace":            _MARKETPLACE,
        "ItemIds":                [asin],
        "Resources":              _RESOURCES,
        "LanguagesOfPreference":  [_LANG],
    }

    data  = _post("/paapi5/getitems", payload)
    items = (data or {}).get("ItemsResult", {}).get("Items", [])
    if not items:
        logger.info("PA API GetItems: لا نتائج للـ ASIN %s", asin)
        return None

    return _parse_item(items[0], asin)


def search_items(keywords: str, max_results: int = 5) -> list[dict]:
    """يبحث بالكلمات المفتاحية عبر PA API."""
    if not paapi_available():
        return []

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

    data  = _post("/paapi5/searchitems", payload)
    items = (data or {}).get("SearchResult", {}).get("Items", [])

    results = []
    for item in items:
        asin = item.get("ASIN")
        if asin:
            parsed = _parse_item(item, asin)
            if parsed:
                results.append(parsed)
    return results
