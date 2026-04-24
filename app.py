import json
import os
import random
import re
import shutil
import string
from datetime import datetime, timedelta
from functools import wraps
from urllib.parse import quote

from flask import (
    Flask,
    flash,
    jsonify,
    redirect,
    render_template,
    request,
    send_from_directory,
    session,
    url_for,
)
from werkzeug.security import check_password_hash, generate_password_hash
from werkzeug.utils import secure_filename

app = Flask(__name__)
app.secret_key = 'your_secret_key_here'
app.config['UPLOAD_FOLDER'] = 'quizzes'
app.config['MAX_CONTENT_LENGTH'] = 16 * 1024 * 1024  # 16MB max upload

ALLOWED_EXTENSIONS = {'png', 'jpg', 'jpeg', 'gif', 'webp'}
DATA_DIR = 'data'
USERS_FILE = os.path.join(DATA_DIR, 'users.json')
RESULTS_FILE = os.path.join(DATA_DIR, 'results.json')
PARTIES_FILE = os.path.join(DATA_DIR, 'parties.json')
DEFAULT_SEEDED_USER = 'susye'
DEFAULT_SEEDED_PASSWORD = 'susye123'
EMPTY_PARTY_TTL_SECONDS = 10
ONLINE_WINDOW_SECONDS = 120
AVATAR_UPLOAD_DIR = os.path.join('static', 'uploads', 'avatars')
FINISHED_PARTY_TTL_SECONDS = 30
OWNER_OFFLINE_PARTY_TTL_SECONDS = 45
CLASSIC_CORRECT_POINTS = 25
CLASSIC_WRONG_POINTS = -10
PARTY_BASE_CORRECT_POINTS = 300
PARTY_SPEED_BONUS_MAX = 300
PARTY_WRONG_POINTS = -150
PARTY_SKIP_POINTS = -100
PRESENCE_TOUCH_INTERVAL_SECONDS = 20


# ----------------------------
# Utility helpers
# ----------------------------
def ensure_dir(path):
    os.makedirs(path, exist_ok=True)


def allowed_file(filename):
    return '.' in filename and filename.rsplit('.', 1)[1].lower() in ALLOWED_EXTENSIONS


def safe_load_json(path, default):
    if not os.path.exists(path):
        return default
    try:
        with open(path, 'r', encoding='utf-8') as f:
            return json.load(f)
    except Exception:
        return default


def safe_dump_json(path, data):
    with open(path, 'w', encoding='utf-8') as f:
        json.dump(data, f, indent=4, ensure_ascii=False)


def normalize_username(username):
    return (username or '').strip().lower()


def avatar_fallback_url(username):
    safe_name = quote((username or 'User').strip() or 'User')
    return f'https://ui-avatars.com/api/?name={safe_name}&background=1f2937&color=ffffff'


def normalize_avatar_value(avatar):
    value = (avatar or '').strip()
    if not value:
        return ''
    if value.startswith(('http://', 'https://', '/')):
        return value
    local_candidate = os.path.join(AVATAR_UPLOAD_DIR, value)
    if os.path.exists(local_candidate):
        return f'/uploads/avatars/{value}'
    return ''


def ensure_user_points_fields(user):
    if not isinstance(user, dict):
        return {'classic_points': 0, 'party_points': 0, 'global_points': 0}
    user['classic_points'] = int(user.get('classic_points', 0) or 0)
    user['party_points'] = int(user.get('party_points', 0) or 0)
    user['global_points'] = int(user.get('classic_points', 0) or 0) + int(user.get('party_points', 0) or 0)
    return user


def display_avatar_url(user):
    avatar = normalize_avatar_value((user or {}).get('avatar', ''))
    if avatar:
        return avatar
    return avatar_fallback_url((user or {}).get('username', 'User'))


def is_local_avatar_path(path):
    return isinstance(path, str) and path.startswith('/uploads/avatars/')


def save_avatar_upload(file, user_key):
    if not file or not file.filename or not allowed_file(file.filename):
        return None
    ext = file.filename.rsplit('.', 1)[1].lower()
    safe_key = secure_filename(user_key or 'user') or 'user'
    filename = f"{safe_key}_{int(datetime.now().timestamp())}_{random.randint(1000, 9999)}.{ext}"
    full_path = os.path.join(AVATAR_UPLOAD_DIR, filename)
    file.save(full_path)
    return filename


# ----------------------------
# Data stores
# ----------------------------
def load_users():
    return safe_load_json(USERS_FILE, {})


def save_users(users):
    safe_dump_json(USERS_FILE, users)


def load_results():
    return safe_load_json(RESULTS_FILE, [])


def save_results(results):
    safe_dump_json(RESULTS_FILE, results)


def load_parties():
    return load_and_prepare_parties()


def save_parties(parties):
    safe_dump_json(PARTIES_FILE, parties)


def ensure_storage():
    ensure_dir('quizzes')
    ensure_dir(DATA_DIR)
    ensure_dir(AVATAR_UPLOAD_DIR)

    if not os.path.exists(USERS_FILE):
        save_users({})
    if not os.path.exists(RESULTS_FILE):
        save_results([])
    if not os.path.exists(PARTIES_FILE):
        save_parties([])

    users = load_users()
    seeded_key = normalize_username(DEFAULT_SEEDED_USER)
    if seeded_key not in users:
        users[seeded_key] = ensure_user_points_fields(
            {
            'username': DEFAULT_SEEDED_USER,
            'password_hash': generate_password_hash(DEFAULT_SEEDED_PASSWORD),
            'description': 'Demo user account',
            'avatar': '',
            'created_at': datetime.now().isoformat(),
            }
        )
        save_users(users)
    else:
        changed = False
        for key, user in users.items():
            if not isinstance(user, dict):
                continue
            before = (user.get('classic_points'), user.get('party_points'), user.get('global_points'))
            user = ensure_user_points_fields(user)
            after = (user.get('classic_points'), user.get('party_points'), user.get('global_points'))
            if before != after:
                changed = True
            users[key] = user
        if changed:
            save_users(users)


def current_user():
    user_key = session.get('username')
    if not user_key:
        return None

    users = load_users()
    user = users.get(user_key)
    if not user:
        session.pop('username', None)
        return None

    hydrated = dict(user)
    hydrated = ensure_user_points_fields(hydrated)
    hydrated['user_key'] = user_key
    hydrated['avatar_url'] = display_avatar_url(hydrated)
    return hydrated


def touch_user_presence(user_key):
    users = load_users()
    user = users.get(user_key)
    if not user:
        return
    user['last_seen_at'] = now_iso()
    users[user_key] = user
    save_users(users)


def get_online_user_keys():
    users = load_users()
    now = datetime.now()
    online = set()
    for key, user in users.items():
        seen_at = parse_iso((user or {}).get('last_seen_at'))
        if seen_at and (now - seen_at) <= timedelta(seconds=ONLINE_WINDOW_SECONDS):
            online.add(key)
    return online


@app.before_request
def update_presence_heartbeat():
    user_key = session.get('username')
    if user_key:
        now_ts = int(datetime.now().timestamp())
        last_ts = int(session.get('presence_last_touch', 0) or 0)
        if now_ts - last_ts >= PRESENCE_TOUCH_INTERVAL_SECONDS:
            touch_user_presence(user_key)
            session['presence_last_touch'] = now_ts


def login_required(view_func):
    @wraps(view_func)
    def wrapped(*args, **kwargs):
        if not current_user():
            flash('Please log in first.', 'error')
            return redirect(url_for('login', next=request.path))
        return view_func(*args, **kwargs)

    return wrapped


@app.context_processor
def inject_auth_user():
    return {'current_user': current_user(), 'online_window_seconds': ONLINE_WINDOW_SECONDS}


# ----------------------------
# Quiz helpers
# ----------------------------
def get_quizzes():
    quizzes = []
    base_dir = 'quizzes'
    if os.path.exists(base_dir):
        for folder in os.listdir(base_dir):
            folder_path = os.path.join(base_dir, folder)
            quiz_file = os.path.join(folder_path, 'quiz.json')
            if os.path.isdir(folder_path) and os.path.exists(quiz_file):
                quiz_data = safe_load_json(quiz_file, None)
                if isinstance(quiz_data, dict):
                    quiz_data['folder'] = folder
                    quizzes.append(quiz_data)
    return sorted(quizzes, key=lambda x: x.get('created_at', ''), reverse=True)


def parse_options_from_form(form, question_index, existing_options=None):
    submitted = form.getlist(f'options_{question_index}[]')

    cleaned = []
    for opt in submitted:
        val = opt.strip()
        if val and val not in cleaned:
            cleaned.append(val)

    if not cleaned and existing_options:
        return existing_options
    return cleaned


def get_answer_from_form(form, question_index, options, existing_answer=None):
    answer_idx_raw = form.get(f'answer{question_index}')

    if answer_idx_raw is not None:
        try:
            idx = int(answer_idx_raw)
            if 0 <= idx < len(options):
                return options[idx]
        except (ValueError, TypeError):
            pass

    if existing_answer and existing_answer in options:
        return existing_answer

    return options[0] if options else ''


# ----------------------------
# Leaderboard helpers
# ----------------------------
def record_quiz_result(user, folder, quiz_title, score, correct_answers, wrong_answers, total_questions):
    points_earned = int(correct_answers) * CLASSIC_CORRECT_POINTS + int(wrong_answers) * CLASSIC_WRONG_POINTS
    results = load_results()
    results.append(
        {
            'username_key': user.get('user_key'),
            'username': user.get('username'),
            'avatar': user.get('avatar', ''),
            'folder': folder,
            'quiz_title': quiz_title,
            'score': round(score, 2),
            'correct': int(correct_answers),
            'wrong': int(wrong_answers),
            'total': int(total_questions),
            'points_earned': points_earned,
            'completed_at': datetime.now().isoformat(),
        }
    )
    save_results(results)

    users = load_users()
    user_key = user.get('user_key')
    target = users.get(user_key)
    if target:
        target = ensure_user_points_fields(target)
        target['classic_points'] = int(target.get('classic_points', 0) or 0) + points_earned
        target['global_points'] = int(target.get('classic_points', 0) or 0) + int(target.get('party_points', 0) or 0)
        users[user_key] = target
        save_users(users)


def get_top_users(limit=10):
    users = load_users()
    leaderboard = []
    changed = False
    for key, user in users.items():
        if not isinstance(user, dict):
            continue
        before = (user.get('classic_points'), user.get('party_points'), user.get('global_points'))
        user = ensure_user_points_fields(user)
        after = (user.get('classic_points'), user.get('party_points'), user.get('global_points'))
        if before != after:
            changed = True
        users[key] = user
        leaderboard.append(
            {
                'username': user.get('username', key),
                'avatar_url': display_avatar_url(user),
                'classic_points': int(user.get('classic_points', 0) or 0),
                'party_points': int(user.get('party_points', 0) or 0),
                'global_points': int(user.get('global_points', 0) or 0),
            }
        )

    if changed:
        save_users(users)

    leaderboard.sort(key=lambda x: (-x['global_points'], -x['party_points'], -x['classic_points'], x['username'].lower()))
    return leaderboard[:limit]


# ----------------------------
# Party helpers
# ----------------------------
def now_iso():
    return datetime.now().isoformat()


def parse_iso(value):
    if not value:
        return None
    try:
        return datetime.fromisoformat(value)
    except (TypeError, ValueError):
        return None


def default_party_settings():
    return {
        'hide_live_rating_between_rounds': False,
        'question_time_seconds': 30,
        'question_mode': 'full',
    }


def default_game_state():
    return {
        'status': 'idle',
        'quiz_folder': None,
        'current_round': 0,
        'total_rounds': 0,
        'question_indices': [],
        'round_phase': 'lobby',
        'round_started_at': None,
        'question_deadline_at': None,
        'review_deadline_at': None,
        'points_awarded': False,
        'answers_by_round': {},
        'scores': {},
    }


def ensure_game_scores_for_members(party):
    scores = party['game_state'].setdefault('scores', {})
    for member in party.get('members', []):
        user_key = member.get('user_key')
        if not user_key:
            continue
        scores.setdefault(
            user_key,
            {
                'username': member.get('username', user_key),
                'points_total': 0,
                'correct_answers': 0,
                'answered_questions': 0,
                'total_response_time': 0.0,
                'average_response_time': 0.0,
            },
        )


def normalize_party(party):
    party = dict(party or {})
    party.setdefault('id', f"party_{int(datetime.now().timestamp() * 1000)}")
    party.setdefault('name', 'Party')
    party.setdefault('description', '')
    party.setdefault('owner', '')
    party.setdefault('owner_key', '')
    party.setdefault('created_at', now_iso())
    party.setdefault('join_code', '')
    party.setdefault('members', [])
    party.setdefault('settings', default_party_settings())
    party.setdefault('game_state', default_game_state())
    party.setdefault('empty_since', None)
    party.setdefault('finished_since', None)
    party.setdefault('owner_offline_since', None)

    if not isinstance(party.get('settings'), dict):
        party['settings'] = default_party_settings()
    party['settings'].setdefault('hide_live_rating_between_rounds', False)
    party['settings'].setdefault('question_time_seconds', 30)
    party['settings'].setdefault('question_mode', 'full')

    if not isinstance(party.get('game_state'), dict):
        party['game_state'] = default_game_state()
    game_state = party['game_state']
    game_state.setdefault('status', 'idle')
    game_state.setdefault('quiz_folder', None)
    game_state.setdefault('current_round', 0)
    game_state.setdefault('total_rounds', 0)
    game_state.setdefault('question_indices', [])
    game_state.setdefault('round_phase', 'lobby')
    game_state.setdefault('round_started_at', None)
    game_state.setdefault('question_deadline_at', None)
    game_state.setdefault('review_deadline_at', None)
    game_state.setdefault('points_awarded', False)
    game_state.setdefault('answers_by_round', {})
    game_state.setdefault('scores', {})

    if not isinstance(party.get('members'), list):
        party['members'] = []

    normalized_members = []
    seen = set()
    for member in party['members']:
        if not isinstance(member, dict):
            continue
        user_key = member.get('user_key')
        username = member.get('username')
        if not user_key and username:
            user_key = normalize_username(username)
        if not user_key:
            continue
        if user_key in seen:
            continue
        seen.add(user_key)
        normalized_members.append(
            {
                'user_key': user_key,
                'username': username or user_key,
                'joined_at': member.get('joined_at') or now_iso(),
            }
        )
    party['members'] = normalized_members

    if party['members']:
        party['empty_since'] = None

    ensure_game_scores_for_members(party)
    return party


def generate_join_code(existing_codes):
    chars = string.ascii_uppercase + string.digits
    while True:
        code = ''.join(random.choice(chars) for _ in range(6))
        if code not in existing_codes:
            return code


def migrate_and_normalize_parties(parties):
    normalized = [normalize_party(row) for row in (parties or [])]
    existing_codes = {p.get('join_code') for p in normalized if p.get('join_code')}
    for party in normalized:
        if not party.get('join_code'):
            party['join_code'] = generate_join_code(existing_codes)
            existing_codes.add(party['join_code'])
    return normalized


def purge_empty_parties(parties, online_user_keys):
    now = datetime.now()
    cleaned = []
    changed = False
    for party in parties:
        party = normalize_party(party)
        game_state = party.get('game_state', {})
        if game_state.get('status') == 'finished':
            finished_since = parse_iso(party.get('finished_since'))
            if finished_since is None:
                party['finished_since'] = now_iso()
                cleaned.append(party)
                changed = True
                continue
            if now - finished_since > timedelta(seconds=FINISHED_PARTY_TTL_SECONDS):
                changed = True
                continue
        else:
            party['finished_since'] = None

        members = party.get('members', [])
        has_online_member = any(m.get('user_key') in online_user_keys for m in members)

        owner_key = party.get('owner_key')
        owner_online = owner_key in online_user_keys if owner_key else False
        if game_state.get('status') != 'in_progress' and owner_key and not owner_online:
            owner_offline_since = parse_iso(party.get('owner_offline_since'))
            if owner_offline_since is None:
                party['owner_offline_since'] = now_iso()
                changed = True
            elif now - owner_offline_since > timedelta(seconds=OWNER_OFFLINE_PARTY_TTL_SECONDS):
                changed = True
                continue
        else:
            party['owner_offline_since'] = None

        if members and has_online_member:
            party['empty_since'] = None
            cleaned.append(party)
            continue
        empty_since = parse_iso(party.get('empty_since'))
        if empty_since is None:
            party['empty_since'] = now_iso()
            cleaned.append(party)
            changed = True
            continue
        if now - empty_since > timedelta(seconds=EMPTY_PARTY_TTL_SECONDS):
            changed = True
            continue
        cleaned.append(party)
    return cleaned, changed


def load_and_prepare_parties():
    raw = safe_load_json(PARTIES_FILE, [])
    normalized = migrate_and_normalize_parties(raw)
    cleaned, changed = purge_empty_parties(normalized, get_online_user_keys())
    if changed or cleaned != raw:
        save_parties(cleaned)
    return cleaned


def find_party_by_id(parties, party_id):
    for party in parties:
        if party.get('id') == party_id:
            return party
    return None


def find_party_by_join_code(parties, join_code):
    code = (join_code or '').strip().upper()
    for party in parties:
        if (party.get('join_code') or '').upper() == code:
            return party
    return None


def is_member(party, user_key):
    return any(m.get('user_key') == user_key for m in party.get('members', []))


def add_member_to_party(party, user):
    user_key = user.get('user_key')
    if is_member(party, user_key):
        return False
    party['members'].append({'user_key': user_key, 'username': user.get('username'), 'joined_at': now_iso()})
    party['empty_since'] = None
    ensure_game_scores_for_members(party)
    return True


def remove_member_from_party(party, user_key):
    before = len(party.get('members', []))
    party['members'] = [m for m in party.get('members', []) if m.get('user_key') != user_key]
    removed = len(party['members']) != before
    if removed and not party['members']:
        party['empty_since'] = now_iso()
    elif removed:
        party['empty_since'] = None
    return removed


def get_party_quiz(party):
    folder = party.get('game_state', {}).get('quiz_folder')
    if not folder:
        return None, None
    quiz_path = os.path.join('quizzes', folder, 'quiz.json')
    if not os.path.exists(quiz_path):
        return None, None
    return folder, safe_load_json(quiz_path, {})


def get_game_question(game_state, quiz_data, round_idx):
    questions = quiz_data.get('questions', [])
    question_indices = game_state.get('question_indices') or list(range(len(questions)))
    if round_idx < 0 or round_idx >= len(question_indices):
        return None, None
    question_idx = question_indices[round_idx]
    if question_idx < 0 or question_idx >= len(questions):
        return None, None
    return questions[question_idx], question_idx


def build_round_result_data(party, quiz_data, round_idx):
    game_state = party.get('game_state', {})
    question, _ = get_game_question(game_state, quiz_data, round_idx)
    if not question:
        return None
    round_answers = game_state.get('answers_by_round', {}).get(str(round_idx), {})
    counts = {opt: 0 for opt in question.get('options', [])}
    for ans in (round_answers or {}).values():
        selected = (ans or {}).get('selected')
        if selected in counts:
            counts[selected] += 1
    return {
        'question': question.get('question', ''),
        'correct_answer': question.get('answer', ''),
        'total_answers': len(round_answers or {}),
        'counts': counts,
    }


def build_points_progress_timeline(party, upto_round=None):
    game_state = party.get('game_state', {})
    answers_by_round = game_state.get('answers_by_round', {}) or {}
    members = party.get('members', []) or []
    member_keys = [m.get('user_key') for m in members if m.get('user_key')]
    member_names = {m.get('user_key'): m.get('username', m.get('user_key')) for m in members if m.get('user_key')}

    cumulative = {key: 0 for key in member_keys}
    timeline = []
    max_points = 0
    total_rounds = int(game_state.get('total_rounds', 0))
    if upto_round is not None:
        total_rounds = max(0, min(total_rounds, int(upto_round)))

    for round_idx in range(total_rounds):
        round_answers = answers_by_round.get(str(round_idx), {}) or {}
        for key in member_keys:
            answer = round_answers.get(key, {}) or {}
            cumulative[key] += int(answer.get('points', 0) or 0)
            if cumulative[key] > max_points:
                max_points = cumulative[key]
        round_rows = []
        for key in member_keys:
            round_rows.append(
                {
                    'user_key': key,
                    'username': member_names.get(key, key),
                    'points_total': cumulative.get(key, 0),
                }
            )
        round_rows.sort(key=lambda r: (-r['points_total'], r['username'].lower()))
        timeline.append({'round_number': round_idx + 1, 'rows': round_rows})

    return {'timeline': timeline, 'max_points': max_points}


def award_party_points_if_needed(party):
    game_state = party.get('game_state', {})
    if game_state.get('points_awarded'):
        return False
    if game_state.get('status') != 'finished':
        return False

    users = load_users()
    scores = game_state.get('scores', {}) or {}
    changed = False
    for user_key, score in scores.items():
        target = users.get(user_key)
        if not target:
            continue
        gained = int((score or {}).get('points_total', 0) or 0)
        target = ensure_user_points_fields(target)
        target['party_points'] = int(target.get('party_points', 0) or 0) + gained
        target['global_points'] = int(target.get('classic_points', 0) or 0) + int(target.get('party_points', 0) or 0)
        users[user_key] = target
        changed = True

    game_state['points_awarded'] = True
    if changed:
        save_users(users)
    return True


def fill_unanswered_for_round(party, quiz_data, round_idx):
    game_state = party.get('game_state', {})
    question, _ = get_game_question(game_state, quiz_data, round_idx)
    if not question:
        return
    round_answers = get_round_answers(game_state, round_idx)
    limit = int(party.get('settings', {}).get('question_time_seconds', 30) or 30)
    for member in party.get('members', []):
        user_key = member.get('user_key')
        if not user_key or user_key in round_answers:
            continue
        round_answers[user_key] = {
            'username': member.get('username', user_key),
            'selected': '',
            'correct_answer': question.get('answer'),
            'is_correct': False,
            'points': PARTY_SKIP_POINTS,
            'response_time': float(limit),
            'answered_at': now_iso(),
            'skipped': True,
        }


def get_round_answers(game_state, round_idx):
    answers_by_round = game_state.setdefault('answers_by_round', {})
    key = str(round_idx)
    answers_by_round.setdefault(key, {})
    return answers_by_round[key]


def recompute_party_scores(party):
    game_state = party['game_state']
    scores = {}
    members_by_key = {m.get('user_key'): m for m in party.get('members', [])}
    for round_answers in game_state.get('answers_by_round', {}).values():
        if not isinstance(round_answers, dict):
            continue
        for user_key, answer in round_answers.items():
            if not isinstance(answer, dict):
                continue
            username = answer.get('username') or members_by_key.get(user_key, {}).get('username') or user_key
            row = scores.setdefault(
                user_key,
                {
                    'username': username,
                    'points_total': 0,
                    'correct_answers': 0,
                    'answered_questions': 0,
                    'total_response_time': 0.0,
                    'average_response_time': 0.0,
                },
            )
            row['points_total'] += int(answer.get('points', 0) or 0)
            row['answered_questions'] += 1
            if answer.get('is_correct'):
                row['correct_answers'] += 1
            row['total_response_time'] += float(answer.get('response_time', 0.0) or 0.0)

    for member_key, member in members_by_key.items():
        scores.setdefault(
            member_key,
            {
                'username': member.get('username', member_key),
                'points_total': 0,
                'correct_answers': 0,
                'answered_questions': 0,
                'total_response_time': 0.0,
                'average_response_time': 0.0,
            },
        )

    for row in scores.values():
        answered = row.get('answered_questions', 0)
        row['points_total'] = int(row.get('points_total', 0) or 0)
        row['average_response_time'] = round((row['total_response_time'] / answered), 3) if answered else None
        row['total_response_time'] = round(row['total_response_time'], 3)

    game_state['scores'] = scores


def party_leaderboard(game_state):
    scores = game_state.get('scores', {})
    rows = []
    for user_key, score in scores.items():
        avg_time = score.get('average_response_time')
        rows.append(
            {
                'user_key': user_key,
                'username': score.get('username', user_key),
                'points_total': int(score.get('points_total', 0) or 0),
                'correct_answers': int(score.get('correct_answers', 0)),
                'answered_questions': int(score.get('answered_questions', 0)),
                'average_response_time': float(avg_time) if avg_time is not None else None,
            }
        )
    rows.sort(
        key=lambda x: (
            -x['points_total'],
            -x['correct_answers'],
            x['average_response_time'] if x['average_response_time'] is not None else 10**9,
            x['username'].lower(),
        )
    )
    return rows


def can_show_party_leaderboard(party):
    game_state = party.get('game_state', {})
    return game_state.get('status') == 'finished'


def sync_party_game_state(party):
    game_state = party.get('game_state', {})
    if game_state.get('status') != 'in_progress':
        return False
    now = datetime.now()
    phase = game_state.get('round_phase')

    if phase == 'question':
        deadline = parse_iso(game_state.get('question_deadline_at'))
        current_round = int(game_state.get('current_round', 0))
        _, quiz_data = get_party_quiz(party)
        if not quiz_data:
            return False

        round_answers = game_state.get('answers_by_round', {}).get(str(current_round), {})
        member_count = len(party.get('members', []))
        everyone_answered = member_count > 0 and len(round_answers or {}) >= member_count
        deadline_passed = bool(deadline and now >= deadline)
        if not everyone_answered and not deadline_passed:
            return False

        if deadline_passed and not everyone_answered:
            fill_unanswered_for_round(party, quiz_data, current_round)
        game_state['round_phase'] = 'review'
        game_state['review_deadline_at'] = (now + timedelta(seconds=4)).isoformat()
        recompute_party_scores(party)
        return True

    if phase == 'review':
        review_deadline = parse_iso(game_state.get('review_deadline_at'))
        if not review_deadline or now < review_deadline:
            return False
        current_round = int(game_state.get('current_round', 0))
        total_rounds = int(game_state.get('total_rounds', 0))
        if current_round + 1 >= total_rounds:
            game_state['status'] = 'finished'
            game_state['round_phase'] = 'finished'
            game_state['question_deadline_at'] = None
            game_state['review_deadline_at'] = None
            party['finished_since'] = now_iso()
            recompute_party_scores(party)
            award_party_points_if_needed(party)
            return True

        game_state['current_round'] = current_round + 1
        game_state['round_phase'] = 'question'
        game_state['round_started_at'] = now_iso()
        limit = int(party.get('settings', {}).get('question_time_seconds', 30) or 30)
        game_state['question_deadline_at'] = (now + timedelta(seconds=limit)).isoformat()
        game_state['review_deadline_at'] = None
        recompute_party_scores(party)
        return True

    return False


def build_party_online_counts(parties, online_keys):
    counts = {}
    for party in parties:
        party_id = party.get('id')
        if not party_id:
            continue
        count = sum(1 for member in party.get('members', []) if member.get('user_key') in online_keys)
        counts[party_id] = count
    return counts


def build_party_member_rows(party, online_keys):
    users = load_users()
    scores = (party.get('game_state') or {}).get('scores', {})
    rows = []
    for member in party.get('members', []):
        key = member.get('user_key')
        profile = users.get(key, {})
        avatar_url = display_avatar_url(
            {
                'username': member.get('username', key),
                'avatar': profile.get('avatar', ''),
            }
        )
        score = scores.get(key, {})
        rows.append(
            {
                'user_key': key,
                'username': member.get('username', key),
                'avatar_url': avatar_url,
                'is_online': key in online_keys,
                'points_total': int(score.get('points_total', 0) or 0),
                'correct_answers': int(score.get('correct_answers', 0) or 0),
            }
        )
    rows.sort(key=lambda x: (not x['is_online'], x['username'].lower()))
    return rows


# ----------------------------
# Auth routes
# ----------------------------
@app.route('/register', methods=['GET', 'POST'])
def register():
    if request.method == 'POST':
        username_raw = request.form.get('username', '').strip()
        password = request.form.get('password', '')
        confirm_password = request.form.get('confirm_password', '')
        description = request.form.get('description', '').strip()
        avatar = request.form.get('avatar', '').strip()

        username_key = normalize_username(username_raw)
        if not re.fullmatch(r'[A-Za-z0-9_]{3,30}', username_raw):
            flash('Username must be 3-30 characters and use only letters, numbers, or _.', 'error')
            return redirect(url_for('register'))

        if len(password) < 6:
            flash('Password must be at least 6 characters.', 'error')
            return redirect(url_for('register'))

        if password != confirm_password:
            flash('Passwords do not match.', 'error')
            return redirect(url_for('register'))

        users = load_users()
        if username_key in users:
            flash('That username is already taken.', 'error')
            return redirect(url_for('register'))

        users[username_key] = ensure_user_points_fields(
            {
            'username': username_raw,
            'password_hash': generate_password_hash(password),
            'description': description,
            'avatar': avatar,
            'created_at': datetime.now().isoformat(),
            }
        )
        save_users(users)

        session['username'] = username_key
        flash(f'Welcome, {username_raw}!', 'success')
        return redirect(url_for('home'))

    return render_template('register.html')


@app.route('/login', methods=['GET', 'POST'])
def login():
    if request.method == 'POST':
        username_raw = request.form.get('username', '').strip()
        password = request.form.get('password', '')
        username_key = normalize_username(username_raw)

        users = load_users()
        user = users.get(username_key)

        if not user or not check_password_hash(user.get('password_hash', ''), password):
            flash('Invalid username or password.', 'error')
            return redirect(url_for('login'))

        session['username'] = username_key
        flash(f'Logged in as {user.get("username", username_raw)}.', 'success')

        next_url = request.args.get('next')
        if next_url:
            return redirect(next_url)
        return redirect(url_for('home'))

    return render_template('login.html')


@app.route('/logout', methods=['POST'])
def logout():
    session.clear()
    flash('You have been logged out.', 'success')
    return redirect(url_for('home'))


@app.route('/profile', methods=['GET', 'POST'])
@login_required
def profile():
    user_key = session.get('username')
    users = load_users()
    user = users.get(user_key)

    if request.method == 'POST':
        user['description'] = request.form.get('description', '').strip()
        avatar_url = request.form.get('avatar', '').strip()
        avatar_file = request.files.get('avatar_file')
        uploaded = save_avatar_upload(avatar_file, user_key)

        if uploaded:
            old_avatar = (user.get('avatar') or '').strip()
            if is_local_avatar_path(old_avatar):
                old_filename = old_avatar.rsplit('/', 1)[-1]
                old_path = os.path.join(AVATAR_UPLOAD_DIR, old_filename)
                if os.path.exists(old_path):
                    try:
                        os.remove(old_path)
                    except Exception:
                        pass
            user['avatar'] = f"/uploads/avatars/{uploaded}"
        else:
            user['avatar'] = avatar_url

        users[user_key] = user
        save_users(users)

        flash('Profile updated successfully.', 'success')
        return redirect(url_for('profile'))

    return render_template('profile.html', user_profile={**user, 'avatar_url': display_avatar_url(user)})


# ----------------------------
# Party routes
# ----------------------------
@app.route('/parties', methods=['GET'])
@login_required
def parties():
    user = current_user()
    all_parties = sorted(load_and_prepare_parties(), key=lambda x: x.get('created_at', ''), reverse=True)
    online_keys = get_online_user_keys()
    online_counts = build_party_online_counts(all_parties, online_keys)
    return render_template(
        'parties.html',
        parties=all_parties,
        user_key=user.get('user_key'),
        online_user_keys=online_keys,
        party_online_counts=online_counts,
    )


@app.route('/parties/create', methods=['POST'])
@login_required
def create_party():
    user = current_user()
    name = request.form.get('name', '').strip()
    description = request.form.get('description', '').strip()
    if not name:
        flash('Party name is required.', 'error')
        return redirect(url_for('parties'))

    all_parties = load_and_prepare_parties()
    existing_codes = {p.get('join_code') for p in all_parties if p.get('join_code')}
    new_party = normalize_party(
        {
            'id': f"party_{int(datetime.now().timestamp() * 1000)}",
            'name': name,
            'description': description,
            'owner': user.get('username'),
            'owner_key': user.get('user_key'),
            'created_at': now_iso(),
            'join_code': generate_join_code(existing_codes),
            'members': [
                {
                    'user_key': user.get('user_key'),
                    'username': user.get('username'),
                    'joined_at': now_iso(),
                }
            ],
            'settings': default_party_settings(),
            'game_state': default_game_state(),
            'empty_since': None,
        }
    )
    all_parties.append(new_party)
    save_parties(all_parties)
    flash(f"Party created. Join code: {new_party.get('join_code')}", 'success')
    return redirect(url_for('party_view', party_id=new_party.get('id')))


@app.route('/parties/join', methods=['POST'])
@login_required
def join_party_by_code():
    user = current_user()
    join_code = request.form.get('join_code', '').strip().upper()
    if not join_code:
        flash('Join code is required.', 'error')
        return redirect(url_for('parties'))

    all_parties = load_and_prepare_parties()
    party = find_party_by_join_code(all_parties, join_code)
    if not party:
        flash('Party not found for that join code.', 'error')
        return redirect(url_for('parties'))

    added = add_member_to_party(party, user)
    save_parties(all_parties)
    if added:
        flash(f'Joined {party.get("name")}.', 'success')
    else:
        flash(f'Already in {party.get("name")}.', 'success')
    return redirect(url_for('party_view', party_id=party.get('id')))


@app.route('/parties/<party_id>/join', methods=['POST'])
@login_required
def join_party_by_button(party_id):
    user = current_user()
    all_parties = load_and_prepare_parties()
    party = find_party_by_id(all_parties, party_id)
    if not party:
        flash('Party not found.', 'error')
        return redirect(url_for('parties'))

    added = add_member_to_party(party, user)
    save_parties(all_parties)
    if added:
        flash(f'Joined {party.get("name")}.', 'success')
    else:
        flash(f'Already in {party.get("name")}.', 'success')
    return redirect(url_for('party_view', party_id=party_id))


@app.route('/parties/<party_id>/leave', methods=['POST'])
@login_required
def leave_party(party_id):
    user = current_user()
    all_parties = load_and_prepare_parties()
    party = find_party_by_id(all_parties, party_id)
    if not party:
        flash('Party not found.', 'error')
        return redirect(url_for('parties'))

    if not remove_member_from_party(party, user.get('user_key')):
        flash('You are not a member of this party.', 'error')
        return redirect(url_for('party_view', party_id=party_id))

    if party.get('owner_key') == user.get('user_key') and party.get('members'):
        new_owner = party['members'][0]
        party['owner_key'] = new_owner.get('user_key')
        party['owner'] = new_owner.get('username')
        flash(f'You left. New owner is {party.get("owner")}.', 'success')
    else:
        flash('You left the party.', 'success')

    save_parties(all_parties)
    return redirect(url_for('parties'))


@app.route('/parties/<party_id>', methods=['GET'])
@login_required
def party_view(party_id):
    user = current_user()
    all_parties = load_and_prepare_parties()
    party = find_party_by_id(all_parties, party_id)
    if not party:
        flash('Party not found or expired.', 'error')
        return redirect(url_for('parties'))

    user_is_member = is_member(party, user.get('user_key'))
    if not user_is_member:
        flash('Join this party first to view room details.', 'error')
        return redirect(url_for('parties'))

    state_changed = sync_party_game_state(party)
    if state_changed:
        save_parties(all_parties)

    online_keys = get_online_user_keys()
    online_member_count = sum(1 for member in party.get('members', []) if member.get('user_key') in online_keys)
    leaderboard_rows = party_leaderboard(party.get('game_state', {}))
    show_leaderboard = can_show_party_leaderboard(party)
    member_rows = build_party_member_rows(party, online_keys)
    game_state = party.get('game_state', {})
    quiz_data = None
    if game_state.get('quiz_folder'):
        _, quiz_data = get_party_quiz(party)

    return render_template(
        'party_lobby.html',
        party=party,
        user_key=user.get('user_key'),
        user_is_member=user_is_member,
        online_user_keys=online_keys,
        online_member_count=online_member_count,
        member_rows=member_rows,
        quizzes=get_quizzes(),
        quiz_data=quiz_data,
        game_state=game_state,
        leaderboard_rows=leaderboard_rows,
        show_leaderboard=show_leaderboard,
    )


@app.route('/parties/<party_id>/game', methods=['GET'])
@login_required
def party_game_view(party_id):
    user = current_user()
    all_parties = load_and_prepare_parties()
    party = find_party_by_id(all_parties, party_id)
    if not party:
        flash('Party not found or expired.', 'error')
        return redirect(url_for('parties'))

    user_is_member = is_member(party, user.get('user_key'))
    if not user_is_member:
        flash('Join this party first to play.', 'error')
        return redirect(url_for('parties'))

    state_changed = sync_party_game_state(party)
    if state_changed:
        save_parties(all_parties)

    online_keys = get_online_user_keys()
    game_state = party.get('game_state', {})
    quiz_data = None
    question = None
    round_answers = {}
    round_result = None
    timer_remaining = None
    current_user_answer = None
    progress_timeline = {'timeline': [], 'max_points': 0}
    progress_round_cap = 0

    if game_state.get('status') in {'in_progress', 'finished'} and game_state.get('quiz_folder'):
        _, quiz_data = get_party_quiz(party)
        current_round = int(game_state.get('current_round', 0))
        if quiz_data:
            question, _ = get_game_question(game_state, quiz_data, current_round)
            round_answers = game_state.get('answers_by_round', {}).get(str(current_round), {})
            round_result = build_round_result_data(party, quiz_data, current_round)

            if game_state.get('round_phase') == 'question':
                deadline = parse_iso(game_state.get('question_deadline_at'))
                if deadline:
                    timer_remaining = max(0, int((deadline - datetime.now()).total_seconds()))
            elif game_state.get('round_phase') == 'review':
                review_deadline = parse_iso(game_state.get('review_deadline_at'))
                if review_deadline:
                    timer_remaining = max(0, int((review_deadline - datetime.now()).total_seconds()))

            current_user_answer = (round_answers or {}).get(user.get('user_key'))
            if game_state.get('status') == 'in_progress':
                progress_round_cap = int(game_state.get('current_round', 0)) + 1

    if game_state.get('status') == 'finished':
        progress_timeline = build_points_progress_timeline(party, upto_round=game_state.get('total_rounds', 0))
    elif game_state.get('status') == 'in_progress':
        progress_timeline = build_points_progress_timeline(party, upto_round=progress_round_cap)

    return render_template(
        'party_game.html',
        party=party,
        game_state=game_state,
        user_key=user.get('user_key'),
        user_is_member=user_is_member,
        online_user_keys=online_keys,
        leaderboard_rows=party_leaderboard(game_state),
        show_leaderboard=can_show_party_leaderboard(party),
        quiz_data=quiz_data,
        question=question,
        round_answers=round_answers,
        round_result=round_result,
        timer_remaining=timer_remaining,
        current_user_answer=current_user_answer,
        progress_timeline=progress_timeline,
        progress_round_cap=progress_round_cap,
        now_iso=now_iso(),
    )


@app.route('/parties/<party_id>/state', methods=['GET'])
@login_required
def party_game_state(party_id):
    user = current_user()
    all_parties = load_and_prepare_parties()
    party = find_party_by_id(all_parties, party_id)
    if not party:
        return jsonify({'error': 'party_not_found'}), 404
    if not is_member(party, user.get('user_key')):
        return jsonify({'error': 'not_member'}), 403

    changed = sync_party_game_state(party)
    if changed:
        save_parties(all_parties)

    game_state = party.get('game_state', {})
    round_idx = int(game_state.get('current_round', 0))
    answered = len(game_state.get('answers_by_round', {}).get(str(round_idx), {}) or {})
    member_count = len(party.get('members', []))

    return jsonify(
        {
            'status': game_state.get('status'),
            'round_phase': game_state.get('round_phase'),
            'current_round': round_idx,
            'total_rounds': int(game_state.get('total_rounds', 0)),
            'answered_count': answered,
            'member_count': member_count,
            'question_deadline_at': game_state.get('question_deadline_at'),
            'review_deadline_at': game_state.get('review_deadline_at'),
        }
    )


@app.route('/parties/<party_id>/settings', methods=['POST'])
@login_required
def update_party_settings(party_id):
    user = current_user()
    all_parties = load_and_prepare_parties()
    party = find_party_by_id(all_parties, party_id)
    if not party:
        flash('Party not found.', 'error')
        return redirect(url_for('parties'))
    if party.get('owner_key') != user.get('user_key'):
        flash('Only the owner can update settings.', 'error')
        return redirect(url_for('party_view', party_id=party_id))

    question_mode = request.form.get('question_mode', 'full').strip().lower()
    if question_mode not in {'full', 'single'}:
        question_mode = 'full'
    try:
        question_time_seconds = int(request.form.get('question_time_seconds', 30))
    except (TypeError, ValueError):
        question_time_seconds = 30
    question_time_seconds = max(5, min(300, question_time_seconds))

    party['settings']['question_mode'] = question_mode
    party['settings']['question_time_seconds'] = question_time_seconds
    save_parties(all_parties)
    if request.headers.get('X-Requested-With') == 'XMLHttpRequest':
        return ('', 204)
    flash('Party settings updated.', 'success')
    return redirect(url_for('party_view', party_id=party_id))


@app.route('/parties/<party_id>/start', methods=['POST'])
@login_required
def start_party_game(party_id):
    user = current_user()
    all_parties = load_and_prepare_parties()
    party = find_party_by_id(all_parties, party_id)
    if not party:
        flash('Party not found.', 'error')
        return redirect(url_for('parties'))
    if party.get('owner_key') != user.get('user_key'):
        flash('Only the owner can start the party game.', 'error')
        return redirect(url_for('party_view', party_id=party_id))

    quiz_folder = request.form.get('quiz_folder', '').strip()
    quiz_path = os.path.join('quizzes', quiz_folder, 'quiz.json')
    if not quiz_folder or not os.path.exists(quiz_path):
        flash('Please select a valid quiz.', 'error')
        return redirect(url_for('party_view', party_id=party_id))

    quiz_data = safe_load_json(quiz_path, {})
    questions = quiz_data.get('questions', [])
    total_available = len(questions)
    total_rounds = total_available
    if total_rounds == 0:
        flash('Selected quiz has no questions.', 'error')
        return redirect(url_for('party_view', party_id=party_id))

    question_mode = party.get('settings', {}).get('question_mode', 'full')
    if question_mode == 'single':
        chosen_idx = random.randint(0, total_available - 1)
        question_indices = [chosen_idx]
        total_rounds = 1
    else:
        question_indices = list(range(total_available))

    party['game_state'] = {
        'status': 'in_progress',
        'quiz_folder': quiz_folder,
        'current_round': 0,
        'total_rounds': total_rounds,
        'question_indices': question_indices,
        'round_phase': 'question',
        'round_started_at': now_iso(),
        'question_deadline_at': (datetime.now() + timedelta(seconds=int(party.get('settings', {}).get('question_time_seconds', 30) or 30))).isoformat(),
        'review_deadline_at': None,
        'points_awarded': False,
        'answers_by_round': {},
        'scores': {},
    }
    party['finished_since'] = None
    ensure_game_scores_for_members(party)
    save_parties(all_parties)
    flash('Party game started. Round 1 is live.', 'success')
    return redirect(url_for('party_game_view', party_id=party_id))


@app.route('/parties/<party_id>/answer', methods=['POST'])
@login_required
def submit_party_answer(party_id):
    user = current_user()
    all_parties = load_and_prepare_parties()
    party = find_party_by_id(all_parties, party_id)
    if not party:
        flash('Party not found.', 'error')
        return redirect(url_for('parties'))
    if not is_member(party, user.get('user_key')):
        flash('Join the party first to answer.', 'error')
        return redirect(url_for('party_game_view', party_id=party_id))

    game_state = party.get('game_state', {})
    state_changed = sync_party_game_state(party)
    if state_changed:
        save_parties(all_parties)
    if game_state.get('status') != 'in_progress' or game_state.get('round_phase') != 'question':
        flash('Game is not in answering state.', 'error')
        return redirect(url_for('party_game_view', party_id=party_id))

    quiz_folder, quiz_data = get_party_quiz(party)
    if not quiz_data:
        flash('Party quiz could not be loaded.', 'error')
        return redirect(url_for('party_game_view', party_id=party_id))

    current_round = int(game_state.get('current_round', 0))
    question, _ = get_game_question(game_state, quiz_data, current_round)
    if question is None:
        flash('No active round.', 'error')
        return redirect(url_for('party_game_view', party_id=party_id))
    selected_answer = request.form.get('answer', '').strip()
    if selected_answer not in question.get('options', []):
        flash('Please choose a valid answer.', 'error')
        return redirect(url_for('party_game_view', party_id=party_id))

    round_started_at = parse_iso(game_state.get('round_started_at'))
    elapsed = max(0.0, (datetime.now() - round_started_at).total_seconds()) if round_started_at else 0.0
    limit = int(party.get('settings', {}).get('question_time_seconds', 30) or 30)
    if elapsed > limit:
        flash('Time is over for this question.', 'error')
        return redirect(url_for('party_game_view', party_id=party_id))

    round_answers = get_round_answers(game_state, current_round)
    if user.get('user_key') in round_answers:
        flash('You already answered this question.', 'error')
        return redirect(url_for('party_game_view', party_id=party_id))

    is_correct = selected_answer == question.get('answer')
    points = PARTY_WRONG_POINTS
    if is_correct:
        speed_factor = max(0.0, (limit - elapsed) / max(limit, 1))
        points = PARTY_BASE_CORRECT_POINTS + int(PARTY_SPEED_BONUS_MAX * speed_factor)

    round_answers[user.get('user_key')] = {
        'username': user.get('username'),
        'selected': selected_answer,
        'correct_answer': question.get('answer'),
        'is_correct': is_correct,
        'points': points,
        'response_time': round(elapsed, 3),
        'answered_at': now_iso(),
    }

    recompute_party_scores(party)
    save_parties(all_parties)
    flash('Answer submitted.', 'success')
    return redirect(url_for('party_game_view', party_id=party_id))


@app.route('/parties/<party_id>/round/next', methods=['POST'])
@login_required
def next_party_round(party_id):
    user = current_user()
    all_parties = load_and_prepare_parties()
    party = find_party_by_id(all_parties, party_id)
    if not party:
        flash('Party not found.', 'error')
        return redirect(url_for('parties'))
    if party.get('owner_key') != user.get('user_key'):
        flash('Only the owner can advance rounds.', 'error')
        return redirect(url_for('party_game_view', party_id=party_id))

    game_state = party.get('game_state', {})
    state_changed = sync_party_game_state(party)
    if state_changed:
        save_parties(all_parties)
    if game_state.get('status') != 'in_progress':
        flash('Game is not active.', 'error')
        return redirect(url_for('party_game_view', party_id=party_id))

    current_round = int(game_state.get('current_round', 0))
    total_rounds = int(game_state.get('total_rounds', 0))
    current_phase = game_state.get('round_phase', 'question')
    if current_phase == 'question':
        game_state['round_phase'] = 'review'
        recompute_party_scores(party)
        save_parties(all_parties)
        flash(f'Round {current_round + 1} answers revealed.', 'success')
        return redirect(url_for('party_game_view', party_id=party_id))

    if current_round + 1 >= total_rounds:
        game_state['status'] = 'finished'
        game_state['round_phase'] = 'finished'
        game_state['question_deadline_at'] = None
        game_state['review_deadline_at'] = None
        party['finished_since'] = now_iso()
        recompute_party_scores(party)
        award_party_points_if_needed(party)
        save_parties(all_parties)
        flash('Game finished. Final ranking is available.', 'success')
        return redirect(url_for('party_game_view', party_id=party_id))

    game_state['current_round'] = current_round + 1
    game_state['round_phase'] = 'question'
    game_state['round_started_at'] = now_iso()
    limit = int(party.get('settings', {}).get('question_time_seconds', 30) or 30)
    game_state['question_deadline_at'] = (datetime.now() + timedelta(seconds=limit)).isoformat()
    game_state['review_deadline_at'] = None
    recompute_party_scores(party)
    save_parties(all_parties)
    flash(f'Advanced to round {game_state["current_round"] + 1}.', 'success')
    return redirect(url_for('party_game_view', party_id=party_id))


# ----------------------------
# Existing quiz routes
# ----------------------------
@app.route('/')
def home():
    quizzes = get_quizzes()
    parties_preview = sorted(load_and_prepare_parties(), key=lambda x: x.get('created_at', ''), reverse=True)[:4]
    leaderboard = get_top_users(limit=8)
    return render_template('home.html', quizzes=quizzes, top_users=leaderboard, parties_preview=parties_preview)


@app.route('/add_quiz', methods=['GET', 'POST'])
def add_quiz():
    if request.method == 'POST':
        title = request.form.get('title', '').strip()
        description = request.form.get('description', '').strip()

        if not title:
            flash('Title is required!', 'error')
            return redirect(url_for('add_quiz'))

        # Generate unique folder
        folder_name = secure_filename(title).lower().replace(' ', '_') or 'quiz'
        base_folder = folder_name
        counter = 1
        while os.path.exists(os.path.join('quizzes', folder_name)):
            folder_name = f'{base_folder}_{counter}'
            counter += 1

        folder_path = os.path.join('quizzes', folder_name)
        assets_path = os.path.join(folder_path, 'assets')
        ensure_dir(assets_path)

        questions = []
        try:
            question_count = int(request.form.get('question_count', 0))
        except (ValueError, TypeError):
            question_count = 0

        for i in range(1, question_count + 1):
            q_text = request.form.get(f'question{i}', '').strip()
            options = parse_options_from_form(request.form, i)

            if not q_text and not options:
                continue

            answer = get_answer_from_form(request.form, i, options)

            image_path = None
            image_url = request.form.get(f'image_url{i}', '').strip()
            file = request.files.get(f'image{i}')
            if file and file.filename and allowed_file(file.filename):
                ext = file.filename.rsplit('.', 1)[1].lower()
                filename = f'q{i}_{int(datetime.now().timestamp())}.{ext}'
                file.save(os.path.join(assets_path, filename))
                image_path = filename

            questions.append(
                {
                    'id': len(questions) + 1,
                    'question': q_text,
                    'options': options,
                    'answer': answer,
                    'image': image_path,
                    'image_url': image_url if image_url else None,
                }
            )

        quiz_data = {
            'title': title,
            'description': description,
            'questions': questions,
            'created_at': datetime.now().isoformat(),
        }

        safe_dump_json(os.path.join(folder_path, 'quiz.json'), quiz_data)
        flash('Quiz created successfully!', 'success')
        return redirect(url_for('admin'))

    return render_template('add_quiz.html')


@app.route('/admin')
def admin():
    quizzes = get_quizzes()
    return render_template('admin.html', quizzes=quizzes)


@app.route('/edit_quiz/<folder>', methods=['GET', 'POST'])
def edit_quiz(folder):
    quiz_path = os.path.join('quizzes', folder, 'quiz.json')
    if not os.path.exists(quiz_path):
        flash('Quiz not found!', 'error')
        return redirect(url_for('admin'))

    current_quiz = safe_load_json(quiz_path, {})

    if request.method == 'POST':
        # 1. Metadata with fallback
        title = request.form.get('title', current_quiz.get('title')).strip()
        description = request.form.get('description', current_quiz.get('description')).strip()

        if not title:
            flash('Title is required!', 'error')
            return redirect(url_for('edit_quiz', folder=folder))

        # 2. Process Questions
        new_questions = []
        old_questions = current_quiz.get('questions', [])

        try:
            question_count = int(request.form.get('question_count', 0))
        except (ValueError, TypeError):
            question_count = len(old_questions)

        for i in range(1, question_count + 1):
            old_q = old_questions[i - 1] if (i - 1) < len(old_questions) else {}

            q_text = request.form.get(f'question{i}', '').strip()
            if not q_text and old_q:
                q_text = old_q.get('question', '')

            options = parse_options_from_form(request.form, i, existing_options=old_q.get('options'))

            if not q_text and not options:
                continue

            answer = get_answer_from_form(request.form, i, options, existing_answer=old_q.get('answer'))

            image_filename = old_q.get('image')
            image_url = request.form.get(f'image_url{i}', '').strip()

            if not image_url and old_q.get('image_url'):
                image_url = old_q.get('image_url')

            image_file = request.files.get(f'image{i}')

            if image_file and image_file.filename and allowed_file(image_file.filename):
                assets_path = os.path.join('quizzes', folder, 'assets')
                ensure_dir(assets_path)
                ext = image_file.filename.rsplit('.', 1)[1].lower()
                new_filename = f'q{i}_{int(datetime.now().timestamp())}.{ext}'
                image_file.save(os.path.join(assets_path, new_filename))

                if image_filename:
                    old_img_path = os.path.join(assets_path, image_filename)
                    if os.path.exists(old_img_path):
                        try:
                            os.remove(old_img_path)
                        except Exception:
                            pass
                image_filename = new_filename

            new_questions.append(
                {
                    'id': len(new_questions) + 1,
                    'question': q_text,
                    'options': options,
                    'answer': answer,
                    'image': image_filename,
                    'image_url': image_url if image_url else None,
                }
            )

        # 3. Finalize and Save
        updated_quiz = {
            'title': title,
            'description': description,
            'questions': new_questions,
            'created_at': current_quiz.get('created_at', datetime.now().isoformat()),
        }

        # 4. Folder Rename Logic
        new_folder = secure_filename(title).lower().replace(' ', '_') or 'quiz'
        if new_folder != folder:
            target_path = os.path.join('quizzes', new_folder)
            counter = 1
            original_new = new_folder
            while os.path.exists(target_path) and target_path != os.path.join('quizzes', folder):
                new_folder = f'{original_new}_{counter}'
                target_path = os.path.join('quizzes', new_folder)
                counter += 1

            if new_folder != folder:
                old_path = os.path.join('quizzes', folder)
                ensure_dir(target_path)

                old_assets = os.path.join(old_path, 'assets')
                new_assets = os.path.join(target_path, 'assets')
                if os.path.exists(old_assets):
                    if os.path.exists(new_assets):
                        shutil.rmtree(new_assets)
                    shutil.move(old_assets, new_assets)

                safe_dump_json(os.path.join(target_path, 'quiz.json'), updated_quiz)
                shutil.rmtree(old_path)
                folder = new_folder
            else:
                safe_dump_json(os.path.join('quizzes', folder, 'quiz.json'), updated_quiz)
        else:
            safe_dump_json(os.path.join('quizzes', folder, 'quiz.json'), updated_quiz)

        flash('Quiz updated successfully!', 'success')
        return redirect(url_for('admin'))

    return render_template('edit_quiz.html', quiz=current_quiz, folder=folder)


@app.route('/delete_quiz/<folder>', methods=['POST'])
def delete_quiz(folder):
    folder_path = os.path.join('quizzes', folder)
    if os.path.exists(folder_path):
        shutil.rmtree(folder_path)
        flash('Quiz deleted successfully!', 'success')
    return redirect(url_for('admin'))


@app.route('/delete_photo/<folder>/<filename>', methods=['POST'])
def delete_photo(folder, filename):
    asset_path = os.path.join('quizzes', folder, 'assets', filename)
    if os.path.exists(asset_path):
        try:
            os.remove(asset_path)
            quiz_path = os.path.join('quizzes', folder, 'quiz.json')
            if os.path.exists(quiz_path):
                quiz_data = safe_load_json(quiz_path, {})
                for question in quiz_data.get('questions', []):
                    if question.get('image') == filename:
                        question['image'] = None
                        break
                safe_dump_json(quiz_path, quiz_data)
        except Exception as e:
            flash(f'Error deleting photo: {e}', 'error')
    return redirect(url_for('edit_quiz', folder=folder))


@app.route('/quiz/<folder>', methods=['GET', 'POST'])
@login_required
def quiz(folder):
    quiz_path = os.path.join('quizzes', folder, 'quiz.json')
    if not os.path.exists(quiz_path):
        return 'Quiz not found', 404

    quiz_data = safe_load_json(quiz_path, {})
    total_questions = len(quiz_data.get('questions', []))

    session_key = f'quiz_{folder}'
    result_saved_key = f'result_saved_{folder}'
    if session_key not in session:
        session[session_key] = {'answers': [], 'current_question': 1}
        session.pop(result_saved_key, None)

    quiz_session = session[session_key]

    if request.method == 'POST':
        question_id = int(request.form['question_id'])
        selected_answer = request.form['answer'].strip()

        question = next((q for q in quiz_data.get('questions', []) if q.get('id') == question_id), None)
        if question is None:
            return 'Question not found', 404

        correct_answer = question['answer'].strip()
        is_correct = selected_answer == correct_answer

        existing_idx = None
        for idx, ans in enumerate(quiz_session['answers']):
            if ans['question_id'] == question_id:
                existing_idx = idx
                break

        answer_data = {
            'question_id': question_id,
            'selected': selected_answer,
            'correct': correct_answer,
            'is_correct': is_correct,
            'question': question['question'],
        }

        if existing_idx is not None:
            quiz_session['answers'][existing_idx] = answer_data
        else:
            quiz_session['answers'].append(answer_data)

        quiz_session['current_question'] = question_id + 1
        session[session_key] = quiz_session

        next_id = question_id + 1
        if next_id > total_questions:
            return redirect(url_for('result', folder=folder))
        return redirect(url_for('quiz', folder=folder, q=next_id))

    question_id = request.args.get('q', 1, type=int)

    if question_id > total_questions:
        return redirect(url_for('result', folder=folder))

    question = quiz_data['questions'][question_id - 1]
    answered_count = len(quiz_session['answers'])
    progress = (answered_count / total_questions) * 100 if total_questions > 0 else 0

    return render_template(
        'quiz.html',
        question=question,
        quiz=quiz_data,
        folder=folder,
        current_question=question_id,
        total_questions=total_questions,
        progress=progress,
        answered=answered_count,
    )


@app.route('/result/<folder>')
@login_required
def result(folder):
    quiz_path = os.path.join('quizzes', folder, 'quiz.json')
    if not os.path.exists(quiz_path):
        return 'Quiz not found', 404

    quiz_data = safe_load_json(quiz_path, {})

    session_key = f'quiz_{folder}'
    result_saved_key = f'result_saved_{folder}'
    quiz_session = session.get(session_key, {'answers': []})
    answers = quiz_session.get('answers', [])

    total_questions = len(quiz_data.get('questions', []))
    correct_answers = sum(1 for ans in answers if ans.get('is_correct'))
    wrong_answers = sum(1 for ans in answers if not ans.get('is_correct'))
    score = correct_answers / total_questions * 100 if total_questions > 0 else 0

    if answers and not session.get(result_saved_key):
        user = current_user()
        if user:
            record_quiz_result(
                user,
                folder,
                quiz_data.get('title', folder),
                score,
                correct_answers,
                wrong_answers,
                total_questions,
            )
            session[result_saved_key] = True

    return render_template(
        'result.html',
        answers=answers,
        score=score,
        total=total_questions,
        correct_answers=correct_answers,
        quiz=quiz_data,
        folder=folder,
    )


@app.route('/reset_quiz/<folder>')
@login_required
def reset_quiz(folder):
    session_key = f'quiz_{folder}'
    result_saved_key = f'result_saved_{folder}'
    if session_key in session:
        session.pop(session_key)
    if result_saved_key in session:
        session.pop(result_saved_key)
    return redirect(url_for('home'))


@app.route('/quizzes/<folder>/assets/<filename>')
def serve_asset(folder, filename):
    return send_from_directory(os.path.join('quizzes', folder, 'assets'), filename)


@app.route('/uploads/avatars/<filename>')
def serve_avatar_upload(filename):
    return send_from_directory(AVATAR_UPLOAD_DIR, filename)


ensure_storage()

if __name__ == '__main__':
    ensure_storage()
    app.run(debug=True, host='0.0.0.0', port=8080)
