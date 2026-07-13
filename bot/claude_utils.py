"""
راوتر الذكاء الاصطناعي — أربع طبقات بالترتيب:

  🥇 DeepSeek-V3    — أسرع + أرخص + عربي ممتاز
  🥈 GPT-4o-mini    — OpenAI عبر Replit Integration
  🥉 Gemini-2.0     — Google عبر GEMINI_API_KEY
  🔁 Claude Haiku   — احتياط أخير (Replit Anthropic Integration)

كل طبقة تُجرَّب فقط إذا فشلت التي قبلها.
bot.py يستورد من هنا — لا يعرف أي provider اشتغل.
"""
import logging
import os
import time

logger = logging.getLogger(__name__)

# ══════════════════════════════════════════════════════════════════
#  Claude client (lazy init + backoff) — الطبقة الأخيرة
# ══════════════════════════════════════════════════════════════════

_client: object | None     = None
_client_error_until: float = 0.0

def _get_claude_client():
    global _client, _client_error_until
    if _client is not None:
        return _client
    if time.time() < _client_error_until:
        raise RuntimeError("Anthropic client في فترة backoff")
    try:
        from anthropic import Anthropic
        base_url = os.getenv("AI_INTEGRATIONS_ANTHROPIC_BASE_URL", "")
        api_key  = os.getenv("AI_INTEGRATIONS_ANTHROPIC_API_KEY", "dummy")
        kwargs: dict = {"api_key": api_key}
        if base_url:
            kwargs["base_url"] = base_url
        _client = Anthropic(**kwargs)
        return _client
    except Exception as e:
        _client_error_until = time.time() + 60
        logger.error("Anthropic client init فشل (backoff 60s): %s", e)
        raise


# ══════════════════════════════════════════════════════════════════
#  System prompts — مشتركة بين كل الـ providers
# ══════════════════════════════════════════════════════════════════

_SYSTEM_CHAT = """\
أنت مساعد تسوق ذكي متخصص في أمازون السعودية، اسمك "بوت الأسعار".
هدفك الأول: مساعدة المستخدم في إيجاد أفضل سعر لأي منتج.
قواعد الرد:
- الردود قصيرة ومباشرة (3 أسطر أو أقل).
- اردّ دائماً بالعربية السعودية العامية أو الفصحى الخفيفة.
- إذا ذكر المستخدم منتجاً → حثّه على إرسال *رابط أمازون* للحصول على السعر والصورة.
- ذكّره بميزة *نبّهني* عند نزول السعر إذا كان يتردد في الشراء.
- لا تختلق أسعاراً — البوت هو الذي يجلب السعر الحقيقي.
- لا تذكر منافسين لأمازون (نون، جرير، إلخ).\
"""

_SYSTEM_EXTRACT = """\
أنت نظام تصنيف نية الشراء. مهمتك محددة جداً:
إذا كان النص يصف منتجاً يريد المستخدم شراءه أو معرفة سعره:
  → أعد اسم المنتج باللغة الأنسب للبحث في أمازون (عربي أو إنجليزي).
  → اذكر الاسم فقط، بدون شرح أو جمل إضافية.
في كل الحالات الأخرى (تحية، شكر، سؤال عام):
  → أعد الكلمة NONE فقط.
أمثلة:
  "أبي سماعات بلوتوث" → سماعات بلوتوث
  "كم سعر آيفون 15؟"  → iPhone 15
  "شكراً جزيلاً"      → NONE
  "كيف الحال؟"        → NONE\
"""

_FALLBACK_CHAT = "أرسل لي 🔗 رابط منتج أمازون وأجيبك بصورة المنتج + أفضل سعر فوراً 🛒"


# ══════════════════════════════════════════════════════════════════
#  دالة مساعدة: تحقق صحة intent
# ══════════════════════════════════════════════════════════════════

def _valid_intent(result: str | None) -> str | None:
    if not result or result.upper().startswith("NONE"):
        return None
    return result if len(result) <= 100 else None


# ══════════════════════════════════════════════════════════════════
#  1. extract_product_intent
#     DeepSeek → GPT → Gemini → Claude
# ══════════════════════════════════════════════════════════════════

def extract_product_intent(text: str) -> str | None:
    """
    قاعدة الـ fallback:
      - RuntimeError من provider = فشل تقني → جرّب التالي
      - None من provider          = لا نية شراء (semantic) → أوقف مباشرة
    """
    # ── 🥇 DeepSeek ──────────────────────────────────────────────
    try:
        from deepseek_utils import extract_product_intent as _fn
        return _fn(text)   # يُعيد str | None | يُطلق RuntimeError
    except RuntimeError:
        logger.warning("DeepSeek intent فشل → GPT")
    except Exception as e:
        logger.warning("DeepSeek intent خطأ غير متوقع → GPT: %s", e)

    # ── 🥈 GPT-4o-mini ───────────────────────────────────────────
    try:
        from openai_text_utils import extract_product_intent as _fn
        return _fn(text)
    except RuntimeError:
        logger.warning("GPT intent فشل → Gemini")
    except Exception as e:
        logger.warning("GPT intent خطأ → Gemini: %s", e)

    # ── 🥉 Gemini ────────────────────────────────────────────────
    try:
        from gemini_text_utils import extract_product_intent as _fn
        return _fn(text)
    except RuntimeError:
        logger.warning("Gemini intent فشل → Claude")
    except Exception as e:
        logger.warning("Gemini intent خطأ → Claude: %s", e)

    # ── 🔁 Claude Haiku ──────────────────────────────────────────
    try:
        client = _get_claude_client()
        msg    = client.messages.create(
            model="claude-haiku-4-5", max_tokens=60,
            system=_SYSTEM_EXTRACT,
            messages=[{"role": "user", "content": text[:500]}],
        )
        return _valid_intent(msg.content[0].text.strip())
    except Exception as e:
        logger.warning("Claude intent فشل: %s", e)
        return None


# ══════════════════════════════════════════════════════════════════
#  2. chat_response
#     DeepSeek → GPT → Gemini → Claude
# ══════════════════════════════════════════════════════════════════

def chat_response(text: str, history: list[dict]) -> str:
    """
    كل provider يُعيد str (نجاح) أو None (فشل) — لا strings خاصة للفشل.
    """
    # ── 🥇 DeepSeek ──────────────────────────────────────────────
    try:
        from deepseek_utils import chat_response as _fn
        result = _fn(text, history)
        if result is not None:
            return result
        logger.warning("DeepSeek chat أعاد None → GPT")
    except Exception as e:
        logger.warning("DeepSeek chat فشل → GPT: %s", e)

    # ── 🥈 GPT-4o-mini ───────────────────────────────────────────
    try:
        from openai_text_utils import chat_response as _fn
        result = _fn(text, history)
        if result is not None:
            return result
        logger.warning("GPT chat أعاد None → Gemini")
    except Exception as e:
        logger.warning("GPT chat فشل → Gemini: %s", e)

    # ── 🥉 Gemini ────────────────────────────────────────────────
    try:
        from gemini_text_utils import chat_response as _fn
        result = _fn(text, history)
        if result is not None:
            return result
        logger.warning("Gemini chat أعاد None → Claude")
    except Exception as e:
        logger.warning("Gemini chat فشل → Claude: %s", e)

    # ── 🔁 Claude Haiku ──────────────────────────────────────────
    try:
        client   = _get_claude_client()
        messages = list(history[-8:]) + [{"role": "user", "content": text[:800]}]
        msg = client.messages.create(
            model="claude-haiku-4-5", max_tokens=250,
            system=_SYSTEM_CHAT, messages=messages,
        )
        return msg.content[0].text.strip()
    except Exception as e:
        logger.warning("Claude chat فشل: %s", e)
        return _FALLBACK_CHAT


# ══════════════════════════════════════════════════════════════════
#  3. price_advice
#     DeepSeek → GPT → Gemini → Claude
# ══════════════════════════════════════════════════════════════════

def price_advice(current_price: float, history_records: list[dict]) -> str:
    if len(history_records) < 3:
        return ""

    # ── 🥇 DeepSeek ──────────────────────────────────────────────
    try:
        from deepseek_utils import price_advice as _fn
        result = _fn(current_price, history_records)
        if result:
            return result
    except Exception as e:
        logger.warning("DeepSeek advice فشل → GPT: %s", e)

    # ── 🥈 GPT-4o-mini ───────────────────────────────────────────
    try:
        from openai_text_utils import price_advice as _fn
        result = _fn(current_price, history_records)
        if result:
            return result
    except Exception as e:
        logger.warning("GPT advice فشل → Gemini: %s", e)

    # ── 🥉 Gemini ────────────────────────────────────────────────
    try:
        from gemini_text_utils import price_advice as _fn
        result = _fn(current_price, history_records)
        if result:
            return result
    except Exception as e:
        logger.warning("Gemini advice فشل → Claude: %s", e)

    # ── 🔁 Claude Haiku ──────────────────────────────────────────
    try:
        prices = [r["price_val"] for r in history_records]
        lo, hi  = min(prices), max(prices)
        avg     = sum(prices) / len(prices)
        days    = max(1, (history_records[-1]["ts"] - history_records[0]["ts"]) // 86400)
        pct     = (current_price - lo) / (hi - lo) * 100 if hi != lo else 50
        prompt  = (
            f"بيانات سعر منتج أمازون ({days} يوم):\n"
            f"الحالي: {current_price:.2f} | الأدنى: {lo:.2f} | الأعلى: {hi:.2f} | المتوسط: {avg:.2f}\n"
            f"السعر عند {pct:.0f}% من النطاق (0=أدنى، 100=أعلى)\n"
            f"أجب بسطر واحد يبدأ بـ 💡 (10 كلمات أو أقل، بدون أرقام)."
        )
        client = _get_claude_client()
        msg    = client.messages.create(
            model="claude-haiku-4-5", max_tokens=80,
            messages=[{"role": "user", "content": prompt}],
        )
        advice = msg.content[0].text.strip().split("\n")[0]
        if not advice.startswith("💡"):
            advice = "💡 " + advice
        return advice[:130]
    except Exception as e:
        logger.warning("Claude advice فشل: %s", e)
        return ""
