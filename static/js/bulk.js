// 검토 화면 '승인 대기' 표의 모두 선택. 일괄 확정할 수 있는 줄(체크박스가 있는 줄)만 고른다.
// CSP(script-src 'self')가 인라인 스크립트를 막으므로 파일로 둔다. 서버가 대상을 다시 검사하므로 이 파일은 편의 기능일 뿐이다.
document.addEventListener("DOMContentLoaded", () => {
  const all = document.getElementById("pick-all");
  if (!all) return;
  const picks = document.querySelectorAll("input.pick");
  if (!picks.length) { all.disabled = true; return; }
  all.addEventListener("change", () => picks.forEach((box) => { box.checked = all.checked; }));
});
