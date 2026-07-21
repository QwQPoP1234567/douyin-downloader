from app.main import app


def test_video_list_exposes_cursor_query_parameter() -> None:
    parameters = app.openapi()["paths"]["/api/videos"]["get"]["parameters"]

    assert any(parameter["name"] == "cursor" for parameter in parameters)
