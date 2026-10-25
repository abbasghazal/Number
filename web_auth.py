"""Telegram OIDC/PKCE and secure local sessions; no Telegram secrets reach the browser."""

import base64
import hashlib
import hmac
import json
import os
import secrets
import time
from urllib.parse import urlencode, urlsplit

import aiohttp
from aiohttp import web
from cryptography.hazmat.primitives import hashes
from cryptography.hazmat.primitives.asymmetric import padding, rsa

from workflow import query

COOKIE = "number_session"
SESSION_POLICY = "web_identity_isolation_v2"
_keys = None
_keys_until = 0


def digest(value):
    return hashlib.sha256(value.encode()).hexdigest()


def b64(value):
    return base64.urlsafe_b64encode(value).rstrip(b"=").decode()


def decode(value):
    return base64.urlsafe_b64decode(value + "=" * (-len(value) % 4))


def origin(request):
    configured = os.getenv("WEB_ORIGIN", "").strip().rstrip("/")
    if not configured:
        from miniapp_server import public_url

        known = public_url()
        if known:
            parsed = urlsplit(known)
            configured = f"{parsed.scheme}://{parsed.netloc}"
    if not configured:
        configured = f"{request.scheme}://{request.host}"
    parsed = urlsplit(configured)
    if parsed.scheme != "https" and parsed.hostname not in {"localhost", "127.0.0.1", "::1"}:
        raise ValueError("اضبط WEB_ORIGIN على أصل HTTPS الصحيح للموقع.")
    if parsed.username or parsed.password or parsed.path not in ("", "/") or parsed.query or parsed.fragment:
        raise ValueError("WEB_ORIGIN غير صالح.")
    return configured


def same_origin(request):
    incoming = request.headers.get("Origin")
    if incoming and incoming.rstrip("/") != origin(request):
        raise web.HTTPForbidden(text="مصدر الطلب غير مسموح.")


def migrate(db):
    db.execute("ALTER TABLE platform_sessions ADD COLUMN IF NOT EXISTS init_data_hash TEXT")
    db.execute("SELECT pg_advisory_xact_lock(hashtext(?))", (SESSION_POLICY,))
    if not db.execute("SELECT 1 FROM settings WHERE key=?", (SESSION_POLICY,)).fetchone():
        # Old sessions did not distinguish browser login from a Telegram launch.
        db.execute("DELETE FROM platform_sessions")
        db.execute("INSERT INTO settings(key,value) VALUES(?,'1')", (SESSION_POLICY,))


async def issue(user, *, init_data=None):
    token, csrf = secrets.token_urlsafe(32), secrets.token_urlsafe(24)
    await query(
        "INSERT INTO platform_sessions(token_hash,user_id,csrf,init_data_hash,expires_at) VALUES(?,?,?,?,CURRENT_TIMESTAMP+INTERVAL '8 hours')",
        (digest(token), user, csrf, digest(init_data) if init_data else None),
        commit=True,
    )
    return token, csrf


def set_cookie(response, request, token):
    response.set_cookie(
        COOKIE,
        token,
        max_age=28800,
        httponly=True,
        secure=origin(request).startswith("https://"),
        samesite="Lax",
        path="/",
    )


async def session(request, *, init_data=None):
    token = request.cookies.get(COOKIE, "")
    if not token:
        return None
    row = await query(
        "SELECT user_id,csrf,init_data_hash FROM platform_sessions WHERE token_hash=? AND expires_at>CURRENT_TIMESTAMP",
        (digest(token),),
        fetchone=True,
    )
    if not row:
        return None
    expected = digest(init_data) if init_data else None
    if row[2] != expected:
        return None
    if request.method not in ("GET", "HEAD", "OPTIONS"):
        same_origin(request)
        if not hmac.compare_digest(request.headers.get("X-CSRF-Token", ""), row[1]):
            raise web.HTTPForbidden(text="انتهى رمز حماية الطلب؛ أعد تحميل الصفحة.")
    return row[:2]


async def authenticate(request):
    """A Telegram launch can never inherit another launch or browser identity."""
    from miniapp_server import TOKEN_KEY, validate_init_data

    if request.method not in ("GET", "HEAD", "OPTIONS"):
        same_origin(request)
    raw = request.headers.get("X-Telegram-Init-Data", "")
    bound = None
    if raw or request.headers.get("X-Number-Client") != "miniapp":
        bound = await session(request, init_data=raw or None)
    if raw:
        if bound:
            user, csrf = bound
        else:
            try:
                user = validate_init_data(raw, request.app[TOKEN_KEY])
            except (ValueError, TypeError, json.JSONDecodeError) as exc:
                raise web.HTTPUnauthorized(text=str(exc)) from None
            csrf = ""
        source = "miniapp"
    elif bound:
        user, csrf = bound
        source = "browser"
    else:
        raise web.HTTPUnauthorized(text="سجّل الدخول أو افتح الموقع من البوت لتوثيق حسابك.")
    expected = request.headers.get("X-Number-User", "")
    if expected and expected != str(user):
        raise web.HTTPUnauthorized(text="تغير حساب الدخول؛ أعد التحقق من هويتك.")
    return user, csrf, source


async def jwks():
    global _keys, _keys_until
    if _keys and time.monotonic() < _keys_until:
        return _keys
    async with aiohttp.ClientSession(timeout=aiohttp.ClientTimeout(total=15)) as client:
        async with client.get("https://oauth.telegram.org/.well-known/jwks.json") as result:
            if result.status != 200:
                raise ValueError("تعذر تحميل مفاتيح دخول تيليجرام.")
            data = await result.json()
    keys = data.get("keys", [])
    if not isinstance(keys, list) or not 1 <= len(keys) <= 20:
        raise ValueError("مفاتيح دخول غير صالحة.")
    _keys, _keys_until = keys, time.monotonic() + 3600
    return keys


async def validate_id_token(token, client_id, nonce, *, now=None, keys=None):
    if not isinstance(token, str) or len(token) > 16384:
        raise ValueError("رمز دخول غير صالح.")
    try:
        head, payload, signature = token.split(".")
        header = json.loads(decode(head))
        claims = json.loads(decode(payload))
        if header.get("alg") != "RS256" or not header.get("kid"):
            raise ValueError("خوارزمية دخول غير مسموحة.")
        key = next(
            k
            for k in (keys if keys is not None else await jwks())
            if k.get("kid") == header["kid"] and k.get("kty") == "RSA"
        )
        public = rsa.RSAPublicNumbers(
            int.from_bytes(decode(key["e"]), "big"), int.from_bytes(decode(key["n"]), "big")
        ).public_key()
        public.verify(decode(signature), f"{head}.{payload}".encode(), padding.PKCS1v15(), hashes.SHA256())
        stamp = time.time() if now is None else now
        audience = claims.get("aud")
        valid_audience = str(client_id) in audience if isinstance(audience, list) else audience == str(client_id)
        if (
            claims.get("iss") != "https://oauth.telegram.org"
            or not valid_audience
            or not stamp - 900 <= float(claims["iat"]) <= stamp + 30
            or float(claims["exp"]) <= stamp
            or float(claims.get("nbf", 0)) > stamp + 30
        ):
            raise ValueError("انتهى رمز الدخول أو لا ينتمي لهذا الموقع.")
        if not hmac.compare_digest(str(claims.get("nonce", "")), nonce):
            raise ValueError("رمز جلسة الدخول غير مطابق.")
        identity = claims.get("id")
        if isinstance(identity, bool) or not isinstance(identity, int) or not 0 < identity < 2**63:
            raise ValueError("هوية دخول غير صالحة؛ يجب طلب نطاق profile.")
        return identity
    except (ValueError, TypeError, KeyError, StopIteration, OverflowError):
        raise ValueError("تعذر توثيق هوية تيليجرام.") from None
    except Exception:
        raise ValueError("توقيع دخول غير صالح.") from None


async def login(request):
    from miniapp_server import throttle

    if not throttle("login:" + str(request.remote)):
        raise web.HTTPTooManyRequests(text="طلبات كثيرة؛ انتظر قليلاً.")
    client_id = os.getenv("TELEGRAM_LOGIN_CLIENT_ID", "").strip()
    if not client_id or not os.getenv("TELEGRAM_LOGIN_CLIENT_SECRET", "").strip():
        from miniapp_server import response

        return response({"error": "دخول المتصفح يحتاج إعداد Telegram Login من المطور. يمكنك فتح المنصة من البوت."}, 503)
    state, verifier, nonce = secrets.token_urlsafe(32), secrets.token_urlsafe(48), secrets.token_urlsafe(24)
    from helpers import encrypt_session

    await query(
        "INSERT INTO platform_oauth(state_hash,verifier,nonce,expires_at) VALUES(?,?,?,CURRENT_TIMESTAMP+INTERVAL '10 minutes')",
        (digest(state), encrypt_session(verifier), nonce),
        commit=True,
    )
    url = "https://oauth.telegram.org/auth?" + urlencode(
        dict(
            client_id=client_id,
            redirect_uri=origin(request) + "/auth/callback",
            response_type="code",
            scope="openid profile",
            state=state,
            nonce=nonce,
            code_challenge=b64(hashlib.sha256(verifier.encode()).digest()),
            code_challenge_method="S256",
        )
    )
    result = web.Response(status=302, headers={"Location": url})
    result.set_cookie(
        "number_oauth",
        state,
        max_age=600,
        httponly=True,
        secure=origin(request).startswith("https://"),
        samesite="Lax",
        path="/auth/",
    )
    return result


async def exchange_code(client_id, secret, code, redirect_uri, verifier):
    async with aiohttp.ClientSession(timeout=aiohttp.ClientTimeout(total=20)) as client:
        async with client.post(
            "https://oauth.telegram.org/token",
            auth=aiohttp.BasicAuth(client_id, secret),
            data=dict(
                grant_type="authorization_code",
                code=code,
                redirect_uri=redirect_uri,
                client_id=client_id,
                code_verifier=verifier,
            ),
        ) as result:
            if result.status != 200:
                raise web.HTTPUnauthorized(text="تعذر إتمام الدخول إلى تيليجرام.")
            return await result.json()


async def callback(request):
    state = request.query.get("state", "")
    if (
        not state
        or not hmac.compare_digest(state, request.cookies.get("number_oauth", ""))
        or len(request.query.get("code", "")) > 4096
    ):
        raise web.HTTPUnauthorized(text="جلسة الدخول غير صالحة.")
    row = await query(
        "UPDATE platform_oauth SET consumed=TRUE WHERE state_hash=? AND NOT consumed AND expires_at>CURRENT_TIMESTAMP RETURNING verifier,nonce",
        (digest(state),),
        commit=True,
        fetchone=True,
    )
    if not row:
        raise web.HTTPUnauthorized(text="انتهت محاولة الدخول أو استُخدمت سابقًا.")
    from helpers import decrypt_session, create_user

    client_id = os.getenv("TELEGRAM_LOGIN_CLIENT_ID", "").strip()
    secret = os.getenv("TELEGRAM_LOGIN_CLIENT_SECRET", "").strip()
    data = await exchange_code(
        client_id, secret, request.query.get("code", ""), origin(request) + "/auth/callback", decrypt_session(row[0])
    )
    user = await validate_id_token(data.get("id_token"), client_id, row[1])
    await create_user(user)
    previous = request.cookies.get(COOKIE, "")
    if previous:
        await query("DELETE FROM platform_sessions WHERE token_hash=?", (digest(previous),), commit=True)
    token, _ = await issue(user)
    result = web.Response(status=302, headers={"Location": "/app"})
    result.del_cookie("number_oauth", path="/auth/")
    set_cookie(result, request, token)
    return result


async def bootstrap(request):
    from miniapp_server import validate_init_data, TOKEN_KEY, check_identity, response, throttle

    same_origin(request)
    raw = request.headers.get("X-Telegram-Init-Data", "")
    try:
        user = validate_init_data(raw, request.app[TOKEN_KEY])
    except (ValueError, TypeError, json.JSONDecodeError) as exc:
        raise web.HTTPUnauthorized(text=str(exc)) from None
    if not throttle(user):
        raise web.HTTPTooManyRequests(text="طلبات كثيرة؛ انتظر قليلاً.")
    await check_identity(user)
    previous = request.cookies.get(COOKIE, "")
    if previous:
        await query("DELETE FROM platform_sessions WHERE token_hash=?", (digest(previous),), commit=True)
    token, csrf = await issue(user, init_data=raw)
    result = response({"ok": True, "id": user, "csrf": csrf})
    set_cookie(result, request, token)
    return result


async def logout(request):
    from miniapp_server import response

    await query(
        "DELETE FROM platform_sessions WHERE token_hash=? AND user_id=?",
        (digest(request.cookies.get(COOKIE, "")), request["user"]),
        commit=True,
    )
    result = response({"ok": True})
    result.del_cookie(COOKIE, path="/")
    return result
