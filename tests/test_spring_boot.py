"""The spring-boot kind: Spring Boot's default log layout in its three forms, Java exceptions, request paths, and how
Fix this builds and tests with Maven."""

import pytest

from senti import fixer
from senti.kinds import kind_of
from senti.kinds.spring_boot import maven
from senti.signatures import parse_line, signature_of

BOOT = kind_of("spring-boot")
FORMS = [
    "2026-11-21T08:00:00.123+01:00 ERROR 4242 --- [shop] [nio-8080-exec-1] c.e.shop.CartController : Cart 81 failed",
    "2026-11-21T08:00:00.123+01:00 ERROR 4242 --- [nio-8080-exec-1] c.e.shop.CartController : Cart 81 failed",
    "2026-11-21 08:00:00.123 ERROR 4242 --- [nio-8080-exec-1] c.e.shop.CartController                  : Cart 81 failed",
]


@pytest.mark.parametrize("raw", FORMS)
def test_each_form_of_the_layout_reads_the_same(raw):
    parsed = parse_line(raw, BOOT)
    assert (parsed.thread, parsed.logger, parsed.message) == ("nio-8080-exec-1", "c.e.shop.CartController",
                                                              "Cart 81 failed")
    assert parsed.ts.year == 2026 and parsed.ts.minute == 0


def test_the_three_forms_are_one_signature():
    assert len({signature_of("error", parse_line(raw, BOOT), BOOT).id for raw in FORMS}) == 1


def test_a_java_exception_and_the_request_path_are_found():
    raw = ("2026-11-21T08:00:00.123Z ERROR 1 --- [nio-8080-exec-3] o.a.c.c.C.[.[.[/].[dispatcherServlet] : Servlet "
           "failed for GET /api/v1/carts/81/items/9f2c\njava.lang.IllegalStateException: no stock\n\tat "
           "com.example.shop.CartService.add(CartService.java:42)")
    sig = signature_of("error", parse_line(raw, BOOT), BOOT)
    assert sig.exception == "java.lang.IllegalStateException"
    assert sig.endpoint == "/api/{id}/carts/{id}/items/{id}" and "/api/{id}/carts" in sig.template


def test_a_file_path_is_not_taken_for_a_request():
    raw = "2026-11-21T08:00:00.123Z WARN 1 --- [main] c.e.shop.Config : Could not read /etc/shop/app.yml"
    assert signature_of("warning", parse_line(raw, BOOT), BOOT).endpoint == ""


def test_an_unrecognised_line_is_a_plain_message():
    parsed = parse_line("Exception in thread \"main\" java.lang.OutOfMemoryError: Java heap space", BOOT)
    assert parsed.ts is None and parsed.message.startswith("Exception in thread")


def test_fix_this_builds_with_the_projects_maven_wrapper_if_it_has_one(tmp_path):
    assert maven(str(tmp_path))[0] == "mvn"
    (tmp_path / "mvnw").write_text("#!/bin/sh\n")
    assert maven(str(tmp_path))[:2] == [str(tmp_path / "mvnw"), "-o"]
    text = fixer.prompt({"title": "t", "evidence": "e", "hypothesis": "h", "files": "[]", "suggested_fix": "f"},
                        "/w", BOOT.toolchain)
    assert "4. Compile: mvn -o -q -f /w/pom.xml test-compile" in text and "-Dtest=<Class1,Class2>" in text
    assert "only in /w/src." in text and "ResponseStatusException" in text and "ErrorMessages" not in text


def test_the_analyst_is_told_about_spring_boot_code():
    assert "@RestController" in BOOT.notes and "http.server.requests" in BOOT.notes
