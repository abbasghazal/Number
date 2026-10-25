# Auto-split from handlers.py. Keep shared imports/state in base.py.
from .base import *
from account_importer import validate_phone_submission
import re


async def auction_menu_handler(event):
    await event.edit("**🏷️︙قائمة المزاد:**", buttons=auction_menu_keyboard())


async def add_auction_handler(event):
    from typed_auctions import create_panel
    return await create_panel(event)


async def add_username_auction_handler(event):
    """Create a public-username listing without ever collecting account credentials."""
    if not await enforce_rate_limit(event, "create_username_auction", 10):
        return
    async with event.client.conversation(event.chat_id, exclusive=False) as conv:
        active_conversations[event.chat_id] = conv
        try:
            await conv.send_message(
                "أرسل اليوزر المراد عرضه، مثال: @example\n\n"
                "تنبيه: لا ترسل رقم هاتف أو كود تسجيل أو جلسة. البوت لا يحتفظ ببيانات دخول اليوزرات.",
                buttons=cancel_operation_keyboard(),
            )
            response = await conv.get_response(timeout=300)
            username = response.text.strip().lstrip("@").lower()
            if not re.fullmatch(r"[a-z][a-z0-9_]{4,31}", username):
                await conv.send_message("❌ اليوزر غير صالح. استخدم 5–32 حرفاً/رقماً/شرطة سفلية ويبدأ بحرف.")
                return
            await conv.send_message("🔎 جارٍ التحقق من حالة اليوزر على TonAPI…")
            listed_on_ton, tonapi_error = await is_username_listed_on_tonapi(username)
            if listed_on_ton:
                await conv.send_message("❌ رُفض اليوزر لأنه ظاهر ضمن مزاد/إدراج TON لنطاق t.me. لا يمكن إضافته إلى مزاد البوت.")
                return
            if listed_on_ton is None:
                # A 5xx response is an upstream failure, not evidence that a
                # username is listed. Preserve the reason in logs and allow
                # the seller to continue instead of falsely rejecting it.
                logger.warning("TonAPI username-auction check unavailable for @%s: %s", username, tonapi_error)
                await conv.send_message(f"⚠️ تعذر التحقق من TonAPI حالياً ({tonapi_error}). لا يوجد إدراج مؤكد، لذا سيستمر إنشاء المزاد.")
            await conv.send_message("أدخل أقل سعر للمزاد (أكثر من 0.5$):", buttons=cancel_operation_keyboard())
            price_response = await conv.get_response(timeout=300)
            try:
                min_price = parse_positive_amount(price_response.text, minimum=0.49)
            except ValueError:
                await conv.send_message("❌ السعر غير صالح!")
                return
            existing = await execute_query(
                """SELECT auction_id FROM username_auctions
                   WHERE username=? AND status IN ('active', 'awaiting_transfer', 'processing')""",
                (username,), fetchone=True,
            )
            if existing:
                await conv.send_message("❌ هذا اليوزر لديه مزاد مفتوح بالفعل ولا يمكن عرضه مرتين في الوقت نفسه.")
                return
            row = await execute_query(
                """INSERT INTO username_auctions (username, seller_id, min_price, current_bid, status)
                   VALUES (?, ?, ?, ?, 'active') RETURNING auction_id""",
                (username, event.chat_id, min_price, min_price), fetchone=True, commit=True,
            )
            if not row:
                await conv.send_message("❌ هذا اليوزر معروض بالفعل أو تعذر إنشاء المزاد.")
                return
            sent_count, failed_count = await broadcast_username_auction(event, row[0], username, min_price)
            await conv.send_message(
                f"✅ تم إنشاء مزاد اليوزر @{username}.\n"
                "سيُعرض للمزايدة فقط؛ نقل اليوزر يتم يدوياً بعد موافقة الطرفين.\n"
                f"📢 تم إرسال إشعار المزاد إلى {sent_count} مستخدم.\n"
                f"❌ فشل الإرسال إلى {failed_count} مستخدم."
            )
        except asyncio.TimeoutError:
            await conv.send_message("⏱ انتهى الوقت المحدد للإدخال!")
        finally:
            if active_conversations.get(event.chat_id) is conv:
                active_conversations.pop(event.chat_id, None)


async def username_auction_list_handler(event):
    auctions = await execute_query(
        "SELECT auction_id, username, current_bid FROM username_auctions WHERE status='active' ORDER BY auction_id DESC",
        fetchall=True,
    ) or []
    if not auctions:
        await event.answer("❌ لا توجد مزادات يوزرات نشطة حالياً!", alert=True)
        return
    buttons = arrange_buttons([
        Button.inline(f"@{username} — {bid}$", data=f"view_username_auction:{auction_id}")
        for auction_id, username, bid in auctions
    ])
    buttons.append([Button.inline("• رجوع • ↩️", data="auction_menu")])
    await event.edit("🏷️ مزادات اليوزرات النشطة:", buttons=buttons)


async def broadcast_username_auction(event, auction_id, username, min_price):
    users = await execute_query("SELECT user_id FROM users", fetchall=True) or []
    from outbound_queue import queue_broadcast
    report = await queue_broadcast(
        [user[0] for user in users if user[0] != event.chat_id],
        f"🏷️ **تم إضافة مزاد يوزر جديد!**\n\n"
        f"👤 اليوزر: @{username}\n"
        f"💰 أقل سعر: {min_price}$\n\n"
        "اضغط الزر أدناه لمشاهدة المزاد وتقديم عرض.",
        buttons=[[Button.inline("عرض المزاد", data=f"view_username_auction:{auction_id}")]],
        parse_mode="markdown",
    )
    return report.success, report.failed


async def view_username_auction_handler(event, data):
    auction_id = int(data.split(":", 1)[1])
    auction = await execute_query(
        "SELECT username, seller_id, min_price, current_bid, status, transfer_channel_id FROM username_auctions WHERE auction_id=?",
        (auction_id,), fetchone=True,
    )
    if not auction:
        await event.answer("❌ هذا المزاد غير متاح.", alert=True)
        return
    username, seller_id, min_price, current_bid, status, transfer_channel_id = auction
    seller = await get_user(seller_id) or {}
    seller_name = seller.get("username") or f"المستخدم {seller_id}"
    buttons = []
    if status == "active":
        buttons.append([Button.inline("تقديم عرض", data=f"username_bid:{auction_id}")])
        if event.chat_id == seller_id:
            buttons.append([Button.inline("ربط قناة التحويل", data=f"bind_username_channel:{auction_id}")])
            buttons.append([Button.inline("إنهاء المزاد", data=f"end_username_auction:{auction_id}")])
    buttons.append([Button.inline("• رجوع • ↩️", data="username_auction_list")])
    await event.edit(
        f"🏷️ **مزاد يوزر:** @{username}\n"
        f"👤 البائع: {seller_name}\n"
        f"💰 أقل سعر: {min_price}$\n"
        f"🚀 أعلى عرض: {current_bid}$\n\n"
        "✅ قناة التحويل مربوطة." if transfer_channel_id else "⚠️ يجب على البائع ربط قناة التحويل ورفع البوت مشرفاً قبل إنهاء المزاد.",
        buttons=buttons,
    )


async def bind_username_channel_handler(event, data):
    try:
        auction_id = int(data.split(":", 1)[1])
    except (IndexError, ValueError):
        await event.answer("❌ بيانات المزاد غير صالحة.", alert=True)
        return
    auction = await execute_query("SELECT seller_id, status FROM username_auctions WHERE auction_id=?", (auction_id,), fetchone=True)
    if not auction or auction[0] != event.chat_id or auction[1] != "active":
        await event.answer("❌ ربط القناة متاح لصاحب المزاد النشط فقط.", alert=True)
        return
    async with event.client.conversation(event.chat_id, exclusive=False) as conv:
        active_conversations[event.chat_id] = conv
        try:
            await conv.send_message(
                "أرسل معرف قناة التحويل العامة، مثل @mychannel. يجب أن يكون البوت مشرفاً فيها وأن تكون قناة وليست مجموعة.",
                buttons=cancel_operation_keyboard(),
            )
            entity = await event.client.get_entity((await conv.get_response(timeout=300)).text.strip())
            if not isinstance(entity, types.Channel) or not getattr(entity, "broadcast", False):
                await conv.send_message("❌ أرسل معرف قناة عامة صالحة، وليس مجموعة أو مستخدماً.")
                return
            me = await event.client.get_me()
            participant = (await event.client(GetParticipantRequest(entity, me.id))).participant
            if not isinstance(participant, types.ChannelParticipantCreator) and not getattr(participant, "admin_rights", None):
                await conv.send_message("❌ ارفع البوت مشرفاً في القناة أولاً ثم أعد المحاولة.")
                return
            saved = await execute_query(
                """UPDATE username_auctions SET transfer_channel_id=?, transfer_channel_hash=?, transfer_bound_at=CURRENT_TIMESTAMP
                   WHERE auction_id=? AND seller_id=? AND status='active' RETURNING auction_id""",
                (entity.id, entity.access_hash, auction_id, event.chat_id), fetchone=True, commit=True,
            )
            if saved:
                await conv.send_message(
                    "✅ تم ربط القناة. يمكنك الآن قبول أعلى عرض، ثم انقل اليوزر إلى القناة وملكية القناة للفائز.",
                    buttons=[[Button.inline("✅ قبول أعلى عرض", data=f"close_username_auction:{auction_id}")]],
                )
            else:
                await conv.send_message("❌ تعذر حفظ قناة التحويل.")
        except asyncio.TimeoutError:
            await conv.send_message("⏱ انتهى وقت ربط القناة.")
        except Exception:
            logger.exception("Failed to bind username auction channel: %s", auction_id)
            await conv.send_message("❌ تعذر التحقق من القناة. تأكد من معرفها ومن صلاحيات البوت.")
        finally:
            if active_conversations.get(event.chat_id) is conv:
                active_conversations.pop(event.chat_id, None)


async def place_username_bid_handler(event, data):
    try:
        auction_id = int(data.split(":", 1)[1])
    except (IndexError, ValueError):
        await event.answer("❌ بيانات المزاد غير صالحة.", alert=True)
        return
    auction = await execute_query("SELECT username, current_bid, status, seller_id FROM username_auctions WHERE auction_id=?", (auction_id,), fetchone=True)
    if not auction or auction[2] != "active":
        await event.answer("❌ هذا المزاد لم يعد متاحاً.", alert=True)
        return
    if int(auction[3]) == int(event.chat_id):
        await event.answer("❌ لا يمكنك المزايدة على مزادك.", alert=True)
        return
    minimum = minimum_auction_bid(auction[1])
    async with event.client.conversation(event.chat_id, exclusive=False) as conv:
        active_conversations[event.chat_id] = conv
        try:
            await conv.send_message(f"أعلى عرض: {auction[1]}$\nأقل عرض جديد: {minimum:.2f}$\n\nأدخل عرضك:", buttons=cancel_operation_keyboard())
            response = await conv.get_response(timeout=300)
            amount = parse_positive_amount(response.text)
            # Conditional update prevents accepting a stale or lower competing bid.
            if amount < minimum:
                await conv.send_message(f"❌ يجب أن يكون العرض {minimum:.2f}$ أو أكثر.")
                return
            updated = await execute_query(
                """UPDATE username_auctions SET current_bid=?, current_bidder=?
                   WHERE auction_id=? AND status='active'
                     AND seller_id<>?
                     AND ? >= GREATEST(current_bid + 0.10, current_bid * 1.05)
                   RETURNING seller_id, username""",
                (amount, event.chat_id, auction_id, event.chat_id, amount), fetchone=True, commit=True,
            )
            if not updated:
                await conv.send_message("❌ تغيّر أعلى عرض أو أن عرضك أقل من الحد الأدنى. افتح المزاد مجدداً.")
                return
            await execute_query("INSERT INTO username_auction_bids (auction_id, user_id, bid_amount) VALUES (?, ?, ?)", (auction_id, event.chat_id, amount), commit=True)
            await conv.send_message("✅ تم تسجيل عرضك بنجاح!")
            from outbound_queue import queue_message
            await queue_message(
                updated[0],
                f"🏷️ عرض جديد على @{updated[1]}\n"
                f"💰 العرض: {amount}$\n"
                f"👤 المزايد: {event.chat_id}\n\n"
                "يمكنك قبول أعلى عرض بعد ربط قناة التحويل، أو ترك المزاد مفتوحاً لاستقبال عروض أخرى.",
                buttons=[
                    [Button.inline("🔗 ربط قناة التحويل", data=f"bind_username_channel:{auction_id}")],
                    [Button.inline("✅ قبول العرض", data=f"close_username_auction:{auction_id}")],
                    [Button.inline("⏭️ تجاهل / استمرار المزاد", data=f"ignore_username_bid:{auction_id}")],
                ],
            )
        except (asyncio.TimeoutError, ValueError):
            await conv.send_message("❌ انتهت المهلة أو أن قيمة العرض غير صالحة.")
        finally:
            if active_conversations.get(event.chat_id) is conv:
                active_conversations.pop(event.chat_id, None)


async def close_username_auction_handler(event, data):
    """Close a listing; the asset transfer remains an explicit manual agreement."""
    try:
        auction_id = int(data.split(":", 1)[1])
    except (IndexError, ValueError):
        await event.answer("❌ بيانات المزاد غير صالحة.", alert=True)
        return
    auction = await execute_query(
        """SELECT username, seller_id, current_bid, current_bidder, transfer_channel_id, transfer_channel_hash
           FROM username_auctions WHERE auction_id=? AND status='active'""",
        (auction_id,), fetchone=True,
    )
    if not auction or auction[1] != event.chat_id:
        await event.answer("❌ لا يمكنك إنهاء هذا المزاد.", alert=True)
        return
    if not auction[4] or not auction[5]:
        await event.answer("اربط قناة التحويل أولاً.", alert=True)
        try:
            await event.edit(
                "⚠️ قبل قبول العرض: أرسل قناة التحويل وارفع البوت مشرفاً فيها، ثم اضغط زر الربط أدناه.",
                buttons=[
                    [Button.inline("🔗 ربط قناة التحويل", data=f"bind_username_channel:{auction_id}")],
                    [Button.inline("⏭️ استمرار المزاد", data=f"ignore_username_bid:{auction_id}")],
                ],
            )
        except Exception:
            pass
        return
    if not auction[3]:
        await event.answer("❌ لا يمكن إنهاء المزاد من دون عرض فائز.", alert=True)
        return
    from helpers import reserve_username_auction_atomically
    closed, reason, buyer_id, price, _fee = await reserve_username_auction_atomically(auction_id, event.chat_id)
    if not closed:
        if reason == "insufficient_funds":
            message = "❌ الرصيد النقدي المتاح للفائز غير كافٍ؛ لم يُقبل العرض."
        elif reason == "self_bid":
            message = "❌ لا يمكن للبائع شراء مزاده."
        else:
            message = "❌ تغيّر المزاد أو لم يعد متاحاً."
        await event.answer(message, alert=True)
        return
    username, _seller_id, _shown_price, _old_bidder, _channel_id, _channel_hash = auction
    bidder = buyer_id
    await event.answer("✅ بانتظار نقل الملكية والتحقق.", alert=True)
    if bidder:
        from outbound_queue import queue_message
        await queue_message(
            event.chat_id,
            f"✅ تم حجز {price}$ من الرصيد النقدي للفائز لمزاد @{username}.\n\n"
            "انقل اليوزر إلى قناة التحويل، ثم انقل ملكية القناة إلى الفائز خلال 24 ساعة. أبقِ البوت مشرفاً حتى يكتمل التحقق. إذا انتهت المهلة، يبقى المبلغ محجوزاً للمراجعة ولا يُعاد تلقائياً.",
        )
        await queue_message(
            bidder,
            f"🏷️ تم قبول عرضك على @{username} بقيمة {price}$ وحُجز المبلغ نقداً في الضمان.\n\n"
            "ينتظر البائع الآن نقل اليوزر وملكية القناة إليك خلال 24 ساعة. بعد اكتمال النقل اضغط للتحقق. إذا انتهت المهلة، يبقى المبلغ محجوزاً للمراجعة ولا يُعاد تلقائياً:",
            buttons=[[Button.inline("تحقق من ملكية القناة", data=f"verify_username_transfer:{auction_id}")]],
        )


async def end_username_auction_handler(event, data):
    """Cancel an open username auction without transferring funds or ownership."""
    try:
        auction_id = int(data.split(":", 1)[1])
    except (IndexError, ValueError):
        await event.answer("❌ بيانات المزاد غير صالحة.", alert=True)
        return
    auction = await execute_query(
        "SELECT username, seller_id, current_bidder FROM username_auctions WHERE auction_id=? AND status='active'",
        (auction_id,), fetchone=True,
    )
    if not auction or auction[1] != event.chat_id:
        await event.answer("❌ لا يمكنك إنهاء هذا المزاد.", alert=True)
        return
    cancelled = await execute_query(
        "UPDATE username_auctions SET status='cancelled' WHERE auction_id=? AND status='active' RETURNING auction_id",
        (auction_id,), fetchone=True, commit=True,
    )
    if not cancelled:
        await event.answer("❌ هذا المزاد لم يعد نشطاً.", alert=True)
        return
    username, _seller_id, bidder = auction
    await event.answer("✅ تم إنهاء المزاد دون بيع أو تسوية مالية.", alert=True)
    try:
        await event.edit(f"🏷️ تم إنهاء مزاد @{username} وإزالته من المزادات النشطة.")
    except Exception:
        pass
    if bidder:
        from outbound_queue import queue_message
        await queue_message(bidder, f"ℹ️ تم إنهاء مزاد @{username} من البائع دون إتمام البيع. لم يُخصم أي رصيد منك.")


async def ignore_username_bid_handler(event, data):
    """Acknowledge the seller's choice to keep the username auction open."""
    try:
        auction_id = int(data.split(":", 1)[1])
    except (IndexError, ValueError):
        await event.answer("❌ بيانات المزاد غير صالحة.", alert=True)
        return
    auction = await execute_query(
        "SELECT seller_id, status FROM username_auctions WHERE auction_id=?", (auction_id,), fetchone=True,
    )
    if not auction or auction[0] != event.chat_id or auction[1] != "active":
        await event.answer("❌ هذا المزاد لم يعد متاحاً.", alert=True)
        return
    await event.answer("✅ سيبقى المزاد مفتوحاً لاستقبال عروض جديدة.", alert=True)
    try:
        await event.edit("🏷️ تم تجاهل العرض الحالي. المزاد ما زال مفتوحاً لاستقبال عروض جديدة.")
    except Exception:
        pass


async def verify_username_transfer_handler(event, data):
    try:
        auction_id = int(data.split(":", 1)[1])
    except (IndexError, ValueError):
        await event.answer("❌ بيانات التحقق غير صالحة.", alert=True)
        return
    auction = await execute_query(
        """SELECT username, seller_id, current_bidder, transfer_channel_id, transfer_channel_hash
           FROM username_auctions WHERE auction_id=? AND status='awaiting_transfer'""",
        (auction_id,), fetchone=True,
    )
    if not auction or auction[2] != event.chat_id:
        await event.answer("❌ التحقق متاح لصاحب العرض الفائز فقط.", alert=True)
        return
    username, seller_id, buyer_id, channel_id, access_hash = auction
    try:
        channel = InputPeerChannel(channel_id, access_hash)
        entity = await event.client.get_entity(channel)
        if (getattr(entity, "username", "") or "").lower() != username.lower():
            await event.answer("❌ اليوزر لم يُسند إلى قناة التحويل بعد.", alert=True)
            return
        owner = (await event.client(GetParticipantRequest(channel, buyer_id))).participant
        if not isinstance(owner, types.ChannelParticipantCreator):
            await event.answer("❌ لا تزال ملكية القناة غير منقولة إلى حسابك.", alert=True)
            return
        me = await event.client.get_me()
        bot_participant = (await event.client(GetParticipantRequest(channel, me.id))).participant
        if not isinstance(bot_participant, types.ChannelParticipantCreator) and not getattr(bot_participant, "admin_rights", None):
            await event.answer("❌ يجب أن يبقى البوت مشرفاً لإتمام التحقق.", alert=True)
            return
    except Exception:
        logger.exception("Username transfer verification failed: %s", auction_id)
        await event.answer("❌ تعذر التحقق الآن. تأكد من بقاء البوت مشرفاً وأعد المحاولة.", alert=True)
        return
    settled, fee, seller_amount = await settle_username_auction_atomically(auction_id, buyer_id)
    if not settled:
        await event.answer("❌ تعذر إتمام التسوية؛ تأكد من أن رصيدك ما زال كافياً ثم أعد المحاولة.", alert=True)
        return
    await event.answer("✅ تم التحقق ونقل الرصيد بنجاح.", alert=True)
    from outbound_queue import queue_message
    await queue_message(
        seller_id,
        f"✅ اكتمل التحقق من مزاد @{username}: تم نقل ملكية القناة إلى الفائز.\n"
        f"💵 صافي المبلغ المضاف إلى رصيدك: {seller_amount:.2f}$\n"
        f"🏷️ عمولة المنصة: {fee:.2f}$",
    )


async def broadcast_auction(event, auction_id, phone, min_price):
    users = await execute_query("SELECT user_id FROM users", fetchall=True) or []
    from outbound_queue import queue_broadcast
    report = await queue_broadcast(
        [user[0] for user in users],
        f"🏷️ **تم إضافة مزاد جديد!**\n\n📱 الرقم: +{phone}\n💰 أقل سعر: {min_price}$\n\nاضغط على الزر أدناه لتقديم عرض:",
        buttons=[[Button.inline("تقديم عرض", data=f"bid:{auction_id}")]],
        parse_mode="markdown",
    )
    return report.success, report.failed


async def place_bid_handler(event, data):
    if not await enforce_rate_limit(event, "place_bid", 3):
        return
    try:
        auction_id = int(data.split(":", 1)[1])
    except (IndexError, ValueError):
        await event.answer("❌ بيانات المزاد غير صالحة.", alert=True)
        return
    auction = await execute_query(
        "SELECT * FROM auctions WHERE auction_id=?", 
        (auction_id,), 
        fetchone=True
    )
    
    if not auction or auction[7] != 'active':  # status
        await event.answer("❌ هذا المزاد لم يعد متاحاً!", alert=True)
        return
    
    current_bid = auction[5]  # current_bid
    minimum_bid = minimum_auction_bid(current_bid)
    min_increment = Decimal(str(minimum_bid)) - Decimal(str(current_bid))
    
    async with event.client.conversation(event.chat_id) as conv:
        try:
            await conv.send_message(
                f"💰 أعلى عرض حالياً: {current_bid}$\n"
                f"⬆️ أقل زيادة مسموحة: {min_increment:.2f}$\n\n"
                "أدخل عرضك:",
                buttons=cancel_operation_keyboard()
            )
            bid_resp = await conv.get_response(timeout=300)
            
            try:
                bid_amount = parse_positive_amount(bid_resp.text)
                if bid_amount < Decimal(str(minimum_bid)):
                    await conv.send_message(f"❌ يجب أن يكون العرض أعلى من أو يساوي {minimum_bid:.2f}$")
                    return
            except ValueError:
                await conv.send_message("❌ قيمة غير صالحة!")
                return
            
            accepted, latest_bid, required_bid = await place_auction_bid_atomically(
                auction_id, event.chat_id, bid_amount
            )
            if not accepted:
                if latest_bid == "database_error":
                    await conv.send_message("❌ تعذر حفظ العرض بسبب خطأ في قاعدة البيانات. حاول مجدداً بعد إعادة تشغيل البوت.")
                elif latest_bid == "self_bid":
                    await conv.send_message("❌ لا يمكنك المزايدة على مزادك.")
                elif latest_bid is None:
                    await conv.send_message("❌ هذا المزاد لم يعد متاحاً.")
                else:
                    await conv.send_message(
                        "❌ تغير أعلى عرض أثناء تقديمك للعرض. "
                        f"أعلى عرض الآن: {latest_bid:.2f}$، والحد الأدنى الجديد: {required_bid:.2f}$"
                    )
                return
            
            # إشعار البائع
            from outbound_queue import queue_message
            await queue_message(
                auction[3],  # seller_id
                f"🏷️ **تم تقديم عرض جديد على مزادك!**\n\n"
                f"📱 الرقم: +{auction[1]}\n"
                f"💰 العرض: {bid_amount}$\n"
                f"👤 المزايد: {event.chat_id}\n\n"
                "اختر الإجراء:",
                buttons=[
                    [Button.inline("بيع", data=f"sell_auction:{auction_id}:{event.chat_id}")],
                    [Button.inline("استمرار المزاد", data=f"continue_auction:{auction_id}")]
                ]
            )
            
            await conv.send_message("✅ تم تقديم عرضك بنجاح!")
        except asyncio.TimeoutError:
            await conv.send_message("⏱ انتهى الوقت المحدد للإدخال!")


async def continue_auction_handler(event, data):
    try:
        auction_id = int(data.split(":", 1)[1])
    except (ValueError, IndexError):
        await event.answer("❌ بيانات المزاد غير صالحة!", alert=True)
        return

    auction = await execute_query(
        "SELECT * FROM auctions WHERE auction_id=?",
        (auction_id,),
        fetchone=True
    )
    if not auction or auction[7] != 'active':
        await event.answer("❌ هذا المزاد لم يعد متاحاً!", alert=True)
        return

    if event.chat_id != auction[3]:
        await event.answer("❌ هذه العملية متاحة لصاحب المزاد فقط.", alert=True)
        return

    await event.answer("✅ سيستمر المزاد واستقبال العروض.", alert=True)
    try:
        await event.edit(
            f"🏷️ **المزاد مستمر**\n\n"
            f"📱 الرقم: +{auction[1]}\n"
            f"💰 أعلى عرض حالي: {auction[5]}$\n"
            f"👤 صاحب أعلى عرض: {auction[6] or 'لا يوجد'}"
        )
    except Exception:
        pass


async def sell_auction_handler(event, data):
    auction_id = int(event.data.decode().split(":")[1])
    buyer_id = int(event.data.decode().split(":")[2])
    
    auction = await execute_query(
        "SELECT * FROM auctions WHERE auction_id=?", 
        (auction_id,), 
        fetchone=True
    )
    if not auction or auction[7] != 'active':  # status
        await event.answer("❌ هذا المزاد لم يعد متاحاً!", alert=True)
        return

    if event.chat_id != auction[3]:
        await event.answer("❌ هذه العملية متاحة لصاحب المزاد فقط.", alert=True)
        return

    if auction[6] != buyer_id:  # current_bidder
        await event.answer("❌ هذا العرض لم يعد العرض الأعلى حالياً!", alert=True)
        return

    buyer_data = await get_user(buyer_id)
    
    # التحقق من رصيد المشتري عند موافقة صاحب المزاد
    if not buyer_data or buyer_data['coins'] < auction[5]:  # current_bid
        await event.answer("❌ رصيد المشتري غير كافي الآن!", alert=True)
        try:
            from outbound_queue import queue_message
            await queue_message(
                buyer_id,
                f"❌ تم رفض شراء الرقم +{auction[1]} لأن رصيد الأرقام غير كافٍ لإتمام العرض {auction[5]}$."
            )
        except Exception:
            pass
        return
    
    # طلب تسجيل الخروج من البائع
    await event.answer("تم إرسال طلب تسجيل الخروج للبائع", alert=True)
    from outbound_queue import queue_message
    await queue_message(
        auction[3],  # seller_id
        "🔒 يرجى تسجيل الخروج من جميع الجلسات ما عدا هذه الجلسة، ثم اضغط زر 'تم الخروج'",
        buttons=[[Button.inline("تم الخروج", data=f"auction_logout:{auction_id}:{buyer_id}")]]
    )


async def auction_logout_handler(event, data):
    auction_id = int(event.data.decode().split(":")[1])
    buyer_id = int(event.data.decode().split(":")[2])
    
    auction = await execute_query(
        "SELECT * FROM auctions WHERE auction_id=?", 
        (auction_id,), 
        fetchone=True
    )
    if not auction or auction[7] != 'active':  # status
        await event.answer("❌ هذا المزاد لم يعد متاحاً!", alert=True)
        return

    if event.chat_id != auction[3]:
        await event.answer("❌ هذه العملية متاحة لصاحب المزاد فقط.", alert=True)
        return

    if auction[6] != buyer_id:  # current_bidder
        await event.answer("❌ هذا العرض لم يعد العرض الأعلى حالياً!", alert=True)
        return
    
    # التحقق من عدد الجلسات
    auction_session = decrypt_session(auction[2])
    session_count = await count_ses(auction_session)
    
    if session_count > 0:
        await event.answer("❌ لا يزال هناك جلسات نشطة!", alert=True)
        return
    
    # Funds, account delivery and auction state are one atomic operation.
    settled, fee, seller_amount = await settle_auction_atomically(auction_id, buyer_id, auction)
    if not settled:
        await event.answer("❌ رصيد المشتري غير كافي الآن!", alert=True)
        return
    
    # إرسال التنبيهات
    await event.answer("✅ تم البيع بنجاح!", alert=True)
    await queue_message(
        buyer_id,
        f"✅ تم شراء الرقم +{auction[1]} بنجاح!\n"
        f"💰 السعر: {auction[5]}$\n"
        f"🏷️ عمولة المنصة: {fee:.2f}$ (مخصومة من البائع)\n\n"
        "تم تخزين الحساب في قسم الحسابات المخزنة"
    )
    await queue_message(
        auction[3],
        f"✅ تم بيع الرقم +{auction[1]} بنجاح!\n"
        f"💰 قيمة العرض: {auction[5]}$\n"
        f"🏷️ عمولة المنصة: {fee:.2f}$\n"
        f"💵 صافي ما أضيف إلى رصيدك: {seller_amount:.2f}$"
    )
    await publish_purchase_proof(event.client, buyer_id, auction[1], "مزاد", auction[5])


async def auction_list_handler(event):
    auctions = await execute_query(
        "SELECT * FROM auctions WHERE status='active'",
        fetchall=True
    )
    
    if not auctions:
        await event.answer("❌ لا توجد مزادات نشطة حالياً!", alert=True)
        return
    
    item_buttons = []
    for auction in auctions:
        item_buttons.append(Button.inline(
            f"+{auction[1]} - {auction[5]}$",
            data=f"view_auction:{auction[0]}"
        ))
    buttons = arrange_buttons(item_buttons)
    
    buttons.append([Button.inline("• رجوع • ↩️", data="auction_menu")])
    await event.edit("🏷️ المزادات النشطة:", buttons=buttons)


async def view_auction_handler(event, data):
    auction_id = int(event.data.decode().split(":")[1])
    auction = await execute_query(
        "SELECT * FROM auctions WHERE auction_id=?", 
        (auction_id,), 
        fetchone=True
    )
    
    if not auction:
        await event.answer("❌ هذا المزاد لم يعد متاحاً!", alert=True)
        return
    
    seller_info = await get_user(auction[3])
    seller_name = seller_info.get('username', f"المستخدم {auction[3]}")
    
    text = (
        f"🏷️ **مزاد رقم:** +{auction[1]}\n"
        f"👤 البائع: {seller_name}\n"
        f"💰 أقل سعر: {auction[4]}$\n"
        f"🚀 أعلى عرض: {auction[5]}$\n\n"
        "اضغط لتقديم عرض جديد:"
    )
    
    await event.edit(
        text,
        buttons=[
            [Button.inline("تقديم عرض", data=f"bid:{auction_id}")],
            [Button.inline("• رجوع • ↩️", data="auction_list")]
        ]
    )
