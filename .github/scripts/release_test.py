import io
import json
import os
from pathlib import Path
from subprocess import CompletedProcess
import tarfile
from tempfile import TemporaryDirectory
import unittest
from datetime import date
from unittest.mock import Mock, patch

import release


DIGEST = "sha256:" + "a" * 64
OTHER_DIGEST = "sha256:" + "b" * 64
SHA = "a" * 40
PARENT = "b" * 40
IMAGE = "ghcr.io/delverlab/delverlab-server"
NOTES = "### Added\n\n- First release."
CHANGELOG = f"# Changelog\n\n## [Unreleased]\n\n{NOTES}\n\n## [0.0.1] - 2026-01-01\n\nOld notes.\n"
COMPOSE = f"services:\n  db:\n    image: postgres:18\n  server:\n    image: {IMAGE}:0.1.0\n    restart: unless-stopped\n"


class VersionTests(unittest.TestCase):
    def test_retains_only_the_original_detached_first_release_for_provenance(self):
        old = "d814081da61dc54298fb0ed10aa3e9186a31dd3a"
        self.assertEqual(release.release_revisions({}, {"refs/tags/v0.1.0": old, "refs/tags/v9.0.0": SHA}),
                         {"v0.1.0": old})
        self.assertEqual(release.release_revisions({}, {"refs/tags/v0.1.0": SHA}), {})
        self.assertEqual(release.release_revisions({"v0.2.0": SHA}, {"refs/tags/v0.1.0": old}),
                         {"v0.1.0": old, "v0.2.0": SHA})

    def test_first_release_never_depends_on_bump(self):
        for bump in ("patch", "minor", "major"):
            self.assertEqual(release.choose_version(bump, {}, set(), SHA), ("0.1.0", None))

    def test_uses_highest_numeric_reachable_stable_tag(self):
        reachable = {"v1.10.0": PARENT, "v1.9.9": PARENT, "v2.0.0-rc.1": PARENT}
        all_tags = set(reachable) | {"v4.0.0"}
        for bump, version in [("patch", "1.10.1"), ("minor", "1.11.0"), ("major", "2.0.0")]:
            self.assertEqual(release.choose_version(bump, reachable, all_tags, SHA),
                             (version, "v1.10.0"))

    def test_refuses_a_release_without_new_commits(self):
        with self.assertRaisesRegex(release.ReleaseError, "No new commits"):
            release.choose_version("patch", {"v1.0.0": SHA}, {"v1.0.0"}, SHA)

    def test_refuses_a_duplicate_even_if_tag_is_unreachable(self):
        with self.assertRaisesRegex(release.ReleaseError, "already exists"):
            release.choose_version("patch", {}, {"v0.1.0"}, SHA)

    def test_resume_accepts_only_an_exact_reachable_stable_tag(self):
        reachable = {"v0.1.0": SHA, "v0.2.0-rc.1": SHA}
        self.assertEqual(release.resume_version("v0.1.0", reachable), ("0.1.0", SHA))
        for invalid in ("0.1.0", "v00.1.0", "v0.2.0-rc.1", "v0.2.0", " main", "v0.1.0\n"):
            with self.subTest(invalid=invalid), self.assertRaises(release.ReleaseError):
                release.resume_version(invalid, reachable)


class ChangelogTests(unittest.TestCase):
    def test_moves_unreleased_notes_without_changing_older_sections(self):
        updated = release.close_changelog(CHANGELOG, "0.1.0", date(2026, 11, 18))
        self.assertEqual(updated, CHANGELOG.replace("## [Unreleased]\n", "## [Unreleased]\n\n## [0.1.0] - 2026-11-18\n"))
        self.assertEqual(release.changelog_notes(updated, "0.1.0"), NOTES)

    def test_can_close_empty_unreleased(self):
        updated = release.close_changelog("# Changelog\n\n## [Unreleased]\n", "0.1.0", date(2026, 11, 18))
        self.assertEqual(release.changelog_notes(updated, "0.1.0"), "")

    def test_rejects_missing_duplicate_or_already_released_heading(self):
        for invalid in ("# Changelog\n", CHANGELOG + "\n## [Unreleased]\n", CHANGELOG + "\n## [0.1.0] - 2026-01-01\n"):
            with self.subTest(invalid=invalid), self.assertRaises(release.ReleaseError):
                release.close_changelog(invalid, "0.1.0", date(2026, 11, 18))


class ComposeTests(unittest.TestCase):
    def test_bootstrap_keeps_same_version_and_is_idempotent(self):
        updated = release.update_compose(COMPOSE, "0.1.0", DIGEST)
        self.assertEqual(updated, COMPOSE)
        self.assertEqual(release.update_compose(updated, "0.1.0", DIGEST), updated)

    def test_preserves_other_services_and_line_endings(self):
        pinned = COMPOSE.replace("0.1.0", f"1.9.0@{DIGEST}")
        content = (pinned + f"  mirror:\n    image: {IMAGE}:0.1.0\n").replace("\n", "\r\n")
        updated = release.update_compose(content, "1.10.0", OTHER_DIGEST)
        self.assertEqual(updated, content.replace(f"1.9.0@{DIGEST}", "1.10.0"))

    def test_reads_tag_only_versions_after_bootstrap(self):
        current = COMPOSE.replace("0.1.0", "0.2.0")
        self.assertEqual(release.update_compose(current, "0.3.0", DIGEST), current.replace("0.2.0", "0.3.0"))

    def test_refuses_newer_version_or_mismatched_same_version(self):
        pinned = COMPOSE.replace("0.1.0", f"0.1.0@{DIGEST}")
        for version in ("0.0.9", "0.1.0"):
            with self.subTest(version=version), self.assertRaises(release.ReleaseError):
                release.update_compose(pinned, version, OTHER_DIGEST)

    def test_refuses_unrecognized_layout_or_non_versioned_image(self):
        for invalid in (
            COMPOSE.replace("  server:", "  another:"),
            COMPOSE.replace("0.1.0", "latest"),
            COMPOSE + f"    image: {IMAGE}:0.1.0\n",
            COMPOSE + "  server:\n    image: something-else\n",
            COMPOSE.replace(f"image: {IMAGE}:0.1.0", "image: ${SERVER_IMAGE}"),
        ):
            with self.subTest(invalid=invalid), self.assertRaises(release.ReleaseError):
                release.update_compose(invalid, "1.0.0", DIGEST)


class PublicationTests(unittest.TestCase):
    def test_release_notes_display_tag_only_but_record_manifest_digest(self):
        body = release.release_body(NOTES, "0.1.0", SHA, DIGEST, OTHER_DIGEST)
        self.assertIn(f"Image: `{IMAGE}:0.1.0`", body)
        self.assertNotIn(f"{IMAGE}:0.1.0@", body)
        self.assertIn(DIGEST, body)

    def test_release_verification_accepts_legacy_notes_but_not_a_changed_digest(self):
        for legacy in (False, True):
            record = {"tag_name": "v0.1.0", "draft": False, "prerelease": False,
                      "body": release.release_body(NOTES, "0.1.0", SHA, DIGEST, OTHER_DIGEST, legacy=legacy)}
            release.check_image_release(record, NOTES, "0.1.0", SHA, DIGEST, OTHER_DIGEST)
            with self.assertRaises(release.ReleaseError):
                release.check_image_release(record, NOTES, "0.1.0", SHA, OTHER_DIGEST, OTHER_DIGEST)

    def test_validates_provenance_and_platform(self):
        labels = {"org.opencontainers.image.revision": SHA,
                  "org.opencontainers.image.version": "0.1.0",
                  "io.github.delverlab.source": "https://github.com/DelverLab/delverlab-server"}
        release.check_image("linux", "amd64", labels, "0.1.0", SHA)
        for key in labels:
            with self.subTest(key=key), self.assertRaises(release.ReleaseError):
                release.check_image("linux", "amd64", labels | {key: PARENT}, "0.1.0", SHA)
        with self.assertRaises(release.ReleaseError):
            release.check_image("linux", "arm64", labels, "0.1.0", SHA)

    def test_resume_never_pushes_over_an_existing_version(self):
        lookup, push = Mock(return_value=(DIGEST, OTHER_DIGEST)), Mock()
        self.assertEqual(release.publish_image(lookup, push, resume=True, image_id=OTHER_DIGEST), DIGEST)
        push.assert_not_called()

    def test_resume_refuses_an_existing_image_without_matching_test_evidence(self):
        for image_id in (None, DIGEST):
            with self.subTest(image_id=image_id):
                push = Mock()
                with self.assertRaisesRegex(release.ReleaseError, "tested image"):
                    release.publish_image(Mock(return_value=(DIGEST, OTHER_DIGEST)), push,
                                          resume=True, image_id=image_id)
                push.assert_not_called()

    def test_bump_refuses_existing_registry_tag_without_pushing(self):
        push = Mock()
        with self.assertRaises(release.ReleaseError):
            release.publish_image(Mock(return_value=(DIGEST, OTHER_DIGEST)), push,
                                  resume=False, image_id=OTHER_DIGEST)
        push.assert_not_called()

    def test_push_requires_tested_image_and_checks_the_registry_config_digest(self):
        push = Mock()
        self.assertEqual(release.publish_image(Mock(side_effect=[None, (DIGEST, OTHER_DIGEST)]), push,
                                               resume=True, image_id=OTHER_DIGEST), DIGEST)
        push.assert_called_once_with()
        with self.assertRaises(release.ReleaseError):
            release.publish_image(Mock(side_effect=[None, (DIGEST, DIGEST)]), Mock(),
                                  resume=False, image_id=OTHER_DIGEST)
        with self.assertRaises(release.ReleaseError):
            release.publish_image(Mock(return_value=None), Mock(), resume=True, image_id=None)

    def test_stale_main_refuses_before_tag_validation(self):
        with self.assertRaisesRegex(release.ReleaseError, "main moved"):
            release.check_remote({"refs/heads/main": PARENT}, SHA, {})

    def test_changed_or_added_stable_tag_invalidates_snapshot(self):
        with self.assertRaisesRegex(release.ReleaseError, "Stable tags changed"):
            release.check_remote({"refs/heads/main": SHA, "refs/tags/v0.1.0": PARENT}, SHA, {})

    def test_release_record_must_be_published_for_tag_and_match_notes_and_digest(self):
        body = release.release_body(NOTES, "0.1.0", SHA, DIGEST, OTHER_DIGEST)
        record = {"tag_name": "v0.1.0", "draft": False, "prerelease": False, "body": body}
        release.check_release(record, "v0.1.0", body)
        for invalid in (None, record | {"draft": True}, record | {"body": body.replace(DIGEST, OTHER_DIGEST)}):
            with self.assertRaises(release.ReleaseError):
                release.check_release(invalid, "v0.1.0", body)

    def test_release_notes_preserve_indented_code(self):
        text = "## [0.1.0] - 2026-11-18\n\n    code block\n\n## [0.0.1] - 2026-01-01\n"
        self.assertEqual(release.changelog_notes(text, "0.1.0"), "    code block")

    def test_ghcr_404_is_not_always_a_missing_manifest(self):
        registry = release.Registry.__new__(release.Registry)
        registry.headers = {"Authorization": "test"}
        for status, error in [(401, "UNAUTHORIZED"), (403, "DENIED"), (404, "DENIED"), (500, "UNKNOWN")]:
            with self.subTest(status=status), patch.object(release, "http", return_value=(
                status, {}, json.dumps({"errors": [{"code": error}]}).encode(),
            )), self.assertRaises(release.ReleaseError):
                registry.lookup("0.1.0", SHA)
        with patch.object(release, "http", return_value=(404, {}, b'{"errors":[{"code":"MANIFEST_UNKNOWN"}]}')):
            self.assertIsNone(registry.lookup("0.1.0", SHA))

    def test_registry_verifies_manifest_and_config_hashes(self):
        registry = release.Registry.__new__(release.Registry)
        registry.headers = {"Authorization": "test"}
        config = json.dumps({"os": "linux", "architecture": "amd64", "config": {"Labels": {
            "org.opencontainers.image.revision": SHA, "org.opencontainers.image.version": "0.1.0",
            "io.github.delverlab.source": release.SOURCE,
        }}}).encode()
        config_digest = release.content_digest(config)
        manifest = json.dumps({"schemaVersion": 2, "config": {"digest": config_digest}}).encode()
        manifest_digest = release.content_digest(manifest)
        manifest_response = (200, {"Docker-Content-Digest": manifest_digest}, manifest)
        with patch.object(release, "http", side_effect=[manifest_response, (200, {}, config)]):
            self.assertEqual(registry.lookup("0.1.0", SHA), (manifest_digest, config_digest))
        with patch.object(release, "http", side_effect=[manifest_response, (200, {}, config + b" ")]):
            with self.assertRaisesRegex(release.ReleaseError, "image config"):
                registry.lookup("0.1.0", SHA)

    def test_reads_the_tested_image_config_from_the_archive(self):
        ref = f"{IMAGE}:0.1.0"
        with TemporaryDirectory() as temp:
            path = Path(temp) / "candidate.tar"
            manifest = json.dumps([{"RepoTags": [ref], "Config": "config.json"}]).encode()
            config = json.dumps({"os": "linux", "architecture": "amd64", "config": {"Labels": {
                "org.opencontainers.image.revision": SHA, "org.opencontainers.image.version": "0.1.0",
                "io.github.delverlab.source": release.SOURCE,
            }}}).encode()
            with tarfile.open(path, "w") as archive:
                for name, content in [("manifest.json", manifest), ("config.json", config)]:
                    entry = tarfile.TarInfo(name)
                    entry.size = len(content)
                    archive.addfile(entry, io.BytesIO(content))
            self.assertEqual(release.saved_config_digest(path, "0.1.0", SHA), release.content_digest(config))
            with self.assertRaises(release.ReleaseError):
                release.saved_config_digest(path, "0.2.0", SHA)


class PublishFlowTests(unittest.TestCase):
    def test_incomplete_previous_release_blocks_a_new_bump(self):
        previous = "v0.1.0"
        plan = release.Plan(SHA, PARENT, "0.1.1", False, {}, {previous: PARENT}, previous, NOTES)
        registry = Mock(lookup=Mock(return_value=(DIGEST, OTHER_DIGEST)))
        releases = Mock(find=Mock(return_value=None))
        with patch.object(release, "tagged_notes", return_value=NOTES):
            with self.assertRaisesRegex(release.ReleaseError, "Previous release v0.1.0 is incomplete"):
                release.check_previous(plan, registry, releases, release.update_compose(COMPOSE, "0.1.0", DIGEST))

    def publish(self, plan, registry, releases, **mocks):
        with patch.dict(os.environ, {
            "SERVER_APP_TOKEN": "test", "REGISTRY_TOKEN": "test", "RUNNER_TEMP": "/unused",
            "REGISTRY_USERNAME": "test-publisher", "RESUME_RELEASE_ID": "",
            "CANDIDATE_TIMESTAMP": "2026-11-18T00:00:00Z", "VERSION": plan.version,
            "REVISION": plan.revision, "CANDIDATE_TAGS": release.tag_snapshot(plan.tags),
            "TESTED_IMAGE_ID": OTHER_DIGEST,
        }), patch.multiple(release, make_plan=Mock(return_value=plan),
                           saved_config_digest=Mock(return_value=OTHER_DIGEST),
                           Registry=Mock(return_value=registry), Releases=Mock(return_value=releases),
                           clone_deploy=Mock(), read_compose=Mock(return_value=COMPOSE), **mocks):
            release.publish()

    def test_no_publish_mutations_when_main_moves_after_preflight(self):
        plan = release.Plan(SHA, PARENT, "0.1.0", False, {}, {}, None, NOTES)
        registry = Mock(lookup=Mock(return_value=None))
        releases = Mock(find=Mock(return_value=None))
        git, command = Mock(), Mock()
        with self.assertRaisesRegex(release.ReleaseError, "main moved"):
            self.publish(plan, registry, releases, git=git, command=command,
                         server_refs=Mock(return_value={release.MAIN: PARENT}))
        git.assert_not_called()
        self.assertEqual(command.call_count, 1)  # Local docker load only (mocked).
        releases.create.assert_not_called()

    def test_first_publish_atomically_pushes_candidate_and_tag_without_force(self):
        plan = release.Plan(SHA, PARENT, "0.1.0", False, {}, {}, None, NOTES)
        registry = Mock(lookup=Mock(return_value=None))
        releases = Mock(find=Mock(return_value=None))
        git = Mock()
        with self.assertRaisesRegex(release.ReleaseError, "main moved"):
            # Stop immediately after observing the atomic push, without Docker login or publish.
            self.publish(plan, registry, releases, git=git, command=Mock(),
                         server_refs=Mock(return_value={release.MAIN: SHA}))
        self.assertEqual(git.call_args.args, ("push", "--atomic", f"{release.SOURCE}.git",
                                             f"{PARENT}:refs/heads/main", f"{PARENT}:refs/tags/v0.1.0"))
        git.assert_called_once()

    def test_resume_will_not_replace_an_image_missing_from_a_recorded_release(self):
        tag = "v0.1.0"
        tags = {f"refs/tags/{tag}": PARENT}
        plan = release.Plan(SHA, PARENT, "0.1.0", True, tags, {tag: PARENT}, None, NOTES)
        releases = Mock(find=Mock(return_value={"tag_name": tag}))
        git, command = Mock(), Mock()
        with self.assertRaisesRegex(release.ReleaseError, "refusing to replace"):
            self.publish(plan, Mock(lookup=Mock(return_value=None)), releases, git=git, command=command)
        git.assert_not_called()
        self.assertEqual(command.call_count, 1)
        releases.create_draft.assert_not_called()

    def test_resume_refuses_a_registry_image_that_differs_from_the_draft_record(self):
        tag = "v0.1.0"
        plan = release.Plan(SHA, PARENT, "0.1.0", True, {}, {tag: PARENT}, None, NOTES)
        draft = {"tag_name": tag, "draft": True, "prerelease": False,
                 "body": release.evidence(PARENT, DIGEST)}
        releases = Mock(find=Mock(return_value=draft))
        with self.assertRaisesRegex(release.ReleaseError, "conflicts with the tested image"):
            self.publish(plan, Mock(lookup=Mock(return_value=(DIGEST, OTHER_DIGEST))), releases,
                         command=Mock())
        releases.finalize.assert_not_called()

    def test_resume_finalizes_a_verified_draft_even_if_anonymous_probe_rebuilt(self):
        tag = "v0.1.0"
        plan = release.Plan(SHA, PARENT, "0.1.0", True, {}, {tag: PARENT}, None, NOTES)
        draft = {"id": 42, "tag_name": tag, "draft": True, "prerelease": False,
                 "body": release.evidence(PARENT, DIGEST)}
        releases = Mock(find=Mock(return_value=draft), get=Mock(return_value=draft))
        output = Mock()
        cmd = Mock()
        self.publish(plan, Mock(lookup=Mock(return_value=(OTHER_DIGEST, DIGEST))), releases,
                     command=cmd, output=output, server_refs=Mock(return_value={release.MAIN: SHA}))
        releases.finalize.assert_called_once_with(
            draft, tag, release.release_body(NOTES, "0.1.0", PARENT, OTHER_DIGEST, DIGEST), True)
        cmd.assert_called_once()
        output.assert_called_once_with(digest=OTHER_DIGEST, main=SHA, tags=release.tag_snapshot({}))


class ReleaseApiTests(unittest.TestCase):
    def test_placeholder_draft_must_match_title_revision_and_evidence(self):
        draft = {"id": 42, "tag_name": "untagged-165028226fdddf40e04c", "name": "v0.1.0",
                 "target_commitish": SHA, "draft": True, "prerelease": False,
                 "body": release.evidence(SHA, DIGEST)}
        self.assertEqual(release.recorded_image(draft, "v0.1.0", SHA), DIGEST)
        for invalid in (draft | {"name": "v0.2.0"}, draft | {"target_commitish": PARENT},
                        draft | {"draft": False}):
            with self.assertRaises(release.ReleaseError):
                release.recorded_image(invalid, "v0.1.0", SHA)
        with patch.object(release, "http", return_value=(200, {}, json.dumps(draft).encode())):
            self.assertEqual(release.Releases("test").get(42, "v0.1.0"), draft)

    def test_finalize_explicitly_sets_existing_tag(self):
        client = release.Releases("test")
        published = {"tag_name": "v0.1.0", "draft": False, "prerelease": False, "body": NOTES}
        with patch.object(client, "edit", return_value=published) as edit:
            client.finalize({"id": 42, "draft": True}, "v0.1.0", NOTES, True)
        self.assertEqual(edit.call_args.kwargs["tag_name"], "v0.1.0")

    def test_create_draft_uses_the_creation_response_without_listing(self):
        record = {"id": 42, "tag_name": "v0.1.0", "draft": True,
                  "prerelease": False, "body": release.evidence(SHA, DIGEST)}
        with patch.object(release, "http", return_value=(201, {}, json.dumps(record).encode())) as http:
            self.assertEqual(release.Releases("test").create_draft("v0.1.0", SHA, DIGEST), record)
        http.assert_called_once()

    def test_read_release_by_id_rejects_mismatched_tag(self):
        body = json.dumps({"id": 42, "tag_name": "v0.2.0", "draft": True}).encode()
        with patch.object(release, "http", return_value=(200, {}, body)), self.assertRaisesRegex(
                release.ReleaseError, "does not match"):
            release.Releases("test").get(42, "v0.1.0")


class DeployTests(unittest.TestCase):
    def run_deploy(self, first, second=None, *, stale=False, public_digest=DIGEST):
        # Drive the real updater against a changing file; no git remotes or Docker on the host.
        self.pushes = []
        self.checkouts = 0
        with TemporaryDirectory() as temp:
            folder = Path(temp) / "release-deploy"
            folder.mkdir()
            compose = folder / "compose.yaml"
            public_tip = [SHA]

            def git(*args, **kwargs):
                if args[0] == "checkout":
                    self.checkouts += 1
                    text = first if self.checkouts == 1 else second
                    assert text is not None
                    compose.write_text(text, encoding="utf-8")
                if args[:2] == ("rev-parse", "FETCH_HEAD"):
                    return public_tip[0] + "\n"
                if args[:2] == ("rev-parse", "HEAD"):
                    return "c" * 40 + "\n"
                return ""

            def command(*args, **kwargs):
                if args[:2] == ("git", "push"):
                    self.pushes.append(args)
                    public_tip[0] = PARENT if second else "c" * 40
                    return CompletedProcess(args, int(second is not None))
                return CompletedProcess(args, 0)

            def compose_config(directory, *args):
                if args == ("-q",):
                    return b""
                version, digest, _, _ = release.compose_image(compose.read_text())
                image = f"{IMAGE}:{version}" + (f"@{digest}" if digest else "")
                return json.dumps({"services": {"server": {"image": image}}}).encode()

            with patch.dict(os.environ, {
                "RUNNER_TEMP": temp, "VERSION": "0.1.0", "REVISION": SHA, "PUBLISHED_DIGEST": DIGEST,
                "PUBLISHED_MAIN": SHA, "PUBLISHED_TAGS": release.tag_snapshot({}),
                "SERVER_APP_TOKEN": "test", "DEPLOY_APP_TOKEN": "test",
            }), patch.multiple(release, git=git, command=command, compose_config=compose_config,
                               Registry=Mock(return_value=Mock(lookup=Mock(return_value=(public_digest, OTHER_DIGEST)))),
                               server_refs=lambda _: {release.MAIN: PARENT if stale else SHA},
                               remote_refs=lambda *_: {release.MAIN: public_tip[0]}), patch("builtins.print"):
                release.deploy()

    def test_does_not_push_when_server_main_is_stale(self):
        with self.assertRaisesRegex(release.ReleaseError, "Server main"):
            self.run_deploy(COMPOSE, stale=True)
        self.assertEqual(self.checkouts, 0)
        self.assertEqual(self.pushes, [])

    def test_does_not_push_if_the_public_tag_resolves_to_a_different_digest(self):
        with self.assertRaisesRegex(release.ReleaseError, "expected digest"):
            self.run_deploy(COMPOSE, public_digest=OTHER_DIGEST)
        self.assertEqual(self.pushes, [])

    def test_race_reloads_tip_and_stops_if_already_pinned(self):
        self.run_deploy(COMPOSE.replace("0.1.0", f"0.1.0@{DIGEST}"), COMPOSE)
        self.assertEqual(self.checkouts, 2)
        self.assertEqual(self.pushes, [("git", "push", f"https://github.com/{release.DEPLOY}.git", "HEAD:refs/heads/main")])

    def test_race_reloads_tip_and_refuses_newer_pin(self):
        with self.assertRaisesRegex(release.ReleaseError, "newer version"):
            self.run_deploy(COMPOSE.replace("0.1.0", f"0.1.0@{DIGEST}"), COMPOSE.replace("0.1.0", "0.2.0"))
        self.assertEqual(self.checkouts, 2)
        self.assertEqual(len(self.pushes), 1)


if __name__ == "__main__":
    unittest.main()
