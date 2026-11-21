"""A Java or Kotlin service on Spring Boot, with Actuator and Micrometer, built with Maven.

Spring Boot's default log layout (Logback), in its three forms:

    2026-11-21T08:00:00.123+01:00 ERROR 4242 --- [shop] [nio-8080-exec-1] c.e.shop.CartController : message   (3.2+)
    2026-11-21T08:00:00.123+01:00 ERROR 4242 --- [nio-8080-exec-1] c.e.shop.CartController : message          (3.0, 3.1)
    2026-11-21 08:00:00.123 ERROR 4242 --- [nio-8080-exec-1] c.e.shop.CartController : message                  (2.x)

An exception follows its message, with `\\tat ...` stack lines. A request path is the one after an HTTP method or
`uri=`, as Spring and its filters write them; other slashes (file paths) are left alone.
"""

import re
from pathlib import Path

from . import NOTES, Kind, LogFormat, Toolchain, notes

LOG_FORMAT = LogFormat(
    line=re.compile(
        r"^(?P<ts>\d{4}-\d{2}-\d{2}[T ]\d{2}:\d{2}:\d{2}(?:[.,]\d+)?(?:Z|[+-]\d{2}:?\d{2})?)\s+"
        r"(?P<level>[A-Z]+)\s+\d+\s+---\s+(?:\[[^\]]*\]\s+)?\[\s*(?P<thread>[^\]]*?)\s*\]\s+"
        r"(?P<logger>\S+)\s*:\s?(?P<body>.*)$", re.S),
    exceptions=(re.compile(r"\b((?:[a-zA-Z_$][\w$]*\.)+[A-Z][\w$]*(?:Exception|Error|Throwable)[\w$]*)"),),
    path=re.compile(r"(?:(?<=GET )|(?<=PUT )|(?<=POST )|(?<=HEAD )|(?<=PATCH )|(?<=DELETE )|(?<=OPTIONS )|(?<=uri=)"
                    r"|(?<=URI \[))/[^\s\"'?,;)\]]*"),
    path_kept=1,
)


def maven(worktree: str) -> list[str]:
    """The project's Maven wrapper if it has one, else mvn; offline, so a fix fetches nothing."""
    wrapper = Path(worktree) / "mvnw"
    return [str(wrapper) if wrapper.exists() else "mvn", "-o", "-q", "-f", f"{worktree}/pom.xml"]


TOOLCHAIN = Toolchain(
    build=maven,
    compile_goal=("test-compile",),
    test_goal="test -Dtest=<Class1,Class2> -Dsurefire.failIfNoSpecifiedTests=false",
    source_dirs=("src",),
    fix_notes=(
        "- A new check that fails answers with the HTTP status that matches it, in the way this code already does "
        "(a ResponseStatusException, an @ExceptionHandler or a ProblemDetail): 404 for something not found or not the "
        "caller's, 403 for a missing permission, 400 for bad input.\n"
        "- A test goes next to the tests of that code (src/test), in the style they use (@WebMvcTest, @SpringBootTest "
        "with MockMvc or WebTestClient). Name test classes by simple name to run them."
    ),
)

KIND = Kind("spring-boot", __doc__.split("\n")[0], LOG_FORMAT, notes(NOTES / "spring-boot.md"), toolchain=TOOLCHAIN)
