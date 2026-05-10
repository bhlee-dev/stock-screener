"""
정적 HTML 대시보드 생성기
recommendation_history.json → docs/index.html

보안 원칙:
  - 이 모듈은 API 키, 비밀번호 등 민감 정보를 일절 import하지 않는다.
  - 생성된 HTML에는 주식 시장 공개 데이터(종목코드·가격·점수)만 포함된다.
  - 워크플로우에서 docs/index.html 만 커밋 대상으로 지정한다.
"""
import json
import logging
from datetime import date
from pathlib import Path

logger = logging.getLogger(__name__)

_HISTORY_FILE = Path(__file__).parent / "data" / "recommendation_history.json"
_BACKTEST_FILE = Path(__file__).parent / "data" / "backtest_results.json"
_DOCS_DIR = Path(__file__).parent / "docs"
_MAX_SCORE = 18

_COND_KEYS = [
    ("ma_uptrend",              "MA200",  "200일선 우상향"),
    ("ma_full_alignment",       "MA배열", "강세 배열"),
    ("base_formation",          "베이스", "베이스 형성"),
    ("bollinger_contraction",   "볼린저", "볼린저 수축"),
    ("volume_breakout",         "거래량", "거래량 돌파"),
    ("rsi_not_overbought",      "RSI",   "RSI 과매수 아님"),
    ("eps_growth",              "EPS",   "EPS 성장"),
    ("revenue_growth",          "매출",  "매출 성장"),
    ("operating_profit_growth", "영업익", "영업이익 성장"),
    ("institution_net_buy",     "기관",  "기관 순매수"),
    ("foreign_net_buy",         "외국인", "외국인 순매수"),
]

_COND_SCORES = {
    "ma_uptrend": 2, "ma_full_alignment": 1, "base_formation": 1,
    "bollinger_contraction": 2, "volume_breakout": 3, "rsi_not_overbought": 1,
    "eps_growth": 2, "revenue_growth": 1, "operating_profit_growth": 1,
    "institution_net_buy": 2, "foreign_net_buy": 2,
}


# ── 데이터 수집 ───────────────────────────────────────────────────────────────

def _load_history() -> list:
    if not _HISTORY_FILE.exists():
        return []
    try:
        return json.loads(_HISTORY_FILE.read_text(encoding="utf-8"))
    except Exception as e:
        logger.error(f"추천 이력 로드 실패: {e}")
        return []


def _load_backtest() -> dict | None:
    if not _BACKTEST_FILE.exists():
        return None
    try:
        return json.loads(_BACKTEST_FILE.read_text(encoding="utf-8"))
    except Exception as e:
        logger.error(f"백테스트 결과 로드 실패: {e}")
        return None


def _get_current_prices(tickers: list[str]) -> dict[str, int]:
    """pykrx 현재가 일괄 조회 — portfolio_sim 로직 재사용."""
    from portfolio_sim import _get_current_prices_batch
    return _get_current_prices_batch(tickers)


# ── 계산 ─────────────────────────────────────────────────────────────────────

def _enrich(history: list, prices: dict) -> tuple[list, dict | None]:
    """
    각 주차·종목에 현재가·수익률·누적 수익률을 추가한다.
    반환: (enriched_history, overall_summary)
    """
    cum_inv = 0
    cum_cur = 0
    enriched = []

    for entry in history:
        wk_inv = 0
        wk_cur = 0
        stocks = []

        for s in entry["stocks"]:
            cur = prices.get(s["ticker"], s["buy_price"])
            cur_val = cur * s["shares"]
            ret = (cur - s["buy_price"]) / s["buy_price"] * 100 if s["buy_price"] > 0 else 0.0

            stocks.append({
                "ticker": s["ticker"],
                "name": s["name"],
                "buy_price": s["buy_price"],
                "current_price": cur,
                "return_pct": round(ret, 2),
                "score": s.get("score", 0),
                "conditions": s.get("conditions", {}),
            })
            wk_inv += s["invested"]
            wk_cur += cur_val

        cum_inv += wk_inv
        cum_cur += wk_cur
        wk_ret  = (wk_cur - wk_inv) / wk_inv * 100 if wk_inv > 0 else 0.0
        cum_ret = (cum_cur - cum_inv) / cum_inv * 100 if cum_inv > 0 else 0.0

        enriched.append({
            "week": entry["week"],
            "stocks": stocks,
            "week_return_pct": round(wk_ret, 2),
            "cumulative_return_pct": round(cum_ret, 2),
        })

    if not enriched:
        return [], None

    overall = {
        "total_invested_man": round(cum_inv / 10_000),
        "total_current_man":  round(cum_cur / 10_000),
        "overall_return_pct": round((cum_cur - cum_inv) / cum_inv * 100, 2) if cum_inv > 0 else 0.0,
        "weeks": len(enriched),
    }
    return enriched, overall


def _build_payload(enriched: list, overall: dict | None) -> dict:
    """
    JS에 전달할 데이터 구조.
    포함: 종목코드, 종목명, 가격, 점수, 조건 플래그 — 모두 공개 시장 데이터.
    미포함: API 키, 사용자 자격증명, 개인식별정보.
    """
    return {
        "updated_at":  str(date.today()),
        "latest_week": enriched[-1]["week"] if enriched else "",
        "max_score":   _MAX_SCORE,
        "cond_keys":   [k for k, _, _ in _COND_KEYS],
        "cond_labels": [s for _, s, _ in _COND_KEYS],
        "cond_titles": [t for _, _, t in _COND_KEYS],
        "cond_scores": [_COND_SCORES.get(k, 1) for k, _, _ in _COND_KEYS],
        "history":     enriched,
        "overall":     overall,
        "chart": {
            "labels":     [e["week"] for e in enriched],
            "cumulative": [e["cumulative_return_pct"] for e in enriched],
        },
        "backtest": _load_backtest(),
    }


# ── HTML 렌더링 ───────────────────────────────────────────────────────────────

_HTML = """\
<!DOCTYPE html>
<html lang="ko" data-bs-theme="dark">
<head>
  <meta charset="UTF-8">
  <meta name="viewport" content="width=device-width, initial-scale=1">
  <meta name="build" content="__UPDATED_AT__">
  <title>KRX 주간 종목 스크리너</title>
  <link href="https://cdn.jsdelivr.net/npm/bootstrap@5.3.3/dist/css/bootstrap.min.css" rel="stylesheet">
  <script src="https://cdn.jsdelivr.net/npm/chart.js@4.4.3/dist/chart.umd.min.js"></script>
  <style>
    body{background-color:#0d1117!important;color:#e6edf3}
    .navbar{background-color:#161b22!important;border-bottom:1px solid #30363d}
    .card{background-color:#161b22!important;border-color:#30363d!important}
    .card-header{background-color:#161b22!important;border-bottom-color:#30363d!important}
    .table{--bs-table-color:#e6edf3;--bs-table-border-color:#30363d}
    .table-light{--bs-table-bg:#1c2128;--bs-table-color:#e6edf3;--bs-table-border-color:#30363d}
    .table-hover>tbody>tr:hover>*{--bs-table-hover-bg:rgba(255,255,255,0.04)}
    .accordion-item{background-color:#161b22;border-color:#30363d}
    .accordion-button{background-color:#161b22!important;color:#e6edf3!important;box-shadow:none}
    .accordion-button:not(.collapsed){background-color:#1c2128!important;border-bottom:1px solid #30363d}
    .accordion-button::after{filter:invert(1) brightness(2)}
    .accordion-collapse{border-color:#30363d}
    .badge.bg-secondary{background-color:#30363d!important;color:#8b949e!important}
    .text-muted{color:#8b949e!important}
    .score-wrap{display:flex;align-items:center;gap:6px}
    .score-track{flex:1;height:8px;border-radius:4px;background:#30363d;overflow:hidden;min-width:60px}
    .score-fill-lg{height:100%;border-radius:4px;background:#238636}
    .score-num{font-size:.85rem;font-weight:600;white-space:nowrap}
    .score-max{font-weight:400;color:#8b949e;font-size:.75rem}
    .dots-wrap{display:flex;flex-wrap:wrap;gap:2px}
    .dot{font-size:.8rem;cursor:default;line-height:1.3;user-select:none}
    .dot-ok{color:#3fb950}
    .dot-no{color:#30363d}
    .section-divider{border:0;border-top:1px solid #30363d;margin:1.75rem 0 1.5rem}
    .kpi-label{font-size:.7rem;font-weight:600;letter-spacing:.08em;text-transform:uppercase;color:#8b949e;margin-bottom:.5rem}
    .kpi-value{line-height:1.15}
    .kpi-sub{font-size:.78rem;color:#8b949e;margin-top:.4rem}
    footer{border-top:1px solid #30363d;padding-top:1rem}
    .modal-content{background:#161b22;border:1px solid #30363d}
    .modal-header{border-bottom:1px solid #30363d}
    .modal-footer{border-top:1px solid #30363d}
    .clickable-row{cursor:pointer}
    .detail-score-bar{height:10px;border-radius:5px;background:#30363d;overflow:hidden;flex:1}
    .detail-score-fill{height:100%;border-radius:5px;transition:width .4s}
    .nav-tabs .nav-link.active{color:#e6edf3!important;border-color:#30363d #30363d #161b22!important;background:#161b22!important}
    .nav-tabs .nav-link:hover{color:#e6edf3!important;border-color:transparent}
    .bt-kpi-card{text-align:center;padding:1.5rem 1rem}
    .bt-kpi-label{font-size:.7rem;font-weight:600;letter-spacing:.08em;text-transform:uppercase;color:#8b949e;margin-bottom:.4rem}
    .bt-kpi-value{font-size:1.6rem;font-weight:700;line-height:1.2}
    .bt-kpi-sub{font-size:.75rem;color:#8b949e;margin-top:.3rem}
  </style>
</head>
<body>

<nav class="navbar navbar-dark mb-0">
  <div class="container-fluid px-4">
    <span class="navbar-brand fw-bold">📊 KRX 주간 종목 스크리너</span>
    <span class="text-light small">최종 업데이트: __UPDATED_AT__</span>
  </div>
</nav>

<!-- 탭 네비게이션 -->
<div style="background:#161b22;border-bottom:1px solid #30363d" class="mb-4">
  <div class="container-lg px-4">
    <ul class="nav nav-tabs border-0" id="mainTabs" role="tablist">
      <li class="nav-item" role="presentation">
        <button class="nav-link active" id="tab-recommend-btn"
                data-bs-toggle="tab" data-bs-target="#tab-recommend"
                type="button" role="tab" style="color:#8b949e;border-color:transparent">
          추천 현황
        </button>
      </li>
      <li class="nav-item" role="presentation">
        <button class="nav-link" id="tab-backtest-btn"
                data-bs-toggle="tab" data-bs-target="#tab-backtest"
                type="button" role="tab" style="color:#8b949e;border-color:transparent">
          백테스트 성과
        </button>
      </li>
    </ul>
  </div>
</div>

<div class="tab-content">

<!-- ===== 탭 1: 추천 현황 ===== -->
<div class="tab-pane fade show active" id="tab-recommend" role="tabpanel">
<div class="container-lg">

  <!-- 요약 카드 -->
  <div class="row g-3 mb-2" id="summaryCards"></div>

  <hr class="section-divider">

  <!-- 이번 주 TOP 10 -->
  <div class="card shadow-sm mb-4">
    <div class="card-header bg-white d-flex align-items-center">
      <h5 class="mb-0">이번 주 추천 종목</h5>
      <span class="ms-2 badge bg-secondary">__LATEST_WEEK__</span>
    </div>
    <div class="card-body p-0">
      <div class="table-responsive">
        <table class="table table-hover align-middle mb-0 small">
          <thead class="table-light" id="topHead"></thead>
          <tbody id="topBody"></tbody>
        </table>
      </div>
    </div>
  </div>

  <!-- 수익률 차트 + 성과 요약 -->
  <div class="row g-3 mb-4">
    <div class="col-lg-8">
      <div class="card shadow-sm h-100">
        <div class="card-header bg-white">
          <h5 class="mb-0">가상 포트폴리오 누적 수익률</h5>
        </div>
        <div class="card-body">
          <canvas id="returnChart"></canvas>
        </div>
      </div>
    </div>
    <div class="col-lg-4">
      <div class="card shadow-sm h-100">
        <div class="card-header bg-white"><h5 class="mb-0">성과 요약</h5></div>
        <div class="card-body" id="perfSummary"></div>
      </div>
    </div>
  </div>

  <hr class="section-divider">

  <!-- 주차별 이력 -->
  <div class="card shadow-sm mb-4">
    <div class="card-header bg-white"><h5 class="mb-0">주차별 추천 이력</h5></div>
    <div class="card-body p-0">
      <div class="accordion accordion-flush" id="historyAcc"></div>
    </div>
  </div>

  <hr class="section-divider">

  <footer class="text-center text-muted small mb-4">
    ⚠️ 본 대시보드는 투자 참고용 시뮬레이션입니다. 실제 투자 권유가 아닙니다.<br>
    주가 데이터는 KRX 제공이며 실시간이 아닙니다.
  </footer>
</div>
</div><!-- /tab-recommend -->

<!-- ===== 탭 2: 백테스트 성과 ===== -->
<div class="tab-pane fade" id="tab-backtest" role="tabpanel">
<div class="container-lg" id="btContainer">

  <!-- 데이터 없음 안내 (JS로 숨김 처리) -->
  <div id="btEmpty" class="text-center py-5 text-muted">
    <div style="font-size:2.5rem;margin-bottom:1rem">🔬</div>
    <h5>백테스트 데이터가 없습니다</h5>
    <p class="small mt-2">
      아래 명령어로 백테스트를 실행하면 결과가 표시됩니다.<br>
      <code>python main.py --backtest</code>
    </p>
    <p class="small text-muted" style="font-size:.75rem">
      최초 실행 시 OHLCV 캐시 빌드로 10~20분 소요됩니다.
    </p>
  </div>

  <!-- 백테스트 결과 (데이터 있을 때만 표시) -->
  <div id="btResult" style="display:none">

    <!-- KPI 카드 -->
    <div class="row g-3 mb-4" id="btKpiCards"></div>

    <!-- 차트 + 거래 통계 -->
    <div class="row g-3 mb-4">
      <div class="col-lg-8">
        <div class="card shadow-sm h-100">
          <div class="card-header bg-white d-flex align-items-center gap-2">
            <h5 class="mb-0">누적 수익률 vs KOSPI</h5>
            <span class="badge bg-secondary" id="btPeriodBadge"></span>
          </div>
          <div class="card-body">
            <canvas id="btChart"></canvas>
          </div>
        </div>
      </div>
      <div class="col-lg-4">
        <div class="card shadow-sm h-100">
          <div class="card-header bg-white"><h5 class="mb-0">거래 통계</h5></div>
          <div class="card-body" id="btStats"></div>
        </div>
      </div>
    </div>

    <hr class="section-divider">

    <!-- 거래 기록 테이블 -->
    <div class="card shadow-sm mb-4">
      <div class="card-header bg-white d-flex align-items-center gap-2">
        <h5 class="mb-0">거래 기록</h5>
        <span class="badge bg-secondary" id="btTradeCount"></span>
        <span class="text-muted small ms-auto" id="btGenDate"></span>
      </div>
      <div class="card-body p-0">
        <div class="table-responsive">
          <table class="table table-sm table-hover align-middle mb-0 small">
            <thead class="table-light">
              <tr>
                <th>종목명</th><th>코드</th><th>점수</th>
                <th>매수일</th><th>매도일</th>
                <th>매수가</th><th>매도가</th>
                <th>수익률</th><th>보유(주)</th><th>사유</th>
              </tr>
            </thead>
            <tbody id="btTradeBody"></tbody>
          </table>
        </div>
      </div>
    </div>

    <!-- 주의사항 -->
    <div class="card shadow-sm mb-4" style="border-color:#d29922!important">
      <div class="card-body small text-muted">
        <strong style="color:#d29922">⚠️ 백테스트 주의사항</strong><br>
        <ul class="mb-0 mt-2 ps-3" id="btCaveats"></ul>
      </div>
    </div>

  </div><!-- /btResult -->

  <hr class="section-divider">
  <footer class="text-center text-muted small mb-4">
    ⚠️ 본 대시보드는 투자 참고용 시뮬레이션입니다. 실제 투자 권유가 아닙니다.<br>
    주가 데이터는 KRX 제공이며 실시간이 아닙니다.
  </footer>
</div>
</div><!-- /tab-backtest -->

</div><!-- /tab-content -->

<!-- 종목 상세 모달 -->
<div class="modal fade" id="detailModal" tabindex="-1" aria-hidden="true">
  <div class="modal-dialog modal-dialog-centered">
    <div class="modal-content">
      <div class="modal-header py-3">
        <div>
          <h5 class="modal-title fw-bold mb-1" id="modalStockName"></h5>
          <span class="badge bg-secondary" id="modalStockTicker"></span>
        </div>
        <button type="button" class="btn-close btn-close-white ms-auto" data-bs-dismiss="modal"></button>
      </div>
      <div class="modal-body pt-3" id="modalBody"></div>
    </div>
  </div>
</div>

<script>
/*
 * 보안 고지: 아래 DATA 객체는 주식 시장 공개 데이터(종목코드·가격·점수)만 포함합니다.
 * API 키, 사용자 자격증명, 개인식별정보는 일절 포함되지 않습니다.
 */
const DATA = __DATA_JSON__;

const MAX    = DATA.max_score;
const KEYS   = DATA.cond_keys;
const LBLS   = DATA.cond_labels;
const TTLS   = DATA.cond_titles;
const SCORES = DATA.cond_scores;

const STOCK_CACHE = {};

function fmt(n)  { return Number(n).toLocaleString('ko-KR'); }
function sign(v) { return v >= 0 ? '+' : ''; }
function retHtml(v) {
  const cls = v > 0 ? 'text-danger' : v < 0 ? 'text-primary' : 'text-secondary';
  return `<span class="${cls} fw-semibold">${sign(v)}${v.toFixed(1)}%</span>`;
}

function showDetail(cacheKey) {
  const s = STOCK_CACHE[cacheKey];
  if (!s) return;
  document.getElementById('modalStockName').textContent = s.name;
  document.getElementById('modalStockTicker').textContent = s.ticker;

  const pct = MAX > 0 ? Math.round(s.score / MAX * 100) : 0;
  const clr = pct >= 70 ? '#3fb950' : pct >= 40 ? '#d29922' : '#8b949e';

  let html = `
    <div class="mb-4">
      <div class="d-flex justify-content-between align-items-center mb-1">
        <span class="text-muted small">종합 점수</span>
        <span class="fw-bold" style="color:${clr}">${s.score} / ${MAX}</span>
      </div>
      <div class="d-flex align-items-center gap-2">
        <div class="detail-score-bar">
          <div class="detail-score-fill" style="width:${pct}%;background:${clr}"></div>
        </div>
        <span class="small text-muted">${pct}%</span>
      </div>
    </div>`;

  const hasCond = s.conditions && Object.keys(s.conditions).length > 0;
  if (hasCond) {
    const bodyRows = KEYS.map((k, i) => {
      const ok = !!s.conditions[k];
      const pts = SCORES[i];
      return `<tr>
        <td>${TTLS[i]}</td>
        <td class="text-center"><span class="dot ${ok ? 'dot-ok' : 'dot-no'}">${ok ? '●' : '○'}</span></td>
        <td class="text-center text-muted">${pts}</td>
        <td class="text-center fw-semibold" style="color:${ok ? '#3fb950' : '#484f58'}">${ok ? pts : 0}</td>
      </tr>`;
    }).join('');
    html += `
      <table class="table table-sm small mb-0">
        <thead class="table-light">
          <tr><th>조건</th><th class="text-center">충족</th>
              <th class="text-center text-muted">배점</th><th class="text-center">획득</th></tr>
        </thead>
        <tbody>${bodyRows}</tbody>
        <tfoot>
          <tr class="fw-bold" style="border-top:1px solid #30363d">
            <td colspan="2">합계</td>
            <td class="text-center text-muted">${MAX}</td>
            <td class="text-center" style="color:${clr}">${s.score}</td>
          </tr>
        </tfoot>
      </table>`;
  } else {
    html += `<div class="text-center text-muted py-3 small">
      <div style="font-size:1.8rem;margin-bottom:.5rem">📋</div>
      조건 상세 데이터가 없습니다.<br>
      <span style="font-size:.75rem">다음 스크리너 실행 후 표시됩니다.</span>
    </div>`;
  }

  document.getElementById('modalBody').innerHTML = html;
  bootstrap.Modal.getOrCreateInstance(document.getElementById('detailModal')).show();
}

/* 요약 카드 */
(function() {
  const o = DATA.overall;
  if (!o) return;
  const s   = o.overall_return_pct;
  const clr = s >= 0 ? '#3fb950' : '#f85149';
  const arr = s >= 0 ? '▲' : '▼';
  const pnl = o.total_current_man - o.total_invested_man;
  document.getElementById('summaryCards').innerHTML = `
    <div class="col-12 col-md-6">
      <div class="card shadow-sm h-100 text-center py-4 px-3">
        <div class="kpi-label">누적 수익률</div>
        <div class="kpi-value" style="font-size:2rem;font-weight:700;color:${clr}">
          ${arr} ${sign(s)}${s.toFixed(2)}%
        </div>
        <div class="kpi-sub">${o.weeks}주 운용</div>
      </div>
    </div>
    <div class="col-6 col-md-3">
      <div class="card shadow-sm h-100 text-center py-4 px-3">
        <div class="kpi-label">총 투자금</div>
        <div class="kpi-value" style="font-size:1.4rem;font-weight:600">
          ${fmt(o.total_invested_man)}<span style="font-size:.85rem;color:#8b949e"> 만원</span>
        </div>
        <div class="kpi-sub">${o.weeks}주 × 최대 1,000만원</div>
      </div>
    </div>
    <div class="col-6 col-md-3">
      <div class="card shadow-sm h-100 text-center py-4 px-3">
        <div class="kpi-label">현재 평가금</div>
        <div class="kpi-value" style="font-size:1.4rem;font-weight:600">
          ${fmt(o.total_current_man)}<span style="font-size:.85rem;color:#8b949e"> 만원</span>
        </div>
        <div class="kpi-sub" style="color:${clr}">${sign(pnl)}${fmt(pnl)} 만원</div>
      </div>
    </div>`;
})();

/* 이번 주 TOP 10 테이블 헤더 */
(function() {
  document.getElementById('topHead').innerHTML =
    `<tr><th>#</th><th>종목명</th><th>코드</th><th>점수</th>
     <th>매수가</th><th>현재가</th><th>수익률</th><th class="text-center">조건</th></tr>`;
})();

/* 이번 주 TOP 10 테이블 바디 */
(function() {
  const h = DATA.history;
  if (!h.length) return;
  const latest = h[h.length - 1];
  const rows = latest.stocks.map((s, i) => {
    const cacheKey = `${latest.week}_${s.ticker}`;
    STOCK_CACHE[cacheKey] = s;
    const pct = Math.round(s.score / MAX * 100);
    const scoreCell = `<td>
      <div class="score-wrap">
        <div class="score-track"><div class="score-fill-lg" style="width:${pct}%"></div></div>
        <span class="score-num">${s.score}<span class="score-max">/${MAX}</span></span>
      </div></td>`;
    const dots = KEYS.map((k, idx) => {
      const ok = s.conditions && s.conditions[k];
      return `<span class="dot ${ok ? 'dot-ok' : 'dot-no'}" title="${TTLS[idx]}">${ok ? '●' : '○'}</span>`;
    }).join('');
    return `<tr class="clickable-row" onclick="showDetail('${cacheKey}')">
      <td class="text-muted">${i+1}</td>
      <td><strong>${s.name}</strong></td>
      <td><span class="badge bg-secondary">${s.ticker}</span></td>
      ${scoreCell}
      <td>${fmt(s.buy_price)}원</td>
      <td>${fmt(s.current_price)}원</td>
      <td>${retHtml(s.return_pct)}</td>
      <td><div class="dots-wrap">${dots}</div></td>
    </tr>`;
  }).join('');
  document.getElementById('topBody').innerHTML = rows || '<tr><td colspan="8" class="text-center text-muted py-3">데이터 없음</td></tr>';
})();

/* 수익률 차트 */
(function() {
  const c = DATA.chart;
  if (!c || !c.labels.length) return;

  /* 그라디언트 채움 + 0% 기준선 인라인 플러그인 */
  const areaPlugin = {
    id: 'dynamicArea',
    beforeDraw(chart) {
      const { ctx, chartArea, scales: { y } } = chart;
      if (!chartArea || !y) return;
      const yZero = y.getPixelForValue(0);
      const ratio = Math.min(Math.max(
        (yZero - chartArea.top) / (chartArea.bottom - chartArea.top), 0), 1);
      const grad = ctx.createLinearGradient(0, chartArea.top, 0, chartArea.bottom);
      grad.addColorStop(0,     'rgba(63,185,80,0.20)');
      grad.addColorStop(ratio, 'rgba(63,185,80,0.04)');
      grad.addColorStop(ratio, 'rgba(248,81,73,0.04)');
      grad.addColorStop(1,     'rgba(248,81,73,0.20)');
      chart.data.datasets[0].backgroundColor = grad;
    },
    afterDraw(chart) {
      const { ctx, chartArea, scales: { y } } = chart;
      if (!y) return;
      const yZero = y.getPixelForValue(0);
      if (yZero < chartArea.top || yZero > chartArea.bottom) return;
      ctx.save();
      ctx.beginPath();
      ctx.moveTo(chartArea.left, yZero);
      ctx.lineTo(chartArea.right, yZero);
      ctx.strokeStyle = 'rgba(255,255,255,0.30)';
      ctx.lineWidth   = 1;
      ctx.setLineDash([5, 5]);
      ctx.stroke();
      ctx.restore();
    }
  };

  new Chart(document.getElementById('returnChart'), {
    type: 'line',
    plugins: [areaPlugin],
    data: {
      labels: c.labels,
      datasets: [{
        label: '누적 수익률',
        data: c.cumulative,
        borderColor: '#3fb950',
        backgroundColor: 'rgba(63,185,80,0.1)',
        fill: true,
        tension: 0.3,
        pointRadius: 5,
        pointHoverRadius: 7,
        pointBackgroundColor: c.cumulative.map(v => v >= 0 ? '#3fb950' : '#f85149'),
        pointBorderColor:     c.cumulative.map(v => v >= 0 ? '#3fb950' : '#f85149'),
        segment: {
          borderColor: ctx => ctx.p1.parsed.y >= 0 ? '#3fb950' : '#f85149',
        }
      }]
    },
    options: {
      responsive: true,
      plugins: {
        legend: { display: false },
        tooltip: {
          callbacks: {
            title: items => items[0].label,
            label: ctx => {
              const v = ctx.raw;
              return ` 누적 수익률: ${sign(v)}${v.toFixed(2)}%`;
            }
          }
        }
      },
      scales: {
        y: {
          ticks: { color: '#8b949e', callback: v => sign(v) + v + '%' },
          grid:  { color: 'rgba(255,255,255,0.06)' }
        },
        x: { ticks: { color: '#8b949e' } }
      }
    }
  });
})();

/* 성과 요약 (베스트/워스트) */
(function() {
  const all = DATA.history.flatMap(e => e.stocks);
  if (!all.length) return;
  const sorted = [...all].sort((a, b) => b.return_pct - a.return_pct);
  const top3  = sorted.slice(0, 3);
  const bot3  = sorted.slice(-3).reverse();
  let html = '';
  if (top3.length) {
    html += '<p class="mb-1 fw-semibold small">🏆 베스트 종목</p><ul class="list-unstyled small mb-3">';
    top3.forEach(s => { html += `<li>${s.name} ${retHtml(s.return_pct)}</li>`; });
    html += '</ul>';
  }
  if (bot3.length) {
    html += '<p class="mb-1 fw-semibold small">💀 워스트 종목</p><ul class="list-unstyled small">';
    bot3.forEach(s => { html += `<li>${s.name} ${retHtml(s.return_pct)}</li>`; });
    html += '</ul>';
  }
  document.getElementById('perfSummary').innerHTML = html;
})();

/* 주차별 이력 아코디언 */
(function() {
  const history = [...DATA.history].reverse();
  const acc = document.getElementById('historyAcc');
  acc.innerHTML = history.map((entry, i) => {
    const id = `acc${i}`;
    const rows = entry.stocks.map((s, j) => {
      const cacheKey = `${entry.week}_${s.ticker}`;
      STOCK_CACHE[cacheKey] = s;
      const pct = Math.round(s.score / MAX * 100);
      const dots = KEYS.map((k, idx) => {
        const ok = s.conditions && s.conditions[k];
        return `<span class="dot ${ok ? 'dot-ok' : 'dot-no'}" title="${TTLS[idx]}">${ok ? '●' : '○'}</span>`;
      }).join('');
      return `<tr class="clickable-row" onclick="showDetail('${cacheKey}')">
        <td>${j+1}</td>
        <td>${s.name} <span class="badge bg-secondary">${s.ticker}</span></td>
        <td><div class="score-wrap">
          <div class="score-track"><div class="score-fill-lg" style="width:${pct}%"></div></div>
          <span class="score-num">${s.score}<span class="score-max">/${MAX}</span></span>
        </div></td>
        <td>${fmt(s.buy_price)}원</td>
        <td>${fmt(s.current_price)}원</td>
        <td>${retHtml(s.return_pct)}</td>
        <td><div class="dots-wrap">${dots}</div></td>
      </tr>`;
    }).join('');
    return `
      <div class="accordion-item">
        <h2 class="accordion-header">
          <button class="accordion-button ${i > 0 ? 'collapsed' : ''} small py-2"
                  type="button" data-bs-toggle="collapse" data-bs-target="#${id}">
            <span class="fw-semibold me-2">${entry.week}</span>
            <span class="text-muted me-2">${entry.stocks.length}종목</span>
            ${retHtml(entry.week_return_pct)}
          </button>
        </h2>
        <div id="${id}" class="accordion-collapse collapse ${i === 0 ? 'show' : ''}">
          <div class="accordion-body p-0">
            <div class="table-responsive">
              <table class="table table-sm table-hover align-middle mb-0 small">
                <thead class="table-light"><tr>
                  <th>#</th><th>종목</th><th>점수</th>
                  <th>매수가</th><th>현재가</th><th>수익률</th><th class="text-center">조건</th>
                </tr></thead>
                <tbody>${rows}</tbody>
              </table>
            </div>
          </div>
        </div>
      </div>`;
  }).join('');
})();

/* ── 백테스트 탭 렌더링 ─────────────────────────────────────── */
(function() {
  const BT = DATA.backtest;
  if (!BT || !BT.performance) return;

  document.getElementById('btEmpty').style.display = 'none';
  document.getElementById('btResult').style.display = '';

  const p = BT.performance;
  const meta = BT.meta || {};
  const period = meta.backtest_period || {};

  /* 기간 배지 */
  if (period.start && period.end) {
    document.getElementById('btPeriodBadge').textContent =
      `${period.start} ~ ${period.end} (${period.weeks}주)`;
  }
  if (meta.generated_at) {
    document.getElementById('btGenDate').textContent = `생성일: ${meta.generated_at}`;
  }

  /* KPI 카드 */
  function kpiCard(label, value, sub, color) {
    return `<div class="col-6 col-md-3">
      <div class="card shadow-sm h-100">
        <div class="bt-kpi-card">
          <div class="bt-kpi-label">${label}</div>
          <div class="bt-kpi-value" style="color:${color}">${value}</div>
          ${sub ? `<div class="bt-kpi-sub">${sub}</div>` : ''}
        </div>
      </div>
    </div>`;
  }

  const alphaColor = p.alpha_pct >= 0 ? '#3fb950' : '#f85149';
  const totColor   = p.total_return_pct >= 0 ? '#3fb950' : '#f85149';

  document.getElementById('btKpiCards').innerHTML =
    kpiCard('전략 총 수익률',
      `${sign(p.total_return_pct)}${p.total_return_pct.toFixed(1)}%`,
      `KOSPI ${sign(p.kospi_return_pct)}${p.kospi_return_pct.toFixed(1)}%`, totColor) +
    kpiCard('KOSPI 대비 알파',
      `${sign(p.alpha_pct)}${p.alpha_pct.toFixed(1)}%`,
      '초과 수익률', alphaColor) +
    kpiCard('승률',
      `${p.win_rate_pct.toFixed(1)}%`,
      `${p.total_trades}건 거래`, p.win_rate_pct >= 50 ? '#3fb950' : '#f85149') +
    kpiCard('최대 낙폭(MDD)',
      `${p.mdd_pct.toFixed(1)}%`,
      `샤프 ${p.sharpe_ratio.toFixed(2)}`, '#d29922');

  /* 거래 통계 카드 */
  document.getElementById('btStats').innerHTML = `
    <ul class="list-unstyled small mb-0">
      <li class="d-flex justify-content-between py-1" style="border-bottom:1px solid #30363d">
        <span class="text-muted">총 거래 횟수</span>
        <strong>${p.total_trades}건</strong>
      </li>
      <li class="d-flex justify-content-between py-1" style="border-bottom:1px solid #30363d">
        <span class="text-muted">평균 보유 기간</span>
        <strong>${p.avg_holding_weeks}주</strong>
      </li>
      <li class="d-flex justify-content-between py-1" style="border-bottom:1px solid #30363d">
        <span class="text-muted">평균 거래 수익률</span>
        <strong style="color:${p.avg_trade_return_pct >= 0 ? '#3fb950' : '#f85149'}">
          ${sign(p.avg_trade_return_pct)}${p.avg_trade_return_pct.toFixed(2)}%
        </strong>
      </li>
      <li class="d-flex justify-content-between py-1" style="border-bottom:1px solid #30363d">
        <span class="text-muted">샤프 비율</span>
        <strong>${p.sharpe_ratio.toFixed(3)}</strong>
      </li>
      <li class="d-flex justify-content-between py-1">
        <span class="text-muted">최종 평가금</span>
        <strong>${fmt(Math.round(p.final_value / 10000))}만원</strong>
      </li>
    </ul>
    <div class="mt-3 small text-muted" style="font-size:.72rem">
      초기 투자금 ${fmt(p.initial_capital / 10000)}만원 기준<br>
      전략: 기술 지표만 사용, Top-N 탈락 시 매도
    </div>`;

  /* 누적 수익률 vs KOSPI 차트 */
  const ws = BT.weekly_series || [];
  if (ws.length) {
    new Chart(document.getElementById('btChart'), {
      type: 'line',
      data: {
        labels: ws.map(w => w.date),
        datasets: [
          {
            label: '백테스트 전략',
            data: ws.map(w => w.portfolio_return_pct),
            borderColor: '#3fb950',
            backgroundColor: 'rgba(63,185,80,0.08)',
            fill: true,
            tension: 0.3,
            pointRadius: 3,
            pointHoverRadius: 6,
            segment: { borderColor: ctx => ctx.p1.parsed.y >= 0 ? '#3fb950' : '#f85149' }
          },
          {
            label: 'KOSPI',
            data: ws.map(w => w.kospi_return_pct),
            borderColor: '#58a6ff',
            backgroundColor: 'transparent',
            fill: false,
            tension: 0.3,
            pointRadius: 2,
            pointHoverRadius: 5,
            borderDash: [6, 3],
          }
        ]
      },
      options: {
        responsive: true,
        plugins: {
          legend: {
            display: true,
            labels: { color: '#8b949e', boxWidth: 20, padding: 16 }
          },
          tooltip: {
            callbacks: {
              label: ctx => {
                const v = ctx.raw;
                return ` ${ctx.dataset.label}: ${sign(v)}${v.toFixed(2)}%`;
              }
            }
          }
        },
        scales: {
          y: {
            ticks: { color: '#8b949e', callback: v => sign(v) + v + '%' },
            grid:  { color: 'rgba(255,255,255,0.06)' }
          },
          x: { ticks: { color: '#8b949e', maxTicksLimit: 12 } }
        }
      }
    });
  }

  /* 거래 기록 테이블 */
  const trades = [...(BT.trades || [])].sort((a, b) =>
    (b.sell_date || '').localeCompare(a.sell_date || '')
  );
  document.getElementById('btTradeCount').textContent = `${trades.length}건`;

  const EXIT_LABEL = {
    dropped_from_top10: '탈락',
    end_of_backtest:    '종료',
  };

  document.getElementById('btTradeBody').innerHTML = trades.map(t => {
    const clr = t.pnl_pct > 0 ? 'text-danger' : t.pnl_pct < 0 ? 'text-primary' : 'text-secondary';
    const exitLabel = EXIT_LABEL[t.exit_reason] || t.exit_reason || '-';
    return `<tr>
      <td><strong>${t.name}</strong></td>
      <td><span class="badge bg-secondary">${t.ticker}</span></td>
      <td class="text-muted">${t.buy_score ?? '-'}</td>
      <td class="text-muted">${t.buy_date}</td>
      <td class="text-muted">${t.sell_date}</td>
      <td>${fmt(t.buy_price)}원</td>
      <td>${fmt(t.sell_price)}원</td>
      <td><span class="${clr} fw-semibold">${sign(t.pnl_pct)}${t.pnl_pct.toFixed(2)}%</span></td>
      <td class="text-muted">${t.holding_weeks}주</td>
      <td><span class="badge bg-secondary">${exitLabel}</span></td>
    </tr>`;
  }).join('') || '<tr><td colspan="10" class="text-center text-muted py-3">거래 기록 없음</td></tr>';

  /* 주의사항 */
  const caveats = (meta.caveats || []);
  document.getElementById('btCaveats').innerHTML = caveats.map(c => `<li>${c}</li>`).join('');
})();
</script>
<script src="https://cdn.jsdelivr.net/npm/bootstrap@5.3.3/dist/js/bootstrap.bundle.min.js"></script>
</body>
</html>
"""


def _render_html(payload: dict) -> str:
    data_json = json.dumps(payload, ensure_ascii=False, default=str)
    return (
        _HTML
        .replace("__DATA_JSON__",  data_json)
        .replace("__UPDATED_AT__", payload.get("updated_at", ""))
        .replace("__LATEST_WEEK__", payload.get("latest_week", ""))
    )


# ── 진입점 ────────────────────────────────────────────────────────────────────

def generate_dashboard() -> None:
    """
    docs/index.html 정적 대시보드 생성.
    실패 시 예외를 삼키고 경고 로그만 남겨 스크리너 전체 흐름에 영향 없음.
    """
    history = _load_history()
    if not history:
        logger.info("추천 이력 없음 — 대시보드 생성 건너뜀")
        return

    all_tickers = list({s["ticker"] for e in history for s in e["stocks"]})
    prices = _get_current_prices(all_tickers)

    enriched, overall = _enrich(history, prices)
    payload = _build_payload(enriched, overall)
    html = _render_html(payload)

    _DOCS_DIR.mkdir(exist_ok=True)
    (_DOCS_DIR / "index.html").write_text(html, encoding="utf-8")
    (_DOCS_DIR / ".nojekyll").touch(exist_ok=True)
    logger.info(f"대시보드 생성 완료: {_DOCS_DIR / 'index.html'}")
