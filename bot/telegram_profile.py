"""
إعداد ملف البوت في تيليجرام لتحسين الظهور في البحث (Telegram Search SEO).

يضبط عبر Bot API عند الإقلاع:
  • الاسم الظاهر (setMyName)
  • الوصف القصير — يظهر في نتائج البحث (setMyShortDescription) ≤ 120 حرف
  • الوصف الكامل — يظهر عند فتح المحادثة (setMyDescription) ≤ 512 حرف
  • قائمة الأوامر بالعربية (setMyCommands)
"""
from __future__ import annotations

import logging

from telegram import BotCommand, BotCommandScopeDefault, MenuButtonCommands
from telegram.ext import Application

from config import (
    BOT_DESCRIPTION,
    BOT_DISPLAY_NAME,
    BOT_SHORT_DESCRIPTION,
)

logger = logging.getLogger(__name__)

# أوامر عربية — تحسّن تجربة القائمة والبحث الداخلي
_COMMANDS = [
    BotCommand("start", "ابدأ — أرخص أسعار أمازون"),
    BotCommand("deals", "العروض الأكثر طلباً الآن"),
    BotCommand("compare", "قارن بين منتجين"),
    BotCommand("fav", "مفضلتك المحفوظة"),
    BotCommand("help", "طريقة الاستخدام"),
    BotCommand("myalerts", "تنبيهات انخفاض السعر"),
    BotCommand("mute", "إيقاف رسائل العروض"),
    BotCommand("unmute", "تفعيل رسائل العروض"),
    BotCommand("share", "شارك البوت مع أصحابك"),
    BotCommand("version", "إصدار البوت"),
]


def _clip(text: str, limit: int) -> str:
    text = (text or "").strip()
    if len(text) <= limit:
        return text
    return text[: limit - 1].rstrip() + "…"


async def apply_telegram_profile(application: Application) -> None:
    """يحدّث ملف البوت في تيليجرام — يُستدعى من post_init."""
    bot = application.bot
    short = _clip(BOT_SHORT_DESCRIPTION, 120)
    full = _clip(BOT_DESCRIPTION, 512)
    name = _clip(BOT_DISPLAY_NAME, 64)

    try:
        await bot.set_my_short_description(short_description=short)
        logger.info("📣 short_description OK (%d حرف)", len(short))
    except Exception as e:
        logger.warning("set_my_short_description فشل: %s", e)

    try:
        await bot.set_my_description(description=full)
        logger.info("📣 description OK (%d حرف)", len(full))
    except Exception as e:
        logger.warning("set_my_description فشل: %s", e)

    try:
        await bot.set_my_name(name=name)
        logger.info("📣 display name OK: %s", name)
    except Exception as e:
        logger.warning("set_my_name فشل: %s", e)

    try:
        await bot.set_my_commands(_COMMANDS, scope=BotCommandScopeDefault())
        logger.info("📣 commands OK (%d أمر)", len(_COMMANDS))
    except Exception as e:
        logger.warning("set_my_commands فشل: %s", e)

    try:
        await bot.set_chat_menu_button(menu_button=MenuButtonCommands())
        logger.info("📣 menu button = commands")
    except Exception as e:
        logger.warning("set_chat_menu_button فشل: %s", e)

    me = await bot.get_me()
    logger.info(
        "🔍 Telegram profile: @%s | name=%s | يمكن البحث عنه في تيليجرام",
        me.username or "?",
        me.first_name or name,
    )
