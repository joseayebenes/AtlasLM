import json
import socket
import subprocess
import sys
import time
import urllib.request
from pathlib import Path

import pytest


@pytest.fixture
def fake(tmp_path):
    sock = socket.socket(); sock.bind(("127.0.0.1", 0)); port = sock.getsockname()[1]; sock.close()
    control = tmp_path / "faults.json"; control.write_text("{}")
    proc = subprocess.Popen([sys.executable, str(Path(__file__).parent / "fakes" / "fake_api.py"),
                             "--port", str(port), "--control", str(control)])
    for _ in range(50):
        try:
            urllib.request.urlopen(f"http://127.0.0.1:{port}/", timeout=0.2); break
        except Exception:
            time.sleep(0.1)
    yield f"http://127.0.0.1:{port}/v1", control
    proc.terminate(); proc.wait(timeout=5)


def _service(url):
    from atlas.llm import NetConfig, ServiceConfig
    return ServiceConfig(base_url=url, model="fake", api_key="x", net=NetConfig(proxy="", no_proxy="*"))


def test_chat_and_embeddings_without_a_model(fake):
    url, control = fake
    from atlas.llm import create_client
    cl, _ = create_client(_service(url))
    r = cl.chat.completions.create(model="fake", messages=[{"role": "user", "content": "Reply with: ok"}])
    assert r.choices[0].message.content == "ok"
    e = cl.embeddings.create(model="fake", input=["a", "b"])
    assert len(e.data) == 2 and len(e.data[0].embedding) == 64
    control.write_text(json.dumps({"down": True}))
    with pytest.raises(Exception) as ex:
        cl.chat.completions.create(model="fake", messages=[{"role": "user", "content": "x"}])
    assert "401" in str(ex.value) or "AuthenticationError" in type(ex.value).__name__

