"""Build metadata-only archive requests from committed generation records."""
from orchestration import model_capabilities


def archive_request(job, record, plan):
    kind = model_capabilities.CAPABILITIES[job["uses"]]["output"]
    return {"artifact": record["artifacts"][kind], "delivery": plan["delivery"],
            "project": plan["project"]}
