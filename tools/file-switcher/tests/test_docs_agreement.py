"""The error-code list has three homes; this keeps them in agreement.

A code must exist in the backend script, be explained in ``fsx.errors``, and be
documented in ``docs/技术文档.md``.  Drift between those is how a user ends up
seeing a blank or English dialog, so it is checked rather than trusted.
"""
import re
import unittest
from pathlib import Path

import sys
sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from fsx import errors

ROOT = Path(__file__).resolve().parents[1]
SCRIPT = ROOT / "folder-switcher.ps1"
DOC = ROOT / "docs" / "技术文档.md"

# Codes produced by fsx.backend itself, which never appear in the script.
TRANSPORT_CODES = {
    "backend-no-output", "backend-bad-json", "backend-contract",
    "backend-exit-mismatch", "backend-timeout", "backend-start-failed",
}

# Codes the script can report without a literal throw: the catch-all default.
DEFAULT_CODES = {"operation-failed"}

# String literals that look like codes but are not.
NOT_CODES = {"null", "true", "false"}


def script_codes(script: str) -> set[str]:
    """Every error code the script can emit.

    Three call shapes exist:
    - ``Throw-Coded 'code' 'message'`` with a literal
    - ``Fail 'code' 'message'`` for the catch-all default
    - ``return 'code'`` inside the Plan-Code helper, whose result is passed to
      ``Throw-Coded (Plan-Code $blockers) '...'``
    """
    codes = set(re.findall(r"Throw-Coded\s+'([a-z][a-z0-9-]*)'", script))
    codes.update(re.findall(r"Fail\s+'([a-z][a-z0-9-]*)'", script))
    codes.update(re.findall(r"return\s+'([a-z][a-z0-9-]*)'", script))
    return codes - NOT_CODES


class CodeListAgreementTests(unittest.TestCase):
    def setUp(self):
        self.script = SCRIPT.read_text(encoding="utf-8")
        self.doc = DOC.read_text(encoding="utf-8")
        self.explained = errors.known_codes()

    def test_every_script_code_has_an_explanation(self):
        """Any code the backend can emit must be translatable."""
        emitted = script_codes(self.script)
        self.assertGreater(len(emitted), 30, "code extraction looks broken")
        missing = sorted(emitted - self.explained)
        self.assertFalse(missing, f"codes emitted but never explained: {missing}")

    def test_every_explanation_is_reachable(self):
        """An explanation for a code nothing emits is dead weight or a typo."""
        emitted = script_codes(self.script)
        unexplained = sorted(self.explained - emitted - TRANSPORT_CODES - DEFAULT_CODES)
        self.assertFalse(
            unexplained,
            f"explained but never emitted (typo or stale entry?): {unexplained}")

    def test_the_document_lists_every_code(self):
        missing = sorted(code for code in self.explained if code not in self.doc)
        self.assertFalse(missing, f"missing from docs/技术文档.md: {missing}")

    def test_documented_plan_codes_match_the_script_blockers(self):
        """The blocker prefixes the UI maps to codes must still be the ones used."""
        for blocker in ("target-conflict", "delete-not-authorized",
                        "delete-hash-unrecognized"):
            self.assertIn(blocker, self.script)
            self.assertIn(blocker, self.doc)

    def test_script_uses_no_blanket_failure_for_business_errors(self):
        """Nothing should fall back to operation-failed when a code fits."""
        # operation-failed may only be the catch-all default, never a literal
        # Fail argument.
        self.assertNotIn("Fail 'operation-failed'", self.script)


if __name__ == "__main__":
    unittest.main()
