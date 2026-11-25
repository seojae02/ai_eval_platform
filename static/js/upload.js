// 제출 파일을 1MB 조각으로 나눠 순서대로 보낸다.
// 조각이 도착할 때마다 서버가 '마지막 수신 시각'을 갱신한다 → 느린 망에서도 받고 있는 동안은 만료되지 않는다(D3).
// 세 파일을 모두 보낸 뒤 폼을 제출하면 서버가 내용을 검사한다(제출 완료).
(function () {
  "use strict";
  const form = document.getElementById("upload-form");
  if (!form) return;
  const chunkUrl = form.dataset.chunkUrl;
  const chunkSize = Number(form.dataset.chunkSize);
  const csrfToken = form.querySelector("input[name=csrfmiddlewaretoken]").value;
  const statusBox = document.getElementById("upload-status");
  const button = form.querySelector("button[type=submit]");

  async function sendFile(input) {
    const file = input.files[0];
    const kind = input.dataset.kind;
    const progress = document.getElementById("progress-" + kind);
    for (let offset = 0; offset < file.size; offset += chunkSize) {
      const params = new URLSearchParams({ kind: kind, offset: String(offset), name: file.name });
      const resp = await fetch(chunkUrl + "?" + params.toString(), {
        method: "POST",
        credentials: "same-origin",
        headers: { "X-CSRFToken": csrfToken, "Content-Type": "application/octet-stream" },
        body: file.slice(offset, offset + chunkSize),
      });
      const body = await resp.json().catch(function () { return {}; });
      if (!resp.ok) throw new Error(body.error || "전송에 실패했습니다 (" + resp.status + ")");
      progress.value = (body.received / file.size) * 100;
    }
  }

  form.addEventListener("submit", async function (event) {
    event.preventDefault();
    const inputs = Array.from(form.querySelectorAll("input[type=file]"));
    if (inputs.some(function (i) { return i.files.length === 0; })) {
      statusBox.textContent = "파일 3개를 모두 선택하세요";
      return;
    }
    button.disabled = true;
    statusBox.textContent = "";
    try {
      for (const input of inputs) await sendFile(input);
      form.submit();   // 조각 전송이 모두 끝난 뒤에만 '제출 완료'
    } catch (err) {
      // textContent: 서버 문구를 HTML로 해석하지 않는다(XSS 방지)
      statusBox.textContent = err.message.replace(/\.$/, "").replace(/([^\d\s])\.\s+/g, "$1, ") + ", 문제가 계속되면 '업로드 취소' 후 다시 시작하세요";
      button.disabled = false;
    }
  });
})();
