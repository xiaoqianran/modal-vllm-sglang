"""Dispatch HF delivery to a deployed Modal CPU function. Never transfer bytes to PC2."""
from orchestration import model_capabilities

from runtime.archive.config import ASSET_APP_NAME


def archive_from_volume(modal_client, job, record, plan):
    """Only metadata crosses the boundary; remote CPU reads the mounted Volume."""
    kind = model_capabilities.CAPABILITIES[job["uses"]]["output"]
    artifact = record["artifacts"][kind]
    # Integrity/path validation takes place on the Modal CPU worker.
    function = modal_client.Function.from_name(ASSET_APP_NAME, "archive_asset")
    return function.remote(artifact, plan["delivery"], plan["project"])
