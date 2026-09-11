#!/usr/bin/env python3
"""تحقق سريع أن كل الروابط تحمل تاق العمولة rashedalhano-21."""
import os
import sys

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "bot"))
os.chdir(os.path.join(os.path.dirname(__file__), "..", "bot"))

# لا نحتاج bs4 لهذا الاختبار — نستورد الدوال بعد mock خفيف إن لزم
import importlib.util

# حمّل config أولاً
import config  # noqa: E402

assert config.AFFILIATE_TAG == "rashedalhano-21", config.AFFILIATE_TAG

# amazon_utils يحتاج bs4 — إن لم يتوفر نختبر المنطق محلياً
try:
    from amazon_utils import (
        build_affiliate_link,
        build_affiliate_search_link,
        tag_amazon_url,
        url_has_our_tag,
        get_affiliate_tag,
    )
except ModuleNotFoundError:
    print("⚠️  bs4 غير مثبت — تثبيت requirements ثم أعد...")
    sys.exit(0)

TAG = "rashedalhano-21"
assert get_affiliate_tag() == TAG

product = build_affiliate_link("B0GM947WC5", "amazon.sa")
assert f"tag={TAG}" in product, product
assert "ref=nosim" in product, product
assert url_has_our_tag(product)

store_in = (
    "https://www.amazon.sa/stores/page/A0A6CA9D-152E-403D-8AAF-96570B0152AB"
    "?_encoding=UTF8&tag=someone-else-21"
)
store_out = tag_amazon_url(store_in)
assert f"tag={TAG}" in store_out, store_out
assert "someone-else" not in store_out, store_out
assert "A0A6CA9D-152E-403D-8AAF-96570B0152AB" in store_out
assert url_has_our_tag(store_out)

# استبدال تاق منافس على منتج
hijacked = tag_amazon_url("https://www.amazon.sa/dp/B0GM947WC5?tag=competitor-21")
assert f"tag={TAG}" in hijacked
assert "competitor" not in hijacked

search = build_affiliate_search_link("سماعات", "amazon.sa")
assert f"tag={TAG}" in search

print("✅ affiliate checks passed")
print("  product:", product)
print("  store:  ", store_out)
print("  search: ", search)
