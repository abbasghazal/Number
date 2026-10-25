# Auto-split from handlers.py. Keep shared imports/state in base.py.
from .base import *
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from .base import Button, arrange_buttons


async def can_manage_licenses(user_id):
    from staff_security import has_permission
    return await has_permission(user_id, 'manage')


async def sell_price_handler(event):
    countries = await get_countries()
    text = "\n".join([f"{c[1]} ({c[0]}): {c[3]}$" for c in countries])
    await event.respond(text)


async def license_guard_handler(event):
    if await is_banned(event.chat_id):
        conversation = active_conversations.pop(event.chat_id, None)
        if conversation:
            conversation.cancel()
        await event.respond(msgs['BANNED_MESSAGE'])
        raise events.StopPropagation
    # /start must remain available so a new user can create an account and
    # reach the activation controls.  The activation/status commands are
    # likewise intentionally available without a subscription.
    text = (event.raw_text or "").strip()
    command = text.split(maxsplit=1)[0].split("@", 1)[0].lower() if text else ""
    if command != "/start":
        from mandatory_subscription import verify_required_subscriptions
        subscribed, _missing_channel = await verify_required_subscriptions(event.client, event.chat_id)
        if not subscribed:
            await event.respond("اشترك في القنوات المطلوبة أولاً، ثم أعد إرسال الأمر. أرسل /start لعرض القائمة.")
            raise events.StopPropagation
    if command in ("/start", *LICENSE_FREE_COMMANDS):
        return
    if not await enforce_license_for_message(event):
        raise events.StopPropagation


async def activate_license_handler(event):
    if not await enforce_rate_limit(event, "license_activation", 5):
        return
    user_id = event.chat_id
    parts = (event.raw_text or "").split(maxsplit=1)
    if len(parts) < 2:
        await event.respond(
            "أرسل المفتاح بعد الأمر بهذا الشكل:\n"
            "`/activate X7K9M-3P2N8-L4R6Q-V5W1E`",
            parse_mode="markdown",
            buttons=license_access_keyboard()
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


async def activate_license_callback(event):
    if not await enforce_rate_limit(event, "license_activation", 5):
        return
    user_id = event.chat_id
    await event.answer()
    await create_user(user_id)

    async with event.client.conversation(user_id) as conv:
        active_conversations[user_id] = conv
        try:
            await conv.send_message(
                "🔑 أرسل مفتاح التفعيل الآن:",
                buttons=cancel_operation_keyboard()
            )
            response = await conv.get_response(timeout=300)
            license_key = (response.raw_text or response.text or "").strip()
            success, result = await activate_license(user_id, license_key)
            if not success:
                await conv.send_message(
                    f"❌ فشل التفعيل: {result}",
                    buttons=license_access_keyboard()
                )
                return

            await conv.send_message(
                "✅ تم تفعيل اشتراكك بنجاح!\n\n"
                f"• تاريخ الانتهاء: `{result.strftime('%Y-%m-%d %H:%M')}`\n"
                f"• المدة المتبقية: {format_remaining_time(result)}",
                parse_mode="markdown"
            )
            await conv.send_message(
                await format_start_message(user_id),
                buttons=start_keyboard(user_id, await is_admin(user_id)), parse_mode='html'
            )
        except asyncio.TimeoutError:
            await conv.send_message("⏱ انتهى وقت إدخال المفتاح.", buttons=license_access_keyboard())
        finally:
            if user_id in active_conversations:
                del active_conversations[user_id]


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
            await event.respond(
                license_required_message("trial_expired", trial_status.get("expires_at")),
                parse_mode="markdown",
                buttons=license_access_keyboard()
            )
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
        await event.respond(
            license_required_message(status["reason"], expires_at),
            parse_mode="markdown",
            buttons=license_access_keyboard()
        )


async def license_status_callback(event):
    await event.answer()
    user_id = event.chat_id
    if user_id == ADMIN_ID:
        await event.respond("✅ أنت المطور الأساسي، لا تحتاج إلى ترخيص.")
        return

    status = await get_license_status(user_id)
    if status["allowed"]:
        expires_at = status.get("expires_at")
        await event.respond(
            "✅ اشتراكك نشط.\n\n"
            f"• المفتاح: `{status['license'][1]}`\n"
            f"• ينتهي في: `{expires_at.strftime('%Y-%m-%d %H:%M')}`\n"
            f"• المتبقي: {format_remaining_time(expires_at)}",
            parse_mode="markdown"
        )
        return

    trial_status = await get_free_trial_status(user_id, create_if_missing=True)
    if not status["license"] and trial_status["allowed"]:
        await event.respond(
            "🎁 أنت تستخدم الفترة المجانية الآن.\n\n"
            f"• تنتهي في: `{trial_status['expires_at'].strftime('%Y-%m-%d %H:%M')}`\n"
            f"• المتبقي: {format_remaining_time(trial_status['expires_at'])}\n\n"
            "يمكنك تفعيل مفتاح مدفوع من الزر أدناه في أي وقت.",
            parse_mode="markdown",
            buttons=license_access_keyboard()
        )
        return

    effective_status = trial_status if not status["license"] else status
    await event.respond(
        license_required_message(effective_status.get("reason"), effective_status.get("expires_at")),
        parse_mode="markdown",
        buttons=license_access_keyboard()
    )



async def generate_license_handler(event):
    user_id = event.chat_id
    if not await can_manage_licenses(user_id):
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


async def list_licenses_handler(event):
    user_id = event.chat_id
    if not await can_manage_licenses(user_id):
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


async def revoke_license_handler(event):
    user_id = event.chat_id
    if not await can_manage_licenses(user_id):
        await event.respond("❌ هذا الأمر متاح للمطور والمشرفين فقط.")
        return

    parts = (event.raw_text or "").split(maxsplit=1)
    if len(parts) < 2:
        await event.respond("أرسل المفتاح بعد الأمر:\n`/revokelicense LICENSE-KEY`", parse_mode="markdown")
        return

    revoked = await revoke_license(parts[1])
    await event.respond("✅ تم إلغاء المفتاح وتعطيل ترخيص مستخدمه." if revoked else "❌ لم يتم العثور على المفتاح.")


async def clear_expired_licenses_handler(event):
    user_id = event.chat_id
    if not await can_manage_licenses(user_id):
        await event.respond("❌ هذا الأمر متاح للمطور والمشرفين فقط.")
        return

    count = await delete_expired_licenses()
    await event.respond(f"✅ تم حذف {count} مفتاح منتهي.")


async def license_stats_handler(event):
    user_id = event.chat_id
    if not await can_manage_licenses(user_id):
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


async def export_licenses_handler(event):
    user_id = event.chat_id
    if not await can_manage_licenses(user_id):
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
    if not await can_manage_licenses(user_id):
        await event.answer("❌ هذا القسم متاح للمطور والمشرفين فقط.", alert=True)
        return

    await event.edit("🔑 قسم المفاتيح - إدارة تراخيص استخدام البوت", buttons=license_settings_keyboard())


def license_duration_selection():
    return arrange_buttons([
        Button.inline("7 أيام", data="license_duration:7"),
        Button.inline("30 يوماً", data="license_duration:30"),
        Button.inline("90 يوماً", data="license_duration:90"),
        Button.inline("365 يوماً", data="license_duration:365"),
        Button.inline("مدة مخصصة", data="license_duration:custom"),
        Button.inline("• رجوع • ↩️", data="license_menu"),
    ])


def _license_page_buttons(page, has_next):
    nav = []
    if page > 0:
        nav.append(Button.inline("◀ السابق", data=f"license_list_page:{page - 1}"))
    if has_next:
        nav.append(Button.inline("التالي ▶", data=f"license_list_page:{page + 1}"))

    buttons = []
    if nav:
        buttons.append(nav)
    buttons.append([Button.inline("• رجوع • ↩️", data="license_menu")])
    return buttons


async def license_create_with_buttons(event):
    user_id = event.chat_id
    if not await can_manage_licenses(user_id):
        await event.answer("❌ هذا القسم متاح للمطور والمشرفين فقط.", alert=True)
        return

    await event.edit("➕ اختر مدة صلاحية المفتاح:", buttons=license_duration_selection())


async def license_create_callback(event):
    await license_create_with_buttons(event)


async def license_duration_callback(event, data):
    user_id = event.chat_id
    if not await can_manage_licenses(user_id):
        await event.answer("❌ هذا القسم متاح للمطور والمشرفين فقط.", alert=True)
        return

    duration_text = data.split(":", 1)[1]
    if duration_text != "custom":
        duration_days = int(duration_text)
        license_key = await create_license(duration_days, user_id)
        await event.edit(
            "✅ تم إنشاء مفتاح جديد:\n\n"
            f"`{license_key}`\n\n"
            f"• المدة: {duration_days} يوم\n\n"
            "اضغط مطولاً على المفتاح لنسخه.",
            parse_mode="markdown",
            buttons=license_settings_keyboard()
        )
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
                f"• المدة: {duration_days} يوم\n\n"
                "اضغط مطولاً على المفتاح لنسخه.",
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
    await license_list_with_navigation(event, 0)


async def license_list_with_navigation(event, page=0):
    user_id = event.chat_id
    if not await can_manage_licenses(user_id):
        await event.answer("❌ هذا القسم متاح للمطور والمشرفين فقط.", alert=True)
        return

    per_page = 10
    rows = await list_licenses(100000)
    if not rows:
        await event.edit("لا توجد مفاتيح حتى الآن.", buttons=license_settings_keyboard())
        return

    page = max(0, int(page))
    start = page * per_page
    page_rows = rows[start:start + per_page]
    if not page_rows and page > 0:
        page = 0
        start = 0
        page_rows = rows[:per_page]

    total_pages = max(1, (len(rows) + per_page - 1) // per_page)
    lines = [f"🔑 المفاتيح - صفحة {page + 1}/{total_pages}:\n"]
    for row in page_rows:
        state = license_state_text(row)
        used_by = f" | المستخدم: {row[4]}" if row[4] else ""
        expires_at = f" | ينتهي: {row[6]}" if row[6] else ""
        lines.append(f"• `{row[0]}` | {row[1]} يوم | {state}{used_by}{expires_at}")

    text = "\n".join(lines)
    if len(text) > 3800:
        text = text[:3800] + "\n\n... تم اختصار القائمة، استخدم زر التصدير لعرض الكل."
    await event.edit(
        text,
        parse_mode="markdown",
        buttons=_license_page_buttons(page, start + per_page < len(rows))
    )


async def license_revoke_with_selection(event, page=0):
    user_id = event.chat_id
    if not await can_manage_licenses(user_id):
        await event.answer("❌ هذا القسم متاح للمطور والمشرفين فقط.", alert=True)
        return

    per_page = 10
    rows = [
        row for row in await list_licenses(100000)
        if not int(row[7] or 0)
    ]
    if not rows:
        await event.edit("لا توجد مفاتيح قابلة للإلغاء.", buttons=license_settings_keyboard())
        return

    page = max(0, int(page))
    start = page * per_page
    page_rows = rows[start:start + per_page]
    if not page_rows and page > 0:
        page = 0
        start = 0
        page_rows = rows[:per_page]

    item_buttons = []
    for row in page_rows:
        state = license_state_text(row)
        item_buttons.append(Button.inline(f"{row[0]} | {state}", data=f"license_revoke_confirm:{row[0]}"))
    buttons = arrange_buttons(item_buttons)

    nav = []
    if page > 0:
        nav.append(Button.inline("◀ السابق", data=f"license_revoke_page:{page - 1}"))
    if start + per_page < len(rows):
        nav.append(Button.inline("التالي ▶", data=f"license_revoke_page:{page + 1}"))
    if nav:
        buttons.append(nav)
    buttons.append([Button.inline("• رجوع • ↩️", data="license_menu")])
    await event.edit("🚫 اختر المفتاح الذي تريد إلغاءه:", buttons=buttons)


async def license_revoke_callback(event):
    await license_revoke_with_selection(event, 0)


async def license_revoke_confirm(event, data):
    user_id = event.chat_id
    if not await can_manage_licenses(user_id):
        await event.answer("❌ هذا القسم متاح للمطور والمشرفين فقط.", alert=True)
        return

    license_key = data.split(":", 1)[1]
    buttons = arrange_buttons([
        Button.inline("✅ تأكيد الإلغاء", data=f"license_revoke_do:{license_key}"),
        Button.inline("• رجوع • ↩️", data="license_revoke"),
    ])
    await event.edit(
        f"هل تريد إلغاء هذا المفتاح؟\n\n`{license_key}`",
        parse_mode="markdown",
        buttons=buttons
    )


async def license_revoke_do(event, data):
    user_id = event.chat_id
    if not await can_manage_licenses(user_id):
        await event.answer("❌ هذا القسم متاح للمطور والمشرفين فقط.", alert=True)
        return

    license_key = data.split(":", 1)[1]
    revoked = await revoke_license(license_key)
    await event.edit(
        "✅ تم إلغاء المفتاح وتعطيل ترخيص مستخدمه." if revoked else "❌ لم يتم العثور على المفتاح.",
        buttons=license_settings_keyboard()
    )


async def license_revoke_text_prompt(event):
    user_id = event.chat_id
    if not await can_manage_licenses(user_id):
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
    if not await can_manage_licenses(user_id):
        await event.answer("❌ هذا القسم متاح للمطور والمشرفين فقط.", alert=True)
        return

    count = await delete_expired_licenses()
    await event.edit(f"✅ تم حذف {count} مفتاح منتهي.", buttons=license_settings_keyboard())


async def license_stats_callback(event):
    user_id = event.chat_id
    if not await can_manage_licenses(user_id):
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
    if not await can_manage_licenses(user_id):
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
