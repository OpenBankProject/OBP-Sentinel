"""The collector polls whatever an instance's source offers, and nothing else; kinds say how to read log lines."""

from dataclasses import replace

import pytest

from senti.collector import SECURED, Collector
from senti.kinds import KINDS, kind_of
from senti.sources import LOGS, TELEMETRY, Source, source_for
from senti.packs.obp.api_source import ObpSource
from senti.packs.obp.app_source import ObpAppSource
from senti.store import Store


class LogsOnly(Source):
    """A source with nothing but log lines, and no token to check."""

    name = "logs-only"
    offers = frozenset({LOGS})

    def logs(self, level, limit):
        return ["[2026-10-04 09:00:00Z] [t] [code.X] Boom"] if level == "error" else []


def test_the_collector_polls_only_what_the_source_offers(config, tmp_path):
    store = Store(str(tmp_path / "s.db"))
    Collector(replace(config, log_levels=["error", "warning"]), store, LogsOnly(config)).poll_once()
    assert [r["source"] for r in store.db.execute("SELECT source FROM polls ORDER BY id")] == ["log:error", "log:warning"]
    assert store.db.execute("SELECT COUNT(*) FROM signatures").fetchone()[0] == 1
    assert store.get_state(SECURED) is None  # no paths to check without a token
    assert store.get_state("root") is None


def test_a_telemetry_source_without_a_commit_of_its_own_gives_it_in_the_telemetry(config, tmp_path):
    class TelemetryOnly(Source):
        offers = frozenset({TELEMETRY})

        def telemetry(self):
            return {"git_commit": "abc1234", "meters": []}

    store = Store(str(tmp_path / "s.db"))
    Collector(config, store, TelemetryOnly(config)).poll_once()
    assert store.db.execute("SELECT git_commit FROM deployments").fetchone()[0] == "abc1234"


def test_obp_api_and_its_apps_have_their_own_sources(config):
    assert type(source_for(config)) is ObpSource
    app = replace(config, monitoring_path="/monitoring", kind="sveltekit")
    assert type(source_for(app)) is ObpAppSource
    assert ObpSource.offers > ObpAppSource.offers == {"logs", "telemetry"}


def test_every_obp_kind_reads_lines_the_same_way_so_signature_ids_stay_comparable():
    assert {k.log_format for k in KINDS.values()} == {kind_of("obp-api").log_format}


def test_an_unknown_kind_is_explained():
    with pytest.raises(ValueError, match="does not know the kind 'cobol'"):
        kind_of("cobol")
