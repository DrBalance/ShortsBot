"""
ffmpeg_composer.py
Phase 2-3: Kling 영상 청크 + ElevenLabs TTS + (선택) 음악 트랙을 ffmpeg로 합성하고,
ElevenLabs word_timestamps에서 SRT 자막을 생성한다.

파이프라인 위치:
  script_generator.py (Kling 영상 생성)
  tts_timing.py       (씬별 TTS mp3 + word_timestamps)
  beat_sync.py        (start_offset, video_end, fade_in_ms, fade_out_ms)
    → ffmpeg_composer.py
    → final.mp4 + en.srt
    → uploader.py (R2 업로드 + Supabase 업데이트)

음악 합성 방식 (config.MUSIC_MODE):
  "external" : 짤스튜디오 등 외부 음원 — TTS만 합성, 음악은 유튜브 앱에서 수동 추가.
               짤스튜디오 ISRC 코드를 유튜브 앱 "사운드 추가"에 입력해야
               음원 수익이 인식됨. 직접 다운받아 삽입하면 수익 미인식.
  "embedded" : YouTube Audio Library 등 자유 음원 — ffmpeg에서 음악 트랙 직접 합성.
               유튜브 정책 변경 또는 짤스튜디오 미사용 시 전환.
  전환: .env의 MUSIC_MODE 값만 변경하면 됨 — 코드 수정 불필요.

자막 방식:
  burn-in 금지. en.srt만 생성하고 YouTube 업로드 봇(Phase 3)이 자막 트랙으로 업로드.
  이유: burn-in하면 다국어 자막 추가 시 영어 자막이 겹쳐 보임.

설계 원칙:
  - 영상(Kling 청크)과 나레이션(TTS)은 완전히 독립 트랙.
    컷 경계가 일치하지 않아도 됨 — 영상은 비트에, 나레이션은 자기 흐름대로.
  - 알고리즘 단순성 우선: ffmpeg 필터그래프를 최대한 단순하게 유지.
  - 음악 볼륨 (embedded 모드): TTS(나레이션) 대비 -12dB.
"""
from __future__ import annotations

import logging
import os
import subprocess
import tempfile
from dataclasses import dataclass, field
from pathlib import Path

from config import config

logger = logging.getLogger(__name__)

# 음악 볼륨 감쇄 (embedded 모드 전용, TTS를 0dB 기준으로 볼 때)
MUSIC_VOLUME_DB = -12.0

# SRT 한 줄 최대 글자 수 (YouTube 권장)
SRT_MAX_LINE_CHARS = 42


# ---------------------------------------------------------------------------
# 데이터 클래스
# ---------------------------------------------------------------------------

@dataclass
class ComposerInput:
    """ffmpeg_composer가 받는 모든 입력을 한 곳에 모은 구조체."""
    video_clips: list[str]              # Kling mp4 경로 (재생 순서대로)
    tts_clips: list[str]                # 씬별 mp3 경로 (씬 순서대로)
    scenes: list[dict]                  # {"scene_index","start","end","role","text"}
    word_timestamps: list[list[dict]]   # scenes와 1:1, 각 항목: [{"word","start","end"},...]
    output_dir: str                     # 출력 디렉터리 (final.mp4, en.srt 저장)

    # embedded 모드에서만 필요. external 모드면 None으로 두면 됨.
    music_path: str | None = None       # 원본 음악 파일 경로
    start_offset: float | None = None   # beat_sync.BeatSyncResult.start_offset
    video_end: float | None = None      # beat_sync.BeatSyncResult.video_end
    fade_in_ms: float = 40.0            # beat_sync.BeatSyncResult.fade_in_ms
    fade_out_ms: float = 180.0          # beat_sync.BeatSyncResult.fade_out_ms


@dataclass
class ComposerResult:
    video_path: str     # final.mp4 절대 경로
    srt_path: str       # en.srt 절대 경로
    duration: float     # 최종 영상 길이(초)
    music_mode: str     # 실제 사용된 MUSIC_MODE ("external" | "embedded")


# ---------------------------------------------------------------------------
# 내부 유틸리티
# ---------------------------------------------------------------------------

def _run(cmd: list[str], desc: str) -> None:
    """ffmpeg/ffprobe 명령 실행. 실패 시 stderr 포함 예외를 발생시킨다."""
    logger.debug(f"[{desc}] {' '.join(cmd)}")
    result = subprocess.run(cmd, capture_output=True, text=True)
    if result.returncode != 0:
        raise RuntimeError(
            f"ffmpeg 오류 [{desc}]:\n"
            f"STDOUT: {result.stdout}\n"
            f"STDERR: {result.stderr}"
        )


def _probe_duration(path: str) -> float:
    """ffprobe로 미디어 파일 길이(초)를 반환."""
    result = subprocess.run(
        [
            "ffprobe", "-v", "quiet",
            "-show_entries", "format=duration",
            "-of", "csv=p=0",
            path,
        ],
        capture_output=True, text=True, timeout=15,
    )
    if result.returncode != 0:
        raise RuntimeError(f"ffprobe 실패: {path}\n{result.stderr}")
    return float(result.stdout.strip())


# ---------------------------------------------------------------------------
# Step 1: 비디오 클립 concat
# ---------------------------------------------------------------------------

def concat_video_clips(video_clips: list[str], output_path: str) -> str:
    """
    Kling mp4 청크들을 재인코딩 없이 concat (concat demuxer 방식).
    모든 클립의 해상도·코덱·프레임레이트가 동일해야 한다 (Kling 출력은 항상 동일).
    """
    if not video_clips:
        raise ValueError("video_clips가 비어있습니다.")
    if len(video_clips) == 1:
        import shutil
        shutil.copy2(video_clips[0], output_path)
        logger.info(f"비디오 클립 1개 — 복사: {output_path}")
        return output_path

    list_path = output_path + ".concat_list.txt"
    with open(list_path, "w", encoding="utf-8") as f:
        for clip in video_clips:
            abs_path = os.path.abspath(clip)
            f.write(f"file '{abs_path}'\n")

    _run(
        [
            "ffmpeg", "-y",
            "-f", "concat", "-safe", "0",
            "-i", list_path,
            "-c", "copy",
            output_path,
        ],
        desc="video_concat",
    )
    os.unlink(list_path)
    logger.info(f"비디오 {len(video_clips)}개 concat → {output_path}")
    return output_path


# ---------------------------------------------------------------------------
# Step 2: TTS 클립 concat
# ---------------------------------------------------------------------------

def concat_tts_clips(tts_clips: list[str], output_path: str) -> str:
    """
    씬별 mp3를 순서대로 이어붙여 단일 나레이션 트랙 생성.
    ffmpeg concat filter 사용 (오디오만이라 demuxer concat보다 안정적).
    """
    if not tts_clips:
        raise ValueError("tts_clips가 비어있습니다.")
    if len(tts_clips) == 1:
        import shutil
        shutil.copy2(tts_clips[0], output_path)
        logger.info(f"TTS 클립 1개 — 복사: {output_path}")
        return output_path

    inputs = []
    for clip in tts_clips:
        inputs += ["-i", clip]

    filter_str = "".join(f"[{i}:a]" for i in range(len(tts_clips)))
    filter_str += f"concat=n={len(tts_clips)}:v=0:a=1[aout]"

    _run(
        [
            "ffmpeg", "-y",
            *inputs,
            "-filter_complex", filter_str,
            "-map", "[aout]",
            "-c:a", "libmp3lame", "-q:a", "2",
            output_path,
        ],
        desc="tts_concat",
    )
    logger.info(f"TTS {len(tts_clips)}개 concat → {output_path}")
    return output_path


# ---------------------------------------------------------------------------
# Step 3: 음악 트랙 처리 (embedded 모드 전용)
# ---------------------------------------------------------------------------

def process_music(
    music_path: str,
    start_offset: float,
    video_end: float,
    fade_in_ms: float,
    fade_out_ms: float,
    output_path: str,
) -> str:
    """
    [embedded 모드 전용]
    원본 음악에서 [start_offset, video_end] 구간만 잘라내고
    fade in/out + 볼륨 감쇄(-12dB)를 적용.

    fade in/out은 beat_sync.py가 계산한 값을 그대로 사용 (기본 40ms/180ms).
    """
    clip_duration = video_end - start_offset
    if clip_duration <= 0:
        raise ValueError(
            f"music trim 구간이 비어있습니다: start_offset={start_offset}, video_end={video_end}"
        )

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

    _run(
        [
            "ffmpeg", "-y",
            "-i", music_path,
            "-af", filter_str,
            "-c:a", "libmp3lame", "-q:a", "2",
            output_path,
        ],
        desc="music_process",
    )
    logger.info(
        f"음악 처리 완료: {start_offset:.2f}s~{video_end:.2f}s trim, "
        f"fade in {fade_in_ms}ms / out {fade_out_ms}ms, "
        f"{MUSIC_VOLUME_DB}dB → {output_path}"
    )
    return output_path


# ---------------------------------------------------------------------------
# Step 4: TTS + 음악 믹싱 (embedded 모드 전용)
# ---------------------------------------------------------------------------

def mix_audio(tts_path: str, music_path: str, output_path: str) -> str:
    """
    [embedded 모드 전용]
    TTS(나레이션)와 음악 트랙을 amix로 믹싱.
    duration=longest: 둘 중 긴 쪽 길이 유지.
    TTS는 0dB 기준, 음악은 process_music에서 이미 -12dB 처리됨.
    """
    _run(
        [
            "ffmpeg", "-y",
            "-i", tts_path,
            "-i", music_path,
            "-filter_complex", "amix=inputs=2:duration=longest:normalize=0",
            "-c:a", "aac", "-b:a", "192k",
            output_path,
        ],
        desc="audio_mix",
    )
    logger.info(f"TTS + 음악 믹싱 완료 → {output_path}")
    return output_path


# ---------------------------------------------------------------------------
# Step 5: 비디오 + 오디오 합성
# ---------------------------------------------------------------------------

def mux_video_audio(video_path: str, audio_path: str, output_path: str) -> str:
    """
    비디오 트랙과 오디오 트랙을 합성.
    -shortest: 영상이 끝나면 오디오도 자름 (TTS가 약간 길어질 경우 대비).
    비디오는 재인코딩 없이 copy, 오디오만 aac로 인코딩.
    """
    _run(
        [
            "ffmpeg", "-y",
            "-i", video_path,
            "-i", audio_path,
            "-c:v", "copy",
            "-c:a", "aac", "-b:a", "192k",
            "-shortest",
            "-map", "0:v:0",
            "-map", "1:a:0",
            output_path,
        ],
        desc="mux_video_audio",
    )
    logger.info(f"영상 + 오디오 합성 완료 → {output_path}")
    return output_path


# ---------------------------------------------------------------------------
# Step 6: SRT 자막 생성
# ---------------------------------------------------------------------------

def _seconds_to_srt_time(seconds: float) -> str:
    """초 단위 float를 SRT 타임코드(HH:MM:SS,mmm)로 변환."""
    total_ms = int(round(seconds * 1000))
    ms = total_ms % 1000
    total_s = total_ms // 1000
    s = total_s % 60
    total_m = total_s // 60
    m = total_m % 60
    h = total_m // 60
    return f"{h:02d}:{m:02d}:{s:02d},{ms:03d}"


def _wrap_line(text: str, max_chars: int = SRT_MAX_LINE_CHARS) -> str:
    """
    단어 경계로 줄 나눔. max_chars를 넘으면 다음 줄로.
    YouTube Shorts는 세로 화면이라 짧게 나누는 게 가독성에 좋음.
    """
    words = text.split()
    lines: list[str] = []
    current = ""
    for word in words:
        if current and len(current) + 1 + len(word) > max_chars:
            lines.append(current)
            current = word
        else:
            current = (current + " " + word).strip()
    if current:
        lines.append(current)
    return "\n".join(lines)


def build_srt(
    scenes: list[dict],
    word_timestamps: list[list[dict]],
) -> str:
    """
    씬별 word_timestamps(0초 기준 상대 시간)를 절대 시간으로 변환하고
    SRT 포맷 문자열을 반환.

    scenes[i]["start"]를 offset으로 더해 절대 시간 계산.
    word_timestamps[i]가 비어있는 씬은 씬 텍스트 전체를 단일 블록으로 처리.
    """
    if len(scenes) != len(word_timestamps):
        raise ValueError(
            f"scenes({len(scenes)})와 word_timestamps({len(word_timestamps)}) 길이가 다릅니다."
        )

    srt_blocks: list[str] = []
    idx = 1

    for scene, words in zip(scenes, word_timestamps):
        scene_start = scene["start"]

        if not words:
            # word_timestamps가 없는 씬: 씬 전체를 단일 블록으로
            start_tc = _seconds_to_srt_time(scene_start)
            end_tc = _seconds_to_srt_time(scene["end"])
            text = _wrap_line(scene.get("text", ""))
            if text:
                srt_blocks.append(f"{idx}\n{start_tc} --> {end_tc}\n{text}")
                idx += 1
            continue

        for word_info in words:
            abs_start = scene_start + word_info["start"]
            abs_end = scene_start + word_info["end"]
            word_text = word_info["word"].strip()
            if not word_text:
                continue
            start_tc = _seconds_to_srt_time(abs_start)
            end_tc = _seconds_to_srt_time(abs_end)
            srt_blocks.append(f"{idx}\n{start_tc} --> {end_tc}\n{word_text}")
            idx += 1

    return "\n\n".join(srt_blocks) + "\n"


def save_srt(
    scenes: list[dict],
    word_timestamps: list[list[dict]],
    output_path: str,
) -> str:
    """SRT 파일 생성 및 저장."""
    srt_content = build_srt(scenes, word_timestamps)
    with open(output_path, "w", encoding="utf-8") as f:
        f.write(srt_content)
    logger.info(f"SRT 자막 저장 완료 → {output_path}")
    return output_path


# ---------------------------------------------------------------------------
# 메인 진입점
# ---------------------------------------------------------------------------

def compose(inp: ComposerInput, music_mode: str | None = None) -> ComposerResult:
    """
    전체 합성 파이프라인 실행.

    Args:
        inp: ComposerInput
        music_mode: "external" | "embedded". None이면 config.MUSIC_MODE 사용.

    처리 순서 (external 모드):
      1. video concat
      2. TTS concat
      3. 비디오 + TTS 합성 → final.mp4
      4. SRT 생성 → en.srt
      ※ 음악은 유튜브 앱 업로드 시 짤스튜디오 ISRC 코드로 수동 추가

    처리 순서 (embedded 모드):
      1. video concat
      2. TTS concat
      3. 음악 trim + fade + 볼륨
      4. TTS + 음악 믹싱
      5. 비디오 + 오디오 합성 → final.mp4
      6. SRT 생성 → en.srt
    """
    mode = music_mode or config.MUSIC_MODE
    if mode not in ("external", "embedded"):
        raise ValueError(f"MUSIC_MODE는 'external' 또는 'embedded'여야 합니다. (현재: {mode})")

    out_dir = Path(inp.output_dir)
    out_dir.mkdir(parents=True, exist_ok=True)

    with tempfile.TemporaryDirectory() as tmp:
        tmp = Path(tmp)

        logger.info(f"=== Phase 2-3 ffmpeg 합성 시작 (MUSIC_MODE={mode}) ===")

        # Step 1: 비디오 concat
        video_concat = str(tmp / "video_concat.mp4")
        concat_video_clips(inp.video_clips, video_concat)

        # Step 2: TTS concat
        tts_concat = str(tmp / "tts_concat.mp3")
        concat_tts_clips(inp.tts_clips, tts_concat)

        # Step 3~4: 음악 모드 분기
        if mode == "embedded":
            # embedded: 음악 트랙 합성
            if not inp.music_path:
                raise ValueError("embedded 모드에서는 music_path가 필요합니다.")
            if inp.start_offset is None or inp.video_end is None:
                raise ValueError("embedded 모드에서는 start_offset과 video_end가 필요합니다.")

            music_processed = str(tmp / "music_processed.mp3")
            process_music(
                inp.music_path,
                inp.start_offset,
                inp.video_end,
                inp.fade_in_ms,
                inp.fade_out_ms,
                music_processed,
            )
            audio_mix = str(tmp / "audio_mix.aac")
            mix_audio(tts_concat, music_processed, audio_mix)
            final_audio = audio_mix

        else:
            # external: TTS만 사용, 음악은 유튜브 앱에서 수동 추가
            logger.info(
                "MUSIC_MODE=external — 음악 트랙 제외. "
                "유튜브 앱 업로드 시 짤스튜디오 ISRC 코드로 사운드 추가 필요."
            )
            final_audio = tts_concat

        # Step 5: 비디오 + 오디오 합성
        final_mp4 = str(out_dir / "final.mp4")
        mux_video_audio(video_concat, final_audio, final_mp4)

        # Step 6: SRT 생성
        srt_path = str(out_dir / "en.srt")
        save_srt(inp.scenes, inp.word_timestamps, srt_path)

    duration = _probe_duration(final_mp4)
    logger.info(f"=== 합성 완료: {final_mp4} ({duration:.2f}s), MUSIC_MODE={mode} ===")

    return ComposerResult(
        video_path=final_mp4,
        srt_path=srt_path,
        duration=duration,
        music_mode=mode,
    )


# ---------------------------------------------------------------------------
# CLI (수동 테스트용)
# ---------------------------------------------------------------------------

if __name__ == "__main__":
    """
    수동 테스트 사용법 (external 모드):
      python ffmpeg_composer.py \
        --video clip1.mp4 clip2.mp4 clip3.mp4 \
        --tts scene_0.mp3 scene_1.mp3 scene_2.mp3 scene_3.mp3 \
        --scenes scenes.json \
        --output-dir ./output

    수동 테스트 사용법 (embedded 모드):
      python ffmpeg_composer.py \
        --video clip1.mp4 clip2.mp4 \
        --tts scene_0.mp3 scene_1.mp3 \
        --scenes scenes.json \
        --music track.mp3 \
        --start-offset 32.5 \
        --video-end 68.2 \
        --music-mode embedded \
        --output-dir ./output

    scenes.json: claude_analyzer.generate_script()의 "scenes" 필드를 그대로 저장한 JSON.
    각 씬에 "word_timestamps" 키가 포함되어 있어야 함.
    """
    import argparse
    import json

    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")

    parser = argparse.ArgumentParser(description="ShortsBot ffmpeg 합성기")
    parser.add_argument("--video", nargs="+", required=True, help="Kling mp4 클립 경로 (순서대로)")
    parser.add_argument("--tts", nargs="+", required=True, help="씬별 TTS mp3 경로 (순서대로)")
    parser.add_argument("--scenes", required=True, help="scenes.json 경로 (word_timestamps 포함)")
    parser.add_argument("--music", default=None, help="음악 원본 파일 경로 (embedded 모드 전용)")
    parser.add_argument("--start-offset", type=float, default=None, help="beat_sync start_offset (embedded 모드 전용)")
    parser.add_argument("--video-end", type=float, default=None, help="beat_sync video_end (embedded 모드 전용)")
    parser.add_argument("--fade-in-ms", type=float, default=40.0)
    parser.add_argument("--fade-out-ms", type=float, default=180.0)
    parser.add_argument("--music-mode", default=None, choices=["external", "embedded"],
                        help="음악 합성 방식. 미지정 시 config.MUSIC_MODE 사용.")
    parser.add_argument("--output-dir", default="./output")
    args = parser.parse_args()

    with open(args.scenes, encoding="utf-8") as f:
        scenes_data = json.load(f)

    word_ts = [s.get("word_timestamps", []) for s in scenes_data]

    inp = ComposerInput(
        video_clips=args.video,
        tts_clips=args.tts,
        scenes=scenes_data,
        word_timestamps=word_ts,
        output_dir=args.output_dir,
        music_path=args.music,
        start_offset=args.start_offset,
        video_end=args.video_end,
        fade_in_ms=args.fade_in_ms,
        fade_out_ms=args.fade_out_ms,
    )

    result = compose(inp, music_mode=args.music_mode)
    print(f"✅ final.mp4  → {result.video_path}")
    print(f"✅ en.srt    → {result.srt_path}")
    print(f"   영상 길이  : {result.duration:.2f}s")
    print(f"   음악 모드  : {result.music_mode}")
