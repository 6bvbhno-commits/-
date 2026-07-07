"""
Claude AI — ثلاث قدرات لتحسين تجربة البوت:
  1. extract_product_intent  — يستخرج اسم المنتج من نص حر
  2. chat_response           — ردّ محادثي ذكي للرسائل العامة
  3. price_advice            — توصية شراء بناءً على تاريخ السعر
"""
import logging
import os

logger = logging.getLogger(__name__)

# ── Client (lazy init) ────────────────────────────────────────────────────────
_client = None

def _get_client():
    global _client
    if _client is None:
        from anthropic import Anthropic
        base_url = os.getenv("AI_INTEGRATIONS_ANTHROPIC_BASE_URL", "")
        api_key  = os.getenv("AI_INTEGRATIONS_ANTHROPIC_API_KEY", "dummy")
        kwargs   = {"api_key": api_key}
        if base_url:
            kwargs["base_url"] = base_url
        _client = Anthropic(**kwargs)
    return _client


# ── System prompts ─────────────────────────────────────────────────────────────

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
  "كم سعر آيفون 15؟" → iPhone 15
  "مكيف سبليت 18000" → مكيف سبليت 18000 وحدة
  "شكراً جزيلاً" → NONE
  "كيف الحال؟" → NONE
  "هل أنت بوت؟" → NONE\
"""


# ── الدوال العامة ─────────────────────────────────────────────────────────────

def extract_product_intent(text: str) -> str | None:
    """
    يستخرج اسم المنتج من نص حر.
    يُعيد None إذا لم يجد نية شراء واضحة.
    سريع جداً — يستخدم Haiku.
    """
    try:
        client = _get_client()
        msg = client.messages.create(
            model="claude-haiku-4-5",
            max_tokens=60,
            system=_SYSTEM_EXTRACT,
            messages=[{"role": "user", "content": text[:500]}],
        )
        result = msg.content[0].text.strip()
        if not result or result.upper().startswith("NONE"):
            return None
        # تجاهل الردود الطويلة جداً (ليست اسم منتج)
        if len(result) > 100:
            return None
        return result
    except Exception as e:
        logger.warning("Claude extract_product_intent فشل: %s", e)
        return None


def chat_response(text: str, history: list[dict]) -> str:
    """
    يُعيد ردّاً محادثياً من Claude.
    history: قائمة من {"role": "user"/"assistant", "content": "..."}
    """
    try:
        client   = _get_client()
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
        return (
            "أرسل لي 🔗 رابط منتج أمازون أو 📸 صورة منتج "
            "وأجيبك بأفضل سعر فوراً."
        )


def price_advice(current_price: float, history_records: list[dict]) -> str:
    """
    يُعيد توصية شراء في سطر واحد (💡 ...) بناءً على تاريخ السعر.
    يُعيد نصاً فارغاً إذا كانت البيانات غير كافية.
    """
    if len(history_records) < 3:
        return ""
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
            f"لا تذكر أرقاماً — فقط توصية واضحة (مثل: وقت مناسب للشراء، أو انتظر ينزل أكثر)."
        )

        client = _get_client()
        msg = client.messages.create(
            model="claude-haiku-4-5",
            max_tokens=80,
            messages=[{"role": "user", "content": prompt}],
        )
        advice = msg.content[0].text.strip().split("\n")[0]
        # تأكد أنها تبدأ بـ 💡
        if not advice.startswith("💡"):
            advice = "💡 " + advice
        return advice[:130]
    except Exception as e:
        logger.warning("Claude price_advice فشل: %s", e)
        return ""
