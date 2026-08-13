# ruff: noqa: T201

"""
菜单增量同步工具

按 route_name 去重，只添加数据库中不存在的菜单，已存在则跳过。
适用于版本升级时新增菜单，可安全重复执行。

Usage:
    cd hohu-admin
    python scripts/sync_menus.py
"""

import asyncio

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession, create_async_engine
from sqlalchemy.orm import sessionmaker

from app.core.config import settings
from app.core.id_generator import next_id
from app.modules.system.models.menu import Menu

# 菜单定义：每条记录用 parent_route 替代 parent_id，运行时自动解析。
# route_name 作为唯一标识，已存在则跳过。
#
# parent_route 规则:
#   "0"  — 顶级目录
#   其他 — 对应父菜单的 route_name
#
# 新增菜单只需在末尾追加，不要修改已有条目。

MENU_DEFINITIONS = [
    # ============ 首页 ============
    {
        "route_name": "home",
        "parent_route": "0",
        "menu_name": "首页",
        "menu_type": "C",
        "icon": "carbon:home",
        "icon_type": "1",
        "component": "layout.base$view.home",
        "layout": "base",
        "page": "home",
        "route_path": "/home",
        "i18n_key": "route.home",
        "order": 0,
        "status": "1",
        "hide_in_menu": False,
        "keep_alive": False,
        "constant": False,
        "multi_tab": False,
    },
    # ============ AI 助手 ============
    {
        "route_name": "ai",
        "parent_route": "0",
        "menu_name": "AI 助手",
        "menu_type": "M",
        "icon": "carbon:chat-bot",
        "icon_type": "1",
        "component": "layout.base",
        "layout": "base",
        "route_path": "/ai",
        "i18n_key": "route.ai",
        "order": 1,
        "status": "1",
        "hide_in_menu": False,
        "keep_alive": False,
        "constant": False,
        "multi_tab": False,
    },
    {
        "route_name": "ai_chat",
        "parent_route": "ai",
        "menu_name": "AI 对话",
        "menu_type": "C",
        "icon": "carbon:chat",
        "icon_type": "1",
        "component": "view.ai_chat",
        "page": "ai_chat",
        "route_path": "/ai/chat",
        "i18n_key": "route.ai_chat",
        "order": 1,
        "status": "1",
        "hide_in_menu": False,
        "keep_alive": False,
        "constant": False,
        "multi_tab": False,
    },
    {
        "route_name": "ai_provider",
        "parent_route": "ai",
        "menu_name": "模型管理",
        "menu_type": "C",
        "icon": "carbon:settings-adjust",
        "icon_type": "1",
        "component": "view.ai_provider",
        "page": "ai_provider",
        "route_path": "/ai/provider",
        "i18n_key": "route.ai_provider",
        "order": 2,
        "status": "1",
        "hide_in_menu": False,
        "keep_alive": False,
        "constant": False,
        "multi_tab": False,
    },
    # ---- AI 模型管理按钮权限 ----
    {
        "key": "ai_provider_list",
        "parent_route": "ai_provider",
        "menu_name": "查询",
        "menu_type": "F",
        "permission": "ai:provider:list",
        "route_path": "",
        "status": "1",
    },
    {
        "key": "ai_provider_add",
        "parent_route": "ai_provider",
        "menu_name": "新增",
        "menu_type": "F",
        "permission": "ai:provider:add",
        "route_path": "",
        "status": "1",
    },
    {
        "key": "ai_provider_edit",
        "parent_route": "ai_provider",
        "menu_name": "修改",
        "menu_type": "F",
        "permission": "ai:provider:edit",
        "route_path": "",
        "status": "1",
    },
    {
        "key": "ai_provider_delete",
        "parent_route": "ai_provider",
        "menu_name": "删除",
        "menu_type": "F",
        "permission": "ai:provider:delete",
        "route_path": "",
        "status": "1",
    },
    {
        "key": "ai_provider_test-model",
        "parent_route": "ai_provider",
        "menu_name": "连通性测试",
        "menu_type": "F",
        "permission": "ai:provider:test-model",
        "route_path": "",
        "status": "1",
    },
    # ============ 权限管理 ============
    {
        "route_name": "auth",
        "parent_route": "0",
        "menu_name": "权限管理",
        "menu_type": "M",
        "icon": "carbon:security",
        "icon_type": "1",
        "component": "layout.base",
        "layout": "base",
        "route_path": "/auth",
        "i18n_key": "route.auth",
        "order": 98,
        "status": "1",
        "hide_in_menu": False,
        "keep_alive": False,
        "constant": False,
        "multi_tab": False,
    },
    {
        "route_name": "system_dept",
        "parent_route": "auth",
        "menu_name": "部门管理",
        "menu_type": "C",
        "icon": "carbon:user-multiple",
        "icon_type": "1",
        "component": "view.system_dept",
        "page": "system_dept",
        "route_path": "/system/dept",
        "i18n_key": "route.system_dept",
        "order": 1,
        "status": "1",
        "hide_in_menu": False,
        "keep_alive": False,
        "constant": False,
        "multi_tab": False,
    },
    # ---- 部门管理按钮权限 ----
    {
        "key": "system_dept_list",
        "parent_route": "system_dept",
        "menu_name": "查询",
        "menu_type": "F",
        "permission": "system:dept:list",
        "route_path": "",
        "status": "1",
    },
    {
        "key": "system_dept_add",
        "parent_route": "system_dept",
        "menu_name": "新增",
        "menu_type": "F",
        "permission": "system:dept:add",
        "route_path": "",
        "status": "1",
    },
    {
        "key": "system_dept_edit",
        "parent_route": "system_dept",
        "menu_name": "修改",
        "menu_type": "F",
        "permission": "system:dept:edit",
        "route_path": "",
        "status": "1",
    },
    {
        "key": "system_dept_delete",
        "parent_route": "system_dept",
        "menu_name": "删除",
        "menu_type": "F",
        "permission": "system:dept:delete",
        "route_path": "",
        "status": "1",
    },
    {
        "key": "system_dept_batch-delete",
        "parent_route": "system_dept",
        "menu_name": "批量删除",
        "menu_type": "F",
        "permission": "system:dept:batch-delete",
        "route_path": "",
        "status": "1",
    },
    {
        "route_name": "system_user",
        "parent_route": "auth",
        "menu_name": "用户管理",
        "menu_type": "C",
        "icon": "carbon:user",
        "icon_type": "1",
        "component": "view.system_user",
        "page": "system_user",
        "route_path": "/system/user",
        "i18n_key": "route.system_user",
        "order": 2,
        "status": "1",
        "hide_in_menu": False,
        "keep_alive": False,
        "constant": False,
        "multi_tab": False,
    },
    # ---- 用户管理按钮权限 ----
    {
        "key": "system_user_list",
        "parent_route": "system_user",
        "menu_name": "查询",
        "menu_type": "F",
        "permission": "system:user:list",
        "route_path": "",
        "status": "1",
    },
    {
        "key": "system_user_add",
        "parent_route": "system_user",
        "menu_name": "新增",
        "menu_type": "F",
        "permission": "system:user:add",
        "route_path": "",
        "status": "1",
    },
    {
        "key": "system_user_edit",
        "parent_route": "system_user",
        "menu_name": "修改",
        "menu_type": "F",
        "permission": "system:user:edit",
        "route_path": "",
        "status": "1",
    },
    {
        "key": "system_user_delete",
        "parent_route": "system_user",
        "menu_name": "删除",
        "menu_type": "F",
        "permission": "system:user:delete",
        "route_path": "",
        "status": "1",
    },
    {
        "key": "system_user_batch-delete",
        "parent_route": "system_user",
        "menu_name": "批量删除",
        "menu_type": "F",
        "permission": "system:user:batch-delete",
        "route_path": "",
        "status": "1",
    },
    {
        "key": "system_user_reset-password",
        "parent_route": "system_user",
        "menu_name": "重置密码",
        "menu_type": "F",
        "permission": "system:user:reset-password",
        "route_path": "",
        "status": "1",
    },
    # ---- 用户导入导出按钮权限 ----
    # 路由层 require_permissions("system:user:import" / "system:user:export") 依赖这两个 seed
    # 父菜单 system_user 已存在（line 238），sync_menus 按 permission 去重，重复执行安全。
    {
        "key": "system_user_import",
        "parent_route": "system_user",
        "menu_name": "导入",
        "menu_type": "F",
        "permission": "system:user:import",
        "route_path": "",
        "status": "1",
    },
    {
        "key": "system_user_export",
        "parent_route": "system_user",
        "menu_name": "导出",
        "menu_type": "F",
        "permission": "system:user:export",
        "route_path": "",
        "status": "1",
    },
    {
        "route_name": "system_role",
        "parent_route": "auth",
        "menu_name": "角色管理",
        "menu_type": "C",
        "icon": "carbon:user-role",
        "icon_type": "1",
        "component": "view.system_role",
        "page": "system_role",
        "route_path": "/system/role",
        "i18n_key": "route.system_role",
        "order": 3,
        "status": "1",
        "hide_in_menu": False,
        "keep_alive": False,
        "constant": False,
        "multi_tab": False,
    },
    # ---- 角色管理按钮权限 ----
    {
        "key": "system_role_list",
        "parent_route": "system_role",
        "menu_name": "查询",
        "menu_type": "F",
        "permission": "system:role:list",
        "route_path": "",
        "status": "1",
    },
    {
        "key": "system_role_add",
        "parent_route": "system_role",
        "menu_name": "新增",
        "menu_type": "F",
        "permission": "system:role:add",
        "route_path": "",
        "status": "1",
    },
    {
        "key": "system_role_edit",
        "parent_route": "system_role",
        "menu_name": "修改",
        "menu_type": "F",
        "permission": "system:role:edit",
        "route_path": "",
        "status": "1",
    },
    {
        "key": "system_role_delete",
        "parent_route": "system_role",
        "menu_name": "删除",
        "menu_type": "F",
        "permission": "system:role:delete",
        "route_path": "",
        "status": "1",
    },
    {
        "key": "system_role_batch-delete",
        "parent_route": "system_role",
        "menu_name": "批量删除",
        "menu_type": "F",
        "permission": "system:role:batch-delete",
        "route_path": "",
        "status": "1",
    },
    {
        "key": "system_role_menu-auth",
        "parent_route": "system_role",
        "menu_name": "菜单权限",
        "menu_type": "F",
        "permission": "system:role:menu-auth",
        "route_path": "",
        "status": "1",
    },
    {
        "route_name": "system_menu",
        "parent_route": "auth",
        "menu_name": "菜单管理",
        "menu_type": "C",
        "icon": "carbon:menu",
        "icon_type": "1",
        "component": "view.system_menu",
        "page": "system_menu",
        "route_path": "/system/menu",
        "i18n_key": "route.system_menu",
        "order": 4,
        "status": "1",
        "hide_in_menu": False,
        "keep_alive": False,
        "constant": False,
        "multi_tab": False,
    },
    # ---- 菜单管理按钮权限 ----
    {
        "key": "system_menu_list",
        "parent_route": "system_menu",
        "menu_name": "查询",
        "menu_type": "F",
        "permission": "system:menu:list",
        "route_path": "",
        "status": "1",
    },
    {
        "key": "system_menu_add",
        "parent_route": "system_menu",
        "menu_name": "新增",
        "menu_type": "F",
        "permission": "system:menu:add",
        "route_path": "",
        "status": "1",
    },
    {
        "key": "system_menu_edit",
        "parent_route": "system_menu",
        "menu_name": "修改",
        "menu_type": "F",
        "permission": "system:menu:edit",
        "route_path": "",
        "status": "1",
    },
    {
        "key": "system_menu_delete",
        "parent_route": "system_menu",
        "menu_name": "删除",
        "menu_type": "F",
        "permission": "system:menu:delete",
        "route_path": "",
        "status": "1",
    },
    {
        "key": "system_menu_batch-delete",
        "parent_route": "system_menu",
        "menu_name": "批量删除",
        "menu_type": "F",
        "permission": "system:menu:batch-delete",
        "route_path": "",
        "status": "1",
    },
    # ============ 系统管理 ============
    {
        "route_name": "system",
        "parent_route": "0",
        "menu_name": "系统管理",
        "menu_type": "M",
        "icon": "carbon:cloud-service-management",
        "icon_type": "1",
        "component": "layout.base",
        "layout": "base",
        "route_path": "/system",
        "i18n_key": "route.system",
        "order": 99,
        "status": "1",
        "hide_in_menu": False,
        "keep_alive": False,
        "constant": False,
        "multi_tab": False,
    },
    {
        "route_name": "system_dict",
        "parent_route": "system",
        "menu_name": "字典管理",
        "menu_type": "C",
        "icon": "fluent-mdl2:dictionary",
        "icon_type": "1",
        "component": "view.system_dict",
        "page": "system_dict",
        "route_path": "/system/dict",
        "i18n_key": "route.system_dict",
        "order": 1,
        "status": "1",
        "hide_in_menu": False,
        "keep_alive": False,
        "constant": False,
        "multi_tab": False,
    },
    # ---- 字典类型管理按钮权限 ----
    {
        "key": "system_dict-type_list",
        "parent_route": "system_dict",
        "menu_name": "查询",
        "menu_type": "F",
        "permission": "system:dict-type:list",
        "route_path": "",
        "status": "1",
    },
    {
        "key": "system_dict-type_add",
        "parent_route": "system_dict",
        "menu_name": "新增",
        "menu_type": "F",
        "permission": "system:dict-type:add",
        "route_path": "",
        "status": "1",
    },
    {
        "key": "system_dict-type_edit",
        "parent_route": "system_dict",
        "menu_name": "修改",
        "menu_type": "F",
        "permission": "system:dict-type:edit",
        "route_path": "",
        "status": "1",
    },
    {
        "key": "system_dict-type_delete",
        "parent_route": "system_dict",
        "menu_name": "删除",
        "menu_type": "F",
        "permission": "system:dict-type:delete",
        "route_path": "",
        "status": "1",
    },
    {
        "key": "system_dict-type_batch-delete",
        "parent_route": "system_dict",
        "menu_name": "批量删除",
        "menu_type": "F",
        "permission": "system:dict-type:batch-delete",
        "route_path": "",
        "status": "1",
    },
    {
        "route_name": "system_dict_data",
        "parent_route": "system",
        "menu_name": "字典数据",
        "menu_type": "C",
        "icon": "fluent-mdl2:dictionary",
        "icon_type": "1",
        "component": "view.system_dict_data",
        "page": "system_dict_data",
        "route_path": "/system/dict/data",
        "i18n_key": "route.system_dict_data",
        "order": 2,
        "status": "1",
        "hide_in_menu": True,
        "keep_alive": False,
        "constant": False,
        "multi_tab": False,
    },
    # ---- 字典数据管理按钮权限 ----
    {
        "key": "system_dict-data_list",
        "parent_route": "system_dict_data",
        "menu_name": "查询",
        "menu_type": "F",
        "permission": "system:dict-data:list",
        "route_path": "",
        "status": "1",
    },
    {
        "key": "system_dict-data_add",
        "parent_route": "system_dict_data",
        "menu_name": "新增",
        "menu_type": "F",
        "permission": "system:dict-data:add",
        "route_path": "",
        "status": "1",
    },
    {
        "key": "system_dict-data_edit",
        "parent_route": "system_dict_data",
        "menu_name": "修改",
        "menu_type": "F",
        "permission": "system:dict-data:edit",
        "route_path": "",
        "status": "1",
    },
    {
        "key": "system_dict-data_delete",
        "parent_route": "system_dict_data",
        "menu_name": "删除",
        "menu_type": "F",
        "permission": "system:dict-data:delete",
        "route_path": "",
        "status": "1",
    },
    {
        "key": "system_dict-data_batch-delete",
        "parent_route": "system_dict_data",
        "menu_name": "批量删除",
        "menu_type": "F",
        "permission": "system:dict-data:batch-delete",
        "route_path": "",
        "status": "1",
    },
    {
        "route_name": "system_file",
        "parent_route": "system",
        "menu_name": "文件管理",
        "menu_type": "C",
        "icon": "carbon:cloud-upload",
        "icon_type": "1",
        "component": "view.system_file",
        "page": "system_file",
        "route_path": "/system/file",
        "i18n_key": "route.system_file",
        "order": 3,
        "status": "1",
        "hide_in_menu": False,
        "keep_alive": False,
        "constant": False,
        "multi_tab": False,
    },
    # ---- 文件管理按钮权限 ----
    {
        "key": "system_file_list",
        "parent_route": "system_file",
        "menu_name": "查询",
        "menu_type": "F",
        "permission": "system:file:list",
        "route_path": "",
        "status": "1",
    },
    {
        "key": "system_file_upload",
        "parent_route": "system_file",
        "menu_name": "上传",
        "menu_type": "F",
        "permission": "system:file:upload",
        "route_path": "",
        "status": "1",
    },
    {
        "key": "system_file_delete",
        "parent_route": "system_file",
        "menu_name": "删除",
        "menu_type": "F",
        "permission": "system:file:delete",
        "route_path": "",
        "status": "1",
    },
    # ---- 系统设置 ----
    {
        "route_name": "system_config",
        "parent_route": "system",
        "menu_name": "系统设置",
        "menu_type": "C",
        "icon": "carbon:settings",
        "icon_type": "1",
        "component": "view.system_config",
        "page": "system_config",
        "route_path": "/system/config",
        "i18n_key": "route.system_config",
        "order": 4,
        "status": "1",
        "hide_in_menu": False,
        "keep_alive": False,
        "constant": False,
        "multi_tab": False,
    },
    # ---- 系统设置按钮权限 ----
    {
        "key": "system_config_list",
        "parent_route": "system_config",
        "menu_name": "查询",
        "menu_type": "F",
        "permission": "system:config:list",
        "route_path": "",
        "status": "1",
    },
    {
        "key": "system_config_add",
        "parent_route": "system_config",
        "menu_name": "新增",
        "menu_type": "F",
        "permission": "system:config:add",
        "route_path": "",
        "status": "1",
    },
    {
        "key": "system_config_edit",
        "parent_route": "system_config",
        "menu_name": "修改",
        "menu_type": "F",
        "permission": "system:config:edit",
        "route_path": "",
        "status": "1",
    },
    {
        "key": "system_config_delete",
        "parent_route": "system_config",
        "menu_name": "删除",
        "menu_type": "F",
        "permission": "system:config:delete",
        "route_path": "",
        "status": "1",
    },
    {
        "key": "system_config_batch-delete",
        "parent_route": "system_config",
        "menu_name": "批量删除",
        "menu_type": "F",
        "permission": "system:config:batch-delete",
        "route_path": "",
        "status": "1",
    },
    {
        "key": "system_config_export",
        "parent_route": "system_config",
        "menu_name": "导出",
        "menu_type": "F",
        "permission": "system:config:export",
        "route_path": "",
        "status": "1",
    },
    {
        "key": "system_config_import",
        "parent_route": "system_config",
        "menu_name": "导入",
        "menu_type": "F",
        "permission": "system:config:import",
        "route_path": "",
        "status": "1",
    },
    # ---- 数据权限演示 ----
    # route_name 和 i18n_key 用 kebab-case 匹配前端 @elegant-router 生成的命名
    # （目录 data-scope-demo/index.vue → route name 'system_data-scope-demo'）
    {
        "route_name": "system_data-scope-demo",
        "parent_route": "system",
        "menu_name": "数据权限演示",
        "menu_type": "C",
        "icon": "carbon:security",
        "icon_type": "1",
        "component": "view.system_data-scope-demo",
        "page": "system_data-scope-demo",
        "route_path": "/system/data-scope-demo",
        "i18n_key": "route.system_data-scope-demo",
        "order": 8,
        "status": "1",
        "hide_in_menu": False,
        "keep_alive": False,
        "constant": False,
        "multi_tab": False,
    },
    # ---- 数据权限演示按钮权限 ----
    {
        "key": "system_data-scope-demo_list",
        "parent_route": "system_data-scope-demo",
        "menu_name": "查询",
        "menu_type": "F",
        "permission": "system:data-scope-demo:list",
        "route_path": "",
        "status": "1",
    },
    {
        "key": "system_data-scope-demo_add",
        "parent_route": "system_data-scope-demo",
        "menu_name": "新增",
        "menu_type": "F",
        "permission": "system:data-scope-demo:add",
        "route_path": "",
        "status": "1",
    },
    {
        "key": "system_data-scope-demo_edit",
        "parent_route": "system_data-scope-demo",
        "menu_name": "修改",
        "menu_type": "F",
        "permission": "system:data-scope-demo:edit",
        "route_path": "",
        "status": "1",
    },
    {
        "key": "system_data-scope-demo_delete",
        "parent_route": "system_data-scope-demo",
        "menu_name": "删除",
        "menu_type": "F",
        "permission": "system:data-scope-demo:delete",
        "route_path": "",
        "status": "1",
    },
    # ---- 监控管理 ----
    {
        "route_name": "system_monitor",
        "parent_route": "system",
        "menu_name": "监控管理",
        "menu_type": "M",
        "icon": "carbon:activity",
        "icon_type": "1",
        "component": "layout.base",
        "layout": "base",
        "route_path": "/system/monitor",
        "i18n_key": "route.system_monitor",
        "order": 5,
        "status": "1",
        "hide_in_menu": True,
        "keep_alive": False,
        "constant": False,
        "multi_tab": False,
    },
    {
        "route_name": "system_operation-log",
        "parent_route": "system",
        "menu_name": "操作日志",
        "menu_type": "C",
        "icon": "carbon:document",
        "icon_type": "1",
        "component": "view.system_operation-log",
        "page": "system_operation-log",
        "route_path": "/system/operation-log",
        "i18n_key": "route.system_operation-log",
        "order": 6,
        "status": "1",
        "hide_in_menu": False,
        "keep_alive": False,
        "constant": False,
        "multi_tab": False,
    },
    # ---- 操作日志按钮权限 ----
    {
        "key": "monitor_operation-log_list",
        "parent_route": "system_operation-log",
        "menu_name": "查询",
        "menu_type": "F",
        "permission": "monitor:operation-log:list",
        "route_path": "",
        "status": "1",
    },
    {
        "key": "monitor_operation-log_clean",
        "parent_route": "system_operation-log",
        "menu_name": "清理",
        "menu_type": "F",
        "permission": "monitor:operation-log:clean",
        "route_path": "",
        "status": "1",
    },
    {
        "key": "monitor_operation-log_delete",
        "parent_route": "system_operation-log",
        "menu_name": "批量删除",
        "menu_type": "F",
        "permission": "monitor:operation-log:delete",
        "route_path": "",
        "status": "1",
    },
    {
        "route_name": "system_login-log",
        "parent_route": "system",
        "menu_name": "登录日志",
        "menu_type": "C",
        "icon": "carbon:login",
        "icon_type": "1",
        "component": "view.system_login-log",
        "page": "system_login-log",
        "route_path": "/system/login-log",
        "i18n_key": "route.system_login-log",
        "order": 7,
        "status": "1",
        "hide_in_menu": False,
        "keep_alive": False,
        "constant": False,
        "multi_tab": False,
    },
    # ---- 登录日志按钮权限 ----
    {
        "key": "monitor_login-log_list",
        "parent_route": "system_login-log",
        "menu_name": "查询",
        "menu_type": "F",
        "permission": "monitor:login-log:list",
        "route_path": "",
        "status": "1",
    },
    {
        "key": "monitor_login-log_clean",
        "parent_route": "system_login-log",
        "menu_name": "清理",
        "menu_type": "F",
        "permission": "monitor:login-log:clean",
        "route_path": "",
        "status": "1",
    },
    {
        "key": "monitor_login-log_delete",
        "parent_route": "system_login-log",
        "menu_name": "批量删除",
        "menu_type": "F",
        "permission": "monitor:login-log:delete",
        "route_path": "",
        "status": "1",
    },
    # ============ 任务中心 ============
    {
        "route_name": "task",
        "parent_route": "0",
        "menu_name": "任务中心",
        "menu_type": "M",
        "icon": "carbon:task",
        "icon_type": "1",
        "component": "layout.base",
        "layout": "base",
        "route_path": "/task",
        "i18n_key": "route.task",
        "order": 100,
        "status": "1",
        "hide_in_menu": False,
        "keep_alive": False,
        "constant": False,
        "multi_tab": False,
    },
    {
        "route_name": "system_job",
        "parent_route": "task",
        "menu_name": "定时任务",
        "menu_type": "C",
        "icon": "carbon:timer",
        "icon_type": "1",
        "component": "view.system_job",
        "page": "system_job",
        "route_path": "/system/job",
        "i18n_key": "route.system_job",
        "order": 1,
        "status": "1",
        "hide_in_menu": False,
        "keep_alive": False,
        "constant": False,
        "multi_tab": False,
    },
    # ---- 定时任务管理按钮权限 ----
    {
        "key": "system_job_list",
        "parent_route": "system_job",
        "menu_name": "查询",
        "menu_type": "F",
        "permission": "system:job:list",
        "route_path": "",
        "status": "1",
    },
    {
        "key": "system_job_add",
        "parent_route": "system_job",
        "menu_name": "新增",
        "menu_type": "F",
        "permission": "system:job:add",
        "route_path": "",
        "status": "1",
    },
    {
        "key": "system_job_edit",
        "parent_route": "system_job",
        "menu_name": "修改",
        "menu_type": "F",
        "permission": "system:job:edit",
        "route_path": "",
        "status": "1",
    },
    {
        "key": "system_job_delete",
        "parent_route": "system_job",
        "menu_name": "删除",
        "menu_type": "F",
        "permission": "system:job:delete",
        "route_path": "",
        "status": "1",
    },
    {
        "key": "system_job_batch-delete",
        "parent_route": "system_job",
        "menu_name": "批量删除",
        "menu_type": "F",
        "permission": "system:job:batch-delete",
        "route_path": "",
        "status": "1",
    },
    {
        "key": "system_job_run",
        "parent_route": "system_job",
        "menu_name": "立即执行",
        "menu_type": "F",
        "permission": "system:job:run",
        "route_path": "",
        "status": "1",
    },
    {
        "route_name": "system_job-log",
        "parent_route": "task",
        "menu_name": "任务日志",
        "menu_type": "C",
        "icon": "carbon:document-tasks",
        "icon_type": "1",
        "component": "view.system_job-log",
        "page": "system_job-log",
        "route_path": "/system/job-log",
        "i18n_key": "route.system_job-log",
        "order": 2,
        "status": "1",
        "hide_in_menu": False,
        "keep_alive": False,
        "constant": False,
        "multi_tab": False,
    },
    # ---- 任务日志按钮权限 ----
    {
        "key": "system_job-log_list",
        "parent_route": "system_job-log",
        "menu_name": "查询",
        "menu_type": "F",
        "permission": "system:job-log:list",
        "route_path": "",
        "status": "1",
    },
    {
        "key": "system_job-log_clean",
        "parent_route": "system_job-log",
        "menu_name": "清理",
        "menu_type": "F",
        "permission": "system:job-log:clean",
        "route_path": "",
        "status": "1",
    },
    {
        "key": "system_job-log_batch-delete",
        "parent_route": "system_job-log",
        "menu_name": "批量删除",
        "menu_type": "F",
        "permission": "system:job-log:batch-delete",
        "route_path": "",
        "status": "1",
    },
    # ============ 应用市场 ============
    {
        "route_name": "marketplace",
        "parent_route": "0",
        "menu_name": "应用市场",
        "menu_type": "C",
        "icon": "carbon:store",
        "icon_type": "1",
        "component": "layout.base$view.marketplace",
        "layout": "base",
        "page": "marketplace",
        "route_path": "/marketplace",
        "i18n_key": "route.marketplace",
        "order": 50,
        "status": "1",
        "hide_in_menu": False,
        "keep_alive": False,
        "constant": False,
        "multi_tab": False,
    },
    # ---- 应用详情（隐藏页，点卡片进入）----
    {
        "route_name": "marketplace-detail",
        "parent_route": "0",
        "menu_name": "应用详情",
        "menu_type": "C",
        "icon": "carbon:store",
        "icon_type": "1",
        "component": "layout.base$view.marketplace-detail",
        "layout": "base",
        "page": "marketplace-detail",
        "route_path": "/marketplace/detail",
        "i18n_key": "route.marketplace-detail",
        "order": 51,
        "status": "1",
        "hide_in_menu": True,
        "keep_alive": False,
        "constant": False,
        "multi_tab": False,
    },
    # ============ 应用管理（目录）============
    {
        "route_name": "app-management",
        "parent_route": "0",
        "menu_name": "应用管理",
        "menu_type": "M",
        "icon": "carbon:cloud-services",
        "icon_type": "1",
        "component": "layout.base",
        "layout": "base",
        "route_path": "/marketplace",
        "i18n_key": "route.app-management",
        "order": 52,
        "status": "1",
        "hide_in_menu": False,
        "keep_alive": False,
        "constant": False,
        "multi_tab": False,
    },
    {
        "route_name": "marketplace_installed",
        "parent_route": "app-management",
        "menu_name": "已安装应用",
        "menu_type": "C",
        "icon": "carbon:cloud-download",
        "icon_type": "1",
        # 子菜单 route_name 用下划线 → transform 走 isView 路径
        # component 用连字符 → 匹配 @elegant-router imports.ts 的 key
        "component": "view.marketplace-installed",
        "page": "marketplace-installed",
        "route_path": "/marketplace/installed",
        "i18n_key": "route.marketplace-installed",
        "order": 1,
        "status": "1",
        "hide_in_menu": False,
        "keep_alive": False,
        "constant": False,
        "multi_tab": False,
    },
    # ---- 已安装应用按钮权限（安装/卸载/启用/停用） ----
    {
        "key": "marketplace_install",
        "parent_route": "marketplace_installed",
        "menu_name": "安装/卸载/启停",
        "menu_type": "F",
        "permission": "marketplace:install",
        "route_path": "",
        "status": "1",
    },
    {
        "route_name": "marketplace_upload",
        "parent_route": "app-management",
        "menu_name": "上传应用",
        "menu_type": "C",
        "icon": "carbon:upload",
        "icon_type": "1",
        "component": "view.marketplace-upload",
        "page": "marketplace-upload",
        "route_path": "/marketplace/upload",
        "i18n_key": "route.marketplace-upload",
        "order": 2,
        "status": "1",
        "hide_in_menu": False,
        "keep_alive": False,
        "constant": False,
        "multi_tab": False,
    },
    {
        # 应用审核（仅 marketplace:review 权限可见）
        # 云端与本地拆分后归入 cloud-only 菜单。
        "route_name": "marketplace_review",
        "parent_route": "app-management",
        "menu_name": "应用审核",
        "menu_type": "C",
        "icon": "carbon:task-approved",
        "icon_type": "1",
        "component": "view.marketplace-review",
        "page": "marketplace-review",
        "route_path": "/marketplace/review",
        "i18n_key": "route.marketplace-review",
        "order": 3,
        "status": "1",
        "hide_in_menu": False,
        "keep_alive": False,
        "constant": False,
        "multi_tab": False,
    },
    # ---- 应用审核按钮权限（批准/拒绝） ----
    {
        "key": "marketplace_review_btn",
        "parent_route": "marketplace_review",
        "menu_name": "审核",
        "menu_type": "F",
        "permission": "marketplace:review",
        "route_path": "",
        "status": "1",
    },
    # ============ AI 助手管理 ============
    {
        "route_name": "ai_agent",
        "parent_route": "ai",
        "menu_name": "AI 助手管理",
        "menu_type": "C",
        "icon": "carbon:bot",
        "icon_type": "1",
        "component": "view.ai_agent",
        "page": "ai_agent",
        "route_path": "/ai/agent",
        "i18n_key": "route.ai_agent",
        "order": 3,
        "status": "1",
        # 前端管理页面已实现，菜单可见。
        "hide_in_menu": False,
        "keep_alive": False,
        "constant": False,
        "multi_tab": False,
    },
    # ---- AI Agent 管理按钮权限 ----
    {
        "key": "ai_agent_list",
        "parent_route": "ai_agent",
        "menu_name": "查询",
        "menu_type": "F",
        "permission": "ai:agent:list",
        "route_path": "",
        "status": "1",
    },
    {
        "key": "ai_agent_add",
        "parent_route": "ai_agent",
        "menu_name": "新增",
        "menu_type": "F",
        "permission": "ai:agent:add",
        "route_path": "",
        "status": "1",
    },
    {
        "key": "ai_agent_edit",
        "parent_route": "ai_agent",
        "menu_name": "修改",
        "menu_type": "F",
        "permission": "ai:agent:edit",
        "route_path": "",
        "status": "1",
    },
    {
        "key": "ai_agent_delete",
        "parent_route": "ai_agent",
        "menu_name": "删除",
        "menu_type": "F",
        "permission": "ai:agent:delete",
        "route_path": "",
        "status": "1",
    },
    # ---- AI Trace 查看（审计员，独立于 ai:agent:*） ----
    {
        "key": "ai_trace_view",
        "parent_route": "ai",
        "menu_name": "AI Trace 查看",
        "menu_type": "F",
        "permission": "ai:trace:view",
        "route_path": "",
        "status": "1",
    },
    # ---- AI 路由反馈 ----
    # route_name / component / page / i18n_key 用 kebab-case 匹配前端 @elegant-router 命名约定
    # （view.ai_routing-feedback 对应 src/views/ai/routing-feedback/index.vue；下划线形式会被
    # transformElegantRouteToVueRoute 视为查不到 view 而抛 "View component not found"，路由被静默丢弃）
    {
        "route_name": "ai_routing-feedback",
        "parent_route": "ai",
        "menu_name": "AI 路由反馈",
        "menu_type": "C",
        "icon": "carbon:analytics",
        "icon_type": "1",
        "component": "view.ai_routing-feedback",
        "page": "ai_routing-feedback",
        "route_path": "/ai/routing-feedback",
        "i18n_key": "route.ai_routing-feedback",
        "order": 4,
        "status": "1",
        "hide_in_menu": False,
        "keep_alive": False,
        "constant": False,
        "multi_tab": False,
    },
    # ---- AI 路由反馈按钮权限 ----
    {
        "key": "ai_routing_feedback_list",
        "parent_route": "ai_routing-feedback",
        "menu_name": "查询",
        "menu_type": "F",
        "permission": "ai:routing-feedback:list",
        "route_path": "",
        "status": "1",
    },
    # ---- 角色 AI Agent 授权按钮权限 ----
    {
        "key": "system_role_ai_agent_auth",
        "parent_route": "system_role",
        "menu_name": "AI Agent 授权",
        "menu_type": "F",
        "permission": "system:role:ai-agent-auth",
        "route_path": "",
        "status": "1",
    },
]


def _get_def_key(d: dict) -> str:
    """获取菜单定义的唯一标识：F 类型用 key，其他用 route_name"""
    return d.get("key") or d["route_name"]


async def sync_menus():
    engine = create_async_engine(settings.DATABASE_URL)
    async_session = sessionmaker(engine, class_=AsyncSession, expire_on_commit=False)

    async with async_session() as db:
        # 1. 查询所有已存在的 route_name 和 permission
        result = await db.execute(select(Menu.route_name))
        existing_routes = set(result.scalars().all())

        result2 = await db.execute(select(Menu.permission))
        existing_perms = {p for p in result2.scalars().all() if p is not None}

        # 2. 过滤出需要新增的菜单（F 类型按 permission 去重，其他按 route_name 去重）
        new_defs = []
        for d in MENU_DEFINITIONS:
            if d["menu_type"] == "F":
                perm = d.get("permission")
                if perm and perm in existing_perms:
                    continue
            else:
                if d["route_name"] in existing_routes:
                    continue
            new_defs.append(d)

        if not new_defs:
            print("All menus already exist, nothing to sync.")
            await engine.dispose()
            return

        # 3. 查询需要用到的父菜单 ID（已存在的 + 本轮即将插入的）
        #    先处理已存在数据库中的父菜单
        parent_routes_needed = {
            d["parent_route"] for d in new_defs if d["parent_route"] != "0"
        }
        result = await db.execute(
            select(Menu.menu_id, Menu.route_name).where(
                Menu.route_name.in_(parent_routes_needed)
            )
        )
        route_to_id = {name: mid for mid, name in result.all()}

        # 4. 按依赖顺序插入（先目录后子菜单）
        #    多轮处理确保父子依赖都能解析
        inserted = {}
        remaining = list(new_defs)

        for _ in range(3):  # 最多 3 层嵌套，足够了
            next_round = []
            for d in remaining:
                parent_route = d["parent_route"]

                # 解析 parent_id
                if parent_route == "0":
                    parent_id = 0
                elif parent_route in route_to_id:
                    parent_id = route_to_id[parent_route]
                elif parent_route in inserted:
                    parent_id = inserted[parent_route]
                else:
                    next_round.append(d)
                    continue

                menu_id = next_id()
                is_button = d["menu_type"] == "F"
                menu = Menu(
                    menu_id=menu_id,
                    parent_id=parent_id,
                    route_name=None if is_button else d["route_name"],
                    menu_name=d["menu_name"],
                    menu_type=d["menu_type"],
                    permission=d.get("permission"),
                    icon=d.get("icon"),
                    icon_type=d.get("icon_type"),
                    component=d.get("component"),
                    layout=d.get("layout"),
                    page=d.get("page"),
                    route_path=d.get("route_path"),
                    i18n_key=d.get("i18n_key"),
                    order=d.get("order", 0),
                    status=d.get("status", "1"),
                    hide_in_menu=d.get("hide_in_menu", False),
                    keep_alive=d.get("keep_alive", False),
                    constant=d.get("constant", False),
                    multi_tab=d.get("multi_tab", False),
                )
                db.add(menu)
                def_key = _get_def_key(d)
                # F-type 按钮不写 inserted：按钮不是任何菜单的父，
                # 若写入会用按钮 ID 覆盖父菜单的 route_name 映射，
                # 导致后续 sibling 按钮的 parent_id 错指到上一个按钮。
                if not is_button:
                    inserted[d["route_name"]] = menu_id
                print(f"  + [{d['menu_type']}] {d['menu_name']} ({def_key})")

            remaining = next_round
            if not remaining:
                break

        if remaining:
            print(f"Warning: {len(remaining)} menus skipped (parent not found):")
            for d in remaining:
                print(f"  - {d['menu_name']} (parent: {d['parent_route']})")

        await db.commit()

        # 一次性兜底：把 ai_agent 菜单的 hide_in_menu 从 True 改 False
        # 前端管理页面已实现，旧库需要将菜单更新为可见。
        result = await db.execute(select(Menu).where(Menu.route_name == "ai_agent"))
        ai_agent_menu = result.scalars().first()
        if ai_agent_menu and ai_agent_menu.hide_in_menu:
            ai_agent_menu.hide_in_menu = False
            await db.commit()
            print("Updated ai_agent menu: hide_in_menu -> False")

        # 一次性兜底：把 ai_routing_feedback (underscore) 改为 ai_routing-feedback (mixed)
        # 匹配前端 @elegant-router kebab-case 命名约定，避免动态路由模式下 view component
        # 查找失败（transformElegantRouteToVueRoute 会抛 "View component not found" 静默丢弃路由）
        result = await db.execute(
            select(Menu).where(Menu.route_name == "ai_routing_feedback")
        )
        old_feedback_menu = result.scalars().first()
        if old_feedback_menu:
            old_feedback_menu.route_name = "ai_routing-feedback"
            old_feedback_menu.component = "view.ai_routing-feedback"
            old_feedback_menu.page = "ai_routing-feedback"
            old_feedback_menu.i18n_key = "route.ai_routing-feedback"
            # 子按钮通过 parent_id (int) 关联父菜单，父菜单 menu_id 不变，
            # 所以无需 UPDATE 子行的 parent 链接 — 它们自动跟随。
            await db.commit()
            print(
                "Renamed ai_routing_feedback -> ai_routing-feedback (kebab convention)"
            )

        print(
            f"\nSynced {len(inserted)} new menus. Total menus in DB: {len(existing_routes) + len(inserted)}"
        )

    await engine.dispose()


if __name__ == "__main__":
    asyncio.run(sync_menus())
