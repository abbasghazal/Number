"""Explicit account delegation and administrative catalog without ownership transfer."""

import asyncio
from telethon import Button

import account_access
import number_catalog
from workflow import query
from .base import active_conversations
from .number_handlers import prompt
from .input_choices import choose_accounts, confirm, input_session


async def menu(event, data):
    user = event.chat_id
    parts = data.split(":")
    action = parts[1]
    if action == "logout":
        from .storage_handlers import confirmed_logout

        return await confirmed_logout(event, parts[2])
    if action in {"grant", "sensitive", "revoke"}:
        phone = parts[2]
        if len(parts) < 4:
            return await event.edit(
                "🔐 تفويض إدارة الحساب\n"
                "يتيح هذا الإذن للمطور المحدد تشغيل الخدمات على حسابك مع بقاء ملكيتك. "
                + (
                    "يشمل هذا الاختيار الوصول إلى أكواد الدخول وإنهاء الجلسات؛ راجع الإذن قبل التأكيد."
                    if action == "sensitive"
                    else "يمكنك إلغاء الإذن في أي وقت."
                ),
                parse_mode=None,
                buttons=[
                    [Button.inline("تأكيد", data=f"acct:{action}:{phone}:yes")],
                    [Button.inline("إلغاء", data="accounts_view")],
                ],
            )
        await account_access.grant(user, phone, sensitive=action == "sensitive", revoke=action == "revoke")
        return await event.edit(
            "✅ حُدّث تفويض إدارة الحساب.", parse_mode=None, buttons=[[Button.inline("رجوع", data="accounts_view")]]
        )
    if action == "mine":
        rows = await account_access.accounts(user, owned_only=True)
        return await event.edit(
            "🔐 أذونات إدارة حساباتي\nاختر الإذن المناسب لكل حساب. لا يغير الإذن ملكية الحساب.",
            parse_mode=None,
            buttons=[
                [
                    Button.inline(f"+{r[0]} · خدمات", data=f"acct:grant:{r[0]}"),
                    Button.inline("إذن حساس", data=f"acct:sensitive:{r[0]}"),
                    Button.inline("إلغاء الإذن", data=f"acct:revoke:{r[0]}"),
                ]
                for r in rows
            ]
            + [[Button.inline("رجوع", data="accounts_view")]],
        )
    if not account_access.is_manager(user):
        return await event.answer("هذه اللوحة لمدير الحسابات المحدد في الإعدادات.", alert=True)
    if action == "accounts":
        page = int(parts[2]) if len(parts) > 2 else 0
        if page < 0:
            raise ValueError("صفحة غير صالحة.")
        rows = await account_access.accounts(user)
        buttons = []
        for account in rows[page * 15 : (page + 1) * 15]:
            metadata = await query(
                "SELECT number_type FROM number_metadata WHERE phone=?", (account[0],), fetchone=True
            )
            buttons.append(
                [
                    Button.inline(
                        f"+{account[0]} · مالك {account[2]} · {account[4]} · {number_catalog.TYPES.get(metadata[0] if metadata else None, 'مراجعة')}",
                        data=f"acct:detail:{account[0]}",
                    )
                ]
            )
        if page:
            buttons.append([Button.inline("السابق", data=f"acct:accounts:{page - 1}")])
        if len(rows) > (page + 1) * 15:
            buttons.append([Button.inline("التالي", data=f"acct:accounts:{page + 1}")])
        buttons += [
            [Button.inline("الخدمات واختيار الحسابات", data="engagement_menu")],
            [Button.inline("رجوع", data="control_menu")],
        ]
        return await event.edit(
            f"👥 الحسابات المصرح بإدارتها\nالعدد: {len(rows)}\nيبقى كل حساب منسوبًا لمالكه الأصلي.",
            parse_mode=None,
            buttons=buttons,
        )
    if action == "detail":
        account = await account_access.resolve(user, parts[2])
        if not account:
            raise ValueError("لم يعد الإذن متاحًا.")
        from account_health import records, session_key, LABELS

        status = await records([session_key(account[1])])
        state = status.get(session_key(account[1]), {})
        return await event.edit(
            f"📱 +{account[0]}\nالمالك: {account[2]}\nالمصدر: {account[4]}\nالحالة: {LABELS.get(state.get('status'), 'غير مفحوص')}\n"
            + await number_catalog.detail(account[0]),
            parse_mode=None,
            buttons=[
                [Button.inline("إعدادات الحساب", data=f"account_actions:stored:{account[0]}")],
                [Button.inline("رجوع", data="acct:accounts:0")],
            ],
        )


async def search_wizard(event):
    from .account_services import get_service_accounts_for_user
    from search_join import start

    user = event.chat_id
    async with event.client.conversation(user) as conv, input_session(conv):
        active_conversations[user] = conv
        try:
            phrase = await prompt(
                conv, "🔎 رشق بحث\nأرسل عبارة البحث. تُجرى محاولة انضمام فقط إذا ظهر الهدف المطابق في نتائج الحساب نفسه."
            )
            target = await prompt(conv, "معرف القناة المستهدفة @username أو معرفها الرقمي الثابت:")
            accounts = await get_service_accounts_for_user(user)
            if not accounts:
                raise ValueError("لا توجد حسابات متاحة لك.")
            phones = set(await choose_accounts(conv, [a[0] for a in accounts], allow_count=True))
            accounts = [a for a in accounts if a[0] in phones]
            if not await confirm(conv,
                f"البحث: {phrase}\nالهدف: {target}\nالحسابات: {len(accounts)}\nبدء المهمة؟ لا تتضمن الخدمة وعدًا بتحسين ترتيب البحث.",
            ):
                return
            job = await start(event.client, user, phrase, target, accounts)
            await conv.send_message(
                f"⏳ بدأت المهمة {job}. تابع النتيجة من مركز المهام.",
                parse_mode=None,
                buttons=[
                    [Button.inline("إلغاء المهمة", data=f"search:cancel:{job}")],
                    [Button.inline("مركز المهام", data="ops:jobs:list:0")],
                ],
            )
        except (ValueError, asyncio.TimeoutError) as exc:
            await conv.send_message(str(exc) or "انتهى وقت الإدخال.", parse_mode=None)
        finally:
            active_conversations.pop(user, None)
