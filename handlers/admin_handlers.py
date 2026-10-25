import asyncio
import datetime
import logging
from datetime import timedelta

from telethon import Button
from telethon.errors import MessageNotModifiedError

from config import ADMIN_ID


logger = logging.getLogger(__name__)
from helpers import (
    add_auto_engagement,
    add_monitored_engagement_channel,
    add_vip_user,
    adjust_balance_atomically,
    delete_auto_engagement,
    delete_monitored_engagement_channel,
    execute_query,
    extract_story_target_from_url,
    get_all_bot_service_accounts,
    get_engagement_channels,
    get_stored_accounts,
    join_super_target_with_stored_account,
    mass_story_view_and_react,
    normalize_super_target,
    set_setting,
    toggle_auto_engagement,
    verify_super_target_with_stored_account,
)
from keyboards import (
    admin_panel_keyboard,
    admin_settings_keyboard,
    auto_engagement_settings_keyboard,
    ban_settings_keyboard,
    broadcast_target_keyboard,
    buy_sell_settings_keyboard,
    cancel_operation_keyboard,
    engagement_period_keyboard,
    num_settings_keyboard,
)
from messages import msgs

from .base import (
    active_conversations,
    ensure_admin,
    ensure_developer,
    ensure_owner,
    logger,
    parse_positive_amount,
)


async def num_settings(event):
    if not await ensure_admin(event):
        return
    await event.edit(
        msgs['ADMIN_NUM_SETTINGS'],
        buttons=num_settings_keyboard()
    )

async def auto_engagement_menu(event):
    if not await ensure_owner(event):
        return
    await event.edit("🔄 **إدارة الرشق التلقائي**\n\nاختر الإجراء:", buttons=auto_engagement_settings_keyboard())


async def funding_auto_global_menu(event):
    """Developer-only master control for the automatic funding workers."""
    if not await ensure_developer(event):
        return
    import funding_auto_collect as auto_collect
    enabled, updated_by, updated_at = await auto_collect.global_status()
    text = (
        "إدارة التجميع التلقائي للتمويل\n\n"
        f"الحالة العامة: {'مفعّل' if enabled else 'متوقف'}\n"
        "يشغّله المطور مركزيًا باستخدام حسابات المطور، ومخزون البوت المعروض للبيع، "
        "والحسابات المعروضة للبيع أو المزاد. تُزامن القائمة قبل كل دورة.\n"
        "لا يستطيع المستخدمون إضافة حساباتهم أو تشغيل المهمة من قوائمهم.\n"
        f"آخر تحديث: {updated_at or '—'}"
    )
    await event.edit(text, parse_mode=None, buttons=[
        [Button.inline('تشغيل التجميع الدوري', data='fund_auto_global_on')],
        [Button.inline('إيقاف التجميع الدوري', data='fund_auto_global_off')],
        [Button.inline('رجوع', data='admin_panel')],
    ])


async def funding_auto_global_toggle(event, enabled):
    if not await ensure_developer(event):
        return
    import funding_auto_collect as auto_collect
    await auto_collect.configure_global(event.sender_id or event.chat_id, bool(enabled))
    await event.answer('تم تحديث حالة التجميع التلقائي.', alert=True)
    await funding_auto_global_menu(event)


async def fund_channel_handler(event):
    """Join an exact number of globally managed accounts to one channel/super."""
    if not await ensure_owner(event):
        return
    async with event.client.conversation(event.chat_id) as conv:
        active_conversations[event.chat_id] = conv
        try:
            await conv.send_message(
                "أرسل رابط أو معرف القناة/السوبر المراد تمويله:",
                buttons=cancel_operation_keyboard(),
            )
            target_response = await conv.get_response(timeout=300)
            target = normalize_super_target(target_response.text)
            if not target:
                await conv.send_message("الرابط أو المعرف غير صالح.")
                return

            accounts = await get_all_bot_service_accounts(event.chat_id)
            available_count = len(accounts)
            if available_count == 0:
                await conv.send_message("لا توجد جلسات حسابات متاحة داخل البوت.")
                return
            await conv.send_message(
                f"المتاح {available_count} حساباً دون تكرار. أرسل العدد المراد استخدامه:",
                buttons=cancel_operation_keyboard(),
            )
            count_response = await conv.get_response(timeout=300)
            count = int(count_response.text.strip())
            if count < 1 or count > available_count:
                await conv.send_message(f"أرسل عدداً من 1 إلى {available_count}.")
                return

            selected = accounts[:count]
            await conv.send_message(f"بدأ تمويل القناة باستخدام {count} حساباً...")
            joined = 0
            failed = []
            for phone, session, source in selected:
                if not await service_account_authorized(event.chat_id,(phone,session)):
                    failed.append(phone)
                    continue
                try:
                    await join_super_target_with_stored_account(session, target)
                    joined += 1
                except Exception as exc:
                    logger.warning(
                        "Funding join failed for account %s from %s: %s",
                        phone, source, exc,
                    )
                    failed.append(phone)

            result = [
                f"انتهى التمويل: نجح {joined} من أصل {count} حساباً.",
                f"فشل {len(failed)} حساباً.",
            ]
            if failed:
                shown = ", ".join(f"+{phone}" for phone in failed[:30])
                suffix = " ..." if len(failed) > 30 else ""
                result.append(f"الحسابات التي فشلت: {shown}{suffix}")
            await conv.send_message("\n".join(result))
        except ValueError:
            await conv.send_message("أرسل عدداً صحيحاً فقط.")
        except asyncio.TimeoutError:
            await conv.send_message("انتهى وقت إدخال البيانات.")
        finally:
            active_conversations.pop(event.chat_id, None)

async def add_engagement_channel(event):
    if not await ensure_owner(event):
        return
    async with event.client.conversation(event.chat_id) as conv:
        active_conversations[event.chat_id] = conv
        try:
            await conv.send_message(
                "أرسل رابط القناة أو معرفها لإضافتها للمراقبة:",
                buttons=cancel_operation_keyboard(),
            )
            channel_input = await conv.get_response(timeout=300)
            channel_id = normalize_super_target(channel_input.text)
            stored = await get_stored_accounts(event.chat_id)
            if not stored:
                await conv.send_message("❌ لا توجد حسابات مخزنة للتحقق من القناة.")
                return
            entity = await verify_super_target_with_stored_account(stored[0][1], channel_id)
            if not entity:
                await conv.send_message("❌ تعذر الوصول إلى القناة. تأكد من الرابط وصلاحية الحساب.")
                return
            channel_name = getattr(entity, "title", None) or getattr(entity, "username", None) or channel_id
            await add_monitored_engagement_channel(channel_id, channel_name, event.chat_id)
            await conv.send_message(f"✅ تمت إضافة قناة المراقبة: **{channel_name}**", parse_mode="markdown")
        except asyncio.TimeoutError:
            await conv.send_message("⏱ انتهى الوقت المحدد للإدخال!")
        except Exception as exc:
            await conv.send_message(f"❌ تعذر إضافة القناة: {exc}")
        finally:
            active_conversations.pop(event.chat_id, None)


async def choose_engagement_service(event):
    if not await ensure_owner(event):
        return
    await engagement_type_selected(event, "engagement_type:views_react")


async def auto_story_burst_handler(event):
    """Developer-only story campaign using the global bot account pool."""
    if not await ensure_developer(event):
        return
    accounts = await get_all_bot_service_accounts(event.chat_id)
    if not accounts:
        await event.answer("❌ لا توجد حسابات في مخزون المطور.", alert=True)
        return
    async with event.client.conversation(event.chat_id) as conv:
        active_conversations[event.chat_id] = conv
        try:
            await conv.send_message("أرسل رابط القصة (مثال: https://t.me/username/s/123):", buttons=cancel_operation_keyboard())
            story_url = (await conv.get_response(timeout=300)).text.strip()
            if not all(extract_story_target_from_url(story_url)):
                await conv.send_message("❌ رابط القصة غير صالح أو غير عام.")
                return
            await conv.send_message("أرسل إيموجي التفاعل المطلوب (مثال: ❤️):", buttons=cancel_operation_keyboard())
            emoji = (await conv.get_response(timeout=300)).text.strip()
            await conv.send_message(
                f"المتاح في مخزون المطور: {len(accounts)} حساباً. أرسل العدد المطلوب:",
                buttons=cancel_operation_keyboard(),
            )
            count = int((await conv.get_response(timeout=300)).text.strip())
            if count < 1 or count > len(accounts):
                await conv.send_message(f"❌ أرسل عدداً من 1 إلى {len(accounts)}.")
                return
            await conv.send_message("⏳ جارٍ تنفيذ مشاهدة القصة والتفاعل عليها من مخزون المطور…")
            views, reactions = await mass_story_view_and_react(accounts, story_url, emoji, count,user_id=event.chat_id)
            await conv.send_message(f"✅ تمت المشاهدة من {views} حساب والتفاعل من {reactions} حساب.")
        except (ValueError, asyncio.TimeoutError):
            await conv.send_message("❌ انتهى الوقت أو أُرسلت قيمة غير صحيحة.")
        finally:
            active_conversations.pop(event.chat_id, None)

async def engagement_type_selected(event, data):
    if not await ensure_owner(event):
        return
    engagement_type = data.split(':')[1]
    channels = await get_engagement_channels()
    if not channels:
        await event.answer("أضف قناة للمراقبة أولاً.", alert=True)
        await auto_engagement_menu(event)
        return
    buttons = [[Button.inline("• جميع القنوات • 🌐", data=f"engagement_target_all:{engagement_type}")]]
    for channel_id, target, name in channels:
        label = (name or target)[:22]
        buttons.append([
            Button.inline(f"• {label} •", data=f"engagement_target:{engagement_type}:{channel_id}"),
            Button.inline("حذف", data=f"delete_engagement_channel:{channel_id}"),
        ])
    buttons.append([Button.inline("• رجوع • ↩️", data="choose_engagement_service")])
    try:
        await event.edit("اختر القناة التي تُطبّق عليها الخدمة:", buttons=buttons)
    except MessageNotModifiedError:
        # الضغط المتكرر يعيد نفس النص والأزرار؛ لا يعد ذلك فشلاً.
        await event.answer("القائمة معروضة بالفعل.")


async def delete_engagement_channel(event, data):
    if not await ensure_owner(event):
        return
    try:
        channel_row_id = int(data.split(":", 1)[1])
    except (ValueError, IndexError):
        await event.answer("بيانات القناة غير صالحة.", alert=True)
        return
    await delete_monitored_engagement_channel(channel_row_id)
    await event.answer("تم حذف القناة من قائمة المراقبة.")
    await choose_engagement_service(event)


async def engagement_channel_selected(event, data):
    if not await ensure_owner(event):
        return
    parts = data.split(':')
    all_channels = parts[0] == "engagement_target_all"
    engagement_type = parts[1]
    if engagement_type != "views_react":
        return await event.answer("هذه الخدمة لم تعد متاحة.", alert=True)
    selected_id = 0 if all_channels else int(parts[2])
    await event.edit(
        "اختر مدة مراقبة القناة من لحظة التفعيل:",
        buttons=engagement_period_keyboard("all" if all_channels else "one", selected_id),
    )


async def engagement_period_selected(event, data):
    if not await ensure_owner(event):
        return
    _, scope, selected_id, period = data.split(":", 3)
    durations = {
        "daily": (timedelta(days=1), "يوم واحد"),
        "weekly": (timedelta(weeks=1), "أسبوع واحد"),
        "monthly": (timedelta(days=30), "شهر واحد"),
        "yearly": (timedelta(days=365), "سنة واحدة"),
    }
    if scope not in {"all", "one"} or period not in durations:
        return await event.answer("مدة المراقبة غير صحيحة.", alert=True)
    channels = await get_engagement_channels()
    if scope == "one":
        channels = [channel for channel in channels if channel[0] == int(selected_id)]
    if not channels:
        return await event.answer("القناة المحددة لم تعد موجودة.", alert=True)
    accounts = await get_all_bot_service_accounts(event.chat_id)
    available_count = len(accounts or [])
    if available_count == 0:
        return await event.answer("لا توجد لديك حسابات مخزنة لتنفيذ العملية.", alert=True)

    async with event.client.conversation(event.chat_id) as conv:
        active_conversations[event.chat_id] = conv
        try:
            await conv.send_message(
                f"يتوفر في مخزون البوت العام {available_count} حساباً صالحاً دون تكرار. أرسل عدد الحسابات التي تنفذ العملية:",
                buttons=cancel_operation_keyboard(),
            )
            response = await conv.get_response(timeout=300)
            account_count = int(response.text.strip())
            if account_count < 1 or account_count > available_count:
                await conv.send_message(f"أرسل عدداً من 1 إلى {available_count}.")
                return
        except ValueError:
            await conv.send_message("الرجاء إرسال عدد صحيح فقط.")
            return
        except asyncio.TimeoutError:
            await conv.send_message("انتهى وقت إدخال العدد.")
            return
        finally:
            active_conversations.pop(event.chat_id, None)

    duration, duration_label = durations[period]
    expires_at = datetime.datetime.now() + duration
    for _, channel_id, _ in channels:
        await add_auto_engagement(
            channel_id,
            "views_react",
            created_by=event.chat_id,
            period=period,
            expires_at=expires_at,
            account_count=account_count,
        )
    target_label = "جميع القنوات" if scope == "all" else (channels[0][2] or channels[0][1])
    await event.edit(
        f"تم تفعيل المشاهدة والتفاعل التلقائي لـ {target_label} لمدة {duration_label} باستخدام {account_count} حساباً من مخزون البوت العام.",
        buttons=auto_engagement_settings_keyboard(),
    )

async def list_engagement_channels(event):
    if not await ensure_owner(event):
        return
    rows = await execute_query("SELECT * FROM auto_engagement ORDER BY id", fetchall=True)
    if not rows:
        await event.edit("لا توجد قنوات مضافّة للرشق التلقائي.", buttons=auto_engagement_settings_keyboard())
        return
    text = "📋 **قنوات الرشق التلقائي:**\n\n"
    buttons = []
    for row in rows:
        id, channel, eng_type, emoji, vote_opt, comment, active, _, _, last_msg, period, expires_at, account_count = row
        expired = bool(expires_at and expires_at <= datetime.datetime.now())
        status = "منتهي" if expired else ("مفعل" if active else "معطل")
        type_label = {'views_react':'مشاهدة وتفاعل', 'react':'تفاعل', 'vote':'تصويت', 'comment':'تعليق', 'views':'مشاهدات'}.get(eng_type, eng_type)
        period_label = {'daily':'يومي', 'weekly':'أسبوعي', 'monthly':'شهري', 'yearly':'سنوي'}.get(period, period or "قديم")
        channel_name = str(channel)
        try:
            entity = await event.client.get_entity(normalize_super_target(channel))
            channel_name = getattr(entity, "title", None) or getattr(entity, "username", None) or channel_name
        except Exception:
            # نعرض المعرّف المحفوظ إن تعذر على البوت الوصول للقناة حالياً.
            pass

        count_label = account_count if account_count is not None else "كل حسابات المالك"
        text += f"• {channel_name} — {type_label} — {period_label} — {count_label} حساب — {status}\n"
        button_name = channel_name[:18]
        row_buttons = []
        if not expired:
            row_buttons.append(
                Button.inline(f"{'إيقاف' if active else 'تفعيل'} {button_name}", data=f"toggle_engagement:{id}")
            )
        row_buttons.append(Button.inline(f"حذف {button_name}", data=f"delete_engagement:{id}"))
        buttons.append(row_buttons)
    buttons.append([Button.inline("• رجوع • ↩️", data="auto_engagement_menu")])
    await event.edit(text, buttons=buttons)

async def toggle_engagement(event, data):
    if not await ensure_owner(event):
        return
    id = int(data.split(':')[1])
    row = await execute_query("SELECT is_active FROM auto_engagement WHERE id=?", (id,), fetchone=True)
    if not row:
        return await event.answer("الإعداد غير موجود!", alert=True)
    new_state = not row[0]
    await toggle_auto_engagement(id, new_state)
    await event.answer(f"✅ تم {'تفعيل' if new_state else 'إيقاف'} المهمة.", alert=True)
    await list_engagement_channels(event)

async def delete_engagement(event, data):
    if not await ensure_owner(event):
        return
    id = int(data.split(':')[1])
    await delete_auto_engagement(id)
    await event.answer("✅ تم الحذف.", alert=True)
    await list_engagement_channels(event)
async def admin_settings(event):
    if not await ensure_admin(event):
        return
    await event.edit(
        msgs['ADMIN_ADMIN_SETTINGS'],
        buttons=admin_settings_keyboard()
    )


async def buy_sell_settings(event):
    if not await ensure_admin(event):
        return
    await event.edit(
        msgs['ADMIN_BUY_SELL_SETTINGS'],
        buttons=buy_sell_settings_keyboard()
    )


async def ban_settings(event):
    if not await ensure_admin(event):
        return
    await event.edit(
        msgs['ADMIN_BAN_SETTINGS'],
        buttons=ban_settings_keyboard()
    )


async def admin_panel(event):
    if not await ensure_admin(event):
        return
    from staff_security import filtered_panel
    await event.edit(msgs['ADMIN_MESSAGE'], buttons=await filtered_panel(admin_panel_keyboard(), event.sender_id))


async def _count_query(query, args=()):
    row = await execute_query(query, args, fetchone=True)
    return row[0] if row else 0


def _format_admin_number(value):
    value = float(value or 0)
    return str(int(value)) if value.is_integer() else f"{value:.2f}"


async def admin_bot_stats(event):
    if not await ensure_admin(event):
        return
    total_users = await _count_query("SELECT COUNT(*) FROM users")
    banned_users = await _count_query("SELECT COUNT(*) FROM bad_guys")
    admins_count = await _count_query("SELECT COUNT(*) FROM admins")
    vip_count = await _count_query("SELECT COUNT(*) FROM vip_users")
    active_licenses = await _count_query(
        "SELECT COUNT(*) FROM user_licenses WHERE is_valid=1 AND expires_at>CURRENT_TIMESTAMP"
    )
    expired_licenses = await _count_query(
        "SELECT COUNT(*) FROM user_licenses WHERE is_valid=0 OR expires_at<=CURRENT_TIMESTAMP"
    )

    available_numbers = await _count_query("SELECT COUNT(*) FROM accounts WHERE is_sold=0 AND is_active=1")
    sold_numbers = await _count_query("SELECT COUNT(*) FROM sold_accounts")
    stored_accounts_count = await _count_query("SELECT COUNT(*) FROM stored_accounts")
    purchased_accounts_count = await _count_query("SELECT COUNT(*) FROM purchased_accounts")
    super_channels_count = await _count_query("SELECT COUNT(*) FROM super_channels")
    force_channels_count = await _count_query("SELECT COUNT(*) FROM force_channels")
    posting_tasks = await _count_query("SELECT COUNT(*) FROM active_posting_tasks WHERE active=1")
    creation_tasks = await _count_query("SELECT COUNT(*) FROM auto_creation_tasks WHERE active=1")
    auctions_active = await _count_query("SELECT COUNT(*) FROM auctions WHERE status='active'")

    text = (
        "📊 **إحصائيات البوت**\n\n"
        "👥 **المستخدمون والتراخيص**\n"
        f"• إجمالي المستخدمين: `{total_users}`\n"
        f"• المحظورون: `{banned_users}`\n"
        f"• المشرفون: `{admins_count}`\n"
        f"• المميزون: `{vip_count}`\n"
        f"• التراخيص الفعالة: `{active_licenses}`\n"
        f"• التراخيص المنتهية/غير الصالحة: `{expired_licenses}`\n\n"
        "📱 **الأرقام والحسابات**\n"
        f"• أرقام متاحة للبيع: `{available_numbers}`\n"
        f"• أرقام مباعة: `{sold_numbers}`\n"
        f"• حسابات مخزنة: `{stored_accounts_count}`\n"
        f"• حسابات مشتراة: `{purchased_accounts_count}`\n\n"
        "🚀 **القنوات والمجموعات والخدمات**\n"
        f"• السوبرات المضافة: `{super_channels_count}`\n"
        f"• قنوات الاشتراك الإجباري: `{force_channels_count}`\n"
        f"• مهام نشر نشطة: `{posting_tasks}`\n"
        f"• مهام إنشاء نشطة: `{creation_tasks}`\n"
        f"• مزادات نشطة: `{auctions_active}`"
    )
    await event.edit(
        text,
        buttons=[[Button.inline("• تحديث • 🔄", data="admin_bot_stats")], [Button.inline("• رجوع • ↩️", data="admin_panel")]],
        parse_mode="markdown"
    )


async def admin_operations(event):
    if not await ensure_admin(event):
        return
    from operations import get_operational_stats
    stats = await get_operational_stats()
    size_mb = stats["database_size"] / (1024 * 1024)
    await event.edit(
        "🩺 **حالة التشغيل**\n\n"
        f"• المستخدمون النشطون اليوم: `{stats['active_today']}/{stats['users']}`\n"
        f"• تذاكر الدعم المفتوحة: `{stats['open_tickets']}`\n"
        f"• طلبات السحب المعلقة: `{stats['withdrawals']}`\n"
        f"• المزادات النشطة: `{stats['active_auctions']}`\n"
        f"• مهام النشر النشطة: `{stats['posting_tasks']}`\n"
        f"• حجم قاعدة البيانات: `{size_mb:.2f} MB`",
        parse_mode="markdown",
        buttons=[[Button.inline("• تحديث • 🔄", data="admin_operations")], [Button.inline("• رجوع • ↩️", data="admin_panel")]],
    )


async def add_vip_callback(event):
    if not await ensure_admin(event):
        return
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


async def add_admin(event):
    if not await ensure_owner(event):
        return
    async with event.client.conversation(event.chat_id) as conv:
        active_conversations[event.chat_id] = conv
        try:
            await conv.send_message("أرسل ايدي العضو:", buttons=cancel_operation_keyboard())
            user_id = await conv.get_response(timeout=300)
            try:
                from staff_security import set_role
                await set_role(event.sender_id,int(user_id.text),'administrator')
                await conv.send_message("✅ تم رفع العضو إلى ادمن بنجاح!")
            except ValueError:
                await conv.send_message("❌ ايدي غير صالح!")
        except asyncio.TimeoutError:
            await conv.send_message("⏱ انتهى الوقت المحدد للإدخال!")
        finally:
            if event.chat_id in active_conversations:
                del active_conversations[event.chat_id]


async def del_admin(event):
    if not await ensure_owner(event):
        return
    async with event.client.conversation(event.chat_id) as conv:
        active_conversations[event.chat_id] = conv
        try:
            await conv.send_message("أرسل ايدي العضو:", buttons=cancel_operation_keyboard())
            user_id = await conv.get_response(timeout=300)
            try:
                from staff_security import remove_role
                await remove_role(event.sender_id,int(user_id.text))
                await conv.send_message("✅ تم حذف العضو من الادمنية بنجاح!")
            except ValueError:
                await conv.send_message("❌ ايدي غير صالح!")
        except asyncio.TimeoutError:
            await conv.send_message("⏱ انتهى الوقت المحدد للإدخال!")
        finally:
            if event.chat_id in active_conversations:
                del active_conversations[event.chat_id]


async def add_coins(event):
    if not await ensure_admin(event):
        return
    async with event.client.conversation(event.chat_id) as conv:
        active_conversations[event.chat_id] = conv
        try:
            await conv.send_message("أرسل ايدي العضو:", buttons=cancel_operation_keyboard())
            user_id = await conv.get_response(timeout=300)
            await conv.send_message("أرسل المبلغ:", buttons=cancel_operation_keyboard())
            amount = await conv.get_response(timeout=300)
            
            try:
                success, _ = await adjust_balance_atomically(
                    int(user_id.text), parse_positive_amount(amount.text), "admin_credit", event.chat_id
                )
                if not success:
                    raise ValueError
                await conv.send_message(f"✅ تم إضافة {amount.text}$ للعضو {user_id.text}!")
            except ValueError:
                await conv.send_message("❌ قيم غير صالحة!")
        except asyncio.TimeoutError:
            await conv.send_message("⏱ انتهى الوقت المحدد للإدخال!")
        finally:
            if event.chat_id in active_conversations:
                del active_conversations[event.chat_id]


async def del_coins(event):
    if not await ensure_admin(event):
        return
    async with event.client.conversation(event.chat_id) as conv:
        active_conversations[event.chat_id] = conv
        try:
            await conv.send_message("أرسل ايدي العضو:", buttons=cancel_operation_keyboard())
            user_id = await conv.get_response(timeout=300)
            await conv.send_message("أرسل المبلغ:", buttons=cancel_operation_keyboard())
            amount = await conv.get_response(timeout=300)
            
            try:
                success, _ = await adjust_balance_atomically(
                    int(user_id.text), -parse_positive_amount(amount.text), "admin_debit", event.chat_id
                )
                if not success:
                    await conv.send_message("❌ الرصيد غير كافٍ أو العضو غير موجود.")
                    return
                await conv.send_message(f"✅ تم خصم {amount.text}$ من العضو {user_id.text}!")
            except ValueError:
                await conv.send_message("❌ قيم غير صالحة!")
        except asyncio.TimeoutError:
            await conv.send_message("⏱ انتهى الوقت المحدد للإدخال!")
        finally:
            if event.chat_id in active_conversations:
                del active_conversations[event.chat_id]


async def ban_user(event):
    if not await ensure_admin(event):
        return
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
            except ValueError:
                await conv.send_message("❌ ايدي غير صالح!")
        except asyncio.TimeoutError:
            await conv.send_message("⏱ انتهى الوقت المحدد للإدخال!")
        finally:
            if event.chat_id in active_conversations:
                del active_conversations[event.chat_id]


async def unban_user(event):
    if not await ensure_admin(event):
        return
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
            except ValueError:
                await conv.send_message("❌ ايدي غير صالح!")
        except asyncio.TimeoutError:
            await conv.send_message("⏱ انتهى الوقت المحدد للإدخال!")
        finally:
            if event.chat_id in active_conversations:
                del active_conversations[event.chat_id]


async def zip_database(event):
    if not await ensure_owner(event):
        return
    try:
        await event.answer("⏳ جارٍ إنشاء النسخة الاحتياطية وإرسالها...", alert=True)
        from operations import create_and_send_database_backup
        await create_and_send_database_backup(event.client, ADMIN_ID)
        today = datetime.datetime.now().date().isoformat()
        await set_setting("daily_backup_enabled", "1")
        await set_setting("last_daily_backup_date", today)
        await event.respond(
            "✅ تم إرسال حزمة الاستعادة للمطور وتفعيل النسخ التلقائي اليومي. "
            "تتضمن الحزمة قاعدة البيانات وتعليمات الاستعادة؛ مفتاح التشفير يبقى خارج النسخة الاحتياطية."
        )
    except Exception:
        logger.exception("Manual database backup failed")
        await event.respond("❌ تعذر إنشاء النسخة. تحقق من إعداد PG_DUMP_PATH ومساحة التخزين.")


async def all_numbers_count(event):
    if not await ensure_admin(event):
        return
    result = await execute_query("SELECT COUNT(*) FROM accounts", (), fetchone=True)
    count = result[0] if result else 0
    await event.answer(f"عدد ارقام البوت: {count}", alert=True)


async def broadcast_start(event):
    if not await ensure_admin(event, 'manage'):
        await event.answer("❌ ليس لديك صلاحية!", alert=True)
        return

    await event.respond(
        "📢 اختر وجهة الإذاعة:",
        buttons=broadcast_target_keyboard(),
    )


async def broadcast_target_selected(event, data):
    """يبث النص إلى المستخدمين و/أو كل المجموعات والقنوات المتاحة للبوت."""
    user_id = event.chat_id
    if not await ensure_admin(event, 'manage'):
        await event.answer("❌ ليس لديك صلاحية!", alert=True)
        return

    target = data.split(":", 1)[1]
    labels = {
        "users": "كافة المستخدمين",
        "chats": "المجموعات والقنوات",
        "all": "الجميع",
    }
    if target not in labels:
        await event.answer("وجهة إذاعة غير صالحة.", alert=True)
        return

    async with event.client.conversation(user_id) as conv:
        active_conversations[user_id] = conv
        try:
            await conv.send_message(
                f"✉️ أرسل الرسالة التي تريد بثها إلى {labels[target]}:",
                buttons=cancel_operation_keyboard(),
            )
            msg = await conv.get_response(timeout=300)
            text = (msg.text or "").strip()
            if not text:
                await conv.send_message("❌ الإذاعة النصية تحتاج إلى رسالة غير فارغة.")
                return

            destinations = []
            if target in {"users", "all"}:
                users = await execute_query("SELECT user_id FROM users", fetchall=True) or []
                destinations.extend(user[0] for user in users)

            if target in {"chats", "all"}:
                # Telegram bots cannot use GetDialogsRequest.  The registry
                # contains groups/channels that have already sent the bot an
                # update and can therefore be addressed safely.
                from broadcast_registry import destinations as broadcast_destinations
                destinations.extend(await broadcast_destinations())

            # قد تتكرر الوجهة، خصوصاً عند ظهور نفس القناة في أكثر من حوار.
            unique_destinations = []
            seen = set()
            for destination in destinations:
                if isinstance(destination, int):
                    key = ("user", destination)
                else:
                    key = (
                        type(destination).__name__,
                        getattr(destination, "channel_id", getattr(destination, "chat_id", getattr(destination, "id", None))),
                    )
                if key not in seen:
                    seen.add(key)
                    unique_destinations.append(destination)

            if not unique_destinations:
                await conv.send_message(
                    "ℹ️ لا توجد جهات مسجّلة للإذاعة بعد. أرسل رسالة في المجموعة، "
                    "أو أضف البوت مشرفًا في القناة ثم انشر رسالة فيها، وبعدها أعد المحاولة."
                )
                return

            from outbound_queue import queue_broadcast
            report = await queue_broadcast(unique_destinations, text)

            await conv.send_message(
                f"✅ تم إرسال الإذاعة إلى {labels[target]}!\n\n"
                f"تم الإرسال إلى: {report.success} ✅\nفشل الإرسال إلى: {report.failed} ❌"
            )
        except asyncio.TimeoutError:
            await conv.send_message("⏱ انتهى الوقت، لم يتم إرسال الإذاعة.")
        except Exception:
            # Keep broadcast failures inside the conversation instead of
            # letting a secondary handler error turn them into a generic
            # callback-popup failure.
            logger.exception("Broadcast failed for target %s", target)
            await conv.send_message("❌ تعذر إرسال الإذاعة الآن. تحقق من اتصال البوت وحاول مجددًا.")
        finally:
            if user_id in active_conversations:
                del active_conversations[user_id]
