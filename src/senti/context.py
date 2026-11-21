"""What every AI run is told about the service it works on: its name, kind, instance, environment and where its
source is, then what Senti knows about that kind of code (the kind's notes) and about this service (its notes in
senti.toml, or a pack's: every OBP app's). Generic agents and prompts say "the service"; this says which."""

from pathlib import Path

from .config import Config
from .kinds import kind_of


def service_context(config: Config) -> str:
    kind = kind_of(config.kind)
    source = str(Path(config.obp_api_source).resolve()) if config.obp_api_source else "not set"
    parts = [f"The service `{config.service}` is of the kind `{kind.name}`: {kind.description} "
             f"This is its instance `{config.name}` in the {config.environment} environment, at "
             f"{config.obp_base_url}{config.monitoring_path}. Its source code is at {source}."]
    if kind.notes:
        parts.append(f"About {kind.name} code:\n{kind.notes}")
    if config.service_notes:
        parts.append(f"About {config.service}:\n{config.service_notes}")
    return "\n\n".join(parts)
