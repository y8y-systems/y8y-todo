"""临时探针 —— 用来验证「没有人工 Approve 时，PR 合不进 main」。

一次性文件：验证完就随分支一起删掉。

- 纯占位，没有敏感信息、没有外部依赖、恒真
- 不参与任何功能路径，也不改变应用行为
"""


def test_approval_gate_probe_passes():
    assert True
