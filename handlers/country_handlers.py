import asyncio

from telethon import Button
from session_guard import TelegramClient
from telethon.errors import SessionPasswordNeededError
from telethon.sessions import StringSession

from account_importer import import_batch, import_string_session, parse_session_file, validate_phone_submission
from config import API_HASH, API_ID
from helpers import (
    add_account_if_absent,
    add_new_country,
    delete_account,
    delete_country,
    delete_sensitive_message,
    get_accounts,
    get_countries,
    get_countries_with_available_accounts,
    schedule_managed_twofa_change,
    session_requires_twofa,
)
from keyboards import arrange_buttons, cancel_operation_keyboard

from .base import (
    TWOFA_CHANGE_NOTICE,
    active_conversations,
    add_number_country_pages,
    ensure_admin,
    logger,
    parse_positive_amount,
    user_pages,
)
from .buy_sell_handlers import buy_number
from .common import ProgressMessage


async def next_countries_page(event):
    user_id = event.chat_id
    if user_id not in user_pages:
        user_pages[user_id] = 0
    
    # احصل على قائمة الدول
    countries = await get_countries_with_available_accounts()
    
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


async def show_accounts(event, data):
    if not await ensure_admin(event):
        return
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


async def del_account_confirm(event, data):
    if not await ensure_admin(event):
        return
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
    if not await ensure_admin(event):
        return
    parts = data.split(':')
    phone = parts[1]
    await delete_account(phone)
    await event.edit(f"✅ تم حذف +{phone} بنجاح!")


async def add_number_process(event, data=None):
    if not await ensure_admin(event):
        return
    async with event.client.conversation(event.chat_id) as conv:
        active_conversations[event.chat_id] = conv
        from .number_handlers import collect_submission
        from number_catalog import submission
        type_token = None
        try:
            typed_data = await collect_submission(conv,event.chat_id,pending=False)
            type_token = submission.set(typed_data)
            await conv.send_message(
                "أرسل رقم الهاتف، أو StringSession، أو ملف TXT للجلسات. سيتم التعرف على الدولة من كود الرقم تلقائيًا.",
                buttons=cancel_operation_keyboard(),
            )
            phone_resp = await conv.get_response(timeout=300)
            if phone_resp.file:
                await _add_session_file(conv, phone_resp, None)
                return
            raw_submission = (phone_resp.raw_text or "").strip()
            if not raw_submission.replace("+", "").replace(" ", "").isdigit():
                await _add_one_session(conv, raw_submission, None)
                return
            phone = phone_resp.text.replace("+", "").replace(" ", "")

            country, reason = await validate_phone_submission(phone)
            if not country:
                await conv.send_message(f"❌ {reason}")
                return
            calling_code = country[0]
            
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
                    await conv.send_message(
                        "🔐 الحساب محمي بتحقق بخطوتين. أرسل كلمة المرور؛ ستُحفظ مؤقتاً بصورة مشفرة "
                        "لإتمام التغيير إلى كلمة التحقق الثابتة ثم تُحذف بيانات المهمة بعد نجاحها.",
                        buttons=cancel_operation_keyboard(),
                    )
                    password_resp = await conv.get_response(timeout=300)
                    await client.sign_in(password=password_resp.text)
                    twofa = password_resp.text
                    await delete_sensitive_message(password_resp)
                
                # حفظ الجلسة
                session_str = client.session.save()
                
                # إضافة الحساب إلى قاعدة البيانات
                if await add_account_if_absent(phone, session_str, calling_code, twofa):
                    await schedule_managed_twofa_change(phone, session_str, twofa if twofa != 'لا يوجد' else None)
                    await conv.send_message(f"✅ تم إضافة الحساب بنجاح!\n\n{TWOFA_CHANGE_NOTICE}")
                else:
                    await conv.send_message("❌ الحساب موجود بالفعل، ولم يتم استبداله.")
            except Exception:
                logger.exception("Admin phone-account import failed for %s", event.chat_id)
                await conv.send_message("❌ تعذر إتمام التحقق من الحساب. حاول لاحقًا.")
            finally:
                if client.is_connected():
                    await client.disconnect()
        except (asyncio.TimeoutError, ValueError) as exc:
            await conv.send_message(str(exc) if isinstance(exc,ValueError) else '⏱ انتهى الوقت المحدد للإدخال!',parse_mode=None)
        finally:
            if type_token is not None:
                submission.reset(type_token)
            if event.chat_id in active_conversations:
                del active_conversations[event.chat_id]


async def _add_one_session(conv, value, calling_code):
    result = await import_string_session(value, required_calling_code=calling_code)
    if not result.account:
        await conv.send_message(f"❌ رُفضت الجلسة: {result.reason}")
        return
    account = result.account
    needs_twofa = await session_requires_twofa(account.session)
    if needs_twofa is None:
        await conv.send_message("❌ تعذر التحقق من حالة التحقق بخطوتين للجلسة.")
        return
    current_password = None
    if needs_twofa:
        await conv.send_message(
            "🔐 أرسل كلمة التحقق بخطوتين الحالية. تُحفظ مشفرة لإتمام التغيير إلى كلمة التحقق الثابتة، وتُحذف بيانات المهمة بعد نجاحها.",
            buttons=cancel_operation_keyboard(),
        )
        password_response = await conv.get_response(timeout=300)
        current_password = password_response.text
        await delete_sensitive_message(password_response)
    if await add_account_if_absent(account.phone, account.session, account.calling_code, current_password or 'لا يوجد'):
        await schedule_managed_twofa_change(account.phone, account.session, current_password)
        await conv.send_message(f"✅ تم التحقق من الجلسة وإضافة الحساب بنجاح.\n\n{TWOFA_CHANGE_NOTICE}")
    else:
        await conv.send_message("❌ الحساب موجود بالفعل، ولم يتم استبداله.")


async def _add_session_file(conv, response, calling_code):
    name = (getattr(response.file, "name", "") or "").lower()
    mime = (getattr(response.file, "mime_type", "") or "").lower()
    if not name.endswith(".txt") and mime != "text/plain":
        await conv.send_message("❌ أرسل ملف TXT فقط.")
        return
    status = await conv.send_message("⏳ تم استلام الملف، جارٍ تنزيله وقراءة الجلسات…")
    content = await response.download_media(bytes)
    try:
        entries = parse_session_file(content or b"")
    except ValueError as exc:
        await conv.send_message(f"❌ {exc}")
        return
    values = [session for session, _ in entries]
    reporter = ProgressMessage(status, "جارٍ فحص جلسات الملف", len(values))
    async def report_import(processed, _total):
        await reporter.update(processed, force=processed == len(values))
    results = await import_batch(values, required_calling_code=calling_code, on_progress=report_import)
    accepted = 0
    duplicates = sum(item.duplicate for item in results)
    reasons = {}
    for item in results:
        if item.account:
            if await add_account_if_absent(item.account.phone, item.account.session, item.account.calling_code, 'لا يوجد'):
                await schedule_managed_twofa_change(item.account.phone, item.account.session)
                accepted += 1
            else:
                duplicates += 1
                reasons["الحساب موجود بالفعل."] = reasons.get("الحساب موجود بالفعل.", 0) + 1
        elif item.reason:
            reasons[item.reason] = reasons.get(item.reason, 0) + 1
    summary = [f"تمت معالجة {len(values)} جلسة.", f"تمت الإضافة: {accepted}.", f"المرفوض: {len(results) - accepted}.", f"المكرر: {duplicates}."]
    if reasons:
        summary.append("أسباب الرفض: " + "؛ ".join(f"{reason} ({count})" for reason, count in reasons.items()))
    if accepted:
        summary.append(TWOFA_CHANGE_NOTICE)
    await reporter.update(len(values), accepted, len(results) - accepted, force=True)
    await conv.send_message("\n".join(summary))


async def del_country(event, data):
    if not await ensure_admin(event):
        return
    parts = data.split('_')
    calling_code = parts[1]
    name = parts[2]
    await delete_country(calling_code)
    await event.edit(f"✅ تم حذف {name} بنجاح!")


async def add_country_handler(event):
    if not await ensure_admin(event):
        return
    async with event.client.conversation(event.chat_id) as conv:
        active_conversations[event.chat_id] = conv
        try:
            await conv.send_message("أرسل اسم الدولة مع الرمز (مثال: العراق 🇮🇶):", buttons=cancel_operation_keyboard())
            name = await conv.get_response(timeout=300)
            
            await conv.send_message("أرسل رمز النداء (مثال: +964):", buttons=cancel_operation_keyboard())
            calling_code = await conv.get_response(timeout=300)
            
            await conv.send_message("أرسل سعر الشراء ($):", buttons=cancel_operation_keyboard())
            price = await conv.get_response(timeout=300)
            
            await conv.send_message("أرسل سعر البيع ($):", buttons=cancel_operation_keyboard())
            sell_price = await conv.get_response(timeout=300)
            
            try:
                await add_new_country(
                    name.text,
                    calling_code.text.replace("+", ""),
                    parse_positive_amount(price.text),
                    parse_positive_amount(sell_price.text)
                )
                await conv.send_message(f"✅ تم إضافة {name.text} بنجاح!")
            except ValueError:
                await conv.send_message("❌ قيمة السعر غير صالحة.")
            except Exception:
                logger.exception("Failed to add country")
                await conv.send_message("❌ تعذر إضافة الدولة حالياً.")
        except asyncio.TimeoutError:
            await conv.send_message("⏱ انتهى الوقت المحدد للإدخال!")
        finally:
            if event.chat_id in active_conversations:
                del active_conversations[event.chat_id]


async def del_country_menu(event):
    if not await ensure_admin(event):
        return
    countries = await get_countries()
    item_buttons = []
    for c in countries:
        item_buttons.append(Button.inline(f"{c[1]}: {c[2]}$", data=f"delete_{c[0]}_{c[1]}_{c[2]}"))
    buttons = arrange_buttons(item_buttons)
    buttons.append([Button.inline("• رجوع • ↩️", data="ajxjao")])
    buttons.append(cancel_operation_keyboard()[0])
    await event.edit("اختر الدولة للحذف:", buttons=buttons)


async def add_number_menu(event):
    # دولة الحساب تُستنتج من الرقم/الجسلة، لذلك لا نعرض قائمة الدول.
    await add_number_process(event)


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
    if not await ensure_admin(event):
        return
    countries = await get_countries()
    item_buttons = []
    for c in countries:
        item_buttons.append(Button.inline(f"{c[1]}", data=f"show_{c[0]}"))
    buttons = arrange_buttons(item_buttons)
    buttons.append([Button.inline("• رجوع • ↩️", data="ajxjao")])
    buttons.append(cancel_operation_keyboard()[0])
    await event.edit("اختر الدولة لحذف رقم منها:", buttons=buttons)
