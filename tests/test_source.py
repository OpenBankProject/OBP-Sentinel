import subprocess
from dataclasses import replace

import pytest

from obp_sentinel.source import REACH_FORMAT, Reviews, fetch_reach, fingerprint, handler_files, next_endpoints, tier_of
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
    mark = fingerprint(str(repo), first, V6, "OBPv6.0.0-getBank")
    assert mark.startswith("code:")
    other = commit(repo, {V6: endpoints_file("bank").replace("banks", "allBanks")})
    assert fingerprint(str(repo), other, V6, "OBPv6.0.0-getBank") == mark
    changed = commit(repo, {V6: endpoints_file("bank(id)")})
    assert fingerprint(str(repo), changed, V6, "OBPv6.0.0-getBank") != mark


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

    reviews.record("OBPv6.0.0-getBank", c, {V6: fingerprint(str(repo), c, V6, "OBPv6.0.0-getBank"),
                                             UTIL: fingerprint(str(repo), c, UTIL, "OBPv6.0.0-getBank")})
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
