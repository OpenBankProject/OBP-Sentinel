"""What the OBP pack adds to Senti's help page: OBP-API's Glossary item OBP-Sentinel, read from the first OBP-API
instance watched (no token needed), with a link to the item in API Explorer II when the instance's app directory lists
one."""

import json
import urllib.request
from typing import TYPE_CHECKING
from urllib.parse import quote

if TYPE_CHECKING:
    from ...config import Config

GLOSSARY_ITEM = "OBP-Sentinel"
GLOSSARY_PATH = f"/obp/v7.0.0/api/glossary/{GLOSSARY_ITEM}"


def about(configs: list["Config"]) -> dict | None:
    """{"url", "html" or "error", "link"}; None when no OBP-API instance is watched."""
    from ...config import app_url

    obp = next((c for c in configs if c.kind == "obp-api"), None)
    if not obp:
        return None
    url = obp.obp_base_url + GLOSSARY_PATH
    explorer = app_url(obp, "explorer")
    link = f"{explorer}/glossary#{quote(GLOSSARY_ITEM)}" if explorer else None
    try:
        with urllib.request.urlopen(url, timeout=15) as response:
            return {"url": url, "html": json.load(response)["description"]["html"], "link": link}
    except (OSError, ValueError, KeyError, TypeError) as e:
        return {"url": url, "error": str(e)[:300], "link": link}
