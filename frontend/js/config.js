(function () {
  "use strict";

  var DEFAULT_PORT = 8001;
  var STORAGE_KEY = "cus_backend_url";

  // Cache-busting token: every page must load this file as config.js?v=<CUS_CONFIG_VERSION>.
  // Increment it each time this file's behaviour changes so browsers that cached an
  // older config.js (e.g. one that still rebuilt "http://localhost:8001" and therefore
  // depended on IPv6/IPv4-ambiguous localhost resolution) are forced to fetch the new
  // copy. The value is intentionally high so no already-cached copy (old files shipped
  // as ?v=0..3) can collide with it. backend/tests/test_backend_connectivity.py asserts
  // this constant equals the ?v= used by every frontend page.
  var CUS_CONFIG_VERSION = "4";

  function detectBaseUrl() {
    // 1. Check localStorage override
    try {
      var stored = localStorage.getItem(STORAGE_KEY);
      if (stored) return stored.replace(/\/+$/, "");
    } catch (_) {}

    // 2. Check <meta name="cus-backend-url">
    var meta = document.querySelector('meta[name="cus-backend-url"]');
    if (meta) return meta.getAttribute("content").replace(/\/+$/, "");

    // 3. If the page itself was served by the backend on the backend's own
    //    port (uvicorn serves the site and the API from the same origin), reuse
    //    the exact origin the browser already reached successfully instead of
    //    rebuilding "http://<hostname>:DEFAULT_PORT". Rebuilding from the
    //    hostname re-introduces localhost IPv6/IPv4 ambiguity: this backend
    //    listens on IPv4 (0.0.0.0), so ::1:8001 is unreachable while the
    //    origin used to load this page is reachable by definition.
    if (window.location.port === String(DEFAULT_PORT)) {
      return window.location.origin.replace(/\/+$/, "");
    }

    // 4. Page served separately (python -m http.server, live-server, file://...).
    //    Use a deterministic IPv4 loopback on the local machine: "localhost"
    //    can resolve to ::1 first (Windows getaddrinfo prefers IPv6), and the
    //    backend binds IPv4 only, so 127.0.0.1:DEFAULT_PORT is the reachable
    //    loopback address. For pages served from a remote hostname keep using
    //    that hostname so the backend is addressed on the same network host.
    var host = window.location.hostname || "127.0.0.1";
    if (host === "localhost") host = "127.0.0.1";
    return "http://" + host + ":" + DEFAULT_PORT;
  }

  window.CUS_API_BASE = detectBaseUrl();
})();
