"""Release metadata execution tests — verify build emits correct descriptor."""
import os
import json
import re
import subprocess
import tempfile
import shutil
from pathlib import Path
import unittest


REPO = Path(__file__).resolve().parents[1]
BUILD = REPO / "build.sh"


class TestReleaseDescriptorEmission(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.mkdtemp(prefix="asrsub-release-")
        # Create fake docker stub
        self.bin_dir = os.path.join(self.tmp, "bin")
        os.makedirs(self.bin_dir, exist_ok=True)
        docker_stub = os.path.join(self.bin_dir, "docker")
        with open(docker_stub, "w") as fh:
            fh.write("#!/usr/bin/env bash\n")
            fh.write('if [[ "$1" == "build" ]]; then echo "fake docker build $@"; exit 0; fi\n')
            fh.write('if [[ "$1" == "image" && "$2" == "inspect" ]]; then exit 0; fi\n')
            fh.write('echo "fake docker $@"\n')
            fh.write("exit 0\n")
        os.chmod(docker_stub, 0o755)
        # Copy build.sh and repo files needed
        self.build_copy = os.path.join(self.tmp, "build.sh")
        shutil.copy2(BUILD, self.build_copy)
        # Create a minimal git repo with a known SHA
        self.repo_dir = os.path.join(self.tmp, "repo")
        os.makedirs(self.repo_dir, exist_ok=True)
        subprocess.run(["git", "init", "-q", self.repo_dir], check=True)
        subprocess.run(["git", "-C", self.repo_dir, "config", "user.email", "test@test.local"], check=True)
        subprocess.run(["git", "-C", self.repo_dir, "config", "user.name", "Test"], check=True)
        # Add a file and commit
        Path(self.repo_dir, "dummy.txt").write_text("hello")
        subprocess.run(["git", "-C", self.repo_dir, "add", "."], check=True)
        subprocess.run(["git", "-C", self.repo_dir, "commit", "-qm", "init"], check=True)
        # Get full SHA
        self.sha = subprocess.check_output(["git", "-C", self.repo_dir, "rev-parse", "HEAD"], text=True).strip()
        self.assertRegex(self.sha, r"^[0-9a-f]{40}$")
        # Copy build.sh into repo
        shutil.copy2(BUILD, os.path.join(self.repo_dir, "build.sh"))
        # Copy a minimal Dockerfile to satisfy docker build
        Path(os.path.join(self.repo_dir, "Dockerfile")).write_text("FROM scratch\n")
        # Need docker-compose.yml stub? build.sh does docker build, not compose, so no need

    def tearDown(self):
        shutil.rmtree(self.tmp, ignore_errors=True)

    def test_build_emits_release_env_and_json(self):
        env = os.environ.copy()
        env["PATH"] = self.bin_dir + ":" + env.get("PATH", "")
        # Run build.sh inside repo (should use git rev-parse HEAD)
        result = subprocess.run(["bash", "build.sh"], cwd=self.repo_dir, env=env, capture_output=True, text=True)
        self.assertEqual(result.returncode, 0, msg=f"build.sh failed: stdout={result.stdout} stderr={result.stderr}")
        release_env = Path(self.repo_dir, ".release.env")
        release_json = Path(self.repo_dir, "release.json")
        self.assertTrue(release_env.is_file(), msg=".release.env must be emitted")
        self.assertTrue(release_json.is_file(), msg="release.json must be emitted")
        env_text = release_env.read_text()
        self.assertIn(f"ASRSUB_IMAGE=ghcr.io/bedasrv/asrsub:{self.sha}", env_text)
        self.assertIn(f"GIT_SHA={self.sha}", env_text)
        self.assertRegex(env_text, r"BUILD_TIME=")
        # No secrets in descriptor
        for secret in ("CONTROL_API_KEY", "BAZARR_API_KEY", "SONARR_API_KEY"):
            self.assertNotIn(secret, env_text)
        self.assertNotIn(secret, release_json.read_text())
        j = json.loads(release_json.read_text())
        self.assertEqual(j["asrsub_image"], f"ghcr.io/bedasrv/asrsub:{self.sha}")
        self.assertEqual(j["git_sha"], self.sha)
        self.assertIn("build_time", j)

    def test_build_fails_on_short_sha_arg(self):
        env = os.environ.copy()
        env["PATH"] = self.bin_dir + ":" + env.get("PATH", "")
        # Pass short SHA explicitly -> should fail closed
        short = self.sha[:7]
        result = subprocess.run(["bash", "build.sh", short], cwd=self.repo_dir, env=env, capture_output=True, text=True)
        self.assertNotEqual(result.returncode, 0, msg="build.sh must fail on short SHA")
        self.assertIn("40-char", result.stderr + result.stdout)

    def test_build_registry_override(self):
        # ASRSUB_REGISTRY redirects the tag (mirrors, forks) without
        # touching the SHA-immutable scheme.
        env = os.environ.copy()
        env["PATH"] = self.bin_dir + ":" + env.get("PATH", "")
        env["ASRSUB_REGISTRY"] = "example.com/x"
        result = subprocess.run(["bash", "build.sh"], cwd=self.repo_dir, env=env, capture_output=True, text=True)
        self.assertEqual(result.returncode, 0, msg=f"build.sh failed: stdout={result.stdout} stderr={result.stderr}")
        env_text = Path(self.repo_dir, ".release.env").read_text()
        self.assertIn(f"ASRSUB_IMAGE=example.com/x/asrsub:{self.sha}", env_text)
