import io
import json
import re
from pathlib import Path
from urllib.error import HTTPError, URLError

import pytest


WORKFLOW = Path(__file__).resolve().parents[1] / ".github" / "workflows" / "release.yml"


def _python_step(name):
    text = WORKFLOW.read_text(encoding="utf-8")
    step = text.split(f"      - name: {name}\n", 1)[1].split("      - name:", 1)[0]
    code = step.split("        run: |\n", 1)[1]
    return "\n".join(line[10:] if line.startswith("          ") else line for line in code.splitlines())


def _release_check(monkeypatch, tmp_path, responses):
    output = tmp_path / "github-output"
    monkeypatch.setenv("GITHUB_API_URL", "https://api.github.com")
    monkeypatch.setenv("GITHUB_REPOSITORY", "example/beamchat")
    monkeypatch.setenv("RELEASE_TAG", "v1.2.3")
    monkeypatch.setenv("GITHUB_TOKEN", "test-secret-token")
    monkeypatch.setenv("GITHUB_OUTPUT", str(output))
    requested = []
    replies = iter(responses)

    def fake_urlopen(request, *, timeout):
        requested.append(request.full_url)
        assert request.get_header("Authorization") == "Bearer test-secret-token"
        assert timeout == 30
        reply = next(replies)
        if isinstance(reply, Exception):
            raise reply
        return io.BytesIO(json.dumps(reply).encode("utf-8"))

    monkeypatch.setattr("urllib.request.urlopen", fake_urlopen)
    exec(compile(_python_step("检查是否已有发布或草稿"), str(WORKFLOW), "exec"), {})
    return output.read_text(encoding="utf-8"), requested


def _http_error(code):
    return HTTPError("https://api.github.com/releases", code, "test", {}, None)


def test_release_check_preserves_published_release(monkeypatch, tmp_path, capsys):
    output, requested = _release_check(monkeypatch, tmp_path, [{"tag_name": "v1.2.3"}])
    assert output == "exists=true\n"
    assert len(requested) == 1
    assert "test-secret-token" not in capsys.readouterr().out


def test_release_check_preserves_draft_on_later_page(monkeypatch, tmp_path):
    first_page = [{"tag_name": f"v0.0.{index}"} for index in range(100)]
    output, requested = _release_check(
        monkeypatch,
        tmp_path,
        [_http_error(404), first_page, [{"tag_name": "v1.2.3", "draft": True}]],
    )
    assert output == "exists=true\n"
    assert requested[-1].endswith("?per_page=100&page=2")


def test_release_check_allows_new_release_after_404_and_no_draft(monkeypatch, tmp_path):
    output, requested = _release_check(monkeypatch, tmp_path, [_http_error(404), []])
    assert output == "exists=false\n"
    assert len(requested) == 2


@pytest.mark.parametrize("code", [401, 403, 429, 500, 503])
def test_release_check_fails_closed_on_http_error(monkeypatch, tmp_path, code):
    with pytest.raises(RuntimeError, match=f"HTTP {code}"):
        _release_check(monkeypatch, tmp_path, [_http_error(code)])
    assert not (tmp_path / "github-output").exists()


def test_release_check_fails_on_draft_listing_error(monkeypatch, tmp_path):
    with pytest.raises(RuntimeError, match="HTTP 403"):
        _release_check(monkeypatch, tmp_path, [_http_error(404), _http_error(403)])
    assert not (tmp_path / "github-output").exists()


def test_release_check_fails_on_network_error(monkeypatch, tmp_path):
    with pytest.raises(RuntimeError, match="无法连接"):
        _release_check(monkeypatch, tmp_path, [URLError("test")])
    assert not (tmp_path / "github-output").exists()


@pytest.mark.parametrize("responses", [[None], [_http_error(404), {}], [_http_error(404), [{}]]])
def test_release_check_fails_on_incomplete_response(monkeypatch, tmp_path, responses):
    with pytest.raises(RuntimeError, match="数据不完整"):
        _release_check(monkeypatch, tmp_path, responses)
    assert not (tmp_path / "github-output").exists()


def test_workflow_updates_both_versions_without_protocol_changes(monkeypatch, tmp_path):
    monkeypatch.chdir(tmp_path)
    monkeypatch.setenv("RELEASE_VERSION", "1.2.3")
    protocol = 'PROTOCOL_VERSION = 5\nCLIENT_VERSION = "1.2.2"\nBASE_CAPABILITIES = ["encrypted_files"]\n'
    Path("protocol.py").write_text(protocol, encoding="utf-8")
    Path("version.py").write_text('__version__ = "1.2.2"\n', encoding="utf-8")
    exec(compile(_python_step("写入版本文件"), str(WORKFLOW), "exec"), {})
    assert Path("version.py").read_text(encoding="utf-8") == '__version__ = "1.2.3"\n'
    assert Path("protocol.py").read_text(encoding="utf-8") == protocol.replace("1.2.2", "1.2.3")


@pytest.mark.parametrize("protocol", ['PROTOCOL_VERSION = 5\n', 'CLIENT_VERSION = "1"\nCLIENT_VERSION = "2"\n'])
def test_workflow_rejects_missing_or_duplicate_client_version(monkeypatch, tmp_path, protocol):
    monkeypatch.chdir(tmp_path)
    monkeypatch.setenv("RELEASE_VERSION", "1.2.3")
    Path("protocol.py").write_text(protocol, encoding="utf-8")
    previous = '__version__ = "1.2.2"\n'
    Path("version.py").write_text(previous, encoding="utf-8")
    with pytest.raises(RuntimeError, match="唯一"):
        exec(compile(_python_step("写入版本文件"), str(WORKFLOW), "exec"), {})
    assert Path("protocol.py").read_text(encoding="utf-8") == protocol
    assert Path("version.py").read_text(encoding="utf-8") == previous


def test_workflow_publish_step_uses_guard_and_preserves_existing_assets():
    step = WORKFLOW.read_text(encoding="utf-8").split("      - name: 创建 GitHub Release 并上传 EXE\n", 1)[1]
    assert re.search(r"if:.*steps\.existing_release\.outputs\.exists == 'false'", step)
    assert "overwrite_files: false" in step
