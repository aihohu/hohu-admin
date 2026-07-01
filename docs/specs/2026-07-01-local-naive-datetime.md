# LocalNaiveDatetime：统一 datetime 范围查询类型

> 状态：✅ Plan 已完成（2026-07-01）
>
> 通过抽出一个 Pydantic 类型，根治"前端 NDatePicker 时间范围搜索 → 后端 500"的反复陷阱。

## 背景

历史上有 3 个 Query schema（`LoginLogQuery` / `OperationLogQuery` / `JobLogQuery`）各
写了一份相同的 `_strip_tzinfo` field_validator，把前端传来的 tz-aware datetime 剥成
naive UTC，避免 asyncpg 把 aware datetime 绑定到 `TIMESTAMP WITHOUT TIME ZONE` 列
时抛 `TypeError: can't subtract offset-naive and offset-aware datetimes`。

但 `JobLogQuery` 当时漏了这个 validator → 用户在前端选 datetimerange 后，API 直接
500。**根因不在某个 schema，而在"每个新模块都要重学一遍这个坑"**——这是架构层面的
缺失。

本次新增 `app/schemas/types.py:LocalNaiveDatetime` 类型，统一处理 ms timestamp /
数字字符串 / ISO 字符串 / datetime 输入，转成 naive **本地** datetime；前端配套改
成直接发 NDatePicker 原生的 ms timestamp，不再 `toISOString()`。

---

## 决策记录

### 1. **抽 Pydantic 类型而非保留 schema-level validator** —
3 个 schema 抄同一份 validator 已经是 DRY 违反；下一个写"审计日志查询"的人还会
重踩。抽成 `Annotated[datetime, BeforeValidator(...)]` 类型后，新模块只需用类型，
不需要懂底层 tz 处理。**反例**：维持现状，靠 code review 抓遗漏 → 已经证明不可靠
（JobLogQuery 漏写就上了生产）。**回归**：`tests/schemas/test_types.py` 9 个 case
覆盖 ms timestamp / 数字字符串 / ISO / datetime 各种输入；3 个 Query schema 用
新类型后原服务测试不变。

### 2. **按服务器本地时区转 naive，不按 UTC** —
DB 写入用 `func.now()` / `datetime.now()`，结果都是 naive 本地时间（无 tz 标签但
数值是本地）。读取过滤必须用同一基准，否则跨时区部署（UTC 云主机 + 中国用户）会
出现"用户选 7/1 00:00 → 后端按 UTC 00:00 查 → 实际比对 6/30 16:00 → 少看 8 小时
数据"。**反例**：旧的 `_strip_tzinfo` 用 `v.astimezone(UTC).replace(tzinfo=None)`
→ 转 naive UTC，在 UTC 服务器上没事，但本地部署时差 8 小时（用户体感"我选了今天
但今天的数据没出现"）。**回归**：`test_z_suffix_converted_to_local_naive` 用
`astimezone().replace(tzinfo=None)`（无参数 astimezone = 系统时区）作期望值。

### 3. **前端发 ms timestamp，不发 ISO 字符串** —
NDatePicker 的 v-model:value 原生就是 `[number, number]`（unix ms），无需任何转换。
直接发 `value[0]` / `value[1]` 到后端，类型透明。**反例**：旧代码
`new Date(value[0]).toISOString()` 把本地时间转 UTC ISO 字符串——这步转换没意义
（后端最终要比对本地），还引入"UTC 偏差" + "Pydantic aware datetime 解析"
双重 bug 风险。**回归**：3 个 vue 改成直接发 timestamp，typings `startTime: string`
改 `number`，前端 typecheck 干净。

### 4. **同时识别数字字符串作为 ms timestamp** —
FastAPI query string 透传时，即使前端发 `startTime=1751241600000`（数字），后端
Pydantic 看到的是 str `"1751241600000"`。BeforeValidator 必须先尝试解析为 int 才能
走到 timestamp 分支。**反例**：只 `isinstance(v, int)` 检查 → str 走 ISO 分支 →
`datetime.fromisoformat("1751241600000")` → `ValueError: month must be in 1..12`
→ HTTP 422。**回归**：`test_numeric_string_treated_as_ms_timestamp` 用数字字符串
输入断言正确转换为 datetime。

### 5. **ms timestamp 区间限制 `[1e12, 1e13)`** —
合理 ms timestamp 落在 2001-09-09 ~ 2286-11-20。秒级 timestamp（10 位）落在区间外，
被拒绝并报错"必须是毫秒级"。**反例**：不限制 → 前端误传秒级 `1700000000` 被当成
ms 解析 → 得到 1970-01-20 → 静默错乱。**回归**：`test_seconds_timestamp_rejected`
断言 ValidationError。

### 6. **兼容 ISO 字符串输入（不强制只接受 timestamp）** —
虽然前端统一改发 timestamp，但后端不能假设只有前端会调 API：curl 手测、Postman
collection、第三方集成可能传 ISO 字符串。BeforeValidator 第二个分支保留 ISO 解析
能力（带或不带 tz 后缀都行）。**反例**：硬性要求 timestamp → 任何 ISO 输入报错
→ API 兼容性差，违反 RESTful 通用约定。**回归**：`TestFromISO` 3 个 case 覆盖
`Z` / `+08:00` / 无 tz 三种 ISO 变体；`test_iso_with_z_does_not_raise` 在 JobLogQuery
服务测试层验证。

### 7. **类型放 `app/schemas/types.py` 而非 `app/utils/`** —
现有 `app/utils/validators.py` 装的是函数（validate_password 等），而 Pydantic
Annotated 类型是另一类抽象。新建 `app/schemas/` 顶层模块专门放跨模块复用的 schema
类型，跟 SQLAlchemy 类型装饰器（`app/db/`）形成对称结构。后续若新增
`SnowflakeId`、`TimeString` 等通用类型也归这里。**反例**：塞进 validators.py 让
文件语义混杂；放 `app/core/` 又跟 config/auth/redis 这些基础设施混淆。
**回归**：CLAUDE.md 加 "Project Structure Note" 指明此约定。

---

## 已接入模块清单

| 文件 | 改动 |
|---|---|
| `app/schemas/__init__.py` | 新建空文件 |
| `app/schemas/types.py` | 新建：`LocalNaiveDatetime` + `_to_local_naive_datetime` |
| `app/modules/job/schemas/job.py` | `JobLogQuery.start_time/end_time` 改用类型，删除 `_strip_tzinfo` |
| `app/modules/system/schemas/login_log.py` | `LoginLogQuery.start_time/end_time` 改用类型，删除 `_strip_tzinfo` |
| `app/modules/system/schemas/operation_log.py` | `OperationLogQuery.start_time/end_time` 改用类型，删除 `_strip_tzinfo` |
| `tests/schemas/__init__.py` + `test_types.py` | 新建：9 个单元测试 |
| `tests/modules/job/__init__.py` + `conftest.py` + `test_job_log_service.py` | 新建：6 个服务层测试 |
| 前端 `views/system/{job-log,login-log,operation-log}/modules/*-search.vue` | `handleDateRangeChange` 直接发 ms timestamp |
| 前端 `typings/api/system-manage.d.ts` | 3 处 `startTime: string` → `number` |
| `CLAUDE.md` | Common Pitfalls 第 12 条 + Project Structure Note |

## 用法

后端：

```python
from app.schemas.types import LocalNaiveDatetime

class OrderQuery(BaseModel):
    start_time: LocalNaiveDatetime | None = None
    end_time: LocalNaiveDatetime | None = None
```

前端（NDatePicker 配套）：

```vue
<script setup lang="ts">
function handleDateRangeChange(value: [number, number] | null) {
  if (value) {
    model.value.startTime = value[0];  // 直接发 ms timestamp
    model.value.endTime = value[1];
  } else {
    model.value.startTime = null;
    model.value.endTime = null;
  }
}
</script>
```

## 验证步骤

```bash
cd hohu-admin
python -m pytest tests/schemas/ tests/modules/job/ -v   # 15 个新测试
python -m pytest                                          # 345 全量
ruff check . && ruff format .

cd ../hohu-admin-web
pnpm lint && pnpm typecheck

# HTTP e2e
TOKEN=$(curl -s -X POST http://127.0.0.1:8000/auth/login \
  -H "Content-Type: application/json" \
  -d '{"userName":"admin","password":"123456"}' \
  | python -c "import sys,json; print(json.load(sys.stdin)['data']['token'])")
curl -s "http://127.0.0.1:8000/system/job-log/list?startTime=1751241600000&endTime=1751327999999" \
  -H "Authorization: Bearer $TOKEN"
```

## 参考借鉴

- 数据范围 demo spec：[`2026-07-01-data-scope-demo.md`](./2026-07-01-data-scope-demo.md)（决策记录格式标杆）
- 数据策略设计：[`2026-04-29-data-policy-design.md`](./2026-04-29-data-policy-design.md)
- CLAUDE.md Common Pitfalls 第 12 条：`hohu-admin/CLAUDE.md`
