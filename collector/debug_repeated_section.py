"""
debug_repeated_section.py
find_repeated_section(v7)이 어떤 피크를 왜 채택했는지 진단하는 스크립트.

사용법:
    python3 debug_repeated_section.py <음원파일경로> [close_threshold]

출력:
    - 검출된 모든 에너지 피크의 chroma 유사도 + 반복 짝
    - min_sim_for_repeat(0.75) 이상인 "검증된" 피크 목록
    - 1위와 근소한 차이(close_threshold 이내)인 "근소 그룹"
    - 최종 채택된 피크 (근소 그룹이 1개면 그대로, 여러 개면 가장 늦은 것의 짝)
"""
import sys
import librosa
import numpy as np

from beat_sync import _find_energy_peaks, _chroma_similarity_cached, _compare_chroma


def main():
    if len(sys.argv) < 2:
        print("사용법: python3 debug_repeated_section.py <음원파일경로> [close_threshold]")
        sys.exit(1)

    audio_path = sys.argv[1]
    close_threshold = float(sys.argv[2]) if len(sys.argv) > 2 else 0.005
    compare_duration = 20.0
    min_sim_for_repeat = 0.75
    hop_length = 2048
    max_peaks = 60

    y, sr = librosa.load(audio_path, sr=None, mono=True)
    total_duration = len(y) / sr
    print(f"음원 길이: {total_duration:.1f}s")
    print(f"close_threshold: {close_threshold}\n")

    peak_times, peak_rms, peak_prom = _find_energy_peaks(y, sr, hop_length=hop_length)
    print(f"검출된 에너지 피크 수: {len(peak_times)}")

    if len(peak_times) > max_peaks:
        top_idx = np.argsort(peak_prom)[::-1][:max_peaks]
        peak_times = peak_times[np.sort(top_idx)]
        print(f"prominence 상위 {max_peaks}개로 제한됨")

    chroma_cache = {}
    for t in peak_times:
        _chroma_similarity_cached(chroma_cache, y, sr, float(t), compare_duration, hop_length)

    best_match = {}
    best_partner = {}
    for t1 in peak_times:
        c1 = chroma_cache[float(t1)]
        best_sim = -1.0
        partner = None
        for t2 in peak_times:
            if abs(t1 - t2) < compare_duration:
                continue
            c2 = chroma_cache[float(t2)]
            sim = _compare_chroma(c1, c2)
            if sim > best_sim:
                best_sim = sim
                partner = float(t2)
        best_match[float(t1)] = best_sim
        best_partner[float(t1)] = partner

    verified = [(t, sim) for t, sim in best_match.items() if sim >= min_sim_for_repeat]

    if not verified:
        print("\n검증된 후보가 없습니다 (min_sim_for_repeat 미달).")
        return

    top1_sim = max(sim for _, sim in verified)
    close_group = [(t, sim) for t, sim in verified if top1_sim - sim <= close_threshold]

    print(f"\n{'피크 시각':>10} | {'chroma유사도':>11} | {'짝 피크':>10} | {'검증':>4} | {'근소그룹'}")
    print("-" * 65)
    close_group_times = {t for t, _ in close_group}
    for t1 in sorted(peak_times):
        sim = best_match[float(t1)]
        verified_flag = "✓" if sim >= min_sim_for_repeat else ""
        close_flag = "★" if float(t1) in close_group_times else ""
        print(f"{t1:>9.2f}s | {sim:>11.4f} | {best_partner[float(t1)]:>9.2f}s | "
              f"{verified_flag:>4} | {close_flag}")

    print(f"\n1위 유사도: {top1_sim:.4f}")
    print(f"근소 그룹({close_threshold} 이내) 크기: {len(close_group)}")

    if len(close_group) == 1:
        best_peak_time, best_sim = close_group[0]
        print(f"\n>>> 근소 그룹 1개(1위 단독) — 그대로 채택: {best_peak_time:.2f}s (유사도 {best_sim:.4f})")
    else:
        latest_t, _ = max(close_group, key=lambda x: x[0])
        partner_t = best_partner[latest_t]
        best_peak_time, best_sim = partner_t, best_match[partner_t]
        print(f"\n근소 그룹 내 가장 늦은 피크: {latest_t:.2f}s")
        print(f">>> 그 반복 짝을 채택: {partner_t:.2f}s (유사도 {best_sim:.4f})")

    lead_in = 5.0
    final_start = max(0.0, best_peak_time - lead_in)
    print(f"\n최종 start_offset (lead_in {lead_in}s 적용): {final_start:.2f}s")


if __name__ == "__main__":
    main()
