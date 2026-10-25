"""Safe self-update command for a bot deployed from a Git repository."""
import asyncio
import json
import os
import sys
import tempfile
from datetime import datetime, timezone
from pathlib import Path
from typing import Tuple

from .base import logger
from config import DEVELOPER_ID, UPDATE_BRANCH, UPDATE_REPOSITORY_URL

_update_lock = asyncio.Lock()


async def _run_command(*command, cwd=None):
    process = await asyncio.create_subprocess_exec(
        *command,
        stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.STDOUT,
        # Tests import project modules from the repository root.
        env={**os.environ, "PYTHONPATH": str(cwd or Path.cwd())},
        cwd=cwd,
    )
    output, _ = await process.communicate()
    return process.returncode, output.decode("utf-8", errors="replace").strip()


def _normalise_repository_url(url):
    """Compare GitHub remote URLs without caring about a trailing .git/slash."""
    value = url.strip().removesuffix("/").removesuffix(".git").lower()
    if value.startswith("git@") and ":" in value:
        host_path = value[4:].replace(":", "/", 1)
        return f"https://{host_path}"
    return value


def _write_update_record(project_dir, *, previous, target, status, detail=""):
    """Keep a non-secret audit record that permits an intentional rollback."""
    path = project_dir / "database" / "backups" / "last_update.json"
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps({
        "previous_commit": previous,
        "target_commit": target,
        "status": status,
        "detail": detail[:500],
        "recorded_at": datetime.now(timezone.utc).isoformat(),
    }, ensure_ascii=False, indent=2), encoding="utf-8")


async def _rollback_production(previous_commit: str) -> Tuple[bool, str]:
    """Restore the known-clean code and its pinned Python dependencies."""
    code, output = await _run_command("git", "reset", "--hard", previous_commit)
    if code:
        return False, output
    requirements = Path.cwd() / "requirements.txt"
    if requirements.is_file():
        dep_code, dep_output = await _run_command(sys.executable, "-m", "pip", "install", "-r", str(requirements))
        if dep_code:
            return False, f"Code rollback succeeded but dependency restoration failed: {dep_output}"
        output = "\n".join(part for part in (output, dep_output) if part)
    return True, output


async def _pre_restart_checks(cwd=None, python_executable=None):
    """Checks that do not start the bot or touch Telegram production state."""
    check_dir = Path(cwd) if cwd else Path.cwd()
    python_executable = python_executable or sys.executable
    code, output = await _run_command(python_executable, "-m", "compileall", "-q", ".", cwd=cwd)
    if code:
        return False, "syntax", output
    if (check_dir / "tests").is_dir():
        code, output = await _run_command(python_executable, "-m", "pytest", "-q", cwd=cwd)
        if code:
            return False, "tests", output
    return True, "", ""


async def _prepare_staging_environment(staging_dir: str) -> Tuple[bool, str, str]:
    """Install test dependencies in an isolated venv for a candidate update."""
    root = Path(staging_dir)
    environment = root / ".update-venv"
    code, output = await _run_command(sys.executable, "-m", "venv", str(environment), cwd=staging_dir)
    if code:
        return False, "venv", output
    python_name = "Scripts/python.exe" if os.name == "nt" else "bin/python"
    candidate_python = environment / python_name
    requirements = root / "requirements-dev.txt"
    if not requirements.is_file():
        requirements = root / "requirements.txt"
    code, output = await _run_command(
        str(candidate_python), "-m", "pip", "install", "--disable-pip-version-check", "-r", str(requirements), cwd=staging_dir
    )
    if code:
        return False, "dependencies", output
    return True, "", str(candidate_python)


async def github_update_handler(event):
    """Update only from the configured project repository and restart safely."""
    if event.sender_id != DEVELOPER_ID:
        return
    if _update_lock.locked():
        await event.respond("⏳ يوجد تحديث قيد التنفيذ بالفعل.")
        return

    async with _update_lock:
        project_dir = Path.cwd()
        code, inside_work_tree = await _run_command("git", "rev-parse", "--is-inside-work-tree")
        if code or inside_work_tree.strip() != "true":
            await event.respond("❌ التحديث التلقائي يتطلب أن يكون البوت مثبتًا من مستودع Git.")
            return

        await event.respond("🔄 جارٍ فحص التحديثات من GitHub…")
        code, status = await _run_command("git", "status", "--porcelain")
        if code or status:
            await event.respond("❌ توجد تعديلات محلية على الخادم؛ تم إيقاف التحديث لحماية الملفات.")
            return

        code, origin = await _run_command("git", "remote", "get-url", "origin")
        if code:
            await event.respond("❌ لا يوجد remote باسم origin للمشروع.")
            return
        if not UPDATE_REPOSITORY_URL or _normalise_repository_url(origin) != _normalise_repository_url(UPDATE_REPOSITORY_URL):
            logger.warning("Update rejected because origin does not match the configured repository")
            await event.respond("❌ مستودع origin لا يطابق المستودع المسموح في الإعدادات.")
            return

        code, branch = await _run_command("git", "branch", "--show-current")
        branch = branch.strip()
        if code or not branch or branch != UPDATE_BRANCH:
            await event.respond("❌ تعذر تحديد فرع Git الحالي.")
            return

        code, output = await _run_command("git", "fetch", "--prune", "origin", branch)
        if code:
            logger.error("Git fetch failed: %s", output)
            await event.respond("❌ تعذر الاتصال بـGitHub. راجع سجل الخدمة.")
            return

        code, before = await _run_command("git", "rev-parse", "HEAD")
        code_after, target = await _run_command("git", "rev-parse", f"origin/{branch}")
        if code or code_after:
            await event.respond("❌ تعذر قراءة نسخة المشروع من GitHub.")
            return
        if before == target:
            await event.respond("✅ البوت يعمل بالفعل بأحدث نسخة.")
            return

        code, changed_files = await _run_command("git", "diff", "--name-only", "HEAD", f"origin/{branch}")
        changed_count = len([line for line in changed_files.splitlines() if line]) if not code else 0
        _write_update_record(project_dir, previous=before, target=target, status="checking")

        requirements_changed = (await _run_command(
            "git", "diff", "--quiet", "HEAD", f"origin/{branch}", "--",
            "requirements.txt", "pyproject.toml", "poetry.lock", "Pipfile.lock"
        ))[0] == 1
        # Test the exact target in a disposable worktree. Production is still
        # untouched if compilation or tests fail here.
        with tempfile.TemporaryDirectory(prefix="bot-update-") as staging_dir:
            code, output = await _run_command("git", "worktree", "add", "--detach", staging_dir, target)
            if code:
                logger.error("Could not create update staging worktree: %s", output)
                await event.respond("❌ تعذر تجهيز نسخة اختبار التحديث.")
                return
            try:
                prepared, check_name, prepared_output = await _prepare_staging_environment(staging_dir)
                if prepared:
                    ok, check_name, output = await _pre_restart_checks(
                        cwd=staging_dir, python_executable=prepared_output
                    )
                else:
                    ok, output = False, prepared_output
            finally:
                await _run_command("git", "worktree", "remove", "--force", staging_dir)
            if not ok:
                _write_update_record(project_dir, previous=before, target=target, status="preflight_failed", detail=output)
                logger.error("Staged update %s validation failed: %s", check_name, output)
                await event.respond("❌ فشل فحص نسخة التحديث؛ لم يتم لمس النسخة الحالية.")
                return
        code, output = await _run_command("git", "merge", "--ff-only", f"origin/{branch}")
        if code:
            logger.error("Git pull failed: %s", output)
            await event.respond("❌ فشل سحب التحديث. لم يتم إعادة تشغيل البوت.")
            return

        if requirements_changed and (project_dir / "requirements.txt").is_file():
            code, output = await _run_command(sys.executable, "-m", "pip", "install", "-r", "requirements.txt")
            if code:
                logger.error("Dependency update failed: %s", output)
                rolled_back, rollback_output = await _rollback_production(before)
                status = "rolled_back" if rolled_back else "rollback_failed"
                _write_update_record(project_dir, previous=before, target=target, status=status, detail=rollback_output or output)
                await event.respond("❌ فشل تثبيت المتطلبات؛ تمت إعادة الكود إلى النسخة السابقة بأمان." if rolled_back else "❌ فشل تثبيت المتطلبات وتعذرت إعادة الكود تلقائياً؛ راجع سجل الخدمة فوراً.")
                return

        _write_update_record(project_dir, previous=before, target=target, status="ready")
        await event.respond(f"✅ تم تثبيت التحديث ({changed_count} ملفًا). سيعاد تشغيل البوت الآن خلال ثوانٍ قليلة.")
        await asyncio.sleep(1)
        await event.client.disconnect()
        os.execv(sys.executable, [sys.executable, "main.py"])
