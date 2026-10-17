"""The window of the limiter shared by the processes of the workstation (atlas.llm.connection:
SharedWindow): atlas serve, a long atlas run and atlas develop go to the same gateway, and each
one believed it had the whole quota."""
import hashlib
import json
import os
import subprocess
import sys
import textwrap
import time

import pytest

from atlas.llm.connection import NetConfig, RateLimiter, ServiceConfig, SharedWindow, rate_limiter_for


def test_two_limiters_on_one_file_share_the_window_and_the_pace(tmp_path):
    """Two limiters on the same file are two processes: the sends of both are spaced 60/rpm,
    and a rejection seen by one lowers the pace of the other."""
    path = tmp_path / "gateway-443.json"
    net = NetConfig(requests_per_minute=120)                 # 0.5 s between sends
    a, b = RateLimiter(net, "a", SharedWindow(path)), RateLimiter(net, "b", SharedWindow(path))
    t0 = time.time()
    a.wait(), b.wait(), a.wait()
    assert time.time() - t0 >= 0.95
    state = json.loads(path.read_text())
    assert len(state["stamps"]) == 3 and state["rpm"] == 0      # nobody lowered the pace yet
    a.rejected()
    b.wait()
    assert b.rpm == pytest.approx(84) and json.loads(path.read_text())["rpm"] == pytest.approx(84)
    a.pause(0.6)                                             # a pause of one holds the other
    t0 = time.time()
    b.wait()
    assert time.time() - t0 >= 0.55


def test_the_file_has_no_key_and_is_rewritten_when_broken(tmp_path, monkeypatch, caplog):
    monkeypatch.setenv("ATLAS_LIMITS_DIR", str(tmp_path))
    key = "sk-or-v1-secret-of-the-gateway"
    cfg = ServiceConfig(base_url="https://gw.example:8443/v1", model="m", api_key=key,
                        net=NetConfig(requests_per_minute=600))
    lim = rate_limiter_for(cfg)
    path = tmp_path / "gw.example-8443.json"
    assert lim.shared is not None and lim.shared.path == path
    lim.wait()
    text = path.read_text()
    assert key not in text and hashlib.sha256(key.encode()).hexdigest()[:8] not in text
    assert set(json.loads(text)) <= {"stamps", "tokens", "rpm", "rejections", "last_send", "last_rejection",
                                      "last_increase", "paused_until", "updated"}
    path.write_text("{not json")
    lim.wait()
    assert len(json.loads(path.read_text())["stamps"]) == 1                 # started afresh
    assert "rewritten" in caplog.text
    # a window nobody touched for 10 minutes starts afresh: yesterday's pace does not hold today
    path.write_text(json.dumps({"stamps": [], "rpm": 5, "rejections": 3, "updated": time.time() - 3600}))
    lim.wait()
    assert json.loads(path.read_text())["rpm"] == 0 and lim.rpm == 600
    # without limits, or with shared_limit = false, nothing is shared
    assert rate_limiter_for(ServiceConfig(base_url="https://other.example/v1", api_key=key)).shared is None
    assert rate_limiter_for(ServiceConfig(base_url="https://third.example/v1", api_key=key, net=NetConfig(
        requests_per_minute=600, shared_limit=False))).shared is None


def test_a_window_that_cannot_be_locked_falls_back_to_this_process(tmp_path, caplog):
    blocker = tmp_path / "a-file"
    blocker.write_text("")
    lim = RateLimiter(NetConfig(requests_per_minute=600), "x", SharedWindow(blocker / "x-1.json"))
    lim.wait(), lim.wait()                                   # its folder cannot be made: in memory
    assert lim.shared is None and len(lim.stamps) == 2
    assert "paces on its own" in caplog.text


CLIENT = textwrap.dedent("""
    import sys
    sys.path.insert(0, sys.argv[1])
    from atlas.llm import NetConfig, ServiceConfig, create_client
    client, _ = create_client(ServiceConfig(base_url=sys.argv[2], model="e", api_key="x", net=NetConfig(
        proxy="", no_proxy="*", requests_per_minute=120, shared_limit=sys.argv[3] == "1")))
    for _ in range(3):
        client.embeddings.create(model="e", input=["a"])
""")


@pytest.mark.parametrize("shared", [True, False])
def test_two_processes_share_the_quota(fake, fake_stats, tmp_path, shared):
    """Two real processes, 120 requests per minute each. With the shared window, the limiter
    sends their 6 requests 0.5 s apart (the times it decided, in the window: those of arrival
    at the server also carry the jitter of a loaded machine); without it, there is no window
    and both send at the same time."""
    url, _ = fake
    root = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
    limits = tmp_path / "limits"
    env = {**os.environ, "ATLAS_LIMITS_DIR": str(limits)}
    procs = [subprocess.Popen([sys.executable, "-c", CLIENT, root, url, "1" if shared else "0"], env=env)
             for _ in range(2)]
    assert [p.wait(timeout=120) for p in procs] == [0, 0]
    assert len(fake_stats()["times"]) == 6
    window = limits / f"127.0.0.1-{url.rsplit(':', 1)[1].split('/')[0]}.json"
    if shared:
        sends = sorted(json.loads(window.read_text())["stamps"])
        assert len(sends) == 6 and min(b - a for a, b in zip(sends, sends[1:])) >= 0.49
    else:
        assert not window.exists()
        times = sorted(fake_stats()["times"])
        assert min(b - a for a, b in zip(times, times[1:])) < 0.45     # two series 0.5 s apart, any offset


def test_shared_limit_comes_from_the_network_of_atlas_toml(toml):
    from atlas.config import load_config
    from atlas.llm import from_config
    conn = from_config(load_config(toml('[network]\nrequests_per_minute = 28\n[llm.network]\nshared_limit = false\n')))
    assert conn.llm.net.shared_limit is False and conn.embeddings.net.shared_limit is True
