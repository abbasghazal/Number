import inspect

from telethon import Button as TelethonButton
from config import ADMIN_ID

TELETHON_INLINE_PARAMS = inspect.signature(TelethonButton.inline).parameters
TELETHON_URL_PARAMS = inspect.signature(TelethonButton.url).parameters
SUPPORTS_BUTTON_STYLE = "style" in TELETHON_INLINE_PARAMS
SUPPORTS_BUTTON_ICON = "icon" in TELETHON_INLINE_PARAMS
SUPPORTS_URL_BUTTON_STYLE = "style" in TELETHON_URL_PARAMS
SUPPORTS_URL_BUTTON_ICON = "icon" in TELETHON_URL_PARAMS

BUTTON_ICONS = {
    "primary": 6323256281456970434,
    "danger": 5447645982218017334,
    "success": 5436562963469052418,
}

SUCCESS_WORDS = (
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
)


def button_style_for(text, data=None):
    text = str(text)
    data = str(data or "")

    if any(word in text for word in DANGER_WORDS) or data.startswith(("del", "delete", "ban", "stop", "cancel", "logout")):
        return "danger"

    if any(word in text for word in SUCCESS_WORDS) or data.startswith(("add", "confirm", "buy", "store", "enable", "license_create")):
        return "success"

    return "primary"


class Button:
    @staticmethod
    def inline(text, data=None, style=None, icon=None):
        style = style or button_style_for(text, data)
        icon = icon or BUTTON_ICONS.get(style)

        kwargs = {"data": data}
        if SUPPORTS_BUTTON_STYLE:
            kwargs["style"] = style
        if SUPPORTS_BUTTON_ICON:
            kwargs["icon"] = icon

        return TelethonButton.inline(text, **kwargs)

    @staticmethod
    def url(text, url, style=None, icon=None, **kwargs):
        style = style or button_style_for(text, url)
        icon = icon or BUTTON_ICONS.get(style)

        if SUPPORTS_URL_BUTTON_STYLE:
            kwargs["style"] = style
        if SUPPORTS_URL_BUTTON_ICON:
            kwargs["icon"] = icon

        return TelethonButton.url(text, url=url, **kwargs)

    @staticmethod
    def text(text, *args, **kwargs):
        return TelethonButton.text(text, *args, **kwargs)

    @staticmethod
    def request_phone(text, *args, **kwargs):
        return TelethonButton.request_phone(text, *args, **kwargs)

    @staticmethod
    def request_location(text, *args, **kwargs):
        return TelethonButton.request_location(text, *args, **kwargs)

    @staticmethod
    def auth(text, *args, **kwargs):
        return TelethonButton.auth(text, *args, **kwargs)

    @staticmethod
    def switch_inline(text, *args, **kwargs):
        return TelethonButton.switch_inline(text, *args, **kwargs)

    @staticmethod
    def buy(text, *args, **kwargs):
        return TelethonButton.buy(text, *args, **kwargs)

    @staticmethod
    def game(text, *args, **kwargs):
        return TelethonButton.game(text, *args, **kwargs)

def arrange_buttons(buttons, pattern=(2, 1)):
    rows = []
    index = 0
    pattern_index = 0
    while index < len(buttons):
        row_size = pattern[pattern_index % len(pattern)]
        rows.append(buttons[index:index + row_size])
        index += row_size
        pattern_index += 1
    return rows

def start_keyboard(user_id, is_admin=False):
    flat_buttons = [
        Button.inline("• قسم الأرقام • 📱", data="numbers_menu"),
        Button.inline("• قسم التحكم • ⚙️", data="control_menu"),
        Button.inline("• قسم التنصيب • 💾", data="install_menu"),
        Button.inline("• قسم المزاد • 🏷️", data="auction_menu"),
        Button.inline("• عرض الحسابات • 👤", data="accounts_view"),
        Button.url('•المـطـور•', url='https://t.me/SHAHM41', style="danger", icon=5287528620288397788)
    ]
    
    if user_id == ADMIN_ID or is_admin:
        flat_buttons.append(Button.inline("• لوحة تحكم المطور • 👑", data="admin_panel"))
    
    return arrange_buttons(flat_buttons)

def numbers_menu_keyboard():
    return arrange_buttons([
        Button.inline("• شراء رقم • ✅", data="buy"),
        Button.inline("• بيع حساب • 💰", data="sell"),
        Button.inline("• دعم • 🛠️", data="supper"),
        Button.inline("• سحب الرصيد • 💳", data="ssart"),
        Button.inline("• تحويل الرصيد • 🔄", data="transfer"),
        Button.inline("• القوانين • 📜", data="liscgh"),
        Button.inline("• رجوع • ↩️", data="main")
    ])

def control_menu_keyboard():
    return arrange_buttons([
        Button.inline("• اعدادات السوبر • ⚡️", data="control_settings_super"),
        Button.inline("• اعدادات النشر • 📢", data="control_settings_posting"),
        Button.inline("• اعدادات الانشاء • 👥", data="control_settings_creation"),
        Button.inline("• اعدادات الحساب • 👤", data="control_settings_account"),
        Button.inline("• رجوع • ↩️", data="main")
    ])

def admin_panel_keyboard():
    return arrange_buttons([
        Button.inline("• اعدادات الارقام • ⚙️", data="ajxjao"),
        Button.inline("• الاشتراك الاجباري • 📢", data="ajxkho"),
        Button.inline("• قسم الادمنيه • 👨‍✈️", data="aksgl"),
        Button.inline("• قسم البيع والشراء • ♻️", data="ajkofgl"),
        Button.inline("• قسم الرصيد • 💰", data="ajkcoingl"),
        Button.inline("• قسم الحظر • 🚫", data="bbvjls"),
        Button.inline("• قناة اثباتات التسليم • 📢", data="set_trust_channel"),
        Button.inline("• إذاعة عامة 📢", data="broadcast_message"),
        Button.inline("• تعديل رسالة القوانين • 📜", data="edit_rules"),
        Button.inline("• القناة الإجبارية • 🔒", data="add_mandatory_channel"),
        Button.inline("• تمويل قناة/مجموعة • 💰", data="funding"),
        Button.inline("• قسم المفاتيح • 🔑", data="license_menu"),
        Button.inline("• رفع مميز • ⭐", data="add_vip"),
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
        Button.inline("• رجوع • ↩️", data="admin_panel"),
        Button.inline("• العودة للقائمة الرئيسية • ↩️", data="main")
    ])

def admin_settings_keyboard():
    return arrange_buttons([
        Button.inline("• إضافة ادمن • ➕", data="add_admin"),
        Button.inline("• حذف ادمن • ➖", data="del_admin"),
        Button.inline("• رجوع • ↩️", data="admin_panel"),
        Button.inline("• العودة للقائمة الرئيسية • ↩️", data="main")
    ])

def buy_sell_settings_keyboard():
    return arrange_buttons([
        Button.inline("• تغيير سعر الشراء • 💵", data="change_price"),
        Button.inline("• تغيير سعر البيع • 💰", data="change_sell_price"),
        Button.inline("• رجوع • ↩️", data="admin_panel"),
        Button.inline("• العودة للقائمة الرئيسية • ↩️", data="main")
    ])

def balance_settings_keyboard():
    return arrange_buttons([
        Button.inline("• إضافة رصيد • ➕", data="add_coins"),
        Button.inline("• خصم رصيد • ➖", data="del_coins"),
        Button.inline("• رجوع • ↩️", data="admin_panel"),
        Button.inline("• العودة للقائمة الرئيسية • ↩️", data="main")
    ])

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
        Button.inline("• تفعيل اسم وقتي • ✅", data="timed_name_on"),
        Button.inline("• ايقاف الاسم الوقتي • ❌", data="timed_name_off"),
        Button.inline("• تغيير البايو • 📝", data="change_bio"),
        Button.inline("• رشق تفاعل • ♥️", data="mass_react"),
        Button.inline("• رشق تصويت • 🗳️", data="mass_vote"),
        Button.inline("• رشق تعليق • 💬", data="mass_comment"),
        Button.inline("• زيادة مشاهدات • 👀", data="increment_views"),
        Button.inline("• تمويل قناة/مجموعة • 💰", data="funding"),
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
        Button.inline("• عرض الحسابات المخزنة • 💾", data="view_stored"),
        Button.inline("• عرض الحسابات المشتراة • 🛒", data="view_purchased"),
        Button.inline("• عرض الحسابات المباعة • 💰", data="view_sold"),
        Button.inline("• عرض الحسابات النشطة • 🔥", data="view_active"),
        Button.inline("• رجوع • ↩️", data="main")
    ])

def install_menu_keyboard():
    return arrange_buttons([
        Button.inline("• تنصيب حساب • 📱", data="install_session"),
        Button.inline("• حذف تنصيب • 🗑️", data="delete_install"),
        Button.inline("• رجوع • ↩️", data="main")
    ])

def auction_menu_keyboard():
    return arrange_buttons([
        Button.inline("• اضف مزاد • ➕", data="add_auction"),
        Button.inline("• قائمة المزاد • 📋", data="auction_list"),
        Button.inline("• رجوع • ↩️", data="main")
    ])

def reaction_buttons():
    return arrange_buttons([
        Button.inline("👍", data="react_like"),
        Button.inline("❤️", data="react_love"),
        Button.inline("🔥", data="react_fire"),
        Button.inline("🎉", data="react_celebration"),
        Button.inline("😮", data="react_wow"),
        Button.inline("😢", data="react_sad"),
        Button.inline("😂", data="react_laugh"),
        Button.inline("• رجوع • ↩️", data="control_settings_account")
    ])

def cancel_operation_keyboard():
    return [[Button.inline("• إلغاء العملية • ❌", data="cancel_operation")]]

def back_button():
    return [Button.inline("• رجوع • ↩️", data="main")]
