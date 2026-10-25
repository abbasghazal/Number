"""Same-origin Telegram Mini App API; all identities come from verified initData."""
import asyncio
import hashlib
import hmac
import json
import logging
import os
import re
import time
from collections import OrderedDict
from datetime import date, datetime
from decimal import Decimal
from pathlib import Path
from urllib.parse import parse_qsl, urlsplit

from aiohttp import web
from telethon import functions, types

import business_reports
import account_controls
import commerce
import service_health
import storefront
import support_center
from config import TOKEN
from utils.privacy import mask_phone
from workflow import query

logger = logging.getLogger(__name__)
STATIC = Path(__file__).resolve().parent / 'miniapp'
_rates = OrderedDict()


def public_url():
    explicit = os.getenv('MINI_APP_URL', '').strip()
    render = os.getenv('RENDER_EXTERNAL_URL', '').strip().rstrip('/')
    url = explicit or (render + '/app' if render else '')
    if url:
        parsed = urlsplit(url)
        if parsed.scheme != 'https' or not parsed.hostname or parsed.username or parsed.password or parsed.fragment:
            raise ValueError('MINI_APP_URL يجب أن يكون رابط HTTPS صالحاً.')
    return url


async def configure_menu(client):
    url = public_url()
    if not url:
        return False
    await client(functions.bots.SetBotMenuButtonRequest(user_id=types.InputUserEmpty(),
        button=types.BotMenuButton(text='open', url=url)))
    logger.info('Mini App menu configured using %s', 'Render URL' if not os.getenv('MINI_APP_URL') else 'custom URL')
    return True


def validate_init_data(raw, token, *, now=None, ttl=900):
    if not token or not raw or len(raw) > 8192:
        raise ValueError('افتح الواجهة من داخل البوت.')
    pairs = parse_qsl(raw, keep_blank_values=True, strict_parsing=True, max_num_fields=30)
    data = dict(pairs)
    if len(data) != len(pairs):
        raise ValueError('بيانات دخول غير صالحة.')
    signature = data.pop('hash', '')
    if not re.fullmatch(r'[0-9a-fA-F]{64}', signature):
        raise ValueError('بيانات دخول غير صالحة.')
    check = '\n'.join(f'{key}={value}' for key,value in sorted(data.items()))
    key = hmac.new(b'WebAppData', token.encode(), hashlib.sha256).digest()
    expected = hmac.new(key, check.encode(), hashlib.sha256).hexdigest()
    if not hmac.compare_digest(signature.lower(), expected):
        raise ValueError('بيانات الدخول غير موثقة.')
    now = time.time() if now is None else now
    auth_date = int(data.get('auth_date', '0'))
    if not now-ttl <= auth_date <= now+30:
        raise ValueError('انتهت جلسة الواجهة؛ أغلقها وافتحها من البوت مجدداً.')
    user = json.loads(data.get('user', '{}'))
    identity = user.get('id') if isinstance(user, dict) else None
    if isinstance(identity, bool) or not isinstance(identity, int) or not 0 < identity < 2**63 or user.get('is_bot'):
        raise ValueError('هوية مستخدم غير صالحة.')
    return identity


def serialize(value):
    if isinstance(value, Decimal):
        return str(value)
    if isinstance(value, (datetime, date)):
        return value.isoformat()
    raise TypeError('unsupported response type')


def response(value, status=200):
    return web.json_response(value, status=status,headers={'Cache-Control':'no-store','X-Content-Type-Options':'nosniff'}, dumps=lambda item: json.dumps(item, default=serialize, ensure_ascii=False))


def throttle(user):
    now = time.monotonic()
    last, tokens = _rates.pop(user, (now, 30))
    tokens = min(30, tokens+(now-last)*5)
    if len(_rates) >= 4096:
        _rates.popitem(last=False)
    _rates[user] = (now, max(0, tokens-1))
    return tokens >= 1


async def check_access(user, client):
    from helpers import is_banned, get_user
    from handlers.base import user_has_license_access
    if await is_banned(user) or not (await user_has_license_access(user))[0]:
        raise web.HTTPForbidden(text='الوصول إلى الخدمة غير متاح.')
    if not await get_user(user):
        raise web.HTTPForbidden(text='أرسل /start للبوت أولاً.')
    from mandatory_subscription import verify_required_subscriptions
    subscribed, _missing_channel = await verify_required_subscriptions(client, user)
    if not subscribed:
        raise web.HTTPForbidden(text='اشترك بالقنوات المطلوبة من البوت أولاً.')


async def check_identity(user):
    from helpers import is_banned, get_user, create_user

    if await is_banned(user):
        raise web.HTTPForbidden(text='الوصول إلى الخدمة غير متاح.')
    if not await get_user(user):
        await create_user(user)


def requires_service_access(request):
    # Viewing one's workspace and contacting support do not execute a service.
    if request.path == '/api/platform/funding/task':
        return True
    return request.method not in {'GET', 'HEAD', 'OPTIONS'} and request.path not in {
        '/api/auth/logout', '/api/platform/preferences', '/api/tickets',
    } and not re.fullmatch(r'/api/ticket/\d+/message', request.path)


@web.middleware
async def security(request, handler):
    claim = None
    try:
        if request.path.startswith('/api/') and request.path not in {'/api/auth/bootstrap', '/api/public'}:
            import web_auth
            user, request['csrf'], request['auth_source'] = await web_auth.authenticate(request)
            if not throttle(user):
                raise web.HTTPTooManyRequests(text='طلبات كثيرة؛ انتظر قليلاً.')
            if request.path != '/api/auth/logout':
                await check_identity(user)
            if requires_service_access(request):
                await check_access(user, request.app[CLIENT_KEY])
            request['user'] = user
            if request.method == 'POST' and request.path.startswith('/api/platform/'):
                key = request.headers.get('X-Idempotency-Key', '')
                if not re.fullmatch(r'[A-Za-z0-9_-]{16,128}', key):
                    raise ValueError('أعد تحميل الواجهة لتوثيق الطلب.')
                payload = await body(request)
                fingerprint = hashlib.sha256(json.dumps(payload, sort_keys=True, separators=(',', ':')).encode()).hexdigest()
                inserted = await query('INSERT INTO platform_requests(user_id,request_key,route,body_hash) VALUES(?,?,?,?) ON CONFLICT DO NOTHING RETURNING request_key', (user,key,request.path,fingerprint),commit=True,fetchone=True)
                if not inserted:
                    previous = await query('SELECT route,body_hash,status,result,http_status,updated_at FROM platform_requests WHERE user_id=? AND request_key=?',(user,key),fetchone=True)
                    if previous[:2] != (request.path,fingerprint):
                        raise web.HTTPConflict(text='معرف الطلب مستخدم لبيانات مختلفة.')
                    if previous[2] == 'completed':
                        return response(json.loads(previous[3]), previous[4])
                    retryable = previous[2] in {'retryable', 'failed'}
                    if previous[2] == 'running':
                        reclaimed = await query("""UPDATE platform_requests SET status='retryable'
                            WHERE user_id=? AND request_key=? AND status='running'
                            AND updated_at<CURRENT_TIMESTAMP-INTERVAL '15 minutes' RETURNING request_key""",
                            (user,key),fetchone=True,commit=True)
                        retryable = bool(reclaimed)
                    if retryable:
                        claimed = await query("""UPDATE platform_requests SET status='running',result=NULL,
                            http_status=NULL,updated_at=CURRENT_TIMESTAMP WHERE user_id=? AND request_key=?
                            AND status IN ('retryable','failed') RETURNING request_key""",
                            (user,key),fetchone=True,commit=True)
                        if claimed:
                            claim = (user,key)
                        else:
                            raise web.HTTPConflict(text='الطلب ما زال قيد التنفيذ؛ حدّث الحالة بعد قليل.')
                    else:
                        raise web.HTTPConflict(text='الطلب ما زال قيد التنفيذ؛ حدّث الحالة بعد قليل.')
                claim = (user,key)
        result = await handler(request)
    except web.HTTPException as exc:
        result = response({'error': exc.text if exc.status < 500 else 'الخدمة غير متاحة.'}, exc.status)
    except PermissionError as exc:
        result = response({'error':str(exc)[:200]},403)
    except commerce.QuoteRefreshRequired as exc:
        result = response({'error': str(exc), 'code': 'quote_refresh'}, 409)
    except (ValueError, TypeError, ArithmeticError, KeyError, json.JSONDecodeError) as exc:
        result = response({'error': str(exc)[:200] or 'راجع بيانات الطلب.'}, 400)
    except Exception:
        logger.exception('Mini App request failed')
        result = response({'error': 'تعذر تنفيذ الطلب. حاول لاحقاً.'}, 503)
    if claim and isinstance(result,web.Response):
        await query("""UPDATE platform_requests SET status=CASE WHEN ?<500 THEN 'completed' ELSE 'retryable' END,
            result=?,http_status=?,updated_at=CURRENT_TIMESTAMP WHERE user_id=? AND request_key=?""",
            (result.status,result.text,result.status,*claim),commit=True)
    if result.status == 401 and request.path.startswith('/api/'):
        from web_auth import COOKIE
        result.del_cookie(COOKIE, path='/')
    result.headers.update({'Cache-Control': 'no-store', 'X-Content-Type-Options': 'nosniff',
                           'Referrer-Policy': 'no-referrer',
                           'Content-Security-Policy': "default-src 'self'; script-src 'self' https://telegram.org; style-src 'self'; img-src 'self' data:; connect-src 'self'; object-src 'none'; base-uri 'self'; form-action 'self'"})
    return result


TOKEN_KEY = web.AppKey('bot_token', str)
CLIENT_KEY = web.AppKey('telegram_client', object)


async def body(request):
    if request.content_type != 'application/json':
        raise ValueError('أرسل الطلب بصيغة JSON.')
    data = await request.json()
    if not isinstance(data, dict):
        raise ValueError('طلب غير صالح.')
    return data


async def api(request):
    user, path = request['user'], request.path
    if path == '/api/session':
        return response({'id':user,'csrf':request.get('csrf','')})
    if path == '/api/me':
        from account_access import is_manager
        from helpers import get_user, get_setting
        account = await get_user(user)
        pref = await query('SELECT coupon FROM commerce_preferences WHERE user_id=?', (user,), fetchone=True)
        client = request.app[CLIENT_KEY]
        return response({'id': user, 'is_account_manager':is_manager(user), 'balance': account['coins'], 'name': account.get('first_name') or 'مرحباً',
                         'bot_username': getattr(client, '_miniapp_username', None),
                         'loyalty': await commerce.loyalty_status(user), 'coupon': pref[0] if pref else None,
                         'rental_daily_price': await get_setting('rental_daily_price') or '0',
                         'rental_max_days': int(await get_setting('rental_max_days') or 30)})
    if path == '/api/catalog':
        return response(await storefront.catalog(user))
    if path.startswith('/api/country/'):
        cid = int(request.match_info['id'])
        page = int(request.query.get('page', '0'))
        rows = await storefront.country_inventory(cid, number_type=request.query.get('type'), offset=page*20, limit=21)
        return response({'items': [{'phone': r[0], 'label': mask_phone(r[0])} for r in rows[:20]], 'more': len(rows)>20})
    if path == '/api/managed-accounts':
        from account_access import is_manager,accounts
        if not is_manager(user):
            raise web.HTTPForbidden(text='صلاحية إدارة الحسابات مطلوبة.')
        page=int(request.query.get('page','0'))
        if page<0:
            raise ValueError('صفحة غير صالحة.')
        rows=await accounts(user)
        visible=rows[page*20:(page+1)*20]
        from account_health import records,session_key
        statuses=await records(session_key(a[1]) for a in visible)
        types=await query('SELECT phone,number_type FROM number_metadata WHERE phone=ANY(?)',([a[0] for a in visible],),fetchall=True) or []
        types=dict(types)
        return response({'items':[{'phone':a[0],'owner_id':a[2],'roles':a[4],'number_type':types.get(a[0]),
            'status':statuses.get(session_key(a[1]),{}).get('status','unknown'),'folder':'','label':'','tags':''} for a in visible],
            'more':len(rows)>(page+1)*20})
    if path == '/api/accounts':
        page = int(request.query.get('page', '0'))
        if page < 0:
            raise ValueError('صفحة غير صالحة.')
        rows = await query('''SELECT phone,roles,folder,label,tags FROM (
            SELECT s.phone,string_agg(DISTINCT s.role,',') roles,COALESCE(l.folder,'') folder,COALESCE(l.label,'') label,COALESCE(l.tags,'') tags
            FROM (SELECT s.phone,'مخزن' role FROM stored_accounts s WHERE s.user_id=?
                    AND NOT EXISTS(SELECT 1 FROM purchased_accounts p WHERE p.phone=s.phone AND p.user_id<>s.user_id)
                UNION SELECT phone,'مشترى' FROM purchased_accounts WHERE user_id=?
                UNION SELECT phone,'مستأجر' FROM rental_leases WHERE renter_id=? AND status='active' AND ends_at>CURRENT_TIMESTAMP) s
            LEFT JOIN account_labels l ON l.user_id=? AND l.phone=s.phone GROUP BY s.phone,l.folder,l.label,l.tags
        ) inventory ORDER BY phone LIMIT 21 OFFSET ?''', (user,user,user,user,page*20), fetchall=True) or []
        from account_access import accounts
        from account_health import records,session_key
        current=await accounts(user)
        statuses=await records(session_key(a[1]) for a in current)
        health={a[0]:statuses.get(session_key(a[1]),{}).get('status','unknown') for a in current}
        return response({'items': [{'phone': r[0], 'roles': r[1], 'folder': r[2], 'label': r[3], 'tags': r[4], 'status':health.get(r[0],'unknown')} for r in rows[:20]], 'more': len(rows)>20})
    if path == '/api/rentals':
        return response(await account_controls.rentals(user,int(request.query.get('page','0'))))
    if path.startswith('/api/account/') and request.method == 'GET':
        return response(await account_controls.account_status(user,request.match_info['phone']))
    if path == '/api/statement':
        period = request.query.get('period', 'month')
        file = await business_reports.statement_csv(user, period)
        return web.Response(body=file.getvalue(), content_type='text/csv',
                            headers={'Content-Disposition': 'attachment; filename="wallet-statement.csv"'})
    if path == '/api/activity':
        rows = await query('''SELECT entry_id,created_at,operation,amount,balance_after FROM balance_ledger
            WHERE user_id=? ORDER BY entry_id DESC LIMIT 30''', (user,), fetchall=True) or []
        return response([{'id': r[0], 'date': r[1], 'operation': r[2], 'amount': r[3], 'balance': r[4]} for r in rows])
    if path == '/api/tickets' and request.method == 'GET':
        page = int(request.query.get('page', '0'))
        rows = await support_center.listing(user, page)
        return response({'items': [{'id': r[0], 'status': r[2], 'assigned_to': r[3], 'priority': r[4]} for r in rows[:20]], 'more': len(rows)>20})
    if path.startswith('/api/ticket/') and request.method == 'GET':
        return response(await support_center.detail(user, int(request.match_info['id']), int(request.query.get('page', '0'))))
    data = await body(request)
    if path.startswith('/api/account/'):
        phone = request.match_info['phone']
        if path.endswith('/labels'):
            await account_controls.save_labels(user,phone,data.get('label',''),data.get('folder',''),data.get('tags',''))
        elif path.endswith('/health'):
            await account_controls.request_check(request.app[CLIENT_KEY],user,phone)
        else:
            raise web.HTTPNotFound()
        return response({'ok':True})
    if path == '/api/renewal/quote':
        from platform_services import renewal_quote
        key,amount,end = await renewal_quote(user,int(data['lease_id']),int(data['days']))
        promotion = await query('''SELECT c.base,c.discount,c.coupon FROM commerce_quotes c
            JOIN rental_renewal_quotes r ON r.promotion_quote=c.quote_id WHERE r.quote_id=? AND r.user_id=?''',
            (key,user),fetchone=True)
        status = await commerce.loyalty_status(user)
        return response({'quote_id':key,'base':promotion[0],'discount':promotion[1],'coupon':promotion[2],
                         'amount':amount,'tier':status['tier'],'expected_end':end})
    if path == '/api/renewal/confirm':
        from platform_services import renew_lease
        ok,message = await renew_lease(user,str(data['quote_id']))
        if not ok:
            raise ValueError(message)
        return response({'completed':True,'message':message})
    if path == '/api/watches/toggle':
        return response({'enabled': await storefront.toggle_watch(user, int(data['country_id']), data['service'], data['field'])})
    if path == '/api/coupon':
        return response({'coupon': await commerce.select_coupon(user, data.get('code', '-'))})
    if path == '/api/purchase/quote':
        return response(await commerce.purchase_quote(user, str(data['phone'])))
    if path == '/api/purchase/confirm':
        return response(await commerce.complete_purchase(user, str(data['quote_id'])))
    if path == '/api/rental/quote':
        return response(await commerce.rental_quote(user, int(data['count']), int(data['days'])))
    if path == '/api/rental/confirm':
        return response(await commerce.complete_rental(user, str(data['quote_id'])))
    if path == '/api/tickets':
        return response({'id': await support_center.create(user, data['message'])}, 201)
    if path.endswith('/message'):
        return response({'id': await support_center.reply(user, int(request.match_info['id']), data['message'])}, 201)
    if path.endswith('/action'):
        if data.get('action') not in {'close', 'reopen'}:
            raise ValueError('إجراء غير متاح.')
        await support_center.change(user, int(request.match_info['id']), data['action'])
        return response({'ok': True})
    raise web.HTTPNotFound()


async def health(request):
    if request.path in {'/', '/livez'}:
        return web.Response(text='ok\n')
    state = await service_health.snapshot()
    # Public probes expose component booleans, never internal errors or data.
    return response({'ok': state['ok'], 'database': state['database'], 'telegram': state['telegram'],
                     'workers': all(w['ok'] for w in state['workers'].values())}, 200 if state['ok'] else 503)


async def cleanup_expired_platform_data():
    """Bound storage for expired web credentials, OAuth artifacts and idempotency records."""
    await asyncio.gather(
        query('DELETE FROM platform_sessions WHERE expires_at<=CURRENT_TIMESTAMP', commit=True),
        query('DELETE FROM platform_oauth WHERE expires_at<=CURRENT_TIMESTAMP OR consumed', commit=True),
        query('DELETE FROM platform_handoffs WHERE expires_at<=CURRENT_TIMESTAMP OR consumed', commit=True),
        query('DELETE FROM platform_drafts WHERE expires_at<=CURRENT_TIMESTAMP', commit=True),
        query("UPDATE platform_requests SET status='retryable',updated_at=CURRENT_TIMESTAMP WHERE status='running' AND updated_at<CURRENT_TIMESTAMP-INTERVAL '15 minutes'", commit=True),
        query("DELETE FROM platform_requests WHERE created_at<CURRENT_TIMESTAMP-INTERVAL '30 days' AND status<>'running'", commit=True),
    )


async def static(request):
    name = request.match_info.get('name', 'index.html')
    if name not in {'index.html', 'app.js', 'platform.js', 'services.js','locale.js', 'style.css','platform.css'}:
        raise web.HTTPNotFound()
    return web.FileResponse(STATIC/name)


async def public_catalog(request):
    if not throttle('public:' + str(request.remote)):
        raise web.HTTPTooManyRequests(text='طلبات كثيرة؛ انتظر قليلاً.')
    rows = await storefront.catalog(None)
    return response({'authenticated': False, 'catalog': [
        {key: row[key] for key in ('id', 'name', 'code', 'price', 'number_type', 'available')}
        for row in rows
    ]})


def create_app(client=None, token=None):
    app = web.Application(middlewares=[security], client_max_size=16384)
    app[CLIENT_KEY], app[TOKEN_KEY] = client, TOKEN if token is None else token
    import web_auth
    import platform_api
    import api_service
    app.router.add_get('/auth/login',web_auth.login)
    app.router.add_get('/auth/callback',web_auth.callback)
    app.router.add_post('/api/auth/bootstrap',web_auth.bootstrap)
    app.router.add_post('/api/auth/logout',web_auth.logout)
    app.router.add_get('/api/session',api)
    app.router.add_get('/api/public',public_catalog)
    app.router.add_get('/external/v1/status', api_service.public_status)
    app.router.add_get('/external/v1/services', api_service.services)
    app.router.add_route('*', '/external/v1/platform/{tail:.*}', api_service.platform_gateway)
    app.router.add_route('*', '/external/v1/core/{tail:.*}', api_service.core_gateway)
    app.router.add_get('/api/platform/events',platform_api.events)
    app.router.add_route('*','/api/platform/{tail:.*}',platform_api.dispatch)
    async def cleanup(_app):
        import web_console
        await web_console.stop()
    app.on_cleanup.append(cleanup)
    for path in ['/', '/livez', '/health', '/healthz', '/readyz']:
        app.router.add_get(path, health)
    app.router.add_get('/app', static)
    app.router.add_get('/app/', static)
    app.router.add_get('/app/{name}', static)
    for path in ['/api/me', '/api/catalog', '/api/country/{id}', '/api/accounts','/api/managed-accounts', '/api/statement',
                 '/api/activity', '/api/tickets', '/api/ticket/{id}', '/api/rentals', '/api/account/{phone}/status']:
        app.router.add_get(path, api)
    for path in ['/api/watches/toggle', '/api/coupon', '/api/purchase/quote', '/api/purchase/confirm',
                 '/api/rental/quote', '/api/rental/confirm', '/api/tickets', '/api/ticket/{id}/message', '/api/ticket/{id}/action']:
        app.router.add_post(path, api)
    for path in ['/api/account/{phone}/labels','/api/account/{phone}/health','/api/renewal/quote','/api/renewal/confirm']:
        app.router.add_post(path,api)
    return app


async def serve():
    app = create_app(service_health.client)
    runner = web.AppRunner(app, access_log=None)
    await runner.setup()
    try:
        site = web.TCPSite(runner, os.getenv('HOST', '0.0.0.0'), int(os.getenv('PORT', '10000')))
        await site.start()
        logger.info('HTTP health and Mini App server started')
        await asyncio.Future()
    finally:
        await runner.cleanup()
