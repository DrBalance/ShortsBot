"""
tts_client.py

ElevenLabs API로 한국어 나레이션 음성 생성.

왼손AI가 빠나나AI에서 ElevenLabs를 선택한 이유:
- 한국어 발음 자연스러움
- 감정 표현 풍부
- 목소리 일관성 유지 가능 (voice_id 고정)
- 버그 이슈 있었지만 꼼수로 해결 완료

YouTube Auto-Dubbing 전략:
- 한국어 나레이션만 ElevenLabs로 제작
- 태국어/베트남어/인도네시아어는 YouTube Auto-Dubbing에 위임
- 자막 텍스트는 Claude 번역으로 별도 제공 (정확도 보완)
"""

import os
import httpx
from pathlib import Path
from typing import Optional
from dotenv import load_dotenv
load_dotenv()

ELEVENLABS_API_URL = "https://api.elevenlabs.io/v1"


def generate_narration(
    text: str,
    output_path: str,
    voice_id: Optional[str] = None,
    stability: float = 0.5,
    similarity_boost: float = 0.75,
    style: float = 0.3,
    speed: float = 1.0,
) -> str:
    """
    한국어 나레이션 음성 생성.

    Args:
        text: 나레이션 텍스트 (한국어)
        output_path: 저장할 .mp3 파일 경로
        voice_id: ElevenLabs 보이스 ID (없으면 환경변수 사용)
        stability: 목소리 안정성 (0~1, 높을수록 일관됨)
        similarity_boost: 원본 목소리 유사도 (0~1)
        style: 감정 표현 강도 (0~1)
        speed: 말하기 속도 (0.7~1.2 권장)

    Returns:
        저장된 음성 파일 경로
    """
    api_key = os.environ.get("ELEVENLABS_API_KEY")
    if not api_key:
        raise ValueError("ELEVENLABS_API_KEY 환경변수가 없습니다")

    voice_id = voice_id or os.environ.get("ELEVENLABS_VOICE_ID")
    if not voice_id:
        raise ValueError("ELEVENLABS_VOICE_ID 환경변수가 없습니다")

    headers = {
        "xi-api-key": api_key,
        "Content-Type": "application/json",
    }

    payload = {
        "text": text,
        "model_id": "eleven_multilingual_v2",  # 한국어 지원 최신 모델
        "voice_settings": {
            "stability": stability,
            "similarity_boost": similarity_boost,
            "style": style,
            "use_speaker_boost": True,
        },
    }

    # speed 파라미터 (1.0 아닐 때만 추가)
    if speed != 1.0:
        payload["voice_settings"]["speed"] = speed

    url = f"{ELEVENLABS_API_URL}/text-to-speech/{voice_id}"

    with httpx.Client(timeout=60.0) as client:
        response = client.post(url, headers=headers, json=payload)

    if response.status_code != 200:
        raise RuntimeError(
            f"ElevenLabs API 오류 {response.status_code}: {response.text}"
        )

    Path(output_path).parent.mkdir(parents=True, exist_ok=True)

    with open(output_path, "wb") as f:
        f.write(response.content)

    print(f"나레이션 생성 완료: {output_path} ({len(response.content) / 1024:.1f}KB)")
    return output_path


def get_audio_duration(audio_path: str) -> float:
    """
    음성 파일 길이 반환 (초).
    ffprobe 사용.
    """
    import subprocess
    import json as json_module

    result = subprocess.run(
        [
            "ffprobe",
            "-v", "quiet",
            "-print_format", "json",
            "-show_format",
            audio_path,
        ],
        capture_output=True,
        text=True,
    )

    if result.returncode != 0:
        raise RuntimeError(f"ffprobe 실패: {result.stderr}")

    info = json_module.loads(result.stdout)
    return float(info["format"]["duration"])


def list_available_voices() -> list:
    """
    ElevenLabs에서 사용 가능한 목소리 목록 반환.
    한국어 지원 목소리 필터링에 사용.
    """
    api_key = os.environ.get("ELEVENLABS_API_KEY")
    if not api_key:
        raise ValueError("ELEVENLABS_API_KEY 환경변수가 없습니다")

    with httpx.Client() as client:
        response = client.get(
            f"{ELEVENLABS_API_URL}/voices",
            headers={"xi-api-key": api_key},
        )

    if response.status_code != 200:
        raise RuntimeError(f"목소리 목록 조회 실패: {response.text}")

    voices = response.json().get("voices", [])
    return [
        {
            "voice_id": v["voice_id"],
            "name": v["name"],
            "labels": v.get("labels", {}),
        }
        for v in voices
    ]


def generate_scene_narrations(
    scenes: list,
    candidate_id: str,
    work_dir: str = "/tmp/kbeauty",
) -> list:
    """
    모든 장면의 나레이션 음성 생성.

    Args:
        scenes: script_generator scenes (narration 포함)
        candidate_id: 파일명 구분용 ID
        work_dir: 임시 작업 디렉토리

    Returns:
        scenes에 audio_path가 추가된 리스트
    """
    results = []

    for scene in scenes:
        order = scene["order"]
        narration = scene.get("narration", "")

        if not narration:
            print(f"장면 {order}: 나레이션 없음, 스킵")
            results.append({**scene, "audio_path": None})
            continue

        audio_path = f"{work_dir}/{candidate_id}/scene_{order}_narration.mp3"

        try:
            generate_narration(
                text=narration,
                output_path=audio_path,
                speed=1.05,  # 쇼츠는 살짝 빠르게
            )
            duration = get_audio_duration(audio_path)
            results.append({
                **scene,
                "audio_path": audio_path,
                "actual_duration_sec": duration,
            })
            print(f"장면 {order} 나레이션: {duration:.1f}초")

        except Exception as e:
            print(f"장면 {order} 나레이션 실패: {e}")
            results.append({**scene, "audio_path": None})

    return results

if __name__ == "__main__":
    print("나레이션 생성 테스트 중...")
    path = generate_narration(
        text="마스크팩 매일 쓰면 진짜 피부 달라질까요? 저 직접 7일 동안 해봤어요!",
        output_path="/tmp/test_narration.mp3",
    )
    print(f"완료: {path}")

    duration = get_audio_duration("/tmp/test_narration.mp3")
    print(f"음성 길이: {duration:.1f}초")
