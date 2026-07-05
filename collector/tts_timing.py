"""
tts_timing.py
Phase 2-2: ElevenLabs "Create speech with timing"으로 씬별 발화를 생성하고,
beat_sync.py가 정한 씬 duration에 실제로 맞는지 검증/보정한다.

파이프라인 위치:
  claude_analyzer.generate_script()가 만든 scenes 배열
    (각 항목: {"scene_index", "start", "end", "role", "text"})
  → 씬별로 순차 TTS 호출 (request stitching으로 앞뒤 씬과 프로소디 연결)
  → 실제 발화 길이(actual_duration)를 target_duration(=end-start)과 비교
  → 오차가 허용치를 넘으면 voice_settings.speed로 1회 재보정
  → 그래도 안 맞으면 needs_rewrite=True로 표시 (자동 재작성은 하지 않음 —
    상위 파이프라인이 claude_analyzer로 되돌려 해당 씬만 다시 쓰게 한다)

설계 근거 (2026-07-03 논의):
  - ElevenLabs에는 "SRT 타이밍에 맞춰 자동으로 길이를 맞춰주는" 네이티브 기능이 없음.
    Dubbing Studio의 Dynamic Generation은 기존 원본 오디오 재더빙용이라 우리 케이스
    (텍스트에서 최초 생성)에는 안 맞음. 씬별 개별 호출 + speed 보정이 표준적 접근.
  - 씬별 개별 호출 시 경계에서 프로소디가 끊기는 문제는 request stitching
    (previous_request_ids, 응답 헤더의 request-id를 체이닝)으로 완화한다.
  - request_id는 응답 바디가 아니라 HTTP 헤더 'request-id'에 들어있음. 공식 SDK에서는
    client.text_to_speech.with_raw_response.convert_with_timestamps(...)로 감싸야
    response._response.headers로 접근 가능 (일반 convert_with_timestamps 호출로는 못 얻음).
  - eleven_v3 모델은 request stitching 미지원 → turbo/multilingual v2 계열 사용.

의존성 주의:
  - requirements.txt의 elevenlabs==1.9.0은 request stitching(2025-10 출시)보다 오래된
    버전이라 이 기능이 없다. `pip install -U elevenlabs`로 올리고 설치된 버전으로
    requirements.txt를 다시 고정할 것 (v1→v2 사이에 SDK가 크게 재작성됐음에 유의).
"""
import base64
import io
import logging
import subprocess
import tempfile
import os
from dataclasses import dataclass, field

from elevenlabs.client import ElevenLabs

from config import config

logger = logging.getLogger(__name__)

DEFAULT_MODEL_ID = "eleven_turbo_v2_5"  # request stitching 지원, 영어 전용이라 충분히 빠르고 저렴

# speed 파라미터 클램프 범위. ElevenLabs 허용 범위는 0.7~1.2이지만
# 그 끝단으로 갈수록 부자연스러워지므로 안전 마진을 둔다.
SPEED_MIN = 0.85
SPEED_MAX = 1.15

# 이 오차 비율 이내면 "통과"로 보고 speed 보정 없이 그대로 사용.
# 숏폼 영상에서 씬 경계 ±10% 오차(~0.9초)는 시청자가 인지하지 못하는 수준.
TOLERANCE_PCT = 0.10

# previous_request_ids로 체이닝할 최대 개수 (ElevenLabs 제한: 최대 3개)
MAX_CHAINED_REQUEST_IDS = 3


@dataclass
class SceneAudioResult:
    scene_index: int
    text: str
    target_duration: float
    actual_duration: float
    audio_bytes: bytes
    request_id: str | None
    char_timestamps: dict                 # ElevenLabs 원본 alignment (character 단위)
    word_timestamps: list[dict] = field(default_factory=list)  # [{"word","start","end"}, ...]
    speed_used: float = 1.0
    needs_rewrite: bool = False
    diff_pct: float = 0.0                 # (actual - target) / target

    def to_dict(self) -> dict:
        """audio_bytes/char_timestamps 제외한 요약 (로그/DB 저장용)."""
        return {
            "scene_index": self.scene_index,
            "target_duration": round(self.target_duration, 3),
            "actual_duration": round(self.actual_duration, 3),
            "diff_pct": round(self.diff_pct, 4),
            "speed_used": round(self.speed_used, 3),
            "needs_rewrite": self.needs_rewrite,
            "request_id": self.request_id,
        }


_client: ElevenLabs | None = None


def _get_client() -> ElevenLabs:
    """ElevenLabs 클라이언트 싱글턴. 매 호출마다 새로 만들지 않고 재사용."""
    global _client
    if _client is None:
        if not getattr(config, "ELEVENLABS_API_KEY", None):
            raise RuntimeError(
                "ELEVENLABS_API_KEY가 config에 설정되지 않았습니다. "
                "config.py에 ELEVENLABS_API_KEY를 추가해주세요."
            )
        _client = ElevenLabs(api_key=config.ELEVENLABS_API_KEY)
    return _client


def _get_mp3_duration(audio_bytes: bytes) -> float:
    """ffprobe로 MP3 바이트의 실제 재생 길이를 측정. trailing silence 포함."""
    with tempfile.NamedTemporaryFile(suffix=".mp3", delete=False) as f:
        f.write(audio_bytes)
        tmp_path = f.name
    try:
        result = subprocess.run(
            [
                "ffprobe", "-v", "quiet",
                "-show_entries", "format=duration",
                "-of", "csv=p=0",
                tmp_path,
            ],
            capture_output=True, text=True, timeout=10,
        )
        return float(result.stdout.strip())
    finally:
        os.unlink(tmp_path)


def _to_dict(obj) -> dict:
    """SDK가 돌려주는 pydantic 모델/dict를 dict로 통일. SDK 버전에 따라
    반환 타입이 다를 수 있어 방어적으로 처리."""
    if isinstance(obj, dict):
        return obj
    if hasattr(obj, "model_dump"):
        return obj.model_dump()
    if hasattr(obj, "dict"):
        return obj.dict()
    raise TypeError(f"dict로 변환할 수 없는 타입: {type(obj)}")


def _chars_to_words(char_timestamps) -> list[dict]:
    """
    ElevenLabs character-level alignment를 단어 단위로 그룹핑.
    Phase 3 SRT 자막 생성에서 재사용하기 위해 지금 만들어둔다.

    char_timestamps: dict 또는 SDK pydantic 모델(Alignment) 모두 허용.
    """
    if not isinstance(char_timestamps, dict):
        char_timestamps = _to_dict(char_timestamps)

    chars = char_timestamps.get("characters", [])
    starts = char_timestamps.get("character_start_times_seconds", [])
    ends = char_timestamps.get("character_end_times_seconds", [])

    words: list[dict] = []
    cur_chars: list[str] = []
    cur_start: float | None = None
    cur_end: float | None = None

    for ch, s, e in zip(chars, starts, ends):
        if ch.isspace():
            if cur_chars:
                words.append({"word": "".join(cur_chars), "start": cur_start, "end": cur_end})
                cur_chars = []
                cur_start = None
            continue
        if cur_start is None:
            cur_start = s
        cur_chars.append(ch)
        cur_end = e

    if cur_chars:
        words.append({"word": "".join(cur_chars), "start": cur_start, "end": cur_end})

    return words


def synthesize_scene(
    text: str,
    voice_id: str,
    speed: float = 1.0,
    previous_request_ids: list[str] | None = None,
    next_request_ids: list[str] | None = None,
    model_id: str = DEFAULT_MODEL_ID,
) -> tuple[bytes, dict, str | None, float]:
    """
    씬 텍스트 하나를 ElevenLabs "with-timestamps"로 생성.

    Returns:
        (audio_bytes, char_timestamps, request_id, actual_duration_sec)
    """
    client = _get_client()

    kwargs = dict(
        text=text,
        voice_id=voice_id,
        model_id=model_id,
        voice_settings={
            "stability": 0.5,
            "similarity_boost": 0.75,
            "speed": speed,
        },
    )
    if previous_request_ids:
        kwargs["previous_request_ids"] = previous_request_ids[-MAX_CHAINED_REQUEST_IDS:]
    if next_request_ids:
        kwargs["next_request_ids"] = next_request_ids[-MAX_CHAINED_REQUEST_IDS:]

    # SDK 2.56.0에서는 with_raw_response가 context manager를 지원하지 않음.
    # 직접 호출 후 _response.headers로 request-id를 꺼낸다.
    response = client.text_to_speech.with_raw_response.convert_with_timestamps(**kwargs)
    request_id = response._response.headers.get("request-id")

    raw = response.data  # AudioWithTimestampsResponse pydantic 모델
    # SDK 2.56.0 실제 필드명: audio_base_64 (언더스코어 포함), alignment
    audio_bytes = base64.b64decode(raw.audio_base_64)

    # actual_duration: ffprobe로 실제 MP3 파일 길이를 측정.
    # character_end_times_seconds는 trailing silence를 포함하지 않고,
    # mutagen은 VBR MP3 헤더를 잘못 읽는 케이스가 있어 ffprobe를 사용.
    actual_duration = _get_mp3_duration(audio_bytes)

    alignment = raw.alignment
    char_timestamps = alignment if isinstance(alignment, dict) else _to_dict(alignment)

    return audio_bytes, char_timestamps, request_id, actual_duration


def validate_and_fit_scene(
    scene: dict,
    voice_id: str,
    previous_request_ids: list[str] | None = None,
    next_request_ids: list[str] | None = None,
) -> SceneAudioResult:
    """
    씬 하나를 생성 → duration 검증 → 필요 시 speed로 1회 재보정.

    Args:
        scene: {"scene_index", "start", "end", "role", "text"}
               (claude_analyzer.generate_script()가 만든 scenes 배열의 항목 하나)
        previous_request_ids: 직전 씬(들)의 request_id. 순서상 앞쪽부터 최대 3개.
        next_request_ids: 뒤 씬(들)의 request_id. 재생성(패치) 시에만 사용.
    """
    scene_index = scene["scene_index"]
    text = scene["text"]
    target_duration = scene["end"] - scene["start"]

    if target_duration <= 0:
        raise ValueError(f"씬 {scene_index}: target_duration이 0 이하입니다 ({target_duration}).")

    # 1차 생성 (speed=1.0)
    audio_bytes, char_ts, request_id, actual_duration = synthesize_scene(
        text, voice_id, speed=1.0,
        previous_request_ids=previous_request_ids,
        next_request_ids=next_request_ids,
    )
    diff_pct = (actual_duration - target_duration) / target_duration
    speed_used = 1.0

    logger.info(
        f"씬 {scene_index}: target={target_duration:.2f}s actual={actual_duration:.2f}s "
        f"diff={diff_pct*100:+.1f}%"
    )

    needs_rewrite = False

    if abs(diff_pct) > TOLERANCE_PCT:
        # 실제 발화가 target보다 길면 speed>1로, 짧으면 speed<1로 보정
        raw_needed_speed = actual_duration / target_duration
        corrected_speed = max(SPEED_MIN, min(SPEED_MAX, raw_needed_speed))

        logger.info(f"씬 {scene_index}: speed={corrected_speed:.3f}로 재생성 시도")
        audio_bytes, char_ts, request_id, actual_duration = synthesize_scene(
            text, voice_id, speed=corrected_speed,
            previous_request_ids=previous_request_ids,
            next_request_ids=next_request_ids,
        )
        diff_pct = (actual_duration - target_duration) / target_duration
        speed_used = corrected_speed

        # 필요 speed가 애초에 클램프 범위를 벗어났거나(raw_needed_speed),
        # 보정 후에도 여전히 허용 오차를 넘으면 텍스트 자체가 안 맞는 것 → 재작성 필요
        if raw_needed_speed != corrected_speed or abs(diff_pct) > TOLERANCE_PCT:
            needs_rewrite = True
            logger.warning(
                f"씬 {scene_index}: speed 보정으로 부족 "
                f"(필요 speed={raw_needed_speed:.3f}, 보정 후 diff={diff_pct*100:+.1f}%) "
                f"— 텍스트 재작성 필요"
            )

    word_ts = _chars_to_words(char_ts)

    return SceneAudioResult(
        scene_index=scene_index,
        text=text,
        target_duration=target_duration,
        actual_duration=actual_duration,
        audio_bytes=audio_bytes,
        request_id=request_id,
        char_timestamps=char_ts,
        word_timestamps=word_ts,
        speed_used=speed_used,
        needs_rewrite=needs_rewrite,
        diff_pct=diff_pct,
    )


def fit_all_scenes(scenes: list[dict], voice_id: str) -> list[SceneAudioResult]:
    """
    씬 배열 전체를 순서대로 생성. request_id를 체이닝해서 request stitching 적용.

    Args:
        scenes: claude_analyzer.generate_script()가 만든 scenes 배열
                ({"scene_index","start","end","role","text"} 리스트)

    Returns:
        SceneAudioResult 리스트 (scenes와 1:1, 순서 동일)
    """
    if not scenes:
        raise ValueError("scenes가 비어있습니다.")

    results: list[SceneAudioResult] = []
    chained_request_ids: list[str] = []

    for scene in scenes:
        result = validate_and_fit_scene(
            scene, voice_id, previous_request_ids=chained_request_ids,
        )
        results.append(result)

        if result.request_id:
            # 최근 것이 앞에 오도록 유지 (synthesize_scene에서 뒤에서부터 최대 3개 사용)
            chained_request_ids = [result.request_id] + chained_request_ids
            chained_request_ids = chained_request_ids[:MAX_CHAINED_REQUEST_IDS]

    rewrite_needed = [r.scene_index for r in results if r.needs_rewrite]
    if rewrite_needed:
        logger.warning(f"텍스트 재작성이 필요한 씬 인덱스: {rewrite_needed}")
    else:
        logger.info(f"전체 {len(results)}개 씬 duration 검증 통과")

    return results


def save_scene_audio(result: SceneAudioResult, output_path: str) -> str:
    """검증 통과한 씬 오디오를 로컬 파일로 저장. R2 업로드는 상위 파이프라인에서 처리."""
    with open(output_path, "wb") as f:
        f.write(result.audio_bytes)
    return output_path


if __name__ == "__main__":
    # 수동 검증용 CLI: python tts_timing.py scenes.json VOICE_ID
    # scenes.json은 claude_analyzer.generate_script()가 반환한 dict의 "scenes" 필드를
    # 그대로 저장한 JSON 배열이어야 함.
    import json
    import sys

    logging.basicConfig(level=logging.INFO, format="%(message)s")

    if len(sys.argv) != 3:
        print("사용법: python tts_timing.py <scenes.json> <voice_id>")
        sys.exit(1)

    scenes_path, voice_id_arg = sys.argv[1], sys.argv[2]
    with open(scenes_path, encoding="utf-8") as f:
        scenes_data = json.load(f)

    results = fit_all_scenes(scenes_data, voice_id_arg)

    for r in results:
        out_path = f"scene_{r.scene_index}.mp3"
        save_scene_audio(r, out_path)
        print(f"씬 {r.scene_index}: {out_path} 저장 완료 — {r.to_dict()}")
