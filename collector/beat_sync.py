"""
beat_sync.py
음원의 BPM/비트를 추출하고, 곡 안에서 실제로 반복되는 에너지 고점(코러스 등)을
찾아 쇼츠 장면 분할용 시간 슬롯을 계산합니다.

다른 모듈에 대한 의존성 없는 독립 유닛 (Phase 1-1).

핵심 아이디어:
  0. "듣기 좋은 지점은 에너지 고점(피크)에서 시작해 하강하는 패턴이며, 그 패턴이
     곡 안에서 반복될수록 신뢰할 수 있다"는 원칙.
     - RMS 에너지 곡선에서 로컬 피크들을 모두 찾는다.
     - 각 피크 이후 구간의 멜로디(chroma) 패턴이 다른 피크 이후 구간과 얼마나
       닮았는지 비교해 "반복이 검증된" 피크만 후보로 남긴다. (에너지 형태가 아니라
       멜로디로 비교하는 이유: 편곡/다이나믹은 매번 달라도 화성 진행은 반복되므로)
     - 검증된 후보 중 가장 강하게 반복되는 피크를 채택하고, 그보다 lead_in(기본 5초)
       앞에서 클립을 시작한다 (빌드업 여유).
  1. librosa로 비트 타임스탬프 배열을 뽑는다.
  2. start_offset 이후, 목표 길이 근처에 가장 가까운 비트를 영상의 끝점으로
     잡는다 (음원 수익화 임계점을 넘기면서도 불필요하게 길어지지 않도록).
  3. start_offset부터 끝점까지를 등분한 "이상적인 전환 지점"을 계산한 뒤,
     각 지점을 가장 가까운 실제 비트로 스냅한다.
  4. 스냅된 비트들을 경계로 장면(scene) 슬롯 리스트를 만든다.
"""
from __future__ import annotations

import logging
from dataclasses import dataclass, asdict

import librosa
import numpy as np

logger = logging.getLogger(__name__)


@dataclass
class SceneSlot:
    """하나의 장면(씬)에 해당하는 시간 구간."""
    index: int          # 0부터 시작하는 장면 순서
    start: float         # 초 단위 시작 시각 (음원 원본 기준)
    end: float           # 초 단위 종료 시각 (음원 원본 기준)

    @property
    def duration(self) -> float:
        return round(self.end - self.start, 3)

    def to_dict(self) -> dict:
        d = asdict(self)
        d["duration"] = self.duration
        return d


@dataclass
class BeatSyncResult:
    """반복구간 탐지 + 비트 분석 + 씬 슬롯 계산 결과 전체."""
    bpm: float
    beat_times: list[float]        # 전체 비트 타임스탬프(초, 음원 원본 기준)
    start_offset: float             # 채택된 하이라이트(반복 구간) 시작 시각
    repetition_score: float         # 채택된 구간의 반복 점수 (참고용)
    video_end: float                # 최종 확정된 영상 종료 시각(초, 비트에 스냅됨)
    scenes: list[SceneSlot]
    fade_in_ms: float = 40.0        # 클릭 노이즈 방지용 최소 페이드인 (기본 40ms)
    fade_out_ms: float = 180.0      # 루프 이음매 자연스럽게 하는 페이드아웃 (기본 180ms)
    # 쇼츠는 루프 재생되므로 fade_out 끝~fade_in 시작이 매 루프 이어지는 "이음매"가 된다.
    # 이 구간이 길면 반복될 때마다 정적 공백이 두드러지므로, fade_in+fade_out 합이
    # 500ms를 넘지 않도록 제한한다.

    def to_dict(self) -> dict:
        return {
            "bpm": round(self.bpm, 2),
            "beat_times": [round(t, 3) for t in self.beat_times],
            "start_offset": round(self.start_offset, 3),
            "repetition_score": round(self.repetition_score, 2),
            "video_end": round(self.video_end, 3),
            "scenes": [s.to_dict() for s in self.scenes],
            "fade_in_ms": round(self.fade_in_ms, 1),
            "fade_out_ms": round(self.fade_out_ms, 1),
        }


def _extract_beats(y: np.ndarray, sr: int) -> tuple[float, np.ndarray]:
    """오디오 배열에서 BPM과 비트 타임스탬프(초)를 추출."""
    tempo, beat_frames = librosa.beat.beat_track(y=y, sr=sr, units="frames")
    beat_times = librosa.frames_to_time(beat_frames, sr=sr)
    bpm = float(np.asarray(tempo).reshape(-1)[0])

    if len(beat_times) < 2:
        raise ValueError(
            f"비트를 충분히 검출하지 못했습니다 (검출된 비트 수: {len(beat_times)}). "
            "음원 길이나 품질을 확인하세요."
        )
    return bpm, beat_times


def _nearest_beat(target: float, beat_times: np.ndarray) -> float:
    """target 시각에 가장 가까운 비트 시각을 반환."""
    idx = int(np.argmin(np.abs(beat_times - target)))
    return float(beat_times[idx])


def _pick_video_end(
    beat_times: np.ndarray,
    start_offset: float,
    target_duration: float,
    min_duration: float,
) -> float:
    """
    start_offset 이후, (start_offset + target_duration)에 가장 가까운 비트를
    영상 종료 시각으로 선택. (start_offset + min_duration) 미만인 비트는 후보에서 제외.
    적절한 비트가 없으면 음원 마지막 비트를 사용.
    """
    target_time = start_offset + target_duration
    floor_time = start_offset + min_duration

    candidates = beat_times[beat_times >= floor_time]
    if len(candidates) == 0:
        logger.warning(
            f"시작점({start_offset:.2f}s) + 최소길이({min_duration}s) 이상인 비트가 없어 "
            f"마지막 비트({beat_times[-1]:.2f}s)로 대체합니다."
        )
        return float(beat_times[-1])

    idx = int(np.argmin(np.abs(candidates - target_time)))
    return float(candidates[idx])


def _find_energy_peaks(
    y: np.ndarray,
    sr: int,
    hop_length: int = 2048,
    min_peak_distance_sec: float = 2.0,
    prominence: float = 0.03,
) -> tuple[np.ndarray, np.ndarray, np.ndarray]:
    """
    RMS 에너지 곡선에서 로컬 피크(에너지 고점)들을 찾는다.

    Returns:
        (peak_times, peak_rms_values, peak_prominences)
    """
    from scipy.signal import find_peaks

    rms = librosa.feature.rms(y=y, hop_length=hop_length)[0]
    rms_times = librosa.frames_to_time(np.arange(len(rms)), sr=sr, hop_length=hop_length)
    hop_sec = rms_times[1] - rms_times[0] if len(rms_times) > 1 else 0.1

    distance = max(1, int(min_peak_distance_sec / hop_sec))
    peaks, props = find_peaks(rms, distance=distance, prominence=prominence)

    return rms_times[peaks], rms[peaks], props["prominences"]


def _chroma_similarity_cached(chroma_cache: dict, y: np.ndarray, sr: int, t: float, dur: float, hop_length: int) -> np.ndarray | None:
    """t 시점부터 dur초 구간의 chroma를 캐시에서 가져오거나 계산해서 캐싱."""
    if t not in chroma_cache:
        s, e = int(t * sr), int(min((t + dur) * sr, len(y)))
        if e - s < sr * 2:
            chroma_cache[t] = None
        else:
            chroma_cache[t] = librosa.feature.chroma_cqt(y=y[s:e], sr=sr, hop_length=hop_length)
    return chroma_cache[t]


def _compare_chroma(c1: np.ndarray | None, c2: np.ndarray | None) -> float:
    if c1 is None or c2 is None:
        return 0.0
    n = min(c1.shape[1], c2.shape[1])
    if n < 5:
        return 0.0
    c1, c2 = c1[:, :n], c2[:, :n]
    norms1 = np.linalg.norm(c1, axis=0)
    norms2 = np.linalg.norm(c2, axis=0)
    dots = np.sum(c1 * c2, axis=0)
    sims = dots / (norms1 * norms2 + 1e-9)
    return float(np.mean(sims))


def find_repeated_section(
    y: np.ndarray,
    sr: int,
    target_duration: float = 35.0,
    hop_length: int = 2048,
    compare_duration: float = 20.0,
    lead_in: float = 5.0,
    min_sim_for_repeat: float = 0.75,
    max_peaks: int = 60,
) -> tuple[float, float]:
    """
    "듣기 좋은 지점은 에너지 고점(피크)에서 시작해 하강하는 패턴이며,
    그 패턴이 곡 안에서 반복될수록 신뢰할 수 있다"는 원칙으로 시작 오프셋을 찾는다.

    절차:
      1. RMS 에너지 곡선에서 로컬 피크(에너지 고점)들을 모두 찾는다.
      2. 각 피크 이후 compare_duration초 구간의 멜로디(chroma) 패턴을, 다른 모든 피크
         이후 구간과 비교해 가장 유사한 짝을 찾는다.
         (에너지 곡선 자체의 형태가 아니라 멜로디로 비교하는 이유: 같은 코러스라도
         편곡/다이나믹은 매번 조금씩 달라 에너지 형태는 안 닮을 수 있지만, 멜로디·화성
         진행은 반복되기 때문)
      3. 반복 짝의 유사도가 min_sim_for_repeat 이상인 피크들만 "검증된 후보"로 남긴다.
      4. 검증된 후보 중 유사도가 가장 높은 피크를 채택.
      5. 최종 시작점 = 채택된 피크 시각 - lead_in(기본 5초, 빌드업 여유).

    성능: chroma는 피크당 1회만 계산해 캐싱하고(O(n) 추출), 유사도 비교는
    캐싱된 배열끼리 O(n^2)로 수행한다. 피크가 너무 많으면 prominence 상위
    max_peaks개로 제한한다.

    Returns:
        (final_start_time, best_similarity)
    """
    peak_times, peak_rms, peak_prom = _find_energy_peaks(y, sr, hop_length=hop_length)
    total_duration = len(y) / sr

    if len(peak_times) == 0:
        logger.warning("에너지 피크를 찾지 못했습니다. 0초부터 시작합니다.")
        return 0.0, 0.0

    if len(peak_times) > max_peaks:
        top_idx = np.argsort(peak_prom)[::-1][:max_peaks]
        peak_times = peak_times[np.sort(top_idx)]

    chroma_cache: dict = {}
    for t in peak_times:
        _chroma_similarity_cached(chroma_cache, y, sr, float(t), compare_duration, hop_length)

    best_match = {}
    for t1 in peak_times:
        c1 = chroma_cache[float(t1)]
        best_sim = -1.0
        for t2 in peak_times:
            if abs(t1 - t2) < compare_duration:
                continue
            c2 = chroma_cache[float(t2)]
            sim = _compare_chroma(c1, c2)
            if sim > best_sim:
                best_sim = sim
        best_match[float(t1)] = best_sim

    verified = [(t, sim) for t, sim in best_match.items() if sim >= min_sim_for_repeat]

    if not verified:
        logger.warning(
            f"유사도 {min_sim_for_repeat} 이상인 반복 피크가 없어, 가장 높은 유사도의 피크를 사용합니다."
        )
        best_peak_time, best_sim = max(best_match.items(), key=lambda x: x[1])
    else:
        best_peak_time, best_sim = max(verified, key=lambda x: x[1])

    logger.info(f"채택된 에너지 피크: {best_peak_time:.2f}s (반복 유사도: {best_sim:.3f})")

    final_start = max(0.0, best_peak_time - lead_in)

    if final_start + target_duration > total_duration:
        final_start = max(0.0, total_duration - target_duration)

    return final_start, best_sim


def compute_scene_slots(
    audio_path: str,
    target_duration: float = 35.0,
    min_duration: float = 35.0,
    n_transitions: int = 3,
    auto_detect_highlight: bool = True,
    start_offset: float | None = None,
    lead_in: float = 5.0,
    fade_in_ms: float = 40.0,
    fade_out_ms: float = 180.0,
) -> BeatSyncResult:
    """
    음원 파일로부터 장면 분할용 시간 슬롯을 계산.

    Args:
        audio_path: 음원 파일 경로 (mp3/wav 등 librosa가 지원하는 포맷)
        target_duration: 목표 영상 길이(초). 시작점 이후 이 값에 가장 가까운 비트로 스냅됨.
        min_duration: 최소 허용 길이(초). 음원 수익화 임계점(예: 35초) 등을 반영.
        n_transitions: 전환 횟수. 장면 수는 n_transitions + 1개가 됨.
        auto_detect_highlight: True면 에너지 피크+반복 검증으로 start_offset을 자동 결정.
            start_offset이 명시적으로 주어지면 이 값은 무시됨.
        start_offset: 수동으로 지정하는 시작 오프셋(초). 짤스튜디오 음원 등록 시
            사람이 귀로 들어보고 직접 지정하고 싶을 때 사용.
        lead_in: 채택된 에너지 피크보다 몇 초 앞에서 클립을 시작할지 (기본 5초, 빌드업 여유).
        fade_in_ms: 클립 시작 페이드인 길이(ms). 임의 지점에서 잘라낸 파형의 클릭 노이즈만
            제거할 정도의 최소값이면 충분 (기본 40ms).
        fade_out_ms: 클립 종료 페이드아웃 길이(ms). 쇼츠는 루프 재생되므로 이 구간이
            매 루프 반복되는 "이음매 공백"이 된다 (기본 180ms).
            fade_in_ms + fade_out_ms 합이 500ms를 넘으면 ValueError.

    Returns:
        BeatSyncResult (bpm, 전체 비트 목록, 채택된 시작점, 확정 영상 종료 시각,
        장면별 시간 슬롯, 페이드 인/아웃 길이)
    """
    if n_transitions < 1:
        raise ValueError("n_transitions는 1 이상이어야 합니다.")
    if fade_in_ms + fade_out_ms > 500.0:
        raise ValueError(
            f"fade_in_ms({fade_in_ms}) + fade_out_ms({fade_out_ms})가 500ms를 초과합니다. "
            "쇼츠는 루프 재생되므로 두 페이드 합이 500ms를 넘지 않아야 이음매 공백이 두드러지지 않습니다."
        )

    y, sr = librosa.load(audio_path, sr=None, mono=True)
    total_duration = len(y) / sr

    if start_offset is not None:
        chosen_offset = start_offset
        rep_score = 0.0
        logger.info(f"수동 지정 시작점 사용: {chosen_offset:.2f}s")
    elif auto_detect_highlight:
        chosen_offset, rep_score = find_repeated_section(
            y, sr, target_duration=target_duration, lead_in=lead_in
        )
        logger.info(f"에너지 피크+반복 검증 자동 탐지: {chosen_offset:.2f}s (유사도: {rep_score:.3f})")
    else:
        chosen_offset, rep_score = 0.0, 0.0

    # 시작점이 음원 끝부분과 너무 가까워 target_duration을 못 채우면 앞으로 당김
    if chosen_offset + min_duration > total_duration:
        adjusted = max(0.0, total_duration - min_duration)
        logger.warning(
            f"시작점({chosen_offset:.2f}s)에서 최소길이({min_duration}s)를 채울 수 없어 "
            f"{adjusted:.2f}s로 보정합니다."
        )
        chosen_offset = adjusted

    bpm, beat_times = _extract_beats(y, sr)
    logger.info(f"BPM 추출 완료: {bpm:.1f}, 비트 {len(beat_times)}개")

    video_end = _pick_video_end(beat_times, chosen_offset, target_duration, min_duration)
    logger.info(f"영상 종료 시각 확정: {video_end:.2f}s (시작 {chosen_offset:.2f}s 기준 목표 {target_duration}s)")

    # 시작점도 가장 가까운 비트로 스냅 (완전히 임의의 프레임이 아니라 비트 그리드에 맞춤)
    snapped_start = _nearest_beat(chosen_offset, beat_times[beat_times <= video_end]) \
        if np.any(beat_times <= video_end) else chosen_offset

    # snapped_start ~ video_end 구간을 n_transitions+1개로 균등 분할한 "이상적" 경계
    n_scenes = n_transitions + 1
    ideal_boundaries = np.linspace(snapped_start, video_end, n_scenes + 1)

    boundaries = [snapped_start]
    usable_beats = beat_times[(beat_times > snapped_start) & (beat_times < video_end)]
    for b in ideal_boundaries[1:-1]:
        snapped = _nearest_beat(b, usable_beats) if len(usable_beats) > 0 else b
        boundaries.append(snapped)
    boundaries.append(video_end)

    boundaries = sorted(set(round(b, 3) for b in boundaries))
    if len(boundaries) < n_scenes + 1:
        logger.warning(
            "일부 경계가 중복되어 스냅되었습니다. 균등 분할 경계로 대체합니다: "
            f"{boundaries}"
        )
        boundaries = list(np.linspace(snapped_start, video_end, n_scenes + 1))

    scenes = [
        SceneSlot(index=i, start=round(boundaries[i], 3), end=round(boundaries[i + 1], 3))
        for i in range(len(boundaries) - 1)
    ]

    return BeatSyncResult(
        bpm=bpm,
        beat_times=beat_times.tolist(),
        start_offset=snapped_start,
        repetition_score=rep_score,
        video_end=video_end,
        scenes=scenes,
        fade_in_ms=fade_in_ms,
        fade_out_ms=fade_out_ms,
    )


if __name__ == "__main__":
    import sys
    import json

    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")

    if len(sys.argv) < 2:
        print("사용법: python beat_sync.py <음원파일경로> [target_duration] [n_transitions] [start_offset]")
        sys.exit(1)

    path = sys.argv[1]
    target = float(sys.argv[2]) if len(sys.argv) > 2 else 35.0
    transitions = int(sys.argv[3]) if len(sys.argv) > 3 else 3
    manual_offset = float(sys.argv[4]) if len(sys.argv) > 4 else None

    result = compute_scene_slots(
        path, target_duration=target, n_transitions=transitions, start_offset=manual_offset
    )
    print(json.dumps(result.to_dict(), ensure_ascii=False, indent=2))

