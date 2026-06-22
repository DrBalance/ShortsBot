"""
ffmpeg_composer.py

ffmpeg로 최종 쇼츠 영상 합성:
- 장면별 영상 클립 연결
- 나레이션 음성 합성
- 한국어 자막 삽입
- 9:16 세로형 MP4 출력

자막 전략:
- 한국어: ffmpeg로 영상에 직접 삽입 (SRT)
- 다국어: 별도 SRT 파일로 저장 → YouTube Studio에서 업로드
"""

import os
import json
import subprocess
from pathlib import Path
from typing import Optional


def create_srt_file(scenes: list, output_path: str) -> str:
    """
    장면별 나레이션으로 SRT 자막 파일 생성.

    Args:
        scenes: actual_duration_sec가 포함된 scenes 리스트
        output_path: .srt 파일 저장 경로

    Returns:
        SRT 파일 경로
    """
    srt_content = []
    current_time = 0.0

    for i, scene in enumerate(scenes, 1):
        narration = scene.get("narration", "")
        duration = scene.get("actual_duration_sec", scene.get("duration_sec", 5))

        if not narration:
            current_time += duration
            continue

        start = _sec_to_srt_time(current_time)
        end = _sec_to_srt_time(current_time + duration)

        srt_content.append(f"{i}")
        srt_content.append(f"{start} --> {end}")
        srt_content.append(narration)
        srt_content.append("")

        current_time += duration

    Path(output_path).parent.mkdir(parents=True, exist_ok=True)

    with open(output_path, "w", encoding="utf-8") as f:
        f.write("\n".join(srt_content))

    print(f"SRT 자막 생성: {output_path}")
    return output_path


def create_multilingual_srts(
    scenes: list,
    subtitles: dict,
    work_dir: str,
    candidate_id: str,
) -> dict:
    """
    다국어 SRT 파일 생성.

    Args:
        scenes: 장면 리스트 (타이밍 정보 포함)
        subtitles: {"ko": "...", "en": "...", "th": "...", ...}
        work_dir: 작업 디렉토리
        candidate_id: 파일명 구분용 ID

    Returns:
        {"ko": "경로", "en": "경로", ...}
    """
    srt_paths = {}

    for lang, full_text in subtitles.items():
        # 전체 자막을 장면 수로 분할 (단순 분할)
        lines = [line.strip() for line in full_text.split(".") if line.strip()]

        srt_path = f"{work_dir}/{candidate_id}/subtitle_{lang}.srt"
        current_time = 0.0
        srt_content = []

        for i, scene in enumerate(scenes):
            duration = scene.get("actual_duration_sec", scene.get("duration_sec", 5))
            text = lines[i] if i < len(lines) else ""

            if text:
                start = _sec_to_srt_time(current_time)
                end = _sec_to_srt_time(current_time + duration)
                srt_content.extend([
                    str(i + 1),
                    f"{start} --> {end}",
                    text,
                    "",
                ])

            current_time += duration

        Path(srt_path).parent.mkdir(parents=True, exist_ok=True)
        with open(srt_path, "w", encoding="utf-8") as f:
            f.write("\n".join(srt_content))

        srt_paths[lang] = srt_path
        print(f"자막 생성 ({lang}): {srt_path}")

    return srt_paths


def _sec_to_srt_time(seconds: float) -> str:
    """초를 SRT 타임코드 형식으로 변환 (HH:MM:SS,mmm)"""
    hours = int(seconds // 3600)
    minutes = int((seconds % 3600) // 60)
    secs = int(seconds % 60)
    millis = int((seconds - int(seconds)) * 1000)
    return f"{hours:02d}:{minutes:02d}:{secs:02d},{millis:03d}"


def concatenate_scene_clips(
    scenes: list,
    output_path: str,
    work_dir: str,
    candidate_id: str,
) -> str:
    """
    장면별 영상 클립을 하나로 연결.
    영상 없는 장면은 이미지로 대체 (ffmpeg 슬라이드쇼).

    Args:
        scenes: video_path 또는 character_image_path가 있는 scenes
        output_path: 출력 영상 경로
        work_dir: 임시 파일 디렉토리
        candidate_id: ID

    Returns:
        연결된 영상 경로
    """
    concat_list_path = f"{work_dir}/{candidate_id}/concat_list.txt"
    clip_paths = []

    for scene in scenes:
        order = scene["order"]
        duration = scene.get("actual_duration_sec", scene.get("duration_sec", 5))

        video_path = scene.get("video_path")
        image_path = scene.get("character_image_path")

        if video_path and Path(video_path).exists():
            # 영상 클립 사용
            clip_paths.append(video_path)

        elif image_path and Path(image_path).exists():
            # 이미지를 영상으로 변환 (켄번즈 효과)
            img_video_path = f"{work_dir}/{candidate_id}/scene_{order}_img.mp4"
            _image_to_video_clip(image_path, img_video_path, duration)
            clip_paths.append(img_video_path)

        else:
            print(f"장면 {order}: 영상/이미지 없음, 검정 화면 사용")
            black_path = f"{work_dir}/{candidate_id}/scene_{order}_black.mp4"
            _create_black_clip(black_path, duration)
            clip_paths.append(black_path)

    # concat 리스트 파일 생성
    Path(concat_list_path).parent.mkdir(parents=True, exist_ok=True)
    with open(concat_list_path, "w") as f:
        for clip in clip_paths:
            f.write(f"file '{clip}'\n")

    # ffmpeg concat
    cmd = [
        "ffmpeg", "-y",
        "-f", "concat",
        "-safe", "0",
        "-i", concat_list_path,
        "-c:v", "libx264",
        "-preset", "fast",
        "-crf", "23",
        "-vf", "scale=1080:1920:force_original_aspect_ratio=decrease,"
               "pad=1080:1920:(ow-iw)/2:(oh-ih)/2",
        "-r", "30",
        output_path,
    ]

    result = subprocess.run(cmd, capture_output=True, text=True)
    if result.returncode != 0:
        raise RuntimeError(f"ffmpeg concat 실패: {result.stderr}")

    print(f"영상 클립 연결 완료: {output_path}")
    return output_path


def _image_to_video_clip(
    image_path: str,
    output_path: str,
    duration: float,
) -> str:
    """
    이미지를 켄번즈 효과의 영상 클립으로 변환.
    Kling 영상이 없는 장면에 사용.
    """
    # 줌인 효과 (켄번즈)
    zoom_filter = (
        f"zoompan=z='min(zoom+0.0015,1.5)':d={int(duration * 25)}:s=1080x1920"
    )

    cmd = [
        "ffmpeg", "-y",
        "-loop", "1",
        "-i", image_path,
        "-vf", zoom_filter,
        "-c:v", "libx264",
        "-t", str(duration),
        "-pix_fmt", "yuv420p",
        output_path,
    ]

    result = subprocess.run(cmd, capture_output=True, text=True)
    if result.returncode != 0:
        raise RuntimeError(f"이미지→영상 변환 실패: {result.stderr}")

    return output_path


def _create_black_clip(output_path: str, duration: float) -> str:
    """검정 화면 클립 생성 (fallback용)"""
    cmd = [
        "ffmpeg", "-y",
        "-f", "lavfi",
        "-i", "color=black:size=1080x1920:rate=30",
        "-t", str(duration),
        "-c:v", "libx264",
        output_path,
    ]
    subprocess.run(cmd, capture_output=True)
    return output_path


def compose_final_video(
    scenes: list,
    narration_audio_path: str,
    srt_path: str,
    output_path: str,
    work_dir: str,
    candidate_id: str,
) -> str:
    """
    최종 쇼츠 영상 합성.
    영상 클립 + 나레이션 음성 + 한국어 자막 → 최종 MP4

    Args:
        scenes: 완성된 scenes (video_path, audio_path 포함)
        narration_audio_path: 전체 나레이션 합본 음성
        srt_path: 한국어 SRT 자막 경로
        output_path: 최종 MP4 저장 경로
        work_dir: 임시 디렉토리
        candidate_id: ID

    Returns:
        최종 영상 파일 경로
    """
    # 1. 영상 클립 연결
    concat_path = f"{work_dir}/{candidate_id}/concat_raw.mp4"
    concatenate_scene_clips(scenes, concat_path, work_dir, candidate_id)

    # 2. 자막 스타일 (쇼츠용 - 화면 중하단, 굵은 폰트)
    subtitle_filter = (
        f"subtitles={srt_path}:force_style='"
        "FontName=Pretendard,"
        "FontSize=13,"
        "Bold=1,"
        "PrimaryColour=&H00FFFFFF,"
        "OutlineColour=&H00000000,"
        "Outline=1,"
        "Shadow=0,"
        "Alignment=2,"  # 하단 중앙
        "MarginV=60,"    # 하단 여백
        "Spacing=2"
        "'"
    )
    
       # 3. 영상 + 음성 + 자막 합성
    Path(output_path).parent.mkdir(parents=True, exist_ok=True)

    cmd = [
        "ffmpeg", "-y",
        "-i", concat_path,
        "-i", narration_audio_path,
        "-vf", subtitle_filter,
        "-c:v", "libx264",
        "-preset", "medium",
        "-crf", "20",
        "-c:a", "aac",
        "-b:a", "192k",
        "-shortest",  # 음성/영상 중 짧은 것에 맞춤
        "-movflags", "+faststart",  # 스트리밍 최적화
        output_path,
    ]

    result = subprocess.run(cmd, capture_output=True, text=True)
    if result.returncode != 0:
        raise RuntimeError(f"최종 합성 실패: {result.stderr}")

    file_size_mb = Path(output_path).stat().st_size / (1024 * 1024)
    print(f"최종 영상 완성: {output_path} ({file_size_mb:.1f}MB)")
    return output_path


def merge_scene_audios(
    scenes: list,
    output_path: str,
    work_dir: str,
    candidate_id: str,
) -> str:
    """
    장면별 개별 음성 파일을 하나의 나레이션 파일로 합병.

    Args:
        scenes: audio_path가 포함된 scenes
        output_path: 합병된 음성 파일 경로
        work_dir: 임시 디렉토리
        candidate_id: ID

    Returns:
        합병된 음성 파일 경로
    """
    audio_list_path = f"{work_dir}/{candidate_id}/audio_list.txt"
    audio_paths = []

    for scene in scenes:
        audio_path = scene.get("audio_path")
        if audio_path and Path(audio_path).exists():
            audio_paths.append(audio_path)

    if not audio_paths:
        raise ValueError("합병할 음성 파일이 없습니다")

    Path(audio_list_path).parent.mkdir(parents=True, exist_ok=True)
    with open(audio_list_path, "w") as f:
        for ap in audio_paths:
            f.write(f"file '{ap}'\n")

    cmd = [
        "ffmpeg", "-y",
        "-f", "concat",
        "-safe", "0",
        "-i", audio_list_path,
        "-c:a", "copy",
        output_path,
    ]

    result = subprocess.run(cmd, capture_output=True, text=True)
    if result.returncode != 0:
        raise RuntimeError(f"음성 합병 실패: {result.stderr}")

    print(f"나레이션 합병 완료: {output_path}")
    return output_path

if __name__ == "__main__":
    import os
    from dotenv import load_dotenv
    load_dotenv()

    # 앞서 생성한 파일들 사용
    test_image = "/tmp/test_character.png"
    test_audio = "/tmp/test_narration.mp3"
    work_dir = "/tmp/kbeauty"
    candidate_id = "test_001"

    # 테스트용 scenes 구성
    scenes = [
        {
            "order": 1,
            "duration_sec": 6,
            "narration": "마스크팩 매일 쓰면 진짜 피부 달라질까요? 저 직접 7일 동안 해봤어요!",
            "actual_duration_sec": 5.9,
            "character_image_path": test_image,
            "location_image_path": None,
            "video_path": None,
            "audio_path": test_audio,
        }
    ]

    print("1. SRT 자막 생성 중...")
    srt_path = f"{work_dir}/{candidate_id}/subtitle_ko.srt"
    create_srt_file(scenes=scenes, output_path=srt_path)
    print(f"   완료: {srt_path}")

    print("2. 나레이션 합병 중...")
    narration_path = f"{work_dir}/{candidate_id}/narration_full.mp3"
    merge_scene_audios(
        scenes=scenes,
        output_path=narration_path,
        work_dir=work_dir,
        candidate_id=candidate_id,
    )

    print("3. 최종 영상 합성 중...")
    final_path = f"{work_dir}/{candidate_id}/final.mp4"
    compose_final_video(
        scenes=scenes,
        narration_audio_path=narration_path,
        srt_path=srt_path,
        output_path=final_path,
        work_dir=work_dir,
        candidate_id=candidate_id,
    )

    print(f"\n✅ 완료! 영상 확인:")
    print(f"   open {final_path}")