import csv
import os
from flask import Flask, render_template_string, jsonify, request, redirect, url_for
from flask_sqlalchemy import SQLAlchemy
from flask_login import LoginManager, UserMixin, login_user, login_required, logout_user, current_user
from werkzeug.security import generate_password_hash, check_password_hash
import requests
from bs4 import BeautifulSoup
from urllib.parse import urljoin, urlparse, parse_qsl, urlencode, urlunparse
import urllib3
import re
import time
from selenium import webdriver
from selenium.webdriver.chrome.options import Options

urllib3.disable_warnings(urllib3.exceptions.InsecureRequestWarning)

app = Flask(__name__)
# 보안 키 및 데이터베이스 설정
app.config['SECRET_KEY'] = 'skku_super_secret_key_123'
app.config['SQLALCHEMY_DATABASE_URI'] = 'sqlite:///skku_notice.db'
app.config['SQLALCHEMY_TRACK_MODIFICATIONS'] = False

db = SQLAlchemy(app)
login_manager = LoginManager()
login_manager.init_app(app)
login_manager.login_view = 'login' 

# ==========================================
# 1. 데이터베이스(DB) 모델 설계
# ==========================================
user_board = db.Table('user_board',
    db.Column('user_id', db.Integer, db.ForeignKey('user.id')),
    db.Column('board_id', db.Integer, db.ForeignKey('board.id'))
)

class User(UserMixin, db.Model):
    id = db.Column(db.Integer, primary_key=True)
    username = db.Column(db.String(150), unique=True, nullable=False)
    password = db.Column(db.String(150), nullable=False)
    subscriptions = db.relationship('Board', secondary=user_board, backref='subscribers')

class Board(db.Model):
    id = db.Column(db.Integer, primary_key=True)
    board_id = db.Column(db.String(50)) 
    name = db.Column(db.String(100))
    url = db.Column(db.String(300), nullable=False)
    is_dynamic = db.Column(db.Boolean, default=False)
    page_param = db.Column(db.String(100))

@login_manager.user_loader
def load_user(user_id):
    return User.query.get(int(user_id))

def init_db():
    db.create_all()
    
    # DB에 등록된 게시판이 하나도 없다면 실행
    if not Board.query.first(): 
        # 같은 폴더에 notices.csv 파일이 있는지 확인
        if os.path.exists('notices.csv'):
            print("엑셀(CSV) 파일에서 게시판 정보를 불러오는 중입니다...")
            
            # 파일 열기 (한글 깨짐 방지를 위해 utf-8 설정)
            with open('notices.csv', 'r', encoding='utf-8') as f:
                reader = csv.reader(f)
                next(reader) # 첫 번째 줄(분류 제목들)은 데이터가 아니니 건너뛰기
                
                for idx, row in enumerate(reader):
                    # 만약 줄이 비어있으면 패스
                    if len(row) < 3: 
                        continue 
                    
                    college = row[0].strip()  # 예: 문과대학
                    category = row[1].strip() # 예: 학부
                    url = row[2].strip()      # 예: https://...
                    
                    # 링크가 'X'라고 적혀있거나 빈칸이면 DB에 넣지 않고 패스
                    if url == 'X' or not url:
                        continue
                        
                    # ================= 바꿀 코드 =================
                    # 'Notice'나 '공지' 같은 단어만 '공지사항'으로 통일해주고, 나머지는 그대로 붙임
                    if category in ['공지', 'Notice']:
                        category = '공지사항'
                        
                    # [단과대/학과] 학부, [단과대/학과] 대학원 형태로 깔끔하게 결합
                    board_name = f"[{college}] {category}"
                    
                    # 게시판 고유 ID 만들기
                    board_id = f"board_{idx}"
                    # =============================================
                    
                    # 성대 게시판들의 기본 페이지 넘기기 규칙
                    default_param = "?mode=list&articleLimit=10&article.offset={offset}"
                    
                    # 새 게시판 정보를 DB 객체로 만들기
                    new_board = Board(
                        board_id=board_id, 
                        name=board_name, 
                        url=url, 
                        is_dynamic=False, 
                        page_param=default_param
                    )
                    db.session.add(new_board)
            
            # DB에 최종 저장!
            db.session.commit()
            print("✅ 80여 개의 게시판 링크가 성공적으로 DB에 저장되었습니다!")
            
        else:
            print("⚠️ 'notices.csv' 파일을 찾을 수 없습니다. 파일 이름과 위치를 확인해 주세요.")

# ==========================================
# 2. 크롤링 함수
# ==========================================
def get_dynamic_html(url):
    options = Options()
    options.add_argument('--headless')
    options.add_argument('--no-sandbox')
    options.add_argument('--disable-dev-shm-usage')
    driver = webdriver.Chrome(options=options)
    try:
        driver.get(url)
        time.sleep(3) 
        return driver.page_source
    finally:
        driver.quit()

def clean_link(link):
    parsed = urlparse(link)
    qs = parse_qsl(parsed.query)
    exclude_keys = {'article.offset', 'pager.offset', 'page', 'cpage', 'pg', 'offset'}
    cleaned_qs = [(k, v) for k, v in qs if k.lower() not in exclude_keys]
    cleaned_qs.sort()
    parsed = parsed._replace(query=urlencode(cleaned_qs))
    return urlunparse(parsed)

def check_if_new(row):
    if not row: return False
    new_element = row.find(lambda tag: 
        (tag.name == 'img' and (
            (tag.has_attr('alt') and re.search(r'새글|new', tag['alt'], re.I)) or 
            (tag.has_attr('src') and re.search(r'new', tag['src'], re.I))
        )) or 
        (tag.has_attr('class') and any('new' in c.lower() for c in (tag['class'] if isinstance(tag['class'], list) else [tag['class']]))) or
        (tag.name in ['span', 'em', 'i', 'b', 'strong'] and tag.text.strip().upper() == 'NEW')
    )
    return bool(new_element)

def get_view_count(row):
    if not row: return ""
    text = row.get_text(" ", strip=True)
    match = re.search(r'조회수?\s*[:]?\s*([\d,]+)', text)
    if match: return match.group(1)
    elements = row.find_all(['td', 'span', 'div'], class_=re.compile(r'hit|view|read|count', re.I))
    for el in elements:
        num_text = el.get_text(strip=True).replace(',', '')
        if num_text.isdigit() and len(num_text) < 7: return el.get_text(strip=True)
    tds = row.find_all('td')
    number_tds = []
    for i, td in enumerate(tds):
        txt = td.get_text(strip=True).replace(',', '')
        if txt.isdigit(): number_tds.append((i, td.get_text(strip=True)))
    if number_tds:
        last_index, last_value = number_tds[-1]
        if last_index > 0: return last_value
    return ""

def scrape_single_site(board, page=1):
    notices = []
    headers = {'User-Agent': 'Mozilla/5.0'}
    date_pattern = re.compile(r'\d{2,4}[-./]\d{2}[-./]\d{2}')
    url = board.url
    
    if page > 1 and board.page_param:
        if '{offset}' in board.page_param:
            url += board.page_param.format(offset=(page-1)*10)
        elif '{page}' in board.page_param:
            url += board.page_param.format(page=page)

    try:
        if board.is_dynamic:
            html_content = get_dynamic_html(url)
        else:
            response = requests.get(url, headers=headers, timeout=10, verify=False)
            response.encoding = 'utf-8'
            html_content = response.text
            
        soup = BeautifulSoup(html_content, 'html.parser')
        count = 0 
        
        items = soup.select('td.jwxe_tl a, td.title a, td.subject a, td.left a, .board-list-content-title a, .title a, .tit a, .post-title a, .item-subject, .list-title a')
        if not items:
            items = [a for a in soup.find_all('a', href=True) if 'mode=view' in a['href'] or 'articleNo=' in a['href'] or 'bmode=view' in a['href']]
        
        for item in items:
            title_text = item.text.strip()
            link_path = item.get('href', '')
            if not title_text or len(title_text) < 3 or link_path.startswith('javascript'): continue
                
            full_link = urljoin(url, link_path)
            full_link = clean_link(full_link)
            
            date_text = ""
            is_new = False
            views = ""
            parent_row = item.find_parent(['tr', 'li', 'div']) 
            if parent_row:
                match = date_pattern.search(parent_row.text)
                if match: date_text = match.group()
                is_new = check_if_new(parent_row)
                views = get_view_count(parent_row) 
            
            notices.append({
                'category_id': board.board_id, 'category_name': board.name, 
                'title': title_text, 'link': full_link, 'date': date_text, 
                'is_new': is_new, 'views': views
            })
            count += 1
        
        if count == 0 and page == 1:
            notices.append({'category_id': board.board_id, 'category_name': board.name, 'title': f"👉 {board.name} 공지사항 직접 확인하기", 'link': board.url, 'date': "-", 'is_new': False, 'views': ''})
                
    except Exception as e:
        if page == 1:
            notices.append({'category_id': board.board_id, 'category_name': board.name, 'title': f"⚠️ 서버 접속 지연 (직접 접속)", 'link': board.url, 'date': "-", 'is_new': False, 'views': ''})
    return notices

# ==========================================
# 3. 웹페이지 접속 경로 (라우팅)
# ==========================================

# 모든 페이지의 기본이 되는 HTML 틀
HTML_BASE = """
<!DOCTYPE html>
<html>
<head>
    <title>성대 맞춤형 공지 대시보드</title>
    <style>
        body { font-family: 'Apple SD Gothic Neo', sans-serif; background-color: #f4f7f6; padding: 30px 10px; margin: 0; }
        .container { max-width: 850px; margin: auto; background: white; padding: 30px; border-radius: 20px; box-shadow: 0 5px 20px rgba(0,0,0,0.08); }
        h1 { color: #003e21; text-align: center; border-bottom: 2px solid #003e21; padding-bottom: 15px; margin-bottom: 25px; }
        .nav { text-align: right; margin-bottom: 20px; }
        .nav a { color: #003e21; text-decoration: none; font-weight: bold; margin-left: 15px; border: 1px solid #003e21; padding: 5px 15px; border-radius: 20px;}
        .nav a:hover { background-color: #003e21; color: white; }
        input[type="text"], input[type="password"] { width: 100%; padding: 10px; margin: 10px 0; border: 1px solid #ccc; border-radius: 5px; box-sizing: border-box;}
        button.btn { width: 100%; padding: 10px; background-color: #003e21; color: white; border: none; border-radius: 5px; cursor: pointer; font-size: 16px;}
        .form-container { max-width: 400px; margin: auto; }
        ul { list-style: none; padding: 0; margin: 0; }
        li { padding: 15px; border-bottom: 1px solid #eee; display: flex; align-items: center; gap: 10px; transition: 0.2s; }
        li:hover { background-color: #f1f8f4; transform: translateX(5px); }
        li a { text-decoration: none; color: #333; font-weight: 500; flex-grow: 1; }
        .tag { font-size: 0.75rem; font-weight: bold; padding: 4px 8px; border-radius: 6px; background-color: #e9ecef; color: #2d3436; white-space: nowrap; }
        .badge-new { background-color: #ff7675; color: white; font-size: 0.65rem; padding: 2px 5px; border-radius: 4px; margin-left: 8px;}
        .checkbox-group { display: flex; flex-direction: column; gap: 10px; margin-bottom: 20px; }
        .checkbox-group label { background: #f8f9fa; padding: 15px; border-radius: 8px; cursor: pointer; border: 1px solid #dee2e6;}
        
        .btn-group { display: flex; flex-wrap: wrap; gap: 10px; justify-content: center; margin-bottom: 20px; }
        .filter-btn { padding: 8px 16px; border: none; border-radius: 30px; background-color: #e9ecef; color: #495057; font-weight: bold; cursor: pointer; transition: 0.3s; font-size: 0.9rem; }
        .filter-btn:hover { background-color: #d3d9df; }
        .filter-btn.active { background-color: #003e21; color: white; box-shadow: 0 4px 10px rgba(0,62,33,0.3); }
    </style>
</head>
<body>
    <div class="container">
        {% if current_user.is_authenticated %}
            <div class="nav">
                <span>환영합니다, <b>{{ current_user.username }}</b>님!</span>
                <a href="{{ url_for('settings') }}">⚙️ 공지사항 설정</a>
                <a href="{{ url_for('logout') }}">로그아웃</a>
            </div>
        {% endif %}
        
        [[CONTENT]]
    </div>
</body>
</html>
"""

@app.route('/login', methods=['GET', 'POST'])
def login():
    if request.method == 'POST':
        user = User.query.filter_by(username=request.form.get('username')).first()
        if user and check_password_hash(user.password, request.form.get('password')):
            login_user(user)
            return redirect(url_for('home'))
        return "<script>alert('아이디나 비밀번호가 틀렸습니다.'); history.back();</script>"
    
    content = """
    <h1 style="border:none;">로그인</h1>
    <div class="form-container">
        <form method="POST">
            <input type="text" name="username" placeholder="아이디" required>
            <input type="password" name="password" placeholder="비밀번호" required>
            <button type="submit" class="btn">로그인</button>
        </form>
        <p style="text-align:center; margin-top:20px;"><a href="{{ url_for('register') }}">아직 계정이 없으신가요? 회원가입</a></p>
    </div>
    """
    final_html = HTML_BASE.replace('[[CONTENT]]', content)
    return render_template_string(final_html)

@app.route('/register', methods=['GET', 'POST'])
def register():
    if request.method == 'POST':
        username = request.form.get('username')
        password = request.form.get('password')
        if User.query.filter_by(username=username).first():
            return "<script>alert('이미 존재하는 아이디입니다.'); history.back();</script>"
            
        new_user = User(username=username, password=generate_password_hash(password, method='scrypt'))
        db.session.add(new_user)
        db.session.commit()
        return "<script>alert('회원가입 성공! 로그인해주세요.'); window.location.href='/login';</script>"
    
    content = """
    <h1 style="border:none;">회원가입</h1>
    <div class="form-container">
        <form method="POST">
            <input type="text" name="username" placeholder="사용할 아이디" required>
            <input type="password" name="password" placeholder="비밀번호" required>
            <button type="submit" class="btn">가입하기</button>
        </form>
    </div>
    """
    final_html = HTML_BASE.replace('[[CONTENT]]', content)
    return render_template_string(final_html)

@app.route('/logout')
@login_required
def logout():
    logout_user()
    return redirect(url_for('login'))

@app.route('/settings', methods=['GET', 'POST'])
@login_required
def settings():
    all_boards = Board.query.all()
    if request.method == 'POST':
        selected_ids = request.form.getlist('boards')
        current_user.subscriptions = [Board.query.get(int(bid)) for bid in selected_ids]
        db.session.commit()
        return redirect(url_for('home'))
        
    content = """
    <h1>공지사항 선택📝</h1>
    <form method="POST">
        <div class="checkbox-group">
            {% for board in boards %}
            <label>
                <input type="checkbox" name="boards" value="{{ board.id }}" 
                {% if board in current_user.subscriptions %}checked{% endif %}>
                {{ board.name }}
            </label>
            {% endfor %}
        </div>
        <button type="submit" class="btn">저장하고 메인으로 가기</button>
    </form>
    """
    final_html = HTML_BASE.replace('[[CONTENT]]', content)
    return render_template_string(final_html, boards=all_boards)

@app.route('/')
@login_required
def home():
    if not current_user.subscriptions:
        return redirect(url_for('settings'))
        
    sorted_boards = sorted(current_user.subscriptions, key=lambda board: board.id)
        
    all_notices = []
    for board in sorted_boards:
        # 각 게시판에서 긁어온 원래 순서(홈페이지 순서) 그대로 리스트에 추가합니다.
        all_notices.extend(scrape_single_site(board, page=1))
        
    content = """
    <style>
        {% for board in boards %}
            .tag-{{ board.board_id }} {
                background-color: hsl({{ (board.id * 137) % 360 }}, 70%, 85%);
                color: hsl({{ (board.id * 137) % 360 }}, 70%, 25%);
            }
        {% endfor %}
        .load-more-btn { background-color: #003e21; color: white; border: none; padding: 10px 30px; border-radius: 30px; font-size: 1rem; font-weight: bold; cursor: pointer; transition: 0.3s; box-shadow: 0 4px 10px rgba(0,62,33,0.3); }
        .load-more-btn:hover { background-color: #002815; transform: translateY(-2px); }
        .load-more-btn:disabled { background-color: #95a5a6; cursor: not-allowed; transform: none; box-shadow: none; }
        .meta-info { font-size: 0.85rem; color: #7f8c8d; white-space: nowrap; margin-left: 10px; display: flex; gap: 8px; align-items: center; }
    </style>

    <h1>{{ current_user.username }}'s 실시간 공지🔊</h1>
    
    <div class="btn-group">
        <button class="filter-btn active" onclick="filterNotices('all', this)">전체보기</button>
        {% for board in boards %}
            <button class="filter-btn" onclick="filterNotices('{{ board.board_id }}', this)">{{ board.name }}</button>
        {% endfor %}
    </div>

    <ul id="notice-list">
        {% for notice in notices %}
            <li class="notice-item {{ notice.category_id }}">
                <span class="tag tag-{{ notice.category_id }}">{{ notice.category_name }}</span>
                <a href="{{ notice.link }}" target="_blank">{{ notice.title }}
                    {% if notice.is_new %}<span class="badge-new">NEW</span>{% endif %}
                </a>
                
                <div class="meta-info">
                    {% if notice.views %}
                        <span>👁️ {{ notice.views }}</span> <span style="color:#ddd;">|</span>
                    {% endif %}
                    <span>{{ notice.date }}</span>
                </div>
            </li>
        {% endfor %}
    </ul>
    
    <div id="load-more-container" style="display: none; text-align: center; margin-top: 25px;">
        <button id="load-more-btn" class="load-more-btn" onclick="loadMore()">더보기 🔽</button>
    </div>

    <script>
        let currentCategory = 'all';
        let pages = {};
        
        {% for board in boards %}
            pages['{{ board.board_id }}'] = 1;
        {% endfor %}

        function filterNotices(categoryId, btnElement) {
            currentCategory = categoryId;
            
            document.querySelectorAll('.filter-btn').forEach(btn => btn.classList.remove('active'));
            btnElement.classList.add('active');

            const items = document.querySelectorAll('.notice-item');
            items.forEach(item => {
                if (categoryId === 'all' || item.classList.contains(categoryId)) {
                    item.style.display = 'flex';
                } else {
                    item.style.display = 'none';
                }
            });

            const loadMoreContainer = document.getElementById('load-more-container');
            if (categoryId === 'all') {
                loadMoreContainer.style.display = 'none';
            } else {
                loadMoreContainer.style.display = 'block';
            }
        }

        function loadMore() {
            if (currentCategory === 'all') return;
            
            const btn = document.getElementById('load-more-btn');
            btn.innerText = '불러오는 중... ⏳';
            btn.disabled = true;

            pages[currentCategory]++;
            const page = pages[currentCategory];

            fetch(`/api/notices?board_id=${currentCategory}&page=${page}`)
                .then(response => response.json())
                .then(data => {
                    const list = document.getElementById('notice-list');
                    let newCount = 0;
                    
                    const existingLinks = new Set(Array.from(document.querySelectorAll('.notice-item a')).map(a => a.href));
                    
                    data.forEach(notice => {
                        if (existingLinks.has(notice.link)) return;
                        
                        existingLinks.add(notice.link); 

                        const li = document.createElement('li');
                        li.className = `notice-item ${notice.category_id}`;
                        
                        const newBadge = notice.is_new ? `<span class="badge-new">NEW</span>` : '';
                        const viewsHtml = notice.views ? `<span>👁️ ${notice.views}</span> <span style="color:#ddd;">|</span> ` : '';
                        
                        li.innerHTML = `
                            <span class="tag tag-${notice.category_id}">${notice.category_name}</span>
                            <a href="${notice.link}" target="_blank">${notice.title} ${newBadge}</a>
                            <div class="meta-info">
                                ${viewsHtml}
                                <span>${notice.date}</span>
                            </div>
                        `;
                        list.appendChild(li);
                        newCount++;
                    });

                    if (newCount === 0) {
                        alert('더 이상 불러올 새로운 공지사항이 없거나 마지막 페이지입니다.');
                    }
                })
                .catch(error => {
                    console.error(error);
                    alert('데이터를 불러오는데 실패했습니다.');
                    pages[currentCategory]--; 
                })
                .finally(() => {
                    btn.innerText = '더보기 🔽';
                    btn.disabled = false;
                });
        }
    </script>
    """
    final_html = HTML_BASE.replace('[[CONTENT]]', content)
    return render_template_string(final_html, notices=all_notices, boards=sorted_boards)
@app.route('/api/notices')
@login_required
def api_get_notices():
    board_id = request.args.get('board_id')
    page = int(request.args.get('page', 1))
    
    board = Board.query.filter_by(board_id=board_id).first()
    if not board:
        return jsonify([])
        
    notices = scrape_single_site(board, page=page)
    return jsonify(notices)

if __name__ == '__main__':
    with app.app_context():
        init_db() 
    app.run(debug=True)



