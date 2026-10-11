"""Role-based access control.

Permissions are strings namespaced by domain. They are granted to *roles within
a school membership* and, separately, to platform administrators. The two
authority domains are deliberately disjoint: holding platform administration
never confers academic approval authority.
"""

from __future__ import annotations

from .models import Membership, Role


class Perm:
    # Platform domain
    PLATFORM_SCHOOLS_MANAGE = "platform.schools.manage"
    PLATFORM_SCHOOLS_SUSPEND = "platform.schools.suspend"
    PLATFORM_HEALTH_VIEW = "platform.health.view"
    PLATFORM_AUDIT_VIEW = "platform.audit.view"

    # School domain
    SCHOOL_VIEW = "school.view"
    SCHOOL_SETTINGS_MANAGE = "school.settings.manage"
    STAFF_MANAGE = "staff.manage"
    ACADEMICS_MANAGE = "academics.manage"
    TIMETABLE_VIEW = "timetable.view"
    TIMETABLE_MANAGE = "timetable.manage"
    TIMETABLE_PUBLISH = "timetable.publish"
    REQUEST_SUBMIT = "request.submit"
    REQUEST_REVIEW = "request.review"
    REQUEST_DECIDE = "request.decide"
    REQUEST_VIEW_SCHOOL = "request.view.school"
    BELL_VIEW = "bell.view"
    BELL_MANAGE = "bell.manage"
    AUDIT_VIEW_SCHOOL = "audit.view.school"


ROLE_PERMISSIONS: dict[str, frozenset[str]] = {
    Role.SCHOOL_HEAD: frozenset(
        {
            Perm.SCHOOL_VIEW,
            Perm.SCHOOL_SETTINGS_MANAGE,
            Perm.STAFF_MANAGE,
            Perm.ACADEMICS_MANAGE,
            Perm.TIMETABLE_VIEW,
            Perm.TIMETABLE_MANAGE,
            Perm.TIMETABLE_PUBLISH,
            Perm.REQUEST_DECIDE,
            Perm.REQUEST_VIEW_SCHOOL,
            Perm.BELL_VIEW,
            Perm.BELL_MANAGE,
            Perm.AUDIT_VIEW_SCHOOL,
        }
    ),
    Role.DOS: frozenset(
        {
            Perm.SCHOOL_VIEW,
            Perm.ACADEMICS_MANAGE,
            Perm.TIMETABLE_VIEW,
            Perm.TIMETABLE_MANAGE,
            Perm.REQUEST_REVIEW,
            Perm.REQUEST_VIEW_SCHOOL,
            Perm.BELL_VIEW,
        }
    ),
    Role.TEACHER: frozenset(
        {
            Perm.SCHOOL_VIEW,
            Perm.TIMETABLE_VIEW,
            Perm.REQUEST_SUBMIT,
            Perm.BELL_VIEW,
        }
    ),
    Role.BELL_MONITOR: frozenset(
        {
            Perm.SCHOOL_VIEW,
            Perm.BELL_VIEW,
        }
    ),
}

PLATFORM_PERMISSIONS: frozenset[str] = frozenset(
    {
        Perm.PLATFORM_SCHOOLS_MANAGE,
        Perm.PLATFORM_SCHOOLS_SUSPEND,
        Perm.PLATFORM_HEALTH_VIEW,
        Perm.PLATFORM_AUDIT_VIEW,
    }
)


def membership_permissions(membership: Membership | None) -> frozenset[str]:
    if membership is None or membership.status != "active":
        return frozenset()
    return ROLE_PERMISSIONS.get(membership.role, frozenset())


def effective_permissions(user, membership: Membership | None) -> frozenset[str]:
    perms = set(membership_permissions(membership))
    if getattr(user, "is_platform_admin", False):
        perms |= PLATFORM_PERMISSIONS
    return frozenset(perms)


def has_permission(user, membership: Membership | None, permission: str) -> bool:
    return permission in effective_permissions(user, membership)
