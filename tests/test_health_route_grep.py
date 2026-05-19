"""Sprint-1 baseline: the Dockerfile healthcheck path must exist as a route in source.

dashboard_api.py is too heavy to import in a unit test (lifespan kicks off
background tasks, opens Redis, etc.). Grep the source instead.
"""
import re
from pathlib import Path

import pytest

CORE = Path(__file__).resolve().parent.parent
DASHBOARD_API = CORE / "dashboard_api.py"
DOCKERFILE = CORE / "Dockerfile"


@pytest.mark.smoke
def test_health_endpoint_defined():
    text = DASHBOARD_API.read_text()
    assert re.search(r'@app\.get\("/health"\)', text), "/health endpoint missing"


@pytest.mark.smoke
def test_api_health_endpoint_defined():
    text = DASHBOARD_API.read_text()
    assert re.search(r'@app\.get\("/api/health"\)', text), "/api/health endpoint missing"


@pytest.mark.smoke
def test_dockerfile_healthcheck_path_matches_a_real_route():
    """The Dockerfile HEALTHCHECK must hit a path that exists in dashboard_api.py."""
    dockerfile = DOCKERFILE.read_text()
    m = re.search(r'curl\s+-f\s+http://[^/]+(/\S+?)(?:\s|"|\|)', dockerfile)
    assert m, f"could not parse HEALTHCHECK URL from Dockerfile"
    path = m.group(1)
    api_source = DASHBOARD_API.read_text()
    assert re.search(rf'@app\.get\("{re.escape(path)}"\)', api_source), (
        f"Dockerfile healthcheck path {path!r} has no matching @app.get in dashboard_api.py"
    )
