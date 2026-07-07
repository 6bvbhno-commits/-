"""
OpenAI GPT — نصوص فقط (بدون صور).
يستخدم Replit OpenAI Integration الموجود مسبقاً
(AI_INTEGRATIONS_OPENAI_BASE_URL + AI_INTEGRATIONS_OPENAI_API_KEY).
نموذج: gpt-4o-mini — سريع وعربي ممتاز.
"""
import logging
import os
import requests

logger = logging.getLogger(__name__)

_MODEL   = "gpt-4o-mini"
_TIMEOUT = 20

_SYSTEM_EXTRACT = """\
أنت نظام تصنيف نية الشراء. مهمتك محددة جداً:
إذا كان النص يصف منتجاً يريد المستخدم شراءه أو معرفة سعره:
  → أعد اسم المنتج باللغة الأنسب للبحث في أمازون (عربي أو إنجليزي).
  → اذكر الاسم فقط، بدون شرح أو جمل إضافية.
في كل الحالات الأخرى: → أعد الكلمة NONE فقط.
أمثلة: "أبي سماعات بلوتوث" → سماعات بلوتوث | "شكراً" → NONE\
"""

_SYSTEM_CHAT = """\
أنت مساعد تسوق ذكي متخصص في أمازون السعودية، اسمك "بوت الأسعار".
الردود قصيرة (3 أسطر أو أقل)، بالعربية السعودية العامية.
لا تختلق أسعاراً — البوت يجلب السعر الحقيقي.
لا تذكر منافسين لأمازون.\
"""


def _get_endpoint() -> tuple[str, str] | None:
    base = os.getenv("AI_INTEGRATIONS_OPENAI_BASE_URL", "").rstrip("/")
    key  = os.getenv("AI_INTEGRATIONS_OPENAI_API_KEY", "")
    if not base or not key:
        return None
    return f"{base}/chat/completions", key


def _call(messages: list[dict], max_tokens: int = 300) -> str | None:
    ep = _get_endpoint()
    if not ep:
        logger.warning("OpenAI text: مفاتيح غير موجودة")
        return None
    url, key = ep
    try:
        resp = requests.post(
            url,
            headers={"Authorization": f"Bearer {key}", "Content-Type": "application/json"},
            json={"model": _MODEL, "messages": messages, "max_tokens": max_tokens},
            timeout=_TIMEOUT,
        )
        if resp.status_code == 429:
            logger.warning("OpenAI text: rate limit 429")
            return None
        if resp.status_code != 200:
            logger.warning("OpenAI text HTTP %s: %s", resp.status_code, resp.text[:100])
            return None
        text = (
            resp.json()
            .get("choices", [{}])[0]
            .get("message", {})
            .get("content", "")
            .strip()
        )
        if text:
            logger.info("OpenAI text نجح (%d حرف)", len(text))
        return text or None
    except requests.Timeout:
        logger.warning("OpenAI text: timeout")
        return None
    except Exception as e:
        logger.error("OpenAI text exception: %s", e)
        return None


def extract_product_intent(text: str) -> str | None:
    """يُطلق RuntimeError عند فشل الـ provider، يُعيد None للـ semantic NONE."""
    msgs = [
        {"role": "system",  "content": _SYSTEM_EXTRACT},
        {"role": "user",    "content": text[:500]},
    ]
    result = _call(msgs, max_tokens=60)
    if result is None:
        raise RuntimeError("OpenAI provider failure")
    if result.upper().startswith("NONE"):
        return None
    return result if len(result) <= 100 else None


def chat_response(text: str, history: list[dict]) -> str | None:
    """يُعيد None عند فشل الـ provider."""
    msgs = [{"role": "system", "content": _SYSTEM_CHAT}]
    for m in history[-8:]:
        msgs.append({"role": m["role"], "content": m["content"][:300]})
    msgs.append({"role": "user", "content": text[:700]})
    result = _call(msgs, max_tokens=220)
    return result or None


def price_advice(current_price: float, history_records: list[dict]) -> str:
    if len(history_records) < 3:
        return ""
    prices = [r["price_val"] for r in history_records]
    lo, hi  = min(prices), max(prices)
    avg     = sum(prices) / len(prices)
    days    = max(1, (history_records[-1]["ts"] - history_records[0]["ts"]) // 86400)
    pct     = (current_price - lo) / (hi - lo) * 100 if hi != lo else 50

    msgs = [{"role": "user", "content": (
        f"بيانات سعر منتج أمازون ({days} يوم):\n"
        f"الحالي: {current_price:.2f} | الأدنى: {lo:.2f} | الأعلى: {hi:.2f} | المتوسط: {avg:.2f}\n"
        f"السعر عند {pct:.0f}% من النطاق (0=أدنى، 100=أعلى)\n"
        f"أجب بسطر واحد يبدأ بـ 💡 (10 كلمات أو أقل، بدون أرقام)."
    )}]
    result = _call(msgs, max_tokens=60)
    if not result:
        return ""
    advice = result.strip().split("\n")[0]
    if not advice.startswith("💡"):
        advice = "💡 " + advice
    return advice[:130]
