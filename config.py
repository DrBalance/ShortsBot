"""
config.py
환경변수를 로드하고 전역 설정을 관리합니다.
.env 파일이 없어도 에러 없이 None으로 처리됩니다.
"""
import os
from dotenv import load_dotenv

load_dotenv()


class Config:
    # ─── Apify ───────────────────────────────────────
    APIFY_API_TOKEN: str = os.getenv("APIFY_API_TOKEN", "")

    # Apify Actor ID: Instagram Hashtag Scraper
    APIFY_ACTOR_ID: str = "apify~instagram-scraper"

    # 수집 대상 해시태그
    HASHTAGS_KR: list[str] = [
        "올리브영",
        "올영신상",
        "올리브영추천",
        "올리브영구매",
        "뷰티추천",
        "K뷰티",
    ]
    HASHTAGS_JP: list[str] = [
        "韓国コスメ",
        "韓国美容",
        "オルリブヤン",
    ]

    # 필터링 기준
    MIN_LIKES: int = 100
    MAX_POSTS_PER_HASHTAG: int = 30

    # ─── Google ──────────────────────────────────────
    GOOGLE_API_KEY: str = os.getenv("GOOGLE_API_KEY", "")

    # ─── Anthropic ───────────────────────────────────
    ANTHROPIC_API_KEY: str = os.getenv("ANTHROPIC_API_KEY", "")
    CLAUDE_MODEL: str = "claude-sonnet-4-6"

    # ─── Supabase ────────────────────────────────────
    SUPABASE_URL: str = os.getenv("SUPABASE_URL", "")
    SUPABASE_SERVICE_KEY: str = os.getenv("SUPABASE_SERVICE_KEY", "")

    # ─── Cloudflare R2 ───────────────────────────────
    R2_ACCOUNT_ID: str = os.getenv("R2_ACCOUNT_ID", "")
    R2_ACCESS_KEY_ID: str = os.getenv("R2_ACCESS_KEY_ID", "")
    R2_SECRET_ACCESS_KEY: str = os.getenv("R2_SECRET_ACCESS_KEY", "")
    R2_BUCKET_NAME: str = os.getenv("R2_BUCKET_NAME", "kbeauty-shorts")
    # R2 버킷 Public Access(r2.dev 서브도메인) 활성화 후 발급되는 공개 URL.
    # {account_id}.r2.cloudflarestorage.com 형태의 S3 API 엔드포인트는 SigV4 서명이
    # 있어야만 접근되는 주소라 외부 서비스(Kling 등)가 그냥 못 읽는다 — 실제 공개
    # 서빙에는 반드시 이 값을 써야 한다.
    R2_PUBLIC_BASE_URL: str = os.getenv("R2_PUBLIC_BASE_URL", "")

    # ─── ElevenLabs ──────────────────────────────────
    ELEVENLABS_API_KEY: str = os.getenv("ELEVENLABS_API_KEY", "")
    ELEVENLABS_VOICE_ID: str = os.getenv(
        "ELEVENLABS_VOICE_ID",
        "EXAVITQu4vr4xnSDxMaL",   # Sarah — US English, news presenter style
    )

    # ─── Kling ───────────────────────────────────────
    # 공식 Kling AI API Platform (kling.ai/document-api)
    # 인증 방식: 단일 API Key → "Bearer {key}" 헤더
    # API Key 형식: api-key-kling-...
    KLING_API_KEY: str = os.getenv("KLING_API_KEY", "")

    # ─── Vidu ────────────────────────────────────────
    # 공식 Vidu API Platform (platform.vidu.com)
    # 인증 방식: API Key → "Token {key}" 헤더
    VIDU_API_KEY: str = os.getenv("VIDU_API_KEY", "")

    # ─── 영상 생성 엔진 선택 ──────────────────────────
    # "kling" : Kling v3.0 (현재 Trial Package 소진 중)
    # "vidu"  : Vidu Q3 (Kling 소진 후 전환)
    # 전환 시 이 값만 바꾸면 됨 — 코드 수정 불필요
    VIDEO_ENGINE: str = os.getenv("VIDEO_ENGINE", "kling")

    # ─── 음악 합성 방식 ───────────────────────────────
    # "external" : 짤스튜디오 등 외부 음원 사용.
    #              ffmpeg 합성 시 음악 트랙을 포함하지 않고,
    #              유튜브 앱 업로드 시 ISRC 코드로 수동으로 사운드 추가.
    #              → 짤스튜디오 음원 수익 인식을 위해 필수.
    #                (직접 다운받아 삽입하면 유튜브에서 음원 인식 안 됨)
    # "embedded" : YouTube Audio Library 등 자유 음원 사용.
    #              ffmpeg에서 음악 트랙을 직접 합성. 완전 자동화 가능.
    #              → 유튜브 정책 변경 또는 짤스튜디오 미사용 시 전환.
    # 전환 시 이 값과 .env의 MUSIC_MODE만 바꾸면 됨 — 코드 수정 불필요.
    MUSIC_MODE: str = os.getenv("MUSIC_MODE", "external")

    # ─── YouTube ─────────────────────────────────────
    YOUTUBE_CLIENT_ID: str = os.getenv("YOUTUBE_CLIENT_ID", "")
    YOUTUBE_CLIENT_SECRET: str = os.getenv("YOUTUBE_CLIENT_SECRET", "")
    YOUTUBE_REFRESH_TOKEN: str = os.getenv("YOUTUBE_REFRESH_TOKEN", "")

    # ─── 올리브영 ─────────────────────────────────────
    OLIVEYOUNG_CURATOR_BASE_URL: str = os.getenv("OLIVEYOUNG_CURATOR_BASE_URL", "")

    # ─── 스케줄 ───────────────────────────────────────
    COLLECT_INTERVAL_HOURS: int = int(os.getenv("COLLECT_INTERVAL_HOURS", 6))
    PRODUCE_INTERVAL_HOURS: int = int(os.getenv("PRODUCE_INTERVAL_HOURS", 8))
    UPLOAD_INTERVAL_HOURS: int = int(os.getenv("UPLOAD_INTERVAL_HOURS", 24))
    MAX_ITEMS_PER_RUN: int = int(os.getenv("MAX_ITEMS_PER_RUN", 5))

    # ─── 대상 언어 ────────────────────────────────────
    # 현재 영어권(미국/영국/캐나다/호주) 단일 타겟으로 운영.
    # 채널 성장 후 다국어 자막 추가 시 이 목록을 확장.
    TARGET_LANGUAGES: list[dict] = [
        {"code": "en", "name": "English"},
    ]

    def validate(self) -> list[str]:
        """필수 키가 설정되어 있는지 확인. 누락된 키 목록 반환."""
        required = {
            "APIFY_API_TOKEN":      self.APIFY_API_TOKEN,
            "ANTHROPIC_API_KEY":    self.ANTHROPIC_API_KEY,
            "SUPABASE_URL":         self.SUPABASE_URL,
            "SUPABASE_SERVICE_KEY": self.SUPABASE_SERVICE_KEY,
            "ELEVENLABS_API_KEY":   self.ELEVENLABS_API_KEY,
            "KLING_API_KEY":        self.KLING_API_KEY,
        }
        return [k for k, v in required.items() if not v]


config = Config()
