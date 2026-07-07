"""
youtube_uploader.py
Phase 3: kbeauty_videos 테이블에서 video_ready 상태인 영상을 가져와
YouTube Shorts에 업로드하고 자막 트랙을 추가한다.

처리 순서:
  1. Supabase kbeauty_videos에서 status='video_ready' 레코드 조회
  2. R2에서 final.mp4 다운로드
  3. YouTube Data API v3로 영상 업로드
  4. en.srt R2에서 다운로드 후 자막 트랙 업로드
  5. Supabase kbeauty_videos 업데이트
     (youtube_video_id, youtube_url, uploaded_at)
  6. kbeauty_content_candidates status → 'published'

사전 준비:
  1. Google Cloud Console에서 YouTube Data API v3 활성화
  2. OAuth 2.0 클라이언트 ID 생성 (데스크톱 앱)
  3. 최초 1회 인증 실행:
       python youtube_uploader.py --auth
     → token.json 생성됨 (이후 자동 갱신)
  4. .env에 설정:
       YOUTUBE_CLIENT_ID=...
       YOUTUBE_CLIENT_SECRET=...

사용법:
    # 최초 OAuth 인증
    python youtube_uploader.py --auth

    # 업로드 실행 (video_ready 전체)
    python youtube_uploader.py

    # 특정 candidate_id만 업로드
    python youtube_uploader.py --candidate-id <uuid>

    # 업로드 후 공개 상태 (기본: public)
    python youtube_uploader.py --privacy unlisted
"""
from __future__ import annotations

import argparse
import json
import logging
import os
import sys
import tempfile
import time
from datetime import datetime, timezone
from pathlib import Path

import boto3
from botocore.config import Config as BotoConfig
from google.auth.transport.requests import Request
from google.oauth2.credentials import Credentials
from google_auth_oauthlib.flow import InstalledAppFlow
from googleapiclient.discovery import build
from googleapiclient.errors import HttpError
from googleapiclient.http import MediaFileUpload

sys.path.insert(0, str(Path(__file__).parent.parent))
from config import config

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s %(levelname)s %(message)s",
)
logger = logging.getLogger(__name__)

# OAuth 스코프
SCOPES = [
    "https://www.googleapis.com/auth/youtube.upload",
    "https://www.googleapis.com/auth/youtube.force-ssl",
]

TOKEN_PATH = Path(__file__).parent / "token.json"
CLIENT_SECRET_PATH = Path(__file__).parent / "client_secret.json"


# ---------------------------------------------------------------------------
# OAuth 인증
# ---------------------------------------------------------------------------

def get_youtube_client():
    """OAuth2 인증된 YouTube API 클라이언트 반환."""
    creds = None

    if TOKEN_PATH.exists():
        creds = Credentials.from_authorized_user_file(str(TOKEN_PATH), SCOPES)

    if not creds or not creds.valid:
        if creds and creds.expired and creds.refresh_token:
            logger.info("토큰 갱신 중...")
            creds.refresh(Request())
        else:
            if not CLIENT_SECRET_PATH.exists():
                # .env 값으로 client_secret.json 동적 생성
                client_secret = {
                    "installed": {
                        "client_id": config.YOUTUBE_CLIENT_ID,
                        "client_secret": config.YOUTUBE_CLIENT_SECRET,
                        "redirect_uris": ["urn:ietf:wg:oauth:2.0:oob", "http://localhost"],
                        "auth_uri": "https://accounts.google.com/o/oauth2/auth",
                        "token_uri": "https://oauth2.googleapis.com/token",
                    }
                }
                CLIENT_SECRET_PATH.write_text(
                    json.dumps(client_secret), encoding="utf-8"
                )
                logger.info(f"client_secret.json 생성됨: {CLIENT_SECRET_PATH}")

            flow = InstalledAppFlow.from_client_secrets_file(
                str(CLIENT_SECRET_PATH), SCOPES
            )
            creds = flow.run_local_server(port=0)

        TOKEN_PATH.write_text(creds.to_json(), encoding="utf-8")
        logger.info(f"토큰 저장됨: {TOKEN_PATH}")

    return build("youtube", "v3", credentials=creds)


# ---------------------------------------------------------------------------
# R2 헬퍼
# ---------------------------------------------------------------------------

def _get_r2_client():
    endpoint = f"https://{config.R2_ACCOUNT_ID}.r2.cloudflarestorage.com"
    return boto3.client(
        "s3",
        endpoint_url=endpoint,
        aws_access_key_id=config.R2_ACCESS_KEY_ID,
        aws_secret_access_key=config.R2_SECRET_ACCESS_KEY,
        config=BotoConfig(signature_version="s3v4"),
        region_name="auto",
    )


def _download_from_r2(r2_client, r2_key: str, local_path: Path) -> None:
    logger.info(f"R2 다운로드: {r2_key}")
    r2_client.download_file(
        Bucket=config.R2_BUCKET_NAME,
        Key=r2_key,
        Filename=str(local_path),
    )


# ---------------------------------------------------------------------------
# 설명란 생성
# ---------------------------------------------------------------------------

def _build_description(row: dict, music_title: str, music_artist: str, thematic_url: str, affiliate_url: str) -> str:
    """
    YouTube 영상 설명란 생성.
    - 제품 설명
    - Olive Young 제휴 링크
    - Thematic 음악 크레딧 (Content ID 보호를 위해 필수)
    """
    # youtube_description이 jsonb ({"en": "..."} 형태)
    desc_json = row.get("youtube_description") or {}
    product_desc = desc_json.get("en", "") if isinstance(desc_json, dict) else ""

    lines = []

    if product_desc:
        lines.append(product_desc)
        lines.append("")

    if affiliate_url:
        lines.append(f"🛒 Shop here: {affiliate_url}")
        lines.append("")

    # Thematic 크레딧 (필수 — 없으면 Content ID 보호 안 됨)
    lines.append("🎵 Music")
    lines.append(f"{music_title} by {music_artist}")
    lines.append(f"Promoted by Thematic: {thematic_url}")
    lines.append("")

    lines.append("#KBeauty #KBeautyFinds #OliveYoung #KoreanSkincare #Shorts")

    return "\n".join(lines)


def _build_title(row: dict) -> str:
    """youtube_title jsonb에서 영어 제목 추출."""
    title_json = row.get("youtube_title") or {}
    if isinstance(title_json, dict):
        return title_json.get("en", "K-Beauty Find ✨")
    return str(title_json) or "K-Beauty Find ✨"


# ---------------------------------------------------------------------------
# YouTube 업로드
# ---------------------------------------------------------------------------

def upload_video(
    youtube,
    video_path: Path,
    title: str,
    description: str,
    privacy: str = "public",
) -> str:
    """
    YouTube에 영상을 업로드하고 video_id를 반환.
    재시도 로직 포함 (최대 3회).
    """
    body = {
        "snippet": {
            "title": title,
            "description": description,
            "tags": ["kbeauty", "oliveyoung", "koreanbeauty", "skincare", "shorts"],
            "categoryId": "26",  # Howto & Style
        },
        "status": {
            "privacyStatus": privacy,
            "selfDeclaredMadeForKids": False,
        },
    }

    media = MediaFileUpload(
        str(video_path),
        mimetype="video/mp4",
        resumable=True,
        chunksize=1024 * 1024 * 10,  # 10MB 청크
    )

    for attempt in range(1, 4):
        try:
            logger.info(f"YouTube 업로드 시작 (시도 {attempt}/3): {title}")
            request = youtube.videos().insert(
                part="snippet,status",
                body=body,
                media_body=media,
            )

            response = None
            while response is None:
                status, response = request.next_chunk()
                if status:
                    pct = int(status.progress() * 100)
                    logger.info(f"  업로드 진행: {pct}%")

            video_id = response["id"]
            logger.info(f"YouTube 업로드 완료: https://youtube.com/shorts/{video_id}")
            return video_id

        except HttpError as e:
            if e.resp.status in (500, 502, 503, 504) and attempt < 3:
                wait = 2 ** attempt
                logger.warning(f"HTTP {e.resp.status} 오류. {wait}초 후 재시도...")
                time.sleep(wait)
            else:
                raise

    raise RuntimeError("YouTube 업로드 최대 재시도 횟수 초과")


def upload_caption(youtube, video_id: str, srt_path: Path) -> None:
    """en.srt를 YouTube 자막 트랙으로 업로드."""
    logger.info(f"자막 업로드: {srt_path.name} → video_id={video_id}")

    media = MediaFileUpload(str(srt_path), mimetype="application/octet-stream")
    youtube.captions().insert(
        part="snippet",
        body={
            "snippet": {
                "videoId": video_id,
                "language": "en",
                "name": "English",
                "isDraft": False,
            }
        },
        media_body=media,
    ).execute()
    logger.info("자막 업로드 완료")


# ---------------------------------------------------------------------------
# Supabase 업데이트
# ---------------------------------------------------------------------------

def _update_supabase(candidate_id: str, video_row_id: str, video_id: str) -> None:
    from supabase import create_client

    sb = create_client(config.SUPABASE_URL, config.SUPABASE_SERVICE_KEY)
    now_iso = datetime.now(timezone.utc).isoformat()

    # kbeauty_videos 업데이트
    sb.table("kbeauty_videos").update({
        "youtube_video_id": video_id,
        "youtube_url": f"https://youtube.com/shorts/{video_id}",
        "uploaded_at": now_iso,
        "updated_at": now_iso,
    }).eq("id", video_row_id).execute()

    # kbeauty_content_candidates 상태 → published
    sb.table("kbeauty_content_candidates").update({
        "status": "published",
        "updated_at": now_iso,
    }).eq("id", candidate_id).execute()

    logger.info(f"Supabase 업데이트 완료: youtube_video_id={video_id}")


# ---------------------------------------------------------------------------
# 단일 영상 처리
# ---------------------------------------------------------------------------

def process_one(row: dict, youtube, privacy: str) -> None:
    """kbeauty_videos 레코드 하나를 처리한다."""
    candidate_id = row["candidate_id"]
    video_row_id = row["id"]

    logger.info(f"=== 처리 시작: candidate_id={candidate_id} ===")

    # music_track 정보 조회
    music_title, music_artist, thematic_url = "", "", ""
    if row.get("music_track_id"):
        from supabase import create_client
        sb = create_client(config.SUPABASE_URL, config.SUPABASE_SERVICE_KEY)
        track = sb.table("music_tracks").select(
            "title,artist,thematic_url"
        ).eq("id", row["music_track_id"]).single().execute()
        if track.data:
            music_title = track.data["title"]
            music_artist = track.data["artist"]
            thematic_url = track.data["thematic_url"]

    # 제휴 링크 (candidate 테이블에서 가져옴)
    affiliate_url = ""
    try:
        from supabase import create_client
        sb = create_client(config.SUPABASE_URL, config.SUPABASE_SERVICE_KEY)
        cand = sb.table("kbeauty_content_candidates").select(
            "affiliate_url"
        ).eq("id", candidate_id).single().execute()
        affiliate_url = cand.data.get("affiliate_url", "") if cand.data else ""
    except Exception:
        pass

    title = _build_title(row)
    description = _build_description(
        row, music_title, music_artist, thematic_url, affiliate_url
    )

    r2 = _get_r2_client()

    with tempfile.TemporaryDirectory() as tmp:
        tmp_path = Path(tmp)
        video_path = tmp_path / "final.mp4"
        srt_path = tmp_path / "en.srt"

        # R2에서 다운로드
        _download_from_r2(r2, row["video_r2_key"], video_path)
        _download_from_r2(r2, row["srt_r2_key"], srt_path)

        # YouTube 업로드
        video_id = upload_video(youtube, video_path, title, description, privacy)

        # 자막 업로드
        try:
            upload_caption(youtube, video_id, srt_path)
        except Exception as e:
            logger.warning(f"자막 업로드 실패 (영상 업로드는 완료됨): {e}")

    # Supabase 업데이트
    _update_supabase(candidate_id, video_row_id, video_id)
    logger.info(f"=== 완료: https://youtube.com/shorts/{video_id} ===")


# ---------------------------------------------------------------------------
# 메인
# ---------------------------------------------------------------------------

def run(candidate_id: str | None = None, privacy: str = "public", max_items: int = 5) -> None:
    """
    video_ready 상태 영상을 YouTube에 업로드한다.

    Args:
        candidate_id: 특정 candidate만 처리. None이면 전체 video_ready 처리.
        privacy: "public" | "unlisted" | "private"
        max_items: 한 번에 처리할 최대 영상 수
    """
    from supabase import create_client

    youtube = get_youtube_client()
    sb = create_client(config.SUPABASE_URL, config.SUPABASE_SERVICE_KEY)

    if candidate_id:
        resp = sb.table("kbeauty_videos").select("*").eq(
            "candidate_id", candidate_id
        ).execute()
    else:
        # video_ready이면서 youtube_video_id가 없는 레코드
        resp = sb.table("kbeauty_videos").select("*").is_(
            "youtube_video_id", None
        ).order("created_at", desc=False).limit(max_items).execute()

    rows = resp.data
    if not rows:
        logger.info("업로드할 영상이 없습니다.")
        return

    logger.info(f"업로드 대상: {len(rows)}개")
    success, fail = 0, 0

    for row in rows:
        try:
            process_one(row, youtube, privacy)
            success += 1
            # YouTube API quota 보호 — 영상 사이 3초 대기
            if len(rows) > 1:
                time.sleep(3)
        except Exception as e:
            logger.error(f"업로드 실패: candidate_id={row.get('candidate_id')} — {e}", exc_info=True)
            fail += 1

    logger.info(f"=== 전체 완료: 성공 {success}개 / 실패 {fail}개 ===")


# ---------------------------------------------------------------------------
# CLI
# ---------------------------------------------------------------------------

def main() -> None:
    parser = argparse.ArgumentParser(description="ShortsBot YouTube 업로드 봇")
    parser.add_argument(
        "--auth", action="store_true",
        help="OAuth 인증만 수행하고 종료 (최초 1회 실행 필요)"
    )
    parser.add_argument(
        "--candidate-id", type=str, default=None,
        help="특정 candidate_id만 업로드"
    )
    parser.add_argument(
        "--privacy", type=str, default="public",
        choices=["public", "unlisted", "private"],
        help="영상 공개 상태 (기본: public)"
    )
    parser.add_argument(
        "--max-items", type=int, default=5,
        help="한 번에 업로드할 최대 영상 수 (기본: 5)"
    )
    args = parser.parse_args()

    if args.auth:
        logger.info("OAuth 인증을 시작합니다. 브라우저가 열립니다...")
        get_youtube_client()
        logger.info("인증 완료. token.json이 저장되었습니다.")
        return

    run(
        candidate_id=args.candidate_id,
        privacy=args.privacy,
        max_items=args.max_items,
    )


if __name__ == "__main__":
    main()
