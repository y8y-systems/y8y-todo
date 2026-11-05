"""Caddy Webhook 入口配置的静态/语法回归测试 (P1)。

* 旧文档把 ``header_up`` 写成了 ``handle`` 的直接子指令 —— Caddy 会报
  ``unrecognized directive: header_up``, 配置根本起不来。
* 本机/CI 若提供 caddy (PATH 或 ``CADDY_BIN``), 会真的执行
  ``caddy validate`` + ``caddy adapt`` 并核对生成的 JSON 路由;
  没有 caddy 时仍做确定性的结构检查, 语法验证结果另见 PR 报告。
"""
import json
import os
import shutil
import subprocess
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parent.parent
CADDYFILE = ROOT / 'packaging' / 'Caddyfile.line-webhook.example'


def _caddy_bin():
    explicit = os.environ.get('CADDY_BIN', '').strip()
    if explicit and Path(explicit).is_file() and os.access(explicit, os.X_OK):
        return explicit
    return shutil.which('caddy')


def _blocks(text):
    """返回 (行号, 去掉注释的行, 大括号深度) 列表。"""
    out = []
    depth = 0
    for no, raw in enumerate(text.splitlines(), 1):
        line = raw.split('#', 1)[0].rstrip()
        stripped = line.strip()
        if stripped:
            out.append((no, stripped, depth))
        depth += stripped.count('{') - stripped.count('}')
    return out


# ══════════════════════════════════════════════════════════════════════
# 结构检查 (永远执行)
# ══════════════════════════════════════════════════════════════════════
def test_caddyfile_example_exists_and_is_documented():
    assert CADDYFILE.is_file()
    text = CADDYFILE.read_text(encoding='utf-8')
    assert 'caddy validate' in text
    assert 'header_up' in text and 'reverse_proxy' in text


def test_header_up_is_nested_inside_reverse_proxy():
    """每条 header_up 都必须处在某个 reverse_proxy { ... } 块内部。"""
    lines = _blocks(CADDYFILE.read_text(encoding='utf-8'))
    proxy_depths = {d + 1 for _no, s, d in lines if s.startswith('reverse_proxy ')
                    and s.rstrip().endswith('{')}
    for no, stripped, depth in lines:
        if stripped.startswith('header_up '):
            assert depth in proxy_depths, (
                f'第 {no} 行 header_up 不在 reverse_proxy 块内 (深度 {depth}); '
                f'可用深度 {sorted(proxy_depths)}')
    assert proxy_depths, '没有找到带块的 reverse_proxy'


def test_webhook_route_is_exact_path_and_post_only():
    text = CADDYFILE.read_text(encoding='utf-8')
    assert '@line_webhook_post {' in text
    assert 'path /line/webhook' in text
    assert '\tmethod POST' in text or '    method POST' in text
    # 不能用 path_regexp / 通配前缀来转发 (那会把别的路径也送到 webhook)
    assert 'path /line/webhook*' not in text
    assert 'reverse_proxy 127.0.0.1:8087' in text


def test_bypass_prefix_is_denied_before_the_todo_app():
    text = CADDYFILE.read_text(encoding='utf-8')
    assert '@line_webhook_prefix path /line/webhook /line/webhook/*' in text
    assert 'respond "Not Found" 404' in text
    prefix_at = text.index('@line_webhook_prefix')
    fallback_at = text.rindex('handle {')
    assert prefix_at < fallback_at, '拒绝规则必须排在通用 handle 之前'


def test_normal_todo_api_keeps_authentication_and_token_stripping():
    text = CADDYFILE.read_text(encoding='utf-8')
    assert 'forward_auth 127.0.0.1:4180' in text
    assert 'copy_headers Cf-Access-Authenticated-User-Email' in text
    assert 'reverse_proxy 127.0.0.1:8086' in text
    # Webhook 路由与通用路由都要剥离不可信身份头/机器 Token
    assert text.count('header_up -X-Internal-Token') >= 2
    assert 'header_up -Cf-Access-Authenticated-User-Email' in text
    # 不能为了 webhook 整站放宽认证
    assert 'basic_auth' not in text  # 示例统一走 forward_auth
    assert text.count('handle {') == 1, '只能有一个无 matcher 的兜底 handle'


# ══════════════════════════════════════════════════════════════════════
# 真实 Caddy 语法验证 (有二进制时执行)
# ══════════════════════════════════════════════════════════════════════
@pytest.mark.skipif(_caddy_bin() is None, reason='本机/CI 未提供 caddy 二进制')
def test_caddy_validate_accepts_the_shipped_example():
    proc = subprocess.run(
        [_caddy_bin(), 'validate', '--config', str(CADDYFILE),
         '--adapter', 'caddyfile'],
        capture_output=True, text=True, timeout=30)
    assert proc.returncode == 0, proc.stderr
    assert 'Valid configuration' in (proc.stdout + proc.stderr)


@pytest.mark.skipif(_caddy_bin() is None, reason='本机/CI 未提供 caddy 二进制')
def test_caddy_adapt_produces_exact_match_and_header_stripping():
    proc = subprocess.run(
        [_caddy_bin(), 'adapt', '--config', str(CADDYFILE),
         '--adapter', 'caddyfile'],
        capture_output=True, text=True, timeout=30)
    assert proc.returncode == 0, proc.stderr
    adapted = json.loads(proc.stdout)
    blob = json.dumps(adapted, ensure_ascii=False)
    assert '"path": ["/line/webhook"]' in blob
    assert '"method": ["POST"]' in blob
    assert 'X-Internal-Token' in blob
    assert 'Cf-Access-Authenticated-User-Email' in blob
    assert '"delete"' in blob
    assert '8087' in blob and '8086' in blob
    assert '"static_response"' in blob          # 前缀 404
    assert '"forward_auth"' in blob or 'X-Forwarded-Method' in blob


@pytest.mark.skipif(_caddy_bin() is None, reason='本机/CI 未提供 caddy 二进制')
def test_the_old_header_up_placement_is_rejected(tmp_path):
    """负对照: 旧文档那种写法必须被 Caddy 拒绝。"""
    broken = tmp_path / 'broken.caddyfile'
    broken.write_text(
        'todo.example.com {\n'
        '  @linewebhook path /line/webhook\n'
        '  handle @linewebhook {\n'
        '    header_up -X-Internal-Token\n'
        '    reverse_proxy 127.0.0.1:8087\n'
        '  }\n'
        '  handle {\n'
        '    reverse_proxy 127.0.0.1:8086\n'
        '  }\n'
        '}\n', encoding='utf-8')
    proc = subprocess.run(
        [_caddy_bin(), 'validate', '--config', str(broken),
         '--adapter', 'caddyfile'],
        capture_output=True, text=True, timeout=30)
    assert proc.returncode != 0
    assert 'header_up' in (proc.stdout + proc.stderr)
