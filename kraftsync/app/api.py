"""Local HTTP service: n8n posts interpreted updates here, we check them against the graph
deterministically and call the n8n webhook only when there is something to tell people.

Start it with `uv run python -m kraftsync.app.serve` (never expose `create_app` without a token).
Contract: docs/api.md. No `from __future__ import annotations` on purpose (FastAPI introspection).
"""

import logging
import secrets
import time
from datetime import date, datetime
from pathlib import Path
from typing import Literal

import httpx
from fastapi import BackgroundTasks, Depends, FastAPI, Header, HTTPException, Request

from kraftsync.app.api_models import (
    ClaimResult,
    ConflictHook,
    ContextRequest,
    ContextResponse,
    ActionOut,
    ActionResultRequest,
    Recipient,
    StateResponse,
    TaskView,
    UpdateRequest,
    UpdateResponse,
)
from kraftsync.app.settings import Settings
from kraftsync.app.state import ProjectState
from kraftsync.contracts import Action, ActionStatus, EventKind, GraphEvent, TaskUpdate, UpdateKind
from kraftsync.engine.check import check, forecast, project_finish
from kraftsync.engine.resolve import ask_clarification, resolve_all

log = logging.getLogger("kraftsync.api")
MAX_FORECAST_DAYS = 60   # sanity bound on dates the LLM reports, relative to today


def post_hook(url: str, token: str | None, hook: ConflictHook) -> None:
    """Call the n8n webhook. Never raises; never logs the URL (it may contain a secret path)."""
    headers = {"Content-Type": "application/json"}
    if token:
        headers["X-KraftSync-Token"] = token
    try:
        httpx.post(url, content=hook.model_dump_json(), headers=headers, timeout=5.0).raise_for_status()
    except httpx.HTTPError as exc:
        log.warning("conflict hook failed: %s", type(exc).__name__)


def create_app(settings: Settings | None = None, state: ProjectState | None = None) -> FastAPI:
    settings = settings or Settings.from_env()
    settings.validate()
    if settings.log_path:
        settings.log_path.parent.mkdir(parents=True, exist_ok=True)
        logger = logging.getLogger("kraftsync")
        logger.setLevel(logging.INFO)
        if not any(isinstance(handler, logging.FileHandler) and
                   Path(handler.baseFilename) == settings.log_path.resolve() for handler in logger.handlers):
            handler = logging.FileHandler(settings.log_path, encoding="utf-8")
            handler.setFormatter(logging.Formatter("%(asctime)s %(message)s", "%Y-%m-%d %H:%M:%S"))
            logger.addHandler(handler)
    state = state or ProjectState(settings.plan_path, settings.events_path, settings.snapshot_path)
    token = settings.token.encode()
    docs_on = settings.docs and settings.loopback

    def require_token(x_kraftsync_token: str | None = Header(default=None)) -> None:
        if x_kraftsync_token is None or not secrets.compare_digest(x_kraftsync_token.encode(), token):
            raise HTTPException(status_code=401, detail="invalid or missing token")

    app = FastAPI(
        title="KraftSync local service", version="0",
        docs_url="/docs" if docs_on else None, redoc_url=None,
        openapi_url="/openapi.json" if docs_on else None,
    )

    @app.middleware("http")
    async def call_log(request: Request, call_next):
        started = time.perf_counter()
        response = await call_next(request)
        if not (request.method == "GET" and request.url.path in {"/health", "/state", "/events"}
                and response.status_code < 400):
            logging.getLogger("kraftsync").info("%s %s %d %dms", request.method, request.url.path,
                                                  response.status_code,
                                                  int((time.perf_counter() - started) * 1000))
        return response

    def today(received_at: datetime | None = None) -> date:
        """DEMO_TODAY (stage override) wins; else the message date; else the real date."""
        return settings.demo_today or (received_at.date() if received_at else date.today())

    def now() -> datetime:
        return datetime.now().astimezone()

    def build_state() -> StateResponse:
        fc = forecast(state.tasks, state.deps)
        views = [TaskView(**task.model_dump(), cpm_start=fc[task.id][0], cpm_finish=fc[task.id][1])
                 for task in state.tasks.values()]
        baseline = {task.id: task for task in state.plan.tasks}
        return StateResponse(
            project_id=state.plan.project_id, name=state.plan.name, today=today(),
            project_finish_planned=project_finish(forecast(baseline, state.deps), baseline, state.deps),
            project_finish_forecast=project_finish(fc, state.tasks, state.deps),
            tasks=views, dependencies=state.deps, open_discrepancies=list(state.discrepancies))

    def persist() -> None:
        try:
            state.write_snapshot(build_state().model_dump_json(indent=2))
        except OSError as exc:
            log.warning("snapshot write failed: %s", type(exc).__name__)

    def action_out(action: Action) -> ActionOut:
        person = state.persons[action.to_person_id]
        return ActionOut(**action.model_dump(),
                         recipient=Recipient(person_id=person.id, name=person.name,
                                             telegram_chat_id=settings.redirect_chat_id or person.telegram_chat_id),
                         detail=state.action_detail.get(action.id))

    def finalize(actions: list[Action]) -> tuple[list[Action], dict[str, Recipient]]:
        """Attach chat ids; in stage mode redirect everything to one chat, prefixed with the real recipient."""
        out, recipients = [], {}
        for a in actions:
            p = state.persons[a.to_person_id]
            if settings.redirect_chat_id:
                a = a.model_copy(update={"text": f"[an {p.name}] {a.text}"})
            out.append(a)
            recipients[p.id] = Recipient(
                person_id=p.id, name=p.name, telegram_chat_id=settings.redirect_chat_id or p.telegram_chat_id)
        return out, recipients

    def process_claim(u: TaskUpdate, req: UpdateRequest, sender, t: datetime, day: date) -> ClaimResult:
        if state.has_message(u.message_id):
            return ClaimResult(message_id=u.message_id, status="duplicate")
        actor = sender.id if sender else "unknown"
        interpreted = state.log(EventKind.update_interpreted, actor, t, task_id=u.task_id,
                                source_message_id=u.message_id, payload={"update": u.model_dump(mode="json")})
        events: list[GraphEvent] = [interpreted]

        def done(status, note=None, **kw) -> ClaimResult:
            return ClaimResult(message_id=u.message_id, status=status, note=note, events=events, **kw)

        if u.kind in (UpdateKind.irrelevant, UpdateKind.question):
            return done("ignored", f"{u.kind.value}: nothing to apply")
        if sender is None:
            return done("ignored", "unknown sender")
        if u.task_id is None or u.confidence < settings.confidence_min:
            options = [state.tasks[i] for i in u.candidate_task_ids if i in state.tasks]                 or state.candidates_for(sender.id, day, req.text)
            return done("needs_clarification", "low confidence or no task",
                        actions=[ask_clarification(sender, options, u.message_id, t)])

        before = dict(state.tasks)
        changed = state.apply(u, actor, t)
        if changed:
            events.append(changed)
        found = [d.model_copy(update={"source_event_id": interpreted.id})
                 for d in check(before, state.tasks, state.deps, u, t)]
        for d in found:
            events.append(state.record_discrepancy(d, t))
        return done("applied", discrepancies=found,
                    actions=resolve_all(found, before, state.tasks, state.deps, state.persons, u, sender, t))

    def process(req: UpdateRequest) -> UpdateResponse:
        ids = [u.message_id for u in req.updates]
        if len(set(ids)) != len(ids):
            raise HTTPException(422, "each claim needs its own unique message_id")
        day = today(req.received_at)
        for u in req.updates:
            if u.task_id is not None and u.task_id not in state.tasks:
                raise HTTPException(422, f"unknown task_id {u.task_id!r}; choose from /context candidates")
            if u.new_forecast_finish and abs((u.new_forecast_finish - day).days) > MAX_FORECAST_DAYS:
                raise HTTPException(422, "new_forecast_finish is implausibly far from today")

        t = now()
        sender = state.resolve_sender(req.sender_name)
        fresh = [u for u in req.updates if not state.has_message(u.message_id)]
        message_event = None
        if fresh:   # the raw message is logged once, even if it carries several claims
            message_event = state.log(
                EventKind.message_received, sender.id if sender else "unknown", t,
                source_message_id=fresh[0].message_id,
                payload={"channel": req.channel.value, "chat_ref": req.chat_ref, "text": req.text})
        results = [process_claim(u, req, sender, t, day) for u in req.updates]

        order = ["applied", "needs_clarification", "ignored", "duplicate"]
        recipients: dict[str, Recipient] = {}
        for r in results:
            r.actions, rec = finalize(r.actions)
            recipients |= rec
        return UpdateResponse(
            status=min((r.status for r in results), key=order.index),
            results=results,
            events=([message_event] if message_event else []) + [e for r in results for e in r.events],
            discrepancies=[d for r in results for d in r.discrepancies],
            actions=[a for r in results for a in r.actions], recipients=recipients)

    @app.get("/health")
    def health() -> dict:
        return {"ok": True}

    api = Depends(require_token)

    @app.post("/context", response_model=ContextResponse, dependencies=[api])
    def context(req: ContextRequest) -> ContextResponse:
        # POST (not GET) so names and message text never end up in URLs or access logs
        with state.lock:
            person = state.resolve_sender(req.sender_name)
            day = today(req.received_at)
            return ContextResponse(person=person, today=day,
                                   candidates=state.candidates_for(person.id if person else None, day, req.text))

    @app.post("/updates", response_model=UpdateResponse, dependencies=[api])
    def updates(req: UpdateRequest, background: BackgroundTasks) -> UpdateResponse:
        with state.lock:
            resp = process(req)
            for update, result in zip(req.updates, resp.results):
                for action in result.actions:
                    state.draft_action(action, update.message_id, now())
            persist()
        if settings.webhook_url and (resp.discrepancies or resp.actions):
            background.add_task(post_hook, settings.webhook_url, settings.webhook_token, ConflictHook(
                message_id=req.updates[0].message_id, discrepancies=resp.discrepancies,
                actions=resp.actions, recipients=resp.recipients))
        return resp

    @app.get("/state", response_model=StateResponse, dependencies=[api])
    def get_state() -> StateResponse:
        with state.lock:
            return build_state()

    @app.get("/actions", response_model=list[ActionOut], dependencies=[api])
    def get_actions(status: Literal["open", "drafted", "sent", "failed", "all"] = "open") -> list[ActionOut]:
        wanted = {"open": {ActionStatus.drafted, ActionStatus.failed},
                  "all": set(ActionStatus)}.get(status) or {ActionStatus(status)}
        with state.lock:
            return [action_out(action) for action in state.actions.values() if action.status in wanted]

    @app.post("/actions/result", response_model=ActionOut, dependencies=[api])
    def post_action_result(req: ActionResultRequest) -> ActionOut:
        with state.lock:
            try:
                action = state.record_action_result(req.action_id, ActionStatus(req.status), req.detail, now())
            except KeyError:
                raise HTTPException(404, "unknown action_id")
            except ValueError as exc:
                raise HTTPException(409, str(exc))
            persist()
            return action_out(action)

    @app.get("/events", response_model=list[GraphEvent], dependencies=[api])
    def get_events(since: int = 0) -> list[GraphEvent]:
        with state.lock:
            return [e for e in state.events if (e.id or 0) > since]

    @app.post("/reset", dependencies=[api])
    def reset() -> dict:
        state.reset()
        persist()
        return {"ok": True, "tasks": len(state.tasks)}

    persist()
    return app
