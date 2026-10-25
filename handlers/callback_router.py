# Auto-split from handlers.py. Keep shared imports/state in base.py.
import contextlib
from telethon import errors

from .base import *
from typing import TYPE_CHECKING

# Keep callback targets explicitly imported.  The compatibility namespace in
# handlers.__init__ is populated only after the package is loaded, while this
# module can also be imported directly by the registry or development tools.
# Explicit imports avoid both runtime NameError exceptions and unresolved-name
# warnings in editors.
from .admin_handlers import (
    add_engagement_channel,
    admin_operations,
    auto_engagement_menu,
    auto_story_burst_handler,
    choose_engagement_service,
    delete_engagement,
    delete_engagement_channel,
    broadcast_target_selected,
    engagement_channel_selected,
    engagement_period_selected,
    engagement_type_selected,
    fund_channel_handler,
    funding_auto_global_menu,
    funding_auto_global_toggle,
    list_engagement_channels,
    toggle_engagement,
)
from .common import developer_menu, resolve_control_accounts
from .account_services import (
    increment_views_handler,
    mass_comment_handler,
    mass_react_handler,
    mass_vote_handler,
    mass_story_handler,
    join_owned_channel_handler,
    premium_join_handler,
)
from .leave_handlers import leave_channels_account_menu
from .rental_handlers import rent_numbers_handler, rental_settings_handler
from .health_handlers import health_menu
from .statement_handlers import statement_menu
from .platform_handlers import platform_menu
from .achievement_handlers import achievements_menu
from .settings_handlers import join_selected_accounts_to_super, set_referral_reward_handler
from .storage_handlers import create_new_session_handler, extract_session_handler, install_sessions_txt_handler, terminate_sessions_menu
from .support_handlers import close_ticket
from .user_handlers import balance_menu, my_activity, referral_menu
from .payment_handlers import ask_custom_topup, choose_payment_method, edit_payment_setting, payment_settings_menu, set_topup_amount, topup_menu

if TYPE_CHECKING:
    from .base import (
        account_actions_menu,
        account_details,
        account_settings_menu,
        accounts_view_menu,
        add_admin,
        add_auction_handler,
        add_coins,
        add_country_handler,
        add_force_channel_handler,
        add_mandatory_channel_handler,
        add_number_menu,
        add_number_process,
        add_posting_template_handler,
        add_super_channel_handler,
        add_vip_callback,
        activate_license_callback,
        admin_panel,
        admin_bot_stats,
        admin_settings,
        all_numbers_count,
        auction_list_handler,
        auction_logout_handler,
        auction_menu_handler,
        auto_group_creation_handler,
        balance_settings,
        ban_settings,
        ban_user,
        broadcast_start,
        buy_confirmed,
        buy_number,
        buy_sell_settings,
        cancel_operation,
        change_buy_price,
        change_buy_price_menu,
        change_profile_photo,
        change_random_bios_for_all_accounts,
        change_random_names_for_all_accounts,
        change_sell_price,
        change_sell_price_menu,
        check_account,
        clear_posting_templates_handler,
        clear_super_channels,
        confirm_purchase_handler,
        confirm_withdraw,
        continue_auction_handler,
        control_menu,
        country_selected,
        creation_settings_menu,
        del_account_confirm,
        del_account_done,
        del_account_menu,
        del_admin,
        del_coins,
        del_country,
        del_country_menu,
        del_force_channel_handler,
        del_posting_template_handler,
        del_super_channel_handler,
        delete_install_account,
        delete_install_handler,
        delete_posting_template_callback,
        disable_notifications,
        disable_stealth_mode,
        edit_posting_template_callback,
        edit_posting_template_handler,
        edit_rules,
        enable_notifications,
        enable_stealth_mode,
        force_settings,
        get_verification_code,
        handle_all_templates_posting,
        handle_delete_super_channel,
        handle_single_template_posting,
        handle_single_template_selection,
        install_menu_handler,
        install_session_handler,
        leave_all_channels_confirm,
        leave_all_channels_execute,
        leave_all_groups_execute,
        leave_channels_account_menu,
        leave_channels_account_selected,
        leave_groups_account_menu,
        leave_groups_account_selected,
        leave_specific_channel_menu,
        leave_specific_channel_selected,
        license_clear_expired_callback,
        license_create_callback,
        license_duration_callback,
        license_export_callback,
        license_list_callback,
        license_list_with_navigation,
        license_menu_handler,
        license_revoke_callback,
        license_revoke_confirm,
        license_revoke_do,
        license_revoke_with_selection,
        license_status_callback,
        license_stats_callback,
        logout_account,
        logout_account_sessions,
        main_menu,
        manual_group_creation_handler,
        next_add_number_countries_page,
        next_countries_page,
        next_price_countries_page,
        next_sell,
        num_settings,
        numbers_menu,
        place_bid_handler,
        posting_all_channels,
        posting_settings_menu,
        posting_specific_channel,
        prev_add_number_countries_page,
        prev_countries_page,
        prev_price_countries_page,
        profile_edit_account_menu,
        profile_edit_account_selected,
        reply_to_user,
        save_post_account_menu,
        save_post_account_selected,
        select_account,
        select_account_for_posting,
        select_channel_for_posting,
        sell_account,
        sell_auction_handler,
        sell_logout_handler,
        set_posting_setting,
        set_trust_channel,
        show_account_options,
        show_accounts,
        show_posting_templates,
        show_rules,
        show_super_channels,
        start_posting_handler,
        stop_all_posting_handler,
        stop_auto_creation_handler,
        stop_manual_creation_handler,
        stop_posting_channel_handler,
        stop_posting_group_handler,
        storage_account_menu,
        storage_account_selected,
        store_account_handler,
        store_existing_account,
        super_settings_menu,
        support_request,
        toggle_timed_name,
        transfer_balance,
        unban_user,
        unsupported_account_feature,
        view_active_accounts,
        view_auction_handler,
        view_purchased_accounts,
        view_sold_accounts,
        view_stored_accounts,
        withdraw_balance,
        zip_database,
    )


async def callback_handler(event):
    """Keep malformed or stale callback payloads from crashing the router."""
    from ui_config import PanelEvent
    event = PanelEvent(event)
    try:
        await _callback_handler_impl(event)
    except errors.AlreadyInConversationError:
        await event.answer('توجد عملية تنتظر ردك. أكملها أو اضغط إلغاء العملية أولًا.', alert=True)
    except ValueError as exc:
        # Feature handlers deliberately use ValueError for user-safe business
        # rules (for example, an API key requires an active subscription).
        # Do not mask those messages as malformed callback payloads.
        message = str(exc).strip()
        logger.warning("Callback rejected: %s", message or "invalid value")
        await event.answer(message[:500] if message else "❌ تعذر تنفيذ العملية. افتح القائمة مجدداً.", alert=True)
    except (IndexError, TypeError):
        logger.warning("Rejected malformed callback payload")
        await event.answer("❌ بيانات الزر غير صالحة. افتح القائمة مجدداً.", alert=True)
    except Exception:
        # Individual feature handlers may raise for transient Telegram or
        # database failures.  Do not reveal internal details to the user.
        logger.exception("Callback processing failed")
        # PanelEvent sends a fresh message if the callback has expired or
        # input was already requested, so the failure stays visible to the user.
        with contextlib.suppress(Exception):
            await event.answer("❌ تعذر تنفيذ العملية الآن. حاول مجدداً.", alert=True)


async def _callback_handler_impl(event):
    try:
        data = event.data.decode('utf-8')
    except UnicodeDecodeError:
        await event.answer("❌ بيانات الزر غير صالحة.", alert=True)
        return
    if not data or len(data) > 64:
        await event.answer("❌ بيانات الزر غير صالحة.", alert=True)
        return
    user_id = event.chat_id
    if getattr(event, 'sender_id', user_id) != user_id:
        return await event.answer('استخدم لوحة البوت في المحادثة الخاصة.', alert=True)
    if data.startswith('points:') or data == 'ui:edit':
        return await event.answer('افتح القائمة الرئيسية مجددًا باستخدام /start.', alert=True)
    from .input_choices import clear_country
    if not data.startswith('input:'):
        clear_country(user_id)

    if data == 'auto:stopall' or data.startswith('auto:pausejob:'):
        from .automation_handlers import menu
        return await menu(event,data)

    if data == 'shahm:stop':
        from .shahm_handlers import menu
        return await menu(event, data)

    if data.startswith(('promo:cancel:', 'fund:cancel:')) or data in {'codes:stop','promo:poolstop'}:
        from .promotion_handlers import menu
        return await menu(event, data)

    if not await enforce_rate_limit(event, f"callback:{data}", 0.7):
        return
    
    if await is_banned(user_id):
        return
    if user_id in active_conversations and data not in {'cancel_operation', 'main'} and not data.startswith('input:'):
        await event.answer('أكمل الإدخال الحالي أو اضغط إلغاء العملية قبل فتح عملية أخرى.', alert=True)
        return
    if data == 'main' and user_id in active_conversations:
        await cancel_operation(event)
        return

    # API access has its own paid monthly/yearly subscription and therefore
    # must remain reachable even when the regular bot-service license expired.
    if data not in LICENSE_FREE_CALLBACKS and not data.startswith(("api:", "codes:")) and not await enforce_license_for_callback(event):
        return

    # التحقق من القنوات التي أضافها المطور والقنوات المدفوعة للمستخدمين.
    from mandatory_subscription import verify_required_subscriptions
    subscribed, missing_channel = await verify_required_subscriptions(event.client, user_id)
    if not subscribed:
        await event.answer(f"عليك الاشتراك في قناة @{str(missing_channel).lstrip('@')} أولاً!", alert=True)
        return

    if data.startswith("mandatory:"):
        from .mandatory_handlers import menu as mandatory_menu
        return await mandatory_menu(event, data)

    if data.startswith('input:'):
        from .inline_input import callback as input_callback
        return await input_callback(event)

    admin_only_actions = {
        "admin_panel", "ajxjao", "ajxkho", "aksgl", "ajkofgl", "ajkcoingl", "bbvjls",
        "change_price", "change_sell_price", "next_buy_price_countries", "prev_buy_price_countries",
        "next_sell_price_countries", "prev_sell_price_countries",
        "add_mandatory_channel", "add_force", "del_force", "set_referral_reward", "edit_rules", "broadcast_message",
        "add_admin", "del_admin", "add_coins", "del_coins", "ban", "unban", "zip_all",
        "payment_settings", "payment_set_stars_rate", "payment_set_ton_wallet",
        "payment_set_ton_rate",
        "all_of_number", "add_country", "del_country",
        "set_trust_channel",
        "fund_channel",
        "license_menu",
        "license_create",
        "license_list",
        "license_revoke",
        "license_clear_expired",
        "license_stats",
        "license_export",
        "add_vip",
        "daily_backup",
        "auto_engagement_menu", "add_engagement_channel", "choose_engagement_service", "list_engagement_channels",
        "admin_bot_stats",
        "admin_operations",
    }
    admin_only_prefixes = (
        "chg_", "chs_", "delete_", "confirm_withdraw_", "reply_",
        "license_duration:",
        "license_list_page:",
        "license_revoke_page:",
        "license_revoke_confirm:",
        "license_revoke_do:", "close_ticket_", "engagement_", "toggle_engagement:", "delete_engagement:",
    )
    if (data in admin_only_actions or data.startswith(admin_only_prefixes)) and user_id != ADMIN_ID and not await is_admin(user_id):
        await event.answer("❌ هذه العملية متاحة للمطور والمشرفين فقط.", alert=True)
        return

    # معالجة الأحداث
    if data in admin_only_actions or data.startswith(admin_only_prefixes):
        from staff_security import require, callback_permission
        if not await require(event, callback_permission(data)):
            return
    if data.startswith('num:'):
        from .number_handlers import callback
        return await callback(event,data)
    if data.startswith('api:'):
        return await event.edit(
            "🚧 قسم API\n\nسيتم إضافة هذا القسم قريبًا.",
            parse_mode=None,
            buttons=[[Button.inline("رجوع", data="main")]],
        )
    if data.startswith(('promo:', 'fund:', 'codes:')):
        from .promotion_handlers import menu
        return await menu(event, data)

    if data.startswith('shahm:'):
        from .shahm_handlers import menu
        return await menu(event, data)

    if data.startswith('auto:'):
        from .automation_handlers import menu
        return await menu(event,data)
    if data.startswith('acct:'):
        from .management_handlers import menu
        return await menu(event,data)
    if data == 'search_join':
        from .management_handlers import search_wizard
        return await search_wizard(event)
    if data.startswith('search:cancel:'):
        from workflow import query
        await query("UPDATE workflow_jobs SET cancel_requested=TRUE WHERE job_id=? AND user_id=? AND kind='search_join'",
                    (data.split(':')[2],user_id),commit=True)
        return await event.answer('طُلب الإلغاء؛ تُحفظ نتائج الحسابات المكتملة.',alert=True)
    if data.startswith('deferred:'):
        from deferred_purchase import menu
        _, action, value = data.split(':', 2)
        await menu(event, action, value)
        return
    if data.startswith('stars:'):
        from .stars_handlers import callback as stars_callback
        await stars_callback(event, data)
        return
    if data.startswith('feature:'):
        from .feature_handlers import feature_menu
        await feature_menu(event, data)
    elif data.startswith("ops:"):
        await platform_menu(event, data)
    elif data.startswith("stmt:"):
        await statement_menu(event, data)
    elif data.startswith("health:"):
        await health_menu(event, data)
    elif data == "ajxjao":
        await num_settings(event)
    elif data == "change_price":
        await change_buy_price_menu(event)
    elif data == "change_sell_price":
        await change_sell_price_menu(event)
    elif data == "next_buy_price_countries":
        await next_price_countries_page(event, "buy")
    elif data == "prev_buy_price_countries":
        await prev_price_countries_page(event, "buy")
    elif data == "next_sell_price_countries":
        await next_price_countries_page(event, "sell")
    elif data == "prev_sell_price_countries":
        await prev_price_countries_page(event, "sell")
    elif data == "ajxkho":
        await force_settings(event)
    elif data == "aksgl":
        await admin_settings(event)
    elif data == "ajkofgl":
        await buy_sell_settings(event)
    elif data == "ajkcoingl":
        await balance_settings(event)
    elif data == "set_referral_reward":
        await set_referral_reward_handler(event)
    elif data == "bbvjls":
        await ban_settings(event)
    elif data == "set_trust_channel":
        await set_trust_channel(event)
    elif data == "license_menu":
        await license_menu_handler(event)
    elif data == "license_create":
        await license_create_callback(event)
    elif data.startswith("license_duration:"):
        await license_duration_callback(event, data)
    elif data == "license_list":
        await license_list_callback(event)
    elif data.startswith("license_list_page:"):
        await license_list_with_navigation(event, int(data.split(":", 1)[1]))
    elif data == "license_revoke":
        await license_revoke_callback(event)
    elif data.startswith("license_revoke_page:"):
        await license_revoke_with_selection(event, int(data.split(":", 1)[1]))
    elif data.startswith("license_revoke_confirm:"):
        await license_revoke_confirm(event, data)
    elif data.startswith("license_revoke_do:"):
        await license_revoke_do(event, data)
    elif data == "license_clear_expired":
        await license_clear_expired_callback(event)
    elif data == "license_stats":
        await license_stats_callback(event)
    elif data == "license_export":
        await license_export_callback(event)
    elif data == "activate_license":
        await activate_license_callback(event)
    elif data == "license_status_user":
        await license_status_callback(event)
    elif data == "edit_rules":
        await edit_rules(event)
    elif data == "numbers_menu":
        await numbers_menu(event)
    elif data == "balance_menu":
        await balance_menu(event)
    elif data == "topup":
        await topup_menu(event)
    elif data.startswith("topup_amount:"):
        await set_topup_amount(event, data.split(":", 1)[1])
    elif data == "topup_custom":
        await ask_custom_topup(event)
    elif data.startswith("pay:"):
        await choose_payment_method(event, data.split(":", 1)[1])
    elif data == "payment_settings":
        await payment_settings_menu(event)
    elif data.startswith("payment_set_"):
        payment_keys = {
            "payment_set_stars_rate": "payment_stars_per_credit",
            "payment_set_ton_wallet": "payment_ton_wallet",
            "payment_set_ton_rate": "payment_ton_per_credit",
        }
        await edit_payment_setting(event, payment_keys[data])
    elif data == "developer_menu":
        await developer_menu(event)
    elif data == "control_menu":
        await control_menu(event)
    elif data == "engagement_menu":
        await event.edit(
            '🚀 **✦ مـركـز الـتـفـاعـل ✦**\n\n━━━━━━━━━━━━━━━━\n\n'
            'اختر خدمة التفاعل التي تناسب احتياجاتك من الخيارات المتوفرة.\n\n'
            '❤️ رشق التفاعلات.\n🗳️ رشق التصويت.\n💬 رشق التعليقات.\n'
            '👁️ زيادة المشاهدات.\n📖 رشق الستوري.\n🔎 رشق البحث.\n📢 الانضمام إلى قناة.\n\n'
            '━━━━━━━━━━━━━━━━\n📌 تختلف متطلبات كل خدمة بحسب نوع العملية والحسابات المتاحة.',
            buttons=engagement_keyboard(), parse_mode='markdown',
        )
    elif data == "premium_join":
        await premium_join_handler(event)
    elif data == "rent_numbers":
        await rent_numbers_handler(event)
    elif data == "view_rented":
        await view_rented_accounts(event)
    elif data == "rented_account_info":
        await event.answer("هذا الحساب مؤجّر للخدمات فقط ولا يمكن فتح إعداداته.", alert=True)
    elif data == "rental_settings":
        await rental_settings_handler(event)
    elif data == "achievements":
        await achievements_menu(event)
    elif data == "mass_react":
        await mass_react_handler(event)
    elif data == "mass_vote":
        await mass_vote_handler(event)
    elif data == "mass_story":
        await mass_story_handler(event)
    elif data == "mass_comment":
        await mass_comment_handler(event)
    elif data == "increment_views":
        await increment_views_handler(event)
    elif data == "auto_story_burst":
        await auto_story_burst_handler(event)
    elif data == "fund_auto_global":
        await funding_auto_global_menu(event)
    elif data == "fund_auto_global_on":
        await funding_auto_global_toggle(event, True)
    elif data == "fund_auto_global_off":
        await funding_auto_global_toggle(event, False)
    elif data == "join_owned_channel":
        await join_owned_channel_handler(event)
    elif data == "buy":
        await buy_number(event)
    elif data == "ssart":
        await withdraw_balance(event)
    elif data == "transfer":
        await transfer_balance(event)
    elif data == "supper":
        await support_request(event)
    elif data == "liscgh":
        await show_rules(event)
    elif data == "my_activity":
        await my_activity(event)
    elif data == "referral_menu":
        await referral_menu(event)
    elif data == "sell":
        await sell_account(event)
    elif data == "admin_panel":
        await admin_panel(event)
    elif data == "admin_bot_stats":
        await admin_bot_stats(event)
    elif data == "admin_operations":
        await admin_operations(event)
    elif data == "control_settings_super":
        await super_settings_menu(event)
    elif data == "control_settings_posting":
        await posting_settings_menu(event)
    elif data == "posting_settings_menu":
        await posting_settings_menu(event)
    elif data == "control_settings_creation":
        await creation_settings_menu(event)
    elif data == "control_settings_account":
        await account_settings_menu(event)
    elif data == "add_super":
        await add_super_channel_handler(event)
    elif data == "super_join_all" or data.startswith("super_join_account:"):
        await join_selected_accounts_to_super(event, data)
    elif data == "del_super":
        await del_super_channel_handler(event)
    elif data == "show_supers":
        await show_super_channels(event)
    elif data == "clear_supers":
        await clear_super_channels(event)
    elif data == "add_template":
        await add_posting_template_handler(event)
    elif data == "del_template":
        await del_posting_template_handler(event)
    elif data == "show_templates":
        await show_posting_templates(event)
    elif data == "clear_templates":
        await clear_posting_templates_handler(event)
    elif data == "edit_template":
        await edit_posting_template_handler(event)
    elif data.startswith("del_template:"):
        await delete_posting_template_callback(event, data)
    elif data.startswith("edit_template:"):
        await edit_posting_template_callback(event, data)
    elif data == "enable_multi_posting":
        await set_posting_setting(event, "multi_posting", "1")
    elif data == "disable_multi_posting":
        await set_posting_setting(event, "multi_posting", "0")
    elif data == "start_posting":
        await start_posting_handler(event)
    elif data == "posting_mode_all":
        await handle_all_templates_posting(event)
    elif data == "posting_mode_single":
        await handle_single_template_selection(event)
    elif data.startswith("posting_template:"):
        await handle_single_template_posting(event, data)
    elif data == "stop_posting_group":
        await stop_posting_group_handler(event)
    elif data.startswith("stop_posting:"):
        await stop_posting_channel_handler(event, data)
    elif data == "stop_all_posting":
        await stop_all_posting_handler(event)
    elif data == "manual_creation":
        await manual_group_creation_handler(event)
    elif data.startswith("control_accounts:"):
        action, accounts = await resolve_control_accounts(event, data)
        if not accounts:
            await event.answer("❌ الحساب المحدد لم يعد متاحاً.", alert=True)
        elif action == "manual_creation":
            await manual_group_creation_handler(event, accounts)
        elif action == "auto_creation":
            await auto_group_creation_handler(event, accounts)
        elif action == "timed_name_on":
            await toggle_timed_name(event, True, accounts)
        elif action == "timed_name_off":
            await toggle_timed_name(event, False, accounts)
    elif data == "stop_manual_creation":
        await stop_manual_creation_handler(event)
    elif data == "auto_creation":
        await auto_group_creation_handler(event)
    elif data == "stop_auto_creation":
        await stop_auto_creation_handler(event)
    elif data == "timed_name_on":
        await toggle_timed_name(event, True)
    elif data == "timed_name_off":
        await toggle_timed_name(event, False)
    elif data == "change_profile_photo":
        await change_profile_photo(event)
    elif data == "change_bio":
        await profile_edit_account_menu(event, "bio")
    elif data == "change_random_name":
        await change_random_names_for_all_accounts(event)
    elif data == "change_random_bio":
        await change_random_bios_for_all_accounts(event)
    elif data == "change_username":
        await profile_edit_account_menu(event, "username")
    elif data.startswith("profile_edit_account:"):
        await profile_edit_account_selected(event, data)
    elif data == "enable_stealth_mode":
        await enable_stealth_mode(event)
    elif data == "disable_stealth_mode":
        await disable_stealth_mode(event)
    elif data == "enable_notifications":
        await enable_notifications(event)
    elif data == "disable_notifications":
        await disable_notifications(event)
    elif data == "add_mandatory_channel":
        await add_mandatory_channel_handler(event)
    elif data == "fund_channel":
        await fund_channel_handler(event)
    elif data == "add_vip":
        await add_vip_callback(event)
    elif data in {
        "disable_self_destruct_save",
        "enable_self_destruct_save",
        "flash_members",
        "stop_flash_members",
        "show_monitors",
        "add_monitor_targets",
        "disable_monitoring",
        "enable_monitoring",
    }:
        await unsupported_account_feature(event)
    elif data == "save_restricted_post":
        await save_post_account_menu(event)
    elif data.startswith("save_post_account:"):
        await save_post_account_selected(event, data)
    elif data == "set_storage_group":
        await storage_account_menu(event, "set_group")
    elif data == "disable_incoming_storage":
        await storage_account_menu(event, "disable")
    elif data == "enable_incoming_storage":
        await storage_account_menu(event, "enable")
    elif data.startswith("storage_account:"):
        await storage_account_selected(event, data)
    elif data == "leave_groups_menu":
        await leave_groups_account_menu(event)
    elif data.startswith("leave_groups_account:"):
        await leave_groups_account_selected(event, data)
    elif data == "leave_all_groups_execute":
        await leave_all_groups_execute(event)
    elif data == "leave_channels_menu":
        await leave_channels_account_menu(event)
    elif data.startswith("leave_channels_account:"):
        await leave_channels_account_selected(event, data)
    elif data == "leave_specific_channel_menu":
        await leave_specific_channel_menu(event)
    elif data.startswith("leave_specific_channel:"):
        await leave_specific_channel_selected(event, data)
    elif data == "leave_all_channels_confirm":
        await leave_all_channels_confirm(event)
    elif data == "leave_all_channels_execute":
        await leave_all_channels_execute(event)
    elif data == "install_session":
        await install_session_handler(event)
    elif data == "install_sessions_txt":
        await install_sessions_txt_handler(event)
    elif data.startswith("country_"):
        await country_selected(event, data)
    elif data.startswith("countries_"):
        parts = data.split("_", 2)
        if len(parts) < 2 or not parts[1]:
            await event.answer("بيانات الدولة غير صالحة.", alert=True)
        else:
            await country_selected(event, f"country_{parts[1]}")
    elif data.startswith("select_account_"):
        await select_account(event, data)
    elif data.startswith("buy_"):
        await buy_confirmed(event, data)
    elif data.startswith("confirm_purchase:"):
        await confirm_purchase_handler(event, data)
    elif data.startswith("store_account:"):
        await store_account_handler(event, data)
    elif data.startswith("logout:"):
        await logout_account(event, data)
    elif data.startswith("sell_logout_"):
        await sell_logout_handler(event, data)
    elif data.startswith("show_"):
        await show_accounts(event, data)
    elif data.startswith("v:"):
        await account_details(event, data)
    elif data.startswith("del:"):
        await del_account_confirm(event, data)
    elif data.startswith("del_done:"):
        await del_account_done(event, data)
    elif data.startswith("rig_"):
        await add_number_process(event, data)
    elif data.startswith("toggle_engagement:"):
        await toggle_engagement(event, data)
    elif data.startswith("delete_engagement:"):
        await delete_engagement(event, data)
    elif data.startswith("delete_engagement_channel:"):
        await delete_engagement_channel(event, data)
    elif data.startswith("delete_"):
        await del_country(event, data)
    elif data.startswith("next_sell:"):
        await next_sell(event, data)
    elif data.startswith("check:"):
        await check_account(event, data)
    elif data.startswith("chs_"):
        await change_sell_price(event, data)
    elif data.startswith("chg_"):
        await change_buy_price(event, data)
    elif data == "add_force":
        await add_force_channel_handler(event)
    elif data == "del_force":
        await del_force_channel_handler(event)
    elif data == "add_admin":
        await add_admin(event)
    elif data == "del_admin":
        await del_admin(event)
    elif data == "add_coins":
        await add_coins(event)
    elif data == "del_coins":
        await del_coins(event)
    elif data == "ban":
        await ban_user(event)
    elif data == "unban":
        await unban_user(event)
    elif data.startswith("confirm_withdraw_"):
        await confirm_withdraw(event, data)
    elif data.startswith("reply_"):
        await reply_to_user(event, data)
    elif data.startswith("close_ticket_"):
        await close_ticket(event, data)
    elif data == "zip_all":
        await zip_database(event)
    elif data == "daily_backup":
        await zip_database(event)
    elif data == "all_of_number":
        await all_numbers_count(event)
    elif data == "add_country":
        await add_country_handler(event)
    elif data == "del_country":
        await del_country_menu(event)
    elif data == "add":
        await add_number_menu(event)
    elif data == "next_add_number_countries":
        await next_add_number_countries_page(event)
    elif data == "prev_add_number_countries":
        await prev_add_number_countries_page(event)
    elif data == "del_account":
        await del_account_menu(event)
    elif data == "cancel_operation":
        await cancel_operation(event)
    elif data == "main":
        await main_menu(event)
    elif data == "back":
        await buy_number(event)
    elif data == "accounts_view":
        await accounts_view_menu(event)
    elif data == "view_stored":
        await view_stored_accounts(event)
    elif data == "view_purchased":
        await view_purchased_accounts(event)
    elif data == "view_sold":
        await view_sold_accounts(event)
    elif data == "view_active":
        await view_active_accounts(event)
    elif data == "next_page_countries":
        await next_countries_page(event)
    elif data == "prev_page_countries":
        await prev_countries_page(event)
    elif data.startswith("account_actions:"):
        await account_actions_menu(event, data)
    elif data.startswith("purchased_account:") or data.startswith("bot_account:"):
        await show_account_options(event, data)
    elif data.startswith("select_acc_for_posting:"):
        await select_account_for_posting(event, data)
    elif data == "posting_all":
        await posting_all_channels(event)
    elif data == "posting_specific":
        await select_channel_for_posting(event)
    elif data.startswith("select_channel:"):
        await posting_specific_channel(event, data)
    elif data == "broadcast_message":
        await broadcast_start(event)
    elif data.startswith("broadcast_target:"):
        await broadcast_target_selected(event, data)
    elif data.startswith("del_super_channel:"):
        await handle_delete_super_channel(event, data)
    # إضافة معالجات أزرار التنصيب والمزاد
    elif data == "install_menu":
        await install_menu_handler(event)
    elif data == "delete_install":
        await delete_install_handler(event)
    elif data == "terminate_sessions":
        await terminate_sessions_menu(event)
    elif data.startswith("del_install:"):
        await delete_install_account(event, data)
    elif data == "extract_session":
        await extract_session_handler(event)
    elif data == "new_session_telethon":
        await create_new_session_handler(event, "telethon")
    elif data == "new_session_pyrogram":
        await create_new_session_handler(event, "pyrogram")
    elif data == "auction_menu":
        await auction_menu_handler(event)
    elif data == "add_auction":
        await add_auction_handler(event)
    elif data == "add_username_auction":
        await add_username_auction_handler(event)
    elif data == "username_auction_list":
        await username_auction_list_handler(event)
    elif data.startswith("view_username_auction:"):
        await view_username_auction_handler(event, data)
    elif data.startswith("username_bid:"):
        await place_username_bid_handler(event, data)
    elif data.startswith("close_username_auction:"):
        await close_username_auction_handler(event, data)
    elif data.startswith("bind_username_channel:"):
        await bind_username_channel_handler(event, data)
    elif data.startswith("verify_username_transfer:"):
        await verify_username_transfer_handler(event, data)
    elif data.startswith("ignore_username_bid:"):
        await ignore_username_bid_handler(event, data)
    elif data.startswith("end_username_auction:"):
        await end_username_auction_handler(event, data)
    elif data == "auction_list":
        await auction_list_handler(event)
    elif data.startswith("view_auction:"):
        await view_auction_handler(event, data)
    elif data.startswith("bid:"):
        await place_bid_handler(event, data)
    elif data.startswith("sell_auction:"):
        await sell_auction_handler(event, data)
    elif data.startswith("continue_auction:"):
        await continue_auction_handler(event, data)
    elif data.startswith("auction_logout:"):
        await auction_logout_handler(event, data)
    elif data.startswith("purchased_account:") or data.startswith("stored_account:") or data.startswith("bot_account:"):
        await show_account_options(event, data)
        
    elif data.startswith("store_existing:"):
        phone = data.split(':')[1]
        await store_existing_account(event, phone)

    elif data.startswith("get_code:"):
        parts = data.split(':')
        phone = parts[1]
        await get_verification_code(event, phone)

    elif data.startswith("logout_sessions:"):
        parts = data.split(':')
        phone = parts[1]
        await logout_account_sessions(event, phone)
    elif data == "auto_engagement_menu":
        await auto_engagement_menu(event)
    elif data == "add_engagement_channel":
        await add_engagement_channel(event)
    elif data == "choose_engagement_service":
        await choose_engagement_service(event)
    elif data.startswith("engagement_type:"):
        await engagement_type_selected(event, data)
    elif data.startswith("engagement_target_all:") or data.startswith("engagement_target:"):
        await engagement_channel_selected(event, data)
    elif data.startswith("engagement_period:"):
        await engagement_period_selected(event, data)
    elif data == "list_engagement_channels":
        await list_engagement_channels(event)
    else:
        # Old inline keyboards can remain visible after a deployment or a
        # menu refresh.  Always acknowledge them instead of leaving the
        # Telegram client spinning indefinitely.
        logger.warning("Received an unknown callback action")
        await event.answer("❌ هذا الزر لم يعد صالحاً. افتح القائمة مجدداً.", alert=True)
