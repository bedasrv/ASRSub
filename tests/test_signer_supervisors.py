import json
import os
import shutil
import stat
import subprocess
import tempfile
import unittest
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
SCRATCH = Path("/tmp/agent-scratch/asrsub-signer-supervisors")
BUILD = ROOT / "tools" / "build_signer_supervisors.sh"
POLICY_PATH = ROOT / "tools" / "signer_argv_policy.json"
if not SCRATCH.exists():
    SCRATCH.mkdir(mode=0o700, parents=True, exist_ok=True)
MODULE_ROOT = Path(tempfile.mkdtemp(prefix="test-", dir=SCRATCH))
BIN_DIR = MODULE_ROOT / "bin"
subprocess.run([str(BUILD), "--output-dir", str(BIN_DIR)], cwd=ROOT, check=True)
POLICY = json.loads(POLICY_PATH.read_text(encoding="utf-8"))


def tearDownModule():
    shutil.rmtree(MODULE_ROOT, ignore_errors=True)


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
        first = MODULE_ROOT / "first"
        second = MODULE_ROOT / "second"
        subprocess.run([str(BUILD), "--output-dir", str(first)], cwd=ROOT, check=True)
        subprocess.run([str(BUILD), "--output-dir", str(second)], cwd=ROOT, check=True)
        for name in ("asrsub-bundle-signer", "asrsub-approval-signer"):
            left = first / name
            right = second / name
            self.assertTrue(os.access(left, os.X_OK))
            self.assertEqual(stat.S_IMODE(left.stat().st_mode), 0o755)
            self.assertEqual(left.read_bytes(), right.read_bytes())


class TestSignerInvocationRejection(unittest.TestCase):
    def setUp(self):
        self.root = Path(tempfile.mkdtemp(prefix="reject-", dir=SCRATCH))
        (self.root / "tools").mkdir()

    def tearDown(self):
        shutil.rmtree(self.root, ignore_errors=True)

    def test_wrong_role_token_is_rejected(self):
        result = run_reject("bundle", self.root, token=POLICY["approval"]["role_token"])
        self.assertNotEqual(result.returncode, 0)
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
            self.assertNotEqual(result.returncode, 0, candidate)
            self.assertNotIn("private-test-key.pem", result.stderr)

    def test_relative_traversal_and_symlink_implementation_roots_are_rejected(self):
        traversal = str(self.root / ".." / self.root.name)
        result = run_reject("bundle", traversal)
        self.assertNotEqual(result.returncode, 0)

        real = self.root / "real"
        real.mkdir()
        link = self.root / "link"
        link.symlink_to(real, target_is_directory=True)
        result = run_reject("bundle", link)
        self.assertNotEqual(result.returncode, 0)

    def test_relative_and_missing_implementation_roots_are_rejected(self):
        result = run_reject("approval", "relative-root")
        self.assertNotEqual(result.returncode, 0)
        result = run_reject("approval", self.root / "missing")
        self.assertNotEqual(result.returncode, 0)


def privileged_command(*args, check=True):
    prefix = [] if os.geteuid() == 0 else ["sudo", "-n"]
    return subprocess.run(prefix + list(args), check=check, capture_output=True, text=True)


def trusted_scratch_ancestry(path):
    current = path
    while current != current.parent:
        status = current.stat()
        if status.st_uid != 0 and not (status.st_mode & stat.S_ISVTX):
            return False
        current = current.parent
    return True


class TestSignerExecutionBoundary(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        if os.geteuid() != 0 and privileged_command("true", check=False).returncode != 0:
            raise unittest.SkipTest("root-owned fixed-key integration needs root or passwordless sudo")
        if not trusted_scratch_ancestry(SCRATCH):
            raise unittest.SkipTest("temporary implementation root is not under a trusted directory")
        cls.fixed_root = Path("/etc/asrsub")
        if cls.fixed_root.exists():
            raise unittest.SkipTest("do not disturb an existing /etc/asrsub signing installation")
        cls.tmp = Path(tempfile.mkdtemp(prefix="execution-", dir=SCRATCH))
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
        (cls.implementation / "tools").mkdir(parents=True)
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
    def tearDownClass(cls):
        if not hasattr(cls, "fixed_root") or not cls.fixed_root.exists():
            return
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

        privileged_command("chown", f"{os.getuid()}:{os.getgid()}", str(fixed))
        self.assertNotEqual(self.invoke("bundle").returncode, 0)
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
