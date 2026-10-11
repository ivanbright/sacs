"""Lesson-completion workflow specified as an explicit state machine.

States
------
``submitted``                Awaiting DOS review.
``under_dos_review``         DOS has taken the request for review.
``forwarded_to_head``        DOS reviewed and forwarded; awaiting the head.
``approved`` / ``rejected``  Terminal decisions by the school head.
``returned_for_correction``  DOS asked the teacher to correct/resubmit.

There is no persisted ``not_submitted`` row: a lesson with no request is simply
in that implicit state.

Every transition is validated here (actor role, active membership, school
status, permission, preconditions) and, on success, writes the request update,
an append-only ``RequestAction`` history row, an audit record, a notification,
and a transactional outbox event -- all in one database transaction.

No generic update endpoint can move a request between workflow states.
"""

from __future__ import annotations

from dataclasses import dataclass

from sqlalchemy.orm import Session as OrmSession

from . import audit as audit_mod
from .events import Topic, enqueue, notify
from .models import (
    CompletionRequest,
    LessonSession,
    Membership,
    RequestAction,
    RequestState,
    User,
    utcnow,
)
from .rbac import Perm, effective_permissions


class WorkflowError(Exception):
    def __init__(self, message: str, status_code: int = 422, code: str = "workflow_error"):
        super().__init__(message)
        self.message = message
        self.status_code = status_code
        self.code = code


@dataclass(frozen=True)
class TransitionSpec:
    action: str
    actor_roles: tuple[str, ...]
    permission: str
    allowed_from: tuple[str, ...]
    to: str
    requires_input: str | None = None  # field name


# Review/forward/return can be performed by either the Director (school_head) or a
# dedicated Director-of-Studies membership when one exists. In the initial
# three-role model the Director exercises the whole chain from submission to
# final decision.
_REVIEWERS = ("dos", "school_head")

# States and their allowed transitions.
TRANSITIONS: dict[str, list[TransitionSpec]] = {
    RequestState.SUBMITTED: [
        TransitionSpec("review", _REVIEWERS, Perm.REQUEST_REVIEW, (RequestState.SUBMITTED,), RequestState.UNDER_DOS_REVIEW),
        TransitionSpec("forward", _REVIEWERS, Perm.REQUEST_REVIEW, (RequestState.SUBMITTED,), RequestState.FORWARDED_TO_HEAD),
        TransitionSpec("return", _REVIEWERS, Perm.REQUEST_REVIEW, (RequestState.SUBMITTED,), RequestState.RETURNED_FOR_CORRECTION, requires_input="reason"),
    ],
    RequestState.UNDER_DOS_REVIEW: [
        TransitionSpec("forward", _REVIEWERS, Perm.REQUEST_REVIEW, (RequestState.UNDER_DOS_REVIEW,), RequestState.FORWARDED_TO_HEAD),
        TransitionSpec("return", _REVIEWERS, Perm.REQUEST_REVIEW, (RequestState.UNDER_DOS_REVIEW,), RequestState.RETURNED_FOR_CORRECTION, requires_input="reason"),
    ],
    RequestState.FORWARDED_TO_HEAD: [
        TransitionSpec("approve", ("school_head",), Perm.REQUEST_DECIDE, (RequestState.FORWARDED_TO_HEAD,), RequestState.APPROVED),
        TransitionSpec("reject", ("school_head",), Perm.REQUEST_DECIDE, (RequestState.FORWARDED_TO_HEAD,), RequestState.REJECTED, requires_input="reason"),
    ],
    RequestState.RETURNED_FOR_CORRECTION: [
        TransitionSpec("submit", ("teacher",), Perm.REQUEST_SUBMIT, (RequestState.RETURNED_FOR_CORRECTION,), RequestState.SUBMITTED),
    ],
}


def _find_spec(request: CompletionRequest, action: str) -> TransitionSpec | None:
    for spec in TRANSITIONS.get(request.state, []):
        if spec.action == action:
            return spec
    return None


def _validate_actor(request, membership: Membership, user: User, spec: TransitionSpec) -> None:
    """Actor must hold the right role *and* be the request's legitimately bound
    participant (e.g. only the assigned teacher may resubmit)."""
    if membership.status != "active":
        raise WorkflowError("Membership is not active", 403, "membership_inactive")
    if user.id != membership.user_id:
        raise WorkflowError("Session user does not match membership", 403, "identity_mismatch")
    perms = effective_permissions(user, membership)
    if spec.permission not in perms:
        raise WorkflowError(
            f"Role '{membership.role}' is not authorized for '{spec.action}'", 403, "forbidden"
        )
    if membership.role not in spec.actor_roles:
        wanted = "' or '".join(spec.actor_roles)
        raise WorkflowError(
            f"'{spec.action}' requires role '{wanted}', not '{membership.role}'",
            403,
            "wrong_role",
        )
    if request.teacher_membership_id == membership.id and spec.actor_roles == ("teacher",):
        return
    if membership.role == "teacher" and request.teacher_membership_id != membership.id:
        raise WorkflowError("A teacher may only act on their own requests", 403, "not_own_request")
    if set(spec.actor_roles) & {"dos", "school_head"}:
        return  # membership role matches spec.actor_roles already
    raise WorkflowError("Actor cannot perform this transition", 403, "forbidden")


def create_request(
    db: OrmSession,
    *,
    lesson: LessonSession,
    membership: Membership,
    user: User,
    note: str | None = None,
) -> CompletionRequest:
    """Teacher submits a completion request for an assigned lesson (first time)."""
    if membership.school_id != lesson.school_id:
        raise WorkflowError("Lesson does not belong to the teacher's school", 403, "cross_tenant")
    if membership.role != "teacher" or membership.status != "active":
        raise WorkflowError("Only an active teacher may submit", 403, "forbidden")
    existing = (
        db.query(CompletionRequest)
        .filter(
            CompletionRequest.school_id == lesson.school_id,
            CompletionRequest.lesson_session_id == lesson.id,
        )
        .first()
    )
    if existing is not None:
        if existing.state in RequestState.OPEN:
            raise WorkflowError("This lesson already has an open request", 409, "duplicate")
        raise WorkflowError("This lesson already has a finalised request", 409, "duplicate_final")

    request = CompletionRequest(
        school_id=lesson.school_id,
        lesson_session_id=lesson.id,
        teacher_membership_id=membership.id,
        state=RequestState.SUBMITTED,
        note=note,
    )
    db.add(request)
    db.flush()

    db.add(
        RequestAction(
            request_id=request.id,
            school_id=request.school_id,
            actor_user_id=user.id,
            actor_role="teacher",
            action="submit",
            from_state="not_submitted",
            to_state=RequestState.SUBMITTED,
            reason=note,
        )
    )
    _after_change(db, request, user, membership)
    return request


def apply_transition(
    db: OrmSession,
    *,
    request: CompletionRequest,
    action: str,
    membership: Membership,
    user: User,
    input: dict | None = None,
) -> CompletionRequest:
    """Apply one validated workflow transition (single guarded codepath)."""
    input = input or {}
    spec = _find_spec(request, action)
    if spec is None:
        raise WorkflowError(
            f"'{action}' is not valid from state '{request.state}'", 422, "invalid_transition"
        )
    if request.state in RequestState.TERMINAL:
        raise WorkflowError("Terminal states cannot change", 422, "terminal")
    if membership.school_id != request.school_id:
        raise WorkflowError("Member does not belong to this request's school", 403, "cross_tenant")
    if request.teacher_membership_id == membership.id and spec.actor_role == "teacher":
        pass  # allowed path; _validate_actor confirms below
    _validate_actor(request, membership, user, spec)

    if spec.requires_input:
        value = (input or {}).get(spec.requires_input) or (request.note if spec.requires_input == "note" else None)
        if not value or not str(value).strip():
            raise WorkflowError(
                f"Field '{spec.requires_input}' is required for '{action}'", 422, "input_required"
            )

    from_state = request.state
    request.state = spec.to
    request.updated_at = utcnow()
    request.version += 1
    if input.get("note") is not None:
        request.note = str(input["note"])
    if spec.requires_input == "reason" and input.get(spec.requires_input):
        request.note = str(input[spec.requires_input])

    db.add(
        RequestAction(
            request_id=request.id,
            school_id=request.school_id,
            actor_user_id=user.id,
            actor_role=membership.role,
            action=action,
            from_state=from_state,
            to_state=spec.to,
            reason=input.get("reason") or input.get("note"),
        )
    )
    _after_change(db, request, user, membership)
    return request


def _lesson_for(db: OrmSession, request: CompletionRequest) -> LessonSession | None:
    return (
        db.query(LessonSession)
        .filter(
            LessonSession.id == request.lesson_session_id,
            LessonSession.school_id == request.school_id,
        )
        .first()
    )


def request_event(db: OrmSession, request: CompletionRequest) -> dict:
    lesson = _lesson_for(db, request)
    teacher_membership = (
        db.query(Membership).filter(Membership.id == request.teacher_membership_id).first()
    )
    teacher = (
        db.query(User).filter(User.id == teacher_membership.user_id).first()
        if teacher_membership
        else None
    )
    return {
        "id": request.id,
        "state": request.state,
        "version": request.version,
        "note": request.note,
        "school_id": request.school_id,
        "lesson_session_id": request.lesson_session_id,
        "lesson": {
            "id": lesson.id if lesson else None,
            "date": lesson.session_date if lesson else None,
            "start": lesson.start_at.isoformat() if lesson else None,
            "end": lesson.end_at.isoformat() if lesson else None,
        } if lesson else None,
        "teacher": {"id": teacher.id if teacher else None, "name": teacher.display_name if teacher else None},
        "updated_at": request.updated_at.isoformat() + "Z",
    }


def _after_change(db, request, user: User, membership: Membership) -> None:
    """Audit + notify + enqueue the real-time event within this transaction."""
    school_id = request.school_id
    ev = request_event(db, request)

    from .models import MembershipStatus

    teacher_membership = (
        db.query(Membership).filter(Membership.id == request.teacher_membership_id).first()
    )
    teacher = db.query(User).filter(User.id == teacher_membership.user_id).first() if teacher_membership else None
    head = (
        db.query(Membership)
        .filter(
            Membership.school_id == school_id,
            Membership.role == "school_head",
            Membership.status == MembershipStatus.ACTIVE,
        )
        .first()
    )
    head_user = db.query(User).filter(User.id == head.user_id).first() if head else None

    audit_mod.record(
        db,
        school_id=school_id,
        actor_user_id=user.id,
        actor_role=membership.role,
        action=f"request.{request.state}",
        summary=f"Lesson completion request {request.id[:8]} -> {request.state}",
        entity_type="completion_request",
        entity_id=request.id,
        meta={"state": request.state, "version": request.version},
    )

    titles = {
        "submitted": "Lesson completion submitted",
        "under_dos_review": "Request under DOS review",
        "forwarded_to_head": "Request forwarded to school head",
        "approved": "Lesson completion approved",
        "rejected": "Lesson completion rejected",
        "returned_for_correction": "Request returned for correction",
    }

    teacher_id = teacher.id if teacher else None
    dos = head_id = None
    if request.state != RequestState.SUBMITTED:
        dos_membership = (
            db.query(Membership)
            .filter(
                Membership.school_id == school_id,
                Membership.role == "dos",
                Membership.status == MembershipStatus.ACTIVE,
            )
            .first()
        )
        dos = db.query(User).filter(User.id == dos_membership.user_id).first() if dos_membership else None
        head_id = head_user.id if head_user else None

    if request.state == RequestState.SUBMITTED:
        audience = {"roles": ["dos", "school_head"]}
        ev["audience_label"] = "reviewer"
    elif request.state == RequestState.UNDER_DOS_REVIEW:
        audience = {"users": [teacher_id]} if teacher_id else {"roles": ["teacher"]}
    elif request.state == RequestState.FORWARDED_TO_HEAD:
        audience = {"users": [head_id]} if head_id else {"roles": ["school_head"]}
    elif request.state in (RequestState.APPROVED, RequestState.REJECTED):
        audience = {"users": [teacher_id] if teacher_id else [], "roles": ["dos"]}
    elif request.state == RequestState.RETURNED_FOR_CORRECTION:
        audience = {"users": [teacher_id]} if teacher_id else {"roles": ["teacher"]}
    else:
        audience = {"roles": ["dos", "school_head"]}

    ev["action"] = {
        "submitted": "submitted",
        "under_dos_review": "in_review",
        "forwarded_to_head": "forwarded",
        "returned_for_correction": "returned",
    }.get(request.state, request.state)

    # Targeted notifications (kept lightweight; full history lives in request actions).
    if request.state == RequestState.SUBMITTED:
        for reviewer in (dos, head_user):
            if reviewer is not None:
                notify(
                    db, school_id=school_id, user_id=reviewer.id, type=f"request.{request.state}",
                    title=titles[request.state], body="A teacher submitted a lesson completion request.",
                    entity_type="completion_request", entity_id=request.id,
                )
    elif request.state in (RequestState.UNDER_DOS_REVIEW, RequestState.RETURNED_FOR_CORRECTION) and teacher:
        notify(
            db, school_id=school_id, user_id=teacher.id, type=f"request.{request.state}",
            title=titles[request.state], body=f"Your lesson completion request was updated.",
            entity_type="completion_request", entity_id=request.id,
        )
    elif request.state == RequestState.FORWARDED_TO_HEAD and head_user:
        notify(
            db, school_id=school_id, user_id=head_user.id, type=f"request.{request.state}",
            title=titles[request.state], body=f"A reviewed lesson completion request awaits your decision.",
            entity_type="completion_request", entity_id=request.id,
        )
    elif request.state in (RequestState.APPROVED, RequestState.REJECTED):
        if teacher:
            notify(
                db, school_id=school_id, user_id=teacher.id, type=f"request.{request.state}",
                title=titles[request.state], body=f"The school head made a final decision on your request.",
                entity_type="completion_request", entity_id=request.id,
            )
        if dos:
            notify(
                db, school_id=school_id, user_id=dos.id, type=f"request.{request.state}",
                title=titles[request.state], body=f"The school head decided on a request you reviewed.",
                entity_type="completion_request", entity_id=request.id,
            )

    enqueue(
        db,
        Topic.REQUESTS,
        {
            "school_id": school_id,
            "audience": audience,
            "data": ev,
        },
    )


def _notify_role(state: str) -> str | None:
    return None