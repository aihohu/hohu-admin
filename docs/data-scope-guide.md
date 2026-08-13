# 数据权限（Data Scope）使用指南

本文档说明如何在 hohu-admin 中使用数据权限，实现「不同角色 / 不同部门的用户看到不同的数据」。

---

## 一、核心概念

数据权限基于**角色的 `data_scope` 字段**实现，共有 5 种范围：

| 值 | 常量 | 含义 | 是否需要配 `dept_ids` |
|---|---|---|---|
| 1 | `DATA_SCOPE_ALL` | 全部数据 | 否 |
| 2 | `DATA_SCOPE_CUSTOM` | 自定义部门 | **是** |
| 3 | `DATA_SCOPE_DEPT` | 本部门 | 否 |
| 4 | `DATA_SCOPE_DEPT_AND_SUB` | 本部门及以下 | 否 |
| 5 | `DATA_SCOPE_SELF` | 仅本人 | 否 |

### 多角色取最大权限

用户若有多个角色，`_get_best_scope()` 会取权限最大的那个（ALL > DEPT_AND_SUB > DEPT > CUSTOM > SELF）。

### 超级管理员免过滤

满足任一条件即跳过数据权限：
- `user_name == "admin"`
- 角色列表包含 `SUPER_ADMIN_ROLE_CODE`（见 `app/constants/constants.py`）

---

## 二、实现位置

| 文件 | 作用 |
|---|---|
| `app/constants/constants.py` | `DATA_SCOPE_*` 常量定义 |
| `app/modules/system/models/role.py` | Role 模型，含 `data_scope` 字段 |
| `app/db/base.py` | 关联表：`user_depts`、`role_depts` 等 |
| `app/utils/data_scope.py` | 核心过滤逻辑 |
| `app/modules/auth/service.py` | `get_current_user()` 预加载 roles / menus |

### 两个过滤函数

| 函数 | 适用场景 |
|---|---|
| `get_data_scope_filters(db, user, model)` | **通用业务表**（模型有 `dept_id` 字段，如订单、文章） |
| `get_user_data_scope_filters(db, user)` | **User 模型专用**（User 通过 `user_depts` 多对多关联部门） |

---

## 三、使用流程（以订单管理为例）

### 场景设定

部门树：
```
集团总部 (id=1)
├── 华北区 (id=2)
│   ├── 北京分公司 (id=3)
│   └── 天津分公司 (id=4)
└── 华南区 (id=5)
    └── 深圳分公司 (id=6)
```

四种角色看订单的范围不同：
- **销售员**（张三，北京）：只看北京订单
- **大区经理**（李四，华北）：看华北+北京+天津订单
- **客服**（王五，北京）：只看自己经手的订单
- **总监**（赵六，集团）：看全部订单

---

### Step 1：业务模型必须包含 `dept_id` 字段

```python
# app/modules/order/models/order.py
from sqlalchemy import BigInteger, Numeric, String
from sqlalchemy.orm import Mapped, mapped_column

from app.core.id_generator import next_id
from app.db.base import Base


class Order(Base):
    __tablename__ = "sys_order"

    order_id: Mapped[int] = mapped_column(
        BigInteger, primary_key=True, default=next_id
    )
    order_no: Mapped[str] = mapped_column(String(64), unique=True)
    amount: Mapped[Decimal] = mapped_column(Numeric(12, 2))

    # ↓↓↓ 数据权限依赖的关键字段 ↓↓↓
    dept_id: Mapped[int] = mapped_column(BigInteger, index=True)   # 归属部门
    create_by: Mapped[int] = mapped_column(BigInteger, index=True)  # 创建人（SELF 用）
    # ↑↑↑
```

> **重要提醒**：创建订单时一定要把**当前用户的主部门 ID** 写进 `dept_id`，否则数据权限会失效。

---

### Step 2：Service 层接入 data scope

```python
# app/modules/order/service/order_service.py
from sqlalchemy.ext.asyncio import AsyncSession

from app.modules.order.models.order import Order
from app.modules.order.schemas.order import OrderQuery
from app.utils.data_scope import get_data_scope_filters
from app.utils.pagination import build_filters, paginate


class OrderService:
    async def get_list(self, db: AsyncSession, query: OrderQuery, current_user):
        """获取订单分页列表（带数据权限过滤）"""
        field_mapping = {
            "order_no": ("order_no", "contains"),
            "status": ("status", "=="),
        }
        filters = build_filters(Order, field_mapping, **query.model_dump())

        # ★ 关键一步：追加数据权限过滤条件
        filters.extend(
            await get_data_scope_filters(db, current_user, Order)
        )

        return await paginate(
            db=db,
            model=Order,
            query_params=query,
            filters=filters,
            order_by=Order.create_time.desc(),
        )


order_service = OrderService()
```

---

### Step 3：API 层注入 `current_user`

```python
# app/modules/order/api/order.py
from fastapi import APIRouter, Depends
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.auth import get_current_user
from app.core.base_response import PageResult, ResponseModel
from app.db.session import get_db
from app.modules.order.schemas.order import OrderItemOut, OrderQuery
from app.modules.order.service.order_service import order_service
from app.modules.system.models.user import User

router = APIRouter()


@router.get(
    "/list",
    response_model=ResponseModel[PageResult[OrderItemOut]],
    summary="获取订单列表",
)
async def get_order_list(
    query: OrderQuery = Depends(),
    db: AsyncSession = Depends(get_db),
    current_user: User = Depends(get_current_user),  # ← 必须注入
):
    page = await order_service.get_list(db, query, current_user)
    return ResponseModel.success(data=page)
```

---

### Step 4：配置角色（关键）

通过 `PUT /system/role/{id}` 或前端「角色管理」界面设置：

| 角色 | role_code | data_scope | dept_ids | 效果 |
|---|---|---|---|---|
| 销售员 | `sales` | **3**（本部门） | — | 仅看 `dept_id = 自己部门` 的订单 |
| 大区经理 | `region_mgr` | **4**（本部门及以下） | — | 按 ancestors 自动展开子部门 |
| 客服 | `cs` | **5**（仅本人） | — | 仅看 `create_by = 自己` 的订单 |
| 总监 | `director` | **1**（全部） | — | 无过滤 |
| 运营专员 | `ops` | **2**（自定义） | `[3, 6]` | 只看北京 + 深圳订单 |

示例请求：
```bash
# 创建「大区经理」角色
POST /system/role/add
{
  "role_name": "大区经理",
  "role_code": "region_mgr",
  "data_scope": 4
}

# 创建「运营专员」角色（自定义部门范围）
POST /system/role/add
{
  "role_name": "运营专员",
  "role_code": "ops",
  "data_scope": 2,
  "dept_ids": [3, 6]
}
```

---

### Step 5：给用户分配部门 + 角色

```bash
# 张三 - 北京销售
PUT /system/user/1001
{
  "dept_ids": [{"dept_id": "3", "is_primary": true}],
  "roles": ["sales"]
}

# 李四 - 华北大区经理
PUT /system/user/1002
{
  "dept_ids": [{"dept_id": "2", "is_primary": true}],
  "roles": ["region_mgr"]
}

# 王五 - 北京客服
PUT /system/user/1003
{
  "dept_ids": [{"dept_id": "3", "is_primary": true}],
  "roles": ["cs"]
}

# 赵六 - 集团总监
PUT /system/user/1004
{
  "dept_ids": [{"dept_id": "1", "is_primary": true}],
  "roles": ["director"]
}
```

---

### Step 6：实际效果验证

不同用户调用同一接口，SQL 自动加不同 WHERE 条件：

```bash
# 张三（北京销售）调列表
GET /order/list
# → SELECT * FROM sys_order WHERE dept_id = 3 ORDER BY ...

# 李四（华北大区经理）调列表
GET /order/list
# → SELECT * FROM sys_order WHERE dept_id IN (2, 3, 4) ORDER BY ...
#   （华北大区的 ancestors 包含 2，子部门按 LIKE 展开）

# 王五（客服）调列表
GET /order/list
# → SELECT * FROM sys_order WHERE create_by = 1003 ORDER BY ...

# 赵六（总监）调列表
GET /order/list
# → SELECT * FROM sys_order ORDER BY ...
#   （data_scope=1，无过滤）
```

---

## 四、字段命名约定

`get_data_scope_filters` 支持自定义字段名：

```python
await get_data_scope_filters(
    db, current_user, Order,
    dept_field="dept_id",    # 默认 "dept_id"，业务表归属部门字段
    user_field="create_by",  # 默认 "create_by"，SELF 范围用的创建人字段
)
```

如果你的业务表用 `owner_dept_id` 或 `creator_id`，传入对应字段名即可。

---

## 五、五种范围的底层逻辑

| data_scope | 生成的 SQL 条件 |
|---|---|
| 1（全部） | （无 WHERE） |
| 2（自定义） | `WHERE dept_id IN (SELECT dept_id FROM role_depts WHERE role_id IN ...)` |
| 3（本部门） | `WHERE dept_id IN (用户所属的部门 ID 列表)` |
| 4（本部门及以下） | `WHERE dept_id IN (用户部门 + 通过 ancestors LIKE 展开的所有子部门)` |
| 5（仅本人） | `WHERE create_by = 当前用户 ID` |

### 「本部门及以下」的展开逻辑

利用 `Dept.ancestors` 字段（如 `"0,1,2"` 表示顶级→集团→华北）。查询时用 LIKE 匹配：
```sql
WHERE ancestors LIKE '%2%'  -- 匹配所有 ancestors 中含 2 的部门（即华北及其子部门）
```

---

## 六、常见问题

### Q1：为什么用户看不到任何数据？

检查清单：
1. 用户是否分配了部门？（`SELECT * FROM user_depts WHERE user_id = ?`）
2. 用户是否分配了角色？（`SELECT * FROM user_roles WHERE user_id = ?`）
3. 角色的 `data_scope` 是否设置正确？
4. 订单的 `dept_id` 是否正确写入了？

### Q2：用户既是销售又是客服，按哪个权限算？

按**最大权限**算。`_get_best_scope` 取所有启用角色中 `data_scope` 最大的，所以销售（DEPT=3）+ 客服（SELF=5）→ 按 DEPT 算。

### Q3：跨部门订单怎么处理？

订单的 `dept_id` 是数据权限的唯一依据。如果想把订单从一个部门转移到另一个部门：
```bash
PUT /order/{id}
{ "dept_id": 4 }  # 改 dept_id 后，权限自动跟着变
```

### Q4：admin 用户能否被过滤？

不能。`is_super_admin()` 会先判断 `user_name == "admin"`，直接返回空过滤列表。

### Q5：能否在前端感知当前用户的数据范围？

可以。前端可以从 `/auth/profile` 拿到当前用户的角色列表，根据 `data_scope` 显示不同的按钮或筛选器。但**后端过滤是不可绕过的**，前端只是 UI 提示。

---

## 七、已接入与待接入

### 角色管理列表展示与筛选

✅ Plan 1 已完成（2026-08-13）：角色管理列表展示每个角色的 `data_scope` 中文名称，并允许通过
`GET /system/role/list?dataScope=<1~5>` 进行等值筛选；Web 新增、搜索和列表共用同一份
五种数据权限映射。

1. **角色列表筛选使用现有 `data_scope` 字段的等值查询** — 该条件描述角色配置本身，
   不改变当前操作者能访问哪些角色的权限边界；查询参数沿用 Pydantic camelCase 别名，
   Web 发送 `dataScope`，后端内部保持 `data_scope`。**反例**: 前端拿到整页数据后本地
   过滤，会造成分页总数和跨页结果错误；接受任意字符串则会隐藏配置错误。
   **回归**: `tests/modules/system/test_role_service.py`、
   `tests/modules/system/test_role_schema.py`、
   `hohu-admin-web/src/views/system/role/__tests__/role-list-data-scope.spec.ts`。

| 模块 | 是否接入 data scope |
|---|---|
| 用户列表（`/system/user/list`） | ✅ 已接入（`user_service.get_user_list`） |
| **数据权限演示（`/system/data-scope-demo/list`）** | ✅ 已接入（`data_scope_demo_service.get_list`，用通用 `get_data_scope_filters`） |
| 订单 / 业务模块 | 参考本指南自行接入 |
| 角色列表 | ⏳ 暂未接入 |
| 部门列表 | ⏳ 暂未接入 |
| 登录日志 / 操作日志 | ⏳ 暂未接入 |

接入方式统一：在对应 Service 的 `get_list` 里加一行 `filters.extend(await get_data_scope_filters(...))` 即可。

### 演示页

`/system/data-scope-demo` 提供一张演示业务表，前端切换登录账号即可看到同一份数据
的不同子集。完整设计、决策记录、演示账号见
[`docs/specs/2026-07-01-data-scope-demo.md`](./specs/2026-07-01-data-scope-demo.md)。

跑 seed：

```bash
cd hohu-admin
uv run python scripts/seed_demo_data_scope.py    # 幂等：6 部门 + 5 角色 + 5 用户 + 30 数据
```

演示账号（密码统一 `demo@12345`）：`demo_all` / `demo_dept_sub` / `demo_dept` /
`demo_custom` / `demo_self`。

---

## 八、相关文件索引

- 常量：`app/constants/constants.py`
- Role 模型：`app/modules/system/models/role.py`
- 关联表：`app/db/base.py`（`user_depts` / `role_depts` / `user_roles` / `role_menus`）
- 过滤工具：`app/utils/data_scope.py`
- 用户列表应用示例：`app/modules/system/service/user_service.py`
- 鉴权依赖：`app/modules/auth/service.py`（`get_current_user`）
- 分页工具：`app/utils/pagination.py`（`build_filters` / `paginate`）
