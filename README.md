# Examination Management Portal (EMP)

A local Flask + Jinja2 + Bootstrap + SQLite examination management portal for Admins, Examiners and Students.

## Technology
- Flask backend
- Jinja2 templates
- HTML/CSS + Bootstrap 5 frontend
- SQLite database created programmatically at first run
- No JavaScript is required for core workflows

## Project structure
```text
examination_management_portal/
├── app.py
├── requirements.txt
├── README.md
├── .gitignore
├── instance/
│   └── .gitkeep
├── static/
│   └── css/
│       └── style.css
└── templates/
    ├── base.html
    ├── index.html
    ├── error.html
    ├── auth/
    ├── admin/
    ├── examiner/
    └── student/
```

## Run locally
1. Open a terminal in this folder.
2. Create a virtual environment:
   - Windows: `python -m venv .venv` then `.venv\\Scripts\\activate`
   - macOS/Linux: `python3 -m venv .venv` then `source .venv/bin/activate`
3. Install dependencies: `pip install -r requirements.txt`
4. Start the server: `python app.py`
5. Open `http://127.0.0.1:5000`

The SQLite file is automatically created at `instance/emp.sqlite3`. No manual database creation is required.

## Default Admin
- Username: `admin`
- Password: `admin123`

Change the default password / secret key before using this beyond a demo.

## Typical demo flow
1. Login as Admin.
2. Create a course.
3. Create an examination and configure its dates.
4. Add one or more rubric criteria.
5. Register an Examiner in a separate browser/incognito window.
6. Approve the examiner as Admin.
7. Set the examination to **Slot Creation** (or use dates that cause the automatic status to move there).
8. Examiner creates slots.
9. Set/open booking using **Booking Open** or use the configured booking dates.
10. Register a Student and book a slot.
11. Examiner views booked students and submits rubric marks.
12. Student views the published result/history.

## Core constraints implemented
- Role-based access for Admin / Examiner / Student.
- Examiner approval before dashboard access.
- Examination lifecycle and time-window checks.
- Slot capacity and automatic Full status.
- One active booking per student per examination.
- Cancellation only before the booking deadline.
- Examiner evaluation restricted to students in the examiner's own slots.
- Admin search, booking view, rescheduling and examiner reassignment.
- Database tables are created by Python code on application startup.
