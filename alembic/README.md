# Generic single-database configuration.

Alembic 是 SQLAlchemy 官方推荐的数据库迁移工具，用于在不丢失数据的前提下对数据库结构进行版本控制和演进。以下是使用 Alembic 的基本步骤和说明：

---

## 一、安装 Alembic

```bash
pip install alembic
```

---

## 二、初始化 Alembic 环境

在项目根目录运行：

```bash
alembic init alembic
```

这会创建一个 `alembic/` 目录，包含以下主要文件：

- `env.py`：配置数据库连接和元数据（metadata）
- `alembic.ini`：Alembic 的主配置文件
- `versions/`：存放每次生成的迁移脚本

---

## 三、配置 Alembic

### 1. 修改 `alembic.ini`

找到 `sqlalchemy.url` 行，填入你的数据库连接 URL，例如：

```ini
sqlalchemy.url = postgresql://user:password@localhost/mydb
```

> 如果你不想明文写密码，也可以在 `env.py` 中动态设置。

### 2. 修改 `alembic/env.py`

确保 Alembic 能获取到你的模型元数据（即 SQLAlchemy 的 `Base.metadata`）。

假设你的模型定义在 `myapp.models` 中：

```python
from myapp.models import Base
target_metadata = Base.metadata
```

并在 `run_migrations_online()` 函数中确保使用了正确的引擎：

```python
def run_migrations_online():
    connectable = create_engine(config.get_main_option("sqlalchemy.url"))

    with connectable.connect() as connection:
        context.configure(
            connection=connection,
            target_metadata=target_metadata
        )

        with context.begin_transaction():
            context.run_migrations()
```

> 如果你使用的是 Flask + Flask-SQLAlchemy，可能需要从 app 中导入 `db` 对象，并设置 `target_metadata = db.metadata`。

---

## 四、生成第一个迁移脚本（自动检测模型变更）

当你修改了 SQLAlchemy 模型后，可以自动生成迁移脚本：

```bash
alembic revision --autogenerate -m "create users table"
```

这会在 `alembic/versions/` 下生成一个 Python 文件，内容类似：

```python
def upgrade():
    op.create_table('users', ...)

def downgrade():
    op.drop_table('users')
```

> ⚠️ 注意：`--autogenerate` 只能检测部分变更（如新增表、列），对列类型变更、索引等可能需要手动调整。

---

## 五、应用迁移（升级数据库）

```bash
alembic upgrade head
```

这会将数据库升级到最新版本（即所有迁移脚本按顺序执行）。

其他常用命令：

- 升级到指定版本：`alembic upgrade <revision_id>`
- 回滚到上一个版本：`alembic downgrade -1`
- 查看当前版本：`alembic current`
- 列出所有迁移历史：`alembic history`

---

## 六、完整工作流示例

1. 定义模型（`models.py`）：
   ```python
   from sqlalchemy import Column, Integer, String
   from sqlalchemy.ext.declarative import declarative_base

   Base = declarative_base()

   class User(Base):
       __tablename__ = 'users'
       id = Column(Integer, primary_key=True)
       name = Column(String(50))
   ```

2. 初始化 Alembic 并配置（如上所述）

3. 生成迁移：
   ```bash
   alembic revision --autogenerate -m "add user table"
   ```

4. 应用迁移：
   ```bash
   alembic upgrade head
   ```

5. 后续每次修改模型，重复步骤 3~4

---

## 七、常见问题

- **模型变更未被检测到？**  
  确保 `env.py` 中 `target_metadata` 正确指向你的模型元数据。

- **想手动写迁移？**  
  不加 `--autogenerate`，直接 `alembic revision -m "xxx"`，然后编辑 `upgrade()` 和 `downgrade()`。

- **与 Flask 集成？**  
  推荐使用 `Flask-Migrate`（基于 Alembic 的封装）：
  ```bash
  pip install Flask-Migrate
  ```
  然后在 Flask app 中：
  ```python
  from flask_migrate import Migrate
  migrate = Migrate(app, db)
  ```
  命令变为：`flask db init`, `flask db migrate -m "..."`, `flask db upgrade`