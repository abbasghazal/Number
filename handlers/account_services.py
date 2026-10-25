# Auto-split from handlers.py. Keep shared imports/state in base.py.
from .base import *
from account_health import filter_usable_accounts
from utils.privacy import mask_phone
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from .base import get_owned_stored_account


async def get_service_accounts_for_user(user_id):
    from account_access import accounts
    owned = await accounts(user_id)
    rented = await get_rented_accounts(user_id)
    combined = list(owned)
    known = {str(a[0]) for a in combined}
    for phone, session, ends_at in rented:
        if str(phone) not in known:
            combined.append((phone,session,user_id,"",ends_at))
    return await filter_usable_accounts(combined)


async def premium_join_handler(event):
    """Join a giveaway channel with the requester's own stored/rented accounts."""
    user_id = event.sender_id or event.chat_id
    accounts = await get_service_accounts_for_user(user_id)
    if not accounts:
        await event.answer("❌ لا توجد حسابات تملكها أو تستأجرها.", alert=True)
        return
    async with event.client.conversation(user_id) as conv:
        active_conversations[user_id] = conv
        try:
            await conv.send_message("أرسل رابط قناة الهدايا:", buttons=cancel_operation_keyboard())
            response = await conv.get_response(timeout=300)
            target = normalize_super_target(response.text)
            if not target:
                await conv.send_message("❌ رابط القناة غير صالح.")
                return
            await conv.send_message(f"أرسل العدد من 1 إلى {len(accounts)}:", buttons=cancel_operation_keyboard())
            count = int((await conv.get_response(timeout=300)).text.strip())
            if count < 1 or count > len(accounts):
                await conv.send_message("❌ العدد خارج النطاق المتاح.")
                return
            joined = 0
            for phone, session, *_ in accounts[:count]:
                if not await service_account_authorized(user_id, (phone, session)):
                    continue
                if not await session_is_usable(session):
                    continue
                try:
                    await join_super_target_with_stored_account(session, target)
                    joined += 1
                except Exception as exc:
                    logger.info("Premium channel join failed for %s: %s", mask_phone(phone), exc)
            await conv.send_message(f"✅ انضم {joined} من أصل {count} حساباً إلى القناة. راقب الهدايا من داخل الحسابات؛ تيليجرام لا يوفّر إشعاراً موثوقاً للبوت عند الفوز.")
            await event.client.send_message(ADMIN_ID, f"⭐ طلب جلب مميز: المستخدم {user_id} انضم بـ {joined}/{count} حساب إلى {target}.")
        except (ValueError, asyncio.TimeoutError):
            await conv.send_message("❌ انتهى الوقت أو أُرسل عدد غير صحيح.")
        finally:
            active_conversations.pop(user_id, None)


async def join_owned_channel_handler(event):
    """Join a channel only with accounts accessible to the requesting user."""
    user_id = event.sender_id or event.chat_id
    accounts = await get_service_accounts_for_user(user_id)
    if not accounts:
        await event.answer("❌ لا توجد حسابات تملكها أو تستأجرها.", alert=True)
        return
    async with event.client.conversation(user_id) as conv:
        active_conversations[user_id] = conv
        try:
            await conv.send_message("أرسل رابط القناة أو السوبر:", buttons=cancel_operation_keyboard())
            target = normalize_super_target((await conv.get_response(timeout=300)).text)
            if not target:
                await conv.send_message("❌ الرابط غير صالح.")
                return
            await conv.send_message(f"أرسل العدد من 1 إلى {len(accounts)}:", buttons=cancel_operation_keyboard())
            count = int((await conv.get_response(timeout=300)).text.strip())
            if count < 1 or count > len(accounts):
                await conv.send_message("❌ العدد خارج النطاق المتاح.")
                return
            joined = 0
            for _, session, *_ in accounts[:count]:
                account = next(a for a in accounts if a[1] == session)
                if not await service_account_authorized(user_id, account):
                    continue
                if not await session_is_usable(session):
                    continue
                try:
                    await join_super_target_with_stored_account(session, target)
                    joined += 1
                except Exception as exc:
                    logger.info("Owned-account channel join failed: %s", exc)
            await conv.send_message(f"✅ انضم {joined} من أصل {count} حساباً إلى القناة.")
        except (ValueError, asyncio.TimeoutError):
            await conv.send_message("❌ انتهى الوقت أو أُرسل عدد غير صحيح.")
        finally:
            active_conversations.pop(user_id, None)


async def validate_service_count(conv, accounts, count):
    if count <= 0:
        await conv.send_message("❌ العدد يجب أن يكون أكبر من صفر!")
        return False
    if not accounts:
        await conv.send_message("❌ ليس لديك حسابات مخزنة!")
        return False
    if count > len(accounts):
        await conv.send_message(f"❌ عدد الحسابات المتاحة هو {len(accounts)} فقط!")
        return False
    return True


async def single_react_handler(event):
    """Let the user explicitly react once from one of their stored accounts."""
    user_id = event.chat_id
    accounts = await get_service_accounts_for_user(user_id)
    if not accounts:
        await event.answer("❌ لا توجد حسابات مخزنة!", alert=True)
        return

    account_phones = "\n".join(f"• {account[0]}" for account in accounts)
    async with event.client.conversation(user_id) as conv:
        active_conversations[user_id] = conv
        try:
            await conv.send_message("أرسل رابط المنشور:", buttons=cancel_operation_keyboard())
            url = await conv.get_response(timeout=300)
            await conv.send_message("أرسل الإيموجي المطلوب (مثال: ❤️):", buttons=cancel_operation_keyboard())
            emoji = await conv.get_response(timeout=300)
            await conv.send_message(
                f"اختر رقم حساب واحد من القائمة التالية وأرسله كما هو:\n{account_phones}",
                buttons=cancel_operation_keyboard(),
            )
            phone_response = await conv.get_response(timeout=300)
            account = next(
                (item for item in accounts if str(item[0]) == phone_response.text.strip()),
                None,
            )
            if not account:
                await conv.send_message("❌ الحساب المحدد غير موجود ضمن حساباتك المخزنة.")
                return

            success = await react_to_message(account[1], url.text.strip(), emoji.text.strip())
            await conv.send_message(
                "✅ تم إرسال التفاعل من الحساب المحدد." if success
                else "❌ تعذر إرسال التفاعل. تأكد من الرابط، الإيموجي، وصلاحية جلسة الحساب."
            )
        except asyncio.TimeoutError:
            await conv.send_message("⏱ انتهى الوقت المحدد للإدخال!")
        finally:
            active_conversations.pop(user_id, None)


async def mass_react_handler(event):
    """معالج التفاعل الجماعي"""
    user_id = event.chat_id
    async with event.client.conversation(user_id) as conv:
        active_conversations[user_id] = conv
        try:
            await conv.send_message("أرسل رابط المنشور:", buttons=cancel_operation_keyboard())
            url = await conv.get_response(timeout=300)
            
            await conv.send_message("أرسل الإيموجي المطلوب (مثال: ❤️):", buttons=cancel_operation_keyboard())
            emoji = await conv.get_response(timeout=300)
            
            await conv.send_message("أرسل عدد الحسابات التي تريد استخدامها:", buttons=cancel_operation_keyboard())
            count_resp = await conv.get_response(timeout=300)
            
            try:
                count = int(count_resp.text)
                accounts = await get_service_accounts_for_user(user_id)
                if not await validate_service_count(conv, accounts, count):
                    return
                success = await mass_react(user_id, url.text, emoji.text, count, accounts=accounts)
                await conv.send_message(f"✅ تم التفاعل باستخدام {success} حساب!")
            except ValueError:
                await conv.send_message("عدد غير صحيح!")
        except asyncio.TimeoutError:
            await conv.send_message("⏱ انتهى الوقت المحدد للإدخال!")
        finally:
            if user_id in active_conversations:
                del active_conversations[user_id]


async def mass_vote_handler(event):
    """معالج التصويت الجماعي"""
    user_id = event.chat_id
    async with event.client.conversation(user_id) as conv:
        active_conversations[user_id] = conv
        try:
            await conv.send_message("أرسل رابط الاستفتاء:", buttons=cancel_operation_keyboard())
            url = await conv.get_response(timeout=300)
            
            await conv.send_message("أرسل رقم الخيار المراد التصويت له:", buttons=cancel_operation_keyboard())
            option = await conv.get_response(timeout=300)
            
            await conv.send_message("أرسل عدد الحسابات التي تريد استخدامها:", buttons=cancel_operation_keyboard())
            count_resp = await conv.get_response(timeout=300)
            
            try:
                count = int(count_resp.text)
                accounts = await get_service_accounts_for_user(user_id)
                if not await validate_service_count(conv, accounts, count):
                    return
                success = await mass_vote(user_id, url.text, int(option.text), count, accounts=accounts)
                await conv.send_message(f"✅ تم التصويت باستخدام {success} حساب!")
            except ValueError:
                await conv.send_message("قيمة غير صالحة!")
        except asyncio.TimeoutError:
            await conv.send_message("⏱ انتهى الوقت المحدد للإدخال!")
        finally:
            if user_id in active_conversations:
                del active_conversations[user_id]


async def mass_comment_handler(event):
    """معالج التعليق الجماعي"""
    user_id = event.chat_id
    async with event.client.conversation(user_id) as conv:
        active_conversations[user_id] = conv
        try:
            await conv.send_message("أرسل رابط المنشور:", buttons=cancel_operation_keyboard())
            url = await conv.get_response(timeout=300)
            
            await conv.send_message("أرسل نص التعليق:", buttons=cancel_operation_keyboard())
            comment = await conv.get_response(timeout=300)
            
            await conv.send_message("أرسل عدد الحسابات التي تريد استخدامها:", buttons=cancel_operation_keyboard())
            count_resp = await conv.get_response(timeout=300)
            
            try:
                count = int(count_resp.text)
                accounts = await get_service_accounts_for_user(user_id)
                if not await validate_service_count(conv, accounts, count):
                    return
                success = await mass_comment(user_id, url.text, comment.text, count, accounts=accounts)
                if success == 0:
                    await conv.send_message("❌ لم يتم إرسال أي تعليق. تأكد أن المنشور يدعم التعليقات وأن الحسابات لديها صلاحية الكتابة في مجموعة النقاش.")
                    return
                await conv.send_message(f"✅ تم التعليق باستخدام {success} حساب!")
            except ValueError:
                await conv.send_message("عدد غير صحيح!")
        except asyncio.TimeoutError:
            await conv.send_message("⏱ انتهى الوقت المحدد للإدخال!")
        finally:
            if user_id in active_conversations:
                del active_conversations[user_id]


async def increment_views_handler(event):
    """معالج زيادة المشاهدات"""
    user_id = event.chat_id
    async with event.client.conversation(user_id) as conv:
        active_conversations[user_id] = conv
        try:
            await conv.send_message("أرسل رابط المنشور:", buttons=cancel_operation_keyboard())
            url = await conv.get_response(timeout=300)
            
            await conv.send_message("أرسل عدد المشاهدات المطلوبة:", buttons=cancel_operation_keyboard())
            count_resp = await conv.get_response(timeout=300)
            
            try:
                count = int(count_resp.text)
                accounts = await get_service_accounts_for_user(user_id)
                if not await validate_service_count(conv, accounts, count):
                    return
                success = await increment_views(user_id, url.text, count, accounts=accounts)
                await conv.send_message(f"✅ تم زيادة المشاهدات بمقدار {success}!")
            except ValueError:
                await conv.send_message("عدد غير صحيح!")
        except asyncio.TimeoutError:
            await conv.send_message("⏱ انتهى الوقت المحدد للإدخال!")
        finally:
            if user_id in active_conversations:
                del active_conversations[user_id]


async def mass_story_handler(event):
    """Story campaign using only the requester's owned or rented accounts."""
    user_id = event.chat_id
    accounts = await get_service_accounts_for_user(user_id)
    if not accounts:
        await event.answer("❌ لا توجد حسابات تملكها أو تستأجرها.", alert=True)
        return
    async with event.client.conversation(user_id) as conv:
        active_conversations[user_id] = conv
        try:
            await conv.send_message("أرسل رابط القصة (مثال: https://t.me/username/s/123):", buttons=cancel_operation_keyboard())
            story_url = (await conv.get_response(timeout=300)).text.strip()
            if not all(extract_story_target_from_url(story_url)):
                await conv.send_message("❌ رابط القصة غير صالح أو غير عام.")
                return
            await conv.send_message("أرسل إيموجي التفاعل المطلوب (مثال: ❤️):", buttons=cancel_operation_keyboard())
            emoji = (await conv.get_response(timeout=300)).text.strip()
            await conv.send_message(f"أرسل عدد الحسابات المطلوب استخدامه (المتاح: {len(accounts)}):", buttons=cancel_operation_keyboard())
            count = int((await conv.get_response(timeout=300)).text.strip())
            if not await validate_service_count(conv, accounts, count):
                return
            await conv.send_message("⏳ جارٍ تنفيذ مشاهدة القصة والتفاعل عليها…")
            views, reactions = await mass_story_view_and_react(accounts, story_url, emoji, count, user_id=user_id)
            await conv.send_message(f"✅ تمت المشاهدة من {views} حساب والتفاعل من {reactions} حساب.")
        except (ValueError, asyncio.TimeoutError):
            await conv.send_message("❌ انتهى الوقت أو أُرسلت قيمة غير صحيحة.")
        finally:
            active_conversations.pop(user_id, None)


async def save_post_account_menu(event):
    user_id = event.chat_id
    accounts = await get_stored_accounts(user_id)
    if not accounts:
        await event.answer("❌ لا توجد حسابات مخزنة!", alert=True)
        return

    item_buttons = [
        Button.inline(f"+{account[0]}", data=f"save_post_account:{account[0]}")
        for account in accounts
    ]
    buttons = arrange_buttons(item_buttons)
    buttons.append([Button.inline("• رجوع • ↩️", data="control_settings_account")])
    await event.edit("📱 اختر الحساب الذي تريد استخدامه لجلب المنشور:", buttons=buttons)


async def save_post_account_selected(event, data):
    user_id = event.chat_id
    phone = data.split(":", 1)[1]
    account = await get_owned_stored_account(user_id, phone)
    if not account:
        await event.answer("❌ لم يتم العثور على الحساب!", alert=True)
        return

    async with event.client.conversation(user_id) as conv:
        active_conversations[user_id] = conv
        try:
            await conv.send_message(
                f"أرسل رابط المنشور للحساب +{phone}:",
                buttons=cancel_operation_keyboard()
            )
            post_resp = await conv.get_response(timeout=300)
            await save_allowed_post(conv, account, post_resp.text.strip())
        except asyncio.TimeoutError:
            await conv.send_message("⏱ انتهى الوقت المحدد للإدخال!")
        finally:
            if user_id in active_conversations:
                del active_conversations[user_id]


async def save_allowed_post(conv, account, post_link):
    phone, session = account[0], account[1]
    entity, msg_id = await extract_entity_from_url(post_link)
    if not entity or not msg_id:
        await conv.send_message("❌ رابط المنشور غير صالح.")
        return

    client = None
    file_path = None
    try:
        client = TelegramClient(StringSession(session), API_ID, API_HASH)
        await client.connect()
        if isinstance(entity, str):
            entity = await resolve_posting_target(client, entity)

        message = await client.get_messages(entity, ids=msg_id)
        if not message:
            await conv.send_message("❌ لم أستطع العثور على المنشور عبر هذا الحساب.")
            return

        chat = await message.get_chat()
        if getattr(message, "noforwards", False) or getattr(chat, "noforwards", False):
            await conv.send_message(
                "❌ هذا المنشور محمي من الحفظ أو التوجيه بواسطة تيليجرام، لذلك لا يمكن نسخه."
            )
            return

        caption = message.text or ""
        if message.media:
            save_dir = "/tmp/number_saved_posts"
            os.makedirs(save_dir, exist_ok=True)
            file_path = await client.download_media(message, file=save_dir)
            if not file_path:
                await conv.send_message("❌ فشل تنزيل ميديا المنشور.")
                return
            await conv.send_file(
                file_path,
                caption=caption or f"✅ تم جلب المنشور بنجاح بواسطة الحساب +{phone}"
            )
        elif message.text:
            await conv.send_message(message.text)
        else:
            await conv.send_message("❌ المنشور لا يحتوي على نص أو ميديا قابلة للجلب.")
            return

        await conv.send_message(f"✅ تم جلب المنشور بنجاح بواسطة الحساب +{phone}.")
    except Exception as exc:
        await conv.send_message(f"❌ فشل جلب المنشور: {exc}")
    finally:
        if file_path:
            with contextlib.suppress(Exception):
                os.remove(file_path)
        if client and client.is_connected():
            await client.disconnect()
