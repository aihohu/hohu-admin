"""Excel/CSV 解析 + 字段校验（spec §3.6 line 2036, Task 8）。

职责：
- ``parse_import_excel(file_bytes, mime_type) -> list[UserImportRecord]``
- MIME 白名单 / 文件大小 / 行数硬上限校验（spec §2.10）
- 字段级格式校验：必填 / 长度 / 邮箱 / 手机号 / gender / status（spec §2.12）
- 失败行一次性抛 ``ImportErrorCollection``（不一次一个）
- ``employee_no`` 空串 → ``None`` 规范化（spec §2.24）

**不在本模块做**：
- ``dept_input`` / ``role_input`` 存在性反查（留给 dry_run，spec line 2045）
- ``file_sha256`` 计算（dry_run 调用方算，spec line 2056）
- DB 查询（解析层纯函数，便于复用 + 单测）

user_service.parse_import_excel（service 层）是 thin wrapper：直接 delegate 到本模块。
"""

import csv
import io
import re

from openpyxl import load_workbook

from app.core.exceptions import BusinessRuleException
from app.modules.system.user.constants import USER_IMPORT_MAX_ROWS
from app.modules.system.user.schemas import FailedRow, UserImportRecord

#: spec §2.10 line 277 MIME 白名单
MIME_XLSX = "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet"
MIME_XLS = "application/vnd.ms-excel"
MIME_CSV = "text/csv"
ALLOWED_MIME_TYPES: frozenset[str] = frozenset({MIME_XLSX, MIME_XLS, MIME_CSV})

#: spec §2.10：≤ 10MB（常量在模块内，避免 settings 误改导致安全边界漂移）
MAX_FILE_SIZE_BYTES = 10 * 1024 * 1024

#: 模板表头顺序（与 Task 14 模板下载一致；解析时按表头名称匹配列索引）
EXCEL_HEADERS: tuple[str, ...] = (
    "user_name",
    "employee_no",
    "nickname",
    "user_email",
    "user_phone",
    "dept_input",
    "role_input",
    "user_gender",
    "status",
)

#: 邮箱格式（简单 RFC 5322 子集，足够滤掉常见错字）
_EMAIL_RE = re.compile(r"^[a-zA-Z0-9._%+\-]+@[a-zA-Z0-9.\-]+\.[a-zA-Z]{2,}$")
#: 中国大陆手机号格式（11 位，1 开头，第二位 3-9）
_PHONE_RE = re.compile(r"^1[3-9]\d{9}$")

#: gender / status 合法取值（与 UserImportRecord Literal 对齐）
_GENDER_VALUES: frozenset[str] = frozenset({"0", "1", "2"})
#: v2.3 §2.9.1 修订：status 取值对齐 DB / 前端 / 其他模块真实约定 ("1","2")
#: （原 {"0","1"} 是 spec §3.1 line 1634 笔误，会拦掉真实合法的 "2" 禁用用户）。
_STATUS_VALUES: frozenset[str] = frozenset({"1", "2"})

#: v2.3 §2.9.1：中文字面值反查表（导出 Excel 翻译后的值可 round-trip 给导入）。
#: 反向与 export_service._STATUS_LABELS / _GENDER_LABELS 一一对应。
#: 反查失败 fallback 到字面值继续走 _STATUS_VALUES / _GENDER_VALUES 校验。
_STATUS_LABELS_INV: dict[str, str] = {"启用": "1", "禁用": "2"}
_GENDER_LABELS_INV: dict[str, str] = {"未知": "0", "男": "1", "女": "2"}


class ImportErrorCollection(Exception):
    """字段格式校验失败集合（spec §2.12 + §3.6 line 2046）。

    一次收集所有 FailedRow（避免用户改一行重传一次）。HTTP 层 catch 后转
    400 响应，errorCode 由前端 i18n 表兜底（ ``AI_IMPORT_FIELD_ERRORS`` 或
    直接展示 ``errors[].error_code``）。

    Attributes:
        errors: 所有失败行的 FailedRow 列表（按 row_num 升序）
    """

    def __init__(self, errors: list[FailedRow]) -> None:
        self.errors = errors
        super().__init__(f"{len(errors)} field errors")


def parse_import_excel(file_bytes: bytes, mime_type: str) -> list[UserImportRecord]:
    """解析 Excel/CSV → 验证 → 返回 records（spec §3.6 line 2036, Task 8）。

    流程：
    1. MIME 白名单校验 → ``AI_IMPORT_INVALID_MIME``
    2. 文件大小校验 → ``AI_IMPORT_FILE_TOO_LARGE``
    3. 解析为 raw rows（xlsx 用 openpyxl / csv 用标准库）
    4. 行数硬上限校验 → ``AI_IMPORT_TOO_MANY_ROWS``
    5. 每行字段校验，累积 FailedRow
    6. 任一 FailedRow → 抛 ``ImportErrorCollection``（含全部错误）

    Args:
        file_bytes: 文件二进制内容
        mime_type: Content-Type，必须在 ALLOWED_MIME_TYPES 内

    Returns:
        list[UserImportRecord]：合法行（按 Excel 行号升序）

    Raises:
        BusinessRuleException: ``AI_IMPORT_INVALID_MIME`` / ``AI_IMPORT_FILE_TOO_LARGE``
            / ``AI_IMPORT_TOO_MANY_ROWS``
        ImportErrorCollection: 含所有字段格式 FailedRow（即使有合法行，只要有错就抛）
    """
    if mime_type not in ALLOWED_MIME_TYPES:
        raise BusinessRuleException(
            f"不支持的文件类型: {mime_type}（允许 xlsx/xls/csv）",
            error_code="AI_IMPORT_INVALID_MIME",
        )

    if len(file_bytes) > MAX_FILE_SIZE_BYTES:
        raise BusinessRuleException(
            f"文件超过 {MAX_FILE_SIZE_BYTES // (1024 * 1024)}MB 限制"
            f"（实际 {len(file_bytes) / (1024 * 1024):.1f}MB）",
            error_code="AI_IMPORT_FILE_TOO_LARGE",
        )

    if mime_type == MIME_CSV:
        raw_rows = _parse_csv_rows(file_bytes)
    else:
        raw_rows = _parse_xlsx_rows(file_bytes)

    if len(raw_rows) > USER_IMPORT_MAX_ROWS:
        raise BusinessRuleException(
            f"行数超 {USER_IMPORT_MAX_ROWS} 上限（实际 {len(raw_rows)} 行），"
            "请分批导入或等待 Phase 3 异步通道",
            error_code="AI_IMPORT_TOO_MANY_ROWS",
        )

    records: list[UserImportRecord] = []
    errors: list[FailedRow] = []
    for idx, raw in enumerate(raw_rows, start=2):  # row 1 是表头，数据从 row 2 起
        record, row_errors = _validate_row(idx, raw)
        if record is not None:
            records.append(record)
        errors.extend(row_errors)

    if errors:
        raise ImportErrorCollection(errors)

    return records


def _parse_xlsx_rows(file_bytes: bytes) -> list[dict[str, str]]:
    """openpyxl 解析 xlsx/xls → list[row_dict]（按 EXCEL_HEADERS 提取列）。

    - read_only + data_only 模式（不加载公式 / 样式）
    - 表头按名称匹配（大小写不敏感），允许列顺序变化
    - 完全空行跳过（防止模板尾部空白行被误判）
    - 单元格 None → ""（统一字符串处理）
    """
    wb = load_workbook(io.BytesIO(file_bytes), read_only=True, data_only=True)
    try:
        ws = wb.active
        rows_iter = ws.iter_rows(values_only=True)
        try:
            header_row = next(rows_iter)
        except StopIteration:
            return []
        header_idx = _resolve_header_indices(header_row)

        result: list[dict[str, str]] = []
        for raw in rows_iter:
            if raw is None or all(c is None or str(c).strip() == "" for c in raw):
                continue
            row_dict: dict[str, str] = {}
            for fname, i in header_idx.items():
                if i is None or i >= len(raw) or raw[i] is None:
                    row_dict[fname] = ""
                else:
                    row_dict[fname] = str(raw[i]).strip()
            result.append(row_dict)
        return result
    finally:
        wb.close()


def _parse_csv_rows(file_bytes: bytes) -> list[dict[str, str]]:
    """csv 标准库解析 → list[row_dict]。

    - utf-8-sig 解码（兼容 Excel 导出的 BOM）
    - 表头大小写不敏感匹配
    - 完全空行跳过
    """
    text = file_bytes.decode("utf-8-sig", errors="strict")
    reader = csv.DictReader(io.StringIO(text))
    fieldnames = reader.fieldnames or []
    header_idx = _resolve_header_indices(fieldnames)

    result: list[dict[str, str]] = []
    for raw in reader:
        if all((v or "").strip() == "" for v in raw.values()):
            continue
        row_dict: dict[str, str] = {}
        for fname, _ in header_idx.items():
            # DictReader 用原始 fieldnames 索引，需要回查
            csv_key = _find_csv_key_for_field(fieldnames, fname)
            cell = raw.get(csv_key) if csv_key else None
            row_dict[fname] = "" if cell is None else str(cell).strip()
        result.append(row_dict)
    return result


def _resolve_header_indices(header_row) -> dict[str, int | None]:
    """表头行 → {field_name: col_index | None}。

    表头大小写不敏感匹配；缺失字段的 col_index = None，_validate_row 视为空值。
    """
    lowered: dict[str, int] = {}
    for i, c in enumerate(header_row):
        if c is None:
            continue
        lowered.setdefault(str(c).strip().lower(), i)
    return {fname: lowered.get(fname.lower()) for fname in EXCEL_HEADERS}


def _find_csv_key_for_field(fieldnames: list[str], fname: str) -> str | None:
    """DictReader 用原始 key 取值，需要大小写不敏感回查。"""
    for fn in fieldnames:
        if fn and fn.strip().lower() == fname.lower():
            return fn
    return None


def _validate_row(
    row_num: int, raw: dict[str, str]
) -> tuple[UserImportRecord | None, list[FailedRow]]:
    """单行 → (record | None, [failed_rows])。

    校验顺序：必填 → 长度 → 格式 → 取值。一行可产生多个 FailedRow。
    任一错误 → record 为 None（保证返回的 records 都是合法的）。
    """
    errors: list[FailedRow] = []

    def _get(name: str) -> str:
        v = raw.get(name)
        return "" if v is None else str(v).strip()

    # user_name 必填 + 长度 2-16（统一用 AI_IMPORT_USERNAME_INVALID）
    user_name = _get("user_name")
    if not user_name:
        errors.append(
            FailedRow(
                row_num=row_num,
                field="user_name",
                value=user_name,
                reason="user_name 必填",
                error_code="AI_IMPORT_USERNAME_INVALID",
            )
        )
    elif len(user_name) < 2 or len(user_name) > 16:
        errors.append(
            FailedRow(
                row_num=row_num,
                field="user_name",
                value=user_name,
                reason="user_name 长度需 2-16 字符",
                error_code="AI_IMPORT_USERNAME_INVALID",
            )
        )

    # employee_no 可选，max 64；空串 → None（spec §2.24）
    employee_no_raw = _get("employee_no")
    employee_no: str | None = employee_no_raw or None
    if employee_no and len(employee_no) > 64:
        errors.append(
            FailedRow(
                row_num=row_num,
                field="employee_no",
                value=employee_no,
                reason="employee_no 长度超 64",
                error_code="AI_IMPORT_EMPLOYEE_NO_TOO_LONG",
            )
        )

    # nickname 可选，max 16
    nickname_raw = _get("nickname")
    nickname: str | None = nickname_raw or None
    if nickname and len(nickname) > 16:
        errors.append(
            FailedRow(
                row_num=row_num,
                field="nickname",
                value=nickname,
                reason="nickname 长度超 16",
                error_code="AI_IMPORT_NICKNAME_TOO_LONG",
            )
        )

    # user_email 可选，max 128，邮箱格式
    email_raw = _get("user_email")
    email: str | None = email_raw or None
    if email:
        if len(email) > 128:
            errors.append(
                FailedRow(
                    row_num=row_num,
                    field="user_email",
                    value=email,
                    reason="user_email 长度超 128",
                    error_code="AI_IMPORT_EMAIL_INVALID",
                )
            )
        elif not _EMAIL_RE.match(email):
            errors.append(
                FailedRow(
                    row_num=row_num,
                    field="user_email",
                    value=email,
                    reason="邮箱格式错",
                    error_code="AI_IMPORT_EMAIL_INVALID",
                )
            )

    # user_phone 可选，max 32，中国大陆手机号格式
    phone_raw = _get("user_phone")
    phone: str | None = phone_raw or None
    if phone:
        if len(phone) > 32:
            errors.append(
                FailedRow(
                    row_num=row_num,
                    field="user_phone",
                    value=phone,
                    reason="user_phone 长度超 32",
                    error_code="AI_IMPORT_PHONE_INVALID",
                )
            )
        elif not _PHONE_RE.match(phone):
            errors.append(
                FailedRow(
                    row_num=row_num,
                    field="user_phone",
                    value=phone,
                    reason="手机号格式错（11 位数字，1 开头）",
                    error_code="AI_IMPORT_PHONE_INVALID",
                )
            )

    # dept_input 必填
    dept_input = _get("dept_input")
    if not dept_input:
        errors.append(
            FailedRow(
                row_num=row_num,
                field="dept_input",
                value=dept_input,
                reason="dept_input 必填（部门名或完整路径）",
                error_code="AI_IMPORT_DEPT_INPUT_REQUIRED",
            )
        )

    # role_input 可选
    role_input_raw = _get("role_input")
    role_input: str | None = role_input_raw or None

    # user_gender Literal["0","1","2"]，默认 "0"
    # v2.3 §2.9.1：先反查中文字面值（"男"/"女"/"未知"），未命中走原字面值
    gender_input = _get("user_gender") or "0"
    gender_raw = _GENDER_LABELS_INV.get(gender_input, gender_input)
    if gender_raw not in _GENDER_VALUES:
        errors.append(
            FailedRow(
                row_num=row_num,
                field="user_gender",
                value=gender_input,
                reason="user_gender 取值需 0/1/2 或 未知/男/女",
                error_code="AI_IMPORT_GENDER_INVALID",
            )
        )
        gender = "0"
    else:
        gender = gender_raw

    # status Literal["1","2"]（v2.3 §2.9.1 修订对齐 DB），默认 "1"
    # v2.3 §2.9.1：先反查中文字面值（"启用"/"禁用"），未命中走原字面值
    status_input = _get("status") or "1"
    status_raw = _STATUS_LABELS_INV.get(status_input, status_input)
    if status_raw not in _STATUS_VALUES:
        errors.append(
            FailedRow(
                row_num=row_num,
                field="status",
                value=status_input,
                reason="status 取值需 1/2 或 启用/禁用",
                error_code="AI_IMPORT_STATUS_INVALID",
            )
        )
        status = "1"
    else:
        status = status_raw

    if errors:
        return None, errors

    record = UserImportRecord(
        row_num=row_num,
        user_name=user_name,
        employee_no=employee_no,
        nickname=nickname,
        user_email=email,
        user_phone=phone,
        dept_input=dept_input,
        role_input=role_input,
        user_gender=gender,
        status=status,
    )
    return record, []


__all__ = [
    "ALLOWED_MIME_TYPES",
    "EXCEL_HEADERS",
    "MAX_FILE_SIZE_BYTES",
    "MIME_CSV",
    "MIME_XLS",
    "MIME_XLSX",
    "ImportErrorCollection",
    "parse_import_excel",
]
