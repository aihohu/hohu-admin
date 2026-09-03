"""scripts/seed_ai_agents.py 独立运行时的模型注册回归测试。

ai_conversation 声明了 (tenant_id, user_id) → sys_user 复合外键，mapper 配置期
强制解析 FK 目标表。脚本独立运行只 import AI 模型时 sys_user 不在 Base.metadata，
触发 NoReferencedTableError（CI Seed test data 步骤曾因此失败）。脚本必须同时
import FK 目标模型模块保证注册表完整。
"""

import subprocess
import sys


def test_seed_ai_agents_configures_mappers_standalone() -> None:
    """独立解释器中 import 脚本并触发 mapper 全量配置必须成功。"""
    result = subprocess.run(
        [
            sys.executable,
            "-c",
            "from sqlalchemy.orm import configure_mappers; "
            "import scripts.seed_ai_agents; "
            "configure_mappers()",
        ],
        capture_output=True,
        text=True,
        check=False,
    )
    assert result.returncode == 0, result.stderr
