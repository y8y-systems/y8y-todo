#!/usr/bin/env python3
"""End-to-end API verification for a running y8y-todo instance.

Designed to be run by anyone against a live server (CI or a real
deployment behind a proxy). Standard library only.

⚠️ SAFETY — read before running
    The `full` phase MUTATES the target instance: it creates tasks,
    completes and restores them, advances a recurring task and writes
    persistent projects. Point it ONLY at an isolated test instance
    holding fictional data — never at a deployment with real tasks.
    The `persist` phase is read-only. As a speed bump, `full` refuses
    to run when the token identity already sees tasks on the target;
    set Y8Y_E2E_ALLOW_NONEMPTY=1 to override deliberately — note the
    full phase is not idempotent, so a re-run against a used instance
    can collide on list/project names (409).

Usage:
    e2e-api.py full    <base-url> <identity-email> <internal-token> <state-file> [data-dir]
    e2e-api.py persist <base-url> <identity-email> <internal-token> <state-file> [data-dir]

Prerequisites:
    - a running y8y-todo instance (dev mode, or production behind a
      proxy that injects the identity header)
    - <data_dir>/internal_tokens.json on that instance containing the
      token you pass, mapped to a user identity
    - fictional data only

Phases:
    full    create tasks (normal / recurring / project / list / shopping),
            complete & restore, advance a recurring task, verify the
            y8ysystems machine contract, then write the state file.
    persist after a server restart: everything from `full` must still
            be readable and correct; the machine contract must hold.

Exit code 0 = all checks passed.
"""
import json
import os
import sys
import urllib.error
import urllib.request
from datetime import date, timedelta
from urllib.parse import quote

PASS = []
FAIL = []


def guard_isolated_instance(base, token):
    """Refuse the mutating `full` phase against a non-empty instance.

    A fresh test instance has no tasks; a production one has plenty.
    If the machine-token identity already sees tasks, assume real data
    and stop before writing anything. Override with
    Y8Y_E2E_ALLOW_NONEMPTY=1 when you really mean it.
    """
    if os.environ.get('Y8Y_E2E_ALLOW_NONEMPTY') == '1':
        return
    st, body = req(base, 'GET', '/todo/api/tasks',
                   headers={'X-Internal-Token': token})
    if st != 200:
        return  # auth/connectivity problems surface in the checks below
    existing = len(body.get('tasks', [])) + len(body.get('completed_tasks', []))
    if existing:
        print(f'[abort] target instance already holds {existing} task(s) for this token.')
        print('        The full phase mutates data and is only meant for isolated')
        print('        test instances with fictional data. Re-point it at a')
        print('        throwaway instance, or set Y8Y_E2E_ALLOW_NONEMPTY=1')
        print('        to override on purpose.')
        sys.exit(3)


def check(name, ok, detail=''):
    (PASS if ok else FAIL).append(name)
    mark = 'PASS' if ok else 'FAIL'
    print(f'  [{mark}] {name}' + (f'  ({detail})' if detail and not ok else ''))
    return ok


def req(base, method, path, body=None, headers=None):
    data = json.dumps(body).encode() if body is not None else None
    r = urllib.request.Request(base + path, data=data, method=method)
    r.add_header('Content-Type', 'application/json')
    for k, v in (headers or {}).items():
        r.add_header(k, v)
    # never route through an ambient proxy (localhost!)
    opener = urllib.request.build_opener(urllib.request.ProxyHandler({}))
    try:
        with opener.open(r, timeout=20) as resp:
            raw = resp.read()
            return resp.status, json.loads(raw.decode() or '{}')
    except urllib.error.HTTPError as e:
        raw = e.read()
        try:
            return e.code, json.loads(raw.decode() or '{}')
        except json.JSONDecodeError:
            return e.code, {'_raw': raw.decode('utf-8', 'replace')}


def phase_full(base, email, token, state_file, data_dir):
    guard_isolated_instance(base, token)
    H = {'Cf-Access-Authenticated-User-Email': email}
    M = {'X-Internal-Token': token}
    today = date.today()
    state = {}

    print('== 认证边界 ==')
    st, _ = req(base, 'GET', '/todo/api/tasks')
    check('无身份请求 → 401', st == 401, f'got {st}')
    st, _ = req(base, 'GET', '/todo/api/tasks',
                headers={'Cf-Access-Authenticated-User-Email': 'attacker@evil.example'})
    check('未配置邮箱 → 401', st == 401, f'got {st}')
    st, _ = req(base, 'GET', '/todo/api/tasks', headers=M)
    check('机器 token → 200', st == 200, f'got {st}')
    st, _ = req(base, 'GET', '/todo/api/tasks', headers={'X-Internal-Token': 'wrong'})
    check('错误 token → 401', st == 401, f'got {st}')

    print('== 任务创建 ==')
    due = (today + timedelta(days=3)).isoformat()
    st, body = req(base, 'POST', '/todo/api/tasks',
                   {'title': 'E2E-普通任务', 'due': due, 'content': '端到端验证'},
                   H)
    check('创建普通任务 → 201', st == 201, f'got {st}')
    state['normal_id'] = body['id']
    state['normal_title'] = 'E2E-普通任务'
    check('任务是可见标题与存储名分离的 id', '-' in body['id'])

    st, body = req(base, 'POST', '/todo/api/tasks',
                   {'title': 'E2E-重复任务', 'due': date(today.year, today.month, 1).isoformat(),
                    'recurring': True, 'recurring_dates': [1, 15]}, H)
    check('创建重复任务 → 201', st == 201, f'got {st}')
    state['recurring_id'] = body['id']
    old_due = date(today.year, today.month, 1).isoformat()

    st, body = req(base, 'POST', '/todo/api/tasks',
                   {'title': 'E2E-项目任务', 'due': due, 'project': 'E2E项目'}, H)
    check('创建项目任务 → 201', st == 201, f'got {st}')
    state['project_id'] = body['id']

    st, body = req(base, 'POST', '/todo/api/lists', {'name': 'E2E清单'}, H)
    check('创建自定义列表 → 201', st == 201, f'got {st}')
    state['list_id'] = body['id']
    st, _ = req(base, 'POST', f"/todo/api/lists/{quote(state['list_id'])}/items",
                {'title': '清单条目'}, H)
    check('列表加条目 → 201', st == 201, f'got {st}')

    st, _ = req(base, 'POST', '/todo/api/shopping', {'title': 'E2E购物'}, H)
    check('购物别名加条目 → 201', st == 201, f'got {st}')

    st, body = req(base, 'PUT', '/todo/api/persistent-projects',
                   {'projects': ['E2E项目']}, H)
    check('持久项目写入 → 200', st == 200, f'got {st}')

    print('== 完成 / 恢复 ==')
    st, body = req(base, 'POST', f"/todo/api/tasks/{quote(state['normal_id'])}/done", headers=H)
    check('完成任务 → 200', st == 200, f'got {st}')
    archived_id = body.get('id', '')
    check('完成响应返回可解析的归档 id', archived_id.startswith('done/') and '..' not in archived_id,
          f'got {archived_id!r}')
    st, body = req(base, 'GET', '/todo/api/tasks', headers=H)
    active = [t['title'] for t in body['tasks']]
    check('完成后从活动列表消失', 'E2E-普通任务' not in active)
    check('完成后出现在已完成列表',
          'E2E-普通任务' in [t['title'] for t in body['completed_tasks']])

    st, body = req(base, 'PUT', f"/todo/api/tasks/{quote(archived_id)}",
                   {'status': 'todo'}, H)
    check('恢复任务 → 200', st == 200, f'got {st}')
    restored_id = body.get('id', '')
    check('恢复响应返回干净 id', '..' not in restored_id, f'got {restored_id!r}')
    st, body = req(base, 'GET', '/todo/api/tasks', headers=H)
    check('恢复后回到活动列表', 'E2E-普通任务' in [t['title'] for t in body['tasks']])

    print('== 重复任务推进 ==')
    st, body = req(base, 'POST', f"/todo/api/tasks/{quote(state['recurring_id'])}/done", headers=H)
    check('完成重复任务 → 200', st == 200, f'got {st}')
    next_due = body.get('next_due', '')
    check('due 推进到下一天', next_due > old_due, f'{old_due} → {next_due}')
    st, body = req(base, 'GET', '/todo/api/tasks', headers=H)
    rec = [t for t in body['tasks'] if t['title'] == 'E2E-重复任务']
    check('推进后仍在活动列表', len(rec) == 1 and rec[0]['due'] == next_due)
    state['recurring_due'] = next_due

    print('== y8ysystems 机器契约 ==')
    st, body = req(base, 'GET', '/todo/api/tasks', headers=M)
    check('契约端点 → 200', st == 200, f'got {st}')
    for key in ('current_user', 'tasks', 'completed_tasks', 'sections'):
        check(f'契约字段 {key}', key in body)
    check('契约 sections 完整',
          set(body['sections'].keys()) == {'overdue', 'today', 'this_week', 'future', 'nodue'})
    task = [t for t in body['tasks'] if t['title'] == 'E2E-普通任务']
    check('契约可见机器用户自己的任务', len(task) == 1)
    if task:
        for key in ('id', 'title', 'due', 'due_end', 'due_time', 'created',
                    'color', 'status', 'recurring', 'project', 'owner', 'body'):
            check(f'任务字段 {key}', key in task[0])

    print('== 磁盘布局 ==')
    if data_dir:
        todo_dir = os.path.join(data_dir, 'todo')
        check('数据目录含 todo/', os.path.isdir(todo_dir))
        check('数据目录含 done/', os.path.isdir(os.path.join(data_dir, 'done')))
        check('数据目录含 lists/', os.path.isdir(os.path.join(data_dir, 'lists')))
        check('项目任务落在项目子目录',
              os.path.isfile(os.path.join(todo_dir, 'E2E项目',
                                          os.path.basename(state['project_id']))))
        check('frontmatter 含 title 字段',
              'title: E2E-普通任务' in open(
                  os.path.join(todo_dir, state['normal_id']), encoding='utf-8').read())

    with open(state_file, 'w', encoding='utf-8') as f:
        json.dump(state, f, ensure_ascii=False, indent=2)
    print(f'  状态已写入 {state_file}')


def phase_persist(base, email, token, state_file, data_dir):
    H = {'Cf-Access-Authenticated-User-Email': email}
    M = {'X-Internal-Token': token}
    with open(state_file, encoding='utf-8') as f:
        state = json.load(f)

    print('== 重启后数据完整 ==')
    st, body = req(base, 'GET', '/todo/api/tasks', headers=H)
    check('重启后可读 → 200', st == 200, f'got {st}')
    active = {t['title']: t for t in body['tasks']}
    done = {t['title']: t for t in body['completed_tasks']}
    check('普通任务仍在活动列表', state['normal_title'] in active)
    check('重复任务 due 仍是推进后的日期',
          active.get('E2E-重复任务', {}).get('due') == state['recurring_due'],
          f"got {active.get('E2E-重复任务', {}).get('due')}")
    check('项目任务仍在', 'E2E-项目任务' in active)
    check('归档任务仍在已完成列表', 'E2E-普通任务' in done or state['normal_title'] in active)

    st, body = req(base, 'GET', '/todo/api/lists', headers=H)
    check('自定义列表仍在', any(l['id'] == state['list_id'] for l in body['lists']))
    st, body = req(base, 'GET', f"/todo/api/lists/{quote(state['list_id'])}/items", headers=H)
    check('列表条目仍在', [i['title'] for i in body['items']] == ['清单条目'])
    st, body = req(base, 'GET', '/todo/api/shopping', headers=H)
    check('购物条目仍在', 'E2E购物' in [i['title'] for i in body['items']])
    st, body = req(base, 'GET', '/todo/api/persistent-projects', headers=H)
    check('持久项目仍在', body['projects'] == ['E2E项目'])

    st, body = req(base, 'GET', '/todo/api/tasks', headers=M)
    check('重启后机器契约仍成立', st == 200 and body.get('current_user'))
    check('重启后契约仍含任务字段',
          all(k in body['tasks'][0] for k in ('id', 'title', 'due', 'owner'))
          if body.get('tasks') else False)


def main():
    if len(sys.argv) < 6:
        print(__doc__)
        return 2
    phase, base, email, token, state_file = sys.argv[1:6]
    data_dir = sys.argv[6] if len(sys.argv) > 6 else None
    base = base.rstrip('/')
    if phase == 'full':
        phase_full(base, email, token, state_file, data_dir)
    elif phase == 'persist':
        phase_persist(base, email, token, state_file, data_dir)
    else:
        print(f'unknown phase: {phase}')
        return 2
    print(f'\n结果: {len(PASS)} 通过, {len(FAIL)} 失败')
    if FAIL:
        print('失败项:', FAIL)
        return 1
    return 0


if __name__ == '__main__':
    sys.exit(main())
