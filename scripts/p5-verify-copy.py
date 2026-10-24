#!/usr/bin/env python3
"""p5-verify-copy.py — 真实数据副本的只读验证 (P5.5, stdlib only)。

用途 (Runbook §4/§6): 把生产数据目录复制/恢复到隔离实例后, 在**不输出
任何任务内容、不输出任何真实文件名**的前提下证明副本与原件一致、服务
契约完整。

  python3 p5-verify-copy.py <original-dir> <copy-dir> [--api BASE]
         [--identity USER --users u1,u2,...] [--diagnostic]

为什么默认不输出文件名 (独立审查问题 A): markdown 文件名可能就是任务
标题本身 — 「bob的预约.md」这样的文件名即私人内容。因此:

  - 生产模式 (默认): 缺失/多余/修改只报告**数量**、**结构分类计数**和
    **单向匿名标识** (`#` + sha256(相对路径)[:8], 不可逆, 但可跨次运行
    比对同一差异集)。永不输出相对路径、标题、正文或令牌。
  - `--diagnostic`: 输出完整相对路径 — 仅限**虚构测试环境**, 且必须同时
    设置 `Y8Y_FICTIONAL_DATA=1` (双重显式确认); 否则以退出码 2 拒绝。
    生产机上误加 --diagnostic 也不会泄露 (没有那个环境变量)。

身份验证 (独立审查问题 B): --api 必须配 --identity。Token 按**标识精确
匹配** (不随机挑选), 并校验:
  - 响应 current_user == 预期标识 (身份不符即 FAIL, 不靠 HTTP 200 放行)
  - 契约字段齐全; tasks/completed 数量与按文件的可见性模型推导的预期
    一致 — 空列表但预期非零 = 明确 FAIL (防空列表假阳性)
  - 软隐藏 (owner 不在用户表) 与 shared 任务按服务端同一规则计入预期

退出码: 0 = 全部通过; 1 = 存在不一致; 2 = 用法/环境错误。
"""
import hashlib
import json
import os
import re
import sys
import urllib.error
import urllib.request

TOKEN_FILE = 'internal_tokens.json'


def sha256_file(path):
    h = hashlib.sha256()
    with open(path, 'rb') as f:
        for chunk in iter(lambda: f.read(65536), b''):
            h.update(chunk)
    return h.hexdigest()


def path_id(rel):
    """相对路径的单向匿名标识: 不可逆推标题, 可跨运行比对同一差异。"""
    return '#' + hashlib.sha256(rel.encode('utf-8')).hexdigest()[:8]


def top_category(rel):
    """结构分类 (目录名/根级文件) — 结构信息, 非私人内容。"""
    head = rel.split('/')[0]
    return head + '/' if '/' in rel else '<root>'


def walk_files(root):
    """rel → sha256; .tmp 崩溃残留按约定排除 (与备份工具一致)。"""
    out = {}
    for dp, _dn, fn in os.walk(root):
        for name in fn:
            if name.endswith('.tmp'):
                continue
            p = os.path.join(dp, name)
            out[os.path.relpath(p, root)] = sha256_file(p)
    return out


_FM_RE = re.compile(r'^---\s*\n(.*?)\n---', re.DOTALL)


def read_frontmatter_fields(path, keys):
    """最小 frontmatter 读取: 只取需要的标量字段 (owner/status)。"""
    try:
        with open(path, 'r', encoding='utf-8', errors='replace') as f:
            head = f.read(4096)
    except OSError:
        return {}
    m = _FM_RE.match(head)
    if not m:
        return {}
    out = {}
    for line in m.group(1).split('\n'):
        if ':' not in line:
            continue
        k, _, v = line.partition(':')
        k = k.strip()
        if k in keys:
            v = v.strip()
            if len(v) >= 2 and v[0] == '"' and v[-1] == '"':
                try:
                    v = json.loads(v)
                except Exception:
                    pass
            out[k] = v
    return out


def expected_visible_counts(data_dir, identity, users):
    """按服务端同一套可见性规则推导 identity 应见到的任务数。

    服务端语义 (src/serve.py): owner 缺省 = 第一个配置用户; 可见 =
    owner ∈ (identity, 'shared'); 软隐藏 (owner 不在用户表且非 shared)
    对所有人不可见; list_tasks 只收 status(todo), list_completed_tasks
    只收 status(done), 都只扫 .md。
    """
    default_owner = users[0] if users else identity
    tasks = completed = 0
    for folder, want in (('todo', 'todo'), ('recurring', 'todo')):
        base = os.path.join(data_dir, folder)
        for dp, _dn, fn in os.walk(base):
            for name in fn:
                if not name.endswith('.md'):
                    continue
                fm = read_frontmatter_fields(os.path.join(dp, name), ('owner', 'status'))
                if fm.get('status', 'todo') != want:
                    continue
                owner = fm.get('owner') or default_owner
                if owner in (identity, 'shared'):
                    tasks += 1
    done_root = os.path.join(data_dir, 'done')
    if os.path.isdir(done_root):
        for dp, _dn, fn in os.walk(done_root):
            for name in fn:
                if not name.endswith('.md'):
                    continue
                fm = read_frontmatter_fields(os.path.join(dp, name), ('owner', 'status'))
                if fm.get('status', 'done') != 'done':
                    continue
                owner = fm.get('owner') or default_owner
                if owner in (identity, 'shared'):
                    completed += 1
    return tasks, completed


def check(name, ok, detail=''):
    print(f'  [{"PASS" if ok else "FAIL"}] {name}' + (f'  ({detail})' if detail and not ok else ''))
    return ok


def api_get(base, path, token=None):
    req = urllib.request.Request(base + path, method='GET')
    if token:
        req.add_header('X-Internal-Token', token)
    opener = urllib.request.build_opener(urllib.request.ProxyHandler({}))
    try:
        with opener.open(req, timeout=15) as resp:
            return resp.status, resp.read()
    except urllib.error.HTTPError as e:
        return e.code, e.read()


def report_divergence(label, rels, diagnostic):
    """差异报告: 默认只给数量+分类+匿名标识; --diagnostic (虚构环境) 给路径。"""
    cats = {}
    for r in rels:
        cats[top_category(r)] = cats.get(top_category(r), 0) + 1
    cat_s = ', '.join(f'{k}:{v}' for k, v in sorted(cats.items()))
    ids = ' '.join(path_id(r) for r in sorted(rels)[:8])
    more = f' (+{len(rels) - 8})' if len(rels) > 8 else ''
    if diagnostic:
        names = ' | '.join(sorted(rels)[:8]) + more
        print(f'    {label}: {len(rels)} 个 [{cat_s}]')
        print(f'      路径 (虚构环境诊断): {names}')
        if len(rels) > 8:
            print(f'      其余 {len(rels) - 8} 个同理')
    else:
        print(f'    {label}: {len(rels)} 个 [{cat_s}]')
        print(f'      匿名标识 (不可逆; 加 --diagnostic 需虚构环境): {ids}{more}')


def main(argv):
    args = list(argv[1:])
    api_base = identity = None
    users = []
    diagnostic = False
    positional = []
    i = 0
    while i < len(args):
        a = args[i]
        if a == '--api':
            if i + 1 >= len(args):
                print('missing value for --api')
                return 2
            api_base = args[i + 1].rstrip('/')
            i += 2
        elif a == '--identity':
            if i + 1 >= len(args):
                print('missing value for --identity')
                return 2
            identity = args[i + 1]
            i += 2
        elif a == '--users':
            if i + 1 >= len(args):
                print('missing value for --users')
                return 2
            users = [u.strip() for u in args[i + 1].split(',') if u.strip()]
            i += 2
        elif a == '--diagnostic':
            diagnostic = True
            i += 1
        elif a.startswith('--'):
            print(f'unknown option: {a}')
            return 2
        else:
            positional.append(a)
            i += 1

    if diagnostic and os.environ.get('Y8Y_FICTIONAL_DATA') != '1':
        print('[FATAL] --diagnostic 输出真实文件名, 仅限虚构测试环境;')
        print('       如确在虚构环境, 设 Y8Y_FICTIONAL_DATA=1 后重试。')
        return 2
    if len(positional) != 2:
        print(__doc__)
        return 2
    original, copy = positional
    if api_base and not identity:
        print('[FATAL] --api 必须配 --identity (明确验证目标身份, 不随机选 Token)')
        return 2
    if api_base and not users:
        print('[FATAL] --api 必须配 --users u1,u2,... (推导可见性预期数量;')
        print('       第一个为默认 owner, 与服务的 Y8Y_USERS 顺序一致)')
        return 2
    if identity and users and identity not in users:
        print(f'[FATAL] --identity {identity} 不在 --users 列表里 — 身份预期矛盾')
        return 2

    if not os.path.isdir(original) or not os.path.isdir(copy):
        print(f'[FATAL] 目录不存在: {original} / {copy}')
        return 2

    ok = True
    print('== 文件级比对 (集合 + SHA-256; 默认不输出真实文件名) ==')
    a, b = walk_files(original), walk_files(copy)
    missing = sorted(set(a) - set(b))
    extra = sorted(set(b) - set(a))
    changed = sorted(r for r in set(a) & set(b) if a[r] != b[r])
    ok &= check(f'文件集合一致 (原件 {len(a)} / 副本 {len(b)})',
                not missing and not extra,
                f'missing={len(missing)} extra={len(extra)}')
    for label, rels in (('缺失 (原件有副本无)', missing),
                        ('多余 (副本有原件无)', extra),
                        ('内容变更', changed)):
        if rels:
            report_divergence(label, rels, diagnostic)
    ok &= check('逐文件 SHA-256 一致', not changed, f'changed={len(changed)}')
    md_a = sum(1 for r in a if r.endswith('.md'))
    md_b = sum(1 for r in b if r.endswith('.md'))
    print(f'  markdown 文件: 原件 {md_a} / 副本 {md_b}')
    ok &= check('markdown 文件数一致', md_a == md_b, f'{md_a} vs {md_b}')
    # Google 历史目录: 存在性必须一致 (内容哈希已在上面覆盖)
    g_a = sorted(r for r in a if r.startswith('google_calendar/'))
    g_b = sorted(r for r in b if r.startswith('google_calendar/'))
    if g_a or g_b:
        sym = sorted(set(g_a) ^ set(g_b))
        ok &= check('历史 google_calendar/ 文件集合一致',
                    set(g_a) == set(g_b), f'symmetric-diff={len(sym)}')
        if sym:
            report_divergence('google_calendar 差异', sym, diagnostic)
        print(f'  历史 Google 配置: {len(g_a)} 个文件 (哈希已核验, 内容未输出)')

    if api_base:
        print(f'== API 只读检查 (身份={identity}; 不输出任何任务内容/令牌) ==')
        tok_file = os.path.join(copy, TOKEN_FILE)
        if not os.path.isfile(tok_file):
            ok &= check(f'副本含 {TOKEN_FILE}', False, tok_file + ' 缺失')
        else:
            try:
                tokens = json.load(open(tok_file, encoding='utf-8'))
            except (json.JSONDecodeError, OSError) as e:
                tokens = {}
                ok &= check(f'{TOKEN_FILE} 可解析', False, type(e).__name__)
            if not isinstance(tokens, dict):
                tokens = {}
            if identity not in tokens:
                ok &= check(f'标识 {identity} 在 {TOKEN_FILE} 中存在', False,
                            f'可用标识数={len(tokens)} (名称不回显)')
            else:
                ok &= check(f'标识 {identity} 的 Token 已精确定位 (非随机)', True)
                st, raw = api_get(api_base, '/todo/api/tasks', tokens[identity])
                if st != 200:
                    ok &= check('GET /todo/api/tasks → 200', False, f'got {st}')
                else:
                    ok &= check('GET /todo/api/tasks → 200', True)
                    try:
                        body = json.loads(raw.decode('utf-8'))
                    except (json.JSONDecodeError, UnicodeDecodeError):
                        body = None
                        ok &= check('响应是合法 JSON', False)
                    if isinstance(body, dict):
                        ok &= check('响应是合法 JSON', True)
                        fields = {'current_user', 'tasks', 'completed_tasks', 'sections'}
                        ok &= check('契约字段齐全 (current_user/tasks/completed_tasks/sections)',
                                    fields <= set(body))
                        cu = body.get('current_user')
                        ok &= check(f'current_user == 预期身份 ({identity})', cu == identity,
                                    f'got {cu!r}')
                        n_tasks = len(body.get('tasks') or [])
                        n_done = len(body.get('completed_tasks') or [])
                        exp_tasks, exp_done = expected_visible_counts(copy, identity, users)
                        print(f'  tasks={n_tasks} completed={n_done} (仅数量)')
                        print(f'  按可见性模型预期: tasks={exp_tasks} completed={exp_done} '
                              f'(软隐藏/shared 已按服务端规则计入)')
                        if exp_tasks == 0 and exp_done == 0:
                            ok &= check('副本中存在该身份可见的任务文件', False,
                                        '预期为 0 — 数据可能为空或身份与数据不匹配')
                        elif n_tasks == 0 and n_done == 0:
                            ok &= check('API 未返回空列表 (防空列表假阳性)', False,
                                        f'预期 tasks={exp_tasks} completed={exp_done}')
                        else:
                            ok &= check('API 未返回空列表 (防空列表假阳性)', True)
                        ok &= check('tasks 数量与预期一致', n_tasks == exp_tasks,
                                    f'api={n_tasks} expected={exp_tasks}')
                        ok &= check('completed 数量与预期一致', n_done == exp_done,
                                    f'api={n_done} expected={exp_done}')
        st, _ = api_get(api_base, '/todo/')
        ok &= check('GET /todo/ → 200 (UI)', st == 200, f'got {st}')

    print(f'结果: {"全部通过" if ok else "存在不一致"}')
    return 0 if ok else 1


if __name__ == '__main__':
    sys.exit(main(sys.argv))
