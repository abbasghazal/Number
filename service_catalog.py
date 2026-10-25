"""One server-owned catalog exposing every bot menu and its permitted tools."""

from ui_config import DEFAULT_ITEMS

# key, callback, Arabic, English, group, permission, native page
SPECS = [
    ("main", "main", "القائمة الكاملة", "All bot services", "general", None, None),
    ("numbers", "numbers_menu", "الأرقام والبيع", "Numbers and sales", "accounts", None, None),
    ("store", "feature:shop:list:0", "المتجر والمفضلة", "Marketplace and favorites", "accounts", None, "store"),
    ("rentals", "rent_numbers", "تأجير الأرقام", "Rent numbers", "accounts", None, None),
    ("rental_history", "ops:rent:list:0", "إيجاراتي والتجديد", "Rentals and renewals", "accounts", None, None),
    ("deferred", "deferred:list:0", "حجوزات الشراء", "Deferred purchases", "accounts", None, None),
    ("stars", "numbers_menu", "شراء بالنجوم", "Buy with Telegram Stars", "accounts", None, None),
    ("sales", "num:mysales:0", "طلبات بيع أرقامي", "My number sale requests", "accounts", None, None),
    ("accounts", "accounts_view", "أدوات حساباتي", "My account tools", "accounts", None, None),
    (
        "account_inventory",
        "ops:accounts:list:0",
        "تنظيم وفرز الحسابات",
        "Organize and filter accounts",
        "accounts",
        None,
        None,
    ),
    (
        "account_dashboard",
        "ops:accounts:dashboard:0",
        "إحصائيات الحسابات",
        "Account statistics",
        "accounts",
        None,
        None,
    ),
    ("account_health", "health:page:0", "صحة الحسابات والفحص", "Account health checks", "accounts", None, None),
    ("bulk", "ops:bulk:list:0", "العمليات الجماعية", "Bulk operations", "accounts", None, None),
    (
        "account_management",
        "acct:accounts:0",
        "الحسابات المصرح بإدارتها",
        "Authorized account management",
        "accounts",
        "manager",
        None,
    ),
    ("account_grants", "acct:mine:0", "تفويض إدارة حساباتي", "Account management grants", "accounts", None, None),
    ("twofa", "ops:twofa:list:0", "إدارة التحقق بخطوتين", "Two-step verification", "accounts", None, None),
    ("import", "install_menu", "تنصيب واستخراج الجلسات", "Import and export sessions", "accounts", None, None),
    ("settings", "control_settings_account", "إعدادات الحساب", "Account settings", "accounts", None, None),
    ("tools", "control_menu", "التحكم وأدوات الحسابات", "Account utilities", "accounts", None, None),
    ("posting", "control_settings_posting", "النشر والقوالب", "Posting and templates", "automation", None, None),
    ("automation", "auto:menu", "الأتمتة والردود", "Automation and replies", "automation", None, None),
    ("schedules", "feature:schedule:list:0", "المهام المجدولة", "Scheduled operations", "automation", None, None),
    ("jobs", "ops:jobs:list:0", "مركز المهام والنتائج", "Tasks and results", "automation", None, "tasks"),
    ("api", "api:menu", "قسم API", "External API", "general", None, None),
    ("engagement", "engagement_menu", "جميع خدمات الرشق", "All engagement services", "engagement", None, None),
    ("search", "search_join", "الرشق عبر البحث", "Search and join", "engagement", None, None),
    ("promotions", "promo:menu", "الرشق المدفوع", "Paid promotions", "engagement", None, "promotions"),
    ("funding", "fund:menu", "التمويل وجمع النقاط", "Point funding and rewards", "engagement", None, "funding"),
    ("funding_tasks", "fund:collect", "مهام جمع النقاط", "Earn membership points", "engagement", None, None),
    (
        "shahm",
        "shahm:menu",
        "شهم: التجميع والتحويل والهدية",
        "Shahm collection, transfers and gifts",
        "engagement",
        None,
        None,
    ),
    ("auctions", "auction_menu", "المزادات", "Auctions", "commerce", None, None),
    (
        "wallet",
        "balance_menu",
        "الدفع والتحويل والسحب",
        "Payments, transfers and withdrawals",
        "commerce",
        None,
        "wallet",
    ),
    ("topup", "topup", "شحن الرصيد", "Top up wallet", "commerce", None, None),
    ("statement", "stmt:l:all:0:0", "كشف الحساب والتفاصيل", "Financial statement and details", "commerce", None, None),
    ("loyalty", "feature:loyalty:view:0", "الكوبونات والولاء", "Coupons and loyalty", "commerce", None, None),
    ("licenses", "license_status_user", "التراخيص والتفعيل", "Licenses and activation", "general", None, None),
    ("support", "supper", "التواصل مع الدعم", "Contact support", "general", None, None),
    ("tickets", "feature:tickets:list:0", "تذاكري ومتابعتها", "My support tickets", "general", None, "support"),
    (
        "notifications",
        "ops:notifications:list:0",
        "الإشعارات وإعداداتها",
        "Notifications and preferences",
        "general",
        None,
        None,
    ),
    ("activity", "my_activity", "نشاطي", "My activity", "general", None, None),
    ("referrals", "referral_menu", "الإحالات والمكافآت", "Referrals and rewards", "general", None, None),
    ("achievements", "achievements", "الإنجازات", "Achievements", "general", None, None),
    ("rules", "liscgh", "القوانين", "Rules", "general", None, None),
    ("premium", "premium_join", "الانضمام للمميز", "Premium membership", "general", None, None),
    ("contact", "developer_menu", "التواصل مع المطور", "Contact developer", "general", None, None),
    ("admin", "admin_panel", "لوحة الإدارة", "Administration", "admin", "staff", None),
    ("developer", "developer_menu", "لوحة المطور", "Developer tools", "admin", "developer", None),
    ("codes", "codes:menu", "مراقبة البوتات", "Bot monitoring", "automation", None, None),
    (
        "promotion_admin",
        "promo:admin",
        "إدارة الرشق والنقاط",
        "Promotion and point settings",
        "admin",
        "developer",
        None,
    ),
    ("admin_inventory", "ajxjao", "إدارة مخزون الأرقام", "Inventory administration", "admin", "inventory", None),
    ("admin_pricing", "ajkofgl", "أسعار البيع والشراء", "Purchase and sale pricing", "admin", "finance", None),
    ("admin_wallet", "ajkcoingl", "إدارة الأرصدة والعمولات", "Wallet administration", "admin", "finance", None),
    ("admin_payments", "payment_settings", "إعدادات الدفع", "Payment settings", "admin", "finance", None),
    ("admin_withdrawals", "ops:withdraw:list:0", "طلبات السحب", "Withdrawal requests", "admin", "finance", None),
    ("admin_reports", "feature:reports:view:0", "التقارير المالية", "Financial reports", "admin", "finance", None),
    ("admin_coupons", "feature:coupons:admin:0", "إدارة الكوبونات", "Coupon administration", "admin", "finance", None),
    (
        "admin_tickets",
        "feature:tickets:staff:0",
        "الدعم والرد على التذاكر",
        "Staff support tickets",
        "admin",
        "support",
        None,
    ),
    ("admin_bans", "bbvjls", "إدارة الحظر", "Ban management", "admin", "support", None),
    ("admin_staff", "feature:staff:list:0", "المشرفون والصلاحيات", "Staff and roles", "admin", "owner", None),
    ("admin_service", "ops:service:list:0", "حالة الخدمة", "Service health", "admin", "operations", None),
    ("admin_backups", "ops:backup:list:0", "النسخ والاستعادة", "Backups and restore", "admin", "operations", None),
    ("admin_licenses", "license_menu", "إدارة التراخيص", "License administration", "admin", "manage", None),
    ("admin_channels", "ajxkho", "قنوات الاشتراك الإجباري", "Mandatory channels", "admin", "manage", None),
    ("admin_engagement", "auto_engagement_menu", "مراقبة التفاعل", "Engagement monitoring", "admin", "manage", None),
    ("admin_settings", "aksgl", "إعدادات الإدارة", "Administration settings", "admin", "manage", None),
]

# Main-menu additions are exposed automatically, rather than silently omitted.
known = {r[1] for r in SPECS}
for index, (payload, label) in enumerate(DEFAULT_ITEMS):
    if payload not in known:
        SPECS.append((f"bot_menu_{index}", payload, label, label, "general", None, None))
ROOTS = {r[0]: r[1] for r in SPECS}
BY_KEY = {r[0]: r for r in SPECS}


async def permitted(user, permission):
    from config import ADMIN_ID, DEVELOPER_ID
    import account_access
    import staff_security

    if permission is None:
        return True
    if permission == "developer":
        return bool(DEVELOPER_ID and user == DEVELOPER_ID)
    if permission == "owner":
        return bool(ADMIN_ID and user == ADMIN_ID)
    if permission == "manager":
        return account_access.is_manager(user)
    if permission == "staff":
        return await staff_security.is_staff(user)
    return await staff_security.has_permission(user, permission)


async def available(user):
    permissions = {r[5] for r in SPECS}
    allowed = {permission: await permitted(user, permission) for permission in permissions}
    return [
        dict(key=k, title_ar=ar, title_en=en, group=group, native=native)
        for k, _, ar, en, group, permission, native in SPECS
        if allowed[permission]
    ]


async def require(user, key):
    row = BY_KEY.get(key)
    if not row or not await permitted(user, row[5]):
        raise PermissionError("لا تملك صلاحية فتح هذه الخدمة.")
