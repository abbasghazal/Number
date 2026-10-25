"""Automatic seller settlement; inventory transfer and credit commit together."""

import asyncio
import json

from telethon import Button

import number_catalog as catalog
from postgres import connect
from utils.asyncio_compat import to_thread
from workflow import query


def complete_sale_sync(user, phone, session):
    """Move a verified seller account into inventory and credit its owner once."""
    with connect() as db:
        db.execute("SELECT user_id FROM users WHERE user_id=? FOR UPDATE", (user,))
        db.execute("SELECT pg_advisory_xact_lock(hashtext(?))", (phone,))
        row = db.execute(
            """SELECT seller_id,price,calling_code,session,twofa,number_snapshot
            FROM pending_sales WHERE phone=? FOR UPDATE""",
            (phone,),
        ).fetchone()
        if not row:
            # A replay cannot issue another credit.
            return False
        if row[0] != user or row[3] != session or not row[5]:
            raise ValueError("بيانات طلب البيع لم تعد صالحة؛ أعد تقديم الرقم.")
        saved = json.loads(row[5])
        current = catalog.snapshot(db, phone, buying=True)
        if any(saved[k] != current[k] for k in ("number_type", "country_id", "type_revision")):
            raise ValueError("تغير التصنيف؛ يجب مراجعة العرض مجددًا.")
        if db.execute("SELECT 1 FROM accounts WHERE phone=?", (phone,)).fetchone():
            raise ValueError("الرقم موجود في المخزون.")
        # A user may sell a number they previously bought from this bot. Lock
        # the live ownership rows and refuse the transfer if ownership changed
        # before the automatic settlement.
        owner_rows = []
        for table in ("stored_accounts", "purchased_accounts"):
            owner = db.execute(
                f"SELECT user_id FROM {table} WHERE phone=? FOR UPDATE", (phone,)
            ).fetchone()
            if owner:
                owner_rows.append(owner[0])
        if any(owner != user for owner in owner_rows):
            raise ValueError("تغيرت ملكية الرقم؛ ألغِ الطلب وأعد تقديمه من المالك الحالي.")

        # Never let jobs/grants from the seller follow the same phone into a
        # new inventory authorization / future owner.
        from session_lifecycle import clear_operational_tasks
        clear_operational_tasks(db, phone, reason="resold")
        db.execute("DELETE FROM stored_accounts WHERE phone=? AND user_id=?", (phone, user))
        db.execute("DELETE FROM purchased_accounts WHERE phone=? AND user_id=?", (phone, user))
        db.execute(
            """INSERT INTO accounts(phone,session,calling_code,twofa,seller_id,country_id,acquisition_cost,is_sold,is_active)
            VALUES(?,?,?,?,?,?,?,FALSE,TRUE)""",
            (phone, row[3], row[2], row[4], user, saved["country_id"], row[1]),
        )
        db.execute(
            """INSERT INTO sold_accounts(phone,session,user_id,seller_id,price) VALUES(?,?,?,?,?)
            ON CONFLICT(phone) DO UPDATE SET session=excluded.session,user_id=excluded.user_id,
            seller_id=excluded.seller_id,price=excluded.price,sold_at=CURRENT_TIMESTAMP""",
            (phone, row[3], user, user, row[1]),
        )
        balance = db.execute(
            "UPDATE users SET coins=coins+? WHERE user_id=? RETURNING coins", (row[1], user)
        ).fetchone()
        if not balance:
            raise ValueError("حساب البائع غير موجود.")
        db.execute(
            """INSERT INTO balance_ledger(user_id,amount,balance_after,operation,reference,number_snapshot)
            VALUES(?,?,?,'account_sale_credit',?,?)""",
            (user, row[1], balance[0], phone, row[5]),
        )
        db.execute(
            "INSERT INTO number_sale_receipts(phone,user_id,amount,number_snapshot) VALUES(?,?,?,?)",
            (phone, user, row[1], row[5]),
        )
        db.execute("DELETE FROM pending_sales WHERE phone=?", (phone,))
        return True


async def seller_offer(event, phone):
    row = await query(
        "SELECT seller_id,price,number_snapshot,review_version FROM pending_sales WHERE phone=?",
        (phone,),
        fetchone=True,
    )
    if not row or row[0] != event.chat_id:
        raise ValueError("طلب البيع غير موجود ضمن حسابك.")
    if not row[2]:
        return await event.edit(
            "❌ هذا طلب قديم لا يحتوي على عرض تلقائي. أعد تقديم الرقم لإتمامه.",
            buttons=[[Button.inline("رجوع", data="sell")]],
            parse_mode=None,
        )
    saved = json.loads(row[2])
    await event.edit(
        f"📱 عرض بيع الرقم +{phone}\nالنوع المعتمد: {catalog.TYPES[saved['number_type']]}\n"
        f"المبلغ الذي يدفعه البوت: {row[1]}$\n"
        "أكمل الخروج من جميع الجلسات الأخرى، ثم اضغط «تم الخروج» ليُعرض الرقم للبيع ويُضاف المبلغ إلى رصيدك تلقائيًا.",
        parse_mode=None,
        buttons=[
            [Button.inline("• تم الخروج • ✅", data=f"sell_logout_{phone}")],
            [Button.inline("إلغاء", data="cancel_operation")],
        ],
    )


async def review_candidates(page=0):
    """List live unreviewed accounts, including installations and legacy rows."""
    return await query(
        """WITH candidates AS (
            SELECT phone,'inventory' AS source,NULL AS owner_id,1 AS priority FROM accounts WHERE NOT is_sold
            UNION ALL SELECT phone,'installed',user_id,2 FROM stored_accounts
            UNION ALL SELECT phone,'installed',user_id,2 FROM purchased_accounts
        ), ranked AS (
            SELECT c.*,n.number_type,
                ROW_NUMBER() OVER(PARTITION BY c.phone ORDER BY c.priority,c.owner_id NULLS LAST) AS position
            FROM candidates c LEFT JOIN number_metadata n ON n.phone=c.phone
            WHERE NOT COALESCE(n.reviewed,FALSE)
        )
        SELECT phone,source,owner_id,number_type FROM ranked WHERE position=1
        ORDER BY priority,phone LIMIT 21 OFFSET ?""",
        (max(0, page) * 20,), fetchall=True,
    ) or []


async def menu(event, action, parts):
    from staff_security import require
    from handlers.base import active_conversations

    if action == "accept":
        raise ValueError("لا توجد موافقة مطلوبة؛ استخدم زر «تم الخروج».")
    if not await require(event, "inventory"):
        return
    if action == "reviews":
        page = max(0, int(parts[0])) if parts else 0
        rows = await review_candidates(page)
        labels = {'sale': 'طلب بيع', 'inventory': 'مخزون', 'installed': 'منصب'}
        buttons = [[Button.inline(
            f"{labels[r[1]]} +{r[0]} · {catalog.TYPES.get(r[3], 'غير مصنف')}"
            + (f" · البائع {r[2]}" if r[1] == 'sale' else ''), data=f"num:review:{r[0]}",
        )] for r in rows[:20]]
        if page:
            buttons.append([Button.inline("السابق", data=f"num:reviews:{page - 1}")])
        if len(rows) > 20:
            buttons.append([Button.inline("التالي", data=f"num:reviews:{page + 1}")])
        buttons += [[Button.inline("تحديث", data=f"num:reviews:{page}")], [Button.inline("رجوع", data="admin_panel")]]
        return await event.edit(
            "📋 مراجعة الأرقام\n" + (
                "اختر رقمًا من المخزون أو الحسابات المنصبة لاعتماد تصنيفه."
                if rows else "لا توجد حسابات تنتظر اعتماد التصنيف في هذه الصفحة."
            ),
            parse_mode=None,
            buttons=buttons,
        )
    from handlers.number_handlers import collect_submission

    phone = parts[0]
    if await query("SELECT 1 FROM pending_sales WHERE phone=?", (phone,), fetchone=True):
        raise ValueError("طلبات البيع لا تحتاج مراجعة؛ يتحقق النظام منها تلقائيًا عند الضغط على «تم الخروج».")
    async with event.client.conversation(event.chat_id) as conv:
        active_conversations[event.chat_id] = conv
        try:
            data = await collect_submission(conv, event.chat_id)
            await catalog.classify(
                event.chat_id,
                phone,
                data["country_id"],
                data["number_type"],
                data.get("features", ""),
                data.get("warranty"),
                {"checked_at": data.get("checked_at"), "scope": data.get("scope")},
            )
            await conv.send_message("✅ اعتُمد التصنيف للرقم.", parse_mode=None)
        except (ValueError, asyncio.TimeoutError) as exc:
            await conv.send_message(str(exc) or "انتهى وقت الإدخال.", parse_mode=None)
        finally:
            active_conversations.pop(event.chat_id, None)
