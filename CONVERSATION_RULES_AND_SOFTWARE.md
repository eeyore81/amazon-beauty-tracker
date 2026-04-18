# Amazon Bestseller Tracker — Rules and Software Stack

## 1. Conversation Rules

- 사용자 요청에 따라 기능을 구현할 때는 먼저 변경할 파일과 기존 동작을 점검한다.
- Telegram 명령 처리에서 `summary`와 `update`는 즉시 실행되어야 한다.
- `/summary`는 백그라운드 업데이트를 예약하지 않고, 필요한 경우 즉시 데이터를 수집해 결과를 반환해야 한다.
- `bestsellers.json`은 서버에서 테스트용으로 생성될 수 있지만, 배포/병합 시에는 Git에 포함되지 않아야 한다.
- `data/bestsellers.json`은 실제 결과를 보기 위해 서버에서 다시 생성하도록 유지하되, 저장소에 커밋하지 않는다.
- `requirements.txt`는 필요한 패키지를 명시하며, 새 기능 추가 시 반드시 업데이트해야 한다.
- 코드 변경 후에는 `python -m py_compile main.py`로 문법 검사를 반드시 수행한다.
- 사용자 요구에 대해 답변은 간결하고 명확하게 제공한다.

## 2. Software Stack

- Python 3
- Flask: Telegram webhook 서버 및 간단한 HTTP 헬스 체크
- requests: HTTP 요청 및 Telegram API 호출
- BeautifulSoup + lxml: Amazon HTML 파싱
- Playwright: 브라우저 자동화 기반 동적 페이지 스크롤링 및 콘텐츠 수집
- Pillow: Telegram으로 전송할 카드형 이미지 생성
- APScheduler: 주기 업데이트 예약(현재 `start_auto_update`는 유지되지만, 명령 자체는 즉시 실행)

## 3. 주요 기능 요구 사항

- Amazon 베스트셀러 스크래핑
  - 기본적으로 `requests` + `BeautifulSoup`로 처리
  - 동적 로딩이 필요한 경우 `Playwright` 헤드리스 브라우저 사용
  - 최대 100개 아이템을 가져옴
  - 페이징이 필요한 경우, 2페이지까지 로드하고 페이지 내부 스크롤을 수행하여 추가 데이터를 확보
- Telegram 명령
  - `/start` 또는 `시작`: 채팅 등록 및 초기 데이터 수집
  - `/add <브랜드>` 또는 `추가 <브랜드>`: 브랜드 추가
  - `/remove <브랜드>` 또는 `삭제 <브랜드>`: 브랜드 제거
  - `/update` 또는 `업데이트`: 즉시 크롤링 및 업데이트, 카드 이미지 전송
  - `/summary` 또는 `요약`: 현재 요약 카드 이미지 전송
  - `/list` 또는 `목록`: 추적 브랜드 목록
  - `/help` 또는 `도움`: 도움말
- Telegram 알림
  - 일반 텍스트 메시지는 `sendMessage` 사용
  - 카드 이미지는 `sendPhoto`로 전송
  - 이미지 생성 실패 시 텍스트 폴백 지원

## 4. 카드 이미지 디자인 정책

- `Pillow`를 사용하여 PNG 카드 이미지 생성
- 영문 텍스트로 표시
- 타이틀과 메타정보는 더 크게 표시
- 제품명은 최대 약 20자 내외로 잘라서 보여줌
- 이미지가 없는 경우 `No Image` 자리 표시자 표시
- 업데이트 요약과 현재 요약 모두 이미지 카드 형태로 전송

## 5. 저장 및 상태 관리

- `config.json`: URL, 데이터 파일 경로, 업데이트 간격, browser automation 설정
- `data/state.json`: 추적 브랜드와 Telegram chat_id 저장
- `data/bestsellers_prev.json`: 이전 수집 결과 백업
- `data/bestsellers.json`: 현재 수집 결과 저장(배포에는 제외)

## 6. 배포 및 CI 고려 사항

- CI에서 `--use-browser` 옵션으로 Playwright 모드를 활성화할 수 있다.
- 배포 시 `main.py`가 즉시 동작하도록 유지하며, 명령 호출 시 블로킹 방식으로 결과 전송
- 백그라운드 업데이트 스레드 방식은 제거하거나 최소화

## 7. 현재 작업 요약

- `main.py`에 Telegram 카드 이미지 전송 기능 추가
- `fetch_bestsellers_with_browser()`에서 2페이지 스크롤 처리 강화
- `build_summary_image()` 및 `build_update_image()` 구현
- `requirements.txt`에 `Pillow` 추가
- `data/bestsellers.json`은 커밋에서 제거함
