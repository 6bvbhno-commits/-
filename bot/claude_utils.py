"""
طبقة الذكاء الاصطناعي — DeepSeek أولاً، Claude احتياط تلقائي.

DeepSeek-V3 : extract_product_intent + chat_response  (سريع، عربي ممتاز)
DeepSeek-R1 : price_advice                            (تفكير عميق لتحليل الأسعار)
Claude Haiku : fallback لكل وظيفة إذا فشل DeepSeek   (موثوق، بلا مفتاح خاص)
"""
import logging
import os
import time

logger = logging.getLogger(__name__)

# ══════════════════════════════════════════════════════════════════
#  Claude client (lazy init + backoff)
# ══════════════════════════════════════════════════════════════════

_client: object | None      = None
_client_error_until: float  = 0.0

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
#  System prompts (مشتركة بين DeepSeek و Claude)
# ══════════════════════════════════════════════════════════════════

_SYSTEM_CHAT = """\
أنت مساعد تسوق ذكي متخصص في أمازون السعودية، اسمك "بوت الأسعار".
هدفك الأول: مساعدة المستخدم في إيجاد أفضل سعر لأي منتج.

قواعد الرد:
- الردود قصيرة ومباشرة (3 أسطر أو أقل).
- اردّ دائماً بالعربية السعودية العامية أو الفصحى الخفيفة.
- إذا ذكر المستخدم منتجاً → حثّه على إرسال الرابط أو الصورة.
- إذا سأل سؤالاً عاماً → أجب باختصار ثم وجّهه لإرسال رابط/صورة.
- لا تختلق أسعاراً من عندك — البوت هو الذي يجلب السعر الحقيقي.
- لا تذكر منافسين لأمازون (نون، جرير، إلخ).\
"""

_SYSTEM_EXTRACT = """\
أنت نظام تصنيف نية الشراء. مهمتك محددة جداً:

إذا كان النص يصف منتجاً يريد المستخدم شراءه أو معرفة سعره:
  → أعد اسم المنتج باللغة الأنسب للبحث في أمازون (عربي أو إنجليزي).
  → اذكر الاسم فقط، بدون شرح أو جمل إضافية.

في كل الحالات الأخرى (تحية، شكر، سؤال عام، شكوى، إلخ):
  → أعد الكلمة NONE فقط.

أمثلة:
  "أبي سماعات بلوتوث" → سماعات بلوتوث
  "كم سعر آيفون 15؟"  → iPhone 15
  "مكيف سبليت 18000"  → مكيف سبليت 18000 وحدة
  "شكراً جزيلاً"      → NONE
  "كيف الحال؟"        → NONE
  "هل أنت بوت؟"       → NONE\
"""


# ══════════════════════════════════════════════════════════════════
#  1. extract_product_intent
# ══════════════════════════════════════════════════════════════════

def extract_product_intent(text: str) -> str | None:
    """
    يستخرج اسم المنتج من نص حر.
    DeepSeek-V3 أولاً → Claude Haiku احتياط.
    """
    # ── DeepSeek أولاً ───────────────────────────────────────────
    try:
        from deepseek_utils import extract_product_intent as ds_extract
        result = ds_extract(text)
        if result is not None:
            return result
        # None يعني "لا نية شراء" — لا داعي لـ fallback
        logger.debug("DeepSeek: لا نية شراء في النص")
        return None
    except Exception as e:
        logger.warning("DeepSeek extract فشل، أنتقل لـ Claude: %s", e)

    # ── Claude احتياط ────────────────────────────────────────────
    try:
        client = _get_claude_client()
        msg = client.messages.create(
            model="claude-haiku-4-5",
            max_tokens=60,
            system=_SYSTEM_EXTRACT,
            messages=[{"role": "user", "content": text[:500]}],
        )
        result = msg.content[0].text.strip()
        if not result or result.upper().startswith("NONE"):
            return None
        if len(result) > 100:
            return None
        return result
    except Exception as e:
        logger.warning("Claude extract_product_intent فشل: %s", e)
        return None


# ══════════════════════════════════════════════════════════════════
#  2. chat_response
# ══════════════════════════════════════════════════════════════════

def chat_response(text: str, history: list[dict]) -> str:
    """
    يُعيد ردّاً محادثياً.
    DeepSeek-V3 أولاً → Claude Haiku احتياط.
    """
    _fallback = (
        "أرسل لي 🔗 رابط منتج أمازون أو 📸 صورة منتج "
        "وأجيبك بأفضل سعر فوراً."
    )

    # ── DeepSeek أولاً ───────────────────────────────────────────
    try:
        from deepseek_utils import chat_response as ds_chat
        result = ds_chat(text, history)
        # دالة deepseek_utils.chat_response تُعيد fallback نصي عند فشلها
        # نتحقق: إذا رجعت نفس الـ fallback → جرب Claude
        if result and result != _fallback:
            return result
    except Exception as e:
        logger.warning("DeepSeek chat فشل، أنتقل لـ Claude: %s", e)

    # ── Claude احتياط ────────────────────────────────────────────
    try:
        client   = _get_claude_client()
        messages = list(history[-8:]) + [{"role": "user", "content": text[:800]}]
        msg = client.messages.create(
            model="claude-haiku-4-5",
            max_tokens=250,
            system=_SYSTEM_CHAT,
            messages=messages,
        )
        return msg.content[0].text.strip()
    except Exception as e:
        logger.warning("Claude chat_response فشل: %s", e)
        return _fallback


# ══════════════════════════════════════════════════════════════════
#  3. price_advice
# ══════════════════════════════════════════════════════════════════

def price_advice(current_price: float, history_records: list[dict]) -> str:
    """
    توصية شراء — DeepSeek-R1 (تفكير عميق) أولاً → Claude احتياط.
    """
    if len(history_records) < 3:
        return ""

    # ── DeepSeek-R1 أولاً ────────────────────────────────────────
    try:
        from deepseek_utils import price_advice as ds_advice
        result = ds_advice(current_price, history_records)
        if result:
            return result
    except Exception as e:
        logger.warning("DeepSeek price_advice فشل، أنتقل لـ Claude: %s", e)

    # ── Claude Haiku احتياط ──────────────────────────────────────
    try:
        prices = [r["price_val"] for r in history_records]
        lo     = min(prices)
        hi     = max(prices)
        avg    = sum(prices) / len(prices)
        days   = max(1, (history_records[-1]["ts"] - history_records[0]["ts"]) // 86400)

        prompt = (
            f"منتج أمازون السعودية — بيانات سعرية:\n"
            f"• السعر الحالي:  {current_price:.2f} ر.س\n"
            f"• أدنى سعر ({days} يوم):  {lo:.2f} ر.س\n"
            f"• أعلى سعر:  {hi:.2f} ر.س\n"
            f"• المتوسط:  {avg:.2f} ر.س\n\n"
            f"اكتب توصية شراء واحدة مختصرة (10-15 كلمة) تبدأ بـ 💡\n"
            f"لا تذكر أرقاماً — فقط توصية واضحة."
        )
        client = _get_claude_client()
        msg = client.messages.create(
            model="claude-haiku-4-5",
            max_tokens=80,
            messages=[{"role": "user", "content": prompt}],
        )
        advice = msg.content[0].text.strip().split("\n")[0]
        if not advice.startswith("💡"):
            advice = "💡 " + advice
        return advice[:130]
    except Exception as e:
        logger.warning("Claude price_advice فشل: %s", e)
        return ""
