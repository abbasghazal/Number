from telethon import errors, events, types


class _TextMenuEvent:
    """Present callback-style menus as new replies to incoming text."""
    def __init__(self, event):
        self._event = event

    def __getattr__(self, name):
        return getattr(self._event, name)

    async def edit(self, *args, **kwargs):
        return await self._event.respond(*args, **kwargs)

    async def answer(self, text=None, **kwargs):
        if text:
            return await self._event.respond(text)


def register_handlers(client):
    """Register handlers after the package has loaded all feature modules."""
    # Importing the explicit public handlers here avoids a package-import cycle
    # while preserving deterministic registration at application startup.
    from . import (
        account_settings_menu,
        add_admin,
        add_auction_handler,
        add_country_handler,
        add_force_channel_handler,
        add_number_menu,
        add_posting_template_handler,
        add_super_channel_handler,
        admin_panel,
        ask_posting_settings,
        auction_list_handler,
        auction_menu_handler,
        auto_group_creation_handler,
        balance_settings,
        ban_settings,
        ban_user,
        broadcast_start,
        buy_number,
        callback_handler,
        cancel_operation,
        control_menu,
        creation_settings_menu,
        del_account_menu,
        del_admin,
        del_country_menu,
        del_force_channel_handler,
        del_posting_template_handler,
        del_super_channel_handler,
        edit_posting_template_handler,
        developer_menu,
        force_settings,
        install_menu_handler,
        install_session_handler,
        license_guard_handler,
        manual_group_creation_handler,
        num_settings,
        posting_settings_menu,
        select_channel_for_posting,
        sell_account,
        sell_price_handler,
        show_rules,
        start_handler,
        start_posting_handler,
        stop_all_posting_handler,
        stop_auto_creation_handler,
        stop_manual_creation_handler,
        stop_posting_group_handler,
        super_settings_menu,
        support_request,
        transfer_balance,
        unban_user,
        view_active_accounts,
        view_purchased_accounts,
        view_sold_accounts,
        view_stored_accounts,
        withdraw_balance,
        fund_channel_handler,
        github_update_handler,
        handle_star_precheckout,
        handle_star_payment_update,
    )
    from .auth_license import (
        activate_license_handler, license_status_handler, generate_license_handler,
        list_licenses_handler, revoke_license_handler, clear_expired_licenses_handler,
        license_stats_handler, export_licenses_handler,
    )
    # Text commands open a selection menu; identifiers arrive via its buttons.
    back_button = cancel_operation
    logout_account = view_stored_accounts
    store_account_handler = view_purchased_accounts
    select_account_for_posting = start_posting_handler
    toggle_timed_name = account_settings_menu
    view_auction_handler = auction_list_handler
    place_bid_handler = auction_list_handler
    sell_auction_handler = add_auction_handler
    auction_logout_handler = auction_list_handler

    def register_message(handler, builder):
        builder.incoming = True
        async def dispatch(event):
            if event.chat_id in active_conversations and handler not in (cancel_operation, start_handler):
                return
            from .input_choices import clear_country
            clear_country(event.chat_id)
            from ui_config import PanelEvent
            try:
                await handler(PanelEvent(_TextMenuEvent(event)))
            except errors.AlreadyInConversationError:
                await event.respond('المحادثة السابقة ما زالت تُغلق. انتظر لحظة ثم أعد فتح العملية.')
        client.add_event_handler(dispatch, builder)

    from .base import active_conversations
    from funding_service import membership_update
    from broadcast_registry import remember_event_chat

    async def remember_broadcast_destination(event):
        await remember_event_chat(event)

    # Bots are forbidden from listing their dialogs.  Record each group or
    # channel that sends the bot an update, then use this list for broadcasts.
    client.add_event_handler(
        remember_broadcast_destination,
        events.NewMessage(incoming=True, func=lambda e: e.is_group or e.is_channel),
    )
    client.add_event_handler(
        remember_broadcast_destination,
        events.ChatAction(func=lambda e: e.is_group or e.is_channel),
    )
    client.add_event_handler(membership_update,events.Raw(types.UpdateChannelParticipant))
    # Payment completion is a Telegram service message. Telethon does not
    # emit it as NewMessage, so receive its underlying MTProto update directly.
    client.add_event_handler(
        lambda update: handle_star_payment_update(client, update),
        events.Raw(types.UpdateNewMessage),
    )
    # Must be registered before the feature handlers so an expired licence
    # cannot continue into a second message handler.
    client.add_event_handler(license_guard_handler, events.NewMessage(incoming=True, func=lambda e: e.is_private))
    register_message(github_update_handler, events.NewMessage(pattern=r'^/update(?:@\w+)?\s*$', func=lambda e: e.is_private))

    for command, handler in {
        'activate': activate_license_handler, 'status': license_status_handler,
        'mylicense': license_status_handler, 'genlicense': generate_license_handler,
        'licenses': list_licenses_handler, 'revokelicense': revoke_license_handler,
        'clearlicenses': clear_expired_licenses_handler, 'licensestats': license_stats_handler,
        'exportlicenses': export_licenses_handler,
    }.items():
        register_message(handler, events.NewMessage(pattern=rf'^/{command}(?:@\w+)?(?:\s|$)', func=lambda e: e.is_private))

    # معالجات الأوامر الأساسية
    register_message(start_handler, events.NewMessage(pattern='/start', func=lambda e: e.is_private))
    from .shahm_handlers import menu as shahm_menu
    register_message(shahm_menu, events.NewMessage(pattern=r'^/shahm(?:@\w+)?\s*$', func=lambda e: e.is_private))
    from .promotion_handlers import menu as promotion_menu
    async def promotions(event):
        return await promotion_menu(event, 'promo:menu')
    async def funding(event):
        return await promotion_menu(event, 'fund:menu')
    register_message(promotions, events.NewMessage(pattern=r'^/promote(?:@\w+)?\s*$', func=lambda e: e.is_private))
    register_message(funding, events.NewMessage(pattern=r'^/points(?:@\w+)?\s*$', func=lambda e: e.is_private))
    register_message(admin_panel, events.NewMessage(pattern='/admin', func=lambda e: e.is_private))
    register_message(fund_channel_handler, events.NewMessage(pattern=r'^(?:/fund|تمويل)$', func=lambda e: e.is_private))
    register_message(control_menu, events.NewMessage(pattern='لوحة التحكم', func=lambda e: e.is_private))
    register_message(developer_menu, events.NewMessage(pattern='قسم المطور', func=lambda e: e.is_private))
    register_message(back_button, events.NewMessage(pattern='رجوع', func=lambda e: e.is_private))
    register_message(support_request, events.NewMessage(pattern='دعم', func=lambda e: e.is_private))
    from .stars_handlers import paysupport
    register_message(paysupport, events.NewMessage(pattern=r'^/paysupport(?:@\w+)?\s*$', func=lambda e: e.is_private))
    register_message(show_rules, events.NewMessage(pattern='القوانين', func=lambda e: e.is_private))
    register_message(sell_price_handler, events.NewMessage(pattern='/sell_price', func=lambda e: e.is_private))
    
    # معالجات إدارة الحسابات والأرقام
    register_message(add_number_menu, events.NewMessage(pattern='إضافة رقم', func=lambda e: e.is_private))
    register_message(buy_number, events.NewMessage(pattern='شراء رقم', func=lambda e: e.is_private))
    register_message(sell_account, events.NewMessage(pattern='بيع حساب', func=lambda e: e.is_private))
    register_message(view_purchased_accounts, events.NewMessage(pattern='الحسابات المشتراة', func=lambda e: e.is_private))
    register_message(view_sold_accounts, events.NewMessage(pattern='الحسابات المباعة', func=lambda e: e.is_private))
    register_message(view_stored_accounts, events.NewMessage(pattern='الحسابات المخزنة', func=lambda e: e.is_private))
    register_message(view_active_accounts, events.NewMessage(pattern='الحسابات النشطة', func=lambda e: e.is_private))
    register_message(del_account_menu, events.NewMessage(pattern='حذف حساب', func=lambda e: e.is_private))
    register_message(logout_account, events.NewMessage(pattern='تسجيل الخروج', func=lambda e: e.is_private))
    
    # معالجات الإعدادات
    register_message(account_settings_menu, events.NewMessage(pattern='إعدادات الحساب', func=lambda e: e.is_private))
    register_message(balance_settings, events.NewMessage(pattern='رصيدي', func=lambda e: e.is_private))
    register_message(creation_settings_menu, events.NewMessage(pattern='إعدادات الإنشاء', func=lambda e: e.is_private))
    register_message(posting_settings_menu, events.NewMessage(pattern='إعدادات النشر', func=lambda e: e.is_private))
    register_message(force_settings, events.NewMessage(pattern='القنوات الإجبارية', func=lambda e: e.is_private))
    register_message(super_settings_menu, events.NewMessage(pattern='القنوات السوبر', func=lambda e: e.is_private))
    register_message(ban_settings, events.NewMessage(pattern='حظر مستخدم', func=lambda e: e.is_private))
    register_message(num_settings, events.NewMessage(pattern='إعدادات الأرقام', func=lambda e: e.is_private))
    
    # معالجات الأزرار (Callbacks)
    client.add_event_handler(callback_handler, events.CallbackQuery())
    client.add_event_handler(
        lambda update: handle_star_precheckout(client, update),
        events.Raw(types.UpdateBotPrecheckoutQuery),
    )
    
    # معالجات العمليات المتقدمة
    register_message(add_country_handler, events.NewMessage(pattern='إضافة دولة', func=lambda e: e.is_private))
    register_message(del_country_menu, events.NewMessage(pattern='حذف دولة', func=lambda e: e.is_private))
    register_message(transfer_balance, events.NewMessage(pattern='تحويل رصيد', func=lambda e: e.is_private))
    register_message(withdraw_balance, events.NewMessage(pattern='سحب رصيد', func=lambda e: e.is_private))
    register_message(cancel_operation, events.NewMessage(pattern=r'^(?:إلغاء العملية|إلغاء|الغاء|/cancel(?:@\w+)?)\s*$', func=lambda e: e.is_private))
    
    # معالجات عمليات الإنشاء
    register_message(manual_group_creation_handler, events.NewMessage(pattern='إنشاء يدوي', func=lambda e: e.is_private))
    register_message(auto_group_creation_handler, events.NewMessage(pattern='إنشاء تلقائي', func=lambda e: e.is_private))
    register_message(stop_manual_creation_handler, events.NewMessage(pattern='إيقاف إنشاء يدوي', func=lambda e: e.is_private))
    register_message(stop_auto_creation_handler, events.NewMessage(pattern='إيقاف إنشاء تلقائي', func=lambda e: e.is_private))
    
    # معالجات النشر
    register_message(start_posting_handler, events.NewMessage(pattern='بدء النشر', func=lambda e: e.is_private))
    register_message(stop_all_posting_handler, events.NewMessage(pattern='إيقاف كل النشرات', func=lambda e: e.is_private))
    register_message(stop_posting_group_handler, events.NewMessage(pattern='إيقاف النشر في مجموعة', func=lambda e: e.is_private))
    register_message(select_account_for_posting, events.NewMessage(pattern='اختيار حساب للنشر', func=lambda e: e.is_private))
    register_message(select_channel_for_posting, events.NewMessage(pattern='اختيار قناة للنشر', func=lambda e: e.is_private))
    register_message(ask_posting_settings, events.NewMessage(pattern='ضبط إعدادات النشر', func=lambda e: e.is_private))
    register_message(add_posting_template_handler, events.NewMessage(pattern='إضافة قالب نشر', func=lambda e: e.is_private))
    register_message(edit_posting_template_handler, events.NewMessage(pattern='تعديل قالب نشر', func=lambda e: e.is_private))
    register_message(del_posting_template_handler, events.NewMessage(pattern='حذف قالب نشر', func=lambda e: e.is_private))
    
    # معالجات إدارة القنوات
    register_message(add_force_channel_handler, events.NewMessage(pattern='إضافة قناة إجبارية', func=lambda e: e.is_private))
    register_message(del_force_channel_handler, events.NewMessage(pattern='حذف قناة إجبارية', func=lambda e: e.is_private))
    register_message(add_super_channel_handler, events.NewMessage(pattern='إضافة قناة سوبر', func=lambda e: e.is_private))
    register_message(del_super_channel_handler, events.NewMessage(pattern='حذف قناة سوبر', func=lambda e: e.is_private))
    
    # معالجات VIP والإدارة
    register_message(add_admin, events.NewMessage(pattern='إضافة مشرف', func=lambda e: e.is_private))
    register_message(del_admin, events.NewMessage(pattern='حذف مشرف', func=lambda e: e.is_private))
    register_message(ban_user, events.NewMessage(pattern='حظر مستخدم', func=lambda e: e.is_private))
    register_message(unban_user, events.NewMessage(pattern='رفع حظر مستخدم', func=lambda e: e.is_private))
    
    # معالجات إضافية
    register_message(store_account_handler, events.NewMessage(pattern='تخزين حساب', func=lambda e: e.is_private))
    register_message(install_session_handler, events.NewMessage(pattern='تثبيت جلسة', func=lambda e: e.is_private))
    register_message(sell_price_handler, events.NewMessage(pattern='تحديد سعر البيع', func=lambda e: e.is_private))
    register_message(toggle_timed_name, events.NewMessage(pattern='تفعيل/تعطيل الاسم المؤقت', func=lambda e: e.is_private))
    register_message(broadcast_start, events.NewMessage(pattern='اذاعة عامة', func=lambda e: e.is_private))
    # في نهاية register_handlers()
    register_message(install_menu_handler, events.NewMessage(pattern='قسم التنصيب', func=lambda e: e.is_private))
    register_message(auction_menu_handler, events.NewMessage(pattern='قسم المزاد', func=lambda e: e.is_private))
    register_message(add_auction_handler, events.NewMessage(pattern='إضافة مزاد', func=lambda e: e.is_private))
    register_message(auction_list_handler, events.NewMessage(pattern='قائمة المزاد', func=lambda e: e.is_private))
    register_message(view_auction_handler, events.NewMessage(pattern='عرض مزاد', func=lambda e: e.is_private))
    register_message(place_bid_handler, events.NewMessage(pattern='تقديم عرض', func=lambda e: e.is_private))
    register_message(sell_auction_handler, events.NewMessage(pattern='بيع بالمزاد', func=lambda e: e.is_private))
    register_message(auction_logout_handler, events.NewMessage(pattern='تسجيل خروج المزاد', func=lambda e: e.is_private))
    from .input_choices import country_text
    client.add_event_handler(country_text, events.NewMessage(incoming=True, func=lambda e: e.is_private))
    print("✅ تم تسجيل جميع معالجات الأحداث بنجاح")
