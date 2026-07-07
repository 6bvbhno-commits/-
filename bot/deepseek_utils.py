"""
DeepSeek AI — بديل أقوى وأرخص لـ Claude في ثلاث مهام:
  1. extract_product_intent  — استخراج اسم المنتج من نص حر
  2. chat_response           — ردّ محادثي ذكي بالعربية السعودية
  3. price_advice            — توصية شراء بناءً على تاريخ السعر (مع تفكير عميق)

يستخدم DeepSeek-V3 للمهام السريعة و DeepSeek-R1 لتحليل الأسعار.
API متوافق مع OpenAI — لا حاجة لمكتبة إضافية.
"""
import logging
import os
import requests

logger = logging.getLogger(__name__)

# ── إعدادات ───────────────────────────────────────────────────────────────────
_BASE_URL   = "https://api.deepseek.com/v1/chat/completions"
_MODEL_CHAT = "deepseek-chat"       # DeepSeek-V3  — سريع، رخيص، عربي ممتاز
_MODEL_R1   = "deepseek-reasoner"   # DeepSeek-R1  — تفكير عميق لتحليل الأسعار
_TIMEOUT    = 25  # ثانية

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
  "كم سعر آيفون 15؟"  → iPhone 15
  "مكيف سبليت 18000"  → مكيف سبليت 18000 وحدة
  "شكراً جزيلاً"      → NONE
  "كيف الحال؟"        → NONE
  "هل أنت بوت؟"       → NONE\
"""


# ── دالة مساعدة ───────────────────────────────────────────────────────────────

def _call(model: str, messages: list[dict],
          system: str | None = None, max_tokens: int = 300) -> str | None:
    """
    يستدعي DeepSeek API مباشرة عبر requests (OpenAI-compatible).
    يُعيد نص الرد أو None عند الفشل.
    """
    api_key = os.getenv("DEEPSEEK_API_KEY", "")
    if not api_key:
        logger.warning("DEEPSEEK_API_KEY غير موجود")
        return None

    msgs = []
    if system:
        msgs.append({"role": "system", "content": system})
    msgs.extend(messages)

    payload = {
        "model":      model,
        "messages":   msgs,
        "max_tokens": max_tokens,
        "stream":     False,
    }

    try:
        resp = requests.post(
            _BASE_URL,
            headers={
                "Authorization": f"Bearer {api_key}",
                "Content-Type":  "application/json",
            },
            json=payload,
            timeout=_TIMEOUT,
        )
        if resp.status_code == 402:
            logger.warning("DeepSeek: رصيد منتهٍ (402)")
            return None
        if resp.status_code == 429:
            logger.warning("DeepSeek: rate limit (429)")
            return None
        if resp.status_code != 200:
            logger.warning("DeepSeek HTTP %s: %s", resp.status_code, resp.text[:150])
            return None

        data = resp.json()

        choice  = data.get("choices", [{}])[0]
        message = choice.get("message", {})
        text    = message.get("content", "").strip()

        # DeepSeek-R1: إذا كان content فارغاً، استخرج الجواب من آخر reasoning_content
        if not text:
            reasoning = message.get("reasoning_content", "").strip()
            if reasoning:
                # آخر جملة غير فارغة هي الاستنتاج النهائي
                lines = [l.strip() for l in reasoning.split("\n") if l.strip()]
                text  = lines[-1] if lines else ""
                if text:
                    logger.info("DeepSeek R1: استُخرج الجواب من reasoning_content")

        if not text:
            logger.warning("DeepSeek: رد فارغ")
            return None

        logger.info("DeepSeek (%s): نجح (%d حرف)", model, len(text))
        return text

    except requests.Timeout:
        logger.warning("DeepSeek: timeout بعد %ds", _TIMEOUT)
        return None
    except Exception as e:
        logger.error("DeepSeek exception: %s", e)
        return None


# ── الدوال العامة ─────────────────────────────────────────────────────────────

def extract_product_intent(text: str) -> str | None:
    """
    يستخرج اسم المنتج من نص حر — DeepSeek-V3.
    يُعيد None إذا لم يجد نية شراء واضحة.
    """
    result = _call(
        model=_MODEL_CHAT,
        messages=[{"role": "user", "content": text[:500]}],
        system=_SYSTEM_EXTRACT,
        max_tokens=60,
    )
    if not result or result.upper().startswith("NONE"):
        return None
    if len(result) > 100:
        return None
    return result


def chat_response(text: str, history: list[dict]) -> str:
    """
    يُعيد ردّاً محادثياً من DeepSeek-V3.
    history: قائمة {"role": "user"/"assistant", "content": "..."}
    """
    messages = list(history[-8:]) + [{"role": "user", "content": text[:800]}]
    result = _call(
        model=_MODEL_CHAT,
        messages=messages,
        system=_SYSTEM_CHAT,
        max_tokens=250,
    )
    if result:
        return result
    return (
        "أرسل لي 🔗 رابط منتج أمازون أو 📸 صورة منتج "
        "وأجيبك بأفضل سعر فوراً."
    )


def price_advice(current_price: float, history_records: list[dict]) -> str:
    """
    يُعيد توصية شراء في سطر واحد (💡 ...) — DeepSeek-V3.
    V3 أنظف وأسرع من R1 لهذه المهمة القصيرة.
    """
    if len(history_records) < 3:
        return ""

    prices = [r["price_val"] for r in history_records]
    lo     = min(prices)
    hi     = max(prices)
    avg    = sum(prices) / len(prices)
    days   = max(1, (history_records[-1]["ts"] - history_records[0]["ts"]) // 86400)
    pct    = (current_price - lo) / (hi - lo) * 100 if hi != lo else 50

    prompt = (
        f"بيانات سعر منتج أمازون ({days} يوم):\n"
        f"• الحالي: {current_price:.2f} ر.س | الأدنى: {lo:.2f} | الأعلى: {hi:.2f} | المتوسط: {avg:.2f}\n"
        f"• السعر الحالي عند {pct:.0f}% من النطاق (0%=أدنى سعر، 100%=أعلى سعر)\n\n"
        f"أجب بسطر واحد فقط يبدأ بـ 💡 — توصية شراء بدون أرقام (10 كلمات أو أقل).\n"
        f"مثال: 💡 وقت ممتاز للشراء، السعر في قاعه\n"
        f"مثال: 💡 انتظر قليلاً، السعر مرتفع نسبياً\n"
        f"لا تكتب إلا السطر."
    )

    result = _call(
        model=_MODEL_CHAT,
        messages=[{"role": "user", "content": prompt}],
        max_tokens=60,
    )
    if not result:
        return ""

    advice = result.strip().split("\n")[0]
    if not advice.startswith("💡"):
        advice = "💡 " + advice
    return advice[:130]
