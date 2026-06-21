"""
pipeline.py

2단계 영상 제작 파이프라인 오케스트레이터.

실행 흐름:
1. Supabase에서 status='pending' 후보 조회
2. 스크립트 + 이미지 프롬프트 생성 (Claude)
3. 이미지 생성 (Gemini Image API = 나노바나나2)
4. 모션 영상 생성 (Kling API)
5. TTS 나레이션 생성 (ElevenLabs)
6. 최종 합성 (ffmpeg)
7. R2 업로드
8. Supabase 상태 업데이트 → status='produced'
"""

import os
import json
import traceback
from datetime import datetime
from pathlib import Path
from supabase import create_client, Client

from producer.script_generator import generate_shorts_script
from producer.image_generator import generate_scene_images, edit_product_background
from producer.kling_client import generate_scene_videos
from producer.tts_client import generate_scene_narrations
from producer.ffmpeg_composer import (
    create_srt_file,
    create_multilingual_srts,
    merge_scene_audios,
    compose_final_video,
)
from producer.r2_uploader import upload_video, upload_subtitles

WORK_DIR = "/tmp/kbeauty"


def get_supabase() -> Client:
    url = os.environ.get("SUPABASE_URL")
    key = os.environ.get("SUPABASE_KEY")
    if not url or not key:
        raise ValueError("SUPABASE_URL, SUPABASE_KEY 환경변수 필요")
    return create_client(url, key)


def fetch_pending_candidates(supabase: Client, limit: int = 1) -> list:
    """
    제작 대기 중인 콘텐츠 후보 조회.
    한 번에 하나씩 처리 (API 비용 관리).
    """
    response = (
        supabase.table("kbeauty_content_candidates")
        .select("*")
        .eq("status", "pending")
        .order("relevance_score", desc=True)  # 관련도 높은 것 먼저
        .limit(limit)
        .execute()
    )
    return response.data


def update_candidate_status(
    supabase: Client,
    candidate_id: str,
    status: str,
    extra: dict = None,
) -> None:
    """후보 상태 업데이트"""
    data = {"status": status}
    if extra:
        data.update(extra)

    supabase.table("kbeauty_content_candidates").update(data).eq(
        "id", candidate_id
    ).execute()
    print(f"상태 업데이트: {candidate_id[:8]}... → {status}")


def save_video_record(
    supabase: Client,
    candidate_id: str,
    video_data: dict,
    subtitles: dict,
    srt_r2_keys: dict,
) -> str:
    """kbeauty_videos 테이블에 제작 완료 영상 저장"""
    record = {
        "candidate_id": candidate_id,
        "video_r2_key": video_data.get("r2_key"),
        "video_url": video_data.get("video_url"),
        "subtitles": subtitles,  # JSONB: {"ko": "...", "en": "...", ...}
    }

    response = (
        supabase.table("kbeauty_videos").insert(record).execute()
    )
    video_id = response.data[0]["id"]
    print(f"영상 레코드 저장: video_id={video_id[:8]}...")
    return video_id


def produce_video(candidate: dict) -> dict:
    """
    단일 후보에 대한 전체 영상 제작 파이프라인.

    Returns:
        {"video_id": "...", "video_url": "...", "success": True}
    """
    candidate_id = candidate["id"]
    work_dir = f"{WORK_DIR}/{candidate_id}"
    Path(work_dir).mkdir(parents=True, exist_ok=True)

    print(f"\n{'='*60}")
    print(f"영상 제작 시작: {candidate_id[:8]}...")
    print(f"주제: {candidate.get('trend_topic', '')}")
    print(f"{'='*60}")

    # ─────────────────────────────────────────
    # 1단계: 스크립트 + 이미지 프롬프트 생성
    # ─────────────────────────────────────────
    print("\n[1/7] 스크립트 생성 중...")
    script_data = generate_shorts_script(candidate)

    # 스크립트 저장 (디버깅용)
    with open(f"{work_dir}/script.json", "w", encoding="utf-8") as f:
        json.dump(script_data, f, ensure_ascii=False, indent=2)

    scenes = script_data["scenes"]
    subtitles = script_data["subtitles"]
    print(f"  제목: {script_data['title']}")
    print(f"  장면 수: {len(scenes)}개")

    # ─────────────────────────────────────────
    # 2단계: 이미지 생성 (Gemini = 나노바나나2)
    # ─────────────────────────────────────────
    print("\n[2/7] 이미지 생성 중 (나노바나나2)...")

    # 제품 이미지 배경 합성 (올리브영 흰 배경 → 감성 배경)
    products = candidate.get("products", [])
    product_images = candidate.get("product_images", [])  # 1단계에서 수집한 이미지 URLs

    scenes = generate_scene_images(
        scenes=scenes,
        candidate_id=candidate_id,
        work_dir=WORK_DIR,
    )

    # ─────────────────────────────────────────
    # 3단계: 모션 영상 생성 (Kling)
    # ─────────────────────────────────────────
    print("\n[3/7] 모션 영상 생성 중 (Kling)...")
    scenes = generate_scene_videos(
        scenes=scenes,
        candidate_id=candidate_id,
        work_dir=WORK_DIR,
    )

    # ─────────────────────────────────────────
    # 4단계: TTS 나레이션 생성 (ElevenLabs)
    # ─────────────────────────────────────────
    print("\n[4/7] 나레이션 생성 중 (ElevenLabs)...")
    scenes = generate_scene_narrations(
        scenes=scenes,
        candidate_id=candidate_id,
        work_dir=WORK_DIR,
    )

    # ─────────────────────────────────────────
    # 5단계: 음성 합병 + SRT 자막 생성
    # ─────────────────────────────────────────
    print("\n[5/7] 음성 합병 + 자막 생성 중...")

    narration_path = f"{work_dir}/narration_full.mp3"
    merge_scene_audios(
        scenes=scenes,
        output_path=narration_path,
        work_dir=WORK_DIR,
        candidate_id=candidate_id,
    )

    ko_srt_path = f"{work_dir}/subtitle_ko.srt"
    create_srt_file(scenes=scenes, output_path=ko_srt_path)

    srt_paths = create_multilingual_srts(
        scenes=scenes,
        subtitles=subtitles,
        work_dir=WORK_DIR,
        candidate_id=candidate_id,
    )

    # ─────────────────────────────────────────
    # 6단계: ffmpeg 최종 합성
    # ─────────────────────────────────────────
    print("\n[6/7] 영상 최종 합성 중 (ffmpeg)...")
    final_video_path = f"{work_dir}/final.mp4"
    compose_final_video(
        scenes=scenes,
        narration_audio_path=narration_path,
        srt_path=ko_srt_path,
        output_path=final_video_path,
        work_dir=WORK_DIR,
        candidate_id=candidate_id,
    )

    # ─────────────────────────────────────────
    # 7단계: R2 업로드
    # ─────────────────────────────────────────
    print("\n[7/7] Cloudflare R2 업로드 중...")
    video_data = upload_video(
        local_path=final_video_path,
        candidate_id=candidate_id,
    )

    srt_r2_keys = upload_subtitles(
        srt_paths=srt_paths,
        candidate_id=candidate_id,
    )

    print(f"\n✅ 제작 완료!")
    print(f"   영상 URL: {video_data['video_url']}")

    return {
        "success": True,
        "candidate_id": candidate_id,
        "video_data": video_data,
        "subtitles": subtitles,
        "srt_r2_keys": srt_r2_keys,
        "script": script_data,
    }


def run_pipeline(max_items: int = 1) -> None:
    """
    파이프라인 메인 실행.
    APScheduler에서 주기적으로 호출.

    Args:
        max_items: 한 번에 처리할 최대 후보 수
    """
    supabase = get_supabase()

    candidates = fetch_pending_candidates(supabase, limit=max_items)

    if not candidates:
        print("제작할 후보가 없습니다.")
        return

    print(f"제작 대기 후보: {len(candidates)}개")

    for candidate in candidates:
        candidate_id = candidate["id"]

        # 상태를 producing으로 변경 (중복 처리 방지)
        update_candidate_status(supabase, candidate_id, "producing")

        try:
            result = produce_video(candidate)

            # 영상 레코드 저장
            video_id = save_video_record(
                supabase=supabase,
                candidate_id=candidate_id,
                video_data=result["video_data"],
                subtitles=result["subtitles"],
                srt_r2_keys=result["srt_r2_keys"],
            )

            # 후보 상태 produced로 업데이트
            update_candidate_status(
                supabase,
                candidate_id,
                "produced",
                {
                    "shorts_title": result["script"]["title"],
                    "shorts_script": result["script"]["script"],
                    "hook_line": result["script"]["hook_line"],
                },
            )

        except Exception as e:
            print(f"\n❌ 제작 실패: {candidate_id[:8]}...")
            print(traceback.format_exc())
            update_candidate_status(supabase, candidate_id, "failed")


if __name__ == "__main__":
    # 직접 실행 테스트
    import dotenv
    dotenv.load_dotenv()

    run_pipeline(max_items=1)
