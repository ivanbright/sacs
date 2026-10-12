"""Interactive terminal application for SACS.

A single, consistent numbered-menu experience for all three roles (Platform
Administrator, Director / School Head, Teacher). It is deliberately *not* a
full-screen TUI: no boxes, borders, ASCII art, or animation - just green text
on the terminal's own (usually black) background, with red for errors.

Navigation is a menu stack (see :mod:`sacs.ui`): ``0`` always returns to the
immediate parent, the main menu uses ``0. Logout``, and the login screen uses
``0. Exit``. Every action is executed through the authenticated API client, so
the server - not the menu - decides what a user may do.
"""

from __future__ import annotations

from typing import Optional

from .api_client import ApiClient, ApiError
from .ui import (
    NAV_BACK,
    NAV_EXIT,
    Cancelled,
    Console,
    Menu,
    Navigator,
    ask_int,
    ask_optional,
    ask_required,
)

ROLE_LABELS = {
    "platform": "Platform Administrator",
    "school_head": "Director / School Head",
    "teacher": "Teacher",
    "dos": "Director of Studies",
    "bell_monitor": "Bell & Schedule Monitor",
}


class TerminalApp:
    def __init__(self, console: Optional[Console] = None, client: Optional[ApiClient] = None):
        self.console = console or Console()
        self.api = client or ApiClient()
        self.me: Optional[dict] = None
        self.school_id: Optional[str] = None
        self.role: Optional[str] = None
        self.school_name: Optional[str] = None

    # ------------------------------------------------------------------ #
    # Session
    # ------------------------------------------------------------------ #
    def _apply_identity(self) -> None:
        me = self.me or {}
        membership = me.get("membership") or {}
        self.school_id = membership.get("school_id")
        self.school_name = membership.get("school_name")
        if me.get("is_platform"):
            self.role = "platform"
        else:
            self.role = membership.get("role")

    def logout(self):
        self.api.logout()
        self.me = None
        self.school_id = None
        self.role = None
        self.school_name = None
        return NAV_BACK

    # ------------------------------------------------------------------ #
    # Login
    # ------------------------------------------------------------------ #
    def login_menu(self) -> Menu:
        menu = Menu("SACS - Smart Academic Coordination System", back_label="Exit", back=NAV_EXIT)
        menu.add("Platform Administrator", lambda: self.attempt_login("Platform Administrator"))
        menu.add("Director / School Head", lambda: self.attempt_login("Director / School Head"))
        menu.add("Teacher", lambda: self.attempt_login("Teacher"))
        return menu

    def attempt_login(self, hint: str):
        c = self.console
        c.blank()
        c.info(f"Sign in as: {hint}")
        email = c.read("Email: ")
        if email is None:
            return None
        password = c.read("Password: ")
        if password is None:
            return None
        try:
            self.me = self.api.login(email.strip(), password)
        except ApiError as exc:
            if exc.status_code == 400:
                sid = c.read("This account spans schools. School id: ")
                if sid is None:
                    return None
                try:
                    self.me = self.api.login(email.strip(), password, sid.strip())
                except ApiError as exc2:
                    c.error(str(exc2.detail))
                    return None
            else:
                c.error(str(exc.detail))
                return None
        self._apply_identity()
        c.blank()
        c.say(f"Welcome, {self.me['user']['display_name']}.")
        c.info(f"Role: {ROLE_LABELS.get(self.role, self.role or 'unknown')}")
        if self.school_name:
            c.info(f"School: {self.school_name}")
        return self.main_menu()

    # ------------------------------------------------------------------ #
    # Role dispatch
    # ------------------------------------------------------------------ #
    def main_menu(self) -> Menu:
        if self.role == "platform":
            return self.platform_menu()
        if self.role in ("school_head", "dos"):
            return self.director_menu()
        if self.role == "teacher":
            return self.teacher_menu()
        # A user with no active membership: only notifications/account apply.
        return self.memberless_menu()

    def _with_common(self, menu: Menu) -> Menu:
        menu.add("Notifications", self.notifications_menu())
        menu.add("Account", self.account_menu)
        return menu

    def memberless_menu(self) -> Menu:
        menu = Menu("SACS (no school membership)", back_label="Logout", back=self.logout)
        menu.add("My Invitations", self.my_invitations)
        menu.add("Accept with invitation token", self.accept_with_token)
        return self._with_common(menu)

    def accept_with_token(self):
        c = self.console
        c.blank()
        try:
            token = ask_required(c, "Invitation token")
            password = ask_required(c, "New password (min 8)")
            display_name = ask_required(c, "Your name")
        except Cancelled:
            c.info("Cancelled.")
            return None
        try:
            data = self.api.post(
                "/api/invitations/accept-token",
                json={"token": token, "password": password, "display_name": display_name},
            )
            c.say("Invitation accepted.")
            self.me = self.api.me()
            self._apply_identity()
            return self.main_menu()
        except ApiError as exc:
            c.error(str(exc.detail))
            return None

    def my_invitations(self):
        try:
            rows = self.api.get("/api/invitations/mine")
        except ApiError as exc:
            self.console.error(str(exc.detail))
            return None
        c = self.console
        pending = [r for r in rows if r.get("status") == "pending"]
        c.blank()
        c.title("My Invitations")
        if not pending:
            c.info("No pending invitations.")
            return None
        for i, r in enumerate(pending, 1):
            c.say(f"{i}. {r['school_name']} as {r['role_label']} (expires {r['expires_at']})")
        idx = ask_int(c, "Accept", minimum=0, maximum=len(pending))
        if idx == 0:
            return None
        inv = pending[idx - 1]
        try:
            self.api.post("/api/invitations/accept", json={"invitation_id": inv["id"]})
        except ApiError as exc:
            c.error(str(exc.detail))
            return None
        c.say("Invitation accepted.")
        self.me = self.api.me()
        self._apply_identity()
        return self.main_menu()

    # ------------------------------------------------------------------ #
    # Platform administrator
    # ------------------------------------------------------------------ #
    def platform_menu(self) -> Menu:
        menu = Menu("Platform Administrator", back_label="Logout", back=self.logout)
        menu.add("Manage Schools", self.platform_schools_menu())
        menu.add("System Health", self.show_health)
        menu.add("Platform Audit Log", self.show_platform_audit)
        return self._with_common(menu)

    def platform_schools_menu(self):
        def factory() -> Menu:
            menu = Menu("Manage Schools", back_label="Back")
            menu.add("List all schools", self.show_schools)
            menu.add("Register new school", self.register_school)
            menu.add("View school details", self.show_school_detail_selected)
            menu.add("Invite Director", self.invite_director_selected)
            menu.add("Suspend school", self.suspend_selected)
            menu.add("Reactivate school", self.reactivate_selected)
            return menu

        return factory

    # ------------------------------------------------------------------ #
    # Director
    # ------------------------------------------------------------------ #
    def director_menu(self) -> Menu:
        menu = Menu(f"Director - {self.school_name or 'School'}", back_label="Logout", back=self.logout)
        menu.add("Teachers and Staff", self.staff_menu())
        menu.add("Classes", self.classes_menu())
        menu.add("Subjects", self.subjects_menu())
        menu.add("Classrooms", self.classrooms_menu())
        menu.add("Timetable", self.timetable_menu())
        menu.add("Lesson Requests", self.requests_menu())
        menu.add("School Bell", self.bell_menu())
        return self._with_common(menu)

    # ------------------------------------------------------------------ #
    # Teacher
    # ------------------------------------------------------------------ #
    def teacher_menu(self) -> Menu:
        menu = Menu(f"Teacher - {self.school_name or 'School'}", back_label="Logout", back=self.logout)
        menu.add("My Profile", self.account_menu)
        menu.add("My Timetable", self.my_lessons)
        menu.add("Submit Lesson Completion", self.submit_menu())
        menu.add("My Requests", self.requests_menu())
        return self._with_common(menu)

    # ================================================================== #
    # Shared: notifications and account
    # ================================================================== #
    def notifications_menu(self):
        def factory() -> Menu:
            menu = Menu("Notifications", back_label="Back")
            menu.add("Unread", self.show_notifications_unread)
            menu.add("All", self.show_notifications_all)
            menu.add("Mark all as read", self.mark_all_notifications_read)
            return menu

        return factory

    def account_menu(self):
        try:
            me = self.api.me()

            def factory() -> Menu:
                c = self.console
                c.blank()
                c.title("Account")
                c.say(f"Name: {me['user']['display_name']}")
                c.say(f"Email: {me['user']['email']}")
                c.info(f"Role: {ROLE_LABELS.get(self.role, self.role or '-')}")
                if me.get("school"):
                    c.info(f"School: {me['school']['name']} ({me['school']['status']})")
                c.info(f"Session expires: {me.get('session_expires_at')}")
                menu = Menu("Account", back_label="Back")
                menu.add("Refresh", self.account_menu)
                return menu

            return factory()
        except ApiError as exc:
            self.console.error(str(exc.detail))
            return None

    # ================================================================== #
    # Platform actions
    # ================================================================== #
    def show_schools(self):
        try:
            schools = self.api.get("/api/platform/schools")
        except ApiError as exc:
            self.console.error(str(exc.detail))
            return None
        c = self.console
        c.blank()
        c.title("Schools")
        if not schools:
            c.info("No schools registered yet.")
            return None
        for s in schools:
            counts = s.get("counts", {})
            c.say(f"{s['name']} [{s['slug']}] - {s['status']}")
            c.info(
                f"    members={counts.get('members', 0)} lessons={counts.get('lessons', 0)} "
                f"requests={counts.get('completion_requests', 0)} bells={counts.get('bell_events', 0)}"
            )
        return None

    def register_school(self):
        c = self.console
        c.blank()
        try:
            name = ask_required(c, "School name")
            slug = ask_optional(c, "Slug")
            tz = ask_optional(c, "Timezone (IANA, default UTC)")
            head = ask_optional(c, "Director email to invite")
        except Cancelled:
            c.info("Cancelled.")
            return None
        payload = {"name": name, "timezone": tz or "UTC"}
        if slug:
            payload["slug"] = slug
        if head:
            payload["head_email"] = head
        try:
            result = self.api.post("/api/platform/schools", json=payload)
        except ApiError as exc:
            c.error(str(exc.detail))
            return None
        c.say(f"Registered '{result['name']}' ({result['slug']}).")
        token = result.get("head_invitation_token")
        if token:
            c.say("Director invitation token (copy now):")
            c.info(token)
        return None

    def pick_school(self, prompt="Select school"):
        try:
            schools = self.api.get("/api/platform/schools")
        except ApiError as exc:
            self.console.error(str(exc.detail))
            return None
        if not schools:
            self.console.info("No schools available.")
            return None
        c = self.console
        c.blank()
        c.title("Select a school")
        for i, s in enumerate(schools, 1):
            c.say(f"{i}. {s['name']} [{s['slug']}] - {s['status']}")
        c.say("0. Back")
        idx = ask_int(c, prompt, minimum=0, maximum=len(schools))
        if idx == 0:
            return None
        return schools[idx - 1]

    def show_school_detail(self, school_id: str):
        try:
            s = self.api.get(f"/api/platform/schools/{school_id}")
        except ApiError as exc:
            self.console.error(str(exc.detail))
            return None
        c = self.console
        c.blank()
        c.title(f"School: {s['name']}")
        c.say(f"Slug: {s['slug']}")
        c.say(f"Status: {s['status']}")
        c.info(f"Timezone: {s['timezone']}")
        counts = s.get("counts", {})
        c.info(
            "Members={} Classes={} Subjects={} Classrooms={} Entries={} Lessons={} Requests={}".format(
                counts.get("members", 0),
                counts.get("classes", 0),
                counts.get("subjects", 0),
                counts.get("classrooms", 0),
                counts.get("timetable_entries", 0),
                counts.get("lessons", 0),
                counts.get("completion_requests", 0),
            )
        )
        return None

    def open_school(self, school_id: str):
        def factory() -> Menu:
            menu = Menu("School", back_label="Back")
            menu.add("View details", lambda: self.show_school_detail(school_id))
            menu.add("Invite Director", lambda: self.invite_director(school_id))
            menu.add("Suspend", lambda: self.set_school_status(school_id, "suspend"))
            menu.add("Reactivate", lambda: self.set_school_status(school_id, "reactivate"))
            return menu

        return factory

    def _open_selected_school(self):
        school = self.pick_school()
        if school is None:
            return None
        return self.open_school(school["id"])

    def set_school_status(self, school_id: str, action: str):
        try:
            result = self.api.post(f"/api/platform/schools/{school_id}/{action}")
        except ApiError as exc:
            self.console.error(str(exc.detail))
            return None
        self.console.say(f"School '{result['name']}' is now {result['status']}.")
        return None

    def invite_director(self, school_id: str):
        c = self.console
        c.blank()
        try:
            email = ask_required(c, "Director email")
        except Cancelled:
            c.info("Cancelled.")
            return None
        try:
            result = self.api.post(
                f"/api/platform/schools/{school_id}/invitations", json={"email": email}
            )
        except ApiError as exc:
            c.error(str(exc.detail))
            return None
        c.say("Invitation created. Give this token to the Director:")
        c.info(result["invitation_token"])
        return None

    # convenience wrappers used as menu handlers (return a factory to descend)
    def show_school_detail_selected(self):
        s = self.pick_school()
        if s is None:
            return None
        return self.show_school_detail(s["id"])

    def invite_director_selected(self):
        s = self.pick_school()
        if s is None:
            return None
        self.invite_director(s["id"])
        return None

    def suspend_selected(self):
        s = self.pick_school()
        if s is None:
            return None
        self.set_school_status(s["id"], "suspend")
        return None

    def reactivate_selected(self):
        s = self.pick_school()
        if s is None:
            return None
        self.set_school_status(s["id"], "reactivate")
        return None

    def show_health(self):
        try:
            h = self.api.get("/api/platform/health")
        except ApiError as exc:
            self.console.error(str(exc.detail))
            return None
        c = self.console
        c.blank()
        c.title("System Health")
        c.say(f"Status: {h.get('status')}  Version: {h.get('version')}")
        c.info(f"Database: {h.get('database')}")
        c.info(f"Uptime: {h.get('uptime_seconds')}s")
        sched = h.get("scheduler", {})
        c.info(f"Scheduler: clock={sched.get('clock')} fired={sched.get('fired')} missed={sched.get('missed')}")
        return None

    def show_platform_audit(self):
        try:
            rows = self.api.get("/api/platform/audit")
        except ApiError as exc:
            self.console.error(str(exc.detail))
            return None
        c = self.console
        c.blank()
        c.title("Platform Audit (latest 20)")
        for e in rows[:20]:
            c.say(f"{e['created_at']}  {e['action']}")
            c.info(f"    {e['summary']}")
        return None

    # ================================================================== #
    # Director actions
    # ================================================================== #
    def staff_menu(self):
        def factory() -> Menu:
            menu = Menu("Teachers and Staff", back_label="Back")
            menu.add("List staff", self.show_staff)
            menu.add("Invite teacher", self.invite_teacher)
            menu.add("Invitations", self.school_invitations)
            return menu

        return factory

    def show_staff(self):
        try:
            rows = self.api.get(f"/api/schools/{self.school_id}/staff")
        except ApiError as exc:
            self.console.error(str(exc.detail))
            return None
        c = self.console
        c.blank()
        c.title("Staff")
        for m in rows:
            c.say(f"{m['user']['display_name']} <{m['user']['email']}> - {m['role']} ({m['status']})")
        return None

    def invite_teacher(self):
        c = self.console
        c.blank()
        try:
            email = ask_required(c, "Teacher email")
        except Cancelled:
            c.info("Cancelled.")
            return None
        try:
            result = self.api.post(
                f"/api/schools/{self.school_id}/invitations",
                json={"email": email, "role": "teacher"},
            )
        except ApiError as exc:
            c.error(str(exc.detail))
            return None
        c.say("Invitation created. Share this token with the teacher:")
        c.info(result.get("invitation_token", ""))
        return None

    def school_invitations(self):
        try:
            rows = self.api.get(f"/api/schools/{self.school_id}/invitations")
        except ApiError as exc:
            self.console.error(str(exc.detail))
            return None
        c = self.console
        c.blank()
        c.title("Invitations")
        pending = []
        for inv in rows:
            c.say(f"{inv['email']} - {inv['role']} - {inv['status']}")
            if inv["status"] == "pending":
                pending.append(inv)
        if not pending:
            return None
        c.blank()
        c.info("Type an invitation number to revoke it, or 0 to skip.")
        for i, inv in enumerate(pending, 1):
            c.say(f"{i}. {inv['email']}")
        idx = ask_int(c, "Revoke", minimum=0, maximum=len(pending))
        if idx == 0:
            return None
        try:
            self.api.post(
                f"/api/schools/{self.school_id}/invitations/{pending[idx - 1]['id']}/revoke"
            )
            c.say("Invitation revoked.")
        except ApiError as exc:
            c.error(str(exc.detail))
        return None

    def classes_menu(self):
        def factory() -> Menu:
            menu = Menu("Classes", back_label="Back")
            menu.add("List", self.list_classes)
            menu.add("Add", self.add_class)
            return menu

        return factory

    def list_classes(self):
        self._list_academics("classes", "Classes")

    def add_class(self):
        self._add_academic("classes", "Class name", "Classes")

    def subjects_menu(self):
        def factory() -> Menu:
            menu = Menu("Subjects", back_label="Back")
            menu.add("List", lambda: self._list_academics("subjects", "Subjects"))
            menu.add("Add", lambda: self._add_academic("subjects", "Subject name", "Subjects"))
            return menu

        return factory

    def classrooms_menu(self):
        def factory() -> Menu:
            menu = Menu("Classrooms", back_label="Back")
            menu.add("List", lambda: self._list_academics("classrooms", "Classrooms"))
            menu.add("Add", lambda: self._add_academic("classrooms", "Classroom name", "Classrooms"))
            return menu

        return factory

    def _list_academics(self, path: str, title: str):
        try:
            rows = self.api.get(f"/api/schools/{self.school_id}/academics/{path}")
        except ApiError as exc:
            self.console.error(str(exc.detail))
            return None
        c = self.console
        c.blank()
        c.title(title)
        if not rows:
            c.info("None yet.")
        for r in rows:
            extra = f" ({r.get('code')})" if r.get("code") else ""
            c.say(f"{r.get('name')}{extra}")
        return None

    def _add_academic(self, path: str, label: str, title: str):
        c = self.console
        c.blank()
        try:
            name = ask_required(c, label)
        except Cancelled:
            c.info("Cancelled.")
            return None
        try:
            self.api.post(f"/api/schools/{self.school_id}/academics/{path}", json={"name": name})
            c.say("Added.")
        except ApiError as exc:
            c.error(str(exc.detail))
        return None

    def timetable_menu(self):
        def factory() -> Menu:
            menu = Menu("Timetable", back_label="Back")
            menu.add("List entries", self.list_timetable)
            menu.add("Add draft entry", self.add_timetable_entry)
            menu.add("Publish draft entries", self.publish_timetable)
            menu.add("My school lessons", self.my_lessons)
            return menu

        return factory

    def _select(self, rows, label: str):
        if len(rows) == 1:
            return rows[0]
        c = self.console
        c.blank()
        for i, r in enumerate(rows, 1):
            name = (
                r.get("name")
                or (r.get("user") or {}).get("display_name")
                or r.get("id")
            )
            c.say(f"{i}. {name}")
        idx = ask_int(c, f"Choose {label}", minimum=1, maximum=len(rows))
        return rows[idx - 1]

    def add_timetable_entry(self):
        c = self.console
        sid = self.school_id
        try:
            classes = self.api.get(f"/api/schools/{sid}/academics/classes")
            subjects = self.api.get(f"/api/schools/{sid}/academics/subjects")
            classrooms = self.api.get(f"/api/schools/{sid}/academics/classrooms")
            staff = self.api.get(f"/api/schools/{sid}/staff")
        except ApiError as exc:
            c.error(str(exc.detail))
            return None
        teachers = [m for m in staff if m["role"] == "teacher"]
        if not classes or not subjects or not teachers:
            c.info("Add at least one class, subject, and teacher first.")
            return None
        try:
            cls = self._select(classes, "class")
            subj = self._select(subjects, "subject")
            teacher = self._select(teachers, "teacher")
            room = self._select(classrooms, "classroom") if classrooms else None
            day = ask_int(c, "Day (0=Mon .. 6=Sun)", minimum=0, maximum=6)
            start = ask_required(c, "Start time (HH:MM)")
            end = ask_required(c, "End time (HH:MM)")
        except Cancelled:
            c.info("Cancelled.")
            return None
        payload = {
            "class_id": cls["id"],
            "subject_id": subj["id"],
            "teacher_membership_id": teacher["id"],
            "day_of_week": day,
            "start_time": start,
            "end_time": end,
        }
        if room:
            payload["classroom_id"] = room["id"]
        try:
            result = self.api.post(f"/api/schools/{sid}/timetable", json=payload)
        except ApiError as exc:
            c.error(str(exc.detail))
            return None
        c.say(f"Created draft entry [{result['id'][:8]}].")
        return None

    def list_timetable(self):
        try:
            rows = self.api.get(f"/api/schools/{self.school_id}/timetable")
        except ApiError as exc:
            self.console.error(str(exc.detail))
            return None
        c = self.console
        c.blank()
        c.title("Timetable entries")
        days = ["Mon", "Tue", "Wed", "Thu", "Fri", "Sat", "Sun"]
        for e in rows:
            c.say(
                f"{days[e['day_of_week']]} {e['start_time']}-{e['end_time']} "
                f"[{e['status']}]"
            )
        return None

    def publish_timetable(self):
        try:
            result = self.api.post(f"/api/schools/{self.school_id}/timetable/publish")
        except ApiError as exc:
            self.console.error(str(exc.detail))
            return None
        self.console.say(
            f"Published {result.get('published_entries')} entries; "
            f"created {result.get('sessions_created')} lesson sessions."
        )
        return None

    def my_lessons(self):
        try:
            rows = self.api.get(f"/api/schools/{self.school_id}/timetable/lessons")
        except ApiError as exc:
            self.console.error(str(exc.detail))
            return None
        c = self.console
        c.blank()
        c.title("Lessons")
        if not rows:
            c.info("No lessons scheduled.")
        for lesson in rows:
            c.say(
                f"{lesson['date']} {lesson['start'][11:16]} "
                f"{lesson.get('subject') or '-'} / {lesson.get('class') or '-'} "
                f"[{lesson['id'][:8]}]"
            )
        return None

    def request_detail_menu(self, request: dict):
        req_id = request["id"]

        def factory() -> Menu:
            c = self.console
            try:
                detail = self.api.get(f"/api/schools/{self.school_id}/requests/{req_id}")
            except ApiError as exc:
                c.error(str(exc.detail))
                return Menu("Request", back_label="Back")
            c.blank()
            c.title(f"Request {req_id[:8]}")
            c.say(f"State: {detail['state']}")
            c.info(f"Teacher: {detail.get('teacher', {}).get('name')}")
            c.info(f"Note: {detail.get('note') or '-'}")
            for action in detail.get("actions", []):
                c.info(f"  - {action['action']} by {action['actor_role']} -> {action['to_state']}")
            menu = Menu("Request actions", back_label="Back")
            if self.role in ("school_head", "dos"):
                menu.add("Forward", lambda: self.transition(req_id, "forward"))
                menu.add("Return for correction", lambda: self.transition(req_id, "return"))
            if self.role == "school_head":
                menu.add("Approve", lambda: self.transition(req_id, "approve"))
                menu.add("Reject", lambda: self.transition(req_id, "reject"))
            if self.role == "teacher" and detail["state"] == "returned_for_correction":
                menu.add("Resubmit", lambda: self.transition(req_id, "submit"))
            return menu

        return factory

    def transition(self, req_id: str, action: str):
        c = self.console
        reason = None
        if action in ("return", "reject"):
            reason = c.read("Reason: ")
            if reason is None:
                return None
        try:
            result = self.api.post(
                f"/api/schools/{self.school_id}/requests/{req_id}/transition",
                json={"action": action, "reason": reason},
            )
        except ApiError as exc:
            c.error(str(exc.detail))
            return None
        c.say(f"Request is now {result['state']}.")
        return NAV_BACK

    def bell_menu(self):
        def factory() -> Menu:
            menu = Menu("School Bell", back_label="Back")
            menu.add("Schedules", self.show_bell_schedules)
            menu.add("Create schedule (one period)", self.create_bell_schedule)
            menu.add("Ring now", self.ring_bell)
            menu.add("Test sound", self.test_bell_sound)
            menu.add("Recent events", self.show_bell_events)
            return menu

        return factory

    def show_bell_schedules(self):
        try:
            rows = self.api.get(f"/api/schools/{self.school_id}/bell/schedules")
        except ApiError as exc:
            self.console.error(str(exc.detail))
            return None
        c = self.console
        c.blank()
        c.title("Bell schedules")
        if not rows:
            c.info("No schedules.")
        for s in rows:
            state = "paused" if s["paused"] else ("active" if s["is_active"] else "inactive")
            days = s.get("days")
            daystr = "all days" if days is None else ",".join(str(d) for d in days)
            periods = s.get("periods") or []
            c.say(f"{s['name']} ({s['timezone']}) - {state}; {len(periods)} periods; {daystr}")
            for p in periods:
                c.info(f"    {p['start']}-{p['end']} {p['label']}")
        return None

    def create_bell_schedule(self):
        c = self.console
        c.blank()
        try:
            name = ask_required(c, "Schedule name")
            label = ask_required(c, "First period label")
            start = ask_required(c, "Start time (HH:MM)")
            end = ask_required(c, "End time (HH:MM)")
        except Cancelled:
            c.info("Cancelled.")
            return None
        try:
            self.api.post(
                f"/api/schools/{self.school_id}/bell/schedules",
                json={
                    "name": name,
                    "timezone": "UTC",
                    "periods": [{"label": label, "start": start, "end": end}],
                },
            )
            c.say("Schedule created.")
        except ApiError as exc:
            c.error(str(exc.detail))
        return None

    def ring_bell(self):
        try:
            result = self.api.post(f"/api/schools/{self.school_id}/bell/ring", json={"label": "Manual ring"})
        except ApiError as exc:
            self.console.error(str(exc.detail))
            return None
        c = self.console
        if result.get("played"):
            c.say("Ringing the bell now.")
        else:
            c.info("Bell recorded, but audio is unavailable or disabled on this host.")
        return None

    def test_bell_sound(self):
        try:
            result = self.api.post(f"/api/schools/{self.school_id}/bell/test-sound")
        except ApiError as exc:
            self.console.error(str(exc.detail))
            return None
        c = self.console
        c.say(
            f"audio_available={result['audio_available']} enabled={result['enabled']} "
            f"played={result['played']}"
        )
        return None

    def show_bell_events(self):
        try:
            data = self.api.get(f"/api/schools/{self.school_id}/bell/events")
        except ApiError as exc:
            self.console.error(str(exc.detail))
            return None
        c = self.console
        c.blank()
        c.title("Recent bell events")
        events = data.get("events", [])
        if not events:
            c.info("No bell events recorded yet.")
        for e in events[:20]:
            c.say(f"{e['scheduled_at']} {e['outcome']} - {e['label']}")
        return None

    # ================================================================== #
    # Teacher actions
    # ================================================================== #
    def submit_menu(self):
        def factory() -> Menu:
            try:
                lessons = self.api.get(f"/api/schools/{self.school_id}/timetable/lessons")
            except ApiError as exc:
                self.console.error(str(exc.detail))
                return Menu("Submit Lesson Completion", back_label="Back")
            c = self.console
            c.blank()
            c.title("Submit Lesson Completion")
            if not lessons:
                c.info("No lessons available to submit.")
                return Menu("Submit Lesson Completion", back_label="Back")

            def submit(lesson_id: str):
                return self._submit_lesson(lesson_id)

            menu = Menu("Submit Lesson Completion", back_label="Back")
            for lesson in lessons[:15]:
                label = f"{lesson['date']} {lesson['start'][11:16]} {lesson.get('subject') or '-'}"
                menu.add(label, (lambda lid=lesson["id"]: submit(lid)))
            return menu

        return factory

    def _submit_lesson(self, lesson_id: str):
        c = self.console
        note = ask_optional(c, "Note")
        try:
            result = self.api.post(
                f"/api/schools/{self.school_id}/requests",
                json={"lesson_session_id": lesson_id, "note": note},
            )
        except ApiError as exc:
            c.error(str(exc.detail))
            return None
        c.say(f"Submitted. Request state: {result['state']}.")
        return None

    def requests_menu(self):  # teacher + director share this
        def factory() -> Menu:
            menu = Menu("Lesson Requests", back_label="Back")
            menu.add("Open requests", self._open_requests_list)
            return menu

        return factory

    def _open_requests_list(self):
        try:
            rows = self.api.get(f"/api/schools/{self.school_id}/requests")
        except ApiError as exc:
            self.console.error(str(exc.detail))
            return None
        c = self.console
        c.blank()
        c.title("Lesson Requests")
        if not rows:
            c.info("No requests.")
            return None
        for i, r in enumerate(rows, 1):
            c.say(f"{i}. {r['state']} - {r.get('teacher', {}).get('name') or '-'}")
        idx = ask_int(c, "Open", minimum=0, maximum=len(rows))
        if idx == 0:
            return None
        return self.request_detail_menu(rows[idx - 1])

    # ================================================================== #
    # Notifications actions
    # ================================================================== #
    def _render_notifications(self, unread_only: bool):
        try:
            rows = self.api.get("/api/notifications", params={"unread_only": unread_only})
        except ApiError as exc:
            self.console.error(str(exc.detail))
            return None
        c = self.console
        c.blank()
        c.title("Notifications" + (" (unread)" if unread_only else ""))
        if not rows:
            c.info("Nothing here.")
        for n in rows[:30]:
            flag = "NEW " if not n["read"] else ""
            c.say(f"{flag}{n['title']}")
            c.info(f"    {n['created_at']}  {n['body'] or ''}")
            if not n["read"]:
                try:
                    self.api.post(f"/api/notifications/{n['id']}/read")
                except ApiError:
                    pass
        return None

    def show_notifications_unread(self):
        return self._render_notifications(True)

    def show_notifications_all(self):
        return self._render_notifications(False)

    def mark_all_notifications_read(self):
        try:
            result = self.api.post("/api/notifications/read-all")
        except ApiError as exc:
            self.console.error(str(exc.detail))
            return None
        self.console.say(f"Marked {result.get('updated')} notification(s) as read.")
        return None


def run(console: Optional[Console] = None) -> None:
    """Entry point: run the interactive terminal application until exit."""
    console = console or Console()
    client = ApiClient()
    app = TerminalApp(console=console, client=client)
    try:
        Navigator(console).run(lambda: app.login_menu())
    except KeyboardInterrupt:
        console.blank()
        console.info("Interrupted.")
    finally:
        client.close()
    console.blank()
    console.info("Goodbye.")
