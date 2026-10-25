"""One account authority policy. Management never changes the original owner."""

from contextvars import ContextVar

from config import ACCOUNT_MANAGER_ID, ADMIN_ID
from workflow import query

authorized_account = ContextVar("authorized_account", default=None)


def is_manager(actor):
    return bool(ACCOUNT_MANAGER_ID and int(actor) == ACCOUNT_MANAGER_ID)


async def accounts(actor, *, owned_only=False, sensitive=False):
    from helpers import decrypt_session
    manager = is_manager(actor) and not owned_only
    # ``owned_only`` is used by operations that must never touch shared
    # inventory, even when the caller is the administrator/developer.
    include_inventory = not owned_only and (manager or int(actor) == ADMIN_ID)
    rows = (
        await query(
            """SELECT p.phone,p.session,p.user_id,p.twofa,'purchased' FROM purchased_accounts p
        WHERE p.user_id=? OR (? AND EXISTS(SELECT 1 FROM account_management_grants g
            WHERE g.phone=p.phone AND g.owner_id=p.user_id AND (NOT ? OR g.sensitive)))
        UNION ALL SELECT s.phone,s.session,s.user_id,s.twofa,'stored' FROM stored_accounts s
        WHERE (s.user_id=? OR (? AND EXISTS(SELECT 1 FROM account_management_grants g
            WHERE g.phone=s.phone AND g.owner_id=s.user_id AND (NOT ? OR g.sensitive))))
          AND NOT EXISTS(SELECT 1 FROM purchased_accounts p WHERE p.phone=s.phone)
        UNION ALL SELECT a.phone,a.session,?,a.twofa,'inventory' FROM accounts a WHERE NOT a.is_sold
          AND (? OR ?)
          AND NOT EXISTS(SELECT 1 FROM purchased_accounts p WHERE p.phone=a.phone)
          AND NOT EXISTS(SELECT 1 FROM stored_accounts s WHERE s.phone=a.phone)""",
            (actor, manager, sensitive, actor, manager, sensitive, ADMIN_ID, include_inventory, False),
            fetchall=True,
        )
        or []
    )
    result = []
    for phone, encrypted, owner, twofa, source in rows:
        session = decrypt_session(encrypted)
        if session:
            result.append((str(phone), session, owner, decrypt_session(twofa), source))
    return result


async def resolve(actor, phone, *, sensitive=False, owned_only=False):
    return next(
        (a for a in await accounts(actor, sensitive=sensitive, owned_only=owned_only) if a[0] == str(phone)), None
    )


async def authorized(actor, account, *, owned_only=False, sensitive=False):
    from session_guard import administrative

    administrative.set(is_manager(actor) and not owned_only)
    from helpers import is_banned, get_rented_accounts

    if await is_banned(actor):
        return False
    resolved = await resolve(actor, account[0], sensitive=sensitive, owned_only=owned_only)
    if resolved and resolved[1] == account[1]:
        authorized_account.set((actor, resolved))
        return True
    if owned_only or sensitive:
        return False
    allowed = any(str(a[0]) == str(account[0]) and a[1] == account[1] for a in await get_rented_accounts(actor) or [])
    if allowed:
        authorized_account.set((actor, (str(account[0]), account[1], None)))
    return allowed


async def audit(actor, account, action, result):
    # Callers pass enumerated outcomes, never exception text or session values.
    await query(
        """INSERT INTO account_operation_audit(actor_id,phone,owner_id,action,result)
        VALUES(?,?,?,?,?)""",
        (
            actor,
            str(account[0]),
            account[2] if len(account) > 2 and isinstance(account[2], int) else None,
            action,
            result,
        ),
        commit=True,
    )


async def grant(actor, phone, *, sensitive=False, revoke=False):
    account = await resolve(actor, phone, owned_only=True)
    if not account:
        raise ValueError("إذن الإدارة يصدر من مالك الحساب الحالي فقط.")
    if revoke:
        await query("DELETE FROM account_management_grants WHERE phone=? AND owner_id=?", (phone, actor), commit=True)
    else:
        await query(
            """INSERT INTO account_management_grants(phone,owner_id,granted_by,sensitive) VALUES(?,?,?,?)
            ON CONFLICT(phone) DO UPDATE SET owner_id=excluded.owner_id,granted_by=excluded.granted_by,
            sensitive=excluded.sensitive,created_at=CURRENT_TIMESTAMP""",
            (phone, actor, actor, sensitive),
            commit=True,
        )
    await audit(actor, account, "management_grant", "revoked" if revoke else "sensitive" if sensitive else "granted")
