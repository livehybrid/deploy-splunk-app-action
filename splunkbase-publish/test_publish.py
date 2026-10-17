"""Tests for publish.py.

Every assertion about what was sent reads the FakeSplunkbase call log, which
the code under test never consults, so a guard that silently stops guarding
cannot pass by agreeing with itself. test_happy_path is the positive control
for the "nothing was uploaded" assertions: it proves the same log does record
an upload when one happens.
"""
import io
import json
import tarfile

import pytest

import publish

APP_ID = "4207"
PASSWORD = "s3cret-pa55word"


def make_package(tmp_path, app="TA-demo", version="1.2.2+b000f21", top=None, name=None):
    top = top or app
    conf = f"[id]\nname = {app}\nversion = {version}\n\n[launcher]\nversion = {version}\n\n[package]\nid = {app}\n"
    path = tmp_path / (name or f"{app}-{version}.tar.gz")
    with tarfile.open(path, "w:gz") as tar:
        data = conf.encode()
        info = tarfile.TarInfo(f"{top}/default/app.conf")
        info.size = len(data)
        tar.addfile(info, io.BytesIO(data))
    return path


class FakeSplunkbase:
    """Routes requests like the real service and records every call."""

    def __init__(self, appid="TA-demo", releases=("1.2.1+6ffd30d",), archived=False,
                 poll_results=("", "pass"), login_ok=True, upload_status=200, login_flaps=0):
        self.appid, self.release_names, self.archived = appid, list(releases), archived
        self.login_flaps = login_flaps
        self.poll_results = list(poll_results)
        self.login_ok, self.upload_status = login_ok, upload_status
        self.calls = []

    def methods(self):
        return [(m, u) for m, u, _, _ in self.calls]

    def sent(self, method):
        return [c for c in self.calls if c[0] == method]

    def __call__(self, method, url, headers=None, data=None, timeout=120):
        self.calls.append((method, url, headers or {}, data))
        if method == "GET" and url.endswith(f"/api/v1/app/{APP_ID}/"):
            return 200, json.dumps({"appid": self.appid, "is_archived": self.archived}).encode()
        if method == "GET" and url.endswith(f"/api/v1/app/{APP_ID}/release/"):
            return 200, json.dumps([{"name": n} for n in self.release_names]).encode()
        if method == "GET" and url == publish.LOGIN_URL:
            if self.login_flaps:
                self.login_flaps -= 1
                return 504, b'{"message": "Endpoint request timed out"}'
            if not self.login_ok:
                return 401, b'{"msg": "Failed to authenticate user"}'
            return 200, json.dumps({"data": {"token": "jwt-token-value"}}).encode()
        if method == "POST" and url.endswith(f"/api/v1/app/{APP_ID}/new_release/"):
            if self.upload_status >= 500:
                return self.upload_status, b'{"message": "Endpoint request timed out"}'
            if self.upload_status != 200:
                return self.upload_status, b'{"detail": "Cannot upload a new version while another release is pending validation."}'
            return 200, b'{"id": "pkg-77"}'
        if method == "GET" and "/api/v1/package/" in url:
            result = self.poll_results.pop(0) if len(self.poll_results) > 1 else self.poll_results[0]
            msg = {"release_file": 9911} if result == "pass" else {"error": "check_x failed"}
            return 200, json.dumps({"result": result, "message": msg}).encode()
        if method == "PUT" and "/api/v2/apps/" in url:
            return 200, b"{}"
        if method == "PATCH" and url.endswith(f"/api/v2/apps/{APP_ID}/"):
            return 200, b"{}"
        return 404, b"not routed"


def args_for(package, **over):
    argv = ["--app-id", APP_ID, "--package", str(package), "--splunk-versions", "10.6,10.4,10.0,9.4",
            "--poll-interval", "1", "--timeout", "60"]
    for k, v in over.items():
        flag = "--" + k.replace("_", "-")
        argv += [flag] if v is True else [flag, str(v)]
    return publish.parse_args(argv)


@pytest.fixture(autouse=True)
def creds(monkeypatch):
    monkeypatch.setenv("SPLUNKBASE_USERNAME", "will")
    monkeypatch.setenv("SPLUNKBASE_PASSWORD", PASSWORD)
    monkeypatch.delenv("GITHUB_ACTIONS", raising=False)
    monkeypatch.delenv("GITHUB_STEP_SUMMARY", raising=False)


def run(fake, args):
    clock = iter(range(0, 100000, 5))
    return publish.run(args, transport=fake, sleep=lambda s: None, clock=lambda: next(clock))


def test_happy_path_uploads_hidden_then_publishes(tmp_path):
    fake = FakeSplunkbase()
    assert run(fake, args_for(make_package(tmp_path), release_notes="notes here")) == 0
    order = [m for m, _ in fake.methods()]
    assert order == ["GET", "GET", "GET", "POST", "GET", "GET", "PUT", "PATCH"]
    post_body = fake.sent("POST")[0][3]
    assert b'name="visibility"\r\n\r\nfalse' in post_body
    assert b'name="splunk_versions"\r\n\r\n10.6,10.4,10.0,9.4' in post_body
    assert b'name="files[]"; filename="TA-demo-1.2.2+b000f21.tar.gz"' in post_body
    assert fake.sent("POST")[0][2]["Authorization"] == "Bearer jwt-token-value"
    put = fake.sent("PUT")[0]
    assert put[1].endswith(f"/api/v2/apps/{APP_ID}/releases/9911/")
    assert json.loads(put[3]) == {"public": True, "release_notes": "notes here"}
    assert json.loads(fake.sent("PATCH")[0][3]) == {"latest_release": 9911}


def test_already_published_is_a_noop(tmp_path):
    fake = FakeSplunkbase(releases=("1.2.1+6ffd30d", "1.2.2+b000f21"))
    assert run(fake, args_for(make_package(tmp_path))) == 0
    assert fake.sent("POST") == []
    assert not any(u == publish.LOGIN_URL for _, u in fake.methods())


def test_wrong_listing_refuses_before_login(tmp_path):
    fake = FakeSplunkbase(appid="some-other-app")
    with pytest.raises(publish.PublishError, match="Wrong app id"):
        run(fake, args_for(make_package(tmp_path)))
    assert fake.sent("POST") == []
    assert not any(u == publish.LOGIN_URL for _, u in fake.methods())


def test_archived_listing_refuses(tmp_path):
    fake = FakeSplunkbase(archived=True)
    with pytest.raises(publish.PublishError, match="archived"):
        run(fake, args_for(make_package(tmp_path)))
    assert fake.sent("POST") == []


def test_older_version_refuses(tmp_path):
    fake = FakeSplunkbase(releases=("1.2.1+6ffd30d", "1.3.0"))
    with pytest.raises(publish.PublishError, match="older than the newest"):
        run(fake, args_for(make_package(tmp_path)))
    assert fake.sent("POST") == []


def test_same_release_number_with_other_build_refuses(tmp_path):
    fake = FakeSplunkbase(releases=("1.2.2+aaaaaaa",))
    with pytest.raises(publish.PublishError, match="already on Splunkbase as '1.2.2\\+aaaaaaa'"):
        run(fake, args_for(make_package(tmp_path)))
    assert fake.sent("POST") == []


def test_failed_validation_never_goes_public(tmp_path):
    fake = FakeSplunkbase(poll_results=("", "fail"))
    with pytest.raises(publish.PublishError, match="validation FAILED.*stays hidden"):
        run(fake, args_for(make_package(tmp_path)))
    assert len(fake.sent("POST")) == 1
    assert fake.sent("PUT") == [] and fake.sent("PATCH") == []


def test_hidden_flag_validates_but_does_not_publish(tmp_path):
    fake = FakeSplunkbase()
    assert run(fake, args_for(make_package(tmp_path), hidden=True)) == 0
    assert len(fake.sent("POST")) == 1
    assert fake.sent("PUT") == [] and fake.sent("PATCH") == []


def test_dry_run_logs_in_but_uploads_nothing(tmp_path):
    fake = FakeSplunkbase()
    assert run(fake, args_for(make_package(tmp_path), dry_run=True)) == 0
    assert any(u == publish.LOGIN_URL for _, u in fake.methods())
    assert fake.sent("POST") == [] and fake.sent("PUT") == []


def test_login_failure_stops_before_upload(tmp_path):
    fake = FakeSplunkbase(login_ok=False)
    with pytest.raises(publish.PublishError, match="login failed"):
        run(fake, args_for(make_package(tmp_path)))
    assert fake.sent("POST") == []


def test_resume_with_package_id_skips_upload(tmp_path):
    fake = FakeSplunkbase(releases=("1.2.1+6ffd30d",))
    assert run(fake, args_for(make_package(tmp_path), package_id="pkg-77")) == 0
    assert fake.sent("POST") == []
    assert len(fake.sent("PUT")) == 1


def test_pending_upload_error_explains_itself(tmp_path):
    fake = FakeSplunkbase(upload_status=400)
    with pytest.raises(publish.PublishError, match="still being validated"):
        run(fake, args_for(make_package(tmp_path)))


def test_validation_timeout_names_the_package(tmp_path):
    fake = FakeSplunkbase(poll_results=("",))
    with pytest.raises(publish.PublishError, match="--package-id pkg-77"):
        run(fake, args_for(make_package(tmp_path), timeout=20))
    assert fake.sent("PUT") == []


def test_password_never_printed(tmp_path, capsys):
    for fake in (FakeSplunkbase(), FakeSplunkbase(login_ok=False), FakeSplunkbase(poll_results=("", "fail"))):
        try:
            run(fake, args_for(make_package(tmp_path)))
        except publish.PublishError as e:
            print(e)
    out = capsys.readouterr()
    assert PASSWORD not in out.out + out.err


def test_bad_splunk_versions_rejected(tmp_path):
    fake = FakeSplunkbase()
    with pytest.raises(publish.PublishError, match="splunk-versions"):
        run(fake, args_for(make_package(tmp_path), splunk_versions="10.6;latest"))
    assert fake.calls == []


def test_directory_must_hold_exactly_one_package(tmp_path):
    make_package(tmp_path, name="a.tar.gz")
    make_package(tmp_path, name="b.tar.gz")
    with pytest.raises(publish.PublishError, match="exactly one package"):
        publish.find_package(tmp_path)


def test_package_dir_must_match_app_id(tmp_path):
    pkg = make_package(tmp_path, app="TA-demo", top="TA-other")
    with pytest.raises(publish.PublishError, match="does not match the package directory"):
        publish.read_package(pkg)


@pytest.mark.parametrize("raw,key", [
    ("1.2.2+b000f21", (1, 2, 2)), ("1.1.4Rb8f0f45", (1, 1, 4)), ("v2026.2.10", (2026, 2, 10)),
    ("1.0", (1, 0, 0)), ("not-a-version", None),
])
def test_version_key(raw, key):
    assert publish.version_key(raw) == key


def test_transient_login_504_is_retried(tmp_path):
    fake = FakeSplunkbase(login_flaps=2)
    assert run(fake, args_for(make_package(tmp_path))) == 0
    assert sum(1 for _, u in fake.methods() if u == publish.LOGIN_URL) == 3
    assert len(fake.sent("POST")) == 1


def test_upload_5xx_is_not_retried(tmp_path):
    fake = FakeSplunkbase(upload_status=504)
    with pytest.raises(publish.PublishError, match="may still have gone through"):
        run(fake, args_for(make_package(tmp_path)))
    assert len(fake.sent("POST")) == 1
