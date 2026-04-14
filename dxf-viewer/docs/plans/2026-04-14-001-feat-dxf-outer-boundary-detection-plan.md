---
title: feat: Add DXF outer boundary detection with overlay
type: feat
status: active
date: 2026-04-14
deepened: 2026-04-14
---

# feat: Add DXF outer boundary detection with overlay

## Overview

DXF 뷰어에 외곽선 자동 탐지 기능을 추가합니다. 클라이언트에서 업로드한 DXF 파일을 백엔드 Python API로 전송하여 9단계 알고리즘으로 외곽선을 탐지하고, 결과를 dxf-viewer 캔버스에 레이어로 오버레이합니다.

## Problem Frame

현재 DXF 뷰어는 도면을 표시만 하고, 건물의 외곽선을 자동으로 인식하는 기능이 없습니다. 사용자가 수동으로 외곽선을 찾아야 하므로 비효율적입니다. CAD 도면에서 자동으로 건물 외곽선을 추출하여 시각화하면 도면 분석 작업이 훨씬 효율적이 됩니다.

## Requirements Trace

### Functional Requirements

- **R1**: 클라이언트에서 DXF 파일 업로드 시 백엔드 API로 전송
- **R2**: 백엔드에서 9단계 알고리즘으로 외곽선 탐지
- **R3**: 탐지된 외곽선을 dxf-viewer 캔버스에 레이어로 오버레이
- **R4**: 외곽선 오버레이 토글 기능 제공
- **R5**: 처리 결과 메타데이터 (탐지된 면적, 신뢰도 등) 표시

### Algorithm Requirements (9-Step Pipeline)

- **R6** (STEP 1): ezdxf로 DXF 파싱 및 INSERT 재귀적 전개
- **R7** (STEP 2): 엔티티를 (start, end, meta) 세그먼트로 정규화. **ARC는 N개의 짧은 LINE으로 근사 변환** (Option A)
- **R8** (STEP 3): tolerance 스냅 선행 (KD-tree + Union-Find) → shapely unary_union으로 교차점 분할 (Noding)
- **R9** (STEP 4): scipy KD-tree + Union-Find로 tolerance 스냅 (STEP 3에서 일부 수행됨)
- **R10** (STEP 5): degree=1 노드 반복 제거로 Dangling Edge Pruning
- **R11** (STEP 6): **shapely.polygonize()로 사이클 탐지**, Angular sweep DFS는 fallback
- **R12** (STEP 7): 바운딩박스 면적 0.5-2% 범위 기준 면적 필터 (adaptive)
- **R13** (STEP 8): shapely unary_union으로 외곽선 추출 (.exterior)
- **R14** (STEP 9): simplify(), make_valid()로 보정, HATCH IoU 검증

### Non-Functional Requirements

- **R15**: 동기 처리로 업로드 후 즉시 결과 반환, adaptive timeout (base 30s + entity_count/1000 * 10s, 최대 60s)
- **R16**: 100MB 이하 DXF 파일 지원, 10MB 이하 동기 처리, 초과 시 비동기 job queue fallback
- **R17**: CORS 설정으로 프론트엔드 통합, 환경 변수로 tolerance/area filter 파라미터 외부화
- **R18**: 에러 발생 시 명확한 메시지 반환, 각 STEP에서 예외 catch하고 상세 스택 트레이스 로깅
- **R19**: 좌표계 자동 감지 (DWG $INSUNITS), 밀리미터로 정규화하여 tolerance 계산
- **R20**: Planarity validation, non-planar 시 fallback 알고리즘 또는 경고
- **R21**: AI 판단 개입 지점 3곳 - 그래프 이상 감지, 면적 필터 기준 판단, 최종 결과 sanity check, `backend/core/ai_judge.py` 모듈

### Scope Boundaries

- **Included**: 단일 건물 전제, 2D 도면만, **LINE/**LWPOLYLINE/POLYLINE/SPLINE** 엔티티 (ARC는 LINE 근사 변환)
- **Excluded**: 3D 엔티티, **CIRCLE** (건물 외벽이 원형인 경우는 드물고 기둥/심볼 CIRCLE이 노이즈 원인이 됨), 복합 건물 단지, TEXT/MTEXT/DIMENSION 처리
- **Excluded**: 뷰어 내에서의 외곽선 편집 기능 (읽기 전용)

## Context & Research

### Relevant Code and Patterns

- **Current Frontend**: `src/components/DxfViewer.jsx` - dxf-viewer 라이브러리로 렌더링
- **Current Uploader**: `src/components/DxfUploader.jsx` - 파일 업로드 핸들링
- **No Backend**: 백엔드가 없으므로 새로 구축 필요

### Institutional Learnings

N/A - 새로운 기능

### External References

- **ezdxf 1.4.3**: DXF 파싱, 블록 전개, 엔티티 필터링 (https://ezdxf.readthedocs.io/)
- **shapely 2.0+**: Noding, unary_union, Polygon, make_valid (https://shapely.readthedocs.io/)
- **scipy.spatial.KDTree**: tolerance 기반 근접점 탐색
- **networkx**: 무방향 그래프, degree 연산, DFS
- **FastAPI**: 파일 업로드, CORS, async 지원 (https://fastapi.tiangolo.com/)

### Research Summary

**DXF Processing**: ezdxf가 현재 표준, 재귀적 블록 전개 지원 robust
**Geometric Processing**: shapely unary_union이 교차점 자동 분할 (Noding), shapely 2.0+는 make_valid()로 유효성 수복
**Graph Algorithms**: Angular sweep DFS가 planar graph의 minimal cycle 탐지에 최적
**Web Framework**: FastAPI가 Flask보다 async, validation, performance에서 우월
**Integration**: 프로젝트 내부 backend/ 폴더 구조, 동기 처리 (업로드 후 즉시 반환)

## Key Technical Decisions

### Architecture Decisions

- **Backend Framework**: FastAPI 선택 - async 지원, Pydantic validation, 자동 OpenAPI 문서
- **Processing Mode**: 동기 처리 - 단일 건물 전제하면 처리 시간 30초 이내로 충분
- **Project Structure**: 프로젝트 내부 backend/ 폴더 - 단일 모노레포, 배포 간편
- **Overlay Method**: dxf-viewer Scene에 직접 Three.js Line 추가 - 레이어 토글 용이

### Algorithm Design Decisions

- **Adaptive Tolerance Calculation**: 전역 스케일 (바운딩박스 대각선의 0.1%) + 지역 스케일 (평균 세그먼트 길이의 1%) 중 최소값, [0.001mm, 1.0mm]로 클램핑. DWG 단위 자동 감지 후 밀리미터로 정규화.
  - **Rationale**: 고정 0.1%는 scale-dependent (1000m 건물 → 1.41m tolerance), unit-sensitive (mm vs m vs inches), aspect-ratio blind (100m×10m 건물 과도한 tolerance). Adaptive 방식은 다양한 도면 단위/스케일/형태에 견고.
  - **Tradeoff**: 더 복잡하지만 DWG $INSUNITS 기반 단위 변환으로 단위 무관성 확보. 클램핑으로 floating-point underflow/overflow 방지.

- **Adaptive Area Filter**: 바운딩박스 면적의 0.5-2% 범위, 엔티티 수와 기하학적 복잡도 (ARC 밀도)에 따라 동적 조절.
  - **Rationale**: 고정 1%는 L자/U자 건물에서 작은 wing 제거 위험. 엔티티 수가 많을수록 복잡하므로 임계값 높임. ARC가 많으면 곡선 요소가 많아 더 보수적 필터 필요.
  - **Tradeoff**: 복잡도 계산 추가하지만 단일 건물 전제하면 여전히 빠르고 L/U자 건물 안전.

- **Processing Mode with Fallback**: 동기 처리 기본 (단일 건물, <10MB, <10K entities), 파일 크기/복잡도 초과 시 비동기 job queue로 fallback.
  - **Rationale**: 30초 가정은 100MB 파일에 비현실적. 파싱(10-20초) + 그래프(O(n²)) + 사이클 탐지로 30초 초과 가능. Adaptive timeout: `base_timeout + (entity_count / 1000) * additional_timeout`.
  - **Tradeoff**: 비동기 분기 추가로 복잡하지만 대용량 파일에서 타임아웃 방지. Job ID polling으로 UX 유지.

- **Noding Strategy**: shapely unary_union 사용 - T자/X자 교차점 자동 분할, floating-point 정밀도로 tolerance * 1.0001 사용.
  - **Rationale**: shapely가 robust하지만 `query_pairs(tolerance)`의 strict equality가 floating-point 오류 누락 가능. 1.0001 배수로 안전마진 추가.

- **Pruning Strategy**: Dangling edge 반복 제거 + 안전장치 (최대 반복 횟수, 최소 그래프 크기, 제거된 엣지 % 로깅).
  - **Rationale**: degree=1 제거가 노이즈 제거에 효과적이지만 legitimate exterior wall extension 제거 가능. 최대 반복 제한으로 무한 루프 방지. 로깅으로 과다 pruning 감지.

- **Validation Method**: make_valid() (shapely 2.0+) + fallback 전략.
  - **Rationale**: make_valid()이 topology 변경 가능하므로 결과 검증 필요. 실패 시 geometry simplification → buffer(0) → 복원 polygon 선택 fallback.

- **Configuration Management**: 외부화된 설정 (tolerance method, area filter 범위, pruning limits).
  - **Rationale**: 하드코딩된 파라미터를 환경 변수로 외부화하여 실행 시 튜닝 가능. A/B 테스트로 최적값 찾기.

### Technology Stack

| 역할 | 라이브러리 | 버전 | 용도 |
|------|----------|------|------|
| DXF 파싱 | ezdxf | ≥1.4.3 | 파일 읽기, 블록 Explode, 엔티티 순회, $INSUNITS 감지 |
| 기하 연산 | shapely | ≥2.0 | Noding, Polygon 생성, unary_union, make_valid, simplify |
| 공간 탐색 | scipy.spatial.KDTree | ≥1.10 | tolerance 스냅용 근접 끝점 탐색 |
| 그래프 처리 | networkx | ≥3.0 | 무방향 그래프 구성, degree 연산, DFS |
| 수치 연산 | numpy | ≥1.21 | 좌표 배열 연산, 각도 계산 |
| 웹 프레임워크 | FastAPI | ≥0.100 | 파일 업로드 API, CORS, async I/O |
| ASGI 서버 | Uvicorn | ≥0.20 | FastAPI 개발/프로덕션 서버 |
| 데이터 검증 | Pydantic | ≥2.0 | 요청/응답 모델, validation |

## Open Questions

### Resolved During Planning

- **Q1**: 길이 필터를 쓸 것인가? → **A**: Dangling Pruning으로 대체, 더 자연스러운 노이즈 제거
- **Q2**: 교차점 분할 (Noding) 방법은? → **A**: shapely unary_union 사용, 자동 분할
- **Q3**: shapely 버전은? → **A**: 2.0+ 사용, make_valid() 함수로 유효성 수복
- **Q4**: 백엔드 구조는? → **A**: 프로젝트 내부 backend/ 폴더, 단일 모노레포
- **Q5**: Tolerance 계산 방식은? → **A**: Adaptive (global 0.1% + local 1% of avg segment), DWG units 감지, mm으로 정규화
- **Q6**: Area filter 기준은? → **A**: Adaptive 0.5-2% 범위, 엔티티 수와 ARC 밀도로 동적 조절
- **Q7**: Synchronous 처리 가능 여부? → **A**: <10MB files 가능, 초과 시 비동기 fallback
- **Q8**: Floating-point 안전장치는? → **A**: tolerance * 1.0001, epsilon comparisons, shapely buffer() fallback

### Deferred to Implementation

- **Q9**: 정확한 tolerance 계수 (0.1% vs 0.05%) - 실행 시 튜닝, 환경 변수로 외부화
- **Q10**: 최대 파일 크기 제한 (100MB vs 200MB) - 실제 파일로 테스트 후 결정, 메모리 프로파일링 기반
- **Q11**: Non-planar graph fallback 알고리즘 - planarity validation 실패 시 대안 연구 필요
- **Q12**: Overlay 색상/스타일 - UX 논의 후 결정, 환경 변수로 설정 가능하게
- **Q13**: Pruning 최대 반복 횟수 - 실제 그래프에서 테스트 후 결정 (제안: 1000 또는 엣지 수)
- **Q14**: **CIRCLE 수집 여부** - 건물 외벽이 원형인 경우는 드물고 기둥/심볼 CIRCLE이 노이즈 원인이 됨. 현재는 **CIRCLE 제외**로 진행, 외곽선에 구멍이 생기면 별도 강력 필터 검토.

## AI Intervention Points

> *신뢰도 지표가 임계값 이하일 때만 AI 판단 개입. 비용 최소화를 위해 정상 범위에서는 기존 알고리즘만 실행.*

**개요:** 9단계 파이프라인의 중요 결정점에서 AI가 판단자로 개입하여 결과 품질을 보장합니다.

**원칙:**
- 정상 범위: 기존 알고리즘만 실행 (AI 호출 없음)
- 비정상 감지: 신뢰도 지표가 임계값 벗어날 때만 Claude API 호출
- 최소화: 각 지점에서 구체적인 질문과 함축된 정보만 전송
- 비용 절감: API 호출 횟수를 최소화하여 운영 비용 억제

### Point 1: STEP 5 이후 — 그래프 이상 감지

**목적:** Pruning 후 그래프가 비정상적일 때 AI 판단

**트리거 조건 (모두 충족 시 AI 호출):**
- connected components가 3개 이상 (분리된 그래프가 너무 많음)
- 최대 node degree가 6 이상 (비정상적 교차점 존재)
- Pruning으로 50% 이상 엣지 제거됨 (과다 pruning 의심)

**AI에게 전달할 정보:**
```json
{
  "component_count": 3,
  "max_degree": 8,
  "pruned_pct": 65.2,
  "node_count": 1250,
  "edge_count": 890,
  "original_entities": 4500
}
```

**AI에게 물어볼 것:**
- 어느 connected component가 외곽선 후보인지?
- 그래프가 정상 건물 구조인지 이상 징후가 있는지?
- Pruning이 과도하게 된 건지? 덜 pruning해야 할지?

### Point 2: STEP 7 — 면적 필터 기준 판단

**목적:** 폴리곤 면적 분포가 명확하지 않을 때 AI 판단

**트리거 조건:**
- sorted_areas[0] / sorted_areas[1] < 5 (1위와 2위 면적 차이가 5배 미만)
- 즉, 가장 큰 폴리곤들이 서로 비슷한 크기로 구별될 때

**AI에게 전달할 정보:**
```json
{
  "polygons": [
    {"id": 0, "area": 125.4, "vertex_count": 8, "bbox_ratio": 1.2},
    {"id": 1, "area": 118.7, "vertex_count": 12, "bbox_ratio": 1.5},
    {"id": 2, "area": 95.3, "vertex_count": 6, "bbox_ratio": 0.8},
    ...
  ],
  "bbox_area": 15000,
  "total_area": 4500
}
```

**AI에게 물어볼 것:**
- 어떤 폴리곤들이 외곽선 후보이고 어떤 게 노이즈인지?
- 면적 필터 기준을 얼마나 조정해야 할지 (현재 0.5-2% 범위)?
- L자/U자 건물에서 작은 wing이 필터링되지 않도록 기준 조정 필요?

### Point 3: STEP 9 — 최종 결과 sanity check

**목적:** 최종 결과가 타당한지 검증

**트리거 조건 (하나라도 충족 시 AI 호출):**
- 결과 폴리곤 면적 / 바운딩박스 면적 < 0.1 (너무 작음)
- 결과 꼭짓점 수 > 500 (너무 복잡함)
- HATCH IoU < 0.5 (HATCH와 일치하지 않음)

**AI에게 전달할 정보:**
```json
{
  "vertex_count": 850,
  "area_ratio": 0.08,  // 결과 면적 / 바운딩박스 면적
  "convex_hull_ratio": 0.75,
  "min_angle": 15.5,
  "interior_count": 2,
  "hatch_iou": 0.35,
  "processing_time_ms": 8500
}
```

**AI에게 물어볼 것:**
- 결과가 정상 건물 외곽선으로 타당한지?
- 어느 단계로 rollback해야 할지? (예: 면적 필터 완화, pruning 재조정)
- 외곽선에 구멍/돌출이 있으면 어느 단계가 문제인지?

### 구현 개요

**파일 구조:**
```
backend/core/
└── ai_judge.py          # AI 판단 인터페이스
```

**함수 시그니처:**
```python
async def should_invoke_ai(graph_metrics: Dict) -> bool:
    # 트리거 조건 확인
    pass

async def get_ai_judgment(point: str, data: Dict) -> Dict:
    # Claude API 호출
    # 응답: { "decision": "keep"/"adjust"/"retry", "reason": "...", "params": {...} }
    pass
```

**API 통합:**
- Anthropic Claude API 사용 (Messages API)
- Environment variable: `ANTHROPIC_API_KEY`
- Timeout: 10초
- Fallback: API 실패 시 보수적 기본값 사용

**비용 최적화:**
- 각 Point별 평균 호출 횟수: < 1회/파일 (비정상 시만 호출)
- Token 사용량: 약 500-1000 tokens/호출
- 월간 추정 비용: $0.001-0.002/파일 (1만 파일 처리 시 $10-20)

## High-Level Technical Design

> *This illustrates the intended 9-step pipeline architecture and is directional guidance for review, not implementation specification. The implementing agent should treat it as context, not code to reproduce.*

```mermaid
graph TD
    subgraph Frontend
        A[DXF File Upload] --> B[DxfUploader.jsx]
        B --> C[POST /api/detect-boundary]
        C --> D[DxfViewer.jsx]
    end

    subgraph Backend API
        C --> E[FastAPI Endpoint]
        E --> F[File Validation]
    end

    subgraph Algorithm Pipeline
        F --> G[STEP 1: Parse & Explode<br/>ezdxf 재귀적 블록 전개]
        G --> H[STEP 2: Segment Normalization<br/>ARC → LINE 근사 변환]
        H --> I[STEP 3: Tolerance Snapping<br/>KD-tree + Union-Find로 끝점 병합]
        I --> J[STEP 4: Noding<br/>shapely unary_union 교차점 분할]
        J --> K[STEP 5: Pruning<br/>degree=1 노드 반복 제거]
        K --> L[STEP 6: Cycle Detection<br/>shapely.polygonize() (primary)]
        L --> M[STEP 7: Area Filter<br/>바운딩박스 면적 0.5-2% 기준]
        M --> N[STEP 8: Union & Extract<br/>unary_union → .exterior]
        N --> O[STEP 9: Validation<br/>simplify, make_valid, HATCH IoU]
    end

    subgraph Response
        O --> P[JSON Response<br/>boundary_coords, metadata]
        P --> D
        D --> Q[Add Three.js Lines to Scene]
    end

    style G fill:#e1f5e1
    style H fill:#e1f5e1
    style I fill:#e1f5e1
    style J fill:#e1f5e1
    style K fill:#ffe1e1
    style L fill:#e1f5e1
    style M fill:#e1f5ff
    style N fill:#e1f5ff
    style O fill:#ffe1ff
```

### Data Flow Sketch

```
Request Flow:
POST /api/detect-boundary
Content-Type: multipart/form-data
Body: { file: <dxf_file> }

Response Flow:
{
  "success": true,
  "boundary": {
    "exterior": [[x1, y1], [x2, y2], ...],  // 외곽선 좌표
    "interiors": [                           // 중정 (있으면)
      [[x1, y1], [x2, y2], ...],
      ...
    ]
  },
  "metadata": {
    "area": 1234.56,                          // 외곽선 면적
    "bbox_area": 123456.78,                   // 바운딩박스 면적
    "confidence": 0.95,                       // HATCH IoU 신뢰도
    "cycles_detected": 15,                    // 탐지된 사이클 수
    "processing_time_ms": 2345
  }
}
```

### Key Algorithm Modules

```
backend/
├── api/
│   └── detect_boundary.py     # FastAPI endpoint
├── core/
│   ├── parser.py              # STEP 1: ezdxf 파싱, 블록 Explode
│   ├── segment.py             # STEP 2: 세그먼트 정규화, ARC → LINE 근사
│   ├── snapping.py            # STEP 3: KD-tree + Union-Find tolerance 스냅
│   ├── noding.py              # STEP 4: shapely unary_union Noding
│   ├── graph.py               # STEP 5: networkx 그래프 구성, Pruning
│   ├── cycles.py              # STEP 6: shapely.polygonize() 사이클 탐지
│   ├── filter.py              # STEP 7: 면적 필터 (adaptive)
│   ├── union.py               # STEP 8: unary_union, exterior extraction
│   ├── validate.py            # STEP 9: simplify, make_valid, HATCH IoU
│   └── ai_judge.py            # AI 판단 개입 지점 (신규)
└── models/
    └── schemas.py             # Pydantic models
```

## Implementation Units

### Phase 1: Backend API Foundation

- [ ] **Unit 1: Backend project setup**

**Goal:** 백엔드 개발 환경 구축

**Requirements:** R15, R17

**Dependencies:** None

**Files:**
- Create: `backend/requirements.txt`
- Create: `backend/main.py`
- Create: `backend/.gitignore`

**Approach:**
- FastAPI + Uvicorn 개발 서버 구성
- ezdxf, shapely, scipy, networkx, numpy 의존성 정의
- CORS middleware 설정 (프론트엔드 localhost:5173 허용)
- Pydantic BaseModel으로 요청/응답 모델 정의

**Patterns to follow:**
- FastAPI 공식 문서의 파일 업로드 패턴
- 프로젝트 최상단 `.gitignore` 사용하여 `.venv/`, `__pycache__/` 제외

**Test scenarios:**
- Happy path: `pip install -r requirements.txt` 성공
- Edge case: 의존성 충돌 없음 확인
- Error path: 잘못된 패키지명 설치 실패

**Verification:**
- `uvicorn main:app --reload`로 개발 서버 시작 성공
- `http://localhost:8000/docs`로 Swagger UI 접근 가능
- CORS preflight 요청 성공 (OPTIONS 메서드)

- [ ] **Unit 2: File upload endpoint with validation**

**Goal:** DXF 파일 업로드 API 구현

**Requirements:** R1, R16, R18

**Dependencies:** Unit 1

**Files:**
- Create: `backend/api/detect_boundary.py`
- Create: `backend/models/schemas.py`
- Test: `backend/tests/test_api/test_upload.py`

**Approach:**
- `POST /api/detect-boundary` 엔드포인트 구현
- `UploadFile`로 multipart 파일 수신, `.dxf` 확장자 검증
- 파일 크기 100MB 제한 (`httpx`로 청크 확인)
- `ezdxf.DXFStructureError` catch하여 400 에러 반환
- 임시 파일 처리 후 `os.unlink()`로 cleanup (try-finally)

**Patterns to follow:**
- FastAPI 공식 문서의 파일 업로드 예제
- ezdxf 공식 문서의 `ezdxf.readfile()` 에러 핸들링

**Test scenarios:**
- Happy path: 정상 DXF 파일 업로드 → 200 OK
- Edge case: 0바이트 파일, 100MB 정확히 파일
- Error path: DXF 아닌 파일 → 400 Bad Request, 손상된 DXF → 400 상세 메시지
- Integration: 업로드 후 임시 파일 cleanup 확인

**Verification:**
- Swagger UI에서 파일 업로드 테스트 성공
- 잘못된 파일 업로드 시 명확한 에러 메시지
- 업로드된 파일이 파일 시스템에 남지 않음

### Phase 2: Core Algorithm Implementation

- [ ] **Unit 3: DXF parser and segment normalizer**

**Goal:** STEP 1-2 구현 - ezdxf 파싱, 블록 전개, 세그먼트 정규화

**Requirements:** R6, R7

**Dependencies:** Unit 2

**Files:**
- Create: `backend/core/parser.py`
- Create: `backend/core/models.py`
- Test: `backend/tests/test_parser.py`

**Approach:**
- `ezdxf.readfile()`로 DXF 로드, `modelspace()`로 엔티티 순회
- 재귀 함수로 INSERT 전개: `process_insert(doc, block_name, visited)` (순환 참조 방지)
- `EntityQuery`로 LINE, ARC, LWPOLYLINE, POLYLINE, SPLINE, CIRCLE 필터링
- SPLINE → `spline.discretize(50)`으로 LWPOLYLINE 변환
- HATCH 엔티티 별도 리스트에 보관 (STEP 9 검증용)
- 세그먼트 정규화: `{start: Point, end: Point, meta: Dict}` 리스트 생성
  - LINE: `{type: 'line'}`
  - ARC: `{type: 'arc', center, radius, start_angle, end_angle}`
  - LWPOLYLINE: bulge 있는 구간 → arc 메타, 나머지 line
  - CIRCLE: 즉시 Polygon으로 변환하여 별도 반환

**Patterns to follow:**
- ezdxf 공식 문서의 VirtualExplorer 패턴
- 재귀 깊이 제한 (sys.getrecursionlimit() 확인)

**Test scenarios:**
- Happy path: 단순 LINE만 있는 DXF → 세그먼트 리스트
- Edge case: 중첩 INSERT (3-depth), SPLINE 포함 파일
- Error path: 순환 참조 있는 블록 → visited로 무한 루프 방지, 재귀 깊이 초과 → 로깅 후 종료
- Edge case: 3D 엔티티 (3DLINE, 3DPOLYLINE) → 무시 또는 경고, WCS/UCS 혼합 → coordinate system validation
- Edge case: 빈 modelspace, 정의되지 않은 블록 → graceful handling
- Integration: HATCH가 별도 리스트에 보관됨

**Verification:**
- 중첩 블록이 전부 전개되어 평면 엔티티 리스트로 변환
- SPLINE이 LWPOLYLINE 근사로 변환
- CIRCLE이 Polygon으로 별도 반환

- [ ] **Unit 4: Noding with shapely**

**Goal:** STEP 3 구현 - 교차점 분할

**Requirements:** R8

**Dependencies:** Unit 3

**Files:**
- Create: `backend/core/noding.py`
- Test: `backend/tests/test_noding.py`

**Approach:**
- 세그먼트 리스트를 shapely `LineString` 객체 리스트로 변환
- `MultiLineString(segment_lines)`으로 묶기
- `unary_union(multiline)` 적용 - 교차점에서 자동 분할
- 결과 `noded.geoms`를 다시 세그먼트 포맷으로 변환
- ARC 메타는 원본 세그먼트에서 보관 (분할 후 엣지마다 복사)

**Patterns to follow:**
- shapely 공식 문서의 unary_union 예제
- 분할 후 세그먼트 ID 매칭 (원본 인덱스 추적)

**Test scenarios:**
- Happy path: X자 교차, T자 접합이 분할됨
- Edge case: 삼중 교차점, 겹치는 선분
- Error path: degenerate line (길이 0) 제거

**Verification:**
- 교차점이 새로운 노드로 생성됨
- 분할된 세그먼트들이 원본 메타 보관

- [ ] **Unit 5: Tolerance snapping and graph construction**

**Goal:** STEP 4 구현 - KD-tree, Union-Find, networkx 그래프

**Requirements:** R9

**Dependencies:** Unit 4

**Files:**
- Create: `backend/core/graph.py`
- Test: `backend/tests/test_graph.py`

**Approach:**
- 바운딩박스 계산: 모든 좌표의 min/max로 `[minX, minY, maxX, maxY]`
- `tolerance = diagonal * 0.001` (대각선의 0.1%)
- 끝점들을 numpy 배열로 구성 (N x 2)
- `scipy.spatial.KDTree(endpoints)`로 KD-tree 구성
- `query_pairs(tolerance)`로 tolerance 이내 끝점 쌍 탐색
- Union-Find로 병합: 각 쌍을 `union(id1, id2)`
- 각 그룹의 centroid 계산 → 대표 노드
- 세그먼트 끝점을 대표 노드 ID로 교체
- `networkx.Graph()`에 노드(위치)와 엣지(세그먼트 메타 포함) 추가

**Patterns to follow:**
- scipy KDTree 공식 문서의 query_pairs 패턴
- Union-Find는 `scipy.optimize.linear_sum_assignment`의 disjoint set 참조

**Test scenarios:**
- Happy path: 0.1단위 차이 끝점들이 병합됨
- Edge case: tolerance 경계에 있는 끝점, 고립된 끝점
- Edge case: DXF files in different units (feet, meters, inches) → tolerance scaling 확인
- Edge case: Floating-point precision issues (near-zero distances) → tolerance * 1.0001로 처리
- Error path: 빈 세그먼트 리스트, 단일 엣지만 있는 그래프

**Verification:**
- tolerance 이내 끝점들이 동일 노드로 병합
- 그래프가 planar (엣지 교차 없음), non-planar 시 경고
- networkx graph에 노드/엣지가 올바르게 추가됨
- KD-tree query_pairs가 tolerance * 1.0001로 floating-point 안전마진 포함

- [ ] **Unit 6: Dangling edge pruning**

**Goal:** STEP 5 구현 - degree=1 노드 반복 제거

**Requirements:** R10

**Dependencies:** Unit 5

**Files:**
- Modify: `backend/core/graph.py`
- Test: `backend/tests/test_graph.py` (extend)

**Approach:**
- `while True:` 루프
- `dangling = [n for n in G.nodes if G.degree(n) == 1]`
- `if not dangling: break`
- `G.remove_nodes_from(dangling)`
- 안전장치:
  - 최대 반복 횟수 제한: `max_iterations = 1000` (또는 엣지 수)
  - 최소 그래프 크기 확인: `if G.number_of_nodes() < 3: break` (충분한 루프 남기 위함)
  - 제거된 엣지 % 로깅: `removed_pct = removed_edges / total_edges * 100`
  - `if removed_pct > 50: warning("Aggressive pruning - may have removed legitimate geometry")`

**Patterns to follow:**
- networkx의 `Graph.degree()`, `remove_nodes_from()` 패턴
- 최대 반복 횟수 제한 (안전장치)

**Test scenarios:**
- Happy path: 막다른 선분이 제거됨
- Edge case: 여러 개의 dangling이 연쇄 제거
- Error path: 빈 그래프

**Verification:**
- 남은 그래프의 모든 노드가 degree ≥ 2
- 고립된 짧은 엣지가 자연스럽게 제거됨
- 닫힌 루프만 남음

- [ ] **Unit 7: Minimal cycle detection**

**Goal:** STEP 6 구현 - Angular sweep DFS

**Requirements:** R11

**Dependencies:** Unit 6

**Files:**
- Create: `backend/core/cycles.py`
- Test: `backend/tests/test_cycles.py`

**Approach:**
- 각 노드에서 연결된 엣지를 각도 순으로 정렬
  - `get_angle(center, point) = atan2(dy, dx)`
  - `sorted(neighbors, key=lambda n: angle(n))`
- DFS에서 다음 엣지 선택: 현재 방향 기준 가장 오른쪽 (시계방향 최소 회전)
- `visited_edges` set으로 중복 방지
- 발견된 사이클을 리스트에 저장
- ARC 메타가 있는 엣지: 폴리곤 변환 시 호 보간
  - `arc_points = interpolate_arc(center, radius, start_angle, end_angle, num_points=20)`

**Patterns to follow:**
- Planar graph face traversal 논문의 right-hand rule 패턴
- 각도 계산은 numpy 사용하여 벡터화

**Test scenarios:**
- Happy path: 단일 사각형, 중첩 사각형 (안+밖)
- Edge case: ARC가 포함된 사이클, 복잡한 다각형
- Edge case: T-junctions (세 선분이 한 점에서 만남), overlapping polylines
- Edge case: Multi-edges between same nodes (parallel edges) → merging 또는 special handling
- Error path: 비-planar 그래프 (tolerance snapping으로 인한 edge crossing) → planarity validation 실패 시 fallback
- Error path: 빈 그래프 (pruning 후), 단일 노드만 있는 그래프

**Verification:**
- Minimal cycle만 탐지됨 (큰 사이클 안에 작은 사이클 포함 안 함)
- ARC가 올바르게 보간되어 꼭짓점에 추가됨
- 각 엣지가 정확히 하나의 face에 속함
- Polygon winding direction 표준화 (CCW for exterior, CW for interiors)

- [ ] **Unit 8: Area filter and polygon conversion**

**Goal:** STEP 7 구현 - 면적 필터

**Requirements:** R12

**Dependencies:** Unit 7

**Files:**
- Create: `backend/core/filter.py`
- Test: `backend/tests/test_filter.py`

**Approach:**
- 사이클 좌표를 shapely `Polygon`으로 변환
- 바운딩박스 면적 계산: `(maxX - minX) * (maxY - minY)`
- `polygon.area < bbox_area * 0.01`면 제거
- `polygon.is_valid == False`면 제거 (자기교차, 꼭짓점 < 3)
- 유효한 폴리곤만 리스트에 보관

**Patterns to follow:**
- shapely `Polygon.area`, `is_valid` 패턴
- 면적 계산은 shapely가 좌표계를 자동 처리

**Test scenarios:**
- Happy path: 작은 노이즈 폴리곤 제거됨
- Edge case: 면적 기준 경계에 있는 폴리곤
- Error path: 유효하지 않은 폴리곤 (자기교차)

**Verification:**
- 바운딩박스 면적의 1% 미만 폴리곤 제거됨
- 유효하지 않은 폴리곤 제거됨
- 건물 본체 폴리곤만 남음

- [ ] **Unit 9: Unary union and boundary extraction**

**Goal:** STEP 8 구현 - 외곽선 추출

**Requirements:** R13

**Dependencies:** Unit 8

**Files:**
- Create: `backend/core/union.py`
- Test: `backend/tests/test_union.py`

**Approach:**
- `unary_union(valid_polygons)`로 병합
- 결과가 항상 `Polygon` (단일 건물 전제)
- `merged.exterior.coords`로 외곽선 좌표 추출
- `list(merged.interiors)`로 중정 좌표 추출 (있으면)
- 좌표를 `[[x, y], ...]` 리스트로 변환

**Patterns to follow:**
- shapely `unary_union`, `Polygon.exterior` 패턴
- 단일 건물 전제이므로 MultiPolygon 분기 불필요

**Test scenarios:**
- Happy path: 여러 폴리곤이 하나로 병합됨
- Edge case: ㅁ자 건물 (중정 포함), 인접 건물
- Error path: 빈 폴리곤 리스트

**Verification:**
- 내벽선들이 내부로 흡수되고 외부 경계만 남음
- exterior가 단일 닫힌 루프
- interiors가 있으면 각각 별도 리스트

- [ ] **Unit 10: Validation and HATCH IoU check**

**Goal:** STEP 9 구현 - 보정 및 검증

**Requirements:** R14

**Dependencies:** Unit 9

**Files:**
- Create: `backend/core/validate.py`
- Test: `backend/tests/test_validate.py`

**Approach:**
- `exterior.simplify(tolerance=0.001, preserve_topology=True)`로 컬리니어 병합
- `if not merged.is_valid: merged = make_valid(merged)`로 유효성 수복
- HATCH IoU 계산 (STEP 1에서 보관해둔 HATCH 경계선):
  - HATCH 경계를 Polygon으로 변환
  - `exterior.intersection(hatch_polygon).area / exterior.union(hatch_polygon).area`
  - IoU < 0.5면 경고 로그
- 메타데이터 구성: `{area, bbox_area, confidence, cycles_detected, processing_time_ms}`

**Patterns to follow:**
- shapely `simplify`, `make_valid` 패턴
- IoU 계산은 `geometries.intersection().area / geometries.union().area`

**Test scenarios:**
- Happy path: 정상 폴리곤이 simplify됨
- Edge case: 자기교차 있는 폴리곤이 make_valid로 수복됨
- Error path: HATCH IoU가 0 (HATCH 없음)

**Verification:**
- Exterior가 단순화됨 (불필요한 꼭짓점 제거)
- 유효하지 않은 폴리곤이 수복됨
- HATCH IoU가 낮으면 경고 로그

### Phase 3: Response Model Integration

- [ ] **Unit 11: Response model and endpoint integration**

**Goal:** 파이프라인 통합 및 JSON 응답

**Requirements:** R3, R5

**Dependencies:** Unit 10

**Files:**
- Modify: `backend/models/schemas.py`
- Modify: `backend/api/detect_boundary.py`
- Test: `backend/tests/test_api/test_integration.py`

**Approach:**
- Pydantic 모델 정의:
  ```python
  class BoundaryResponse(BaseModel):
      success: bool
      boundary: Optional[BoundaryData]
      error: Optional[str]
      metadata: Optional[Metadata]

  class BoundaryData(BaseModel):
      exterior: List[List[float]]
      interiors: List[List[List[float]]]

  class Metadata(BaseModel):
      area: float
      bbox_area: float
      confidence: float
      cycles_detected: int
      processing_time_ms: int
  ```
- 엔드포인트에서 파이프라인 호출:
  ```python
  @app.post("/api/detect-boundary")
  async def detect_boundary(file: UploadFile = File(...)):
      start_time = time.time()
      # ... pipeline ...
      processing_time = (time.time() - start_time) * 1000
      return BoundaryResponse(
          success=True,
          boundary=BoundaryData(exterior=..., interiors=...),
          metadata=Metadata(...)
      )
  ```
- 에러 핸들링: 각 STEP에서 예외 발생 시 상세 메시지 반환

**Patterns to follow:**
- Pydantic BaseModel의 자동 검증
- FastAPI의 자동 JSON 직렬화

**Test scenarios:**
- Happy path: 정상 DXF → boundary 포함 응답
- Edge case: 바운딩박스 면적 0인 파일 (빈 도면)
- Error path: 각 STEP에서의 에러가 상세 메시지로 전달

**Verification:**
- Swagger UI에서 응답 스키마 확인
- processing_time_ms가 실제 처리 시간과 근접
- exterior가 닫힌 루프 (첫 좌표 == 마지막 좌표)

### Phase 4: Frontend Integration

- [ ] **Unit 12: Backend API call from frontend**

**Goal:** 프론트엔드에서 백엔드 API 호출

**Requirements:** R1, R3

**Dependencies:** Unit 11

**Files:**
- Create: `src/api/detectionApi.js`
- Modify: `src/components/DxfViewer.jsx`
- Test: `src/tests/api.test.js` (if Vitest configured)

**Approach:**
- `detectionApi.js`에 API 호출 함수:
  ```javascript
  export async function detectBoundary(file) {
      const formData = new FormData()
      formData.append('file', file)
      const response = await fetch('http://localhost:8000/api/detect-boundary', {
          method: 'POST',
          body: formData
      })
      if (!response.ok) throw new Error(...)
      return response.json()
  }
  ```
- `DxfViewer.jsx`에서 loaded 이벤트 후 API 호출
  - Blob URL이 아닌 원본 File 객체 사용
  - 로딩 상태 표시 (스피너)
  - 에러 핸들링 (토스트 메시지)

**Patterns to follow:**
- Fetch API의 FormData 패턴
- React useEffect에서 async 함수 호출 패턴

**Test scenarios:**
- Happy path: 정상 파일 업로드 → boundary 데이터 수신
- Edge case: 큰 파일 (50MB), 빈 도면
- Error path: 서버 다운, 타임아웃

**Verification:**
- 네트워크 탭에서 POST 요청 확인
- 응답 JSON 구조가 Pydantic 모델과 일치
- 에러 시 사용자에게 명확한 메시지

- [ ] **Unit 13: Three.js overlay rendering**

**Goal:** dxf-viewer Scene에 외곽선 레이어 추가

**Requirements:** R3, R4

**Dependencies:** Unit 12

**Files:**
- Modify: `src/components/DxfViewer.jsx`
- Create: `src/utils/overlayRenderer.js`
- Test: `src/tests/overlay.test.js`

**Approach:**
- `overlayRenderer.js`에 Three.js Line 생성 함수:
  ```javascript
  export function createBoundaryLines(exterior, interiors, color = 0xff0000) {
      const material = new THREE.LineBasicMaterial({ color, linewidth: 2 })
      const exteriorLine = createLineFromCoords(exterior, material)
      const interiorLines = interiors.map(interior =>
          createLineFromCoords(interior, material)
      )
      return { exteriorLine, interiorLines }
  }

  function createLineFromCoords(coords, material) {
      const points = coords.map(([x, y]) => new THREE.Vector3(x, y, 0))
      const geometry = new THREE.BufferGeometry().setFromPoints(points)
      return new THREE.Line(geometry, material)
  }
  ```
- `DxfViewer.jsx`에서:
  - `viewerRef.current.GetScene()`으로 Three.js scene 접근
  - exterior, interior Line 객체 추가
  - `viewerRef.current.Render()`로 재렌더링
- 토글 버튼으로 레이어 표시/숨김:
  ```javascript
  const toggleOverlay = () => {
      exteriorLine.visible = !exteriorLine.visible
      interiorLines.forEach(line => line.visible = !line.visible)
      viewerRef.current.Render()
  }
  ```

**Patterns to follow:**
- Three.js Scene, Line, BufferGeometry 패턴
- dxf-viewer의 GetScene(), Render() 패턴

**Test scenarios:**
- Happy path: exterior가 빨간색 선으로 오버레이됨
- Edge case: 중정이 있으면 interior도 별도로 렌더링
- Error path: 좌표가 비어있거나 잘못됨

**Verification:**
- dxf-viewer 캔버스 위에 외곽선이 겹쳐서 보임
- 토글 버튼으로 외곽선 표시/숨김 가능
- 원본 DXF 도면과 외곽선이 동시에 zoom/pan

- [ ] **Unit 14: UI improvements (metadata display, controls)**

**Goal:** 메타데이터 표시 및 UI 개선

**Requirements:** R4, R5

**Dependencies:** Unit 13

**Files:**
- Modify: `src/components/DxfViewer.jsx`
- Modify: `src/components/DxfViewer.css`

**Approach:**
- 메타데이터 표시:
  ```javascript
  <div className="metadata">
      <p>Area: {metadata.area.toFixed(2)}㎡</p>
      <p>Confidence: {(metadata.confidence * 100).toFixed(1)}%</p>
      <p>Cycles detected: {metadata.cycles_detected}</p>
  </div>
  ```
- 컨트롤 버튼:
  - "Detect Boundary" 버튼 (초기 상태)
  - "Toggle Overlay" 체크박스 (감지 후)
  - "Clear" 버튼 (리셋)
- 로딩 상태: 스피너 또는 "Processing..." 메시지
- 에러 상태: 빨간색 토스트 메시지

**Patterns to follow:**
- React state로 로딩/에러/성공 상태 관리
- CSS className으로 조건부 스타일링

**Test scenarios:**
- Happy path: 메타데이터가 올바르게 표시됨
- Edge case: confidence가 낮을 때 (0.5 미만) 경고 표시
- Error path: API 실패 시 에러 메시지

**Verification:**
- 면적, 신뢰도, 사이클 수가 정확히 표시됨
- 토글 버튼으로 오버레이 제어 가능
- 로딩/에러 상태가 명확히 구분됨

## System-Wide Impact

- **Interaction graph:** `DxfViewer.jsx` → `detectBoundary()` → FastAPI → Algorithm Pipeline → Three.js Scene updates
- **Error propagation:** 각 STEP에서 예외 발생 시 FastAPI → 400/500 에러 → 프론트엔드에서 사용자 메시지로 변환
- **State lifecycle risks:** 대용량 파일 처리 시 메모리 사용량 급증 가능, 임시 파일 cleanup 필수
- **API surface parity:** 향후 다른 탐지 기능 (inner boundary, room detection) 추가 시 같은 패턴 사용
- **Integration coverage:** 엔드투엔드 테스트: 파일 업로드 → 외곽선 탐지 → 오버레이 렌더링

## Risks & Dependencies

| Risk | Likelihood | Impact | Mitigation |
|------|-----------|--------|------------|
| 대용량 파일 처리 메모리 초과 | Medium | High | 파일 크기 100MB 제한, 스트리밍 처리, 메모리 프로파일링, 각 STEP 후 intermediate 데이터 cleanup |
| 알고리즘 복잡도로 인한 처리 시간 초과 | Medium | High | 30초 타임아웃, 병렬 처리 고려, 사이클 탐지 최적화, adaptive timeout based on entity count |
| Coordinate system 차이로 tolerance/area filter 오동작 | High | High | DWG $INSUNITS 자동 감지, 밀리미터로 정규화, 다양한 단위 테스트 케이스 (mm, m, ft, in) |
| Non-planar graphs from tolerance snapping | Medium | High | Planarity validation after snapping, fallback algorithm (conservative boundary detection) |
| Floating-point precision issues in geometric operations | Medium | Medium | tolerance * 1.0001 safety margin, epsilon values for equality comparisons, shapely buffer(0.0001) for robustness |
| shapely/numpy/scipy 버전 호환성 문제 | Low | High | requirements.txt에 정확한 버전 고정 (ezdxf>=1.4.3, shapely>=2.0, scipy>=1.10), 테스트 환경 동기화 |
| dxf-viewer Scene 접근 방식 변경 | Low | Medium | dxf-viewer 공식 문서 확인, GetScene() API 안정성 확인, 버전 고정 |
| CORS 설정 오류로 프론트엔드 연결 실패 | Low | Medium | 개발 시 localhost 허용, 프로덕션 시 명시적 origin 지정 |
| HATCH IoU 계산의 부정확성 | Medium | Low | 여러 HATCH 엔티티 평균, 낮은 신뢰도 시 경고 로그만 |
| Over-pruning으로 legitimate geometry 제거 | Medium | Medium | Pruning statistics 로깅, 최대 반복 제한, 최소 그래프 크기 확인 |
| make_valid()이 topology를 변경하여 외곽선 왜곡 | Low | Medium | make_valid()前后 geometry 비교, significant 변화 시 fallback, 사용자 경고 |

## Documentation / Operational Notes

### API Documentation

- FastAPI Swagger UI: `http://localhost:8000/docs` (자동 생성)
- 엔드포인트: `POST /api/detect-boundary`
- 요청: `multipart/form-data` with `file` field
- 응답: JSON with `boundary`, `metadata` fields

### Installation Instructions

**Backend:**
```bash
cd backend
python -m venv .venv
source .venv/bin/activate  # Windows: .venv\Scripts\activate
pip install -r requirements.txt
uvicorn main:app --reload
```

**Frontend:**
```bash
npm run dev  # 이미 실행 중
```

### Development Notes

- 백엔드: `http://localhost:8000` (FastAPI dev server)
- 프론트엔드: `http://localhost:5174` (Vite dev server)
- CORS 설정: 개발 시 `allow_origins=["*"]`, 프로덕션 시 명시적 origin 지정

### Monitoring / Debugging

- 로깅: 각 STEP에서 처리 시간, 엔티티 수, 사이클 수 로깅
- 에러 로그: 각 STEP에서 예외 발생 시 상세 스택 트레이스 로깅
- 성능 모니터링: `processing_time_ms` 메타데이터로 전체 파이프라인 시간 추적

## Sources & References

- **Origin document:** 사용자 요청 (9-step algorithm specification)
- Research: ezdxf, shapely, scipy, networkx, FastAPI 공식 문서 (2024-2025)
- Related code: `src/components/DxfViewer.jsx`, `src/components/DxfUploader.jsx`
- Algorithm references:
  - Planar graph face traversal (right-hand rule)
  - Angular sweep DFS for minimal cycles
  - shapely unary_union for noding
  - KD-tree + Union-Find for tolerance snapping
