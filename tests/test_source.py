import json
import subprocess
from dataclasses import replace

import pytest

from obp_sentinel.cli import seen_line
from obp_sentinel.source import (BIG_FILE_LINES, REACH_FORMAT, Reviews, fetch_reach, function_code, handler_files, mark_of,
                                 next_endpoints, tier_of)
from obp_sentinel.store import Store

V6 = "obp-api/src/main/scala/code/api/v6_0_0/Http4s600.scala"
V5 = "obp-api/src/main/scala/code/api/v5_1_0/Http4s510.scala"
UTIL = "obp-api/src/main/scala/code/api/util/Util.scala"


def endpoints_file(get_bank_body: str) -> str:
    return f"""object Http4s600 {{
  lazy val getBank: HttpRoutes[IO] = HttpRoutes.of[IO] {{
    {get_bank_body}
  }}
  resourceDocs += ResourceDoc(nameOf(getBank))

  lazy val getBanks: HttpRoutes[IO] = HttpRoutes.of[IO] {{
    banks
  }}
  resourceDocs += ResourceDoc(nameOf(getBanks))
}}
"""


def git(repo, *args) -> str:
    return subprocess.run(["git", "-C", str(repo), *args], check=True, capture_output=True, text=True).stdout.strip()


def commit(repo, files: dict[str, str]) -> str:
    for path, text in files.items():
        (repo / path).parent.mkdir(parents=True, exist_ok=True)
        (repo / path).write_text(text)
    git(repo, "add", "-A")
    git(repo, "-c", "user.name=t", "-c", "user.email=t@t", "commit", "-q", "-m", "c")
    return git(repo, "rev-parse", "HEAD")


@pytest.fixture
def repo(tmp_path):
    r = tmp_path / "OBP-API"
    r.mkdir()
    git(r, "init", "-q")
    return r


@pytest.fixture
def instance(config, repo, tmp_path):
    return replace(config, obp_api_source=str(repo), source_db_path=str(tmp_path / "source.db"))


def set_reach(config, git_commit, endpoints, roles):
    store = Store(config.db_path)
    endpoints = {op: {"roles": roles_needed, "login": bool(roles_needed) or op.endswith("Mine")}
                 for op, roles_needed in endpoints.items()}
    store.set_state("reach", {"format": REACH_FORMAT, "ts": 1, "git_commit": git_commit, "endpoints": endpoints,
                              "roles": roles})
    store.commit()
    store.close()


def test_tiers_no_login_then_login_then_role_held_then_unknown_then_nobody():
    assert tier_of({"roles": [], "login": False}, []) == 0
    assert tier_of({"roles": [], "login": True}, []) == 1
    assert tier_of({"roles": ["CanX", "CanY"], "login": True}, ["CanY"]) == 2
    assert tier_of({"roles": ["CanX"], "login": True}, None) == 3
    assert tier_of({"roles": ["CanX"], "login": True}, ["CanY"]) == 4  # nobody holds its Role: last


def test_handler_file_is_the_endpoints_own_version(repo):
    c = commit(repo, {V6: "nameOf(root)", V5: "nameOf(root)"})
    assert handler_files(str(repo), c, "OBPv5.1.0-root") == [V5]


def test_only_the_endpoints_own_code_counts_in_its_file(repo):
    first = commit(repo, {V6: endpoints_file("bank")})
    mark = mark_of(str(repo), first, V6, "OBPv6.0.0-getBank")[0]
    assert mark.startswith("code:")
    other = commit(repo, {V6: endpoints_file("bank").replace("banks", "allBanks")})
    assert mark_of(str(repo), other, V6, "OBPv6.0.0-getBank")[0] == mark
    changed = commit(repo, {V6: endpoints_file("bank(id)")})
    assert mark_of(str(repo), changed, V6, "OBPv6.0.0-getBank")[0] != mark


def test_queue_order_and_skipping_what_was_reviewed(instance, repo):
    c = commit(repo, {V6: endpoints_file("bank"), UTIL: "util"})
    set_reach(instance, c,
              {"OBPv6.0.0-getBank": ["CanGetBank"], "OBPv6.0.0-getBanks": [], "OBPv6.0.0-secret": ["CanNobody"],
               "OBPv6.0.0-held": ["CanHeld"], "OBPv6.0.0-getBanksMine": []},
              ["CanHeld", "CanGetBank"])
    reviews = Reviews(instance.source_db_path)
    picked, total, reviewed = next_endpoints([instance], reviews, 10)
    assert [e["operation_id"] for e in picked] == [
        "OBPv6.0.0-getBanks", "OBPv6.0.0-getBanksMine", "OBPv6.0.0-getBank", "OBPv6.0.0-held", "OBPv6.0.0-secret"]
    assert (total, reviewed) == (5, 0)
    assert picked[2]["handlers"] == [V6] and picked[2]["read_at"] == c

    reviews.record("OBPv6.0.0-getBank", c, {V6: mark_of(str(repo), c, V6, "OBPv6.0.0-getBank")[0],
                                             UTIL: mark_of(str(repo), c, UTIL, "OBPv6.0.0-getBank")[0]})
    assert reviews.file(git(repo, "rev-parse", f"{c}:{UTIL}"))  # a supporting file is reviewed as a whole
    assert reviews.last_review_of(V6) is None  # the endpoint's own file is not: only its code was read
    picked, _, reviewed = next_endpoints([instance], reviews, 10)
    assert reviewed == 1 and picked[0]["operation_id"] == "OBPv6.0.0-getBanks"

    later = commit(repo, {UTIL: "util changed"})
    set_reach(instance, later, {"OBPv6.0.0-getBank": ["CanGetBank"]}, ["CanGetBank"])
    picked, _, reviewed = next_endpoints([instance], reviews, 10)
    assert reviewed == 0 and picked[0]["changed"] == [UTIL]


def test_reach_lists_active_static_endpoints_and_survives_an_instance_without_reachable_roles():
    class Client:
        def api_versions(self):
            return [{"fully_qualified_version": "OBPv6.0.0", "api_short_version": "v6.0.0", "is_active": True},
                    {"fully_qualified_version": "OBPv5.0.0", "api_short_version": "v5.0.0", "is_active": False},
                    {"fully_qualified_version": "OBPdynamic-entity", "api_short_version": "dynamic-entity", "is_active": True}]

        def resource_docs(self, version):
            assert version == "OBPv6.0.0"
            return [{"operation_id": "OBPv6.0.0-getBank", "roles": [{"role": "CanB"}, {"role": "CanA"}],
                     "error_response_bodies": ["OBP-20001: User not logged in."]},
                    {"operation_id": "OBPv6.0.0-getBanks", "error_response_bodies": ["OBP-50000: Unknown Error."]},
                    {"operation_id": "OBPv7.0.0-getCurrentConsumerScopes",
                     "error_response_bodies": ["OBP-20200: The application cannot be identified."]}]

        def reachable_roles(self):
            raise RuntimeError("404")

    reach = fetch_reach(Client(), "abc")
    assert reach["endpoints"] == {"OBPv6.0.0-getBank": {"roles": ["CanA", "CanB"], "login": True},
                                  "OBPv6.0.0-getBanks": {"roles": [], "login": False},
                                  "OBPv7.0.0-getCurrentConsumerScopes": {"roles": [], "login": True}}
    assert reach["roles"] is None and reach["git_commit"] == "abc"
    assert reach["versions"] == ["OBPv6.0.0"]  # active ones, dynamic left out


BIG = "obp-api/src/main/scala/code/api/util/NewStyle.scala"


def new_style(get_bank: str) -> str:
    padding = "\n".join(f"  // line {i}" for i in range(BIG_FILE_LINES))
    return f"""object NewStyle {{
  object function {{
    def getBank(bankId: BankId,
                cc: Option[CallContext]
               ): Future[Bank] = {{
      {get_bank}
    }}

    override def getBanks(cc: Option[CallContext]): Future[List[Bank]] =
      banks()
    def getBank(bankId: String): Future[Bank] = getBank(BankId(bankId), None)
  }}
{padding}
}}
"""


def test_a_function_is_its_definitions_overloads_included():
    code = function_code(new_style("connector.getBank(bankId)"), "getBank")
    assert code[0].strip().startswith("def getBank(bankId: BankId,") and "               ): Future[Bank] = {" in code
    assert any("connector.getBank" in line for line in code) and code[-1].strip().startswith("def getBank(bankId: String)")
    assert not any("getBanks" in line for line in code)
    assert function_code(new_style("x"), "getBanks") == ["    override def getBanks(cc: Option[CallContext]): Future[List[Bank]] =",
                                                       "      banks()"]
    assert function_code(new_style("x"), "nope") is None


def test_a_big_file_is_recorded_per_function(repo, instance, capsys):
    c = commit(repo, {BIG: new_style("connector.getBank(bankId)"), UTIL: "util"})
    mark, problem = mark_of(str(repo), c, BIG, "OBPv6.0.0-getBank")
    assert mark is None and "#name" in problem  # a whole big file cannot be recorded
    assert mark_of(str(repo), c, f"{BIG}#nope")[1] == f"{BIG} has no def or val nope"

    reviews = Reviews(instance.source_db_path)
    reviews.record("OBPv6.0.0-getBank", c, {f"{BIG}#getBank": mark_of(str(repo), c, f"{BIG}#getBank")[0],
                                             UTIL: mark_of(str(repo), c, UTIL)[0]})
    assert reviews.counts() == {"files": 1, "functions": 1}
    assert "reviewed, unchanged (for OBPv6.0.0-getBank)" in seen_line(str(repo), c, reviews, f"{BIG}#getBank")
    assert seen_line(str(repo), c, reviews, f"{BIG}#getBanks").endswith("not reviewed")
    assert seen_line(str(repo), c, reviews, BIG).endswith("Reviewed in it so far: getBank (unchanged)")

    other = commit(repo, {BIG: new_style("connector.getBank(bankId)").replace("banks()", "allBanks()")})  # elsewhere
    assert "reviewed, unchanged" in seen_line(str(repo), other, reviews, f"{BIG}#getBank")
    changed = commit(repo, {BIG: new_style("connector.getBank(bankId, cc)")})
    assert "changed since its review" in seen_line(str(repo), changed, reviews, f"{BIG}#getBank")
    assert seen_line(str(repo), changed, reviews, BIG).endswith("getBank (changed)")


def test_reviews_are_written_as_text_and_read_back_into_an_empty_database(tmp_path):
    from obp_sentinel.source import export_reviews, import_reviews

    reviews = Reviews(str(tmp_path / "a.db"))
    reviews.record("OBPv6.0.0-getBanks", "c1", {"x/Big.scala#getBank": "code:aa", "x/Small.scala": "b1"})
    reviews.record("OBPv6.0.0-getBank", "c1", {"x/V6.scala": "code:bb", "x/Small.scala": "b1"})
    export_reviews(reviews, str(tmp_path / "review"))
    endpoints = (tmp_path / "review/endpoints.jsonl").read_text().splitlines()
    units = (tmp_path / "review/units.jsonl").read_text().splitlines()
    assert [json.loads(line)["operation_id"] for line in endpoints] == ["OBPv6.0.0-getBank", "OBPv6.0.0-getBanks"]
    assert [json.loads(line)["unit"] for line in units] == ["x/Big.scala#getBank", "x/Small.scala"]  # sorted
    assert json.loads(units[1])["for"] == "OBPv6.0.0-getBanks"  # where it was first read

    fresh = Reviews(str(tmp_path / "b.db"))
    assert import_reviews(fresh, str(tmp_path / "review")) == 4
    assert fresh.counts() == {"files": 1, "functions": 1} and fresh.endpoint("OBPv6.0.0-getBank")["git_commit"] == "c1"
    assert import_reviews(fresh, str(tmp_path / "review")) == 0  # nothing new the second time
