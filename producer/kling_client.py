"""
kling_client.py

Kling API로 이미지 → 모션 영상 생성.
새로운 API Key 방식 (Bearer 토큰) 사용.

왼손AI 선택 기준:
- Kling 3.0: 화질 우수, 자연스러운 연기 톤 → K뷰티 쇼츠에 적합
- SeeDance 2.0: 액션씬, 멀티컷 전환에 강함
"""

import os
import time
import httpx
import base64
from pathlib import Path
from typing import Optional
from dotenv import load_dotenv

load_dotenv()

KLING_API_URL = "https://api.klingai.com"


def _get_headers() -> dict:
    """Kling API 요청 헤더 반환"""
    api_key = os.environ.get("KLING_API_KEY")
    if not api_key:
        raise ValueError("KLING_API_KEY 환경변수가 없습니다")

    return {
        "Authorization": f"Bearer {api_key}",
        "Content-Type": "application/json",
    }


def _image_to_base64(image_path: str) -> str:
    """이미지 파일을 base64 문자열로 변환"""
    with open(image_path, "rb") as f:
        return base64.b64encode(f.read()).decode()


def create_image_to_video(
    character_image_path: str,
    action_prompt: str,
    output_path: str,
    location_image_path: Optional[str] = None,
    duration: int = 5,
    aspect_ratio: str = "9:16",
    model: str = "kling-v1-6",
) -> str:
    """
    이미지 → 모션 영상 생성 (Kling Image to Video).

    Args:
        character_image_path: 캐릭터 이미지 경로 (단색 배경)
        action_prompt: 모션 설명 프롬프트 (영어)
        output_path: 저장할 .mp4 파일 경로
        location_image_path: 배경 이미지 경로 (선택)
            - 지정 시 image_tail로 전달 → 캐릭터~배경 사이를 자연스럽게 보간
            - image_tail은 pro 모드에서만 지원됨 → 자동으로 mode=pro 전환
        duration: 영상 길이 초 (5 또는 10)
        aspect_ratio: 화면 비율 (쇼츠는 9:16)
        model: Kling 모델 버전

    Returns:
        저장된 영상 파일 경로

    Note:
        image_tail 지원 여부 (Kling 공식 기준):
            kling-v1-5 std  → ❌  kling-v1-5 pro  → ✅
            kling-v1-6 std  → ❌  kling-v1-6 pro  → ✅
            kling-v2-master → ✅ (모드 구분 없음)
        location_image_path 없이 캐릭터 이미지만 사용하는 경우 std로 충분.
    """
    char_b64 = _image_to_base64(character_image_path)
    use_image_tail = bool(
        location_image_path and Path(location_image_path).exists()
    )

    # image_tail은 pro 모드에서만 동작 → 자동 전환
    # kling-v2-master는 모드 파라미터 없이도 image_tail 지원
    if use_image_tail and model != "kling-v2-master":
        mode = "pro"
        print(f"  [Kling] image_tail 사용 → mode 자동 전환: std → pro")
    else:
        mode = "std"

    payload = {
        "model_name": model,
        "mode": mode,
        "image": char_b64,
        "prompt": action_prompt,
        "duration": str(duration),
        "aspect_ratio": aspect_ratio,
        "cfg_scale": 0.5,
    }

    # 로케이션 이미지를 끝 프레임으로 추가 (캐릭터→배경 보간)
    if use_image_tail:
        loc_b64 = _image_to_base64(location_image_path)
        payload["image_tail"] = loc_b64
        print(f"  [Kling] image(캐릭터) + image_tail(배경) 보간 모드")

    with httpx.Client(timeout=30.0) as client:
        response = client.post(
            f"{KLING_API_URL}/v1/videos/image2video",
            headers=_get_headers(),
            json=payload,
        )

    if response.status_code != 200:
        raise RuntimeError(
            f"Kling API 요청 실패 {response.status_code}: {response.text}"
        )

    task_data = response.json()
    task_id = task_data.get("data", {}).get("task_id")

    if not task_id:
        raise RuntimeError(f"task_id 없음: {task_data}")

    print(f"Kling 영상 생성 시작: task_id={task_id}")

    video_url = _poll_video_task(task_id)
    _download_video(video_url, output_path)

    print(f"Kling 영상 저장: {output_path}")
    return output_path


def _poll_video_task(
    task_id: str,
    max_wait_sec: int = 300,
    interval_sec: int = 10,
) -> str:
    """Kling 영상 생성 완료까지 폴링"""
    elapsed = 0

    while elapsed < max_wait_sec:
        time.sleep(interval_sec)
        elapsed += interval_sec

        with httpx.Client(timeout=30.0) as client:
            response = client.get(
                f"{KLING_API_URL}/v1/videos/image2video/{task_id}",
                headers=_get_headers(),
            )

        if response.status_code != 200:
            print(f"폴링 오류 {response.status_code}, 재시도...")
            continue

        data = response.json().get("data", {})
        status = data.get("task_status")

        print(f"  Kling 상태: {status} ({elapsed}초 경과)")

        if status == "succeed":
            videos = data.get("task_result", {}).get("videos", [])
            if videos:
                return videos[0].get("url")
            raise RuntimeError("영상 URL 없음")

        elif status == "failed":
            raise RuntimeError(
                f"Kling 영상 생성 실패: {data.get('task_status_msg')}"
            )

    raise TimeoutError(f"Kling 타임아웃: {max_wait_sec}초 초과")


def _download_video(url: str, output_path: str) -> None:
    """영상 URL에서 파일 다운로드"""
    Path(output_path).parent.mkdir(parents=True, exist_ok=True)

    with httpx.Client(timeout=120.0) as client:
        response = client.get(url)

    if response.status_code != 200:
        raise RuntimeError(f"영상 다운로드 실패: {response.status_code}")

    with open(output_path, "wb") as f:
        f.write(response.content)


def generate_scene_videos(
    scenes: list,
    candidate_id: str,
    work_dir: str = "/tmp/kbeauty",
) -> list:
    """모든 장면의 모션 영상 생성"""
    results = []

    for scene in scenes:
        order = scene["order"]
        char_path = scene.get("character_image_path")
        loc_path = scene.get("location_image_path")
        action_prompt = scene.get("action_prompt", "")

        if not char_path or not Path(char_path).exists():
            print(f"장면 {order}: 캐릭터 이미지 없음, 스킵")
            results.append({**scene, "video_path": None})
            continue

        video_path = f"{work_dir}/{candidate_id}/scene_{order}_video.mp4"

        try:
            create_image_to_video(
                character_image_path=char_path,
                action_prompt=action_prompt,
                output_path=video_path,
                location_image_path=loc_path,
                duration=5,
                aspect_ratio="9:16",
            )
            results.append({**scene, "video_path": video_path})

        except Exception as e:
            print(f"장면 {order} 영상 생성 실패: {e}")
            results.append({**scene, "video_path": None})

    return results


if __name__ == "__main__":
    # 테스트 - 앞서 생성한 캐릭터 이미지로 모션 영상 생성
    test_image = "/tmp/test_character.png"
    output = "/tmp/test_kling_video.mp4"

    print("Kling 영상 생성 테스트 중...")
    create_image_to_video(
        character_image_path=test_image,
        action_prompt=(
            "A young Korean woman holds up a sheet mask package toward camera, "
            "smiles brightly and nods with excitement, "
            "soft natural movement, beauty influencer style"
        ),
        output_path=output,
        duration=5,
        aspect_ratio="9:16",
    )
    print(f"완료: {output}")
