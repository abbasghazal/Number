"""Auction listings retain the reviewed number identity until settlement."""

import asyncio
import json

from postgres import connect
from utils.asyncio_compat import to_thread
import number_catalog


def create_sync(user, phone, minimum):
    from commerce import money

    minimum = money(minimum, positive=True)
    if minimum < 0.5:
        raise ValueError("الحد الأدنى للمزاد 0.5$.")
    with connect() as db:
        db.execute("SELECT pg_advisory_xact_lock(hashtext(?))", (phone,))
        account = db.execute(
            """SELECT session,twofa FROM stored_accounts s WHERE phone=? AND user_id=?
            AND NOT EXISTS(SELECT 1 FROM purchased_accounts p WHERE p.phone=s.phone AND p.user_id<>s.user_id)
            FOR UPDATE""",
            (phone, user),
        ).fetchone()
        if (
            not account
            or db.execute(
                "SELECT 1 FROM auctions WHERE phone=? AND status IN ('active','processing')", (phone,)
            ).fetchone()
        ):
            raise ValueError("الحساب غير متاح لإنشاء مزاد.")
        saved = number_catalog.snapshot(db, phone)
        return db.execute(
            """INSERT INTO auctions(phone,session,seller_id,min_price,current_bid,status,twofa,number_snapshot)
            VALUES(?,?,?,?,?,'active',?,?) RETURNING auction_id""",
            (phone, account[0], user, minimum, minimum, account[1], json.dumps(saved, default=str)),
        ).fetchone()[0]


def verify(db, auction_id):
    row = db.execute(
        "SELECT phone,number_snapshot,seller_id FROM auctions WHERE auction_id=? FOR UPDATE", (auction_id,)
    ).fetchone()
    if not row or not row[1]:
        raise ValueError("المزاد يحتاج مراجعة نوع الرقم.")
    number_catalog.verify_snapshot(db, row[0], row[1])
    owner = db.execute("SELECT user_id FROM stored_accounts WHERE phone=? FOR UPDATE", (row[0],)).fetchone()
    purchased = db.execute("SELECT user_id FROM purchased_accounts WHERE phone=? FOR UPDATE", (row[0],)).fetchone()
    if not owner or owner[0] != row[2] or purchased and purchased[0] != row[2]:
        raise ValueError("تغيرت ملكية حساب المزاد.")
    return row[1]


async def create_panel(event):
    import account_access
    from handlers.base import active_conversations
    from handlers.number_handlers import prompt
    from handlers.input_choices import choose, confirm, input_session

    user = event.chat_id
    async with event.client.conversation(user) as conv, input_session(conv):
        active_conversations[user] = conv
        try:
            accounts = await account_access.accounts(user, owned_only=True)
            await conv.send_message(
                "🏷️ إضافة مزاد\nيجب أن يكون الحساب مخزنًا لديك، وأن تعتمد الإدارة نوعه قبل عرضه.\n"
                + "\n".join(a[0] for a in accounts),
                parse_mode=None,
            )
            phone = await choose(conv, "اختر الحساب المراد عرضه:", [(a[0], f"+{a[0]}") for a in accounts])
            if phone not in {a[0] for a in accounts}:
                raise ValueError("الحساب ليس مملوكًا لك.")
            minimum = await prompt(conv, "أقل سعر للمزاد بالدولار (0.5 على الأقل):")
            details = await number_catalog.detail(phone)
            if not await confirm(conv, f"{details}\nأقل سعر: {minimum}$\nنشر المزاد؟"):
                return
            aid = await to_thread(create_sync, user, phone, minimum)
            await conv.send_message(f"✅ نُشر المزاد #{aid}.", parse_mode=None)
        except (ValueError, asyncio.TimeoutError) as exc:
            await conv.send_message(str(exc) or "انتهى وقت الإدخال.", parse_mode=None)
        finally:
            active_conversations.pop(user, None)
