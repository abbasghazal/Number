import inspect
import re

from telethon import Button as TelethonButton, types
from config import ADMIN_ID

TELETHON_INLINE_PARAMS = inspect.signature(TelethonButton.inline).parameters
TELETHON_URL_PARAMS = inspect.signature(TelethonButton.url).parameters
SUPPORTS_BUTTON_STYLE = "style" in TELETHON_INLINE_PARAMS
SUPPORTS_BUTTON_ICON = "icon" in TELETHON_INLINE_PARAMS
SUPPORTS_URL_BUTTON_STYLE = "style" in TELETHON_URL_PARAMS
SUPPORTS_URL_BUTTON_ICON = "icon" in TELETHON_URL_PARAMS

BUTTON_ICONS = {
    "primary": 5350344883519388891,
    "danger": 5350344883519388891,
    "success": 5350344883519388891,
}

# Telegram custom-emoji document IDs assigned to individual actions.  These
# take precedence over the generic icon selected from the button style.
BUTTON_ICONS_BY_DATA = {
    "control_menu": 6032716570109678163,
    "numbers_menu": 5399857047464333138,
    "add": 5399857047464333138,
    "balance_menu": 5372938056468812330,
    "accounts_view": 5400289971577827204,
    "install_menu": 5789640546645447251,
    "auction_menu": 5235666993268406129,
    "premium_join": 5345847511594584660,
    "engagement_menu": 6035163683561148861,
    "rent_numbers": 5400166160555585268,
    "supper": 6033052118724645353,
    "achievements": 5233340791736251240,
}
BACK_BUTTON_ICON = 5257967043289822840

# Child actions inherit their parent section's icon. Exact entries above still
# win, allowing an individual button to be customized later without changing
# the entire section.
SECTION_ICON_PREFIXES = (
    (6035163683561148861, ("mass_", "increment_views", "join_owned_channel", "add_engagement_", "choose_engagement_", "auto_story_", "list_engagement_", "engagement_", "react_")),
    (5372938056468812330, ("topup", "pay:", "transfer", "ssart", "add_coins", "del_coins", "set_referral_reward", "payment_", "ajkcoingl")),
    (5235666993268406129, ("add_auction", "auction_", "bid:", "sell_auction", "continue_auction", "username_")),
    (5399857047464333138, ("buy", "sell", "all_of_number", "add_country", "del_country", "del_account")),
    (5789640546645447251, ("install_", "extract_session", "delete_install", "del_install:")),
    (5400166160555585268, ("rent_", "rental_", "view_rented")),
    (5400289971577827204, ("accounts_view", "view_stored", "view_purchased", "view_sold", "view_active", "account_actions:")),
    (6032716570109678163, ("control_", "add_super", "del_super", "show_supers", "clear_supers", "add_template", "del_template", "show_templates", "clear_templates", "edit_template", "enable_multi_posting", "disable_multi_posting", "start_posting", "stop_posting", "manual_creation", "auto_creation", "timed_name_", "change_bio", "change_random_", "set_storage_group", "disable_incoming_storage", "enable_incoming_storage", "leave_groups_", "leave_channels_", "change_username")),
)


def button_icon_for(data, style, text=""):
    """Return an action-specific or inherited section icon."""
    if "رجوع" in str(text) or "العودة" in str(text):
        return BACK_BUTTON_ICON
    action = str(data or "")
    if action in BUTTON_ICONS_BY_DATA:
        return BUTTON_ICONS_BY_DATA[action]
    for icon, prefixes in SECTION_ICON_PREFIXES:
        if any(action == prefix or action.startswith(prefix) for prefix in prefixes):
            return icon
    return BUTTON_ICONS.get(style)

PLAIN_EMOJI_PATTERN = re.compile(
    "["
    "\u2190-\u21ff"  # arrows
    "\u2300-\u23ff"  # technical symbols
    "\u2600-\u27bf"  # miscellaneous symbols and dingbats
    "\U0001f000-\U0001faff"  # emoji and pictographs
    "]"
)


def remove_plain_button_emojis(text):
    """Keep button labels textual; premium button icons are added separately."""
    cleaned = PLAIN_EMOJI_PATTERN.sub("", str(text)).replace("\ufe0f", "")
    cleaned = re.sub(r"\(\s*\)", "", cleaned)
    return re.sub(r"\s+", " ", cleaned).strip()

SUCCESS_WORDS = (
    "تشغيل",
    "استئناف",
    "موافقة",
    "حفظ",
    "تأكيد",
    "تم",
    "شراء",
    "بيع",
    "إضافة",
    "اضف",
    "رفع",
    "تفعيل",
    "إنشاء",
    "انشاء",
    "بدء",
    "تحويل",
    "سحب",
    "تحقق",
)

DANGER_WORDS = (
    "تعطيل",
    "إنهاء",
    "رفض",
    "حذف",
    "إلغاء",
    "ايقاف",
    "إيقاف",
    "حظر",
    "خصم",
    "خروج",
    "الخروج",
    "مغادرة",
    "إلغاء مفتاح",
    "حذف المنتهية",
    "تأكيد",
    "تاكيد",
)


def button_style_for(text, data=None):
    text = str(text)
    if isinstance(data, (bytes, bytearray, memoryview)):
        data = bytes(data).decode('utf-8', errors='replace')
    data = str(data or "")

    if any(word in text for word in DANGER_WORDS) or data.startswith(("del", "delete", "ban", "stop", "cancel", "logout")):
        return "danger"

    if any(word in text for word in SUCCESS_WORDS) or data.startswith(("add", "confirm", "buy", "store", "enable", "license_create")):
        return "success"

    return "primary"


def ensure_button_color(button, color=None):
    """Fill a missing background without changing labels, payloads or icons."""
    if isinstance(button, str):
        if 'style' not in inspect.signature(TelethonButton.text).parameters:
            return button
        return TelethonButton.text(button, style=color or button_style_for(button))
    raw = getattr(button, 'button', button)
    if not hasattr(raw, 'style') or not hasattr(types, 'KeyboardButtonStyle'):
        return button
    style = raw.style
    if color is None and style is not None and any(getattr(style, f'bg_{name}', False) for name in ('primary', 'danger', 'success')):
        return button
    if style is None:
        style = types.KeyboardButtonStyle()
        raw.style = style
    if color is not None:
        for name in ('primary','danger','success'):
            setattr(style,f'bg_{name}',False)
    color = color or button_style_for(getattr(raw, 'text', ''), getattr(raw, 'data', None))
    setattr(style, f'bg_{color}', True)
    return button


class FixedButtonRows(list):
    """Explicit rows that the shared theme must color without rearranging."""


def _is_danger_action(button):
    """Confirmation, deletion and cancellation must remain red in every row."""
    raw = getattr(button, 'button', button)
    text = str(getattr(raw, 'text', button))
    data = getattr(raw, 'data', '')
    if isinstance(data, (bytes, bytearray, memoryview)):
        data = bytes(data).decode('utf-8', errors='replace')
    return any(word in text for word in DANGER_WORDS) or str(data or '').startswith(
        ('confirm', 'del', 'delete', 'ban', 'stop', 'cancel', 'logout', 'revoke', 'remove')
    )


def _row_color(row):
    """Apply the visual convention: one normal action is green, two are blue."""
    if any(_is_danger_action(button) for button in row):
        return 'danger'
    if len(row) == 1:
        return 'success'
    if len(row) == 2:
        return 'primary'
    return 'primary'


def ensure_button_colors(buttons):
    """Apply the shared row-based palette to inline and reply keyboards."""
    if isinstance(buttons,(list,tuple)):
        if buttons and all(isinstance(item, (list, tuple)) for item in buttons):
            output = [
                [ensure_button_color(button, _row_color(row)) for button in row]
                for row in buttons
            ]
            return FixedButtonRows(output) if isinstance(buttons, FixedButtonRows) else tuple(output) if isinstance(buttons, tuple) else output
        output=[]
        for item in buttons:
            output.append(ensure_button_color(item))
        return FixedButtonRows(output) if isinstance(buttons,FixedButtonRows) else tuple(output) if isinstance(buttons,tuple) else output
    if hasattr(buttons, 'rows'):
        for row in buttons.rows:
            items = list(getattr(row, 'buttons', row))
            color = _row_color(items)
            for button in items:
                ensure_button_color(button, color)
    elif hasattr(buttons, 'buttons'):
        items = list(buttons.buttons)
        color = _row_color(items)
        for button in items:
            ensure_button_color(button, color)
    else:
        return ensure_button_color(buttons)
    return buttons


class Button:
    @staticmethod
    def inline(text, data=None, style=None, icon=None):
        text = remove_plain_button_emojis(text)
        style = style or button_style_for(text, data)
        icon = icon if icon is not None else button_icon_for(data, style, text)

        kwargs = {"data": data}
        if SUPPORTS_BUTTON_STYLE:
            kwargs["style"] = style
        if SUPPORTS_BUTTON_ICON:
            kwargs["icon"] = icon

        return TelethonButton.inline(text, **kwargs)

    @staticmethod
    def url(text, url, style=None, icon=None, **kwargs):
        text = remove_plain_button_emojis(text)
        style = style or button_style_for(text, url)
        icon = icon if icon is not None else button_icon_for(url, style, text)

        if SUPPORTS_URL_BUTTON_STYLE:
            kwargs["style"] = style
        if SUPPORTS_URL_BUTTON_ICON:
            kwargs["icon"] = icon

        return TelethonButton.url(text, url=url, **kwargs)

    @staticmethod
    def text(text, *args, **kwargs):
        return ensure_button_color(TelethonButton.text(remove_plain_button_emojis(text), *args, **kwargs))

    @staticmethod
    def clear(*args, **kwargs):
        """Remove a temporary reply keyboard after a text-button choice."""
        return TelethonButton.clear(*args, **kwargs)

    @staticmethod
    def request_phone(text, *args, **kwargs):
        return ensure_button_color(TelethonButton.request_phone(text, *args, **kwargs))

    @staticmethod
    def request_location(text, *args, **kwargs):
        return ensure_button_color(TelethonButton.request_location(text, *args, **kwargs))

    @staticmethod
    def auth(text, *args, **kwargs):
        return ensure_button_color(TelethonButton.auth(text, *args, **kwargs))

    @staticmethod
    def switch_inline(text, *args, **kwargs):
        return ensure_button_color(TelethonButton.switch_inline(text, *args, **kwargs))

    @staticmethod
    def buy(text, *args, **kwargs):
        return ensure_button_color(TelethonButton.buy(text, *args, **kwargs))

    @staticmethod
    def game(text, *args, **kwargs):
        return ensure_button_color(TelethonButton.game(text, *args, **kwargs))

def arrange_buttons(buttons, pattern=(1, 2)):
    buttons = ensure_button_colors(buttons)
    rows = []
    index = 0
    pattern_index = 0
    while index < len(buttons):
        row_size = pattern[pattern_index % len(pattern)]
        rows.append(buttons[index:index + row_size])
        index += row_size
        pattern_index += 1
    return ensure_button_colors(rows)


def engagement_keyboard():
    """لوحة إجراءات التفاعل اليدوية مع المنشورات."""
    return arrange_buttons([
        Button.inline('الرشق المدفوع', data='promo:menu', style='success'),
        Button.inline('تمويل القنوات وجمع النقاط', data='fund:menu', style='primary'),
        Button.inline('رشق بحث 🔎', data='search_join'),
        Button.inline("• رشق تفاعل • ♥️", data="mass_react"),
        Button.inline("• رشق تصويت • 🗳️", data="mass_vote"),
        Button.inline("• رشق تعليق • 💬", data="mass_comment"),
        Button.inline("• زيادة مشاهدات • 👀", data="increment_views"),
        Button.inline("• رشق ستوري • 📖", data="mass_story"),
        Button.inline("• انضمام إلى قناة • 📢", data="join_owned_channel"),
        Button.inline("• رجوع • ↩️", data="main"),
    ], pattern=(1,))


def auto_engagement_settings_keyboard():
    return arrange_buttons([
        Button.inline("• إضافة قناة للمراقبة • ➕", data="add_engagement_channel"),
        Button.inline("• تفعيل المشاهدة والتفاعل • 🚀", data="choose_engagement_service"),
        Button.inline("• رشق ستوري من مخزون المطور • 📖", data="auto_story_burst"),
        Button.inline("• عرض الخدمات المفعلة • 👁️", data="list_engagement_channels"),
        Button.inline("• رجوع • ↩️", data="admin_panel")
    ])


def broadcast_target_keyboard():
    """خيارات وجهة الإذاعة الإدارية."""
    return arrange_buttons([
        Button.inline("• كافة المستخدمين • 👥", data="broadcast_target:users"),
        Button.inline("• المجموعات والقنوات • 📣", data="broadcast_target:chats"),
        Button.inline("• الجميع • 🌐", data="broadcast_target:all"),
        Button.inline("• رجوع • ↩️", data="admin_panel"),
    ], pattern=(1,))

def engagement_type_keyboard():
    return arrange_buttons([
        Button.inline("المشاهدة والتفاعل", data="engagement_type:views_react"),
        Button.inline("• رجوع • ↩️", data="auto_engagement_menu")
    ])


def engagement_period_keyboard(scope, channel_id=0):
    return arrange_buttons([
        Button.inline("يومي", data=f"engagement_period:{scope}:{channel_id}:daily"),
        Button.inline("أسبوعي", data=f"engagement_period:{scope}:{channel_id}:weekly"),
        Button.inline("شهري", data=f"engagement_period:{scope}:{channel_id}:monthly"),
        Button.inline("سنوي", data=f"engagement_period:{scope}:{channel_id}:yearly"),
        Button.inline("• رجوع • ↩️", data="choose_engagement_service"),
    ], pattern=(2, 2, 1))
def start_keyboard(user_id, is_admin=False):
    from ui_config import cached
    flat_buttons = [Button.inline(f'✦ {label} ✦', data=action, style='success' if action == 'numbers_menu' else None)
                    for action, label in cached()['items']]
    
    if user_id == ADMIN_ID or is_admin:
        flat_buttons.append(Button.inline("• لوحة تحكم المطور • 👑", data="admin_panel"))
    
    return arrange_buttons(flat_buttons)

def license_access_keyboard():
    return arrange_buttons([
        Button.inline("• القائمة الرئيسية •", data="main"),
    ])

def numbers_menu_keyboard():
    return arrange_buttons([
        Button.inline('حجوزات الشراء المؤجل', data='deferred:list:0'),
        Button.inline("• شراء رقم • ✅", data="buy"),
        Button.inline("• بيع حساب • 💰", data="sell"),
        Button.inline("• رجوع • ↩️", data="main")
    ])

def balance_menu_keyboard():
    return arrange_buttons([
        Button.inline("• شحن الرصيد • ➕", data="topup"),
        Button.inline("• تحويل الرصيد • 🔄", data="transfer"),
        Button.inline("• سحب الرصيد • 💳", data="ssart"),
        Button.inline("• رجوع • ↩️", data="main"),
    ], pattern=(2, 1))

def payment_methods_keyboard():
    return arrange_buttons([
        Button.inline("• تيليجرام ستارز • ⭐", data="pay:stars"),
        Button.inline("• TON • 💎", data="pay:ton"),
        Button.inline("• رجوع • ↩️", data="balance_menu"),
    ], pattern=(1, 1, 1))

def payment_amount_keyboard():
    return arrange_buttons([
        Button.inline("1$", data="topup_amount:1"),
        Button.inline("5$", data="topup_amount:5"),
        Button.inline("10$", data="topup_amount:10"),
        Button.inline("• مبلغ آخر • ✏️", data="topup_custom"),
        Button.inline("• رجوع • ↩️", data="balance_menu"),
    ], pattern=(3, 1, 1))

def developer_menu_keyboard(is_developer=False):
    buttons = [
        Button.url("• التواصل مع المطور • 👨‍💻", url="https://t.me/SHAHM41", style="danger"),
        Button.inline("• رجوع • ↩️", data="main")
    ]
    if is_developer:
        buttons.insert(1, Button.inline('إدارة الرشق والنقاط', data='promo:admin', style='success'))
        buttons.insert(2, Button.inline('أسعار الاشتراك الإجباري', data='mandatory:prices'))
        buttons.insert(1, Button.inline('تذاكر الدعم والرد', data='feature:tickets:staff:0'))
        buttons.insert(2,Button.inline('إعدادات البيع والشراء حسب النوع',data='num:prices'))
        buttons.insert(3,Button.inline('مراجعة أنواع الأرقام والبيع',data='num:reviews'))
        buttons.insert(4,Button.inline('الحسابات المصرح بإدارتها',data='acct:accounts:0'))
    return arrange_buttons(buttons)

def control_menu_keyboard(is_admin=False):
    """Show a consistent control menu to every user.

    Authorization is enforced by the action handlers for global settings.
    """
    buttons = [
        Button.inline('شهم · التجميع والتحويل والهدية', data='shahm:menu'),
        Button.inline("• اعدادات السوبر • ⚡️", data="control_settings_super"),
        Button.inline("• اعدادات النشر • 📢", data="control_settings_posting"),
        Button.inline("• اعدادات الانشاء • 👥", data="control_settings_creation"),
        Button.inline("• اعدادات الحساب • 👤", data="control_settings_account"),
    ]
    buttons.append(Button.inline("• رجوع • ↩️", data="main"))
    return arrange_buttons(buttons)

def admin_panel_keyboard():
    return arrange_buttons([
        Button.inline('مراجعة أنواع الأرقام والبيع 📋',data='num:reviews'),
        Button.inline('أتمتة حساباتي 🤖', data='auto:menu'),
        Button.inline('تحكم التجميع التلقائي للتمويل', data='fund_auto_global'),
        Button.inline('الحسابات المصرح بإدارتها 👥', data='acct:accounts:0'),
        Button.inline('صلاحيات المشرفين', data='feature:staff:list:0'),
        Button.inline('إدارة الكوبونات', data='feature:coupons:admin:0'),
        Button.inline('تقارير ومصاريف وتكاليف', data='feature:reports:view:month'),
        Button.inline("• مراجعة السحب والاسترداد •", data="ops:withdraw:list:0"),
        Button.inline("• مراقبة النسخ والاستعادة •", data="ops:backup:list:0"),
        Button.inline("• اعدادات الارقام • ⚙️", data="ajxjao"),
        Button.inline("• الاشتراك الاجباري • 📢", data="ajxkho"),
        Button.inline("• قسم الادمنيه • 👨‍✈️", data="aksgl"),
        Button.inline("• قسم البيع والشراء • ♻️", data="ajkofgl"),
        Button.inline("• قسم الرصيد • 💰", data="ajkcoingl"),
        Button.inline("• قسم الحظر • 🚫", data="bbvjls"),
        Button.inline("• إحصائيات البوت • 📊", data="admin_bot_stats"),
        Button.inline("• حالة التشغيل • 🩺", data="admin_operations"),
        Button.inline("• التقارير المالية • 📈", data="ops:reports:list:day"),
        Button.inline("• تذاكر الدعم • 🎫", data="ops:tickets:list:0"),
        Button.inline("• حالة الخدمة المفصلة • 🩺", data="ops:service:list:0"),
        Button.inline("• النسخ الاحتياطي اليومي • 🗄", data="daily_backup"),
        Button.inline("• قناة اثباتات التسليم • 📢", data="set_trust_channel"),
        Button.inline("• إذاعة عامة 📢", data="broadcast_message"),
        Button.inline("• تعديل رسالة القوانين • 📜", data="edit_rules"),
        Button.inline("• قناة إلزامية للحسابات • 🔒", data="add_mandatory_channel"),
        Button.inline("• تمويل قناة •", data="fund_channel"),
        Button.inline("• رفع مميز • ⭐", data="add_vip"),
        Button.inline("• الرشق التلقائي • 🔄", data="auto_engagement_menu"),
        Button.inline("• إعدادات تأجير الأرقام • ⏳", data="rental_settings"),
        Button.inline("• العودة للقائمة الرئيسية • ↩️", data="main")
    ])

def license_settings_keyboard():
    return arrange_buttons([
        Button.inline("• إنشاء مفتاح • ➕", data="license_create"),
        Button.inline("• عرض المفاتيح • 🔑", data="license_list"),
        Button.inline("• إلغاء مفتاح • 🚫", data="license_revoke"),
        Button.inline("• حذف المنتهية • 🗑️", data="license_clear_expired"),
        Button.inline("• إحصائيات المفاتيح • 📊", data="license_stats"),
        Button.inline("• تصدير المفاتيح • 📄", data="license_export"),
        Button.inline("• رجوع • ↩️", data="admin_panel"),
        Button.inline("• العودة للقائمة الرئيسية • ↩️", data="main")
    ])

def num_settings_keyboard():
    return arrange_buttons([
        Button.inline("• عدد ارقام البوت • 🔢", data="all_of_number"),
        Button.inline("• إضافة دولة • 🌍", data="add_country"),
        Button.inline("• حذف دولة • 🗑️", data="del_country"),
        Button.inline("• إضافة رقم • ➕", data="add"),
        Button.inline("• حذف رقم • ➖", data="del_account"),
        Button.inline("• رجوع • ↩️", data="admin_panel"),
        Button.inline("• العودة للقائمة الرئيسية • ↩️", data="main")
    ])

def force_settings_keyboard():
    return arrange_buttons([
        Button.inline("• إضافة قناة • ➕", data="add_force"),
        Button.inline("• حذف قناة • ➖", data="del_force"),
        Button.inline("• أسعار قنوات المستخدمين • 💳", data="mandatory:prices"),
        Button.inline("• رجوع • ↩️", data="admin_panel"),
        Button.inline("• العودة للقائمة الرئيسية • ↩️", data="main")
    ])

def admin_settings_keyboard():
    return arrange_buttons([
        Button.inline("• إضافة ادمن • ➕", data="add_admin"),
        Button.inline("• حذف ادمن • ➖", data="del_admin"),
        Button.inline("• تشغيل التجميع الدوري للتمويل • 🔄", data="fund_auto_global"),
        Button.inline("• رجوع • ↩️", data="admin_panel"),
        Button.inline("• العودة للقائمة الرئيسية • ↩️", data="main")
    ])

def buy_sell_settings_keyboard():
    return arrange_buttons([
        Button.inline('الأسعار حسب النوع والدولة', data='num:prices'),
        Button.inline('مراجعة أنواع الأرقام والبيع', data='num:reviews'),
        Button.inline("• رجوع • ↩️", data="admin_panel"),
        Button.inline("• العودة للقائمة الرئيسية • ↩️", data="main")
    ])

def balance_settings_keyboard():
    return arrange_buttons([
        Button.inline("• إضافة رصيد • ➕", data="add_coins"),
        Button.inline("• خصم رصيد • ➖", data="del_coins"),
        Button.inline("• مكافأة الإحالة • 🎁", data="set_referral_reward"),
        Button.inline("• إعدادات الدفع • 💳", data="payment_settings"),
        Button.inline("• رجوع • ↩️", data="admin_panel"),
        Button.inline("• العودة للقائمة الرئيسية • ↩️", data="main")
    ])

def payment_settings_keyboard():
    return arrange_buttons([
        Button.inline("• سعر الرصيد بالنجوم • ⭐", data="payment_set_stars_rate"),
        Button.inline("• أسعار الأرقام بالنجوم • ⭐", data="stars:prices:0"),
        Button.inline("• محفظة TON • 💎", data="payment_set_ton_wallet"),
        Button.inline("• سعر الرصيد بـ TON • 💎", data="payment_set_ton_rate"),
        Button.inline("• عرض الإعدادات • 👁️", data="payment_settings"),
        Button.inline("• رجوع • ↩️", data="ajkcoingl"),
    ], pattern=(1, 1, 1, 1))

def ban_settings_keyboard():
    return arrange_buttons([
        Button.inline("• حظر عضو • ⛔", data="ban"),
        Button.inline("• رفع حظر • ✅", data="unban"),
        Button.inline("• رجوع • ↩️", data="admin_panel"),
        Button.inline("• العودة للقائمة الرئيسية • ↩️", data="main")
    ])

def super_settings_keyboard():
    return arrange_buttons([
        Button.inline("• إضافة سوبر • ➕", data="add_super"),
        Button.inline("• حذف سوبر • ➖", data="del_super"),
        Button.inline("• عرض السوبرات المضافه • 👁️", data="show_supers"),
        Button.inline("• حذف السوبرات المضافه • 🗑️", data="clear_supers"),
        Button.inline("• رجوع • ↩️", data="control_menu")
    ])

def posting_settings_keyboard():
    return arrange_buttons([
        Button.inline("• إضافة كليشة نشر • ➕", data="add_template"),
        Button.inline("• حذف كليشة نشر • ➖", data="del_template"),
        Button.inline("• عرض كلايش المضافة • 👁️", data="show_templates"),
        Button.inline("• حذف الكلايش المضافة • 🗑️", data="clear_templates"),
        Button.inline("• تعديل كليشة مضافة • ✏️", data="edit_template"),
        Button.inline("• تفعيل النشر المتعدد • ✅", data="enable_multi_posting"),
        Button.inline("• إيقاف النشر المتعدد • ❌", data="disable_multi_posting"),
        Button.inline("• بدء النشر • 🚀", data="start_posting"),
        Button.inline("• إيقاف النشر في مجموعة معينه • ⏸️", data="stop_posting_group"),
        Button.inline("• إيقاف النشر في جميع السوبرات • ■", data="stop_all_posting"),
        Button.inline("• رجوع • ↩️", data="control_menu")
    ])

def creation_settings_keyboard():
    return arrange_buttons([
        Button.inline("• انشاء مجموعات يدوي • 🛠️", data="manual_creation"),
        Button.inline("• ايقاف انشاء مجموعات يدوي • ⏹️", data="stop_manual_creation"),
        Button.inline("• انشاء مجموعات تلقائي • 🤖", data="auto_creation"),
        Button.inline("• ايقاف انشاء مجموعات تلقائي • ⏹️", data="stop_auto_creation"),
        Button.inline("• رجوع • ↩️", data="control_menu")
    ])

def account_settings_keyboard():
    return arrange_buttons([
        Button.inline('أتمتة حساباتي 🤖', data='auto:menu'),
        Button.inline("• إدارة كلمات 2FA •", data="ops:twofa:list:0"),
        Button.inline("• تنظيم الحسابات •", data="ops:accounts:list:0"),
        Button.inline("• صحة الحسابات • 🩺", data="health:page:0"),
        Button.inline("• تفعيل اسم وقتي • ✅", data="timed_name_on"),
        Button.inline("• ايقاف الاسم الوقتي • ❌", data="timed_name_off"),
        Button.inline("• تغيير البايو • 📝", data="change_bio"),
        Button.inline("• بايو عشوائي • 🎲", data="change_random_bio"),
        Button.inline("• تغيير الاسم • 🎲", data="change_random_name"),
        Button.inline("• اضافة كروب تخزين • 🗂️", data="set_storage_group"),
        Button.inline("• تعطيل التخزين • ❌", data="disable_incoming_storage"),
        Button.inline("• تفعيل التخزين • ✅", data="enable_incoming_storage"),
        Button.inline("• مغادرة الكروبات • 🚪", data="leave_groups_menu"),
        Button.inline("• مغادرة القنوات • 🚪", data="leave_channels_menu"),
        Button.inline("• تغيير اسم المستخدم • 👤", data="change_username"),
        Button.inline("• رجوع • ↩️", data="control_menu")
    ])

def accounts_view_keyboard():
    return arrange_buttons([
        Button.inline('أذونات إدارة حساباتي 🔐', data='acct:mine'),
        Button.inline("• إدارة كلمات 2FA •", data="ops:twofa:list:0"),
        Button.inline("• تنظيم الحسابات •", data="ops:accounts:list:0"),
        Button.inline("• عمليات جماعية آمنة •", data="ops:bulk:list:0"),
        Button.inline("• الإشعارات • 🔔", data="ops:notifications:list:0"),
        Button.inline("• جدولة المهام • 🗓️", data="ops:schedule:list:0"),
        Button.inline("• إيجاراتي والتجديد •", data="ops:rent:list:0"),
        Button.inline("• صحة الحسابات • 🩺", data="health:page:0"),
        Button.inline("• عرض الحسابات المخزنة • 💾", data="view_stored"),
        Button.inline("• عرض الحسابات المشتراة • 🛒", data="view_purchased"),
        Button.inline("• عرض الحسابات المستأجرة • ⏳", data="view_rented"),
        Button.inline("• عرض الحسابات المباعة • 💰", data="view_sold"),
        Button.inline("• عرض الحسابات النشطة • 🔥", data="view_active"),
        Button.inline("• رجوع • ↩️", data="main")
    ])

def install_menu_keyboard():
    return arrange_buttons([
        Button.inline("• تنصيب حساب • 📱", data="install_session"),
        Button.inline("• استخراج الجلسة • 🔐", data="extract_session"),
        Button.inline("• تنصيب ملفات TXT • 📄", data="install_sessions_txt"),
        Button.inline("• حذف تنصيب • 🗑️", data="delete_install"),
        Button.inline("• إنهاء الجلسات • 🚪", data="terminate_sessions"),
        Button.inline("• رجوع • ↩️", data="main")
    ])

def auction_menu_keyboard():
    return arrange_buttons([
        Button.inline("• اضف مزاد • ➕", data="add_auction"),
        Button.inline("• مزاد يوزر • @", data="add_username_auction"),
        Button.inline("• قائمة اليوزرات • 📋", data="username_auction_list"),
        Button.inline("• قائمة المزاد • 📋", data="auction_list"),
        Button.inline("• رجوع • ↩️", data="main")
    ])

def reaction_buttons():
    return arrange_buttons([
        Button.inline("إعجاب", data="react_like"),
        Button.inline("حب", data="react_love"),
        Button.inline("نار", data="react_fire"),
        Button.inline("احتفال", data="react_celebration"),
        Button.inline("دهشة", data="react_wow"),
        Button.inline("حزن", data="react_sad"),
        Button.inline("ضحك", data="react_laugh"),
        Button.inline("• رجوع • ↩️", data="control_settings_account")
    ])

def cancel_operation_keyboard():
    return [[Button.inline("• إلغاء العملية • ❌", data="cancel_operation")]]

def back_button():
    return [Button.inline("• رجوع • ↩️", data="main")]
