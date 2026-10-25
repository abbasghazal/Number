# Auto-split from handlers.py. Keep shared imports/state in base.py.
import asyncio
import datetime
import logging
import html
from decimal import Decimal, InvalidOperation
from telethon import Button
from config import ADMIN_ID, DEVELOPER_ID, LICENSE_ENFORCEMENT_ENABLED
from helpers import *
from keyboards import *

# إعداد نظام تسجيل الأخطاء
logger = logging.getLogger(__name__)
TELEGRAM_NOTIFICATIONS_USER_ID = 777000

# متغيرات حالة المحادثة
active_conversations = {}
manual_creation_tasks = {}
auto_creation_tasks = {}
posting_active = False
user_pages = {}
price_country_pages = {}
add_number_country_pages = {}
user_purchased_accounts = {}
user_posting_accounts = {}
user_posting_modes = {}
user_posting_targets = {}
user_control_account_choices = {}
user_operation_locks = {}
rate_limit_state = {}
user_leave_channel_accounts = {}
user_leave_channel_targets = {}
user_leave_group_accounts = {}
incoming_storage_clients = {}
pending_super_additions = {}
bot = None
LICENSE_FREE_COMMANDS = ("/activate", "/status", "/mylicense", "/paysupport")
LICENSE_ADMIN_COMMANDS = ("/genlicense", "/licenses", "/revokelicense", "/clearlicenses", "/licensestats", "/exportlicenses")
LICENSE_FREE_CALLBACKS = ("activate_license", "license_status_user", "cancel_operation", "topup")
NUMBERS_FREE_CALLBACKS = {
    "numbers_menu", "balance_menu", "buy", "sell", "install_menu", "auction_menu", "accounts_view",
    "referral_menu", "my_activity",
    "topup",
    "supper", "ssart", "transfer", "liscgh", "install_session", "extract_session", "new_session_telethon", "new_session_pyrogram", "delete_install", "terminate_sessions",
    "view_stored", "view_purchased", "view_sold", "view_active", "back",
    "next_page_countries", "prev_page_countries", "auction_list", "add_auction",
    "add_username_auction", "username_auction_list",
}
NUMBERS_FREE_CALLBACK_PREFIXES = (
    'num:', 'acct:', 'search:',
    'deferred:', 'ui:',
    "stars:",
    "ops:",
    "stmt:",
    "health:",
    "country_", "select_account_", "buy_", "confirm_purchase:", "store_account:",
    "logout:", "logout_sessions:", "sell_logout_", "show_", "v:", "check:",
    "account_actions:", "purchased_account:", "stored_account:", "bot_account:",
    "del_install:", "view_auction:", "bid:", "sell_auction:",
    "continue_auction:", "auction_logout:", "username_bid:", "view_username_auction:", "close_username_auction:", "end_username_auction:", "bind_username_channel:", "verify_username_transfer:", "ignore_username_bid:", "store_existing:", "get_code:",
    "confirm_withdraw_", "next_sell:",
    "pay:",
    "topup_",
)
NUMBERS_FREE_TEXTS = ("قسم الأرقام", "شراء رقم", "بيع حساب", "تنصيب حساب")
FREE_TRIAL_HOURS = 24



def get_user_lock(user_id):
    if user_id not in user_operation_locks:
        user_operation_locks[user_id] = asyncio.Lock()
    return user_operation_locks[user_id]


async def enforce_rate_limit(event, action, seconds=1.0):
    """Small in-memory throttle for interactive actions and callback floods."""
    user_id = getattr(event, "sender_id", None) or event.chat_id
    key = (user_id, action)
    now = asyncio.get_running_loop().time()
    # Bound the in-memory cache; entries older than an hour have no value.
    if len(rate_limit_state) > 5000:
        cutoff = now - 3600
        stale_keys = [item for item, timestamp in rate_limit_state.items() if timestamp < cutoff]
        for stale_key in stale_keys:
            rate_limit_state.pop(stale_key, None)
    previous = rate_limit_state.get(key, 0.0)
    if now - previous >= seconds:
        rate_limit_state[key] = now
        return True
    try:
        await event.answer("⏳ يرجى الانتظار قليلاً قبل إعادة المحاولة.", alert=True)
    except Exception:
        pass
    return False


def is_numbers_free_callback(data):
    data = str(data or "")
    return data in NUMBERS_FREE_CALLBACKS or data.startswith(NUMBERS_FREE_CALLBACK_PREFIXES)



def pending_purchase_status(purchase):
    if len(purchase) > 9 and purchase[8] in (0, 1, False, True):
        return purchase[9]
    return purchase[8] if len(purchase) > 8 else "pending"


def mask_phone_number(phone):
    digits = str(phone).replace("+", "").strip()
    if not digits:
        return "+****"

    visible_count = max(1, len(digits) // 2)
    return f"+{digits[:visible_count]}{'*' * (len(digits) - visible_count)}"


async def get_buyer_display(client, user_id):
    try:
        entity = await client.get_entity(user_id)
        name_parts = [getattr(entity, "first_name", None), getattr(entity, "last_name", None)]
        full_name = " ".join(part for part in name_parts if part).strip()
        username = f"@{entity.username}" if getattr(entity, "username", None) else ""
        display_name = full_name or username or "غير معروف"
    except Exception:
        user_data = await get_user(user_id)
        display_name = " ".join(
            part for part in [
                user_data.get("first_name") if user_data else None,
                user_data.get("last_name") if user_data else None
            ] if part
        ).strip() or (user_data.get("username") if user_data and user_data.get("username") else "غير معروف")

    return html.escape(display_name)


async def publish_purchase_proof(client, user_id, phone, country_name=None, price=None):
    trust_channel = await get_setting("trust_channel")
    if not trust_channel:
        return

    buyer_name = await get_buyer_display(client, user_id)
    masked_phone = mask_phone_number(phone)
    now = datetime.datetime.now().strftime("%Y-%m-%d %H:%M")

    country_line = f"• الدولة: {html.escape(str(country_name))}\n" if country_name else ""
    price_line = f"• السعر: {html.escape(str(price))} $\n" if price is not None else ""
    proof_text = (
        "✅ <b>إثبات تسليم رقم جديد</b>\n\n"
        f"• المشتري: <a href=\"tg://user?id={user_id}\">{buyer_name}</a>\n"
        f"• ايدي المشتري: <code>{user_id}</code>\n"
        f"{country_line}"
        f"• الرقم: <code>{masked_phone}</code>\n"
        f"{price_line}"
        "• الحالة: تم التسليم بنجاح ✅\n"
        f"• التاريخ: {now}"
    )

    try:
        bot_info = await client.get_me()
        buttons = None
        if getattr(bot_info, "username", None):
            buttons = [[Button.url("• الدخول الى البوت •", f"https://t.me/{bot_info.username}?start=start")]]

        await client.send_message(trust_channel, proof_text, parse_mode="html", buttons=buttons)
    except Exception as exc:
        logger.error(f"Failed to publish purchase proof: {str(exc)}")


def license_required_message(reason, expires_at=None):
    if reason == "trial_expired":
        expired_text = f"\n• انتهت الفترة المجانية بتاريخ: {expires_at.strftime('%Y-%m-%d %H:%M')}" if expires_at else ""
        return (
            "⏱ انتهت فترة التجربة المجانية الخاصة بك.\n"
            f"{expired_text}\n\n"
            "للاستمرار في استخدام البوت يرجى شراء مفتاح تفعيل من المطور: @SHahM41\n"
            "ثم أرسل:\n"
            "`/activate YOUR-KEY`"
        )

    if reason == "expired":
        expired_text = f"\n• انتهى بتاريخ: {expires_at.strftime('%Y-%m-%d %H:%M')}" if expires_at else ""
        return (
            "❌ انتهت صلاحية اشتراكك في البوت.\n"
            f"{expired_text}\n\n"
            f"للتجديد يرجى شراء مفتاح جديد من المطور: @SHahM41\n"
            "ثم أرسل:\n"
            "`/activate YOUR-KEY`"
        )

    if reason == "invalid":
        return (
            "❌ ترخيصك غير صالح أو تم إلغاؤه.\n\n"
            "يرجى التواصل مع المطور: @SHahM41\n"
            "ثم تفعيل مفتاح جديد عبر:\n"
            "`/activate YOUR-KEY`"
        )

    return (
        "🔐 البوت يعمل بنظام الاشتراك المدفوع.\n\n"
        "للاستخدام يرجى شراء مفتاح تفعيل من المطور: @SHahM41\n"
        "بعد استلام المفتاح أرسل:\n"
        "`/activate YOUR-KEY`\n\n"
        "لمعرفة حالة اشتراكك أرسل: `/status`"
    )


async def get_free_trial_status(user_id, create_if_missing=False):
    user_data = await get_user(user_id)
    if not user_data and create_if_missing:
        await create_user(user_id)
        user_data = await get_user(user_id)
    if not user_data:
        return {"allowed": False, "reason": "missing", "expires_at": None}

    join_date = parse_db_datetime(user_data.get("join_date"))
    if not join_date:
        return {"allowed": False, "reason": "trial_expired", "expires_at": None}

    trial_expires_at = join_date + datetime.timedelta(hours=FREE_TRIAL_HOURS)
    if trial_expires_at > datetime.datetime.now():
        return {"allowed": True, "reason": "trial", "expires_at": trial_expires_at}
    return {"allowed": False, "reason": "trial_expired", "expires_at": trial_expires_at}


async def user_has_license_access(user_id):
    if not LICENSE_ENFORCEMENT_ENABLED:
        return True, {"reason": "public"}
    if await is_license_admin(user_id):
        return True, {"reason": "admin"}
    status = await get_license_status(user_id)
    if status["allowed"]:
        return True, status
    trial = await get_free_trial_status(user_id)
    return trial["allowed"], trial


async def enforce_license_for_message(event):
    if not LICENSE_ENFORCEMENT_ENABLED:
        return True
    allowed, status = await user_has_license_access(event.chat_id)
    if allowed:
        return True
    await event.respond(license_required_message(status.get("reason"), status.get("expires_at")))
    return False


async def enforce_license_for_callback(event):
    if not LICENSE_ENFORCEMENT_ENABLED:
        return True
    allowed, status = await user_has_license_access(event.chat_id)
    if allowed:
        return True
    await event.answer("❌ يلزم تفعيل اشتراكك لاستخدام هذه الخدمة.", alert=True)
    return False


def format_remaining_time(expires_at):
    if not expires_at:
        return "غير معروف"
    delta = expires_at - datetime.datetime.now()
    if delta.total_seconds() <= 0:
        return "منتهي"
    days = delta.days
    hours = delta.seconds // 3600
    minutes = (delta.seconds % 3600) // 60
    return f"{days} يوم و {hours} ساعة و {minutes} دقيقة"


async def is_license_admin(user_id):
    return user_id == ADMIN_ID or await is_admin(user_id)


async def ensure_admin(event, permission=None):
    """Enforce administrator access at the operation itself, not only in UI routing."""
    user_id = getattr(event, "sender_id", None) or event.chat_id
    from staff_security import caller_permission, caller_name, has_permission, is_staff
    permission = permission or caller_permission()
    allowed = await is_staff(user_id) if permission == 'staff' else await has_permission(user_id, permission)
    if allowed:
        await log_audit_event(user_id, f"admin_action:{caller_name()}")
        return True
    try:
        await event.answer("❌ لا تملك الصلاحية المطلوبة لهذه العملية.", alert=True)
    except Exception:
        await event.respond("❌ هذه العملية متاحة للمطور والمشرفين فقط.")
    return False


async def ensure_owner(event):
    """Actions that change trust boundaries belong to the configured owner only."""
    user_id = getattr(event, "sender_id", None) or event.chat_id
    if user_id == ADMIN_ID:
        return True
    try:
        await event.answer("❌ هذه العملية متاحة للمطور الأساسي فقط.", alert=True)
    except Exception:
        await event.respond("❌ هذه العملية متاحة للمطور الأساسي فقط.")
    return False


async def ensure_developer(event):
    """Restrict deployment-level actions to the explicitly configured developer."""
    user_id = getattr(event, "sender_id", None) or event.chat_id
    if user_id == DEVELOPER_ID:
        return True
    try:
        await event.answer("❌ هذه العملية متاحة للمطور فقط.", alert=True)
    except Exception:
        await event.respond("❌ هذه العملية متاحة للمطور فقط.")
    return False


def parse_positive_amount(value, *, minimum=Decimal("0")) -> Decimal:
    """Parse money safely; rejects negative, NaN and infinite values."""
    try:
        amount = Decimal(str(value).strip())
        minimum_amount = Decimal(str(minimum))
    except (InvalidOperation, ValueError):
        raise ValueError("amount must be finite and positive")
    if not amount.is_finite() or amount <= minimum_amount:
        raise ValueError("amount must be finite and positive")
    return amount.quantize(Decimal("0.01"))


def license_state_text(row):
    used_by = row[4]
    expires_at = parse_db_datetime(row[6])
    is_revoked = int(row[8] or 0) == 1
    if is_revoked:
        return "ملغي"
    if used_by and expires_at and expires_at <= datetime.datetime.now():
        return "منتهي"
    if used_by:
        return "مستخدم"
    return "غير مستخدم"
