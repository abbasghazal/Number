import os

# ===== ثوابت API =====
API_ID = 1724716
API_HASH = "00b2d8f59c12c1b9a4bc63b70b461b2f"
ADMIN_ID = 6848908141
TOKEN = "7596646993:AAGDjGeouqWXQF8z1Ot1eD9ske43211ALqE"
bot = None
manual_creation_tasks = {}
auto_creation_tasks = {}
# أسماء المجموعات العشوائية
GROUP_NAMES = [
    "مجموعة الدعم الفني 📞",
    "قروب الأصدقاء 👥",
    "قناة الأخبار 📰",
    "مجتمع المطورين 💻",
    "قروب العائلة 👨‍👩‍👧‍👦",
    "مجموعة الدراسة 📚",
    "قروب العمل 💼",
    "مجموعة السفر ✈️",
    "قناة التكنولوجيا 🔧",
    "مجموعة الألعاب 🎮",
    "قروب المطبخ 🍳",
    "مجتمع الرياضة ⚽",
    "قروب السيارات 🚗",
    "مجموعة الصحة والجمال 💄",
    "قناة التعليم عن بعد 🎓"
]

# مسار قاعدة البيانات
DB_PATH = 'database/KingA.db'

# تهيئة مجلد قاعدة البيانات
if not os.path.exists('database'):
    os.makedirs('database')
