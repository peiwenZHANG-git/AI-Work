"""Local read-only morning brief; no MCP entry point or automatic actions."""
from __future__ import annotations

from datetime import datetime, timedelta
import html
import json
import os
from pathlib import Path
import re
import tempfile
import time

MAILBOX_IDS = ('bachelor_mail', 'master_mail', 'qq_mail')
MAIL_LIMIT = 100
AUTH_CODES = {'NOT_AUTHENTICATED', 'TOKEN_EXPIRED', 'AUTH_REQUIRED',
              'IMAP_NOT_CONFIGURED', 'IMAP_AUTH_FAILED', 'FORBIDDEN', 'IDENTITY_MISMATCH'}
UNAVAILABLE_CODES = {'IMAP_NETWORK_FAILED', 'FALLBACK_REQUIRED', 'BROWSER_BACKEND_NOT_READY'}


def safe_text(value, limit=160):
    text = ' '.join(str(value or '').split())
    # Titles/subjects are untrusted metadata; never persist addresses, URLs or paths.
    text = re.sub(r'\b[A-Za-z][A-Za-z0-9+.-]*://\S+|\b[^\s@]+@[^\s@]+|[A-Za-z]:[\\/]\S+|\\\\\S+', '[已隐藏]', text)
    text = re.sub(r'(?i)\b(token|password|secret|authorization|cookie|sid)\s*[:=]\s*\S+', '[已隐藏]', text)
    return text[:limit]


def collect_mail(*, backend_factory=None):
    from .mail_backends import BackendStatus
    from .mailboxes import MAILBOX_IDENTITIES
    from .mail_summary import _backend_for_identity, _classify_important
    if backend_factory is None:
        from .mail_digest import ensure_environment
        ensure_environment()
    factory = backend_factory or _backend_for_identity
    boxes = []
    for name in MAILBOX_IDS:
        box = {'mailbox_id': name, 'status': 'unavailable', 'code': 'backend_failure',
               'today_count': None, 'important_count': None, 'count_scope': 'unknown',
               'items': []}
        try:
            result = factory(MAILBOX_IDENTITIES[name]).summarize_today(MAIL_LIMIT)
            code = result.status.value
            if result.status not in (BackendStatus.READY, BackendStatus.EMPTY_TODAY):
                box['status'] = ('auth/config error' if code in AUTH_CODES else
                                 'unavailable' if code in UNAVAILABLE_CODES else 'parser/backend failure')
                box['code'] = code if code in {s.value for s in BackendStatus} else 'backend_failure'
            else:
                emails = result.emails[:MAIL_LIMIT]
                if result.status == BackendStatus.EMPTY_TODAY and emails:
                    raise ValueError('inconsistent empty result')
                groups = _classify_important([{'display_name': name, 'emails': [
                    {'sender': '', 'subject': e.subject, 'time': ''} for e in emails]}])
                important = [item for group in groups.values() for item in group]
                box.update(status='ok', code=code, today_count=len(emails),
                           important_count=len(important),
                           count_scope='confirmed_empty' if code == 'EMPTY_TODAY' else 'bounded_metadata',
                           items=[{'subject': safe_text(item['subject'])} for item in important[:5]])
        except Exception:
            box.update(status='parser/backend failure', code='backend_failure')
        boxes.append(box)
    return boxes


def collect_downloads(now, *, inspector=None):
    from .files import inspect
    try:
        raw = (inspector or inspect)({'operation': 'list', 'path': 'Downloads',
                                     'sort': 'modified_desc', 'limit': 200})
        if raw.get('status') not in ('ok', 'partial'):
            raise ValueError('scan unavailable')
        items = []
        for entry in raw['entries']:
            if entry['type'] != 'file':
                continue
            label = entry['path']
            if (not label.startswith('Downloads/') or len(label) > 240
                    or any(p in ('', '.', '..') for p in label.split('/')[1:])
                    or any(c in label for c in '\\:')):
                raise ValueError('invalid relative path')
            modified = datetime.fromisoformat(entry['modified_time']).astimezone()
            if now - timedelta(hours=24) <= modified <= now:
                items.append({'filename': label, 'size_bytes': entry['size'],
                              'modified_time': modified.isoformat(),
                              'file_type': Path(label).suffix.lower()[:12] or '无扩展名'})
        items.sort(key=lambda e: (e['modified_time'], e['filename']), reverse=True)
        partial = bool(raw.get('partial') or raw.get('results_truncated'))
        return {'status': 'partial' if partial else 'ok', 'code': 'partial_scan' if partial else 'ok',
                'partial': partial, 'recent_count': len(items),
                'display_truncated': len(items) > 10, 'items': items[:10], 'scope': 'Downloads top level / 24h'}
    except Exception:
        return {'status': 'unavailable', 'code': 'scan_failed', 'partial': True,
                'recent_count': None, 'display_truncated': False, 'items': [],
                'scope': 'Downloads top level / 24h'}


def collect_system(*, collector=None):
    from .system_status import collect_status
    try:
        raw = (collector or collect_status)()
    except Exception:
        raw = {}
    result = {}
    fields = {'battery': ('state', 'percent', 'charging', 'ac_connected'),
              'foreground_window': ('title', 'title_truncated'), 'screen': ('primary',)}
    for component, keys in fields.items():
        part = raw.get(component, {})
        clean = {'status': 'ok' if part.get('status') == 'ok' else 'unknown'}
        for key in keys:
            if key in part:
                clean[key] = safe_text(part[key], 256) if key == 'title' else part[key]
        result[component] = clean
    disks = raw.get('disks', {})
    result['disks'] = {'status': 'ok' if disks.get('status') == 'ok' else 'unknown', 'volumes': [
        {k: v[k] for k in ('drive', 'status', 'free_bytes', 'total_bytes') if k in v}
        for v in disks.get('volumes', [])[:26]]}
    result['status'] = 'partial' if any(p['status'] != 'ok' for p in result.values()) else 'ok'
    return result


def suggestions(brief):
    items = []
    if any((b['important_count'] or 0) > 0 for b in brief['mailboxes']):
        items.append('优先查看规则识别的重要邮件。')
    if any(v.get('status') == 'ok' and v.get('total_bytes', 0) > 0
           and v['free_bytes'] / v['total_bytes'] < .1 for v in brief['system']['disks']['volumes']):
        items.append('固定磁盘可用空间低于 10%，建议检查磁盘使用情况。')
    battery = brief['system']['battery']
    if battery.get('status') == 'ok' and battery.get('percent') is not None and battery['percent'] < 20:
        items.append('电量低于 20%，建议连接电源。')
    if any(i['file_type'] == '.pdf' for i in brief['downloads']['items']):
        items.append('Downloads 有最近新增或修改的 PDF，建议查看或整理。')
    incomplete = (any(b['status'] != 'ok' for b in brief['mailboxes'])
                  or brief['downloads']['status'] != 'ok' or brief['system']['status'] != 'ok')
    if incomplete:
        items.append('部分信息不可用或扫描不完整，请查看各区块状态。')
    return (items or ['今天没有明显待处理电脑事项（基于本次可见数据）。'])[:3]


def collect_brief(*, now=None, mail_collector=None, downloads_collector=None, system_collector=None):
    now = now or datetime.now().astimezone()
    brief = {'generated_at': now.isoformat(), 'date': now.date().isoformat(),
             'mailboxes': (mail_collector or collect_mail)(),
             'downloads': (downloads_collector or collect_downloads)(now),
             'system': (system_collector or collect_system)()}
    brief['suggestions'] = suggestions(brief)
    return brief


def render_html(brief):
    esc = lambda value: html.escape(str(value))
    def listing(values):
        return '<ul>' + ''.join('<li>' + esc(v) + '</li>' for v in values) + '</ul>'
    mail = ''
    for box in brief['mailboxes']:
        count = 'unknown' if box['today_count'] is None else str(box['today_count'])
        important = 'unknown' if box['important_count'] is None else str(box['important_count'])
        mail += '<h3>' + esc(box['mailbox_id']) + '</h3><p>' + esc(
            f"{box['status']} · {box['code']} · 今日 {count} 封 · 规则重要 {important} 封") + '</p>'
        if box['count_scope'] == 'bounded_metadata':
            mail += '<p>有界元数据计数，最多检查 100 条；可能遗漏分页、截断或未解析邮件，不代表完整邮箱总量。</p>'
        mail += listing(i['subject'] for i in box['items'])
    downloads = brief['downloads']
    rows = ''.join('<tr>' + ''.join('<td>' + esc(i[k]) + '</td>' for k in
                   ('filename', 'size_bytes', 'modified_time', 'file_type')) + '</tr>' for i in downloads['items'])
    system = brief['system']
    return f'''<!doctype html><html lang="zh-CN"><meta charset="utf-8">
<meta name="viewport" content="width=device-width,initial-scale=1">
<meta http-equiv="Content-Security-Policy" content="default-src 'none'; style-src 'unsafe-inline'; base-uri 'none'; form-action 'none'">
<title>AI-Work 电脑晨报 — {esc(brief['date'])}</title>
<style>body{{font:16px/1.7 system-ui,sans-serif;max-width:1000px;margin:32px auto;padding:0 20px;background:#f4f6fa;color:#203047}}section{{background:white;padding:20px;margin:16px 0;border-radius:12px}}td,th{{text-align:left;padding:8px;border-bottom:1px solid #ddd;overflow-wrap:anywhere}}table{{width:100%;border-collapse:collapse}}pre{{white-space:pre-wrap;overflow-wrap:anywhere}}h1{{font-size:28px}}</style>
<h1>AI-Work 电脑晨报 — {esc(brief['date'])}</h1><p>{esc(brief['generated_at'])} · 本地只读观察</p>
<section><h2>1. 今日重点</h2>{listing(brief['suggestions'])}</section>
<section><h2>2. 邮箱摘要</h2>{mail}</section>
<section><h2>3. 最近下载</h2><p>{esc(downloads['status'])} · 最近24小时 · 顶层目录 · 最多10项 · 观察到 {esc(downloads['recent_count'])} 个文件</p>
<p>{'partial：不是完整扫描，计数为可见范围，不能保证最新。' if downloads['partial'] else '扫描范围完整。'}</p>
<table><tr><th>根相对文件名</th><th>字节</th><th>修改时间</th><th>类型</th></tr>{rows}</table></section>
<section><h2>4. 系统状态</h2>{''.join('<h3>' + label + '</h3><pre>' + esc(json.dumps(system[key], ensure_ascii=False, indent=2)) + '</pre>' for key, label in [('battery','电池'),('disks','固定磁盘'),('foreground_window','前台窗口'),('screen','主屏幕')])}</section>
<section><h2>5. 建议</h2>{listing(brief['suggestions'])}<p>仅提供建议，不自动执行任何动作。</p></section></html>'''


def artifact_dir():
    return Path(os.environ['LOCALAPPDATA']) / 'AI-Work' / 'computer-brief'


def atomic_write(path, text):
    """Same fsync + sibling temporary pattern as digest, without copy fallback."""
    path.parent.mkdir(parents=True, exist_ok=True)
    # Packaged desktop hosts can virtualize LocalAppData onto another volume.
    # Resolve the existing directory by handle before creating *both* names;
    # MoveFileEx otherwise resolves the new destination on the logical volume.
    import win32file
    handle = win32file.CreateFile(str(path.parent), 0, 7, None, 3, 0x02000000, None)
    try:
        parent = Path(win32file.GetFinalPathNameByHandle(handle, 0))
    finally:
        handle.Close()
    path = parent / path.name
    descriptor, temporary = tempfile.mkstemp(prefix='.' + path.name + '.', suffix='.tmp', dir=parent)
    try:
        with os.fdopen(descriptor, 'w', encoding='utf-8', newline='') as stream:
            stream.write(text)
            stream.flush()
            os.fsync(stream.fileno())
        for attempt in range(3):
            try:
                os.replace(temporary, path)
                break
            except OSError:
                if attempt == 2:
                    raise
                time.sleep(.02 * (attempt + 1))
    finally:
        if os.path.exists(temporary):
            os.unlink(temporary)  # only this invocation's owned temporary


def notify_brief(brief):
    from .mail_digest import build_toast_powershell
    import subprocess
    important = sum(b['important_count'] or 0 for b in brief['mailboxes'])
    downloads = brief['downloads']['recent_count']
    line = f"{important} 封规则重要邮件 · {downloads if downloads is not None else '未知'} 个最近下载（可见范围）"
    script = build_toast_powershell('AI-Work 电脑晨报已生成', line, '')
    # Reuse existing fixed Toast renderer; no shared digest artifact is changed.
    result = subprocess.run(['powershell', '-NoProfile', '-STA', '-Command', script],
                            capture_output=True, timeout=30)
    return result.returncode == 0


def run(*, dry_run=False, no_notify=False, output_dir=None, collector=None, notifier=None):
    started = datetime.now().astimezone().isoformat()
    attempt = {'started_at': started, 'finished_at': None, 'ok': False,
               'stage': 'collect', 'component_status': {}, 'counts': {}, 'code': 'ok'}
    directory = None
    try:
        if not dry_run:
            directory = output_dir or artifact_dir()
            atomic_write(directory / 'last-attempt.json', json.dumps(attempt))
        brief = (collector or collect_brief)()
        if dry_run:
            return {'ok': True, 'dry_run': True, 'brief': brief}
        attempt['component_status'] = {b['mailbox_id']: b['status'] for b in brief['mailboxes']}
        attempt['component_status'].update(downloads=brief['downloads']['status'], system=brief['system']['status'])
        attempt['counts'] = {b['mailbox_id']: b['today_count'] for b in brief['mailboxes']}
        attempt['counts']['downloads'] = brief['downloads']['recent_count']
        attempt['stage'] = 'artifacts'
        atomic_write(directory / 'last-attempt.json', json.dumps(attempt))
        atomic_write(directory / 'latest.json', json.dumps(brief, ensure_ascii=False, indent=2))
        atomic_write(directory / 'latest.html', render_html(brief))
        notification = 'disabled'
        if not no_notify:
            try:
                notification = 'ok' if (notifier or notify_brief)(brief) else 'degraded'
            except Exception:
                notification = 'degraded'
        attempt['component_status']['notification'] = notification
        attempt.update(ok=True, stage='complete')
    except Exception:
        attempt['code'] = 'collection_failed' if attempt['stage'] == 'collect' else 'artifact_write_failed'
    attempt['finished_at'] = datetime.now().astimezone().isoformat()
    if directory is not None:
        try:
            atomic_write(directory / 'last-attempt.json', json.dumps(attempt, indent=2))
        except Exception:
            attempt.update(ok=False, code='diagnostic_write_failed')
    return attempt
