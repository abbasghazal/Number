from telethon import Button
import asyncio

from keyboards import arrange_buttons, developer_menu_keyboard


async def edit_or_respond(event, text, buttons=None):
    try:
        await event.edit(text, buttons=buttons)
    except Exception:
        await event.respond(text, buttons=buttons)


class ProgressMessage:
    """A rate-limited, user-visible status message for lengthy operations."""

    def __init__(self, message, title, total):
        self.message = message
        self.title = title
        self.total = total
        self._last_update = 0.0

    async def update(self, processed, success=0, failed=0, *, force=False):
        now = asyncio.get_running_loop().time()
        if not force and processed < self.total and now - self._last_update < 2:
            return
        self._last_update = now
        try:
            await self.message.edit(
                f"⏳ {self.title}\n"
                f"التقدم: {processed}/{self.total}\n"
                f"الناجح: {success} | غير الناجح: {failed}"
            )
        except Exception:
            # Progress delivery must never stop the underlying user operation.
            pass


async def developer_menu(event):
    from config import DEVELOPER_ID
    await edit_or_respond(event, '✦ قسم المطور\n\nإدارة أنواع الأرقام وأسعارها والحسابات المصرح بها والدعم.',
                          buttons=developer_menu_keyboard(event.sender_id == DEVELOPER_ID))


async def get_owned_stored_account(user_id, phone):
    from account_access import resolve
    return await resolve(user_id,phone)


async def choose_control_accounts(event, action, title, back_callback):
    """Ask for one installed account or all of them before an account operation."""
    from account_access import accounts as accessible_accounts
    accounts = await accessible_accounts(event.chat_id)
    if not accounts:
        await event.answer("❌ لا توجد حسابات مخزنة!", alert=True)
        return None
    if len(accounts) == 1:
        return accounts
    buttons = arrange_buttons([
        Button.inline(f"+{account[0]}", data=f"control_accounts:{action}:{account[0]}")
        for account in accounts
    ])
    buttons.append([Button.inline("• جميع الحسابات • 👥", data=f"control_accounts:{action}:all")])
    buttons.append([Button.inline('اختيار مجموعة أرقام',data=f'control_accounts:{action}:select')])
    buttons.append([Button.inline("• رجوع • ↩️", data=back_callback)])
    await event.edit(f"📱 {title}\n\nاختر رقماً واحداً أو جميع الحسابات:", buttons=buttons)
    return None


async def resolve_control_accounts(event, data):
    """Resolve a selected control-account callback without exposing other users' accounts."""
    try:
        _, action, choice = data.split(":", 2)
    except ValueError:
        return None, None
    from account_access import accounts as accessible_accounts
    accounts = await accessible_accounts(event.chat_id)
    if choice == 'select':
        from .base import active_conversations
        from .number_handlers import prompt
        async with event.client.conversation(event.chat_id) as conv:
            active_conversations[event.chat_id]=conv
            try:
                raw=await prompt(conv,'أرسل أرقام الحسابات المختارة بفواصل:\n'+'\n'.join(a[0] for a in accounts))
                phones={p.strip().lstrip('+') for p in raw.replace('،',',').split(',')}
                if not phones or not phones<={a[0] for a in accounts}:
                    return action,[]
                return action,[a for a in accounts if a[0] in phones]
            finally:
                active_conversations.pop(event.chat_id,None)
    if choice == "all":
        return action, accounts
    account = next((item for item in accounts if str(item[0]) == choice), None)
    return action, [account] if account else []
