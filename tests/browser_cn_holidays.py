#!/usr/bin/env python3
"""中国节假日 / 调休 / 日本祝日 / 手机端 / 离线 的真实浏览器回归。

用法（本地或 CI，连的是隔离的 dev-mode 实例，绝不连生产 Todo）::

    python3 tests/browser_cn_holidays.py http://127.0.0.1:8086

需要 playwright + chromium。任何一条失败都以非 0 退出。
"""
import sys
import urllib.parse

from playwright.sync_api import sync_playwright

BASE = (sys.argv[1] if len(sys.argv) > 1 else "http://127.0.0.1:8086").rstrip("/")
RESULTS = []


def check(name, ok, detail=""):
    RESULTS.append((name, bool(ok), detail))
    print(("PASS  " if ok else "FAIL  ") + name + (" — " + detail if detail else ""))


def goto_month(page, year, month):
    # month 为 1-12。calendarCursor / renderCalendar 是页面里的全局绑定。
    page.evaluate(
        "([y, m]) => { calendarCursor = new Date(y, m - 1, 1); renderCalendar(); }",
        [year, month],
    )
    page.wait_for_timeout(120)


def cell(page, day_key):
    return page.locator('.cal-cell[data-date="%s"]' % day_key)


def texts(page, day_key, selector):
    loc = cell(page, day_key).locator(selector)
    return [loc.nth(i).inner_text().strip() for i in range(loc.count())]


def main():
    external = []
    with sync_playwright() as pw:
        browser = pw.chromium.launch(headless=True)
        ctx = browser.new_context(viewport={"width": 1280, "height": 900})
        page = ctx.new_page()

        def on_request(req):
            host = urllib.parse.urlsplit(req.url).hostname
            if host and host not in ("127.0.0.1", "localhost", "::1"):
                external.append(req.url)
        page.on("request", on_request)

        page.goto(BASE + "/todo/", wait_until="domcontentloaded")
        page.wait_for_selector("#calendarGrid .cal-cell", timeout=15000)

        # ── 中国节假日 + 覆盖标签 ──
        goto_month(page, 2026, 10)
        check("2026-10-01 中国节假日格有淡黄底类",
              "cal-cn-holiday-cell" in (cell(page, "2026-10-01").get_attribute("class") or ""))
        check("2026-10-01 显示国庆节", "国庆节" in texts(page, "2026-10-01", ".cal-holiday"))
        goto_month(page, 2025, 10)
        check("2025-10-06 overrides 显示中秋节",
              "中秋节" in texts(page, "2025-10-06", ".cal-holiday"))

        # ── 调休补班「班」 ──
        goto_month(page, 2026, 1)
        cls = cell(page, "2026-01-04").get_attribute("class") or ""
        check("2026-01-04 显示「班」", "班" in texts(page, "2026-01-04", ".cal-workday"))
        check("调休上班日不涂周末/节假日底色",
              "cal-saturday-cell" not in cls and "cal-cn-holiday-cell" not in cls)
        check("2026-01-01 显示元旦", "元旦" in texts(page, "2026-01-01", ".cal-holiday"))

        # ── 日本祝日不受影响 ──
        check("2026-01-01 日本祝日「元日」仍在",
              "元日" in texts(page, "2026-01-01", ".cal-holiday"))
        goto_month(page, 2026, 2)
        check("2026-02-11 建国記念の日仍在",
              "建国記念の日" in texts(page, "2026-02-11", ".cal-holiday"))

        # ── 未公布年份回退到节日本身，且没有「班」 ──
        goto_month(page, 2027, 1)
        check("2027（尚无完整安排）仍显示元旦",
              "元旦" in texts(page, "2027-01-01", ".cal-holiday"))
        check("2027 没有凭空出现调休上班日",
              page.locator("#calendarGrid .cal-workday").count() == 0)

        # ── 用户可见性：本机覆盖关掉中国节假日后，日本祝日仍保留 ──
        goto_month(page, 2026, 1)
        page.evaluate("localStorage.setItem('todoCnHolidays_dev', '0')")
        page.reload(wait_until="domcontentloaded")
        page.wait_for_selector("#calendarGrid .cal-cell", timeout=15000)
        goto_month(page, 2026, 1)
        check("关闭后无中国节假日格", page.locator("#calendarGrid .cal-cn-holiday-cell").count() == 0)
        check("关闭后无「班」标签", page.locator("#calendarGrid .cal-workday").count() == 0)
        check("关闭后日本祝日仍显示", "元日" in texts(page, "2026-01-01", ".cal-holiday"))
        page.evaluate("localStorage.removeItem('todoCnHolidays_dev')")
        page.reload(wait_until="domcontentloaded")
        page.wait_for_selector("#calendarGrid .cal-cell", timeout=15000)
        goto_month(page, 2026, 1)
        check("重新开启后中国节假日恢复",
              page.locator("#calendarGrid .cal-cn-holiday-cell").count() > 0)

        # ── 任务交互未被破坏（拖拽落点 + 日期抽屉） ──
        check("日历格仍带 data-date（拖拽落点）",
              page.locator("#calendarGrid .cal-cell[data-date]").count() > 27)
        cell(page, "2026-01-15").click()
        page.wait_for_timeout(150)
        check("点击日期格可打开任务抽屉",
              "open" in (page.locator("#dayDrawerOverlay").get_attribute("class") or ""))
        page.keyboard.press("Escape")

        # ── 桌面端 / 手机端 @media 没有退化 ──
        rows_desktop = page.evaluate(
            "getComputedStyle(document.getElementById('calendarGrid')).gridAutoRows")
        check("桌面端日历行高 130px", rows_desktop.strip() == "130px", rows_desktop)
        page.set_viewport_size({"width": 375, "height": 812})
        page.wait_for_timeout(150)
        rows_mobile = page.evaluate(
            "getComputedStyle(document.getElementById('calendarGrid')).gridAutoRows")
        check("手机端 @media 生效（行高 110px）", rows_mobile.strip() == "110px", rows_mobile)
        check("手机端仍有完整日历格", page.locator("#calendarGrid .cal-cell").count() > 27)

        # ── 离线：没有任何外部网络请求 ──
        check("全程无外部网络请求（离线可用）", not external, "; ".join(external[:3]))
        ctx.close()
        browser.close()

    failed = [n for n, ok, _ in RESULTS if not ok]
    print("\n%d checks, %d failed" % (len(RESULTS), len(failed)))
    return 1 if failed else 0


if __name__ == "__main__":
    sys.exit(main())
