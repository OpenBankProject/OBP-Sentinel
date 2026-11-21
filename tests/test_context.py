"""Every AI run is told which service it works on, and what Senti knows about its kind of code and about it."""

from dataclasses import replace

from senti.analyst import command
from senti.context import service_context
from senti.packs.obp import APP_NOTES, LIFT_REFERENCE
from senti.store import Store
from senti.summary import build_summary, to_markdown


def portal(config):
    return replace(config, name="local-portal", service="portal", kind="sveltekit", environment="local",
                   monitoring_path="/monitoring", obp_base_url="http://localhost:5174", service_notes=APP_NOTES)


def test_obp_api_is_described_with_its_kinds_notes(config):
    text = service_context(replace(config, environment="production", obp_api_source="/src/OBP-API"))
    assert "The service `obp-api` is of the kind `obp-api`" in text and "in the production environment" in text
    assert "Its source code is at /src/OBP-API." in text
    assert str(LIFT_REFERENCE) in text and "obp-api/src/main/scala" in text
    assert "About obp-api:" not in text  # no notes of its own


def test_an_obp_app_gets_its_kinds_notes_and_the_obp_app_notes(config):
    text = service_context(portal(config))
    assert "The service `portal` is of the kind `sveltekit`" in text and "+page.server.ts" in text
    assert "About portal:\nThis is an OBP app" in text
    assert "Lift" not in text  # OBP-API's notes are for OBP-API only


def test_the_analyst_is_told_about_the_service(config, tmp_path):
    prompt = command(replace(config, obp_api_source=str(tmp_path)))[2]
    assert "Run the Senti analysis of the instance `default`" in prompt and "About obp-api code:" in prompt


def test_the_summary_names_the_service_and_leaves_out_metrics_it_does_not_have(config):
    store = Store(config.db_path)
    obp_api = to_markdown(build_summary(store, config, 6))
    app = to_markdown(build_summary(store, portal(config), 6))
    store.close()
    assert "The service `obp-api` (obp-api), in the default environment." in obp_api
    assert "## Calls and response times" in obp_api
    assert "The service `portal` (sveltekit), in the local environment. Its source offers no metrics" in app
    assert "## Calls and response times" not in app and "copied from portal's logs" in app
    assert "their responses on every sveltekit instance" in app
