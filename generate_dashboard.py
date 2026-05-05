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


# ── 데이터 수집 ───────────────────────────────────────────────────────────────

def _load_history() -> list:
    if not _HISTORY_FILE.exists():
        return []
    try:
        return json.loads(_HISTORY_FILE.read_text(encoding="utf-8"))
    except Exception as e:
        logger.error(f"추천 이력 로드 실패: {e}")
        return []


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
        "history":     enriched,
        "overall":     overall,
        "chart": {
            "labels":     [e["week"] for e in enriched],
            "cumulative": [e["cumulative_return_pct"] for e in enriched],
        },
    }


# ── HTML 렌더링 ───────────────────────────────────────────────────────────────

_HTML = """\
<!DOCTYPE html>
<html lang="ko">
<head>
  <meta charset="UTF-8">
  <meta name="viewport" content="width=device-width, initial-scale=1">
  <title>KRX 주간 종목 스크리너</title>
  <link href="https://cdn.jsdelivr.net/npm/bootstrap@5.3.3/dist/css/bootstrap.min.css" rel="stylesheet">
  <script src="https://cdn.jsdelivr.net/npm/chart.js@4.4.3/dist/chart.umd.min.js"></script>
  <style>
    .score-bar{display:flex;height:5px;border-radius:3px;overflow:hidden;margin-top:3px}
    .score-fill{background:#198754}
    .score-empty{background:#dee2e6}
    .cond-ok{color:#198754;font-size:.85rem}
    .cond-no{color:#adb5bd;font-size:.85rem}
    footer{border-top:1px solid #dee2e6;padding-top:1rem}
  </style>
</head>
<body class="bg-light">

<nav class="navbar navbar-dark bg-dark mb-4">
  <div class="container-fluid px-4">
    <span class="navbar-brand fw-bold">📊 KRX 주간 종목 스크리너</span>
    <span class="text-light small">최종 업데이트: __UPDATED_AT__</span>
  </div>
</nav>

<div class="container-lg">

  <!-- 요약 카드 -->
  <div class="row g-3 mb-4" id="summaryCards"></div>

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

  <!-- 주차별 이력 -->
  <div class="card shadow-sm mb-4">
    <div class="card-header bg-white"><h5 class="mb-0">주차별 추천 이력</h5></div>
    <div class="card-body p-0">
      <div class="accordion accordion-flush" id="historyAcc"></div>
    </div>
  </div>

  <footer class="text-center text-muted small mb-4">
    ⚠️ 본 대시보드는 투자 참고용 시뮬레이션입니다. 실제 투자 권유가 아닙니다.<br>
    주가 데이터는 KRX 제공이며 실시간이 아닙니다.
  </footer>
</div>

<script>
/*
 * 보안 고지: 아래 DATA 객체는 주식 시장 공개 데이터(종목코드·가격·점수)만 포함합니다.
 * API 키, 사용자 자격증명, 개인식별정보는 일절 포함되지 않습니다.
 */
const DATA = __DATA_JSON__;

const MAX  = DATA.max_score;
const KEYS = DATA.cond_keys;
const LBLS = DATA.cond_labels;
const TTLS = DATA.cond_titles;

function fmt(n)  { return Number(n).toLocaleString('ko-KR'); }
function sign(v) { return v >= 0 ? '+' : ''; }
function retHtml(v) {
  const cls = v > 0 ? 'text-danger' : v < 0 ? 'text-primary' : 'text-secondary';
  return `<span class="${cls} fw-semibold">${sign(v)}${v.toFixed(1)}%</span>`;
}

/* 요약 카드 */
(function() {
  const o = DATA.overall;
  if (!o) return;
  const s = o.overall_return_pct;
  const col = s >= 0 ? 'danger' : 'primary';
  const items = [
    ['누적 수익률',   `<span class="display-6 fw-bold text-${col}">${sign(s)}${s.toFixed(1)}%</span>`],
    ['총 투자금',     `<span class="h5">${fmt(o.total_invested_man)}만원</span><div class="text-muted small">${o.weeks}주차</div>`],
    ['현재 평가금',   `<span class="h5">${fmt(o.total_current_man)}만원</span>`],
  ];
  document.getElementById('summaryCards').innerHTML = items.map(([t, v]) =>
    `<div class="col-sm-4"><div class="card shadow-sm text-center py-3 px-2">
      <div class="text-muted small mb-1">${t}</div>${v}</div></div>`
  ).join('');
})();

/* 이번 주 TOP 10 테이블 헤더 */
(function() {
  const condThs = LBLS.map((l, i) =>
    `<th class="text-center" title="${TTLS[i]}">${l}</th>`
  ).join('');
  document.getElementById('topHead').innerHTML =
    `<tr><th>#</th><th>종목명</th><th>코드</th><th>점수</th>
     <th>매수가</th><th>현재가</th><th>수익률</th>${condThs}</tr>`;
})();

/* 이번 주 TOP 10 테이블 바디 */
(function() {
  const h = DATA.history;
  if (!h.length) return;
  const latest = h[h.length - 1];
  const rows = latest.stocks.map((s, i) => {
    const pct = Math.round(s.score / MAX * 100);
    const scoreCell =
      `<td><span class="fw-semibold">${s.score}</span><span class="text-muted">/${MAX}</span>
       <div class="score-bar"><div class="score-fill" style="width:${pct}%"></div>
       <div class="score-empty" style="width:${100-pct}%"></div></div></td>`;
    const condCells = KEYS.map(k => {
      const ok = s.conditions && s.conditions[k];
      return `<td class="text-center">${ok
        ? '<span class="cond-ok" title="충족">✔</span>'
        : '<span class="cond-no" title="미충족">✘</span>'}
      </td>`;
    }).join('');
    return `<tr>
      <td class="text-muted">${i+1}</td>
      <td><strong>${s.name}</strong></td>
      <td><span class="badge bg-secondary">${s.ticker}</span></td>
      ${scoreCell}
      <td>${fmt(s.buy_price)}원</td>
      <td>${fmt(s.current_price)}원</td>
      <td>${retHtml(s.return_pct)}</td>
      ${condCells}
    </tr>`;
  }).join('');
  document.getElementById('topBody').innerHTML = rows || '<tr><td colspan="20" class="text-center text-muted py-3">데이터 없음</td></tr>';
})();

/* 수익률 차트 */
(function() {
  const c = DATA.chart;
  if (!c || !c.labels.length) return;
  new Chart(document.getElementById('returnChart'), {
    type: 'line',
    data: {
      labels: c.labels,
      datasets: [{
        label: '누적 수익률 (%)',
        data: c.cumulative,
        borderColor: '#dc3545',
        backgroundColor: 'rgba(220,53,69,0.07)',
        fill: true,
        tension: 0.3,
        pointRadius: 5,
        pointHoverRadius: 7,
      }]
    },
    options: {
      responsive: true,
      plugins: {
        legend: { display: false },
        tooltip: { callbacks: { label: ctx => sign(ctx.raw) + ctx.raw.toFixed(2) + '%' } }
      },
      scales: {
        y: {
          ticks: { callback: v => sign(v) + v + '%' },
          grid: { color: 'rgba(0,0,0,0.04)' }
        }
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
  const condThs = LBLS.map((l, i) =>
    `<th class="text-center" title="${TTLS[i]}">${l}</th>`
  ).join('');
  const acc = document.getElementById('historyAcc');
  acc.innerHTML = history.map((entry, i) => {
    const id = `acc${i}`;
    const rows = entry.stocks.map((s, j) => {
      const condCells = KEYS.map(k => {
        const ok = s.conditions && s.conditions[k];
        return `<td class="text-center">${ok ? '<span class="cond-ok">✔</span>' : '<span class="cond-no">✘</span>'}</td>`;
      }).join('');
      return `<tr>
        <td>${j+1}</td>
        <td>${s.name} <span class="badge bg-secondary">${s.ticker}</span></td>
        <td><span class="fw-semibold">${s.score}</span>/${MAX}</td>
        <td>${fmt(s.buy_price)}원</td>
        <td>${fmt(s.current_price)}원</td>
        <td>${retHtml(s.return_pct)}</td>
        ${condCells}
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
                  <th>매수가</th><th>현재가</th><th>수익률</th>
                  ${condThs}
                </tr></thead>
                <tbody>${rows}</tbody>
              </table>
            </div>
          </div>
        </div>
      </div>`;
  }).join('');
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
