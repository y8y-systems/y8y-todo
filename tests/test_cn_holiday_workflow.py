"""定时工作流的结构性回归：调度语义、最小权限、防重复 PR、CI Gate 约束。"""
import subprocess
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
WF = ROOT / ".github" / "workflows" / "cn-holiday-updater.yml"
GATES = {
    "pytest.yml": "gate / pytest",
    "ubuntu-2204.yml": "gate / ubuntu",
    "linux-deploy-e2e.yml": "gate / e2e",
    "p4-upgrade-rehearsal.yml": "gate / upgrade",
}


def test_workflow_exists():
    assert WF.is_file()


def test_schedule_preserves_old_cron_semantics():
    text = WF.read_text(encoding="utf-8")
    assert "- cron: '0 1 16 11,12,1 *'" in text, "01:00 UTC = 10:00 JST，日期 11/16、12/16、1/16"
    assert "workflow_dispatch:" in text
    assert "type: boolean" in text   # 手动重跑入口


def test_least_privilege_and_no_personal_credentials():
    text = WF.read_text(encoding="utf-8")
    assert "permissions:\n  contents: read" in text
    assert "secrets.GITHUB_TOKEN" not in text.replace("GH_TOKEN: ${{ secrets.GITHUB_TOKEN }}", "")
    assert "PAT" not in text.upper() or "PERSONAL_ACCESS" not in text.upper()
    assert "y8y-systems@users.noreply.github.com" in text  # commit 作者不是私人邮箱
    assert "pull-requests: read" in text                    # 只用 GITHUB_TOKEN 读已有 PR


def test_pr_creation_requires_human_approval_and_no_auto_merge_or_deploy():
    text = WF.read_text(encoding="utf-8")
    for forbidden in ("gh pr merge", "--auto", "auto-merge", "deploy", "scp ", "ssh "):
        assert forbidden not in text, "不得自动合并或部署生产: %s" % forbidden
    assert "peter-evans/create-pull-request@v6" in text
    assert "delete-branch: true" in text


def test_no_duplicate_pr():
    text = WF.read_text(encoding="utf-8")
    assert "gh pr list --head \"$BRANCH\" --state open" in text
    assert "steps.existing.outputs.count == '0'" in text


def test_offline_rehearsal_cannot_create_pr():
    text = WF.read_text(encoding="utf-8")
    assert "--no-network" in text
    assert '[ "$REHEARSAL" != "true" ]' in text
    assert '"ready=false"' in text


def test_scheduled_run_can_reach_official_history_fallback():
    """正常检查不能被钉死在离线样本上：历史归档/精确检索必须允许联网兜底。"""
    text = WF.read_text(encoding="utf-8")
    assert "--history-feed" not in text, "定时检查不得固定历史检索离线样本"
    assert "--exact-feed" not in text, "定时检查不得固定精确检索离线样本"
    # --no-network 只允许出现在 offline_rehearsal 分支里
    rehearsal_branch = text.split('if [ "$REHEARSAL" = "true" ]; then', 1)[1].split("else", 1)[0]
    assert "--no-network" in rehearsal_branch


def test_failure_is_reported():
    text = WF.read_text(encoding="utf-8")
    assert "GITHUB_STEP_SUMMARY" in text
    assert "::error::" in text


def test_gate_job_satisfies_repo_gate_rules():
    text = WF.read_text(encoding="utf-8")
    assert "name: gate / cn-holidays" in text
    assert "needs: [check, propose]" in text
    assert "if: always()" in text
    assert "bash scripts/ci-gate-check.sh" in text
    assert "RESULT_check: ${{ needs.check.result }}" in text
    assert "RESULT_propose: ${{ needs.propose.result }}" in text


def test_repo_gate_checker_is_clean():
    proc = subprocess.run(["bash", "scripts/check-ci-gates.sh"],
                          cwd=ROOT, capture_output=True, text=True)
    assert proc.returncode == 0, proc.stdout + proc.stderr
    assert "CI GATE CHECK: CLEAN" in proc.stdout


def test_required_check_names_are_unchanged():
    for name, expected in GATES.items():
        text = (ROOT / ".github" / "workflows" / name).read_text(encoding="utf-8")
        assert "name: %s" % expected in text
