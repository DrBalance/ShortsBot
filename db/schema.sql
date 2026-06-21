-- ============================================================
-- K뷰티 쇼츠 자동화 - Supabase 스키마
-- Supabase SQL Editor에서 실행하세요
-- ============================================================

-- 수집된 인스타 게시물 원본
CREATE TABLE IF NOT EXISTS kbeauty_raw_posts (
    id              UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    instagram_id    TEXT UNIQUE NOT NULL,        -- 인스타 게시물 ID
    hashtag         TEXT NOT NULL,               -- 수집 출처 해시태그
    caption         TEXT,                        -- 게시물 본문
    likes_count     INTEGER DEFAULT 0,
    comments_count  INTEGER DEFAULT 0,
    image_urls      JSONB DEFAULT '[]',          -- 이미지 URL 목록
    posted_at       TIMESTAMPTZ,                 -- 원본 게시 시각
    collected_at    TIMESTAMPTZ DEFAULT NOW(),
    is_processed    BOOLEAN DEFAULT FALSE        -- Claude 분석 완료 여부
);

-- Claude가 분석한 콘텐츠 후보 목록
CREATE TABLE IF NOT EXISTS kbeauty_content_candidates (
    id              UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    raw_post_id     UUID REFERENCES kbeauty_raw_posts(id),
    
    -- 트렌드 분석 결과
    trend_topic     TEXT NOT NULL,               -- 예: "선크림 TOP3"
    products        JSONB DEFAULT '[]',          -- 언급된 제품 목록
    keywords        JSONB DEFAULT '[]',          -- 핵심 키워드
    relevance_score FLOAT DEFAULT 0,             -- 관련도 점수 (0~1)
    
    -- 쇼츠 제작용 데이터
    shorts_title    TEXT,                        -- 영상 제목 (한국어)
    shorts_script   TEXT,                        -- 나레이션 스크립트 (한국어)
    hook_line       TEXT,                        -- 첫 3초 후킹 문구
    
    -- 상태 관리
    status          TEXT DEFAULT 'pending'
                    CHECK (status IN (
                        'pending',      -- 대기 중
                        'producing',    -- 제작 중
                        'produced',     -- 제작 완료
                        'uploading',    -- 업로드 중
                        'uploaded',     -- 업로드 완료
                        'failed'        -- 실패
                    )),
    
    created_at      TIMESTAMPTZ DEFAULT NOW(),
    updated_at      TIMESTAMPTZ DEFAULT NOW()
);

-- 제작된 영상
CREATE TABLE IF NOT EXISTS kbeauty_videos (
    id              UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    candidate_id    UUID REFERENCES kbeauty_content_candidates(id),
    
    -- 파일 경로
    video_r2_key    TEXT,                        -- R2 저장 경로
    video_url       TEXT,                        -- 공개 URL
    
    -- 자막 (다국어)
    subtitles       JSONB DEFAULT '{}',          -- {"ko": "...", "en": "...", ...}
    
    -- 유튜브 업로드 정보
    youtube_video_id    TEXT,
    youtube_url         TEXT,
    youtube_title       JSONB DEFAULT '{}',      -- 언어별 제목
    youtube_description JSONB DEFAULT '{}',      -- 언어별 설명
    scheduled_at        TIMESTAMPTZ,             -- 예약 업로드 시각
    uploaded_at         TIMESTAMPTZ,
    
    -- 성과 지표 (Analytics에서 주기적으로 업데이트)
    views           INTEGER DEFAULT 0,
    likes           INTEGER DEFAULT 0,
    ctr             FLOAT DEFAULT 0,             -- 클릭률
    watch_time_avg  FLOAT DEFAULT 0,             -- 평균 시청 시간(초)
    
    created_at      TIMESTAMPTZ DEFAULT NOW(),
    updated_at      TIMESTAMPTZ DEFAULT NOW()
);

-- 수익 기록
CREATE TABLE IF NOT EXISTS kbeauty_revenue (
    id              UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    video_id        UUID REFERENCES kbeauty_videos(id),
    date            DATE NOT NULL,
    source          TEXT CHECK (source IN ('oliveyoung_curator', 'youtube_shopping')),
    clicks          INTEGER DEFAULT 0,
    purchases       INTEGER DEFAULT 0,
    revenue_krw     INTEGER DEFAULT 0,           -- 원화 수익
    created_at      TIMESTAMPTZ DEFAULT NOW()
);

-- updated_at 자동 갱신 트리거
CREATE OR REPLACE FUNCTION update_updated_at()
RETURNS TRIGGER AS $$
BEGIN
    NEW.updated_at = NOW();
    RETURN NEW;
END;
$$ LANGUAGE plpgsql;

CREATE TRIGGER trg_candidates_updated_at
    BEFORE UPDATE ON kbeauty_content_candidates
    FOR EACH ROW EXECUTE FUNCTION update_updated_at();

CREATE TRIGGER trg_videos_updated_at
    BEFORE UPDATE ON kbeauty_videos
    FOR EACH ROW EXECUTE FUNCTION update_updated_at();

-- 인덱스
CREATE INDEX IF NOT EXISTS idx_raw_posts_processed ON kbeauty_raw_posts(is_processed);
CREATE INDEX IF NOT EXISTS idx_candidates_status ON kbeauty_content_candidates(status);
CREATE INDEX IF NOT EXISTS idx_videos_candidate ON kbeauty_videos(candidate_id);
