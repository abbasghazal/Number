# Auto-split from handlers.py. Keep shared imports/state in base.py.
from .base import *


async def posting_settings_menu(event):
    await event.edit(msgs['POSTING_MENU'], buttons=posting_settings_keyboard())


async def add_posting_template_handler(event):
    async with event.client.conversation(event.chat_id) as conv:
        active_conversations[event.chat_id] = conv
        try:
            await conv.send_message("أرسل كليشة النشر الجديدة:", buttons=cancel_operation_keyboard())
            template = await conv.get_response(timeout=300)
            await add_posting_template(event.chat_id, template.text)
            await conv.send_message("✅ تم إضافة الكليشة بنجاح!")
        except asyncio.TimeoutError:
            await conv.send_message("⏱ انتهى الوقت المحدد للإدخال!")
        finally:
            if event.chat_id in active_conversations:
                del active_conversations[event.chat_id]


async def del_posting_template_handler(event):
    templates = await get_posting_templates(event.chat_id)
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
    templates = await get_posting_templates(event.chat_id)
    if not templates:
        return await event.answer("لا توجد كلايش مضافّة!", alert=True)
    
    text = "📝 الكلايش المضافّة:\n\n"
    for i, template in enumerate(templates, 1):
        text += f"{i}. {template[1]}\n\n"
    
    await event.answer(text, alert=True)


async def edit_posting_template_handler(event):
    templates = await get_posting_templates(event.chat_id)
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
    templates = await get_posting_templates(event.chat_id)
    if not templates:
        await event.answer("لا توجد كلايش مضافّة!", alert=True)
        return

    await execute_query("DELETE FROM user_posting_templates WHERE user_id=?", (event.chat_id,), commit=True)
    await event.answer("✅ تم حذف جميع الكلايش بنجاح!", alert=True)
    await posting_settings_menu(event)


async def delete_posting_template_callback(event, data):
    try:
        template_id = int(data.split(":", 1)[1])
    except (ValueError, IndexError):
        await event.answer("❌ بيانات الكليشة غير صالحة!", alert=True)
        return

    await delete_posting_template(event.chat_id, template_id)
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

            await update_posting_template(event.chat_id, template_id, new_template.text.strip())
            await conv.send_message("✅ تم تعديل الكليشة بنجاح!")
        except asyncio.TimeoutError:
            await conv.send_message("⏱ انتهى الوقت المحدد للإدخال!")
        finally:
            if event.chat_id in active_conversations:
                del active_conversations[event.chat_id]


async def set_posting_setting(event, key, value):
    # Settings here are per-user; global system settings remain administrator-only.
    await set_setting(f"posting:{event.chat_id}:{key}", value)
    await event.answer("✅ تم تحديث الإعداد بنجاح!", alert=True)


async def start_posting_handler(event):
    user_id = event.chat_id
    
    # التحقق من وجود حسابات مخزنة للمستخدم
    stored_accounts = await filter_usable_accounts(await get_stored_accounts(user_id))
    if not stored_accounts:
        await event.answer("❌ لا توجد حسابات مخزنة لاستخدامها في النشر!", alert=True)
        return
    
    # عرض قائمة الحسابات المخزنة للاختيار
    item_buttons = []
    for account in stored_accounts:
        phone = account[0]
        item_buttons.append(Button.inline(f"+{phone}", data=f"select_acc_for_posting:{phone}"))
    if len(stored_accounts) > 1:
        item_buttons.append(Button.inline("• جميع الحسابات • 👥", data="select_acc_for_posting:all"))
    buttons = arrange_buttons(item_buttons)
    
    buttons.append(cancel_operation_keyboard()[0])
    
    await event.edit("📱 اختر الحساب الذي تريد استخدامه للنشر:", buttons=buttons)


async def select_account_for_posting(event, data):
    phone = data.split(':')[1]
    user_id = event.chat_id
    
    # تخزين الحساب المختار في الذاكرة
    if phone == "all":
        accounts = await filter_usable_accounts(await get_stored_accounts(user_id))
        user_posting_accounts[user_id] = [account[0] for account in accounts]
    else:
        account = await get_owned_stored_account(user_id, phone)
        if not account or not await session_is_usable(account[1]):
            await event.answer("❌ الحساب المحدد لم يعد متاحاً.", alert=True)
            return
        user_posting_accounts[user_id] = phone
    user_posting_modes.pop(user_id, None)
    user_posting_targets.pop(user_id, None)
    
    await show_posting_mode_options(event)


async def show_posting_mode_options(event):
    user_id = event.chat_id
    phone = user_posting_accounts.get(user_id)
    
    if not phone:
        await event.answer("❌ لم يتم تحديد حساب للنشر!", alert=True)
        return
    
    # التحقق من وجود قوالب نشر
    templates = await get_posting_templates(user_id)
    if not templates:
        await event.answer("❌ لا توجد قوالب نشر مضافّة!", alert=True)
        return

    buttons = arrange_buttons([
        Button.inline("📋 نشر جميع الكلايش", data="posting_mode_all"),
        Button.inline("📝 نشر كليشة محددة", data="posting_mode_single"),
        Button.inline("• إلغاء العملية • ❌", data="cancel_operation")
    ])

    await event.edit(
        f"📝 اختر نوع الكليشة للحساب: {'جميع الحسابات' if isinstance(phone, list) else '+' + str(phone)}",
        buttons=buttons
    )


async def ask_posting_settings(event):
    user_id = event.chat_id
    phone = user_posting_accounts.get(user_id)
    
    if not phone:
        await event.answer("❌ لم يتم تحديد حساب للنشر!", alert=True)
        return
    if user_id not in user_posting_modes:
        await show_posting_mode_options(event)
        return
    
    # التحقق من وجود قنوات سوبر
    super_channels = await get_super_channels(user_id)
    if not super_channels:
        await event.answer("❌ لا توجد قنوات سوبر مضافّة!", alert=True)
        return
    
    buttons = arrange_buttons([
        Button.inline("النشر في جميع القنوات", data="posting_all"),
        Button.inline("النشر في قناة محددة", data="posting_specific"),
        Button.inline("• إلغاء العملية • ❌", data="cancel_operation")
    ])
    
    await event.edit(
        f"🔧 إعدادات النشر للحساب: {'جميع الحسابات' if isinstance(phone, list) else '+' + str(phone)}\n\n"
        "اختر طريقة النشر:",
        buttons=buttons
    )


async def handle_all_templates_posting(event):
    user_posting_modes[event.chat_id] = {"mode": "all_templates", "template_id": None}
    await ask_posting_settings(event)


async def handle_single_template_selection(event):
    templates = await get_posting_templates(event.chat_id)
    if not templates:
        await event.answer("❌ لا توجد قوالب نشر مضافّة!", alert=True)
        return

    item_buttons = []
    for template in templates:
        preview = template[1].replace("\n", " ")
        preview = preview[:35] + "..." if len(preview) > 35 else preview
        item_buttons.append(Button.inline(preview, data=f"posting_template:{template[0]}"))
    buttons = arrange_buttons(item_buttons)
    buttons.append([Button.inline("• رجوع • ↩️", data="start_posting")])
    buttons.append(cancel_operation_keyboard()[0])
    await event.edit("📝 اختر الكليشة التي تريد النشر بها:", buttons=buttons)


async def handle_single_template_posting(event, data):
    try:
        template_id = int(data.split(":", 1)[1])
    except (ValueError, IndexError):
        await event.answer("❌ بيانات الكليشة غير صالحة!", alert=True)
        return

    templates = await get_posting_templates(event.chat_id)
    if not any(int(template[0]) == template_id for template in templates):
        await event.answer("❌ الكليشة غير موجودة!", alert=True)
        return

    user_posting_modes[event.chat_id] = {"mode": "single_template", "template_id": template_id}
    await ask_posting_settings(event)


async def _ask_posting_interval_and_start(event, all_channels=False, channel_id=None):
    user_id = event.chat_id
    phone = user_posting_accounts.get(user_id)
    mode_data = user_posting_modes.get(user_id, {"mode": "single_template", "template_id": None})
    
    async with event.client.conversation(user_id) as conv:
        active_conversations[user_id] = conv
        try:
            await conv.send_message(
                "أدخل الفترة بين النشرات بالثواني (60 ثانية كحد أدنى):",
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
                
                if interval < 60:
                    await conv.send_message("يجب أن تكون الفترة 60 ثانية على الأقل!")
                    return
                
                # بدء مهمة النشر
                phones = phone if isinstance(phone, list) else [phone]
                for selected_phone in phones:
                    task_id = await create_active_posting_task(
                        user_id,
                        selected_phone,
                        interval,
                        repetitions,
                        all_channels,
                        channel_id,
                        mode_data.get("mode", "single_template"),
                        mode_data.get("template_id"),
                    )
                    if task_id is None:
                        await conv.send_message("تعذر حفظ مهمة النشر في قاعدة البيانات.")
                        continue
                    start_posting_worker(run_posting_task(
                        user_id=user_id,
                        phone=selected_phone,
                        interval=interval,
                        repetitions=repetitions,
                        all_channels=all_channels,
                        channel_id=channel_id,
                        posting_mode=mode_data.get("mode", "single_template"),
                        selected_template_id=mode_data.get("template_id"),
                        task_id=task_id,
                        total_repetitions=repetitions,
                    ), name=f"posting-{task_id}", user_id=user_id, task_id=task_id)
                
                target_text = "جميع القنوات" if all_channels else "القناة المحددة"
                mode_text = "جميع الكلايش" if mode_data.get("mode") == "all_templates" else "الكليشة المحددة"
                await conv.send_message(f"✅ بدأ النشر التلقائي في {target_text} باستخدام {mode_text} للحسابات المحددة بنجاح!")
            except ValueError:
                await conv.send_message("الرجاء إدخال أرقام صحيحة!")
        except asyncio.TimeoutError:
            await conv.send_message("⏱ انتهى الوقت المحدد للإدخال!")
        finally:
            if user_id in active_conversations:
                del active_conversations[user_id]


async def posting_all_channels(event):
    await _ask_posting_interval_and_start(event, all_channels=True)


async def select_channel_for_posting(event):
    user_id = event.chat_id
    super_channels = await get_super_channels(user_id)
    
    item_buttons = []
    for index, channel in enumerate(super_channels):
        item_buttons.append(Button.inline(channel[1], data=f"select_channel:{index}"))
    buttons = arrange_buttons(item_buttons)
    
    buttons.append(cancel_operation_keyboard()[0])
    
    await event.edit("📢 اختر القناة للنشر:", buttons=buttons)


async def posting_specific_channel(event, data):
    index_text = data.split(':', 1)[1]
    user_id = event.chat_id
    super_channels = await get_super_channels(user_id)
    try:
        channel_id = super_channels[int(index_text)][0]
    except (TypeError, ValueError, IndexError):
        await event.answer("❌ بيانات السوبر غير صالحة!", alert=True)
        return
    
    await _ask_posting_interval_and_start(event, all_channels=False, channel_id=channel_id)


async def stop_posting_group_handler(event):
    supers = await get_super_channels(event.chat_id)
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
    supers = await get_super_channels(event.chat_id)
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
    # إيقاف مهام صاحب الأمر فقط؛ المطور لا يوقف مهام بقية المستخدمين.
    task_rows = await execute_query(
        "SELECT id FROM active_posting_tasks WHERE user_id=? AND active=1",
        (event.chat_id,),
        fetchall=True,
    ) or []
    task_ids = [row[0] for row in task_rows]
    await execute_query(
        "UPDATE active_posting_tasks SET active=0 WHERE user_id=?",
        (event.chat_id,),
        commit=True
    )
    cancelled = await stop_user_posting_workers(event.chat_id, task_ids)
    await event.answer(
        f"{msgs['POSTING_STOPPED']}\nتم إيقاف {max(len(task_ids), cancelled)} مهمة تخص حساباتك.",
        alert=True,
    )
