'use strict';
const numberTypes = {spam:'سبام',special:'مميز',clean:'سليم',guaranteed:'ضمان'};
const tg = window.Telegram?.WebApp;
const state = { tab: 'dashboard', csrf:'', numberType: null, me: null, catalog: [], country: null, page: 0, rentalPage: 0, ticket: null, ticketPage: 0 };
const main = document.getElementById('main');
const dialog = document.getElementById('dialog');
const dialogContent = document.getElementById('dialog-content');
let toastTimer;
let renderVersion = 0;
let authGeneration = 0;

function el(tag, attributes = {}, ...children) {
  const node = document.createElement(tag);
  for (const [key, value] of Object.entries(attributes)) {
    if (key.startsWith('on')) node.addEventListener(key.slice(2), value);
    else if (key === 'class') node.className = value;
    else node.setAttribute(key, ['placeholder','aria-label','title'].includes(key)?localized(value):value);
  }
  for (const child of children.flat()) if (child !== null && child !== undefined) node.append(child instanceof Node ? child : localized(String(child)));
  return node;
}
function toast(message) {
  const node = document.getElementById('toast');
  node.textContent = localized(message); node.hidden = false;
  clearTimeout(toastTimer); toastTimer = setTimeout(() => { node.hidden = true; }, 6000);
}
function action(fn) {
  return async event => {
    const button = event.currentTarget;
    button.disabled = true;
    try { await fn(event); } catch (error) { toast(error.message); }
    finally { button.disabled = false; }
  };
}
function button(text, fn, cls = '') { return el('button', { class: cls, onclick: action(fn), type: 'button' }, text); }
function amount(value) { return `${Number(value).toLocaleString('en-US', { minimumFractionDigits: 2, maximumFractionDigits: 2 })} $`; }
async function api(path, data, download = false, requestKey) {
  const generation = authGeneration;
  const result = await fetch(`/api/${path}`, { method: data === undefined ? 'GET' : 'POST',
    credentials:'same-origin',
    headers: { 'X-Telegram-Init-Data': tg?.initData || '', 'X-CSRF-Token':state.csrf,
      'X-Number-Client':tg?.initData || (tg?.platform && tg.platform!=='unknown') ? 'miniapp' : 'browser',
      ...(state.me || state.expectedIdentity ? {'X-Number-User':String(state.me?.id || state.expectedIdentity)} : {}),
      ...(data === undefined ? {} : { 'Content-Type': 'application/json', 'X-Idempotency-Key':requestKey||state.confirmationKey||crypto.randomUUID() }) },
    body: data === undefined ? undefined : JSON.stringify(data), cache: 'no-store' });
  const value = await (result.ok && download ? result.blob() : result.json().catch(() => ({})));
  if (generation!==authGeneration) throw new Error('تغيرت جلسة الدخول؛ أُهملت الاستجابة السابقة.');
  if (!result.ok) { const error=new Error(value.error || 'تعذر الاتصال بالخدمة.'); error.status=result.status; error.code=value.code;
    if(result.status===401 && state.me) expireIdentity(error.message);
    throw error;
  }
  return value;
}
async function reload() { [state.me, state.catalog] = await Promise.all([api('me'), api('catalog')]); }
function heading(title, sub = '') { return [el('h1', {}, title), el('p', { class: 'subtitle' }, sub)]; }
function empty(text) { return el('div', { class: 'empty-state' }, text); }
function hero() {
  return el('section', { class: 'hero' }, el('p', { class: 'eyebrow' }, '✦ SHAHEM PLATFORM ✦'), el('h2', {}, `أهلاً وسهلاً بك، ${state.me.name}`),
    el('div', { class: 'balance amount' }, amount(state.me.balance)), el('p', { class: 'meta' }, `رصيد محفظتك · مستوى ${state.me.loyalty.tier}`),
    button('عرض محفظتي', () => navigate('wallet')));
}
async function navigate(tab) {
  state.tab = tab; state.country = null; state.page = 0; state.ticket = null;
  document.querySelectorAll('[data-tab]').forEach(b => b.classList.toggle('active', b.dataset.tab === tab));
  document.body.classList.remove('menu-open');
  await render();
}
async function render() {
  if (!state.me) return loginPage();
  if (await renderPlatform()) return;
  if (state.country !== null) return renderCountry();
  if (state.tab === 'store' || state.tab === 'favorites') return renderStore();
  if (state.tab === 'accounts') return renderAccounts();
  if (state.tab === 'wallet') return renderWallet();
  if (state.tab === 'support') return state.ticket ? renderTicket() : renderSupport();
}
function renderStore() {
  renderVersion++;
  const typePicker = el('div', {class: 'toolbar'}, ...Object.entries(numberTypes).map(([key,label]) => button(`${label} (${state.catalog.filter(c=>c.number_type===key).reduce((sum,c)=>sum+c.available,0)})`, () => {state.numberType=key; renderStore();}, state.numberType===key ? 'selected' : '')));
  const favorites = state.tab === 'favorites';
  const search = el('input', { class: 'search', type: 'search', placeholder: 'ابحث عن دولة أو رمز اتصال', 'aria-label': 'بحث الدول' });
  const grid = el('div', { class: 'grid' });
  const draw = () => {
    if (!state.numberType) {grid.replaceChildren(empty('اختر نوع الرقم أولًا لعرض الدول والأرقام المتاحة.'));return;}
    const term = search.value.trim().replace(/^\+/, '').toLocaleLowerCase();
    const countries = state.catalog.filter(c => c.number_type === state.numberType && (!favorites || c.purchase.favorite || c.rental.favorite) && (!term || c.name.toLocaleLowerCase().includes(term) || String(c.code).includes(term)));
    grid.replaceChildren(...countries.map(c => el('article', { class: 'card' }, el('h3', {}, c.name), el('span', { class: 'code' }, `+${c.code}`),
      el('div', { class: 'price amount' }, amount(c.price)), el('p', { class: `stock ${c.available ? '' : 'empty'}` }, c.available ? `${c.available} رقم متاح` : 'المخزون غير متوفر حالياً'),
      button(c.available ? 'عرض الأرقام' : 'تفعيل تنبيه التوفر', async () => { state.country = c.id; state.page = 0; await renderCountry(); }))));
    if (!countries.length) grid.append(empty(favorites ? 'أضف دولاً إلى المفضلة لتظهر هنا.' : 'لا توجد دول تطابق بحثك.'));
  };
  search.addEventListener('input', draw);
  main.replaceChildren(...(favorites ? heading('المفضلة', 'الدول التي تهمك، وتنبيهات التوفر والأسعار.') : [hero(), ...heading('اختر وجهتك', 'تصفح الدول، واحصل على السعر النهائي قبل تأكيد الشراء.')]),
    el('div',{class:'toolbar'},button(favorites?'كل الدول':'عرض المفضلة',()=>navigate(favorites?'store':'favorites'),'secondary'),button('بيع أرقامي',()=>openConsole('numbers'),'secondary')),typePicker, search, grid);
  draw();
}
async function renderCountry() {
  const version = ++renderVersion;
  const countryId = state.country;
  const c = state.catalog.find(item => item.id === state.country && item.number_type === state.numberType);
  if (!c) { state.country = null; return renderStore(); }
  const inventory = await api(`country/${c.id}?type=${state.numberType}&page=${state.page}`);
  if (version !== renderVersion || state.country !== countryId) return;
  const controls = el('div', { class: 'stack' });
  for (const [service, title] of [['purchase', 'للشراء'], ['rental', 'للتأجير']]) {
    const toolbar = el('div', { class: 'toolbar' });
    for (const [field, label] of [['favorite', 'المفضلة'], ['stock_alert', 'تنبيه التوفر'], ['price_alert', 'تنبيه انخفاض السعر']]) {
      toolbar.append(button(`${c[service][field] ? '✓ ' : ''}${label}`, async () => {
        await api('watches/toggle', { country_id: c.id, service, field });
        state.catalog = await api('catalog'); await render();
      }, c[service][field] ? 'selected' : ''));
    }
    controls.append(el('div', {}, el('h3', {}, title), toolbar));
  }
  const rows = inventory.items.map(p => el('div', { class: 'row' }, el('div', {}, p.label, el('small', {}, 'شراء وتخزين في حسابك')), button('عرض السعر', () => quotePurchase(p.phone))));
  const nav = el('div', { class: 'toolbar' });
  if (state.page) nav.append(button('السابق', async () => { state.page--; await renderCountry(); }));
  if (inventory.more) nav.append(button('التالي', async () => { state.page++; await renderCountry(); }));
  main.replaceChildren(button('رجوع إلى الدول', async () => { state.country = null; renderStore(); }, 'secondary'),
    el('div', { class: 'section-head' }, el('h1', {}, c.name), el('span', { class: 'count' }, `${c.available} متاح`)),
    el('p', { class: 'subtitle' }, `سعر الشراء الأساسي ${amount(c.price)}. الخصم يُحسب في الخطوة التالية.`), controls,
    el('div', { class: 'stack' }, ...(rows.length ? rows : [empty('لا توجد أرقام متاحة الآن. فعّل تنبيه التوفر.') ])), nav,
    button('تأجير من المخزون العام', () => rentalForm(), 'full'));
}
async function quotePurchase(phone) { showQuote(await api('purchase/quote', { phone }), 'شراء وتخزين الحساب', 'purchase',()=>quotePurchase(phone)); }
function showQuote(q, title, service, refreshQuote) {
  let remaining = 300;
  const timer = el('p', { class: 'muted' }, 'العرض صالح لخمس دقائق.');
  const confirm = button(`تأكيد مقابل ${amount(q.amount)}`, async () => {
    let result;
    try { result = await api(`${service}/confirm`, { quote_id: q.quote_id }); }
    catch(error) {
      if(error.code==='quote_refresh' && refreshQuote){timer.textContent=error.message;confirm.remove();dialogContent.append(button('تحديث عرض السعر',async()=>{dialog.close();await refreshQuote();},'full'));return;}
      throw error;
    }
    dialog.close();
    toast(result.deferred ? 'حُجز الرقم لك. التسليم والخصم بعد انتهاء جميع الإيجارات؛ تابع حجزك من قسم الأرقام في البوت.' : {purchase:'اكتمل الشراء وتخزين الحساب.',rental:'اكتمل التأجير.',renewal:'تم تجديد الإيجار.'}[service]); await reload(); await navigate('accounts');
  }, 'confirm full');
  dialogContent.replaceChildren(el('h2', {}, title), ...(q.number_type ? [el('p', {}, numberTypes[q.number_type]),el('p', {}, q.country_name)] : []), el('div', { class: 'row' }, 'السعر الأساسي', el('span', { class: 'amount' }, amount(q.base))),
    el('div', { class: 'row' }, q.coupon ? `الكوبون ${q.coupon}` : `خصم مستوى ${q.tier}`, el('span', { class: 'amount positive' }, amount(q.discount))),
    el('div', { class: 'row' }, 'المطلوب', el('strong', { class: 'amount total' }, amount(q.amount))),
    q.coupon_notice ? el('p', {class:'error-note'}, q.coupon_notice) : null, timer, confirm);
  dialog.showModal();
  const countdown = setInterval(() => {
    remaining--; timer.textContent = `متبقي ${Math.floor(remaining / 60)}:${String(remaining % 60).padStart(2, '0')}`;
    if (remaining <= 0) { clearInterval(countdown); confirm.disabled = true; timer.textContent = 'انتهى العرض. أغلق النافذة واطلب عرضاً جديداً.'; }
  }, 1000);
  dialog.addEventListener('close', () => clearInterval(countdown), { once: true });
}
function rentalForm() {
  const count = el('input', { type: 'number', min: '1', max: '10000', value: '1', required: '' });
  const days = el('input', { type: 'number', min: '1', max: String(state.me.rental_max_days), value: '1', required: '' });
  const form = el('form', { class: 'form' }, el('label', {}, 'عدد الأرقام', count), el('label', {}, 'مدة التأجير بالأيام', days),
    el('p', { class: 'muted' }, `السعر الأساسي ${amount(state.me.rental_daily_price)} للرقم يومياً. الأرقام المستأجرة متاحة لخدمات البوت، دون عرض جلسات الدخول.`));
  const submit = el('button', { type: 'submit' }, 'عرض السعر النهائي'); form.append(submit);
  form.addEventListener('submit', async event => { event.preventDefault(); submit.disabled = true; try {
    const q = await api('rental/quote', { count: Number(count.value), days: Number(days.value) }); showQuote(q, 'تأكيد تأجير الأرقام', 'rental');
  } catch (error) { toast(error.message); } finally { submit.disabled = false; } });
  main.replaceChildren(...heading('تأجير الأرقام', 'اختر العدد والمدة ثم راجع السعر قبل التأكيد.'), form, button('رجوع', () => render(), 'secondary'));
}
async function renderAccounts() {
  const version = ++renderVersion;
  const result = await api(`${state.managed ? 'managed-accounts' : 'accounts'}?page=${state.page}`);
  if (version !== renderVersion || state.tab !== 'accounts') return;
  const rows = result.items.map(a => el('div', { class: 'row' }, el('div', {}, el('strong', { class: 'amount' }, `+${a.phone}`),
    el('small', {}, [a.label, a.folder, a.owner_id ? `المالك: ${a.owner_id}` : '', numberTypes[a.number_type]].filter(Boolean).join(' · '))),
    el('div', {}, el('span', { class: 'pill' }, a.roles),el('span',{class:'pill'},({healthy:'سليم',unknown:'غير مفحوص',revoked:'جلسة ملغاة',banned:'محظور',deleted:'محذوف',restricted:'مقيّد',invalid:'جلسة غير صالحة',mismatch:'هوية غير مطابقة'})[a.status||'unknown']||a.status), button('إدارة', () => manageAccount(a)))));
  const nav = el('div', { class: 'toolbar' });
  if (state.page) nav.append(button('السابق', async () => { state.page--; await renderAccounts(); }));
  if (result.more) nav.append(button('التالي', async () => { state.page++; await renderAccounts(); }));
  main.replaceChildren(...heading('حساباتي', 'نظّم حساباتك بأسماء ومجموعات خاصة، وافحص صحتها أو جدد إيجاراتك.'),
    el('div',{class:'toolbar'},button('إضافة حسابات',()=>openConsole('import')),button('أدوات الحسابات',()=>openConsole('accounts'),'secondary'),button('الأتمتة والسجلات',()=>openConsole('automation'),'secondary')),
    ...(state.me.is_account_manager ? [button(state.managed ? 'عرض حساباتي' : 'الحسابات المصرح بإدارتها', async () => {state.managed=!state.managed;state.page=0;await renderAccounts();})] : []),
    button('إيجاراتي والتجديد', async () => { state.rentalPage = 0; await renderRentals(); }, 'secondary full'),
    el('div', { class: 'stack' }, ...(rows.length ? rows : [empty('لم تضف حسابات بعد. تصفح المتجر للبدء.')])), nav);
}
function manageAccount(account) {
  const label = el('input', { value: account.label || '', maxlength: '60' });
  const folder = el('input', { value: account.folder || '', maxlength: '40' });
  const tags = el('input', { value: account.tags || '', maxlength: '160' });
  const form = el('form', { class: 'form' }, el('label', {}, 'الاسم الداخلي', label), el('label', {}, 'المجموعة', folder), el('label', {}, 'الوسوم', tags));
  const submit = el('button', { type: 'submit' }, 'حفظ التنظيم'); form.append(submit);
  form.addEventListener('submit', async event => { event.preventDefault(); submit.disabled = true; try {
    await api(`account/${encodeURIComponent(account.phone)}/labels`, { label:label.value, folder:folder.value, tags:tags.value });
    dialog.close(); toast('تم حفظ التنظيم الداخلي.'); await render();
  } catch(error) { toast(error.message); } finally { submit.disabled = false; } });
  const status = el('p', { class: 'muted' }, 'اعرض آخر نتيجة فحص محفوظة أو اطلب فحصاً جديداً.');
  const labels = {healthy:'سليم',unknown:'غير مفحوص',revoked:'جلسة ملغاة',banned:'محظور',deleted:'محذوف',restricted:'مقيّد',invalid:'جلسة غير صالحة',mismatch:'هوية غير مطابقة'};
  dialogContent.replaceChildren(el('h2', { class:'amount' }, `+${account.phone}`), el('p', { class:'muted' }, 'هذه الأسماء خاصة بك ولا تغير ملف الحساب في تيليجرام.'), form, status,
    el('div', {class:'toolbar'},button('آخر فحص', async () => {
      const records = await api(`account/${encodeURIComponent(account.phone)}/status`);
      status.textContent = records.map(r => `${labels[r.status] || r.status}${r.checked_at ? ' · '+r.checked_at.replace('T',' ').slice(0,16) : ''}`).join(' / ');
    }), button('فحص الآن', async () => { await api(`account/${encodeURIComponent(account.phone)}/health`, {}); toast('بدأ الفحص؛ ستصلك النتيجة في البوت.'); })));
  dialog.showModal();
}
async function renderRentals() {
  const version = ++renderVersion;
  const result = await api(`rentals?page=${state.rentalPage}`);
  if(version !== renderVersion || state.tab !== 'accounts') return;
  const rows = result.items.map(lease => {
    const remaining = lease.remaining_seconds;
    const detail = el('div', {}, el('strong', {class:'amount'}, `+${lease.phone}`),
      el('small', {}, `#${lease.id} · ينتهي ${lease.ends_at.replace('T',' ').slice(0,16)}`),
      el('small', {}, `${Math.floor(remaining/86400)} يوم و${Math.floor(remaining/3600)%24} ساعة متبقية`));
    return el('div', {class:'row'},detail, lease.status === 'active' && remaining > 0 ? button('تجديد', () => renewalForm(lease)) : el('span',{class:'pill'},'منتهي'));
  });
  const nav = el('div',{class:'toolbar'});
  if(state.rentalPage) nav.append(button('السابق',async () => {state.rentalPage--; await renderRentals();}));
  if(result.more) nav.append(button('التالي',async () => {state.rentalPage++; await renderRentals();}));
  main.replaceChildren(...heading('إيجاراتي والتجديد','راجع الأيام الإضافية والسعر النهائي قبل تأكيد التجديد.'),
    el('div',{class:'stack'},...(rows.length ? rows : [empty('لا توجد إيجارات مسجلة.')])),nav,button('رجوع إلى حساباتي', () => renderAccounts(),'secondary full'));
}
function renewalForm(lease) {
  const days = el('input',{type:'number',min:'1',max:String(state.me.rental_max_days),value:'1',required:''});
  const form = el('form',{class:'form'},el('label',{},'الأيام الإضافية',days));
  const submit = el('button',{type:'submit'},'عرض تكلفة التجديد'); form.append(submit);
  form.addEventListener('submit',async event => {event.preventDefault(); submit.disabled=true; try {
    const quote = await api('renewal/quote',{lease_id:lease.id,days:Number(days.value)});
    showQuote(quote,'تجديد الإيجار','renewal');
  } catch(error) {toast(error.message);} finally {submit.disabled=false;} });
  main.replaceChildren(...heading(`تجديد الإيجار #${lease.id}`,'تُضاف المدة إلى موعد الانتهاء الحالي.'),form,button('رجوع',() => renderRentals(),'secondary'));
}
function openBot(section) {
  const username = state.me.bot_username;
  if(!username || !/^[A-Za-z0-9_]{5,32}$/.test(username)) {toast('افتح قسم الرصيد من محادثة البوت.'); return;}
  const link=`https://t.me/${username}?start=${section}`;
  if(tg?.openTelegramLink) {tg.openTelegramLink(link);tg.close();} else window.open(link,'_blank','noopener,noreferrer');
}
const operations = { number_purchase: 'شراء رقم', number_rental: 'تأجير أرقام', rental_renewal: 'تجديد إيجار',
  payment_completed: 'شحن الرصيد', transfer_sent: 'تحويل صادر', transfer_received: 'تحويل وارد', withdrawal_requested: 'سحب',
  withdrawal_refund: 'استرداد سحب', referral_reward: 'مكافأة إحالة', admin_credit: 'إضافة إدارية', admin_debit: 'خصم إداري',
  promotion_purchase:'طلب رشق',promotion_refund:'استرداد رشق',number_sale:'بيع رقم',account_sale_credit:'بيع حساب',auction_purchase:'شراء في مزاد',auction_sale:'بيع في مزاد',username_auction_purchase:'شراء يوزر في مزاد',username_auction_sale:'بيع يوزر في مزاد',balance_set:'تعديل إداري للرصيد',refund:'استرداد مبلغ'};
async function renderWallet() {
  const version = ++renderVersion;
  const activity = await api('activity');
  if (version !== renderVersion || state.tab !== 'wallet') return;
  const code = el('input', { value: state.me.coupon || '', placeholder: 'رمز الكوبون', maxlength: '32', 'aria-label': 'رمز الكوبون' });
  const form = el('form', { class: 'form' }, el('h3', {}, 'الكوبونات والولاء'),
    el('p', { class: 'muted' }, `مستواك ${state.me.loyalty.tier} · خصم ${state.me.loyalty.percent}% · إنفاق مؤهل ${amount(state.me.loyalty.spent)}. يُطبّق الخصم الأكبر دون جمع الخصومات.`), code);
  const apply = el('button', { type: 'submit' }, 'حفظ الكوبون'); form.append(apply);
  form.addEventListener('submit', async event => { event.preventDefault(); apply.disabled = true; try {
    await api('coupon', { code: code.value.trim() || '-' }); await reload(); toast('تم تحديث الكوبون.'); await render();
  } catch (error) { toast(error.message); } finally { apply.disabled = false; } });
  const period = el('select', { 'aria-label': 'فترة كشف الحساب' }, ...[['week', 'آخر أسبوع'], ['month', 'آخر شهر'], ['quarter', 'آخر 90 يوماً'], ['all', 'كل الحركات']].map(([v,t]) => el('option', { value: v }, t)));
  const exportButton = button('تحميل كشف CSV', async () => {
    const blob = await api(`statement?period=${period.value}`, undefined, true);
    const link = el('a', { href: URL.createObjectURL(blob), download: 'wallet-statement.csv' });
    document.body.append(link); link.click(); link.remove(); setTimeout(() => URL.revokeObjectURL(link.href), 10000);
  });
  const rows = activity.map(a => el('div', { class: 'row' }, el('div', {}, operations[a.operation] || 'حركة رصيد', el('small', {}, `#${a.id} · ${a.date.replace('T', ' ').slice(0,16)}`)),
    el('span', { class: `amount ${Number(a.amount) >= 0 ? 'positive' : 'negative'}` }, `${Number(a.amount) >= 0 ? '+' : ''}${amount(a.amount)}`)));
  main.replaceChildren(hero(),el('div',{class:'toolbar'},button('شحن الرصيد',() => openConsole('topup')),button('تحويل أو سحب',() => openConsole('wallet'),'secondary')), form, el('div', { class: 'toolbar' }, period, exportButton), el('div', { class: 'section-head' }, el('h2', {}, 'آخر الحركات')),
    el('div', { class: 'stack' }, ...(rows.length ? rows : [empty('لا توجد حركات مسجلة بعد.')])));
}
const statuses = { open: 'مفتوحة', closed: 'مغلقة', in_progress: 'قيد المتابعة' };
async function renderSupport() {
  const version = ++renderVersion;
  const result = await api(`tickets?page=${state.page}`);
  if (version !== renderVersion || state.tab !== 'support' || state.ticket) return;
  const message = el('textarea', { placeholder: 'اكتب تفاصيل طلب الدعم…', maxlength: '3000', required: '', 'aria-label': 'رسالة الدعم' });
  const form = el('form', { class: 'form' }, el('h3', {}, 'تذكرة جديدة'), message);
  const send = el('button', { type: 'submit' }, 'إرسال الطلب'); form.append(send);
  form.addEventListener('submit', async event => { event.preventDefault(); send.disabled = true; try {
    const result = await api('tickets', { message: message.value }); state.ticket = result.id; state.ticketPage = 0; await renderTicket();
  } catch (error) { toast(error.message); } finally { send.disabled = false; } });
  const rows = result.items.map(t => el('div', { class: 'row' }, el('div', {}, `التذكرة #${t.id}`, el('small', {}, statuses[t.status] || t.status)),
    button('المتابعة', async () => { state.ticket = t.id; state.ticketPage = 0; await renderTicket(); })));
  const nav = el('div', { class: 'toolbar' });
  if (state.page) nav.append(button('السابق', async () => { state.page--; await renderSupport(); }));
  if (result.more) nav.append(button('التالي', async () => { state.page++; await renderSupport(); }));
  main.replaceChildren(...heading('الدعم', 'أرسل طلبك وتابع المحادثة وحالة التذكرة.'), form, el('h2', {}, 'تذاكري'),
    el('div', { class: 'stack' }, ...(rows.length ? rows : [empty('لا توجد تذاكر بعد.')])), nav);
}
async function renderTicket() {
  const version = ++renderVersion;
  const ticketId = state.ticket;
  const t = await api(`ticket/${state.ticket}?page=${state.ticketPage}`);
  if (version !== renderVersion || state.tab !== 'support' || state.ticket !== ticketId) return;
  const content = [button('رجوع إلى التذاكر', async () => { state.ticket = null; await renderSupport(); }, 'secondary'),
    el('div', { class: 'section-head' }, el('h2', {}, `التذكرة #${t.id}`), el('span', { class: 'pill' }, statuses[t.status] || t.status)),
    ...t.messages.map(([actor,text,staff,date]) => el('section', { class: 'message' }, el('p', { class: 'muted' }, `${staff ? 'الدعم' : 'العميل'} · ${date.replace('T',' ').slice(0,16)}`), el('p', {}, text)))];
  const nav = el('div', { class: 'toolbar' });
  if (t.more) nav.append(button('رسالة أقدم', async () => { state.ticketPage++; await renderTicket(); }));
  if (state.ticketPage) nav.append(button('رسالة أحدث', async () => { state.ticketPage--; await renderTicket(); }));
  content.push(nav);
  if (t.status !== 'closed') {
    const message = el('textarea', { maxlength: '3000', required: '', placeholder: 'أضف رسالة…', 'aria-label': 'رسالة في التذكرة' });
    const form = el('form', { class: 'form' }, message);
    const send = el('button', { type: 'submit' }, 'إرسال الرسالة'); form.append(send);
    form.addEventListener('submit', async event => { event.preventDefault(); send.disabled = true; try {
      await api(`ticket/${t.id}/message`, { message: message.value }); state.ticketPage = 0; await renderTicket();
    } catch (error) { toast(error.message); } finally { send.disabled = false; } }); content.push(form);
  }
  content.push(button(t.status === 'closed' ? 'إعادة فتح التذكرة' : 'إغلاق التذكرة', async () => {
    await api(`ticket/${t.id}/action`, { action: t.status === 'closed' ? 'reopen' : 'close' }); await renderTicket();
  }, 'secondary full'));
  main.replaceChildren(...content);
}
