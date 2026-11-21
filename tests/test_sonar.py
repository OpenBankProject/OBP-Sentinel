from dataclasses import replace

from senti import sonar
from senti.store import Store

PROJECT = "Ada_OBP-API"


def issue(key, rule, path, line, quality, severity, message="Do this. Because."):
    return {"key": key, "rule": rule, "component": f"{PROJECT}:{path}", "line": line, "message": message,
            "impacts": [{"softwareQuality": quality, "severity": severity}]}


def fake(monkeypatch, issues, hotspots=(), pr_issues=(), pr=None):
    def get(path, params):
        if path == "issues/search":
            found = list(pr_issues) if "pullRequest" in params else list(issues)
            return {"issues": found, "total": len(found)}
        if path == "hotspots/search":
            return {"hotspots": list(hotspots)}
        raise AssertionError(path)
    monkeypatch.setattr(sonar, "_get", get)
    monkeypatch.setattr(sonar, "pull_request", lambda project, source: pr)


MAIN = "obp-api/src/main/scala/code/Api.scala"


def test_keeps_security_and_serious_reliability_outside_tests_one_finding_per_rule_and_file(monkeypatch):
    fake(monkeypatch, [
        issue("a", "s:1", MAIN, 10, "SECURITY", "MEDIUM", "Check this password. More."),
        issue("b", "s:1", MAIN, 20, "SECURITY", "HIGH", "Check this password. More."),
        issue("c", "s:1", "obp-api/src/test/scala/code/ApiTest.scala", 5, "SECURITY", "HIGH"),  # test code
        issue("d", "s:2", "run.sh", 3, "RELIABILITY", "MEDIUM"),  # reliability below high
        issue("e", "s:3", "run.sh", 4, "RELIABILITY", "HIGH", "Use [[. Safer.")],
        hotspots=[{"key": "h", "ruleKey": "s:4", "component": f"{PROJECT}:Dockerfile", "line": 1,
                   "message": "Root user.", "vulnerabilityProbability": "LOW"}])  # low probability
    kept, pr = sonar.fetch(PROJECT)
    made = {f["title"]: f for f in sonar.findings(PROJECT, kept)}
    assert sorted(made) == ["Check this password in Api.scala (2 places)", "Use [[ in run.sh"]
    password = made["Check this password in Api.scala (2 places)"]
    assert (password["category"], password["impact"], password["files"]) == ("security", 4, [f"{MAIN}:10", f"{MAIN}:20"])
    assert "&open=b" in password["evidence"]
    assert made["Use [[ in run.sh"]["impact"] == 3  # outside src/main counts for one less


def test_new_issues_on_the_pull_request_count_from_medium_and_are_new(monkeypatch):
    fake(monkeypatch, [], pr_issues=[issue("p", "s:5", MAIN, 7, "MAINTAINABILITY", "HIGH", "Too complex."),
                                     issue("q", "s:6", MAIN, 8, "MAINTAINABILITY", "LOW")],
         pr={"key": "2968"})
    kept, pr = sonar.fetch(PROJECT)
    made = sonar.findings(PROJECT, kept)
    assert [(f["title"], f["trend"], f["category"]) for f in made] == [("Too complex in Api.scala", "new", "readability")]
    assert "pullRequest=2968" in made[0]["evidence"]


def test_a_finding_sonarcloud_no_longer_reports_is_marked_fixed_unless_dismissed(config, monkeypatch):
    config = replace(config, sonarcloud_project=PROJECT)
    fake(monkeypatch, [issue("a", "s:1", MAIN, 10, "SECURITY", "HIGH"), issue("b", "s:7", MAIN, 11, "SECURITY", "HIGH")])
    assert sonar.check(config) == {"added": 2, "updated": 0, "fixed": 0, "pull_request": None}
    store = Store(config.db_path)
    dismissed = store.db.execute("SELECT id FROM findings WHERE title LIKE 'Do this%' ORDER BY id").fetchall()[1][0]
    store.add_feedback(dismissed, "dismissed", None, 0)
    store.commit()
    store.close()
    fake(monkeypatch, [issue("a", "s:1", MAIN, 12, "SECURITY", "HIGH")])  # moved, still there
    assert sonar.check(config) == {"added": 0, "updated": 1, "fixed": 0, "pull_request": None}
    fake(monkeypatch, [])
    assert sonar.check(config)["fixed"] == 1
    store = Store(config.db_path)
    assert [r[0] for r in store.db.execute("SELECT status FROM findings ORDER BY id")] == ["fixed", "dismissed"]
    store.close()


def test_each_project_is_read_once_into_the_first_instance_naming_it(config):
    a, b, c = (replace(config, name=n, sonarcloud_project=p) for n, p in (("a", ""), ("b", PROJECT), ("c", PROJECT)))
    assert [x.name for x in sonar.instances([a, b, c])] == ["b"]


def test_suggests_the_file_sonarcloud_reads_with_tests_as_tests_and_scripts_left_out(tmp_path, monkeypatch):
    import subprocess
    repo = tmp_path / "api"
    for path, text in {"obp-api/src/main/A.scala": "a", "obp-api/src/test/ATest.scala": "t", "scripts/run.py": "r",
                       "sonar-project.properties": "# why\nsonar.issue.ignore.multicriteria=e1\nsonar.exclusions=docs/**\n"}.items():
        (repo / path).parent.mkdir(parents=True, exist_ok=True)
        (repo / path).write_text(text)
    for args in (["init", "-q"], ["add", "-A"], ["-c", "user.name=x", "-c", "user.email=x@x", "commit", "-qm", "c"]):
        subprocess.run(["git", "-C", str(repo), *args], check=True, capture_output=True)

    def get(path, params):
        if path == "components/tree":
            return {"paging": {"total": 0}}
        if path == "measures/component":
            n = {f"{PROJECT}:obp-api/src/test": 24, f"{PROJECT}:scripts": 7}[params["component"]]
            return {"component": {"measures": [{"metric": "vulnerabilities", "value": str(n)}]}}
        raise AssertionError(path)
    monkeypatch.setattr(sonar, "_get", get)
    s = sonar.suggest(PROJECT, str(repo))
    assert s["reads"] == ".sonarcloud.properties" and not s["ci"]  # no workflow runs a scanner
    assert "sonar.tests=.\nsonar.test.inclusions=**/src/test/**" in s["suggested"]
    assert "sonar.exclusions=docs/**,scripts/**,**/src/test/**" in s["suggested"]  # its own exclusions kept
    assert "# why\nsonar.issue.ignore.multicriteria=e1" in s["suggested"]  # kept, with its comment
    assert [e["counts"]["vulnerabilities"] for e in s["effects"]] == [24, 7]
    assert "reads .sonarcloud.properties, not sonar-project.properties" in s["notes"][0]
