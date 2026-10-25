"""Restore only to a database created by this invocation, never in-place."""
import os
from pathlib import Path
import secrets
import shutil
import subprocess

import psycopg
from psycopg import sql

import operations
import workflow
from utils.asyncio_compat import to_thread


def restore_into_disposable_database(dump_path, admin_url):
    if not admin_url:
        raise ValueError('اضبط RESTORE_TEST_ADMIN_URL لاتصال الصيانة في خادم الاختبار.')
    root = Path(operations.BACKUP_DIR).resolve()
    path = Path(dump_path).resolve()
    if root not in path.parents or path.suffix != '.dump' or not path.is_file():
        raise ValueError('ملف النسخة غير متاح داخل مجلد النسخ المسموح.')
    dump_tool = Path(operations.find_pg_dump())
    candidate = dump_tool.with_name('pg_restore.exe' if dump_tool.suffix.lower() == '.exe' else 'pg_restore')
    restore_tool = str(candidate) if candidate.is_file() else shutil.which('pg_restore')
    if not restore_tool:
        raise ValueError('أداة pg_restore غير متوفرة.')
    target = 'shahem_restore_test_' + secrets.token_hex(10)
    environment = operations.backup_connection_environment(admin_url)
    # PGDATABASE is explicitly replaced with our newly created, unique target.
    environment['PGDATABASE'] = target
    created = False
    with psycopg.connect(admin_url, autocommit=True, connect_timeout=5) as admin:
        try:
            admin.execute(sql.SQL('CREATE DATABASE {} TEMPLATE template0').format(sql.Identifier(target)))
            created = True
            subprocess.run([restore_tool, '--no-password', '--exit-on-error', '--single-transaction',
                            '--no-owner', '--no-privileges', f'--dbname={target}', str(path)],
                           stdin=subprocess.DEVNULL, capture_output=True, check=True,
                           env=environment, timeout=600)
            with psycopg.connect(admin_url, dbname=target, connect_timeout=5) as restored:
                if restored.info.dbname != target:
                    raise RuntimeError('Unexpected restore target')
                counts = {}
                for table in ('users','accounts','stored_accounts','balance_ledger'):
                    counts[table] = restored.execute(sql.SQL('SELECT COUNT(*) FROM {}').format(sql.Identifier(table))).fetchone()[0]
                return counts
        finally:
            if created:
                # No wildcard, production name, --clean, or user-supplied target.
                admin.execute(sql.SQL('DROP DATABASE {}').format(sql.Identifier(target)))


@workflow.tracked('restore', cancellable=False)
async def verify_backup(user_id, backup_id):
    admin_url = os.getenv('RESTORE_TEST_ADMIN_URL', '')
    if not admin_url:
        raise ValueError('اضبط RESTORE_TEST_ADMIN_URL في إعدادات الخادم لاختبار الاستعادة على قاعدة مؤقتة.')
    row = await workflow.query("UPDATE backup_runs SET restore_status='running' WHERE backup_id=? AND status='success' AND restore_status<>'running' RETURNING path",
                               (backup_id,), commit=True, fetchone=True)
    if not row:
        raise ValueError('النسخة غير متاحة أو اختبارها جارٍ بالفعل.')
    try:
        counts = await to_thread(restore_into_disposable_database, row[0], admin_url)
    except Exception:
        await workflow.query("UPDATE backup_runs SET restore_status='failed',restore_details='تعذر الاستعادة؛ تحقق من خادم الاختبار والصلاحيات وإصدار الأدوات',restored_at=CURRENT_TIMESTAMP WHERE backup_id=?",
                             (backup_id,), commit=True)
        await workflow.queue_notice(user_id, f'restore-failed:{backup_id}', f'فشل اختبار استعادة النسخة #{backup_id}. قاعدة الإنتاج لم تُستخدم كهدف للاستعادة.')
        raise RuntimeError('فشل اختبار الاستعادة. راجع حالة النسخ الاحتياطي.') from None
    summary = 'نجحت الاستعادة إلى قاعدة مؤقتة وحُذفت بعد الاختبار. ' + ', '.join(f'{name}: {count}' for name,count in counts.items())
    await workflow.query("UPDATE backup_runs SET restore_status='passed',restore_details=?,restored_at=CURRENT_TIMESTAMP WHERE backup_id=?",
                         (summary,backup_id), commit=True)
    return summary
