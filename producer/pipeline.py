"""
producer/pipeline.py
Phase 2-4: candidate 하나를 curated 상태부터 최종 R2 업로드까지 잇는 오케스트레이터.

파이프라인 위치:
  claude_analyzer.run_analysis()                → candidate 저장 (status=pending)
  큐레이터 수동 입력 + db.mark_candidate_curated() → status=curated
    ↓
  producer.pipeline.run_production(candidate_id)
    1. script_generator.run_generation()      → Kling/Vidu 영상 + 음악 선택 (status=raw_video_ready)
    2. video_url(임시 CDN, 24h 유효) 로컬 다운로드
    3. tts_timing.fit_all_scenes()            → 씬별 나레이션 mp3
    4. ffmpeg_composer.compose()              → final.mp4 + en.srt
    5. uploader.upload_from_composer_result() → R2 업로드 + status=video_ready
    ↓
  (Phase 3) youtube_uploader.py가 이어받아 유튜브에 게시

재시도 정책:
  각 단계 실패 시 status를 tts_failed/compose_failed/upload_failed로 남기고 예외를 다시 던진다.
  자동 재시도는 하지 않는다 — run_production_batch()는 실패한 candidate를 건너뛰고 계속 진행한다.
  status가 이미 raw_video_ready인 candidate는 1단계(Kling/Vidu 생성)를 건너뛴다
  (영상 생성 API는 Trial Package 등 쿼터 제약이 있어, 재시도 때마다 다시 호출하지 않기 위함).
"""
from __future__ import annotations

import logging
import os
import tempfile

import requests

from config import config
from db import supabase_client as db
from producer.script_generator import run_generation
from producer.tts_timing import fit_all_scenes, save_scene_audio
from producer.ffmpeg_composer import compose, ComposerInput
from producer.music_selector import get_track_clip
from uploader.uploader import upload_from_composer_result, UploadResult

logger = logging.getLogger(__name__)

# run_production_batch가 배치 조회 시 우선순위대로 훑는 진입 상태.
# raw_video_ready를 먼저 처리해 영상 생성 API 재호출 없이 마무리되는 건부터 끝낸다.
ENTRY_STATUSES = ("raw_video_ready", "curated")


def _download_video(video_url: str, dest_path: str) -> str:
    """Kling/Vidu 임시 CDN URL을 로컬로 다운로드."""
    r = requests.get(video_url, stream=True, timeout=60)
    r.raise_for_status()
    with open(dest_path, "wb") as f:
        for chunk in r.iter_content(chunk_size=1 << 20):
            f.write(chunk)
    return dest_path


def run_production(candidate_id: str) -> UploadResult:
    """
    candidate 하나를 완제품 영상(R2 업로드)까지 처리.

    status가 'curated'면 1단계(Kling/Vidu 영상 생성)부터 시작하고,
    이미 'raw_video_ready'면 1단계를 건너뛰고 저장된 video_url로 이어서 진행한다.

    Args:
        candidate_id: kbeauty_content_candidates.id

    Returns:
        UploadResult
    """
    candidate = db.get_candidate(candidate_id)
    if not candidate:
        raise ValueError(f"candidate {candidate_id}를 찾을 수 없습니다.")

    status = candidate.get("status")
    if status not in ENTRY_STATUSES:
        raise ValueError(
            f"candidate {candidate_id}: status={status} — "
            f"{ENTRY_STATUSES} 상태에서만 run_production을 실행할 수 있습니다."
        )

    if status == "curated":
        run_generation(candidate_id)
        candidate = db.get_candidate(candidate_id)  # video_url, music_track_id 반영된 값 재조회

    video_url = candidate.get("video_url")
    scenes = candidate.get("scenes") or []
    if not video_url:
        raise ValueError(f"candidate {candidate_id}: video_url이 없습니다 (raw_video_ready 상태 확인 필요).")
    if not scenes:
        raise ValueError(f"candidate {candidate_id}: scenes가 없습니다.")

    with tempfile.TemporaryDirectory() as workdir:
        local_video = os.path.join(workdir, "raw_video.mp4")
        _download_video(video_url, local_video)

        # 2단계: TTS 생성
        try:
            tts_results = fit_all_scenes(scenes, config.ELEVENLABS_VOICE_ID)
        except Exception as e:
            db.update_candidate_status(candidate_id, "tts_failed", {"error": str(e)})
            raise

        tts_paths = []
        for r in tts_results:
            path = os.path.join(workdir, f"scene_{r.scene_index}.mp3")
            save_scene_audio(r, path)
            tts_paths.append(path)

        # 3단계: ffmpeg 합성
        composer_kwargs = dict(
            video_clips=[local_video],
            tts_clips=tts_paths,
            scenes=scenes,
            word_timestamps=[r.word_timestamps for r in tts_results],
            output_dir=workdir,
        )

        music_mode = config.MUSIC_MODE
        if music_mode == "embedded":
            track_id = candidate.get("music_track_id")
            if not track_id:
                error = "MUSIC_MODE=embedded인데 candidate에 music_track_id가 없습니다."
                db.update_candidate_status(candidate_id, "compose_failed", {"error": error})
                raise ValueError(f"candidate {candidate_id}: {error}")

            # run_generation() 시점의 로컬 클립은 임시 파일이라 이 시점엔 이미
            # 사라졌을 수 있으므로 같은 track_id로 다시 받는다 (used_count는 증가시키지 않음).
            # get_track_clip()이 내려주는 파일은 music_preprocessor.py가 이미
            # trim+fade+볼륨까지 처리해둔 클립이므로 music_preprocessed=True로
            # 표시해 ffmpeg_composer가 다시 트림하지 않도록 한다.
            track = get_track_clip(track_id, download_dir=workdir)
            composer_kwargs.update(
                music_path=track.local_clip_path,
                music_preprocessed=True,
            )

        try:
            composer_result = compose(ComposerInput(**composer_kwargs), music_mode=music_mode)
        except Exception as e:
            db.update_candidate_status(candidate_id, "compose_failed", {"error": str(e)})
            raise

        # 4단계: R2 업로드 + Supabase 최종 반영
        try:
            upload_result = upload_from_composer_result(candidate_id, composer_result)
        except Exception as e:
            db.update_candidate_status(candidate_id, "upload_failed", {"error": str(e)})
            raise

    logger.info(f"제작 파이프라인 완료: candidate={candidate_id}")
    return upload_result


def run_production_batch(limit: int = 5) -> list[UploadResult]:
    """
    curated 또는 raw_video_ready 상태 candidate를 최대 limit개 처리.
    스케줄러(producer/scheduler.py)와 main.py의 수동 트리거가 공용으로 쓰는 진입점.
    """
    candidates: list[dict] = []
    for status in ENTRY_STATUSES:
        candidates += db.get_candidates_by_status(status, limit=limit)
    candidates = candidates[:limit]

    if not candidates:
        logger.info("처리할 curated/raw_video_ready candidate 없음")
        return []

    results: list[UploadResult] = []
    for candidate in candidates:
        cid = candidate["id"]
        try:
            results.append(run_production(cid))
        except Exception as e:
            logger.error(f"제작 파이프라인 실패: candidate={cid}, error={e}")
            continue

    logger.info(f"제작 배치 완료: {len(results)}/{len(candidates)}개 성공")
    return results


if __name__ == "__main__":
    import sys

    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")

    if len(sys.argv) == 2:
        result = run_production(sys.argv[1])
        print(f"완료: video_url={result.video_url}, srt_url={result.srt_url}")
    else:
        print("사용법: python producer/pipeline.py <candidate_id>")
