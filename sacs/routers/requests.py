"""Lesson-completion request router: submit, review, forward, decide."""

from __future__ import annotations

from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel, Field
from sqlalchemy import func, select
from sqlalchemy.orm import Session as OrmSession

from ..db import get_db
from ..deps import AuthContext, get_auth_context, require_membership
from ..events import Topic, dispatch_pending
from ..models import (
    CompletionRequest,
    LessonSession,
    Membership,
    RequestAction,
    RequestState,
    TimetableEntry,
    TimetableStatus,
    User,
)
from ..rbac import Perm
from .. import workflow

router = APIRouter(prefix="/api/schools/{school_id}/requests", tags=["requests"])


def _with_lesson(db: OrmSession, request: CompletionRequest) -> dict:
    data = workflow.request_event(db, request)
    actions = (
        db.query(RequestAction)
        .filter(RequestAction.request_id == request.id)
        .order_by(RequestAction.created_at)
        .all()
    )
    data["actions"] = [
        {
            "action": a.action,
            "actor_role": a.actor_role,
            "from_state": a.from_state,
            "to_state": a.to_state,
            "reason": a.reason,
            "created_at": a.created_at.isoformat() + "Z",
        }
        for a in actions
    ]
    return data


@router.get("")
def list_requests(
    school_id: str,
    state: str | None = None,
    ctx: AuthContext = Depends(get_auth_context),
    db: OrmSession = Depends(get_db),
) -> list[dict]:
    viewed = require_membership(ctx, db, school_id, Perm.REQUEST_VIEW_SCHOOL)
    q = db.query(CompletionRequest).filter(CompletionRequest.school_id == school_id)
    if viewed.role == "teacher":
        q = q.filter(CompletionRequest.teacher_membership_id == viewed.id)
    if state:
        if state not in RequestState.ALL:
            raise HTTPException(status_code=422, detail="Invalid state filter")
        q = q.filter(CompletionRequest.state == state)
    rows = q.order_by(CompletionRequest.updated_at.desc()).all()
    return [_with_lesson(db, r) for r in rows]


class SubmitRequest(BaseModel):
    lesson_session_id: str
    note: str | None = Field(default=None, max_length=2000)


@router.post("", status_code=201)
def submit(
    school_id: str,
    payload: SubmitRequest,
    ctx: AuthContext = Depends(get_auth_context),
    db: OrmSession = Depends(get_db),
) -> dict:
    membership = require_membership(ctx, db, school_id, Perm.REQUEST_SUBMIT)
    lesson = db.get(LessonSession, payload.lesson_session_id)
    if lesson is None or lesson.school_id != school_id:
        raise HTTPException(status_code=404, detail="Lesson not found")
    # The lesson must come from a published timetable entry assigned to me.
    entry = db.get(TimetableEntry, lesson.timetable_entry_id)
    if entry is None or entry.school_id != school_id or entry.status != TimetableStatus.PUBLISHED:
        raise HTTPException(status_code=403, detail="Lesson is not from a published timetable")
    if entry.teacher_membership_id != membership.id:
        raise HTTPException(status_code=403, detail="Lesson is not assigned to you")
    try:
        request = workflow.create_request(db, lesson=lesson, membership=membership, user=ctx.user, note=payload.note)
        db.commit()
    except workflow.WorkflowError as exc:
        raise HTTPException(status_code=exc.status_code, detail=exc.message)
    dispatch_pending(db)
    return _with_lesson(db, request)


class TransitionRequest(BaseModel):
    action: str
    reason: str | None = Field(default=None, max_length=2000)
    note: str | None = Field(default=None, max_length=2000)


@router.post("/{request_id}/transition")
def transition(
    school_id: str,
    request_id: str,
    payload: TransitionRequest,
    ctx: AuthContext = Depends(get_auth_context),
    db: OrmSession = Depends(get_db),
) -> dict:
    request = db.get(CompletionRequest, request_id)
    if request is None or request.school_id != school_id:
        raise HTTPException(status_code=404, detail="Request not found")
    membership = require_membership(ctx, db, school_id, _perm_for(payload.action))
    try:
        result = workflow.apply_transition(
            db,
            request=request,
            action=payload.action,
            membership=membership,
            user=ctx.user,
            input={"reason": payload.reason, "note": payload.note},
        )
        db.commit()
    except workflow.WorkflowError as exc:
        raise HTTPException(status_code=exc.status_code, detail=exc.message)
    dispatch_pending(db)
    return _with_lesson(db, result)


@router.get("/{request_id}")
def request_detail(
    school_id: str,
    request_id: str,
    ctx: AuthContext = Depends(get_auth_context),
    db: OrmSession = Depends(get_db),
) -> dict:
    viewed = require_membership(ctx, db, school_id, Perm.REQUEST_VIEW_SCHOOL)
    request = db.get(CompletionRequest, request_id)
    if request is None or request.school_id != school_id:
        raise HTTPException(status_code=404, detail="Request not found")
    if viewed.role == "teacher" and request.teacher_membership_id != viewed.id:
        raise HTTPException(status_code=403, detail="Not your request")
    return _with_lesson(db, request)


def _perm_for(action: str) -> str:
    if action in ("approve", "reject"):
        return Perm.REQUEST_DECIDE
    if action in ("review", "forward", "return"):
        return Perm.REQUEST_REVIEW
    if action == "submit":
        return Perm.REQUEST_SUBMIT
    raise HTTPException(status_code=422, detail=f"Unknown transition action: {action}")