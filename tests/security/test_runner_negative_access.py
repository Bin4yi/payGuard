"""CLAUDE_CODE_PROMPT.md Phase 5, item 2: "Check real deployed negative
access: runner cannot write Firestore, sign or invoke an alternate payment
path." A real deployed check requires an actual Cloud Run/IAM environment
this session does not have (see README.md's Phase 5 section). What follows
is the honest, weaker, code-level substitute: static proof that the runner
module has no dependency path to a signing capability at all — it cannot
sign a mandate, grant, or receipt no matter what code inside it runs,
because the modules that can sign are never imported.

This does NOT prove a deployed runner service account lacks IAM permission
to invoke KMS — only that the Python code has no client for it. Record
this as code-level evidence with that explicit limitation, not as "the
runner cannot sign" full stop (SPEC.md Section 13: "Record evidence and
limitations, not claims of universal proof.").
"""

from __future__ import annotations

import ast
import pathlib

RUNNER_DIR = pathlib.Path(__file__).resolve().parents[2] / "services" / "runner"

# Modules that can actually produce a signature. Note ``services.shared.crypto``
# is deliberately NOT here: it only defines the Signer/VerifierRegistry
# Protocols (no key material, no cryptography import) — the runner legitimately
# holds a VerifierRegistry reference to pass through to the gateway, without
# ever gaining the ability to sign anything. ``local_adapters`` is where an
# actual RSA-PSS signing implementation lives; that one must never appear here.
FORBIDDEN_MODULES = {
    "services.shared.local_adapters",
}


def _imported_module_names(py_file: pathlib.Path) -> set[str]:
    tree = ast.parse(py_file.read_text(encoding="utf-8"), filename=str(py_file))
    names: set[str] = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            for alias in node.names:
                names.add(alias.name)
        elif isinstance(node, ast.ImportFrom) and node.module:
            names.add(node.module)
    return names


def test_runner_module_never_imports_a_signing_capable_module():
    offenders = []
    for py_file in RUNNER_DIR.rglob("*.py"):
        imported = _imported_module_names(py_file)
        hit = imported & FORBIDDEN_MODULES
        if hit:
            offenders.append((py_file, hit))
    assert offenders == [], f"services/runner imports signing-capable modules directly: {offenders}"


def test_runner_module_never_imports_kms_or_firebase_admin_sdks():
    """Even if the local test/dev adapters were removed, the runner must
    not import a real cloud signing SDK either."""
    forbidden_substrings = ("google.cloud.kms", "firebase_admin")
    offenders = []
    for py_file in RUNNER_DIR.rglob("*.py"):
        text = py_file.read_text(encoding="utf-8")
        for needle in forbidden_substrings:
            if needle in text:
                offenders.append((py_file, needle))
    assert offenders == [], f"services/runner references a cloud signing SDK: {offenders}"
