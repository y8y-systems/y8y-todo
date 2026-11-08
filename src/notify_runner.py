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
recorded in the state file as a **final** outcome: nothing is retried, and
a notification whose window has passed is dropped instead of fired late
(at-most-once).
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
    NotificationStateError,
    NotifyConfig,
    Scheduler,
    SettingsStore,
    _resolve_tz,
    notify_state_dir,
    publish_capabilities,
    redact_secrets,
)
from procguard import harden as harden_process                       # noqa: E402
import procguard                                                     # noqa: E402


def safe_text(config, text, limit=200):
    """脱敏后的错误文本 —— 凭据值不会进入 stdout/journald。"""
    getter = getattr(config, 'secrets', None)
    values = getter() if callable(getter) else ()
    return redact_secrets(text, values)[:limit]


def log_result(item, status):
    """CLI output contains delivery status, never task/recipient/error text."""
    result = {key: item[key] for key in ('channel', 'kind', 'status') if key in item}
    result.setdefault('status', status)
    if item.get('error'):
        result['error'] = 'provider_failed'
    return result


def log_report(report):
    result = {key: report[key] for key in
              ('now', 'dry_run', 'scope', 'window_minutes', 'missed_reminders')
              if key in report}
    for status in ('planned', 'sent', 'failed', 'skipped'):
        result[status] = [log_result(item, status) for item in report.get(status, [])]
    return result


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
        raise SystemExit('invalid --now value (expected ISO 8601)')
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
    # 进程一启动就把自己变成"不可 dump": 同 UID 的其他进程 (例如被攻破的
    # Web 服务) 就读不到 /proc/<pid>/environ 里的 LINE token / API key,
    # 也读不到 /proc/<pid>/mem。命令行与进程状态照常可见, systemd 的监控
    # 与排查不受影响。详见 src/procguard.py 与 docs/deployment.md §5.2。
    harden_process()
    args = parse_args(argv if argv is not None else sys.argv[1:])
    config = NotifyConfig()
    if args.data_dir:
        config.data_dir = os.path.abspath(os.path.expanduser(args.data_dir))
    tz = _resolve_tz(config.timezone)
    now = resolve_now(args.now, tz)

    if args.test:
        results = [log_result(item, item['status']) for item in send_test(config, args.test)]
        if args.json:
            print(json.dumps({'results': results}, ensure_ascii=False))
        else:
            for item in results:
                print(f'  {item["channel"]}: {item["status"]}'
                      + (f' ({item["error"]})' if item.get('error') else ''))
        return 0 if all(r['status'] in ('sent', 'unavailable') for r in results) else 1

    scheduler = Scheduler(config.data_dir, config, now=now, tz=tz)

    # 能力表: 让 Web 端知道"谁能用哪个渠道" —— 不含任何凭据。
    # 三个通知 JSON 都在专用状态目录里 (docs/deployment.md §5.2)。
    state_dir = notify_state_dir(config.data_dir)
    settings_store = SettingsStore(os.path.join(state_dir, 'notification_settings.json'))
    try:
        publish_capabilities(os.path.join(state_dir, CHANNELS_FILE),
                             config, scheduler.providers, users=settings_store.all().keys())
    except OSError as exc:
        # 状态目录写不了 —— 能力表发不出去, Web 端看不到渠道。
        # 这不是"凭据错了", 是部署没配好: 明确说出来, 别让运维去猜。
        print(f'[notify] capability_publish_failed errno={exc.errno}',
              file=sys.stderr)
        return 1

    try:
        report = scheduler.run(dry_run=args.dry_run)
    except NotificationStateError as exc:
        # fail closed: 记不下投递状态就不发 —— 否则每次 tick 都会重发。
        print(f'[notify] state_unavailable type={type(exc).__name__}', file=sys.stderr)
        return 1

    report = log_report(report)
    if args.json:
        print(json.dumps(report, ensure_ascii=False, indent=2))
    else:
        mode = 'dry-run' if args.dry_run else 'run'
        # dumpable= 是给运维看的加固状态标记: 0 = 已加固 (同 UID 读不到
        # environ), 1 = 未加固 (调试逃生阀开着), None = 取不到。
        # journal 里 grep 一下就能确认加固在生效。
        dumpable = procguard.dumpable()
        print(f'[notify] {mode} at {report["now"]} '
              f'notify={"on" if config.notify_enabled else "off"} scope={config.task_scope} '
              f'dumpable={"unknown" if dumpable is None else dumpable}')
        for item in report.get('planned', []):
            print(f'  [plan] channel={item["channel"]} kind={item["kind"]}')
        for item in report.get('sent', []):
            print(f'  [sent] channel={item["channel"]} kind={item.get("kind", "reminder")}')
        for item in report.get('failed', []):
            print(f'  [fail] channel={item["channel"]} '
                  f'kind={item.get("kind", "reminder")} status={item.get("status")} '
                  f'error={item.get("error", "")}')
        if report.get('skipped'):
            print(f'  [skip] count={len(report["skipped"])}')
        # 错过的单点提醒只报个数 —— 逐条列会让 journal 被刷屏。
        missed = report.get('missed_reminders', 0)
        if missed:
            print(f'  [missed] {missed} reminder window(s) expired — not sent late')
    return 0


if __name__ == '__main__':
    try:
        sys.exit(main())
    except Exception as exc:                                   # noqa: BLE001
        # 调度器永远不该让 systemd 看到崩溃循环 —— 记录后干净退出。
        # 只记异常类型，原始正文可能包含任务、收件人或本机路径。
        print(f'[notify] fatal error: {type(exc).__name__}',
              file=sys.stderr)
        sys.exit(1)
