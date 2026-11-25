// 백그라운드 작업(채점·검증) 완료 알림과 검증 진행 화면 갱신.
//
// 화면에 <div id="live" data-mode=...>(templates/_live.html)가 있을 때만 동작한다.
//   alert     기다리는 제출이 끝나면 화면 위에 알림 띠 + [새로고침]   (내 제출, 제출 목록, 검토, 리포트)
//   progress  검증 진행 화면의 단계·시간을 갱신, 끝나면 [리포트 보기]  (진행 화면)
//   board     리더보드 비교값이 바뀌면 "순위가 바뀌었습니다"           (리더보드)
//
// 원칙
//   - 자동으로 새로고침하지 않는다(검토 사유를 쓰는 중일 수 있으므로). 알림만 띄운다.
//   - 3초마다 묻고, 1분이 지나면 10초로 늦춘다(리더보드는 15초). 탭이 안 보이면 멈췄다가 다시 보이면 이어 간다.
//   - 기다릴 것이 없으면 묻지 않는다.
//   - 서버 문구는 textContent로만 넣는다(HTML로 해석하지 않음 → XSS 방지). 우리 서버 주소만 부른다(CSP).
(function () {
  "use strict";
  const box = document.getElementById("live");
  const banner = document.getElementById("live-banner");
  if (!box || !banner) return;

  const mode = box.dataset.mode;
  const url = box.dataset.url;
  let ids = (box.dataset.ids || "").split(",").filter(Boolean);
  let board = box.dataset.board || null;
  const finished = [];
  const startedAt = Date.now();
  let timer = null;

  function interval() {
    if (mode === "board") return 15000;
    return Date.now() - startedAt > 60000 ? 10000 : 3000;
  }

  function waiting() {
    return mode === "board" ? board !== null : ids.length > 0;
  }

  function showBanner(message, linkText, href) {
    const text = document.createElement("span");
    text.textContent = message;
    const link = document.createElement("a");
    link.className = "btn small";
    link.textContent = linkText;
    link.href = href || window.location.href;
    banner.replaceChildren(text, link);
    banner.hidden = false;
  }

  function renderProgress(item) {
    const headline = document.getElementById("live-headline");
    if (headline) headline.textContent = item.headline;
    (item.steps || []).forEach(function (step) {
      const li = document.querySelector('.step[data-step="' + step.key + '"]');
      if (!li) return;
      li.className = "step " + step.state;
      li.querySelector(".step-time").textContent = step.seconds === null ? "" : step.seconds + "초";
    });
    const wrap = document.getElementById("live-limit");
    if (wrap) {
      wrap.hidden = !item.limit;
      if (item.limit) {
        const bar = document.getElementById("live-bar");
        bar.max = item.limit;
        bar.value = Math.min(item.elapsed || 0, item.limit);
        document.getElementById("live-bar-text").textContent = (item.elapsed || 0) + "초 / 제한 " + item.limit + "초";
      }
    }
  }

  function handle(data) {
    if (mode === "board") {
      if (board !== null && data.board && data.board !== board) {
        showBanner("순위가 바뀌었습니다", "새로고침");
        board = null;   // 한 번 알렸으면 더 묻지 않는다
      }
      return;
    }
    const items = data.items || {};
    if (mode === "progress") {
      const item = items[ids[0]];
      if (!item) return;
      renderProgress(item);
      if (item.done) {
        ids = [];
        showBanner(item.text, "리포트 보기", box.dataset.reportUrl);
      }
      return;
    }
    // alert: 끝난 것을 모아 한 줄로 알린다. 응답에 없는 번호(권한 없음 등)는 더 묻지 않는다
    ids = ids.filter(function (id) {
      const item = items[id];
      if (item && item.done) finished.push(item.text);
      return item && !item.done;
    });
    if (finished.length === 1) showBanner(finished[0], "새로고침");
    else if (finished.length > 1) showBanner(finished.length + "건 완료 (" + finished[finished.length - 1] + " 등)", "새로고침");
  }

  async function tick() {
    timer = null;
    if (document.hidden || !waiting()) return;
    const query = url + (ids.length ? "&ids=" + ids.join(",") : "");
    try {
      const resp = await fetch(query, { credentials: "same-origin", headers: { Accept: "application/json" } });
      if (resp.ok) handle(await resp.json());
    } catch (err) {
      // 네트워크가 잠깐 끊겨도 다음 차례에 다시 묻는다
    }
    schedule();
  }

  function schedule() {
    if (timer === null && !document.hidden && waiting()) timer = setTimeout(tick, interval());
  }

  document.addEventListener("visibilitychange", schedule);
  schedule();
})();
