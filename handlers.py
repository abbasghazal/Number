import re
import asyncio
import random
import datetime
import logging
import shutil
import os
import html
from datetime import timedelta
from telethon import events, Button, functions, types, TelegramClient
from telethon.errors import (
    AuthKeyInvalidError,
    AuthKeyUnregisteredError,
    ChannelPrivateError,
    SessionExpiredError,
    SessionPasswordNeededError,
    SessionRevokedError,
    UnauthorizedError,
    UserDeactivatedBanError,
    UserDeactivatedError,
    UserNotParticipantError,
)
from telethon.sessions import StringSession
from telethon.tl.functions.account import GetAuthorizationsRequest, ResetAuthorizationRequest, UpdateProfileRequest
from telethon.tl.functions.channels import JoinChannelRequest, CreateChannelRequest, GetParticipantRequest
from telethon.tl.functions.messages import ExportChatInviteRequest, SendMessageRequest, EditChatDefaultBannedRightsRequest
from telethon.tl.types import ChatBannedRights, InputPeerChannel
from config import API_ID, API_HASH, ADMIN_ID, DB_PATH, GROUP_NAMES
from helpers import *
from messages import msgs
from keyboards import *

# إعداد نظام تسجيل الأخطاء
logger = logging.getLogger(__name__)
TELEGRAM_NOTIFICATIONS_USER_ID = 777000

# متغيرات حالة المحادثة
active_conversations = {}
manual_creation_tasks = {}
auto_creation_tasks = {}
posting_active = False
user_pages = {}
price_country_pages = {}
add_number_country_pages = {}
user_purchased_accounts = {}
user_posting_accounts = {}
user_operation_locks = {}
user_leave_channel_accounts = {}
user_leave_channel_targets = {}
user_leave_group_accounts = {}
incoming_storage_clients = {}
bot = None
LICENSE_FREE_COMMANDS = ("/activate", "/status", "/mylicense")
LICENSE_ADMIN_COMMANDS = ("/genlicense", "/licenses", "/revokelicense", "/clearlicenses", "/licensestats", "/exportlicenses")
FREE_TRIAL_HOURS = 24

def get_user_lock(user_id):
    if user_id not in user_operation_locks:
        user_operation_locks[user_id] = asyncio.Lock()
    return user_operation_locks[user_id]

def pending_purchase_status(purchase):
    if len(purchase) > 9 and purchase[8] in (0, 1, False, True):
        return purchase[9]
    return purchase[8] if len(purchase) > 8 else "pending"

def mask_phone_number(phone):
    digits = str(phone).replace("+", "").strip()
    if not digits:
        return "+****"

    visible_count = max(1, len(digits) // 2)
    return f"+{digits[:visible_count]}{'*' * (len(digits) - visible_count)}"

async def get_buyer_display(client, user_id):
    try:
        entity = await client.get_entity(user_id)
        name_parts = [getattr(entity, "first_name", None), getattr(entity, "last_name", None)]
        full_name = " ".join(part for part in name_parts if part).strip()
        username = f"@{entity.username}" if getattr(entity, "username", None) else ""
        display_name = full_name or username or "غير معروف"
    except Exception:
        user_data = await get_user(user_id)
        display_name = " ".join(
            part for part in [
                user_data.get("first_name") if user_data else None,
                user_data.get("last_name") if user_data else None
            ] if part
        ).strip() or (user_data.get("username") if user_data and user_data.get("username") else "غير معروف")

    return html.escape(display_name)

async def publish_purchase_proof(client, user_id, phone, country_name=None, price=None):
    trust_channel = await get_setting("trust_channel")
    if not trust_channel:
        return

    buyer_name = await get_buyer_display(client, user_id)
    masked_phone = mask_phone_number(phone)
    now = datetime.datetime.now().strftime("%Y-%m-%d %H:%M")

    country_line = f"• الدولة: {html.escape(str(country_name))}\n" if country_name else ""
    price_line = f"• السعر: {html.escape(str(price))} $\n" if price is not None else ""
    proof_text = (
        "✅ <b>إثبات تسليم رقم جديد</b>\n\n"
        f"• المشتري: <a href=\"tg://user?id={user_id}\">{buyer_name}</a>\n"
        f"• ايدي المشتري: <code>{user_id}</code>\n"
        f"{country_line}"
        f"• الرقم: <code>{masked_phone}</code>\n"
        f"{price_line}"
        "• الحالة: تم التسليم بنجاح ✅\n"
        f"• التاريخ: {now}"
    )

    try:
        bot_info = await client.get_me()
        buttons = None
        if getattr(bot_info, "username", None):
            buttons = [[Button.url("• الدخول الى البوت •", f"https://t.me/{bot_info.username}?start=start")]]

        await client.send_message(trust_channel, proof_text, parse_mode="html", buttons=buttons)
    except Exception as exc:
        logger.error(f"Failed to publish purchase proof: {str(exc)}")

def license_required_message(reason, expires_at=None):
    if reason == "trial_expired":
        expired_text = f"\n• انتهت الفترة المجانية بتاريخ: {expires_at.strftime('%Y-%m-%d %H:%M')}" if expires_at else ""
        return (
            "⏱ انتهت فترة التجربة المجانية الخاصة بك.\n"
            f"{expired_text}\n\n"
            "للاستمرار في استخدام البوت يرجى شراء مفتاح تفعيل من المطور: @SaHaM41\n"
            "ثم أرسل:\n"
            "`/activate YOUR-KEY`"
        )

    if reason == "expired":
        expired_text = f"\n• انتهى بتاريخ: {expires_at.strftime('%Y-%m-%d %H:%M')}" if expires_at else ""
        return (
            "❌ انتهت صلاحية اشتراكك في البوت.\n"
            f"{expired_text}\n\n"
            f"للتجديد يرجى شراء مفتاح جديد من المطور: @SaHaM41\n"
            "ثم أرسل:\n"
            "`/activate YOUR-KEY`"
        )

    if reason == "invalid":
        return (
            "❌ ترخيصك غير صالح أو تم إلغاؤه.\n\n"
            f"يرجى التواصل مع المطور: @SaHaM41\n"
            "ثم تفعيل مفتاح جديد عبر:\n"
            "`/activate YOUR-KEY`"
        )

    return (
        "🔐 البوت يعمل بنظام الاشتراك المدفوع.\n\n"
        f"للاستخدام يرجى شراء مفتاح تفعيل من المطور: @SaHaM41\n"
        "بعد استلام المفتاح أرسل:\n"
        "`/activate YOUR-KEY`\n\n"
        "لمعرفة حالة اشتراكك أرسل: `/status`"
    )

async def get_free_trial_status(user_id, create_if_missing=False):
    user_data = await get_user(user_id)
    if not user_data and create_if_missing:
        await create_user(user_id)
        user_data = await get_user(user_id)
    if not user_data:
        return {"allowed": False, "reason": "missing", "expires_at": None}

    join_date = parse_db_datetime(user_data.get("join_date"))
    if not join_date:
        return {"allowed": False, "reason": "trial_expired", "expires_at": None}

    trial_expires_at = join_date + datetime.timedelta(hours=FREE_TRIAL_HOURS)
    if trial_expires_at > datetime.datetime.now():
        return {"allowed": True, "reason": "trial", "expires_at": trial_expires_at}
    return {"allowed": False, "reason": "trial_expired", "expires_at": trial_expires_at}

async def user_has_license_access(user_id):
    if user_id == ADMIN_ID:
        return True, {"reason": "admin"}
    status = await get_license_status(user_id)
    if status["allowed"]:
        return True, status
    if status.get("reason") == "missing":
        trial_status = await get_free_trial_status(user_id, create_if_missing=True)
        return trial_status["allowed"], trial_status
    return status["allowed"], status

async def enforce_license_for_message(event):
    user_id = event.chat_id
    if user_id == ADMIN_ID:
        return
    text = (event.raw_text or "").strip()
    command = text.split()[0].lower() if text else ""
    if command in LICENSE_FREE_COMMANDS or command in LICENSE_ADMIN_COMMANDS:
        return
    if command == "/start" and not await get_user(user_id):
        return

    allowed, status = await user_has_license_access(user_id)
    if allowed:
        return

    await event.respond(
        license_required_message(status.get("reason"), status.get("expires_at")),
        parse_mode="markdown"
    )
    raise events.StopPropagation

async def enforce_license_for_callback(event):
    user_id = event.chat_id
    if user_id == ADMIN_ID or await is_admin(user_id):
        return True

    allowed, status = await user_has_license_access(user_id)
    if allowed:
        return True

    await event.answer("🔐 يجب تفعيل اشتراكك أولاً لاستخدام البوت.", alert=True)
    try:
        await event.respond(
            license_required_message(status.get("reason"), status.get("expires_at")),
            parse_mode="markdown"
        )
    except Exception:
        pass
    return False

def format_remaining_time(expires_at):
    if not expires_at:
        return "غير معروف"
    delta = expires_at - datetime.datetime.now()
    if delta.total_seconds() <= 0:
        return "منتهي"
    days = delta.days
    hours = delta.seconds // 3600
    minutes = (delta.seconds % 3600) // 60
    return f"{days} يوم و {hours} ساعة و {minutes} دقيقة"

async def is_license_admin(user_id):
    return user_id == ADMIN_ID or await is_admin(user_id)

def license_state_text(row):
    used_by = row[4]
    expires_at = parse_db_datetime(row[6])
    is_revoked = int(row[8] or 0) == 1
    if is_revoked:
        return "ملغي"
    if used_by and expires_at and expires_at <= datetime.datetime.now():
        return "منتهي"
    if used_by:
        return "مستخدم"
    return "غير مستخدم"

# ===== معالجات الأحداث =====

@events.register(events.NewMessage(pattern="/sell_price", func=lambda e: e.is_private))
async def sell_price_handler(event):
    countries = await get_countries()
    text = "\n".join([f"{c[1]} ({c[0]}): {c[3]}$" for c in countries])
    await event.respond(text)

async def license_guard_handler(event):
    await enforce_license_for_message(event)

@events.register(events.NewMessage(pattern=r"^/activate(?:\s+(.+))?$", func=lambda e: e.is_private))
async def activate_license_handler(event):
    user_id = event.chat_id
    parts = (event.raw_text or "").split(maxsplit=1)
    if len(parts) < 2:
        await event.respond(
            "أرسل المفتاح بعد الأمر بهذا الشكل:\n"
            "`/activate X7K9M-3P2N8-L4R6Q-V5W1E`",
            parse_mode="markdown"
        )
        return

    await create_user(user_id)
    success, result = await activate_license(user_id, parts[1])
    if not success:
        await event.respond(f"❌ فشل التفعيل: {result}")
        return

    await event.respond(
        "✅ تم تفعيل اشتراكك بنجاح!\n\n"
        f"• تاريخ الانتهاء: `{result.strftime('%Y-%m-%d %H:%M')}`\n"
        f"• المدة المتبقية: {format_remaining_time(result)}",
        parse_mode="markdown"
    )

@events.register(events.NewMessage(pattern=r"^/(status|mylicense)$", func=lambda e: e.is_private))
async def license_status_handler(event):
    user_id = event.chat_id
    if user_id == ADMIN_ID:
        await event.respond("✅ أنت المطور الأساسي، لا تحتاج إلى ترخيص.")
        return

    status = await get_license_status(user_id)
    if not status["license"]:
        trial_status = await get_free_trial_status(user_id, create_if_missing=True)
        if trial_status["allowed"]:
            await event.respond(
                "🎁 أنت تستخدم الفترة المجانية الآن.\n\n"
                f"• تنتهي في: `{trial_status['expires_at'].strftime('%Y-%m-%d %H:%M')}`\n"
                f"• المتبقي: {format_remaining_time(trial_status['expires_at'])}\n\n"
                "بعد انتهاء الفترة المجانية ستحتاج إلى تفعيل مفتاح.",
                parse_mode="markdown"
            )
        else:
            await event.respond(license_required_message("trial_expired", trial_status.get("expires_at")), parse_mode="markdown")
        return

    expires_at = status.get("expires_at")
    if status["allowed"]:
        await event.respond(
            "✅ اشتراكك نشط.\n\n"
            f"• المفتاح: `{status['license'][1]}`\n"
            f"• ينتهي في: `{expires_at.strftime('%Y-%m-%d %H:%M')}`\n"
            f"• المتبقي: {format_remaining_time(expires_at)}",
            parse_mode="markdown"
        )
    else:
        await event.respond(license_required_message(status["reason"], expires_at), parse_mode="markdown")

@events.register(events.NewMessage(pattern=r"^/genlicense(?:\s+(\d+))?$", func=lambda e: e.is_private))
async def generate_license_handler(event):
    user_id = event.chat_id
    if not await is_license_admin(user_id):
        await event.respond("❌ هذا الأمر متاح للمطور والمشرفين فقط.")
        return

    parts = (event.raw_text or "").split()
    if len(parts) >= 2:
        try:
            duration_days = int(parts[1])
        except ValueError:
            await event.respond("❌ المدة يجب أن تكون رقماً بالأيام.")
            return
    else:
        async with event.client.conversation(user_id) as conv:
            active_conversations[user_id] = conv
            try:
                await conv.send_message("أرسل مدة صلاحية المفتاح بالأيام:", buttons=cancel_operation_keyboard())
                response = await conv.get_response(timeout=300)
                duration_days = int(response.text.strip())
            except ValueError:
                await conv.send_message("❌ المدة يجب أن تكون رقماً صحيحاً.")
                return
            except asyncio.TimeoutError:
                await conv.send_message("⏱ انتهى وقت الإدخال.")
                return
            finally:
                if user_id in active_conversations:
                    del active_conversations[user_id]

    if duration_days <= 0:
        await event.respond("❌ يجب أن تكون المدة أكبر من صفر.")
        return

    license_key = await create_license(duration_days, user_id)
    await event.respond(
        "✅ تم إنشاء مفتاح جديد:\n\n"
        f"`{license_key}`\n\n"
        f"• المدة: {duration_days} يوم",
        parse_mode="markdown"
    )

@events.register(events.NewMessage(pattern=r"^/licenses$", func=lambda e: e.is_private))
async def list_licenses_handler(event):
    user_id = event.chat_id
    if not await is_license_admin(user_id):
        await event.respond("❌ هذا الأمر متاح للمطور والمشرفين فقط.")
        return

    rows = await list_licenses(50)
    if not rows:
        await event.respond("لا توجد مفاتيح حتى الآن.")
        return

    lines = ["🔑 آخر المفاتيح:\n"]
    for row in rows:
        state = license_state_text(row)
        used_by = f" | المستخدم: {row[4]}" if row[4] else ""
        expires_at = f" | ينتهي: {row[6]}" if row[6] else ""
        lines.append(f"• `{row[0]}` | {row[1]} يوم | {state}{used_by}{expires_at}")
    await event.respond("\n".join(lines), parse_mode="markdown")

@events.register(events.NewMessage(pattern=r"^/revokelicense(?:\s+(.+))?$", func=lambda e: e.is_private))
async def revoke_license_handler(event):
    user_id = event.chat_id
    if not await is_license_admin(user_id):
        await event.respond("❌ هذا الأمر متاح للمطور والمشرفين فقط.")
        return

    parts = (event.raw_text or "").split(maxsplit=1)
    if len(parts) < 2:
        await event.respond("أرسل المفتاح بعد الأمر:\n`/revokelicense LICENSE-KEY`", parse_mode="markdown")
        return

    revoked = await revoke_license(parts[1])
    await event.respond("✅ تم إلغاء المفتاح وتعطيل ترخيص مستخدمه." if revoked else "❌ لم يتم العثور على المفتاح.")

@events.register(events.NewMessage(pattern=r"^/clearlicenses$", func=lambda e: e.is_private))
async def clear_expired_licenses_handler(event):
    user_id = event.chat_id
    if not await is_license_admin(user_id):
        await event.respond("❌ هذا الأمر متاح للمطور والمشرفين فقط.")
        return

    count = await delete_expired_licenses()
    await event.respond(f"✅ تم حذف {count} مفتاح منتهي.")

@events.register(events.NewMessage(pattern=r"^/licensestats$", func=lambda e: e.is_private))
async def license_stats_handler(event):
    user_id = event.chat_id
    if not await is_license_admin(user_id):
        await event.respond("❌ هذا الأمر متاح للمطور والمشرفين فقط.")
        return

    stats = await get_license_stats()
    await event.respond(
        "📊 إحصائيات التراخيص:\n\n"
        f"• المستخدمون النشطون: {stats['active_users']}\n"
        f"• المفاتيح المستخدمة: {stats['used_keys']}\n"
        f"• المفاتيح غير المستخدمة: {stats['unused_keys']}\n"
        f"• المفاتيح الملغاة: {stats['revoked_keys']}\n"
        f"• المفاتيح المنتهية: {stats['expired_keys']}"
    )

@events.register(events.NewMessage(pattern=r"^/exportlicenses$", func=lambda e: e.is_private))
async def export_licenses_handler(event):
    user_id = event.chat_id
    if not await is_license_admin(user_id):
        await event.respond("❌ هذا الأمر متاح للمطور والمشرفين فقط.")
        return

    rows = await list_licenses(100000)
    file_path = f"/tmp/licenses_export_{user_id}.txt"
    with open(file_path, "w", encoding="utf-8") as export_file:
        export_file.write("license_key | duration_days | status | created_by | created_at | used_by | used_at | expires_at\n")
        for row in rows or []:
            export_file.write(
                f"{row[0]} | {row[1]} | {license_state_text(row)} | {row[2]} | {row[3]} | "
                f"{row[4] or ''} | {row[5] or ''} | {row[6] or ''}\n"
            )

    await event.client.send_file(user_id, file_path, caption="📄 تصدير مفاتيح الترخيص")
    try:
        os.remove(file_path)
    except OSError:
        pass

async def license_menu_handler(event):
    user_id = event.chat_id
    if not await is_license_admin(user_id):
        await event.answer("❌ هذا القسم متاح للمطور والمشرفين فقط.", alert=True)
        return

    await event.edit("🔑 قسم المفاتيح - إدارة تراخيص استخدام البوت", buttons=license_settings_keyboard())

async def license_create_callback(event):
    user_id = event.chat_id
    if not await is_license_admin(user_id):
        await event.answer("❌ هذا القسم متاح للمطور والمشرفين فقط.", alert=True)
        return

    async with event.client.conversation(user_id) as conv:
        active_conversations[user_id] = conv
        try:
            await conv.send_message("أرسل مدة صلاحية المفتاح بالأيام:", buttons=cancel_operation_keyboard())
            response = await conv.get_response(timeout=300)
            duration_days = int(response.text.strip())
            if duration_days <= 0:
                await conv.send_message("❌ يجب أن تكون المدة أكبر من صفر.")
                return

            license_key = await create_license(duration_days, user_id)
            await conv.send_message(
                "✅ تم إنشاء مفتاح جديد:\n\n"
                f"`{license_key}`\n\n"
                f"• المدة: {duration_days} يوم",
                parse_mode="markdown",
                buttons=license_settings_keyboard()
            )
        except ValueError:
            await conv.send_message("❌ المدة يجب أن تكون رقماً صحيحاً.")
        except asyncio.TimeoutError:
            await conv.send_message("⏱ انتهى وقت الإدخال.")
        finally:
            if user_id in active_conversations:
                del active_conversations[user_id]

async def license_list_callback(event):
    user_id = event.chat_id
    if not await is_license_admin(user_id):
        await event.answer("❌ هذا القسم متاح للمطور والمشرفين فقط.", alert=True)
        return

    rows = await list_licenses(30)
    if not rows:
        await event.edit("لا توجد مفاتيح حتى الآن.", buttons=license_settings_keyboard())
        return

    lines = ["🔑 آخر المفاتيح:\n"]
    for row in rows:
        state = license_state_text(row)
        used_by = f" | المستخدم: {row[4]}" if row[4] else ""
        expires_at = f" | ينتهي: {row[6]}" if row[6] else ""
        lines.append(f"• `{row[0]}` | {row[1]} يوم | {state}{used_by}{expires_at}")

    text = "\n".join(lines)
    if len(text) > 3800:
        text = text[:3800] + "\n\n... تم اختصار القائمة، استخدم زر التصدير لعرض الكل."
    await event.edit(text, parse_mode="markdown", buttons=license_settings_keyboard())

async def license_revoke_callback(event):
    user_id = event.chat_id
    if not await is_license_admin(user_id):
        await event.answer("❌ هذا القسم متاح للمطور والمشرفين فقط.", alert=True)
        return

    async with event.client.conversation(user_id) as conv:
        active_conversations[user_id] = conv
        try:
            await conv.send_message("أرسل المفتاح الذي تريد إلغاءه:", buttons=cancel_operation_keyboard())
            response = await conv.get_response(timeout=300)
            revoked = await revoke_license(response.text.strip())
            await conv.send_message(
                "✅ تم إلغاء المفتاح وتعطيل ترخيص مستخدمه." if revoked else "❌ لم يتم العثور على المفتاح.",
                buttons=license_settings_keyboard()
            )
        except asyncio.TimeoutError:
            await conv.send_message("⏱ انتهى وقت الإدخال.")
        finally:
            if user_id in active_conversations:
                del active_conversations[user_id]

async def license_clear_expired_callback(event):
    user_id = event.chat_id
    if not await is_license_admin(user_id):
        await event.answer("❌ هذا القسم متاح للمطور والمشرفين فقط.", alert=True)
        return

    count = await delete_expired_licenses()
    await event.edit(f"✅ تم حذف {count} مفتاح منتهي.", buttons=license_settings_keyboard())

async def license_stats_callback(event):
    user_id = event.chat_id
    if not await is_license_admin(user_id):
        await event.answer("❌ هذا القسم متاح للمطور والمشرفين فقط.", alert=True)
        return

    stats = await get_license_stats()
    await event.edit(
        "📊 إحصائيات التراخيص:\n\n"
        f"• المستخدمون النشطون: {stats['active_users']}\n"
        f"• المفاتيح المستخدمة: {stats['used_keys']}\n"
        f"• المفاتيح غير المستخدمة: {stats['unused_keys']}\n"
        f"• المفاتيح الملغاة: {stats['revoked_keys']}\n"
        f"• المفاتيح المنتهية: {stats['expired_keys']}",
        buttons=license_settings_keyboard()
    )

async def license_export_callback(event):
    user_id = event.chat_id
    if not await is_license_admin(user_id):
        await event.answer("❌ هذا القسم متاح للمطور والمشرفين فقط.", alert=True)
        return

    rows = await list_licenses(100000)
    file_path = f"/tmp/licenses_export_{user_id}.txt"
    with open(file_path, "w", encoding="utf-8") as export_file:
        export_file.write("license_key | duration_days | status | created_by | created_at | used_by | used_at | expires_at\n")
        for row in rows or []:
            export_file.write(
                f"{row[0]} | {row[1]} | {license_state_text(row)} | {row[2]} | {row[3]} | "
                f"{row[4] or ''} | {row[5] or ''} | {row[6] or ''}\n"
            )

    await event.client.send_file(user_id, file_path, caption="📄 تصدير مفاتيح الترخيص")
    await event.answer("✅ تم إرسال ملف التصدير.", alert=True)
    try:
        os.remove(file_path)
    except OSError:
        pass

async def license_notification_task(client):
    while True:
        try:
            rows = await get_license_notification_targets()
            now = datetime.datetime.now()
            for row in rows or []:
                user_id, license_key, expires_at_raw, reminded_3d, reminded_1d, expired_notified = row
                expires_at = parse_db_datetime(expires_at_raw)
                if not expires_at:
                    continue

                remaining = expires_at - now
                remaining_seconds = remaining.total_seconds()
                try:
                    if 0 < remaining_seconds <= 86400 and not reminded_1d:
                        await client.send_message(
                            user_id,
                            "⚠️ تنبيه: اشتراكك سينتهي خلال أقل من يوم واحد.\n"
                            f"• المفتاح: `{license_key}`\n"
                            f"• تاريخ الانتهاء: `{expires_at.strftime('%Y-%m-%d %H:%M')}`",
                            parse_mode="markdown"
                        )
                        await mark_license_reminder(user_id, "reminded_1d")
                    elif 86400 < remaining_seconds <= 259200 and not reminded_3d:
                        await client.send_message(
                            user_id,
                            "⚠️ تنبيه: اشتراكك سينتهي خلال أقل من 3 أيام.\n"
                            f"• المفتاح: `{license_key}`\n"
                            f"• تاريخ الانتهاء: `{expires_at.strftime('%Y-%m-%d %H:%M')}`",
                            parse_mode="markdown"
                        )
                        await mark_license_reminder(user_id, "reminded_3d")
                    elif remaining_seconds <= 0 and not expired_notified:
                        await execute_query(
                            "UPDATE user_licenses SET is_valid=0 WHERE user_id=?",
                            (user_id,),
                            commit=True
                        )
                        await client.send_message(
                            user_id,
                            "❌ انتهت صلاحية اشتراكك في البوت.\n\n"
                            f"• المفتاح: `{license_key}`\n"
                            "يرجى شراء مفتاح جديد من المطور وإرساله عبر:\n"
                            "`/activate YOUR-KEY`",
                            parse_mode="markdown"
                        )
                        await mark_license_reminder(user_id, "expired_notified")
                except Exception as exc:
                    logger.error(f"Failed to send license notification to {user_id}: {str(exc)}")
        except Exception as exc:
            logger.error(f"License notification task error: {str(exc)}")

        await asyncio.sleep(86400)

@events.register(events.NewMessage(pattern="/start", func=lambda e: e.is_private))
async def start_handler(event):
    user_id = event.chat_id
    if await is_banned(user_id):
        return
    
    # التحقق من الاشتراك في القنوات الإجبارية
    force_channels = await get_force_channels()
    for channel in force_channels:
        try:
            channel_entity = await event.client.get_input_entity(channel)
            await event.client(functions.channels.GetParticipantRequest(
                channel=channel_entity,
                participant=user_id
            ))
        except Exception:
            await event.respond(f"**⚠️︙عذراً عزيزي يجب عليك الاشتراك بقناة البوت**\n🚀︙القناه: @{channel}\n\n• اشترك في القناه ثم أرسل: /start")
            return

    # تسجيل المستخدم الجديد
    user_data = await get_user(user_id)
    if not user_data:
        await create_user(user_id)
        user_info = await event.client.get_entity(user_id)
        username = f"@{user_info.username}" if user_info.username else "None"
        await event.client.send_message(
            ADMIN_ID,
            f'• عضو جديد:\n- الاسم: <a href="tg://user?id={user_id}">{user_info.first_name}</a>\n- المعرف: {username}\n- الايدي: {user_id}',
            parse_mode="html"
        )
        user_data = await get_user(user_id)

    coins = user_data['coins'] if user_data else 0
    
    # تحضير الأزرار
    await event.respond(msgs['START_MESSAGE'].format(
        user_id, 
        coins
    ), buttons=start_keyboard(user_id, await is_admin(user_id)))

    if user_id != ADMIN_ID:
        allowed, status = await user_has_license_access(user_id)
        if not allowed:
            await event.respond(
                license_required_message(status.get("reason"), status.get("expires_at")),
                parse_mode="markdown"
            )
        elif status.get("reason") == "trial":
            await event.respond(
                "🎁 لديك فترة مجانية لاستخدام البوت لمدة 24 ساعة.\n\n"
                f"• تنتهي في: `{status['expires_at'].strftime('%Y-%m-%d %H:%M')}`\n"
                f"• المتبقي: {format_remaining_time(status['expires_at'])}",
                parse_mode="markdown"
            )

@events.register(events.CallbackQuery())
async def callback_handler(event):
    data = event.data.decode('utf-8')
    user_id = event.chat_id
    
    if await is_banned(user_id):
        return

    if not await enforce_license_for_callback(event):
        return
    
    # التحقق من الاشتراك في القنوات الإجبارية
    force_channels = await get_force_channels()
    for channel in force_channels:
        try:
            channel_entity = await event.client.get_input_entity(channel)
            await event.client(functions.channels.GetParticipantRequest(
                channel=channel_entity,
                participant=user_id
            ))
        except Exception:
            await event.answer("عليك الاشتراك في القناة أولاً!", alert=True)
            return

    admin_only_actions = {
        "set_trust_channel",
        "license_menu",
        "license_create",
        "license_list",
        "license_revoke",
        "license_clear_expired",
        "license_stats",
        "license_export",
        "add_vip",
    }
    if data in admin_only_actions and user_id != ADMIN_ID and not await is_admin(user_id):
        await event.answer("❌ هذه العملية متاحة للمطور والمشرفين فقط.", alert=True)
        return

    account_service_actions = {"funding", "mass_react", "mass_vote", "mass_comment", "increment_views"}
    if data in account_service_actions:
        accounts = await get_all_stored_accounts() if user_id == ADMIN_ID else await get_stored_accounts(user_id)
        if not accounts:
            await event.answer("❌ ليس لديك حسابات مخزنة!" if user_id != ADMIN_ID else "❌ لا توجد حسابات مخزنة داخل البوت!", alert=True)
            return

    # معالجة الأحداث
    if data == "ajxjao":
        await num_settings(event)
    elif data == "change_price":
        await change_buy_price_menu(event)
    elif data == "change_sell_price":
        await change_sell_price_menu(event)
    elif data == "next_buy_price_countries":
        await next_price_countries_page(event, "buy")
    elif data == "prev_buy_price_countries":
        await prev_price_countries_page(event, "buy")
    elif data == "next_sell_price_countries":
        await next_price_countries_page(event, "sell")
    elif data == "prev_sell_price_countries":
        await prev_price_countries_page(event, "sell")
    elif data == "ajxkho":
        await force_settings(event)
    elif data == "aksgl":
        await admin_settings(event)
    elif data == "ajkofgl":
        await buy_sell_settings(event)
    elif data == "ajkcoingl":
        await balance_settings(event)
    elif data == "bbvjls":
        await ban_settings(event)
    elif data == "set_trust_channel":
        await set_trust_channel(event)
    elif data == "license_menu":
        await license_menu_handler(event)
    elif data == "license_create":
        await license_create_callback(event)
    elif data == "license_list":
        await license_list_callback(event)
    elif data == "license_revoke":
        await license_revoke_callback(event)
    elif data == "license_clear_expired":
        await license_clear_expired_callback(event)
    elif data == "license_stats":
        await license_stats_callback(event)
    elif data == "license_export":
        await license_export_callback(event)
    elif data == "edit_rules":
        await edit_rules(event)
    elif data == "numbers_menu":
        await numbers_menu(event)
    elif data == "control_menu":
        await control_menu(event)
    elif data == "buy":
        await buy_number(event)
    elif data == "ssart":
        await withdraw_balance(event)
    elif data == "transfer":
        await transfer_balance(event)
    elif data == "supper":
        await support_request(event)
    elif data == "liscgh":
        await show_rules(event)
    elif data == "sell":
        await sell_account(event)
    elif data == "admin_panel":
        await admin_panel(event)
    elif data == "control_settings_super":
        await super_settings_menu(event)
    elif data == "control_settings_posting":
        await posting_settings_menu(event)
    elif data == "control_settings_creation":
        await creation_settings_menu(event)
    elif data == "control_settings_account":
        await account_settings_menu(event)
    elif data == "add_super":
        await add_super_channel_handler(event)
    elif data == "del_super":
        await del_super_channel_handler(event)
    elif data == "show_supers":
        await show_super_channels(event)
    elif data == "clear_supers":
        await clear_super_channels(event)
    elif data == "add_template":
        await add_posting_template_handler(event)
    elif data == "del_template":
        await del_posting_template_handler(event)
    elif data == "show_templates":
        await show_posting_templates(event)
    elif data == "clear_templates":
        await clear_posting_templates_handler(event)
    elif data == "edit_template":
        await edit_posting_template_handler(event)
    elif data.startswith("del_template:"):
        await delete_posting_template_callback(event, data)
    elif data.startswith("edit_template:"):
        await edit_posting_template_callback(event, data)
    elif data == "enable_multi_posting":
        await set_posting_setting(event, "multi_posting", "1")
    elif data == "disable_multi_posting":
        await set_posting_setting(event, "multi_posting", "0")
    elif data == "start_posting":
        await start_posting_handler(event)
    elif data == "stop_posting_group":
        await stop_posting_group_handler(event)
    elif data.startswith("stop_posting:"):
        await stop_posting_channel_handler(event, data)
    elif data == "stop_all_posting":
        await stop_all_posting_handler(event)
    elif data == "manual_creation":
        await manual_group_creation_handler(event)
    elif data == "stop_manual_creation":
        await stop_manual_creation_handler(event)
    elif data == "auto_creation":
        await auto_group_creation_handler(event)
    elif data == "stop_auto_creation":
        await stop_auto_creation_handler(event)
    elif data == "timed_name_on":
        await toggle_timed_name(event, True)
    elif data == "timed_name_off":
        await toggle_timed_name(event, False)
    elif data == "change_profile_photo":
        await change_profile_photo(event)
    elif data == "change_bio":
        await profile_edit_account_menu(event, "bio")
    elif data == "change_username":
        await profile_edit_account_menu(event, "username")
    elif data.startswith("profile_edit_account:"):
        await profile_edit_account_selected(event, data)
    elif data == "enable_stealth_mode":
        await enable_stealth_mode(event)
    elif data == "disable_stealth_mode":
        await disable_stealth_mode(event)
    elif data == "enable_notifications":
        await enable_notifications(event)
    elif data == "disable_notifications":
        await disable_notifications(event)
    elif data == "add_mandatory_channel":
        await add_mandatory_channel_handler(event)
    elif data == "add_vip":
        await add_vip_callback(event)
    elif data == "funding":
        await funding_handler(event)
    elif data in {
        "disable_self_destruct_save",
        "enable_self_destruct_save",
        "flash_members",
        "stop_flash_members",
        "show_monitors",
        "add_monitor_targets",
        "disable_monitoring",
        "enable_monitoring",
    }:
        await unsupported_account_feature(event)
    elif data == "save_restricted_post":
        await save_post_account_menu(event)
    elif data.startswith("save_post_account:"):
        await save_post_account_selected(event, data)
    elif data == "set_storage_group":
        await storage_account_menu(event, "set_group")
    elif data == "disable_incoming_storage":
        await storage_account_menu(event, "disable")
    elif data == "enable_incoming_storage":
        await storage_account_menu(event, "enable")
    elif data.startswith("storage_account:"):
        await storage_account_selected(event, data)
    elif data == "leave_groups_menu":
        await leave_groups_account_menu(event)
    elif data.startswith("leave_groups_account:"):
        await leave_groups_account_selected(event, data)
    elif data == "leave_all_groups_execute":
        await leave_all_groups_execute(event)
    elif data == "leave_channels_menu":
        await leave_channels_account_menu(event)
    elif data.startswith("leave_channels_account:"):
        await leave_channels_account_selected(event, data)
    elif data == "leave_specific_channel_menu":
        await leave_specific_channel_menu(event)
    elif data.startswith("leave_specific_channel:"):
        await leave_specific_channel_selected(event, data)
    elif data == "leave_all_channels_confirm":
        await leave_all_channels_confirm(event)
    elif data == "leave_all_channels_execute":
        await leave_all_channels_execute(event)
    elif data == "install_session":
        await install_session_handler(event)
    elif data.startswith("country_"):
        await country_selected(event, data)
    elif data.startswith("select_account_"):
        await select_account(event, data)
    elif data.startswith("buy_"):
        await buy_confirmed(event, data)
    elif data.startswith("confirm_purchase:"):
        await confirm_purchase_handler(event, data)
    elif data.startswith("store_account:"):
        await store_account_handler(event, data)
    elif data.startswith("logout:"):
        await logout_account(event, data)
    elif data.startswith("sell_logout_"):
        await sell_logout_handler(event, data)
    elif data.startswith("show_"):
        await show_accounts(event, data)
    elif data.startswith("v:"):
        await account_details(event, data)
    elif data.startswith("del:"):
        await del_account_confirm(event, data)
    elif data.startswith("del_done:"):
        await del_account_done(event, data)
    elif data.startswith("rig_"):
        await add_number_process(event, data)
    elif data.startswith("delete_"):
        await del_country(event, data)
    elif data.startswith("next_sell:"):
        await next_sell(event, data)
    elif data.startswith("check:"):
        await check_account(event, data)
    elif data.startswith("chs_"):
        await change_sell_price(event, data)
    elif data.startswith("chg_"):
        await change_buy_price(event, data)
    elif data == "add_force":
        await add_force_channel_handler(event)
    elif data == "del_force":
        await del_force_channel_handler(event)
    elif data == "add_admin":
        await add_admin(event)
    elif data == "del_admin":
        await del_admin(event)
    elif data == "add_coins":
        await add_coins(event)
    elif data == "del_coins":
        await del_coins(event)
    elif data == "ban":
        await ban_user(event)
    elif data == "unban":
        await unban_user(event)
    elif data.startswith("confirm_withdraw_"):
        await confirm_withdraw(event, data)
    elif data.startswith("reply_"):
        await reply_to_user(event, data)
    elif data == "zip_all":
        await zip_database(event)
    elif data == "all_of_number":
        await all_numbers_count(event)
    elif data == "add_country":
        await add_country_handler(event)
    elif data == "del_country":
        await del_country_menu(event)
    elif data == "add":
        await add_number_menu(event)
    elif data == "next_add_number_countries":
        await next_add_number_countries_page(event)
    elif data == "prev_add_number_countries":
        await prev_add_number_countries_page(event)
    elif data == "del_account":
        await del_account_menu(event)
    elif data == "cancel_operation":
        await cancel_operation(event)
    elif data == "main":
        await main_menu(event)
    elif data == "back":
        await buy_number(event)
    elif data == "accounts_view":
        await accounts_view_menu(event)
    elif data == "view_stored":
        await view_stored_accounts(event)
    elif data == "view_purchased":
        await view_purchased_accounts(event)
    elif data == "view_sold":
        await view_sold_accounts(event)
    elif data == "view_active":
        await view_active_accounts(event)
    elif data == "next_page_countries":
        await next_countries_page(event)
    elif data == "prev_page_countries":
        await prev_countries_page(event)
    elif data.startswith("account_actions:"):
        await account_actions_menu(event, data)
    elif data.startswith("purchased_account:") or data.startswith("bot_account:"):
        await show_account_options(event, data)
    elif data.startswith("select_acc_for_posting:"):
        await select_account_for_posting(event, data)
    elif data == "posting_all":
        await posting_all_channels(event)
    elif data == "posting_specific":
        await select_channel_for_posting(event)
    elif data.startswith("select_channel:"):
        await posting_specific_channel(event, data)
    elif data == "broadcast_message":
        await broadcast_start(event)
    elif data.startswith("del_super_channel:"):
        await handle_delete_super_channel(event, data)
    # إضافة معالجات أزرار التنصيب والمزاد
    elif data == "install_menu":
        await install_menu_handler(event)
    elif data == "delete_install":
        await delete_install_handler(event)
    elif data.startswith("del_install:"):
        await delete_install_account(event, data)
    elif data == "auction_menu":
        await auction_menu_handler(event)
    elif data == "add_auction":
        await add_auction_handler(event)
    elif data == "auction_list":
        await auction_list_handler(event)
    elif data.startswith("view_auction:"):
        await view_auction_handler(event, data)
    elif data.startswith("bid:"):
        await place_bid_handler(event, data)
    elif data.startswith("sell_auction:"):
        await sell_auction_handler(event, data)
    elif data.startswith("continue_auction:"):
        await continue_auction_handler(event, data)
    elif data.startswith("auction_logout:"):
        await auction_logout_handler(event, data)
    elif data == "mass_react":
        await mass_react_handler(event)
    elif data == "mass_vote":
        await mass_vote_handler(event)
    elif data == "mass_comment":
        await mass_comment_handler(event)
    elif data == "increment_views":
        await increment_views_handler(event)
    elif data.startswith("purchased_account:") or data.startswith("stored_account:") or data.startswith("bot_account:"):
        await show_account_options(event, data)
        
    elif data.startswith("store_existing:"):
        phone = data.split(':')[1]
        await store_existing_account(event, phone)

    elif data.startswith("get_code:"):
        parts = data.split(':')
        phone = parts[1]
        await get_verification_code(event, phone)

    elif data.startswith("logout_sessions:"):
        parts = data.split(':')
        phone = parts[1]
        await logout_account_sessions(event, phone)
        
# ===== وظائف القوائم الرئيسية =====
async def show_account_options(event, data):
    parts = data.split(':')
    account_type = parts[0]
    phone = parts[1]
    user_id = event.chat_id
    
    # الحصول على معلومات الحساب
    if account_type == "purchased_account":
        account_info = await get_purchased_account_info(phone)
        if account_info and user_id != ADMIN_ID and account_info[2] != user_id:
            account_info = None
    elif account_type == "stored_account":
        account_info = await get_stored_account_info(phone)
        if account_info and user_id != ADMIN_ID and account_info[2] != user_id:
            account_info = None
    elif account_type == "bot_account" and user_id == ADMIN_ID:
        account_info = await get_account_info(phone)
    else:
        account_info = None
    
    if not account_info:
        await event.answer("❌ لم يتم العثور على الحساب!", alert=True)
        return
    
    if account_type in ("stored_account", "bot_account"):
        twofa = account_info[3] if len(account_info) > 3 and account_info[3] else "لا يوجد"
    elif account_type == "purchased_account":
        twofa = account_info[8] if len(account_info) > 8 and account_info[8] else "لا يوجد"
    else:
        twofa = "لا يوجد"
    
    buttons = arrange_buttons([
        Button.inline("• تخزين الرقم • 💾", data=f"store_existing:{phone}"),
        Button.inline("• الحصول على الكود • 🔐", data=f"get_code:{phone}"),
        Button.inline("• إنهاء الجلسات • 🚪", data=f"logout_sessions:{phone}"),
        Button.inline("• رجوع • ↩️", data="accounts_view")
    ])
    
    await event.edit(
        f"**خيارات الحساب:**\n\n"
        f"📱 الرقم: +{phone}\n"
        f"🔑 كلمة المرور: {twofa}\n\n"
        f"اختر الإجراء المطلوب:",
        buttons=buttons
    )

async def account_actions_menu(event, data):
    parts = data.split(':')
    if len(parts) < 2:
        await event.answer("❌ بيانات الحساب غير مكتملة!", alert=True)
        return

    phone = parts[-1]
    if len(parts) >= 3 and parts[1] in ("purchased", "stored"):
        account_type = "purchased_account" if parts[1] == "purchased" else "stored_account"
    elif await get_purchased_account_info(phone):
        account_type = "purchased_account"
    elif await get_stored_account_info(phone):
        account_type = "stored_account"
    else:
        await event.answer("❌ لم يتم العثور على الحساب!", alert=True)
        return

    await show_account_options(event, f"{account_type}:{phone}")

async def get_account_for_user_action(user_id, phone):
    if user_id == ADMIN_ID:
        return (
            await get_stored_account_info(phone)
            or await get_purchased_account_info(phone)
            or await get_account_info(phone)
        )

    account = await get_stored_account_info(phone)
    if account and account[2] == user_id:
        return account

    account = await get_purchased_account_info(phone)
    if account and account[2] == user_id:
        return account

    return None

async def get_account_twofa_for_user_action(user_id, phone):
    account = await get_stored_account_info(phone)
    if account and (user_id == ADMIN_ID or account[2] == user_id):
        return account[3] if len(account) > 3 and account[3] else "لا يوجد"

    account = await get_purchased_account_info(phone)
    if account and (user_id == ADMIN_ID or account[2] == user_id):
        return account[8] if len(account) > 8 and account[8] else "لا يوجد"

    if user_id == ADMIN_ID:
        account = await get_account_info(phone)
        if account:
            return account[3] if len(account) > 3 and account[3] else "لا يوجد"

    return "لا يوجد"

async def get_service_accounts_for_user(user_id):
    if user_id == ADMIN_ID:
        return await get_all_stored_accounts()
    return await get_stored_accounts(user_id)

async def validate_service_count(conv, accounts, count):
    if count <= 0:
        await conv.send_message("❌ العدد يجب أن يكون أكبر من صفر!")
        return False
    if not accounts:
        await conv.send_message("❌ ليس لديك حسابات مخزنة!")
        return False
    if count > len(accounts):
        await conv.send_message(f"❌ عدد الحسابات المتاحة هو {len(accounts)} فقط!")
        return False
    return True

async def mass_react_handler(event):
    """معالج التفاعل الجماعي"""
    user_id = event.chat_id
    async with event.client.conversation(user_id) as conv:
        active_conversations[user_id] = conv
        try:
            await conv.send_message("أرسل رابط المنشور:", buttons=cancel_operation_keyboard())
            url = await conv.get_response(timeout=300)
            
            await conv.send_message("أرسل الإيموجي المطلوب (مثال: ❤️):", buttons=cancel_operation_keyboard())
            emoji = await conv.get_response(timeout=300)
            
            await conv.send_message("أرسل عدد الحسابات التي تريد استخدامها:", buttons=cancel_operation_keyboard())
            count_resp = await conv.get_response(timeout=300)
            
            try:
                count = int(count_resp.text)
                accounts = await get_service_accounts_for_user(user_id)
                if not await validate_service_count(conv, accounts, count):
                    return
                success = await mass_react(user_id, url.text, emoji.text, count, accounts=accounts)
                await conv.send_message(f"✅ تم التفاعل باستخدام {success} حساب!")
            except ValueError:
                await conv.send_message("عدد غير صحيح!")
        except asyncio.TimeoutError:
            await conv.send_message("⏱ انتهى الوقت المحدد للإدخال!")
        finally:
            if user_id in active_conversations:
                del active_conversations[user_id]

async def mass_vote_handler(event):
    """معالج التصويت الجماعي"""
    user_id = event.chat_id
    async with event.client.conversation(user_id) as conv:
        active_conversations[user_id] = conv
        try:
            await conv.send_message("أرسل رابط الاستفتاء:", buttons=cancel_operation_keyboard())
            url = await conv.get_response(timeout=300)
            
            await conv.send_message("أرسل رقم الخيار المراد التصويت له:", buttons=cancel_operation_keyboard())
            option = await conv.get_response(timeout=300)
            
            await conv.send_message("أرسل عدد الحسابات التي تريد استخدامها:", buttons=cancel_operation_keyboard())
            count_resp = await conv.get_response(timeout=300)
            
            try:
                count = int(count_resp.text)
                accounts = await get_service_accounts_for_user(user_id)
                if not await validate_service_count(conv, accounts, count):
                    return
                success = await mass_vote(user_id, url.text, int(option.text), count, accounts=accounts)
                await conv.send_message(f"✅ تم التصويت باستخدام {success} حساب!")
            except ValueError:
                await conv.send_message("قيمة غير صالحة!")
        except asyncio.TimeoutError:
            await conv.send_message("⏱ انتهى الوقت المحدد للإدخال!")
        finally:
            if user_id in active_conversations:
                del active_conversations[user_id]

async def store_existing_account(event, phone):
    user_id = event.chat_id
    account = await get_account_for_user_action(user_id, phone)
    if not account:
        await event.answer("❌ لم يتم العثور على الحساب!", alert=True)
        return
    
    session_str = account[1]
    twofa = await get_account_twofa_for_user_action(user_id, phone)
    await add_stored_account(phone, session_str, user_id, twofa)
    if user_id == ADMIN_ID and await get_account_info(phone):
        await delete_account(phone)
        await event.answer("✅ تم سحب الرقم وتخزينه لدى المطور وإزالته من البيع!", alert=True)
        return

    await event.answer("✅ تم تخزين الحساب بنجاح!", alert=True)

async def get_verification_code(event, phone):
    client = None
    try:
        account = await get_account_for_user_action(event.chat_id, phone)
        if not account:
            await event.answer("❌ لم يتم العثور على الحساب", alert=True)
            return
        session_str = account[1]
        if not session_str:
            await event.answer("❌ لا توجد جلسة محفوظة لهذا الحساب", alert=True)
            return

        client = TelegramClient(StringSession(session_str), API_ID, API_HASH)
        await client.connect()

        if not await client.is_user_authorized():
            await event.answer("❌ جلسة هذا الرقم منتهية. أعد إضافة الحساب.", alert=True)
            return

        notifications_entity = types.InputPeerUser(TELEGRAM_NOTIFICATIONS_USER_ID, 0)
        messages = await client.get_messages(notifications_entity, limit=10)

        for message in messages:
            text = message.message or ""
            code_match = re.search(r'\b\d{5,6}\b', text)
            if code_match:
                code = code_match.group()
                await event.answer(f"✅ تم العثور على الكود: {code}", alert=True)
                return

        await event.answer("❌ لم يتم العثور على كود حديث في رسائل Telegram", alert=True)

    except (
        AuthKeyInvalidError,
        AuthKeyUnregisteredError,
        SessionExpiredError,
        SessionRevokedError,
        UnauthorizedError,
        UserDeactivatedBanError,
        UserDeactivatedError,
    ) as e:
        logger.warning(f"Invalid session while getting code for {phone}: {str(e)}")
        await event.answer("❌ جلسة هذا الرقم ملغاة. أعد إضافة الحساب.", alert=True)
    except Exception as e:
        logger.error(f"Error getting verification code: {str(e)}")
        await event.answer("❌ حدث خطأ أثناء محاولة الحصول على الكود", alert=True)
    finally:
        if client and client.is_connected():
            await client.disconnect()

async def logout_account_sessions(event, phone):
    account = await get_account_for_user_action(event.chat_id, phone)
    if not account:
        await event.answer("❌ لم يتم العثور على الحساب", alert=True)
        return
    session_str = account[1]
    success = await logout_all_sessions(session_str)
    if success:
        await event.answer("✅ تم إنهاء جميع الجلسات بنجاح!", alert=True)
    else:
        await event.answer("❌ فشل في إنهاء الجلسات!", alert=True)

async def mass_comment_handler(event):
    """معالج التعليق الجماعي"""
    user_id = event.chat_id
    async with event.client.conversation(user_id) as conv:
        active_conversations[user_id] = conv
        try:
            await conv.send_message("أرسل رابط المنشور:", buttons=cancel_operation_keyboard())
            url = await conv.get_response(timeout=300)
            
            await conv.send_message("أرسل نص التعليق:", buttons=cancel_operation_keyboard())
            comment = await conv.get_response(timeout=300)
            
            await conv.send_message("أرسل عدد الحسابات التي تريد استخدامها:", buttons=cancel_operation_keyboard())
            count_resp = await conv.get_response(timeout=300)
            
            try:
                count = int(count_resp.text)
                accounts = await get_service_accounts_for_user(user_id)
                if not await validate_service_count(conv, accounts, count):
                    return
                success = await mass_comment(user_id, url.text, comment.text, count, accounts=accounts)
                if success == 0:
                    await conv.send_message("❌ لم يتم إرسال أي تعليق. تأكد أن المنشور يدعم التعليقات وأن الحسابات لديها صلاحية الكتابة في مجموعة النقاش.")
                    return
                await conv.send_message(f"✅ تم التعليق باستخدام {success} حساب!")
            except ValueError:
                await conv.send_message("عدد غير صحيح!")
        except asyncio.TimeoutError:
            await conv.send_message("⏱ انتهى الوقت المحدد للإدخال!")
        finally:
            if user_id in active_conversations:
                del active_conversations[user_id]

async def increment_views_handler(event):
    """معالج زيادة المشاهدات"""
    user_id = event.chat_id
    async with event.client.conversation(user_id) as conv:
        active_conversations[user_id] = conv
        try:
            await conv.send_message("أرسل رابط المنشور:", buttons=cancel_operation_keyboard())
            url = await conv.get_response(timeout=300)
            
            await conv.send_message("أرسل عدد المشاهدات المطلوبة:", buttons=cancel_operation_keyboard())
            count_resp = await conv.get_response(timeout=300)
            
            try:
                count = int(count_resp.text)
                accounts = await get_service_accounts_for_user(user_id)
                if not await validate_service_count(conv, accounts, count):
                    return
                success = await increment_views(user_id, url.text, count, accounts=accounts)
                await conv.send_message(f"✅ تم زيادة المشاهدات بمقدار {success}!")
            except ValueError:
                await conv.send_message("عدد غير صحيح!")
        except asyncio.TimeoutError:
            await conv.send_message("⏱ انتهى الوقت المحدد للإدخال!")
        finally:
            if user_id in active_conversations:
                del active_conversations[user_id]

async def edit_or_respond(event, text, buttons=None):
    try:
        await event.edit(text, buttons=buttons)
    except Exception:
        await event.respond(text, buttons=buttons)

async def change_price_country_menu(event, mode):
    user_id = event.chat_id
    countries = await get_countries()
    if not countries:
        await edit_or_respond(event, "⭕️ لا توجد دول متاحة حالياً")
        return

    if mode not in ("buy", "sell"):
        await event.answer("❌ نوع السعر غير صالح!", alert=True)
        return

    page_key = (user_id, mode)
    if page_key not in price_country_pages:
        price_country_pages[page_key] = 0

    per_page = 20
    current_page = price_country_pages[page_key]
    max_page = max(0, (len(countries) - 1) // per_page)
    if current_page > max_page:
        current_page = max_page
        price_country_pages[page_key] = current_page

    start_index = current_page * per_page
    end_index = min(start_index + per_page, len(countries))
    country_chunk = countries[start_index:end_index]

    prefix = "chg" if mode == "buy" else "chs"
    price_index = 2 if mode == "buy" else 3
    price_label = "شراء" if mode == "buy" else "بيع"
    message = "اختر الدولة لتغيير سعر الشراء:" if mode == "buy" else "اختر الدولة لتغيير سعر البيع:"

    item_buttons = []
    for i in range(0, len(country_chunk), 2):
        c1 = country_chunk[i]
        item_buttons.append(Button.inline(f"{c1[1]}: {c1[price_index]}$", data=f"{prefix}_{c1[0]}"))
        if i + 1 < len(country_chunk):
            c2 = country_chunk[i + 1]
            item_buttons.append(Button.inline(f"{c2[1]}: {c2[price_index]}$", data=f"{prefix}_{c2[0]}"))

    buttons = arrange_buttons(item_buttons)

    nav_buttons = []
    if current_page > 0:
        nav_buttons.append(Button.inline("◀ السابق", data=f"prev_{mode}_price_countries"))
    if end_index < len(countries):
        nav_buttons.append(Button.inline("التالي ▶", data=f"next_{mode}_price_countries"))
    if nav_buttons:
        buttons.append(nav_buttons)

    buttons.append([Button.inline("• رجوع • ↩️", data="ajkofgl")])
    await edit_or_respond(
        event,
        f"{message}\n\n"
        f"• الصفحة: {current_page + 1}/{max_page + 1}\n"
        f"• نوع السعر: {price_label}",
        buttons=buttons
    )

async def change_buy_price_menu(event):
    price_country_pages[(event.chat_id, "buy")] = 0
    await change_price_country_menu(event, "buy")

async def change_sell_price_menu(event):
    price_country_pages[(event.chat_id, "sell")] = 0
    await change_price_country_menu(event, "sell")

async def next_price_countries_page(event, mode):
    user_id = event.chat_id
    countries = await get_countries()
    if not countries:
        await event.answer("⭕️ لا توجد دول متاحة حالياً", alert=True)
        return

    page_key = (user_id, mode)
    current_page = price_country_pages.get(page_key, 0)
    max_page = max(0, (len(countries) - 1) // 20)
    if current_page >= max_page:
        await event.answer("❗️ هذه آخر صفحة", alert=True)
        return

    price_country_pages[page_key] = current_page + 1
    await change_price_country_menu(event, mode)

async def prev_price_countries_page(event, mode):
    user_id = event.chat_id
    countries = await get_countries()
    if not countries:
        await event.answer("⭕️ لا توجد دول متاحة حالياً", alert=True)
        return

    page_key = (user_id, mode)
    current_page = price_country_pages.get(page_key, 0)
    if current_page <= 0:
        await event.answer("❗️ هذه أول صفحة", alert=True)
        return

    price_country_pages[page_key] = current_page - 1
    await change_price_country_menu(event, mode)
    
async def change_buy_price(event, data):
    parts = data.split('_')
    if len(parts) < 2:
        await event.answer("❌ بيانات الدولة غير صالحة!", alert=True)
        return
    calling_code = parts[1]
    
    # جلب بيانات الدولة من قاعدة البيانات
    country = await get_country(calling_code)
    if not country:
        await event.answer("❌ الدولة غير موجودة!", alert=True)
        return
        
    name = country[1]
    old_price = country[2]
    
    async with event.client.conversation(event.chat_id) as conv:
        active_conversations[event.chat_id] = conv
        try:
            await conv.send_message(f"أرسل سعر الشراء الجديد لـ {name}:\nالسعر الحالي: {old_price}$", buttons=cancel_operation_keyboard())
            new_price = await conv.get_response(timeout=300)
            
            try:
                await execute_query(
                    "UPDATE countries SET price=? WHERE calling_code=?",
                    (float(new_price.text), calling_code),
                    commit=True
                )
                await conv.send_message(f"✅ تم تحديث سعر الشراء لـ {name} إلى {new_price.text}$")
            except:
                await conv.send_message("❌ قيمة غير صالحة!")
        except asyncio.TimeoutError:
            await conv.send_message("⏱ انتهى الوقت المحدد للإدخال!")
        finally:
            if event.chat_id in active_conversations:
                del active_conversations[event.chat_id]

async def change_sell_price(event, data):
    parts = data.split('_')
    if len(parts) < 2:
        await event.answer("❌ بيانات الدولة غير صالحة!", alert=True)
        return
    calling_code = parts[1]
    
    # جلب بيانات الدولة من قاعدة البيانات
    country = await get_country(calling_code)
    if not country:
        await event.answer("❌ الدولة غير موجودة!", alert=True)
        return
        
    name = country[1]
    old_sell_price = country[3]
    
    async with event.client.conversation(event.chat_id) as conv:
        active_conversations[event.chat_id] = conv
        try:
            await conv.send_message(f"أرسل سعر البيع الجديد لـ {name}:\nالسعر الحالي: {old_sell_price}$", buttons=cancel_operation_keyboard())
            new_price = await conv.get_response(timeout=300)
            
            try:
                await execute_query(
                    "UPDATE countries SET sell_price=? WHERE calling_code=?",
                    (float(new_price.text), calling_code),
                    commit=True
                )
                await conv.send_message(f"✅ تم تحديث سعر البيع لـ {name} إلى {new_price.text}$")
            except:
                await conv.send_message("❌ قيمة غير صالحة!")
        except asyncio.TimeoutError:
            await conv.send_message("⏱ انتهى الوقت المحدد للإدخال!")
        finally:
            if event.chat_id in active_conversations:
                del active_conversations[event.chat_id]

async def num_settings(event):
    await event.edit(
        msgs['ADMIN_NUM_SETTINGS'],
        buttons=num_settings_keyboard()
    )

async def force_settings(event):
    await event.edit(
        msgs['ADMIN_FORCE_SETTINGS'],
        buttons=force_settings_keyboard()
    )

async def admin_settings(event):
    await event.edit(
        msgs['ADMIN_ADMIN_SETTINGS'],
        buttons=admin_settings_keyboard()
    )

async def buy_sell_settings(event):
    await event.edit(
        msgs['ADMIN_BUY_SELL_SETTINGS'],
        buttons=buy_sell_settings_keyboard()
    )

async def balance_settings(event):
    await event.edit(
        msgs['ADMIN_BALANCE_SETTINGS'],
        buttons=balance_settings_keyboard()
    )

async def ban_settings(event):
    await event.edit(
        msgs['ADMIN_BAN_SETTINGS'],
        buttons=ban_settings_keyboard()
    )

async def set_trust_channel(event):
    async with event.client.conversation(event.chat_id) as conv:
        active_conversations[event.chat_id] = conv
        await conv.send_message("أرسل معرف القناة:", buttons=cancel_operation_keyboard())
        try:
            response = await conv.get_response(timeout=300)
            channel = response.text.replace('https://t.me/', '').replace('@', '').strip()
            try:
                await event.client.send_message(channel, "تم تفعيل القناة بنجاح")
                await set_setting("trust_channel", channel)
                await conv.send_message("✅ تم تعيين قناة الثبات بنجاح")
            except:
                await conv.send_message("❌ فشل في تعيين القناة! تأكد من رفع البوت كمسؤول")
        except asyncio.TimeoutError:
            await conv.send_message("⏱ انتهى الوقت المحدد للإدخال!")
        finally:
            if event.chat_id in active_conversations:
                del active_conversations[event.chat_id]

async def edit_rules(event):
    async with event.client.conversation(event.chat_id) as conv:
        active_conversations[event.chat_id] = conv
        await conv.send_message("أرسل نص القوانين الجديد:", buttons=cancel_operation_keyboard())
        try:
            response = await conv.get_response(timeout=300)
            await set_setting("rules_message", response.text)
            await conv.send_message("✅ تم تحديث القوانين بنجاح")
        except asyncio.TimeoutError:
            await conv.send_message("⏱ انتهى الوقت المحدد للإدخال!")
        finally:
            if event.chat_id in active_conversations:
                del active_conversations[event.chat_id]

async def numbers_menu(event):
    await event.edit(msgs['NUMBERS_MENU'], buttons=numbers_menu_keyboard())

async def control_menu(event):
    await event.edit(msgs['CONTROL_MENU'], buttons=control_menu_keyboard())

async def buy_number(event):
    user_id = event.chat_id
    countries = await get_countries()
     # إذا لم توجد دول
    if not countries:
        await event.edit("⭕️ لا توجد دول متاحة حالياً")
        return
    # تهيئة التخزين المؤقت للصفحات
    if user_id not in user_pages:
        user_pages[user_id] = 0
    
    # تجهيز الأزرار للصفحة الحالية
    item_buttons = []
    start_index = user_pages[user_id] * 20
    end_index = min(start_index + 20, len(countries))
    
    # تنظيم الأزرار في صفوف (صفين لكل صف)
    country_chunk = countries[start_index:end_index]
    for i in range(0, len(country_chunk), 2):
        if i < len(country_chunk):
            c1 = country_chunk[i]
            item_buttons.append(Button.inline(f"{c1[1]}: {c1[2]}$", data=f"country_{c1[0]}"))
        if i+1 < len(country_chunk):
            c2 = country_chunk[i+1]
            item_buttons.append(Button.inline(f"{c2[1]}: {c2[2]}$", data=f"country_{c2[0]}"))

    buttons = arrange_buttons(item_buttons)
    
    # إضافة أزرار التنقل بين الصفحات
    nav_buttons = []
    if user_pages[user_id] > 0:
        nav_buttons.append(Button.inline("◀ السابق", data="prev_page_countries"))
    if end_index < len(countries):
        nav_buttons.append(Button.inline("التالي ▶", data="next_page_countries"))
    
    if nav_buttons:
        buttons.append(nav_buttons)
    
    buttons.append([Button.inline("• رجوع • ↩️", data="numbers_menu")])
    buttons.append(cancel_operation_keyboard()[0])
    
    await event.edit(msgs['COUNTRY_LIST'], buttons=buttons)

async def next_countries_page(event):
    user_id = event.chat_id
    if user_id not in user_pages:
        user_pages[user_id] = 0
    
    # احصل على قائمة الدول
    countries = await get_countries()
    
    # احسب عدد الصفحات المتاحة
    total_pages = (len(countries) // 20)
    
    # إذا كانت الصفحة الحالية هي الأخيرة
    if user_pages[user_id] >= total_pages:
        await event.answer("❗️ هذه آخر صفحة", alert=True)
        return
    
    user_pages[user_id] += 1
    await buy_number(event)

async def prev_countries_page(event):
    user_id = event.chat_id
    if user_id not in user_pages:
        user_pages[user_id] = 0
    
    if user_pages[user_id] <= 0:
        await event.answer("❗️ هذه أول صفحة", alert=True)
        return
    
    user_pages[user_id] -= 1
    await buy_number(event)
    
async def withdraw_balance(event):
    user_id = event.chat_id
    async with event.client.conversation(user_id) as conv:
        active_conversations[user_id] = conv
        try:
            user_data = await get_user(user_id)
            coins = user_data['coins'] if user_data else 0
            if coins < 1:
                await event.answer("الحد الأدنى للسحب هو 1$", alert=True)
                return

            await conv.send_message("أرسل رقم الكاش أو المحفظة:", buttons=cancel_operation_keyboard())
            cash_info = await conv.get_response(timeout=300)
            
            await conv.send_message("أدخل المبلغ المراد سحبه:", buttons=cancel_operation_keyboard())
            amount_info = await conv.get_response(timeout=300)
            
            try:
                amount = float(amount_info.text)
                async with get_user_lock(user_id):
                    user_data = await get_user(user_id)
                    coins = user_data['coins'] if user_data else 0
                    if amount > coins:
                        await conv.send_message("رصيدك غير كافي!")
                        return
                    await update_user_coins(user_id, coins - amount)
                
                await event.client.send_message(
                    ADMIN_ID, 
                    f"• طلب سحب رصيد:\n- العضو: {user_id}\n- المبلغ: {amount}$\n- المحفظة: {cash_info.text}",
                    buttons=[[Button.inline("• تأكيد التحويل • ✅", data=f"confirm_withdraw_{user_id}")]]
                )
                await conv.send_message(msgs['WITHDRAW_SUCCESS'].format(amount))
            except ValueError:
                await conv.send_message("المبلغ غير صحيح!")
        except asyncio.TimeoutError:
            await conv.send_message("⏱ انتهى الوقت المحدد للإدخال!")
        finally:
            if user_id in active_conversations:
                del active_conversations[user_id]

async def transfer_balance(event):
    async with event.client.conversation(event.chat_id) as conv:
        active_conversations[event.chat_id] = conv
        try:
            await conv.send_message(msgs['TRANSFER_MESSAGE'], buttons=cancel_operation_keyboard())
            user_id_resp = await conv.get_response(timeout=300)
            try: target_id = int(user_id_resp.text)
            except: 
                await conv.send_message("ايدي غير صحيح!")
                return
            
            if event.chat_id == target_id: 
                await conv.send_message("لا يمكن التحويل لنفسك!")
                return
            if not await get_user(target_id): 
                await conv.send_message("العضو غير موجود!")
                return
            
            min_transfer = float(await get_setting("transfer_minimum") or 5)
            await conv.send_message(f"أدخل المبلغ (الحد الأدنى: {min_transfer}$):", buttons=cancel_operation_keyboard())
            amount_resp = await conv.get_response(timeout=300)
            try: amount = float(amount_resp.text)
            except: 
                await conv.send_message("مبلغ غير صحيح!")
                return
            
            if amount < min_transfer: 
                await conv.send_message(f"الحد الأدنى: {min_transfer}$")
                return
            # تطبيق العمولة
            fee = amount * 0.02
            total = amount + fee
            
            first_id, second_id = sorted([event.chat_id, target_id])
            async with get_user_lock(first_id):
                async with get_user_lock(second_id):
                    sender_data = await get_user(event.chat_id)
                    sender_coins = sender_data['coins'] if sender_data else 0
                    if sender_coins < total:
                        await conv.send_message("رصيدك غير كافي!")
                        return
                    receiver_data = await get_user(target_id)
                    receiver_coins = receiver_data['coins'] if receiver_data else 0
                    await update_user_coins(event.chat_id, sender_coins - total)
                    await update_user_coins(target_id, receiver_coins + amount)
            
            await conv.send_message(f"✅ تم تحويل {amount}$ للعضو {target_id}")
            await event.client.send_message(target_id, f"استلمت تحويل بقيمة {amount}$ من {event.chat_id}")
            await event.client.send_message(ADMIN_ID, f"• تحويل رصيد:\n- من: {event.chat_id}\n- إلى: {target_id}\n- المبلغ: {amount}$\n- العمولة: {fee}$")
        except asyncio.TimeoutError:
            await conv.send_message("⏱ انتهى الوقت المحدد للإدخال!")
        finally:
            if event.chat_id in active_conversations:
                del active_conversations[event.chat_id]

async def support_request(event):
    async with event.client.conversation(event.chat_id) as conv:
        active_conversations[event.chat_id] = conv
        try:
            await conv.send_message("أرسل رسالتك للدعم:", buttons=cancel_operation_keyboard())
            message = await conv.get_response(timeout=300)
            
            user_info = await event.client.get_entity(event.chat_id)
            username = f"@{user_info.username}" if user_info.username else "لا يوجد"
            
            await event.client.send_message(
                ADMIN_ID,
                f"📩 رسالة دعم جديدة:\n\n"
                f"- العضو: <a href='tg://user?id={event.chat_id}'>{user_info.first_name}</a>\n"
                f"- المعرف: {username}\n"
                f"- الايدي: {event.chat_id}\n\n"
                f"الرسالة: {message.text}",
                parse_mode="html",
                buttons=[[Button.inline("• الرد على العضو • ↩️", data=f"reply_{event.chat_id}")]]
            )
            await conv.send_message("تم إرسال رسالتك للدعم، سيتم الرد قريباً ✅")
        except asyncio.TimeoutError:
            await conv.send_message("⏱ انتهى الوقت المحدد للإدخال!")
        finally:
            if event.chat_id in active_conversations:
                del active_conversations[event.chat_id]

async def show_rules(event):
    rules = await get_setting("rules_message") or "مرحباً، القوانين قيد التحديث..."
    await event.edit(rules, buttons=[[Button.inline("• رجوع • ↩️", data="control_menu")]])

async def sell_account(event):
    async with event.client.conversation(event.chat_id) as conv:
        active_conversations[event.chat_id] = conv
        try:
            await conv.send_message("أرسل رقم الهاتف (مثال: +9647801234567):", buttons=cancel_operation_keyboard())
            phone_resp = await conv.get_response(timeout=300)
            phone = phone_resp.text.replace("+", "").replace(" ", "")
            
            # التحقق من الدولة
            country = None
            countries = await get_countries()
            for c in countries:
                if phone.startswith(c[0]):
                    country = c
                    break
            
            if not country:
                await conv.send_message("❌ الدولة غير مدعومة!")
                return
            
            # عملية تسجيل الدخول
            client = TelegramClient(StringSession(), API_ID, API_HASH)
            await client.connect()
            try:
                await client.send_code_request(f"+{phone}")
                await conv.send_message("أرسل الكود (5 أرقام):", buttons=cancel_operation_keyboard())
                code_resp = await conv.get_response(timeout=300)
                code = code_resp.text.replace(" ", "")
                
                try:
                    # محاولة تسجيل الدخول بدون كلمة مرور
                    await client.sign_in(f"+{phone}", code)
                    twofa = 'لا يوجد'
                except SessionPasswordNeededError:
                    # إذا طلب كلمة المرور
                    await conv.send_message("🔐 الحساب محمي بتحقق بخطوتين. أرسل كلمة المرور:", buttons=cancel_operation_keyboard())
                    password_resp = await conv.get_response(timeout=300)
                    await client.sign_in(password=password_resp.text)
                    twofa = password_resp.text
                
                # حفظ الجلسة
                session_str = client.session.save()
                
                # إضافة الحساب كبيع معلق
                await add_pending_sale(phone, event.chat_id, country[3], country[0], session_str, twofa)
                
                # إرسال رسالة نجاح
                await conv.send_message(msgs['PENDING_SALE'].format(phone, country[3]))
                
                # طلب تسجيل الخروج من الجلسات الأخرى
                buttons = [[Button.inline("• تم الخروج • ✅", data=f"sell_logout_{phone}")]]
                await conv.send_message(msgs['SELL_LOGOUT_INSTRUCTIONS'], buttons=buttons)
                
            except Exception as e:
                await conv.send_message(f"❌ خطأ: {str(e)}")
        except asyncio.TimeoutError:
            await conv.send_message("⏱ انتهى الوقت المحدد للإدخال!")
        finally:
            if event.chat_id in active_conversations:
                del active_conversations[event.chat_id]

async def admin_panel(event):
    await event.edit(msgs['ADMIN_MESSAGE'], buttons=admin_panel_keyboard())

async def super_settings_menu(event):
    await event.edit(msgs['SUPER_MENU'], buttons=super_settings_keyboard())

async def add_super_channel_handler(event):
    user_id = event.chat_id
    if user_id != ADMIN_ID and not await is_admin(user_id):
        await event.answer("❌ ليس لديك صلاحية!", alert=True)
        return

    async with event.client.conversation(user_id) as conv:
        active_conversations[user_id] = conv
        try:
            await conv.send_message(
                "أرسل رابط أو معرف المجموعة/القناة السوبر:\n"
                "• @username\n"
                "• https://t.me/username\n"
                "• https://t.me/+invite\n"
                "• ID رقمي",
                buttons=cancel_operation_keyboard()
            )
            channel_resp = await conv.get_response(timeout=300)
            channel_id = normalize_super_target(channel_resp.text)

            await conv.send_message("أرسل عنوان القناة:", buttons=cancel_operation_keyboard())
            title_resp = await conv.get_response(timeout=300)
            title = title_resp.text.strip()

            stored_accounts = await get_stored_accounts(user_id)
            if not stored_accounts:
                await conv.send_message("❌ لا توجد حسابات مخزنة للتحقق من السوبر والانضمام إليه!")
                return

            verifier_account = stored_accounts[0]
            try:
                await verify_super_target_with_stored_account(verifier_account[1], channel_id)
            except Exception as exc:
                await conv.send_message(
                    "❌ لم يستطع الحساب المخزن الانضمام أو التعرف على هذا السوبر.\n"
                    f"السبب: {exc}"
                )
                return

            await add_super_channel(channel_id, title)
            await conv.send_message(
                "✅ تم إضافة السوبر بنجاح!\n\n"
                f"تم التحقق والانضمام باستخدام الحساب المخزن: +{verifier_account[0]}"
            )
        except asyncio.TimeoutError:
            await conv.send_message("⏱ انتهى الوقت المحدد للإدخال!")
        finally:
            if user_id in active_conversations:
                del active_conversations[user_id]

async def show_super_channels(event):
    channels = await get_super_channels()
    if not channels:
        await event.answer("❌ لا توجد قنوات سوبر مضافّة!", alert=True)
        return
    
    text = "📢 القنوات السوبر المضافّة:\n\n"
    for i, channel in enumerate(channels, 1):
        text += f"{i}. {channel[1]} (ID: {channel[0]})\n"
    
    await event.answer(text, alert=True)

async def clear_super_channels(event):
    await execute_query("DELETE FROM super_channels", commit=True)
    await event.answer("✅ تم حذف جميع القنوات السوبر!", alert=True)

async def del_super_channel_handler(event):
    channels = await get_super_channels()
    if not channels:
        await event.answer("❌ لا توجد قنوات سوبر!", alert=True)
        return
    
    item_buttons = []
    for index, channel in enumerate(channels):
        item_buttons.append(Button.inline(channel[1], data=f"del_super_channel:{index}"))
    buttons = arrange_buttons(item_buttons)
    
    buttons.append([Button.inline("• رجوع • ↩️", data="control_settings_super")])
    await event.edit("اختر القناة لحذفها:", buttons=buttons)

async def handle_delete_super_channel(event, data):
    index_text = data.split(':', 1)[1]
    channels = await get_super_channels()
    try:
        channel = channels[int(index_text)]
    except (TypeError, ValueError, IndexError):
        await event.answer("❌ بيانات السوبر غير صالحة!", alert=True)
        return
    channel_id = channel[0]
    await delete_super_channel(channel_id)
    await event.answer("✅ تم حذف السوبر بنجاح!", alert=True)
    await super_settings_menu(event)
    
async def posting_settings_menu(event):
    await event.edit(msgs['POSTING_MENU'], buttons=posting_settings_keyboard())

async def add_posting_template_handler(event):
    async with event.client.conversation(event.chat_id) as conv:
        active_conversations[event.chat_id] = conv
        try:
            await conv.send_message("أرسل كليشة النشر الجديدة:", buttons=cancel_operation_keyboard())
            template = await conv.get_response(timeout=300)
            await add_posting_template(template.text)
            await conv.send_message("✅ تم إضافة الكليشة بنجاح!")
        except asyncio.TimeoutError:
            await conv.send_message("⏱ انتهى الوقت المحدد للإدخال!")
        finally:
            if event.chat_id in active_conversations:
                del active_conversations[event.chat_id]

async def del_posting_template_handler(event):
    templates = await get_posting_templates()
    if not templates:
        await event.answer("لا توجد كلايش مضافّة!", alert=True)
        return

    item_buttons = []
    for template in templates:
        preview = template[1][:30] + "..." if len(template[1]) > 30 else template[1]
        item_buttons.append(Button.inline(preview, data=f"del_template:{template[0]}"))
    buttons = arrange_buttons(item_buttons)
    buttons.append([Button.inline("• رجوع • ↩️", data="posting_settings_menu")])
    await event.edit("اختر الكليشة للحذف:", buttons=buttons)

async def show_posting_templates(event):
    templates = await get_posting_templates()
    if not templates:
        return await event.answer("لا توجد كلايش مضافّة!", alert=True)
    
    text = "📝 الكلايش المضافّة:\n\n"
    for i, template in enumerate(templates, 1):
        text += f"{i}. {template[1]}\n\n"
    
    await event.answer(text, alert=True)

async def edit_posting_template_handler(event):
    templates = await get_posting_templates()
    if not templates:
        await event.answer("لا توجد كلايش مضافّة!", alert=True)
        return

    item_buttons = []
    for template in templates:
        preview = template[1][:30] + "..." if len(template[1]) > 30 else template[1]
        item_buttons.append(Button.inline(preview, data=f"edit_template:{template[0]}"))
    buttons = arrange_buttons(item_buttons)
    buttons.append([Button.inline("• رجوع • ↩️", data="posting_settings_menu")])
    await event.edit("اختر الكليشة للتعديل:", buttons=buttons)

async def clear_posting_templates_handler(event):
    templates = await get_posting_templates()
    if not templates:
        await event.answer("لا توجد كلايش مضافّة!", alert=True)
        return

    await execute_query("DELETE FROM posting_templates", commit=True)
    await event.answer("✅ تم حذف جميع الكلايش بنجاح!", alert=True)
    await posting_settings_menu(event)

async def delete_posting_template_callback(event, data):
    try:
        template_id = int(data.split(":", 1)[1])
    except (ValueError, IndexError):
        await event.answer("❌ بيانات الكليشة غير صالحة!", alert=True)
        return

    await delete_posting_template(template_id)
    await event.answer("✅ تم حذف الكليشة بنجاح!", alert=True)
    await del_posting_template_handler(event)

async def edit_posting_template_callback(event, data):
    try:
        template_id = int(data.split(":", 1)[1])
    except (ValueError, IndexError):
        await event.answer("❌ بيانات الكليشة غير صالحة!", alert=True)
        return

    async with event.client.conversation(event.chat_id) as conv:
        active_conversations[event.chat_id] = conv
        try:
            await conv.send_message("أرسل النص الجديد للكليشة:", buttons=cancel_operation_keyboard())
            new_template = await conv.get_response(timeout=300)
            if not new_template.text or not new_template.text.strip():
                await conv.send_message("❌ لا يمكن حفظ كليشة فارغة.")
                return

            await update_posting_template(template_id, new_template.text.strip())
            await conv.send_message("✅ تم تعديل الكليشة بنجاح!")
        except asyncio.TimeoutError:
            await conv.send_message("⏱ انتهى الوقت المحدد للإدخال!")
        finally:
            if event.chat_id in active_conversations:
                del active_conversations[event.chat_id]

async def set_posting_setting(event, key, value):
    await set_setting(key, value)
    await event.answer(f"✅ تم تحديث الإعداد بنجاح!", alert=True)

async def start_posting_handler(event):
    user_id = event.chat_id
    
    # التحقق من وجود حسابات مخزنة للمستخدم
    stored_accounts = await get_stored_accounts(user_id)
    if not stored_accounts:
        await event.answer("❌ لا توجد حسابات مخزنة لاستخدامها في النشر!", alert=True)
        return
    
    # عرض قائمة الحسابات المخزنة للاختيار
    item_buttons = []
    for account in stored_accounts:
        phone = account[0]
        item_buttons.append(Button.inline(f"+{phone}", data=f"select_acc_for_posting:{phone}"))
    buttons = arrange_buttons(item_buttons)
    
    buttons.append(cancel_operation_keyboard()[0])
    
    await event.edit("📱 اختر الحساب الذي تريد استخدامه للنشر:", buttons=buttons)

async def select_account_for_posting(event, data):
    phone = data.split(':')[1]
    user_id = event.chat_id
    
    # تخزين الحساب المختار في الذاكرة
    user_posting_accounts[user_id] = phone
    
    # المتابعة لإعدادات النشر
    await ask_posting_settings(event)

async def ask_posting_settings(event):
    user_id = event.chat_id
    phone = user_posting_accounts.get(user_id)
    
    if not phone:
        await event.answer("❌ لم يتم تحديد حساب للنشر!", alert=True)
        return
    
    # التحقق من وجود قوالب نشر
    templates = await get_posting_templates()
    if not templates:
        await event.answer("❌ لا توجد قوالب نشر مضافّة!", alert=True)
        return
    
    # التحقق من وجود قنوات سوبر
    super_channels = await get_super_channels()
    if not super_channels:
        await event.answer("❌ لا توجد قنوات سوبر مضافّة!", alert=True)
        return
    
    buttons = arrange_buttons([
        Button.inline("النشر في جميع القنوات", data="posting_all"),
        Button.inline("النشر في قناة محددة", data="posting_specific"),
        Button.inline("• إلغاء العملية • ❌", data="cancel_operation")
    ])
    
    await event.edit(
        f"🔧 إعدادات النشر للحساب: +{phone}\n\n"
        "اختر طريقة النشر:",
        buttons=buttons
    )

async def posting_all_channels(event):
    user_id = event.chat_id
    phone = user_posting_accounts.get(user_id)
    
    async with event.client.conversation(user_id) as conv:
        active_conversations[user_id] = conv
        try:
            await conv.send_message(
                "أدخل الفترة بين النشرات بالثواني (180 ثانية كحد أدنى):",
                buttons=cancel_operation_keyboard()
            )
            interval_resp = await conv.get_response(timeout=300)
            
            await conv.send_message(
                "أدخل عدد مرات التكرار:",
                buttons=cancel_operation_keyboard()
            )
            repetitions_resp = await conv.get_response(timeout=300)
            
            try:
                interval = int(interval_resp.text)
                repetitions = int(repetitions_resp.text)
                
                if interval < 180:
                    await conv.send_message("يجب أن تكون الفترة 180 ثانية على الأقل!")
                    return
                
                # بدء مهمة النشر
                asyncio.create_task(
                    run_posting_task(
                        user_id=user_id,
                        phone=phone,
                        interval=interval,
                        repetitions=repetitions,
                        all_channels=True
                    )
                )
                
                await conv.send_message("✅ بدأ النشر التلقائي في جميع القنوات بنجاح!")
            except ValueError:
                await conv.send_message("الرجاء إدخال أرقام صحيحة!")
        except asyncio.TimeoutError:
            await conv.send_message("⏱ انتهى الوقت المحدد للإدخال!")
        finally:
            if user_id in active_conversations:
                del active_conversations[user_id]

async def select_channel_for_posting(event):
    user_id = event.chat_id
    super_channels = await get_super_channels()
    
    item_buttons = []
    for index, channel in enumerate(super_channels):
        item_buttons.append(Button.inline(channel[1], data=f"select_channel:{index}"))
    buttons = arrange_buttons(item_buttons)
    
    buttons.append(cancel_operation_keyboard()[0])
    
    await event.edit("📢 اختر القناة للنشر:", buttons=buttons)

async def posting_specific_channel(event, data):
    index_text = data.split(':', 1)[1]
    user_id = event.chat_id
    phone = user_posting_accounts.get(user_id)
    super_channels = await get_super_channels()
    try:
        channel_id = super_channels[int(index_text)][0]
    except (TypeError, ValueError, IndexError):
        await event.answer("❌ بيانات السوبر غير صالحة!", alert=True)
        return
    
    async with event.client.conversation(user_id) as conv:
        active_conversations[user_id] = conv
        try:
            await conv.send_message(
                "أدخل الفترة بين النشرات بالثواني (180 ثانية كحد أدنى):",
                buttons=cancel_operation_keyboard()
            )
            interval_resp = await conv.get_response(timeout=300)
            
            await conv.send_message(
                "أدخل عدد مرات التكرار:",
                buttons=cancel_operation_keyboard()
            )
            repetitions_resp = await conv.get_response(timeout=300)
            
            try:
                interval = int(interval_resp.text)
                repetitions = int(repetitions_resp.text)
                
                if interval < 180:
                    await conv.send_message("يجب أن تكون الفترة 180 ثانية على الأقل!")
                    return
                
                # بدء مهمة النشر
                asyncio.create_task(
                    run_posting_task(
                        user_id=user_id,
                        phone=phone,
                        interval=interval,
                        repetitions=repetitions,
                        all_channels=False,
                        channel_id=channel_id
                    )
                )
                
                await conv.send_message("✅ بدأ النشر التلقائي في القناة المحددة بنجاح!")
            except ValueError:
                await conv.send_message("الرجاء إدخال أرقام صحيحة!")
        except asyncio.TimeoutError:
            await conv.send_message("⏱ انتهى الوقت المحدد للإدخال!")
        finally:
            if user_id in active_conversations:
                del active_conversations[user_id]

async def stop_posting_group_handler(event):
    supers = await get_super_channels()
    if not supers:
        await event.answer("❌ لا توجد قنوات سوبر لإيقاف النشر فيها!", alert=True)
        return
    item_buttons = []
    for index, super_ch in enumerate(supers):
        item_buttons.append(Button.inline(super_ch[1], data=f"stop_posting:{index}"))
    buttons = arrange_buttons(item_buttons)
    buttons.append([Button.inline("• رجوع • ↩️", data="posting_settings_menu")])
    await event.edit("اختر السوبر لإيقاف النشر فيه:", buttons=buttons)

async def stop_posting_channel_handler(event, data):
    parts = data.split(":", 1)
    if len(parts) != 2 or not parts[1]:
        await event.answer("❌ بيانات القناة غير صالحة!", alert=True)
        return

    index_text = parts[1]
    supers = await get_super_channels()
    try:
        channel_id = supers[int(index_text)][0]
    except (TypeError, ValueError, IndexError):
        await event.answer("❌ بيانات السوبر غير صالحة!", alert=True)
        return
    await execute_query(
        "UPDATE active_posting_tasks SET active=0 WHERE user_id=? AND channel_id=?",
        (event.chat_id, channel_id),
        commit=True
    )
    await event.answer("✅ تم إيقاف النشر في هذه القناة.", alert=True)

async def stop_all_posting_handler(event):
    # إيقاف جميع مهام النشر للمستخدم
    await execute_query(
        "UPDATE active_posting_tasks SET active=0 WHERE user_id=?",
        (event.chat_id,),
        commit=True
    )
    await event.answer(msgs['POSTING_STOPPED'], alert=True)

async def creation_settings_menu(event):
    await event.edit(msgs['CREATION_MENU'], buttons=creation_settings_keyboard())

async def manual_group_creation_handler(event):
    """
    معالج الإنشاء اليدوي للمجموعات
    """
    user_id = event.chat_id
    async with event.client.conversation(user_id) as conv:
        try:
            await conv.send_message("أرسل عدد المجموعات المراد إنشاؤها:", buttons=cancel_operation_keyboard())
            count_resp = await conv.get_response(timeout=300)
            
            try:
                count = int(count_resp.text)
                if count < 1 or count > 50:  # حد أقصى 50 مجموعة لكل عملية
                    await conv.send_message("الرجاء إدخال عدد بين 1 و 50!")
                    return
                
                # تخزين المهمة
                manual_creation_tasks[user_id] = {
                    'count': count,
                    'created': 0,
                    'active': True
                }
                
                # بدء عملية الإنشاء
                await conv.send_message(f"⏳ جاري إنشاء {count} مجموعات...")
                
                success_count = 0
                stored_accounts = await get_stored_accounts(user_id)
                
                if not stored_accounts:
                    await conv.send_message("❌ لا توجد حسابات مخزنة!")
                    return
                
                for i in range(count):
                    if not manual_creation_tasks.get(user_id, {}).get('active', True):
                        break
                    
                    # اختيار حساب عشوائي
                    account = random.choice(stored_accounts)
                    group_id, invite_link, _ = await create_private_group(account[1])
                    
                    if group_id:
                        success_count += 1
                        manual_creation_tasks[user_id]['created'] = success_count
                        
                        # إرسال نتيجة الإنشاء
                        await event.client.send_message(
                            user_id,
                            f"✅ المجموعة #{success_count}\n"
                            f"🆔: {group_id}\n"
                            f"🔗: {invite_link}"
                        )
                    
                    await asyncio.sleep(10)  # تأخير بين كل إنشاء
                
                # إرسال ملخص النتائج
                await conv.send_message(
                    f"🎉 تم إنشاء {success_count} من أصل {count} مجموعات بنجاح!"
                )
                
            except ValueError:
                await conv.send_message("الرجاء إدخال عدد صحيح!")
                
        except asyncio.TimeoutError:
            await conv.send_message("⏱ انتهى الوقت المحدد للإدخال!")
        finally:
            if user_id in manual_creation_tasks:
                del manual_creation_tasks[user_id]
                
async def stop_manual_creation_handler(event):
    if event.chat_id in manual_creation_tasks:
        manual_creation_tasks[event.chat_id]['active'] = False
        await event.answer("✅ تم إيقاف الإنشاء اليدوي بنجاح!", alert=True)
    else:
        await event.answer("لا توجد مهمة إنشاء يدوي نشطة!", alert=True)

async def auto_group_creation_handler(event):
    """
    معالج الإنشاء التلقائي للمجموعات
    """
    user_id = event.chat_id
    async with event.client.conversation(user_id) as conv:
        try:
            await conv.send_message("أرسل الفترة بين كل إنشاء (بالثواني - بين 20 و 3600):", buttons=cancel_operation_keyboard())
            seconds_resp = await conv.get_response(timeout=300)
            
            await conv.send_message("أرسل المدة الزمنية للتكرار (بـ الساعات - بين 1 و 24):", buttons=cancel_operation_keyboard())
            hours_resp = await conv.get_response(timeout=300)
            
            try:
                seconds = int(seconds_resp.text)
                hours = int(hours_resp.text)
                
                if not 20 <= seconds <= 3600:
                    await conv.send_message("يجب أن تكون الفترة بين 20 و 3600 ثانية!")
                    return
                
                if not 1 <= hours <= 24:
                    await conv.send_message("يجب أن تكون المدة بين 1 و 24 ساعة!")
                    return
                
                # إنشاء مهمة الإنشاء التلقائي
                await create_auto_creation_task(user_id, seconds, hours)
                auto_creation_tasks[user_id] = {
                    'active': True,
                    'total_created': 0
                }
                
                # بدء عملية الإنشاء في الخلفية
                asyncio.create_task(run_auto_creation(user_id, seconds, hours))
                
                await conv.send_message(
                    f"✅ تم بدء الإنشاء التلقائي بنجاح!\n"
                    f"⏱ الفترة بين الإنشاءات: {seconds} ثانية\n"
                    f"🕒 المدة الإجمالية: {hours} ساعة"
                )
                
            except ValueError:
                await conv.send_message("الرجاء إدخال أرقام صحيحة!")
                
        except asyncio.TimeoutError:
            await conv.send_message("⏱ انتهى الوقت المحدد للإدخال!")
            
async def stop_auto_creation_handler(event):
    user_id = event.chat_id
    # إيقاف جميع مهام الإنشاء التلقائي لهذا المستخدم
    if user_id in auto_creation_tasks:
        auto_creation_tasks[user_id] = False

    # تحديث قاعدة البيانات لإيقاف المهام النشطة
    tasks = await get_active_auto_creation_tasks()
    for task in tasks:
        if task[1] == user_id:  # task[1] is user_id
            await update_auto_creation_task(task[0], active=False)

    await event.answer(msgs['AUTO_CREATION_STOPPED'], alert=True)

async def account_settings_menu(event):
    await event.edit(msgs['ACCOUNT_MENU'], buttons=account_settings_keyboard())

async def get_owned_stored_account(user_id, phone):
    accounts = await get_stored_accounts(user_id)
    return next((account for account in accounts if str(account[0]) == str(phone)), None)

async def unsupported_account_feature(event):
    await event.answer(
        "❌ لا يمكن تنفيذ هذا الخيار بشكل فعلي لأنه يتجاوز خصوصية المستخدمين أو قيود تيليجرام.",
        alert=True
    )

async def save_post_account_menu(event):
    user_id = event.chat_id
    accounts = await get_stored_accounts(user_id)
    if not accounts:
        await event.answer("❌ لا توجد حسابات مخزنة!", alert=True)
        return

    item_buttons = [
        Button.inline(f"+{account[0]}", data=f"save_post_account:{account[0]}")
        for account in accounts
    ]
    buttons = arrange_buttons(item_buttons)
    buttons.append([Button.inline("• رجوع • ↩️", data="control_settings_account")])
    await event.edit("📱 اختر الحساب الذي تريد استخدامه لجلب المنشور:", buttons=buttons)

async def save_post_account_selected(event, data):
    user_id = event.chat_id
    phone = data.split(":", 1)[1]
    account = await get_owned_stored_account(user_id, phone)
    if not account:
        await event.answer("❌ لم يتم العثور على الحساب!", alert=True)
        return

    async with event.client.conversation(user_id) as conv:
        active_conversations[user_id] = conv
        try:
            await conv.send_message(
                f"أرسل رابط المنشور للحساب +{phone}:",
                buttons=cancel_operation_keyboard()
            )
            post_resp = await conv.get_response(timeout=300)
            await save_allowed_post(conv, account, post_resp.text.strip())
        except asyncio.TimeoutError:
            await conv.send_message("⏱ انتهى الوقت المحدد للإدخال!")
        finally:
            if user_id in active_conversations:
                del active_conversations[user_id]

async def save_allowed_post(conv, account, post_link):
    phone, session = account[0], account[1]
    entity, msg_id = await extract_entity_from_url(post_link)
    if not entity or not msg_id:
        await conv.send_message("❌ رابط المنشور غير صالح.")
        return

    client = None
    file_path = None
    try:
        client = TelegramClient(StringSession(session), API_ID, API_HASH)
        await client.connect()
        if isinstance(entity, str):
            entity = await resolve_posting_target(client, entity)

        message = await client.get_messages(entity, ids=msg_id)
        if not message:
            await conv.send_message("❌ لم أستطع العثور على المنشور عبر هذا الحساب.")
            return

        chat = await message.get_chat()
        if getattr(message, "noforwards", False) or getattr(chat, "noforwards", False):
            await conv.send_message(
                "❌ هذا المنشور محمي من الحفظ أو التوجيه بواسطة تيليجرام، لذلك لا يمكن نسخه."
            )
            return

        caption = message.text or ""
        if message.media:
            save_dir = "/tmp/number_saved_posts"
            os.makedirs(save_dir, exist_ok=True)
            file_path = await client.download_media(message, file=save_dir)
            if not file_path:
                await conv.send_message("❌ فشل تنزيل ميديا المنشور.")
                return
            await conv.send_file(
                file_path,
                caption=caption or f"✅ تم جلب المنشور بنجاح بواسطة الحساب +{phone}"
            )
        elif message.text:
            await conv.send_message(message.text)
        else:
            await conv.send_message("❌ المنشور لا يحتوي على نص أو ميديا قابلة للجلب.")
            return

        await conv.send_message(f"✅ تم جلب المنشور بنجاح بواسطة الحساب +{phone}.")
    except Exception as exc:
        await conv.send_message(f"❌ فشل جلب المنشور: {exc}")
    finally:
        if file_path:
            with contextlib.suppress(Exception):
                os.remove(file_path)
        if client and client.is_connected():
            await client.disconnect()

def storage_action_text(action):
    labels = {
        "set_group": "إضافة كروب تخزين",
        "enable": "تفعيل التخزين",
        "disable": "تعطيل التخزين",
    }
    return labels.get(action, "إعداد التخزين")

async def storage_account_menu(event, action):
    user_id = event.chat_id
    accounts = await get_stored_accounts(user_id)
    if not accounts:
        await event.answer("❌ لا توجد حسابات مخزنة!", alert=True)
        return

    item_buttons = [
        Button.inline(f"+{account[0]}", data=f"storage_account:{action}:{account[0]}")
        for account in accounts
    ]
    buttons = arrange_buttons(item_buttons)
    buttons.append([Button.inline("• رجوع • ↩️", data="control_settings_account")])
    await event.edit(
        f"📱 اختر الحساب لتنفيذ: {storage_action_text(action)}",
        buttons=buttons
    )

async def storage_account_selected(event, data):
    parts = data.split(":", 2)
    if len(parts) != 3:
        await event.answer("❌ بيانات العملية غير صالحة!", alert=True)
        return

    action, phone = parts[1], parts[2]
    user_id = event.chat_id
    account = await get_owned_stored_account(user_id, phone)
    if not account:
        await event.answer("❌ لم يتم العثور على الحساب!", alert=True)
        return

    if action == "set_group":
        await set_storage_group_handler(event, account)
    elif action == "enable":
        await enable_incoming_storage_for_account(event, account)
    elif action == "disable":
        await disable_incoming_storage_for_account(event, account)
    else:
        await event.answer("❌ نوع العملية غير معروف!", alert=True)

async def set_storage_group_handler(event, account):
    user_id = event.chat_id
    phone = account[0]
    async with event.client.conversation(user_id) as conv:
        active_conversations[user_id] = conv
        try:
            await conv.send_message(
                f"أرسل رابط أو معرف كروب التخزين للحساب +{phone}:",
                buttons=cancel_operation_keyboard()
            )
            group_resp = await conv.get_response(timeout=300)
            storage_group = group_resp.text.strip()

            verifier = None
            try:
                verifier = TelegramClient(StringSession(account[1]), API_ID, API_HASH)
                await verifier.connect()
                await resolve_posting_target(verifier, storage_group)
            except Exception as exc:
                await conv.send_message(
                    "❌ لم يستطع الحساب الوصول إلى كروب التخزين.\n"
                    f"السبب: {exc}"
                )
                return
            finally:
                if verifier and verifier.is_connected():
                    await verifier.disconnect()

            await set_setting(f"storage_group:{user_id}:{phone}", storage_group)
            await conv.send_message(f"✅ تم تعيين كروب التخزين للحساب +{phone} بنجاح.")
        except asyncio.TimeoutError:
            await conv.send_message("⏱ انتهى الوقت المحدد للإدخال!")
        finally:
            if user_id in active_conversations:
                del active_conversations[user_id]

async def stop_incoming_storage_client(user_id, phone):
    key = (user_id, str(phone))
    client = incoming_storage_clients.pop(key, None)
    if client and client.is_connected():
        await client.disconnect()

async def enable_incoming_storage_for_account(event, account):
    user_id = event.chat_id
    phone, session = account[0], account[1]
    storage_group = await get_setting(f"storage_group:{user_id}:{phone}") or await get_setting(f"storage_group:{user_id}")
    if not storage_group:
        await event.answer("❌ عيّن كروب التخزين لهذا الحساب أولاً.", alert=True)
        return

    await stop_incoming_storage_client(user_id, phone)
    client = TelegramClient(StringSession(session), API_ID, API_HASH)
    try:
        await client.connect()
        target = await resolve_posting_target(client, storage_group)

        async def forward_private_message(storage_event):
            try:
                sender = await storage_event.get_sender()
                sender_name = " ".join(
                    part for part in [
                        getattr(sender, "first_name", None),
                        getattr(sender, "last_name", None)
                    ] if part
                ).strip() or getattr(sender, "username", None) or str(storage_event.sender_id)
                await client.send_message(
                    target,
                    f"📩 رسالة واردة للحساب +{phone}\n"
                    f"👤 من: {sender_name}\n"
                    f"🆔: {storage_event.sender_id}"
                )
                await client.forward_messages(target, storage_event.message)
            except Exception as exc:
                logger.error(f"Error storing incoming message: {str(exc)}")

        client.add_event_handler(
            forward_private_message,
            events.NewMessage(incoming=True, func=lambda e: e.is_private)
        )
        incoming_storage_clients[(user_id, str(phone))] = client
        await set_setting(f"incoming_storage:{user_id}:{phone}", "1")
        await event.edit(
            f"✅ تم تفعيل التخزين فعلياً للحساب +{phone}.\n"
            "سيتم تحويل الرسائل الخاصة الواردة إلى كروب التخزين.",
            buttons=[[Button.inline("• رجوع • ↩️", data="control_settings_account")]]
        )
    except Exception as exc:
        if client and client.is_connected():
            await client.disconnect()
        await event.edit(
            f"❌ فشل تفعيل التخزين للحساب +{phone}.\n"
            f"السبب: {exc}",
            buttons=[[Button.inline("• رجوع • ↩️", data="control_settings_account")]]
        )

async def disable_incoming_storage_for_account(event, account):
    user_id = event.chat_id
    phone = account[0]
    await stop_incoming_storage_client(user_id, phone)
    await set_setting(f"incoming_storage:{user_id}:{phone}", "0")
    await event.edit(
        f"✅ تم تعطيل التخزين للحساب +{phone}.",
        buttons=[[Button.inline("• رجوع • ↩️", data="control_settings_account")]]
    )

async def leave_groups_account_menu(event):
    user_id = event.chat_id
    accounts = await get_stored_accounts(user_id)
    if not accounts:
        await event.answer("❌ لا توجد حسابات مخزنة!", alert=True)
        return

    item_buttons = [
        Button.inline(f"+{account[0]}", data=f"leave_groups_account:{account[0]}")
        for account in accounts
    ]
    buttons = arrange_buttons(item_buttons)
    buttons.append([Button.inline("• رجوع • ↩️", data="control_settings_account")])
    await event.edit("📱 اختر الحساب الذي تريد مغادرة الكروبات منه:", buttons=buttons)

async def leave_groups_account_selected(event, data):
    user_id = event.chat_id
    phone = data.split(":", 1)[1]
    account = await get_owned_stored_account(user_id, phone)
    if not account:
        await event.answer("❌ لم يتم العثور على الحساب!", alert=True)
        return

    user_leave_group_accounts[user_id] = phone
    await event.edit(
        f"⚠️ تأكيد مغادرة جميع الكروبات للحساب +{phone}؟",
        buttons=[
            [Button.inline("• تأكيد مغادرة الكروبات • ✅", data="leave_all_groups_execute")],
            [Button.inline("• إلغاء • ↩️", data="control_settings_account")]
        ]
    )

async def leave_all_groups_execute(event):
    user_id = event.chat_id
    phone = user_leave_group_accounts.get(user_id)
    if not phone:
        await event.answer("❌ اختر الحساب أولاً!", alert=True)
        return

    account = await get_owned_stored_account(user_id, phone)
    if not account:
        await event.answer("❌ الحساب المحدد لم يعد متاحاً!", alert=True)
        return

    await event.edit(f"⏳ جاري مغادرة جميع الكروبات للحساب +{phone} ...")
    success_count, failed_count = await leave_all_groups_with_session(account[1])
    user_leave_group_accounts.pop(user_id, None)
    await event.edit(
        f"✅ انتهت عملية مغادرة الكروبات للحساب +{phone}\n\n"
        f"• تم مغادرة: {success_count}\n"
        f"• فشل: {failed_count}",
        buttons=[[Button.inline("• رجوع • ↩️", data="control_settings_account")]]
    )

async def leave_channels_account_menu(event):
    user_id = event.chat_id
    accounts = await get_stored_accounts(user_id)
    if not accounts:
        await event.answer("❌ لا توجد حسابات مخزنة!", alert=True)
        return

    item_buttons = [
        Button.inline(f"+{account[0]}", data=f"leave_channels_account:{account[0]}")
        for account in accounts
    ]
    buttons = arrange_buttons(item_buttons)
    buttons.append([Button.inline("• رجوع • ↩️", data="control_settings_account")])
    await event.edit("📱 اختر الحساب الذي تريد تنفيذ مغادرة القنوات عليه:", buttons=buttons)

async def leave_channels_account_selected(event, data):
    user_id = event.chat_id
    phone = data.split(":", 1)[1]
    account = await get_owned_stored_account(user_id, phone)
    if not account:
        await event.answer("❌ لم يتم العثور على الحساب!", alert=True)
        return

    user_leave_channel_accounts[user_id] = phone
    user_leave_channel_targets.pop(user_id, None)
    await event.edit(
        f"🚪 الحساب المحدد: +{phone}\n\nاختر العملية:",
        buttons=[
            [Button.inline("• مغادرة قناة معيّنة •", data="leave_specific_channel_menu")],
            [Button.inline("• مغادرة جميع القنوات •", data="leave_all_channels_confirm")],
            [Button.inline("• رجوع • ↩️", data="leave_channels_menu")]
        ]
    )

async def leave_specific_channel_menu(event):
    user_id = event.chat_id
    phone = user_leave_channel_accounts.get(user_id)
    if not phone:
        await event.answer("❌ اختر الحساب أولاً!", alert=True)
        return

    account = await get_owned_stored_account(user_id, phone)
    if not account:
        await event.answer("❌ الحساب المحدد لم يعد متاحاً!", alert=True)
        return

    await event.edit(f"⏳ جاري جلب القنوات للحساب +{phone} ...")
    channels = await get_joined_channels_for_session(account[1])
    if not channels:
        await event.edit(
            f"❌ لا توجد قنوات أو مجموعات سوبر في الحساب +{phone}.",
            buttons=[[Button.inline("• رجوع • ↩️", data=f"leave_channels_account:{phone}")]]
        )
        return

    user_leave_channel_targets[user_id] = channels
    item_buttons = []
    for index, channel in enumerate(channels):
        title = channel["title"]
        if len(title) > 32:
            title = title[:29] + "..."
        item_buttons.append(Button.inline(title, data=f"leave_specific_channel:{index}"))

    buttons = arrange_buttons(item_buttons, pattern=(1,))
    buttons.append([Button.inline("• رجوع • ↩️", data=f"leave_channels_account:{phone}")])
    await event.edit(
        f"📢 اختر القناة التي تريد مغادرتها من الحساب +{phone}:",
        buttons=buttons
    )

async def leave_specific_channel_selected(event, data):
    user_id = event.chat_id
    phone = user_leave_channel_accounts.get(user_id)
    if not phone:
        await event.answer("❌ اختر الحساب أولاً!", alert=True)
        return

    account = await get_owned_stored_account(user_id, phone)
    if not account:
        await event.answer("❌ الحساب المحدد لم يعد متاحاً!", alert=True)
        return

    try:
        index = int(data.split(":", 1)[1])
        channel = user_leave_channel_targets.get(user_id, [])[index]
    except (ValueError, IndexError):
        await event.answer("❌ بيانات القناة غير صالحة!", alert=True)
        return

    await event.edit(f"⏳ جاري مغادرة: {channel['title']}")
    success = await leave_channel_with_session(account[1], channel["entity"])
    if success:
        user_leave_channel_targets.pop(user_id, None)
        await event.edit(
            f"✅ غادر الحساب +{phone} القناة:\n{channel['title']}",
            buttons=[[Button.inline("• رجوع • ↩️", data=f"leave_channels_account:{phone}")]]
        )
    else:
        await event.edit(
            "❌ فشلت مغادرة القناة. تأكد أن الجلسة صالحة وأن الحساب عضو في القناة.",
            buttons=[[Button.inline("• رجوع • ↩️", data=f"leave_channels_account:{phone}")]]
        )

async def leave_all_channels_confirm(event):
    user_id = event.chat_id
    phone = user_leave_channel_accounts.get(user_id)
    if not phone:
        await event.answer("❌ اختر الحساب أولاً!", alert=True)
        return

    account = await get_owned_stored_account(user_id, phone)
    if not account:
        await event.answer("❌ الحساب المحدد لم يعد متاحاً!", alert=True)
        return

    await event.edit(
        f"⚠️ تأكيد مغادرة جميع القنوات والمجموعات السوبر للحساب +{phone}؟",
        buttons=[
            [Button.inline("• تأكيد المغادرة • ✅", data="leave_all_channels_execute")],
            [Button.inline("• إلغاء • ↩️", data=f"leave_channels_account:{phone}")]
        ]
    )

async def leave_all_channels_execute(event):
    user_id = event.chat_id
    phone = user_leave_channel_accounts.get(user_id)
    if not phone:
        await event.answer("❌ اختر الحساب أولاً!", alert=True)
        return

    account = await get_owned_stored_account(user_id, phone)
    if not account:
        await event.answer("❌ الحساب المحدد لم يعد متاحاً!", alert=True)
        return

    await event.edit(f"⏳ جاري مغادرة جميع القنوات للحساب +{phone} ...")
    success_count, failed_count = await leave_all_channels_with_session(account[1])
    user_leave_channel_targets.pop(user_id, None)
    await event.edit(
        f"✅ انتهت العملية للحساب +{phone}\n\n"
        f"• تم مغادرة: {success_count}\n"
        f"• فشل: {failed_count}",
        buttons=[[Button.inline("• رجوع • ↩️", data="control_settings_account")]]
    )

async def toggle_timed_name(event, active):
    user_id = event.chat_id
    
    # الحصول على الحسابات المخزنة
    stored_accounts = await get_stored_accounts(user_id)
    if not stored_accounts:
        return await event.answer("❌ لا توجد حسابات مخزنة!", alert=True)
    
    if active:
        # تفعيل الاسم الوقتي
        await set_timed_name_active(user_id, True)
        for account in stored_accounts:
            await update_timed_name(account[1])
            await add_active_account(account[0], account[1], user_id, "تحديث الاسم الوقتي")
        await event.answer(msgs['TIMED_NAME_ACTIVATED'], alert=True)
    else:
        # إيقاف الاسم الوقتي
        await set_timed_name_active(user_id, False)
        for account in stored_accounts:
            await remove_timed_name(account[1])
            await remove_active_account(account[0])
        await event.answer(msgs['TIMED_NAME_DEACTIVATED'], alert=True)

async def change_profile_photo(event):
    async with event.client.conversation(event.chat_id) as conv:
        active_conversations[event.chat_id] = conv
        try:
            await conv.send_message("أرسل الصورة الجديدة:", buttons=cancel_operation_keyboard())
            photo = await conv.get_response(timeout=300)
            if not photo.media:
                await conv.send_message("❌ يجب إرسال صورة!")
                return
            
            # الحصول على الحسابات المخزنة للمستخدم
            stored_accounts = await get_stored_accounts(event.chat_id)
            if not stored_accounts:
                await conv.send_message("❌ لا توجد حسابات مخزنة!")
                return
            
            # تحديث الصورة لكل حساب
            for account in stored_accounts:
                session = account[1]
                try:
                    client = TelegramClient(StringSession(session), API_ID, API_HASH)
                    await client.connect()
                    await client.upload_profile_photo(await photo.download_media())
                    await client.disconnect()
                except Exception as e:
                    print(f"Error updating profile photo: {str(e)}")
            
            await conv.send_message("✅ تم تحديث الصورة الشخصية لجميع الحسابات المخزنة!")
        except asyncio.TimeoutError:
            await conv.send_message("⏱ انتهى الوقت المحدد للإدخال!")
        finally:
            if event.chat_id in active_conversations:
                del active_conversations[event.chat_id]

def profile_edit_action_text(action):
    labels = {
        "bio": "تغيير البايو",
        "username": "تغيير اسم المستخدم",
    }
    return labels.get(action, "تعديل الحساب")

async def profile_edit_account_menu(event, action):
    user_id = event.chat_id
    accounts = await get_stored_accounts(user_id)
    if not accounts:
        await event.answer("❌ لا توجد حسابات مخزنة!", alert=True)
        return

    item_buttons = [
        Button.inline(f"+{account[0]}", data=f"profile_edit_account:{action}:{account[0]}")
        for account in accounts
    ]
    buttons = arrange_buttons(item_buttons)
    buttons.append([Button.inline("• رجوع • ↩️", data="control_settings_account")])
    await event.edit(
        f"📱 اختر الحساب لتنفيذ: {profile_edit_action_text(action)}",
        buttons=buttons
    )

async def profile_edit_account_selected(event, data):
    parts = data.split(":", 2)
    if len(parts) != 3:
        await event.answer("❌ بيانات العملية غير صالحة!", alert=True)
        return

    action, phone = parts[1], parts[2]
    user_id = event.chat_id
    account = await get_owned_stored_account(user_id, phone)
    if not account:
        await event.answer("❌ لم يتم العثور على الحساب!", alert=True)
        return

    if action == "bio":
        await change_bio_for_account(event, account)
    elif action == "username":
        await change_username_for_account(event, account)
    else:
        await event.answer("❌ نوع العملية غير معروف!", alert=True)

async def change_bio_for_account(event, account):
    user_id = event.chat_id
    phone, session = account[0], account[1]
    async with event.client.conversation(user_id) as conv:
        active_conversations[user_id] = conv
        try:
            await conv.send_message(f"أرسل البايو الجديد للحساب +{phone}:", buttons=cancel_operation_keyboard())
            bio = await conv.get_response(timeout=300)
            if not bio.text:
                await conv.send_message("❌ البايو غير صالح.")
                return

            client = TelegramClient(StringSession(session), API_ID, API_HASH)
            try:
                await client.connect()
                await client(functions.account.UpdateProfileRequest(about=bio.text.strip()))
                await conv.send_message(f"✅ تم تحديث البايو للحساب +{phone} بنجاح.")
            except Exception as e:
                await conv.send_message(f"❌ فشل تحديث البايو للحساب +{phone}.\nالسبب: {e}")
            finally:
                if client.is_connected():
                    await client.disconnect()
        except asyncio.TimeoutError:
            await conv.send_message("⏱ انتهى الوقت المحدد للإدخال!")
        finally:
            if user_id in active_conversations:
                del active_conversations[user_id]

async def change_username_for_account(event, account):
    user_id = event.chat_id
    phone, session = account[0], account[1]
    async with event.client.conversation(user_id) as conv:
        active_conversations[user_id] = conv
        try:
            await conv.send_message(f"أرسل اسم المستخدم الجديد للحساب +{phone} بدون @:", buttons=cancel_operation_keyboard())
            username = await conv.get_response(timeout=300)
            new_username = (username.text or "").strip().lstrip("@")
            if not new_username:
                await conv.send_message("❌ اسم المستخدم غير صالح.")
                return

            client = TelegramClient(StringSession(session), API_ID, API_HASH)
            try:
                await client.connect()
                await client(functions.account.UpdateUsernameRequest(new_username))
                await conv.send_message(f"✅ تم تحديث اسم المستخدم للحساب +{phone} إلى @{new_username}.")
            except Exception as e:
                await conv.send_message(f"❌ فشل تحديث اسم المستخدم للحساب +{phone}.\nالسبب: {e}")
            finally:
                if client.is_connected():
                    await client.disconnect()
        except asyncio.TimeoutError:
            await conv.send_message("⏱ انتهى الوقت المحدد للإدخال!")
        finally:
            if user_id in active_conversations:
                del active_conversations[user_id]

async def enable_stealth_mode(event):
    await event.answer("✅ تم تفعيل الوضع الخفي بنجاح!", alert=True)

async def disable_stealth_mode(event):
    await event.answer("✅ تم إيقاف الوضع الخفي بنجاح!", alert=True)

async def enable_notifications(event):
    await event.answer("✅ تم تفعيل التنبيهات بنجاح!", alert=True)

async def disable_notifications(event):
    await event.answer("✅ تم إيقاف التنبيهات بنجاح!", alert=True)

async def add_mandatory_channel_handler(event):
    async with event.client.conversation(event.chat_id) as conv:
        active_conversations[event.chat_id] = conv
        try:
            await conv.send_message("أرسل معرف القناة الإجبارية:", buttons=cancel_operation_keyboard())
            channel = await conv.get_response(timeout=300)
            await set_setting("mandatory_channel", channel.text.strip())
            await conv.send_message(msgs['MANDATORY_CHANNEL_SET'])
        except asyncio.TimeoutError:
            await conv.send_message("⏱ انتهى الوقت المحدد للإدخال!")
        finally:
            if event.chat_id in active_conversations:
                del active_conversations[event.chat_id]

async def add_vip_callback(event):
    async with event.client.conversation(event.chat_id) as conv:
        active_conversations[event.chat_id] = conv
        try:
            await conv.send_message("أرسل ايدي العضو المراد رفعه مميز:", buttons=cancel_operation_keyboard())
            user_resp = await conv.get_response(timeout=300)
            try:
                vip_user_id = int(user_resp.text.strip())
            except (ValueError, AttributeError):
                await conv.send_message("❌ الايدي غير صحيح.")
                return

            await add_vip_user(vip_user_id)
            await conv.send_message(f"✅ تم رفع العضو `{vip_user_id}` إلى مميز بنجاح.", parse_mode="markdown")
        except asyncio.TimeoutError:
            await conv.send_message("⏱ انتهى الوقت المحدد للإدخال!")
        finally:
            if event.chat_id in active_conversations:
                del active_conversations[event.chat_id]

async def funding_handler(event):
    user_id = event.chat_id
    accounts = await get_service_accounts_for_user(user_id)
    if not accounts:
        message = "❌ ليس لديك حسابات مخزنة!" if user_id != ADMIN_ID else "❌ لا توجد حسابات مخزنة داخل البوت!"
        try:
            await event.answer(message, alert=True)
        except (AttributeError, TypeError):
            await event.respond(message)
        return
    
    async with event.client.conversation(user_id) as conv:
        active_conversations[user_id] = conv
        try:
            await conv.send_message(
                "أرسل رابط القناة أو المجموعة (رابط مباشر أو رابط مشاركة):\n"
                "مثال للرابط المباشر: @channel_name أو https://t.me/channel_name\n"
                "مثال لرابط المشاركة: https://t.me/c/123456789/123",
                buttons=cancel_operation_keyboard()
            )
            channel_input = await conv.get_response(timeout=300)
            input_text = channel_input.text.strip()

            # تحليل الرابط لتحديد المعرف أو ID
            target = None
            if input_text.startswith("https://t.me/"):
                parts = input_text.split("/")
                if "c/" in input_text:  # رابط مشاركة (https://t.me/c/123456789/123)
                    target = parts[3]  # الجزء الذي يحتوي على ID القناة
                else:  # رابط مباشر (https://t.me/channel_name)
                    target = parts[3].replace("@", "")
            elif input_text.startswith("@"):  # معرف مباشر (@channel_name)
                target = input_text.replace("@", "")
            else:  # قد يكون ID رقمي مباشر
                target = input_text

            if not target:
                await conv.send_message("❌ الرابط غير صالح!")
                return

            await conv.send_message(
                f"أرسل عدد الحسابات المطلوب انضمامها للقناة (المتاح: {len(accounts)}):",
                buttons=cancel_operation_keyboard()
            )
            count_resp = await conv.get_response(timeout=300)
            try:
                target_count = int(count_resp.text)
            except ValueError:
                await conv.send_message("❌ العدد غير صحيح!")
                return

            if not await validate_service_count(conv, accounts, target_count):
                return

            message = (
                "✅ تم بدء تمويل القناة باستخدام **جميع الحسابات المخزنة في البوت**!"
                if user_id == ADMIN_ID
                else "✅ تم بدء تمويل القناة باستخدام حساباتك المخزنة!"
            )
            success_count = 0
            failed_count = 0
            processed_count = 0
            processing_message = await conv.send_message(
                f"⏳ جاري معالجة الحسابات...\n"
                f"🎯 المطلوب: {target_count}\n"
                f"✅ الحسابات الناجحة: {success_count}\n"
                f"❌ الحسابات الفاشلة: {failed_count}"
            )

            for account in accounts:
                if success_count >= target_count:
                    break

                processed_count += 1
                phone, session = account[0], account[1]
                client = None
                try:
                    client = TelegramClient(StringSession(session), API_ID, API_HASH)
                    await client.connect()
                    
                    # محاولة الانضمام إلى القناة/المجموعة
                    try:
                        # محاولة الانضمام باستخدام المعرف أو ID
                        await client(JoinChannelRequest(channel=target))
                        success_count += 1
                        
                        # تحديث الرسالة كل 10 محاولات أو عند اكتمال العدد المطلوب
                        if processed_count % 10 == 0 or success_count >= target_count:
                            await processing_message.edit(
                                f"⏳ جاري المعالجة...\n"
                                f"🎯 المطلوب: {target_count}\n"
                                f"✅ الحسابات الناجحة: {success_count}\n"
                                f"❌ الحسابات الفاشلة: {failed_count}"
                            )
                            
                        await asyncio.sleep(2)  # تأخير بين المحاولات
                    except Exception as join_error:
                        print(f"فشل الانضمام للحساب {phone}: {str(join_error)}")
                        failed_count += 1
                    finally:
                        if client and client.is_connected():
                            await client.disconnect()
                            
                except Exception as client_error:
                    print(f"خطأ في جلسة الحساب {phone}: {str(client_error)}")
                    failed_count += 1
                    if client and client.is_connected():
                        await client.disconnect()

            # إرسال النتيجة النهائية
            result_message = (
                f"{message}\n\n"
                f"• الرابط المستهدف: {input_text}\n"
                f"• المعرف/ID المستخدم: {target}\n"
                f"• العدد المطلوب: {target_count}\n"
                f"• عدد الحسابات التي تمت معالجتها: {processed_count}\n"
                f"• عدد الحسابات الناجحة: {success_count}\n"
                f"• عدد الحسابات الفاشلة: {failed_count}"
            )
            
            await processing_message.edit(result_message)

        except asyncio.TimeoutError:
            await conv.send_message("⏱ انتهى الوقت المحدد للإدخال!")
        except Exception as e:
            await conv.send_message(f"❌ حدث خطأ غير متوقع: {str(e)}")
        finally:
            if user_id in active_conversations:
                del active_conversations[user_id]

async def install_session_handler(event):
    async with event.client.conversation(event.chat_id) as conv:
        active_conversations[event.chat_id] = conv
        try:
            await conv.send_message(
                "أرسل كود الجلسة (تليثون أو بايروجرام):",
                buttons=cancel_operation_keyboard()
            )
            session_response = await conv.get_response(timeout=300)
            session_str = session_response.text.strip()
            
            try:
                # التحقق من نوع الجلسة وتحويلها إذا لزم الأمر
                if session_str.startswith('1'):
                    # جلسة تليثون
                    tele_session = session_str
                else:
                    # جلسة بايروجرام، تحويل إلى تليثون
                    tele_session = MangSession.PYROGRAM_TO_TELETHON(session_str)
                
                # اختبار الجلسة
                client = TelegramClient(StringSession(tele_session), API_ID, API_HASH)
                await client.connect()
                me = await client.get_me()
                phone = me.phone
                
                # تخزين الجلسة
                await add_stored_account(phone, tele_session, event.chat_id)
                await conv.send_message(f"✅ تم تخزين الحساب بنجاح! الرقم: +{phone}")
                
            except Exception as e:
                await conv.send_message(f"❌ خطأ في الجلسة: {str(e)}")
        except asyncio.TimeoutError:
            await conv.send_message("⏱ انتهى الوقت المحدد للإدخال!")
        finally:
            if event.chat_id in active_conversations:
                del active_conversations[event.chat_id]

async def country_selected(event, data):
    try:
        # تقسيم البيانات لاستخراج رمز الدولة
        parts = data.split('_')
        if len(parts) < 2:
            return await event.answer("❌ بيانات غير صالحة!", alert=True)
            
        calling_code = parts[1]
        
        # جلب بيانات الدولة من قاعدة البيانات
        country = await get_country(calling_code)
        if not country:
            return await event.answer("❌ الدولة غير موجودة في قاعدة البيانات!", alert=True)
            
        # استخراج المعلومات المطلوبة
        name = country[1]
        price = country[2]  # السعر من قاعدة البيانات
        
        # التحقق من رصيد المستخدم
        user_data = await get_user(event.chat_id)
        coins = user_data['coins'] if user_data else 0
        
        if coins < float(price):
            await event.answer(
            msgs['INSUFFICIENT_BALANCE'].format(price),
            alert=True)
            return
        
        # جلب الأرقام المتاحة لهذه الدولة
        accounts = await get_accounts(calling_code)
        if not accounts:
            return await event.answer("❌ لا توجد أرقام متاحة حالياً لهذه الدولة!", alert=True)
        
        # إنشاء أزرار الاختيار
        item_buttons = []
        for account in accounts:
            phone = account[0]
            item_buttons.append(Button.inline(f"+{phone}", data=f"select_account_{calling_code}_{name}_{price}_{phone}"))
        buttons = arrange_buttons(item_buttons)
        
        # إضافة أزرار التنقل
        buttons.append([Button.inline("• رجوع • ↩️", data="buy")])
        buttons.append(cancel_operation_keyboard()[0])
        
        # عرض القائمة للمستخدم
        await event.edit(msgs['ACCOUNT_LIST'], buttons=buttons)
        
    except Exception as e:
        logger.error(f"Error in country_selected: {str(e)}")
        await event.answer("❌ حدث خطأ أثناء معالجة طلبك!", alert=True)

async def select_account(event, data):
    parts = data.split('_')
    calling_code = parts[2]
    name = parts[3]
    price = parts[4]
    phone = parts[5]
    
    buttons = arrange_buttons([
        Button.inline("• تأكيد الشراء • ✅", data=f"buy_{calling_code}_{name}_{price}_{phone}"),
        Button.inline("• رجوع • ↩️", data=f"countries_{calling_code}_{name}_{price}"),
        Button.inline("• إلغاء العملية • ❌", data="cancel_operation")
    ])
    
    await event.edit(
        msgs['BUY_MESSAGE'].format(name, phone, price),
        buttons=buttons
    )

async def buy_confirmed(event, data):
    parts = data.split('_')
    calling_code = parts[1]
    name = parts[2]
    price = parts[3]
    phone = parts[4]
    
    # تخزين عملية الشراء المؤقتة
    accounts = await get_accounts(calling_code)
    account = next((a for a in accounts if a[0] == phone), None)
    
    if not account:
        return await event.answer("الحساب غير موجود!", alert=True)
    
    await add_pending_purchase(
        event.chat_id,
        phone,
        calling_code,
        name,
        price,
        account[1],  # session
        account[3]   # twofa
    )
    
    # عرض خيارات ما بعد الشراء
    buttons = arrange_buttons([
        Button.inline("• تم الشراء • ✅", data=f"confirm_purchase:{phone}"),
        Button.inline("• تخزين الحساب • 💾", data=f"store_account:{phone}"),
        Button.inline("• خروج جميع الجلسات • 🚪", data=f"logout:{phone}:{calling_code}:{name}:{price}"),
        Button.inline("• إلغاء العملية • ❌", data="cancel_operation")
    ])
    
    await event.edit(
        "✅ تم حجز الحساب بنجاح! اختر الإجراء:",
        buttons=buttons
    )

async def confirm_purchase_handler(event, data):
    phone = data.split(':')[1]
    user_id = event.chat_id

    async with get_user_lock(user_id):
        purchase = await get_pending_purchase(user_id, phone)
        if not purchase:
            return await event.answer("لم يتم العثور على عملية شراء!", alert=True)
        if pending_purchase_status(purchase) != "pending":
            return await event.answer("هذه العملية قيد المعالجة أو مكتملة بالفعل.", alert=True)

        await execute_query(
            "UPDATE pending_purchases SET status='processing', processing=1 WHERE user_id=? AND phone=? AND status='pending'",
            (user_id, phone),
            commit=True
        )

        user_data = await get_user(user_id)
        price = float(purchase[5])
        if not user_data or user_data['coins'] < price:
            await execute_query(
                "UPDATE pending_purchases SET status='pending', processing=0 WHERE user_id=? AND phone=?",
                (user_id, phone),
                commit=True
            )
            return await event.answer("رصيدك غير كافٍ لإكمال الشراء.", alert=True)

        await update_user_coins(user_id, user_data['coins'] - price)

        session_str = purchase[6]
        success = await logout_all_sessions(session_str)

        if success:
            await delete_account(phone)
            await add_purchased_account(phone, session_str, user_id, purchase[3], purchase[4], price, twofa=purchase[7])
            await join_mandatory_channel(session_str)
            await publish_purchase_proof(event.client, user_id, phone, purchase[4], price)
            await execute_query(
                "UPDATE pending_purchases SET status='completed', processing=0 WHERE user_id=? AND phone=?",
                (user_id, phone),
                commit=True
            )
            await event.answer(msgs['PURCHASE_CONFIRMED'], alert=True)
            await event.edit(msgs['PURCHASE_CONFIRMED'])
        else:
            await update_user_coins(user_id, user_data['coins'])
            await execute_query(
                "UPDATE pending_purchases SET status='pending', processing=0 WHERE user_id=? AND phone=?",
                (user_id, phone),
                commit=True
            )
            await event.answer(msgs['LOGOUT_FAILED'], alert=True)

        await delete_pending_purchase(user_id, phone)

async def store_account_handler(event, data):
    phone = data.split(':')[1]
    user_id = event.chat_id

    async with get_user_lock(user_id):
        purchase = await get_pending_purchase(user_id, phone)
        if not purchase:
            return await event.answer("لم يتم العثور على عملية شراء!", alert=True)
        if pending_purchase_status(purchase) != "pending":
            return await event.answer("هذه العملية قيد المعالجة أو مكتملة بالفعل.", alert=True)

        user_data = await get_user(user_id)
        price = float(purchase[5])
        if not user_data or user_data['coins'] < price:
            return await event.answer("رصيدك غير كافٍ لإكمال الشراء.", alert=True)

        await execute_query(
            "UPDATE pending_purchases SET status='processing', processing=1 WHERE user_id=? AND phone=? AND status='pending'",
            (user_id, phone),
            commit=True
        )
        await update_user_coins(user_id, user_data['coins'] - price)
        await add_stored_account(phone, purchase[6], user_id, purchase[7])
        await add_purchased_account(phone, purchase[6], user_id, purchase[3], purchase[4], price, twofa=purchase[7])
        await join_mandatory_channel(purchase[6])
        await delete_account(phone)
        await delete_pending_purchase(user_id, phone)
        await publish_purchase_proof(event.client, user_id, phone, purchase[4], price)

        await event.answer(msgs['STORAGE_SUCCESS'], alert=True)
        await main_menu(event)

async def logout_account(event, data):
    parts = data.split(':')
    phone = parts[1]
    calling_code = parts[2]
    name = parts[3]
    price = parts[4]
    
    accounts = await get_accounts(calling_code)
    account = next((a for a in accounts if a[0] == phone), None)
    
    if not account:
        return await event.answer("الحساب غير موجود!", alert=True)
    
    # تسجيل الخروج من جميع الجلسات
    success = await logout_all_sessions(account[1])
    if success:
        await event.answer(msgs['LOGOUT_SUCCESS'], alert=True)
    else:
        await event.answer(msgs['LOGOUT_FAILED'], alert=True)
    
    # حذف الحساب من قاعدة البيانات بعد تسليمه
    await delete_account(phone)

async def show_accounts(event, data):
    parts = data.split('_')
    calling_code = parts[1]
    name = parts[2]
    price = parts[3]
    
    accounts = await get_accounts(calling_code)
    if not accounts:
        await event.answer("لا توجد حسابات!", alert=True)
        return
    
    item_buttons = []
    for i, acc in enumerate(accounts, 1):
        item_buttons.append(Button.inline(f"{i}: +{acc[0]}", data=f"v:{acc[0]}:{calling_code}:{name}:{price}"))
    buttons = arrange_buttons(item_buttons)
    buttons.append([Button.inline("• رجوع • ↩️", data="del_account")])
    buttons.append(cancel_operation_keyboard()[0])
    await event.edit(f"حسابات {name}:", buttons=buttons)

async def account_details(event, data):
    parts = data.split(':')
    phone = parts[1]
    calling_code = parts[2]
    name = parts[3]
    price = parts[4]
    
    accounts = await get_accounts(calling_code)
    account = next((a for a in accounts if a[0] == phone), None)
    
    if not account:
        return await event.answer("الحساب غير موجود!", alert=True)
    
    buttons = arrange_buttons([
        Button.inline("• حذف الحساب • 🗑️", data=f"del:{phone}:{calling_code}:{name}"),
        Button.inline("• رجوع • ↩️", data=f"show_{calling_code}_{name}_{price}"),
        Button.inline("• إلغاء العملية • ❌", data="cancel_operation")
    ])
    await event.edit(f"رقم الهاتف: +{phone}\nكلمة المرور: {account[3]}", buttons=buttons)

async def del_account_confirm(event, data):
    parts = data.split(':')
    phone = parts[1]
    calling_code = parts[2]
    name = parts[3]
    
    buttons = arrange_buttons([
        Button.inline("• إلغاء • ↩️", data=f"v:{phone}:{calling_code}:{name}"),
        Button.inline("• تأكيد الحذف • ✅", data=f"del_done:{phone}:{calling_code}:{name}"),
        Button.inline("• إلغاء العملية • ❌", data="cancel_operation")
    ])
    await event.edit(f"هل تريد حذف +{phone}؟", buttons=buttons)

async def del_account_done(event, data):
    parts = data.split(':')
    phone = parts[1]
    calling_code = parts[2]
    name = parts[3]
    
    await delete_account(phone)
    await event.edit(f"✅ تم حذف +{phone} بنجاح!")

async def add_number_process(event, data):
    # استخراج رمز الدولة فقط من البيانات
    calling_code = data.split('_')[1]
    
    # جلب بيانات الدولة من قاعدة البيانات
    country = await get_country(calling_code)
    if not country:
        await event.answer("❌ الدولة غير موجودة!", alert=True)
        return
        
    async with event.client.conversation(event.chat_id) as conv:
        active_conversations[event.chat_id] = conv
        try:
            await conv.send_message("أرسل رقم الهاتف (مثال: +9647801234567):", buttons=cancel_operation_keyboard())
            phone_resp = await conv.get_response(timeout=300)
            phone = phone_resp.text.replace("+", "").replace(" ", "")
            
            # التحقق من أن الرقم يطابق رمز الدولة
            if not phone.startswith(calling_code):
                await conv.send_message(f"❌ الرقم يجب أن يبدأ ب{calling_code} لهذه الدولة!")
                return
            
            # عملية تسجيل الدخول
            client = TelegramClient(StringSession(), API_ID, API_HASH)
            await client.connect()
            try:
                await client.send_code_request(f"+{phone}")
                await conv.send_message("أرسل الكود (5 أرقام):", buttons=cancel_operation_keyboard())
                code_resp = await conv.get_response(timeout=300)
                code = code_resp.text.replace(" ", "")
                
                try:
                    # محاولة تسجيل الدخول بدون كلمة مرور
                    await client.sign_in(f"+{phone}", code)
                    twofa = 'لا يوجد'
                except SessionPasswordNeededError:
                    # إذا طلب كلمة المرور
                    await conv.send_message("🔐 الحساب محمي بتحقق بخطوتين. أرسل كلمة المرور:", buttons=cancel_operation_keyboard())
                    password_resp = await conv.get_response(timeout=300)
                    await client.sign_in(password=password_resp.text)
                    twofa = password_resp.text
                
                # حفظ الجلسة
                session_str = client.session.save()
                
                # إضافة الحساب إلى قاعدة البيانات
                await add_account(phone, session_str, calling_code, twofa)
                await conv.send_message(f"✅ تم إضافة الحساب بنجاح!")
            except Exception as e:
                await conv.send_message(f"❌ خطأ: {str(e)}")
        except asyncio.TimeoutError:
            await conv.send_message("⏱ انتهى الوقت المحدد للإدخال!")
        finally:
            if event.chat_id in active_conversations:
                del active_conversations[event.chat_id]

async def del_country(event, data):
    parts = data.split('_')
    calling_code = parts[1]
    name = parts[2]
    price = parts[3]
    
    await delete_country(calling_code)
    await event.edit(f"✅ تم حذف {name} بنجاح!")

async def next_sell(event, data):
    phone = data.split(':')[1]
    async with event.client.conversation(event.chat_id) as conv:
        active_conversations[event.chat_id] = conv
        try:
            # عملية تسجيل الدخول
            client = TelegramClient(StringSession(), API_ID, API_HASH)
            await client.connect()
            try:
                await client.send_code_request(f"+{phone}")
                await conv.send_message("أرسل الكود (5 أرقام):", buttons=cancel_operation_keyboard())
                code_resp = await conv.get_response(timeout=300)
                await client.sign_in(f"+{phone}", code_resp.text.replace(" ", ""))
                
                # حفظ الجلسة
                session_str = client.session.save()
                await conv.send_message("✅ تم التحقق بنجاح! اضغط تحقق بعد تسجيل الخروج من الجلسات الأخرى", 
                                      buttons=[[Button.inline("• تحقق • ✅", data=f"check:{phone}")]])
            except Exception as e:
                await conv.send_message(f"❌ خطأ: {str(e)}")
        except asyncio.TimeoutError:
            await conv.send_message("⏱ انتهى الوقت المحدد للإدخال!")
        finally:
            if event.chat_id in active_conversations:
                del active_conversations[event.chat_id]

async def check_account(event, data):
    phone = data.split(':')[1]
    pending_sale = await get_pending_sale(phone)
    if not pending_sale:
        await event.answer("❌ لم يتم العثور على الحساب المعلق!", alert=True)
        return

    sessions = await count_ses(pending_sale[4])
    if sessions == 0:
        await event.answer(msgs['VERIFICATION_SUCCESS'], alert=True)
    else:
        await event.answer(msgs['SELL_VERIFICATION_FAILED'], alert=True)

async def add_force_channel_handler(event):
    async with event.client.conversation(event.chat_id) as conv:
        active_conversations[event.chat_id] = conv
        try:
            await conv.send_message("أرسل معرف القناة:", buttons=cancel_operation_keyboard())
            channel = await conv.get_response(timeout=300)
            await add_force_channel(channel.text.strip())
            await conv.send_message("✅ تم إضافة القناة بنجاح!")
        except asyncio.TimeoutError:
            await conv.send_message("⏱ انتهى الوقت المحدد للإدخال!")
        finally:
            if event.chat_id in active_conversations:
                del active_conversations[event.chat_id]

async def del_force_channel_handler(event):
    async with event.client.conversation(event.chat_id) as conv:
        active_conversations[event.chat_id] = conv
        try:
            await conv.send_message("أرسل معرف القناة:", buttons=cancel_operation_keyboard())
            channel = await conv.get_response(timeout=300)
            await remove_force_channel(channel.text.strip())
            await conv.send_message("✅ تم حذف القناة بنجاح!")
        except asyncio.TimeoutError:
            await conv.send_message("⏱ انتهى الوقت المحدد للإدخال!")
        finally:
            if event.chat_id in active_conversations:
                del active_conversations[event.chat_id]

async def add_admin(event):
    async with event.client.conversation(event.chat_id) as conv:
        active_conversations[event.chat_id] = conv
        try:
            await conv.send_message("أرسل ايدي العضو:", buttons=cancel_operation_keyboard())
            user_id = await conv.get_response(timeout=300)
            try:
                await execute_query(
                    "INSERT OR IGNORE INTO admins (user_id) VALUES (?)",
                    (int(user_id.text),),
                    commit=True
                )
                await conv.send_message("✅ تم رفع العضو إلى ادمن بنجاح!")
            except:
                await conv.send_message("❌ ايدي غير صالح!")
        except asyncio.TimeoutError:
            await conv.send_message("⏱ انتهى الوقت المحدد للإدخال!")
        finally:
            if event.chat_id in active_conversations:
                del active_conversations[event.chat_id]

async def del_admin(event):
    async with event.client.conversation(event.chat_id) as conv:
        active_conversations[event.chat_id] = conv
        try:
            await conv.send_message("أرسل ايدي العضو:", buttons=cancel_operation_keyboard())
            user_id = await conv.get_response(timeout=300)
            try:
                await execute_query(
                    "DELETE FROM admins WHERE user_id=?",
                    (int(user_id.text),),
                    commit=True
                )
                await conv.send_message("✅ تم حذف العضو من الادمنية بنجاح!")
            except:
                await conv.send_message("❌ ايدي غير صالح!")
        except asyncio.TimeoutError:
            await conv.send_message("⏱ انتهى الوقت المحدد للإدخال!")
        finally:
            if event.chat_id in active_conversations:
                del active_conversations[event.chat_id]

async def add_coins(event):
    async with event.client.conversation(event.chat_id) as conv:
        active_conversations[event.chat_id] = conv
        try:
            await conv.send_message("أرسل ايدي العضو:", buttons=cancel_operation_keyboard())
            user_id = await conv.get_response(timeout=300)
            await conv.send_message("أرسل المبلغ:", buttons=cancel_operation_keyboard())
            amount = await conv.get_response(timeout=300)
            
            try:
                user_data = await get_user(int(user_id.text))
                current_coins = user_data['coins'] if user_data else 0
                new_coins = current_coins + float(amount.text)
                await update_user_coins(int(user_id.text), new_coins)
                await conv.send_message(f"✅ تم إضافة {amount.text}$ للعضو {user_id.text}!")
            except:
                await conv.send_message("❌ قيم غير صالحة!")
        except asyncio.TimeoutError:
            await conv.send_message("⏱ انتهى الوقت المحدد للإدخال!")
        finally:
            if event.chat_id in active_conversations:
                del active_conversations[event.chat_id]

async def del_coins(event):
    async with event.client.conversation(event.chat_id) as conv:
        active_conversations[event.chat_id] = conv
        try:
            await conv.send_message("أرسل ايدي العضو:", buttons=cancel_operation_keyboard())
            user_id = await conv.get_response(timeout=300)
            await conv.send_message("أرسل المبلغ:", buttons=cancel_operation_keyboard())
            amount = await conv.get_response(timeout=300)
            
            try:
                user_data = await get_user(int(user_id.text))
                current_coins = user_data['coins'] if user_data else 0
                new_coins = current_coins - float(amount.text)
                await update_user_coins(int(user_id.text), new_coins)
                await conv.send_message(f"✅ تم خصم {amount.text}$ من العضو {user_id.text}!")
            except:
                await conv.send_message("❌ قيم غير صالحة!")
        except asyncio.TimeoutError:
            await conv.send_message("⏱ انتهى الوقت المحدد للإدخال!")
        finally:
            if event.chat_id in active_conversations:
                del active_conversations[event.chat_id]

async def ban_user(event):
    async with event.client.conversation(event.chat_id) as conv:
        active_conversations[event.chat_id] = conv
        try:
            await conv.send_message("أرسل ايدي العضو:", buttons=cancel_operation_keyboard())
            user_id = await conv.get_response(timeout=300)
            try:
                await execute_query(
                    "INSERT OR IGNORE INTO bad_guys (user_id) VALUES (?)",
                    (int(user_id.text),),
                    commit=True
                )
                await conv.send_message("✅ تم حظر العضو بنجاح!")
            except:
                await conv.send_message("❌ ايدي غير صالح!")
        except asyncio.TimeoutError:
            await conv.send_message("⏱ انتهى الوقت المحدد للإدخال!")
        finally:
            if event.chat_id in active_conversations:
                del active_conversations[event.chat_id]

async def unban_user(event):
    async with event.client.conversation(event.chat_id) as conv:
        active_conversations[event.chat_id] = conv
        try:
            await conv.send_message("أرسل ايدي العضو:", buttons=cancel_operation_keyboard())
            user_id = await conv.get_response(timeout=300)
            try:
                await execute_query(
                    "DELETE FROM bad_guys WHERE user_id=?",
                    (int(user_id.text),),
                    commit=True
                )
                await conv.send_message("✅ تم إلغاء حظر العضو بنجاح!")
            except:
                await conv.send_message("❌ ايدي غير صالح!")
        except asyncio.TimeoutError:
            await conv.send_message("⏱ انتهى الوقت المحدد للإدخال!")
        finally:
            if event.chat_id in active_conversations:
                del active_conversations[event.chat_id]

async def confirm_withdraw(event, data):
    user_id = int(data.split('_')[-1])
    await event.client.send_message(user_id, "✅ تم تحويل الرصيد بنجاح!")
    await event.edit("تم إرسال التأكيد للعضو!")

async def reply_to_user(event, data):
    user_id = int(data.split('_')[-1])
    async with event.client.conversation(event.chat_id) as conv:
        active_conversations[event.chat_id] = conv
        try:
            await conv.send_message("أرسل ردك:", buttons=cancel_operation_keyboard())
            response = await conv.get_response(timeout=300)
            await event.client.send_message(user_id, f"📩 رد الدعم:\n{response.text}")
            await conv.send_message("✅ تم إرسال الرد!")
        except asyncio.TimeoutError:
            await conv.send_message("⏱ انتهى الوقت المحدد للإدخال!")
        finally:
            if event.chat_id in active_conversations:
                del active_conversations[event.chat_id]

async def zip_database(event):
    folder_path = "./database"
    zip_file = "database.zip"
    try:
        shutil.make_archive("database", 'zip', folder_path)
        await event.client.send_file(event.chat_id, zip_file)
        os.remove(zip_file)
    except Exception as e:
        await event.answer(f"❌ خطأ: {str(e)}", alert=True)

async def sell_logout_handler(event, data):
    phone = data.split('_')[2]
    
    # الحصول على جلسة الحساب من المبيعات المعلقة
    pending_sale = await get_pending_sale(phone)
    if not pending_sale:
        await event.answer("❌ لم يتم العثور على الحساب المعلق!", alert=True)
        return
    
    session_str = pending_sale[4]  # session
    
    # التحقق من عدد الجلسات مع إعادة المحاولة لأن تسجيل الخروج قد يتأخر
    session_count = 0
    for attempt in range(3):
        session_count = await count_ses(session_str)
        if session_count == 0:
            break
        if attempt < 2:
            await asyncio.sleep(3)
    
    # إذا لم توجد جلسات أخرى غير الجلسة الحالية
    if session_count == 0:
        # نقل الحساب إلى قاعدة البيانات الرئيسية
        seller_id, price, calling_code, session_str, twofa = pending_sale[1], pending_sale[2], pending_sale[3], pending_sale[4], pending_sale[5]
        await add_account(phone, session_str, calling_code, twofa, seller_id)
        await add_sold_account(phone, session_str, seller_id)
        await delete_pending_sale(phone)
        
        # إضافة الرصيد للبائع
        seller_data = await get_user(seller_id)
        if seller_data:
            seller_coins = seller_data['coins']
            await update_user_coins(seller_id, seller_coins + float(price))
        
        await event.answer(msgs['VERIFICATION_SUCCESS'], alert=True)
        await main_menu(event)
    else:
        await event.answer(msgs['SELL_VERIFICATION_FAILED'], alert=True)

async def all_numbers_count(event):
    result = await execute_query("SELECT COUNT(*) FROM accounts", (), fetchone=True)
    count = result[0] if result else 0
    await event.answer(f"عدد ارقام البوت: {count}", alert=True)

async def add_country_handler(event):
    async with event.client.conversation(event.chat_id) as conv:
        active_conversations[event.chat_id] = conv
        try:
            await conv.send_message("أرسل اسم الدولة مع الرمز (مثال: العراق 🇮🇶):", buttons=cancel_operation_keyboard())
            name = await conv.get_response(timeout=300)
            
            await conv.send_message(f"أرسل رمز النداء (مثال: +964):", buttons=cancel_operation_keyboard())
            calling_code = await conv.get_response(timeout=300)
            
            await conv.send_message(f"أرسل سعر الشراء ($):", buttons=cancel_operation_keyboard())
            price = await conv.get_response(timeout=300)
            
            await conv.send_message(f"أرسل سعر البيع ($):", buttons=cancel_operation_keyboard())
            sell_price = await conv.get_response(timeout=300)
            
            try:
                await add_new_country(
                    name.text,
                    calling_code.text.replace("+", ""),
                    float(price.text),
                    float(sell_price.text)
                )
                await conv.send_message(f"✅ تم إضافة {name.text} بنجاح!")
            except Exception as e:
                await conv.send_message(f"❌ خطأ: {str(e)}")
        except asyncio.TimeoutError:
            await conv.send_message("⏱ انتهى الوقت المحدد للإدخال!")
        finally:
            if event.chat_id in active_conversations:
                del active_conversations[event.chat_id]

async def del_country_menu(event):
    countries = await get_countries()
    item_buttons = []
    for c in countries:
        item_buttons.append(Button.inline(f"{c[1]}: {c[2]}$", data=f"delete_{c[0]}_{c[1]}_{c[2]}"))
    buttons = arrange_buttons(item_buttons)
    buttons.append([Button.inline("• رجوع • ↩️", data="ajxjao")])
    buttons.append(cancel_operation_keyboard()[0])
    await event.edit("اختر الدولة للحذف:", buttons=buttons)

async def add_number_menu(event):
    add_number_country_pages[event.chat_id] = 0
    await show_add_number_countries_page(event)

async def show_add_number_countries_page(event):
    user_id = event.chat_id
    countries = await get_countries()
    if not countries:
        await event.edit("⭕️ لا توجد دول متاحة حالياً")
        return

    if user_id not in add_number_country_pages:
        add_number_country_pages[user_id] = 0

    per_page = 20
    current_page = add_number_country_pages[user_id]
    max_page = max(0, (len(countries) - 1) // per_page)
    if current_page > max_page:
        current_page = max_page
        add_number_country_pages[user_id] = current_page

    start_index = current_page * per_page
    end_index = min(start_index + per_page, len(countries))
    country_chunk = countries[start_index:end_index]

    item_buttons = []
    for i in range(0, len(country_chunk), 2):
        c1 = country_chunk[i]
        item_buttons.append(Button.inline(f"{c1[1]}", data=f"rig_{c1[0]}"))
        if i + 1 < len(country_chunk):
            c2 = country_chunk[i + 1]
            item_buttons.append(Button.inline(f"{c2[1]}", data=f"rig_{c2[0]}"))
    buttons = arrange_buttons(item_buttons)

    nav_buttons = []
    if current_page > 0:
        nav_buttons.append(Button.inline("◀ السابق", data="prev_add_number_countries"))
    if end_index < len(countries):
        nav_buttons.append(Button.inline("التالي ▶", data="next_add_number_countries"))
    if nav_buttons:
        buttons.append(nav_buttons)

    buttons.append([Button.inline("• رجوع • ↩️", data="ajxjao")])
    buttons.append(cancel_operation_keyboard()[0])
    await event.edit(
        f"اختر الدولة لإضافة رقم:\n\n• الصفحة: {current_page + 1}/{max_page + 1}",
        buttons=buttons
    )

async def next_add_number_countries_page(event):
    user_id = event.chat_id
    countries = await get_countries()
    if not countries:
        await event.answer("⭕️ لا توجد دول متاحة حالياً", alert=True)
        return

    current_page = add_number_country_pages.get(user_id, 0)
    max_page = max(0, (len(countries) - 1) // 20)
    if current_page >= max_page:
        await event.answer("❗️ هذه آخر صفحة", alert=True)
        return

    add_number_country_pages[user_id] = current_page + 1
    await show_add_number_countries_page(event)

async def prev_add_number_countries_page(event):
    user_id = event.chat_id
    current_page = add_number_country_pages.get(user_id, 0)
    if current_page <= 0:
        await event.answer("❗️ هذه أول صفحة", alert=True)
        return

    add_number_country_pages[user_id] = current_page - 1
    await show_add_number_countries_page(event)

async def del_account_menu(event):
    countries = await get_countries()
    item_buttons = []
    for c in countries:
        item_buttons.append(Button.inline(f"{c[1]}", data=f"show_{c[0]}"))
    buttons = arrange_buttons(item_buttons)
    buttons.append([Button.inline("• رجوع • ↩️", data="ajxjao")])
    buttons.append(cancel_operation_keyboard()[0])
    await event.edit("اختر الدولة لحذف رقم منها:", buttons=buttons)

async def cancel_operation(event):
    try:
        if hasattr(event, 'data') and event.data:
            data = event.data.decode('utf-8')
            # معالجة البيانات إذا كانت موجودة
    except Exception as e:
        logger.error(f"Error in cancel_operation: {str(e)}")
    
    user_id = event.chat_id
    if user_id in active_conversations:
        active_conversations[user_id].cancel()
        del active_conversations[user_id]
    await event.answer(msgs['OPERATION_CANCELLED'], alert=True)
    await main_menu(event)
    
async def main_menu(event):
    user_id = event.chat_id
    user_data = await get_user(user_id)
    coins = user_data['coins'] if user_data else 0
    await event.edit(msgs['START_MESSAGE'].format(
        user_id, 
        coins
    ), buttons=start_keyboard(user_id, await is_admin(user_id)))

async def accounts_view_menu(event):
    await event.edit("**👤︙قائمة عرض الحسابات:**\nاختر نوع الحسابات التي تريد عرضها:", buttons=accounts_view_keyboard())

async def view_purchased_accounts(event):
    user_id = event.chat_id
    accounts = await get_purchased_accounts(user_id)
    if not accounts:
        await event.answer("❌ لا توجد حسابات مشتراة!", alert=True)
        return
    
    item_buttons = []
    for account in accounts:
        phone = account[0]
        item_buttons.append(Button.inline(f"+{phone}", data=f"purchased_account:{phone}"))
    buttons = arrange_buttons(item_buttons)
    
    buttons.append([Button.inline("• رجوع • ↩️", data="accounts_view")])
    await event.edit("**🛒 الحسابات المشتراة:**\nاختر حساباً:", buttons=buttons)

async def view_stored_accounts(event):
    user_id = event.chat_id
    accounts = await get_all_stored_accounts() if user_id == ADMIN_ID else await get_stored_accounts(user_id)
    sale_accounts = await get_all_accounts() if user_id == ADMIN_ID else []

    if not accounts and not sale_accounts:
        await event.answer("❌ لا توجد حسابات مخزنة!", alert=True)
        return
    
    item_buttons = []
    seen_phones = set()
    for account in accounts:
        phone = account[0]
        seen_phones.add(phone)
        owner = f" - {account[2]}" if user_id == ADMIN_ID and len(account) > 2 else ""
        item_buttons.append(Button.inline(f"+{phone}{owner}", data=f"stored_account:{phone}"))

    for account in sale_accounts:
        phone = account[0]
        if phone in seen_phones:
            continue
        item_buttons.append(Button.inline(f"+{phone} - للبيع", data=f"bot_account:{phone}"))
    buttons = arrange_buttons(item_buttons)
    
    buttons.append([Button.inline("• رجوع • ↩️", data="accounts_view")])
    title = "**💾 كل الأرقام المخزنة داخل البوت:**" if user_id == ADMIN_ID else "**💾 الحسابات المخزنة:**"
    await event.edit(f"{title}\nاختر حساباً:", buttons=buttons)


async def purchased_account_selected(event, data):
    phone = data.split(':')[1]
    user_id = event.chat_id
    
    # البحث عن الحساب المحدد
    accounts = user_purchased_accounts.get(user_id, [])
    account = next((acc for acc in accounts if acc[0] == phone), None)
    
    if not account:
        await event.answer("❌ لم يتم العثور على الحساب!", alert=True)
        return
    
    # جلب بيانات إضافية من جدول accounts
    account_details = await execute_query(
        "SELECT calling_code, name, price FROM accounts WHERE phone=?",
        (phone,),
        fetchone=True
    )
    
    if not account_details:
        await event.answer("❌ تفاصيل الحساب غير متوفرة!", alert=True)
        return
    
    calling_code, name, price = account_details
    
    # إنشاء أزرار الإجراءات
    buttons = arrange_buttons([
        Button.inline("• تم الشراء • ✅", data=f"confirm_purchase:{phone}"),
        Button.inline("• تخزين الحساب • 💾", data=f"store_account:{phone}"),
        Button.inline("• خروج جميع الجلسات • 🚪", data=f"logout:{phone}"),
        Button.inline("• رجوع • ↩️", data="view_purchased")
    ])
    
    await event.edit(f"**خيارات الحساب +{phone}:**\nاختر الإجراء المطلوب:", buttons=buttons)
    
async def view_sold_accounts(event):
    user_id = event.chat_id
    accounts = await get_sold_accounts(user_id)
    if not accounts:
        await event.answer("❌ لا توجد حسابات مباعة!", alert=True)
        return
    
    text = "**💰 الحسابات المباعة:**\n\n"
    for i, account in enumerate(accounts, 1):
        text += f"{i}. +{account[0]}\n"
    
    await event.answer(text, alert=True)

async def view_active_accounts(event):
    user_id = event.chat_id
    accounts = await get_active_accounts(user_id)
    if not accounts:
        await event.answer("❌ لا توجد حسابات نشطة!", alert=True)
        return
    
    text = "**🔥 الحسابات النشطة:**\n\n"
    for i, account in enumerate(accounts, 1):
        text += f"{i}. +{account[0]} - {account[3]}\n"
    
    await event.answer(text, alert=True)

async def broadcast_start(event):
    user_id = event.chat_id
    if user_id != ADMIN_ID and not await is_admin(user_id):
        await event.answer("❌ ليس لديك صلاحية!", alert=True)
        return

    async with event.client.conversation(user_id) as conv:
        active_conversations[user_id] = conv
        try:
            await conv.send_message("✉️ أرسل الرسالة التي تريد بثها لجميع المستخدمين:", buttons=cancel_operation_keyboard())
            msg = await conv.get_response(timeout=300)
            text = msg.text

            users = await execute_query("SELECT user_id FROM users", fetchall=True)
            success = 0
            failed = 0

            for user in users:
                try:
                    await event.client.send_message(user[0], text)
                    success += 1
                    await asyncio.sleep(0.1)
                except:
                    failed += 1

            await conv.send_message(f"✅ تم إرسال الإذاعة!\n\nتم الإرسال إلى: {success} ✅\nفشل الإرسال إلى: {failed} ❌")
        except asyncio.TimeoutError:
            await conv.send_message("⏱ انتهى الوقت، لم يتم إرسال الإذاعة.")
        finally:
            if user_id in active_conversations:
                del active_conversations[user_id]

# قسم التنصيب
async def install_menu_handler(event):
    await event.edit("**💾︙قائمة التنصيب:**", buttons=install_menu_keyboard())

async def delete_install_handler(event):
    user_id = event.chat_id
    accounts = await get_stored_accounts(user_id)
    
    if not accounts:
        await event.answer("❌ لا توجد حسابات مخزنة!", alert=True)
        return
    
    item_buttons = []
    for account in accounts:
        item_buttons.append(Button.inline(f"+{account[0]}", data=f"del_install:{account[0]}"))
    buttons = arrange_buttons(item_buttons)
    
    buttons.append([Button.inline("• رجوع • ↩️", data="install_menu")])
    await event.edit("اختر الحساب لحذفه:", buttons=buttons)

async def delete_install_account(event, data):
    phone = event.data.decode().split(":")[1]
    await delete_stored_account(phone)
    await event.answer(f"✅ تم حذف الحساب +{phone} بنجاح!", alert=True)
    await install_menu_handler(event)

# قسم المزاد
async def auction_menu_handler(event):
    await event.edit("**🏷️︙قائمة المزاد:**", buttons=auction_menu_keyboard())

async def add_auction_handler(event):
    async with event.client.conversation(event.chat_id) as conv:
        try:
            await conv.send_message("أرسل رقم الهاتف (مثال: +9647801234567):", 
                                  buttons=cancel_operation_keyboard())
            phone_resp = await conv.get_response(timeout=300)
            phone = phone_resp.text.replace("+", "").replace(" ", "")
            
            # تسجيل الدخول
            client = TelegramClient(StringSession(), API_ID, API_HASH)
            await client.connect()
            try:
                await client.send_code_request(f"+{phone}")
                await conv.send_message("أرسل الكود (5 أرقام):", buttons=cancel_operation_keyboard())
                code_resp = await conv.get_response(timeout=300)
                code = code_resp.text.replace(" ", "")
                
                try:
                    await client.sign_in(f"+{phone}", code)
                    twofa = 'لا يوجد'
                except SessionPasswordNeededError:
                    await conv.send_message("🔐 أرسل كلمة المرور:", buttons=cancel_operation_keyboard())
                    password_resp = await conv.get_response(timeout=300)
                    await client.sign_in(password=password_resp.text)
                    twofa = password_resp.text
                
                session_str = client.session.save()
                
                # طلب أقل سعر للمزاد
                await conv.send_message("أدخل أقل سعر للمزاد (يجب أن يكون أكثر من 0.5$):", 
                                      buttons=cancel_operation_keyboard())
                price_resp = await conv.get_response(timeout=300)
                
                try:
                    min_price = float(price_resp.text)
                    if min_price < 0.5:
                        raise ValueError
                except:
                    await conv.send_message("❌ السعر غير صالح!")
                    return
                
                # إضافة المزاد إلى قاعدة البيانات
                await execute_query(
                    '''INSERT INTO auctions 
                    (phone, session, seller_id, min_price, current_bid, status, twofa) 
                    VALUES (?, ?, ?, ?, ?, ?, ?)''',
                    (phone, encrypt_session(session_str), event.chat_id, min_price, min_price, 'active', twofa),
                    commit=True
                )
                
                # إرسال إشعار لجميع المستخدمين
                auction_id = await execute_query("SELECT last_insert_rowid()", fetchone=True)
                sent_count, failed_count = await broadcast_auction(event, auction_id[0], phone, min_price)
                
                await conv.send_message(
                    f"✅ تم إضافة المزاد بنجاح!\n"
                    f"📢 تم إرسال إشعار المزاد إلى {sent_count} مستخدم.\n"
                    f"❌ فشل الإرسال إلى {failed_count} مستخدم."
                )
            except Exception as e:
                await conv.send_message(f"❌ خطأ: {str(e)}")
        except asyncio.TimeoutError:
            await conv.send_message("⏱ انتهى الوقت المحدد للإدخال!")

async def broadcast_auction(event, auction_id, phone, min_price):
    users = await execute_query("SELECT user_id FROM users", fetchall=True) or []
    success = 0
    failed = 0
    for user in users:
        try:
            await event.client.send_message(
                user[0],
                f"🏷️ **تم إضافة مزاد جديد!**\n\n"
                f"📱 الرقم: +{phone}\n"
                f"💰 أقل سعر: {min_price}$\n\n"
                "اضغط على الزر أدناه لتقديم عرض:",
                buttons=[[Button.inline("تقديم عرض", data=f"bid:{auction_id}")]]
            )
            success += 1
            await asyncio.sleep(0.1)
        except:
            failed += 1
    return success, failed

async def place_bid_handler(event, data):
    auction_id = int(event.data.decode().split(":")[1])
    auction = await execute_query(
        "SELECT * FROM auctions WHERE auction_id=?", 
        (auction_id,), 
        fetchone=True
    )
    
    if not auction or auction[7] != 'active':  # status
        await event.answer("❌ هذا المزاد لم يعد متاحاً!", alert=True)
        return
    
    current_bid = auction[5]  # current_bid
    min_increment = max(0.1, current_bid * 0.05)  # زيادة 5% كحد أدنى
    
    async with event.client.conversation(event.chat_id) as conv:
        try:
            await conv.send_message(
                f"💰 أعلى عرض حالياً: {current_bid}$\n"
                f"⬆️ أقل زيادة مسموحة: {min_increment:.2f}$\n\n"
                "أدخل عرضك:",
                buttons=cancel_operation_keyboard()
            )
            bid_resp = await conv.get_response(timeout=300)
            
            try:
                bid_amount = float(bid_resp.text)
                if bid_amount < current_bid + min_increment:
                    await conv.send_message(f"❌ يجب أن يكون العرض أعلى من {current_bid + min_increment:.2f}$")
                    return
            except:
                await conv.send_message("❌ قيمة غير صالحة!")
                return
            
            # تحديث المزاد
            await execute_query(
                "UPDATE auctions SET current_bid=?, current_bidder=? WHERE auction_id=?",
                (bid_amount, event.chat_id, auction_id),
                commit=True
            )
            
            # إضافة العرض
            await execute_query(
                "INSERT INTO auction_bids (auction_id, user_id, bid_amount) VALUES (?, ?, ?)",
                (auction_id, event.chat_id, bid_amount),
                commit=True
            )
            
            # إشعار البائع
            await event.client.send_message(
                auction[3],  # seller_id
                f"🏷️ **تم تقديم عرض جديد على مزادك!**\n\n"
                f"📱 الرقم: +{auction[1]}\n"
                f"💰 العرض: {bid_amount}$\n"
                f"👤 المزايد: {event.chat_id}\n\n"
                "اختر الإجراء:",
                buttons=[
                    [Button.inline("بيع", data=f"sell_auction:{auction_id}:{event.chat_id}")],
                    [Button.inline("استمرار المزاد", data=f"continue_auction:{auction_id}")]
                ]
            )
            
            await conv.send_message("✅ تم تقديم عرضك بنجاح!")
        except asyncio.TimeoutError:
            await conv.send_message("⏱ انتهى الوقت المحدد للإدخال!")

async def continue_auction_handler(event, data):
    try:
        auction_id = int(data.split(":", 1)[1])
    except (ValueError, IndexError):
        await event.answer("❌ بيانات المزاد غير صالحة!", alert=True)
        return

    auction = await execute_query(
        "SELECT * FROM auctions WHERE auction_id=?",
        (auction_id,),
        fetchone=True
    )
    if not auction or auction[7] != 'active':
        await event.answer("❌ هذا المزاد لم يعد متاحاً!", alert=True)
        return

    await event.answer("✅ سيستمر المزاد واستقبال العروض.", alert=True)
    try:
        await event.edit(
            f"🏷️ **المزاد مستمر**\n\n"
            f"📱 الرقم: +{auction[1]}\n"
            f"💰 أعلى عرض حالي: {auction[5]}$\n"
            f"👤 صاحب أعلى عرض: {auction[6] or 'لا يوجد'}"
        )
    except Exception:
        pass

async def sell_auction_handler(event, data):
    auction_id = int(event.data.decode().split(":")[1])
    buyer_id = int(event.data.decode().split(":")[2])
    
    auction = await execute_query(
        "SELECT * FROM auctions WHERE auction_id=?", 
        (auction_id,), 
        fetchone=True
    )
    if not auction or auction[7] != 'active':  # status
        await event.answer("❌ هذا المزاد لم يعد متاحاً!", alert=True)
        return

    if auction[6] != buyer_id:  # current_bidder
        await event.answer("❌ هذا العرض لم يعد العرض الأعلى حالياً!", alert=True)
        return

    buyer_data = await get_user(buyer_id)
    
    # التحقق من رصيد المشتري عند موافقة صاحب المزاد
    if not buyer_data or buyer_data['coins'] < auction[5]:  # current_bid
        await event.answer("❌ رصيد المشتري غير كافي الآن!", alert=True)
        try:
            await event.client.send_message(
                buyer_id,
                f"❌ تم رفض شراء الرقم +{auction[1]} لأن رصيدك الحالي غير كافٍ لإتمام العرض {auction[5]}$."
            )
        except Exception:
            pass
        return
    
    # طلب تسجيل الخروج من البائع
    await event.answer("تم إرسال طلب تسجيل الخروج للبائع", alert=True)
    await event.client.send_message(
        auction[3],  # seller_id
        "🔒 يرجى تسجيل الخروج من جميع الجلسات ما عدا هذه الجلسة، ثم اضغط زر 'تم الخروج'",
        buttons=[[Button.inline("تم الخروج", data=f"auction_logout:{auction_id}:{buyer_id}")]]
    )

async def auction_logout_handler(event, data):
    auction_id = int(event.data.decode().split(":")[1])
    buyer_id = int(event.data.decode().split(":")[2])
    
    auction = await execute_query(
        "SELECT * FROM auctions WHERE auction_id=?", 
        (auction_id,), 
        fetchone=True
    )
    if not auction or auction[7] != 'active':  # status
        await event.answer("❌ هذا المزاد لم يعد متاحاً!", alert=True)
        return

    if auction[6] != buyer_id:  # current_bidder
        await event.answer("❌ هذا العرض لم يعد العرض الأعلى حالياً!", alert=True)
        return
    
    # التحقق من عدد الجلسات
    auction_session = decrypt_session(auction[2])
    session_count = await count_ses(auction_session)
    
    if session_count > 0:
        await event.answer("❌ لا يزال هناك جلسات نشطة!", alert=True)
        return
    
    # إتمام البيع
    # خصم المبلغ من المشتري
    buyer_data = await get_user(buyer_id)
    if not buyer_data or buyer_data['coins'] < auction[5]:
        await event.answer("❌ رصيد المشتري غير كافي الآن!", alert=True)
        return

    await update_user_coins(buyer_id, buyer_data['coins'] - auction[5])
    
    # إضافة الرصيد للبائع
    seller_data = await get_user(auction[3])  # seller_id
    await update_user_coins(auction[3], seller_data['coins'] + auction[5])
    
    # نقل الحساب للمشتري
    auction_twofa = auction[10] if len(auction) > 10 and auction[10] else "لا يوجد"
    await add_stored_account(auction[1], auction_session, buyer_id, auction_twofa)  # phone, session
    await execute_query(
        "UPDATE auctions SET status='sold' WHERE auction_id=?",
        (auction_id,),
        commit=True
    )
    
    # إرسال التنبيهات
    await event.answer("✅ تم البيع بنجاح!", alert=True)
    await event.client.send_message(
        buyer_id,
        f"✅ تم شراء الرقم +{auction[1]} بنجاح!\n"
        f"💰 السعر: {auction[5]}$\n\n"
        "تم تخزين الحساب في قسم الحسابات المخزنة"
    )
    await publish_purchase_proof(event.client, buyer_id, auction[1], "مزاد", auction[5])

async def auction_list_handler(event):
    auctions = await execute_query(
        "SELECT * FROM auctions WHERE status='active'",
        fetchall=True
    )
    
    if not auctions:
        await event.answer("❌ لا توجد مزادات نشطة حالياً!", alert=True)
        return
    
    item_buttons = []
    for auction in auctions:
        item_buttons.append(Button.inline(
            f"+{auction[1]} - {auction[5]}$",
            data=f"view_auction:{auction[0]}"
        ))
    buttons = arrange_buttons(item_buttons)
    
    buttons.append([Button.inline("• رجوع • ↩️", data="auction_menu")])
    await event.edit("🏷️ المزادات النشطة:", buttons=buttons)

async def view_auction_handler(event, data):
    auction_id = int(event.data.decode().split(":")[1])
    auction = await execute_query(
        "SELECT * FROM auctions WHERE auction_id=?", 
        (auction_id,), 
        fetchone=True
    )
    
    if not auction:
        await event.answer("❌ هذا المزاد لم يعد متاحاً!", alert=True)
        return
    
    seller_info = await get_user(auction[3])
    seller_name = seller_info.get('username', f"المستخدم {auction[3]}")
    
    text = (
        f"🏷️ **مزاد رقم:** +{auction[1]}\n"
        f"👤 البائع: {seller_name}\n"
        f"💰 أقل سعر: {auction[4]}$\n"
        f"🚀 أعلى عرض: {auction[5]}$\n\n"
        "اضغط لتقديم عرض جديد:"
    )
    
    await event.edit(
        text,
        buttons=[
            [Button.inline("تقديم عرض", data=f"bid:{auction_id}")],
            [Button.inline("• رجوع • ↩️", data="auction_list")]
        ]
    )

def register_handlers(client):
    """تسجيل جميع معالجات الأحداث للبوت"""
    client.add_event_handler(license_guard_handler, events.NewMessage(func=lambda e: e.is_private))

    client.add_event_handler(activate_license_handler, events.NewMessage(pattern=r'^/activate(?:\s+(.+))?$', func=lambda e: e.is_private))
    client.add_event_handler(license_status_handler, events.NewMessage(pattern=r'^/(status|mylicense)$', func=lambda e: e.is_private))
    client.add_event_handler(generate_license_handler, events.NewMessage(pattern=r'^/genlicense(?:\s+(\d+))?$', func=lambda e: e.is_private))
    client.add_event_handler(list_licenses_handler, events.NewMessage(pattern=r'^/licenses$', func=lambda e: e.is_private))
    client.add_event_handler(revoke_license_handler, events.NewMessage(pattern=r'^/revokelicense(?:\s+(.+))?$', func=lambda e: e.is_private))
    client.add_event_handler(clear_expired_licenses_handler, events.NewMessage(pattern=r'^/clearlicenses$', func=lambda e: e.is_private))
    client.add_event_handler(license_stats_handler, events.NewMessage(pattern=r'^/licensestats$', func=lambda e: e.is_private))
    client.add_event_handler(export_licenses_handler, events.NewMessage(pattern=r'^/exportlicenses$', func=lambda e: e.is_private))

    # معالجات الأوامر الأساسية
    client.add_event_handler(start_handler, events.NewMessage(pattern='/start', func=lambda e: e.is_private))
    client.add_event_handler(admin_panel, events.NewMessage(pattern='/admin', func=lambda e: e.is_private))
    client.add_event_handler(control_menu, events.NewMessage(pattern='لوحة التحكم', func=lambda e: e.is_private))
    client.add_event_handler(back_button, events.NewMessage(pattern='رجوع', func=lambda e: e.is_private))
    client.add_event_handler(support_request, events.NewMessage(pattern='دعم', func=lambda e: e.is_private))
    client.add_event_handler(show_rules, events.NewMessage(pattern='القوانين', func=lambda e: e.is_private))
    client.add_event_handler(sell_price_handler, events.NewMessage(pattern='/sell_price', func=lambda e: e.is_private))
    
    # معالجات إدارة الحسابات والأرقام
    client.add_event_handler(add_number_menu, events.NewMessage(pattern='إضافة رقم', func=lambda e: e.is_private))
    client.add_event_handler(buy_number, events.NewMessage(pattern='شراء رقم', func=lambda e: e.is_private))
    client.add_event_handler(sell_account, events.NewMessage(pattern='بيع حساب', func=lambda e: e.is_private))
    client.add_event_handler(view_purchased_accounts, events.NewMessage(pattern='الحسابات المشتراة', func=lambda e: e.is_private))
    client.add_event_handler(view_sold_accounts, events.NewMessage(pattern='الحسابات المباعة', func=lambda e: e.is_private))
    client.add_event_handler(view_stored_accounts, events.NewMessage(pattern='الحسابات المخزنة', func=lambda e: e.is_private))
    client.add_event_handler(view_active_accounts, events.NewMessage(pattern='الحسابات النشطة', func=lambda e: e.is_private))
    client.add_event_handler(del_account_menu, events.NewMessage(pattern='حذف حساب', func=lambda e: e.is_private))
    client.add_event_handler(logout_account, events.NewMessage(pattern='تسجيل الخروج', func=lambda e: e.is_private))
    client.add_event_handler(select_account, events.NewMessage(pattern='اختيار حساب', func=lambda e: e.is_private))
    
    # معالجات الإعدادات
    client.add_event_handler(account_settings_menu, events.NewMessage(pattern='إعدادات الحساب', func=lambda e: e.is_private))
    client.add_event_handler(balance_settings, events.NewMessage(pattern='رصيدي', func=lambda e: e.is_private))
    client.add_event_handler(creation_settings_menu, events.NewMessage(pattern='إعدادات الإنشاء', func=lambda e: e.is_private))
    client.add_event_handler(posting_settings_menu, events.NewMessage(pattern='إعدادات النشر', func=lambda e: e.is_private))
    client.add_event_handler(force_settings, events.NewMessage(pattern='القنوات الإجبارية', func=lambda e: e.is_private))
    client.add_event_handler(super_settings_menu, events.NewMessage(pattern='القنوات السوبر', func=lambda e: e.is_private))
    client.add_event_handler(ban_settings, events.NewMessage(pattern='حظر مستخدم', func=lambda e: e.is_private))
    client.add_event_handler(num_settings, events.NewMessage(pattern='إعدادات الأرقام', func=lambda e: e.is_private))
    
    # معالجات الأزرار (Callbacks)
    client.add_event_handler(callback_handler, events.CallbackQuery())
    
    # معالجات العمليات المتقدمة
    client.add_event_handler(add_country_handler, events.NewMessage(pattern='إضافة دولة', func=lambda e: e.is_private))
    client.add_event_handler(del_country_menu, events.NewMessage(pattern='حذف دولة', func=lambda e: e.is_private))
    client.add_event_handler(funding_handler, events.NewMessage(pattern='تمويل', func=lambda e: e.is_private))
    client.add_event_handler(transfer_balance, events.NewMessage(pattern='تحويل رصيد', func=lambda e: e.is_private))
    client.add_event_handler(withdraw_balance, events.NewMessage(pattern='سحب رصيد', func=lambda e: e.is_private))
    client.add_event_handler(confirm_withdraw, events.NewMessage(pattern='تأكيد السحب', func=lambda e: e.is_private))
    client.add_event_handler(cancel_operation, events.NewMessage(pattern='إلغاء العملية', func=lambda e: e.is_private))
    
    # معالجات عمليات الإنشاء
    client.add_event_handler(manual_group_creation_handler, events.NewMessage(pattern='إنشاء يدوي', func=lambda e: e.is_private))
    client.add_event_handler(auto_group_creation_handler, events.NewMessage(pattern='إنشاء تلقائي', func=lambda e: e.is_private))
    client.add_event_handler(stop_manual_creation_handler, events.NewMessage(pattern='إيقاف إنشاء يدوي', func=lambda e: e.is_private))
    client.add_event_handler(stop_auto_creation_handler, events.NewMessage(pattern='إيقاف إنشاء تلقائي', func=lambda e: e.is_private))
    
    # معالجات النشر
    client.add_event_handler(start_posting_handler, events.NewMessage(pattern='بدء النشر', func=lambda e: e.is_private))
    client.add_event_handler(stop_all_posting_handler, events.NewMessage(pattern='إيقاف كل النشرات', func=lambda e: e.is_private))
    client.add_event_handler(stop_posting_group_handler, events.NewMessage(pattern='إيقاف النشر في مجموعة', func=lambda e: e.is_private))
    client.add_event_handler(select_account_for_posting, events.NewMessage(pattern='اختيار حساب للنشر', func=lambda e: e.is_private))
    client.add_event_handler(select_channel_for_posting, events.NewMessage(pattern='اختيار قناة للنشر', func=lambda e: e.is_private))
    client.add_event_handler(ask_posting_settings, events.NewMessage(pattern='ضبط إعدادات النشر', func=lambda e: e.is_private))
    client.add_event_handler(add_posting_template_handler, events.NewMessage(pattern='إضافة قالب نشر', func=lambda e: e.is_private))
    client.add_event_handler(edit_posting_template_handler, events.NewMessage(pattern='تعديل قالب نشر', func=lambda e: e.is_private))
    client.add_event_handler(del_posting_template_handler, events.NewMessage(pattern='حذف قالب نشر', func=lambda e: e.is_private))
    
    # معالجات إدارة القنوات
    client.add_event_handler(add_force_channel_handler, events.NewMessage(pattern='إضافة قناة إجبارية', func=lambda e: e.is_private))
    client.add_event_handler(del_force_channel_handler, events.NewMessage(pattern='حذف قناة إجبارية', func=lambda e: e.is_private))
    client.add_event_handler(add_super_channel_handler, events.NewMessage(pattern='إضافة قناة سوبر', func=lambda e: e.is_private))
    client.add_event_handler(del_super_channel_handler, events.NewMessage(pattern='حذف قناة سوبر', func=lambda e: e.is_private))
    
    # معالجات VIP والإدارة
    client.add_event_handler(add_vip_user, events.NewMessage(pattern='إضافة عضو VIP', func=lambda e: e.is_private))
    client.add_event_handler(remove_vip_user, events.NewMessage(pattern='إزالة عضو VIP', func=lambda e: e.is_private))
    client.add_event_handler(add_admin, events.NewMessage(pattern='إضافة مشرف', func=lambda e: e.is_private))
    client.add_event_handler(del_admin, events.NewMessage(pattern='حذف مشرف', func=lambda e: e.is_private))
    client.add_event_handler(ban_user, events.NewMessage(pattern='حظر مستخدم', func=lambda e: e.is_private))
    client.add_event_handler(unban_user, events.NewMessage(pattern='رفع حظر مستخدم', func=lambda e: e.is_private))
    
    # معالجات إضافية
    client.add_event_handler(store_account_handler, events.NewMessage(pattern='تخزين حساب', func=lambda e: e.is_private))
    client.add_event_handler(install_session_handler, events.NewMessage(pattern='تثبيت جلسة', func=lambda e: e.is_private))
    client.add_event_handler(sell_price_handler, events.NewMessage(pattern='تحديد سعر البيع', func=lambda e: e.is_private))
    client.add_event_handler(toggle_timed_name, events.NewMessage(pattern='تفعيل/تعطيل الاسم المؤقت', func=lambda e: e.is_private))
    client.add_event_handler(update_timed_name, events.NewMessage(pattern='تحديث الاسم المؤقت', func=lambda e: e.is_private))
    client.add_event_handler(join_mandatory_channel, events.NewMessage(pattern='الانضمام للقناة الإجبارية', func=lambda e: e.is_private))
    client.add_event_handler(broadcast_start, events.NewMessage(pattern='اذاعة عامة', func=lambda e: e.is_private))
    # في نهاية register_handlers()
    client.add_event_handler(install_menu_handler, events.NewMessage(pattern='قسم التنصيب', func=lambda e: e.is_private))
    client.add_event_handler(auction_menu_handler, events.NewMessage(pattern='قسم المزاد', func=lambda e: e.is_private))
    client.add_event_handler(add_auction_handler, events.NewMessage(pattern='إضافة مزاد', func=lambda e: e.is_private))
    client.add_event_handler(auction_list_handler, events.NewMessage(pattern='قائمة المزاد', func=lambda e: e.is_private))
    client.add_event_handler(view_auction_handler, events.NewMessage(pattern='عرض مزاد', func=lambda e: e.is_private))
    client.add_event_handler(place_bid_handler, events.NewMessage(pattern='تقديم عرض', func=lambda e: e.is_private))
    client.add_event_handler(sell_auction_handler, events.NewMessage(pattern='بيع بالمزاد', func=lambda e: e.is_private))
    client.add_event_handler(auction_logout_handler, events.NewMessage(pattern='تسجيل خروج المزاد', func=lambda e: e.is_private))
    print("✅ تم تسجيل جميع معالجات الأحداث بنجاح")
