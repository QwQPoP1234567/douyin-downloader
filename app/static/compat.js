(function exposeIdempotencyKey(root) {
  function createIdempotencyKey(cryptoProvider = root.crypto) {
    if (typeof cryptoProvider?.randomUUID === "function") {
      try { return cryptoProvider.randomUUID(); } catch (_) {}
    }
    const bytes = new Uint8Array(16);
    if (typeof cryptoProvider?.getRandomValues === "function") {
      cryptoProvider.getRandomValues(bytes);
    } else {
      for (let index = 0; index < bytes.length; index++) {
        bytes[index] = Math.floor(Math.random() * 256);
      }
    }
    bytes[6] = (bytes[6] & 0x0f) | 0x40;
    bytes[8] = (bytes[8] & 0x3f) | 0x80;
    const hex = [...bytes].map(value => value.toString(16).padStart(2, "0"));
    return `${hex.slice(0, 4).join("")}-${hex.slice(4, 6).join("")}-${hex.slice(6, 8).join("")}-${hex.slice(8, 10).join("")}-${hex.slice(10).join("")}`;
  }

  root.createIdempotencyKey = createIdempotencyKey;
  if (typeof module === "object" && module.exports) {
    module.exports = createIdempotencyKey;
  }
})(typeof globalThis === "object" ? globalThis : window);
