from obp_sentinel.signatures import normalise_path, parse_line, signature_of


def line(msg: str, ts="2026-10-04 09:12:01+02", logger="code.api.util.APIUtil") -> str:
    return f"[{ts}] [http4s-io-3] [{logger}] {msg}"


def test_parse_line():
    parsed = parse_line(line("Something failed\njava.lang.IllegalStateException: boom"))
    assert parsed.logger == "code.api.util.APIUtil"
    assert parsed.thread == "http4s-io-3"
    assert parsed.message == "Something failed"
    assert parsed.detail == "java.lang.IllegalStateException: boom"
    assert parsed.ts.isoformat() == "2026-10-04T09:12:01+02:00"


def test_parse_line_utc_and_unparsed():
    assert parse_line(line("x", ts="2026-10-04 07:12:01Z")).ts.utcoffset().total_seconds() == 0
    parsed = parse_line("no brackets here")
    assert parsed.ts is None and parsed.message == "no brackets here"


def test_same_problem_different_ids_is_one_signature():
    a = signature_of("ERROR", parse_line(line(
        "Account not found for BANK_ID 'gh.29.uk' ACCOUNT_ID 'a1b2c3' at /obp/v5.1.0/banks/gh.29.uk/accounts/a1b2c3/owner/account"
    )))
    b = signature_of("ERROR", parse_line(line(
        "Account not found for BANK_ID 'rbs.12' ACCOUNT_ID 'zz99' at /obp/v5.1.0/banks/rbs.12/accounts/zz99/owner/account",
        ts="2026-10-04 11:00:00+02",
    )))
    assert a.id == b.id
    assert a.endpoint == "/obp/v5.1.0/banks/{id}/accounts/{id}/owner/account"


def test_normalises_uuids_numbers_emails_ips():
    sig = signature_of("warning", parse_line(line(
        "User 9b2f6e2a-1c3d-4e5f-8a9b-0c1d2e3f4a5b (bob@example.com) from 10.0.0.12 took 1234 ms"
    )))
    assert sig.template == "User <uuid> (<email>) from <ip> took <n> ms"


def test_different_loggers_or_levels_are_different_signatures():
    a = signature_of("error", parse_line(line("Timeout")))
    b = signature_of("error", parse_line(line("Timeout", logger="code.bankconnectors.Connector")))
    c = signature_of("warning", parse_line(line("Timeout")))
    assert len({a.id, b.id, c.id}) == 3


def test_exception_from_detail_line():
    sig = signature_of("error", parse_line(line("Failed\njava.sql.SQLTransientConnectionException: pool exhausted")))
    assert sig.exception == "java.sql.SQLTransientConnectionException"


def test_normalise_path_keeps_literals_and_version():
    assert normalise_path("/obp/v7.0.0/management/telemetry") == "/obp/v7.0.0/management/telemetry"
    assert normalise_path("/obp/v5.1.0/users/user_id/9b2f6e2a") == "/obp/v5.1.0/users/{id}/{id}"
