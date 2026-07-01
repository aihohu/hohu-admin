# 模块开发指南

本文档面向想要为 hohu-admin 开发模块的开发者，详细讲解从零开始开发一个完整模块的全流程。

---

## 概览

### 什么是模块？

模块是一个独立的业务功能包，包含**后端**（Python/FastAPI）和**前端**（Vue 3）两部分。模块可以被用户通过应用商店一键安装，也可以通过命令行安装。

### 一个模块包含什么？

```
hohu-admin-crm/                          # 后端 Python 包
  pyproject.toml                          # 包定义
  hohu_module.yaml                        # 模块元数据（应用商店展示信息）
  hohu_admin_crm/                         # Python 源码
    __init__.py                           # ModuleDefinition 声明（必须）
    api/                                  # API 路由层
      customer.py
    models/                               # 数据模型层
      customer.py
    schemas/                              # 请求/响应 Schema 层
      customer.py
    service/                              # 业务逻辑层
      customer_service.py
    migrations/                           # 数据库迁移
      001_initial.py
    seed.py                               # 初始化数据（菜单、权限）
    dist/                                 # 编译好的前端产物（发布时包含）
  README.md

hohu-admin-crm-ui/                        # 前端 Vue 3 源码包
  package.json
  vite.config.ts
  src/
    views/customer/index.vue              # 页面组件
    api/customer.ts                       # API 调用
    stores/customer.ts                    # Pinia Store
    types/index.ts                        # TypeScript 类型
    index.ts                              # 导出入口
```

### 模块开发者视角的全流程

```
1. 初始化项目 --> hohu create-module crm
2. 开发后端   --> Model -> Schema -> Service -> API
3. 开发前端   --> 页面组件 -> API 调用 -> Store
4. 本地联调   --> hohu dev
5. 编写测试   --> hohu test
6. 构建发布   --> hohu publish
```

---

## 第一步：项目初始化

### 使用 CLI 工具（推荐）

```bash
pip install hohu-admin-cli

hohu create-module crm
```

交互式问答：

```
模块名称 (小写, 用于包名): crm
显示名称: CRM客户管理
分类 (business/tool/integration): business
作者: your-name
描述: 客户关系管理模块
依赖模块 (逗号分隔，默认 system): system
需要生成示例文件? [Y/n]: Y

模块 crm 创建成功！
```

### 手动创建

如果不想用 CLI，手动创建以下结构：

#### 后端项目

```bash
mkdir -p hohu-admin-crm/hohu_admin_crm/{api,models,schemas,service,migrations}
mkdir -p hohu-admin-crm/hohu_admin_crm/dist
touch hohu-admin-crm/hohu_admin_crm/__init__.py
```

`pyproject.toml`:

```toml
[project]
name = "hohu-admin-crm"
version = "0.1.0"
description = "CRM客户管理模块"
requires-python = ">=3.12"
dependencies = [
    "hohu-admin-core>=0.1.0",
]

[project.entry-points."hohu.modules"]
crm = "hohu_admin_crm:module"
```

`hohu_module.yaml`（应用商店元数据）:

```yaml
name: crm
display_name: CRM客户管理
version: 0.1.0
category: business
author: your-name
description: 客户管理、商机跟踪、合同管理
icon: mdi:account-group
screenshots: []
dependencies:
  - system
```

---

## 第二步：模块声明

每个模块必须在 `__init__.py` 中导出一个 `module` 对象：

```python
# hohu_admin_crm/__init__.py
from app.core.module_registry import ModuleDefinition, MenuDefinition
from hohu_admin_crm.api.customer import router as customer_router

module = ModuleDefinition(
    # 基本信息
    name="crm",
    display_name="CRM客户管理",
    version="0.1.0",
    category="business",
    icon="mdi:account-group",
    description="客户管理、商机跟踪、合同管理",
    author="your-name",
    dependencies=["system"],

    # 路由注册（路由器, URL前缀, 标签名）
    routers=[
        (customer_router, "/crm/customer", "客户管理"),
    ],

    # 数据模型模块路径（Alembic 迁移用）
    models_module="hohu_admin_crm.models",

    # 菜单声明（安装时自动写入 sys_menu）
    menus=[
        MenuDefinition(
            name="CRM",
            path="/crm",
            icon="account-group",
            order=100,
            children=[
                MenuDefinition(
                    name="客户管理",
                    path="/crm/customer",
                    component="module:crm/customer/index",
                    permission="crm:customer:list",
                ),
            ],
        ),
    ],

    # 权限声明（安装时自动创建按钮级权限）
    permissions=[
        "crm:customer:list",
        "crm:customer:create",
        "crm:customer:edit",
        "crm:customer:delete",
    ],
)
```

### ModuleDefinition 字段说明

| 字段 | 类型 | 必须 | 说明 |
|------|------|------|------|
| `name` | str | 是 | 模块唯一标识，小写字母 |
| `display_name` | str | 是 | 应用商店显示名称 |
| `version` | str | 是 | 语义化版本号 |
| `category` | str | 是 | 分类：`business` / `tool` / `integration` |
| `dependencies` | list[str] | 否 | 依赖的其他模块名称 |
| `routers` | list[tuple] | 是 | `[(router, prefix, tag), ...]` |
| `models_module` | str | 否 | 模型模块路径，用于 Alembic 发现 |
| `menus` | list[MenuDefinition] | 是 | 安装时创建的菜单树 |
| `permissions` | list[str] | 是 | 安装时创建的权限码 |

### MenuDefinition 字段说明

| 字段 | 类型 | 说明 |
|------|------|------|
| `name` | str | 菜单名称 |
| `path` | str | 前端路由路径 |
| `icon` | str | 图标（MDI 图标名） |
| `component` | str | 前端组件路径，模块组件以 `module:` 开头 |
| `permission` | str | 关联的权限码 |
| `order` | int | 排序号 |
| `children` | list[MenuDefinition] | 子菜单 |

**权限码命名规范：** `模块:资源:操作`，如 `crm:customer:list`

---

## 第三步：开发后端

后端遵循四层架构：**Model -> Schema -> Service -> API**

### 3.1 数据模型（Model）

```python
# hohu_admin_crm/models/customer.py
from datetime import datetime
from sqlalchemy import BigInteger, DateTime, String, func
from sqlalchemy.orm import Mapped, mapped_column
from app.core.id_generator import next_id
from app.db.base import Base


class Customer(Base):
    """客户模型"""
    __tablename__ = "crm_customer"

    # 主键：Snowflake ID，所有表必须这样写
    customer_id: Mapped[int] = mapped_column(
        BigInteger, primary_key=True, default=next_id, comment="客户ID"
    )

    # 业务字段
    name: Mapped[str] = mapped_column(String(100), nullable=False, comment="客户名称")
    contact: Mapped[str | None] = mapped_column(String(50), nullable=True, comment="联系人")
    phone: Mapped[str | None] = mapped_column(String(20), nullable=True, comment="联系电话")
    email: Mapped[str | None] = mapped_column(String(100), nullable=True, comment="邮箱")
    address: Mapped[str | None] = mapped_column(String(255), nullable=True, comment="地址")
    status: Mapped[str] = mapped_column(String(1), default="1", comment="状态: 1=启用, 2=禁用")
    remark: Mapped[str | None] = mapped_column(String(500), nullable=True, comment="备注")

    # 时间戳（所有表都应该有这两个字段）
    create_time: Mapped[datetime] = mapped_column(
        DateTime, server_default=func.now(), comment="创建时间"
    )
    update_time: Mapped[datetime] = mapped_column(
        DateTime, server_default=func.now(), onupdate=func.now(), comment="更新时间"
    )
```

**模型开发要点：**

1. 必须继承 `app.db.base.Base`
2. 主键固定写法：`BigInteger, primary_key=True, default=next_id`
3. 使用 `Mapped[T]` 类型注解 + `mapped_column()`
4. `create_time` 和 `update_time` 固定写法如上
5. 表名建议用 `模块缩写_实体名`（如 `crm_customer`）

关联关系示例：

```python
from typing import TYPE_CHECKING
from sqlalchemy.orm import relationship

if TYPE_CHECKING:
    from .deal import Deal

class Customer(Base):
    # ... 其他字段 ...
    deals: Mapped[list["Deal"]] = relationship(
        "Deal", back_populates="customer", lazy="selectin"
    )
```

模型导出：

```python
# hohu_admin_crm/models/__init__.py
from .customer import Customer
__all__ = ["Customer"]
```

### 3.2 Schema（请求/响应模型）

```python
# hohu_admin_crm/schemas/customer.py
from datetime import datetime
from pydantic import BaseModel, ConfigDict, Field, field_serializer
from pydantic.alias_generators import to_camel
from app.core.config import settings


# ========== 请求 Schema ==========

class CustomerBase(BaseModel):
    """客户基础字段"""
    name: str = Field(..., min_length=1, max_length=100, description="客户名称")
    contact: str | None = Field(None, max_length=50, description="联系人")
    phone: str | None = Field(None, pattern=r"^1[3-9]\d{9}$", description="联系电话")
    email: str | None = Field(None, description="邮箱")
    address: str | None = Field(None, max_length=255, description="地址")
    status: str = Field("1", pattern=r"^[12]$", description="状态")
    remark: str | None = Field(None, max_length=500, description="备注")

    model_config = ConfigDict(alias_generator=to_camel, populate_by_name=True)


class CustomerCreate(CustomerBase):
    """创建客户"""
    pass


class CustomerUpdate(BaseModel):
    """更新客户（所有字段可选）"""
    name: str | None = Field(None, min_length=1, max_length=100)
    contact: str | None = None
    phone: str | None = None
    email: str | None = None
    address: str | None = None
    status: str | None = None
    remark: str | None = None

    model_config = ConfigDict(alias_generator=to_camel, populate_by_name=True)


class CustomerQuery(BaseModel):
    """查询参数"""
    current: int = Field(1, ge=1, description="页码")
    size: int = Field(10, ge=1, le=100, description="每页数量")
    name: str | None = None
    phone: str | None = None
    status: str | None = None

    model_config = ConfigDict(alias_generator=to_camel, populate_by_name=True)


# ========== 响应 Schema ==========

class CustomerOut(BaseModel):
    """客户列表项"""
    customer_id: int
    name: str
    contact: str | None
    phone: str | None
    email: str | None
    status: str
    create_time: datetime | None

    @field_serializer("customer_id")
    def serialize_id(self, v: int, _info):
        return str(v)

    @field_serializer("create_time")
    def serialize_time(self, v: datetime | None, _info):
        return v.strftime(settings.DATETIME_FORMAT) if v else None

    model_config = ConfigDict(from_attributes=True, alias_generator=to_camel)
```

**Schema 开发要点：**

1. 请求 Schema 用 `alias_generator=to_camel, populate_by_name=True`
2. 响应 Schema 额外加 `from_attributes=True`
3. **所有 BigInteger ID 必须用 `@field_serializer` 转为字符串**
4. 查询 Schema 必须有 `current` 和 `size` 字段
5. 更新 Schema 的字段全部可选，配合 `exclude_unset=True` 实现部分更新
6. **datetime 范围查询字段必须用 `LocalNaiveDatetime`**（来自 `app.schemas.types`），不要直接用 `datetime`：
   - DB 列是 `TIMESTAMP WITHOUT TIME ZONE`（naive），前端 NDatePicker 发 ms timestamp，Pydantic 默认解析成 aware datetime 会触发 asyncpg `TypeError: can't subtract offset-naive and offset-aware datetimes` → HTTP 500
   - `LocalNaiveDatetime` 自动按服务器本地时区转 naive，兼容 ms timestamp / 数字字符串 / ISO 字符串 / datetime 输入
   - 设计决策详见 [`specs/2026-07-01-local-naive-datetime.md`](./specs/2026-07-01-local-naive-datetime.md)

   ```python
   from app.schemas.types import LocalNaiveDatetime

   class OrderQuery(BaseModel):
       start_time: LocalNaiveDatetime | None = None
       end_time: LocalNaiveDatetime | None = None
   ```

### 3.3 Service（业务逻辑层）

```python
# hohu_admin_crm/service/customer_service.py
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.exceptions import NotFoundException
from app.utils.pagination import build_filters, paginate
from hohu_admin_crm.models.customer import Customer
from hohu_admin_crm.schemas.customer import CustomerCreate, CustomerQuery, CustomerUpdate


class CustomerNotFoundException(NotFoundException):
    def __init__(self):
        super().__init__(resource_type="客户")


class CustomerService:
    """客户业务逻辑"""

    async def get_list(self, db: AsyncSession, query: CustomerQuery):
        """获取客户分页列表"""
        field_mapping = {
            "name": ("name", "contains"),     # 模糊搜索
            "phone": ("phone", "contains"),   # 模糊搜索
            "status": ("status", "=="),       # 精确匹配
        }
        filters = build_filters(Customer, field_mapping, **query.model_dump())
        return await paginate(
            db=db,
            model=Customer,
            query_params=query,
            filters=filters,
            order_by=Customer.create_time.desc(),
        )

    async def get_by_id(self, db: AsyncSession, customer_id: int) -> Customer:
        """根据 ID 获取客户"""
        result = await db.execute(
            select(Customer).where(Customer.customer_id == customer_id)
        )
        customer = result.scalars().first()
        if not customer:
            raise CustomerNotFoundException()
        return customer

    async def create(self, db: AsyncSession, customer_in: CustomerCreate) -> Customer:
        """创建客户"""
        customer = Customer(**customer_in.model_dump())
        db.add(customer)
        return customer  # 不要在这里 commit！

    async def update(
        self, db: AsyncSession, customer_id: int, customer_in: CustomerUpdate
    ) -> Customer:
        """更新客户"""
        customer = await self.get_by_id(db, customer_id)
        update_data = customer_in.model_dump(exclude_unset=True)
        for field, value in update_data.items():
            setattr(customer, field, value)
        return customer  # 不要在这里 commit！

    async def delete(self, db: AsyncSession, customer_id: int) -> None:
        """删除客户"""
        customer = await self.get_by_id(db, customer_id)
        await db.delete(customer)  # 不要在这里 commit！

    async def batch_delete(self, db: AsyncSession, ids: list[int]) -> None:
        """批量删除客户"""
        for customer_id in ids:
            await self.delete(db, customer_id)


# 单例！整个模块共用这一个实例
customer_service = CustomerService()
```

**Service 开发要点：**

1. **永远不要在 Service 中调用 `db.commit()`** -- API 层负责提交
2. 第一个参数永远是 `db: AsyncSession`
3. 用 `build_filters()` + `paginate()` 实现分页查询
4. 用 `select(Model).where()` 查询单条记录
5. 抛出具体的异常类（继承自 `NotFoundException` 等），不要返回错误码
6. 在文件底部创建单例：`customer_service = CustomerService()`
7. 更新操作用 `model_dump(exclude_unset=True)` + `setattr()` 实现部分更新

### 3.4 API（路由层）

```python
# hohu_admin_crm/api/customer.py
from fastapi import APIRouter, Depends
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.auth import get_current_user
from app.core.base_response import PageResult, ResponseModel
from app.db.session import get_db
from app.modules.system.models.user import User
from hohu_admin_crm.schemas.customer import (
    CustomerCreate, CustomerOut, CustomerQuery, CustomerUpdate,
)
from hohu_admin_crm.service.customer_service import customer_service

router = APIRouter()


@router.get("/list", response_model=ResponseModel[PageResult[CustomerOut]])
async def get_customer_list(
    query: CustomerQuery = Depends(),
    db: AsyncSession = Depends(get_db),
    _current_user: User = Depends(get_current_user),
):
    """获取客户分页列表"""
    page_data = await customer_service.get_list(db, query)
    return ResponseModel.success(data=page_data)


@router.post("/add")
async def create_customer(
    customer_in: CustomerCreate,
    db: AsyncSession = Depends(get_db),
    _current_user: User = Depends(get_current_user),
):
    """创建客户"""
    await customer_service.create(db, customer_in)
    await db.commit()  # 在 API 层提交！
    return ResponseModel.success(msg="创建成功")


@router.put("/{customer_id}")
async def update_customer(
    customer_id: int,
    customer_in: CustomerUpdate,
    db: AsyncSession = Depends(get_db),
    _current_user: User = Depends(get_current_user),
):
    """更新客户"""
    await customer_service.update(db, customer_id, customer_in)
    await db.commit()
    return ResponseModel.success(msg="更新成功")


@router.delete("/{customer_id}")
async def delete_customer(
    customer_id: int,
    db: AsyncSession = Depends(get_db),
    _current_user: User = Depends(get_current_user),
):
    """删除客户"""
    await customer_service.delete(db, customer_id)
    await db.commit()
    return ResponseModel.success(msg="删除成功")


@router.post("/batch-delete")
async def batch_delete_customer(
    ids: list[int],
    db: AsyncSession = Depends(get_db),
    _current_user: User = Depends(get_current_user),
):
    """批量删除客户"""
    await customer_service.batch_delete(db, ids)
    await db.commit()
    return ResponseModel.success(msg="删除成功")
```

**API 开发要点：**

1. 参数顺序固定：路径/请求体参数 -> `db` -> `_current_user`
2. 查询参数用 `Depends()` 方式注入（不是请求体）
3. 写操作（POST/PUT/DELETE）在调用 Service 后 `await db.commit()`
4. 读操作（GET）不需要 commit
5. 所有返回值用 `ResponseModel.success(data=..., msg=...)` 包装
6. 分页接口声明 `response_model=ResponseModel[PageResult[XxxOut]]`
7. 如果需要权限控制，添加 `dependencies=[Depends(require_permissions("crm:customer:list"))]`

---

## 第四步：开发前端

### 前端项目结构

```
hohu-admin-crm-ui/
  package.json
  vite.config.ts
  tsconfig.json
  src/
    views/
      customer/
        index.vue             # 客户列表页
        detail.vue            # 客户详情页
        form.vue              # 新增/编辑表单
    api/
      customer.ts             # 后端 API 调用
    stores/
      customer.ts             # Pinia 状态管理
    types/
      index.ts                # TypeScript 类型定义
    components/
      CustomerSelect.vue      # 可复用组件
    index.ts                  # 导出入口
```

### TypeScript 类型定义

```typescript
// src/types/index.ts

export interface Customer {
  customerId: string        // Snowflake ID 是字符串
  name: string
  contact?: string
  phone?: string
  email?: string
  status: string
  remark?: string
  createTime?: string
}

export interface CustomerQuery {
  current?: number
  size?: number
  name?: string
  phone?: string
  status?: string
}
```

### API 调用

```typescript
// src/api/customer.ts
import request from '@/utils/request'
import type { Customer, CustomerQuery } from '../types'

const BASE_URL = '/crm/customer'

export function getCustomerList(params: CustomerQuery) {
  return request.get(`${BASE_URL}/list`, { params })
}

export function getCustomerById(id: string) {
  return request.get(`${BASE_URL}/${id}`)
}

export function createCustomer(data: Partial<Customer>) {
  return request.post(`${BASE_URL}/add`, data)
}

export function updateCustomer(id: string, data: Partial<Customer>) {
  return request.put(`${BASE_URL}/${id}`, data)
}

export function deleteCustomer(id: string) {
  return request.delete(`${BASE_URL}/${id}`)
}

export function batchDeleteCustomers(ids: string[]) {
  return request.post(`${BASE_URL}/batch-delete`, ids)
}
```

### 页面组件示例

```vue
<!-- src/views/customer/index.vue -->
<template>
  <div class="customer-list">
    <NCard class="mb-4">
      <NForm inline>
        <NFormItem label="客户名称">
          <NInput v-model:value="query.name" placeholder="请输入" clearable />
        </NFormItem>
        <NFormItem label="状态">
          <NSelect v-model:value="query.status" :options="statusOptions" clearable />
        </NFormItem>
        <NButton type="primary" @click="handleSearch">搜索</NButton>
        <NButton @click="handleReset">重置</NButton>
      </NForm>
    </NCard>

    <NCard>
      <NSpace class="mb-4">
        <NButton type="primary" @click="handleAdd">新增客户</NButton>
      </NSpace>
      <NDataTable
        :columns="columns"
        :data="tableData"
        :pagination="pagination"
        :loading="loading"
        remote
        @update:page="handlePageChange"
      />
    </NCard>
  </div>
</template>

<script setup lang="ts">
import { ref, reactive, onMounted } from 'vue'
import { getCustomerList, deleteCustomer } from '../../api/customer'

const loading = ref(false)
const tableData = ref([])
const query = reactive({ name: '', status: null, current: 1, size: 10 })
const pagination = reactive({ page: 1, pageSize: 10, itemCount: 0 })

async function fetchList() {
  loading.value = true
  try {
    const { data } = await getCustomerList(query)
    tableData.value = data.records
    pagination.itemCount = data.total
  } finally {
    loading.value = false
  }
}

function handleSearch() { query.current = 1; fetchList() }
function handleReset() { query.name = ''; query.status = null; handleSearch() }
function handlePageChange(page: number) { query.current = page; fetchList() }
function handleAdd() { /* 打开新增表单弹窗或跳转 */ }

async function handleDelete(id: string) {
  await deleteCustomer(id)
  fetchList()
}

const columns = [
  { title: '客户名称', key: 'name' },
  { title: '联系人', key: 'contact' },
  { title: '电话', key: 'phone' },
  { title: '状态', key: 'status' },
  { title: '创建时间', key: 'createTime' },
  { title: '操作', key: 'actions', fixed: 'right' },
]

onMounted(fetchList)
</script>
```

### 导出入口

```typescript
// src/index.ts
export { default as CustomerList } from './views/customer/index.vue'
export { default as CustomerDetail } from './views/customer/detail.vue'
export { default as CustomerForm } from './views/customer/form.vue'
export * from './types'
```

---

## 第五步：本地联调

### 方式一：CLI 工具

```bash
# 终端 1：启动后端（自动挂载模块）
cd hohu-admin-crm && hohu dev

# 终端 2：启动前端
cd hohu-admin-crm-ui && pnpm dev
```

### 方式二：手动配置

在 hohu-admin 的 `.env` 中添加：

```
INSTALLED_MODULES=["auth","system","crm"]
```

开发模式安装模块：

```bash
cd hohu-admin-crm && pip install -e .
```

前端 Vite 代理配置：

```typescript
// vite.config.ts
export default defineConfig({
  server: { proxy: { '/api': 'http://localhost:8000' } }
})
```

---

## 第六步：数据库迁移

```bash
# 在 hohu-admin 项目中（模块以开发模式安装后）
alembic revision --autogenerate -m "add crm customer table"
alembic upgrade head
```

种子数据（首次安装时自动执行）：

```python
# hohu_admin_crm/seed.py
from sqlalchemy.ext.asyncio import AsyncSession
from app.core.id_generator import next_id
from hohu_admin_crm.models.customer import Customer


async def seed(db: AsyncSession):
    """初始化数据"""
    sample = Customer(
        customer_id=next_id(),
        name="示例客户",
        contact="张三",
        phone="13800138000",
        status="1",
    )
    db.add(sample)
```

---

## 第七步：构建和发布

```bash
# 1. 构建前端
cd hohu-admin-crm-ui && pnpm build

# 2. 拷贝前端产物到后端包
cp -r dist/ ../hohu-admin-crm/hohu_admin_crm/dist/

# 3. 构建 Python 包
cd ../hohu-admin-crm
pip install build && python -m build

# 4. 发布
twine upload dist/hohu_admin_crm-0.1.0-py3-none-any.whl  # PyPI
cd ../hohu-admin-crm-ui && npm publish                     # npm（可选）

# 5. 提交到模块注册中心
# Fork hohu-admin-registry -> 添加模块信息 -> 提交 PR
```

---

## 项目需要提供给开发者的

### 1. Module API 包（hohu-admin-core）

开发者 pip install 后获得的 Python 包，包含所有共享基础设施：

```
app/core/module_registry.py      # ModuleDefinition, MenuDefinition
app/core/auth.py                  # require_permissions(), get_current_user()
app/core/base_response.py         # ResponseModel, PageResult
app/core/exceptions.py            # 所有异常类
app/core/id_generator.py          # next_id()
app/core/security.py              # 密码哈希工具
app/core/config.py                # Settings 基类
app/db/base.py                    # Base 类
app/db/session.py                 # get_db() 依赖
app/utils/pagination.py           # paginate, build_filters
app/utils/mask_util.py            # MaskUtil
app/constants/                    # 常量
```

### 2. CLI 开发工具（hohu-admin-cli）

| 命令 | 功能 |
|------|------|
| `hohu create-module <name>` | 交互式创建模块项目（后端+前端） |
| `hohu dev` | 启动本地开发环境（热重载） |
| `hohu validate` | 验证模块格式是否正确 |
| `hohu test` | 运行模块测试 |
| `hohu build` | 构建前后端产物 |
| `hohu publish` | 发布到 PyPI + npm + 注册中心 |

### 3. 模块模板

CLI 使用的 Cookiecutter 模板，包含完整的示例代码：

```
templates/module/
  backend/
    {{module_name}}/
      __init__.py.jinja          # ModuleDefinition
      api/entity.py.jinja        # CRUD API
      models/entity.py.jinja     # Model
      schemas/entity.py.jinja    # Schema
      service/entity_service.py.jinja  # Service
      seed.py.jinja              # 种子数据
    pyproject.toml.jinja
    hohu_module.yaml.jinja
  frontend/
    src/views/entity/index.vue.jinja    # 列表页
    src/api/entity.ts.jinja             # API 调用
    src/types/index.ts.jinja            # 类型定义
    package.json.jinja
    vite.config.ts.jinja
```

### 4. 文档站点

| 页面 | 内容 |
|------|------|
| 快速开始 | 5 分钟创建第一个模块 |
| Module API 参考 | ModuleDefinition、MenuDefinition 完整字段说明 |
| 后端开发指南 | Model / Schema / Service / API 各层写法 |
| 前端开发指南 | Vue 组件写法、API 调用、状态管理 |
| 发布流程 | 构建、测试、发布的完整流程 |
| 示例模块 | 完整的 CRM 示例模块源码 |

### 5. 模块注册中心

GitHub 仓库 `hohu-admin-registry`，接受模块作者 PR：

```json
{
  "modules": [
    {
      "name": "crm",
      "display_name": "CRM客户管理",
      "version": "0.1.0",
      "category": "business",
      "author": "hohu",
      "description": "客户管理、商机跟踪、合同管理",
      "icon": "mdi:account-group",
      "pypi_package": "hohu-admin-crm",
      "npm_package": "hohu-admin-crm-ui",
      "source_url": "https://github.com/xxx/hohu-admin-crm"
    }
  ]
}
```

---

## 开发速查表

### 后端可用导入

```python
# 数据库
from app.db.base import Base                              # ORM 基类
from app.db.session import get_db                         # 数据库会话依赖

# ID 生成
from app.core.id_generator import next_id                 # Snowflake ID

# 认证授权
from app.core.auth import require_permissions             # 权限检查
from app.modules.auth.service import get_current_user     # 当前用户

# 响应格式
from app.core.base_response import ResponseModel, PageResult

# 异常
from app.core.exceptions import (
    NotFoundException, DuplicateException, ValidationException,
    AuthenticationException, AuthorizationException, BusinessRuleException,
)

# 分页
from app.utils.pagination import paginate, paginate_custom, build_filters, QueryParams

# 安全
from app.core.security import get_password_hash, verify_password

# 数据脱敏
from app.utils.mask_util import MaskUtil

# 配置
from app.core.config import settings

# 常量
from app.constants import STATUS_ENABLED, STATUS_DISABLED
```

### 命名规范

| 场景 | 规范 | 示例 |
|------|------|------|
| 表名 | `模块_实体` | `crm_customer` |
| 主键字段 | `实体_id` | `customer_id` |
| 权限码 | `模块:资源:操作` | `crm:customer:list` |
| API 路径 | `/模块/资源` | `/crm/customer` |
| Service 文件 | `实体_service.py` | `customer_service.py` |
| Service 单例 | `实体_service` | `customer_service` |
| Schema 创建 | `实体Create` | `CustomerCreate` |
| Schema 更新 | `实体Update` | `CustomerUpdate` |
| Schema 查询 | `实体Query` | `CustomerQuery` |
| Schema 响应 | `实体Out` | `CustomerOut` |

### 完整数据流

```
前端请求 (camelCase)
  userName=xxx&current=1&size=10
       |
       v
API 层 (FastAPI 路由)
  Depends(get_db) -> 注入数据库会话
  Depends(get_current_user) -> 认证
  CustomerQuery = Depends() -> 解析查询参数
       |
       v
Service 层 (业务逻辑)
  build_filters() -> 构建查询条件
  paginate() -> 执行分页查询
  raise NotFoundException() -> 抛异常
       |
       v
API 层 (提交+返回)
  await db.commit() -> 提交事务
  ResponseModel.success(data=...) -> 包装响应
       |
       v
前端响应 (camelCase)
  { code: 200, data: { records: [...], total: 100 } }
```
