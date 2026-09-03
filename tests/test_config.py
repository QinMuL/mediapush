"""config 测试：报告开关别名（新键优先，旧键兜底）+ 旧四段开关迁移提醒。"""

from __future__ import annotations

from app.config import Settings, _env_bool_alias, find_env_file


def test_alias_new_key_wins(monkeypatch):
    """新旧键都配时新键优先。"""
    monkeypatch.setenv("PIPELINE_REPORT_ADMIN", "false")
    monkeypatch.setenv("CD2_REPORT_ADMIN", "true")
    assert _env_bool_alias("PIPELINE_REPORT_ADMIN", "CD2_REPORT_ADMIN", True) is False


def test_alias_legacy_key_fallback(monkeypatch):
    """只配旧键时旧键生效。"""
    monkeypatch.setenv("CD2_REPORT_ADMIN", "false")
    assert _env_bool_alias("PIPELINE_REPORT_ADMIN", "CD2_REPORT_ADMIN", True) is False


def test_alias_default_when_neither_set(monkeypatch):
    """两键都未配 → 取默认值。"""
    assert _env_bool_alias("PIPELINE_REPORT_ADMIN", "CD2_REPORT_ADMIN", True) is True


def test_settings_load_report_alias_wiring(monkeypatch):
    """Settings.load 布线：PIPELINE_REPORT_ADMIN 落到 pipeline_report_admin。"""
    monkeypatch.setenv("PIPELINE_REPORT_ADMIN", "false")
    s = Settings.load(dotenv_override=False)
    assert s.pipeline_report_admin is False


def test_settings_load_report_legacy_wiring(monkeypatch):
    """旧键 CD2_REPORT_ADMIN 仍生效（向后兼容）。"""
    monkeypatch.setenv("CD2_REPORT_ADMIN", "false")
    s = Settings.load(dotenv_override=False)
    assert s.pipeline_report_admin is False


def test_settings_load_pipeline_dry_flags(monkeypatch):
    """三阶段 dry 开关布线。"""
    monkeypatch.setenv("PIPELINE_RENAME_DRY_RUN", "false")
    monkeypatch.setenv("PIPELINE_PUSH_DRY_RUN", "false")
    monkeypatch.setenv("PIPELINE_UPLOAD_DRY_RUN", "false")
    s = Settings.load(dotenv_override=False)
    assert s.pipeline_rename_dry_run is False
    assert s.pipeline_push_dry_run is False
    assert s.pipeline_upload_dry_run is False


def test_legacy_enabled_keys_trigger_migration_warning(monkeypatch):
    """旧四段开关任一为 true 且未开 PIPELINE → validate 提示迁移。"""
    monkeypatch.setenv("LOCAL_MEDIA_ENABLED", "true")
    monkeypatch.setenv("PIPELINE_ENABLED", "false")
    s = Settings.load(dotenv_override=False)
    warns = s.validate()
    assert any("旧版四段开关" in w and "LOCAL_MEDIA_ENABLED" in w for w in warns)


def test_no_legacy_warning_when_clean(monkeypatch):
    """无旧键或旧键全 false → 无迁移警告。"""
    monkeypatch.setenv("ED2K_ENABLED", "false")
    monkeypatch.setenv("PIPELINE_ENABLED", "false")
    s = Settings.load(dotenv_override=False)
    warns = s.validate()
    assert not any("旧版四段开关" in w for w in warns)


def test_pipeline_dir_validation(monkeypatch):
    """目录未配/互相嵌套 → 告警。"""
    monkeypatch.setenv("PIPELINE_ENABLED", "true")
    monkeypatch.setenv("PIPELINE_INPUT_DIR", "/data/A")
    monkeypatch.setenv("PIPELINE_LIBRARY_DIR", "/data/A/sub")
    s = Settings.load(dotenv_override=False)
    warns = s.validate()
    assert any("不得互相嵌套" in w for w in warns)


# ---------------- cookie 文件首启自动创建 ---------------- #
def test_cookie_file_auto_created_on_first_deploy(tmp_path, monkeypatch):
    """PAN115_COOKIE_FILE 指向不存在路径：load() 创建空文件（首次部署体验），
    cookie 视为未配置（匿名模式），不产生读取告警。"""
    ck = tmp_path / "sub" / "115cookie.txt"
    monkeypatch.setenv("PAN115_COOKIE_FILE", str(ck))
    monkeypatch.setenv("DB_PATH", str(tmp_path / "cache.db"))
    monkeypatch.setenv("LOG_FILE", str(tmp_path / "logs" / "mediapush.log"))

    s = Settings.load(dotenv_override=False)

    assert ck.exists()          # 空占位已创建
    assert ck.read_text(encoding="utf-8") == ""
    assert s.pan115_cookie == ""    # 空 = 未配置
    assert s.pan115_cookie_direct is False


def test_cookie_file_existing_not_overwritten(tmp_path, monkeypatch):
    """已有内容的 cookie 文件：不覆盖、内容照常读入。"""
    ck = tmp_path / "115cookie.txt"
    ck.write_text("UID=1;CID=2;", encoding="utf-8")
    monkeypatch.setenv("PAN115_COOKIE_FILE", str(ck))
    monkeypatch.setenv("DB_PATH", str(tmp_path / "cache.db"))
    monkeypatch.setenv("LOG_FILE", str(tmp_path / "logs" / "mediapush.log"))

    s = Settings.load(dotenv_override=False)

    assert ck.read_text(encoding="utf-8") == "UID=1;CID=2;"
    assert s.pan115_cookie == "UID=1;CID=2;"


# ---------------------------------------------------------------------- #
# .env 文件数据源（回归：容器内无 .env → /reload 永远假报无变更）
# ---------------------------------------------------------------------- #
def test_find_env_file_search_order(tmp_path, monkeypatch):
    """按 _ENV_FILE_SEARCH 顺序取第一个存在的；都不存在返回 None。"""
    a, b = tmp_path / "a.env", tmp_path / "b.env"
    b.write_text("X=1\n", encoding="utf-8")
    monkeypatch.setattr("app.config._ENV_FILE_SEARCH", (a, b))
    assert find_env_file() == b
    monkeypatch.setattr("app.config._ENV_FILE_SEARCH", (a,))
    assert find_env_file() is None


def test_load_env_file_source_semantics(tmp_path, monkeypatch):
    """核心回归：/reload（override=True）用挂载 .env 的值覆盖容器注入的
    创建时快照；启动（override=False）快照优先——这是热加载能感知宿主机
    .env 修改的机制基础。"""
    env = tmp_path / ".env"
    env.write_text("PIPELINE_CLEAN_DRY_RUN=false\n", encoding="utf-8")
    monkeypatch.setattr("app.config._ENV_FILE_SEARCH", (env,))
    monkeypatch.delenv("PIPELINE_CLEAN_DRY_RUN", raising=False)

    # 启动语义：env 变量（env_file 注入快照）优先，文件不覆盖
    monkeypatch.setenv("PIPELINE_CLEAN_DRY_RUN", "true")
    s = Settings.load(dotenv_override=False)
    assert s.pipeline_clean_dry_run is True

    # /reload 语义：文件值覆盖快照
    monkeypatch.setenv("PIPELINE_CLEAN_DRY_RUN", "true")
    s = Settings.load(dotenv_override=True)
    assert s.pipeline_clean_dry_run is False
