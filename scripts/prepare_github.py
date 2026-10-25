"""Export current Git source files, without runtime secrets or repository history."""
import hashlib
import json
import re
import subprocess
from datetime import datetime
from pathlib import Path
from zipfile import ZIP_DEFLATED, ZipFile

ROOT = Path(__file__).resolve().parents[1]
PRIVATE_DIRS = {
    '.git', '.venv', 'venv', 'node_modules', '__pycache__', '.pytest_cache',
    '.pytest-tmp', '.ruff_cache', '.mypy_cache', '.idea', '.vscode', '.tailscale',
    'database', 'backups', 'uploads', 'logs', 'exports', 'credentials', 'secrets',
    'tmp', 'temp', 'tdata', 'dist', 'build', 'htmlcov', '.tox', '.nox',
}
PRIVATE_SUFFIXES = (
    '.session', '.session-journal', '.session-wal', '.session-shm', '.session.txt',
    '.db', '.sqlite', '.sqlite3', '.log', '.dump', '.backup', '.bak', '.sql',
    '.sql.gz', '.pem', '.key', '.p12', '.pfx', '.zip', '.tar', '.tar.gz', '.tgz',
    '.pyc', '.pyo',
)
PATTERNS = {
    'Telegram bot token': r'(?<![\w])\d{7,12}:[A-Za-z0-9_-]{30,}',
    'GitHub token': r'\bgh[pousr]_[A-Za-z0-9]{30,}|\bgithub_pat_[A-Za-z0-9_]{30,}',
    'private key': r'-----BEGIN (?:RSA |EC |OPENSSH )?PRIVATE KEY-----',
    'Telegram session': r'(?<![A-Za-z0-9_])1[A-Za-z0-9_-]{340,}(?![A-Za-z0-9_-])',
}
TEXT_SUFFIXES = {'.py', '.md', '.txt', '.sh', '.json', '.yaml', '.yml', '.toml',
                 '.ini', '.html', '.css', '.js', '.cjs'}


def private_path(name):
    path = Path(name)
    parts = [part.casefold() for part in path.parts]
    if path.is_absolute() or '..' in parts or any(part in PRIVATE_DIRS for part in parts):
        return True
    leaf = parts[-1]
    return ((leaf == '.env' or leaf.startswith('.env.')) and leaf != '.env.example'
            or leaf in {'sessions.txt', '.coverage', '.feature-test-runtime-path', 'thumbs.db', '.ds_store'}
            or leaf.endswith(PRIVATE_SUFFIXES) or '.db-' in leaf or '.sqlite' in leaf)


def source_files():
    raw = subprocess.check_output(
        ['git', 'ls-files', '--cached', '--others', '--exclude-standard', '-z'], cwd=ROOT)
    names = sorted(set(filter(None, raw.decode('utf-8').split('\0'))))
    ignored = subprocess.run(
        ['git', 'check-ignore', '--no-index', '-z', '--stdin'], cwd=ROOT,
        input=('\0'.join(names)+'\0').encode('utf-8'), capture_output=True, check=False)
    if ignored.returncode not in (0, 1):
        raise RuntimeError('Could not read Git ignore rules.')
    excluded = set(ignored.stdout.decode('utf-8').split('\0'))
    return [name for name in names if name not in excluded and not private_path(name) and (ROOT/name).is_file()]


def checked_content(name):
    path = ROOT/name
    if path.is_symlink() or not path.resolve().is_relative_to(ROOT.resolve()):
        raise ValueError(f'External or linked file cannot be exported: {name}')
    content = path.read_bytes()
    try:
        text = content.decode('utf-8-sig')
    except UnicodeDecodeError:
        return content
    if path.name == '.env.example':
        secret_names = {'API_ID', 'API_HASH', 'ADMIN_ID', 'DEVELOPER_ID', 'BOT_TOKEN',
                        'SESSION_ENCRYPTION_KEY', 'DATABASE_URL', 'TON_API_KEY',
                        'TONAPI_KEY', 'RESTORE_TEST_ADMIN_URL','TELEGRAM_LOGIN_CLIENT_SECRET',
                        'TELEGRAM_LOGIN_CLIENT_ID'}
        for line in text.splitlines():
            key, separator, value = line.strip().partition('=')
            if separator and key in secret_names and value.strip():
                raise ValueError(f'{name}: {key} must be empty in the published template.')
    for rule, pattern in PATTERNS.items():
        match = re.search(pattern, text)
        if match:
            line = text[:match.start()].count('\n')+1
            raise ValueError(f'Possible {rule} in {name}:{line}; no export created.')
    if path.suffix in TEXT_SUFFIXES or path.name in {'.env.example', '.gitignore', '.dockerignore', '.gitattributes', 'Dockerfile'}:
        return text.replace('\r\n', '\n').encode('utf-8')
    return content


def main():
    files = source_files()
    if not {'main.py', 'requirements.txt', '.env.example', '.gitignore'} <= set(files):
        raise ValueError('Required source files are missing from the export.')
    # Validate all files before creating an artifact; never copy .env for scanning.
    contents = {name: checked_content(name) for name in files}
    timestamp = datetime.now().strftime('%Y%m%d-%H%M%S-%f')
    name = 'Number-main-github-'+timestamp
    output = ROOT/'dist'/name
    output.mkdir(parents=True, exist_ok=False)
    for relative, content in contents.items():
        target = output/relative
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_bytes(content)
        if target.suffix == '.sh':
            target.chmod(0o755)
    archive = output.with_suffix('.zip')
    with ZipFile(archive, 'x', ZIP_DEFLATED) as bundle:
        for relative in files:
            bundle.write(output/relative, relative)
    with ZipFile(archive) as bundle:
        if set(bundle.namelist()) != set(files) or bundle.testzip() is not None:
            raise RuntimeError('Archive verification failed.')
        if any(bundle.read(relative) != content for relative, content in contents.items()):
            raise RuntimeError('Archive contents differ from the reviewed source.')
    result = {
        'folder': str(output), 'archive': str(archive), 'files': len(files),
        'archive_sha256': hashlib.sha256(archive.read_bytes()).hexdigest(),
        'excludes': ['.env and private settings', 'sessions and account data',
                     'database dumps and backups', 'caches', '.git history'],
    }
    output.with_suffix('.manifest.json').write_text(json.dumps(result,indent=2)+'\n',encoding='utf-8')
    print(json.dumps(result,ensure_ascii=False,indent=2))


if __name__ == '__main__':
    try:
        main()
    except (ValueError, RuntimeError, subprocess.CalledProcessError) as exc:
        raise SystemExit(str(exc)) from None
