"""
collector/uploader.py
Phase 2-3: ffmpeg_composer.py가 만든 final.mp4와 en.srt를
Cloudflare R2에 업로드하고 Supabase를 업데이트한다.

파이프라인 위치:
  ffmpeg_composer.compose() → ComposerResult(video_path, srt_path, duration)
    → uploader.upload()
    → R2: kbeauty-shorts/{candidate_id}/final.mp4
           kbeauty-shorts/{candidate_id}/en.srt
    → kbeauty_content_candidates: status=video_ready, video_url 업데이트
    → kbeauty_videos: 레코드 생성

Phase 3 재사용:
  YouTube 업로드 봇에서도 이 모듈을 import해서 SRT 경로를 꺼내 자막 업로드에 쓴다.
  get_video_r2_url() / get_srt_r2_url() 헬퍼를 제공한다.
"""
from __future__ import annotations

import logging
import mimetypes
import os
from dataclasses import dataclass
from datetime import datetime, timezone

import boto3
from botocore.config import Config as BotoConfig

import sys as _sys
import os as _os
_sys.path.insert(0, _os.path.join(_os.path.dirname(__file__), ".."))
from config import config

logger = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# R2 클라이언트
# ---------------------------------------------------------------------------

def _get_r2_client():
    """boto3 S3-compatible R2 클라이언트 싱글턴."""
    endpoint = f"https://{config.R2_ACCOUNT_ID}.r2.cloudflarestorage.com"
    return boto3.client(
        "s3",
        endpoint_url=endpoint,
        aws_access_key_id=config.R2_ACCESS_KEY_ID,
        aws_secret_access_key=config.R2_SECRET_ACCESS_KEY,
        config=BotoConfig(signature_version="s3v4"),
        region_name="auto",
    )


# ---------------------------------------------------------------------------
# R2 키 / URL 헬퍼
# ---------------------------------------------------------------------------

def get_video_r2_key(candidate_id: str) -> str:
    return f"{candidate_id}/final.mp4"


def get_raw_video_r2_key(candidate_id: str) -> str:
    """TTS/음악 합성 전, script_generator.py가 세그먼트를 이어붙인 원본 영상 키."""
    return f"{candidate_id}/raw_video.mp4"


def get_srt_r2_key(candidate_id: str) -> str:
    return f"{candidate_id}/en.srt"


def _r2_public_url(key: str) -> str:
    """
    R2 공개 URL 조립. R2_PUBLIC_BASE_URL(버킷 Public Access로 발급된 r2.dev
    서브도메인, 또는 연결한 커스텀 도메인)을 반드시 써야 한다 —
    {account_id}.r2.cloudflarestorage.com 형태의 S3 API 엔드포인트는 SigV4
    서명이 있어야만 접근되는 주소라 외부 서비스(Kling 등)가 그냥 못 읽는다.
    """
    if not config.R2_PUBLIC_BASE_URL:
        raise RuntimeError(
            "R2_PUBLIC_BASE_URL이 설정되지 않았습니다. R2 버킷의 Public Access를 "
            "활성화하고 발급된 URL을 .env의 R2_PUBLIC_BASE_URL로 넣어주세요."
        )
    return f"{config.R2_PUBLIC_BASE_URL.rstrip('/')}/{key}"


def get_video_r2_url(candidate_id: str) -> str:
    """R2 공개 URL."""
    return _r2_public_url(get_video_r2_key(candidate_id))


def get_srt_r2_url(candidate_id: str) -> str:
    return _r2_public_url(get_srt_r2_key(candidate_id))


# ---------------------------------------------------------------------------
# 업로드 결과
# ---------------------------------------------------------------------------

@dataclass
class UploadResult:
    candidate_id: str
    video_r2_key: str
    video_url: str
    srt_r2_key: str
    srt_url: str
    duration: float
    video_engine: str   # "kling" | "vidu" 등 config.VIDEO_ENGINE 값


# ---------------------------------------------------------------------------
# 메인 함수
# ---------------------------------------------------------------------------

def upload(
    candidate_id: str,
    video_path: str,
    srt_path: str,
    duration: float,
    video_engine: str | None = None,
) -> UploadResult:
    """
    final.mp4 + en.srt → R2 업로드 → Supabase 업데이트.

    Args:
        candidate_id: kbeauty_content_candidates.id (UUID 문자열)
        video_path:   ffmpeg_composer가 만든 final.mp4 로컬 경로
        srt_path:     ffmpeg_composer가 만든 en.srt 로컬 경로
        duration:     영상 길이(초) — ComposerResult.duration
        video_engine: 영상 생성 엔진 식별자. None이면 config.VIDEO_ENGINE 사용.

    Returns:
        UploadResult
    """
    engine = video_engine or config.VIDEO_ENGINE

    r2 = _get_r2_client()

    # --- R2 업로드 ---
    video_key = get_video_r2_key(candidate_id)
    srt_key = get_srt_r2_key(candidate_id)

    logger.info(f"R2 업로드 시작: {candidate_id}")

    _upload_file(r2, video_path, video_key, content_type="video/mp4")
    _upload_file(r2, srt_path, srt_key, content_type="text/plain; charset=utf-8")

    video_url = get_video_r2_url(candidate_id)
    srt_url = get_srt_r2_url(candidate_id)

    logger.info(f"R2 업로드 완료: {video_url}")

    # --- Supabase 업데이트 ---
    _update_supabase(
        candidate_id=candidate_id,
        video_url=video_url,
        video_r2_key=video_key,
        srt_r2_key=srt_key,
        duration=duration,
        engine=engine,
    )

    return UploadResult(
        candidate_id=candidate_id,
        video_r2_key=video_key,
        video_url=video_url,
        srt_r2_key=srt_key,
        srt_url=srt_url,
        duration=duration,
        video_engine=engine,
    )


def upload_raw_video(candidate_id: str, video_path: str) -> str:
    """
    script_generator.py가 세그먼트를 ffmpeg concat으로 이어붙인 원본(TTS/음악 합성 전)
    영상을 R2에 업로드하고 영구 URL을 반환한다.

    Kling/Vidu가 주는 세그먼트 URL은 24시간 후 만료되는 임시 CDN URL이라,
    candidate가 raw_video_ready 상태로 오래 대기하다가 producer/pipeline.py가
    나중에(다른 프로세스에서) 합성 단계를 이어받아도 링크가 살아있도록
    생성 직후 바로 R2에 옮겨 candidate.video_url에 저장한다.

    Args:
        candidate_id: kbeauty_content_candidates.id
        video_path: ffmpeg concat으로 만든 로컬 mp4 경로

    Returns:
        R2 퍼블릭 URL
    """
    r2 = _get_r2_client()
    key = get_raw_video_r2_key(candidate_id)
    _upload_file(r2, video_path, key, content_type="video/mp4")

    url = _r2_public_url(key)
    logger.info(f"raw 영상 R2 업로드 완료: {url}")
    return url


# ---------------------------------------------------------------------------
# 내부 헬퍼
# ---------------------------------------------------------------------------

def _upload_file(r2_client, local_path: str, r2_key: str, content_type: str) -> None:
    """단일 파일을 R2에 업로드."""
    file_size = os.path.getsize(local_path)
    logger.info(f"  업로드: {r2_key} ({file_size / 1024 / 1024:.1f} MB)")
    with open(local_path, "rb") as f:
        r2_client.put_object(
            Bucket=config.R2_BUCKET_NAME,
            Key=r2_key,
            Body=f,
            ContentType=content_type,
        )


def _update_supabase(
    candidate_id: str,
    video_url: str,
    video_r2_key: str,
    srt_r2_key: str,
    duration: float,
    engine: str,
) -> None:
    """
    kbeauty_content_candidates 상태 업데이트 +
    kbeauty_videos 레코드 생성.
    """
    # import here to avoid circular dependency
    import sys
    import os
    sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))
    from db.supabase_client import update_candidate_status, insert_video

    now_iso = datetime.now(timezone.utc).isoformat()

    # 1) candidate 상태 → video_ready
    update_candidate_status(
        candidate_id=candidate_id,
        status="video_ready",
        extra={
            "video_url": video_url,
            "video_engine": engine,
            "video_generation_sec": round(duration, 2),
        },
    )

    # 2) kbeauty_videos 레코드 생성
    insert_video({
        "candidate_id": candidate_id,
        "video_r2_key": video_r2_key,
        "srt_r2_key": srt_r2_key,
        "video_url": video_url,
        "duration_sec": round(duration, 2),
        "video_engine": engine,
        "created_at": now_iso,
    })

    logger.info(f"Supabase 업데이트 완료: candidate_id={candidate_id}, status=video_ready")


# ---------------------------------------------------------------------------
# 편의 함수: ffmpeg_composer.ComposerResult를 바로 받는 버전
# ---------------------------------------------------------------------------

def upload_from_composer_result(
    candidate_id: str,
    composer_result,           # ffmpeg_composer.ComposerResult
    video_engine: str | None = None,
) -> UploadResult:
    """
    ffmpeg_composer.compose()의 결과를 바로 받아 업로드.

    사용 예:
        from collector.ffmpeg_composer import compose, ComposerInput
        from collector.uploader import upload_from_composer_result

        result = compose(inp)
        upload_result = upload_from_composer_result(candidate_id, result)
    """
    return upload(
        candidate_id=candidate_id,
        video_path=composer_result.video_path,
        srt_path=composer_result.srt_path,
        duration=composer_result.duration,
        video_engine=video_engine,
    )
