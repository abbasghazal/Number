"""A compact, deterministic pool of 300 Telegram-safe display names."""
import random


# 100 Arabic and English base names × 3 visual styles = 300 unique names.
BASE_DISPLAY_NAMES = (
    "نور", "ليان", "رُبى", "جود", "سارة", "مريم", "تالا", "رَوان", "آية", "شهد",
    "دانا", "جنى", "ياسمين", "رَند", "لينا", "ريماس", "فرح", "رَهف", "سُهى", "مَلَك",
    "أمير", "آدم", "عمر", "علي", "زيد", "ياسر", "كرار", "سيف", "حسن", "نور الدين",
    "محمد", "أحمد", "يوسف", "إياد", "ريان", "بدر", "رامي", "سامي", "أنس", "هادي",
    "قمر", "ورد", "لؤلؤ", "ندى", "غيم", "شمس", "نجمة", "نسمة", "أثير", "سكينة",
    "Luna", "Mia", "Emma", "Olivia", "Sofia", "Lily", "Zoe", "Nora", "Ivy", "Aria",
    "Noah", "Liam", "Ethan", "Leo", "Mason", "Owen", "Aiden", "Ryan", "Adam", "Elias",
    "Nova", "Sky", "Rose", "Pearl", "Daisy", "Jade", "Amber", "Hazel", "River", "Sunny",
    "Atlas", "Orion", "Phoenix", "Sage", "Kai", "Milo", "Theo", "Ezra", "Ava", "Ella",
    "Violet", "Ruby", "Iris", "Aurora", "Sienna", "Freya", "Lyra", "Elio", "Nico", "Zayn",
)

DISPLAY_NAME_POOL = tuple(
    styled
    for name in BASE_DISPLAY_NAMES
    for styled in (name, f"𓆩 {name} 𓆪", f"✦ {name} ✦")
)

assert len(BASE_DISPLAY_NAMES) == 100
assert len(DISPLAY_NAME_POOL) == 300


def random_display_names(count):
    """Return non-repeating names where possible, then safely reuse the pool."""
    if count <= len(DISPLAY_NAME_POOL):
        return random.sample(DISPLAY_NAME_POOL, count)
    return [random.choice(DISPLAY_NAME_POOL) for _ in range(count)]


BASE_BIOS = (
    "الحمد لله دائماً", "أثر طيب", "كن لطيفاً", "بداية جديدة", "سلام داخلي",
    "أحب التفاصيل", "قلبٌ مطمئن", "أمل لا ينتهي", "طموح بلا حدود", "ابتسم للحياة",
    "كل يوم أجمل", "روح هادئة", "نصنع الفرق", "لنا في الصبر خير", "أحلام كبيرة",
    "بسيط وأنيق", "خير قادم", "الله معي", "أعيش كما أريد", "لا شيء مستحيل",
    "أؤمن بنفسي", "اللطف لغة", "وأفوض أمري لله", "من القلب", "وقت للسلام",
    "خذ الأمور ببساطة", "نجمة في السماء", "أبحث عن الجمال", "فوق الغيم", "حياة واحدة",
    "Keep it simple", "Good vibes only", "Stay kind", "Dream big", "Be yourself",
    "Make it happen", "Choose joy", "Keep moving", "Create your path", "Peace of mind",
    "One day at a time", "Believe and grow", "Live with purpose", "Quietly strong", "Always learning",
    "Less noise, more soul", "Made of dreams", "Here for the journey", "Stay curious", "Be the light",
    "Small steps matter", "Love the moment", "Mindset is everything", "Better every day", "Own your story",
    "Just breathe", "Find your calm", "Beautifully human", "Trust the process", "Hello, world",
    "قهوة ومزاج", "أحب الهدوء", "أنا كما أنا", "موعد مع النجاح", "حديث الروح",
    "لله الحمد", "أمنيات مؤجلة", "أجمل مما تظن", "قريباً أجمل", "كل شيء يمر",
    "أناقة الروح", "نعيش ونبتسم", "ليلي هادئ", "نهاري مشرق", "كلنا أمل",
    "Be real", "Stay golden", "Soul over ego", "Born to shine", "Life is now",
    "Positive mind", "Make memories", "Stay humble", "Chasing sunsets", "Simply happy",
    "Built on hope", "Good energy", "Free spirit", "Just living", "Never settle",
    "Kind heart", "Little moments", "Stay focused", "Love wins", "On my way",
    "Hello sunshine", "No bad days", "Always forward", "Calm soul", "New horizons",
)

BIO_POOL = tuple(
    styled
    for bio in BASE_BIOS
    for styled in (bio, f"✦ {bio} ✦", f"𓂀 {bio} 𓂀")
)

assert len(BASE_BIOS) == 100
assert len(BIO_POOL) == 300


def random_bios(count):
    if count <= len(BIO_POOL):
        return random.sample(BIO_POOL, count)
    return [random.choice(BIO_POOL) for _ in range(count)]
