# Auto-split from handlers.py. Keep shared imports/state in base.py.
from .base import *
from telethon.errors import SessionPasswordNeededError
from utils.privacy import mask_phone
from account_importer import import_batch, import_string_session, split_sessions, validate_phone_submission
from typing import TYPE_CHECKING
from .common import ProgressMessage

if TYPE_CHECKING:
    from .base import main_menu


async def buy_number(event):
    from .number_handlers import buy_menu
    return await buy_menu(event)


async def sell_account(event):
    await event.edit('📱 بيع رقم للبوت\nقدّم رقمًا جديدًا، ثم أكمل الخروج من الجلسات الأخرى لتحويل الرصيد تلقائيًا.',parse_mode=None,
        buttons=[[Button.inline('تقديم رقم أو دفعة جديدة',data='num:new_sale')],
                 [Button.inline('طلبات البيع',data='num:mysales')],
                 [Button.inline('رجوع',data='numbers_menu')]])


async def submit_sale(event):
    if not await enforce_rate_limit(event, "sell_account", 10):
        return
    async with event.client.conversation(event.chat_id) as conv:
        active_conversations[event.chat_id] = conv
        from .number_handlers import collect_submission
        from number_catalog import submission
        type_token = None
        try:
            typed_data = await collect_submission(conv,event.chat_id,pending=True)
            type_token = submission.set(typed_data)
            await conv.send_message(
                "أرسل رقم الهاتف (مثال: +9647801234567)، أو StringSession، أو ملف TXT للجلسات.",
                buttons=cancel_operation_keyboard(),
            )
            phone_resp = await conv.get_response(timeout=300)
            if phone_resp.file:
                await _sell_session_file(conv, phone_resp, event.chat_id)
                return
            raw_submission = (phone_resp.raw_text or "").strip()
            if not raw_submission.replace("+", "").replace(" ", "").isdigit():
                await _sell_one_session(conv, raw_submission, event.chat_id)
                return
            phone = phone_resp.text.replace("+", "").replace(" ", "")
            
            # Selling is allowed for the current owner even when the number
            # was previously purchased/installed in this same system.  The
            # transactional intake step below verifies ownership safely.
            country, reason = await validate_phone_submission(phone, check_existing=False)
            if not country:
                await conv.send_message(f"❌ {reason}")
                return
            
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
                
                session_str = client.session.save()
                
                # إضافة الحساب كبيع معلق
                if not await add_pending_sale_if_absent(phone, event.chat_id, country[3], country[0], session_str, twofa):
                    await conv.send_message("❌ هذا الحساب موجود أو لديه عملية بيع جارية.")
                    return
                await schedule_managed_twofa_change(phone, session_str, twofa if twofa != 'لا يوجد' else None)
                
                # إرسال رسالة نجاح
                await conv.send_message(f"{msgs['PENDING_SALE'].format(phone, country[3])}\n\n{TWOFA_CHANGE_NOTICE}")
                
                # طلب تسجيل الخروج من الجلسات الأخرى
                buttons = [[Button.inline("• تم الخروج • ✅", data=f"sell_logout_{phone}")]]
                await conv.send_message(msgs['SELL_LOGOUT_INSTRUCTIONS'], buttons=buttons)
                
            except Exception:
                logger.exception("Phone sale login failed for seller %s", event.chat_id)
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


async def _sell_one_session(conv, value, seller_id):
    result = await import_string_session(value, check_existing=False)
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
    country = await get_country(account.calling_code)
    if not await add_pending_sale_if_absent(account.phone, seller_id, country[3], account.calling_code, account.session, current_password or 'لا يوجد'):
        await conv.send_message("❌ الحساب موجود أو لديه عملية بيع جارية.")
        return
    await schedule_managed_twofa_change(account.phone, account.session, current_password)
    await conv.send_message(f"{msgs['PENDING_SALE'].format(account.phone, country[3])}\n\n{TWOFA_CHANGE_NOTICE}")
    await conv.send_message(
        msgs['SELL_LOGOUT_INSTRUCTIONS'],
        buttons=[[Button.inline("• تم الخروج • ✅", data=f"sell_logout_{account.phone}")]],
    )


async def _sell_session_file(conv, response, seller_id):
    name = (getattr(response.file, "name", "") or "").lower()
    mime = (getattr(response.file, "mime_type", "") or "").lower()
    if not name.endswith(".txt") and mime != "text/plain":
        await conv.send_message("❌ أرسل ملف TXT فقط.")
        return
    status = await conv.send_message("⏳ تم استلام الملف، جارٍ تنزيله وقراءة الجلسات…")
    content = await response.download_media(bytes)
    try:
        values = split_sessions(content or b"")
    except ValueError as exc:
        await conv.send_message(f"❌ {exc}")
        return
    reporter = ProgressMessage(status, "جارٍ فحص جلسات الملف", len(values))
    async def report_import(processed, _total):
        await reporter.update(processed, force=processed == len(values))
    results = await import_batch(values, on_progress=report_import, check_existing=False)
    accepted = 0
    accepted_phones = []
    duplicates = sum(item.duplicate for item in results)
    reasons = {}
    for item in results:
        if item.account:
            country = await get_country(item.account.calling_code)
            if await add_pending_sale_if_absent(item.account.phone, seller_id, country[3], item.account.calling_code, item.account.session, 'لا يوجد'):
                await schedule_managed_twofa_change(item.account.phone, item.account.session)
                accepted += 1
                accepted_phones.append(item.account.phone)
            else:
                duplicates += 1
                reasons["الحساب موجود أو لديه عملية بيع جارية."] = reasons.get("الحساب موجود أو لديه عملية بيع جارية.", 0) + 1
        elif item.reason:
            reasons[item.reason] = reasons.get(item.reason, 0) + 1
    rejected = len(results) - accepted
    summary = [f"تمت معالجة {len(values)} جلسة.", f"المقبول للبيع: {accepted}.", f"المرفوض: {rejected}.", f"المكرر: {duplicates}."]
    if reasons:
        summary.append("أسباب الرفض: " + "؛ ".join(f"{reason} ({count})" for reason, count in reasons.items()))
    summary.append("أكمل تسجيل الخروج من الجلسات الأخرى ثم استخدم زر «تم الخروج» لكل حساب.")
    if accepted:
        summary.append(TWOFA_CHANGE_NOTICE)
    await reporter.update(len(values), accepted, len(results) - accepted, force=True)
    await conv.send_message("\n".join(summary))
    for phone in accepted_phones:
        await conv.send_message(
            f"📱 +{phone}\nبعد تسجيل الخروج من جميع الجلسات الأخرى، اضغط الزر لإتمام البيع وتحويل الرصيد.",
            buttons=[[Button.inline("• تم الخروج • ✅", data=f"sell_logout_{phone}")]],
        )


async def country_selected(event,data):
    return await buy_number(event)


async def select_account(event,data):
    from .number_handlers import quote_panel
    return await quote_panel(event,data.rsplit('_',1)[-1])


async def buy_confirmed(event,data):
    from .number_handlers import quote_panel
    return await quote_panel(event,data.rsplit('_',1)[-1])


async def confirm_purchase_handler(event,data):
    from commerce import complete_purchase
    from .number_handlers import quote_panel
    phone = data.split(':')[1]
    row = await execute_query('SELECT quote_id FROM pending_purchases WHERE user_id=? AND phone=?',(event.chat_id,phone),fetchone=True)
    if not row or not row[0]:
        return await quote_panel(event,phone)
    result = await complete_purchase(event.chat_id,row[0])
    return await event.edit('✅ اكتمل الشراء.' if result.get('completed') else '⏳ حُجز الشراء حتى انتهاء الإيجار.',parse_mode=None,
        buttons=[[Button.inline('حساباتي',data='accounts_view')]])


async def next_sell(event,data):
    from sale_review import seller_offer
    return await seller_offer(event,data.split(':')[1])


async def check_account(event,data):
    from sale_review import seller_offer
    return await seller_offer(event,data.split(':')[1])


async def sell_logout_handler(event,data):
    from helpers import decrypt_session, count_ses
    from sale_review import complete_sale_sync
    from utils.asyncio_compat import to_thread
    from workflow import query

    phone = data.rsplit('_', 1)[-1]
    if not await enforce_rate_limit(event, f"sell_logout:{phone}", 2):
        return
    row = await query(
        "SELECT session FROM pending_sales WHERE phone=? AND seller_id=?",
        (phone, event.chat_id),
        fetchone=True,
    )
    if not row:
        return await event.answer("هذا البيع مكتمل أو غير متاح ضمن حسابك.", alert=True)
    session = decrypt_session(row[0])
    if not session:
        return await event.answer("تعذر قراءة جلسة الحساب. أعد تقديم الرقم.", alert=True)

    remaining_sessions = await count_ses(session)
    if remaining_sessions is None:
        return await event.answer("تعذر التحقق من الجلسات الآن؛ حاول لاحقًا.", alert=True)
    if remaining_sessions:
        return await event.answer(
            f"لا تزال هناك {remaining_sessions} جلسة أخرى نشطة. سجّل الخروج منها أولًا.",
            alert=True,
        )

    # ``count_ses`` only returns a number after Telegram accepts the stored
    # authorization and serves GetAuthorizations.  Do not re-import this same
    # session here: import validation also enforces a changing DC-address
    # allowlist, which can reject an otherwise valid authorized session.

    try:
        completed = await to_thread(complete_sale_sync, event.chat_id, phone, row[0])
    except ValueError as exc:
        return await event.answer(str(exc), alert=True)
    if completed:
        return await event.edit(
            "✅ تم التحقق: جلسة البوت هي الجلسة الوحيدة. عُرض الرقم للبيع وأُضيف المبلغ إلى رصيدك.",
            parse_mode=None,
            buttons=[[Button.inline("حساباتي", data="accounts_view")]],
        )
    return await event.answer("تم إكمال البيع مسبقًا.", alert=True)
