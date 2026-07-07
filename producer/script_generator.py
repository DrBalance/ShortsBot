"""
script_generator.py
Phase 2-1: Supabase content_candidates 테이블의 scenes 배열을
           Vidu Q3 멀티샷 프롬프트로 변환하고 영상을 생성한다.

파이프라인 위치:
  Supabase content_candidates (status='pending')
    → scenes []  : beat_sync 타이밍 + claude_analyzer 나레이션 텍스트
    → product_image_url : Olive Young og:image (prdtNo로 파싱, R2 캐시)
    ↓
  generate_vidu_prompts()
    → 씬별 role + duration + content_type → Vidu 프롬프트 문단 조립
    → 멀티샷 프롬프트 문자열 (Shot 1 ~ Shot N 형식)
    ↓
  call_vidu_api()
    → referenceImages: [product_image_url]
    → 단일 호출로 전체 영상 생성 (Vidu Q3 Smart Cuts)
    ↓
  R2 업로드 → candidates status 'video_ready' 업데이트

설계 근거 (2026-07-05 논의):
  - 나레이션(ElevenLabs)과 영상(Vidu)은 완전히 독립 트랙.
    스크립트 파트 경계와 비트 컷 경계가 일치하지 않아도 된다.
    나레이션은 귀로, 컷은 눈으로 — 각자의 리듬으로 흐른다.
  - Vidu Q3 Reference-to-Video: 제품 이미지 1장을 referenceImage로 넣으면
    영상 전체에 걸쳐 제품 외형 + 인물 외형 일관성이 유지된다.
  - 역동성 원칙: 씬당 카메라 무브 1개만. 두 개 이상 쌓으면 "video soup".
    duration에 따라 속도 조정 (짧은 씬 → 빠른 무빙/컷, 긴 씬 → 여유 있는 무빙).
  - Constraint 라인 필수: 없으면 Vidu가 텍스트/로고/왜곡 엣지를 자체 생성.
  - 제품 이미지 소싱: 큐레이터 링크의 prdtNo → product detail 1회 접근 →
    og:image URL 파싱 → R2 캐시. 검색 페이지는 Cloudflare 차단으로 사용 불가.
"""
from __future__ import annotations

import logging
import random
import re
import time
from dataclasses import dataclass, field



import requests
from bs4 import BeautifulSoup

from config import config
from db import supabase_client as db

logger = logging.getLogger(__name__)

# ─── 영상 생성 모델 선택 ──────────────────────────────────────────
# config.VIDEO_ENGINE = "kling" | "vidu" 로 전환.
# 기본값은 "kling" (현재 Trial Package 소진 중).
# Vidu로 전환 시 config.py에서 VIDEO_ENGINE = "vidu"로 변경.

VIDEO_ENGINE_KLING = "kling"
VIDEO_ENGINE_VIDU  = "vidu"

# 영상 스펙 (공통)
VIDEO_ASPECT_RATIO = "9:16"   # YouTube Shorts
VIDEO_RESOLUTION   = "1080p"
VIDEO_GENERATE_AUDIO = False  # ElevenLabs 오디오를 별도 트랙으로 사용

# ─── Kling API 설정 ──────────────────────────────────────────────
# 공식 Kling AI API Platform (kling.ai/document-api)
KLING_API_BASE         = "https://api.klingai.com"
KLING_IMAGE2VIDEO_PATH = "/v1/videos/image2video"
KLING_TASK_PATH        = "/v1/videos/image2video/{task_id}"
KLING_POLL_INTERVAL_SEC = 5
KLING_POLL_TIMEOUT_SEC  = 300   # 5분
# Kling 최대 생성 길이: 10초. 35초 영상은 청크로 분할 후 ffmpeg 이어붙임.
KLING_MAX_DURATION_SEC  = 10
KLING_MODEL             = "kling-v3"   # v3.0: multi_prompt + 레퍼런스 이미지 지원

# ─── Vidu API 설정 ───────────────────────────────────────────────
# 공식 Vidu API Platform (platform.vidu.com)
VIDU_API_BASE          = "https://api.vidu.com/ent/v2"
VIDU_REF2VIDEO_PATH    = "/reference2video"
VIDU_TASK_PATH         = "/tasks/{task_id}"
VIDU_POLL_INTERVAL_SEC = 5
VIDU_POLL_TIMEOUT_SEC  = 300   # 5분
# Vidu Q3 최대 생성 길이: 16초. 35초 영상은 청크로 분할 후 ffmpeg 이어붙임.
VIDU_MAX_DURATION_SEC  = 16
VIDU_MODEL             = "viduq3"   # viduq3 | viduq3-turbo | viduq3-mix


# ─── 카메라 무빙 풀 ───────────────────────────────────────────────

# duration 기준으로 카메라 무빙 카테고리를 분기한다.
# SHORT: 2~4초 — 임팩트 있는 빠른 무브
# MEDIUM: 4~8초 — 뚜렷한 방향성 있는 무브
# LONG: 8초+ — 여유 있는 서사적 무브
SHORT_SEC = 4.0
LONG_SEC = 8.0

# role → (카메라 무브, 주체 액션) 풀
# 각 항목: (camera_move, subject_action)
# content_type 분위기 수식어는 별도 MOOD_TOKENS로 관리
ROLE_SHOT_POOL: dict[str, dict[str, list[tuple[str, str]]]] = {
    "hook": {
        "short": [
            ("quick push-in from medium to extreme close-up",
             "product emerges from dark shadow into sharp light"),
            ("fast rack focus from blurred background to product",
             "a single droplet lands on the product surface"),
        ],
        "medium": [
            ("slow dramatic push-in, camera closing in on label",
             "soft mist rises around the product as it is revealed"),
            ("low-angle push-in, camera rises to product height",
             "product stands still on reflective surface, light sweeps across"),
        ],
        "long": [
            ("slow 45-degree arc left around the product",
             "water droplets form and slide slowly down the bottle"),
            ("gentle pull-back revealing product in full studio setting",
             "steam curls upward beside the product in warm light"),
        ],
    },
    "relate": {
        "short": [
            ("static with subtle handheld sway",
             "a hand enters frame and gently picks up the product"),
            ("gentle tilt down to product held in open palm",
             "fingers curl softly around the bottle"),
        ],
        "medium": [
            ("slow follow shot as hand lifts product toward camera",
             "hand picks up product, turns it slowly to show front label"),
            ("smooth push-in toward hand holding product at waist height",
             "thumb presses pump once, a small amount dispenses"),
        ],
        "long": [
            ("tracking shot alongside person walking to bathroom counter",
             "person sets product on counter, looks at it thoughtfully"),
            ("handheld follow, slight sway, intimate feel",
             "hands cradle the product, fingers trace the bottle shape"),
        ],
    },
    # what_it_does: 제품 카테고리별 action은 _pick_shot_for_what_it_does()에서 분기.
    # 여기 pool은 카테고리 감지 실패 시 fallback용으로만 사용.
    "what_it_does": {
        "short": [
            ("extreme macro drift across product surface left to right",
             "product texture fills the frame, light catching surface detail"),
            ("quick tilt up from base to cap of product",
             "light catches the product edge cleanly"),
        ],
        "medium": [
            ("slow macro push-in on product opening or nozzle",
             "product dispensed onto fingertip in a small controlled amount"),
            ("smooth pan right across skincare lineup on marble surface",
             "products stand neatly, morning light crosses them"),
        ],
        "long": [
            ("slow 90-degree clockwise orbit around product on marble",
             "product applied to fingertip, spreads naturally into skin"),
            ("tilt down from cap to base, then slow push-in on label",
             "key ingredient visual effect — light refracts through formula"),
        ],
    },
    "solution": {
        "short": [
            ("quick macro cut to skin texture close-up",
             "fingertip applies product gently to forearm skin"),
            ("fast push-in to product nozzle dispensing",
             "small pearl of cream sits on back of hand"),
        ],
        "medium": [
            ("slow push-in toward fingertip applying product to cheek",
             "product blends into skin seamlessly, no white cast"),
            ("gentle handheld follow as hand moves across face",
             "application motion is smooth, skin looks visibly hydrated"),
        ],
        "long": [
            ("slow arc around person applying product at mirror",
             "before-and-after texture visible as product absorbs"),
            ("steady tracking shot, camera at mirror height",
             "person pats product gently into skin, expression satisfied"),
        ],
    },
    "agitate": {
        "short": [
            ("static close-up, no camera move",
             "person touches face, slight frown — skin concern visible"),
            ("subtle handheld, slight shake",
             "hand holds old product, sets it down with hesitation"),
        ],
        "medium": [
            ("slow push-in toward mirror reflection of skin concern",
             "person examines skin closely in mirror, touches problem area"),
            ("gentle tilt down, then hold",
             "cluttered shelf of tried products — none quite right"),
        ],
        "long": [
            ("slow pull-back from mirror to full bathroom scene",
             "person sighs softly, sets down product that didn't work"),
            ("steady handheld, intimate",
             "hands resting on counter, face slightly frustrated at reflection"),
        ],
    },
    "proof": {
        "short": [
            ("static, slight zoom pulse on product",
             "product alone on white surface — clean, confident"),
            ("quick pan left across three identical repurchased bottles",
             "three identical bottles lined up in a row, one visibly empty"),
        ],
        "medium": [
            ("slow tilt up from product base to brand logo area",
             "product stands tall, light hits label cleanly"),
            ("gentle push-in toward product surrounded by empty packaging",
             "empty bottle beside new full bottle — visible repurchase moment"),
        ],
        "long": [
            ("slow 180-degree arc around product on reflective surface",
             "empty box and used-up bottle beside a fresh new one — cycle of repurchase"),
            ("tracking shot across bathroom shelf — camera settles on hero product",
             "shelf full of skincare, hero product front and center, label worn from use"),
        ],
    },
    "product_rundown": {
        "short": [
            ("quick cut push-in to product label",
             "product placed down on surface with light tap"),
            ("fast rack focus from hand to product face",
             "hand sets product upright, label faces camera"),
        ],
        "medium": [
            ("slow push-in toward product centered on surface",
             "product sits clean on marble, single hand adjusts it gently"),
            ("smooth pan to product from empty space left",
             "product enters frame from left, stops centered"),
        ],
        "long": [
            ("slow orbit left 60 degrees around product",
             "product rotates slightly on turntable, all angles visible"),
            ("gentle tilt from top-down angle to eye-level",
             "product revealed from above, camera tilts to label view"),
        ],
    },
    "cta": {
        "short": [
            ("static, clean — no movement",
             "product stands alone on white background, perfectly lit"),
            ("very slow subtle zoom out to full product",
             "product centered, negative space around it"),
        ],
        "medium": [
            ("slow pull-back revealing full product on seamless white",
             "product stands upright, clean studio light, minimal shadow"),
            ("gentle tilt from close-up label to full product",
             "product revealed fully, packaging detail sharp"),
        ],
        "long": [
            ("slow top-down tilt to eye-level, product centered",
             "product sits on clean marble, camera descends to label height"),
            ("smooth 90-degree arc right, ending on label face",
             "product rotates into final hero shot position"),
        ],
    },
    "subscribe": {
        "short": [
            ("static, clean",
             "product alone on white, perfectly still"),
        ],
        "medium": [
            ("slow pull-back to wider product scene",
             "product in soft lifestyle setting, warm light"),
        ],
        "long": [
            ("gentle arc to final hero position",
             "product in aspirational context, light fades softly"),
        ],
    },
}

# role 매핑 — claude_analyzer가 만들 수 있는 다양한 role 문자열 정규화
ROLE_ALIASES: dict[str, str] = {
    # new_find
    "hook": "hook",
    "relate": "relate",
    "what_it_does": "what_it_does",
    "proof": "proof",
    "subscribe": "subscribe",
    "subscribe_cta": "subscribe",
    # problem_solution
    "agitate": "agitate",
    "solution": "solution",
    "buy_cta": "cta",
    "cta": "cta",
    # top_picks
    "product_rundown": "product_rundown",
    # 범용 fallback
    "intro": "hook",
    "outro": "cta",
    "evidence": "proof",
}

# content_type별 분위기 수식어 (프롬프트 끝에 추가)
MOOD_TOKENS: dict[str, str] = {
    "new_find":        "editorial discovery feel, clean studio lighting, premium K-beauty commercial",
    "problem_solution":"intimate skin-focused atmosphere, soft natural daylight, relatable and warm",
    "top_picks":       "bright energetic commercial style, crisp product lighting, confident and clear",
}

# 고정 constraint 라인 — 모든 씬에 붙임
CONSTRAINT_LINE = (
    "No text overlays, no logos, no watermarks, no extra objects. "
    "Preserve product silhouette and packaging design exactly. "
    "Avoid distorted edges or morphing product shape."
)


# ─── 데이터 클래스 ────────────────────────────────────────────────

@dataclass
class ScenePrompt:
    """씬 하나의 Vidu 프롬프트 조립 결과."""
    scene_index: int
    role: str
    duration: float
    camera_move: str
    subject_action: str
    full_prompt: str         # Vidu에 실제로 보내는 문단


@dataclass
class ViduRequest:
    """영상 생성 API에 보낼 최종 요청 묶음. engine에 따라 Kling/Vidu 분기."""
    candidate_id: str
    product_image_url: str       # Olive Young og:image (제품 단독 컷, 메타데이터용)
    scene_image_url: str         # ChatGPT 생성 9:16 배경 합성 이미지 (Kling referenceImage)
    scene_prompts: list[ScenePrompt]
    multishot_prompt: str        # Shot 1~N 형식으로 조립된 전체 프롬프트
    total_duration: float
    content_type: str
    engine: str = VIDEO_ENGINE_KLING   # "kling" | "vidu"


@dataclass
class ViduResult:
    """영상 생성 API 응답 결과."""
    candidate_id: str
    video_url: str
    engine: str = VIDEO_ENGINE_KLING   # 실제로 사용된 엔진
    r2_url: str = ""                   # R2 업로드 후 채움
    generation_sec: float = 0.0
    music_track_id: str = ""           # music_tracks.id (선택된 음악)
    music_local_path: str = ""         # 다운로드된 클립 로컬 경로


# ─── 제품 카테고리 감지 ───────────────────────────────────────────

# 제품명 키워드 → 카테고리 매핑
# what_it_does 씬에서 카테고리별로 다른 action을 사용하기 위함
PRODUCT_CATEGORY_KEYWORDS: dict[str, list[str]] = {
    "toner":    ["toner", "토너", "lotion", "softener", "essence water"],
    "serum":    ["serum", "세럼", "ampoule", "앰플", "booster", "concentrate"],
    "cream":    ["cream", "크림", "moisturizer", "balm", "gel cream"],
    "sunscreen":["sunscreen", "선크림", "spf", "sun cream", "uv", "sunblock"],
    "cleanser": ["cleanser", "클렌저", "foam", "cleansing", "wash", "scrub"],
    "mask":     ["mask", "마스크", "sheet mask", "pack", "패치", "patch"],
    "oil":      ["oil", "오일", "face oil", "dry oil"],
    "mist":     ["mist", "미스트", "spray", "setting spray"],
}

# 카테고리별 what_it_does action (bucket별)
# 제품 제형/사용법에 맞는 시각적 액션
CATEGORY_WHAT_IT_DOES: dict[str, dict[str, str]] = {
    "toner": {
        "short": "toner mist sprays lightly into air, droplets catch light",
        "medium": "cotton pad soaked in toner pressed gently to back of hand, skin visibly plumps",
        "long": "toner poured onto palm, hand pats it onto skin in gentle upward motion, absorbs instantly",
    },
    "serum": {
        "short": "single drop of serum falls in slow motion onto fingertip",
        "medium": "serum dispensed from dropper onto fingertip, spread across back of hand",
        "long": "serum drop on fingertip pressed into cheek skin, spreads naturally, no residue",
    },
    "cream": {
        "short": "small pearl of cream sits on back of hand, light catches texture",
        "medium": "fingertip scoops small amount of cream, swatched on wrist in slow stroke",
        "long": "cream applied to cheek with gentle tapping motion, melts into skin seamlessly",
    },
    "sunscreen": {
        "short": "sunscreen dispensed from tube onto finger, no white cast visible",
        "medium": "sunscreen spread across back of hand — blends clear, skin looks natural",
        "long": "sunscreen applied to forearm in smooth strokes, absorbed without greasiness",
    },
    "cleanser": {
        "short": "small amount of cleanser pressed onto palm, lathers slightly",
        "medium": "cleanser foams between hands under water, rich lather builds",
        "long": "foam cleanser applied to face in circular motion, skin squeaky clean after rinse",
    },
    "mask": {
        "short": "sheet mask unfolded slowly, essence drips from surface",
        "medium": "sheet mask laid flat, essence glistening under soft light",
        "long": "sheet mask applied to face, hands smooth it flat, edges sealed",
    },
    "oil": {
        "short": "single drop of oil on fingertip, golden tone under light",
        "medium": "oil massaged between palms, pressed gently onto cheeks",
        "long": "face oil applied in upward strokes, skin develops subtle glow",
    },
    "mist": {
        "short": "mist sprayed into air, fine droplets catch backlight",
        "medium": "mist bottle held at arm's length, spray settles on skin",
        "long": "mist sprayed across face in sweeping motion, fine droplets visible in slow motion",
    },
    # 감지 실패 시 fallback
    "unknown": {
        "short": "product texture fills the frame, light catching surface detail",
        "medium": "product dispensed onto fingertip in a small controlled amount",
        "long": "product applied to skin gently, absorbs naturally",
    },
}


def _detect_product_category(product_name: str) -> str:
    """
    제품명 문자열에서 카테고리를 감지.
    감지 실패 시 'unknown' 반환.
    """
    name_lower = product_name.lower()
    for category, keywords in PRODUCT_CATEGORY_KEYWORDS.items():
        if any(kw in name_lower for kw in keywords):
            return category
    return "unknown"


def _pick_shot_what_it_does(bucket: str, product_name: str) -> tuple[str, str]:
    """
    what_it_does role 전용 — 제품 카테고리를 감지해서 카테고리별 action 반환.
    camera_move는 일반 pool에서 그대로 가져오고, subject_action만 교체.
    """
    category = _detect_product_category(product_name)
    category_actions = CATEGORY_WHAT_IT_DOES.get(category, CATEGORY_WHAT_IT_DOES["unknown"])
    subject_action = category_actions.get(bucket, category_actions["medium"])

    # camera_move는 what_it_does pool에서 그대로 선택
    pool = ROLE_SHOT_POOL["what_it_does"]
    fallback_order = {"short": ["short","medium","long"],
                      "medium": ["medium","short","long"],
                      "long": ["long","medium","short"]}
    camera_move = "slow macro push-in on product"
    for b in fallback_order[bucket]:
        if b in pool and pool[b]:
            camera_move = random.choice(pool[b])[0]
            break

    return camera_move, subject_action


# ─── 유틸 ─────────────────────────────────────────────────────────

def _normalize_role(raw_role: str) -> str:
    """claude_analyzer가 만든 다양한 role 문자열을 shot pool 키로 정규화."""
    key = raw_role.lower().strip().replace(" ", "_").replace("-", "_")
    return ROLE_ALIASES.get(key, "proof")   # 알 수 없는 role은 proof로 fallback


def _duration_bucket(duration: float) -> str:
    """duration(초) → 'short' | 'medium' | 'long'"""
    if duration < SHORT_SEC:
        return "short"
    if duration < LONG_SEC:
        return "medium"
    return "long"


def _pick_shot(
    role: str,
    bucket: str,
    last_shot: tuple | None = None,
) -> tuple:
    """
    role + duration bucket으로 (camera_move, subject_action) 랜덤 선택.
    해당 bucket이 없으면 인접 bucket으로 fallback.

    last_shot: 직전 씬에서 선택된 (camera_move, subject_action).
               동일 role이 연속될 때 같은 항목이 반복되지 않도록 제외.
               pool에 항목이 1개뿐이면 중복 허용 (선택지 없음).
    """
    pool = ROLE_SHOT_POOL.get(role, ROLE_SHOT_POOL["proof"])
    fallback_order = {
        "short":  ["short", "medium", "long"],
        "medium": ["medium", "short", "long"],
        "long":   ["long", "medium", "short"],
    }
    candidates: list = []
    for b in fallback_order[bucket]:
        if b in pool and pool[b]:
            candidates = pool[b]
            break
    if not candidates:
        return ("slow push-in", "product stands on surface, light sweeps across")
    if last_shot and len(candidates) > 1:
        filtered = [c for c in candidates if c != last_shot]
        return random.choice(filtered if filtered else candidates)
    return random.choice(candidates)


def _build_scene_prompt(
    scene: dict,
    content_type: str,
    product_name: str,
    last_shot: tuple | None = None,
) -> ScenePrompt:
    """
    씬 dict 하나 → ScenePrompt 조립.

    scene: {"scene_index", "start", "end", "role", "text", "duration"(optional)}
    last_shot: 직전 씬의 (camera_move, subject_action). 연속 중복 방지용.
    """
    scene_index = scene["scene_index"]
    raw_role = scene.get("role", "proof")
    start = scene["start"]
    end = scene["end"]
    duration = scene.get("duration", end - start)

    role = _normalize_role(raw_role)
    bucket = _duration_bucket(duration)

    # what_it_does는 제품 카테고리별 action으로 분기
    if role == "what_it_does":
        camera_move, subject_action = _pick_shot_what_it_does(bucket, product_name)
    else:
        camera_move, subject_action = _pick_shot(role, bucket, last_shot=last_shot)

    mood = MOOD_TOKENS.get(content_type, MOOD_TOKENS["new_find"])

    # 프롬프트 조립: [Subject+Setting] [Action] [Camera] [Lighting+Mood] [Constraint]
    # product_name은 Subject 앵커로 사용 — Vidu가 레퍼런스 이미지와 연결하도록
    full_prompt = (
        f"{product_name} skincare product on a clean surface in a minimal studio setting. "
        f"{subject_action}. "
        f"{camera_move}. "
        f"Soft directional lighting, shallow depth of field, realistic materials "
        f"(glass, pump, matte plastic), subtle specular highlights, {mood}. "
        f"{CONSTRAINT_LINE}"
    )

    return ScenePrompt(
        scene_index=scene_index,
        role=role,
        duration=duration,
        camera_move=camera_move,
        subject_action=subject_action,
        full_prompt=full_prompt,
    )


def _build_multishot_prompt(scene_prompts: list[ScenePrompt]) -> str:
    """
    씬별 ScenePrompt → Vidu 멀티샷 프롬프트 문자열.

    Vidu Q3 멀티샷은 "Shot N (Xs): [prompt]" 형식으로 시간 정보와 함께 작성한다.
    씬 경계 전환 큐도 포함 (match action / focus handoff / light cue).
    """
    lines = []
    for i, sp in enumerate(scene_prompts):
        duration_str = f"{sp.duration:.1f}s"
        transition_cue = _pick_transition_cue(
            current_role=sp.role,
            next_role=scene_prompts[i + 1].role if i + 1 < len(scene_prompts) else None,
        )
        shot_line = f"Shot {i + 1} ({duration_str}): {sp.full_prompt}"
        if transition_cue:
            shot_line += f" {transition_cue}"
        lines.append(shot_line)

    return "\n".join(lines)


def _pick_transition_cue(current_role: str, next_role: str | None) -> str:
    """
    현재 씬 role + 다음 씬 role 조합으로 전환 큐 선택.
    마지막 씬이면 빈 문자열 반환.
    """
    if next_role is None:
        return ""

    # role 쌍별 전환 큐
    cue_map: dict[tuple[str, str], list[str]] = {
        ("hook",          "relate"):         ["Cut as light reaches peak brightness."],
        ("hook",          "what_it_does"):   ["Cut as product label comes into sharp focus."],
        ("hook",          "agitate"):        ["Cut on the moment product is fully revealed."],
        ("relate",        "what_it_does"):   ["Match action cut as hand sets product down; next shot opens with product already steady."],
        ("relate",        "solution"):       ["Cut as hand lifts toward face; next shot opens with application in progress."],
        ("agitate",       "solution"):       ["Hard cut to clean white space — reset energy."],
        ("what_it_does",  "proof"):          ["Rack focus from product to background; cut on resolve."],
        ("solution",      "proof"):          ["Cut as product is set down cleanly."],
        ("proof",         "cta"):            ["Slow fade through white into final hero shot."],
        ("proof",         "subscribe"):      ["Gentle pull-back cue: camera begins widening as cut happens."],
        ("product_rundown","product_rundown"):["Match action cut: next product placed down as previous exits frame."],
        ("product_rundown","cta"):           ["Final product placed center frame; cut on stillness."],
        ("cta",           "subscribe"):      ["Static hold — no camera cue needed."],
    }

    key = (current_role, next_role)
    options = cue_map.get(key)
    if options:
        return random.choice(options)

    # 범용 fallback 전환 큐
    generic = [
        "Cut on the peak of motion.",
        "Match action cut.",
        "Cut as camera move completes.",
    ]
    return random.choice(generic)


# ─── Olive Young 이미지 파싱 ──────────────────────────────────────

def fetch_product_image_url(prdt_no: str) -> str | None:
    """
    Olive Young Global product detail 페이지에서 og:image URL을 파싱.

    호출 시점: 큐레이터가 prdtNo를 DB에 입력했을 때 1회만 실행.
    이후에는 R2 캐시 URL을 사용하므로 재호출 없음.

    테스트 결과 (2026-07-05):
      - product detail 페이지: 200 OK, og:image 파싱 성공
      - 검색 페이지: Cloudflare 차단 (사용 불가)
    """
    url = f"https://global.oliveyoung.com/product/detail?prdtNo={prdt_no}"
    try:
        r = requests.get(
            url,
            headers={
                "User-Agent": (
                    "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) "
                    "AppleWebKit/537.36 (KHTML, like Gecko) "
                    "Chrome/124.0.0.0 Safari/537.36"
                ),
                "Accept-Language": "en-US,en;q=0.9",
            },
            timeout=10,
        )
        if r.status_code != 200:
            logger.warning(f"Olive Young 이미지 파싱 실패: status={r.status_code}, prdtNo={prdt_no}")
            return None

        soup = BeautifulSoup(r.text, "html.parser")
        og_image = soup.find("meta", property="og:image")
        if og_image and og_image.get("content"):
            image_url = og_image["content"]
            logger.info(f"Olive Young 이미지 파싱 성공: {image_url}")
            return image_url

        logger.warning(f"og:image 태그 없음: prdtNo={prdt_no}")
        return None

    except Exception as e:
        logger.error(f"Olive Young 이미지 파싱 오류: {e}")
        return None


# ─── 핵심 함수 ───────────────────────────────────────────────────

def generate_vidu_request(candidate: dict) -> ViduRequest:
    """
    DB candidate dict → ViduRequest 조립.

    candidate 필수 필드:
      - id: str
      - scenes: list[dict]  (scene_index, start, end, role, text)
      - content_type: str
      - products: list[str]
      - product_image_url: str  (R2 캐시 URL 또는 Olive Young CDN URL)
    """
    candidate_id = candidate["id"]
    scenes = candidate["scenes"]
    content_type = candidate.get("content_type", "new_find")
    products = candidate.get("products", [])
    product_image_url = candidate.get("product_image_url", "")
    # scene_image_url: ChatGPT로 생성한 9:16 배경 합성 이미지 (Kling referenceImage)
    # 없으면 product_image_url로 fallback (품질 저하 가능)
    scene_image_url = candidate.get("scene_image_url") or product_image_url
    product_name = products[0] if products else "K-beauty skincare product"

    if not scenes:
        raise ValueError(f"candidate {candidate_id}: scenes가 비어있습니다.")
    if not product_image_url:
        raise ValueError(f"candidate {candidate_id}: product_image_url이 없습니다.")
    if not scene_image_url:
        raise ValueError(f"candidate {candidate_id}: scene_image_url이 없습니다.")

    # 씬별 프롬프트 조립 — 연속 동일 role 중복 방지를 위해 last_shot 추적
    scene_prompts: list[ScenePrompt] = []
    last_shot: tuple | None = None
    for scene in sorted(scenes, key=lambda s: s["scene_index"]):
        sp = _build_scene_prompt(scene, content_type, product_name, last_shot=last_shot)
        last_shot = (sp.camera_move, sp.subject_action)
        scene_prompts.append(sp)

    # 멀티샷 프롬프트 문자열 조립
    multishot_prompt = _build_multishot_prompt(scene_prompts)
    total_duration = scenes[-1]["end"] - scenes[0]["start"]

    logger.info(
        f"ViduRequest 조립 완료: candidate={candidate_id}, "
        f"씬={len(scene_prompts)}개, total={total_duration:.1f}s"
    )

    return ViduRequest(
        candidate_id=candidate_id,
        product_image_url=product_image_url,
        scene_image_url=scene_image_url,
        scene_prompts=scene_prompts,
        multishot_prompt=multishot_prompt,
        total_duration=total_duration,
        content_type=content_type,
    )


def _poll_until_done(
    poll_url: str,
    headers: dict,
    poll_interval: float,
    timeout: float,
    done_status: str,
    fail_statuses: list[str],
    label: str,
) -> dict:
    """
    공통 polling 루프. done_status가 될 때까지 poll_url을 반복 조회.
    Returns: 완료 시점의 응답 JSON dict.
    """
    start_time = time.time()
    while True:
        elapsed = time.time() - start_time
        if elapsed > timeout:
            raise TimeoutError(f"{label} 타임아웃: elapsed={elapsed:.0f}s")
        time.sleep(poll_interval)
        r = requests.get(poll_url, headers=headers, timeout=15)
        r.raise_for_status()
        data = r.json()
        status = data.get("status", "")
        logger.info(f"{label} polling: status={status}, elapsed={elapsed:.0f}s")
        if status == done_status:
            return data
        if status in fail_statuses:
            raise RuntimeError(f"{label} 실패: status={status}, 응답={data}")


def _build_kling_multi_prompt(scene_prompts: list[ScenePrompt], total_duration: float) -> list[dict]:
    """
    Kling multi_prompt 파라미터 형식으로 변환.
    Kling 최대 10초 제약 때문에 씬을 10초 청크로 묶는다.

    Returns:
        [{"index": int, "prompt": str, "duration": str}, ...]
        각 청크 duration 합 = total_duration (소수점 반올림 처리).

    Kling multi_prompt 스펙:
      - 최대 6개 스토리보드
      - 각 스토리보드 prompt 최대 512자
      - duration 합 = 영상 총 길이
    """
    MAX_STORYBOARDS = 6
    # 씬을 MAX_STORYBOARDS개 이하로 균등 묶음
    n = len(scene_prompts)
    chunk_size = max(1, (n + MAX_STORYBOARDS - 1) // MAX_STORYBOARDS)

    chunks: list[dict] = []
    for i in range(0, n, chunk_size):
        group = scene_prompts[i:i + chunk_size]
        # 청크 내 씬 중 가장 긴 프롬프트를 대표 프롬프트로 (512자 제한)
        rep_prompt = max(group, key=lambda sp: len(sp.full_prompt)).full_prompt[:512]
        chunk_duration = sum(sp.duration for sp in group)
        chunks.append({
            "index": len(chunks) + 1,  # Kling index는 1부터 시작
            "prompt": rep_prompt,
            "duration": str(round(chunk_duration)),
        })

    # duration 합이 정확히 맞도록 마지막 청크 보정
    total_assigned = sum(int(c["duration"]) for c in chunks)
    target = round(total_duration)
    if chunks and total_assigned != target:
        chunks[-1]["duration"] = str(int(chunks[-1]["duration"]) + (target - total_assigned))

    return chunks


def call_kling_api(vidu_request: ViduRequest) -> str:
    """
    Kling v3.0 Image-to-Video API 호출 → 영상 URL 반환.

    공식 Kling AI API Platform (kling.ai/document-api) 사용.
    multi_prompt로 멀티샷 스토리보드 전달.
    비동기 패턴: POST 제출 → GET polling → 완료 URL.

    Kling 최대 10초 제약:
      35초 영상은 여러 청크로 분할해서 생성하고 ffmpeg로 이어붙인다.
      현재 구현은 단일 호출(최대 10초)로 첫 번째 청크만 생성.
      TODO: 청크 분할 + ffmpeg 이어붙임은 Phase 2-3에서 구현.

    Returns:
        생성된 영상의 임시 CDN URL (24시간 유효 — 즉시 R2에 저장해야 함)
    """
    if not getattr(config, "KLING_API_KEY", None):
        raise RuntimeError("KLING_API_KEY가 config에 설정되지 않았습니다.")

    headers = {
        "Authorization": f"Bearer {config.KLING_API_KEY}",
        "Content-Type": "application/json",
    }

    multi_prompt = _build_kling_multi_prompt(
        vidu_request.scene_prompts, vidu_request.total_duration
    )
    duration_sec = min(int(vidu_request.total_duration), KLING_MAX_DURATION_SEC)

    payload = {
        "model_name":   KLING_MODEL,
        "image":        vidu_request.scene_image_url,  # 9:16 배경 합성 이미지 (Kling referenceImage)
        "multi_shot":   "true",
        "shot_type":    "customize",
        "multi_prompt": multi_prompt,
        "duration":     duration_sec,
        "aspect_ratio": VIDEO_ASPECT_RATIO,
        "mode":         "std",
    }

    logger.info(
        f"Kling API 호출 시작: candidate={vidu_request.candidate_id}, "
        f"duration={duration_sec}s, storyboards={len(multi_prompt)}"
    )
    logger.debug(f"multi_prompt: {multi_prompt}")

    url = f"{KLING_API_BASE}{KLING_IMAGE2VIDEO_PATH}"
    r = requests.post(url, json=payload, headers=headers, timeout=30)
    r.raise_for_status()
    resp = r.json()

    task_id = (resp.get("data", {}).get("task_id")
               or resp.get("task_id")
               or resp.get("id"))
    if not task_id:
        raise RuntimeError(f"Kling API: task_id 없음. 응답: {resp}")
    logger.info(f"Kling 태스크 제출 완료: task_id={task_id}")

    poll_url = f"{KLING_API_BASE}{KLING_TASK_PATH.format(task_id=task_id)}"
    done_data = _poll_until_done(
        poll_url=poll_url,
        headers=headers,
        poll_interval=KLING_POLL_INTERVAL_SEC,
        timeout=KLING_POLL_TIMEOUT_SEC,
        done_status="succeed",
        fail_statuses=["failed", "error"],
        label=f"Kling[{task_id}]",
    )

    # Kling 응답 구조 (실제 확인): data.task_result.videos[0].url
    try:
        video_url = done_data["data"]["task_result"]["videos"][0]["url"]
    except (KeyError, IndexError):
        video_url = (done_data.get("data", {}).get("video_url")
                     or done_data.get("video_url"))
    if not video_url:
        raise RuntimeError(f"Kling 완료됐으나 video_url 없음: {done_data}")

    logger.info(f"Kling 생성 완료: {video_url}")
    return video_url


def call_vidu_api(vidu_request: ViduRequest) -> str:
    """
    Vidu Q3 Reference-to-Video API 호출 → 영상 URL 반환.

    공식 Vidu API Platform (platform.vidu.com) 사용.
    비동기 태스크 패턴: POST 제출 → GET polling → 완료 URL.

    Vidu 최대 16초 제약:
      35초 영상은 여러 청크로 분할 후 ffmpeg 이어붙임.
      TODO: 청크 분할은 Phase 2-3에서 구현.

    Returns:
        생성된 영상의 임시 CDN URL (만료 전 R2에 저장해야 함)
    """
    if not getattr(config, "VIDU_API_KEY", None):
        raise RuntimeError("VIDU_API_KEY가 config에 설정되지 않았습니다.")

    headers = {
        "Authorization": f"Token {config.VIDU_API_KEY}",
        "Content-Type": "application/json",
    }

    duration_sec = min(int(vidu_request.total_duration), VIDU_MAX_DURATION_SEC)

    payload = {
        "model":      VIDU_MODEL,
        "images":     [vidu_request.product_image_url],
        "prompt":     vidu_request.multishot_prompt,
        "duration":   duration_sec,
        "aspect_ratio": VIDEO_ASPECT_RATIO,
        "resolution": VIDEO_RESOLUTION,
        "audio":      False,
    }

    logger.info(
        f"Vidu API 호출 시작: candidate={vidu_request.candidate_id}, "
        f"duration={duration_sec}s"
    )

    url = f"{VIDU_API_BASE}{VIDU_REF2VIDEO_PATH}"
    r = requests.post(url, json=payload, headers=headers, timeout=30)
    r.raise_for_status()
    resp = r.json()

    task_id = resp.get("data", {}).get("id") or resp.get("task_id") or resp.get("id")
    if not task_id:
        raise RuntimeError(f"Vidu API: task_id 없음. 응답: {resp}")
    logger.info(f"Vidu 태스크 제출 완료: task_id={task_id}")

    poll_url = f"{VIDU_API_BASE}{VIDU_TASK_PATH.format(task_id=task_id)}"
    done_data = _poll_until_done(
        poll_url=poll_url,
        headers=headers,
        poll_interval=VIDU_POLL_INTERVAL_SEC,
        timeout=VIDU_POLL_TIMEOUT_SEC,
        done_status="success",
        fail_statuses=["failed", "error"],
        label=f"Vidu[{task_id}]",
    )

    # Vidu 응답 구조: data.creations[0].url
    try:
        video_url = done_data["data"]["creations"][0]["url"]
    except (KeyError, IndexError):
        video_url = done_data.get("data", {}).get("url") or done_data.get("url")
    if not video_url:
        raise RuntimeError(f"Vidu 완료됐으나 video_url 없음: {done_data}")

    logger.info(f"Vidu 생성 완료: {video_url}")
    return video_url


def call_video_api(vidu_request: ViduRequest) -> str:
    """
    config.VIDEO_ENGINE에 따라 Kling 또는 Vidu API로 라우팅.

    사용법:
        # config.py
        VIDEO_ENGINE = "kling"   # Kling Trial Package 소진 중
        VIDEO_ENGINE = "vidu"    # Vidu로 전환 시

    Returns:
        생성된 영상의 임시 CDN URL
    """
    engine = getattr(config, "VIDEO_ENGINE", VIDEO_ENGINE_KLING)

    if engine == VIDEO_ENGINE_KLING:
        return call_kling_api(vidu_request)
    elif engine == VIDEO_ENGINE_VIDU:
        return call_vidu_api(vidu_request)
    else:
        raise ValueError(
            f"알 수 없는 VIDEO_ENGINE: '{engine}'. "
            f"config.py에서 'kling' 또는 'vidu'로 설정하세요."
        )


def run_generation(candidate_id: str) -> ViduResult:
    """
    단일 candidate에 대해 영상 생성 전체 파이프라인 실행.

    1. DB에서 candidate 로드
    2. ViduRequest 조립
    3. Vidu API 호출
    4. (TODO Phase 2-3) R2 업로드
    5. DB status 업데이트

    Args:
        candidate_id: Supabase content_candidates.id

    Returns:
        ViduResult
    """
    # 1. DB에서 candidate 로드
    candidate = db.get_candidate(candidate_id)
    if not candidate:
        raise ValueError(f"candidate {candidate_id}를 DB에서 찾을 수 없습니다.")

    # 2. ViduRequest 조립 (engine 정보 포함)
    engine = getattr(config, "VIDEO_ENGINE", VIDEO_ENGINE_KLING)
    vidu_request = generate_vidu_request(candidate)
    vidu_request.engine = engine

    # 3. 영상 생성 API 호출 (engine에 따라 Kling / Vidu 자동 분기)
    start = time.time()
    video_url = call_video_api(vidu_request)
    generation_sec = time.time() - start

    result = ViduResult(
        candidate_id=candidate_id,
        video_url=video_url,
        engine=engine,
        generation_sec=generation_sec,
    )

    # 4. 음악 선택 (music_tracks에서 랜덤 선택 + R2 클립 다운로드)
    music_track_id = None
    try:
        import sys as _sys
        from pathlib import Path as _Path
        _sys.path.insert(0, str(_Path(__file__).parent.parent))
        from producer.music_selector import select_track
        track = select_track()
        result.music_track_id = track.id
        result.music_local_path = track.local_clip_path
        music_track_id = track.id
        logger.info(f"음악 선택 완료: [{track.mood}] {track.title} - {track.artist}")
    except Exception as e:
        logger.warning(f"음악 선택 실패 (영상 생성은 완료됨): {e}")

    # 5. TODO: R2 업로드 (Phase 2-3에서 구현)
    # result.r2_url = upload_to_r2(video_url, candidate_id)

    # 6. DB 상태 업데이트
    extra = {
        "video_url": video_url,
        "video_engine": engine,
        "video_generation_sec": round(generation_sec, 1),
    }
    if music_track_id:
        extra["music_track_id"] = music_track_id
    db.update_candidate_status(candidate_id, "video_ready", extra)
    logger.info(
        f"영상 생성 완료: candidate={candidate_id}, engine={engine}, "
        f"video_url={video_url}, elapsed={generation_sec:.1f}s"
    )

    return result


def run_batch(limit: int = 5) -> list[ViduResult]:
    """
    pending 상태 candidates를 최대 limit개 처리.
    스케줄러 진입점.
    """
    candidates = db.get_candidates_by_status("pending", limit=limit)
    if not candidates:
        logger.info("처리할 pending candidate 없음")
        return []

    results = []
    for candidate in candidates:
        cid = candidate["id"]
        try:
            result = run_generation(cid)
            results.append(result)
        except Exception as e:
            logger.error(f"영상 생성 실패: candidate={cid}, error={e}")
            db.update_candidate_status(cid, "generation_failed", {"error": str(e)})
            continue

    logger.info(f"배치 완료: {len(results)}/{len(candidates)}개 성공")
    return results


# ─── 디버그용 프롬프트 미리보기 ──────────────────────────────────

def preview_prompts(candidate: dict) -> None:
    """
    실제 API 호출 없이 생성될 Vidu 프롬프트를 터미널에 출력.
    개발/검증용.

    사용:
        from script_generator import preview_prompts
        preview_prompts(candidate_dict)
    """
    vr = generate_vidu_request(candidate)
    print(f"\n{'='*70}")
    print(f"candidate: {vr.candidate_id}")
    print(f"content_type: {vr.content_type}")
    print(f"product_image_url: {vr.product_image_url}")
    print(f"scene_image_url:   {vr.scene_image_url}")
    print(f"total_duration: {vr.total_duration:.1f}s  |  씬 수: {len(vr.scene_prompts)}")
    print(f"{'='*70}")
    print("\n[멀티샷 프롬프트]\n")
    print(vr.multishot_prompt)
    print(f"\n{'='*70}")
    for sp in vr.scene_prompts:
        print(
            f"씬 {sp.scene_index} | role={sp.role} | {sp.duration:.1f}s | "
            f"bucket={_duration_bucket(sp.duration)}"
        )
        print(f"  camera: {sp.camera_move}")
        print(f"  action: {sp.subject_action}")
    print()


if __name__ == "__main__":
    # 수동 검증용 CLI: python script_generator.py <candidate_id>
    # 또는 mock 데이터로 프롬프트 미리보기
    import json
    import sys

    logging.basicConfig(level=logging.INFO, format="%(levelname)s %(message)s")

    if len(sys.argv) == 2 and sys.argv[1] == "--mock":
        # mock candidate 3종 — 카테고리별 action 분기 검증
        mock_cases = [
            {
                "id": "mock-toner",
                "content_type": "new_find",
                "products": ["Round Lab Birch Juice Moisturizing Toner"],
                "product_image_url": "https://cdn-image.oliveyoung.com/mock.jpg",
                "scene_image_url": "https://r2.example.com/scene/mock-toner-916.jpg",
                "scenes": [
                    {"scene_index": 0, "start": 0.0,  "end": 3.2,  "role": "hook",         "text": "This Korean toner sold out in 3 hours."},
                    {"scene_index": 1, "start": 3.2,  "end": 9.8,  "role": "relate",       "text": "If you've been struggling to find a toner that actually works..."},
                    {"scene_index": 2, "start": 9.8,  "end": 22.1, "role": "what_it_does", "text": "Round Lab's birch juice formula delivers deep hydration without stickiness."},
                    {"scene_index": 3, "start": 22.1, "end": 30.5, "role": "proof",        "text": "Over 50,000 repurchases on Olive Young."},
                    {"scene_index": 4, "start": 30.5, "end": 35.8, "role": "subscribe",    "text": "Follow for K-beauty finds before they blow up globally."},
                ],
            },
            {
                "id": "mock-serum",
                "content_type": "problem_solution",
                "products": ["medicube PDRN Pink Peptide Ampoule"],
                "product_image_url": "https://cdn-image.oliveyoung.com/mock2.jpg",
                "scene_image_url": "https://r2.example.com/scene/mock-serum-916.jpg",
                "scenes": [
                    {"scene_index": 0, "start": 0.0,  "end": 3.5,  "role": "hook",     "text": "If your skin feels dull no matter what you try..."},
                    {"scene_index": 1, "start": 3.5,  "end": 8.0,  "role": "agitate",  "text": "Most serums sit on the surface and do nothing."},
                    {"scene_index": 2, "start": 8.0,  "end": 21.0, "role": "solution",  "text": "PDRN technology actually repairs skin at a cellular level."},
                    {"scene_index": 3, "start": 21.0, "end": 29.0, "role": "proof",     "text": "Korean dermatology clinics recommend this ingredient."},
                    {"scene_index": 4, "start": 29.0, "end": 35.0, "role": "cta",       "text": "Link in description."},
                ],
            },
            {
                "id": "mock-sunscreen",
                "content_type": "top_picks",
                "products": ["Beauty of Joseon Relief Sun SPF50+"],
                "product_image_url": "https://cdn-image.oliveyoung.com/mock3.jpg",
                "scene_image_url": "https://r2.example.com/scene/mock-sunscreen-916.jpg",
                "scenes": [
                    {"scene_index": 0, "start": 0.0,  "end": 4.0,  "role": "hook",            "text": "The 3 most repurchased Korean sunscreens right now."},
                    {"scene_index": 1, "start": 4.0,  "end": 13.0, "role": "product_rundown",  "text": "Number 3: Isntree Hyaluronic Acid Sun Serum."},
                    {"scene_index": 2, "start": 13.0, "end": 22.0, "role": "product_rundown",  "text": "Number 2: Round Lab Dokdo Sunscreen."},
                    {"scene_index": 3, "start": 22.0, "end": 31.0, "role": "product_rundown",  "text": "Number 1: Beauty of Joseon Relief Sun."},
                    {"scene_index": 4, "start": 31.0, "end": 35.5, "role": "cta",              "text": "Links in description."},
                ],
            },
        ]
        for mock in mock_cases:
            preview_prompts(mock)
            print()

    elif len(sys.argv) == 2:
        candidate_id = sys.argv[1]
        result = run_generation(candidate_id)
        print(f"완료: video_url={result.video_url}")

    else:
        print("사용법:")
        print("  python script_generator.py --mock          # mock 데이터로 프롬프트 미리보기")
        print("  python script_generator.py <candidate_id>  # 실제 생성")
