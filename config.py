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
    TARGET_LANGUAGES: list[dict] = [
        {"code": "en", "name": "English"},
        {"code": "ko", "name": "한국어"},
        {"code": "th", "name": "ภาษาไทย"},
        {"code": "vi", "name": "Tiếng Việt"},
        {"code": "id", "name": "Bahasa Indonesia"},
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
