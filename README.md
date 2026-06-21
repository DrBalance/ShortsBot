# K뷰티 쇼츠 자동화

K뷰티 트렌드 소식을 자동으로 수집하고, 다국어 쇼츠 영상을 제작하여 유튜브에 자동 업로드하는 시스템.

## 현재 진행 상황

- [x] **1단계: 수집 봇** ← 지금 여기
  - Apify Instagram Hashtag Scraper
  - Claude 트렌드 분석
  - APScheduler 주기 실행
- [ ] 2단계: 영상 제작 파이프라인 (TTS + Kling + ffmpeg)
- [ ] 3단계: 유튜브 자동 업로드
- [ ] 4단계: 성과 분석 봇
- [ ] 5단계: React 대시보드

---

## 빠른 시작

### 1. 환경 세팅

```bash
# 의존 패키지 설치
pip install -r requirements.txt

# 환경변수 설정
cp .env.example .env
# .env 파일을 열고 API 키 입력
```

### 2. Supabase DB 초기화

Supabase 대시보드 → SQL Editor에서 `db/schema.sql` 전체 실행.

### 3. 서버 실행

```bash
# FastAPI 서버 (대시보드 API)
python main.py

# 수집 봇 스케줄러 (별도 터미널)
python -m collector.scheduler
```

### 4. 수동 테스트

```bash
# 수집 한 번 실행
curl -X POST http://localhost:8000/api/trigger/collect

# 분석 한 번 실행
curl -X POST http://localhost:8000/api/trigger/analyze

# 통계 확인
curl http://localhost:8000/api/stats
```

---

## 프로젝트 구조

```
kbeauty-shorts/
├── main.py                    # FastAPI 앱
├── config.py                  # 환경변수 설정
├── requirements.txt
├── .env.example               # 환경변수 템플릿
│
├── collector/                 # 1단계: 수집 봇
│   ├── apify_scraper.py       # Apify 인스타 스크래퍼
│   ├── claude_analyzer.py     # Claude 트렌드 분석
│   └── scheduler.py           # APScheduler
│
├── db/
│   ├── schema.sql             # Supabase 테이블 스키마
│   └── supabase_client.py     # DB 작업 함수
│
├── producer/                  # 2단계: 영상 제작 (예정)
├── uploader/                  # 3단계: 유튜브 업로드 (예정)
├── analyzer/                  # 4단계: 성과 분석 (예정)
└── dashboard/                 # 5단계: React 대시보드 (예정)
```

---

## 환경변수 목록

| 키 | 필수 | 설명 |
|---|---|---|
| `APIFY_API_TOKEN` | ✅ | Apify 콘솔에서 발급 |
| `ANTHROPIC_API_KEY` | ✅ | console.anthropic.com |
| `SUPABASE_URL` | ✅ | Supabase 프로젝트 URL |
| `SUPABASE_SERVICE_KEY` | ✅ | Supabase service_role 키 |
| `ELEVENLABS_API_KEY` | 2단계 | TTS 음성 생성 |
| `KLING_ACCESS_KEY` | 2단계 | 영상 생성 |
| `YOUTUBE_CLIENT_ID` | 3단계 | 유튜브 업로드 |

---

## Apify 비용

- 무료 플랜 $5 크레딧으로 월 3,000~6,000건 수집 가능
- 하루 6시간마다 수집 → 월 약 4,000건 수집
