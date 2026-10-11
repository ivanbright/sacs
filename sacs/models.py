"""SQLAlchemy domain model for SACS.

Design notes
------------
* Tenant-scoped entities carry a ``school_id`` foreign key and are indexed on it.
  Tenant isolation is enforced in the service/authorization layer and is *never*
  delegated to a frontend filter.
* Primary keys are opaque UUID strings to avoid trivially enumerable identifiers
  (defence in depth against insecure direct object references).
* Approval history (``RequestAction``) and audit records (``AuditEvent``) are
  append-only from the application's perspective.
* A global user (:class:`User`) may belong to multiple schools through
  :class:`Membership`; roles/permissions are always evaluated for a specific
  school membership, never for the user globally.
"""

from __future__ import annotations

import uuid
from datetime import datetime, timezone

from sqlalchemy import (
    JSON,
    Boolean,
    DateTime,
    ForeignKey,
    Index,
    Integer,
    String,
    Text,
    UniqueConstraint,
)
from sqlalchemy.orm import DeclarativeBase, Mapped, mapped_column, relationship
from sqlalchemy.types import TypeDecorator


def new_id() -> str:
    return str(uuid.uuid4())


def utcnow() -> datetime:
    """Time-zone-aware UTC timestamp (stored naive, read back aware)."""
    return datetime.now(timezone.utc)


class UTCDateTime(TypeDecorator):
    """Store naive UTC datetimes; always read back as timezone-aware UTC."""

    impl = DateTime
    cache_ok = True

    def process_bind_param(self, value, dialect):  # pragma: no cover - trivial
        if value is None:
            return None
        if value.tzinfo is not None:
            value = value.astimezone(timezone.utc).replace(tzinfo=None)
        return value

    def process_result_value(self, value, dialect):  # pragma: no cover - trivial
        if value is None:
            return None
        return value.replace(tzinfo=timezone.utc)


class Base(DeclarativeBase):
    pass


# --------------------------------------------------------------------------- #
# Role / status constants
# --------------------------------------------------------------------------- #

class Role:
    SCHOOL_HEAD = "school_head"
    DOS = "dos"
    TEACHER = "teacher"
    BELL_MONITOR = "bell_monitor"

    ALL = (SCHOOL_HEAD, DOS, TEACHER, BELL_MONITOR)
    LABELS = {
        SCHOOL_HEAD: "School Head",
        DOS: "Director of Studies",
        TEACHER: "Teacher",
        BELL_MONITOR: "Bell & Schedule Monitor",
    }


class MembershipStatus:
    INVITED = "invited"
    ACTIVE = "active"
    DEACTIVATED = "deactivated"


class SchoolStatus:
    PROVISIONING = "provisioning"
    ACTIVE = "active"
    SUSPENDED = "suspended"


class TimetableStatus:
    DRAFT = "draft"
    PUBLISHED = "published"


class RequestState:
    SUBMITTED = "submitted"
    UNDER_DOS_REVIEW = "under_dos_review"
    FORWARDED_TO_HEAD = "forwarded_to_head"
    APPROVED = "approved"
    REJECTED = "rejected"
    RETURNED_FOR_CORRECTION = "returned_for_correction"

    ALL = (
        SUBMITTED,
        UNDER_DOS_REVIEW,
        FORWARDED_TO_HEAD,
        APPROVED,
        REJECTED,
        RETURNED_FOR_CORRECTION,
    )
    TERMINAL = (APPROVED, REJECTED)
    OPEN = (SUBMITTED, UNDER_DOS_REVIEW, FORWARDED_TO_HEAD, RETURNED_FOR_CORRECTION)


class BellOutcome:
    FIRED = "fired"
    MISSED = "missed"
    SKIPPED = "skipped"
    FAILED = "failed"


# --------------------------------------------------------------------------- #
# Identity & tenant
# --------------------------------------------------------------------------- #

class User(Base):
    __tablename__ = "users"

    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=new_id)
    email: Mapped[str] = mapped_column(String(255), unique=True, nullable=False, index=True)
    password_hash: Mapped[str] = mapped_column(String(255), nullable=False)
    display_name: Mapped[str] = mapped_column(String(255), nullable=False)
    is_platform_admin: Mapped[bool] = mapped_column(Boolean, default=False, nullable=False)
    is_demo: Mapped[bool] = mapped_column(Boolean, default=False, nullable=False)
    is_active: Mapped[bool] = mapped_column(Boolean, default=True, nullable=False)
    created_at: Mapped[datetime] = mapped_column(UTCDateTime, default=utcnow, nullable=False)

    memberships: Mapped[list["Membership"]] = relationship(
        back_populates="user", cascade="all, delete-orphan"
    )


class School(Base):
    __tablename__ = "schools"

    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=new_id)
    name: Mapped[str] = mapped_column(String(255), nullable=False)
    slug: Mapped[str] = mapped_column(String(120), unique=True, nullable=False, index=True)
    status: Mapped[str] = mapped_column(String(32), default=SchoolStatus.ACTIVE, nullable=False)
    timezone: Mapped[str] = mapped_column(String(64), default="UTC", nullable=False)
    is_demo: Mapped[bool] = mapped_column(Boolean, default=False, nullable=False)
    created_at: Mapped[datetime] = mapped_column(UTCDateTime, default=utcnow, nullable=False)
    verified_at: Mapped[datetime | None] = mapped_column(UTCDateTime, nullable=True)

    memberships: Mapped[list["Membership"]] = relationship(
        back_populates="school", cascade="all, delete-orphan"
    )


class Membership(Base):
    __tablename__ = "memberships"
    __table_args__ = (
        UniqueConstraint("user_id", "school_id", name="uq_membership_user_school"),
        Index("ix_membership_school_role", "school_id", "role"),
    )

    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=new_id)
    user_id: Mapped[str] = mapped_column(ForeignKey("users.id"), nullable=False, index=True)
    school_id: Mapped[str] = mapped_column(ForeignKey("schools.id"), nullable=False, index=True)
    role: Mapped[str] = mapped_column(String(32), nullable=False)
    status: Mapped[str] = mapped_column(String(32), default=MembershipStatus.ACTIVE, nullable=False)
    created_at: Mapped[datetime] = mapped_column(UTCDateTime, default=utcnow, nullable=False)
    activated_at: Mapped[datetime | None] = mapped_column(UTCDateTime, nullable=True)
    deactivated_at: Mapped[datetime | None] = mapped_column(UTCDateTime, nullable=True)

    user: Mapped[User] = relationship(back_populates="memberships")
    school: Mapped[School] = relationship(back_populates="memberships")


class Invitation(Base):
    __tablename__ = "invitations"
    __table_args__ = (Index("ix_invitation_school_status", "school_id", "status"),)

    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=new_id)
    school_id: Mapped[str] = mapped_column(ForeignKey("schools.id"), nullable=False, index=True)
    email: Mapped[str] = mapped_column(String(255), nullable=False)
    role: Mapped[str] = mapped_column(String(32), nullable=False)
    token_hash: Mapped[str] = mapped_column(String(128), nullable=False, unique=True)
    status: Mapped[str] = mapped_column(String(32), default="pending", nullable=False)
    invited_by: Mapped[str | None] = mapped_column(ForeignKey("users.id"), nullable=True)
    created_at: Mapped[datetime] = mapped_column(UTCDateTime, default=utcnow, nullable=False)
    expires_at: Mapped[datetime | None] = mapped_column(UTCDateTime, nullable=True)
    accepted_at: Mapped[datetime | None] = mapped_column(UTCDateTime, nullable=True)


class Session(Base):
    __tablename__ = "sessions"

    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=new_id)
    token_hash: Mapped[str] = mapped_column(String(128), nullable=False, unique=True, index=True)
    user_id: Mapped[str] = mapped_column(ForeignKey("users.id"), nullable=False, index=True)
    school_id: Mapped[str | None] = mapped_column(
        ForeignKey("schools.id"), nullable=True, index=True
    )
    created_at: Mapped[datetime] = mapped_column(UTCDateTime, default=utcnow, nullable=False)
    expires_at: Mapped[datetime] = mapped_column(UTCDateTime, nullable=False)
    revoked: Mapped[bool] = mapped_column(Boolean, default=False, nullable=False)
    label: Mapped[str | None] = mapped_column(String(64), nullable=True)


# --------------------------------------------------------------------------- #
# Academic structures
# --------------------------------------------------------------------------- #

class SchoolClass(Base):
    __tablename__ = "classes"
    __table_args__ = (
        UniqueConstraint("school_id", "name", name="uq_class_school_name"),
        Index("ix_class_school", "school_id"),
    )

    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=new_id)
    school_id: Mapped[str] = mapped_column(ForeignKey("schools.id"), nullable=False)
    name: Mapped[str] = mapped_column(String(120), nullable=False)
    level: Mapped[str | None] = mapped_column(String(60), nullable=True)
    created_at: Mapped[datetime] = mapped_column(UTCDateTime, default=utcnow, nullable=False)


class Subject(Base):
    __tablename__ = "subjects"
    __table_args__ = (
        UniqueConstraint("school_id", "name", name="uq_subject_school_name"),
        Index("ix_subject_school", "school_id"),
    )

    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=new_id)
    school_id: Mapped[str] = mapped_column(ForeignKey("schools.id"), nullable=False)
    name: Mapped[str] = mapped_column(String(120), nullable=False)
    code: Mapped[str | None] = mapped_column(String(32), nullable=True)
    created_at: Mapped[datetime] = mapped_column(UTCDateTime, default=utcnow, nullable=False)


class Classroom(Base):
    __tablename__ = "classrooms"
    __table_args__ = (
        UniqueConstraint("school_id", "name", name="uq_classroom_school_name"),
        Index("ix_classroom_school", "school_id"),
    )

    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=new_id)
    school_id: Mapped[str] = mapped_column(ForeignKey("schools.id"), nullable=False)
    name: Mapped[str] = mapped_column(String(120), nullable=False)
    capacity: Mapped[int | None] = mapped_column(Integer, nullable=True)
    created_at: Mapped[datetime] = mapped_column(UTCDateTime, default=utcnow, nullable=False)


class TeacherAssignment(Base):
    __tablename__ = "teacher_assignments"
    __table_args__ = (Index("ix_assignment_school", "school_id"),)

    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=new_id)
    school_id: Mapped[str] = mapped_column(ForeignKey("schools.id"), nullable=False)
    teacher_membership_id: Mapped[str] = mapped_column(
        ForeignKey("memberships.id"), nullable=False, index=True
    )
    class_id: Mapped[str] = mapped_column(ForeignKey("classes.id"), nullable=False)
    subject_id: Mapped[str] = mapped_column(ForeignKey("subjects.id"), nullable=False)
    classroom_id: Mapped[str | None] = mapped_column(ForeignKey("classrooms.id"), nullable=True)
    created_at: Mapped[datetime] = mapped_column(UTCDateTime, default=utcnow, nullable=False)


class TimetableEntry(Base):
    __tablename__ = "timetable_entries"
    __table_args__ = (Index("ix_timetable_school_status", "school_id", "status"),)

    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=new_id)
    school_id: Mapped[str] = mapped_column(ForeignKey("schools.id"), nullable=False)
    class_id: Mapped[str] = mapped_column(ForeignKey("classes.id"), nullable=False)
    subject_id: Mapped[str] = mapped_column(ForeignKey("subjects.id"), nullable=False)
    classroom_id: Mapped[str | None] = mapped_column(ForeignKey("classrooms.id"), nullable=True)
    teacher_membership_id: Mapped[str] = mapped_column(
        ForeignKey("memberships.id"), nullable=False
    )
    day_of_week: Mapped[int] = mapped_column(Integer, nullable=False)  # 0=Monday
    start_time: Mapped[str] = mapped_column(String(5), nullable=False)  # "HH:MM"
    end_time: Mapped[str] = mapped_column(String(5), nullable=False)  # "HH:MM"
    status: Mapped[str] = mapped_column(
        String(32), default=TimetableStatus.DRAFT, nullable=False
    )
    academic_term: Mapped[str | None] = mapped_column(String(60), nullable=True)
    created_at: Mapped[datetime] = mapped_column(UTCDateTime, default=utcnow, nullable=False)


class LessonSession(Base):
    __tablename__ = "lesson_sessions"
    __table_args__ = (
        UniqueConstraint(
            "timetable_entry_id", "session_date", name="uq_lesson_entry_date"
        ),
        Index("ix_lesson_school_date", "school_id", "session_date"),
    )

    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=new_id)
    school_id: Mapped[str] = mapped_column(ForeignKey("schools.id"), nullable=False)
    timetable_entry_id: Mapped[str] = mapped_column(
        ForeignKey("timetable_entries.id"), nullable=False, index=True
    )
    session_date: Mapped[str] = mapped_column(String(10), nullable=False)  # YYYY-MM-DD
    start_at: Mapped[datetime] = mapped_column(UTCDateTime, nullable=False)
    end_at: Mapped[datetime] = mapped_column(UTCDateTime, nullable=False)
    created_at: Mapped[datetime] = mapped_column(UTCDateTime, default=utcnow, nullable=False)


# --------------------------------------------------------------------------- #
# Lesson completion workflow
# --------------------------------------------------------------------------- #

class CompletionRequest(Base):
    __tablename__ = "completion_requests"
    __table_args__ = (
        Index("ix_request_school_state", "school_id", "state"),
        Index("ix_request_teacher", "teacher_membership_id"),
    )

    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=new_id)
    school_id: Mapped[str] = mapped_column(ForeignKey("schools.id"), nullable=False)
    lesson_session_id: Mapped[str] = mapped_column(
        ForeignKey("lesson_sessions.id"), nullable=False, index=True
    )
    teacher_membership_id: Mapped[str] = mapped_column(
        ForeignKey("memberships.id"), nullable=False
    )
    state: Mapped[str] = mapped_column(String(40), default=RequestState.SUBMITTED, nullable=False)
    note: Mapped[str | None] = mapped_column(Text, nullable=True)
    version: Mapped[int] = mapped_column(Integer, default=1, nullable=False)
    created_at: Mapped[datetime] = mapped_column(UTCDateTime, default=utcnow, nullable=False)
    updated_at: Mapped[datetime] = mapped_column(UTCDateTime, default=utcnow, nullable=False)

    actions: Mapped[list["RequestAction"]] = relationship(
        back_populates="request", cascade="all, delete-orphan"
    )


class RequestAction(Base):
    __tablename__ = "request_actions"
    __table_args__ = (Index("ix_action_request", "request_id", "created_at"),)

    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=new_id)
    request_id: Mapped[str] = mapped_column(
        ForeignKey("completion_requests.id"), nullable=False
    )
    school_id: Mapped[str] = mapped_column(ForeignKey("schools.id"), nullable=False)
    actor_user_id: Mapped[str] = mapped_column(ForeignKey("users.id"), nullable=False)
    actor_role: Mapped[str] = mapped_column(String(32), nullable=False)
    action: Mapped[str] = mapped_column(String(40), nullable=False)
    from_state: Mapped[str] = mapped_column(String(40), nullable=False)
    to_state: Mapped[str] = mapped_column(String(40), nullable=False)
    reason: Mapped[str | None] = mapped_column(Text, nullable=True)
    created_at: Mapped[datetime] = mapped_column(UTCDateTime, default=utcnow, nullable=False)

    request: Mapped[CompletionRequest] = relationship(back_populates="actions")


# --------------------------------------------------------------------------- #
# Bell scheduling
# --------------------------------------------------------------------------- #

class BellSchedule(Base):
    __tablename__ = "bell_schedules"
    __table_args__ = (Index("ix_bell_schedule_school", "school_id", "is_active"),)

    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=new_id)
    school_id: Mapped[str] = mapped_column(ForeignKey("schools.id"), nullable=False)
    name: Mapped[str] = mapped_column(String(120), nullable=False)
    timezone: Mapped[str] = mapped_column(String(64), default="UTC", nullable=False)
    is_active: Mapped[bool] = mapped_column(Boolean, default=True, nullable=False)
    paused: Mapped[bool] = mapped_column(Boolean, default=False, nullable=False)
    periods: Mapped[list] = mapped_column(JSON, default=list, nullable=False)
    # Weekdays (0=Monday .. 6=Sunday) on which the schedule applies. ``None``
    # means every day, preserving the behaviour of schedules created before the
    # column existed.
    days: Mapped[list | None] = mapped_column(JSON, nullable=True)
    created_at: Mapped[datetime] = mapped_column(UTCDateTime, default=utcnow, nullable=False)
    updated_at: Mapped[datetime] = mapped_column(UTCDateTime, default=utcnow, nullable=False)


class BellEvent(Base):
    __tablename__ = "bell_events"
    __table_args__ = (
        UniqueConstraint("schedule_id", "occurrence_key", name="uq_bell_occurrence"),
        Index("ix_bell_event_school", "school_id", "scheduled_at"),
    )

    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=new_id)
    school_id: Mapped[str] = mapped_column(ForeignKey("schools.id"), nullable=False)
    schedule_id: Mapped[str] = mapped_column(
        ForeignKey("bell_schedules.id"), nullable=False, index=True
    )
    occurrence_key: Mapped[str] = mapped_column(String(160), nullable=False)
    label: Mapped[str] = mapped_column(String(120), nullable=False)
    event_type: Mapped[str] = mapped_column(String(40), nullable=False)  # start|end
    scheduled_at: Mapped[datetime] = mapped_column(UTCDateTime, nullable=False)
    fired_at: Mapped[datetime | None] = mapped_column(UTCDateTime, nullable=True)
    outcome: Mapped[str] = mapped_column(String(32), nullable=False)
    detail: Mapped[str | None] = mapped_column(Text, nullable=True)
    created_at: Mapped[datetime] = mapped_column(UTCDateTime, default=utcnow, nullable=False)


# --------------------------------------------------------------------------- #
# Notifications, audit, outbox
# --------------------------------------------------------------------------- #

class Notification(Base):
    __tablename__ = "notifications"
    __table_args__ = (Index("ix_notification_user", "user_id", "read"),)

    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=new_id)
    school_id: Mapped[str | None] = mapped_column(ForeignKey("schools.id"), nullable=True)
    user_id: Mapped[str | None] = mapped_column(ForeignKey("users.id"), nullable=True, index=True)
    audience_role: Mapped[str | None] = mapped_column(String(32), nullable=True)
    type: Mapped[str] = mapped_column(String(64), nullable=False)
    title: Mapped[str] = mapped_column(String(255), nullable=False)
    body: Mapped[str | None] = mapped_column(Text, nullable=True)
    entity_type: Mapped[str | None] = mapped_column(String(64), nullable=True)
    entity_id: Mapped[str | None] = mapped_column(String(36), nullable=True)
    read: Mapped[bool] = mapped_column(Boolean, default=False, nullable=False)
    created_at: Mapped[datetime] = mapped_column(UTCDateTime, default=utcnow, nullable=False)


class AuditEvent(Base):
    __tablename__ = "audit_events"
    __table_args__ = (Index("ix_audit_school", "school_id", "created_at"),)

    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=new_id)
    school_id: Mapped[str | None] = mapped_column(ForeignKey("schools.id"), nullable=True)
    actor_user_id: Mapped[str | None] = mapped_column(ForeignKey("users.id"), nullable=True)
    actor_role: Mapped[str | None] = mapped_column(String(32), nullable=True)
    action: Mapped[str] = mapped_column(String(80), nullable=False)
    entity_type: Mapped[str | None] = mapped_column(String(64), nullable=True)
    entity_id: Mapped[str | None] = mapped_column(String(36), nullable=True)
    summary: Mapped[str] = mapped_column(String(500), nullable=False)
    meta: Mapped[dict] = mapped_column(JSON, default=dict, nullable=False)
    created_at: Mapped[datetime] = mapped_column(UTCDateTime, default=utcnow, nullable=False)


class Outbox(Base):
    __tablename__ = "outbox"
    __table_args__ = (Index("ix_outbox_pending", "processed_at"),)

    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=new_id)
    topic: Mapped[str] = mapped_column(String(64), nullable=False)
    school_id: Mapped[str | None] = mapped_column(String(36), nullable=True)
    payload: Mapped[dict] = mapped_column(JSON, default=dict, nullable=False)
    created_at: Mapped[datetime] = mapped_column(UTCDateTime, default=utcnow, nullable=False)
    processed_at: Mapped[datetime | None] = mapped_column(UTCDateTime, nullable=True)
