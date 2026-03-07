import csv
import os
import threading
import time
import logging
import json
from datetime import datetime
from flask import Flask, render_template_string, jsonify, request, redirect, url_for, make_response
from flask_sqlalchemy import SQLAlchemy
from flask_login import LoginManager, UserMixin, login_user, login_required, logout_user, current_user
from werkzeug.security import generate_password_hash, check_password_hash
import requests
from bs4 import BeautifulSoup
from urllib.parse import urljoin, urlparse, parse_qsl, urlencode, urlunparse
import urllib3
import re
import smtplib
from email.mime.text import MIMEText
import random
from dotenv import load_dotenv
from datetime import datetime, timedelta
from pywebpush import webpush, WebPushException

# ==========================================
# 로깅 설정
# ==========================================
logging.basicConfig(
    level=logging.INFO,
    format='%(asctime)s [%(levelname)s] %(message)s',
    datefmt='%Y-%m-%d %H:%M:%S'
)
logger = logging.getLogger(__name__)


urllib3.disable_warnings(urllib3.exceptions.InsecureRequestWarning)

load_dotenv()
app = Flask(__name__)
app.config['SECRET_KEY'] = os.getenv('SECRET_KEY', 'default-fallback-key')
app.config['SQLALCHEMY_DATABASE_URI'] = 'sqlite:///skku_notice.db'
app.config['SQLALCHEMY_TRACK_MODIFICATIONS'] = False

# ==========================================
# VAPID 키 설정 (Web Push)
# ==========================================
# 최초 1회: python -c "from pywebpush import webpush; from py_vapid import Vapid; v=Vapid(); v.generate_keys(); print('PRIVATE:', v.private_pem()); print('PUBLIC:', v.public_key)"
# 또는 https://vapidkeys.com/ 에서 생성 후 .env에 저장
VAPID_PRIVATE_KEY = os.getenv('VAPID_PRIVATE_KEY', '')
VAPID_PUBLIC_KEY = os.getenv('VAPID_PUBLIC_KEY', '')
VAPID_CLAIMS = {"sub": "mailto:" + os.getenv('SENDER_EMAIL', 'admin@skku.edu')}

# ==========================================
# 크롤링 스케줄러 설정값
# ==========================================
CRAWL_INTERVAL_SECONDS = int(os.getenv('CRAWL_INTERVAL', '1800'))  # 기본 30분 (초 단위)
CRAWL_TIMEOUT = int(os.getenv('CRAWL_TIMEOUT', 15))             # 요청 타임아웃
CRAWL_MAX_WORKERS = int(os.getenv('CRAWL_MAX_WORKERS', 5))      # 동시 크롤링 스레드 수

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

# 기존 모델 아래에 추가
class VerificationCode(db.Model):
    id = db.Column(db.Integer, primary_key=True)
    email = db.Column(db.String(50), unique=True, nullable=False)
    code = db.Column(db.String(10), nullable=False)
    created_at = db.Column(db.DateTime, default=datetime.utcnow)

class User(UserMixin, db.Model):
    id = db.Column(db.Integer, primary_key=True)
    username = db.Column(db.String(20), unique=True, nullable=False)
    password = db.Column(db.String(80), nullable=False)
    email = db.Column(db.String(50), unique=True, nullable=False)
    department = db.Column(db.String(50), nullable=False)
    student_id = db.Column(db.String(20), nullable=False)
    subscriptions = db.relationship('Board', secondary=user_board, backref=db.backref('subscribers', lazy='dynamic'))

class Board(db.Model):
    id = db.Column(db.Integer, primary_key=True)
    board_id = db.Column(db.String(50))
    name = db.Column(db.String(100))
    url = db.Column(db.String(300), nullable=False)
    page_param = db.Column(db.String(100))

# [신규] 크롤링 결과를 DB에 캐싱하는 테이블
class CachedNotice(db.Model):
    id = db.Column(db.Integer, primary_key=True)
    board_id = db.Column(db.String(50), db.ForeignKey('board.board_id'), index=True)
    page = db.Column(db.Integer, default=1, index=True)
    title = db.Column(db.String(500))
    link = db.Column(db.String(500))
    date = db.Column(db.String(50))
    is_new = db.Column(db.Boolean, default=False)
    views = db.Column(db.String(20), default='')
    category_name = db.Column(db.String(100))
    crawled_at = db.Column(db.DateTime, default=datetime.utcnow)

    # 복합 인덱스: board_id + page 조합 조회 최적화
    __table_args__ = (
        db.Index('ix_board_page', 'board_id', 'page'),
    )

# [신규] 크롤링 상태 추적 테이블
class CrawlStatus(db.Model):
    id = db.Column(db.Integer, primary_key=True)
    board_id = db.Column(db.String(50), unique=True, index=True)
    last_crawled = db.Column(db.DateTime)
    last_success = db.Column(db.Boolean, default=True)
    error_count = db.Column(db.Integer, default=0)      # 연속 실패 횟수
    notice_count = db.Column(db.Integer, default=0)      # 마지막 크롤링 건수

# [신규] 푸시 알림 구독 정보 테이블
class PushSubscription(db.Model):
    id = db.Column(db.Integer, primary_key=True)
    user_id = db.Column(db.Integer, db.ForeignKey('user.id'), nullable=False, index=True)
    subscription_json = db.Column(db.Text, nullable=False)  # 브라우저 Push 구독 정보 (JSON)
    created_at = db.Column(db.DateTime, default=datetime.utcnow)
    is_active = db.Column(db.Boolean, default=True)

    user = db.relationship('User', backref=db.backref('push_subscriptions', lazy='dynamic'))

def is_valid_password(password):
    if len(password) < 8:
        return False
    if not re.search(r"[!@#$%^&*(),.?\":{}|<>]", password):
        return False
    return True

@login_manager.user_loader
def load_user(user_id):
    return User.query.get(int(user_id))

def init_db():
    db.create_all()
    if not Board.query.first():
        if os.path.exists('notices.csv'):
            logger.info("CSV 파일에서 게시판 정보를 불러오는 중...")
            with open('notices.csv', 'r', encoding='utf-8') as f:
                reader = csv.reader(f)
                next(reader)
                for idx, row in enumerate(reader):
                    if len(row) < 3:
                        continue
                    college = row[0].strip()
                    category = row[1].strip()
                    url = row[2].strip()
                    if url == 'X' or not url:
                        continue
                    if category in ['공지', 'Notice']:
                        category = '공지사항'
                    board_name = f"[{college}] {category}"
                    board_id = f"board_{idx}"
                    
                    # [수정된 부분] URL 확장자 및 형태에 따라 페이지네이션 파라미터 분기
                    if '.php' in url or '.asp' in url or '.jsp' in url or 'skkustartup.kr' in url:
                        # 그누보드 등 일반 게시판은 page=1, 2 형태를 사용
                        default_param = "page={page}"
                    else:
                        # 성대 표준 CMS 게시판은 offset 형태를 사용
                        default_param = "article.offset={offset}&articleLimit=10&mode=list"
                        
                    new_board = Board(
                        board_id=board_id, name=board_name, url=url,
                        page_param=default_param
                    )
                    db.session.add(new_board)
            db.session.commit()
            logger.info("✅ 게시판 링크가 DB에 저장되었습니다!")
        else:
            logger.warning("⚠️ 'notices.csv' 파일을 찾을 수 없습니다.")


# ==========================================
# 2. 크롤링 핵심 함수 (정적 HTTP 요청만 사용)
# ==========================================

def clean_link(link):
    parsed = urlparse(link)
    qs = parse_qsl(parsed.query)
    exclude_keys = {'article.offset', 'pager.offset', 'page', 'cpage', 'pg', 'offset'}
    cleaned_qs = [(k, v) for k, v in qs if k.lower() not in exclude_keys]
    cleaned_qs.sort()
    parsed = parsed._replace(query=urlencode(cleaned_qs))
    return urlunparse(parsed)

def check_if_new(row):
    if not row:
        return False
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
    if not row:
        return ""
    text = row.get_text(" ", strip=True)
    match = re.search(r'조회수?\s*[:]?\s*([\d,]+)', text)
    if match:
        return match.group(1)
    elements = row.find_all(['td', 'span', 'div'], class_=re.compile(r'hit|view|read|count', re.I))
    for el in elements:
        num_text = el.get_text(strip=True).replace(',', '')
        if num_text.isdigit() and len(num_text) < 7:
            return el.get_text(strip=True)
    tds = row.find_all('td')
    number_tds = []
    for i, td in enumerate(tds):
        txt = td.get_text(strip=True).replace(',', '')
        if txt.isdigit():
            number_tds.append((i, td.get_text(strip=True)))
    if number_tds:
        last_index, last_value = number_tds[-1]
        if last_index > 0:
            return last_value
    return ""

# HTTP Session 재사용 (커넥션 풀링)
_http_session = requests.Session()
_http_session.headers.update({'User-Agent': 'Mozilla/5.0'})
_http_session.verify = False

def build_page_url(base_url, page_param, page):
    """
    게시판 URL에 페이지네이션 파라미터를 올바르게 적용.
    기존 URL의 쿼리스트링과 page_param을 병합하여 중복/충돌 방지.
    """
    if page <= 1 or not page_param:
        return base_url

    # page_param 템플릿에 값 채우기
    if '{offset}' in page_param:
        param_str = page_param.format(offset=(page - 1) * 10)
    elif '{page}' in page_param:
        param_str = page_param.format(page=page)
    else:
        return base_url

    # page_param에서 파라미터만 추출 (앞의 ? 제거)
    new_params = dict(parse_qsl(param_str.lstrip('?')))

    # 기존 URL 파싱
    parsed = urlparse(base_url)
    existing_params = dict(parse_qsl(parsed.query))

    # 병합: page_param이 기존 파라미터를 덮어씀
    existing_params.update(new_params)

    # URL 재조립
    new_query = urlencode(existing_params)
    return urlunparse(parsed._replace(query=new_query))


def scrape_single_site(board, page=1):
    """단일 게시판 크롤링 (정적 HTTP 요청만 사용)"""
    notices = []
    date_pattern = re.compile(r'\d{2,4}[-./]\d{2}[-./]\d{2}')
    url = build_page_url(board.url, board.page_param, page)
    logger.debug(f"크롤링 요청: [{board.name}] page={page} → {url}")

    try:
        response = _http_session.get(url, timeout=CRAWL_TIMEOUT)
        response.encoding = 'utf-8'
        html_content = response.text

        soup = BeautifulSoup(html_content, 'lxml')
        count = 0

        # [수정된 부분] 다른 웹 프레임워크(그누보드 등)의 CSS 선택자 추가
        items = soup.select(
            'td.jwxe_tl a, td.title a, td.subject a, td.left a, '
            '.board-list-content-title a, .title a, .tit a, .post-title a, '
            '.item-subject, .list-title a, '
            '.bo_tit a, .td_subject a, td.subject_pc a, .board_list .subject a, .subject a'
        )
        
        if not items:
            items = [a for a in soup.find_all('a', href=True)
                     if 'mode=view' in a['href'] or 'articleNo=' in a['href'] or 'bmode=view' in a['href'] or 'wr_id=' in a['href']]

        for item in items:
            title_text = item.text.strip()
            link_path = item.get('href', '')
            if not title_text or len(title_text) < 3 or link_path.startswith('javascript'):
                continue

            full_link = urljoin(url, link_path)
            full_link = clean_link(full_link)

            date_text = ""
            is_new = False
            views = ""
            parent_row = item.find_parent(['tr', 'li', 'div'])
            if parent_row:
                match = date_pattern.search(parent_row.text)
                if match:
                    date_text = match.group()
                is_new = check_if_new(parent_row)
                views = get_view_count(parent_row)

            notices.append({
                'category_id': board.board_id, 'category_name': board.name,
                'title': title_text, 'link': full_link, 'date': date_text,
                'is_new': is_new, 'views': views
            })
            count += 1

        if count == 0 and page == 1:
            notices.append({
                'category_id': board.board_id, 'category_name': board.name,
                'title': f"👉 {board.name} 공지사항 직접 확인하기 (자동 수집 불가)",
                'link': board.url, 'date': "-", 'is_new': False, 'views': ''
            })

    except Exception as e:
        logger.error(f"크롤링 실패 [{board.name}]: {e}")
        if page == 1:
            notices.append({
                'category_id': board.board_id, 'category_name': board.name,
                'title': f"⚠️ 접속 지연 - 직접 확인하기",
                'link': board.url, 'date': "-", 'is_new': False, 'views': ''
            })
    return notices

# ==========================================
# 3. 백그라운드 크롤링 스케줄러
# ==========================================
from concurrent.futures import ThreadPoolExecutor, as_completed

def crawl_board_task(board_id, board_name, board_url, page_param):
    """
    개별 게시판 크롤링 작업 (스레드 풀에서 실행).
    """
    class _FakeBoard:
        pass

    fake = _FakeBoard()
    fake.board_id = board_id
    fake.name = board_name
    fake.url = board_url
    fake.page_param = page_param

    notices = scrape_single_site(fake, page=1)
    return board_id, board_name, notices


def send_push_notification(subscription_json, title, body, url_link=""):
    """
    개별 구독자에게 Web Push 알림 발송.
    실패 시 해당 구독을 비활성화.
    """
    if not VAPID_PRIVATE_KEY or not VAPID_PUBLIC_KEY:
        return False
    try:
        payload = json.dumps({
            "title": title,
            "body": body,
            "url": url_link,
            "icon": "/static/icon-192.png",
            "badge": "/static/icon-72.png"
        }, ensure_ascii=False)

        webpush(
            subscription_info=json.loads(subscription_json),
            data=payload,
            vapid_private_key=VAPID_PRIVATE_KEY,
            vapid_claims=VAPID_CLAIMS
        )
        return True
    except WebPushException as e:
        logger.warning(f"푸시 발송 실패: {e}")
        # 410 Gone 또는 404 → 구독 만료/해제
        if hasattr(e, 'response') and e.response and e.response.status_code in (404, 410):
            try:
                sub = PushSubscription.query.filter_by(subscription_json=subscription_json).first()
                if sub:
                    sub.is_active = False
                    db.session.commit()
            except Exception:
                pass
        return False
    except Exception as e:
        logger.error(f"푸시 발송 오류: {e}")
        return False


def notify_subscribers_of_new_notices(board_id, new_notices):
    """
    특정 게시판의 구독자들에게 새 공지 알림을 보냄.
    new_notices: 새로 감지된 공지사항 리스트
    """
    if not new_notices or not VAPID_PRIVATE_KEY:
        return

    board = Board.query.filter_by(board_id=board_id).first()
    if not board:
        return

    # 이 게시판의 구독자 중 활성 푸시 구독이 있는 사람들
    subscribers = board.subscribers.all()
    if not subscribers:
        return

    for user in subscribers:
        active_subs = PushSubscription.query.filter_by(
            user_id=user.id, is_active=True
        ).all()

        for sub in active_subs:
            # 새 공지 중 최대 3개까지 알림
            for notice in new_notices[:3]:
                title = f"📢 {board.name}"
                body = notice['title'][:80]
                send_push_notification(
                    sub.subscription_json,
                    title=title,
                    body=body,
                    url_link=notice.get('link', '/')
                )

    count = len(new_notices)
    logger.info(f"🔔 [{board.name}] 새 공지 {count}건 → {len(subscribers)}명에게 푸시 발송 시도")


def run_scheduled_crawl():
    """
    구독자가 1명 이상인 게시판만 크롤링하여 DB에 캐싱.
    ThreadPoolExecutor로 병렬 처리.
    """
    with app.app_context():
        # 구독자가 있는 게시판만 추출
        subscribed_boards = Board.query.filter(Board.subscribers.any()).all()

        if not subscribed_boards:
            logger.info("구독자가 있는 게시판이 없어 크롤링을 건너뜁니다.")
            return

        logger.info(f"⏰ 예약 크롤링 시작: {len(subscribed_boards)}개 게시판")
        start_time = time.time()

        tasks = [
            (b.board_id, b.name, b.url, b.page_param)
            for b in subscribed_boards
        ]

        results = {}
        # 동시 요청을 3개로 제한하고, 대상 서버 부담 완화
        with ThreadPoolExecutor(max_workers=min(CRAWL_MAX_WORKERS, 3)) as executor:
            futures = {
                executor.submit(crawl_board_task, *t): t[0]
                for t in tasks
            }
            for future in as_completed(futures):
                board_id = futures[future]
                try:
                    bid, bname, notices = future.result(timeout=30)
                    results[bid] = notices
                except Exception as e:
                    logger.error(f"크롤링 스레드 실패 [{board_id}]: {e}")
                    results[board_id] = []
                # 요청 간 딜레이 (서버 부담 완화)
                time.sleep(1)

        # DB에 캐시 저장 (한번에 batch로) + 새 공지 감지 → 푸시 알림
        now = datetime.utcnow()
        for board_id, notices in results.items():
            # 기존 캐시에서 링크 목록을 미리 수집 (새 공지 판별용)
            existing_links = set(
                row[0] for row in
                db.session.query(CachedNotice.link).filter_by(board_id=board_id, page=1).all()
            )

            # 기존 page=1 캐시 삭제
            CachedNotice.query.filter_by(board_id=board_id, page=1).delete()

            new_notices_for_push = []
            for n in notices:
                cached = CachedNotice(
                    board_id=n['category_id'],
                    page=1,
                    title=n['title'],
                    link=n['link'],
                    date=n['date'],
                    is_new=n['is_new'],
                    views=n.get('views', ''),
                    category_name=n['category_name'],
                    crawled_at=now
                )
                db.session.add(cached)

                # 기존에 없던 링크 → 새 공지로 간주
                if n['link'] not in existing_links and n.get('title', '').startswith(('⚠️', '👉')) is False:
                    new_notices_for_push.append(n)

            # 크롤링 상태 업데이트
            status = CrawlStatus.query.filter_by(board_id=board_id).first()
            if not status:
                status = CrawlStatus(board_id=board_id)
                db.session.add(status)
            status.last_crawled = now
            status.last_success = len(notices) > 0
            status.notice_count = len(notices)
            if len(notices) > 0:
                status.error_count = 0
            else:
                status.error_count = (status.error_count or 0) + 1

            # 새 공지가 있으면 푸시 알림 발송 (첫 크롤링 제외: existing_links가 비어있으면 무시)
            if new_notices_for_push and existing_links:
                try:
                    notify_subscribers_of_new_notices(board_id, new_notices_for_push)
                except Exception as e:
                    logger.error(f"푸시 알림 발송 오류 [{board_id}]: {e}")

        db.session.commit()
        elapsed = time.time() - start_time
        logger.info(f"✅ 예약 크롤링 완료: {len(results)}개 게시판, {elapsed:.1f}초 소요")


def scheduler_loop():
    """30분 간격으로 크롤링을 반복하는 데몬 스레드"""
    logger.info(f"📅 크롤링 스케줄러 시작 (간격: {CRAWL_INTERVAL_SECONDS}초)")
    # 서버 시작 직후 첫 크롤링
    time.sleep(5)  # Flask 초기화 완료 대기
    while True:
        try:
            run_scheduled_crawl()
        except Exception as e:
            logger.error(f"스케줄러 오류: {e}")
        time.sleep(CRAWL_INTERVAL_SECONDS)


def get_cached_notices(board_id, page=1):
    """
    DB 캐시에서 공지사항을 읽어옴.
    캐시가 없으면(아직 한번도 크롤링 안됨) 즉시 크롤링 후 반환.
    """
    cached = CachedNotice.query.filter_by(board_id=board_id, page=page)\
        .order_by(CachedNotice.id.asc()).all()

    if cached:
        return [{
            'category_id': c.board_id,
            'category_name': c.category_name,
            'title': c.title,
            'link': c.link,
            'date': c.date,
            'is_new': c.is_new,
            'views': c.views
        } for c in cached]

    # 캐시 미스: page>1이거나 최초 접근 시 실시간 크롤링 (fallback)
    board = Board.query.filter_by(board_id=board_id).first()
    if not board:
        return []
    notices = scrape_single_site(board, page=page)

    # page==1이면 캐시에 저장
    if page == 1 and notices:
        now = datetime.utcnow()
        for n in notices:
            db.session.add(CachedNotice(
                board_id=n['category_id'], page=1,
                title=n['title'], link=n['link'],
                date=n['date'], is_new=n['is_new'],
                views=n.get('views', ''),
                category_name=n['category_name'],
                crawled_at=now
            ))
        db.session.commit()

    return notices


# ==========================================
# 4. 웹페이지 접속 경로 (라우팅)
# ==========================================

HTML_BASE = """
<!DOCTYPE html>
<html lang="ko">
<head>
    <meta charset="UTF-8">
    <meta name="viewport" content="width=device-width, initial-scale=1.0">
    <title>성대 맞춤형 공지 대시보드</title>

    <!-- PWA 메타태그 -->
    <link rel="manifest" href="/manifest.json">
    <meta name="theme-color" content="#003e21">
    <meta name="apple-mobile-web-app-capable" content="yes">
    <meta name="apple-mobile-web-app-status-bar-style" content="black-translucent">
    <meta name="apple-mobile-web-app-title" content="성대공지">
    <link rel="apple-touch-icon" href="/static/icon-192.png">

    <style>
        * { box-sizing: border-box; }
        body { font-family: 'Apple SD Gothic Neo', -apple-system, BlinkMacSystemFont, sans-serif; background-color: #f4f7f6; padding: 15px 8px; margin: 0; }
        .container { max-width: 850px; margin: auto; background: white; padding: 20px 15px; border-radius: 20px; box-shadow: 0 5px 20px rgba(0,0,0,0.08); }
        h1 { color: #003e21; text-align: center; border-bottom: 2px solid #003e21; padding-bottom: 15px; margin-bottom: 20px; font-size: 1.4rem; }
        .nav { margin-bottom: 20px; display: flex; flex-wrap: wrap; justify-content: flex-end; align-items: center; gap: 8px; }
        .nav span { margin-right: auto; font-size: 0.88rem; }
        .nav a { color: #003e21; text-decoration: none; font-weight: bold; border: 1px solid #003e21; padding: 5px 12px; border-radius: 20px; font-size: 0.8rem; white-space: nowrap; }
        .nav a:hover { background-color: #003e21; color: white; }
        input[type="text"], input[type="password"] { width: 100%; padding: 10px; margin: 10px 0; border: 1px solid #ccc; border-radius: 5px; box-sizing: border-box;}
        button.btn { width: 100%; padding: 10px; background-color: #003e21; color: white; border: none; border-radius: 5px; cursor: pointer; font-size: 16px;}
        .form-container { max-width: 400px; margin: auto; }
        ul { list-style: none; padding: 0; margin: 0; }

        /* ===== 공지 리스트: 3줄 레이아웃 (태그 → 제목 → 메타) ===== */
        li.notice-item { padding: 12px 8px; border-bottom: 1px solid #eee; transition: background 0.2s; }
        li.notice-item:hover { background-color: #f1f8f4; }
        .notice-row-tag { margin-bottom: 5px; display: flex; align-items: center; gap: 6px; }
        .notice-row-title { margin-bottom: 4px; }
        .notice-row-title a { text-decoration: none; color: #333; font-weight: 500; word-break: keep-all; overflow-wrap: break-word; line-height: 1.45; font-size: 0.93rem; display: block; }
        .notice-row-meta { display: flex; align-items: center; gap: 6px; }

        .tag { font-size: 0.68rem; font-weight: bold; padding: 2px 7px; border-radius: 6px; background-color: #e9ecef; color: #2d3436; white-space: nowrap; }
        .badge-new { background-color: #ff7675; color: white; font-size: 0.6rem; padding: 1px 5px; border-radius: 4px; margin-left: 2px; vertical-align: middle; }
        .meta-info { font-size: 0.78rem; color: #95a5a6; white-space: nowrap; display: flex; gap: 6px; align-items: center; }

        .checkbox-group { display: flex; flex-direction: column; gap: 10px; margin-bottom: 20px; }
        .checkbox-group label { background: #f8f9fa; padding: 15px; border-radius: 8px; cursor: pointer; border: 1px solid #dee2e6;}
        .btn-group { display: flex; flex-wrap: wrap; gap: 8px; justify-content: center; margin-bottom: 20px; }
        .filter-btn { padding: 7px 14px; border: none; border-radius: 30px; background-color: #e9ecef; color: #495057; font-weight: bold; cursor: pointer; transition: 0.3s; font-size: 0.82rem; }
        .filter-btn:hover { background-color: #d3d9df; }
        .filter-btn.active { background-color: #003e21; color: white; box-shadow: 0 4px 10px rgba(0,62,33,0.3); }
        .crawl-info { text-align: center; font-size: 0.8rem; color: #95a5a6; margin-top: -15px; margin-bottom: 20px; }
        /* 알림 버튼 */
        .notify-btn { background: none; border: 2px solid #003e21; color: #003e21; padding: 6px 14px; border-radius: 20px; cursor: pointer; font-size: 0.85rem; font-weight: bold; transition: 0.3s; white-space: nowrap; }
        .notify-btn:hover { background-color: #003e21; color: white; }
        .notify-btn.active { background-color: #003e21; color: white; }
        .notify-btn.active:hover { background-color: #c0392b; border-color: #c0392b; }
        /* 홈화면 추가 배너 */
        .pwa-install-banner { display: none; background: linear-gradient(135deg, #003e21 0%, #006633 100%); color: white; padding: 12px 20px; border-radius: 10px; margin-bottom: 15px; font-size: 0.85rem; text-align: center; cursor: pointer; }
        .pwa-install-banner:hover { opacity: 0.9; }

        /* 모바일 세부 조정 */
        @media (max-width: 600px) {
            body { padding: 6px 3px; }
            .container { padding: 14px 10px; border-radius: 14px; }
            h1 { font-size: 1.15rem; }
            .nav a { padding: 4px 9px; font-size: 0.72rem; }
            .filter-btn { padding: 6px 10px; font-size: 0.75rem; }
            .tag { font-size: 0.62rem; padding: 2px 5px; }
            li.notice-item { padding: 10px 5px; }
            .notice-row-title a { font-size: 0.88rem; }
            .meta-info { font-size: 0.73rem; }
        }
    </style>
</head>
<body>
    <div class="container">
        {% if current_user.is_authenticated %}
            <div class="nav">
                <span>환영합니다, <b>{{ current_user.username }}</b>님!</span>
                {% if current_user.username == 'admin환휘' %}
                    <a href="{{ url_for('admin_dashboard') }}">📊 관리자 페이지</a>
                {% endif %}
                <a href="{{ url_for('settings') }}">⚙️ 공지사항 설정</a>
                <a href="{{ url_for('logout') }}">로그아웃</a>
            </div>
        {% endif %}
        [[CONTENT]]
    </div>

    <!-- PWA Service Worker 등록 + 푸시 알림 헬퍼 -->
    <script>
    // Service Worker 등록
    if ('serviceWorker' in navigator) {
        navigator.serviceWorker.register('/sw.js', { scope: '/' })
            .then(reg => console.log('SW 등록 성공:', reg.scope))
            .catch(err => console.warn('SW 등록 실패:', err));
    }

    // VAPID 키를 base64 → Uint8Array 변환
    function urlBase64ToUint8Array(base64String) {
        const padding = '='.repeat((4 - base64String.length % 4) % 4);
        const base64 = (base64String + padding).replace(/-/g, '+').replace(/_/g, '/');
        const rawData = window.atob(base64);
        const outputArray = new Uint8Array(rawData.length);
        for (let i = 0; i < rawData.length; ++i) {
            outputArray[i] = rawData.charCodeAt(i);
        }
        return outputArray;
    }

    // 푸시 알림 구독
    async function subscribePush() {
        try {
            const permission = await Notification.requestPermission();
            if (permission !== 'granted') {
                alert('알림 권한이 거부되었습니다. 브라우저 설정에서 알림을 허용해주세요.');
                return false;
            }

            const reg = await navigator.serviceWorker.ready;
            const keyRes = await fetch('/api/vapid-public-key');
            const keyData = await keyRes.json();

            if (!keyData.publicKey) {
                alert('서버에서 푸시 키를 가져올 수 없습니다. 관리자에게 문의해주세요.');
                return false;
            }

            const subscription = await reg.pushManager.subscribe({
                userVisibleOnly: true,
                applicationServerKey: urlBase64ToUint8Array(keyData.publicKey)
            });

            const res = await fetch('/api/push/subscribe', {
                method: 'POST',
                headers: { 'Content-Type': 'application/json' },
                body: JSON.stringify({ subscription: subscription.toJSON() })
            });

            const result = await res.json();
            if (result.success) {
                updateNotifyButton(true);
                return true;
            }
            return false;
        } catch (err) {
            console.error('푸시 구독 오류:', err);
            alert('알림 설정 중 오류가 발생했습니다: ' + err.message);
            return false;
        }
    }

    // 푸시 알림 해제
    async function unsubscribePush() {
        try {
            const reg = await navigator.serviceWorker.ready;
            const subscription = await reg.pushManager.getSubscription();
            if (subscription) {
                await subscription.unsubscribe();
                await fetch('/api/push/unsubscribe', {
                    method: 'POST',
                    headers: { 'Content-Type': 'application/json' },
                    body: JSON.stringify({ endpoint: subscription.endpoint })
                });
            }
            updateNotifyButton(false);
        } catch (err) {
            console.error('푸시 해제 오류:', err);
        }
    }

    // 알림 버튼 토글
    async function togglePush() {
        const btn = document.getElementById('notify-toggle-btn');
        if (!btn) return;
        if (btn.classList.contains('active')) {
            if (confirm('알림을 해제하시겠습니까?')) {
                await unsubscribePush();
            }
        } else {
            await subscribePush();
        }
    }

    // 테스트 알림 발송
    async function sendTestPush() {
        const res = await fetch('/api/push/test', { method: 'POST' });
        const data = await res.json();
        alert(data.message);
    }

    // 버튼 UI 업데이트 (모든 알림 버튼을 동기화)
    function updateNotifyButton(isActive) {
        const btnIds = [
            'notify-toggle-btn',
            'notify-toggle-btn-ios',
            'notify-toggle-btn-android',
            'notify-toggle-btn-android-sa'
        ];
        btnIds.forEach(id => {
            const btn = document.getElementById(id);
            if (!btn) return;
            if (isActive) {
                btn.classList.add('active');
                btn.textContent = '🔔 알림 ON';
            } else {
                btn.classList.remove('active');
                btn.textContent = '🔕 알림 받기';
            }
        });
    }

    // 기기/브라우저 감지
    function detectPlatform() {
        const ua = navigator.userAgent;
        const isIOS = /iPad|iPhone|iPod/.test(ua) || (navigator.platform === 'MacIntel' && navigator.maxTouchPoints > 1);
        const isAndroid = /Android/.test(ua);
        const isStandalone = window.matchMedia('(display-mode: standalone)').matches || window.navigator.standalone === true;
        const hasPush = 'serviceWorker' in navigator && 'PushManager' in window;
        return { isIOS, isAndroid, isStandalone, hasPush };
    }

    // 서버에서 푸시 구독 상태를 가져와 버튼 반영
    async function syncPushButton() {
        try {
            const res = await fetch('/api/push/status');
            const data = await res.json();
            updateNotifyButton(data.subscribed);
        } catch (e) {
            console.warn('푸시 상태 확인 실패:', e);
        }
    }

    // 초기 상태 체크 + 기기별 적절한 가이드 표시
    async function checkPushStatus() {
        const guide = document.getElementById('notification-guide');
        if (!guide) return;

        const { isIOS, isAndroid, isStandalone, hasPush } = detectPlatform();

        guide.style.display = 'block';

        if (isIOS && !isStandalone) {
            // 아이폰 + Safari 브라우저 (홈화면 추가 전)
            document.getElementById('guide-ios-not-installed').style.display = 'block';

        } else if (isIOS && isStandalone) {
            // 아이폰 + 홈화면 앱으로 접속
            document.getElementById('guide-ios-installed').style.display = 'block';
            if (hasPush) await syncPushButton();

        } else if (isAndroid && isStandalone) {
            // 안드로이드 + 홈화면 앱으로 접속
            document.getElementById('guide-android-standalone').style.display = 'block';
            if (hasPush) await syncPushButton();

        } else if (isAndroid && !isStandalone) {
            // 안드로이드 + 브라우저에서 접속
            document.getElementById('guide-android-browser').style.display = 'block';
            if (hasPush) await syncPushButton();

        } else if (!hasPush) {
            // 알림 미지원 브라우저
            document.getElementById('guide-unsupported').style.display = 'block';

        } else {
            // PC (Chrome, Edge, Firefox 등)
            document.getElementById('guide-pc').style.display = 'block';
            await syncPushButton();
        }
    }

    // 홈화면 추가 안내 (Android / PC)
    let deferredPrompt = null;
    window.addEventListener('beforeinstallprompt', e => {
        e.preventDefault();
        deferredPrompt = e;
        const banner = document.getElementById('pwa-install-banner');
        if (banner) banner.style.display = 'block';
    });

    function installPWA() {
        if (deferredPrompt) {
            deferredPrompt.prompt();
            deferredPrompt.userChoice.then(choice => {
                deferredPrompt = null;
                document.getElementById('pwa-install-banner').style.display = 'none';
            });
        } else {
            const { isIOS } = detectPlatform();
            if (isIOS) {
                alert('Safari 하단의 공유 버튼(📤)을 눌러\\n"홈 화면에 추가"를 선택해주세요!');
            } else {
                alert('브라우저 메뉴에서 "홈 화면에 추가" 또는\\n"앱 설치"를 선택해주세요!');
            }
        }
    }

    // 페이지 로드 후 상태 체크
    document.addEventListener('DOMContentLoaded', checkPushStatus);
    </script>
</body>
</html>
"""

@app.route('/login', methods=['GET', 'POST'])
def login():
    if request.method == 'POST':
        user = User.query.filter_by(username=request.form.get('username')).first()
        if user and check_password_hash(user.password, request.form.get('password')):
            login_user(user)
            # [수정] 강제 전체 구독 로직 삭제 완료
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
        <div style="text-align:center; margin-top:15px; font-size:0.9rem;">
            <a href="{{ url_for('find_id') }}" style="color:#7f8c8d; text-decoration:none;">아이디 찾기</a>
            <span style="color:#ccc; margin:0 5px;">|</span>
            <a href="{{ url_for('reset_password') }}" style="color:#7f8c8d; text-decoration:none;">비밀번호 재설정</a>
        </div>
        <p style="text-align:center; margin-top:20px;"><a href="{{ url_for('register') }}" style="color:#003e21; font-weight:bold;">아직 계정이 없으신가요? 회원가입</a></p>
    </div>
    """
    return render_template_string(HTML_BASE.replace('[[CONTENT]]', content))

@app.route('/find_id', methods=['GET', 'POST'])
def find_id():
    if request.method == 'POST':
        email = request.form.get('email')
        user = User.query.filter_by(email=email).first()
        if user:
            return f"<script>alert('회원님의 아이디는 [{user.username}] 입니다.'); window.location.href='/login';</script>"
        else:
            return "<script>alert('해당 이메일로 가입된 정보가 없습니다.'); history.back();</script>"

    content = """
    <h1 style="border:none;">아이디 찾기</h1>
    <div class="form-container">
        <p style="text-align:center; color:#555; margin-bottom:20px;">가입 시 등록한 이메일을 입력해주세요.</p>
        <form method="POST">
            <input type="email" name="email" placeholder="학교 이메일" required>
            <button type="submit" class="btn">아이디 찾기</button>
        </form>
        <p style="text-align:center; margin-top:20px;"><a href="{{ url_for('login') }}">로그인으로 돌아가기</a></p>
    </div>
    """
    return render_template_string(HTML_BASE.replace('[[CONTENT]]', content))

@app.route('/reset_password', methods=['GET', 'POST'])
def reset_password():
    if request.method == 'POST':
        username = request.form.get('username')
        email = request.form.get('email')
        auth_code = request.form.get('auth_code')
        new_password = request.form.get('new_password')
        new_password_confirm = request.form.get('new_password_confirm')

        record = VerificationCode.query.filter_by(email=email).first()
        if not record or record.code != auth_code:
            return "<script>alert('비정상적인 접근입니다.'); history.back();</script>"
        user = User.query.filter_by(username=username, email=email).first()
        if not user:
            return "<script>alert('입력하신 아이디와 이메일이 일치하는 정보가 없습니다.'); history.back();</script>"
        if new_password != new_password_confirm:
            return "<script>alert('새 비밀번호가 서로 일치하지 않습니다.'); history.back();</script>"
        if not is_valid_password(new_password):
            return "<script>alert('비밀번호는 8자 이상이어야 하며, 특수기호를 최소 1개 이상 포함해야 합니다.'); history.back();</script>"

        user.password = generate_password_hash(new_password, method='scrypt')
        # 인증 기록 삭제
        if record:
            db.session.delete(record) 
        db.session.commit()
        return "<script>alert('비밀번호가 성공적으로 변경되었습니다.'); window.location.href='/login';</script>"

    content = """
    <h1 style="border:none;">비밀번호 재설정</h1>
    <div class="form-container">
        <p style="text-align:center; color:#555; margin-bottom:20px;">아이디와 등록한 이메일을 인증하고,<br>새로운 비밀번호를 설정해주세요.</p>
        <form method="POST" onsubmit="return checkVerified()">
            <input type="text" name="username" placeholder="아이디" required>
            <div style="display: flex; gap: 10px; margin: 10px 0;">
                <input type="email" id="email" name="email" placeholder="학교 이메일 (@skku.edu)" required style="margin:0; flex:1;">
                <button type="button" onclick="sendAuthCode('reset')" class="btn" style="width: auto; padding: 0 15px; background-color: #7f8c8d;">인증번호 받기</button>
            </div>
            <div style="display: flex; gap: 10px; margin: 10px 0;">
                <input type="text" id="auth_code" name="auth_code" placeholder="6자리 인증번호" required style="margin:0; flex:1;">
                <button type="button" onclick="verifyAuthCode()" class="btn" style="width: auto; padding: 0 15px; background-color: #003e21;">인증확인</button>
            </div>
            <div id="auth_status" style="font-size: 0.85rem; margin-bottom: 10px; font-weight: bold;"></div>
            <input type="password" name="new_password" placeholder="새 비밀번호 (8자 이상, 특수기호 포함)" required style="margin-top:20px;">
            <input type="password" name="new_password_confirm" placeholder="새 비밀번호 한 번 더 입력" required>
            <button type="submit" class="btn" style="margin-top:10px;">비밀번호 변경</button>
        </form>
        <p style="text-align:center; margin-top:20px;"><a href="{{ url_for('login') }}">로그인으로 돌아가기</a></p>
    </div>
    <script>
        let isVerified = false;
        function sendAuthCode(action) {
            const emailInput = document.getElementById('email');
            const email = emailInput.value;
            if (!email) { alert('이메일을 먼저 입력해주세요.'); emailInput.focus(); return; }
            alert('인증번호 발송을 요청했습니다. 잠시만 기다려주세요...');
            fetch('/api/send_code', { method: 'POST', headers: { 'Content-Type': 'application/json' }, body: JSON.stringify({ email: email, action: action }) })
            .then(res => res.json()).then(data => { alert(data.message); if(data.success) { isVerified = false; document.getElementById('auth_status').innerText = ''; } })
            .catch(err => { console.error(err); alert('서버 통신 중 오류가 발생했습니다.'); });
        }
        function verifyAuthCode() {
            const email = document.getElementById('email').value;
            const code = document.getElementById('auth_code').value;
            const statusDiv = document.getElementById('auth_status');
            if (!email || !code) { alert('이메일과 인증번호를 모두 입력해주세요.'); return; }
            fetch('/api/verify_code', { method: 'POST', headers: { 'Content-Type': 'application/json' }, body: JSON.stringify({ email: email, code: code }) })
            .then(res => res.json()).then(data => { statusDiv.innerText = data.message; statusDiv.style.color = data.success ? 'green' : 'red'; isVerified = data.success; });
        }
        function checkVerified() { if (!isVerified) { alert('이메일 인증을 먼저 완료해주세요.'); return false; } return true; }
    </script>
    """
    return render_template_string(HTML_BASE.replace('[[CONTENT]]', content))

@app.route('/register', methods=['GET', 'POST'])
def register():
    if request.method == 'POST':
        username = request.form.get('username')
        password = request.form.get('password')
        password_confirm = request.form.get('password_confirm')
        email = request.form.get('email')
        auth_code = request.form.get('auth_code')
        department = request.form.get('department')
        student_id = request.form.get('student_id')
        consent = request.form.get('consent')

        if not consent: return "<script>alert('개인정보 수집 및 이용에 동의해야 가입할 수 있습니다.'); history.back();</script>"
        if not (student_id.isdigit() and len(student_id) == 2): return "<script>alert('학번은 숫자 2자리로 입력해주세요.'); history.back();</script>"
        record = VerificationCode.query.filter_by(email=email).first()
        if not record or record.code != auth_code:
            return "<script>alert('비정상적인 접근입니다.'); history.back();</script>"
        if password != password_confirm: return "<script>alert('비밀번호가 서로 일치하지 않습니다.'); history.back();</script>"
        if not is_valid_password(password): return "<script>alert('비밀번호 정책을 확인해주세요.'); history.back();</script>"
        if User.query.filter_by(username=username).first() or User.query.filter_by(email=email).first(): return "<script>alert('중복된 아이디나 이메일입니다.'); history.back();</script>"

        new_user = User(username=username, password=generate_password_hash(password, method='scrypt'), email=email, department=department, student_id=student_id)
        db.session.add(new_user)
        db.session.commit()
        db.session.add(new_user)
        # 인증 기록 삭제
        if record:
            db.session.delete(record) 
        db.session.commit()
        return "<script>alert('회원가입 성공! 로그인해주세요.'); window.location.href='/login';</script>"

    content = """
    <h1 style="border:none;">회원가입</h1>
    <div class="form-container">
        <form method="POST" onsubmit="return checkVerified()">
            <input type="text" name="username" placeholder="사용할 아이디" required>
            <input type="password" name="password" placeholder="비밀번호 (8자 이상, 특수기호 포함)" required>
            <input type="password" name="password_confirm" placeholder="비밀번호 한 번 더 입력" required>
            <div style="display: flex; gap: 10px; margin: 10px 0;">
                <input type="email" id="email" name="email" placeholder="학교 이메일 (@skku.edu)" required style="margin:0; flex:1;">
                <button type="button" onclick="sendAuthCode('register')" class="btn" style="width: auto; padding: 0 15px; background-color: #7f8c8d;">인증번호 받기</button>
            </div>
            <div style="display: flex; gap: 10px; margin: 10px 0;">
                <input type="text" id="auth_code" name="auth_code" placeholder="6자리 인증번호" required style="margin:0; flex:1;">
                <button type="button" onclick="verifyAuthCode()" class="btn" style="width: auto; padding: 0 15px; background-color: #003e21;">인증확인</button>
            </div>
            <div id="auth_status" style="font-size: 0.85rem; margin-bottom: 10px; font-weight: bold;"></div>
            <input type="text" name="department" placeholder="학과 (예: 소프트웨어학과)" required>
            <input type="text" name="student_id" placeholder="학번 앞 2자리 (예: 26)" maxlength="2" pattern="\\d{2}" required>
            <div style="margin: 15px 0; padding: 15px; background-color: #f8f9fa; border: 1px solid #dee2e6; border-radius: 5px; font-size: 0.85rem; color: #555;">
                <p style="margin: 0 0 10px 0; font-weight: bold; color: #333;">[필수] 개인정보 수집 및 이용 동의</p>
                <ul style="margin: 0; padding-left: 20px; line-height: 1.5;">
                    <li>수집 항목: 아이디, 학교 이메일, 학과, 학번(2자리)</li>
                    <li>수집 목적: 서비스 제공, 본인 확인, 통계 분석</li>
                    <li>보유 기간: <b>회원 탈퇴 시 즉시 파기</b></li>
                </ul>
            </div>
            <label style="display: flex; align-items: center; gap: 8px; margin-bottom: 20px; font-size: 0.9rem; cursor: pointer;">
                <input type="checkbox" name="consent" required style="width: auto; margin: 0; transform: scale(1.2);">
                <b>위 개인정보 수집 및 이용에 동의합니다.</b>
            </label>
            <button type="submit" class="btn">가입하기</button>
        </form>
        <p style="text-align:center; margin-top:20px;"><a href="{{ url_for('login') }}">이미 계정이 있으신가요? 로그인</a></p>
    </div>
    <script>
        let isVerified = false;
        function sendAuthCode(action) {
            const emailInput = document.getElementById('email');
            const email = emailInput.value;
            if (!email) { alert('이메일을 먼저 입력해주세요.'); emailInput.focus(); return; }
            alert('인증번호 발송을 요청했습니다. 잠시만 기다려주세요...');
            fetch('/api/send_code', { method: 'POST', headers: { 'Content-Type': 'application/json' }, body: JSON.stringify({ email: email, action: action }) })
            .then(res => res.json()).then(data => { alert(data.message); if(data.success) { isVerified = false; document.getElementById('auth_status').innerText = ''; } })
            .catch(err => { console.error(err); alert('서버 통신 중 오류가 발생했습니다.'); });
        }
        function verifyAuthCode() {
            const email = document.getElementById('email').value;
            const code = document.getElementById('auth_code').value;
            const statusDiv = document.getElementById('auth_status');
            if (!email || !code) { alert('이메일과 인증번호를 모두 입력해주세요.'); return; }
            fetch('/api/verify_code', { method: 'POST', headers: { 'Content-Type': 'application/json' }, body: JSON.stringify({ email: email, code: code }) })
            .then(res => res.json()).then(data => { statusDiv.innerText = data.message; statusDiv.style.color = data.success ? 'green' : 'red'; isVerified = data.success; });
        }
        function checkVerified() { if (!isVerified) { alert('이메일 인증을 먼저 완료해주세요.'); return false; } return true; }
    </script>
    """
    return render_template_string(HTML_BASE.replace('[[CONTENT]]', content))

@app.route('/logout')
@login_required
def logout():
    logout_user()
    return redirect(url_for('login'))

@app.route('/settings', methods=['GET', 'POST'])
@login_required
def settings():
    # [추가] 당장 크롤링이 안 되는 학과들 숨기기 (이름에 포함된 키워드로 필터링)
    hidden_keywords = [
        '생명과학과','건설환경공학부', '나노공학과', '의과대학','약학대학','바이오신약규제과학과', '명륜학사', '봉룡학사', 
        '창업지원단', '에너지학과', '영상학과', '화학공학부', '반도체융합공학과','반도체시스템공학과','화학과'
    ]
    
    all_boards = Board.query.all()
    # 키워드가 포함되지 않은 게시판만 visible_boards에 담아 화면에 전달
    visible_boards = [
        b for b in all_boards 
        if not any(keyword in b.name for keyword in hidden_keywords)
    ]

    if request.method == 'POST':
        selected_ids = request.form.getlist('boards')
        current_user.subscriptions = [Board.query.get(int(bid)) for bid in selected_ids]
        db.session.commit()
        return redirect(url_for('home'))

    content = """
    <h1>공지사항 선택📝</h1>
    
    <div style="background-color: #fff3cd; color: #856404; padding: 15px; border-radius: 8px; margin-bottom: 20px; border: 1px solid #ffeeba; font-size: 0.9rem; line-height: 1.5;">
        <strong>🚨 안내:</strong> 현재 베타(Beta) 버전이며, 일부 학과는 시스템 연동 중입니다.<br>
        (목록에 보이지 않는 학과는 안정화 후 순차적으로 추가될 예정입니다.)
    </div>

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
    
    <script>
        if (!sessionStorage.getItem('betaAlertShown')) {
            alert('현재 베타(Beta) 버전이며, 일부 학과는 시스템 연동 중입니다.');
            sessionStorage.setItem('betaAlertShown', 'true');
        }
    </script>
    """
    return render_template_string(HTML_BASE.replace('[[CONTENT]]', content), boards=visible_boards)
@app.route('/')
@login_required
def home():
    if not current_user.subscriptions:
        return redirect(url_for('settings'))

    sorted_boards = sorted(current_user.subscriptions, key=lambda board: board.id)

    # ★ 핵심 변경: DB 캐시에서 읽어옴 (크롤링 X)
    all_notices = []
    for board in sorted_boards:
        all_notices.extend(get_cached_notices(board.board_id, page=1))

    # 마지막 크롤링 시각 표시용
    latest_crawl = CrawlStatus.query.order_by(CrawlStatus.last_crawled.desc()).first()
    if latest_crawl and latest_crawl.last_crawled:
        # DB에 저장된 UTC 시간에 9시간을 더해 한국 시간(KST)으로 변환
        kst_time = latest_crawl.last_crawled + timedelta(hours=9)
        last_updated = kst_time.strftime('%Y-%m-%d %H:%M')
    else:
        last_updated = '업데이트 전'

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
    </style>

    <h1>{{ current_user.username }}'s 실시간 공지🔊</h1>

    <!-- PWA 설치 + 알림 안내 통합 배너 -->
    <div id="notification-guide" style="display:none; background:#f8f9fa; border:1px solid #dee2e6; border-radius:12px; padding:18px; margin-bottom:18px; font-size:0.88rem; line-height:1.6;">

        <!-- ===== PC 사용자용 ===== -->
        <div id="guide-pc" style="display:none;">
            <div style="display:flex; align-items:center; justify-content:space-between; flex-wrap:wrap; gap:10px;">
                <div>
                    <b>🔔 새 공지 알림을 받아보세요!</b><br>
                    <span style="color:#666;">구독한 게시판에 새 공지가 올라오면 자동으로 알려드려요.</span>
                </div>
                <div style="display:flex; gap:8px; align-items:center;">
                    <button id="notify-toggle-btn" class="notify-btn" onclick="togglePush()">🔕 알림 받기</button>
                    <button class="notify-btn" onclick="sendTestPush()" style="font-size:0.8rem; padding:5px 10px;" title="알림이 잘 오는지 테스트">테스트</button>
                </div>
            </div>
            <p style="margin:10px 0 0; color:#888; font-size:0.8rem;">💻 Chrome, Edge, Firefox에서 사용 가능해요.</p>
        </div>

        <!-- ===== 안드로이드 사용자용 (브라우저에서 접속) ===== -->
        <div id="guide-android-browser" style="display:none;">
            <b>📱 안드로이드에서 알림 받는 법</b>
            <div style="background:white; border-radius:8px; padding:14px; margin-top:10px; border:1px solid #eee;">
                <div style="margin-bottom:8px;"><b>Step 1.</b> 아래 <b>"🔕 알림 받기"</b> 버튼을 눌러주세요</div>
                <div style="margin-bottom:8px;"><b>Step 2.</b> <b>"알림 허용"</b> 팝업이 뜨면 허용을 눌러주세요</div>
                <div><b>Step 3.</b> 끝! 새 공지가 올라오면 자동으로 알림이 와요 🎉</div>
            </div>
            <div style="display:flex; gap:8px; align-items:center; margin-top:12px;">
                <button id="notify-toggle-btn-android" class="notify-btn" onclick="togglePush()">🔕 알림 받기</button>
                <button class="notify-btn" onclick="sendTestPush()" style="font-size:0.8rem; padding:5px 10px;">테스트</button>
            </div>
            <div style="margin-top:12px; padding:10px; background:#eef6ff; border-radius:6px; font-size:0.82rem; color:#555;">
                <b>💡 팁:</b> 홈 화면에 추가하면 앱처럼 바로 접속할 수 있어요!<br>
                Chrome: 우측 상단 <b>⋮</b> 메뉴 → <b>"홈 화면에 추가"</b> 또는 <b>"앱 설치"</b>
            </div>
            <p style="margin:8px 0 0; color:#888; font-size:0.8rem;">✅ Chrome, Edge, Samsung Internet, Firefox 등 대부분 브라우저에서 지원돼요.</p>
        </div>

        <!-- ===== 안드로이드 사용자용 (홈화면 앱에서 접속) ===== -->
        <div id="guide-android-standalone" style="display:none;">
            <div style="display:flex; align-items:center; justify-content:space-between; flex-wrap:wrap; gap:10px;">
                <div>
                    <b>🎉 앱으로 접속 중이에요!</b><br>
                    <span style="color:#666;">알림 버튼을 눌러 새 공지 알림을 받아보세요.</span>
                </div>
                <div style="display:flex; gap:8px; align-items:center;">
                    <button id="notify-toggle-btn-android-sa" class="notify-btn" onclick="togglePush()">🔕 알림 받기</button>
                    <button class="notify-btn" onclick="sendTestPush()" style="font-size:0.8rem; padding:5px 10px;">테스트</button>
                </div>
            </div>
            <p style="margin:8px 0 0; color:#888; font-size:0.8rem;">
                ⚠️ 알림이 안 오면: 설정 → 앱 → 배터리 최적화에서 브라우저를 <b>"제한 없음"</b>으로 변경해주세요.
            </p>
        </div>

        <!-- ===== iOS 사용자용 (Safari 브라우저에서 접속, 홈화면 추가 전) ===== -->
        <div id="guide-ios-not-installed" style="display:none;">
            <b>🍎 아이폰에서 알림을 받으려면?</b>
            <div style="background:white; border-radius:8px; padding:14px; margin-top:10px; border:1px solid #eee;">
                <div style="margin-bottom:8px;"><b>Step 1.</b> 반드시 <b>Safari</b>로 이 페이지에 접속해주세요</div>
                <div style="margin-bottom:8px;"><b>Step 2.</b> 하단의 <b>공유 버튼</b>(📤)을 탭하세요</div>
                <div style="margin-bottom:8px;"><b>Step 3.</b> <b>"홈 화면에 추가"</b>를 선택하세요</div>
                <div><b>Step 4.</b> 홈 화면에서 앱을 열고 알림 버튼을 눌러주세요!</div>
            </div>
            <p style="margin:10px 0 0; color:#e67e22; font-size:0.82rem;">
                ⚠️ iOS 16.4 이상 + Safari에서 홈화면에 추가해야만 알림을 받을 수 있어요.<br>
                ❌ Chrome, Edge 등 다른 브라우저에서는 홈화면에 추가해도 알림이 작동하지 않아요.
            </p>
        </div>

        <!-- ===== iOS 사용자용 (홈화면 앱에서 접속 = standalone 모드) ===== -->
        <div id="guide-ios-installed" style="display:none;">
            <div style="display:flex; align-items:center; justify-content:space-between; flex-wrap:wrap; gap:10px;">
                <div>
                    <b>🎉 홈 화면 앱으로 접속 중이에요!</b><br>
                    <span style="color:#666;">아래 버튼을 눌러 알림을 활성화하세요.</span>
                </div>
                <div style="display:flex; gap:8px; align-items:center;">
                    <button id="notify-toggle-btn-ios" class="notify-btn" onclick="togglePush()">🔕 알림 받기</button>
                    <button class="notify-btn" onclick="sendTestPush()" style="font-size:0.8rem; padding:5px 10px;">테스트</button>
                </div>
            </div>
            <p style="margin:8px 0 0; color:#888; font-size:0.8rem;">
                알림 설정은 아이폰 <b>설정 → 알림</b>에서도 관리할 수 있어요.
            </p>
        </div>

        <!-- ===== 알림 미지원 브라우저 ===== -->
        <div id="guide-unsupported" style="display:none;">
            <b>😢 이 브라우저에서는 알림을 지원하지 않아요</b>
            <p style="margin:8px 0 0; color:#666;">
                <b>안드로이드:</b> Chrome, Edge, Samsung Internet 등을 사용해주세요.<br>
                <b>아이폰:</b> Safari로 접속 후 홈화면에 추가해주세요.<br>
                <b>PC:</b> Chrome 또는 Edge를 사용해주세요.
            </p>
        </div>

        <!-- 닫기 버튼 -->
        <div style="text-align:right; margin-top:8px;">
            <button onclick="this.closest('#notification-guide').style.display='none'" style="background:none; border:none; color:#aaa; cursor:pointer; font-size:0.8rem;">닫기 ✕</button>
        </div>
    </div>

    <!-- Android/PC용 홈화면 설치 안내 (beforeinstallprompt 지원 시에만 표시) -->
    <div id="pwa-install-banner" class="pwa-install-banner" onclick="installPWA()">
        📲 홈 화면에 추가하면 앱처럼 편리하게 사용할 수 있어요! (탭하여 설치)
    </div>

    <p class="crawl-info" style="color: #7f8c8d; font-size: 0.85rem; text-align: center; margin-bottom: 20px;">마지막 업데이트: {{ last_updated }}</p>

    <div class="btn-group">
        <button class="filter-btn active" onclick="filterNotices('all', this)">전체보기</button>
        {% for board in boards %}
            <button class="filter-btn" onclick="filterNotices('{{ board.board_id }}', this)">{{ board.name }}</button>
        {% endfor %}
    </div>

    <ul id="notice-list">
        {% for notice in notices %}
            <li class="notice-item {{ notice.category_id }}">
                <div class="notice-row-tag">
                    <span class="tag tag-{{ notice.category_id }}">{{ notice.category_name }}</span>
                    {% if notice.is_new %}<span class="badge-new">NEW</span>{% endif %}
                </div>
                <div class="notice-row-title">
                    <a href="{{ notice.link }}" target="_blank">{{ notice.title }}</a>
                </div>
                <div class="notice-row-meta">
                    <div class="meta-info">
                        {% if notice.views %}
                            <span>👁️ {{ notice.views }}</span> <span style="color:#ddd;">|</span>
                        {% endif %}
                        <span>{{ notice.date }}</span>
                    </div>
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
                item.style.display = (categoryId === 'all' || item.classList.contains(categoryId)) ? 'block' : 'none';
            });
            document.getElementById('load-more-container').style.display = categoryId === 'all' ? 'none' : 'block';
        }

        function loadMore() {
            if (currentCategory === 'all') return;
            const btn = document.getElementById('load-more-btn');
            btn.innerText = '불러오는 중... ⏳';
            btn.disabled = true;
            pages[currentCategory]++;

            fetch(`/api/notices?board_id=${currentCategory}&page=${pages[currentCategory]}`)
                .then(response => response.json())
                .then(data => {
                    const list = document.getElementById('notice-list');
                    // 기존 공지의 제목+링크 조합으로 중복 체크 (link만으로는 부정확)
                    const existingKeys = new Set(
                        Array.from(document.querySelectorAll('.notice-item')).map(li => {
                            const a = li.querySelector('a');
                            return a ? a.textContent.trim() + '|' + a.href : '';
                        })
                    );
                    let newCount = 0;
                    data.forEach(notice => {
                        const key = notice.title + '|' + notice.link;
                        if (existingKeys.has(key)) return;
                        existingKeys.add(key);
                        const li = document.createElement('li');
                        li.className = `notice-item ${notice.category_id}`;
                        const newBadge = notice.is_new ? ` <span class="badge-new">NEW</span>` : '';
                        const viewsHtml = notice.views ? `<span>👁️ ${notice.views}</span> <span style="color:#ddd;">|</span> ` : '';
                        li.innerHTML = `
                            <div class="notice-row-tag">
                                <span class="tag tag-${notice.category_id}">${notice.category_name}</span>
                                ${newBadge}
                            </div>
                            <div class="notice-row-title">
                                <a href="${notice.link}" target="_blank">${notice.title}</a>
                            </div>
                            <div class="notice-row-meta">
                                <div class="meta-info">${viewsHtml}<span>${notice.date}</span></div>
                            </div>
                        `;
                        list.appendChild(li);
                        newCount++;
                    });
                    if (newCount === 0) alert('더 이상 불러올 새로운 공지사항이 없거나 마지막 페이지입니다.');
                })
                .catch(error => { console.error(error); alert('데이터를 불러오는데 실패했습니다.'); pages[currentCategory]--; })
                .finally(() => { btn.innerText = '더보기 🔽'; btn.disabled = false; });
        }
    </script>
    """
    return render_template_string(
        HTML_BASE.replace('[[CONTENT]]', content),
        notices=all_notices, boards=sorted_boards, last_updated=last_updated
    )

@app.route('/api/notices')
@login_required
def api_get_notices():
    board_id = request.args.get('board_id')
    page = int(request.args.get('page', 1))

    # page>1은 "더보기" 요청이므로 실시간 크롤링
    if page > 1:
        board = Board.query.filter_by(board_id=board_id).first()
        if not board:
            return jsonify([])
        target_url = build_page_url(board.url, board.page_param, page)
        logger.info(f"[더보기] {board.name} page={page} → {target_url}")
        notices = scrape_single_site(board, page=page)
        logger.info(f"[더보기] {board.name} page={page} → {len(notices)}건 수집")
        return jsonify(notices)

    # page==1은 캐시에서
    notices = get_cached_notices(board_id, page=1)
    return jsonify(notices)

# ==========================================
# 관리자 대시보드 (크롤링 상태 추가)
# ==========================================
@app.route('/admin')
@login_required
def admin_dashboard():
    if current_user.username != 'admin환휘':
        return "<script>alert('관리자만 접근할 수 있는 페이지입니다.'); history.back();</script>"

    total_users = User.query.count()
    dept_stats = db.session.query(User.department, db.func.count(User.id))\
        .group_by(User.department).order_by(db.func.count(User.id).desc()).all()
    sid_stats = db.session.query(User.student_id, db.func.count(User.id))\
        .group_by(User.student_id).order_by(db.func.count(User.id).desc()).all()

    # 크롤링 상태 정보
    crawl_statuses = CrawlStatus.query.order_by(CrawlStatus.last_crawled.desc()).all()
    total_cached = CachedNotice.query.count()

    content = """
    <h1 style="border:none; color:#003e21;">📊 관리자 통계 대시보드</h1>

    <div style="display:flex; gap:20px; justify-content:center; margin-bottom:30px; flex-wrap:wrap;">
        <div style="background:#f8f9fa; padding:20px; border-radius:10px; text-align:center; flex:1; min-width:150px; border:1px solid #dee2e6;">
            <h3 style="margin-top:0;">총 가입자 수</h3>
            <p style="font-size:28px; font-weight:bold; color:#003e21; margin:0;">{{ total }}명</p>
        </div>
        <div style="background:#f8f9fa; padding:20px; border-radius:10px; text-align:center; flex:1; min-width:150px; border:1px solid #dee2e6;">
            <h3 style="margin-top:0;">캐시된 공지 수</h3>
            <p style="font-size:28px; font-weight:bold; color:#003e21; margin:0;">{{ total_cached }}건</p>
        </div>
        <div style="background:#f8f9fa; padding:20px; border-radius:10px; text-align:center; flex:1; min-width:150px; border:1px solid #dee2e6;">
            <h3 style="margin-top:0;">크롤링 주기</h3>
            <p style="font-size:28px; font-weight:bold; color:#003e21; margin:0;">{{ interval }}분</p>
        </div>
    </div>

    <div style="display:flex; gap:20px; flex-wrap:wrap;">
        <div style="flex:1; min-width:300px; background:white; padding:20px; border-radius:10px; border:1px solid #eee;">
            <h3 style="border-bottom:2px solid #003e21; padding-bottom:10px; margin-top:0;">🏢 학과별 분포</h3>
            <ul style="padding:0; list-style:none;">
                {% for dept, count in dept_stats %}
                <li style="display:flex; justify-content:space-between; padding:10px 0; border-bottom:1px solid #f1f1f1;">
                    <span>{{ dept }}</span>
                    <span style="font-weight:bold; color:#003e21;">{{ count }}명</span>
                </li>
                {% else %}
                <li style="text-align:center; color:#999; padding:10px;">데이터가 없습니다.</li>
                {% endfor %}
            </ul>
        </div>
        <div style="flex:1; min-width:300px; background:white; padding:20px; border-radius:10px; border:1px solid #eee;">
            <h3 style="border-bottom:2px solid #003e21; padding-bottom:10px; margin-top:0;">🎓 학번별 분포</h3>
            <ul style="padding:0; list-style:none;">
                {% for sid, count in sid_stats %}
                <li style="display:flex; justify-content:space-between; padding:10px 0; border-bottom:1px solid #f1f1f1;">
                    <span>{{ sid }}학번</span>
                    <span style="font-weight:bold; color:#003e21;">{{ count }}명</span>
                </li>
                {% else %}
                <li style="text-align:center; color:#999; padding:10px;">데이터가 없습니다.</li>
                {% endfor %}
            </ul>
        </div>
    </div>

    <div style="margin-top:30px; background:white; padding:20px; border-radius:10px; border:1px solid #eee;">
        <h3 style="border-bottom:2px solid #003e21; padding-bottom:10px; margin-top:0;">🔄 크롤링 상태</h3>
        <div style="overflow-x:auto;">
        <table style="width:100%; border-collapse:collapse; font-size:0.85rem;">
            <tr style="background:#f8f9fa;">
                <th style="padding:8px; text-align:left;">게시판</th>
                <th style="padding:8px;">마지막 크롤링</th>
                <th style="padding:8px;">상태</th>
                <th style="padding:8px;">건수</th>
            </tr>
            {% for cs in crawl_statuses %}
            <tr style="border-bottom:1px solid #eee;">
                <td style="padding:8px;">{{ cs.board_id }}</td>
                <td style="padding:8px; text-align:center;">{{ cs.last_crawled.strftime('%m/%d %H:%M') if cs.last_crawled else '-' }}</td>
                <td style="padding:8px; text-align:center;">{{ '✅' if cs.last_success else '❌ (' + (cs.error_count|string) + '연속 실패)' }}</td>
                <td style="padding:8px; text-align:center;">{{ cs.notice_count }}</td>
            </tr>
            {% else %}
            <tr><td colspan="4" style="text-align:center; color:#999; padding:15px;">아직 크롤링 기록이 없습니다.</td></tr>
            {% endfor %}
        </table>
        </div>
    </div>

    <div style="text-align:center; margin-top:30px;">
        <a href="{{ url_for('trigger_crawl') }}" class="btn" style="text-decoration:none; display:inline-block; width:auto; padding:10px 30px; margin-right:10px; background-color:#e67e22; border-radius:20px; color:white; font-weight:bold;">🔄 수동 크롤링 실행</a>
        <a href="{{ url_for('home') }}" class="btn" style="text-decoration:none; display:inline-block; width:auto; padding:10px 40px; border-radius:20px;">메인으로 돌아가기</a>
    </div>
    """
    return render_template_string(
        HTML_BASE.replace('[[CONTENT]]', content),
        total=total_users, dept_stats=dept_stats, sid_stats=sid_stats,
        crawl_statuses=crawl_statuses, total_cached=total_cached,
        interval=CRAWL_INTERVAL_SECONDS // 60
    )

# [신규] 관리자 수동 크롤링 트리거
@app.route('/admin/trigger_crawl')
@login_required
def trigger_crawl():
    if current_user.username != 'admin환휘':
        return "<script>alert('관리자만 접근할 수 있습니다.'); history.back();</script>"
    # 별도 스레드에서 즉시 실행
    threading.Thread(target=run_scheduled_crawl, daemon=True).start()
    return "<script>alert('크롤링이 백그라운드에서 시작되었습니다. 잠시 후 새로고침해주세요.'); window.location.href='/admin';</script>"

# ==========================================
# PWA: manifest.json / service-worker / push 구독 API
# ==========================================

@app.route('/manifest.json')
def manifest():
    manifest_data = {
        "name": "성대 공지 알리미",
        "short_name": "성대공지",
        "description": "성균관대학교 맞춤형 공지사항 알림 서비스",
        "start_url": "/",
        "display": "standalone",
        "background_color": "#f4f7f6",
        "theme_color": "#003e21",
        "orientation": "portrait-primary",
        "icons": [
            {"src": "/static/icon-72.png", "sizes": "72x72", "type": "image/png"},
            {"src": "/static/icon-96.png", "sizes": "96x96", "type": "image/png"},
            {"src": "/static/icon-128.png", "sizes": "128x128", "type": "image/png"},
            {"src": "/static/icon-144.png", "sizes": "144x144", "type": "image/png"},
            {"src": "/static/icon-152.png", "sizes": "152x152", "type": "image/png"},
            {"src": "/static/icon-192.png", "sizes": "192x192", "type": "image/png", "purpose": "any maskable"},
            {"src": "/static/icon-384.png", "sizes": "384x384", "type": "image/png"},
            {"src": "/static/icon-512.png", "sizes": "512x512", "type": "image/png", "purpose": "any maskable"}
        ]
    }
    response = make_response(json.dumps(manifest_data, ensure_ascii=False))
    response.headers['Content-Type'] = 'application/manifest+json'
    return response


@app.route('/sw.js')
def service_worker():
    sw_code = """
// 성대 공지 알리미 Service Worker
const CACHE_NAME = 'skku-notice-v1';
const OFFLINE_URL = '/offline';

// 설치: 기본 리소스 캐시
self.addEventListener('install', event => {
    self.skipWaiting();
});

// 활성화: 이전 캐시 정리
self.addEventListener('activate', event => {
    event.waitUntil(clients.claim());
});

// 푸시 알림 수신
self.addEventListener('push', event => {
    let data = { title: '성대 공지 알리미', body: '새 공지사항이 등록되었습니다.' };

    if (event.data) {
        try {
            data = event.data.json();
        } catch (e) {
            data.body = event.data.text();
        }
    }

    const options = {
        body: data.body || '',
        icon: data.icon || '/static/icon-192.png',
        badge: data.badge || '/static/icon-72.png',
        vibrate: [200, 100, 200],
        data: { url: data.url || '/' },
        actions: [
            { action: 'open', title: '확인하기' },
            { action: 'close', title: '닫기' }
        ],
        tag: 'skku-notice',
        renotify: true
    };

    event.waitUntil(
        self.registration.showNotification(data.title || '성대 공지 알리미', options)
    );
});

// 알림 클릭 시 해당 페이지로 이동
self.addEventListener('notificationclick', event => {
    event.notification.close();

    const targetUrl = event.notification.data?.url || '/';

    if (event.action === 'close') return;

    event.waitUntil(
        clients.matchAll({ type: 'window', includeUncontrolled: true }).then(clientList => {
            // 이미 열려 있는 탭이 있으면 포커스
            for (const client of clientList) {
                if (client.url.includes(self.location.origin) && 'focus' in client) {
                    client.navigate(targetUrl);
                    return client.focus();
                }
            }
            // 없으면 새 탭 열기
            return clients.openWindow(targetUrl);
        })
    );
});
"""
    response = make_response(sw_code)
    response.headers['Content-Type'] = 'application/javascript'
    response.headers['Service-Worker-Allowed'] = '/'
    return response


@app.route('/api/vapid-public-key')
def vapid_public_key():
    """프론트엔드에서 VAPID 공개키를 가져가는 API"""
    return jsonify({'publicKey': VAPID_PUBLIC_KEY})


@app.route('/api/push/subscribe', methods=['POST'])
@login_required
def push_subscribe():
    """브라우저 Push 구독 정보를 서버에 저장"""
    data = request.get_json()
    subscription = data.get('subscription')
    if not subscription:
        return jsonify({'success': False, 'message': '구독 정보가 없습니다.'}), 400

    sub_json = json.dumps(subscription, ensure_ascii=False)

    # 중복 방지: 같은 유저 + 같은 endpoint면 업데이트
    existing = PushSubscription.query.filter_by(user_id=current_user.id).all()
    for ex in existing:
        try:
            ex_data = json.loads(ex.subscription_json)
            if ex_data.get('endpoint') == subscription.get('endpoint'):
                ex.subscription_json = sub_json
                ex.is_active = True
                ex.created_at = datetime.utcnow()
                db.session.commit()
                return jsonify({'success': True, 'message': '알림이 갱신되었습니다.'})
        except (json.JSONDecodeError, AttributeError):
            continue

    new_sub = PushSubscription(
        user_id=current_user.id,
        subscription_json=sub_json,
        is_active=True
    )
    db.session.add(new_sub)
    db.session.commit()
    return jsonify({'success': True, 'message': '알림이 활성화되었습니다! 🔔'})


@app.route('/api/push/unsubscribe', methods=['POST'])
@login_required
def push_unsubscribe():
    """푸시 알림 구독 해제"""
    data = request.get_json()
    endpoint = data.get('endpoint', '')

    subs = PushSubscription.query.filter_by(user_id=current_user.id, is_active=True).all()
    count = 0
    for sub in subs:
        try:
            sub_data = json.loads(sub.subscription_json)
            if not endpoint or sub_data.get('endpoint') == endpoint:
                sub.is_active = False
                count += 1
        except (json.JSONDecodeError, AttributeError):
            sub.is_active = False
            count += 1
    db.session.commit()
    return jsonify({'success': True, 'message': f'알림이 해제되었습니다. ({count}건)'})


@app.route('/api/push/status')
@login_required
def push_status():
    """현재 유저의 푸시 구독 상태 확인"""
    active_count = PushSubscription.query.filter_by(
        user_id=current_user.id, is_active=True
    ).count()
    return jsonify({'subscribed': active_count > 0, 'count': active_count})


@app.route('/api/push/test', methods=['POST'])
@login_required
def push_test():
    """푸시 알림 테스트 발송"""
    subs = PushSubscription.query.filter_by(
        user_id=current_user.id, is_active=True
    ).all()

    if not subs:
        return jsonify({'success': False, 'message': '활성화된 알림 구독이 없습니다.'})

    success_count = 0
    for sub in subs:
        if send_push_notification(
            sub.subscription_json,
            title="🔔 테스트 알림",
            body="성대 공지 알리미 푸시 알림이 정상 작동 중입니다!",
            url_link="/"
        ):
            success_count += 1

    return jsonify({
        'success': success_count > 0,
        'message': f'테스트 알림 발송 완료 ({success_count}/{len(subs)})'
    })

# ==========================================
# 이메일 인증 API (기존 유지)
# ==========================================
@app.route('/api/send_code', methods=['POST'])
def send_code():
    data = request.get_json()
    email = data.get('email')
    action = data.get('action', 'register')

    if not email.endswith(('@skku.edu', '@g.skku.edu')):
        return jsonify({'success': False, 'message': '성균관대학교 이메일(@skku.edu 또는 @g.skku.edu)만 사용 가능합니다.'})

    user_exists = User.query.filter_by(email=email).first()
    if action == 'register' and user_exists:
        return jsonify({'success': False, 'message': '이미 가입된 이메일입니다.'})
    elif action == 'reset' and not user_exists:
        return jsonify({'success': False, 'message': '가입되지 않은 이메일입니다.'})

    code = str(random.randint(100000, 999999))
    
    # --- 수정된 부분: DB에 인증번호 저장 ---
    record = VerificationCode.query.filter_by(email=email).first()
    if record:
        record.code = code
        record.created_at = datetime.utcnow()
    else:
        record = VerificationCode(email=email, code=code)
        db.session.add(record)
    db.session.commit()
    # ------------------------------------

    SENDER_EMAIL = os.getenv('SENDER_EMAIL')
    APP_PASSWORD = os.getenv('APP_PASSWORD')

    try:
        msg = MIMEText(f"성대 공지사항 알리미 인증번호는 [{code}] 입니다.")
        msg['Subject'] = '[성대 공지 알리미] 이메일 인증번호 안내'
        msg['From'] = SENDER_EMAIL
        msg['To'] = email
        server = smtplib.SMTP('smtp.gmail.com', 587)
        server.starttls()
        server.login(SENDER_EMAIL, APP_PASSWORD)
        server.send_message(msg)
        server.quit()
        return jsonify({'success': True, 'message': '인증번호가 발송되었습니다. 이메일함을 확인해주세요.'})
    except Exception as e:
        logger.error(f"이메일 전송 오류: {e}")
        return jsonify({'success': False, 'message': '이메일 발송에 실패했습니다.'})

@app.route('/api/verify_code', methods=['POST'])
def verify_code():
    data = request.get_json()
    email = data.get('email')
    code = data.get('code')
    
    # --- 수정된 부분: DB에서 인증번호 확인 ---
    record = VerificationCode.query.filter_by(email=email).first()
    
    # (선택사항) 인증 만료 시간 체크를 넣고 싶다면 아래 주석을 활용하세요.
    # if record and (datetime.utcnow() - record.created_at) > timedelta(minutes=5):
    #     return jsonify({'success': False, 'message': '❌ 인증 시간이 만료되었습니다. 다시 시도해주세요.'})

    if record and record.code == code:
        return jsonify({'success': True, 'message': '✅ 인증이 완료되었습니다.'})
    else:
        return jsonify({'success': False, 'message': '❌ 인증번호가 일치하지 않거나 만료되었습니다.'})


# ==========================================
# DB 초기화 및 크롤링 스케줄러 시작
# (gunicorn 등 외부 WSGI 서버에서도 실행되도록 모듈 레벨에서 수행)
# ==========================================
with app.app_context():
    init_db()

# 스케줄러 중복 실행 방지 플래그
_scheduler_started = False

def start_scheduler():
    global _scheduler_started
    if _scheduler_started:
        return
    _scheduler_started = True
    crawler_thread = threading.Thread(target=scheduler_loop, daemon=True)
    crawler_thread.start()
    logger.info("🚀 크롤링 스케줄러 스레드가 시작되었습니다.")

# gunicorn, python app.py 모두에서 스케줄러 시작
start_scheduler()

if __name__ == '__main__':
    app.run(debug=False, use_reloader=False)