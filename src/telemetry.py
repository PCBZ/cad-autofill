"""A local trace of one run, written with the OpenTelemetry SDK.

Each step of the Flow is a span. The spans go to `trace.jsonl` in the shipment's output
folder through a file exporter, one JSON line per span: name, start and end time,
status, and the small facts a step adds (counts, token numbers). Nothing else is
exported anywhere: no OTLP endpoint, no network. Spans never carry document text or
prompts, and an exception is recorded by type only, because a validation error can quote
the data it rejected.

The provider is created here, not set globally, so it does not touch the one CrewAI makes
for its own telemetry (kept off by main.py). The SDK does nothing when
OTEL_SDK_DISABLED=true, so main.py removes that variable.

The file feeds the cost and pass-rate measurements of issue #13.
"""

from collections.abc import Iterator
from contextlib import contextmanager
from pathlib import Path

from opentelemetry.sdk.resources import Resource
from opentelemetry.sdk.trace import TracerProvider
from opentelemetry.sdk.trace.export import ConsoleSpanExporter, SimpleSpanProcessor
from opentelemetry.trace import Span, Status, StatusCode

Fact = str | int | float | bool


class RunTrace:
    def __init__(self, path: Path, **resource: str):
        path.parent.mkdir(parents=True, exist_ok=True)
        self._file = path.open("a", encoding="utf-8")
        self._provider = TracerProvider(
            resource=Resource.create({"service.name": "cad-autofill", **resource})
        )
        exporter = ConsoleSpanExporter(
            out=self._file, formatter=lambda s: s.to_json(indent=None) + "\n"
        )
        self._provider.add_span_processor(SimpleSpanProcessor(exporter))
        self._tracer = self._provider.get_tracer("cad_autofill")

    @contextmanager
    def step(self, name: str) -> Iterator[Span]:
        """Time a step. The body can add facts: `span.set_attribute("lines", 3)`."""
        # record_exception=False: the exception text may quote shipment data.
        with self._tracer.start_as_current_span(
            name, record_exception=False, set_status_on_exception=False
        ) as span:
            try:
                yield span
            except BaseException as e:
                span.set_status(Status(StatusCode.ERROR, type(e).__name__))
                raise

    def close(self) -> None:
        self._provider.shutdown()
        self._file.close()
