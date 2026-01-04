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


def configure_database_url():
    default_url = "postgresql+asyncpg://myuser:mypassword@localhost:5432/hohu_admin"
    print("\n🗃️ 数据库连接 (PostgreSQL + asyncpg)")
    print(f"格式示例: {default_url}")

    url = input("请输入 DATABASE_URL [直接回车使用默认值]: ").strip()

    if not url:
        # 提供分步配置选项
        if input("是否分步配置数据库信息? (y/n) [n]: ").lower() == "y":
            from urllib.parse import quote_plus

            user = input("  用户名: ").strip() or "myuser"
            password = input("  密码: ").strip() or "mypassword"
            host = input("  主机: ").strip() or "localhost"
            port = input("  端口: ").strip() or "5432"
            db = input("  数据库名: ").strip() or "hohu_admin"
            safe_pass = quote_plus(password)
            url = f"postgresql+asyncpg://{user}:{safe_pass}@{host}:{port}/{db}"
        else:
            url = default_url

    return url


def ask_for_env_config():
    print("\n🔧 正在配置 .env 文件，请按提示输入（直接回车使用默认值）：\n")

    # 1. ENV
    env = input("运行环境 (dev/test/prod) [默认: dev]: ").strip() or "dev"

    # 2. SECRET_KEY
    secret_key = generate_secret_key()
    print(f"已自动生成 SECRET_KEY: {secret_key}")
    if input("是否重新生成或手动输入? (y/n) [默认: n]: ").lower() == "y":
        secret_key = input("请输入 SECRET_KEY: ").strip() or secret_key

    # 3. DATABASE_URL
    db_url = configure_database_url()

    # 4. Redis 配置（简化）
    redis_host = input("Redis 主机 [默认: localhost]: ").strip() or "localhost"
    redis_port = input("Redis 端口 [默认: 6379]: ").strip() or "6379"
    redis_password = input("Redis 密码 [默认: 无]: ").strip()
    redis_db = input("Redis DB [默认: 0]: ").strip() or "0"

    # 构建 .env 内容（保留原始注释结构）
    env_content = f"""# ======================================
# Application Settings
# ======================================

# Environment: dev | test | prod
ENV={env}

# Generate with: openssl rand -hex 32
SECRET_KEY={secret_key}  # ⚠️ 请妥善保管！
ALGORITHM=HS256
ACCESS_TOKEN_EXPIRE_MINUTES=60

# ======================================
# Database (PostgreSQL + asyncpg)
# ======================================
# Format: postgresql+asyncpg://<user>:<password>@<host>:<port>/<db_name>
DATABASE_URL={db_url}

# ======================================
# Redis (for caching, sessions, etc.)
# ======================================
REDIS_HOST={redis_host}
REDIS_PORT={redis_port}
REDIS_PASSWORD={redis_password}  # Leave empty if no password
REDIS_DB={redis_db}
"""
    return env_content


def init_env_file():
    if os.path.exists(".env"):
        print("✅ 已存在 .env 文件，跳过配置。")
        return

    if not os.path.exists(".env.example"):
        print("⚠️ 警告：未找到 .env.example，无法引导配置。")
        create_blank = input("是否仍要创建空 .env 文件? (y/n): ")
        if create_blank.lower() == "y":
            open(".env", "w").close()
        return

    print("未检测到 .env 文件，正在引导你完成配置...")
    env_content = ask_for_env_config()

    with open(".env", "w") as f:
        f.write(env_content)

    print("\n✅ .env 文件已生成！请检查配置是否正确。\n")


def init_project():
    print("🥞 欢迎使用 HoHo Admin 后端初始化工具\n")

    # 1. 安装依赖
    if input("是否安装依赖? (y/n): ").lower() == "y":
        run_command(["uv", "sync"])

    # 2. 检查 .env 文件
    init_env_file()

    # 3. 数据库迁移
    if input("是否执行数据库迁移 (Alembic)? (y/n): ").lower() == "y":
        run_command(["alembic", "upgrade", "head"])

    # 4. 初始化种子数据
    seed_script = "scripts/seed_data.py"
    if input("是否初始化管理账号和菜单数据? (y/n): ").lower() == "y":
        if os.path.exists(seed_script):
            run_command([sys.executable, seed_script])
        else:
            print(f"❌ 种子脚本 {seed_script} 不存在，跳过。")

    print("\n✅ 初始化完成！请运行 `fastapi dev app/main.py` 启动项目。")


if __name__ == "__main__":
    init_project()
