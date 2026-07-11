"""
music_preprocessor.py
music/raw/ 폴더의 미처리 음악 파일들을 일괄 전처리한다.

처리 순서 (파일당):
  1. music/raw/{stem}.json 메타데이터 읽기
  2. beat_sync.compute_scene_slots() → start_offset, video_end, bpm 추출
  3. ffmpeg로 35초 클립 생성 (fade in/out 포함)
  4. 원본 + 클립을 R2에 업로드
  5. Supabase music_tracks 테이블에 저장
  6. 처리 완료된 파일을 music/processed/ 로 이동

사용법:
    # 전체 미처리 파일 일괄 처리
    python music_preprocessor.py

    # 특정 파일만 처리
    python music_preprocessor.py --file music/raw/sunday_morning.mp3

    # beat_sync 결과가 마음에 안 들 때 시작점 수동 지정
    python music_preprocessor.py --file music/raw/sunday_morning.mp3 --start-offset 42.5
"""
from __future__ import annotations

import argparse
import json
import logging
import os
import subprocess
import sys
import tempfile
from pathlib import Path

import boto3
from botocore.config import Config as BotoConfig

# 상위 디렉터리의 config, beat_sync 임포트
sys.path.insert(0, str(Path(__file__).parent.parent))
from config import config
from producer.beat_sync import compute_scene_slots, BeatSyncResult

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s %(levelname)s %(message)s",
)
logger = logging.getLogger(__name__)

MUSIC_RAW_DIR = Path(__file__).parent / "music" / "raw"

# R2 키 prefix
R2_MUSIC_PREFIX = "music"

# 목표 영상 길이 (beat_sync와 동일하게 맞춤)
TARGET_DURATION = 35.0
MIN_DURATION = 35.0
N_TRANSITIONS = 3  # 씬 슬롯 분할 수 (music_preprocessor에서는 참고용)

# 음악 볼륨 감쇄 (TTS 대비 -12dB, ffmpeg_composer와 동일)
MUSIC_VOLUME_DB = -12.0


# ---------------------------------------------------------------------------
# R2 클라이언트
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


def _upload_to_r2(r2_client, local_path: Path, r2_key: str, content_type: str) -> str:
    """파일을 R2에 업로드하고 R2 key를 반환."""
    size_mb = local_path.stat().st_size / 1024 / 1024
    logger.info(f"  R2 업로드: {r2_key} ({size_mb:.1f} MB)")
    with open(local_path, "rb") as f:
        r2_client.put_object(
            Bucket=config.R2_BUCKET_NAME,
            Key=r2_key,
            Body=f,
            ContentType=content_type,
        )
    return r2_key


# ---------------------------------------------------------------------------
# ffmpeg 클립 생성
# ---------------------------------------------------------------------------

def _make_clip(
    src: Path,
    dest: Path,
    start_offset: float,
    video_end: float,
    fade_in_ms: float,
    fade_out_ms: float,
) -> None:
    """
    원본 음악에서 [start_offset, video_end] 구간을 잘라내고
    fade in/out + 볼륨 감쇄(-12dB)를 적용해 dest에 저장.
    """
    clip_duration = video_end - start_offset
    fade_in_sec = fade_in_ms / 1000.0
    fade_out_sec = fade_out_ms / 1000.0
    fade_out_start = clip_duration - fade_out_sec
    volume_linear = 10 ** (MUSIC_VOLUME_DB / 20.0)

    filter_str = (
        f"atrim=start={start_offset}:end={video_end},"
        f"asetpts=PTS-STARTPTS,"
        f"afade=t=in:st=0:d={fade_in_sec:.4f},"
        f"afade=t=out:st={fade_out_start:.4f}:d={fade_out_sec:.4f},"
        f"volume={volume_linear:.6f}"
    )

    cmd = [
        "ffmpeg", "-y",
        "-i", str(src),
        "-af", filter_str,
        "-c:a", "libmp3lame", "-q:a", "2",
        str(dest),
    ]
    result = subprocess.run(cmd, capture_output=True, text=True)
    if result.returncode != 0:
        raise RuntimeError(
            f"ffmpeg 클립 생성 실패:\nSTDERR: {result.stderr}"
        )
    logger.info(
        f"  클립 생성: {start_offset:.2f}s ~ {video_end:.2f}s "
        f"({clip_duration:.2f}s), fade {fade_in_ms}ms/{fade_out_ms}ms"
    )


# ---------------------------------------------------------------------------
# Supabase 저장
# ---------------------------------------------------------------------------

def _save_to_supabase(meta: dict, beat: BeatSyncResult, original_key: str, clip_key: str) -> str:
    """music_tracks 테이블에 레코드를 삽입하고 생성된 id를 반환."""
    from supabase import create_client

    sb = create_client(config.SUPABASE_URL, config.SUPABASE_SERVICE_KEY)

    row = {
        "title": meta["title"],
        "artist": meta["artist"],
        "attribution": meta["attribution"],
        "thematic_url": meta["thematic_url"],
        "mood": meta["mood"],
        "bpm": round(beat.bpm, 2),
        "original_r2_key": original_key,
        "clip_r2_key": clip_key,
        "start_offset": round(beat.start_offset, 3),
        "video_end": round(beat.video_end, 3),
        "fade_in_ms": beat.fade_in_ms,
        "fade_out_ms": beat.fade_out_ms,
        # claude_analyzer.run_analysis()가 그대로 쓸 수 있는 씬 슬롯 구조.
        # 여기서 저장해두면 분석 시점에 매번 beat_sync를 다시 돌릴 필요가 없다.
        "scenes": [s.to_dict() for s in beat.scenes],
        "used_count": 0,
        "is_active": True,
    }

    resp = sb.table("music_tracks").insert(row).execute()
    track_id = resp.data[0]["id"]
    logger.info(f"  Supabase 저장 완료: music_tracks id={track_id}")
    return track_id


# ---------------------------------------------------------------------------
# 단일 파일 처리
# ---------------------------------------------------------------------------

def process_file(mp3_path: Path, start_offset_override: float | None = None) -> None:
    """
    단일 음악 파일을 전처리한다.

    Args:
        mp3_path: music/raw/ 안의 mp3 파일 경로
        start_offset_override: 수동 시작점 지정 (None이면 beat_sync 자동 탐지)
    """
    stem = mp3_path.stem
    json_path = mp3_path.parent / (stem + ".json")

    # 메타데이터 확인
    if not json_path.exists():
        logger.error(f"메타데이터 JSON 없음: {json_path}. music_add.py로 먼저 등록하세요.")
        return

    meta = json.loads(json_path.read_text(encoding="utf-8"))
    logger.info(f"=== 처리 시작: {meta['title']} - {meta['artist']} ===")

    # 이미 처리된 파일인지 확인 (Supabase에 동일 파일명이 있는지)
    try:
        from supabase import create_client
        sb = create_client(config.SUPABASE_URL, config.SUPABASE_SERVICE_KEY)
        existing = sb.table("music_tracks").select("id").eq(
            "original_r2_key", f"{R2_MUSIC_PREFIX}/original/{mp3_path.name}"
        ).execute()
        if existing.data:
            logger.warning(f"이미 처리된 파일입니다 (id={existing.data[0]['id']}). 건너뜁니다.")
            return
    except Exception as e:
        logger.warning(f"중복 확인 실패 (계속 진행): {e}")

    with tempfile.TemporaryDirectory() as tmp:
        tmp_path = Path(tmp)
        clip_path = tmp_path / f"{stem}_clip.mp3"

        # Step 1: beat_sync
        logger.info("beat_sync 분석 중...")
        beat = compute_scene_slots(
            audio_path=str(mp3_path),
            target_duration=TARGET_DURATION,
            min_duration=MIN_DURATION,
            n_transitions=N_TRANSITIONS,
            start_offset=start_offset_override,
        )
        logger.info(
            f"  BPM={beat.bpm:.1f}, "
            f"start={beat.start_offset:.2f}s, "
            f"end={beat.video_end:.2f}s, "
            f"반복점수={beat.repetition_score:.3f}"
        )

        # Step 2: 35초 클립 생성
        _make_clip(
            src=mp3_path,
            dest=clip_path,
            start_offset=beat.start_offset,
            video_end=beat.video_end,
            fade_in_ms=beat.fade_in_ms,
            fade_out_ms=beat.fade_out_ms,
        )

        # Step 3: R2 업로드
        r2 = _get_r2_client()
        original_key = f"{R2_MUSIC_PREFIX}/original/{mp3_path.name}"
        clip_key = f"{R2_MUSIC_PREFIX}/clips/{stem}_clip.mp3"

        _upload_to_r2(r2, mp3_path, original_key, "audio/mpeg")
        _upload_to_r2(r2, clip_path, clip_key, "audio/mpeg")

        # Step 4: Supabase 저장
        track_id = _save_to_supabase(meta, beat, original_key, clip_key)

    # Step 5: 로컬 파일 삭제 (원본은 R2에 저장됨)
    mp3_path.unlink()
    json_path.unlink()

    logger.info(
        f"=== 완료: {meta['title']} → track_id={track_id} "
        f"(로컬 파일 삭제, R2에 보관) ==="
    )


# ---------------------------------------------------------------------------
# 전체 처리
# ---------------------------------------------------------------------------

def process_all() -> None:
    """music/raw/ 폴더의 미처리 파일 전체 처리."""
    if not MUSIC_RAW_DIR.exists():
        logger.error(f"music/raw/ 폴더가 없습니다: {MUSIC_RAW_DIR}")
        sys.exit(1)

    mp3_files = sorted(MUSIC_RAW_DIR.glob("*.mp3"))
    if not mp3_files:
        logger.info("처리할 파일이 없습니다. music_add.py로 먼저 파일을 등록하세요.")
        return

    logger.info(f"처리 대상: {len(mp3_files)}개 파일")
    success, fail = 0, 0

    for mp3_path in mp3_files:
        try:
            process_file(mp3_path)
            success += 1
        except Exception as e:
            logger.error(f"처리 실패: {mp3_path.name} — {e}", exc_info=True)
            fail += 1

    logger.info(f"=== 전체 완료: 성공 {success}개 / 실패 {fail}개 ===")


# ---------------------------------------------------------------------------
# CLI
# ---------------------------------------------------------------------------

def main() -> None:
    parser = argparse.ArgumentParser(description="ShortsBot 음악 전처리기")
    parser.add_argument(
        "--file", type=str, default=None,
        help="특정 파일만 처리 (생략 시 music/raw/ 전체 처리)"
    )
    parser.add_argument(
        "--start-offset", type=float, default=None,
        help="beat_sync 시작점 수동 지정(초). beat_sync 결과가 마음에 안 들 때 사용."
    )
    args = parser.parse_args()

    if args.file:
        mp3_path = Path(args.file).expanduser().resolve()
        if not mp3_path.exists():
            logger.error(f"파일을 찾을 수 없습니다: {mp3_path}")
            sys.exit(1)
        process_file(mp3_path, start_offset_override=args.start_offset)
    else:
        if args.start_offset is not None:
            logger.warning("--start-offset은 --file과 함께 사용해야 합니다. 무시합니다.")
        process_all()


if __name__ == "__main__":
    main()
