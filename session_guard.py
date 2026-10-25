"""Coordinate shared and exclusive service clients in this process.

Normal clients retain their existing shared behaviour. An exclusive client can only
connect when no other service uses that authorization key, and blocks new
service clients until it disconnects. This bot runs one main process.
"""
import hashlib
import threading
import contextvars
import contextlib
from telethon import TelegramClient as BaseClient

_guard = threading.RLock()
_users = {}
administrative = contextvars.ContextVar('administrative_session',default=False)


class SessionBusyError(RuntimeError):
    pass


def claim(key, owner, exclusive=False):
    if not key:
        return
    with _guard:
        users = _users.setdefault(key, {})
        others = {who: mode for who, mode in users.items() if who is not owner}
        if others and (exclusive or any(others.values())):
            raise SessionBusyError('الحساب مشغول بخدمة أخرى؛ أوقفها أو انتظر انتهاءها.')
        users[owner] = exclusive


def release(key, owner):
    with _guard:
        users = _users.get(key, {})
        users.pop(owner, None)
        if not users:
            _users.pop(key, None)


class TelegramClient(BaseClient):
    def __init__(self, *args, exclusive_session=False, actor_id=None, account_phone=None, sensitive_action=False, **kwargs):
        super().__init__(*args, **kwargs)
        if actor_id is None:
            from account_access import authorized_account
            from telethon.sessions import StringSession
            context=authorized_account.get()
            if context:
                try:
                    expected=StringSession(context[1][1]).auth_key
                    actual=self.session.auth_key
                    if expected and actual and expected.key==actual.key:
                        actor_id,account_phone=context[0],context[1][0]
                except (TypeError,ValueError):
                    pass
        self._exclusive_session = exclusive_session or actor_id is not None
        self._guard_key = None
        self._actor_id = actor_id
        self._account_phone = account_phone
        self._sensitive_action = sensitive_action

    async def _invoke(self, request, *args, **kwargs):
        try:
            return await super().__call__(request,*args,**kwargs)
        except Exception as exc:
            await self._record_revocation(exc)
            raise

    async def _record_revocation(self, exc):
        from session_lifecycle import REVOKED_ERRORS,on_rpc_error
        if type(exc).__name__ in REVOKED_ERRORS:
            with contextlib.suppress(Exception):
                await on_rpc_error(self.session.save(),exc,phone=self._account_phone)

    async def __call__(self, request, *args, **kwargs):
        if self._actor_id is None or self._account_phone is None:
            return await self._invoke(request,*args,**kwargs)
        from account_access import authorized,resolve,audit
        names = [type(r).__name__ for r in request] if isinstance(request,(list,tuple)) else [type(request).__name__]
        sensitive = self._sensitive_action or any(n in {'ResetAuthorizationsRequest','ResetAuthorizationRequest',
            'UpdatePasswordSettingsRequest','DeleteAccountRequest'} for n in names)
        account=(str(self._account_phone),self.session.save())
        if not await authorized(self._actor_id,account,sensitive=sensitive):
            await audit(self._actor_id,account,','.join(names)[:160],'permission_denied')
            raise PermissionError('انتهت صلاحية إدارة الحساب.')
        resolved=await resolve(self._actor_id,self._account_phone,sensitive=sensitive)
        account=resolved or account
        result='failed'
        try:
            response=await self._invoke(request,*args,**kwargs)
            result='completed'
            return response
        finally:
            await audit(self._actor_id,account,','.join(names)[:160],result)

    async def connect(self):
        if self._actor_id is not None and self._account_phone is not None:
            from account_access import authorized
            if not await authorized(self._actor_id,(str(self._account_phone),self.session.save()),sensitive=self._sensitive_action):
                raise PermissionError('انتهت صلاحية إدارة الحساب.')
        auth = self.session.auth_key
        key = hashlib.sha256(auth.key).hexdigest() if auth else None
        if administrative.get():
            with _guard:
                preemptible = [other for other in _users.get(key,{})
                               if other is not self and getattr(other,'_automation_preemptible',False)]
            for other in preemptible:
                await other.disconnect()
        claim(key, self, self._exclusive_session)
        self._guard_key = key
        try:
            return await super().connect()
        except BaseException as exc:
            release(key, self)
            self._guard_key = None
            if isinstance(exc,Exception):
                await self._record_revocation(exc)
            raise

    async def _disconnect_coro(self):
        try:
            return await super()._disconnect_coro()
        finally:
            release(self._guard_key, self)
            self._guard_key = None
