"""Telegram menus for the store, promotions, staff and customer support."""
import business_reports
import commerce
import recurring_jobs
import staff_security
import storefront
import support_center
from config import ADMIN_ID
from keyboards import Button
from utils.privacy import mask_phone
from workflow import query
from .platform_handlers import ask


def btn(text, section, action, value='0'):
    return Button.inline(text, data=f'feature:{section}:{action}:{value}')


def back(data='main'):
    return [Button.inline('رجوع', data=data)]


async def shop(event,action,value):
    from .number_handlers import buy_menu,quote_panel
    if action=='quote':
        return await quote_panel(event,value)
    return await buy_menu(event)


async def loyalty_menu(event, action, value):
    if action == 'edit':
        if not await staff_security.require(event, 'finance'):
            return
        text = await ask(event, 'أرسل: اسم المستوى | إجمالي المشتريات المطلوب بالدولار | نسبة الخصم\nمثال: فضي | 100 | 2')
        if text is None:
            return
        name, threshold, percent = (p.strip() for p in text.split('|'))
        await commerce.configure_tier(event.sender_id, name, threshold, percent)
    data = await commerce.loyalty_status(event.sender_id)
    tiers = await query('SELECT name,threshold,percent FROM loyalty_tiers ORDER BY threshold', fetchall=True) or []
    pref = await query('SELECT coupon FROM commerce_preferences WHERE user_id=?', (event.sender_id,), fetchone=True)
    text = (f"مستواك: {data['tier']}\nالمشتريات المكتملة: {data['purchases']}\n"
            f"إجمالي الإنفاق المؤهل: {data['spent']}$\nخصم الولاء: {data['percent']}%\n"
            f"الكوبون المختار: {pref[0] if pref and pref[0] else 'لا يوجد'}\n\n"
            'يُطبّق الخصم الأكبر بين الكوبون والولاء دون جمعهما. يشمل شراء المخزون والتأجير والتجديد.\n'
            'يبدأ احتساب الولاء من تفعيل هذه الإضافة؛ الإيداع والتحويل والمزاد لا يرفع المستوى.\n\n' +
            '\n'.join(f'• {name}: من {threshold}$، خصم {percent}%' for name, threshold, percent in tiers))
    buttons = [[btn('إدخال كوبون', 'coupons', 'select')], back()]
    if await staff_security.has_permission(event.sender_id, 'finance'):
        buttons.insert(1, [btn('إعداد مستويات الولاء', 'loyalty', 'edit')])
    await event.edit(text, parse_mode=None, buttons=buttons)


async def coupons(event, action, value):
    if action == 'select':
        text = await ask(event, 'أرسل رمز الكوبون، أو - لإزالة الكوبون المختار:')
        if text is None:
            return
        code = await commerce.select_coupon(event.sender_id, text)
        await event.respond(f'✅ تم اختيار الكوبون {code}. يُطبّق على عرض الشراء أو التأجير التالي.' if code else
                            '✅ تم إلغاء الكوبون المختار.', parse_mode=None)
        return await loyalty_menu(event, 'view', '0')
    if not await staff_security.require(event, 'finance'):
        return
    if action == 'new':
        prompt = ('أرسل الحقول السبعة بالترتيب، مع مسافة بين كل حقل:\n'
                  '1. رمز الكوبون\n2. النوع: نسبة أو مبلغ\n3. قيمة الخصم\n'
                  '4. الخدمة: شراء أو تأجير أو تجديد أو الكل\n5. مدة الصلاحية بالأيام\n'
                  '6. إجمالي مرات الاستخدام\n7. الحد لكل مستخدم\n\n'
                  'مثال: SAVE10 نسبة 10 الكل 30 100 1')
        while True:
            text = await ask(event, prompt)
            if text is None:
                return
            fields = text.split()
            if len(fields) == 7:
                break
            prompt = (f'أرسلت {len(fields)} حقول؛ المطلوب 7. لم يتم إنشاء كوبون.\n\n' +
                      'أعد إرسال البيانات كاملة:\nالرمز النوع القيمة الخدمة الأيام إجمالي الاستخدامات الحد لكل مستخدم\n'
                      'مثال: SAVE10 نسبة 10 الكل 30 100 1')
        code, kind, amount, service, days, uses, per_user = fields
        kind = {'نسبة': 'percent', 'مبلغ': 'fixed'}.get(kind, kind)
        service = {'شراء': 'purchase', 'تأجير': 'rental', 'تجديد': 'renewal', 'الكل': 'all'}.get(service, service)
        await commerce.create_coupon(event.sender_id, code, kind, amount, service, int(days), int(uses), int(per_user))
    elif action == 'disable':
        await commerce.disable_coupon(event.sender_id, value)
    page = int(value) if action == 'admin' else 0
    if page < 0:
        raise ValueError('صفحة غير صالحة.')
    rows = await query('''SELECT code,kind,value,service,used,max_uses,per_user,expires_at,enabled
        FROM commerce_coupons ORDER BY created_at DESC LIMIT 16 OFFSET ?''', (page*15,), fetchall=True) or []
    text = 'إدارة الكوبونات\n\n' + ('\n'.join(f'{c}: {v}{"%" if k == "percent" else "$"} · {s} · {u}/{m} · لكل مستخدم {p} · حتى {e:%Y-%m-%d} · {"مفعل" if on else "متوقف"}'
                                            for c,k,v,s,u,m,p,e,on in rows[:15]) or 'لا توجد كوبونات.')
    buttons = [[btn('إنشاء كوبون', 'coupons', 'new')]]
    buttons += [[btn(f'إيقاف {r[0]}', 'coupons', 'disable', r[0])] for r in rows[:15] if r[8]]
    if page:
        buttons.append([btn('السابق', 'coupons', 'admin', str(page-1))])
    if len(rows) > 15:
        buttons.append([btn('التالي', 'coupons', 'admin', str(page+1))])
    buttons.append(back('admin_panel'))
    await event.edit(text, parse_mode=None, buttons=buttons)


async def tickets(event, action, value):
    user = event.sender_id
    if action == 'new':
        text = await ask(event, 'أرسل تفاصيل طلب الدعم (حتى 3000 حرف):')
        if text is None:
            return
        ticket = await support_center.create(user, text)
        action, value = 'view', str(ticket)
    if action in {'reply', 'close', 'reopen', 'assign', 'priority'}:
        ticket = int(value)
        await support_center.detail(user, ticket)
        if action == 'reply':
            text = await ask(event, 'أرسل رسالتك لإضافتها إلى التذكرة:')
            if text is None:
                return
            await support_center.reply(user, ticket, text)
        elif action == 'assign':
            text = await ask(event, 'أرسل آيدي مسؤول الدعم، أو آيديك لتولي التذكرة:')
            if text is None:
                return
            await support_center.change(user, ticket, action, int(text))
        elif action == 'priority':
            item = await support_center.detail(user, ticket)
            await support_center.change(user, ticket, action, 'normal' if item['priority'] == 'high' else 'high')
        else:
            await support_center.change(user, ticket, action)
        action, value = 'view', str(ticket)
    if action == 'view':
        parts = value.split(':')
        ticket, page = int(parts[0]), int(parts[1]) if len(parts) > 1 else 0
        item = await support_center.detail(user, ticket, page)
        text = f"التذكرة #{ticket}\nالحالة: {item['status']}\nالمسؤول: {item['assigned_to'] or 'لم يُعيّن'}\nالأولوية: {item['priority']}\n\n"
        text += '\n'.join(f'{"الدعم" if staff else "العميل"} · {created:%Y-%m-%d %H:%M}\n{message}'
                          for actor,message,staff,created in item['messages']) or 'لا توجد رسائل.'
        buttons = [[btn('إعادة فتح' if item['status'] == 'closed' else 'إغلاق التذكرة', 'tickets',
                        'reopen' if item['status'] == 'closed' else 'close', str(ticket))]]
        if item['status'] != 'closed':
            buttons.insert(0, [btn('إضافة رسالة', 'tickets', 'reply', str(ticket))])
        if item['staff']:
            buttons.append([btn('تعيين المسؤول', 'tickets', 'assign', str(ticket)),
                            btn('تبديل الأولوية', 'tickets', 'priority', str(ticket))])
        navigation = []
        if item['more']:
            navigation.append(btn('رسالة أقدم', 'tickets', 'view', f'{ticket}:{page+1}'))
        if page:
            navigation.append(btn('رسالة أحدث', 'tickets', 'view', f'{ticket}:{page-1}'))
        if navigation:
            buttons.append(navigation)
        buttons.append(back('feature:tickets:staff:0' if item['staff'] else 'feature:tickets:list:0'))
        return await event.edit(text, parse_mode=None, buttons=buttons)
    staff = action == 'staff'
    page = int(value)
    rows = await support_center.listing(user, page, staff=staff)
    buttons = [[btn(f'#{i} · {status} · {priority}', 'tickets', 'view', str(i))] for i,owner,status,assigned,priority,created in rows[:20]]
    buttons.insert(0, [btn('تذكرة جديدة', 'tickets', 'new')])
    nav = []
    if page:
        nav.append(btn('السابق', 'tickets', action, str(page-1)))
    if len(rows) > 20:
        nav.append(btn('التالي', 'tickets', action, str(page+1)))
    if nav:
        buttons.append(nav)
    buttons.append(back('admin_panel' if staff else 'main'))
    await event.edit('لوحة الدعم' if staff else 'تذاكر الدعم الخاصة بك', buttons=buttons)


async def staff_menu(event, action, value):
    if event.sender_id != ADMIN_ID:
        return await event.answer('إدارة المشرفين متاحة لصاحب البوت فقط.', alert=True)
    if action == 'audit':
        page = int(value)
        if page < 0:
            raise ValueError('صفحة غير صالحة.')
        rows = await query('''SELECT user_id,action,created_at FROM audit_log WHERE
            left(action,6)='staff_' OR left(action,13)='admin_action:' OR left(action,7)='coupon_'
            OR left(action,15)='inventory_cost:' OR left(action,16)='expense_created:' OR left(action,13)='loyalty_tier:'
            ORDER BY event_id DESC LIMIT 11 OFFSET ?''', (page*10,), fetchall=True) or []
        buttons = []
        if page:
            buttons.append([btn('السابق', 'staff', 'audit', str(page-1))])
        if len(rows)>10:
            buttons.append([btn('التالي', 'staff', 'audit', str(page+1))])
        buttons.append(back('feature:staff:list:0'))
        return await event.edit('سجل إدارة المشرفين والتعديلات\n\n' + ('\n'.join(f'{date:%Y-%m-%d %H:%M} · {actor}\n{action}'
            for actor,action,date in rows[:10]) or 'لا توجد تعديلات.'), parse_mode=None, buttons=buttons)
    if action == 'grant':
        if value not in staff_security.ROLES:
            raise ValueError('دور غير صالح.')
        raw = await ask(event, 'أرسل آيدي المستخدم لتعيين دور ' + staff_security.ROLE_NAMES[value] + ':')
        if raw is None:
            return
        target = int(raw)
        if target <= 0:
            raise ValueError('آيدي غير صالح.')
        await staff_security.set_role(event.sender_id, target, value)
    elif action == 'remove':
        target = int(value)
        if target == ADMIN_ID:
            raise ValueError('لا يمكن حذف صاحب البوت.')
        await staff_security.remove_role(event.sender_id,target)
    page = int(value) if action == 'list' else 0
    if page < 0:
        raise ValueError('صفحة غير صالحة.')
    rows = await query('SELECT r.user_id,r.role FROM staff_roles r JOIN admins a ON a.user_id=r.user_id ORDER BY r.user_id LIMIT 16 OFFSET ?',
                       (page*15,), fetchall=True) or []
    text = 'صلاحيات المشرفين\n\n' + ('\n'.join(f'{u}: {staff_security.ROLE_NAMES[r]}' for u,r in rows[:15]) or 'لا يوجد مشرفون.')
    buttons = [[btn('تعيين ' + label, 'staff', 'grant', role)] for role,label in staff_security.ROLE_NAMES.items()]
    buttons.insert(0, [btn('سجل التعديلات', 'staff', 'audit')])
    buttons += [[btn(f'إزالة {u}', 'staff', 'remove', str(u))] for u,r in rows[:15] if u != ADMIN_ID]
    if page:
        buttons.append([btn('السابق', 'staff', 'list', str(page-1))])
    if len(rows) > 15:
        buttons.append([btn('التالي', 'staff', 'list', str(page+1))])
    buttons.append(back('admin_panel'))
    await event.edit(text, parse_mode=None, buttons=buttons)


async def reports(event, action, period):
    if not await staff_security.require(event, 'finance'):
        return
    data = await business_reports.report(period)
    if action == 'export':
        return await event.client.send_file(event.sender_id, business_reports.report_csv(data), caption='مقارنة الفترتين؛ التكاليف غير المسجلة موضحة.')
    current, previous = data['current'], data['previous']
    text = (f"التقرير المالي · آخر {data['days']} يوم\n\n"
            f"مبيعات المخزون: {current['sales']}$\nالتأجير والتجديد: {current['rentals']}$\n"
            f"العمولات المسجلة: {current['fees']}$\nالإيرادات: {current['revenue']}$\n"
            f"تكلفة المخزون المباع المسجلة: {current['costs']}$\nالمصاريف: {current['expenses']}$\n"
            f"{'ربح محسوب جزئياً' if current['unknown_costs'] else 'الربح المسجل'}: {current['profit']}$\n"
            f"مبيعات بتكلفة غير معروفة: {current['unknown_costs']}\n\n"
            f"إيرادات الفترة السابقة: {previous['revenue']}$\nالتغير: {data['change']:+}$\n"
            'الربح يعتمد على التكاليف والمصاريف المدخلة. الإيداعات والتحويلات ليست إيرادات بيع.')
    buttons = [[btn(label, 'reports', 'view', key) for key,label in [('day','يومي'), ('week','أسبوعي')]],
               [btn(label, 'reports', 'view', key) for key,label in [('month','شهري'), ('quarter','ربع سنوي')]],
               [btn('تصدير CSV', 'reports', 'export', period)],
               [btn('تسجيل تكلفة مخزون', 'cost', 'new'), btn('تسجيل مصروف', 'expense', 'new')], back('admin_panel')]
    await event.edit(text, parse_mode=None, buttons=buttons)


async def schedule_menu(event, action, value):
    user = event.sender_id
    if action == 'new':
        raw = await ask(event, 'أرسل: دقائق حتى أول فحص | التكرار | اسم المجموعة\n'
                        'التكرار: مرة أو يومي أو أسبوعي. استخدم * لكل الحسابات.\nمثال: 60 | يومي | *')
        if raw is None:
            return
        minutes, recurrence, folder = (p.strip() for p in raw.split('|'))
        mapping = {'مرة': 0, 'يومي': 1440, 'أسبوعي': 10080}
        if recurrence not in mapping:
            raise ValueError('اختر مرة أو يومي أو أسبوعي.')
        await recurring_jobs.create(user, int(minutes), mapping[recurrence], '' if folder == '*' else folder)
    elif action == 'cancel':
        await recurring_jobs.cancel(user, int(value))
    page = int(value) if action == 'list' else 0
    if page < 0:
        raise ValueError('صفحة غير صالحة.')
    rows = await query('''SELECT schedule_id,due_at,status,interval_minutes,payload,run_count
        FROM scheduled_operations WHERE user_id=? ORDER BY schedule_id DESC LIMIT 16 OFFSET ?''',
        (user, page*15), fetchall=True) or []
    text = 'جدولة فحص الحسابات\n\n' + ('\n'.join(f'#{i} · {status} · {due:%Y-%m-%d %H:%M} · {"يومي" if interval == 1440 else "أسبوعي" if interval == 10080 else "مرة"} · مرات التنفيذ {runs}'
                                              for i,due,status,interval,payload,runs in rows[:15]) or 'لا توجد مهام.')
    buttons = [[btn('جدولة فحص جديد', 'schedule', 'new')]]
    buttons += [[btn(f'إيقاف #{i}', 'schedule', 'cancel', str(i))] for i,due,status,interval,payload,runs in rows[:15] if status in {'running','pending'}]
    if page:
        buttons.append([btn('السابق', 'schedule', 'list', str(page-1))])
    if len(rows) > 15:
        buttons.append([btn('التالي', 'schedule', 'list', str(page+1))])
    buttons.append(back('accounts_view'))
    await event.edit(text + '\nالتواريخ بتوقيت قاعدة البيانات. المجموعة تُراجع عند كل تنفيذ.', parse_mode=None, buttons=buttons)


async def feature_menu(event, data):
    if event.chat_id != event.sender_id:
        return await event.answer('استخدم المحادثة الخاصة.', alert=True)
    _, section, action, value = data.split(':', 3)
    try:
        handlers = {'shop': shop, 'loyalty': loyalty_menu, 'coupons': coupons, 'tickets': tickets,
                    'staff': staff_menu, 'reports': reports, 'schedule': schedule_menu}
        if section in handlers:
            return await handlers[section](event, action, value)
        if section == 'statement':
            return await event.client.send_file(event.sender_id, await business_reports.statement_csv(event.sender_id, value))
        if section in {'cost', 'expense'}:
            if not await staff_security.require(event, 'finance'):
                return
            prompt = 'أرسل: رقم الهاتف | تكلفة الشراء بالدولار' if section == 'cost' else 'أرسل: المبلغ بالدولار | وصف المصروف'
            raw = await ask(event, prompt)
            if raw is None:
                return
            first, second = (p.strip() for p in raw.split('|', 1))
            if section == 'cost':
                await business_reports.record_cost(event.sender_id, first.lstrip('+'), second)
            else:
                await business_reports.record_expense(event.sender_id, first, second)
            return await reports(event, 'view', 'month')
        if section == 'miniapp':
            from miniapp_server import public_url
            url = public_url()
            buttons = [back()]
            return await event.edit('واجهة المتجر متاحة من زر open بجانب حقل الكتابة.' if url else
                                    'رابط الموقع يتحدد تلقائياً عند التشغيل كخدمة Web على Render.',
                                    buttons=buttons)
        raise ValueError('قسم غير صالح.')
    except (ValueError, ArithmeticError) as exc:
        await event.answer(str(exc)[:180] or 'راجع القيم وحاول مجدداً.', alert=True)
