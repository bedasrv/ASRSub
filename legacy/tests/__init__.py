"""Shared helpers for the orchestrator test suite.

Hermeticity rule: any test that can reach orchestrator's on-disk state must
do so through redirected paths. Production files under
~/.config/asr-pipeline are live artifacts (subtitle_registry.jsonl is an
append-only provenance ledger; hunt backoff state drives pass cadence) and
must never be read-as-state or written by a suite run.

Use HermeticStateMixin on any TestCase that executes orchestrator code —
it redirects every state-file module constant into one fresh temp dir for
the duration of each test. Classes with their own setUp MUST call
super().setUp() first. Explicit per-test patches of the same constants keep
working (inner patch wins while active).
"""

import os
import shutil
import tempfile
import unittest
from unittest.mock import patch

# Module constants in orchestrator pointing at on-disk state (production:
# ~/.config/asr-pipeline/* + ~/.cache/asr-pipeline/*).
ISOLATED_STATE_VARS = (
    "REGISTRY_FILE",
    "REFINE_STATE_FILE",
    "HUNT_STATE_FILE",
    "STATE_FILE",
    "ACTIONS_FILE",
    "EXCLUSIONS_FILE",
    "OVERRIDE_FILE",
    "ASR_CACHE_DIR",
)


class HermeticStateMixin(unittest.TestCase):
    """Redirect orchestrator's state files to a per-test temp dir."""

    def setUp(self):
        self.state_tmp = tempfile.mkdtemp(prefix="asrsub-tests-")
        self.addCleanup(
            shutil.rmtree, self.state_tmp, ignore_errors=True
        )
        # isolate CONTROL_API_KEY env and module global between tests (auth tests set it and reload orchestrator)
        self._prev_control_key = os.environ.get("CONTROL_API_KEY")
        if "CONTROL_API_KEY" in os.environ:
            del os.environ["CONTROL_API_KEY"]
        self.addCleanup(self._restore_control_key)
        self._prev_control_key_file = os.environ.get("CONTROL_API_KEY_FILE")
        if "CONTROL_API_KEY_FILE" in os.environ:
            del os.environ["CONTROL_API_KEY_FILE"]
        self.addCleanup(self._restore_control_key_file)
        # also isolate orchestrator.CONTROL_API_KEY global (reloaded with env in some tests)
        import orchestrator as _o
        self._prev_mod_key = getattr(_o, "CONTROL_API_KEY", "")
        _o.CONTROL_API_KEY = ""
        self.addCleanup(self._restore_mod_key)

        for name in ISOLATED_STATE_VARS:
            p = patch(
                f"orchestrator.{name}",
                os.path.join(self.state_tmp, name.lower()),
            )
            p.start()
            self.addCleanup(p.stop)

    def _restore_control_key(self):
        if self._prev_control_key is not None:
            os.environ["CONTROL_API_KEY"] = self._prev_control_key
        elif "CONTROL_API_KEY" in os.environ:
            del os.environ["CONTROL_API_KEY"]

    def _restore_control_key_file(self):
        if self._prev_control_key_file is not None:
            os.environ["CONTROL_API_KEY_FILE"] = self._prev_control_key_file
        elif "CONTROL_API_KEY_FILE" in os.environ:
            del os.environ["CONTROL_API_KEY_FILE"]

    def _restore_mod_key(self):
        import orchestrator as _o
        _o.CONTROL_API_KEY = self._prev_mod_key

    def tearDown(self):
        # Fixture dirs subclasses create as self.tmp (mkdtemp) are per-test
        # garbage too: without this, every suite run leaves behind
        # /tmp/tmp*/E1.mkv-style artifact trees forever.
        tmp = getattr(self, "tmp", None)
        if (
            tmp
            and isinstance(tmp, str)
            and os.path.isdir(tmp)
            and os.path.commonpath([tmp, tempfile.gettempdir()])
            == tempfile.gettempdir()
        ):
            shutil.rmtree(tmp, ignore_errors=True)
