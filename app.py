import sqlite3
import bcrypt
from contextlib import contextmanager
from datetime import datetime, time, timedelta

import streamlit as st
from streamlit_calendar import calendar

DB_PATH = "scheduler.db"
BOOTSTRAP_ADMIN_PASSWORD = "setup123"  # CHANGE THIS


# ---------------- DB helpers ----------------
@contextmanager
def get_conn():
    conn = sqlite3.connect(DB_PATH, check_same_thread=False)
    conn.row_factory = sqlite3.Row
    try:
        yield conn
        conn.commit()
    finally:
        conn.close()


def init_db():
    with get_conn() as conn:
        conn.execute(
            """
            CREATE TABLE IF NOT EXISTS employees (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                name TEXT NOT NULL UNIQUE
            )
            """
        )
        conn.execute(
            """
            CREATE TABLE IF NOT EXISTS slots (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                employee_id INTEGER NOT NULL,
                start_at TEXT NOT NULL,
                end_at TEXT NOT NULL,
                status TEXT NOT NULL CHECK(status IN ('AVAILABLE','PENDING','BOOKED')),
                UNIQUE(employee_id, start_at),
                FOREIGN KEY(employee_id) REFERENCES employees(id)
            )
            """
        )
        conn.execute(
            """
            CREATE TABLE IF NOT EXISTS booking_requests (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                slot_id INTEGER NOT NULL UNIQUE,
                customer_name TEXT NOT NULL,
                customer_email TEXT NOT NULL,
                status TEXT NOT NULL CHECK(status IN ('PENDING','ACCEPTED','DECLINED')),
                created_at TEXT NOT NULL,
                decided_at TEXT,
                FOREIGN KEY(slot_id) REFERENCES slots(id)
            )
            """
        )
        conn.execute(
            """
            CREATE TABLE IF NOT EXISTS employee_logins (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                employee_id INTEGER NOT NULL UNIQUE,
                username TEXT NOT NULL UNIQUE,
                password_hash BLOB NOT NULL,
                FOREIGN KEY(employee_id) REFERENCES employees(id)
            )
            """
        )


# ---------------- Employees ----------------
def create_employee(name: str) -> tuple[bool, str]:
    name_clean = name.strip()
    if not name_clean:
        return False, "Name cannot be empty."
    try:
        with get_conn() as conn:
            conn.execute("INSERT INTO employees(name) VALUES(?)", (name_clean,))
        return True, f"Employee '{name_clean}' created."
    except sqlite3.IntegrityError:
        return False, f"Employee '{name_clean}' already exists."


def list_employees():
    with get_conn() as conn:
        rows = conn.execute("SELECT id, name FROM employees ORDER BY name").fetchall()
        return [(int(r["id"]), r["name"]) for r in rows]


def delete_employee_everything(employee_id: int) -> tuple[bool, str]:
    with get_conn() as conn:
        emp = conn.execute("SELECT name FROM employees WHERE id = ?", (employee_id,)).fetchone()
        if not emp:
            return False, "Employee not found."

        slot_rows = conn.execute("SELECT id FROM slots WHERE employee_id = ?", (employee_id,)).fetchall()
        slot_ids = [int(r["id"]) for r in slot_rows]

        if slot_ids:
            placeholders = ",".join(["?"] * len(slot_ids))
            conn.execute(
                f"DELETE FROM booking_requests WHERE slot_id IN ({placeholders})",
                tuple(slot_ids),
            )

        conn.execute("DELETE FROM slots WHERE employee_id = ?", (employee_id,))
        conn.execute("DELETE FROM employee_logins WHERE employee_id = ?", (employee_id,))
        conn.execute("DELETE FROM employees WHERE id = ?", (employee_id,))

        return True, f"Deleted employee '{emp['name']}' and all related data."


# ---------------- Slots ----------------
def generate_slots(
    employee_id: int,
    days_ahead: int = 14,
    work_start: time = time(9, 0),
    work_end: time = time(17, 0),
    slot_minutes: int = 30,
    weekdays_only: bool = True,
):
    now = datetime.now()
    start_day = now.date()

    with get_conn() as conn:
        for d in range(days_ahead):
            day = start_day + timedelta(days=d)
            if weekdays_only and day.weekday() >= 5:
                continue

            dt_start = datetime.combine(day, work_start)
            dt_end = datetime.combine(day, work_end)

            cur = dt_start
            while cur + timedelta(minutes=slot_minutes) <= dt_end:
                s = cur
                e = cur + timedelta(minutes=slot_minutes)
                conn.execute(
                    """
                    INSERT OR IGNORE INTO slots(employee_id, start_at, end_at, status)
                    VALUES(?,?,?, 'AVAILABLE')
                    """,
                    (employee_id, s.isoformat(), e.isoformat()),
                )
                cur = e


def fetch_slots(employee_id: int, limit_days: int = 30):
    today = datetime.now().date()
    end_day = today + timedelta(days=limit_days)
    with get_conn() as conn:
        rows = conn.execute(
            """
            SELECT s.id, s.start_at, s.end_at, s.status,
                   br.customer_name, br.customer_email, br.status as req_status
            FROM slots s
            LEFT JOIN booking_requests br ON br.slot_id = s.id
            WHERE s.employee_id = ?
              AND date(s.start_at) >= date(?)
              AND date(s.start_at) <= date(?)
            ORDER BY s.start_at
            """,
            (employee_id, today.isoformat(), end_day.isoformat()),
        ).fetchall()
        return [dict(r) for r in rows]


def fetch_pending_requests(employee_id: int):
    with get_conn() as conn:
        rows = conn.execute(
            """
            SELECT br.id as request_id, br.customer_name, br.customer_email, br.created_at,
                   s.id as slot_id, s.start_at, s.end_at
            FROM booking_requests br
            JOIN slots s ON s.id = br.slot_id
            WHERE br.status = 'PENDING'
              AND s.employee_id = ?
            ORDER BY s.start_at
            """,
            (employee_id,),
        ).fetchall()
        return [dict(r) for r in rows]


# ---------------- Booking actions ----------------
def request_slot(slot_id: int, customer_name: str, customer_email: str) -> tuple[bool, str]:
    now = datetime.now().isoformat()
    with get_conn() as conn:
        slot = conn.execute("SELECT status FROM slots WHERE id = ?", (slot_id,)).fetchone()
        if not slot:
            return False, "Slot not found."
        if slot["status"] != "AVAILABLE":
            return False, "Sorry — that slot is no longer available."

        conn.execute("UPDATE slots SET status = 'PENDING' WHERE id = ?", (slot_id,))
        conn.execute(
            """
            INSERT INTO booking_requests(slot_id, customer_name, customer_email, status, created_at)
            VALUES(?,?,?,?,?)
            """,
            (slot_id, customer_name.strip(), customer_email.strip().lower(), "PENDING", now),
        )
    return True, "Request sent! The employee will accept or decline."


def accept_request(request_id: int) -> tuple[bool, str]:
    now = datetime.now().isoformat()
    with get_conn() as conn:
        req = conn.execute(
            "SELECT slot_id, status FROM booking_requests WHERE id = ?", (request_id,)
        ).fetchone()
        if not req:
            return False, "Request not found."
        if req["status"] != "PENDING":
            return False, "Request is not pending."

        slot = conn.execute("SELECT status FROM slots WHERE id = ?", (req["slot_id"],)).fetchone()
        if not slot:
            return False, "Slot not found."
        if slot["status"] != "PENDING":
            return False, "Slot is not pending anymore."

        conn.execute(
            "UPDATE booking_requests SET status='ACCEPTED', decided_at=? WHERE id=?",
            (now, request_id),
        )
        conn.execute("UPDATE slots SET status='BOOKED' WHERE id=?", (req["slot_id"],))
    return True, "Accepted."


def decline_request(request_id: int) -> tuple[bool, str]:
    now = datetime.now().isoformat()
    with get_conn() as conn:
        req = conn.execute(
            "SELECT slot_id, status FROM booking_requests WHERE id = ?", (request_id,)
        ).fetchone()
        if not req:
            return False, "Request not found."
        if req["status"] != "PENDING":
            return False, "Request is not pending."

        conn.execute(
            "UPDATE booking_requests SET status='DECLINED', decided_at=? WHERE id=?",
            (now, request_id),
        )
        conn.execute("UPDATE slots SET status='AVAILABLE' WHERE id=?", (req["slot_id"],))
    return True, "Declined (slot is available again)."


# ---------------- Logins ----------------
def list_logins():
    with get_conn() as conn:
        rows = conn.execute(
            """
            SELECT e.id as employee_id, e.name as employee_name, l.username
            FROM employee_logins l
            JOIN employees e ON e.id = l.employee_id
            ORDER BY e.name
            """
        ).fetchall()
        return [dict(r) for r in rows]


def create_or_update_login(employee_id: int, username: str, password: str) -> tuple[bool, str]:
    username_clean = username.strip().lower()
    if not username_clean:
        return False, "Username cannot be empty."
    if not password.strip():
        return False, "Password cannot be empty."

    pw_hash = bcrypt.hashpw(password.encode("utf-8"), bcrypt.gensalt())

    try:
        with get_conn() as conn:
            existing = conn.execute(
                "SELECT id FROM employee_logins WHERE employee_id = ?", (employee_id,)
            ).fetchone()

            if existing:
                conn.execute(
                    "UPDATE employee_logins SET username=?, password_hash=? WHERE employee_id=?",
                    (username_clean, pw_hash, employee_id),
                )
            else:
                conn.execute(
                    "INSERT INTO employee_logins(employee_id, username, password_hash) VALUES(?,?,?)",
                    (employee_id, username_clean, pw_hash),
                )
        return True, "Login saved."
    except sqlite3.IntegrityError:
        return False, f"Username '{username_clean}' is already taken. Pick another."


def delete_login_by_employee(employee_id: int) -> tuple[bool, str]:
    with get_conn() as conn:
        row = conn.execute(
            "SELECT username FROM employee_logins WHERE employee_id = ?", (employee_id,)
        ).fetchone()
        if not row:
            return False, "No login exists for that employee."
        conn.execute("DELETE FROM employee_logins WHERE employee_id = ?", (employee_id,))
        return True, f"Deleted login '{row['username']}'."


def authenticate(username: str, password: str):
    with get_conn() as conn:
        row = conn.execute(
            "SELECT employee_id, password_hash FROM employee_logins WHERE username = ?",
            (username.strip().lower(),),
        ).fetchone()
        if not row:
            return None
        if bcrypt.checkpw(password.encode("utf-8"), row["password_hash"]):
            return int(row["employee_id"])
        return None


# ---------------- Calendar + selection helpers ----------------
def iso_to_dt(iso_str: str) -> datetime:
    return datetime.fromisoformat(iso_str)


def fmt_range(start_iso: str, end_iso: str) -> str:
    s = iso_to_dt(start_iso)
    e = iso_to_dt(end_iso)
    return f"{s.strftime('%Y-%m-%d %H:%M')} – {e.strftime('%H:%M')}"


def get_slot_by_id(slot_id: int) -> dict | None:
    with get_conn() as conn:
        row = conn.execute(
            "SELECT id, start_at, end_at, status FROM slots WHERE id = ?",
            (slot_id,),
        ).fetchone()
        return dict(row) if row else None


def login_gate() -> bool:
    if st.session_state.get("employee_id") is not None:
        return True

    st.subheader("Employee Login")
    u = st.text_input("Username")
    p = st.text_input("Password", type="password")
    if st.button("Login"):
        emp_id = authenticate(u, p)
        if emp_id is not None:
            st.session_state.employee_id = emp_id
            st.success("Logged in.")
            st.rerun()
        else:
            st.error("Wrong username or password.")
    return False


def slots_to_calendar_events(rows: list[dict]) -> list[dict]:
    events = []
    for r in rows:
        status = r["status"]  # AVAILABLE / PENDING / BOOKED
        title = status
        if r.get("customer_name"):
            title = f"{status} - {r['customer_name']}"

        if status == "AVAILABLE":
            color = "#22c55e"
        elif status == "PENDING":
            color = "#f59e0b"
        else:
            color = "#ef4444"

        events.append(
            {
                "title": title,
                "start": r["start_at"],
                "end": r["end_at"],
                "backgroundColor": color,
                "borderColor": color,
            }
        )
    return events


def slots_to_customer_events(rows: list[dict], selected_ids: list[int]) -> list[dict]:
    events = []
    selected_set = set(selected_ids or [])

    for r in rows:
        status = r["status"]

        if status == "AVAILABLE":
            title = "Available"
            color = "#22c55e"
            if r["id"] in selected_set:
                title = "Selected"
                color = "#60a5fa"  # blue
        elif status == "PENDING":
            title = "Pending"
            color = "#f59e0b"
        else:
            title = "Booked"
            color = "#ef4444"

        events.append(
            {
                "id": str(r["id"]),  # slot_id
                "title": title,
                "start": r["start_at"],
                "end": r["end_at"],
                "backgroundColor": color,
                "borderColor": color,
            }
        )
    return events


# ---------------- App start ----------------
st.set_page_config(page_title="Internal Scheduler", layout="wide")
init_db()

# Simple styling
st.markdown(
    """
<style>
.stApp { background: #0b1220; }
h1, h2, h3, p, label, .stMarkdown { color: #e7eefc; }
div.stButton > button {
  border-radius: 10px;
  padding: 0.6rem 0.9rem;
  border: 1px solid rgba(255,255,255,0.15);
  background: #1f6feb;
  color: white;
  font-weight: 600;
}
div.stButton > button:hover {
  background: #2b7cff;
  border-color: rgba(255,255,255,0.25);
}
input, textarea { border-radius: 10px !important; }
</style>
""",
    unsafe_allow_html=True,
)

# Session defaults
if "employee_id" not in st.session_state:
    st.session_state.employee_id = None
if "admin_authed" not in st.session_state:
    st.session_state.admin_authed = False
if "selected_slot_ids" not in st.session_state:
    st.session_state.selected_slot_ids = []

st.title("Internal Scheduler (MVP)")
page = st.sidebar.radio("Mode", ["Customer", "Employee", "Admin"])

# Always refresh lists each run
employee_list = list_employees()
employee_name_by_id = {eid: name for eid, name in employee_list}


# ---------------- Customer ----------------
if page == "Customer":
    st.header("Book a time")

    if not employee_list:
        st.info("No employees exist yet. Ask admin to create one.")
        st.stop()

    emp_id = st.selectbox(
        "Choose employee",
        options=[eid for eid, _ in employee_list],
        format_func=lambda x: employee_name_by_id[x],
    )

    # Reset selection if employee changes
    if st.session_state.get("customer_emp_id") != emp_id:
        st.session_state.customer_emp_id = emp_id
        st.session_state.selected_slot_ids = []

    rows = fetch_slots(emp_id, limit_days=14)

    # build customer events with highlight for selected
    events = slots_to_customer_events(rows, st.session_state.selected_slot_ids)

    cal_options = {
        "initialView": "timeGridWeek",
        "slotMinTime": "06:30:00",
        "slotMaxTime": "20:00:00",
        "scrollTime": "06:30:00",
        "allDaySlot": False,
        "nowIndicator": True,
        "height": "auto",
    }

    st.subheader("Click green slots to select (multi-select). Click again to unselect.")
    result = calendar(events=events, options=cal_options)

    # Handle click -> toggle selection
    if result and result.get("callback") == "eventClick":
        ev = result["eventClick"]["event"]
        slot_id = int(ev["id"])
        title = ev.get("title", "")

        # only allow toggling if underlying slot is AVAILABLE
        slot = get_slot_by_id(slot_id)
        if not slot or slot["status"] != "AVAILABLE":
            st.warning("That slot is not available anymore.")
        else:
            if slot_id in st.session_state.selected_slot_ids:
                st.session_state.selected_slot_ids.remove(slot_id)
            else:
                st.session_state.selected_slot_ids.append(slot_id)
        st.rerun()

    # Selected table + request
    st.subheader("Selected slots")

    selected_rows = []
    for sid in st.session_state.selected_slot_ids:
        slot = get_slot_by_id(sid)
        if slot and slot["status"] == "AVAILABLE":
            selected_rows.append(
                {"slot_id": slot["id"], "time": fmt_range(slot["start_at"], slot["end_at"]), "status": slot["status"]}
            )

    # Drop invalid ones automatically
    st.session_state.selected_slot_ids = [r["slot_id"] for r in selected_rows]

    if not selected_rows:
        st.info("No slots selected yet.")
    else:
        st.dataframe(selected_rows, use_container_width=True)

        colA, colB = st.columns([1, 2])
        with colA:
            if st.button("Clear selection"):
                st.session_state.selected_slot_ids = []
                st.rerun()

        st.subheader("Request selected slots")
        with st.form("request_multi_form"):
            name = st.text_input("Your name")
            email = st.text_input("Your email")
            submitted = st.form_submit_button("Request selected")

        if submitted:
            if not name.strip() or not email.strip():
                st.error("Please enter name and email.")
            else:
                ok_count = 0
                fail_msgs = []

                for sid in st.session_state.selected_slot_ids:
                    ok, msg = request_slot(sid, name, email)
                    if ok:
                        ok_count += 1
                    else:
                        fail_msgs.append(f"Slot {sid}: {msg}")

                if ok_count:
                    st.success(f"Requested {ok_count} slot(s).")
                    st.session_state.selected_slot_ids = []
                    st.rerun()

                if fail_msgs:
                    st.warning("Some requests failed:")
                    for m in fail_msgs:
                        st.write("-", m)


# ---------------- Employee ----------------
elif page == "Employee":
    st.header("Employee dashboard")

    if not login_gate():
        st.stop()

    emp_id = st.session_state.get("employee_id")
    if emp_id is None:
        st.error("Not logged in.")
        st.stop()

    st.write(f"Logged in as **{employee_name_by_id.get(emp_id, 'Unknown')}**")

    if st.button("Logout"):
        st.session_state.employee_id = None
        st.rerun()

    st.subheader("Pending requests")
    pending = fetch_pending_requests(emp_id)
    if not pending:
        st.write("No pending requests.")
    else:
        for r in pending:
            col1, col2, col3 = st.columns([3, 1, 1])
            with col1:
                st.write(
                    f"**{r['start_at']} – {r['end_at'][-5:]}**  \n"
                    f"{r['customer_name']} ({r['customer_email']})"
                )
            with col2:
                if st.button("Accept", key=f"acc_{r['request_id']}"):
                    ok, msg = accept_request(r["request_id"])
                    (st.success if ok else st.error)(msg)
                    st.rerun()
            with col3:
                if st.button("Decline", key=f"dec_{r['request_id']}"):
                    ok, msg = decline_request(r["request_id"])
                    (st.success if ok else st.error)(msg)
                    st.rerun()

    st.divider()
    st.subheader("Calendar (AVAILABLE / PENDING / BOOKED)")

    rows = fetch_slots(emp_id, limit_days=30)
    events = slots_to_calendar_events(rows)

    cal_options = {
        "initialView": "timeGridWeek",
        "slotMinTime": "05:30:00",
        "slotMaxTime": "23:30:00",
        "scrollTime": "05:30:00",
        "allDaySlot": False,
        "nowIndicator": True,
        "height": "auto",
    }

    calendar(events=events, options=cal_options)


# ---------------- Admin ----------------
elif page == "Admin":
    st.header("Admin")

    if not st.session_state.admin_authed:
        st.subheader("Bootstrap admin login")
        bootstrap_pw = st.text_input("Bootstrap password", type="password")
        if st.button("Unlock admin"):
            if bootstrap_pw == BOOTSTRAP_ADMIN_PASSWORD:
                st.session_state.admin_authed = True
                st.success("Admin unlocked.")
                st.rerun()
            else:
                st.error("Wrong bootstrap password.")
        st.stop()

    col1, col2 = st.columns([1, 3])
    with col1:
        if st.button("Lock admin"):
            st.session_state.admin_authed = False
            st.rerun()

    st.subheader("Create employee")
    new_emp = st.text_input("Employee name")
    if st.button("Add employee"):
        ok, msg = create_employee(new_emp)
        (st.success if ok else st.error)(msg)
        st.rerun()

    # Refresh employees after potential changes
    employee_list = list_employees()
    employee_name_by_id = {eid: name for eid, name in employee_list}

    if not employee_list:
        st.info("Create an employee first.")
        st.stop()

    emp_id = st.selectbox(
        "Select employee",
        options=[eid for eid, _ in employee_list],
        format_func=lambda x: employee_name_by_id[x],
    )

    st.divider()
    st.subheader("Create / update employee login")
    new_user = st.text_input("Username")
    new_pass = st.text_input("Password", type="password")
    if st.button("Save login"):
        ok, msg = create_or_update_login(emp_id, new_user, new_pass)
        (st.success if ok else st.error)(msg)
        st.rerun()

    st.caption("Existing logins:")
    logins = list_logins()
    if not logins:
        st.write("No logins created yet.")
    else:
        for l in logins:
            st.write(f"- {l['employee_name']}: **{l['username']}**")

    st.divider()
    st.subheader("Delete login (selected employee)")
    if st.button("Delete login"):
        ok, msg = delete_login_by_employee(emp_id)
        (st.success if ok else st.error)(msg)
        if ok and st.session_state.get("employee_id") == emp_id:
            st.session_state.employee_id = None
        st.rerun()

    st.divider()
    st.subheader("Delete employee completely (HARD DELETE)")
    emp_name = employee_name_by_id[emp_id]
    st.warning(f"This deletes **{emp_name}**, their login, all slots, and all booking requests.")

    confirm_name = st.text_input("Type the employee name to confirm")
    confirm_check = st.checkbox("I understand this cannot be undone")

    if st.button("DELETE EMPLOYEE COMPLETELY"):
        if not confirm_check or confirm_name.strip() != emp_name:
            st.error("Confirmation failed. Check the box and type the exact employee name.")
        else:
            ok, msg = delete_employee_everything(emp_id)
            (st.success if ok else st.error)(msg)

            if ok and st.session_state.get("employee_id") == emp_id:
                st.session_state.employee_id = None

            # if customer was viewing this employee, clear selection
            if st.session_state.get("customer_emp_id") == emp_id:
                st.session_state.customer_emp_id = None
                st.session_state.selected_slot_ids = []

            st.rerun()

    st.divider()
    st.subheader("Generate slots")
    days = st.number_input("Days ahead", min_value=1, max_value=60, value=14)
    slot_minutes = st.selectbox("Slot length (minutes)", [30, 60], index=0)
    work_start_h = st.number_input("Work start hour", min_value=0, max_value=23, value=9)
    work_end_h = st.number_input("Work end hour", min_value=1, max_value=24, value=17)
    weekdays_only = st.checkbox("Weekdays only (Mon–Fri)", value=True)

    if st.button("Generate"):
        generate_slots(
            emp_id,
            days_ahead=int(days),
            work_start=time(int(work_start_h), 0),
            work_end=time(int(work_end_h), 0),
            slot_minutes=int(slot_minutes),
            weekdays_only=weekdays_only,
        )
        st.success("Slots generated.")
        st.rerun()




