#!/usr/bin/env python3
"""Fake OpenAI-compatible server (chat + embeddings) for tests without a model.

    python tests/fakes/fake_api.py --port 8770 --control faults.json

The control file is re-read on every request; keys:
  down: true                       -> 401 on everything (fatal error)
  emb_409_after: N                 -> 409 "rate limit" on embeddings from request N on
  emb_fail_if: ["text"]            -> 400 if any text in the batch contains the string
  section_fail_if: ["text"]        -> prose answer (not JSON) if the section contains the string
  slow: seconds                    -> delay per request
GET / returns counters. Sections are analysed with a heuristic (sentences with "shall").
"""
import argparse, json, re, hashlib, os
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
_ap = argparse.ArgumentParser(); _ap.add_argument("--port", type=int, default=8770); _ap.add_argument("--control", default="faults.json")
ARGS = _ap.parse_args()
stats = {"chat": 0, "embeddings": 0, "emb_texts": 0}
def ctl():
    try: return json.load(open(ARGS.control))
    except Exception: return {}
def emb(t):
    v = [0.0]*64
    for w in re.findall(r"\w+", t.lower()): v[int(hashlib.md5(w.encode()).hexdigest(), 16) % 64] += 1
    return v
class H(BaseHTTPRequestHandler):
    def log_message(self, *a): pass
    def send(self, code, obj):
        data = json.dumps(obj).encode()
        self.send_response(code); self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(data))); self.end_headers(); self.wfile.write(data)
    def do_GET(self): self.send(200, stats)
    def do_POST(self):
        body = json.loads(self.rfile.read(int(self.headers["Content-Length"]))); c = ctl()
        import time; time.sleep(c.get("slow", 0))
        if c.get("down"):
            return self.send(401, {"error": {"message": "invalid api key", "type": "auth"}})
        if self.path.endswith("/embeddings"):
            if c.get("emb_409_after") is not None and stats["embeddings"] >= c["emb_409_after"]:
                stats["rejections"] = stats.get("rejections", 0) + 1
                return self.send(409, {"error": {"message": "rate limit exceeded", "type": "rate_limit"}})
            stats["embeddings"] += 1
            inp = body["input"] if isinstance(body["input"], list) else [body["input"]]
            stats["emb_texts"] += len(inp)
            if any(k in t for t in inp for k in c.get("emb_fail_if", [])):
                return self.send(400, {"error": {"message": "input rejected by gateway", "type": "invalid_request_error"}})
            return self.send(200, {"object": "list", "model": "e", "usage": {"prompt_tokens": 1, "total_tokens": 1},
                                   "data": [{"object": "embedding", "index": i, "embedding": emb(t)} for i, t in enumerate(inp)]})
        stats["chat"] += 1
        user = body["messages"][-1]["content"]
        if "<section>" in user:
            sec = user.split("<section>")[-1].split("</section>")[0]; flat = " ".join(sec.split())
            if any(k in flat for k in c.get("section_fail_if", [])):
                content = "Sorry, I cannot produce JSON."
            else:
                reqs = [{"id": (re.search(r"Req: (\S+)", m) or [None, ""])[1], "text": re.sub(r"^Req: \S+ ", "", m.strip())}
                        for m in re.findall(r"(?:Req: \S+ )?[A-Z][^.]*\bshall\b[^.]*\.", flat)]
                content = json.dumps({"summary": f"Section about {flat[:50]}.", "key_points": [], "requirements": reqs,
                                      "definitions": [], "entities": [], "references": [], "open_issues": []})
        elif "Reply with: ok" in user:
            content = "ok"
        elif "search queries" in (body["messages"][0]["content"] if body["messages"] else ""):
            content = json.dumps({"queries": [user[:60]], "keywords": []})
        else:
            content = json.dumps({"summary": "Overall.", "purpose": "p", "scope": "s", "main_topics": ["t"]})
        message, finish = {"role": "assistant", "content": content}, "stop"
        self.send(200, {"id": "x", "object": "chat.completion", "created": 0, "model": "m",
                        "choices": [{"index": 0, "finish_reason": finish, "message": message}],
                        "usage": {"prompt_tokens": 1, "completion_tokens": 1, "total_tokens": 2}})
if __name__ == "__main__":
    print(f"fake API at http://127.0.0.1:{ARGS.port}/v1  (control: {ARGS.control})", flush=True)
    ThreadingHTTPServer(("127.0.0.1", ARGS.port), H).serve_forever()
