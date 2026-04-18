# Amazon Beauty Bestseller Tracker

Telegram 명령으로 Amazon Beauty 베스트셀러를 수집하고, 추적 브랜드 요약/업데이트를 이미지 카드로 전송하는 Python 서비스입니다.

## 핵심 기능

- Amazon Beauty 1, 2페이지 기준 최대 100개 수집
- Telegram 명령 지원: /start, /add, /remove, /update, /summary, /list
- `/summary`, `/update` 결과를 모바일 친화 이미지 카드로 전송
- CI 모드 지원 (`--update-now`, `--telegram-chat-id`, `--telegram-text`)

## 동작 요약

1. `fetch_bestsellers()`가 기본 수집 경로입니다 (`use_browser: false`).
2. 페이지 HTML의 `data-client-recs-list`에서 ASIN+rank(50개)를 읽어 전체 순위를 복원합니다.
3. DOM에 없는 항목은 ASIN 상세 페이지(`/dp/<asin>`)로 보강해 title/price/rating/reviews/image를 채웁니다.
4. 결과는 `data/bestsellers.json`에 저장되고, 이전 스냅샷은 `data/bestsellers_prev.json`으로 관리합니다.

## Telegram 이미지 카드

현재 카드 렌더링은 휴대폰 Telegram 보기 기준으로 최적화되어 있습니다.

- 세로형 1080px 폭 카드
- 섹션별 라운드 박스 + 아이템별 미니 카드
- 상품 제목은 20자로 절단 (`_truncate_text(..., 20)`)
- 긴 메타 정보는 줄바꿈 처리

렌더링 함수 위치:
- `main.py`의 `_build_image_card`
- `build_summary_image`, `build_update_image`

## 설정 (`config.json`)

```json
{
  "url": "https://www.amazon.com/Best-Sellers-Beauty-Personal-Care/zgbs/beauty",
  "update_interval_hours": 6,
  "data_file": "data/bestsellers.json",
  "previous_data_file": "data/bestsellers_prev.json",
  "state_file": "data/state.json",
  "user_agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36",
  "use_browser": false,
  "browser_max_pages": 6,
  "browser_initial_wait": 4,
  "browser_scroll_steps": 12,
  "browser_scroll_delay": 2
}
```

- CI 기본 경로는 브라우저 비사용(`use_browser: false`)입니다.
- 필요 시 `--use-browser` 인자로 브라우저 모드를 강제할 수 있습니다.

## 실행 방법

### 로컬 실행

```bash
python main.py --serve
```

### CI/원샷 실행

```bash
python main.py --update-now
```

```bash
python main.py --telegram-chat-id <CHAT_ID> --telegram-text "/summary"
```

## 환경 변수

- `TELEGRAM_BOT_TOKEN`: Telegram Bot API 토큰
- `TELEGRAM_CHAT_ID`: CI에서 기본 chat id로 사용 가능
- `TELEGRAM_TEXT`: CI에서 기본 명령 텍스트로 사용 가능

## 개발 메모

- 생성 데이터 파일은 Git 추적에서 제외됩니다:
  - `data/bestsellers.json`
  - `data/bestsellers_prev.json`
- 가상환경 디렉터리 `.venv/`도 제외됩니다.
