"use strict";

(() => {
  const $ = (id) => document.getElementById(id);
  const state = { user: null, weather: null, lastCity: "", searchId: 0, loading: false };

  const ACTIVITY_LABELS = {
    account_created: "Account created",
    forecast_saved: "Saved forecast",
    forecast_updated: "Updated saved forecast",
    forecast_deleted: "Deleted saved forecast",
  };

  class ApiError extends Error {
    constructor(status, code, message) {
      super(message);
      this.status = status;
      this.code = code;
    }
  }

  async function api(path, options = {}) {
    const headers = { Accept: "application/json", "X-Requested-With": "fetch" };
    if (options.body !== undefined) headers["Content-Type"] = "application/json";
    let response;
    try {
      response = await fetch(path, {
        method: options.method || "GET",
        headers,
        body: options.body === undefined ? undefined : JSON.stringify(options.body),
        credentials: "same-origin",
        cache: "no-store",
      });
    } catch {
      throw new ApiError(0, "network_error", "Network error. Check your connection and try again.");
    }
    if (response.status === 204) return null;
    let data = null;
    try {
      data = await response.json();
    } catch {
      // Non-JSON (e.g. an edge error page); handled below.
    }
    if (!response.ok) {
      const error = (data && data.error) || {};
      throw new ApiError(response.status, error.code || "http_error", error.message || "Request failed.");
    }
    return data;
  }

  function el(tag, className, text) {
    const node = document.createElement(tag);
    if (className) node.className = className;
    if (text !== undefined && text !== null) node.textContent = String(text);
    return node;
  }

  function setStatus(message, kind = "info") {
    const status = $("status");
    status.textContent = message || "";
    status.dataset.kind = kind;
  }

  function weatherIcon(id) {
    if (typeof id !== "number") return "🌡️";
    if (id >= 200 && id < 300) return "⛈️";
    if (id >= 300 && id < 400) return "🌦️";
    if (id >= 500 && id < 600) return "🌧️";
    if (id >= 600 && id < 700) return "🌨️";
    if (id >= 700 && id < 800) return "🌫️";
    if (id === 800) return "☀️";
    if (id === 801 || id === 802) return "⛅";
    return "☁️";
  }

  const round = (value) => (typeof value === "number" ? Math.round(value) : "–");
  const placeName = (loc) => [loc.name, loc.state, loc.country].filter(Boolean).join(", ");

  // OpenWeather timestamps are UTC; shift by the city's offset and format as UTC
  // so dates and times are shown in the city's local time.
  function cityDate(unixSeconds, offsetSeconds) {
    return new Date((unixSeconds + offsetSeconds) * 1000);
  }
  const fmt = (options) => new Intl.DateTimeFormat("en-US", { ...options, timeZone: "UTC", hour12: true });
  const fmtTime = fmt({ hour: "2-digit", minute: "2-digit" });
  const fmtDay = fmt({ weekday: "short", day: "numeric", month: "short" });
  const fmtDayKey = fmt({ year: "numeric", month: "2-digit", day: "2-digit" });
  const fmtFull = fmt({ weekday: "long", day: "numeric", month: "long", hour: "2-digit", minute: "2-digit" });
  const fmtActivity = new Intl.DateTimeFormat("en-US", {
    year: "numeric", month: "short", day: "numeric", hour: "2-digit", minute: "2-digit", hour12: true,
  });

  function renderWeather(weather) {
    state.weather = weather;
    const current = weather.current;
    const offset = weather.timezone_offset || 0;
    const condition = (current.weather && current.weather[0]) || {};

    $("current-place").textContent = placeName(weather.location);
    $("current-time").textContent = `Local time ${fmtFull.format(cityDate(current.dt, offset))}`;
    $("current-icon").textContent = weatherIcon(condition.id);
    $("current-temp").textContent = `${round(current.main.temp)}°C`;
    $("current-desc").textContent = condition.description || "";

    const details = $("current-details");
    details.replaceChildren();
    const rows = [
      ["Feels like", `${round(current.main.feels_like)}°C`],
      ["Humidity", `${round(current.main.humidity)}%`],
      ["Wind", current.wind ? `${round(current.wind.speed * 3.6)} km/h` : "–"],
      ["Pressure", `${round(current.main.pressure)} hPa`],
    ];
    if (current.sys && current.sys.sunrise) {
      rows.push(["Sunrise", fmtTime.format(cityDate(current.sys.sunrise, offset))]);
      rows.push(["Sunset", fmtTime.format(cityDate(current.sys.sunset, offset))]);
    }
    for (const [label, value] of rows) {
      const wrap = el("div");
      wrap.append(el("dt", null, label), el("dd", null, value));
      details.append(wrap);
    }

    renderDaily(weather.forecast.items, offset);
    renderHourly(weather.forecast.items, offset);
    $("current").hidden = false;
    updateSaveButton();
  }

  function groupByDay(items, offset) {
    const days = new Map();
    for (const item of items) {
      const date = cityDate(item.dt, offset);
      const key = fmtDayKey.format(date);
      if (!days.has(key)) days.set(key, { date, items: [] });
      days.get(key).items.push(item);
    }
    return [...days.values()];
  }

  function renderDaily(items, offset) {
    const container = $("daily");
    container.replaceChildren();
    for (const day of groupByDay(items, offset)) {
      const temps = day.items.map((i) => i.main.temp).filter((t) => typeof t === "number");
      const pop = Math.max(0, ...day.items.map((i) => (typeof i.pop === "number" ? i.pop : 0)));
      // Use the condition closest to midday as the representative one.
      const midday = day.items.reduce((best, item) => {
        const hour = cityDate(item.dt, offset).getUTCHours();
        const bestHour = cityDate(best.dt, offset).getUTCHours();
        return Math.abs(hour - 12) < Math.abs(bestHour - 12) ? item : best;
      });
      const condition = (midday.weather && midday.weather[0]) || {};
      const card = el("div", "day card");
      card.append(
        el("div", "day-name", fmtDay.format(day.date)),
        el("div", "day-icon", weatherIcon(condition.id)),
        el("div", "day-desc", condition.description || ""),
        el("div", "day-temps", `${round(Math.max(...temps))}° / ${round(Math.min(...temps))}°`),
        el("div", "day-pop muted", `💧 ${Math.round(pop * 100)}%`),
      );
      container.append(card);
    }
    $("daily-section").hidden = false;
  }

  function renderHourly(items, offset) {
    const container = $("hourly");
    container.replaceChildren();
    for (const item of items) {
      const date = cityDate(item.dt, offset);
      const condition = (item.weather && item.weather[0]) || {};
      const cell = el("div", "hour");
      cell.title = condition.description || "";
      cell.append(
        el("div", "hour-day muted", fmtDay.format(date)),
        el("div", "hour-time", fmtTime.format(date)),
        el("div", "hour-icon", weatherIcon(condition.id)),
        el("div", "hour-temp", `${round(item.main.temp)}°`),
      );
      container.append(cell);
    }
    $("hourly-section").hidden = false;
    container.scrollLeft = 0;
    updateHourlyNavigation();
  }

  function updateHourlyNavigation() {
    const container = $("hourly");
    $("hourly-prev").disabled = container.scrollLeft <= 1;
    $("hourly-next").disabled = container.scrollLeft + container.clientWidth >= container.scrollWidth - 1;
  }

  function scrollHours(direction) {
    const container = $("hourly");
    container.scrollBy({
      left: direction * Math.max(90, container.clientWidth * 0.8),
      behavior: window.matchMedia("(prefers-reduced-motion: reduce)").matches ? "instant" : "smooth",
    });
  }

  function updateSaveButton() {
    $("save-button").hidden = !(state.user && state.weather);
    $("save-button").disabled = state.loading;
  }

  async function search(city, refresh = false) {
    const searchId = ++state.searchId;
    state.loading = true;
    updateSaveButton();
    setStatus("Loading forecast…");
    try {
      const weather = await api(`/api/weather?city=${encodeURIComponent(city)}${refresh ? "&refresh=true" : ""}`);
      if (searchId !== state.searchId) return;
      state.lastCity = city;
      renderWeather(weather);
      setStatus("");
    } catch (error) {
      if (searchId !== state.searchId) return;
      setStatus(error.message, "error");
    } finally {
      if (searchId === state.searchId) {
        state.loading = false;
        updateSaveButton();
      }
    }
  }

  async function saveCurrent() {
    if (!state.lastCity) return;
    const button = $("save-button");
    button.disabled = true;
    try {
      const result = await api("/api/favorites", { method: "POST", body: { city: state.lastCity } });
      setStatus(`Saved “${placeName(result.favorite.location)}” to your forecasts.`, "success");
      await Promise.all([loadFavorites(), loadActivity()]);
    } catch (error) {
      handleAuthError(error);
    } finally {
      button.disabled = false;
    }
  }

  function handleAuthError(error) {
    if (error.status === 401) {
      setStatus("Your session has expired. Please sign in again.", "error");
      state.user = null;
      renderAccount({ authenticated: false });
    } else {
      setStatus(error.message, "error");
    }
  }

  async function loadFavorites() {
    const list = $("favorites");
    if (!state.user) {
      list.replaceChildren();
      return;
    }
    try {
      const data = await api("/api/favorites");
      list.replaceChildren();
      $("favorites-hint").textContent = data.favorites.length
        ? "Select a city to get its up-to-date forecast."
        : "No saved forecasts yet. Search a city and press “Save forecast”.";
      for (const favorite of data.favorites) list.append(favoriteItem(favorite));
    } catch (error) {
      $("favorites-hint").textContent = error.message;
    }
  }

  function favoriteItem(favorite) {
    const summary = favorite.summary || {};
    const item = el("li", "favorite");
    const open = el("button", "favorite-open");
    open.type = "button";
    open.title = "Get the latest forecast";
    open.append(
      el("span", "favorite-icon", weatherIcon(summary.condition_id)),
      el("span", "favorite-name", placeName(favorite.location)),
      el("span", "favorite-meta muted", "View latest forecast"),
    );
    open.addEventListener("click", () => {
      $("city-input").value = favorite.city_query;
      search(favorite.city_query, true);
      window.scrollTo({ top: 0, behavior: "smooth" });
    });

    const remove = el("button", "icon-button", "🗑️");
    remove.type = "button";
    remove.title = "Delete";
    remove.setAttribute("aria-label", `Delete ${placeName(favorite.location)}`);
    remove.addEventListener("click", async () => {
      if (!window.confirm(`Delete “${placeName(favorite.location)}” from your saved forecasts?`)) return;
      try {
        await api(`/api/favorites/${favorite.id}`, { method: "DELETE" });
        await Promise.all([loadFavorites(), loadActivity()]);
      } catch (error) {
        handleAuthError(error);
      }
    });

    item.append(open, remove);
    return item;
  }

  async function loadActivity() {
    if (!state.user) return;
    const list = $("activity");
    try {
      const data = await api("/api/activity");
      list.replaceChildren();
      if (!data.activity.length) list.append(el("li", "muted", "No activity yet."));
      for (const entry of data.activity) {
        const label = ACTIVITY_LABELS[entry.action] || entry.action;
        const where = entry.details && entry.details.location ? `: ${entry.details.location}` : "";
        const li = el("li");
        li.append(el("span", null, `${label}${where}`), el("time", "muted", fmtActivity.format(new Date(entry.at))));
        list.append(li);
      }
    } catch (error) {
      list.replaceChildren(el("li", "muted", error.message));
    }
  }

  async function deleteAllData() {
    if (!window.confirm("Permanently delete your saved forecasts and activity history?")) return;
    try {
      await api("/api/me", { method: "DELETE" });
      setStatus("All your data was deleted.", "success");
      await Promise.all([loadFavorites(), loadActivity()]);
    } catch (error) {
      handleAuthError(error);
    }
  }

  // Only same-origin relative paths are accepted for login/logout links.
  const safeLocalUrl = (url) => (typeof url === "string" && url.startsWith("/") && !url.startsWith("//") ? url : null);

  function renderAccount(me) {
    const login = safeLocalUrl(me.login_url);
    const logout = safeLocalUrl(me.logout_url);
    $("user-name").textContent = me.authenticated ? `Hi, ${me.name}` : "";
    $("login-link").hidden = me.authenticated || !login;
    if (login) $("login-link").href = login;
    $("logout-link").hidden = !me.authenticated || !logout;
    if (logout) $("logout-link").href = logout;
    $("activity-card").hidden = !me.authenticated;
    $("delete-data").hidden = !me.authenticated;
    if (!me.authenticated) {
      $("favorites").replaceChildren();
      $("favorites-hint").textContent = "Sign in to save forecasts and see them here.";
    } else if (me.storage_available === false) {
      $("favorites-hint").textContent = "Saved forecasts are not available right now.";
    }
    updateSaveButton();
  }

  async function init() {
    $("search-form").addEventListener("submit", (event) => {
      event.preventDefault();
      const city = $("city-input").value.trim();
      if (city) search(city);
    });
    $("save-button").addEventListener("click", saveCurrent);
    $("delete-data").addEventListener("click", deleteAllData);
    $("hourly-prev").addEventListener("click", () => scrollHours(-1));
    $("hourly-next").addEventListener("click", () => scrollHours(1));
    $("hourly").addEventListener("scroll", updateHourlyNavigation, { passive: true });
    new ResizeObserver(updateHourlyNavigation).observe($("hourly"));
    $("activity-details").addEventListener("toggle", (event) => {
      if (event.target.open) loadActivity();
    });

    try {
      const me = await api("/api/me");
      state.user = me.authenticated ? me : null;
      renderAccount(me);
      if (state.user && me.storage_available !== false) await loadFavorites();
    } catch {
      renderAccount({ authenticated: false });
    }
  }

  document.addEventListener("DOMContentLoaded", init);
})();
