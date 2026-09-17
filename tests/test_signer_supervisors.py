import atexit
import json
import os
import pwd
import runpy
import signal
import shutil
import stat
import subprocess
import sys
import tempfile
import time
import unittest
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
TASK_SCRATCH_PARENT = Path(os.environ.get("ASRSUB_SIGNER_TASK_PARENT", "/tmp/agent-scratch"))
BUILD = ROOT / "tools" / "build_signer_supervisors.sh"
POLICY_PATH = ROOT / "tools" / "signer_argv_policy.json"
TASK_SCRATCH_PARENT.mkdir(mode=0o700, parents=True, exist_ok=True)
TASK_SCRATCH = Path(tempfile.mkdtemp(prefix="asrsub-signer-supervisors-", dir=TASK_SCRATCH_PARENT))
BIN_DIR = TASK_SCRATCH / "bin"


def cleanup_task_scratch():
    shutil.rmtree(TASK_SCRATCH, ignore_errors=True)


atexit.register(cleanup_task_scratch)


def tearDownModule():
    cleanup_task_scratch()


subprocess.run([str(BUILD), "--output-dir", str(BIN_DIR)], cwd=ROOT, check=True)
POLICY = json.loads(POLICY_PATH.read_text(encoding="utf-8"))


def command_for(role):
    value = POLICY[role]
    return [
        str(BIN_DIR / Path(value["supervisor"]).name),
        value["role_token"],
        "--implementation-root",
        "ROOT",
        "--",
        *value["command"],
    ]


def run_reject(role, root, command=None, token=None):
    args = command_for(role)
    args[3] = str(root)
    if token is not None:
        args[1] = token
    if command is not None:
        args[5:] = command
    environment = os.environ.copy()
    environment.update(
        {
            "ASRSUB_TEST_INHERITED": "must-not-reach-child",
            "PRIVATE_KEY_PATH": "/tmp/private-test-key.pem",
        }
    )
    return subprocess.run(args, cwd=ROOT, env=environment, capture_output=True, text=True)


class TestSignerPolicyAndBuild(unittest.TestCase):
    def test_policy_binds_both_fixed_supervisors_and_keys(self):
        self.assertEqual(POLICY["supervisor"]["source"], "tools/asrsub_signer_supervisor.c")
        self.assertEqual(POLICY["supervisor"]["build"], "tools/build_signer_supervisors.sh")
        self.assertEqual(POLICY["supervisor"]["install_directory"], "/usr/local/sbin")
        self.assertEqual(POLICY["bundle"]["role_token"], "asrsub-bundle-signing-key")
        self.assertEqual(POLICY["approval"]["role_token"], "asrsub-approval-key")
        self.assertEqual(
            POLICY["bundle"]["private_key_source"],
            "/etc/asrsub/signing/bundle-signing-key.pem",
        )
        self.assertEqual(
            POLICY["approval"]["private_key_source"],
            "/etc/asrsub/signing/approval-key.pem",
        )
        for role, target_fd in (("bundle", "3"), ("approval", "4")):
            self.assertEqual(POLICY[role]["command"][0:4], ["/usr/bin/python3", "tools/asrsub-env", "--pass-fd", target_fd])
            self.assertNotIn("--fixture", POLICY[role]["command"])
            self.assertEqual(POLICY[role]["command"][-2:], ["--key-fd", "3"] if role == "bundle" else ["--approval-key-fd", "4"])

    def test_recipe_emits_two_reproducible_executables(self):
        first = TASK_SCRATCH / "first"
        second = TASK_SCRATCH / "second"
        subprocess.run([str(BUILD), "--output-dir", str(first)], cwd=ROOT, check=True)
        subprocess.run([str(BUILD), "--output-dir", str(second)], cwd=ROOT, check=True)
        for name in ("asrsub-bundle-signer", "asrsub-approval-signer"):
            left = first / name
            right = second / name
            self.assertTrue(os.access(left, os.X_OK))
            self.assertEqual(stat.S_IMODE(left.stat().st_mode), 0o755)
            self.assertEqual(left.read_bytes(), right.read_bytes())

    def test_descriptor_cleanup_has_a_complete_non_close_range_fallback(self):
        source = (ROOT / "tools" / "asrsub_signer_supervisor.c").read_text(encoding="utf-8")
        self.assertIn('opendir("/proc/self/fd")', source)
        self.assertNotIn("maximum = 65536U", source)

    def test_build_recipe_pins_production_tooling_and_destination(self):
        source = BUILD.read_text(encoding="utf-8")
        self.assertTrue(source.startswith("#!/usr/bin/bash\n"))
        self.assertIn('CC_BIN="/usr/bin/cc"', source)
        self.assertIn('INSTALL_BIN="/usr/bin/install"', source)
        self.assertIn('OUTPUT_DIR="/usr/local/sbin"', source)
        self.assertNotIn("--cc", source)


class TestSignerInvocationRejection(unittest.TestCase):
    def setUp(self):
        self.root = Path(tempfile.mkdtemp(prefix="reject-", dir=TASK_SCRATCH))
        (self.root / "tools").mkdir()
        self.sentinel = self.root / "child-ran"
        wrapper = self.root / "tools" / "asrsub-env"
        wrapper.write_text(
            "#!/usr/bin/python3\n"
            "from pathlib import Path\n"
            "Path('child-ran').write_text('ran', encoding='utf-8')\n",
            encoding="utf-8",
        )
        wrapper.chmod(0o755)

    def tearDown(self):
        shutil.rmtree(self.root, ignore_errors=True)

    def assert_rejected_without_child(self, result):
        self.assertEqual(result.returncode, 64, result.stderr)
        self.assertFalse(self.sentinel.exists(), "the rejected child command executed")

    def test_wrong_role_token_is_rejected(self):
        result = run_reject("bundle", self.root, token=POLICY["approval"]["role_token"])
        self.assert_rejected_without_child(result)
        self.assertNotIn("private-test-key.pem", result.stderr)

    def test_extra_reordered_and_caller_selected_key_arguments_are_rejected(self):
        command = POLICY["bundle"]["command"]
        for candidate in (
            command + ["extra"],
            [command[1], command[0], *command[2:]],
            [*command[:-2], "--key-path", "/tmp/private-test-key.pem"],
            [*command[:-1], "7"],
            [*command[:1], "/tmp/tools/asrsub-env", *command[2:]],
        ):
            result = run_reject("bundle", self.root, candidate)
            self.assert_rejected_without_child(result)
            self.assertNotIn("private-test-key.pem", result.stderr)

    def test_relative_traversal_and_symlink_implementation_roots_are_rejected(self):
        traversal = str(self.root / ".." / self.root.name)
        result = run_reject("bundle", traversal)
        self.assert_rejected_without_child(result)

        real = self.root / "real"
        real.mkdir()
        link = self.root / "link"
        link.symlink_to(real, target_is_directory=True)
        result = run_reject("bundle", link)
        self.assert_rejected_without_child(result)

    def test_relative_and_missing_implementation_roots_are_rejected(self):
        result = run_reject("approval", "relative-root")
        self.assert_rejected_without_child(result)
        result = run_reject("approval", self.root / "missing")
        self.assert_rejected_without_child(result)


class TestRealAsrsubEnvBoundary(unittest.TestCase):
    def test_real_wrapper_forwards_fd_environment_and_exit_status(self):
        parent = TASK_SCRATCH / "asrsub-env-parent"
        parent.mkdir(mode=0o700)
        child = TASK_SCRATCH / "forward-child.py"
        observation = TASK_SCRATCH / "forward-observation.json"
        child.write_text(
            "import json, os, pathlib, sys\n"
            "visible = []\n"
            "for name in os.listdir('/proc/self/fd'):\n"
            "    fd = int(name)\n"
            "    if fd >= 3:\n"
            "        try: os.fstat(fd)\n"
            "        except OSError: continue\n"
            "        visible.append(fd)\n"
            "pathlib.Path(sys.argv[1]).write_text(json.dumps({'env': dict(os.environ), 'fds': sorted(visible)}), encoding='utf-8')\n"
            "raise SystemExit(17)\n",
            encoding="utf-8",
        )
        key = TASK_SCRATCH / "forward-key.pem"
        key.write_bytes(b"temporary-test-key\n")
        key_fd = os.open(key, os.O_RDONLY)
        try:
            if key_fd != 3:
                os.dup2(key_fd, 3)
                os.close(key_fd)
                key_fd = 3
            module = runpy.run_path(str(ROOT / "tools" / "asrsub-env"))
            module["SCRATCH_PARENT"] = parent
            old_environment = os.environ.copy()
            os.environ["ASRSUB_TEST_INHERITED"] = "must-not-reach-child"
            try:
                result = module["main"](
                    [
                        "--pass-fd",
                        "3",
                        "/usr/bin/python3",
                        str(child),
                        str(observation),
                    ]
                )
            finally:
                os.environ.clear()
                os.environ.update(old_environment)
            self.assertEqual(result, 17)
            observed = json.loads(observation.read_text(encoding="utf-8"))
            self.assertEqual(observed["fds"], [3])
            self.assertNotIn("ASRSUB_TEST_INHERITED", observed["env"])
        finally:
            try:
                os.close(3)
            except OSError:
                pass

    def test_wrapper_cancellation_contract_forwards_process_group_signal(self):
        source = (ROOT / "tools" / "asrsub-env").read_text(encoding="utf-8")
        self.assertIn("start_new_session=True", source)
        self.assertIn("os.killpg", source)
        self.assertIn("signal.SIGTERM", source)

    def test_real_wrapper_cancellation_stops_its_child_group(self):
        parent = TASK_SCRATCH / "cancel-parent"
        parent.mkdir(mode=0o700)
        child = TASK_SCRATCH / "cancel-child.py"
        marker = TASK_SCRATCH / "cancel-child-survived"
        child.write_text(
            "import pathlib, sys, time\n"
            "time.sleep(1.0)\n"
            "pathlib.Path(sys.argv[1]).write_text('survived', encoding='utf-8')\n",
            encoding="utf-8",
        )
        runner = (
            "import pathlib, runpy, sys\n"
            "module = runpy.run_path(sys.argv[4])\n"
            "module['SCRATCH_PARENT'] = pathlib.Path(sys.argv[1])\n"
            "raise SystemExit(module['main'](['/usr/bin/python3', sys.argv[2], sys.argv[3]]))\n"
        )
        process = subprocess.Popen(
            [sys.executable, "-c", runner, str(parent), str(child), str(marker), str(ROOT / "tools" / "asrsub-env")],
            cwd=ROOT,
            start_new_session=True,
        )
        try:
            time.sleep(0.25)
            process.terminate()
            returncode = process.wait(timeout=3)
            time.sleep(1.1)
            survived = marker.exists()
        except subprocess.TimeoutExpired:
            os.killpg(process.pid, signal.SIGKILL)
            process.wait(timeout=3)
            raise
        finally:
            try:
                os.killpg(process.pid, signal.SIGKILL)
            except ProcessLookupError:
                pass
        self.assertEqual(returncode, 128 + signal.SIGTERM)
        self.assertFalse(survived, "the cancelled wrapper left its child running")


def privileged_command(*args, check=True):
    prefix = [] if os.geteuid() == 0 else ["sudo", "-n"]
    return subprocess.run(prefix + list(args), check=check, capture_output=True, text=True)


def non_root_identity():
    try:
        account = pwd.getpwnam("nobody")
    except KeyError:
        account = next((item for item in pwd.getpwall() if item.pw_uid != 0), None)
    if account is None or account.pw_uid == 0:
        return None
    return account.pw_uid, account.pw_gid


class TestSignerExecutionBoundary(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        if os.geteuid() != 0 and privileged_command("true", check=False).returncode != 0:
            raise RuntimeError("root-owned fixed-key integration needs root or passwordless sudo")
        cls.fixed_root = Path("/etc/asrsub")
        if cls.fixed_root.exists():
            raise RuntimeError("refusing to disturb an existing /etc/asrsub signing installation")
        cls.tmp = Path(tempfile.mkdtemp(prefix="asrsub-signer-supervisors-", dir="/var/tmp"))
        cls.fixed_created = False
        cls.addClassCleanup(cls._cleanup)
        cls.key_source = cls.tmp / "temporary-rsa-key.pem"
        subprocess.run(
            [
                "/usr/bin/openssl",
                "genpkey",
                "-algorithm",
                "RSA",
                "-pkeyopt",
                "rsa_keygen_bits:2048",
                "-out",
                str(cls.key_source),
            ],
            check=True,
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
        )
        privileged_command("install", "-d", "-o", "root", "-g", "root", "-m", "0755", "/etc/asrsub/signing")
        cls.fixed_created = True
        for name in ("bundle-signing-key.pem", "approval-key.pem"):
            privileged_command(
                "install",
                "-o",
                "root",
                "-g",
                "root",
                "-m",
                "0400",
                str(cls.key_source),
                f"/etc/asrsub/signing/{name}",
            )
        cls.implementation = cls.tmp / "implementation"
        for directory in (
            "tools",
            "release",
            "release/runtime",
            "release/systemd",
            "release/systemd/docker.service.d",
        ):
            (cls.implementation / directory).mkdir(parents=True, exist_ok=True)
            (cls.implementation / directory).chmod(0o755)
        required_files = (
            "tools/package_bundle.py",
            "tools/create_approval.py",
            "release/approval-canonical.json",
            "release/runtime/asrsub",
            "release/runtime/asrsub-state",
            "release/runtime/asrsub-record-rollout",
            "release/runtime/asrsub-generate-media-runtime-manifest",
            "release/runtime/asrsub-recover",
            "release/runtime/asrsub-runtime",
            "release/runtime/asrsub-health-probe",
            "release/runtime/asrsub-provision-statefs",
            "release/runtime/media-runtime-dependencies.json",
            "release/runtime/production_entrypoint.py",
            "release/runtime/production_adapter_common.py",
            "release/runtime/deploy_docker.py",
            "release/runtime/compose.yaml",
            "release/systemd/asrsub-recovery.service",
            "release/systemd/asrsub-runtime.service",
            "release/systemd/docker.service.d/asrsub-recovery.conf",
        )
        for relative in required_files:
            path = cls.implementation / relative
            path.write_text("fixture\n", encoding="utf-8")
            path.chmod(0o644)
        (cls.implementation / "supervisor-exit-code").write_text("0\n", encoding="utf-8")
        (cls.implementation / "supervisor-exit-code").chmod(0o666)
        (cls.implementation / "tools" / "asrsub-env").write_text(
            """#!/usr/bin/python3
import json
import os
import pathlib
import sys

target = int(sys.argv[2])
visible = []
for entry in os.listdir('/proc/self/fd'):
    fd = int(entry)
    try:
        os.fstat(fd)
    except OSError:
        continue
    if fd >= 3:
        visible.append(fd)
pathlib.Path('supervisor-observation.json').write_text(
    json.dumps({'argv': sys.argv, 'env': dict(os.environ), 'fds': sorted(visible)}) + '\\n',
    encoding='utf-8',
)
raise SystemExit(int(pathlib.Path('supervisor-exit-code').read_text(encoding='utf-8')))
""",
            encoding="utf-8",
        )
        (cls.implementation / "tools" / "asrsub-env").chmod(0o755)
        privileged_command("chown", "-R", "root:root", str(cls.tmp))
        privileged_command("chmod", "0755", str(cls.tmp), str(cls.implementation))

    @classmethod
    def _cleanup(cls):
        if getattr(cls, "fixed_created", False):
            for name in ("bundle-signing-key.pem", "approval-key.pem"):
                privileged_command("rm", "-f", f"/etc/asrsub/signing/{name}", check=False)
            privileged_command("rmdir", "/etc/asrsub/signing", check=False)
            privileged_command("rmdir", "/etc/asrsub", check=False)
        if hasattr(cls, "tmp"):
            privileged_command("rm", "-rf", str(cls.tmp), check=False)

    def invoke(self, role):
        value = POLICY[role]
        args = [
            str(BIN_DIR / Path(value["supervisor"]).name),
            value["role_token"],
            "--implementation-root",
            str(self.implementation),
            "--",
            *value["command"],
        ]
        inherited = os.environ.copy()
        inherited.update(
            {
                "ASRSUB_TEST_INHERITED": "must-not-reach-child",
                "PRIVATE_KEY_PATH": "/tmp/private-test-key.pem",
            }
        )
        if os.geteuid() == 0:
            return subprocess.run(args, cwd=ROOT, env=inherited, capture_output=True, text=True)
        return subprocess.run(
            ["sudo", "-n", "env", "ASRSUB_TEST_INHERITED=must-not-reach-child", "PRIVATE_KEY_PATH=/tmp/private-test-key.pem", *args],
            cwd=ROOT,
            env=inherited,
            capture_output=True,
            text=True,
        )

    def observation(self):
        return json.loads((self.implementation / "supervisor-observation.json").read_text(encoding="utf-8"))

    def test_good_invocation_has_exact_command_fd_and_scrubbed_environment(self):
        for role, target_fd in (("bundle", 3), ("approval", 4)):
            (self.implementation / "supervisor-exit-code").write_text("0\n", encoding="utf-8")
            result = self.invoke(role)
            self.assertEqual(result.returncode, 0, result.stderr)
            observed = self.observation()
            self.assertEqual(observed["argv"], POLICY[role]["command"][1:])
            self.assertEqual(observed["fds"], [target_fd])
            self.assertEqual(set(observed["env"]), {"LANG", "LC_ALL", "PATH"})
            self.assertNotIn("PRIVATE_KEY_PATH", observed["env"])
            self.assertNotIn("private-test-key.pem", json.dumps(observed))

    def test_child_nonzero_status_is_returned_unchanged(self):
        (self.implementation / "supervisor-exit-code").write_text("23\n", encoding="utf-8")
        result = self.invoke("bundle")
        self.assertEqual(result.returncode, 23, result.stderr)

    def test_wrong_key_mode_owner_type_and_symlink_fail_closed(self):
        fixed = Path("/etc/asrsub/signing/bundle-signing-key.pem")
        privileged_command("chmod", "0600", str(fixed))
        self.assertNotEqual(self.invoke("bundle").returncode, 0)
        privileged_command("chmod", "0400", str(fixed))

        identity = non_root_identity()
        if identity is None:
            self.skipTest("no non-root account is available for the ownership-negative case")
        try:
            privileged_command("chown", f"{identity[0]}:{identity[1]}", str(fixed))
            self.assertNotEqual(self.invoke("bundle").returncode, 0)
        finally:
            privileged_command("chown", "root:root", str(fixed))

        backup = Path("/etc/asrsub/signing/.bundle-signing-key.pem.test-backup")
        privileged_command("mv", str(fixed), str(backup))
        privileged_command("mkdir", str(fixed))
        self.assertNotEqual(self.invoke("bundle").returncode, 0)
        privileged_command("rmdir", str(fixed))
        privileged_command("mv", str(backup), str(fixed))
        privileged_command("chmod", "0400", str(fixed))

        privileged_command("mv", str(fixed), str(backup))
        privileged_command("ln", "-s", str(self.key_source), str(fixed))
        self.assertNotEqual(self.invoke("bundle").returncode, 0)
        privileged_command("rm", "-f", str(fixed))
        privileged_command("mv", str(backup), str(fixed))
        privileged_command("chmod", "0400", str(fixed))

    def test_unsafe_implementation_root_mode_fails_closed(self):
        privileged_command("chmod", "0777", str(self.implementation))
        result = self.invoke("bundle")
        self.assertNotEqual(result.returncode, 0)
        privileged_command("chmod", "0755", str(self.implementation))


if __name__ == "__main__":
    unittest.main()
