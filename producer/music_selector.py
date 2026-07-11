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

    # ffmpeg_composer.ComposerInput에 전달 (clip은 이미 trim+fade+볼륨 처리 완료됨)
    inp = ComposerInput(
        ...
        music_path=track.local_clip_path,
        music_preprocessed=True,
    )

get_scene_template()은 분석 시점(claude_analyzer.run_analysis)에 필요한 씬 슬롯
구조만 music_tracks 풀에서 빌려온다. 실제 배경음악은 제작 시점에 select_track()으로
별도 랜덤 선택되므로, 여기서는 used_count를 증가시키지 않는다.
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

    local_path = _download_clip_for_row(row, download_dir)

    # used_count 증가
    sb.table("music_tracks").update(
        {"used_count": row["used_count"] + 1}
    ).eq("id", row["id"]).execute()

    return _row_to_track(row, local_path)


def _download_clip_for_row(row: dict, download_dir: str | None) -> str:
    """row(music_tracks 레코드)의 clip_r2_key를 download_dir로 다운로드하고 로컬 경로를 반환."""
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
    return str(local_path)


def _row_to_track(row: dict, local_path: str) -> MusicTrack:
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
        local_clip_path=local_path,
    )


def get_track_clip(track_id: str, download_dir: str | None = None) -> MusicTrack:
    """
    track_id로 특정 곡의 메타데이터를 조회하고 클립을 (재)다운로드한다.
    select_track()과 달리 used_count는 증가시키지 않는다 — 이미 select_track()이
    선택 시점에 1회 증가시켰으므로, 여기서는 같은 곡의 클립 파일만 다시 받는다.

    사용처: producer/pipeline.py — run_generation()이 select_track()으로 곡을
    고른 뒤 시간이 지나 별도 프로세스(스케줄러)가 합성 단계를 이어받을 때,
    최초 선택 시 받은 로컬 임시 파일이 이미 사라졌을 수 있어 재다운로드가 필요하다.

    Args:
        track_id: music_tracks.id
        download_dir: 클립을 저장할 디렉터리. None이면 시스템 임시 디렉터리.

    Returns:
        MusicTrack (local_clip_path에 재다운로드된 경로 포함)
    """
    from supabase import create_client

    sb = create_client(config.SUPABASE_URL, config.SUPABASE_SERVICE_KEY)
    resp = sb.table("music_tracks").select("*").eq("id", track_id).single().execute()
    row = resp.data
    if not row:
        raise RuntimeError(f"music_tracks에서 id={track_id}를 찾을 수 없습니다.")

    local_path = _download_clip_for_row(row, download_dir)
    return _row_to_track(row, local_path)


def get_scene_template(mood: str | None = None) -> list[dict]:
    """
    분석 시점(claude_analyzer.run_analysis)에 필요한 씬 슬롯 구조를
    music_tracks 풀에서 하나 골라 반환한다. 오디오 파일 다운로드는 필요 없다 —
    music_preprocessor.py가 이미 계산해 저장해둔 scenes 컬럼을 그대로 읽는다.

    실제 배경음악은 제작 시점(script_generator.run_generation)에 select_track()으로
    별도 랜덤 선택되므로, 이 함수는 씬 "모양"(개수/길이)만 빌려오는 것이고
    used_count는 증가시키지 않는다.

    Args:
        mood: 필터링할 mood. None이면 전체에서 랜덤.

    Returns:
        beat_sync.SceneSlot.to_dict() 리스트
        (claude_analyzer.run_analysis(scenes=...)에 그대로 전달 가능)

    Raises:
        RuntimeError: scenes가 저장된 활성 트랙이 없을 때
    """
    import random

    from supabase import create_client

    sb = create_client(config.SUPABASE_URL, config.SUPABASE_SERVICE_KEY)
    query = (
        sb.table("music_tracks")
        .select("title, artist, scenes")
        .eq("is_active", True)
        .not_.is_("scenes", "null")
    )
    if mood:
        query = query.eq("mood", mood)

    resp = query.limit(20).execute()
    candidates = resp.data

    if not candidates:
        if mood:
            logger.warning(f"mood='{mood}'이고 scenes가 있는 트랙이 없어 전체에서 선택합니다.")
            return get_scene_template(mood=None)
        raise RuntimeError(
            "scenes가 저장된 music_tracks 레코드가 없습니다. "
            "music_preprocessor.py로 트랙을 먼저 등록하세요."
        )

    row = random.choice(candidates)
    logger.info(f"씬 템플릿 차용: [{row['title']} - {row['artist']}] {len(row['scenes'])}개 씬")
    return row["scenes"]
