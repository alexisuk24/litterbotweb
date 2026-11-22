"""Calendar page API: the robot in the URL must name a folder inside the clips folder."""
import pytest


@pytest.fixture
def outside(env):
    """An MP4 in the folder above the clips folder, which the API must never list."""
    (env.root / "Other_2026-03-04_05-06-07.mp4").write_bytes(b"x")


def test_dates_and_videos_list_a_robots_clips(env):
    folder = env.clips / "Marvin"
    folder.mkdir()
    (folder / "Marvin_2026-03-04_05-06-07.mp4").write_bytes(b"x")

    assert env.client.get("/api/dates/Marvin").get_json() == ["2026-03-04"]
    videos = env.client.get("/api/videos/Marvin/2026-03-04").get_json()
    assert [v["filename"] for v in videos] == ["Marvin_2026-03-04_05-06-07.mp4"]


@pytest.mark.parametrize("robot", ["..", "%2e%2e", "."])
def test_dates_refuse_folders_outside_a_robot(env, outside, robot):
    resp = env.client.get(f"/api/dates/{robot}")
    assert resp.status_code in (403, 404)
    assert "2026-03-04" not in resp.get_data(as_text=True)


@pytest.mark.parametrize("robot", ["..", "%2e%2e", "."])
def test_videos_refuse_folders_outside_a_robot(env, outside, robot):
    resp = env.client.get(f"/api/videos/{robot}/2026-03-04")
    assert resp.status_code in (403, 404)
    assert "Other_" not in resp.get_data(as_text=True)
