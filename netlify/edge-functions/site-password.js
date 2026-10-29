// netlify/edge-functions/site-password.js
//
// Site-wide password for Ops Live, the KPI page and the wall displays.
// Runs before every page and file on the site and asks for a username and
// password using the browser's standard sign-in box (HTTP Basic auth).
//
// WHY THIS EXISTS: Netlify's built-in password and the Basic-Auth _headers
// rule are Pro-plan features. Edge Functions work on the current plan, so
// this does the same check itself.
//
// The credentials live only in Netlify: Project configuration >
// Environment variables > BASIC_AUTH_CREDENTIALS, as "username:password".
// Several logins can be listed separated by spaces:
//   "laballey:First-Password tv:Second-Password"
// The variable's Scopes must include Functions.
//
// Fails closed: if the variable is missing, every page shows a short
// "not configured" message rather than opening the site to everyone.

export default async (request, context) => {
  const configured = (Netlify.env.get("BASIC_AUTH_CREDENTIALS") || "").trim();
  if (!configured) {
    return new Response(
      "Site password is not configured. Add BASIC_AUTH_CREDENTIALS in Netlify's environment variables (Functions scope) and redeploy.",
      { status: 503, headers: { "Content-Type": "text/plain; charset=utf-8", "Cache-Control": "no-store" } },
    );
  }

  const allowed = configured.split(/\s+/);
  const header = request.headers.get("authorization") || "";
  if (header.toLowerCase().startsWith("basic ")) {
    let given = "";
    try {
      given = new TextDecoder().decode(Uint8Array.from(atob(header.slice(6).trim()), c => c.charCodeAt(0)));
    } catch (e) {
      given = "";
    }
    if (allowed.some(pair => same(pair, given))) return context.next();
  }

  return new Response("Password required.", {
    status: 401,
    headers: {
      "WWW-Authenticate": 'Basic realm="Lab Alley Ops", charset="UTF-8"',
      "Content-Type": "text/plain; charset=utf-8",
      "Cache-Control": "no-store",
    },
  });
};

// Compare without stopping at the first different character, so response
// timing doesn't hint at how much of a guess was right.
function same(a, b) {
  let diff = a.length ^ b.length;
  for (let i = 0; i < Math.max(a.length, b.length); i++) diff |= (a.charCodeAt(i) || 0) ^ (b.charCodeAt(i) || 0);
  return diff === 0;
}

export const config = { path: "/*" };
