"""中国节假日更新器的数据/解析/写入回归测试。

全部使用离线 fixture 与临时目录；不访问公网、不调用生产 Todo API、
不写真实任务数据。2027 的模拟数据只存在于 tests/fixtures/ 与临时目录。
"""
import datetime as dt
import importlib.util
import json
import shutil
import subprocess
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parent.parent
FIX = ROOT / "tests" / "fixtures" / "cn_holiday"
SCRIPT = ROOT / "scripts" / "cn_holiday_updater.py"

_spec = importlib.util.spec_from_file_location("cn_holiday_updater", SCRIPT)
u = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(u)


def fixture(name):
    return (FIX / name).read_bytes()


@pytest.fixture
def repo(tmp_path, monkeypatch):
    """一份隔离的仓库副本；所有写操作都落在这里。"""
    (tmp_path / "holidays").mkdir()
    (tmp_path / "src").mkdir()
    data_path = tmp_path / "holidays" / "cn-holidays.json"
    index_path = tmp_path / "src" / "index.html"
    shutil.copy(ROOT / "holidays" / "cn-holidays.json", data_path)
    shutil.copy(ROOT / "src" / "index.html", index_path)
    monkeypatch.setattr(u, "DATA_PATH", data_path)
    monkeypatch.setattr(u, "INDEX_PATH", index_path)
    return {
        "root": tmp_path,
        "data": data_path,
        "index": index_path,
        "data_bytes": data_path.read_bytes(),
        "index_bytes": index_path.read_bytes(),
    }


# ── 年份计算 / 调度切换 ────────────────────────────────────────────────────

@pytest.mark.parametrize("today,expected", [
    ("2026-11-16", 2027), ("2026-12-16", 2027),
    ("2027-01-16", 2027), ("2026-01-16", 2026),
    ("2026-10-31", 2027), ("2028-01-16", 2028),
    ("2031-11-16", 2032),
])
def test_target_year(today, expected):
    assert u.target_year(dt.date.fromisoformat(today)) == expected


@pytest.mark.parametrize("today,expected", [
    ("2026-11-16", "2026-12-16"), ("2026-12-16", "2027-01-16"),
    ("2027-01-16", "2027-11-16"), ("2026-11-03", "2026-11-16"),
])
def test_next_check(today, expected):
    assert u.next_check(dt.date.fromisoformat(today)).startswith(expected)


def test_year_is_not_hardcoded():
    source = SCRIPT.read_text(encoding="utf-8")
    for year in ("2027", "2028", "2029"):
        assert year not in source, "更新器源码不得写死目标年份 %s" % year


def test_no_llm_or_agent_dependency():
    import re
    source = SCRIPT.read_text(encoding="utf-8")
    for word in ("hermes", "openai", "anthropic", "llm", "gpt-", "web_search"):
        assert not re.search(r"\b" + re.escape(word), source.lower()), \
            "更新器不得依赖 %s" % word


# ── 检查流程 ───────────────────────────────────────────────────────────────

def test_not_published_writes_nothing(repo):
    data = u.load_data()
    result = u.run_check(data, dt.date(2026, 11, 16),
                         feed_bytes=fixture("feed-not-published.json"),
                         allow_network=False)
    assert result["status"] == "not-published"
    assert "尚未" in result["message"] and "12-16" in result["next_check"]
    assert repo["data"].read_bytes() == repo["data_bytes"]
    assert repo["index"].read_bytes() == repo["index_bytes"]


def test_untrusted_source_rejected(repo):
    data = u.load_data()
    result = u.run_check(data, dt.date(2026, 11, 16),
                         feed_bytes=fixture("feed-untrusted.json"),
                         notice_bytes=fixture("notice-2027.html"),
                         allow_network=False)
    assert result["status"] == "untrusted-source"
    assert repo["data"].read_bytes() == repo["data_bytes"]


def test_source_conflict_stops(repo):
    data = u.load_data()
    result = u.run_check(data, dt.date(2026, 11, 16),
                         feed_bytes=fixture("feed-conflict.json"),
                         notice_bytes=fixture("notice-2027.html"),
                         allow_network=False)
    assert result["status"] == "source-conflict"
    assert repo["data"].read_bytes() == repo["data_bytes"]


def test_broken_feed_is_unavailable_not_not_published(repo):
    data = u.load_data()
    result = u.run_check(data, dt.date(2026, 11, 16), feed_bytes=b"<html>403</html>",
                         allow_network=False)
    assert result["status"] == "check-unavailable"
    assert repo["data"].read_bytes() == repo["data_bytes"]


def test_offline_without_feed_is_unavailable(repo):
    data = u.load_data()
    result = u.run_check(data, dt.date(2026, 11, 16), allow_network=False)
    assert result["status"] == "check-unavailable"


def test_already_present_is_silent_no_op(repo):
    data = u.load_data()
    result = u.run_check(data, dt.date(2025, 11, 16),
                         feed_bytes=fixture("feed-not-published.json"),
                         notice_bytes=fixture("notice-2026.html"),
                         allow_network=False)
    assert result["status"] == "already-present"
    assert repo["data"].read_bytes() == repo["data_bytes"]
    assert repo["index"].read_bytes() == repo["index_bytes"]


def test_conflicting_manual_url_stops(repo):
    data = u.load_data()
    result = u.run_check(data, dt.date(2026, 11, 16),
                         feed_bytes=fixture("feed-2027-published.json"),
                         notice_bytes=fixture("notice-2027.html"),
                         notice_url="https://www.gov.cn/zhengce/other.htm",
                         allow_network=False)
    assert result["status"] == "source-conflict"


def test_two_identical_runs_are_identical(repo):
    data = u.load_data()
    kw = dict(feed_bytes=fixture("feed-2027-published.json"),
              notice_bytes=fixture("notice-2027.html"), allow_network=False)
    a = u.run_check(data, dt.date(2026, 11, 16), **kw)
    b = u.run_check(u.load_data(), dt.date(2026, 11, 16), **kw)
    assert a == b
    assert a["status"] == "ready"


# ── 解析：真实官方通知 + 2027 模拟 ─────────────────────────────────────────

@pytest.mark.parametrize("year", [2025, 2026])
def test_real_official_notice_reproduces_existing_data(year):
    data = u.load_data()
    candidate, errors = u.parse_notice(fixture("notice-%d.html" % year), year, data)
    assert errors == [], errors
    assert candidate["schedule"] == data["schedules"][str(year)]


def test_2027_simulation_is_fixture_only_and_not_in_real_data():
    data = u.load_data()
    assert "2027" in data["festivals"]
    assert "2027" not in data["schedules"], "模拟数据不得写入真实节假日数据"
    assert "TEST FIXTURE ONLY" in fixture("notice-2027.html").decode("utf-8")


def test_2027_simulation_parses_and_validates():
    data = u.load_data()
    candidate, errors = u.parse_notice(fixture("notice-2027.html"), 2027, data)
    assert errors == [], errors
    assert candidate["year"] == 2027
    assert candidate["wenhao"] == "国办发明电〔2026〕1号"
    assert len(candidate["schedule"]["workdays"]) == 5
    assert u.validate_schedule(2027, candidate["schedule"], data) == []


def test_missing_workdays_requires_human():
    data = u.load_data()
    text = fixture("notice-2027.html").decode("utf-8")
    for sentence in ("2月13日（周六）、2月14日（周日）上班。",
                     "5月8日（周六）上班。",
                     "10月9日（周六）、10月10日（周日）上班。"):
        text = text.replace(sentence, "")
    candidate, errors = u.parse_notice(text.encode("utf-8"), 2027, data)
    assert candidate is None
    assert any("调休上班日" in e for e in errors)


def test_mismatched_day_count_is_rejected():
    data = u.load_data()
    text = fixture("notice-2027.html").decode("utf-8").replace("共8天", "共9天")
    candidate, errors = u.parse_notice(text.encode("utf-8"), 2027, data)
    assert candidate is None
    assert any("不一致" in e for e in errors)


def test_wrong_weekday_is_rejected():
    data = u.load_data()
    text = fixture("notice-2027.html").decode("utf-8").replace("1月1日（周五）", "1月1日（周一）")
    candidate, errors = u.parse_notice(text.encode("utf-8"), 2027, data)
    assert candidate is None
    assert any("星期" in e for e in errors)


def test_title_of_other_year_is_not_trusted():
    data = u.load_data()
    text = fixture("notice-2027.html").decode("utf-8").replace("2027年", "2026年")
    candidate, errors = u.parse_notice(text.encode("utf-8"), 2027, data)
    assert candidate is None
    assert errors


def test_missing_festival_dates_requires_human():
    data = json.loads(json.dumps(u.load_data()))
    data["festivals"].pop("2027")
    candidate, errors = u.parse_notice(fixture("notice-2027.html"), 2027, data)
    assert candidate is None
    assert any("节日本身" in e for e in errors)


# ── 校验器 ─────────────────────────────────────────────────────────────────

def _valid_2027():
    return {
        "ranges": [
            ["01-01", "01-03", "元旦"], ["02-05", "02-12", "春节"],
            ["04-03", "04-05", "清明节"], ["05-01", "05-05", "劳动节"],
            ["06-09", "06-09", "端午节"], ["09-15", "09-15", "中秋节"],
            ["10-01", "10-07", "国庆节"],
        ],
        "overrides": {},
        "workdays": ["02-13", "02-14", "05-08", "10-09", "10-10"],
    }


def test_validator_accepts_good_schedule():
    assert u.validate_schedule(2027, _valid_2027(), u.load_data()) == []


def test_validator_rejects_overlapping_ranges():
    s = _valid_2027()
    # 元旦区间延长到 2/6，与春节 2/5-2/12 重叠。
    s["ranges"][0] = ["01-01", "02-06", "元旦"]
    assert any("重叠" in e for e in u.validate_schedule(2027, s, u.load_data()))


def test_validator_rejects_duplicate_workdays():
    s = _valid_2027()
    s["workdays"].append("02-13")
    assert any("重复" in e for e in u.validate_schedule(2027, s, u.load_data()))


def test_validator_rejects_invalid_date():
    s = _valid_2027()
    s["ranges"][0] = ["02-30", "02-30", "元旦"]
    assert any("无效日期" in e for e in u.validate_schedule(2027, s, u.load_data()))


def test_validator_rejects_cross_year_range():
    s = _valid_2027()
    s["ranges"][0] = ["12-31", "01-02", "元旦"]
    assert any("跨年" in e for e in u.validate_schedule(2027, s, u.load_data()))


def test_validator_requires_all_seven_festivals():
    s = _valid_2027()
    s["ranges"] = s["ranges"][:-1]
    assert any("七个主要节日" in e for e in u.validate_schedule(2027, s, u.load_data()))


def test_validator_requires_workdays():
    s = _valid_2027()
    s["workdays"] = []
    assert any("缺少调休" in e for e in u.validate_schedule(2027, s, u.load_data()))


# ── 原子写入 / 幂等 / 前端保护 ─────────────────────────────────────────────

def test_failed_second_write_leaves_no_half_written_files(tmp_path, monkeypatch):
    a, b = tmp_path / "a.txt", tmp_path / "b.txt"
    a.write_text("AAA", encoding="utf-8")
    b.write_text("BBB", encoding="utf-8")
    real_replace = u._replace
    calls = {"n": 0}

    def flaky(src, dst):
        calls["n"] += 1
        if calls["n"] == 2:
            raise OSError("injected failure on second rename")
        return real_replace(src, dst)

    monkeypatch.setattr(u, "_replace", flaky)
    with pytest.raises(OSError):
        u.atomic_write_pairs([(a, "NEW-A"), (b, "NEW-B")])
    assert a.read_text(encoding="utf-8") == "AAA"
    assert b.read_text(encoding="utf-8") == "BBB"
    assert sorted(p.suffix for p in tmp_path.iterdir()) == [".txt", ".txt"]


def test_apply_adds_only_target_year_and_keeps_2026(repo):
    data = u.load_data()
    result = u.run_check(data, dt.date(2026, 11, 16),
                         feed_bytes=fixture("feed-2027-published.json"),
                         notice_bytes=fixture("notice-2027.html"),
                         allow_network=False)
    assert result["status"] == "ready"
    applied = u.apply_candidate(data, result["candidate"])
    assert applied["status"] == "updated"

    new_data = json.loads(repo["data"].read_text(encoding="utf-8"))
    assert "2027" in new_data["schedules"]
    assert new_data["schedules"]["2026"] == data["schedules"]["2026"]
    assert new_data["schedules"]["2025"] == data["schedules"]["2025"]
    assert new_data["festivals"] == data["festivals"]
    assert new_data["sources"]["2027"]["wenhao"] == "国办发明电〔2026〕1号"

    index_text = repo["index"].read_text(encoding="utf-8")
    prefix, block, suffix = u.split_index(index_text)
    assert prefix == u.split_index(repo["index_bytes"].decode("utf-8"))[0]
    assert suffix == u.split_index(repo["index_bytes"].decode("utf-8"))[2]
    assert "'2027':" in block and "@media" not in block
    assert u.media_count(index_text) == 5
    assert block == u.render_block(new_data)
    assert u.validate_data(new_data) == []


def test_apply_twice_is_idempotent(repo):
    data = u.load_data()
    kw = dict(feed_bytes=fixture("feed-2027-published.json"),
              notice_bytes=fixture("notice-2027.html"), allow_network=False)
    first = u.run_check(data, dt.date(2026, 11, 16), **kw)
    u.apply_candidate(data, first["candidate"])
    snapshot = (repo["data"].read_bytes(), repo["index"].read_bytes())
    second = u.run_check(u.load_data(), dt.date(2026, 11, 16), **kw)
    assert second["status"] == "already-present"
    assert snapshot == (repo["data"].read_bytes(), repo["index"].read_bytes())


def test_replace_block_refuses_media_loss():
    original = (ROOT / "src" / "index.html").read_text(encoding="utf-8")
    with pytest.raises(u.UpdaterError):
        u.replace_block(original, u.BEGIN_MARKER + "\n@media (max-width: 1px) {}\n" + u.END_MARKER + "\n")


def test_verify_cli_detects_block_drift(repo):
    text = repo["index"].read_text(encoding="utf-8")
    repo["index"].write_text(text.replace("const CN_FESTIVALS = {",
                                          "const CN_FESTIVALS = { /* drift */"),
                             encoding="utf-8")
    proc = subprocess.run(
        [sys.executable, str(SCRIPT), "verify", "--root", str(repo["root"])],
        capture_output=True, text=True,
    )
    assert proc.returncode != 0
    assert "不一致" in proc.stdout


def test_verify_cli_detects_missing_media_header(repo):
    text = repo["index"].read_text(encoding="utf-8")
    repo["index"].write_text(text.replace("@media (max-width: 640px) {", "@media (max-width: 640px) {", 1)
                             .replace("@media (min-width: 600px) {", "/* removed */ {", 1),
                             encoding="utf-8")
    proc = subprocess.run(
        [sys.executable, str(SCRIPT), "verify", "--root", str(repo["root"])],
        capture_output=True, text=True,
    )
    assert proc.returncode != 0
    assert "@media" in proc.stdout


def test_real_repo_verify_is_clean():
    proc = subprocess.run([sys.executable, str(SCRIPT), "verify", "--root", str(ROOT)],
                          capture_output=True, text=True)
    assert proc.returncode == 0, proc.stdout
    assert "结果：`ok`" in proc.stdout
