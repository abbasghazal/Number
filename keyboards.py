from telethon import Button
from config import ADMIN_ID

def start_keyboard(user_id, is_admin=False):
    buttons = [
    [Button.inline("• قسم الأرقام • 📱", data="numbers_menu"),
    Button.inline("• قسم التحكم • ⚙️", data="control_menu")],
    [Button.inline("• قسم التنصيب • 💾", data="install_menu")],  # زر جديد
    [Button.inline("• قسم المزاد • 🏷️", data="auction_menu"),    # زر جديد
    Button.inline("• عرض الحسابات • 👤", data="accounts_view")],
    [Button.url('•المـطـور•',url='https://t.me/O_P_G')]
]
    
    if user_id == ADMIN_ID or is_admin:
        buttons.append([Button.inline("• لوحة تحكم المطور • 👑", data="admin_panel")])
    
    return buttons

def numbers_menu_keyboard():
    return [
        #[Button.inline("• بحث عن رقم • 🔍", data="search_number")],
        [Button.inline("• شراء رقم • ✅", data="buy"), Button.inline("• بيع رقم • 💰", data="sell")],
        [Button.inline("• دعم • 🛠️", data="supper")],
        [Button.inline("• سحب الرصيد • 💳", data="ssart"),
        Button.inline("• تحويل الرصيد • 🔄", data="transfer")],
        [Button.inline("• القوانين • 📜", data="liscgh")],
        [Button.inline("• رجوع • ↩️", data="main")]
    ]

def control_menu_keyboard():
    return [
        [Button.inline("• اعدادات السوبر • ⚡️", data="control_settings_super"),
        Button.inline("• اعدادات النشر • 📢", data="control_settings_posting")],
        [Button.inline("• اعدادات الانشاء • 👥", data="control_settings_creation"),
        Button.inline("• اعدادات الحساب • 👤", data="control_settings_account")],
        [Button.inline("• رجوع • ↩️", data="main")]
    ]

def admin_panel_keyboard():
    return [
        [Button.inline("• اعدادات الارقام • ⚙️", data="ajxjao")],
        [Button.inline("• الاشتراك الاجباري • 📢", data="ajxkho"), Button.inline("• قسم الادمنيه • 👨‍✈️", data="aksgl")],
        [Button.inline("• قسم البيع والشراء • ♻️", data="ajkofgl")],
        [Button.inline("• قسم الرصيد • 💰", data="ajkcoingl"), Button.inline("• قسم الحظر • 🚫", data="bbvjls")],
        [Button.inline("• قناة اثباتات التسليم • 📢", data="set_trust_channel"),
        Button.inline("• إذاعة عامة 📢", data="broadcast_message")],
        [Button.inline("• تعديل رسالة القوانين • 📜", data="edit_rules")],
        [Button.inline("• القناة الإجبارية • 🔒", data="add_mandatory_channel")],
        [Button.inline("• تمويل قناة/مجموعة • 💰", data="funding")],
        [Button.inline("• رفع مميز • ⭐", data="add_vip")],
        [Button.inline("• العودة للقائمة الرئيسية • ↩️", data="main")]
    ]

def num_settings_keyboard():
    return [
        [Button.inline("• عدد ارقام البوت • 🔢", data="all_of_number")],
        [Button.inline("• إضافة دولة • 🌍", data="add_country"), Button.inline("• حذف دولة • 🗑️", data="del_country")],
        [Button.inline("• إضافة رقم • ➕", data="add"), Button.inline("• حذف رقم • ➖", data="del_account")],
        [Button.inline("• رجوع • ↩️", data="admin_panel"), Button.inline("• العودة للقائمة الرئيسية • ↩️", data="main")]
    ]

def force_settings_keyboard():
    return [
        [Button.inline("• إضافة قناة • ➕", data="add_force"), Button.inline("• حذف قناة • ➖", data="del_force")],
        [Button.inline("• رجوع • ↩️", data="admin_panel"), Button.inline("• العودة للقائمة الرئيسية • ↩️", data="main")]
    ]

def admin_settings_keyboard():
    return [
        [Button.inline("• إضافة ادمن • ➕", data="add_admin"), Button.inline("• حذف ادمن • ➖", data="del_admin")],
        [Button.inline("• رجوع • ↩️", data="admin_panel"), Button.inline("• العودة للقائمة الرئيسية • ↩️", data="main")]
    ]

def buy_sell_settings_keyboard():
    return [
        [Button.inline("• تغيير سعر الشراء • 💵", data="change_price"), Button.inline("• تغيير سعر البيع • 💰", data="change_sell_price")],
        [Button.inline("• رجوع • ↩️", data="admin_panel"), Button.inline("• العودة للقائمة الرئيسية • ↩️", data="main")]
    ]

def balance_settings_keyboard():
    return [
        [Button.inline("• إضافة رصيد • ➕", data="add_coins"), Button.inline("• خصم رصيد • ➖", data="del_coins")],
        [Button.inline("• رجوع • ↩️", data="admin_panel"), Button.inline("• العودة للقائمة الرئيسية • ↩️", data="main")]
    ]

def ban_settings_keyboard():
    return [
        [Button.inline("• حظر عضو • ⛔", data="ban"), Button.inline("• رفع حظر • ✅", data="unban")],
        [Button.inline("• رجوع • ↩️", data="admin_panel"), Button.inline("• العودة للقائمة الرئيسية • ↩️", data="main")]
    ]

def super_settings_keyboard():
    return [
        [Button.inline("• إضافة سوبر • ➕", data="add_super"),
        Button.inline("• حذف سوبر • ➖", data="del_super")],
        [Button.inline("• عرض السوبرات المضافه • 👁️", data="show_supers"),
        Button.inline("• حذف السوبرات المضافه • 🗑️", data="clear_supers")],
        [Button.inline("• رجوع • ↩️", data="control_menu")]
    ]

def posting_settings_keyboard():
    return [
        [Button.inline("• إضافة كليشة نشر • ➕", data="add_template"),
        Button.inline("• حذف كليشة نشر • ➖", data="del_template")],
        [Button.inline("• عرض كلايش المضافة • 👁️", data="show_templates"),
        Button.inline("• حذف الكلايش المضافة • 🗑️", data="clear_templates")],
        [Button.inline("• تعديل كليشة مضافة • ✏️", data="edit_template")],
        [Button.inline("• تفعيل النشر المتعدد • ✅", data="enable_multi_posting"),
        Button.inline("• إيقاف النشر المتعدد • ❌", data="disable_multi_posting")],
        [Button.inline("• بدء النشر • 🚀", data="start_posting"),
        Button.inline("• إيقاف النشر في مجموعة معينه • ⏸️", data="stop_posting_group")],
        [Button.inline("• إيقاف النشر في جميع السوبرات • ■", data="stop_all_posting")],
        [Button.inline("• رجوع • ↩️", data="control_menu")]
    ]

def creation_settings_keyboard():
    return [
        [Button.inline("• انشاء مجموعات يدوي • 🛠️", data="manual_creation"),
        Button.inline("• ايقاف انشاء مجموعات يدوي • ⏹️", data="stop_manual_creation")],
        [Button.inline("• انشاء مجموعات تلقائي • 🤖", data="auto_creation"),
        Button.inline("• ايقاف انشاء مجموعات تلقائي • ⏹️", data="stop_auto_creation")],
        [Button.inline("• رجوع • ↩️", data="control_menu")]
    ]

def account_settings_keyboard():
    return [
        [Button.inline("• تفعيل اسم وقتي • ✅", data="timed_name_on"),
        Button.inline("• ايقاف الاسم الوقتي • ❌", data="timed_name_off")],
        [Button.inline("• تغيير البايو • 📝", data="change_bio")],
        [Button.inline("• تفعيل الوضع الخفي • 👁️", data="enable_stealth_mode"),
        Button.inline("• إيقاف الوضع الخفي • 🕵️", data="disable_stealth_mode")],
        [Button.inline("• تغيير اسم المستخدم • 👤", data="change_username")],
        [Button.inline("• تفعيل التنبيهات • 🔔", data="enable_notifications"),
        Button.inline("• إيقاف التنبيهات • 🔕", data="disable_notifications")],
        [Button.inline("• رجوع • ↩️", data="control_menu")]
    ]

def accounts_view_keyboard():
    return [
        [Button.inline("• عرض الحسابات المخزنة • 💾", data="view_stored"),
        Button.inline("• عرض الحسابات المشتراة • 🛒", data="view_purchased")],
        [Button.inline("• عرض الحسابات المباعة • 💰", data="view_sold"),
        Button.inline("• عرض الحسابات النشطة • 🔥", data="view_active")],
        [Button.inline("• رجوع • ↩️", data="main")]
    ]

# قسم التنصيب
def install_menu_keyboard():
    return [
        [Button.inline("• تنصيب حساب • 📱", data="install_session")],
        [Button.inline("• حذف تنصيب • 🗑️", data="delete_install")],
        [Button.inline("• رجوع • ↩️", data="main")]
    ]

# قسم المزاد
def auction_menu_keyboard():
    return [
        [Button.inline("• اضف مزاد • ➕", data="add_auction")],
        [Button.inline("• قائمة المزاد • 📋", data="auction_list")],
        [Button.inline("• رجوع • ↩️", data="main")]
    ]
    
def cancel_operation_keyboard():
    return [[Button.inline("• إلغاء العملية • ❌", data="cancel_operation")]]

def back_button():
    return [Button.inline("• رجوع • ↩️", data="main")]