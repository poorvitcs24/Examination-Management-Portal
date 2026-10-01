import os
import sqlite3
from datetime import datetime, date
from functools import wraps
from flask import Flask, render_template, request, redirect, url_for, session, flash, g
from werkzeug.security import generate_password_hash, check_password_hash

BASE_DIR = os.path.abspath(os.path.dirname(__file__))
DB_PATH = os.path.join(BASE_DIR, 'instance', 'emp.sqlite3')

app = Flask(__name__)
app.config['SECRET_KEY'] = os.environ.get('EMP_SECRET_KEY', 'dev-secret-change-me')
app.config['DATABASE'] = DB_PATH


# --------------------------- Database ---------------------------

def get_db():
    if 'db' not in g:
        os.makedirs(os.path.dirname(DB_PATH), exist_ok=True)
        g.db = sqlite3.connect(DB_PATH)
        g.db.row_factory = sqlite3.Row
        g.db.execute('PRAGMA foreign_keys = ON')
    return g.db


@app.teardown_appcontext
def close_db(exception=None):
    db = g.pop('db', None)
    if db is not None:
        db.close()


def init_db():
    db = get_db()
    db.executescript('''
    CREATE TABLE IF NOT EXISTS users (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        username TEXT UNIQUE NOT NULL,
        password_hash TEXT NOT NULL,
        role TEXT NOT NULL CHECK(role IN ('admin','examiner','student')),
        name TEXT NOT NULL,
        email TEXT UNIQUE NOT NULL,
        phone TEXT,
        department TEXT,
        status TEXT NOT NULL DEFAULT 'pending',
        created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP
    );

    CREATE TABLE IF NOT EXISTS courses (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        course_code TEXT UNIQUE NOT NULL,
        course_name TEXT NOT NULL,
        description TEXT,
        status TEXT NOT NULL DEFAULT 'Active',
        created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP
    );

    CREATE TABLE IF NOT EXISTS examinations (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        course_id INTEGER NOT NULL,
        name TEXT NOT NULL,
        exam_type TEXT NOT NULL,
        duration INTEGER NOT NULL,
        max_marks INTEGER NOT NULL,
        slot_start TEXT NOT NULL,
        slot_end TEXT NOT NULL,
        booking_start TEXT NOT NULL,
        booking_end TEXT NOT NULL,
        status TEXT NOT NULL DEFAULT 'Draft',
        description TEXT,
        created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
        FOREIGN KEY(course_id) REFERENCES courses(id) ON DELETE CASCADE
    );

    CREATE TABLE IF NOT EXISTS rubrics (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        examination_id INTEGER NOT NULL,
        criterion_name TEXT NOT NULL,
        max_marks REAL NOT NULL,
        weightage REAL NOT NULL DEFAULT 100,
        description TEXT,
        FOREIGN KEY(examination_id) REFERENCES examinations(id) ON DELETE CASCADE
    );

    CREATE TABLE IF NOT EXISTS slots (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        examination_id INTEGER NOT NULL,
        examiner_id INTEGER NOT NULL,
        slot_date TEXT NOT NULL,
        start_time TEXT NOT NULL,
        end_time TEXT NOT NULL,
        capacity INTEGER NOT NULL,
        status TEXT NOT NULL DEFAULT 'Available',
        created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
        FOREIGN KEY(examination_id) REFERENCES examinations(id) ON DELETE CASCADE,
        FOREIGN KEY(examiner_id) REFERENCES users(id) ON DELETE RESTRICT
    );

    CREATE TABLE IF NOT EXISTS bookings (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        student_id INTEGER NOT NULL,
        slot_id INTEGER NOT NULL,
        booking_date TEXT NOT NULL,
        status TEXT NOT NULL DEFAULT 'Booked',
        original_slot_id INTEGER,
        updated_at TEXT,
        UNIQUE(student_id, slot_id),
        FOREIGN KEY(student_id) REFERENCES users(id) ON DELETE RESTRICT,
        FOREIGN KEY(slot_id) REFERENCES slots(id) ON DELETE RESTRICT,
        FOREIGN KEY(original_slot_id) REFERENCES slots(id) ON DELETE SET NULL
    );

    CREATE TABLE IF NOT EXISTS evaluations (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        booking_id INTEGER UNIQUE NOT NULL,
        examiner_id INTEGER NOT NULL,
        total_marks REAL NOT NULL,
        remarks TEXT,
        published INTEGER NOT NULL DEFAULT 0,
        evaluated_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
        FOREIGN KEY(booking_id) REFERENCES bookings(id) ON DELETE CASCADE,
        FOREIGN KEY(examiner_id) REFERENCES users(id) ON DELETE RESTRICT
    );

    CREATE TABLE IF NOT EXISTS evaluation_items (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        evaluation_id INTEGER NOT NULL,
        rubric_id INTEGER NOT NULL,
        marks REAL NOT NULL,
        FOREIGN KEY(evaluation_id) REFERENCES evaluations(id) ON DELETE CASCADE,
        FOREIGN KEY(rubric_id) REFERENCES rubrics(id) ON DELETE RESTRICT
    );
    ''')
    # Required pre-existing admin. Password is intentionally documented in README.
    admin = db.execute("SELECT id FROM users WHERE username = 'admin'").fetchone()
    if not admin:
        db.execute('''INSERT INTO users(username,password_hash,role,name,email,status)
                      VALUES(?,?,?,?,?,?)''',
                   ('admin', generate_password_hash('admin123'), 'admin', 'System Administrator',
                    'admin@emp.local', 'approved'))
    db.commit()


with app.app_context():
    init_db()


def q(sql, params=(), one=False):
    cur = get_db().execute(sql, params)
    rows = cur.fetchone() if one else cur.fetchall()
    cur.close()
    return rows


def execute(sql, params=()):
    db = get_db()
    cur = db.execute(sql, params)
    db.commit()
    return cur.lastrowid


def now_iso():
    return datetime.now().strftime('%Y-%m-%d %H:%M:%S')


def parse_dt(value):
    return datetime.strptime(value, '%Y-%m-%dT%H:%M')


def refresh_exam_status(exam_id):
    exam = q('SELECT * FROM examinations WHERE id=?', (exam_id,), one=True)
    if not exam or exam['status'] == 'Draft':
        return
    current = datetime.now()
    slot_start = datetime.fromisoformat(exam['slot_start'])
    slot_end = datetime.fromisoformat(exam['slot_end'])
    booking_start = datetime.fromisoformat(exam['booking_start'])
    booking_end = datetime.fromisoformat(exam['booking_end'])
    status = exam['status']
    # Draft remains explicitly controlled by Admin. Otherwise derive the lifecycle
    # from the configured windows. This also supports booking windows that overlap
    # or occur after slot creation.
    if current < slot_start:
        status = 'Draft'
    elif booking_start <= current <= booking_end:
        status = 'Booking Open'
    elif slot_start <= current <= slot_end and current < booking_start:
        status = 'Slot Creation'
    elif current > booking_end:
        status = 'Completed' if current > max(slot_end, booking_end) else 'Closed'
    if status != exam['status']:
        execute('UPDATE examinations SET status=? WHERE id=?', (status, exam_id))


def refresh_all_exam_statuses():
    for row in q('SELECT id FROM examinations'):
        refresh_exam_status(row['id'])


# --------------------------- Auth / guards ---------------------------

def current_user():
    uid = session.get('user_id')
    if not uid:
        return None
    return q('SELECT * FROM users WHERE id=?', (uid,), one=True)


@app.context_processor
def inject_globals():
    return {'current_user': current_user(), 'today': date.today().isoformat()}


def login_required(view):
    @wraps(view)
    def wrapped(*args, **kwargs):
        if not current_user():
            flash('Please log in to continue.', 'warning')
            return redirect(url_for('login'))
        return view(*args, **kwargs)
    return wrapped


def role_required(*roles):
    def decorator(view):
        @wraps(view)
        def wrapped(*args, **kwargs):
            user = current_user()
            if not user:
                flash('Please log in to continue.', 'warning')
                return redirect(url_for('login'))
            if user['role'] not in roles:
                flash('You do not have permission to access that page.', 'danger')
                return redirect(url_for('dashboard'))
            if user['role'] == 'examiner' and user['status'] != 'approved':
                session.clear()
                flash('Your examiner account is awaiting Admin approval.', 'warning')
                return redirect(url_for('login'))
            return view(*args, **kwargs)
        return wrapped
    return decorator


@app.route('/')
def index():
    if current_user():
        return redirect(url_for('dashboard'))
    return render_template('index.html')


@app.route('/login', methods=['GET', 'POST'])
def login():
    if request.method == 'POST':
        username = request.form.get('username', '').strip()
        password = request.form.get('password', '')
        user = q('SELECT * FROM users WHERE username=?', (username,), one=True)
        if user and check_password_hash(user['password_hash'], password):
            if user['role'] == 'examiner' and user['status'] != 'approved':
                flash('Examiner account is pending Admin approval.', 'warning')
                return render_template('auth/login.html')
            if user['status'] == 'deactivated':
                flash('This account has been deactivated.', 'danger')
                return render_template('auth/login.html')
            session.clear()
            session['user_id'] = user['id']
            flash(f'Welcome, {user["name"]}!', 'success')
            return redirect(url_for('dashboard'))
        flash('Invalid username or password.', 'danger')
    return render_template('auth/login.html')


@app.route('/logout')
def logout():
    session.clear()
    flash('You have been logged out.', 'info')
    return redirect(url_for('index'))


@app.route('/register/<role>', methods=['GET', 'POST'])
def register(role):
    if role not in ('student', 'examiner'):
        return redirect(url_for('index'))
    if request.method == 'POST':
        username = request.form['username'].strip()
        name = request.form['name'].strip()
        email = request.form['email'].strip().lower()
        phone = request.form.get('phone', '').strip()
        department = request.form.get('department', '').strip()
        password = request.form['password']
        confirm = request.form['confirm_password']
        if password != confirm:
            flash('Passwords do not match.', 'danger')
            return render_template('auth/register.html', role=role)
        if len(password) < 6:
            flash('Password must contain at least 6 characters.', 'danger')
            return render_template('auth/register.html', role=role)
        try:
            execute('''INSERT INTO users(username,password_hash,role,name,email,phone,department,status)
                       VALUES(?,?,?,?,?,?,?,?)''',
                    (username, generate_password_hash(password), role, name, email, phone, department,
                     'pending' if role == 'examiner' else 'approved'))
        except sqlite3.IntegrityError:
            flash('Username or email is already registered.', 'danger')
            return render_template('auth/register.html', role=role)
        if role == 'examiner':
            flash('Registration submitted. Wait for Admin approval before logging in.', 'success')
        else:
            flash('Registration successful. You can now log in.', 'success')
        return redirect(url_for('login'))
    return render_template('auth/register.html', role=role)


@app.route('/dashboard')
@login_required
def dashboard():
    user = current_user()
    if user['role'] == 'admin':
        return redirect(url_for('admin_dashboard'))
    if user['role'] == 'examiner':
        return redirect(url_for('examiner_dashboard'))
    return redirect(url_for('student_dashboard'))


# --------------------------- Admin ---------------------------

@app.route('/admin')
@role_required('admin')
def admin_dashboard():
    stats = {
        'courses': q('SELECT COUNT(*) c FROM courses', one=True)['c'],
        'examinations': q('SELECT COUNT(*) c FROM examinations', one=True)['c'],
        'examiners': q("SELECT COUNT(*) c FROM users WHERE role='examiner' AND status='approved'", one=True)['c'],
        'students': q("SELECT COUNT(*) c FROM users WHERE role='student'", one=True)['c'],
        'slots': q('SELECT COUNT(*) c FROM slots', one=True)['c'],
        'bookings': q('SELECT COUNT(*) c FROM bookings WHERE status != "Cancelled"', one=True)['c'],
    }
    recent = q('''SELECT b.id, b.status, b.booking_date, s.slot_date, s.start_time,
                 st.name student_name, e.name exam_name, ex.name examiner_name
                 FROM bookings b JOIN slots s ON b.slot_id=s.id JOIN examinations e ON s.examination_id=e.id
                 JOIN users st ON b.student_id=st.id JOIN users ex ON s.examiner_id=ex.id
                 ORDER BY b.id DESC LIMIT 8''')
    pending_examiners = q("SELECT * FROM users WHERE role='examiner' AND status='pending' ORDER BY created_at DESC")
    return render_template('admin/dashboard.html', stats=stats, recent=recent, pending_examiners=pending_examiners)


@app.route('/admin/courses')
@role_required('admin')
def admin_courses():
    courses = q('SELECT * FROM courses ORDER BY id DESC')
    return render_template('admin/courses.html', courses=courses)


@app.route('/admin/courses/create', methods=['GET', 'POST'])
@role_required('admin')
def create_course():
    if request.method == 'POST':
        try:
            execute('INSERT INTO courses(course_code,course_name,description,status) VALUES(?,?,?,?)',
                    (request.form['course_code'].strip().upper(), request.form['course_name'].strip(),
                     request.form.get('description','').strip(), request.form.get('status','Active')))
            flash('Course created.', 'success')
            return redirect(url_for('admin_courses'))
        except sqlite3.IntegrityError:
            flash('Course code already exists.', 'danger')
    return render_template('admin/course_form.html', course=None)


@app.route('/admin/courses/<int:course_id>/edit', methods=['GET', 'POST'])
@role_required('admin')
def edit_course(course_id):
    course = q('SELECT * FROM courses WHERE id=?', (course_id,), one=True)
    if not course:
        flash('Course not found.', 'danger'); return redirect(url_for('admin_courses'))
    if request.method == 'POST':
        try:
            execute('''UPDATE courses SET course_code=?, course_name=?, description=?, status=? WHERE id=?''',
                    (request.form['course_code'].strip().upper(), request.form['course_name'].strip(),
                     request.form.get('description','').strip(), request.form.get('status','Active'), course_id))
            flash('Course updated.', 'success'); return redirect(url_for('admin_courses'))
        except sqlite3.IntegrityError:
            flash('Course code already exists.', 'danger')
    return render_template('admin/course_form.html', course=course)


@app.post('/admin/courses/<int:course_id>/delete')
@role_required('admin')
def delete_course(course_id):
    try:
        execute('DELETE FROM courses WHERE id=?', (course_id,))
        flash('Course removed.', 'success')
    except sqlite3.IntegrityError:
        flash('Cannot remove a course that has examinations.', 'danger')
    return redirect(url_for('admin_courses'))


@app.route('/admin/examinations')
@role_required('admin')
def admin_examinations():
    exams = q('''SELECT e.*, c.course_code, c.course_name,
                 (SELECT COUNT(*) FROM slots s WHERE s.examination_id=e.id) slot_count,
                 (SELECT COUNT(*) FROM bookings b JOIN slots s2 ON b.slot_id=s2.id WHERE s2.examination_id=e.id AND b.status!='Cancelled') booking_count
                 FROM examinations e JOIN courses c ON e.course_id=c.id ORDER BY e.id DESC''')
    return render_template('admin/examinations.html', examinations=exams)


@app.route('/admin/examinations/create', methods=['GET', 'POST'])
@role_required('admin')
def create_examination():
    courses = q("SELECT * FROM courses WHERE status='Active' ORDER BY course_code")
    if request.method == 'POST':
        try:
            slot_start = parse_dt(request.form['slot_start']).isoformat(timespec='minutes')
            slot_end = parse_dt(request.form['slot_end']).isoformat(timespec='minutes')
            booking_start = parse_dt(request.form['booking_start']).isoformat(timespec='minutes')
            booking_end = parse_dt(request.form['booking_end']).isoformat(timespec='minutes')
            if not (slot_start < slot_end and booking_start < booking_end):
                raise ValueError
            execute('''INSERT INTO examinations(course_id,name,exam_type,duration,max_marks,slot_start,slot_end,booking_start,booking_end,status,description)
                       VALUES(?,?,?,?,?,?,?,?,?,?,?)''',
                    (request.form['course_id'], request.form['name'].strip(), request.form['exam_type'],
                     int(request.form['duration']), int(request.form['max_marks']), slot_start, slot_end,
                     booking_start, booking_end, 'Draft', request.form.get('description','').strip()))
            flash('Examination created. Add its rubric before opening the process.', 'success')
            return redirect(url_for('admin_examinations'))
        except (ValueError, TypeError):
            flash('Please provide valid dates, times and numeric values.', 'danger')
    return render_template('admin/examination_form.html', examination=None, courses=courses)


@app.route('/admin/examinations/<int:exam_id>/edit', methods=['GET', 'POST'])
@role_required('admin')
def edit_examination(exam_id):
    exam = q('SELECT * FROM examinations WHERE id=?', (exam_id,), one=True)
    courses = q("SELECT * FROM courses WHERE status='Active' ORDER BY course_code")
    if not exam:
        flash('Examination not found.', 'danger'); return redirect(url_for('admin_examinations'))
    if request.method == 'POST':
        try:
            slot_start = parse_dt(request.form['slot_start']).isoformat(timespec='minutes')
            slot_end = parse_dt(request.form['slot_end']).isoformat(timespec='minutes')
            booking_start = parse_dt(request.form['booking_start']).isoformat(timespec='minutes')
            booking_end = parse_dt(request.form['booking_end']).isoformat(timespec='minutes')
            if not (slot_start < slot_end and booking_start < booking_end): raise ValueError
            execute('''UPDATE examinations SET course_id=?,name=?,exam_type=?,duration=?,max_marks=?,slot_start=?,slot_end=?,booking_start=?,booking_end=?,description=? WHERE id=?''',
                    (request.form['course_id'], request.form['name'].strip(), request.form['exam_type'], int(request.form['duration']),
                     int(request.form['max_marks']), slot_start, slot_end, booking_start, booking_end,
                     request.form.get('description','').strip(), exam_id))
            flash('Examination updated.', 'success'); return redirect(url_for('admin_examinations'))
        except (ValueError, TypeError):
            flash('Invalid examination values.', 'danger')
    return render_template('admin/examination_form.html', examination=exam, courses=courses)


@app.post('/admin/examinations/<int:exam_id>/delete')
@role_required('admin')
def delete_examination(exam_id):
    try:
        execute('DELETE FROM examinations WHERE id=?', (exam_id,))
        flash('Examination removed.', 'success')
    except sqlite3.IntegrityError:
        flash('This examination has dependent records and cannot be removed.', 'danger')
    return redirect(url_for('admin_examinations'))


@app.route('/admin/examinations/<int:exam_id>/rubric', methods=['GET', 'POST'])
@role_required('admin')
def manage_rubric(exam_id):
    exam = q('SELECT e.*, c.course_code FROM examinations e JOIN courses c ON e.course_id=c.id WHERE e.id=?', (exam_id,), one=True)
    if not exam:
        flash('Examination not found.', 'danger'); return redirect(url_for('admin_examinations'))
    if request.method == 'POST':
        try:
            execute('INSERT INTO rubrics(examination_id,criterion_name,max_marks,weightage,description) VALUES(?,?,?,?,?)',
                    (exam_id, request.form['criterion_name'].strip(), float(request.form['max_marks']),
                     float(request.form['weightage']), request.form.get('description','').strip()))
            flash('Rubric criterion added.', 'success')
        except ValueError:
            flash('Marks and weightage must be numeric.', 'danger')
    rubrics = q('SELECT * FROM rubrics WHERE examination_id=? ORDER BY id', (exam_id,))
    return render_template('admin/rubric.html', exam=exam, rubrics=rubrics)


@app.post('/admin/rubrics/<int:rubric_id>/delete')
@role_required('admin')
def delete_rubric(rubric_id):
    exam = q('SELECT examination_id FROM rubrics WHERE id=?', (rubric_id,), one=True)
    try:
        execute('DELETE FROM rubrics WHERE id=?', (rubric_id,))
        flash('Rubric criterion removed.', 'success')
    except sqlite3.IntegrityError:
        flash('This criterion is already used in an evaluation and cannot be removed.', 'danger')
    return redirect(url_for('manage_rubric', exam_id=exam['examination_id'])) if exam else redirect(url_for('admin_examinations'))


@app.post('/admin/examinations/<int:exam_id>/status')
@role_required('admin')
def update_exam_status(exam_id):
    status = request.form['status']
    allowed = {'Draft','Slot Creation','Booking Open','Closed','Completed'}
    if status not in allowed:
        flash('Invalid status.', 'danger')
    else:
        execute('UPDATE examinations SET status=? WHERE id=?', (status, exam_id))
        flash('Examination status updated.', 'success')
    return redirect(url_for('admin_examinations'))


@app.route('/admin/students')
@role_required('admin')
def admin_students():
    students = q("SELECT * FROM users WHERE role='student' ORDER BY name")
    return render_template('admin/students.html', students=students)


@app.route('/admin/examiners')
@role_required('admin')
def admin_examiners():
    examiners = q("SELECT * FROM users WHERE role='examiner' ORDER BY CASE status WHEN 'pending' THEN 0 ELSE 1 END, id DESC")
    return render_template('admin/examiners.html', examiners=examiners)


@app.post('/admin/examiners/<int:user_id>/approve')
@role_required('admin')
def approve_examiner(user_id):
    execute("UPDATE users SET status='approved' WHERE id=? AND role='examiner'", (user_id,))
    flash('Examiner approved.', 'success'); return redirect(url_for('admin_examiners'))


@app.post('/admin/examiners/<int:user_id>/toggle')
@role_required('admin')
def toggle_examiner(user_id):
    user = q("SELECT status FROM users WHERE id=? AND role='examiner'", (user_id,), one=True)
    if user:
        new_status = 'deactivated' if user['status'] == 'approved' else 'approved'
        execute('UPDATE users SET status=? WHERE id=?', (new_status, user_id))
        flash(f'Examiner status changed to {new_status}.', 'success')
    return redirect(url_for('admin_examiners'))


@app.route('/admin/slots')
@role_required('admin')
def admin_slots():
    slots = q('''SELECT s.*, e.name exam_name, c.course_code, u.name examiner_name,
                 (SELECT COUNT(*) FROM bookings b WHERE b.slot_id=s.id AND b.status!='Cancelled') booked
                 FROM slots s JOIN examinations e ON s.examination_id=e.id
                 JOIN courses c ON e.course_id=c.id JOIN users u ON s.examiner_id=u.id
                 ORDER BY s.slot_date, s.start_time''')
    return render_template('admin/slots.html', slots=slots)


@app.route('/admin/bookings')
@role_required('admin')
def admin_bookings():
    search = request.args.get('q','').strip()
    base = '''SELECT b.*, st.name student_name, st.username student_username,
              s.slot_date,s.start_time,s.end_time,e.name exam_name,c.course_code,u.name examiner_name
              FROM bookings b JOIN users st ON b.student_id=st.id JOIN slots s ON b.slot_id=s.id
              JOIN examinations e ON s.examination_id=e.id JOIN courses c ON e.course_id=c.id JOIN users u ON s.examiner_id=u.id'''
    if search:
        bookings = q(base + ''' WHERE st.name LIKE ? OR st.username LIKE ? OR e.name LIKE ? OR c.course_code LIKE ? OR b.id LIKE ?
                              ORDER BY s.slot_date,s.start_time''', tuple([f'%{search}%']*5))
    else:
        bookings = q(base + ' ORDER BY s.slot_date,s.start_time')
    return render_template('admin/bookings.html', bookings=bookings, search=search)


@app.route('/admin/search')
@role_required('admin')
def admin_search():
    term = request.args.get('q','').strip()
    students = examiners = exams = bookings = []
    if term:
        like = f'%{term}%'
        students = q("SELECT * FROM users WHERE role='student' AND (name LIKE ? OR username LIKE ? OR email LIKE ?) ORDER BY name", (like,like,like))
        examiners = q("SELECT * FROM users WHERE role='examiner' AND (name LIKE ? OR username LIKE ? OR email LIKE ?) ORDER BY name", (like,like,like))
        exams = q('''SELECT e.*,c.course_code FROM examinations e JOIN courses c ON e.course_id=c.id
                     WHERE e.name LIKE ? OR c.course_code LIKE ? ORDER BY e.id DESC''', (like,like))
        bookings = q('''SELECT b.id, b.status, st.name student_name, e.name exam_name, s.slot_date
                        FROM bookings b JOIN users st ON b.student_id=st.id JOIN slots s ON b.slot_id=s.id
                        JOIN examinations e ON s.examination_id=e.id
                        WHERE CAST(b.id AS TEXT) LIKE ? OR st.name LIKE ? OR e.name LIKE ? ORDER BY b.id DESC''', (like,like,like))
    return render_template('admin/search.html', term=term, students=students, examiners=examiners, exams=exams, bookings=bookings)


@app.post('/admin/bookings/<int:booking_id>/reschedule')
@role_required('admin')
def reschedule_booking(booking_id):
    booking = q('SELECT * FROM bookings WHERE id=?', (booking_id,), one=True)
    slot_id = request.form.get('slot_id', type=int)
    new_slot = q('SELECT * FROM slots WHERE id=?', (slot_id,), one=True) if slot_id else None
    if not booking or not new_slot:
        flash('Booking or target slot not found.', 'danger'); return redirect(url_for('admin_bookings'))
    exam_old = q('SELECT examination_id FROM slots WHERE id=?', (booking['slot_id'],), one=True)
    exam_new = q('SELECT examination_id FROM slots WHERE id=?', (slot_id,), one=True)
    if exam_old['examination_id'] != exam_new['examination_id']:
        flash('Rescheduling must remain within the same examination.', 'danger'); return redirect(url_for('admin_bookings'))
    count = q("SELECT COUNT(*) c FROM bookings WHERE slot_id=? AND status='Booked'", (slot_id,), one=True)['c']
    if count >= new_slot['capacity']:
        flash('Target slot is full.', 'danger'); return redirect(url_for('admin_bookings'))
    execute('UPDATE bookings SET slot_id=?, status="Booked", updated_at=? WHERE id=?', (slot_id, now_iso(), booking_id))
    flash('Booking rescheduled.', 'success'); return redirect(url_for('admin_bookings'))


@app.post('/admin/bookings/<int:booking_id>/examiner')
@role_required('admin')
def change_booking_examiner(booking_id):
    booking = q('''SELECT b.*, s.examination_id FROM bookings b JOIN slots s ON b.slot_id=s.id WHERE b.id=?''', (booking_id,), one=True)
    examiner_id = request.form.get('examiner_id', type=int)
    if not booking or not examiner_id:
        flash('Invalid booking or examiner.', 'danger'); return redirect(url_for('admin_bookings'))
    examiner = q("SELECT id FROM users WHERE id=? AND role='examiner' AND status='approved'", (examiner_id,), one=True)
    if not examiner:
        flash('Examiner is not active/approved.', 'danger'); return redirect(url_for('admin_bookings'))
    slot = q('SELECT * FROM slots WHERE id=?', (booking['slot_id'],), one=True)
    execute('UPDATE slots SET examiner_id=? WHERE id=?', (examiner_id, slot['id']))
    flash('Assigned examiner changed for the booking slot.', 'success')
    return redirect(url_for('admin_bookings'))


# --------------------------- Examiner ---------------------------

@app.route('/examiner')
@role_required('examiner')
def examiner_dashboard():
    user = current_user()
    refresh_all_exam_statuses()
    assigned = q('''SELECT e.*,c.course_code,c.course_name,
                    (SELECT COUNT(*) FROM slots s WHERE s.examination_id=e.id AND s.examiner_id=?) slot_count,
                    (SELECT COUNT(*) FROM bookings b JOIN slots s2 ON b.slot_id=s2.id WHERE s2.examination_id=e.id AND s2.examiner_id=? AND b.status='Booked') booking_count
                    FROM examinations e JOIN courses c ON e.course_id=c.id
                    WHERE EXISTS(SELECT 1 FROM slots sx WHERE sx.examination_id=e.id AND sx.examiner_id=?)
                    ORDER BY e.slot_start''', (user['id'],user['id'],user['id']))
    pending = q('''SELECT b.id, st.name student_name, e.name exam_name, s.slot_date,s.start_time
                   FROM bookings b JOIN users st ON b.student_id=st.id JOIN slots s ON b.slot_id=s.id
                   JOIN examinations e ON s.examination_id=e.id
                   WHERE s.examiner_id=? AND b.status='Booked' AND NOT EXISTS(SELECT 1 FROM evaluations ev WHERE ev.booking_id=b.id)
                   ORDER BY s.slot_date,s.start_time LIMIT 10''', (user['id'],))
    slots = q('''SELECT s.*,e.name exam_name,c.course_code,
                 (SELECT COUNT(*) FROM bookings b WHERE b.slot_id=s.id AND b.status!='Cancelled') booked
                 FROM slots s JOIN examinations e ON s.examination_id=e.id JOIN courses c ON e.course_id=c.id
                 WHERE s.examiner_id=? ORDER BY s.slot_date,s.start_time LIMIT 10''', (user['id'],))
    return render_template('examiner/dashboard.html', assigned=assigned, pending=pending, slots=slots)


@app.route('/examiner/slots')
@role_required('examiner')
def examiner_slots():
    user = current_user()
    slots = q('''SELECT s.*,e.name exam_name,c.course_code,
                 (SELECT COUNT(*) FROM bookings b WHERE b.slot_id=s.id AND b.status!='Cancelled') booked
                 FROM slots s JOIN examinations e ON s.examination_id=e.id JOIN courses c ON e.course_id=c.id
                 WHERE s.examiner_id=? ORDER BY s.slot_date,s.start_time''', (user['id'],))
    exams = q("SELECT e.*,c.course_code FROM examinations e JOIN courses c ON e.course_id=c.id WHERE e.status='Slot Creation' ORDER BY e.slot_start")
    return render_template('examiner/slots.html', slots=slots, exams=exams)


@app.post('/examiner/slots/create')
@role_required('examiner')
def create_slot():
    user = current_user()
    exam_id = request.form.get('exam_id', type=int)
    exam = q('SELECT * FROM examinations WHERE id=?', (exam_id,), one=True)
    if not exam or exam['status'] != 'Slot Creation':
        flash('Slot creation is not currently open for this examination.', 'danger'); return redirect(url_for('examiner_slots'))
    try:
        d = datetime.strptime(request.form['slot_date'], '%Y-%m-%d').date()
        start = datetime.strptime(request.form['start_time'], '%H:%M').time()
        end = datetime.strptime(request.form['end_time'], '%H:%M').time()
        start_dt = datetime.combine(d,start); end_dt = datetime.combine(d,end)
        window_start = datetime.fromisoformat(exam['slot_start']); window_end = datetime.fromisoformat(exam['slot_end'])
        if not (window_start <= start_dt < end_dt <= window_end): raise ValueError
        capacity = int(request.form['capacity'])
        if capacity < 1: raise ValueError
        existing = q('''SELECT id FROM slots WHERE examination_id=? AND examiner_id=? AND slot_date=? AND start_time=? AND end_time=?''',
                     (exam_id,user['id'],d.isoformat(),request.form['start_time'],request.form['end_time']), one=True)
        if existing:
            flash('You already created an identical slot.', 'warning'); return redirect(url_for('examiner_slots'))
        execute('INSERT INTO slots(examination_id,examiner_id,slot_date,start_time,end_time,capacity,status) VALUES(?,?,?,?,?,?,?)',
                (exam_id,user['id'],d.isoformat(),request.form['start_time'],request.form['end_time'],capacity,'Available'))
        flash('Examination slot created.', 'success')
    except (ValueError, TypeError):
        flash('Slot must fall inside the examination slot-creation window.', 'danger')
    return redirect(url_for('examiner_slots'))


@app.post('/examiner/slots/<int:slot_id>/delete')
@role_required('examiner')
def delete_slot(slot_id):
    user = current_user()
    slot = q('''SELECT s.*,e.status exam_status FROM slots s JOIN examinations e ON s.examination_id=e.id
                WHERE s.id=? AND s.examiner_id=?''', (slot_id,user['id']), one=True)
    booked = q("SELECT COUNT(*) c FROM bookings WHERE slot_id=? AND status='Booked'", (slot_id,), one=True)['c'] if slot else 0
    if not slot:
        flash('Slot not found.', 'danger')
    elif slot['exam_status'] != 'Slot Creation' or booked:
        flash('A slot can only be removed during slot creation and before booking starts.', 'danger')
    else:
        execute('DELETE FROM slots WHERE id=?', (slot_id,)); flash('Slot removed.', 'success')
    return redirect(url_for('examiner_slots'))


@app.route('/examiner/slots/<int:slot_id>/students')
@role_required('examiner')
def slot_students(slot_id):
    user = current_user()
    slot = q('''SELECT s.*,e.name exam_name,c.course_code FROM slots s JOIN examinations e ON s.examination_id=e.id JOIN courses c ON e.course_id=c.id
                WHERE s.id=? AND s.examiner_id=?''', (slot_id,user['id']), one=True)
    if not slot:
        flash('Slot not found.', 'danger'); return redirect(url_for('examiner_slots'))
    students = q('''SELECT b.*,u.name,u.username,u.email FROM bookings b JOIN users u ON b.student_id=u.id
                    WHERE b.slot_id=? AND b.status='Booked' ORDER BY u.name''', (slot_id,))
    return render_template('examiner/students.html', slot=slot, students=students)


@app.route('/examiner/evaluate/<int:booking_id>', methods=['GET', 'POST'])
@role_required('examiner')
def evaluate(booking_id):
    user = current_user()
    booking = q('''SELECT b.*,s.examination_id,s.slot_date,s.start_time,e.name exam_name,e.max_marks,e.status exam_status,
                   st.name student_name, st.username student_username
                   FROM bookings b JOIN slots s ON b.slot_id=s.id JOIN examinations e ON s.examination_id=e.id
                   JOIN users st ON b.student_id=st.id
                   WHERE b.id=? AND s.examiner_id=? AND b.status='Booked' ''', (booking_id,user['id']), one=True)
    if not booking:
        flash('You can evaluate only students booked into your own slots.', 'danger'); return redirect(url_for('examiner_dashboard'))
    rubrics = q('SELECT * FROM rubrics WHERE examination_id=? ORDER BY id', (booking['examination_id'],))
    existing = q('SELECT * FROM evaluations WHERE booking_id=?', (booking_id,), one=True)
    items = {r['rubric_id']: r['marks'] for r in q('SELECT rubric_id,marks FROM evaluation_items WHERE evaluation_id=?', (existing['id'],))} if existing else {}
    if request.method == 'POST':
        if not rubrics:
            flash('Admin has not configured a rubric for this examination.', 'danger')
            return render_template('examiner/evaluate.html', booking=booking, rubrics=rubrics, existing=existing, items=items)
        marks_list = []
        try:
            for rubric in rubrics:
                value = float(request.form.get(f'marks_{rubric["id"]}', '0'))
                if value < 0 or value > rubric['max_marks']: raise ValueError
                marks_list.append((rubric['id'], value))
            total = sum(v for _,v in marks_list)
            remarks = request.form.get('remarks','').strip()
            db = get_db()
            if existing:
                db.execute('UPDATE evaluations SET examiner_id=?,total_marks=?,remarks=?,published=1,evaluated_at=? WHERE id=?',
                           (user['id'],total,remarks,now_iso(),existing['id']))
                evaluation_id = existing['id']
                db.execute('DELETE FROM evaluation_items WHERE evaluation_id=?', (evaluation_id,))
            else:
                cur = db.execute('INSERT INTO evaluations(booking_id,examiner_id,total_marks,remarks,published,evaluated_at) VALUES(?,?,?,?,1,?)',
                                 (booking_id,user['id'],total,remarks,now_iso()))
                evaluation_id = cur.lastrowid
            for rid, value in marks_list:
                db.execute('INSERT INTO evaluation_items(evaluation_id,rubric_id,marks) VALUES(?,?,?)', (evaluation_id,rid,value))
            db.commit()
            execute('UPDATE bookings SET status="Completed", updated_at=? WHERE id=?', (now_iso(),booking_id))
            flash('Evaluation submitted and result published.', 'success')
            return redirect(url_for('examiner_dashboard'))
        except ValueError:
            flash('Marks must be valid and within each criterion maximum.', 'danger')
    return render_template('examiner/evaluate.html', booking=booking, rubrics=rubrics, existing=existing, items=items)


# --------------------------- Student ---------------------------

@app.route('/student')
@role_required('student')
def student_dashboard():
    user = current_user()
    available = q('''SELECT e.*,c.course_code,c.course_name,
                     (SELECT COUNT(*) FROM slots s WHERE s.examination_id=e.id AND s.status!='Cancelled'
                      AND (SELECT COUNT(*) FROM bookings b WHERE b.slot_id=s.id AND b.status='Booked') < s.capacity) available_slots
                     FROM examinations e JOIN courses c ON e.course_id=c.id
                     WHERE e.status='Booking Open' ORDER BY e.booking_end''')
    upcoming = q('''SELECT b.*,s.slot_date,s.start_time,s.end_time,e.name exam_name,c.course_code,u.name examiner_name
                    FROM bookings b JOIN slots s ON b.slot_id=s.id JOIN examinations e ON s.examination_id=e.id
                    JOIN courses c ON e.course_id=c.id JOIN users u ON s.examiner_id=u.id
                    WHERE b.student_id=? AND b.status='Booked' ORDER BY s.slot_date,s.start_time''', (user['id'],))
    results = q('''SELECT b.id booking_id,e.name exam_name,c.course_code,ev.total_marks,e.max_marks,ev.remarks,ev.evaluated_at
                   FROM bookings b JOIN slots s ON b.slot_id=s.id JOIN examinations e ON s.examination_id=e.id
                   JOIN courses c ON e.course_id=c.id JOIN evaluations ev ON ev.booking_id=b.id
                   WHERE b.student_id=? AND ev.published=1 ORDER BY ev.evaluated_at DESC''', (user['id'],))
    return render_template('student/dashboard.html', available=available, upcoming=upcoming, results=results)


@app.route('/student/examinations')
@role_required('student')
def student_examinations():
    term = request.args.get('q','').strip()
    exam_type = request.args.get('type','').strip()
    sql = '''SELECT e.*,c.course_code,c.course_name,
             (SELECT COUNT(*) FROM slots s WHERE s.examination_id=e.id AND s.status!='Cancelled'
              AND (SELECT COUNT(*) FROM bookings b WHERE b.slot_id=s.id AND b.status='Booked') < s.capacity) available_slots
             FROM examinations e JOIN courses c ON e.course_id=c.id WHERE e.status IN ('Booking Open','Closed','Completed')'''
    params=[]
    if term:
        sql += ' AND (e.name LIKE ? OR c.course_code LIKE ? OR c.course_name LIKE ?)'; params += [f'%{term}%']*3
    if exam_type:
        sql += ' AND e.exam_type=?'; params.append(exam_type)
    sql += ' ORDER BY e.booking_end DESC'
    exams = q(sql, tuple(params))
    return render_template('student/examinations.html', examinations=exams, term=term, exam_type=exam_type)


@app.route('/student/examinations/<int:exam_id>')
@role_required('student')
def exam_detail(exam_id):
    user = current_user()
    exam = q('''SELECT e.*,c.course_code,c.course_name FROM examinations e JOIN courses c ON e.course_id=c.id WHERE e.id=?''', (exam_id,), one=True)
    if not exam:
        flash('Examination not found.', 'danger'); return redirect(url_for('student_examinations'))
    slots = q('''SELECT s.*,u.name examiner_name,
                 (SELECT COUNT(*) FROM bookings b WHERE b.slot_id=s.id AND b.status='Booked') booked,
                 (SELECT COUNT(*) FROM bookings b2 WHERE b2.slot_id=s.id AND b2.student_id=? AND b2.status='Booked') mine
                 FROM slots s JOIN users u ON s.examiner_id=u.id WHERE s.examination_id=? AND s.status!='Cancelled'
                 ORDER BY s.slot_date,s.start_time''', (user['id'],exam_id))
    booked_exam = q('''SELECT b.id,b.status,s.slot_date,s.start_time,s.end_time FROM bookings b JOIN slots s ON b.slot_id=s.id
                       WHERE b.student_id=? AND s.examination_id=? AND b.status='Booked' ''', (user['id'],exam_id), one=True)
    rubrics = q('SELECT * FROM rubrics WHERE examination_id=? ORDER BY id', (exam_id,))
    return render_template('student/exam_detail.html', exam=exam, slots=slots, booked_exam=booked_exam, rubrics=rubrics)


@app.post('/student/slots/<int:slot_id>/book')
@role_required('student')
def book_slot(slot_id):
    user = current_user()
    slot = q('''SELECT s.*,e.status exam_status,e.booking_start,e.booking_end FROM slots s JOIN examinations e ON s.examination_id=e.id WHERE s.id=?''', (slot_id,), one=True)
    if not slot:
        flash('Slot not found.', 'danger'); return redirect(url_for('student_examinations'))
    now = datetime.now()
    if slot['exam_status'] != 'Booking Open' or not (datetime.fromisoformat(slot['booking_start']) <= now <= datetime.fromisoformat(slot['booking_end'])):
        flash('Booking is not open for this examination.', 'danger'); return redirect(url_for('exam_detail', exam_id=slot['examination_id']))
    if q("SELECT id FROM bookings WHERE student_id=? AND slot_id=? AND status='Booked'", (user['id'],slot_id), one=True):
        flash('You already booked this slot.', 'warning'); return redirect(url_for('exam_detail', exam_id=slot['examination_id']))
    if q("SELECT b.id FROM bookings b JOIN slots s ON b.slot_id=s.id WHERE b.student_id=? AND s.examination_id=? AND b.status='Booked'", (user['id'],slot['examination_id']), one=True):
        flash('You already have a booking for this examination.', 'warning'); return redirect(url_for('exam_detail', exam_id=slot['examination_id']))
    booked = q("SELECT COUNT(*) c FROM bookings WHERE slot_id=? AND status='Booked'", (slot_id,), one=True)['c']
    if booked >= slot['capacity']:
        execute("UPDATE slots SET status='Full' WHERE id=?", (slot_id,))
        flash('This slot is full.', 'danger'); return redirect(url_for('exam_detail', exam_id=slot['examination_id']))
    execute('INSERT INTO bookings(student_id,slot_id,booking_date,status) VALUES(?,?,?,?)', (user['id'],slot_id,now_iso(),'Booked'))
    booked += 1
    if booked >= slot['capacity']:
        execute("UPDATE slots SET status='Full' WHERE id=?", (slot_id,))
    flash('Examination slot booked successfully.', 'success')
    return redirect(url_for('student_dashboard'))


@app.post('/student/bookings/<int:booking_id>/cancel')
@role_required('student')
def cancel_booking(booking_id):
    user = current_user()
    booking = q('''SELECT b.*,s.examination_id,e.booking_end FROM bookings b JOIN slots s ON b.slot_id=s.id JOIN examinations e ON s.examination_id=e.id
                   WHERE b.id=? AND b.student_id=? AND b.status='Booked' ''', (booking_id,user['id']), one=True)
    if not booking:
        flash('Booking not found.', 'danger'); return redirect(url_for('student_dashboard'))
    if datetime.now() > datetime.fromisoformat(booking['booking_end']):
        flash('The booking deadline has passed; cancellation is not allowed.', 'danger'); return redirect(url_for('student_dashboard'))
    execute('UPDATE bookings SET status="Cancelled", updated_at=? WHERE id=?', (now_iso(),booking_id))
    execute("UPDATE slots SET status='Available' WHERE id=? AND status='Full'", (booking['slot_id'],))
    flash('Booking cancelled.', 'success')
    return redirect(url_for('student_dashboard'))


@app.route('/student/history')
@role_required('student')
def student_history():
    user = current_user()
    history = q('''SELECT b.*,s.slot_date,s.start_time,s.end_time,e.name exam_name,c.course_code,u.name examiner_name,
                   ev.id evaluation_id,ev.total_marks,ev.remarks,ev.published
                   FROM bookings b JOIN slots s ON b.slot_id=s.id JOIN examinations e ON s.examination_id=e.id
                   JOIN courses c ON e.course_id=c.id JOIN users u ON s.examiner_id=u.id
                   LEFT JOIN evaluations ev ON ev.booking_id=b.id
                   WHERE b.student_id=? ORDER BY b.booking_date DESC''', (user['id'],))
    return render_template('student/history.html', history=history)


@app.route('/student/profile', methods=['GET','POST'])
@role_required('student')
def student_profile():
    user = current_user()
    if request.method == 'POST':
        name=request.form['name'].strip(); email=request.form['email'].strip().lower(); phone=request.form.get('phone','').strip()
        try:
            execute('UPDATE users SET name=?,email=?,phone=? WHERE id=?', (name,email,phone,user['id']))
            flash('Profile updated.', 'success')
        except sqlite3.IntegrityError:
            flash('That email is already in use.', 'danger')
        return redirect(url_for('student_profile'))
    return render_template('student/profile.html', user=user)


# --------------------------- Error handlers ---------------------------

@app.errorhandler(404)
def not_found(e):
    return render_template('error.html', code=404, message='Page not found.'), 404


@app.errorhandler(500)
def server_error(e):
    return render_template('error.html', code=500, message='Something went wrong on the server.'), 500


if __name__ == '__main__':
    app.run(debug=True)
