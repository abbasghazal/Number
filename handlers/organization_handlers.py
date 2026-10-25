"""Labels are private metadata, never edits to the Telegram account profile."""
import csv
import json
import os
import tempfile
import account_health
from keyboards import Button
from utils.privacy import mask_phone
from workflow import query
from .platform_handlers import ask, back

_filters = {}


def _csv_safe(value):
    text = '' if value is None else str(value)
    probe = text.lstrip(" \t\r\n\u00a0\u200b\ufeff")
    return "'" + text if probe.startswith(('=', '+', '-', '@')) else text


async def account_inventory(user_id):
    accounts = await account_health.accounts_for(user_id)
    states = await account_health.records(accounts)
    labels = await query('SELECT phone,label,folder,tags FROM account_labels WHERE user_id=?', (user_id,), fetchall=True)
    labels = {str(p): (label, folder, tags) for p, label, folder, tags in labels or []}
    sources = await query('''SELECT phone,'owned' FROM stored_accounts WHERE user_id=?
        UNION SELECT phone,'owned' FROM purchased_accounts WHERE user_id=?
        UNION SELECT phone,'rented' FROM rental_leases WHERE renter_id=? AND status='active' AND ends_at>CURRENT_TIMESTAMP''',
        (user_id,user_id,user_id), fetchall=True)
    roles = {}
    for phone, role in sources or []:
        roles.setdefault(str(phone), set()).add(role)
    countries = await query('SELECT calling_code,name FROM countries ORDER BY length(calling_code) DESC', fetchall=True)
    result = []
    for key, account in accounts.items():
        state = states.get(key, {})
        detail = state.get('details', {})
        country_code, country = next(
            ((str(code).lstrip('+'), name) for code, name in countries or []
             if account.phone.lstrip('+').startswith(str(code).lstrip('+'))),
            ('unknown', 'غير محددة'),
        )
        label, folder, tags = labels.get(account.phone, ('', '', ''))
        result.append(dict(key=key, phone=account.phone, label=label, folder=folder,
                           username=detail.get('username') or '', name=detail.get('name') or '',
                           status=state.get('status','unknown'), country=country, country_code=country_code,
                           tags=tags or '', roles=roles.get(account.phone, {'inventory'})))
    return result


def matches(item, filters):
    search = filters.get('search','').casefold().lstrip('@+')
    haystack = ' '.join(str(item.get(k, '')) for k in ('phone','username','name','label','tags')).casefold()
    country = filters.get('country','').casefold().lstrip('+')
    return (not search or search in haystack) and (
        not country or country == str(item.get('country_code', '')).casefold()
        or (not item.get('country_code') and item['phone'].lstrip('+').startswith(country))
    ) and (not filters.get('folder') or filters['folder'].casefold() == item['folder'].casefold()) and (
        filters.get('state','all') == 'all' or filters['state'] == item['status']
    ) and (filters.get('ownership','all') == 'all' or filters['ownership'] in item['roles'])


async def organization_menu(event, action, value):
    user = event.sender_id
    if len(_filters) > 1000:
        _filters.pop(next(iter(_filters)))
    filters = _filters.setdefault(user, {})
    if action == 'dashboard':
        accounts = await account_inventory(user)
        countries = {}
        statuses = {}
        roles = {'owned': 0, 'rented': 0, 'inventory': 0}
        for account in accounts:
            countries[account['country']] = countries.get(account['country'], 0) + 1
            statuses[account['status']] = statuses.get(account['status'], 0) + 1
            for role in account['roles']:
                roles[role] = roles.get(role, 0) + 1
        state_text = ' · '.join(f"{account_health.LABELS.get(key, key)}: {count}" for key, count in statuses.items()) or 'لا توجد حسابات'
        country_text = '\n'.join(f'• {name}: {count}' for name, count in sorted(countries.items())) or '—'
        return await event.edit(
            f'📊 لوحة الحسابات المتقدمة\n\nإجمالي الحسابات المتاحة لك: {len(accounts)}\n'
            f'مملوكة: {roles["owned"]} · مستأجرة: {roles["rented"]} · مخزون: {roles["inventory"]}\n\n'
            f'الحالات: {state_text}\n\nالحسابات حسب الدولة:\n{country_text}', parse_mode=None,
            buttons=[[Button.inline('تصدير الحسابات الظاهرة CSV', data='ops:accounts:export:0')],
                     [back('ops:accounts:list:0')[0]]])
    if action in {'search','folder'}:
        prompts = {'search': 'أرسل الرقم أو اليوزر أو الاسم الداخلي للبحث. أرسل - لإزالة البحث.',
                   'folder': 'أرسل اسم المجموعة الداخلية، أو - لعرض كل المجموعات.'}
        text = await ask(event, prompts[action])
        if text is None:
            return
        if len(text) > 64:
            raise ValueError('النص طويل')
        filters[action] = '' if text.strip() == '-' else text.strip()
        action, value = 'list', '0'
    accounts = await account_inventory(user)
    if action == 'country':
        # Countries are chosen from actual stored accounts, not typed manually.
        if value == '0':
            grouped = {}
            for account in accounts:
                grouped.setdefault(account['country_code'], [account['country'], 0])[1] += 1
            buttons = []
            for code, (name, count) in sorted(grouped.items(), key=lambda item: item[1][0]):
                marker = ' ✓' if filters.get('country') == code else ''
                buttons.append(Button.inline(f'{name} ({count}){marker}', data=f'ops:accounts:country:{code}'))
            rows = [buttons[index:index + 2] for index in range(0, len(buttons), 2)]
            rows += [[Button.inline('كل الدول' + (' ✓' if not filters.get('country') else ''), data='ops:accounts:country:all')],
                     [back('ops:accounts:list:0')[0]]]
            return await event.edit('اختر الدولة لعرض حساباتها فقط:', buttons=rows)
        filters['country'] = '' if value == 'all' else value.lstrip('+')
        action, value = 'list', '0'
    if action == 'state':
        if value not in account_health.LABELS and value != 'all':
            raise ValueError('حالة غير صالحة')
        filters['state'] = value
        action, value = 'list', '0'
    if action == 'ownership':
        if value not in {'all','owned','rented','inventory'}:
            raise ValueError('ملكية غير صالحة')
        filters['ownership'] = value
        action, value = 'list', '0'
    if action == 'reset':
        filters.clear()
        action, value = 'list', '0'
    if action == 'export':
        accounts = [account for account in await account_inventory(user) if matches(account, filters)]
        handle = tempfile.NamedTemporaryFile(prefix='accounts-', suffix='.csv', delete=False, mode='w', newline='', encoding='utf-8')
        try:
            writer = csv.DictWriter(handle, fieldnames=['phone', 'country', 'status', 'ownership', 'label', 'folder', 'tags', 'username', 'name'])
            writer.writeheader()
            for account in accounts:
                writer.writerow({key: _csv_safe(value) for key, value in {
                                 'phone': account['phone'], 'country': account['country'],
                                 'status': account_health.LABELS.get(account['status'], account['status']),
                                 'ownership': ', '.join(sorted(account['roles'])), 'label': account['label'],
                                 'folder': account['folder'], 'tags': account['tags'],
                                 'username': account['username'], 'name': account['name']}.items()})
            handle.close()
            await event.client.send_file(user, handle.name, caption=f'📄 تصدير آمن لـ {len(accounts)} حساب. لا يحتوي جلسات أو كلمات مرور.')
            return await event.answer('✅ تم إرسال ملف CSV.', alert=True)
        finally:
            if not handle.closed:
                handle.close()
            with __import__('contextlib').suppress(OSError):
                os.unlink(handle.name)
    if action == 'edit':
        matched = [a for a in accounts if a['key'].startswith(value)]
        if len(matched) != 1:
            return await event.answer('الحساب غير متاح لك.', alert=True)
        account = matched[0]
        text = await ask(event, 'أرسل: الاسم | المجموعة | الوسوم\nمثال: أساسي | العراق | مميز،قديم\nأرسل - لمسح البيانات. لن يتغير اسم الحساب في تيليجرام.')
        if text is None:
            return
        label, _, remainder = text.partition('|')
        folder, _, tags = remainder.partition('|')
        label, folder = label.strip(), folder.strip()
        tags = ','.join(dict.fromkeys(tag.strip() for tag in tags.split(',') if tag.strip()))
        if text.strip() == '-':
            label = folder = tags = ''
        if len(label) > 60 or len(folder) > 40 or len(tags) > 160:
            raise ValueError('الاسم أو المجموعة طويل')
        if account['key'] not in await account_health.accounts_for(user):
            return await event.respond('لم يعد الحساب متاحًا لك.')
        await query('''INSERT INTO account_labels(user_id,phone,label,folder,tags) VALUES(?,?,?,?,?)
            ON CONFLICT(user_id,phone) DO UPDATE SET label=excluded.label,folder=excluded.folder,tags=excluded.tags''',
            (user,account['phone'],label,folder,tags), commit=True)
        await event.respond('تم حفظ التنظيم الداخلي.', buttons=[back('ops:accounts:list:0')])
        return
    page = max(0, min(10000, int(value)))
    filtered = sorted([a for a in accounts if matches(a,filters)], key=lambda a:(a['folder'],a['label'],a['phone']))
    filter_names = {'search':'بحث','country':'الدولة','folder':'المجموعة','state':'الحالة','ownership':'الملكية'}
    role_names = {'owned':'مملوكة','rented':'مستأجرة','inventory':'مخزون الإدارة'}
    selected = []
    for key, choice in filters.items():
        if choice and choice != 'all':
            if key == 'country':
                label = next((a['country'] for a in accounts if a['country_code'] == choice), choice)
            else:
                label = account_health.LABELS.get(choice,choice) if key == 'state' else role_names.get(choice,choice) if key == 'ownership' else choice
            selected.append(f'{filter_names[key]}: {label}')
    text = f'تنظيم الحسابات · {len(filtered)} نتيجة\n{" · ".join(selected) or "كل الحسابات المتاحة"}\nالدولة حسب مفتاح الرقم.\n\n'
    buttons = []
    for account in filtered[page*6:(page+1)*6]:
        text += (f'{mask_phone(account["phone"])} · {account["label"] or account["name"] or "بلا اسم"}\n'
                 f'@{account["username"] or "—"} · {account["country"]} · {account["folder"] or "دون مجموعة"}\n'
                 f'الوسوم: {account["tags"] or "—"}\n'
                 f'{account_health.LABELS[account["status"]]}\n\n')
        buttons.append([Button.inline(f'تسمية وتنظيم {mask_phone(account["phone"])}', data=f'ops:accounts:edit:{account["key"][:20]}')])
    if page:
        buttons.append([Button.inline('السابق', data=f'ops:accounts:list:{page-1}')])
    if len(filtered) > (page+1)*6:
        buttons.append([Button.inline('التالي', data=f'ops:accounts:list:{page+1}')])
    buttons += [[Button.inline('📊 لوحة الإحصاءات',data='ops:accounts:dashboard:0'), Button.inline('تصدير CSV',data='ops:accounts:export:0')],
                [Button.inline('بحث' + (' ✓' if filters.get('search') else ''),data='ops:accounts:search:0'),
                 Button.inline('الدولة' + (' ✓' if filters.get('country') else ''),data='ops:accounts:country:0')],
                [Button.inline('المجموعة' + (' ✓' if filters.get('folder') else ''),data='ops:accounts:folder:0'),
                 Button.inline('مسح المرشحات',data='ops:accounts:reset:0')]]
    for state, label in [('healthy','صالح'),('revoked','جلسة منتهية'),('banned','محظور'),('restricted','مقيّد'),('deleted','محذوف'),('invalid','جلسة غير صالحة'),('mismatch','رقم غير مطابق'),('unknown','لم يُفحص'),('all','كل الحالات')]:
        buttons.append([Button.inline(label + (' ✓' if filters.get('state', 'all') == state else ''),data=f'ops:accounts:state:{state}')])
    buttons += [[Button.inline(label + (' ✓' if filters.get('ownership', 'all') == role else ''),data=f'ops:accounts:ownership:{role}') for role,label in [('owned','مملوكة'),('rented','مستأجرة'),('all','كل الملكيات')]],
                [back('accounts_view')[0]]]
    await event.edit(text, parse_mode=None, buttons=buttons)
