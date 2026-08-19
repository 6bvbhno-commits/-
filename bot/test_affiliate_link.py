"""تحقق أن روابط العمولة تحمل تاق Associates الصحيح."""
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

from amazon_utils import build_affiliate_link, build_affiliate_search_link, tag_amazon_url
from config import AFFILIATE_TAG


def test_product_link_uses_associates_tag():
    url = build_affiliate_link("B0BRK8PR48", "amazon.sa")
    assert AFFILIATE_TAG == "rashedalhano-21"
    assert url.startswith("https://www.amazon.sa/dp/B0BRK8PR48?")
    assert "tag=rashedalhano-21" in url
    assert "linkCode=ll2" in url
    assert "ref_=as_li_ss_tl" in url
    assert "ref=nosim" not in url


def test_search_link_uses_associates_tag():
    url = build_affiliate_search_link("iphone", "amazon.sa")
    assert "tag=rashedalhano-21" in url
    assert "linkCode=ll2" in url
    assert url.startswith("https://www.amazon.sa/s?")


def test_existing_url_is_rewritten_to_our_tag():
    foreign = "https://www.amazon.sa/dp/B0BRK8PR48?tag=someone-else-21"
    url = tag_amazon_url(foreign, "amazon.sa")
    assert "tag=rashedalhano-21" in url
    assert "someone-else" not in url
    assert "linkCode=ll2" in url


if __name__ == "__main__":
    test_product_link_uses_associates_tag()
    test_search_link_uses_associates_tag()
    test_existing_url_is_rewritten_to_our_tag()
    print("OK", build_affiliate_link("B0BRK8PR48"))
