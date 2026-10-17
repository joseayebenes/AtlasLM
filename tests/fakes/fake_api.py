#!/usr/bin/env python3
"""Fake OpenAI-compatible server (chat + embeddings) for tests without a model.

    python tests/fakes/fake_api.py --port 8770 --control faults.json

The control file is re-read on every request; keys:
  down: true                       -> 401 on everything (fatal error)
  emb_409_after: N                 -> 409 "rate limit" on embeddings from request N on
  emb_fail_if: ["text"]            -> 400 if any text in the batch contains the string
  section_fail_if: ["text"]        -> prose answer (not JSON) if the section contains the string
  prose: true                      -> prose answer (not JSON) to every chat request
  cut_sections_over: N             -> sections longer than N characters get their JSON cut in
                                      half with finish_reason "length"
  no_structured_output: true       -> 501 to chat requests with response_format (Ollama does
                                      this with some models)
  slow: seconds                    -> delay per request
  reasoning_tokens: N              -> reasoning model: spends N tokens reasoning before the
                                      answer; with max_tokens <= N the answer comes back empty.
                                      reasoning_effort = "none" in the request turns it off
GET / returns counters, and the options of the last chat request (without its messages);
POST /reset puts the counters at zero and forgets the last chat request. Sections are analysed with a heuristic (sentences with "shall");
questions with sources ("Sources: ...") get an answer that cites their first source, and
notes ("Notes: ...") one that cites every source of the notes. The rewriting of a question
calls it broad if it says all, every, explain or summarise, and gives the listed documents
it names.
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
        if self.path == "/reset":
            stats.clear(); stats.update(chat=0, embeddings=0, emb_texts=0)
            return self.send(200, stats)
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
        stats["last_chat"] = {k: v for k, v in body.items() if k != "messages"}   # the options of the request
        if c.get("no_structured_output") and body.get("response_format"):
            return self.send(501, {"error": {"message": "structured output is unavailable", "type": "api_error"}})
        user = body["messages"][-1]["content"]; cut = False
        if c.get("prose"):
            content = "Sorry, I cannot produce JSON."
        elif "<section>" in user:
            sec = user.split("<section>")[-1].split("</section>")[0]; flat = " ".join(sec.split())
            if any(k in flat for k in c.get("section_fail_if", [])):
                content = "Sorry, I cannot produce JSON."
            else:
                reqs = [{"id": (re.search(r"Req: (\S+)", m) or [None, ""])[1], "text": re.sub(r"^Req: \S+ ", "", m.strip())}
                        for m in re.findall(r"(?:Req: \S+ )?[A-Z][^.]*\bshall\b.*?\.(?=\s|$)", flat)]
                content = json.dumps({"summary": f"Section about {flat[:50]}.", "key_points": [], "requirements": reqs,
                                      "definitions": [], "entities": [], "references": [], "open_issues": []})
                if c.get("cut_sections_over") is not None and len(sec) > c["cut_sections_over"]:
                    content, cut = content[:len(content) // 2], True
        elif "Reply with: ok" in user:
            content = "ok"
        elif user.startswith("Sources:"):
            first = re.search(r"^\[(\d+)\]", user, re.M)
            content = "Answer taken from the sources [%s]." % (first[1] if first else 1)
        elif user.startswith("Notes:"):
            content = "Answer combined from the notes " + "".join(dict.fromkeys(re.findall(r"\[\d+\]", user))) + "."
        elif "search queries" in (body["messages"][0]["content"] if body["messages"] else ""):
            question = user.split("Question: ")[-1]
            listed = re.findall(r"^- (.+?): ", user, re.M) if user.startswith("Documents:") else []
            content = json.dumps({"queries": [question[:60]], "keywords": [],
                                  "broad": bool(re.search(r"\b(all|every|explain|summari[sz]e)\b", question, re.I)),
                                  "documents": [n for n in listed if n.lower() in question.lower()]})
        else:
            content = json.dumps({"summary": "Overall.", "purpose": "p", "scope": "s", "main_topics": ["t"]})
        message, finish = {"role": "assistant", "content": content}, "length" if cut else "stop"
        if c.get("reasoning_tokens") and body.get("reasoning_effort") != "none":
            message["reasoning"] = "The user wants a short answer. "
            if body.get("max_tokens") is not None and body["max_tokens"] <= c["reasoning_tokens"]:
                message["content"], finish = "", "length"
        self.send(200, {"id": "x", "object": "chat.completion", "created": 0, "model": "m",
                        "choices": [{"index": 0, "finish_reason": finish, "message": message}],
                        "usage": {"prompt_tokens": 1, "completion_tokens": 1, "total_tokens": 2}})
if __name__ == "__main__":
    print(f"fake API at http://127.0.0.1:{ARGS.port}/v1  (control: {ARGS.control})", flush=True)
    ThreadingHTTPServer(("127.0.0.1", ARGS.port), H).serve_forever()
