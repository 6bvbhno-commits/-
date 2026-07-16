"""
SerpAPI — واجهة بيانات أمازون الجاهزة.
تستخدم engine=amazon_product للأسعار و engine=amazon_search للبحث بالكلمات.

المتطلبات (Replit Secret):
  SERPAPI_KEY — مفتاح API من serpapi.com
"""

import logging
import re
from urllib.parse import urlencode, urlparse, parse_qs, urljoin

import requests

from amazon_utils import build_affiliate_link, build_affiliate_search_link, tag_amazon_url
from config import AFFILIATE_TAG, AMAZON_DOMAIN, SERPAPI_KEY

logger = logging.getLogger(__name__)

_BASE    = "https://serpapi.com/search.json"
_TIMEOUT = 15

# خريطة الأرقام العربية (نفسها في amazon_utils)
_AR_NUM_MAP = str.maketrans("٠١٢٣٤٥٦٧٨٩٫٬،", "0123456789.,,")

# عملة كل domain
_DOMAIN_CURRENCY = {
    "amazon.sa": "SAR",
    "amazon.ae": "AED",
    "amazon.eg": "EGP",
    "amazon.com": "USD",
    "amazon.co.uk": "GBP",
    "amazon.de": "EUR",
    "amazon.fr": "EUR",
}


def serpapi_available() -> bool:
    return bool(SERPAPI_KEY)


# ─── مساعدات ──────────────────────────────────────────────────────────────────

def _currency(domain: str) -> str:
    return _DOMAIN_CURRENCY.get(domain, "SAR")


def _parse_price(raw) -> tuple[float | None, str | None]:
    """
    يحوّل السعر إلى (float_val, نص_مرتّب).
    يدعم: رقم Python، نص إنجليزي، نص عربي، أو dict من SerpAPI.
    """
    if raw is None:
        return None, None

    if isinstance(raw, dict):
        val = raw.get("value")
        if isinstance(val, (int, float)):
            text = (raw.get("raw") or raw.get("extracted_price") or "").strip() or None
            return float(val), text
        raw = raw.get("raw") or raw.get("extracted_price") or raw.get("price")
        if raw is None:
            return None, None

    if isinstance(raw, (int, float)):
        return float(raw), None

    raw_str = str(raw).translate(_AR_NUM_MAP)
    raw_str = raw_str.replace(",", "")
    num_str = re.sub(r"[^\d.]", "", raw_str)

    try:
        return float(num_str), str(raw).strip()
    except ValueError:
        return None, None


def _extract_image(product: dict) -> str:
    """أفضل رابط صورة من نتيجة SerpAPI."""
    for key in ("thumbnail", "main_image", "image"):
        val = product.get(key)
        if isinstance(val, str) and val.startswith("http"):
            return val
        if isinstance(val, dict):
            for k in ("link", "url", "src"):
                u = val.get(k)
                if isinstance(u, str) and u.startswith("http"):
                    return u

    images = product.get("images") or product.get("media_images") or []
    if isinstance(images, dict):
        images = images.get("images") or images.get("all_images") or []
    if isinstance(images, list):
        for item in images:
            if isinstance(item, str) and item.startswith("http"):
                return item
            if isinstance(item, dict):
                for k in ("link", "url", "src", "hi_res", "large"):
                    u = item.get(k)
                    if isinstance(u, str) and u.startswith("http"):
                        return u
    return ""


def _affiliate_link(asin: str | None, raw_link: str, domain: str) -> str:
    """يبني رابط أفلييت بصيغة Associates الرسمية."""
    if asin:
        return build_affiliate_link(asin, domain)
    if raw_link:
        return tag_amazon_url(raw_link, domain)
    return build_affiliate_search_link("", domain)


def _search_fallback_by_asin(asin: str, domain: str) -> dict | None:
    """بحث SerpAPI بالـ ASIN كاحتياط لاسم/صورة/سعر."""
    try:
        results = search_items(asin, domain=domain, max_results=5)
    except Exception as exc:
        logger.warning("SerpAPI search fallback: %s", exc)
        return None
    for item in results:
        link = item.get("link") or ""
        if asin.upper() in link.upper() or item.get("asin", "").upper() == asin.upper():
            return item
    return results[0] if results else None


# ─── جلب بيانات منتج بـ ASIN ──────────────────────────────────────────────────

def get_item_by_asin(asin: str, domain: str = AMAZON_DOMAIN) -> dict | None:
    """
    يجلب السعر وبيانات المنتج عبر SerpAPI (amazon_product engine).
    يرجع dict بنفس شكل نتيجة الكشط، أو None عند الخطأ.
    مهم: يرجع الاسم/الصورة حتى لو السعر غير متوفر.
    """
    if not serpapi_available():
        return None

    currency = _currency(domain)
    aff_link = _affiliate_link(asin, "", domain)

    try:
        resp = requests.get(
            _BASE,
            params={
                "engine":        "amazon_product",
                "asin":          asin,
                "amazon_domain": domain,
                "api_key":       SERPAPI_KEY,
            },
            timeout=_TIMEOUT,
        )

        if resp.status_code == 401:
            logger.error("SerpAPI: مفتاح غير صالح (401)")
            return None
        if resp.status_code == 429:
            logger.warning("SerpAPI: تجاوز الحصة (429)")
            return None
        if resp.status_code != 200:
            logger.error("SerpAPI product: HTTP %s", resp.status_code)
            fb = _search_fallback_by_asin(asin, domain)
            if not fb:
                return None
            return {
                "asin": asin,
                "title": fb.get("title"),
                "price": fb.get("price"),
                "price_val": fb.get("price_val"),
                "currency": currency,
                "seller_name": fb.get("seller_name") or "Amazon.sa",
                "affiliate_link": aff_link,
                "image": fb.get("image") or "",
                "blocked": not bool(fb.get("price_val")),
            }

        data = resp.json()
        if "error" in data:
            logger.error("SerpAPI product error: %s", data["error"])
            fb = _search_fallback_by_asin(asin, domain)
            if not fb:
                return None
            return {
                "asin": asin,
                "title": fb.get("title"),
                "price": fb.get("price"),
                "price_val": fb.get("price_val"),
                "currency": currency,
                "seller_name": fb.get("seller_name") or "Amazon.sa",
                "affiliate_link": aff_link,
                "image": fb.get("image") or "",
                "blocked": not bool(fb.get("price_val")),
            }

        product = data.get("product_results") or {}
        title = (product.get("title") or "").strip()
        image = _extract_image(product)

        # ── أرخص سعر من قائمة البائعين ──────────────────────────────────────
        sellers     = data.get("sellers_results", {}).get("online_sellers", []) or []
        best_val    = None
        best_text   = None
        best_seller = ""
        is_prime    = False
        offer_count = len(sellers) if sellers else 1

        for s in sellers:
            val, text = _parse_price(s.get("price"))
            if val is None:
                continue
            if best_val is None or val < best_val:
                best_val    = val
                best_text   = text or f"{val:.2f} {currency}"
                best_seller = (s.get("name") or "").strip()
                is_prime    = bool(s.get("prime"))

        # fallback: السعر الرئيسي من product_results
        if best_val is None:
            best_val, best_text = _parse_price(product.get("price"))
            if best_val is None:
                best_val, best_text = _parse_price(product.get("extracted_price"))

        if best_text and best_val is not None and currency not in best_text:
            best_text = f"{best_val:.2f} {currency}"

        # إذا ما فيه اسم/صورة — جرّب البحث بالـ ASIN
        if not title or not image:
            fb = _search_fallback_by_asin(asin, domain)
            if fb:
                title = title or (fb.get("title") or "").strip()
                image = image or (fb.get("image") or "")
                if best_val is None and fb.get("price_val"):
                    best_val = fb.get("price_val")
                    best_text = fb.get("price")

        if not title and not image and best_val is None:
            logger.info("SerpAPI: لا بيانات مفيدة للـ ASIN %s", asin)
            return None

        desc_parts: list[str] = []
        for key in ("feature_bullets", "about_this_item", "description"):
            val = product.get(key)
            if isinstance(val, list):
                desc_parts.extend(str(x) for x in val[:5])
            elif isinstance(val, str) and val.strip():
                desc_parts.append(val.strip())
        description = " • ".join(desc_parts)[:500] if desc_parts else ""

        result = {
            "asin":           asin,
            "title":          title or None,
            "description":    description,
            "price":          best_text,
            "price_val":      best_val,
            "currency":       currency,
            "seller_name":    best_seller or "Amazon.sa",
            "condition":      "جديد",
            "is_prime":       is_prime,
            "offer_count":    offer_count,
            "affiliate_link": aff_link,
            "image":          image,
        }
        if best_val is None:
            result["blocked"] = True
            logger.info("SerpAPI: اسم/صورة بدون سعر للـ ASIN %s", asin)
        return result

    except Exception as exc:
        logger.error("SerpAPI get_item_by_asin: %s", exc)
        return None


# ─── بحث بالكلمات المفتاحية ───────────────────────────────────────────────────

def search_items(keywords: str, domain: str = AMAZON_DOMAIN, max_results: int = 5) -> list[dict]:
    """
    يبحث في أمازون بالكلمات عبر SerpAPI (amazon_search engine).
    يرجع قائمة من dicts {title, price, link} جاهزة للعرض في تيليجرام.
    """
    if not serpapi_available():
        return []

    currency = _currency(domain)

    try:
        resp = requests.get(
            _BASE,
            params={
                "engine":        "amazon",
                "k":             keywords,
                "amazon_domain": domain,
                "api_key":       SERPAPI_KEY,
            },
            timeout=_TIMEOUT,
        )

        if resp.status_code == 401:
            logger.error("SerpAPI: مفتاح غير صالح (401)")
            return []
        if resp.status_code == 429:
            logger.warning("SerpAPI: تجاوز الحصة (429)")
            return []
        if resp.status_code != 200:
            logger.error("SerpAPI search: HTTP %s", resp.status_code)
            return []

        data = resp.json()
        if "error" in data:
            logger.error("SerpAPI search error: %s", data["error"])
            return []

        organic = data.get("organic_results", [])[:max_results]
        results = []

        for item in organic:
            asin  = item.get("asin") or ""
            title = item.get("title", "")
            if not title:
                continue

            raw_link = item.get("link", "")
            link     = _affiliate_link(asin or None, raw_link, domain)

            val, text = _parse_price(
                item.get("price") or item.get("extracted_price")
            )
            if val is not None and text and currency not in text:
                text = f"{val:.2f} {currency}"
            price_text = text or "غير محدد"

            results.append({
                "asin": asin,
                "title": title[:120],
                "price": price_text,
                "price_val": val,
                "seller_name": (item.get("seller") or item.get("merchant") or "").strip() or "Amazon.sa",
                "link":  link,
                "image": _extract_image(item) or (item.get("thumbnail") or ""),
            })

        return results

    except Exception as exc:
        logger.error("SerpAPI search_items: %s", exc)
        return []
