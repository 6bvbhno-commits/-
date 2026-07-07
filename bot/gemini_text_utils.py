"""
Gemini (Google) — نصوص فقط (بدون صور).
يستخدم GEMINI_API_KEY الموجود مسبقاً.
نموذج: gemini-2.0-flash — سريع ورخيص وعربي جيد.
"""
import logging
import os
import requests

logger = logging.getLogger(__name__)

_MODEL   = "gemini-2.0-flash"
_BASE    = "https://generativelanguage.googleapis.com/v1beta/models"
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


def _call(prompt: str, system: str | None = None, max_tokens: int = 300) -> str | None:
    api_key = os.getenv("GEMINI_API_KEY", "")
    if not api_key:
        return None

    parts = []
    if system:
        parts.append({"text": f"[تعليمات النظام]\n{system}\n\n[رسالة المستخدم]\n{prompt}"})
    else:
        parts.append({"text": prompt})

    payload = {
        "contents": [{"parts": parts}],
        "generationConfig": {"maxOutputTokens": max_tokens, "temperature": 0.3},
    }
    try:
        resp = requests.post(
            f"{_BASE}/{_MODEL}:generateContent?key={api_key}",
            json=payload,
            timeout=_TIMEOUT,
        )
        if resp.status_code == 429:
            logger.warning("Gemini text: rate limit 429")
            return None
        if resp.status_code != 200:
            logger.warning("Gemini text HTTP %s", resp.status_code)
            return None
        text = (
            resp.json()
            .get("candidates", [{}])[0]
            .get("content", {})
            .get("parts", [{}])[0]
            .get("text", "")
            .strip()
        )
        if text:
            logger.info("Gemini text نجح (%d حرف)", len(text))
        return text or None
    except requests.Timeout:
        logger.warning("Gemini text: timeout")
        return None
    except Exception as e:
        logger.error("Gemini text exception: %s", e)
        return None


def extract_product_intent(text: str) -> str | None:
    """يُطلق RuntimeError عند فشل الـ provider، يُعيد None للـ semantic NONE."""
    result = _call(text[:500], system=_SYSTEM_EXTRACT, max_tokens=60)
    if result is None:
        raise RuntimeError("Gemini provider failure")
    if result.upper().startswith("NONE"):
        return None
    return result if len(result) <= 100 else None


def chat_response(text: str, history: list[dict]) -> str | None:
    """يُعيد None عند فشل الـ provider."""
    history_text = ""
    for m in history[-6:]:
        role = "مستخدم" if m["role"] == "user" else "بوت"
        history_text += f"{role}: {m['content'][:200]}\n"
    prompt = f"{history_text}مستخدم: {text[:600]}\nبوت:"
    result = _call(prompt, system=_SYSTEM_CHAT, max_tokens=200)
    return result or None


def price_advice(current_price: float, history_records: list[dict]) -> str:
    if len(history_records) < 3:
        return ""
    prices = [r["price_val"] for r in history_records]
    lo, hi  = min(prices), max(prices)
    avg     = sum(prices) / len(prices)
    days    = max(1, (history_records[-1]["ts"] - history_records[0]["ts"]) // 86400)
    pct     = (current_price - lo) / (hi - lo) * 100 if hi != lo else 50

    prompt = (
        f"بيانات سعر منتج أمازون ({days} يوم):\n"
        f"الحالي: {current_price:.2f} | الأدنى: {lo:.2f} | الأعلى: {hi:.2f} | المتوسط: {avg:.2f}\n"
        f"السعر عند {pct:.0f}% من النطاق (0=أدنى، 100=أعلى)\n"
        f"أجب بسطر واحد يبدأ بـ 💡 (10 كلمات أو أقل، بدون أرقام)."
    )
    result = _call(prompt, max_tokens=60)
    if not result:
        return ""
    advice = result.strip().split("\n")[0]
    if not advice.startswith("💡"):
        advice = "💡 " + advice
    return advice[:130]
