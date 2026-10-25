"""Default main menu and styled bot panels."""
import re
from telethon import errors


DEFAULT_ITEMS = [
    ('numbers_menu', 'قسم الأرقام'), ('control_menu', 'قسم التحكم'), ('balance_menu', 'قسم الرصيد'),
    ('install_menu', 'تنصيب الحسابات'), ('accounts_view', 'حساباتي'), ('engagement_menu', 'قسم الرشق'),
    ('rent_numbers', 'تأجير الأرقام'), ('shahm:menu', 'شهم · التجميع والتحويل'),
    ('promo:menu', 'الرشق المدفوع'), ('fund:menu', 'التمويل وجمع النقاط'),
    ('feature:shop:list:0', 'المتجر والمفضلة'), ('auction_menu', 'المزادات'),
    ('feature:loyalty:view:0', 'الكوبونات والولاء'), ('ops:jobs:list:0', 'مركز المهام'),
    ('codes:menu', 'مراقبة البوتات'),
    ('mandatory:menu', 'اشتراك إجباري لقناتي'),
    ('api:menu', 'قسم API'),
    ('supper', 'الدعم'), ('feature:tickets:list:0', 'تذاكري'), ('liscgh', 'القوانين'),
    ('my_activity', 'نشاطي'), ('referral_menu', 'رابط الإحالة'), ('achievements', 'الإنجازات'),
    ('premium_join', 'الانضمام للمميز'), ('developer_menu', 'قسم المطور'),
]
DEFAULT_TITLE = (
    '✨ <b>✦ SHAHEM PLATFORM ✦</b>\n'
    '<b>مـنـصـة شـهـم للخـدمـات الـرقـمـيـة</b>\n\n'
    '━━━━━━━━━━━━━━━━\n\n'
    '🚀 <b>جميع خدماتك في مكان واحد</b>\n'
    'اختر القسم الذي ترغب باستخدامه من القائمة أدناه.\n\n'
    '━━━━━━━━━━━━━━━━'
)
_cached = {'title': DEFAULT_TITLE, 'items': DEFAULT_ITEMS}


async def refresh():
    # Keep the standard main menu consistent after refreshing.
    global _cached
    _cached = {'title': DEFAULT_TITLE, 'items': list(DEFAULT_ITEMS)}
    return _cached


def cached():
    return _cached


def panel_text(text):
    if not isinstance(text, str) or text.startswith(('✦ ', '╭─')) or not text.strip():
        return text
    title, _, body = text.strip().partition('\n')
    title = re.sub(r'^\s*[•*]+\s*|\s*\*+\s*$', '', title).strip()
    result = f'✦ {title}'
    if body.strip():
        result += '\n\n' + body.strip()
    return result


class PanelEvent:
    """Style bot panels, leaving credentials, files and plain messages unchanged."""
    def __init__(self, event):
        self._event = event
        self._input_started = False

    def mark_input_started(self):
        # After a prompt, results must be visible below the user's reply rather
        # than in a stale callback popup or an old panel above the prompt.
        self._input_started = True

    def __getattr__(self, name):
        return getattr(self._event, name)

    def prepare(self, args, kwargs):
        from keyboards import arrange_buttons, ensure_button_colors, FixedButtonRows
        buttons = kwargs.get('buttons')
        if buttons is not None:
            buttons = kwargs['buttons'] = ensure_button_colors(buttons)
        if isinstance(buttons, (list, tuple)) and buttons:
            flat = [button for row in buttons for button in (row if isinstance(row, (list, tuple)) else [row])]
            if not isinstance(buttons, FixedButtonRows):
                kwargs['buttons'] = arrange_buttons(flat)
            if args:
                args = (panel_text(args[0]), *args[1:])
        return args, kwargs

    async def edit(self, *args, **kwargs):
        args, kwargs = self.prepare(args, kwargs)
        if self._input_started:
            return await self._event.respond(*args, **kwargs)
        try:
            return await self._event.edit(*args, **kwargs)
        except errors.MessageNotModifiedError:
            return None
        except (errors.MessageIdInvalidError, errors.MessageEditTimeExpiredError):
            return await self._event.respond(*args, **kwargs)

    async def answer(self, text=None, **kwargs):
        # CallbackQuery.answer may also silently ignore text after an earlier
        # acknowledgement. Never rely on it to deliver results after user input.
        if text and (self._input_started or getattr(self._event, '_answered', False)):
            return await self._event.respond(text, parse_mode=None)
        try:
            return await self._event.answer(text, **kwargs)
        except errors.QueryIdInvalidError:
            if text:
                return await self._event.respond(text, parse_mode=None)

    async def respond(self, *args, **kwargs):
        args, kwargs = self.prepare(args, kwargs)
        return await self._event.respond(*args, **kwargs)


def style_transport(client):
    """Apply the same panel layout to conversation prompts and background bot notices."""
    if getattr(client, '_ui_transport_ready', False):
        return
    send = client.send_message
    edit = client.edit_message
    send_file = getattr(client, 'send_file', None)

    def prepare(text, kwargs):
        from keyboards import arrange_buttons, ensure_button_colors, FixedButtonRows
        buttons = kwargs.get('buttons')
        if buttons is not None:
            buttons = kwargs['buttons'] = ensure_button_colors(buttons)
        if isinstance(buttons, (list, tuple)) and buttons:
            flat = [button for row in buttons for button in (row if isinstance(row, (list, tuple)) else [row])]
            if not isinstance(buttons, FixedButtonRows):
                kwargs['buttons'] = arrange_buttons(flat)
            text = panel_text(text)
        return text

    async def themed_send(entity, message=None, *args, **kwargs):
        return await send(entity, prepare(message, kwargs), *args, **kwargs)

    async def themed_edit(entity, message=None, text=None, *args, **kwargs):
        if text is not None:
            text = prepare(text, kwargs)
        elif isinstance(message, str):
            message = prepare(message, kwargs)
        return await edit(entity, message, text, *args, **kwargs)

    client.send_message = themed_send
    client.edit_message = themed_edit
    if callable(send_file):
        async def themed_file(entity, file, *args, **kwargs):
            from keyboards import ensure_button_colors
            if kwargs.get('buttons') is not None:
                kwargs['buttons'] = ensure_button_colors(kwargs['buttons'])
            return await send_file(entity, file, *args, **kwargs)
        client.send_file = themed_file
    client._ui_transport_ready = True


