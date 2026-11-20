# ruff: noqa: T201

import os
import secrets
import subprocess
import sys
from pathlib import Path

from dotenv import dotenv_values

if hasattr(sys.stdout, "reconfigure"):
    sys.stdout.reconfigure(encoding="utf-8")
if hasattr(sys.stderr, "reconfigure"):
    sys.stderr.reconfigure(encoding="utf-8")


def generate_secret_key():
    return secrets.token_hex(32)


SECRET_KEY_PLACEHOLDER = "<YOUR_SUPER_SECRET_KEY_HERE>"


def init_env_file():
    if os.path.exists(".env"):
        print("[OK] 已存在 .env 文件，跳过配置。")
        return

    if not os.path.exists(".env.example"):
        print("[WARN] 警告：未找到 .env.example，跳过 .env 配置。")
        return

    # 复制 .env.example 为 .env
    with open(".env.example", encoding="utf-8") as f:
        content = f.read()

    # 自动替换 SECRET_KEY 占位符
    secret_key = generate_secret_key()
    content = content.replace(
        f"SECRET_KEY={SECRET_KEY_PLACEHOLDER}",
        f"SECRET_KEY={secret_key}",
    )

    with open(".env", "w", encoding="utf-8") as f:
        f.write(content)

    print("[WARN] 请检查 .env 中的数据库、Redis 等配置是否正确。\n")


def init_project():
    init_env_file()
    values = dotenv_values(".env")
    if not values.get("HOHU_ADMIN_PASSWORD") and not os.environ.get(
        "HOHU_ADMIN_PASSWORD"
    ):
        password = "Aa1" + secrets.token_hex(8)[:15]
        with Path(".env").open("a", encoding="utf-8") as stream:
            stream.write(f"\nHOHU_ADMIN_PASSWORD={password}\n")
        values["HOHU_ADMIN_PASSWORD"] = password
    environment = {**{k: v for k, v in values.items() if v is not None}, **os.environ}
    inventory = environment.get("AUTHORIZATION_MAINTENANCE_INVENTORY")
    if not inventory:
        print(
            "初始化需要 AUTHORIZATION_MAINTENANCE_INVENTORY，参见 docs/AUTHORIZATION-MAINTENANCE.md。",
            file=sys.stderr,
        )
        raise SystemExit(1)
    seed_command = [sys.executable, "-m", "scripts.init_db", "--inventory", inventory]
    if directory := environment.get("AUTHORIZATION_CONTROL_DIR"):
        seed_command.extend(("--control-dir", directory))
    if environment.get("AUTHORIZATION_BOOTSTRAP", "false").lower() == "true":
        seed_command.append("--bootstrap")
    if environment.get("HOHU_ADMIN_PASSWORD"):
        environment.setdefault(
            "AUTHORIZATION_MAINTENANCE_PASSWORD", environment["HOHU_ADMIN_PASSWORD"]
        )
    try:
        subprocess.run(
            [sys.executable, "-m", "alembic", "upgrade", "head"],
            check=True,
            env=environment,
        )
        subprocess.run(seed_command, check=True, env=environment)
    except (subprocess.CalledProcessError, FileNotFoundError):
        print("初始化失败，请修复错误后重新运行 hohu init。", file=sys.stderr)
        raise SystemExit(1) from None
    print("种子已完成，数据库仍停写；请按授权维护手册显式恢复后启动应用。")


if __name__ == "__main__":
    init_project()
