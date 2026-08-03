"""ImportValidator 反查校验（v2.2 P0/P1）。

Task 0a 占位：业务在 Task 4/5/6/7/7a 落地，含：
- resolve_dept(dept_input) — 路径 / 名称 / 重名容错，#2.17
- resolve_role_input(role_input) — code/name 双支持 + 去重，#2.18
- check_permission_boundary(operator, requested_role_codes) — 角色越权防御，#2.15
- check_dept_data_scope(operator, dept_ids) — 部门越界防御，#2.11
- _resolve_existing_user(employee_no, user_name) — employee_no 优先，#2.24
"""
