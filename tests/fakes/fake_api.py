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
                                      this with some models); "json_schema" -> 501 only to
                                      json_schema, json_object is accepted
  chat_429_after: N                -> 429 "rate limit" on chat from request N on
  chat_status_once: [500, 503]     -> the next chat requests answer with these statuses, one
                                      each and in order (again if the list changes)
  retry_after: seconds             -> Retry-After header in every rejection (409, 429, statuses)
  ratelimit_headers: {"remaining": 0, "reset": 1}
                                   -> x-ratelimit-remaining / x-ratelimit-reset in every rejection
  tool_call: {"name": "f", "args": {...}}
                                   -> a request with tools gets that tool call; the next one
                                      (the last message is the result of a tool) an answer that
                                      quotes the result
  usage_chars_per_token: 4         -> usage from the real size: prompt_tokens of the request,
                                      completion_tokens of the answer and its reasoning; an
                                      answer longer than max_tokens is cut, finish_reason "length"
  slow: seconds                    -> delay per request
  reasoning_tokens: N              -> reasoning model: spends N tokens reasoning before the
                                      answer; with max_tokens <= N the answer comes back empty.
                                      reasoning_effort = "none" in the request turns it off
  no_stream: true                  -> 400 "stream is not supported" to chat requests with stream;
                                      "ignore" -> they get the whole answer as JSON, without SSE
  chunk_chars: N                   -> characters of content, reasoning or arguments per chunk of a
                                      streamed answer (8 by default)
  slow_between_chunks: seconds     -> delay before each chunk of a streamed answer
  hang_after_chunks: N             -> a streamed answer stops after N chunks and the connection
                                      stays open, without sending anything, until the client closes
                                      it (or hang_seconds, 120 by default)
  hang_requests: N                 -> only the first N streamed answers hang
  hang_keepalive: seconds          -> while it hangs, an SSE comment every so many seconds (what
                                      OpenRouter sends while the model works)
A chat request with "stream": true gets the answer in SSE chunks: the reasoning, the content and
each tool call by its index (the id and the name first, then the arguments in pieces), then
finish_reason, the usage in a last chunk without choices if stream_options.include_usage, and
[DONE].
GET / returns counters (chat, embeddings, emb_texts, rejections, streams; open: the POST requests
being answered now), and the options of the last chat request (without its messages). Sections are analysed with a heuristic (sentences with "shall");
questions with sources ("Sources: ...") get an answer that cites their first source, and
notes ("Notes: ...") one that cites every source of the notes. The rewriting of a question
calls it broad if it says all, every, explain or summarise, and gives the listed documents
it names.
"""
import argparse, json, math, re, hashlib, select, socket, threading, time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
_ap = argparse.ArgumentParser(); _ap.add_argument("--port", type=int, default=8770); _ap.add_argument("--control", default="faults.json")
ARGS = _ap.parse_args()
stats = {"chat": 0, "embeddings": 0, "emb_texts": 0, "streams": 0, "open": 0}
lock = threading.Lock()
once = {"statuses": None, "sent": 0}       # where chat_status_once goes
def ctl():
    try: return json.load(open(ARGS.control))
    except Exception: return {}
def emb(t):
    v = [0.0]*64
    for w in re.findall(r"\w+", t.lower()): v[int(hashlib.md5(w.encode()).hexdigest(), 16) % 64] += 1
    return v
def tokens(text, c):
    return math.ceil(len(text) / c["usage_chars_per_token"]) if c.get("usage_chars_per_token") else 1
class H(BaseHTTPRequestHandler):
    def log_message(self, *a): pass
    def send(self, code, obj, headers=None):
        data = json.dumps(obj).encode()
        self.send_response(code); self.send_header("Content-Type", "application/json")
        for k, v in (headers or {}).items(): self.send_header(k, str(v))
        self.send_header("Content-Length", str(len(data))); self.end_headers(); self.wfile.write(data)
    def reject(self, code, message, kind, c):
        stats["rejections"] = stats.get("rejections", 0) + 1
        headers = {"Retry-After": c["retry_after"]} if c.get("retry_after") is not None else {}
        if c.get("ratelimit_headers"):
            headers.update({f"x-ratelimit-{k}": v for k, v in c["ratelimit_headers"].items()})
        return self.send(code, {"error": {"message": message, "type": kind}}, headers)
    def do_GET(self): self.send(200, stats)
    def do_POST(self):
        with lock: stats["open"] += 1
        try: self.answer()
        except (BrokenPipeError, ConnectionResetError): pass       # the client went away
        finally:
            with lock: stats["open"] -= 1
    def gone(self):
        """Whether the client closed the connection (what is left to read is its end)."""
        try:
            return bool(select.select([self.connection], [], [], 0)[0]) and not self.connection.recv(1, socket.MSG_PEEK)
        except OSError:
            return True
    def chunk(self, obj):
        self.wfile.write(b"data: " + (obj if isinstance(obj, bytes) else json.dumps(obj).encode()) + b"\n\n")
        self.wfile.flush()
    def stream(self, message, finish, usage, body, c):
        """The answer in SSE chunks (see the module)."""
        stats["streams"] += 1; hangs = c.get("hang_after_chunks")
        if c.get("hang_requests") is not None and stats["streams"] > c["hang_requests"]:
            hangs = None
        self.send_response(200); self.send_header("Content-Type", "text/event-stream")
        self.send_header("Cache-Control", "no-cache"); self.end_headers()
        size = c.get("chunk_chars") or 8
        pieces = lambda text: [text[i:i + size] for i in range(0, len(text), size)]
        deltas = [{"role": "assistant", "content": ""}]
        deltas += [{"reasoning": p} for p in pieces(message.get("reasoning") or "")]
        deltas += [{"content": p} for p in pieces(message.get("content") or "")]
        for i, call in enumerate(message.get("tool_calls") or []):
            deltas.append({"tool_calls": [{"index": i, "id": call["id"], "type": "function",
                                           "function": {"name": call["function"]["name"], "arguments": ""}}]})
            deltas += [{"tool_calls": [{"index": i, "function": {"arguments": p}}]}
                       for p in pieces(call["function"]["arguments"])]
        head = {"id": "x", "object": "chat.completion.chunk", "created": 0, "model": "m"}
        chunks = [{**head, "choices": [{"index": 0, "delta": d, "finish_reason": None}]} for d in deltas]
        chunks.append({**head, "choices": [{"index": 0, "delta": {}, "finish_reason": finish}]})
        if (body.get("stream_options") or {}).get("include_usage"):
            chunks.append({**head, "choices": [], "usage": usage})
        for n, obj in enumerate(chunks + [b"[DONE]"]):
            if hangs is not None and n >= hangs:
                return self.hang(c)
            time.sleep(c.get("slow_between_chunks", 0))
            self.chunk(obj)
    def hang(self, c):
        """Sends nothing more (or only SSE comments) until the client closes the connection."""
        end, beat = time.time() + c.get("hang_seconds", 120), time.time()
        while time.time() < end and not self.gone():
            time.sleep(0.05)
            if c.get("hang_keepalive") and time.time() - beat >= c["hang_keepalive"]:
                self.wfile.write(b": OPENROUTER PROCESSING\n\n"); self.wfile.flush(); beat = time.time()
    def answer(self):
        body = json.loads(self.rfile.read(int(self.headers["Content-Length"]))); c = ctl()
        time.sleep(c.get("slow", 0))
        if c.get("down"):
            return self.send(401, {"error": {"message": "invalid api key", "type": "auth"}})
        if self.path.endswith("/embeddings"):
            if c.get("emb_409_after") is not None and stats["embeddings"] >= c["emb_409_after"]:
                return self.reject(409, "rate limit exceeded", "rate_limit", c)
            stats["embeddings"] += 1
            inp = body["input"] if isinstance(body["input"], list) else [body["input"]]
            stats["emb_texts"] += len(inp)
            if any(k in t for t in inp for k in c.get("emb_fail_if", [])):
                return self.send(400, {"error": {"message": "input rejected by gateway", "type": "invalid_request_error"}})
            used = sum(tokens(t, c) for t in inp)
            return self.send(200, {"object": "list", "model": "e", "usage": {"prompt_tokens": used, "total_tokens": used},
                                   "data": [{"object": "embedding", "index": i, "embedding": emb(t)} for i, t in enumerate(inp)]})
        if c.get("chat_429_after") is not None and stats["chat"] >= c["chat_429_after"]:
            return self.reject(429, "rate limit exceeded", "rate_limit", c)
        statuses = c.get("chat_status_once") or None
        if statuses != once["statuses"]:
            once.update(statuses=statuses, sent=0)
        if statuses and once["sent"] < len(statuses):
            once["sent"] += 1
            return self.reject(statuses[once["sent"] - 1], "the upstream server failed", "server_error", c)
        stats["chat"] += 1
        stats["last_chat"] = {k: v for k, v in body.items() if k != "messages"}   # the options of the request
        if body.get("stream") and c.get("no_stream") is True:
            return self.send(400, {"error": {"message": "stream is not supported", "type": "invalid_request_error"}})
        unsupported = c.get("no_structured_output")
        if unsupported and body.get("response_format") and \
                (unsupported is True or body["response_format"].get("type") == unsupported):
            return self.send(501, {"error": {"message": "structured output is unavailable", "type": "api_error"}})
        last = body["messages"][-1]; user = last.get("content") or ""; cut = False
        message = None
        if c.get("tool_call") and last.get("role") == "tool":
            content = f"The tool {c['tool_call']['name']} returned: {user}"
        elif c.get("tool_call") and body.get("tools"):
            call = c["tool_call"]
            content, message = None, {"role": "assistant", "content": None, "tool_calls": [{
                "id": f"call_{stats['chat']}", "type": "function",
                "function": {"name": call["name"], "arguments": json.dumps(call.get("args", {}))}}]}
        elif c.get("prose"):
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
        finish = "tool_calls" if message else "length" if cut else "stop"
        message = message or {"role": "assistant", "content": content}
        spent = 0
        if c.get("reasoning_tokens") and body.get("reasoning_effort") != "none":
            message["reasoning"], spent = "The user wants a short answer. ", c["reasoning_tokens"]
            if body.get("max_tokens") is not None and body["max_tokens"] <= c["reasoning_tokens"]:
                message["content"], finish = "", "length"
        answer = message.get("content") or ""
        if c.get("usage_chars_per_token") and body.get("max_tokens") is not None and answer and \
                spent + tokens(answer, c) > body["max_tokens"]:
            message["content"] = answer[:max(0, body["max_tokens"] - spent) * c["usage_chars_per_token"]]
            finish = "length"
        prompt = tokens(json.dumps(body["messages"]), c)
        completion = spent + tokens(message.get("content") or "", c) if c.get("usage_chars_per_token") else 1
        usage = {"prompt_tokens": prompt, "completion_tokens": completion, "total_tokens": prompt + completion}
        if body.get("stream") and c.get("no_stream") != "ignore":
            return self.stream(message, finish, usage, body, c)
        self.send(200, {"id": "x", "object": "chat.completion", "created": 0, "model": "m",
                        "choices": [{"index": 0, "finish_reason": finish, "message": message}], "usage": usage})
if __name__ == "__main__":
    print(f"fake API at http://127.0.0.1:{ARGS.port}/v1  (control: {ARGS.control})", flush=True)
    ThreadingHTTPServer(("127.0.0.1", ARGS.port), H).serve_forever()
