"""
kling_client.py

Kling API로 이미지 → 모션 영상 생성.

왼손AI 선택 기준:
- Kling 3.0: 화질 우수, 텍스트 안 깨짐, 자연스러운 연기 톤
- SeeDance 2.0: 액션씬, 멀티컷 전환에 강함
→ K뷰티 쇼츠는 자연스러운 연기 필요 → Kling 3.0 선택

Kling Elements 방식:
- 캐릭터 이미지(단색 배경) + 로케이션 이미지를 함께 전달
- 인물 일관성 + 배경 일관성 동시 유지
"""

import os
import time
import httpx
import base64
import jwt
from pathlib import Path
from typing import Optional
from datetime import datetime, timezone

KLING_API_URL = "https://api.klingai.com"


def _generate_kling_token() -> str:
    """
    Kling API JWT 토큰 생성.
    Access Key + Secret Key로 서명.
    """
    access_key = os.environ.get("KLING_ACCESS_KEY")
    secret_key = os.environ.get("KLING_SECRET_KEY")

    if not access_key or not secret_key:
        raise ValueError("KLING_ACCESS_KEY, KLING_SECRET_KEY 환경변수 필요")

    now = int(datetime.now(timezone.utc).timestamp())
    payload = {
        "iss": access_key,
        "exp": now + 1800,  # 30분
        "nbf": now - 5,
    }

    token = jwt.encode(payload, secret_key, algorithm="HS256")
    return token


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

    왼손AI 방식:
    - 캐릭터 이미지(단색 배경)를 첫 번째 입력으로
    - 로케이션 이미지를 참조로 추가 (Elements 방식)
    - 프롬프트에 카메라 무브먼트 포함

    Args:
        character_image_path: 캐릭터 이미지 경로 (단색 배경)
        action_prompt: 모션 설명 프롬프트 (영어)
        output_path: 저장할 .mp4 파일 경로
        location_image_path: 배경 이미지 경로 (선택)
        duration: 영상 길이 초 (5 또는 10)
        aspect_ratio: 화면 비율 (쇼츠는 9:16)
        model: Kling 모델 버전

    Returns:
        저장된 영상 파일 경로
    """
    token = _generate_kling_token()
    headers = {
        "Authorization": f"Bearer {token}",
        "Content-Type": "application/json",
    }

    # 캐릭터 이미지 base64 변환
    char_b64 = _image_to_base64(character_image_path)

    # 요청 페이로드
    payload = {
        "model": model,
        "image": char_b64,
        "prompt": action_prompt,
        "duration": str(duration),
        "aspect_ratio": aspect_ratio,
        "cfg_scale": 0.5,  # 프롬프트 준수도 (0~1)
    }

    # 로케이션 이미지가 있으면 참조 이미지로 추가
    if location_image_path and Path(location_image_path).exists():
        loc_b64 = _image_to_base64(location_image_path)
        payload["image_tail"] = loc_b64  # 끝 프레임 참조

    # 영상 생성 요청
    with httpx.Client(timeout=30.0) as client:
        response = client.post(
            f"{KLING_API_URL}/v1/videos/image2video",
            headers=headers,
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

    # 폴링 - 완료까지 대기
    video_url = _poll_video_task(task_id, token)

    # 영상 다운로드
    _download_video(video_url, output_path)

    print(f"Kling 영상 저장: {output_path}")
    return output_path


def _poll_video_task(
    task_id: str,
    token: str,
    max_wait_sec: int = 300,
    interval_sec: int = 10,
) -> str:
    """
    Kling 영상 생성 완료까지 폴링.

    Returns:
        완성된 영상의 다운로드 URL
    """
    headers = {"Authorization": f"Bearer {token}"}
    elapsed = 0

    while elapsed < max_wait_sec:
        time.sleep(interval_sec)
        elapsed += interval_sec

        with httpx.Client(timeout=30.0) as client:
            response = client.get(
                f"{KLING_API_URL}/v1/videos/image2video/{task_id}",
                headers=headers,
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
            raise RuntimeError(f"Kling 영상 생성 실패: {data.get('task_status_msg')}")

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
    """
    모든 장면의 모션 영상 생성.

    Args:
        scenes: image_paths가 포함된 scenes 리스트
        candidate_id: 파일명 구분용 ID
        work_dir: 임시 작업 디렉토리

    Returns:
        scenes에 video_path가 추가된 리스트
    """
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
