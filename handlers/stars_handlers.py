"""Direct Stars checkout, country pricing and ownership-scoped payment support."""
import logging

from telethon import functions, types

import stars_purchase as purchases
from helpers import create_user
from keyboards import Button
from utils.asyncio_compat import to_thread
from utils.privacy import mask_phone
from workflow import query

logger = logging.getLogger(__name__)
LABELS = {'pending': 'بانتظار الدفع', 'checkout': 'الدفع قيد التنفيذ', 'paid': 'مدفوع، بانتظار التسليم',
          'completed': 'تم التسليم', 'refund_pending': 'الاسترداد قيد التنفيذ', 'refunded': 'أُعيدت النجوم',
          'expired': 'انتهت الفاتورة', 'cancelled': 'أُلغي الطلب'}


async def buy_stars(event, phone):
    try:
        order = await purchases.create(event.sender_id, phone)
    except ValueError as exc:
        return await event.answer(str(exc), alert=True)
    invoice = types.InputMediaInvoice(
        title='شراء رقم بالنجوم',
        description=f"شراء رقم من {order['name']} مقابل {order['amount']} نجمة، يُحفظ ضمن الحسابات المشتراة بعد الدفع.",
        invoice=types.Invoice(currency='XTR', prices=[types.LabeledPrice('شراء رقم', order['amount'])]),
        payload=(purchases.PREFIX + order['id']).encode(), provider='', provider_data=types.DataJSON(data='{}'),
        start_param='number',
    )
    try:
        await event.client(functions.messages.SendMediaRequest(peer=event.sender_id, media=invoice, message=''))
    except Exception:
        logger.exception('Direct Stars invoice creation failed: order=%s', order['id'])
        # Only a pending invoice can be cancelled; a concurrent checkout remains valid.
        try:
            await to_thread(purchases.action_sync, event.sender_id, order['id'], 'cancel')
        except ValueError:
            pass
        return await event.answer('تعذر إرسال الفاتورة. راجع /paysupport ثم حاول مجدداً.', alert=True)
    await event.edit(
        f"⭐ تم إرسال فاتورة بقيمة {order['amount']} نجمة. الحجز صالح لخمس دقائق.\n"
        'سيُضاف الرقم إلى الحسابات المشتراة بعد تأكيد الدفع.\nلمتابعة الطلب أو مشاكل الدفع: /paysupport',
        buttons=[[Button.inline('متابعة الطلب', data=f"stars:order:{order['id']}")],
                 [Button.inline('إلغاء الفاتورة', data=f"stars:cancel:{order['id']}")]],
    )


async def country_prices(event,page=0):
    from .number_handlers import pricing_menu
    return await pricing_menu(event)


async def edit_price(event,country):
    from .number_handlers import pricing_menu
    return await pricing_menu(event)


def set_price_sync(actor, country, price, number_type=None):
    from postgres import connect
    from staff_security import permitted
    from number_catalog import validate_type
    validate_type(number_type)
    with connect() as db:
        if not permitted(db, actor, 'finance'):
            raise ValueError('صلاحية المالية مطلوبة.')
        if db.execute('UPDATE number_type_prices SET bot_sell_stars=?,version=version+1 WHERE country_id=? AND number_type=? RETURNING country_id', (price, country,number_type)).fetchone() is None:
            raise ValueError('الدولة غير موجودة.')
        db.execute('INSERT INTO audit_log(user_id,action) VALUES(?,?)', (actor, f'stars_country_price:{country}:{price}'))


async def paysupport(event):
    await create_user(event.sender_id)
    rows = await query('''SELECT order_id,phone,amount,status FROM star_purchase_orders
        WHERE user_id=? ORDER BY created_at DESC LIMIT 10''', (event.sender_id,), fetchall=True)
    buttons = [[Button.inline(f'{mask_phone(phone)} · {amount} ⭐ · {LABELS[state]}', data=f'stars:order:{key}')]
               for key, phone, amount, state in rows]
    buttons.append([Button.inline('فتح تذكرة لمشكلة دفع', data='feature:tickets:new:0')])
    buttons.append([Button.inline('الحسابات المشتراة', data='view_purchased')])
    await event.edit('دعم الدفع بالنجوم\nاختر طلبًا لعرض حالته أو إعادة المحاولة.\n'
                     'أرسل معرّف الطلب عند فتح تذكرة دعم.', buttons=buttons)


async def detail(event, key):
    row = await query('''SELECT phone,amount,status FROM star_purchase_orders
        WHERE order_id=? AND user_id=?''', (key, event.sender_id), fetchone=True)
    if not row:
        raise ValueError('الطلب غير موجود ضمن حسابك.')
    phone, amount, state = row
    buttons = []
    if state == 'pending':
        buttons.append([Button.inline('إلغاء الفاتورة', data=f'stars:cancel:{key}')])
    if state == 'paid':
        buttons += [[Button.inline('إعادة محاولة التسليم', data=f'stars:retry:{key}')],
                    [Button.inline('طلب إعادة النجوم', data=f'stars:refundask:{key}')]]
    if state == 'completed':
        buttons.append([Button.inline('الحسابات المشتراة', data='view_purchased')])
    buttons += [[Button.inline('تحديث الحالة', data=f'stars:order:{key}')],
                [Button.inline('فتح تذكرة دعم', data='feature:tickets:new:0')],
                [Button.inline('كل طلباتي', data='stars:support:0')]]
    await event.edit(f'الطلب: {key}\nالرقم: {mask_phone(phone)}\nالسعر: {amount} ⭐\nالحالة: {LABELS[state]}',
                     parse_mode=None, buttons=buttons)


async def callback(event, data):
    try:
        _, action, value = data.split(':', 2)
        if action == 'buy':
            return await buy_stars(event, value)
        if action == 'prices':
            return await country_prices(event, int(value))
        if action == 'price':
            return await edit_price(event, int(value))
        if action == 'support':
            return await paysupport(event)
        # Validate ownership before every order action.
        if not await query('SELECT 1 FROM star_purchase_orders WHERE order_id=? AND user_id=?',
                           (value, event.sender_id), fetchone=True):
            raise ValueError('الطلب غير موجود ضمن حسابك.')
        if action == 'retry':
            await purchases.process(event.client, value)
        elif action == 'refundask':
            return await event.edit('تأكيد طلب إعادة النجوم إذا لم يُسلّم الرقم بعد؟',
                buttons=[[Button.inline('تأكيد الاسترداد', data=f'stars:refund:{value}')],
                         [Button.inline('رجوع', data=f'stars:order:{value}')]])
        elif action in {'cancel', 'refund'}:
            await to_thread(purchases.action_sync, event.sender_id, value, action)
            if action == 'refund':
                await purchases.refund(event.client, value)
        elif action != 'order':
            raise ValueError('الإجراء غير صالح.')
        await detail(event, value)
    except ValueError as exc:
        await event.answer(str(exc), alert=True)
