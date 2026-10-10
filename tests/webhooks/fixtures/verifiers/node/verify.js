// Verify a Talent OS webhook (TalentOS-Signature: t=<ts>,v1=<hex>[,v1=<hex>]). Pass the raw request body.
const crypto = require("crypto");

function verify(secret, header, rawBody, toleranceSeconds = 300) {
  const parts = header.split(",").map((p) => p.split("=", 2));
  const ts = (parts.find(([k]) => k === "t") || [])[1];
  if (!ts || Math.abs(Date.now() / 1000 - Number(ts)) > toleranceSeconds) return false;
  const expected = crypto.createHmac("sha256", secret).update(`${ts}.`).update(rawBody).digest("hex");
  return parts.some(([k, v]) => k === "v1" && v.length === expected.length &&
    crypto.timingSafeEqual(Buffer.from(v), Buffer.from(expected)));
}

module.exports = { verify };
