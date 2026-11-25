// AUN 轉發 Worker：有些網路（例如校網）會擋 Tailscale Funnel 的 IP，
// 讓使用端改連 https://<worker>.workers.dev，再由 Cloudflare 轉給 Funnel。
// 只轉發到下面這個網址，token 原封不動帶過去，由 AUN 自己驗證。
const HUB = "aun.<你的 tailnet>.ts.net";

export default {
  async fetch(request) {
    const url = new URL(request.url);
    url.protocol = "https:";
    url.hostname = HUB;
    url.port = "";
    return fetch(new Request(url, request));
  },
};
