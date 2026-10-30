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
// Two ways in:
//
// 1. People: username and password, from Netlify > Project configuration >
//    Environment variables > BASIC_AUTH_CREDENTIALS, as "username:password".
//    Several logins can be listed separated by spaces:
//      "laballey:First-Password tv:Second-Password"
//
// 2. The TV (OptiSigns stick): Android signage players can't show the
//    browser's sign-in box, so the TV opens a private link instead:
//      https://<site>/tv/wall.html?tvkey=<TV_ACCESS_KEY>
//    A matching key sets a long-lasting cookie, so the page's own files,
//    frames and update checks get through too. TV access only covers the
//    wall-display files (/tv/... and /ops-logic.js): it does not open Ops
//    Live or the KPI page. Change TV_ACCESS_KEY to shut an old link out.
//
// Both variables need the Functions scope. Fails closed: if
// BASIC_AUTH_CREDENTIALS is missing, every page shows a short "not
// configured" message rather than opening the site to everyone.

const TV_COOKIE = "lab_alley_tv";
const TV_COOKIE_DAYS = 400;   // the longest Chrome keeps a cookie

export default async (request, context) => {
  const configured = (Netlify.env.get("BASIC_AUTH_CREDENTIALS") || "").trim();
  if (!configured) {
    return new Response(
      "Site password is not configured. Add BASIC_AUTH_CREDENTIALS in Netlify's environment variables (Functions scope) and redeploy.",
      { status: 503, headers: { "Content-Type": "text/plain; charset=utf-8", "Cache-Control": "no-store" } },
    );
  }

  // --- 1. Username and password ---
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

  // --- 2. The TV's private link, for the wall-display files only ---
  const tvKey = (Netlify.env.get("TV_ACCESS_KEY") || "").trim();
  const url = new URL(request.url);
  const tvPath = url.pathname.startsWith("/tv/") || url.pathname === "/ops-logic.js";
  if (tvKey && tvPath) {
    const token = await tokenFor(tvKey);
    const fromLink = url.searchParams.get("tvkey");
    if (fromLink && same(fromLink, tvKey)) {
      context.cookies.set({
        name: TV_COOKIE, value: token, path: "/", httpOnly: true, secure: true, sameSite: "Lax",
        expires: new Date(Date.now() + TV_COOKIE_DAYS * 86400000),
      });
      return context.next();
    }
    const fromCookie = context.cookies.get(TV_COOKIE);
    if (fromCookie && same(fromCookie, token)) return context.next();
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

// The cookie holds a fingerprint of the key, not the key itself.
async function tokenFor(key) {
  const bytes = await crypto.subtle.digest("SHA-256", new TextEncoder().encode("lab-alley-tv:" + key));
  return [...new Uint8Array(bytes)].map(b => b.toString(16).padStart(2, "0")).join("");
}

// Compare without stopping at the first different character, so response
// timing doesn't hint at how much of a guess was right.
function same(a, b) {
  let diff = a.length ^ b.length;
  for (let i = 0; i < Math.max(a.length, b.length); i++) diff |= (a.charCodeAt(i) || 0) ^ (b.charCodeAt(i) || 0);
  return diff === 0;
}

export const config = { path: "/*" };
