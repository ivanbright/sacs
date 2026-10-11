"""Academic structures router: classes, subjects, classrooms, teacher assignments."""

from __future__ import annotations

from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel, Field
from sqlalchemy.orm import Session as OrmSession

from ..db import get_db
from ..deps import AuthContext, get_auth_context, require_membership
from ..events import Topic, enqueue
from ..models import (
    Classroom,
    Membership,
    SchoolClass,
    Subject,
    TeacherAssignment,
    User,
    utcnow,
)
from ..rbac import Perm

router = APIRouter(prefix="/api/schools/{school_id}/academics", tags=["academics"])


def _require_academic(ctx: AuthContext, db: OrmSession, school_id: str):
    return require_membership(ctx, db, school_id, Perm.ACADEMICS_MANAGE)


class NameModel(BaseModel):
    name: str = Field(min_length=1, max_length=120)
    level: str | None = None
    code: str | None = None
    capacity: int | None = Field(default=None, ge=1, le=5000)


# --------------------------------------------------------------------------- #
# Classes
# --------------------------------------------------------------------------- #

@router.get("/classes")
def list_classes(
    school_id: str,
    ctx: AuthContext = Depends(get_auth_context),
    db: OrmSession = Depends(get_db),
) -> list[dict]:
    require_membership(ctx, db, school_id, Perm.SCHOOL_VIEW)
    rows = db.query(SchoolClass).filter_by(school_id=school_id).order_by(SchoolClass.name).all()
    return [
        {"id": c.id, "name": c.name, "level": c.level, "created_at": c.created_at.isoformat() + "Z"}
        for c in rows
    ]


@router.post("/classes", status_code=201)
def create_class(
    school_id: str, payload: NameModel,
    ctx: AuthContext = Depends(get_auth_context), db: OrmSession = Depends(get_db),
) -> dict:
    require_membership(ctx, db, school_id, Perm.ACADEMICS_MANAGE)
    if db.query(SchoolClass).filter_by(school_id=school_id, name=payload.name.strip()).first():
        raise HTTPException(status_code=409, detail="Class already exists")
    c = SchoolClass(school_id=school_id, name=payload.name.strip(), level=payload.level)
    db.add(c)
    enqueue(db, Topic.ACADEMICS, {"school_id": school_id, "audience": {"roles": ["school_head", "dos"]}, "data": {"action": "class_created"}})
    db.commit()
    return {"id": c.id, "name": c.name, "level": c.level}


# --------------------------------------------------------------------------- #
# Subjects
# --------------------------------------------------------------------------- #

@router.get("/subjects")
def list_subjects(
    school_id: str,
    ctx: AuthContext = Depends(get_auth_context),
    db: OrmSession = Depends(get_db),
) -> list[dict]:
    require_membership(ctx, db, school_id, Perm.SCHOOL_VIEW)
    rows = db.query(Subject).filter_by(school_id=school_id).order_by(Subject.name).all()
    return [{"id": s.id, "name": s.name, "code": s.code, "created_at": s.created_at.isoformat() + "Z"} for s in rows]


@router.post("/subjects", status_code=201)
def create_subject(
    school_id: str, payload: NameModel,
    ctx: AuthContext = Depends(get_auth_context), db: OrmSession = Depends(get_db),
) -> dict:
    require_membership(ctx, db, school_id, Perm.ACADEMICS_MANAGE)
    if db.query(Subject).filter_by(school_id=school_id, name=payload.name.strip()).first():
        raise HTTPException(status_code=409, detail="Subject already exists")
    s = Subject(school_id=school_id, name=payload.name.strip(), code=payload.code)
    db.add(s)
    enqueue(db, Topic.ACADEMICS, {"school_id": school_id, "audience": {"roles": ["school_head", "dos"]}, "data": {"action": "subject_created"}})
    db.commit()
    return {"id": s.id, "name": s.name, "code": s.code}


# --------------------------------------------------------------------------- #
# Classrooms
# --------------------------------------------------------------------------- #

@router.get("/classrooms")
def list_classrooms(
    school_id: str,
    ctx: AuthContext = Depends(get_auth_context),
    db: OrmSession = Depends(get_db),
) -> list[dict]:
    require_membership(ctx, db, school_id, Perm.SCHOOL_VIEW)
    rows = db.query(Classroom).filter_by(school_id=school_id).order_by(Classroom.name).all()
    return [
        {"id": c.id, "name": c.name, "capacity": c.capacity, "created_at": c.created_at.isoformat() + "Z"}
        for c in rows
    ]


@router.post("/classrooms", status_code=201)
def create_classroom(
    school_id: str, payload: NameModel,
    ctx: AuthContext = Depends(get_auth_context), db: OrmSession = Depends(get_db),
) -> dict:
    require_membership(ctx, db, school_id, Perm.ACADEMICS_MANAGE)
    if db.query(Classroom).filter_by(school_id=school_id, name=payload.name.strip()).first():
        raise HTTPException(status_code=409, detail="Classroom already exists")
    c = Classroom(school_id=school_id, name=payload.name.strip(), capacity=payload.capacity)
    db.add(c)
    enqueue(db, Topic.ACADEMICS, {"school_id": school_id, "audience": {"roles": ["school_head", "dos"]}, "data": {"action": "classroom_created"}})
    db.commit()
    return {"id": c.id, "name": c.name, "capacity": c.capacity}


# --------------------------------------------------------------------------- #
# Teacher assignments
# --------------------------------------------------------------------------- #

class AssignmentCreate(BaseModel):
    teacher_membership_id: str
    class_id: str
    subject_id: str
    classroom_id: str | None = None


@router.get("/assignments")
def list_assignments(
    school_id: str,
    ctx: AuthContext = Depends(get_auth_context),
    db: OrmSession = Depends(get_db),
) -> list[dict]:
    require_membership(ctx, db, school_id, Perm.SCHOOL_VIEW)
    rows = db.query(TeacherAssignment).filter_by(school_id=school_id).all()
    memberships = {m.id: m for m in db.query(Membership).filter(Membership.school_id == school_id).all()}
    users = {u.id: u for u in db.query(User).filter(User.id.in_([m.user_id for m in memberships.values()])).all()}
    classes = {c.id: c for c in db.query(SchoolClass).filter_by(school_id=school_id).all()}
    subjects = {s.id: s for s in db.query(Subject).filter_by(school_id=school_id).all()}
    classrooms = {c.id: c for c in db.query(Classroom).filter_by(school_id=school_id).all()}
    out = []
    for a in rows:
        m = memberships.get(a.teacher_membership_id)
        out.append(
            {
                "id": a.id,
                "teacher_membership_id": a.teacher_membership_id,
                "teacher": users[m.user_id].display_name if m else None,
                "class": classes[a.class_id].name if a.class_id in classes else None,
                "subject": subjects[a.subject_id].name if a.subject_id in subjects else None,
                "classroom": classrooms[a.classroom_id].name if a.classroom_id and a.classroom_id in classrooms else None,
            }
        )
    return out


@router.post("/assignments", status_code=201)
def create_assignment(
    school_id: str, payload: AssignmentCreate,
    ctx: AuthContext = Depends(get_auth_context), db: OrmSession = Depends(get_db),
) -> dict:
    require_membership(ctx, db, school_id, Perm.ACADEMICS_MANAGE)
    target = db.get(Membership, payload.teacher_membership_id)
    if target is None or target.school_id != school_id or target.role != "teacher":
        raise HTTPException(status_code=422, detail="Teacher membership not found in this school")
    for entity, model in (("class", SchoolClass), ("subject", Subject), ("classroom", Classroom)):
        entity_id = getattr(payload, f"{entity}_id")
        if not entity_id:
            continue
        row = db.get(model, entity_id)
        if row is None or row.school_id != school_id:
            raise HTTPException(status_code=422, detail=f"Unknown {entity} in this school")
    a = TeacherAssignment(
        school_id=school_id,
        teacher_membership_id=payload.teacher_membership_id,
        class_id=payload.class_id,
        subject_id=payload.subject_id,
        classroom_id=payload.classroom_id,
    )
    db.add(a)
    enqueue(db, Topic.ACADEMICS, {"school_id": school_id, "audience": {"roles": ["school_head", "dos"]}, "data": {"action": "assignment_created"}})
    db.commit()
    return {"id": a.id, "teacher_membership_id": a.teacher_membership_id, "class_id": a.class_id, "subject_id": a.subject_id}