# Amazon Beauty Bestseller Tracker

Amazon의 뷰티 카테고리 베스트셀러 순위를 자동으로 수집하고, 키워드로 필터링하며, 6시간마다 자동으로 업데이트하는 Python 애플리케이션입니다.

## 기능

- 🔄 **자동 업데이트**: 6시간마다 자동으로 베스트셀러 데이터 갱신
- 🔍 **키워드 필터링**: 상품명으로 원하는 제품만 필터링
- 💾 **데이터 저장**: JSON 형식으로 데이터 저장
- 📊 **순위 추적**: 상품 순위, 가격, 별점, 리뷰 수 저장

## 설치

### 요구사항
- Python 3.7 이상
- pip (Python 패키지 관리자)

### 단계별 설치

1. **저장소 클론**
   ```bash
   cd /Users/undomiel/workspace/amazon-bestseller-tracker
   ```

2. **가상 환경 생성 (선택사항이지만 권장)**
   ```bash
   python3 -m venv venv
   source venv/bin/activate  # macOS/Linux
   # 또는
   venv\Scripts\activate  # Windows
   ```

3. **필요한 패키지 설치**
   ```bash
   pip install -r requirements.txt
   ```

## 사용 방법

### 프로그램 실행

```bash
python main.py
```

### 웹훅 설정

Telegram Bot webhook을 Amazon Bestseller Tracker의 `/webhook` 엔드포인트로 설정하세요.
예시:

```bash
https://your-server-domain.com/webhook
```

### GitHub Actions CI

이 프로젝트에는 Telegram 이벤트를 CI에서 처리하는 워크플로우가 포함되어 있습니다. `workflow_dispatch`를 이용해 수동으로 실행하거나 외부 시스템에서 GitHub Actions API로 트리거할 수 있습니다.

- `telegram_chat_id`: Telegram chat id
- `telegram_text`: Telegram 메시지 텍스트
- `update_now`: 즉시 업데이트 실행 여부

### Telegram 명령어

- `/start` 또는 `시작` - 이 채팅 ID를 알림 대상에 추가
- `/add <브랜드>` 또는 `추가 <브랜드>` - 브랜드 추적 추가
- `/remove <브랜드>` 또는 `삭제 <브랜드>` - 브랜드 추적 제거
- `/update` 또는 `업데이트` - 즉시 크롤링 및 업데이트 실행
- `/summary` 또는 `요약` - 현재 추적 브랜드 요약 정보 전송
- `/list` 또는 `목록` - 현재 추적 중인 브랜드 목록 조회
- `/help` 또는 `도움` - 도움말 표시

### 동작 방식

- 앱 실행 시 6시간마다 Amazon Beauty Bestseller 데이터를 조회합니다.
- 이전 수집 결과와 비교하여 추적 중인 브랜드 순위 변동을 계산합니다.
- 등록된 채팅 ID에 순위 변동 요약을 Telegram 메시지로 전송합니다.

## 설정

`config.json` 파일을 수정하여 다음을 설정할 수 있습니다:

```json
{
  "url": "https://www.amazon.com/Best-Sellers-Beauty-Personal-Care/zgbs/beauty",
  "update_interval_hours": 6,
  "data_file": "data/bestsellers.json",
  "previous_data_file": "data/bestsellers_prev.json",
  "state_file": "data/state.json",
  "telegram_bot_token": "YOUR_TELEGRAM_BOT_TOKEN",
  "user_agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36"
}
```

- **url**: Amazon 베스트셀러 페이지 URL
- **update_interval_hours**: 자동 업데이트 간격 (시간)
- **data_file**: 최신 데이터 저장 위치
- **previous_data_file**: 이전 업데이트 데이터를 저장하는 파일
- **state_file**: Telegram 구독자 및 브랜드 상태 파일
- **telegram_bot_token**: Telegram Bot API 토큰
- **user_agent**: HTTP 요청 헤더의 User-Agent

## 데이터 형식

저장된 데이터 (`data/bestsellers.json`)는 다음 형식입니다:

```json
{
  "timestamp": "2026-04-17T10:30:45.123456",
  "bestsellers": [
    {
      "rank": 1,
      "title": "Product Title",
      "price": "$29.99",
      "rating": "4.5★",
      "reviews": "2,345"
    },
    ...
  ]
}
```

## 주의사항

- Amazon의 웹사이트 구조가 변경될 수 있으므로, 그 경우 파싱 로직을 수정해야 할 수 있습니다.
- 너무 빈번한 요청은 IP 차단으로 이어질 수 있으니 적절한 업데이트 간격을 설정하세요.
- User-Agent를 브라우저 정보로 설정하여 요청을 자연스럽게 만들었습니다.

## 문제 해결

### "No data file found" 에러
- `update` 명령을 먼저 실행하여 데이터를 초기화하세요.

### 데이터가 업데이트되지 않음
- Amazon 페이지 구조가 변경되었을 수 있습니다.
- `main.py`의 파싱 로직을 확인하고 필요에 따라 수정하세요.

### 요청 차단됨
- User-Agent를 변경하거나 요청 간격을 늘려보세요.

## 라이선스

MIT License

## 작성자

Amazon Bestseller Tracker Team
