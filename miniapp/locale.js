'use strict';
// Localize interface labels only; messages and user content are kept intact.
const interfaceEnglish={
  'عرض المفضلة':'View favorites','كل الدول':'All countries','بيع أرقامي':'Sell my numbers','للشراء':'For purchase','للتأجير':'For rental','تنبيه انخفاض السعر':'Price drop alert',
  'طلب رشق':'Promotion order','استرداد رشق':'Promotion refund','بيع رقم':'Number sale','بيع حساب':'Account sale','شراء في مزاد':'Auction purchase','بيع في مزاد':'Auction sale','شراء يوزر في مزاد':'Username auction purchase','بيع يوزر في مزاد':'Username auction sale','تعديل إداري للرصيد':'Administrative balance adjustment','استرداد مبلغ':'Refund',
  'إضافة حسابات':'Import accounts','أدوات الحسابات':'Account tools','الأتمتة والسجلات':'Automation & logs',
  'سبام':'Spam','مميز':'Special','سليم':'Clean','ضمان':'Guaranteed','مخزن':'Stored','مشترى':'Purchased','مستأجر':'Rented',
  'عرض محفظتي':'Open wallet','المفضلة':'Favorites','اختر وجهتك':'Choose a destination','بحث الدول':'Search countries','ابحث عن دولة أو رمز اتصال':'Search a country or calling code',
  'الدول التي تهمك، وتنبيهات التوفر والأسعار.':'Your favorite destinations and availability and price alerts.',
  'تصفح الدول، واحصل على السعر النهائي قبل تأكيد الشراء.':'Explore destinations and review the final price before purchasing.',
  'اختر نوع الرقم أولًا لعرض الدول والأرقام المتاحة.':'Choose a number category to see available countries and numbers.',
  'عرض الأرقام':'View numbers','تفعيل تنبيه التوفر':'Enable stock alert','المخزون غير متوفر حالياً':'Currently out of stock',
  'أضف دولاً إلى المفضلة لتظهر هنا.':'Add a country to your favorites to see it here.','لا توجد دول تطابق بحثك.':'No countries match your search.',
  'الشراء':'Purchase','التأجير':'Rental','تنبيه التوفر':'Stock alert','تنبيه السعر':'Price alert','شراء وتخزين في حسابك':'Purchase and store in your account',
  'عرض السعر':'Review price','السابق':'Previous','التالي':'Next','رجوع إلى الدول':'Back to countries','تأجير من المخزون العام':'Rent from available inventory',
  'شراء الرقم':'Purchase number','تأكيد الشراء':'Confirm purchase','تأكيد التأجير':'Confirm rental','تأكيد التجديد':'Confirm renewal',
  'السعر الأساسي':'Base price','الخصم':'Discount','المبلغ النهائي':'Final total','الإجمالي':'Total','تأكيد':'Confirm','رجوع':'Back',
  'تأجير الأرقام':'Rent numbers','عدد الأرقام':'Number count','مدة التأجير بالأيام':'Rental days','عرض تكلفة التأجير':'Review rental cost',
  'اختر العدد والمدة ثم راجع السعر قبل التأكيد.':'Choose the count and duration, then review the cost.',
  'حساباتي':'My accounts','نظّم حساباتك بأسماء ومجموعات خاصة، وافحص صحتها أو جدد إيجاراتك.':'Organize account labels and folders, check health and renew rentals.',
  'إدارة':'Manage','إيجاراتي والتجديد':'My rentals & renewals','رجوع إلى حساباتي':'Back to accounts','لا توجد حسابات في هذه الصفحة.':'No accounts on this page.',
  'الاسم الداخلي':'Private label','المجموعة':'Folder','الوسوم':'Tags','حفظ التنظيم':'Save organization',
  'هذه الأسماء خاصة بك ولا تغير ملف الحساب في تيليجرام.':'These private labels do not change the Telegram profile.',
  'اعرض آخر نتيجة فحص محفوظة أو اطلب فحصاً جديداً.':'View the last saved health result or request a new check.',
  'آخر فحص':'Last check','فحص الآن':'Check now','غير مفحوص':'Unchecked','جلسة ملغاة':'Revoked session','محظور':'Banned','محذوف':'Deleted',
  'مقيّد':'Restricted','جلسة غير صالحة':'Invalid session','هوية غير مطابقة':'Identity mismatch','بدأ الفحص؛ ستصلك النتيجة في البوت.':'Check started; the result will arrive in the bot.',
  'تم حفظ التنظيم الداخلي.':'Private labels saved.','راجع الأيام الإضافية والسعر النهائي قبل تأكيد التجديد.':'Review additional days and the final price before renewal.',
  'تجديد':'Renew','منتهي':'Expired','الأيام الإضافية':'Additional days','عرض تكلفة التجديد':'Review renewal cost','لا توجد إيجارات مسجلة.':'No rental history.',
  'تُضاف المدة إلى موعد الانتهاء الحالي.':'The duration is added to the current expiration date.',
  'رمز الكوبون':'Coupon code','الكوبونات والولاء':'Coupons & loyalty','حفظ الكوبون':'Save coupon','تم تحديث الكوبون.':'Coupon updated.',
  'فترة كشف الحساب':'Statement period','آخر أسبوع':'Last week','آخر شهر':'Last month','آخر 90 يوماً':'Last 90 days','كل الحركات':'All transactions',
  'تحميل كشف CSV':'Download CSV statement','حركة رصيد':'Wallet transaction','شحن الرصيد':'Top up','تحويل أو سحب':'Transfer or withdraw','آخر الحركات':'Recent transactions',
  'لا توجد حركات مسجلة بعد.':'No transactions yet.','شراء رقم':'Number purchase','تأجير أرقام':'Number rental','تجديد إيجار':'Rental renewal',
  'تحويل صادر':'Outgoing transfer','تحويل وارد':'Incoming transfer','سحب':'Withdrawal','استرداد سحب':'Withdrawal refund','مكافأة إحالة':'Referral reward','إضافة إدارية':'Admin credit','خصم إداري':'Admin debit',
  'مفتوحة':'Open','مغلقة':'Closed','قيد المتابعة':'In progress','رسالة الدعم':'Support message','تذكرة جديدة':'New ticket','إرسال الطلب':'Submit ticket','المتابعة':'View conversation',
  'الدعم':'Support','أرسل طلبك وتابع المحادثة وحالة التذكرة.':'Send a support request and follow its conversation and status.','تذاكري':'My tickets',
  'لا توجد تذاكر بعد.':'No tickets yet.','رجوع إلى التذاكر':'Back to tickets','رسالة أقدم':'Older message','رسالة أحدث':'Newer message','رسالة في التذكرة':'Ticket message',
  'إرسال الرسالة':'Send message','إعادة فتح التذكرة':'Reopen ticket','إغلاق التذكرة':'Close ticket','العميل':'Customer','اكتب تفاصيل طلب الدعم…':'Describe your support request…','أضف رسالة…':'Add a message…',
  'تعذر الاتصال بالخدمة.':'Unable to connect to the service.','تعذر تنفيذ الطلب. حاول لاحقاً.':'Unable to complete the request. Please retry later.',
  'لم يتم تأكيد الاشتراك. انضم ثم أعد التحقق.':'Membership was not confirmed. Join and verify again.',
  'هذه العملية متاحة للمطور فقط.':'This action is available to the developer only.','لا توجد مهام اشتراك جديدة متاحة حاليًا.':'No new membership tasks are currently available.'
};
function localized(value){if(document.documentElement.lang!=='en'||typeof value!=='string')return value;
  if(Object.hasOwn(interfaceEnglish,value))return interfaceEnglish[value];
  if(value.startsWith('✓ '))return '✓ '+localized(value.slice(2));
  const category=value.match(/^(سبام|مميز|سليم|ضمان) \((\d+)\)$/);if(category)return `${localized(category[1])} (${category[2]})`;
  const patterns=[[/^(\d+) رقم متاح$/,'$1 numbers available'],[/^(\d+) متاح$/,'$1 available'],[/^مرحباً (.*)$/,'Welcome, $1'],[/^تجديد الإيجار #(\d+)$/,'Renew rental #$1'],[/^التذكرة #(\d+)$/,'Ticket #$1']];
  for(const [expression,replacement] of patterns)if(expression.test(value))return value.replace(expression,replacement);
  return value;
}
