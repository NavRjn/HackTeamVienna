"""Local HTTP service: n8n posts interpreted updates here, we check them against the graph
deterministically and call the n8n webhook only when there is something to tell people.

Start it with `uv run python -m kraftsync.app.serve` (never expose `create_app` without a token).
Contract: docs/api.md. No `from __future__ import annotations` on purpose (FastAPI introspection).
"""

import logging
import secrets
import sys
import time
from datetime import date, datetime
from pathlib import Path
from typing import Literal

import httpx
from fastapi import BackgroundTasks, Depends, FastAPI, File, Form, Header, HTTPException, Request, UploadFile
from fastapi.exception_handlers import http_exception_handler, request_validation_exception_handler
from fastapi.exceptions import RequestValidationError
from fastapi.responses import FileResponse
from starlette.exceptions import HTTPException as StarletteHTTPException

from kraftsync.app.api_models import (
    ActionOut,
    ActionResultRequest,
    ClaimResult,
    ConflictHook,
    ContextRequest,
    ContextResponse,
    ActionOut,
    ActionResultRequest,
    DocumentCategory,
    DocumentView,
    Recipient,
    StateResponse,
    TaskView,
    UpdateRequest,
    UpdateResponse,
)
from kraftsync.app.settings import Settings
from kraftsync.app.documents import DocumentStore, MEDIA_TYPES
from kraftsync.app.state import ProjectState
from kraftsync.contracts import Action, ActionStatus, EventKind, GraphEvent, TaskUpdate, UpdateKind
from kraftsync.engine.check import check, forecast, project_finish
from kraftsync.engine.resolve import acknowledge, ask_clarification, resolve_all

log = logging.getLogger("kraftsync.api")
MAX_FORECAST_DAYS = 60   # sanity bound on dates the LLM reports, relative to today
QUIET_GETS = {"/health", "/state", "/events"}   # UI polling would bury the real calls; errors on them are still logged


def setup_logging(log_path: Path | None) -> None:
    """One compact line per call to the console and (if set) a file. Never logs tokens or message text."""
    root = logging.getLogger("kraftsync")
    root.setLevel(logging.INFO)
    root.propagate = False
    for h in list(root.handlers):
        root.removeHandler(h)
    console = logging.StreamHandler(sys.stderr)
    console.setFormatter(logging.Formatter("%(asctime)s %(message)s", "%H:%M:%S"))
    root.addHandler(console)
    if log_path:
        log_path.parent.mkdir(parents=True, exist_ok=True)
        fh = logging.FileHandler(log_path, encoding="utf-8")
        fh.setFormatter(logging.Formatter("%(asctime)s %(message)s", "%Y-%m-%d %H:%M:%S"))
        root.addHandler(fh)


def post_hook(url: str, token: str | None, hook: ConflictHook) -> None:
    """Call the n8n webhook. Never raises; never logs the URL (it may contain a secret path)."""
    headers = {"Content-Type": "application/json"}
    if token:
        headers["X-KraftSync-Token"] = token
    try:
        r = httpx.post(url, content=hook.model_dump_json(), headers=headers, timeout=5.0)
        r.raise_for_status()
        log.info("hook -> n8n %d (%d actions)", r.status_code, len(hook.actions))
    except httpx.HTTPError as exc:
        log.warning("hook -> n8n FAILED: %s", type(exc).__name__)


def create_app(settings: Settings | None = None, state: ProjectState | None = None) -> FastAPI:
    settings = settings or Settings.from_env()
    settings.validate()
    setup_logging(settings.log_path)
    state = state or ProjectState(settings.plan_path, settings.events_path, settings.snapshot_path)
    log.info("service up | events=%d | webhook=%s | demo_today=%s | stage_redirect=%s | log=%s", len(state.events),
             "set" if settings.webhook_url else "off", settings.demo_today or "-",
             "on" if settings.redirect_chat_id else "off", settings.log_path.name if settings.log_path else "console only")
    documents = DocumentStore(settings.documents_path)
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
        t0 = time.perf_counter()
        try:
            response = await call_next(request)
        except Exception as exc:   # unhandled -> 500; log the type only, then let it propagate
            log.error("%s %s 500 | unhandled %s", request.method, request.url.path, type(exc).__name__)
            raise
        detail = getattr(request.state, "log", "")
        if not (request.method == "GET" and request.url.path in QUIET_GETS and response.status_code < 400):
            log.info("%s %s %d %dms%s", request.method, request.url.path, response.status_code,
                     (time.perf_counter() - t0) * 1000, f" | {detail}" if detail else "")
        return response

    @app.exception_handler(StarletteHTTPException)
    async def http_error(request: Request, exc: StarletteHTTPException):
        request.state.log = str(exc.detail)
        return await http_exception_handler(request, exc)

    @app.exception_handler(RequestValidationError)
    async def invalid_body(request: Request, exc: RequestValidationError):
        # field names and messages only, never the submitted values
        request.state.log = "invalid request: " + "; ".join(
            f"{'.'.join(str(x) for x in e['loc'])}: {e['msg']}" for e in exc.errors()[:5])
        return await request_validation_exception_handler(request, exc)

    def today(received_at: datetime | None = None) -> date:
        """DEMO_TODAY (stage override) wins; else the message date; else the real date."""
        return settings.demo_today or (received_at.date() if received_at else date.today())

    def now() -> datetime:
        return datetime.now().astimezone()

    def recipient_for(person_id: str) -> Recipient:
        p = state.persons[person_id]
        return Recipient(person_id=p.id, name=p.name, telegram_chat_id=settings.redirect_chat_id or p.telegram_chat_id)

    def finalize(actions: list[Action]) -> tuple[list[Action], dict[str, Recipient]]:
        """Attach chat ids; in stage mode redirect everything to one chat, prefixed with the real recipient."""
        out, recipients = [], {}
        for a in actions:
            if settings.redirect_chat_id:
                a = a.model_copy(update={"text": f"[an {state.persons[a.to_person_id].name}] {a.text}"})
            out.append(a)
            recipients[a.to_person_id] = recipient_for(a.to_person_id)
        return out, recipients

    def action_out(a: Action) -> ActionOut:
        return ActionOut(**a.model_dump(), recipient=recipient_for(a.to_person_id), detail=state.action_detail.get(a.id))

    def build_state() -> StateResponse:
        fc = forecast(state.tasks, state.deps)
        views = [TaskView(**t.model_dump(), cpm_start=fc[t.id][0], cpm_finish=fc[t.id][1]) for t in state.tasks.values()]
        base = {t.id: t for t in state.plan.tasks}
        return StateResponse(
            project_id=state.plan.project_id, name=state.plan.name, today=today(),
            project_finish_planned=project_finish(forecast(base, state.deps), base, state.deps),
            project_finish_forecast=project_finish(fc, state.tasks, state.deps),
            tasks=views, dependencies=state.deps, open_discrepancies=list(state.discrepancies))

    def persist() -> None:
        """Rewrite the live graph snapshot (data/live/graph.json). Never fails a request."""
        try:
            state.write_snapshot(build_state().model_dump_json(indent=2))
        except OSError as exc:   # e.g. a reader holds the file on Windows; the next change rewrites it
            log.warning("snapshot write failed: %s", type(exc).__name__)

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
            mine, related = state.context_for(sender.id, day, req.text)
            options = [state.tasks[i] for i in u.candidate_task_ids if i in state.tasks] or mine or related
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
                raise HTTPException(422, f"unknown task_id {u.task_id!r}; choose from the /context tasks")
            if u.new_forecast_finish and abs((u.new_forecast_finish - day).days) > MAX_FORECAST_DAYS:
                raise HTTPException(422, "new_forecast_finish is implausibly far from today")

        t = now()
        sender = state.resolve_sender(req.sender_id)
        fresh = [u for u in req.updates if not state.has_message(u.message_id)]
        message_event = None
        if fresh:   # the raw message is logged once, even if it carries several claims
            message_event = state.log(
                EventKind.message_received, sender.id if sender else "unknown", t,
                source_message_id=fresh[0].message_id,
                payload={"channel": req.channel.value, "chat_ref": req.chat_ref, "text": req.text})
        results = [process_claim(u, req, sender, t, day) for u in req.updates]

        applied = [(r, u) for r, u in zip(results, req.updates) if r.status == "applied"]
        if sender and applied and state.has_open_clarification(sender.id):   # this message answers our question
            informed = {a.to_person_id for r, _ in applied for a in r.actions} - {sender.id}
            names = list(dict.fromkeys(state.tasks[u.task_id].name for _, u in applied))
            applied[0][0].actions.append(acknowledge(
                sender, names, [state.persons[i].name for i in sorted(informed)], applied[0][0].message_id, t))

        order = ["applied", "needs_clarification", "ignored", "duplicate"]
        recipients: dict[str, Recipient] = {}
        for r in results:
            r.actions, rec = finalize(r.actions)
            recipients |= rec
            for act in r.actions:   # outbox: the notifier pulls/receives these and reports back
                r.events.append(state.record_action(act, t, r.message_id))
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
    def context(req: ContextRequest, request: Request) -> ContextResponse:
        # POST (not GET) so ids and message text never end up in URLs or access logs
        with state.lock:
            person = state.resolve_sender(req.sender_id)
            day = today(req.received_at)
            mine, related = state.context_for(person.id if person else None, day, req.text)
            request.state.log = (f"sender={req.sender_id} -> {person.id if person else 'UNKNOWN (no person in the plan has this telegram id)'}"
                                 f" | today={day} tasks={len(mine)} related={len(related)}")
            return ContextResponse(person=person, today=day, tasks=mine, related_tasks=related)

    @app.post("/updates", response_model=UpdateResponse, dependencies=[api])
    def updates(req: UpdateRequest, background: BackgroundTasks, request: Request) -> UpdateResponse:
        with state.lock:
            resp = process(req)
            persist()
            who = state.resolve_sender(req.sender_id)
            claims = " ; ".join(
                f"{u.message_id} {u.kind.value} {u.task_id} c={u.confidence:.2f} -> {r.status}"
                + (f" [{','.join(d.type.value for d in r.discrepancies)}]" if r.discrepancies else "")
                + (f" actions={len(r.actions)}" if r.actions else "")
                for u, r in zip(req.updates, resp.results))
            request.state.log = f"sender={req.sender_id} -> {who.id if who else 'UNKNOWN'} | {claims}"
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
    def get_actions(request: Request,
                    status: Literal["open", "drafted", "sent", "failed", "all"] = "open") -> list[ActionOut]:
        """The outbox. `open` (default) = drafted + failed = still to send."""
        wanted = {"open": {ActionStatus.drafted, ActionStatus.failed}, "all": set(ActionStatus)}.get(status) or {ActionStatus(status)}
        with state.lock:
            out = [action_out(a) for a in state.actions.values() if a.status in wanted]
        request.state.log = f"status={status} n={len(out)}"
        return out

    @app.post("/actions/result", response_model=ActionOut, dependencies=[api])
    def post_action_result(req: ActionResultRequest, request: Request) -> ActionOut:
        with state.lock:
            try:
                a = state.record_action_result(req.action_id, ActionStatus(req.status), req.detail, now())
            except KeyError:
                raise HTTPException(404, "unknown action_id")
            except ValueError as exc:
                raise HTTPException(409, str(exc))
            persist()
            request.state.log = f"{a.to_person_id} {a.kind.value} -> {a.status.value}" + (f" ({req.detail})" if req.detail else "")
            return action_out(a)

    @app.get("/events", response_model=list[GraphEvent], dependencies=[api])
    def get_events(since: int = 0) -> list[GraphEvent]:
        with state.lock:
            return [e for e in state.events if (e.id or 0) > since]

    @app.get("/documents", response_model=list[DocumentView], dependencies=[api])
    def list_documents() -> list[DocumentView]:
        return [DocumentView(**item) for item in documents.list()]

    @app.post("/documents", response_model=DocumentView, status_code=201, dependencies=[api])
    async def upload_document(
        file: UploadFile = File(...),
        category: DocumentCategory = Form(...),
        revision_label: str | None = Form(default=None),
        location_ref: str | None = Form(default=None),
        task_ref: str | None = Form(default=None),
    ) -> DocumentView:
        record = await documents.save(file, category, revision_label, location_ref, task_ref)
        return DocumentView(**record)

    @app.get("/documents/{document_id}/content", dependencies=[api])
    def document_content(document_id: str, inline: bool = False) -> FileResponse:
        document, path, extension = documents.file(document_id)
        return FileResponse(
            path, media_type=MEDIA_TYPES[extension], filename=document["original_filename"],
            content_disposition_type="inline" if inline else "attachment",
        )

    @app.post("/reset", dependencies=[api])
    def reset(request: Request) -> dict:
        state.reset()
        persist()
        request.state.log = "baseline reloaded, live events cleared"
        return {"ok": True, "tasks": len(state.tasks)}

    persist()   # data/live/graph.json exists from the first start on
    return app
