// Shared settings for all three wall displays.
// Paste the same public anon key Ops Live uses (Supabase: Project Settings > API).
// Never put the service_role key here.
window.TV_CONFIG = {
  SUPABASE_URL: "https://pdflsmequzgpeuodnxyg.supabase.co",
  SUPABASE_ANON_KEY: "YOUR_ANON_KEY_HERE",

  // Optional: an endpoint returning {"pct": 92.5} for % Shipped <48 Hours.
  // Leave blank and that tile stays hidden on the Company screen.
  SHIPPED_48H_URL: "",

  // Shift hours (Central) used to pace progress through the day
  SHIP_SHIFT: [8, 17],
  BUILD_SHIFT: [8, 16],
  // Until a daily goal is set in kpi_targets, target = last week's daily average x this
  BASELINE_MULT: 1.10,

  // Colors. Paste Lab Alley's official hex values here to override the defaults.
  // shipped / built / orders color-code the three sections on every screen;
  // good / warn / bad are reserved for on-track / watch / off-track.
  COLORS: {
    // bg: "#081A33",      // page background (navy)
    // panel: "#0F2747",   // card background
    // shipped: "#3D8FF2", // Lab Alley blue
    // built: "#A47FF2",
    // orders: "#2FC6D4",
  },

  TICKER_SPEED: 110,   // scrolling safety ticker, pixels per second

  REFRESH_MS: 60000,   // how often the queue reloads
  TICKER_MS: 10000,    // Company screen: ms per ticker message
  PANEL_MS: 15000,     // seconds per announcement on the Company screen
};
