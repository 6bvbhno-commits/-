"""
إرسال جماعي لتنبيهات أكواد الخصم — فوري أو مجدول (مثلاً 9 مساءً بتوقيت الرياض).
"""
from __future__ import annotations

import asyncio
import logging
import time
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone
from typing import Any, Callable, Awaitable
from zoneinfo import ZoneInfo

from telegram import InlineKeyboardButton, InlineKeyboardMarkup
from telegram.error import Forbidden, RetryAfter, TelegramError, TimedOut, NetworkError

import users_db as _users
from amazon_utils import build_affiliate_search_link, get_affiliate_tag
from config import AMAZON_DOMAIN

logger = logging.getLogger(__name__)

_RIYADH = ZoneInfo("Asia/Riyadh")


@dataclass
class ScheduledJob:
    id: str
    run_at: float          # monotonic-ish epoch seconds (time.time())
    code: str
    title: str
    body: str
    button_url: str
    status: str = "scheduled"  # scheduled | sending | done | cancelled | failed
    log_id: int = 0
    result: dict = field(default_factory=dict)


_jobs: dict[str, ScheduledJob] = {}
_jobs_lock = asyncio.Lock()
_app_ref: Any = None
_loop_started = False


def set_application(app) -> None:
    global _app_ref
    _app_ref = app


def riyadh_now() -> datetime:
    return datetime.now(_RIYADH)


def next_riyadh_hour(hour: int = 21, minute: int = 0) -> datetime:
    """أقرب موعد الساعة المحددة بتوقيت الرياض (اليوم إن لم يفت، وإلا غداً)."""
    now = riyadh_now()
    target = now.replace(hour=hour, minute=minute, second=0, microsecond=0)
    if target <= now + timedelta(seconds=30):
        target = target + timedelta(days=1)
    return target


def format_discount_message(
    *,
    code: str,
    title: str = "",
    extra: str = "",
    when_label: str = "اليوم",
) -> str:
    code = (code or "").strip().upper()
    title = (title or "كود خصم أمازون").strip()
    extra = (extra or "").strip()
    lines = [
        f"🔥 *{title}*",
        "",
        f"🏷️ الكود: `{code}`" if code else "🏷️ كود الخصم جاهز على أمازون",
        f"🕘 متاح {when_label} — استخدمه قبل ما يخلص!",
        "",
    ]
    if extra:
        lines.append(extra)
        lines.append("")
    lines.extend(
        [
            "1️⃣ انسخ الكود",
            "2️⃣ افتح أمازون من الزر تحت",
            "3️⃣ الصق الكود عند الدفع",
            "",
            "⚡ يفوز اللي يلحّق — الكمية / المدة محدودة غالباً",
        ]
    )
    return "\n".join(lines)


def default_button_url() -> str:
    return build_affiliate_search_link("عروض", AMAZON_DOMAIN)


def list_jobs() -> list[dict]:
    out = []
    for j in sorted(_jobs.values(), key=lambda x: x.run_at):
        out.append(
            {
                "id": j.id,
                "run_at": j.run_at,
                "run_at_riyadh": datetime.fromtimestamp(j.run_at, _RIYADH).strftime(
                    "%Y-%m-%d %H:%M"
                ),
                "code": j.code,
                "title": j.title,
                "status": j.status,
                "result": j.result,
            }
        )
    return out


async def cancel_job(job_id: str) -> bool:
    async with _jobs_lock:
        job = _jobs.get(job_id)
        if not job or job.status != "scheduled":
            return False
        job.status = "cancelled"
        if job.log_id:
            _users.update_broadcast_log(job.log_id, status="cancelled")
        return True


async def send_discount_broadcast(
    *,
    code: str,
    title: str = "كود خصم أمازون اليوم",
    extra: str = "",
    button_url: str = "",
    when_label: str = "اليوم",
    dry_run: bool = False,
) -> dict:
    """يرسل التنبيه فوراً لكل المستخدمين المسجّلين."""
    app = _app_ref
    if app is None:
        return {"ok": False, "error": "البوت غير جاهز بعد"}

    message = format_discount_message(
        code=code, title=title, extra=extra, when_label=when_label
    )
    url = (button_url or "").strip() or default_button_url()
    kb = InlineKeyboardMarkup(
        [
            [InlineKeyboardButton("🛒 تسوّق الآن على أمازون ↗", url=url)],
            [
                InlineKeyboardButton(
                    "📋 انسخ الفكرة: افتح أمازون والصق الكود",
                    url=url,
                )
            ],
        ]
    )

    chat_ids = _users.get_broadcast_chat_ids()
    total = len(chat_ids)
    log_id = _users.log_broadcast(
        kind="discount",
        code=code,
        message=message,
        scheduled_for=None,
        status="dry_run" if dry_run else "sending",
        total=total,
    )

    if dry_run:
        return {
            "ok": True,
            "dry_run": True,
            "total": total,
            "preview": message,
            "button_url": url,
            "tag": get_affiliate_tag(),
            "log_id": log_id,
        }

    if total == 0:
        _users.update_broadcast_log(log_id, status="empty", total=0)
        return {
            "ok": False,
            "error": "لا يوجد مستخدمون مسجّلون بعد. يحتاج شخص يضغط /start مرة على الأقل.",
            "total": 0,
            "log_id": log_id,
        }

    ok = fail = 0
    for i, cid in enumerate(chat_ids):
        try:
            await app.bot.send_message(
                chat_id=cid,
                text=message,
                parse_mode="Markdown",
                reply_markup=kb,
                disable_web_page_preview=True,
            )
            ok += 1
        except Forbidden:
            _users.mark_blocked(cid)
            fail += 1
        except RetryAfter as e:
            wait = min(int(e.retry_after) + 1, 30)
            await asyncio.sleep(wait)
            try:
                await app.bot.send_message(
                    chat_id=cid,
                    text=message,
                    parse_mode="Markdown",
                    reply_markup=kb,
                    disable_web_page_preview=True,
                )
                ok += 1
            except Exception:
                fail += 1
        except (TimedOut, NetworkError):
            await asyncio.sleep(1)
            try:
                await app.bot.send_message(
                    chat_id=cid,
                    text=message.replace("*", "").replace("`", ""),
                    parse_mode=None,
                    reply_markup=kb,
                )
                ok += 1
            except Exception:
                fail += 1
        except TelegramError as e:
            logger.warning("broadcast fail chat=%s: %s", cid, e)
            fail += 1

        # تهدئة بسيطة لتفادي FloodWait
        if (i + 1) % 20 == 0:
            await asyncio.sleep(1.0)
        else:
            await asyncio.sleep(0.05)

    _users.update_broadcast_log(
        log_id, status="done", sent_ok=ok, sent_fail=fail, total=total
    )
    logger.info("📢 broadcast done ok=%d fail=%d total=%d code=%s", ok, fail, total, code)
    return {
        "ok": True,
        "sent_ok": ok,
        "sent_fail": fail,
        "total": total,
        "code": code,
        "button_url": url,
        "log_id": log_id,
    }


async def schedule_discount_broadcast(
    *,
    code: str,
    title: str = "كود خصم أمازون اليوم",
    extra: str = "",
    button_url: str = "",
    hour: int = 21,
    minute: int = 0,
    force_at: datetime | None = None,
) -> dict:
    """يجدول إرسال كود الخصم (افتراضي 21:00 بتوقيت الرياض)."""
    when = force_at or next_riyadh_hour(hour, minute)
    if when.tzinfo is None:
        when = when.replace(tzinfo=_RIYADH)
    else:
        when = when.astimezone(_RIYADH)

    run_at = when.timestamp()
    job_id = f"disc-{int(run_at)}-{int(time.time()) % 10000}"
    message = format_discount_message(
        code=code, title=title, extra=extra, when_label="الحين"
    )
    url = (button_url or "").strip() or default_button_url()
    log_id = _users.log_broadcast(
        kind="discount_scheduled",
        code=code,
        message=message,
        scheduled_for=int(run_at),
        status="scheduled",
        total=_users.count_users().get("active", 0),
    )
    job = ScheduledJob(
        id=job_id,
        run_at=run_at,
        code=code,
        title=title,
        body=message,
        button_url=url,
        log_id=log_id,
    )
    # نخزّن الحقول الإضافية على الكائن
    job.extra = extra  # type: ignore[attr-defined]
    job.when_label = "الحين"  # type: ignore[attr-defined]

    async with _jobs_lock:
        _jobs[job_id] = job

    logger.info(
        "⏰ scheduled discount code=%s at %s Asia/Riyadh (job=%s)",
        code,
        when.strftime("%Y-%m-%d %H:%M"),
        job_id,
    )
    return {
        "ok": True,
        "job_id": job_id,
        "run_at_riyadh": when.strftime("%Y-%m-%d %H:%M"),
        "run_at_unix": int(run_at),
        "code": code,
        "log_id": log_id,
        "seconds_left": max(0, int(run_at - time.time())),
    }


async def scheduler_loop() -> None:
    """يراجع المهام المجدولة كل 5 ثوانٍ."""
    global _loop_started
    if _loop_started:
        return
    _loop_started = True
    logger.info("⏰ broadcast scheduler بدأ")
    while True:
        try:
            now = time.time()
            due: list[ScheduledJob] = []
            async with _jobs_lock:
                for job in list(_jobs.values()):
                    if job.status == "scheduled" and job.run_at <= now:
                        job.status = "sending"
                        due.append(job)
            for job in due:
                try:
                    extra = getattr(job, "extra", "")
                    result = await send_discount_broadcast(
                        code=job.code,
                        title=job.title,
                        extra=extra,
                        button_url=job.button_url,
                        when_label="الحين",
                    )
                    job.result = result
                    job.status = "done" if result.get("ok") else "failed"
                    if job.log_id and result.get("ok"):
                        _users.update_broadcast_log(
                            job.log_id,
                            status="done",
                            sent_ok=result.get("sent_ok", 0),
                            sent_fail=result.get("sent_fail", 0),
                            total=result.get("total", 0),
                        )
                except Exception as e:
                    job.status = "failed"
                    job.result = {"ok": False, "error": str(e)}
                    logger.error("scheduled broadcast فشل: %s", e, exc_info=True)
        except Exception as e:
            logger.warning("scheduler_loop: %s", e)
        await asyncio.sleep(5)
