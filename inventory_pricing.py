"""Versioned dollar prices and defaults for each number type."""

from decimal import Decimal

# (What the customer pays the bot, what the bot pays the customer), in dollars.
DEFAULT_PRICES = {
    "spam": (Decimal("0.18"), Decimal("0.15")),
    "clean": (Decimal("0.65"), Decimal("0.60")),
    "guaranteed": (Decimal("1.20"), Decimal("1.00")),
    "special": (Decimal("3.00"), Decimal("2.00")),
}
POLICY_KEY = "number_type_dollar_prices_20261021_v1"

INSERT_PRICE = """INSERT INTO number_type_prices(country_id,number_type,
    bot_sell_price,bot_buy_price,sell_enabled,buy_enabled)
    SELECT c.country_id,?,?,?,TRUE,TRUE FROM countries c WHERE TRUE"""


def seed_type_price(db, country, kind):
    # Existing type settings, including an explicit pause, always take precedence.
    sell, buy = DEFAULT_PRICES[kind]
    return db.execute(
        INSERT_PRICE + " AND c.country_id=?"
        + " ON CONFLICT(country_id,number_type) DO NOTHING",
        (kind, sell, buy, country),
    ).rowcount


def seed_country_prices(db, country):
    return sum(seed_type_price(db, country, kind) for kind in DEFAULT_PRICES)


def repair_missing_prices(db):
    # Pricing does not classify accounts or approve unreviewed inventory.
    return sum(
        db.execute(
            INSERT_PRICE + " ON CONFLICT(country_id,number_type) DO NOTHING", (kind, sell, buy)
        ).rowcount
        for kind, (sell, buy) in DEFAULT_PRICES.items()
    )


def apply_default_prices(db):
    """Apply the requested rates once; later runs preserve administrator edits.

    The caller's transaction also commits the policy marker, so concurrent starts
    and interrupted migrations cannot partially apply the policy.
    """
    db.execute("SELECT pg_advisory_xact_lock(hashtext(?))", (POLICY_KEY,))
    if db.execute("SELECT 1 FROM settings WHERE key=?", (POLICY_KEY,)).fetchone():
        return repair_missing_prices(db)
    changed = 0
    for kind, (sell, buy) in DEFAULT_PRICES.items():
        changed += db.execute(
            INSERT_PRICE + """ ON CONFLICT(country_id,number_type) DO UPDATE SET
            bot_sell_price=excluded.bot_sell_price,bot_buy_price=excluded.bot_buy_price,
            sell_enabled=CASE WHEN number_type_prices.updated_by IS NULL THEN TRUE
                ELSE number_type_prices.sell_enabled END,
            buy_enabled=CASE WHEN number_type_prices.updated_by IS NULL THEN TRUE
                ELSE number_type_prices.buy_enabled END,
            version=number_type_prices.version+1,updated_at=CURRENT_TIMESTAMP""",
            (kind, sell, buy),
        ).rowcount
    db.execute("INSERT INTO settings(key,value) VALUES(?,'1')", (POLICY_KEY,))
    return changed
