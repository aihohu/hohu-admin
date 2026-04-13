# ruff: noqa: T201

import os
import secrets
import subprocess
import sys


def run_command(args):
    cmd_str = " ".join(args)
    print(f"执行中: {cmd_str}")
    try:
        subprocess.run(args, check=True)
    except subprocess.CalledProcessError as e:
        print(f"❌ 命令执行失败: {cmd_str} (退出码: {e.returncode})")
        sys.exit(1)
    except FileNotFoundError:
        print(f"❌ 命令未找到: {args[0]}，请确保已安装相关工具")
        sys.exit(1)


def generate_secret_key():
    return secrets.token_hex(32)


SECRET_KEY_PLACEHOLDER = "<YOUR_SUPER_SECRET_KEY_HERE>"


def init_env_file():
    if os.path.exists(".env"):
        print("✅ 已存在 .env 文件，跳过配置。")
        return

    if not os.path.exists(".env.example"):
        print("⚠️ 警告：未找到 .env.example，跳过 .env 配置。")
        return

    # 复制 .env.example 为 .env
    with open(".env.example") as f:
        content = f.read()

    # 自动替换 SECRET_KEY 占位符
    secret_key = generate_secret_key()
    content = content.replace(
        f"SECRET_KEY={SECRET_KEY_PLACEHOLDER}",
        f"SECRET_KEY={secret_key}",
    )

    with open(".env", "w") as f:
        f.write(content)

    print("⚠️ 请检查 .env 中的数据库、Redis 等配置是否正确。\n")


def init_project():
    print("🥳 欢迎使用 HoHu Admin 后端初始化工具")

    # 1. 检查 .env 文件
    init_env_file()

    # 2. 数据库迁移
    if input("是否执行数据库迁移 (Alembic)? (y/n): ").lower() == "y":
        try:
            subprocess.run(
                [sys.executable, "-m", "alembic", "upgrade", "head"], check=True
            )
        except subprocess.CalledProcessError:
            print("⚠️ 数据库迁移失败（可能表已存在），尝试标记迁移版本...")
            try:
                subprocess.run(
                    [sys.executable, "-m", "alembic", "stamp", "head"], check=True
                )
                print("✅ 已标记所有迁移为已应用。")
            except subprocess.CalledProcessError:
                print("❌ 标记迁移版本失败，请手动检查数据库。")
                sys.exit(1)

    # 3. 初始化种子数据
    seed_script = "scripts/init_db.py"
    if input("是否初始化数据? (y/n): ").lower() == "y":
        if os.path.exists(seed_script):
            run_command([sys.executable, seed_script])
        else:
            print(f"❌ 种子脚本 {seed_script} 不存在，跳过。")

    print("\n✅ HoHu Admin 初始化完成！")


if __name__ == "__main__":
    init_project()
