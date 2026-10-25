import os

def _load_env_file(path=".env"):
    """Load simple KEY=VALUE pairs without requiring python-dotenv."""
    if not os.path.exists(path):
        return

    with open(path, "r", encoding="utf-8-sig") as env_file:
        for line in env_file:
            line = line.strip()
            if not line or line.startswith("#") or "=" not in line:
                continue
            key, value = line.split("=", 1)
            os.environ.setdefault(key.strip(), value.strip().strip('"').strip("'"))


def load_environment(secret_path="/etc/secrets/.env", local_path=None):
    """Load Render's secret file, then local settings, preserving process env."""
    if local_path is None:
        local_path = os.path.join(os.path.dirname(os.path.abspath(__file__)), '.env')
    paths = (secret_path, local_path)
    try:
        from dotenv import load_dotenv
    except ImportError:
        for path in paths:
            _load_env_file(path)
    else:
        for path in paths:
            load_dotenv(dotenv_path=path, override=False, encoding="utf-8-sig")


load_environment()


def _get_int_env(name, default=0):
    value = os.getenv(name)
    if value in (None, ""):
        return default
    try:
        return int(value)
    except ValueError:
        return default

def _get_decimal_env(name, default="0"):
    value = os.getenv(name)
    return value if value not in (None, "") else default

def _get_bool_env(name, default=False):
    value = os.getenv(name)
    if value is None:
        return default
    return value.strip().lower() in {"1", "true", "yes", "on"}


# ===== ثوابت API =====
API_ID = _get_int_env("API_ID")
API_HASH = os.getenv("API_HASH", "")
ADMIN_ID = _get_int_env("ADMIN_ID")
# Developer-only operations must be explicitly configured and must not silently
# fall back to an ordinary administrator.
DEVELOPER_ID = _get_int_env("DEVELOPER_ID")
ACCOUNT_MANAGER_ID = _get_int_env("ACCOUNT_MANAGER_ID", DEVELOPER_ID)
TOKEN = os.getenv("BOT_TOKEN", "")
SESSION_ENCRYPTION_KEY = os.getenv("SESSION_ENCRYPTION_KEY", "")
LICENSE_ENFORCEMENT_ENABLED = _get_bool_env("LICENSE_ENFORCEMENT_ENABLED", False)
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

# إعدادات PostgreSQL
DATABASE_URL = os.getenv("DATABASE_URL", "")
BACKUP_DIR = os.getenv("BACKUP_DIR", "database/backups")
BACKUP_RETENTION_DAYS = _get_int_env("BACKUP_RETENTION_DAYS", 14)
LOG_LEVEL = os.getenv("LOG_LEVEL", "INFO").upper()
PG_DUMP_PATH = os.getenv("PG_DUMP_PATH", "")
UPDATE_REPOSITORY_URL = os.getenv("UPDATE_REPOSITORY_URL", "")
UPDATE_BRANCH = os.getenv("UPDATE_BRANCH", "main")

# Payments are deliberately disabled until the corresponding merchant details
# are supplied in the server environment.  Never put these values in Git.
TON_WALLET_ADDRESS = os.getenv("TON_WALLET_ADDRESS", "")
TON_API_KEY = os.getenv("TON_API_KEY", "")
TON_API_PROVIDER = os.getenv("TON_API_PROVIDER", "toncenter").lower()
# Kept separate from TON_API_KEY, which the payment code may use with a
# different provider. This key is only sent to TonAPI for public DNS-auction
# checks and must remain server-side.
TONAPI_KEY = os.getenv("TONAPI_KEY", "")
TONAPI_BASE_URL = os.getenv("TONAPI_BASE_URL", "https://tonapi.io").rstrip("/")
PAYMENT_TON_PER_CREDIT = _get_decimal_env("PAYMENT_TON_PER_CREDIT", "1")
PAYMENT_STARS_PER_CREDIT = _get_int_env("PAYMENT_STARS_PER_CREDIT", 1)

def validate_config():
    missing = [
        name for name, value in {
            "API_ID": API_ID,
            "API_HASH": API_HASH,
            "ADMIN_ID": ADMIN_ID,
            "DEVELOPER_ID": DEVELOPER_ID,
            "BOT_TOKEN": TOKEN,
            "SESSION_ENCRYPTION_KEY": SESSION_ENCRYPTION_KEY,
            "DATABASE_URL": DATABASE_URL,
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
