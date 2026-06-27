export default {
  async fetch(request, env) {
    const url = new URL(request.url);
    const path = url.pathname;

    if (!path.startsWith("/bot")) {
      return new Response("Not found", { status: 404 });
    }

    const target = new URL(`https://api.telegram.org${path}`);
    target.search = url.search;

    const headers = new Headers(request.headers);
    headers.set("Host", "api.telegram.org");
    headers.set("CF-Worker-Telegram-Proxy", "true");

    const proxyRequest = new Request(target, {
      method: request.method,
      headers,
      body: request.body,
      redirect: "follow",
    });

    const response = await fetch(proxyRequest);

    const respHeaders = new Headers(response.headers);
    respHeaders.set("Access-Control-Allow-Origin", "*");

    return new Response(response.body, {
      status: response.status,
      statusText: response.statusText,
      headers: respHeaders,
    });
  },
};
