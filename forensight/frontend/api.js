const configured = window.FORENSIGHT_CONFIG?.apiUrl || "";
const base = configured.replace(/\/+$/, "");
export async function api(path, options = {}) {
  const controller = new AbortController();
  const timer = setTimeout(() => controller.abort(), 20000);
  try {
    const isForm = typeof FormData !== "undefined" && options.body instanceof FormData;
    const response = await fetch(base + path, {
      ...options,
      signal: controller.signal,
      headers: { Accept: "application/json", ...(options.body && !isForm ? { "Content-Type": "application/json" } : {}), ...options.headers }
    });
    const payload = await response.json().catch(() => ({}));
    if (!response.ok) throw new Error(payload.detail || "Request failed (" + response.status + ")");
    return payload;
  } catch (error) {
    if (error.name === "AbortError") throw new Error("The backend took too long to respond. Please retry.");
    if (error instanceof TypeError) throw new Error("The backend is unavailable. Check the API URL and try again.");
    throw error;
  } finally {
    clearTimeout(timer);
  }
}
export function query(params) {
  const values = new URLSearchParams();
  Object.entries(params).forEach(([key, value]) => {
    if (value !== undefined && value !== null && value !== "") values.set(key, value);
  });
  return values.toString() ? "?" + values : "";
}

