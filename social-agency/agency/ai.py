"""Claude integration with caching, spend limits, usage/cost tracking and graceful degradation.

Every call goes through ``AIService.structured`` which:
  * returns a cached result for an identical request (keyed by content hash),
  * refuses to call the API once the monthly budget is spent (callers fall back to heuristics),
  * records tokens and estimated cost per account and workflow,
  * raises ``AIUnavailable`` instead of crashing the pipeline when the service is down.
"""
from __future__ import annotations

import base64
import datetime as dt
import hashlib
import io
import json
import logging
from typing import Type, TypeVar

from PIL import Image
from pydantic import BaseModel
from sqlalchemy import case, func, select
from sqlalchemy.orm import Session

from .config import MODEL_PRICING, get_settings
from .models import AICache, AIUsage, utcnow
from .security import scrub

log = logging.getLogger("agency.ai")

T = TypeVar("T", bound=BaseModel)


class AIUnavailable(Exception):
    """AI is not configured, over budget, refused, or temporarily failing."""


def month_spend(s: Session) -> float:
    start = utcnow().replace(day=1, hour=0, minute=0, second=0, microsecond=0)
    return float(s.scalar(select(func.coalesce(func.sum(AIUsage.cost_usd), 0.0)).where(AIUsage.ts >= start)) or 0.0)


def estimate_cost(model: str, input_tokens: int, output_tokens: int) -> float:
    pin, pout = MODEL_PRICING.get(model, MODEL_PRICING["claude-opus-5-5"])
    return round(input_tokens / 1e6 * pin + output_tokens / 1e6 * pout, 6)


def image_block(path: str, max_side: int = 1568) -> dict:
    """Downscale before sending: larger images cost more tokens without improving analysis."""
    with Image.open(path) as im:
        im = im.convert("RGB")
        im.thumbnail((max_side, max_side))
        buf = io.BytesIO()
        im.save(buf, "JPEG", quality=85)
    return {"type": "image", "source": {"type": "base64", "media_type": "image/jpeg",
                                         "data": base64.standard_b64encode(buf.getvalue()).decode()}}


class AIService:
    def __init__(self, client=None):
        self._client = client

    @property
    def configured(self) -> bool:
        return self._client is not None or bool(get_settings().anthropic_api_key)

    def _get_client(self):
        if self._client is None:
            import anthropic

            self._client = anthropic.Anthropic(api_key=get_settings().anthropic_api_key, max_retries=2, timeout=300)
        return self._client

    def structured(
        self,
        s: Session,
        *,
        workflow: str,
        schema: Type[T],
        system: str,
        content: list[dict],
        account_id: int | None = None,
        light: bool = False,
        max_tokens: int = 16000,
    ) -> tuple[T, str]:
        """Return (parsed result, source label). Raises AIUnavailable when AI cannot be used."""
        settings = get_settings()
        model = settings.ai_model_light if light else settings.ai_model
        if not self.configured:
            raise AIUnavailable("ANTHROPIC_API_KEY is not configured")

        key = hashlib.sha256(json.dumps([workflow, model, system, content, schema.model_json_schema()],
                                        sort_keys=True, default=str).encode()).hexdigest()
        cached = s.get(AICache, key)
        if cached is not None:
            s.add(AIUsage(account_id=account_id, workflow=workflow, model=model, cache_hit=True))
            return schema.model_validate(cached.value), f"ai:{model}"

        spent = month_spend(s)
        if spent >= settings.ai_monthly_budget_usd:
            raise AIUnavailable(f"Monthly AI budget of ${settings.ai_monthly_budget_usd:.2f} reached (${spent:.2f} spent)")

        import anthropic

        try:
            response = self._get_client().beta.messages.parse(
                model=model,
                max_tokens=max_tokens,
                system=system,
                messages=[{"role": "user", "content": content}],
                output_format=schema,
                betas=["server-side-fallback-2026-07-01"],
                fallbacks="default",
            )
        except anthropic.RateLimitError as exc:
            self._record(s, account_id, workflow, model, 0, 0, ok=False)
            raise AIUnavailable(f"Claude rate limited: {exc}") from exc
        except anthropic.APIStatusError as exc:
            self._record(s, account_id, workflow, model, 0, 0, ok=False)
            raise AIUnavailable(scrub(f"Claude API error {exc.status_code}: {exc.message}")) from exc
        except anthropic.APIConnectionError as exc:
            self._record(s, account_id, workflow, model, 0, 0, ok=False)
            raise AIUnavailable(f"Claude unreachable: {exc}") from exc

        usage = getattr(response, "usage", None)
        in_tok = getattr(usage, "input_tokens", 0) or 0
        out_tok = getattr(usage, "output_tokens", 0) or 0
        served = getattr(response, "model", model) or model
        if response.stop_reason == "refusal":
            self._record(s, account_id, workflow, served, in_tok, out_tok, ok=False)
            raise AIUnavailable("Claude declined this request; it needs human review")
        parsed = getattr(response, "parsed_output", None)
        if parsed is None:
            self._record(s, account_id, workflow, served, in_tok, out_tok, ok=False)
            raise AIUnavailable(f"Claude returned no structured output (stop_reason={response.stop_reason})")
        self._record(s, account_id, workflow, served, in_tok, out_tok, ok=True)
        s.merge(AICache(key=key, value=parsed.model_dump()))
        return parsed, f"ai:{served}"

    @staticmethod
    def _record(s: Session, account_id, workflow, model, in_tok, out_tok, ok: bool) -> None:
        s.add(AIUsage(account_id=account_id, workflow=workflow, model=model, input_tokens=in_tok,
                      output_tokens=out_tok, cost_usd=estimate_cost(model, in_tok, out_tok), ok=ok))
        s.flush()


_service: AIService | None = None


def get_ai() -> AIService:
    global _service
    if _service is None:
        _service = AIService()
    return _service


def set_ai(service: AIService | None) -> None:
    """Tests inject a fake client here."""
    global _service
    _service = service


def usage_summary(s: Session, days: int = 30) -> dict:
    since = utcnow() - dt.timedelta(days=days)
    rows = s.execute(
        select(AIUsage.workflow, AIUsage.account_id, func.count(), func.sum(AIUsage.cost_usd),
               func.sum(AIUsage.input_tokens), func.sum(AIUsage.output_tokens),
               func.sum(case((AIUsage.cache_hit.is_(True), 1), else_=0)))
        .where(AIUsage.ts >= since).group_by(AIUsage.workflow, AIUsage.account_id)
    ).all()
    settings = get_settings()
    return {
        "period_days": days,
        "month_to_date_usd": round(month_spend(s), 4),
        "monthly_budget_usd": settings.ai_monthly_budget_usd,
        "rows": [
            {"workflow": w, "account_id": a, "calls": int(n), "cost_usd": round(float(c or 0), 4),
             "input_tokens": int(i or 0), "output_tokens": int(o or 0), "cache_hits": int(h or 0)}
            for w, a, n, c, i, o, h in rows
        ],
        "note": "Costs are estimates from token usage and list prices, not an invoice.",
    }
