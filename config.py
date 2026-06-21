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
    MIN_LIKES: int = 100          # 최소 좋아요 수
    MAX_POSTS_PER_HASHTAG: int = 30  # 해시태그당 최대 수집 게시물

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
    # 한국어 여성 보이스 ID (ElevenLabs 라이브러리에서 교체 가능)
    ELEVENLABS_VOICE_ID: str = os.getenv("ELEVENLABS_VOICE_ID", "pNInz6obpgDQGcFmaJgB")

    # ─── Kling ───────────────────────────────────────
    KLING_ACCESS_KEY: str = os.getenv("KLING_ACCESS_KEY", "")
    KLING_SECRET_KEY: str = os.getenv("KLING_SECRET_KEY", "")

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
        {"code": "ko", "name": "한국어"},
        {"code": "en", "name": "English"},
        {"code": "th", "name": "ภาษาไทย"},
        {"code": "vi", "name": "Tiếng Việt"},
        {"code": "id", "name": "Bahasa Indonesia"},
    ]

    def validate(self) -> list[str]:
        """필수 키가 설정되어 있는지 확인. 누락된 키 목록 반환."""
        required = {
            "APIFY_API_TOKEN": self.APIFY_API_TOKEN,
            "ANTHROPIC_API_KEY": self.ANTHROPIC_API_KEY,
            "SUPABASE_URL": self.SUPABASE_URL,
            "SUPABASE_SERVICE_KEY": self.SUPABASE_SERVICE_KEY,
        }
        return [k for k, v in required.items() if not v]


config = Config()
