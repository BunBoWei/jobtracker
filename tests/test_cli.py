import socket
import threading
import time

import httpx
import pytest
from filelock import FileLock

from job_tracker import cli


def free_port():
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        return s.getsockname()[1]


@pytest.fixture
def env(tmp_path, monkeypatch):
    port = free_port()
    (tmp_path / "Employment").mkdir()  # the data folder's parent (Google Drive) must exist; the folder itself is created
    monkeypatch.setenv("JOBS_PORT", str(port))
    monkeypatch.setenv("JOBS_DATA_DIR", str(tmp_path / "Employment" / "Job Tracker"))
    monkeypatch.setenv("JOBS_LOG_FILE", str(tmp_path / "server.log"))
    opened = []
    monkeypatch.setattr(cli, "open_browser", opened.append)
    yield {"port": port, "opened": opened, "data": tmp_path / "Employment" / "Job Tracker"}
    cli.stop_server()  # never leave a server behind


def test_status_when_not_running(env, capsys):
    assert cli.main(["status"]) == 0
    assert "not running" in capsys.readouterr().out


def test_is_running_ignores_foreign_listener(env):
    import http.server

    srv = http.server.HTTPServer(("127.0.0.1", env["port"]), http.server.SimpleHTTPRequestHandler)
    threading.Thread(target=srv.serve_forever, daemon=True).start()
    try:
        assert cli.is_running() is False   # something answers on the port, but it is not Job Tracker
    finally:
        srv.shutdown()


def test_jobs_starts_server_once_then_only_opens_browser(env, monkeypatch):
    assert cli.main([]) == 0
    assert cli.is_running()
    assert env["opened"] == [f"http://localhost:{env['port']}"]

    spawned = []
    with monkeypatch.context() as m:  # scoped, so the fixture's cleanup can still spawn `lsof`
        m.setattr(cli.subprocess, "Popen", lambda *a, **k: spawned.append(a) or pytest.fail("second server started"))
        assert cli.main([]) == 0
    assert spawned == [] and len(env["opened"]) == 2   # browser opened again, no new server


def test_jobs_stop_frees_the_port(env, capsys):
    cli.main([])
    assert cli.main(["stop"]) == 0
    assert not cli.is_running()
    with socket.socket() as s:
        assert s.connect_ex(("127.0.0.1", env["port"])) != 0
    assert cli.main(["stop"]) == 0 and "not running" in capsys.readouterr().out


def test_stop_during_a_write_lets_the_write_finish(env):
    cli.main([])
    base = f"http://127.0.0.1:{env['port']}"
    # create a job to update, then hold the data lock so the next request is stuck mid-write
    app_id = httpx.post(base + "/add", data={"company": "Acme", "role": "Dev", "status": "Ready"}, follow_redirects=False).headers["location"].rsplit("/", 1)[1]
    result = {}

    with FileLock(str(env["data"] / ".tracker.lock")):
        def slow_write():
            result["r"] = httpx.post(f"{base}/a/{app_id}/status", data={"status": "Interview"}, timeout=30, follow_redirects=False)

        t = threading.Thread(target=slow_write)
        t.start()
        time.sleep(1)                                   # the request is now waiting on the lock
        stopper = threading.Thread(target=cli.stop_server)
        stopper.start()                                 # SIGTERM while the write is in flight
        time.sleep(1)
        assert stopper.is_alive()                       # server is draining, not dead
    t.join(timeout=30)
    stopper.join(timeout=30)
    assert result["r"].status_code == 303
    rows = [l for l in (env["data"] / "applications.csv").read_text().splitlines() if app_id in l]
    assert "Interview" in rows[0]
    assert not cli.is_running()


def test_launcher_source_quotes_path():
    src = cli.launcher_source('/Users/a b/.local/bin/jo"bs')
    assert src == 'do shell script quoted form of "/Users/a b/.local/bin/jo\\"bs"\n'


def test_command_is_not_named_jobs(capsys):
    """`jobs` is a zsh builtin, so the installed command must be called something else."""
    import tomllib
    from pathlib import Path

    scripts = tomllib.loads(Path(__file__).parents[1].joinpath("pyproject.toml").read_text())["project"]["scripts"]
    assert scripts == {"jobtracker": "job_tracker.cli:main"}
    with pytest.raises(SystemExit):
        cli.main(["--help"])
    assert "usage: jobtracker" in capsys.readouterr().out
