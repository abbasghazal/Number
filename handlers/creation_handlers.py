import asyncio
import workflow

from helpers import (
    create_auto_creation_task,
    create_private_group,
    get_active_auto_creation_tasks,
    run_auto_creation,
    update_auto_creation_task,
)
from keyboards import cancel_operation_keyboard, creation_settings_keyboard
from messages import msgs

from .base import auto_creation_tasks, enforce_rate_limit, manual_creation_tasks
from .common import choose_control_accounts


async def creation_settings_menu(event):
    await event.edit(msgs['CREATION_MENU'], buttons=creation_settings_keyboard())


@workflow.tracked('creation', owner='event', event=True)
async def manual_group_creation_handler(event, selected_accounts=None):
    """
    معالج الإنشاء اليدوي للمجموعات
    """
    user_id = event.chat_id
    if selected_accounts is None:
        selected_accounts = await choose_control_accounts(event, "manual_creation", "اختر الحسابات المستخدمة لإنشاء المجموعات", "control_settings_creation")
        if selected_accounts is None:
            return
    if not await enforce_rate_limit(event, "manual_group_creation", 15):
        return
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
                total_requested = count * len(selected_accounts)
                scope_text = (
                    f"{count} مجموعة لكل حساب ({total_requested} مجموعة إجمالاً)"
                    if len(selected_accounts) > 1 else f"{count} مجموعات"
                )
                await conv.send_message(f"⏳ جاري إنشاء {scope_text}...")
                
                success_count = 0
                processed = 0
                await workflow.progress(total=total_requested)
                for account in selected_accounts:
                    for _ in range(count):
                        await workflow.progress(processed=processed, success=success_count, failed=processed-success_count)
                        if not manual_creation_tasks.get(user_id, {}).get('active', True):
                            break
                        group_id, invite_link, _ = await create_private_group(account[1])
                        processed += 1
                        if group_id:
                            success_count += 1
                            manual_creation_tasks[user_id]['created'] = success_count
                            await event.client.send_message(
                                user_id,
                                f"✅ المجموعة #{success_count} من الحساب +{account[0]}\n"
                                f"🆔: {group_id}\n"
                                f"🔗: {invite_link}"
                            )
                        await workflow.pause(10)
                    if not manual_creation_tasks.get(user_id, {}).get('active', True):
                        break
                
                # إرسال ملخص النتائج
                await workflow.progress(processed=processed, success=success_count, failed=processed-success_count)
                await conv.send_message(
                    f"🎉 تم إنشاء {success_count} من أصل {total_requested} مجموعات بنجاح!"
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


async def auto_group_creation_handler(event, selected_accounts=None):
    """
    معالج الإنشاء التلقائي للمجموعات
    """
    user_id = event.chat_id
    if selected_accounts is None:
        selected_accounts = await choose_control_accounts(event, "auto_creation", "اختر الحسابات المستخدمة للإنشاء التلقائي", "control_settings_creation")
        if selected_accounts is None:
            return
    if not await enforce_rate_limit(event, "auto_group_creation", 15):
        return
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
                asyncio.create_task(run_auto_creation(user_id, seconds, hours, selected_accounts))
                
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
