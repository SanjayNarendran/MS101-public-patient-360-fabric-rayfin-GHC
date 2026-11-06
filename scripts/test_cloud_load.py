import argparse
import base64
import json
import pathlib
import shutil
import subprocess
import tempfile
import unittest
from unittest.mock import Mock, call, patch

import requests

import cloud_load
import run_all
from setup_fabric import Fabric


class CloudConfigTests(unittest.TestCase):
    def test_renders_files_and_command(self):
        text = cloud_load.cloud_config("https://example.com/repo.git", "abc123", "WORKSPACE_ID=ws\n", ["--skip-raw"])
        self.assertTrue(text.startswith("#cloud-config\n"))
        config = json.loads(text.split("\n", 1)[1])
        files = {f["path"]: base64.b64decode(f["content"]).decode() for f in config["write_files"]}
        self.assertEqual(files["/var/lib/p360/fabric.env"], "WORKSPACE_ID=ws\n")
        self.assertNotIn("\r", files["/var/lib/p360/cloud_load.sh"])
        self.assertTrue(files["/var/lib/p360/cloud_load.sh"].startswith("#!/bin/bash\n"))
        self.assertEqual(config["runcmd"][0][-4:], ["/var/lib/p360/cloud_load.sh", "https://example.com/repo.git",
                                                    "abc123", "--skip-raw"])


class CheckoutTests(unittest.TestCase):
    @unittest.skipUnless(shutil.which("bash") and shutil.which("git"), "Bash and Git are required")
    def test_checks_out_a_commit_available_only_through_a_pull_request_ref(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = pathlib.Path(tmp)
            source = root / "source"

            def git(*args):
                return subprocess.run(["git", "-C", str(source), *args], check=True,
                                      capture_output=True, text=True).stdout.strip()

            subprocess.run(["git", "init", "-q", "--initial-branch=main", str(source)], check=True)
            for message in ("base", "pull request"):
                git("-c", "user.name=Test", "-c", "user.email=test@example.invalid",
                    "commit", "-q", "--allow-empty", "-m", message)
            commit = git("rev-parse", "HEAD")
            git("update-ref", "refs/pull/1/head", commit)
            git("update-ref", "refs/heads/main", git("rev-parse", "HEAD~1"))
            script = (cloud_load.ROOT / "scripts" / "cloud_load.sh").read_text().split("\nshift 2", 1)[0]
            script = script.replace("/var/lib/p360/state", f'"{(root / "state").as_posix()}"')
            script = script.replace("/var/log/p360-loader.log", f'"{(root / "loader.log").as_posix()}"')
            script = script.replace("/opt/p360", "clone")
            result = subprocess.run([shutil.which("bash"), "-c", script, "cloud_load.sh", source.as_uri(), commit],
                                    cwd=root, capture_output=True, text=True)
            self.assertEqual(result.returncode, 0, (root / "loader.log").read_text())
            actual = subprocess.run(["git", "-C", str(root / "clone"), "rev-parse", "HEAD"],
                                    check=True, capture_output=True, text=True).stdout.strip()
            self.assertEqual(actual, commit)
            self.assertEqual((root / "state").read_text().strip(), "DONE")


class UpTests(unittest.TestCase):
    def setUp(self):
        self.fabric = self.enterContext(patch("cloud_load.Fabric")).return_value
        self.wait = self.enterContext(patch("cloud_load.wait"))

    def test_refuses_a_url_the_vm_cannot_clone(self):
        args = argparse.Namespace(resource_group="p360-loader", repo="git@github.com:o/r.git", ref="abc",
                                  run_all_args=[], location="x", size="y")
        with patch("cloud_load.fabric_config.load", return_value={"WORKSPACE_ID": "ws"}), \
                patch("cloud_load.az", return_value="false") as az:
            with self.assertRaises(SystemExit):
                cloud_load.up(args)
        self.assertEqual(az.call_count, 1)

    def test_refuses_credentials_and_query_parameters_in_repo_urls(self):
        for repo in ("https://user:password@example.invalid/repo.git",
                     "https://example.invalid/repo.git?token=example",
                     "https://example.invalid/repo.git#fragment"):
            with self.subTest(repo=repo):
                args = argparse.Namespace(resource_group="p360-loader", repo=repo, ref="abc",
                                          run_all_args=[], location="x", size="y")
                with patch("cloud_load.fabric_config.load", return_value={"WORKSPACE_ID": "ws"}), \
                        patch("cloud_load.az", return_value="false") as az:
                    with self.assertRaises(SystemExit) as error:
                        cloud_load.up(args)
                self.assertNotIn(repo, str(error.exception))
                self.assertEqual(az.call_count, 1)

    def test_up_passes_only_fabric_config_and_pipeline_options_to_vm(self):
        args = argparse.Namespace(resource_group="p360-loader", repo="https://example.invalid/repo.git", ref="abc",
                                  run_all_args=["--", "--skip-raw"], location="region", size="size")
        cfg = {"WORKSPACE_ID": "ws", "UNRELATED_SETTING": "must-stay-local"}
        events = []

        def fake_az(*args):
            events.append(args[:2])
            return {("group", "exists"): "false", ("vm", "show"): "pid"}.get(args[:2], "")

        self.fabric.post.side_effect = lambda *args: events.append("grant")
        with patch("cloud_load.fabric_config.load", return_value=cfg), \
                patch("cloud_load.az", side_effect=fake_az) as az, \
                patch("cloud_load.cloud_config", wraps=cloud_load.cloud_config) as config:
            cloud_load.up(args)
        config.assert_called_once_with(args.repo, args.ref, "WORKSPACE_ID=ws\n", ["--skip-raw"])
        self.fabric.post.assert_called_once_with(
            "/workspaces/ws/roleAssignments",
            {"principal": {"id": "pid", "type": "ServicePrincipal"}, "role": "Contributor"})
        self.wait.assert_called_once_with(args.resource_group)
        create = next(c.args for c in az.call_args_list if c.args[:2] == ("group", "create"))
        self.assertIn("p360-workspace=ws", create)
        self.assertIn("p360-principal=pending", create)
        update = next(c.args for c in az.call_args_list if c.args[:2] == ("group", "update"))
        self.assertIn("tags.p360-principal=pid", update)
        self.assertLess(events.index(("group", "update")), events.index("grant"))

    def test_does_not_grant_access_if_cleanup_context_cannot_be_saved(self):
        args = argparse.Namespace(resource_group="p360-loader", repo="https://example.invalid/repo.git", ref="abc",
                                  run_all_args=[], location="region", size="size")

        def fake_az(*args):
            if args[:2] == ("group", "update"):
                raise SystemExit("could not save cleanup context")
            return {("group", "exists"): "false", ("vm", "show"): "pid"}.get(args[:2], "")

        with patch("cloud_load.fabric_config.load", return_value={"WORKSPACE_ID": "ws"}), \
                patch("cloud_load.az", side_effect=fake_az):
            with self.assertRaisesRegex(SystemExit, "cleanup context"):
                cloud_load.up(args)
        self.fabric.post.assert_not_called()
        self.wait.assert_not_called()


class DownTests(unittest.TestCase):
    def setUp(self):
        self.assignment = {"id": "assignment-id", "principal": {"id": "pid"}, "role": "Contributor"}
        self.other = {"id": "other-assignment", "principal": {"id": "other-principal"}, "role": "Admin"}
        self.snapshots = [[self.assignment, self.other], [self.other]]
        self.tags = {"p360-loader": "cloud_load.py", "p360-workspace": "ws", "p360-principal": "pid"}
        self.events = []

        def fake_az(*args, **kwargs):
            self.events.append(args[:2])
            return {("group", "show"): json.dumps(self.tags), ("vm", "list"): "[]"}.get(args[:2], "")

        def assignments(path):
            self.events.append("list roles")
            return self.snapshots.pop(0)

        self.az = self.enterContext(patch("cloud_load.az", side_effect=fake_az))
        self.config = self.enterContext(
            patch("cloud_load.fabric_config.load", return_value={"WORKSPACE_ID": "different-workspace"}))
        self.fabric = self.enterContext(patch("cloud_load.Fabric")).return_value
        self.fabric.get_all.side_effect = assignments
        self.response = Mock(spec=requests.Response, status_code=200, text="response")
        self.delete = self.enterContext(patch("cloud_load.requests.delete", side_effect=self.delete_role))

    def delete_role(self, *args, **kwargs):
        self.events.append("delete role")
        return self.response

    def assert_group_kept(self):
        self.assertNotIn(("group", "delete"), [c.args[:2] for c in self.az.call_args_list])

    def test_refuses_a_group_it_did_not_create(self):
        with patch("cloud_load.az", return_value='{"owner": "someone"}') as az:
            with self.assertRaises(SystemExit):
                cloud_load.down("p360-loader")
        self.assertEqual(az.call_count, 1)
        self.fabric.get_all.assert_not_called()
        self.delete.assert_not_called()

    def test_removes_assignment_by_its_id_and_verifies_before_deleting_group(self):
        cloud_load.down("p360-loader")
        self.delete.assert_called_once_with(
            f"{cloud_load.API}/workspaces/ws/roleAssignments/assignment-id",
            headers=self.fabric._headers.return_value, timeout=60)
        self.assertEqual(self.fabric.get_all.call_args_list,
                         [call("/workspaces/ws/roleAssignments")] * 2)
        self.assertEqual(self.events[-4:], ["list roles", "delete role", "list roles", ("group", "delete")])
        self.config.assert_not_called()
        self.assertNotIn(("vm", "list"), self.events)

    def test_finds_assignment_on_a_later_page(self):
        self.fabric.get_all.side_effect = lambda path: Fabric.get_all(self.fabric, path)
        pages = [Mock(), Mock(), Mock()]
        pages[0].json.return_value = {"value": [self.other], "continuationToken": "next-page"}
        pages[1].json.return_value = {"value": [self.assignment]}
        pages[2].json.return_value = {"value": [self.other]}
        with patch("cloud_load.requests.get", side_effect=pages) as get:
            cloud_load.down("p360-loader")
        self.assertEqual(get.call_count, 3)
        self.assertTrue(get.call_args_list[1].args[0].endswith("?continuationToken=next-page"))
        self.assertTrue(self.delete.call_args.args[0].endswith("/roleAssignments/assignment-id"))

    def test_keeps_group_when_assignment_still_exists_even_after_404(self):
        for status in (200, 404):
            with self.subTest(status=status):
                self.response.status_code = status
                self.snapshots = [[self.assignment], [self.assignment]]
                with self.assertRaisesRegex(SystemExit, "still"):
                    cloud_load.down("p360-loader")
                self.assert_group_kept()

    def test_accepts_404_only_after_verifying_assignment_is_gone(self):
        self.response.status_code = 404
        cloud_load.down("p360-loader")
        self.assertEqual(self.fabric.get_all.call_count, 2)
        self.assertEqual(self.events[-1], ("group", "delete"))

    def test_keeps_group_when_delete_fails(self):
        for status in (403, 429, 500):
            with self.subTest(status=status):
                self.response.status_code = status
                self.snapshots = [[self.assignment]]
                with self.assertRaisesRegex(SystemExit, str(status)):
                    cloud_load.down("p360-loader")
                self.assert_group_kept()

    def test_keeps_group_when_role_lookup_or_verification_fails(self):
        for results in ([requests.HTTPError("lookup failed")],
                        [[self.assignment], requests.HTTPError("verification failed")]):
            with self.subTest(results=results):
                self.fabric.get_all.side_effect = results
                with self.assertRaises(requests.HTTPError):
                    cloud_load.down("p360-loader")
                self.assert_group_kept()

    def test_cleans_up_group_before_any_grant_was_attempted(self):
        self.tags["p360-principal"] = "pending"
        cloud_load.down("p360-loader")
        self.fabric.get_all.assert_not_called()
        self.delete.assert_not_called()
        self.assertEqual(self.events[-1], ("group", "delete"))

    def test_keeps_group_if_cleanup_metadata_is_missing(self):
        for key in ("p360-workspace", "p360-principal"):
            with self.subTest(key=key):
                saved = self.tags.pop(key)
                with self.assertRaisesRegex(SystemExit, "metadata"):
                    cloud_load.down("p360-loader")
                self.assert_group_kept()
                self.fabric.get_all.assert_not_called()
                self.delete.assert_not_called()
                self.tags[key] = saved

    def test_removes_all_matching_assignments_without_touching_other_principals(self):
        second = {"id": "second-assignment", "principal": {"id": "PID"}, "role": "Viewer"}
        self.snapshots = [[self.assignment, self.other, second], [self.other]]
        cloud_load.down("p360-loader")
        self.assertEqual([c.args[0].rsplit("/", 1)[-1] for c in self.delete.call_args_list],
                         ["assignment-id", "second-assignment"])
        self.assertEqual(self.events[-1], ("group", "delete"))

    def test_cleans_up_when_assignment_is_already_absent(self):
        self.snapshots = [[self.other], [self.other]]
        cloud_load.down("p360-loader")
        self.delete.assert_not_called()
        self.assertEqual(self.events[-1], ("group", "delete"))


class StatusTests(unittest.TestCase):
    def status(self, output):
        response = {"value": [{"message": "[stdout]\n" + output[-4096:] + "\n[stderr]\n"}]}
        with patch("cloud_load.az", return_value=json.dumps(response)):
            return cloud_load.status_text("loader")

    def test_reads_trailing_state_after_truncation(self):
        self.assertEqual(self.status("x" * 8000 + "\nP360_STATE=DONE\n").splitlines()[0], "DONE")

    def test_missing_or_invalid_state_is_an_error(self):
        for output in ("log without a marker", "P360_STATE=unexpected\n"):
            with self.subTest(output=output), self.assertRaisesRegex(SystemExit, "state"):
                self.status(output)

    def test_passes_a_temporary_script_file_to_azure_cli(self):
        paths = []

        def fake_az(*args):
            value = args[args.index("--scripts") + 1]
            self.assertTrue(value.startswith("@"))
            path = pathlib.Path(value[1:])
            paths.append(path)
            self.assertEqual(path.read_text(encoding="utf-8"), cloud_load.STATUS_SCRIPT)
            return json.dumps({"value": [{"message": "[stdout]\r\nP360_STATE=DONE\r\n[stderr]\r\n"}]})

        with patch("cloud_load.az", side_effect=fake_az):
            self.assertEqual(cloud_load.status_text("loader"), "DONE")
        self.assertFalse(paths[0].exists())

    @unittest.skipUnless(shutil.which("bash"), "Bash is required for the Linux status-script checks")
    def test_remote_status_script(self):
        script = cloud_load.STATUS_SCRIPT.replace("/var/lib/p360/state", "state")
        script = script.replace("/var/log/p360-loader.log", "loader.log")
        for state, boot, failed, active, expected in (
            ("DONE", "done", False, False, "DONE"),
            ("RUNNING", "done", False, True, "RUNNING"),
            ("RUNNING", "done", True, False, "FAILED"),
            ("RUNNING", "done", False, False, "FAILED"),
            (None, "error - done", False, False, "FAILED"),
            (None, "degraded done", False, False, "FAILED"),
            (None, "running", False, False, "STARTING"),
        ):
            with self.subTest(state=state, boot=boot, failed=failed, active=active), \
                    tempfile.TemporaryDirectory() as tmp:
                root = pathlib.Path(tmp)
                (root / "loader.log").write_text("x" * 12000)
                if state:
                    (root / "state").write_text(state + "\n")
                stubs = (f'systemctl() {{ if [ "$1" = is-failed ]; then return {0 if failed else 1}; '
                         f'else return {0 if active else 1}; fi; }}\n'
                         f'cloud-init() {{ printf "status: {boot}\\n"; }}\n')
                result = subprocess.run([shutil.which("bash"), "-c", stubs + script],
                                        cwd=tmp, capture_output=True, text=True, check=True)
                self.assertLess(len(result.stdout.encode()), 4096)
                self.assertEqual(self.status(result.stdout).splitlines()[0], expected)

    def test_wait_cleans_up_only_after_done(self):
        with patch("cloud_load.time.sleep"), \
                patch("cloud_load.status_text", side_effect=["STARTING", "RUNNING", "DONE"]), \
                patch("cloud_load.down") as down:
            cloud_load.wait("loader")
        down.assert_called_once_with("loader")

    def test_wait_keeps_failed_run(self):
        with patch("cloud_load.time.sleep"), patch("cloud_load.status_text", return_value="FAILED\nboot failed"), \
                patch("cloud_load.down") as down:
            with self.assertRaisesRegex(SystemExit, "failed"):
                cloud_load.wait("loader")
        down.assert_not_called()

    def test_wait_has_a_deadline(self):
        with patch("cloud_load.time.monotonic", side_effect=[0, 21601]), \
                patch("cloud_load.time.sleep"), patch("cloud_load.status_text") as status, \
                patch("cloud_load.down") as down:
            with self.assertRaisesRegex(SystemExit, "Timed out"):
                cloud_load.wait("loader")
        status.assert_not_called()
        down.assert_not_called()


class RunnerTests(unittest.TestCase):
    def test_step_order_and_optional_flags(self):
        for tissue in (False, True):
            for skip_raw in (False, True):
                with self.subTest(tissue=tissue, skip_raw=skip_raw):
                    expected = [["download_sources.py"], ["download_imaging.py"]]
                    if tissue:
                        expected.append(["download_imaging.py", "--tissue-slides"])
                    expected += [["fetch_clinical_genomics.py"], ["build_synthea.py"], ["build_slides.py"]]
                    if tissue:
                        expected.append(["build_slides.py", "--tissue"])
                    expected += [["build_radiology.py"], ["build_tables.py"],
                                 ["upload_onelake.py"] + (["--skip-raw"] if skip_raw else [])]
                    self.assertEqual(run_all.steps(tissue, skip_raw), expected)

    def test_failure_stops_before_upload(self):
        with patch("sys.argv", ["run_all.py"]), \
                patch("run_all.subprocess.run", side_effect=[Mock(returncode=0), Mock(returncode=1)]) as run:
            with self.assertRaisesRegex(SystemExit, "download_imaging.py failed"):
                run_all.main()
        self.assertEqual(run.call_count, 2)
        self.assertEqual(run.call_args.args[0],
                         [run_all.sys.executable, "-u", str(run_all.HERE / "download_imaging.py")])


if __name__ == "__main__":
    unittest.main()
