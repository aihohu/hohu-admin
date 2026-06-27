# 安全规范（SECURITY）

> **主视角**：CTO / 安全负责人
> **受众**：全员（开发 / 运维 / 产品 / 测试）
> **目的**：把认证、授权、数据保护、租户隔离、漏洞响应标准化，让安全成为「设计阶段就考虑」的属性，而不是「上线前补的补丁」。
>
> 本文件覆盖 hohu-admin（后端）为主，前端 / 移动端 / 桌面端的安全要求在各自章节。

---

## 1. 认证（Authentication）

### 1.1 登录机制

| 项 | 值 | 备注 |
|---|---|---|
| Token 类型 | JWT | HS256 |
| 有效期 | 7 天 | refresh token Phase 2+ |
| 携带方式 | `Authorization: Bearer <token>` | |
| 密码哈希 | bcrypt | cost factor = 12 |
| 密码策略 | 最少 8 位，含字母 + 数字 | spec 决策可收紧 |

### 1.2 密码处理

```python
# ✅ 正确：bcrypt 哈希
from app.core.security import hash_password, verify_password

hashed = hash_password(plain)  # 内部 bcrypt
verify_password(plain, hashed)

# ❌ 错误：MD5 / SHA1 / 自实现
import hashlib
hashlib.md5(plain.encode()).hexdigest()  # 禁用
```

**铁律**：
- 密码**永远不进日志**（即使 debug 级别）
- API 响应**永不返回** `hashed_password` 字段（Pydantic Schema 显式 exclude）
- 改密码接口必须验证旧密码（或重新登录）
- 找回密码用一次性 token，禁止明文发送

### 1.3 登录保护

| 攻击 | 防御 |
|---|---|
| 暴力破解 | 5 次失败锁定 15 分钟（按 user_name + IP） |
| 字典攻击 | 密码强度策略 |
| 撞库 | 密码强度 + 异地登录告警（Phase 2） |
| 会话固定 | 登录后重新生成 session id |
| CSRF | 同源 cookie + SameSite=Lax；API 走 Bearer token（天然防 CSRF） |

### 1.4 JWT 实现

```python
from jose import jwt

def create_access_token(data: dict) -> str:
    payload = {**data, "exp": datetime.utcnow() + timedelta(days=7)}
    return jwt.encode(payload, settings.SECRET_KEY, algorithm="HS256")
```

**铁律**：
- `SECRET_KEY` 必须从环境变量读，**禁止**写死代码
- `SECRET_KEY` 至少 32 字符随机串
- 生产环境定期轮换（建议 90 天）
- token 撤销走黑名单（Redis）或版本号（user.token_version）

---

## 2. 授权（Authorization）

### 2.1 RBAC 三层模型

```
User ──role_code──> Role ──menu_id──> Menu (含权限码 type=F)
```

权限码（`Menu.type == 'F'` 的 `permission` 字段）格式：`<module>:<resource>:<action>`

| 模块 | 资源 | 动作 | 例 |
|---|---|---|---|
| sys | user | list/create/edit/delete | `sys:user:list` |
| mk | app | install/uninstall | `mk:app:install` |
| mk | review | approve/reject | `mk:review:approve` |

### 2.2 权限检查

```python
from app.core.auth import require_permissions

@router.post("/install", dependencies=[require_permissions("mk:app:install")])
async def install(...): ...
```

**铁律**：
- 每个**写** API 必须有 `require_permissions`
- 只读 API 至少有 `require_permissions` 或 `require_login`
- `require_permissions(super_admin_only=True)` 仅限超管功能（如全局配置）

### 2.3 super_admin bypass

满足任一即 bypass 所有权限检查：
- `user.user_name == "admin"`
- `user.roles` 含 `R_SUPER` role code

**铁律**：
- `R_SUPER` 角色只给真正的运维负责人
- bypass 逻辑只在 `require_permissions` 装饰器内实现，业务代码**不要**自己写 `if user.is_super`
- super_admin 操作必须额外留审计日志（即使没有 `require_permissions` 标记）

### 2.4 数据级权限（Data Scope）

数据权限模型：`all` / `dept` / `dept_and_below` / `self` / `custom`

```python
# Service 内强制应用 data_scope
stmt = self.scoped(User)  # 自动按 user.data_scope 过滤
```

详见 [`data-scope-guide.md`](./data-scope-guide.md)。

**铁律**：Service 必须用 `self.scoped(Model)` 而非 `select(Model)` —— 后者会跳过 scope 检查。

---

## 3. 多租户隔离（Tenant Isolation）

### 3.1 tenant_id 强制 scope

```python
# ✅ 正确：用 scoped()，自动加 WHERE tenant_id = ?
class MarketplaceBaseService:
    def scoped(self, model):
        return select(model).where(model.tenant_id == self.tenant_id)

# ❌ 错误：直接 select，绕过 scope
stmt = select(App)  # 会查出所有租户数据！
```

**铁律**：
- 所有 Service 继承 `MarketplaceBaseService` 或类似 base，强制 `scoped()`
- 跨租户查询（管理员视角）必须有 `super_admin_only=True` 装饰器 + 审计日志
- Phase 1 单租户（`tenant_id=0`）也要遵守 —— Phase 2 多租户上线时不用改业务代码

### 3.2 app_data_* 表的 tenant_id

应用数据表（如 `app_data_lowcode_crm`）的 system columns 必含 `tenant_id`：

```sql
CREATE TABLE app_data_<slug> (
    id BIGINT PRIMARY KEY,
    tenant_id BIGINT NOT NULL,  -- 必须有
    created_at TIMESTAMPTZ,
    updated_at TIMESTAMPTZ,
    -- 用户字段...
);
CREATE INDEX idx_<slug>_tenant_id ON app_data_<slug>(tenant_id);
```

应用运行时（如 lowcode CRUD）查询必须 `WHERE tenant_id = ?`。

### 3.3 越权检查清单

新 API / Service 必答：
- [ ] 同一租户内不同用户能否访问？（data_scope 处理）
- [ ] 跨租户访问能否阻止？（tenant_id scope）
- [ ] 通过 URL 参数（如 `?user_id=`）能否枚举他人资源？
- [ ] 通过外键关系（如 `app_id`）能否间接访问？

---

## 4. 输入校验与防注入

### 4.1 Pydantic v2 边界校验

```python
from pydantic import BaseModel, Field, field_validator

class UserCreate(BaseModel):
    user_name: str = Field(min_length=3, max_length=50, pattern=r"^[a-zA-Z0-9_]+$")
    password: str = Field(min_length=8, max_length=128)
    email: EmailStr

    @field_validator("password")
    @classmethod
    def password_strength(cls, v: str) -> str:
        if not re.search(r"[A-Za-z]", v) or not re.search(r"\d", v):
            raise ValueError("密码必须含字母和数字")
        return v
```

**铁律**：所有外部输入（HTTP body / query / path / header）必经 Pydantic，禁用 `Request.body()` 直接读 raw。

### 4.2 SQL 注入

```python
# ✅ 正确：参数化
from sqlalchemy import text
await db.execute(text("SELECT * FROM users WHERE name = :name"), {"name": name})

# ✅ 正确：ORM 自动参数化
await db.execute(select(User).where(User.name == name))

# ❌ 错误：f-string 拼接
await db.execute(text(f"SELECT * FROM users WHERE name = '{name}'"))  # 禁用
await db.execute(text(f"DROP TABLE IF EXISTS {table_name}"))  # 危险！

# 例外：DDL 表名（不能参数化）必须显式白名单校验
def safe_table_name(name: str) -> str:
    if not re.match(r"^[a-z_][a-z0-9_]*$", name):
        raise ValueError(f"Invalid table name: {name}")
    return name
```

详见 [`ARCHITECTURE-GUIDELINES.md`](./ARCHITECTURE-GUIDELINES.md) §10 反模式。

### 4.3 XSS（前端）

```vue
<!-- ✅ 正确：默认转义 -->
<div>{{ user_input }}</div>

<!-- ❌ 错误：v-html 直接渲染未消毒输入 -->
<div v-html="user_input"></div>

<!-- 例外：必须 v-html 时强制 sanitization -->
<div v-html="DOMPurify.sanitize(user_input)"></div>
```

### 4.4 CSRF

- API 走 `Authorization: Bearer` → 天然防 CSRF
- 若用 cookie auth → 必须 `SameSite=Lax` + 双重 token
- 状态变更 API（POST/PUT/DELETE）必检 `Origin` / `Referer`

### 4.5 命令注入

```python
# ✅ 正确：subprocess 列表参数
subprocess.run(["git", "clone", url], shell=False)

# ❌ 错误：shell=True + 字符串拼接
subprocess.run(f"git clone {url}", shell=True)  # 禁用
```

CLI / desktop 涉及子进程时严格禁用 `shell=True`。

---

## 5. 文件上传

### 5.1 类型白名单

```python
ALLOWED_MIMES = {
    "image/png", "image/jpeg", "image/webp", "image/gif",
    "application/pdf",
    "application/zip",   # 仅 marketplace app bundle
}
ALLOWED_EXT = {".png", ".jpg", ".jpeg", ".webp", ".gif", ".pdf", ".zip"}

def validate_upload(filename: str, content: bytes) -> None:
    ext = Path(filename).suffix.lower()
    if ext not in ALLOWED_EXT:
        raise InvalidParameterException(f"不允许的文件类型: {ext}")

    # 双重检查：MIME + magic bytes
    mime = magic.from_buffer(content, mime=True)
    if mime not in ALLOWED_MIMES:
        raise InvalidParameterException(f"不允许的 MIME: {mime}")
```

**铁律**：
- 后缀 + MIME + magic bytes **三重校验**
- 不要信任 `Content-Type` header（前端可伪造）

### 5.2 大小限制

```python
MAX_SIZE = 50 * 1024 * 1024  # 50MB（按业务调整）

if len(content) > MAX_SIZE:
    raise InvalidParameterException(f"文件超过 {MAX_SIZE // 1024 // 1024}MB")
```

Nginx / FastAPI 都要配限制（取小者）：
```nginx
client_max_body_size 50M;
```

### 5.3 路径穿越（path traversal）

```python
# ✅ 正确：用 uuid 重命名，原始文件名只存数据库
safe_name = f"{uuid.uuid4().hex}{ext}"
final_path = UPLOAD_DIR / safe_name

# ❌ 错误：直接用上传文件名
final_path = UPLOAD_DIR / filename  # ../../etc/passwd

# ❌ 错误：拼接 zip 内文件路径
import zipfile
with zipfile.ZipFile(zip_path) as zf:
    zf.extractall("/target")  # zip slip 漏洞！
```

**zip slip 防御**：
```python
def safe_extract(zf: zipfile.ZipFile, target: Path) -> None:
    target_resolved = target.resolve()
    for member in zf.namelist():
        member_path = (target / member).resolve()
        if not str(member_path).startswith(str(target_resolved)):
            raise ValueError(f"Path traversal detected: {member}")
```

### 5.4 病毒扫描（Phase 3+）

- 上传后异步走 ClamAV / 物理机病毒库
- marketplace 应用包（zip）必扫
- 扫描结果异步回写 `scan_status` 字段

### 5.5 上传文件存储

| 类型 | 存储位置 |
|---|---|
| 用户头像 / 截图 | 本地 `uploads/` 或 OSS（推荐） |
| marketplace 应用包 | 私有 OSS + 签名 URL（过期时间 1h） |
| 应用数据导出 | 私有 OSS + 一次性签名 URL |

**禁止**把上传文件放在 web root 直接访问 —— 走 `/files/<uuid>` 中间接口，带权限校验。

---

## 6. 第三方集成

### 6.1 API Key 存储

```python
# ✅ 正确：环境变量
api_key = settings.OPENAI_API_KEY

# ✅ 正确：加密后存 DB（用 KMS / vault 主密钥）
encrypted = cipher.encrypt(plain_key)
# 读时 cipher.decrypt(encrypted)

# ❌ 错误：明文写代码 / 配置文件
api_key = "sk-xxx"  # 禁用
```

**铁律**：
- `.env` 文件**永不**进 git（`.gitignore` 已配）
- 生产环境密钥用 vault（HashiCorp Vault / AWS KMS / 阿里云 KMS）
- 密钥轮换流程必须文档化（按 provider 而定）

### 6.2 出站请求

```python
import httpx

# ✅ 正确：超时 + retry + 大小限制
async with httpx.AsyncClient(timeout=30.0) as client:
    try:
        resp = await client.post(url, json=payload)
        resp.raise_for_status()
    except httpx.TimeoutException:
        raise BusinessException("第三方请求超时")
    except httpx.HTTPStatusError as e:
        raise BusinessException(f"第三方返回 {e.response.status_code}")
```

**铁律**：
- 所有出站请求必设 `timeout`（默认 30s，长任务调高）
- SSRF 防御：拒绝访问内网 IP（`127.x` / `10.x` / `192.168.x` / `169.254.169.254`）
- 重试限定次数（最多 3 次，指数退避），禁止死循环

### 6.3 Webhook

**接收方**：
- 必验签（HMAC-SHA256 + 共享 secret）
- 必幂等（按 event_id 去重）
- 必异步处理（先 200 ACK，再处理）

**发送方**（hohu 作为 webhook 触发源）：
- 出站请求带签名 header（`X-Hohu-Signature`）
- 失败走 outbox 重试（详见 [`APP-MARKETPLACE.md`](./APP-MARKETPLACE.md) 决策 #71）
- 用户可配置目标 URL + secret

---

## 7. 加密

### 7.1 At Rest

| 数据 | 加密方式 |
|---|---|
| 密码 | bcrypt（单向哈希，不可逆） |
| API key / 第三方 token | AES-256-GCM + KMS 主密钥 |
| PII（身份证 / 手机） | 仅脱敏存储（不存原文） |
| 数据库备份 | OS 级加密（LUKS / 云盘加密） |

### 7.2 In Transit

- 对外 API：HTTPS（强制 HSTS）
- 内部服务间：mTLS（Phase 3 微服务化后）
- DB 连接：SSL（生产强制）

### 7.3 Hash

- 密码：bcrypt（cost=12）
- 文件指纹：SHA-256（marketplace app bundle）
- 短哈希（如 ID 哈希）：xxHash（非加密用途）

**禁用**：MD5 / SHA1（已不安全）。

---

## 8. PII 与脱敏

### 8.1 PII 识别

| 类别 | 字段例 |
|---|---|
| 高敏 | 身份证 / 护照 / 银行卡 / 医保号 |
| 中敏 | 手机 / 邮箱 / 详细地址 / 出生日期 |
| 低敏 | 姓名 / 性别 / 部门 |

### 8.2 MaskUtil

```python
from app.utils.mask_util import MaskUtil

MaskUtil.mask_phone("13812345678")      # "138****5678"
MaskUtil.mask_email("a@b.com")          # "a***@b.com"
MaskUtil.mask_id_card("110101199001011234")  # "110***********1234"
```

**铁律**：
- API 响应必须脱敏，**永不**返回原文 PII
- DB 存原文（按业务需要）但展示必脱敏
- 日志严禁打 PII（含 debug 级别）

详见 [`app/utils/mask_util.py`](../app/utils/mask_util.py)。

### 8.3 导出脱敏

数据导出（CSV / Excel）默认脱敏，导出原文需：
- 权限：`<module>:data:export_sensitive`
- 审计：导出操作必留日志（who / what / when）

---

## 9. 审计日志（Audit Log）

### 9.1 必审计操作

| 类别 | 操作 |
|---|---|
| 认证 | login / logout / 密码改 / token 撤销 |
| 权限 | role 分配 / 权限变更 |
| 安装 | install / uninstall / enable / disable |
| 数据 | 高敏数据导出 / 批量删除 |
| 系统 | 配置变更 / 模块启用禁用 |

### 9.2 实现

```python
# app/modules/system/models/operation_log.py
class OperationLog(Base):
    __tablename__ = "sys_operation_log"
    id: Mapped[int]
    user_id: Mapped[int]
    user_name: Mapped[str]
    operation: Mapped[str]       # "install_app"
    method: Mapped[str]          # "POST"
    params: Mapped[dict]         # 脱敏后的参数
    url: Mapped[str]
    ip: Mapped[str]
    location: Mapped[str]        # 地理位置（IP 反查）
    status: Mapped[int]          # HTTP status
    error_msg: Mapped[str | None]
    cost_time: Mapped[int]       # ms
    operation_time: Mapped[datetime]
```

### 9.3 安全要求

- **Append-only**：禁止 UPDATE / DELETE（DDL 层限制）
- **不可篡改**：可选加 hash chain（每条 log 含上一条 hash）
- **保留期**：默认 90 天，合规场景 1-7 年（按行业）
- **访问控制**：仅安全 / 审计岗可读

---

## 10. 漏洞响应

### 10.1 报告渠道

- **私有邮箱**：`security@aihohu.com`（不公开 issue）
- **GitHub Security Advisory**：每个子项目仓库都有 `Security` tab → `Report a vulnerability`
- **PGP 公钥**：（远期，发布到 keyserver）

### 10.2 SLA

| 严重度 | 响应 | 修复 |
|---|---|---|
| P0（RCE / SQL 注入 / 越权） | 24h | 7 天内 |
| P1（XSS / 信息泄露） | 48h | 14 天内 |
| P2（CSRF / 弱密码策略） | 72h | 30 天内 |
| P3（best practice 缺失） | 1 周 | 下个 release |

### 10.3 CVE 流程

- 修复后向 [GitHub Advisory Database](https://github.com/advisories) 申请 CVE 编号
- 严重漏洞（CVSS >= 7）发 release notes 显著位置公告
- 受影响用户通过 mailing list / GitHub 通知

### 10.4 事后流程

P0 / P1 漏洞修复后必写 **postmortem**：
- 时间线（发现 → 评估 → 修复 → 公开）
- 根因（5 whys）
- 影响范围（哪些用户 / 数据受影响）
- 改进项（流程 / 代码 / 监控）

---

## 11. CI 安全检查

### 11.1 静态扫描

```yaml
# .github/workflows/security.yml
- name: pip audit
  run: uv run pip-audit

- name: pnpm audit
  run: pnpm audit --prod --audit-level=high

- name: ruff security rules
  run: ruff check --select S .

- name: eslint security
  run: pnpm lint -- --rules-dir eslint-plugin-security
```

### 11.2 频率

| 检查 | 频率 |
|---|---|
| pip / pnpm audit | 每次 PR + 每日 cron |
| 静态扫描 | 每次 PR |
| 依赖升级 | Dependabot 周报 |
| 渗透测试 | 发布前 + 每季度 |

### 11.3 失败阻塞

- Critical 漏洞 → CI fail，阻塞合并
- High 漏洞 → CI warn，但建议修
- Medium / Low → 进 backlog

---

## 12. 反模式（Don't）

| 反模式 | 正解 |
|---|---|
| `SECRET_KEY` 写代码 | 环境变量 |
| raw SQL 用 f-string | 参数化 / ORM |
| 密码进日志 | 永不 log |
| API 返回 hashed_password | Schema 显式 exclude |
| `subprocess(shell=True)` | 列表参数 |
| `v-html="user_input"` | `DOMPurify.sanitize` |
| 上传文件名直接用 | uuid 重命名 |
| 跨租户查询 | `scoped()` 强制 scope |
| super_admin 判断散落业务代码 | 只在 `require_permissions` 内 |
| 全部信任 Content-Type | 后缀 + MIME + magic bytes 三重 |
| 第三方请求不设 timeout | 30s 默认 |
| 审计日志可 UPDATE | append-only |
| 内网请求不防 SSRF | 拒绝 `127.x` / `10.x` / `169.254.169.254` |

---

## 13. 安全 checklist（PR 自检）

PR 涉及以下场景时必填：

### 新 API

- [ ] 有 `require_permissions` 装饰器
- [ ] Pydantic Schema 校验入参
- [ ] 越权检查（同租户 / 跨租户）
- [ ] 不返回敏感字段（密码 / token）

### 数据库变更

- [ ] 不存密码 / PII 原文
- [ ] `tenant_id` 列存在
- [ ] 敏感字段加密（API key 等）

### 文件上传

- [ ] 三重类型校验
- [ ] 大小限制
- [ ] 路径穿越防御
- [ ] uuid 重命名

### 第三方调用

- [ ] 设置 timeout
- [ ] SSRF 防御（拒内网）
- [ ] 重试次数限定

### 权限模型变更

- [ ] 新权限码进入 `sys_menu`（type=F）
- [ ] 角色映射更新
- [ ] 测试覆盖「无权限拒绝」场景

---

## 14. 参考

- 异常层级：[`app/core/exceptions.py`](../app/core/exceptions.py)
- 密码 + JWT：[`app/core/security.py`](../app/core/security.py)
- 权限检查：[`app/core/auth.py`](../app/core/auth.py)
- 脱敏工具：[`app/utils/mask_util.py`](../app/utils/mask_util.py)
- 数据权限：[`data-scope-guide.md`](./data-scope-guide.md)
- 架构边界：[`ARCHITECTURE-GUIDELINES.md`](./ARCHITECTURE-GUIDELINES.md)
- 贡献者协议：[`CONTRIBUTING.md`](./CONTRIBUTING.md)
