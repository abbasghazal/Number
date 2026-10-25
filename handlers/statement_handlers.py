import asyncio

import financial_statement as statement
from helpers import get_user
from keyboards import Button, cancel_operation_keyboard
from .base import active_conversations, enforce_rate_limit


async def show_detail(event, identifier, *, respond=False):
    entry = await statement.get_entry(event.sender_id, identifier)
    if entry is None:
        await event.answer('العملية غير موجودة ضمن حسابك.', alert=True)
        return
    send = event.respond if respond else event.edit
    await send(statement.format_entry(entry), parse_mode=None, buttons=[
        [Button.inline('الدعم بخصوص هذه العملية', data=f"stmt:t:{entry['id']}")],
        [Button.inline('رجوع إلى كشف الحساب', data='stmt:l:all:0:0')],
    ])


async def statement_menu(event, data='stmt:l:all:0:0'):
    if event.chat_id != event.sender_id:
        return await event.answer('كشف الحساب متاح في المحادثة الخاصة فقط.', alert=True)
    parts = data.split(':')
    try:
        if parts[:2] == ['stmt', 'v'] and len(parts) == 3:
            return await show_detail(event, statement.validate_id(parts[2]))
        if parts[:2] == ['stmt', 't'] and len(parts) == 3:
            identifier = statement.validate_id(parts[2])
            from .user_handlers import support_request
            return await support_request(event, operation_reference=identifier)
        if data == 'stmt:s':
            return await search_statement(event)
        if data == 'stmt:a':
            from .user_handlers import audit_activity
            return await audit_activity(event)
        if len(parts) != 5 or parts[:2] != ['stmt', 'l']:
            raise ValueError('invalid callback')
        group, days, page = parts[2], int(parts[3]), int(parts[4])
        entries, more = await statement.list_entries(event.sender_id, group, days, page)
    except ValueError:
        return await event.answer('رقم العملية أو مرشح العرض غير صالح.', alert=True)
    user = await get_user(event.sender_id) or {}
    text = (f"🧾 كشف حسابك\nالرصيد الحالي: {statement.money(user.get('coins'))}\n"
            f"{statement.GROUPS[group][0]} · {statement.PERIODS[days]} · صفحة {page+1}\n"
            'الأرصدة بالدولار داخل البوت، والتواريخ بتوقيت قاعدة البيانات.\n\n')
    buttons = []
    for entry in entries:
        label = statement.OPERATIONS.get(entry['operation'], 'حركة رصيد أخرى')
        amount = entry['amount']
        change = 'فاتورة دون حركة مسجلة' if amount is None else f"{'+' if amount > 0 else '−'}{statement.money(abs(amount))}"
        text += (f"#{entry['id']} · {label}\n"
                 f"{entry['created_at']:%Y-%m-%d %H:%M} · {change} · "
                 f"{statement.STATUS.get(entry['status'], 'غير معروفة')}\n\n")
        buttons.append([Button.inline(f"تفاصيل #{entry['id']}", data=f"stmt:v:{entry['id']}")])
    if not entries:
        text += 'لا توجد عمليات مسجلة تطابق هذا الاختيار.\n'
    nav = []
    if page:
        nav.append(Button.inline('السابق', data=f'stmt:l:{group}:{days}:{page-1}'))
    if more:
        nav.append(Button.inline('التالي', data=f'stmt:l:{group}:{days}:{page+1}'))
    if nav:
        buttons.append(nav)
    categories = [Button.inline(label, data=f'stmt:l:{key}:{days}:0')
                  for key, (label, _) in statement.GROUPS.items()]
    buttons += [categories[i:i+2] for i in range(0, len(categories), 2)]
    periods = [Button.inline(label, data=f'stmt:l:{group}:{period}:0')
               for period, label in statement.PERIODS.items()]
    buttons += [periods[:2], periods[2:]]
    buttons += [[Button.inline('بحث برقم العملية', data='stmt:s'),
                 Button.inline('سجل النشاط السابق', data='stmt:a')],
                [Button.inline('رجوع', data='main')]]
    period = {0: 'all', 7: 'week', 30: 'month', 90: 'quarter'}.get(days, 'all')
    buttons.insert(-1, [Button.inline('تصدير كشف الحساب CSV', data=f'feature:statement:export:{period}')])
    await event.edit(text, parse_mode=None, buttons=buttons)


async def search_statement(event):
    user_id = event.sender_id
    if not await enforce_rate_limit(event, 'statement_search', 3):
        return
    if user_id in active_conversations:
        return await event.answer('أكمل المحادثة الحالية أو ألغها أولًا.', alert=True)
    async with event.client.conversation(user_id) as conv:
        active_conversations[user_id] = conv
        try:
            await conv.send_message('أرسل رقم العملية كما يظهر في الكشف، مثل L1052 أو رقم فاتورة يبدأ بـP.',
                                    buttons=cancel_operation_keyboard())
            response = await conv.get_response(timeout=120)
            try:
                identifier = statement.validate_id(response.raw_text)
            except ValueError:
                await conv.send_message('رقم غير صالح. استخدم الرقم الظاهر في كشف الحساب.')
                return
            entry = await statement.get_entry(user_id, identifier)
            if entry is None:
                await conv.send_message('العملية غير موجودة ضمن حسابك.')
                return
            await conv.send_message(statement.format_entry(entry), parse_mode=None, buttons=[
                [Button.inline('الدعم بخصوص هذه العملية', data=f'stmt:t:{identifier}')],
                [Button.inline('رجوع', data='stmt:l:all:0:0')],
            ])
        except asyncio.TimeoutError:
            await conv.send_message('انتهى وقت البحث.')
        finally:
            active_conversations.pop(user_id, None)
