"""Manual release orchestration; run from the server checkout on the Actions runner."""

import base64
from dataclasses import dataclass
from datetime import datetime, timezone
import hashlib
import json
import os
from pathlib import Path
import re
import subprocess
import sys
import tarfile
from tempfile import TemporaryDirectory
from urllib.error import HTTPError, URLError
from urllib.parse import urlencode, urlsplit
from urllib.request import HTTPRedirectHandler, Request, build_opener


SERVER = "DelverLab/delverlab-server"
DEPLOY = "DelverLab/delverlab"
IMAGE = "ghcr.io/delverlab/delverlab-server"
SOURCE = f"https://github.com/{SERVER}"
MAIN = "refs/heads/main"
VERSION = r"(?:0|[1-9][0-9]*)\.(?:0|[1-9][0-9]*)\.(?:0|[1-9][0-9]*)"
SHA = r"[a-f0-9]{40}"
DIGEST = r"sha256:[a-f0-9]{64}"
BOOTSTRAP = "0.1.0"
CHANGELOG = Path("CHANGELOG.md")
BOT_NAME = "delverlab-release[bot]"
BOT_EMAIL = f"358323233+{BOT_NAME}@users.noreply.github.com"
# The first published image uses this commit. Keep its tag usable after the
# attribution cleanup removes the original commit from main's history.
ORIGINAL_FIRST_RELEASE = "d814081da61dc54298fb0ed10aa3e9186a31dd3a"


class ReleaseError(Exception):
    pass


def valid(pattern, value):
    return isinstance(value, str) and re.fullmatch(pattern, value) is not None


def version_key(version):
    if not valid(VERSION, version) or len(version) > 128:
        raise ReleaseError("Expected a stable Docker-compatible X.Y.Z version")
    return tuple(map(int, version.split(".")))


def latest_tag(reachable):
    stable = [tag for tag in reachable if valid("v" + VERSION, tag)]
    return max(stable, key=lambda tag: version_key(tag[1:]), default=None)


def release_revisions(reachable, tag_refs):
    tag = f"v{BOOTSTRAP}"
    if tag not in reachable and tag_refs.get(f"refs/tags/{tag}") == ORIGINAL_FIRST_RELEASE:
        return {**reachable, tag: ORIGINAL_FIRST_RELEASE}
    return reachable


def choose_version(bump, reachable, all_tags, base):
    if bump not in ("patch", "minor", "major"):
        raise ReleaseError("Expected patch, minor or major")
    previous = latest_tag(reachable)
    version = BOOTSTRAP
    if previous:
        if reachable[previous] == base:
            raise ReleaseError("No new commits since the last stable tag; use resume")
        major, minor, patch = version_key(previous[1:])
        parts = {"patch": (major, minor, patch + 1), "minor": (major, minor + 1, 0),
                 "major": (major + 1, 0, 0)}[bump]
        version = ".".join(map(str, parts))
    version_key(version)
    if "v" + version in all_tags:
        raise ReleaseError(f"Tag v{version} already exists, including tags outside main")
    return version, previous


def resume_version(tag, reachable):
    if not valid("v" + VERSION, tag) or tag not in reachable:
        raise ReleaseError("Resume requires an exact existing stable vX.Y.Z tag reachable from main")
    version_key(tag[1:])
    return tag[1:], reachable[tag]


def section(text, name):
    headings = list(re.finditer(r"^## [^\r\n]+", text, re.MULTILINE))
    prefix = f"## [{name}]"
    matches = [i for i, h in enumerate(headings)
               if h.group() == prefix or h.group().startswith(prefix + " ")]
    if len(matches) != 1:
        raise ReleaseError(f"Expected exactly one changelog section for {name}")
    i = matches[0]
    return headings[i], headings[i + 1].start() if i + 1 < len(headings) else len(text)


def close_changelog(text, version, day):
    version_key(version)
    heading, _ = section(text, "Unreleased")
    if heading.group() != "## [Unreleased]" or re.search(
        rf"^## \[{re.escape(version)}\](?: |\r?$)", text, re.MULTILINE
    ):
        raise ReleaseError("Unreleased heading is not canonical, or the version is already in the changelog")
    if re.search(r"^## \[[0-9]", text[:heading.start()], re.MULTILINE):
        raise ReleaseError("Unreleased must come before released versions")
    eol = "\r\n" if text[heading.end():].startswith("\r\n") else "\n"
    rest = text[heading.end():]
    return (text[:heading.end()] + eol * 2 + f"## [{version}] - {day.isoformat()}"
            + (rest if rest.startswith(eol) else eol + rest))


def changelog_notes(text, version):
    heading, end = section(text, version)
    if not valid(rf"## \[{re.escape(version)}\] - [0-9]{{4}}-[0-9]{{2}}-[0-9]{{2}}", heading.group()):
        raise ReleaseError("Tagged changelog section must have a release date")
    body = text[heading.end():end].replace("\r\n", "\n")
    return re.sub(r"\A(?:[ \t]*\n)+|(?:\n[ \t]*)+\Z", "", body)


def compose_image(text):
    # This is a surgical edit of the agreed layout, not a general YAML parser.
    # Compose itself checks the resulting service and image before any deploy push.
    services = list(re.finditer(r"^services:[ \t]*\r?$", text, re.MULTILINE))
    if len(services) != 1:
        raise ReleaseError("Expected one top-level services block in compose.yaml")
    tail = text[services[0].end():]
    boundary = re.search(r"^[^\s#]", tail, re.MULTILINE)
    block = tail[:boundary.start()] if boundary else tail
    servers = list(re.finditer(r"^  server:[ \t]*\r?$", block, re.MULTILINE))
    if len(servers) != 1:
        raise ReleaseError("Expected one server service in compose.yaml")
    tail = block[servers[0].end():]
    boundary = re.search(r"^  [^\s#]", tail, re.MULTILINE)
    block = tail[:boundary.start()] if boundary else tail
    entries = list(re.finditer(r"^    image:[^\r\n]*", block, re.MULTILINE))
    if len(entries) != 1:
        raise ReleaseError("Expected one server image line in compose.yaml")
    entry = entries[0]
    match = re.fullmatch(rf"    image: {re.escape(IMAGE)}:({VERSION})(?:@({DIGEST}))?", entry.group())
    if not match:
        raise ReleaseError("Unexpected server image reference in compose.yaml")
    start = services[0].end() + servers[0].end() + entry.start()
    return match[1], match[2], start, start + len(entry.group())


def update_compose(text, version, digest):
    old_version, old_digest, start, end = compose_image(text)
    if version_key(old_version) > version_key(version):
        raise ReleaseError("Deploy already uses a newer version; refusing stale update")
    if not valid(DIGEST, digest):
        raise ReleaseError("Invalid image digest")
    if old_version == version and old_digest and old_digest != digest:
        raise ReleaseError("Deploy pins a different digest for the same version")
    return f"{text[:start]}    image: {IMAGE}:{version}{text[end:]}"


def check_image(os_name, arch, labels, version, revision):
    if (os_name, arch) != ("linux", "amd64") or not isinstance(labels, dict) or (
        labels.get("org.opencontainers.image.revision") != revision
        or labels.get("org.opencontainers.image.version") != version
        or str(labels.get("io.github.delverlab.source")).lower() != SOURCE.lower()
    ):
        raise ReleaseError(f"Image {version} must be linux/amd64 with matching OCI source, version and revision")


def publish_image(lookup, push, *, resume, image_id):
    existing = lookup()
    if existing:
        if not resume:
            raise ReleaseError("Registry tag already exists; refusing to overwrite it")
        if not image_id or existing[1] != image_id:
            raise ReleaseError("Registry image does not match the tested image recorded for this release")
        return existing[0]
    if not image_id:
        raise ReleaseError("Registry tag is missing without a tested image; dispatch resume again")
    push()
    observed = lookup()
    if not observed or observed[1] != image_id:
        raise ReleaseError("Registry image config does not match the tested image after push")
    return observed[0]


def check_remote(refs, expected_main, expected_tags):
    if refs.get(MAIN) != expected_main:
        raise ReleaseError("Server main moved; dispatch again from its current tip (use resume if tagged)")
    if stable_refs(refs) != expected_tags:
        raise ReleaseError("Stable tags changed; dispatch again from the current main")


def release_body(notes, version, revision, digest, image_id, *, legacy=False):
    image = f"{IMAGE}:{version}"
    image_line = f"Image: `{image}@{digest}`" if legacy else f"Image: `{image}`"
    provenance = f"{image_line}\nSource commit: `{revision}`"
    if not legacy:
        provenance += f"\n\n<!-- delverlab-manifest {digest} -->"
    return f"{notes}\n\n{provenance}\n\n{evidence(revision, image_id)}".lstrip("\n")


def check_image_release(record, notes, version, revision, digest, image_id):
    body = release_body(notes, version, revision, digest, image_id)
    legacy_body = release_body(notes, version, revision, digest, image_id, legacy=True)
    check_release(record, f"v{version}", legacy_body if record and record.get("body") == legacy_body else body)


def evidence(revision, image_id):
    if not valid(SHA, revision) or not valid(DIGEST, image_id):
        raise ReleaseError("Invalid tested image evidence")
    return f"<!-- delverlab-tested-image {revision} {image_id} -->"


def release_matches_tag(record, tag):
    return record.get("tag_name") == tag or (
        record.get("draft") is True and record.get("name") == tag
        and valid(r"untagged-[a-f0-9]+", record.get("tag_name"))
    )


def recorded_image(record, tag, revision):
    if not record or not release_matches_tag(record, tag) or record.get("prerelease") is not False:
        raise ReleaseError("Missing tested image record for this release")
    if record.get("tag_name") != tag and record.get("target_commitish") != revision:
        raise ReleaseError("Draft release does not target the expected commit")
    text = record.get("body") or ""
    matches = re.findall(rf"^<!-- delverlab-tested-image ({SHA}) ({DIGEST}) -->$", text, re.MULTILINE)
    if len(matches) != 1 or matches[0][0] != revision:
        raise ReleaseError("Missing or invalid tested image record for the release commit")
    if record.get("draft") is True and text != evidence(revision, matches[0][1]):
        raise ReleaseError("The draft is not a workflow-owned release record")
    return matches[0][1]


def check_release(record, tag, body):
    if (not record or record.get("tag_name") != tag or record.get("body") != body
            or record.get("draft") is not False or record.get("prerelease") is not False):
        raise ReleaseError(f"GitHub Release {tag} is missing, unpublished, or does not match notes and provenance")


def child_env(**extra):
    # Do not pass credentials or debug tracing to Compose, Docker or ordinary Git commands.
    return {k: v for k, v in os.environ.items() if k in ("PATH", "HOME", "LANG", "TMPDIR", "RUNNER_TEMP")} | extra


def command(*args, cwd=None, env=None, data=None, allow_failure=False):
    result = subprocess.run(args, cwd=cwd, env=env if env is not None else child_env(),
                            input=data, capture_output=True, check=False)
    if result.returncode and not allow_failure:
        # Never print subprocess stderr (it may contain credentials or resolved Compose values).
        raise ReleaseError(f"{args[0]} {args[1]} failed (exit {result.returncode})")
    return result


def git(*args, **kwargs):
    return command("git", *args, **kwargs).stdout.decode("utf-8")


def basic(user, token):
    return "Basic " + base64.b64encode(f"{user}:{token}".encode()).decode("ascii")


def git_auth(repo, token):
    if not token:
        raise ReleaseError("Missing Git credential")
    # Config exists only in this subprocess and its Git transport; never in command args or repo config.
    return child_env(GIT_TERMINAL_PROMPT="0", GIT_CONFIG_COUNT="2",
                     GIT_CONFIG_KEY_0=f"http.https://github.com/{repo}.git.extraheader",
                     GIT_CONFIG_VALUE_0=f"Authorization: {basic('x-access-token', token)}",
                     GIT_CONFIG_KEY_1="http.followRedirects", GIT_CONFIG_VALUE_1="false")


def remote_refs(repo, token=None, *patterns):
    result = git("ls-remote", "--refs", f"https://github.com/{repo}.git", *patterns,
                 env=git_auth(repo, token) if token else child_env(GIT_TERMINAL_PROMPT="0"))
    return dict((ref, sha) for sha, ref in (line.split("\t") for line in result.splitlines()))


def stable_refs(refs):
    return {ref: sha for ref, sha in refs.items() if valid(r"refs/tags/v" + VERSION, ref)}


def tag_snapshot(tags):
    return hashlib.sha256(json.dumps(tags, sort_keys=True).encode()).hexdigest()


def server_refs(token):
    return remote_refs(SERVER, token, MAIN, "refs/tags/v*")


def tagged_notes(tag, revision):
    subject = git("show", "-s", "--format=%s", revision).strip()
    if subject != f"chore(release): {tag}":
        raise ReleaseError(f"Tag {tag} does not point to a release commit")
    return changelog_notes(git("show", f"{revision}:CHANGELOG.md"), tag[1:])


@dataclass(frozen=True)
class Plan:
    base: str
    revision: str
    version: str
    resume: bool
    tags: dict
    reachable: dict
    previous: str | None
    notes: str

    @property
    def ref(self):
        return f"refs/tags/v{self.version}"


def make_plan(read_token, timestamp):
    base = os.environ["GITHUB_SHA"]
    if not valid(SHA, base) or git("rev-parse", "HEAD").strip() != base:
        raise ReleaseError("Checkout does not match dispatch SHA")
    local_tags = stable_refs(dict(line.split(" ") for line in git(
        "for-each-ref", "--format=%(refname) %(objectname)", "refs/tags").splitlines()))
    check_remote(server_refs(read_token), base, local_tags)
    reachable = {tag: git("rev-parse", f"refs/tags/{tag}^{{commit}}").strip()
                 for tag in git("tag", "--merged", base, "--list", "v*").splitlines()
                 if valid("v" + VERSION, tag)}
    reachable = release_revisions(reachable, local_tags)
    resume_tag = os.environ["RESUME_TAG"]
    if resume_tag:
        version, revision = resume_version(resume_tag, reachable)
        return Plan(base, revision, version, True, local_tags, reachable, None,
                    tagged_notes(resume_tag, revision))

    version, previous = choose_version(os.environ["BUMP"], reachable,
                                      {ref.removeprefix("refs/tags/") for ref in local_tags}, base)
    day = datetime.strptime(timestamp, "%Y-%m-%dT%H:%M:%SZ").date()
    updated = close_changelog(git("show", f"{base}:CHANGELOG.md"), version, day)
    # Both runners construct the same single-parent commit. No bundle or checkout of transferred code.
    CHANGELOG.write_bytes(updated.encode("utf-8"))
    git("add", "--", str(CHANGELOG))
    tree = git("write-tree").strip()
    revision = git("-c", "commit.gpgsign=false", "commit-tree", tree, "-p", base,
                   "-m", f"chore(release): v{version}", env=child_env(
                       GIT_AUTHOR_NAME=BOT_NAME, GIT_COMMITTER_NAME=BOT_NAME,
                       GIT_AUTHOR_EMAIL=BOT_EMAIL, GIT_COMMITTER_EMAIL=BOT_EMAIL,
                       GIT_AUTHOR_DATE=timestamp, GIT_COMMITTER_DATE=timestamp)).strip()
    return Plan(base, revision, version, False, local_tags, reachable, previous,
                changelog_notes(updated, version))


class SafeRedirect(HTTPRedirectHandler):
    def __init__(self, allow):
        self.allow = allow

    def redirect_request(self, req, fp, code, msg, headers, newurl):
        if not self.allow or urlsplit(newurl).scheme != "https":
            return None
        redirected = super().redirect_request(req, fp, code, msg, headers, newurl)
        if redirected:
            redirected.remove_header("Authorization")
        return redirected


def http(url, headers, *, payload=None, redirects=False, method=None):
    data = None if payload is None else json.dumps(payload).encode()
    req = Request(url, data=data, headers=headers, method=method)
    try:
        with build_opener(SafeRedirect(redirects)).open(req, timeout=30) as response:
            return response.status, response.headers, response.read()
    except HTTPError as error:
        with error:
            return error.code, error.headers, error.read()
    except (URLError, TimeoutError) as error:
        raise ReleaseError(f"Request to {urlsplit(url).hostname} failed") from error


def content_digest(data):
    return "sha256:" + hashlib.sha256(data).hexdigest()


class Registry:
    BASE = "https://ghcr.io"
    REPO = "/".join(IMAGE.split("/")[1:])
    ACCEPT = ", ".join(("application/vnd.oci.image.manifest.v1+json",
                        "application/vnd.docker.distribution.manifest.v2+json",
                        "application/vnd.oci.image.index.v1+json",
                        "application/vnd.docker.distribution.manifest.list.v2+json"))

    def __init__(self, token=None, *, write=False, probe=False):
        self.headers = None
        scope = "pull,push" if write else "pull"
        headers = {"Authorization": basic(os.environ["REGISTRY_USERNAME"], token)} if token else {}
        query = urlencode({"service": "ghcr.io", "scope": f"repository:{self.REPO}:{scope}"})
        status, _, raw = http(f"{self.BASE}/token?{query}", headers)
        # An anonymous probe cannot see private images. Build if unsure,
        # but only an authoritative authenticated manifest 404 can ever permit a push.
        if probe and status in (401, 403, 404):
            return
        if status != 200 or not (bearer := json.loads(raw).get("token")):
            raise ReleaseError(f"GHCR token exchange failed (HTTP {status})")
        self.headers = {"Authorization": f"Bearer {bearer}", "Accept": self.ACCEPT}

    def lookup(self, version, revision, *, probe=False):
        if self.headers is None and probe:
            return None
        status, headers, raw = http(f"{self.BASE}/v2/{self.REPO}/manifests/{version}", self.headers)
        if probe and status in (401, 403):
            return None
        if status == 404 and any(e.get("code") in ("MANIFEST_UNKNOWN", "NAME_UNKNOWN")
                                 for e in json.loads(raw).get("errors", [])):
            return None
        if status != 200:
            raise ReleaseError(f"GHCR manifest check failed (HTTP {status}); no push attempted")
        digest = content_digest(raw)
        if headers.get("Docker-Content-Digest") != digest:
            raise ReleaseError("GHCR reported an invalid manifest digest")
        manifest = json.loads(raw)
        config_digest = manifest.get("config", {}).get("digest")
        if manifest.get("schemaVersion") != 2 or not valid(DIGEST, config_digest):
            raise ReleaseError("Expected a single-platform GHCR image manifest")
        status, _, raw = http(f"{self.BASE}/v2/{self.REPO}/blobs/{config_digest}", self.headers, redirects=True)
        if status != 200 or content_digest(raw) != config_digest:
            raise ReleaseError("Could not verify GHCR image config by digest")
        config = json.loads(raw)
        check_image(config.get("os"), config.get("architecture"), config.get("config", {}).get("Labels"),
                    version, revision)
        return digest, config_digest


class Releases:
    URL = f"https://api.github.com/repos/{SERVER}/releases"

    def __init__(self, token):
        self.headers = {"Authorization": f"Bearer {token}", "Accept": "application/vnd.github+json",
                        "Content-Type": "application/json", "X-GitHub-Api-Version": "2022-11-28"}

    def find(self, tag):
        # List using the App so we also detect draft collisions before touching Git or GHCR.
        page = 1
        match = None
        while True:
            status, headers, raw = http(f"{self.URL}?per_page=100&page={page}", self.headers)
            if status != 200:
                raise ReleaseError(f"Could not list GitHub Releases (HTTP {status})")
            for record in json.loads(raw):
                if release_matches_tag(record, tag):
                    if match:
                        raise ReleaseError(f"Multiple GitHub Releases for {tag}")
                    match = record
            if 'rel="next"' not in headers.get("Link", ""):
                return match
            page += 1

    def create_draft(self, tag, revision, image_id):
        status, _, raw = http(self.URL, self.headers, payload={
            "tag_name": tag, "target_commitish": revision, "name": tag,
            "body": evidence(revision, image_id), "draft": True, "prerelease": False,
        })
        if status != 201:
            raise ReleaseError(f"GitHub Release draft creation failed (HTTP {status}); use resume")
        return json.loads(raw)

    def get(self, release_id, tag):
        if not re.fullmatch(r"[1-9][0-9]{0,19}", str(release_id)):
            raise ReleaseError("Expected a numeric GitHub Release ID")
        status, _, raw = http(f"{self.URL}/{release_id}", self.headers)
        if status != 200:
            raise ReleaseError(f"Could not read GitHub Release by ID (HTTP {status})")
        record = json.loads(raw)
        if not release_matches_tag(record, tag):
            raise ReleaseError("GitHub Release ID does not match the requested tag")
        return record

    def edit(self, record, **fields):
        release_id = record.get("id")
        if not isinstance(release_id, int) or release_id <= 0 or record.get("draft") is not True:
            raise ReleaseError("Expected a GitHub Release draft")
        status, _, raw = http(f"{self.URL}/{release_id}", self.headers, payload=fields, method="PATCH")
        if status != 200:
            raise ReleaseError(f"GitHub Release draft update failed (HTTP {status}); use resume")
        return json.loads(raw)

    def finalize(self, record, tag, body, latest):
        updated = self.edit(record, tag_name=tag, body=body, draft=False, make_latest="true" if latest else "false")
        check_release(updated, tag, body)


def saved_config_digest(path, version, revision):
    # Read the actual config blob so it can be compared to GHCR after the push.
    with tarfile.open(path) as archive:
        def read(name):
            entry = archive.getmember(name)
            if not entry.isfile() or entry.size > 4 * 1024 * 1024:
                raise ReleaseError("Invalid Docker archive metadata")
            file = archive.extractfile(entry)
            if file is None:
                raise ReleaseError("Missing Docker archive metadata")
            with file:
                return file.read()

        entries = json.loads(read("manifest.json"))
        if len(entries) != 1 or entries[0].get("RepoTags") != [f"{IMAGE}:{version}"]:
            raise ReleaseError("Docker archive must contain only the tested release image")
        raw = read(entries[0]["Config"])
    config = json.loads(raw)
    check_image(config.get("os"), config.get("architecture"), config.get("config", {}).get("Labels"),
                version, revision)
    return content_digest(raw)


def workspace():
    return Path(os.environ["RUNNER_TEMP"])


def docker_env(directory):
    # A nonempty auth map prevents Docker from auto-detecting an external credential store.
    (directory / "config.json").write_text('{"auths":{"ghcr.io":{}}}', encoding="utf-8")
    return child_env(DOCKER_CONFIG=str(directory))


def output(**values):
    with open(os.environ["GITHUB_OUTPUT"], "a", encoding="utf-8") as file:
        for key, value in values.items():
            print(f"{key}={value}", file=file)


def prepare():
    timestamp = datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
    read_token = os.environ["READ_TOKEN"]
    plan = make_plan(read_token, timestamp)
    existing = plan.resume and Registry(probe=True).lookup(plan.version, plan.revision, probe=True)
    if not existing:
        git("checkout", "--detach", plan.revision)
    output(version=plan.version, revision=plan.revision, timestamp=timestamp, tags=tag_snapshot(plan.tags),
           build="false" if existing else "true")


def save():
    version = os.environ["VERSION"]
    revision = os.environ["REVISION"]
    if git("rev-parse", "HEAD").strip() != revision:
        raise ReleaseError("Build did not leave the candidate checked out")
    ref = f"{IMAGE}:{version}"
    path = workspace() / "candidate.tar"
    command("docker", "save", "--platform", "linux/amd64", "-o", str(path), ref)
    output(image_id=saved_config_digest(path, version, revision))


def clone_deploy():
    directory = workspace() / "release-deploy"
    git("clone", "--depth=1", "--branch", "main", f"https://github.com/{DEPLOY}.git", str(directory))
    return directory


def read_compose(directory):
    path = directory / "compose.yaml"
    if path.is_symlink() or not path.is_file():
        raise ReleaseError("compose.yaml must be a regular file")
    return path.read_bytes().decode("utf-8")


def check_previous(plan, registry, releases, compose):
    if not plan.previous:
        version, digest, _, _ = compose_image(compose)
        if version != BOOTSTRAP or digest:
            raise ReleaseError("First release requires the unpinned 0.1.0 bootstrap Compose")
        return
    tag = plan.previous
    version = tag[1:]
    revision = plan.reachable[tag]
    try:
        observed = registry.lookup(version, revision)
        current_version, current_digest, _, _ = compose_image(compose)
        if not observed or current_version != version or (current_digest and current_digest != observed[0]):
            raise ReleaseError("Prior image is missing or not configured in deploy")
        record = releases.find(tag)
        if recorded_image(record, tag, revision) != observed[1]:
            raise ReleaseError("Prior image does not match its tested image record")
        check_image_release(record, tagged_notes(tag, revision), version, revision, *observed)
        if Registry().lookup(version, revision) != observed:
            raise ReleaseError("Prior image is not publicly available at the verified digest")
    except ReleaseError as error:
        raise ReleaseError(f"Previous release {tag} is incomplete; resume it before bumping: {error}") from error


def publish():
    server_token = os.environ["SERVER_APP_TOKEN"]
    registry_token = os.environ["REGISTRY_TOKEN"]
    if not registry_token or not re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9-]{0,38}", os.environ["REGISTRY_USERNAME"]):
        raise ReleaseError("Set RELEASE_REGISTRY_TOKEN and RELEASE_REGISTRY_USERNAME in the release environment")
    plan = make_plan(server_token, os.environ["CANDIDATE_TIMESTAMP"])
    if (plan.version != os.environ["VERSION"] or plan.revision != os.environ["REVISION"]
            or tag_snapshot(plan.tags) != os.environ["CANDIDATE_TAGS"]):
        raise ReleaseError("Candidate or stable tags changed since build; dispatch again")

    image_id = os.environ["TESTED_IMAGE_ID"] or None
    ref = f"{IMAGE}:{plan.version}"
    if image_id:
        path = workspace() / "release-candidate" / "candidate.tar"
        if saved_config_digest(path, plan.version, plan.revision) != image_id:
            raise ReleaseError("Downloaded artifact does not match the image tested in the build job")
        command("docker", "load", "-i", str(path))

    tag = f"v{plan.version}"
    registry = Registry(registry_token, write=True)
    releases = Releases(server_token)
    compose = read_compose(clone_deploy())
    old_version, old_digest, _, _ = compose_image(compose)
    if version_key(old_version) > version_key(plan.version):
        raise ReleaseError("Deploy already uses a newer version; refusing stale release")
    existing = registry.lookup(plan.version, plan.revision)
    resume_id = os.environ.get("RESUME_RELEASE_ID", "")
    if resume_id and not plan.resume:
        raise ReleaseError("A release ID can only be given when resuming a tag")
    record = releases.get(resume_id, tag) if resume_id else releases.find(tag)
    if plan.resume:
        if not existing and ((record is not None and record.get("draft") is not True)
                             or (old_version == plan.version and old_digest)):
            raise ReleaseError("Published version is missing from GHCR; refusing to replace its image")
        if existing:
            recorded = recorded_image(record, tag, plan.revision)
            if recorded != existing[1] or record is None:
                raise ReleaseError("Registry image conflicts with the tested image record")
            # Anonymous preflight may have rebuilt for a private tag; trust the recorded digest.
            image_id = recorded
            update_compose(compose, plan.version, existing[0])
            if record.get("draft") is not True:
                check_image_release(record, plan.notes, plan.version, plan.revision, existing[0], image_id)
    else:
        check_previous(plan, registry, releases, compose)
        if existing or record is not None or not image_id:
            raise ReleaseError("New version requires a tested image and unused registry tag and GitHub Release")

    expected_main = plan.base
    tags = plan.tags.copy()

    def guard():
        check_remote(server_refs(server_token), expected_main, tags)

    if not plan.resume:
        guard()
        git("push", "--atomic", f"https://github.com/{SERVER}.git", f"{plan.revision}:{MAIN}",
            f"{plan.revision}:{plan.ref}", env=git_auth(SERVER, server_token))
        expected_main = plan.revision
        tags[plan.ref] = plan.revision
    guard()

    if not existing:
        if not image_id:
            raise ReleaseError("Missing tested image; dispatch resume again")
        if record is None:
            record = releases.create_draft(tag, plan.revision, image_id)
        elif recorded_image(record, tag, plan.revision) != image_id:
            # No published image exists yet; this is a newly tested rebuild of the tagged commit.
            record = releases.edit(record, body=evidence(plan.revision, image_id))
    if recorded_image(record, tag, plan.revision) != image_id:
        raise ReleaseError("GitHub did not record the tested image")

    def push():
        guard()
        with TemporaryDirectory(dir=workspace()) as config:
            env = docker_env(Path(config))
            command("docker", "login", "ghcr.io", "-u", os.environ["REGISTRY_USERNAME"], "--password-stdin",
                    env=env, data=registry_token.encode())
            # Keep stable GHCR tags single-writer: the registry push has no compare-and-swap.
            if registry.lookup(plan.version, plan.revision):
                raise ReleaseError("Registry tag appeared during publication; refusing to overwrite it")
            guard()
            command("docker", "push", "--platform", "linux/amd64", ref, env=env)

    def lookup():
        observed = registry.lookup(plan.version, plan.revision)
        if existing and observed != existing:
            raise ReleaseError("Existing version changed or disappeared during resume")
        return observed

    digest = publish_image(lookup, push, resume=plan.resume, image_id=image_id)
    body = release_body(plan.notes, plan.version, plan.revision, digest, image_id)
    guard()
    current_record = releases.get(record["id"], tag) if record and record.get("id") else None
    if current_record is None or recorded_image(current_record, tag, plan.revision) != image_id:
        raise ReleaseError("Tested image record changed during publication")
    if current_record.get("draft") is True:
        guard()
        releases.finalize(current_record, tag, body, not plan.resume or tag == latest_tag(plan.reachable))
    else:
        check_image_release(current_record, plan.notes, plan.version, plan.revision, digest, image_id)
    output(digest=digest, main=expected_main, tags=tag_snapshot(tags))
    print(f"Verified {tag} at {plan.revision}: {ref} (manifest {digest})")


def compose_config(directory, *args):
    return command("docker", "compose", "-f", "compose.yaml", "config", *args,
                   cwd=directory, env=child_env(DELVERLAB_DB_PASSWORD="test")).stdout


def deploy():
    version = os.environ["VERSION"]
    revision = os.environ["REVISION"]
    digest = os.environ["PUBLISHED_DIGEST"]
    version_key(version)
    if not valid(DIGEST, digest):
        raise ReleaseError("Invalid published image digest")
    image = f"{IMAGE}:{version}"
    server_token = os.environ["SERVER_APP_TOKEN"]
    deploy_token = os.environ["DEPLOY_APP_TOKEN"]
    directory = workspace() / "release-deploy"

    def guard():
        refs = server_refs(server_token)
        if (refs.get(MAIN) != os.environ["PUBLISHED_MAIN"]
                or tag_snapshot(stable_refs(refs)) != os.environ["PUBLISHED_TAGS"]):
            raise ReleaseError("Server main or stable tags changed; dispatch resume from current main")

    def check_public_tag():
        try:
            observed = Registry().lookup(version, revision)
        except ReleaseError as error:
            raise ReleaseError(f"Anonymous GHCR lookup failed; verify visibility and resume v{version}: {error}") from error
        if not observed or observed[0] != digest:
            raise ReleaseError("Anonymous tag lookup did not resolve to the expected digest; verify visibility and resume")

    guard()
    check_public_tag()
    with TemporaryDirectory(dir=workspace()) as config:
        result = command("docker", "pull", "--platform", "linux/amd64", image,
                         env=docker_env(Path(config)), allow_failure=True)
    if result.returncode:
        raise ReleaseError("Anonymous GHCR pull failed; verify availability/visibility and resume this tag")
    for _ in range(3):
        git("fetch", "--no-tags", "--depth=1", "origin", MAIN, cwd=directory)
        tip = git("rev-parse", "FETCH_HEAD", cwd=directory).strip()
        git("checkout", "--detach", tip, cwd=directory)
        original = read_compose(directory)
        updated = update_compose(original, version, digest)
        old_version, old_digest, _, _ = compose_image(original)
        old_image = f"{IMAGE}:{old_version}" + (f"@{old_digest}" if old_digest else "")
        before = json.loads(compose_config(directory, "--format", "json"))
        if before.get("services", {}).get("server", {}).get("image") != old_image:
            raise ReleaseError("Compose's effective server image does not match the tracked image line")
        if original != updated:
            (directory / "compose.yaml").write_bytes(updated.encode("utf-8"))
        compose_config(directory, "-q")
        before["services"]["server"]["image"] = image
        if json.loads(compose_config(directory, "--format", "json")) != before:
            raise ReleaseError("Compose resolves to changes beyond the server image")
        check_public_tag()
        guard()
        if original == updated:
            if remote_refs(DEPLOY, None, MAIN).get(MAIN) == tip:
                print(f"Deploy main already uses {image}")
                return
            continue
        git("-c", f"user.name={BOT_NAME}", "-c", f"user.email={BOT_EMAIL}",
            "-c", "commit.gpgsign=false", "commit", "-m", f"Deploy server v{version}", "--", "compose.yaml",
            cwd=directory)
        guard()
        result = command("git", "push", f"https://github.com/{DEPLOY}.git", f"HEAD:{MAIN}",
                         cwd=directory, env=git_auth(DEPLOY, deploy_token), allow_failure=True)
        remote_tip = remote_refs(DEPLOY, None, MAIN).get(MAIN)
        if remote_tip == git("rev-parse", "HEAD", cwd=directory).strip():
            print(f"Deploy main uses {image}")
            return
        if remote_tip is None or remote_tip == tip:
            raise ReleaseError(f"Deploy push did not land (exit {result.returncode}); check permissions and resume")
        # Only retry on a changed remote tip. Re-parse the file and reject stale/mismatched pins again.
    raise ReleaseError("Deploy main kept advancing; dispatch resume again")


if __name__ == "__main__":
    try:
        {"prepare": prepare, "save": save, "publish": publish, "deploy": deploy}[sys.argv[1]]()
    except ReleaseError as error:
        sys.exit(str(error))
    except (KeyError, TypeError, ValueError, OSError, IndexError, AttributeError, tarfile.TarError) as error:
        # Exception values may contain HTTP data, environment values, or Compose secrets.
        sys.exit(f"Release orchestration failed ({type(error).__name__})")
