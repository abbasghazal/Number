from telethon import Button

from helpers import execute_query


async def achievements_menu(event):
    """Show aggregate leaders by Telegram ID only, preserving user privacy."""
    rows = await execute_query(
        """WITH leaders AS (
            SELECT 'أفضل مزايد' AS label, current_bidder AS user_id, MAX(current_bid) AS score
            FROM auctions WHERE current_bidder IS NOT NULL GROUP BY current_bidder
            UNION ALL
            SELECT 'أفضل بائع', seller_id, COUNT(*)::numeric FROM sold_accounts
            WHERE seller_id IS NOT NULL GROUP BY seller_id
            UNION ALL
            SELECT 'أعلى مالك أرقام', user_id, COUNT(*)::numeric FROM (
                SELECT user_id, phone FROM stored_accounts
                UNION SELECT user_id, phone FROM purchased_accounts
            ) owned GROUP BY user_id
        )
        SELECT DISTINCT ON (label) label, user_id, score FROM leaders
        WHERE user_id IS NOT NULL ORDER BY label, score DESC, user_id ASC""",
        fetchall=True,
    ) or []
    by_label = {label: (user_id, score) for label, user_id, score in rows}
    text = "🏆 **الإنجازات**\n\n"
    for label in ("أفضل مزايد", "أفضل بائع", "أعلى مالك أرقام"):
        result = by_label.get(label)
        text += f"• {label}: `{result[0]}`\n" if result else f"• {label}: لا توجد بيانات بعد\n"
    await event.edit(text, buttons=[[Button.inline("• رجوع • ↩️", data="main")]])
