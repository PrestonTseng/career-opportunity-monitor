from __future__ import annotations

import hashlib
import json
from collections.abc import Mapping
from dataclasses import asdict, dataclass
from decimal import Decimal
from http.client import HTTPException, HTTPMessage
from pathlib import Path
from typing import Literal, Protocol, cast
from urllib.error import URLError
from urllib.request import HTTPRedirectHandler, Request, build_opener

from .models import CanonicalJob, Evaluation, Strategy

LlmMode = Literal["off", "openai"]
AssessmentStatus = Literal["off", "applied", "invalid", "timeout", "unavailable"]


@dataclass(frozen=True)
class LlmConfiguration:
    mode: LlmMode = "off"
    base_url: str | None = None
    model: str | None = None
    api_key: str | None = None
    timeout_seconds: float = 10.0
    retries: int = 0
    prompt_version: str = "v1"

    @classmethod
    def from_environment(cls, environment: Mapping[str, str]) -> LlmConfiguration:
        raw_mode = environment.get("CAREER_MONITOR_LLM_MODE", "off")
        if raw_mode not in ("off", "openai"):
            raise ValueError("LLM mode must be off or openai")
        mode: LlmMode = raw_mode
        api_key = environment.get("CAREER_MONITOR_LLM_API_KEY")
        secret_path = environment.get("CAREER_MONITOR_LLM_API_KEY_FILE")
        if mode == "openai" and secret_path:
            api_key = Path(secret_path).read_text(encoding="utf-8").strip()
        return cls(
            mode=mode,
            base_url=environment.get("CAREER_MONITOR_LLM_BASE_URL"),
            model=environment.get("CAREER_MONITOR_LLM_MODEL"),
            api_key=api_key,
            timeout_seconds=float(
                environment.get("CAREER_MONITOR_LLM_TIMEOUT_SECONDS", "10")
            ),
            retries=int(environment.get("CAREER_MONITOR_LLM_RETRIES", "0")),
            prompt_version=environment.get("CAREER_MONITOR_LLM_PROMPT_VERSION", "v1"),
        )


@dataclass(frozen=True)
class LlmAssessment:
    status: AssessmentStatus
    base_score: Decimal
    adjustment: int
    final_score: Decimal
    model: str | None
    prompt_version: str
    request_hash: str | None
    response_hash: str | None
    reasons: tuple[str, ...]

    def to_json_bytes(self) -> bytes:
        return _canonical_json(
            {
                "adjustment": self.adjustment,
                "base_score": f"{self.base_score:.2f}",
                "final_score": f"{self.final_score:.2f}",
                "model": self.model,
                "prompt_version": self.prompt_version,
                "reasons": list(self.reasons),
                "request_hash": self.request_hash,
                "response_hash": self.response_hash,
                "status": self.status,
            }
        )


class AssessmentRepository(Protocol):
    def store_llm_assessment(
        self, evaluation_id: int, key: str, content: bytes, created_at: str
    ) -> int: ...


class _UrlResponse(Protocol):
    def __enter__(self) -> _UrlResponse: ...

    def __exit__(
        self,
        exception_type: type[BaseException] | None,
        exception: BaseException | None,
        traceback: object,
    ) -> None: ...

    def read(self, amount: int | None = None) -> bytes: ...


class _NoRedirectHandler(HTTPRedirectHandler):
    def redirect_request(
        self,
        req: Request,
        fp: object,
        code: int,
        msg: str,
        headers: HTTPMessage,
        newurl: str,
    ) -> None:
        return None


def _open_request(request: Request, timeout: float) -> _UrlResponse:
    opener = build_opener(_NoRedirectHandler())
    return cast(_UrlResponse, opener.open(request, timeout=timeout))


def store_assessment(
    repository: AssessmentRepository,
    evaluation_id: int,
    assessment: LlmAssessment,
    created_at: str,
) -> int:
    """Persist one immutable, content-addressed assessment record."""
    content = assessment.to_json_bytes()
    key = f"assessment:{hashlib.sha256(content).hexdigest()}"
    return repository.store_llm_assessment(evaluation_id, key, content, created_at)


def _canonical_json(value: object) -> bytes:
    return json.dumps(
        value, ensure_ascii=False, sort_keys=True, separators=(",", ":")
    ).encode("utf-8")


def _request_bytes(
    configuration: LlmConfiguration,
    strategy: Strategy,
    job: CanonicalJob,
    evaluation: Evaluation,
) -> bytes:
    assert configuration.model is not None
    schema = {
        "additionalProperties": False,
        "properties": {
            "adjustment": {
                "maximum": strategy.llm_adjustment_maximum,
                "minimum": strategy.llm_adjustment_minimum,
                "type": "integer",
            },
            "reasons": {
                "items": {"minLength": 1, "type": "string"},
                "minItems": 1,
                "type": "array",
            },
        },
        "required": ["adjustment", "reasons"],
        "type": "object",
    }
    evidence = json.loads(evaluation.to_json_bytes())
    job_context = {
        "company": job.company,
        "description_text": job.description_text,
        "employment_type": job.employment_type,
        "location": None if job.location is None else asdict(job.location),
        "posted_at": job.posted_at,
        "source_job_id": job.source_job_id,
        "title": job.title,
    }
    return _canonical_json(
        {
            "messages": [
                {
                    "content": (
                        "Assess only the supplied job and deterministic evidence. "
                        "Do not create resume facts or propose strategy edits. "
                        "Return JSON only."
                    ),
                    "role": "system",
                },
                {
                    "content": json.dumps(
                        {"evaluation": evidence, "job": job_context},
                        ensure_ascii=False,
                        sort_keys=True,
                        separators=(",", ":"),
                    ),
                    "role": "user",
                },
            ],
            "model": configuration.model,
            "response_format": {
                "json_schema": {
                    "name": f"career_adjustment_{configuration.prompt_version}",
                    "schema": schema,
                    "strict": True,
                },
                "type": "json_schema",
            },
            "temperature": 0,
        }
    )


def _failure(
    status: Literal["invalid", "timeout", "unavailable"],
    configuration: LlmConfiguration,
    evaluation: Evaluation,
    request_hash: str,
    reason: str,
    *,
    response_hash: str | None = None,
) -> LlmAssessment:
    return LlmAssessment(
        status=status,
        base_score=evaluation.base_score,
        adjustment=0,
        final_score=evaluation.base_score,
        model=configuration.model,
        prompt_version=configuration.prompt_version,
        request_hash=request_hash,
        response_hash=response_hash,
        reasons=(reason,),
    )


def _unique_object(pairs: list[tuple[str, object]]) -> dict[str, object]:
    value: dict[str, object] = {}
    for key, item in pairs:
        if key in value:
            raise ValueError("duplicate JSON key")
        value[key] = item
    return value


def _parse_adjustment(
    raw_response: bytes, strategy: Strategy
) -> tuple[int, tuple[str, ...]]:
    envelope = json.loads(raw_response)
    content = envelope["choices"][0]["message"]["content"]
    if not isinstance(content, str):
        raise ValueError("response content is not text")
    decoded: object = json.loads(content, object_pairs_hook=_unique_object)
    if not isinstance(decoded, dict):
        raise ValueError("response is not an object")
    value = cast(dict[str, object], decoded)
    if set(value) != {"adjustment", "reasons"}:
        raise ValueError("response has the wrong keys")
    adjustment = value["adjustment"]
    reasons = value["reasons"]
    if type(adjustment) is not int:
        raise ValueError("adjustment is not an integer")
    if not (
        strategy.llm_adjustment_minimum <= adjustment <= strategy.llm_adjustment_maximum
    ):
        raise ValueError("adjustment exceeds the configured cap")
    if not isinstance(reasons, list):
        raise ValueError("reasons must be a list")
    typed_reasons = cast(list[object], reasons)
    if not typed_reasons or any(
        not isinstance(reason, str) or not reason for reason in typed_reasons
    ):
        raise ValueError("reasons must be non-empty strings")
    return adjustment, tuple(cast(str, reason) for reason in typed_reasons)


def assess_job(
    configuration: LlmConfiguration,
    strategy: Strategy,
    job: CanonicalJob,
    evaluation: Evaluation,
) -> LlmAssessment:
    """Apply an optional bounded assessment without changing deterministic evidence."""
    if configuration.mode == "off" or not strategy.llm_adjustment_enabled:
        return LlmAssessment(
            status="off",
            base_score=evaluation.base_score,
            adjustment=0,
            final_score=evaluation.base_score,
            model=configuration.model,
            prompt_version=configuration.prompt_version,
            request_hash=None,
            response_hash=None,
            reasons=("LLM mode is off",),
        )
    if configuration.mode != "openai":
        raise ValueError("LLM mode must be off or openai")
    if not configuration.base_url or not configuration.model:
        raise ValueError("OpenAI mode requires base URL and model")
    if configuration.timeout_seconds <= 0 or configuration.retries < 0:
        raise ValueError("LLM timeout and retries must be valid")

    request_bytes = _request_bytes(configuration, strategy, job, evaluation)
    request_hash = hashlib.sha256(request_bytes).hexdigest()
    headers = {"Content-Type": "application/json"}
    if configuration.api_key:
        headers["Authorization"] = f"Bearer {configuration.api_key}"
    request = Request(
        f"{configuration.base_url.rstrip('/')}/chat/completions",
        data=request_bytes,
        headers=headers,
        method="POST",
    )
    raw_response: bytes | None = None
    failure_status: Literal["timeout", "unavailable"] = "unavailable"
    for attempt in range(configuration.retries + 1):
        try:
            with _open_request(request, configuration.timeout_seconds) as response:
                raw_response = response.read(1_000_001)
            break
        except TimeoutError:
            failure_status = "timeout"
        except URLError as exc:
            failure_status = (
                "timeout" if isinstance(exc.reason, TimeoutError) else "unavailable"
            )
        except (HTTPException, OSError):
            failure_status = "unavailable"
        if attempt == configuration.retries:
            reason = (
                "LLM request timed out"
                if failure_status == "timeout"
                else "LLM endpoint is unavailable"
            )
            return _failure(
                failure_status, configuration, evaluation, request_hash, reason
            )
    assert raw_response is not None
    response_hash = hashlib.sha256(raw_response).hexdigest()
    if len(raw_response) > 1_000_000:
        return _failure(
            "invalid",
            configuration,
            evaluation,
            request_hash,
            "LLM response exceeds the size limit",
            response_hash=response_hash,
        )
    try:
        adjustment, reasons = _parse_adjustment(raw_response, strategy)
    except (IndexError, KeyError, TypeError, ValueError, json.JSONDecodeError):
        return _failure(
            "invalid",
            configuration,
            evaluation,
            request_hash,
            "LLM response failed strict validation",
            response_hash=response_hash,
        )
    final_score = max(Decimal(0), min(Decimal(100), evaluation.base_score + adjustment))
    return LlmAssessment(
        status="applied",
        base_score=evaluation.base_score,
        adjustment=adjustment,
        final_score=final_score,
        model=configuration.model,
        prompt_version=configuration.prompt_version,
        request_hash=request_hash,
        response_hash=response_hash,
        reasons=reasons,
    )
