import sqlite3
from config import DB_PATH

def _enable_foreign_keys(conn):
    conn.execute("PRAGMA foreign_keys = ON")

def _table_columns(cursor, table_name):
    cursor.execute(f"PRAGMA table_info({table_name})")
    return {row[1] for row in cursor.fetchall()}

def _add_column_if_missing(cursor, table_name, column_name, column_definition):
    if column_name not in _table_columns(cursor, table_name):
        cursor.execute(f"ALTER TABLE {table_name} ADD COLUMN {column_name} {column_definition}")

def init_db():
    conn = sqlite3.connect(DB_PATH)
    _enable_foreign_keys(conn)
    c = conn.cursor()
    
    # إنشاء جدول المستخدمين
    c.execute('''CREATE TABLE IF NOT EXISTS users (
                user_id INTEGER PRIMARY KEY,
                username TEXT,
                first_name TEXT,
                last_name TEXT,
                coins REAL DEFAULT 0,
                join_date DATETIME DEFAULT CURRENT_TIMESTAMP,
                last_active DATETIME DEFAULT CURRENT_TIMESTAMP
                )''')
    
    # إنشاء جدول الدول (مع تعديل الأسعار)
    c.execute('''CREATE TABLE IF NOT EXISTS countries (
                calling_code TEXT PRIMARY KEY,
                name TEXT,
                price REAL,
                sell_price REAL,
                is_active BOOLEAN DEFAULT 1
                )''')
    
    # إنشاء بقية الجداول (بدون تعديل)
    c.execute('''CREATE TABLE IF NOT EXISTS accounts (
                phone TEXT PRIMARY KEY,
                session TEXT,
                calling_code TEXT,
                twofa TEXT DEFAULT 'لا يوجد',
                seller_id INTEGER,
                added_date DATETIME DEFAULT CURRENT_TIMESTAMP,
                is_sold BOOLEAN DEFAULT 0,
                is_active BOOLEAN DEFAULT 1
                )''')
    
    c.execute('''CREATE TABLE IF NOT EXISTS bad_guys (
                user_id INTEGER PRIMARY KEY
                )''')
    
    c.execute('''CREATE TABLE IF NOT EXISTS force_channels (
                channel_id TEXT PRIMARY KEY,
                channel_name TEXT,
                invite_link TEXT
                )''')
    
    c.execute('''CREATE TABLE IF NOT EXISTS admins (
                user_id INTEGER PRIMARY KEY,
                admin_level INTEGER DEFAULT 1,
                added_by INTEGER,
                added_date DATETIME DEFAULT CURRENT_TIMESTAMP
                )''')
    
    c.execute('''CREATE TABLE IF NOT EXISTS settings (
                key TEXT PRIMARY KEY,
                value TEXT,
                description TEXT,
                last_updated DATETIME DEFAULT CURRENT_TIMESTAMP
                )''')
    
    c.execute('''CREATE TABLE IF NOT EXISTS pending_purchases (
                purchase_id INTEGER PRIMARY KEY AUTOINCREMENT,
                user_id INTEGER,
                phone TEXT,
                calling_code TEXT,
                name TEXT,
                price REAL,
                session TEXT,
                twofa TEXT,
                processing BOOLEAN DEFAULT 0,
                status TEXT DEFAULT 'pending',
                timestamp DATETIME DEFAULT CURRENT_TIMESTAMP,
                FOREIGN KEY (user_id) REFERENCES users(user_id)
                )''')
    
    c.execute('''CREATE TABLE IF NOT EXISTS stored_accounts (
                phone TEXT PRIMARY KEY,
                session TEXT,
                user_id INTEGER,
                twofa TEXT DEFAULT 'لا يوجد',
                storage_date DATETIME DEFAULT CURRENT_TIMESTAMP,
                FOREIGN KEY (user_id) REFERENCES users(user_id)
                )''')
    
    c.execute('''CREATE TABLE IF NOT EXISTS super_channels (
                channel_id TEXT PRIMARY KEY,
                title TEXT,
                invite_link TEXT,
                is_active BOOLEAN DEFAULT 1
                )''')
    
    c.execute('''CREATE TABLE IF NOT EXISTS posting_templates (
                template_id INTEGER PRIMARY KEY AUTOINCREMENT,
                template_text TEXT,
                created_by INTEGER,
                created_at DATETIME DEFAULT CURRENT_TIMESTAMP,
                is_active BOOLEAN DEFAULT 1,
                FOREIGN KEY (created_by) REFERENCES users(user_id)
                )''')
    
    c.execute('''CREATE TABLE IF NOT EXISTS posting_settings (
                key TEXT PRIMARY KEY,
                value TEXT,
                description TEXT
                )''')
    
    c.execute('''CREATE TABLE IF NOT EXISTS timed_names (
                user_id INTEGER PRIMARY KEY,
                active BOOLEAN DEFAULT 0,
                name_type TEXT,
                start_time DATETIME,
                end_time DATETIME,
                FOREIGN KEY (user_id) REFERENCES users(user_id)
                )''')
    
    c.execute('''CREATE TABLE IF NOT EXISTS auto_creation_tasks (
                task_id INTEGER PRIMARY KEY AUTOINCREMENT,
                user_id INTEGER,
                seconds_interval INTEGER,
                hours_duration INTEGER,
                remaining_runs INTEGER DEFAULT 10,
                active BOOLEAN DEFAULT 1,
                start_time DATETIME DEFAULT CURRENT_TIMESTAMP,
                last_run DATETIME,
                FOREIGN KEY (user_id) REFERENCES users(user_id)
                )''')
    
    c.execute('''CREATE TABLE IF NOT EXISTS pending_sales (
                phone TEXT PRIMARY KEY,
                seller_id INTEGER,
                price REAL,
                calling_code TEXT,
                session TEXT,
                twofa TEXT,
                added_date DATETIME DEFAULT CURRENT_TIMESTAMP,
                FOREIGN KEY (seller_id) REFERENCES users(user_id)
                )''')
    
    c.execute('''CREATE TABLE IF NOT EXISTS sold_accounts (
                phone TEXT PRIMARY KEY,
                session TEXT,
                user_id INTEGER,
                seller_id INTEGER,
                price REAL,
                sold_at DATETIME DEFAULT CURRENT_TIMESTAMP,
                FOREIGN KEY (user_id) REFERENCES users(user_id),
                FOREIGN KEY (seller_id) REFERENCES users(user_id)
                )''')
    
    c.execute('''CREATE TABLE IF NOT EXISTS purchased_accounts (
                phone TEXT PRIMARY KEY,
                session TEXT,
                user_id INTEGER,
                seller_id INTEGER,
                calling_code TEXT,
                name TEXT,
                price REAL,
                twofa TEXT DEFAULT 'لا يوجد',
                purchased_at DATETIME DEFAULT CURRENT_TIMESTAMP,
                FOREIGN KEY (user_id) REFERENCES users(user_id),
                FOREIGN KEY (seller_id) REFERENCES users(user_id)
                )''')
    
    c.execute('''CREATE TABLE IF NOT EXISTS active_accounts (
                phone TEXT PRIMARY KEY,
                session TEXT,
                user_id INTEGER,
                activity TEXT,
                started_at DATETIME DEFAULT CURRENT_TIMESTAMP,
                last_activity DATETIME,
                FOREIGN KEY (user_id) REFERENCES users(user_id)
                )''')
    
    c.execute('''CREATE TABLE IF NOT EXISTS active_posting_tasks (
                task_id INTEGER PRIMARY KEY AUTOINCREMENT,
                user_id INTEGER,
                phone TEXT,
                channel_id TEXT,
                all_channels BOOLEAN DEFAULT 0,
                seconds_interval INTEGER,
                repetitions INTEGER,
                remaining INTEGER,
                active BOOLEAN DEFAULT 1,
                last_post DATETIME,
                next_post DATETIME,
                FOREIGN KEY (user_id) REFERENCES users(user_id)
                )''')
    
    c.execute('''CREATE TABLE IF NOT EXISTS vip_users (
                user_id INTEGER PRIMARY KEY,
                vip_level INTEGER DEFAULT 1,
                start_date DATETIME DEFAULT CURRENT_TIMESTAMP,
                end_date DATETIME,
                added_by INTEGER,
                FOREIGN KEY (user_id) REFERENCES users(user_id),
                FOREIGN KEY (added_by) REFERENCES users(user_id)
                )''')

    # إنشاء جدول المزادات
    c.execute('''CREATE TABLE IF NOT EXISTS auctions (
            auction_id INTEGER PRIMARY KEY AUTOINCREMENT,
            phone TEXT UNIQUE,
            session TEXT,
            seller_id INTEGER,
            min_price REAL,
            current_bid REAL DEFAULT 0,
            current_bidder INTEGER,
            status TEXT DEFAULT 'active',
            start_time DATETIME DEFAULT CURRENT_TIMESTAMP,
            end_time DATETIME,
            twofa TEXT DEFAULT 'لا يوجد',
            FOREIGN KEY (seller_id) REFERENCES users(user_id),
            FOREIGN KEY (current_bidder) REFERENCES users(user_id)
            )''')

    # إنشاء جدول العروض
    c.execute('''CREATE TABLE IF NOT EXISTS auction_bids (
            bid_id INTEGER PRIMARY KEY AUTOINCREMENT,
            auction_id INTEGER,
            user_id INTEGER,
            bid_amount REAL,
            bid_time DATETIME DEFAULT CURRENT_TIMESTAMP,
            FOREIGN KEY (auction_id) REFERENCES auctions(auction_id),
            FOREIGN KEY (user_id) REFERENCES users(user_id)
            )''')

    c.execute('''CREATE TABLE IF NOT EXISTS licenses (
            license_key TEXT PRIMARY KEY,
            duration_days INTEGER NOT NULL,
            created_by INTEGER,
            created_at DATETIME DEFAULT CURRENT_TIMESTAMP,
            used_by INTEGER,
            used_at DATETIME,
            expires_at DATETIME,
            is_active BOOLEAN DEFAULT 1,
            is_revoked BOOLEAN DEFAULT 0
            )''')

    c.execute('''CREATE TABLE IF NOT EXISTS user_licenses (
            user_id INTEGER PRIMARY KEY,
            license_key TEXT,
            activated_at DATETIME DEFAULT CURRENT_TIMESTAMP,
            expires_at DATETIME,
            is_valid BOOLEAN DEFAULT 1,
            reminded_3d BOOLEAN DEFAULT 0,
            reminded_1d BOOLEAN DEFAULT 0,
            expired_notified BOOLEAN DEFAULT 0,
            FOREIGN KEY (license_key) REFERENCES licenses(license_key)
            )''')

    # ترحيلات قواعد البيانات الموجودة
    _add_column_if_missing(c, "pending_purchases", "processing", "BOOLEAN DEFAULT 0")
    _add_column_if_missing(c, "purchased_accounts", "calling_code", "TEXT")
    _add_column_if_missing(c, "purchased_accounts", "name", "TEXT")
    _add_column_if_missing(c, "purchased_accounts", "seller_id", "INTEGER")
    _add_column_if_missing(c, "purchased_accounts", "calling_code", "TEXT")
    _add_column_if_missing(c, "purchased_accounts", "twofa", "TEXT DEFAULT 'لا يوجد'")
    _add_column_if_missing(c, "stored_accounts", "twofa", "TEXT DEFAULT 'لا يوجد'")
    _add_column_if_missing(c, "auctions", "twofa", "TEXT DEFAULT 'لا يوجد'")
    _add_column_if_missing(c, "sold_accounts", "calling_code", "TEXT")
    _add_column_if_missing(c, "sold_accounts", "name", "TEXT")
    _add_column_if_missing(c, "active_posting_tasks", "phone", "TEXT")
    _add_column_if_missing(c, "active_posting_tasks", "channel_id", "TEXT")
    _add_column_if_missing(c, "active_posting_tasks", "all_channels", "BOOLEAN DEFAULT 0")
    _add_column_if_missing(c, "user_licenses", "reminded_3d", "BOOLEAN DEFAULT 0")
    _add_column_if_missing(c, "user_licenses", "reminded_1d", "BOOLEAN DEFAULT 0")
    _add_column_if_missing(c, "user_licenses", "expired_notified", "BOOLEAN DEFAULT 0")

    # فهارس الأداء
    c.execute("CREATE INDEX IF NOT EXISTS idx_users_user_id ON users(user_id)")
    c.execute("CREATE INDEX IF NOT EXISTS idx_accounts_phone ON accounts(phone)")
    c.execute("CREATE INDEX IF NOT EXISTS idx_stored_accounts_user ON stored_accounts(user_id)")
    c.execute("CREATE INDEX IF NOT EXISTS idx_stored_accounts_phone ON stored_accounts(phone)")
    c.execute("CREATE INDEX IF NOT EXISTS idx_purchased_accounts_user ON purchased_accounts(user_id)")
    c.execute("CREATE INDEX IF NOT EXISTS idx_sold_accounts_user ON sold_accounts(user_id)")
    c.execute("CREATE INDEX IF NOT EXISTS idx_auctions_status ON auctions(status)")
    c.execute("CREATE INDEX IF NOT EXISTS idx_countries_calling_code ON countries(calling_code)")
    c.execute("CREATE INDEX IF NOT EXISTS idx_active_posting_user_active ON active_posting_tasks(user_id, active)")
    c.execute("CREATE INDEX IF NOT EXISTS idx_auto_creation_user_active ON auto_creation_tasks(user_id, active)")
    c.execute("CREATE INDEX IF NOT EXISTS idx_licenses_status ON licenses(is_revoked, used_by, expires_at)")
    c.execute("CREATE INDEX IF NOT EXISTS idx_user_licenses_valid ON user_licenses(is_valid, expires_at)")
                
    # الإعدادات الأساسية (بدون تعديل)
    default_settings = [
        ('trust_channel', '', 'رابط القناة الرسمية'),
        ('rules_message', '', 'رسالة القوانين'),
        ('transfer_minimum', '5', 'الحد الأدنى للتحويل'),
        ('mandatory_channel', '', 'القناة الإجبارية للاشتراك'),
        ('posting_active', '0', 'تفعيل نظام النشر التلقائي'),
        ('multi_posting', '0', 'النشر المتعدد')
    ]
    
    for key, value, desc in default_settings:
        c.execute("INSERT OR IGNORE INTO settings (key, value, description) VALUES (?, ?, ?)", (key, value, desc))
    
    # إضافة الدول مع الأسعار الجديدة (سعر الشراء 1.5، سعر البيع 1.0)
    countries_data = [
        ("العراق 🇮🇶", "964", 1.5, 1.0),
        ("مصر 🇪🇬", "20", 1.5, 1.0),
        ("السعودية 🇸🇦", "966", 1.5, 1.0),
        ("الإمارات 🇦🇪", "971", 1.5, 1.0),
        ("الكويت 🇰🇼", "965", 1.5, 1.0),
        ("قطر 🇶🇦", "974", 1.5, 1.0),
        ("عُمان 🇴🇲", "968", 1.5, 1.0),
        ("لبنان 🇱🇧", "961", 1.5, 1.0),
        ("الأردن 🇯🇴", "962", 1.5, 1.0),
        ("الجزائر 🇩🇿", "213", 1.5, 1.0),
        ("المغرب 🇲🇦", "212", 1.5, 1.0),
        ("تونس 🇹🇳", "216", 1.5, 1.0),
        ("ليبيا 🇱🇾", "218", 1.5, 1.0),
        ("السودان 🇸🇩", "249", 1.5, 1.0),
        ("اليمن 🇾🇪", "967", 1.5, 1.0),
        ("سوريا 🇸🇾", "963", 1.5, 1.0),
        ("فلسطين 🇵🇸", "970", 1.5, 1.0),
        ("البحرين 🇧🇭", "973", 1.5, 1.0),
        ("موريتانيا 🇲🇷", "222", 1.5, 1.0),
        ("جيبوتي 🇩🇯", "253", 1.5, 1.0),
        ("تركيا 🇹🇷", "90", 1.5, 1.0),
        ("إيران 🇮🇷", "98", 1.5, 1.0),
        ("أفغانستان 🇦🇫", "93", 1.5, 1.0),
        ("ألبانيا 🇦🇱", "355", 1.5, 1.0),
        ("أندورا 🇦🇩", "376", 1.5, 1.0),
        ("أنغولا 🇦🇴", "244", 1.5, 1.0),
        ("أنتيغوا وباربودا 🇦🇬", "1268", 1.5, 1.0),
        ("الأرجنتين 🇦🇷", "54", 1.5, 1.0),
        ("أرمينيا 🇦🇲", "374", 1.5, 1.0),
        ("أستراليا 🇦🇺", "61", 1.5, 1.0),
        ("النمسا 🇦🇹", "43", 1.5, 1.0),
        ("أذربيجان 🇦🇿", "994", 1.5, 1.0),
        ("باهاماس 🇧🇸", "1242", 1.5, 1.0),
        ("بنغلاديش 🇧🇩", "880", 1.5, 1.0),
        ("باربادوس 🇧🇧", "1246", 1.5, 1.0),
        ("بيلاروسيا 🇧🇾", "375", 1.5, 1.0),
        ("بلجيكا 🇧🇪", "32", 1.5, 1.0),
        ("بليز 🇧🇿", "501", 1.5, 1.0),
        ("بنين 🇧🇯", "229", 1.5, 1.0),
        ("بوتان 🇧🇹", "975", 1.5, 1.0),
        ("بوليفيا 🇧🇴", "591", 1.5, 1.0),
        ("البوسنة والهرسك 🇧🇦", "387", 1.5, 1.0),
        ("بوتسوانا 🇧🇼", "267", 1.5, 1.0),
        ("البرازيل 🇧🇷", "55", 1.5, 1.0),
        ("بروناي 🇧🇳", "673", 1.5, 1.0),
        ("بلغاريا 🇧🇬", "359", 1.5, 1.0),
        ("بوركينا فاسو 🇧🇫", "226", 1.5, 1.0),
        ("بوروندي 🇧🇮", "257", 1.5, 1.0),
        ("الرأس الأخضر 🇨🇻", "238", 1.5, 1.0),
        ("كمبوديا 🇰🇭", "855", 1.5, 1.0),
        ("الكاميرون 🇨🇲", "237", 1.5, 1.0),
        ("كندا 🇨🇦", "1", 1.5, 1.0),
        ("جمهورية أفريقيا الوسطى 🇨🇫", "236", 1.5, 1.0),
        ("تشاد 🇹🇩", "235", 1.5, 1.0),
        ("تشيلي 🇨🇱", "56", 1.5, 1.0),
        ("الصين 🇨🇳", "86", 1.5, 1.0),
        ("كولومبيا 🇨🇴", "57", 1.5, 1.0),
        ("جزر القمر 🇰🇲", "269", 1.5, 1.0),
        ("الكونغو 🇨🇬", "242", 1.5, 1.0),
        ("كوستاريكا 🇨🇷", "506", 1.5, 1.0),
        ("كرواتيا 🇭🇷", "385", 1.5, 1.0),
        ("كوبا 🇨🇺", "53", 1.5, 1.0),
        ("قبرص 🇨🇾", "357", 1.5, 1.0),
        ("التشيك 🇨🇿", "420", 1.5, 1.0),
        ("الدنمارك 🇩🇰", "45", 1.5, 1.0),
        ("دومينيكا 🇩🇲", "1767", 1.5, 1.0),
        ("جمهورية الدومينيكان 🇩🇴", "1809", 1.5, 1.0),
        ("تيمور الشرقية 🇹🇱", "670", 1.5, 1.0),
        ("الإكوادور 🇪🇨", "593", 1.5, 1.0),
        ("السلفادور 🇸🇻", "503", 1.5, 1.0),
        ("غينيا الاستوائية 🇬🇶", "240", 1.5, 1.0),
        ("إريتريا 🇪🇷", "291", 1.5, 1.0),
        ("إستونيا 🇪🇪", "372", 1.5, 1.0),
        ("إسواتيني 🇸🇿", "268", 1.5, 1.0),
        ("إثيوبيا 🇪🇹", "251", 1.5, 1.0),
        ("فيجي 🇫🇯", "679", 1.5, 1.0),
        ("فنلندا 🇫🇮", "358", 1.5, 1.0),
        ("فرنسا 🇫🇷", "33", 1.5, 1.0),
        ("الغابون 🇬🇦", "241", 1.5, 1.0),
        ("غامبيا 🇬🇲", "220", 1.5, 1.0),
        ("جورجيا 🇬🇪", "995", 1.5, 1.0),
        ("ألمانيا 🇩🇪", "49", 1.5, 1.0),
        ("غانا 🇬🇭", "233", 1.5, 1.0),
        ("اليونان 🇬🇷", "30", 1.5, 1.0),
        ("غرينادا 🇬🇩", "1473", 1.5, 1.0),
        ("غواتيمالا 🇬🇹", "502", 1.5, 1.0),
        ("غينيا 🇬🇳", "224", 1.5, 1.0),
        ("غينيا بيساو 🇬🇼", "245", 1.5, 1.0),
        ("غيانا 🇬🇾", "592", 1.5, 1.0),
        ("هايتي 🇭🇹", "509", 1.5, 1.0),
        ("هندوراس 🇭🇳", "504", 1.5, 1.0),
        ("المجر 🇭🇺", "36", 1.5, 1.0),
        ("آيسلندا 🇮🇸", "354", 1.5, 1.0),
        ("الهند 🇮🇳", "91", 1.5, 1.0),
        ("إندونيسيا 🇮🇩", "62", 1.5, 1.0),
        ("جمهورية أيرلندا 🇮🇪", "353", 1.5, 1.0),
        ("إيطاليا 🇮🇹", "39", 1.5, 1.0),
        ("جامايكا 🇯🇲", "1876", 1.5, 1.0),
        ("اليابان 🇯🇵", "81", 1.5, 1.0),
        ("كازاخستان 🇰🇿", "7", 1.5, 1.0),
        ("كينيا 🇰🇪", "254", 1.5, 1.0),
        ("كيريباتي 🇰🇮", "686", 1.5, 1.0),
        ("كوسوفو 🇽🇰", "383", 1.5, 1.0),
        ("قيرغيزستان 🇰🇬", "996", 1.5, 1.0),
        ("لاوس 🇱🇦", "856", 1.5, 1.0),
        ("لاتفيا 🇱🇻", "371", 1.5, 1.0),
        ("ليسوتو 🇱🇸", "266", 1.5, 1.0),
        ("ليبيريا 🇱🇷", "231", 1.5, 1.0),
        ("ليختنشتاين 🇱🇮", "423", 1.5, 1.0),
        ("ليتوانيا 🇱🇹", "370", 1.5, 1.0),
        ("لوكسمبورغ 🇱🇺", "352", 1.5, 1.0),
        ("مدغشقر 🇲🇬", "261", 1.5, 1.0),
        ("ملاوي 🇲🇼", "265", 1.5, 1.0),
        ("ماليزيا 🇲🇾", "60", 1.5, 1.0),
        ("جزر المالديف 🇲🇻", "960", 1.5, 1.0),
        ("مالي 🇲🇱", "223", 1.5, 1.0),
        ("مالطا 🇲🇹", "356", 1.5, 1.0),
        ("جزر مارشال 🇲🇭", "692", 1.5, 1.0),
        ("موريشيوس 🇲🇺", "230", 1.5, 1.0),
        ("المكسيك 🇲🇽", "52", 1.5, 1.0),
        ("ميكرونيزيا 🇫🇲", "691", 1.5, 1.0),
        ("مولدوفا 🇲🇩", "373", 1.5, 1.0),
        ("موناكو 🇲🇨", "377", 1.5, 1.0),
        ("منغوليا 🇲🇳", "976", 1.5, 1.0),
        ("الجبل الأسود 🇲🇪", "382", 1.5, 1.0),
        ("موزمبيق 🇲🇿", "258", 1.5, 1.0),
        ("ميانمار 🇲🇲", "95", 1.5, 1.0),
        ("ناميبيا 🇳🇦", "264", 1.5, 1.0),
        ("ناورو 🇳🇷", "674", 1.5, 1.0),
        ("نيبال 🇳🇵", "977", 1.5, 1.0),
        ("هولندا 🇳🇱", "31", 1.5, 1.0),
        ("نيوزيلندا 🇳🇿", "64", 1.5, 1.0),
        ("نيكاراجوا 🇳🇮", "505", 1.5, 1.0),
        ("النيجر 🇳🇪", "227", 1.5, 1.0),
        ("نيجيريا 🇳🇬", "234", 1.5, 1.0),
        ("كوريا الشمالية 🇰🇵", "850", 1.5, 1.0),
        ("مقدونيا الشمالية 🇲🇰", "389", 1.5, 1.0),
        ("النرويج 🇳🇴", "47", 1.5, 1.0),
        ("باكستان 🇵🇰", "92", 1.5, 1.0),
        ("بالاو 🇵🇼", "680", 1.5, 1.0),
        ("بنما 🇵🇦", "507", 1.5, 1.0),
        ("بابوا غينيا الجديدة 🇵🇬", "675", 1.5, 1.0),
        ("باراغواي 🇵🇾", "595", 1.5, 1.0),
        ("بيرو 🇵🇪", "51", 1.5, 1.0),
        ("الفلبين 🇵🇭", "63", 1.5, 1.0),
        ("بولندا 🇵🇱", "48", 1.5, 1.0),
        ("البرتغال 🇵🇹", "351", 1.5, 1.0),
        ("رومانيا 🇷🇴", "40", 1.5, 1.0),
        ("روسيا 🇷🇺", "7", 1.5, 1.0),
        ("رواندا 🇷🇼", "250", 1.5, 1.0),
        ("سانت كيتس ونيفيس 🇰🇳", "1869", 1.5, 1.0),
        ("سانت لوسيا 🇱🇨", "1758", 1.5, 1.0),
        ("سانت فينسنت والغرينادين 🇻🇨", "1784", 1.5, 1.0),
        ("ساموا 🇼🇸", "685", 1.5, 1.0),
        ("سان مارينو 🇸🇲", "378", 1.5, 1.0),
        ("ساو تومي وبرينسيبي 🇸🇹", "239", 1.5, 1.0),
        ("السنغال 🇸🇳", "221", 1.5, 1.0)
    ]
    
    for name, code, price, sell_price in countries_data:
        c.execute("INSERT OR IGNORE INTO countries (name, calling_code, price, sell_price) VALUES (?, ?, ?, ?)", 
                 (name, code, price, sell_price))
    
    conn.commit()
    conn.close()
