from scripts import docker_setup
from scripts.docker_setup import build_env, env_value, read_env


def test_docker_setup_generates_secrets_and_required_paths() -> None:
    content = build_env({
        "data_path": "/volume1/docker/douyin/data",
        "download_path": "/volume1/video/douyin downloads",
        "browser_path": "/volume1/docker/douyin/browser",
        "web_port": "8765",
        "novnc_port": "6080",
        "download_concurrency": "1",
        "novnc_enabled": "true",
    })

    assert "MYSQL_PASSWORD=" in content
    assert "MYSQL_ROOT_PASSWORD=" in content
    assert "DOUYIN_VNC_PASSWORD=" in content
    assert 'DOUYIN_DOWNLOAD_PATH="/volume1/video/douyin downloads"' in content
    assert "DOUYIN_DOWNLOAD_CONCURRENCY=1" in content


def test_docker_setup_validates_limits_and_quotes_values() -> None:
    assert env_value("path with spaces") == '"path with spaces"'
    try:
        build_env({"web_port": "70000", "novnc_port": "6080", "download_concurrency": "1"})
    except ValueError as exc:
        assert "端口" in str(exc)
    else:
        raise AssertionError("invalid port should fail")


def test_docker_setup_preserves_existing_secrets(tmp_path) -> None:
    current = tmp_path / ".env"
    current.write_text('MYSQL_PASSWORD="old password"\nMYSQL_ROOT_PASSWORD=old-root\nDOUYIN_VNC_PASSWORD=old-vnc\n', encoding="utf-8")
    existing = read_env(current)
    content = build_env({"web_port": "8765", "novnc_port": "6080", "download_concurrency": "1"}, existing)

    assert 'MYSQL_PASSWORD="old password"' in content
    assert "MYSQL_ROOT_PASSWORD=old-root" in content
    assert "DOUYIN_VNC_PASSWORD=old-vnc" in content


def test_docker_setup_prefills_existing_choices(tmp_path, monkeypatch) -> None:
    (tmp_path / ".env").write_text(
        "DOUYIN_DOWNLOAD_CONCURRENCY=3\nDOUYIN_LINUX_NOVNC_ENABLED=false\nMYSQL_INNODB_BUFFER_POOL_SIZE=512M\n",
        encoding="utf-8",
    )
    monkeypatch.setattr(docker_setup, "WORKSPACE", tmp_path)

    rendered = docker_setup.form_page().decode("utf-8")

    assert '<option value="3" selected>' in rendered
    assert '<option value="false" selected>' in rendered
    assert '<option value="512M" selected>' in rendered
