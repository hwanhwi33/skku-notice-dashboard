import requests
from bs4 import BeautifulSoup

url = "https://www.skku.edu/skku/campus/skk_comm/notice01.do"
headers = {
    'User-Agent': 'Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36'
}

try:
    response = requests.get(url, headers=headers)
    response.encoding = 'utf-8'
    soup = BeautifulSoup(response.text, 'html.parser')

    # 방법 1: 기존 방식 (td 안의 a 태그)
    titles = soup.select('td.jwxe_tl a')
    
    # 방법 2: 방법 1이 안 될 경우 대비 (공지사항 제목에 흔히 쓰이는 클래스 'title')
    if not titles:
        titles = soup.select('.title a') or soup.select('dt a')

    if not titles:
        print("💡 힌트를 찾았습니다! 응답은 성공(200)했으나 제목 위치가 다릅니다.")
        # 실제 사이트의 일부 내용을 출력해 위치를 파악해봅니다.
        print("-" * 30)
        print(response.text[:500]) # 사이트 소스 앞부분 출력
    else:
        print(f"🎉 드디어 찾았습니다! 총 {len(titles)}개의 공지사항 발견!\n")
        for i, title in enumerate(titles[:10]):
            clean_title = title.text.strip()
            if clean_title: # 빈 글자가 아닌 경우만 출력
                print(f"[{i+1}] {clean_title}")

except Exception as e:
    print(f"오류 발생: {e}")