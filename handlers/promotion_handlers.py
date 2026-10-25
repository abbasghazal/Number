"""Inline, colored panels for paid services, point funding and code monitoring."""

import asyncio
from contextlib import asynccontextmanager

from telethon import errors

import account_access
import code_monitor
import promotion_service as service
import promotion_store as store
from keyboards import Button
from utils.asyncio_compat import to_thread
from workflow import query
from .base import active_conversations
from .input_choices import choose, choose_accounts, confirm, preset, read

STATES = {'quote': 'بانتظار التأكيد', 'running': 'قيد التنفيذ', 'completed': 'مكتمل', 'partial': 'مكتمل جزئيًا',
          'review': 'بانتظار حسم النتائج', 'cancelled': 'متوقف', 'expired': 'منتهي'}
KINDS = {'members': 'أعضاء قناة أو مجموعة', 'reactions': 'تفاعلات منشور', 'search':'أعضاء عبر البحث'}


def btn(label, data, style='primary'):
    return Button.inline(label, data=data, style=style)


@asynccontextmanager
async def conversation(event):
    mark = getattr(event, 'mark_input_started', None)
    if mark:
        mark()
    async with event.client.conversation(event.chat_id) as conv:
        active_conversations[event.chat_id] = conv
        try:
            yield conv
        finally:
            if active_conversations.get(event.chat_id) is conv:
                active_conversations.pop(event.chat_id, None)


async def target_wizard(conv,event,kind):
    raw=await read(conv,'أضف البوت مشرفًا في هدف التمويل بالنقاط، ثم أرسل @معرفه أو معرفه الرقمي -100…:')
    return await service.validate_target(event.client,event.chat_id,raw,kind)


async def paid_target_wizard(conv, event, kind):
    prompt = 'أرسل @معرف القناة أو المجموعة أو رابط دعوة الهدف الخاص. الرشق المدفوع لا يحتاج البوت مشرفًا:' if kind in ('members','search') else 'أرسل رابط المنشور. الرشق المدفوع لا يحتاج البوت مشرفًا:'
    raw=await read(conv,prompt)
    try:
        return await service.validate_paid_target(event.client,event.chat_id,raw,kind)
    except service.PaidInviteRequired:
        invite=await read(conv,'أرسل رابط دعوة الهدف الخاص حتى تتحقق حسابات التنفيذ من الوصول إليه:')
        return await service.validate_paid_target(event.client,event.chat_id,raw,kind,invite=invite)


async def paid_wizard(event, kind):
    async with conversation(event) as conv:
        target = await paid_target_wizard(conv, event, kind)
        available = await service.pool()
        if not available:
            raise ValueError('لا توجد حسابات مشاركة متاحة بالخدمة حاليًا.')
        quantity = int(await preset(conv, f'العدد المطلوب من 1 إلى {min(1000,len(available))}:', [1, 10, 25, 50, 100]))
        if not 1 <= quantity <= min(1000, len(available)):
            raise ValueError('العدد أكبر من الحسابات المتاحة أو خارج النطاق.')
        emoji = ''
        if kind == 'reactions':
            emoji = await choose(conv, 'اختر التفاعل:', list(zip(service.EMOJIS, ['إعجاب', 'قلب', 'نار', 'تصفيق', 'احتفال', 'حب', 'ابتسامة', 'انبهار'])))
        phrase = await read(conv,'أرسل عبارة البحث التي يظهر بها الهدف:') if kind=='search' else ''
        key, cost = await to_thread(store.quote, event.chat_id, kind, target, quantity, emoji, phrase)
        if not await confirm(conv, f"معاينة {KINDS[kind]}\nالهدف: {target['title']}\nالعدد: {quantity}\nالتفاعل: {emoji or '—'}\nالتكلفة: {cost}$\nيُخصم المبلغ عند التأكيد ويُسترد مقابل غير المنفذ. النتائج غير المحسومة تبقى للمراجعة. تأكيد الطلب؟"):
            return await conv.send_message('أُلغي التأكيد؛ لم يُخصم الرصيد.', parse_mode=None)
        # Recheck target ownership and availability after the user's review.
        await service.revalidate_paid_target(event.client,event.chat_id,kind,target)
        status = await to_thread(store.accept, event.chat_id, key, await service.pool())
        await service.dispatch()
        await conv.send_message(f'الطلب {key} · {STATES.get(status,status)}', parse_mode=None, buttons=[[btn('متابعة الطلب', f'promo:view:{key}')]])


async def pool_wizard(event):
    accounts = await service.pool()
    return await event.respond(
        f'مصادر الرشق المدفوع مركزية ويشغّلها المطور فقط. الحسابات الجاهزة الآن: {len(accounts)}.\n'
        'تُستخدم نفس مصادر التجميع الدوري، ولا يمكن للمستخدمين تشغيلها أو إيقافها أو إضافة حساباتهم.',
        parse_mode=None,
        buttons=[[btn('رجوع', 'promo:menu')]],
    )


async def paid_menu(event, action, value):
    user = event.chat_id
    if action in KINDS:
        return await paid_wizard(event, action)
    if action == 'pool':
        return await pool_wizard(event)
    if action == 'poolstop':
        raise ValueError('لا يمكن للمستخدمين إيقاف مصادر الرشق المدفوع؛ يتحكم المطور بالتنفيذ المركزي.')
    if action == 'cancel':
        await to_thread(store.cancel_order,user,value)
        await service.dispatch()
        return await event.respond('طُلب إيقاف الطلب. تُحفظ النتائج المنجزة وتُسترد تكلفة غير المنفذ بعد التسوية.', parse_mode=None)
    if action == 'view':
        row = await query("SELECT kind,title,quantity,charge,refunded,status FROM promotion_orders WHERE order_id=? AND user_id=?", (value, user), fetchone=True)
        if not row:
            raise ValueError('الطلب غير موجود ضمن حسابك.')
        counts = dict(await query("SELECT status,COUNT(*) FROM promotion_items WHERE order_id=? GROUP BY status", (value,), fetchall=True) or [])
        kind, title, quantity, charge, refund, status = row
        return await event.edit(f"طلب {value}\n{KINDS[kind]} · {title}\nالحالة: {STATES[status]}\nالمطلوب: {quantity} · المنجز: {counts.get('success',0)} · غير المحسوم: {counts.get('unknown',0)}\nالمخصوم: {charge}$ · المسترد: {refund}$\nالأعداد هي نتائج التنفيذ المؤكدة، ولا يوجد ضمان لبقاء الأعضاء.", parse_mode=None,
            buttons=[[btn('تحديث',f'promo:view:{value}')], [btn('إيقاف الطلب',f'promo:cancel:{value}','danger')] if status == 'running' else [], [btn('رجوع','promo:orders')]])
    if action == 'orders':
        rows = await query("SELECT order_id,kind,status FROM promotion_orders WHERE user_id=? ORDER BY created_at DESC LIMIT 20", (user,), fetchall=True) or []
        return await event.edit('آخر طلباتك' if rows else 'لا توجد طلبات.', buttons=[[btn(f'{KINDS[kind]} · {STATES[state]}',f'promo:view:{key}')] for key,kind,state in rows]+[[btn('رجوع','promo:menu')]])
    if action == 'admin':
        store.developer(user)
        return await event.edit('إدارة الرشق والتمويل', buttons=[
            [btn('تسعير الأعضاء','promo:price:members'),btn('تسعير التفاعلات','promo:price:reactions')],
            [btn('تسعير البحث','promo:price:search'),btn('حدود التنفيذ','promo:execution')],
            [btn('مصادر التنفيذ المركزية','promo:pool')], [btn('إعدادات النقاط','fund:config'),btn('إضافة نقاط','fund:grant','success')],
            [btn('طلبات غير محسومة','promo:reviews'),btn('مراقبة الأكواد','codes:menu')], [btn('رجوع','developer_menu')]])
    if action == 'execution':
        store.developer(user)
        async with conversation(event) as conv:
            names=('max_orders','members_limit','reactions_limit','search_limit','delay_seconds','error_limit','cooldown_seconds','attempt_multiplier')
            labels=('عدد الطلبات المتزامنة 1–20','تزامن الأعضاء 1–10','تزامن التفاعلات 1–10','تزامن البحث 1–10','الفاصل بالثواني 1–60','حد الأخطاء 1–100','التهدئة بالثواني 30–86400','مضاعف المحاولات 1–10')
            data={key:int(await read(conv,label+':')) for key,label in zip(names,labels)}
            data['enabled']=await choose(conv,'حالة الطابور:',[(True,'تفعيل'),(False,'تعطيل')])
            if await confirm(conv,'حفظ حدود التنفيذ الجديدة؟'):
                await to_thread(store.configure_execution,user,data)
                await conv.send_message('حُفظت حدود التنفيذ.',parse_mode=None)
        return
    if action == 'price':
        store.developer(user)
        if value not in KINDS:
            raise ValueError('نوع الخدمة غير صالح.')
        current = await query('SELECT unit_price,enabled FROM promotion_prices WHERE kind=?',(value,),fetchone=True)
        async with conversation(event) as conv:
            rate = await read(conv, f'{KINDS[value]} · السعر الحالي {current[0]}$ للوحدة. أدخل سعر الوحدة الجديد بالدولار:')
            enabled = await choose(conv, 'حالة الخدمة:', [(True,'تفعيل الخدمة'),(False,'تعطيل الخدمة')])
            if await confirm(conv, f'السعر {rate}$ للوحدة. حفظ الإعدادات؟'):
                await to_thread(store.price,user,value,rate,enabled)
                await conv.send_message('حُفظ السعر وحالة الخدمة.',parse_mode=None)
        return
    if action == 'reviews':
        store.developer(user)
        rows = await query("SELECT order_id,user_id FROM promotion_orders WHERE status='review' ORDER BY created_at LIMIT 20",fetchall=True) or []
        return await event.edit('نتائج غير محسومة' if rows else 'لا توجد طلبات للمراجعة.',buttons=[[btn(f'{key} · المستخدم {owner}',f'promo:review:{key}')] for key,owner in rows]+[[btn('رجوع','promo:admin')]])
    if action in {'review','recheck','release'}:
        store.developer(user)
        row = await query("SELECT user_id,charge,refunded FROM promotion_orders WHERE order_id=? AND status='review'",(value,),fetchone=True)
        if not row:
            raise ValueError('الطلب غير متاح للمراجعة.')
        if action == 'recheck':
            await service.dispatch()
            return await event.respond('جُدولت إعادة قراءة النتيجة دون إعادة إرسال الاشتراك أو التفاعل.',parse_mode=None)
        if action == 'release':
            async with conversation(event) as conv:
                if await confirm(conv, 'استرداد تكلفة النتائج غير المحسومة لهذا الطلب؟ قد تكون بعض العمليات وصلت إلى تيليجرام. يُسجل القرار باسمك.'):
                    await to_thread(store.release_unknown,user,value)
                    await conv.send_message('سُجل القرار وسُويت تكلفة الطلب.',parse_mode=None)
            return
        return await event.edit(f'مراجعة {value}\nالمستخدم: {row[0]}\nالخصم {row[1]}$ · المسترد {row[2]}$',buttons=[[btn('إعادة التحقق',f'promo:recheck:{value}')],[btn('استرداد غير المحسوم',f'promo:release:{value}','danger')],[btn('رجوع','promo:reviews')]])
    if action != 'menu':
        raise ValueError('العملية غير صالحة.')
    rates = await query('SELECT kind,unit_price,enabled FROM promotion_prices ORDER BY kind',fetchall=True) or []
    text = 'الرشق المدفوع\n\n'+'\n'.join(f'{KINDS[kind]}: {rate}$ للوحدة · {"مفعّل" if enabled else "متوقف"}' for kind,rate,enabled in rates)
    return await event.edit(text,parse_mode=None,buttons=[[btn('أعضاء قناة أو مجموعة','promo:members','success')],[btn('تفاعلات منشور','promo:reactions','success')],[btn('أعضاء عبر البحث','promo:search','success')],[btn('طلباتي','promo:orders')], [btn('تمويل بالنقاط','fund:menu')],[btn('رجوع','main')]])


async def funding_task(event):
    user = event.chat_id
    existing = await query("SELECT t.task_id,c.target,c.title,c.reward FROM funding_tasks t JOIN funding_campaigns c ON c.campaign_id=t.campaign_id WHERE t.user_id=? AND t.status='reserved' AND t.expires_at>CURRENT_TIMESTAMP AND c.status IN ('active','pausing') ORDER BY t.expires_at LIMIT 1",(user,),fetchone=True)
    task = existing
    if not task:
        rows = await query("SELECT campaign_id,target_id,target,title,reward FROM funding_campaigns c WHERE status='active' AND delivered<quantity AND user_id<>? AND NOT EXISTS(SELECT 1 FROM funding_memberships m WHERE m.target_id=c.target_id AND m.user_id=?) ORDER BY campaign_id LIMIT 30",(user,user),fetchall=True) or []
        for key,target_id,link,title,reward in rows:
            if await service.is_member(event.client,target_id,user):
                continue
            try:
                task_id = await to_thread(store.reserve_task,user,key)
            except ValueError:
                continue
            task = task_id,link,title,reward
            break
    if not task:
        return await event.edit('لا توجد مهام اشتراك جديدة متاحة حاليًا.',buttons=[[btn('تحديث','fund:collect')],[btn('رجوع','fund:menu')]])
    key,link,title,reward = task
    policy=await query('SELECT penalty_multiplier FROM funding_tasks WHERE task_id=?',(key,),fetchone=True)
    terms=f'تُراقب العضوية بعد الدفع، وعند رصد المغادرة يُخصم {reward*2} نقطة مرة واحدة، حتى إن أصبح رصيدك سالبًا.' if policy[0]==2 else 'تحتفظ هذه المهمة بشروطها السابقة.'
    return await event.edit(f'اشترك في {title} ثم اضغط تحقق.\nالمكافأة: {reward} نقطة فور التحقق · المهلة 10 دقائق.\n{terms}\nلا تُمنح مكافأة للعضوية السابقة أو لنفس الهدف أكثر من مرة.',parse_mode=None,buttons=[[Button.url('الانضمام إلى الهدف',link,style='primary')],[btn('تحقق واحصل على النقاط',f'fund:verify:{key}','success')],[btn('رجوع','fund:menu')]])


async def funding_menu(event, action, value):
    user = event.chat_id
    if action in {'auto_setup_all', 'auto_setup', 'auto_stop'}:
        raise ValueError('التجميع الدوري مركزي ويشغّله أو يوقفه المطور من إعدادات الإدارة فقط.')
    if action == 'auto':
        import funding_auto_collect as auto_collect
        global_cfg = await auto_collect.global_status()
        text = (
            f"التجميع الدوري للنقاط\nالحالة: {'مفعّل' if global_cfg[0] else 'متوقف'}\n\n"
            "يدير المطور هذه المهمة مركزيًا. عند تشغيلها تستخدم حسابات المطور، "
            "وحسابات المخزون المعروضة للبيع، والحسابات المعروضة في المزاد. "
            "يفحص النظام الحملات غير المكتملة فقط ويحجز مكانًا قبل الانضمام."
        )
        return await event.edit(text, parse_mode=None, buttons=[[btn('رجوع','fund:menu')]])
    if action == 'collect':
        cfg = await query('SELECT enabled FROM funding_config WHERE id=1',fetchone=True)
        if not cfg or not cfg[0]:
            raise ValueError('خدمة النقاط متوقفة حاليًا.')
        return await funding_task(event)
    if action == 'verify':
        from funding_service import verify
        result=await verify(event.client,user,value)
        text=f"أضيفت {result['reward']} نقطة فورًا." if result['reward'] else 'حُسبت مكافأة هذه المهمة مسبقًا.'
        return await event.edit(text,buttons=[[btn('مهمة أخرى','fund:collect','success')],[btn('رجوع','fund:menu')]])
    if action == 'new':
        async with conversation(event) as conv:
            target = await target_wizard(conv,event,'members')
            quantity = int(await preset(conv,'عدد المشتركين المطلوب:',[10,50,100,500]))
            reward,cost,enabled,minimum,maximum,guarantee = await query('SELECT reward,cost,enabled,minimum,maximum,guarantee_seconds FROM funding_config WHERE id=1',fetchone=True)
            if not enabled:
                raise ValueError('خدمة النقاط متوقفة حاليًا.')
            if not minimum<=quantity<=maximum:
                raise ValueError(f'اختر عددًا بين {minimum} و{maximum}.')
            if await confirm(conv,f"تمويل {target['title']}\nالمطلوب {quantity} مشترك\nالتكلفة {cost*quantity} نقطة ({cost} لكل مشترك)\nمكافأة المنفذ {reward} نقطة فورية؛ يُخصم ضعفها عند رصد المغادرة لاحقًا. تأكيد التمويل؟"):
                await service.validate_target(event.client,user,str(target['id']),'members')
                key = await to_thread(store.campaign,user,target,quantity,cost,reward,expected_guarantee=guarantee)
                await conv.send_message(f'أُنشئ التمويل #{key}. سيظهر للمستخدمين في مهام جمع النقاط.',parse_mode=None,buttons=[[btn('تمويلاتي','fund:campaigns')]])
        return
    if action == 'cancel':
        amount = await to_thread(store.cancel_campaign,user,int(value))
        return await event.respond(f'أُعيدت {amount} نقطة. المهام المحجوزة والمكافآت المعلقة محفوظة حتى حسمها، ثم يُسترد الباقي تلقائيًا.',parse_mode=None)
    if action == 'increase':
        row=await query("SELECT title,cost FROM funding_campaigns WHERE campaign_id=? AND user_id=? AND status='active'",(int(value),user),fetchone=True)
        if not row:
            raise ValueError('الحملة غير متاحة ضمن حسابك.')
        async with conversation(event) as conv:
            extra=int(await preset(conv,'عدد المشتركين الإضافي:',[10,50,100]))
            if await confirm(conv,f'زيادة تمويل {row[0]} بمقدار {extra} مقابل {extra*row[1]} نقطة؟'):
                import secrets
                await to_thread(store.increase_campaign,user,int(value),extra,secrets.token_hex(16))
                await conv.send_message('زِيد التمويل وسُجل الخصم.',parse_mode=None)
        return
    if action == 'campaigns':
        rows = await query('SELECT campaign_id,title,quantity,delivered,status FROM funding_campaigns WHERE user_id=? ORDER BY campaign_id DESC LIMIT 20',(user,),fetchall=True) or []
        labels={'active':'نشط','pausing':'تسوية المهام المحجوزة','completed':'مكتمل','cancelled':'متوقف'}
        text = 'تمويلاتي\n'+'\n'.join(f'#{key} · {title} · {done}/{qty} · {labels[status]}' for key,title,qty,done,status in rows)
        return await event.edit(text,parse_mode=None,buttons=[[btn(f'زيادة #{key}',f'fund:increase:{key}'),btn(f'إيقاف #{key}',f'fund:cancel:{key}','danger')] for key,_,_,_,status in rows if status=='active']+[[btn('رجوع','fund:menu')]])
    if action == 'ledger':
        rows = await query('SELECT amount,balance_after,operation,created_at FROM funding_ledger WHERE user_id=? ORDER BY entry_id DESC LIMIT 20',(user,),fetchall=True) or []
        labels = {'developer_grant':'إضافة المطور','campaign_purchase':'تمويل هدف','task_reward':'مكافأة اشتراك','campaign_refund':'استرداد تمويل','membership_penalty':'خصم مغادرة مضاعف','auto_task_reward':'مكافأة تجميع تلقائي','auto_membership_penalty':'خصم مغادرة تلقائي','campaign_increase':'زيادة تمويل'}
        return await event.edit('حركات النقاط\n'+'\n'.join(f'{date}: {labels.get(kind,kind)} · {amount:+d} · الرصيد {balance}' for amount,balance,kind,date in rows),parse_mode=None,buttons=[[btn('رجوع','fund:menu')]])
    if action in {'grant','config'}:
        store.developer(user)
        async with conversation(event) as conv:
            if action == 'grant':
                recipient = int(await read(conv,'معرف المستخدم المسجل الذي تريد إضافة نقاط له:'))
                amount = int(await preset(conv,'عدد النقاط المراد إضافتها:',[100,1000,10000]))
                if await confirm(conv,f'إضافة {amount} نقطة إلى {recipient}؟'):
                    balance = await to_thread(store.grant_points,user,recipient,amount)
                    await conv.send_message(f'أضيفت النقاط. الرصيد الجديد {balance}.',parse_mode=None)
            else:
                reward = int(await preset(conv,'مكافأة الاشتراك المنفذ بالنقاط:',[5,10,20]))
                cost = int(await preset(conv,'تكلفة المشترك على الممول بالنقاط؛ لا تقل عن المكافأة:',[10,15,30]))
                enabled = await choose(conv,'حالة النقاط:',[(True,'تفعيل'),(False,'تعطيل')])
                minimum=int(await preset(conv,'الحد الأدنى للحملة:',[1,10,50]))
                maximum=int(await preset(conv,'الحد الأقصى للحملة:',[1000,10000,100000]))
                if await confirm(conv,f'المكافأة الفورية {reward} والتكلفة {cost}. الحدود {minimum}–{maximum}. خصم المغادرة ضعف المكافأة. حفظ؟'):
                    await to_thread(store.configure_points,user,reward,cost,enabled,minimum=minimum,maximum=maximum)
                    await conv.send_message('حُفظت إعدادات النقاط. الحملات السابقة تحتفظ بأسعارها.',parse_mode=None)
        return
    if action != 'menu':
        raise ValueError('العملية غير صالحة.')
    balance = await query('SELECT points FROM funding_wallets WHERE user_id=?',(user,),fetchone=True)
    reward,cost,enabled = await query('SELECT reward,cost,enabled FROM funding_config WHERE id=1',fetchone=True)
    return await event.edit(f"تمويل القنوات والمجموعات بالنقاط\nرصيدك: {balance[0] if balance else 0} نقطة\nمكافأة الاشتراك {reward} فورية · تكلفة التمويل للمشترك {cost}\nعند رصد مغادرة هدف حصلت على مكافأته الجديدة يُخصم ضعف المكافأة مرة واحدة، وقد يصبح الرصيد سالبًا.\nالحالة: {'مفعّل' if enabled else 'متوقف'}\nالنقاط مستقلة عن رصيد المحفظة بالدولار.",parse_mode=None,buttons=[[btn('جمع نقاط بالاشتراك','fund:collect','success'),btn('إعدادات التجميع الدوري','fund:auto','success')],[btn('تمويل قناتي أو مجموعتي','fund:new','success')],[btn('تمويلاتي','fund:campaigns'),btn('حركات النقاط','fund:ledger')],[btn('رجوع','main')]])


async def codes_menu(event, action, value):
    user = event.chat_id
    if action == 'stop':
        await code_monitor.configure(user,None,False)
        await code_monitor.stop(user)
        return await event.respond('أُوقفت مراقبة الأكواد ومحاولاتها. الأكواد التي بدأ إرسالها لا تتكرر تلقائيًا.',parse_mode=None)
    if action == 'enable':
        await code_monitor.configure(user,None,True)
        return await event.respond('فُعلت مراقبة جميع قنوات المصادر المضافة.', parse_mode=None, buttons=[[btn('رجوع','codes:menu')]])
    if action == 'source':
        accounts = await account_access.accounts(user,owned_only=True)
        async with conversation(event) as conv:
            raw = await read(conv,'أرسل @معرف قناة المراقبة أو رابط t.me العام أو المعرف الرقمي -100…:')
            phone = await choose(conv,'اختر حسابًا تملكه لقراءة هذه القناة:',[(a[0],'+'+a[0]) for a in accounts])
            if await confirm(conv,f'إضافة {raw} كمصدر مراقبة باستخدام +{phone}؟'):
                source = await code_monitor.add_source(user,raw,phone)
                await conv.send_message(f'أضيف مصدر المراقبة {source}. يمكنك إضافة قنوات أخرى بالطريقة نفسها.',parse_mode=None)
        return
    if action == 'source_remove':
        source = await code_monitor.remove_source(user,int(value))
        return await event.respond(f'حُذف مصدر المراقبة {source}.',parse_mode=None,buttons=[[btn('رجوع','codes:menu')]])
    if action == 'bot':
        async with conversation(event) as conv:
            from shahm_engine import profile_for
            profile = profile_for(await read(conv,'معرف بوت الأكواد المسموح به مثل @FAABOT:'))
            caption = await read(conv,'نص زر استخدام الكود كما يظهر في البوت، مثل استخدام كود:')
            if not 1<=len(caption)<=80:
                raise ValueError('نص الزر طويل أو فارغ.')
            enabled = await choose(conv,'حالة البوت:',[(True,'تفعيل البوت'),(False,'تعطيل البوت')])
            if await confirm(conv,f'حفظ {profile.username} وزر «{caption}»؟'):
                await query("""INSERT INTO user_code_monitor_bots(owner_id,username,button_text,enabled) VALUES(?,?,?,?)
                    ON CONFLICT(owner_id,username) DO UPDATE SET button_text=excluded.button_text,enabled=excluded.enabled""",
                            (user,profile.username.lower(),caption,enabled),commit=True)
                await conv.send_message('حُفظ تعريف البوت. يمكنك إضافة أكثر من بوت مراقبة.',parse_mode=None)
        return
    if action == 'history':
        rows = await query("""SELECT c.code_id,c.bot,c.code,a.status,COUNT(*) FROM user_monitored_codes c
            JOIN user_monitored_code_accounts a ON a.code_id=c.code_id WHERE c.owner_id=?
            GROUP BY c.code_id,c.bot,c.code,a.status ORDER BY c.code_id DESC LIMIT 30""",(user,),fetchall=True) or []
        labels = {'pending':'بانتظار التنفيذ','preparing':'تحضير','sending':'إرسال','confirmed':'مؤكد','failed':'فشل','unknown':'غير محسوم'}
        return await event.edit('نتائج الأكواد\n'+'\n'.join(f'#{key} · {bot} · {code} · {labels[status]}: {count}' for key,bot,code,status,count in rows),parse_mode=None,buttons=[[btn('تحديث','codes:history')],[btn('رجوع','codes:menu')]])
    if action != 'menu':
        raise ValueError('العملية غير صالحة.')
    cfg = await code_monitor.configuration(user)
    sources = await code_monitor.sources(user)
    bots = await query('SELECT username,enabled FROM user_code_monitor_bots WHERE owner_id=? ORDER BY username',(user,),fetchall=True) or []
    src_text = '\n'.join(f'• {source} · +{phone} · آخر رسالة {last}' for _,_,source,phone,last,active in sources if active) or 'لا توجد قنوات مراقبة.'
    bot_text = '، '.join(f'{b} ({"مفعّل" if enabled else "متوقف"})' for b,enabled in bots) or 'لم تُضف بوتات بعد'
    remove = [[btn(f'حذف {source}',f'codes:source_remove:{source_id}','danger')] for source_id,_,source,_,_,active in sources[:10] if active]
    return await event.edit(
        f"مراقبة الأكواد\nالحالة: {'مفعّلة' if cfg and cfg[0] else 'متوقفة'}\n\nقنوات المراقبة:\n{src_text}\n\nالبوتات: {bot_text}\n\nتُستخدم حساباتك التي تملكها فقط.",
        parse_mode=None,
        buttons=[[btn('تشغيل المراقبة','codes:enable','success'),btn('إيقاف المراقبة','codes:stop','danger')],
                 [btn('إضافة قناة مراقبة','codes:source'),btn('إضافة / تعديل بوت','codes:bot')],
                 [btn('النتائج','codes:history')]]+remove+[[btn('رجوع','main')]],
    )


async def menu(event, data):
    user = event.chat_id
    if getattr(event,'sender_id',user)!=user:
        return await event.answer('استخدم المحادثة الخاصة.',alert=True)
    parts = data.split(':',2)
    prefix,action = parts[:2]
    value = parts[2] if len(parts)>2 else '0'
    try:
        if prefix == 'promo':
            return await paid_menu(event,action,value)
        if prefix == 'fund':
            return await funding_menu(event,action,value)
        if prefix == 'codes':
            return await codes_menu(event,action,value)
        raise ValueError('القسم غير صالح.')
    except (ValueError, asyncio.TimeoutError) as exc:
        text = str(exc) if isinstance(exc,ValueError) and not str(exc).startswith(('invalid literal','[<class')) else 'الإدخال غير صالح أو انتهت مهلة المحادثة.'
        return await event.respond(text or 'انتهت مهلة الإدخال.',parse_mode=None)
    except errors.RPCError:
        return await event.respond('تعذر التحقق من تيليجرام الآن؛ راجع صلاحيات البوت وحاول مجددًا.',parse_mode=None)
    except PermissionError:
        return await event.respond('تعذر تأكيد ملكية الحساب أو صلاحية استخدامه حاليًا.',parse_mode=None)
