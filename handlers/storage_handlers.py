# Auto-split from handlers.py. Keep shared imports/state in base.py.
from decimal import Decimal

from telethon.errors import SessionPasswordNeededError

from .base import *
from account_importer import MAX_TXT_BYTES, parse_session_file
import workflow
from session_converter import MangSession, PyroSession, TeleSession, ValidationError
from utils.privacy import mask_phone
from typing import TYPE_CHECKING
from .common import ProgressMessage

if TYPE_CHECKING:
    from .base import (
        get_account_for_user_action,
        get_account_twofa_for_user_action,
        get_owned_stored_account,
        main_menu,
    )


async def store_existing_account(event, phone):
    user_id = event.chat_id
    account = await get_account_for_user_action(user_id, phone)
    if not account:
        await event.answer("❌ لم يتم العثور على الحساب!", alert=True)
        return
    if account[2] != user_id:
        return await event.answer('تبقى ملكية الحساب لصاحبه؛ استخدم لوحة الإدارة لتشغيله.', alert=True)
    
    session_str = account[1]
    twofa = await get_account_twofa_for_user_action(user_id, phone)
    await add_stored_account(phone, session_str, user_id, twofa)
    if user_id == ADMIN_ID and await get_account_info(phone):
        await delete_account(phone)
        await event.answer("✅ تم سحب الرقم وتخزينه لدى المطور وإزالته من البيع!", alert=True)
        return

    await event.answer("✅ تم تخزين الحساب بنجاح!", alert=True)


async def get_verification_code(event, phone):
    if not await enforce_rate_limit(event, "verification_code", 10):
        return
    client = None
    try:
        account = await get_account_for_user_action(event.chat_id, phone)
        if not account:
            await event.answer("❌ لم يتم العثور على الحساب", alert=True)
            return
        session_str = account[1]
        if not session_str:
            await event.answer("❌ لا توجد جلسة محفوظة لهذا الحساب", alert=True)
            return

        client = TelegramClient(StringSession(session_str), API_ID, API_HASH,actor_id=event.chat_id,account_phone=phone,sensitive_action=True,exclusive_session=True)
        await client.connect()

        if not await client.is_user_authorized():
            from session_lifecycle import retire
            await retire(session_str,phone=phone,reason='not_authorized')
            await event.answer("❌ جلسة هذا الرقم منتهية. أعد إضافة الحساب.", alert=True)
            return

        notifications_entity = types.InputPeerUser(TELEGRAM_NOTIFICATIONS_USER_ID, 0)
        messages = await client.get_messages(notifications_entity, limit=10)

        for message in messages:
            text = message.message or ""
            code_match = re.search(r'\b\d{5,6}\b', text)
            if code_match:
                code = code_match.group()
                from account_access import audit
                await audit(event.chat_id,account,'read_login_code','completed')
                await event.answer(f"✅ تم العثور على الكود: {code}", alert=True)
                return

        await event.answer("❌ لم يتم العثور على كود حديث في رسائل Telegram", alert=True)

    except (
        AuthKeyInvalidError,
        AuthKeyUnregisteredError,
        SessionExpiredError,
        SessionRevokedError,
        UnauthorizedError,
        UserDeactivatedBanError,
        UserDeactivatedError,
    ) as exc:
        logger.warning("Invalid session while getting code for %s", mask_phone(phone))
        from session_lifecycle import on_rpc_error
        if 'session_str' in locals():
            await on_rpc_error(session_str,exc,phone=phone)
        await event.answer("❌ جلسة هذا الرقم ملغاة. أعد إضافة الحساب.", alert=True)
    except Exception as e:
        logger.error(f"Error getting verification code: {str(e)}")
        await event.answer("❌ حدث خطأ أثناء محاولة الحصول على الكود", alert=True)
    finally:
        if client and client.is_connected():
            await client.disconnect()


async def logout_account_sessions(event, phone):
    account = await get_account_for_user_action(event.chat_id, phone)
    if not account:
        await event.answer("❌ لم يتم العثور على الحساب", alert=True)
        return
    await event.edit('⚠️ إنهاء جلسات الحساب\nستُنهي هذه العملية الجلسات الأخرى فقط، وتُبقي جلسة البوت والحساب المنصب محفوظين. أكد التنفيذ.',
        buttons=[[Button.inline('تأكيد إنهاء الجلسات', data=f'acct:logout:{phone}')],
                 [Button.inline('إلغاء', data='accounts_view')]])


async def confirmed_logout(event, phone):
    from account_access import resolve, audit
    account = await resolve(event.chat_id,phone,sensitive=True)
    if not account:
        return await event.answer('لم يعد الإذن متاحًا.',alert=True)
    session_str = account[1]
    success = await logout_all_sessions(session_str, actor_id=event.chat_id, phone=phone)
    await audit(event.chat_id,account,'logout_sessions','completed' if success else 'failed')
    if success:
        await event.answer("✅ تم إنهاء الجلسات الأخرى مع الاحتفاظ بجلسة البوت والحساب المنصب.", alert=True)
    else:
        await event.answer("❌ تعذر إكمال إنهاء الجلسات الأخرى. أعد المحاولة لاحقًا.", alert=True)


async def terminate_sessions_menu(event):
    from account_access import accounts
    rows = await accounts(event.chat_id, sensitive=True)
    if not rows:
        return await event.answer("❌ لا توجد حسابات متاحة لإنهاء جلساتها.", alert=True)
    buttons = [[Button.inline(f"+{row[0]}", data=f"logout_sessions:{row[0]}")] for row in rows]
    buttons.append([Button.inline("رجوع", data="install_menu")])
    await event.edit("اختر الحساب لإنهاء جلساته الأخرى مع الإبقاء على جلسة البوت:", buttons=buttons)


def storage_action_text(action):
    labels = {
        "set_group": "إضافة كروب تخزين",
        "enable": "تفعيل التخزين",
        "disable": "تعطيل التخزين",
    }
    return labels.get(action, "إعداد التخزين")


async def storage_account_menu(event, action):
    user_id = event.chat_id
    accounts = await get_stored_accounts(user_id)
    if not accounts:
        await event.answer("❌ لا توجد حسابات مخزنة!", alert=True)
        return

    item_buttons = [
        Button.inline(f"+{account[0]}", data=f"storage_account:{action}:{account[0]}")
        for account in accounts
    ]
    buttons = arrange_buttons(item_buttons)
    buttons.append([Button.inline("• رجوع • ↩️", data="control_settings_account")])
    await event.edit(
        f"📱 اختر الحساب لتنفيذ: {storage_action_text(action)}",
        buttons=buttons
    )


async def storage_account_selected(event, data):
    parts = data.split(":", 2)
    if len(parts) != 3:
        await event.answer("❌ بيانات العملية غير صالحة!", alert=True)
        return

    action, phone = parts[1], parts[2]
    user_id = event.chat_id
    account = await get_owned_stored_account(user_id, phone)
    if not account:
        await event.answer("❌ لم يتم العثور على الحساب!", alert=True)
        return

    if action == "set_group":
        await set_storage_group_handler(event, account)
    elif action == "enable":
        await enable_incoming_storage_for_account(event, account)
    elif action == "disable":
        await disable_incoming_storage_for_account(event, account)
    else:
        await event.answer("❌ نوع العملية غير معروف!", alert=True)


async def set_storage_group_handler(event, account):
    user_id = event.chat_id
    phone = account[0]
    async with event.client.conversation(user_id) as conv:
        active_conversations[user_id] = conv
        try:
            await conv.send_message(
                f"أرسل رابط أو معرف كروب التخزين للحساب +{phone}:",
                buttons=cancel_operation_keyboard()
            )
            group_resp = await conv.get_response(timeout=300)
            storage_group = group_resp.text.strip()

            verifier = None
            try:
                verifier = TelegramClient(StringSession(account[1]), API_ID, API_HASH)
                await verifier.connect()
                await resolve_posting_target(verifier, storage_group)
            except Exception as exc:
                await conv.send_message(
                    "❌ لم يستطع الحساب الوصول إلى كروب التخزين.\n"
                    f"السبب: {exc}"
                )
                return
            finally:
                if verifier and verifier.is_connected():
                    await verifier.disconnect()

            await set_setting(f"storage_group:{user_id}:{phone}", storage_group)
            await conv.send_message(f"✅ تم تعيين كروب التخزين للحساب +{phone} بنجاح.")
        except asyncio.TimeoutError:
            await conv.send_message("⏱ انتهى الوقت المحدد للإدخال!")
        finally:
            if user_id in active_conversations:
                del active_conversations[user_id]


async def stop_incoming_storage_client(user_id, phone):
    key = (user_id, str(phone))
    client = incoming_storage_clients.pop(key, None)
    if client and client.is_connected():
        await client.disconnect()


async def enable_incoming_storage_for_account(event, account):
    user_id = event.chat_id
    phone, session = account[0], account[1]
    storage_group = await get_setting(f"storage_group:{user_id}:{phone}") or await get_setting(f"storage_group:{user_id}")
    if not storage_group:
        await event.answer("❌ عيّن كروب التخزين لهذا الحساب أولاً.", alert=True)
        return

    await stop_incoming_storage_client(user_id, phone)
    client = TelegramClient(StringSession(session), API_ID, API_HASH)
    try:
        await client.connect()
        target = await resolve_posting_target(client, storage_group)

        async def forward_private_message(storage_event):
            try:
                sender = await storage_event.get_sender()
                sender_name = " ".join(
                    part for part in [
                        getattr(sender, "first_name", None),
                        getattr(sender, "last_name", None)
                    ] if part
                ).strip() or getattr(sender, "username", None) or str(storage_event.sender_id)
                await client.send_message(
                    target,
                    f"📩 رسالة واردة للحساب +{phone}\n"
                    f"👤 من: {sender_name}\n"
                    f"🆔: {storage_event.sender_id}"
                )
                await client.forward_messages(target, storage_event.message)
            except Exception as exc:
                logger.error(f"Error storing incoming message: {str(exc)}")

        client.add_event_handler(
            forward_private_message,
            events.NewMessage(incoming=True, func=lambda e: e.is_private)
        )
        incoming_storage_clients[(user_id, str(phone))] = client
        await set_setting(f"incoming_storage:{user_id}:{phone}", "1")
        await event.edit(
            f"✅ تم تفعيل التخزين فعلياً للحساب +{phone}.\n"
            "سيتم تحويل الرسائل الخاصة الواردة إلى كروب التخزين.",
            buttons=[[Button.inline("• رجوع • ↩️", data="control_settings_account")]]
        )
    except Exception as exc:
        if client and client.is_connected():
            await client.disconnect()
        await event.edit(
            f"❌ فشل تفعيل التخزين للحساب +{phone}.\n"
            f"السبب: {exc}",
            buttons=[[Button.inline("• رجوع • ↩️", data="control_settings_account")]]
        )


async def disable_incoming_storage_for_account(event, account):
    user_id = event.chat_id
    phone = account[0]
    await stop_incoming_storage_client(user_id, phone)
    await set_setting(f"incoming_storage:{user_id}:{phone}", "0")
    await event.edit(
        f"✅ تم تعطيل التخزين للحساب +{phone}.",
        buttons=[[Button.inline("• رجوع • ↩️", data="control_settings_account")]]
    )


async def install_session_handler(event):
    if not await enforce_rate_limit(event, "install_session", 10):
        return
    async with event.client.conversation(event.chat_id) as conv:
        active_conversations[event.chat_id] = conv
        from .number_handlers import collect_submission
        from number_catalog import submission
        type_token=None
        try:
            data=await collect_submission(conv,event.chat_id,require_evidence=False)
            type_token=submission.set(data)
            await conv.send_message(
                "أرسل كود الجلسة (تليثون أو بايروجرام):",
                buttons=cancel_operation_keyboard()
            )
            session_response = await conv.get_response(timeout=300)
            session_str = "".join(session_response.text.replace("`", "").split())
            
            try:
                # التحقق من نوع الجلسة وتحويلها إذا لزم الأمر
                if session_str.startswith('1'):
                    # جلسة تليثون
                    tele_session = TeleSession.normalize_string(session_str)
                else:
                    # جلسة بايروجرام، تحويل إلى تليثون
                    tele_session = MangSession.PYROGRAM_TO_TELETHON(session_str)
                
                # اختبار الجلسة
                client = TelegramClient(StringSession(tele_session), API_ID, API_HASH)
                await client.connect()
                me = await client.get_me()
                phone = getattr(me, 'phone', None)
                if not me or not phone or not await client.is_user_authorized():
                    await conv.send_message("❌ الجلسة غير مخوّلة أو لا تحتوي على رقم صالح.")
                    return
                needs_twofa = await session_requires_twofa(tele_session)
                if needs_twofa is None:
                    await conv.send_message("❌ تعذر التحقق من حالة التحقق بخطوتين للجلسة.")
                    return
                current_password = None
                if needs_twofa:
                    await conv.send_message(
                        "🔐 أرسل كلمة التحقق بخطوتين الحالية. تُحفظ مشفرة لإتمام التغيير إلى كلمة التحقق الثابتة، وتُحذف بيانات المهمة بعد نجاحها. عند فشله تبقى لاستكماله.",
                        buttons=cancel_operation_keyboard(),
                    )
                    password_response = await conv.get_response(timeout=300)
                    current_password = password_response.text
                    await delete_sensitive_message(password_response)
                # تخزين الجلسة
                await add_stored_account(phone, tele_session, event.chat_id, current_password or 'لا يوجد', schedule_rotation=True)
                await conv.send_message(f"✅ تم تخزين الحساب بنجاح! الرقم: +{phone}\n{TWOFA_CHANGE_NOTICE}")
                
            except ValidationError:
                await conv.send_message("❌ كود الجلسة غير صالح أو غير مكتمل. أرسله كاملاً من دون علامات تنسيق أو مسافات.")
            except Exception:
                logger.exception("Session installation failed for user %s", event.chat_id)
                await conv.send_message("❌ تعذر تثبيت الجلسة. تأكد من صلاحيتها ثم حاول مجددًا.")
        except (asyncio.TimeoutError,ValueError):
            await conv.send_message("⏱ انتهى الوقت المحدد للإدخال!")
        finally:
            if type_token is not None:
                submission.reset(type_token)
            if event.chat_id in active_conversations:
                del active_conversations[event.chat_id]
            if 'client' in locals() and client and client.is_connected():
                await client.disconnect()


@workflow.tracked('import', owner='event', event=True)
async def install_sessions_txt_handler(event):
    """Validate and install a bounded batch of session strings from a TXT file."""
    user_id = event.chat_id
    if not await enforce_rate_limit(event, "install_sessions_txt", 30):
        return
    async with event.client.conversation(user_id) as conv:
        active_conversations[user_id] = conv
        from .number_handlers import collect_submission
        from number_catalog import submission
        type_token=None
        try:
            data=await collect_submission(conv,event.chat_id,require_evidence=False)
            type_token=submission.set(data)
            await conv.send_message(
                "أرسل ملف TXT يحتوي جلسة واحدة في كل سطر (الحد 1000 جلسة / 512KB).\n"
                "يمكن إرفاق كلمة 2FA الحالية اختيارياً هكذا: `SESSION | CURRENT_2FA_PASSWORD`.",
                parse_mode="markdown",
                buttons=cancel_operation_keyboard(),
            )
            response = await conv.get_response(timeout=300)
            if not response.file or not (response.file.name or "").lower().endswith(".txt"):
                await workflow.note_failure('الملف ليس TXT صالحًا')
                await conv.send_message("❌ أرسل ملف TXT صالحاً.")
                return
            if not isinstance(response.file.size, int) or response.file.size > MAX_TXT_BYTES:
                await workflow.note_failure('حجم الملف غير صالح أو أكبر من الحد المسموح')
                await conv.send_message('❌ حجم الملف أكبر من 512KB أو تعذر التحقق من حجمه.')
                return
            received_status = await conv.send_message("⏳ تم استلام الملف، جارٍ تنزيله وقراءة محتواه…")
            content = await response.download_media(bytes)
            try:
                entries = parse_session_file(content, allow_current_password=True)
            except ValueError as exc:
                await workflow.note_failure('تعذر قراءة ملف الجلسات أو تجاوز الحدود المسموحة')
                await conv.send_message(f"❌ {exc}")
                return
            if not entries:
                await workflow.note_failure('ملف الجلسات فارغ')
                await conv.send_message("❌ يجب أن يحتوي الملف على جلسة واحدة على الأقل.")
                return
            valid = failed = 0
            reporter = ProgressMessage(received_status, "جارٍ فحص الجلسات وتنصيب الصالح منها", len(entries))
            await reporter.update(0, force=True)
            await workflow.progress(total=len(entries), message='التحقق من الجلسات وتخزينها')
            protected_without_password = []
            for index, (session_str, supplied_password) in enumerate(entries, start=1):
                await workflow.progress(processed=index - 1, success=valid, failed=failed)
                client = None
                try:
                    tele_session = TeleSession.normalize_string(session_str) if session_str.startswith("1") else MangSession.PYROGRAM_TO_TELETHON(session_str)
                    client = TelegramClient(StringSession(tele_session), API_ID, API_HASH)
                    await client.connect()
                    if not await client.is_user_authorized():
                        raise ValidationError("unauthorized")
                    me = await client.get_me()
                    if not me or not me.phone:
                        raise ValidationError("missing phone")
                    needs_twofa = await session_requires_twofa(tele_session)
                    if needs_twofa is None:
                        raise ValidationError("twofa state unavailable")
                    if needs_twofa and not supplied_password:
                        protected_without_password.append((str(me.phone), tele_session))
                        continue
                    await add_stored_account(me.phone, tele_session, user_id, supplied_password or 'لا يوجد', schedule_rotation=True)
                    valid += 1
                except Exception:
                    failed += 1
                finally:
                    if client and client.is_connected():
                        await client.disconnect()
                    await reporter.update(index, valid, failed)
            skipped = 0
            while protected_without_password:
                await workflow.progress(processed=valid+failed, success=valid, failed=failed,
                                        message='بانتظار كلمة التحقق للحسابات المحمية')
                all_choice = "جميع الحسابات المحمية"
                stop_choice = "إنهاء التنصيب"
                phone_choices = {f"+{phone}": (phone, session) for phone, session in protected_without_password}
                labels = list(phone_choices)
                page = 0
                while True:
                    visible = labels[page * 20:(page + 1) * 20]
                    navigation = []
                    if page:
                        navigation.append("السابق")
                    if (page + 1) * 20 < len(labels):
                        navigation.append("التالي")
                    from .inline_input import ask as inline_ask
                    response_choice = await inline_ask(conv,
                        "🔐 اختر جميع الحسابات المتبقية لكلمة مشتركة أو رقم حساب لإدخال كلمته منفرداً.",
                        [all_choice, *visible, *navigation, stop_choice],
                    )
                    choice = (response_choice.raw_text or "").strip()
                    if choice == "التالي" and (page + 1) * 20 < len(labels):
                        page += 1
                    elif choice == "السابق" and page:
                        page -= 1
                    elif choice == all_choice or choice == stop_choice or choice in visible:
                        break
                    else:
                        await conv.send_message("اختر أحد الأزرار المعروضة.")
                if choice == stop_choice:
                    skipped = len(protected_without_password)
                    await conv.send_message("تم إنهاء الاختيار.")
                    break
                selected = protected_without_password[:] if choice == all_choice else [phone_choices[choice]]
                await conv.send_message(
                    f"🔑 أرسل كلمة التحقق الحالية فقط ({len(selected)} حساب).",
                    buttons=cancel_operation_keyboard(),
                )
                password_response = await conv.get_response(timeout=300)
                current_password = password_response.raw_text or ""
                await delete_sensitive_message(password_response)
                if not current_password:
                    await conv.send_message("❌ أرسل كلمة المرور كرسالة نصية.")
                    continue
                for phone, tele_session in selected:
                    try:
                        await add_stored_account(phone, tele_session, user_id, current_password, schedule_rotation=True)
                        valid += 1
                    except (ValueError, DatabaseError):
                        failed += 1
                        await conv.send_message(f"❌ تعذر تنصيب +{phone}. قد يكون مسجلاً لمستخدم آخر أو تعذر حفظه.")
                    protected_without_password = [item for item in protected_without_password if item[0] != phone]
            await workflow.progress(processed=valid+failed+skipped, success=valid, failed=failed,
                                    message=f'اكتملت المعالجة؛ تم ترك {skipped} حساب')
            await reporter.update(len(entries), valid, failed + skipped, force=True)
            await conv.send_message(
                f"✅ اكتمل الفحص والتنصيب: {valid} تم تنصيبه، {failed} فشل، {skipped} تم تركه."
                + (f"\n{TWOFA_CHANGE_NOTICE}" if valid else "")
            )
        except (asyncio.TimeoutError,ValueError):
            await conv.send_message("⏱ انتهى الوقت المحدد للإدخال!")
        finally:
            if type_token is not None:
                submission.reset(type_token)
            active_conversations.pop(user_id, None)
            with contextlib.suppress(Exception):
                await conv.send_message("انتهت عملية التنصيب.")


async def store_account_handler(event, data):
    phone = data.split(':')[1]
    user_id = event.chat_id

    async with get_user_lock(("purchase", phone)):
        purchase = await get_pending_purchase(user_id, phone)
        if not purchase:
            return await event.answer("لم يتم العثور على عملية شراء!", alert=True)
        from deferred_purchase import reserve
        if await reserve(user_id, purchase):
            return await event.edit('⏳ حُجز الرقم للشراء بعد انتهاء جميع الإيجارات دون خصم الآن.',
                buttons=[[Button.inline('حجوزات الشراء', data='deferred:list:0')]])
        if pending_purchase_status(purchase) != "pending":
            return await event.answer("هذه العملية قيد المعالجة أو مكتملة بالفعل.", alert=True)

        if not await get_account_info(phone):
            await delete_pending_purchase(user_id, phone)
            return await event.answer("تم شراء هذا الحساب من مستخدم آخر.", alert=True)

        user_data = await get_user(user_id)
        price = Decimal(str(purchase[5])).quantize(Decimal("0.01"))
        if not user_data or user_data['coins'] < price:
            return await event.answer("رصيدك غير كافٍ لإكمال الشراء.", alert=True)

        await execute_query(
            "UPDATE pending_purchases SET status='processing', processing=1 WHERE user_id=? AND phone=? AND status='pending'",
            (user_id, phone),
            commit=True
        )
        settled, _ = await settle_purchase_atomically(user_id, purchase, store_account=True)
        if not settled:
            await execute_query(
                "UPDATE pending_purchases SET status='pending', processing=0 WHERE user_id=? AND phone=?",
                (user_id, phone), commit=True
            )
            return await event.answer("تعذر إتمام الشراء، حاول لاحقاً.", alert=True)
        await join_mandatory_channel(purchase[6])
        await publish_purchase_proof(event.client, user_id, phone, purchase[4], price)

        await event.answer(msgs['STORAGE_SUCCESS'], alert=True)
        await main_menu(event)


async def logout_account(event, data):
    # The legacy public callback could revoke sessions and delete unsold stock
    # without ownership or payment. Inventory actions must go through checkout.
    phone = data.split(':')[1]
    from .buy_sell_handlers import confirm_purchase_handler
    await confirm_purchase_handler(event, f'confirm_purchase:{phone}')


async def install_menu_handler(event):
    await event.edit(msgs['INSTALL_MENU'], buttons=install_menu_keyboard(), parse_mode='markdown')


async def delete_install_handler(event):
    user_id = event.chat_id
    accounts = await execute_query("SELECT phone FROM stored_accounts WHERE user_id=? ORDER BY phone", (user_id,), fetchall=True) or []
    
    if not accounts:
        await event.answer("❌ لا توجد حسابات مخزنة!", alert=True)
        return
    
    item_buttons = []
    for account in accounts:
        item_buttons.append(Button.inline(f"+{account[0]}", data=f"del_install:{account[0]}"))
    buttons = arrange_buttons(item_buttons)
    
    buttons.append([Button.inline("• رجوع • ↩️", data="install_menu")])
    await event.edit("اختر الحساب لحذف تنصيبه من تخزين البوت فقط. لن تُنهى أي جلسة في Telegram:", buttons=buttons)


async def extract_session_handler(event):
    await event.edit(
        "🔐 **إنشاء جلسة جديدة**\n\nاختر نوع كود الجلسة الذي تريد إنشاؤه، ثم سيطلب منك البوت رقم الهاتف وكود تسجيل الدخول.",
        buttons=[
            [Button.inline("• Telethon • 📦", data="new_session_telethon")],
            [Button.inline("• Pyrogram • 🔥", data="new_session_pyrogram")],
            [Button.inline("• رجوع • ↩️", data="install_menu")],
        ],
    )


async def create_new_session_handler(event, session_format):
    if not await enforce_rate_limit(event, "create_new_session", 10):
        return
    client = TelegramClient(StringSession(), API_ID, API_HASH)
    async with event.client.conversation(event.chat_id) as conv:
        active_conversations[event.chat_id] = conv
        try:
            await conv.send_message("أرسل رقم الهاتف كاملاً مع رمز الدولة، مثال: +9647XXXXXXXXX", buttons=cancel_operation_keyboard())
            phone = (await conv.get_response(timeout=300)).text.strip().replace(" ", "")
            if not phone.startswith("+") or not phone[1:].isdigit():
                await conv.send_message("❌ رقم الهاتف غير صالح. أرسله مع رمز الدولة وبداية +.")
                return

            await client.connect()
            await client.send_code_request(phone)
            await conv.send_message("📩 تم إرسال كود تسجيل الدخول إلى تيليجرام. أرسل الكود هنا:", buttons=cancel_operation_keyboard())
            code = (await conv.get_response(timeout=300)).text.strip().replace(" ", "")
            current_password = None
            try:
                await client.sign_in(phone=phone, code=code)
            except SessionPasswordNeededError:
                await conv.send_message(
                    "🔐 هذا الحساب محمي بكلمة مرور التحقق بخطوتين. أرسل كلمة المرور؛ "
                    "ستُحفظ مشفرة لإتمام التغيير إلى كلمة التحقق الثابتة، وتُحذف بيانات المهمة بعد نجاحها.",
                    buttons=cancel_operation_keyboard(),
                )
                password_response = await conv.get_response(timeout=300)
                current_password = password_response.text
                await delete_sensitive_message(password_response)
                await client.sign_in(password=current_password)

            me = await client.get_me()
            if not me:
                await conv.send_message("❌ تعذر إنشاء الجلسة، حاول مجدداً.")
                return
            telethon_session = client.session.save()
            await schedule_managed_twofa_change(str(me.phone or phone).lstrip("+"), telethon_session, current_password, owner_id=event.chat_id)
            if session_format == "pyrogram":
                tele_session = TeleSession.from_string(telethon_session)
                exported_session = PyroSession(
                    dc_id=tele_session.dc_id,
                    auth_key=tele_session.auth_key,
                    api_id=API_ID,
                    user_id=me.id,
                    is_bot=bool(getattr(me, "bot", False)),
                ).to_string()
                label = "Pyrogram"
            else:
                exported_session = telethon_session
                label = "Telethon"
            await conv.send_message(
                f"✅ تم إنشاء جلسة {label} للحساب +{me.phone or phone}:\n\n`{exported_session}`\n\n"
                f"⚠️ احتفظ بها بسرية تامة، ثم أرسلها إلى زر «تنصيب حساب» عند الحاجة.\n\n{TWOFA_CHANGE_NOTICE}",
                parse_mode="markdown",
                buttons=[[Button.inline("• رجوع إلى التنصيب • ↩️", data="install_menu")]],
            )
        except asyncio.TimeoutError:
            await conv.send_message("⏱ انتهى الوقت المحدد للإدخال!")
        except Exception:
            logger.exception("Failed to create a new %s session", session_format)
            await conv.send_message("❌ تعذر تسجيل الدخول أو إنشاء الجلسة. تحقق من الرقم والكود ثم حاول مجدداً.")
        finally:
            active_conversations.pop(event.chat_id, None)
            if client.is_connected():
                await client.disconnect()


async def delete_install_account(event, data):
    phone = data.split(":")[1]
    if not await delete_stored_account(phone, user_id=event.chat_id):
        await event.answer("❌ لا تملك هذا الحساب.", alert=True)
        return
    await stop_incoming_storage_client(event.chat_id, phone)
    await set_setting(f"incoming_storage:{event.chat_id}:{phone}", "0")
    await event.answer(f"✅ تم حذف تنصيب +{phone} من تخزين البوت فقط. لم تُنهَ أي جلسة.", alert=True)
    await install_menu_handler(event)
