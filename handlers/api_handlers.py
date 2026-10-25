"""Telegram management panel for paid external API access."""

from telethon import Button

import api_service


async def _menu(event, notice=None):
    info = await api_service.status(event.chat_id)
    active = info["active"]
    plan_label = api_service.PLANS.get(info.get("plan"), (0, "غير فعال"))[1] if info.get("plan") else "غير فعال"
    expiry = str(info.get("expires_at") or "-")
    key = (info.get("key_prefix") + "…") if info.get("key_prefix") else "لا يوجد"
    text = (
        "🔌 قسم API\n"
        "استخدم نفس خدمات المنصة من بوت آخر أو موقع إلكتروني عبر HTTPS وBearer API Key.\n\n"
        f"الحالة: {'فعال' if active else 'غير فعال'}\n"
        f"الخطة: {plan_label}\n"
        f"ينتهي: {expiry}\n"
        f"المفتاح: {key}\n"
        f"الشهري: {info['prices']['monthly']}$ · السنوي: {info['prices']['yearly']}$\n"
        f"Base URL: {api_service.api_base_url()}"
    )
    if notice:
        text = notice + "\n\n" + text
    buttons = [
        [Button.inline("اشتراك شهري", data="api:subscribe:monthly"), Button.inline("اشتراك سنوي", data="api:subscribe:yearly")],
        [Button.inline("إنشاء مفتاح", data="api:key:new"), Button.inline("تدوير المفتاح", data="api:key:rotate")],
        [Button.inline("إلغاء المفتاح", data="api:key:revoke"), Button.inline("طريقة الاستخدام", data="api:docs")],
    ]
    from config import DEVELOPER_ID
    if DEVELOPER_ID and event.chat_id == DEVELOPER_ID:
        buttons.append([
            Button.inline("سعر الشهري", data="api:price:monthly"),
            Button.inline("سعر السنوي", data="api:price:yearly"),
        ])
    buttons.append([Button.inline("رجوع", data="main")])
    return await event.edit(text, parse_mode=None, buttons=buttons)


async def menu(event, data="api:menu"):
    parts = data.split(":")
    action = parts[1] if len(parts) > 1 else "menu"
    if action == "menu":
        return await _menu(event)
    if action == "subscribe":
        plan = parts[2]
        info = await api_service.status(event.chat_id)
        if plan not in api_service.PLANS:
            raise ValueError("خطة API غير صالحة.")
        label = api_service.PLANS[plan][1]
        price = info["prices"][plan]
        return await event.edit(
            f"تأكيد الاشتراك {label} في API مقابل {price}$ من رصيدك؟\n"
            "إذا كان لديك اشتراك فعال فستُضاف المدة إلى تاريخ الانتهاء الحالي.",
            parse_mode=None,
            buttons=[
                [Button.inline("تأكيد الدفع", data=f"api:confirm:{plan}")],
                [Button.inline("إلغاء", data="api:menu")],
            ],
        )
    if action == "confirm":
        result = await api_service.subscribe(event.chat_id, parts[2])
        return await _menu(
            event,
            f"✅ تم تفعيل الاشتراك {result['label']} حتى {result['expires_at']} وخصم {result['price']}$."
        )
    if action == "key":
        operation = parts[2]
        if operation == "new":
            raw = await api_service.issue_key(event.chat_id)
            await _menu(event, "✅ تم إنشاء مفتاح API.")
            return await event.respond(
                "🔐 مفتاح API الجديد — سيظهر كاملًا هذه المرة فقط:\n\n"
                f"{raw}\n\nاحفظه في مكان آمن ولا تضعه داخل كود الواجهة الأمامية أو مستودع عام.",
                parse_mode=None,
            )
        if operation == "rotate":
            return await event.edit(
                "تدوير المفتاح يلغي المفتاح الحالي فورًا. هل تريد المتابعة؟",
                buttons=[[Button.inline("تأكيد التدوير", data="api:keyrotate:confirm")], [Button.inline("رجوع", data="api:menu")]],
                parse_mode=None,
            )
        if operation == "revoke":
            return await event.edit(
                "إلغاء المفتاح يوقف جميع التطبيقات الخارجية التي تستخدمه. هل تريد المتابعة؟",
                buttons=[[Button.inline("تأكيد الإلغاء", data="api:keyrevoke:confirm")], [Button.inline("رجوع", data="api:menu")]],
                parse_mode=None,
            )
    if action == "keyrotate":
        raw = await api_service.issue_key(event.chat_id, rotate=True)
        await _menu(event, "✅ أُلغي المفتاح السابق وتم إنشاء مفتاح جديد.")
        return await event.respond(
            "🔐 مفتاح API الجديد — سيظهر كاملًا هذه المرة فقط:\n\n"
            f"{raw}\n\nحدّثه في تطبيقاتك الخارجية فورًا.",
            parse_mode=None,
        )
    if action == "keyrevoke":
        revoked = await api_service.revoke_key(event.chat_id)
        return await _menu(event, "✅ أُلغي مفتاح API." if revoked else "لا يوجد مفتاح فعال لإلغائه.")
    if action == "price":
        from config import DEVELOPER_ID
        from .platform_handlers import ask

        if not DEVELOPER_ID or event.chat_id != DEVELOPER_ID:
            raise PermissionError("هذه العملية متاحة للمطور فقط.")
        plan = parts[2] if len(parts) > 2 else ""
        if plan not in api_service.PLANS:
            raise ValueError("خطة API غير صالحة.")
        raw = await ask(event, f"أرسل السعر الجديد بالدولار لاشتراك API {api_service.PLANS[plan][1]}:")
        if raw is None:
            return await _menu(event)
        price = await api_service.set_price(event.chat_id, plan, raw)
        return await _menu(event, f"✅ تم تحديث سعر الخطة إلى {price}$.")
    if action == "docs":
        base = api_service.api_base_url()
        return await event.edit(
            "طريقة استخدام API\n\n"
            f"Base URL: {base}\n"
            "Authentication: Authorization: Bearer YOUR_API_KEY\n"
            "حد الطلبات: 120 طلبًا/دقيقة لكل مفتاح.\n"
            "طلبات POST إلى منصة الخدمات تحتاج X-Idempotency-Key بطول 16–128 حرفًا.\n\n"
            "أهم المسارات:\n"
            "GET /status — حالة الاشتراك والمفتاح\n"
            "GET /services — قائمة الخدمات المتاحة\n"
            "GET|POST /platform/... — خدمات المنصة الأصلية\n"
            "GET|POST /core/... — الشراء والتأجير والحسابات والدعم الأساسية",
            parse_mode=None,
            buttons=[[Button.inline("رجوع", data="api:menu")]],
        )
    raise ValueError("عملية API غير معروفة.")
