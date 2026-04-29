import os
import random
from datetime import datetime, timedelta
from functools import wraps

from flask import Flask, render_template, request, redirect, url_for, session, flash
from flask_mail import Mail, Message
from flask_sqlalchemy import SQLAlchemy
from sqlalchemy import text
from werkzeug.security import check_password_hash, generate_password_hash


app = Flask(__name__)
app.secret_key = os.environ.get('SECRET_KEY', 'change-this-secret-key')

# ------------------- DATABASE CONFIG -------------------
app.config['SQLALCHEMY_DATABASE_URI'] = os.environ.get(
    'DATABASE_URL',
    'mysql+mysqlconnector://root:1234@localhost/exammate'
)
app.config['SQLALCHEMY_TRACK_MODIFICATIONS'] = False

# ------------------- EMAIL CONFIG -------------------
app.config['MAIL_SERVER'] = os.environ.get('MAIL_SERVER', 'smtp.gmail.com')
app.config['MAIL_PORT'] = int(os.environ.get('MAIL_PORT', 587))
app.config['MAIL_USE_TLS'] = True
app.config['MAIL_USERNAME'] = os.environ.get('MAIL_USERNAME', 'nikita.kirar04@gmail.com')
app.config['MAIL_PASSWORD'] = os.environ.get('MAIL_PASSWORD', 'your_app_password')

db = SQLAlchemy(app)
mail = Mail(app)


# ------------------- DATABASE MODELS -------------------
class User(db.Model):
    __tablename__ = 'users'

    id = db.Column(db.Integer, primary_key=True)
    username = db.Column(db.String(100), nullable=False)
    email = db.Column(db.String(120), unique=True, nullable=False)
    password_hash = db.Column(db.String(255), nullable=False)
    role = db.Column(db.String(20), nullable=False, default='student')


class Exam(db.Model):
    __tablename__ = 'exams'

    id = db.Column(db.Integer, primary_key=True)
    exam_name = db.Column(db.String(100), nullable=False)
    subject = db.Column(db.String(100), nullable=False)
    total_questions = db.Column(db.Integer, nullable=False)
    time_limit_minutes = db.Column(db.Integer, nullable=False, default=30)
    difficulty = db.Column(db.String(20), nullable=False, default='medium')
    created_by = db.Column(db.Integer, db.ForeignKey('users.id'), nullable=False)
    created_at = db.Column(db.DateTime, server_default=db.func.now())
    status = db.Column(db.Enum('draft', 'active', 'completed'), default='draft', nullable=False)

    admin = db.relationship('User', backref=db.backref('created_exams', lazy=True))


class Question(db.Model):
    __tablename__ = 'questions'

    id = db.Column(db.Integer, primary_key=True)
    exam_id = db.Column(db.Integer, db.ForeignKey('exams.id'), nullable=False)
    question_text = db.Column(db.String(500), nullable=False)
    option_a = db.Column(db.String(200), nullable=False)
    option_b = db.Column(db.String(200), nullable=False)
    option_c = db.Column(db.String(200), nullable=False)
    option_d = db.Column(db.String(200), nullable=False)
    correct_option = db.Column(db.String(1), nullable=False)

    exam = db.relationship('Exam', backref=db.backref('questions', lazy=True, cascade='all, delete-orphan'))


# Backward-compatible alias for templates/routes that used the plural name.
Questions = Question


class Result(db.Model):
    __tablename__ = 'results'

    id = db.Column(db.Integer, primary_key=True)
    exam_id = db.Column(db.Integer, db.ForeignKey('exams.id'), nullable=False)
    student_id = db.Column(db.Integer, db.ForeignKey('users.id'), nullable=False)
    score = db.Column(db.Float, nullable=False)
    total_questions = db.Column(db.Integer, nullable=False)
    feedback = db.Column(db.String(255), nullable=False, default='')
    submitted_at = db.Column(db.DateTime, default=db.func.current_timestamp())

    exam = db.relationship('Exam', backref=db.backref('results', lazy=True))
    student = db.relationship('User', backref=db.backref('results', lazy=True))


# ------------------- HELPERS -------------------
def current_user():
    if 'user_id' not in session:
        return None
    return User.query.get(session['user_id'])


def login_required(view):
    @wraps(view)
    def wrapped_view(*args, **kwargs):
        if 'user_id' not in session:
            return redirect(url_for('home'))
        return view(*args, **kwargs)
    return wrapped_view


def role_required(*roles):
    def decorator(view):
        @wraps(view)
        def wrapped_view(*args, **kwargs):
            if 'user_id' not in session or session.get('role') not in roles:
                return redirect(url_for('home'))
            return view(*args, **kwargs)
        return wrapped_view
    return decorator


def is_admin_role(role):
    return role in ('admin', 'teacher')


def admin_required(view):
    @wraps(view)
    def wrapped_view(*args, **kwargs):
        if 'user_id' not in session or not is_admin_role(session.get('role')):
            return redirect(url_for('home'))
        return view(*args, **kwargs)
    return wrapped_view


def calculate_feedback(score, total_questions):
    if total_questions == 0:
        return 'No questions were available for this exam.'

    percentage = (score / total_questions) * 100
    if percentage >= 80:
        return 'Excellent performance. Keep it up!'
    if percentage >= 50:
        return 'Good attempt. Review the incorrect topics and try again.'
    return 'Needs improvement. Study the subject again and practice more questions.'


def sync_legacy_schema():
    """Adds missing columns for older local databases created from the first SQL file."""
    database_name = db.session.execute(text('SELECT DATABASE()')).scalar()
    if not database_name:
        return

    checks = {
        'users.role': "ALTER TABLE users ADD COLUMN role VARCHAR(20) NOT NULL DEFAULT 'student'",
        'exams.time_limit_minutes': "ALTER TABLE exams ADD COLUMN time_limit_minutes INT NOT NULL DEFAULT 30",
        'exams.difficulty': "ALTER TABLE exams ADD COLUMN difficulty VARCHAR(20) NOT NULL DEFAULT 'medium'",
        'results.feedback': "ALTER TABLE results ADD COLUMN feedback VARCHAR(255) NOT NULL DEFAULT ''",
    }

    for table_column, alter_sql in checks.items():
        table_name, column_name = table_column.split('.')
        exists = db.session.execute(
            text(
                """
                SELECT COUNT(*)
                FROM INFORMATION_SCHEMA.COLUMNS
                WHERE TABLE_SCHEMA = :database_name
                  AND TABLE_NAME = :table_name
                  AND COLUMN_NAME = :column_name
                """
            ),
            {
                'database_name': database_name,
                'table_name': table_name,
                'column_name': column_name,
            }
        ).scalar()
        if not exists:
            db.session.execute(text(alter_sql))
    db.session.commit()


# ------------------- ROUTES -------------------
@app.route('/')
def home():
    return render_template('login.html')


@app.route('/signup')
def signup_choice():
    return render_template('signup.html')


@app.route('/signup/student', methods=['GET', 'POST'])
def signup_student():
    if request.method == 'POST':
        username = request.form['username'].strip()
        email = request.form['email'].strip().lower()
        password = request.form['password']

        if User.query.filter_by(email=email).first():
            return render_template('signup_student.html', error='Email already registered.')

        user = User(
            username=username,
            email=email,
            password_hash=generate_password_hash(password),
            role='student'
        )
        db.session.add(user)
        db.session.commit()
        return redirect(url_for('home'))
    return render_template('signup_student.html')


@app.route('/signup/teacher', methods=['GET', 'POST'])
@app.route('/signup/admin', methods=['GET', 'POST'])
def signup_teacher():
    if request.method == 'POST':
        secret_code = request.form['secret_code']
        if secret_code != os.environ.get('ADMIN_SECRET_CODE', 'EXAMMATE2025'):
            return render_template('signup_teacher.html', error='Invalid admin code.')

        username = request.form['username'].strip()
        email = request.form['email'].strip().lower()
        password = request.form['password']

        if User.query.filter_by(email=email).first():
            return render_template('signup_teacher.html', error='Email already registered.')

        admin = User(
            username=username,
            email=email,
            password_hash=generate_password_hash(password),
            role='admin'
        )
        db.session.add(admin)
        db.session.commit()
        return redirect(url_for('home'))
    return render_template('signup_teacher.html')


@app.route('/login', methods=['POST'])
def login():
    email = request.form['email'].strip().lower()
    password = request.form['password']

    user = User.query.filter_by(email=email).first()
    if not user or not check_password_hash(user.password_hash, password):
        return render_template('login.html', error='Invalid email or password.')

    session.clear()
    session['user_id'] = user.id
    session['username'] = user.username
    session['role'] = user.role

    if is_admin_role(user.role):
        return redirect(url_for('teacher_dashboard'))
    return redirect(url_for('student_dashboard'))


@app.route('/student/dashboard')
@role_required('student')
def student_dashboard():
    active_exams = Exam.query.filter_by(status='active').all()
    student_results = (
        Result.query
        .filter_by(student_id=session['user_id'])
        .order_by(Result.submitted_at.desc())
        .all()
    )
    return render_template(
        'student_dashboard.html',
        username=session['username'],
        active_exams=active_exams,
        results=student_results
    )


@app.route('/teacher/dashboard')
@app.route('/admin/dashboard')
@admin_required
def teacher_dashboard():
    admin_id = session['user_id']
    exams = Exam.query.filter_by(created_by=admin_id).all()
    total_results = (
        Result.query
        .join(Exam)
        .filter(Exam.created_by == admin_id)
        .count()
    )
    return render_template(
        'teacher_dashboard.html',
        username=session['username'],
        exams=exams,
        total_results=total_results
    )


@app.route('/create_exam', methods=['GET', 'POST'])
@admin_required
def create_exam():
    if request.method == 'POST':
        exam_name = request.form['exam_name'].strip()
        subject = request.form['subject'].strip()
        difficulty = request.form.get('difficulty', 'medium')
        total_questions = int(request.form['total_questions'])
        time_limit_minutes = int(request.form.get('time_limit_minutes') or 30)

        if total_questions < 1:
            return render_template('create_exam.html', error='Total questions must be at least 1.')
        if time_limit_minutes < 1:
            return render_template('create_exam.html', error='Time limit must be at least 1 minute.')

        exam = Exam(
            exam_name=exam_name,
            subject=subject,
            difficulty=difficulty,
            total_questions=total_questions,
            time_limit_minutes=time_limit_minutes,
            created_by=session['user_id'],
            status='draft'
        )
        db.session.add(exam)
        db.session.commit()
        return redirect(url_for('manage_questions', exam_id=exam.id))

    return render_template('create_exam.html')


@app.route('/generate-quiz/<int:exam_id>')
@admin_required
def generate_quiz(exam_id):
    exam = Exam.query.get_or_404(exam_id)
    if exam.created_by != session['user_id']:
        return 'Unauthorized', 403
    return redirect(url_for('manage_questions', exam_id=exam.id))


@app.route('/manage_questions', methods=['GET', 'POST'])
@admin_required
def manage_questions():
    exams = Exam.query.filter_by(created_by=session['user_id']).order_by(Exam.created_at.desc()).all()
    selected_exam_id = request.args.get('exam_id', type=int)

    if request.method == 'POST':
        exam_id = request.form.get('exam_id', type=int)
        exam = Exam.query.get_or_404(exam_id)
        if exam.created_by != session['user_id']:
            return 'Unauthorized', 403

        question = Question(
            exam_id=exam_id,
            question_text=request.form['question_text'].strip(),
            option_a=request.form['option_a'].strip(),
            option_b=request.form['option_b'].strip(),
            option_c=request.form['option_c'].strip(),
            option_d=request.form['option_d'].strip(),
            correct_option=request.form['correct_option'].strip().upper()
        )
        db.session.add(question)
        db.session.commit()
        return redirect(url_for('manage_questions', exam_id=exam_id))

    questions_query = Question.query.join(Exam).filter(Exam.created_by == session['user_id'])
    if selected_exam_id:
        questions_query = questions_query.filter(Question.exam_id == selected_exam_id)
    questions = questions_query.order_by(Question.id.desc()).all()
    return render_template(
        'manage_questions.html',
        exams=exams,
        questions=questions,
        selected_exam_id=selected_exam_id
    )


@app.route('/view_results')
@admin_required
def view_results():
    exams = Exam.query.filter_by(created_by=session['user_id']).order_by(Exam.created_at.desc()).all()

    exam_data = []
    for exam in exams:
        results = Result.query.filter_by(exam_id=exam.id).all()
        total_students = len(results)
        avg_score = sum(result.score for result in results) / total_students if total_students else 0
        exam_data.append({
            'exam': exam,
            'total_students': total_students,
            'average_score': round(avg_score, 2)
        })

    return render_template('view_results.html', exam_data=exam_data)


@app.route('/view_results/<int:exam_id>')
@admin_required
def exam_result_detail(exam_id):
    exam = Exam.query.get_or_404(exam_id)
    if exam.created_by != session['user_id']:
        return 'Unauthorized', 403

    results = Result.query.filter_by(exam_id=exam_id).order_by(Result.submitted_at.desc()).all()
    return render_template('exam_result_detail.html', exam=exam, results=results)


@app.route('/results')
@role_required('student')
def student_results():
    results = (
        Result.query
        .filter_by(student_id=session['user_id'])
        .order_by(Result.submitted_at.desc())
        .all()
    )
    return render_template('student_results.html', results=results)


@app.route('/start_exam')
@admin_required
def start_exam_list():
    exams = Exam.query.filter_by(created_by=session['user_id']).order_by(Exam.created_at.desc()).all()
    return render_template('start_exam.html', exams=exams)


@app.route('/start_exam/<int:exam_id>')
@admin_required
def start_exam_for_students(exam_id):
    exam = Exam.query.get_or_404(exam_id)
    if exam.created_by != session['user_id']:
        return 'Exam not found or unauthorized', 404

    question_count = Question.query.filter_by(exam_id=exam.id).count()
    if question_count == 0:
        flash('Add questions before starting the exam.')
        return redirect(url_for('manage_questions', exam_id=exam.id))

    exam.total_questions = question_count
    exam.status = 'active'
    db.session.commit()
    return redirect(url_for('start_exam_list'))


@app.route('/complete_exam/<int:exam_id>')
@admin_required
def complete_exam(exam_id):
    exam = Exam.query.get_or_404(exam_id)
    if exam.created_by != session['user_id']:
        return 'Exam not found or unauthorized', 404
    exam.status = 'completed'
    db.session.commit()
    return redirect(url_for('start_exam_list'))


@app.route('/student/available_exams')
@role_required('student')
def available_exams():
    attempted_exam_ids = [
        result.exam_id
        for result in Result.query.filter_by(student_id=session['user_id']).all()
    ]
    active_exams = (
        Exam.query
        .filter(Exam.status == 'active')
        .filter(~Exam.id.in_(attempted_exam_ids) if attempted_exam_ids else text('1=1'))
        .all()
    )
    return render_template('available_exams.html', exams=active_exams)


@app.route('/take_exam/<int:exam_id>', methods=['GET', 'POST'])
@role_required('student')
def take_exam(exam_id):
    exam = Exam.query.get_or_404(exam_id)
    if exam.status != 'active':
        return 'Exam not available', 404

    existing_result = Result.query.filter_by(
        exam_id=exam_id,
        student_id=session['user_id']
    ).first()
    if existing_result:
        return redirect(url_for('student_results'))

    questions = Question.query.filter_by(exam_id=exam_id).order_by(Question.id.asc()).all()
    if not questions:
        return 'No questions available for this exam.', 404

    session_key = f'exam_{exam_id}_started_at'
    if session_key not in session:
        session[session_key] = datetime.utcnow().isoformat()

    started_at = datetime.fromisoformat(session[session_key])
    deadline = started_at + timedelta(minutes=exam.time_limit_minutes)
    remaining_seconds = max(0, int((deadline - datetime.utcnow()).total_seconds()))

    if request.method == 'POST' or remaining_seconds == 0:
        score = 0
        for question in questions:
            submitted_answer = request.form.get(f'q{question.id}', '').upper()
            if submitted_answer == question.correct_option.upper():
                score += 1

        result = Result(
            exam_id=exam_id,
            student_id=session['user_id'],
            score=score,
            total_questions=len(questions),
            feedback=calculate_feedback(score, len(questions))
        )
        db.session.add(result)
        db.session.commit()
        session.pop(session_key, None)
        return redirect(url_for('student_results'))

    return render_template(
        'take_exam.html',
        exam=exam,
        questions=questions,
        remaining_seconds=remaining_seconds
    )


@app.route('/forgot-password', methods=['GET', 'POST'])
def forgot_password():
    if request.method == 'POST':
        email = request.form['email'].strip().lower()
        user = User.query.filter_by(email=email).first()

        if not user:
            return render_template('forgot.html', error='Email not found.')

        otp = str(random.randint(100000, 999999))
        session['reset_email'] = email
        session['reset_otp'] = otp

        msg = Message(
            'Your Password Reset OTP',
            sender=app.config['MAIL_USERNAME'],
            recipients=[email]
        )
        msg.body = f'Your OTP is: {otp}'
        mail.send(msg)
        return redirect(url_for('verify_otp'))

    return render_template('forgot.html')


@app.route('/verify-otp', methods=['GET', 'POST'])
def verify_otp():
    if request.method == 'POST':
        entered_otp = request.form['otp']
        if entered_otp == session.get('reset_otp'):
            return redirect(url_for('reset_password'))
        return render_template('verify_otp.html', error='Invalid OTP.')
    return render_template('verify_otp.html')


@app.route('/reset-password', methods=['GET', 'POST'])
def reset_password():
    if request.method == 'POST':
        email = session.get('reset_email')
        if email:
            user = User.query.filter_by(email=email).first()
            if user:
                user.password_hash = generate_password_hash(request.form['password'])
                db.session.commit()

            session.pop('reset_email', None)
            session.pop('reset_otp', None)
            return redirect(url_for('home'))

    return render_template('reset_password.html')


@app.route('/profile')
@login_required
def profile():
    return render_template('student_dashboard.html', username=session['username'])


@app.route('/logout')
def logout():
    session.clear()
    return redirect(url_for('home'))


# ------------------- RUN -------------------
if __name__ == '__main__':
    with app.app_context():
        db.create_all()
        sync_legacy_schema()
    app.run(debug=True)
