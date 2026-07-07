"""
music_selector.py
Supabase music_tracks 테이블에서 랜덤으로 곡을 선택하고
R2에서 35초 클립을 로컬에 다운로드하는 헬퍼 모듈.

파이프라인 위치:
  script_generator.py
    → music_selector.select_track() ← 여기
    → ffmpeg_composer.compose()      (clip_path를 music_path로 전달)
    → uploader.upload()

사용 예:
    from music_selector import select_track, MusicTrack

    track = select_track(mood="upbeat")   # mood 지정 (선택)
    track = select_track()                # 전체에서 랜덤

    # ffmpeg_composer.ComposerInput에 전달
    inp = ComposerInput(
        ...
        music_path=track.local_clip_path,
        start_offset=track.start_offset,
        video_end=track.video_end,
        fade_in_ms=track.fade_in_ms,
        fade_out_ms=track.fade_out_ms,
    )
"""
from __future__ import annotations

import logging
import sys
import tempfile
from dataclasses import dataclass
from pathlib import Path

import boto3
from botocore.config import Config as BotoConfig

sys.path.insert(0, str(Path(__file__).parent.parent))
from config import config

logger = logging.getLogger(__name__)


@dataclass
class MusicTrack:
    """선택된 음악 트랙 정보."""
    id: str                  # music_tracks.id (UUID)
    title: str
    artist: str
    thematic_url: str        # 설명란 크레딧용
    mood: str
    bpm: float
    start_offset: float
    video_end: float
    fade_in_ms: float
    fade_out_ms: float
    clip_r2_key: str
    local_clip_path: str     # 다운로드된 로컬 경로 (임시 파일)


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


def select_track(
    mood: str | None = None,
    exclude_ids: list[str] | None = None,
    download_dir: str | None = None,
) -> MusicTrack:
    """
    music_tracks 테이블에서 랜덤으로 곡을 선택하고 클립을 다운로드한다.

    Args:
        mood: 필터링할 mood ("upbeat", "chill" 등). None이면 전체에서 랜덤.
        exclude_ids: 제외할 track id 목록 (같은 배치에서 중복 방지).
        download_dir: 클립을 저장할 디렉터리. None이면 시스템 임시 디렉터리.

    Returns:
        MusicTrack (local_clip_path에 다운로드된 경로 포함)

    Raises:
        RuntimeError: 선택 가능한 트랙이 없을 때
    """
    from supabase import create_client

    sb = create_client(config.SUPABASE_URL, config.SUPABASE_SERVICE_KEY)

    # used_count 오름차순으로 정렬해서 가져온 뒤 Python에서 랜덤 선택
    # (Supabase는 RANDOM() ORDER를 직접 지원하지 않음)
    query = sb.table("music_tracks").select("*").eq("is_active", True)

    if mood:
        query = query.eq("mood", mood)

    # used_count 낮은 순으로 상위 20개 후보 풀
    resp = query.order("used_count", desc=False).limit(20).execute()
    candidates = resp.data

    if not candidates:
        if mood:
            logger.warning(f"mood='{mood}'인 트랙이 없어 전체에서 선택합니다.")
            return select_track(mood=None, exclude_ids=exclude_ids, download_dir=download_dir)
        raise RuntimeError("선택 가능한 music_tracks 레코드가 없습니다. music_preprocessor.py를 먼저 실행하세요.")

    # exclude_ids 제거
    if exclude_ids:
        candidates = [c for c in candidates if c["id"] not in exclude_ids]
        if not candidates:
            logger.warning("exclude_ids 제거 후 후보가 없어 exclude 없이 재시도합니다.")
            return select_track(mood=mood, download_dir=download_dir)

    # 랜덤 선택
    import random
    row = random.choice(candidates)
    logger.info(f"음악 선택: [{row['mood']}] {row['title']} - {row['artist']} (used_count={row['used_count']})")

    # R2에서 클립 다운로드
    dl_dir = Path(download_dir) if download_dir else Path(tempfile.gettempdir())
    dl_dir.mkdir(parents=True, exist_ok=True)
    local_path = dl_dir / Path(row["clip_r2_key"]).name

    r2 = _get_r2_client()
    logger.info(f"R2 클립 다운로드: {row['clip_r2_key']}")
    r2.download_file(
        Bucket=config.R2_BUCKET_NAME,
        Key=row["clip_r2_key"],
        Filename=str(local_path),
    )
    logger.info(f"다운로드 완료: {local_path}")

    # used_count 증가
    sb.table("music_tracks").update(
        {"used_count": row["used_count"] + 1}
    ).eq("id", row["id"]).execute()

    return MusicTrack(
        id=row["id"],
        title=row["title"],
        artist=row["artist"],
        thematic_url=row["thematic_url"],
        mood=row["mood"],
        bpm=row["bpm"],
        start_offset=row["start_offset"],
        video_end=row["video_end"],
        fade_in_ms=row["fade_in_ms"],
        fade_out_ms=row["fade_out_ms"],
        clip_r2_key=row["clip_r2_key"],
        local_clip_path=str(local_path),
    )
