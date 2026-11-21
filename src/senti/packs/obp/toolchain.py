"""How Fix this makes and checks a fix in OBP-API: Maven offline, the code and the reviewed Lift/http4s ResourceDoc
differences it may change, and the props files the build needs."""

from ...kinds import Toolchain
from . import LIFT_REFERENCE

PARITY_SCRIPTS = ("scripts/check_lift_http4s_resource_doc_parity.py", "scripts/resource_doc_baseline/allowlist_helper.py")


def maven(worktree: str) -> list[str]:
    """Offline, and both modules in one build, so a change to obp-commons is never installed in ~/.m2."""
    return ["mvn", "-o", "-q", "-f", f"{worktree}/pom.xml", "-pl", "obp-commons,obp-api"]


FIX_NOTES = (
    "- Leave obp-commons alone unless the fix needs it. Read {worktree}/CLAUDE.md first: it says how endpoints and "
    "their tests are written. If the endpoint did this right in Lift, do what Lift did (Read "
    f"{LIFT_REFERENCE} for the commit to git show it at).\n"
    "- A new check that fails returns an OBP error message from ErrorMessages with the HTTP code that matches it: "
    "404 for something not found, or not belonging to the bank, account or customer in the path (as if it did not "
    "exist, so nothing is told about other banks' data), 403 for a missing permission, 400 for bad input "
    "(booleanToFuture(<message>, 404, Some(cc)) { ... }).\n"
    "- Every error the endpoint can now return goes in its ResourceDoc's error list. If you changed a ResourceDoc, "
    f"check it against the Lift baseline: python3 {{worktree}}/{PARITY_SCRIPTS[0]} <version, e.g. v3_1_0>. For each "
    f"difference it reports that your change made, print an allowlist entry with python3 {{worktree}}/{PARITY_SCRIPTS[1]} "
    "field-mismatch <version> <endpoint> <field> \"<reason: what was added and why>\", add it to "
    "{worktree}/scripts/resource_doc_baseline/parity_allowlist.json next to the entries of that version, in the "
    "file's own JSON layout, and run the check again until it reports nothing new.\n"
    "- A test is a scenario in the suite of that endpoint. Name suites by fully qualified class name (grep the test "
    "sources for the endpoint or class)."
)

TOOLCHAIN = Toolchain(
    build=maven,
    compile_goal=("test-compile",),
    test_goal="test -DwildcardSuites=<Suite1,Suite2> -DfailIfNoTests=false",
    # the code, and the reviewed Lift/http4s ResourceDoc differences (parity_allowlist.json) a new error needs
    source_dirs=("obp-api/src", "obp-commons/src", "scripts/resource_doc_baseline"),
    scripts=PARITY_SCRIPTS,
    copied=("obp-api/src/main/resources/props/*.props",),
    fix_notes=FIX_NOTES,
)
