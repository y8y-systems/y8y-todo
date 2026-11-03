"""前端 / 渲染行为回归（静态 + 数据语义；真实浏览器验证见 browser_cn_holidays.py）。

这些断言不启动服务、不联网、不写任务数据。重点是：抽离数据后渲染语义不变，
以及 2026-09-22 误删 @media 头那类事故不可能悄悄发生。
"""
import datetime as dt
import importlib.util
import json
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
INDEX = ROOT / "src" / "index.html"
DATA = ROOT / "holidays" / "cn-holidays.json"

_spec = importlib.util.spec_from_file_location("cn_holiday_updater_frontend",
                                               ROOT / "scripts" / "cn_holiday_updater.py")
u = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(u)

TEXT = INDEX.read_text(encoding="utf-8")
DATA_JSON = json.loads(DATA.read_text(encoding="utf-8"))


# ── 结构化数据与前端内联区块严格同步 ───────────────────────────────────────

def test_guarded_block_is_exactly_generated_from_json():
    prefix, block, suffix = u.split_index(TEXT)
    assert block == u.render_block(DATA_JSON)
    assert TEXT.count(u.BEGIN_MARKER) == 1 and TEXT.count(u.END_MARKER) == 1
    assert "@media" not in block
    assert prefix.endswith("\n")


def test_inline_constants_still_exist_and_are_the_only_copy():
    for name in ("const CN_HOLIDAY_SCHEDULE =", "const CN_FESTIVALS =",
                 "const CN_FIXED_FESTIVALS =", "function getCnYearData("):
        assert TEXT.count(name) == 1


def test_all_media_headers_intact():
    assert u.media_count(TEXT) == 5
    for rule in ("@media (max-width: 640px)", "@media (min-width: 600px)",
                 "@media (max-width: 390px)", "@media (prefers-reduced-motion: reduce)"):
        assert rule in TEXT
    assert "grid-auto-rows: 130px" in TEXT       # 桌面端
    assert ".calendar-grid { grid-auto-rows: 110px" in TEXT  # 手机端规则仍在


# ── getCnYearData 的数据语义（按前端同一套规则在 Python 里展开） ─────────────

def expand(year):
    """复刻 getCnYearData(year)：ranges 展开 → overrides 覆盖 → workdays 集合。"""
    holidays, workdays = {}, set()
    key = str(year)
    sch = DATA_JSON["schedules"].get(key)
    if sch:
        for a, b, name in sch["ranges"]:
            d = dt.date(year, int(a[:2]), int(a[3:]))
            end = dt.date(year, int(b[:2]), int(b[3:]))
            while d <= end:
                holidays[d.isoformat()] = name
                d += dt.timedelta(days=1)
        for mmdd, name in (sch.get("overrides") or {}).items():
            holidays["%d-%s" % (year, mmdd)] = name
        for mmdd in sch["workdays"]:
            workdays.add("%d-%s" % (year, mmdd))
    else:
        for mmdd, name in DATA_JSON["festivals"].get(key, DATA_JSON["fixed_festivals"]):
            holidays["%d-%s" % (year, mmdd)] = name
    return holidays, workdays


def test_cn_holiday_names_render_as_expected():
    h, _ = expand(2026)
    assert h["2026-01-01"] == "元旦"
    assert h["2026-02-17"] == "春节"
    assert h["2026-10-01"] == "国庆节"
    h25, _ = expand(2025)
    assert h25["2025-10-06"] == "中秋节"   # overrides：国庆中秋连休里的中秋当天
    assert h25["2025-10-01"] == "国庆节"


def test_workday_badge_and_no_weekend_color():
    _, w = expand(2026)
    assert "2026-01-04" in w and "2026-10-10" in w
    h, _ = expand(2026)
    assert "2026-01-04" not in h, "调休上班日不能同时是节假日（会与「班」标签打架）"
    assert "cellEl.classList.add('cal-workday')" not in TEXT  # 标签是 createElement 挂的
    assert "tag.textContent = '班'" in TEXT
    assert "tag.title = '调休上班日（周末补班）'" in TEXT
    assert "if (isSaturday && !isCnWorkday) cellEl.classList.add('cal-saturday-cell');" in TEXT


def test_year_without_schedule_falls_back_to_festivals_only():
    h27, w27 = expand(2027)
    assert h27["2027-01-01"] == "元旦"
    assert h27["2027-02-06"] == "春节"
    assert len(h27) == 7
    assert w27 == set(), "未公布年份不能凭空推算调休上班日"
    h28, w28 = expand(2028)
    assert set(h28) == {"2028-01-01", "2028-05-01", "2028-10-01"}
    assert w28 == set()


def test_japanese_holidays_are_untouched():
    assert "const JP_FIXED_HOLIDAYS = [" in TEXT
    assert "['01-01', '元日']" in TEXT
    assert "['02-11', '建国記念の日']" in TEXT
    assert "['11-23', '勤労感謝の日']" in TEXT
    assert "function getJpHolidays(year)" in TEXT
    assert "const holidayName = getJpHolidays(cell.date.getFullYear()).get(cell.key);" in TEXT
    assert "'2027': ['03-21', '09-23']" in TEXT


def test_visibility_rules_are_config_driven_not_username_driven():
    assert "return cnHolidaysEnabled ? getCnYearData(year).holidays : new Map();" in TEXT
    assert "return cnHolidaysEnabled ? getCnYearData(year).workdays : new Set();" in TEXT
    assert "function cnHolidaysKey() { return 'todoCnHolidays_' + (currentUser || ''); }" in TEXT
    # 注释里会提到旧实现的 hardcoded 用户名，但代码里不许再出现。
    code_lines = [ln for ln in TEXT.splitlines() if not ln.lstrip().startswith("//")]
    assert not any("currentUser === 'alice'" in ln for ln in code_lines)
    assert not any("currentUser === 'bob'" in ln for ln in code_lines)


# ── 离线 / 无运行时外部依赖 / 任务交互未受影响 ──────────────────────────────

def test_holiday_data_has_no_runtime_network_dependency():
    _, block, _ = u.split_index(TEXT)
    for forbidden in ("fetch(", "XMLHttpRequest", "import(", "http://", "https://"):
        assert forbidden not in block, "节假日数据区块不得引入运行时网络依赖: %s" % forbidden
    assert "holidays/cn-holidays.json" in block  # 只是注释，说明真源在哪


def test_no_cdn_scripts():
    import re
    srcs = re.findall(r'<script[^>]+src="([^"]+)"', TEXT)
    assert srcs, "页面应当仍有本地脚本"
    assert all(not s.startswith(("http://", "https://", "//")) for s in srcs), srcs


def test_task_create_edit_drag_hooks_unchanged():
    assert "cellEl.dataset.date = cell.key;" in TEXT
    assert "function openDayDrawer(dayKey)" in TEXT
    assert "function changeCalendarMonth(delta)" in TEXT
    assert "id=\"dayDrawerOverlay\"" in TEXT
    assert "cal-cn-holiday-cell" in TEXT
