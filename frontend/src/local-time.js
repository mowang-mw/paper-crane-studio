const TIMEZONE_SUFFIX = /(?:Z|[+-]\d{2}:?\d{2})$/i;

export function parseApiUtcDateTime(value) {
  if (typeof value !== "string" || !value.trim()) return null;
  const trimmed = value.trim();
  const normalized = TIMEZONE_SUFFIX.test(trimmed) ? trimmed : `${trimmed}Z`;
  const parsed = new Date(normalized);
  return Number.isNaN(parsed.getTime()) ? null : parsed;
}

export function formatLocalDateTime(value, options = {}) {
  if (!value) return "尚未检查";
  const parsed = parseApiUtcDateTime(value);
  return parsed
    ? parsed.toLocaleString("zh-CN", { hour12: false, ...options })
    : value;
}
