"""LINE 自助绑定前端回归：过期状态必须能一键重新生成绑定码。

2026-11-08 生产实测: 绑定码 10 分钟过期后页面只显示 复制 / 打开 LINE / 取消,
而「取消」只清 sessionStorage, 不清服务端的 ``code-*.json``；该文件按 mtime
24 小时后才被 GC, 于是用户会被卡在「绑定码已过期」直到旧文件被清理。
这些断言钉死「重新生成」入口，防止再次退化。
"""
import re
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
TEXT = (ROOT / "src" / "index.html").read_text(encoding="utf-8")


def _render_line_link():
    start = TEXT.index("function renderLineLink() {")
    end = TEXT.index("async function refreshLineLink()", start)
    return TEXT[start:end]


def test_expired_state_has_a_regenerate_button():
    assert TEXT.count('id="lineLinkRegenerateBtn"') == 1
    assert "重新生成绑定码" in TEXT
    # 作者样式 display:flex 会盖掉 UA 的 [hidden]{display:none}, 这条规则必须留着
    assert "#lineLinkGroup [hidden] { display: none !important; }" in TEXT


def test_render_resets_buttons_and_shows_regenerate_only_when_expired():
    render = _render_line_link()
    assert re.search(r"regenBtn\.hidden = true; cancelBtn\.hidden = false;", render)
    # 每次渲染先复位, 避免上一状态残留
    assert render.index("regenBtn.hidden = true") < render.index("if (s.state === 'expired') {")
    branch = render[render.index("if (s.state === 'expired') {"):]
    assert branch.index("regenBtn.hidden = false") < branch.index("return;")
    assert branch.index("regenBtn.disabled = !s.available") < branch.index("return;")
    # 「取消」只清浏览器缓存, 过期态里必须藏起来, 别再误导用户
    assert branch.index("cancelBtn.hidden = true") < branch.index("return;")


def test_regenerate_click_creates_a_fresh_code():
    assert re.search(
        r"getElementById\('lineLinkRegenerateBtn'\)\.addEventListener\('click'", TEXT)
    segment = TEXT[TEXT.index("getElementById('lineLinkRegenerateBtn').addEventListener"):][:300]
    assert "startLineLink(false)" in segment
    assert "b.disabled = true" in segment          # 防连点
    assert "已过期，点「重新生成绑定码」取一个新码再发送。" in TEXT
