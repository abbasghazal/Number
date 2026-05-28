import os

def _load_env_file(path=".env"):
    """Load simple KEY=VALUE pairs without requiring python-dotenv."""
    if not os.path.exists(path):
        return

    with open(path, "r", encoding="utf-8") as env_file:
        for line in env_file:
            line = line.strip()
            if not line or line.startswith("#") or "=" not in line:
                continue
            key, value = line.split("=", 1)
            os.environ.setdefault(key.strip(), value.strip().strip('"').strip("'"))


try:
    from dotenv import load_dotenv
    load_dotenv()
except ImportError:
    _load_env_file()


def _get_int_env(name, default=0):
    value = os.getenv(name)
    if value in (None, ""):
        return default
    try:
        return int(value)
    except ValueError:
        return default


# ===== ثوابت API =====
API_ID = _get_int_env("API_ID")
API_HASH = os.getenv("API_HASH", "")
ADMIN_ID = _get_int_env("ADMIN_ID")
TOKEN = os.getenv("BOT_TOKEN", "")
SESSION_ENCRYPTION_KEY = os.getenv("SESSION_ENCRYPTION_KEY", "")
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
DB_PATH = os.getenv("DB_PATH", "database/KingA.db")

def validate_config():
    missing = [
        name for name, value in {
            "API_ID": API_ID,
            "API_HASH": API_HASH,
            "ADMIN_ID": ADMIN_ID,
            "BOT_TOKEN": TOKEN,
            "SESSION_ENCRYPTION_KEY": SESSION_ENCRYPTION_KEY,
        }.items()
        if not value
    ]
    if missing:
        raise RuntimeError(
            "Missing required environment variables: " + ", ".join(missing)
        )
    try:
        from cryptography.fernet import Fernet
        Fernet(SESSION_ENCRYPTION_KEY.encode())
    except Exception as exc:
        raise RuntimeError(
            "SESSION_ENCRYPTION_KEY is invalid. Generate it with: "
            "python3 -c \"from cryptography.fernet import Fernet; print(Fernet.generate_key().decode())\""
        ) from exc

# تهيئة مجلد قاعدة البيانات
db_dir = os.path.dirname(DB_PATH) or "."
if not os.path.exists(db_dir):
    os.makedirs(db_dir)
