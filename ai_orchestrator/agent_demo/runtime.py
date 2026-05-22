from __future__ import annotations

import json
import time
from dataclasses import dataclass, field
from typing import Any, Callable

from ai_orchestrator.agent_demo.schemas import AgentAction, AgentActionError
from ai_orchestrator.agent_demo import tools
from ai_orchestrator.services.llm_providers import VLLMProviderService
from ai_orchestrator.services.runtime import AIRuntimeService


ToolFn = Callable[..., dict[str, Any]]


TOOL_REGISTRY: dict[str, ToolFn] = {
    "search_deals": tools.search_deals,
    "retrieve_chunks": tools.retrieve_chunks,
    "verify_evidence": tools.verify_evidence,
    "final_answer": tools.final_answer,
}


@dataclass
class AgentTraceStep:
    index: int
    action: str
    reason: str
    arguments: dict[str, Any]
    observation: dict[str, Any]
    duration_ms: int
    confidence: float = 0.0


@dataclass
class AgentDemoResult:
    question: str
    final_answer: str = ""
    citations: list[Any] = field(default_factory=list)
    steps: list[AgentTraceStep] = field(default_factory=list)
    error: str = ""


class DemoAgentRuntime:
    def __init__(
        self,
        *,
        question: str,
        max_iterations: int = 8,
        model: str | None = None,
        stdout=None,
    ):
        self.question = question.strip()
        self.max_iterations = max(1, min(int(max_iterations or 8), 20))
        self.provider = VLLMProviderService()
        self.model = model or AIRuntimeService.get_planner_model()
        self.stdout = stdout
        self.steps: list[AgentTraceStep] = []
        self.observations: list[dict[str, Any]] = []
        self.retrieved_evidence: list[dict[str, Any]] = []
        self.verified = False

    def run(self) -> AgentDemoResult:
        result = AgentDemoResult(question=self.question)
        if not self.question:
            result.error = "Question is required."
            return result

        repair_hint = ""
        for iteration in range(1, self.max_iterations + 1):
            prompt = self._build_prompt(repair_hint=repair_hint)
            repair_hint = ""
            try:
                action = self._next_action(prompt)
            except AgentActionError as exc:
                repair_hint = str(exc)
                self._write(f"[{iteration}] Invalid model action: {exc}")
                continue
            except Exception as exc:
                result.error = f"Model call failed: {exc}"
                return result

            started = time.time()
            observation = self._execute_action(action)
            duration_ms = int((time.time() - started) * 1000)
            step = AgentTraceStep(
                index=iteration,
                action=action.action,
                reason=action.reason,
                arguments=action.arguments,
                observation=observation,
                duration_ms=duration_ms,
                confidence=action.confidence,
            )
            self.steps.append(step)
            self.observations.append(
                {
                    "action": action.action,
                    "arguments": action.arguments,
                    "observation": observation,
                }
            )
            self._print_step(step)

            if action.action == "retrieve_chunks":
                self.retrieved_evidence = observation.get("chunks") or []
            elif action.action == "verify_evidence":
                self.verified = True
            elif action.action == "final_answer":
                if not observation.get("ok"):
                    continue
                result.final_answer = observation.get("answer") or ""
                result.citations = observation.get("citations") or []
                result.steps = self.steps
                return result

        result.steps = self.steps
        result.error = f"Agent stopped after {self.max_iterations} iterations without final_answer."
        return result

    def _next_action(self, prompt: str) -> AgentAction:
        payload = {
            "model": self.model,
            "system": self._system_prompt(),
            "prompt": prompt,
            "stream": False,
            "options": {
                "temperature": 0.1,
                "max_tokens": 1400,
            },
            "chat_template_kwargs": {"enable_thinking": False},
        }
        response = self.provider.execute_standard(payload, timeout=180)
        text = response.get("response") or ""
        return AgentAction.from_model_text(text)

    def _execute_action(self, action: AgentAction) -> dict[str, Any]:
        if action.action == "final_answer" and not self.verified:
            return {
                "ok": False,
                "message": "final_answer rejected: call verify_evidence before final_answer.",
            }
        if action.action == "verify_evidence" and not action.arguments.get("evidence"):
            action.arguments["evidence"] = self.retrieved_evidence
        tool = TOOL_REGISTRY[action.action]
        try:
            return {"ok": True, **tool(**action.arguments)}
        except Exception as exc:
            return {"ok": False, "error": str(exc), "message": f"Tool {action.action} failed."}

    def _system_prompt(self) -> str:
        return (
            "You are a small read-only agent demo for an investment deal database. "
            "You must choose exactly one tool per turn and return JSON only. "
            "Do not write prose outside JSON. Do not invent facts. "
            "Use retrieved evidence before answering. You must call verify_evidence before final_answer."
        )

    def _build_prompt(self, *, repair_hint: str = "") -> str:
        state = {
            "question": self.question,
            "verified": self.verified,
            "has_retrieved_evidence": bool(self.retrieved_evidence),
            "recent_observations": self.observations[-5:],
        }
        repair = f"\n\nPrevious output was invalid: {repair_hint}\nReturn corrected JSON only." if repair_hint else ""
        return f"""
Available tools:
1. search_deals
   arguments: {{"query": string, "limit": integer optional}}
   purpose: find candidate deals by title, industry, sector, summary, or details.

2. retrieve_chunks
   arguments: {{"query": string, "deal_ids": [string] optional, "limit": integer optional}}
   purpose: retrieve evidence chunks for selected deals.

3. verify_evidence
   arguments: {{"question": string, "draft_answer": string, "evidence": [object] optional}}
   purpose: verify whether a draft answer is supportable by retrieved evidence.

4. final_answer
   arguments: {{"answer": string, "citations": [object] optional}}
   purpose: finish the run. Only use after verify_evidence.

Return exactly this JSON shape:
{{
  "action": "search_deals|retrieve_chunks|verify_evidence|final_answer",
  "arguments": {{}},
  "reason": "short reason",
  "confidence": 0.0
}}

Decision rules:
- First find candidate deals unless the question is impossible to search.
- Retrieve chunks for promising deal_ids before drafting an answer.
- If no chunks are found, answer that evidence is insufficient.
- Before final_answer, call verify_evidence with your draft answer.
- Keep final answers concise and cite deal/source names from the evidence.

Current state:
{json.dumps(state, default=str, indent=2)}
{repair}
""".strip()

    def _print_step(self, step: AgentTraceStep) -> None:
        self._write(f"\n[{step.index}] Agent action: {step.action}")
        if step.reason:
            self._write(f"[{step.index}] Reason: {step.reason}")
        message = step.observation.get("message") or step.observation.get("error") or "No message."
        self._write(f"[{step.index}] Observation: {message}")

    def _write(self, message: str) -> None:
        if self.stdout:
            self.stdout.write(message)
