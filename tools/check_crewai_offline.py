"""Check that CrewAI makes no network requests except to the local fake model server.

    python tools/check_crewai_offline.py            # with the switches main.py sets
    python tools/check_crewai_offline.py --control  # without them: shows the check can see traffic

It runs a one-agent Crew inside a Flow, the way the extraction step will, against a
fake OpenAI-compatible server on 127.0.0.1. Every hostname lookup and connection to
anything else is recorded and refused, so nothing leaves the computer in either mode.
Exit status 0 means no outside connection was attempted and no trace prompt appeared.
"""

import argparse
import io
import json
import os
import socket
import sys
import threading
from contextlib import redirect_stderr, redirect_stdout
from http.server import BaseHTTPRequestHandler, HTTPServer
from pathlib import Path

LOCAL = {"127.0.0.1", "localhost", "::1"}
attempts: list[str] = []


def block_outside_network() -> None:
    real_getaddrinfo, real_connect = socket.getaddrinfo, socket.socket.connect

    def getaddrinfo(host, *args, **kwargs):
        if str(host) not in LOCAL:
            attempts.append(f"dns {host}")
            raise socket.gaierror(f"blocked by check_crewai_offline: {host}")
        return real_getaddrinfo(host, *args, **kwargs)

    def connect(self, address):
        if isinstance(address, tuple) and str(address[0]) not in LOCAL:
            attempts.append(f"connect {address[0]}")
            raise ConnectionRefusedError(f"blocked by check_crewai_offline: {address[0]}")
        return real_connect(self, address)

    socket.getaddrinfo = getaddrinfo
    socket.socket.connect = connect


class FakeModel(BaseHTTPRequestHandler):
    def do_POST(self):
        self.rfile.read(int(self.headers.get("Content-Length", 0)))
        body = json.dumps(
            {
                "id": "x",
                "object": "chat.completion",
                "created": 0,
                "model": "fake",
                "choices": [
                    {
                        "index": 0,
                        "finish_reason": "stop",
                        "message": {"role": "assistant", "content": "Final Answer: OK"},
                    }
                ],
                "usage": {"prompt_tokens": 5, "completion_tokens": 3, "total_tokens": 8},
            }
        ).encode()
        self.send_response(200)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def log_message(self, *args):
        pass


def run_crew(base_url: str) -> str:
    from crewai import LLM, Agent, Crew, Task
    from crewai.flow.flow import Flow, start

    llm = LLM(model="openrouter/anthropic/claude-haiku-5.5", base_url=base_url, api_key="fake")

    class Check(Flow):
        @start()
        def extract(self):
            agent = Agent(role="Reader", goal="Say OK", backstory="A test agent.", llm=llm)
            task = Task(description="Say OK.", expected_output="OK", agent=agent)
            return Crew(agents=[agent], tasks=[task]).kickoff().raw

    answer = Check().kickoff()
    # Telemetry exports in batches and at exit; flush now so any export happens
    # while the network is still being watched.
    from crewai.telemetry import Telemetry

    Telemetry()._shutdown()
    return answer


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--control", action="store_true", help="leave telemetry and tracing on")
    args = parser.parse_args()

    if args.control:
        for name in (
            "CREWAI_DISABLE_TELEMETRY",
            "OTEL_SDK_DISABLED",
            "CREWAI_TRACING_ENABLED",
            "CREWAI_DISABLE_VERSION_CHECK",
        ):
            os.environ.pop(name, None)
    else:
        sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
        import main as cad_main  # sets the switches, same as every real run

        assert all(os.environ[k] == v for k, v in cad_main.CREWAI_OFF.items())

    block_outside_network()
    server = HTTPServer(("127.0.0.1", 0), FakeModel)
    threading.Thread(target=server.serve_forever, daemon=True).start()
    out = io.StringIO()
    try:
        with redirect_stdout(out), redirect_stderr(out):
            answer = run_crew(f"http://127.0.0.1:{server.server_port}")
    except Exception as e:  # report, but still show what was attempted
        answer = f"ERROR {type(e).__name__}: {e}"
    finally:
        server.shutdown()

    text = out.getvalue()
    prompts = [
        line.strip()
        for line in text.splitlines()
        if any(w in line.lower() for w in ("trace", "tracing", "telemetry"))
    ]
    print(f"crew answer: {answer!r}")
    print(f"outside connection attempts: {sorted(set(attempts)) or 'none'}")
    print(f"trace/telemetry lines in output: {prompts or 'none'}")
    return 1 if attempts or prompts else 0


if __name__ == "__main__":
    sys.exit(main())
