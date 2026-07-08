# 数据策略（Data Policy）设计方案

> **状态：设计文档，供后续开发参考，不在当前阶段实施。**
> 当前阶段保留现有的 5 级 `data_scope` 方案，待 ERP/CRM 等业务模块开发时再实施此方案。

## 1. 背景

### 1.1 当前方案

Role 模型有一个 `data_scope` 字段（5 级：全部/自定义/本部门/本部门及以下/仅本人），全局生效，仅按部门维度过滤。适合基础后台管理，无法满足 ERP/CRM 等业务场景。

### 1.2 局限性

| 问题 | 说明 |
|------|------|
| 全局单维度 | 一个角色只有一种 data_scope，不能按模块区分 |
| 仅部门维度 | 不支持区域、项目、业务线等其他维度 |
| 无扩展性 | 新维度需改核心逻辑和数据库结构 |

### 1.3 设计目标

- **按资源配置策略**：不同模块（用户、客户、订单、设备）各自独立的数据权限规则
- **多维度过滤**：部门、区域、项目、业务线等
- **向后兼容**：现有 `data_scope` 字段保留为默认值，无破坏性变更
- **可扩展**：新业务模块可注册自己的维度和解析逻辑

---

## 2. 数据模型

### 2.1 新表：`sys_data_policy`（数据策略）

存储每个角色、每个资源类型、每个维度的权限策略。

| 字段 | 类型 | 约束 | 说明 |
|------|------|------|------|
| `policy_id` | BigInteger | PK | Snowflake ID |
| `role_id` | BigInteger | FK → sys_role, CASCADE | 所属角色 |
| `resource_type` | String(64) | NOT NULL | 资源标识，如 `"system:user"`, `"crm:customer"` |
| `scope_type` | String(2) | NOT NULL | 范围类型：`"1"` 全部 / `"2"` 自定义 / `"3"` 本部门 / `"4"` 本部门及以下 / `"5"` 仅本人 |
| `dimension` | String(64) | NOT NULL, DEFAULT `"dept"` | 过滤维度：`"dept"`, `"region"`, `"project"` 等 |
| `status` | String(1) | NOT NULL, DEFAULT `"1"` | 策略状态 |
| `create_by` | String(64) | NULL | |
| `create_time` | DateTime | server_default=now | |
| `update_by` | String(64) | NULL | |
| `update_time` | DateTime | onupdate=now | |

**唯一约束**：`(role_id, resource_type, dimension)` — 每个角色每个资源每个维度只有一条策略。

### 2.2 新表：`sys_data_policy_value`（策略值）

当 `scope_type = "2"`（自定义）时，存储具体的维度值（部门 ID、区域 ID 等）。

| 字段 | 类型 | 约束 | 说明 |
|------|------|------|------|
| `policy_id` | BigInteger | FK → sys_data_policy, CASCADE | |
| `dimension_value_id` | BigInteger | NOT NULL | 维度值的 ID（如 dept_id、region_id） |

**主键**：`(policy_id, dimension_value_id)`

### 2.3 与现有表的关系

```
sys_role 1---* sys_data_policy (role_id)
sys_data_policy 1---* sys_data_policy_value (policy_id)

# 现有表保留不变
sys_role.data_scope        — 保留，作为未配置策略时的默认回退
sys_role_dept              — 保留，向后兼容
sys_user_role              — 不变
sys_user_dept              — 不变
```

**向后兼容**：`apply_data_policy()` 解析时，优先查 `sys_data_policy`，找不到则回退到 `sys_role.data_scope` + `sys_role_dept`。

---

## 3. 核心设计

### 3.1 维度注册表

模块通过注册表声明支持的维度和解析逻辑：

```python
# app/utils/data_policy.py

class DimensionResolver(Protocol):
    """维度解析器协议"""
    async def resolve(
        self,
        db: AsyncSession,
        user: User,
        model: type,
        scope_type: str,
        value_ids: list[int] | None,
        field_name: str,
    ) -> list[Any]:
        """返回 SQLAlchemy 过滤条件"""
        ...

class DataPolicyRegistry:
    _dimensions: dict[str, type[DimensionResolver]]
    _resources: dict[str, ResourceMeta]  # resource_type -> 元信息

    def register_dimension(self, name: str, resolver: type[DimensionResolver]): ...
    def register_resource(self, resource_type: str, display_name: str,
                          default_dimension: str = "dept"): ...

# 全局单例
data_policy_registry = DataPolicyRegistry()
```

### 3.2 内置维度解析器

**DeptDimensionResolver** — 封装现有 `data_scope.py` 逻辑：
- 处理 scope_type 1-5
- 自定义（"2"）时使用策略值中的部门 ID
- 本部门及以下（"4"）时通过 `Dept.ancestors` 查子部门
- 通过 `field_name` 参数适配不同模型的字段名

### 3.3 核心 API

```python
# app/utils/data_policy.py

async def apply_data_policy(
    db: AsyncSession,
    user: User,
    resource_type: str,
    model: type,
    dimension: str = "dept",
    dept_field: str = "dept_id",
    user_field: str = "create_by",
) -> list[Any]:
    """
    解析 (用户, 资源类型, 维度) 的有效数据策略，返回过滤条件。

    解析顺序：
    1. 超级管理员 → 无过滤
    2. 查 sys_data_policy 中用户角色的匹配策略
    3. 回退到角色的 data_scope 字段（向后兼容）
    4. 都没有 → 默认 DATA_SCOPE_SELF
    """
```

### 3.4 服务层集成方式

```python
# 现有模式（user_service.py 中）
filters = build_filters(User, field_mapping, **query.model_dump())
scope_filters = await get_user_data_scope_filters(db, current_user)
filters.extend(scope_filters)

# 新模式（服务方法中）
filters = build_filters(Customer, field_mapping, **query.model_dump())
policy_filters = await apply_data_policy(
    db, current_user,
    resource_type="crm:customer",
    model=Customer,
    dimension="region",
    dept_field="region_id",
)
filters.extend(policy_filters)
```

显式调用，不使用装饰器，与现有代码风格一致。

---

## 4. API 设计

路由前缀：`/system/data-policy`

| 方法 | 路径 | 说明 |
|------|------|------|
| GET | `/list` | 分页查询策略列表 |
| GET | `/role/{role_id}` | 获取角色的所有策略 |
| GET | `/role/{role_id}/resource/{resource_type}` | 获取角色某资源的策略 |
| POST | `/add` | 创建策略 |
| PUT | `/{policy_id}` | 更新策略 |
| DELETE | `/{policy_id}` | 删除策略 |
| POST | `/batch-delete` | 批量删除 |
| GET | `/resource-types` | 获取所有已注册的资源类型 |
| GET | `/dimensions` | 获取所有已注册的维度 |

---

## 5. Schema 设计

```python
# 请求
class DataPolicyCreate(BaseModel):
    role_id: int
    resource_type: str          # "crm:customer"
    scope_type: str             # "1"-"5"
    dimension: str = "dept"     # "dept", "region", ...
    value_ids: list[int] | None = None  # scope_type="2" 时的具体值

class DataPolicyUpdate(BaseModel):
    scope_type: str | None
    dimension: str | None
    value_ids: list[int] | None
    status: str | None

# 响应
class DataPolicyOut(BaseModel):
    policy_id: int      # 序列化为字符串
    role_id: int
    resource_type: str
    scope_type: str
    dimension: str
    status: str
    value_ids: list[int]  # 从关系提取

# 查询
class DataPolicyQuery(BaseModel):
    current: int = 1
    size: int = 10
    role_id: int | None
    resource_type: str | None
    dimension: str | None

# 资源类型信息
class ResourceTypeOut(BaseModel):
    resource_type: str
    display_name: str
    default_dimension: str
    supported_dimensions: list[str]
```

---

## 6. 文件组织

### 新增文件

```
hohu-admin/
  app/
    modules/system/
      models/data_policy.py            # DataPolicy 模型
      schemas/data_policy.py           # Pydantic schemas
      service/data_policy_service.py   # DataPolicyService
      api/data_policy.py               # API 端点
    utils/
      data_policy.py                   # DataPolicyRegistry, apply_data_policy()
  alembic/versions/
    xxxx_add_data_policy.py            # 迁移
```

### 修改文件

| 文件 | 变更 |
|------|------|
| `app/db/base.py` | 添加 `data_policy_values` 关联表 |
| `app/modules/system/models/role.py` | 添加 `policies` 关系 |
| `app/constants/constants.py` | 添加维度常量 |
| `app/modules/system/service/user_service.py` | 迁移到 `apply_data_policy()` |
| `app/main.py` | 注册新路由 |
| `app/core/exceptions.py` | 添加 `DataPolicyNotFoundException` |

### 前端文件

```
hohu-admin-web/
  src/
    typings/api/system-manage.d.ts     # 添加 DataPolicy 类型
    service/api/system.ts              # 添加策略 API 函数
    views/system/data-policy/index.vue # 策略管理页面
    locales/langs/zh-cn.ts             # i18n
    locales/langs/en-us.ts             # i18n
```

---

## 7. 迁移策略

### 阶段 1：并行运行（无破坏性变更）

1. 创建 `sys_data_policy` 和 `sys_data_policy_value` 表
2. 实现核心逻辑（注册表、解析器、`apply_data_policy()`）
3. 保留 `sys_role.data_scope` 和 `sys_role_dept` 不变
4. `apply_data_policy()` 找不到策略时回退到 `data_scope` → 100% 向后兼容
5. 迁移 `user_service.py` 调用新函数，行为不变

### 阶段 2：模块接入

新模块（CRM、ERP 等）开发时：
1. 注册资源类型：`data_policy_registry.register_resource("crm:customer", "客户管理")`
2. 服务中调用：`await apply_data_policy(db, user, "crm:customer", Customer)`
3. 管理员通过 UI 为角色配置策略

### 阶段 3：清理（远期）

- 弃用角色编辑器中的 `data_scope` 字段（隐藏 UI，保留列）
- 考虑将 `sys_role_dept` 数据迁移到 `sys_data_policy_value`
- 最终移除回退逻辑

---

## 8. 未来扩展点

### 8.1 角色层级（Phase 2）

- `sys_role` 添加 `parent_role_id` 字段
- `apply_data_policy()` 递归收集父角色的策略
- 或新增 scope_type `"6"` = "继承上级角色"

### 8.2 记录共享（Phase 3）

- 新表 `sys_data_share`：`(share_id, resource_type, resource_id, shared_by, shared_with, permission, expire_time)`
- `apply_data_policy()` 在策略过滤后追加共享条件：`OR resource_id IN (shared records)`

### 8.3 字段级权限（Phase 4）

- `sys_data_policy` 添加 `field_rules` JSON 列：`{"hidden": ["salary"], "readonly": ["phone"]}`
- 前端根据策略隐藏/禁用字段
- 后端序列化时排除隐藏字段

### 8.4 自定义维度

模块注册新维度：

```python
# CRM 模块注册"区域"维度
data_policy_registry.register_dimension("region", RegionDimensionResolver)
data_policy_registry.register_resource("crm:customer", "客户管理", default_dimension="region")
```

---

## 9. 设计决策

| 决策 | 理由 |
|------|------|
| 策略独立建表而非扩展 Role 表 | 角色的策略数量不固定，规范化避免稀疏列或 JSON |
| `resource_type` 用字符串而非枚举 | 新模块无需改代码，与菜单 permission 命名风格一致（`"sys:user:list"`） |
| 显式 `apply_data_policy()` 而非装饰器 | 与现有代码风格一致，逻辑透明可见 |
| 保留 `data_scope` 作回退 | 100% 向后兼容，渐进式迁移 |
| 策略值独立关联表而非 JSON 列 | 支持外键约束和高效 JOIN，与现有 `role_depts` 风格一致 |
