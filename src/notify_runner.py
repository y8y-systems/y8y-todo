#!/usr/bin/env python3
"""Notification scheduler entry point — one shot, then exit.

Designed to be driven by a **systemd timer** (every few minutes), not by
a long-running daemon:

    [Timer]
    OnBootSec=2min
    OnUnitActiveSec=5min

Usage:
    python3 src/notify_runner.py                 # normal run (systemd)
    python3 src/notify_runner.py --dry-run       # 只算不发送, 打印计划
    python3 src/notify_runner.py --now 2026-10-27T09:00:00   # 指定"现在"
    python3 src/notify_runner.py --test alice    # 给某个用户发一条测试通知

Exit codes:
    0  正常结束 (含"没什么要发的")
    1  运行失败 (数据目录不可读、状态文件写不了等)

The runner never raises on a provider failure — a broken LINE token must
not turn into a crash loop that fills the journal.  Delivery failures are
recorded in the state file and retried with backoff on the next tick.
"""
import argparse
import json
import os
import sys
from datetime import datetime

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

from notifications import (                                          # noqa: E402
    CHANNELS_FILE,
    Message,
    NotifyConfig,
    Scheduler,
    SettingsStore,
    _resolve_tz,
    publish_capabilities,
    redact_secrets,
)


def safe_text(config, text, limit=200):
    """脱敏后的错误文本 —— 凭据值不会进入 stdout/journald。"""
    getter = getattr(config, 'secrets', None)
    values = getter() if callable(getter) else ()
    return redact_secrets(text, values)[:limit]


def parse_args(argv):
    parser = argparse.ArgumentParser(
        prog='notify_runner',
        description='y8y Todo 通知调度器 (一次性运行, 由 systemd timer 驱动)')
    parser.add_argument('--dry-run', action='store_true',
                        help='只计算要发什么, 不发送、不写状态文件')
    parser.add_argument('--now', default='',
                        help='覆盖"现在" (ISO 8601, 例如 2026-10-27T09:00:00)')
    parser.add_argument('--data-dir', default='',
                        help='覆盖数据目录 (默认取 Y8Y_DATA_DIR)')
    parser.add_argument('--test', default='',
                        help='给指定用户发一条测试通知并退出')
    parser.add_argument('--json', action='store_true',
                        help='以 JSON 输出结果')
    return parser.parse_args(argv)


def resolve_now(raw, tz):
    if not raw:
        return None
    try:
        parsed = datetime.fromisoformat(raw)
    except ValueError:
        raise SystemExit(f'invalid --now value: {raw!r} (expected ISO 8601)')
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=tz)
    return parsed


def send_test(config, user, scheduler=None):
    """给一个用户发一条测试通知 (不写投递状态)。"""
    from notifications import build_providers
    providers = scheduler.providers if scheduler else build_providers(config)
    results = []
    for name, provider in sorted(providers.items()):
        if not provider.available_for(user):
            results.append({'channel': name, 'status': 'unavailable'})
            continue
        message = Message('test', 'y8y Todo 通知测试',
                          '【y8y Todo】这是一条测试通知。\n'
                          '如果你收到了它，说明这个渠道配置正确。')
        try:
            provider.send(user, message)
        except Exception as e:                                 # noqa: BLE001
            results.append({'channel': name, 'status': 'failed',
                            'error': safe_text(config, str(e))})
        else:
            results.append({'channel': name, 'status': 'sent'})
    return results


def main(argv=None):
    args = parse_args(argv if argv is not None else sys.argv[1:])
    config = NotifyConfig()
    if args.data_dir:
        config.data_dir = os.path.abspath(os.path.expanduser(args.data_dir))
    tz = _resolve_tz(config.timezone)
    now = resolve_now(args.now, tz)

    if args.test:
        results = send_test(config, args.test)
        if args.json:
            print(json.dumps({'test': args.test, 'results': results}, ensure_ascii=False))
        else:
            for item in results:
                print(f'  {item["channel"]}: {item["status"]}'
                      + (f' ({item["error"]})' if item.get('error') else ''))
        return 0 if all(r['status'] in ('sent', 'unavailable') for r in results) else 1

    scheduler = Scheduler(config.data_dir, config, now=now, tz=tz)

    # 能力表: 让 Web 端知道"谁能用哪个渠道" —— 不含任何凭据。
    settings_store = SettingsStore(os.path.join(config.data_dir, 'notification_settings.json'))
    publish_capabilities(os.path.join(config.data_dir, CHANNELS_FILE),
                         config, scheduler.providers, users=settings_store.all().keys())

    report = scheduler.run(dry_run=args.dry_run)

    if args.json:
        print(json.dumps(report, ensure_ascii=False, indent=2))
    else:
        mode = 'dry-run' if args.dry_run else 'run'
        print(f'[notify] {mode} at {report["now"]} '
              f'{config.notification_summary()} '
              f'data_dir={config.data_dir}')
        for item in report.get('planned', []):
            print(f'  [plan] user={item["user"]} channel={item["channel"]} '
                  f'kind={item["kind"]} {item.get("task", "")}')
        for item in report.get('sent', []):
            print(f'  [sent] user={item["user"]} channel={item["channel"]} '
                  f'kind={item.get("kind", "reminder")} {item.get("task", "")}')
        for item in report.get('failed', []):
            print(f'  [fail] user={item["user"]} channel={item["channel"]} '
                  f'kind={item.get("kind", "reminder")} status={item.get("status")} '
                  f'error={item.get("error", "")}')
        for item in report.get('skipped', []):
            print(f'  [skip] {item.get("user", "-")} {item.get("reason", "")}')
    return 0


if __name__ == '__main__':
    try:
        sys.exit(main())
    except Exception as exc:                                   # noqa: BLE001
        # 调度器永远不该让 systemd 看到崩溃循环 —— 记录后干净退出。
        # 兜底脱敏: 顶层异常理论上也可能带出凭据 (例如 URL 解析失败)。
        secrets = [os.environ.get('Y8Y_NOTIFY_LINE_TOKEN', ''),
                   os.environ.get('Y8Y_NOTIFY_EMAIL_API_KEY', '')]
        print(f'[notify] fatal error: {redact_secrets(str(exc), secrets)}',
              file=sys.stderr)
        sys.exit(1)
