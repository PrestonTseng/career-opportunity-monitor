from __future__ import annotations

import json
import threading
import time
from collections.abc import Generator, Mapping
from contextlib import contextmanager, suppress
from dataclasses import replace
from decimal import Decimal
from http.client import HTTPException
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import cast
from unittest.mock import patch

import pytest
import yaml

import career_opportunity_monitor.llm_adjustment as llm_adjustment
from career_opportunity_monitor.config import load_configuration
from career_opportunity_monitor.llm_adjustment import (
    LlmConfiguration,
    assess_job,
    store_assessment,
)
from career_opportunity_monitor.models import CanonicalJob, Evaluation
from career_opportunity_monitor.ranking import evaluate_job, parse_job

ROOT = Path(__file__).parents[1]
CONFIG = load_configuration(
    ROOT / "examples" / "resume_facts.yaml", ROOT / "examples" / "strategy" / "v1"
)


class _FakeOpenAIHandler(BaseHTTPRequestHandler):
    request_bodies: list[bytes] = []
    request_methods: list[str] = []
    response_body = b""
    response_headers: Mapping[str, str] = {}
    response_status = 500
    delay_seconds = 0.0
    response_content_length: int | None = None

    def do_POST(self) -> None:
        self.request_methods.append("POST")
        content_length = int(self.headers.get("Content-Length", "0"))
        self.request_bodies.append(self.rfile.read(content_length))
        self._send_test_response()

    def do_GET(self) -> None:
        self.request_methods.append("GET")
        self._send_test_response()

    def _send_test_response(self) -> None:
        if self.delay_seconds:
            time.sleep(self.delay_seconds)
        self.send_response(self.response_status)
        self.send_header("Content-Type", "application/json")
        for name, value in self.response_headers.items():
            self.send_header(name, value)
        if self.response_content_length is not None:
            self.send_header("Content-Length", str(self.response_content_length))
        self.end_headers()
        with suppress(BrokenPipeError):
            self.wfile.write(self.response_body)

    def log_message(self, format: str, *args: object) -> None:
        pass


@contextmanager
def _fake_openai_server(
    *,
    response_body: bytes = b"",
    response_status: int = 500,
    delay_seconds: float = 0,
    response_content_length: int | None = None,
    response_headers: Mapping[str, str] | None = None,
) -> Generator[tuple[str, type[_FakeOpenAIHandler]]]:
    handler = type(
        "RecordingHandler",
        (_FakeOpenAIHandler,),
        {
            "delay_seconds": delay_seconds,
            "request_bodies": [],
            "request_methods": [],
            "response_body": response_body,
            "response_content_length": response_content_length,
            "response_headers": response_headers or {},
            "response_status": response_status,
        },
    )
    server = ThreadingHTTPServer(("127.0.0.1", 0), handler)
    thread = threading.Thread(target=server.serve_forever)
    thread.start()
    try:
        host, port = cast(tuple[str, int], server.server_address)
        yield f"http://{host}:{port}/v1", handler
    finally:
        server.shutdown()
        thread.join()
        server.server_close()


def _job_and_evaluation() -> tuple[CanonicalJob, Evaluation]:
    raw = yaml.safe_load((ROOT / "examples" / "job.yaml").read_text())
    assert isinstance(raw, dict)
    job = parse_job(cast(dict[str, object], raw))
    return job, evaluate_job(CONFIG, job)


def test_off_mode_makes_no_request_and_keeps_scores_separate() -> None:
    job, evaluation = _job_and_evaluation()
    with _fake_openai_server() as (base_url, handler):
        result = assess_job(
            LlmConfiguration(mode="off", base_url=base_url),
            CONFIG.strategy,
            job,
            evaluation,
        )

    assert handler.request_bodies == []
    assert result.status == "off"
    assert result.base_score == evaluation.base_score
    assert result.adjustment == 0
    assert result.final_score == evaluation.base_score


def test_environment_defaults_llm_mode_to_off() -> None:
    assert LlmConfiguration.from_environment({}) == LlmConfiguration()


def test_environment_rejects_unknown_llm_mode() -> None:
    with pytest.raises(ValueError, match="LLM mode"):
        LlmConfiguration.from_environment({"CAREER_MONITOR_LLM_MODE": "maybe"})


def test_environment_configures_openai_compatible_endpoint() -> None:
    configuration = LlmConfiguration.from_environment(
        {
            "CAREER_MONITOR_LLM_MODE": "openai",
            "CAREER_MONITOR_LLM_BASE_URL": "https://llm.example/v1",
            "CAREER_MONITOR_LLM_MODEL": "example-model",
            "CAREER_MONITOR_LLM_API_KEY": "test-key",
            "CAREER_MONITOR_LLM_TIMEOUT_SECONDS": "2.5",
            "CAREER_MONITOR_LLM_RETRIES": "2",
            "CAREER_MONITOR_LLM_PROMPT_VERSION": "assessment-v2",
        }
    )

    assert configuration == LlmConfiguration(
        mode="openai",
        base_url="https://llm.example/v1",
        model="example-model",
        api_key="test-key",
        timeout_seconds=2.5,
        retries=2,
        prompt_version="assessment-v2",
    )


def test_environment_reads_api_key_from_secret_file(tmp_path: Path) -> None:
    secret = tmp_path / "api-key"
    secret.write_text("file-key\n", encoding="utf-8")

    configuration = LlmConfiguration.from_environment(
        {
            "CAREER_MONITOR_LLM_MODE": "openai",
            "CAREER_MONITOR_LLM_BASE_URL": "https://llm.example/v1",
            "CAREER_MONITOR_LLM_MODEL": "example-model",
            "CAREER_MONITOR_LLM_API_KEY_FILE": str(secret),
        }
    )

    assert configuration.api_key == "file-key"


def test_empty_secret_file_setting_uses_environment_api_key() -> None:
    configuration = LlmConfiguration.from_environment(
        {
            "CAREER_MONITOR_LLM_MODE": "openai",
            "CAREER_MONITOR_LLM_API_KEY": "environment-key",
            "CAREER_MONITOR_LLM_API_KEY_FILE": "",
        }
    )

    assert configuration.api_key == "environment-key"


def test_valid_response_applies_only_the_capped_component() -> None:
    job, evaluation = _job_and_evaluation()
    response = json.dumps(
        {
            "choices": [
                {
                    "message": {
                        "content": json.dumps(
                            {"adjustment": 5, "reasons": ["Strong role alignment"]}
                        )
                    }
                }
            ]
        }
    ).encode()
    strategy = replace(CONFIG.strategy, llm_adjustment_enabled=True)

    with _fake_openai_server(response_body=response, response_status=200) as (
        base_url,
        handler,
    ):
        result = assess_job(
            LlmConfiguration(
                mode="openai",
                base_url=base_url,
                model="example-model",
                api_key="test-key",
                prompt_version="assessment-v1",
            ),
            strategy,
            job,
            evaluation,
        )

    request = json.loads(handler.request_bodies[0])
    assert request["model"] == "example-model"
    assert request["response_format"]["type"] == "json_schema"
    assert result.status == "applied"
    assert result.base_score == evaluation.base_score
    assert result.adjustment == 5
    assert result.final_score == evaluation.base_score + 5
    assert result.model == "example-model"
    assert result.prompt_version == "assessment-v1"
    assert len(result.request_hash or "") == 64
    assert len(result.response_hash or "") == 64
    assert result.reasons == ("Strong role alignment",)


@pytest.mark.parametrize(
    "content",
    [
        '{"adjustment":1,"adjustment":2,"reasons":["duplicate"]}',
        '{"adjustment":1,"reasons":["ok"],"extra":true}',
        '{"adjustment":true,"reasons":["wrong type"]}',
        '{"adjustment":1.5,"reasons":["wrong type"]}',
        '{"adjustment":6,"reasons":["over cap"]}',
        'result: {"adjustment":1,"reasons":["mixed text"]}',
    ],
)
def test_non_strict_model_content_uses_zero_adjustment(content: str) -> None:
    job, evaluation = _job_and_evaluation()
    response = json.dumps({"choices": [{"message": {"content": content}}]}).encode()
    strategy = replace(CONFIG.strategy, llm_adjustment_enabled=True)

    with _fake_openai_server(response_body=response, response_status=200) as (
        base_url,
        _handler,
    ):
        result = assess_job(
            LlmConfiguration(mode="openai", base_url=base_url, model="example-model"),
            strategy,
            job,
            evaluation,
        )

    assert result.status == "invalid"
    assert result.adjustment == 0
    assert result.final_score == evaluation.base_score
    assert result.response_hash is not None
    assert result.reasons == ("LLM response failed strict validation",)


def test_unavailable_endpoint_retries_then_uses_zero_adjustment() -> None:
    job, evaluation = _job_and_evaluation()
    strategy = replace(CONFIG.strategy, llm_adjustment_enabled=True)

    with _fake_openai_server(response_status=503) as (base_url, handler):
        result = assess_job(
            LlmConfiguration(
                mode="openai",
                base_url=base_url,
                model="example-model",
                retries=2,
            ),
            strategy,
            job,
            evaluation,
        )

    assert len(handler.request_bodies) == 3
    assert result.status == "unavailable"
    assert result.adjustment == 0
    assert result.final_score == evaluation.base_score
    assert result.response_hash is None
    assert result.reasons == ("LLM endpoint is unavailable",)


def test_endpoint_redirect_is_not_followed() -> None:
    job, evaluation = _job_and_evaluation()
    strategy = replace(CONFIG.strategy, llm_adjustment_enabled=True)

    with (
        _fake_openai_server(response_status=500) as (
            redirect_target,
            target_handler,
        ),
        _fake_openai_server(
            response_status=302,
            response_headers={"Location": f"{redirect_target}/capture"},
        ) as (base_url, source_handler),
    ):
        result = assess_job(
            LlmConfiguration(
                mode="openai",
                base_url=base_url,
                model="example-model",
                api_key="test-key",
            ),
            strategy,
            job,
            evaluation,
        )

    assert source_handler.request_methods == ["POST"]
    assert target_handler.request_methods == []
    assert result.status == "unavailable"
    assert result.adjustment == 0


def test_timed_out_endpoint_uses_zero_adjustment_with_timeout_status() -> None:
    job, evaluation = _job_and_evaluation()
    strategy = replace(CONFIG.strategy, llm_adjustment_enabled=True)

    with _fake_openai_server(response_status=200, delay_seconds=0.05) as (
        base_url,
        _handler,
    ):
        result = assess_job(
            LlmConfiguration(
                mode="openai",
                base_url=base_url,
                model="example-model",
                timeout_seconds=0.001,
            ),
            strategy,
            job,
            evaluation,
        )

    assert result.status == "timeout"
    assert result.adjustment == 0
    assert result.final_score == evaluation.base_score
    assert result.response_hash is None
    assert result.reasons == ("LLM request timed out",)


def test_truncated_endpoint_response_is_invalid_and_uses_zero_adjustment() -> None:
    job, evaluation = _job_and_evaluation()
    strategy = replace(CONFIG.strategy, llm_adjustment_enabled=True)

    with _fake_openai_server(
        response_body=b"{}", response_status=200, response_content_length=20
    ) as (base_url, _handler):
        result = assess_job(
            LlmConfiguration(mode="openai", base_url=base_url, model="example-model"),
            strategy,
            job,
            evaluation,
        )

    assert result.status == "invalid"
    assert result.adjustment == 0
    assert result.final_score == evaluation.base_score
    assert result.response_hash is not None
    assert result.reasons == ("LLM response failed strict validation",)


def test_http_protocol_failure_uses_zero_adjustment() -> None:
    job, evaluation = _job_and_evaluation()
    strategy = replace(CONFIG.strategy, llm_adjustment_enabled=True)

    def fail_request(*_args: object, **_kwargs: object) -> None:
        raise HTTPException("malformed HTTP response")

    with patch.object(llm_adjustment, "_open_request", fail_request):
        result = assess_job(
            LlmConfiguration(
                mode="openai",
                base_url="http://llm.invalid/v1",
                model="example-model",
            ),
            strategy,
            job,
            evaluation,
        )

    assert result.status == "unavailable"
    assert result.adjustment == 0
    assert result.final_score == evaluation.base_score
    assert result.response_hash is None
    assert result.reasons == ("LLM endpoint is unavailable",)


@pytest.mark.parametrize(
    "base_score, adjustment, expected_final",
    [
        (Decimal("98.50"), 5, Decimal(100)),
        (Decimal("1.50"), -5, Decimal(0)),
    ],
)
def test_valid_adjustment_clamps_final_score_to_score_range(
    base_score: Decimal, adjustment: int, expected_final: Decimal
) -> None:
    job, evaluation = _job_and_evaluation()
    boundary_evaluation = replace(evaluation, base_score=base_score)
    response = json.dumps(
        {
            "choices": [
                {
                    "message": {
                        "content": json.dumps(
                            {"adjustment": adjustment, "reasons": ["Bounded fit"]}
                        )
                    }
                }
            ]
        }
    ).encode()
    strategy = replace(CONFIG.strategy, llm_adjustment_enabled=True)

    with _fake_openai_server(response_body=response, response_status=200) as (
        base_url,
        _handler,
    ):
        result = assess_job(
            LlmConfiguration(mode="openai", base_url=base_url, model="example-model"),
            strategy,
            job,
            boundary_evaluation,
        )

    assert result.base_score == base_score
    assert result.adjustment == adjustment
    assert result.final_score == expected_final


def test_assessment_serialization_stores_provenance_and_separate_scores() -> None:
    job, evaluation = _job_and_evaluation()
    response = json.dumps(
        {
            "choices": [
                {
                    "message": {
                        "content": '{"adjustment":-2,"reasons":["Limited detail"]}'
                    }
                }
            ]
        }
    ).encode()
    strategy = replace(CONFIG.strategy, llm_adjustment_enabled=True)

    with _fake_openai_server(response_body=response, response_status=200) as (
        base_url,
        _handler,
    ):
        result = assess_job(
            LlmConfiguration(
                mode="openai",
                base_url=base_url,
                model="example-model",
                prompt_version="assessment-v1",
            ),
            strategy,
            job,
            evaluation,
        )

    stored = json.loads(result.to_json_bytes())
    assert stored == {
        "adjustment": -2,
        "base_score": "80.00",
        "final_score": "78.00",
        "model": "example-model",
        "prompt_version": "assessment-v1",
        "reasons": ["Limited detail"],
        "request_hash": result.request_hash,
        "response_hash": result.response_hash,
        "status": "applied",
    }


class _RecordingAssessmentRepository:
    def __init__(self) -> None:
        self.stored: tuple[int, str, bytes, str] | None = None

    def store_llm_assessment(
        self, evaluation_id: int, key: str, content: bytes, created_at: str
    ) -> int:
        self.stored = (evaluation_id, key, content, created_at)
        return 41


def test_store_assessment_persists_the_complete_content_addressed_record() -> None:
    job, evaluation = _job_and_evaluation()
    assessment = assess_job(LlmConfiguration(), CONFIG.strategy, job, evaluation)
    repository = _RecordingAssessmentRepository()

    assessment_id = store_assessment(repository, 7, assessment, "2026-08-27T10:00:00Z")

    assert assessment_id == 41
    assert repository.stored is not None
    evaluation_id, key, content, created_at = repository.stored
    assert evaluation_id == 7
    assert key.startswith("assessment:")
    assert len(key.removeprefix("assessment:")) == 64
    assert content == assessment.to_json_bytes()
    assert created_at == "2026-08-27T10:00:00Z"
