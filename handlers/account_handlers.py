# Auto-split from handlers.py. Keep shared imports/state in base.py.
from .base import *
from utils.privacy import mask_phone


async def show_account_options(event, data):
    parts = data.split(':')
    account_type = parts[0]
    phone = parts[1]
    user_id = event.chat_id
    
    # الحصول على معلومات الحساب
    from account_access import resolve
    account_info = await resolve(user_id,phone,sensitive=True)
    if not account_info:
        await event.answer("❌ لم يتم العثور على الحساب!", alert=True)
        return
    
    actions = [
        Button.inline("• تخزين الرقم • 💾", data=f"store_existing:{phone}"),
        Button.inline("• الحصول على الكود • 🔐", data=f"get_code:{phone}"),
        Button.inline("• إنهاء الجلسات • 🚪", data=f"logout_sessions:{phone}"),
    ]
    stored = await get_stored_account_info(phone)
    if stored and stored[2] == user_id:
        actions.append(Button.inline("• حذف التنصيب • 🗑️", data=f"del_install:{phone}"))
    actions.append(Button.inline("• رجوع • ↩️", data="accounts_view"))
    buttons = arrange_buttons(actions)
    
    await event.edit(
        f"**خيارات الحساب:**\n\n"
        f"📱 الرقم: +{phone}\n"
        f"{await __import__('number_catalog').detail(phone)}\n\n"
        f"اختر الإجراء المطلوب:",
        buttons=buttons,parse_mode=None
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
    from account_access import resolve
    return await resolve(user_id,phone,sensitive=True)


async def get_account_twofa_for_user_action(user_id, phone):
    from account_access import resolve
    account = await resolve(user_id,phone,sensitive=True)
    return account[3] if account and account[3] else ""


async def account_settings_menu(event):
    await event.edit(msgs['ACCOUNT_MENU'], buttons=account_settings_keyboard())


async def unsupported_account_feature(event):
    await event.answer(
        "❌ لا يمكن تنفيذ هذا الخيار بشكل فعلي لأنه يتجاوز خصوصية المستخدمين أو قيود تيليجرام.",
        alert=True
    )


async def account_details(event, data):
    if not await ensure_admin(event):
        return
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
    twofa_state = "مفعّل" if account[3] and account[3] not in {"لا يوجد", "no password"} else "غير مفعّل"
    await event.edit(f"رقم الهاتف: +{phone}\nالتحقق بخطوتين: {twofa_state}", buttons=buttons)


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
    from account_access import accounts as accessible
    from number_catalog import TYPES
    accounts=await accessible(event.chat_id)
    metadata=await execute_query('SELECT phone,number_type FROM number_metadata WHERE phone=ANY(?)',([a[0] for a in accounts],),fetchall=True) or []
    types=dict(metadata)
    buttons=[[Button.inline(f"+{a[0]} · {TYPES.get(types.get(a[0]),'مراجعة')} · المالك {a[2]}",data=f'stored_account:{a[0]}')] for a in accounts[:50]]
    buttons.append([Button.inline('رجوع',data='accounts_view')])
    await event.edit('📱 الحسابات المتاحة لك\nاختر حسابًا لعرض تفاصيله وإجراءاته.',buttons=buttons,parse_mode=None)


async def view_rented_accounts(event):
    """Show the renter's active leases without exposing sessions or 2FA."""
    accounts = await get_rented_accounts(event.chat_id)
    if not accounts:
        await event.answer("❌ لا توجد حسابات مستأجرة نشطة حالياً.", alert=True)
        return
    buttons = arrange_buttons([
        Button.inline(f"+{phone} — ينتهي {ends_at:%Y-%m-%d %H:%M}", data="rented_account_info")
        for phone, _session, ends_at in accounts
    ])
    buttons.append([Button.inline("• رجوع • ↩️", data="accounts_view")])
    await event.edit(
        "**⏳ الحسابات المستأجرة النشطة:**\n"
        "هذه الحسابات متاحة لخدمات الرشق والانضمام فقط، ولا تعرض جلساتها أو إعداداتها.",
        buttons=buttons,
    )


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
    # "النشطة" هنا تعني أن جلسة الحساب ما زالت صالحة ومصرحاً لها لدى تيليجرام،
    # وليس مجرد أن الحساب استُخدم سابقاً في ميزة الاسم الوقتي.
    stored_accounts = await get_stored_accounts(user_id)
    purchased_accounts = await get_purchased_accounts(user_id)

    # قد يكون الحساب المشترى مخزناً أيضاً؛ لا نتحقق منه مرتين.
    accounts = {
        account[0]: ("purchased_account", account)
        for account in purchased_accounts
    }
    accounts.update({
        account[0]: ("stored_account", account)
        for account in stored_accounts
    })

    if not accounts:
        await event.answer("❌ لا توجد حسابات محفوظة أو مشتراة للتحقق منها!", alert=True)
        return

    active_accounts = []
    unavailable_count = 0
    unchecked_count = 0

    for phone, (account_type, account) in accounts.items():
        client = None
        try:
            client = TelegramClient(StringSession(account[1]), API_ID, API_HASH)
            await client.connect()
            if await client.is_user_authorized():
                active_accounts.append((phone, account_type))
            else:
                unavailable_count += 1
        except (AuthKeyInvalidError, AuthKeyUnregisteredError, SessionExpiredError,
                SessionRevokedError, UnauthorizedError, UserDeactivatedBanError,
                UserDeactivatedError):
            unavailable_count += 1
        except Exception as exc:
            # لا نعدّ مشكلة الشبكة أو API دليلاً على أن الجلسة منتهية.
            unchecked_count += 1
            logger.warning("Could not verify account %s: %s", mask_phone(phone), exc)
        finally:
            if client and client.is_connected():
                try:
                    await client.disconnect()
                except Exception as exc:
                    logger.warning("Could not disconnect account %s: %s", mask_phone(phone), exc)

    if not active_accounts:
        message = "❌ لا توجد حسابات صالحة ومتّصلة حالياً."
        if unchecked_count:
            message += f"\n⚠️ تعذر التحقق من {unchecked_count} حساب بسبب الاتصال."
        await event.answer(message, alert=True)
        return

    buttons = arrange_buttons([
        Button.inline(f"🔥 +{phone}", data=f"{account_type}:{phone}")
        for phone, account_type in active_accounts
    ])
    buttons.append([Button.inline("• رجوع • ↩️", data="accounts_view")])

    text = "**🔥 الحسابات الصالحة والمتّصلة:**\n\nاختر حساباً لعرض خياراته."
    if unavailable_count:
        text += f"\n\n❌ جلسات غير صالحة: {unavailable_count}"
    if unchecked_count:
        text += f"\n⚠️ تعذر التحقق: {unchecked_count}"

    await event.edit(text, buttons=buttons)
