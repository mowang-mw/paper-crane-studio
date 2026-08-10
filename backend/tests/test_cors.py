from __future__ import annotations

from fastapi.testclient import TestClient


def test_visual_selection_put_preflight_is_allowed(client: TestClient) -> None:
    response = client.options(
        "/api/projects/cors-regression-project/visual-selection",
        headers={
            "Origin": "http://127.0.0.1:5173",
            "Access-Control-Request-Method": "PUT",
            "Access-Control-Request-Headers": "content-type",
        },
    )

    assert response.status_code == 200, response.text
    allowed_methods = {
        method.strip()
        for method in response.headers["access-control-allow-methods"].split(",")
    }
    assert "PUT" in allowed_methods
    assert response.headers["access-control-allow-origin"] == "http://127.0.0.1:5173"
