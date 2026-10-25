"""Type-first inventory, pricing, review and purchase panels."""

import asyncio

from telethon import Button

import number_catalog as catalog
from workflow import query
from .base import active_conversations
from .input_choices import choose, choose_country, confirm, input_session, read, remember_country, country_label, clear_country


async def prompt(conv, message):
    return await read(conv, message)


async def collect_submission(conv, actor, *, pending=False, require_evidence=True):
    async with input_session(conv):
        return await _collect_submission(conv, actor, pending=pending, require_evidence=require_evidence)


async def _collect_submission(conv, actor, *, pending=False, require_evidence=True):
    kind = await choose(conv, "📱 اختر نوع الرقم لتحديد فئته السعرية:", catalog.TYPES.items())
    countries = (
        await query("SELECT country_id,name,calling_code FROM countries WHERE is_active ORDER BY name", fetchall=True)
        or []
    )
    country = await choose_country(conv, countries)
    data = {"actor": actor, "number_type": kind, "country_id": country[0]}
    if pending:
        from postgres import connect
        from utils.asyncio_compat import to_thread

        def read():
            with connect() as db:
                return catalog.pricing(db, data["country_id"], kind, buying=True)

        price = await to_thread(read)
        await conv.send_message(
            f"المبلغ الذي يدفعه البوت: {price[1]}$. بعد تسجيل الخروج من الجلسات الأخرى سيُضاف المبلغ إلى رصيدك تلقائيًا.", parse_mode=None
        )
    return data


async def country_panel(event, action, kind, rows, page=0, hint=None, remember=True):
    rows = list(rows)
    if remember:
        if rows:
            remember_country(event.chat_id, action, kind, rows)
        else:
            clear_country(event.chat_id)
    page = max(0, min(int(page), max(0, (len(rows) - 1) // 20)))
    buttons = [[Button.inline(r[3] if len(r) > 3 else country_label(r), data=f"num:{action}:{kind}:{r[0]}")]
               for r in rows[page * 20:(page + 1) * 20]]
    prefix = "countries" if action == "country" else "pricecountries"
    if page:
        buttons.append([Button.inline("السابق", data=f"num:{prefix}:{kind}:{page - 1}")])
    if (page + 1) * 20 < len(rows):
        buttons.append([Button.inline("التالي", data=f"num:{prefix}:{kind}:{page + 1}")])
    buttons.append([Button.inline("رجوع", data="buy" if action == "country" else "num:prices")])
    await event.edit(
        f"🌍 {'أسعار ' if action == 'price' else ''}{catalog.TYPES[kind]}\n"
        + (hint or "اختر الدولة من الأزرار أو اكتب اسمها في المحادثة.")
        + ("\nالعدد المعروض للأرقام المتاحة حاليًا." if action == "country" else "")
        + ("\nلا توجد دول متاحة لهذا النوع حاليًا." if not rows else ""),
        parse_mode=None, buttons=buttons,
    )


async def buy_menu(event, kind=None, country=None, page=0, availability="available"):
    clear_country(event.chat_id)
    if kind is None:
        counts = {k: sum(r[3] for r in await catalog.countries(k)) for k in catalog.TYPES}
        return await event.edit(
            "📱 شراء رقم\nاختر النوع لعرض الدول والأرقام المتاحة.",
            parse_mode=None,
            buttons=[
                [Button.inline(f"{label} ({counts[k]})", data=f"num:buy:{k}")] for k, label in catalog.TYPES.items()
            ]
            + [[Button.inline("رجوع", data="numbers_menu")]],
        )
    catalog.validate_type(kind)
    if country is None:
        rows = await catalog.countries(kind)
        return await country_panel(event, "country", kind,
            [(r[0], r[1], None, f"{r[1]} ({r[3]}) · {r[2]}$") for r in rows], page)
    rows = await catalog.inventory(kind, country, availability, page)
    buttons = [[Button.inline(f"+{r[0]}", data=f"num:quote:{r[0]}")] for r in rows]
    if page:
        buttons += [[Button.inline("السابق", data=f"num:page:{kind}:{country}:{page - 1}:{availability}")]]
    if rows and (page + 1) * 20 < rows[0][1]:
        buttons += [[Button.inline("التالي", data=f"num:page:{kind}:{country}:{page + 1}:{availability}")]]
    buttons += [
        [
            Button.inline("متاح", data=f"num:page:{kind}:{country}:0:available"),
            Button.inline("محجوز", data=f"num:page:{kind}:{country}:0:reserved"),
        ],
        [Button.inline("رجوع", data=f"num:buy:{kind}")],
    ]
    await event.edit(
        f"📱 {catalog.TYPES[kind]}\nالحالة: {availability}\nالعدد: {rows[0][1] if rows else 0}\nاختر رقمًا لعرض السعر المعتمد.",
        buttons=buttons,
        parse_mode=None,
    )


async def quote_panel(event, phone):
    from commerce import purchase_quote, price_text

    q = await purchase_quote(event.chat_id, phone)
    await event.edit(
        "📱 شراء رقم\n" + price_text(q) + "\n\nراجع التفاصيل ثم أكد الشراء.",
        parse_mode=None,
        buttons=[
            [Button.inline("تأكيد الشراء", data=f"num:confirm:{q['quote_id']}")],
            [Button.inline("الدفع بالنجوم", data=f"stars:buy:{phone}")],
            [Button.inline("رجوع", data=f"num:country:{q['number_type']}:{q['country_id']}")],
            [Button.inline("إلغاء", data="cancel_operation")],
        ],
    )


async def pricing_menu(event, kind=None, country=None, page=0):
    from staff_security import require

    if not await require(event, "finance"):
        return
    clear_country(event.chat_id)
    if not kind:
        return await event.edit(
            "⚙️ إعدادات البيع والشراء\nاختر نوع الرقم.",
            parse_mode=None,
            buttons=[[Button.inline(label, data=f"num:prices:{k}")] for k, label in catalog.TYPES.items()]
            + [[Button.inline("رجوع", data="admin_panel")]],
        )
    catalog.validate_type(kind)
    if not country:
        rows = await query("SELECT country_id,name,calling_code FROM countries ORDER BY name", fetchall=True) or []
        return await country_panel(event, "price", kind, rows, page)
    async with event.client.conversation(event.chat_id) as conv, input_session(conv):
        active_conversations[event.chat_id] = conv
        try:
            old = await query(
                "SELECT bot_sell_price,bot_buy_price,sell_enabled,buy_enabled,version FROM number_type_prices WHERE country_id=? AND number_type=?",
                (country, kind),
                fetchone=True,
            )
            await conv.send_message(f"الإعداد الحالي: {old or 'غير مسعّر'}", parse_mode=None)
            sell = await prompt(conv, "سعر بيع البوت للمستخدم ($):")
            buy = await prompt(conv, "سعر شراء البوت من المستخدم ($):")
            stars = await choose(conv, "سعر بيع البوت بالنجوم لهذا النوع والدولة:",
                                 [("-", "التحويل حسب سعر الرصيد"), (None, "تحديد سعر بالنجوم")], custom=True)
            if stars is None:
                stars = await prompt(conv, "أدخل سعر البيع بالنجوم:")
            stars = None if stars == "-" else int(stars)
            se = await confirm(conv, "تفعيل بيع البوت للمستخدم؟")
            be = await confirm(conv, "تفعيل استقبال أرقام المستخدمين؟")
            policy = None
            if (
                await confirm(
                    conv,
                    f"النوع: {catalog.TYPES[kind]}\nبيع البوت: {sell}$\nشراء البوت: {buy}$\nالبيع: {se}؛ الاستقبال: {be}\nحفظ الإعدادات؟",
                )
            ):
                await catalog.configure(event.chat_id, country, kind, sell, buy, se, be, policy, stars)
                await conv.send_message("✅ حُفظ إصدار تسعير جديد.", parse_mode=None)
        except (ValueError, asyncio.TimeoutError) as exc:
            await conv.send_message(str(exc) or "انتهى وقت الإدخال.", parse_mode=None)
        finally:
            active_conversations.pop(event.chat_id, None)


async def callback(event, data):
    parts = data.split(":")
    action = parts[1]
    try:
        if action == "new_sale":
            from .buy_sell_handlers import submit_sale

            return await submit_sale(event)
        if action == "mysales":
            rows = (
                await query(
                    "SELECT phone,number_type,price FROM pending_sales WHERE seller_id=? ORDER BY added_date DESC",
                    (event.chat_id,),
                    fetchall=True,
                )
                or []
            )
            return await event.edit(
                "📋 طلبات البيع\nاختر طلبًا لإكمال التحقق من خروج الجلسات.",
                parse_mode=None,
                buttons=[
                    [
                        Button.inline(
                            f"+{r[0]} · {catalog.TYPES.get(r[1], 'مراجعة')} · {r[2]}$", data=f"num:offer:{r[0]}"
                        )
                    ]
                    for r in rows
                ]
                + [[Button.inline("رجوع", data="sell")]],
            )
        if action == "offer":
            from sale_review import seller_offer

            return await seller_offer(event, parts[2])
        if action == "buy":
            return await buy_menu(event, parts[2])
        if action == "countries":
            return await buy_menu(event, parts[2], page=int(parts[3]))
        if action == "pricecountries":
            return await pricing_menu(event, parts[2], page=int(parts[3]))
        if action == "country":
            return await buy_menu(event, parts[2], int(parts[3]))
        if action == "page":
            return await buy_menu(event, parts[2], int(parts[3]), int(parts[4]), parts[5])
        if action == "quote":
            return await quote_panel(event, parts[2])
        if action == "confirm":
            from commerce import complete_purchase

            result = await complete_purchase(event.chat_id, parts[2])
            return await event.edit(
                "✅ اكتمل الشراء." if result.get("completed") else "⏳ حُجز الشراء حتى انتهاء الإيجار.",
                buttons=[[Button.inline("حساباتي", data="accounts_view")]],
                parse_mode=None,
            )
        if action in {"prices", "price"}:
            return await pricing_menu(
                event, parts[2] if len(parts) > 2 else None, int(parts[3]) if len(parts) > 3 else None
            )
        if action in {"reviews", "review", "accept"}:
            from sale_review import menu

            return await menu(event, action, parts[2:])
    except ValueError as exc:
        await event.answer(str(exc), alert=True)
