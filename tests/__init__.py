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
        for name in ISOLATED_STATE_VARS:
            p = patch(
                f"orchestrator.{name}",
                os.path.join(self.state_tmp, name.lower()),
            )
            p.start()
            self.addCleanup(p.stop)

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
