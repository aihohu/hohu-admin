"""Schema baseline at the v0.1.4 release boundary (consolidated).

Consolidates the 13 published revisions (7035c014fc57 .. bf244f9a8b76) into one
boundary file. The revision id stays bf244f9a8b76 so databases already at the
v0.1.4 boundary continue to upgrade from here. Emitted from the actual v0.1.4
schema (scratch database upgraded with the original chain) because the
v0.1.4-era models were ahead of the published chain.
"""

from collections.abc import Sequence

from alembic import op

revision: str = "bf244f9a8b76"
down_revision: str | Sequence[str] | None = None
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

_UPGRADE_DDL = [
    "CREATE TABLE ai_provider (\n\tprovider_id BIGSERIAL NOT NULL, \n\tprovider_code VARCHAR(50) NOT NULL, \n\tname VARCHAR(100) NOT NULL, \n\tapi_key VARCHAR(500) NOT NULL, \n\tbase_url VARCHAR(500), \n\tis_enabled BOOLEAN NOT NULL, \n\tconfig JSON, \n\tcreate_time TIMESTAMP WITHOUT TIME ZONE DEFAULT now() NOT NULL, \n\tupdate_time TIMESTAMP WITHOUT TIME ZONE DEFAULT now() NOT NULL, \n\tcreate_by VARCHAR(64), \n\tCONSTRAINT ai_provider_pkey PRIMARY KEY (provider_id), \n\tCONSTRAINT ai_provider_provider_code_key UNIQUE NULLS DISTINCT (provider_code)\n)",
    "CREATE TABLE sys_config (\n\tconfig_id BIGSERIAL NOT NULL, \n\tconfig_name VARCHAR(100) NOT NULL, \n\tconfig_key VARCHAR(100) NOT NULL, \n\tconfig_value TEXT NOT NULL, \n\tconfig_type VARCHAR(20) NOT NULL, \n\tconfig_group VARCHAR(50) NOT NULL, \n\tstatus VARCHAR(2) NOT NULL, \n\tis_public BOOLEAN DEFAULT false NOT NULL, \n\tremark VARCHAR(500), \n\tcreate_by VARCHAR(64), \n\tcreate_time TIMESTAMP WITHOUT TIME ZONE DEFAULT now() NOT NULL, \n\tupdate_by VARCHAR(64), \n\tupdate_time TIMESTAMP WITHOUT TIME ZONE DEFAULT now() NOT NULL, \n\tCONSTRAINT sys_config_pkey PRIMARY KEY (config_id), \n\tCONSTRAINT sys_config_config_key_key UNIQUE NULLS DISTINCT (config_key)\n)",
    "CREATE TABLE sys_data_scope_demo (\n\tdemo_id BIGSERIAL NOT NULL, \n\ttitle VARCHAR(100) NOT NULL, \n\tcontent TEXT, \n\tdept_id BIGINT NOT NULL, \n\tcreate_by BIGINT NOT NULL, \n\tstatus VARCHAR(2) NOT NULL, \n\tcreate_time TIMESTAMP WITHOUT TIME ZONE DEFAULT now() NOT NULL, \n\tupdate_time TIMESTAMP WITHOUT TIME ZONE DEFAULT now() NOT NULL, \n\tCONSTRAINT sys_data_scope_demo_pkey PRIMARY KEY (demo_id)\n)",
    "CREATE TABLE sys_dept (\n\tdept_id BIGSERIAL NOT NULL, \n\tparent_id BIGINT, \n\tancestors VARCHAR(500), \n\tdept_name VARCHAR(100) NOT NULL, \n\torder_num INTEGER NOT NULL, \n\tleader VARCHAR(50), \n\tphone VARCHAR(20), \n\temail VARCHAR(100), \n\tstatus VARCHAR(2) NOT NULL, \n\tcreate_by VARCHAR(64), \n\tcreate_time TIMESTAMP WITHOUT TIME ZONE DEFAULT now() NOT NULL, \n\tupdate_by VARCHAR(64), \n\tupdate_time TIMESTAMP WITHOUT TIME ZONE DEFAULT now() NOT NULL, \n\tCONSTRAINT sys_dept_pkey PRIMARY KEY (dept_id)\n)",
    "CREATE TABLE sys_dict_data (\n\tdict_code BIGSERIAL NOT NULL, \n\tdict_sort INTEGER NOT NULL, \n\tdict_label VARCHAR(100) NOT NULL, \n\tdict_value VARCHAR(100) NOT NULL, \n\tdict_type VARCHAR(100) NOT NULL, \n\tcss_class VARCHAR(100), \n\tlist_class VARCHAR(100), \n\tis_default VARCHAR(2) NOT NULL, \n\tstatus VARCHAR(2) NOT NULL, \n\tcreate_by VARCHAR(64), \n\tcreate_time TIMESTAMP WITHOUT TIME ZONE DEFAULT now() NOT NULL, \n\tupdate_by VARCHAR(64), \n\tupdate_time TIMESTAMP WITHOUT TIME ZONE DEFAULT now() NOT NULL, \n\tCONSTRAINT sys_dict_data_pkey PRIMARY KEY (dict_code)\n)",
    "CREATE TABLE sys_dict_type (\n\tdict_type_id BIGSERIAL NOT NULL, \n\tdict_name VARCHAR(100) NOT NULL, \n\tdict_type VARCHAR(100) NOT NULL, \n\tstatus VARCHAR(2) NOT NULL, \n\tremark VARCHAR(500), \n\tcreate_by VARCHAR(64), \n\tcreate_time TIMESTAMP WITHOUT TIME ZONE DEFAULT now() NOT NULL, \n\tupdate_by VARCHAR(64), \n\tupdate_time TIMESTAMP WITHOUT TIME ZONE DEFAULT now() NOT NULL, \n\tCONSTRAINT sys_dict_type_pkey PRIMARY KEY (dict_type_id), \n\tCONSTRAINT sys_dict_type_dict_name_key UNIQUE NULLS DISTINCT (dict_name), \n\tCONSTRAINT sys_dict_type_dict_type_key UNIQUE NULLS DISTINCT (dict_type)\n)",
    "CREATE TABLE sys_file (\n\tfile_id BIGSERIAL NOT NULL, \n\toriginal_name VARCHAR(255) NOT NULL, \n\tfile_name VARCHAR(255) NOT NULL, \n\tfile_path VARCHAR(500) NOT NULL, \n\tfile_url VARCHAR(500) NOT NULL, \n\tfile_size BIGINT NOT NULL, \n\tfile_ext VARCHAR(20) NOT NULL, \n\tmime_type VARCHAR(100), \n\tbusiness_type VARCHAR(50), \n\tbusiness_id BIGINT, \n\tdel_flag VARCHAR(1) NOT NULL, \n\tcreate_by VARCHAR(64), \n\tcreate_time TIMESTAMP WITHOUT TIME ZONE DEFAULT now() NOT NULL, \n\tCONSTRAINT sys_file_pkey PRIMARY KEY (file_id)\n)",
    "CREATE TABLE sys_job (\n\tjob_id BIGSERIAL NOT NULL, \n\tjob_name VARCHAR(64) NOT NULL, \n\tjob_key VARCHAR(64) NOT NULL, \n\tcron_expression VARCHAR(64), \n\ttrigger_type VARCHAR(10) NOT NULL, \n\tinterval_value INTEGER, \n\tinterval_unit VARCHAR(10), \n\tjob_args TEXT, \n\tstatus VARCHAR(2) NOT NULL, \n\tconcurrent VARCHAR(2) NOT NULL, \n\tremark VARCHAR(256), \n\tcreate_by VARCHAR(64), \n\tcreate_time TIMESTAMP WITHOUT TIME ZONE DEFAULT now() NOT NULL, \n\tupdate_by VARCHAR(64), \n\tupdate_time TIMESTAMP WITHOUT TIME ZONE DEFAULT now() NOT NULL, \n\ttimeout_seconds INTEGER, \n\tmax_retries INTEGER DEFAULT 0 NOT NULL, \n\trun_on_enable BOOLEAN DEFAULT false NOT NULL, \n\tCONSTRAINT sys_job_pkey PRIMARY KEY (job_id), \n\tCONSTRAINT sys_job_job_key_key UNIQUE NULLS DISTINCT (job_key)\n)",
    "CREATE TABLE sys_job_log (\n\tjob_log_id BIGSERIAL NOT NULL, \n\tjob_id BIGINT NOT NULL, \n\tjob_name VARCHAR(64) NOT NULL, \n\tjob_key VARCHAR(64) NOT NULL, \n\tstatus VARCHAR(2) NOT NULL, \n\terror_msg TEXT, \n\tstart_time TIMESTAMP WITHOUT TIME ZONE NOT NULL, \n\tend_time TIMESTAMP WITHOUT TIME ZONE, \n\tduration INTEGER, \n\tattempt_count INTEGER DEFAULT 1 NOT NULL, \n\tCONSTRAINT sys_job_log_pkey PRIMARY KEY (job_log_id)\n)",
    "CREATE TABLE sys_login_log (\n\tlogin_log_id BIGSERIAL NOT NULL, \n\tuser_id BIGINT, \n\tusername VARCHAR(50) NOT NULL, \n\tip VARCHAR(50), \n\tuser_agent VARCHAR(500), \n\tstatus VARCHAR(2) NOT NULL, \n\tmessage VARCHAR(200), \n\tlogin_time TIMESTAMP WITHOUT TIME ZONE DEFAULT now() NOT NULL, \n\tCONSTRAINT sys_login_log_pkey PRIMARY KEY (login_log_id)\n)",
    "CREATE INDEX ix_login_log_login_time ON sys_login_log (login_time)",
    'CREATE TABLE sys_menu (\n\tmenu_id BIGSERIAL NOT NULL, \n\tparent_id BIGINT, \n\tmenu_name VARCHAR(50) NOT NULL, \n\tmenu_type VARCHAR(1) NOT NULL, \n\ticon VARCHAR(50), \n\ticon_type VARCHAR(1), \n\tpath VARCHAR(255), \n\tcomponent VARCHAR(255), \n\troute_name VARCHAR(50), \n\troute_path VARCHAR(255), \n\t"order" INTEGER NOT NULL, \n\tstatus VARCHAR(2) NOT NULL, \n\tcreate_by VARCHAR(32), \n\tupdate_by VARCHAR(32), \n\tcreate_time TIMESTAMP WITHOUT TIME ZONE DEFAULT now() NOT NULL, \n\tupdate_time TIMESTAMP WITHOUT TIME ZONE DEFAULT now() NOT NULL, \n\tpath_param VARCHAR(255), \n\tpage VARCHAR(255), \n\tlayout VARCHAR(255), \n\ti18n_key VARCHAR(100), \n\tkeep_alive BOOLEAN, \n\tconstant BOOLEAN, \n\thref VARCHAR(255), \n\thide_in_menu BOOLEAN, \n\tactive_menu VARCHAR(50), \n\tmulti_tab BOOLEAN, \n\tfixed_index_in_tab INTEGER, \n\tpermission VARCHAR(50), \n\tquery JSON, \n\tCONSTRAINT sys_menu_pkey PRIMARY KEY (menu_id)\n)',
    "CREATE TABLE sys_operation_log (\n\toperation_log_id BIGSERIAL NOT NULL, \n\tuser_id BIGINT NOT NULL, \n\tusername VARCHAR(50) NOT NULL, \n\tmodule VARCHAR(50) NOT NULL, \n\taction VARCHAR(20) NOT NULL, \n\tmethod VARCHAR(10) NOT NULL, \n\tpath VARCHAR(200) NOT NULL, \n\trequest_params TEXT, \n\tstatus_code INTEGER, \n\tip VARCHAR(50), \n\tduration INTEGER, \n\tcreate_time TIMESTAMP WITHOUT TIME ZONE DEFAULT now() NOT NULL, \n\tCONSTRAINT sys_operation_log_pkey PRIMARY KEY (operation_log_id)\n)",
    "CREATE INDEX ix_operation_log_create_time ON sys_operation_log (create_time)",
    "CREATE INDEX ix_operation_log_user_id ON sys_operation_log (user_id)",
    "CREATE TABLE sys_role (\n\trole_id BIGSERIAL NOT NULL, \n\trole_name VARCHAR(50) NOT NULL, \n\trole_code VARCHAR(50) NOT NULL, \n\trole_desc VARCHAR(255), \n\tstatus VARCHAR(2) NOT NULL, \n\tcreate_by VARCHAR(32), \n\tcreate_time TIMESTAMP WITHOUT TIME ZONE DEFAULT now() NOT NULL, \n\tupdate_by VARCHAR(64), \n\tupdate_time TIMESTAMP WITHOUT TIME ZONE DEFAULT now() NOT NULL, \n\tdata_scope VARCHAR(2) DEFAULT '1'::character varying NOT NULL, \n\tCONSTRAINT sys_role_pkey PRIMARY KEY (role_id), \n\tCONSTRAINT sys_role_role_code_key UNIQUE NULLS DISTINCT (role_code), \n\tCONSTRAINT sys_role_role_name_key UNIQUE NULLS DISTINCT (role_name)\n)",
    "CREATE TABLE sys_user (\n\tuser_id BIGSERIAL NOT NULL, \n\tuser_name VARCHAR(50) NOT NULL, \n\tnickname VARCHAR(50), \n\thashed_password VARCHAR(255) NOT NULL, \n\tstatus VARCHAR(10) NOT NULL, \n\tuser_avatar VARCHAR(255), \n\tuser_email VARCHAR(100), \n\tuser_phone VARCHAR(20), \n\tuser_gender VARCHAR(1), \n\tcreate_time TIMESTAMP WITHOUT TIME ZONE DEFAULT now() NOT NULL, \n\tupdate_time TIMESTAMP WITHOUT TIME ZONE DEFAULT now() NOT NULL, \n\tCONSTRAINT sys_user_pkey PRIMARY KEY (user_id)\n)",
    "CREATE UNIQUE INDEX ix_sys_user_user_name ON sys_user (user_name)",
    "CREATE TABLE ai_conversation (\n\tconversation_id BIGSERIAL NOT NULL, \n\tuser_id BIGINT NOT NULL, \n\ttitle VARCHAR(200) NOT NULL, \n\tmodel_name VARCHAR(100) NOT NULL, \n\tsystem_prompt TEXT, \n\tstatus SMALLINT NOT NULL, \n\tcreate_time TIMESTAMP WITHOUT TIME ZONE DEFAULT now() NOT NULL, \n\tupdate_time TIMESTAMP WITHOUT TIME ZONE DEFAULT now() NOT NULL, \n\tCONSTRAINT ai_conversation_pkey PRIMARY KEY (conversation_id), \n\tCONSTRAINT ai_conversation_user_id_fkey FOREIGN KEY(user_id) REFERENCES sys_user (user_id) ON DELETE CASCADE\n)",
    "CREATE TABLE ai_model (\n\tmodel_id BIGSERIAL NOT NULL, \n\tprovider_id BIGINT NOT NULL, \n\tname VARCHAR(100) NOT NULL, \n\tcapabilities JSONB NOT NULL, \n\tbase_url VARCHAR(500), \n\tis_enabled BOOLEAN DEFAULT true NOT NULL, \n\tsort_order INTEGER DEFAULT 0 NOT NULL, \n\tconfig JSONB, \n\tcreate_by VARCHAR(64), \n\tcreate_time TIMESTAMP WITHOUT TIME ZONE DEFAULT now(), \n\tupdate_time TIMESTAMP WITHOUT TIME ZONE DEFAULT now(), \n\tCONSTRAINT ai_model_pkey PRIMARY KEY (model_id), \n\tCONSTRAINT ai_model_provider_id_fkey FOREIGN KEY(provider_id) REFERENCES ai_provider (provider_id) ON DELETE CASCADE, \n\tCONSTRAINT uq_ai_model_provider_name UNIQUE NULLS DISTINCT (provider_id, name)\n)",
    "CREATE INDEX ix_ai_model_capabilities ON ai_model USING gin (capabilities jsonb_path_ops)",
    "CREATE TABLE mk_app (\n\tid BIGSERIAL NOT NULL, \n\ttenant_id BIGINT DEFAULT '0'::bigint NOT NULL, \n\tname VARCHAR(100) NOT NULL, \n\tslug VARCHAR(150) NOT NULL, \n\ttype VARCHAR(20) NOT NULL, \n\tcategory VARCHAR(30) NOT NULL, \n\tdescription TEXT, \n\ticon VARCHAR(500), \n\tauthor_id BIGINT, \n\tauthor_name VARCHAR(100), \n\tstatus VARCHAR(20) DEFAULT 'draft'::character varying NOT NULL, \n\tcurrent_version_id BIGINT, \n\thomepage VARCHAR(500), \n\tlicense VARCHAR(50), \n\tdownload_count INTEGER DEFAULT 0 NOT NULL, \n\tavg_rating NUMERIC(3, 1) DEFAULT 0.0 NOT NULL, \n\trating_count INTEGER DEFAULT 0 NOT NULL, \n\ttags_text TEXT, \n\tcreated_at TIMESTAMP WITH TIME ZONE DEFAULT now() NOT NULL, \n\tupdated_at TIMESTAMP WITH TIME ZONE DEFAULT now() NOT NULL, \n\tCONSTRAINT mk_app_pkey PRIMARY KEY (id), \n\tCONSTRAINT mk_app_author_id_fkey FOREIGN KEY(author_id) REFERENCES sys_user (user_id) ON DELETE SET NULL, \n\tCONSTRAINT mk_app_slug_key UNIQUE NULLS DISTINCT (slug), \n\tCONSTRAINT ck_mk_app_avg_rating_range CHECK (avg_rating >= 0::numeric AND avg_rating <= 5::numeric)\n)",
    "CREATE INDEX ix_mk_app_status_category ON mk_app (status, category)",
    "CREATE INDEX ix_mk_app_tenant_status ON mk_app (tenant_id, status)",
    "CREATE TABLE sys_role_dept (\n\trole_id BIGINT NOT NULL, \n\tdept_id BIGINT NOT NULL, \n\tCONSTRAINT sys_role_dept_pkey PRIMARY KEY (role_id, dept_id), \n\tCONSTRAINT sys_role_dept_dept_id_fkey FOREIGN KEY(dept_id) REFERENCES sys_dept (dept_id) ON DELETE CASCADE, \n\tCONSTRAINT sys_role_dept_role_id_fkey FOREIGN KEY(role_id) REFERENCES sys_role (role_id) ON DELETE CASCADE\n)",
    "CREATE TABLE sys_role_menu (\n\trole_id BIGINT NOT NULL, \n\tmenu_id BIGINT NOT NULL, \n\tCONSTRAINT sys_role_menu_pkey PRIMARY KEY (role_id, menu_id), \n\tCONSTRAINT sys_role_menu_menu_id_fkey FOREIGN KEY(menu_id) REFERENCES sys_menu (menu_id) ON DELETE CASCADE, \n\tCONSTRAINT sys_role_menu_role_id_fkey FOREIGN KEY(role_id) REFERENCES sys_role (role_id) ON DELETE CASCADE\n)",
    "CREATE TABLE sys_user_dept (\n\tuser_id BIGINT NOT NULL, \n\tdept_id BIGINT NOT NULL, \n\tis_primary VARCHAR(2) NOT NULL, \n\tCONSTRAINT sys_user_dept_pkey PRIMARY KEY (user_id, dept_id), \n\tCONSTRAINT sys_user_dept_dept_id_fkey FOREIGN KEY(dept_id) REFERENCES sys_dept (dept_id) ON DELETE CASCADE, \n\tCONSTRAINT sys_user_dept_user_id_fkey FOREIGN KEY(user_id) REFERENCES sys_user (user_id) ON DELETE CASCADE\n)",
    "CREATE TABLE sys_user_role (\n\tuser_id BIGINT NOT NULL, \n\trole_id BIGINT NOT NULL, \n\tCONSTRAINT sys_user_role_pkey PRIMARY KEY (user_id, role_id), \n\tCONSTRAINT sys_user_role_role_id_fkey FOREIGN KEY(role_id) REFERENCES sys_role (role_id) ON DELETE CASCADE, \n\tCONSTRAINT sys_user_role_user_id_fkey FOREIGN KEY(user_id) REFERENCES sys_user (user_id) ON DELETE CASCADE\n)",
    "CREATE TABLE ai_message (\n\tmessage_id BIGSERIAL NOT NULL, \n\tconversation_id BIGINT NOT NULL, \n\tparent_message_id BIGINT, \n\trole VARCHAR(20) NOT NULL, \n\tmessage_type VARCHAR(20) NOT NULL, \n\tcontent TEXT, \n\ttokens_input INTEGER, \n\ttokens_output INTEGER, \n\ttool_calls JSON, \n\tcreate_time TIMESTAMP WITHOUT TIME ZONE DEFAULT now() NOT NULL, \n\tparts JSON, \n\tCONSTRAINT ai_message_pkey PRIMARY KEY (message_id), \n\tCONSTRAINT ai_message_conversation_id_fkey FOREIGN KEY(conversation_id) REFERENCES ai_conversation (conversation_id) ON DELETE CASCADE\n)",
    "CREATE TABLE mk_app_permission (\n\tid BIGSERIAL NOT NULL, \n\tapp_id BIGINT NOT NULL, \n\ttype VARCHAR(30) NOT NULL, \n\tdetail JSONB NOT NULL, \n\tdetail_hash VARCHAR(64) NOT NULL, \n\tdetail_canonical TEXT NOT NULL, \n\tcreated_at TIMESTAMP WITH TIME ZONE DEFAULT now() NOT NULL, \n\tCONSTRAINT mk_app_permission_pkey PRIMARY KEY (id), \n\tCONSTRAINT mk_app_permission_app_id_fkey FOREIGN KEY(app_id) REFERENCES mk_app (id) ON DELETE CASCADE, \n\tCONSTRAINT uq_mk_app_permission_app_type_hash UNIQUE NULLS DISTINCT (app_id, type, detail_hash)\n)",
    "CREATE INDEX ix_mk_app_permission_app_type ON mk_app_permission (app_id, type)",
    "CREATE TABLE mk_app_rating (\n\tid BIGSERIAL NOT NULL, \n\tapp_id BIGINT NOT NULL, \n\tuser_id BIGINT NOT NULL, \n\trating SMALLINT NOT NULL, \n\tcomment TEXT, \n\tcreated_at TIMESTAMP WITH TIME ZONE DEFAULT now() NOT NULL, \n\tupdated_at TIMESTAMP WITH TIME ZONE DEFAULT now() NOT NULL, \n\tCONSTRAINT mk_app_rating_pkey PRIMARY KEY (id), \n\tCONSTRAINT mk_app_rating_app_id_fkey FOREIGN KEY(app_id) REFERENCES mk_app (id) ON DELETE CASCADE, \n\tCONSTRAINT mk_app_rating_user_id_fkey FOREIGN KEY(user_id) REFERENCES sys_user (user_id) ON DELETE CASCADE, \n\tCONSTRAINT uq_mk_app_rating_app_user UNIQUE NULLS DISTINCT (app_id, user_id), \n\tCONSTRAINT ck_mk_app_rating_range CHECK (rating >= 1 AND rating <= 5)\n)",
    "CREATE INDEX ix_mk_app_rating_app ON mk_app_rating (app_id)",
    "CREATE TABLE mk_app_version (\n\tid BIGSERIAL NOT NULL, \n\tapp_id BIGINT NOT NULL, \n\tversion VARCHAR(20) NOT NULL, \n\tchangelog TEXT, \n\tmanifest JSONB NOT NULL, \n\tfile_url VARCHAR(500) NOT NULL, \n\tfile_hash VARCHAR(64) NOT NULL, \n\tfile_size BIGINT, \n\treview_status VARCHAR(20) DEFAULT 'pending'::character varying NOT NULL, \n\tcreated_at TIMESTAMP WITH TIME ZONE DEFAULT now() NOT NULL, \n\tCONSTRAINT mk_app_version_pkey PRIMARY KEY (id), \n\tCONSTRAINT mk_app_version_app_id_fkey FOREIGN KEY(app_id) REFERENCES mk_app (id) ON DELETE CASCADE, \n\tCONSTRAINT uq_mk_app_version_app_version UNIQUE NULLS DISTINCT (app_id, version)\n)",
    "CREATE INDEX ix_mk_app_version_app_created ON mk_app_version (app_id, created_at)",
    "CREATE TABLE mk_tenant_app (\n\tid BIGSERIAL NOT NULL, \n\ttenant_id BIGINT DEFAULT '0'::bigint NOT NULL, \n\tapp_id BIGINT NOT NULL, \n\tinstalled_version VARCHAR(20) NOT NULL, \n\tstatus VARCHAR(20) DEFAULT 'installed'::character varying NOT NULL, \n\tconfig JSONB, \n\tapproved_permissions JSONB, \n\tretained_table_names JSONB, \n\thas_data BOOLEAN DEFAULT false NOT NULL, \n\tinstalled_at TIMESTAMP WITH TIME ZONE DEFAULT now() NOT NULL, \n\tupdated_at TIMESTAMP WITH TIME ZONE DEFAULT now() NOT NULL, \n\tCONSTRAINT mk_tenant_app_pkey PRIMARY KEY (id), \n\tCONSTRAINT mk_tenant_app_app_id_fkey FOREIGN KEY(app_id) REFERENCES mk_app (id) ON DELETE CASCADE, \n\tCONSTRAINT uq_mk_tenant_app_tenant_app UNIQUE NULLS DISTINCT (tenant_id, app_id)\n)",
    "CREATE INDEX ix_mk_tenant_app_tenant_status ON mk_tenant_app (tenant_id, status)",
    "CREATE TABLE mk_app_review (\n\tid BIGSERIAL NOT NULL, \n\tapp_id BIGINT NOT NULL, \n\tversion_id BIGINT NOT NULL, \n\trule_check_result JSONB, \n\trule_check_at TIMESTAMP WITH TIME ZONE, \n\tai_risk_level VARCHAR(10), \n\tai_report JSONB, \n\tai_review_at TIMESTAMP WITH TIME ZONE, \n\thuman_status VARCHAR(20) DEFAULT 'pending'::character varying NOT NULL, \n\thuman_reviewer_id BIGINT, \n\thuman_comment TEXT, \n\thuman_reviewed_at TIMESTAMP WITH TIME ZONE, \n\tfinal_status VARCHAR(20) DEFAULT 'pending'::character varying NOT NULL, \n\tcreated_at TIMESTAMP WITH TIME ZONE DEFAULT now() NOT NULL, \n\tupdated_at TIMESTAMP WITH TIME ZONE DEFAULT now() NOT NULL, \n\tCONSTRAINT mk_app_review_pkey PRIMARY KEY (id), \n\tCONSTRAINT mk_app_review_app_id_fkey FOREIGN KEY(app_id) REFERENCES mk_app (id) ON DELETE CASCADE, \n\tCONSTRAINT mk_app_review_human_reviewer_id_fkey FOREIGN KEY(human_reviewer_id) REFERENCES sys_user (user_id) ON DELETE SET NULL, \n\tCONSTRAINT mk_app_review_version_id_fkey FOREIGN KEY(version_id) REFERENCES mk_app_version (id) ON DELETE CASCADE\n)",
    "CREATE INDEX ix_mk_app_review_app_version ON mk_app_review (app_id, version_id)",
    "ALTER TABLE mk_app ADD CONSTRAINT fk_mk_app_current_version_id_mk_app_version FOREIGN KEY(current_version_id) REFERENCES mk_app_version (id) ON DELETE SET NULL",
]

_DOWNGRADE_DDL = [
    "DROP TABLE IF EXISTS mk_app_review CASCADE",
    "DROP TABLE IF EXISTS mk_tenant_app CASCADE",
    "DROP TABLE IF EXISTS mk_app_version CASCADE",
    "DROP TABLE IF EXISTS mk_app_rating CASCADE",
    "DROP TABLE IF EXISTS mk_app_permission CASCADE",
    "DROP TABLE IF EXISTS ai_message CASCADE",
    "DROP TABLE IF EXISTS sys_user_role CASCADE",
    "DROP TABLE IF EXISTS sys_user_dept CASCADE",
    "DROP TABLE IF EXISTS sys_role_menu CASCADE",
    "DROP TABLE IF EXISTS sys_role_dept CASCADE",
    "DROP TABLE IF EXISTS mk_app CASCADE",
    "DROP TABLE IF EXISTS ai_model CASCADE",
    "DROP TABLE IF EXISTS ai_conversation CASCADE",
    "DROP TABLE IF EXISTS sys_user CASCADE",
    "DROP TABLE IF EXISTS sys_role CASCADE",
    "DROP TABLE IF EXISTS sys_operation_log CASCADE",
    "DROP TABLE IF EXISTS sys_menu CASCADE",
    "DROP TABLE IF EXISTS sys_login_log CASCADE",
    "DROP TABLE IF EXISTS sys_job_log CASCADE",
    "DROP TABLE IF EXISTS sys_job CASCADE",
    "DROP TABLE IF EXISTS sys_file CASCADE",
    "DROP TABLE IF EXISTS sys_dict_type CASCADE",
    "DROP TABLE IF EXISTS sys_dict_data CASCADE",
    "DROP TABLE IF EXISTS sys_dept CASCADE",
    "DROP TABLE IF EXISTS sys_data_scope_demo CASCADE",
    "DROP TABLE IF EXISTS sys_config CASCADE",
    "DROP TABLE IF EXISTS ai_provider CASCADE",
]


def upgrade() -> None:
    for stmt in _UPGRADE_DDL:
        op.execute(stmt)


def downgrade() -> None:
    for stmt in _DOWNGRADE_DDL:
        op.execute(stmt)
