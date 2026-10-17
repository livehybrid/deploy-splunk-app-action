#!/usr/bin/env python3
"""Publish a packaged Splunk app to its Splunkbase listing.

The flow mirrors the publisher Splunk use for their own add-on
(signalfx/splunk-otel-collector, moved to JWT auth in 2026-09 because
"Splunkbase is asking to switch to JWT"):

  1. Checks against the PUBLIC listing, before anything is sent:
       - the package's app id matches the listing's appid (wrong listing: stop)
       - the listing is not archived
       - the version is not already published (already there: success, so
         re-running a tag pipeline is a no-op)
       - the version is newer than the newest published release
  2. Log in at api.splunk.com for a JWT.
  3. Upload as a HIDDEN release (visibility=false).
  4. Poll the package until Splunkbase's own vetting passes or fails.
  5. On a pass, make the release public with release notes and set it as the
     listing's latest release.

A release therefore only goes public after Splunkbase has validated it. A
failed vetting leaves a hidden release, which can be deleted from the
Splunkbase UI. A visible release can never be deleted or replaced, so that
step is last.

Credentials come from SPLUNKBASE_USERNAME / SPLUNKBASE_PASSWORD and are never
printed. Standard library only, so it runs on any runner without installs.
"""
from __future__ import annotations

import argparse
import base64
import configparser
import json
import os
import re
import sys
import tarfile
import time
import urllib.error
import urllib.request
import uuid
from pathlib import Path

SPLUNKBASE = "https://splunkbase.splunk.com"
LOGIN_URL = "https://api.splunk.com/2.0/rest/login/splunk"
PACKAGE_SUFFIXES = (".tar.gz", ".tgz", ".spl")
SPLUNK_VERSIONS_RE = re.compile(r"^\d+\.\d+(,\d+\.\d+)*$")
PASS_RESULTS = {"pass", "passed", "success"}
FAIL_RESULTS = {"fail", "failed", "failure", "error", "rejected"}


class PublishError(Exception):
    """A condition that must stop the publish. The message is shown to the user."""


# --------------------------------------------------------------------------
# HTTP. One seam, so tests can swap the transport and see every call made.
# --------------------------------------------------------------------------
def urllib_transport(method, url, headers=None, data=None, timeout=120):
    req = urllib.request.Request(url, data=data, method=method, headers=headers or {})
    try:
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            return resp.status, resp.read()
    except urllib.error.HTTPError as e:
        return e.code, e.read()
    except (urllib.error.URLError, TimeoutError, ConnectionError) as e:
        return 0, f"network error: {e}".encode()


def _json(body):
    try:
        return json.loads(body.decode("utf-8") if isinstance(body, bytes) else body)
    except (ValueError, UnicodeDecodeError):
        return None


def _snippet(body, limit=800):
    text = body.decode("utf-8", "replace") if isinstance(body, bytes) else str(body)
    return text[:limit]


# --------------------------------------------------------------------------
# Package inspection
# --------------------------------------------------------------------------
def find_package(path):
    p = Path(path)
    if p.is_file():
        return p
    if not p.is_dir():
        raise PublishError(f"No package at {path}")
    found = sorted(x for x in p.rglob("*") if x.is_file() and x.name.endswith(PACKAGE_SUFFIXES))
    if not found:
        raise PublishError(f"No .tar.gz/.tgz/.spl package under {path}")
    if len(found) > 1:
        names = ", ".join(x.name for x in found)
        raise PublishError(f"Expected exactly one package under {path}, found {len(found)}: {names}")
    return found[0]


def read_package(path):
    """Return (app_id, version) from the package's default/app.conf."""
    with tarfile.open(path, "r:*") as tar:
        members = {}
        for m in tar.getmembers():
            name = m.name[2:] if m.name.startswith("./") else m.name
            if name and name != ".":
                members[name] = m
        tops = {name.split("/", 1)[0] for name in members}
        if len(tops) != 1:
            raise PublishError(f"Package must contain exactly one top-level app directory, found {sorted(tops)}")
        top = tops.pop()
        member = members.get(f"{top}/default/app.conf")
        if member is None:
            raise PublishError(f"{top}/default/app.conf is missing from the package")
        conf_text = tar.extractfile(member).read().decode("utf-8", "replace")
    conf = configparser.ConfigParser(strict=False, interpolation=None)
    conf.optionxform = str
    conf.read_string(conf_text)
    app_id = conf.get("package", "id", fallback=None) or conf.get("id", "name", fallback=None) or top
    version = conf.get("launcher", "version", fallback=None) or conf.get("id", "version", fallback=None)
    if not version:
        raise PublishError(f"No version in {top}/default/app.conf ([launcher] or [id])")
    if app_id != top:
        raise PublishError(f"app.conf id '{app_id}' does not match the package directory '{top}'")
    return app_id, version.strip()


def version_key(version):
    """Numeric release tuple, ignoring build metadata: '1.2.2+b000f21' -> (1, 2, 2).

    Copes with the estate's older stamps too ('1.1.4Rb8f0f45' -> (1, 1, 4)).
    """
    m = re.match(r"\s*v?(\d+(?:\.\d+)*)", version or "")
    if not m:
        return None
    parts = [int(x) for x in m.group(1).split(".")]
    while len(parts) < 3:
        parts.append(0)
    return tuple(parts)


# --------------------------------------------------------------------------
# Splunkbase
# --------------------------------------------------------------------------
class Splunkbase:
    # Read-only calls are retried on gateway/network failures: api.splunk.com
    # answers the odd login with HTTP 504 "Endpoint request timed out". The
    # upload is NEVER retried, because a 5xx there may still have created the
    # release, and a blind retry could upload it twice.
    GET_RETRIES = 4

    def __init__(self, app_id, transport=urllib_transport, log=print, sleep=time.sleep):
        self.app_id = str(app_id)
        self.transport = transport
        self.log = log
        self.sleep = sleep
        self.jwt = None

    def _get(self, url, headers=None):
        for attempt in range(self.GET_RETRIES + 1):
            status, body = self.transport("GET", url, headers=headers or {})
            if status != 0 and status < 500:
                return status, body
            if attempt < self.GET_RETRIES:
                self.log(f"  GET {url.split('?')[0]} -> HTTP {status or 'network error'}, retrying")
                self.sleep(2 ** (attempt + 1))
        return status, body

    def _get_public(self, url):
        status, body = self._get(url)
        if status != 200:
            raise PublishError(f"GET {url} returned HTTP {status}: {_snippet(body)}")
        data = _json(body)
        if data is None:
            raise PublishError(f"GET {url} did not return JSON: {_snippet(body)}")
        return data

    def listing(self):
        return self._get_public(f"{SPLUNKBASE}/api/v1/app/{self.app_id}/")

    def releases(self):
        data = self._get_public(f"{SPLUNKBASE}/api/v1/app/{self.app_id}/release/")
        if not isinstance(data, list):
            raise PublishError(f"Unexpected release listing shape: {_snippet(json.dumps(data))}")
        return [r.get("name", "") for r in data if r.get("name")]

    def login(self, username, password):
        basic = base64.b64encode(f"{username}:{password}".encode()).decode()
        status, body = self._get(LOGIN_URL, headers={"Authorization": f"Basic {basic}"})
        data = _json(body) or {}
        token = (data.get("data") or {}).get("token")
        if status != 200 or not token:
            # The body carries an error message, never the credentials.
            raise PublishError(f"Splunk API login failed (HTTP {status}): {_snippet(body, 300)}")
        if os.environ.get("GITHUB_ACTIONS") == "true":
            print(f"::add-mask::{token}")
        self.jwt = token

    def _auth(self, extra=None):
        h = {"Authorization": f"Bearer {self.jwt}"}
        h.update(extra or {})
        return h

    def upload_hidden(self, package, splunk_versions, cim_versions):
        boundary = uuid.uuid4().hex
        fields = {"filename": package.name, "splunk_versions": splunk_versions, "visibility": "false"}
        if cim_versions:
            fields["cim_versions"] = cim_versions
        parts = []
        for name, value in fields.items():
            parts += [f"--{boundary}".encode(), f'Content-Disposition: form-data; name="{name}"'.encode(),
                      b"", str(value).encode()]
        parts += [f"--{boundary}".encode(),
                  f'Content-Disposition: form-data; name="files[]"; filename="{package.name}"'.encode(),
                  b"Content-Type: application/gzip", b"", package.read_bytes(),
                  f"--{boundary}--".encode(), b""]
        body = b"\r\n".join(parts)
        url = f"{SPLUNKBASE}/api/v1/app/{self.app_id}/new_release/"
        status, resp = self.transport(
            "POST", url, data=body,
            headers=self._auth({"Content-Type": f"multipart/form-data; boundary={boundary}"}))
        data = _json(resp) or {}
        if status not in (200, 201, 202) or not data.get("id"):
            hint = ""
            if status == 0 or status >= 500:
                hint = (" The request may still have gone through: check the listing's releases in the "
                        "Splunkbase UI before re-running, and finish an upload that did land with --package-id.")
            elif "pending" in _snippet(resp).lower():
                hint = (" Splunkbase refuses a new upload while another release of this app is still "
                        "being validated; wait for it, or finish it with --package-id.")
            raise PublishError(f"Upload failed (HTTP {status}): {_snippet(resp)}{hint}")
        return str(data["id"])

    def wait_for_validation(self, package_id, timeout, interval, sleep=time.sleep, clock=time.monotonic):
        url = f"{SPLUNKBASE}/api/v1/package/{package_id}/"
        deadline = clock() + timeout
        last = None
        while True:
            status, body = self._get(url, headers=self._auth())
            data = _json(body) or {}
            result = str(data.get("result") or "").lower()
            if status == 200 and result != last:
                self.log(f"  validation: {result or 'pending'}")
                last = result
            if status == 200 and result in PASS_RESULTS:
                message = data.get("message")
                release_file = message.get("release_file") if isinstance(message, dict) else None
                if not release_file:
                    raise PublishError(f"Validation passed but no release_file came back: {_snippet(body)}")
                return str(release_file)
            if status == 200 and result in FAIL_RESULTS:
                raise PublishError(
                    f"Splunkbase validation FAILED for package {package_id}; the release stays hidden. "
                    f"Details: {_snippet(json.dumps(data.get('message') or data), 1500)}")
            if status not in (200, 404):
                raise PublishError(f"Polling package {package_id} returned HTTP {status}: {_snippet(body)}")
            if clock() >= deadline:
                raise PublishError(
                    f"Timed out after {timeout}s waiting for Splunkbase to validate package {package_id}. "
                    f"The release is uploaded but hidden. Re-run with --package-id {package_id} to finish it.")
            sleep(interval)

    def make_public(self, release_file, release_notes):
        url = f"{SPLUNKBASE}/api/v2/apps/{self.app_id}/releases/{release_file}/"
        body = json.dumps({"public": True, "release_notes": release_notes}).encode()
        status, resp = self.transport("PUT", url, data=body, headers=self._auth({"Content-Type": "application/json"}))
        if status not in (200, 201, 202, 204):
            raise PublishError(f"Making release {release_file} public failed (HTTP {status}): {_snippet(resp)}")

    def set_latest(self, release_file):
        url = f"{SPLUNKBASE}/api/v2/apps/{self.app_id}/"
        body = json.dumps({"latest_release": int(release_file) if str(release_file).isdigit() else release_file}).encode()
        status, resp = self.transport("PATCH", url, data=body, headers=self._auth({"Content-Type": "application/json"}))
        if status not in (200, 201, 202, 204):
            raise PublishError(f"Setting release {release_file} as latest failed (HTTP {status}): {_snippet(resp)}")


# --------------------------------------------------------------------------
# Orchestration
# --------------------------------------------------------------------------
def preflight(sb, app_id, version):
    """Listing checks. Returns 'publish' or 'already-published'. Raises on any mismatch."""
    listing = sb.listing()
    listed_id = listing.get("appid")
    if listed_id != app_id:
        raise PublishError(f"Splunkbase listing {sb.app_id} is '{listed_id}' but the package is '{app_id}'. "
                           "Wrong app id for this package, refusing to upload.")
    if listing.get("is_archived"):
        raise PublishError(f"Splunkbase listing {sb.app_id} ('{listed_id}') is archived.")
    published = sb.releases()
    if version in published:
        return "already-published"
    key = version_key(version)
    if key is None:
        raise PublishError(f"Cannot read a release number from package version '{version}'.")
    for name in published:
        other = version_key(name)
        if other == key:
            raise PublishError(f"Release {key} is already on Splunkbase as '{name}' (package says '{version}').")
    newest = max((version_key(n) for n in published if version_key(n)), default=None)
    if newest and key < newest:
        raise PublishError(f"Package version '{version}' is older than the newest Splunkbase release "
                           f"{'.'.join(map(str, newest))}; refusing to publish out of order.")
    return "publish"


def summary(line):
    print(line)
    path = os.environ.get("GITHUB_STEP_SUMMARY")
    if path:
        with open(path, "a", encoding="utf-8") as fh:
            fh.write(line + "\n")


def run(args, transport=urllib_transport, sleep=time.sleep, clock=time.monotonic):
    if not SPLUNK_VERSIONS_RE.match(args.splunk_versions.replace(" ", "")):
        raise PublishError(f"--splunk-versions must look like '10.6,10.4,9.4', got '{args.splunk_versions}'")
    splunk_versions = args.splunk_versions.replace(" ", "")
    package = find_package(args.package)
    app_id, version = read_package(package)
    sb = Splunkbase(args.app_id, transport=transport, sleep=sleep)
    print(f"Package {package.name}: app '{app_id}' version '{version}' -> Splunkbase listing {args.app_id}")

    if not args.package_id:
        if preflight(sb, app_id, version) == "already-published":
            summary(f"Splunkbase {args.app_id}: '{version}' is already published, nothing to do.")
            return 0

    username = os.environ.get("SPLUNKBASE_USERNAME", "")
    password = os.environ.get("SPLUNKBASE_PASSWORD", "")
    if not username or not password:
        raise PublishError("SPLUNKBASE_USERNAME and SPLUNKBASE_PASSWORD must be set.")
    sb.login(username, password)

    if args.dry_run:
        visibility = "hidden" if args.hidden else "public after validation"
        summary(f"DRY RUN Splunkbase {args.app_id}: would upload '{version}' ({package.name}) for Splunk "
                f"{splunk_versions}, {visibility}. Listing checks and login passed.")
        return 0

    package_id = args.package_id or sb.upload_hidden(package, splunk_versions, args.cim_versions)
    print(f"Uploaded as hidden package {package_id}; waiting for Splunkbase validation...")
    release_file = sb.wait_for_validation(package_id, args.timeout, args.poll_interval, sleep=sleep, clock=clock)

    if args.hidden:
        summary(f"Splunkbase {args.app_id}: '{version}' validated and left HIDDEN (release {release_file}). "
                "Make it visible from the Splunkbase UI when ready.")
        return 0
    sb.make_public(release_file, args.release_notes or f"{app_id} {version}")
    sb.set_latest(release_file)
    summary(f"Splunkbase {args.app_id}: published '{version}' (release {release_file}) and set it as latest. "
            f"https://splunkbase.splunk.com/app/{args.app_id}")
    return 0


def parse_args(argv=None):
    p = argparse.ArgumentParser(description=__doc__.split("\n")[0])
    p.add_argument("--app-id", required=True, help="Numeric Splunkbase app id (the number in the listing URL)")
    p.add_argument("--package", default="dist", help="Package file, or a directory holding exactly one")
    p.add_argument("--splunk-versions", required=True, help="Comma-separated, e.g. 10.6,10.4,10.2,10.0,9.4")
    p.add_argument("--cim-versions", default="", help="Comma-separated, e.g. 6.x (omit if not CIM-related)")
    p.add_argument("--release-notes", default="", help="Release notes shown on Splunkbase")
    p.add_argument("--hidden", action="store_true", help="Upload and validate, but leave the release hidden")
    p.add_argument("--dry-run", action="store_true", help="Listing checks and login only, upload nothing")
    p.add_argument("--package-id", default="", help="Resume an earlier upload: skip it, poll and publish")
    p.add_argument("--timeout", type=int, default=1800, help="Seconds to wait for validation (default 1800)")
    p.add_argument("--poll-interval", type=int, default=15, help="Seconds between validation polls")
    return p.parse_args(argv)


def main(argv=None):
    try:
        return run(parse_args(argv))
    except PublishError as e:
        print(f"ERROR: {e}", file=sys.stderr)
        if os.environ.get("GITHUB_ACTIONS") == "true":
            print(f"::error title=Splunkbase publish::{e}")
        return 1


if __name__ == "__main__":
    sys.exit(main())
